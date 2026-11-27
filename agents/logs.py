"""Deterministic log collection; the sandbox never receives Kubernetes credentials."""
import asyncio
import os

import httpx

from agents.contracts import EvidenceReport

MAX_LOG_CHARS = 24_000


def fetch_kubernetes_pod_logs(namespace: str, label_selector: str) -> str:
    proxy_url = os.getenv("LOG_PROXY_URL")
    if not proxy_url:
        raise ValueError("LOG_PROXY_URL is not configured; cannot fetch pod logs")
    response = httpx.get(
        f"{proxy_url.rstrip('/')}/pod-logs",
        params={"namespace": namespace, "label_selector": label_selector},
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()["logs"]


def _truncate_logs(logs: str) -> str:
    if len(logs) <= MAX_LOG_CHARS:
        return logs
    if logs.startswith("--- LOGS FOR POD "):
        header, sep, body = logs.partition("\n")
        if sep and len(header) + len(sep) < MAX_LOG_CHARS:
            return header + sep + body[-(MAX_LOG_CHARS - len(header) - len(sep)):]
    return logs[-MAX_LOG_CHARS:]


class LogAnalystAgent:
    async def collect(self, namespace: str, stable_selector: str, canary_selector: str) -> EvidenceReport:
        report = EvidenceReport(source="logs")
        for side, selector in (("stable", stable_selector), ("canary", canary_selector)):
            try:
                logs = await asyncio.to_thread(fetch_kubernetes_pod_logs, namespace, selector)
                report.data[side] = _truncate_logs(logs)
                if len(logs) > MAX_LOG_CHARS:
                    report.warnings.append(f"{side} logs truncated to {MAX_LOG_CHARS} characters")
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                report.errors.append(f"{side} logs unavailable: {exc}")
        return report
