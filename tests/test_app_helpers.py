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
