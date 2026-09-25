"""
corbelity.workbench.app -- the Corbelity Model Workbench backend.

FastAPI over corbelity.model_client. The UI's model list is the model catalog (the
built-in one, with an optional user file named by CORBELITY_MODEL_CATALOG merged over it),
and every request is routed by the `service` on the selected catalog entry.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import socket
import time
from collections.abc import Callable
from dataclasses import asdict
from ipaddress import IPv4Address, IPv6Address, ip_address
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from corbelity.model_client import (
    IMAGE,
    SOUND,
    SUPPORTED_IMAGE_MIMES,
    TEXT,
    ImageInput,
    MediaResult,
    ModelCatalog,
    ModelInfo,
    TooManyImagesError,
    UnsupportedImageInputError,
    UnsupportedModalityError,
    catalog_for,
    get_default_config,
    known_services,
    make_model_client,
    make_trace_logger,
    provider_spec,
    resolve_service,
    sniff_image_mime,
    supported_modalities,
    trace_file_path,
    validate_history,
    validate_images,
)
from dotenv import find_dotenv, load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# The model client deliberately reads no .env file and configures no logging: both are
# application decisions, and this is the application. usecwd=True searches from where the
# server was launched rather than from this file's directory -- an installed copy of this
# package lives in site-packages, which is nowhere near the user's .env.
load_dotenv(find_dotenv(usecwd=True))

logging.basicConfig(
    level=logging.getLevelNamesMapping().get(
        os.getenv("LOGGING_LEVEL", "").strip().upper(), logging.INFO
    ),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Resolved once at import, so a malformed CORBELITY_* variable fails the launch with a
# readable message instead of surfacing as a 500 on the first request.
CONFIG = get_default_config()

DEV_MODE = os.getenv("WORKBENCH_DEV", "").strip().lower() in ("1", "true", "yes", "on")

# Anchored to THIS FILE, never the process cwd.
PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"

# One tracer per server process, so every call made in a sitting shares a run_id. None
# unless TRACE_ENABLED is set -- traces hold full prompts and responses.
TRACE = make_trace_logger()
if TRACE is not None:
    logger.info("Tracing enabled (run %s) -> %s", TRACE.run_id, TRACE.path)

app = FastAPI(title="Corbelity Model Workbench")

# Development convenience. StaticFiles sends caching headers while the index is served by a
# FileResponse that does not, so an edit to style.css or app.js can leave the browser
# rendering new markup against a stale stylesheet -- which looks like a broken layout
# rather than a caching problem. In dev, nothing is cached.
#
# Off by default: on a normal run the assets never change while the server is up, and
# re-sending them on every request is waste. `--reload` turns it on (see __main__.py), or
# set WORKBENCH_DEV=1 when running uvicorn directly.
if DEV_MODE:
    logger.info("Dev mode: responses are sent with Cache-Control: no-store.")

    @app.middleware("http")
    async def disable_caching(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    # Browsers request /favicon.ico regardless of the <link> in index.html, so this has to
    # resolve to a real file or every page load logs a 500.
    return FileResponse(STATIC_DIR / "favicon.png", media_type="image/png")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# Which request field carries a per-session override for each service. A service absent
# from a map simply has no UI field for it: Ollama Cloud has no key input yet, and only
# the local Ollama host takes an endpoint override. Everything else about credentials --
# which environment variables, in what order -- is the client's business, read from its
# ProviderSpec rather than duplicated here.
UI_KEY_FIELDS: dict[str, str] = {
    "anthropic": "anthropic_key",
    "openai": "openai_key",
    "gemini": "gemini_key",
    "openrouter": "openrouter_key",
    "huggingface": "huggingface_key",
}
UI_URL_FIELDS: dict[str, str] = {"ollama-local": "ollama_url"}

# The UI's sound pill posts "sound"; accept the obvious synonyms too.
MODALITY_ALIASES = {"audio": SOUND, "speech": SOUND, "img": IMAGE}

# How long a media prompt may be inside a context placeholder before it is trimmed.
MEDIA_PLACEHOLDER_PROMPT_CHARS = 200

# Image-input policy. These are POLICY, which is why they live here and not in
# corbelity.model_client -- a script caller sending one large scan should not be bound by
# a limit that exists to keep the browser and the providers happy.
MAX_INPUT_IMAGES = 4
MAX_IMAGE_BYTES = 8 * 1024 * 1024          # per image
MAX_TOTAL_IMAGE_BYTES = 16 * 1024 * 1024   # across one request
IMAGE_FETCH_TIMEOUT_S = 15.0
IMAGE_FETCH_MAX_REDIRECTS = 3

# One resolved IP address, and the shape of a resolver. The indirection exists so tests can
# supply their own answers instead of depending on DNS.
IPAddress = IPv4Address | IPv6Address
HostResolver = Callable[[str], list[IPAddress]]

_DATA_URL_RE = re.compile(r"^data:(image/[A-Za-z0-9.+-]+);base64,(.+)$", re.DOTALL)


def load_models() -> ModelCatalog:
    """The catalog the UI offers: the built-in entries, with the user's catalog file (if
    CORBELITY_MODEL_CATALOG names one) merged over them, narrowed to CORBELITY_SERVICES
    when that names an allow-list.

    catalog_for() is the library's listing path and is presentation only -- narrowing what
    is shown never changes how a call behaves, and a filtered-out service is still
    callable. A user catalog is re-read on every call, so editing the file and refreshing
    the page is enough. A broken one is logged loudly and skipped rather than emptying the
    dropdown; the fallback drops the user catalog, not the service filter."""
    try:
        return catalog_for(CONFIG)
    except (OSError, ValueError) as err:   # json.JSONDecodeError is a ValueError
        logger.error("Could not load model catalog %s (%s); using the built-in catalog.",
                     CONFIG.catalog_path, err)
        return catalog_for(CONFIG.with_overrides(catalog_path=None))


class Message(BaseModel):
    """One prior conversation turn. `system` is intentionally NOT a valid role here --
    the system prompt has its own field, and allowing it would create a second place
    that sets it."""
    role: Literal["user", "assistant"]
    content: str


class ImageAttachment(BaseModel):
    """One image the user attached to the current prompt.

    Exactly one of `data_url` (what a browser FileReader produces) and `url` (a remote
    image) must be set. `name` is for humans -- the transcript placeholder and the trace
    record; it never reaches a provider."""
    data_url: str | None = None
    url: str | None = None
    name: str | None = None


class GenerateRequest(BaseModel):
    model: str
    system_prompt: str | None = "You are a helpful assistant."
    prompt: str
    history: list[Message] = []
    images: list[ImageAttachment] = []
    modality: str = TEXT
    temperature: float = 0.7
    top_p: float = 0.9
    max_tokens: int = 2048
    anthropic_key: str | None = None
    openai_key: str | None = None
    gemini_key: str | None = None
    openrouter_key: str | None = None
    huggingface_key: str | None = None
    ollama_url: str | None = None


def override_for(req: GenerateRequest, service: str, fields: dict[str, str]) -> str | None:
    """The per-session UI override for `service`, or None when the UI has no field for it
    or the field was left empty (an empty string means "not set", never "blank key")."""
    field = fields.get(service)
    value = getattr(req, field, None) if field else None
    if not value:
        return None
    return value.strip() or None


def key_origin(service: str, override: str | None) -> str:
    """Where the credential for a service came from -- shown in the payload drawer so a
    surprising result can be traced to the wrong key. Reports the source, never the value."""
    if override:
        return "UI override"
    names = provider_spec(service).key_env
    if not names:
        return "Not required"
    return "Set in environment" if any(os.getenv(n, "").strip() for n in names) else "Missing"


def data_url(media: MediaResult) -> str:
    """Inline the payload so the frontend can drop it straight into an <img>/<audio> src
    without a second round trip or any server-side file storage."""
    encoded = base64.b64encode(media.data).decode("ascii")
    return f"data:{media.mime_type};base64,{encoded}"


def _decode_data_url(index: int, value: str, name: str | None) -> ImageInput:
    match = _DATA_URL_RE.match(value.strip())
    if match is None:
        raise HTTPException(
            status_code=400,
            detail=f"Image {index} is not a base64 image data URL.",
        )
    mime, payload = match.group(1).lower(), match.group(2)
    try:
        # validate=True rejects stray characters rather than silently decoding garbage;
        # whitespace is stripped first because a hand-pasted URL is usually wrapped.
        data = base64.b64decode(re.sub(r"\s+", "", payload), validate=True)
    except (binascii.Error, ValueError) as err:
        raise HTTPException(
            status_code=400,
            detail=f"Image {index} has malformed base64 data: {err}",
        ) from err
    return ImageInput(data=data, mime_type=mime, name=name)


def resolve_host(host: str) -> list[IPAddress]:
    """Every address `host` resolves to. A literal IP needs no lookup and returns itself."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as err:
        raise HTTPException(
            status_code=400,
            detail=f"Could not resolve {host!r}: {err}.",
        ) from err

    addresses: list[IPAddress] = []
    for info in infos:
        address = ip_address(info[4][0])
        # ::ffff:127.0.0.1 is loopback wearing an IPv6 costume. Unmap before judging it,
        # or the v4 checks below never run.
        if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        addresses.append(address)
    return addresses


def address_block_reason(address: IPAddress) -> str | None:
    """Why this address is off limits, or None if it is ordinary public space.

    The specific kinds are tested before is_private, which is also true for several of
    them, so that the error names the real reason rather than a catch-all."""
    if address.is_unspecified:
        return "unspecified"
    if address.is_loopback:
        return "loopback"
    if address.is_link_local:       # 169.254.0.0/16 -- cloud instance metadata lives here
        return "link-local"
    if address.is_multicast:
        return "multicast"
    if address.is_reserved:
        return "reserved"
    if address.is_private:          # RFC1918, unique-local, carrier-grade NAT
        return "private"
    return None


def assert_fetchable(index: int, url: str, resolve: HostResolver) -> None:
    """Refuse a URL this server should not be made to request.

    The fetch runs on the SERVER, so a URL is really an instruction to make this machine
    issue a request. On the single-user box this tool targets that grants a local user
    nothing new, but on a cloud VM or any host with reachable internal services it would be
    a server-side request forgery primitive -- so the destination is checked, not just the
    scheme.

    Called for the original URL and again for every redirect hop, because a public URL that
    redirects inward is the obvious way around a check that only ran once.

    LIMITATION: this resolves the name and httpx resolves it again when it connects, so a
    name that changes its answer between the two (DNS rebinding) is not stopped. Closing
    that needs a transport that pins the address checked here. What this does remove is
    every straightforward case: literal internal IPs, names that simply point inward, and
    redirect chains that turn inward partway."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=400,
            detail=(f"Image {index} must be an http(s) URL or a data URL"
                    f" (got scheme {parsed.scheme or 'none'!r})."),
        )

    host = parsed.hostname
    if not host:
        raise HTTPException(
            status_code=400,
            detail=f"Image {index}: {url!r} has no host.",
        )

    addresses = resolve(host)
    if not addresses:
        raise HTTPException(
            status_code=400,
            detail=f"Image {index}: {host!r} resolved to no addresses.",
        )

    # ANY blocked address disqualifies the host. A name that round-robins between a public
    # address and an internal one must not come down to which answer arrived first.
    for address in addresses:
        reason = address_block_reason(address)
        if reason is not None:
            raise HTTPException(
                status_code=400,
                detail=(f"Image {index}: {host} resolves to a {reason} address"
                        f" ({address}), which this server will not fetch."
                        " Attach the file directly instead."),
            )


def _read_capped_body(index: int, response: httpx.Response) -> bytes:
    """Accumulated against the cap as it arrives: a Content-Length header is a claim, not a
    fact, and reading the whole body to measure it afterwards is exactly what the cap is
    there to prevent."""
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > MAX_IMAGE_BYTES:
            raise HTTPException(
                status_code=400,
                detail=(f"Image {index} exceeds the per-image limit of"
                        f" {MAX_IMAGE_BYTES} bytes."),
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _fetch_image(index: int, url: str, name: str | None,
                 client: httpx.Client | None = None,
                 resolve: HostResolver | None = None) -> ImageInput:
    """Resolve a remote image to bytes.

    Fetching here rather than passing the URL to the provider is what makes every provider
    behave identically -- native Ollama has no URL form at all. The cost is an outbound
    request to a user-supplied URL, bounded by scheme, destination, timeout, redirect count
    and byte count.

    Redirects are followed by hand rather than by httpx, because each hop has to go back
    through assert_fetchable."""
    resolve = resolve or resolve_host
    owned = client is None
    if client is None:
        # follow_redirects stays off: the loop below does it, checking every hop.
        client = httpx.Client(timeout=IMAGE_FETCH_TIMEOUT_S, follow_redirects=False)

    current = url
    try:
        for _ in range(IMAGE_FETCH_MAX_REDIRECTS + 1):
            assert_fetchable(index, current, resolve)
            with client.stream("GET", current, follow_redirects=False) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise HTTPException(
                            status_code=400,
                            detail=(f"Image {index}: {current} returned a redirect with no"
                                    " Location header."),
                        )
                    # Relative Locations are legal, so resolve against the current URL.
                    current = str(httpx.URL(current).join(location))
                    continue

                if response.status_code >= 400:
                    raise HTTPException(
                        status_code=400,
                        detail=(f"Image {index}: fetching {current} returned HTTP"
                                f" {response.status_code}."),
                    )

                data = _read_capped_body(index, response)
                declared = (response.headers.get("content-type") or "").split(";")[0]
                declared = declared.strip().lower()
                mime = declared if declared in SUPPORTED_IMAGE_MIMES else sniff_image_mime(data)
                return ImageInput(
                    data=data, mime_type=mime,
                    name=name or Path(urlparse(current).path).name or None,
                )

        raise HTTPException(
            status_code=400,
            detail=(f"Image {index}: more than {IMAGE_FETCH_MAX_REDIRECTS} redirects"
                    f" starting at {url}."),
        )
    except httpx.HTTPError as err:
        raise HTTPException(
            status_code=400,
            detail=f"Image {index}: could not fetch {current} ({type(err).__name__}: {err}).",
        ) from err
    finally:
        if owned:
            client.close()


def normalize_attachments(attachments: list[ImageAttachment],
                          client: httpx.Client | None = None,
                          resolve: HostResolver | None = None) -> list[ImageInput]:
    """Both posted forms become bytes here, before any provider client is built.

    `client` and `resolve` exist so the tests can supply an httpx.MockTransport and fixed
    address answers; production passes neither and the real ones are used."""
    if not attachments:
        return []
    if len(attachments) > MAX_INPUT_IMAGES:
        raise HTTPException(
            status_code=400,
            detail=f"At most {MAX_INPUT_IMAGES} images per request; got {len(attachments)}.",
        )

    resolved: list[ImageInput] = []
    for index, item in enumerate(attachments):
        has_data, has_url = bool(item.data_url), bool(item.url)
        if has_data == has_url:
            raise HTTPException(
                status_code=400,
                detail=f"Image {index} needs exactly one of 'data_url' or 'url'.",
            )
        resolved.append(
            _decode_data_url(index, item.data_url or "", item.name) if has_data
            else _fetch_image(index, item.url or "", item.name, client, resolve)
        )

    total = 0
    for index, image in enumerate(resolved):
        if len(image.data) > MAX_IMAGE_BYTES:
            raise HTTPException(
                status_code=400,
                detail=(f"Image {index} is {len(image.data)} bytes, over the per-image"
                        f" limit of {MAX_IMAGE_BYTES}."),
            )
        total += len(image.data)
    if total > MAX_TOTAL_IMAGE_BYTES:
        raise HTTPException(
            status_code=400,
            detail=(f"Images total {total} bytes, over the request limit of"
                    f" {MAX_TOTAL_IMAGE_BYTES}."),
        )

    try:
        validate_images(resolved)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    return resolved


def media_placeholder(modality: str, prompt: str,
                      images: list[ImageInput] | None = None) -> str:
    """What a media generation leaves in the conversation. The payload itself can't go
    there -- a data URL can't be replayed to a text model and would swamp localStorage
    (one generated PNG measured 1.68 MB) -- so the transcript records that it happened.
    Formatted here rather than in JS so there is one definition of it.

    Reference images are named for the same reason the prompt is: without them the turn
    claims the image came from the prompt alone, which is the wrong thing to read back
    when you are working out why two generations differ."""
    trimmed = prompt.strip()
    if len(trimmed) > MEDIA_PLACEHOLDER_PROMPT_CHARS:
        trimmed = trimmed[:MEDIA_PLACEHOLDER_PROMPT_CHARS] + "…"
    kind = "image" if modality == IMAGE else "audio"
    entry = f'[{kind} generated: "{trimmed}"]'
    if images:
        named = [image.name for image in images if image.name]
        detail = ", ".join(named) if named else f"{len(images)} image(s)"
        entry += f"\n[reference: {detail}]"
    return entry


def attachment_placeholder(prompt: str, images: list[ImageInput]) -> str:
    """What an attached image leaves in the transcript.

    Images never enter conversation history -- a photo is 1-3 MB as base64 and
    localStorage caps around 5 MB -- so the turn records that one was there without
    carrying it. Formatted here beside media_placeholder() for the same reason that one
    is: a single definition, rather than a Python one and a drifting copy in JS."""
    if not images:
        return prompt
    named = [image.name for image in images if image.name]
    if named:
        return f"{prompt}\n[image attached: {', '.join(named)}]"
    if len(images) == 1:
        return f"{prompt}\n[image attached]"
    return f"{prompt}\n[{len(images)} images attached]"


def estimate_cost(model_info: ModelInfo, prompt_tokens: int, completion_tokens: int) -> float:
    """Rates come from the catalog and are None when a model is unpriced -- which reads as
    $0 rather than a fabricated number."""
    rate_in = model_info.cost_per_1k_input or 0.0
    rate_out = model_info.cost_per_1k_output or 0.0
    return round((prompt_tokens / 1000.0 * rate_in) + (completion_tokens / 1000.0 * rate_out), 6)


# Services whose API has no sampling parameters at all, so temperature and top_p are
# accepted and ignored rather than honoured. Anthropic withdrew temperature, top_p and
# top_k from the Messages API; the client stopped sending them, which is correct but
# invisible -- a slider that still looks live is a lie about what the request contains.
# This is a UI-presentation list, which is why it lives here and not in the library.
SAMPLING_BLIND_SERVICES = frozenset({"anthropic"})


def honors_sampling(model: ModelInfo) -> bool:
    """Whether temperature and top_p reach the provider for this model.

    Two ways to lose them: the service has no such parameters (above), or the catalog
    flags this particular model as rejecting them. Note the explicit `is not False` --
    supports_sampling is None for most entries, which means "no reason to think not".
    """
    try:
        service = resolve_service(model.service)
    except ValueError:
        return model.supports_sampling is not False
    if service in SAMPLING_BLIND_SERVICES:
        return False
    return model.supports_sampling is not False


@app.get("/api/models")
async def get_models():
    """The models the UI can offer, straight from the catalog.

    Each entry carries a computed `honors_sampling` so the sidebar can disable the
    sliders that would have no effect. Computed here because it depends on the provider
    registry, which the browser has no view of.
    """
    return {
        "models": [
            {**asdict(model), "honors_sampling": honors_sampling(model)}
            for model in load_models()
        ]
    }


@app.get("/api/config")
async def get_config():
    """Which credentials the server already has. Booleans only -- a key is never sent to
    the browser.

    Derived from the registry rather than a hand-written list, so a provider added to the
    library shows up here without an edit. This reports what is SET, which is not the same
    question as available_services() answers for the library: a service with no credential
    still appears in the dropdown, because hiding a model because a key is missing is how
    you get "why is my model gone?"."""
    def has_key(service: str) -> bool:
        return any(os.getenv(name, "").strip() for name in provider_spec(service).key_env)

    local_url = next(
        (value for name in provider_spec("ollama-local").base_url_env
         if (value := os.getenv(name, "").strip())),
        "",
    )
    env_status: dict[str, bool | str] = {
        service: has_key(service) for service in known_services()
    }
    # The local Ollama box takes an endpoint, not a credential, so it is reported as the
    # URL itself -- the sidebar prefills its field from this.
    env_status["ollama_url"] = local_url
    return {"env_status": env_status}


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    return FileResponse(STATIC_DIR / "index.html")


# ------------------------------- trace review -------------------------------- #
# Reading traces is deliberately independent of whether THIS server is recording them:
# the point is to review calls made earlier, possibly by a script or a previous process.
def read_trace_records() -> tuple[list[dict[str, Any]], int]:
    """Parse the trace file into records, plus a count of the lines that would not parse.

    A killed process leaves a half-written last line, and a JSONL file is exactly the
    format where that costs you nothing if the reader steps over it -- so a bad line is
    counted and skipped rather than raised. The count is surfaced instead of swallowed,
    because silently reporting 3 of 4 calls is worse than saying one line was lost."""
    path = trace_file_path()
    records: list[dict[str, Any]] = []
    skipped = 0
    if not path.exists():
        return records, skipped
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
                # Valid JSON that isn't an object can't be a call record either.
                if isinstance(record, dict):
                    records.append(record)
                else:
                    skipped += 1
    except OSError as err:
        logger.error("Could not read traces from %s: %s", path, err)
    return records, skipped


def summarise_runs(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse call records into one row per run, newest run first.

    This is the scanning view: enough to spot the run worth opening (when, how many
    calls, which models, did anything fail) without loading every prompt."""
    runs: dict[str, dict[str, Any]] = {}
    for record in records:
        run_id = str(record.get("run_id", "unknown"))
        run = runs.get(run_id)
        if run is None:
            run = runs[run_id] = {
                "run_id": run_id, "started": None, "ended": None, "calls": 0,
                "services": set(), "models": set(), "modalities": set(),
                "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                "errors": 0, "artifacts": 0,
            }
        run["calls"] += 1
        for field, collection in (("service", "services"), ("model", "models"),
                                  ("modality", "modalities")):
            value = record.get(field)
            if value:
                run[collection].add(str(value))

        ts = record.get("ts")
        if ts:
            run["started"] = ts if run["started"] is None else min(run["started"], ts)
            run["ended"] = ts if run["ended"] is None else max(run["ended"], ts)

        # A failed call records no usage and a media call records nulls, so neither a
        # missing key nor an explicit None may be allowed to poison the total.
        usage = record.get("usage") or {}
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            run[field] += usage.get(field) or 0

        if record.get("error"):
            run["errors"] += 1
        response = record.get("response") or {}
        if isinstance(response, dict) and "artifact" in response:
            run["artifacts"] += 1

    summaries = []
    for run in runs.values():
        summaries.append({**run, "services": sorted(run["services"]),
                          "models": sorted(run["models"]),
                          "modalities": sorted(run["modalities"])})
    # Timestamps are UTC ISO-8601 with a fixed shape, so lexical order is chronological.
    summaries.sort(key=lambda run: run["started"] or "", reverse=True)
    return summaries


@app.get("/api/traces")
def list_traces():
    """Every recorded run, newest first. Plain `def`: reading the file blocks."""
    records, skipped = read_trace_records()
    return {
        "enabled": TRACE is not None,       # is this server recording NEW calls?
        "trace_file": str(trace_file_path()),
        "skipped_lines": skipped,
        "runs": summarise_runs(records),
    }


@app.get("/api/traces/{run_id}")
def get_trace_run(run_id: str):
    """One run's calls, in order and untruncated -- a trimmed trace cannot reproduce the
    call, which is the whole reason for keeping one."""
    records, skipped = read_trace_records()
    matching = [r for r in records if str(r.get("run_id", "")) == run_id]
    if not matching:
        raise HTTPException(status_code=404,
                            detail=f"No trace run '{run_id}' in {trace_file_path()}")
    matching.sort(key=lambda record: record.get("seq") or 0)
    return {"run_id": run_id, "calls": len(matching), "skipped_lines": skipped,
            "records": matching}


@app.post("/api/generate")
def generate(req: GenerateRequest):
    # Deliberately NOT async: every provider SDK here is blocking, and an image or audio
    # generation runs for tens of seconds. Declared `async def`, that would block the event
    # loop and stall every other request (including /api/models) for the whole call.
    # FastAPI runs a plain `def` endpoint in a threadpool instead, which is what keeps the
    # UI responsive during long generations.
    start_time = time.perf_counter()

    # 1. Resolve the model entry -> service. The catalog is descriptive to the library, but
    #    the workbench only ever offers listed models, so an unlisted id is a stale page or
    #    a hand-built request -- and has no service to dispatch to.
    model_info = load_models().get(req.model)
    if model_info is None:
        raise HTTPException(
            status_code=404,
            detail=(f"Unknown model id {req.model!r}. Add it to your model catalog"
                    " (see CORBELITY_MODEL_CATALOG) first."),
        )

    try:
        service = resolve_service(model_info.service)
        available = supported_modalities(service)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err

    modality = MODALITY_ALIASES.get(req.modality.strip().lower(), req.modality.strip().lower())
    if modality not in available:
        # Checked before the client is built so this reads as a routing problem rather
        # than surfacing as a credential or provider error.
        raise HTTPException(
            status_code=400,
            detail=(
                f"Model {req.model!r} is served by {service!r}, which cannot produce "
                f"{modality!r} output (supports: {', '.join(sorted(available))})."
            ),
        )

    # Validated here, before a client is built, for the same reason the modality check is:
    # a malformed conversation should not need working credentials to diagnose. Only for
    # text, though -- history is validated because it gets SENT, and a media call ignores
    # it, so a broken conversation must not block an unrelated image.
    history = [message.model_dump() for message in req.history] if modality == TEXT else []
    try:
        validate_history(history)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err

    # Both checks run before a client is built, for the same reason the modality check
    # does: an impossible combination should read as a routing problem, and you should not
    # pay for a call to discover it.
    # Images are input in two different senses, and the catalog's accepts_images covers
    # both: a text model READS them, and an image model takes REFERENCE images to
    # condition what it generates -- a character sheet, a set, a prop, so a face or a
    # place survives between generations. Speech takes neither.
    if req.images:
        if modality == SOUND:
            raise HTTPException(
                status_code=400,
                detail=("Speech generation takes no image input;"
                        f" {req.model!r} produces audio from text alone."),
            )
        if not model_info.accepts_images:
            raise HTTPException(
                status_code=400,
                detail=(f"Model {req.model!r} is not registered as accepting image input."
                        ' Set "accepts_images": true in your model catalog if it does.'),
            )
    pictures = normalize_attachments(req.images)

    # Descriptors, never base64 -- the Request Payload drawer exists to be read.
    current_turn: dict[str, Any] = {"role": "user", "content": req.prompt}
    if pictures:
        current_turn["images"] = [
            {"name": image.name, "mime_type": image.mime_type, "bytes": len(image.data)}
            for image in pictures
        ]

    context_payload: dict[str, Any] = {
        "model": req.model,
        "service": service,
        "modality": modality,
        "parameters": {
            "temperature": req.temperature,
            "top_p": req.top_p,
            "max_tokens": req.max_tokens,
        },
        "credential_source": {
            name: key_origin(name, override_for(req, name, UI_KEY_FIELDS))
            for name in known_services()
        },
        # The full list actually sent, so the payload drawer stays truthful once a
        # conversation is under way. Media calls send no history (see below).
        "messages": [
            {"role": "system", "content": req.system_prompt},
            *(history if modality == TEXT else []),
            current_turn,
        ],
    }

    # 2. Build the provider client and dispatch on modality. The client resolves the
    #    credential itself: a UI override wins, then each environment variable the
    #    provider's spec names, in order.
    try:
        client = make_model_client(
            service,
            model=req.model,
            temperature=req.temperature,
            top_p=req.top_p,
            max_tokens=req.max_tokens,
            api_key=override_for(req, service, UI_KEY_FIELDS),
            base_url=override_for(req, service, UI_URL_FIELDS),
            trace=TRACE,
        )

        if modality == TEXT:
            content = client.complete(req.system_prompt or "", req.prompt,
                                      history=history, images=pictures)
            context_entry = {"role": "assistant", "content": content}
        elif modality == IMAGE:
            # Image generation is single-shot -- history has no meaning for it -- but it
            # does take reference images, which is why this is no longer folded in with
            # speech below.
            content = data_url(client.generate_image(req.prompt, images=pictures))
            context_entry = {"role": "assistant",
                             "content": media_placeholder(modality, req.prompt, pictures)}
        else:
            content = data_url(client.generate_speech(req.prompt))
            context_entry = {"role": "assistant",
                             "content": media_placeholder(modality, req.prompt)}

    except (UnsupportedModalityError, UnsupportedImageInputError, TooManyImagesError) as err:
        # Capability mismatches, all of them the caller's to fix: the service cannot
        # produce this modality, cannot take reference images at all, or was given more
        # than its published limit.
        raise HTTPException(status_code=400, detail=str(err)) from err
    except ValueError as err:
        # Missing credential, missing endpoint, unknown service -- all ValueErrors, and all
        # things the caller can fix.
        raise HTTPException(status_code=400, detail=str(err)) from err
    except Exception as err:
        logger.exception("Generation failed for %s via %s", req.model, service)
        raise HTTPException(status_code=500, detail=f"{type(err).__name__}: {err}") from err

    time_taken = round(time.perf_counter() - start_time, 3)

    # 3. Real telemetry from the provider. Providers that report nothing stay at 0 rather
    #    than being back-filled with a word count pretending to be a token count.
    prompt_tokens = client.prompt_tokens or 0
    completion_tokens = client.completion_tokens or 0
    total_tokens = client.total_tokens or (prompt_tokens + completion_tokens)
    context_payload["finish_reason"] = client.finish_reason

    return {
        "status": "success",
        "result_type": modality,
        "content": content,
        # The turn the client should append for the user side. Supplied by the server so
        # the attachment placeholder has one definition (see attachment_placeholder).
        "user_context_entry": {"role": "user",
                               "content": attachment_placeholder(req.prompt, pictures)},
        # The turn the client should append. Only ever sent on a 200, so a failed call
        # never enters the transcript.
        "context_entry": context_entry,
        "metrics": {
            "time_seconds": time_taken,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "estimated_cost_usd": estimate_cost(model_info, prompt_tokens, completion_tokens),
        },
        "context_payload": context_payload,
    }
