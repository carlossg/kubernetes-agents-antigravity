import asyncio
import os
import unittest
from unittest.mock import patch, MagicMock

# Import the agents and tools to test
from agents.logs import LogAnalystAgent, fetch_kubernetes_pod_logs
from agents.log_proxy import get_first_pod_logs
from agents.metrics import MetricsAnalystAgent, fetch_kubernetes_pod_metrics
from agents.events import EventAnalystAgent, fetch_kubernetes_namespace_events
from agents.orchestrator import LeadOrchestratorAgent

# ----------------------------------------------------------------------
# Kubernetes Mock Helpers
# ----------------------------------------------------------------------

class MockPodMetadata:
    def __init__(self, name, deletion_timestamp=None):
        self.name = name
        self.deletion_timestamp = deletion_timestamp

class MockContainer:
    def __init__(self, name):
        self.name = name

class MockPodSpec:
    def __init__(self, containers=None):
        self.containers = containers or [MockContainer("app")]

class MockPodItem:
    def __init__(self, name, deletion_timestamp=None, containers=None):
        self.metadata = MockPodMetadata(name, deletion_timestamp=deletion_timestamp)
        self.spec = MockPodSpec(containers=containers)

class MockPodList:
    def __init__(self, items):
        self.items = items

class MockInvolvedObject:
    def __init__(self, kind, name):
        self.kind = kind
        self.name = name

class MockEventItem:
    def __init__(self, e_type, kind, name, reason, message, count=1):
        self.type = e_type
        self.involved_object = MockInvolvedObject(kind, name)
        self.reason = reason
        self.message = message
        self.count = count

class MockEventList:
    def __init__(self, items):
        self.items = items

class MockCoreV1Api:
    def __init__(self, pods_items=None, logs_dict=None, events_items=None):
        self.pods_items = pods_items or []
        self.logs_dict = logs_dict or {}
        self.events_items = events_items or []

    def list_namespaced_pod(self, namespace, label_selector):
        # Return a list of pods matching the label selector (just mock list)
        return MockPodList(self.pods_items)

    def read_namespaced_pod_log(self, pod_name, namespace, container=None, tail_lines=200):
        return self.logs_dict.get(pod_name, "mock log data")

    def list_namespaced_event(self, namespace, limit=30):
        return MockEventList(self.events_items)

class MockCustomObjectsApi:
    def __init__(self, metrics_response=None):
        self.metrics_response = metrics_response or {"items": []}

    def list_namespaced_custom_object(self, group, version, namespace, plural, label_selector):
        return self.metrics_response

# ----------------------------------------------------------------------
# Google Antigravity Agent Mock Helpers
# ----------------------------------------------------------------------

class MockAgentResponse:
    def __init__(self, text_content):
        self.text_content = text_content

    async def text(self):
        return self.text_content

class MockAgentInstance:
    def __init__(self, response_text):
        self.response_text = response_text
        self.chat_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def chat(self, prompt):
        self.chat_calls.append(prompt)
        return MockAgentResponse(self.response_text)

# ----------------------------------------------------------------------
# Unit Tests
# ----------------------------------------------------------------------

class TestKubernetesAgentTools(unittest.TestCase):
    
    # -- agents.log_proxy: the trusted, non-sandboxed service that actually
    # talks to the K8s API on behalf of the sandboxed Log Analyst agent.

    @patch("kubernetes.config.load_incluster_config")
    @patch("kubernetes.client.CoreV1Api")
    def test_get_first_pod_logs_healthy(self, mock_core_v1, mock_in_cluster_cfg):
        mock_api = MockCoreV1Api(
            pods_items=[MockPodItem("stable-pod-1")],
            logs_dict={"stable-pod-1": "2026-06-25 INFO Application is healthy\nINFO Request processed in 10ms"}
        )
        mock_core_v1.return_value = mock_api

        result = get_first_pod_logs("test-ns", "app=test,role=stable")
        self.assertIn("LOGS FOR POD stable-pod-1", result)
        self.assertIn("Application is healthy", result)

    @patch("kubernetes.config.load_incluster_config")
    @patch("kubernetes.client.CoreV1Api")
    def test_get_first_pod_logs_unhealthy(self, mock_core_v1, mock_in_cluster_cfg):
        mock_api = MockCoreV1Api(
            pods_items=[MockPodItem("canary-pod-1")],
            logs_dict={"canary-pod-1": "2026-06-25 ERROR Database connection failed\nTraceback:\nValueError: Connection timeout"}
        )
        mock_core_v1.return_value = mock_api

        result = get_first_pod_logs("test-ns", "app=test,role=canary")
        self.assertIn("LOGS FOR POD canary-pod-1", result)
        self.assertIn("Database connection failed", result)

    @patch("kubernetes.config.load_incluster_config")
    @patch("kubernetes.client.CoreV1Api")
    def test_get_first_pod_logs_no_pods(self, mock_core_v1, mock_in_cluster_cfg):
        mock_api = MockCoreV1Api(pods_items=[])
        mock_core_v1.return_value = mock_api

        result = get_first_pod_logs("test-ns", "app=test,role=canary")
        self.assertEqual(result, "No pods found in namespace 'test-ns' matching selector 'app=test,role=canary'")

    @patch("kubernetes.config.load_incluster_config")
    @patch("kubernetes.client.CoreV1Api")
    def test_get_first_pod_logs_skips_terminating_pod(self, mock_core_v1, mock_in_cluster_cfg):
        mock_api = MockCoreV1Api(
            pods_items=[
                MockPodItem("terminating-pod", deletion_timestamp="2026-06-25T00:00:00Z"),
                MockPodItem("running-pod"),
            ],
            logs_dict={"running-pod": "INFO all good"}
        )
        mock_core_v1.return_value = mock_api

        result = get_first_pod_logs("test-ns", "app=test,role=canary")
        self.assertIn("LOGS FOR POD running-pod", result)

    # -- agents.logs: the sandboxed agent's tool, which has no direct K8s API
    # access and must call the log-proxy service over HTTP instead.

    @patch.dict(os.environ, {"LOG_PROXY_URL": "http://kubernetes-agent-log-proxy:8080"})
    @patch("agents.logs.httpx.get")
    def test_fetch_kubernetes_pod_logs_calls_proxy(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = {"logs": "--- LOGS FOR POD stable-pod-1 ---\nINFO healthy"}
        mock_get.return_value = mock_response

        result = fetch_kubernetes_pod_logs("test-ns", "app=test,role=stable")

        mock_get.assert_called_once_with(
            "http://kubernetes-agent-log-proxy:8080/pod-logs",
            params={"namespace": "test-ns", "label_selector": "app=test,role=stable"},
            timeout=30.0,
        )
        self.assertIn("LOGS FOR POD stable-pod-1", result)

    @patch.dict(os.environ, {}, clear=True)
    def test_fetch_kubernetes_pod_logs_no_proxy_url(self):
        result = fetch_kubernetes_pod_logs("test-ns", "app=test,role=stable")
        self.assertIn("LOG_PROXY_URL is not configured", result)

    @patch.dict(os.environ, {"LOG_PROXY_URL": "http://kubernetes-agent-log-proxy:8080"})
    @patch("agents.logs.httpx.get", side_effect=Exception("connection refused"))
    def test_fetch_kubernetes_pod_logs_proxy_error(self, mock_get):
        result = fetch_kubernetes_pod_logs("test-ns", "app=test,role=stable")
        self.assertIn("Failed to fetch logs via log-proxy", result)
        self.assertIn("connection refused", result)

    @patch("kubernetes.config.load_incluster_config")
    @patch("kubernetes.config.load_kube_config")
    @patch("kubernetes.client.CustomObjectsApi")
    def test_fetch_kubernetes_pod_metrics_healthy(self, mock_custom_api, mock_kube_cfg, mock_in_cluster_cfg):
        mock_api = MockCustomObjectsApi(metrics_response={
            "items": [
                {
                    "metadata": {"name": "stable-pod-1"},
                    "containers": [{"name": "agent", "usage": {"cpu": "10m", "memory": "64Mi"}}]
                }
            ]
        })
        mock_custom_api.return_value = mock_api

        result = fetch_kubernetes_pod_metrics("test-ns", "app=test,role=stable")
        self.assertIn("RESOURCE UTILIZATION METRICS", result)
        self.assertIn("Pod: stable-pod-1, Container: agent -> CPU usage: 10m, Memory usage: 64Mi", result)

    @patch("kubernetes.config.load_incluster_config")
    @patch("kubernetes.config.load_kube_config")
    @patch("kubernetes.client.CustomObjectsApi")
    def test_fetch_kubernetes_pod_metrics_no_metrics(self, mock_custom_api, mock_kube_cfg, mock_in_cluster_cfg):
        mock_api = MockCustomObjectsApi(metrics_response={"items": []})
        mock_custom_api.return_value = mock_api

        result = fetch_kubernetes_pod_metrics("test-ns", "app=test,role=stable")
        self.assertEqual(result, "No metrics found in namespace 'test-ns' matching selector 'app=test,role=stable'")

    @patch("kubernetes.config.load_incluster_config")
    @patch("kubernetes.config.load_kube_config")
    @patch("kubernetes.client.CoreV1Api")
    def test_fetch_kubernetes_namespace_events_healthy(self, mock_core_v1, mock_kube_cfg, mock_in_cluster_cfg):
        mock_api = MockCoreV1Api(events_items=[
            MockEventItem("Normal", "Pod", "stable-pod-1", "Scheduled", "Successfully assigned to node-1"),
            MockEventItem("Normal", "Pod", "stable-pod-1", "Started", "Started container agent")
        ])
        mock_core_v1.return_value = mock_api

        result = fetch_kubernetes_namespace_events("test-ns")
        self.assertIn("RECENT NAMESPACE EVENTS", result)
        self.assertIn("[Normal] Object: Pod/stable-pod-1, Reason: Scheduled", result)
        self.assertIn("[Normal] Object: Pod/stable-pod-1, Reason: Started", result)

    @patch("kubernetes.config.load_incluster_config")
    @patch("kubernetes.config.load_kube_config")
    @patch("kubernetes.client.CoreV1Api")
    def test_fetch_kubernetes_namespace_events_unhealthy(self, mock_core_v1, mock_kube_cfg, mock_in_cluster_cfg):
        mock_api = MockCoreV1Api(events_items=[
            MockEventItem("Warning", "Pod", "canary-pod-1", "FailedScheduling", "0/3 nodes are available"),
            MockEventItem("Warning", "Pod", "canary-pod-1", "BackOff", "Back-off restarting failed container")
        ])
        mock_core_v1.return_value = mock_api

        result = fetch_kubernetes_namespace_events("test-ns")
        self.assertIn("RECENT NAMESPACE EVENTS", result)
        self.assertIn("[Warning] Object: Pod/canary-pod-1, Reason: FailedScheduling", result)
        self.assertIn("[Warning] Object: Pod/canary-pod-1, Reason: BackOff", result)


class TestSpecialistAgents(unittest.IsolatedAsyncioTestCase):

    @patch("agents.logs.Agent")
    async def test_log_analyst_agent_promote(self, mock_agent_class):
        expected_report = "Analysis indicates the canary logs are clean and stable. Recommend promotion. promote: true"
        mock_agent_class.return_value = MockAgentInstance(expected_report)

        agent = LogAnalystAgent(model="gemma-4-diffusion")
        self.assertEqual(agent.config.model, "gemma-4-diffusion")

        report = await agent.analyze("test-ns", "app=test,role=stable", "app=test,role=canary")
        self.assertEqual(report, expected_report)

    @patch("agents.logs.Agent")
    async def test_log_analyst_agent_revert(self, mock_agent_class):
        expected_report = "Analysis shows multiple ValueError exceptions and traceback error in canary logs. Revert rollout."
        mock_agent_class.return_value = MockAgentInstance(expected_report)

        agent = LogAnalystAgent(model="gemma-4-diffusion")
        report = await agent.analyze("test-ns", "app=test,role=stable", "app=test,role=canary")
        self.assertEqual(report, expected_report)

    @patch("agents.metrics.Agent")
    async def test_metrics_analyst_agent_promote(self, mock_agent_class):
        expected_report = "Metrics show stable CPU and Memory usage. promote: true"
        mock_agent_class.return_value = MockAgentInstance(expected_report)

        agent = MetricsAnalystAgent(model="gemma-4-diffusion")
        self.assertEqual(agent.config.model, "gemma-4-diffusion")

        report = await agent.analyze("test-ns", "app=test,role=stable", "app=test,role=canary")
        self.assertEqual(report, expected_report)

    @patch("agents.metrics.Agent")
    async def test_metrics_analyst_agent_revert(self, mock_agent_class):
        expected_report = "Canary is consuming 2Gi of memory, showing a clear memory leak regression. Revert."
        mock_agent_class.return_value = MockAgentInstance(expected_report)

        agent = MetricsAnalystAgent(model="gemma-4-diffusion")
        report = await agent.analyze("test-ns", "app=test,role=stable", "app=test,role=canary")
        self.assertEqual(report, expected_report)

    @patch("agents.events.Agent")
    async def test_event_analyst_agent_promote(self, mock_agent_class):
        expected_report = "Only normal events observed. Deployment healthy."
        mock_agent_class.return_value = MockAgentInstance(expected_report)

        agent = EventAnalystAgent(model="gemma-4-diffusion")
        self.assertEqual(agent.config.model, "gemma-4-diffusion")

        report = await agent.analyze("test-ns", "app=test,role=stable", "app=test,role=canary")
        self.assertEqual(report, expected_report)

    @patch("agents.events.Agent")
    async def test_event_analyst_agent_revert(self, mock_agent_class):
        expected_report = "Warning events: OOMKilled detected for canary pod. Rollback recommended."
        mock_agent_class.return_value = MockAgentInstance(expected_report)

        agent = EventAnalystAgent(model="gemma-4-diffusion")
        report = await agent.analyze("test-ns", "app=test,role=stable", "app=test,role=canary")
        self.assertEqual(report, expected_report)


class TestLeadOrchestratorAgent(unittest.IsolatedAsyncioTestCase):

    @patch("agents.orchestrator.Agent")
    @patch("agents.logs.LogAnalystAgent.analyze")
    @patch("agents.metrics.MetricsAnalystAgent.analyze")
    @patch("agents.events.EventAnalystAgent.analyze")
    async def test_orchestrator_promote_consensus(self, mock_events, mock_metrics, mock_logs, mock_agent_class):
        mock_logs.return_value = "Log report: Everything is healthy. promote: true"
        mock_metrics.return_value = "Metrics report: Resource usage stable. promote: true"
        mock_events.return_value = "Events report: No issues found. Healthy"

        debate_response = """
        {
            "promote": true,
            "confidence": 95,
            "analysis": "All three analysts reports are positive. Logs, metrics and events show a healthy deployment.",
            "rootCause": "",
            "remediation": "",
            "votingRationale": "Consensus resolved positively. All subagents vote promote."
        }
        """
        mock_agent_class.return_value = MockAgentInstance(debate_response)

        orchestrator = LeadOrchestratorAgent(
            model="gemma-4-diffusion",
            log_model="gemma-4-diffusion",
            metrics_model="gemma-4-diffusion",
            event_model="gemma-4-diffusion"
        )
        self.assertEqual(orchestrator.config.model, "gemma-4-diffusion")
        self.assertEqual(orchestrator.log_model, "gemma-4-diffusion")

        result = await orchestrator.analyze_rollout(
            namespace="test-ns",
            rollout_name="test-rollout",
            stable_selector="app=test,role=stable",
            canary_selector="app=test,role=canary"
        )

        self.assertTrue(result["promote"])
        self.assertEqual(result["confidence"], 95)
        self.assertEqual(len(result["modelResults"]), 3)
        self.assertTrue(result["modelResults"][0]["promote"]) # Log analyst promote: True
        self.assertTrue(result["modelResults"][1]["promote"]) # Metrics analyst promote: True
        self.assertTrue(result["modelResults"][2]["promote"]) # Event analyst promote: True

    @patch("agents.orchestrator.Agent")
    @patch("agents.logs.LogAnalystAgent.analyze")
    @patch("agents.metrics.MetricsAnalystAgent.analyze")
    @patch("agents.events.EventAnalystAgent.analyze")
    async def test_orchestrator_revert_consensus(self, mock_events, mock_metrics, mock_logs, mock_agent_class):
        mock_logs.return_value = "Log report: Traceback observed. Failed."
        mock_metrics.return_value = "Metrics report: Memory usage high. Rollback."
        mock_events.return_value = "Events report: BackOff warnings. Fail."

        debate_response = """
        {
            "promote": false,
            "confidence": 90,
            "analysis": "Severe failures detected across logs, metrics, and cluster events.",
            "rootCause": "Memory leak and crash loops",
            "remediation": "Revert the rollout immediately and check heap memory usage",
            "votingRationale": "Consensus resolved to rollback due to multi-signal failure."
        }
        """
        mock_agent_class.return_value = MockAgentInstance(debate_response)

        orchestrator = LeadOrchestratorAgent()
        result = await orchestrator.analyze_rollout(
            namespace="test-ns",
            rollout_name="test-rollout",
            stable_selector="app=test,role=stable",
            canary_selector="app=test,role=canary"
        )

        self.assertFalse(result["promote"])
        self.assertEqual(result["confidence"], 90)
        self.assertEqual(result["rootCause"], "Memory leak and crash loops")
        self.assertEqual(len(result["modelResults"]), 3)
        self.assertFalse(result["modelResults"][0]["promote"]) # Log analyst promote: False
        self.assertFalse(result["modelResults"][1]["promote"]) # Metrics analyst promote: False
        self.assertFalse(result["modelResults"][2]["promote"]) # Event analyst promote: False

if __name__ == "__main__":
    unittest.main()
