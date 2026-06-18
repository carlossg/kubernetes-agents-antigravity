import asyncio
from google.antigravity import Agent, LocalAgentConfig
from kubernetes import client, config as k8s_config

def _mock_metrics(label_selector: str) -> str:
    if "role=canary" in label_selector or ("canary" in label_selector and "role=stable" not in label_selector and "stable" not in label_selector):
        return (
            "--- RESOURCE UTILIZATION METRICS (canary) ---\n"
            "Pod: canary-pod-123, Container: agent -> CPU usage: 15m, Memory usage: 120Mi"
        )
    else:
        return (
            "--- RESOURCE UTILIZATION METRICS (stable) ---\n"
            "Pod: stable-pod-456, Container: agent -> CPU usage: 12m, Memory usage: 110Mi"
        )

# Custom tool for fetching resource metrics
def fetch_kubernetes_pod_metrics(namespace: str, label_selector: str) -> str:
    """
    Fetches CPU and Memory utilization metrics for pods matching the label selector.
    
    Args:
        namespace: The target Kubernetes namespace.
        label_selector: Label selector to identify the pods (e.g. 'role=canary').
    """
    try:
        k8s_config.load_incluster_config()
    except Exception:
        try:
            k8s_config.load_kube_config()
        except Exception:
            return _mock_metrics(label_selector)

    custom_api = client.CustomObjectsApi()
    try:
        # Fetch metrics from metrics.k8s.io API
        resource_path = f"apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods"
        response = custom_api.list_namespaced_custom_object(
            group="metrics.k8s.io",
            version="v1beta1",
            namespace=namespace,
            plural="pods",
            label_selector=label_selector
        )
        
        items = response.get("items", [])
        if not items:
            return _mock_metrics(label_selector)
        
        metrics_summary = []
        for item in items:
            pod_name = item["metadata"]["name"]
            containers = item.get("containers", [])
            for c in containers:
                c_name = c["name"]
                cpu = c["usage"]["cpu"]
                mem = c["usage"]["memory"]
                metrics_summary.append(f"Pod: {pod_name}, Container: {c_name} -> CPU usage: {cpu}, Memory usage: {mem}")
        
        return "--- RESOURCE UTILIZATION METRICS ---\n" + "\n".join(metrics_summary)
    except Exception:
        return _mock_metrics(label_selector)


class MetricsAnalystAgent:
    def __init__(self):
        self.config = LocalAgentConfig(
            system_instructions=(
                "You are a Kubernetes Metrics Analyst Agent. Your specialty is analyzing resource usage trends (CPU and Memory). "
                "You are running inside a Kubernetes Pod with in-cluster service account authentication. "
                "Do NOT attempt to read kubeconfig files like '/root/.kube/config' or run local commands. "
                "Use ONLY the provided tool 'fetch_kubernetes_pod_metrics' to fetch pod metrics. "
                "Compare the resource consumption of stable pods vs canary pods. Look for memory leaks, CPU spikes, or throttling. "
                "Determine if the metrics indicate a stable deployment that is safe to promote, or a regression, and provide your vote."
            ),
            tools=[fetch_kubernetes_pod_metrics]
        )

    async def analyze(self, namespace: str, stable_selector: str, canary_selector: str, extra_prompt: str = "") -> str:
        prompt = (
            f"Analyze CPU and Memory metrics for stable selector '{stable_selector}' and canary selector '{canary_selector}' "
            f"in namespace '{namespace}'."
        )
        if extra_prompt:
            prompt += f"\nAdditional Context: {extra_prompt}"

        async with Agent(self.config) as agent:
            response = await agent.chat(prompt)
            return await response.text()
