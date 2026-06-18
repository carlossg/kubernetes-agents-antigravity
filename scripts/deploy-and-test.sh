#!/usr/bin/env bash

# ==============================================================================
# Build, Deploy, and Test Agent Team on GKE Autopilot with Agent Sandbox
# ==============================================================================

set -euo pipefail

# Auto-detect Colima docker socket if running on macOS
if [ -S "${HOME}/.colima/default/docker.sock" ]; then
    export DOCKER_HOST="unix://${HOME}/.colima/default/docker.sock"
    echo "✓ Detected Colima Docker socket. Configured DOCKER_HOST."
fi

PROJECT_ID=$(gcloud config get-value project 2>/dev/null || true)
if [ -z "${PROJECT_ID}" ]; then
    echo "❌ Error: No default Google Cloud project set."
    exit 1
fi

REGISTRY="us-central1-docker.pkg.dev"
IMAGE_URL="${REGISTRY}/${PROJECT_ID}/github/kubernetes-agents-antigravity:latest"

echo "====================================================================="
echo " Deploying Agent Team to GKE Cluster"
echo " Image target: ${IMAGE_URL}"
echo "====================================================================="

# 1. Check GEMINI_API_KEY
if [ -z "${GEMINI_API_KEY:-}" ]; then
    echo "❌ Error: GEMINI_API_KEY environment variable is not set."
    echo "Please export it before running this script: export GEMINI_API_KEY=..."
    exit 1
fi

# 2. Build Docker Image
echo "📦 Building docker image..."
docker build -t "${IMAGE_URL}" .

# 3. Configure Docker credential helper & Push
echo "🔑 Logging into Google Artifact Registry..."
gcloud auth configure-docker "${REGISTRY}" --quiet

echo "🚀 Pushing docker image..."
docker push "${IMAGE_URL}"

# 4. Prepare Namespace & Secret
echo "⚙️ Creating namespace 'argo-rollouts'..."
kubectl create namespace argo-rollouts --dry-run=client -o yaml | kubectl apply -f -

echo "🔑 Creating secret 'argo-rollouts-secret'..."
kubectl create secret generic argo-rollouts-secret \
    --namespace=argo-rollouts \
    --from-literal=GEMINI_API_KEY="${GEMINI_API_KEY}" \
    --dry-run=client -o yaml | kubectl apply -f -

# 5. Deploy manifests using the GCR registry image
echo "📄 Deploying agent workloads..."
cat k8s/agents.yaml | sed "s|kubernetes-agents-antigravity:latest|${IMAGE_URL}|g" | kubectl apply -f -
cat k8s/log-analyst-sandbox.yaml | sed "s|kubernetes-agents-antigravity:latest|${IMAGE_URL}|g" | kubectl apply -f -

# 6. Wait for deployments
echo "⏳ Waiting for specialist deployments to be ready..."
kubectl rollout status deployment/kubernetes-agent-orchestrator -n argo-rollouts --timeout=3m
kubectl rollout status deployment/kubernetes-agent-metrics -n argo-rollouts --timeout=3m
kubectl rollout status deployment/kubernetes-agent-events -n argo-rollouts --timeout=3m

echo "⏳ Waiting for log analyst sandbox to be ready..."
# Give GKE a moment to schedule sandbox pods
sleep 15
kubectl wait --for=condition=Ready pod -l app=kubernetes-agent-logs -n argo-rollouts --timeout=5m

# 7. Run Test Analysis Execution
echo "🧪 Running end-to-end orchestration analysis test..."
kubectl exec -n argo-rollouts deployment/kubernetes-agent-orchestrator -- python3 -c "
import urllib.request, json
req_data = {
  'userId': 'argo-rollouts',
  'prompt': 'Analyze canary deployment for rollout canary-demo.',
  'context': {
    'namespace': 'rollouts-test-system',
    'rolloutName': 'canary-demo',
    'stableSelector': 'role=stable',
    'canarySelector': 'role=canary'
  }
}
req = urllib.request.Request(
    'http://localhost:8080/a2a/analyze',
    data=json.dumps(req_data).encode(),
    headers={'Content-Type': 'application/json'}
)
print('\n Consensus Response Received:\n')
print(json.dumps(json.loads(urllib.request.urlopen(req).read().decode()), indent=2))
"
