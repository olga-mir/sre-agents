#!/usr/bin/env bash
# Link the agent source repository so task deploy can build e2e-agent
# from GitHub. Idempotent: an existing connection or link is left as it is.
#
# This GitHub App is Google's Developer Connect app, used only to fetch
# source at deploy time. It is not the GitHub App whose PEM lives in
# Secret Manager for e2e-agent's runtime API calls.
#
# The connection needs an OAuth token that Developer Connect wrote into
# Secret Manager, plus that app's installation id. The console flow
# (Developer Connect → GitHub) produces both. Then:
#
#   DC_CLONE_URI=https://github.com/OWNER/sre-agents.git \
#   DC_REPO_LINK=sre-agents \
#   DC_OAUTH_SECRET_VERSION=projects/PROJECT/secrets/NAME/versions/1 \
#   DC_INSTALLATION_ID=123456 \
#   deploy/developer_connect.sh
#
# If the connection already exists, the OAuth variables can be omitted
# and this script only creates the git repository link.
set -euo pipefail

PROJECT="${PROJECT:-${GOOGLE_CLOUD_PROJECT:?set PROJECT or GOOGLE_CLOUD_PROJECT}}"
LOCATION="${DC_LOCATION:-${LOCATION:-us-central1}}"
CONNECTION="${DC_CONNECTION:-gh-connect}"
LINK="${DC_REPO_LINK:-}"
CLONE_URI="${DC_CLONE_URI:-}"

command -v gcloud >/dev/null 2>&1 || {
  echo "gcloud is required" >&2
  exit 1
}

gcloud services enable \
  developerconnect.googleapis.com \
  secretmanager.googleapis.com \
  --project="${PROJECT}"

connection_exists() {
  gcloud developer-connect connections describe "${CONNECTION}" \
    --location="${LOCATION}" \
    --project="${PROJECT}" >/dev/null 2>&1
}

link_exists() {
  gcloud developer-connect connections git-repository-links describe "${LINK}" \
    --connection="${CONNECTION}" \
    --location="${LOCATION}" \
    --project="${PROJECT}" >/dev/null 2>&1
}

dc_service_agent() {
  local out email number
  if ! out="$(gcloud beta services identity create \
    --service=developerconnect.googleapis.com \
    --project="${PROJECT}" 2>&1)"; then
    echo "${out}" >&2
    exit 1
  fi
  email="$(printf '%s\n' "${out}" | grep -oE '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.iam\.gserviceaccount\.com' | head -1 || true)"
  if [[ -z "${email}" ]]; then
    number="$(gcloud projects describe "${PROJECT}" --format='value(projectNumber)')"
    email="service-${number}@gcp-sa-devconnect.iam.gserviceaccount.com"
    echo "Using Developer Connect service agent ${email}" >&2
  fi
  printf '%s\n' "${email}"
}

ensure_connection() {
  if connection_exists; then
    echo "Connection ${CONNECTION} exists"
    return 0
  fi
  if [[ -z "${DC_OAUTH_SECRET_VERSION:-}" || -z "${DC_INSTALLATION_ID:-}" ]]; then
    echo "Connection ${CONNECTION} does not exist in ${LOCATION}." >&2
    echo "Create it in the console (Developer Connect → GitHub), or rerun with:" >&2
    echo "  DC_OAUTH_SECRET_VERSION=projects/${PROJECT}/secrets/NAME/versions/1" >&2
    echo "  DC_INSTALLATION_ID=<Developer Connect GitHub App installation id>" >&2
    echo "https://console.cloud.google.com/developer-connect/connections?project=${PROJECT}" >&2
    exit 1
  fi
  if [[ ! "${DC_OAUTH_SECRET_VERSION}" =~ ^projects/.*/secrets/.*/versions/.+ ]]; then
    echo "DC_OAUTH_SECRET_VERSION must be projects/PROJECT/secrets/NAME/versions/VERSION" >&2
    exit 1
  fi
  if [[ ! "${DC_INSTALLATION_ID}" =~ ^[0-9]+$ ]]; then
    echo "DC_INSTALLATION_ID must be digits (the Developer Connect GitHub App installation)." >&2
    exit 1
  fi

  local secret_id agent
  secret_id="$(printf '%s\n' "${DC_OAUTH_SECRET_VERSION}" | sed -n 's|.*/secrets/\([^/]*\)/versions/.*|\1|p')"
  agent="$(dc_service_agent)"
  # Developer Connect reads this secret as its service agent when the
  # connection is created. secretAccessor on this secret is enough;
  # the agent runtime does not use this secret.
  gcloud secrets add-iam-policy-binding "${secret_id}" \
    --project="${PROJECT}" \
    --member="serviceAccount:${agent}" \
    --role="roles/secretmanager.secretAccessor" \
    --condition=None

  echo "Creating connection ${CONNECTION}"
  gcloud developer-connect connections create "${CONNECTION}" \
    --project="${PROJECT}" \
    --location="${LOCATION}" \
    --github-config-app=developer-connect \
    --github-config-app-installation-id="${DC_INSTALLATION_ID}" \
    --github-config-authorizer-credential-oauth-token-secret-version="${DC_OAUTH_SECRET_VERSION}"
}

ensure_link() {
  if [[ -z "${LINK}" || -z "${CLONE_URI}" ]]; then
    echo "Set DC_REPO_LINK (link id) and DC_CLONE_URI (https://github.com/OWNER/REPO.git)." >&2
    exit 1
  fi
  if link_exists; then
    echo "Git repository link ${LINK} exists"
    return 0
  fi
  echo "Creating git repository link ${LINK}"
  gcloud developer-connect connections git-repository-links create "${LINK}" \
    --project="${PROJECT}" \
    --connection="${CONNECTION}" \
    --location="${LOCATION}" \
    --clone-uri="${CLONE_URI}"
}

ensure_connection
ensure_link

echo "DC_REPO_LINK=${LINK}"
echo "DC_CONNECTION=${CONNECTION}"
echo "DC_LOCATION=${LOCATION}"
echo "projects/${PROJECT}/locations/${LOCATION}/connections/${CONNECTION}/gitRepositoryLinks/${LINK}"
