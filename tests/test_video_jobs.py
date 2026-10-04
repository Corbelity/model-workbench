"""The video job endpoints and the store behind them.

Nothing here reaches a provider. Two different kinds of test live in this file, and the
split is the point:

  * The dry-run checks call /api/video/check, which runs the LIBRARY's real
    resolve_video_request() -- no client, no credential, no network. So these assert
    against the actual constraint rules in the shipped catalog rather than against a
    re-statement of them, which is the whole reason that endpoint exists.
  * The lifecycle tests replace the provider client with a scripted stand-in, because what
    is being tested there is this application's bookkeeping -- that a submission is
    recorded before the response goes out, that a finished job stops being polled, that a
    restart finds the job again -- none of which depends on a real provider.
"""
import time

import pytest
from corbelity.model_client import (
    VIDEO_ROLES,
    MediaResult,
    VideoJobNotFoundError,
    VideoJobRef,
    VideoOptions,
    VideoStatus,
)
from fastapi.testclient import TestClient

from corbelity.workbench import app as workbench
from corbelity.workbench import videostore

# The two video models in the built-in catalog. Named rather than discovered because the
# constraint assertions below are about these entries specifically; a test that found
# "some video model" would pass against a catalog that had lost the rows being checked.
VEO = "veo-3.1-generate-preview"
VEO_LITE = "veo-3.1-lite-generate-preview"

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"corbelity-test-clip" * 4


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """Point the job store at a fresh directory per test.

    _schema_ready is reset as well: it caches which directory this process has already
    created the table in, and a leftover value from another test would otherwise be
    compared against a path that no longer exists."""
    directory = tmp_path / "video"
    monkeypatch.setenv(videostore.VIDEO_DIR_ENV, str(directory))
    monkeypatch.setattr(videostore, "_schema_ready", None)
    videostore.init_db()
    return directory


@pytest.fixture
def client():
    return TestClient(workbench.app)


# --------------------------------------------------------------------------- #
# the provider stand-in
# --------------------------------------------------------------------------- #
class FakeJob:
    """What submit_video() and resume_video() hand back.

    Mirrors the parts of the library's VideoJob this application actually uses: to_ref(),
    poll() and result(). Deliberately NOT a subclass -- the real one owns a lock, a clock
    and trace records, and inheriting it would mean testing against a half-initialised
    version of the thing under test."""

    def __init__(self, provider, ref):
        self._provider = provider
        self._ref = ref

    def to_ref(self):
        return self._ref

    def poll(self):
        self._provider.polls += 1
        if self._provider.poll_raises is not None:
            raise self._provider.poll_raises
        # The last scripted state repeats, so a test can poll once more than it scripted
        # and get the terminal answer again rather than an IndexError.
        states = self._provider.states
        state = states.pop(0) if len(states) > 1 else states[0]
        return VideoStatus(
            state=state,
            elapsed_s=1.5,
            progress=0.5 if state == "running" else None,
            error="the provider gave up" if state == "failed" else None,
            filtered_reasons=("safety",) if state == "filtered" else (),
        )

    def result(self):
        return MediaResult(data=MP4, mime_type="video/mp4",
                           source_uri="files/generated-clip")


class FakeProvider:
    """A scripted provider client, installed in place of make_model_client()."""

    def __init__(self, *, states=("succeeded",), operation="models/veo/operations/abc123",
                 submit_raises=None, poll_raises=None):
        self.states = list(states)
        self.operation = operation
        self.submit_raises = submit_raises
        self.poll_raises = poll_raises
        self.submitted = []
        self.resumed = []
        self.polls = 0
        self.built_with = []

    def submit_video(self, prompt, *, first_frame=None, last_frame=None, references=(),
                     **settings):
        self.submitted.append({
            "prompt": prompt,
            "roles": [name for name, value in (("first_frame", first_frame),
                                               ("last_frame", last_frame),
                                               ("references", references)) if value],
            "settings": settings,
        })
        if self.submit_raises is not None:
            raise self.submit_raises
        return FakeJob(self, VideoJobRef(
            service="gemini-native", model="veo-3.1-generate-preview",
            operation=self.operation, submitted_at=time.time(), requested=settings,
        ))

    def resume_video(self, ref):
        self.resumed.append(ref)
        return FakeJob(self, ref)


@pytest.fixture
def provider(monkeypatch):
    """Install a FakeProvider for every client the endpoints build.

    Patched at workbench.make_model_client, which is the name the endpoints call, so the
    credential resolution the real one does is bypassed -- these tests are about the job
    bookkeeping, and requiring a Gemini key to assert that a row was written would make
    the suite depend on the developer's .env."""
    fake = FakeProvider()

    def build(service, **kwargs):
        fake.built_with.append({"service": service, **kwargs})
        return fake

    monkeypatch.setattr(workbench, "make_model_client", build)
    return fake


def submit(client, **overrides):
    body = {"model": VEO, "prompt": "a corbel bracket, slowly rotating"}
    body.update(overrides)
    response = client.post("/api/video/submit", json=body)
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# the dry run: real library rules, no client and no credential
# --------------------------------------------------------------------------- #
def test_a_plain_text_to_video_request_is_accepted(client):
    body = client.post("/api/video/check", json={"model": VEO, "roles": []}).json()
    assert body["ok"] is True
    assert body["capabilities"]["aspect_ratios"] == ["16:9", "9:16"]


def test_the_check_never_claims_to_have_judged_the_images(client):
    """Both answers carry images_checked=False. The check is asked with role names and a
    placeholder frame, so a green answer must not be read as "your frame is fine" -- an
    unsupported container or an oversized file is refused at submission."""
    accepted = client.post("/api/video/check",
                           json={"model": VEO, "roles": ["first_frame"]}).json()
    refused = client.post("/api/video/check",
                          json={"model": VEO, "roles": ["last_frame"]}).json()
    assert accepted["ok"] is True and refused["ok"] is False
    assert accepted["images_checked"] is False
    assert refused["images_checked"] is False


def test_a_refused_combination_is_an_answer_not_an_http_error(client):
    """200 with ok=false, deliberately. The UI re-asks this on every change to a control,
    so a 400 per refusal would fill the console with red and hide a real fault. A last
    frame with no first frame is the catalog's own rule, from its constraint table."""
    response = client.post("/api/video/check", json={"model": VEO, "roles": ["last_frame"]})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "first_frame" in body["detail"]


def test_a_forced_setting_comes_back_resolved(client):
    """The catalog says 1080p forces an 8-second clip. Asking for 1080p alone is accepted
    and the duration is reported, which is what lets the UI show the 8 seconds rather than
    leaving the field blank and the user to discover it from the bill."""
    body = client.post("/api/video/check",
                       json={"model": VEO, "resolution": "1080p"}).json()
    assert body["ok"] is True
    assert body["resolved"] == {"resolution": "1080p", "duration_seconds": 8}


def test_a_forced_setting_contradicted_is_refused(client):
    body = client.post("/api/video/check", json={
        "model": VEO, "resolution": "1080p", "duration_seconds": 4}).json()
    assert body["ok"] is False
    assert "duration_seconds" in body["detail"]


def test_a_resolution_the_model_does_not_offer_is_refused(client):
    """4k is in the catalog for Veo 3.1 and not for Veo 3.1 Lite, so the same request gets
    opposite answers -- which is the per-model enforcement working rather than a blanket
    list of resolutions somewhere."""
    full = client.post("/api/video/check",
                       json={"model": VEO, "resolution": "4k"}).json()
    lite = client.post("/api/video/check",
                       json={"model": VEO_LITE, "resolution": "4k"}).json()
    assert full["ok"] is True
    assert lite["ok"] is False
    assert "4k" in lite["detail"]


def test_silent_output_is_refused_rather_than_ignored(client):
    """Asked for and refused twice over: gemini-native does not carry generate_audio at
    all (it is a Vertex-only parameter), and both Veo catalog entries are audio: "always".
    Which layer answers first matters less than that one does -- a setting accepted and
    dropped would leave someone believing a clip is silent."""
    body = client.post("/api/video/check",
                       json={"model": VEO, "generate_audio": False}).json()
    assert body["ok"] is False
    assert "audio" in body["detail"].lower()


def test_references_are_refused_by_the_shipped_catalog(client):
    """Phase one: both Veo entries list first_frame and last_frame as their inputs, so
    references are refused even though the same entries carry max_references and
    reference constraint rows ready for later. The workbench accepts the role and lets the
    catalog decide, which is why this test is about the ANSWER and not about a block in
    this application -- when model-client enables references, it is this test that should
    fail and be updated."""
    body = client.post("/api/video/check", json={"model": VEO, "roles": ["references"]}).json()
    assert body["ok"] is False
    assert "references" in body["detail"]


def test_a_misspelled_role_is_a_client_error_rather_than_an_answer(client):
    """The difference matters: roles arrive as NAMES, so a typo is simply an absent role,
    and the check would otherwise answer truthfully about a request nobody asked about."""
    response = client.post("/api/video/check",
                           json={"model": VEO, "roles": ["frist_frame"]})
    assert response.status_code == 400
    assert "frist_frame" in response.json()["detail"]


def test_extend_is_not_offered_by_this_interface(client):
    """The library knows the role; this panel has no control for it, because it needs a
    handle to a clip the service itself generated rather than an upload. Refused by name
    here rather than passed down to fail less clearly."""
    assert "extend" in VIDEO_ROLES
    assert "extend" not in workbench.VIDEO_INPUT_ROLES
    response = client.post("/api/video/check", json={"model": VEO, "roles": ["extend"]})
    assert response.status_code == 400


def test_a_text_model_cannot_be_checked_as_video(client):
    text_model = next(m.id for m in workbench.load_models() if m.modality == "text")
    response = client.post("/api/video/check", json={"model": text_model})
    assert response.status_code == 400
    assert "not video" in response.json()["detail"]


def test_an_unknown_model_is_a_404(client):
    assert client.post("/api/video/check", json={"model": "no-such-model"}).status_code == 404


def test_the_dry_run_needs_no_credential(client, monkeypatch):
    """The point of resolve_video_request(): the UI can ask before anything is configured.
    Both of Gemini's credential names are cleared, so this would fail if the endpoint were
    building a client to answer."""
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert client.post("/api/video/check", json={"model": VEO}).json()["ok"] is True


# --------------------------------------------------------------------------- #
# the surfaces this application has to keep in step with the library
# --------------------------------------------------------------------------- #
def test_the_settings_model_matches_the_library_options():
    """VideoSettings.options() is a straight **model_dump(), so a field this UI gained and
    VideoOptions did not would raise at request time rather than here."""
    assert set(workbench.VideoSettings.model_fields) == set(VideoOptions.__dataclass_fields__)


def test_the_role_names_match_the_library():
    """corbelity.model_client exports VIDEO_ROLES but not the individual names, so they
    are spelled out in app.py. This is what catches a rename there."""
    assert set(workbench.VIDEO_INPUT_ROLES) < set(VIDEO_ROLES)
    offered = tuple(role for role in VIDEO_ROLES if role != "extend")
    assert offered == workbench.VIDEO_INPUT_ROLES


def test_the_job_states_match_the_library():
    """No state constants are exported either -- the library's public surface for state is
    VideoStatus and its `done` property -- so each spelled-out value is round-tripped
    through it. A renamed state makes `done` disagree and fails here."""
    assert VideoStatus(state=workbench.VIDEO_RUNNING, elapsed_s=None).done is False
    for state in workbench.VIDEO_TERMINAL:
        assert VideoStatus(state=state, elapsed_s=None).done is True


def test_every_mapped_ui_field_exists_on_the_credentials_model():
    """override_for() reads a field off the request by the name in UI_KEY_FIELDS, with
    getattr. A name in the map with no field behind it resolves to None, so the key typed
    into the sidebar would be silently dropped -- which looks like a bad key, not a bug."""
    fields = set(workbench.Credentials.model_fields)
    for mapping in (workbench.UI_KEY_FIELDS, workbench.UI_URL_FIELDS):
        for service, field in mapping.items():
            assert field in fields, f"{service} maps to {field!r}, which no request carries"


def test_the_catalog_capabilities_reach_the_browser(client):
    """ModelInfo.video is a dataclass, so the asdict() already in /api/models recurses into
    it -- capabilities need no second serializer. Asserted because that is an easy thing to
    duplicate by hand later, and a hand copy would be the one that goes stale."""
    models = {m["id"]: m for m in client.get("/api/models").json()["models"]}
    video = models[VEO]["video"]
    assert video["durations"] == [4, 6, 8]
    assert {"when": "last_frame", "require": None, "require_input": "first_frame",
            "exclude_input": []} in video["constraints"]


# --------------------------------------------------------------------------- #
# the lifecycle
# --------------------------------------------------------------------------- #
def test_submitting_records_the_job_and_returns_at_once(client, provider):
    job = submit(client)
    assert job["state"] == "running"
    assert job["video_url"] is None
    assert provider.submitted[0]["prompt"] == "a corbel bracket, slowly rotating"
    # Written before the response, so a server killed now still knows the operation.
    stored = videostore.get_job(job["id"])
    assert stored["operation"] == provider.operation
    assert stored["state"] == "running"


def test_the_job_id_is_not_the_provider_operation(client, provider):
    """The provider's operation name is a path, which cannot be a URL segment -- hence an
    opaque id of our own. Both are kept: the id addresses the job here, the operation
    addresses it at the provider."""
    job = submit(client)
    assert "/" in provider.operation
    assert "/" not in job["id"]
    assert job["operation"] == provider.operation
    assert client.get(f"/api/video/jobs/{job['id']}").status_code == 200


def test_settings_reach_the_provider_and_are_recorded(client, provider):
    job = submit(client, resolution="1080p", duration_seconds=8)
    assert provider.submitted[0]["settings"] == {"resolution": "1080p",
                                                 "duration_seconds": 8}
    # Kept on the row, because "what did I ask for?" is the first question about a clip
    # that came back wrong.
    assert job["requested"] == {"resolution": "1080p", "duration_seconds": 8}


def test_an_unrequested_setting_is_not_sent_as_none(client, provider):
    """None means NOT REQUESTED throughout, so the provider's own default applies. Sending
    an explicit None would be this UI choosing, and then reporting the choice as the
    caller's."""
    submit(client)
    assert provider.submitted[0]["settings"] == {}


def test_polling_reports_running_then_stores_the_finished_video(client, provider, store):
    provider.states = ["running", "succeeded"]
    job = submit(client)

    first = client.post(f"/api/video/jobs/{job['id']}/poll", json={}).json()
    assert first["state"] == "running"
    assert first["progress"] == 0.5
    assert first["video_url"] is None

    second = client.post(f"/api/video/jobs/{job['id']}/poll", json={}).json()
    assert second["state"] == "succeeded"
    assert second["size_bytes"] == len(MP4)
    assert second["video_url"] == f"/api/video/jobs/{job['id']}/file"
    # The bytes landed beside the database, not in it.
    written = list(store.glob("*.mp4"))
    assert len(written) == 1
    assert written[0].read_bytes() == MP4


def test_a_finished_job_is_answered_from_the_table(client, provider):
    """The library's own job finalizes once and then serves from its cache; this is that
    contract carried across a restart. Polling a finished job must not contact the
    provider -- or cost anything."""
    job = submit(client)
    client.post(f"/api/video/jobs/{job['id']}/poll", json={})
    after_first = provider.polls

    again = client.post(f"/api/video/jobs/{job['id']}/poll", json={}).json()
    assert again["state"] == "succeeded"
    assert provider.polls == after_first


def test_reading_a_job_never_polls(client, provider):
    """Rendering the page must not be a billable or rate-limited act."""
    job = submit(client)
    client.get("/api/video/jobs")
    client.get(f"/api/video/jobs/{job['id']}")
    assert provider.polls == 0


def test_a_restart_recovers_an_in_flight_job(client, provider):
    """The acceptance criterion: the server goes away mid-generation, the page reopens, and
    the job is found and finished.

    The restart is simulated the way it actually matters -- a brand new TestClient and a
    fresh provider stand-in, with nothing carried over but the store on disk. The job is
    recovered from the table and resumed through the library's reference, which is the only
    thing that can address it at the provider."""
    provider.states = ["running"]
    job = submit(client)
    assert client.post(f"/api/video/jobs/{job['id']}/poll", json={}).json()["state"] == "running"

    restarted = TestClient(workbench.app)
    listed = restarted.get("/api/video/jobs").json()["jobs"]
    assert [j["id"] for j in listed] == [job["id"]]
    assert listed[0]["state"] == "running"

    provider.states = ["succeeded"]
    provider.resumed.clear()
    finished = restarted.post(f"/api/video/jobs/{job['id']}/poll", json={}).json()
    assert finished["state"] == "succeeded"
    # Resumed by reference, carrying the service, model and operation the row stored.
    ref = provider.resumed[0]
    assert (ref.service, ref.model, ref.operation) == (
        "gemini-native", VEO, provider.operation)


def test_the_stored_reference_round_trips_through_the_library(client, provider):
    """ref_for() goes through VideoJobRef.from_dict() on purpose: that is where the library
    validates a stored reference, so a row written by an older version of this table fails
    there, naming the field, rather than resuming something subtly wrong."""
    job = submit(client, resolution="720p")
    ref = videostore.ref_for(videostore.get_job(job["id"]))
    assert ref.operation == provider.operation
    assert ref.requested == {"resolution": "720p"}
    assert ref.submitted_at is not None


def test_the_download_serves_the_bytes(client, provider):
    job = submit(client)
    client.post(f"/api/video/jobs/{job['id']}/poll", json={})
    response = client.get(f"/api/video/jobs/{job['id']}/file")
    assert response.status_code == 200
    assert response.content == MP4
    assert response.headers["content-type"] == "video/mp4"


def test_the_download_of_an_unfinished_job_says_why(client, provider):
    provider.states = ["running"]
    job = submit(client)
    response = client.get(f"/api/video/jobs/{job['id']}/file")
    assert response.status_code == 404
    assert "running" in response.json()["detail"]


def test_a_filtered_job_records_its_reasons_and_has_no_file(client, provider, store):
    provider.states = ["filtered"]
    job = submit(client)
    polled = client.post(f"/api/video/jobs/{job['id']}/poll", json={}).json()
    assert polled["state"] == "filtered"
    assert polled["filtered_reasons"] == ["safety"]
    assert polled["video_url"] is None
    assert list(store.glob("*.mp4")) == []
    assert "Filtered: safety" in client.get(
        f"/api/video/jobs/{job['id']}/file").json()["detail"]


def test_a_failed_job_records_the_reason(client, provider):
    provider.states = ["failed"]
    job = submit(client)
    polled = client.post(f"/api/video/jobs/{job['id']}/poll", json={}).json()
    assert polled["state"] == "failed"
    assert polled["error"] == "the provider gave up"


def test_a_job_the_provider_has_forgotten_stops_being_polled(client, provider):
    """410, not 404: the job existed, and the difference between "wrong id" and "too late"
    is the difference between a typo and an expired clip. Recorded as failed so the page
    does not poll it forever."""
    provider.poll_raises = VideoJobNotFoundError("gemini-native", provider.operation)
    job = submit(client)
    response = client.post(f"/api/video/jobs/{job['id']}/poll", json={})
    assert response.status_code == 410
    assert videostore.get_job(job["id"])["state"] == "failed"

    before = provider.polls
    assert client.post(f"/api/video/jobs/{job['id']}/poll", json={}).json()["state"] == "failed"
    assert provider.polls == before


def test_a_transient_poll_failure_leaves_the_job_pollable(client, provider):
    """The library raises and leaves the job unfinalized precisely so the next poll
    retries -- including when the video itself failed to download. Nothing is recorded, and
    the message has to say that trying again is safe."""
    provider.poll_raises = RuntimeError("connection reset")
    job = submit(client)
    response = client.post(f"/api/video/jobs/{job['id']}/poll", json={})
    assert response.status_code == 503
    assert "still running" in response.json()["detail"]
    assert videostore.get_job(job["id"])["state"] == "running"

    provider.poll_raises = None
    assert client.post(f"/api/video/jobs/{job['id']}/poll",
                       json={}).json()["state"] == "succeeded"


def test_polling_an_unknown_job_is_a_404(client, provider):
    assert client.post("/api/video/jobs/deadbeef/poll", json={}).status_code == 404


def test_a_submission_refused_by_the_library_is_a_400(client, provider):
    provider.submit_raises = ValueError("GEMINI_API_KEY is not set")
    response = client.post("/api/video/submit", json={"model": VEO, "prompt": "x"})
    assert response.status_code == 400
    assert "GEMINI_API_KEY" in response.json()["detail"]
    # Nothing was recorded, because nothing was submitted.
    assert videostore.list_jobs() == []


def test_a_frame_is_decoded_and_its_role_recorded(client, provider):
    """A 1x1 PNG as a browser FileReader would post it."""
    png = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
           "2mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")
    job = submit(client, first_frame={"data_url": png, "name": "open.png"})
    assert provider.submitted[0]["roles"] == ["first_frame"]
    assert job["roles"] == ["first_frame"]


# --------------------------------------------------------------------------- #
# the store itself
# --------------------------------------------------------------------------- #
def ref(**overrides):
    fields = {"service": "gemini-native", "model": VEO,
              "operation": "models/veo/operations/x", "submitted_at": 1_000.0,
              "requested": {"resolution": "720p"}}
    fields.update(overrides)
    return VideoJobRef(**fields)


def test_elapsed_is_computed_by_the_server():
    """submitted_at is server wall clock, so a browser whose clock is a minute out would
    otherwise render a job as having taken a minute longer than it did."""
    videostore.record_submission("j1", ref(), prompt="x")
    videostore.record_terminal(
        "j1", VideoStatus(state="succeeded", elapsed_s=None),
        MediaResult(data=MP4, mime_type="video/mp4"))
    row = videostore.get_job("j1")
    row["finished_at"] = 1_042.0
    assert workbench.public_job(row)["elapsed_s"] == 42.0


def test_elapsed_is_none_when_the_job_was_resumed_from_a_bare_operation_id():
    """The library leaves submitted_at None there, and this reports it the same way rather
    than inventing a start time."""
    videostore.record_submission("j2", ref(submitted_at=None), prompt="x")
    assert workbench.public_job(videostore.get_job("j2"))["elapsed_s"] is None


def test_a_file_name_with_a_path_in_it_is_refused(store):
    """The column is written as a bare name. A row carrying a directory part has been
    tampered with, and must not be joined onto the media directory."""
    videostore.record_submission("j3", ref(), prompt="x")
    row = videostore.get_job("j3")
    row["file_name"] = "../../../etc/passwd"
    assert videostore.file_path(row) is None


def test_a_missing_file_reports_no_file_rather_than_a_dead_path(store):
    """A row left from a run with a different WORKBENCH_VIDEO_DIR must not hand out a path
    that does not resolve -- that renders as a broken <video> element."""
    videostore.record_submission("j4", ref(), prompt="x")
    videostore.record_terminal(
        "j4", VideoStatus(state="succeeded", elapsed_s=None),
        MediaResult(data=MP4, mime_type="video/mp4"))
    assert videostore.file_path(videostore.get_job("j4")) is not None
    next(store.glob("j4*")).unlink()
    assert videostore.file_path(videostore.get_job("j4")) is None
    assert workbench.public_job(videostore.get_job("j4"))["video_url"] is None


def test_an_unreadable_json_column_does_not_break_the_job_list(store):
    """One corrupt `requested` must not turn the whole list into a 500: the fields that
    recover a job -- service, model, operation -- are plain text and still there."""
    videostore.record_submission("j5", ref(), prompt="x")
    with videostore._connect() as connection:
        connection.execute("UPDATE jobs SET requested = ? WHERE id = ?", ("{not json", "j5"))
    row = videostore.get_job("j5")
    assert row["requested"] == {}
    assert row["operation"] == "models/veo/operations/x"


def test_the_container_decides_the_extension(store):
    videostore.record_submission("j6", ref(), prompt="x")
    videostore.record_terminal(
        "j6", VideoStatus(state="succeeded", elapsed_s=None),
        MediaResult(data=MP4, mime_type="video/webm"))
    assert videostore.file_path(videostore.get_job("j6")).suffix == ".webm"


def test_no_partial_file_is_left_where_a_reader_would_find_it(store):
    """The video is written to a neighbouring name and moved into place, so a reader that
    catches the directory mid-write sees the whole file or none of it."""
    videostore.record_submission("j7", ref(), prompt="x")
    videostore.record_terminal(
        "j7", VideoStatus(state="succeeded", elapsed_s=None),
        MediaResult(data=MP4, mime_type="video/mp4"))
    assert list(store.glob("*.part")) == []


def test_jobs_come_back_newest_first(store):
    for index, when in enumerate([1_000.0, 3_000.0, 2_000.0]):
        videostore.record_submission(f"k{index}", ref(submitted_at=when), prompt="x")
    assert [row["id"] for row in videostore.list_jobs()] == ["k1", "k2", "k0"]


def test_each_operation_opens_its_own_connection(store):
    """FastAPI runs these endpoints in a threadpool, so the submit and the poll of one job
    arrive on different threads -- and a sqlite3 connection belongs to the thread that
    opened it. Nothing is cached between calls but the file itself."""
    videostore.record_submission("k9", ref(), prompt="x")
    assert (store / videostore.DB_NAME).is_file()
    assert videostore.get_job("k9")["state"] == "running"


def test_the_schema_follows_the_configured_directory(tmp_path, monkeypatch):
    """_schema_ready caches which directory the table was created in. Compared rather than
    set to a boolean, so repointing the store -- which every test here does -- creates the
    table in the new place instead of trusting a flag set against the old one."""
    videostore.record_submission("k8", ref(), prompt="x")
    monkeypatch.setenv(videostore.VIDEO_DIR_ENV, str(tmp_path / "elsewhere"))
    assert videostore.get_job("k8") is None
    videostore.record_submission("k8", ref(), prompt="x")
    assert videostore.get_job("k8")["state"] == "running"
