#!/usr/bin/env bash

# ==============================================================================
# GKE Agent Sandbox Cluster Provisioner
# Configures a GKE Autopilot cluster with the Agent Sandbox addon enabled.
# Requirement: GKE version >= 1.35.2-gke.1269000
# ==============================================================================

set -euo pipefail

# Configurable defaults (can be overridden by environment variables)
CLUSTER_NAME="${CLUSTER_NAME:-autopilot-cluster-1}"
LOCATION="${LOCATION:-us-central1}"
CLUSTER_VERSION="${CLUSTER_VERSION:-latest}"

echo "====================================================================="
echo " Starting GKE Agent Sandbox Cluster Creation (Autopilot)"
echo " Cluster Name : ${CLUSTER_NAME}"
echo " Location     : ${LOCATION}"
echo " Version      : ${CLUSTER_VERSION}"
echo "====================================================================="

# 1. Check prerequisites
if ! command -v gcloud &> /dev/null; then
    echo "❌ Error: gcloud CLI is not installed or not in PATH."
    exit 1
fi

PROJECT_ID=$(gcloud config get-value project 2>/dev/null || true)
if [ -z "${PROJECT_ID}" ]; then
    echo "❌ Error: No default Google Cloud project set."
    echo "Please set your project first: gcloud config set project <PROJECT_ID>"
    exit 1
fi
echo "✓ GCP Project detected: ${PROJECT_ID}"

# 2. Enable Container Service API
echo "⚙️ Enabling GKE API (container.googleapis.com)..."
gcloud services enable container.googleapis.com

# 3. Create GKE Autopilot Cluster
echo "🚀 Deploying GKE Autopilot Cluster with Agent Sandbox addon..."
gcloud beta container clusters create-auto "${CLUSTER_NAME}" \
    --location="${LOCATION}" \
    --cluster-version="${CLUSTER_VERSION}" \
    --enable-agent-sandbox

# 4. Fetch Credentials
echo "🔑 Configuring kubeconfig..."
gcloud container clusters get-credentials "${CLUSTER_NAME}" --location="${LOCATION}"

# 5. Verification
echo "🔍 Verifying Sandbox Addon activation..."
STATUS=$(gcloud beta container clusters describe "${CLUSTER_NAME}" \
    --location="${LOCATION}" \
    --format="value(addonsConfig.agentSandboxConfig.enabled)" 2>/dev/null || echo "Unknown")

if [ "${STATUS}" = "True" ]; then
    echo "✨ Success! GKE Agent Sandbox is enabled on cluster '${CLUSTER_NAME}'."
else
    echo "⚠️ Warning: Expected addon status 'True', but got '${STATUS}'."
    echo "Please inspect via GCP console or run: gcloud beta container clusters describe ${CLUSTER_NAME} --location=${LOCATION}"
fi
