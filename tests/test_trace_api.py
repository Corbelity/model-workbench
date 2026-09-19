"""Reading traces back over HTTP.

The client library's own tests pin what lands on disk; these pin what comes back out. The
cases that matter most are the unhappy ones -- a trace file that was never created, and a
file whose last line was cut short by a killed process -- because a reader that fails on
either is useless exactly when you reach for it.
"""
import json

import pytest
from fastapi.testclient import TestClient

from corbelity.workbench import app as workbench


@pytest.fixture
def client():
    return TestClient(workbench.app)


@pytest.fixture
def traces(monkeypatch, tmp_path):
    """Points the server at a trace file under tmp_path. The file itself is written by each
    test, so "not created yet" stays testable.

    This works because trace_file_path() reads TRACE_FILE on every call rather than
    caching it -- reading traces is deliberately independent of whether this server is
    recording any."""
    path = tmp_path / "traces.jsonl"
    monkeypatch.setenv("TRACE_FILE", str(path))
    return path


def _record(**overrides):
    record = {
        "ts": "2026-08-21T10:00:00.000Z",
        "run_id": "run01",
        "seq": 1,
        "service": "openrouter",
        "model": "vendor/model",
        "modality": "text",
        "latency_ms": 800.0,
        "request": {"system": "Be terse.", "user": "Capital of France?", "history": []},
        "response": {"text": "Paris."},
        "usage": {"prompt_tokens": 16, "completion_tokens": 7, "total_tokens": 23},
        "finish_reason": "stop",
        "error": None,
    }
    record.update(overrides)
    return record


def _write(path, *records, trailing_garbage=None):
    lines = [json.dumps(r) for r in records]
    if trailing_garbage is not None:
        lines.append(trailing_garbage)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _runs(client):
    return client.get("/api/traces").json()["runs"]


def _by_id(client, run_id):
    return next(run for run in _runs(client) if run["run_id"] == run_id)


# ------------------------------ the empty case ------------------------------ #
def test_an_absent_trace_file_reports_no_runs_rather_than_failing(client, traces):
    """Tracing never having been switched on is the normal state, not an error."""
    response = client.get("/api/traces")
    assert response.status_code == 200
    assert response.json()["runs"] == []


def test_the_listing_names_the_file_it_read(client, traces):
    assert client.get("/api/traces").json()["trace_file"] == str(traces)


def test_the_listing_reports_whether_this_server_is_recording(client, traces, monkeypatch):
    """'enabled' is about writing new records, not about whether old ones can be read."""
    monkeypatch.setattr(workbench, "TRACE", None)
    assert client.get("/api/traces").json()["enabled"] is False
    monkeypatch.setattr(workbench, "TRACE", object())
    assert client.get("/api/traces").json()["enabled"] is True


# -------------------------------- grouping ---------------------------------- #
def test_calls_are_grouped_into_runs(client, traces):
    _write(traces,
           _record(run_id="run01", seq=1),
           _record(run_id="run01", seq=2),
           _record(run_id="run02", seq=1))
    assert {run["run_id"] for run in _runs(client)} == {"run01", "run02"}


def test_a_run_counts_its_calls(client, traces):
    _write(traces, _record(seq=1), _record(seq=2), _record(seq=3))
    assert _by_id(client, "run01")["calls"] == 3


def test_a_run_lists_the_services_models_and_modalities_it_used(client, traces):
    _write(traces,
           _record(seq=1, service="openrouter", model="vendor/a", modality="text"),
           _record(seq=2, service="huggingface", model="vendor/b", modality="image"))
    run = _by_id(client, "run01")
    assert run["services"] == ["huggingface", "openrouter"]
    assert run["models"] == ["vendor/a", "vendor/b"]
    assert run["modalities"] == ["image", "text"]


def test_a_run_totals_its_token_usage(client, traces):
    _write(traces,
           _record(seq=1, usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
           _record(seq=2, usage={"prompt_tokens": 20, "completion_tokens": 1, "total_tokens": 21}))
    run = _by_id(client, "run01")
    assert (run["prompt_tokens"], run["completion_tokens"], run["total_tokens"]) == (30, 6, 36)


def test_missing_token_counts_total_as_zero_not_an_error(client, traces):
    """A failed call records no usage at all; a media call records nulls. Neither may
    poison the total with a TypeError."""
    _write(traces,
           _record(seq=1, usage={"prompt_tokens": None, "completion_tokens": None,
                                 "total_tokens": None}),
           _record(seq=2, usage={}))
    assert _by_id(client, "run01")["total_tokens"] == 0


def test_a_run_reports_when_it_started_and_ended(client, traces):
    _write(traces,
           _record(seq=1, ts="2026-08-21T10:00:00.000Z"),
           _record(seq=2, ts="2026-08-21T10:04:30.000Z"))
    run = _by_id(client, "run01")
    assert run["started"] == "2026-08-21T10:00:00.000Z"
    assert run["ended"] == "2026-08-21T10:04:30.000Z"


def test_a_run_counts_its_failures(client, traces):
    _write(traces,
           _record(seq=1),
           _record(seq=2, error="RuntimeError('boom')", response=None))
    assert _by_id(client, "run01")["errors"] == 1


def test_a_run_counts_the_artifacts_it_produced(client, traces):
    """Generated media is written beside the trace, not inlined, so the record carries a
    descriptor and this is how you find out a run produced something to look at."""
    _write(traces,
           _record(seq=1),
           _record(seq=2, modality="image",
                   response={"artifact": {"name": "run01-002.png", "mime_type": "image/png",
                                          "bytes": 1_400_000}}))
    assert _by_id(client, "run01")["artifacts"] == 1


def test_the_newest_run_is_listed_first(client, traces):
    _write(traces,
           _record(run_id="older", ts="2026-08-20T09:00:00.000Z"),
           _record(run_id="newer", ts="2026-08-21T09:00:00.000Z"))
    assert [run["run_id"] for run in _runs(client)] == ["newer", "older"]


# ------------------------------ damaged files ------------------------------- #
def test_a_truncated_last_line_does_not_hide_the_rest(client, traces):
    """A process killed mid-write leaves half a line. One bad byte must not cost you the
    whole history."""
    _write(traces, _record(seq=1), _record(seq=2), trailing_garbage='{"ts": "2026-08-2')
    assert _by_id(client, "run01")["calls"] == 2


def test_unreadable_lines_are_reported_not_silently_dropped(client, traces):
    """Silently reporting 3 of 4 calls is worse than saying one line was lost."""
    _write(traces, _record(seq=1), trailing_garbage="not json at all")
    assert client.get("/api/traces").json()["skipped_lines"] == 1


def test_a_json_line_that_is_not_a_record_is_skipped(client, traces):
    _write(traces, _record(seq=1), trailing_garbage="[1, 2, 3]")
    body = client.get("/api/traces").json()
    assert body["skipped_lines"] == 1
    assert len(body["runs"]) == 1


def test_a_clean_file_reports_nothing_skipped(client, traces):
    _write(traces, _record(seq=1))
    assert client.get("/api/traces").json()["skipped_lines"] == 0


# -------------------------------- run detail -------------------------------- #
def test_a_run_returns_its_records_in_sequence(client, traces):
    _write(traces,
           _record(run_id="run01", seq=2),
           _record(run_id="run01", seq=1),
           _record(run_id="run02", seq=1))
    body = client.get("/api/traces/run01").json()
    assert [r["seq"] for r in body["records"]] == [1, 2]


def test_a_run_returns_only_its_own_records(client, traces):
    _write(traces, _record(run_id="run01"), _record(run_id="run02"))
    body = client.get("/api/traces/run01").json()
    assert {r["run_id"] for r in body["records"]} == {"run01"}


def test_prompts_and_responses_come_back_whole(client, traces):
    """A trimmed trace cannot reproduce the call, which is the entire point of keeping one."""
    long_prompt = "x" * 20_000
    _write(traces, _record(request={"system": "s", "user": long_prompt, "history": []},
                           response={"text": "y" * 20_000}))
    record = client.get("/api/traces/run01").json()["records"][0]
    assert record["request"]["user"] == long_prompt
    assert record["response"]["text"] == "y" * 20_000


def test_an_unknown_run_is_a_404(client, traces):
    _write(traces, _record(run_id="run01"))
    assert client.get("/api/traces/nosuchrun").status_code == 404


def test_a_run_requested_from_a_missing_file_is_a_404(client, traces):
    assert client.get("/api/traces/run01").status_code == 404
