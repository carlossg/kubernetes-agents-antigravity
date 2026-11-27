"""An opt-in hold for uncertain promotion; failed writes are never reported as pauses."""
import asyncio
import threading

from kubernetes import client

from agents.kube import REQUEST_TIMEOUT, ensure_config, validate_namespace


def _pause_rollout(namespace: str, rollout_name: str, cancelled: threading.Event | None = None) -> str:
    validate_namespace(namespace)
    ensure_config()
    api = client.CustomObjectsApi()
    arguments = {
        "group": "argoproj.io", "version": "v1alpha1", "namespace": namespace,
        "plural": "rollouts", "name": rollout_name, "_request_timeout": REQUEST_TIMEOUT,
    }
    rollout = api.get_namespaced_custom_object(**arguments)
    if not rollout.get("spec", {}).get("paused"):
        if cancelled is not None and cancelled.is_set():
            raise RuntimeError("Rollout pause cancelled before write")
        api.patch_namespaced_custom_object(**arguments, body={"spec": {"paused": True}})
        rollout = api.get_namespaced_custom_object(**arguments)
    if rollout.get("spec", {}).get("paused") is not True:
        raise RuntimeError("Rollout pause was not persisted")
    return rollout_name


async def pause_rollout(namespace: str, rollout_name: str) -> str:
    cancelled = threading.Event()
    try:
        return await asyncio.to_thread(_pause_rollout, namespace, rollout_name, cancelled)
    except BaseException:
        cancelled.set()
        raise
