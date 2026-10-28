#!/usr/bin/env python3
"""Offline Claude CLI double used by Forge adapter and worker tests."""

from __future__ import annotations

import json
import os
import select
import signal
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any


VERSION = "2.1.248 (Claude Code)"
CONTROL_NAME = "forge-fake-claude-control.json"
MAX_CONTROL_BYTES = 64 * 1024
PROVIDER_ERRORS = {
    "authentication": "Authentication failed",
    "oauth_organization": "OAuth authentication is currently not allowed for this organization",
    "billing": "Credit balance is too low",
    "credits": "Usage credits exhausted",
    "rate_limit": "Rate limit exceeded",
    "model_unavailable": "Requested model was not found",
    "permission": "Permission denied",
    "sandbox_unavailable": "Sandbox unavailable",
    "transient_transport": "Connection reset by peer",
    "protocol": "Malformed provider response",
    "cancellation": "Request cancelled",
    "internal": "Internal provider failure",
}
PROVIDER_ERROR_ENUM = {
    "authentication": "authentication_failed",
    "oauth_organization": "oauth_org_not_allowed",
    "billing": "billing_error",
    "credits": "billing_error",
    "rate_limit": "rate_limit",
    "model_unavailable": "model_not_found",
    "transient_transport": "server_error",
    "protocol": "invalid_request",
    "internal": "unknown",
}
CONTROL: dict[str, Any] = {}

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from forge_lib.processes import ProcessError, process_instance_identity  # noqa: E402


def _load_control() -> dict[str, Any]:
    temporary_root = os.environ.get("TMPDIR")
    if not temporary_root:
        return {}
    path = Path(temporary_root) / CONTROL_NAME
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return {}
    if not payload or len(payload) > MAX_CONTROL_BYTES:
        raise SystemExit("invalid fake-Claude control file size")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SystemExit(f"invalid fake-Claude control JSON: {error}") from error
    if not isinstance(value, dict):
        raise SystemExit("fake-Claude control must be an object")
    return value


def _append_json(control_name: str, payload: Any) -> None:
    destination = CONTROL.get(control_name)
    if not isinstance(destination, str) or not destination:
        return
    with Path(destination).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        stream.write("\n")


def _descendant_manifest_destination() -> str | None:
    injected = os.environ.get("FORGE_TEST_DESCENDANT_MANIFEST")
    if injected:
        return injected
    configured = CONTROL.get("descendant_manifest")
    return configured if isinstance(configured, str) and configured else None


def _append_descendant_manifest(pid: int) -> None:
    destination = _descendant_manifest_destination()
    if destination is None:
        return
    deadline = time.monotonic() + 2
    while True:
        try:
            identity = process_instance_identity(pid)
            process_group_id = os.getpgid(pid)
            break
        except (ProcessError, ProcessLookupError):
            if time.monotonic() >= deadline:
                raise SystemExit("fake descendant identity could not be captured")
            time.sleep(0.005)
    payload = json.dumps(
        {
            "identity": identity,
            "pid": pid,
            "process_group_id": process_group_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    path = Path(destination)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        if os.write(descriptor, payload) != len(payload):
            raise SystemExit("fake descendant manifest write was incomplete")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _reset_descendant_manifest() -> None:
    destination = _descendant_manifest_destination()
    if destination is None:
        return
    path = Path(destination)
    # Each fake provider owns one complete generation. Replacing a prior sealed
    # generation also makes any concurrently admitted older pin fail closed.
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _seal_descendant_manifest() -> None:
    destination = _descendant_manifest_destination()
    if destination is None:
        return
    path = Path(destination)
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fchmod(descriptor, 0o400)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _argument_value(flag: str, default: str) -> str:
    try:
        return sys.argv[sys.argv.index(flag) + 1]
    except (ValueError, IndexError):
        return default


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _delay() -> None:
    delay = float(CONTROL.get("delay_seconds", 0))
    if delay:
        time.sleep(delay)


def _spawn_descendant() -> None:
    if CONTROL.get("spawn_descendant") is not True:
        return
    descendant_code = "import signal\n"
    if CONTROL.get("descendant_ignore_term") is True:
        descendant_code += "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    descendant_code += "while True:\n    signal.pause()\n"
    descendant = subprocess.Popen(
        [sys.executable, "-c", descendant_code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _append_descendant_manifest(descendant.pid)
    _append_json("descendant_log", {"pid": descendant.pid})


def _spawn_double_fork_descendant() -> None:
    if CONTROL.get("spawn_double_fork_descendant") is not True:
        return
    temporary_root = Path(os.environ.get("TMPDIR", "/tmp"))
    daemon_pid_path = temporary_root / f"forge-double-fork-{os.getpid()}.pid"
    daemon_code = (
        "import signal,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "time.sleep(30)"
    )
    helper_code = (
        "import pathlib,subprocess,sys;"
        f"daemon=subprocess.Popen([sys.executable,'-c',{daemon_code!r}],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL,start_new_session=True);"
        f"pathlib.Path({str(daemon_pid_path)!r}).write_text(str(daemon.pid))"
    )
    helper = subprocess.Popen(
        [sys.executable, "-c", helper_code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _append_descendant_manifest(helper.pid)
    deadline = time.monotonic() + 2
    while not daemon_pid_path.exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    if not daemon_pid_path.exists():
        raise SystemExit("fake double-fork daemon did not publish its pid")
    daemon_pid = int(daemon_pid_path.read_text(encoding="ascii"))
    _append_descendant_manifest(daemon_pid)
    helper.wait(timeout=2)
    _append_json("double_fork_log", {"pid": daemon_pid})


def _spawn_preflight_pipe_holder() -> None:
    """Double-fork one same-group daemon that keeps preflight pipes open."""

    if CONTROL.get("preflight_double_fork_pipe_holder") is not True:
        return
    read_descriptor, write_descriptor = os.pipe()
    helper_pid = os.fork()
    if helper_pid == 0:
        try:
            os.close(read_descriptor)
            daemon_pid = os.fork()
            if daemon_pid == 0:
                os.close(write_descriptor)
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                if CONTROL.get("preflight_descendant_close_pipes") is True:
                    os.close(sys.stdout.fileno())
                    os.close(sys.stderr.fileno())
                time.sleep(30)
                os._exit(0)
            os.write(write_descriptor, str(daemon_pid).encode("ascii"))
        finally:
            try:
                os.close(write_descriptor)
            except OSError:
                pass
            os._exit(0)
    os.close(write_descriptor)
    try:
        encoded_pid = os.read(read_descriptor, 32)
    finally:
        os.close(read_descriptor)
    os.waitpid(helper_pid, 0)
    try:
        daemon_pid = int(encoded_pid.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as error:
        raise SystemExit("fake preflight daemon did not publish its pid") from error
    _append_json(
        "preflight_descendant_log",
        {
            "pid": daemon_pid,
            "identity": process_instance_identity(daemon_pid),
            "process_group_id": os.getpgid(daemon_pid),
        },
    )


def _exact_mutation_path(relative: Any) -> Path:
    """Resolve one test-only provider mutation without permitting scope escape."""

    if (
        not isinstance(relative, str)
        or not relative
        or "\0" in relative
        or Path(relative).is_absolute()
        or any(component in {"", ".", ".."} for component in Path(relative).parts)
    ):
        raise SystemExit("invalid fake-Claude mutation path")
    root = Path.cwd().resolve(strict=True)
    candidate = root.joinpath(relative)
    try:
        parent = candidate.parent.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit("fake-Claude mutation parent is unavailable") from error
    if not parent.is_relative_to(root):
        raise SystemExit("fake-Claude mutation escapes its repository")
    return candidate


def _apply_provider_mutations() -> None:
    """Apply bounded offline mutations requested only by the test control file."""

    directories = CONTROL.get("make_directories", [])
    if not isinstance(directories, list) or len(directories) > 64:
        raise SystemExit("invalid fake-Claude directory mutation controls")
    for relative in directories:
        destination = _exact_mutation_path(relative)
        try:
            destination.mkdir()
        except FileExistsError:
            information = os.stat(destination, follow_symlinks=False)
            if not stat.S_ISDIR(information.st_mode):
                raise SystemExit("fake-Claude directory mutation target is not a directory")

    files = CONTROL.get("mutate_files", {})
    if not isinstance(files, dict) or len(files) > 64:
        raise SystemExit("invalid fake-Claude file mutation controls")
    for relative, content in files.items():
        destination = _exact_mutation_path(relative)
        if content is None:
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
            continue
        if not isinstance(content, str) or len(content.encode("utf-8")) > 1024 * 1024:
            raise SystemExit("invalid fake-Claude mutation content")
        destination.write_text(content, encoding="utf-8")

    hardlinks = CONTROL.get("hardlink_files", {})
    if not isinstance(hardlinks, dict) or len(hardlinks) > 16:
        raise SystemExit("invalid fake-Claude hard-link mutation controls")
    root = Path.cwd().resolve(strict=True)
    for relative, raw_source in hardlinks.items():
        destination = _exact_mutation_path(relative)
        if (
            not isinstance(raw_source, str)
            or not raw_source
            or "\0" in raw_source
            or not Path(raw_source).is_absolute()
            or ".." in Path(raw_source).parts
        ):
            raise SystemExit("invalid fake-Claude hard-link source")
        try:
            source = Path(raw_source).resolve(strict=True)
            information = os.stat(source, follow_symlinks=False)
        except (OSError, RuntimeError, ValueError) as error:
            raise SystemExit("fake-Claude hard-link source is unavailable") from error
        if source.is_relative_to(root) or not stat.S_ISREG(information.st_mode):
            raise SystemExit("fake-Claude hard-link source is not external regular data")
        destination.unlink(missing_ok=True)
        os.link(source, destination, follow_symlinks=False)

    operations = CONTROL.get("mutate_git", [])
    if not isinstance(operations, list) or len(operations) > 16:
        raise SystemExit("invalid fake-Claude Git mutation controls")
    for operation in operations:
        if not isinstance(operation, dict) or not isinstance(
            operation.get("operation"), str
        ):
            raise SystemExit("invalid fake-Claude Git mutation")
        kind = operation["operation"]
        if kind == "add" and set(operation) == {"operation", "paths"}:
            paths = operation["paths"]
            if not isinstance(paths, list) or not paths or len(paths) > 64:
                raise SystemExit("invalid fake-Claude Git add mutation")
            exact = [str(_exact_mutation_path(path).relative_to(Path.cwd())) for path in paths]
            command = ["/usr/bin/git", "-C", str(Path.cwd()), "add", "--", *exact]
        elif kind == "commit" and set(operation) == {"operation"}:
            command = [
                "/usr/bin/git",
                "-C",
                str(Path.cwd()),
                "commit",
                "-qm",
                "fake provider mutation",
            ]
        elif kind == "update_ref" and set(operation) == {
            "operation",
            "ref",
            "target",
        }:
            reference = operation["ref"]
            target = operation["target"]
            if (
                not isinstance(reference, str)
                or not reference.startswith("refs/")
                or ".." in Path(reference).parts
                or not isinstance(target, str)
                or not target
            ):
                raise SystemExit("invalid fake-Claude Git ref mutation")
            command = [
                "/usr/bin/git",
                "-C",
                str(Path.cwd()),
                "update-ref",
                reference,
                target,
            ]
        else:
            raise SystemExit("unsupported fake-Claude Git mutation")
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            raise SystemExit("fake-Claude Git mutation failed")


class _FrameReader:
    def __init__(self) -> None:
        self.descriptor = sys.stdin.fileno()
        self.buffer = bytearray()
        self.eof = False

    def read(self, timeout: float | None) -> bytes | None:
        deadline = None if timeout is None else time.monotonic() + max(timeout, 0)
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                frame = bytes(self.buffer[: newline + 1])
                del self.buffer[: newline + 1]
                return frame
            if self.eof:
                if self.buffer:
                    raise SystemExit("fake-Claude stdin frame lacks newline")
                return None
            remaining = None if deadline is None else max(deadline - time.monotonic(), 0)
            ready, _, _ = select.select([self.descriptor], [], [], remaining)
            if not ready:
                return None
            chunk = os.read(self.descriptor, 65_536)
            if not chunk:
                self.eof = True
            else:
                self.buffer.extend(chunk)


def _decode_frame(frame: bytes) -> dict[str, Any]:
    try:
        message = json.loads(frame)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SystemExit(f"invalid fake-Claude stdin JSONL: {error}") from error
    if not isinstance(message, dict):
        raise SystemExit("fake-Claude stdin message must be an object")
    if message.get("type") == "user":
        inner = message.get("message")
        content = inner.get("content") if isinstance(inner, dict) else None
        if (
            set(message)
            != {"type", "session_id", "message", "parent_tool_use_id", "origin"}
            or message.get("session_id") != ""
            or message.get("parent_tool_use_id") is not None
            or message.get("origin") != {"kind": "coordinator"}
            or not isinstance(inner, dict)
            or set(inner) != {"role", "content"}
            or inner.get("role") != "user"
            or not isinstance(content, list)
            or len(content) != 1
            or not isinstance(content[0], dict)
            or set(content[0]) != {"type", "text"}
            or content[0].get("type") != "text"
            or not isinstance(content[0].get("text"), str)
            or not content[0]["text"]
        ):
            raise SystemExit("invalid fake-Claude user message")
    elif message.get("type") == "control_request":
        if (
            set(message) != {"type", "request_id", "request"}
            or not isinstance(message.get("request_id"), str)
            or not message["request_id"]
            or message.get("request") != {"subtype": "interrupt"}
        ):
            raise SystemExit("invalid fake-Claude interrupt message")
    else:
        raise SystemExit("unsupported fake-Claude stdin message")
    return message


def _record_frame(frame: bytes) -> dict[str, Any]:
    message = _decode_frame(frame)
    _append_json("stdin_log", message)
    return message


def _auth_status() -> int:
    _delay()
    override = CONTROL.get("auth_json")
    if isinstance(override, str):
        sys.stdout.write(override)
        if not override.endswith("\n"):
            sys.stdout.write("\n")
    else:
        _emit({"loggedIn": True, "authMethod": "oauth", "apiProvider": "firstParty"})
    return int(CONTROL.get("auth_exit", 0))


def _version() -> int:
    _delay()
    _spawn_preflight_pipe_holder()
    byte_count = int(CONTROL.get("version_bytes", 0))
    version_override = CONTROL.get("version_override")
    version_text = version_override if isinstance(version_override, str) else VERSION
    sys.stdout.write("V" * byte_count if byte_count else version_text)
    sys.stdout.write("\n")
    return int(CONTROL.get("version_exit", 0))


def _stream() -> int:
    _reset_descendant_manifest()
    _spawn_descendant()
    _spawn_double_fork_descendant()
    _seal_descendant_manifest()
    reader = _FrameReader()
    first_frame = reader.read(None)
    if first_frame is None or _record_frame(first_frame).get("type") != "user":
        raise SystemExit("fake Claude requires an initial user message")
    _apply_provider_mutations()
    if CONTROL.get("malformed_event") is True:
        sys.stdout.write("{not-json}\n")
        sys.stdout.flush()
        return 0
    oversized = int(CONTROL.get("oversize_event_bytes", 0))
    if oversized:
        sys.stdout.write("X" * oversized + "\n")
        sys.stdout.flush()
        return 0

    requested_model = _argument_value("--model", "fable")
    observed_model = str(CONTROL.get("observed_model", requested_model))
    cli_accepted_model = str(CONTROL.get("cli_accepted_model", observed_model))
    session_id = _argument_value("--session-id", _argument_value("--resume", str(uuid.uuid4())))
    assistant_text = str(CONTROL.get("assistant_text", "fake response"))
    configured_models = CONTROL.get("assistant_models")
    assistant_models = (
        [str(item) for item in configured_models]
        if isinstance(configured_models, list)
        else []
    )
    first_assistant_model = assistant_models[0] if assistant_models else observed_model
    stderr_text = CONTROL.get("stderr_text")
    if isinstance(stderr_text, str):
        sys.stderr.write(stderr_text)
        sys.stderr.flush()
    stderr_bytes = int(CONTROL.get("stderr_bytes", 0))
    if stderr_bytes:
        sys.stderr.write("X" * stderr_bytes)
        sys.stderr.flush()
    if CONTROL.get("omit_init") is not True:
        _emit(
            {
                "type": "system",
                "subtype": "init",
                "apiKeySource": "none",
                "agents": ["claude", "Explore", "general-purpose", "Plan"],
                "analytics_disabled": False,
                "capabilities": ["interrupt_receipt_v1", "msg_lifecycle_v1"],
                "claude_code_version": "2.1.248",
                "cwd": str(Path.cwd()),
                "fast_mode_disabled_reason": "sdk_opt_in_required",
                "fast_mode_state": "off",
                "memory_paths": {"auto": "/bounded/claude/memory"},
                "messaging_socket_path": "/tmp/cc-socks/bounded.sock",
                "session_id": session_id,
                "model": cli_accepted_model,
                "permissionMode": "dontAsk",
                "product_feedback_disabled": False,
                "tools": _argument_value("--tools", "").split(","),
                "mcp_servers": [],
                "slash_commands": [],
                "output_style": "default",
                "skills": [],
                "plugins": [],
                "uuid": str(uuid.uuid4()),
            }
        )
    if CONTROL.get("emit_lifecycle_events") is True:
        lifecycle_task_id = "agent-bounded-lifecycle"
        lifecycle_tool_id = "toolu-bounded-lifecycle"
        lifecycle_usage = {
            "total_tokens": 123,
            "tool_uses": 2,
            "duration_ms": 456,
        }
        _emit(
            {
                "type": "system",
                "subtype": "task_started",
                "task_id": lifecycle_task_id,
                "tool_use_id": lifecycle_tool_id,
                "description": "identity-canary lifecycle description",
                "subagent_type": "identity-canary-agent",
                "task_type": "local_agent",
                "prompt": "identity-canary lifecycle prompt",
                "is_backgrounded": True,
                "spawn_depth": 1,
                "uuid": str(uuid.uuid4()),
                "session_id": session_id,
            }
        )
        _emit(
            {
                "type": "system",
                "subtype": "background_tasks_changed",
                "tasks": [
                    {
                        "task_id": lifecycle_task_id,
                        "task_type": "local_agent",
                        "description": "identity-canary lifecycle description",
                        "ambient": False,
                    }
                ],
                "uuid": str(uuid.uuid4()),
                "session_id": session_id,
            }
        )
        _emit(
            {
                "type": "system",
                "subtype": "task_progress",
                "task_id": lifecycle_task_id,
                "tool_use_id": lifecycle_tool_id,
                "description": "identity-canary lifecycle progress",
                "subagent_type": "identity-canary-agent",
                "usage": lifecycle_usage,
                "last_tool_name": "mcp__identity-canary__search",
                "summary": "identity-canary lifecycle summary",
                "uuid": str(uuid.uuid4()),
                "session_id": session_id,
            }
        )
        _emit(
            {
                "type": "system",
                "subtype": "task_updated",
                "task_id": lifecycle_task_id,
                "patch": {
                    "status": "completed",
                    "description": "identity-canary lifecycle description",
                    "end_time": 1_780_405_729_183,
                    "total_paused_ms": 0,
                    "is_backgrounded": True,
                },
                "uuid": str(uuid.uuid4()),
                "session_id": session_id,
            }
        )
        _emit(
            {
                "type": "system",
                "subtype": "task_notification",
                "task_id": lifecycle_task_id,
                "tool_use_id": lifecycle_tool_id,
                "status": "completed",
                "output_file": "/tmp/identity-canary-lifecycle-output",
                "summary": "identity-canary lifecycle complete",
                "usage": lifecycle_usage,
                "uuid": str(uuid.uuid4()),
                "session_id": session_id,
            }
        )
        _emit(
            {
                "type": "system",
                "subtype": "thinking_tokens",
                "estimated_tokens": 100,
                "estimated_tokens_delta": 25,
                "uuid": str(uuid.uuid4()),
                "session_id": session_id,
            }
        )
    provider_error = CONTROL.get("provider_error")

    def emit_api_retries() -> None:
        configured_attempts = CONTROL.get(
            "api_retry_attempts",
            1 if provider_error == "transient_transport" else 0,
        )
        attempts = (
            [int(value) for value in configured_attempts]
            if isinstance(configured_attempts, list)
            else list(range(1, int(configured_attempts) + 1))
        )
        for attempt in attempts:
            _emit(
                {
                    "type": "system",
                    "subtype": "api_retry",
                    "session_id": session_id,
                    "attempt": attempt,
                    "max_retries": max(len(attempts), 1),
                    "retry_delay_ms": 1,
                    "error_status": None,
                    "error": str(
                        CONTROL.get(
                            "api_retry_error",
                            (
                                "server_error"
                                if provider_error == "transient_transport"
                                else "unknown"
                            ),
                        )
                    ),
                    "uuid": str(uuid.uuid4()),
                }
            )

    if CONTROL.get("api_retry_after_assistant") is not True:
        emit_api_retries()
    provider_error_enum = PROVIDER_ERROR_ENUM.get(provider_error)
    if provider_error_enum is None:
        _emit(
            {
                "type": "assistant",
                "session_id": session_id,
                "parent_tool_use_id": CONTROL.get("parent_tool_use_id"),
                "uuid": str(uuid.uuid4()),
                "message": {
                    "id": "msg_fake",
                    "type": "message",
                    "role": "assistant",
                    "model": first_assistant_model,
                    "content": [{"type": "text", "text": assistant_text}],
                    "usage": {"input_tokens": 7, "output_tokens": 3},
                },
            }
        )
    else:
        _emit(
            {
                "type": "assistant",
                "session_id": session_id,
                "parent_tool_use_id": None,
                "uuid": str(uuid.uuid4()),
                "error": provider_error_enum,
                "api_error_status": 500,
                "message": {
                    "id": "msg_fake_api_error",
                    "type": "message",
                    "role": "assistant",
                    "model": "<synthetic>",
                    "content": [
                        {"type": "text", "text": PROVIDER_ERRORS[provider_error]}
                    ],
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            }
        )
    if CONTROL.get("api_retry_after_assistant") is True:
        emit_api_retries()
    extra_events = int(CONTROL.get("extra_assistant_events", 0))
    for index in range(extra_events):
        extra_model = (
            assistant_models[index + 1]
            if index + 1 < len(assistant_models)
            else observed_model
        )
        _emit(
            {
                "type": "assistant",
                "session_id": session_id,
                "parent_tool_use_id": None,
                "uuid": str(uuid.uuid4()),
                "message": {
                    "id": f"msg_extra_{index}",
                    "type": "message",
                    "role": "assistant",
                    "model": extra_model,
                    "content": [{"type": "text", "text": f"extra {index}"}],
                    "usage": {"input_tokens": 0, "output_tokens": 1},
                },
            }
        )

    fallback_kind = CONTROL.get("fallback_kind")
    if fallback_kind in {"model_fallback", "model_refusal_fallback"}:
        fallback_event: dict[str, Any] = {
            "type": "system",
            "subtype": fallback_kind,
            "session_id": session_id,
            "original_model": requested_model,
            "fallback_model": str(CONTROL.get("fallback_model", "fable-fallback")),
            "content": "fake fallback",
            "uuid": str(uuid.uuid4()),
        }
        if fallback_kind == "model_fallback":
            fallback_event["trigger"] = str(
                CONTROL.get("fallback_trigger", "model_not_found")
            )
        else:
            fallback_event.update(
                {
                    "trigger": "refusal",
                    "direction": "retry",
                    "request_id": "req_fake_refusal",
                }
            )
        _emit(fallback_event)

    if "rate_limit_utilization" in CONTROL:
        _emit(
            {
                "type": "rate_limit_event",
                "session_id": session_id,
                "uuid": str(uuid.uuid4()),
                "rate_limit_info": {
                    "status": str(CONTROL.get("rate_limit_status", "allowed_warning")),
                    "rateLimitType": "five_hour",
                    "utilization": float(CONTROL["rate_limit_utilization"]),
                },
            }
        )

    if CONTROL.get("abrupt_eof_before_result") is True:
        return 0

    interrupted = False
    interrupt_request_id = None
    exact_interrupt_acknowledged = False
    interrupt_index = 0
    window = min(max(float(CONTROL.get("control_window_seconds", 0)), 0), 5)
    deadline = time.monotonic() + window
    while True:
        frame = reader.read(max(deadline - time.monotonic(), 0))
        if frame is None:
            break
        message = _record_frame(frame)
        if message["type"] == "control_request":
            interrupted = True
            interrupt_request_id = message["request_id"]
            if CONTROL.get("interrupt_malformed_event") is True:
                sys.stdout.write("{malformed-after-interrupt}\n")
                sys.stdout.flush()
                break
            modes = CONTROL.get("interrupt_ack_modes")
            if not isinstance(modes, list) and "emit_interrupt_ack" not in CONTROL:
                # Preserve the original general adapter-fixture behavior: a
                # control request ends the input window immediately when the
                # test has not opted into Task-9 acknowledgement simulation.
                break
            if isinstance(modes, list) and interrupt_index < len(modes):
                mode = modes[interrupt_index]
            else:
                mode = "exact" if CONTROL.get("emit_interrupt_ack") is True else "missing"
            interrupt_index += 1
            if mode not in {"exact", "late", "missing", "wrong"}:
                raise SystemExit("invalid fake interrupt acknowledgement mode")
            if mode == "missing":
                continue
            if (
                mode == "wrong"
                or CONTROL.get("emit_mismatched_interrupt_ack_before_exact") is True
            ):
                _emit(
                    {
                        "type": "control_response",
                        "response": {
                            "subtype": "success",
                            "request_id": "not-the-delivered-request",
                            "response": {},
                        },
                    }
                )
                if mode == "wrong":
                    continue
            interrupt_ack_delay = min(
                max(float(CONTROL.get("interrupt_ack_delay_seconds", 0)), 0),
                30,
            )
            if mode == "late" and interrupt_ack_delay <= 0:
                interrupt_ack_delay = 1.2
            if interrupt_ack_delay:
                time.sleep(interrupt_ack_delay)
            acknowledgement_request_id = str(
                CONTROL.get("interrupt_ack_request_id", interrupt_request_id)
            )
            acknowledgement_subtype = str(
                CONTROL.get("interrupt_ack_subtype", "success")
            )
            response: dict[str, Any] = {
                "subtype": acknowledgement_subtype,
                "request_id": acknowledgement_request_id,
            }
            if acknowledgement_subtype == "success":
                response["response"] = CONTROL.get("interrupt_ack_response", {})
            else:
                response["error"] = str(
                    CONTROL.get("interrupt_ack_error", "interrupt rejected")
                )
            _emit(
                {
                    "type": "control_response",
                    "response": response,
                }
            )
            exact_interrupt_acknowledged = (
                acknowledgement_subtype == "success"
                and acknowledgement_request_id == interrupt_request_id
                and response.get("response") == {}
            )
            if exact_interrupt_acknowledged:
                break

    if exact_interrupt_acknowledged and provider_error is None:
        provider_error = "cancellation"
    if provider_error:
        if provider_error not in PROVIDER_ERRORS:
            raise SystemExit(f"unknown provider_error={provider_error!r}")
        result_text = PROVIDER_ERRORS[provider_error]
        is_error = True
    else:
        is_error = CONTROL.get("result_is_error") is True
        result_text = assistant_text
    api_failure_result = provider_error in PROVIDER_ERROR_ENUM or (
        provider_error is None and CONTROL.get("result_is_error") is True
    )

    num_turns = int(CONTROL.get("num_turns", 1))
    result: dict[str, Any] = {
        "type": "result",
        "subtype": str(
            CONTROL.get(
                "result_subtype",
                "success"
                if api_failure_result or not is_error
                else "error_during_execution",
            )
        ),
        "is_error": is_error,
        "session_id": session_id,
        "uuid": str(uuid.uuid4()),
        "duration_ms": 5,
        "duration_api_ms": 4,
        "stop_reason": None,
        "num_turns": num_turns,
        "total_cost_usd": float(CONTROL.get("total_cost_usd", 0.0123)),
        "permission_denials": [],
        "usage": {
            "input_tokens": 7,
            "output_tokens": 3,
            "cache_read_input_tokens": 1,
            "cache_creation_input_tokens": 2,
            "server_tool_use": {
                "web_search_requests": 0,
                "web_fetch_requests": 0,
            },
            "cache_creation": {
                "ephemeral_5m_input_tokens": 2,
                "ephemeral_1h_input_tokens": 0,
            },
            "service_tier": "standard",
            "iterations": [],
            "speed": "standard",
        },
        "modelUsage": {
            observed_model: {
                "inputTokens": 7,
                "outputTokens": 3,
                "cacheReadInputTokens": 1,
                "cacheCreationInputTokens": 2,
                "webSearchRequests": 0,
                "costUSD": 0.0123,
                "contextWindow": 200000,
                "maxOutputTokens": 32000,
                "canonicalModel": observed_model,
                "provider": "firstParty",
                "costBasis": "list",
            }
        },
    }
    if CONTROL.get("null_total_cost_usd") is True:
        result["total_cost_usd"] = None
    if api_failure_result:
        result["result"] = result_text
        result["api_error_status"] = 500
    elif is_error:
        result["errors"] = [result_text]
        if provider_error == "permission":
            result["permission_denials"] = [
                {
                    "tool_name": "Bash",
                    "tool_use_id": "toolu_fake_denied",
                    "tool_input": {"command": "blocked fake command"},
                }
            ]
    else:
        result["result"] = result_text
    if CONTROL.get("empty_model_usage") is True:
        result["modelUsage"] = {}
    usage_extension = CONTROL.get("usage_extension")
    if isinstance(usage_extension, dict):
        result["usage"].update(usage_extension)
    if num_turns == 0:
        result["total_cost_usd"] = 0
        result["usage"] = {
            "input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 0,
            "server_tool_use": {
                "web_search_requests": 0,
                "web_fetch_requests": 0,
            },
            "service_tier": "standard",
            "cache_creation": {
                "ephemeral_1h_input_tokens": 0,
                "ephemeral_5m_input_tokens": 0,
            },
            "inference_geo": "",
            "iterations": [],
            "speed": "standard",
        }
    extra_usage_models = CONTROL.get("model_usage_models")
    if isinstance(extra_usage_models, list):
        for model_name in extra_usage_models:
            result["modelUsage"][str(model_name)] = dict(
                result["modelUsage"][observed_model]
            )
    _emit(result)
    if CONTROL.get("wait_for_stdin_eof_after_result", True) is True:
        eof_deadline = time.monotonic() + min(
            max(float(CONTROL.get("stdin_eof_timeout_seconds", 10)), 0.1),
            30,
        )
        while not reader.eof:
            frame = reader.read(max(eof_deadline - time.monotonic(), 0))
            if frame is None:
                if reader.eof:
                    break
                raise SystemExit("fake Claude timed out waiting for stdin EOF")
            _record_frame(frame)
        _append_json("stdin_eof_log", {"stdin_eof_after_result": True})
    return int(CONTROL.get("stream_exit", 0))


def main() -> int:
    global CONTROL
    CONTROL = _load_control()
    _append_json("environment_log", dict(os.environ))
    _append_json("argv_log", sys.argv[1:])
    if sys.argv[1:] == ["--version"]:
        return _version()
    if sys.argv[1:] == ["auth", "status", "--json"]:
        return _auth_status()
    return _stream()


if __name__ == "__main__":
    raise SystemExit(main())
