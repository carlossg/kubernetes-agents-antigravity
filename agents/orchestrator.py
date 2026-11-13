"""Collect -> analyze -> grade -> optional second opinion -> pause -> remediate."""
import os

import httpx
from kubernetes import client
from kubernetes.config.config_exception import ConfigException

from agents import activity
from agents.analysis import AnalysisAgent, SecondOpinionAnalysisAgent
from agents.contracts import AnalysisContext, AnalysisResult, EvidenceBundle, RemediationRequest
from agents.data import AnalysisDataAgent
from agents.decisions import AnalysisGrade, grade_analysis
from agents.pause import pause_rollout
from agents.remediation import RemediationAgent


class AnalysisOrchestratorAgent:
    def __init__(
        self, model: str | None = None, decision_model: str | None = None,
        second_opinion_model: str | None = None,
    ):
        self.primary = AnalysisAgent(model)
        self.decision_model = decision_model
        self.second_opinion_model = (
            second_opinion_model if second_opinion_model is not None
            else os.getenv("ANALYSIS_SECOND_OPINION_MODEL", "")
        )
        self.grading_mode = os.getenv("DECISION_GRADING_MODE", "shadow")
        if self.grading_mode not in {"shadow", "enforce"}:
            raise ValueError("DECISION_GRADING_MODE must be shadow or enforce")

    async def _grade(self, result: AnalysisResult, evidence: EvidenceBundle) -> AnalysisGrade | None:
        try:
            grade = await grade_analysis(result, evidence, model=self.decision_model)
        except (httpx.HTTPError, ValueError) as exc:
            activity.feed.emit(activity.ERROR, "Grader unavailable; using self-reported confidence", str(exc))
            return None
        activity.feed.emit(
            activity.QUALITY_CHECKED,
            "Conclusion disputed" if grade.disputed else "Conclusion supported" if grade.supported >= 0.5 else "Evidence neutral",
            f"mode={self.grading_mode}; supportScore={grade.supported:.3f}; "
            f"contradiction={grade.contradiction:.3f}; sufficiency={grade.sufficiency:.3f}",
        )
        return grade

    async def _analyze(self, agent: AnalysisAgent, evidence: EvidenceBundle, notes: str) -> AnalysisResult:
        activity.feed.emit(activity.AGENT_START, agent.__class__.__name__, agent.model)
        result = await agent.analyze(evidence, notes)
        activity.feed.emit(activity.AGENT_COMPLETE, agent.__class__.__name__, agent.model)
        activity.feed.emit(activity.ANALYSIS_SUMMARY, result.summary, result.analysis)
        activity.feed.emit(activity.CONFIDENCE_SCORE, f"Self-reported confidence {result.confidence}%")
        return result

    async def analyze_rollout(
        self, namespace: str, rollout_name: str, stable_selector: str, canary_selector: str,
        extra_prompt: str = "", repo_url: str = "", base_branch: str = "main",
        pause_on_uncertainty: bool = False, uncertainty_threshold: int = 50, prompt: str = "",
    ) -> dict:
        context = AnalysisContext(
            namespace=namespace, rolloutName=rollout_name, stableSelector=stable_selector,
            canarySelector=canary_selector, pauseOnUncertainty=pause_on_uncertainty,
            uncertaintyThreshold=uncertainty_threshold,
        )
        activity.feed.emit(activity.ANALYSIS_START, f"Analyzing canary rollout '{rollout_name}'", namespace)
        evidence = await AnalysisDataAgent().collect(namespace, rollout_name, stable_selector, canary_selector)
        notes = "\n\n".join(s for s in (prompt, extra_prompt) if s)
        result = await self._analyze(self.primary, evidence, notes)
        model_results = [{"modelName": self.primary.model, **result.model_dump()}]
        grade = await self._grade(result, evidence)
        if self.grading_mode == "enforce" and grade is not None and grade.disputed and self.second_opinion_model:
            activity.feed.emit(activity.ANALYSIS_ESCALATED, "Requesting an independent second opinion",
                               self.second_opinion_model)
            try:
                escalated = await self._analyze(
                    SecondOpinionAnalysisAgent(self.second_opinion_model), evidence, notes,
                )
            except (httpx.HTTPError, ValueError) as exc:
                activity.feed.emit(activity.ERROR, "Second opinion unavailable; retaining primary conclusion", str(exc))
            else:
                result = escalated
                model_results.append({"modelName": self.second_opinion_model, **result.model_dump()})
                grade = await self._grade(result, evidence)

        effective = (
            grade.effective_confidence(result.confidence)
            if self.grading_mode == "enforce" and grade is not None else result.confidence
        )
        paused = None
        if context.pauseOnUncertainty and result.promote and effective < context.uncertaintyThreshold:
            try:
                paused = await pause_rollout(namespace, rollout_name)
            except (client.ApiException, ConfigException, ValueError, RuntimeError, OSError):
                # Fail the HTTP measurement rather than allow an uncertain promotion after a failed hold.
                activity.feed.emit(activity.ERROR, "Required rollout pause failed", f"{namespace}/{rollout_name}")
                raise
            activity.feed.emit(activity.PAUSE_GATE_TRIGGERED, f"Paused rollout '{paused}'",
                               f"effectiveConfidence={effective}; threshold={context.uncertaintyThreshold}")

        verdict = "PROMOTE" if result.promote else "ABORT"
        activity.feed.emit(activity.CONFIDENCE_SCORE, f"Effective confidence {effective}%")
        activity.feed.emit(activity.DECISION, f"{verdict}: {result.summary}", result.rootCause)
        if not result.promote and repo_url:
            RemediationAgent().delegate(RemediationRequest(
                repoUrl=repo_url, baseBranch=base_branch or "main",
                issueCategory=result.issueCategory, title=result.summary, rootCause=result.rootCause,
                analysis=result.analysis, diagnosticContext=evidence.model_dump_json()[:4000],
            ))
        rationale = (
            f"Decision 1 ({self.grading_mode}): support score={grade.supported:.3f}; effective confidence={effective}%"
            if grade is not None else "Grader unavailable; self-reported confidence used"
        )
        response = {
            **result.model_dump(), "codeBug": result.issueCategory == "CODE_BUG",
            "votingRationale": rationale, "effectiveConfidence": effective,
            "gradingMode": self.grading_mode,
            "modelResults": model_results,
            "evidenceStatus": {name: report.status for name, report in evidence.reports.items()},
        }
        if paused is not None:
            response["pausedRollout"] = paused
        return response


LeadOrchestratorAgent = AnalysisOrchestratorAgent
