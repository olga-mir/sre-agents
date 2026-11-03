# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# e2e-agent image. Build context is the staged directory that
# deploy/stage.sh assembles: this directory plus agent_serving/.
# python:3.12-slim matches the agents-cli ADK template and sits inside
# requires-python (>=3.11,<3.14). uv.lock is gitignored, so this is pip.
FROM python:3.12-slim

WORKDIR /code

COPY agent_serving ./agent_serving
COPY pyproject.toml requirements.txt ./
COPY e2e_agent ./e2e_agent

# pip's default timeout is 15s. 60s is the bound for each install step.
# agent_serving goes first so `.` finds it installed instead of on PyPI.
# requirements.txt pins the runtime set, including google-adk[otel-gcp] and
# the GenAI completion-upload stack. uvicorn is a dependency of agent-serving;
# it is also named here so the CMD does not depend on that staying true.
RUN pip install --no-cache-dir --timeout 60 ./agent_serving \
 && pip install --no-cache-dir --timeout 60 . \
 && pip install --no-cache-dir --timeout 60 -r requirements.txt \
 && pip install --no-cache-dir --timeout 60 "uvicorn>=0.34"

# The module that exports the ADK `app`. agent_serving has no default.
ENV AGENT_MODULE=e2e_agent.agent.agent
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "exec uvicorn agent_serving.fast_api_app:app --host 0.0.0.0 --port ${PORT:-8080}"]
