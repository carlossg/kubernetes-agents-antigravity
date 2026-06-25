import os
import asyncio
from google.antigravity import Agent, LocalAgentConfig
from kubernetes import client, config as k8s_config

# Custom tool for listing Kubernetes events
def fetch_kubernetes_namespace_events(namespace: str) -> str:
    """
    Lists the recent events in the target namespace to find warnings or failures.
    
    Args:
        namespace: The target Kubernetes namespace.
    """
    try:
        k8s_config.load_incluster_config()
    except Exception:
        try:
            k8s_config.load_kube_config()
        except Exception as e:
            return f"Failed to load kubernetes configuration: {str(e)}"

    v1 = client.CoreV1Api()
    try:
        events = v1.list_namespaced_event(namespace, limit=30)
        if not events.items:
            return f"No events found in namespace '{namespace}'"
        
        event_lines = []
        for e in events.items:
            reason = e.reason
            message = e.message
            obj_kind = e.involved_object.kind
            obj_name = e.involved_object.name
            e_type = e.type # Warning or Normal
            count = e.count or 1
            event_lines.append(
                f"[{e_type}] Object: {obj_kind}/{obj_name}, Reason: {reason}, Message: {message} (Count: {count})"
            )
        
        return "--- RECENT NAMESPACE EVENTS ---\n" + "\n".join(event_lines)
    except Exception as e:
        return f"Failed to fetch events: {str(e)}"


class EventAnalystAgent:
    def __init__(self, model: str | None = None):
        # Allow override via environment variable if not passed directly
        effective_model = model or os.getenv("EVENTS_AGENT_MODEL")
        self.config = LocalAgentConfig(
            system_instructions=(
                "You are a Kubernetes Event Analyst Agent. Your specialty is examining warning events, crash loops, "
                "readiness probe failures, or OOM restarts in the namespace. "
                "You are running inside a Kubernetes Pod with in-cluster service account authentication. "
                "Do NOT attempt to read kubeconfig files like '/root/.kube/config' or run local commands. "
                "Use ONLY the provided tool 'fetch_kubernetes_namespace_events' to inspect recent namespace events. "
                "Identify any events relating to the stable/canary pods. "
                "Determine if there are infrastructure or lifecycle warnings that should block the promotion."
            ),
            tools=[fetch_kubernetes_namespace_events],
            model=effective_model
        )

    async def analyze(self, namespace: str, stable_selector: str, canary_selector: str, extra_prompt: str = "") -> str:
        prompt = (
            f"Analyze recent events in namespace '{namespace}' focusing on stable selector '{stable_selector}' "
            f"and canary selector '{canary_selector}'."
        )
        if extra_prompt:
            prompt += f"\nAdditional Context: {extra_prompt}"

        async with Agent(self.config) as agent:
            response = await agent.chat(prompt)
            return await response.text()

