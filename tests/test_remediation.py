import asyncio
import base64
import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from agents import activity
from agents.remediation import GitHubRepo, extract_json, parse_repo_url, remediate


class ActivityFeedTests(unittest.TestCase):
    def test_ids_increase_and_since_filters(self):
        feed = activity.ActivityFeed()
        first = feed.emit(activity.ANALYSIS_START, "start")
        second = feed.emit(activity.DECISION, "ABORT", "NPE")
        self.assertEqual([first["id"], second["id"]], [1, 2])
        self.assertEqual([e["id"] for e in feed.events(since=1)], [2])
        self.assertEqual(set(second), {"id", "timestamp", "type", "message", "details"})

    def test_bounded(self):
        feed = activity.ActivityFeed(max_events=3)
        for i in range(5):
            feed.emit(activity.TOOL_CALL, str(i))
        self.assertEqual([e["message"] for e in feed.events()], ["2", "3", "4"])


class GitHubRepoTests(unittest.TestCase):
    def test_parse_repo_url(self):
        self.assertEqual(parse_repo_url("https://github.com/carlossg/demo.git"), ("carlossg", "demo"))
        with self.assertRaises(ValueError):
            parse_repo_url("https://github.com/")

    def test_extract_json_from_markdown(self):
        self.assertEqual(extract_json('```json\n{"path": null}\n```'), {"path": None})

    def test_create_pull_request_sequence(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path))
            path = request.url.path
            if path.endswith("/git/ref/heads/main"):
                return httpx.Response(200, json={"object": {"sha": "abc"}})
            if path.endswith("/git/refs"):
                return httpx.Response(201, json={})
            if request.method == "GET" and "/contents/" in path:
                return httpx.Response(200, json={"sha": "old", "content": base64.b64encode(b"x").decode()})
            if request.method == "PUT":
                return httpx.Response(200, json={})
            if path.endswith("/pulls"):
                return httpx.Response(201, json={"html_url": "https://github.com/o/r/pull/1"})
            return httpx.Response(404, json={})

        client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
        gh = GitHubRepo("https://github.com/o/r", "tok", "main", client=client)
        url = gh.create_pull_request("src/A.java", "fixed", "Fix: A", "body", "fix/a")
        self.assertEqual(url, "https://github.com/o/r/pull/1")
        self.assertEqual(calls[-1], ("POST", "/repos/o/r/pulls"))

    def test_create_issue_sends_label(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["json"] = json.loads(request.content)
            return httpx.Response(201, json={"html_url": "https://github.com/o/r/issues/2"})

        client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
        gh = GitHubRepo("https://github.com/o/r", "tok", "main", client=client)
        url = gh.create_issue("T", "B", ["jules"])
        self.assertEqual(url, "https://github.com/o/r/issues/2")
        self.assertEqual(seen["path"], "/repos/o/r/issues")
        self.assertEqual(seen["json"]["labels"], ["jules"])

    def test_api_error_raises(self):
        client = httpx.Client(
            base_url="https://api.github.com",
            transport=httpx.MockTransport(lambda r: httpx.Response(403, text="forbidden")),
        )
        gh = GitHubRepo("https://github.com/o/r", "tok", "main", client=client)
        with self.assertRaises(RuntimeError):
            gh.list_paths()


class RemediateTests(unittest.IsolatedAsyncioTestCase):
    async def test_skipped_without_token(self):
        with patch.dict(os.environ, {}, clear=True):
            result = await remediate("https://github.com/o/r", "main", "ro", "ns", "NPE", "logs", "analysis")
        self.assertIsNone(result)

    async def test_no_confident_fix_returns_none(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}), patch(
            "agents.remediation.RemediationAgent.propose_fix", new=AsyncMock(return_value={"path": None})
        ):
            result = await remediate("https://github.com/o/r", "main", "ro", "ns", "NPE", "logs", "analysis")
        self.assertIsNone(result)

    async def test_creates_pr_then_labeled_issue_and_survives_issue_failure(self):
        fix = {"path": "A.java", "newContent": "x", "title": "t", "summary": "s"}
        for issue_effect in ("https://github.com/o/r/issues/2", RuntimeError("boom")):
            create_issue = MagicMock(side_effect=[issue_effect] if isinstance(issue_effect, Exception) else None,
                                     return_value=None if isinstance(issue_effect, Exception) else issue_effect)
            with patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}), patch(
                "agents.remediation.RemediationAgent.propose_fix", new=AsyncMock(return_value=fix)
            ), patch("agents.remediation.GitHubRepo.create_pull_request", return_value="https://github.com/o/r/pull/1"), patch(
                "agents.remediation.GitHubRepo.create_issue", create_issue
            ):
                result = await remediate("https://github.com/o/r", "main", "ro", "ns", "NPE", "logs", "analysis")
            self.assertEqual(result, "https://github.com/o/r/pull/1")
            self.assertEqual(create_issue.call_args.args[2], ["jules"])


if __name__ == "__main__":
    unittest.main()
