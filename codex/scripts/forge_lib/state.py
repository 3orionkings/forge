"""Private, transactional SQLite state for Forge jobs."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import hmac
import json
import math
import os
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .processes import (
    ProcessError,
    process_instance_identity,
    process_instance_is_definitively_dead,
    process_instance_is_live,
)


SCHEMA_VERSION = 12
SQLITE_TIMEOUT_SECONDS = 10
DIRECTORY_MODE = 0o700
FILE_MODE = 0o600
CREATE_NEW_ATTEMPT = "CREATE_NEW_ATTEMPT"
JOURNAL_MODE_RETRY_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.16)
MAX_ATTEMPT_EVENTS = 1024
MAX_STATE_EVENT_BYTES = 1_100_000
MAX_ATTEMPT_EVENT_BYTES = 16 * 1024 * 1024
MAX_PARENT_TOOL_USE_IDS = 128
MAX_IDENTITY_JSON_BYTES = 64 * 1024
MAX_COMMANDS_PER_JOB = 1024
MAX_COMMAND_PAYLOAD_BYTES = 3_200_000
MAX_TERMINAL_RECEIPT_BYTES = 256 * 1024
MAX_CANCELLATION_RESERVED_EVENT_BYTES = 32 * 1024
MAX_CANCELLATION_CONTROL_EVENT_BYTES = 4 * 1024
NATIVE_RECOVERY_UNSUPPORTED = (
    "native recovery lanes are unsupported because a mutable worktree has no "
    "atomic filesystem snapshot; use typed bridge recovery or hand off a "
    "content-addressed patch into a freshly admitted clean normal feature worktree"
)
ATTEMPT_LOCK_DIRECTORY = "attempt-locks"
ADMISSION_LOCK_FILE = "admission.lock"
CANCELLATION_MANIFEST_PIN_FIELDS = frozenset(
    {"device", "inode", "mode", "size", "modified_ns", "content_hash"}
)

TRANSITIONS = {
    "CREATED": {"PREFLIGHT", "FAILED"},
    "PREFLIGHT": {
        "RUNNING",
        "PAUSED_PROVIDER_ACCESS",
        "PAUSED_POLICY",
        "FAILED",
    },
    "RUNNING": {
        "SUCCEEDED",
        "PAUSED_PROVIDER_ACCESS",
        "PAUSED_POLICY",
        "INTERRUPTING",
        "FAILED",
    },
    "INTERRUPTING": {"CANCELLED", "FAILED"},
    "SUCCEEDED": set(),
    "CANCELLED": set(),
    "FAILED": set(),
    "PAUSED_PROVIDER_ACCESS": set(),
    "PAUSED_POLICY": set(),
}

TERMINAL_STATUSES = frozenset(
    status for status, successors in TRANSITIONS.items() if not successors
)


class StateError(RuntimeError):
    """Base class for durable-state failures."""


class SchemaMigrationError(StateError):
    """Raised when a database schema cannot be migrated safely."""


class JobNotFound(StateError):
    """Raised when a Forge job does not exist."""


class AttemptNotFound(StateError):
    """Raised when a Forge attempt does not exist."""


class RevisionConflict(StateError):
    """Raised when a compare-and-swap revision is stale."""

    def __init__(self, job_id: str, expected: int, actual: int):
        self.job_id = job_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"revision conflict for job {job_id}: expected {expected}, actual {actual}"
        )


class IdempotencyConflict(StateError):
    """Raised when an idempotency key is reused for a different request."""


class InvalidTransition(StateError):
    """Raised when an attempt state transition is not allowed."""


class WorkerIdentityUnavailable(StateError):
    """Raised when cancellation cannot prove the live durable worker identity."""


class CancellationAcknowledgementExpired(StateError):
    """Raised when a control ACK reaches the durable fence after its deadline."""


def _validate_cancellation_manifest_pin(value: Any) -> dict[str, int | str]:
    """Validate the bounded test-oracle seal persisted with a cancel command."""

    if not isinstance(value, dict) or set(value) != CANCELLATION_MANIFEST_PIN_FIELDS:
        raise StateError("cancellation manifest admission seal is invalid")
    numeric_fields = ("device", "inode", "mode", "size", "modified_ns")
    if any(
        isinstance(value[field], bool)
        or not isinstance(value[field], int)
        or value[field] < 0
        for field in numeric_fields
    ):
        raise StateError("cancellation manifest admission seal is invalid")
    if (
        value["inode"] <= 0
        or value["mode"] != 0o400
        or value["size"] > 64 * 1024
        or not isinstance(value["content_hash"], str)
        or len(value["content_hash"]) != 64
        or any(
            character not in "0123456789abcdef"
            for character in value["content_hash"]
        )
    ):
        raise StateError("cancellation manifest admission seal is invalid")
    return {field: value[field] for field in sorted(CANCELLATION_MANIFEST_PIN_FIELDS)}


@dataclass(frozen=True)
class Job:
    id: str
    repo_id: str
    idempotency_key: str
    request_hash: str
    payload: Any
    status: str
    revision: int
    current_attempt_id: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Attempt:
    id: str
    job_id: str
    number: int
    parent_attempt_id: str | None
    status: str
    revision: int
    dispatch_state: str
    dispatch_owner_pid: int | None
    dispatch_token: str | None
    worker_pid: int | None
    worker_identity: str | None
    provider_pgid: int | None
    provider_identity: str | None
    session_id: str | None
    resume_session_id: str | None
    command_inbox_state: str
    model_override: str | None
    model_cli_accepted: str | None
    models_observed: tuple[str, ...]
    verification_level: str
    observed_model: str | None
    parent_tool_use_ids: tuple[str, ...]
    event_count: int
    event_bytes: int
    terminal_receipt_name: str | None
    terminal_receipt_hash: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class DispatchClaim:
    job_id: str
    attempt_id: str
    owner_pid: int
    token: str
    revision: int


@dataclass(frozen=True)
class Event:
    id: int
    job_id: str
    attempt_id: str
    sequence: int
    kind: str
    payload: Any
    payload_json: str
    previous_hash: str | None
    event_hash: str
    revision: int
    created_at: str


@dataclass(frozen=True)
class Command:
    id: str
    job_id: str
    attempt_id: str
    sequence: int
    kind: str
    payload: Any
    expected_revision: int
    idempotency_key: str
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class CancellationAckFailure:
    rolled_back: bool
    inbox_closed: bool
    failed_commands: tuple[Command, ...]


@dataclass(frozen=True)
class CancellationAdmission:
    command: Command | None
    status: str
    revision: int
    idempotent: bool
    outcome: str


@dataclass(frozen=True, slots=True, eq=False)
class _WorkerAuthority:
    """Unforgeable in-process authority held only by the actual current worker."""

    store_marker: object
    issuer_pid: int
    job_id: str
    attempt_id: str
    dispatch_token: str
    worker_identity: str


_WORKER_AUTHORITY_LOCK = threading.Lock()
_WORKER_AUTHORITY_CLAIMS: dict[tuple[str, str, str], _WorkerAuthority] = {}


@dataclass(frozen=True)
class _AttemptEvidenceLease:
    store_root: Path
    job_id: str
    attempt_id: str
    descriptor: int | None
    exclusive: bool
    device: int
    inode: int
    owner: int
    directory_context: AbstractContextManager[int] | None


@dataclass
class _ProvisionedAttemptLock:
    descriptor: int
    device: int
    inode: int
    owner: int
    created: bool
    published: bool = False


_SCHEMA_V1 = (
    """
    CREATE TABLE jobs (
        id TEXT PRIMARY KEY,
        repo_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 0),
        current_attempt_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (repo_id, idempotency_key),
        FOREIGN KEY (current_attempt_id) REFERENCES attempts(id)
            DEFERRABLE INITIALLY DEFERRED
    )
    """,
    """
    CREATE TABLE attempts (
        id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        number INTEGER NOT NULL CHECK (number >= 1),
        parent_attempt_id TEXT REFERENCES attempts(id),
        status TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (job_id, number)
    )
    """,
    """
    CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
        sequence INTEGER NOT NULL CHECK (sequence >= 1),
        kind TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        previous_hash TEXT,
        event_hash TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 1),
        created_at TEXT NOT NULL,
        UNIQUE (job_id, sequence)
    )
    """,
    """
    CREATE TABLE commands (
        id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
        sequence INTEGER NOT NULL CHECK (sequence >= 1),
        kind TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        expected_revision INTEGER NOT NULL CHECK (expected_revision >= 0),
        idempotency_key TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'QUEUED',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (job_id, sequence),
        UNIQUE (job_id, idempotency_key)
    )
    """,
    "CREATE INDEX events_job_attempt_idx ON events(job_id, attempt_id, sequence)",
    "CREATE INDEX commands_job_status_idx ON commands(job_id, status, sequence)",
)

_SCHEMA_V2 = (
    "ALTER TABLE attempts ADD COLUMN dispatch_state TEXT NOT NULL DEFAULT 'UNCLAIMED'",
    "ALTER TABLE attempts ADD COLUMN worker_pid INTEGER",
    "ALTER TABLE attempts ADD COLUMN provider_pgid INTEGER",
    "ALTER TABLE attempts ADD COLUMN session_id TEXT",
    "ALTER TABLE attempts ADD COLUMN observed_model TEXT",
    "ALTER TABLE attempts ADD COLUMN parent_tool_use_ids_json TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE attempts ADD COLUMN event_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE attempts ADD COLUMN event_bytes INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE attempts ADD COLUMN terminal_receipt_name TEXT",
    "ALTER TABLE attempts ADD COLUMN terminal_receipt_hash TEXT",
    """
    UPDATE attempts
    SET event_count = (
            SELECT COUNT(*) FROM events WHERE events.attempt_id = attempts.id
        ),
        event_bytes = COALESCE((
            SELECT SUM(LENGTH(CAST(payload_json AS BLOB)))
            FROM events WHERE events.attempt_id = attempts.id
        ), 0)
    """,
)

_SCHEMA_V3 = (
    "ALTER TABLE attempts ADD COLUMN dispatch_owner_pid INTEGER",
)

_SCHEMA_V4 = (
    "ALTER TABLE attempts ADD COLUMN dispatch_token TEXT",
)

_SCHEMA_V5 = (
    "ALTER TABLE attempts ADD COLUMN resume_session_id TEXT",
)

_SCHEMA_V6 = (
    "ALTER TABLE attempts ADD COLUMN command_inbox_state TEXT NOT NULL DEFAULT 'OPEN'",
)

_SCHEMA_V7 = (
    """
    CREATE TABLE attempt_evidence_locks (
        attempt_id TEXT PRIMARY KEY REFERENCES attempts(id) ON DELETE CASCADE,
        device_hex TEXT NOT NULL
            CHECK (
                typeof(device_hex) = 'text'
                AND length(device_hex) BETWEEN 1 AND 32
                AND device_hex NOT GLOB '*[^0-9a-f]*'
            ),
        inode_hex TEXT NOT NULL
            CHECK (
                typeof(inode_hex) = 'text'
                AND length(inode_hex) BETWEEN 1 AND 32
                AND inode_hex NOT GLOB '*[^0-9a-f]*'
            ),
        owner INTEGER NOT NULL
            CHECK (typeof(owner) = 'integer' AND owner >= 0),
        UNIQUE (device_hex, inode_hex)
    )
    """,
)

_SCHEMA_V8 = (
    "ALTER TABLE attempts ADD COLUMN worker_identity TEXT",
)

_SCHEMA_V9 = (
    "ALTER TABLE attempts ADD COLUMN model_override TEXT",
    "ALTER TABLE attempts ADD COLUMN model_cli_accepted TEXT",
    "ALTER TABLE attempts ADD COLUMN models_observed_json TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE attempts ADD COLUMN verification_level TEXT NOT NULL DEFAULT 'unverified'",
)

_SCHEMA_V10 = (
    """
    CREATE TABLE IF NOT EXISTS attempt_admissions (
        attempt_id TEXT PRIMARY KEY REFERENCES attempts(id) ON DELETE CASCADE,
        evidence_json TEXT NOT NULL,
        evidence_hash TEXT NOT NULL
            CHECK (
                typeof(evidence_hash) = 'text'
                AND length(evidence_hash) = 64
                AND evidence_hash NOT GLOB '*[^0-9a-f]*'
            )
    )
    """,
)

_SCHEMA_V11 = (
    "ALTER TABLE attempts ADD COLUMN provider_identity TEXT",
)

_SCHEMA_V12 = (
    """
    CREATE TABLE IF NOT EXISTS attempt_contexts (
        attempt_id TEXT PRIMARY KEY REFERENCES attempts(id) ON DELETE CASCADE,
        context_json TEXT NOT NULL,
        context_hash TEXT NOT NULL
            CHECK (
                typeof(context_hash) = 'text'
                AND length(context_hash) = 64
                AND context_hash NOT GLOB '*[^0-9a-f]*'
            )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS lane_reservations (
        lease_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('bridge', 'native')),
        job_id TEXT UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
        attempt_id TEXT REFERENCES attempts(id) ON DELETE CASCADE,
        repo_id TEXT NOT NULL,
        base_sha TEXT NOT NULL,
        lane TEXT NOT NULL,
        weight TEXT NOT NULL CHECK (weight IN ('LIGHT', 'HEAVY')),
        writer TEXT,
        files_json TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 0),
        owner_token_hash TEXT,
        receipt_pin_json TEXT,
        admission_json TEXT NOT NULL,
        admission_hash TEXT NOT NULL,
        last_operation_hash TEXT
            CHECK (
                last_operation_hash IS NULL
                OR (
                    typeof(last_operation_hash) = 'text'
                    AND length(last_operation_hash) = 64
                    AND last_operation_hash NOT GLOB '*[^0-9a-f]*'
                )
            ),
        last_command_id TEXT REFERENCES commands(id),
        ttl_seconds INTEGER CHECK (ttl_seconds IS NULL OR ttl_seconds BETWEEN 5 AND 86400),
        heartbeat_at TEXT,
        stale_at TEXT,
        released_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK (
            (kind = 'bridge' AND job_id IS NOT NULL AND attempt_id IS NOT NULL
             AND owner_token_hash IS NULL AND receipt_pin_json IS NULL)
            OR
            (kind = 'native' AND job_id IS NULL AND attempt_id IS NULL
             AND owner_token_hash IS NOT NULL AND receipt_pin_json IS NOT NULL)
        )
    )
    """,
    "CREATE INDEX IF NOT EXISTS lane_reservations_kind_idx "
    "ON lane_reservations(kind, released_at)",
)


def canonical_json(payload: Any) -> str:
    """Return the sole JSON representation used for identity and event hashing."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=DIRECTORY_MODE)
    path.chmod(DIRECTORY_MODE)


def _ensure_private_file(path: Path) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, FILE_MODE)
    os.close(descriptor)
    path.chmod(FILE_MODE)


class Store:
    """SQLite-backed Forge state with one immediate transaction per mutation."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path).expanduser().resolve()
        self._worker_authority_marker = object()
        self._worker_authorities: dict[str, _WorkerAuthority] = {}
        self._dispatch_claims: dict[str, DispatchClaim] = {}
        self.root_path = self.path.parent
        self.runs_path = self.root_path / "runs"
        _ensure_private_directory(self.root_path)
        _ensure_private_file(self.path)
        with self.connection() as connection:
            journal_mode = self._enable_wal(connection)
            if str(journal_mode).lower() != "wal":
                raise StateError(f"unable to enable SQLite WAL mode: {journal_mode}")
            self._migrate(connection)
        self._bootstrap_attempt_locks()

    @staticmethod
    def _enable_wal(connection: sqlite3.Connection) -> str:
        for delay in (*JOURNAL_MODE_RETRY_DELAYS, None):
            try:
                return connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            except sqlite3.OperationalError as exc:
                error_code = getattr(exc, "sqlite_errorcode", None)
                lock_error = (
                    error_code is not None
                    and error_code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                ) or "locked" in str(exc).lower()
                if not lock_error or delay is None:
                    raise
                time.sleep(delay)
        raise AssertionError("unreachable")

    def _connect(self, *, busy_timeout_ms: int = 10_000) -> sqlite3.Connection:
        if (
            isinstance(busy_timeout_ms, bool)
            or not isinstance(busy_timeout_ms, int)
            or not 0 <= busy_timeout_ms <= 10_000
        ):
            raise StateError("SQLite busy timeout is invalid")
        connection = sqlite3.connect(
            str(self.path),
            timeout=busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        return connection

    @contextmanager
    def connection(
        self,
        *,
        _busy_timeout_ms: int = 10_000,
    ) -> Iterator[sqlite3.Connection]:
        """Open an autocommit connection configured for Forge state."""
        connection = self._connect(busy_timeout_ms=_busy_timeout_ms)
        try:
            yield connection
        finally:
            connection.close()
            self._secure_sqlite_files()

    @contextmanager
    def _transaction(
        self,
        *,
        _busy_timeout_ms: int = 10_000,
    ) -> Iterator[sqlite3.Connection]:
        with self.connection(_busy_timeout_ms=_busy_timeout_ms) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
            else:
                connection.commit()

    @staticmethod
    def _lock_identifier(value: str, label: str) -> str:
        if (
            not isinstance(value, str)
            or len(value) != 32
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise StateError(f"{label} is invalid for an evidence lease")
        return value

    @staticmethod
    def _lock_identity_from_row(row: sqlite3.Row) -> tuple[int, int, int]:
        encoded = (row["device_hex"], row["inode_hex"])
        if any(
            not isinstance(value, str)
            or not 1 <= len(value) <= 32
            or any(character not in "0123456789abcdef" for character in value)
            or (len(value) > 1 and value.startswith("0"))
            for value in encoded
        ):
            raise StateError("stored attempt evidence lock identity is malformed")
        owner = row["owner"]
        if type(owner) is not int or owner < 0:
            raise StateError("stored attempt evidence lock identity is malformed")
        if owner != os.geteuid():
            raise StateError("stored attempt evidence lock is not owned by this user")
        return int(encoded[0], 16), int(encoded[1], 16), owner

    @staticmethod
    def _lock_identity_from_stat(
        information: os.stat_result,
    ) -> tuple[int, int, int]:
        identity = (
            information.st_dev,
            information.st_ino,
            information.st_uid,
        )
        if any(type(value) is not int or value < 0 for value in identity):
            raise StateError("attempt evidence lock identity is invalid")
        return identity

    @staticmethod
    def _insert_attempt_lock_identity(
        connection: sqlite3.Connection,
        attempt_id: str,
        identity: tuple[int, int, int],
    ) -> None:
        device, inode, owner = identity
        try:
            connection.execute(
                """
                INSERT INTO attempt_evidence_locks(
                    attempt_id, device_hex, inode_hex, owner
                ) VALUES (?, ?, ?, ?)
                """,
                (attempt_id, format(device, "x"), format(inode, "x"), owner),
            )
        except sqlite3.IntegrityError as error:
            raise StateError(
                "attempt evidence lock identity could not be published uniquely"
            ) from error

    @contextmanager
    def _attempt_lock_directory(self, *, create: bool) -> Iterator[int]:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        root_descriptor: int | None = None
        lock_directory_descriptor: int | None = None
        try:
            root_descriptor = os.open(self.root_path, directory_flags)
            self._validate_private_directory_info(os.fstat(root_descriptor))
            if create:
                try:
                    os.mkdir(
                        ATTEMPT_LOCK_DIRECTORY,
                        DIRECTORY_MODE,
                        dir_fd=root_descriptor,
                    )
                    lock_directory_created = True
                except FileExistsError:
                    lock_directory_created = False
            else:
                lock_directory_created = False
            lock_directory_descriptor = os.open(
                ATTEMPT_LOCK_DIRECTORY,
                directory_flags,
                dir_fd=root_descriptor,
            )
            if lock_directory_created:
                os.fchmod(lock_directory_descriptor, DIRECTORY_MODE)
            self._validate_private_directory_info(
                os.fstat(lock_directory_descriptor)
            )
            current = os.stat(
                ATTEMPT_LOCK_DIRECTORY,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
            self._validate_private_directory_info(current)
            held = os.fstat(lock_directory_descriptor)
            if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
                raise StateError("attempt evidence lock directory was substituted")
            yield lock_directory_descriptor
            rebound = os.stat(
                ATTEMPT_LOCK_DIRECTORY,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
            self._validate_private_directory_info(rebound)
            if (held.st_dev, held.st_ino) != (rebound.st_dev, rebound.st_ino):
                raise StateError("attempt evidence lock directory changed while open")
        except StateError:
            raise
        except OSError as error:
            raise StateError(
                "attempt evidence lock directory could not be opened safely"
            ) from error
        finally:
            if lock_directory_descriptor is not None:
                os.close(lock_directory_descriptor)
            if root_descriptor is not None:
                os.close(root_descriptor)

    def _validate_attempt_lock_binding(
        self,
        job_id: str,
        attempt_id: str,
        descriptor: int,
        expected_identity: tuple[int, int, int],
    ) -> None:
        lock_name = f"{job_id}-{attempt_id}.lock"
        held = os.fstat(descriptor)
        self._validate_private_artifact_info(held)
        if (held.st_dev, held.st_ino, held.st_uid) != expected_identity:
            raise StateError("attempt evidence lock does not match durable identity")
        with self._attempt_lock_directory(create=False) as lock_directory_descriptor:
            current = os.stat(
                lock_name,
                dir_fd=lock_directory_descriptor,
                follow_symlinks=False,
            )
            self._validate_private_artifact_info(current)
            if (
                current.st_dev,
                current.st_ino,
                current.st_uid,
            ) != expected_identity:
                raise StateError("attempt evidence lock pathname changed")

    @contextmanager
    def _provision_attempt_lock(
        self,
        job_id: str,
        attempt_id: str,
        *,
        allow_existing: bool,
    ) -> Iterator[_ProvisionedAttemptLock]:
        """Keep one canonical lock exclusively held through durable publication."""

        job_id = self._lock_identifier(job_id, "job id")
        attempt_id = self._lock_identifier(attempt_id, "attempt id")
        lock_name = f"{job_id}-{attempt_id}.lock"
        file_flags = os.O_RDWR | os.O_NOFOLLOW
        file_flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor: int | None = None
        with self._attempt_lock_directory(create=True) as lock_directory_descriptor:
            provision: _ProvisionedAttemptLock | None = None
            try:
                try:
                    descriptor = os.open(
                        lock_name,
                        file_flags | os.O_CREAT | os.O_EXCL,
                        FILE_MODE,
                        dir_fd=lock_directory_descriptor,
                    )
                    os.fchmod(descriptor, FILE_MODE)
                    os.fsync(descriptor)
                    os.fsync(lock_directory_descriptor)
                    created = True
                except FileExistsError:
                    if not allow_existing:
                        raise StateError("attempt evidence lock already exists")
                    descriptor = os.open(
                        lock_name,
                        file_flags,
                        dir_fd=lock_directory_descriptor,
                    )
                    created = False
                information = os.fstat(descriptor)
                self._validate_private_artifact_info(information)
                identity = self._lock_identity_from_stat(information)
                provision = _ProvisionedAttemptLock(
                    descriptor=descriptor,
                    device=identity[0],
                    inode=identity[1],
                    owner=identity[2],
                    created=created,
                )
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                self._validate_attempt_lock_binding(
                    job_id,
                    attempt_id,
                    descriptor,
                    identity,
                )
                yield provision
                self._validate_attempt_lock_binding(
                    job_id,
                    attempt_id,
                    descriptor,
                    identity,
                )
            except StateError:
                raise
            except OSError as error:
                raise StateError(
                    "attempt evidence lock could not be provisioned safely"
                ) from error
            finally:
                if (
                    provision is not None
                    and provision.created
                    and not provision.published
                    and not self._attempt_lock_has_durable_publication(attempt_id)
                ):
                    try:
                        current = os.stat(
                            lock_name,
                            dir_fd=lock_directory_descriptor,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        pass
                    else:
                        if self._lock_identity_from_stat(current) == (
                            provision.device,
                            provision.inode,
                            provision.owner,
                        ):
                            os.unlink(lock_name, dir_fd=lock_directory_descriptor)
                            os.fsync(lock_directory_descriptor)
                if descriptor is not None:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    finally:
                        os.close(descriptor)

    def _durable_attempt_lock_identity(
        self,
        job_id: str,
        attempt_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[int, int, int]:
        def read(active: sqlite3.Connection) -> tuple[int, int, int]:
            row = active.execute(
                """
                SELECT attempts.job_id, locks.device_hex, locks.inode_hex,
                       locks.owner
                FROM attempts
                LEFT JOIN attempt_evidence_locks AS locks
                    ON locks.attempt_id = attempts.id
                WHERE attempts.id = ?
                """,
                (attempt_id,),
            ).fetchone()
            if row is None or row["job_id"] != job_id:
                raise AttemptNotFound(
                    "attempt does not belong to the requested job"
                )
            if row["device_hex"] is None:
                raise StateError("attempt evidence lock identity is not provisioned")
            return self._lock_identity_from_row(row)

        if connection is not None:
            return read(connection)
        with self.connection() as active:
            return read(active)

    def _attempt_lock_has_durable_publication(self, attempt_id: str) -> bool:
        """Conservatively retain a lock whenever durable publication may exist."""

        try:
            with self.connection() as connection:
                return connection.execute(
                    """
                    SELECT 1
                    FROM attempt_evidence_locks
                    WHERE attempt_id = ?
                    """,
                    (attempt_id,),
                ).fetchone() is not None
        except (OSError, sqlite3.Error, StateError):
            # Cleanup is an availability optimization. If durable state cannot be
            # consulted safely, retaining an orphan is preferable to unlinking a
            # lock whose identity may already have committed.
            return True

    def _bootstrap_attempt_locks(self) -> None:
        """Idempotently pin locks for attempts created by schemas before v7."""

        with self.connection() as connection:
            missing = connection.execute(
                """
                SELECT attempts.job_id, attempts.id
                FROM attempts
                LEFT JOIN attempt_evidence_locks AS locks
                    ON locks.attempt_id = attempts.id
                WHERE locks.attempt_id IS NULL
                ORDER BY attempts.job_id, attempts.number
                """
            ).fetchall()
        for row in missing:
            job_id = row["job_id"]
            attempt_id = row["id"]
            with self._provision_attempt_lock(
                job_id,
                attempt_id,
                allow_existing=True,
            ) as provision:
                identity = (
                    provision.device,
                    provision.inode,
                    provision.owner,
                )
                with self._transaction() as connection:
                    attempt = connection.execute(
                        "SELECT job_id FROM attempts WHERE id = ?",
                        (attempt_id,),
                    ).fetchone()
                    if attempt is None or attempt["job_id"] != job_id:
                        raise AttemptNotFound(
                            "attempt does not belong to the requested job"
                        )
                    pinned = connection.execute(
                        """
                        SELECT device_hex, inode_hex, owner
                        FROM attempt_evidence_locks
                        WHERE attempt_id = ?
                        """,
                        (attempt_id,),
                    ).fetchone()
                    if pinned is None:
                        self._insert_attempt_lock_identity(
                            connection,
                            attempt_id,
                            identity,
                        )
                    elif self._lock_identity_from_row(pinned) != identity:
                        raise StateError(
                            "attempt evidence lock changed during bootstrap"
                        )
                provision.published = True

    def _validate_attempt_lock_durable(
        self,
        job_id: str,
        attempt_id: str,
        descriptor: int,
        expected_identity: tuple[int, int, int],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if (
            self._durable_attempt_lock_identity(
                job_id,
                attempt_id,
                connection=connection,
            )
            != expected_identity
        ):
            raise StateError("attempt evidence lock durable identity changed")
        self._validate_attempt_lock_binding(
            job_id,
            attempt_id,
            descriptor,
            expected_identity,
        )

    @contextmanager
    def _attempt_lock(
        self,
        job_id: str,
        attempt_id: str,
        *,
        exclusive: bool,
        expected_identity: tuple[int, int, int],
    ) -> Iterator[_AttemptEvidenceLease]:
        """Hold one persistent, private per-attempt advisory lock."""

        job_id = self._lock_identifier(job_id, "job id")
        attempt_id = self._lock_identifier(attempt_id, "attempt id")
        file_flags = os.O_RDWR | os.O_NOFOLLOW
        file_flags |= getattr(os, "O_CLOEXEC", 0)
        lock_descriptor: int | None = None
        lease: _AttemptEvidenceLease | None = None
        lock_name = f"{job_id}-{attempt_id}.lock"
        directory_context = self._attempt_lock_directory(create=False)
        directory_entered = False
        acquired = False
        try:
            lock_directory_descriptor = directory_context.__enter__()
            directory_entered = True
            lock_descriptor = os.open(
                lock_name,
                file_flags,
                dir_fd=lock_directory_descriptor,
            )
            self._validate_attempt_lock_binding(
                job_id,
                attempt_id,
                lock_descriptor,
                expected_identity,
            )
            fcntl.flock(
                lock_descriptor,
                fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
            )
            acquired = True
            self._validate_attempt_lock_durable(
                job_id,
                attempt_id,
                lock_descriptor,
                expected_identity,
            )
            lease = _AttemptEvidenceLease(
                store_root=self.root_path,
                job_id=job_id,
                attempt_id=attempt_id,
                descriptor=lock_descriptor,
                exclusive=exclusive,
                device=expected_identity[0],
                inode=expected_identity[1],
                owner=expected_identity[2],
                directory_context=directory_context,
            )
            try:
                yield lease
            finally:
                try:
                    self._release_evidence_lease(
                        lease,
                        validate_binding=True,
                    )
                finally:
                    lock_descriptor = None
                    acquired = False
                    directory_entered = False
        except StateError:
            raise
        except OSError as error:
            raise StateError(
                "attempt evidence lock could not be acquired safely"
            ) from error
        finally:
            try:
                if lock_descriptor is not None:
                    try:
                        if acquired:
                            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
                    finally:
                        os.close(lock_descriptor)
            finally:
                if directory_entered:
                    directory_context.__exit__(None, None, None)

    @contextmanager
    def attempt_evidence_lease(
        self,
        job_id: str,
        attempt_id: str,
        *,
        exclusive: bool,
        require_mutable: bool = True,
    ) -> Iterator[_AttemptEvidenceLease]:
        """Serialize attempt evidence writers against terminal publication."""

        if type(exclusive) is not bool or type(require_mutable) is not bool:
            raise StateError("attempt evidence lease mode is invalid")
        lock_identity = self._durable_attempt_lock_identity(job_id, attempt_id)
        with self._attempt_lock(
            job_id,
            attempt_id,
            exclusive=exclusive,
            expected_identity=lock_identity,
        ) as lease:
            with self._transaction() as connection:
                current = connection.execute(
                    "SELECT id, job_id, status FROM attempts WHERE id = ?",
                    (attempt_id,),
                ).fetchone()
            if current is None or current["job_id"] != job_id:
                raise AttemptNotFound("attempt does not belong to the requested job")
            if require_mutable and current["status"] in TERMINAL_STATUSES:
                raise InvalidTransition("terminal attempt evidence is immutable")
            yield lease

    def _validate_evidence_lease(
        self,
        lease: _AttemptEvidenceLease,
        *,
        job_id: str,
        attempt_id: str,
        require_exclusive: bool = False,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if (
            not isinstance(lease, _AttemptEvidenceLease)
            or lease.store_root != self.root_path
            or lease.job_id != job_id
            or lease.attempt_id != attempt_id
            or (require_exclusive and not lease.exclusive)
        ):
            raise StateError("attempt evidence lease does not match the operation")
        expected_identity = (lease.device, lease.inode, lease.owner)
        if lease.descriptor is None:
            raise StateError("attempt evidence lease is no longer held")
        try:
            self._validate_attempt_lock_durable(
                job_id,
                attempt_id,
                lease.descriptor,
                expected_identity,
                connection=connection,
            )
        except OSError as error:
            raise StateError("attempt evidence lease is no longer held") from error

    def _release_evidence_lease(
        self,
        lease: _AttemptEvidenceLease,
        *,
        validate_binding: bool,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        """Unlock and close one lease exactly once."""

        if not isinstance(lease, _AttemptEvidenceLease):
            raise StateError("attempt evidence lease does not match the operation")
        descriptor = lease.descriptor
        directory_context = lease.directory_context
        if descriptor is None and directory_context is None:
            return
        try:
            if descriptor is not None:
                if validate_binding:
                    self._validate_evidence_lease(
                        lease,
                        job_id=lease.job_id,
                        attempt_id=lease.attempt_id,
                        connection=connection,
                    )
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError as error:
            raise StateError("attempt evidence lease could not be released safely") from error
        finally:
            try:
                if descriptor is not None:
                    os.close(descriptor)
            finally:
                try:
                    object.__setattr__(lease, "descriptor", None)
                    if directory_context is not None:
                        directory_context.__exit__(None, None, None)
                finally:
                    object.__setattr__(lease, "directory_context", None)

    @contextmanager
    def _terminal_transaction(
        self,
        lease: _AttemptEvidenceLease,
    ) -> Iterator[sqlite3.Connection]:
        """Commit terminal state only after the exclusive evidence lease unlocks."""

        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                self._validate_evidence_lease(
                    lease,
                    job_id=lease.job_id,
                    attempt_id=lease.attempt_id,
                    require_exclusive=True,
                    connection=connection,
                )
                self._release_evidence_lease(
                    lease,
                    validate_binding=False,
                    connection=connection,
                )
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise

    @contextmanager
    def _artifact_mutation_lease(
        self,
        job_id: str,
        relative: Path,
        lease: _AttemptEvidenceLease | None,
    ) -> Iterator[None]:
        if len(relative.parts) < 2:
            if lease is not None:
                raise StateError("attempt evidence lease cannot guard a job artifact")
            yield
            return
        attempt_id = relative.parts[0]
        with self.connection() as connection:
            row = connection.execute(
                "SELECT id FROM attempts WHERE id = ? AND job_id = ?",
                (attempt_id, job_id),
            ).fetchone()
        if row is None:
            if lease is not None:
                raise StateError("attempt evidence lease target does not exist")
            yield
            return
        if lease is not None:
            self._validate_evidence_lease(
                lease,
                job_id=job_id,
                attempt_id=attempt_id,
            )
            with self.connection() as connection:
                current = connection.execute(
                    "SELECT status FROM attempts WHERE id = ? AND job_id = ?",
                    (attempt_id, job_id),
                ).fetchone()
            if current is None:
                raise AttemptNotFound("attempt does not belong to the requested job")
            if current["status"] in TERMINAL_STATUSES:
                raise InvalidTransition("terminal attempt evidence is immutable")
            yield
            return
        with self.attempt_evidence_lease(
            job_id,
            attempt_id,
            exclusive=False,
        ):
            yield

    def _secure_sqlite_files(self) -> None:
        for suffix in ("", "-wal", "-shm", "-journal"):
            candidate = self.path.with_name(self.path.name + suffix)
            if not suffix:
                candidate.chmod(FILE_MODE)
                continue
            try:
                candidate.chmod(FILE_MODE)
            except FileNotFoundError:
                # SQLite removes transient sidecars during connection teardown.
                # A disappeared sidecar must not turn a successful operation into
                # an apparent failure at context-manager exit.
                pass

    @staticmethod
    def _context_envelope(context: Any) -> tuple[str, str]:
        if not isinstance(context, dict):
            raise StateError("attempt context must be an object")
        encoded = canonical_json(context)
        if len(encoded.encode("utf-8")) > MAX_COMMAND_PAYLOAD_BYTES:
            raise StateError("attempt context exceeds its storage bound")
        return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalized_lane_reservation(value: Any) -> dict[str, Any]:
        required = {
            "repo_id",
            "base_sha",
            "lane",
            "weight",
            "writer",
            "files",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise StateError("lane reservation fields are malformed")
        result = dict(value)
        if (
            not isinstance(result["repo_id"], str)
            or not result["repo_id"]
            or not isinstance(result["base_sha"], str)
            or not 7 <= len(result["base_sha"]) <= 64
            or result["lane"] not in {"review", "feature", "integration", "recovery"}
            or result["weight"] not in {"LIGHT", "HEAVY"}
            or (
                result["writer"] is not None
                and (not isinstance(result["writer"], str) or not result["writer"])
            )
            or not isinstance(result["files"], list)
            or len(result["files"]) > 256
            or len(result["files"]) != len(set(result["files"]))
            or any(
                not isinstance(path, str) or not path or "\x00" in path
                for path in result["files"]
            )
        ):
            raise StateError("lane reservation values are malformed")
        result["files"] = list(result["files"])
        return result

    @staticmethod
    def _bridge_context_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
        prompt = payload.get("prompt")
        baseline = payload.get("baseline")
        evidence = payload.get("admission_evidence")
        if (
            not isinstance(prompt, str)
            or not prompt
            or not isinstance(baseline, dict)
            or not isinstance(evidence, dict)
        ):
            raise StateError("active legacy bridge job has no valid attempt context")
        review_controls = evidence.get("review_controls", {})
        if not isinstance(review_controls, dict):
            raise StateError("active legacy bridge review controls are malformed")
        scheduler = evidence.get("scheduler_snapshot", {})
        repository = scheduler.get("repository", {}) if isinstance(scheduler, dict) else {}
        dirty_files = repository.get("dirty_paths", []) if isinstance(repository, dict) else []
        if not isinstance(dirty_files, list):
            raise StateError("active legacy bridge dirty-file context is malformed")
        return {
            "version": 1,
            "prompt": prompt,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "baseline": baseline,
            "baseline_sha256": hashlib.sha256(
                canonical_json(baseline).encode("utf-8")
            ).hexdigest(),
            "review_controls": review_controls,
            "review_controls_sha256": hashlib.sha256(
                canonical_json(review_controls).encode("utf-8")
            ).hexdigest(),
            "dirty_files": dirty_files,
            "dirty_files_sha256": hashlib.sha256(
                canonical_json(dirty_files).encode("utf-8")
            ).hexdigest(),
            "request_hash": payload.get("request_hash"),
        }

    def _backfill_v12(self, connection: sqlite3.Connection) -> None:
        """Backfill valid bridge state; fail closed for malformed active bridge jobs."""

        rows = connection.execute("SELECT * FROM jobs").fetchall()
        for row in rows:
            active = row["status"] not in TERMINAL_STATUSES
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                if active:
                    raise SchemaMigrationError(
                        "active legacy bridge job payload is malformed"
                    ) from None
                continue
            # Store-level fixtures are not bridge jobs. A bridge payload is
            # identified by its pinned executable and admission evidence.
            if not isinstance(payload, dict) or "claude_executable" not in payload:
                continue
            try:
                context = self._bridge_context_from_payload(payload)
                evidence = payload["admission_evidence"]
                reservation = dict(evidence["reservation"])
                reservation["base_sha"] = payload["baseline"]["head"]
                reservation = self._normalized_lane_reservation(reservation)
                claimed = evidence.get("sha256")
                unsigned = dict(evidence)
                unsigned.pop("sha256", None)
                if claimed != hashlib.sha256(
                    canonical_json(unsigned).encode("utf-8")
                ).hexdigest():
                    raise StateError("legacy admission evidence hash is invalid")
            except (KeyError, StateError, TypeError, UnicodeError):
                if active:
                    raise SchemaMigrationError(
                        "active legacy bridge job lacks a valid reservation/context"
                    ) from None
                continue
            context_json, context_hash = self._context_envelope(context)
            connection.execute(
                "INSERT OR IGNORE INTO attempt_contexts(attempt_id, context_json, context_hash) "
                "VALUES (?, ?, ?)",
                (row["current_attempt_id"], context_json, context_hash),
            )
            admission_json = canonical_json(evidence)
            now = row["updated_at"]
            connection.execute(
                """
                INSERT OR IGNORE INTO lane_reservations(
                    lease_id, kind, job_id, attempt_id, repo_id, base_sha, lane,
                    weight, writer, files_json, revision, owner_token_hash,
                    receipt_pin_json, admission_json, admission_hash,
                    last_operation_hash, last_command_id, ttl_seconds,
                    heartbeat_at, stale_at, released_at, created_at, updated_at
                ) VALUES (?, 'bridge', ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL,
                          ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)
                """,
                (
                    f"bridge-{row['id']}",
                    row["id"],
                    row["current_attempt_id"],
                    reservation["repo_id"],
                    reservation["base_sha"],
                    reservation["lane"],
                    reservation["weight"],
                    reservation["writer"],
                    canonical_json(reservation["files"]),
                    admission_json,
                    hashlib.sha256(admission_json.encode("utf-8")).hexdigest(),
                    row["created_at"],
                    now,
                ),
            )

    def _migrate(self, connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    version INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_version(singleton, version) VALUES (1, 0)"
            )
            row = connection.execute(
                "SELECT version FROM schema_version WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise SchemaMigrationError("schema_version singleton is missing")
            version = row[0]
            if not isinstance(version, int) or version < 0:
                raise SchemaMigrationError(f"invalid schema version: {version!r}")
            if version > SCHEMA_VERSION:
                raise SchemaMigrationError(
                    f"database schema {version} is newer than supported {SCHEMA_VERSION}"
                )
            if version < 1:
                for statement in _SCHEMA_V1:
                    connection.execute(statement)
                connection.execute(
                    "UPDATE schema_version SET version = 1 WHERE singleton = 1"
                )
                version = 1
            if version < 2:
                for statement in _SCHEMA_V2:
                    connection.execute(statement)
                connection.execute(
                    "UPDATE schema_version SET version = 2 WHERE singleton = 1"
                )
                version = 2
            if version < 3:
                for statement in _SCHEMA_V3:
                    connection.execute(statement)
                connection.execute(
                    "UPDATE schema_version SET version = 3 WHERE singleton = 1"
                )
                version = 3
            if version < 4:
                for statement in _SCHEMA_V4:
                    connection.execute(statement)
                connection.execute(
                    "UPDATE schema_version SET version = 4 WHERE singleton = 1"
                )
                version = 4
            if version < 5:
                for statement in _SCHEMA_V5:
                    connection.execute(statement)
                connection.execute(
                    "UPDATE schema_version SET version = 5 WHERE singleton = 1"
                )
                version = 5
            if version < 6:
                for statement in _SCHEMA_V6:
                    connection.execute(statement)
                connection.execute(
                    "UPDATE schema_version SET version = 6 WHERE singleton = 1"
                )
                version = 6
            if version < 7:
                for statement in _SCHEMA_V7:
                    connection.execute(statement)
                connection.execute(
                    "UPDATE schema_version SET version = 7 WHERE singleton = 1"
                )
                version = 7
            if version < 8:
                for statement in _SCHEMA_V8:
                    connection.execute(statement)
                connection.execute(
                    "UPDATE schema_version SET version = 8 WHERE singleton = 1"
                )
                version = 8
            if version < 9:
                for statement in _SCHEMA_V9:
                    connection.execute(statement)
                connection.execute(
                    "UPDATE schema_version SET version = 9 WHERE singleton = 1"
                )
                version = 9
            if version < 10:
                for statement in _SCHEMA_V10:
                    connection.execute(statement)
                connection.execute(
                    "UPDATE schema_version SET version = 10 WHERE singleton = 1"
                )
                version = 10
            if version < 11:
                for statement in _SCHEMA_V11:
                    connection.execute(statement)
                connection.execute(
                    "UPDATE schema_version SET version = 11 WHERE singleton = 1"
                )
                version = 11
            if version < 12:
                for statement in _SCHEMA_V12:
                    connection.execute(statement)
                self._backfill_v12(connection)
                connection.execute(
                    "UPDATE schema_version SET version = 12 WHERE singleton = 1"
                )
                version = 12
            admission_columns = [
                (row["name"], row["type"], row["notnull"], row["pk"])
                for row in connection.execute(
                    "PRAGMA table_info(attempt_admissions)"
                ).fetchall()
            ]
            admission_foreign_keys = [
                (
                    row["table"],
                    row["from"],
                    row["to"],
                    row["on_update"],
                    row["on_delete"],
                )
                for row in connection.execute(
                    "PRAGMA foreign_key_list(attempt_admissions)"
                ).fetchall()
            ]
            admission_sql_row = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'attempt_admissions'"
            ).fetchone()
            admission_sql = (
                "" if admission_sql_row is None else admission_sql_row["sql"] or ""
            ).casefold()
            if (
                admission_columns
                != [
                    ("attempt_id", "TEXT", 0, 1),
                    ("evidence_json", "TEXT", 1, 0),
                    ("evidence_hash", "TEXT", 1, 0),
                ]
                or admission_foreign_keys
                != [("attempts", "attempt_id", "id", "NO ACTION", "CASCADE")]
                or "length(evidence_hash) = 64" not in admission_sql
                or "evidence_hash not glob '*[^0-9a-f]*'" not in admission_sql
            ):
                raise SchemaMigrationError(
                    "attempt admission schema does not match version 10"
                )
            if version != SCHEMA_VERSION:
                raise SchemaMigrationError(
                    f"no migration path from schema {version} to {SCHEMA_VERSION}"
                )
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        else:
            connection.commit()

    def create_job(
        self,
        *,
        repo_id: str,
        idempotency_key: str,
        request_hash: str,
        payload: Any,
    ) -> Job:
        """Create a job and its first attempt, or return an identical prior job."""
        payload_json = canonical_json(payload)
        job_id = uuid.uuid4().hex
        attempt_id = uuid.uuid4().hex
        with self._provision_attempt_lock(
            job_id,
            attempt_id,
            allow_existing=False,
        ) as provision:
            identity = (
                provision.device,
                provision.inode,
                provision.owner,
            )
            published = False
            with self._transaction() as connection:
                existing = connection.execute(
                    "SELECT * FROM jobs WHERE repo_id = ? AND idempotency_key = ?",
                    (repo_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["request_hash"] != request_hash
                        or existing["payload_json"] != payload_json
                    ):
                        raise IdempotencyConflict(
                            "idempotency key already belongs to a different request: "
                            f"repo={repo_id!r} key={idempotency_key!r}"
                        )
                    result = self._job_from_row(existing)
                else:
                    now = _utc_now()
                    connection.execute(
                        """
                        INSERT INTO jobs(
                            id, repo_id, idempotency_key, request_hash, payload_json,
                            status, revision, current_attempt_id, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 'CREATED', 0, NULL, ?, ?)
                        """,
                        (
                            job_id,
                            repo_id,
                            idempotency_key,
                            request_hash,
                            payload_json,
                            now,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO attempts(
                            id, job_id, number, parent_attempt_id, status,
                            revision, created_at, updated_at
                        ) VALUES (?, ?, 1, NULL, 'CREATED', 0, ?, ?)
                        """,
                        (attempt_id, job_id, now, now),
                    )
                    self._insert_attempt_lock_identity(
                        connection,
                        attempt_id,
                        identity,
                    )
                    connection.execute(
                        "UPDATE jobs SET current_attempt_id = ? WHERE id = ?",
                        (attempt_id, job_id),
                    )
                    created = connection.execute(
                        "SELECT * FROM jobs WHERE id = ?", (job_id,)
                    ).fetchone()
                    result = self._job_from_row(created)
                    published = True
            if published:
                provision.published = True
            return result

    @contextmanager
    def _admission_lock(self) -> Iterator[None]:
        """Hold the private cross-process scheduler lock by inode identity."""

        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        file_flags = os.O_RDWR | os.O_NOFOLLOW
        file_flags |= getattr(os, "O_CLOEXEC", 0)
        root_descriptor: int | None = None
        descriptor: int | None = None
        body_started = False
        try:
            root_descriptor = os.open(self.root_path, directory_flags)
            root = os.fstat(root_descriptor)
            if (
                not stat.S_ISDIR(root.st_mode)
                or root.st_uid != os.geteuid()
                or stat.S_IMODE(root.st_mode) != DIRECTORY_MODE
            ):
                raise StateError("admission lock root is not private")
            try:
                descriptor = os.open(
                    ADMISSION_LOCK_FILE,
                    file_flags | os.O_CREAT | os.O_EXCL,
                    FILE_MODE,
                    dir_fd=root_descriptor,
                )
            except FileExistsError:
                descriptor = os.open(
                    ADMISSION_LOCK_FILE,
                    file_flags,
                    dir_fd=root_descriptor,
                )
            os.fchmod(descriptor, FILE_MODE)
            held = os.fstat(descriptor)
            current = os.stat(
                ADMISSION_LOCK_FILE,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(held.st_mode)
                or held.st_uid != os.geteuid()
                or held.st_nlink != 1
                or stat.S_IMODE(held.st_mode) != FILE_MODE
                or (held.st_dev, held.st_ino, held.st_uid)
                != (current.st_dev, current.st_ino, current.st_uid)
            ):
                raise StateError("admission lock identity is invalid")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            rebound = os.stat(
                ADMISSION_LOCK_FILE,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
            if (held.st_dev, held.st_ino, held.st_uid) != (
                rebound.st_dev,
                rebound.st_ino,
                rebound.st_uid,
            ):
                raise StateError("admission lock changed while held")
            body_started = True
            yield
            body_started = False
        except StateError:
            raise
        except OSError as error:
            if body_started:
                raise
            raise StateError(
                "admission lock could not be acquired safely: "
                f"{error.__class__.__name__} errno={error.errno}"
            ) from error
        finally:
            if descriptor is not None:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
            if root_descriptor is not None:
                os.close(root_descriptor)

    def create_admitted_job(
        self,
        *,
        repo_id: str,
        idempotency_key: str,
        request_hash: str,
        build_payload: Callable[[str, str, list[dict[str, Any]]], Any],
    ) -> tuple[Job, bool]:
        """Resolve replay first, then admit and publish one reservation atomically."""

        job_id = uuid.uuid4().hex
        attempt_id = uuid.uuid4().hex
        with self._provision_attempt_lock(
            job_id,
            attempt_id,
            allow_existing=False,
        ) as provision:
            identity = (provision.device, provision.inode, provision.owner)
            published = False
            with self._admission_lock():
                with self._transaction() as connection:
                    existing = connection.execute(
                        "SELECT * FROM jobs WHERE repo_id = ? AND idempotency_key = ?",
                        (repo_id, idempotency_key),
                    ).fetchone()
                    if existing is not None:
                        if existing["request_hash"] != request_hash:
                            raise IdempotencyConflict(
                                "idempotency key already belongs to a different request: "
                                f"repo={repo_id!r} key={idempotency_key!r}"
                            )
                        return self._job_from_row(existing), True

                    active_reservations = self._active_lane_reservations(connection)
                    payload = build_payload(job_id, attempt_id, active_reservations)
                    attempt_admission_json, attempt_admission_hash = (
                        self._admission_envelope(payload.get("admission_evidence"))
                        if isinstance(payload, dict)
                        else self._admission_envelope(None)
                    )
                    payload_json = canonical_json(payload)
                    now = _utc_now()
                    connection.execute(
                        """
                        INSERT INTO jobs(
                            id, repo_id, idempotency_key, request_hash, payload_json,
                            status, revision, current_attempt_id, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 'CREATED', 0, NULL, ?, ?)
                        """,
                        (
                            job_id,
                            repo_id,
                            idempotency_key,
                            request_hash,
                            payload_json,
                            now,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO attempts(
                            id, job_id, number, parent_attempt_id, status,
                            revision, created_at, updated_at
                        ) VALUES (?, ?, 1, NULL, 'CREATED', 0, ?, ?)
                        """,
                        (attempt_id, job_id, now, now),
                    )
                    self._insert_attempt_lock_identity(connection, attempt_id, identity)
                    connection.execute(
                        """
                        INSERT INTO attempt_admissions(
                            attempt_id, evidence_json, evidence_hash
                        ) VALUES (?, ?, ?)
                        """,
                        (attempt_id, attempt_admission_json, attempt_admission_hash),
                    )
                    if not isinstance(payload, dict):
                        raise StateError("admitted job payload must be an object")
                    context = self._bridge_context_from_payload(payload)
                    self._insert_attempt_context(connection, attempt_id, context)
                    evidence = payload["admission_evidence"]
                    reservation = dict(evidence["reservation"])
                    reservation["base_sha"] = payload["baseline"]["head"]
                    reservation = self._normalized_lane_reservation(reservation)
                    lane_admission_json, lane_admission_hash = (
                        self._lane_admission_envelope(evidence)
                    )
                    connection.execute(
                        """
                        INSERT INTO lane_reservations(
                            lease_id, kind, job_id, attempt_id, repo_id, base_sha,
                            lane, weight, writer, files_json, revision,
                            owner_token_hash, receipt_pin_json, admission_json,
                            admission_hash, last_operation_hash, last_command_id,
                            ttl_seconds, heartbeat_at, stale_at, released_at,
                            created_at, updated_at
                        ) VALUES (?, 'bridge', ?, ?, ?, ?, ?, ?, ?, ?, 0,
                                  NULL, NULL, ?, ?, NULL, NULL, NULL, NULL, NULL,
                                  NULL, ?, ?)
                        """,
                        (
                            f"bridge-{job_id}",
                            job_id,
                            attempt_id,
                            reservation["repo_id"],
                            reservation["base_sha"],
                            reservation["lane"],
                            reservation["weight"],
                            reservation["writer"],
                            canonical_json(reservation["files"]),
                            lane_admission_json,
                            lane_admission_hash,
                            now,
                            now,
                        ),
                    )
                    connection.execute(
                        "UPDATE jobs SET current_attempt_id = ? WHERE id = ?",
                        (attempt_id, job_id),
                    )
                    created = connection.execute(
                        "SELECT * FROM jobs WHERE id = ?", (job_id,)
                    ).fetchone()
                    result = self._job_from_row(created)
                    published = True
            if published:
                provision.published = True
            return result, False

    @staticmethod
    def _admission_envelope(evidence: Any) -> tuple[str, str]:
        if not isinstance(evidence, dict):
            raise StateError("attempt admission evidence must be an object")
        claimed_hash = evidence.get("sha256")
        without_hash = dict(evidence)
        without_hash.pop("sha256", None)
        actual_hash = hashlib.sha256(
            canonical_json(without_hash).encode("utf-8")
        ).hexdigest()
        if claimed_hash != actual_hash:
            raise StateError("attempt admission evidence hash is invalid")
        return canonical_json(evidence), actual_hash

    def get_attempt_admission(self, attempt_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT evidence_json, evidence_hash FROM attempt_admissions "
                "WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        if row is None:
            raise StateError("attempt has no durable admission evidence")
        try:
            evidence = json.loads(row["evidence_json"])
        except (TypeError, json.JSONDecodeError) as error:
            raise StateError("stored attempt admission evidence is malformed") from error
        canonical, evidence_hash = self._admission_envelope(evidence)
        if canonical != row["evidence_json"] or evidence_hash != row["evidence_hash"]:
            raise StateError("stored attempt admission evidence does not match its hash")
        return evidence

    def _insert_attempt_context(
        self,
        connection: sqlite3.Connection,
        attempt_id: str,
        context: dict[str, Any],
    ) -> str:
        context_json, context_hash = self._context_envelope(context)
        connection.execute(
            "INSERT INTO attempt_contexts(attempt_id, context_json, context_hash) "
            "VALUES (?, ?, ?)",
            (attempt_id, context_json, context_hash),
        )
        return context_hash

    def get_attempt_context(self, attempt_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT context_json, context_hash FROM attempt_contexts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        if row is None:
            raise StateError("attempt has no durable execution context")
        try:
            context = json.loads(row["context_json"])
        except (TypeError, json.JSONDecodeError) as error:
            raise StateError("stored attempt context is malformed") from error
        context_json, context_hash = self._context_envelope(context)
        if context_json != row["context_json"] or context_hash != row["context_hash"]:
            raise StateError("stored attempt context does not match its hash")
        result = dict(context)
        result["sha256"] = context_hash
        return result

    @classmethod
    def _lane_reservation_from_row(cls, row: sqlite3.Row) -> dict[str, Any]:
        try:
            files = json.loads(row["files_json"])
            admission = json.loads(row["admission_json"])
        except (TypeError, json.JSONDecodeError) as error:
            raise StateError("stored lane reservation is malformed") from error
        reservation = cls._normalized_lane_reservation(
            {
                "repo_id": row["repo_id"],
                "base_sha": row["base_sha"],
                "lane": row["lane"],
                "weight": row["weight"],
                "writer": row["writer"],
                "files": files,
            }
        )
        if canonical_json(files) != row["files_json"]:
            raise StateError("stored lane file set is not canonical")
        admission_json = canonical_json(admission)
        if (
            admission_json != row["admission_json"]
            or hashlib.sha256(admission_json.encode("utf-8")).hexdigest()
            != row["admission_hash"]
        ):
            raise StateError("stored lane admission does not match its hash")
        stale = False
        if row["kind"] == "native" and row["released_at"] is None:
            try:
                stale = datetime.fromisoformat(row["stale_at"]) <= datetime.now(timezone.utc)
            except (TypeError, ValueError):
                raise StateError("stored native lane expiry is malformed") from None
        return {
            "lease_id": row["lease_id"],
            "kind": row["kind"],
            "job_id": row["job_id"],
            "attempt_id": row["attempt_id"],
            **reservation,
            "revision": row["revision"],
            "status": (
                "RELEASED"
                if row["released_at"] is not None
                else "STALE" if stale else "ACTIVE"
            ),
            "admission_sha256": row["admission_hash"],
            "heartbeat_at": row["heartbeat_at"],
            "stale_at": row["stale_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _active_lane_reservations(
        self,
        connection: sqlite3.Connection,
        *,
        exclude_job_id: str | None = None,
        exclude_lease_id: str | None = None,
    ) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
        uncertain_parameters: list[Any] = list(sorted(TERMINAL_STATUSES))
        excluded = ""
        if exclude_job_id is not None:
            excluded = "AND jobs.id != ?"
            uncertain_parameters.append(exclude_job_id)
        uncertain = connection.execute(
            f"""
            SELECT jobs.id
            FROM jobs
            WHERE jobs.status NOT IN ({placeholders})
              {excluded}
              AND NOT EXISTS (
                    SELECT 1
                    FROM lane_reservations AS lanes
                    WHERE lanes.kind = 'bridge'
                      AND lanes.job_id = jobs.id
                      AND lanes.released_at IS NULL
                  )
            LIMIT 1
            """,
            tuple(uncertain_parameters),
        ).fetchone()
        if uncertain is not None:
            raise StateError(
                "ADMISSION_UNCERTAINTY: active job lacks an authoritative reservation"
            )
        rows = connection.execute(
            f"""
            SELECT lanes.*
            FROM lane_reservations AS lanes
            LEFT JOIN jobs ON jobs.id = lanes.job_id
            WHERE lanes.released_at IS NULL
              AND (
                    lanes.kind = 'native'
                    OR (lanes.kind = 'bridge' AND jobs.status NOT IN ({placeholders}))
                  )
            ORDER BY lanes.created_at, lanes.lease_id
            """,
            tuple(sorted(TERMINAL_STATUSES)),
        ).fetchall()
        return [
            self._lane_reservation_from_row(row)
            for row in rows
            if (exclude_job_id is None or row["job_id"] != exclude_job_id)
            and (exclude_lease_id is None or row["lease_id"] != exclude_lease_id)
        ]

    def list_lane_reservations(self, *, include_released: bool = False) -> list[dict[str, Any]]:
        with self.connection() as connection:
            if include_released:
                rows = connection.execute(
                    "SELECT * FROM lane_reservations ORDER BY created_at, lease_id"
                ).fetchall()
                return [self._lane_reservation_from_row(row) for row in rows]
            return self._active_lane_reservations(connection)

    def get_bridge_lane_reservation(
        self,
        job_id: str,
        *,
        attempt_id: str | None = None,
    ) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM lane_reservations WHERE kind = 'bridge' AND job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise StateError("bridge job has no authoritative lane reservation")
        reservation = self._lane_reservation_from_row(row)
        if attempt_id is not None and reservation["attempt_id"] != attempt_id:
            raise StateError("bridge lane reservation attempt binding changed")
        return reservation

    def get_bridge_expansion_delivery_binding(
        self,
        job_id: str,
        *,
        attempt_id: str,
        command_id: str,
        expansion: dict[str, Any],
    ) -> dict[str, Any]:
        """Read the exact immutable expansion receipt held for live delivery."""

        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM lane_reservations WHERE kind = 'bridge' AND job_id = ?",
                (job_id,),
            ).fetchone()
            command_row = connection.execute(
                "SELECT * FROM commands WHERE id = ? AND job_id = ? AND attempt_id = ?",
                (command_id, job_id, attempt_id),
            ).fetchone()
        if row is None or command_row is None:
            raise StateError("bridge expansion delivery binding is unavailable")
        reservation = self._lane_reservation_from_row(row)
        command = self._command_from_row(command_row)
        if (
            reservation["attempt_id"] != attempt_id
            or reservation["status"] != "ACTIVE"
            or row["last_command_id"] != command_id
            or command.kind != "STEER"
            or command.status != "DELIVERING"
            or command.payload.get("admission_expansion") != expansion
            or row["lease_id"] != expansion.get("lease_id")
            or row["revision"] != expansion.get("lease_revision")
            or row["files_json"] != canonical_json(expansion.get("files"))
            or row["admission_hash"] != expansion.get("admission_sha256")
            or row["last_operation_hash"] != expansion.get("operation_sha256")
        ):
            raise StateError("bridge expansion delivery binding changed")
        return reservation

    @staticmethod
    def _assert_lane_available(
        candidate: dict[str, Any],
        active: list[dict[str, Any]],
    ) -> None:
        if len(active) >= 3:
            raise StateError("LANE_CAP: maximum three active lanes")
        if candidate["weight"] == "HEAVY" and any(
            item["weight"] == "HEAVY" for item in active
        ):
            raise StateError("HEAVY_LEASE: a HEAVY lane is already active")
        if candidate["writer"] is not None and any(
            item["writer"] == candidate["writer"] for item in active
        ):
            raise StateError("DUPLICATE_WRITER: worktree already has an active writer")
        parallel = {
            path
            for item in active
            if item["repo_id"] == candidate["repo_id"]
            for path in item["files"]
        }
        if set(candidate["files"]) & parallel:
            raise StateError("FILE_OVERLAP: declared file is already reserved")

    @staticmethod
    def _native_token_hash(owner_token: str) -> str:
        if (
            not isinstance(owner_token, str)
            or len(owner_token) != 64
            or any(character not in "0123456789abcdef" for character in owner_token)
        ):
            raise StateError("native lane owner capability is invalid")
        return hashlib.sha256(owner_token.encode("ascii")).hexdigest()

    @staticmethod
    def _validate_native_receipt_pin(pin: Any) -> dict[str, Any]:
        fields = {"path", "device", "inode", "owner", "mode", "links", "sha256"}
        if not isinstance(pin, dict) or set(pin) != fields:
            raise StateError("native owner receipt pin is malformed")
        path = Path(pin["path"])
        if not path.is_absolute() or ".." in path.parts:
            raise StateError("native owner receipt path is unsafe")
        try:
            info = path.stat(follow_symlinks=False)
            data = path.read_bytes()
        except OSError as error:
            raise StateError("native owner receipt is unavailable") from error
        if (
            path.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or len(data) > 64 * 1024
            or (info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode), info.st_nlink)
            != (pin["device"], pin["inode"], pin["owner"], pin["mode"], pin["links"])
            or hashlib.sha256(data).hexdigest() != pin["sha256"]
        ):
            raise StateError("native owner receipt identity changed")
        return dict(pin)

    @staticmethod
    def _lane_admission_envelope(admission: Any) -> tuple[str, str]:
        if not isinstance(admission, dict):
            raise StateError("lane admission receipt must be an object")
        encoded = canonical_json(admission)
        return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def reserve_native_lane(
        self,
        *,
        lease_id: str,
        owner_token: str,
        receipt_pin: dict[str, Any],
        reservation: dict[str, Any],
        ttl_seconds: int,
        admission: dict[str, Any] | None = None,
        build_admission: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None,
        operation_context_sha256: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if not isinstance(lease_id, str) or not lease_id or len(lease_id) > 256:
            raise StateError("native lease id is invalid")
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or not 5 <= ttl_seconds <= 86_400
        ):
            raise StateError("native lease TTL is outside its bound")
        token_hash = self._native_token_hash(owner_token)
        if operation_context_sha256 is not None and (
            not isinstance(operation_context_sha256, str)
            or len(operation_context_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in operation_context_sha256
            )
        ):
            raise StateError("native reservation operation context is invalid")
        receipt_pin = self._validate_native_receipt_pin(receipt_pin)
        reservation = self._normalized_lane_reservation(reservation)
        if (admission is None) == (build_admission is None):
            raise StateError("native lane requires exactly one admission source")
        admission_envelope = (
            None if admission is None else self._lane_admission_envelope(admission)
        )
        with self._admission_lock():
            with self._transaction() as connection:
                if reservation["lane"] == "recovery":
                    raise StateError(NATIVE_RECOVERY_UNSUPPORTED)
                existing = connection.execute(
                    "SELECT * FROM lane_reservations WHERE lease_id = ?",
                    (lease_id,),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["kind"] != "native"
                        or not hmac.compare_digest(existing["owner_token_hash"], token_hash)
                        or existing["repo_id"] != reservation["repo_id"]
                        or existing["base_sha"] != reservation["base_sha"]
                        or existing["lane"] != reservation["lane"]
                        or existing["weight"] != reservation["weight"]
                        or existing["writer"] != reservation["writer"]
                        or existing["files_json"] != canonical_json(reservation["files"])
                        or (
                            admission_envelope is not None
                            and existing["admission_hash"] != admission_envelope[1]
                        )
                        or (
                            operation_context_sha256 is not None
                            and existing["last_operation_hash"]
                            != operation_context_sha256
                        )
                        or existing["released_at"] is not None
                    ):
                        raise IdempotencyConflict(
                            "native lease id already belongs to a different reservation"
                        )
                    return self._lane_reservation_from_row(existing), True
                active = self._active_lane_reservations(connection)
                self._assert_lane_available(reservation, active)
                if build_admission is not None:
                    admission_json, admission_hash = self._lane_admission_envelope(
                        build_admission(active)
                    )
                else:
                    assert admission_envelope is not None
                    admission_json, admission_hash = admission_envelope
                now_value = datetime.now(timezone.utc)
                now = now_value.isoformat(timespec="microseconds")
                stale_at = (now_value + timedelta(seconds=ttl_seconds)).isoformat(
                    timespec="microseconds"
                )
                connection.execute(
                    """
                    INSERT INTO lane_reservations(
                        lease_id, kind, job_id, attempt_id, repo_id, base_sha, lane,
                        weight, writer, files_json, revision, owner_token_hash,
                        receipt_pin_json, admission_json, admission_hash,
                        last_operation_hash, last_command_id, ttl_seconds,
                        heartbeat_at, stale_at, released_at, created_at, updated_at
                    ) VALUES (?, 'native', NULL, NULL, ?, ?, ?, ?, ?, ?, 0, ?, ?,
                              ?, ?, ?, NULL, ?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        lease_id,
                        reservation["repo_id"],
                        reservation["base_sha"],
                        reservation["lane"],
                        reservation["weight"],
                        reservation["writer"],
                        canonical_json(reservation["files"]),
                        token_hash,
                        canonical_json(receipt_pin),
                        admission_json,
                        admission_hash,
                        operation_context_sha256,
                        ttl_seconds,
                        now,
                        stale_at,
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM lane_reservations WHERE lease_id = ?", (lease_id,)
                ).fetchone()
                return self._lane_reservation_from_row(row), False

    def _owned_native_lane(
        self,
        connection: sqlite3.Connection,
        lease_id: str,
        owner_token: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM lane_reservations WHERE lease_id = ? AND kind = 'native'",
            (lease_id,),
        ).fetchone()
        token_hash = self._native_token_hash(owner_token)
        if row is None or not hmac.compare_digest(row["owner_token_hash"], token_hash):
            raise StateError("native lane owner capability is invalid")
        try:
            pin = json.loads(row["receipt_pin_json"])
        except (TypeError, json.JSONDecodeError):
            raise StateError("native owner receipt pin is malformed") from None
        self._validate_native_receipt_pin(pin)
        return row

    def heartbeat_native_lane(
        self,
        lease_id: str,
        *,
        owner_token: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        with self._admission_lock():
            with self._transaction() as connection:
                row = self._owned_native_lane(connection, lease_id, owner_token)
                if row["released_at"] is not None:
                    raise InvalidTransition("released native lane cannot heartbeat")
                if row["revision"] != expected_revision:
                    raise RevisionConflict(lease_id, expected_revision, row["revision"])
                now_value = datetime.now(timezone.utc)
                now = now_value.isoformat(timespec="microseconds")
                stale_at = (
                    now_value + timedelta(seconds=row["ttl_seconds"])
                ).isoformat(timespec="microseconds")
                connection.execute(
                    "UPDATE lane_reservations SET revision = revision + 1, heartbeat_at = ?, "
                    "stale_at = ?, updated_at = ? WHERE lease_id = ?",
                    (now, stale_at, now, lease_id),
                )
                updated = connection.execute(
                    "SELECT * FROM lane_reservations WHERE lease_id = ?", (lease_id,)
                ).fetchone()
                return self._lane_reservation_from_row(updated)

    def expand_native_lane(
        self,
        lease_id: str,
        *,
        owner_token: str,
        expected_revision: int,
        prior_files: list[str],
        files: list[str],
        admission: dict[str, Any] | None = None,
        build_admission: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None,
        operation_context_sha256: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if (admission is None) == (build_admission is None):
            raise StateError("native expansion requires exactly one admission source")
        if (
            not isinstance(prior_files, list)
            or not isinstance(files, list)
            or not prior_files
            or len(prior_files) != len(set(prior_files))
            or len(files) != len(set(files))
            or not set(prior_files).issubset(files)
        ):
            raise StateError("native expansion file sets are malformed")
        if operation_context_sha256 is not None and (
            not isinstance(operation_context_sha256, str)
            or len(operation_context_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in operation_context_sha256
            )
        ):
            raise StateError("native expansion operation context is invalid")
        operation_hash = hashlib.sha256(
            canonical_json(
                {
                    "action": "expand",
                    "lease_id": lease_id,
                    "expected_revision": expected_revision,
                    "prior_files": prior_files,
                    "files": files,
                    "operation_context_sha256": operation_context_sha256,
                }
            ).encode("utf-8")
        ).hexdigest()
        with self._admission_lock():
            with self._transaction() as connection:
                row = self._owned_native_lane(connection, lease_id, owner_token)
                if row["lane"] == "recovery":
                    raise StateError(NATIVE_RECOVERY_UNSUPPORTED)
                if row["released_at"] is not None:
                    raise InvalidTransition("released native lane cannot expand")
                current = self._lane_reservation_from_row(row)
                if row["revision"] != expected_revision:
                    # The exact already-published operation is the sole stale replay.
                    if (
                        row["last_operation_hash"] == operation_hash
                        and current["files"] == files
                        and row["revision"] == expected_revision + 1
                    ):
                        return current, True
                    raise RevisionConflict(lease_id, expected_revision, row["revision"])
                if current["files"] != prior_files:
                    raise StateError("native expansion prior file set is stale")
                candidate = self._normalized_lane_reservation(
                    {
                        "repo_id": current["repo_id"],
                        "base_sha": current["base_sha"],
                        "lane": current["lane"],
                        "weight": current["weight"],
                        "writer": current["writer"],
                        "files": files,
                    }
                )
                active = self._active_lane_reservations(
                    connection,
                    exclude_lease_id=lease_id,
                )
                self._assert_lane_available(candidate, active)
                if build_admission is not None:
                    admission_value = build_admission(active)
                else:
                    assert admission is not None
                    admission_value = admission
                admission_json, admission_hash = self._lane_admission_envelope(
                    admission_value
                )
                now = _utc_now()
                connection.execute(
                    """
                    UPDATE lane_reservations
                    SET files_json = ?, revision = revision + 1,
                        admission_json = ?, admission_hash = ?,
                        last_operation_hash = ?, last_command_id = NULL,
                        updated_at = ?
                    WHERE lease_id = ? AND revision = ?
                    """,
                    (
                        canonical_json(files),
                        admission_json,
                        admission_hash,
                        operation_hash,
                        now,
                        lease_id,
                        expected_revision,
                    ),
                )
                updated = connection.execute(
                    "SELECT * FROM lane_reservations WHERE lease_id = ?", (lease_id,)
                ).fetchone()
                return self._lane_reservation_from_row(updated), False

    def release_native_lane(
        self,
        lease_id: str,
        *,
        owner_token: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        with self._admission_lock():
            with self._transaction() as connection:
                row = self._owned_native_lane(connection, lease_id, owner_token)
                if row["revision"] != expected_revision:
                    raise RevisionConflict(lease_id, expected_revision, row["revision"])
                if row["released_at"] is None:
                    now = _utc_now()
                    connection.execute(
                        "UPDATE lane_reservations SET revision = revision + 1, "
                        "released_at = ?, updated_at = ? WHERE lease_id = ?",
                        (now, now, lease_id),
                    )
                updated = connection.execute(
                    "SELECT * FROM lane_reservations WHERE lease_id = ?", (lease_id,)
                ).fetchone()
                return self._lane_reservation_from_row(updated)

    def expand_bridge_lane_and_queue(
        self,
        job_id: str,
        *,
        attempt_id: str,
        expected_job_revision: int,
        lease_id: str,
        expected_lease_revision: int,
        prior_files: list[str],
        files: list[str],
        prompt: str,
        idempotency_key: str,
        operation_hash: str,
        build_admission: Callable[
            [dict[str, Any], dict[str, Any], list[dict[str, Any]]],
            dict[str, Any],
        ],
    ) -> tuple[dict[str, Any], Command, bool]:
        """Re-admit one bridge expansion and queue its prompt atomically."""

        if (
            not isinstance(lease_id, str)
            or not lease_id
            or len(lease_id) > 256
            or isinstance(expected_lease_revision, bool)
            or not isinstance(expected_lease_revision, int)
            or expected_lease_revision < 0
            or not isinstance(prior_files, list)
            or not isinstance(files, list)
            or len(prior_files) > 256
            or len(files) > 256
            or len(prior_files) != len(set(prior_files))
            or len(files) != len(set(files))
            or not set(prior_files).issubset(files)
            or prior_files == files
            or any(
                not isinstance(path, str) or not path or "\x00" in path
                for path in (*prior_files, *files)
            )
            or not isinstance(operation_hash, str)
            or len(operation_hash) != 64
            or any(character not in "0123456789abcdef" for character in operation_hash)
        ):
            raise StateError("bridge expansion declaration is malformed")
        with self._admission_lock():
            with self._transaction() as connection:
                row = connection.execute(
                    "SELECT * FROM lane_reservations "
                    "WHERE lease_id = ? AND kind = 'bridge' AND job_id = ?",
                    (lease_id, job_id),
                ).fetchone()
                if row is None:
                    raise StateError("bridge expansion lease identity is invalid")
                current = self._lane_reservation_from_row(row)
                if row["last_operation_hash"] == operation_hash:
                    if (
                        row["revision"] != expected_lease_revision + 1
                        or current["files"] != files
                        or row["last_command_id"] is None
                    ):
                        raise StateError("stored bridge expansion replay is inconsistent")
                    command_row = connection.execute(
                        "SELECT * FROM commands WHERE id = ? AND job_id = ? AND attempt_id = ?",
                        (row["last_command_id"], job_id, attempt_id),
                    ).fetchone()
                    if command_row is None:
                        raise StateError("stored bridge expansion command is unavailable")
                    return current, self._command_from_row(command_row), True
                if row["revision"] != expected_lease_revision:
                    raise RevisionConflict(
                        lease_id,
                        expected_lease_revision,
                        row["revision"],
                    )
                if row["last_command_id"] is not None:
                    preceding = connection.execute(
                        "SELECT * FROM commands WHERE id = ? AND job_id = ? "
                        "AND attempt_id = ? AND kind = 'STEER'",
                        (row["last_command_id"], job_id, attempt_id),
                    ).fetchone()
                    if preceding is None:
                        raise StateError(
                            "preceding expansion command binding is unavailable"
                        )
                    if preceding["status"] != "DELIVERED":
                        raise StateError(
                            "preceding expansion command must be delivered before re-expansion"
                        )
                job = self._job_row(connection, job_id)
                if (
                    job["current_attempt_id"] != attempt_id
                    or row["attempt_id"] != attempt_id
                ):
                    raise StateError("bridge expansion attempt binding changed")
                if current["files"] != prior_files:
                    raise StateError("bridge expansion prior file set is stale")
                candidate = self._normalized_lane_reservation(
                    {
                        "repo_id": current["repo_id"],
                        "base_sha": current["base_sha"],
                        "lane": current["lane"],
                        "weight": current["weight"],
                        "writer": current["writer"],
                        "files": files,
                    }
                )
                active = self._active_lane_reservations(
                    connection,
                    exclude_lease_id=lease_id,
                )
                self._assert_lane_available(candidate, active)
                try:
                    prior_admission = json.loads(row["admission_json"])
                except (TypeError, json.JSONDecodeError) as error:
                    raise StateError("stored bridge lane admission is malformed") from error
                admission = build_admission(current, prior_admission, active)
                admission_json, admission_hash = self._lane_admission_envelope(
                    admission
                )
                command = self._queue_command_in_transaction(
                    connection,
                    job_id,
                    expected_revision=expected_job_revision,
                    kind="STEER",
                    payload={
                        "prompt": prompt,
                        "admission_expansion": {
                            "lease_id": lease_id,
                            "prior_lease_revision": expected_lease_revision,
                            "lease_revision": expected_lease_revision + 1,
                            "prior_files": list(prior_files),
                            "files": list(files),
                            "admission_sha256": admission_hash,
                            "operation_sha256": operation_hash,
                        },
                    },
                    idempotency_key=idempotency_key,
                )
                now = _utc_now()
                updated = connection.execute(
                    """
                    UPDATE lane_reservations
                    SET files_json = ?, revision = revision + 1,
                        admission_json = ?, admission_hash = ?,
                        last_operation_hash = ?, last_command_id = ?, updated_at = ?
                    WHERE lease_id = ? AND kind = 'bridge' AND job_id = ?
                      AND attempt_id = ? AND revision = ? AND released_at IS NULL
                    """,
                    (
                        canonical_json(files),
                        admission_json,
                        admission_hash,
                        operation_hash,
                        command.id,
                        now,
                        lease_id,
                        job_id,
                        attempt_id,
                        expected_lease_revision,
                    ),
                )
                if updated.rowcount != 1:
                    raise StateError("bridge expansion lost its lease revision")
                published = connection.execute(
                    "SELECT * FROM lane_reservations WHERE lease_id = ?",
                    (lease_id,),
                ).fetchone()
                return self._lane_reservation_from_row(published), command, False

    def get_job(self, job_id: str) -> Job:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise JobNotFound(f"job not found: {job_id}")
        return self._job_from_row(row)

    def get_attempt(self, attempt_id: str) -> Attempt:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
        if row is None:
            raise AttemptNotFound(f"attempt not found: {attempt_id}")
        return self._attempt_from_row(row)

    def list_attempts(self, job_id: str) -> list[Attempt]:
        with self.connection() as connection:
            if connection.execute(
                "SELECT 1 FROM jobs WHERE id = ?", (job_id,)
            ).fetchone() is None:
                raise JobNotFound(f"job not found: {job_id}")
            rows = connection.execute(
                "SELECT * FROM attempts WHERE job_id = ? ORDER BY number",
                (job_id,),
            ).fetchall()
        return [self._attempt_from_row(row) for row in rows]

    @staticmethod
    def _process_exists(process_id: int) -> bool:
        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except (OSError, OverflowError, ValueError):
            return False
        return True

    @staticmethod
    def _validate_dispatch_token(dispatch_token: str) -> str:
        if (
            not isinstance(dispatch_token, str)
            or len(dispatch_token) != 32
            or any(character not in "0123456789abcdef" for character in dispatch_token)
        ):
            raise StateError("dispatch token is invalid")
        return dispatch_token

    def claim_dispatch(
        self,
        job_id: str,
        *,
        owner_pid: int | None = None,
    ) -> DispatchClaim | None:
        """Durably elect and return one opaque detached-worker launch claim."""
        if owner_pid is None:
            owner_pid = os.getpid()
        if isinstance(owner_pid, bool) or not isinstance(owner_pid, int) or owner_pid <= 0:
            raise StateError("dispatch owner pid must be a positive integer")
        with self._transaction() as connection:
            job = self._job_row(connection, job_id)
            attempt = self._current_attempt_row(connection, job)
            if job["status"] != "CREATED":
                return None
            unclaimed = attempt["dispatch_state"] == "UNCLAIMED"
            dead_bound_worker = False
            if (
                attempt["dispatch_state"] == "DISPATCHED"
                and attempt["worker_pid"] is not None
                and attempt["worker_identity"] is not None
            ):
                try:
                    dead_bound_worker = process_instance_is_definitively_dead(
                        attempt["worker_pid"],
                        attempt["worker_identity"],
                    )
                except ProcessError:
                    # A cold bridge may reclaim only from affirmative OS
                    # evidence. Inspection ambiguity leaves the launch fenced.
                    dead_bound_worker = False
            orphaned = (
                attempt["dispatch_state"] == "DISPATCHED"
                and (
                    (
                        attempt["worker_pid"] is None
                        and (
                            attempt["dispatch_owner_pid"] is None
                            or not self._process_exists(
                                attempt["dispatch_owner_pid"]
                            )
                        )
                    )
                    or dead_bound_worker
                )
            )
            if not unclaimed and not orphaned:
                return None
            dispatch_token = uuid.uuid4().hex
            new_revision = job["revision"] + 1
            now = _utc_now()
            connection.execute(
                """
                UPDATE attempts
                SET dispatch_state = 'DISPATCHED', dispatch_owner_pid = ?,
                    dispatch_token = ?, worker_pid = NULL, worker_identity = NULL,
                    revision = ?, updated_at = ?
                WHERE id = ?
                """,
                (owner_pid, dispatch_token, new_revision, now, attempt["id"]),
            )
            updated = connection.execute(
                """
                UPDATE jobs
                SET revision = ?, updated_at = ?
                WHERE id = ? AND revision = ?
                """,
                (new_revision, now, job_id, job["revision"]),
            )
            if updated.rowcount != 1:
                current = self._job_row(connection, job_id)["revision"]
                raise RevisionConflict(job_id, job["revision"], current)
            claim = DispatchClaim(
                job_id=job_id,
                attempt_id=attempt["id"],
                owner_pid=owner_pid,
                token=dispatch_token,
                revision=new_revision,
            )
            self._dispatch_claims[attempt["id"]] = claim
            return claim

    def release_dispatch(
        self,
        job_id: str,
        *,
        owner_pid: int,
        dispatch_token: str,
    ) -> bool:
        """Release a claim only when no detached worker PID was ever returned."""
        if isinstance(owner_pid, bool) or not isinstance(owner_pid, int) or owner_pid <= 0:
            raise StateError("dispatch owner pid must be a positive integer")
        dispatch_token = self._validate_dispatch_token(dispatch_token)
        with self._transaction() as connection:
            job = self._job_row(connection, job_id)
            attempt = self._current_attempt_row(connection, job)
            if (
                job["status"] != "CREATED"
                or attempt["dispatch_state"] != "DISPATCHED"
                or attempt["dispatch_owner_pid"] != owner_pid
                or attempt["dispatch_token"] != dispatch_token
                or attempt["worker_pid"] is not None
            ):
                return False
            new_revision = job["revision"] + 1
            now = _utc_now()
            updated_attempt = connection.execute(
                """
                UPDATE attempts
                SET dispatch_state = 'UNCLAIMED', dispatch_owner_pid = NULL,
                    dispatch_token = NULL, revision = ?, updated_at = ?
                WHERE id = ? AND dispatch_state = 'DISPATCHED'
                    AND dispatch_owner_pid = ? AND dispatch_token = ?
                    AND worker_pid IS NULL
                """,
                (
                    new_revision,
                    now,
                    attempt["id"],
                    owner_pid,
                    dispatch_token,
                ),
            )
            if updated_attempt.rowcount != 1:
                return False
            updated_job = connection.execute(
                """
                UPDATE jobs
                SET revision = ?, updated_at = ?
                WHERE id = ? AND revision = ?
                """,
                (new_revision, now, job_id, job["revision"]),
            )
            if updated_job.rowcount != 1:
                current = self._job_row(connection, job_id)["revision"]
                raise RevisionConflict(job_id, job["revision"], current)
            return True

    def abort_bound_worker_launch(
        self,
        job_id: str,
        *,
        worker_pid: int,
        worker_identity: str,
        _dispatch_claim: DispatchClaim | None = None,
    ) -> bool:
        """Release one durably bound launch only after that exact child died."""

        if isinstance(worker_pid, bool) or not isinstance(worker_pid, int) or worker_pid <= 0:
            raise StateError("worker pid must be a positive integer")
        worker_identity = self._validate_worker_identity(worker_identity)
        if (
            not isinstance(_dispatch_claim, DispatchClaim)
            or _dispatch_claim.job_id != job_id
            or _dispatch_claim.owner_pid != os.getpid()
            or self._dispatch_claims.get(_dispatch_claim.attempt_id)
            is not _dispatch_claim
        ):
            raise StateError("opaque dispatch launch authority is required")
        with self._transaction() as connection:
            job = self._job_row(connection, job_id)
            attempt = self._current_attempt_row(connection, job)
            if (
                job["status"] != "CREATED"
                or attempt["id"] != _dispatch_claim.attempt_id
                or attempt["dispatch_state"] != "DISPATCHED"
                or attempt["dispatch_owner_pid"] != _dispatch_claim.owner_pid
                or attempt["dispatch_token"] != _dispatch_claim.token
                or attempt["worker_pid"] != worker_pid
                or attempt["worker_identity"] != worker_identity
            ):
                return False
            if process_instance_is_live(worker_pid, worker_identity):
                raise StateError("bound worker launch is still live")
            new_revision = job["revision"] + 1
            now = _utc_now()
            updated_attempt = connection.execute(
                """
                UPDATE attempts
                SET dispatch_state = 'UNCLAIMED', dispatch_owner_pid = NULL,
                    dispatch_token = NULL, worker_pid = NULL,
                    worker_identity = NULL, revision = ?, updated_at = ?
                WHERE id = ? AND status = 'CREATED'
                    AND dispatch_state = 'DISPATCHED'
                    AND dispatch_owner_pid = ? AND dispatch_token = ?
                    AND worker_pid = ? AND worker_identity = ?
                """,
                (
                    new_revision,
                    now,
                    attempt["id"],
                    _dispatch_claim.owner_pid,
                    _dispatch_claim.token,
                    worker_pid,
                    worker_identity,
                ),
            )
            updated_job = connection.execute(
                """
                UPDATE jobs
                SET revision = ?, updated_at = ?
                WHERE id = ? AND revision = ? AND status = 'CREATED'
                """,
                (new_revision, now, job_id, job["revision"]),
            )
            if updated_attempt.rowcount != 1 or updated_job.rowcount != 1:
                raise StateError("bound worker launch abort lost its fence")
        self._dispatch_claims.pop(_dispatch_claim.attempt_id, None)
        return True

    def reset_resume_preflight_for_retry(
        self,
        job_id: str,
        *,
        dispatch_token: str,
        worker_pid: int,
    ) -> bool:
        """Return a no-effect resume preparation to the same dispatchable child."""

        dispatch_token = self._validate_dispatch_token(dispatch_token)
        if isinstance(worker_pid, bool) or not isinstance(worker_pid, int) or worker_pid <= 0:
            raise StateError("worker pid must be a positive integer")
        with self._transaction() as connection:
            job = self._job_row(connection, job_id)
            attempt = self._current_attempt_row(connection, job)
            if (
                job["status"] != "PREFLIGHT"
                or attempt["status"] != "PREFLIGHT"
                or not self._owned_dispatch(
                    attempt,
                    dispatch_token=dispatch_token,
                    worker_pid=worker_pid,
                )
                or attempt["parent_attempt_id"] is None
                or attempt["resume_session_id"] is None
                or attempt["provider_pgid"] is not None
                or attempt["session_id"] is not None
                or attempt["observed_model"] is not None
                or attempt["model_cli_accepted"] is not None
                or attempt["models_observed_json"] != "[]"
                or attempt["verification_level"] != "unverified"
                or attempt["parent_tool_use_ids_json"] != "[]"
                or attempt["terminal_receipt_name"] is not None
                or attempt["terminal_receipt_hash"] is not None
                or attempt["command_inbox_state"] != "OPEN"
            ):
                return False
            if connection.execute(
                "SELECT 1 FROM commands WHERE attempt_id = ? LIMIT 1",
                (attempt["id"],),
            ).fetchone() is not None:
                return False
            new_revision = job["revision"] + 1
            now = _utc_now()
            updated_attempt = connection.execute(
                """
                UPDATE attempts
                SET status = 'CREATED', revision = ?,
                    dispatch_state = 'UNCLAIMED', dispatch_owner_pid = NULL,
                    dispatch_token = NULL, worker_pid = NULL,
                    worker_identity = NULL, updated_at = ?
                WHERE id = ? AND status = 'PREFLIGHT'
                    AND dispatch_token = ? AND worker_pid = ?
                """,
                (
                    new_revision,
                    now,
                    attempt["id"],
                    dispatch_token,
                    worker_pid,
                ),
            )
            if updated_attempt.rowcount != 1:
                return False
            updated_job = connection.execute(
                """
                UPDATE jobs
                SET status = 'CREATED', revision = ?, updated_at = ?
                WHERE id = ? AND revision = ? AND status = 'PREFLIGHT'
                """,
                (new_revision, now, job_id, job["revision"]),
            )
            if updated_job.rowcount != 1:
                current = self._job_row(connection, job_id)["revision"]
                raise RevisionConflict(job_id, job["revision"], current)
            return True

    def claim_worker_execution(
        self,
        job_id: str,
        dispatch_token: str,
        *,
        worker_pid: int,
    ) -> bool:
        """Fence provider execution to one worker for the current dispatch token."""
        dispatch_token = self._validate_dispatch_token(dispatch_token)
        if isinstance(worker_pid, bool) or not isinstance(worker_pid, int) or worker_pid <= 0:
            raise StateError("worker pid must be a positive integer")
        with self._transaction() as connection:
            job = self._job_row(connection, job_id)
            attempt = self._current_attempt_row(connection, job)
            if (
                attempt["dispatch_state"] != "DISPATCHED"
                or attempt["dispatch_token"] != dispatch_token
            ):
                return False
            if attempt["worker_pid"] is not None:
                return attempt["worker_pid"] == worker_pid
            if job["status"] != "CREATED":
                return False
            updated = connection.execute(
                """
                UPDATE attempts
                SET worker_pid = ?, updated_at = ?
                WHERE id = ? AND dispatch_state = 'DISPATCHED'
                    AND dispatch_token = ? AND worker_pid IS NULL
                """,
                (worker_pid, _utc_now(), attempt["id"], dispatch_token),
            )
            return updated.rowcount == 1

    def record_worker_pid(
        self,
        job_id: str,
        worker_pid: int,
        *,
        _dispatch_claim: DispatchClaim | None = None,
        dispatch_token: str | None = None,
    ) -> None:
        """Bind the spawned worker birth identity using the launcher's capability."""

        if isinstance(worker_pid, bool) or not isinstance(worker_pid, int) or worker_pid <= 0:
            raise StateError("worker pid must be a positive integer")
        if (
            not isinstance(_dispatch_claim, DispatchClaim)
            or _dispatch_claim.job_id != job_id
            or _dispatch_claim.owner_pid != os.getpid()
            or self._dispatch_claims.get(_dispatch_claim.attempt_id)
            is not _dispatch_claim
        ):
            raise StateError("opaque dispatch launch authority is required")
        try:
            worker_identity = process_instance_identity(worker_pid)
        except ProcessError as error:
            raise StateError("spawned worker identity is unavailable") from error
        with self._transaction() as connection:
            job = self._job_row(connection, job_id)
            attempt = self._current_attempt_row(connection, job)
            if (
                job["status"] != "CREATED"
                or attempt["id"] != _dispatch_claim.attempt_id
                or attempt["dispatch_state"] != "DISPATCHED"
                or attempt["dispatch_owner_pid"] != _dispatch_claim.owner_pid
                or attempt["dispatch_token"] != _dispatch_claim.token
                or attempt["worker_pid"] is not None
                or attempt["worker_identity"] is not None
            ):
                raise StateError("dispatch launch claim is no longer current")
            updated = connection.execute(
                """
                UPDATE attempts
                SET worker_pid = ?, worker_identity = ?, updated_at = ?
                WHERE id = ? AND status = 'CREATED'
                    AND dispatch_state = 'DISPATCHED'
                    AND dispatch_owner_pid = ? AND dispatch_token = ?
                    AND worker_pid IS NULL AND worker_identity IS NULL
                """,
                (
                    worker_pid,
                    worker_identity,
                    _utc_now(),
                    attempt["id"],
                    _dispatch_claim.owner_pid,
                    _dispatch_claim.token,
                ),
            )
            if updated.rowcount != 1:
                raise StateError("worker launch binding lost its durable fence")

    @staticmethod
    def _validate_worker_identity(worker_identity: str) -> str:
        if (
            not isinstance(worker_identity, str)
            or len(worker_identity) != 64
            or any(character not in "0123456789abcdef" for character in worker_identity)
        ):
            raise StateError("worker process identity is invalid")
        return worker_identity

    def claim_worker_authority(
        self,
        job_id: str,
        dispatch_token: str,
    ) -> _WorkerAuthority | None:
        """Mint one process-local capability only inside the recorded worker."""

        dispatch_token = self._validate_dispatch_token(dispatch_token)
        worker_pid = os.getpid()
        try:
            worker_identity = process_instance_identity(worker_pid)
        except ProcessError as error:
            raise StateError("worker process identity is unavailable") from error
        self._validate_worker_identity(worker_identity)
        with _WORKER_AUTHORITY_LOCK:
            with self._transaction() as connection:
                job = self._job_row(connection, job_id)
                attempt = self._current_attempt_row(connection, job)
                key = (str(self.path), job_id, attempt["id"])
                existing = _WORKER_AUTHORITY_CLAIMS.get(key)
                if existing is not None:
                    return self._worker_authorities.get(attempt["id"])
                if (
                    job["status"] != "CREATED"
                    or attempt["dispatch_state"] != "DISPATCHED"
                    or attempt["dispatch_token"] != dispatch_token
                    or attempt["worker_pid"] != worker_pid
                    or attempt["worker_identity"] != worker_identity
                ):
                    return None
                authority = _WorkerAuthority(
                    store_marker=self._worker_authority_marker,
                    issuer_pid=worker_pid,
                    job_id=job_id,
                    attempt_id=attempt["id"],
                    dispatch_token=dispatch_token,
                    worker_identity=worker_identity,
                )
            _WORKER_AUTHORITY_CLAIMS[key] = authority
            self._worker_authorities[attempt["id"]] = authority
            return authority

    def _require_worker_authority(
        self,
        authority: _WorkerAuthority | None,
        *,
        job_id: str,
        attempt: sqlite3.Row,
    ) -> _WorkerAuthority:
        """Reject persisted scalar replay, fork inheritance, and PID reuse."""

        if (
            not isinstance(authority, _WorkerAuthority)
            or authority.store_marker is not self._worker_authority_marker
            or authority.issuer_pid != os.getpid()
            or authority.job_id != job_id
            or authority.attempt_id != attempt["id"]
            or authority.dispatch_token != attempt["dispatch_token"]
            or authority.issuer_pid != attempt["worker_pid"]
            or authority.worker_identity != attempt["worker_identity"]
            or self._worker_authorities.get(attempt["id"]) is not authority
        ):
            raise StateError("live worker cancellation authority is required")
        key = (str(self.path), job_id, attempt["id"])
        if _WORKER_AUTHORITY_CLAIMS.get(key) is not authority:
            raise StateError("live worker cancellation authority is not current")
        try:
            current_identity = process_instance_identity(os.getpid())
        except ProcessError as error:
            raise StateError("live worker cancellation authority is unavailable") from error
        if current_identity != authority.worker_identity:
            raise StateError("live worker cancellation authority identity changed")
        return authority

    @staticmethod
    def _identity_text(value: str | None, label: str, maximum_bytes: int = 256) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value or "\x00" in value:
            raise StateError(f"{label} must be non-empty safe text")
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise StateError(f"{label} must be valid UTF-8") from error
        if size > maximum_bytes:
            raise StateError(f"{label} exceeds its byte bound")
        return value

    def record_provider_identity(
        self,
        job_id: str,
        *,
        provider_pgid: int | None = None,
        provider_identity: str | None = None,
        session_id: str | None = None,
        observed_model: str | None = None,
        model_cli_accepted: str | None = None,
        models_observed: tuple[str, ...] | list[str] | None = None,
        verification_level: str | None = None,
        parent_tool_use_id: str | None = None,
    ) -> None:
        """Persist bounded provider identity independently of the event window."""
        if provider_pgid is not None and (
            isinstance(provider_pgid, bool)
            or not isinstance(provider_pgid, int)
            or provider_pgid <= 0
        ):
            raise StateError("provider process-group id must be a positive integer")
        if provider_pgid is None and provider_identity is not None:
            raise StateError("provider birth identity requires its process-group id")
        if provider_pgid is not None and provider_identity is None:
            try:
                provider_identity = process_instance_identity(provider_pgid)
            except ProcessError as error:
                raise StateError("provider birth identity is unavailable") from error
        provider_identity = self._identity_text(
            provider_identity, "provider birth identity", 64
        )
        if provider_identity is not None:
            if (
                len(provider_identity) != 64
                or any(character not in "0123456789abcdef" for character in provider_identity)
            ):
                raise StateError("provider birth identity is invalid")
            try:
                observed_process_identity = process_instance_identity(provider_pgid)
            except ProcessError as error:
                raise StateError("provider birth identity is unavailable") from error
            if observed_process_identity != provider_identity:
                raise StateError("provider birth identity does not match the live process")
        session_id = self._identity_text(session_id, "session id")
        observed_model = self._identity_text(observed_model, "observed model")
        model_cli_accepted = self._identity_text(
            model_cli_accepted, "CLI-accepted model"
        )
        normalized_models: list[str] | None = None
        models_observed_json: str | None = None
        if models_observed is not None:
            if not isinstance(models_observed, (tuple, list)):
                raise StateError("observed models must be an ordered sequence")
            normalized_models = []
            for model in models_observed:
                normalized = self._identity_text(model, "observed model")
                if normalized is None or normalized in normalized_models:
                    raise StateError(
                        "observed models must be unique non-empty identifiers"
                    )
                normalized_models.append(normalized)
            if len(normalized_models) > 16:
                raise StateError("observed model count exceeds its bound")
            models_observed_json = canonical_json(normalized_models)
            if len(models_observed_json.encode("utf-8")) > MAX_IDENTITY_JSON_BYTES:
                raise StateError("observed models exceed their byte bound")
        if verification_level is not None and verification_level not in {
            "unverified",
            "alias_observed",
            "full_name_exact",
        }:
            raise StateError("model verification level is invalid")
        parent_tool_use_id = self._identity_text(
            parent_tool_use_id,
            "parent tool-use id",
            4096,
        )
        with self._transaction() as connection:
            job = self._job_row(connection, job_id)
            attempt = self._current_attempt_row(connection, job)
            if attempt["status"] in TERMINAL_STATUSES:
                raise InvalidTransition("cannot mutate provider identity after terminal state")
            if provider_pgid is not None and attempt["provider_pgid"] not in (
                None,
                provider_pgid,
            ):
                raise StateError("provider process-group id changed")
            if provider_identity is not None and attempt["provider_identity"] not in (
                None,
                provider_identity,
            ):
                raise StateError("provider birth identity changed")
            if session_id is not None and attempt["session_id"] not in (None, session_id):
                raise StateError("provider session id changed")
            if model_cli_accepted is not None and attempt["model_cli_accepted"] not in (
                None,
                model_cli_accepted,
            ):
                raise StateError("CLI-accepted model changed")
            try:
                stored_models = json.loads(attempt["models_observed_json"])
            except (TypeError, json.JSONDecodeError) as error:
                raise StateError("stored observed models are malformed") from error
            if not isinstance(stored_models, list) or any(
                not isinstance(item, str) for item in stored_models
            ):
                raise StateError("stored observed models are malformed")
            if normalized_models is not None:
                if normalized_models[: len(stored_models)] != stored_models:
                    raise StateError("provider observed model order changed")
                if normalized_models:
                    observed_model = normalized_models[0]
            elif observed_model is not None and attempt["observed_model"] not in (
                None,
                observed_model,
            ):
                raise StateError("provider observed model changed")
            try:
                parents = json.loads(attempt["parent_tool_use_ids_json"])
            except (TypeError, json.JSONDecodeError) as error:
                raise StateError("stored parent tool-use identities are malformed") from error
            if not isinstance(parents, list) or any(not isinstance(item, str) for item in parents):
                raise StateError("stored parent tool-use identities are malformed")
            if parent_tool_use_id is not None and parent_tool_use_id not in parents:
                if len(parents) >= MAX_PARENT_TOOL_USE_IDS:
                    raise StateError("parent tool-use identity count exceeds its bound")
                parents.append(parent_tool_use_id)
            parents_json = canonical_json(parents)
            if len(parents_json.encode("utf-8")) > MAX_IDENTITY_JSON_BYTES:
                raise StateError("parent tool-use identities exceed their byte bound")
            connection.execute(
                """
                UPDATE attempts
                SET provider_pgid = COALESCE(?, provider_pgid),
                    provider_identity = COALESCE(?, provider_identity),
                    session_id = COALESCE(?, session_id),
                    observed_model = COALESCE(?, observed_model),
                    model_cli_accepted = COALESCE(?, model_cli_accepted),
                    models_observed_json = COALESCE(?, models_observed_json),
                    verification_level = COALESCE(?, verification_level),
                    parent_tool_use_ids_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    provider_pgid,
                    provider_identity,
                    session_id,
                    observed_model,
                    model_cli_accepted,
                    models_observed_json,
                    verification_level,
                    parents_json,
                    _utc_now(),
                    attempt["id"],
                ),
            )

    def record_stderr_signature(
        self,
        job_id: str,
        *,
        provider_pgid: int,
        provider_identity: str,
        signature_sha256: str,
        repeat_count: int,
        byte_count: int,
    ) -> Event:
        """Persist bounded repeated-stderr evidence without storing provider text."""

        if (
            isinstance(provider_pgid, bool)
            or not isinstance(provider_pgid, int)
            or provider_pgid <= 0
            or not isinstance(provider_identity, str)
            or len(provider_identity) != 64
            or any(character not in "0123456789abcdef" for character in provider_identity)
        ):
            raise StateError("provider stderr authority is invalid")
        if (
            not isinstance(signature_sha256, str)
            or len(signature_sha256) != 64
            or any(character not in "0123456789abcdef" for character in signature_sha256)
        ):
            raise StateError("provider stderr signature is invalid")
        if (
            isinstance(repeat_count, bool)
            or not isinstance(repeat_count, int)
            or repeat_count < 2
            or repeat_count > 64
            or isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or byte_count < 1
            or byte_count > 4096
        ):
            raise StateError("provider stderr repetition evidence is out of bounds")
        try:
            current_identity = process_instance_identity(provider_pgid)
        except ProcessError as error:
            raise StateError("provider stderr authority is unavailable") from error
        if current_identity != provider_identity:
            raise StateError("provider stderr authority identity changed")

        payload = {
            "signature_sha256": signature_sha256,
            "repeat_count": repeat_count,
            "byte_count": byte_count,
        }
        for _attempt_index in range(3):
            with self.connection() as connection:
                job = self._job_row(connection, job_id)
                attempt = self._current_attempt_row(connection, job)
                if job["status"] != "RUNNING" or attempt["status"] != "RUNNING":
                    raise InvalidTransition(
                        "provider stderr signatures require a running attempt"
                    )
                if (
                    attempt["provider_pgid"] != provider_pgid
                    or attempt["provider_identity"] != provider_identity
                ):
                    raise StateError("provider stderr authority is not current")
                revision = job["revision"]
            try:
                return self.append_event(
                    job_id,
                    "STDERR_SIGNATURE",
                    payload,
                    expected_revision=revision,
                )
            except RevisionConflict:
                continue
        raise StateError("provider stderr signature lost its bounded revision race")

    def record_objective_progress(
        self,
        job_id: str,
        *,
        provider_pgid: int,
        provider_identity: str,
        source: str,
        evidence_sha256: str,
        changed_file_count: int,
    ) -> Event | None:
        """Append exact attempt-scoped objective progress from the live provider.

        Provider stream chatter is deliberately ineligible.  The worker may
        publish only a content-derived digest for its admitted writable files,
        and a duplicate digest is an idempotent no-op.
        """

        if (
            isinstance(provider_pgid, bool)
            or not isinstance(provider_pgid, int)
            or provider_pgid <= 0
            or not isinstance(provider_identity, str)
            or len(provider_identity) != 64
            or any(character not in "0123456789abcdef" for character in provider_identity)
        ):
            raise StateError("objective progress authority is invalid")
        if source != "declared_artifact":
            raise StateError("objective progress source is invalid")
        if (
            not isinstance(evidence_sha256, str)
            or len(evidence_sha256) != 64
            or any(character not in "0123456789abcdef" for character in evidence_sha256)
        ):
            raise StateError("objective progress digest is invalid")
        if (
            isinstance(changed_file_count, bool)
            or not isinstance(changed_file_count, int)
            or not 1 <= changed_file_count <= 256
        ):
            raise StateError("objective progress file count is invalid")
        try:
            current_identity = process_instance_identity(provider_pgid)
        except ProcessError as error:
            raise StateError("objective progress authority is unavailable") from error
        if current_identity != provider_identity:
            raise StateError("objective progress authority identity changed")

        payload = {
            "source": source,
            "evidence_sha256": evidence_sha256,
            "changed_file_count": changed_file_count,
        }
        for _attempt_index in range(3):
            with self.connection() as connection:
                job = self._job_row(connection, job_id)
                attempt = self._current_attempt_row(connection, job)
                if job["status"] != "RUNNING" or attempt["status"] != "RUNNING":
                    raise InvalidTransition(
                        "objective progress requires a running attempt"
                    )
                if (
                    attempt["provider_pgid"] != provider_pgid
                    or attempt["provider_identity"] != provider_identity
                ):
                    raise StateError("objective progress authority is not current")
                latest = connection.execute(
                    """
                    SELECT payload_json
                    FROM events
                    WHERE job_id = ? AND attempt_id = ?
                        AND kind = 'OBJECTIVE_PROGRESS'
                    ORDER BY sequence DESC LIMIT 1
                    """,
                    (job_id, attempt["id"]),
                ).fetchone()
                if latest is not None:
                    try:
                        latest_payload = json.loads(latest["payload_json"])
                    except (TypeError, json.JSONDecodeError) as error:
                        raise StateError(
                            "stored objective progress evidence is malformed"
                        ) from error
                    if (
                        isinstance(latest_payload, dict)
                        and latest_payload.get("evidence_sha256") == evidence_sha256
                    ):
                        return None
                revision = job["revision"]
            try:
                return self.append_event(
                    job_id,
                    "OBJECTIVE_PROGRESS",
                    payload,
                    expected_revision=revision,
                )
            except RevisionConflict:
                continue
        raise StateError("objective progress lost its bounded revision race")

    def get_liveness_snapshot(
        self,
        job_id: str,
        *,
        attempt_id: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Read one attempt-scoped liveness view from a single SQLite snapshot."""

        if not isinstance(attempt_id, str) or not attempt_id:
            raise StateError("liveness attempt id is invalid")
        if expected_revision is not None and (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise StateError("liveness revision is invalid")
        with self.connection() as connection:
            connection.execute("BEGIN")
            job = self._job_row(connection, job_id)
            if expected_revision is not None and job["revision"] != expected_revision:
                raise RevisionConflict(job_id, expected_revision, job["revision"])
            attempt = self._current_attempt_row(connection, job)
            if attempt["id"] != attempt_id:
                raise StateError("liveness attempt is not current")
            objective = connection.execute(
                """
                SELECT sequence, kind, payload_json, event_hash, created_at
                FROM events
                WHERE job_id = ? AND attempt_id = ?
                    AND kind IN ('OBJECTIVE_PROGRESS', 'ARTIFACT_PROGRESS')
                ORDER BY sequence DESC LIMIT 1
                """,
                (job_id, attempt_id),
            ).fetchone()
            stderr = connection.execute(
                """
                SELECT sequence, payload_json, event_hash, created_at
                FROM events
                WHERE job_id = ? AND attempt_id = ? AND kind = 'STDERR_SIGNATURE'
                ORDER BY sequence DESC LIMIT 1
                """,
                (job_id, attempt_id),
            ).fetchone()
        stderr_payload = None
        if stderr is not None:
            try:
                stderr_payload = json.loads(stderr["payload_json"])
            except (TypeError, json.JSONDecodeError) as error:
                raise StateError("stored stderr signature evidence is malformed") from error
            if (
                not isinstance(stderr_payload, dict)
                or set(stderr_payload)
                != {"signature_sha256", "repeat_count", "byte_count"}
                or not isinstance(stderr_payload["signature_sha256"], str)
                or len(stderr_payload["signature_sha256"]) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in stderr_payload["signature_sha256"]
                )
                or isinstance(stderr_payload["repeat_count"], bool)
                or not isinstance(stderr_payload["repeat_count"], int)
                or not 2 <= stderr_payload["repeat_count"] <= 64
                or isinstance(stderr_payload["byte_count"], bool)
                or not isinstance(stderr_payload["byte_count"], int)
                or not 1 <= stderr_payload["byte_count"] <= 4096
            ):
                raise StateError("stored stderr signature evidence is malformed")
        return {
            "job_id": job["id"],
            "attempt_id": attempt["id"],
            "revision": job["revision"],
            "status": job["status"],
            "updated_at": job["updated_at"],
            "attempt_created_at": attempt["created_at"],
            "worker_pid": attempt["worker_pid"],
            "worker_identity": attempt["worker_identity"],
            "provider_pgid": attempt["provider_pgid"],
            "provider_identity": attempt["provider_identity"],
            "terminal_receipt_name": attempt["terminal_receipt_name"],
            "terminal_receipt_hash": attempt["terminal_receipt_hash"],
            "last_objective_progress_at": (
                None if objective is None else objective["created_at"]
            ),
            "last_objective_progress_sequence": (
                None if objective is None else objective["sequence"]
            ),
            "last_objective_progress_hash": (
                None if objective is None else objective["event_hash"]
            ),
            "last_stderr_signature_at": (
                None if stderr is None else stderr["created_at"]
            ),
            "last_stderr_signature_sequence": (
                None if stderr is None else stderr["sequence"]
            ),
            "last_stderr_signature_sha256": (
                None
                if stderr_payload is None
                else stderr_payload["signature_sha256"]
            ),
            "last_stderr_repeat_count": (
                None if stderr_payload is None else stderr_payload["repeat_count"]
            ),
        }

    @staticmethod
    def _require_terminal_inbox_settled(
        connection: sqlite3.Connection,
        job_id: str,
        attempt: sqlite3.Row,
    ) -> None:
        if attempt["command_inbox_state"] not in {"OPEN", "CLOSED"}:
            raise StateError("stored command inbox state is invalid")
        pending = connection.execute(
            """
            SELECT COUNT(*)
            FROM commands
            WHERE job_id = ? AND attempt_id = ?
                AND status IN ('QUEUED', 'DELIVERING')
            """,
            (job_id, attempt["id"]),
        ).fetchone()[0]
        if pending:
            raise InvalidTransition(
                "terminal transition requires every accepted command to be settled"
            )

    def finalize_with_receipt(
        self,
        job_id: str,
        *,
        expected_revision: int,
        new_status: str,
        receipt_name: str,
        receipt_hash: str,
        _lease: _AttemptEvidenceLease | None = None,
        _worker_authority: _WorkerAuthority | None = None,
    ) -> int:
        """Atomically publish a terminal state and its already-durable receipt."""
        if new_status not in TERMINAL_STATUSES:
            raise InvalidTransition("receipt finalization requires a terminal status")
        if receipt_name != "receipt.json":
            raise StateError("terminal receipt name is invalid")
        if (
            not isinstance(receipt_hash, str)
            or len(receipt_hash) != 64
            or any(character not in "0123456789abcdef" for character in receipt_hash)
        ):
            raise StateError("terminal receipt hash is invalid")
        with self.connection() as connection:
            admitted_job = self._job_row(connection, job_id)
            if admitted_job["revision"] != expected_revision:
                raise RevisionConflict(
                    job_id,
                    expected_revision,
                    admitted_job["revision"],
                )
            admitted_attempt = self._current_attempt_row(connection, admitted_job)
            admitted_attempt_id = admitted_attempt["id"]
        if _lease is not None:
            self._validate_evidence_lease(
                _lease,
                job_id=job_id,
                attempt_id=admitted_attempt_id,
                require_exclusive=True,
            )
            lease_context = nullcontext(_lease)
        else:
            lease_context = self.attempt_evidence_lease(
                job_id,
                admitted_attempt_id,
                exclusive=True,
            )
        with lease_context as active_lease:
            with self.open_attempt_artifact(
                job_id,
                admitted_attempt_id,
                receipt_name,
                _lease=_lease,
            ) as (receipt_descriptor, _receipt_path):
                receipt_data = bytearray()
                while True:
                    remaining = MAX_TERMINAL_RECEIPT_BYTES + 1 - len(receipt_data)
                    if remaining <= 0:
                        raise StateError("terminal receipt exceeds its byte bound")
                    chunk = os.read(receipt_descriptor, min(65_536, remaining))
                    if not chunk:
                        break
                    receipt_data.extend(chunk)
                if len(receipt_data) > MAX_TERMINAL_RECEIPT_BYTES:
                    raise StateError("terminal receipt exceeds its byte bound")
            if hashlib.sha256(receipt_data).hexdigest() != receipt_hash:
                raise StateError("terminal receipt hash does not match its artifact")

            def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                parsed: dict[str, Any] = {}
                for key, value in pairs:
                    if key in parsed:
                        raise StateError("terminal receipt contains a duplicate key")
                    parsed[key] = value
                return parsed

            def reject_constant(_value: str) -> None:
                raise StateError("terminal receipt contains a non-finite number")

            try:
                receipt = json.loads(
                    bytes(receipt_data).decode("utf-8", errors="strict"),
                    object_pairs_hook=reject_duplicates,
                    parse_constant=reject_constant,
                )
            except StateError:
                raise
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
                raise StateError("terminal receipt is malformed") from error
            if not isinstance(receipt, dict):
                raise StateError("terminal receipt is not an object")

            with self._terminal_transaction(active_lease) as connection:
                job = self._job_row(connection, job_id)
                if job["revision"] != expected_revision:
                    raise RevisionConflict(job_id, expected_revision, job["revision"])
                attempt = self._current_attempt_row(connection, job)
                if attempt["id"] != admitted_attempt_id:
                    raise StateError("current attempt changed during terminal finalization")
                if new_status not in TRANSITIONS[attempt["status"]]:
                    raise InvalidTransition(
                        f"attempt transition not allowed: {attempt['status']} -> {new_status}"
                    )
                self._require_terminal_inbox_settled(connection, job_id, attempt)
                if new_status == "CANCELLED":
                    self._require_worker_authority(
                        _worker_authority,
                        job_id=job_id,
                        attempt=attempt,
                    )
                    expected_cancellation = self._durable_cancellation_receipt(
                        connection,
                        job,
                        attempt,
                    )
                    if receipt.get("cancellation") != expected_cancellation:
                        raise StateError(
                            "terminal cancellation receipt contradicts durable evidence"
                        )
                latest_event = connection.execute(
                    """
                    SELECT event_hash
                    FROM events
                    WHERE job_id = ? AND attempt_id = ?
                    ORDER BY sequence DESC
                    LIMIT 1
                    """,
                    (job_id, attempt["id"]),
                ).fetchone()
                try:
                    stored_parents = json.loads(attempt["parent_tool_use_ids_json"])
                except (TypeError, json.JSONDecodeError) as error:
                    raise StateError(
                        "stored parent tool-use identities are malformed"
                    ) from error
                expected_snapshot = {
                    "job_id": job_id,
                    "attempt_id": attempt["id"],
                    "event_hash": (
                        None if latest_event is None else latest_event["event_hash"]
                    ),
                    "session_id": attempt["session_id"],
                    "observed_model": attempt["observed_model"],
                    "parent_tool_use_ids": stored_parents,
                    "terminal_status": new_status,
                }
                if any(receipt.get(name) != value for name, value in expected_snapshot.items()):
                    raise StateError(
                        "terminal receipt does not match the durable attempt snapshot"
                    )
                self._validate_evidence_lease(
                    active_lease,
                    job_id=job_id,
                    attempt_id=attempt["id"],
                    require_exclusive=True,
                    connection=connection,
                )
                new_revision = expected_revision + 1
                now = _utc_now()
                connection.execute(
                    """
                    UPDATE attempts
                    SET status = ?, revision = ?, terminal_receipt_name = ?,
                        terminal_receipt_hash = ?, command_inbox_state = 'CLOSED',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        new_status,
                        new_revision,
                        receipt_name,
                        receipt_hash,
                        now,
                        attempt["id"],
                    ),
                )
                updated = connection.execute(
                    """
                    UPDATE jobs
                    SET status = ?, revision = ?, updated_at = ?
                    WHERE id = ? AND revision = ?
                    """,
                    (new_status, new_revision, now, job_id, expected_revision),
                )
                if updated.rowcount != 1:
                    current = self._job_row(connection, job_id)["revision"]
                    raise RevisionConflict(job_id, expected_revision, current)
                return new_revision

    def transition(
        self,
        job_id: str,
        *,
        expected_revision: int,
        new_status: str,
    ) -> int:
        """CAS-transition the current attempt and return its new job revision."""
        if new_status not in TRANSITIONS:
            raise InvalidTransition(f"unknown attempt status: {new_status}")
        if new_status == "CANCELLED":
            raise InvalidTransition(
                "CANCELLED requires durable receipt finalization"
            )
        admitted_attempt_id: str | None = None
        if new_status in TERMINAL_STATUSES:
            with self.connection() as connection:
                admitted_job = self._job_row(connection, job_id)
                if admitted_job["revision"] != expected_revision:
                    raise RevisionConflict(
                        job_id,
                        expected_revision,
                        admitted_job["revision"],
                    )
                admitted_attempt_id = self._current_attempt_row(
                    connection,
                    admitted_job,
                )["id"]
            lease_context = self.attempt_evidence_lease(
                job_id,
                admitted_attempt_id,
                exclusive=True,
            )
        else:
            lease_context = nullcontext(None)
        with lease_context as active_lease:
            transaction_context = (
                self._terminal_transaction(active_lease)
                if new_status in TERMINAL_STATUSES
                else self._transaction()
            )
            with transaction_context as connection:
                job = self._job_row(connection, job_id)
                actual_revision = job["revision"]
                if actual_revision != expected_revision:
                    raise RevisionConflict(job_id, expected_revision, actual_revision)
                attempt = self._current_attempt_row(connection, job)
                if (
                    admitted_attempt_id is not None
                    and attempt["id"] != admitted_attempt_id
                ):
                    raise StateError("current attempt changed during terminal transition")
                old_status = attempt["status"]
                if new_status not in TRANSITIONS[old_status]:
                    raise InvalidTransition(
                        f"attempt transition not allowed: {old_status} -> {new_status}"
                    )
                if new_status in TERMINAL_STATUSES:
                    self._require_terminal_inbox_settled(connection, job_id, attempt)
                    self._validate_evidence_lease(
                        active_lease,
                        job_id=job_id,
                        attempt_id=attempt["id"],
                        require_exclusive=True,
                        connection=connection,
                    )

                new_revision = actual_revision + 1
                now = _utc_now()
                connection.execute(
                    """
                    UPDATE attempts
                    SET status = ?, revision = ?,
                        command_inbox_state = CASE
                            WHEN ? THEN 'CLOSED' ELSE command_inbox_state END,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        new_status,
                        new_revision,
                        int(new_status in TERMINAL_STATUSES),
                        now,
                        attempt["id"],
                    ),
                )
                updated = connection.execute(
                    """
                    UPDATE jobs
                    SET status = ?, revision = ?, updated_at = ?
                    WHERE id = ? AND revision = ?
                    """,
                    (new_status, new_revision, now, job_id, expected_revision),
                )
                if updated.rowcount != 1:
                    current = self._job_row(connection, job_id)["revision"]
                    raise RevisionConflict(job_id, expected_revision, current)
                return new_revision

    def create_new_attempt(
        self,
        job_id: str,
        *,
        expected_revision: int,
    ) -> Attempt:
        """Create a child attempt without mutating the terminal parent attempt."""
        attempt_id = uuid.uuid4().hex
        with self._provision_attempt_lock(
            job_id,
            attempt_id,
            allow_existing=False,
        ) as provision:
            identity = (
                provision.device,
                provision.inode,
                provision.owner,
            )
            with self._transaction() as connection:
                job = self._job_row(connection, job_id)
                actual_revision = job["revision"]
                if actual_revision != expected_revision:
                    raise RevisionConflict(job_id, expected_revision, actual_revision)
                parent = self._current_attempt_row(connection, job)
                if parent["status"] not in TERMINAL_STATUSES:
                    raise InvalidTransition(
                        f"{CREATE_NEW_ATTEMPT} requires a terminal attempt, "
                        f"found {parent['status']}"
                    )

                number = connection.execute(
                    "SELECT COALESCE(MAX(number), 0) + 1 FROM attempts WHERE job_id = ?",
                    (job_id,),
                ).fetchone()[0]
                new_revision = actual_revision + 1
                now = _utc_now()
                connection.execute(
                    """
                    INSERT INTO attempts(
                        id, job_id, number, parent_attempt_id, status,
                        revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'CREATED', ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        job_id,
                        number,
                        parent["id"],
                        new_revision,
                        now,
                        now,
                    ),
                )
                self._insert_attempt_lock_identity(
                    connection,
                    attempt_id,
                    identity,
                )
                updated = connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'CREATED', revision = ?, current_attempt_id = ?, updated_at = ?
                    WHERE id = ? AND revision = ?
                    """,
                    (new_revision, attempt_id, now, job_id, expected_revision),
                )
                if updated.rowcount != 1:
                    current = self._job_row(connection, job_id)["revision"]
                    raise RevisionConflict(job_id, expected_revision, current)
                created = connection.execute(
                    "SELECT * FROM attempts WHERE id = ?", (attempt_id,)
                ).fetchone()
                result = self._attempt_from_row(created)
            provision.published = True
            return result

    def create_attempt(
        self,
        job_id: str,
        *,
        expected_revision: int,
    ) -> Attempt:
        """Compatibility spelling for the explicit new-attempt operation."""
        return self.create_new_attempt(job_id, expected_revision=expected_revision)

    def create_resume_attempt(
        self,
        job_id: str,
        *,
        expected_revision: int,
        recorded_session_id: str,
        model_override: str | None = None,
    ) -> Attempt:
        """Create one child attempt pinned to the terminal parent's session."""
        recorded_session_id = self._identity_text(
            recorded_session_id,
            "recorded session id",
        )
        if recorded_session_id is None:
            raise StateError("recorded session id is required")
        model_override = self._identity_text(model_override, "model override")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise StateError("expected revision must be a non-negative integer")
        attempt_id = uuid.uuid4().hex
        with self._provision_attempt_lock(
            job_id,
            attempt_id,
            allow_existing=False,
        ) as provision:
            identity = (
                provision.device,
                provision.inode,
                provision.owner,
            )
            with self._transaction() as connection:
                job = self._job_row(connection, job_id)
                if job["revision"] != expected_revision:
                    raise RevisionConflict(job_id, expected_revision, job["revision"])
                parent = self._current_attempt_row(connection, job)
                if parent["status"] not in TERMINAL_STATUSES:
                    raise InvalidTransition(
                        f"{CREATE_NEW_ATTEMPT} requires a terminal attempt, "
                        f"found {parent['status']}"
                    )
                if parent["session_id"] != recorded_session_id:
                    raise StateError(
                        "recorded resume session does not match the terminal attempt"
                    )

                number = connection.execute(
                    "SELECT COALESCE(MAX(number), 0) + 1 FROM attempts WHERE job_id = ?",
                    (job_id,),
                ).fetchone()[0]
                new_revision = expected_revision + 1
                now = _utc_now()
                connection.execute(
                    """
                    INSERT INTO attempts(
                        id, job_id, number, parent_attempt_id, status, revision,
                        resume_session_id, model_override, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'CREATED', ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        job_id,
                        number,
                        parent["id"],
                        new_revision,
                        recorded_session_id,
                        model_override,
                        now,
                        now,
                    ),
                )
                self._insert_attempt_lock_identity(
                    connection,
                    attempt_id,
                    identity,
                )
                updated = connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'CREATED', revision = ?, current_attempt_id = ?, updated_at = ?
                    WHERE id = ? AND revision = ?
                    """,
                    (new_revision, attempt_id, now, job_id, expected_revision),
                )
                if updated.rowcount != 1:
                    current = self._job_row(connection, job_id)["revision"]
                    raise RevisionConflict(job_id, expected_revision, current)
                row = connection.execute(
                    "SELECT * FROM attempts WHERE id = ?", (attempt_id,)
                ).fetchone()
                result = self._attempt_from_row(row)
            provision.published = True
            return result

    def create_admitted_resume_attempt(
        self,
        job_id: str,
        *,
        expected_revision: int,
        recorded_session_id: str,
        model_override: str | None,
        validate_reservation: Callable[[str, list[dict[str, Any]]], dict[str, Any]],
        attempt_context: dict[str, Any] | None = None,
    ) -> Attempt:
        """Atomically reacquire a released scheduler reservation for resume."""

        recorded_session_id = self._identity_text(
            recorded_session_id,
            "recorded session id",
        )
        if recorded_session_id is None:
            raise StateError("recorded session id is required")
        model_override = self._identity_text(model_override, "model override")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
            or not callable(validate_reservation)
        ):
            raise StateError("admitted resume inputs are invalid")
        attempt_id = uuid.uuid4().hex
        with self._provision_attempt_lock(
            job_id,
            attempt_id,
            allow_existing=False,
        ) as provision:
            identity = (provision.device, provision.inode, provision.owner)
            with self._admission_lock():
                with self._transaction() as connection:
                    job = self._job_row(connection, job_id)
                    if job["revision"] != expected_revision:
                        raise RevisionConflict(job_id, expected_revision, job["revision"])
                    parent = self._current_attempt_row(connection, job)
                    if parent["status"] not in TERMINAL_STATUSES:
                        raise InvalidTransition(
                            f"{CREATE_NEW_ATTEMPT} requires a terminal attempt, "
                            f"found {parent['status']}"
                        )
                    if parent["session_id"] != recorded_session_id:
                        raise StateError(
                            "recorded resume session does not match the terminal attempt"
                        )
                    active_reservations = self._active_lane_reservations(
                        connection,
                        exclude_job_id=job_id,
                    )
                    resume_admission = validate_reservation(
                        attempt_id,
                        active_reservations,
                    )
                    admission_json, admission_hash = self._admission_envelope(
                        resume_admission
                    )

                    number = connection.execute(
                        "SELECT COALESCE(MAX(number), 0) + 1 FROM attempts WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()[0]
                    new_revision = expected_revision + 1
                    now = _utc_now()
                    connection.execute(
                        """
                        INSERT INTO attempts(
                            id, job_id, number, parent_attempt_id, status, revision,
                            resume_session_id, model_override, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'CREATED', ?, ?, ?, ?, ?)
                        """,
                        (
                            attempt_id,
                            job_id,
                            number,
                            parent["id"],
                            new_revision,
                            recorded_session_id,
                            model_override,
                            now,
                            now,
                        ),
                    )
                    self._insert_attempt_lock_identity(
                        connection,
                        attempt_id,
                        identity,
                    )
                    if attempt_context is None:
                        try:
                            job_payload = json.loads(job["payload_json"])
                            context_value = self._bridge_context_from_payload(job_payload)
                        except (TypeError, json.JSONDecodeError, StateError) as error:
                            raise StateError(
                                "resume attempt context is unavailable"
                            ) from error
                    else:
                        context_value = attempt_context
                    self._insert_attempt_context(
                        connection,
                        attempt_id,
                        context_value,
                    )
                    connection.execute(
                        """
                        INSERT INTO attempt_admissions(
                            attempt_id, evidence_json, evidence_hash
                        ) VALUES (?, ?, ?)
                        """,
                        (attempt_id, admission_json, admission_hash),
                    )
                    lane_admission_json, lane_admission_hash = (
                        self._lane_admission_envelope(resume_admission)
                    )
                    context_baseline = context_value.get("baseline")
                    if not isinstance(context_baseline, dict) or not isinstance(
                        context_baseline.get("head"), str
                    ):
                        raise StateError("resume context baseline is malformed")
                    rebound = connection.execute(
                        """
                        UPDATE lane_reservations
                        SET attempt_id = ?, base_sha = ?, revision = revision + 1,
                            admission_json = ?, admission_hash = ?, updated_at = ?
                        WHERE kind = 'bridge' AND job_id = ? AND attempt_id = ?
                          AND released_at IS NULL
                        """,
                        (
                            attempt_id,
                            context_baseline["head"],
                            lane_admission_json,
                            lane_admission_hash,
                            now,
                            job_id,
                            parent["id"],
                        ),
                    )
                    if rebound.rowcount != 1:
                        raise StateError("resume lost its authoritative lane reservation")
                    updated = connection.execute(
                        """
                        UPDATE jobs
                        SET status = 'CREATED', revision = ?, current_attempt_id = ?, updated_at = ?
                        WHERE id = ? AND revision = ?
                        """,
                        (new_revision, attempt_id, now, job_id, expected_revision),
                    )
                    if updated.rowcount != 1:
                        current = self._job_row(connection, job_id)["revision"]
                        raise RevisionConflict(job_id, expected_revision, current)
                    row = connection.execute(
                        "SELECT * FROM attempts WHERE id = ?",
                        (attempt_id,),
                    ).fetchone()
                    result = self._attempt_from_row(row)
            provision.published = True
            return result

    @staticmethod
    def _command_inputs(
        *,
        expected_revision: int,
        kind: str,
        payload: Any,
        idempotency_key: str,
    ) -> tuple[str, str, str]:
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise StateError("expected revision must be a non-negative integer")
        if kind == "STEER":
            if not isinstance(payload, dict) or set(payload) not in (
                {"prompt"},
                {"prompt", "admission_expansion"},
            ):
                raise StateError("steering command payload is invalid")
            command_value = payload["prompt"]
            if (
                not isinstance(command_value, str)
                or not command_value
                or "\x00" in command_value
            ):
                raise StateError("steering prompt must be non-empty safe text")
            try:
                value_size = len(command_value.encode("utf-8"))
            except UnicodeEncodeError as error:
                raise StateError("steering prompt must be valid UTF-8") from error
            if value_size > 524_288:
                raise StateError("steering prompt exceeds its byte bound")
            normalized_payload = {"prompt": command_value}
            if "admission_expansion" in payload:
                expansion = payload["admission_expansion"]
                required = {
                    "lease_id",
                    "prior_lease_revision",
                    "lease_revision",
                    "prior_files",
                    "files",
                    "admission_sha256",
                    "operation_sha256",
                }
                if (
                    not isinstance(expansion, dict)
                    or set(expansion) != required
                    or not isinstance(expansion["lease_id"], str)
                    or not expansion["lease_id"]
                    or len(expansion["lease_id"]) > 256
                    or any(
                        isinstance(expansion[name], bool)
                        or not isinstance(expansion[name], int)
                        or expansion[name] < 0
                        for name in ("prior_lease_revision", "lease_revision")
                    )
                    or expansion["lease_revision"]
                    != expansion["prior_lease_revision"] + 1
                    or any(
                        not isinstance(expansion[name], list)
                        or len(expansion[name]) > 256
                        or len(expansion[name]) != len(set(expansion[name]))
                        or any(
                            not isinstance(path, str)
                            or not path
                            or "\x00" in path
                            for path in expansion[name]
                        )
                        for name in ("prior_files", "files")
                    )
                    or not set(expansion["prior_files"]).issubset(
                        expansion["files"]
                    )
                    or expansion["prior_files"] == expansion["files"]
                    or any(
                        not isinstance(expansion[name], str)
                        or len(expansion[name]) != 64
                        or any(
                            character not in "0123456789abcdef"
                            for character in expansion[name]
                        )
                        for name in ("admission_sha256", "operation_sha256")
                    )
                ):
                    raise StateError("steering expansion receipt is invalid")
                normalized_payload["admission_expansion"] = json.loads(
                    canonical_json(expansion)
                )
        elif kind == "CANCEL":
            if (
                not isinstance(payload, dict)
                or set(payload) not in (
                    {"request_id"},
                    {"request_id", "manifest_pin"},
                )
            ):
                raise StateError("cancellation command payload is invalid")
            command_value = payload["request_id"]
            if (
                not isinstance(command_value, str)
                or not command_value.startswith("cancel-")
                or len(command_value) != 39
                or any(character not in "0123456789abcdef" for character in command_value[7:])
            ):
                raise StateError("cancellation request id is invalid")
            normalized_payload = {"request_id": command_value}
            if "manifest_pin" in payload:
                normalized_payload["manifest_pin"] = _validate_cancellation_manifest_pin(
                    payload["manifest_pin"]
                )
        else:
            raise StateError("command kind is invalid")
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key
            or "\x00" in idempotency_key
        ):
            raise StateError("command idempotency key is invalid")
        try:
            key_size = len(idempotency_key.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise StateError("command idempotency key must be valid UTF-8") from error
        if key_size > 256:
            raise StateError("command idempotency key exceeds its byte bound")
        payload_json = canonical_json(normalized_payload)
        if len(payload_json.encode("utf-8")) > MAX_COMMAND_PAYLOAD_BYTES:
            raise StateError("command payload exceeds its storage bound")
        return command_value, payload_json, idempotency_key

    def _append_command_event(
        self,
        connection: sqlite3.Connection,
        job: sqlite3.Row,
        attempt: sqlite3.Row,
        kind: str,
        payload: dict[str, Any],
    ) -> Event:
        """Append a small command receipt inside its command-state transaction."""
        if attempt["status"] in TERMINAL_STATUSES:
            raise InvalidTransition("cannot append a command event to a terminal attempt")
        payload_json = canonical_json(payload)
        payload_bytes = len(payload_json.encode("utf-8"))
        if payload_bytes > MAX_STATE_EVENT_BYTES:
            raise StateError("command receipt exceeds its byte bound")
        if attempt["event_count"] >= MAX_ATTEMPT_EVENTS:
            raise StateError("attempt event count exceeds its bound")
        if attempt["event_bytes"] + payload_bytes > MAX_ATTEMPT_EVENT_BYTES:
            raise StateError("attempt event bytes exceed their bound")
        previous = connection.execute(
            """
            SELECT sequence, event_hash
            FROM events
            WHERE job_id = ?
            ORDER BY sequence DESC
            LIMIT 1
            """,
            (job["id"],),
        ).fetchone()
        sequence = 1 if previous is None else previous["sequence"] + 1
        previous_hash = None if previous is None else previous["event_hash"]
        hash_envelope = canonical_json(
            {
                "kind": kind,
                "payload": payload,
                "previous_hash": previous_hash,
                "sequence": sequence,
            }
        )
        event_hash = hashlib.sha256(hash_envelope.encode("utf-8")).hexdigest()
        new_revision = job["revision"] + 1
        now = _utc_now()
        cursor = connection.execute(
            """
            INSERT INTO events(
                job_id, attempt_id, sequence, kind, payload_json,
                previous_hash, event_hash, revision, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job["id"],
                attempt["id"],
                sequence,
                kind,
                payload_json,
                previous_hash,
                event_hash,
                new_revision,
                now,
            ),
        )
        connection.execute(
            """
            UPDATE attempts
            SET revision = ?, event_count = event_count + 1,
                event_bytes = event_bytes + ?, updated_at = ?
            WHERE id = ?
            """,
            (new_revision, payload_bytes, now, attempt["id"]),
        )
        updated = connection.execute(
            """
            UPDATE jobs
            SET revision = ?, updated_at = ?
            WHERE id = ? AND revision = ?
            """,
            (new_revision, now, job["id"], job["revision"]),
        )
        if updated.rowcount != 1:
            current = self._job_row(connection, job["id"])["revision"]
            raise RevisionConflict(job["id"], job["revision"], current)
        row = connection.execute(
            "SELECT * FROM events WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return self._event_from_row(row)

    @staticmethod
    def _command_receipt_payload(
        command: sqlite3.Row | dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "command_id": command["id"],
            "command_sequence": command["sequence"],
            "command_kind": command["kind"],
        }

    def _reserved_command_receipts(
        self,
        connection: sqlite3.Connection,
        job: sqlite3.Row,
        attempt: sqlite3.Row,
        *,
        extra_queued: tuple[dict[str, Any], ...] = (),
    ) -> tuple[int, int]:
        """Return the durable count/byte capacity owed to accepted commands."""

        if job["status"] != "RUNNING" or attempt["command_inbox_state"] != "OPEN":
            return 0, 0
        rows = connection.execute(
            """
            SELECT id, sequence, kind, status
            FROM commands
            WHERE job_id = ? AND attempt_id = ?
                AND status IN ('QUEUED', 'DELIVERING')
            ORDER BY sequence
            """,
            (job["id"], attempt["id"]),
        ).fetchall()
        pending: list[sqlite3.Row | dict[str, Any]] = [*rows, *extra_queued]
        receipt_count = 1
        receipt_bytes = 0
        closure_items: list[dict[str, Any]] = []
        for command in pending:
            command_payload = self._command_receipt_payload(command)
            if command["status"] == "QUEUED":
                receipt_count += 1
                receipt_bytes += len(canonical_json(command_payload).encode("utf-8"))
            receipt_count += 1
            receipt_bytes += len(canonical_json(command_payload).encode("utf-8"))
            closure_items.append(
                {
                    "command_id": command["id"],
                    "command_sequence": command["sequence"],
                    "prior_status": command["status"],
                }
            )
        closure_payload = {"failed_commands": closure_items}
        receipt_bytes += len(canonical_json(closure_payload).encode("utf-8"))
        return receipt_count, receipt_bytes

    def _reserved_cancellation_receipts(
        self,
        connection: sqlite3.Connection,
        job: sqlite3.Row,
        attempt: sqlite3.Row,
    ) -> tuple[int, int]:
        """Reserve every event still required to finish an admitted interrupt."""

        if (
            job["status"] != "INTERRUPTING"
            or attempt["command_inbox_state"] != "OPEN"
        ):
            return 0, 0
        rows = connection.execute(
            """
            SELECT * FROM commands
            WHERE job_id = ? AND attempt_id = ?
            ORDER BY sequence
            """,
            (job["id"], attempt["id"]),
        ).fetchall()
        cancellations = [
            row
            for row in rows
            if row["kind"] == "CANCEL" and row["status"] != "DELIVERY_FAILED"
        ]
        if len(cancellations) != 1:
            raise StateError("interrupting attempt cancellation cardinality is invalid")
        cancellation = cancellations[0]
        count = 1  # CANCELLATION_INBOX_CLOSED
        receipt_bytes = 0
        closure_items: list[dict[str, Any]] = []
        for command in rows:
            if command["status"] not in {"QUEUED", "DELIVERING"}:
                continue
            command_payload = self._command_receipt_payload(command)
            if command["kind"] == "CANCEL" and command["status"] == "QUEUED":
                count += 1
                receipt_bytes += len(
                    canonical_json(command_payload).encode("utf-8")
                )
            if command["status"] == "DELIVERING" or command["kind"] == "CANCEL":
                count += 1
                receipt_bytes += len(
                    canonical_json(command_payload).encode("utf-8")
                )
            closure_items.append(
                {
                    "command_id": command["id"],
                    "command_sequence": command["sequence"],
                    "prior_status": command["status"],
                }
            )
        receipt_bytes += len(
            canonical_json(
                {
                    "failed_commands": closure_items,
                    "quiescence_required": True,
                }
            ).encode("utf-8")
        )
        if cancellation["status"] == "DELIVERY_FAILED":
            return count, receipt_bytes

        prior_kinds = {
            row["kind"]
            for row in connection.execute(
                "SELECT kind FROM events WHERE job_id = ? AND attempt_id = ?",
                (job["id"], attempt["id"]),
            ).fetchall()
        }
        if "CANCELLATION_ACKNOWLEDGED" not in prior_kinds:
            count += 2  # exact control_response CLAUDE_EVENT + acknowledgement
            receipt_bytes += (
                MAX_CANCELLATION_CONTROL_EVENT_BYTES
                + MAX_CANCELLATION_RESERVED_EVENT_BYTES
            )
        has_term = "CANCELLATION_TERM_SENT" in prior_kinds
        has_provider_exit = "CANCELLATION_PROVIDER_EXITED" in prior_kinds
        has_kill = "CANCELLATION_KILL_SENT" in prior_kinds
        if has_term and has_provider_exit:
            raise StateError("cancellation termination evidence is contradictory")
        if has_provider_exit and has_kill:
            raise StateError("provider-exit cancellation cannot contain KILL evidence")
        if not has_term and not has_provider_exit:
            count += 2  # worst case: TERM followed by KILL
        elif has_term and not has_kill:
            count += 1
        if "CANCELLATION_QUIESCENT" not in prior_kinds:
            count += 1
        if "CANCELLATION_ACKNOWLEDGED" in prior_kinds:
            receipt_bytes += MAX_CANCELLATION_RESERVED_EVENT_BYTES
        return count, receipt_bytes

    @staticmethod
    def _is_expected_cancellation_control_event(
        kind: str,
        payload: Any,
        request_id: str,
    ) -> bool:
        if kind != "CLAUDE_EVENT" or not isinstance(payload, dict):
            return False
        if set(payload) != {
            "type",
            "subtype",
            "model",
            "parent_tool_use_id",
            "data",
        }:
            return False
        return (
            payload["type"] == "control_response"
            and payload["subtype"] is None
            and payload["model"] is None
            and payload["parent_tool_use_id"] is None
            and payload["data"]
            == {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": request_id,
                    "response": {},
                },
            }
        )

    def request_cancellation(
        self,
        job_id: str,
        *,
        expected_revision: int,
        live_worker_identity: str | None = None,
        cancellation_manifest_pin: dict[str, object] | None = None,
    ) -> CancellationAdmission:
        """Atomically receipt one exact-revision cancellation before effects."""

        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise StateError("expected revision must be a non-negative integer")
        if live_worker_identity is not None:
            live_worker_identity = self._validate_worker_identity(
                live_worker_identity
            )
        if cancellation_manifest_pin is not None:
            cancellation_manifest_pin = _validate_cancellation_manifest_pin(
                cancellation_manifest_pin
            )
        with self._transaction() as connection:
            job = self._job_row(connection, job_id)
            attempt = self._current_attempt_row(connection, job)
            existing_row = connection.execute(
                """
                SELECT * FROM commands
                WHERE job_id = ? AND attempt_id = ? AND kind = 'CANCEL'
                    AND status != 'DELIVERY_FAILED'
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (job_id, attempt["id"]),
            ).fetchone()
            existing = (
                None if existing_row is None else self._command_from_row(existing_row)
            )

            def require_repeat_revision() -> None:
                if existing is None:
                    return
                admitted_revision = existing.expected_revision + 2
                if expected_revision not in {
                    existing.expected_revision,
                    admitted_revision,
                    job["revision"],
                }:
                    raise RevisionConflict(
                        job_id,
                        expected_revision,
                        job["revision"],
                    )

            # A terminal result or an already-admitted interrupt is an immutable
            # idempotent outcome. This ordering lets natural completion win a
            # race without rewriting its terminal attempt or receipt.
            if job["status"] in TERMINAL_STATUSES:
                if existing is None:
                    if job["revision"] != expected_revision:
                        raise RevisionConflict(
                            job_id,
                            expected_revision,
                            job["revision"],
                        )
                else:
                    require_repeat_revision()
                return CancellationAdmission(
                    command=existing,
                    status=job["status"],
                    revision=job["revision"],
                    idempotent=True,
                    outcome="already_terminal",
                )
            if job["status"] == "INTERRUPTING":
                if existing is None:
                    raise StateError("interrupting attempt has no cancellation receipt")
                require_repeat_revision()
                return CancellationAdmission(
                    command=existing,
                    status=job["status"],
                    revision=job["revision"],
                    idempotent=True,
                    outcome="already_interrupting",
                )
            if job["revision"] != expected_revision:
                raise RevisionConflict(job_id, expected_revision, job["revision"])
            if job["status"] != "RUNNING":
                raise InvalidTransition("cancellation requires a RUNNING attempt")
            if (
                live_worker_identity is None
                or attempt["worker_pid"] is None
                or attempt["worker_identity"] != live_worker_identity
            ):
                raise WorkerIdentityUnavailable(
                    "live worker identity is required for cancellation"
                )
            if attempt["command_inbox_state"] == "CLOSED":
                return CancellationAdmission(
                    command=None,
                    status=job["status"],
                    revision=job["revision"],
                    idempotent=True,
                    outcome="natural_completion_won",
                )
            if attempt["command_inbox_state"] != "OPEN":
                raise StateError("stored command inbox state is invalid")
            # Re-sample the non-reusable process birth identity while the same
            # BEGIN IMMEDIATE transaction owns the command-inbox cleanup fence.
            # A service-side liveness check is only a preflight hint: the worker
            # may die or begin natural cleanup before admission reaches SQLite.
            if not process_instance_is_live(
                attempt["worker_pid"],
                attempt["worker_identity"],
            ):
                raise WorkerIdentityUnavailable(
                    "recorded worker identity is not live at cancellation admission"
                )
            if existing is not None:
                raise StateError("running attempt already has a cancellation command")
            count = connection.execute(
                "SELECT COUNT(*) FROM commands WHERE job_id = ?",
                (job_id,),
            ).fetchone()[0]
            if count >= MAX_COMMANDS_PER_JOB:
                raise StateError("job command count exceeds its bound")
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM commands WHERE job_id = ?",
                (job_id,),
            ).fetchone()[0]
            command_id = uuid.uuid4().hex
            request_id = f"cancel-{command_id}"
            payload = {"request_id": request_id}
            if cancellation_manifest_pin is not None:
                payload["manifest_pin"] = cancellation_manifest_pin
            idempotency_key = f"cancel-{attempt['id']}-{command_id}"
            _request_id, payload_json, idempotency_key = self._command_inputs(
                expected_revision=expected_revision,
                kind="CANCEL",
                payload=payload,
                idempotency_key=idempotency_key,
            )
            accepted_payload = {
                "command_id": command_id,
                "command_sequence": sequence,
                "command_kind": "CANCEL",
                "expected_revision": expected_revision,
                "request_id": request_id,
                "payload_hash": hashlib.sha256(
                    payload_json.encode("utf-8")
                ).hexdigest(),
            }
            accepted_bytes = len(canonical_json(accepted_payload).encode("utf-8"))
            reserved_count, reserved_bytes = self._reserved_command_receipts(
                connection,
                job,
                attempt,
                extra_queued=(
                    {
                        "id": command_id,
                        "sequence": sequence,
                        "kind": "CANCEL",
                        "status": "QUEUED",
                    },
                ),
            )
            if attempt["event_count"] + 1 + reserved_count + 5 > MAX_ATTEMPT_EVENTS:
                raise StateError("cancellation receipts exceed the event count bound")
            if (
                attempt["event_bytes"]
                + accepted_bytes
                + reserved_bytes
                + MAX_CANCELLATION_RESERVED_EVENT_BYTES
                + MAX_CANCELLATION_CONTROL_EVENT_BYTES
                > MAX_ATTEMPT_EVENT_BYTES
            ):
                raise StateError("cancellation receipts exceed the event byte bound")
            now = _utc_now()
            connection.execute(
                """
                INSERT INTO commands(
                    id, job_id, attempt_id, sequence, kind, payload_json,
                    expected_revision, idempotency_key, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'CANCEL', ?, ?, ?, 'QUEUED', ?, ?)
                """,
                (
                    command_id,
                    job_id,
                    attempt["id"],
                    sequence,
                    payload_json,
                    expected_revision,
                    idempotency_key,
                    now,
                    now,
                ),
            )
            accepted = self._append_command_event(
                connection,
                job,
                attempt,
                "CANCELLATION_ACCEPTED",
                accepted_payload,
            )
            interrupt_revision = accepted.revision + 1
            now = _utc_now()
            updated_attempt = connection.execute(
                """
                UPDATE attempts
                SET status = 'INTERRUPTING', revision = ?, updated_at = ?
                WHERE id = ? AND status = 'RUNNING'
                """,
                (interrupt_revision, now, attempt["id"]),
            )
            updated_job = connection.execute(
                """
                UPDATE jobs
                SET status = 'INTERRUPTING', revision = ?, updated_at = ?
                WHERE id = ? AND revision = ? AND status = 'RUNNING'
                """,
                (interrupt_revision, now, job_id, accepted.revision),
            )
            if updated_attempt.rowcount != 1 or updated_job.rowcount != 1:
                raise StateError("cancellation admission lost its state transition")
            command = self._command_from_row(
                connection.execute(
                    "SELECT * FROM commands WHERE id = ?", (command_id,)
                ).fetchone()
            )
            return CancellationAdmission(
                command=command,
                status="INTERRUPTING",
                revision=interrupt_revision,
                idempotent=False,
                outcome="accepted",
            )

    def _queue_command_in_transaction(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        *,
        expected_revision: int,
        kind: str,
        payload: Any,
        idempotency_key: str,
    ) -> Command:
        """Accept one command inside the caller's already-open transaction."""
        _prompt, payload_json, idempotency_key = self._command_inputs(
            expected_revision=expected_revision,
            kind=kind,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        job = self._job_row(connection, job_id)
        if job["revision"] != expected_revision:
            raise RevisionConflict(job_id, expected_revision, job["revision"])
        attempt = self._current_attempt_row(connection, job)
        if job["status"] != "RUNNING":
            raise InvalidTransition("steering requires a RUNNING attempt")
        if attempt["command_inbox_state"] != "OPEN":
            raise InvalidTransition("steering inbox is closed")
        if connection.execute(
            "SELECT 1 FROM commands WHERE job_id = ? AND idempotency_key = ?",
            (job_id, idempotency_key),
        ).fetchone() is not None:
            raise IdempotencyConflict("duplicate steering command is not allowed")
        count = connection.execute(
            "SELECT COUNT(*) FROM commands WHERE job_id = ?",
            (job_id,),
        ).fetchone()[0]
        if count >= MAX_COMMANDS_PER_JOB:
            raise StateError("job command count exceeds its bound")
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM commands WHERE job_id = ?",
            (job_id,),
        ).fetchone()[0]
        command_id = uuid.uuid4().hex
        queued_event_payload = {
            "command_id": command_id,
            "command_sequence": sequence,
            "command_kind": kind,
            "expected_revision": expected_revision,
            "payload_hash": hashlib.sha256(
                payload_json.encode("utf-8")
            ).hexdigest(),
        }
        queued_event_bytes = len(
            canonical_json(queued_event_payload).encode("utf-8")
        )
        reserved_count, reserved_bytes = self._reserved_command_receipts(
            connection,
            job,
            attempt,
            extra_queued=(
                {
                    "id": command_id,
                    "sequence": sequence,
                    "kind": kind,
                    "status": "QUEUED",
                },
            ),
        )
        if attempt["event_count"] + 1 + reserved_count > MAX_ATTEMPT_EVENTS:
            raise StateError("steering command receipts exceed the event count bound")
        if (
            attempt["event_bytes"] + queued_event_bytes + reserved_bytes
            > MAX_ATTEMPT_EVENT_BYTES
        ):
            raise StateError("steering command receipts exceed the event byte bound")
        now = _utc_now()
        connection.execute(
            """
            INSERT INTO commands(
                id, job_id, attempt_id, sequence, kind, payload_json,
                expected_revision, idempotency_key, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'QUEUED', ?, ?)
            """,
            (
                command_id,
                job_id,
                attempt["id"],
                sequence,
                kind,
                payload_json,
                expected_revision,
                idempotency_key,
                now,
                now,
            ),
        )
        self._append_command_event(
            connection,
            job,
            attempt,
            "COMMAND_QUEUED",
            queued_event_payload,
        )
        row = connection.execute(
            "SELECT * FROM commands WHERE id = ?", (command_id,)
        ).fetchone()
        return self._command_from_row(row)

    def queue_command(
        self,
        job_id: str,
        *,
        expected_revision: int,
        kind: str,
        payload: Any,
        idempotency_key: str,
    ) -> Command:
        """Accept and receipt one steering command at an exact running revision."""
        with self._transaction() as connection:
            return self._queue_command_in_transaction(
                connection,
                job_id,
                expected_revision=expected_revision,
                kind=kind,
                payload=payload,
                idempotency_key=idempotency_key,
            )

    @staticmethod
    def _owned_dispatch(
        attempt: sqlite3.Row,
        *,
        dispatch_token: str,
        worker_pid: int,
    ) -> bool:
        return (
            attempt["dispatch_state"] == "DISPATCHED"
            and attempt["dispatch_token"] == dispatch_token
            and attempt["worker_pid"] == worker_pid
        )

    def claim_next_command(
        self,
        job_id: str,
        *,
        dispatch_token: str,
        worker_pid: int,
    ) -> Command | None:
        """Fence one queued command to this worker before external delivery."""
        dispatch_token = self._validate_dispatch_token(dispatch_token)
        if isinstance(worker_pid, bool) or not isinstance(worker_pid, int) or worker_pid <= 0:
            raise StateError("worker pid must be a positive integer")
        # The worker polls this path repeatedly. Avoid taking SQLite's writer lock
        # when the durable inbox is empty; a command arriving after this read is
        # observed on the next bounded poll.
        with self.connection() as connection:
            queued = connection.execute(
                "SELECT 1 FROM commands WHERE job_id = ? AND status = 'QUEUED' LIMIT 1",
                (job_id,),
            ).fetchone()
        if queued is None:
            return None
        with self._transaction() as connection:
            job = self._job_row(connection, job_id)
            attempt = self._current_attempt_row(connection, job)
            if job["status"] not in {"RUNNING", "INTERRUPTING"}:
                return None
            if (
                attempt["status"] != job["status"]
                or attempt["command_inbox_state"] != "OPEN"
            ):
                return None
            if not self._owned_dispatch(
                attempt,
                dispatch_token=dispatch_token,
                worker_pid=worker_pid,
            ):
                raise StateError("worker does not own command delivery")
            if job["status"] == "INTERRUPTING":
                row = connection.execute(
                    """
                    SELECT * FROM commands
                    WHERE job_id = ? AND attempt_id = ?
                        AND status = 'QUEUED' AND kind = 'CANCEL'
                    ORDER BY sequence
                    LIMIT 1
                    """,
                    (job_id, attempt["id"]),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT * FROM commands
                    WHERE job_id = ? AND attempt_id = ?
                        AND status = 'QUEUED' AND kind = 'STEER'
                    ORDER BY sequence
                    LIMIT 1
                    """,
                    (job_id, attempt["id"]),
                ).fetchone()
            if row is None:
                return None
            command = self._command_from_row(row)
            now = _utc_now()
            updated = connection.execute(
                """
                UPDATE commands
                SET status = 'DELIVERING', updated_at = ?
                WHERE id = ? AND status = 'QUEUED'
                """,
                (now, command.id),
            )
            if updated.rowcount != 1:
                raise StateError("command delivery claim was lost")
            self._append_command_event(
                connection,
                job,
                attempt,
                "COMMAND_DELIVERY_CLAIMED",
                {
                    "command_id": command.id,
                    "command_sequence": command.sequence,
                    "command_kind": command.kind,
                },
            )
            claimed = connection.execute(
                "SELECT * FROM commands WHERE id = ?", (command.id,)
            ).fetchone()
            return self._command_from_row(claimed)

    def _finish_command_delivery(
        self,
        job_id: str,
        command_id: str,
        *,
        dispatch_token: str,
        worker_pid: int,
        new_status: str,
    ) -> Command:
        dispatch_token = self._validate_dispatch_token(dispatch_token)
        if isinstance(worker_pid, bool) or not isinstance(worker_pid, int) or worker_pid <= 0:
            raise StateError("worker pid must be a positive integer")
        if new_status not in {"DELIVERED", "DELIVERY_FAILED"}:
            raise StateError("command delivery terminal status is invalid")
        with self._transaction() as connection:
            job = self._job_row(connection, job_id)
            attempt = self._current_attempt_row(connection, job)
            if (
                job["status"] not in {"RUNNING", "INTERRUPTING"}
                or attempt["status"] != job["status"]
                or attempt["command_inbox_state"] != "OPEN"
            ):
                raise InvalidTransition(
                    "command delivery mutation requires an active open inbox"
                )
            if not self._owned_dispatch(
                attempt,
                dispatch_token=dispatch_token,
                worker_pid=worker_pid,
            ):
                raise StateError("worker does not own command delivery")
            row = connection.execute(
                "SELECT * FROM commands WHERE id = ? AND job_id = ? AND attempt_id = ?",
                (command_id, job_id, attempt["id"]),
            ).fetchone()
            if row is None:
                raise StateError("command does not belong to the current attempt")
            command = self._command_from_row(row)
            if command.status != "DELIVERING":
                raise StateError("command is not held for delivery")
            if (
                (job["status"] == "RUNNING" and command.kind != "STEER")
                or (job["status"] == "INTERRUPTING" and command.kind not in {"STEER", "CANCEL"})
            ):
                raise StateError("command kind does not match active delivery state")
            connection.execute(
                "UPDATE commands SET status = ?, updated_at = ? WHERE id = ?",
                (new_status, _utc_now(), command_id),
            )
            event_kind = (
                "COMMAND_DELIVERED"
                if new_status == "DELIVERED"
                else "COMMAND_DELIVERY_FAILED"
            )
            self._append_command_event(
                connection,
                job,
                attempt,
                event_kind,
                {
                    "command_id": command.id,
                    "command_sequence": command.sequence,
                    "command_kind": command.kind,
                },
            )
            finished = connection.execute(
                "SELECT * FROM commands WHERE id = ?", (command_id,)
            ).fetchone()
            return self._command_from_row(finished)

    def mark_command_delivered(
        self,
        job_id: str,
        command_id: str,
        *,
        dispatch_token: str,
        worker_pid: int,
    ) -> Command:
        return self._finish_command_delivery(
            job_id,
            command_id,
            dispatch_token=dispatch_token,
            worker_pid=worker_pid,
            new_status="DELIVERED",
        )

    def mark_command_delivery_failed(
        self,
        job_id: str,
        command_id: str,
        *,
        dispatch_token: str,
        worker_pid: int,
    ) -> Command:
        return self._finish_command_delivery(
            job_id,
            command_id,
            dispatch_token=dispatch_token,
            worker_pid=worker_pid,
            new_status="DELIVERY_FAILED",
        )

    def abort_cancellation_delivery(
        self,
        job_id: str,
        command_id: str,
        *,
        reason: str,
        _worker_authority: _WorkerAuthority | None = None,
    ) -> bool:
        """Atomically make a provably incomplete CANCEL frame inert."""

        if reason not in {"frame_not_written", "partial_frame_channel_poisoned"}:
            raise StateError("cancellation delivery failure reason is invalid")
        with self._transaction() as connection:
            job = self._job_row(connection, job_id)
            attempt = self._current_attempt_row(connection, job)
            if job["status"] != "INTERRUPTING" or attempt["status"] != "INTERRUPTING":
                return False
            self._require_worker_authority(
                _worker_authority,
                job_id=job_id,
                attempt=attempt,
            )
            row = connection.execute(
                """
                SELECT * FROM commands
                WHERE id = ? AND job_id = ? AND attempt_id = ?
                """,
                (command_id, job_id, attempt["id"]),
            ).fetchone()
            if row is None:
                raise StateError("cancellation command is not current")
            command = self._command_from_row(row)
            if command.kind != "CANCEL":
                raise StateError("cancellation delivery failure requires CANCEL")
            if command.status == "DELIVERY_FAILED":
                return True
            if command.status != "DELIVERING":
                raise StateError(
                    "cancellation delivery failure requires an unconfirmed frame"
                )
            if attempt["command_inbox_state"] != "OPEN":
                raise InvalidTransition(
                    "cancellation delivery failure requires an open inbox"
                )
            failed_at = _utc_now()
            connection.execute(
                "UPDATE commands SET status = 'DELIVERY_FAILED', updated_at = ? WHERE id = ?",
                (failed_at, command.id),
            )
            failure = self._append_command_event(
                connection,
                job,
                attempt,
                "COMMAND_DELIVERY_FAILED",
                {
                    "command_id": command.id,
                    "command_sequence": command.sequence,
                    "command_kind": command.kind,
                    "reason": reason,
                },
            )
            resumed_revision = failure.revision + 1
            resumed_at = _utc_now()
            updated_attempt = connection.execute(
                """
                UPDATE attempts
                SET status = 'RUNNING', revision = ?, updated_at = ?
                WHERE id = ? AND status = 'INTERRUPTING'
                    AND command_inbox_state = 'OPEN'
                """,
                (resumed_revision, resumed_at, attempt["id"]),
            )
            updated_job = connection.execute(
                """
                UPDATE jobs
                SET status = 'RUNNING', revision = ?, updated_at = ?
                WHERE id = ? AND revision = ? AND status = 'INTERRUPTING'
                """,
                (resumed_revision, resumed_at, job_id, failure.revision),
            )
            if updated_attempt.rowcount != 1 or updated_job.rowcount != 1:
                raise StateError("cancellation delivery rollback lost its fence")
            return True

    def confirm_cancellation_delivery(
        self,
        job_id: str,
        command_id: str,
        *,
        _worker_authority: _WorkerAuthority | None = None,
        _busy_timeout_ms: int = 10_000,
    ) -> Command:
        """Idempotently persist one already-written CANCEL control frame."""

        with self._transaction(_busy_timeout_ms=_busy_timeout_ms) as connection:
            job = self._job_row(connection, job_id)
            attempt = self._current_attempt_row(connection, job)
            if job["status"] != "INTERRUPTING" or attempt["status"] != "INTERRUPTING":
                raise InvalidTransition(
                    "cancellation delivery confirmation requires INTERRUPTING"
                )
            self._require_worker_authority(
                _worker_authority,
                job_id=job_id,
                attempt=attempt,
            )
            row = connection.execute(
                """
                SELECT * FROM commands
                WHERE id = ? AND job_id = ? AND attempt_id = ?
                """,
                (command_id, job_id, attempt["id"]),
            ).fetchone()
            if row is None:
                raise StateError("cancellation command is not current")
            command = self._command_from_row(row)
            if command.kind != "CANCEL":
                raise StateError("cancellation delivery confirmation requires CANCEL")
            if command.status in {"DELIVERED", "ACKNOWLEDGED"}:
                return command
            if command.status != "DELIVERING":
                raise StateError("cancellation frame cannot be confirmed")
            connection.execute(
                "UPDATE commands SET status = 'DELIVERED', updated_at = ? WHERE id = ?",
                (_utc_now(), command.id),
            )
            self._append_command_event(
                connection,
                job,
                attempt,
                "COMMAND_DELIVERED",
                {
                    "command_id": command.id,
                    "command_sequence": command.sequence,
                    "command_kind": command.kind,
                },
            )
            return self._command_from_row(
                connection.execute(
                    "SELECT * FROM commands WHERE id = ?", (command.id,)
                ).fetchone()
            )

    @staticmethod
    def _cancellation_timestamp(value: str) -> str:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise StateError("cancellation timestamp is invalid")
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise StateError("cancellation timestamp is not valid UTF-8") from error
        if size > 128:
            raise StateError("cancellation timestamp exceeds its byte bound")
        return value

    @staticmethod
    def _current_cancellation_row(
        connection: sqlite3.Connection,
        job_id: str,
        attempt_id: str,
    ) -> sqlite3.Row:
        rows = connection.execute(
            """
            SELECT * FROM commands
            WHERE job_id = ? AND attempt_id = ? AND kind = 'CANCEL'
                AND status != 'DELIVERY_FAILED'
            ORDER BY sequence DESC
            """,
            (job_id, attempt_id),
        ).fetchall()
        if len(rows) != 1:
            raise StateError("active cancellation command cardinality is invalid")
        return rows[0]

    @staticmethod
    def _parsed_cancellation_timestamp(value: Any, label: str) -> datetime:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise StateError(f"cancellation {label} timestamp is invalid")
        try:
            instant = datetime.fromisoformat(value)
        except ValueError as error:
            raise StateError(
                f"cancellation {label} timestamp is malformed"
            ) from error
        offset = None if instant.tzinfo is None else instant.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise StateError(f"cancellation {label} timestamp is not UTC")
        return instant

    def _durable_cancellation_receipt(
        self,
        connection: sqlite3.Connection,
        job: sqlite3.Row,
        attempt: sqlite3.Row,
    ) -> dict[str, Any]:
        """Derive the only cancellation receipt authorized by durable evidence."""

        if job["status"] not in {"INTERRUPTING", "CANCELLED"}:
            raise InvalidTransition("cancellation evidence requires an interrupting attempt")
        if attempt["command_inbox_state"] != "CLOSED":
            raise InvalidTransition("cancellation evidence requires a closed inbox")
        command_row = self._current_cancellation_row(
            connection,
            job["id"],
            attempt["id"],
        )
        command = self._command_from_row(command_row)
        if command.status != "ACKNOWLEDGED":
            raise InvalidTransition("cancellation evidence lacks acknowledgement")
        request_id = command.payload.get("request_id")
        provider_pgid = attempt["provider_pgid"]
        if (
            not isinstance(request_id, str)
            or isinstance(provider_pgid, bool)
            or not isinstance(provider_pgid, int)
            or provider_pgid <= 0
        ):
            raise StateError("cancellation durable identity is invalid")

        rows = connection.execute(
            """
            SELECT sequence, kind, payload_json
            FROM events
            WHERE job_id = ? AND attempt_id = ?
            ORDER BY sequence
            """,
            (job["id"], attempt["id"]),
        ).fetchall()
        decoded: list[tuple[int, str, dict[str, Any]]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError) as error:
                raise StateError("stored cancellation event is malformed") from error
            if not isinstance(payload, dict):
                raise StateError("stored cancellation event payload is invalid")
            decoded.append((row["sequence"], row["kind"], payload))
        if any(
            event_kind == "CANCELLATION_CLEANUP_FAILED"
            for _sequence, event_kind, _payload in decoded
        ):
            raise StateError(
                "cancellation cleanup failure irrevocably bars CANCELLED"
            )

        def one(kind: str, predicate: Any) -> tuple[int, dict[str, Any]]:
            matches = [
                (sequence, payload)
                for sequence, event_kind, payload in decoded
                if event_kind == kind and predicate(payload)
            ]
            if len(matches) != 1:
                raise StateError(
                    f"cancellation durable {kind} cardinality is invalid"
                )
            return matches[0]

        command_match = lambda payload: (
            payload.get("command_id") == command.id
            and payload.get("command_sequence") == command.sequence
        )
        accepted = one(
            "CANCELLATION_ACCEPTED",
            lambda payload: command_match(payload)
            and payload.get("request_id") == request_id,
        )
        claimed = one("COMMAND_DELIVERY_CLAIMED", command_match)
        delivered = one("COMMAND_DELIVERED", command_match)
        control = one(
            "CLAUDE_EVENT",
            lambda payload: self._is_expected_cancellation_control_event(
                "CLAUDE_EVENT",
                payload,
                request_id,
            ),
        )
        acknowledged = one(
            "CANCELLATION_ACKNOWLEDGED",
            lambda payload: command_match(payload)
            and payload.get("request_id") == request_id,
        )

        def stage(kind: str) -> tuple[int, dict[str, Any]]:
            return one(
                kind,
                lambda payload: payload.get("request_id") == request_id
                and payload.get("provider_pgid") == provider_pgid,
            )

        term_matches = [
            (sequence, payload)
            for sequence, event_kind, payload in decoded
            if event_kind == "CANCELLATION_TERM_SENT"
            and payload.get("request_id") == request_id
            and payload.get("provider_pgid") == provider_pgid
        ]
        provider_exit_matches = [
            (sequence, payload)
            for sequence, event_kind, payload in decoded
            if event_kind == "CANCELLATION_PROVIDER_EXITED"
            and payload.get("request_id") == request_id
            and payload.get("provider_pgid") == provider_pgid
        ]
        if len(term_matches) > 1 or len(provider_exit_matches) > 1:
            raise StateError("cancellation durable termination cardinality is invalid")
        if bool(term_matches) == bool(provider_exit_matches):
            raise StateError("cancellation durable termination path is invalid")
        term = None if not term_matches else term_matches[0]
        provider_exit = (
            None if not provider_exit_matches else provider_exit_matches[0]
        )
        kill_matches = [
            (sequence, payload)
            for sequence, event_kind, payload in decoded
            if event_kind == "CANCELLATION_KILL_SENT"
            and payload.get("request_id") == request_id
            and payload.get("provider_pgid") == provider_pgid
        ]
        if len(kill_matches) > 1:
            raise StateError("cancellation durable KILL cardinality is invalid")
        kill = None if not kill_matches else kill_matches[0]
        if provider_exit is not None:
            if kill is not None:
                raise StateError(
                    "provider-exit cancellation cannot contain KILL evidence"
                )
            if (
                provider_exit[1].get("termination_mode")
                != "provider_exit_after_ack"
            ):
                raise StateError("provider-exit cancellation mode is invalid")
        quiescent = stage("CANCELLATION_QUIESCENT")
        if quiescent[1].get("non_zombie_descendants") != 0:
            raise StateError("cancellation durable quiescence proof is invalid")
        closed = one(
            "CANCELLATION_INBOX_CLOSED",
            lambda payload: payload.get("quiescence_required") is True,
        )

        sequence_chain = [
            accepted[0],
            claimed[0],
            delivered[0],
            control[0],
            acknowledged[0],
            term[0] if term is not None else provider_exit[0],
        ]
        if kill is not None:
            sequence_chain.append(kill[0])
        sequence_chain.extend((quiescent[0], closed[0]))
        if sequence_chain != sorted(sequence_chain) or len(sequence_chain) != len(
            set(sequence_chain)
        ):
            raise StateError("cancellation durable event order is invalid")

        acknowledged_at = acknowledged[1].get("control_acknowledged_at")
        term_sent_at = None if term is None else term[1].get("recorded_at")
        provider_exited_at = (
            None if provider_exit is None else provider_exit[1].get("recorded_at")
        )
        kill_sent_at = None if kill is None else kill[1].get("recorded_at")
        quiescent_at = quiescent[1].get("recorded_at")
        instants = [
            self._parsed_cancellation_timestamp(acknowledged_at, "acknowledgement"),
        ]
        if term_sent_at is not None:
            instants.append(self._parsed_cancellation_timestamp(term_sent_at, "TERM"))
        else:
            instants.append(
                self._parsed_cancellation_timestamp(
                    provider_exited_at,
                    "provider exit",
                )
            )
        if kill_sent_at is not None:
            instants.append(
                self._parsed_cancellation_timestamp(kill_sent_at, "KILL")
            )
        instants.append(
            self._parsed_cancellation_timestamp(quiescent_at, "quiescence")
        )
        if instants != sorted(instants):
            raise StateError("cancellation durable timestamps are out of order")
        receipt = {
            "request_id": request_id,
            "provider_pgid": provider_pgid,
            "control_acknowledged_at": acknowledged_at,
            "term_sent_at": term_sent_at,
            "kill_sent_at": kill_sent_at,
            "quiescent_at": quiescent_at,
            "non_zombie_descendants": 0,
        }
        if provider_exited_at is not None:
            receipt.update(
                {
                    "provider_exited_at": provider_exited_at,
                    "termination_mode": "provider_exit_after_ack",
                }
            )
        return receipt

    def validate_cancellation_receipt(
        self,
        job_id: str,
        cancellation: Any,
    ) -> dict[str, Any]:
        """Cross-check a cancellation claim against its immutable state chain."""

        with self.connection() as connection:
            job = self._job_row(connection, job_id)
            attempt = self._current_attempt_row(connection, job)
            expected = self._durable_cancellation_receipt(
                connection,
                job,
                attempt,
            )
        if cancellation != expected:
            raise StateError("cancellation receipt contradicts durable evidence")
        return dict(expected)

    def validate_provider_cancellation(
        self,
        job_id: str,
        *,
        request_id: str,
        provider_pgid: int,
        _worker_authority: _WorkerAuthority | None = None,
        dispatch_token: str | None = None,
        worker_pid: int | None = None,
    ) -> Command:
        """Prove the live worker owns the exact persisted provider identity."""

        if (
            isinstance(provider_pgid, bool)
            or not isinstance(provider_pgid, int)
            or provider_pgid <= 0
        ):
            raise StateError("provider cancellation process identity is invalid")
        with self.connection() as connection:
            job = self._job_row(connection, job_id)
            attempt = self._current_attempt_row(connection, job)
            if job["status"] != "INTERRUPTING" or attempt["status"] != "INTERRUPTING":
                raise InvalidTransition("provider cancellation requires INTERRUPTING")
            self._require_worker_authority(
                _worker_authority,
                job_id=job_id,
                attempt=attempt,
            )
            if attempt["provider_pgid"] != provider_pgid:
                raise StateError("provider process-group identity does not match durable state")
            row = self._current_cancellation_row(connection, job_id, attempt["id"])
            command = self._command_from_row(row)
            if command.payload.get("request_id") != request_id:
                raise StateError("cancellation request does not match durable state")
            return command

    def acknowledge_cancellation(
        self,
        job_id: str,
        *,
        request_id: str,
        acknowledged_at: str,
        acknowledgement_deadline: float,
        _worker_authority: _WorkerAuthority | None = None,
        dispatch_token: str | None = None,
        worker_pid: int | None = None,
    ) -> Command:
        """Persist the exact successful control response before any signal."""

        acknowledged_at = self._cancellation_timestamp(acknowledged_at)
        if (
            isinstance(acknowledgement_deadline, bool)
            or not isinstance(acknowledgement_deadline, (int, float))
            or not math.isfinite(acknowledgement_deadline)
        ):
            raise StateError("cancellation acknowledgement deadline is invalid")
        with self._transaction() as connection:
            job = self._job_row(connection, job_id)
            attempt = self._current_attempt_row(connection, job)
            if job["status"] != "INTERRUPTING" or attempt["status"] != "INTERRUPTING":
                raise InvalidTransition("cancellation acknowledgement requires INTERRUPTING")
            self._require_worker_authority(
                _worker_authority,
                job_id=job_id,
                attempt=attempt,
            )
            row = self._current_cancellation_row(connection, job_id, attempt["id"])
            command = self._command_from_row(row)
            if command.payload.get("request_id") != request_id:
                raise StateError("cancellation acknowledgement request id is invalid")
            if command.status == "ACKNOWLEDGED":
                return command
            if command.status != "DELIVERED":
                raise StateError("cancellation acknowledgement preceded delivery")
            # BEGIN IMMEDIATE above is the persistence fence. Recheck only
            # after it is held, immediately before accepting the ACK, so lock
            # contention cannot turn an on-time readable frame into a late
            # durable acknowledgement.
            if time.monotonic() >= acknowledgement_deadline:
                raise CancellationAcknowledgementExpired(
                    "cancellation acknowledgement deadline expired"
                )
            connection.execute(
                "UPDATE commands SET status = 'ACKNOWLEDGED', updated_at = ? WHERE id = ?",
                (_utc_now(), command.id),
            )
            self._append_command_event(
                connection,
                job,
                attempt,
                "CANCELLATION_ACKNOWLEDGED",
                {
                    "command_id": command.id,
                    "command_sequence": command.sequence,
                    "request_id": request_id,
                    "control_acknowledged_at": acknowledged_at,
                },
            )
            return self._command_from_row(
                connection.execute(
                    "SELECT * FROM commands WHERE id = ?", (command.id,)
                ).fetchone()
            )

    def acknowledge_cancellation_control_event(
        self,
        job_id: str,
        *,
        request_id: str,
        control_event: dict[str, Any],
        acknowledged_at: str,
        acknowledgement_deadline: float,
        _worker_authority: _WorkerAuthority | None = None,
        _busy_timeout_ms: int = 10_000,
    ) -> Command:
        """Atomically persist delivery repair, exact control event, and ACK."""

        acknowledged_at = self._cancellation_timestamp(acknowledged_at)
        if (
            isinstance(acknowledgement_deadline, bool)
            or not isinstance(acknowledgement_deadline, (int, float))
            or not math.isfinite(acknowledgement_deadline)
        ):
            raise StateError("cancellation acknowledgement deadline is invalid")
        if not self._is_expected_cancellation_control_event(
            "CLAUDE_EVENT",
            control_event,
            request_id,
        ):
            raise StateError("cancellation control response is invalid")
        if (
            len(canonical_json(control_event).encode("utf-8"))
            > MAX_CANCELLATION_CONTROL_EVENT_BYTES
        ):
            raise StateError("cancellation control response exceeds its byte bound")
        with self._transaction(_busy_timeout_ms=_busy_timeout_ms) as connection:
            job = self._job_row(connection, job_id)
            attempt = self._current_attempt_row(connection, job)
            if job["status"] != "INTERRUPTING" or attempt["status"] != "INTERRUPTING":
                raise InvalidTransition(
                    "cancellation acknowledgement requires INTERRUPTING"
                )
            self._require_worker_authority(
                _worker_authority,
                job_id=job_id,
                attempt=attempt,
            )
            row = self._current_cancellation_row(connection, job_id, attempt["id"])
            command = self._command_from_row(row)
            if command.payload.get("request_id") != request_id:
                raise StateError("cancellation acknowledgement request id is invalid")
            if command.status == "ACKNOWLEDGED":
                return command
            if command.status not in {"DELIVERING", "DELIVERED"}:
                raise StateError("cancellation acknowledgement preceded delivery")
            if time.monotonic() >= acknowledgement_deadline:
                raise CancellationAcknowledgementExpired(
                    "cancellation acknowledgement deadline expired"
                )

            if command.status == "DELIVERING":
                connection.execute(
                    "UPDATE commands SET status = 'DELIVERED', updated_at = ? WHERE id = ?",
                    (_utc_now(), command.id),
                )
                self._append_command_event(
                    connection,
                    job,
                    attempt,
                    "COMMAND_DELIVERED",
                    {
                        "command_id": command.id,
                        "command_sequence": command.sequence,
                        "command_kind": command.kind,
                    },
                )
                job = self._job_row(connection, job_id)
                attempt = self._current_attempt_row(connection, job)

            existing_controls = [
                existing
                for existing in connection.execute(
                    """
                    SELECT * FROM events
                    WHERE job_id = ? AND attempt_id = ? AND kind = 'CLAUDE_EVENT'
                    ORDER BY sequence
                    """,
                    (job_id, attempt["id"]),
                ).fetchall()
                if self._is_expected_cancellation_control_event(
                    existing["kind"],
                    json.loads(existing["payload_json"]),
                    request_id,
                )
            ]
            if len(existing_controls) > 1:
                raise StateError(
                    "cancellation control response cardinality is invalid"
                )
            if not existing_controls:
                self._append_command_event(
                    connection,
                    job,
                    attempt,
                    "CLAUDE_EVENT",
                    control_event,
                )
                job = self._job_row(connection, job_id)
                attempt = self._current_attempt_row(connection, job)

            # Recheck after every preparatory mutation while BEGIN IMMEDIATE is
            # still held. A deadline crossing rolls the entire transaction
            # back, including any delivery repair/control-event append.
            if time.monotonic() >= acknowledgement_deadline:
                raise CancellationAcknowledgementExpired(
                    "cancellation acknowledgement deadline expired"
                )
            connection.execute(
                "UPDATE commands SET status = 'ACKNOWLEDGED', updated_at = ? WHERE id = ?",
                (_utc_now(), command.id),
            )
            self._append_command_event(
                connection,
                job,
                attempt,
                "CANCELLATION_ACKNOWLEDGED",
                {
                    "command_id": command.id,
                    "command_sequence": command.sequence,
                    "request_id": request_id,
                    "control_acknowledged_at": acknowledged_at,
                },
            )
            return self._command_from_row(
                connection.execute(
                    "SELECT * FROM commands WHERE id = ?", (command.id,)
                ).fetchone()
            )

    def fail_cancellation_acknowledgement(
        self,
        job_id: str,
        *,
        request_id: str,
        reason: str,
        close_inbox_for_terminal_result: bool = False,
        _worker_authority: _WorkerAuthority | None = None,
    ) -> CancellationAckFailure:
        """Atomically make one unacknowledged interrupt inert and resume work."""

        if reason not in {
            "deadline_exceeded",
            "provider_ended",
            "supervision_error",
        }:
            raise StateError("cancellation acknowledgement failure reason is invalid")
        if type(close_inbox_for_terminal_result) is not bool:
            raise StateError("cancellation acknowledgement inbox mode is invalid")
        with self._transaction() as connection:
            job = self._job_row(connection, job_id)
            attempt = self._current_attempt_row(connection, job)
            if job["status"] != "INTERRUPTING" or attempt["status"] != "INTERRUPTING":
                return CancellationAckFailure(False, False, ())
            self._require_worker_authority(
                _worker_authority,
                job_id=job_id,
                attempt=attempt,
            )
            command_row = self._current_cancellation_row(
                connection,
                job_id,
                attempt["id"],
            )
            command = self._command_from_row(command_row)
            if command.payload.get("request_id") != request_id:
                raise StateError("cancellation acknowledgement failure request is stale")
            if command.status == "ACKNOWLEDGED":
                return CancellationAckFailure(False, False, ())
            if command.status not in {"DELIVERING", "DELIVERED"}:
                raise StateError("cancellation acknowledgement failure preceded delivery")
            if attempt["command_inbox_state"] != "OPEN":
                raise InvalidTransition(
                    "cancellation acknowledgement rollback requires an open inbox"
                )
            if command.status == "DELIVERING":
                connection.execute(
                    "UPDATE commands SET status = 'DELIVERED', updated_at = ? WHERE id = ?",
                    (_utc_now(), command.id),
                )
                self._append_command_event(
                    connection,
                    job,
                    attempt,
                    "COMMAND_DELIVERED",
                    {
                        "command_id": command.id,
                        "command_sequence": command.sequence,
                        "command_kind": command.kind,
                    },
                )
                job = self._job_row(connection, job_id)
                attempt = self._current_attempt_row(connection, job)
            failed_at = _utc_now()
            connection.execute(
                "UPDATE commands SET status = 'DELIVERY_FAILED', updated_at = ? WHERE id = ?",
                (failed_at, command.id),
            )
            failure = self._append_command_event(
                connection,
                job,
                attempt,
                "CANCELLATION_ACK_FAILED",
                {
                    "command_id": command.id,
                    "command_sequence": command.sequence,
                    "request_id": request_id,
                    "reason": reason,
                    "failed_at": failed_at,
                },
            )
            failed_commands: tuple[Command, ...] = ()
            final_event_revision = failure.revision
            expected_inbox_state = "OPEN"
            if close_inbox_for_terminal_result:
                refreshed_job = self._job_row(connection, job_id)
                refreshed_attempt = self._current_attempt_row(
                    connection,
                    refreshed_job,
                )
                rows = connection.execute(
                    """
                    SELECT * FROM commands
                    WHERE job_id = ? AND attempt_id = ?
                        AND status IN ('QUEUED', 'DELIVERING')
                    ORDER BY sequence
                    """,
                    (job_id, attempt["id"]),
                ).fetchall()
                pending = tuple(self._command_from_row(row) for row in rows)
                closed_at = _utc_now()
                connection.execute(
                    """
                    UPDATE commands
                    SET status = 'DELIVERY_FAILED', updated_at = ?
                    WHERE job_id = ? AND attempt_id = ?
                        AND status IN ('QUEUED', 'DELIVERING')
                    """,
                    (closed_at, job_id, attempt["id"]),
                )
                closed = connection.execute(
                    """
                    UPDATE attempts
                    SET command_inbox_state = 'CLOSED', updated_at = ?
                    WHERE id = ? AND command_inbox_state = 'OPEN'
                    """,
                    (closed_at, attempt["id"]),
                )
                if closed.rowcount != 1:
                    raise StateError(
                        "cancellation acknowledgement inbox closure lost its fence"
                    )
                refreshed_attempt = connection.execute(
                    "SELECT * FROM attempts WHERE id = ?",
                    (attempt["id"],),
                ).fetchone()
                closure = self._append_command_event(
                    connection,
                    refreshed_job,
                    refreshed_attempt,
                    "COMMAND_INBOX_CLOSED",
                    {
                        "failed_commands": [
                            {
                                "command_id": pending_command.id,
                                "command_sequence": pending_command.sequence,
                                "prior_status": pending_command.status,
                            }
                            for pending_command in pending
                        ],
                    },
                )
                failed_commands = tuple(
                    self._command_from_row(
                        connection.execute(
                            "SELECT * FROM commands WHERE id = ?",
                            (pending_command.id,),
                        ).fetchone()
                    )
                    for pending_command in pending
                )
                final_event_revision = closure.revision
                expected_inbox_state = "CLOSED"
            resumed_revision = final_event_revision + 1
            resumed_at = _utc_now()
            updated_attempt = connection.execute(
                """
                UPDATE attempts
                SET status = 'RUNNING', revision = ?, updated_at = ?
                WHERE id = ? AND status = 'INTERRUPTING'
                    AND command_inbox_state = ?
                """,
                (
                    resumed_revision,
                    resumed_at,
                    attempt["id"],
                    expected_inbox_state,
                ),
            )
            updated_job = connection.execute(
                """
                UPDATE jobs
                SET status = 'RUNNING', revision = ?, updated_at = ?
                WHERE id = ? AND revision = ? AND status = 'INTERRUPTING'
                """,
                (resumed_revision, resumed_at, job_id, final_event_revision),
            )
            if updated_attempt.rowcount != 1 or updated_job.rowcount != 1:
                raise StateError("cancellation acknowledgement rollback lost its fence")
            return CancellationAckFailure(
                True,
                close_inbox_for_terminal_result,
                failed_commands,
            )

    def record_cancellation_stage(
        self,
        job_id: str,
        *,
        request_id: str,
        provider_pgid: int,
        stage: str,
        recorded_at: str,
        _worker_authority: _WorkerAuthority | None = None,
        dispatch_token: str | None = None,
        worker_pid: int | None = None,
    ) -> str:
        """Append one ordered signal/quiescence receipt for the owning worker."""

        kinds = {
            "TERM_SENT": "CANCELLATION_TERM_SENT",
            "KILL_SENT": "CANCELLATION_KILL_SENT",
            "PROVIDER_EXITED": "CANCELLATION_PROVIDER_EXITED",
            "QUIESCENT": "CANCELLATION_QUIESCENT",
        }
        if stage not in kinds:
            raise StateError("cancellation stage is invalid")
        recorded_at = self._cancellation_timestamp(recorded_at)
        if (
            isinstance(provider_pgid, bool)
            or not isinstance(provider_pgid, int)
            or provider_pgid <= 0
        ):
            raise StateError("cancellation stage process identity is invalid")
        event_kind = kinds[stage]
        with self._transaction() as connection:
            job = self._job_row(connection, job_id)
            attempt = self._current_attempt_row(connection, job)
            if job["status"] != "INTERRUPTING" or attempt["status"] != "INTERRUPTING":
                raise InvalidTransition("cancellation stage requires INTERRUPTING")
            self._require_worker_authority(
                _worker_authority,
                job_id=job_id,
                attempt=attempt,
            )
            if attempt["provider_pgid"] != provider_pgid:
                raise StateError("cancellation stage provider identity changed")
            command = self._command_from_row(
                self._current_cancellation_row(connection, job_id, attempt["id"])
            )
            if (
                command.status != "ACKNOWLEDGED"
                or command.payload.get("request_id") != request_id
            ):
                raise StateError("cancellation stage lacks its acknowledgement")
            prior = {
                row["kind"]
                for row in connection.execute(
                    "SELECT kind FROM events WHERE job_id = ? AND attempt_id = ?",
                    (job_id, attempt["id"]),
                ).fetchall()
            }
            if event_kind in prior:
                raise StateError("cancellation stage is already durably recorded")
            has_term = "CANCELLATION_TERM_SENT" in prior
            has_kill = "CANCELLATION_KILL_SENT" in prior
            has_provider_exit = "CANCELLATION_PROVIDER_EXITED" in prior
            has_quiescent = "CANCELLATION_QUIESCENT" in prior
            if has_quiescent and stage != "QUIESCENT":
                raise StateError("cancellation stage order is invalid")
            if stage == "TERM_SENT" and (has_kill or has_provider_exit):
                raise StateError("cancellation stage order is invalid")
            if stage == "KILL_SENT" and (not has_term or has_provider_exit):
                raise StateError("cancellation stage order is invalid")
            if stage == "PROVIDER_EXITED" and (has_term or has_kill):
                raise StateError("cancellation stage order is invalid")
            if stage == "QUIESCENT" and (
                has_term == has_provider_exit
                or (has_provider_exit and has_kill)
            ):
                raise StateError("cancellation stage order is invalid")
            payload = {
                "request_id": request_id,
                "provider_pgid": provider_pgid,
                "recorded_at": recorded_at,
            }
            if stage == "PROVIDER_EXITED":
                payload["termination_mode"] = "provider_exit_after_ack"
            if stage == "QUIESCENT":
                payload["non_zombie_descendants"] = 0
            self._append_command_event(
                connection,
                job,
                attempt,
                event_kind,
                payload,
            )
            return recorded_at

    def record_cancellation_cleanup_failure(
        self,
        job_id: str,
        *,
        request_id: str,
        provider_pgid: int,
        failures: list[dict[str, str]],
        _worker_authority: _WorkerAuthority | None = None,
    ) -> None:
        """Publish a bounded non-success outcome after physical quiescence."""

        if (
            not isinstance(failures, list)
            or not 1 <= len(failures) <= 3
            or any(
                not isinstance(failure, dict)
                or set(failure) != {"error_type", "message", "stage"}
                or not all(isinstance(value, str) and value for value in failure.values())
                or any(len(value.encode("utf-8")) > 512 for value in failure.values())
                for failure in failures
            )
        ):
            raise StateError("cancellation cleanup failure evidence is invalid")
        if (
            isinstance(provider_pgid, bool)
            or not isinstance(provider_pgid, int)
            or provider_pgid <= 0
        ):
            raise StateError("cancellation cleanup provider identity is invalid")
        with self._transaction() as connection:
            job = self._job_row(connection, job_id)
            attempt = self._current_attempt_row(connection, job)
            if job["status"] != "INTERRUPTING" or attempt["status"] != "INTERRUPTING":
                raise InvalidTransition(
                    "cancellation cleanup failure requires INTERRUPTING"
                )
            self._require_worker_authority(
                _worker_authority,
                job_id=job_id,
                attempt=attempt,
            )
            if attempt["provider_pgid"] != provider_pgid:
                raise StateError("cancellation cleanup provider identity changed")
            command = self._command_from_row(
                self._current_cancellation_row(connection, job_id, attempt["id"])
            )
            if (
                command.status != "ACKNOWLEDGED"
                or command.payload.get("request_id") != request_id
            ):
                raise StateError("cancellation cleanup failure lacks acknowledgement")
            if connection.execute(
                """
                SELECT 1 FROM events
                WHERE job_id = ? AND attempt_id = ?
                    AND kind = 'CANCELLATION_CLEANUP_FAILED'
                LIMIT 1
                """,
                (job_id, attempt["id"]),
            ).fetchone() is not None:
                raise StateError("cancellation cleanup failure is already recorded")
            self._append_command_event(
                connection,
                job,
                attempt,
                "CANCELLATION_CLEANUP_FAILED",
                {
                    "request_id": request_id,
                    "provider_pgid": provider_pgid,
                    "failures": failures,
                    "recorded_at": _utc_now(),
                },
            )

    def close_cancellation_inbox(
        self,
        job_id: str,
        *,
        _worker_authority: _WorkerAuthority | None = None,
        dispatch_token: str | None = None,
        worker_pid: int | None = None,
        require_quiescence: bool = True,
    ) -> list[Command]:
        """Settle residual commands after cancellation or failed cancellation."""

        if type(require_quiescence) is not bool:
            raise StateError("cancellation inbox closure mode is invalid")
        with self._transaction() as connection:
            job = self._job_row(connection, job_id)
            attempt = self._current_attempt_row(connection, job)
            if job["status"] != "INTERRUPTING" or attempt["status"] != "INTERRUPTING":
                raise InvalidTransition("cancellation inbox closure requires INTERRUPTING")
            self._require_worker_authority(
                _worker_authority,
                job_id=job_id,
                attempt=attempt,
            )
            if attempt["command_inbox_state"] == "CLOSED":
                return []
            if attempt["command_inbox_state"] != "OPEN":
                raise StateError("stored command inbox state is invalid")
            if require_quiescence:
                quiescent = connection.execute(
                    """
                    SELECT 1 FROM events
                    WHERE job_id = ? AND attempt_id = ?
                        AND kind = 'CANCELLATION_QUIESCENT'
                    LIMIT 1
                    """,
                    (job_id, attempt["id"]),
                ).fetchone()
                if quiescent is None:
                    raise InvalidTransition(
                        "cancellation inbox cannot close before provider quiescence"
                    )
            rows = connection.execute(
                """
                SELECT * FROM commands
                WHERE job_id = ? AND attempt_id = ?
                    AND status IN ('QUEUED', 'DELIVERING')
                ORDER BY sequence
                """,
                (job_id, attempt["id"]),
            ).fetchall()
            pending = [self._command_from_row(row) for row in rows]
            now = _utc_now()
            connection.execute(
                """
                UPDATE commands
                SET status = 'DELIVERY_FAILED', updated_at = ?
                WHERE job_id = ? AND attempt_id = ?
                    AND status IN ('QUEUED', 'DELIVERING')
                """,
                (now, job_id, attempt["id"]),
            )
            connection.execute(
                """
                UPDATE attempts
                SET command_inbox_state = 'CLOSED', updated_at = ?
                WHERE id = ? AND command_inbox_state = 'OPEN'
                """,
                (now, attempt["id"]),
            )
            self._append_command_event(
                connection,
                job,
                attempt,
                "CANCELLATION_INBOX_CLOSED",
                {
                    "failed_commands": [
                        {
                            "command_id": command.id,
                            "command_sequence": command.sequence,
                            "prior_status": command.status,
                        }
                        for command in pending
                    ],
                    "quiescence_required": require_quiescence,
                },
            )
            return [
                self._command_from_row(
                    connection.execute(
                        "SELECT * FROM commands WHERE id = ?", (command.id,)
                    ).fetchone()
                )
                for command in pending
            ]

    def close_command_inbox(
        self,
        job_id: str,
        *,
        dispatch_token: str,
        worker_pid: int,
    ) -> list[Command]:
        """Atomically reject residual commands and close terminal-result admission."""
        dispatch_token = self._validate_dispatch_token(dispatch_token)
        if isinstance(worker_pid, bool) or not isinstance(worker_pid, int) or worker_pid <= 0:
            raise StateError("worker pid must be a positive integer")
        with self._transaction() as connection:
            job = self._job_row(connection, job_id)
            attempt = self._current_attempt_row(connection, job)
            if job["status"] != "RUNNING":
                raise InvalidTransition("command inbox closure requires RUNNING")
            if not self._owned_dispatch(
                attempt,
                dispatch_token=dispatch_token,
                worker_pid=worker_pid,
            ):
                raise StateError("worker does not own command inbox closure")
            if attempt["command_inbox_state"] == "CLOSED":
                return []
            if attempt["command_inbox_state"] != "OPEN":
                raise StateError("stored command inbox state is invalid")
            rows = connection.execute(
                """
                SELECT * FROM commands
                WHERE job_id = ? AND attempt_id = ?
                    AND status IN ('QUEUED', 'DELIVERING')
                ORDER BY sequence
                """,
                (job_id, attempt["id"]),
            ).fetchall()
            pending = [self._command_from_row(row) for row in rows]
            now = _utc_now()
            connection.execute(
                """
                UPDATE commands
                SET status = 'DELIVERY_FAILED', updated_at = ?
                WHERE job_id = ? AND attempt_id = ?
                    AND status IN ('QUEUED', 'DELIVERING')
                """,
                (now, job_id, attempt["id"]),
            )
            connection.execute(
                """
                UPDATE attempts
                SET command_inbox_state = 'CLOSED', updated_at = ?
                WHERE id = ? AND command_inbox_state = 'OPEN'
                """,
                (now, attempt["id"]),
            )
            self._append_command_event(
                connection,
                job,
                attempt,
                "COMMAND_INBOX_CLOSED",
                {
                    "failed_commands": [
                        {
                            "command_id": command.id,
                            "command_sequence": command.sequence,
                            "prior_status": command.status,
                        }
                        for command in pending
                    ],
                },
            )
            return [
                self._command_from_row(
                    connection.execute(
                        "SELECT * FROM commands WHERE id = ?", (command.id,)
                    ).fetchone()
                )
                for command in pending
            ]

    def list_commands(self, job_id: str) -> list[Command]:
        with self.connection() as connection:
            if connection.execute(
                "SELECT 1 FROM jobs WHERE id = ?", (job_id,)
            ).fetchone() is None:
                raise JobNotFound(f"job not found: {job_id}")
            rows = connection.execute(
                "SELECT * FROM commands WHERE job_id = ? ORDER BY sequence",
                (job_id,),
            ).fetchall()
        return [self._command_from_row(row) for row in rows]

    def append_event(
        self,
        job_id: str,
        kind: str,
        payload: Any,
        *,
        expected_revision: int | None = None,
    ) -> Event:
        """Append one canonical, hash-linked event and advance the job revision."""
        payload_json = canonical_json(payload)
        payload_bytes = len(payload_json.encode("utf-8"))
        if payload_bytes > MAX_STATE_EVENT_BYTES:
            raise StateError("event payload exceeds its byte bound")
        with self._transaction() as connection:
            job = self._job_row(connection, job_id)
            actual_revision = job["revision"]
            if expected_revision is not None and actual_revision != expected_revision:
                raise RevisionConflict(job_id, expected_revision, actual_revision)
            attempt = self._current_attempt_row(connection, job)
            if attempt["status"] in TERMINAL_STATUSES:
                raise InvalidTransition(
                    "cannot append an event to a terminal attempt; create a new attempt"
                )
            reserved_count, reserved_bytes = self._reserved_command_receipts(
                connection,
                job,
                attempt,
            )
            if job["status"] == "INTERRUPTING":
                cancellation_count, cancellation_bytes = (
                    self._reserved_cancellation_receipts(
                        connection,
                        job,
                        attempt,
                    )
                )
                cancellation = self._current_cancellation_row(
                    connection,
                    job_id,
                    attempt["id"],
                )
                command_payload = json.loads(cancellation["payload_json"])
                if (
                    cancellation["status"] == "DELIVERED"
                    and self._is_expected_cancellation_control_event(
                        kind,
                        payload,
                        command_payload.get("request_id"),
                    )
                ):
                    cancellation_count -= 1
                    cancellation_bytes -= MAX_CANCELLATION_CONTROL_EVENT_BYTES
                reserved_count += cancellation_count
                reserved_bytes += cancellation_bytes
            if attempt["event_count"] + 1 + reserved_count > MAX_ATTEMPT_EVENTS:
                raise StateError("attempt event count exceeds its bound")
            if (
                attempt["event_bytes"] + payload_bytes + reserved_bytes
                > MAX_ATTEMPT_EVENT_BYTES
            ):
                raise StateError("attempt event bytes exceed their bound")

            previous = connection.execute(
                """
                SELECT sequence, event_hash
                FROM events
                WHERE job_id = ?
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
            sequence = 1 if previous is None else previous["sequence"] + 1
            previous_hash = None if previous is None else previous["event_hash"]
            hash_envelope = canonical_json(
                {
                    "kind": kind,
                    "payload": json.loads(payload_json),
                    "previous_hash": previous_hash,
                    "sequence": sequence,
                }
            )
            event_hash = hashlib.sha256(hash_envelope.encode("utf-8")).hexdigest()
            new_revision = actual_revision + 1
            now = _utc_now()
            cursor = connection.execute(
                """
                INSERT INTO events(
                    job_id, attempt_id, sequence, kind, payload_json,
                    previous_hash, event_hash, revision, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    attempt["id"],
                    sequence,
                    kind,
                    payload_json,
                    previous_hash,
                    event_hash,
                    new_revision,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE attempts
                SET revision = ?, event_count = event_count + 1,
                    event_bytes = event_bytes + ?, updated_at = ?
                WHERE id = ?
                """,
                (new_revision, payload_bytes, now, attempt["id"]),
            )
            updated = connection.execute(
                """
                UPDATE jobs
                SET revision = ?, updated_at = ?
                WHERE id = ? AND revision = ?
                """,
                (new_revision, now, job_id, actual_revision),
            )
            if updated.rowcount != 1:
                current = self._job_row(connection, job_id)["revision"]
                raise RevisionConflict(job_id, actual_revision, current)
            row = connection.execute(
                "SELECT * FROM events WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return self._event_from_row(row)

    def list_events(self, job_id: str) -> list[Event]:
        with self.connection() as connection:
            if connection.execute(
                "SELECT 1 FROM jobs WHERE id = ?", (job_id,)
            ).fetchone() is None:
                raise JobNotFound(f"job not found: {job_id}")
            rows = connection.execute(
                "SELECT * FROM events WHERE job_id = ? ORDER BY sequence",
                (job_id,),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def get_last_event(self, job_id: str) -> Event | None:
        """Return only the latest event without materializing attempt history."""
        with self.connection() as connection:
            if connection.execute(
                "SELECT 1 FROM jobs WHERE id = ?", (job_id,)
            ).fetchone() is None:
                raise JobNotFound(f"job not found: {job_id}")
            row = connection.execute(
                "SELECT * FROM events WHERE job_id = ? ORDER BY sequence DESC LIMIT 1",
                (job_id,),
            ).fetchone()
        return None if row is None else self._event_from_row(row)

    def get_last_attempt_event(self, job_id: str, attempt_id: str) -> Event | None:
        """Return the latest event owned by one immutable attempt."""

        attempt = self.get_attempt(attempt_id)
        if attempt.job_id != job_id:
            raise AttemptNotFound("attempt does not belong to the requested job")
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM events
                WHERE job_id = ? AND attempt_id = ?
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (job_id, attempt_id),
            ).fetchone()
        return None if row is None else self._event_from_row(row)

    def _attempt_artifact_relative(
        self,
        job_id: str,
        attempt_id: str,
        name: str,
        *,
        require_mutable: bool = False,
    ) -> str:
        with self.connection() as connection:
            attempt = connection.execute(
                "SELECT id, job_id, status FROM attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
        if attempt is None or attempt["job_id"] != job_id:
            raise AttemptNotFound("attempt does not belong to the requested job")
        if require_mutable and attempt["status"] in TERMINAL_STATUSES:
            raise InvalidTransition("terminal attempt evidence is immutable")
        relative = Path(name)
        if (
            not name
            or relative.is_absolute()
            or len(relative.parts) != 1
            or relative.parts[0] in {"", ".", ".."}
        ):
            raise ValueError("attempt artifact name must be one safe path component")
        return f"{attempt['id']}/{name}"

    def attempt_artifact_path(
        self,
        job_id: str,
        attempt_id: str,
        name: str,
    ) -> Path:
        relative = self._attempt_artifact_relative(job_id, attempt_id, name)
        return self.runs_path / job_id / relative

    def create_attempt_artifact(
        self,
        job_id: str,
        attempt_id: str,
        name: str,
        content: bytes | str = b"",
        *,
        _lease: _AttemptEvidenceLease | None = None,
    ) -> Path:
        relative = self._attempt_artifact_relative(
            job_id,
            attempt_id,
            name,
            require_mutable=True,
        )
        return self.create_artifact(job_id, relative, content, _lease=_lease)

    @contextmanager
    def open_attempt_artifact(
        self,
        job_id: str,
        attempt_id: str,
        name: str,
        *,
        create: bool = False,
        writable: bool = False,
        _lease: _AttemptEvidenceLease | None = None,
    ) -> Iterator[tuple[int, Path]]:
        relative = self._attempt_artifact_relative(
            job_id,
            attempt_id,
            name,
            require_mutable=create or writable,
        )
        with self.open_artifact(
            job_id,
            relative,
            create=create,
            writable=writable,
            _lease=_lease,
        ) as opened:
            yield opened

    def create_artifact(
        self,
        job_id: str,
        name: str,
        content: bytes | str = b"",
        *,
        _lease: _AttemptEvidenceLease | None = None,
    ) -> Path:
        """Create or replace one private artifact under ``runs/<job-id>/``."""
        job = self.get_job(job_id)
        relative = Path(name)
        if (
            not name
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ValueError("artifact name must be a contained relative path")
        with self._artifact_mutation_lease(job.id, relative, _lease):
            run_directory = self.runs_path / job.id
            artifact = run_directory / relative
            data = content.encode("utf-8") if isinstance(content, str) else content
            final_name = relative.parts[-1]
            with self._artifact_parent(job.id, relative.parts[:-1]) as parent_descriptor:
                try:
                    existing = os.stat(
                        final_name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    self._validate_private_artifact_info(existing)

                temporary_name = f".forge-{uuid.uuid4().hex}.tmp"
                descriptor: int | None = os.open(
                    temporary_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    FILE_MODE,
                    dir_fd=parent_descriptor,
                )
                try:
                    os.fchmod(descriptor, FILE_MODE)
                    view = memoryview(data)
                    while view:
                        written = os.write(descriptor, view)
                        if written <= 0:
                            raise StateError("short write to artifact")
                        view = view[written:]
                    os.fsync(descriptor)
                    os.replace(
                        temporary_name,
                        final_name,
                        src_dir_fd=parent_descriptor,
                        dst_dir_fd=parent_descriptor,
                    )
                    held = os.fstat(descriptor)
                    current = os.stat(
                        final_name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                    self._validate_private_artifact_info(held)
                    self._validate_private_artifact_info(current)
                    if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
                        raise StateError("artifact path was substituted during publication")
                    os.fsync(parent_descriptor)
                except BaseException:
                    try:
                        os.unlink(temporary_name, dir_fd=parent_descriptor)
                    except FileNotFoundError:
                        pass
                    raise
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
        return artifact

    @contextmanager
    def open_artifact(
        self,
        job_id: str,
        name: str,
        *,
        create: bool = False,
        writable: bool = False,
        _lease: _AttemptEvidenceLease | None = None,
    ) -> Iterator[tuple[int, Path]]:
        """Open one private artifact through anchored no-follow descriptors.

        The final path and every parent are revalidated after use so a caller can
        safely hash from the held descriptor and detect path/inode substitution.
        """
        job = self.get_job(job_id)
        relative = Path(name)
        if (
            not name
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ValueError("artifact name must be a contained relative path")
        final_name = relative.parts[-1]
        flags = os.O_NOFOLLOW | (os.O_RDWR if writable else os.O_RDONLY)
        if create:
            flags |= os.O_CREAT | os.O_EXCL
        artifact = self.runs_path / job.id / relative
        lease_context = (
            self._artifact_mutation_lease(job.id, relative, _lease)
            if create or writable
            else nullcontext()
        )
        with lease_context:
            with self._artifact_parent(
                job.id,
                relative.parts[:-1],
                create=create,
            ) as parent_descriptor:
                try:
                    descriptor = os.open(
                        final_name,
                        flags,
                        FILE_MODE,
                        dir_fd=parent_descriptor,
                    )
                except OSError as error:
                    if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                        raise StateError("artifact path contains a symlink") from error
                    raise
                try:
                    information = os.fstat(descriptor)
                    if create:
                        os.fchmod(descriptor, FILE_MODE)
                        information = os.fstat(descriptor)
                    self._validate_private_artifact_info(information)
                    yield descriptor, artifact
                    after = os.fstat(descriptor)
                    self._validate_private_artifact_info(after)
                    current = os.stat(
                        final_name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                    self._validate_private_artifact_info(current)
                    if (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino):
                        raise StateError("artifact path was substituted while open")
                finally:
                    os.close(descriptor)

    @contextmanager
    def _artifact_parent(
        self,
        job_id: str,
        relative_parent_parts: tuple[str, ...],
        *,
        create: bool = True,
    ) -> Iterator[int]:
        """Open an artifact parent without following any directory symlink."""
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            root_descriptor = os.open(self.root_path, directory_flags)
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise StateError("artifact root contains a symlink") from error
            raise
        descriptors = [root_descriptor]
        components = ("runs", job_id, *relative_parent_parts)
        try:
            self._validate_private_directory_info(os.fstat(descriptors[0]))
            for component in components:
                parent_descriptor = descriptors[-1]
                created = False
                if create:
                    try:
                        os.mkdir(component, DIRECTORY_MODE, dir_fd=parent_descriptor)
                        created = True
                    except FileExistsError:
                        pass
                try:
                    descriptor = os.open(
                        component,
                        directory_flags,
                        dir_fd=parent_descriptor,
                    )
                except OSError as exc:
                    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                        raise ValueError("artifact path contains a symlink") from exc
                    raise
                if created:
                    os.fchmod(descriptor, DIRECTORY_MODE)
                self._validate_private_directory_info(os.fstat(descriptor))
                descriptors.append(descriptor)
            yield descriptors[-1]
            self._verify_directory_binding(components, descriptors)
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    @staticmethod
    def _validate_private_directory_info(information: os.stat_result) -> None:
        if not stat.S_ISDIR(information.st_mode):
            raise StateError("artifact directory is not a directory")
        if information.st_uid != os.geteuid():
            raise StateError("artifact directory is not owned by the current user")
        if stat.S_IMODE(information.st_mode) != DIRECTORY_MODE:
            raise StateError("artifact directory is not private")

    @staticmethod
    def _validate_private_artifact_info(information: os.stat_result) -> None:
        if not stat.S_ISREG(information.st_mode):
            raise StateError("artifact is not a regular file")
        if information.st_uid != os.geteuid():
            raise StateError("artifact is not owned by the current user")
        if stat.S_IMODE(information.st_mode) != FILE_MODE:
            raise StateError("artifact is not private")
        if information.st_nlink != 1:
            raise StateError("artifact must have exactly one hard link")

    def _verify_directory_binding(
        self,
        components: tuple[str, ...],
        held_descriptors: list[int],
    ) -> None:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            root_descriptor = os.open(self.root_path, directory_flags)
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise StateError("artifact root contains a symlink") from error
            raise
        fresh = [root_descriptor]
        try:
            self._validate_private_directory_info(os.fstat(fresh[0]))
            held_root = os.fstat(held_descriptors[0])
            current_root = os.fstat(fresh[0])
            if (held_root.st_dev, held_root.st_ino) != (
                current_root.st_dev,
                current_root.st_ino,
            ):
                raise StateError("artifact root was substituted while open")
            for index, component in enumerate(components, start=1):
                try:
                    descriptor = os.open(
                        component,
                        directory_flags,
                        dir_fd=fresh[-1],
                    )
                except OSError as error:
                    if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                        raise StateError("artifact path contains a symlink") from error
                    raise
                fresh.append(descriptor)
                held = os.fstat(held_descriptors[index])
                current = os.fstat(descriptor)
                self._validate_private_directory_info(current)
                if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
                    raise StateError("artifact directory path was substituted while open")
        finally:
            for descriptor in reversed(fresh):
                os.close(descriptor)

    @staticmethod
    def _job_row(connection: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise JobNotFound(f"job not found: {job_id}")
        return row

    @staticmethod
    def _current_attempt_row(
        connection: sqlite3.Connection,
        job: sqlite3.Row,
    ) -> sqlite3.Row:
        attempt_id = job["current_attempt_id"]
        row = connection.execute(
            "SELECT * FROM attempts WHERE id = ? AND job_id = ?",
            (attempt_id, job["id"]),
        ).fetchone()
        if row is None:
            raise AttemptNotFound(
                f"current attempt {attempt_id!r} missing for job {job['id']}"
            )
        if row["status"] != job["status"]:
            raise StateError(
                f"job/attempt status mismatch for {job['id']}: "
                f"{job['status']} != {row['status']}"
            )
        return row

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            repo_id=row["repo_id"],
            idempotency_key=row["idempotency_key"],
            request_hash=row["request_hash"],
            payload=json.loads(row["payload_json"]),
            status=row["status"],
            revision=row["revision"],
            current_attempt_id=row["current_attempt_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _attempt_from_row(row: sqlite3.Row) -> Attempt:
        try:
            parent_ids = json.loads(row["parent_tool_use_ids_json"])
        except (TypeError, json.JSONDecodeError) as error:
            raise StateError("stored parent tool-use identities are malformed") from error
        if not isinstance(parent_ids, list) or any(
            not isinstance(item, str) for item in parent_ids
        ):
            raise StateError("stored parent tool-use identities are malformed")
        try:
            models_observed = json.loads(row["models_observed_json"])
        except (TypeError, json.JSONDecodeError) as error:
            raise StateError("stored observed models are malformed") from error
        if (
            not isinstance(models_observed, list)
            or any(not isinstance(item, str) or not item for item in models_observed)
            or len(models_observed) != len(set(models_observed))
            or len(models_observed) > 16
            or row["verification_level"] not in {
                "unverified",
                "alias_observed",
                "full_name_exact",
            }
        ):
            raise StateError("stored observed models are malformed")
        return Attempt(
            id=row["id"],
            job_id=row["job_id"],
            number=row["number"],
            parent_attempt_id=row["parent_attempt_id"],
            status=row["status"],
            revision=row["revision"],
            dispatch_state=row["dispatch_state"],
            dispatch_owner_pid=row["dispatch_owner_pid"],
            dispatch_token=row["dispatch_token"],
            worker_pid=row["worker_pid"],
            worker_identity=row["worker_identity"],
            provider_pgid=row["provider_pgid"],
            provider_identity=row["provider_identity"],
            session_id=row["session_id"],
            resume_session_id=row["resume_session_id"],
            command_inbox_state=row["command_inbox_state"],
            model_override=row["model_override"],
            model_cli_accepted=row["model_cli_accepted"],
            models_observed=tuple(models_observed),
            verification_level=row["verification_level"],
            observed_model=row["observed_model"],
            parent_tool_use_ids=tuple(parent_ids),
            event_count=row["event_count"],
            event_bytes=row["event_bytes"],
            terminal_receipt_name=row["terminal_receipt_name"],
            terminal_receipt_hash=row["terminal_receipt_hash"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> Event:
        return Event(
            id=row["id"],
            job_id=row["job_id"],
            attempt_id=row["attempt_id"],
            sequence=row["sequence"],
            kind=row["kind"],
            payload=json.loads(row["payload_json"]),
            payload_json=row["payload_json"],
            previous_hash=row["previous_hash"],
            event_hash=row["event_hash"],
            revision=row["revision"],
            created_at=row["created_at"],
        )

    @classmethod
    def _command_from_row(cls, row: sqlite3.Row) -> Command:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError, RecursionError) as error:
            raise StateError("stored command payload is malformed") from error
        cls._command_inputs(
            expected_revision=row["expected_revision"],
            kind=row["kind"],
            payload=payload,
            idempotency_key=row["idempotency_key"],
        )
        if row["status"] not in {
            "QUEUED",
            "DELIVERING",
            "DELIVERED",
            "DELIVERY_FAILED",
            "ACKNOWLEDGED",
        }:
            raise StateError("stored command status is invalid")
        if (
            isinstance(row["sequence"], bool)
            or not isinstance(row["sequence"], int)
            or not 1 <= row["sequence"] <= MAX_COMMANDS_PER_JOB
        ):
            raise StateError("stored command sequence is invalid")
        return Command(
            id=row["id"],
            job_id=row["job_id"],
            attempt_id=row["attempt_id"],
            sequence=row["sequence"],
            kind=row["kind"],
            payload=payload,
            expected_revision=row["expected_revision"],
            idempotency_key=row["idempotency_key"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
