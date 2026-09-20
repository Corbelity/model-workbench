"""Backend helpers and the model catalog itself. Nothing here calls a provider."""
import base64

import pytest
from corbelity.model_client import (
    MediaResult,
    ModelInfo,
    known_services,
    make_trace_logger,
    provider_spec,
    resolve_service,
    supported_modalities,
)
from fastapi.testclient import TestClient

from corbelity.workbench import app as workbench
from corbelity.workbench.app import GenerateRequest


def priced(**rates) -> ModelInfo:
    """A catalog entry carrying only what estimate_cost reads. ModelInfo is kw_only, and
    id/service are required."""
    return ModelInfo(id="vendor/model", service="openrouter", **rates)


# ----------------------------- the catalog --------------------------------- #
def test_the_catalog_loads():
    assert len(workbench.load_models()) > 0


def test_every_catalogued_model_routes_to_a_real_service():
    """The catalog is the UI's source of truth and the only model ids /api/generate will
    accept, so a typo in `service` would surface as a 400 at execution time. provider_spec
    rather than client_class: this needs to fail on an unknown name, not on a provider SDK
    that happens not to be installed."""
    for model in workbench.load_models():
        provider_spec(model.service)   # raises UnknownServiceError if it is not real


def test_every_catalogued_model_declares_a_modality_its_service_supports():
    for model in workbench.load_models():
        assert model.modality in supported_modalities(model.service), (
            f"{model.id} wants {model.modality!r} but {model.service!r} cannot produce it"
        )


def test_the_local_ollama_model_is_catalogued_as_local():
    """'ollama' is Ollama Cloud and 'ollama-local' is your own box -- they take different
    credentials, so mixing them up sends a local model at a cloud endpoint."""
    assert workbench.load_models().get("llama3:latest").service == "ollama-local"


def test_every_service_named_in_the_catalog_is_registered():
    for model in workbench.load_models():
        assert resolve_service(model.service) in known_services()


# --------------------------- key resolution -------------------------------- #
# The client resolves credentials itself; the workbench only decides whether the UI
# supplied a per-session override to hand it.
def test_a_ui_override_is_passed_through():
    req = GenerateRequest(model="m", prompt="p", anthropic_key="ui-value")
    assert workbench.override_for(req, "anthropic", workbench.UI_KEY_FIELDS) == "ui-value"


def test_a_whitespace_only_override_is_treated_as_absent():
    """An empty field means "not set", never "blank key" -- otherwise clearing the box
    would send an empty credential instead of falling back to the environment."""
    req = GenerateRequest(model="m", prompt="p", anthropic_key="   ")
    assert workbench.override_for(req, "anthropic", workbench.UI_KEY_FIELDS) is None


def test_a_service_with_no_ui_field_has_no_override():
    req = GenerateRequest(model="m", prompt="p")
    assert workbench.override_for(req, "ollama", workbench.UI_KEY_FIELDS) is None


@pytest.mark.parametrize(
    "override, env_value, expected",
    [
        ("ui-value", "env-value", "UI override"),
        (None, "env-value", "Set in environment"),
        (None, "", "Missing"),
    ],
)
def test_key_origin_is_reported_for_the_payload_drawer(monkeypatch, override, env_value, expected):
    """The drawer reports where a credential came from, never its value, so a surprising
    result can be traced to the wrong key."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", env_value)
    assert workbench.key_origin("anthropic", override) == expected


def test_a_keyless_service_reports_that_no_key_is_required():
    """ollama-local needs an endpoint, not a credential."""
    assert workbench.key_origin("ollama-local", None) == "Not required"


def test_key_origin_honours_every_name_a_provider_accepts(monkeypatch):
    """HuggingFace reads HF_TOKEN, but two older names are still honoured. The drawer must
    agree with the client about that or it will report 'Missing' for a working key."""
    for name in provider_spec("huggingface").key_env:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(provider_spec("huggingface").key_env[-1], "legacy-value")
    assert workbench.key_origin("huggingface", None) == "Set in environment"


# ------------------------------- payloads ---------------------------------- #
@pytest.mark.parametrize("service, field", [
    ("anthropic", "anthropic_key"),
    ("openai", "openai_key"),
    ("gemini", "gemini_key"),
    ("openrouter", "openrouter_key"),
    ("huggingface", "huggingface_key"),
])
def test_every_keyed_service_has_a_ui_override_field(service, field):
    """A service missing from UI_KEY_FIELDS silently ignores whatever is typed into the
    sidebar and falls back to the environment -- which looks like the override not
    working, with nothing in the logs."""
    assert workbench.UI_KEY_FIELDS[service] == field
    req = GenerateRequest(model="m", prompt="p", **{field: "ui-value"})
    assert workbench.override_for(req, service, workbench.UI_KEY_FIELDS) == "ui-value"


def test_gemini_accepts_either_google_credential(monkeypatch):
    """Gemini reads GEMINI_API_KEY first and GOOGLE_API_KEY second; the badge must agree
    with the client about both."""
    for name in provider_spec("gemini").key_env:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "from-google-name")
    assert workbench.key_origin("gemini", None) == "Set in environment"


def test_media_is_inlined_as_a_data_url():
    url = workbench.data_url(MediaResult(data=b"\x89PNG\r\n", mime_type="image/png"))
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == b"\x89PNG\r\n"


def test_audio_keeps_its_own_mime_type():
    url = workbench.data_url(MediaResult(data=b"RIFFxxxx", mime_type="audio/wav"))
    assert url.startswith("data:audio/wav;base64,")


# --------------------------------- cost ------------------------------------ #
def test_cost_uses_the_catalogue_rates():
    model = priced(cost_per_1k_input=0.003, cost_per_1k_output=0.015)
    assert workbench.estimate_cost(model, 1000, 1000) == pytest.approx(0.018)


def test_an_unpriced_model_costs_zero():
    """A local model must not report a fabricated dollar figure."""
    assert workbench.estimate_cost(priced(), 5000, 5000) == 0.0


def test_null_rates_are_treated_as_zero():
    model = priced(cost_per_1k_input=None, cost_per_1k_output=None)
    assert workbench.estimate_cost(model, 100, 100) == 0.0


def test_input_and_output_are_priced_separately():
    """Output usually costs several times input; one shared rate would quietly under-report."""
    model = priced(cost_per_1k_input=0.001, cost_per_1k_output=0.010)
    assert workbench.estimate_cost(model, 1000, 0) == pytest.approx(0.001)
    assert workbench.estimate_cost(model, 0, 1000) == pytest.approx(0.010)


# ------------------------------- tracing ------------------------------------ #
class _StubClient:
    prompt_tokens = completion_tokens = total_tokens = 0
    finish_reason = "stop"

    def complete(self, system, user, history=None, images=None):
        return "ok"


def test_the_server_runs_trace_logger_is_handed_to_every_client(monkeypatch):
    """One logger per server process, injected into each per-request client, so a
    session's calls share a run_id."""
    captured = {}

    def fake_factory(service, **kwargs):
        captured.update(kwargs)
        return _StubClient()

    sentinel = object()
    monkeypatch.setattr(workbench, "make_model_client", fake_factory)
    monkeypatch.setattr(workbench, "TRACE", sentinel)

    TestClient(workbench.app).post("/api/generate", json={
        "model": "google/gemini-2.5-flash", "prompt": "hi", "modality": "text"})
    assert captured["trace"] is sentinel


def test_tracing_is_off_by_default(monkeypatch):
    """Traces hold full prompts and responses, so an unconfigured server writes none."""
    monkeypatch.delenv("TRACE_ENABLED", raising=False)
    assert make_trace_logger() is None


# ------------------------------ modality ----------------------------------- #
@pytest.mark.parametrize("posted, expected",
                         [("audio", "sound"), ("speech", "sound"), ("img", "image")])
def test_modality_synonyms_are_normalised(posted, expected):
    assert workbench.MODALITY_ALIASES[posted] == expected


# ------------------------- image input capability --------------------------- #
# accepts_images is what /api/generate gates on and what the UI reads to enable the attach
# controls, so a missing or mistyped flag is a silently disabled feature.
def test_every_model_declares_image_capability_as_a_boolean():
    for model in workbench.load_models():
        assert isinstance(model.accepts_images, bool), model.id


def test_the_known_vision_models_accept_images():
    catalog = workbench.load_models()
    for model_id in ("openai/gpt-4o", "google/gemini-2.5-flash", "claude-sonnet-5"):
        assert catalog.get(model_id).accepts_images is True, model_id


def test_models_without_vision_are_not_flagged():
    catalog = workbench.load_models()
    assert catalog.get("deepseek/deepseek-r1").accepts_images is False
    assert catalog.get("llama3:latest").accepts_images is False


def test_only_text_models_are_flagged_for_image_input():
    """Image input is a text-generation feature; flagging a text-to-image model would
    describe a request /api/generate refuses."""
    for model in workbench.load_models():
        if model.accepts_images:
            assert model.modality == "text", model.id


def test_the_cloud_and_local_vision_paths_are_both_reachable():
    """Every provider gained image support; at least one cloud and one local catalog row
    must exist or most of that code can never be exercised by hand."""
    services = {m.service for m in workbench.load_models() if m.accepts_images}
    assert "anthropic" in services
    assert "ollama-local" in services


# --------------------------- media generation ------------------------------- #
# HuggingFace was once the only service that produced images and sound. OpenAI now does
# both natively, so nothing may assume a single media provider.
def test_media_generation_is_not_tied_to_one_provider():
    catalog = workbench.load_models()
    image_services = {m.service for m in catalog if m.modality == "image"}
    sound_services = {m.service for m in catalog if m.modality == "sound"}
    assert {"openai", "huggingface"} <= image_services
    # Sound is OpenAI only in the built-in catalog: the HuggingFace speech model was
    # removed after it proved unreliable on the free Inference endpoint. Asserted rather
    # than left implicit so that reintroducing one is a deliberate change here.
    assert sound_services == {"openai"}


def test_every_generative_modality_has_at_least_one_model():
    """A modality the UI offers as a pill but has no catalogued model for is a dead
    control."""
    modalities = {m.modality for m in workbench.load_models()}
    assert {"text", "image", "sound"} <= modalities


# ------------------------- sampling applicability --------------------------- #
# The sidebar greys the temperature and top_p sliders when they would not reach the
# provider. The server decides that, because it depends on the provider registry.
def test_anthropic_models_do_not_honor_sampling():
    """Anthropic withdrew sampling from the Messages API, so the client never sends it."""
    for model in workbench.load_models():
        if model.service == "anthropic":
            assert workbench.honors_sampling(model) is False, model.id


def test_a_model_flagged_in_the_catalog_does_not_honor_sampling():
    assert workbench.honors_sampling(workbench.load_models().get("gpt-5.6-terra")) is False


def test_an_ordinary_model_honors_sampling():
    """supports_sampling is None for most entries, which means no reason to think not --
    it must not be read as False."""
    entry = workbench.load_models().get("google/gemini-2.5-flash")
    assert entry.supports_sampling is None
    assert workbench.honors_sampling(entry) is True


def test_an_unknown_service_still_answers_rather_than_raising():
    """A hand-written catalog can name a service that does not exist. The dropdown should
    not 500 over it -- the routing error belongs at dispatch, with a readable message."""
    assert workbench.honors_sampling(
        ModelInfo(id="x/y", service="nosuchservice")) is True


def test_the_models_endpoint_publishes_sampling_applicability():
    """The browser has no view of the registry, so the flag has to ride along."""
    body = TestClient(workbench.app).get("/api/models").json()
    by_id = {m["id"]: m for m in body["models"]}
    assert by_id["claude-sonnet-5"]["honors_sampling"] is False
    assert by_id["google/gemini-2.5-flash"]["honors_sampling"] is True


def test_the_models_endpoint_still_carries_the_catalog_fields():
    """honors_sampling is added alongside the entry, not in place of it."""
    body = TestClient(workbench.app).get("/api/models").json()
    entry = next(m for m in body["models"] if m["id"] == "gpt-5.6-terra")
    assert entry["service"] == "openai"
    assert entry["modality"] == "text"
    assert entry["max_tokens_param"] == "max_completion_tokens"


def test_models_that_reject_sampling_are_flagged():
    """A model that 400s on temperature/top_p must carry supports_sampling: false, or the
    UI's sliders make it uncallable -- they always send a value. The flag is the catalog's
    job because the affected set grows on the providers' schedule, not ours.

    Anthropic entries are deliberately NOT here: the Messages API withdrew sampling
    entirely, so that client never sends it and the flag would have nothing to govern."""
    assert workbench.load_models().get("gpt-5.6-terra").supports_sampling is False


def test_anthropic_entries_do_not_carry_a_sampling_flag():
    """The flag is meaningless for this provider. Left on an entry it would read as a
    per-model capability difference that does not exist."""
    for model in workbench.load_models():
        if model.service == "anthropic":
            assert model.supports_sampling is None, model.id


def test_openai_models_needing_the_newer_token_parameter_say_so():
    """Newer OpenAI models reject max_tokens outright and want max_completion_tokens."""
    entry = workbench.load_models().get("gpt-5.6-terra")
    assert entry.max_tokens_param == "max_completion_tokens"


def test_the_new_direct_providers_are_catalogued():
    services = {m.service for m in workbench.load_models()}
    assert {"openai", "gemini"} <= services


def test_openai_offers_all_three_modalities():
    """The spec claims text, image and sound; the catalog has to actually list a model for
    each or the capability is unreachable from the UI."""
    modalities = {m.modality for m in workbench.load_models() if m.service == "openai"}
    assert modalities == {"text", "image", "sound"}


def test_gemini_is_catalogued_as_text_only():
    """Gemini runs through Google's OpenAI-compatibility endpoint, which this library
    supports for text only -- a catalogued image or sound model would be refused at
    dispatch."""
    modalities = {m.modality for m in workbench.load_models() if m.service == "gemini"}
    assert modalities == {"text"}


# ------------------------ service allow-list -------------------------------- #
def test_the_catalog_can_be_narrowed_to_named_services(monkeypatch):
    """CORBELITY_SERVICES narrows what the dropdown offers."""
    monkeypatch.setattr(workbench, "CONFIG",
                        workbench.CONFIG.with_overrides(services=("openai",)))
    assert {m.service for m in workbench.load_models()} == {"openai"}


def test_narrowing_does_not_make_a_filtered_model_uncallable(monkeypatch):
    """The allow-list is presentation only. Hiding a service must not change what a
    request to it does, so a filtered-out model is still routable."""
    monkeypatch.setattr(workbench, "CONFIG",
                        workbench.CONFIG.with_overrides(services=("openai",)))
    assert resolve_service("anthropic") in known_services()


def test_an_unknown_name_in_the_allow_list_is_ignored(monkeypatch):
    """A stale entry should not empty the dropdown."""
    monkeypatch.setattr(workbench, "CONFIG",
                        workbench.CONFIG.with_overrides(services=("openai", "nosuchservice")))
    assert {m.service for m in workbench.load_models()} == {"openai"}
