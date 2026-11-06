"""Gemini Decisions API (EAP) REST client and rollout evaluation rubric.

Calls POST https://generativelanguage.googleapis.com/v1beta/decisions with model
'gemini-decision-eap' to evaluate typed questions (noul, choice, score) over the
specialist agent reports in a single request with zero output tokens.
"""
from dataclasses import dataclass
import json
import os
from typing import Any

import httpx

DEFAULT_DECISION_MODEL = "gemini-decision-eap"
DEFAULT_DECISIONS_API_URL = "https://generativelanguage.googleapis.com/v1beta/decisions"
DEFAULT_PROMOTE_THRESHOLD = 0.70
DEFAULT_CODE_BUG_THRESHOLD = 0.70
DECISIONS_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class NoulDecision:
    noul: float
    confidence: float
    passed: bool

    @property
    def confidence_pct(self) -> int:
        return int(round(self.confidence * 100))


@dataclass(frozen=True)
class RolloutDecisionResult:
    promote: bool
    confidence: int
    promote_noul: float
    code_bug: bool
    code_bug_noul: float
    failure_category: str
    failure_category_confidence: float
    severity_score: float
    severity_label: str
    logs_decision: NoulDecision
    metrics_decision: NoulDecision
    events_decision: NoulDecision
    voting_rationale: str


def build_rollout_questions() -> dict[str, dict[str, Any]]:
    """Return the Decisions API questions map for evaluating a canary rollout."""
    return {
        "promote": {
            "type": "noul",
            "instructions": (
                "Based on `logs_report`, `metrics_report`, and `events_report`, "
                "is the canary deployment healthy and safe to promote to production?"
            ),
            "criteria": {
                "true_description": (
                    "Canary logs, resource metrics, and Kubernetes events show a healthy deployment "
                    "with no new errors, exceptions, memory leaks, CPU spikes, or warning events."
                ),
                "false_description": (
                    "Canary exhibits regressions such as exceptions, errors, high error rates, "
                    "memory leaks, CPU spikes, crash loops, probe failures, or warning events."
                ),
            },
        },
        "logs_healthy": {
            "type": "noul",
            "instructions": (
                "Does `logs_report` indicate the canary application logs are healthy and free of "
                "new exceptions, stack traces, or error regressions compared to stable?"
            ),
            "criteria": {
                "true_description": "Canary logs are clean, normal, and recommend promotion.",
                "false_description": "Canary logs contain exceptions, stack traces, error spikes, or recommend rollback.",
            },
        },
        "metrics_healthy": {
            "type": "noul",
            "instructions": (
                "Does `metrics_report` indicate the canary CPU and memory utilization are healthy "
                "without memory leaks, resource exhaustion, or CPU spikes compared to stable?"
            ),
            "criteria": {
                "true_description": "Canary CPU and memory usage are stable and within normal bounds.",
                "false_description": "Canary shows memory leaks, excessive memory usage, CPU spikes, or throttling.",
            },
        },
        "events_healthy": {
            "type": "noul",
            "instructions": (
                "Does `events_report` indicate the canary pods are healthy and free of Kubernetes "
                "warning events, crash loops, readiness probe failures, or OOM restarts?"
            ),
            "criteria": {
                "true_description": "Only normal lifecycle events are present for canary pods.",
                "false_description": "Warning events, BackOff, Unhealthy probes, FailedScheduling, or OOMKilled are present.",
            },
        },
        "code_bug": {
            "type": "noul",
            "instructions": (
                "Does `logs_report` show a bug in the application's own source code "
                "(for example an unhandled exception or stack trace pointing at the application's classes) "
                "that a source code change can fix?"
            ),
            "criteria": {
                "true_description": (
                    "Application logs show a code exception or bug in the application's own source code "
                    "(such as NullPointerException, ValueError, or a stack trace in app classes) fixable by a code change."
                ),
                "false_description": (
                    "Deployment is healthy, or the failure is an operational, resource, or infrastructure issue "
                    "such as a memory leak, CPU exhaustion, OOM, latency, or Kubernetes scheduling/probe issue."
                ),
            },
        },
        "failure_category": {
            "type": "choice",
            "instructions": (
                "What is the primary classification of the canary rollout status across "
                "`logs_report`, `metrics_report`, and `events_report`?"
            ),
            "criteria": {
                "application_bug": "Application source-code exception, stack trace, or HTTP 5xx bug in `logs_report`.",
                "healthy": "All signals (logs, metrics, and events) are healthy with no regressions.",
                "infrastructure_event": "Kubernetes lifecycle failure such as CrashLoopBackOff, FailedScheduling, or probe failure in `events_report`.",
                "other": "Any other issue or inconclusive telemetry.",
                "resource_regression": "Memory leak, OOM, CPU spike, or resource saturation in `metrics_report`.",
            },
        },
        "severity": {
            "type": "score",
            "instructions": (
                "How severe are the issues observed in the canary deployment across "
                "`logs_report`, `metrics_report`, and `events_report`?"
            ),
            "criteria": [
                "Healthy: no errors, normal resource usage, and normal cluster events.",
                "Minor warning: transient warning or negligible variance, safe to monitor.",
                "Degraded: noticeable error rate, resource spike, or probe instability.",
                "Critical failure: unhandled exception, crash loop, OOM, or severe memory leak blocking promotion.",
            ],
        },
    }


def _parse_noul(answers: dict[str, Any], key: str, threshold: float) -> NoulDecision:
    ans = answers.get(key) or {}
    noul = float(ans.get("noul", 0.01))
    passed = noul >= threshold
    raw_conf = ans.get("confidence")
    if raw_conf is not None:
        confidence = float(raw_conf)
    else:
        confidence = noul if passed else (1.0 - noul)
    return NoulDecision(noul=noul, confidence=confidence, passed=passed)


def parse_rollout_decisions(
    response_data: dict[str, Any],
    promote_threshold: float = DEFAULT_PROMOTE_THRESHOLD,
    code_bug_threshold: float = DEFAULT_CODE_BUG_THRESHOLD,
) -> RolloutDecisionResult:
    """Parse a Decisions API response JSON dict into a calibrated RolloutDecisionResult."""
    if "prompt_feedback" in response_data and "answers" not in response_data:
        block_reason = response_data["prompt_feedback"].get("block_reason", "UNKNOWN")
        msg = response_data["prompt_feedback"].get("block_reason_message", "")
        reason_str = f"{block_reason}: {msg}".strip(": ")
        blocked_decision = NoulDecision(noul=0.0, confidence=0.0, passed=False)
        return RolloutDecisionResult(
            promote=False,
            confidence=0,
            promote_noul=0.0,
            code_bug=False,
            code_bug_noul=0.0,
            failure_category="other",
            failure_category_confidence=0.0,
            severity_score=3.0,
            severity_label="Blocked by safety filter",
            logs_decision=blocked_decision,
            metrics_decision=blocked_decision,
            events_decision=blocked_decision,
            voting_rationale=f"Decisions API request blocked by safety filter ({reason_str}). Defaulting to ABORT.",
        )

    answers = response_data.get("answers") or {}
    if not answers:
        raise ValueError("Decisions API response did not contain 'answers'")

    promote_dec = _parse_noul(answers, "promote", promote_threshold)
    logs_dec = _parse_noul(answers, "logs_healthy", promote_threshold)
    metrics_dec = _parse_noul(answers, "metrics_healthy", promote_threshold)
    events_dec = _parse_noul(answers, "events_healthy", promote_threshold)
    code_bug_dec = _parse_noul(answers, "code_bug", code_bug_threshold)

    category_ans = answers.get("failure_category") or {}
    failure_category = str(category_ans.get("choice", "other"))
    failure_category_conf = float(category_ans.get("confidence", 0.0))

    severity_ans = answers.get("severity") or {}
    severity_score = float(severity_ans.get("score", 0.0))
    probabilities = severity_ans.get("probabilities") or {}
    legend = severity_ans.get("legend") or {}
    if probabilities and legend:
        top_level = max(probabilities, key=lambda k: float(probabilities[k]))
        severity_label = str(legend.get(top_level, ""))
    else:
        severity_label = ""

    # All three specialists and overall promote probability must pass threshold to promote
    promote = promote_dec.passed
    code_bug = (not promote) and code_bug_dec.passed

    verdict_str = "PROMOTE" if promote else "ROLLBACK"
    voting_rationale = (
        f"Decisions API ({verdict_str}): "
        f"P(promote)={promote_dec.noul:.2f} (confidence={promote_dec.confidence_pct}%), "
        f"P(logs_healthy)={logs_dec.noul:.2f}, "
        f"P(metrics_healthy)={metrics_dec.noul:.2f}, "
        f"P(events_healthy)={events_dec.noul:.2f}, "
        f"category={failure_category} ({failure_category_conf:.2f}), "
        f"severity={severity_score:.2f}/3.0, "
        f"P(code_bug)={code_bug_dec.noul:.2f}."
    )

    return RolloutDecisionResult(
        promote=promote,
        confidence=promote_dec.confidence_pct,
        promote_noul=promote_dec.noul,
        code_bug=code_bug,
        code_bug_noul=code_bug_dec.noul,
        failure_category=failure_category,
        failure_category_confidence=failure_category_conf,
        severity_score=severity_score,
        severity_label=severity_label,
        logs_decision=logs_dec,
        metrics_decision=metrics_dec,
        events_decision=events_dec,
        voting_rationale=voting_rationale,
    )


async def evaluate_rollout_decisions(
    rollout_name: str,
    namespace: str,
    logs_report: str,
    metrics_report: str,
    events_report: str,
    extra_prompt: str = "",
    model: str | None = None,
    api_key: str | None = None,
    api_url: str | None = None,
    promote_threshold: float | None = None,
    code_bug_threshold: float | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> RolloutDecisionResult:
    """Call the Gemini Decisions REST API to evaluate specialist reports for a rollout."""
    effective_model = model or os.getenv("DECISION_AGENT_MODEL") or DEFAULT_DECISION_MODEL
    effective_url = api_url or os.getenv("DECISIONS_API_URL") or DEFAULT_DECISIONS_API_URL
    effective_key = api_key if api_key is not None else os.getenv("GEMINI_API_KEY", "")
    eff_promote_threshold = (
        promote_threshold
        if promote_threshold is not None
        else float(os.getenv("DECISION_PROMOTE_THRESHOLD", str(DEFAULT_PROMOTE_THRESHOLD)))
    )
    eff_code_bug_threshold = (
        code_bug_threshold
        if code_bug_threshold is not None
        else float(os.getenv("DECISION_CODE_BUG_THRESHOLD", str(DEFAULT_CODE_BUG_THRESHOLD)))
    )

    input_payload = json.dumps(
        {
            "rollout_name": rollout_name,
            "namespace": namespace,
            "extra_prompt": extra_prompt,
            "logs_report": logs_report,
            "metrics_report": metrics_report,
            "events_report": events_report,
        }
    )

    request_body = {
        "model": effective_model,
        "input": [{"type": "text", "text": input_payload}],
        "questions": build_rollout_questions(),
    }

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": effective_key,
    }

    if http_client is not None:
        resp = await http_client.post(effective_url, json=request_body, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    else:
        async with httpx.AsyncClient(timeout=DECISIONS_TIMEOUT_SECONDS) as client:
            resp = await client.post(effective_url, json=request_body, headers=headers)
            resp.raise_for_status()
            data = resp.json()

    return parse_rollout_decisions(
        data,
        promote_threshold=eff_promote_threshold,
        code_bug_threshold=eff_code_bug_threshold,
    )
