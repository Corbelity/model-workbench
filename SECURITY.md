# Security Policy

## Reporting a vulnerability

Please report security issues **privately**, not as a public GitHub issue.

- Preferred: [GitHub private vulnerability reporting](https://github.com/Corbelity/model-workbench/security/advisories/new)
- Or email: **info@corbelity.com**

Please include what you were running (version, Python version, browser if relevant), what
you observed, and the smallest reproduction you have. If you have a fix in mind, say so —
but please don't open a public pull request for a security issue before we've agreed how
to handle it.

**What to expect.** This is a small project maintained alongside other work; there is no
SLA. A realistic expectation is acknowledgement within about a week, and a fix or a
decision within thirty days for anything confirmed. You'll be credited in the changelog
unless you'd rather not be. Please give us a chance to ship a fix before disclosing
publicly.

## The trust model, first

Everything below depends on this, so it is worth stating plainly.

**This is a single-user tool for your own machine.** It has no authentication, no
authorization and no concept of a user. It binds to `127.0.0.1` by default, and that
default is load-bearing rather than a convention — several deliberate design choices are
only safe because of it.

Anyone who can reach the port can spend your API keys, read every prompt and response you
have recorded, and make the server issue HTTP requests to hosts of their choosing.

`--host 0.0.0.0` prints a warning for that reason. Exposing the workbench to a network,
putting it behind a tunnel, or running it on a shared or cloud machine takes it outside
the model this tool was built for. **Doing that is not a vulnerability in the workbench,
but it may well produce one in your environment.**

## Supported versions

Pre-1.0, only the latest released version is supported. Fixes ship in a new release rather
than as patches to older ones.

## Scope

**In scope:** anything that leaks credentials, escapes the sanitizer, reads or writes
outside the trace paths configured, or lets a *locally* supplied prompt or model response
cause an effect beyond what is described below.

**Out of scope:**

- Consequences of running the server on a reachable interface — that is documented above,
  not a defect.
- Vulnerabilities in [`corbelity-model-client`](https://github.com/Corbelity/model-client);
  report those there.
- Provider SDKs and provider services (report to the vendor), and the content models
  generate.
- The third-party CDN scripts the page loads; see [Supply chain](#supply-chain) for what
  is and isn't guaranteed about them.

## Security properties worth knowing

### The server fetches URLs you give it

When you attach an image by URL, **the server fetches it**, not the browser. That is what
makes every provider behave identically — native Ollama accepts no URL form at all — but
it does mean an outbound request to a host supplied at request time.

The fetch is bounded: `http` and `https` only, a 15-second timeout, at most 3 redirects, a
per-image cap of 8 MB enforced as the body arrives (a `Content-Length` header is a claim,
not a fact), a per-request total of 16 MB and at most 4 images.

The **destination** is bounded too. The hostname is resolved and the request is refused if
any resolved address is loopback, private (RFC1918 or IPv6 unique-local), link-local
(which is where cloud instance metadata lives, at `169.254.169.254`), multicast, reserved
or unspecified. IPv4-mapped IPv6 addresses are unmapped before being judged, and a host is
refused if *any* of its addresses is blocked, so a name that round-robins between a public
and an internal address cannot get through on a lucky answer.

Redirects are followed by hand rather than by the HTTP library, and every hop goes back
through the same scheme and destination checks. A public URL that redirects to
`169.254.169.254` is refused at the second hop.

**Known limitation: DNS rebinding.** The name is resolved for the check, and the HTTP
client resolves it again when it connects. A name that returns a public address to the
first lookup and an internal one to the second is not stopped. Closing that needs a
transport that pins the address already checked, which is not implemented. What the
current check does remove is every straightforward case: literal internal addresses, names
that simply point inward, and redirect chains that turn inward partway.

None of this makes the workbench safe to expose. It narrows the blast radius of a bad URL;
it does not change the trust model above.

### Traces contain your prompts

With `TRACE_ENABLED` set, every call is appended to a JSONL file with the **full** prompt,
conversation history and response, and generated or attached media is written beside it.
Nothing is truncated.

- Tracing is **off by default**. Enabling it is a deliberate choice.
- `GET /api/traces` and `/api/traces/{run_id}` serve those records over HTTP with no
  authentication. Reading is independent of recording: a server started *without*
  `TRACE_ENABLED` will still serve traces written earlier, by an earlier process or by a
  script using the client library directly.
- `logs/` is in `.gitignore`. Keep it that way, and keep trace files out of shared drives
  and any log shipper you haven't reviewed.

### Credentials

- Keys are read from the environment, by the client library, using the variable names in
  each provider's spec. They are never sent to the browser: `/api/config` reports only
  whether each one is set.
- A key typed into the sidebar is stored in that browser's `localStorage` **in plain
  text**, readable by any script running on the same origin, and persists until cleared.
  It is a convenience for a throwaway key, not a place to keep a production one — prefer
  `.env` for anything long-lived.
- The request payload drawer reports where a credential came from (`UI override`, `Set in
  environment`, `Missing`), never its value.
- Errors name the environment variables consulted; they never echo a value.

### Model output is untrusted

A model response can contain anything, including markup, and a prompt-injected response
can try to. Because this page holds API keys in `localStorage`, that matters more than it
would elsewhere.

- Responses are rendered as markdown and sanitized with DOMPurify before reaching the DOM.
- If DOMPurify fails to load, rendering **fails closed** to escaped plain text. It does not
  fall back to raw HTML.
- Everything else — conversation cards, error messages, provider text — is written with
  `textContent`, never `innerHTML`.

### Request handling

- Conversation history and attachments are validated before any provider client is built,
  so malformed input costs no credential and no network call.
- Image MIME types come from magic bytes, not from a filename or a server-supplied
  `Content-Type`.
- There is **no CSRF token and no `Origin` check**. Cross-origin JavaScript cannot read the
  responses, and a JSON `POST` requires a preflight the server does not grant, so ordinary
  cross-site requests fail. A **DNS rebinding** attack against a browser on the same
  machine is not defended against, because the server does not validate the `Host` header.
  Treat that as a known limitation of the localhost trust model.
- The server writes no files of its own beyond the trace file and artifacts directory the
  client library is configured to use.

### Supply chain

The page loads five third-party assets from public CDNs: `marked`, `DOMPurify` and
`highlight.js` (plus its stylesheet) from cdnjs and jsDelivr, and fonts from Google Fonts.

- Versions are **pinned**, so an upstream release cannot silently change behaviour.
- There are **no Subresource Integrity hashes**, so a compromised CDN could serve different
  content at those pinned URLs. On a tool that holds API keys in `localStorage`, that is a
  real if unlikely exposure.
- The workbench works offline apart from these: without them, markdown renders as escaped
  plain text and the fallback font stacks apply.

Vendoring these locally would remove the CDN from the trust boundary and is a reasonable
change to propose.
