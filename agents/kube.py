"""Kubernetes access for trusted deterministic collectors, never model tools."""
import os

from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException

from agents.log_proxy import FORBIDDEN_NAMESPACES

REQUEST_TIMEOUT = 10


def ensure_config():
    try:
        config.load_incluster_config()
    except ConfigException:
        if os.getenv("KUBERNETES_SERVICE_HOST"):
            raise
        config.load_kube_config()


def validate_namespace(namespace: str):
    if namespace.lower().strip() in FORBIDDEN_NAMESPACES:
        raise ValueError(f"Access to namespace '{namespace}' is forbidden.")


def list_pods(api, namespace: str, selector: str):
    validate_namespace(namespace)
    pods = api.list_namespaced_pod(
        namespace, label_selector=selector, _request_timeout=REQUEST_TIMEOUT,
    )
    return [p for p in pods.items if p.metadata.deletion_timestamp is None]


def pod_diagnostics(pod) -> dict:
    statuses = pod.status.container_statuses or []
    return {
        "name": pod.metadata.name,
        "uid": pod.metadata.uid,
        "phase": pod.status.phase,
        "containers": [
            {
                "name": s.name,
                "ready": s.ready,
                "restarts": s.restart_count,
                "waiting": s.state.waiting.reason if s.state and s.state.waiting else None,
                "lastTermination": (
                    s.last_state.terminated.reason
                    if s.last_state and s.last_state.terminated else None
                ),
            }
            for s in statuses
        ],
    }
