import asyncio
from google.antigravity import Agent, LocalAgentConfig
from kubernetes import client, config as k8s_config

# Custom tool for fetching Kubernetes logs
def fetch_kubernetes_pod_logs(namespace: str, label_selector: str) -> str:
    """
    Fetches the logs of the first pod matching the label selector in the namespace.
    
    Args:
        namespace: The target Kubernetes namespace.
        label_selector: Label selector to identify the pods (e.g. 'role=canary').
    """
    try:
        k8s_config.load_incluster_config()
    except Exception:
        try:
            k8s_config.load_kube_config()
        except Exception as e:
            return f"Failed to load Kubernetes configuration: {str(e)}"

    v1 = client.CoreV1Api()
    try:
        pods = v1.list_namespaced_pod(namespace, label_selector=label_selector)
        if not pods.items:
            return f"No pods found for selector '{label_selector}' in namespace '{namespace}'"
        
        pod_name = pods.items[0].metadata.name
        # Get logs (limit to last 200 lines to avoid token saturation)
        logs = v1.read_namespaced_pod_log(pod_name, namespace, tail_lines=200)
        return f"--- LOGS FOR POD {pod_name} ({label_selector}) ---\n{logs}"
    except Exception as e:
        return f"Error reading logs for selector '{label_selector}': {str(e)}"


class LogAnalystAgent:
    def __init__(self):
        self.config = LocalAgentConfig(
            system_instructions=(
                "You are a Kubernetes Log Analyst Agent. Your specialty is analyzing application logs for regressions. "
                "You are provided with a tool 'fetch_kubernetes_pod_logs' to fetch pod logs. "
                "Compare the logs of stable pods vs canary pods. Look for exceptions, error rates, and regressions. "
                "Determine if the canary pod logs indicate a healthy application that can be promoted, "
                "or if there are new failures, and output your recommendation."
            ),
            # Register the custom log fetching tool
            tools=[fetch_kubernetes_pod_logs]
        )

    async def analyze(self, namespace: str, stable_selector: str, canary_selector: str, extra_prompt: str = "") -> str:
        prompt = (
            f"Analyze logs for stable selector '{stable_selector}' and canary selector '{canary_selector}' "
            f"in namespace '{namespace}'."
        )
        if extra_prompt:
            prompt += f"\nAdditional Context: {extra_prompt}"

        async with Agent(self.config) as agent:
            response = await agent.chat(prompt)
            return await response.text()
