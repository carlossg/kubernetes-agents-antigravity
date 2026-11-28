import asyncio
import base64
import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from agents.contracts import RemediationRequest
from agents.remediation import call_remediation
from agents.remediation_server import (
    AntigravityRemediationAgent,
    GitHubRepo,
    RemediationService,
    extract_json,
    is_repo_allowed,
    parse_repo_url,
)
from main import create_app


def _sample_request(category: str = "CODE_BUG", repo_url: str = "https://github.com/o/r") -> RemediationRequest:
    return RemediationRequest(
        repoUrl=repo_url,
        baseBranch="main",
        issueCategory=category,
        title="NullPointerException in MetricsResource",
        rootCause="NullPointerException at MetricsResource.java:42",
        analysis="Canary 500 error rate spiked to 100%",
        diagnosticContext="java.lang.NullPointerException at dev.kevindubois.MetricsResource.get(MetricsResource.java:42)",
    )


class RepoHelpersTests(unittest.TestCase):
    def test_parse_and_allow_list(self):
        self.assertEqual(parse_repo_url("https://github.com/carlossg/argo-rollouts-aiops-dashboard.git"),
                         ("carlossg", "argo-rollouts-aiops-dashboard"))
        with self.assertRaises(ValueError):
            parse_repo_url("https://github.com/only-owner")

        allowed = ["https://github.com/Carlossg/argo-rollouts-aiops-dashboard"]
        self.assertTrue(is_repo_allowed("https://github.com/carlossg/argo-rollouts-aiops-dashboard", allowed))
        self.assertFalse(is_repo_allowed("https://github.com/other/repo", allowed))
        self.assertTrue(is_repo_allowed("https://github.com/any/repo", []))

    def test_extract_json_from_fenced_text(self):
        raw = 'Here is the fix:\n```json\n{"path": "src/App.java", "newContent": "class App {}"}\n```'
        self.assertEqual(extract_json(raw), {"path": "src/App.java", "newContent": "class App {}"})


class GitHubRepoTests(unittest.TestCase):
    def test_find_read_and_create_pr(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path))
            if request.method == "GET" and request.url.path.endswith("/git/trees/main"):
                return httpx.Response(200, json={"tree": [
                    {"path": "src/main/java/MetricsResource.java", "type": "blob"},
                    {"path": "README.md", "type": "blob"},
                ]})
            if request.method == "GET" and "/contents/" in request.url.path:
                encoded = base64.b64encode(b"class MetricsResource {}").decode("ascii")
                return httpx.Response(200, json={"content": encoded, "sha": "blob-sha"})
            if request.method == "GET" and request.url.path.endswith("/git/ref/heads/main"):
                return httpx.Response(200, json={"object": {"sha": "base-sha"}})
            if request.method == "POST" and request.url.path.endswith("/git/refs"):
                return httpx.Response(201, json={"ref": "refs/heads/fix/1"})
            if request.method == "PUT" and "/contents/" in request.url.path:
                return httpx.Response(200, json={"commit": {"sha": "new-sha"}})
            if request.method == "POST" and request.url.path.endswith("/pulls"):
                return httpx.Response(201, json={"html_url": "https://github.com/o/r/pull/42"})
            if request.method == "POST" and request.url.path.endswith("/issues"):
                return httpx.Response(201, json={"html_url": "https://github.com/o/r/issues/9"})
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        with httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler)) as client:
            gh = GitHubRepo("https://github.com/o/r", "tok", client=client)
            self.assertEqual(gh.find_files("Metrics"), ["src/main/java/MetricsResource.java"])
            self.assertEqual(gh.read_file("src/main/java/MetricsResource.java"), "class MetricsResource {}")
            pr_url = gh.create_pull_request(
                "src/main/java/MetricsResource.java",
                "class MetricsResource { /* fixed */ }",
                "Fix: NPE",
                "body",
                "fix/1",
            )
            self.assertEqual(pr_url, "https://github.com/o/r/pull/42")
            issue_url = gh.create_issue("Issue", "body")
            self.assertEqual(issue_url, "https://github.com/o/r/issues/9")


class AntigravityAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_propose_fix_configures_antigravity_sdk_and_parses_json(self):
        fake_response = MagicMock()
        fake_response.text = AsyncMock(return_value=json.dumps({
            "path": "src/MetricsResource.java",
            "newContent": "fixed",
            "title": "Guard null input",
            "summary": "Added null check",
        }))
        fake_agent = MagicMock()
        fake_agent.chat = AsyncMock(return_value=fake_response)
        fake_cm = MagicMock()
        fake_cm.__aenter__ = AsyncMock(return_value=fake_agent)
        fake_cm.__aexit__ = AsyncMock(return_value=False)

        captured_configs = []

        def fake_agent_cls(config):
            captured_configs.append(config)
            return fake_cm

        fake_gh = MagicMock(spec=GitHubRepo)
        with patch("agents.remediation_server.Agent", side_effect=fake_agent_cls):
            agent = AntigravityRemediationAgent(model="gemini-4-neon", api_key="test-key")
            result = await agent.propose_fix(fake_gh, _sample_request())

        self.assertEqual(result["path"], "src/MetricsResource.java")
        self.assertEqual(len(captured_configs), 1)
        cfg = captured_configs[0]
        self.assertEqual(cfg.gemini_config.models.default.name, "gemini-4-neon")
        self.assertEqual(cfg.workspaces, [])
        self.assertEqual([t.__name__ for t in cfg.tools], ["find_repo_files", "read_repo_file"])


class RemediationServiceAndA2ATests(unittest.IsolatedAsyncioTestCase):
    @patch.dict(os.environ, {"REMEDIATION_ALLOWED_REPOS": "https://github.com/allowed/repo"}, clear=True)
    async def test_rejected_repo(self):
        svc = RemediationService()
        resp = await svc.remediate(_sample_request(repo_url="https://github.com/o/r"))
        self.assertEqual(resp.status, "rejected")

    @patch.dict(os.environ, {"GITHUB_TOKEN": "tok", "GEMINI_API_KEY": "key"}, clear=True)
    async def test_code_bug_opens_pr(self):
        fake_gh = MagicMock()
        fake_gh.create_pull_request.return_value = "https://github.com/o/r/pull/10"
        fake_agent = MagicMock()
        fake_agent.api_key = "key"
        fake_agent.propose_fix = AsyncMock(return_value={
            "path": "src/App.java",
            "newContent": "fixed",
            "title": "Fix NPE",
            "summary": "Added null guard",
        })
        svc = RemediationService(agent=fake_agent, repo_factory=lambda *_: fake_gh)
        resp = await svc.remediate(_sample_request("CODE_BUG"))
        self.assertEqual(resp.status, "success")
        self.assertEqual(resp.prLink, "https://github.com/o/r/pull/10")
        self.assertEqual(resp.message, "Added null guard")

    @patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}, clear=True)
    async def test_operational_opens_issue_without_llm(self):
        fake_gh = MagicMock()
        fake_gh.create_issue.return_value = "https://github.com/o/r/issues/5"
        fake_agent = MagicMock()
        fake_agent.propose_fix = AsyncMock()
        svc = RemediationService(agent=fake_agent, repo_factory=lambda *_: fake_gh)
        resp = await svc.remediate(_sample_request("OPERATIONAL"))
        self.assertEqual(resp.status, "issue")
        self.assertEqual(resp.issueLink, "https://github.com/o/r/issues/5")
        fake_agent.propose_fix.assert_not_awaited()

    @patch.dict(os.environ, {"GITHUB_TOKEN": "tok", "GEMINI_API_KEY": "key"}, clear=True)
    async def test_a2a_server_end_to_end_with_client(self):
        app = create_app("remediation")
        fake_gh = MagicMock()
        fake_gh.create_pull_request.return_value = "https://github.com/o/r/pull/99"
        fake_agent = MagicMock()
        fake_agent.api_key = "key"
        fake_agent.propose_fix = AsyncMock(return_value={
            "path": "src/App.java",
            "newContent": "fixed",
            "title": "Fix NPE",
            "summary": "Added null guard",
        })
        # Re-register routes with mocked service on a fresh app
        from fastapi import FastAPI
        from agents.remediation_server import register_remediation_routes
        test_app = FastAPI()
        store = register_remediation_routes(
            test_app,
            service=RemediationService(agent=fake_agent, repo_factory=lambda *_: fake_gh),
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=test_app), base_url="http://remediation",
            ) as client:
                card = await client.get("/.well-known/agent-card.json")
                self.assertEqual(card.status_code, 200)
                self.assertEqual(card.json()["skills"][0]["id"], "remediate")

                real_sleep = asyncio.sleep

                async def fast_sleep(_):
                    await real_sleep(0)

                with patch("agents.remediation.asyncio.sleep", new=fast_sleep):
                    result = await call_remediation(_sample_request(), "http://remediation/", http_client=client)
            self.assertEqual(result.status, "success")
            self.assertEqual(result.prLink, "https://github.com/o/r/pull/99")
            self.assertEqual(result.message, "Added null guard")
        finally:
            await store.shutdown()


if __name__ == "__main__":
    unittest.main()
