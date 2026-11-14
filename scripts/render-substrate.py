#!/usr/bin/env python3
"""Render native Substrate API documents, not Kubernetes resources."""
import argparse
import hashlib
import json
import re


def documents(image):
    if not re.fullmatch(r"[a-zA-Z0-9._:/-]+@sha256:[a-f0-9]{64}", image):
        raise ValueError("Substrate actor image must be pinned as name@sha256:<64 lowercase hex characters>")
    template = "logs-" + hashlib.sha256(image.encode()).hexdigest()[:16]
    return {
        "template": {
            "metadata": {"atespace": "argo-rollouts", "name": template},
            "workerSelector": {"matchLabels": {"workload": "kubernetes-agent-logs"}},
            "containers": [{
                "name": "logs", "image": image,
                "command": ["python", "main.py", "--role", "logs"],
                "env": [
                    {"name": "HOME", "value": "/tmp"},
                    {"name": "LOG_PROXY_URL", "value": "http://kubernetes-agent-log-proxy.argo-rollouts.svc.cluster.local:8080"},
                ],
                "securityContext": {"capabilities": {"drop": ["ALL"]}},
                "wakeupProbe": {"httpGet": {"path": "/", "port": 8080}},
            }],
            "resources": {"limits": {"cpuMillicores": 500, "memoryBytes": 1073741824}},
        },
        "policy": {
            "rules": [{"http": {
                "hostnames": ["kubernetes-agent-log-proxy.argo-rollouts.svc.cluster.local"],
                "ports": {"numbers": [8080]},
            }}],
        },
        "template-name": template,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", choices=["template", "policy", "template-name"])
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    try:
        value = documents(args.image)[args.document]
    except ValueError as exc:
        parser.error(str(exc))
    print(value if isinstance(value, str) else json.dumps(value, indent=2))
