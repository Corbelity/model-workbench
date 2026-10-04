# Design notes

Why the workbench is shaped the way it is. The README covers what it does; this covers the
decisions behind it, including the ones that cost something.

The thing to hold onto: this is **deliberately a thin application**. Its job is to drive
[`corbelity-model-client`](https://github.com/Corbelity/model-client) from a browser and let
someone try models without writing code. Whenever a decision could go "put it in the app" or
"put it in the library", the answer has been the library — unless it is policy, presentation,
or something a library must not do.

## 1. The server is stateless; the conversation lives in the browser

History is held in the browser and posted with every request. Nothing about a session exists
on the server.

The consequences are the point rather than side effects:

- **A model can be switched mid-conversation.** The same context is handed to a different
  provider on the next request. This is the workbench's central trick, and a server-side
  session would make it a migration problem instead of a non-event.
- **Any earlier turn can be edited or deleted.** Each is a card in the browser, and the next
  request carries whatever the cards now say.
- **Restarting the server loses nothing.**

The costs are real: a long conversation is re-sent on every request, and browser storage has
a ceiling — which is what forces the next decision.

## 2. Generated media enters the conversation as a placeholder, not a payload

An image or audio result comes back as a data URL for display, but what enters the
conversation history is a short text placeholder.

Two independent reasons, either sufficient on its own:

- **A data URL cannot be replayed to a text model.** Putting one in history would send a wall
  of base64 to the next model as though it were dialogue.
- **It would exhaust `localStorage`.** A single generated image can be megabytes; a handful
  would break the conversation store outright.

## 3. Routing is the catalog entry's `service`

The model dropdown is the library's catalog — the built-in set with an optional user file
(`CORBELITY_MODEL_CATALOG`) merged over it by model id. The selected entry carries a
`service`, and that is what picks the provider.

So the workbench holds **no model list of its own** and no provider-selection logic. An
unlisted model id is rejected rather than attempted, because a request naming a model nobody
catalogued cannot be routed.

The catalog is re-read **per request**, so editing the file needs a page refresh rather than a
restart — in a tool whose whole purpose is trying models, a restart to add one is friction in
the wrong place. A catalog that fails to parse is logged and the built-in list is used
instead, so a typo costs you your additions rather than the application.

## 4. Credentials: the app decides *whether*, the library decides *which*

The workbench decides exactly one thing — whether the UI supplied a per-session override.
Which environment variables are consulted, and in what order, is read from each provider's
spec in the library.

That is why `/api/config` derives its field list from the provider registry rather than
hardcoding one: **a provider added to the library appears in the UI with no edit here.**

Two rules the sidebar follows:

- **Environment credentials are never sent to the browser.** `/api/config` reports only
  whether each one is set.
- **A key typed into the UI lives in that browser's local storage, in plain text.** The
  payload drawer reports where each credential came from — the source, never the value — so
  which key a call will use is visible before sending it.

## 5. The server fetches image URLs itself, and defends itself while doing so

An attached image can be a file or a URL. Both are resolved to **bytes before any provider
client is built**, so every provider behaves identically. Without this the two attachment
routes would diverge per provider: native Ollama has no URL form at all.

That means the server performs an outbound request to a user-supplied URL, which is an SSRF
surface. It is bounded by **destination**, not only by scheme:

- The hostname is resolved and every resulting address classified. Loopback, private,
  link-local (including the cloud instance-metadata range), multicast, reserved and
  unspecified are refused.
- IPv4-mapped IPv6 is unmapped before being judged, or `::ffff:127.0.0.1` walks straight past
  the v4 checks.
- The specific kinds are tested **before** the general private check, which is also true for
  several of them, so the error names the real reason rather than a catch-all.
- Redirects are followed **manually** so every hop is re-checked. Delegating the redirect
  chain to the HTTP client would check only the first URL.
- Bounded by a timeout, a redirect limit, and per-image and per-request size caps enforced as
  the body arrives rather than after it has all arrived.

DNS rebinding is out of scope; see [SECURITY.md](SECURITY.md).

## 6. Attachment caps are the application's job

Four images per request, 8 MB each, 16 MB in total.

The library deliberately enforces no count or size caps in its shared validation, on the
grounds that those are *policy* and belong at the HTTP boundary. This is that boundary. A
script legitimately sending one 40 MB scan through the library is not this UI's problem, and
this UI's limits are not that script's problem.

*This split is the clearest demonstration that the library/application line is real rather
than aspirational.*

## 7. `/api/generate` is a plain `def`, not `async def`

Every provider SDK blocks, and an image generation can run for tens of seconds. Declared
`async`, one such call would stall every other request for its whole duration. FastAPI runs a
plain `def` in a threadpool instead.

This is also why the library's trace logger locks its appends: a sync handler in a threadpool
produces genuinely concurrent calls, and unlocked writes would interleave into unparseable
lines.

## 8. Capability decisions are computed server-side and published

`/api/models` publishes a computed `honors_sampling` per model, and the temperature and top-p
sliders are dimmed with the reason in place of the hint text.

The browser cannot work this out: it depends on the provider registry, which lives in the
library and has no browser-side view. Two options were available and only one is honest —
either the sliders keep pretending, or the server tells the browser the truth. **A control
that silently does nothing is worse than a disabled one**, because the user forms a theory
about the output that the software already knows is false.

The same principle gates the attach controls on the catalog's `accepts_images`: on a text
model it means the model can read an image, and on an image model that it accepts reference
images to condition what it generates. Speech takes neither.

## 9. No build step, and the design tokens are public

The UI is plain HTML, CSS and JavaScript served straight from the package — no framework, no
bundler, no transpile step. The moment there is one, a contributor needs a JavaScript
toolchain installed to change a colour.

Every colour, typeface, radius and spacing value comes from the `:root` block in `style.css`.
**A raw hex value further down that file is a bug.** That block is treated as public even
though `app.js` and `style.css` are otherwise implementation details, because re-skinning by
overriding the tokens is a supported thing to do.

A development consequence worth knowing: `--reload` also sends `Cache-Control: no-store`. The
page itself is served uncached while the static files are not, so without it a frontend edit
can leave the browser drawing new markup against the old stylesheet — which presents as a
broken layout rather than as a stale file.

## 10. It runs on your machine, and says so

No authentication. It binds `127.0.0.1` by default and **warns explicitly** when bound
anywhere else, because whoever can reach it can spend the API keys, read every recorded
trace, and make the server fetch arbitrary URLs.

Model output is untrusted: responses are rendered as markdown sanitized with DOMPurify, and
if the sanitizer fails to load, rendering **falls back to escaped plain text** rather than raw
HTML. A failed dependency degrades to safe-and-ugly instead of unsafe-and-pretty.

## 11. Trace review reads whatever this server is recording

`GET /api/traces` and `/api/traces/{run_id}` read the trace file regardless of
`TRACE_ENABLED`, which governs only *recording*. Reviewing a trace captured yesterday must
not require turning capture on today — the same reasoning the library applies to where traces
live.

A trace file whose last line was truncated by a killed process still reads, with the skipped
line **reported rather than swallowed**. A partial trace is the normal end state of an
interrupted run, so refusing to open one would make the tool useless exactly when it is most
needed.

## 12. Video is submitted, not awaited — and the job outlives the request

Every other modality finishes inside one request: `/api/generate` calls the provider, gets
bytes back, and returns them. Video cannot work that way. A generation runs for tens of
seconds to minutes, and every provider the library targets is submit-then-poll. So video
has its own endpoints and its own shape:

| | |
|---|---|
| `POST /api/video/check` | would this combination be accepted? No client, no credential, no network. |
| `POST /api/video/submit` | start it; returns a job row in about one round trip. |
| `GET /api/video/jobs` | what this server knows about, newest first. |
| `GET /api/video/jobs/{id}` | one job, from the table — polls nothing. |
| `POST /api/video/jobs/{id}/poll` | ask the provider, and record a terminal outcome. |
| `GET /api/video/jobs/{id}/file` | the finished clip. |

Four decisions follow from that shape.

**The job is written to disk before the submitting response returns.** A submission that is
not recorded is a job that is running, billed, and unreachable — the provider keeps working
whether or not anyone still knows the operation id. The store is stdlib `sqlite3` with the
clips as files beside it: a row per job holding everything the library's `VideoJobRef`
carries, which is what `resume_video()` needs to pick a job up in another process. This is
the one place the workbench holds state, and §1 still holds for conversations — a job is
not a conversation.

**The clip is a file, not a data URL.** Generated images and speech are inlined as base64
(§2 covers what enters the transcript). A video cannot be: tens of megabytes becomes a
third more again as base64, has to be held in memory whole to build, and a `<video>`
element wants byte ranges to seek, which a data URL cannot serve.

**A refused combination is an answer, not an error.** `/api/video/check` calls the
library's `resolve_video_request()` — *the same function a real submission runs*, minus the
client and the credential. So the UI refuses an impossible choice using the real rules
rather than a copy of the constraint table in JavaScript, and a catalog edit changes both
at once. It returns `200` with `ok: false`, because the UI asks on every change to a
control and a 400 per refusal would fill the console with red. A `400` there means the
*question* was malformed. The check judges the combination, not the frames, and says so
(`images_checked: false`) so a green answer is not read as more than it checked.

**Credentials are the one thing a restart loses.** A key typed into the sidebar is a
per-request override and is deliberately never written to the job table, so a job polled
after a restart needs that key sent again — which is why the poll is a `POST` with a body
rather than a `GET` with the key in a URL that lands in an access log — or the provider's
environment variable. Nothing else about the job is lost.

Video is absent from `GENERATE_MODALITIES` for this reason and not because it is
unsupported, so `/api/generate` names where it does belong rather than only saying no.

## 13. What is deliberately not here

- **Authentication, multi-user, hosting.** It is a local tool. Adding auth would imply it is
  safe to expose, which the SSRF surface and the browser-stored keys argue against.
- **A build step.** See §9.
- **Its own model list.** That is the library's catalog. Duplicating it would mean two places
  to add a model and two places to forget.
- **Cost as truth.** Cost is *estimated* from the catalog's per-1k rates, and a model with no
  rates reports $0 rather than an invented number.
