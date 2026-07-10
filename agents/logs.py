import os
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
    print(f"[Tool] fetch_kubernetes_pod_logs called for namespace={namespace}, selector={label_selector}")
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

    v1 = client.CoreV1Api()
    try:
        pods = v1.list_namespaced_pod(namespace, label_selector=label_selector)
        if not pods.items:
            return f"No pods found in namespace '{namespace}' matching selector '{label_selector}'"
        
        pod_name = pods.items[0].metadata.name
        # Get logs (limit to last 200 lines to avoid token saturation)
        logs = v1.read_namespaced_pod_log(pod_name, namespace, tail_lines=200)
        return f"--- LOGS FOR POD {pod_name} ({label_selector}) ---\n{logs}"
    except Exception as e:
        return f"Failed to fetch logs: {str(e)}"


class LogAnalystAgent:
    def __init__(self, model: str | None = None):
        # Allow override via environment variable if not passed directly
        effective_model = model or os.getenv("LOGS_AGENT_MODEL")
        self.config = LocalAgentConfig(
            system_instructions=(
                "You are a Kubernetes Log Analyst Agent. Your specialty is analyzing application logs for regressions. "
                "You are running inside a Kubernetes Pod with in-cluster service account authentication. "
                "Do NOT attempt to read kubeconfig files like '/root/.kube/config' or run local commands. "
                "Use ONLY the provided tool 'fetch_kubernetes_pod_logs' to retrieve logs. "
                "Compare the logs of stable pods vs canary pods. Look for exceptions, error rates, and regressions. "
                "Determine if the canary pod logs indicate a healthy application that can be promoted, "
                "or if there are new failures, and output your recommendation."
            ),
            # Register the custom log fetching tool
            tools=[fetch_kubernetes_pod_logs],
            model=effective_model
        )

    async def analyze(self, namespace: str, stable_selector: str, canary_selector: str, extra_prompt: str = "") -> str:
        prompt = (
            f"Analyze logs for stable selector '{stable_selector}' and canary selector '{canary_selector}' "
            f"in namespace '{namespace}'. You MUST invoke the 'fetch_kubernetes_pod_logs' tool for both the stable and canary selectors to get the real logs. Do NOT speculate or simulate logs."
        )
        if extra_prompt:
            prompt += f"\nAdditional Context: {extra_prompt}"

        async with Agent(self.config) as agent:
            response = await agent.chat(prompt)
            return await response.text()

