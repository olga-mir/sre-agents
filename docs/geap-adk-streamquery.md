# GEAP invoke path — ADK `:streamQuery`, not A2A on Agent Runtime

**Status:** Shipped as the deploy and Gemini Enterprise registration path.
`task deploy` builds a container with `agents-cli`. The process serves
`AdkApp` on the reasoning-engine routes. `task publish` uses
`--registration-type adk`.

**Date:** 2026-09-14.

---

## 1. What we wanted

Gemini Enterprise as a **client** in front of `e2e-agent`, and Agent Registry
as the place that **governs** that agent. The pitch mixed three products into
one loop: develop in ADK, catalog in Registry, talk A2A so GE (or another
agent) is just a peer.

That loop is not how Agent Runtime actually works for an ADK graph.

## 2. The three products

| Product | What it actually is |
|---|---|
| **Agent Runtime** | Managed ADK process. Native API is `:query` / `:streamQuery` / sessions on a reasoning engine. Playground and traces are this surface. |
| **Agent Registry** | Catalog of runtime agents, MCP servers, endpoints, and bindings. Agent Runtime engines are **auto-registered**. Listing here does not mean the engine speaks A2A. |
| **A2A** | HTTP protocol (Agent Card + `message/send`). Default way to reach something that has **no** reasoning-engine API (Cloud Run, GKE). |

Gemini Enterprise is a chat app that can invoke a worker. For an AdkApp on
Agent Runtime, the supported worker mode is **ADK** (`:streamQuery` on the
engine resource). A2A-GE is for a worker that already has a card URL.

The container is a FastAPI process so Agent Engine can proxy `:streamQuery`.
It is not a second trigger API. Graph entry is still TriggerEvent JSON.
Local HITL is `run_local.py`. Slack REST resume is still unwritten.

## 3. What we tried (and what broke)

1. **`to_a2a(root_agent)` on the ADK `App`.** That builds a Starlette app for
   uvicorn. Importing it on an Agent Runtime deploy only produced experimental
   `A2aAgentExecutor` warnings. No Agent Card, nothing for Registry to index
   as A2A.

2. **`vertexai.agent_engines.templates.a2a.A2aAgent` as `entrypoint_object`.**
   This *is* the Agent Runtime A2A template (`on_message_send`, card under
   `/a2a`). `A2aAgent.agent_framework` on the Python class is `"a2a"`.

3. **Deploy config `agent_framework: "a2a"`.** The Agent Runtime API rejects
   it. Allowed literals: `google-adk`, `langchain`, `langgraph`, `ag2`,
   `llama-index`, `custom`. Pydantic error on `client.runtimes.update`.

4. **Same A2A object with `agent_framework: "custom"`.** Deploy succeeded.
   Console lost the ADK framework: traces, playground, and related features
   off. `agents-cli publish gemini-enterprise --registration-type a2a` then
   failed: *Agent card URL is required for A2A registration* — source-based
   deploy does not write `deployment_metadata.json`, so the CLI cannot invent
   the card URL.

## 4. What we do instead

Stay on the ADK-native path and stop treating A2A as a prerequisite for GE
or Registry.

1. `task deploy` runs `agents-cli deploy`. The image's process is
   `uvicorn app.fast_api_app:app`, which serves `AdkApp` on
   `/api/reasoning_engine` and `/api/stream_reasoning_engine`. `agents-cli`
   records `agent_framework: google-adk` and `class_methods` from
   `AdkApp.register_operations()` (sessions + `stream_query`).
2. Treat Agent Registry as the catalog of that engine. `e2e-agent` showing
   up and **not** being A2A is correct.
3. Register Gemini Enterprise with `--registration-type adk`. GE calls
   `:streamQuery`. No card URL.
4. Keep `mcp_entrypoint.py` for Cursor / Claude on a laptop. MCP is a
   different client protocol; Registry MCP servers are unrelated.

```bash
task deploy
task publish   # lists apps when GE_APP is unset
task publish GE_APP=projects/YOUR_PROJECT/locations/global/collections/default_collection/engines/APP_ID
```

The build is the working tree `agents-cli` uploads. Publish only after the
engine is ADK again — ADK registration against a custom/A2A revision would
point `:streamQuery` at methods that are not there.

Graph START still validates `TriggerEvent` JSON. Playground, GE, MCP, and
`deploy/invoke_agent.py` all send that payload. Chat-to-JSON parsing
(`trigger_parse.py`) was written for the A2A executor and is **not** on this
path.

HITL from Gemini Enterprise is still unwired (session resume across GE
turns). Local pause/resume works.

## 5. If we actually need A2A later

Do it as a **second** deploy: `to_a2a` (or Cloud Run serving a card URL),
then `agents-cli publish … --registration-type a2a --agent-card-url …`.
Keep the AdkApp engine if we still want the console. Do not bind
`A2aAgent` as this repo's Agent Runtime `entrypoint_object`.

`src/e2e_agent/agent/a2a_app.py` remains as a constructible wrapper and
tests only.

## 6. Why Cloud Run A2A is not “ADK prefers Cloud Run”

Cloud Run has no `:streamQuery` reasoning engine. A2A (or any HTTP) is the
only way GE can call it. Agent Runtime has a native ADK invoke API, so GE
does not need A2A there. The CLI even warns against A2A registration for
an ADK agent on Agent Runtime for that reason.
