"""The /api/generate history contract, with the provider stubbed out.

Covers what the browser and the server have to agree on: history order on the way in, and
the turn the browser is told to append on the way back.
"""
import pytest
from corbelity.model_client import MediaResult
from fastapi.testclient import TestClient

from corbelity.workbench import app as workbench

TEXT_MODEL = "google/gemini-2.5-flash"      # routes to openrouter
IMAGE_MODEL = "black-forest-labs/FLUX.1-dev"
SOUND_MODEL = "gpt-4o-mini-tts"

HISTORY = [
    {"role": "user", "content": "What is a k-d tree?"},
    {"role": "assistant", "content": "A space-partitioning structure."},
]


class FakeClient:
    """Stands in for a built provider client; records what complete() received."""

    prompt_tokens = 5
    completion_tokens = 7
    total_tokens = 12
    finish_reason = "stop"

    def __init__(self):
        self.seen = None

    def complete(self, system, user, history=None, images=None):
        self.seen = {"system": system, "user": user, "history": history}
        return "canned answer"

    def generate_image(self, prompt):
        return MediaResult(data=b"\x89PNG\r\n", mime_type="image/png")

    def generate_speech(self, text):
        return MediaResult(data=b"RIFFxxxx", mime_type="audio/wav")


@pytest.fixture
def stub(monkeypatch):
    """Replaces the factory so no provider is built and no credential is needed."""
    built = {"count": 0, "client": FakeClient()}

    def fake_factory(service, **kwargs):
        built["count"] += 1
        built["service"] = service
        built["kwargs"] = kwargs
        return built["client"]

    monkeypatch.setattr(workbench, "make_model_client", fake_factory)
    return built


@pytest.fixture
def client():
    return TestClient(workbench.app)


def _post(client, **overrides):
    body = {"model": TEXT_MODEL, "prompt": "And then?", "modality": "text"}
    body.update(overrides)
    return client.post("/api/generate", json=body)


def test_history_is_forwarded_to_the_client_in_order(client, stub):
    _post(client, history=HISTORY)
    assert stub["client"].seen["history"] == HISTORY


def test_omitting_history_sends_an_empty_conversation(client, stub):
    _post(client)
    assert stub["client"].seen["history"] == []


def test_the_model_is_routed_to_its_catalogued_service(client, stub):
    """The catalog entry decides the provider; the browser never names one."""
    _post(client)
    assert stub["service"] == "openrouter"


def test_the_payload_drawer_shows_the_full_message_list(client, stub):
    """The drawer claims to show what was sent; with history it must stop showing a
    two-message fiction."""
    r = _post(client, history=HISTORY, system_prompt="Be terse.")
    messages = r.json()["context_payload"]["messages"]
    assert messages == [
        {"role": "system", "content": "Be terse."},
        {"role": "user", "content": "What is a k-d tree?"},
        {"role": "assistant", "content": "A space-partitioning structure."},
        {"role": "user", "content": "And then?"},
    ]


def test_a_text_response_returns_itself_as_the_context_entry(client, stub):
    r = _post(client, history=HISTORY)
    assert r.json()["context_entry"] == {"role": "assistant", "content": "canned answer"}


def test_an_image_response_returns_a_placeholder_not_the_payload(client, stub):
    """A data URL must never enter the transcript -- it cannot be replayed to a text model
    and would blow through localStorage."""
    r = client.post("/api/generate", json={
        "model": IMAGE_MODEL, "prompt": "a cozy cabin", "modality": "image"})
    entry = r.json()["context_entry"]
    assert entry["role"] == "assistant"
    assert entry["content"] == '[image generated: "a cozy cabin"]'
    assert "base64" not in entry["content"]


def test_an_audio_response_returns_a_placeholder(client, stub):
    r = client.post("/api/generate", json={
        "model": SOUND_MODEL, "prompt": "boarding call", "modality": "sound"})
    assert r.json()["context_entry"]["content"] == '[audio generated: "boarding call"]'


def test_a_long_media_prompt_is_truncated_in_the_placeholder(client, stub):
    long_prompt = "x" * 300
    r = client.post("/api/generate", json={
        "model": IMAGE_MODEL, "prompt": long_prompt, "modality": "image"})
    content = r.json()["context_entry"]["content"]
    assert len(content) < 260
    assert "x" * 200 in content
    assert long_prompt not in content


def test_malformed_history_is_rejected_with_400(client, stub):
    r = _post(client, history=[{"role": "assistant", "content": "backwards"}])
    assert r.status_code == 400
    assert "0" in r.json()["detail"]


def test_malformed_history_is_caught_before_a_client_is_built(client, stub):
    """A broken conversation should not require working credentials to diagnose."""
    _post(client, history=[{"role": "assistant", "content": "backwards"}])
    assert stub["count"] == 0


def test_a_trailing_user_turn_is_rejected(client, stub):
    r = _post(client, history=HISTORY + [{"role": "user", "content": "orphaned"}])
    assert r.status_code == 400
    assert "delete" in r.json()["detail"].lower()


def test_an_unknown_role_is_rejected_by_schema_validation(client, stub):
    r = _post(client, history=[{"role": "wizard", "content": "hi"}])
    assert r.status_code == 422


def test_a_system_role_in_history_is_rejected(client, stub):
    """The system prompt has its own field; allowing it here would create a second place
    that sets it."""
    r = _post(client, history=[{"role": "system", "content": "you are helpful"}])
    assert r.status_code == 422


def test_malformed_history_does_not_block_media_generation(client, stub):
    """History is validated because it gets sent, not on principle. A broken conversation
    shouldn't stop you generating an unrelated image."""
    r = client.post("/api/generate", json={
        "model": IMAGE_MODEL, "prompt": "a cabin", "modality": "image",
        "history": [{"role": "assistant", "content": "backwards"}]})
    assert r.status_code == 200


def test_history_is_not_sent_to_media_generation(client, stub):
    """Image and audio generation is single-shot; history has no meaning there."""
    r = client.post("/api/generate", json={
        "model": IMAGE_MODEL, "prompt": "a cabin", "modality": "image", "history": HISTORY})
    assert r.status_code == 200
    assert stub["client"].seen is None


def test_a_modality_the_service_cannot_produce_is_refused(client, stub):
    """OpenRouter is text-only here, so asking it for an image is a routing error -- and
    must read as one rather than as a credential or provider failure."""
    r = _post(client, modality="image")
    assert r.status_code == 400
    assert stub["count"] == 0


def test_an_unknown_model_is_a_404(client, stub):
    r = _post(client, model="nope/nope")
    assert r.status_code == 404
