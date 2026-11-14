import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import MagicMock, patch

import yaml
from fastapi import HTTPException
from fastapi.testclient import TestClient

from agents import substrate_bridge

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("render_substrate", ROOT / "scripts/render-substrate.py")
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)
IMAGE = "registry.example.com/agents@sha256:" + "a" * 64


class BackendSelectionTests(unittest.TestCase):
    def run_plan(self, backend=None, script="deploy-and-test.sh", option="--backend-plan"):
        env = os.environ.copy()
        env.pop("SANDBOX_BACKEND", None)
        if backend is not None:
            env["SANDBOX_BACKEND"] = backend
        return subprocess.run(
            ["bash", str(ROOT / "scripts" / script), option],
            env=env, capture_output=True, text=True, check=False,
        )

    def test_default_and_explicit_gvisor_plan_are_offline(self):
        for backend in (None, "gvisor"):
            result = self.run_plan(backend)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("agents.x-k8s.io Sandbox CRD", result.stdout)

    def test_substrate_demo_and_provisioner_select_pinned_installer(self):
        for script, option in (
            ("deploy-and-test.sh", "--backend-plan"),
            ("create-gke-sandbox.sh", "--plan"),
        ):
            result = self.run_plan("substrate", script, option)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Substrate v1.0.0 (b34fbee742906494b5eb4f1f36987176bf8770a4)", result.stdout)
            self.assertIn("control plane AND every node pool", result.stdout)
            self.assertIn("spiffe://cluster.local/ns/argo-rollouts/sa/kubernetes-agent-logs-bridge", result.stdout)

    def test_invalid_or_empty_backend_fails_before_tools_or_mutations(self):
        for backend in ("", "docker", "Substrate"):
            for script in ("deploy-and-test.sh", "create-gke-sandbox.sh", "install-substrate.sh"):
                result = self.run_plan(backend, script)
                self.assertEqual(result.returncode, 2)
                self.assertIn("Invalid SANDBOX_BACKEND", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_renderer_rejects_mutable_or_malformed_image(self):
        for image in ("agents:latest", "agents@sha256:abc", IMAGE + "|command"):
            with self.assertRaisesRegex(ValueError, "must be pinned"):
                renderer.documents(image)

    def test_native_actor_template_and_allowlisted_egress(self):
        docs = renderer.documents(IMAGE)
        template = docs["template"]
        self.assertNotIn("apiVersion", template)
        self.assertNotIn("kind", template)
        container = template["containers"][0]
        self.assertEqual(container["command"], ["python", "main.py", "--role", "logs"])
        self.assertEqual(container["securityContext"]["capabilities"]["drop"], ["ALL"])
        self.assertEqual({entry["name"] for entry in container["env"]}, {"HOME", "LOG_PROXY_URL"})
        self.assertNotIn("volumes", template)
        self.assertEqual(docs["policy"]["rules"], [{
            "http": {
                "hostnames": ["kubernetes-agent-log-proxy.argo-rollouts.svc.cluster.local"],
                "ports": {"numbers": [8080]},
            },
        }])
        self.assertNotEqual(docs["template-name"], renderer.documents(IMAGE.replace("a" * 64, "b" * 64))["template-name"])

    def test_substrate_manifest_keeps_credentials_outside_actor(self):
        worker, account, bridge = list(yaml.safe_load_all((ROOT / "k8s/log-analyst-substrate.yaml").read_text()))
        self.assertEqual(worker["apiVersion"], "ate.dev/v1")
        self.assertEqual(worker["spec"]["sandboxClasses"], [{"name": "gvisor", "configRef": {"name": "gvisor-default"}}])
        self.assertFalse(account["automountServiceAccountToken"])
        pod = bridge["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(pod["serviceAccountName"], account["metadata"]["name"])
        self.assertNotIn("env", pod["containers"][0])
        self.assertNotIn("envFrom", pod["containers"][0])
        service = next(
            item for item in yaml.safe_load_all((ROOT / "k8s/agents.yaml").read_text())
            if item["kind"] == "Service" and item["metadata"]["name"] == "kubernetes-agent-logs"
        )
        self.assertEqual(service["spec"]["selector"], bridge["spec"]["template"]["metadata"]["labels"])
        self.assertIn("USER 1000:1000", (ROOT / "Dockerfile").read_text())


class SubstrateBridgeTests(unittest.TestCase):
    def test_connect_uses_verified_mtls_and_fixed_actor_header(self):
        raw = MagicMock()
        tunnel = MagicMock()
        tunnel.makefile.return_value = io.BytesIO(b"HTTP/1.1 200 Connection established\r\n\r\n")
        context = MagicMock()
        context.wrap_socket.return_value = tunnel
        connection = substrate_bridge.ActorConnection()
        with patch.object(substrate_bridge.ssl, "create_default_context", return_value=context) as make_context, \
                patch.object(substrate_bridge.socket, "create_connection", return_value=raw):
            connection.connect()
        make_context.assert_called_once_with(cafile="/var/run/substrate/router-ca.pem")
        context.load_cert_chain.assert_called_once_with("/var/run/substrate/credential-bundle.pem")
        context.wrap_socket.assert_called_once_with(raw, server_hostname=substrate_bridge.ROUTER)
        self.assertIn(b"CONNECT kubernetes-agent-logs:8080 HTTP/1.1", tunnel.sendall.call_args.args[0])
        self.assertIn(b"ate-target-actor: argo-rollouts/kubernetes-agent-logs", tunnel.sendall.call_args.args[0])
        connection.close()

    def test_connect_refusal_closes_socket(self):
        raw = MagicMock()
        tunnel = MagicMock()
        tunnel.makefile.return_value = io.BytesIO(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        context = MagicMock()
        context.wrap_socket.return_value = tunnel
        with patch.object(substrate_bridge.ssl, "create_default_context", return_value=context), \
                patch.object(substrate_bridge.socket, "create_connection", return_value=raw):
            with self.assertRaisesRegex(OSError, "CONNECT refused: 403"):
                substrate_bridge.ActorConnection().connect()
        raw.close.assert_called_once()
        tunnel.close.assert_called_once()

    def test_forward_bounds_response_and_does_not_fallback(self):
        connection = MagicMock()
        connection.getresponse.return_value.read.return_value = b"x" * (substrate_bridge.MAX_BYTES + 1)
        with patch.object(substrate_bridge, "ActorConnection", return_value=connection):
            with self.assertRaises(HTTPException) as error:
                substrate_bridge.forward("POST", "/analyze/logs", b"{}")
        self.assertEqual(error.exception.status_code, 502)
        connection.close.assert_called_once()

    def test_http_surface_is_fixed_and_body_is_bounded(self):
        with TestClient(substrate_bridge.app) as client, \
                patch.object(substrate_bridge, "forward", return_value=substrate_bridge.Response(
                    json.dumps({"evidence": {"source": "logs"}}), media_type="application/json",
                )) as forward:
            response = client.post("/analyze/logs", content=b'{"namespace":"demo"}')
            self.assertEqual(response.status_code, 200)
            forward.assert_called_once_with("POST", "/analyze/logs", b'{"namespace":"demo"}')
            self.assertEqual(client.post("/analyze/logs", content=b"x" * (substrate_bridge.MAX_BYTES + 1)).status_code, 413)
            self.assertEqual(client.post("/pod-logs", content=b"{}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
