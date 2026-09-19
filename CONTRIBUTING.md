# Contributing

Thanks for looking. This is a small tool maintained alongside other work, so the sections
below are mostly about setting expectations honestly — what tends to get merged, what
doesn't, and how long you should expect to wait.

## Which repository does your change belong in?

Worth settling first, because it decides where you spend your evening.

The workbench is a browser UI over
[`corbelity-model-client`](https://github.com/Corbelity/model-client). It owns the screen,
the HTTP endpoints and the conversation the browser keeps. It owns nothing about how a
provider is called.

| Your change | Repository |
|---|---|
| A provider request or response is mishandled | model-client |
| A new or retired model in the catalog | model-client |
| Tracing records the wrong thing | model-client |
| The UI misrenders something, or a control is wrong | here |
| An endpoint returns the wrong status or payload | here |
| A new panel, control or workflow in the browser | here |

The clue is whether a script calling the library directly would hit it too. If yes, it
belongs upstream — fixing it here would paper over it for one caller and leave it broken
for everyone else.

## Before you write code

**Open an issue first for anything substantial.** A bug report needs no preamble, and a
one-line fix can come straight in as a pull request. But for a new panel, a new endpoint
or a refactor, please describe it in an issue before building it. The worst outcome is you
spending an evening on something that was never going to be merged, and a short
conversation avoids it entirely.

## What tends to get merged

- Bug fixes, with a test that fails before the fix and passes after it.
- Accessibility fixes — contrast, focus order, keyboard traps, missing labels.
- Corrections where the UI misrepresents what was actually sent or received.
- Documentation corrections, including in code comments.
- Improved error messages, especially ones that name the specific thing to fix.

## What probably doesn't

- **Anything that belongs in model-client.** See the table above.
- **Persuading the server to remember things.** The conversation lives in the browser and
  is posted with each request. That is what lets you switch models mid-conversation and
  edit any earlier turn, and it keeps the server stateless. Server-side sessions would
  cost both.
- **Features that assume a deployment.** No authentication, no multi-user state, no
  database. This binds to localhost and is built for one person on one machine; see
  [Security](#security) below.
- **Weakening the safety rails.** Output is sanitized before it reaches the DOM, image
  fetches are bounded by scheme, size, timeout and redirect count, and non-loopback binds
  warn loudly. Each looks removable and is not.
- **Off-brand styling.** Every colour, typeface and radius comes from the `:root` block in
  `style.css`. A raw hex value further down the file is a bug, not a shortcut.
- **Pure style changes.** Formatting is `ruff`'s business. Renaming things for taste
  creates review load without changing behaviour.

## Development setup

```bash
git clone https://github.com/Corbelity/model-workbench.git
cd model-workbench
uv sync --group dev
cp .env.example .env    # PowerShell: Copy-Item .env.example .env
```

Add at least one provider key to `.env`, then:

```bash
uv run corbelity-workbench
```

Before you open a pull request:

```bash
uv run ruff check .
uv run pytest
```

Both run in CI and both must pass. CI also builds the wheel and verifies the UI files are
inside it — see [Frontend changes](#frontend-changes).

Requires Python 3.12 or newer.

### Tests

New behaviour needs a test. The suite runs with no network access and no credentials, and
it should stay that way. Two seams make that possible:

- **The provider.** Monkeypatch `make_model_client` with a stub that records what it was
  handed. Every test in `tests/test_generate_history.py` works this way.
- **Image fetching.** `normalize_attachments` accepts an `httpx.Client`, so
  `httpx.MockTransport` answers without a socket. See `tests/test_image_attachments.py`.

If a test seems to need more than that, the seam has probably leaked, and that's worth
raising in the pull request.

`tests/conftest.py` pins every test to the built-in model catalog, so a
`CORBELITY_MODEL_CATALOG` in your own `.env` doesn't change what the suite asserts.

pytest runs with `filterwarnings = ["error"]`. A deprecation warning from our own code
fails the suite rather than waiting to become a break on some later dependency bump. The
one ignored warning is third-party and documented in `pyproject.toml`.

### Frontend changes

`index.html`, `app.js`, `style.css` and the images are data files, not Python. That has
two consequences worth knowing before you move or add one:

- They are served from the package directory, so they ship inside the wheel. Adding a file
  that the build excludes breaks an *installed* copy while your source tree keeps working.
  CI checks for exactly this; if you add an asset, add it to that check in
  `.github/workflows/ci.yml`.
- There is no build step, no bundler and no framework. Plain HTML, CSS and JavaScript,
  loaded directly. Please keep it that way — a toolchain here would cost more than it
  returns.

Model output is untrusted: it is rendered as markdown and sanitized with DOMPurify before
it reaches the DOM, and it falls back to escaped plain text if the sanitizer fails to
load. If you touch rendering, that fallback has to stay closed.

### Code style

`ruff` handles formatting and lint; don't hand-tune to taste.

A change to anything public — an endpoint, a request or response field, an environment
variable, the catalog format, the `corbelity-workbench` command, or the `:root` design
tokens — needs a [CHANGELOG.md](CHANGELOG.md) entry under `[Unreleased]`. Purely internal
changes don't.

The one convention worth stating: **comments explain why, not what.** The existing code
documents the non-obvious reasons — why `/api/generate` is a plain `def` and not `async`,
why a generated image never enters the conversation history, why the alpha on the logo was
cleaned up before use. Those comments are the most valuable thing in the source, and a
pull request that removes one to save a line will be asked to put it back.

## Security

This tool has no authentication. It binds to `127.0.0.1`, `/api/traces` serves full
prompts and responses, and the server fetches image URLs on your behalf. Those facts are
connected: the trust model is a single user on a single machine, and several deliberate
choices only hold under it.

A change that widens exposure — binding elsewhere by default, relaxing the URL rules,
adding an endpoint that reads outside the trace file — needs to say so explicitly in the
pull request, even when it looks like a convenience.

Found a vulnerability? Please don't open a public issue. See [SECURITY.md](SECURITY.md).

## Sign your commits (DCO)

Contributions are accepted under the
[Developer Certificate of Origin](https://developercertificate.org/): a short statement
that you wrote the contribution or otherwise have the right to submit it under this
project's license. There is no CLA to sign.

Add a sign-off line to each commit:

```bash
git commit -s -m "Keep the focus ring visible on the modality pills"
```

which appends:

```
Signed-off-by: Your Name <your.email@example.com>
```

Forgot on the last commit? `git commit --amend -s`. Across several? `git rebase --signoff`
over the range. CI checks every non-merge commit in a pull request.

## Pull requests

- One logical change per pull request. A fix bundled with a refactor is hard to review and
  harder to revert.
- Describe what changes and **why**. A link to the issue is enough for the what.
- Add a `CHANGELOG.md` entry if the change is user-visible (see [Code style](#code-style)).
- For anything visual, include a screenshot. It is much faster than reading the diff.
- Note anything you couldn't verify — a provider you have no credentials for, a browser
  you couldn't test in. That's useful information, not a weakness in the submission.
- Expect follow-up questions. Being asked why you chose an approach isn't a rejection.

**AI-assisted contributions are fine** — this tool exists to be used with these models. But
you're accountable for what you submit: read it, run it, and be able to explain why it
works. A patch the submitter can't explain is worse than no patch, because reviewing it
costs more than writing it would have.

## Response times

This is maintained in gaps between other work. Realistically: a week or so for a first
response, sometimes longer. A pull request sitting unreviewed means I haven't got to it,
not that it's been rejected — feel free to bump it after a couple of weeks.

## License

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE), the same license that covers this project.
