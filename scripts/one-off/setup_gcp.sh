#!/usr/bin/env bash
# Enable the APIs e2e-agent uses, and optionally store a GitHub token secret.
set -euo pipefail

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:?set GOOGLE_CLOUD_PROJECT}"
SECRET_NAME="${SECRET:-ci-agent-github-token}"

echo "Using GCP Project: ${PROJECT_ID}"

echo "Enabling GCP APIs..."
gcloud services enable \
  aiplatform.googleapis.com \
  cloudresourcemanager.googleapis.com \
  cloudbuild.googleapis.com \
  secretmanager.googleapis.com \
  --project "${PROJECT_ID}"

if [[ -n "${PAT:-}" ]]; then
  echo "Creating Secret Manager secret ${SECRET_NAME}..."
  printf "%s" "${PAT}" | gcloud secrets create "${SECRET_NAME}" --data-file=- --project "${PROJECT_ID}"
fi

echo "Granting Secret Manager access to Agent Runtime service account..."
PROJECT_NUM="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
gcloud secrets add-iam-policy-binding "${SECRET_NAME}" \
  --member="serviceAccount:service-${PROJECT_NUM}@gcp-sa-aiplatform-re.iam.gserviceaccount.com" \
  --role=roles/secretmanager.secretAccessor \
  --project "${PROJECT_ID}"

echo "Setup completed successfully."
