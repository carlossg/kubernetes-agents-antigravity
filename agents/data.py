"""Deterministic scatter/gather workers and evidence combiner."""
import asyncio
import os

import httpx
from kubernetes import client
from kubernetes.config.config_exception import ConfigException
from urllib3.exceptions import HTTPError as KubernetesHTTPError

from agents import activity
from agents.contracts import COLLECTOR_TIMEOUT, EvidenceBundle, EvidenceReport
from agents.events import EventAnalystAgent
from agents.kube import REQUEST_TIMEOUT, ensure_config, validate_namespace
from agents.logs import LogAnalystAgent
from agents.metrics import MetricsDataAgent


async def collect_report(name: str, url: str | None, path: str, local, payload: dict) -> EvidenceReport:
    activity.feed.emit(activity.AGENT_START, name)
    try:
        async with asyncio.timeout(COLLECTOR_TIMEOUT):
            if url:
                async with httpx.AsyncClient(timeout=COLLECTOR_TIMEOUT) as http:
                    response = await http.post(f"{url.rstrip('/')}{path}", json=payload)
                    response.raise_for_status()
                    report = EvidenceReport.model_validate(response.json()["evidence"])
            else:
                report = await local()
    except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError,
            client.ApiException, ConfigException, KubernetesHTTPError, OSError) as exc:
        report = EvidenceReport(source=name, errors=[f"{name} unavailable: {exc}"])
    for error in report.errors:
        activity.feed.emit(activity.ERROR, f"{name} collection error", error)
    activity.feed.emit(activity.TOOL_RESULT, name, f"status={report.status}; " + "; ".join(report.warnings))
    activity.feed.emit(activity.AGENT_COMPLETE, name)
    return report


class DiagnosticsDataAgent:
    async def collect(self, payload: dict) -> EvidenceReport:
        args = (payload["namespace"], payload["stableSelector"], payload["canarySelector"])
        logs, pods = await asyncio.gather(
            collect_report("LogsCollector", os.getenv("LOGS_AGENT_URL"), "/analyze/logs",
                           lambda: LogAnalystAgent().collect(*args), payload),
            collect_report("PodDiagnosticsCollector", os.getenv("EVENTS_AGENT_URL"), "/analyze/events",
                           lambda: EventAnalystAgent().collect(*args), payload),
        )
        data = {}
        if logs.data:
            data["logs"] = logs.model_dump()
        if pods.data:
            data["pods"] = pods.model_dump()
        return EvidenceReport(
            source="diagnostics",
            data=data,
            errors=logs.errors + pods.errors, warnings=logs.warnings + pods.warnings,
        )


class RolloutStatusDataAgent:
    def _collect(self, namespace: str, rollout_name: str) -> EvidenceReport:
        validate_namespace(namespace)
        ensure_config()
        rollout = client.CustomObjectsApi().get_namespaced_custom_object(
            group="argoproj.io", version="v1alpha1", namespace=namespace,
            plural="rollouts", name=rollout_name, _request_timeout=REQUEST_TIMEOUT,
        )
        spec, status = rollout.get("spec", {}), rollout.get("status", {})
        return EvidenceReport(source="rollout", data={
            "name": rollout["metadata"]["name"],
            "generation": rollout["metadata"].get("generation"),
            "observedGeneration": status.get("observedGeneration"),
            "paused": spec.get("paused", False),
            "replicas": spec.get("replicas"),
            "status": {
                key: status[key] for key in (
                    "phase", "message", "currentStepIndex", "currentPodHash", "stableRS",
                    "readyReplicas", "availableReplicas", "updatedReplicas", "conditions", "pauseConditions",
                ) if key in status
            },
        })

    async def collect(self, namespace: str, rollout_name: str) -> EvidenceReport:
        return await asyncio.to_thread(self._collect, namespace, rollout_name)


class DataCombinerAgent:
    def combine(self, namespace: str, rollout_name: str, stable_selector: str,
                canary_selector: str, reports: dict[str, EvidenceReport]) -> EvidenceBundle:
        return EvidenceBundle(
            namespace=namespace, rolloutName=rollout_name, stableSelector=stable_selector,
            canarySelector=canary_selector, reports=reports,
        )


class AnalysisDataAgent:
    async def collect(self, namespace: str, rollout_name: str,
                      stable_selector: str, canary_selector: str) -> EvidenceBundle:
        payload = {"namespace": namespace, "stableSelector": stable_selector, "canarySelector": canary_selector}
        reports = await asyncio.gather(
            collect_report("DiagnosticsDataAgent", None, "",
                           lambda: DiagnosticsDataAgent().collect(payload), payload),
            collect_report("RolloutStatusDataAgent", None, "",
                           lambda: RolloutStatusDataAgent().collect(namespace, rollout_name), payload),
            collect_report("MetricsDataAgent", os.getenv("METRICS_AGENT_URL"), "/analyze/metrics",
                           lambda: MetricsDataAgent().collect(namespace, stable_selector, canary_selector), payload),
        )
        return DataCombinerAgent().combine(
            namespace, rollout_name, stable_selector, canary_selector,
            dict(zip(("diagnostics", "rollout", "metrics"), reports)),
        )
