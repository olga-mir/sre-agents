# Gemma 4 26B-A4B on Agent Platform

Self-hosted Model Garden deploy of
[`google/gemma4@gemma-4-26b-a4b-it`](https://adk.dev/agents/models/agent-platform/).
Nothing here deploys until you pass `CONFIRM=yes`. e2e-agent stays on `Gemini`
unless you point it at an endpoint that has passed `smoke.sh`.

## What the catalog says

`gcloud ai model-garden models list` shows two relevant ids:

| Catalog id | What it is |
|---|---|
| `google/gemma4@gemma-4-26b-a4b-it` | Instruction-tuned Gemma 4, 26B total / 4B active. This folder deploys this. |
| `publishers/google/models/gemma-4-26b-a4b-it-maas` | Managed Model-as-a-Service. Pay per token, no GPU to undeploy. Out of scope. |

`list-deployment-config` for the self-hosted id offers several machines. The
default here is the smallest verified row:

| Machine | Accelerator | Container | List price |
|---|---|---|---|
| `g4-standard-48` | 1x `NVIDIA_RTX_PRO_6000` | `pytorch-vllm-serve:gemma4` | **$5.1751 / hour** in `us-central1` |

That hourly figure is the Model Garden on-demand list price for that machine
in `us-central1`. A full day left running is about $124. Discounts,
reservations, and other regions change the bill. The GPU bills from the moment
the replica is up until `undeploy.sh` finishes, including when nobody is calling it.

That same catalog row already starts vLLM with
`--enable-auto-tool-choice`, `--tool-call-parser=gemma4`, and
`--reasoning-parser=gemma4`. `deploy.sh` pins the image and the machine so
gcloud selects that row, and it does not pass `--container-args`.

Other verified machines (`g4-standard-96`, `a3-highgpu-1g`, `a3-highgpu-2g`)
and an SGLang image exist. Override `MACHINE_TYPE` only after checking
`list-deployment-config` again. `SPOT=1` schedules a Spot VM; this README does
not quote a Spot price, and preemption drops the replica.

## Gemma uses a different adapter than Gemini

`src/e2e_agent/agent/nodes.py` builds a `Gemini` adapter plus `BuiltInPlanner`
(`ThinkingConfig`). A Model Garden Gemma 4 endpoint is a vLLM server. ADK's
Gemma page routes that through `LiteLlm` and an OpenAI-compatible
`/chat/completions` URL, with thinking turned on per request via
`chat_template_kwargs.enable_thinking`. Passing the endpoint resource name to
`Gemini` is the path for generateContent models, which this container is not.

Gemma 4 does support tools and structured output, which the investigator needs
(`output_schema=CodeFinding`, a tool loop). Gemma 3 does not, which is why
this is the 26B-A4B instruction model and not `gemma-3-27b-it`. The smoke
script's second call is the gate: it should return `tool_calls`, not a prose
answer. Wire `LiteLlm` into the two `LlmAgent`s only after that succeeds.
Drop `BuiltInPlanner` on that path; thinking is the vLLM `enable_thinking` flag.

Scripts read `GOOGLE_CLOUD_PROJECT`. `us-central1` needs RTX Pro 6000 quota.

## Commands

```bash
cp model-garden/endpoint.env.example model-garden/endpoint.env

# prints the cost and exits
bash model-garden/deploy.sh

# actually deploys (accepts the Gemma EULA, async)
CONFIRM=yes bash model-garden/deploy.sh

# after the operation succeeds, set ENDPOINT_ID in endpoint.env
bash model-garden/smoke.sh
bash model-garden/smoke.sh "Explain this failed run in one sentence."

# stops the GPU bill
CONFIRM=delete bash model-garden/undeploy.sh
```

`deploy.sh` prints an operation id. Poll it with
`gcloud ai operations describe OPERATION_ID --region=us-central1 --project=$GOOGLE_CLOUD_PROJECT`.
Then `gcloud ai endpoints list --region=us-central1 --project=$GOOGLE_CLOUD_PROJECT`
and put the numeric id in `endpoint.env`.
