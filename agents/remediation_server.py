"""Antigravity SDK remediation agent and A2A 1.0 JSON-RPC server."""
import asyncio
import base64
import json
import logging
import os
import re
import tempfile
import uuid
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from google.antigravity import Agent, CapabilitiesConfig, LocalAgentConfig
from google.antigravity.hooks import policy
from pydantic import ValidationError

from agents.contracts import RemediationRequest, RemediationResponse

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
DEFAULT_REMEDIATION_MODEL = "gemini-4-neon"
MAX_FILE_CHARS = 60_000
MAX_LISTED_FILES = 40
MAX_STORED_TASKS = 200


def parse_repo_url(repo_url: str) -> tuple[str, str]:
    """Extract (owner, repo) from https://github.com/owner/repo[.git]."""
    parts = [p for p in urlparse(repo_url).path.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"Not a GitHub repository URL: {repo_url}")
    return parts[0], re.sub(r"\.git$", "", parts[1])


def is_repo_allowed(repo_url: str, allowed_repos: list[str] | None = None) -> bool:
    """Return True if repo_url matches REMEDIATION_ALLOWED_REPOS (or if allow-list is empty)."""
    if allowed_repos is None:
        raw = os.getenv("REMEDIATION_ALLOWED_REPOS", "").strip()
        allowed_repos = [entry.strip() for entry in raw.split(",") if entry.strip()] if raw else []
    if not allowed_repos:
        return True
    try:
        owner, repo = parse_repo_url(repo_url)
    except ValueError:
        return False
    for allowed in allowed_repos:
        try:
            a_owner, a_repo = parse_repo_url(allowed)
        except ValueError:
            continue
        if owner.lower() == a_owner.lower() and repo.lower() == a_repo.lower():
            return True
    return False


def extract_json(text: str) -> dict[str, Any]:
    """Extract the first JSON object from a model response."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(match.group(0) if match else text)


class GitHubRepo:
    """Minimal GitHub REST API client for reading repo files and opening PRs/issues."""

    def __init__(
        self,
        repo_url: str,
        token: str,
        base_branch: str = "main",
        client: httpx.Client | None = None,
    ):
        self.owner, self.repo = parse_repo_url(repo_url)
        self.base_branch = base_branch or "main"
        self._owns_client = client is None
        self._http = client or httpx.Client(
            base_url=GITHUB_API,
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        self._paths: list[str] | None = None

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    @property
    def _slug(self) -> str:
        return f"/repos/{self.owner}/{self.repo}"

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        resp = self._http.request(method, f"{self._slug}{path}", **kwargs)
        if resp.status_code >= 400:
            raise RuntimeError(f"GitHub {method} {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def list_paths(self) -> list[str]:
        if self._paths is None:
            tree = self._request("GET", f"/git/trees/{self.base_branch}", params={"recursive": "1"})
            self._paths = [e["path"] for e in tree.get("tree", []) if e.get("type") == "blob"]
        return self._paths

    def find_files(self, name_fragment: str) -> list[str]:
        needle = name_fragment.lower()
        return [p for p in self.list_paths() if needle in p.lower()][:MAX_LISTED_FILES]

    def read_file(self, path: str) -> str:
        data = self._request("GET", f"/contents/{path}", params={"ref": self.base_branch})
        return base64.b64decode(data["content"]).decode("utf-8")

    def create_pull_request(
        self, path: str, new_content: str, title: str, body: str, branch: str,
    ) -> str:
        base_sha = self._request("GET", f"/git/ref/heads/{self.base_branch}")["object"]["sha"]
        self._request("POST", "/git/refs", json={"ref": f"refs/heads/{branch}", "sha": base_sha})
        current = self._request("GET", f"/contents/{path}", params={"ref": branch})
        self._request(
            "PUT",
            f"/contents/{path}",
            json={
                "message": title,
                "content": base64.b64encode(new_content.encode("utf-8")).decode("ascii"),
                "sha": current["sha"],
                "branch": branch,
            },
        )
        pr = self._request(
            "POST",
            "/pulls",
            json={"title": title, "head": branch, "base": self.base_branch, "body": body},
        )
        return pr["html_url"]

    def create_issue(self, title: str, body: str, labels: list[str] | None = None) -> str:
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        issue = self._request("POST", "/issues", json=payload)
        return issue["html_url"]


def _make_tools(gh: GitHubRepo):
    def find_repo_files(name_fragment: str) -> str:
        """
        Lists repository file paths whose path contains the given fragment (case-insensitive).
        Use the class or file name from a stack trace, e.g. 'MetricsResource'.

        Args:
            name_fragment: Part of a file path or file name to search for.
        """
        try:
            matches = gh.find_files(name_fragment)
        except Exception as e:
            return f"Failed to list repository files: {e}"
        return "\n".join(matches) if matches else f"No files matching '{name_fragment}'."

    def read_repo_file(path: str) -> str:
        """
        Reads the UTF-8 contents of a file from the GitHub repository.

        Args:
            path: Exact repository-relative path returned by find_repo_files.
        """
        try:
            content = gh.read_file(path)
        except Exception as e:
            return f"Failed to read {path}: {e}"
        return content[:MAX_FILE_CHARS]

    return [find_repo_files, read_repo_file]


class AntigravityRemediationAgent:
    """Uses the Antigravity Python SDK Agent to diagnose and propose a minimal code fix."""

    def __init__(self, model: str | None = None, api_key: str | None = None):
        self.model = (
            model
            or os.getenv("REMEDIATION_MODEL")
            or os.getenv("GEMINI_MODEL")
            or DEFAULT_REMEDIATION_MODEL
        )
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")

    async def propose_fix(self, gh: GitHubRepo, request: RemediationRequest) -> dict[str, Any]:
        app_data_dir = os.getenv("ANTIGRAVITY_APP_DATA_DIR") or tempfile.mkdtemp(prefix="antigravity_remediation_")
        config = LocalAgentConfig(
            system_instructions=(
                "You are a Remediation Agent. A canary deployment was rejected because of a bug in the "
                "application source code. Locate the offending source file in the GitHub repository using the "
                "tools 'find_repo_files' and 'read_repo_file', then fix the bug with the smallest possible "
                "change: keep the existing structure, flags, logging and behavior, and only add the missing "
                "guard or correction (for example a null check). Never delete or rewrite surrounding code, "
                "and keep the diff to a few lines. Do NOT use any other tool, do not run commands, and do "
                "not read local files. Be efficient: use at most a couple of find_repo_files calls and read "
                "only the relevant source file(s).\n\n"
                "Respond ONLY with a single JSON object with these fields:\n"
                '{"path": "repository-relative path of the file you fixed",\n'
                ' "newContent": "the COMPLETE new content of that file",\n'
                ' "title": "short pull request title",\n'
                ' "summary": "what was wrong and what you changed"}\n'
                'If you cannot confidently fix the bug, respond with {"path": null}.'
            ),
            tools=_make_tools(gh),
            capabilities=CapabilitiesConfig(enable_subagents=False, enabled_tools=[]),
            policies=[policy.deny_all(), policy.allow("find_repo_files"), policy.allow("read_repo_file")],
            workspaces=[],
            app_data_dir=app_data_dir,
            model=self.model,
            api_key=self.api_key,
        )
        prompt = (
            f"Canary rollout failed with title: {request.title}\n\n"
            f"Root cause:\n{request.rootCause}\n\n"
            f"Analysis:\n{request.analysis}\n\n"
            f"Diagnostic context (logs/evidence):\n{request.diagnosticContext}\n\n"
            "Find the source file, fix the bug, and return the JSON object."
        )
        async with Agent(config) as agent:
            response = await agent.chat(prompt)
            return extract_json(await response.text())


class RemediationService:
    """Validates remediation requests and orchestrates GitHub PR/Issue creation."""

    def __init__(
        self,
        agent: AntigravityRemediationAgent | None = None,
        repo_factory=GitHubRepo,
    ):
        self.agent = agent or AntigravityRemediationAgent()
        self.repo_factory = repo_factory

    async def remediate(self, request: RemediationRequest) -> RemediationResponse:
        if not request.repoUrl or not is_repo_allowed(request.repoUrl):
            return RemediationResponse(
                status="rejected",
                message=f"Repository is not in REMEDIATION_ALLOWED_REPOS: {request.repoUrl}",
            )

        token = os.getenv("GITHUB_TOKEN", "").strip()
        if not token:
            return RemediationResponse(
                status="infra_error",
                message="GITHUB_TOKEN is not configured",
            )

        gh = self.repo_factory(request.repoUrl, token, request.baseBranch or "main")
        try:
            if request.issueCategory == "OPERATIONAL":
                title = f"Operational issue: {request.title[:80]}"
                body = (
                    f"Operational issue detected by canary analysis.\n\n"
                    f"**Root cause**\n{request.rootCause}\n\n"
                    f"**Analysis**\n{request.analysis}\n\n"
                    f"**Diagnostic context**\n```\n{request.diagnosticContext[:2000]}\n```"
                )
                issue_url = await asyncio.to_thread(gh.create_issue, title, body)
                return RemediationResponse(
                    status="issue",
                    issueLink=issue_url,
                    message="Operational issue recorded on GitHub",
                )

            if not (self.agent.api_key or os.getenv("GEMINI_API_KEY")):
                return RemediationResponse(
                    status="infra_error",
                    message="GEMINI_API_KEY is not configured",
                )

            fix = await self.agent.propose_fix(gh, request)
            path = fix.get("path")
            new_content = fix.get("newContent")
            if not path or not new_content:
                title = f"Canary failure: {request.title[:80]}"
                body = (
                    f"Automated remediation could not produce a confident code patch.\n\n"
                    f"**Root cause**\n{request.rootCause}\n\n"
                    f"**Analysis**\n{request.analysis}"
                )
                issue_url = await asyncio.to_thread(gh.create_issue, title, body)
                return RemediationResponse(
                    status="issue",
                    issueLink=issue_url,
                    message="No confident automated fix found; opened GitHub issue",
                )

            branch = f"fix/remediation-{uuid.uuid4().hex[:8]}"
            title = f"Fix: {fix.get('title') or request.title[:80]}"
            summary = fix.get("summary") or "Automated fix proposed by Antigravity Remediation Agent."
            body = (
                f"Automated fix proposed by the Antigravity Remediation Agent.\n\n"
                f"**Root cause**\n{request.rootCause}\n\n"
                f"**Change**\n{summary}\n\n"
                f"**Analysis**\n{request.analysis}"
            )
            try:
                pr_url = await asyncio.to_thread(
                    gh.create_pull_request, path, new_content, title, body, branch,
                )
                return RemediationResponse(
                    status="success",
                    prLink=pr_url,
                    message=summary,
                )
            except Exception as exc:
                logger.exception("Pull request creation failed")
                try:
                    issue_url = await asyncio.to_thread(
                        gh.create_issue,
                        title,
                        f"{body}\n\n**PR creation error**\n{exc}",
                    )
                except Exception:
                    issue_url = None
                return RemediationResponse(
                    status="pr_failed",
                    issueLink=issue_url,
                    message=str(exc),
                )
        except Exception as exc:
            logger.exception("Remediation failed")
            return RemediationResponse(
                status="agent_error",
                message=str(exc),
            )
        finally:
            if hasattr(gh, "close"):
                gh.close()


class A2ATaskStore:
    """In-memory bounded store for A2A 1.0 tasks."""

    def __init__(self, max_tasks: int = MAX_STORED_TASKS):
        self.max_tasks = max_tasks
        self._tasks: dict[str, dict[str, Any]] = {}
        self._background: set[asyncio.Task[Any]] = set()

    def create(self) -> str:
        task_id = str(uuid.uuid4())
        if len(self._tasks) >= self.max_tasks:
            oldest = next(iter(self._tasks))
            self._tasks.pop(oldest, None)
        self._tasks[task_id] = {
            "id": task_id,
            "status": {"state": "TASK_STATE_WORKING"},
        }
        return task_id

    def get(self, task_id: str) -> dict[str, Any] | None:
        return self._tasks.get(task_id)

    def complete(self, task_id: str, response: RemediationResponse) -> None:
        self._tasks[task_id] = {
            "id": task_id,
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [
                {"parts": [{"kind": "text", "text": response.model_dump_json()}]},
            ],
        }

    def fail(self, task_id: str, message: str) -> None:
        self._tasks[task_id] = {
            "id": task_id,
            "status": {"state": "TASK_STATE_FAILED", "message": message},
        }

    def track(self, task: asyncio.Task[Any]) -> None:
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def shutdown(self) -> None:
        tasks = list(self._background)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _extract_request_text(params: dict[str, Any]) -> str:
    message = params.get("message")
    if not isinstance(message, dict):
        raise ValueError("Missing params.message")
    parts = message.get("parts")
    if not isinstance(parts, list):
        raise ValueError("Missing params.message.parts")
    text = "".join(
        p.get("text", "")
        for p in parts
        if isinstance(p, dict) and p.get("kind") in (None, "text") and isinstance(p.get("text"), str)
    ).strip()
    if not text:
        raise ValueError("Empty remediation request message")
    return text


def _rpc_error(request_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content={"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}},
    )


def register_remediation_routes(
    app: FastAPI,
    service: RemediationService | None = None,
    task_store: A2ATaskStore | None = None,
) -> A2ATaskStore:
    """Register A2A 1.0 agent-card and JSON-RPC routes on the FastAPI app."""
    svc = service or RemediationService()
    store = task_store or A2ATaskStore()

    @app.get("/.well-known/agent-card.json")
    def agent_card():
        return {
            "name": "Antigravity Remediation Agent",
            "description": (
                "Remediates failed canary rollouts using the Antigravity Python SDK: "
                "inspects the repository, proposes a minimal fix, and opens a GitHub PR or issue."
            ),
            "version": "1.0.0",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "skills": [
                {
                    "id": "remediate",
                    "name": "Remediate canary rollback",
                    "description": (
                        "Accepts a JSON RemediationRequest and returns a JSON RemediationResponse "
                        "with prLink, issueLink, and message."
                    ),
                    "tags": ["remediation", "canary", "github", "antigravity"],
                }
            ],
            "defaultInputModes": ["text"],
            "defaultOutputModes": ["text"],
        }

    @app.post("/")
    async def a2a_jsonrpc(http_request: Request):
        try:
            payload = await http_request.json()
        except Exception:
            return _rpc_error(None, -32700, "Parse error")
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
            return _rpc_error(payload.get("id") if isinstance(payload, dict) else None, -32600, "Invalid Request")

        req_id = payload.get("id")
        method = payload.get("method")
        params = payload.get("params") or {}
        if not isinstance(params, dict):
            return _rpc_error(req_id, -32602, "Invalid params")

        if method == "SendMessage":
            try:
                raw_text = _extract_request_text(params)
                remediation_req = RemediationRequest.model_validate_json(raw_text)
            except (ValueError, ValidationError) as exc:
                return _rpc_error(req_id, -32602, f"Invalid RemediationRequest: {exc}")

            task_id = store.create()

            async def _run():
                try:
                    response = await svc.remediate(remediation_req)
                    store.complete(task_id, response)
                except Exception as exc:
                    logger.exception("Unhandled error in remediation task %s", task_id)
                    store.fail(task_id, str(exc))

            store.track(asyncio.create_task(_run()))
            return {"jsonrpc": "2.0", "id": req_id, "result": {"task": store.get(task_id)}}

        if method == "GetTask":
            task_id = params.get("id")
            if not isinstance(task_id, str) or not task_id:
                return _rpc_error(req_id, -32602, "Missing task id")
            task = store.get(task_id)
            if task is None:
                return _rpc_error(req_id, -32001, f"Task not found: {task_id}")
            return {"jsonrpc": "2.0", "id": req_id, "result": {"task": task}}

        return _rpc_error(req_id, -32601, f"Method not found: {method}")

    return store
