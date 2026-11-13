"""Validated boundaries between evidence, analysis, grading, and remediation."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvidenceReport(BaseModel):
    source: str
    data: dict[str, Any] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def status(self) -> str:
        if self.errors:
            return "partial" if self.data else "unavailable"
        return "ok" if self.data else "unavailable"

    def text(self) -> str:
        return self.model_dump_json()


class EvidenceBundle(BaseModel):
    namespace: str
    rolloutName: str
    stableSelector: str
    canarySelector: str
    reports: dict[str, EvidenceReport]


class MetricComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str
    canary: str
    stable: str
    verdict: Literal["pass", "fail"]


class AnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    promote: bool
    confidence: int = Field(ge=0, le=100)
    analysis: str = Field(min_length=1)
    rootCause: str
    remediation: str
    summary: str = Field(min_length=1, max_length=120)
    issueCategory: Literal["CODE_BUG", "OPERATIONAL", "NO_ISSUES"]
    suspectClasses: list[str]
    metricComparisons: list[MetricComparison] = Field(max_length=6)

    @model_validator(mode="after")
    def consistent_verdict(self):
        if self.promote != (self.issueCategory == "NO_ISSUES"):
            raise ValueError("promote and issueCategory disagree")
        return self


class AnalysisContext(BaseModel):
    namespace: str = Field(min_length=1, pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63)
    rolloutName: str = Field(min_length=1, pattern=r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$", max_length=253)
    stableSelector: str = Field(min_length=1)
    canarySelector: str = Field(min_length=1)
    extraPrompt: str = ""
    repoUrl: str = ""
    baseBranch: str = "main"
    model: str | None = None
    secondOpinionModel: str | None = None
    decisionModel: str | None = None
    pauseOnUncertainty: bool = False
    uncertaintyThreshold: int = 50

    @model_validator(mode="after")
    def clamp_threshold(self):
        self.uncertaintyThreshold = max(0, min(100, self.uncertaintyThreshold))
        return self


class RemediationRequest(BaseModel):
    repoUrl: str
    baseBranch: str
    issueCategory: Literal["CODE_BUG", "OPERATIONAL"]
    title: str
    rootCause: str
    analysis: str
    diagnosticContext: str


class RemediationResponse(BaseModel):
    status: Literal[
        "success", "tests_failed", "no_changes", "agent_error", "rejected",
        "issue", "timeout", "infra_error", "pr_failed",
    ]
    prLink: str | None = None
    issueLink: str | None = None
    message: str | None = None
