#!/usr/bin/env bash
# The actual demo, meant to be run live while `asciinema rec` is capturing
# this terminal. Run ./prepare-baseline.sh first so canary-demo starts
# Healthy on blue.
#
# Each act prints a grep-able "ACT_MARKER" line before its banner. After
# recording, build-video.sh scans the .cast file for these markers to
# derive the exact wall-clock timestamp of each act automatically - AI
# response latency varies run to run, so timestamps can't be hardcoded.
#
# Requires: kubectl, kubectl-argo-rollouts plugin, jq, GNU timeout.

set -uo pipefail

NAMESPACE="${NAMESPACE:-default}"
AGENT_NS="argo-rollouts"

act() {
	local id="$1" title="$2"
	echo ""
	echo "### ACT_MARKER: ${id} ###"
	echo "════════════════════════════════════════════════════════════"
	echo "  ${title}"
	echo "════════════════════════════════════════════════════════════"
	echo ""
}

pause() {
	sleep "${1:-2}"
}

show_verdict() {
	local revision_label="$1"
	local run
	run=$(kubectl get analysisrun -n "${NAMESPACE}" \
		-l "app=canary-demo,rollout-type=Background" \
		--sort-by=.metadata.creationTimestamp -o name 2>/dev/null | tail -1)
	if [ -z "${run}" ]; then
		echo "(no AnalysisRun found yet)"
		return
	fi
	echo "--- AI verdict (${revision_label}) ---"
	kubectl get -n "${NAMESPACE}" "${run}" -o json |
		jq -r '.status.metricResults[0].measurements[-1].metadata as $m |
			"promote:    " + ($m.analysisJSON | fromjson | .promote | tostring) +
			"\nconfidence: " + $m.confidence +
			"\n\n" + $m.analysis +
			"\n\n--- voting rationale ---\n" + ($m.votingRationale // "n/a")'
}

# ---------------------------------------------------------------------------
act "architecture" "ACT 1 — Meet the agents"
# ---------------------------------------------------------------------------
echo "\$ kubectl get pods -n ${AGENT_NS}"
kubectl get pods -n "${AGENT_NS}"
pause 4

echo ""
echo "One orchestrator, three specialists (logs, metrics, events), plus a"
echo "log-proxy that gives the sandboxed log agent safe, policy-compliant"
echo "access to real pod logs. They debate every canary and vote."
pause 4

# ---------------------------------------------------------------------------
act "success_start" "ACT 2 — A healthy release (green)"
# ---------------------------------------------------------------------------
echo "\$ kubectl argo rollouts get rollout canary-demo -n ${NAMESPACE}"
kubectl argo rollouts get rollout canary-demo -n "${NAMESPACE}"
pause 3

echo ""
echo "\$ kubectl argo rollouts set image canary-demo \"*=argoproj/rollouts-demo:green\" -n ${NAMESPACE}"
kubectl argo rollouts set image canary-demo "*=argoproj/rollouts-demo:green" -n "${NAMESPACE}"
pause 2

echo ""
echo "### ACT_MARKER: success_watch ###"
if kubectl argo rollouts status canary-demo -n "${NAMESPACE}" --timeout 300s; then
	echo "✔ Promoted."
else
	echo "✖ Unexpected: this build should have promoted."
fi
pause 2

echo ""
echo "### ACT_MARKER: success_verdict ###"
show_verdict "green canary"
pause 6

echo ""
echo "### ACT_MARKER: success_final ###"
kubectl argo rollouts get rollout canary-demo -n "${NAMESPACE}"
pause 4

# ---------------------------------------------------------------------------
act "failure_start" "ACT 3 — A broken release (bad-red)"
# ---------------------------------------------------------------------------
echo "\$ kubectl argo rollouts set image canary-demo \"*=argoproj/rollouts-demo:bad-red\" -n ${NAMESPACE}"
kubectl argo rollouts set image canary-demo "*=argoproj/rollouts-demo:bad-red" -n "${NAMESPACE}"
pause 2

echo ""
echo "### ACT_MARKER: failure_debate ###"
echo "\$ kubectl logs -n ${AGENT_NS} -l app=kubernetes-agent-orchestrator -f"
echo "(watching the agents debate live - this normally takes under a minute)"
timeout 75 kubectl logs -n "${AGENT_NS}" -l app=kubernetes-agent-orchestrator -f --since=1s 2>/dev/null
pause 2

echo ""
echo "### ACT_MARKER: failure_watch ###"
if kubectl argo rollouts status canary-demo -n "${NAMESPACE}" --timeout 60s; then
	echo "✖ Unexpected: this build should have been aborted."
else
	echo "✔ Aborted, as expected - the regression was caught."
fi
pause 2

echo ""
echo "### ACT_MARKER: failure_verdict ###"
show_verdict "bad-red canary"
pause 8

echo ""
echo "### ACT_MARKER: wrap_up ###"
kubectl argo rollouts get rollout canary-demo -n "${NAMESPACE}"
pause 3

echo ""
echo "Blue stays stable. The AI agents caught the regression from real log"
echo "evidence, out-voted the agents that only looked at resource metrics,"
echo "and Argo Rollouts aborted automatically."
pause 4

echo ""
echo "### ACT_MARKER: end ###"
