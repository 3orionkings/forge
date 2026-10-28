"""Fail-closed child environment, redaction, and Claude sandbox policy."""

from __future__ import annotations

import errno
import ipaddress
import json
import os
import re
import secrets
import socket
import stat
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


BASE_ENV = {"HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "SHELL", "USER"}
NEVER_INHERIT_PREFIXES = ("FORGE_", "AWS_", "GITHUB_", "GOOGLE_", "AZURE_", "OPENAI_")
ANTHROPIC_AUTH_NAMES = {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}
REDACTION = "[REDACTED]"


class SecurityError(ValueError):
    """Raised when a child policy cannot be represented safely."""


_CAMEL_ACRONYM_BOUNDARY = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_WORD_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_NAME_SEPARATOR = re.compile(r"[^A-Za-z0-9]+")
_SENSITIVE_FINAL_WORDS = {
    "authorization",
    "credential",
    "passphrase",
    "password",
    "secret",
    "token",
}
_SENSITIVE_KEY_SUFFIXES = {
    ("access", "key"),
    ("access", "key", "id"),
    ("api", "key"),
    ("client", "secret"),
    ("private", "key"),
    ("secret", "access", "key"),
    ("secret", "key"),
}
_ASSIGNMENT_PREFIX = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"(?P<key_quote>[\"']?)"
    r"(?P<name>[A-Za-z][A-Za-z0-9_-]*)"
    r"(?P=key_quote)[ \t]*[:=][ \t]*",
)
_AUTHORIZATION_SCHEME = re.compile(
    r"(?P<scheme>bearer|basic|digest)(?P<space>[ \t]+)",
    re.IGNORECASE,
)
_BEARER = re.compile(
    r"(?P<scheme>\bbearer)(?P<space>\s+)(?P<secret>[A-Za-z0-9._~+/=-]{8,})",
    re.IGNORECASE,
)
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----.*?"
    r"-----END(?: [A-Z0-9]+)* PRIVATE KEY-----",
    re.DOTALL,
)
_COMMON_SECRET_SHAPES = (
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{16,}\b"),
    re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
    ),
)
_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def _environment_value(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise SecurityError(f"environment variable {name} must contain text")
    if "\x00" in name or "\x00" in value or "=" in name:
        raise SecurityError(f"environment variable {name!r} is not safe for exec")
    return value


def build_child_environment(
    parent_environment: Mapping[str, str] | None = None,
    *,
    allowed_auth_names: Iterable[str] = (),
) -> dict[str, str]:
    """Return the minimal environment allowed for a Claude child.

    Baseline process variables are copied when present. Anthropic authentication
    variables are copied only when their exact names are explicitly requested.
    Requests for all other variables are ignored, and bridge/cloud prefixes remain
    denied regardless of a caller request.
    """

    parent = os.environ if parent_environment is None else parent_environment
    requested: set[str] = set()
    for name in allowed_auth_names:
        if not isinstance(name, str) or "\x00" in name or "=" in name:
            raise SecurityError("allowed authentication names must be safe text names")
        requested.add(name)

    selected_names = set(BASE_ENV)
    selected_names.update(requested & ANTHROPIC_AUTH_NAMES)
    child: dict[str, str] = {}
    for name in sorted(selected_names):
        if name.startswith(NEVER_INHERIT_PREFIXES):
            continue
        if name in parent:
            child[name] = _environment_value(name, parent[name])
    return child


def _quoted_atom_end(text: str, start: int) -> tuple[int, bool]:
    quote = text[start]
    position = start + 1
    while position < len(text):
        character = text[position]
        if character in "\r\n":
            return position, False
        if character == "\\":
            position += 2
            continue
        position += 1
        if character == quote:
            return position, True
    return len(text), False


def _logical_line_end(text: str, start: int) -> int:
    position = start
    while position < len(text) and text[position] not in "\r\n":
        position += 1
    return position


def _authorization_replacement(text: str, start: int, end: int) -> str:
    scheme = _AUTHORIZATION_SCHEME.match(text, start, end)
    if scheme is None:
        return REDACTION
    return f"{scheme.group('scheme')} {REDACTION}"


def _sensitive_value_span(
    text: str,
    start: int,
    *,
    authorization: bool,
) -> tuple[int, str] | None:
    if start >= len(text) or text[start] in "\r\n":
        return None
    if text[start] in "\"'":
        quote = text[start]
        end, closed = _quoted_atom_end(text, start)
        content_end = end - 1 if closed else end
        replacement = (
            _authorization_replacement(text, start + 1, content_end)
            if authorization
            else REDACTION
        )
        closing_quote = quote if closed else ""
        return end, f"{quote}{replacement}{closing_quote}"
    if authorization:
        end = _logical_line_end(text, start)
        if end == start:
            return None
        return end, _authorization_replacement(text, start, end)

    end = start
    while end < len(text) and text[end] not in " \t\r\n,;}]":
        end += 1
    if end == start:
        return None
    return end, REDACTION


def _redact_sensitive_assignments(text: str) -> str:
    chunks: list[str] = []
    unchanged_start = 0
    search_start = 0
    while match := _ASSIGNMENT_PREFIX.search(text, search_start):
        name = match.group("name")
        normalized_words = _normalized_name_words(name)
        if not _is_sensitive_name_words(normalized_words):
            # Advance only past the candidate's first character so a sensitive
            # assignment nested in its value remains visible to the next search.
            search_start = match.start() + 1
            continue
        authorization = normalized_words[-1] == "authorization"
        value_span = _sensitive_value_span(
            text,
            match.end(),
            authorization=authorization,
        )
        if value_span is None:
            search_start = match.end()
            continue
        value_end, replacement = value_span
        chunks.append(text[unchanged_start : match.end()])
        chunks.append(replacement)
        unchanged_start = value_end
        search_start = value_end
    chunks.append(text[unchanged_start:])
    return "".join(chunks)


def redact_text(text: str) -> str:
    """Conservatively remove common credentials from log-derived text."""

    if not isinstance(text, str):
        raise TypeError("redact_text requires a string")
    redacted = _PRIVATE_KEY_BLOCK.sub(REDACTION, text)
    redacted = _redact_sensitive_assignments(redacted)
    redacted = _BEARER.sub(
        lambda match: f"{match.group('scheme')}{match.group('space')}{REDACTION}",
        redacted,
    )
    for pattern in _COMMON_SECRET_SHAPES:
        redacted = pattern.sub(REDACTION, redacted)
    return redacted


def _normalized_name_words(name: str) -> tuple[str, ...]:
    expanded = _CAMEL_ACRONYM_BOUNDARY.sub(r"\1_\2", name)
    expanded = _CAMEL_WORD_BOUNDARY.sub(r"\1_\2", expanded)
    return tuple(
        word.lower()
        for word in _NAME_SEPARATOR.split(expanded)
        if word
    )


def _is_sensitive_name_words(words: tuple[str, ...]) -> bool:
    if not words:
        return False
    if words[-1] in _SENSITIVE_FINAL_WORDS:
        return True
    if words in {("bearer",), ("key",)}:
        return True
    return any(
        len(words) >= len(suffix) and words[-len(suffix) :] == suffix
        for suffix in _SENSITIVE_KEY_SUFFIXES
    )


def _is_sensitive_name(name: Any) -> bool:
    if not isinstance(name, str):
        return False
    return _is_sensitive_name_words(_normalized_name_words(name))


def redact_sensitive(value: Any) -> Any:
    """Return a redacted copy suitable for MCP summaries and receipts."""

    if isinstance(value, Mapping):
        redacted_mapping: dict[Any, Any] = {}
        for key, item in value.items():
            safe_key = redact_text(key) if isinstance(key, str) else key
            redacted_mapping[safe_key] = (
                REDACTION if _is_sensitive_name(key) else redact_sensitive(item)
            )
        return redacted_mapping
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in value)
    if isinstance(value, set):
        return {redact_sensitive(item) for item in value}
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, bytes):
        return redact_text(value.decode("utf-8", errors="replace")).encode("utf-8")
    return value


def _path_text(path: os.PathLike[str] | str, label: str) -> tuple[Path, str]:
    try:
        raw = os.fspath(path)
    except TypeError as error:
        raise SecurityError(f"{label} must be a filesystem path") from error
    if not isinstance(raw, str):
        raise SecurityError(f"{label} must be a text filesystem path")
    if "\x00" in raw:
        raise SecurityError(f"{label} contains NUL")
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise SecurityError(f"{label} must be absolute")
    if ".." in candidate.parts:
        raise SecurityError(f"{label} contains traversal")
    return candidate, raw


def _canonical_directory(path: os.PathLike[str] | str, label: str) -> Path:
    candidate, _ = _path_text(path, label)
    try:
        canonical = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise SecurityError(f"{label} does not resolve to an existing directory") from error
    if candidate != canonical:
        raise SecurityError(f"{label} must be its canonical path, not an alias")
    if not canonical.is_dir():
        raise SecurityError(f"{label} must be a directory")
    return canonical


def _contained_directories(
    paths: Iterable[os.PathLike[str] | str],
    worktree: Path,
    label: str,
) -> list[str]:
    result: list[str] = []
    seen: set[Path] = set()
    for path in paths:
        canonical = _canonical_directory(path, label)
        if not canonical.is_relative_to(worktree):
            raise SecurityError(f"{label} must remain inside the admitted worktree")
        if canonical in seen:
            raise SecurityError(f"{label} contains a duplicate path")
        seen.add(canonical)
        result.append(str(canonical))
    return result


def _contained_write_paths(
    paths: Iterable[os.PathLike[str] | str],
    worktree: Path,
    label: str,
) -> list[str]:
    """Validate exact existing or creatable write paths under one worktree."""

    result: list[str] = []
    seen: set[Path] = set()
    for path in paths:
        candidate, _ = _path_text(path, label)
        try:
            information = os.stat(candidate, follow_symlinks=False)
        except FileNotFoundError:
            parent = _canonical_directory(candidate.parent, f"{label} parent")
            canonical = parent / candidate.name
            if candidate != canonical:
                raise SecurityError(f"{label} must be its canonical path, not an alias")
        except OSError as error:
            raise SecurityError(f"{label} cannot be inspected") from error
        else:
            try:
                canonical = candidate.resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise SecurityError(f"{label} cannot be resolved") from error
            if (
                candidate != canonical
                or stat.S_ISLNK(information.st_mode)
                or not (
                    stat.S_ISREG(information.st_mode)
                    or stat.S_ISDIR(information.st_mode)
                )
            ):
                raise SecurityError(f"{label} must be one exact regular file or directory")
        if not canonical.is_relative_to(worktree):
            raise SecurityError(f"{label} must remain inside the admitted worktree")
        if canonical in seen:
            raise SecurityError(f"{label} contains a duplicate path")
        seen.add(canonical)
        result.append(str(canonical))
    return result


def _validated_domains(domains: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for domain in domains:
        if not isinstance(domain, str):
            raise SecurityError("network allowlist entries must be domain strings")
        if (
            not domain
            or "\x00" in domain
            or domain != domain.strip()
            or not domain.isascii()
            or domain != domain.lower()
            or len(domain) > 253
            or domain.endswith(".")
            or "." not in domain
        ):
            raise SecurityError(f"invalid network allowlist domain: {domain!r}")
        try:
            ipaddress.ip_address(domain)
        except ValueError:
            pass
        else:
            raise SecurityError("network allowlist entries must not be IP addresses")
        try:
            socket.inet_aton(domain)
        except OSError:
            pass
        else:
            raise SecurityError(
                "network allowlist entries must not be numeric IPv4 aliases"
            )
        labels = domain.split(".")
        if any(_DOMAIN_LABEL.fullmatch(part) is None for part in labels):
            raise SecurityError(f"invalid network allowlist domain: {domain!r}")
        if domain in seen:
            raise SecurityError(f"duplicate network allowlist domain: {domain}")
        seen.add(domain)
        result.append(domain)
    return result


def build_claude_settings(
    role: str,
    admitted_worktree: os.PathLike[str] | str,
    *,
    recovery_write_paths: Iterable[os.PathLike[str] | str] = (),
    deny_write_paths: Iterable[os.PathLike[str] | str] = (),
    deny_read_paths: Iterable[os.PathLike[str] | str] = (),
    allowed_network_domains: Iterable[str] = (),
) -> dict[str, Any]:
    """Build one attempt's immutable, fail-closed Claude sandbox settings."""

    if role not in {"review", "build", "recovery"}:
        raise SecurityError(f"unsupported Claude role: {role!r}")
    worktree = _canonical_directory(admitted_worktree, "admitted worktree")

    recovery_writes = _contained_write_paths(
        recovery_write_paths,
        worktree,
        "recovery write path",
    )
    if role != "recovery" and recovery_writes:
        raise SecurityError("recovery write paths are valid only for recovery jobs")
    if role == "build":
        allow_write = [str(worktree)]
    elif role == "recovery":
        allow_write = recovery_writes
    else:
        allow_write = []

    deny_write = _contained_directories(deny_write_paths, worktree, "deny-write path")
    deny_read = _contained_directories(deny_read_paths, worktree, "deny-read path")
    domains = _validated_domains(allowed_network_domains)
    return {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
            "excludedCommands": [],
            "filesystem": {
                "allowWrite": allow_write,
                "denyWrite": deny_write,
                "denyRead": deny_read,
            },
            "network": {
                "allowedDomains": domains,
                "allowUnixSockets": [],
                "allowAllUnixSockets": False,
                "allowLocalBinding": False,
            },
        }
    }


def _private_directory_info(file_descriptor: int, label: str) -> os.stat_result:
    try:
        info = os.fstat(file_descriptor)
    except OSError as error:
        raise SecurityError(f"could not inspect {label}") from error
    if not stat.S_ISDIR(info.st_mode):
        raise SecurityError(f"{label} must be a directory")
    if info.st_uid != os.geteuid():
        raise SecurityError(f"{label} must be owned by the effective user")
    return info


def _harden_private_directory(file_descriptor: int, label: str) -> None:
    _private_directory_info(file_descriptor, label)
    try:
        os.fchmod(file_descriptor, 0o700)
    except OSError as error:
        raise SecurityError(f"could not make {label} private") from error
    hardened = _private_directory_info(file_descriptor, label)
    if stat.S_IMODE(hardened.st_mode) != 0o700:
        raise SecurityError(f"{label} did not retain mode 0700")


def _private_directory_open_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory:
        raise SecurityError(
            "platform cannot prove no-follow private-directory containment"
        )
    return os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)


def _open_private_root(root: Path) -> int:
    directory_flags = _private_directory_open_flags()
    try:
        root_fd = os.open(root, directory_flags)
    except OSError as error:
        raise SecurityError("settings root is not a safe directory") from error
    try:
        root_info = _private_directory_info(root_fd, "settings root")
        try:
            path_info = os.stat(root, follow_symlinks=False)
        except OSError as error:
            raise SecurityError("settings root path changed during validation") from error
        if not os.path.samestat(root_info, path_info):
            raise SecurityError("settings root path changed during validation")
        _harden_private_directory(root_fd, "settings root")
        return root_fd
    except BaseException:
        os.close(root_fd)
        raise


def _open_private_parent(root: Path, relative_parent: Path) -> int:
    directory_flags = _private_directory_open_flags()
    current_fd = _open_private_root(root)
    try:
        for component in relative_parent.parts:
            if component in {"", ".", ".."}:
                raise SecurityError("settings parent contains an unsafe component")
            try:
                os.mkdir(component, mode=0o700, dir_fd=current_fd)
            except FileExistsError:
                pass
            try:
                next_fd = os.open(
                    component,
                    directory_flags,
                    dir_fd=current_fd,
                )
            except OSError as error:
                raise SecurityError("settings parent contains a symlink or non-directory") from error
            try:
                _harden_private_directory(next_fd, "settings attempt directory")
            except BaseException:
                os.close(next_fd)
                raise
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _private_regular_file_info(file_descriptor: int, label: str) -> os.stat_result:
    try:
        info = os.fstat(file_descriptor)
    except OSError as error:
        raise SecurityError(f"could not inspect {label}") from error
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        raise SecurityError(f"{label} must be a safely owned regular file")
    return info


def _harden_private_regular_file(file_descriptor: int, label: str) -> None:
    _private_regular_file_info(file_descriptor, label)
    try:
        os.fchmod(file_descriptor, 0o600)
    except OSError as error:
        raise SecurityError(f"could not make {label} private") from error
    hardened = _private_regular_file_info(file_descriptor, label)
    if stat.S_IMODE(hardened.st_mode) != 0o600:
        raise SecurityError(f"{label} did not retain mode 0600")


def _settings_temp_prefix(filename: str) -> str:
    return f".{filename}.forge-tmp-"


def _private_temp_info_at(parent_fd: int, name: str) -> os.stat_result:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise SecurityError("could not inspect stale settings temp") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise SecurityError("stale settings temp is not a private owned regular file")
    return info


def _remove_private_temp(parent_fd: int, name: str) -> bool:
    try:
        _private_temp_info_at(parent_fd, name)
    except FileNotFoundError:
        return False
    try:
        os.unlink(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise SecurityError("could not remove private settings temp") from error
    return True


def _fsync_private_parent(parent_fd: int) -> None:
    try:
        os.fsync(parent_fd)
    except OSError as error:
        raise SecurityError("could not durably sync settings directory") from error


def _clean_stale_settings_temps(parent_fd: int, filename: str) -> None:
    prefix = _settings_temp_prefix(filename)
    try:
        entries = os.listdir(parent_fd)
    except OSError as error:
        raise SecurityError("could not inspect settings attempt directory") from error
    removed = False
    for entry in sorted(entries):
        if isinstance(entry, str) and entry.startswith(prefix):
            removed = _remove_private_temp(parent_fd, entry) or removed
    if removed:
        _fsync_private_parent(parent_fd)


def _create_private_settings_temp(parent_fd: int, filename: str) -> tuple[str, int]:
    prefix = _settings_temp_prefix(filename)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    for _ in range(16):
        temp_name = prefix + secrets.token_hex(16)
        try:
            file_fd = os.open(temp_name, flags, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            continue
        except OSError as error:
            raise SecurityError("could not create private settings temp") from error
        try:
            _harden_private_regular_file(file_fd, "settings temp")
        except BaseException:
            os.close(file_fd)
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            raise
        return temp_name, file_fd
    raise SecurityError("could not allocate a unique private settings temp")


def _write_all(file_descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        try:
            written = os.write(file_descriptor, view)
        except OSError as error:
            raise SecurityError("could not write private settings temp") from error
        if written <= 0:
            raise SecurityError("short write while creating private settings")
        view = view[written:]


def write_private_settings(
    path: os.PathLike[str] | str,
    settings: Mapping[str, Any],
    *,
    allowed_root: os.PathLike[str] | str,
) -> Path:
    """Create one private settings file under an explicit canonical state root.

    The file is intentionally create-once. Directory traversal uses directory file
    descriptors with no-follow opens so a symlink cannot redirect the write between
    validation and creation.
    """

    root = _canonical_directory(allowed_root, "settings root")
    candidate, _ = _path_text(path, "settings path")
    if candidate == root or not candidate.is_relative_to(root):
        raise SecurityError("settings path must remain below the settings root")
    relative = candidate.relative_to(root)
    if len(relative.parts) < 2:
        # Each attempt owns a private directory; settings never live in the shared root.
        raise SecurityError("settings path must include a private attempt directory")
    filename = relative.parts[-1]
    relative_parent = Path(*relative.parts[:-1])
    if filename in {"", ".", ".."}:
        raise SecurityError("settings filename is invalid")
    try:
        payload = (
            json.dumps(
                settings,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise SecurityError("settings are not strict JSON") from error

    parent_fd = _open_private_parent(root, relative_parent)
    temp_fd: int | None = None
    temp_name: str | None = None
    try:
        _clean_stale_settings_temps(parent_fd, filename)
        temp_name, temp_fd = _create_private_settings_temp(parent_fd, filename)
        _write_all(temp_fd, payload)
        try:
            os.fsync(temp_fd)
        except OSError as error:
            raise SecurityError("could not durably sync settings temp") from error
        os.close(temp_fd)
        temp_fd = None
        try:
            os.link(
                temp_name,
                filename,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError as error:
            if error.errno == errno.EEXIST:
                raise SecurityError("settings target already exists") from error
            raise SecurityError("could not atomically publish private settings") from error
        _fsync_private_parent(parent_fd)
        _remove_private_temp(parent_fd, temp_name)
        temp_name = None
        _fsync_private_parent(parent_fd)
    except BaseException as error:
        if temp_fd is not None:
            os.close(temp_fd)
        if temp_name is not None:
            try:
                removed = _remove_private_temp(parent_fd, temp_name)
                if removed:
                    _fsync_private_parent(parent_fd)
            except BaseException as cleanup_error:
                raise SecurityError(
                    "settings publication failed and temp cleanup was unsafe"
                ) from error
        raise
    finally:
        os.close(parent_fd)
    return candidate
