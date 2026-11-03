#!/usr/bin/env bash
# Assemble the build context for one agent in a clean directory.
#
#   deploy/stage.sh <agent> <dest>
#
# agents-cli deploys the project root it runs in, and an agent's image needs
# libs/agent_serving, which lives outside agents/<agent>/. The stage is the
# agent directory plus agent_serving/, nothing else:
#   - no .env, so GITHUB_TOKEN cannot reach the runtime;
#   - no tests/ or deploy/, which the image never reads;
#   - outside the repo (the caller passes a mktemp dir), so agents-cli's
#     ignore handling never sees this repo's .gitignore as a parent.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"

resolve_agent "${1:?usage: stage.sh <agent> <dest>}"
DEST="${2:?usage: stage.sh <agent> <dest>}"

# rsync with no timeout flag is local file copy only: no socket to hang on.
rsync -a \
  --exclude '__pycache__/' --exclude '.venv/' --exclude '.adk/' \
  --exclude '.env' --exclude '.env.*' \
  --exclude '/tests/' --exclude '/deploy/' \
  --exclude 'deployment_metadata.json' \
  "${AGENT_DIR}/" "${DEST}/"

rsync -a \
  --exclude '__pycache__/' --exclude '/tests/' --exclude '*.egg-info/' \
  "${ROOT}/libs/agent_serving/" "${DEST}/agent_serving/"

# One list of build output for every agent. .gcloudignore includes .gitignore.
cp "${ROOT}/.gitignore" "${ROOT}/.gcloudignore" "${ROOT}/.dockerignore" "${DEST}/"
