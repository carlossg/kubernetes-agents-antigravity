"""Application counter windows and resource snapshots, collected without an LLM."""
import asyncio
import math
import os
import re
import time

from kubernetes import client
from urllib3.exceptions import HTTPError as KubernetesHTTPError

from agents import activity
from agents.contracts import COLLECTOR_TIMEOUT, EvidenceReport
from agents.kube import REQUEST_TIMEOUT, ensure_config, list_pods

SAMPLING_TIMEOUT_SECONDS = COLLECTOR_TIMEOUT - 25.0
COLLECTION_DEADLINE_SECONDS = COLLECTOR_TIMEOUT - 5.0
SAMPLE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*(?:\{.*\})?)\s+(\S+)(?:\s+\S+)?$')
LABEL_PAIR = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"((?:\\.|[^"\\])*)"(?:\s*,\s*|\s*$)')
COUNTERS = {
    "http_requests_total": "requests",
    "http_requests_success_total": "successes",
    "http_requests_error_total": "errors",
    "http_request_duration_seconds_sum": "latencySumSeconds",
    "http_request_duration_seconds_count": "latencyCount",
}


def _canonicalize_series(series: str) -> tuple[str, str, dict[str, str]]:
    if "{" not in series:
        return series, series, {}
    name, rest = series.split("{", 1)
    if not rest.endswith("}"):
        raise ValueError("Malformed application metric sample")
    inner = rest[:-1].strip()
    if not inner:
        return name, name, {}
    labels = {}
    pos = 0
    while pos < len(inner):
        match = LABEL_PAIR.match(inner, pos)
        if not match:
            raise ValueError("Malformed application metric sample")
        label_name, label_value = match.groups()
        if label_name in labels:
            raise ValueError(f"Duplicate label in metric series: {series}")
        labels[label_name] = label_value
        pos = match.end()
    canonical_labels = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    return name, f"{name}{{{canonical_labels}}}", labels


def parse_application_metrics(text: str) -> dict:
    counters = {}
    gauges = {}
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        match = SAMPLE.fullmatch(line.strip())
        if not match:
            raise ValueError("Malformed application metric sample")
        series, raw = match.groups()
        name = series.split("{", 1)[0]
        relevant = name in COUNTERS or name == "http_request_duration_seconds"
        if not relevant:
            continue
        value = float(raw)
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"Invalid application metric value: {name}")
        _, canonical, labels = _canonicalize_series(series)
        if name in COUNTERS:
            if canonical in counters:
                raise ValueError(f"Duplicate application metric series: {canonical}")
            counters[canonical] = value
        else:
            quantile = labels.get("quantile")
            if quantile in ("0.95", "0.99"):
                key = f"p{quantile[2:]}Ms"
                # Individual summary quantiles cannot be averaged into a fleet percentile.
                gauges[key] = max(gauges.get(key, 0), value * 1000)
    if not any(k.split("{", 1)[0] == "http_requests_total" for k in counters):
        raise ValueError("Application endpoint has no http_requests_total counter")
    return {"counters": counters, "maxPodQuantiles": gauges}


def window_metrics(first: dict, latest: dict, seconds: float, excluded: set[str] | None = None) -> dict:
    requests = errors = latency_sum = latency_count = 0.0
    has_errors = has_latency = True
    worst_latency = None
    worst_pod = None
    warnings = []
    usable = 0
    for pod_key, after in latest.items():
        if excluded and pod_key in excluded:
            warnings.append(f"{pod_key}: counter discontinuity observed; excluded from window")
            continue
        before = first.get(pod_key)
        if before is None:
            warnings.append(f"{pod_key}: new pod, no baseline")
            continue
        if before.get("restartCount", 0) != after.get("restartCount", 0):
            warnings.append(f"{pod_key}: container restart observed; excluded from window")
            continue
        old, new = before["counters"], after["counters"]
        if old.keys() != new.keys() or any(new[k] < old[k] for k in old.keys() & new.keys()):
            warnings.append(f"{pod_key}: counter reset or series change; excluded from window")
            continue
        deltas = {}
        for series, value in new.items():
            field = COUNTERS[series.split("{", 1)[0]]
            deltas[field] = deltas.get(field, 0) + value - old[series]
        pod_requests = deltas["requests"]
        pod_errors = deltas.get("errors")
        if pod_errors is None and "successes" in deltas:
            pod_errors = pod_requests - deltas["successes"]
        if pod_errors is None:
            has_errors = False
        elif not 0 <= pod_errors <= pod_requests:
            warnings.append(f"{pod_key}: inconsistent request/error counters")
            continue
        else:
            errors += pod_errors
        requests += pod_requests
        usable += 1
        if "latencyCount" in deltas and "latencySumSeconds" in deltas:
            count = deltas["latencyCount"]
            latency_delta = deltas["latencySumSeconds"]
            if count > 0:
                latency_sum += latency_delta
                latency_count += count
                mean = latency_delta / count * 1000
                if worst_latency is None or mean > worst_latency:
                    worst_latency, worst_pod = mean, pod_key
            elif count != 0 or latency_delta != 0 or pod_requests != 0:
                has_latency = False
        else:
            has_latency = False
    for pod_key in first.keys() - latest.keys():
        warnings.append(f"{pod_key}: pod disappeared during window")
    if not has_errors:
        warnings.append("Window error counters are unavailable")
    if not has_latency:
        warnings.append("Window latency counters are unavailable")
    return {
        "windowSeconds": seconds,
        "windowRequests": requests if usable else None,
        "windowErrorRate": errors / requests * 100 if requests > 0 and has_errors else None,
        "windowMeanLatencyMs": latency_sum / latency_count * 1000 if latency_count > 0 and has_latency else None,
        "windowWorstPodMeanLatencyMs": worst_latency,
        "windowWorstPod": worst_pod,
        "complete": not warnings and usable > 0,
        "warnings": warnings,
    }


def scrape_application_metrics(namespace: str, selector: str) -> tuple[dict, list[str]]:
    api = client.CoreV1Api()
    snapshots = {}
    errors = []
    try:
        pods = list_pods(api, namespace, selector)
    except (client.ApiException, KubernetesHTTPError, OSError) as exc:
        return {}, [f"Pod listing unavailable for {selector}: {exc}"]
    if not pods:
        return {}, [f"No pods match {selector}"]
    port = int(os.getenv("APPLICATION_METRICS_PORT", "8080"))
    if not 1 <= port <= 65535:
        raise ValueError("Invalid APPLICATION_METRICS_PORT")
    path = os.getenv("APPLICATION_METRICS_PATH", "/q/metrics").lstrip("/")
    for pod in pods:
        key = f"{pod.metadata.name}/{pod.metadata.uid}"
        try:
            text = api.connect_get_namespaced_pod_proxy_with_path(
                name=f"{pod.metadata.name}:{port}", namespace=namespace,
                path=path, _request_timeout=REQUEST_TIMEOUT,
            )
            snapshots[key] = parse_application_metrics(text)
            snapshots[key]["restartCount"] = sum(
                s.restart_count for s in (pod.status.container_statuses or [])
            )
        except (client.ApiException, KubernetesHTTPError, OSError, ValueError) as exc:
            errors.append(f"{pod.metadata.name}: application metrics unavailable: {exc}")
    return snapshots, errors


def fetch_kubernetes_pod_metrics(namespace: str, label_selector: str) -> dict:
    custom_api = client.CustomObjectsApi()
    response = custom_api.list_namespaced_custom_object(
        group="metrics.k8s.io", version="v1beta1", namespace=namespace,
        plural="pods", label_selector=label_selector, _request_timeout=REQUEST_TIMEOUT,
    )
    items = response.get("items", [])
    if not items:
        raise ValueError(f"No resource metrics match {label_selector}")
    return {
        item["metadata"]["name"]: {
            c["name"]: c["usage"] for c in item.get("containers", [])
        }
        for item in items
    }


class MetricsDataAgent:
    async def collect(self, namespace: str, stable_selector: str, canary_selector: str) -> EvidenceReport:
        await asyncio.to_thread(ensure_config)
        minimum = int(os.getenv("ANALYSIS_MIN_CANARY_REQUESTS", "500"))
        seconds = float(os.getenv("ANALYSIS_SAMPLE_WINDOW_SECONDS", "10"))
        waits = int(os.getenv("ANALYSIS_MAX_SAMPLE_WAITS", "3"))
        if minimum < 1 or not 0 < seconds <= 30 or not 0 <= waits <= 3:
            raise ValueError("Invalid application sampling configuration")
        report = EvidenceReport(source="metrics")
        selectors = {"stable": stable_selector, "canary": canary_selector}

        async def scrape(selector):
            return await asyncio.to_thread(scrape_application_metrics, namespace, selector)

        start = time.monotonic()
        sampling_deadline = start + SAMPLING_TIMEOUT_SECONDS
        total_deadline = start + COLLECTION_DEADLINE_SECONDS
        baseline_timed_out = False
        try:
            async with asyncio.timeout(max(0.01, sampling_deadline - time.monotonic())):
                initial = await asyncio.gather(*(scrape(s) for s in selectors.values()))
        except TimeoutError:
            baseline_timed_out = True
            initial = [({}, []), ({}, [])]
            report.errors.append("metrics baseline timed out at collection deadline")
        baseline = {side: pair[0] for side, pair in zip(selectors, initial)}
        initial_errors = {side: list(pair[1]) for side, pair in zip(selectors, initial)}
        previous = {side: dict(snap) for side, snap in baseline.items()}
        seen_pods = {side: set(snap) for side, snap in baseline.items()}
        invalid_pods = {side: set() for side in selectors}
        for side, (_, errors) in zip(selectors, initial):
            report.errors.extend(f"{side}: {e}" for e in errors)
        if not baseline_timed_out:
            for attempt in range(waits + 1):
                remaining = sampling_deadline - time.monotonic()
                if remaining <= 0:
                    report.warnings.append("metrics sampling stopped at collection deadline")
                    break
                try:
                    async with asyncio.timeout(remaining):
                        await asyncio.sleep(seconds)
                        samples = await asyncio.gather(*(scrape(s) for s in selectors.values()))
                except TimeoutError:
                    report.warnings.append("metrics sampling stopped at collection deadline")
                    break
                elapsed = time.monotonic() - start
                for side, (snapshot, errors) in zip(selectors, samples):
                    report.errors.extend(f"{side}: {e}" for e in errors)
                    for pod, sample in snapshot.items():
                        if pod in previous[side]:
                            prior = previous[side][pod]
                            old, new = prior["counters"], sample["counters"]
                            if (old.keys() != new.keys()
                                    or any(new[k] < old[k] for k in old.keys() & new.keys())
                                    or sample.get("restartCount", 0) != prior.get("restartCount", 0)):
                                invalid_pods[side].add(pod)
                        previous[side][pod] = sample
                    seen_pods[side].update(snapshot)
                    window = window_metrics(baseline[side], snapshot, elapsed, invalid_pods[side])
                    full_coverage = (
                        bool(snapshot)
                        and not initial_errors[side]
                        and not errors
                        and seen_pods[side] == set(baseline[side]) == set(snapshot)
                    )
                    report.data[side] = {
                        **window,
                        "sampleSufficient": window["complete"] and full_coverage and (window["windowRequests"] or 0) >= minimum,
                        "minRequests": minimum,
                        "maxPodQuantiles": {
                            pod: sample["maxPodQuantiles"] for pod, sample in snapshot.items()
                        },
                    }
                    report.warnings.extend(f"{side}: {w}" for w in window["warnings"])
                if report.data["canary"]["sampleSufficient"]:
                    break
                if attempt < waits:
                    activity.feed.emit(activity.TOOL_RESULT, "Gathering more canary evidence",
                                       f"requests={report.data['canary']['windowRequests']} minimum={minimum}")
        for side, selector in selectors.items():
            if side in report.data and not report.data[side]["sampleSufficient"]:
                report.warnings.append(f"{side}: insufficient or incomplete request window")
            remaining = total_deadline - time.monotonic()
            if remaining <= 0:
                report.errors.append(f"{side}: resource snapshot skipped at collection deadline")
                continue
            try:
                async with asyncio.timeout(remaining):
                    resources = await asyncio.to_thread(
                        fetch_kubernetes_pod_metrics, namespace, selector,
                    )
                report.data.setdefault(side, {})["resources"] = resources
            except (client.ApiException, KubernetesHTTPError, OSError, ValueError, TimeoutError) as exc:
                report.errors.append(f"{side}: resource snapshot unavailable: {exc}")
        if "stable" in report.data and "canary" in report.data:
            stable, canary = report.data["stable"], report.data["canary"]
            report.data["comparisons"] = {}
            for key in ("windowErrorRate", "windowMeanLatencyMs", "windowWorstPodMeanLatencyMs"):
                a, b = stable.get(key), canary.get(key)
                if a is not None and b is not None:
                    report.data["comparisons"][key] = {
                        "stable": a, "canary": b, "delta": b - a,
                        "ratio": b / a if a > 0 else None,
                    }
        return report


MetricsAnalystAgent = MetricsDataAgent
