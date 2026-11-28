# Log collector sandbox backends

`LogAnalystAgent` (`agents/logs.py`, CLI role `logs`) is the sandboxed workload.
It deterministically collects bounded stable/canary logs through the trusted
`log-proxy`; it is not a generative model or the external remediation agent.
Neither backend gives it Kubernetes tokens, Google API keys, GitHub credentials,
or model tools. The proxy's protected-namespace checks and log limits are unchanged.

## Default: GKE Agent Sandbox / gVisor

`SANDBOX_BACKEND=gvisor` is the default. `k8s/log-analyst-sandbox.yaml` uses
`agents.x-k8s.io/v1alpha1` **Sandbox**, the `gvisor` RuntimeClass, UID 1000 and
`automountServiceAccountToken: false`. `kubectl apply -k k8s/` still selects this
backend; Kustomize does not interpret `SANDBOX_BACKEND`.

## Substrate 1.0

The optional `SANDBOX_BACKEND=substrate` uses the verified stable release
[v1.0.0](https://github.com/agent-substrate/substrate/releases/tag/v1.0.0),
commit `b34fbee742906494b5eb4f1f36987176bf8770a4` (not a release candidate).
It also runs the actor with gVisor; Substrate is a different lifecycle/network
backend, not an instruction to remove sandboxing.

**It is not a drop-in Sandbox CRD.** Its canonical
[counter manifests](https://github.com/agent-substrate/substrate/tree/v1.0.0/demos/counter)
and [API](https://github.com/agent-substrate/substrate/blob/v1.0.0/api/ateapi/v1/ateapi.proto)
define:

* Kubernetes `ate.dev/v1` **WorkerPool** and cluster-scoped **SandboxConfig**.
* **Atespace**, **ActorTemplate**, **Actor**, and actor **EgressPolicy** in the
  separate Substrate API, managed by the pinned `kubectl-ate` binary.
  Never `kubectl apply` an ActorTemplate.
* Digest-pinned OCI actor images. Substrate takes the process UID from image
  `USER`, not a nonexistent ActorTemplate `runAsUser`. The application Dockerfile
  sets `USER 1000:1000`; capabilities are dropped in the template. Do not substitute
  a root-running image.

The WorkerPool hosts one 500m/1Gi actor. The native template renderer allows only
the trusted log-proxy hostname on TCP 8080, without credential injection. The
actor has no host volumes or pod identity projections. The trusted worker's
Substrate identities remain outside the actor, just as Kubernetes credentials
remain in the trusted log proxy.

The original `kubernetes-agent-logs:8080` Service instead selects a small trusted
bridge Deployment. It establishes TLS to `atenet-router.ate-system.svc:8444`,
verifies the serving CA, presents its projected pod identity, and CONNECTs to
the fixed actor/port using the upstream `ate-target-actor` header. Only `/` and
`/analyze/logs` are exposed; bodies/responses are bounded and transport failures
return 502, never a gVisor fallback. Projected credentials are reloaded on each
connection for rotation. They are **not mounted inside the actor**.
See the pinned upstream [router client contract](https://github.com/agent-substrate/substrate/blob/v1.0.0/cmd/atenet/internal/router/README.md)
and [CONNECT implementation](https://github.com/agent-substrate/substrate/blob/v1.0.0/internal/ateclient/router.go).

## GKE demo setup

Preview offline, with no cluster/cloud calls:

```bash
SANDBOX_BACKEND=substrate bash scripts/deploy-and-test.sh --backend-plan
```

Provision/install explicitly (billable; **not executed by local tests**):

```bash
SANDBOX_BACKEND=substrate bash scripts/create-gke-sandbox.sh
# Export the KUBECONFIG printed by the wizard/wrapper, then:
SANDBOX_BACKEND=substrate bash scripts/deploy-and-test.sh
```

Provisioning delegates to the canonical
[substrate-gke installer](https://github.com/ai-on-gke/substrate-gke/tree/0de82014290a31321a9c9f1f6cde48b1d7f48b66),
pinned to that commit, whose Substrate pin is v1.0.0. Use its prebuilt v1.0.0
images/manifest commit, gVisor only, and skip its counter demo. Retain the
prefilled ingress ID:
`spiffe://cluster.local/ns/argo-rollouts/sa/kubernetes-agent-logs-bridge`.
It authorizes the bridge, not the actor. Credential injection is disabled.
The wrapper verifies the installed API server build, CRD, SandboxConfig and
control-plane rollouts, then downloads and checksum-verifies the v1.0.0 CLI
into the ignored project-local `.substrate-tools/bin/`.

**Requirements differ from the old Autopilot addon script:** the official GKE
installer requires GKE **1.37+ on the control plane and every node pool**,
PodCertificateRequest/ClusterTrustBundle APIs, Workload Identity, suitable
Standard node pools, billing, ADC, GCS snapshot storage and IAM grants.
It provisions these with its interactive wizard; it is not an addon installation
onto the old Autopilot cluster. Consult its pinned README and the
[official GKE installation guide](https://docs.cloud.google.com/kubernetes-engine/ai-ml/install-overview-substrate).
Install the required Go toolchain (the pinned upstream `go.mod` requires 1.27),
git, make, curl, shasum, gcloud, gke-gcloud-auth-plugin, kubectl, jq and Docker
with buildx. Keep workers on non-spot nodes and follow upstream's suspend/upgrade
runbook rather than unplanned node upgrades. No `/dev/kvm` is needed for gVisor.

The deployment script builds/pushes the nonroot application image, resolves its
registry digest, applies the WorkerPool/bridge, creates native API resources,
installs the exact proxy-only egress policy, resumes the actor and waits for
end-to-end bridge readiness. Existing model selection and Google/GitHub credential
placement do not change. Set `GEMINI_API_KEY` as before for the **orchestrator only**.
No collector receives that secret.

Backend switching is deliberately not destructive or automatic. Remove the old
log Sandbox before selecting Substrate. Before returning to gVisor, explicitly
delete the native actor and obsolete bridge/WorkerPool; do not leave two live
collectors. Actor templates are immutable and digest-versioned. For an image
upgrade explicitly delete the old actor before deploying its new template.
Use upstream's teardown/upgrade workflow for Substrate's billable resources,
not the old Autopilot cleanup assumptions.

## Validation and limitations

```bash
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -p test_sandbox_backend.py -v
bash -n scripts/{sandbox-backend,install-substrate,deploy-substrate,create-gke-sandbox,deploy-and-test}.sh
```

Tests cover offline backend selection/rejection, digest validation, native
template/egress rendering, mTLS CONNECT shape/refusal, response limits and the
bridge's fixed HTTP surface. They mock transport; they do not prove live GKE
availability, certificate issuance/rotation, image pulls, IAM, snapshots, or
the actual actor's runtime UID. The scripts provide readiness checks but must
be exercised in an explicitly authorized cluster before claiming live support.
