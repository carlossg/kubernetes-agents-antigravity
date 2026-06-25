import os
import asyncio
from google.antigravity import Agent, LocalAgentConfig
from kubernetes import client, config as k8s_config

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
            # Let's inspect contexts in kubeconfig first
            import yaml
            kubeconfig_path = os.path.expanduser("~/.kube/config")
            if os.path.exists(kubeconfig_path):
                with open(kubeconfig_path, "r") as f:
                    config_data = yaml.safe_load(f)
                contexts = config_data.get("contexts", [])
                context_names = [c.get("name") for c in contexts if c.get("name")]
                current_context = config_data.get("current-context")
                
                # If current-context is empty or invalid, try using the first valid context
                if not current_context and context_names:
                    k8s_config.load_kube_config(context=context_names[0])
                else:
                    k8s_config.load_kube_config()
            else:
                k8s_config.load_kube_config()
        except Exception as e:
            return f"Failed to load kubernetes configuration. Error: {str(e)}. Path exists: {os.path.exists(kubeconfig_path)}"

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
            return f"No metrics found in namespace '{namespace}' matching selector '{label_selector}'"
        
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
    except Exception as e:
        return f"Failed to fetch metrics: {str(e)}"


class MetricsAnalystAgent:
    def __init__(self, model: str | None = None):
        # Allow override via environment variable if not passed directly
        effective_model = model or os.getenv("METRICS_AGENT_MODEL")
        self.config = LocalAgentConfig(
            system_instructions=(
                "You are a Kubernetes Metrics Analyst Agent. Your specialty is analyzing resource usage trends (CPU and Memory). "
                "You are running inside a Kubernetes Pod with in-cluster service account authentication. "
                "Do NOT attempt to read kubeconfig files like '/root/.kube/config' or run local commands. "
                "Use ONLY the provided tool 'fetch_kubernetes_pod_metrics' to fetch pod metrics. "
                "Compare the resource consumption of stable pods vs canary pods. Look for memory leaks, CPU spikes, or throttling. "
                "Determine if the metrics indicate a stable deployment that is safe to promote, or a regression, and provide your vote."
            ),
            tools=[fetch_kubernetes_pod_metrics],
            model=effective_model
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

