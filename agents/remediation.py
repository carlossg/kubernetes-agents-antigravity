"""Remediation: turn a code-bug root cause into a GitHub pull request.

Only invoked by the orchestrator when the consensus verdict is "do not promote"
AND the root cause is a bug in application source code (a stack trace pointing
into the repo). Operational problems such as memory leaks are left alone.

Flow: a RemediationAgent explores the repo through read-only tools, returns the
fixed file as JSON, and plain code (not the LLM) creates the branch, commit and
pull request through the GitHub REST API.
"""
import base64
import json
import os
import re
import uuid
from urllib.parse import urlparse

import httpx
from google.antigravity import Agent, LocalAgentConfig

from agents import activity

GITHUB_API = "https://api.github.com"
MAX_FILE_CHARS = 60_000
MAX_LISTED_FILES = 40


def parse_repo_url(repo_url: str) -> tuple[str, str]:
    """Extract (owner, repo) from https://github.com/owner/repo[.git]."""
    parts = [p for p in urlparse(repo_url).path.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"Not a GitHub repository URL: {repo_url}")
    return parts[0], re.sub(r"\.git$", "", parts[1])


class GitHubRepo:
    def __init__(self, repo_url: str, token: str, base_branch: str = "main", client: httpx.Client | None = None):
        self.owner, self.repo = parse_repo_url(repo_url)
        self.base_branch = base_branch or "main"
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

    @property
    def _slug(self) -> str:
        return f"/repos/{self.owner}/{self.repo}"

    def _request(self, method: str, path: str, **kwargs) -> dict:
        resp = self._http.request(method, f"{self._slug}{path}", **kwargs)
        if resp.status_code >= 400:
            raise RuntimeError(f"GitHub {method} {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def list_paths(self) -> list[str]:
        if self._paths is None:
            tree = self._request("GET", f"/git/trees/{self.base_branch}", params={"recursive": "1"})
            self._paths = [e["path"] for e in tree.get("tree", []) if e["type"] == "blob"]
        return self._paths

    def find_files(self, name_fragment: str) -> list[str]:
        needle = name_fragment.lower()
        return [p for p in self.list_paths() if needle in p.lower()][:MAX_LISTED_FILES]

    def read_file(self, path: str) -> str:
        data = self._request("GET", f"/contents/{path}", params={"ref": self.base_branch})
        return base64.b64decode(data["content"]).decode("utf-8")

    def create_pull_request(self, path: str, new_content: str, title: str, body: str, branch: str) -> str:
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


def extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(match.group(0) if match else text)


def _make_tools(gh: GitHubRepo):
    def find_repo_files(name_fragment: str) -> str:
        """
        Lists repository file paths whose path contains the given fragment (case-insensitive).
        Use the class or file name from a stack trace, e.g. 'MetricsResource'.

        Args:
            name_fragment: Part of a file path or file name to search for.
        """
        activity.feed.emit(activity.TOOL_CALL, "find_repo_files", name_fragment)
        try:
            matches = gh.find_files(name_fragment)
        except Exception as e:
            return f"Failed to list repository files: {e}"
        result = "\n".join(matches) if matches else "No matching files"
        activity.feed.emit(activity.TOOL_RESULT, "find_repo_files", result[:500])
        return result

    def read_repo_file(path: str) -> str:
        """
        Reads a file from the repository's base branch and returns its full content.

        Args:
            path: Repository-relative file path, e.g. 'src/main/java/Foo.java'.
        """
        activity.feed.emit(activity.TOOL_CALL, "read_repo_file", path)
        try:
            content = gh.read_file(path)
        except Exception as e:
            return f"Failed to read {path}: {e}"
        activity.feed.emit(activity.TOOL_RESULT, "read_repo_file", f"{path} ({len(content)} chars)")
        return content[:MAX_FILE_CHARS]

    return [find_repo_files, read_repo_file]


class RemediationAgent:
    def __init__(self, model: str | None = None):
        self.model = model or os.getenv("REMEDIATION_AGENT_MODEL")

    async def propose_fix(self, gh: GitHubRepo, rollout_name: str, root_cause: str, logs_report: str) -> dict:
        config = LocalAgentConfig(
            system_instructions=(
                "You are a Remediation Agent. A canary deployment was rejected because of a bug in the "
                "application source code. Locate the offending source file in the GitHub repository using the "
                "tools 'find_repo_files' and 'read_repo_file', then fix the bug with the smallest possible change. "
                "Do NOT use any other tool, do not run commands, and do not read local files. "
                "Be efficient: use at most one find_repo_files call and read only the single most relevant file.\n\n"
                "Respond ONLY with a single JSON object with these fields:\n"
                '{"path": "repository-relative path of the file you fixed",\n'
                ' "newContent": "the COMPLETE new content of that file",\n'
                ' "title": "short pull request title",\n'
                ' "summary": "what was wrong and what you changed"}\n'
                "If you cannot confidently fix the bug, respond with {\"path\": null}."
            ),
            tools=_make_tools(gh),
            model=self.model,
        )
        prompt = (
            f"Rollout '{rollout_name}' canary failed with this root cause:\n{root_cause}\n\n"
            f"Relevant log analysis:\n{logs_report}\n\n"
            "Find the source file, fix the bug, and return the JSON object."
        )
        async with Agent(config) as agent:
            response = await agent.chat(prompt)
            return extract_json(await response.text())


async def remediate(
    repo_url: str,
    base_branch: str,
    rollout_name: str,
    namespace: str,
    root_cause: str,
    logs_report: str,
    analysis: str,
    model: str | None = None,
) -> str | None:
    """Open a fix PR and return its URL, or None when remediation is skipped."""
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        activity.feed.emit(activity.REMEDIATION, "Skipped: GITHUB_TOKEN is not configured")
        return None

    activity.feed.emit(activity.AGENT_START, "RemediationAgent", f"Investigating {repo_url}")
    try:
        gh = GitHubRepo(repo_url, token, base_branch)
        fix = await RemediationAgent(model=model).propose_fix(gh, rollout_name, root_cause, logs_report)
        path = fix.get("path")
        if not path or not fix.get("newContent"):
            activity.feed.emit(activity.REMEDIATION, "No confident fix found", root_cause)
            return None

        branch = f"fix/rollout-{rollout_name}-{uuid.uuid4().hex[:8]}"
        title = f"Fix: {fix.get('title') or root_cause[:80]}"
        body = (
            f"Automated fix proposed by the Kubernetes AI/Ops agents after canary rollout "
            f"`{namespace}/{rollout_name}` was rejected.\n\n"
            f"**Root cause**\n{root_cause}\n\n**Change**\n{fix.get('summary', '')}\n\n"
            f"**Analysis**\n{analysis}"
        )
        pr_url = gh.create_pull_request(path, fix["newContent"], title, body, branch)
        activity.feed.emit(activity.REMEDIATION, f"Pull request created: {title}", pr_url)
        activity.feed.emit(activity.AGENT_COMPLETE, "RemediationAgent", pr_url)
        return pr_url
    except Exception as e:
        activity.feed.emit(activity.ERROR, "Remediation failed", str(e))
        return None
