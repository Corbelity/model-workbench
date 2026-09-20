# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

**Pre-1.0 versioning:** while the major version is 0, the HTTP endpoints, request and
response shapes, and environment variables may change between minor versions. Breaking
changes are listed under `Changed` or `Removed` and called out explicitly, so a minor bump
is worth reading before you take it.

**What counts as public here:** the endpoints under `/api/`, the environment variables the
server reads, the model catalog format, and the `corbelity-workbench` command. The browser
code is an implementation detail — `app.js` and `style.css` may be reorganised freely, but
the `:root` design tokens in `style.css` are treated as public, because re-skinning by
overriding them is a supported thing to do.

## [Unreleased]

Everything below becomes `0.1.0` when the first tag is cut.

### Added

- OpenAI and Gemini support, following the providers added to
  [`corbelity-model-client`](https://github.com/Corbelity/model-client). OpenAI generates
  text, images and speech natively; Gemini is text only, running through Google's
  OpenAI-compatibility endpoint. Both get a sidebar credential field and a `.env` badge,
  and Gemini accepts either `GEMINI_API_KEY` or `GOOGLE_API_KEY`.
- `CORBELITY_SERVICES` narrows the model dropdown to named services. Presentation only:
  it does not disable a service, and a filtered-out model still runs if something asks
  for it.
- The temperature and top-p sliders are dimmed and disabled for models whose provider
  would not receive them, with the hint text replaced by the reason. `/api/models`
  publishes a computed `honors_sampling` per model, because the decision depends on the
  provider registry and the browser has no view of it.
- A browser workbench for running one prompt across text, image and speech models and
  comparing the output, latency, token usage and estimated cost — built on
  [`corbelity-model-client`](https://github.com/Corbelity/model-client), so Anthropic,
  OpenAI, Gemini, OpenRouter, local Ollama, Ollama Cloud and HuggingFace are driven from
  one screen.
- `POST /api/generate`, which routes on the selected model's catalog entry, returning the
  result, real provider-reported token usage, latency, an estimated cost and a
  `context_payload` describing exactly what was sent.
- Browser-held conversation history, posted with each request. The server stays stateless,
  so a model can be switched mid-conversation and any earlier turn edited or deleted.
- Image input for vision models, by file or URL, gated on the catalog's `accepts_images`
  flag. The server resolves both forms to bytes before any provider client is built, so
  every provider behaves identically — native Ollama has no URL form at all.
- Text, image and speech output. Generated media is returned as a data URL and enters the
  conversation as a short placeholder rather than a payload, since a data URL cannot be
  replayed to a text model and would exhaust `localStorage`.
- `GET /api/models` and `GET /api/config`. The model list is the client library's catalog,
  merged with any file named by `CORBELITY_MODEL_CATALOG` and re-read per request, so a
  catalog edit needs a page refresh rather than a restart. `/api/config` reports only
  whether each credential is set, never its value, and derives its list from the provider
  registry so a provider added to the library appears without an edit here.
- `GET /api/traces` and `GET /api/traces/{run_id}` for reviewing recorded calls, grouped
  into runs with token totals, error and artifact counts. Reading works whether or not
  this server is recording, and a trace file whose last line was truncated by a killed
  process still reads, with the skipped line reported rather than swallowed.
- Per-session credential and endpoint overrides in the UI, with a badge showing which
  credentials the server already holds and a payload drawer reporting where each one came
  from — the source, never the value.
- The `corbelity-workbench` command. Binds to `127.0.0.1` by default and warns explicitly
  when bound anywhere else, because the tool has no authentication.
- Corbelity branding: the "Network Node" theme, with every colour, typeface, radius and
  spacing value taken from the design system's tokens and declared in one `:root` block.
- Test suite covering routing, the history and image contracts, attachment normalisation,
  cost estimation, credential reporting and the trace endpoints. Runs with no network
  access and no credentials.
- CI on Python 3.12, 3.13 and 3.14: lint, tests, and a wheel build that verifies the UI
  files are actually inside the wheel.

### Provider behaviour worth knowing

- **Temperature and top-p are accepted and ignored for Anthropic models.** The Messages
  API withdrew `temperature`, `top_p` and `top_k`; they are absent from
  `MessageCreateParams` as of `anthropic` 1.7, so passing one raised a `TypeError` from
  the SDK before anything reached the service. `corbelity-model-client` no longer sends
  them and now requires `anthropic>=1.7`. Failing every Anthropic call because a slider
  holds a value would have been worse, so the values are dropped rather than rejected --
  the dimmed sliders are how that stays visible instead of silent.

### Security

- Server-side image fetches are restricted by destination, not only by scheme. The
  hostname is resolved and the request refused if any resolved address is loopback,
  private, link-local (including the `169.254.169.254` metadata endpoint), multicast,
  reserved or unspecified. Redirects are followed manually so every hop is re-checked.
  DNS rebinding remains out of scope; see `SECURITY.md`.
- Fetches are also bounded by a timeout, a redirect limit, and per-image and per-request
  size caps enforced as the body arrives.
- Model output is rendered as markdown and sanitized with DOMPurify before it reaches the
  DOM. If the sanitizer fails to load, rendering falls back to escaped plain text rather
  than raw HTML.
- Credentials from the environment are never sent to the browser.

### Notes on provenance

Extracted from an internal prototype, alongside the client library it now depends on.
Behaviour is preserved except where listed below.

- The private model registry was replaced by the client library's catalog. The workbench
  no longer ships its own model list, and an unlisted model id is rejected rather than
  attempted.
- Credential resolution moved into the client library. The workbench decides only whether
  the UI supplied a per-session override; which environment variables are consulted, and
  in what order, is read from each provider's spec rather than duplicated here.
- The server loads `.env` and configures logging itself. The client library deliberately
  does neither, so that it stays safe to embed; those are application decisions and this
  is the application.
- Inert provider fields that posted to nothing were removed from the sidebar.
- `/api/generate` is a plain `def` rather than `async def`. Every provider SDK blocks and a
  media generation can run for tens of seconds; declared `async`, it would stall every
  other request for the length of the call.

[Unreleased]: https://github.com/Corbelity/model-workbench/commits/main
