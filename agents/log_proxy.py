from kubernetes import client, config as k8s_config

# Namespaces the log-proxy will never read from, even though its service
# account has cluster-wide pod/log read access.
FORBIDDEN_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease", "argo-rollouts"}

_config_loaded = False


def _ensure_config():
    global _config_loaded
    if not _config_loaded:
        k8s_config.load_incluster_config()
        _config_loaded = True


def get_first_pod_logs(namespace: str, label_selector: str) -> str:
    """Fetches the logs of the first non-terminating pod matching the label selector.

    Runs only in the trusted (non-sandboxed) log-proxy service, which holds the
    in-cluster service account. The sandboxed Log Analyst agent has no direct
    K8s API access by cluster policy and calls this over HTTP instead.
    """
    if namespace.lower().strip() in FORBIDDEN_NAMESPACES:
        raise ValueError(f"Access to namespace '{namespace}' is forbidden.")

    _ensure_config()
    v1 = client.CoreV1Api()
    pods = v1.list_namespaced_pod(namespace, label_selector=label_selector)
    if not pods.items:
        return f"No pods found in namespace '{namespace}' matching selector '{label_selector}'"

    pod = next((p for p in pods.items if p.metadata.deletion_timestamp is None), pods.items[0])
    pod_name = pod.metadata.name

    container_name = ""
    for container in pod.spec.containers:
        if container.name not in ("istio-proxy", "istio-init"):
            container_name = container.name
            break
    if not container_name and pod.spec.containers:
        container_name = pod.spec.containers[0].name

    logs = v1.read_namespaced_pod_log(pod_name, namespace, container=container_name, tail_lines=200)
    return f"--- LOGS FOR POD {pod_name} ({label_selector}) ---\n{logs}"
