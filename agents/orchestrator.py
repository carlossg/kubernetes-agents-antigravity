import asyncio
import json
import re
from google.antigravity import Agent, LocalAgentConfig
from agents.logs import LogAnalystAgent
from agents.metrics import MetricsAnalystAgent
from agents.events import EventAnalystAgent
from agents import activity
from agents.remediation import remediate

REMEDIATION_TIMEOUT_SECONDS = 150
_background_tasks: set = set()


async def _remediate_with_timeout(**kwargs):
    try:
        return await asyncio.wait_for(remediate(**kwargs), timeout=REMEDIATION_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        activity.feed.emit(activity.REMEDIATION, "Remediation timed out; no pull request created")
        return None

class LeadOrchestratorAgent:
    def __init__(self, model: str | None = None, log_model: str | None = None, metrics_model: str | None = None, event_model: str | None = None):
        import os
        effective_model = model or os.getenv("ORCHESTRATOR_AGENT_MODEL")
        self.config = LocalAgentConfig(
            system_instructions=(
                "You are the Lead Orchestrator and Consensus Resolver Agent. "
                "Your job is to read evaluations from three specialized agents (Logs, Metrics, Events) "
                "about a canary deployment, synthesize their feedback, resolve any voting disagreements, "
                "and make the final promote/rollback decision.\n\n"
                "You MUST respond ONLY with a single valid JSON block containing these exact fields:\n"
                "{\n"
                '  "promote": true or false,\n'
                '  "confidence": 0 to 100,\n'
                '  "analysis": "detailed synthesis of findings",\n'
                '  "rootCause": "root cause of any issue, empty if promote is true",\n'
                '  "remediation": "remediation steps or PR recommendations, empty if promote is true",\n'
                '  "codeBug": true only if the root cause is a bug in application source code (for example an exception with a stack trace pointing at the app\'s own classes) that a code change can fix; false for operational issues such as memory leaks, resource exhaustion, latency or infrastructure problems,\n'
                '  "votingRationale": "summary of the team debate and how you resolved conflicts"\n'
                "}\n"
                "Ensure your response is valid JSON that can be parsed directly by json.loads()."
            ),
            model=effective_model
        )
        self.log_model = log_model or os.getenv("LOGS_AGENT_MODEL")
        self.metrics_model = metrics_model or os.getenv("METRICS_AGENT_MODEL")
        self.event_model = event_model or os.getenv("EVENTS_AGENT_MODEL")

    async def analyze_rollout(
        self, 
        namespace: str, 
        rollout_name: str, 
        stable_selector: str, 
        canary_selector: str, 
        extra_prompt: str = "",
        repo_url: str = "",
        base_branch: str = "main",
    ) -> dict:
        import os
        import httpx

        logs_url = os.getenv("LOGS_AGENT_URL")
        metrics_url = os.getenv("METRICS_AGENT_URL")
        events_url = os.getenv("EVENTS_AGENT_URL")

        # Define helper for calling agents (either HTTP or local fallback)
        SUBAGENT_TIMEOUT = 90.0

        async def tracked(name: str, coro):
            activity.feed.emit(activity.AGENT_START, name, f"Analyzing rollout '{rollout_name}'")
            activity.feed.emit(activity.TOOL_CALL, name, f"namespace={namespace} stable={stable_selector} canary={canary_selector}")
            report = await coro
            activity.feed.emit(activity.TOOL_RESULT, name, report[:500])
            activity.feed.emit(activity.AGENT_COMPLETE, name)
            return report

        async def run_logs():
            if logs_url:
                try:
                    async with httpx.AsyncClient(timeout=SUBAGENT_TIMEOUT) as client:
                        payload = {
                            "namespace": namespace,
                            "stableSelector": stable_selector,
                            "canarySelector": canary_selector,
                            "extraPrompt": extra_prompt
                        }
                        if self.log_model:
                            payload["model"] = self.log_model
                        resp = await client.post(f"{logs_url}/analyze/logs", json=payload)
                        return resp.json()["analysis"]
                except Exception as e:
                    return f"Failed to call remote LogAnalystAgent: {str(e)}"
            else:
                logs_agent = LogAnalystAgent(model=self.log_model)
                return await logs_agent.analyze(namespace, stable_selector, canary_selector, extra_prompt)

        async def run_metrics():
            if metrics_url:
                try:
                    async with httpx.AsyncClient(timeout=SUBAGENT_TIMEOUT) as client:
                        payload = {
                            "namespace": namespace,
                            "stableSelector": stable_selector,
                            "canarySelector": canary_selector,
                            "extraPrompt": extra_prompt
                        }
                        if self.metrics_model:
                            payload["model"] = self.metrics_model
                        resp = await client.post(f"{metrics_url}/analyze/metrics", json=payload)
                        return resp.json()["analysis"]
                except Exception as e:
                    return f"Failed to call remote MetricsAnalystAgent: {str(e)}"
            else:
                metrics_agent = MetricsAnalystAgent(model=self.metrics_model)
                return await metrics_agent.analyze(namespace, stable_selector, canary_selector, extra_prompt)

        async def run_events():
            if events_url:
                try:
                    async with httpx.AsyncClient(timeout=SUBAGENT_TIMEOUT) as client:
                        payload = {
                            "namespace": namespace,
                            "stableSelector": stable_selector,
                            "canarySelector": canary_selector,
                            "extraPrompt": extra_prompt
                        }
                        if self.event_model:
                            payload["model"] = self.event_model
                        resp = await client.post(f"{events_url}/analyze/events", json=payload)
                        return resp.json()["analysis"]
                except Exception as e:
                    return f"Failed to call remote EventAnalystAgent: {str(e)}"
            else:
                events_agent = EventAnalystAgent(model=self.event_model)
                return await events_agent.analyze(namespace, stable_selector, canary_selector, extra_prompt)


        # Execute specialized analyses concurrently
        print(f"[Orchestrator] Starting concurrent subagent evaluations for rollout '{rollout_name}'...")
        activity.feed.emit(
            activity.ANALYSIS_START,
            f"Analyzing canary rollout '{rollout_name}'",
            f"namespace={namespace} stable={stable_selector} canary={canary_selector}",
        )
        logs_report, metrics_report, events_report = await asyncio.gather(
            tracked("LogAnalystAgent", run_logs()),
            tracked("MetricsAnalystAgent", run_metrics()),
            tracked("EventAnalystAgent", run_events()),
        )

        print("[Orchestrator] Subagent evaluations completed. Aggregating reports for consensus debate...")

        # Build debate prompt
        debate_prompt = (
            f"We have three specialist agent evaluations for rollout '{rollout_name}' in namespace '{namespace}':\n\n"
            f"--- LOG ANALYST REPORT ---\n{logs_report}\n\n"
            f"--- METRICS ANALYST REPORT ---\n{metrics_report}\n\n"
            f"--- KUBERNETES EVENT REPORT ---\n{events_report}\n\n"
            f"Synthesize these findings, perform a debate, and produce the final JSON response."
        )

        # Execute debate and consensus synthesis
        async with Agent(self.config) as agent:
            response = await agent.chat(debate_prompt)
            response_text = await response.text()

        # Parse the JSON response
        try:
            # Extract JSON block if surrounded by markdown or extra text
            json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group(0))
            else:
                result = json.loads(response_text)
        except Exception as e:
            print(f"[Orchestrator] Error parsing JSON response: {str(e)}. Raw output was: {response_text}")
            result = {
                "promote": False,
                "confidence": 0,
                "analysis": f"Failed to synthesize reports: {str(e)}. Raw output: {response_text}",
                "rootCause": "Consensus parsing failure",
                "remediation": "Check orchestrator agent logic",
                "votingRationale": "Raw text was not valid JSON"
            }

        verdict = "PROMOTE" if result.get("promote") else "ABORT"
        activity.feed.emit(
            activity.DECISION,
            f"{verdict}: {str(result.get('analysis', ''))[:200]}",
            result.get("rootCause", ""),
        )
        activity.feed.emit(activity.CONFIDENCE_SCORE, f"Confidence {result.get('confidence', 0)}%")

        if not result.get("promote") and result.get("codeBug") and repo_url:
            # Run in the background: the verdict must not wait for the PR, or the rollout can
            # finish promoting before the analysis result reaches Argo Rollouts.
            task = asyncio.create_task(
                _remediate_with_timeout(
                    repo_url=repo_url,
                    base_branch=base_branch,
                    rollout_name=rollout_name,
                    namespace=namespace,
                    root_cause=result.get("rootCause", ""),
                    logs_report=logs_report,
                    analysis=str(result.get("analysis", "")),
                )
            )
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
        elif not result.get("promote"):
            activity.feed.emit(
                activity.REMEDIATION,
                "No pull request: operational issue, not a source-code bug",
                result.get("rootCause", ""),
            )

        activity.feed.emit(activity.ANALYSIS_SUMMARY, f"Analysis of '{rollout_name}' complete: {verdict}")

        # Inject individual subagent results for trace reports
        result["modelResults"] = [
            {
                "modelName": "LogAnalystAgent",
                "analysis": logs_report,
                "promote": "promote: true" in logs_report.lower() or "recommend promotion" in logs_report.lower() or "healthy" in logs_report.lower(),
                "confidence": 85,
                "rootCause": "",
                "remediation": ""
            },
            {
                "modelName": "MetricsAnalystAgent",
                "analysis": metrics_report,
                "promote": "promote: true" in metrics_report.lower() or "recommend promotion" in metrics_report.lower() or "healthy" in metrics_report.lower(),
                "confidence": 85,
                "rootCause": "",
                "remediation": ""
            },
            {
                "modelName": "EventAnalystAgent",
                "analysis": events_report,
                "promote": "warning" not in events_report.lower() and "fail" not in events_report.lower(),
                "confidence": 80,
                "rootCause": "",
                "remediation": ""
            }
        ]

        return result
