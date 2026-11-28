import argparse
import asyncio
from contextlib import asynccontextmanager
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from kubernetes import client
from kubernetes.config.config_exception import ConfigException
from pydantic import BaseModel, ValidationError
from urllib3.exceptions import HTTPError as KubernetesHTTPError

from agents import activity, remediation
from agents.contracts import AnalysisContext
from agents.events import EventAnalystAgent
from agents.logs import LogAnalystAgent
from agents.metrics import MetricsDataAgent
from agents.orchestrator import LeadOrchestratorAgent


class A2ARequest(BaseModel):
    userId: str
    prompt: str
    context: dict[str, Any]
    memoryId: str | None = None


class SpecialistRequest(BaseModel):
    namespace: str
    stableSelector: str
    canarySelector: str
    extraPrompt: str | None = ""


@asynccontextmanager
async def lifespan(app):
    yield
    await remediation.shutdown()


def create_app(role: str = "orchestrator") -> FastAPI:
    app = FastAPI(title=f"Kubernetes AI/Ops Agent ({role})", lifespan=lifespan)

    @app.get("/")
    def health_check():
        return {"status": "healthy", "service": "kubernetes-agents-antigravity", "role": role}

    if role == "orchestrator":
        @app.post("/analyze")
        @app.post("/a2a/analyze")
        async def analyze_canary(request: A2ARequest):
            try:
                ctx = AnalysisContext.model_validate(request.context)
            except ValidationError as exc:
                raise HTTPException(status_code=400, detail=exc.errors(include_input=False)) from exc
            try:
                orchestrator = LeadOrchestratorAgent(
                    model=ctx.model, decision_model=ctx.decisionModel,
                    second_opinion_model=ctx.secondOpinionModel,
                )
                async with asyncio.timeout(270):
                    return await orchestrator.analyze_rollout(
                        namespace=ctx.namespace, rollout_name=ctx.rolloutName,
                        stable_selector=ctx.stableSelector, canary_selector=ctx.canarySelector,
                        extra_prompt=ctx.extraPrompt, repo_url=ctx.repoUrl, base_branch=ctx.baseBranch,
                        pause_on_uncertainty=ctx.pauseOnUncertainty,
                        uncertainty_threshold=ctx.uncertaintyThreshold, prompt=request.prompt,
                    )
            except (httpx.HTTPError, ValueError, RuntimeError, TimeoutError,
                    client.ApiException, ConfigException, KubernetesHTTPError, OSError) as exc:
                activity.feed.emit(activity.ERROR, "Orchestrator error", str(exc))
                raise HTTPException(status_code=500, detail="Analysis failed; see activity feed") from exc

        @app.get("/events")
        @app.get("/a2a/events")
        def activity_events(since: int | None = None):
            return activity.feed.events(since)

    elif role in {"logs", "metrics", "events"}:
        collectors = {"logs": LogAnalystAgent, "metrics": MetricsDataAgent, "events": EventAnalystAgent}

        @app.post(f"/analyze/{role}")
        async def collect(request: SpecialistRequest):
            try:
                report = await collectors[role]().collect(
                    request.namespace, request.stableSelector, request.canarySelector,
                )
                return {"analysis": report.text(), "evidence": report.model_dump()}
            except (httpx.HTTPError, ValueError, client.ApiException, ConfigException,
                    KubernetesHTTPError, OSError) as exc:
                activity.feed.emit(activity.ERROR, f"{role} collection error", str(exc))
                raise HTTPException(status_code=500, detail=f"{role} collection failed") from exc

    elif role == "log-proxy":
        from agents.log_proxy import get_first_pod_logs

        @app.get("/pod-logs")
        def pod_logs(namespace: str, label_selector: str):
            try:
                return {"logs": get_first_pod_logs(namespace, label_selector)}
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except (client.ApiException, ConfigException, KubernetesHTTPError, OSError) as exc:
                activity.feed.emit(activity.ERROR, "Log proxy error", str(exc))
                raise HTTPException(status_code=500, detail="Log proxy failed") from exc
    elif role == "remediation":
        from agents.remediation_server import register_remediation_routes

        register_remediation_routes(app)
    else:
        raise ValueError(f"Unknown role: {role}")
    return app


app = create_app()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Kubernetes AI/Ops Agent - Gemini")
    parser.add_argument("--role", choices=["orchestrator", "logs", "metrics", "events", "log-proxy", "remediation"],
                        default="orchestrator")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    uvicorn.run(create_app(args.role), host=args.host, port=args.port)
