#!/usr/bin/env bash
# Run this BEFORE starting the asciinema recording. Not part of the recording itself.
#
# Resets canary-demo to a known-good baseline (blue, fully promoted) so the
# recorded demo always starts from the same state, regardless of whatever
# was left over from previous test runs.

set -euo pipefail

NAMESPACE="${NAMESPACE:-default}"

echo "Resetting canary-demo to blue baseline..."
kubectl argo rollouts set image canary-demo "*=argoproj/rollouts-demo:blue" -n "${NAMESPACE}"
kubectl argo rollouts status canary-demo -n "${NAMESPACE}" --timeout 180s

echo ""
echo "Baseline ready. Verify agent pods are healthy:"
kubectl get pods -n argo-rollouts

echo ""
echo "You're ready to record. Start asciinema now:"
echo "  asciinema rec ai-canary-demo.cast"
echo "then run: ./demo-recording.sh"
