"""One durable Forge job worker using Claude's bounded stream-json adapter."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import selectors
import sqlite3
import stat
import subprocess
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from .boundaries import validate_launch
from .claude import (
    MAX_EVENT_BYTES,
    AdapterError,
    ClaudeEvent,
    ClaudeProtocolError,
    JobSpec,
    PreflightError,
    ProviderPolicyDecision,
    ProviderPolicyTracker,
    UnsupportedClaudeCodeVersion,
    build_command,
    encode_stream_message,
    encode_user_message,
    is_supported_claude_code_version,
    parse_event,
    preflight,
    normalize_budget_usd,
)
from .contracts import RECOVERY_DISABLED_MESSAGE
from .processes import (
    PathPin,
    ProviderLeaderExitedBeforeSignal,
    ProviderLaunchError,
    RepositoryLaunchPin,
    _bind_test_cancellation_containment,
    _test_cancellation_manifest_path,
    cancel_process_group,
    process_instance_identity,
    prove_provider_exit_after_cancellation_ack,
    spawn_provider,
    terminate_process_group,
)
from .security import (
    SecurityError,
    build_child_environment,
    build_claude_settings,
    redact_sensitive,
    redact_text,
    write_private_settings,
)
from .state import (
    TERMINAL_STATUSES,
    CancellationAcknowledgementExpired,
    InvalidTransition,
    RevisionConflict,
    StateError,
    Store,
    canonical_json,
)


PRIVATE_FILE_MODE = 0o600
RECOVERY_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep", "Bash")
MAX_ERROR_TEXT_BYTES = 4096
MAX_PROVIDER_EVENTS = 1000
MAX_PROVIDER_EVENT_BYTES = 16 * 1024 * 1024
MAX_EVENTS_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_STDERR_BYTES = 1024 * 1024
MAX_REPORT_BYTES = 1024 * 1024
MAX_RECEIPT_BYTES = 256 * 1024
MAX_WORKER_LOG_BYTES = 1024 * 1024
MAX_COMMAND_DELIVERIES = 1024
MAX_OUTPUT_BOUNDARY_ENTRIES = 250_000
MAX_OUTPUT_BOUNDARY_GIT_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_BOUNDARY_EVENT_ITEMS = 128
_GENERATED_CACHE_ROOTS = (".next", "node_modules/.vite")
# Apart from the two exact opaque generated-cache roots above, ignored dependency
# and build state remains inside the full output-boundary snapshot.  Keep that
# security check bounded without forcing callers to weaken the captured tree.
OUTPUT_BOUNDARY_SNAPSHOT_SECONDS = 180.0
OBJECTIVE_PROGRESS_POLL_SECONDS = 0.5
CANCELLATION_ACK_TIMEOUT_SECONDS = 1.0
WORKER_LAUNCH_BIND_TIMEOUT_SECONDS = 2.0
WORKER_LAUNCH_GATE_TIMEOUT_SECONDS = 15.0
REPOSITORY_PATH_PIN_FIELDS = frozenset(
    {
        "top_level",
        "common_dir",
        "git_marker",
        "git_dir",
        "git_marker_content_hash",
        "git_commondir_file",
        "git_commondir_file_device",
        "git_commondir_file_inode",
        "git_commondir_file_owner",
        "git_commondir_file_type",
        "git_commondir_file_content_hash",
        "git_worktree_backpointer",
        "git_worktree_backpointer_device",
        "git_worktree_backpointer_inode",
        "git_worktree_backpointer_owner",
        "git_worktree_backpointer_type",
        "git_worktree_backpointer_content_hash",
        "worktree_device",
        "worktree_inode",
        "worktree_owner",
        "worktree_type",
        "git_marker_device",
        "git_marker_inode",
        "git_marker_owner",
        "git_marker_type",
        "git_dir_device",
        "git_dir_inode",
        "git_dir_owner",
        "git_dir_type",
        "common_dir_device",
        "common_dir_inode",
        "common_dir_owner",
        "common_dir_type",
    }
)
GIT_METADATA_LIMIT_BYTES = 1024 * 1024
ADMISSION_OUTPUT_LIMIT_BYTES = 64 * 1024
ADMISSION_EVIDENCE_FIELDS = frozenset(
    {
        "version",
        "declaration",
        "lane",
        "product_lane",
        "executor",
        "driver",
        "integration_authority",
        "scheduler_snapshot",
        "controls",
        "review_controls",
        "manifest_entries",
        "predecessor",
        "predecessor_receipts",
        "act2_review",
        "reservation",
        "resume",
        "guard",
        "argv",
        "argv_sha256",
        "stdout",
        "stderr",
        "stdout_sha256",
        "stderr_sha256",
        "result",
        "result_sha256",
        "runtime",
        "sha256",
    }
)


class WorkerError(RuntimeError):
    """Raised when a durable job cannot be executed safely."""


class ProviderPolicyStop(WorkerError):
    """Interrupt provider work for one durable classified policy outcome."""

    def __init__(self, decision: ProviderPolicyDecision):
        self.decision = decision
        super().__init__(decision.reason)


class _ProviderFrameWriteError(WorkerError):
    """A bounded provider-frame failure with exact write progress."""

    def __init__(
        self,
        message: str,
        *,
        bytes_written: int,
        frame_sent_at: float | None = None,
    ):
        super().__init__(message)
        self.bytes_written = bytes_written
        self.frame_sent_at = frame_sent_at


class ProviderOutputBoundaryError(WorkerError):
    """Raised after Claude changes state outside its admitted reservation."""


@dataclass(frozen=True)
class _CommandDelivery:
    command: Any
    frame_sent_at: float
    receipt_pending: bool


@dataclass
class _OutputBoundarySnapshot:
    root: Path
    declared_files: tuple[str, ...]
    workspace_facts: dict[str, tuple[Any, ...]]
    declared_facts: dict[str, tuple[Any, ...]]
    control_pins: dict[str, dict[str, Any]]
    control_facts: dict[str, tuple[Any, ...]]
    git_executable: str
    git_marker: Path
    git_dir: Path
    common_dir: Path
    git_metadata_facts: dict[str, tuple[Any, ...]]
    git_semantic_facts: dict[str, str]
    git_index_fact: tuple[Any, ...]
    opaque_cache_roots: frozenset[str] = frozenset()


def _bounded_text(value: Any, label: str, *, maximum_bytes: int = 4096) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise WorkerError(f"{label} must be non-empty safe text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise WorkerError(f"{label} must be valid UTF-8") from error
    if len(encoded) > maximum_bytes:
        raise WorkerError(f"{label} exceeds its byte limit")
    return value


def _await_launch_binding_release(launch_gate_fd: int | None) -> str | None:
    """Return the parent-authenticated job role before touching job state."""

    if launch_gate_fd is None:
        return None
    if (
        isinstance(launch_gate_fd, bool)
        or not isinstance(launch_gate_fd, int)
        or launch_gate_fd < 3
    ):
        raise WorkerError("worker launch gate descriptor is invalid")
    selector = selectors.DefaultSelector()
    try:
        selector.register(launch_gate_fd, selectors.EVENT_READ)
        ready = selector.select(WORKER_LAUNCH_GATE_TIMEOUT_SECONDS)
        if not ready:
            raise WorkerError("worker launch binding gate timed out")
        release = os.read(launch_gate_fd, 3)
        role = {b"V": "review", b"B": "build", b"R": "recovery"}.get(
            release[1:] if release[:1] == b"\x01" else b""
        )
        if len(release) != 2 or role is None:
            raise WorkerError("worker launch binding was not released")
        return role
    except OSError as error:
        raise WorkerError("worker launch binding gate failed") from error
    finally:
        selector.close()
        try:
            os.close(launch_gate_fd)
        except OSError:
            pass


def _admission_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _provider_family(value: Any) -> str:
    if not isinstance(value, str):
        return "unknown"
    normalized = value.casefold()
    if "codex" in normalized or "sol" in normalized:
        return "codex"
    if "claude" in normalized or "fable" in normalized or "opus" in normalized:
        return "claude"
    return "unknown"


def _validate_sha256(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise WorkerError(f"{label} is not a SHA-256 digest")
    return value


def _validate_admission_pin_shape(
    pin: Any,
    label: str,
    *,
    guard: bool = False,
) -> str:
    """Validate a durable pin without consulting mutable filesystem state."""

    expected = {"path", "device", "inode", "owner", "sha256"}
    if not guard:
        expected.update({"mode", "type"})
    if not isinstance(pin, dict) or set(pin) != expected:
        raise WorkerError(f"{label} pin is malformed")
    raw_path = pin["path"]
    if (
        not isinstance(raw_path, str)
        or not raw_path
        or "\x00" in raw_path
        or not Path(raw_path).is_absolute()
        or ".." in Path(raw_path).parts
    ):
        raise WorkerError(f"{label} path is not exact and absolute")
    if not guard and pin["type"] != "regular":
        raise WorkerError(f"{label} type is invalid")
    for name in ("device", "inode", "owner"):
        value = pin[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise WorkerError(f"{label} {name} is invalid")
    expected_hash = _validate_sha256(pin["sha256"], f"{label} hash")
    if not guard and (
        isinstance(pin["mode"], bool)
        or not isinstance(pin["mode"], int)
        or pin["mode"] < 0
        or pin["mode"] > 0o7777
    ):
        raise WorkerError(f"{label} mode is invalid")
    return expected_hash


def _review_control_items(
    review_controls: dict[str, Any],
) -> tuple[tuple[str, dict[str, Any]], ...]:
    """Flatten the two required review controls and optional ordered domain docs."""

    items = [
        ("plan", review_controls["plan"]),
        ("review_log", review_controls["review_log"]),
    ]
    items.extend(
        (f"domain_doc:{index}", pin)
        for index, pin in enumerate(review_controls.get("domain_docs", []))
    )
    return tuple(items)


def _read_admission_pin(
    pin: Any,
    label: str,
    *,
    guard: bool = False,
    trusted_root: bool = False,
) -> bytes:
    """Open every path component without following links and verify one file pin."""

    expected_hash = _validate_admission_pin_shape(pin, label, guard=guard)
    raw_path = pin["path"]

    directory_fd: int | None = None
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        directory_fd = os.open(os.path.sep, flags)
        parts = Path(raw_path).parts[1:]
        if not parts:
            raise WorkerError(f"{label} must identify a regular file")
        for component in parts[:-1]:
            next_fd = os.open(component, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        file_flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(parts[-1], file_flags, dir_fd=directory_fd)
        information = os.fstat(descriptor)
        if (
            not stat.S_ISREG(information.st_mode)
            or information.st_uid != (0 if trusted_root else os.geteuid())
            or (not guard and information.st_nlink != 1)
            or (trusted_root and stat.S_IMODE(information.st_mode) & 0o022)
            or information.st_size > GIT_METADATA_LIMIT_BYTES
            or (information.st_dev, information.st_ino, information.st_uid)
            != (pin["device"], pin["inode"], pin["owner"])
            or (
                not guard
                and stat.S_IMODE(information.st_mode) != pin["mode"]
            )
        ):
            raise WorkerError(f"{label} identity changed after admission")
        digest = hashlib.sha256()
        data = bytearray()
        total = 0
        while True:
            chunk = os.read(
                descriptor,
                min(65_536, GIT_METADATA_LIMIT_BYTES + 1 - total),
            )
            if not chunk:
                break
            total += len(chunk)
            if total > GIT_METADATA_LIMIT_BYTES:
                raise WorkerError(f"{label} exceeds its byte bound")
            digest.update(chunk)
            data.extend(chunk)
        if total != information.st_size or digest.hexdigest() != expected_hash:
            raise WorkerError(f"{label} content changed after admission")
        return bytes(data)
    except OSError as error:
        raise WorkerError(f"{label} cannot be revalidated safely") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory_fd is not None:
            os.close(directory_fd)


def _output_stat_identity(information: os.stat_result) -> tuple[int, ...]:
    return (
        information.st_dev,
        information.st_ino,
        information.st_uid,
        information.st_mode,
        information.st_nlink,
        information.st_size,
        information.st_mtime_ns,
        information.st_ctime_ns,
    )


def _output_path_fact_at(
    directory_fd: int,
    name: str,
    information: os.stat_result,
    *,
    deadline: float,
) -> tuple[Any, ...]:
    """Capture one no-follow path fact while rejecting an unstable read."""

    if time.monotonic() > deadline:
        raise WorkerError("provider output boundary snapshot timed out")
    if stat.S_ISDIR(information.st_mode):
        return (
            "directory",
            information.st_dev,
            information.st_ino,
            information.st_uid,
            stat.S_IMODE(information.st_mode),
            information.st_nlink,
        )
    if stat.S_ISREG(information.st_mode):
        descriptor: int | None = None
        try:
            flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(name, flags, dir_fd=directory_fd)
            held_before = os.fstat(descriptor)
            if _output_stat_identity(held_before) != _output_stat_identity(information):
                raise WorkerError("provider output path changed while opened")
            digest = hashlib.sha256()
            total = 0
            while True:
                if time.monotonic() > deadline:
                    raise WorkerError("provider output boundary snapshot timed out")
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                digest.update(chunk)
            held_after = os.fstat(descriptor)
            named_after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (
                total != information.st_size
                or _output_stat_identity(held_after)
                != _output_stat_identity(information)
                or _output_stat_identity(named_after)
                != _output_stat_identity(information)
            ):
                raise WorkerError("provider output path changed while hashed")
            return (
                "regular",
                information.st_dev,
                information.st_ino,
                information.st_uid,
                stat.S_IMODE(information.st_mode),
                information.st_nlink,
                information.st_size,
                information.st_mtime_ns,
                information.st_ctime_ns,
                digest.hexdigest(),
            )
        finally:
            if descriptor is not None:
                os.close(descriptor)
    if stat.S_ISLNK(information.st_mode):
        target = os.readlink(name, dir_fd=directory_fd)
        named_after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if _output_stat_identity(named_after) != _output_stat_identity(information):
            raise WorkerError("provider output symlink changed while inspected")
        target_bytes = target if isinstance(target, bytes) else os.fsencode(target)
        return (
            "symlink",
            information.st_dev,
            information.st_ino,
            information.st_uid,
            stat.S_IMODE(information.st_mode),
            information.st_nlink,
            information.st_size,
            information.st_mtime_ns,
            information.st_ctime_ns,
            hashlib.sha256(target_bytes).hexdigest(),
        )
    return (
        "other",
        information.st_dev,
        information.st_ino,
        information.st_uid,
        information.st_mode,
        information.st_nlink,
        information.st_size,
        information.st_mtime_ns,
        information.st_ctime_ns,
    )


def _output_absolute_path_fact(
    path: Path,
    *,
    deadline: float,
) -> tuple[Any, ...]:
    """Capture one exact absolute path without following any path component."""

    if not path.is_absolute() or ".." in path.parts or not path.name:
        raise WorkerError("provider output boundary path is invalid")
    directory_fd: int | None = None
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        directory_fd = os.open(os.path.sep, flags)
        for component in path.parts[1:-1]:
            next_fd = os.open(component, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        try:
            information = os.stat(
                path.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return ("missing",)
        return _output_path_fact_at(
            directory_fd,
            path.name,
            information,
            deadline=deadline,
        )
    except OSError as error:
        raise WorkerError("provider output boundary path cannot be inspected") from error
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


_OUTPUT_DIRECTORY_ENTRY_CACHE: dict[
    tuple[int, int, int, int], frozenset[str]
] = {}


def _exact_output_directory_entries(directory_fd: int) -> frozenset[str]:
    information = os.fstat(directory_fd)
    key = (
        information.st_dev,
        information.st_ino,
        information.st_mtime_ns,
        information.st_ctime_ns,
    )
    cached = _OUTPUT_DIRECTORY_ENTRY_CACHE.get(key)
    if cached is not None:
        return cached
    entries = frozenset(os.listdir(directory_fd))
    if len(_OUTPUT_DIRECTORY_ENTRY_CACHE) >= 4096:
        _OUTPUT_DIRECTORY_ENTRY_CACHE.clear()
    _OUTPUT_DIRECTORY_ENTRY_CACHE[key] = entries
    return entries


def _validate_exact_output_path_spelling(path: Path, label: str) -> None:
    """Require every admitted component's exact case and Unicode spelling."""

    if not path.is_absolute() or ".." in path.parts or not path.name:
        raise WorkerError(f"{label} path spelling is invalid")
    directory_fd: int | None = None
    try:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        directory_fd = os.open(os.path.sep, directory_flags)
        components = path.parts[1:]
        for index, component in enumerate(components):
            entries = _exact_output_directory_entries(directory_fd)
            if component not in entries:
                aliases = [
                    entry
                    for entry in entries
                    if unicodedata.normalize("NFC", entry).casefold()
                    == unicodedata.normalize("NFC", component).casefold()
                ]
                reason = "is aliased" if aliases else "is unavailable"
                raise WorkerError(f"{label} path spelling {reason}")
            if index != len(components) - 1:
                next_fd = os.open(
                    component,
                    directory_flags,
                    dir_fd=directory_fd,
                )
                os.close(directory_fd)
                directory_fd = next_fd
    except OSError as error:
        raise WorkerError(f"{label} path spelling cannot be revalidated") from error
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def _snapshot_output_tree_once(
    root: Path,
    *,
    excluded: frozenset[str] = frozenset(),
    opaque_directories: frozenset[str] = frozenset(),
    deadline: float,
) -> dict[str, tuple[Any, ...]]:
    """Hash a repository tree without following links or trusting Git ignore rules."""

    facts: dict[str, tuple[Any, ...]] = {}
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    root_descriptor: int | None = None

    def walk(directory_fd: int, prefix: tuple[str, ...]) -> None:
        if time.monotonic() > deadline:
            raise WorkerError("provider output boundary snapshot timed out")
        try:
            with os.scandir(directory_fd) as stream:
                names = sorted(entry.name for entry in stream)
        except OSError as error:
            raise WorkerError("provider output tree cannot be enumerated") from error
        for name in names:
            relative = "/".join((*prefix, name))
            if relative in excluded:
                continue
            if len(facts) >= MAX_OUTPUT_BOUNDARY_ENTRIES:
                raise WorkerError("provider output boundary entry limit exceeded")
            try:
                information = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                fact = _output_path_fact_at(
                    directory_fd,
                    name,
                    information,
                    deadline=deadline,
                )
            except OSError as error:
                raise WorkerError("provider output path cannot be inspected") from error
            if (
                relative in opaque_directories
                and fact[0] == "directory"
                and fact[3] == os.geteuid()
                and not fact[4] & 0o022
            ):
                child_descriptor: int | None = None
                try:
                    child_descriptor = os.open(
                        name,
                        directory_flags,
                        dir_fd=directory_fd,
                    )
                    held = os.fstat(child_descriptor)
                    named_after = os.stat(
                        name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                    held_fact = (
                        "opaque_directory",
                        held.st_dev,
                        held.st_ino,
                        held.st_uid,
                        stat.S_IMODE(held.st_mode),
                    )
                    named_fact = (
                        "opaque_directory",
                        named_after.st_dev,
                        named_after.st_ino,
                        named_after.st_uid,
                        stat.S_IMODE(named_after.st_mode),
                    )
                    expected = ("opaque_directory", *fact[1:5])
                    if held_fact != expected or named_fact != expected:
                        raise WorkerError(
                            "provider output opaque directory changed while opened"
                        )
                    facts[relative] = expected
                    continue
                finally:
                    if child_descriptor is not None:
                        os.close(child_descriptor)
            facts[relative] = fact
            if fact[0] != "directory":
                continue
            child_descriptor: int | None = None
            try:
                child_descriptor = os.open(name, directory_flags, dir_fd=directory_fd)
                held = os.fstat(child_descriptor)
                if (
                    held.st_dev,
                    held.st_ino,
                    held.st_uid,
                    stat.S_IMODE(held.st_mode),
                    held.st_nlink,
                ) != fact[1:]:
                    raise WorkerError("provider output directory changed while opened")
                walk(child_descriptor, (*prefix, name))
                named_after = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                held_after = os.fstat(child_descriptor)
                after_fact = (
                    "directory",
                    held_after.st_dev,
                    held_after.st_ino,
                    held_after.st_uid,
                    stat.S_IMODE(held_after.st_mode),
                    held_after.st_nlink,
                )
                named_fact = (
                    "directory",
                    named_after.st_dev,
                    named_after.st_ino,
                    named_after.st_uid,
                    stat.S_IMODE(named_after.st_mode),
                    named_after.st_nlink,
                )
                if after_fact != fact or named_fact != fact:
                    raise WorkerError("provider output directory changed during snapshot")
            finally:
                if child_descriptor is not None:
                    os.close(child_descriptor)

    try:
        root_descriptor = os.open(root, directory_flags)
        walk(root_descriptor, ())
        return facts
    except OSError as error:
        raise WorkerError("provider output tree cannot be opened safely") from error
    finally:
        if root_descriptor is not None:
            os.close(root_descriptor)


def _stable_output_tree(
    root: Path,
    *,
    excluded: frozenset[str] = frozenset(),
    opaque_directories: frozenset[str] = frozenset(),
) -> dict[str, tuple[Any, ...]]:
    deadline = time.monotonic() + OUTPUT_BOUNDARY_SNAPSHOT_SECONDS
    first = _snapshot_output_tree_once(
        root,
        excluded=excluded,
        opaque_directories=opaque_directories,
        deadline=deadline,
    )
    second = _snapshot_output_tree_once(
        root,
        excluded=excluded,
        opaque_directories=opaque_directories,
        deadline=deadline,
    )
    if first != second:
        raise WorkerError("provider output tree changed during stable snapshot")
    return first


def _output_git_environment() -> dict[str, str]:
    return {
        "HOME": "/var/empty",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    }


def _output_git_fact(executable: str, root: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            [executable, "-C", str(root), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
            shell=False,
            env=_output_git_environment(),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise WorkerError("provider output Git fact cannot be inspected") from error
    if (
        completed.returncode != 0
        or len(completed.stdout) > MAX_OUTPUT_BOUNDARY_GIT_BYTES
        or len(completed.stderr) > MAX_OUTPUT_BOUNDARY_GIT_BYTES
    ):
        raise WorkerError("provider output Git fact cannot be inspected")
    return hashlib.sha256(completed.stdout).hexdigest()


def _untracked_generated_cache_roots(
    executable: str,
    root: Path,
) -> frozenset[str]:
    """Permit opacity only for exact cache roots with no tracked descendants."""

    roots: set[str] = set()
    for cache_root in _GENERATED_CACHE_ROOTS:
        try:
            completed = subprocess.run(
                [executable, "-C", str(root), "ls-files", "-z", "--", cache_root],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
                shell=False,
                env=_output_git_environment(),
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise WorkerError("provider output cache authority cannot be inspected") from error
        if (
            completed.returncode != 0
            or len(completed.stdout) > MAX_OUTPUT_BOUNDARY_GIT_BYTES
            or len(completed.stderr) > MAX_OUTPUT_BOUNDARY_GIT_BYTES
        ):
            raise WorkerError("provider output cache authority cannot be inspected")
        if not completed.stdout:
            roots.add(cache_root)
    return frozenset(roots)


def _git_metadata_roots(git_dir: Path, common_dir: Path) -> tuple[tuple[str, Path], ...]:
    if git_dir == common_dir or git_dir.is_relative_to(common_dir):
        return (("common", common_dir),)
    if common_dir.is_relative_to(git_dir):
        return (("git", git_dir),)
    return (("common", common_dir), ("git", git_dir))


def _snapshot_git_metadata(
    git_marker: Path,
    git_dir: Path,
    common_dir: Path,
) -> dict[str, tuple[Any, ...]]:
    facts: dict[str, tuple[Any, ...]] = {}
    for label, root in _git_metadata_roots(git_dir, common_dir):
        deadline = time.monotonic() + OUTPUT_BOUNDARY_SNAPSHOT_SECONDS
        facts[f"{label}:"] = _output_absolute_path_fact(root, deadline=deadline)
        for relative, fact in _stable_output_tree(root).items():
            facts[f"{label}:{relative}"] = fact
    if not any(root == git_marker for _label, root in _git_metadata_roots(git_dir, common_dir)):
        facts["marker:"] = _output_absolute_path_fact(
            git_marker,
            deadline=time.monotonic() + OUTPUT_BOUNDARY_SNAPSHOT_SECONDS,
        )
    return facts


def _capture_output_boundary(
    payload: dict[str, Any],
    identity: Any,
) -> _OutputBoundarySnapshot:
    evidence = payload["admission_evidence"]
    declared_files = tuple(evidence["reservation"]["files"])
    if any(
        relative == cache_root or relative.startswith(cache_root + "/")
        for relative in declared_files
        for cache_root in _GENERATED_CACHE_ROOTS
    ):
        raise WorkerError("admitted output path cannot be inside a generated cache")
    root = identity.top_level
    git_executable = evidence["runtime"]["git"]["path"]
    opaque_cache_roots = _untracked_generated_cache_roots(git_executable, root)
    try:
        marker_relative = str(identity.git_marker.relative_to(root))
    except ValueError:
        marker_exclusions: frozenset[str] = frozenset()
    else:
        marker_exclusions = frozenset({marker_relative})
    workspace_facts = _stable_output_tree(
        root,
        excluded=marker_exclusions,
        opaque_directories=opaque_cache_roots,
    )
    declared_facts = {
        relative: workspace_facts.get(relative, ("missing",))
        for relative in declared_files
    }
    invalid_declared = [
        relative
        for relative, fact in declared_facts.items()
        if not _safe_declared_output_fact(fact)
    ]
    if invalid_declared:
        raise WorkerError(
            "admitted output path is not a single-link regular file or missing"
        )
    control_pins = copy.deepcopy(evidence["controls"])
    review_items = (
        _review_control_items(evidence["review_controls"])
        if evidence["review_controls"]
        else ()
    )
    for role, pin in review_items:
        control_pins[f"review:{role}"] = copy.deepcopy(pin)
    for index, pin in enumerate(evidence["manifest_entries"]):
        control_pins[f"manifest_entry:{index}"] = copy.deepcopy(pin)
    control_facts: dict[str, tuple[Any, ...]] = {}
    for role, pin in control_pins.items():
        label = f"admission control {role}"
        _validate_exact_output_path_spelling(Path(pin["path"]), label)
        _read_admission_pin(pin, label)
        fact = _output_absolute_path_fact(
            Path(pin["path"]),
            deadline=time.monotonic() + OUTPUT_BOUNDARY_SNAPSHOT_SECONDS,
        )
        _validate_control_snapshot_fact(pin, fact, f"admission control {role}")
        control_facts[role] = fact
    git_marker = identity.git_marker
    git_dir = identity.git_dir
    common_dir = identity.common_dir
    git_semantic_facts = {
        "head": _output_git_fact(git_executable, root, "rev-parse", "HEAD"),
        "refs": _output_git_fact(
            git_executable,
            root,
            "for-each-ref",
            "--format=%(refname)%00%(objectname)%00%(symref)",
        ),
    }
    return _OutputBoundarySnapshot(
        root=root,
        declared_files=declared_files,
        workspace_facts=workspace_facts,
        declared_facts=declared_facts,
        control_pins=control_pins,
        control_facts=control_facts,
        git_executable=git_executable,
        git_marker=git_marker,
        git_dir=git_dir,
        common_dir=common_dir,
        git_metadata_facts=_snapshot_git_metadata(git_marker, git_dir, common_dir),
        git_semantic_facts=git_semantic_facts,
        git_index_fact=_output_absolute_path_fact(
            git_dir / "index",
            deadline=time.monotonic() + OUTPUT_BOUNDARY_SNAPSHOT_SECONDS,
        ),
        opaque_cache_roots=opaque_cache_roots,
    )


def _validate_control_snapshot_fact(
    pin: dict[str, Any],
    fact: tuple[Any, ...],
    label: str,
) -> None:
    """Bind a no-follow output fact to one authoritative admission control pin."""

    if (
        len(fact) != 10
        or fact[0] != "regular"
        or fact[1] != pin["device"]
        or fact[2] != pin["inode"]
        or fact[3] != pin["owner"]
        or fact[4] != pin["mode"]
        or fact[5] != 1
        or fact[9] != pin["sha256"]
    ):
        raise WorkerError(f"{label} snapshot identity changed after admission")


def _revalidate_output_boundary_controls(snapshot: _OutputBoundarySnapshot) -> None:
    """Repeat every immutable control check immediately before provider Popen."""

    if set(snapshot.control_pins) != set(snapshot.control_facts):
        raise WorkerError("provider control snapshot is incomplete")
    for role, pin in snapshot.control_pins.items():
        label = f"admission control {role}"
        _validate_exact_output_path_spelling(Path(pin["path"]), label)
        _read_admission_pin(pin, label)
        fact = _output_absolute_path_fact(
            Path(pin["path"]),
            deadline=time.monotonic() + OUTPUT_BOUNDARY_SNAPSHOT_SECONDS,
        )
        _validate_control_snapshot_fact(pin, fact, label)
        if fact != snapshot.control_facts[role]:
            raise WorkerError(f"{label} snapshot changed before provider spawn")


def _changed_fact_names(
    before: dict[str, tuple[Any, ...]],
    after: dict[str, tuple[Any, ...]],
) -> list[str]:
    return sorted(
        name
        for name in set(before) | set(after)
        if before.get(name, ("missing",)) != after.get(name, ("missing",))
    )


def _allowed_declared_parent_change(
    relative: str,
    before: tuple[Any, ...],
    after: tuple[Any, ...],
    declared_files: set[str],
) -> bool:
    """Allow only link-count drift caused by a declared descendant leaf."""

    return (
        _directory_link_count_only_change(before, after)
        and any(path.startswith(relative + "/") for path in declared_files)
    )


def _directory_link_count_only_change(
    before: tuple[Any, ...],
    after: tuple[Any, ...],
) -> bool:
    """Recognize one unchanged directory whose child-directory count moved."""

    return (
        len(before) == 6
        and len(after) == 6
        and before[0] == "directory"
        and after[0] == "directory"
        and before[:5] == after[:5]
        and before[5] != after[5]
    )


def _final_generated_cache_roots(
    workspace_after: dict[str, tuple[Any, ...]],
) -> frozenset[str]:
    """Return exact generated-cache roots captured as opaque real directories."""

    return frozenset(
        root
        for root in _GENERATED_CACHE_ROOTS
        if (
            workspace_after.get(root, ("missing",))[0] == "opaque_directory"
            and workspace_after[root][3] == os.geteuid()
            and not workspace_after[root][4] & 0o022
        )
    )


def _allowed_generated_cache_change(
    relative: str,
    before: tuple[Any, ...],
    after: tuple[Any, ...],
    cache_roots: frozenset[str],
) -> bool:
    """Allow exact cache-root creation and its necessary parent link drift."""

    for root in cache_roots:
        if relative == root:
            return before == ("missing",) and after[0] == "opaque_directory"
        parent, separator, _name = root.rpartition("/")
        if (
            separator
            and relative == parent
            and _directory_link_count_only_change(before, after)
        ):
            return True
    return False


def _final_empty_claude_marker_roots(
    workspace_after: dict[str, tuple[Any, ...]],
) -> frozenset[str]:
    """Find exact `.claude/.cc-writes` markers whose marker child is empty."""

    roots: set[str] = set()
    for relative, fact in workspace_after.items():
        if (
            not _safe_runtime_directory_fact(fact)
            or (relative != ".claude" and not relative.endswith("/.claude"))
        ):
            continue
        child = relative + "/.cc-writes"
        if not _safe_runtime_directory_fact(
            workspace_after.get(child, ("missing",))
        ):
            continue
        if not any(path.startswith(child + "/") for path in workspace_after):
            roots.add(relative)
    return frozenset(roots)


def _safe_runtime_directory_fact(fact: tuple[Any, ...]) -> bool:
    return (
        len(fact) == 6
        and fact[0] == "directory"
        and fact[3] == os.geteuid()
        and not fact[4] & 0o022
    )


def _allowed_empty_claude_marker_change(
    relative: str,
    before: tuple[Any, ...],
    after: tuple[Any, ...],
    marker_roots: frozenset[str],
) -> bool:
    """Allow only an exact empty Claude runtime marker and parent link drift."""

    for root in marker_roots:
        if relative in {root, root + "/.cc-writes"}:
            return (
                before == ("missing",)
                and len(after) == 6
                and after[0] == "directory"
            ) or _directory_link_count_only_change(before, after)
        parent, separator, _name = root.rpartition("/")
        if (
            separator
            and relative == parent
            and _directory_link_count_only_change(before, after)
        ):
            return True
    return False


def _safe_declared_output_fact(fact: tuple[Any, ...]) -> bool:
    """Writable leaves must be absent or one-link regular files."""

    return fact == ("missing",) or (
        len(fact) == 10 and fact[0] == "regular" and fact[5] == 1
    )


class _ObjectiveArtifactTracker:
    """Publish content-only progress for the admitted writable file set."""

    __slots__ = (
        "store",
        "job_id",
        "provider_pgid",
        "provider_identity",
        "root",
        "declared_files",
        "_facts",
        "_last_polled_at",
        "_disabled",
    )

    def __init__(
        self,
        *,
        store: Store,
        job_id: str,
        provider_pgid: int,
        provider_identity: str,
        root: Path,
        declared_files: tuple[str, ...],
        initial_facts: dict[str, tuple[Any, ...]],
    ) -> None:
        if (
            not root.is_absolute()
            or len(declared_files) > 256
            or len(set(declared_files)) != len(declared_files)
            or any(
                Path(relative).is_absolute()
                or ".." in Path(relative).parts
                or not Path(relative).name
                for relative in declared_files
            )
        ):
            raise WorkerError("objective progress declaration is invalid")
        if set(initial_facts) != set(declared_files):
            raise WorkerError("objective progress baseline is incomplete")
        self.store = store
        self.job_id = job_id
        self.provider_pgid = provider_pgid
        self.provider_identity = provider_identity
        self.root = root
        self.declared_files = declared_files
        self._facts = {
            relative: self._content_fact(initial_facts[relative])
            for relative in declared_files
        }
        self._last_polled_at = 0.0
        self._disabled = False

    @staticmethod
    def _content_fact(fact: tuple[Any, ...]) -> tuple[Any, ...]:
        if fact == ("missing",):
            return fact
        if not _safe_declared_output_fact(fact):
            raise WorkerError(
                "objective progress path is not a single-link regular file or missing"
            )
        # Device, inode and timestamps are identity/boundary evidence, not work.
        # Only a file's exact byte length and digest count as objective progress.
        return ("regular", fact[6], fact[9])

    @classmethod
    def from_declared_paths(
        cls,
        *,
        store: Store,
        job_id: str,
        provider_pgid: int,
        provider_identity: str,
        root: Path,
        declared_files: tuple[str, ...],
    ) -> "_ObjectiveArtifactTracker":
        try:
            canonical_root = root.resolve(strict=True)
        except OSError as error:
            raise WorkerError("objective progress root is unavailable") from error
        deadline = time.monotonic() + OUTPUT_BOUNDARY_SNAPSHOT_SECONDS
        initial_facts = {
            relative: _output_absolute_path_fact(
                canonical_root / relative,
                deadline=deadline,
            )
            for relative in declared_files
        }
        return cls(
            store=store,
            job_id=job_id,
            provider_pgid=provider_pgid,
            provider_identity=provider_identity,
            root=canonical_root,
            declared_files=declared_files,
            initial_facts=initial_facts,
        )

    @classmethod
    def from_output_boundary(
        cls,
        *,
        store: Store,
        job_id: str,
        provider_pgid: int,
        provider_identity: str,
        snapshot: _OutputBoundarySnapshot,
    ) -> "_ObjectiveArtifactTracker":
        return cls(
            store=store,
            job_id=job_id,
            provider_pgid=provider_pgid,
            provider_identity=provider_identity,
            root=snapshot.root,
            declared_files=snapshot.declared_files,
            initial_facts=snapshot.declared_facts,
        )

    def poll(self, *, force: bool = False) -> bool:
        if self._disabled or not self.declared_files:
            return False
        now = time.monotonic()
        if not force and now - self._last_polled_at < OBJECTIVE_PROGRESS_POLL_SECONDS:
            return False
        self._last_polled_at = now
        try:
            deadline = time.monotonic() + OUTPUT_BOUNDARY_SNAPSHOT_SECONDS
            current = {
                relative: self._content_fact(
                    _output_absolute_path_fact(
                        self.root / relative,
                        deadline=deadline,
                    )
                )
                for relative in self.declared_files
            }
        except WorkerError:
            # A file can be observed between write/truncate/rename operations.
            # That is not positive evidence; final output-boundary enforcement
            # remains authoritative for unsafe types or unstable paths.
            return False
        changed = sum(
            current[relative] != self._facts[relative]
            for relative in self.declared_files
        )
        if changed == 0:
            return False
        evidence_sha256 = hashlib.sha256(
            canonical_json(
                [[relative, list(current[relative])] for relative in self.declared_files]
            ).encode("utf-8")
        ).hexdigest()
        try:
            self.store.record_objective_progress(
                self.job_id,
                provider_pgid=self.provider_pgid,
                provider_identity=self.provider_identity,
                source="declared_artifact",
                evidence_sha256=evidence_sha256,
                changed_file_count=changed,
            )
        except InvalidTransition:
            # Expected after cancellation or natural terminal arbitration.  Do
            # not let a diagnostic observer derail the controlling state path.
            self._disabled = True
            return False
        except StateError as error:
            if str(error) != "objective progress authority is unavailable":
                raise
            # A provider can become a zombie between its final filesystem write
            # and the worker's last observation.  Absence is not authority to
            # publish progress, but it is also not a provider-output failure.
            self._disabled = True
            return False
        self._facts = current
        return True

    def expand_declared_files(
        self,
        *,
        prior_files: tuple[str, ...],
        files: tuple[str, ...],
        new_facts: dict[str, tuple[Any, ...]],
    ) -> None:
        """Adopt one already-admitted strict expansion before its prompt is sent."""

        added = tuple(relative for relative in files if relative not in prior_files)
        if (
            self._disabled
            or self.declared_files != prior_files
            or not set(prior_files).issubset(files)
            or prior_files == files
            or len(files) != len(set(files))
            or set(new_facts) != set(added)
        ):
            raise WorkerError("objective progress expansion does not match its prior set")
        content_facts = {
            relative: self._content_fact(new_facts[relative]) for relative in added
        }
        self.declared_files = files
        self._facts.update(content_facts)


def _apply_admission_expansion_to_output_boundary(
    *,
    snapshot: _OutputBoundarySnapshot,
    objective_tracker: _ObjectiveArtifactTracker,
    expansion: Any,
    reservation: dict[str, Any],
) -> None:
    """Bind a durable bridge expansion to the live worker's writable boundary."""

    required = {
        "lease_id",
        "prior_lease_revision",
        "lease_revision",
        "prior_files",
        "files",
        "admission_sha256",
        "operation_sha256",
    }
    if not isinstance(expansion, dict) or set(expansion) != required:
        raise WorkerError("steering expansion receipt is malformed")
    prior_files = tuple(expansion["prior_files"])
    files = tuple(expansion["files"])
    if (
        snapshot.declared_files != prior_files
        or len(files) > 256
        or len(files) != len(set(files))
        or not set(prior_files).issubset(files)
        or prior_files == files
        or expansion["lease_revision"] != expansion["prior_lease_revision"] + 1
        or reservation.get("lease_id") != expansion["lease_id"]
        or reservation.get("kind") != "bridge"
        or reservation.get("status") != "ACTIVE"
        or reservation.get("revision") != expansion["lease_revision"]
        or tuple(reservation.get("files", [])) != files
        or reservation.get("admission_sha256") != expansion["admission_sha256"]
    ):
        raise WorkerError("steering expansion does not match its authoritative lease")
    for name in ("admission_sha256", "operation_sha256"):
        _validate_sha256(expansion[name], f"steering expansion {name}")
    added = tuple(relative for relative in files if relative not in prior_files)
    if any(
        not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
        or not Path(relative).name
        for relative in files
    ):
        raise WorkerError("steering expansion contains an unsafe output path")
    deadline = time.monotonic() + OUTPUT_BOUNDARY_SNAPSHOT_SECONDS
    new_facts = {
        relative: _output_absolute_path_fact(
            snapshot.root / relative,
            deadline=deadline,
        )
        for relative in added
    }
    changed_before_expansion = [
        relative
        for relative, fact in new_facts.items()
        if fact != snapshot.workspace_facts.get(relative, ("missing",))
    ]
    if changed_before_expansion:
        raise WorkerError("expanded output path changed before expansion admission")
    if any(not _safe_declared_output_fact(fact) for fact in new_facts.values()):
        raise WorkerError(
            "expanded output path is not a single-link regular file or missing"
        )
    objective_tracker.expand_declared_files(
        prior_files=prior_files,
        files=files,
        new_facts=new_facts,
    )
    snapshot.declared_files = files
    snapshot.declared_facts.update(new_facts)


def _enforce_output_boundary(
    store: Store,
    job_id: str,
    snapshot: _OutputBoundarySnapshot,
) -> None:
    """Fail closed after provider exit while preserving every provider-written byte."""

    violations: dict[str, list[str]] = {
        "undeclared_paths": [],
        "declared_path_types": [],
        "control_roles": [],
        "git_facts": [],
        "admission_binding": [],
        "snapshot_errors": [],
    }
    try:
        current_job = store.get_job(job_id)
        current_attempt = store.get_attempt(current_job.current_attempt_id)
        _validate_attempt_admission_binding(
            store,
            current_job,
            current_attempt,
            store.get_attempt_admission(current_attempt.id),
        )
    except (StateError, WorkerError):
        violations["admission_binding"].append("changed")
    try:
        marker_relative = str(snapshot.git_marker.relative_to(snapshot.root))
    except ValueError:
        marker_exclusions: frozenset[str] = frozenset()
    else:
        marker_exclusions = frozenset({marker_relative})
    try:
        workspace_after = _stable_output_tree(
            snapshot.root,
            excluded=marker_exclusions,
            opaque_directories=snapshot.opaque_cache_roots,
        )
        changed = _changed_fact_names(snapshot.workspace_facts, workspace_after)
        declared = set(snapshot.declared_files)
        cache_roots = _final_generated_cache_roots(workspace_after)
        marker_roots = _final_empty_claude_marker_roots(workspace_after)
        violations["undeclared_paths"] = [
            path
            for path in changed
            if path not in declared
            and not _allowed_declared_parent_change(
                path,
                snapshot.workspace_facts.get(path, ("missing",)),
                workspace_after.get(path, ("missing",)),
                declared,
            )
            and not _allowed_generated_cache_change(
                path,
                snapshot.workspace_facts.get(path, ("missing",)),
                workspace_after.get(path, ("missing",)),
                cache_roots,
            )
            and not _allowed_empty_claude_marker_change(
                path,
                snapshot.workspace_facts.get(path, ("missing",)),
                workspace_after.get(path, ("missing",)),
                marker_roots,
            )
        ]
        violations["declared_path_types"] = [
            path
            for path in snapshot.declared_files
            if not _safe_declared_output_fact(
                workspace_after.get(path, ("missing",))
            )
        ]
    except WorkerError:
        violations["snapshot_errors"].append("workspace")

    for role, pin in sorted(snapshot.control_pins.items()):
        try:
            label = f"admission control {role}"
            _validate_exact_output_path_spelling(Path(pin["path"]), label)
            _read_admission_pin(pin, label)
            after_fact = _output_absolute_path_fact(
                Path(pin["path"]),
                deadline=time.monotonic() + OUTPUT_BOUNDARY_SNAPSHOT_SECONDS,
            )
            if after_fact != snapshot.control_facts[role]:
                raise WorkerError("admission control fact changed")
        except (OSError, WorkerError):
            violations["control_roles"].append(role)

    try:
        semantic_after = {
            "head": _output_git_fact(
                snapshot.git_executable,
                snapshot.root,
                "rev-parse",
                "HEAD",
            ),
            "refs": _output_git_fact(
                snapshot.git_executable,
                snapshot.root,
                "for-each-ref",
                "--format=%(refname)%00%(objectname)%00%(symref)",
            ),
        }
        for name in ("head", "refs"):
            if semantic_after[name] != snapshot.git_semantic_facts[name]:
                violations["git_facts"].append(name)
        metadata_after = _snapshot_git_metadata(
            snapshot.git_marker,
            snapshot.git_dir,
            snapshot.common_dir,
        )
        metadata_changed = _changed_fact_names(
            snapshot.git_metadata_facts,
            metadata_after,
        )
        if metadata_changed:
            violations["git_facts"].append("metadata")
        for raw_name in metadata_changed:
            relative = raw_name.split(":", 1)[-1]
            components = relative.split("/")
            basename = components[-1]
            if basename == "HEAD" and "head" not in violations["git_facts"]:
                violations["git_facts"].append("head")
            if (
                relative.startswith("refs/")
                or "/refs/" in relative
                or basename in {"packed-refs", "reftable"}
            ) and "refs" not in violations["git_facts"]:
                violations["git_facts"].append("refs")
            if (
                basename == "index"
                or basename.startswith("index.")
                or basename.startswith("sharedindex.")
            ) and "index" not in violations["git_facts"]:
                violations["git_facts"].append("index")
        index_after = _output_absolute_path_fact(
            snapshot.git_dir / "index",
            deadline=time.monotonic() + OUTPUT_BOUNDARY_SNAPSHOT_SECONDS,
        )
        if (
            index_after != snapshot.git_index_fact
            and "index" not in violations["git_facts"]
        ):
            violations["git_facts"].append("index")
    except WorkerError:
        violations["snapshot_errors"].append("git")
        if "metadata" not in violations["git_facts"]:
            violations["git_facts"].append("metadata")

    violations["git_facts"] = sorted(set(violations["git_facts"]))
    if not any(violations.values()):
        return
    violation_counts = {
        name: len(items)
        for name, items in violations.items()
    }
    bounded_violations = {
        name: items[:MAX_OUTPUT_BOUNDARY_EVENT_ITEMS]
        for name, items in violations.items()
    }
    _append_event(
        store,
        job_id,
        "OUTPUT_BOUNDARY_VIOLATION",
        {
            "version": 1,
            "classification": "provider_output_boundary",
            "reason": "provider_changed_state_outside_declared_reservation",
            "declared_file_count": len(snapshot.declared_files),
            "declared_files": list(snapshot.declared_files)[
                :MAX_OUTPUT_BOUNDARY_EVENT_ITEMS
            ],
            "violation_counts": violation_counts,
            "violations": bounded_violations,
        },
    )
    _append_event(
        store,
        job_id,
        "PROVIDER_FAILED",
        {
            "status": "FAILED",
            "classification": "provider_output_boundary",
            "reason": "provider_changed_state_outside_declared_reservation",
        },
    )
    raise ProviderOutputBoundaryError(
        "provider changed state outside its declared file reservation"
    )


def _validate_admission_evidence(
    value: Any,
    *,
    allow_evolved_review_controls: bool = False,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != ADMISSION_EVIDENCE_FIELDS:
        raise WorkerError("admission evidence fields do not match the worker contract")
    evidence = copy.deepcopy(value)
    evidence_hash = _validate_sha256(evidence.pop("sha256"), "admission evidence hash")
    if _admission_hash(evidence) != evidence_hash:
        raise WorkerError("admission evidence hash does not match its contents")
    evidence["sha256"] = evidence_hash
    if (
        evidence["version"] != 1
        or evidence["lane"] not in {"review", "feature", "recovery"}
        or not isinstance(evidence["product_lane"], bool)
        or evidence["executor"] != "claude"
        or evidence["driver"] != "codex"
        or evidence["integration_authority"] != "codex"
        or evidence["product_lane"] != (evidence["lane"] in {"feature", "recovery"})
    ):
        raise WorkerError("admission authority or lane evidence is invalid")
    if not isinstance(evidence["declaration"], dict):
        raise WorkerError("admission declaration evidence is malformed")

    scheduler = evidence["scheduler_snapshot"]
    if not isinstance(scheduler, dict) or "sha256" not in scheduler:
        raise WorkerError("admission scheduler snapshot is malformed")
    scheduler_without_hash = copy.deepcopy(scheduler)
    scheduler_hash = _validate_sha256(
        scheduler_without_hash.pop("sha256"),
        "admission scheduler snapshot hash",
    )
    if _admission_hash(scheduler_without_hash) != scheduler_hash:
        raise WorkerError("admission scheduler snapshot hash does not match")

    argv = evidence["argv"]
    if (
        not isinstance(argv, list)
        or not argv
        or len(argv) > 1024
        or any(
            not isinstance(argument, str)
            or not argument
            or "\x00" in argument
            or len(argument.encode("utf-8")) > 524_288
            for argument in argv
        )
    ):
        raise WorkerError("admission guard argv is malformed")
    argv_hash = hashlib.sha256(
        json.dumps(argv, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if argv_hash != _validate_sha256(evidence["argv_sha256"], "guard argv hash"):
        raise WorkerError("admission guard argv hash does not match")
    for name in ("stdout", "stderr"):
        text = evidence[name]
        if not isinstance(text, str) or len(text.encode("utf-8")) > ADMISSION_OUTPUT_LIMIT_BYTES:
            raise WorkerError(f"admission guard {name} is malformed")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != _validate_sha256(
            evidence[f"{name}_sha256"], f"guard {name} hash"
        ):
            raise WorkerError(f"admission guard {name} hash does not match")
    result = evidence["result"]
    if (
        not isinstance(result, dict)
        or result.get("decision") != "ADMIT"
        or result.get("version") != 1
        or result.get("argv_sha256") != argv_hash
        or _admission_hash(result)
        != _validate_sha256(evidence["result_sha256"], "guard result hash")
    ):
        raise WorkerError("admission guard result is not bound to this launch")
    if evidence["stderr"] or evidence["stdout"] != (
        "ADMIT:" + json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n"
    ):
        raise WorkerError("admission guard output is not the exact result receipt")
    _read_admission_pin(evidence["guard"], "admission guard", guard=True)
    if result.get("guard_sha256") != evidence["guard"]["sha256"]:
        raise WorkerError("admission guard result has the wrong executable hash")
    runtime = evidence["runtime"]
    if not isinstance(runtime, dict) or set(runtime) != {"python", "git"}:
        raise WorkerError("admission guard runtime pins are malformed")
    for name, pin in runtime.items():
        _read_admission_pin(
            pin,
            f"admission guard {name} runtime",
            guard=True,
            trusted_root=True,
        )

    controls = evidence["controls"]
    review_controls = evidence["review_controls"]
    profile = evidence["declaration"].get("execution_profile")
    base_controls = {
        "plan", "review_log", "execution_contract", "project_overlay"
    }
    assured_controls = {
        "engine_contract",
        "routing_policy",
        "liveness_fixture",
        "watchdog_fixture",
        "watchdog_tests_fixture",
        "freeze_manifest",
    }
    expected_controls = (
        (base_controls | assured_controls)
        if evidence["product_lane"] and profile == "assured"
        else base_controls if evidence["product_lane"] and profile == "sprint" else set()
    )
    if (
        evidence["product_lane"]
        and isinstance(evidence["declaration"].get("oracle"), dict)
        and evidence["declaration"]["oracle"].get("exact") is True
    ):
        expected_controls.update({"capability_receipt", "capability_target"})
    if not isinstance(controls, dict) or set(controls) != expected_controls:
        raise WorkerError("admission control evidence is malformed")
    control_bytes: dict[str, bytes] = {}
    for name, pin in controls.items():
        if not isinstance(name, str) or not name:
            raise WorkerError("admission control name is malformed")
        control_bytes[name] = _read_admission_pin(
            pin,
            f"admission control {name}",
        )
    if controls and (
        len({pin["path"] for pin in controls.values()}) != len(controls)
        or len({(pin["device"], pin["inode"]) for pin in controls.values()})
        != len(controls)
    ):
        raise WorkerError("admission control roles are aliased")
    allowed_review_shapes = (
        (set(),)
        if evidence["product_lane"]
        else (
            set(),
            {"plan", "review_log"},
            {"plan", "review_log", "domain_docs"},
        )
    )
    if (
        not isinstance(review_controls, dict)
        or set(review_controls) not in allowed_review_shapes
    ):
        raise WorkerError("review control evidence is malformed")
    domain_docs = review_controls.get("domain_docs", [])
    if not isinstance(domain_docs, list) or len(domain_docs) > 64:
        raise WorkerError("review domain-doc evidence is malformed")
    review_items = _review_control_items(review_controls) if review_controls else ()
    for name, pin in review_items:
        if allow_evolved_review_controls:
            _validate_admission_pin_shape(pin, f"review control {name}")
        else:
            _read_admission_pin(pin, f"review control {name}")
    if review_controls and (
        len({pin["path"] for _name, pin in review_items}) != len(review_items)
        or len({(pin["device"], pin["inode"]) for _name, pin in review_items})
        != len(review_items)
    ):
        raise WorkerError("review control roles are aliased")
    declared_review_controls = evidence["declaration"].get("review_controls")
    if review_controls:
        if (
            not isinstance(declared_review_controls, dict)
            or set(declared_review_controls) != set(review_controls)
        ):
            raise WorkerError("review control declaration is not bound to evidence")
        for name in ("plan", "review_log"):
            declared = declared_review_controls.get(name)
            if (
                not isinstance(declared, dict)
                or set(declared) != {"path", "sha256"}
                or declared["path"] != review_controls[name]["path"]
                or declared["sha256"] != review_controls[name]["sha256"]
            ):
                raise WorkerError(
                    f"review control declaration {name} is not bound to evidence"
                )
        declared_domain_docs = declared_review_controls.get("domain_docs", [])
        repository = scheduler.get("repository")
        repository_root = (
            repository.get("top_level") if isinstance(repository, dict) else None
        )
        if (
            not isinstance(repository_root, str)
            or not Path(repository_root).is_absolute()
            or not isinstance(declared_domain_docs, list)
            or len(declared_domain_docs) != len(domain_docs)
        ):
            raise WorkerError("review domain-doc declaration is not bound to evidence")
        expected_domain_docs: list[dict[str, str]] = []
        try:
            for pin in domain_docs:
                expected_domain_docs.append(
                    {
                        "path": str(
                            Path(pin["path"]).relative_to(Path(repository_root))
                        ),
                        "sha256": pin["sha256"],
                    }
                )
        except (KeyError, TypeError, ValueError) as error:
            raise WorkerError(
                "review domain-doc evidence is outside the repository"
            ) from error
        if (
            declared_domain_docs != expected_domain_docs
            or [item["path"] for item in expected_domain_docs]
            != sorted(item["path"] for item in expected_domain_docs)
            or len({item["path"] for item in expected_domain_docs})
            != len(expected_domain_docs)
        ):
            raise WorkerError("review domain-doc declaration is not canonical")
    elif declared_review_controls is not None:
        raise WorkerError("empty review evidence cannot carry a control declaration")
    manifest_entries = evidence["manifest_entries"]
    if profile == "assured" and evidence["product_lane"]:
        if (
            not isinstance(manifest_entries, list)
            or not manifest_entries
            or len(manifest_entries) > 48
        ):
            raise WorkerError("assured freeze manifest entries are malformed")
        try:
            manifest_lines = control_bytes["freeze_manifest"].decode(
                "utf-8"
            ).splitlines()
        except UnicodeError as error:
            raise WorkerError("assured freeze manifest is not UTF-8") from error
        declared_manifest_entries: list[tuple[str, str]] = []
        for line in manifest_lines:
            match = re.fullmatch(r"([0-9A-Fa-f]{64}) [ *](.+)", line)
            if match is None:
                raise WorkerError("assured freeze manifest is malformed")
            declared_manifest_entries.append(
                (match.group(2), match.group(1).lower())
            )
        pinned_manifest_entries: list[tuple[str, str]] = []
        manifest_paths: set[str] = set()
        manifest_identities: set[tuple[int, int]] = set()
        for index, pin in enumerate(manifest_entries):
            _read_admission_pin(
                pin,
                f"assured freeze manifest entry {index}",
            )
            path = pin["path"]
            identity = (pin["device"], pin["inode"])
            if path in manifest_paths or identity in manifest_identities:
                raise WorkerError("assured freeze manifest entries are aliased")
            if path == controls["freeze_manifest"]["path"] or identity == (
                controls["freeze_manifest"]["device"],
                controls["freeze_manifest"]["inode"],
            ):
                raise WorkerError("assured freeze manifest lists itself")
            manifest_paths.add(path)
            manifest_identities.add(identity)
            pinned_manifest_entries.append((path, pin["sha256"]))
        if pinned_manifest_entries != declared_manifest_entries:
            raise WorkerError(
                "assured freeze manifest entries do not match their pins"
            )
        required_manifest_paths = {
            pin["path"]
            for name, pin in controls.items()
            if name
            not in {"freeze_manifest", "capability_receipt", "capability_target"}
        }
        required_manifest_paths.add(evidence["guard"]["path"])
        if not required_manifest_paths.issubset(manifest_paths):
            raise WorkerError(
                "assured freeze manifest omits a governing executable or control"
            )
    elif manifest_entries != []:
        raise WorkerError("non-assured admission carries freeze manifest entries")
    if "capability_receipt" in controls:
        try:
            receipt_text = control_bytes["capability_receipt"].decode("utf-8")
            receipt: dict[str, str] = {}
            for line in receipt_text.splitlines():
                key, item = line.split("=", 1)
                if key in receipt:
                    raise ValueError
                receipt[key] = item
        except (UnicodeError, ValueError) as error:
            raise WorkerError("oracle capability receipt is malformed") from error
        required_receipt_fields = {
            "version",
            "receipt_id",
            "contract_sha256",
            "mechanism",
            "disposable_target",
            "exit",
            "oracle_sha256",
            "observed_sha256",
        }
        if set(receipt) != required_receipt_fields:
            raise WorkerError("oracle capability receipt fields differ")

        def bounded_receipt_text(name: str, maximum: int) -> str:
            item = receipt[name]
            if (
                not item
                or item != item.strip()
                or len(item.encode("utf-8")) > maximum
                or not item.isascii()
                or any(
                    ord(character) < 0x20 or ord(character) == 0x7F
                    for character in item
                )
            ):
                raise WorkerError(f"oracle capability {name} is malformed")
            return item

        if receipt["version"] != "1" or receipt["exit"] != "0":
            raise WorkerError("oracle capability receipt version or exit is invalid")
        bounded_receipt_text("receipt_id", 256)
        bounded_receipt_text("mechanism", 512)
        target_path = bounded_receipt_text("disposable_target", 4096)
        for name in ("contract_sha256", "oracle_sha256", "observed_sha256"):
            _validate_sha256(receipt[name], f"oracle capability {name}")
        if (
            receipt["contract_sha256"]
            != hashlib.sha256(control_bytes["execution_contract"]).hexdigest()
            or receipt["oracle_sha256"] != receipt["observed_sha256"]
            or receipt["observed_sha256"]
            != hashlib.sha256(control_bytes["capability_target"]).hexdigest()
            or target_path != controls["capability_target"]["path"]
        ):
            raise WorkerError("oracle capability evidence is not hash-bound")
        repository = scheduler.get("repository")
        repository_root = (
            repository.get("top_level") if isinstance(repository, dict) else None
        )
        if (
            not isinstance(repository_root, str)
            or not Path(repository_root).is_absolute()
            or Path(target_path) == Path(repository_root)
            or Path(target_path).is_relative_to(Path(repository_root))
        ):
            raise WorkerError("oracle capability target is not disposable")
    predecessor = evidence["predecessor"]
    if predecessor is not None:
        if not isinstance(predecessor, dict) or not set(predecessor) <= {
            "report",
            "consumption",
            "review",
        }:
            raise WorkerError("admission predecessor evidence is malformed")
        for name, pin in predecessor.items():
            _read_admission_pin(pin, f"admission predecessor {name}")
    receipts = evidence["predecessor_receipts"]
    if (predecessor is None) != (receipts is None):
        raise WorkerError("admission predecessor receipts are incomplete")
    if receipts is not None:
        if not isinstance(receipts, dict) or set(receipts) != {"report", "review"}:
            raise WorkerError("admission predecessor receipts are malformed")
        report_receipt = receipts["report"]
        if not isinstance(report_receipt, dict) or set(report_receipt) != {
            "version",
            "predecessor",
            "consumed_by",
            "report",
            "report_sha256",
            "receipt_id",
            "source_sha256",
            "sha256",
        }:
            raise WorkerError("admission report-consumption receipt is malformed")
        report_without_hash = dict(report_receipt)
        report_hash = report_without_hash.pop("sha256")
        if (
            report_receipt["version"] != 1
            or _provider_family(report_receipt["consumed_by"]) != "codex"
            or report_receipt["report"] != predecessor["report"]["path"]
            or report_receipt["report_sha256"] != predecessor["report"]["sha256"]
            or report_receipt["source_sha256"]
            != predecessor["consumption"]["sha256"]
            or _admission_hash(report_without_hash)
            != _validate_sha256(report_hash, "report-consumption receipt hash")
        ):
            raise WorkerError("admission report-consumption receipt is not bound")
        review_receipt = receipts["review"]
        if review_receipt is not None:
            if (
                not isinstance(review_receipt, dict)
                or set(review_receipt)
                != {
                    "version",
                    "predecessor",
                    "builder",
                    "reviewer",
                    "thread",
                    "verdict",
                    "receipt_id",
                    "artifact_sha256",
                }
                or review_receipt["artifact_sha256"]
                != predecessor["review"]["sha256"]
                or review_receipt["verdict"] != "APPROVED"
            ):
                raise WorkerError("admission predecessor review receipt is not bound")
    act2 = evidence["act2_review"]
    if evidence["product_lane"]:
        if (
            not isinstance(act2, dict)
            or set(act2)
            != {
                "version",
                "unit_id",
                "reviewer_family",
                "verdict",
                "receipt_id",
                "artifact_sha256",
            }
            or act2["version"] != 1
            or act2["verdict"] != "APPROVED"
            or _provider_family(act2["reviewer_family"]) != "claude"
            or act2["artifact_sha256"] != controls["review_log"]["sha256"]
        ):
            raise WorkerError("admission Act-2 review receipt is invalid")
    elif act2 is not None:
        raise WorkerError("review lanes cannot carry an Act-2 product receipt")

    reservation = evidence["reservation"]
    declared_files = evidence["declaration"].get("files")
    if (
        not isinstance(reservation, dict)
        or set(reservation) != {"lane", "weight", "writer", "files", "repo_id"}
        or reservation["lane"] != evidence["lane"]
        or reservation["weight"] not in {"LIGHT", "HEAVY"}
        or not isinstance(reservation["repo_id"], str)
        or not isinstance(reservation["files"], list)
        or any(not isinstance(path, str) or not path for path in reservation["files"])
        or not isinstance(declared_files, list)
        or reservation["files"] != declared_files
        or (
            reservation["writer"] is not None
            and not isinstance(reservation["writer"], str)
        )
    ):
        raise WorkerError("admission reservation evidence is malformed")
    resume = evidence["resume"]
    if resume is not None:
        if (
            not isinstance(resume, dict)
            or set(resume)
            != {
                "parent_attempt_id",
                "parent_revision",
                "attempt_id",
                "model_override",
                "original_admission_sha256",
                "parent_admission_sha256",
                "scheduler_sha256",
                "prompt_sha256",
                "context_request_sha256",
            }
            or any(
                not isinstance(resume[name], str) or not resume[name]
                for name in ("parent_attempt_id", "attempt_id")
            )
            or isinstance(resume["parent_revision"], bool)
            or not isinstance(resume["parent_revision"], int)
            or resume["parent_revision"] < 0
            or (
                resume["model_override"] is not None
                and (
                    not isinstance(resume["model_override"], str)
                    or not resume["model_override"]
                )
            )
        ):
            raise WorkerError("resume admission binding is malformed")
        _validate_sha256(
            resume["original_admission_sha256"],
            "original admission evidence hash",
        )
        _validate_sha256(
            resume["parent_admission_sha256"],
            "parent admission evidence hash",
        )
        _validate_sha256(resume["prompt_sha256"], "resume prompt hash")
        _validate_sha256(
            resume["context_request_sha256"],
            "resume context request hash",
        )
        if resume["scheduler_sha256"] != scheduler_hash:
            raise WorkerError("resume admission scheduler binding is invalid")
    return evidence


def _validate_attempt_admission_binding(
    store: Store,
    job: Any,
    attempt: Any,
    evidence: Any,
) -> dict[str, Any]:
    validated = _validate_admission_evidence(evidence)
    resume = validated["resume"]
    original_hash = job.payload.get("admission_evidence", {}).get("sha256")
    if attempt.parent_attempt_id is None:
        if resume is not None or validated["sha256"] != original_hash:
            raise WorkerError("initial admission is not bound to the job payload")
        return validated
    if resume is None:
        raise WorkerError("resume attempt has no bound resume admission")
    parent = store.get_attempt(attempt.parent_attempt_id)
    parent_admission = store.get_attempt_admission(parent.id)
    attempt_context = store.get_attempt_context(attempt.id)
    if (
        parent.job_id != job.id
        or resume["attempt_id"] != attempt.id
        or resume["parent_attempt_id"] != parent.id
        or resume["parent_revision"] != parent.revision
        # The child is created at parent.revision + 1, then every dispatch,
        # worker-binding, and state transition advances its revision together
        # with the job.  Bind the immutable creation floor and the live CAS
        # pair; requiring the creation revision exactly would reject every
        # legitimately dispatched resume before provider spawn.
        or attempt.revision <= parent.revision
        or attempt.revision != job.revision
        or resume["model_override"] != attempt.model_override
        or resume["parent_admission_sha256"] != parent_admission["sha256"]
        or resume["original_admission_sha256"] != original_hash
        or resume["prompt_sha256"] != attempt_context.get("prompt_sha256")
        or resume["context_request_sha256"] != attempt_context.get("request_hash")
    ):
        raise WorkerError("resume admission does not match durable attempt lineage")
    return validated


def _payload(
    job_payload: Any,
    *,
    allow_evolved_review_controls: bool = False,
) -> dict[str, Any]:
    if not isinstance(job_payload, dict):
        raise WorkerError("job payload must be an object")
    expected = {
        "prompt",
        "cwd",
        "role",
        "model",
        "effort",
        "budget",
        "baseline",
        "claude_executable",
        "admission_evidence",
        "request_hash",
    }
    if set(job_payload) != expected:
        raise WorkerError("job payload fields do not match the worker contract")
    prompt = _bounded_text(job_payload["prompt"], "prompt", maximum_bytes=524_288)
    cwd = _bounded_text(job_payload["cwd"], "cwd")
    role = _bounded_text(job_payload["role"], "role", maximum_bytes=32)
    model = _bounded_text(job_payload["model"], "model", maximum_bytes=256)
    effort = _bounded_text(job_payload["effort"], "effort", maximum_bytes=32)
    executable = _bounded_text(
        job_payload["claude_executable"],
        "Claude executable",
        maximum_bytes=4096,
    )
    admission_evidence = _validate_admission_evidence(
        job_payload["admission_evidence"],
        allow_evolved_review_controls=allow_evolved_review_controls,
    )
    request_hash = _validate_sha256(job_payload["request_hash"], "job request hash")
    if role not in {"review", "build", "recovery"}:
        raise WorkerError("job role is invalid")
    expected_lane = {
        "review": "review",
        "build": "feature",
        "recovery": "recovery",
    }[role]
    if admission_evidence["lane"] != expected_lane:
        raise WorkerError("job role does not match its admitted lane")
    if effort not in {"low", "medium", "high", "xhigh", "max"}:
        raise WorkerError("job effort is invalid")
    budget = job_payload["budget"]
    if not isinstance(budget, dict) or set(budget) != {
        "max_seconds",
        "max_tokens",
        "max_turns",
        "max_cost_usd",
    }:
        raise WorkerError("job budget does not match the worker contract")
    if any(
        isinstance(budget[name], bool) or not isinstance(budget[name], int)
        for name in ("max_seconds", "max_tokens", "max_turns")
    ):
        raise WorkerError("job budget values must be integers")
    if not 1 <= budget["max_seconds"] <= 86_400:
        raise WorkerError("job wall budget is outside its bound")
    if not 1 <= budget["max_tokens"] <= 10_000_000:
        raise WorkerError("job token budget is outside its bound")
    if not 1 <= budget["max_turns"] <= 1_000_000:
        raise WorkerError("job turn budget is outside its bound")
    try:
        max_cost_usd = normalize_budget_usd(budget["max_cost_usd"])
    except AdapterError as error:
        raise WorkerError("job USD budget is outside its bound") from error
    budget = dict(budget)
    budget["max_cost_usd"] = max_cost_usd
    baseline = job_payload["baseline"]
    if not isinstance(baseline, dict) or set(baseline) != {
        "head",
        "status_fingerprint",
        "tracked_content_fingerprint",
        *REPOSITORY_PATH_PIN_FIELDS,
    }:
        raise WorkerError("job baseline does not match the worker contract")
    for name, limit in (
        ("head", 64),
        ("status_fingerprint", 64),
        ("tracked_content_fingerprint", 64),
        ("top_level", 4096),
        ("common_dir", 4096),
        ("git_marker", 4096),
        ("git_dir", 4096),
    ):
        _bounded_text(baseline[name], f"baseline {name}", maximum_bytes=limit)
    for name in (
        "worktree_device",
        "worktree_inode",
        "worktree_owner",
        "git_marker_device",
        "git_marker_inode",
        "git_marker_owner",
        "git_dir_device",
        "git_dir_inode",
        "git_dir_owner",
        "common_dir_device",
        "common_dir_inode",
        "common_dir_owner",
    ):
        value = baseline[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise WorkerError(f"baseline {name} must be a non-negative integer")
    for name in (
        "worktree_type",
        "git_marker_type",
        "git_dir_type",
        "common_dir_type",
    ):
        if baseline[name] not in {"directory", "regular"}:
            raise WorkerError(f"baseline {name} is invalid")
    if (
        baseline["worktree_type"] != "directory"
        or baseline["git_dir_type"] != "directory"
        or baseline["common_dir_type"] != "directory"
    ):
        raise WorkerError("baseline repository directories have invalid types")

    def validate_hash(value: Any, label: str, *, optional: bool) -> None:
        if value is None and optional:
            return
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise WorkerError(f"baseline {label} is invalid")

    marker_hash = baseline["git_marker_content_hash"]
    validate_hash(
        marker_hash,
        "Git marker content hash",
        optional=baseline["git_marker_type"] == "directory",
    )
    if baseline["git_marker_type"] == "directory" and marker_hash is not None:
        raise WorkerError("baseline Git marker directory cannot have a content hash")

    for prefix in ("git_commondir_file", "git_worktree_backpointer"):
        values = {
            "path": baseline[prefix],
            "device": baseline[f"{prefix}_device"],
            "inode": baseline[f"{prefix}_inode"],
            "owner": baseline[f"{prefix}_owner"],
            "type": baseline[f"{prefix}_type"],
            "content_hash": baseline[f"{prefix}_content_hash"],
        }
        if all(value is None for value in values.values()):
            continue
        if any(value is None for value in values.values()):
            raise WorkerError(f"baseline {prefix} identity is incomplete")
        _bounded_text(values["path"], f"baseline {prefix}", maximum_bytes=4096)
        for label in ("device", "inode", "owner"):
            value = values[label]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise WorkerError(
                    f"baseline {prefix} {label} must be a non-negative integer"
                )
        if values["type"] != "regular":
            raise WorkerError(f"baseline {prefix} type is invalid")
        validate_hash(values["content_hash"], f"{prefix} content hash", optional=False)
    return {
        "prompt": prompt,
        "cwd": cwd,
        "role": role,
        "model": model,
        "effort": effort,
        "budget": dict(budget),
        "baseline": dict(baseline),
        "claude_executable": executable,
        "admission_evidence": admission_evidence,
        "request_hash": request_hash,
    }


def _payload_with_attempt_context(
    job_payload: Any,
    context_value: Any,
) -> dict[str, Any]:
    if not isinstance(job_payload, dict) or not isinstance(context_value, dict):
        raise WorkerError("attempt execution context is malformed")
    context = dict(context_value)
    context_hash = context.pop("sha256", None)
    if set(context) != {
        "version",
        "prompt",
        "prompt_sha256",
        "baseline",
        "baseline_sha256",
        "review_controls",
        "review_controls_sha256",
        "dirty_files",
        "dirty_files_sha256",
        "request_hash",
    }:
        raise WorkerError("attempt execution context fields differ")
    prompt = context["prompt"]
    baseline = context["baseline"]
    review_controls = context["review_controls"]
    dirty_files = context["dirty_files"]
    if (
        context["version"] != 1
        or not isinstance(prompt, str)
        or not prompt
        or not isinstance(baseline, dict)
        or not isinstance(review_controls, dict)
        or not isinstance(dirty_files, list)
        or hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        != context["prompt_sha256"]
        or _admission_hash(baseline) != context["baseline_sha256"]
        or _admission_hash(review_controls) != context["review_controls_sha256"]
        or _admission_hash(dirty_files) != context["dirty_files_sha256"]
        or _admission_hash(context) != context_hash
    ):
        raise WorkerError("attempt execution context hashes do not match")
    payload = copy.deepcopy(job_payload)
    payload["prompt"] = prompt
    payload["baseline"] = baseline
    return payload


def _validate_frozen_repository(
    payload: dict[str, Any],
) -> tuple[Any, RepositoryLaunchPin]:
    """Revalidate the immutable Git and worktree identity for provider launch."""
    baseline = payload["baseline"]
    identity = validate_launch(
        payload["cwd"],
        os.environ.get("FORGE_LAUNCH_WORKSPACE", payload["cwd"]),
        role=payload["role"],
        expected_head=baseline["head"],
        expected_status_fingerprint=baseline["status_fingerprint"],
        expected_tracked_content_fingerprint=(
            baseline["tracked_content_fingerprint"]
        ),
        prompt=payload["prompt"],
        allow_dirty_recovery=payload["role"] == "recovery",
        allow_dirty_review=payload["role"] == "review",
        allow_dirty_build=payload["role"] == "build",
    )
    if (
        str(identity.top_level) != baseline["top_level"]
        or str(identity.git_marker) != baseline["git_marker"]
        or str(identity.git_dir) != baseline["git_dir"]
        or str(identity.common_dir) != baseline["common_dir"]
        or identity.git_marker_content_hash
        != baseline["git_marker_content_hash"]
        or (
            None
            if identity.git_commondir_file is None
            else str(identity.git_commondir_file)
        )
        != baseline["git_commondir_file"]
        or identity.git_commondir_content_hash
        != baseline["git_commondir_file_content_hash"]
        or (
            None
            if identity.git_worktree_backpointer is None
            else str(identity.git_worktree_backpointer)
        )
        != baseline["git_worktree_backpointer"]
        or identity.git_worktree_backpointer_content_hash
        != baseline["git_worktree_backpointer_content_hash"]
        or identity.head != baseline["head"]
        or identity.status_fingerprint != baseline["status_fingerprint"]
        or identity.tracked_content_fingerprint
        != baseline["tracked_content_fingerprint"]
    ):
        raise WorkerError("repository identity changed after admission")
    path_specs = [
        (
            "worktree",
            identity.top_level,
            frozenset({"directory"}),
            None,
        ),
        (
            "git_marker",
            identity.git_marker,
            frozenset({"directory", "regular"}),
            identity.git_marker_content_hash,
        ),
        (
            "git_dir",
            identity.git_dir,
            frozenset({"directory"}),
            None,
        ),
        (
            "common_dir",
            identity.common_dir,
            frozenset({"directory"}),
            None,
        ),
    ]
    for prefix, path, content_hash, expected_name in (
        (
            "git_commondir_file",
            identity.git_commondir_file,
            identity.git_commondir_content_hash,
            "commondir",
        ),
        (
            "git_worktree_backpointer",
            identity.git_worktree_backpointer,
            identity.git_worktree_backpointer_content_hash,
            "gitdir",
        ),
    ):
        if path is None:
            continue
        if path != identity.git_dir / expected_name:
            raise WorkerError(f"admitted {prefix} path is inconsistent")
        path_specs.append(
            (prefix, path, frozenset({"regular"}), content_hash)
        )
    path_information: dict[str, os.stat_result] = {}
    for prefix, path, allowed_types, content_hash in path_specs:
        information = os.stat(path, follow_symlinks=False)
        if stat.S_ISDIR(information.st_mode):
            path_type = "directory"
        elif stat.S_ISREG(information.st_mode):
            path_type = "regular"
        else:
            raise WorkerError(f"admitted {prefix} has an unsupported path type")
        if (
            path_type not in allowed_types
            or information.st_uid != os.geteuid()
            or information.st_dev != baseline[f"{prefix}_device"]
            or information.st_ino != baseline[f"{prefix}_inode"]
            or information.st_uid != baseline[f"{prefix}_owner"]
            or path_type != baseline[f"{prefix}_type"]
        ):
            raise WorkerError(f"admitted {prefix} path identity changed before launch")
        if content_hash is not None:
            descriptor = None
            try:
                flags = os.O_RDONLY | os.O_NOFOLLOW
                flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
                descriptor = os.open(path, flags)
                held = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(held.st_mode)
                    or held.st_size > GIT_METADATA_LIMIT_BYTES
                    or (held.st_dev, held.st_ino, held.st_uid)
                    != (information.st_dev, information.st_ino, information.st_uid)
                ):
                    raise WorkerError(
                        f"admitted {prefix} changed while it was opened"
                    )
                data = bytearray()
                while True:
                    chunk = os.read(
                        descriptor,
                        min(65_536, GIT_METADATA_LIMIT_BYTES + 1 - len(data)),
                    )
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > GIT_METADATA_LIMIT_BYTES:
                        raise WorkerError(
                            f"admitted {prefix} exceeds its byte bound"
                        )
                current = os.stat(path, follow_symlinks=False)
                if (
                    (current.st_dev, current.st_ino, current.st_uid)
                    != (held.st_dev, held.st_ino, held.st_uid)
                    or hashlib.sha256(data).hexdigest() != content_hash
                ):
                    raise WorkerError(
                        f"admitted {prefix} content identity changed before launch"
                    )
            except OSError as error:
                raise WorkerError(
                    f"admitted {prefix} could not be opened safely"
                ) from error
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        path_information[prefix] = information
    expected_prefixes = {prefix for prefix, *_rest in path_specs}
    if set(path_information) != expected_prefixes:
        raise AssertionError("repository path identity was not fully inspected")

    def pin(prefix: str, path: Path, content_hash: str | None = None) -> PathPin:
        return PathPin(
            path=path,
            kind=baseline[f"{prefix}_type"],
            device=baseline[f"{prefix}_device"],
            inode=baseline[f"{prefix}_inode"],
            owner=baseline[f"{prefix}_owner"],
            content_hash=content_hash,
        )

    metadata_pins = tuple(
        pin(prefix, path, content_hash)
        for prefix, path, _allowed_types, content_hash in path_specs
        if prefix in {"git_commondir_file", "git_worktree_backpointer"}
    )
    return identity, RepositoryLaunchPin(
        worktree=pin("worktree", identity.top_level),
        git_marker=pin(
            "git_marker",
            identity.git_marker,
            identity.git_marker_content_hash,
        ),
        git_dir=pin("git_dir", identity.git_dir),
        common_dir=pin("common_dir", identity.common_dir),
        head=baseline["head"],
        status_fingerprint=baseline["status_fingerprint"],
        tracked_content_fingerprint=baseline["tracked_content_fingerprint"],
        metadata_files=metadata_pins,
    )


def _validate_private_file(information: os.stat_result, label: str) -> None:
    if not stat.S_ISREG(information.st_mode):
        raise WorkerError(f"{label} is not a regular file")
    if information.st_uid != os.geteuid():
        raise WorkerError(f"{label} is not owned by the current user")
    if stat.S_IMODE(information.st_mode) != PRIVATE_FILE_MODE:
        raise WorkerError(f"{label} is not private")
    if information.st_nlink != 1:
        raise WorkerError(f"{label} must have exactly one hard link")


@contextmanager
def _open_private_path(path: Path, *, writable: bool = False):
    """Open an existing file without following any parent or final symlink."""

    candidate = Path(path)
    if ".." in candidate.parts or not candidate.name:
        raise WorkerError("private artifact path is invalid")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if candidate.is_absolute():
        anchor = Path(candidate.anchor)
        parts = candidate.parts[1:]
    else:
        anchor = Path.cwd()
        parts = candidate.parts
    descriptors: list[int] = []
    file_descriptor: int | None = None
    try:
        descriptors.append(os.open(anchor, directory_flags))
        for component in parts[:-1]:
            descriptors.append(
                os.open(component, directory_flags, dir_fd=descriptors[-1])
            )
        flags = os.O_NOFOLLOW | (os.O_WRONLY | os.O_APPEND if writable else os.O_RDONLY)
        file_descriptor = os.open(parts[-1], flags, dir_fd=descriptors[-1])
        before = os.fstat(file_descriptor)
        _validate_private_file(before, "private artifact")
        yield file_descriptor
        after = os.fstat(file_descriptor)
        current = os.stat(
            parts[-1],
            dir_fd=descriptors[-1],
            follow_symlinks=False,
        )
        _validate_private_file(after, "private artifact")
        _validate_private_file(current, "private artifact")
        if (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino):
            raise WorkerError("private artifact path was substituted while open")
    except OSError as error:
        raise WorkerError("could not open private artifact without following links") from error
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _sha256_fd(descriptor: int, *, maximum_bytes: int) -> str:
    information = os.fstat(descriptor)
    _validate_private_file(information, "artifact")
    if information.st_size > maximum_bytes:
        raise WorkerError("artifact exceeds its hashing byte bound")
    original = os.lseek(descriptor, 0, os.SEEK_CUR)
    digest = hashlib.sha256()
    total = 0
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise WorkerError("artifact exceeds its hashing byte bound")
            digest.update(chunk)
    finally:
        os.lseek(descriptor, original, os.SEEK_SET)
    if total != information.st_size:
        raise WorkerError("artifact changed while it was hashed")
    return digest.hexdigest()


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise WorkerError("short artifact write")
        view = view[written:]


def _replace_fd_content(descriptor: int, data: bytes, *, maximum_bytes: int) -> None:
    if len(data) > maximum_bytes:
        raise WorkerError("artifact content exceeds its byte bound")
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.ftruncate(descriptor, 0)
    _write_all(descriptor, data)
    os.fsync(descriptor)


def _write_or_validate_private_settings(
    path: Path,
    settings: dict[str, Any],
    *,
    allowed_root: Path,
) -> Path:
    """Publish settings once, or verify the same retryable attempt's bytes."""

    try:
        return write_private_settings(path, settings, allowed_root=allowed_root)
    except SecurityError as error:
        if "settings target already exists" not in str(error):
            raise
    expected = (canonical_json(settings) + "\n").encode("utf-8")
    with _open_private_path(path) as descriptor:
        information = os.fstat(descriptor)
        if information.st_size != len(expected):
            raise WorkerError("existing retry settings do not match the admitted attempt")
        observed = bytearray()
        while len(observed) <= len(expected):
            chunk = os.read(descriptor, len(expected) + 1 - len(observed))
            if not chunk:
                break
            observed.extend(chunk)
        if bytes(observed) != expected:
            raise WorkerError("existing retry settings do not match the admitted attempt")
    return path


def _append_private_jsonl(path: Path, value: dict[str, Any]) -> None:
    try:
        encoded = canonical_json(value).encode("utf-8") + b"\n"
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise WorkerError("Claude event could not be written as bounded JSON") from error
    with _open_private_path(Path(path), writable=True) as descriptor:
        if os.fstat(descriptor).st_size + len(encoded) > MAX_EVENTS_ARTIFACT_BYTES:
            raise WorkerError("event artifact exceeds its byte bound")
        _write_all(descriptor, encoded)
        os.fsync(descriptor)


def _session_from_command(
    command: list[str],
    *,
    expected_resume_session_id: str | None,
) -> str:
    if "--continue" in command or "--last" in command:
        raise WorkerError("provider command contains an implicit resume option")
    session_count = command.count("--session-id")
    resume_count = command.count("--resume")
    if expected_resume_session_id is None:
        if session_count != 1 or resume_count != 0:
            raise WorkerError("new provider command lacks one explicit session id")
        option_name = "--session-id"
    else:
        if session_count != 0 or resume_count != 1:
            raise WorkerError("resume provider command is not pinned to one session")
        option_name = "--resume"
    try:
        option = command.index(option_name)
        session_id = command[option + 1]
    except (ValueError, IndexError) as error:
        raise WorkerError("provider command session option is incomplete") from error
    session_id = _bounded_text(session_id, "session id", maximum_bytes=256)
    if (
        expected_resume_session_id is not None
        and session_id != expected_resume_session_id
    ):
        raise WorkerError("provider resume session changed after admission")
    return session_id


def _transition(store: Store, job_id: str, new_status: str) -> int:
    job = store.get_job(job_id)
    return store.transition(job_id, expected_revision=job.revision, new_status=new_status)


def _append_event(store: Store, job_id: str, kind: str, payload: dict[str, Any]):
    return store.append_event(job_id, kind, payload)


def _bounded_redacted_bytes(data: bytes, *, maximum_bytes: int) -> bytes:
    safe = redact_text(data.decode("utf-8", errors="replace")).encode("utf-8")
    if len(safe) <= maximum_bytes:
        return safe
    return safe[:maximum_bytes].decode("utf-8", errors="ignore").encode("utf-8")


def _bounded_redacted_text(value: str, *, maximum_bytes: int) -> str:
    """Redact first, then enforce an exact UTF-8 byte bound."""

    safe = redact_text(value)
    encoded = safe.encode("utf-8")
    if len(encoded) > maximum_bytes:
        safe = encoded[:maximum_bytes].decode("utf-8", errors="ignore")
    return safe


class _RepeatedStderrTracker:
    """Incrementally persist only bounded hashes of repeated stderr lines."""

    __slots__ = (
        "store",
        "job_id",
        "provider_pgid",
        "provider_identity",
        "_carry",
        "_last_signature",
        "_last_byte_count",
        "_repeat_count",
        "_published_count",
        "_disabled",
    )

    def __init__(
        self,
        *,
        store: Store,
        job_id: str,
        provider_pgid: int,
        provider_identity: str,
    ) -> None:
        self.store = store
        self.job_id = job_id
        self.provider_pgid = provider_pgid
        self.provider_identity = provider_identity
        self._carry = bytearray()
        self._last_signature: str | None = None
        self._last_byte_count = 0
        self._repeat_count = 0
        self._published_count = 0
        self._disabled = False

    def _observe(self, raw_line: bytes) -> None:
        if self._disabled:
            return
        safe = _bounded_redacted_bytes(raw_line, maximum_bytes=4096).strip()
        if not safe:
            return
        signature = hashlib.sha256(safe).hexdigest()
        if signature == self._last_signature:
            self._repeat_count = min(self._repeat_count + 1, 64)
        else:
            self._last_signature = signature
            self._last_byte_count = len(safe)
            self._repeat_count = 1
            self._published_count = 0
        if (
            self._repeat_count >= 2
            and self._repeat_count > self._published_count
            and (
                self._repeat_count == 2
                or self._repeat_count == 64
                or self._repeat_count & (self._repeat_count - 1) == 0
            )
        ):
            try:
                self.store.record_stderr_signature(
                    self.job_id,
                    provider_pgid=self.provider_pgid,
                    provider_identity=self.provider_identity,
                    signature_sha256=signature,
                    repeat_count=self._repeat_count,
                    byte_count=self._last_byte_count,
                )
            except InvalidTransition:
                # Cancellation/natural completion owns the state transition;
                # repeated-stderr observation must not derail its cleanup path.
                self._disabled = True
                return
            self._published_count = self._repeat_count

    def feed(self, chunk: bytes) -> None:
        if not isinstance(chunk, bytes):
            raise WorkerError("provider stderr observation must be bytes")
        self._carry.extend(chunk)
        while True:
            newline = self._carry.find(b"\n")
            if newline < 0:
                break
            line = bytes(self._carry[:newline])
            del self._carry[: newline + 1]
            self._observe(line)
        if len(self._carry) > 4096:
            self._observe(bytes(self._carry[:4096]))
            self._carry.clear()


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _write_provider_frame(provider: Any, frame: bytes, *, deadline: float) -> float:
    """Write one bounded frame without allowing a full provider pipe to hang."""
    bytes_written = 0
    frame_sent_at: float | None = None
    try:
        if provider.stdin is None:
            raise WorkerError("provider did not expose a bounded input pipe")
        if not isinstance(frame, bytes) or not frame or not frame.endswith(b"\n"):
            raise WorkerError("provider input frame must be bounded newline JSON")
        if len(frame) > MAX_EVENT_BYTES:
            raise WorkerError("provider input frame exceeds its byte bound")
        descriptor = provider.stdin.fileno()
        os.set_blocking(descriptor, False)
        selector = selectors.DefaultSelector()
        selector.register(descriptor, selectors.EVENT_WRITE)
        remaining_frame = memoryview(frame)
        try:
            while remaining_frame:
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    raise WorkerError(
                        "provider input delivery exceeded the wall-time budget"
                    )
                ready = selector.select(min(0.1, remaining_time))
                if not ready:
                    continue
                try:
                    written = os.write(descriptor, remaining_frame)
                except BlockingIOError:
                    continue
                except BrokenPipeError as error:
                    raise WorkerError("provider closed before input delivery") from error
                if written <= 0:
                    raise WorkerError("provider input delivery made no progress")
                bytes_written += written
                remaining_frame = remaining_frame[written:]
                if not remaining_frame:
                    frame_sent_at = time.monotonic()
        finally:
            selector.close()
        if frame_sent_at is None:
            raise WorkerError("provider input frame lacks a completion timestamp")
        return frame_sent_at
    except _ProviderFrameWriteError:
        raise
    except BaseException as error:
        raise _ProviderFrameWriteError(
            str(error) or "provider input delivery failed",
            bytes_written=bytes_written,
            frame_sent_at=frame_sent_at,
        ) from error


def _close_provider_input(provider: Any) -> None:
    """Close structured input exactly once so a result-emitting CLI can exit."""

    stream = getattr(provider, "stdin", None)
    if stream is None or stream.closed:
        return
    try:
        stream.close()
    except (BrokenPipeError, OSError, ValueError) as error:
        raise WorkerError("provider input could not be closed after terminal result") from error


def _close_cancelled_provider_pipes(provider: Any) -> None:
    for stream in (provider.stdin, provider.stdout, provider.stderr):
        if stream is None or stream.closed:
            continue
        try:
            stream.close()
        except (BrokenPipeError, OSError, ValueError):
            # The cancellation path already proved group quiescence. Pipe close
            # errors cannot authorize a second signal or undo that proof.
            pass


def _deliver_queued_commands(
    provider: Any,
    *,
    store: Store,
    job_id: str,
    dispatch_token: str,
    worker_pid: int,
    deadline: float,
    worker_authority: Any | None = None,
    prepare_steer: Callable[[Any], None] | None = None,
) -> _CommandDelivery | None:
    """Deliver at most one command before polling provider output again."""
    command = store.claim_next_command(
        job_id,
        dispatch_token=dispatch_token,
        worker_pid=worker_pid,
    )
    if command is None:
        return None
    frame: bytes | None = None
    try:
        if command.kind == "STEER" and set(command.payload) in (
            {"prompt"},
            {"prompt", "admission_expansion"},
        ):
            if prepare_steer is not None:
                prepare_steer(command)
            frame = encode_user_message(command.payload["prompt"])
        elif command.kind == "CANCEL" and set(command.payload) in (
            {"request_id"},
            {"request_id", "manifest_pin"},
        ):
            frame = encode_stream_message(
                {
                    "type": "control_request",
                    "request_id": command.payload["request_id"],
                    "request": {"subtype": "interrupt"},
                }
            )
        else:
            raise WorkerError("claimed command does not match its delivery contract")
        frame_sent_at = _write_provider_frame(provider, frame, deadline=deadline)
    except BaseException as error:
        complete_cancel_frame = (
            command.kind == "CANCEL"
            and frame is not None
            and isinstance(error, _ProviderFrameWriteError)
            and error.bytes_written == len(frame)
            and error.frame_sent_at is not None
        )
        if command.kind == "CANCEL" and not complete_cancel_frame:
            bytes_written = getattr(error, "bytes_written", 0)
            rolled_back = store.abort_cancellation_delivery(
                job_id,
                command.id,
                reason=(
                    "partial_frame_channel_poisoned"
                    if bytes_written > 0
                    else "frame_not_written"
                ),
                _worker_authority=worker_authority,
            )
            if not rolled_back:
                raise WorkerError(
                    "cancellation frame failure lost its rollback fence"
                ) from error
        elif command.kind != "CANCEL":
            store.mark_command_delivery_failed(
                job_id,
                command.id,
                dispatch_token=dispatch_token,
                worker_pid=worker_pid,
            )
        if not complete_cancel_frame:
            raise
        frame_sent_at = error.frame_sent_at
    assert frame_sent_at is not None
    if command.kind == "CANCEL":
        receipt_pending = False
        try:
            store.confirm_cancellation_delivery(
                job_id,
                command.id,
                _worker_authority=worker_authority,
                _busy_timeout_ms=0,
            )
        except (OSError, sqlite3.Error, StateError):
            # The full newline-delimited interrupt frame may already be in
            # Claude's pipe. Never write it again; retain the Popen and retry
            # only its durable receipt from the supervision loop.
            receipt_pending = True
        return _CommandDelivery(command, frame_sent_at, receipt_pending)
    store.mark_command_delivered(
        job_id,
        command.id,
        dispatch_token=dispatch_token,
        worker_pid=worker_pid,
    )
    return _CommandDelivery(command, frame_sent_at, False)


def _parse_interrupt_acknowledgement(
    event: ClaudeEvent,
    *,
    expected_request_id: str,
) -> bool:
    """Validate the exact successful Claude control response for one interrupt."""

    if event.type != "control_response":
        return False
    data = event.data
    if set(data) != {"type", "response"} or not isinstance(data["response"], dict):
        return False
    response = data["response"]
    if set(response) != {"subtype", "request_id", "response"}:
        return False
    return not (
        response.get("subtype") != "success"
        or response.get("request_id") != expected_request_id
        or response.get("response") != {}
    )


def _control_response_request_id(event: ClaudeEvent) -> str | None:
    if event.type != "control_response" or not isinstance(event.data, dict):
        return None
    response = event.data.get("response")
    if not isinstance(response, dict):
        return None
    request_id = response.get("request_id")
    return request_id if isinstance(request_id, str) else None


def _fence_generic_provider_cleanup(
    store: Store,
    job_id: str,
    *,
    dispatch_token: str,
    worker_pid: int,
) -> bool:
    """Acquire cleanup authority without racing accepted cancellation.

    Closing a RUNNING inbox and cancellation admission serialize through the
    same SQLite write transaction. Whichever wins becomes authoritative:
    cancellation vetoes generic process effects, while a closed inbox makes a
    later cancellation a natural-completion no-op. PREFLIGHT cannot admit a
    cancellation, so its launch-failure cleanup remains authorized.
    """

    try:
        status = store.get_job(job_id).status
    except (StateError, OSError):
        return False
    if status == "PREFLIGHT":
        return True
    if status != "RUNNING":
        return False
    try:
        store.close_command_inbox(
            job_id,
            dispatch_token=dispatch_token,
            worker_pid=worker_pid,
        )
    except (InvalidTransition, RevisionConflict, StateError, OSError):
        return False
    return True


def _collect_provider(
    provider: Any,
    *,
    store: Store,
    job_id: str,
    session_id: str,
    dispatch_token: str,
    worker_pid: int,
    events_descriptor: int,
    stderr_descriptor: int,
    deadline: float,
    policy_tracker: ProviderPolicyTracker | None = None,
    worker_authority: Any | None = None,
    include_cancellation: bool = False,
    cancellation_containment: object | None = None,
    stderr_tracker: _RepeatedStderrTracker | None = None,
    objective_tracker: _ObjectiveArtifactTracker | None = None,
    output_boundary_snapshot: _OutputBoundarySnapshot | None = None,
) -> (
    tuple[ClaudeEvent | None, str | None, list[str]]
    | tuple[ClaudeEvent | None, str | None, list[str], dict[str, Any] | None]
):
    """Drain provider stdout/stderr concurrently under cumulative hard bounds."""

    if provider.stdout is None or provider.stderr is None:
        raise WorkerError("provider did not expose bounded output pipes")
    selector = selectors.DefaultSelector()
    stdout_descriptor = provider.stdout.fileno()
    stderr_source_descriptor = provider.stderr.fileno()
    os.set_blocking(stdout_descriptor, False)
    os.set_blocking(stderr_source_descriptor, False)
    selector.register(stdout_descriptor, selectors.EVENT_READ, "stdout")
    selector.register(stderr_source_descriptor, selectors.EVENT_READ, "stderr")
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    event_count = 0
    event_bytes = 0
    artifact_bytes = os.fstat(events_descriptor).st_size
    result_event: ClaudeEvent | None = None
    observed_model: str | None = None
    parent_tool_use_ids: list[str] = []
    command_deliveries = 0
    command_deferred_for_ready_output = False
    cancellation_command = None
    cancellation_receipt: dict[str, Any] | None = None
    cancellation_ack_deadline: float | None = None
    pending_delivery_receipt: _CommandDelivery | None = None
    expired_cancellation_request_ids: set[str] = set()
    pending_interrupt_result: ClaudeEvent | None = None
    pending_policy_decision: ProviderPolicyDecision | None = None

    def prepare_steer(command: Any) -> None:
        expansion = command.payload.get("admission_expansion")
        if expansion is None:
            return
        if output_boundary_snapshot is None or objective_tracker is None:
            raise WorkerError("steering expansion has no live output-boundary authority")
        reservation = store.get_bridge_expansion_delivery_binding(
            job_id,
            attempt_id=command.attempt_id,
            command_id=command.id,
            expansion=expansion,
        )
        if reservation.get("job_id") != job_id:
            raise WorkerError("steering expansion job binding changed")
        _apply_admission_expansion_to_output_boundary(
            snapshot=output_boundary_snapshot,
            objective_tracker=objective_tracker,
            expansion=expansion,
            reservation=reservation,
        )

    def stop_for_policy(decision: ProviderPolicyDecision) -> None:
        _append_event(
            store,
            job_id,
            "PROVIDER_PAUSED" if decision.status.startswith("PAUSED_") else "PROVIDER_FAILED",
            {
                "status": decision.status,
                "classification": decision.classification,
                "reason": decision.reason,
            },
        )
        raise ProviderPolicyStop(decision)

    def fail_unacknowledged_cancellation(reason: str) -> bool:
        nonlocal cancellation_command
        nonlocal cancellation_ack_deadline
        nonlocal pending_interrupt_result
        nonlocal pending_policy_decision
        nonlocal result_event
        if cancellation_command is None:
            return False
        request_id = cancellation_command.payload["request_id"]
        rollback = store.fail_cancellation_acknowledgement(
            job_id,
            request_id=request_id,
            reason=reason,
            close_inbox_for_terminal_result=(pending_interrupt_result is not None),
            _worker_authority=worker_authority,
        )
        if not rollback.rolled_back:
            return False
        expired_cancellation_request_ids.add(request_id)
        cancellation_command = None
        cancellation_ack_deadline = None
        if pending_interrupt_result is not None:
            if not rollback.inbox_closed:
                raise WorkerError(
                    "terminal-result cancellation rollback left its inbox open"
                )
            _close_provider_input(provider)
            if rollback.failed_commands:
                raise WorkerError(
                    "provider completed while accepted commands remained pending"
                )
            result_event = pending_interrupt_result
            pending_interrupt_result = None
        if pending_policy_decision is not None:
            decision = pending_policy_decision
            pending_policy_decision = None
            stop_for_policy(decision)
        return True

    def deliver_admitted_cancellation() -> bool:
        nonlocal cancellation_command
        nonlocal cancellation_ack_deadline
        nonlocal pending_delivery_receipt
        nonlocal command_deliveries
        if cancellation_command is not None:
            return True
        if store.get_job(job_id).status != "INTERRUPTING":
            return False
        delivery = _deliver_queued_commands(
            provider,
            store=store,
            job_id=job_id,
            dispatch_token=dispatch_token,
            worker_pid=worker_pid,
            deadline=deadline,
            worker_authority=worker_authority,
            prepare_steer=prepare_steer,
        )
        command_deliveries += int(delivery is not None)
        if command_deliveries > MAX_COMMAND_DELIVERIES:
            raise WorkerError("command delivery count exceeds its bound")
        if delivery is None or delivery.command.kind != "CANCEL":
            raise WorkerError(
                "interrupting attempt has no cancellation available for delivery"
            )
        cancellation_command = delivery.command
        cancellation_ack_deadline = (
            delivery.frame_sent_at + CANCELLATION_ACK_TIMEOUT_SECONDS
        )
        if delivery.receipt_pending:
            pending_delivery_receipt = delivery
        return True

    def reconcile_pending_delivery(*, blocking: bool = False) -> bool:
        nonlocal pending_delivery_receipt
        if pending_delivery_receipt is None:
            return True
        command = pending_delivery_receipt.command
        try:
            store.confirm_cancellation_delivery(
                job_id,
                command.id,
                _worker_authority=worker_authority,
                _busy_timeout_ms=(10_000 if blocking else 0),
            )
        except (OSError, sqlite3.Error, StateError):
            return False
        pending_delivery_receipt = None
        return True

    try:
        while (
            selector.get_map()
            or b"\n" in stdout_buffer
            or (pending_delivery_receipt is not None and bool(stdout_buffer))
        ):
            if objective_tracker is not None:
                objective_tracker.poll()
            reconcile_pending_delivery()
            if cancellation_command is None and deliver_admitted_cancellation():
                # Cancellation was durably admitted before this output batch.
                # Task 9 owns arbitration, so give Claude the interrupt before
                # interpreting already-ready provider policy/result frames.
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if pending_delivery_receipt is not None and not reconcile_pending_delivery(
                    blocking=True
                ):
                    raise WorkerError(
                        "job wall deadline could not reconcile the written cancellation frame"
                    )
                if cancellation_command is not None and cancellation_receipt is None:
                    if not fail_unacknowledged_cancellation("deadline_exceeded"):
                        raise WorkerError(
                            "job wall deadline lost its cancellation rollback race"
                        )
                    raise WorkerError(
                        "job wall deadline expired during cancellation arbitration"
                    )
                if policy_tracker is not None:
                    stop_for_policy(
                        ProviderPolicyDecision(
                            status="PAUSED_POLICY",
                            classification="wall_limit",
                            reason="wall_time_limit_exceeded",
                        )
                    )
                raise WorkerError("provider exceeded the job wall-time budget")
            ready = selector.select(0)
            if cancellation_command is not None and cancellation_receipt is None:
                if cancellation_ack_deadline is None:
                    raise WorkerError("delivered cancellation has no acknowledgement deadline")
                acknowledgement_remaining = (
                    cancellation_ack_deadline - time.monotonic()
                )
                if acknowledgement_remaining <= 0:
                    if (
                        pending_delivery_receipt is not None
                        and not reconcile_pending_delivery(blocking=True)
                    ):
                        raise WorkerError(
                            "interrupt deadline could not reconcile its written frame"
                        )
                    if fail_unacknowledged_cancellation("deadline_exceeded"):
                        continue
                    raise WorkerError(
                        "interrupt acknowledgement deadline lost its rollback race"
                    )
                remaining = min(remaining, max(acknowledgement_remaining, 0))
            if (
                not ready
                and result_event is None
                and pending_delivery_receipt is None
            ):
                delivery = _deliver_queued_commands(
                    provider,
                    store=store,
                    job_id=job_id,
                    dispatch_token=dispatch_token,
                    worker_pid=worker_pid,
                    deadline=deadline,
                    worker_authority=worker_authority,
                    prepare_steer=prepare_steer,
                )
                command_deliveries += int(delivery is not None)
                if command_deliveries > MAX_COMMAND_DELIVERIES:
                    raise WorkerError("command delivery count exceeds its bound")
                command_deferred_for_ready_output = False
                if delivery is not None:
                    delivered_command = delivery.command
                    if delivered_command.kind == "CANCEL":
                        if cancellation_command is not None:
                            raise WorkerError("cancellation command was delivered more than once")
                        cancellation_command = delivered_command
                        cancellation_ack_deadline = (
                            delivery.frame_sent_at
                            + CANCELLATION_ACK_TIMEOUT_SECONDS
                        )
                        if delivery.receipt_pending:
                            pending_delivery_receipt = delivery
                    continue
            if not ready:
                ready = selector.select(min(0.1, remaining))
            ready_items = list(ready)
            if pending_delivery_receipt is None and b"\n" in stdout_buffer:
                # Receipt reconciliation can complete after stdout was fully
                # drained. Parse that buffered ACK/result without requiring a
                # new fd-readiness edge that may never arrive.
                ready_items.insert(0, (None, 0))
            for key, _mask in ready_items:
                if key is None:
                    chunk = None
                else:
                    try:
                        chunk = os.read(key.fd, 65_536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fd)
                        continue
                if key is not None and key.data == "stderr":
                    assert chunk is not None
                    if stderr_tracker is not None:
                        stderr_tracker.feed(chunk)
                    available = MAX_STDERR_BYTES - len(stderr_buffer)
                    if available > 0:
                        stderr_buffer.extend(chunk[:available])
                    if len(chunk) > available:
                        stderr_buffer[:] = (
                            b"[REDACTED: provider stderr exceeded byte bound]\n"
                        )
                        raise WorkerError("provider stderr exceeded its byte bound")
                    continue

                if chunk is not None:
                    event_bytes += len(chunk)
                    if event_bytes > MAX_PROVIDER_EVENT_BYTES:
                        raise WorkerError("provider event stream exceeded its byte bound")
                    stdout_buffer.extend(chunk)
                    if b"\n" not in stdout_buffer and len(stdout_buffer) > MAX_EVENT_BYTES:
                        raise WorkerError("provider event frame exceeded its byte bound")
                if pending_delivery_receipt is not None:
                    # Keep draining the live Popen so its pipes cannot fill,
                    # but do not consume/reorder an ACK ahead of the durable
                    # COMMAND_DELIVERED receipt. The buffered frame is parsed
                    # immediately after receipt reconciliation.
                    continue
                while True:
                    newline = stdout_buffer.find(b"\n")
                    if newline < 0:
                        break
                    frame = bytes(stdout_buffer[: newline + 1])
                    del stdout_buffer[: newline + 1]
                    event_count += 1
                    if event_count > MAX_PROVIDER_EVENTS:
                        raise WorkerError("provider event count exceeded its bound")
                    event = parse_event(frame)
                    if event.session_id is not None and event.session_id != session_id:
                        raise WorkerError("provider event session does not match the admitted session")
                    if result_event is not None:
                        raise WorkerError("provider emitted output after its terminal result")
                    event_data = event.redacted_summary()
                    encoded = canonical_json(event_data).encode("utf-8") + b"\n"
                    artifact_bytes += len(encoded)
                    if artifact_bytes > MAX_EVENTS_ARTIFACT_BYTES:
                        raise WorkerError("provider event artifact exceeded its byte bound")
                    _write_all(events_descriptor, encoded)

                    safe_model = (
                        None if event.model is None else redact_text(event.model)
                    )
                    safe_parent = (
                        None
                        if event.parent_tool_use_id is None
                        else redact_text(event.parent_tool_use_id)
                    )
                    provider_event_payload = {
                        "type": event.type,
                        "subtype": event.subtype,
                        "model": safe_model,
                        "parent_tool_use_id": safe_parent,
                        "data": event_data,
                    }
                    expected_interrupt_ack = (
                        event.type == "control_response"
                        and cancellation_command is not None
                        and _parse_interrupt_acknowledgement(
                            event,
                            expected_request_id=(
                                cancellation_command.payload["request_id"]
                            ),
                        )
                    )
                    if not expected_interrupt_ack:
                        _append_event(
                            store,
                            job_id,
                            "CLAUDE_EVENT",
                            provider_event_payload,
                        )
                    if event.type == "result":
                        if event.usage is None or event.total_cost_usd is None:
                            raise ClaudeProtocolError(
                                "result accounting evidence is unavailable"
                            )
                        _append_event(
                            store,
                            job_id,
                            "PROVIDER_ACCOUNTING",
                            {
                                "usage": redact_sensitive(event.usage),
                                "total_cost_usd": str(event.total_cost_usd),
                                "usage_provenance": "provider_authoritative",
                                "cost_provenance": "provider_authoritative",
                            },
                        )
                    policy_decision = (
                        None
                        if policy_tracker is None
                        else policy_tracker.observe(event)
                    )
                    if (
                        policy_decision is not None
                        and policy_decision.classification == "cancellation"
                        and cancellation_command is not None
                    ):
                        # Once Forge has delivered its interrupt, Task 9 owns
                        # acknowledgement, rollback, and quiescence arbitration.
                        # The provider's expected cancellation result remains
                        # buffered as terminal evidence; it must not bypass that
                        # durable state machine as a generic provider failure.
                        policy_decision = None
                    if policy_tracker is None:
                        if event.model is not None:
                            observed_model = event.model
                        model_receipt = None
                    else:
                        model_receipt = policy_tracker.model_receipt()
                        observed_models = model_receipt["models_observed"]
                        observed_model = (
                            observed_models[0] if observed_models else None
                        )
                    if (
                        safe_model is not None
                        or safe_parent is not None
                        or model_receipt is not None
                    ):
                        store.record_provider_identity(
                            job_id,
                            observed_model=(
                                safe_model if model_receipt is None else observed_model
                            ),
                            model_cli_accepted=(
                                None
                                if model_receipt is None
                                else model_receipt["model_cli_accepted"]
                            ),
                            models_observed=(
                                None
                                if model_receipt is None
                                else model_receipt["models_observed"]
                            ),
                            verification_level=(
                                None
                                if model_receipt is None
                                else model_receipt["verification_level"]
                            ),
                            parent_tool_use_id=safe_parent,
                        )
                    if policy_decision is not None:
                        if cancellation_command is not None:
                            if pending_policy_decision is None:
                                pending_policy_decision = policy_decision
                            policy_decision = None
                        elif not _fence_generic_provider_cleanup(
                            store,
                            job_id,
                            dispatch_token=dispatch_token,
                            worker_pid=worker_pid,
                        ):
                            # Cancellation admission won the same durable inbox
                            # fence. Buffer the provider outcome until ACK or
                            # rollback resolves Task 9 authority.
                            if pending_policy_decision is None:
                                pending_policy_decision = policy_decision
                            deliver_admitted_cancellation()
                            policy_decision = None
                    if policy_decision is not None:
                        _append_event(
                            store,
                            job_id,
                            (
                                "PROVIDER_PAUSED"
                                if policy_decision.status.startswith("PAUSED_")
                                else "PROVIDER_FAILED"
                            ),
                            {
                                "status": policy_decision.status,
                                "classification": policy_decision.classification,
                                "reason": policy_decision.reason,
                            },
                        )
                        raise ProviderPolicyStop(policy_decision)
                    if safe_parent is not None and safe_parent not in parent_tool_use_ids:
                        parent_tool_use_ids.append(safe_parent)
                    if event.type == "control_response":
                        if cancellation_command is None:
                            if (
                                _control_response_request_id(event)
                                in expired_cancellation_request_ids
                            ):
                                continue
                            raise WorkerError("Claude emitted an unsolicited control response")
                        request_id = cancellation_command.payload["request_id"]
                        if cancellation_receipt is not None:
                            raise WorkerError(
                                "Claude emitted a duplicate interrupt acknowledgement"
                            )
                        if expected_interrupt_ack:
                            effective_ack_deadline = (
                                min(cancellation_ack_deadline, deadline)
                                if cancellation_ack_deadline is not None
                                else None
                            )
                            if (
                                effective_ack_deadline is None
                                or time.monotonic() >= effective_ack_deadline
                            ):
                                if fail_unacknowledged_cancellation(
                                    "deadline_exceeded"
                                ):
                                    continue
                                raise WorkerError(
                                    "late interrupt acknowledgement lost its rollback race"
                                )
                            acknowledged_at = _utc_timestamp()
                            acknowledgement_rolled_back = False
                            while True:
                                try:
                                    store.acknowledge_cancellation_control_event(
                                        job_id,
                                        request_id=request_id,
                                        control_event=provider_event_payload,
                                        acknowledged_at=acknowledged_at,
                                        acknowledgement_deadline=(
                                            effective_ack_deadline
                                        ),
                                        _worker_authority=worker_authority,
                                        _busy_timeout_ms=0,
                                    )
                                except CancellationAcknowledgementExpired:
                                    if fail_unacknowledged_cancellation(
                                        "deadline_exceeded"
                                    ):
                                        acknowledgement_rolled_back = True
                                        break
                                    raise WorkerError(
                                        "late interrupt acknowledgement lost its rollback race"
                                    )
                                except (OSError, sqlite3.Error, StateError):
                                    if time.monotonic() >= effective_ack_deadline:
                                        if fail_unacknowledged_cancellation(
                                            "deadline_exceeded"
                                        ):
                                            acknowledgement_rolled_back = True
                                            break
                                        raise WorkerError(
                                            "interrupt acknowledgement persistence lost its rollback race"
                                        )
                                    time.sleep(0.01)
                                    continue
                                break
                            if acknowledgement_rolled_back:
                                continue
                            provider_pgid = provider.pid
                            store.validate_provider_cancellation(
                                job_id,
                                request_id=request_id,
                                provider_pgid=provider_pgid,
                                _worker_authority=worker_authority,
                            )

                            def record_stage(stage: str, recorded_at: str) -> None:
                                store.record_cancellation_stage(
                                    job_id,
                                    request_id=request_id,
                                    provider_pgid=provider_pgid,
                                    stage=stage,
                                    recorded_at=recorded_at,
                                    _worker_authority=worker_authority,
                                )

                            cancellation_arguments = {
                                "expected_process_group_id": provider_pgid,
                                "containment": cancellation_containment,
                                "admission_manifest_pin": (
                                    cancellation_command.payload.get("manifest_pin")
                                ),
                                "stage_callback": record_stage,
                            }
                            try:
                                evidence = cancel_process_group(
                                    provider,
                                    **cancellation_arguments,
                                )
                            except ProviderLeaderExitedBeforeSignal:
                                evidence = prove_provider_exit_after_cancellation_ack(
                                    provider,
                                    **cancellation_arguments,
                                )
                            if evidence.stage_failures:
                                failures = [
                                    {
                                        "stage": failure.stage,
                                        "error_type": failure.error_type,
                                        "message": _bounded_redacted_text(
                                            failure.message,
                                            maximum_bytes=512,
                                        )
                                        or failure.error_type,
                                    }
                                    for failure in evidence.stage_failures
                                ]
                                store.record_cancellation_cleanup_failure(
                                    job_id,
                                    request_id=request_id,
                                    provider_pgid=provider_pgid,
                                    failures=failures,
                                    _worker_authority=worker_authority,
                                )
                                store.close_cancellation_inbox(
                                    job_id,
                                    _worker_authority=worker_authority,
                                    require_quiescence=False,
                                )
                                raise WorkerError(
                                    "cancellation cleanup completed but one or more durable stage receipts failed"
                                )
                            store.close_cancellation_inbox(
                                job_id,
                                _worker_authority=worker_authority,
                            )
                            cancellation_receipt = {
                                "request_id": request_id,
                                "provider_pgid": evidence.provider_pgid,
                                "control_acknowledged_at": acknowledged_at,
                                "term_sent_at": evidence.term_sent_at,
                                "kill_sent_at": evidence.kill_sent_at,
                                "quiescent_at": evidence.quiescent_at,
                                "non_zombie_descendants": (
                                    evidence.non_zombie_descendants
                                ),
                            }
                            if evidence.provider_exited_at is not None:
                                cancellation_receipt.update(
                                    {
                                        "provider_exited_at": (
                                            evidence.provider_exited_at
                                        ),
                                        "termination_mode": (
                                            evidence.termination_mode
                                        ),
                                    }
                                )
                    if event.type == "result":
                        current_status = store.get_job(job_id).status
                        if current_status == "RUNNING":
                            try:
                                failed_commands = store.close_command_inbox(
                                    job_id,
                                    dispatch_token=dispatch_token,
                                    worker_pid=worker_pid,
                                )
                            except InvalidTransition:
                                if store.get_job(job_id).status != "INTERRUPTING":
                                    raise
                            else:
                                _close_provider_input(provider)
                                if failed_commands:
                                    raise WorkerError(
                                        "provider completed before accepted steering was delivered"
                                    )
                                result_event = event
                        elif current_status != "INTERRUPTING":
                            raise WorkerError(
                                "provider result arrived outside an active attempt"
                            )
                        else:
                            pending_interrupt_result = event
            # Give one newly-readable output batch priority before claiming a
            # command so an already-emitted terminal result settles first. Cap
            # the deferral at one batch: a continuous nonterminal stream, or an
            # incomplete frame whose producer waits for input, cannot starve an
            # accepted steering command.
            if result_event is None:
                if (
                    not command_deferred_for_ready_output
                    and selector.select(0)
                ):
                    command_deferred_for_ready_output = True
                    continue
                delivery = _deliver_queued_commands(
                    provider,
                    store=store,
                    job_id=job_id,
                    dispatch_token=dispatch_token,
                    worker_pid=worker_pid,
                    deadline=deadline,
                    worker_authority=worker_authority,
                    prepare_steer=prepare_steer,
                )
                command_deliveries += int(delivery is not None)
                if command_deliveries > MAX_COMMAND_DELIVERIES:
                    raise WorkerError("command delivery count exceeds its bound")
                command_deferred_for_ready_output = False
                if delivery is not None and delivery.command.kind == "CANCEL":
                    delivered_command = delivery.command
                    if cancellation_command is not None:
                        raise WorkerError("cancellation command was delivered more than once")
                    cancellation_command = delivered_command
                    cancellation_ack_deadline = (
                        delivery.frame_sent_at + CANCELLATION_ACK_TIMEOUT_SECONDS
                    )
                    if delivery.receipt_pending:
                        pending_delivery_receipt = delivery
            if (
                cancellation_command is not None
                and cancellation_receipt is None
                and cancellation_ack_deadline is not None
                and time.monotonic() >= cancellation_ack_deadline
            ):
                if (
                    pending_delivery_receipt is not None
                    and not reconcile_pending_delivery(blocking=True)
                ):
                    raise WorkerError(
                        "interrupt deadline could not reconcile its written frame"
                    )
                if fail_unacknowledged_cancellation("deadline_exceeded"):
                    continue
                raise WorkerError(
                    "interrupt acknowledgement deadline lost its rollback race"
                )
        if objective_tracker is not None:
            objective_tracker.poll(force=True)
        if stdout_buffer:
            if pending_delivery_receipt is not None:
                reconcile_pending_delivery(blocking=True)
            raise WorkerError("provider event stream ended without a newline")
        if pending_delivery_receipt is not None and not reconcile_pending_delivery(
            blocking=True
        ):
            raise WorkerError(
                "provider exit could not reconcile its written cancellation frame"
            )
        if cancellation_command is not None and cancellation_receipt is None:
            if not fail_unacknowledged_cancellation("provider_ended"):
                raise WorkerError(
                    "provider exit lost its cancellation acknowledgement rollback race"
                )
        if cancellation_receipt is None and not _fence_generic_provider_cleanup(
            store,
            job_id,
            dispatch_token=dispatch_token,
            worker_pid=worker_pid,
        ):
            raise WorkerError(
                "cancellation admission vetoed generic provider cleanup"
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if policy_tracker is not None:
                stop_for_policy(
                    ProviderPolicyDecision(
                        status="PAUSED_POLICY",
                        classification="wall_limit",
                        reason="wall_time_limit_exceeded",
                    )
                )
            raise WorkerError("provider exceeded the job wall-time budget")
        if cancellation_receipt is not None:
            return_code = provider.poll()
            if return_code is None:
                raise WorkerError("cancelled provider leader was not reaped")
        else:
            try:
                return_code = provider.wait(timeout=min(5.0, remaining))
            except subprocess.TimeoutExpired as error:
                raise WorkerError("provider did not exit within its bounded wait") from error
        if return_code != 0 and cancellation_receipt is None:
            if policy_tracker is not None:
                stop_for_policy(
                    ProviderPolicyDecision(
                        status="FAILED",
                        classification="transport",
                        reason="provider_process_exit_nonzero",
                    )
                )
            raise WorkerError(f"Claude provider exited with status {return_code}")
        base_result = (result_event, observed_model, parent_tool_use_ids)
        if include_cancellation:
            return (*base_result, cancellation_receipt)
        return base_result
    except BaseException:
        # A provider/parse/artifact failure after the interrupt frame is sent
        # must not strand INTERRUPTING. Reconcile only the durable delivery
        # receipt (never rewrite the frame), then atomically make any
        # unacknowledged cancellation inert so ordinary cleanup regains its
        # process-group fence.
        if cancellation_command is not None and cancellation_receipt is None:
            try:
                if pending_delivery_receipt is not None:
                    reconcile_pending_delivery(blocking=True)
                command_status = next(
                    (
                        command.status
                        for command in store.list_commands(job_id)
                        if command.id == cancellation_command.id
                    ),
                    None,
                )
                if command_status != "ACKNOWLEDGED":
                    fail_unacknowledged_cancellation("supervision_error")
            except (
                InvalidTransition,
                RevisionConflict,
                StateError,
                sqlite3.Error,
                OSError,
            ):
                # Durable ambiguity must never be converted into signal or a
                # terminal cancellation receipt.
                pass
        raise
    finally:
        selector.close()
        _replace_fd_content(
            stderr_descriptor,
            _bounded_redacted_bytes(
                bytes(stderr_buffer),
                maximum_bytes=MAX_STDERR_BYTES,
            ),
            maximum_bytes=MAX_STDERR_BYTES,
        )
        os.fsync(events_descriptor)


def _ensure_artifact(
    store: Store,
    job_id: str,
    attempt_id: str,
    name: str,
    *,
    lease: Any | None = None,
) -> None:
    try:
        with store.open_attempt_artifact(job_id, attempt_id, name):
            return
    except FileNotFoundError:
        store.create_attempt_artifact(
            job_id,
            attempt_id,
            name,
            _lease=lease,
        )


def _artifact_hash(
    store: Store,
    job_id: str,
    attempt_id: str,
    name: str,
    maximum_bytes: int,
) -> str:
    with store.open_attempt_artifact(
        job_id,
        attempt_id,
        name,
    ) as (descriptor, _path):
        return _sha256_fd(descriptor, maximum_bytes=maximum_bytes)


def _provider_version_evidence(
    store: Store, job_id: str, attempt_id: str
) -> str | None:
    for event in reversed(store.list_events(job_id)):
        if event.attempt_id != attempt_id or event.kind != "PROVIDER_PREFLIGHT":
            continue
        payload = event.payload
        if (
            not isinstance(payload, dict)
            or set(payload) != {"claude_code_version", "supported"}
            or not isinstance(payload.get("claude_code_version"), str)
            or not payload["claude_code_version"]
            or not isinstance(payload.get("supported"), bool)
        ):
            raise WorkerError("provider preflight evidence is malformed")
        return payload["claude_code_version"]
    return None


def _provider_accounting_evidence(
    store: Store, job_id: str, attempt_id: str
) -> dict[str, Any]:
    unavailable = {
        "usage": None,
        "total_cost_usd": None,
        "usage_provenance": "unavailable",
        "cost_provenance": "unavailable",
    }
    for event in reversed(store.list_events(job_id)):
        if event.attempt_id != attempt_id or event.kind != "PROVIDER_ACCOUNTING":
            continue
        payload = event.payload
        if (
            not isinstance(payload, dict)
            or set(payload) != set(unavailable)
            or not isinstance(payload.get("usage"), dict)
            or not isinstance(payload.get("total_cost_usd"), str)
            or not payload["total_cost_usd"]
            or payload.get("usage_provenance") != "provider_authoritative"
            or payload.get("cost_provenance") != "provider_authoritative"
        ):
            raise WorkerError("provider accounting evidence is malformed")
        return copy.deepcopy(payload)
    return unavailable


def _terminalize(
    store: Store,
    job_id: str,
    terminal_status: str,
    *,
    error: BaseException | None = None,
    success_fields: dict[str, Any] | None = None,
    report: str | None = None,
    worker_authority: Any | None = None,
    policy_decision: ProviderPolicyDecision | None = None,
) -> None:
    job = store.get_job(job_id)
    if job.status in TERMINAL_STATUSES:
        return
    attempt = store.get_attempt(job.current_attempt_id)
    with store.attempt_evidence_lease(
        job_id,
        attempt.id,
        exclusive=True,
    ) as lease:
        job = store.get_job(job_id)
        if job.status in TERMINAL_STATUSES:
            return
        if job.current_attempt_id != attempt.id:
            raise WorkerError("current attempt changed before terminal evidence freeze")
        detail = None
        if error is not None:
            detail = redact_text(str(error)) or error.__class__.__name__
            encoded_detail = detail.encode("utf-8", errors="replace")
            if len(encoded_detail) > MAX_ERROR_TEXT_BYTES:
                detail = encoded_detail[:MAX_ERROR_TEXT_BYTES].decode(
                    "utf-8", errors="replace"
                )
            try:
                _append_event(
                    store,
                    job_id,
                    "WORKER_ERROR",
                    {"error_type": error.__class__.__name__, "message": detail},
                )
            except (InvalidTransition, RevisionConflict, StateError):
                pass
            store.create_attempt_artifact(
                job_id,
                attempt.id,
                "report.txt",
                detail,
                _lease=lease,
            )
        elif report is not None:
            store.create_attempt_artifact(
                job_id,
                attempt.id,
                "report.txt",
                report,
                _lease=lease,
            )

        for name in ("events.jsonl", "stderr.log", "worker.log"):
            _ensure_artifact(
                store,
                job_id,
                attempt.id,
                name,
                lease=lease,
            )
        if terminal_status == "SUCCEEDED":
            _ensure_artifact(
                store,
                job_id,
                attempt.id,
                "report.txt",
                lease=lease,
            )

        attempt = store.get_attempt(attempt.id)
        model_requested = attempt.model_override or job.payload["model"]
        last_event = store.get_last_attempt_event(job_id, attempt.id)
        accounting = _provider_accounting_evidence(store, job_id, attempt.id)
        receipt: dict[str, Any] = {
            "job_id": job_id,
            "attempt_id": attempt.id,
            "repo_id": job.repo_id,
            "worktree": job.payload["baseline"]["top_level"],
            "baseline_head": job.payload["baseline"]["head"],
            "status_fingerprint": job.payload["baseline"]["status_fingerprint"],
            "requested_model": model_requested,
            "observed_model": attempt.observed_model,
            "model_requested": model_requested,
            "model_cli_accepted": attempt.model_cli_accepted,
            "models_observed": list(attempt.models_observed),
            "verification_level": attempt.verification_level,
            "provider_version_observed": _provider_version_evidence(
                store, job_id, attempt.id
            ),
            **accounting,
            "budget": job.payload["budget"],
            "requested_effort": job.payload["effort"],
            "session_id": attempt.session_id,
            "parent_tool_use_ids": list(attempt.parent_tool_use_ids),
            "event_hash": None if last_event is None else last_event.event_hash,
            "report_hash": _artifact_hash(
                store, job_id, attempt.id, "report.txt", MAX_REPORT_BYTES
            ),
            "events_hash": _artifact_hash(
                store,
                job_id,
                attempt.id,
                "events.jsonl",
                MAX_EVENTS_ARTIFACT_BYTES,
            ),
            "stderr_hash": _artifact_hash(
                store, job_id, attempt.id, "stderr.log", MAX_STDERR_BYTES
            ),
            "worker_log_hash": _artifact_hash(
                store, job_id, attempt.id, "worker.log", MAX_WORKER_LOG_BYTES
            ),
            "admission_evidence_sha256": store.get_attempt_admission(attempt.id)[
                "sha256"
            ],
            "terminal_status": terminal_status,
        }
        if error is not None:
            receipt["error"] = {
                "type": error.__class__.__name__,
                "message": detail,
            }
            if policy_decision is not None:
                receipt["error"].update(
                    {
                        "classification": policy_decision.classification,
                        "reason": policy_decision.reason,
                    }
                )
        if policy_decision is not None and terminal_status.startswith("PAUSED_"):
            receipt["pause"] = {
                "classification": policy_decision.classification,
                "reason": policy_decision.reason,
            }
        if success_fields:
            receipt.update(success_fields)
        safe_receipt = redact_sensitive(receipt)
        receipt_bytes = (canonical_json(safe_receipt) + "\n").encode("utf-8")
        if len(receipt_bytes) > MAX_RECEIPT_BYTES:
            raise WorkerError("terminal receipt exceeds its byte bound")
        store.create_attempt_artifact(
            job_id,
            attempt.id,
            "receipt.json",
            receipt_bytes,
            _lease=lease,
        )
        receipt_hash = _artifact_hash(
            store,
            job_id,
            attempt.id,
            "receipt.json",
            MAX_RECEIPT_BYTES,
        )
        current = store.get_job(job_id)
        store.finalize_with_receipt(
            job_id,
            expected_revision=current.revision,
            new_status=terminal_status,
            receipt_name="receipt.json",
            receipt_hash=receipt_hash,
            _lease=lease,
            _worker_authority=worker_authority,
        )


def _fail_job(store: Store, job_id: str, error: BaseException) -> None:
    try:
        job = store.get_job(job_id)
    except Exception:
        return
    if job.status in TERMINAL_STATUSES:
        return
    try:
        _terminalize(store, job_id, "FAILED", error=error)
    except (InvalidTransition, RevisionConflict, StateError, WorkerError, OSError):
        # A terminal status without a durable bounded receipt is forbidden.
        pass


def run_worker(
    job_id: str,
    *,
    dispatch_token: str | None = None,
    state_path: str | os.PathLike[str] | None = None,
    launch_gate_fd: int | None = None,
    dispatch_role: str | None = None,
    cancellation_containment: object | None = None,
) -> int:
    """Claim and execute one job. A duplicate worker exits before provider work."""

    database = state_path or os.environ.get("FORGE_STATE_PATH")
    if not database:
        raise WorkerError("Forge worker state path is not configured")
    token = _bounded_text(dispatch_token, "dispatch token", maximum_bytes=32)
    if len(token) != 32 or any(
        character not in "0123456789abcdef" for character in token
    ):
        raise WorkerError("dispatch token is invalid")
    cancellation_containment = _bind_test_cancellation_containment(
        cancellation_containment,
        job_id,
    )
    bound_role = _await_launch_binding_release(launch_gate_fd)
    if bound_role is not None and dispatch_role is not None:
        raise WorkerError("worker role has more than one authority")
    trusted_role = bound_role if bound_role is not None else dispatch_role
    if trusted_role not in {"review", "build", "recovery"}:
        raise WorkerError("worker launch role is unavailable")
    if trusted_role == "recovery":
        raise WorkerError(RECOVERY_DISABLED_MESSAGE)
    store = Store(database)
    pending_job = store.get_job(job_id)
    stored_role = (
        pending_job.payload.get("role")
        if isinstance(pending_job.payload, dict)
        else None
    )
    if stored_role != trusted_role:
        raise WorkerError("worker launch role does not match durable job")
    provider = None
    provider_popen_attempted = False
    worker_authority = None
    policy_tracker: ProviderPolicyTracker | None = None
    output_boundary_snapshot: _OutputBoundarySnapshot | None = None
    output_boundary_checked = False

    def before_provider_popen() -> None:
        nonlocal provider_popen_attempted
        if output_boundary_snapshot is None:
            raise WorkerError("provider control snapshot is unavailable")
        _revalidate_output_boundary_controls(output_boundary_snapshot)
        provider_popen_attempted = True

    def enforce_output_boundary() -> None:
        nonlocal output_boundary_checked
        if output_boundary_snapshot is None or output_boundary_checked:
            return
        output_boundary_checked = True
        _enforce_output_boundary(store, job_id, output_boundary_snapshot)
    try:
        attempt_started_at = time.monotonic()
        launch_bind_deadline = time.monotonic() + WORKER_LAUNCH_BIND_TIMEOUT_SECONDS
        while worker_authority is None:
            worker_authority = store.claim_worker_authority(job_id, token)
            if worker_authority is not None:
                break
            pending_job = store.get_job(job_id)
            pending_attempt = store.get_attempt(pending_job.current_attempt_id)
            if (
                pending_job.status != "CREATED"
                or pending_attempt.dispatch_state != "DISPATCHED"
                or pending_attempt.dispatch_token != token
                or (
                    pending_attempt.worker_pid is not None
                    and pending_attempt.worker_pid != os.getpid()
                )
            ):
                return 0
            if time.monotonic() >= launch_bind_deadline:
                raise WorkerError("worker launch identity was not durably bound")
            time.sleep(0.01)
        job = store.get_job(job_id)
        if job.status != "CREATED":
            return 0
        initial_attempt = store.get_attempt(job.current_attempt_id)
        if (
            initial_attempt.dispatch_state != "DISPATCHED"
            or initial_attempt.dispatch_token != token
            or initial_attempt.worker_pid != os.getpid()
        ):
            raise WorkerError("job has no durable dispatch claim")
        try:
            store.transition(job_id, expected_revision=job.revision, new_status="PREFLIGHT")
        except RevisionConflict:
            return 0

        attempt_payload = _payload_with_attempt_context(
            job.payload,
            store.get_attempt_context(initial_attempt.id),
        )
        attempt_payload["admission_evidence"] = _validate_attempt_admission_binding(
            store,
            job,
            initial_attempt,
            store.get_attempt_admission(initial_attempt.id),
        )
        payload = _payload(attempt_payload)
        deadline = attempt_started_at + payload["budget"]["max_seconds"]
        baseline = payload["baseline"]
        identity, expected_repository_identity = _validate_frozen_repository(payload)

        attempt = store.get_attempt(store.get_job(job_id).current_attempt_id)
        settings_path = (
            store.attempt_artifact_path(
                job_id,
                attempt.id,
                "claude-settings.json",
            )
        )
        recovery_write_paths: tuple[Path, ...] = ()
        if payload["role"] == "recovery":
            recovery_write_paths = tuple(
                identity.top_level / relative
                for relative in payload["admission_evidence"]["reservation"]["files"]
            )
        settings = build_claude_settings(
            payload["role"],
            identity.top_level,
            recovery_write_paths=recovery_write_paths,
        )
        _write_or_validate_private_settings(
            settings_path,
            settings,
            allowed_root=store.root_path,
        )
        preflight_remaining = deadline - time.monotonic()
        if preflight_remaining <= 0:
            decision = ProviderPolicyDecision(
                status="PAUSED_POLICY",
                classification="wall_limit",
                reason="wall_time_limit_exceeded",
            )
            _append_event(
                store,
                job_id,
                "PROVIDER_PAUSED",
                {
                    "status": decision.status,
                    "classification": decision.classification,
                    "reason": decision.reason,
                },
            )
            raise ProviderPolicyStop(decision)
        try:
            cli_receipt = preflight(
                payload["claude_executable"],
                environment=os.environ,
                timeout_seconds=min(5.0, preflight_remaining),
            )
        except UnsupportedClaudeCodeVersion as version_error:
            if time.monotonic() >= deadline:
                decision = ProviderPolicyDecision(
                    status="PAUSED_POLICY",
                    classification="wall_limit",
                    reason="wall_time_limit_exceeded",
                )
            else:
                _append_event(
                    store,
                    job_id,
                    "PROVIDER_PREFLIGHT",
                    {
                        "claude_code_version": version_error.observed_version,
                        "supported": False,
                    },
                )
                decision = ProviderPolicyDecision(
                    status="PAUSED_POLICY",
                    classification="provider_version",
                    reason="unsupported_claude_code_version_install_2.1.248",
                )
            _append_event(
                store,
                job_id,
                "PROVIDER_PAUSED",
                {
                    "status": decision.status,
                    "classification": decision.classification,
                    "reason": decision.reason,
                },
            )
            raise ProviderPolicyStop(decision)
        except PreflightError as preflight_error:
            if time.monotonic() >= deadline:
                decision = ProviderPolicyDecision(
                    status="PAUSED_POLICY",
                    classification="wall_limit",
                    reason="wall_time_limit_exceeded",
                )
            else:
                if preflight_error.observed_version is not None:
                    _append_event(
                        store,
                        job_id,
                        "PROVIDER_PREFLIGHT",
                        {
                            "claude_code_version": preflight_error.observed_version,
                            "supported": is_supported_claude_code_version(
                                preflight_error.observed_version
                            ),
                        },
                    )
                if (
                    preflight_error.provider_access_classification is not None
                    and preflight_error.provider_access_reason is not None
                ):
                    decision = ProviderPolicyDecision(
                        status="PAUSED_PROVIDER_ACCESS",
                        classification=(
                            preflight_error.provider_access_classification
                        ),
                        reason=preflight_error.provider_access_reason,
                    )
                else:
                    decision = ProviderPolicyDecision(
                        status="FAILED",
                        classification="preflight_or_internal",
                        reason="provider_preflight_failed",
                    )
            _append_event(
                store,
                job_id,
                (
                    "PROVIDER_PAUSED"
                    if decision.status.startswith("PAUSED_")
                    else "PROVIDER_FAILED"
                ),
                {
                    "status": decision.status,
                    "classification": decision.classification,
                    "reason": decision.reason,
                },
            )
            raise ProviderPolicyStop(decision)
        if time.monotonic() >= deadline:
            decision = ProviderPolicyDecision(
                status="PAUSED_POLICY",
                classification="wall_limit",
                reason="wall_time_limit_exceeded",
            )
            _append_event(
                store,
                job_id,
                "PROVIDER_PAUSED",
                {
                    "status": decision.status,
                    "classification": decision.classification,
                    "reason": decision.reason,
                },
            )
            raise ProviderPolicyStop(decision)
        safe_cli_version = redact_text(cli_receipt.version)
        _append_event(
            store,
            job_id,
            "PROVIDER_PREFLIGHT",
            {
                "claude_code_version": safe_cli_version,
                "supported": is_supported_claude_code_version(cli_receipt.version),
            },
        )
        if not is_supported_claude_code_version(cli_receipt.version):
            decision = ProviderPolicyDecision(
                status="PAUSED_POLICY",
                classification="provider_version",
                reason="unsupported_claude_code_version_install_2.1.248",
            )
            _append_event(
                store,
                job_id,
                "PROVIDER_PAUSED",
                {
                    "status": decision.status,
                    "classification": decision.classification,
                    "reason": decision.reason,
                },
            )
            raise ProviderPolicyStop(decision)
        recovery_tools = RECOVERY_TOOLS if payload["role"] == "recovery" else ()
        effective_model = attempt.model_override or payload["model"]
        policy_tracker = ProviderPolicyTracker(
            model_requested=effective_model,
            max_tokens=payload["budget"]["max_tokens"],
            max_turns=payload["budget"]["max_turns"],
            max_cost_usd=Decimal(payload["budget"]["max_cost_usd"]),
            effort_requested=payload["effort"],
        )
        spec = JobSpec(
            role=payload["role"],
            model=effective_model,
            effort=payload["effort"],
            max_budget_usd=payload["budget"]["max_cost_usd"],
            settings_path=settings_path,
            prompt=payload["prompt"],
            resume_session_id=attempt.resume_session_id,
            recovery_tools=recovery_tools,
        )
        command = build_command(spec, executable=payload["claude_executable"])
        session_id = _session_from_command(
            command,
            expected_resume_session_id=attempt.resume_session_id,
        )
        store.create_attempt_artifact(job_id, attempt.id, "events.jsonl")
        stderr_path = store.create_attempt_artifact(
            job_id,
            attempt.id,
            "stderr.log",
        )
        with store.open_attempt_artifact(
            job_id,
            attempt.id,
            "events.jsonl",
            writable=True,
        ) as (events_descriptor, _events_artifact), store.open_attempt_artifact(
            job_id,
            attempt.id,
            "stderr.log",
            writable=True,
        ) as (stderr_descriptor, _stderr_artifact):
            # Settings publication, local CLI preflight and durable receipts can
            # take time. Repeat the full Git/status check immediately before the
            # held-directory provider spawn so deterministic mid-preflight drift
            # never reaches Claude.
            current_job = store.get_job(job_id)
            current_attempt = store.get_attempt(current_job.current_attempt_id)
            current_admission = _validate_attempt_admission_binding(
                store,
                current_job,
                current_attempt,
                store.get_attempt_admission(current_attempt.id),
            )
            if current_admission["sha256"] != payload["admission_evidence"]["sha256"]:
                raise WorkerError("attempt admission changed before provider spawn")
            identity, expected_repository_identity = _validate_frozen_repository(payload)
            output_boundary_snapshot = _capture_output_boundary(payload, identity)
            provider_environment = build_child_environment(os.environ)
            test_manifest_path = _test_cancellation_manifest_path(
                cancellation_containment
            )
            if test_manifest_path is not None:
                provider_environment["FORGE_TEST_DESCENDANT_MANIFEST"] = str(
                    test_manifest_path
                )
            try:
                provider = spawn_provider(
                    command,
                    cwd=identity.top_level,
                    expected_cwd_identity=(
                        expected_repository_identity.worktree.device,
                        expected_repository_identity.worktree.inode,
                    ),
                    expected_repository_identity=expected_repository_identity,
                    environment=provider_environment,
                    stderr_path=stderr_path,
                    before_popen=before_provider_popen,
                )
            except ProviderLaunchError as launch_error:
                # A failed post-Popen repository check can still leave a live
                # process group. Preserve its handle so the outer cleanup path
                # can prove quiescence before any terminal receipt is published.
                provider = launch_error.process
                raise
            provider_identity = process_instance_identity(provider.pid)
            store.record_provider_identity(
                job_id,
                session_id=session_id,
                provider_pgid=provider.pid,
                provider_identity=provider_identity,
            )
            _append_event(
                store,
                job_id,
                "WORKER_PREFLIGHT",
                {
                    "baseline_head": identity.head,
                    "status_fingerprint": identity.status_fingerprint,
                    "claude": cli_receipt.redacted_summary(),
                },
            )
            _append_event(
                store,
                job_id,
                "PROVIDER_LAUNCH",
                {
                    "session_id": session_id,
                    "parent_tool_use_id": None,
                    "requested_model": effective_model,
                    "requested_effort": payload["effort"],
                    "budget": payload["budget"],
                },
            )
            _transition(store, job_id, "RUNNING")
            if provider.stdin is None:
                raise WorkerError("provider did not expose a bounded input pipe")
            try:
                _write_provider_frame(
                    provider,
                    encode_user_message(payload["prompt"]),
                    deadline=deadline,
                )
            except _ProviderFrameWriteError as frame_error:
                if time.monotonic() < deadline:
                    raise
                decision = ProviderPolicyDecision(
                    status="PAUSED_POLICY",
                    classification="wall_limit",
                    reason="wall_time_limit_exceeded",
                )
                _append_event(
                    store,
                    job_id,
                    "PROVIDER_PAUSED",
                    {
                        "status": decision.status,
                        "classification": decision.classification,
                        "reason": decision.reason,
                    },
                )
                raise ProviderPolicyStop(decision) from frame_error
            objective_tracker = _ObjectiveArtifactTracker.from_output_boundary(
                store=store,
                job_id=job_id,
                provider_pgid=provider.pid,
                provider_identity=provider_identity,
                snapshot=output_boundary_snapshot,
            )
            (
                result_event,
                observed_model,
                _parent_tool_use_ids,
                cancellation_receipt,
            ) = _collect_provider(
                provider,
                store=store,
                job_id=job_id,
                session_id=session_id,
                dispatch_token=token,
                worker_pid=os.getpid(),
                worker_authority=worker_authority,
                events_descriptor=events_descriptor,
                stderr_descriptor=stderr_descriptor,
                deadline=deadline,
                policy_tracker=policy_tracker,
                include_cancellation=True,
                cancellation_containment=cancellation_containment,
                stderr_tracker=_RepeatedStderrTracker(
                    store=store,
                    job_id=job_id,
                    provider_pgid=provider.pid,
                    provider_identity=provider_identity,
                ),
                objective_tracker=objective_tracker,
                output_boundary_snapshot=output_boundary_snapshot,
            )
            if cancellation_receipt is None:
                _close_provider_input(provider)
            else:
                _close_cancelled_provider_pipes(provider)

        if cancellation_receipt is not None:
            provider = None
            enforce_output_boundary()
            _terminalize(
                store,
                job_id,
                "CANCELLED",
                success_fields={"cancellation": cancellation_receipt},
                report="Forge job cancelled.",
                worker_authority=worker_authority,
            )
            return 0

        terminate_process_group(provider)
        provider = None
        enforce_output_boundary()
        if result_event is None:
            decision = ProviderPolicyDecision(
                status="FAILED",
                classification="internal_failure",
                reason="terminal_result_missing",
            )
            _append_event(
                store,
                job_id,
                "PROVIDER_FAILED",
                {
                    "status": decision.status,
                    "classification": decision.classification,
                    "reason": decision.reason,
                },
            )
            raise ProviderPolicyStop(decision)
        if result_event.succeeded is not True:
            raise WorkerError("Claude provider returned an error result")
        if observed_model is None:
            raise WorkerError("Claude provider never emitted an observed model")
        model_receipt = policy_tracker.model_receipt()
        if (
            len(model_receipt["models_observed"]) != 1
            or model_receipt["verification_level"] == "unverified"
        ):
            decision = ProviderPolicyDecision(
                status="PAUSED_POLICY",
                classification="model_mismatch",
                reason="model_identity_unverified",
            )
            _append_event(
                store,
                job_id,
                "PROVIDER_PAUSED",
                {
                    "status": decision.status,
                    "classification": decision.classification,
                    "reason": decision.reason,
                },
            )
            raise ProviderPolicyStop(decision)
        report = result_event.data.get("result")
        report = _bounded_text(report, "Claude final report", maximum_bytes=MAX_REPORT_BYTES)
        report = redact_text(report)
        if len(report.encode("utf-8")) > MAX_REPORT_BYTES:
            raise WorkerError("redacted Claude final report exceeds its byte bound")
        _terminalize(
            store,
            job_id,
            "SUCCEEDED",
            success_fields={
                "num_turns": result_event.num_turns,
            },
            report=report,
            worker_authority=worker_authority,
        )
        return 0
    except BaseException as error:
        if not provider_popen_attempted and not isinstance(error, ProviderPolicyStop):
            try:
                current = store.get_job(job_id)
                current_attempt = store.get_attempt(current.current_attempt_id)
                if (
                    current.status == "PREFLIGHT"
                    and current_attempt.resume_session_id is not None
                ):
                    try:
                        _append_event(
                            store,
                            job_id,
                            "WORKER_PREPARATION_RETRYABLE",
                            {
                                "error_type": error.__class__.__name__,
                                "message": redact_text(str(error))[:MAX_ERROR_TEXT_BYTES],
                            },
                        )
                    except (InvalidTransition, RevisionConflict, StateError):
                        pass
                    if store.reset_resume_preflight_for_retry(
                        job_id,
                        dispatch_token=token,
                        worker_pid=os.getpid(),
                    ):
                        return 1
            except (InvalidTransition, RevisionConflict, StateError, OSError):
                pass
        quiescent = True
        if provider is not None and getattr(
            provider,
            "_forge_cancellation_quiescent",
            False,
        ) is True:
            # Cancellation already proved the whole group has no non-zombie
            # members. Reap/close without ever signalling the now-reusable PGID.
            provider.poll()
            _close_cancelled_provider_pipes(provider)
            provider = None
        elif provider is not None:
            if not _fence_generic_provider_cleanup(
                store,
                job_id,
                dispatch_token=token,
                worker_pid=os.getpid(),
            ):
                # An admitted interrupt may signal only through the exact
                # durable ACK -> identity -> TERM/KILL -> quiescence path.
                # Losing the atomic inbox fence drops generic signal authority.
                quiescent = False
                provider = None
            else:
                try:
                    terminate_process_group(provider)
                except BaseException as cleanup_error:
                    quiescent = False
                    try:
                        _append_event(
                            store,
                            job_id,
                            "PROVIDER_CLEANUP_ERROR",
                            {
                                "error_type": cleanup_error.__class__.__name__,
                                "message": redact_text(str(cleanup_error))[:MAX_ERROR_TEXT_BYTES],
                            },
                        )
                    except (InvalidTransition, RevisionConflict, StateError):
                        pass
        if quiescent and output_boundary_snapshot is not None and not output_boundary_checked:
            try:
                enforce_output_boundary()
            except ProviderOutputBoundaryError as boundary_error:
                error = boundary_error
        inbox_settled = True
        try:
            current = store.get_job(job_id)
            if current.status == "RUNNING":
                store.close_command_inbox(
                    job_id,
                    dispatch_token=token,
                    worker_pid=os.getpid(),
                )
            elif current.status == "INTERRUPTING":
                if not quiescent:
                    inbox_settled = False
                else:
                    store.close_cancellation_inbox(
                        job_id,
                        _worker_authority=worker_authority,
                        require_quiescence=False,
                    )
        except (InvalidTransition, RevisionConflict, StateError, OSError):
            # Never publish a terminal receipt while accepted steering remains
            # durably open or queued. Recovery can inspect the nonterminal state.
            inbox_settled = False
        if quiescent and inbox_settled:
            if isinstance(error, ClaudeProtocolError):
                decision = ProviderPolicyDecision(
                    status="FAILED",
                    classification="protocol",
                    reason="protocol_failure",
                )
                try:
                    _append_event(
                        store,
                        job_id,
                        "PROVIDER_FAILED",
                        {
                            "status": decision.status,
                            "classification": decision.classification,
                            "reason": decision.reason,
                        },
                    )
                except (InvalidTransition, RevisionConflict, StateError):
                    pass
                error = ProviderPolicyStop(decision)
            if isinstance(error, ProviderPolicyStop):
                decision = error.decision
                try:
                    _terminalize(
                        store,
                        job_id,
                        decision.status,
                        error=(error if decision.status == "FAILED" else None),
                        report=(
                            None
                            if decision.status == "FAILED"
                            else f"Provider work paused: {decision.reason}."
                        ),
                        worker_authority=worker_authority,
                        policy_decision=decision,
                    )
                except (
                    InvalidTransition,
                    RevisionConflict,
                    StateError,
                    WorkerError,
                    OSError,
                ):
                    pass
            else:
                _fail_job(store, job_id, error)
        return 1


__all__ = ["WorkerError", "run_worker"]
