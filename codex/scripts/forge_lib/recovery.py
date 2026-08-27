#!/usr/bin/env python3
"""Private, fail-closed outcome ledger for role-scoped Forge recovery.

The shell entry point is deliberately tiny so it remains portable to macOS Bash 3.
This module uses only the Python standard library and SQLite.  It never discovers
or signals processes and never calls a provider or the network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time
import uuid
from typing import Any, Iterable


RC_OK = 0
RC_HANDOFF = 10
RC_ESCALATE = 20
RC_UNKNOWN = 30
RC_ERROR = 1

RECOVERY_DISABLED_MESSAGE = (
    "Automated Forge recovery is disabled in this build; existing work and state "
    "were left unchanged. Use a manual handoff."
)

DEFAULT_FAIL_N = 2
DEFAULT_STALL_T = 1200
DEFAULT_ENV_MAX = 3600
DEFAULT_ALIVE_TTL = 600
DEFAULT_RUN_CEILING = 95

ROLES = ("executor", "decider", "reviewer")
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
CANONICAL_UINT = re.compile(r"^(?:0|[1-9][0-9]*)$")
TERMINAL = "DONE"
MAX_TEXT = 4096


class WatchdogError(Exception):
    """A caller-visible, fail-closed watchdog error."""


class Parser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:
        raise WatchdogError(message)


def _now() -> int:
    value = int(time.time())
    if value <= 0:
        raise WatchdogError("epoch must be > 0 (epoch 0 disables age checks)")
    return value


def _id(label: str, value: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise WatchdogError(
            f"{label} must match [A-Za-z0-9][A-Za-z0-9._-]* (no slashes): {value}"
        )
    return value


def _text(label: str, value: str | None, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if required and not value:
        raise WatchdogError(f"{label} is required")
    if len(value.encode("utf-8")) > MAX_TEXT:
        raise WatchdogError(f"{label} exceeds {MAX_TEXT} bytes")
    # Storage is JSON/SQLite rather than TSV, but strip NUL so every output and
    # audit field remains a conventional text value.
    if "\x00" in value:
        raise WatchdogError(f"{label} contains a NUL byte")
    return value


def _sha256(label: str, value: str | None, *, optional: bool = False) -> str | None:
    if value in (None, "") and optional:
        return None
    normalized = (value or "").lower()
    if not SHA256.fullmatch(normalized):
        raise WatchdogError(f"{label} must be exactly 64 hexadecimal characters")
    return normalized


def _canonical_uint(value: str) -> int:
    if not isinstance(value, str) or not CANONICAL_UINT.fullmatch(value):
        raise argparse.ArgumentTypeError("must be a canonical non-negative integer")
    return int(value, 10)


def _safe_path_ancestors(path: Path, label: str) -> None:
    """Reject caller-controlled symlink ancestors without rejecting macOS aliases."""

    if not path.is_absolute() or ".." in path.parts:
        raise WatchdogError(f"{label} must be an absolute path without '..'")
    current = Path(path.anchor)
    allowed_system_aliases = {Path("/var"), Path("/tmp"), Path("/etc")}
    for component in path.parts[1:-1]:
        current = current / component
        try:
            information = current.lstat()
        except FileNotFoundError:
            break
        except OSError as error:
            raise WatchdogError(f"cannot inspect {label} path component: {error}") from error
        if stat.S_ISLNK(information.st_mode):
            if current not in allowed_system_aliases or information.st_uid != 0:
                raise WatchdogError(
                    f"{label} path component must not be a symlink: {current}"
                )


def _secure_regular(path: Path, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as error:
        raise WatchdogError(f"cannot inspect {label}: {error}") from error
    if stat.S_ISLNK(info.st_mode):
        raise WatchdogError(f"{label} must not be a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise WatchdogError(f"{label} must be a regular file")
    if info.st_nlink != 1:
        raise WatchdogError(f"{label} must have exactly one hard link")
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise WatchdogError(f"{label} must be private and owned by the current user")
    return info


def _state_root(raw: str | None) -> tuple[Path, Path]:
    selected = raw or str(Path.home() / ".codex" / "forge" / "recovery")
    root = Path(selected).expanduser()
    if ".." in root.parts:
        raise WatchdogError("state root must not contain '..'")
    if not root.is_absolute():
        root = Path.cwd() / root
    _safe_path_ancestors(root / "sentinel", "state root")

    # Do not resolve: resolution would turn a forbidden caller-supplied symlink
    # into an apparently ordinary directory.
    try:
        if root.exists() or root.is_symlink():
            info = root.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise WatchdogError("state root must not be a symlink")
            if not stat.S_ISDIR(info.st_mode):
                raise WatchdogError("state root must be a directory")
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise WatchdogError("state root must be private and owned by the current user")
        else:
            root.mkdir(mode=0o700, parents=True)
            info = root.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700
            ):
                raise WatchdogError(
                    "new state root is not private and owned by the current user"
                )
    except WatchdogError:
        raise
    except OSError as error:
        raise WatchdogError(f"cannot prepare private state root: {error}") from error

    database = root / "recovery.sqlite3"
    if database.exists() or database.is_symlink():
        _secure_regular(database, "recovery database")
    return root, database


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    integration_authority TEXT NOT NULL CHECK (integration_authority = 'codex'),
    proof_hash TEXT,
    budget_used INTEGER NOT NULL DEFAULT 0 CHECK (budget_used >= 0),
    budget_limit INTEGER NOT NULL DEFAULT 100 CHECK (budget_limit > 0),
    budget_side TEXT NOT NULL DEFAULT 'run',
    budget_initialized INTEGER NOT NULL DEFAULT 0 CHECK (budget_initialized IN (0,1)),
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS units (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    unit_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    peer TEXT NOT NULL,
    primary_role TEXT NOT NULL CHECK (primary_role IN ('executor','decider','reviewer')),
    objective TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'RUNNING' CHECK (status IN ('RUNNING','DONE')),
    started_at INTEGER NOT NULL,
    progress_at INTEGER NOT NULL,
    alive_at INTEGER,
    blocked_at INTEGER,
    fail_streak INTEGER NOT NULL DEFAULT 0 CHECK (fail_streak >= 0),
    tenure_failures INTEGER NOT NULL DEFAULT 0 CHECK (tenure_failures >= 0),
    failed_owners_json TEXT NOT NULL DEFAULT '[]',
    bridge_state TEXT,
    bridge_job_id TEXT,
    bridge_attempt_id TEXT,
    bridge_revision INTEGER,
    terminal_at INTEGER,
    PRIMARY KEY (run_id, unit_id)
);

CREATE TABLE IF NOT EXISTS roles (
    run_id TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('executor','decider','reviewer')),
    holder TEXT NOT NULL,
    PRIMARY KEY (run_id, unit_id, role),
    FOREIGN KEY (run_id, unit_id) REFERENCES units(run_id, unit_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    UNIQUE (run_id, event_id),
    FOREIGN KEY (run_id, unit_id) REFERENCES units(run_id, unit_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS budget_events (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    event_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (run_id, event_id)
);

CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;

CREATE TRIGGER IF NOT EXISTS run_authority_no_update
BEFORE UPDATE OF integration_authority, proof_hash ON runs
WHEN NEW.integration_authority IS NOT OLD.integration_authority
  OR NEW.proof_hash IS NOT OLD.proof_hash
BEGIN SELECT RAISE(ABORT, 'run authority and proof are immutable'); END;

CREATE TRIGGER IF NOT EXISTS bridge_binding_no_update
BEFORE UPDATE OF bridge_state, bridge_job_id, bridge_attempt_id ON units
WHEN NEW.bridge_state IS NOT OLD.bridge_state
  OR NEW.bridge_job_id IS NOT OLD.bridge_job_id
  OR NEW.bridge_attempt_id IS NOT OLD.bridge_attempt_id
BEGIN SELECT RAISE(ABORT, 'bridge binding is immutable'); END;

CREATE TRIGGER IF NOT EXISTS bridge_revision_no_rewind
BEFORE UPDATE OF bridge_revision ON units
WHEN (OLD.bridge_revision IS NULL AND NEW.bridge_revision IS NOT NULL)
  OR (OLD.bridge_revision IS NOT NULL AND NEW.bridge_revision IS NULL)
  OR (OLD.bridge_revision IS NOT NULL AND NEW.bridge_revision < OLD.bridge_revision)
BEGIN SELECT RAISE(ABORT, 'bridge revision cannot move backward'); END;

CREATE TRIGGER IF NOT EXISTS terminal_status_no_reopen
BEFORE UPDATE OF status ON units
WHEN OLD.status = 'DONE' AND NEW.status <> 'DONE'
BEGIN SELECT RAISE(ABORT, 'terminal units cannot be reopened'); END;

CREATE TRIGGER IF NOT EXISTS terminal_role_no_update
BEFORE UPDATE OF holder ON roles
WHEN EXISTS (
    SELECT 1 FROM units
    WHERE units.run_id=OLD.run_id AND units.unit_id=OLD.unit_id
      AND units.status='DONE'
)
BEGIN SELECT RAISE(ABORT, 'terminal role map is immutable'); END;
"""


class Store:
    def __init__(self, raw_root: str | None):
        self.root, self.path = _state_root(raw_root)

    def connect(self) -> sqlite3.Connection:
        before: os.stat_result | None = None
        try:
            if self.path.exists() or self.path.is_symlink():
                before = _secure_regular(self.path, "recovery database")
            connection = sqlite3.connect(str(self.path), timeout=15)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 15000")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(SCHEMA)
            connection.commit()
            os.chmod(self.path, 0o600)
            after = _secure_regular(self.path, "recovery database")
            # sqlite may have opened a swapped inode.  The private directory and a
            # post-open single-link check bound that race without unsafe shell IO.
            if after.st_nlink != 1:
                raise WatchdogError("recovery database link identity changed")
            if before is not None and (before.st_dev, before.st_ino) != (
                after.st_dev,
                after.st_ino,
            ):
                raise WatchdogError("recovery database identity changed while opening")
            return connection
        except WatchdogError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise WatchdogError(f"recovery storage unavailable: {error}") from error


def _begin(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")


def _run(connection: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()


def _unit(connection: sqlite3.Connection, run_id: str, unit_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM units WHERE run_id=? AND unit_id=?", (run_id, unit_id)
    ).fetchone()


def _registered(connection: sqlite3.Connection, run_id: str, unit_id: str) -> sqlite3.Row:
    row = _unit(connection, run_id, unit_id)
    if row is None:
        raise WatchdogError(f"unit {unit_id} has no ledger entries; run init first")
    return row


def _running(row: sqlite3.Row) -> None:
    if row["status"] == TERMINAL:
        raise WatchdogError(
            f"unit {row['unit_id']} is done; refusing post-terminal event"
        )


def _roles(connection: sqlite3.Connection, run_id: str, unit_id: str) -> dict[str, str]:
    rows = connection.execute(
        "SELECT role, holder FROM roles WHERE run_id=? AND unit_id=? ORDER BY role",
        (run_id, unit_id),
    ).fetchall()
    result = {row["role"]: row["holder"] for row in rows}
    if set(result) != set(ROLES):
        raise WatchdogError("role map is incomplete")
    return result


def _roles_string(mapping: dict[str, str]) -> str:
    return ",".join(f"{role}:{mapping[role]}" for role in ROLES)


def _event_payload(
    kind: str, actor: str, result: str, detail: str, role: str
) -> tuple[str, str, str, str, str]:
    return kind, actor, result, detail, role


def _append_event(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    unit_id: str,
    kind: str,
    actor: str = "",
    result: str = "",
    detail: str = "",
    role: str = "",
    event_id: str | None = None,
    created_at: int,
) -> bool:
    identity = event_id or uuid.uuid4().hex
    _id("--event-id", identity)
    prior = connection.execute(
        "SELECT unit_id,kind,actor,result,detail,role FROM events WHERE run_id=? AND event_id=?",
        (run_id, identity),
    ).fetchone()
    payload = _event_payload(kind, actor, result, detail, role)
    if prior is not None:
        if prior["unit_id"] != unit_id:
            raise WatchdogError("event-id is already bound to a different unit")
        stored = tuple(prior[key] for key in ("kind", "actor", "result", "detail", "role"))
        if stored != payload:
            raise WatchdogError("event-id replay payload does not match the original event")
        return False
    connection.execute(
        """
        INSERT INTO events(event_id,run_id,unit_id,kind,actor,result,detail,role,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (identity, run_id, unit_id, kind, actor, result, detail, role, created_at),
    )
    return True


def _event_replay(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    unit_id: str,
    event_id: str | None,
    kind: str,
    detail: str,
    role: str,
) -> sqlite3.Row | None:
    """Return an exact prior request before current-state derivation can change it."""

    if event_id is None:
        return None
    _id("--event-id", event_id)
    prior = connection.execute(
        """
        SELECT unit_id,kind,actor,result,detail,role
        FROM events WHERE run_id=? AND event_id=?
        """,
        (run_id, event_id),
    ).fetchone()
    if prior is None:
        return None
    if prior["unit_id"] != unit_id:
        raise WatchdogError("event-id is already bound to a different unit")
    if (
        prior["kind"] != kind
        or prior["detail"] != detail
        or prior["role"] != role
    ):
        raise WatchdogError("event-id replay payload does not match the original event")
    return prior


def _bridge_file(path_text: str) -> Path:
    path = Path(path_text).expanduser()
    if ".." in path.parts:
        raise WatchdogError("bridge state path must not contain '..'")
    if not path.is_absolute():
        path = Path.cwd() / path
    _safe_path_ancestors(path, "bridge state")
    _secure_regular(path, "bridge state")
    return path


def _bridge_row(path_text: str, job_id: str) -> sqlite3.Row:
    path = _bridge_file(path_text)
    try:
        uri = path.as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=3) as bridge:
            bridge.row_factory = sqlite3.Row
            columns = {
                row["name"] for row in bridge.execute("PRAGMA table_info(jobs)").fetchall()
            }
            required = {"id", "revision", "current_attempt_id", "status"}
            if not required.issubset(columns):
                raise WatchdogError("bridge jobs contract is unavailable")
            row = bridge.execute(
                "SELECT id,revision,current_attempt_id,status FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
    except WatchdogError:
        raise
    except sqlite3.Error as error:
        raise WatchdogError(f"bridge state unavailable: {error}") from error
    if row is None:
        raise WatchdogError(f"bridge job {job_id} is unavailable")
    return row


def _bridge_revision(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WatchdogError(f"{label} is invalid")
    return value


def _validate_bridge(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    supplied_revision: int | None,
) -> str | None:
    path = row["bridge_state"]
    if path is None:
        if supplied_revision is not None:
            raise WatchdogError("--revision requires a bridge-bound unit")
        return None
    bound_revision = _bridge_revision(row["bridge_revision"], "bound bridge revision")
    live = _bridge_row(path, row["bridge_job_id"])
    if live["current_attempt_id"] != row["bridge_attempt_id"]:
        raise WatchdogError("bridge attempt binding is no longer current")
    live_revision = _bridge_revision(live["revision"], "live bridge revision")
    if live_revision < bound_revision:
        raise WatchdogError(
            f"bridge revision moved backward: bound {bound_revision}, got {live_revision}"
        )
    if supplied_revision is not None and supplied_revision != live_revision:
        raise WatchdogError(
            f"bridge revision mismatch: expected {live_revision}, got {supplied_revision}"
        )
    if live_revision > bound_revision:
        updated = connection.execute(
            """
            UPDATE units SET bridge_revision=?
            WHERE run_id=? AND unit_id=? AND bridge_revision=?
            """,
            (
                live_revision,
                row["run_id"],
                row["unit_id"],
                bound_revision,
            ),
        )
        if updated.rowcount != 1:
            raise WatchdogError("bridge revision advance lost its state fence")
    return str(live["status"])


def _thresholds() -> dict[str, int]:
    return {
        "fail_n": DEFAULT_FAIL_N,
        "handoff_max": 2,
        "stall_t": DEFAULT_STALL_T,
        "env_max": DEFAULT_ENV_MAX,
        "alive_ttl": DEFAULT_ALIVE_TTL,
        "run_ceiling": DEFAULT_RUN_CEILING,
    }


def _parser() -> Parser:
    parser = Parser(prog="recovery-watchdog.sh")
    commands = parser.add_subparsers(dest="command", required=True)

    def base(name: str, *, unit: bool = True) -> Parser:
        command = commands.add_parser(name)
        command.add_argument("--state")
        command.add_argument("--run", required=True)
        if unit:
            command.add_argument("--unit", required=True)
        return command

    init = base("init")
    init.add_argument("--owner", required=True)
    init.add_argument("--peer", required=True)
    init.add_argument("--role", choices=ROLES, default="executor")
    init.add_argument("--objective", default="")
    init.add_argument("--integration-authority", default="codex")
    init.add_argument("--proof-hash")
    init.add_argument("--bridge-state")
    init.add_argument("--job-id")
    init.add_argument("--attempt-id")
    init.add_argument("--revision", type=_canonical_uint)

    attempt = base("attempt")
    attempt.add_argument("--result", choices=("pass", "fail"), required=True)
    attempt.add_argument("--detail", default="")
    attempt.add_argument("--event-id")
    attempt.add_argument("--revision", type=_canonical_uint)

    progress = base("progress")
    progress.add_argument("--evidence", required=True)
    progress.add_argument("--event-id")
    progress.add_argument("--revision", type=_canonical_uint)

    alive = base("alive")
    alive.add_argument("--evidence-hash", required=True)
    alive.add_argument("--event-id")
    alive.add_argument("--revision", type=_canonical_uint)

    block = base("block")
    block.add_argument("--detail", default="")
    block.add_argument("--event-id")
    block.add_argument("--revision", type=_canonical_uint)

    handoff = base("handoff")
    handoff.add_argument("--role", choices=ROLES)
    handoff.add_argument("--to")
    handoff.add_argument("--detail", default="")
    handoff.add_argument("--event-id")
    handoff.add_argument("--revision", type=_canonical_uint)

    done = base("done")
    done.add_argument("--detail", default="")
    done.add_argument("--event-id")
    done.add_argument("--revision", type=_canonical_uint)

    check = base("check")
    check.add_argument("--revision", type=_canonical_uint)

    status = base("status", unit=False)

    budget = base("budget", unit=False)
    # Accepted as a compatibility locator for callers that budget while operating
    # on one unit.  Accounting remains run-level and never attaches to the unit.
    budget.add_argument("--unit")
    budget.add_argument("--used", type=_canonical_uint)
    budget.add_argument("--limit", type=_canonical_uint)
    budget.add_argument("--spend", type=_canonical_uint)
    budget.add_argument("--side", default="run")
    budget.add_argument("--detail", default="")
    budget.add_argument("--event-id")
    return parser


def _reject_duplicate_options(arguments: list[str]) -> None:
    seen: set[str] = set()
    for argument in arguments:
        if argument == "--":
            break
        if not argument.startswith("--"):
            continue
        option = argument.split("=", 1)[0]
        if option in seen:
            raise WatchdogError(f"duplicate option is not allowed: {option}")
        seen.add(option)


def _common(args: argparse.Namespace) -> tuple[str, str | None]:
    run_id = _id("--run", args.run)
    unit_id = getattr(args, "unit", None)
    if unit_id is not None:
        unit_id = _id("--unit", unit_id)
    revision = getattr(args, "revision", None)
    if revision is not None and revision < 0:
        raise WatchdogError("--revision must be a non-negative integer")
    return run_id, unit_id


def _init(store: Store, args: argparse.Namespace) -> str:
    run_id, unit_id = _common(args)
    assert unit_id is not None
    owner = _id("--owner", args.owner)
    peer = _id("--peer", args.peer)
    if owner == peer:
        raise WatchdogError("--owner and --peer must differ (no self-handoff)")
    if args.integration_authority != "codex":
        raise WatchdogError("Codex is the immutable integration authority")
    proof_hash = _sha256("--proof-hash", args.proof_hash, optional=True)
    objective = _text("--objective", args.objective)
    bridge_values = (args.bridge_state, args.job_id, args.attempt_id, args.revision)
    if any(value is not None for value in bridge_values) and not all(
        value is not None for value in bridge_values
    ):
        raise WatchdogError(
            "--bridge-state --job-id --attempt-id --revision must be supplied together"
        )
    bridge_path: str | None = None
    if args.bridge_state is not None:
        job_id = _id("--job-id", args.job_id)
        attempt_id = _id("--attempt-id", args.attempt_id)
        if args.revision < 0:
            raise WatchdogError("--revision must be a non-negative integer")
        path = _bridge_file(args.bridge_state)
        live = _bridge_row(str(path), job_id)
        if live["current_attempt_id"] != attempt_id or int(live["revision"]) != args.revision:
            raise WatchdogError("bridge job/attempt/revision binding does not match")
        bridge_path = str(path)
    now = _now()
    connection = store.connect()
    try:
        _begin(connection)
        if _unit(connection, run_id, unit_id) is not None:
            raise WatchdogError(f"unit {unit_id} already registered; refusing duplicate init")
        existing_run = _run(connection, run_id)
        if existing_run is None:
            connection.execute(
                "INSERT INTO runs(run_id,integration_authority,proof_hash,created_at) VALUES(?,?,?,?)",
                (run_id, "codex", proof_hash, now),
            )
        elif (
            existing_run["integration_authority"] != "codex"
            or existing_run["proof_hash"] != proof_hash
        ):
            raise WatchdogError("run integration authority/proof binding is immutable")
        connection.execute(
            """
            INSERT INTO units(
                run_id,unit_id,owner,peer,primary_role,objective,started_at,progress_at,
                bridge_state,bridge_job_id,bridge_attempt_id,bridge_revision
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                unit_id,
                owner,
                peer,
                args.role,
                objective,
                now,
                now,
                bridge_path,
                args.job_id,
                args.attempt_id,
                args.revision,
            ),
        )
        for role in ROLES:
            holder = owner if role == args.role else peer
            connection.execute(
                "INSERT INTO roles(run_id,unit_id,role,holder) VALUES(?,?,?,?)",
                (run_id, unit_id, role, holder),
            )
        _append_event(
            connection,
            run_id=run_id,
            unit_id=unit_id,
            kind="init",
            actor=owner,
            detail=objective,
            role=args.role,
            created_at=now,
        )
        connection.commit()
        mapping = _roles(connection, run_id, unit_id)
        return (
            f"INIT: unit={unit_id} owner={owner} peer={peer} role={args.role} "
            f"roles={_roles_string(mapping)}"
        )
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _attempt(store: Store, args: argparse.Namespace) -> str:
    run_id, unit_id = _common(args)
    assert unit_id is not None
    detail = _text("--detail", args.detail)
    now = _now()
    connection = store.connect()
    try:
        _begin(connection)
        row = _registered(connection, run_id, unit_id)
        _running(row)
        _validate_bridge(connection, row, args.revision)
        inserted = _append_event(
            connection,
            run_id=run_id,
            unit_id=unit_id,
            kind="attempt",
            actor=row["owner"],
            result=args.result,
            detail=detail,
            event_id=args.event_id,
            created_at=now,
        )
        if inserted:
            if args.result == "pass":
                connection.execute(
                    """
                    UPDATE units SET fail_streak=0, tenure_failures=0, progress_at=?,
                        blocked_at=NULL WHERE run_id=? AND unit_id=?
                    """,
                    (now, run_id, unit_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE units SET fail_streak=fail_streak+1,
                        tenure_failures=tenure_failures+1, blocked_at=NULL
                    WHERE run_id=? AND unit_id=?
                    """,
                    (run_id, unit_id),
                )
        connection.commit()
        return f"ATTEMPT: unit={unit_id} owner={row['owner']} result={args.result}"
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _progress(store: Store, args: argparse.Namespace) -> str:
    run_id, unit_id = _common(args)
    assert unit_id is not None
    evidence = _text("--evidence", args.evidence, required=True)
    now = _now()
    connection = store.connect()
    try:
        _begin(connection)
        row = _registered(connection, run_id, unit_id)
        _running(row)
        _validate_bridge(connection, row, args.revision)
        inserted = _append_event(
            connection,
            run_id=run_id,
            unit_id=unit_id,
            kind="progress",
            actor=row["owner"],
            detail=evidence,
            event_id=args.event_id,
            created_at=now,
        )
        if inserted:
            connection.execute(
                """
                UPDATE units SET progress_at=?,fail_streak=0,tenure_failures=0,
                    blocked_at=NULL WHERE run_id=? AND unit_id=?
                """,
                (now, run_id, unit_id),
            )
        connection.commit()
        return f"PROGRESS: unit={unit_id} owner={row['owner']}"
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _alive(store: Store, args: argparse.Namespace) -> str:
    run_id, unit_id = _common(args)
    assert unit_id is not None
    evidence_hash = _sha256("--evidence-hash", args.evidence_hash)
    now = _now()
    connection = store.connect()
    try:
        _begin(connection)
        row = _registered(connection, run_id, unit_id)
        _running(row)
        _validate_bridge(connection, row, args.revision)
        inserted = _append_event(
            connection,
            run_id=run_id,
            unit_id=unit_id,
            kind="alive",
            actor=row["owner"],
            detail=evidence_hash or "",
            event_id=args.event_id,
            created_at=now,
        )
        # Liveness can defer STALL, but never changes objective progress, failure,
        # or the independent blocked clock.
        if inserted:
            connection.execute(
                "UPDATE units SET alive_at=? WHERE run_id=? AND unit_id=?",
                (now, run_id, unit_id),
            )
        connection.commit()
        return f"ALIVE: unit={unit_id} owner={row['owner']}"
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _block(store: Store, args: argparse.Namespace) -> str:
    run_id, unit_id = _common(args)
    assert unit_id is not None
    detail = _text("--detail", args.detail)
    now = _now()
    connection = store.connect()
    try:
        _begin(connection)
        row = _registered(connection, run_id, unit_id)
        _running(row)
        _validate_bridge(connection, row, args.revision)
        inserted = _append_event(
            connection,
            run_id=run_id,
            unit_id=unit_id,
            kind="block",
            actor=row["owner"],
            detail=detail,
            event_id=args.event_id,
            created_at=now,
        )
        if inserted and row["blocked_at"] is None:
            connection.execute(
                "UPDATE units SET blocked_at=? WHERE run_id=? AND unit_id=?",
                (now, run_id, unit_id),
            )
        connection.commit()
        return f"BLOCK: unit={unit_id} owner={row['owner']}"
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _failed_owners(row: sqlite3.Row) -> set[str]:
    try:
        decoded = json.loads(row["failed_owners_json"])
    except (TypeError, json.JSONDecodeError) as error:
        raise WatchdogError("stored tenure evidence is malformed") from error
    if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
        raise WatchdogError("stored tenure evidence is malformed")
    return set(decoded)


def _handoff(store: Store, args: argparse.Namespace) -> str:
    run_id, unit_id = _common(args)
    assert unit_id is not None
    detail = _text("--detail", args.detail)
    if args.to:
        _id("--to", args.to)
    now = _now()
    connection = store.connect()
    try:
        _begin(connection)
        row = _registered(connection, run_id, unit_id)
        if args.role is not None and args.role != row["primary_role"]:
            raise WatchdogError("only the unit's stuck role may transfer")
        if args.to is not None:
            raise WatchdogError("handoff target is derived from the immutable eligible owners")
        role = row["primary_role"]
        replay = _event_replay(
            connection,
            run_id=run_id,
            unit_id=unit_id,
            event_id=args.event_id,
            kind="handoff",
            detail=detail,
            role=role,
        )
        if replay is not None:
            mapping = _roles(connection, run_id, unit_id)
            sides = {row["owner"], row["peer"], *mapping.values()}
            prior_holder = replay["result"]
            if not prior_holder:
                prior_holder = next(
                    (side for side in sorted(sides) if side != replay["actor"]),
                    "unknown",
                )
            connection.commit()
            return (
                f"HANDOFF: unit={unit_id} role={role} from={prior_holder} "
                f"to={replay['actor']} roles={_roles_string(mapping)} replay=true"
            )

        _running(row)
        run_row = _run(connection, run_id)
        if run_row is None:
            raise WatchdogError("unit has no run-level authority record")
        verdict_code, verdict = _verdict(
            connection,
            run_row,
            row,
            _thresholds(),
            now,
            args.revision,
        )
        if verdict_code != RC_HANDOFF:
            raise WatchdogError(
                f"handoff requires an explicit HANDOFF verdict; observed: {verdict}"
            )
        mapping = _roles(connection, run_id, unit_id)
        current = mapping[role]
        sides = {row["owner"], row["peer"]}
        # owner/peer are current for the primary role; recover the immutable two
        # eligible sides from the complete role map plus those columns.
        sides.update(mapping.values())
        if len(sides) != 2:
            raise WatchdogError("eligible owner set is malformed")
        new_holder = next(side for side in sorted(sides) if side != current)
        if new_holder == current:
            raise WatchdogError(f"handoff target already holds role {role}: {current}")
        inserted = _append_event(
            connection,
            run_id=run_id,
            unit_id=unit_id,
            kind="handoff",
            actor=new_holder,
            result=current,
            detail=detail,
            role=role,
            event_id=args.event_id,
            created_at=now,
        )
        if inserted:
            connection.execute(
                "UPDATE roles SET holder=? WHERE run_id=? AND unit_id=? AND role=?",
                (new_holder, run_id, unit_id, role),
            )
            if role == row["primary_role"]:
                failed = _failed_owners(row)
                # A failed tenure is not one incidental red check.  It is a tenure
                # that reached the same repeat threshold which justifies transfer.
                if int(row["tenure_failures"]) >= _thresholds()["fail_n"]:
                    failed.add(row["owner"])
                connection.execute(
                    """
                    UPDATE units SET owner=?,peer=?,fail_streak=0,tenure_failures=0,
                        failed_owners_json=?,progress_at=?,blocked_at=NULL
                    WHERE run_id=? AND unit_id=?
                    """,
                    (
                        new_holder,
                        current,
                        json.dumps(sorted(failed), separators=(",", ":")),
                        now,
                        run_id,
                        unit_id,
                    ),
                )
        connection.commit()
        post = _roles(connection, run_id, unit_id)
        note = ""
        if post["executor"] == post["decider"]:
            note = " note=executor_equals_decider:re-seat_the_decider"
        return (
            f"HANDOFF: unit={unit_id} role={role} from={current} to={new_holder} "
            f"roles={_roles_string(post)}{note}"
        )
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _done(store: Store, args: argparse.Namespace) -> str:
    run_id, unit_id = _common(args)
    assert unit_id is not None
    detail = _text("--detail", args.detail)
    now = _now()
    connection = store.connect()
    try:
        _begin(connection)
        row = _registered(connection, run_id, unit_id)
        replay = _event_replay(
            connection,
            run_id=run_id,
            unit_id=unit_id,
            event_id=args.event_id,
            kind="done",
            detail=detail,
            role="",
        )
        if replay is not None:
            connection.commit()
            return f"DONE: unit={unit_id} owner={replay['actor']} replay=true"
        _running(row)
        _validate_bridge(connection, row, args.revision)
        inserted = _append_event(
            connection,
            run_id=run_id,
            unit_id=unit_id,
            kind="done",
            actor=row["owner"],
            detail=detail,
            event_id=args.event_id,
            created_at=now,
        )
        if inserted:
            connection.execute(
                "UPDATE units SET status='DONE',terminal_at=? WHERE run_id=? AND unit_id=?",
                (now, run_id, unit_id),
            )
        connection.commit()
        return f"DONE: unit={unit_id} owner={row['owner']}"
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _budget(store: Store, args: argparse.Namespace) -> str:
    run_id, _ = _common(args)
    _id("--side", args.side)
    _text("--detail", args.detail)
    using_absolute = args.used is not None or args.limit is not None
    using_increment = args.spend is not None
    if using_absolute == using_increment:
        raise WatchdogError("budget needs either --used N --limit N or --spend N")
    if using_absolute and (args.used is None or args.limit is None):
        raise WatchdogError("--used and --limit must be supplied together")
    values = (args.used, args.limit) if using_absolute else (args.spend,)
    if any(value is None or value < 0 or value > 1_000_000_000_000 for value in values):
        raise WatchdogError("budget values are outside the admitted integer range")
    if using_absolute and args.limit <= 0:
        raise WatchdogError("--limit must be greater than zero")
    now = _now()
    connection = store.connect()
    try:
        _begin(connection)
        row = _run(connection, run_id)
        if row is None:
            # Preserve the source watchdog's run-level budget independence while
            # fixing integration authority to Codex.
            connection.execute(
                "INSERT INTO runs(run_id,integration_authority,created_at) VALUES(?,'codex',?)",
                (run_id, now),
            )
            row = _run(connection, run_id)
        assert row is not None
        event_payload = json.dumps(
            {
                "used": args.used,
                "limit": args.limit,
                "spend": args.spend,
                "side": args.side,
                "detail": args.detail,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        event_hash = hashlib.sha256(event_payload.encode("utf-8")).hexdigest()
        if args.event_id is not None:
            _id("--event-id", args.event_id)
            prior = connection.execute(
                "SELECT payload_sha256 FROM budget_events WHERE run_id=? AND event_id=?",
                (run_id, args.event_id),
            ).fetchone()
            if prior is not None:
                if prior["payload_sha256"] != event_hash:
                    raise WatchdogError("budget event-id replay payload changed")
                connection.commit()
                return (
                    f"BUDGET: run={run_id} side={row['budget_side']} "
                    f"used={row['budget_used']}/{row['budget_limit']}"
                )
        if using_absolute:
            used, limit = args.used, args.limit
            if int(row["budget_initialized"]) and limit != int(row["budget_limit"]):
                raise WatchdogError("run budget limit is immutable after initialization")
            if used < int(row["budget_used"]):
                raise WatchdogError("authoritative budget usage cannot decrease")
        else:
            if args.spend <= 0:
                raise WatchdogError("incremental budget spend must be positive")
            used = int(row["budget_used"]) + args.spend
            limit = int(row["budget_limit"])
        if used > limit:
            raise WatchdogError("budget usage cannot exceed its admitted limit")
        connection.execute(
            "UPDATE runs SET budget_used=?,budget_limit=?,budget_side=?,budget_initialized=1 WHERE run_id=?",
            (used, limit, args.side, run_id),
        )
        if args.event_id is not None:
            connection.execute(
                "INSERT INTO budget_events(run_id,event_id,payload_sha256,created_at) VALUES(?,?,?,?)",
                (run_id, args.event_id, event_hash, now),
            )
        connection.commit()
        return f"BUDGET: run={run_id} side={args.side} used={used}/{limit}"
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _verdict(
    connection: sqlite3.Connection,
    run_row: sqlite3.Row,
    row: sqlite3.Row,
    thresholds: dict[str, int],
    now: int,
    supplied_revision: int | None,
) -> tuple[int, str]:
    run_id, unit_id = row["run_id"], row["unit_id"]
    # The run ceiling authorizes the next move, so it remains authoritative even
    # when this particular unit is already terminal.
    limit = int(run_row["budget_limit"])
    used = int(run_row["budget_used"])
    ceiling_percent = thresholds["run_ceiling"]
    if ceiling_percent > 0 and used * 100 >= limit * ceiling_percent:
        return (
            RC_ESCALATE,
            f"TRIP: BUDGET run={run_id} side={run_row['budget_side']} "
            f"spent={used}/{limit} action=ESCALATE owner_ask_required propose_one_tier_down",
        )

    if row["status"] == TERMINAL:
        return RC_OK, f"OK: unit={unit_id} done"

    bridge_status = _validate_bridge(connection, row, supplied_revision)
    if bridge_status == "PAUSED_PROVIDER_ACCESS":
        return (
            RC_ESCALATE,
            f"TRIP: PROVIDER_PAUSE unit={unit_id} action=ESCALATE owner_ask_required",
        )
    if bridge_status == "PAUSED_POLICY":
        return (
            RC_ESCALATE,
            f"TRIP: POLICY_PAUSE unit={unit_id} action=ESCALATE owner_ask_required",
        )

    mapping = _roles(connection, run_id, unit_id)
    roles_text = _roles_string(mapping)
    conflict = ""
    if mapping["executor"] == mapping["decider"]:
        conflict = " role_conflict=executor_equals_decider"

    failed = _failed_owners(row)
    if int(row["tenure_failures"]) >= thresholds["fail_n"]:
        failed.add(row["owner"])
    eligible = {row["owner"], row["peer"]}
    eligible.update(mapping.values())
    if len(eligible) == 2 and eligible.issubset(failed):
        return (
            RC_ESCALATE,
            f"TRIP: PINGPONG unit={unit_id} failed_tenures={len(failed)}/2 "
            "action=ESCALATE owner_ask_required",
        )

    fails = int(row["fail_streak"])
    if fails >= thresholds["fail_n"]:
        return (
            RC_HANDOFF,
            f"TRIP: REPEAT unit={unit_id} owner={row['owner']} "
            f"consecutive_fails={fails}/{thresholds['fail_n']} action=HANDOFF "
            f"role={row['primary_role']} to={row['peer']}",
        )

    blocked_at = row["blocked_at"]
    if blocked_at is not None and now - int(blocked_at) >= thresholds["env_max"]:
        return (
            RC_HANDOFF,
            f"TRIP: BLOCKED unit={unit_id} owner={row['owner']} "
            f"blocked_seconds={now-int(blocked_at)}/{thresholds['env_max']} "
            f"action=HANDOFF role={row['primary_role']} to={row['peer']} declare_block=true",
        )

    alive_at = row["alive_at"]
    since_progress = now - int(row["progress_at"])
    if alive_at is not None and now - int(alive_at) < thresholds["alive_ttl"]:
        return (
            RC_OK,
            f"OK: unit={unit_id} owner={row['owner']} roles={roles_text}{conflict} "
            f"stall_deferred_liveness_age={now-int(alive_at)}s/{thresholds['alive_ttl']}s",
        )
    if since_progress >= thresholds["stall_t"]:
        return (
            RC_HANDOFF,
            f"TRIP: STALL unit={unit_id} owner={row['owner']} "
            f"no_objective_progress_seconds={since_progress}/{thresholds['stall_t']} "
            f"action=HANDOFF role={row['primary_role']} to={row['peer']}",
        )
    return (
        RC_OK,
        f"OK: unit={unit_id} owner={row['owner']} roles={roles_text}{conflict} "
        f"fails={fails}/{thresholds['fail_n']} failed_tenures={len(failed)}/2 "
        f"since_progress={since_progress}s/{thresholds['stall_t']}s",
    )


def _check(store: Store, args: argparse.Namespace) -> tuple[int, str]:
    run_id, unit_id = _common(args)
    assert unit_id is not None
    now = _now()
    thresholds = _thresholds()
    connection = store.connect()
    try:
        _begin(connection)
        row = _unit(connection, run_id, unit_id)
        if row is None:
            connection.commit()
            return RC_UNKNOWN, f"UNKNOWN: unit={unit_id} never registered (or ledger unreadable)"
        run_row = _run(connection, run_id)
        if run_row is None:
            raise WatchdogError("unit has no run-level authority record")
        verdict = _verdict(connection, run_row, row, thresholds, now, args.revision)
        connection.commit()
        return verdict
    finally:
        connection.close()


def _status(store: Store, args: argparse.Namespace) -> str:
    run_id, _ = _common(args)
    now = _now()
    thresholds = _thresholds()
    connection = store.connect()
    try:
        _begin(connection)
        run_row = _run(connection, run_id)
        if run_row is None:
            raise WatchdogError(f"no ledger for run {run_id}")
        rows = connection.execute(
            "SELECT * FROM units WHERE run_id=? ORDER BY unit_id", (run_id,)
        ).fetchall()
        units: list[dict[str, Any]] = []
        for row in rows:
            rc, verdict = _verdict(connection, run_row, row, thresholds, now, None)
            row = _registered(connection, run_id, row["unit_id"])
            units.append(
                {
                    "unit": row["unit_id"],
                    "roles": _roles_string(_roles(connection, run_id, row["unit_id"])),
                    "attempts": connection.execute(
                        "SELECT COUNT(*) FROM events WHERE run_id=? AND unit_id=? AND kind='attempt'",
                        (run_id, row["unit_id"]),
                    ).fetchone()[0],
                    "exit": rc,
                    "verdict": verdict,
                    "bridge": (
                        None
                        if row["bridge_state"] is None
                        else {
                            "job_id": row["bridge_job_id"],
                            "attempt_id": row["bridge_attempt_id"],
                            "revision": row["bridge_revision"],
                        }
                    ),
                }
            )
        body = {
            "run": run_id,
            "generated_epoch": now,
            "integration_authority": run_row["integration_authority"],
            "proof_hash": run_row["proof_hash"],
            "thresholds": thresholds,
            "budget": {
                "ceiling_percent": thresholds["run_ceiling"],
                "used": run_row["budget_used"],
                "limit": run_row["budget_limit"],
                "side": run_row["budget_side"],
            },
            "units": units,
        }
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":"))
        connection.commit()
        return encoded
    finally:
        connection.close()


def run(argv: Iterable[str]) -> int:
    del argv
    raise WatchdogError(RECOVERY_DISABLED_MESSAGE)


def main() -> int:
    os.umask(0o077)
    try:
        return run(sys.argv[1:])
    except WatchdogError as error:
        print(f"recovery-watchdog: {error}", file=sys.stderr)
        return RC_ERROR
    except (OSError, sqlite3.Error) as error:
        print(f"recovery-watchdog: storage failure: {error}", file=sys.stderr)
        return RC_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
