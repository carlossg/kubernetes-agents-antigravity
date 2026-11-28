#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT}/scripts/sandbox-backend.sh"

# --plan is offline and never invokes the upstream installer.
if [[ "${1:-}" == "--plan" ]]; then
    printf 'Substrate %s (%s)\nGKE installer %s\n' \
        "${SUBSTRATE_VERSION}" "${SUBSTRATE_COMMIT}" "${SUBSTRATE_GKE_COMMIT}"
    echo 'Requires GKE >=1.37 on control plane AND every node pool; Standard, pod certificates, ClusterTrustBundle, Workload Identity, ADC, billable snapshot storage/IAM.'
    echo 'Installer: make run (prebuilt v1.0.0 images; gvisor only; credential injection disabled)'
    echo 'Ingress client: spiffe://cluster.local/ns/argo-rollouts/sa/kubernetes-agent-logs-bridge'
    echo 'Readiness: ate.dev/v1 WorkerPool CRD, gvisor-default SandboxConfig, API/controller/router/egress/atelet rollouts.'
    exit 0
fi
[[ $# == 0 ]] || { echo "Usage: $0 [--plan]" >&2; exit 2; }
for tool in git go make gcloud kubectl gke-gcloud-auth-plugin curl shasum; do
    command -v "${tool}" >/dev/null || { echo "Missing required tool: ${tool}" >&2; exit 1; }
done
echo "This launches the official interactive GKE installer and creates billable resources."
echo "Use a Standard GKE >=1.37 cluster (including ALL node pools), not the legacy Autopilot sandbox cluster."
echo "Accept the pinned prebuilt ${SUBSTRATE_VERSION} images/commit; select gVisor only, skip upstream demos."
echo "Keep the prefilled ingress SPIFFE ID; changing it prevents this demo from connecting."
TOOLS="${ROOT}/.substrate-tools"
mkdir -p "${TOOLS}"
if [[ ! -d "${TOOLS}/substrate-gke/.git" ]]; then
    git clone --quiet https://github.com/ai-on-gke/substrate-gke.git "${TOOLS}/substrate-gke"
fi
git -C "${TOOLS}/substrate-gke" fetch --quiet origin "${SUBSTRATE_GKE_COMMIT}"
git -C "${TOOLS}/substrate-gke" checkout --quiet --detach "${SUBSTRATE_GKE_COMMIT}"
grep -q "Commit = \"${SUBSTRATE_COMMIT}\"" \
    "${TOOLS}/substrate-gke/installer/internal/snapshot/snapshot.go"
export ATE_INGRESS_ALLOWED_SPIFFE_IDS="spiffe://cluster.local/ns/argo-rollouts/sa/kubernetes-agent-logs-bridge"
export ATE_CREDENTIAL_PROVIDER='{"enabled":false}'
export ATE_INSTALL_SANDBOX_CLASSES=gvisor
make -C "${TOOLS}/substrate-gke" run
# The installer may write a separate kubeconfig. Never accidentally verify or deploy
# into the caller's previous cluster.
echo "Set KUBECONFIG to the wizard's printed kubeconfig before continuing."
read -r -p "Kubeconfig path for the newly installed Substrate cluster: " INSTALLED_KUBECONFIG
[[ -f "${INSTALLED_KUBECONFIG}" ]] || { echo "Kubeconfig not found." >&2; exit 1; }
export KUBECONFIG="${INSTALLED_KUBECONFIG}"
kubectl wait --for=condition=Established crd/workerpools.ate.dev --timeout=3m
kubectl get crd workerpools.ate.dev -o jsonpath='{.spec.versions[?(@.name=="v1")].served}' | grep -qx true
kubectl get sandboxconfig gvisor-default
for deployment in ate-api-server ate-controller atenet-router atenet-egress; do
    kubectl rollout status "deployment/${deployment}" -n ate-system --timeout=10m
done
BUILD="$(kubectl exec -n ate-system deployment/ate-api-server -c ate-api-server -- /ko-app/ateapi --version)"
[[ "${BUILD}" == *"${SUBSTRATE_VERSION}"* && "${BUILD}" == *"${SUBSTRATE_COMMIT:0:7}"* ]] || {
    echo "Expected ${SUBSTRATE_VERSION} (${SUBSTRATE_COMMIT}), got: ${BUILD}" >&2; exit 1;
}
kubectl rollout status daemonset/atelet -n ate-system --timeout=10m
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"
case "${ARCH}" in arm64|aarch64) ARCH=arm64 ;; x86_64) ARCH=amd64 ;; *) echo "Unsupported architecture: ${ARCH}" >&2; exit 1 ;; esac
case "${OS}" in linux|darwin) ;; *) echo "Unsupported OS: ${OS}" >&2; exit 1 ;; esac
mkdir -p "${TOOLS}/bin"
ASSET="kubectl-ate-${OS}-${ARCH}"
URL="https://github.com/agent-substrate/substrate/releases/download/${SUBSTRATE_VERSION}/${ASSET}"
curl -fsSL "${URL}" -o "${TOOLS}/bin/${ASSET}"
curl -fsSL "${URL}.sha256" -o "${TOOLS}/bin/${ASSET}.sha256"
(cd "${TOOLS}/bin" && shasum -a 256 -c "${ASSET}.sha256")
mv "${TOOLS}/bin/${ASSET}" "${TOOLS}/bin/kubectl-ate"
chmod +x "${TOOLS}/bin/kubectl-ate"
echo "Substrate is ready. Export KUBECONFIG=${KUBECONFIG} before running deploy-and-test.sh."
