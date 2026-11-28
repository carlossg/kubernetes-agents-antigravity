#!/usr/bin/env bash
# Shared by the provisioning and deployment entrypoints; validate before any mutation.
SANDBOX_BACKEND="${SANDBOX_BACKEND-gvisor}"
case "${SANDBOX_BACKEND}" in
    gvisor|substrate) ;;
    *) echo "Invalid SANDBOX_BACKEND '${SANDBOX_BACKEND}'; expected gvisor or substrate." >&2; exit 2 ;;
esac
SUBSTRATE_VERSION="v1.0.0"
SUBSTRATE_COMMIT="b34fbee742906494b5eb4f1f36987176bf8770a4"
SUBSTRATE_GKE_COMMIT="0de82014290a31321a9c9f1f6cde48b1d7f48b66"
