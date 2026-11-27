"""Delegate the final rollback to a separate sandboxed remediation service over A2A."""
import asyncio
import os
import re
import uuid

import httpx

from agents import activity
from agents.contracts import RemediationRequest, RemediationResponse

_background_tasks: set[asyncio.Task] = set()
FAILED_STATUSES = {"tests_failed", "agent_error", "rejected", "timeout", "infra_error", "pr_failed"}


def parse_artifact(result: dict) -> RemediationResponse:
    if "task" in result and isinstance(result["task"], dict):
        result = result["task"]
    elif "message" in result and isinstance(result["message"], dict):
        result = result["message"]

    if result.get("kind") == "message" or ("parts" in result and "artifacts" not in result):
        parts = result.get("parts", [])
    else:
        artifacts = result.get("artifacts")
        if not isinstance(artifacts, list) or any(
            not isinstance(a, dict) or not isinstance(a.get("parts"), list) for a in artifacts
        ):
            raise ValueError("Invalid A2A artifacts")
        parts = [p for a in artifacts for p in a["parts"]]
    if not isinstance(parts, list) or any(
        not isinstance(p, dict) or not isinstance(p.get("text", ""), str) for p in parts
    ):
        raise ValueError("Invalid A2A text parts")
    text = "".join(
        p.get("text", "")
        for p in parts
        if p.get("kind") in (None, "text") and p.get("text")
    )
    response = RemediationResponse.model_validate_json(text)
    if response.status == "success" and not response.prLink:
        raise ValueError("Successful remediation did not return a PR")
    if response.status == "issue" and not response.issueLink:
        raise ValueError("Issue remediation did not return an issue")
    for link in (response.prLink, response.issueLink):
        if link and not re.fullmatch(r"https://github\.com/[^/\s]+/[^/\s]+/(pull|issues)/[1-9][0-9]*", link):
            raise ValueError("Invalid remediation artifact URL")
    return response


async def call_remediation(
    request: RemediationRequest, url: str, http_client: httpx.AsyncClient | None = None,
) -> RemediationResponse:
    async def run(http):
        async def rpc(method: str, params: dict):
            request_id = str(uuid.uuid4())
            headers = {"A2A-Version": "1.0"}
            token = os.getenv("REMEDIATION_AGENT_TOKEN")
            if token:
                headers["Authorization"] = f"Bearer {token}"
            response = await http.post(url, headers=headers, json={
                "jsonrpc": "2.0", "id": request_id, "method": method, "params": params,
            })
            response.raise_for_status()
            payload = response.json()
            if (not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0"
                    or payload.get("id") != request_id):
                raise ValueError("Invalid A2A JSON-RPC response")
            if payload.get("error"):
                raise ValueError(f"A2A error: {payload['error']}")
            result = payload.get("result")
            if not isinstance(result, dict):
                raise ValueError("Missing A2A result")
            return result

        result = await rpc("SendMessage", {
            "message": {
                "messageId": str(uuid.uuid4()), "role": "ROLE_USER",
                "parts": [{"text": request.model_dump_json()}],
            },
            "configuration": {"blocking": True, "acceptedOutputModes": ["text/plain", "application/json"]},
        })

        def _unwrap_task(res: dict) -> dict | None:
            if isinstance(res.get("task"), dict):
                return res["task"]
            if res.get("kind") == "task":
                return res
            return None

        while (task := _unwrap_task(result)) is not None:
            status = task.get("status")
            if not isinstance(status, dict):
                raise ValueError("Missing A2A task status")
            state = status.get("state")
            if state in {"completed", "TASK_STATE_COMPLETED"}:
                break
            if state not in {"submitted", "working", "TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"} or not task.get("id"):
                raise ValueError(f"Remediation task did not complete: {state}")
            await asyncio.sleep(2)
            result = await rpc("GetTask", {"id": task["id"]})
        if _unwrap_task(result) is None and result.get("kind") != "message" and not isinstance(result.get("message"), dict):
            raise ValueError("Invalid A2A result kind")
        return parse_artifact(result)

    timeout = float(os.getenv("REMEDIATION_TIMEOUT_SECONDS", "2100"))
    if not 0 < timeout <= 2100:
        raise ValueError("Invalid REMEDIATION_TIMEOUT_SECONDS")
    async with asyncio.timeout(timeout):
        if http_client is not None:
            return await run(http_client)
        async with httpx.AsyncClient(timeout=timeout) as http:
            return await run(http)


async def _run(request: RemediationRequest, url: str):
    activity.feed.emit(activity.AGENT_START, "RemediationAgent", request.issueCategory)
    try:
        result = await call_remediation(request, url)
        if result.status in FAILED_STATUSES:
            activity.feed.emit(activity.ERROR, "Remediation failed",
                               f"{result.status}: {result.message or ''}")
        else:
            activity.feed.emit(activity.REMEDIATION, f"Remediation {result.status}",
                               result.prLink or result.issueLink or result.message or "")
    except (httpx.HTTPError, ValueError, TimeoutError) as exc:
        activity.feed.emit(activity.ERROR, "A2A remediation failed", str(exc))
    finally:
        activity.feed.emit(activity.AGENT_COMPLETE, "RemediationAgent")


class RemediationAgent:
    def delegate(self, request: RemediationRequest):
        url = os.getenv("REMEDIATION_AGENT_URL", "").strip()
        if not url:
            activity.feed.emit(activity.REMEDIATION, "Skipped: REMEDIATION_AGENT_URL is not configured")
            return
        try:
            parsed = httpx.URL(url)
            if parsed.scheme not in {"http", "https"} or not parsed.host:
                raise ValueError("REMEDIATION_AGENT_URL must be an HTTP(S) A2A JSON-RPC endpoint")
        except (ValueError, httpx.InvalidURL) as exc:
            activity.feed.emit(activity.ERROR, "Remediation endpoint is invalid", str(exc))
            return
        activity.feed.emit(activity.REMEDIATION, "Delegating final rollback", request.issueCategory)
        task = asyncio.create_task(_run(request, url))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)


async def shutdown():
    tasks = list(_background_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
