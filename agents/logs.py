import os
import asyncio
import httpx
from google.antigravity import Agent, LocalAgentConfig

# Custom tool for fetching Kubernetes logs
def fetch_kubernetes_pod_logs(namespace: str, label_selector: str) -> str:
    """
    Fetches the logs of the first pod matching the label selector in the namespace.

    Args:
        namespace: The target Kubernetes namespace.
        label_selector: Label selector to identify the pods (e.g. 'role=canary').
    """
    print(f"[Tool] fetch_kubernetes_pod_logs called for namespace={namespace}, selector={label_selector}")

    # This agent runs in a gVisor Sandbox, which by cluster security policy
    # cannot mount a service account token and has no direct K8s API access.
    # Fetch logs via the trusted in-cluster log-proxy service instead.
    proxy_url = os.getenv("LOG_PROXY_URL")
    if not proxy_url:
        return "LOG_PROXY_URL is not configured; cannot fetch pod logs."

    try:
        resp = httpx.get(
            f"{proxy_url}/pod-logs",
            params={"namespace": namespace, "label_selector": label_selector},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()["logs"]
    except Exception as e:
        return f"Failed to fetch logs via log-proxy: {str(e)}"


class LogAnalystAgent:
    def __init__(self, model: str | None = None):
        # Allow override via environment variable if not passed directly
        effective_model = model or os.getenv("LOGS_AGENT_MODEL")
        self.config = LocalAgentConfig(
            system_instructions=(
                "You are a Kubernetes Log Analyst Agent. Your specialty is analyzing application logs for regressions. "
                "You have no direct Kubernetes API access. "
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
            f"in namespace '{namespace}'. You MUST invoke the 'fetch_kubernetes_pod_logs' tool exactly twice: "
            f"once with namespace='{namespace}', label_selector='{stable_selector}', and once with "
            f"namespace='{namespace}', label_selector='{canary_selector}'. Do NOT speculate or simulate logs. "
            f"Do NOT query any other namespace or selector. After these two calls, immediately produce your "
            f"final recommendation text — do not make further tool calls."
        )
        if extra_prompt:
            prompt += f"\nAdditional Context: {extra_prompt}"

        async with Agent(self.config) as agent:
            response = await agent.chat(prompt)
            return await response.text()

