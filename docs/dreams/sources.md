# Dream sources

Standing watchlist for the dreaming skill (`.agents/skills/dreaming/SKILL.md`).
Add a source by editing this file. A dream does not add one on its own.

Each entry is fetched from scratch. `keep` is the filter; everything else on
that page is out of the dream.

## adk-python

- url: https://github.com/google/adk-python/releases
- index: https://adk.dev/release-notes/
- kind: release
- keep: highlights and breaking changes for the Python SDK. This repo depends on `google-adk`. The index only points at the language repos; Python is the one to read. Mention another language only when the note changes how this demo should explain ADK.

## agentplatform

- url: https://github.com/googleapis/python-aiplatform/releases
- package: `google-cloud-agentplatform` (also imported as `agentplatform` from `google-cloud-aiplatform`, which this repo pins)
- kind: release
- keep: items about `agentplatform`, Agent Runtime, Agent Engine, runtimes, memory banks, sandboxes, sessions, or deploying ADK. Skip classic Vertex ML and Model Garden unless the note changes `model-garden/` or `deploy/` in this repo.

## geap-release-notes

- url: https://docs.cloud.google.com/gemini-enterprise-agent-platform/release-notes
- kind: release-notes
- keep: Agent Runtime, ADK, Agent Identity, sessions, memory, registry, gateway, BigQuery, and deployment. Read from the top and stop at the first dated section older than the window.

## developers-blog

- url: https://developers.googleblog.com/
- kind: blog
- keep: posts about the Agent Development Kit, Gemini Enterprise Agent Platform, Agent Runtime, Agent Identity, or agents-cli. Skip the rest of the blog.

## discuss-googler

- url: https://discuss.google.dev/tag/googler-article/54
- kind: forum
- keep: Googler articles whose topic is ADK or Gemini Enterprise Agent Platform, including Agent Runtime, Agent Identity, Agent Registry, Agent Gateway, and agents-cli. Skip other Google products even when they share this tag.

## discuss-agent-platform

- url: https://discuss.google.dev/tag/agent-platform/226
- kind: forum
- keep: product changes, design notes, and release discussion. Skip pure "how do I" threads unless the answer shows a behavior this repo currently gets wrong.
