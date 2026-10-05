"""
corbelity.workbench.videostore -- durable state for submitted video jobs.

Video generation is submit-then-poll and runs for tens of seconds to minutes, so a job
outlives the request that started it and, routinely, the server process as well. The
library is explicit that it does not own persistence (see its jobs.py: "The library
serializes; the application stores"), and this is the application.

Two things are stored, in one directory:

  * a row per job in SQLite -- everything `VideoJobRef.to_dict()` carries, plus the state
    last observed and where the finished video landed;
  * the finished video as a file beside it.

The video is a file rather than a column, and rather than a base64 data URL the way
generated images and speech are returned, because a Veo clip is tens of megabytes: a data
URL would be a third larger again, has to be held in memory whole to build, and cannot be
seeked or ranged by a <video> element. Images get away with it at a megabyte or two; video
does not.

Nothing here holds a credential. A key typed into the sidebar is a per-request override
and is never written to disk, so a job polled after a restart is polled with whatever the
environment supplies. That is a deliberate limit, not an oversight -- see the poll
endpoint in app.py, which says what the caller sees when it bites.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from corbelity.model_client import MediaResult, VideoJobRef, VideoStatus

logger = logging.getLogger(__name__)

# Where jobs and their videos live. Overridable because the default is relative to the
# launch directory, which is right for a workbench run from a checkout and wrong for
# anything else.
DEFAULT_VIDEO_DIR = Path("media") / "video"

# Only ever consulted when nothing has set it, so a test can point the store at tmp_path
# by setting the variable before the first call.
VIDEO_DIR_ENV = "WORKBENCH_VIDEO_DIR"

DB_NAME = "jobs.sqlite3"

# How long a writer waits for another connection's lock before giving up. Polls and
# submissions are short, single-row writes, so contention is measured in milliseconds;
# this is high enough that a slow disk never surfaces as "database is locked".
BUSY_TIMEOUT_MS = 5_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    -- Ours, not the provider's. The provider's operation name is a path
    -- ("models/veo-3.1.../operations/abc123"), which cannot be a URL path segment, and a
    -- job is only addressable at all in company with its service and model -- so the row
    -- needs an opaque key of its own regardless.
    id               TEXT PRIMARY KEY,
    service          TEXT NOT NULL,
    model            TEXT NOT NULL,
    operation        TEXT NOT NULL,
    submitted_at     REAL,
    -- VideoJobRef.requested, as JSON: the settings actually sent, after any the catalog's
    -- constraints filled in. Stored because the ref is rebuilt from this row, and because
    -- "what did I ask for?" is the first question about a clip that came back wrong.
    requested        TEXT NOT NULL DEFAULT '{}',
    prompt           TEXT NOT NULL DEFAULT '',
    -- The input roles supplied (first_frame, last_frame, ...). The frames themselves are
    -- not kept: they are the user's files, already on their disk.
    roles            TEXT NOT NULL DEFAULT '[]',
    state            TEXT NOT NULL,
    error            TEXT,
    filtered_reasons TEXT NOT NULL DEFAULT '[]',
    mime_type        TEXT,
    -- Bare file name, never a full path: the directory can move between runs, and a row
    -- holding an absolute path from a previous machine is a row that points at nothing.
    file_name        TEXT,
    size_bytes       INTEGER,
    finished_at      REAL
);
CREATE INDEX IF NOT EXISTS jobs_by_submitted ON jobs(submitted_at DESC);
"""

# The row shape every reader gets back, so the column list exists once.
_COLUMNS = (
    "id", "service", "model", "operation", "submitted_at", "requested", "prompt", "roles",
    "state", "error", "filtered_reasons", "mime_type", "file_name", "size_bytes",
    "finished_at",
)


def video_dir() -> Path:
    """The directory holding the job database and the finished videos.

    Read on every call rather than resolved at import, for the same reason load_models()
    re-reads the catalog: a test that repoints it must not have to have won a race with
    import order."""
    configured = os.getenv(VIDEO_DIR_ENV, "").strip()
    return Path(configured) if configured else DEFAULT_VIDEO_DIR


def db_path() -> Path:
    return video_dir() / DB_NAME


# The directory whose schema this process has already ensured. Compared rather than set
# to True so that repointing WORKBENCH_VIDEO_DIR -- which every test that touches the
# store does -- creates the table in the new location instead of trusting a flag set
# against the old one.
_schema_ready: Path | None = None


@contextmanager
def _connect(*, ensure_schema: bool = True) -> Iterator[sqlite3.Connection]:
    """A connection for the duration of one operation, then closed.

    Deliberately not a shared module-level connection. FastAPI runs this app's blocking
    endpoints in a threadpool, so consecutive requests -- and the submit and the poll of
    one job -- arrive on different threads, and a sqlite3 connection belongs to the thread
    that opened it. The alternatives are `check_same_thread=False` plus a lock of our own
    (reimplementing what SQLite already does), or a connection per thread held forever
    (file handles that outlive their use). Opening a local database is microseconds.

    WAL so a poll reading the table is never blocked by a submission writing to it.

    The schema is ensured here rather than in a startup hook because the endpoints are
    reached in tests through a bare TestClient, which does not run startup events -- a
    store that only works when something remembered to initialise it is a store that
    fails on the one path nobody exercised. CREATE TABLE IF NOT EXISTS is idempotent, so
    two threads arriving together is harmless.
    """
    global _schema_ready
    directory = video_dir()
    directory.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(directory / DB_NAME, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        if ensure_schema and _schema_ready != directory:
            connection.executescript(_SCHEMA)
            _schema_ready = directory
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    """Create the table if it is not there. Idempotent, and safe to call on every start.

    Not required -- every operation ensures the schema itself (see _connect) -- but worth
    calling at launch so a directory that cannot be written fails there, with the path in
    the message, rather than on the first submission."""
    global _schema_ready
    _schema_ready = None
    with _connect():
        pass


def new_job_id() -> str:
    """An opaque, URL-safe id. uuid4 because it must not be guessable: the id is the only
    thing standing between one browser and another browser's video on a shared host."""
    return uuid.uuid4().hex


def record_submission(job_id: str, ref: VideoJobRef, *, prompt: str,
                      roles: Sequence[str] = ()) -> dict[str, Any]:
    """Store a just-submitted job as running, and return the stored row.

    Written before the submitting request returns, so a server killed one second later
    still knows the operation exists. A submission that is not recorded is a job that is
    running, billed, and lost -- which is precisely what the library's two-trace-records
    design exists to make visible, and what this table exists to prevent."""
    with _connect() as connection:
        connection.execute(
            "INSERT INTO jobs (id, service, model, operation, submitted_at, requested,"
            " prompt, roles, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running')",
            (job_id, ref.service, ref.model, ref.operation, ref.submitted_at,
             json.dumps(dict(ref.requested)), prompt, json.dumps(list(roles))),
        )
    logger.info("Video job %s recorded: %s/%s operation=%s", job_id, ref.service, ref.model,
                ref.operation)
    stored = get_job(job_id)
    assert stored is not None   # just inserted, in the same process
    return stored


def record_terminal(job_id: str, status: VideoStatus,
                    media: MediaResult | None = None) -> dict[str, Any]:
    """Store a job's outcome, writing the video out first when there is one.

    The file lands before the row is updated, so the row never claims a video that is not
    there. The reverse order would leave a succeeded row pointing at a missing file after
    a crash between the two, which reads to the UI as a corrupted result rather than as an
    unfinished write."""
    file_name: str | None = None
    size: int | None = None
    mime: str | None = None
    if media is not None and media.data:
        mime = media.mime_type
        file_name = f"{job_id}{_suffix_for(mime)}"
        target = video_dir() / file_name
        target.parent.mkdir(parents=True, exist_ok=True)
        # Written to a neighbouring name and moved into place, so a reader that catches the
        # directory mid-write sees either no file or the whole file, never a prefix.
        staging = target.with_name(f"{file_name}.part")
        staging.write_bytes(media.data)
        staging.replace(target)
        size = len(media.data)

    with _connect() as connection:
        connection.execute(
            "UPDATE jobs SET state = ?, error = ?, filtered_reasons = ?, mime_type = ?,"
            " file_name = ?, size_bytes = ?, finished_at = ? WHERE id = ?",
            (status.state, status.error, json.dumps(list(status.filtered_reasons)), mime,
             file_name, size, _wall_clock(), job_id),
        )
    logger.info("Video job %s finished: %s%s", job_id, status.state,
                f" ({size} bytes)" if size else "")
    stored = get_job(job_id)
    if stored is None:
        raise LookupError(f"Video job {job_id!r} vanished while its outcome was recorded.")
    return stored


def get_job(job_id: str) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    return _as_dict(row) if row is not None else None


def list_jobs(limit: int = 50) -> list[dict[str, Any]]:
    """Recent jobs, newest first -- what the page asks for on load to pick up anything
    that was still running when it was last closed."""
    with _connect() as connection:
        rows = connection.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM jobs"
            " ORDER BY submitted_at DESC, rowid DESC LIMIT ?",
            (max(1, limit),),
        ).fetchall()
    return [_as_dict(row) for row in rows]


def ref_for(row: Mapping[str, Any]) -> VideoJobRef:
    """Rebuild the library's reference from a stored row, so the job can be resumed.

    Goes through VideoJobRef.from_dict() rather than the constructor on purpose: that is
    where the library validates a stored reference, and a row hand-edited or written by an
    older version of this table should fail there, naming the bad field, rather than
    resuming something subtly wrong."""
    return VideoJobRef.from_dict({
        "service": row["service"],
        "model": row["model"],
        "operation": row["operation"],
        "submitted_at": row["submitted_at"],
        "requested": row["requested"],
    })


def file_path(row: Mapping[str, Any]) -> Path | None:
    """Where this job's video is, or None when it has none yet.

    The stored name is re-derived against the current directory and checked, so a row left
    over from a run with a different WORKBENCH_VIDEO_DIR reports "no file" instead of
    handing out a path that does not resolve."""
    name = row.get("file_name")
    if not name:
        return None
    # Defence against a hand-edited row: the column is written as a bare file name, and
    # anything with a directory part in it must not be joined onto the media directory.
    if Path(name).name != name:
        logger.error("Video job %s has a suspicious file name %r; refusing it.",
                     row.get("id"), name)
        return None
    candidate = video_dir() / name
    return candidate if candidate.is_file() else None


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #
# What a container's MIME type is called on disk. A clip whose type is not in here keeps
# .bin rather than being guessed at: the extension is what the browser and the user's
# player go by, and a wrong one is worse than a neutral one.
_SUFFIXES = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
}


def _suffix_for(mime: str | None) -> str:
    return _SUFFIXES.get((mime or "").split(";")[0].strip().lower(), ".bin")


def _as_dict(row: sqlite3.Row) -> dict[str, Any]:
    """A row with its JSON columns decoded.

    A column that will not parse is reported as empty and logged rather than raising: a
    single unreadable `requested` must not make the whole job list a 500, and the fields
    that matter for recovering the job -- the operation, the service, the model -- are
    plain text."""
    record = dict(row)
    for column, empty in (("requested", {}), ("roles", []), ("filtered_reasons", [])):
        raw = record.get(column)
        if isinstance(raw, str):
            try:
                record[column] = json.loads(raw)
            except ValueError:
                logger.error("Video job %s has unreadable %s: %r", record.get("id"),
                             column, raw)
                record[column] = empty
        else:
            record[column] = empty
    return record


def _wall_clock() -> float:
    """Wall clock, matching the library's choice for submitted_at: a job outlives the
    process that submitted it, and a monotonic reading means nothing after a restart."""
    return time.time()
