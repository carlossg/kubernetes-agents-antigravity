#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT}/scripts/sandbox-backend.sh"
: "${ACTOR_IMAGE:?Set ACTOR_IMAGE to the pushed nonroot application image, pinned by digest}"
RENDER="${ROOT}/scripts/render-substrate.py"
TEMPLATE="$(python3 "${RENDER}" template-name --image "${ACTOR_IMAGE}")"
CLI="${ROOT}/.substrate-tools/bin/kubectl-ate"
[[ -x "${CLI}" ]] || { echo "Run SANDBOX_BACKEND=substrate bash scripts/create-gke-sandbox.sh first." >&2; exit 1; }
ate() { "${CLI}" "$@"; }
if kubectl get crd sandboxes.agents.x-k8s.io >/dev/null 2>&1; then
    [[ -z "$(kubectl get sandbox kubernetes-agent-logs -n argo-rollouts --ignore-not-found -o name)" ]] || {
        echo "Remove the old log Sandbox before switching backends; refusing two active collectors." >&2; exit 1;
    }
fi
kubectl get sandboxconfig gvisor-default >/dev/null
sed "s|kubernetes-agents-antigravity:latest|${ACTOR_IMAGE}|g" \
    "${ROOT}/k8s/log-analyst-substrate.yaml" | kubectl apply -f -
kubectl rollout status deployment/kubernetes-agent-logs -n argo-rollouts --timeout=10m
if ! ate get atespace argo-rollouts >/dev/null 2>&1; then
    ate create atespace argo-rollouts
fi
if ! ate get actor-template "${TEMPLATE}" -a argo-rollouts >/dev/null 2>&1; then
    python3 "${RENDER}" template --image "${ACTOR_IMAGE}" | ate create actor-template -f -
fi
if EXISTING="$(ate get actor kubernetes-agent-logs -a argo-rollouts -o json 2>/dev/null)"; then
    [[ "$(printf '%s' "${EXISTING}" | jq -r '.actorTemplate.name')" == "${TEMPLATE}" ]] || {
        echo "Actor uses a different immutable template; delete the old actor explicitly before upgrading." >&2; exit 1;
    }
else
    ate create actor kubernetes-agent-logs -a argo-rollouts --template "${TEMPLATE}"
fi
if POLICY="$(ate get egress-policy kubernetes-agent-logs -a argo-rollouts -o json 2>/dev/null)"; then
    RULES="$(python3 "${RENDER}" policy --image "${ACTOR_IMAGE}")"
    printf '%s' "${POLICY}" | jq --argjson policy "${RULES}" '.rules = $policy.rules' |
        ate update egress-policy kubernetes-agent-logs -a argo-rollouts -f -
else
    python3 "${RENDER}" policy --image "${ACTOR_IMAGE}" |
        ate create egress-policy kubernetes-agent-logs -a argo-rollouts -f -
fi
echo "Waiting for template initialization and actor readiness (30 bounded RPC attempts)..."
READY=false
for ((attempt=0; attempt<30; attempt++)); do
    if ate resume actor kubernetes-agent-logs -a argo-rollouts; then
        READY=true
        break
    fi
    sleep 10
done
[[ "${READY}" == true ]] || { echo "Substrate actor failed to become ready." >&2; exit 1; }
kubectl rollout status deployment/kubernetes-agent-logs-substrate-bridge -n argo-rollouts --timeout=10m
