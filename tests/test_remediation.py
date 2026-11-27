import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from agents import activity
from agents.contracts import RemediationRequest
from agents.remediation import RemediationAgent, call_remediation, parse_artifact, shutdown


def request(category="CODE_BUG"):
    return RemediationRequest(
        repoUrl="https://github.com/o/r", baseBranch="main", issueCategory=category,
        title="Canary failure", rootCause="NPE", analysis="Measured regression", diagnosticContext="logs",
    )


def artifact(status="success", link="https://github.com/o/r/pull/1"):
    return {"task": {"id": "task-1", "status": {"state": "TASK_STATE_COMPLETED"}, "artifacts": [{
        "parts": [{"text": json.dumps({"status": status, "prLink": link})}],
    }]}}


class ActivityFeedTests(unittest.TestCase):
    def test_bounded_ids_and_since_filters_preserve_dashboard_shape(self):
        feed = activity.ActivityFeed(max_events=2)
        first = feed.emit(activity.ANALYSIS_START, "start")
        second = feed.emit(activity.DECISION, "ABORT", "NPE")
        third = feed.emit(activity.REMEDIATION, "delegated")
        self.assertEqual([first["id"], second["id"], third["id"]], [1, 2, 3])
        self.assertEqual([e["id"] for e in feed.events(since=2)], [3])
        self.assertEqual([e["id"] for e in feed.events()], [2, 3])
        self.assertEqual(set(second), {"id", "timestamp", "type", "message", "details"})


class RemediationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await shutdown()

    async def test_a2a_payload_polling_and_final_artifact(self):
        calls = []

        def handler(http_request):
            self.assertEqual(http_request.headers.get("A2A-Version"), "1.0")
            payload = json.loads(http_request.content)
            calls.append(payload)
            result = (
                {"task": {"id": "task-1", "status": {"state": "TASK_STATE_WORKING"}}}
                if payload["method"] == "SendMessage"
                else artifact()["task"]
            )
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

        with patch("agents.remediation.asyncio.sleep", new_callable=AsyncMock):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                response = await call_remediation(request(), "http://remote/a2a", http_client=http)
        self.assertEqual(response.prLink, "https://github.com/o/r/pull/1")
        self.assertEqual([c["method"] for c in calls], ["SendMessage", "GetTask"])
        payload = json.loads(calls[0]["params"]["message"]["parts"][0]["text"])
        self.assertEqual(payload, request().model_dump())
        self.assertNotIn("model", payload)
        self.assertNotIn("GITHUB_TOKEN", payload)

    async def test_operational_issue_artifact(self):
        result = {"kind": "message", "parts": [{"kind": "text", "text": json.dumps({
            "status": "issue", "issueLink": "https://github.com/o/r/issues/2",
        })}]}
        self.assertEqual(parse_artifact(result).issueLink, "https://github.com/o/r/issues/2")

    async def test_rpc_errors_failed_task_and_missing_artifact_surface(self):
        for response in (
            {"error": {"code": -1, "message": "rejected"}},
            {"result": {"kind": "task", "status": {"state": "failed"}}},
            {"result": {"kind": "task", "status": {"state": "completed"}, "artifacts": []}},
            {"result": {"kind": "task", "status": {"state": "completed"}, "artifacts": None}},
            {"result": {"kind": "task", "status": None}},
        ):
            def handler(http_request):
                payload = json.loads(http_request.content)
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], **response})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                with self.assertRaises(ValueError):
                    await call_remediation(request(), "http://remote", http_client=http)

    @patch.dict(os.environ, {"REMEDIATION_TIMEOUT_SECONDS": "0.01"}, clear=True)
    async def test_background_rpc_has_a_bounded_deadline(self):
        async def handler(http_request):
            await asyncio.Future()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with self.assertRaises(TimeoutError):
                await call_remediation(request(), "http://remote", http_client=http)

    async def test_missing_or_untrusted_artifact_never_claims_success(self):
        for link in (
            None,
            "https://evil.example/pull/1",
            "https://github.com/o/r/not-pull/1",
            "https://github.com/o/r/issues/1",
        ):
            with self.subTest(link=link), self.assertRaises(ValueError):
                parse_artifact(artifact(link=link))
        with self.assertRaises(ValueError):
            parse_artifact({"kind": "message", "parts": [{"kind": "text", "text": json.dumps({
                "status": "issue", "issueLink": "https://github.com/o/r/pull/1",
            })}]})

    async def test_multiple_artifacts_parses_only_final_artifact(self):
        result = {"task": {"id": "task-1", "status": {"state": "TASK_STATE_COMPLETED"}, "artifacts": [
            {"parts": [{"text": "Cloning repository and running tests..."}]},
            {"parts": [{"text": json.dumps({"status": "success", "prLink": "https://github.com/o/r/pull/7"})}]},
        ]}}
        self.assertEqual(parse_artifact(result).prLink, "https://github.com/o/r/pull/7")

    async def test_multiple_artifacts_finds_json_result_when_not_last(self):
        result = {"task": {"id": "task-1", "status": {"state": "TASK_STATE_COMPLETED"}, "artifacts": [
            {"parts": [{"text": json.dumps({"status": "success", "prLink": "https://github.com/o/r/pull/8"})}]},
            {"parts": [{"text": "Execution transcript attached"}]},
        ]}}
        self.assertEqual(parse_artifact(result).prLink, "https://github.com/o/r/pull/8")

    @patch.dict(os.environ, {"REMEDIATION_AGENT_URL": "http://remote/a2a"}, clear=True)
    async def test_delegation_returns_without_waiting_and_shutdown_cancels(self):
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def blocked(*args):
            started.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        with patch("agents.remediation.call_remediation", side_effect=blocked):
            RemediationAgent().delegate(request())
            await asyncio.wait_for(started.wait(), 1)
            await shutdown()
        self.assertTrue(stopped.is_set())

    @patch.dict(os.environ, {}, clear=True)
    async def test_unconfigured_remediation_emits_explicit_skip(self):
        with patch("agents.remediation.call_remediation", new_callable=AsyncMock) as remote, patch(
            "agents.activity.feed", activity.ActivityFeed(),
        ):
            RemediationAgent().delegate(request())
            self.assertIn("not configured", activity.feed.events()[0]["message"])
        remote.assert_not_awaited()

    @patch.dict(os.environ, {"REMEDIATION_AGENT_URL": "not-an-endpoint"}, clear=True)
    async def test_bad_optional_endpoint_does_not_change_returned_verdict(self):
        with patch("agents.activity.feed", activity.ActivityFeed()):
            RemediationAgent().delegate(request())
            self.assertEqual(activity.feed.events()[0]["type"], activity.ERROR)

    @patch.dict(os.environ, {"REMEDIATION_AGENT_URL": "http://remote"}, clear=True)
    async def test_background_failure_is_reported_in_feed(self):
        with patch("agents.remediation.call_remediation", AsyncMock(side_effect=ValueError("broken RPC"))), patch(
            "agents.activity.feed", activity.ActivityFeed(),
        ):
            RemediationAgent().delegate(request())
            await asyncio.sleep(0)
            self.assertTrue(any(e["type"] == activity.ERROR for e in activity.feed.events()))


if __name__ == "__main__":
    unittest.main()
