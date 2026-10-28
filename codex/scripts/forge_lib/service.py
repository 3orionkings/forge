"""Basic durable Forge MCP job service."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import os
import platform
import re
import selectors
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from .boundaries import BoundaryError, RepoIdentity, validate_boundary, validate_launch
from .claude import (
    AdapterError,
    PreflightError,
    normalize_budget_usd,
    normalize_model_identifier,
    preflight,
)
from .contracts import RECOVERY_DISABLED_MESSAGE, WORKSPACE_CONTEXT_FIELD
from .processes import (
    BoundWorkerLaunchError,
    ProcessError,
    _bind_test_cancellation_containment,
    cancellation_admission_preflight,
    cancellation_containment_preflight,
    process_instance_is_live,
    spawn_detached_worker,
)
from .security import SecurityError, build_child_environment, redact_text
from .state import (
    TERMINAL_STATUSES,
    IdempotencyConflict,
    InvalidTransition,
    Job,
    JobNotFound,
    RevisionConflict,
    StateError,
    Store,
    WorkerIdentityUnavailable,
    canonical_json,
)


BRIDGE_VERSION = "0.1.0"
DISPATCH_GUARD_PATH = Path(__file__).resolve().parents[1] / "dispatch-guard.sh"
MAX_GUARD_OUTPUT_BYTES = 64 * 1024
GUARD_TIMEOUT_SECONDS = 10.0
MAX_GUARD_ERROR_BYTES = 4096
GIT_EXECUTABLE = Path("/usr/bin/git")
DF_EXECUTABLE = Path("/bin/df")
MEMORY_PRESSURE_EXECUTABLE = Path("/usr/bin/memory_pressure")
PS_EXECUTABLE = Path("/bin/ps")
PYTHON_EXECUTABLE = Path("/usr/bin/python3")
MAX_RUNTIME_EXECUTABLE_BYTES = 64 * 1024 * 1024
DEFAULT_ROLE = "review"
DEFAULT_MODEL = "fable"
DEFAULT_EFFORT = "max"
DEFAULT_BUDGET = {
    "max_seconds": 1800,
    "max_tokens": 200_000,
    "max_turns": 100,
    "max_cost_usd": "1",
}
MAX_WAIT_SECONDS = 30.0
WAIT_POLL_SECONDS = 0.05
MAX_INLINE_REPORT_BYTES = 16 * 1024
MAX_WORKSPACE_BINDING_BYTES = 4096
MAX_ARTIFACT_BYTES = {
    "events.jsonl": 16 * 1024 * 1024,
    "stderr.log": 1024 * 1024,
    "report.txt": 1024 * 1024,
    "receipt.json": 256 * 1024,
    "worker.log": 1024 * 1024,
}
PUBLIC_STATUSES = {
    "queued": ("CREATED", "PREFLIGHT"),
    "running": ("RUNNING", "INTERRUPTING"),
    "completed": ("SUCCEEDED",),
    "failed": ("FAILED", "PAUSED_PROVIDER_ACCESS", "PAUSED_POLICY"),
    "cancelled": ("CANCELLED",),
}
INTERNAL_TO_PUBLIC = {
    internal: public
    for public, internals in PUBLIC_STATUSES.items()
    for internal in internals
}
RECEIPT_COMMON_FIELDS = {
    "job_id",
    "attempt_id",
    "repo_id",
    "worktree",
    "baseline_head",
    "status_fingerprint",
    "requested_model",
    "observed_model",
    "model_requested",
    "model_cli_accepted",
    "models_observed",
    "verification_level",
    "provider_version_observed",
    "usage",
    "total_cost_usd",
    "usage_provenance",
    "cost_provenance",
    "budget",
    "requested_effort",
    "session_id",
    "parent_tool_use_ids",
    "event_hash",
    "report_hash",
    "events_hash",
    "stderr_hash",
    "worker_log_hash",
    "admission_evidence_sha256",
    "terminal_status",
}
RECEIPT_STATUS_FIELDS = {
    "SUCCEEDED": {"num_turns"},
    "FAILED": {"error"},
    "CANCELLED": {"cancellation"},
    "PAUSED_PROVIDER_ACCESS": {"pause"},
    "PAUSED_POLICY": {"pause"},
}
RECEIPT_ARTIFACT_HASH_FIELDS = {
    "events": "events_hash",
    "stderr": "stderr_hash",
    "report": "report_hash",
    "worker_log": "worker_log_hash",
}
LEGACY_V8_PAYLOAD_FIELDS = {
    "prompt",
    "cwd",
    "role",
    "model",
    "effort",
    "budget",
    "max_budget_usd",
    "baseline",
    "claude_executable",
}
LEGACY_V9_PAYLOAD_FIELDS = {
    "prompt",
    "cwd",
    "role",
    "model",
    "effort",
    "budget",
    "baseline",
    "claude_executable",
}
LEGACY_V8_RECEIPT_COMMON_FIELDS = {
    "job_id",
    "attempt_id",
    "repo_id",
    "worktree",
    "baseline_head",
    "status_fingerprint",
    "requested_model",
    "observed_model",
    "requested_effort",
    "session_id",
    "parent_tool_use_ids",
    "event_hash",
    "report_hash",
    "events_hash",
    "stderr_hash",
    "worker_log_hash",
    "terminal_status",
}
LEGACY_V9_RECEIPT_COMMON_FIELDS = RECEIPT_COMMON_FIELDS - {
    "admission_evidence_sha256"
}
LEGACY_V8_RECEIPT_STATUS_FIELDS = {
    "SUCCEEDED": {"num_turns", "total_cost_usd", "usage"},
    "FAILED": {"error"},
    "CANCELLED": {"cancellation"},
}
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


@dataclass(frozen=True, slots=True)
class ToolExecution:
    body: dict[str, Any]
    message: str
    is_error: bool = False


def _strict_json_object(payload: bytes, label: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise StateError(f"{label} contains a duplicate key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise StateError(f"{label} contains a non-finite number")

    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except StateError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise StateError(f"{label} is malformed") from error
    if not isinstance(value, dict):
        raise StateError(f"{label} is not an object")

    nodes = 0

    def validate(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > 1024 or depth > 8:
            raise StateError(f"{label} exceeds its structural bound")
        if item is None or isinstance(item, bool):
            return
        if isinstance(item, str):
            if "\x00" in item or len(item.encode("utf-8")) > 64 * 1024:
                raise StateError(f"{label} contains unbounded text")
            return
        if isinstance(item, int) and not isinstance(item, bool):
            if abs(item) > 1_000_000_000_000:
                raise StateError(f"{label} contains an unbounded integer")
            return
        if isinstance(item, float):
            if not math.isfinite(item) or abs(item) > 1_000_000_000_000:
                raise StateError(f"{label} contains an unbounded number")
            return
        if isinstance(item, list):
            if len(item) > 256:
                raise StateError(f"{label} contains an unbounded list")
            for child in item:
                validate(child, depth + 1)
            return
        if isinstance(item, dict):
            if len(item) > 256:
                raise StateError(f"{label} contains an unbounded object")
            for key, child in item.items():
                if not isinstance(key, str) or len(key.encode("utf-8")) > 256:
                    raise StateError(f"{label} contains an invalid key")
                validate(child, depth + 1)
            return
        raise StateError(f"{label} contains an unsupported value")

    validate(value, 0)
    return value


def _receipt_hash(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise StateError(f"terminal receipt {label} is invalid")
    return value


def _validate_accounting_evidence(value: Any) -> dict[str, Any]:
    fields = {
        "usage",
        "total_cost_usd",
        "usage_provenance",
        "cost_provenance",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise StateError("provider accounting evidence is malformed")
    usage_provenance = value["usage_provenance"]
    cost_provenance = value["cost_provenance"]
    allowed = {"provider_authoritative", "unavailable"}
    if usage_provenance not in allowed or cost_provenance not in allowed:
        raise StateError("provider accounting provenance is invalid")
    usage = value["usage"]
    if usage_provenance == "unavailable":
        if usage is not None:
            raise StateError("unavailable provider usage must be null")
    elif not isinstance(usage, dict) or len(canonical_json(usage).encode("utf-8")) > 64 * 1024:
        raise StateError("provider-authoritative usage is invalid")
    cost = value["total_cost_usd"]
    if cost_provenance == "unavailable":
        if cost is not None:
            raise StateError("unavailable provider cost must be null")
    else:
        if not isinstance(cost, str) or not cost:
            raise StateError("provider-authoritative cost is invalid")
        try:
            encoded_cost = cost.encode("ascii")
        except UnicodeEncodeError as error:
            raise StateError("provider-authoritative cost is invalid") from error
        if len(encoded_cost) > 64:
            raise StateError("provider-authoritative cost is invalid")
        try:
            parsed_cost = Decimal(cost)
        except InvalidOperation as error:
            raise StateError("provider-authoritative cost is invalid") from error
        if not parsed_cost.is_finite() or parsed_cost < 0 or parsed_cost > Decimal("1000000"):
            raise StateError("provider-authoritative cost is invalid")
    return value


def _is_legacy_v8_payload(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != LEGACY_V8_PAYLOAD_FIELDS:
        return False
    budget = value.get("budget")
    if not isinstance(budget, dict) or set(budget) != {"max_seconds", "max_tokens"}:
        return False
    if any(
        isinstance(budget[name], bool) or not isinstance(budget[name], int)
        for name in ("max_seconds", "max_tokens")
    ):
        return False
    if not 1 <= budget["max_seconds"] <= 86_400:
        return False
    if not 1 <= budget["max_tokens"] <= 10_000_000:
        return False
    try:
        normalized_cost = normalize_budget_usd(value.get("max_budget_usd"))
    except AdapterError:
        return False
    return normalized_cost == value["max_budget_usd"]


def _is_legacy_v9_payload(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != LEGACY_V9_PAYLOAD_FIELDS:
        return False
    budget = value.get("budget")
    if not isinstance(budget, dict) or set(budget) != {
        "max_seconds",
        "max_tokens",
        "max_turns",
        "max_cost_usd",
    }:
        return False
    if any(
        isinstance(budget[name], bool) or not isinstance(budget[name], int)
        for name in ("max_seconds", "max_tokens", "max_turns")
    ):
        return False
    try:
        normalized_cost = normalize_budget_usd(budget["max_cost_usd"])
    except AdapterError:
        return False
    return (
        1 <= budget["max_seconds"] <= 86_400
        and 1 <= budget["max_tokens"] <= 10_000_000
        and 1 <= budget["max_turns"] <= 1_000_000
        and normalized_cost == budget["max_cost_usd"]
    )


def _safe_environment_path(name: str, default: Path, *, must_exist: bool) -> Path:
    raw = os.environ.get(name)
    if raw is None:
        candidate = default
    else:
        if not raw or "\x00" in raw or ".." in Path(raw).parts:
            raise SecurityError(f"{name} is not a safe path")
        candidate = Path(raw).expanduser()
    try:
        resolved = candidate.resolve(strict=must_exist)
    except (OSError, RuntimeError, ValueError) as error:
        raise SecurityError(f"{name} could not be resolved") from error
    if raw is not None and Path(raw).expanduser().absolute() != resolved:
        raise SecurityError(f"{name} must use its canonical path")
    return resolved


def _repo_id(identity: RepoIdentity) -> str:
    envelope = canonical_json({"common_dir": str(identity.common_dir)})
    return hashlib.sha256(envelope.encode("utf-8")).hexdigest()


def _owned_path_pin(
    path: Path,
    *,
    label: str,
    allowed_types: frozenset[str],
    expected_content_hash: str | None = None,
    require_single_link: bool = False,
) -> dict[str, Any]:
    """Capture one canonical, unowned-path-rejecting inode identity."""

    try:
        canonical = Path(path).resolve(strict=True)
        information = os.stat(canonical, follow_symlinks=False)
    except (OSError, RuntimeError, ValueError) as error:
        raise BoundaryError(f"{label} cannot be pinned") from error
    if canonical != Path(path):
        raise BoundaryError(f"{label} is not canonical")
    if stat.S_ISDIR(information.st_mode):
        path_type = "directory"
    elif stat.S_ISREG(information.st_mode):
        path_type = "regular"
    else:
        raise BoundaryError(f"{label} has an unsupported filesystem type")
    if path_type not in allowed_types:
        raise BoundaryError(f"{label} has an invalid filesystem type")
    if information.st_uid != os.geteuid():
        raise BoundaryError(f"{label} is not owned by the current user")
    if path_type == "regular" and require_single_link and information.st_nlink != 1:
        raise BoundaryError(f"{label} must have exactly one hard link")
    content_hash = None
    if path_type == "regular":
        descriptor = None
        try:
            flags = os.O_RDONLY | os.O_NOFOLLOW
            flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
            descriptor = os.open(canonical, flags)
            held = os.fstat(descriptor)
            if (
                not stat.S_ISREG(held.st_mode)
                or held.st_uid != os.geteuid()
                or (require_single_link and held.st_nlink != 1)
                or (held.st_dev, held.st_ino) != (information.st_dev, information.st_ino)
                or held.st_size > GIT_METADATA_LIMIT_BYTES
            ):
                raise BoundaryError(f"{label} changed while it was pinned")
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
                    raise BoundaryError(f"{label} exceeds its byte bound")
            current = os.stat(canonical, follow_symlinks=False)
            if (
                (current.st_dev, current.st_ino, current.st_uid)
                != (held.st_dev, held.st_ino, held.st_uid)
                or (require_single_link and current.st_nlink != 1)
            ):
                raise BoundaryError(f"{label} changed while it was pinned")
            content_hash = hashlib.sha256(data).hexdigest()
        except OSError as error:
            raise BoundaryError(f"{label} cannot be read safely") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if expected_content_hash is not None and content_hash != expected_content_hash:
            raise BoundaryError(f"{label} content changed during validation")
    elif expected_content_hash is not None:
        raise BoundaryError(f"{label} directory cannot have a content hash")
    return {
        "path": str(canonical),
        "device": information.st_dev,
        "inode": information.st_ino,
        "owner": information.st_uid,
        "mode": stat.S_IMODE(information.st_mode),
        "type": path_type,
        "content_hash": content_hash,
    }


def _repository_path_pin(identity: RepoIdentity) -> dict[str, Any]:
    worktree = _owned_path_pin(
        identity.top_level,
        label="Git worktree",
        allowed_types=frozenset({"directory"}),
    )
    marker = _owned_path_pin(
        identity.git_marker,
        label="Git worktree marker",
        allowed_types=frozenset({"directory", "regular"}),
        expected_content_hash=identity.git_marker_content_hash,
    )
    git_dir = _owned_path_pin(
        identity.git_dir,
        label="Git directory",
        allowed_types=frozenset({"directory"}),
    )
    common = _owned_path_pin(
        identity.common_dir,
        label="Git common directory",
        allowed_types=frozenset({"directory"}),
    )
    if (identity.git_commondir_file is None) != (
        identity.git_commondir_content_hash is None
    ):
        raise BoundaryError("Git common-directory pointer identity is incomplete")
    commondir_file = (
        None
        if identity.git_commondir_file is None
        else _owned_path_pin(
            identity.git_commondir_file,
            label="Git common-directory pointer",
            allowed_types=frozenset({"regular"}),
            expected_content_hash=identity.git_commondir_content_hash,
        )
    )
    if (identity.git_worktree_backpointer is None) != (
        identity.git_worktree_backpointer_content_hash is None
    ):
        raise BoundaryError("Git worktree back-pointer identity is incomplete")
    backpointer = (
        None
        if identity.git_worktree_backpointer is None
        else _owned_path_pin(
            identity.git_worktree_backpointer,
            label="Git worktree back-pointer",
            allowed_types=frozenset({"regular"}),
            expected_content_hash=identity.git_worktree_backpointer_content_hash,
        )
    )

    def optional(pin: dict[str, Any] | None, name: str) -> dict[str, Any]:
        return {
            name: None if pin is None else pin["path"],
            f"{name}_device": None if pin is None else pin["device"],
            f"{name}_inode": None if pin is None else pin["inode"],
            f"{name}_owner": None if pin is None else pin["owner"],
            f"{name}_type": None if pin is None else pin["type"],
            f"{name}_content_hash": None if pin is None else pin["content_hash"],
        }

    return {
        "top_level": worktree["path"],
        "common_dir": common["path"],
        "git_marker": marker["path"],
        "git_dir": git_dir["path"],
        "git_marker_content_hash": marker["content_hash"],
        "worktree_device": worktree["device"],
        "worktree_inode": worktree["inode"],
        "worktree_owner": worktree["owner"],
        "worktree_type": worktree["type"],
        "git_marker_device": marker["device"],
        "git_marker_inode": marker["inode"],
        "git_marker_owner": marker["owner"],
        "git_marker_type": marker["type"],
        "git_dir_device": git_dir["device"],
        "git_dir_inode": git_dir["inode"],
        "git_dir_owner": git_dir["owner"],
        "git_dir_type": git_dir["type"],
        "common_dir_device": common["device"],
        "common_dir_inode": common["inode"],
        "common_dir_owner": common["owner"],
        "common_dir_type": common["type"],
        **optional(commondir_file, "git_commondir_file"),
        **optional(backpointer, "git_worktree_backpointer"),
    }


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _terminate_guard_process(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as error:
        raise ProcessError("dispatch guard process group could not be stopped") from error
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.wait()
        raise ProcessError("dispatch guard process group could not be reaped") from error


def _bounded_guard_output(
    process: subprocess.Popen[bytes],
) -> tuple[bytes, bytes]:
    if process.stdout is None or process.stderr is None:
        raise ProcessError("dispatch guard output pipes are unavailable")
    streams = {process.stdout.fileno(): bytearray(), process.stderr.fileno(): bytearray()}
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + GUARD_TIMEOUT_SECONDS
    try:
        for descriptor in streams:
            os.set_blocking(descriptor, False)
            selector.register(descriptor, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_guard_process(process)
                raise ProcessError("dispatch guard timed out")
            ready = selector.select(min(0.1, remaining))
            for key, _mask in ready:
                descriptor = key.fd
                while True:
                    try:
                        chunk = os.read(descriptor, 65_536)
                    except BlockingIOError:
                        break
                    if not chunk:
                        selector.unregister(descriptor)
                        break
                    streams[descriptor].extend(chunk)
                    if len(streams[descriptor]) > MAX_GUARD_OUTPUT_BYTES:
                        _terminate_guard_process(process)
                        raise ProcessError(
                            "dispatch guard output exceeds its byte bound"
                        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_guard_process(process)
            raise ProcessError("dispatch guard timed out")
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            _terminate_guard_process(process)
            raise ProcessError("dispatch guard timed out") from error
        return bytes(streams[process.stdout.fileno()]), bytes(
            streams[process.stderr.fileno()]
        )
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()


def _canonical_evidence(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


_DIRECTORY_ENTRY_CACHE: dict[tuple[int, int, int, int], frozenset[str]] = {}


def _exact_directory_entries(directory_fd: int) -> frozenset[str]:
    """Cache one held directory listing only while its identity facts match."""

    information = os.fstat(directory_fd)
    key = (
        information.st_dev,
        information.st_ino,
        information.st_mtime_ns,
        information.st_ctime_ns,
    )
    cached = _DIRECTORY_ENTRY_CACHE.get(key)
    if cached is not None:
        return cached
    entries = frozenset(os.listdir(directory_fd))
    if len(_DIRECTORY_ENTRY_CACHE) >= 4096:
        _DIRECTORY_ENTRY_CACHE.clear()
    _DIRECTORY_ENTRY_CACHE[key] = entries
    return entries


def _admission_artifact_pin(
    declaration: Any,
    *,
    label: str,
    allowed_root: Path | None = None,
) -> dict[str, Any]:
    if not isinstance(declaration, dict) or set(declaration) != {"path", "sha256"}:
        raise StateError(f"{label} declaration is malformed")
    expected_hash = declaration["sha256"]
    if (
        not isinstance(expected_hash, str)
        or len(expected_hash) != 64
        or any(character not in "0123456789abcdef" for character in expected_hash)
    ):
        raise StateError(f"{label} hash is malformed")
    raw_path = declaration["path"]
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
        raise StateError(f"{label} path is malformed")
    path = Path(raw_path)
    if not path.is_absolute() or ".." in path.parts:
        raise StateError(f"{label} path must be absolute and exact")
    directory_fd: int | None = None
    try:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        directory_fd = os.open(os.path.sep, directory_flags)
        components = path.parts[1:]
        if not components:
            raise BoundaryError(f"{label} must identify a file")
        for index, component in enumerate(components):
            directory_entries = _exact_directory_entries(directory_fd)
            if component not in directory_entries:
                aliases = [
                    entry
                    for entry in directory_entries
                    if unicodedata.normalize("NFC", entry).casefold()
                    == unicodedata.normalize("NFC", component).casefold()
                ]
                if aliases:
                    raise BoundaryError(f"{label} path spelling is aliased")
                raise BoundaryError(f"{label} path does not exist")
            if index != len(components) - 1:
                next_fd = os.open(
                    component,
                    directory_flags,
                    dir_fd=directory_fd,
                )
                os.close(directory_fd)
                directory_fd = next_fd
    except OSError as error:
        raise BoundaryError(f"{label} path spelling cannot be inspected") from error
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
    pin = _owned_path_pin(
        path,
        label=label,
        allowed_types=frozenset({"regular"}),
        expected_content_hash=expected_hash,
        require_single_link=True,
    )
    canonical = Path(pin["path"])
    if allowed_root is not None and not canonical.is_relative_to(allowed_root):
        raise StateError(f"{label} is outside the admitted repository")
    return {
        "path": pin["path"],
        "device": pin["device"],
        "inode": pin["inode"],
        "owner": pin["owner"],
        "mode": pin["mode"],
        "type": pin["type"],
        "sha256": pin["content_hash"],
    }


def _read_admission_bytes(
    pin: dict[str, Any],
    *,
    label: str,
    maximum_bytes: int = GIT_METADATA_LIMIT_BYTES,
) -> bytes:
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW
        flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(pin["path"], flags)
        information = os.fstat(descriptor)
        if (
            not stat.S_ISREG(information.st_mode)
            or information.st_nlink != 1
            or information.st_size > maximum_bytes
            or (information.st_dev, information.st_ino, information.st_uid)
            != (pin["device"], pin["inode"], pin["owner"])
            or stat.S_IMODE(information.st_mode) != pin["mode"]
        ):
            raise StateError(f"{label} identity changed while it was opened")
        data = bytearray()
        while True:
            chunk = os.read(descriptor, min(65_536, maximum_bytes + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > maximum_bytes:
                raise StateError(f"{label} exceeds its byte bound")
        if (
            len(data) != information.st_size
            or _sha256_bytes(bytes(data)) != pin["sha256"]
        ):
            raise StateError(f"{label} content does not match its pin")
        return bytes(data)
    except OSError as error:
        raise StateError(f"{label} cannot be read safely") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_admission_json(
    pin: dict[str, Any],
    *,
    label: str,
    maximum_bytes: int = 64 * 1024,
) -> dict[str, Any]:
    return _strict_json_object(
        _read_admission_bytes(pin, label=label, maximum_bytes=maximum_bytes),
        label,
    )


def _derive_oracle_capability_target_pin(
    receipt_pin: dict[str, Any],
    contract_pin: dict[str, Any],
    repository_root: Path,
) -> dict[str, Any]:
    """Independently verify a claimed exact-oracle probe and pin its target."""

    try:
        text = _read_admission_bytes(
            receipt_pin,
            label="oracle capability receipt",
            maximum_bytes=64 * 1024,
        ).decode("utf-8")
        pairs: dict[str, str] = {}
        for line in text.splitlines():
            key, value = line.split("=", 1)
            if key in pairs:
                raise ValueError
            pairs[key] = value
    except (UnicodeError, ValueError) as error:
        raise StateError("oracle capability receipt is malformed") from error
    required = {
        "version",
        "receipt_id",
        "contract_sha256",
        "mechanism",
        "disposable_target",
        "exit",
        "oracle_sha256",
        "observed_sha256",
    }
    if set(pairs) != required:
        raise StateError("oracle capability receipt fields differ")

    def bounded_ascii(name: str, maximum: int) -> str:
        value = pairs[name]
        if (
            not value
            or value != value.strip()
            or len(value.encode("utf-8")) > maximum
            or not value.isascii()
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        ):
            raise StateError(f"oracle capability {name} is malformed")
        return value

    if pairs["version"] != "1" or pairs["exit"] != "0":
        raise StateError("oracle capability receipt version or exit is invalid")
    bounded_ascii("receipt_id", 256)
    bounded_ascii("mechanism", 512)
    for name in ("contract_sha256", "oracle_sha256", "observed_sha256"):
        digest = pairs[name]
        if (
            len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise StateError(f"oracle capability {name} is malformed")
    if pairs["contract_sha256"] != contract_pin["sha256"]:
        raise StateError("oracle capability execution contract differs")
    if pairs["oracle_sha256"] != pairs["observed_sha256"]:
        raise StateError("oracle capability did not reproduce the exact bytes")

    target_path = bounded_ascii("disposable_target", 4096)
    target_pin = _admission_artifact_pin(
        {"path": target_path, "sha256": pairs["observed_sha256"]},
        label="oracle disposable target",
    )
    canonical_target = Path(target_pin["path"])
    if canonical_target == repository_root or canonical_target.is_relative_to(
        repository_root
    ):
        raise StateError("oracle capability target is not disposable")
    return target_pin


def _derive_freeze_manifest_entry_pins(
    manifest_pin: dict[str, Any],
) -> list[dict[str, Any]]:
    """Parse every assured freeze entry and derive its live inode/content pin."""

    try:
        text = _read_admission_bytes(
            manifest_pin,
            label="assured freeze manifest",
            maximum_bytes=1024 * 1024,
        ).decode("utf-8")
    except UnicodeError as error:
        raise StateError("assured freeze manifest is not UTF-8") from error
    lines = text.splitlines()
    if not lines or len(lines) > 48:
        raise StateError("assured freeze manifest entry count is outside its bound")
    entries: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    seen_identities: set[tuple[int, int]] = set()
    for line in lines:
        match = re.fullmatch(r"([0-9A-Fa-f]{64}) [ *](.+)", line)
        if match is None:
            raise StateError("assured freeze manifest is malformed")
        declaration = {
            "path": match.group(2),
            "sha256": match.group(1).lower(),
        }
        pin = _admission_artifact_pin(
            declaration,
            label="assured freeze manifest entry",
        )
        identity = (pin["device"], pin["inode"])
        if pin["path"] in seen_paths or identity in seen_identities:
            raise StateError("assured freeze manifest entries are aliased")
        if pin["path"] == manifest_pin["path"] or identity == (
            manifest_pin["device"],
            manifest_pin["inode"],
        ):
            raise StateError("assured freeze manifest lists itself")
        seen_paths.add(pin["path"])
        seen_identities.add(identity)
        entries.append(pin)
    return entries


def _exact_declared_path(root: Path, relative: Any, *, allow_missing: bool = True) -> str:
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        raise StateError("declared file path is invalid")
    if unicodedata.normalize("NFC", relative) != relative:
        raise StateError("declared file path has non-canonical Unicode")
    candidate = Path(relative)
    if (
        candidate.is_absolute()
        or relative.startswith(("./", "../"))
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or "*" in relative
        or "?" in relative
    ):
        raise StateError("declared file path must be exact and repository-relative")
    current = root
    for index, component in enumerate(candidate.parts):
        try:
            names = os.listdir(current)
        except OSError as error:
            raise StateError("declared file parent cannot be inspected") from error
        if component not in names:
            aliases = [
                name
                for name in names
                if unicodedata.normalize("NFC", name).casefold()
                == unicodedata.normalize("NFC", component).casefold()
            ]
            if aliases:
                raise StateError("declared file path case does not match the filesystem")
            if index != len(candidate.parts) - 1 or not allow_missing:
                raise StateError("declared file path does not exist")
            current = current / component
            break
        current = current / component
        try:
            information = os.stat(current, follow_symlinks=False)
        except OSError as error:
            raise StateError("declared file path cannot be inspected") from error
        if stat.S_ISLNK(information.st_mode):
            raise StateError("declared file path contains a symlink")
        if index != len(candidate.parts) - 1 and not stat.S_ISDIR(information.st_mode):
            raise StateError("declared file parent is not a directory")
        if (
            index == len(candidate.parts) - 1
            and stat.S_ISREG(information.st_mode)
            and information.st_nlink != 1
        ):
            raise StateError("declared writable file has multiple hard links")
    try:
        canonical_parent = current.parent.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise StateError("declared file parent cannot be resolved") from error
    if not canonical_parent.is_relative_to(root):
        raise StateError("declared file escapes the repository")
    return relative


def _review_control_pin_values(
    review_controls: dict[str, Any],
) -> tuple[dict[str, Any], ...]:
    if not review_controls:
        return ()
    values = [review_controls["plan"], review_controls["review_log"]]
    values.extend(review_controls.get("domain_docs", []))
    return tuple(values)


def _pin_review_controls(
    declaration: Any,
    *,
    root: Path,
    label: str,
) -> dict[str, Any]:
    if not isinstance(declaration, dict) or set(declaration) not in (
        {"plan", "review_log"},
        {"plan", "review_log", "domain_docs"},
    ):
        raise StateError(f"{label} declaration is malformed")
    result = {
        name: _admission_artifact_pin(
            declaration[name],
            label=f"{label} {name}",
            allowed_root=root,
        )
        for name in ("plan", "review_log")
    }
    domain_declarations = declaration.get("domain_docs", [])
    if not isinstance(domain_declarations, list) or len(domain_declarations) > 64:
        raise StateError(f"{label} domain docs exceed their bound")
    if any(
        not isinstance(item, dict) or set(item) != {"path", "sha256"}
        for item in domain_declarations
    ):
        raise StateError(f"{label} domain doc declaration is malformed")
    domain_paths = [item["path"] for item in domain_declarations]
    if any(not isinstance(path, str) for path in domain_paths):
        raise StateError(f"{label} domain doc path is malformed")
    if (
        domain_paths != sorted(domain_paths)
        or len(domain_paths) != len(set(domain_paths))
    ):
        raise StateError(f"{label} domain docs are not canonical sorted unique paths")
    domain_pins: list[dict[str, Any]] = []
    for index, item in enumerate(domain_declarations):
        relative = _exact_declared_path(root, item.get("path"), allow_missing=False)
        domain_pins.append(
            _admission_artifact_pin(
                {"path": str(root / relative), "sha256": item.get("sha256")},
                label=f"{label} domain doc {index}",
                allowed_root=root,
            )
        )
    if domain_pins:
        result["domain_docs"] = domain_pins
    pins = _review_control_pin_values(result)
    if (
        len({pin["path"] for pin in pins}) != len(pins)
        or len({(pin["device"], pin["inode"]) for pin in pins}) != len(pins)
    ):
        raise StateError(f"{label} roles and domain docs must use distinct artifacts")
    return result


def _review_control_declaration_from_pins(
    review_controls: dict[str, Any],
    *,
    root: Path,
) -> dict[str, Any]:
    declaration = {
        name: {
            "path": review_controls[name]["path"],
            "sha256": review_controls[name]["sha256"],
        }
        for name in ("plan", "review_log")
    }
    domain_docs = [
        {
            "path": str(Path(pin["path"]).relative_to(root)),
            "sha256": pin["sha256"],
        }
        for pin in review_controls.get("domain_docs", [])
    ]
    if domain_docs:
        declaration["domain_docs"] = domain_docs
    return declaration


def _trusted_tool(path: Path, label: str) -> str:
    try:
        information = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise StateError(f"trusted {label} executable is unavailable") from error
    if (
        not path.is_absolute()
        or stat.S_ISLNK(information.st_mode)
        or not stat.S_ISREG(information.st_mode)
        or information.st_uid != 0
        or stat.S_IMODE(information.st_mode) & 0o022
    ):
        raise StateError(f"trusted {label} executable identity is unsafe")
    return str(path)


def _trusted_runtime_pin(path: Path, label: str) -> dict[str, Any]:
    _trusted_tool(path, label)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
        information = os.fstat(descriptor)
        if (
            not stat.S_ISREG(information.st_mode)
            or information.st_uid != 0
            or stat.S_IMODE(information.st_mode) & 0o022
            or information.st_size > MAX_RUNTIME_EXECUTABLE_BYTES
        ):
            raise StateError(f"trusted {label} executable identity is unsafe")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_RUNTIME_EXECUTABLE_BYTES:
                raise StateError(f"trusted {label} executable exceeds its byte bound")
            digest.update(chunk)
        rebound = os.stat(path, follow_symlinks=False)
        if (
            total != information.st_size
            or (information.st_dev, information.st_ino, information.st_uid)
            != (rebound.st_dev, rebound.st_ino, rebound.st_uid)
        ):
            raise StateError(f"trusted {label} executable changed while pinned")
        return {
            "path": str(path),
            "device": information.st_dev,
            "inode": information.st_ino,
            "owner": information.st_uid,
            "sha256": digest.hexdigest(),
        }
    except OSError as error:
        raise StateError(f"trusted {label} executable cannot be pinned") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _probe_environment(*, git: bool = False) -> dict[str, str]:
    environment = {
        "HOME": "/var/empty",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }
    if git:
        environment.update(
            {
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_OPTIONAL_LOCKS": "0",
            }
        )
    return environment


def _provider_family(value: Any) -> str:
    if not isinstance(value, str):
        return "unknown"
    normalized = value.casefold()
    if "codex" in normalized or "sol" in normalized:
        return "codex"
    if "claude" in normalized or "fable" in normalized or "opus" in normalized:
        return "claude"
    return "unknown"


def _git_dirty_paths(root: Path) -> tuple[str, ...]:
    try:
        completed = subprocess.run(
            [
                _trusted_tool(GIT_EXECUTABLE, "Git"),
                "-C",
                str(root),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
            shell=False,
            env=_probe_environment(git=True),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise BoundaryError("Git status could not be derived for admission") from error
    if completed.returncode != 0 or len(completed.stdout) > 1024 * 1024:
        raise BoundaryError("Git status could not be derived for admission")
    records = completed.stdout.split(b"\0")
    paths: list[str] = []
    index = 0
    try:
        while index < len(records):
            record = records[index]
            index += 1
            if not record:
                continue
            if len(record) < 4 or record[2:3] != b" ":
                raise ValueError
            status_code = record[:2]
            paths.append(os.fsdecode(record[3:]))
            if b"R" in status_code or b"C" in status_code:
                if index >= len(records) or not records[index]:
                    raise ValueError
                paths.append(os.fsdecode(records[index]))
                index += 1
    except (UnicodeError, ValueError) as error:
        raise BoundaryError("Git status was malformed during admission") from error
    return tuple(sorted(set(paths)))


def _machine_snapshot() -> dict[str, float | int]:
    cores = os.cpu_count()
    if cores is None or cores <= 0:
        raise StateError("machine core count is unavailable")
    try:
        load = float(os.getloadavg()[0])
    except (AttributeError, OSError) as error:
        raise StateError("machine load is unavailable") from error
    if not math.isfinite(load) or load < 0:
        raise StateError("machine load is invalid")
    memory_total = 0.0
    memory_available = 0.0
    swap_free = 0.0
    cpu_idle: float | None = None
    if platform.system() == "Darwin":
        try:
            volume = "/System/Volumes/VM" if Path("/System/Volumes/VM").is_dir() else "/"
            disk = subprocess.run(
                [_trusted_tool(DF_EXECUTABLE, "df"), "-Pk", volume], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, timeout=3, check=False, shell=False,
                env=_probe_environment(),
            )
            swap_free = float(disk.stdout.splitlines()[1].split()[3]) * 1024
            pressure = subprocess.run(
                [_trusted_tool(MEMORY_PRESSURE_EXECUTABLE, "memory_pressure"), "-Q"], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, timeout=3, check=False, shell=False,
                env=_probe_environment(),
            )
            memory_match = re.search(
                r"System-wide memory free percentage:\s*([0-9.]+)%",
                pressure.stdout,
            )
            processes = subprocess.run(
                [_trusted_tool(PS_EXECUTABLE, "ps"), "-A", "-o", "%cpu="], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, timeout=3, check=False, shell=False,
                env=_probe_environment(),
            )
            process_cpu = sum(
                float(value.strip())
                for value in processes.stdout.splitlines()
                if value.strip()
            )
            if disk.returncode != 0 or memory_match is None or processes.returncode != 0:
                raise ValueError
            memory_free_pct = float(memory_match.group(1))
            cpu_idle = max(0.0, min(100.0, 100.0 - process_cpu / cores))
        except (OSError, ValueError, IndexError, subprocess.SubprocessError) as error:
            raise StateError("Darwin machine headroom probes are unavailable") from error
        available_headroom = swap_free
    elif Path("/proc/meminfo").is_file():
        try:
            for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
                name, raw = line.split(":", 1)
                amount = float(raw.strip().split()[0]) * 1024
                if name == "MemTotal":
                    memory_total = amount
                elif name == "MemAvailable":
                    memory_available = amount
                elif name == "SwapFree":
                    swap_free = amount
            if memory_total <= 0 or memory_available < 0:
                raise ValueError
        except (OSError, UnicodeError, ValueError, IndexError) as error:
            raise StateError("Linux machine headroom probes are unavailable") from error
        available_headroom = max(swap_free, 0.0)
        memory_free_pct = memory_available * 100.0 / memory_total
        cpu_idle = max(0.0, min(100.0, 100.0 - (load / cores) * 100.0))
    else:
        raise StateError("machine headroom probes are unsupported")
    if cpu_idle is None:
        raise StateError("machine CPU headroom is unavailable")
    return {
        "core_count": cores,
        "load_1": round(max(load, 0.0), 6),
        "free_swap_gb": round(available_headroom / (1024 ** 3), 6),
        "cpu_idle_pct": round(cpu_idle, 6),
        "memory_free_pct": round(
            max(0.0, min(100.0, memory_free_pct)), 6
        ),
    }


def _measured_wip(root: Path) -> int:
    # Unmerged lane branches are measured against the remote's default branch;
    # a repository where it does not resolve fails closed.
    try:
        base_ref = subprocess.run(
            [
                _trusted_tool(GIT_EXECUTABLE, "Git"),
                "-C",
                str(root),
                "rev-parse",
                "--verify",
                "refs/remotes/origin/HEAD^{commit}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
            shell=False,
            env=_probe_environment(git=True),
        )
        if base_ref.returncode != 0:
            raise OSError
        measurement_base = base_ref.stdout.strip()
        if re.fullmatch(
            r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", measurement_base
        ) is None:
            raise ValueError
        refs = subprocess.run(
            [
                _trusted_tool(GIT_EXECUTABLE, "Git"), "-C", str(root), "for-each-ref",
                "--format=%(objectname)",
                "refs/heads/opus/*", "refs/heads/sol/*", "refs/heads/fable/*",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
            shell=False,
            env=_probe_environment(git=True),
        )
        if refs.returncode != 0:
            raise OSError
        count = 0
        for object_id in refs.stdout.splitlines():
            if re.fullmatch(
                r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", object_id
            ) is None:
                raise ValueError
            ahead = subprocess.run(
                [_trusted_tool(GIT_EXECUTABLE, "Git"), "-C", str(root), "rev-list", "--count", f"{measurement_base}..{object_id}"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
                check=False,
                shell=False,
                env=_probe_environment(git=True),
            )
            ahead_text = ahead.stdout.strip()
            if ahead.returncode != 0 or re.fullmatch(r"[0-9]+", ahead_text) is None:
                raise OSError
            if int(ahead_text) > 0:
                count += 1
        return count
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise StateError(
            "WIP_UNMEASURABLE: WIP could not be measured for admission"
        ) from error


class ForgeService:
    """Implements the non-control Task-7 Forge operations."""

    def __init__(
        self,
        *,
        state_path: str | os.PathLike[str],
        launch_workspace: str | os.PathLike[str],
        claude_executable: str,
        bridge_path: str | os.PathLike[str],
        environment: dict[str, str] | None = None,
        cancellation_containment: object | None = None,
        _admission_machine_probe: Callable[[], dict[str, float | int]] = _machine_snapshot,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.state_path = Path(state_path).resolve()
        self.launch_workspace = Path(launch_workspace).resolve(strict=True)
        self.claude_executable = claude_executable
        self.bridge_path = Path(bridge_path).resolve(strict=True)
        self.environment = dict(os.environ if environment is None else environment)
        self.cancellation_containment = cancellation_containment
        self._admission_machine_probe = _admission_machine_probe
        self.monotonic = monotonic
        self.sleep = sleep
        self.store = Store(self.state_path)
        self._launch_repo_id = _repo_id(self._identity(self.launch_workspace))

    @classmethod
    def from_environment(cls) -> "ForgeService":
        home = Path.home().resolve(strict=True)
        state_path = _safe_environment_path(
            "FORGE_STATE_PATH",
            home / ".codex" / "forge" / "state.sqlite3",
            must_exist=False,
        )
        launch_workspace = _safe_environment_path(
            "FORGE_LAUNCH_WORKSPACE",
            Path.cwd(),
            must_exist=True,
        )
        executable = os.environ.get("FORGE_CLAUDE_EXECUTABLE", "claude")
        if not executable or "\x00" in executable:
            raise SecurityError("FORGE_CLAUDE_EXECUTABLE is invalid")
        bridge_path = Path(__file__).resolve().parents[1] / "forge_bridge.py"
        return cls(
            state_path=state_path,
            launch_workspace=launch_workspace,
            claude_executable=executable,
            bridge_path=bridge_path,
        )

    @classmethod
    def from_environment_for_workspace(
        cls,
        launch_workspace: str | os.PathLike[str],
    ) -> "ForgeService":
        """Build a service for a workspace authenticated by the Codex hook."""

        home = Path.home().resolve(strict=True)
        state_path = _safe_environment_path(
            "FORGE_STATE_PATH",
            home / ".codex" / "forge" / "state.sqlite3",
            must_exist=False,
        )
        try:
            workspace = Path(launch_workspace).resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as error:
            raise SecurityError("bound Forge workspace could not be resolved") from error
        executable = os.environ.get("FORGE_CLAUDE_EXECUTABLE", "claude")
        if not executable or "\x00" in executable:
            raise SecurityError("FORGE_CLAUDE_EXECUTABLE is invalid")
        bridge_path = Path(__file__).resolve().parents[1] / "forge_bridge.py"
        return cls(
            state_path=state_path,
            launch_workspace=workspace,
            claude_executable=executable,
            bridge_path=bridge_path,
        )

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolExecution:
        handlers = {
            "forge_preflight": self.preflight,
            "forge_start": self.start,
            "forge_status": self.status,
            "forge_wait": self.wait,
            "forge_result": self.result,
            "forge_resume": self.resume,
            "forge_steer": self.steer,
            "forge_cancel": self.cancel,
            "forge_list": self.list_jobs,
        }
        if name not in handlers:
            return ToolExecution(
                {
                    "status": "not_implemented",
                    "tool": name,
                    "message": "This Forge control operation is reserved for its planned task.",
                },
                "This Forge control operation is not implemented yet.",
                True,
            )
        try:
            return handlers[name](arguments)
        except (
            AdapterError,
            BoundaryError,
            IdempotencyConflict,
            JobNotFound,
            OSError,
            PreflightError,
            ProcessError,
            SecurityError,
            StateError,
            ValueError,
        ) as error:
            detail = redact_text(str(error)) or error.__class__.__name__
            if len(detail.encode("utf-8", errors="replace")) > 4096:
                detail = detail.encode("utf-8", errors="replace")[:4096].decode(
                    "utf-8", errors="replace"
                )
            body = {
                "status": (
                    "rejected"
                    if name
                    in {
                        "forge_start",
                        "forge_preflight",
                        "forge_resume",
                        "forge_steer",
                        "forge_cancel",
                    }
                    else "error"
                ),
                "tool": name,
                "error_type": error.__class__.__name__,
                "message": detail,
            }
            return ToolExecution(body, detail, True)

    def _identity(self, cwd: str | os.PathLike[str]) -> RepoIdentity:
        return validate_boundary(cwd, self.launch_workspace)

    def _owned_job(self, job_id: str) -> Job:
        """Resolve a job only when it belongs to this bridge repository."""
        try:
            job = self.store.get_job(job_id)
        except JobNotFound as error:
            raise JobNotFound("job not found") from error
        if job.repo_id != self._launch_repo_id:
            raise JobNotFound("job not found")
        return job

    @staticmethod
    def _reject_recovery_mutation(job: Job) -> None:
        if isinstance(job.payload, dict) and job.payload.get("role") == "recovery":
            raise StateError(RECOVERY_DISABLED_MESSAGE)

    def preflight(self, arguments: dict[str, Any]) -> ToolExecution:
        cwd = arguments.get("cwd", str(self.launch_workspace))
        identity = self._identity(cwd)
        with self.store.connection() as connection:
            schema_version = connection.execute(
                "SELECT version FROM schema_version WHERE singleton = 1"
            ).fetchone()[0]
        containment = cancellation_containment_preflight(
            self.cancellation_containment
        )
        common = {
            "bridge_version": BRIDGE_VERSION,
            "schema_version": schema_version,
            "state_path": str(self.store.path),
            "allowed_repository": {
                "top_level": str(identity.top_level),
                "git_marker": str(identity.git_marker),
                "common_dir": str(identity.common_dir),
                "head": identity.head,
                "status_fingerprint": identity.status_fingerprint,
                "tracked_content_fingerprint": identity.tracked_content_fingerprint,
            },
            "containment": containment,
            "provider_called": False,
        }
        cli = preflight(self.claude_executable, environment=self.environment)
        body = {
            "status": "ready",
            **common,
            "claude": cli.redacted_summary(),
        }
        return ToolExecution(body, "Forge preflight passed.")

    def _run_dispatch_guard(
        self,
        guard_argv: list[str],
        *,
        scheduler_snapshot: dict[str, Any],
        cwd: Path,
        pinned_artifacts: tuple[dict[str, Any], ...] = (),
    ) -> dict[str, Any]:
        """Execute immutable snapshots of the guard and every admitted input."""

        guard_path = DISPATCH_GUARD_PATH.absolute()
        try:
            guard_lstat = os.stat(guard_path, follow_symlinks=False)
            if stat.S_ISLNK(guard_lstat.st_mode) or guard_path.resolve(strict=True) != guard_path:
                raise ProcessError("dispatch guard install path is not canonical")
        except OSError as error:
            raise ProcessError("dispatch guard install path cannot be inspected") from error
        guard_pin = _owned_path_pin(
            guard_path,
            label="dispatch guard",
            allowed_types=frozenset({"regular"}),
        )
        runtime_pins = {
            "python": _trusted_runtime_pin(PYTHON_EXECUTABLE, "Python"),
            "git": _trusted_runtime_pin(GIT_EXECUTABLE, "Git"),
        }
        descriptor: int | None = None
        snapshots: list[Any] = []
        try:
            flags = os.O_RDONLY | os.O_NOFOLLOW
            flags |= getattr(os, "O_CLOEXEC", 0)
            descriptor = os.open(guard_path, flags)
            held = os.fstat(descriptor)
            if (
                not stat.S_ISREG(held.st_mode)
                or held.st_uid != os.geteuid()
                or (held.st_dev, held.st_ino)
                != (guard_pin["device"], guard_pin["inode"])
                or held.st_size > GIT_METADATA_LIMIT_BYTES
            ):
                raise ProcessError("dispatch guard identity changed before execution")
            guard_bytes = bytearray()
            while True:
                chunk = os.read(descriptor, 65_536)
                if not chunk:
                    break
                guard_bytes.extend(chunk)
                if len(guard_bytes) > GIT_METADATA_LIMIT_BYTES:
                    raise ProcessError("dispatch guard exceeds its byte bound")
            guard_hash = _sha256_bytes(bytes(guard_bytes))
            if guard_hash != guard_pin["content_hash"]:
                raise ProcessError("dispatch guard content changed while pinned")

            pinned_descriptors: dict[str, int] = {}
            snapshot_bytes: dict[str, bytes] = {}

            def snapshot(path: str, data: bytes) -> int:
                if path in pinned_descriptors:
                    if snapshot_bytes[path] != data:
                        raise ProcessError(
                            "dispatch guard input path has conflicting snapshots"
                        )
                    return pinned_descriptors[path]
                handle = tempfile.TemporaryFile(
                    mode="w+b",
                    dir=self.store.root_path,
                )
                snapshots.append(handle)
                if handle.write(data) != len(data):
                    raise ProcessError("dispatch guard input snapshot was incomplete")
                handle.flush()
                os.fsync(handle.fileno())
                os.fchmod(handle.fileno(), 0o400)
                handle.seek(0)
                pinned_descriptors[path] = handle.fileno()
                snapshot_bytes[path] = data
                return handle.fileno()

            guard_descriptor = snapshot(str(guard_path), bytes(guard_bytes))
            for index, pin in enumerate(pinned_artifacts):
                if not isinstance(pin, dict) or not isinstance(pin.get("path"), str):
                    raise ProcessError("dispatch guard input pin is malformed")
                snapshot(
                    pin["path"],
                    _read_admission_bytes(
                        pin,
                        label=f"dispatch guard input {index}",
                    ),
                )
            machine = scheduler_snapshot["machine"]
            guard_environment = {
                "HOME": str(self.store.root_path),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": "/usr/bin:/bin",
                "FORGE_GUARD_PYTHON": runtime_pins["python"]["path"],
                "FORGE_GUARD_GIT": runtime_pins["git"]["path"],
                "FORGE_GUARD_PINNED_CANONICAL_PATH": str(guard_path),
                "FORGE_GUARD_WIP_MEASURED": str(scheduler_snapshot["measured_wip"]),
                "FORGE_GUARD_CORE_COUNT": str(machine["core_count"]),
                "FORGE_GUARD_LOAD_1": str(machine["load_1"]),
                "FORGE_GUARD_FREE_SWAP_GB": str(machine["free_swap_gb"]),
                "FORGE_GUARD_CPU_IDLE_PCT": str(machine["cpu_idle_pct"]),
                "FORGE_GUARD_MEMORY_FREE_PCT": str(machine["memory_free_pct"]),
                "FORGE_GUARD_PINNED_INPUTS": json.dumps(
                    pinned_descriptors,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
            process = subprocess.Popen(
                ["/bin/bash", f"/dev/fd/{guard_descriptor}", *guard_argv],
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=guard_environment,
                pass_fds=tuple(sorted(pinned_descriptors.values())),
                start_new_session=True,
                shell=False,
            )
            stdout, stderr = _bounded_guard_output(process)
            try:
                stdout_text = stdout.decode("utf-8", errors="strict")
                stderr_text = stderr.decode("utf-8", errors="strict")
            except UnicodeDecodeError as error:
                raise ProcessError("dispatch guard output is not UTF-8") from error
            if process.returncode != 0:
                detail = redact_text((stdout_text + stderr_text).strip())
                detail_bytes = detail.encode("utf-8", errors="replace")
                if len(detail_bytes) > MAX_GUARD_ERROR_BYTES:
                    detail = detail_bytes[:MAX_GUARD_ERROR_BYTES].decode(
                        "utf-8", errors="replace"
                    )
                raise StateError(detail or "dispatch guard rejected the launch")
            if stderr_text or not stdout_text.endswith("\n") or stdout_text.count("\n") != 1:
                raise ProcessError("dispatch guard success output is malformed")
            line = stdout_text[:-1]
            if not line.startswith("ADMIT:"):
                raise ProcessError("dispatch guard did not return an ADMIT receipt")
            try:
                result = json.loads(line[len("ADMIT:"):])
            except json.JSONDecodeError as error:
                raise ProcessError("dispatch guard ADMIT receipt is malformed") from error
            argv_hash = _sha256_bytes(
                json.dumps(
                    guard_argv,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            if (
                not isinstance(result, dict)
                or result.get("decision") != "ADMIT"
                or result.get("version") != 1
                or result.get("guard_sha256") != guard_hash
                or result.get("argv_sha256") != argv_hash
            ):
                raise ProcessError("dispatch guard receipt is not bound to this execution")
            rebound = os.stat(guard_path, follow_symlinks=False)
            if (held.st_dev, held.st_ino, held.st_uid) != (
                rebound.st_dev,
                rebound.st_ino,
                rebound.st_uid,
            ):
                raise ProcessError("dispatch guard pathname changed during execution")
            return {
                "guard": {
                    "path": str(guard_path),
                    "device": held.st_dev,
                    "inode": held.st_ino,
                    "owner": held.st_uid,
                    "sha256": guard_hash,
                },
                "argv": list(guard_argv),
                "argv_sha256": argv_hash,
                "stdout": stdout_text,
                "stderr": stderr_text,
                "stdout_sha256": _sha256_bytes(stdout),
                "stderr_sha256": _sha256_bytes(stderr),
                "result": result,
                "result_sha256": _canonical_evidence(result),
                "runtime": runtime_pins,
            }
        except OSError as error:
            raise ProcessError("dispatch guard could not be executed safely") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            for handle in snapshots:
                handle.close()

    @staticmethod
    def _active_reservations(
        active_lanes: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        reservations: list[dict[str, Any]] = []
        for active in active_lanes:
            reservation = {
                name: active.get(name)
                for name in ("lane", "weight", "writer", "files", "repo_id")
            }
            if (
                not isinstance(reservation, dict)
                or set(reservation) != {"lane", "weight", "writer", "files", "repo_id"}
                or reservation["lane"] not in {
                    "review",
                    "feature",
                    "integration",
                    "recovery",
                }
                or reservation["weight"] not in {"LIGHT", "HEAVY"}
                or not isinstance(reservation["files"], list)
                or len(reservation["files"]) != len(set(reservation["files"]))
                or any(
                    not isinstance(path, str) or not path or "\x00" in path
                    for path in reservation["files"]
                )
                or (
                    reservation["writer"] is not None
                    and (
                        not isinstance(reservation["writer"], str)
                        or not reservation["writer"]
                    )
                )
                or not isinstance(reservation["repo_id"], str)
                or len(reservation["repo_id"]) != 64
            ):
                raise StateError("an active lane reservation is malformed")
            reservations.append(dict(reservation))
        return reservations

    def _resume_admission_evidence(
        self,
        payload: dict[str, Any],
        *,
        job_id: str,
        attempt_id: str,
        parent_attempt_id: str,
        parent_revision: int,
        parent_admission_hash: str,
        model_override: str | None,
        attempt_context: dict[str, Any],
        lane_reservation: dict[str, Any],
        active_reservations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Re-derive facts, rerun the guard, and reacquire one reservation."""

        from .worker import WorkerError, _payload as validate_worker_payload

        context_payload = copy.deepcopy(payload)
        context_payload["prompt"] = attempt_context["prompt"]
        context_payload["baseline"] = attempt_context["baseline"]
        try:
            validate_worker_payload(
                context_payload,
                allow_evolved_review_controls=bool(
                    attempt_context["review_controls"]
                ),
            )
        except WorkerError as error:
            raise StateError("stored admission evidence is invalid") from error

        active = self._active_reservations(active_reservations)
        original = payload["admission_evidence"]
        own = {
            name: lane_reservation[name]
            for name in ("lane", "weight", "writer", "files", "repo_id")
        }
        if len(active) >= 3:
            raise StateError("LANE_CAP: maximum three active lanes")
        if own["weight"] == "HEAVY" and any(
            item["weight"] == "HEAVY" for item in active
        ):
            raise StateError("HEAVY_LEASE: a HEAVY lane is already active")
        same_repo_files = {
            path
            for item in active
            if item["repo_id"] == own["repo_id"]
            for path in item["files"]
        }
        if set(own["files"]) & same_repo_files:
            raise StateError("FILE_OVERLAP: declared file is already reserved")
        if own["writer"] is not None and any(
            item["writer"] == own["writer"] for item in active
        ):
            raise StateError("DUPLICATE_WRITER: worktree already has an active writer")
        machine = self._admission_machine_probe()
        if (
            not isinstance(machine, dict)
            or set(machine)
            != {
                "core_count",
                "load_1",
                "free_swap_gb",
                "cpu_idle_pct",
                "memory_free_pct",
            }
            or isinstance(machine["core_count"], bool)
            or not isinstance(machine["core_count"], int)
            or machine["core_count"] <= 0
            or any(
                isinstance(machine[name], bool)
                or not isinstance(machine[name], (int, float))
                or not math.isfinite(float(machine[name]))
                or float(machine[name]) < 0
                for name in (
                    "load_1",
                    "free_swap_gb",
                    "cpu_idle_pct",
                    "memory_free_pct",
                )
            )
            or float(machine["cpu_idle_pct"]) > 100
            or float(machine["memory_free_pct"]) > 100
        ):
            raise StateError("resume machine admission snapshot is malformed")

        baseline = attempt_context["baseline"]
        identity = validate_launch(
            payload["cwd"],
            self.launch_workspace,
            role=payload["role"],
            expected_head=baseline["head"],
            expected_status_fingerprint=baseline["status_fingerprint"],
            expected_tracked_content_fingerprint=baseline[
                "tracked_content_fingerprint"
            ],
            prompt=attempt_context["prompt"],
            allow_dirty_recovery=payload["role"] == "recovery",
            allow_dirty_review=payload["role"] == "review",
            allow_dirty_build=payload["role"] == "build",
        )
        dirty_paths = _git_dirty_paths(identity.top_level)
        if list(dirty_paths) != attempt_context["dirty_files"]:
            raise BoundaryError("resume dirty paths changed after refreeze")
        declaration = copy.deepcopy(original["declaration"])
        declaration["files"] = list(own["files"])
        preserved_paths = sorted(declaration.get("preserved_paths", []))
        if payload["role"] == "recovery":
            if preserved_paths != list(dirty_paths):
                raise StateError(
                    "recovery dirty paths no longer match exact preserved paths"
                )
        elif payload["role"] == "review":
            allowed = {
                str(Path(pin["path"]).relative_to(identity.top_level))
                for pin in _review_control_pin_values(
                    attempt_context["review_controls"]
                )
            }
            if not set(dirty_paths).issubset(allowed):
                raise BoundaryError("review resume drift exceeds pinned controls")
            if attempt_context["review_controls"]:
                declaration["review_controls"] = (
                    _review_control_declaration_from_pins(
                        attempt_context["review_controls"],
                        root=identity.top_level,
                    )
                )
            else:
                declaration.pop("review_controls", None)
        elif not set(dirty_paths).issubset(own["files"]):
            raise BoundaryError("product resume drift exceeds its exact file set")
        measured_wip = _measured_wip(identity.top_level)
        same_repo_files = sorted(same_repo_files)
        scheduler_snapshot = {
            "active_lanes": len(active),
            "heavy_leases": sum(item["weight"] == "HEAVY" for item in active),
            "active_writers": sorted(
                item["writer"] for item in active if item["writer"] is not None
            ),
            "parallel_files": same_repo_files,
            "machine": machine,
            "measured_wip": measured_wip,
            "repository": {
                "top_level": str(identity.top_level),
                "head": identity.head,
                "status_fingerprint": identity.status_fingerprint,
                "clean": not dirty_paths,
                "dirty_paths": list(dirty_paths),
            },
            "active_job_ids": sorted(
                item["job_id"]
                for item in active_reservations
                if isinstance(item.get("job_id"), str)
            ),
        }
        scheduler_snapshot["sha256"] = _canonical_evidence(scheduler_snapshot)

        original_argv = original["argv"]
        if len(original_argv) % 2:
            raise StateError("stored guard argv is malformed")
        dynamic = {
            "--active-lanes": str(scheduler_snapshot["active_lanes"]),
            "--heavy-leases": str(scheduler_snapshot["heavy_leases"]),
            "--worktree": str(identity.top_level),
            "--clean-tree": "true" if not dirty_paths else "false",
            "--unmerged-branches": str(measured_wip),
            "--work-log": str(self.store.runs_path / job_id / attempt_id / "work.log"),
            "--report": str(self.store.runs_path / job_id / attempt_id / "report.txt"),
            "--admission-action": "resume",
        }
        if attempt_context["review_controls"]:
            dynamic["--plan-file"] = attempt_context["review_controls"]["plan"]["path"]
            dynamic["--act2-review-log"] = attempt_context["review_controls"][
                "review_log"
            ]["path"]
        repeat = {
            "--active-writer-worktree",
            "--parallel-file",
            "--file",
            "--review-domain-doc",
        }
        guard_argv: list[str] = []
        seen_dynamic: set[str] = set()
        for index in range(0, len(original_argv), 2):
            flag, value = original_argv[index:index + 2]
            if not isinstance(flag, str) or not flag.startswith("--"):
                raise StateError("stored guard argv is not structured")
            if flag in repeat:
                continue
            if flag in dynamic:
                value = dynamic[flag]
                seen_dynamic.add(flag)
            guard_argv.extend((flag, value))
        if seen_dynamic != set(dynamic):
            raise StateError("stored guard argv lacks dynamic bindings")
        for writer in scheduler_snapshot["active_writers"]:
            guard_argv.extend(("--active-writer-worktree", writer))
        for path in scheduler_snapshot["parallel_files"]:
            guard_argv.extend(("--parallel-file", path))
        for path in own["files"]:
            guard_argv.extend(("--file", path))
        for pin in attempt_context["review_controls"].get("domain_docs", []):
            guard_argv.extend(("--review-domain-doc", pin["path"]))
        guard_evidence = self._run_dispatch_guard(
            guard_argv,
            scheduler_snapshot=scheduler_snapshot,
            cwd=identity.top_level,
            pinned_artifacts=(
                *tuple(original["controls"].values()),
                *(
                    _review_control_pin_values(
                        attempt_context["review_controls"]
                    )
                    if attempt_context["review_controls"]
                    else ()
                ),
                *tuple(original["manifest_entries"]),
                *(
                    ()
                    if original["predecessor"] is None
                    else tuple(original["predecessor"].values())
                ),
            ),
        )

        try:
            validate_worker_payload(
                context_payload,
                allow_evolved_review_controls=bool(
                    attempt_context["review_controls"]
                ),
            )
        except WorkerError as error:
            raise StateError("admission artifacts changed during resume guard") from error
        identity_after = self._identity(payload["cwd"])
        if (
            _repository_path_pin(identity_after)
            != {name: baseline[name] for name in REPOSITORY_PATH_PIN_FIELDS}
            or identity_after.head != baseline["head"]
            or identity_after.status_fingerprint != baseline["status_fingerprint"]
            or identity_after.tracked_content_fingerprint
            != baseline["tracked_content_fingerprint"]
        ):
            raise BoundaryError("repository changed during resume admission")
        evidence = {
            "version": 1,
            "declaration": json.loads(canonical_json(declaration)),
            "lane": original["lane"],
            "product_lane": original["product_lane"],
            "executor": "claude",
            "driver": "codex",
            "integration_authority": "codex",
            "scheduler_snapshot": scheduler_snapshot,
            "controls": copy.deepcopy(original["controls"]),
            "review_controls": copy.deepcopy(attempt_context["review_controls"]),
            "manifest_entries": copy.deepcopy(original["manifest_entries"]),
            "predecessor": copy.deepcopy(original["predecessor"]),
            "predecessor_receipts": copy.deepcopy(
                original["predecessor_receipts"]
            ),
            "act2_review": copy.deepcopy(original["act2_review"]),
            "reservation": copy.deepcopy(own),
            "resume": {
                "parent_attempt_id": parent_attempt_id,
                "parent_revision": parent_revision,
                "attempt_id": attempt_id,
                "model_override": model_override,
                "original_admission_sha256": original["sha256"],
                "parent_admission_sha256": parent_admission_hash,
                "scheduler_sha256": scheduler_snapshot["sha256"],
                "prompt_sha256": attempt_context["prompt_sha256"],
                "context_request_sha256": attempt_context["request_hash"],
            },
            **guard_evidence,
        }
        evidence["sha256"] = _canonical_evidence(evidence)
        resume_payload = copy.deepcopy(payload)
        resume_payload["prompt"] = attempt_context["prompt"]
        resume_payload["baseline"] = attempt_context["baseline"]
        resume_payload["admission_evidence"] = evidence
        try:
            validate_worker_payload(resume_payload)
        except WorkerError as error:
            raise StateError("fresh resume admission evidence is invalid") from error
        return evidence

    def _admitted_payload(
        self,
        arguments: dict[str, Any],
        *,
        job_id: str,
        attempt_id: str,
        active_reservations: list[dict[str, Any]],
        request_hash: str,
    ) -> dict[str, Any]:
        declaration = arguments.get("admission")
        if not isinstance(declaration, dict):
            raise StateError("structured admission declaration is required")
        prompt = arguments["prompt"]
        cwd = arguments.get("cwd", str(self.launch_workspace))
        role = arguments.get("role", DEFAULT_ROLE)
        model = normalize_model_identifier(arguments.get("model", DEFAULT_MODEL))
        effort = arguments.get("effort", DEFAULT_EFFORT)
        budget = dict(arguments.get("budget", DEFAULT_BUDGET))
        budget["max_cost_usd"] = normalize_budget_usd(budget["max_cost_usd"])
        lane_by_role = {"review": "review", "build": "feature", "recovery": "recovery"}
        if role not in lane_by_role:
            raise StateError("job role is invalid")
        lane = lane_by_role[role]
        product = role in {"build", "recovery"}
        weight = declaration.get("weight")
        proof_scope = declaration.get("proof_scope")
        profile = declaration.get("execution_profile")
        proof_argv = declaration.get("proof_argv")
        declared_files = declaration.get("files")
        if (
            weight not in {"LIGHT", "HEAVY"}
            or proof_scope not in {"targeted", "broad"}
            or profile not in {"sprint", "assured"}
            or not isinstance(proof_argv, list)
            or not proof_argv
            or len(proof_argv) > 128
            or any(
                not isinstance(item, str)
                or not item
                or "\x00" in item
                or len(item.encode("utf-8")) > 4096
                for item in proof_argv
            )
            or not isinstance(declared_files, list)
            or len(declared_files) > 256
        ):
            raise StateError("admission declaration is malformed")

        identity = self._identity(cwd)
        if _repo_id(identity) != self._launch_repo_id:
            raise BoundaryError("admission repository does not match the bridge workspace")
        exact_files = [
            _exact_declared_path(identity.top_level, item)
            for item in declared_files
        ]
        if len(exact_files) != len(set(exact_files)):
            raise StateError("declared files contain duplicates")
        if any(path == ".ai/HANDOFF.md" or path.endswith("/.ai/HANDOFF.md") for path in exact_files):
            raise StateError("HANDOFF is a Codex driver-owned control artifact")
        if not product and exact_files:
            raise StateError("review admission cannot reserve product files")
        review_controls_pins: dict[str, Any] = {}
        review_controls = declaration.get("review_controls")
        if role == "review" and review_controls is not None:
            review_controls_pins = _pin_review_controls(
                review_controls,
                root=identity.top_level,
                label="review control",
            )
            declaration["review_controls"] = _review_control_declaration_from_pins(
                review_controls_pins,
                root=identity.top_level,
            )
        elif review_controls is not None:
            raise StateError("review controls are valid only for review")
        validated_identity = validate_launch(
            identity.top_level,
            self.launch_workspace,
            role=role,
            expected_head=identity.head,
            expected_status_fingerprint=identity.status_fingerprint,
            expected_tracked_content_fingerprint=identity.tracked_content_fingerprint,
            prompt=prompt,
            allow_dirty_recovery=role == "recovery",
            allow_dirty_review=role == "review" and bool(review_controls_pins),
        )
        if validated_identity != identity:
            raise BoundaryError("repository identity changed during launch validation")
        dirty_paths = _git_dirty_paths(identity.top_level)
        if role == "review" and dirty_paths:
            allowed_review_paths = {
                str(Path(pin["path"]).relative_to(identity.top_level))
                for pin in _review_control_pin_values(review_controls_pins)
            }
            if not set(dirty_paths).issubset(allowed_review_paths):
                raise BoundaryError(
                    "dirty review launch is limited to pinned review controls"
                )
        elif role != "recovery" and dirty_paths:
            raise BoundaryError(f"dirty {role} launch is not allowed")
        preserved = declaration.get("preserved_paths", [])
        if not isinstance(preserved, list):
            raise StateError("recovery preserved paths are malformed")
        exact_preserved = [
            _exact_declared_path(identity.top_level, item, allow_missing=False)
            for item in preserved
        ]
        if role == "recovery":
            if tuple(sorted(exact_preserved)) != dirty_paths:
                raise StateError("recovery dirty paths do not match exact preserved paths")
            if not set(exact_preserved).issubset(exact_files):
                raise StateError("recovery preserved paths must be in the declared file set")
        elif exact_preserved:
            raise StateError("preserved paths are valid only for recovery")

        if product:
            base_sha = declaration.get("base_sha")
            if base_sha != identity.head:
                raise BoundaryError("declared immutable base does not match canonical HEAD")
            if declaration.get("verdict") != "APPROVED":
                raise StateError("product admission requires an APPROVED verdict")
            backlog_ids = declaration.get("backlog_ids")
            controls = declaration.get("controls")
            oracle = declaration.get("oracle")
            if (
                not exact_files
                or not isinstance(backlog_ids, list)
                or not backlog_ids
                or not isinstance(controls, dict)
                or not isinstance(oracle, dict)
                or not isinstance(oracle.get("exact"), bool)
            ):
                raise StateError("product admission declaration is incomplete")
        else:
            forbidden_product_fields = {
                "base_sha", "verdict", "backlog_ids", "controls", "oracle",
                "predecessor", "preserved_paths",
            }
            if forbidden_product_fields & set(declaration):
                raise StateError("review admission contains product-only fields")

        baseline = {
            "head": identity.head,
            "status_fingerprint": identity.status_fingerprint,
            "tracked_content_fingerprint": identity.tracked_content_fingerprint,
            **_repository_path_pin(identity),
        }
        reservations = self._active_reservations(active_reservations)
        if len(reservations) >= 3:
            raise StateError("LANE_CAP: maximum three active lanes")
        if weight == "HEAVY" and any(item["weight"] == "HEAVY" for item in reservations):
            raise StateError("HEAVY_LEASE: a HEAVY lane is already active")
        writer = str(identity.top_level) if product else None
        same_repo_files = {
            path
            for item in reservations
            if item["repo_id"] == self._launch_repo_id
            for path in item["files"]
        }
        if set(exact_files) & same_repo_files:
            raise StateError("FILE_OVERLAP: declared file is already reserved")
        if writer is not None and any(item["writer"] == writer for item in reservations):
            raise StateError("DUPLICATE_WRITER: worktree already has an active writer")

        controls_pins: dict[str, Any] = {}
        manifest_entry_pins: list[dict[str, Any]] = []
        predecessor_pins: dict[str, Any] | None = None
        predecessor_receipts: dict[str, Any] | None = None
        act2_review: dict[str, Any] | None = None
        if product:
            controls = declaration["controls"]
            base_controls = {
                "plan", "review_log", "execution_contract", "project_overlay"
            }
            assured_controls = {
                "engine_contract",
                "routing_policy",
                "liveness_fixture",
                "watchdog_fixture",
                "watchdog_tests_fixture",
            }
            required_controls = (
                base_controls | assured_controls | {"freeze_manifest"}
                if profile == "assured"
                else base_controls
            )
            if set(controls) != required_controls:
                raise StateError("governing controls are malformed")
            controls_pins = {
                name: _admission_artifact_pin(
                    value,
                    label=f"admission control {name}",
                    allowed_root=identity.top_level,
                )
                for name, value in controls.items()
            }
            governing_pins = [controls_pins[name] for name in sorted(required_controls)]
            if (
                len({pin["path"] for pin in governing_pins}) != len(governing_pins)
                or len({(pin["device"], pin["inode"]) for pin in governing_pins})
                != len(governing_pins)
            ):
                raise StateError("governing control roles must use distinct artifacts")
            act2 = _read_admission_json(
                controls_pins["review_log"],
                label="Act-2 review log",
            )
            if (
                set(act2)
                != {
                    "version",
                    "unit_id",
                    "reviewer_family",
                    "verdict",
                    "receipt_id",
                }
                or act2["version"] != 1
                or act2["unit_id"] not in declaration["backlog_ids"]
                or act2["verdict"] != declaration["verdict"]
                or not isinstance(act2["receipt_id"], str)
                or not act2["receipt_id"]
                or not isinstance(act2["reviewer_family"], str)
                or _provider_family(act2["reviewer_family"]) != "claude"
            ):
                raise StateError("Act-2 review log does not match the admission declaration")
            act2_review = {
                **act2,
                "artifact_sha256": controls_pins["review_log"]["sha256"],
            }
            control_relatives = {
                str(Path(pin["path"]).relative_to(identity.top_level))
                for pin in controls_pins.values()
            }
            if set(exact_files) & control_relatives:
                raise StateError("Claude executor cannot reserve Codex driver-owned controls")
            oracle = declaration["oracle"]
            if oracle["exact"]:
                if set(oracle) != {"exact", "capability_receipt"}:
                    raise StateError("exact oracle capability declaration is incomplete")
                controls_pins["capability_receipt"] = _admission_artifact_pin(
                    oracle["capability_receipt"],
                    label="oracle capability receipt",
                )
                controls_pins["capability_target"] = (
                    _derive_oracle_capability_target_pin(
                        controls_pins["capability_receipt"],
                        controls_pins["execution_contract"],
                        identity.top_level,
                    )
                )
                if (
                    controls_pins["capability_target"]["path"]
                    == controls_pins["capability_receipt"]["path"]
                    or (
                        controls_pins["capability_target"]["device"],
                        controls_pins["capability_target"]["inode"],
                    )
                    == (
                        controls_pins["capability_receipt"]["device"],
                        controls_pins["capability_receipt"]["inode"],
                    )
                ):
                    raise StateError(
                        "oracle capability receipt and target must be distinct"
                    )
            elif set(oracle) != {"exact"}:
                raise StateError("non-exact oracle declaration has extra fields")
            if profile == "assured":
                manifest_entry_pins = _derive_freeze_manifest_entry_pins(
                    controls_pins["freeze_manifest"]
                )
            predecessor = declaration.get("predecessor")
            if predecessor is not None:
                if not isinstance(predecessor, dict):
                    raise StateError("predecessor declaration is malformed")
                predecessor_pins = {
                    "report": _admission_artifact_pin(
                        predecessor.get("report"),
                        label="predecessor report",
                    ),
                    "consumption": _admission_artifact_pin(
                        predecessor.get("consumption_receipt"),
                        label="Codex report-consumption receipt",
                    ),
                }
                review = predecessor.get("review")
                if profile == "assured" and not isinstance(review, dict):
                    raise StateError("assured successor requires predecessor review evidence")
                if isinstance(review, dict):
                    predecessor_pins["review"] = _admission_artifact_pin(
                        review.get("artifact"),
                        label="predecessor review",
                    )
                    if (
                        review.get("verdict") != "APPROVED"
                        or review.get("reviewer_family", "").casefold()
                        == predecessor.get("builder_family", "").casefold()
                    ):
                        raise StateError("predecessor review is not approved cross-family evidence")
                    review_document = _read_admission_json(
                        predecessor_pins["review"],
                        label="predecessor review artifact",
                    )
                    expected_review = {
                        "version": 1,
                        "predecessor": predecessor.get("unit_id"),
                        "builder": predecessor.get("builder_family"),
                        "reviewer": review.get("reviewer_family"),
                        "thread": review.get("thread"),
                        "verdict": review.get("verdict"),
                        "receipt_id": review.get("receipt_id"),
                    }
                    if review_document != expected_review:
                        raise StateError(
                            "predecessor review artifact does not match its declaration"
                        )
                report_pin = predecessor_pins["report"]
                if os.stat(report_pin["path"], follow_symlinks=False).st_size <= 0:
                    raise StateError("predecessor report is empty")
                consumption_document = _read_admission_json(
                    predecessor_pins["consumption"],
                    label="Codex report-consumption receipt",
                )
                expected_consumption = {
                    "version": 1,
                    "predecessor": predecessor.get("unit_id"),
                    "consumed_by": consumption_document.get("consumed_by"),
                    "report": report_pin["path"],
                    "report_sha256": report_pin["sha256"],
                    "receipt_id": predecessor.get("report_receipt_id"),
                }
                if (
                    consumption_document != expected_consumption
                    or _provider_family(consumption_document.get("consumed_by"))
                    != "codex"
                ):
                    raise StateError(
                        "Codex report-consumption receipt does not match the report"
                    )
                report_receipt = {
                    **consumption_document,
                    "source_sha256": predecessor_pins["consumption"]["sha256"],
                }
                report_receipt["sha256"] = _canonical_evidence(report_receipt)
                predecessor_receipts = {
                    "report": report_receipt,
                    "review": (
                        None
                        if review is None
                        else {
                            **review_document,
                            "artifact_sha256": predecessor_pins["review"]["sha256"],
                        }
                    ),
                }

        machine = self._admission_machine_probe()
        if (
            not isinstance(machine, dict)
            or set(machine)
            != {
                "core_count", "load_1", "free_swap_gb",
                "cpu_idle_pct", "memory_free_pct",
            }
            or isinstance(machine["core_count"], bool)
            or not isinstance(machine["core_count"], int)
            or machine["core_count"] <= 0
            or any(
                isinstance(machine[name], bool)
                or not isinstance(machine[name], (int, float))
                or not math.isfinite(float(machine[name]))
                or float(machine[name]) < 0
                for name in (
                    "load_1", "free_swap_gb", "cpu_idle_pct", "memory_free_pct"
                )
            )
            or float(machine["cpu_idle_pct"]) > 100
            or float(machine["memory_free_pct"]) > 100
        ):
            raise StateError("machine admission snapshot is malformed")
        measured_wip = _measured_wip(identity.top_level)
        scheduler_snapshot = {
            "active_lanes": len(reservations),
            "heavy_leases": sum(item["weight"] == "HEAVY" for item in reservations),
            "active_writers": sorted(
                item["writer"] for item in reservations if item["writer"] is not None
            ),
            "parallel_files": sorted(same_repo_files),
            "machine": machine,
            "measured_wip": measured_wip,
            "repository": {
                "top_level": str(identity.top_level),
                "head": identity.head,
                "status_fingerprint": identity.status_fingerprint,
                "clean": not dirty_paths,
                "dirty_paths": list(dirty_paths),
            },
            "active_job_ids": sorted(
                item["job_id"]
                for item in active_reservations
                if isinstance(item.get("job_id"), str)
            ),
        }
        scheduler_snapshot["sha256"] = _canonical_evidence(scheduler_snapshot)

        proof_command = shlex.join(proof_argv)
        run_root = self.store.runs_path / job_id / attempt_id
        guard_argv = [
            "--active-lanes", str(scheduler_snapshot["active_lanes"]),
            "--heavy-leases", str(scheduler_snapshot["heavy_leases"]),
            "--weight", weight,
            "--lane-type", lane,
            "--mode", "recovery" if role == "recovery" else "normal",
            "--worktree", str(identity.top_level),
            "--clean-tree", "true" if not dirty_paths else "false",
            "--builder", "claude-external",
            "--executor-policy", "claude-only" if product else "any",
            "--proof-scope", proof_scope,
            "--execution-profile", profile,
            "--admission-action", "launch",
            "--owns-heavy-lease", "false",
            "--proof-command", proof_command,
            "--work-log", str(run_root / "work.log"),
            "--report", str(run_root / "report.txt"),
            "--predecessor", "none",
            "--instruction", prompt,
            "--unmerged-branches", str(measured_wip),
        ]
        if review_controls_pins:
            guard_argv.extend(
                [
                    "--plan-file",
                    review_controls_pins["plan"]["path"],
                    "--act2-review-log",
                    review_controls_pins["review_log"]["path"],
                ]
            )
            for pin in review_controls_pins.get("domain_docs", []):
                guard_argv.extend(["--review-domain-doc", pin["path"]])
        if product:
            guard_argv.extend([
                "--verdict", "APPROVED",
                "--base-sha", identity.head,
                "--plan-file", controls_pins["plan"]["path"],
                "--act2-review-log", controls_pins["review_log"]["path"],
                "--execution-contract", controls_pins["execution_contract"]["path"],
                "--project-overlay", controls_pins["project_overlay"]["path"],
                "--oracle-exact", "true" if declaration["oracle"]["exact"] else "false",
            ])
            if profile == "assured":
                guard_argv.extend([
                    "--engine-contract", controls_pins["engine_contract"]["path"],
                    "--routing-policy", controls_pins["routing_policy"]["path"],
                    "--liveness-fixture", controls_pins["liveness_fixture"]["path"],
                    "--watchdog-fixture", controls_pins["watchdog_fixture"]["path"],
                    "--watchdog-tests-fixture",
                    controls_pins["watchdog_tests_fixture"]["path"],
                    "--freeze-manifest", controls_pins["freeze_manifest"]["path"],
                    "--freeze-manifest-sha256", controls_pins["freeze_manifest"]["sha256"],
                ])
            if declaration["oracle"]["exact"]:
                guard_argv.extend([
                    "--capability-receipt", controls_pins["capability_receipt"]["path"],
                    "--capability-receipt-sha256", controls_pins["capability_receipt"]["sha256"],
                ])
            for backlog_id in declaration["backlog_ids"]:
                guard_argv.extend(["--backlog-id", backlog_id])
            for path in exact_files:
                guard_argv.extend(["--file", path])
            predecessor = declaration.get("predecessor")
            if predecessor is not None:
                guard_argv[guard_argv.index("none")] = predecessor["unit_id"]
                guard_argv.extend([
                    "--predecessor-builder", predecessor["builder_family"],
                    "--report-receipt",
                    "predecessor={unit};consumed_by={consumer};report={report};report_sha256={report_hash};receipt_id={receipt}".format(
                        unit=predecessor_receipts["report"]["predecessor"],
                        consumer=predecessor_receipts["report"]["consumed_by"],
                        report=predecessor_receipts["report"]["report"],
                        report_hash=predecessor_receipts["report"]["report_sha256"],
                        receipt=predecessor_receipts["report"]["receipt_id"],
                    ),
                ])
                review = predecessor.get("review")
                if review is not None:
                    guard_argv.extend([
                        "--review-receipt",
                        "predecessor={unit};builder={builder};reviewer={reviewer};thread={thread};verdict={verdict};receipt_id={receipt}".format(
                            unit=predecessor["unit_id"],
                            builder=predecessor["builder_family"],
                            reviewer=review["reviewer_family"],
                            thread=review["thread"],
                            verdict=review["verdict"],
                            receipt=review["receipt_id"],
                        ),
                    ])
        for active_writer in scheduler_snapshot["active_writers"]:
            guard_argv.extend(["--active-writer-worktree", active_writer])
        for path in scheduler_snapshot["parallel_files"]:
            guard_argv.extend(["--parallel-file", path])

        guard_evidence = self._run_dispatch_guard(
            guard_argv,
            scheduler_snapshot=scheduler_snapshot,
            cwd=identity.top_level,
            pinned_artifacts=(
                *tuple(controls_pins.values()),
                *(
                    _review_control_pin_values(review_controls_pins)
                    if review_controls_pins
                    else ()
                ),
                *tuple(manifest_entry_pins),
                *(
                    ()
                    if predecessor_pins is None
                    else tuple(predecessor_pins.values())
                ),
            ),
        )
        if controls_pins:
            rebound_controls = {
                name: _admission_artifact_pin(
                    declaration["controls"][name],
                    label=f"admission control {name}",
                    allowed_root=identity.top_level,
                )
                for name in declaration.get("controls", {})
            }
            if declaration["oracle"]["exact"]:
                rebound_controls["capability_receipt"] = _admission_artifact_pin(
                    declaration["oracle"]["capability_receipt"],
                    label="admission control capability_receipt",
                )
                rebound_controls["capability_target"] = (
                    _derive_oracle_capability_target_pin(
                        rebound_controls["capability_receipt"],
                        rebound_controls["execution_contract"],
                        identity.top_level,
                    )
                )
            if rebound_controls != controls_pins:
                raise StateError("control artifacts changed during guard execution")
            if profile == "assured" and (
                _derive_freeze_manifest_entry_pins(
                    rebound_controls["freeze_manifest"]
                )
                != manifest_entry_pins
            ):
                raise StateError(
                    "assured freeze manifest entries changed during guard execution"
                )
        if review_controls_pins:
            rebound_review_controls = _pin_review_controls(
                declaration["review_controls"],
                root=identity.top_level,
                label="review control",
            )
            if rebound_review_controls != review_controls_pins:
                raise StateError("review controls changed during guard execution")
        if predecessor_pins:
            rebound_predecessor = {
                "report": _admission_artifact_pin(
                    declaration["predecessor"]["report"],
                    label="predecessor report",
                ),
                "consumption": _admission_artifact_pin(
                    declaration["predecessor"]["consumption_receipt"],
                    label="Codex report-consumption receipt",
                ),
            }
            if "review" in predecessor_pins:
                rebound_predecessor["review"] = _admission_artifact_pin(
                    declaration["predecessor"]["review"]["artifact"],
                    label="predecessor review",
                )
            if rebound_predecessor != predecessor_pins:
                raise StateError("predecessor artifacts changed during guard execution")
        identity_after = self._identity(cwd)
        if (
            _repository_path_pin(identity_after) != {
                name: baseline[name] for name in REPOSITORY_PATH_PIN_FIELDS
            }
            or identity_after.head != identity.head
            or identity_after.status_fingerprint != identity.status_fingerprint
            or identity_after.tracked_content_fingerprint
            != identity.tracked_content_fingerprint
        ):
            raise BoundaryError("repository changed during admission")

        reservation = {
            "lane": lane,
            "weight": weight,
            "writer": writer,
            "files": exact_files,
            "repo_id": self._launch_repo_id,
        }
        evidence = {
            "version": 1,
            "declaration": json.loads(canonical_json(declaration)),
            "lane": lane,
            "product_lane": product,
            "executor": "claude",
            "driver": "codex",
            "integration_authority": "codex",
            "scheduler_snapshot": scheduler_snapshot,
            "controls": controls_pins,
            "review_controls": review_controls_pins,
            "manifest_entries": manifest_entry_pins,
            "predecessor": predecessor_pins,
            "predecessor_receipts": predecessor_receipts,
            "act2_review": act2_review,
            "reservation": reservation,
            "resume": None,
            **guard_evidence,
        }
        evidence["sha256"] = _canonical_evidence(evidence)
        return {
            "prompt": prompt,
            "cwd": str(identity.top_level),
            "role": role,
            "model": model,
            "effort": effort,
            "budget": budget,
            "baseline": baseline,
            "claude_executable": self.claude_executable,
            "admission_evidence": evidence,
            "request_hash": request_hash,
        }

    def _expansion_admission_evidence(
        self,
        payload: dict[str, Any],
        *,
        job_id: str,
        attempt_id: str,
        prompt: str,
        current: dict[str, Any],
        prior_admission: dict[str, Any],
        active_reservations: list[dict[str, Any]],
        prior_files: list[str],
        files: list[str],
    ) -> dict[str, Any]:
        """Re-freeze and guard one same-lane bridge file-set expansion."""

        from .worker import WorkerError, _validate_admission_evidence

        try:
            prior = _validate_admission_evidence(prior_admission)
        except WorkerError as error:
            raise StateError("stored bridge lane admission is invalid") from error
        if (
            payload.get("role") not in {"build", "recovery"}
            or not prior["product_lane"]
            or prior["lane"] != current["lane"]
            or prior["reservation"]["repo_id"] != current["repo_id"]
            or prior["reservation"]["writer"] != current["writer"]
            or prior["reservation"]["weight"] != current["weight"]
            or prior["reservation"]["files"] != prior_files
        ):
            raise StateError("bridge expansion cannot change lane ownership or weight")
        identity = self._identity(payload["cwd"])
        baseline = payload["baseline"]
        if (
            current["repo_id"] != self._launch_repo_id
            or current["base_sha"] != baseline["head"]
            or identity.head != baseline["head"]
            or current["writer"] != str(identity.top_level)
            or _repository_path_pin(identity)
            != {name: baseline[name] for name in REPOSITORY_PATH_PIN_FIELDS}
        ):
            raise BoundaryError("bridge expansion repository identity changed")
        exact_prior = [
            _exact_declared_path(identity.top_level, path)
            for path in prior_files
        ]
        exact_files = [
            _exact_declared_path(identity.top_level, path)
            for path in files
        ]
        if exact_prior != prior_files or exact_files != files:
            raise StateError("bridge expansion paths are not exact")
        if any(
            path == ".ai/HANDOFF.md" or path.endswith("/.ai/HANDOFF.md")
            for path in exact_files
        ):
            raise StateError("HANDOFF is a Codex driver-owned control artifact")
        dirty_paths = list(_git_dirty_paths(identity.top_level))
        if not set(dirty_paths).issubset(exact_files):
            raise StateError("DIRTY_PATH_UNDECLARED: expansion drift exceeds its full set")
        active = self._active_reservations(active_reservations)
        same_repo_files = sorted(
            {
                path
                for item in active
                if item["repo_id"] == current["repo_id"]
                for path in item["files"]
            }
        )
        machine = self._admission_machine_probe()
        if (
            not isinstance(machine, dict)
            or set(machine)
            != {
                "core_count",
                "load_1",
                "free_swap_gb",
                "cpu_idle_pct",
                "memory_free_pct",
            }
            or isinstance(machine["core_count"], bool)
            or not isinstance(machine["core_count"], int)
            or machine["core_count"] <= 0
            or any(
                isinstance(machine[name], bool)
                or not isinstance(machine[name], (int, float))
                or not math.isfinite(float(machine[name]))
                or float(machine[name]) < 0
                for name in (
                    "load_1",
                    "free_swap_gb",
                    "cpu_idle_pct",
                    "memory_free_pct",
                )
            )
            or float(machine["cpu_idle_pct"]) > 100
            or float(machine["memory_free_pct"]) > 100
        ):
            raise StateError("expansion machine admission snapshot is malformed")
        measured_wip = _measured_wip(identity.top_level)
        all_active = [*active, current]
        scheduler_snapshot = {
            "active_lanes": len(all_active),
            "heavy_leases": sum(
                item["weight"] == "HEAVY" for item in all_active
            ),
            "active_writers": sorted(
                item["writer"]
                for item in all_active
                if item["writer"] is not None
            ),
            "parallel_files": same_repo_files,
            "machine": machine,
            "measured_wip": measured_wip,
            "repository": {
                "top_level": str(identity.top_level),
                "head": identity.head,
                "status_fingerprint": identity.status_fingerprint,
                "clean": not dirty_paths,
                "dirty_paths": dirty_paths,
            },
            "active_job_ids": sorted(
                {
                    job_id,
                    *(
                        item["job_id"]
                        for item in active_reservations
                        if isinstance(item.get("job_id"), str)
                    ),
                }
            ),
        }
        scheduler_snapshot["sha256"] = _canonical_evidence(scheduler_snapshot)
        original_argv = prior["argv"]
        if len(original_argv) % 2:
            raise StateError("stored expansion guard argv is malformed")
        dynamic = {
            "--active-lanes": str(scheduler_snapshot["active_lanes"]),
            "--heavy-leases": str(scheduler_snapshot["heavy_leases"]),
            "--worktree": str(identity.top_level),
            "--clean-tree": "true" if not dirty_paths else "false",
            "--unmerged-branches": str(measured_wip),
            "--instruction": prompt,
            "--work-log": str(
                self.store.runs_path / job_id / attempt_id / "work.log"
            ),
            "--report": str(
                self.store.runs_path / job_id / attempt_id / "report.txt"
            ),
            "--admission-action": "expand",
            "--owns-heavy-lease": (
                "true" if current["weight"] == "HEAVY" else "false"
            ),
        }
        repeat = {
            "--active-writer-worktree",
            "--parallel-file",
            "--current-file",
            "--file",
        }
        guard_argv: list[str] = []
        seen_dynamic: set[str] = set()
        for index in range(0, len(original_argv), 2):
            flag, value = original_argv[index : index + 2]
            if not isinstance(flag, str) or not flag.startswith("--"):
                raise StateError("stored expansion guard argv is not structured")
            if flag in repeat:
                continue
            if flag in dynamic:
                value = dynamic[flag]
                seen_dynamic.add(flag)
            guard_argv.extend((flag, value))
        if seen_dynamic != set(dynamic):
            raise StateError("stored guard argv lacks expansion bindings")
        for writer in scheduler_snapshot["active_writers"]:
            guard_argv.extend(("--active-writer-worktree", writer))
        for path in same_repo_files:
            guard_argv.extend(("--parallel-file", path))
        for path in exact_prior:
            guard_argv.extend(("--current-file", path))
        for path in exact_files:
            guard_argv.extend(("--file", path))
        guard_evidence = self._run_dispatch_guard(
            guard_argv,
            scheduler_snapshot=scheduler_snapshot,
            cwd=identity.top_level,
            pinned_artifacts=(
                *tuple(prior["controls"].values()),
                *tuple(prior["manifest_entries"]),
                *(
                    ()
                    if prior["predecessor"] is None
                    else tuple(prior["predecessor"].values())
                ),
            ),
        )
        try:
            _validate_admission_evidence(prior_admission)
        except WorkerError as error:
            raise StateError("expansion controls changed during guard") from error
        identity_after = self._identity(payload["cwd"])
        if (
            _repository_path_pin(identity_after)
            != {name: baseline[name] for name in REPOSITORY_PATH_PIN_FIELDS}
            or identity_after.head != identity.head
            or identity_after.status_fingerprint != identity.status_fingerprint
            or identity_after.tracked_content_fingerprint
            != identity.tracked_content_fingerprint
        ):
            raise BoundaryError("repository changed during expansion admission")
        declaration = copy.deepcopy(prior["declaration"])
        declaration["files"] = list(exact_files)
        reservation = {
            "lane": current["lane"],
            "weight": current["weight"],
            "writer": current["writer"],
            "files": list(exact_files),
            "repo_id": current["repo_id"],
        }
        resume_binding = copy.deepcopy(prior["resume"])
        if resume_binding is not None:
            # Expansion creates a new scheduler snapshot but not a new attempt.
            # Preserve the same-session attempt lineage while binding it to this
            # freshly admitted scheduler state.
            resume_binding["scheduler_sha256"] = scheduler_snapshot["sha256"]
        evidence = {
            "version": 1,
            "declaration": json.loads(canonical_json(declaration)),
            "lane": prior["lane"],
            "product_lane": True,
            "executor": "claude",
            "driver": "codex",
            "integration_authority": "codex",
            "scheduler_snapshot": scheduler_snapshot,
            "controls": copy.deepcopy(prior["controls"]),
            "review_controls": {},
            "manifest_entries": copy.deepcopy(prior["manifest_entries"]),
            "predecessor": copy.deepcopy(prior["predecessor"]),
            "predecessor_receipts": copy.deepcopy(
                prior["predecessor_receipts"]
            ),
            "act2_review": copy.deepcopy(prior["act2_review"]),
            "reservation": reservation,
            "resume": resume_binding,
            **guard_evidence,
        }
        evidence["sha256"] = _canonical_evidence(evidence)
        try:
            _validate_admission_evidence(evidence)
        except WorkerError as error:
            raise StateError("fresh expansion admission evidence is invalid") from error
        return evidence

    def _normalized_start(self, arguments: dict[str, Any]) -> tuple[RepoIdentity, dict[str, Any]]:
        prompt = arguments["prompt"]
        cwd = arguments.get("cwd", str(self.launch_workspace))
        role = arguments.get("role", DEFAULT_ROLE)
        model = arguments.get("model", DEFAULT_MODEL)
        effort = arguments.get("effort", DEFAULT_EFFORT)
        budget = dict(arguments.get("budget", DEFAULT_BUDGET))
        model = normalize_model_identifier(model)
        budget["max_cost_usd"] = normalize_budget_usd(budget["max_cost_usd"])
        identity = self._identity(cwd)
        admitted_path_pin = _repository_path_pin(identity)
        identity = validate_launch(
            identity.top_level,
            self.launch_workspace,
            role=role,
            expected_head=identity.head,
            expected_status_fingerprint=identity.status_fingerprint,
            expected_tracked_content_fingerprint=(
                identity.tracked_content_fingerprint
            ),
            prompt=prompt,
        )
        validated_path_pin = _repository_path_pin(identity)
        if validated_path_pin != admitted_path_pin:
            raise BoundaryError("repository path identity changed during validation")
        payload = {
            "prompt": prompt,
            "cwd": str(identity.top_level),
            "role": role,
            "model": model,
            "effort": effort,
            "budget": budget,
            "baseline": {
                "head": identity.head,
                "status_fingerprint": identity.status_fingerprint,
                "tracked_content_fingerprint": identity.tracked_content_fingerprint,
                **validated_path_pin,
            },
            "claude_executable": self.claude_executable,
        }
        return identity, payload

    def _worker_environment(self) -> dict[str, str]:
        child = build_child_environment(self.environment)
        child.update(
            {
                "FORGE_STATE_PATH": str(self.store.path),
                "FORGE_LAUNCH_WORKSPACE": str(self.launch_workspace),
                "FORGE_CLAUDE_EXECUTABLE": self.claude_executable,
            }
        )
        return child

    def _dispatch_job(self, job: Job) -> bool:
        """Dispatch the current CREATED attempt once; return idempotent status."""
        self._reject_recovery_mutation(job)
        dispatch_claim = self.store.claim_dispatch(
            job.id,
            owner_pid=os.getpid(),
        )
        if dispatch_claim is None:
            return True
        try:
            worker_log = self.store.create_attempt_artifact(
                job.id,
                dispatch_claim.attempt_id,
                "worker.log",
            )
            worker_environment = self._worker_environment()
            worker_pid = spawn_detached_worker(
                self.bridge_path,
                job.id,
                job_role=job.payload["role"],
                dispatch_token=dispatch_claim.token,
                worker_log=worker_log,
                environment=worker_environment,
                bind_before_release=lambda spawned_pid: self.store.record_worker_pid(
                    job.id,
                    spawned_pid,
                    _dispatch_claim=dispatch_claim,
                ),
            )
        except BaseException as launch_error:
            bound_attempt = self.store.get_attempt(dispatch_claim.attempt_id)
            if bound_attempt.worker_pid is not None:
                if (
                    bound_attempt.worker_identity is None
                    or (
                        isinstance(launch_error, BoundWorkerLaunchError)
                        and bound_attempt.worker_pid != launch_error.worker_pid
                    )
                    or not self.store.abort_bound_worker_launch(
                        job.id,
                        worker_pid=bound_attempt.worker_pid,
                        worker_identity=bound_attempt.worker_identity,
                        _dispatch_claim=dispatch_claim,
                    )
                ):
                    raise StateError(
                        "reaped bound worker could not release its launch claim"
                    ) from launch_error
                raise
            try:
                released = self.store.release_dispatch(
                    job.id,
                    owner_pid=dispatch_claim.owner_pid,
                    dispatch_token=dispatch_claim.token,
                )
            except BaseException as release_error:
                raise StateError(
                    "worker spawn failed and its dispatch claim could not be released"
                ) from release_error
            if not released:
                raise StateError(
                    "worker spawn failed and its dispatch claim was no longer owned"
                ) from launch_error
            raise
        bound_attempt = self.store.get_attempt(dispatch_claim.attempt_id)
        if (
            bound_attempt.worker_pid != worker_pid
            or bound_attempt.worker_identity is None
        ):
            raise StateError("worker launch returned without durable identity binding")
        return False

    def start(self, arguments: dict[str, Any]) -> ToolExecution:
        if arguments.get("role", DEFAULT_ROLE) == "recovery":
            raise StateError(RECOVERY_DISABLED_MESSAGE)
        request_declaration = {
            "prompt": arguments.get("prompt"),
            "cwd": arguments.get("cwd", str(self.launch_workspace)),
            "role": arguments.get("role", DEFAULT_ROLE),
            "model": arguments.get("model", DEFAULT_MODEL),
            "effort": arguments.get("effort", DEFAULT_EFFORT),
            "budget": arguments.get("budget", DEFAULT_BUDGET),
            "admission": arguments.get("admission"),
        }
        request_hash = _canonical_evidence(request_declaration)
        idempotency_key = arguments.get("idempotency_key", f"auto-{request_hash}")
        job, replay = self.store.create_admitted_job(
            repo_id=self._launch_repo_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            build_payload=lambda job_id, attempt_id, active_reservations: self._admitted_payload(
                arguments,
                job_id=job_id,
                attempt_id=attempt_id,
                active_reservations=active_reservations,
                request_hash=request_hash,
            ),
        )
        self._dispatch_job(job)
        body = self._status_body(self.store.get_job(job.id))
        # Report request ownership, not which racing caller happened to win the
        # detached-worker dispatch claim. Exactly one creator is non-idempotent.
        body["idempotent"] = replay
        return ToolExecution(body, f"Forge job {job.id} accepted.")

    def steer(self, arguments: dict[str, Any]) -> ToolExecution:
        job = self._owned_job(arguments["job_id"])
        self._reject_recovery_mutation(job)
        expected_revision = arguments["revision"]
        prompt = arguments["prompt"]
        expansion = arguments.get("admission_expansion")
        idempotency_envelope = canonical_json(
            {
                "attempt_id": job.current_attempt_id,
                "expected_revision": expected_revision,
                "prompt": prompt,
                "admission_expansion": expansion,
            }
        )
        operation_hash = hashlib.sha256(
            idempotency_envelope.encode("utf-8")
        ).hexdigest()
        idempotency_key = (
            "expand-" if expansion is not None else "steer-"
        ) + operation_hash
        if expansion is None:
            command = self.store.queue_command(
                job.id,
                expected_revision=expected_revision,
                kind="STEER",
                payload={"prompt": prompt},
                idempotency_key=idempotency_key,
            )
            expanded = False
            replay = False
            reservation = None
        else:
            reservation, command, replay = self.store.expand_bridge_lane_and_queue(
                job.id,
                attempt_id=job.current_attempt_id,
                expected_job_revision=expected_revision,
                lease_id=expansion["lease_id"],
                expected_lease_revision=expansion["lease_revision"],
                prior_files=expansion["prior_files"],
                files=expansion["files"],
                prompt=prompt,
                idempotency_key=idempotency_key,
                operation_hash=operation_hash,
                build_admission=lambda current, prior, active: self._expansion_admission_evidence(
                    job.payload,
                    job_id=job.id,
                    attempt_id=job.current_attempt_id,
                    prompt=prompt,
                    current=current,
                    prior_admission=prior,
                    active_reservations=active,
                    prior_files=expansion["prior_files"],
                    files=expansion["files"],
                ),
            )
            expanded = True
        current = self.store.get_job(job.id)
        body = {
            "status": "accepted",
            "job_id": job.id,
            "attempt_id": command.attempt_id,
            "revision": current.revision,
            "command_id": command.id,
            "sequence": command.sequence,
            "command_status": command.status,
            "expected_revision": command.expected_revision,
            "expanded": expanded,
            "idempotent": replay,
        }
        if reservation is not None:
            body.update(
                {
                    "lease_id": reservation["lease_id"],
                    "lease_revision": reservation["revision"],
                    "files": reservation["files"],
                    "admission_evidence_sha256": reservation[
                        "admission_sha256"
                    ],
                }
            )
        return ToolExecution(body, f"Forge steering command {command.sequence} accepted.")

    def cancel(self, arguments: dict[str, Any]) -> ToolExecution:
        job = self._owned_job(arguments["job_id"])
        self._reject_recovery_mutation(job)
        expected_revision = arguments["revision"]
        if job.status == "RUNNING" and job.revision != expected_revision:
            raise RevisionConflict(job.id, expected_revision, job.revision)
        job_cancellation_containment = _bind_test_cancellation_containment(
            self.cancellation_containment,
            job.id,
        )
        containment = cancellation_containment_preflight(
            job_cancellation_containment
        )
        if job.status not in TERMINAL_STATUSES and containment["supported"] is not True:
            if job.revision != expected_revision:
                raise RevisionConflict(job.id, expected_revision, job.revision)
            return ToolExecution(
                {
                    "status": "PAUSED_POLICY",
                    "job_id": job.id,
                    "attempt_id": job.current_attempt_id,
                    "revision": job.revision,
                    "reason": "unsupported_recursive_containment",
                    "containment": containment,
                },
                "Forge cancellation is paused by policy because recursive provider containment is unavailable.",
                True,
            )
        if job.status == "INTERRUPTING":
            attempt = self.store.get_attempt(job.current_attempt_id)
            commands = self.store.list_commands(job.id)
            ambiguous = next(
                (
                    command
                    for command in reversed(commands)
                    if command.kind == "CANCEL"
                    and command.status == "DELIVERING"
                ),
                None,
            )
            if (
                ambiguous is not None
                and (
                    attempt.worker_pid is None
                    or attempt.worker_identity is None
                    or not process_instance_is_live(
                        attempt.worker_pid,
                        attempt.worker_identity,
                    )
                )
            ):
                if job.revision != expected_revision:
                    raise RevisionConflict(job.id, expected_revision, job.revision)
                return ToolExecution(
                    {
                        "status": "PAUSED_POLICY",
                        "job_id": job.id,
                        "attempt_id": job.current_attempt_id,
                        "revision": job.revision,
                        "reason": "delivery_ambiguous",
                        "command_id": ambiguous.id,
                        "command_status": ambiguous.status,
                        "containment": containment,
                    },
                    "Forge cancellation is paused because its delivered-frame state is ambiguous and the owning worker is no longer live.",
                    True,
                )
        live_worker_identity = None
        manifest_pin = None
        if job.status == "RUNNING":
            attempt = self.store.get_attempt(job.current_attempt_id)
            try:
                if (
                    attempt.worker_pid is None
                    or attempt.worker_identity is None
                    or not process_instance_is_live(
                        attempt.worker_pid,
                        attempt.worker_identity,
                    )
                ):
                    raise ProcessError("worker identity does not match durable state")
            except ProcessError:
                return ToolExecution(
                    {
                        "status": "PAUSED_POLICY",
                        "job_id": job.id,
                        "attempt_id": job.current_attempt_id,
                        "revision": job.revision,
                        "reason": "worker_identity_unavailable",
                        "containment": containment,
                    },
                    "Forge cancellation is paused because the recorded worker identity is not live.",
                    True,
                )
            live_worker_identity = attempt.worker_identity
            admission_containment = cancellation_admission_preflight(
                job_cancellation_containment,
                expected_process_group_id=attempt.provider_pgid,
            )
            manifest_pin = admission_containment.get("_test_manifest_pin")
            containment = {
                key: value
                for key, value in admission_containment.items()
                if not key.startswith("_")
            }
            if containment["supported"] is not True:
                return ToolExecution(
                    {
                        "status": "PAUSED_POLICY",
                        "job_id": job.id,
                        "attempt_id": job.current_attempt_id,
                        "revision": job.revision,
                        "reason": containment["reason"],
                        "containment": containment,
                    },
                    "Forge cancellation is paused because the recursive containment scope is invalid.",
                    True,
                )
        try:
            admission = self.store.request_cancellation(
                job.id,
                expected_revision=expected_revision,
                live_worker_identity=live_worker_identity,
                cancellation_manifest_pin=(
                    manifest_pin if isinstance(manifest_pin, dict) else None
                ),
            )
        except WorkerIdentityUnavailable:
            current = self._owned_job(job.id)
            return ToolExecution(
                {
                    "status": "PAUSED_POLICY",
                    "job_id": job.id,
                    "attempt_id": current.current_attempt_id,
                    "revision": current.revision,
                    "reason": "worker_identity_unavailable",
                    "containment": containment,
                },
                "Forge cancellation is paused because the recorded worker identity is not live.",
                True,
            )
        current = self._owned_job(job.id)
        command = admission.command
        body = {
            "status": admission.status,
            "job_id": job.id,
            "attempt_id": current.current_attempt_id,
            "revision": admission.revision,
            "idempotent": admission.idempotent,
            "outcome": admission.outcome,
            "command_id": None if command is None else command.id,
            "request_id": (
                None if command is None else command.payload.get("request_id")
            ),
            "command_status": None if command is None else command.status,
        }
        return ToolExecution(body, f"Forge cancellation for {job.id} was accepted.")

    def _resume_context(self, job: Job) -> tuple[str, str, bool]:
        attempt = self.store.get_attempt(job.current_attempt_id)
        if attempt.status in TERMINAL_STATUSES:
            recorded_session_id = attempt.session_id
            source_attempt_id = attempt.id
            create_child = True
        elif (
            attempt.status in {"CREATED", "PREFLIGHT", "RUNNING"}
            and attempt.resume_session_id is not None
            and attempt.parent_attempt_id is not None
        ):
            parent = self.store.get_attempt(attempt.parent_attempt_id)
            if (
                parent.job_id != job.id
                or parent.status not in TERMINAL_STATUSES
                or parent.session_id != attempt.resume_session_id
                or attempt.session_id not in {None, attempt.resume_session_id}
            ):
                raise StateError("pending resume attempt is not bound to its parent session")
            recorded_session_id = attempt.resume_session_id
            source_attempt_id = parent.id
            create_child = False
        else:
            raise InvalidTransition("resume requires a terminal or pending resume attempt")
        if recorded_session_id is None:
            raise StateError("resume requires an explicitly recorded session")
        if (
            recorded_session_id.casefold() in {"latest", "continue"}
            or recorded_session_id.startswith("-")
        ):
            raise StateError("recorded session is not an explicit resume identity")
        return source_attempt_id, recorded_session_id, create_child

    def _validate_resume_identity(
        self,
        job: Job,
        arguments: dict[str, Any],
        *,
        model_override: str | None,
    ) -> tuple[RepoIdentity, str, str, bool, dict[str, Any]]:
        payload = job.payload
        if _is_legacy_v8_payload(payload):
            legacy_request_hash = hashlib.sha256(
                canonical_json(payload).encode("utf-8")
            ).hexdigest()
            if legacy_request_hash != job.request_hash:
                raise StateError("stored legacy v8 job baseline is not immutable")
            raise StateError(
                "legacy v8 attempts cannot be resumed under Task 10 without "
                "their original turn, cost, and model-verification evidence"
            )
        if _is_legacy_v9_payload(payload):
            legacy_request_hash = hashlib.sha256(
                canonical_json(payload).encode("utf-8")
            ).hexdigest()
            if legacy_request_hash != job.request_hash:
                raise StateError("stored legacy v9 job baseline is not immutable")
            raise StateError(
                "legacy v9 attempts cannot be resumed without durable admission evidence"
            )
        if not isinstance(payload, dict) or set(payload) != {
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
        }:
            raise StateError("stored job payload does not match the resume contract")
        if payload["request_hash"] != job.request_hash:
            raise StateError("stored job baseline is not immutable")
        from .worker import WorkerError, _payload as validate_worker_payload

        try:
            validate_worker_payload(
                payload,
                allow_evolved_review_controls=(
                    payload["role"] == "review"
                    and arguments.get("refreeze") is not None
                ),
            )
        except WorkerError as error:
            raise StateError("stored admission evidence is invalid") from error
        original_baseline = payload["baseline"]
        if not isinstance(original_baseline, dict) or set(original_baseline) != {
            "head",
            "status_fingerprint",
            "tracked_content_fingerprint",
            *REPOSITORY_PATH_PIN_FIELDS,
        }:
            raise StateError("stored resume baseline is malformed")
        if payload["cwd"] != original_baseline["top_level"]:
            raise StateError("stored resume realpath is inconsistent")

        source_attempt_id, recorded_session_id, create_child = self._resume_context(job)
        source_context = self.store.get_attempt_context(source_attempt_id)
        source_context.pop("sha256", None)
        effective_prompt = arguments.get("prompt", source_context.get("prompt"))
        if not isinstance(effective_prompt, str) or not effective_prompt:
            raise StateError("resume prompt is unavailable")
        refreeze = arguments.get("refreeze")
        identity = self._identity(payload["cwd"])
        if (
            _repo_id(identity) != job.repo_id
            or job.repo_id != self._launch_repo_id
            or str(identity.top_level) != original_baseline["top_level"]
            or str(identity.git_marker) != original_baseline["git_marker"]
            or str(identity.common_dir) != original_baseline["common_dir"]
            or identity.head != original_baseline["head"]
        ):
            raise BoundaryError("repository identity changed since the original attempt")
        current_path_pin = _repository_path_pin(identity)
        expected_path_pin = {
            name: original_baseline[name] for name in REPOSITORY_PATH_PIN_FIELDS
        }
        if current_path_pin != expected_path_pin:
            raise BoundaryError("repository ownership or path identity changed")

        dirty_paths = list(_git_dirty_paths(identity.top_level))
        review_controls = source_context.get("review_controls", {})
        if refreeze is None:
            source_baseline = source_context.get("baseline")
            if (
                not isinstance(source_baseline, dict)
                or identity.status_fingerprint != source_baseline.get("status_fingerprint")
                or identity.tracked_content_fingerprint
                != source_baseline.get("tracked_content_fingerprint")
            ):
                raise BoundaryError(
                    "resume drift requires an exact typed refreeze declaration"
                )
        else:
            if refreeze.get("baseline_head") != identity.head:
                raise BoundaryError("refreeze baseline does not match canonical HEAD")
            if payload["role"] == "review":
                declarations = refreeze.get("review_controls")
                if not isinstance(review_controls, dict) or set(review_controls) not in (
                    {"plan", "review_log"},
                    {"plan", "review_log", "domain_docs"},
                ):
                    raise StateError("original review controls are unavailable")
                rebound = _pin_review_controls(
                    declarations,
                    root=identity.top_level,
                    label="review refreeze control",
                )
                if any(
                    rebound[name]["path"] != review_controls[name]["path"]
                    for name in ("plan", "review_log")
                ):
                    raise BoundaryError("review control path changed across attempts")
                prior_domain_paths = [
                    str(Path(pin["path"]).relative_to(identity.top_level))
                    for pin in review_controls.get("domain_docs", [])
                ]
                rebound_domain_paths = [
                    str(Path(pin["path"]).relative_to(identity.top_level))
                    for pin in rebound.get("domain_docs", [])
                ]
                if rebound_domain_paths != prior_domain_paths:
                    raise BoundaryError(
                        "review domain-doc paths changed across attempts"
                    )
                allowed = {
                    str(Path(pin["path"]).relative_to(identity.top_level))
                    for pin in _review_control_pin_values(rebound)
                }
                if not set(dirty_paths).issubset(allowed):
                    raise BoundaryError("unrelated repository drift blocks review resume")
                if refreeze.get("dirty_files", []) not in ([], None):
                    raise StateError("review refreeze cannot declare product dirty files")
                review_controls = rebound
            else:
                if "review_controls" in refreeze:
                    raise StateError("product refreeze cannot evolve review controls")
                declarations = refreeze.get("dirty_files")
                if not isinstance(declarations, list):
                    raise StateError("product refreeze requires exact dirty-file hashes")
                declared_paths = [item.get("path") for item in declarations if isinstance(item, dict)]
                if sorted(declared_paths) != sorted(dirty_paths):
                    raise BoundaryError("product refreeze dirty set is not exact")
                reservation = self.store.get_bridge_lane_reservation(job.id)
                if not set(declared_paths).issubset(reservation["files"]):
                    raise BoundaryError("product refreeze exceeds the reserved file set")
                for item in declarations:
                    relative = _exact_declared_path(
                        identity.top_level,
                        item["path"],
                        allow_missing=False,
                    )
                    actual = hashlib.sha256(
                        (identity.top_level / relative).read_bytes()
                    ).hexdigest()
                    if item.get("sha256") != actual:
                        raise BoundaryError("product refreeze file hash changed")

        baseline = {
            "head": identity.head,
            "status_fingerprint": identity.status_fingerprint,
            "tracked_content_fingerprint": identity.tracked_content_fingerprint,
            **current_path_pin,
        }
        request_declaration = {
            "job_id": job.id,
            "revision": arguments["revision"],
            "prompt": effective_prompt,
            "model_override": model_override,
            "idempotency_key": arguments.get("idempotency_key"),
            "refreeze": refreeze,
        }
        request_hash = _canonical_evidence(request_declaration)
        context = {
            "version": 1,
            "prompt": effective_prompt,
            "prompt_sha256": hashlib.sha256(
                effective_prompt.encode("utf-8")
            ).hexdigest(),
            "baseline": baseline,
            "baseline_sha256": _canonical_evidence(baseline),
            "review_controls": review_controls,
            "review_controls_sha256": _canonical_evidence(review_controls),
            "dirty_files": dirty_paths,
            "dirty_files_sha256": _canonical_evidence(dirty_paths),
            "request_hash": request_hash,
        }
        return identity, source_attempt_id, recorded_session_id, create_child, context

    def _validate_stale_resume_replay(
        self,
        job: Job,
        arguments: dict[str, Any],
        *,
        expected_revision: int,
        model_override: str | None,
    ) -> tuple[str, Any, dict[str, Any]]:
        try:
            source_id, _session_id, create_child = self._resume_context(job)
        except (InvalidTransition, StateError):
            raise RevisionConflict(job.id, expected_revision, job.revision) from None
        source_attempt = self.store.get_attempt(source_id)
        child = self.store.get_attempt(job.current_attempt_id)
        if create_child or expected_revision != source_attempt.revision:
            raise RevisionConflict(job.id, expected_revision, job.revision)
        if child.model_override != model_override:
            raise StateError(
                "resume model override conflicts with the pending attempt"
            )
        source_context = self.store.get_attempt_context(source_id)
        child_context = self.store.get_attempt_context(child.id)
        effective_prompt = arguments.get("prompt", source_context.get("prompt"))
        replay_hash = _canonical_evidence(
            {
                "job_id": job.id,
                "revision": expected_revision,
                "prompt": effective_prompt,
                "model_override": model_override,
                "idempotency_key": arguments.get("idempotency_key"),
                "refreeze": arguments.get("refreeze"),
            }
        )
        if child_context.get("request_hash") != replay_hash:
            raise IdempotencyConflict(
                "stale resume revision belongs to a different request"
            )
        return source_id, child, child_context

    def _stale_resume_replay_result(
        self,
        job: Job,
        *,
        source_id: str,
        child: Any,
        child_context: dict[str, Any],
    ) -> ToolExecution:
        created = self.store.get_job(job.id)
        self._dispatch_job(created)
        body = self._status_body(self.store.get_job(job.id))
        body.update(
            {
                "idempotent": True,
                "resumed_from_attempt_id": source_id,
                "resume_session_id": child.resume_session_id,
                "prompt_sha256": child_context["prompt_sha256"],
                "context_sha256": child_context["sha256"],
            }
        )
        return ToolExecution(body, f"Forge job {job.id} resume replay accepted.")

    def resume(self, arguments: dict[str, Any]) -> ToolExecution:
        job = self._owned_job(arguments["job_id"])
        self._reject_recovery_mutation(job)
        expected_revision = arguments["revision"]
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise StateError("expected revision must be a non-negative integer")
        model_override = arguments.get("model_override")
        if model_override is not None:
            model_override = normalize_model_identifier(model_override)
        initial_attempt_id = job.current_attempt_id
        if job.revision != expected_revision:
            source_id, child, child_context = self._validate_stale_resume_replay(
                job,
                arguments,
                expected_revision=expected_revision,
                model_override=model_override,
            )
            return self._stale_resume_replay_result(
                job,
                source_id=source_id,
                child=child,
                child_context=child_context,
            )
        (
            _identity,
            source_attempt_id,
            recorded_session_id,
            create_child,
            attempt_context,
        ) = self._validate_resume_identity(
            job,
            arguments,
            model_override=model_override,
        )

        # Re-read the CAS state after the filesystem checks so a concurrent state
        # change cannot attach the child to a different terminal attempt.
        current = self._owned_job(job.id)
        if current.current_attempt_id != initial_attempt_id:
            raise StateError("resume attempt changed during validation")
        if create_child and current.revision != expected_revision:
            raise RevisionConflict(job.id, expected_revision, current.revision)
        current_source, current_session, current_create_child = self._resume_context(current)
        if (
            current_source != source_attempt_id
            or current_session != recorded_session_id
            or current_create_child != create_child
        ):
            raise StateError("resume attempt context changed during validation")
        if create_child:
            parent_admission_hash = self.store.get_attempt_admission(
                source_attempt_id
            )["sha256"]
            lane_reservation = self.store.get_bridge_lane_reservation(job.id)
            try:
                child = self.store.create_admitted_resume_attempt(
                    job.id,
                    expected_revision=expected_revision,
                    recorded_session_id=recorded_session_id,
                    model_override=model_override,
                    validate_reservation=lambda attempt_id, active_reservations: self._resume_admission_evidence(
                        current.payload,
                        job_id=job.id,
                        attempt_id=attempt_id,
                        parent_attempt_id=source_attempt_id,
                        parent_revision=expected_revision,
                        parent_admission_hash=parent_admission_hash,
                        model_override=model_override,
                        attempt_context=attempt_context,
                        lane_reservation=lane_reservation,
                        active_reservations=active_reservations,
                    ),
                    attempt_context=attempt_context,
                )
            except RevisionConflict as conflict:
                try:
                    authoritative = self._owned_job(job.id)
                    replay_source, replay_child, replay_context = (
                        self._validate_stale_resume_replay(
                            authoritative,
                            arguments,
                            expected_revision=expected_revision,
                            model_override=model_override,
                        )
                    )
                except StateError:
                    raise conflict from None
                return self._stale_resume_replay_result(
                    authoritative,
                    source_id=replay_source,
                    child=replay_child,
                    child_context=replay_context,
                )
        else:
            child = self.store.get_attempt(current.current_attempt_id)
            if child.model_override != model_override:
                raise StateError("resume model override conflicts with the pending attempt")
        created = self.store.get_job(job.id)
        idempotent = self._dispatch_job(created)
        body = self._status_body(self.store.get_job(job.id))
        body.update(
            {
                "idempotent": idempotent,
                "resumed_from_attempt_id": source_attempt_id,
                "resume_session_id": child.resume_session_id,
                "prompt_sha256": attempt_context["prompt_sha256"],
                "context_sha256": self.store.get_attempt_context(child.id)["sha256"],
            }
        )
        return ToolExecution(body, f"Forge job {job.id} resumed explicitly.")

    def _status_body(self, job: Job) -> dict[str, Any]:
        for _snapshot_attempt in range(16):
            attempt = self.store.get_attempt(job.current_attempt_id)
            try:
                liveness_snapshot = self.store.get_liveness_snapshot(
                    job.id,
                    attempt_id=attempt.id,
                    expected_revision=job.revision,
                )
                break
            except (RevisionConflict, StateError):
                job = self.store.get_job(job.id)
        else:
            raise StateError("status could not acquire a stable liveness snapshot")
        legacy_v9 = _is_legacy_v9_payload(job.payload)
        attempt_admission = (
            None
            if _is_legacy_v8_payload(job.payload) or legacy_v9
            else self.store.get_attempt_admission(attempt.id)
        )
        legacy_v8 = _is_legacy_v8_payload(job.payload)
        model_requested = (
            job.payload["model"]
            if legacy_v8
            else attempt.model_override or job.payload["model"]
        )
        with self.store.connection() as connection:
            latest = connection.execute(
                """
                SELECT sequence, kind, event_hash
                FROM events
                WHERE job_id = ? AND attempt_id = ?
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (job.id, attempt.id),
            ).fetchone()
            outcome = connection.execute(
                """
                SELECT kind, payload_json
                FROM events
                WHERE job_id = ? AND attempt_id = ?
                    AND kind IN ('PROVIDER_PAUSED', 'PROVIDER_FAILED')
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (job.id, attempt.id),
            ).fetchone()
            preflight = connection.execute(
                """
                SELECT payload_json
                FROM events
                WHERE job_id = ? AND attempt_id = ?
                    AND kind = 'PROVIDER_PREFLIGHT'
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (job.id, attempt.id),
            ).fetchone()
            accounting = connection.execute(
                """
                SELECT payload_json
                FROM events
                WHERE job_id = ? AND attempt_id = ?
                    AND kind = 'PROVIDER_ACCOUNTING'
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (job.id, attempt.id),
            ).fetchone()
        provider_version_observed = None
        if preflight is not None and not legacy_v8:
            try:
                preflight_payload = json.loads(preflight["payload_json"])
            except (TypeError, json.JSONDecodeError) as error:
                raise StateError("stored provider preflight is malformed") from error
            if (
                not isinstance(preflight_payload, dict)
                or set(preflight_payload)
                != {"claude_code_version", "supported"}
                or not isinstance(preflight_payload["claude_code_version"], str)
                or not preflight_payload["claude_code_version"]
                or not isinstance(preflight_payload["supported"], bool)
            ):
                raise StateError("stored provider preflight is malformed")
            provider_version_observed = preflight_payload["claude_code_version"]
        accounting_evidence = {
            "usage": None,
            "total_cost_usd": None,
            "usage_provenance": "unavailable",
            "cost_provenance": "unavailable",
        }
        if accounting is not None and not legacy_v8:
            try:
                accounting_evidence = json.loads(accounting["payload_json"])
            except (TypeError, json.JSONDecodeError) as error:
                raise StateError("stored provider accounting is malformed") from error
            _validate_accounting_evidence(accounting_evidence)
        body = {
            "status": job.status,
            "job_id": job.id,
            "revision": job.revision,
            "attempt_id": job.current_attempt_id,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "session_id": attempt.session_id,
            "parent_tool_use_ids": list(attempt.parent_tool_use_ids),
            "requested_model": model_requested,
            "observed_model": attempt.observed_model,
            "requested_effort": job.payload["effort"],
            "progress": {
                "event_count": attempt.event_count,
                "event_bytes": attempt.event_bytes,
                "last_sequence": None if latest is None else latest["sequence"],
                "last_kind": None if latest is None else latest["kind"],
            },
            "event_hash": None if latest is None else latest["event_hash"],
            "liveness": {
                "attempt_id": liveness_snapshot["attempt_id"],
                "revision": liveness_snapshot["revision"],
                "worker_identity_recorded": (
                    liveness_snapshot["worker_identity"] is not None
                ),
                "provider_identity_recorded": (
                    liveness_snapshot["provider_identity"] is not None
                ),
                "last_objective_progress_at": liveness_snapshot[
                    "last_objective_progress_at"
                ],
                "last_stderr_signature_sha256": liveness_snapshot[
                    "last_stderr_signature_sha256"
                ],
                "last_stderr_repeat_count": liveness_snapshot[
                    "last_stderr_repeat_count"
                ],
            },
        }
        if not legacy_v8:
            body.update(
                {
                    "model_requested": model_requested,
                    "model_cli_accepted": attempt.model_cli_accepted,
                    "models_observed": list(attempt.models_observed),
                    "verification_level": attempt.verification_level,
                    "provider_version_observed": provider_version_observed,
                    **accounting_evidence,
                }
            )
            if not legacy_v9:
                body["admission_evidence_sha256"] = attempt_admission["sha256"]
        if outcome is not None and job.status in {
            "PAUSED_PROVIDER_ACCESS",
            "PAUSED_POLICY",
            "FAILED",
        }:
            try:
                payload = json.loads(outcome["payload_json"])
            except (TypeError, json.JSONDecodeError) as error:
                raise StateError("stored provider outcome is malformed") from error
            if (
                not isinstance(payload, dict)
                or set(payload) != {"status", "classification", "reason"}
                or payload["status"] != job.status
                or (
                    job.status in {"PAUSED_PROVIDER_ACCESS", "PAUSED_POLICY"}
                    and outcome["kind"] != "PROVIDER_PAUSED"
                )
                or (
                    job.status == "FAILED"
                    and outcome["kind"] != "PROVIDER_FAILED"
                )
                or not isinstance(payload["classification"], str)
                or not payload["classification"]
                or not isinstance(payload["reason"], str)
                or not payload["reason"]
            ):
                raise StateError("stored provider outcome is malformed")
            field = "pause" if outcome["kind"] == "PROVIDER_PAUSED" else "failure"
            body[field] = {
                "classification": payload["classification"],
                "reason": payload["reason"],
            }
        return body

    def status(self, arguments: dict[str, Any]) -> ToolExecution:
        body = self._status_body(self._owned_job(arguments["job_id"]))
        return ToolExecution(body, f"Forge job {body['job_id']} is {body['status']}.")

    def wait(self, arguments: dict[str, Any]) -> ToolExecution:
        job = self._owned_job(arguments["job_id"])
        expected_revision = arguments.get("revision", job.revision)
        requested_seconds = arguments.get("timeout_ms", 0) / 1000
        timeout_seconds = min(float(requested_seconds), MAX_WAIT_SECONDS)
        deadline = self.monotonic() + timeout_seconds
        while (
            job.revision == expected_revision
            and job.status not in TERMINAL_STATUSES
            and self.monotonic() < deadline
        ):
            remaining = deadline - self.monotonic()
            self.sleep(min(WAIT_POLL_SECONDS, max(remaining, 0)))
            job = self._owned_job(job.id)
        body = self._status_body(job)
        body["wait_timed_out"] = (
            job.revision == expected_revision and job.status not in TERMINAL_STATUSES
        )
        body["wait_cap_ms"] = int(MAX_WAIT_SECONDS * 1000)
        return ToolExecution(body, f"Forge wait returned at revision {job.revision}.")

    @contextmanager
    def _open_artifact(
        self,
        job_id: str,
        name: str,
        *,
        attempt_id: str | None = None,
    ):
        if not job_id or "/" in job_id or name not in {
            "events.jsonl",
            "stderr.log",
            "report.txt",
            "receipt.json",
            "worker.log",
        }:
            raise StateError("job artifact identity is invalid")
        if attempt_id is None:
            attempt_id = self._owned_job(job_id).current_attempt_id
        with self.store.open_attempt_artifact(
            job_id,
            attempt_id,
            name,
        ) as (descriptor, _path):
            yield descriptor, os.fstat(descriptor)

    def _artifact_receipt(
        self,
        job_id: str,
        attempt_id: str,
        name: str,
        *,
        inline_limit: int | None = None,
    ) -> tuple[dict[str, Any], bytes | None]:
        digest = hashlib.sha256()
        inline = bytearray() if inline_limit is not None else None
        total = 0
        with self._open_artifact(
            job_id,
            name,
            attempt_id=attempt_id,
        ) as (descriptor, information):
            maximum = MAX_ARTIFACT_BYTES[name]
            if information.st_size > maximum:
                raise StateError(f"job artifact exceeds its byte bound: {name}")
            while True:
                chunk = os.read(descriptor, min(1024 * 1024, maximum - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    raise StateError(f"job artifact exceeds its byte bound: {name}")
                digest.update(chunk)
                if inline is not None and total <= inline_limit:
                    inline.extend(chunk)
            if total != information.st_size:
                raise StateError("job artifact changed while it was read")
        path = self.store.attempt_artifact_path(job_id, attempt_id, name)
        return (
            {"path": str(path), "sha256": digest.hexdigest()},
            bytes(inline) if inline is not None and total <= inline_limit else None,
        )

    def _validate_terminal_receipt(
        self,
        receipt_bytes: bytes,
        *,
        job: Job,
        attempt: Any,
        artifacts: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        receipt = _strict_json_object(receipt_bytes, "terminal receipt")
        legacy_v8 = _is_legacy_v8_payload(job.payload)
        legacy_v9 = _is_legacy_v9_payload(job.payload)
        status_fields = (
            LEGACY_V8_RECEIPT_STATUS_FIELDS.get(job.status)
            if legacy_v8
            else RECEIPT_STATUS_FIELDS.get(job.status)
        )
        common_fields = (
            LEGACY_V8_RECEIPT_COMMON_FIELDS
            if legacy_v8
            else LEGACY_V9_RECEIPT_COMMON_FIELDS
            if legacy_v9
            else RECEIPT_COMMON_FIELDS
        )
        if status_fields is None or set(receipt) != common_fields | status_fields:
            raise StateError("terminal receipt fields do not match its status")
        model_requested = (
            job.payload["model"]
            if legacy_v8
            else attempt.model_override or job.payload["model"]
        )
        with self.store.connection() as connection:
            preflight = connection.execute(
                """
                SELECT payload_json
                FROM events
                WHERE job_id = ? AND attempt_id = ?
                    AND kind = 'PROVIDER_PREFLIGHT'
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (job.id, attempt.id),
            ).fetchone()
        provider_version_observed = None
        if preflight is not None and not legacy_v8:
            try:
                preflight_payload = json.loads(preflight["payload_json"])
            except (TypeError, json.JSONDecodeError) as error:
                raise StateError("stored provider preflight is malformed") from error
            if (
                not isinstance(preflight_payload, dict)
                or set(preflight_payload)
                != {"claude_code_version", "supported"}
                or not isinstance(preflight_payload["claude_code_version"], str)
                or not preflight_payload["claude_code_version"]
                or not isinstance(preflight_payload["supported"], bool)
            ):
                raise StateError("stored provider preflight is malformed")
            provider_version_observed = preflight_payload["claude_code_version"]
        with self.store.connection() as connection:
            accounting = connection.execute(
                """
                SELECT payload_json
                FROM events
                WHERE job_id = ? AND attempt_id = ?
                    AND kind = 'PROVIDER_ACCOUNTING'
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (job.id, attempt.id),
            ).fetchone()
        accounting_evidence = {
            "usage": None,
            "total_cost_usd": None,
            "usage_provenance": "unavailable",
            "cost_provenance": "unavailable",
        }
        if accounting is not None and not legacy_v8:
            try:
                accounting_evidence = json.loads(accounting["payload_json"])
            except (TypeError, json.JSONDecodeError) as error:
                raise StateError("stored provider accounting is malformed") from error
            _validate_accounting_evidence(accounting_evidence)
        receipt_accounting = None
        if not legacy_v8:
            receipt_accounting = {
                field: receipt[field]
                for field in (
                    "usage",
                    "total_cost_usd",
                    "usage_provenance",
                    "cost_provenance",
                )
            }
            _validate_accounting_evidence(receipt_accounting)
        common_identity_invalid = (
            receipt["job_id"] != job.id
            or receipt["attempt_id"] != attempt.id
            or receipt["repo_id"] != job.repo_id
            or receipt["terminal_status"] != job.status
            or receipt["session_id"] != attempt.session_id
            or receipt["observed_model"] != attempt.observed_model
            or receipt["parent_tool_use_ids"] != list(attempt.parent_tool_use_ids)
            or receipt["requested_model"] != model_requested
            or receipt["requested_effort"] != job.payload["effort"]
        )
        task10_identity_invalid = not legacy_v8 and (
            receipt["model_requested"] != model_requested
            or receipt["model_cli_accepted"] != attempt.model_cli_accepted
            or receipt["models_observed"] != list(attempt.models_observed)
            or receipt["verification_level"] != attempt.verification_level
            or receipt["provider_version_observed"] != provider_version_observed
            or receipt_accounting != accounting_evidence
            or receipt["budget"] != job.payload["budget"]
        )
        if not legacy_v8 and not legacy_v9:
            task10_identity_invalid = task10_identity_invalid or (
                receipt["admission_evidence_sha256"]
                != self.store.get_attempt_admission(attempt.id)["sha256"]
            )
        if common_identity_invalid or task10_identity_invalid:
            raise StateError("terminal receipt identity does not match durable state")
        if (
            receipt["worktree"] != job.payload["baseline"]["top_level"]
            or receipt["baseline_head"] != job.payload["baseline"]["head"]
            or receipt["status_fingerprint"]
            != job.payload["baseline"]["status_fingerprint"]
        ):
            raise StateError("terminal receipt repository identity is invalid")
        _receipt_hash(receipt["event_hash"], "event hash")
        for label, field in RECEIPT_ARTIFACT_HASH_FIELDS.items():
            expected = _receipt_hash(receipt[field], field)
            if artifacts[label]["sha256"] != expected:
                raise StateError(
                    f"terminal artifact does not match its pinned receipt: {label}"
                )
        if job.status == "SUCCEEDED":
            turns = receipt["num_turns"]
            if (
                isinstance(turns, bool)
                or not isinstance(turns, int)
                or not 0 <= turns <= 1_000_000
            ):
                raise StateError("terminal success turn count is invalid")
            if legacy_v8:
                legacy_usage = receipt["usage"]
                legacy_cost = receipt["total_cost_usd"]
                if (
                    not isinstance(legacy_usage, dict)
                    or len(canonical_json(legacy_usage).encode("utf-8")) > 64 * 1024
                    or not isinstance(legacy_cost, str)
                    or not legacy_cost
                    or len(legacy_cost.encode("utf-8")) > 64
                ):
                    raise StateError("legacy v8 success accounting is invalid")
                try:
                    parsed_legacy_cost = Decimal(legacy_cost)
                except InvalidOperation as error:
                    raise StateError("legacy v8 success accounting is invalid") from error
                if (
                    not parsed_legacy_cost.is_finite()
                    or parsed_legacy_cost < 0
                    or parsed_legacy_cost > Decimal("1000000")
                ):
                    raise StateError("legacy v8 success accounting is invalid")
        if job.status.startswith("PAUSED_"):
            pause = receipt["pause"]
            if (
                not isinstance(pause, dict)
                or set(pause) != {"classification", "reason"}
                or any(
                    not isinstance(pause[name], str) or not pause[name]
                    for name in ("classification", "reason")
                )
            ):
                raise StateError("terminal provider pause receipt is invalid")
        if job.status == "FAILED":
            failure = receipt["error"]
            allowed = {"type", "message"}
            classified = allowed | {"classification", "reason"}
            if (
                not isinstance(failure, dict)
                or frozenset(failure)
                not in {frozenset(allowed), frozenset(classified)}
                or any(
                    not isinstance(failure[name], str) or not failure[name]
                    for name in failure
                )
            ):
                raise StateError("terminal failure receipt is invalid")
        if job.status == "CANCELLED":
            cancellation = receipt["cancellation"]
            expected_fields = {
                "request_id",
                "provider_pgid",
                "control_acknowledged_at",
                "term_sent_at",
                "kill_sent_at",
                "quiescent_at",
                "non_zombie_descendants",
            }
            provider_exit_fields = {
                "provider_exited_at",
                "termination_mode",
            }
            cancellation_fields = (
                set() if not isinstance(cancellation, dict) else set(cancellation)
            )
            if not isinstance(cancellation, dict) or cancellation_fields not in (
                expected_fields,
                expected_fields | provider_exit_fields,
            ):
                raise StateError("terminal cancellation receipt fields are invalid")
            provider_exit_mode = provider_exit_fields <= cancellation_fields
            request_id = cancellation["request_id"]
            if (
                not isinstance(request_id, str)
                or not request_id.startswith("cancel-")
                or len(request_id) != 39
                or any(
                    character not in "0123456789abcdef"
                    for character in request_id[7:]
                )
            ):
                raise StateError("terminal cancellation request id is invalid")
            if (
                isinstance(cancellation["provider_pgid"], bool)
                or not isinstance(cancellation["provider_pgid"], int)
                or cancellation["provider_pgid"] <= 0
                or cancellation["provider_pgid"] != attempt.provider_pgid
            ):
                raise StateError("terminal cancellation provider identity is invalid")
            for field in (
                "control_acknowledged_at",
                "quiescent_at",
            ):
                value = cancellation[field]
                if (
                    not isinstance(value, str)
                    or not value
                    or "\x00" in value
                    or len(value.encode("utf-8")) > 128
                ):
                    raise StateError(f"terminal cancellation {field} is invalid")
            term_sent_at = cancellation["term_sent_at"]
            if provider_exit_mode:
                if (
                    cancellation["termination_mode"] != "provider_exit_after_ack"
                    or term_sent_at is not None
                    or cancellation["kill_sent_at"] is not None
                ):
                    raise StateError(
                        "terminal provider-exit cancellation evidence is invalid"
                    )
                provider_exited_at = cancellation["provider_exited_at"]
                if (
                    not isinstance(provider_exited_at, str)
                    or not provider_exited_at
                    or "\x00" in provider_exited_at
                    or len(provider_exited_at.encode("utf-8")) > 128
                ):
                    raise StateError(
                        "terminal cancellation provider exit timestamp is invalid"
                    )
            elif (
                not isinstance(term_sent_at, str)
                or not term_sent_at
                or "\x00" in term_sent_at
                or len(term_sent_at.encode("utf-8")) > 128
            ):
                raise StateError("terminal cancellation term timestamp is invalid")
            kill_sent_at = cancellation["kill_sent_at"]
            if kill_sent_at is not None and (
                not isinstance(kill_sent_at, str)
                or not kill_sent_at
                or "\x00" in kill_sent_at
                or len(kill_sent_at.encode("utf-8")) > 128
            ):
                raise StateError("terminal cancellation kill timestamp is invalid")
            if cancellation["non_zombie_descendants"] != 0:
                raise StateError("terminal cancellation lacks quiescence proof")
            self.store.validate_cancellation_receipt(job.id, cancellation)
        return receipt

    def result(self, arguments: dict[str, Any]) -> ToolExecution:
        job = self._owned_job(arguments["job_id"])
        if job.status not in TERMINAL_STATUSES:
            return ToolExecution(
                {
                    "status": "not_ready",
                    "job_id": job.id,
                    "revision": job.revision,
                },
                "Forge job has not reached a terminal state.",
                True,
            )
        attempt = self.store.get_attempt(job.current_attempt_id)
        if (
            attempt.terminal_receipt_name != "receipt.json"
            or attempt.terminal_receipt_hash is None
        ):
            raise StateError("terminal job has no referenced receipt")
        names = {
            "events": "events.jsonl",
            "stderr": "stderr.log",
            "report": "report.txt",
            "receipt": "receipt.json",
            "worker_log": "worker.log",
        }
        artifacts = {}
        receipt_bytes = None
        for label, filename in names.items():
            try:
                receipt, inline = self._artifact_receipt(
                    job.id,
                    attempt.id,
                    filename,
                    inline_limit=(
                        MAX_ARTIFACT_BYTES["receipt.json"]
                        if label == "receipt"
                        else None
                    ),
                )
            except FileNotFoundError:
                raise StateError(f"required job artifact is missing: {filename}")
            artifacts[label] = receipt
            if label == "receipt":
                receipt_bytes = inline
        receipt = artifacts.get("receipt")
        if receipt is None or receipt["sha256"] != attempt.terminal_receipt_hash:
            raise StateError("terminal receipt does not match durable state")
        if receipt_bytes is None:
            raise StateError("terminal receipt could not be read within its bound")
        validated_receipt = self._validate_terminal_receipt(
            receipt_bytes,
            job=job,
            attempt=attempt,
            artifacts=artifacts,
        )
        report_receipt, report_bytes = self._artifact_receipt(
            job.id,
            attempt.id,
            "report.txt",
            inline_limit=MAX_INLINE_REPORT_BYTES,
        )
        if report_receipt["sha256"] != artifacts["report"]["sha256"]:
            raise StateError("final report changed after receipt validation")
        try:
            report = None if report_bytes is None else report_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise StateError("final report artifact is not valid UTF-8") from error
        body = {
            "status": job.status,
            "job_id": job.id,
            "revision": job.revision,
            "report": report,
            "report_inline": report is not None,
            "artifacts": artifacts,
            "terminal_receipt": receipt,
        }
        if not _is_legacy_v8_payload(job.payload):
            body.update(
                {
                    "provider_version_observed": validated_receipt[
                        "provider_version_observed"
                    ],
                    "usage": validated_receipt["usage"],
                    "total_cost_usd": validated_receipt["total_cost_usd"],
                    "usage_provenance": validated_receipt["usage_provenance"],
                    "cost_provenance": validated_receipt["cost_provenance"],
                }
            )
            if not _is_legacy_v9_payload(job.payload):
                body["admission_evidence_sha256"] = self.store.get_attempt_admission(
                    attempt.id
                )["sha256"]
        if job.status.startswith("PAUSED_"):
            body["pause"] = validated_receipt["pause"]
        elif job.status == "FAILED" and {
            "classification",
            "reason",
        } <= set(validated_receipt["error"]):
            body["failure"] = {
                "classification": validated_receipt["error"]["classification"],
                "reason": validated_receipt["error"]["reason"],
            }
        return ToolExecution(body, f"Forge result for {job.id} is available.")

    def list_jobs(self, arguments: dict[str, Any]) -> ToolExecution:
        repo_id = self._launch_repo_id
        limit = arguments.get("limit", 50)
        status = arguments.get("status")
        parameters: list[Any] = [repo_id]
        predicate = "repo_id = ?"
        if status is not None:
            internal_statuses = PUBLIC_STATUSES[status]
            placeholders = ",".join("?" for _ in internal_statuses)
            predicate += f" AND status IN ({placeholders})"
            parameters.extend(internal_statuses)
        parameters.append(limit)
        with self.store.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT id, status, revision, payload_json, created_at, updated_at
                FROM jobs
                WHERE {predicate}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        jobs = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            jobs.append(
                {
                    "job_id": row["id"],
                    "status": INTERNAL_TO_PUBLIC[row["status"]],
                    "internal_status": row["status"],
                    "revision": row["revision"],
                    "role": payload["role"],
                    "requested_model": payload["model"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )
        return ToolExecution(
            {"status": "ok", "repository_id": repo_id, "jobs": jobs},
            f"Listed {len(jobs)} Forge jobs for the current repository.",
        )


_DEFAULT_SERVICE: ForgeService | None = None


def _workspace_binding_root() -> Path:
    home = Path.home().resolve(strict=True)
    root = _safe_environment_path(
        "FORGE_WORKSPACE_BINDINGS",
        home / ".codex" / "forge" / "workspace-bindings",
        must_exist=True,
    )
    try:
        information = os.lstat(root)
    except OSError as error:
        raise SecurityError("Forge workspace binding root is unavailable") from error
    if (
        not stat.S_ISDIR(information.st_mode)
        or information.st_uid != os.geteuid()
        or stat.S_IMODE(information.st_mode) != 0o700
    ):
        raise SecurityError("Forge workspace binding root is unsafe")
    return root


def _read_workspace_binding(context: Any) -> Path:
    if not isinstance(context, dict) or set(context) != {"session_id", "token"}:
        raise SecurityError("Forge workspace binding is missing or malformed")
    session_id = context.get("session_id")
    token = context.get("token")
    if (
        not isinstance(session_id, str)
        or not session_id
        or len(session_id) > 256
        or "\x00" in session_id
        or not isinstance(token, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None
    ):
        raise SecurityError("Forge workspace binding is missing or malformed")

    root = _workspace_binding_root()
    filename = hashlib.sha256(session_id.encode("utf-8")).hexdigest() + ".json"
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    file_flags |= getattr(os, "O_NOFOLLOW", 0)
    if not getattr(os, "O_DIRECTORY", 0) or not getattr(os, "O_NOFOLLOW", 0):
        raise SecurityError("Forge workspace binding primitives are unavailable")

    directory_fd = -1
    binding_fd = -1
    try:
        directory_fd = os.open(root, directory_flags)
        directory_information = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(directory_information.st_mode)
            or directory_information.st_uid != os.geteuid()
            or stat.S_IMODE(directory_information.st_mode) != 0o700
        ):
            raise SecurityError("Forge workspace binding root changed")
        binding_fd = os.open(filename, file_flags, dir_fd=directory_fd)
        binding_information = os.fstat(binding_fd)
        if (
            not stat.S_ISREG(binding_information.st_mode)
            or binding_information.st_uid != os.geteuid()
            or binding_information.st_nlink != 1
            or stat.S_IMODE(binding_information.st_mode) != 0o600
            or binding_information.st_size > MAX_WORKSPACE_BINDING_BYTES
        ):
            raise SecurityError("Forge workspace binding file is unsafe")
        chunks: list[bytes] = []
        remaining = MAX_WORKSPACE_BINDING_BYTES + 1
        while remaining > 0:
            chunk = os.read(binding_fd, min(remaining, 4096))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_WORKSPACE_BINDING_BYTES:
            raise SecurityError("Forge workspace binding file is oversized")
    except SecurityError:
        raise
    except OSError as error:
        raise SecurityError("Forge workspace binding could not be read") from error
    finally:
        if binding_fd >= 0:
            os.close(binding_fd)
        if directory_fd >= 0:
            os.close(directory_fd)

    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SecurityError("Forge workspace binding is invalid") from error
    if (
        not isinstance(document, dict)
        or set(document) != {"version", "session_id", "cwd", "token"}
        or document.get("version") != 1
        or isinstance(document.get("version"), bool)
        or document.get("session_id") != session_id
        or not isinstance(document.get("token"), str)
        or not hmac.compare_digest(document["token"], token)
        or not isinstance(document.get("cwd"), str)
        or not document["cwd"]
        or "\x00" in document["cwd"]
    ):
        raise SecurityError("Forge workspace binding did not authenticate")
    try:
        workspace_input = Path(document["cwd"])
        workspace = workspace_input.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise SecurityError("Forge workspace binding cwd is unavailable") from error
    if (
        not workspace_input.is_absolute()
        or workspace_input.absolute() != workspace
        or not workspace.is_dir()
    ):
        raise SecurityError("Forge workspace binding cwd is not canonical")
    return workspace


def get_request_service(
    arguments: dict[str, Any],
    *,
    tool_name: str | None = None,
) -> tuple[ForgeService, dict[str, Any]]:
    """Resolve one MCP request to its authenticated Codex task workspace."""

    sanitized = dict(arguments)
    context = sanitized.pop(WORKSPACE_CONTEXT_FIELD, None)
    if tool_name == "forge_start" and sanitized.get("role", DEFAULT_ROLE) == "recovery":
        # Refuse before ForgeService constructs Store, which can create, chmod,
        # migrate, checkpoint, or otherwise touch durable state.
        raise StateError(RECOVERY_DISABLED_MESSAGE)
    if os.environ.get("FORGE_LAUNCH_WORKSPACE") is not None:
        return ForgeService.from_environment(), sanitized
    workspace = _read_workspace_binding(context)
    return ForgeService.from_environment_for_workspace(workspace), sanitized


def get_default_service() -> ForgeService:
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is None:
        _DEFAULT_SERVICE = ForgeService.from_environment()
    return _DEFAULT_SERVICE


__all__ = [
    "ForgeService",
    "MAX_WAIT_SECONDS",
    "ToolExecution",
    "get_default_service",
    "get_request_service",
]
