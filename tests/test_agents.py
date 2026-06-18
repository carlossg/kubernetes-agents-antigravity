import asyncio
import sys
from agents.logs import LogAnalystAgent
from agents.metrics import MetricsAnalystAgent
from agents.events import EventAnalystAgent
from agents.orchestrator import LeadOrchestratorAgent

async def run_local_tests():
    print("==================================================")
    # 1. Log Analyst Agent
    print("🧪 [Test 1/4] Running LogAnalystAgent...")
    logs_agent = LogAnalystAgent()
    logs_report = await logs_agent.analyze(
        namespace="rollouts-demo",
        stable_selector="app=canary-demo,role=stable",
        canary_selector="app=canary-demo,role=canary"
    )
    print("✓ Log Analyst Agent Output:\n", logs_report)
    print("==================================================")

    # 2. Metrics Analyst Agent
    print("🧪 [Test 2/4] Running MetricsAnalystAgent...")
    metrics_agent = MetricsAnalystAgent()
    metrics_report = await metrics_agent.analyze(
        namespace="rollouts-demo",
        stable_selector="app=canary-demo,role=stable",
        canary_selector="app=canary-demo,role=canary"
    )
    print("✓ Metrics Analyst Agent Output:\n", metrics_report)
    print("==================================================")

    # 3. Event Analyst Agent
    print("🧪 [Test 3/4] Running EventAnalystAgent...")
    events_agent = EventAnalystAgent()
    events_report = await events_agent.analyze(
        namespace="rollouts-demo",
        stable_selector="app=canary-demo,role=stable",
        canary_selector="app=canary-demo,role=canary"
    )
    print("✓ Event Analyst Agent Output:\n", events_report)
    print("==================================================")

    # 4. Lead Orchestrator Agent
    print("🧪 [Test 4/4] Running LeadOrchestratorAgent (Debate & Consensus)...")
    orchestrator = LeadOrchestratorAgent()
    consensus = await orchestrator.analyze_rollout(
        namespace="rollouts-demo",
        rollout_name="canary-demo",
        stable_selector="app=canary-demo,role=stable",
        canary_selector="app=canary-demo,role=canary"
    )
    print("✓ Lead Orchestrator Consensus Output (JSON):")
    import pprint
    pprint.pprint(consensus)
    print("==================================================")

    # Simple validations
    assert "promote" in consensus, "Consensus JSON missing 'promote' key"
    assert "confidence" in consensus, "Consensus JSON missing 'confidence' key"
    assert "analysis" in consensus, "Consensus JSON missing 'analysis' key"
    assert len(consensus.get("modelResults", [])) == 3, "Consensus missing individual agent modelResults"
    print("✨ All local agent tests completed successfully!")

if __name__ == "__main__":
    asyncio.run(run_local_tests())
