"""Provider-neutral, read-only liveness classification for one Forge attempt."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .processes import (
    ProcessError,
    process_instance_is_definitively_dead,
    sample_process_cpu_seconds,
)


STALL_SECONDS = 1200
CPU_SAMPLE_SECONDS = 0.05
MAX_TERMINAL_RECEIPT_BYTES = 256 * 1024
TERMINAL_DONE = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})
EXPECTED_SCHEMA_VERSION = 12
DIRECTORY_MODE = 0o700
FILE_MODE = 0o600
SQLITE_BUSY_TIMEOUT_MS = 1000
SQLITE_PROGRESS_INSTRUCTIONS = 100_000
MAX_STDERR_PAYLOAD_BYTES = 4096
REQUIRED_JOB_COLUMNS = frozenset(
    {"id", "revision", "current_attempt_id", "status"}
)
REQUIRED_ATTEMPT_COLUMNS = frozenset(
    {
        "id",
        "job_id",
        "created_at",
        "worker_pid",
        "worker_identity",
        "provider_pgid",
        "provider_identity",
        "terminal_receipt_name",
        "terminal_receipt_hash",
    }
)
REQUIRED_EVENT_COLUMNS = frozenset(
    {
        "job_id",
        "attempt_id",
        "sequence",
        "kind",
        "payload_json",
        "event_hash",
        "created_at",
    }
)


class LivenessError(RuntimeError):
    """A bounded liveness classification error."""


class LivenessUnavailable(LivenessError):
    """The requested state does not exist or cannot be observed."""


def _private_directory(information: os.stat_result, label: str) -> None:
    if (
        not stat.S_ISDIR(information.st_mode)
        or information.st_uid != os.geteuid()
        or stat.S_IMODE(information.st_mode) != DIRECTORY_MODE
    ):
        raise LivenessError(f"{label} is not a private owned directory")


def _private_file(information: os.stat_result, label: str) -> None:
    if (
        not stat.S_ISREG(information.st_mode)
        or information.st_uid != os.geteuid()
        or stat.S_IMODE(information.st_mode) != FILE_MODE
        or information.st_nlink != 1
    ):
        raise LivenessError(f"{label} is not a private single-link file")


def _state_identity(path: Path) -> tuple[int, int]:
    """Validate the literal state path without resolving any symlink component."""

    if not path.is_absolute() or ".." in path.parts:
        raise LivenessError("liveness state path must be absolute and canonical")
    current = Path(path.anchor)
    try:
        for component in path.parts[1:-1]:
            current = current / component
            information = current.lstat()
            if stat.S_ISLNK(information.st_mode):
                # Darwin exposes its root-owned /var and /tmp compatibility names
                # as symlinks into /private.  They are not caller-controlled state
                # ancestors; every other symlink component remains forbidden.
                if not (
                    sys.platform == "darwin"
                    and str(current) in {"/var", "/tmp"}
                    and information.st_uid == 0
                ):
                    raise LivenessError(
                        "state path contains a caller-controlled symlink"
                    )
                continue
            if not stat.S_ISDIR(information.st_mode):
                raise LivenessError("state path contains a symlink or non-directory")
        parent = path.parent.lstat()
        if stat.S_ISLNK(parent.st_mode):
            raise LivenessError("state root must not be a symlink")
        _private_directory(parent, "state root")
        information = path.lstat()
    except FileNotFoundError as error:
        raise LivenessUnavailable("state is missing") from error
    except OSError as error:
        raise LivenessUnavailable("state cannot be inspected") from error
    if stat.S_ISLNK(information.st_mode):
        raise LivenessError("state must not be a symlink")
    _private_file(information, "state")
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        try:
            sidecar_information = sidecar.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise LivenessUnavailable("SQLite sidecar cannot be inspected") from error
        if stat.S_ISLNK(sidecar_information.st_mode):
            raise LivenessError("SQLite sidecar must not be a symlink")
        _private_file(sidecar_information, "SQLite sidecar")
    return information.st_dev, information.st_ino


def _hex_digest(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise LivenessError(f"{label} is malformed")
    return value


class _ReadOnlyObserver:
    """Read schema-v11 evidence without initializing or migrating Forge state."""

    def __init__(self, path: Path):
        self.path = path
        self.root_path = path.parent
        self._identity = _state_identity(path)

    def _confirm_path(self) -> None:
        if _state_identity(self.path) != self._identity:
            raise LivenessError("state identity changed during observation")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        self._confirm_path()
        uri = self.path.as_uri() + "?mode=ro"
        try:
            connection = sqlite3.connect(
                uri,
                uri=True,
                timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.set_progress_handler(
                lambda: 1,
                SQLITE_PROGRESS_INSTRUCTIONS,
            )
        except sqlite3.Error:
            self._confirm_path()
            raise
        try:
            yield connection
        finally:
            connection.close()
            self._confirm_path()

    @staticmethod
    def _columns(
        connection: sqlite3.Connection, table: str, required: frozenset[str]
    ) -> None:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        columns = {row["name"] for row in rows}
        if not required.issubset(columns):
            raise LivenessError(f"schema-v11 {table} contract is unavailable")

    def _schema_v11(self, connection: sqlite3.Connection) -> None:
        try:
            rows = connection.execute(
                "SELECT singleton,version FROM schema_version"
            ).fetchall()
        except sqlite3.Error as error:
            raise LivenessError("schema version is unavailable") from error
        if (
            len(rows) != 1
            or rows[0]["singleton"] != 1
            or rows[0]["version"] != EXPECTED_SCHEMA_VERSION
        ):
            raise LivenessError("schema version is not exactly v11")
        self._columns(connection, "jobs", REQUIRED_JOB_COLUMNS)
        self._columns(connection, "attempts", REQUIRED_ATTEMPT_COLUMNS)
        self._columns(connection, "events", REQUIRED_EVENT_COLUMNS)

    def get_liveness_snapshot(
        self,
        job_id: str,
        *,
        attempt_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        with self.connection() as connection:
            connection.execute("BEGIN")
            self._schema_v11(connection)
            row = connection.execute(
                """
                SELECT jobs.id AS job_id, jobs.revision, jobs.status,
                       jobs.current_attempt_id, attempts.id AS attempt_id,
                       attempts.job_id AS attempt_job_id,
                       attempts.created_at AS attempt_created_at,
                       attempts.worker_pid, attempts.worker_identity,
                       attempts.provider_pgid, attempts.provider_identity,
                       attempts.terminal_receipt_name,
                       attempts.terminal_receipt_hash
                FROM jobs
                JOIN attempts ON attempts.id = jobs.current_attempt_id
                WHERE jobs.id = ?
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                raise LivenessError("job or current attempt is unavailable")
            if (
                isinstance(row["revision"], bool)
                or not isinstance(row["revision"], int)
                or row["revision"] != expected_revision
            ):
                raise LivenessError("job revision is stale or malformed")
            if (
                row["current_attempt_id"] != attempt_id
                or row["attempt_id"] != attempt_id
                or row["attempt_job_id"] != job_id
            ):
                raise LivenessError("liveness attempt is not current")
            objective = connection.execute(
                """
                SELECT sequence,kind,event_hash,created_at
                FROM events
                WHERE job_id=? AND attempt_id=?
                  AND kind IN ('OBJECTIVE_PROGRESS','ARTIFACT_PROGRESS')
                ORDER BY sequence DESC LIMIT 1
                """,
                (job_id, attempt_id),
            ).fetchone()
            stderr = connection.execute(
                """
                SELECT sequence,payload_json,event_hash,created_at
                FROM events
                WHERE job_id=? AND attempt_id=? AND kind='STDERR_SIGNATURE'
                ORDER BY sequence DESC LIMIT 1
                """,
                (job_id, attempt_id),
            ).fetchone()

        stderr_payload: dict[str, Any] | None = None
        if stderr is not None:
            encoded = stderr["payload_json"]
            if (
                not isinstance(encoded, str)
                or len(encoded.encode("utf-8", errors="strict"))
                > MAX_STDERR_PAYLOAD_BYTES
            ):
                raise LivenessError("stored stderr signature evidence is malformed")
            try:
                decoded = json.loads(encoded)
            except (json.JSONDecodeError, RecursionError) as error:
                raise LivenessError(
                    "stored stderr signature evidence is malformed"
                ) from error
            if (
                not isinstance(decoded, dict)
                or set(decoded)
                != {"signature_sha256", "repeat_count", "byte_count"}
                or not isinstance(decoded["repeat_count"], int)
                or isinstance(decoded["repeat_count"], bool)
                or not 2 <= decoded["repeat_count"] <= 64
                or not isinstance(decoded["byte_count"], int)
                or isinstance(decoded["byte_count"], bool)
                or not 1 <= decoded["byte_count"] <= 4096
            ):
                raise LivenessError("stored stderr signature evidence is malformed")
            _hex_digest(decoded["signature_sha256"], "stderr signature")
            stderr_payload = decoded

        if objective is not None:
            _hex_digest(objective["event_hash"], "objective progress hash")
        if stderr is not None:
            _hex_digest(stderr["event_hash"], "stderr event hash")
        return {
            "job_id": row["job_id"],
            "attempt_id": row["attempt_id"],
            "revision": row["revision"],
            "status": row["status"],
            "attempt_created_at": row["attempt_created_at"],
            "worker_pid": row["worker_pid"],
            "worker_identity": row["worker_identity"],
            "provider_pgid": row["provider_pgid"],
            "provider_identity": row["provider_identity"],
            "terminal_receipt_name": row["terminal_receipt_name"],
            "terminal_receipt_hash": row["terminal_receipt_hash"],
            "last_objective_progress_at": (
                None if objective is None else objective["created_at"]
            ),
            "last_objective_progress_hash": (
                None if objective is None else objective["event_hash"]
            ),
            "last_objective_progress_sequence": (
                None if objective is None else objective["sequence"]
            ),
            "last_stderr_signature_at": (
                None if stderr is None else stderr["created_at"]
            ),
            "last_stderr_signature_sha256": (
                None
                if stderr_payload is None
                else stderr_payload["signature_sha256"]
            ),
            "last_stderr_repeat_count": (
                None if stderr_payload is None else stderr_payload["repeat_count"]
            ),
            "last_stderr_sequence": None if stderr is None else stderr["sequence"],
        }

    def artifact_sha256(
        self, job_id: str, attempt_id: str, name: str
    ) -> str | None:
        components = ("runs", job_id, attempt_id)
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        descriptors: list[int] = []
        descriptor: int | None = None
        try:
            root_descriptor = os.open(self.root_path, directory_flags)
            descriptors.append(root_descriptor)
            _private_directory(os.fstat(root_descriptor), "artifact root")
            for component in components:
                next_descriptor = os.open(
                    component,
                    directory_flags,
                    dir_fd=descriptors[-1],
                )
                descriptors.append(next_descriptor)
                _private_directory(os.fstat(next_descriptor), "artifact directory")
            descriptor = os.open(name, file_flags, dir_fd=descriptors[-1])
            before = os.fstat(descriptor)
            _private_file(before, "terminal receipt")
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(descriptor, 65_536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_TERMINAL_RECEIPT_BYTES:
                    return None
                digest.update(chunk)
            after = os.fstat(descriptor)
            _private_file(after, "terminal receipt")
            current = os.stat(name, dir_fd=descriptors[-1], follow_symlinks=False)
            _private_file(current, "terminal receipt")
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or (
                after.st_dev,
                after.st_ino,
            ) != (current.st_dev, current.st_ino):
                raise LivenessError("terminal receipt identity changed")
            return digest.hexdigest()
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as error:
            raise LivenessError("terminal receipt cannot be inspected safely") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            for held in reversed(descriptors):
                os.close(held)
            self._confirm_path()


def _safe_identifier(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8", errors="strict")) > 256
        or "\x00" in value
        or "/" in value
        or value in {".", ".."}
    ):
        raise LivenessError(f"{label} is invalid")
    return value


def _arguments(argv: list[str]) -> dict[str, Any]:
    allowed = {"--state", "--job", "--attempt", "--revision"}
    if len(argv) != 8:
        raise LivenessError("exactly four named liveness arguments are required")
    parsed: dict[str, str] = {}
    index = 0
    while index < len(argv):
        flag = argv[index]
        if flag not in allowed or flag in parsed or index + 1 >= len(argv):
            raise LivenessError("liveness arguments are invalid")
        value = argv[index + 1]
        if value.startswith("--"):
            raise LivenessError("liveness argument value is missing")
        parsed[flag] = value
        index += 2
    try:
        state = Path(parsed["--state"])
    except (KeyError, TypeError, ValueError) as error:
        raise LivenessError("liveness state path is invalid") from error
    if not state.is_absolute() or ".." in state.parts:
        raise LivenessError("liveness state path must be absolute")
    try:
        revision = int(parsed["--revision"], 10)
    except (KeyError, ValueError) as error:
        raise LivenessError("liveness revision is invalid") from error
    if revision < 0 or str(revision) != parsed["--revision"]:
        raise LivenessError("liveness revision is invalid")
    return {
        "state": state,
        "job": _safe_identifier(parsed.get("--job"), "job id"),
        "attempt": _safe_identifier(parsed.get("--attempt"), "attempt id"),
        "revision": revision,
    }


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise LivenessError(f"{label} is malformed")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise LivenessError(f"{label} is malformed") from error
    if parsed.tzinfo is None:
        raise LivenessError(f"{label} is malformed")
    return parsed.astimezone(timezone.utc)


def _terminal_receipt_is_valid(
    observer: _ReadOnlyObserver, snapshot: dict[str, Any]
) -> bool:
    name = snapshot["terminal_receipt_name"]
    expected_hash = snapshot["terminal_receipt_hash"]
    if (
        not isinstance(name, str)
        or not name
        or "/" in name
        or name in {".", ".."}
        or not isinstance(expected_hash, str)
        or len(expected_hash) != 64
        or any(character not in "0123456789abcdef" for character in expected_hash)
    ):
        return False
    try:
        observed_hash = observer.artifact_sha256(
            snapshot["job_id"], snapshot["attempt_id"], name
        )
    except LivenessError:
        return False
    return observed_hash == expected_hash


def _birth_state(pid: Any, identity: Any) -> str:
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or not isinstance(identity, str)
        or len(identity) != 64
        or any(character not in "0123456789abcdef" for character in identity)
    ):
        return "unavailable"
    try:
        if process_instance_is_definitively_dead(pid, identity):
            return "dead"
    except ProcessError:
        return "unavailable"
    return "live"


def classify(
    observer: _ReadOnlyObserver,
    *,
    job_id: str,
    attempt_id: str,
    revision: int,
) -> tuple[int, dict[str, Any]]:
    try:
        snapshot = observer.get_liveness_snapshot(
            job_id,
            attempt_id=attempt_id,
            expected_revision=revision,
        )
    except (LivenessError, sqlite3.Error, UnicodeError) as error:
        raise LivenessError("attempt snapshot is unavailable") from error

    common = {
        "job_id": snapshot["job_id"],
        "attempt_id": snapshot["attempt_id"],
        "revision": snapshot["revision"],
    }

    def confirmed(code: int, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        try:
            observer.get_liveness_snapshot(
                job_id,
                attempt_id=attempt_id,
                expected_revision=snapshot["revision"],
            )
        except (LivenessError, sqlite3.Error, UnicodeError) as error:
            raise LivenessError("liveness evidence changed during classification") from error
        return code, body
    status = snapshot["status"]
    if status in TERMINAL_DONE:
        if not _terminal_receipt_is_valid(observer, snapshot):
            return 2, {"status": "INVALID", "reason": "terminal_receipt_invalid", **common}
        return confirmed(
            0, {"status": "DONE", "reason": status.casefold(), **common}
        )
    if status == "PAUSED_PROVIDER_ACCESS":
        if not _terminal_receipt_is_valid(observer, snapshot):
            return 2, {"status": "INVALID", "reason": "pause_receipt_invalid", **common}
        return confirmed(
            4,
            {
                "status": "PAUSED_PROVIDER_ACCESS",
                "reason": "provider_access_pause",
                **common,
            },
        )
    if status == "PAUSED_POLICY":
        if not _terminal_receipt_is_valid(observer, snapshot):
            return 2, {"status": "INVALID", "reason": "pause_receipt_invalid", **common}
        return confirmed(
            5,
            {"status": "PAUSED_POLICY", "reason": "policy_pause", **common},
        )
    if status != "RUNNING":
        return 2, {"status": "UNAVAILABLE", "reason": "attempt_not_running", **common}

    worker_state = _birth_state(snapshot["worker_pid"], snapshot["worker_identity"])
    provider_state = _birth_state(
        snapshot["provider_pgid"], snapshot["provider_identity"]
    )
    if "dead" in {worker_state, provider_state}:
        return confirmed(3, {
            "status": "STOPPED",
            "reason": "recorded_process_birth_stopped",
            "worker_birth": worker_state,
            "provider_birth": provider_state,
            **common,
        })
    if "unavailable" in {worker_state, provider_state}:
        return confirmed(2, {
            "status": "UNAVAILABLE",
            "reason": "process_birth_unavailable",
            "worker_birth": worker_state,
            "provider_birth": provider_state,
            **common,
        })

    objective_sequence = snapshot["last_objective_progress_sequence"]
    stderr_sequence = snapshot["last_stderr_sequence"]
    repeat_count = snapshot["last_stderr_repeat_count"]
    if (
        isinstance(repeat_count, int)
        and not isinstance(repeat_count, bool)
        and repeat_count >= 2
        and (
            objective_sequence is None
            or not isinstance(objective_sequence, int)
            or not isinstance(stderr_sequence, int)
            or stderr_sequence > objective_sequence
        )
    ):
        return confirmed(3, {
            "status": "STALLED",
            "reason": "repeated_stderr_signature",
            "stderr_signature_sha256": snapshot["last_stderr_signature_sha256"],
            "stderr_repeat_count": repeat_count,
            **common,
        })

    attempt_reference = _timestamp(snapshot["attempt_created_at"], "attempt start")
    objective_reference = None
    if snapshot["last_objective_progress_hash"] is not None:
        objective_reference = _timestamp(
            snapshot["last_objective_progress_at"], "objective progress"
        )
        objective_age = (datetime.now(timezone.utc) - objective_reference).total_seconds()
        if objective_age < STALL_SECONDS:
            return confirmed(0, {
                "status": "ALIVE",
                "reason": "objective_progress",
                "objective_progress_hash": snapshot["last_objective_progress_hash"],
                **common,
            })

    process_subjects: dict[tuple[int, str], set[str]] = {}
    for label, pid, identity in (
        ("worker", snapshot["worker_pid"], snapshot["worker_identity"]),
        ("provider", snapshot["provider_pgid"], snapshot["provider_identity"]),
    ):
        process_subjects.setdefault((pid, identity), set()).add(label)
    try:
        before = {
            subject: sample_process_cpu_seconds(*subject)
            for subject in process_subjects
        }
        time.sleep(CPU_SAMPLE_SECONDS)
        after = {
            subject: sample_process_cpu_seconds(*subject)
            for subject in process_subjects
        }
    except ProcessError:
        states_after_error = {
            label: _birth_state(pid, identity)
            for label, pid, identity in (
                ("worker", snapshot["worker_pid"], snapshot["worker_identity"]),
                ("provider", snapshot["provider_pgid"], snapshot["provider_identity"]),
            )
        }
        if "dead" in set(states_after_error.values()):
            return confirmed(3, {
                "status": "STOPPED",
                "reason": "process_stopped_during_cpu_sample",
                "worker_birth": states_after_error["worker"],
                "provider_birth": states_after_error["provider"],
                **common,
            })
        return confirmed(2, {"status": "UNAVAILABLE", "reason": "cpu_evidence_unavailable", **common})
    active_subjects = [
        subject for subject in process_subjects if after[subject] > before[subject]
    ]
    if active_subjects:
        active_labels = sorted(
            {
                label
                for subject in active_subjects
                for label in process_subjects[subject]
            }
        )
        return confirmed(0, {
            "status": "ALIVE",
            "reason": "cpu_progress",
            "cpu_sources": active_labels,
            "cpu_delta_seconds": round(
                sum(after[subject] - before[subject] for subject in active_subjects),
                6,
            ),
            **common,
        })

    reference = objective_reference or attempt_reference
    age = (datetime.now(timezone.utc) - reference).total_seconds()
    if age >= STALL_SECONDS:
        return confirmed(3, {
            "status": "STALLED",
            "reason": "objective_progress_deadline",
            "age_seconds": min(int(age), 2_147_483_647),
            **common,
        })
    return confirmed(0, {"status": "ALIVE", "reason": "within_stall_window", **common})


def _emit(code: int, body: dict[str, Any]) -> int:
    encoded = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    if len(encoded.encode("ascii")) > 4096:
        encoded = '{"reason":"output_bound_exceeded","status":"INVALID"}'
        code = 2
    sys.stdout.write(encoded + "\n")
    return code


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _arguments(list(sys.argv[1:] if argv is None else argv))
    except (LivenessError, UnicodeError):
        return _emit(2, {"status": "INVALID", "reason": "invalid_arguments"})
    path = arguments["state"]
    try:
        info = path.lstat()
    except FileNotFoundError:
        return _emit(2, {"status": "UNAVAILABLE", "reason": "state_missing"})
    except OSError:
        return _emit(2, {"status": "UNAVAILABLE", "reason": "state_unavailable"})
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        return _emit(2, {"status": "INVALID", "reason": "state_identity_invalid"})
    try:
        observer = _ReadOnlyObserver(path)
        code, body = classify(
            observer,
            job_id=arguments["job"],
            attempt_id=arguments["attempt"],
            revision=arguments["revision"],
        )
    except LivenessUnavailable:
        return _emit(2, {"status": "UNAVAILABLE", "reason": "state_unavailable"})
    except (LivenessError, sqlite3.Error, OSError, ValueError, UnicodeError):
        return _emit(2, {"status": "INVALID", "reason": "state_or_evidence_invalid"})
    return _emit(code, body)


if __name__ == "__main__":
    raise SystemExit(main())
