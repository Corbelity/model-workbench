# Corbelity Model Workbench

A browser workbench for running one prompt across text, image and speech models and
comparing what comes back: the output, the latency, the tokens and the cost.

It is a thin UI over [`corbelity-model-client`](https://github.com/Corbelity/model-client),
so the same screen drives Anthropic, OpenRouter, a local Ollama box, Ollama Cloud and
HuggingFace, and every call is logged and traced identically whichever provider answered.

## What it does

- **Pick any catalog model** and switch between them mid-conversation. The conversation
  lives in your browser and is sent with each request, so the server stays stateless and
  the same context can be handed to a different model.
- **Text, image and sound output.** OpenAI and HuggingFace generate all three; the other
  providers are text only.
- **Tune** temperature, top-p and max tokens. For Anthropic models that reject sampling
  parameters, the client drops `temperature` and `top_p` for you instead of returning a 400
  (a catalog entry's `supports_sampling` flag decides; see below).
- **Edit the conversation.** Every prior turn is a card you can edit, delete, or copy out as
  JSON.
- **Attach images** to a text prompt (file or URL) for vision-capable models.
- **See what a call cost:** latency, prompt/completion tokens as the provider reported them,
  and an estimated cost from the catalog's per-1k rates.
- **Inspect the request** in the payload drawer, including where each credential came from.
- **Review recorded traces** through `/api/traces` (see [Tracing](#tracing)).

## Quickstart

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Corbelity/model-workbench
cd model-workbench
uv sync
cp .env.example .env        # PowerShell: Copy-Item .env.example .env
# edit .env and add at least one provider key
uv run corbelity-workbench
```

Then open <http://127.0.0.1:8000>.

```text
corbelity-workbench [--host HOST] [--port PORT] [--reload]
```

`python -m corbelity.workbench` does the same thing.

> `corbelity-model-client` is resolved from its GitHub repository until it is published to
> PyPI, which is why `uv` is the supported installer for now. If you are developing both
> repositories side by side, point at your checkout instead; see the comment in
> [`pyproject.toml`](pyproject.toml).

## Configuration

The workbench reads a `.env` file from the directory you launch it in. The provider
credentials are the client library's; the full list, with aliases and endpoint overrides,
is in the [model-client README](https://github.com/Corbelity/model-client#providers).

| Variable | Used for |
|---|---|
| `ANTHROPIC_API_KEY` | `anthropic` |
| `OPENAI_API_KEY` | `openai` — text, image and sound |
| `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | `gemini` |
| `OPENROUTER_API_KEY` | `openrouter` |
| `HF_TOKEN` (or `HUGGINGFACE_HUB_KEY`) | `huggingface` — text, image and sound |
| `OLLAMA_API_KEY` | `ollama` (Ollama Cloud) |
| `LOCAL_OLLAMA_URL` | `ollama-local` — the host of your own Ollama box |
| `CORBELITY_MODEL_CATALOG` | path to your own model catalog (see below) |
| `CORBELITY_SERVICES` | comma-separated allow-list of services to offer (see below) |
| `TRACE_ENABLED`, `TRACE_FILE` | record every call (see [Tracing](#tracing)) |
| `WORKBENCH_DEV` | stop the browser caching the UI while you edit it (see below) |
| `LOGGING_LEVEL` | `DEBUG`, `INFO` (default), `WARNING`… |

A key typed into the sidebar overrides the environment for that request. The badge beside
each field lights up when the server already has that key, so you can tell which one a
call will use; the request payload drawer shows the same thing per provider.

## Adding a model

The model list is the client library's catalog. It ships a starting set; yours merges over
it by model id, so a file only has to carry what it adds or changes:

```bash
CORBELITY_MODEL_CATALOG=./my-models.json
```

```json
[
  {
    "id": "anthropic/claude-sonnet-4.5",
    "name": "Claude Sonnet 4.5",
    "service": "openrouter",
    "modality": "text",
    "description": "Anthropic model routed through OpenRouter.",
    "accepts_images": true,
    "cost_per_1k_input": 0.003,
    "cost_per_1k_output": 0.015
  }
]
```

`service` is one of `anthropic`, `openai`, `gemini`, `openrouter`, `ollama-local`, `ollama`
or `huggingface`. `modality` is `text`, `image` or `sound`. `accepts_images` gates the
attach controls for that model. `supports_sampling: false` tells the Anthropic client to
omit `temperature` and `top_p` for a model that rejects them. A model with no
`cost_per_1k_*` reports $0 rather than an invented number.

To show only some providers, name them in `CORBELITY_SERVICES`:

```bash
CORBELITY_SERVICES=openai,ollama-local
```

That narrows the dropdown and nothing else. It does not disable a service: a request
naming a filtered-out model is still routed and still runs. Leaving it unset offers
everything.

The file is re-read on every request, so edit it and refresh the page — no restart. A
catalog that fails to parse is logged as an error and the built-in list is used instead.

## Tracing

Set `TRACE_ENABLED=1` and every call is appended to `logs/traces.jsonl` (override with
`TRACE_FILE`): the full prompts, conversation history, response, latency and token usage.
Generated images and audio, and attached input images, are written beside it under
`logs/artifacts/` and referenced by name so the trace stays readable.

```text
GET /api/traces              every run, newest first, with totals and error counts
GET /api/traces/{run_id}     that run's calls, in order and untruncated
```

Reading works with tracing off; `TRACE_ENABLED` only governs *recording*. A trace file
whose last line was cut short by a killed process still reads, with the skipped line
reported.

**Traces contain full prompt and response text.** `logs/` is gitignored; keep it that way.

## Working on the UI

```bash
uv run corbelity-workbench --reload
```

`--reload` restarts the server when Python changes and sends `Cache-Control: no-store`, so
the browser stops caching `style.css` and `app.js`. That second part matters more than it
sounds: the page itself is served uncached while the static files are not, so without it a
frontend edit can leave the browser drawing new markup with the old stylesheet — which
looks like a broken layout rather than a stale file. Running uvicorn directly instead? Set
`WORKBENCH_DEV=1` yourself.

The UI is plain HTML, CSS and JavaScript loaded directly from
`src/corbelity/workbench/static/` — no build step and no framework. Every colour, typeface
and radius comes from the `:root` block in `style.css`; a raw hex value further down the
file is a bug.

## Security

The workbench is built to run on your own machine, and it behaves accordingly:

- **It has no authentication** and binds to `127.0.0.1` by default. `--host` with anything
  else prints a warning, because anyone who can then reach it can spend your API keys, read
  every recorded trace, and make the server fetch arbitrary URLs.
- **The server fetches image URLs itself** so that every provider treats them the same way.
  That is an outbound request to a URL you supplied, bounded by scheme, timeout, redirect
  count and size. It is one more reason not to expose it.
- **Keys typed into the UI are stored in your browser's local storage in plain text.**
  Prefer `.env` for anything long-lived. Keys from `.env` are never sent to the browser;
  `/api/config` reports only whether each one is set.
- **Model output is untrusted.** Text responses are rendered as markdown and sanitized with
  DOMPurify before they reach the page. If the sanitizer fails to load, the workbench falls
  back to escaped plain text instead of rendering HTML.

## Layout

```text
src/corbelity/workbench/
  app.py          FastAPI app: /api/generate, /api/models, /api/config, /api/traces
  __main__.py     the corbelity-workbench command
  static/         index.html, app.js, style.css, favicon.svg
```

`/api/generate` is a plain `def` rather than `async def` on purpose: every provider SDK
blocks, and an image generation runs for tens of seconds. Declared `async`, that would stall
every other request for the length of the call; FastAPI runs a plain `def` in a threadpool
instead.

## Status and support

Pre-1.0: the interface may change between minor versions. Provided as-is under the Apache
License 2.0, with no support commitment or SLA. Issues and pull requests are welcome, but
response time is best-effort.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

The browser interface loads `marked`, `DOMPurify`, `highlight.js` and Google Fonts from
public CDNs at runtime. None are redistributed here; each carries its own license, listed
in [NOTICE](NOTICE).

Provider names are the trademarks of their respective owners and are used only to identify
the service being called.
