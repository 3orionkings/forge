#!/usr/bin/env python3
"""Bind Forge MCP calls to one canonical workspace per Codex session."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path
from typing import Any


MAX_INPUT_BYTES = 1_048_576
MAX_BINDING_BYTES = 4_096
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
TOOLS = frozenset(
    {
        "forge_preflight",
        "forge_start",
        "forge_status",
        "forge_wait",
        "forge_result",
        "forge_resume",
        "forge_steer",
        "forge_cancel",
        "forge_list",
    }
)
O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
DIRECTORY_FLAGS = os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW


class BindingError(ValueError):
    """Raised when a workspace binding cannot be trusted."""


def _require_secure_open_flags() -> None:
    if not O_DIRECTORY or not O_NOFOLLOW:
        raise BindingError("secure open flags are unavailable")


def _deny() -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "Forge workspace binding denied",
        }
    }


def _allow(tool_input: dict[str, Any], session_id: str, token: str) -> dict[str, Any]:
    updated_input = dict(tool_input)
    updated_input["_forge_workspace"] = {
        "session_id": session_id,
        "token": token,
    }
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": updated_input,
        }
    }


def _read_input() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if not raw or len(raw) > MAX_INPUT_BYTES:
        raise BindingError("invalid hook input")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BindingError("invalid hook input") from error
    if not isinstance(payload, dict):
        raise BindingError("invalid hook input")
    return payload


def _forge_tool(tool_name: object) -> bool:
    if not isinstance(tool_name, str):
        return False
    if tool_name in TOOLS:
        return True
    prefix = "mcp__forge__"
    return tool_name.startswith(prefix) and tool_name[len(prefix) :] in TOOLS


def _canonical_workspace(value: object) -> Path:
    if not isinstance(value, str) or not value or "\0" in value:
        raise BindingError("invalid workspace")
    lexical = Path(value)
    if not lexical.is_absolute() or ".." in lexical.parts:
        raise BindingError("invalid workspace")
    try:
        canonical = lexical.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise BindingError("invalid workspace") from error
    if lexical.absolute() != canonical or not canonical.is_dir():
        raise BindingError("invalid workspace")
    return canonical


def _validated_input(
    payload: dict[str, Any],
) -> tuple[str, Path, dict[str, Any]]:
    if payload.get("hook_event_name") != "PreToolUse":
        raise BindingError("invalid hook event")
    if not _forge_tool(payload.get("tool_name")):
        raise BindingError("invalid tool")

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or SESSION_ID.fullmatch(session_id) is None:
        raise BindingError("invalid session")
    workspace = _canonical_workspace(payload.get("cwd"))

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or "_forge_workspace" in tool_input:
        raise BindingError("invalid tool input")
    return session_id, workspace, tool_input


def _validate_owned_directory(
    descriptor: int,
    *,
    private: bool,
) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise BindingError("unsafe binding directory")
    forbidden = 0o077 if private else 0o022
    if stat.S_IMODE(metadata.st_mode) & forbidden:
        raise BindingError("unsafe binding directory")


def _open_owned_directory(path: Path, *, private: bool) -> int:
    try:
        descriptor = os.open(path, DIRECTORY_FLAGS)
    except OSError as error:
        raise BindingError("unsafe binding directory") from error
    try:
        _validate_owned_directory(descriptor, private=private)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _open_or_create_child(parent: int, name: str, *, private: bool) -> int:
    if not name or name in {".", ".."} or "/" in name or "\0" in name:
        raise BindingError("unsafe binding directory")
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent)
    except FileExistsError:
        pass
    except OSError as error:
        raise BindingError("unsafe binding directory") from error
    try:
        descriptor = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
    except OSError as error:
        raise BindingError("unsafe binding directory") from error
    try:
        _validate_owned_directory(descriptor, private=private)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _default_binding_root() -> tuple[Path, int]:
    home = Path.home()
    if not home.is_absolute() or ".." in home.parts:
        raise BindingError("unsafe home directory")
    try:
        canonical_home = home.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise BindingError("unsafe home directory") from error
    if home.absolute() != canonical_home:
        raise BindingError("unsafe home directory")

    current = _open_owned_directory(canonical_home, private=False)
    path = canonical_home
    try:
        for name, private in (
            (".codex", False),
            ("forge", True),
            ("workspace-bindings", True),
        ):
            child = _open_or_create_child(current, name, private=private)
            os.close(current)
            current = child
            path = path / name
        return path, current
    except Exception:
        os.close(current)
        raise


def _override_binding_root(raw: str) -> tuple[Path, int]:
    if not raw or "\0" in raw:
        raise BindingError("unsafe binding override")
    root = Path(raw)
    if not root.is_absolute() or ".." in root.parts or not root.name:
        raise BindingError("unsafe binding override")
    try:
        parent = root.parent.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise BindingError("unsafe binding override") from error
    if root.parent.absolute() != parent:
        raise BindingError("unsafe binding override")

    parent_descriptor = _open_owned_directory(parent, private=False)
    try:
        root_descriptor = _open_or_create_child(
            parent_descriptor,
            root.name,
            private=True,
        )
    finally:
        os.close(parent_descriptor)
    return parent / root.name, root_descriptor


def _binding_root() -> tuple[Path, int]:
    _require_secure_open_flags()
    override = os.environ.get("FORGE_WORKSPACE_BINDINGS")
    if override is not None:
        return _override_binding_root(override)
    return _default_binding_root()


def _read_bounded_file(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = os.read(descriptor, min(65_536, MAX_BINDING_BYTES + 1 - size))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_BINDING_BYTES:
            raise BindingError("invalid binding")


def _read_binding(
    root_descriptor: int,
    filename: str,
    *,
    session_id: str,
    workspace: Path,
) -> dict[str, Any]:
    try:
        descriptor = os.open(
            filename,
            os.O_RDONLY | O_NOFOLLOW,
            dir_fd=root_descriptor,
        )
    except OSError as error:
        raise BindingError("invalid binding") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o077
            or before.st_size > MAX_BINDING_BYTES
        ):
            raise BindingError("invalid binding")
        raw = _read_bounded_file(descriptor)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise BindingError("invalid binding")
    finally:
        os.close(descriptor)

    try:
        binding = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BindingError("invalid binding") from error
    expected_keys = {"version", "session_id", "cwd", "token"}
    if not isinstance(binding, dict) or set(binding) != expected_keys:
        raise BindingError("invalid binding")
    if (
        binding["version"] != 1
        or binding["session_id"] != session_id
        or binding["cwd"] != str(workspace)
        or not isinstance(binding["token"], str)
        or TOKEN.fullmatch(binding["token"]) is None
    ):
        raise BindingError("invalid binding")
    return binding


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise BindingError("binding write failed")
        offset += written


def _create_or_read_binding(session_id: str, workspace: Path) -> dict[str, Any]:
    _root, root_descriptor = _binding_root()
    filename = hashlib.sha256(session_id.encode("utf-8")).hexdigest() + ".json"
    proposed = {
        "version": 1,
        "session_id": session_id,
        "cwd": str(workspace),
        "token": secrets.token_urlsafe(32),
    }
    if TOKEN.fullmatch(proposed["token"]) is None:
        os.close(root_descriptor)
        raise BindingError("invalid random token")
    payload = (
        json.dumps(proposed, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    temporary_name = f".{filename}.{secrets.token_hex(16)}.tmp"

    try:
        try:
            temporary_descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_NOFOLLOW,
                0o600,
                dir_fd=root_descriptor,
            )
        except OSError as error:
            raise BindingError("binding write failed") from error
        try:
            _write_all(temporary_descriptor, payload)
            os.fsync(temporary_descriptor)
        finally:
            os.close(temporary_descriptor)

        created = False
        try:
            os.link(
                temporary_name,
                filename,
                src_dir_fd=root_descriptor,
                dst_dir_fd=root_descriptor,
                follow_symlinks=False,
            )
            created = True
            os.fsync(root_descriptor)
        except FileExistsError:
            pass
        except OSError as error:
            raise BindingError("binding write failed") from error
        finally:
            try:
                os.unlink(temporary_name, dir_fd=root_descriptor)
            except FileNotFoundError:
                pass

        if created:
            return proposed
        return _read_binding(
            root_descriptor,
            filename,
            session_id=session_id,
            workspace=workspace,
        )
    finally:
        os.close(root_descriptor)


def main() -> int:
    os.umask(0o077)
    try:
        payload = _read_input()
        session_id, workspace, tool_input = _validated_input(payload)
        binding = _create_or_read_binding(session_id, workspace)
        result = _allow(tool_input, session_id, binding["token"])
    except Exception:
        result = _deny()
    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
