"""/api/generate with images attached.

The provider is stubbed out entirely -- what is under test is routing, the two gates, and
what the endpoint hands back to the browser.
"""
import base64

import pytest
from corbelity.model_client import MediaResult
from fastapi.testclient import TestClient

from corbelity.workbench import app as workbench

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")

VISION_MODEL = "openai/gpt-4o"            # accepts_images: true in the catalog
TEXT_ONLY_MODEL = "deepseek/deepseek-r1"  # accepts_images: false
REFERENCE_MODEL = "gpt-image-2.5-flare"   # image generation that takes reference images
SOUND_MODEL = "gpt-4o-mini-tts"


class _StubClient:
    """Records what complete()/generate_image() was handed, so routing can be asserted
    without a network."""
    prompt_tokens = completion_tokens = total_tokens = 0
    finish_reason = "stop"
    last_images = None
    last_reference_images = None

    def complete(self, system, user, history=None, images=None):
        type(self).last_images = images
        return "ok"

    def generate_image(self, prompt, images=None):
        type(self).last_reference_images = images
        return MediaResult(data=b"\x89PNG\r\n", mime_type="image/png")

    def generate_speech(self, text):
        return MediaResult(data=b"RIFFxxxx", mime_type="audio/wav")


@pytest.fixture
def client(monkeypatch):
    _StubClient.last_images = None
    _StubClient.last_reference_images = None
    calls = {"factory": 0}

    def counting_factory(service, **kwargs):
        calls["factory"] += 1
        return _StubClient()

    monkeypatch.setattr(workbench, "make_model_client", counting_factory)
    test_client = TestClient(workbench.app)
    test_client.factory_calls = calls
    return test_client


def post(client, **overrides):
    body = {"model": VISION_MODEL, "prompt": "What is this?", "modality": "text"}
    body.update(overrides)
    return client.post("/api/generate", json=body)


# ------------------------------- happy path --------------------------------- #
def test_an_attached_image_reaches_the_client_as_bytes(client):
    response = post(client, images=[{"data_url": PNG_DATA_URL, "name": "chart.png"}])
    assert response.status_code == 200
    assert [i.data for i in _StubClient.last_images] == [PNG]


def test_a_request_without_images_passes_an_empty_list(client):
    assert post(client).status_code == 200
    assert _StubClient.last_images == []


def test_the_payload_drawer_shows_descriptors_not_base64(client):
    """The payload drawer exists to be read; a megabyte of base64 destroys that."""
    response = post(client, images=[{"data_url": PNG_DATA_URL, "name": "chart.png"}])
    turn = response.json()["context_payload"]["messages"][-1]
    assert turn["images"] == [{"name": "chart.png", "mime_type": "image/png", "bytes": len(PNG)}]
    assert "base64" not in str(turn)


# ------------------------------ transcript ---------------------------------- #
def test_the_user_turn_records_that_an_image_was_attached(client):
    response = post(client, images=[{"data_url": PNG_DATA_URL, "name": "chart.png"}])
    entry = response.json()["user_context_entry"]
    assert entry["role"] == "user"
    assert entry["content"] == "What is this?\n[image attached: chart.png]"


def test_several_attachments_are_listed(client):
    response = post(client, images=[{"data_url": PNG_DATA_URL, "name": "a.png"},
                                    {"data_url": PNG_DATA_URL, "name": "b.png"}])
    assert response.json()["user_context_entry"]["content"].endswith(
        "[image attached: a.png, b.png]")


def test_an_unnamed_attachment_still_leaves_a_marker(client):
    response = post(client, images=[{"data_url": PNG_DATA_URL}])
    assert response.json()["user_context_entry"]["content"].endswith("[image attached]")


def test_several_unnamed_attachments_are_counted(client):
    response = post(client, images=[{"data_url": PNG_DATA_URL}, {"data_url": PNG_DATA_URL}])
    assert response.json()["user_context_entry"]["content"].endswith("[2 images attached]")


def test_a_prompt_without_images_is_its_own_user_turn(client):
    """The field is always present, so the browser can use it unconditionally."""
    assert post(client).json()["user_context_entry"] == {
        "role": "user", "content": "What is this?"}


# --------------------------------- gates ------------------------------------ #
def test_a_model_not_catalogued_for_images_is_refused(client):
    response = post(client, model=TEXT_ONLY_MODEL, images=[{"data_url": PNG_DATA_URL}])
    assert response.status_code == 400
    assert "accepts_images" in response.json()["detail"]


def test_a_refused_request_never_builds_a_client(client):
    """Both gates run before make_model_client, so a bad combination costs no credential
    and no network round trip."""
    post(client, model=TEXT_ONLY_MODEL, images=[{"data_url": PNG_DATA_URL}])
    assert _StubClient.last_images is None
    assert client.factory_calls["factory"] == 0


# ------------------------- reference images --------------------------------- #
# An image-generation model takes reference images to condition its output -- a character
# sheet, a set, a prop -- so a face or a place survives between generations. This is a
# different sense of "image input" from a text model reading a picture, and the endpoint
# used to refuse it outright.
def test_reference_images_reach_an_image_model(client):
    response = client.post("/api/generate", json={
        "model": REFERENCE_MODEL, "prompt": "the same character, now seated",
        "modality": "image", "images": [{"data_url": PNG_DATA_URL, "name": "ref.png"}]})
    assert response.status_code == 200
    assert [i.data for i in _StubClient.last_reference_images] == [PNG]


def test_an_image_generation_without_references_still_works(client):
    """The no-images call must be exactly what it was before references existed."""
    response = client.post("/api/generate", json={
        "model": REFERENCE_MODEL, "prompt": "a cabin", "modality": "image"})
    assert response.status_code == 200
    assert not _StubClient.last_reference_images


def test_the_transcript_names_the_reference_images(client):
    """Without this the turn claims the image came from the prompt alone, which is the
    wrong thing to read back when two generations differ."""
    response = client.post("/api/generate", json={
        "model": REFERENCE_MODEL, "prompt": "seated", "modality": "image",
        "images": [{"data_url": PNG_DATA_URL, "name": "ref.png"}]})
    content = response.json()["context_entry"]["content"]
    assert '[image generated: "seated"]' in content
    assert "[reference: ref.png]" in content
    assert "base64" not in content


def test_speech_still_refuses_image_input(client):
    """Speech takes neither kind of image input."""
    response = client.post("/api/generate", json={
        "model": SOUND_MODEL, "prompt": "boarding call", "modality": "sound",
        "images": [{"data_url": PNG_DATA_URL}]})
    assert response.status_code == 400
    assert "no image input" in response.json()["detail"]
    assert client.factory_calls["factory"] == 0


def test_an_image_model_not_flagged_for_references_is_refused(client):
    """FLUX is text-to-image only; the catalog flag is what separates the two."""
    response = client.post("/api/generate", json={
        "model": "black-forest-labs/FLUX.1-dev", "prompt": "a cabin", "modality": "image",
        "images": [{"data_url": PNG_DATA_URL}]})
    assert response.status_code == 400
    assert "accepts_images" in response.json()["detail"]


def test_an_unknown_model_is_still_a_404(client):
    """The image gates must not shadow the catalog lookup."""
    assert post(client, model="nope/nope", images=[{"data_url": PNG_DATA_URL}]).status_code == 404
