import argparse
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, Any
from agents.orchestrator import LeadOrchestratorAgent
from agents.logs import LogAnalystAgent
from agents.metrics import MetricsAnalystAgent
from agents.events import EventAnalystAgent

# Define CLI argument parser
parser = argparse.ArgumentParser(description="Kubernetes AI/Ops Agent - Google Antigravity SDK")
parser.add_argument(
    "--role", 
    choices=["orchestrator", "logs", "metrics", "events"], 
    default="orchestrator",
    help="Role of this agent microservice instance (default: orchestrator)"
)
parser.add_argument("--host", default="0.0.0.0", help="HTTP server host")
parser.add_argument("--port", type=int, default=8080, help="HTTP server port")
args = parser.parse_args()

app = FastAPI(title=f"Kubernetes AI/Ops Agent ({args.role}) - Google Antigravity SDK")

# Define Pydantic request models
class A2ARequest(BaseModel):
    userId: str
    prompt: str
    context: Dict[str, Any]
    memoryId: Optional[str] = None

class SpecialistRequest(BaseModel):
    namespace: str
    stableSelector: str
    canarySelector: str
    extraPrompt: Optional[str] = ""

@app.get("/")
def health_check():
    """Simple health check endpoint."""
    return {"status": "healthy", "service": "kubernetes-agents-antigravity", "role": args.role}

# Define endpoints based on CLI role
if args.role == "orchestrator":
    @app.post("/a2a/analyze")
    async def analyze_canary(request: A2ARequest):
        """
        Agent-to-Agent endpoint triggered by the Argo Rollouts AI metric plugin.
        Delegates analysis to the specialized agents.
        """
        ctx = request.context
        namespace = ctx.get("namespace")
        rollout_name = ctx.get("rolloutName")
        stable_selector = ctx.get("stableSelector")
        canary_selector = ctx.get("canarySelector")
        extra_prompt = ctx.get("extraPrompt", "")

        if not namespace or not rollout_name or not stable_selector or not canary_selector:
            raise HTTPException(
                status_code=400,
                detail="Missing required context fields (namespace, rolloutName, stableSelector, canarySelector)"
            )

        try:
            orchestrator = LeadOrchestratorAgent()
            result = await orchestrator.analyze_rollout(
                namespace=namespace,
                rollout_name=rollout_name,
                stable_selector=stable_selector,
                canary_selector=canary_selector,
                extra_prompt=extra_prompt
            )
            return result
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Internal orchestrator error: {str(e)}")

elif args.role == "logs":
    @app.post("/analyze/logs")
    async def analyze_logs(request: SpecialistRequest):
        """Analyze logs and return a text report."""
        try:
            agent = LogAnalystAgent()
            report = await agent.analyze(
                namespace=request.namespace,
                stable_selector=request.stableSelector,
                canary_selector=request.canarySelector,
                extra_prompt=request.extraPrompt
            )
            return {"analysis": report}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Logs analysis error: {str(e)}")

elif args.role == "metrics":
    @app.post("/analyze/metrics")
    async def analyze_metrics(request: SpecialistRequest):
        """Analyze pod resource metrics and return a text report."""
        try:
            agent = MetricsAnalystAgent()
            report = await agent.analyze(
                namespace=request.namespace,
                stable_selector=request.stableSelector,
                canary_selector=request.canarySelector,
                extra_prompt=request.extraPrompt
            )
            return {"analysis": report}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Metrics analysis error: {str(e)}")

elif args.role == "events":
    @app.post("/analyze/events")
    async def analyze_events(request: SpecialistRequest):
        """Analyze namespace events and return a text report."""
        try:
            agent = EventAnalystAgent()
            report = await agent.analyze(
                namespace=request.namespace,
                stable_selector=request.stableSelector,
                canary_selector=request.canarySelector,
                extra_prompt=request.extraPrompt
            )
            return {"analysis": report}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Events analysis error: {str(e)}")


if __name__ == "__main__":
    uvicorn.run(app, host=args.host, port=args.port)
