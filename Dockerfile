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

# agents-cli agent_runtime builds this image. python:3.12-slim matches the
# agents-cli ADK template and sits inside requires-python (>=3.11,<3.14).
# uv.lock is gitignored, so this is pip rather than `uv sync --frozen`.
FROM python:3.12-slim

WORKDIR /code

COPY pyproject.toml ./
COPY src ./src

# pip's default timeout is 15s. 60s is the bound for each install step.
# The second install pins the runtime set in src/requirements.txt, including
# google-adk[a2a,otel-gcp] and the GenAI completion-upload stack.
# uvicorn is a dependency of google-adk; install it explicitly so the CMD
# does not depend on that remaining true.
RUN pip install --no-cache-dir --timeout 60 . \
 && pip install --no-cache-dir --timeout 60 -r src/requirements.txt \
 && pip install --no-cache-dir --timeout 60 "uvicorn>=0.34"

COPY app ./app
COPY agents ./agents

ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "exec uvicorn app.fast_api_app:app --host 0.0.0.0 --port ${PORT:-8080}"]
