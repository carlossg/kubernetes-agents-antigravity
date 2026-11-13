"""Deterministic pod-scoped events and readiness diagnostics."""
import asyncio

from kubernetes import client

from agents.contracts import EvidenceReport
from agents.kube import REQUEST_TIMEOUT, ensure_config, list_pods, pod_diagnostics


def collect_events(namespace: str, stable_selector: str, canary_selector: str) -> EvidenceReport:
    ensure_config()
    api = client.CoreV1Api()
    report = EvidenceReport(source="pod_diagnostics")
    for side, selector in (("stable", stable_selector), ("canary", canary_selector)):
        pods = list_pods(api, namespace, selector)
        if not pods:
            report.errors.append(f"No {side} pods match {selector}")
            continue
        report.data[side] = {"pods": [pod_diagnostics(p) for p in pods], "events": []}
        for pod in pods:
            events = api.list_namespaced_event(
                namespace, field_selector=f"involvedObject.uid={pod.metadata.uid}",
                _request_timeout=REQUEST_TIMEOUT,
            )
            # Restrict evidence to these pod UIDs, not unrelated namespace warnings.
            recent = sorted(
                events.items,
                key=lambda e: str(e.last_timestamp or e.event_time or e.metadata.creation_timestamp or ""),
                reverse=True,
            )[:20]
            report.data[side]["events"].extend(
                {
                    "pod": pod.metadata.name, "type": e.type, "reason": e.reason,
                    "message": (e.message or "")[:2000], "count": e.count,
                }
                for e in recent
            )
    return report


class EventAnalystAgent:
    async def collect(self, namespace: str, stable_selector: str, canary_selector: str) -> EvidenceReport:
        return await asyncio.to_thread(collect_events, namespace, stable_selector, canary_selector)
