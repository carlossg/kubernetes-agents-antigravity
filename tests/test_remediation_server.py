import asyncio
import base64
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import FastAPI

from agents.contracts import RemediationRequest, RemediationResponse
from agents.remediation import call_remediation
from agents.remediation_server import (
    A2ATaskStore,
    AntigravityRemediationAgent,
    GitHubRepo,
    MAX_FILE_CHARS,
    RemediationService,
    _make_tools,
    extract_json,
    is_repo_allowed,
    parse_repo_url,
    register_remediation_routes,
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
        self.assertEqual(
            parse_repo_url("https://github.com/carlossg/argo-rollouts-aiops-dashboard.git"),
            ("carlossg", "argo-rollouts-aiops-dashboard"),
        )
        for bad in (
            "https://github.com/only-owner",
            "https://evil.com/o/r",
            "http://github.com/o/r",
            "https://github.com/o/r/extra",
            "https://github.com/../r",
        ):
            with self.assertRaises(ValueError):
                parse_repo_url(bad)

        allowed = ["https://github.com/Carlossg/argo-rollouts-aiops-dashboard"]
        self.assertTrue(is_repo_allowed("https://github.com/carlossg/argo-rollouts-aiops-dashboard", allowed))
        self.assertFalse(is_repo_allowed("https://github.com/other/repo", allowed))
        self.assertFalse(is_repo_allowed("https://github.com/any/repo", []))

    def test_extract_json_handles_fences_and_surrounding_braces(self):
        raw = 'Note {ignore me} here:\n```json\n{"path": "src/App.java", "newContent": "class App {}"}\n```\nTrailing {braces}'
        self.assertEqual(extract_json(raw), {"path": "src/App.java", "newContent": "class App {}"})
        with self.assertRaises(ValueError):
            extract_json("no json here")


class GitHubRepoTests(unittest.IsolatedAsyncioTestCase):
    async def test_find_read_and_create_pr_with_slash_branch(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path.endswith("/git/ref/heads/release/1.0"):
                return httpx.Response(200, json={"ref": "refs/heads/release/1.0", "object": {"sha": "base-sha"}})
            if request.method == "GET" and request.url.path.endswith("/git/trees/base-sha"):
                return httpx.Response(200, json={"tree": [
                    {"path": "src/main/java/MetricsResource.java", "type": "blob"},
                    {"path": "README.md", "type": "blob"},
                ]})
            if request.method == "GET" and "/contents/" in request.url.path:
                encoded = base64.b64encode(b"class MetricsResource {}").decode("ascii")
                return httpx.Response(200, json={
                    "type": "file", "encoding": "base64", "size": 24, "content": encoded, "sha": "blob-sha",
                })
            if request.method == "POST" and request.url.path.endswith("/git/refs"):
                return httpx.Response(201, json={"ref": "refs/heads/fix/1"})
            if request.method == "PUT" and "/contents/" in request.url.path:
                return httpx.Response(200, json={"commit": {"sha": "new-sha"}})
            if request.method == "POST" and request.url.path.endswith("/pulls"):
                return httpx.Response(201, json={"html_url": "https://github.com/o/r/pull/42"})
            if request.method == "POST" and request.url.path.endswith("/issues"):
                return httpx.Response(201, json={"html_url": "https://github.com/o/r/issues/9"})
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler)) as client:
            gh = GitHubRepo("https://github.com/o/r", "tok", base_branch="release/1.0", client=client)
            self.assertEqual(await gh.find_files("Metrics"), ["src/main/java/MetricsResource.java"])
            self.assertEqual(await gh.read_file("src/main/java/MetricsResource.java"), "class MetricsResource {}")
            pr_url = await gh.create_pull_request(
                "src/main/java/MetricsResource.java",
                "class MetricsResource { /* fixed */ }",
                "Fix: NPE",
                "body",
                "fix/1",
            )
            self.assertEqual(pr_url, "https://github.com/o/r/pull/42")
            issue_url = await gh.create_issue("Issue", "body")
            self.assertEqual(issue_url, "https://github.com/o/r/issues/9")

    async def test_security_guards_encoding_none_sha_mismatch_and_cleanup(self):
        deleted_refs = []
        current_sha = "blob-sha"

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path.endswith("/git/ref/heads/main"):
                return httpx.Response(200, json={"ref": "refs/heads/main", "object": {"sha": "base-sha"}})
            if request.method == "GET" and request.url.path.endswith("/git/ref/heads/main-prefix"):
                return httpx.Response(200, json=[{"ref": "refs/heads/main-prefix-1"}])
            if request.method == "GET" and request.url.path.endswith("/contents/huge.bin"):
                return httpx.Response(200, json={
                    "type": "file", "encoding": "none", "size": 2_000_000, "content": "", "sha": "huge-sha",
                })
            if request.method == "GET" and "/contents/" in request.url.path:
                encoded = base64.b64encode(b"class A {}").decode("ascii")
                return httpx.Response(200, json={
                    "type": "file", "encoding": "base64", "size": 10, "content": encoded, "sha": current_sha,
                })
            if request.method == "POST" and request.url.path.endswith("/git/refs"):
                return httpx.Response(201, json={"ref": "refs/heads/fix/1"})
            if request.method == "PUT" and "/contents/" in request.url.path:
                return httpx.Response(409, text="conflict")
            if request.method == "DELETE" and "/git/refs/heads/" in request.url.path:
                deleted_refs.append(request.url.path)
                return httpx.Response(204)
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler)) as client:
            gh = GitHubRepo("https://github.com/o/r", "tok", client=client)
            with self.assertRaises(ValueError):
                await gh.read_file("../../../other/secret")
            with self.assertRaises(ValueError):
                await gh.read_file("huge.bin")
            with self.assertRaises(ValueError):
                await gh._get_branch_sha("main-prefix")
            for blocked in (".GITHUB/workflows/ci.yml", ".gitmodules", "src/Unread.java"):
                with self.assertRaises(ValueError):
                    await gh.create_pull_request(blocked, "x", "t", "b", "fix/1")

            self.assertEqual(await gh.read_file("src/A.java"), "class A {}")
            with self.assertRaises(ValueError):
                await gh.create_pull_request("src/A.java", "x" * (MAX_FILE_CHARS + 1), "t", "b", "fix/1")
            with self.assertRaises(ValueError):
                await gh.create_pull_request("src/A.java", "class A {}", "t", "b", "fix/1")

            current_sha = "changed-sha"
            with self.assertRaises(RuntimeError):
                await gh.create_pull_request("src/A.java", "class A { /* fixed */ }", "t", "b", "fix/1")

            current_sha = "blob-sha"
            with self.assertRaises(RuntimeError):
                await gh.create_pull_request("src/A.java", "class A { /* fixed */ }", "t", "b", "fix/1")
            self.assertEqual(len(deleted_refs), 1)


class AntigravityAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_propose_fix_configures_antigravity_sdk_and_cleans_temp_dir(self):
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
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ, {"ANTIGRAVITY_APP_DATA_DIR": tmpdir}, clear=True,
        ), patch("agents.remediation_server.Agent", side_effect=fake_agent_cls):
            agent = AntigravityRemediationAgent(model="gemini-4-neon", api_key="test-key")
            result = await agent.propose_fix(fake_gh, _sample_request())

        self.assertEqual(result["path"], "src/MetricsResource.java")
        self.assertEqual(len(captured_configs), 1)
        cfg = captured_configs[0]
        self.assertEqual(cfg.model, "gemini-4-neon")
        self.assertEqual(cfg.workspaces, [])
        self.assertEqual([t.__name__ for t in cfg.tools], ["find_repo_files", "read_repo_file"])
        self.assertFalse(os.path.exists(cfg.app_data_dir))

    async def test_propose_fix_timeout_raises_timeout_error(self):
        fake_agent = MagicMock()

        async def slow_chat(_):
            await asyncio.sleep(5)

        fake_agent.chat = slow_chat
        fake_cm = MagicMock()
        fake_cm.__aenter__ = AsyncMock(return_value=fake_agent)
        fake_cm.__aexit__ = AsyncMock(return_value=False)
        fake_gh = MagicMock(spec=GitHubRepo)
        with patch.dict(os.environ, {"REMEDIATION_AGENT_TIMEOUT_SECONDS": "0.01"}, clear=True), patch(
            "agents.remediation_server.Agent", return_value=fake_cm,
        ):
            agent = AntigravityRemediationAgent(model="gemini-4-neon", api_key="test-key")
            with self.assertRaises(TimeoutError):
                await agent.propose_fix(fake_gh, _sample_request())

    async def test_tools_handle_errors_and_oversized_files(self):
        fake_gh = MagicMock(spec=GitHubRepo)
        fake_gh.find_files = AsyncMock(return_value=["src/App.java"])
        fake_gh.read_file = AsyncMock(side_effect=ValueError(f"File 'big.java' exceeds {MAX_FILE_CHARS} characters"))
        find_tool, read_tool = _make_tools(fake_gh)
        self.assertEqual(await find_tool("App"), "src/App.java")
        self.assertIn("exceeds", await read_tool("big.java"))


class RemediationServiceAndA2ATests(unittest.IsolatedAsyncioTestCase):
    @patch.dict(os.environ, {"REMEDIATION_ALLOWED_REPOS": "https://github.com/allowed/repo"}, clear=True)
    async def test_rejected_repo(self):
        svc = RemediationService()
        resp = await svc.remediate(_sample_request(repo_url="https://github.com/o/r"))
        self.assertEqual(resp.status, "rejected")

    @patch.dict(os.environ, {}, clear=True)
    async def test_empty_allowlist_fails_closed(self):
        svc = RemediationService()
        resp = await svc.remediate(_sample_request(repo_url="https://github.com/o/r"))
        self.assertEqual(resp.status, "rejected")

    @patch.dict(os.environ, {"REMEDIATION_ALLOWED_REPOS": "https://github.com/o/r"}, clear=True)
    async def test_missing_tokens_return_infra_error(self):
        svc = RemediationService()
        resp = await svc.remediate(_sample_request("CODE_BUG"))
        self.assertEqual(resp.status, "infra_error")
        self.assertIn("GITHUB_TOKEN", resp.message)

        fake_gh = MagicMock()
        fake_gh.aclose = AsyncMock()
        with patch.dict(os.environ, {"REMEDIATION_ALLOWED_REPOS": "https://github.com/o/r", "GITHUB_TOKEN": "tok"}, clear=True):
            svc2 = RemediationService(repo_factory=lambda *_: fake_gh)
            resp2 = await svc2.remediate(_sample_request("CODE_BUG"))
        self.assertEqual(resp2.status, "infra_error")
        self.assertIn("GEMINI_API_KEY", resp2.message)
        fake_gh.aclose.assert_awaited_once()

    @patch.dict(os.environ, {
        "REMEDIATION_ALLOWED_REPOS": "https://github.com/o/r",
        "GITHUB_TOKEN": "tok",
        "GEMINI_API_KEY": "key",
    }, clear=True)
    async def test_code_bug_opens_pr_and_closes_repo(self):
        fake_gh = MagicMock()
        fake_gh.create_pull_request = AsyncMock(return_value="https://github.com/o/r/pull/10")
        fake_gh.aclose = AsyncMock()
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
        fake_gh.aclose.assert_awaited_once()

    @patch.dict(os.environ, {
        "REMEDIATION_ALLOWED_REPOS": "https://github.com/o/r",
        "GITHUB_TOKEN": "tok",
    }, clear=True)
    async def test_operational_opens_issue_without_llm(self):
        fake_gh = MagicMock()
        fake_gh.create_issue = AsyncMock(return_value="https://github.com/o/r/issues/5")
        fake_gh.aclose = AsyncMock()
        fake_agent = MagicMock()
        fake_agent.propose_fix = AsyncMock()
        svc = RemediationService(agent=fake_agent, repo_factory=lambda *_: fake_gh)
        resp = await svc.remediate(_sample_request("OPERATIONAL"))
        self.assertEqual(resp.status, "issue")
        self.assertEqual(resp.issueLink, "https://github.com/o/r/issues/5")
        fake_agent.propose_fix.assert_not_awaited()

    @patch.dict(os.environ, {
        "REMEDIATION_ALLOWED_REPOS": "https://github.com/o/r",
        "GITHUB_TOKEN": "tok",
        "GEMINI_API_KEY": "key",
    }, clear=True)
    async def test_null_path_pr_failure_and_agent_error(self):
        fake_gh = MagicMock()
        fake_gh.create_issue = AsyncMock(return_value="https://github.com/o/r/issues/11")
        fake_gh.aclose = AsyncMock()
        fake_agent = MagicMock()
        fake_agent.api_key = "key"
        fake_agent.propose_fix = AsyncMock(return_value={"path": None})
        svc = RemediationService(agent=fake_agent, repo_factory=lambda *_: fake_gh)
        resp = await svc.remediate(_sample_request("CODE_BUG"))
        self.assertEqual(resp.status, "issue")
        self.assertEqual(resp.issueLink, "https://github.com/o/r/issues/11")

        fake_agent.propose_fix = AsyncMock(return_value={"path": "src/App.java", "newContent": "x"})
        fake_gh.create_pull_request = AsyncMock(side_effect=RuntimeError("409 conflict"))
        fake_gh.create_issue = AsyncMock(side_effect=RuntimeError("issue failed"))
        resp2 = await svc.remediate(_sample_request("CODE_BUG"))
        self.assertEqual(resp2.status, "pr_failed")
        self.assertIsNone(resp2.issueLink)

        fake_agent.propose_fix = AsyncMock(side_effect=TimeoutError("timed out"))
        resp3 = await svc.remediate(_sample_request("CODE_BUG"))
        self.assertEqual(resp3.status, "agent_error")

    async def test_task_store_eviction_and_shutdown(self):
        store = A2ATaskStore(max_tasks=1, max_active=1)
        t1 = store.create()
        self.assertIsNotNone(t1)
        self.assertIsNone(store.create())
        store.complete(t1, RemediationResponse(status="issue", issueLink="https://github.com/o/r/issues/1"))
        t2 = store.create()
        self.assertIsNotNone(t2)
        store.fail(t1, "late")
        self.assertIsNone(store.get(t1))

        cancelled = asyncio.Event()

        async def sleeper():
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        store.track(asyncio.create_task(sleeper()))
        await asyncio.sleep(0)
        await store.shutdown()
        self.assertTrue(cancelled.is_set())

    @patch.dict(os.environ, {
        "REMEDIATION_ALLOWED_REPOS": "https://github.com/o/r",
        "REMEDIATION_AGENT_TOKEN": "secret-token",
        "GITHUB_TOKEN": "tok",
        "GEMINI_API_KEY": "key",
    }, clear=True)
    async def test_a2a_server_auth_errors_capacity_and_end_to_end(self):
        fake_gh = MagicMock()
        fake_gh.create_pull_request = AsyncMock(return_value="https://github.com/o/r/pull/99")
        fake_gh.aclose = AsyncMock()
        fake_agent = MagicMock()
        fake_agent.api_key = "key"
        fake_agent.propose_fix = AsyncMock(return_value={
            "path": "src/App.java",
            "newContent": "fixed",
            "title": "Fix NPE",
            "summary": "Added null guard",
        })
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
                card_json = card.json()
                self.assertEqual(card_json["skills"][0]["id"], "remediate")
                self.assertTrue(card_json["supportedInterfaces"][0]["url"].startswith("http://remediation"))
                self.assertIn("httpAuthSecurityScheme", card_json["securitySchemes"]["bearerAuth"])
                self.assertEqual(card_json["securityRequirements"], [{"schemes": {"bearerAuth": {"list": []}}}])

                unauth = await client.post("/", json={"jsonrpc": "2.0", "id": "1", "method": "GetTask", "params": {"id": "x"}})
                self.assertEqual(unauth.status_code, 401)
                self.assertEqual(unauth.headers.get("www-authenticate"), "Bearer")

                with patch.dict(os.environ, {"REMEDIATION_AGENT_TOKEN": ""}):
                    unconfigured = await client.post(
                        "/", headers={"Authorization": "Bearer secret-token"},
                        json={"jsonrpc": "2.0", "id": "1", "method": "GetTask", "params": {"id": "x"}},
                    )
                    self.assertEqual(unconfigured.status_code, 401)

                auth_headers = {"Authorization": "Bearer secret-token"}
                for body, expected_code in (
                    ("{not-json", -32700),
                    (json.dumps({"jsonrpc": "2.0", "id": "2"}), -32600),
                    (json.dumps({"jsonrpc": "2.0", "id": "3", "method": "Unknown", "params": {}}), -32601),
                    (json.dumps({"jsonrpc": "2.0", "id": "4", "method": "GetTask", "params": []}), -32602),
                    (json.dumps({"jsonrpc": "2.0", "id": "5", "method": "SendMessage", "params": {"message": {"parts": []}}}), -32602),
                    (json.dumps({"jsonrpc": "2.0", "id": "6", "method": "GetTask", "params": {"id": "missing"}}), -32001),
                ):
                    resp = await client.post("/", headers=auth_headers, content=body)
                    self.assertEqual(resp.json()["error"]["code"], expected_code)

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

    def test_create_app_wires_remediation_store(self):
        app = create_app("remediation")
        self.assertIsNotNone(getattr(app.state, "remediation_task_store", None))


if __name__ == "__main__":
    unittest.main()
