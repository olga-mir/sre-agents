# sre-coordinator image. Build context is the staged directory that
# deploy/stage.sh assembles: this directory plus agent_serving/.
# Deliberately lighter than the e2e-agent image: no tree-sitter, GitHub
# client, or GCS completion upload.
FROM python:3.12-slim

WORKDIR /code

COPY agent_serving ./agent_serving
COPY pyproject.toml requirements.txt ./
COPY sre_coordinator ./sre_coordinator

# pip's default timeout is 15s. 60s is the bound for each install step.
RUN pip install --no-cache-dir --timeout 60 ./agent_serving \
 && pip install --no-cache-dir --timeout 60 . \
 && pip install --no-cache-dir --timeout 60 -r requirements.txt \
 && pip install --no-cache-dir --timeout 60 "uvicorn>=0.34"

# The module that exports the ADK `app`. agent_serving has no default.
ENV AGENT_MODULE=sre_coordinator.agent
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "exec uvicorn agent_serving.fast_api_app:app --host 0.0.0.0 --port ${PORT:-8080}"]
