"""Canonical Git identities and fail-closed Forge launch boundaries."""

from __future__ import annotations

import hashlib
import os
import re
import selectors
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .contracts import MAX_PROMPT_BYTES


_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_LAUNCH_ROLES = frozenset({"review", "build", "recovery"})
_INCLUDE_SECTION = re.compile(
    rb"^[ \t]*\[[ \t]*include(?:if)?(?:[ \t]*\]|[ \t]+[\"'])",
    re.IGNORECASE | re.MULTILINE,
)
_INCLUDE_KEY = re.compile(
    rb"^[ \t]*include(?:if)?[ \t]*\.",
    re.IGNORECASE | re.MULTILINE,
)
_GIT_TIMEOUT_SECONDS = 10.0
_GIT_TERMINATE_GRACE_SECONDS = 0.2
_GIT_CAPTURE_LIMIT_BYTES = 8 * 1024 * 1024
_GIT_METADATA_LIMIT_BYTES = 1024 * 1024


class BoundaryError(ValueError):
    """Raised when a repository identity or launch crosses a Forge boundary."""


@dataclass(frozen=True, slots=True)
class RepoIdentity:
    """Immutable canonical identity for a Git worktree baseline."""

    top_level: Path
    git_marker: Path
    git_dir: Path
    common_dir: Path
    git_marker_content_hash: str | None
    git_commondir_file: Path | None
    git_commondir_content_hash: str | None
    git_worktree_backpointer: Path | None
    git_worktree_backpointer_content_hash: str | None
    head: str
    status_fingerprint: str
    tracked_content_fingerprint: str
    normalized_status: str

    @property
    def is_clean(self) -> bool:
        return not self.normalized_status


@dataclass(frozen=True, slots=True)
class _GitLayout:
    top_level: Path
    git_marker: Path
    git_dir: Path
    common_dir: Path
    marker_pointer: bytes | None
    commondir_pointer: bytes | None
    worktree_backpointer: bytes | None
    config_fingerprints: tuple[tuple[str, str | None], ...]


@dataclass(frozen=True, slots=True)
class _RepoSnapshot:
    top_level: Path
    common_dir: Path
    head: str
    status: bytes
    index_flags: bytes
    tracked_content_fingerprint: str


def _path_text(value: str | os.PathLike[str], label: str) -> str:
    try:
        text = os.fspath(value)
    except TypeError as error:
        raise BoundaryError(f"{label} must be a filesystem path") from error
    if not isinstance(text, str):
        raise BoundaryError(f"{label} must be a text filesystem path")
    if "\0" in text:
        raise BoundaryError(f"{label} contains NUL")
    if ".." in Path(text).parts:
        raise BoundaryError(f"{label} contains parent traversal")
    return text


def _resolve_input_path(
    value: str | os.PathLike[str],
    label: str,
) -> Path:
    text = _path_text(value, label)
    lexical = Path(text)
    try:
        resolved = lexical.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise BoundaryError(f"{label} cannot be resolved") from error

    # The bridge records and admits canonical names only. Even an alias whose target
    # is otherwise permitted is rejected so later receipts cannot name two paths for
    # the same writable tree.
    if lexical.absolute() != resolved:
        raise BoundaryError(f"{label} is a symlink alias")
    if not resolved.is_dir():
        raise BoundaryError(f"{label} must be a directory")
    return resolved


def _read_regular_file_bounded(path: Path, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise BoundaryError(f"{label} cannot be opened safely") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise BoundaryError(f"{label} must be a regular file")
        if metadata.st_size > _GIT_METADATA_LIMIT_BYTES:
            raise BoundaryError(f"{label} exceeds the metadata limit")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(
                descriptor,
                min(65_536, _GIT_METADATA_LIMIT_BYTES + 1 - size),
            )
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > _GIT_METADATA_LIMIT_BYTES:
                raise BoundaryError(f"{label} exceeds the metadata limit")
        return b"".join(chunks)
    except OSError as error:
        raise BoundaryError(f"{label} cannot be read safely") from error
    finally:
        os.close(descriptor)


def _read_optional_regular_file(path: Path, label: str) -> bytes | None:
    try:
        return _read_regular_file_bounded(path, label)
    except FileNotFoundError:
        return None


def _metadata_line(data: bytes, label: str) -> str:
    value = os.fsdecode(data.rstrip(b"\r\n"))
    if not value or "\n" in value or "\r" in value or "\0" in value:
        raise BoundaryError(f"{label} is invalid")
    return value


def _resolve_metadata_directory(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise BoundaryError(f"{label} cannot be resolved") from error
    if not resolved.is_dir():
        raise BoundaryError(f"{label} must be a directory")
    return resolved


def _screen_config(path: Path) -> tuple[str, str | None]:
    data = _read_optional_regular_file(path, "Git config")
    if data is None:
        return str(path), None
    if b"\0" in data:
        raise BoundaryError("Git config contains NUL")
    scan_data = data[3:] if data.startswith(b"\xef\xbb\xbf") else data
    if _INCLUDE_SECTION.search(scan_data) or _INCLUDE_KEY.search(scan_data):
        raise BoundaryError("Git config includes are not allowed")
    return str(path), hashlib.sha256(data).hexdigest()


def _inspect_git_layout(cwd: Path) -> _GitLayout:
    marker: Path | None = None
    for directory in (cwd, *cwd.parents):
        candidate = directory / ".git"
        try:
            candidate_metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        marker = candidate
        break
    if marker is None:
        raise BoundaryError("cwd is not inside a Git worktree")

    marker_pointer: bytes | None = None
    if stat.S_ISDIR(candidate_metadata.st_mode):
        git_dir = _resolve_metadata_directory(marker, "Git directory")
    elif stat.S_ISREG(candidate_metadata.st_mode):
        pointer = _read_regular_file_bounded(marker, "Git directory pointer")
        marker_pointer = pointer
        prefix = b"gitdir: "
        if not pointer.lower().startswith(prefix):
            raise BoundaryError("Git directory pointer is invalid")
        target_text = _metadata_line(pointer[len(prefix) :], "Git directory pointer")
        target = Path(target_text)
        if not target.is_absolute():
            target = marker.parent / target
        git_dir = _resolve_metadata_directory(target, "Git directory")
    else:
        raise BoundaryError("Git worktree marker must be a directory or regular file")

    common_pointer = _read_optional_regular_file(
        git_dir / "commondir",
        "Git common-directory pointer",
    )
    if common_pointer is None:
        common_dir = git_dir
    else:
        common_text = _metadata_line(
            common_pointer,
            "Git common-directory pointer",
        )
        common_path = Path(common_text)
        if not common_path.is_absolute():
            common_path = git_dir / common_path
        common_dir = _resolve_metadata_directory(
            common_path,
            "Git common directory",
        )

    config_paths = tuple(
        dict.fromkeys(
            (
                common_dir / "config",
                git_dir / "config",
                git_dir / "config.worktree",
            )
        )
    )
    return _GitLayout(
        top_level=marker.parent.resolve(strict=True),
        git_marker=marker.resolve(strict=True),
        git_dir=git_dir,
        common_dir=common_dir,
        marker_pointer=marker_pointer,
        commondir_pointer=common_pointer,
        worktree_backpointer=_read_optional_regular_file(
            git_dir / "gitdir",
            "Git worktree back-pointer",
        ),
        config_fingerprints=tuple(_screen_config(path) for path in config_paths),
    )


def _resolve_git_path(raw_path: str, cwd: Path, label: str) -> Path:
    if not raw_path or "\0" in raw_path:
        raise BoundaryError(f"git returned an invalid {label}")
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise BoundaryError(f"git returned an unresolvable {label}") from error
    if not resolved.is_dir():
        raise BoundaryError(f"git returned a non-directory {label}")
    return resolved


def _git_environment() -> dict[str, str]:
    """Ignore ambient Git routing variables that could replace the requested repo."""
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("GIT_")
    }
    environment["LC_ALL"] = "C"
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_SYSTEM"] = os.devnull
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    # Repository-local core.fsmonitor may name an executable hook. Disable it before
    # pre-admission status inspection without changing the required Git argv.
    environment["GIT_CONFIG_COUNT"] = "1"
    environment["GIT_CONFIG_KEY_0"] = "core.fsmonitor"
    environment["GIT_CONFIG_VALUE_0"] = "false"
    return environment


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=_GIT_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass

    # The group may outlive its leader, so always follow with SIGKILL.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=_GIT_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _capture_git_process(
    process: subprocess.Popen[bytes],
) -> tuple[int, bytes, bytes]:
    if process.stdout is None or process.stderr is None:
        raise BoundaryError("Git output pipes are unavailable")
    streams = {process.stdout: "stdout", process.stderr: "stderr"}
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    selector = selectors.DefaultSelector()
    for stream, name in streams.items():
        selector.register(stream, selectors.EVENT_READ, name)
    deadline = time.monotonic() + _GIT_TIMEOUT_SECONDS
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            events = selector.select(remaining)
            if not events:
                raise TimeoutError
            for key, _ in events:
                name = key.data
                buffer = buffers[name]
                read_size = min(
                    65_536,
                    _GIT_CAPTURE_LIMIT_BYTES + 1 - len(buffer),
                )
                chunk = os.read(key.fileobj.fileno(), read_size)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffer.extend(chunk)
                if len(buffer) > _GIT_CAPTURE_LIMIT_BYTES:
                    raise OverflowError

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            raise TimeoutError from error
        return returncode, bytes(buffers["stdout"]), bytes(buffers["stderr"])
    finally:
        selector.close()


def _run_git(cwd: Path, *arguments: str) -> bytes:
    command = ["git", "-C", str(cwd), *arguments]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_git_environment(),
            start_new_session=True,
            close_fds=True,
        )
    except OSError as error:
        raise BoundaryError("git executable is unavailable") from error
    try:
        try:
            returncode, stdout, stderr = _capture_git_process(process)
        except TimeoutError as error:
            _terminate_process_group(process)
            raise BoundaryError("git identity command timed out") from error
        except OverflowError as error:
            _terminate_process_group(process)
            raise BoundaryError("git identity command output exceeded the limit") from error
        except (OSError, ValueError) as error:
            _terminate_process_group(process)
            raise BoundaryError("git identity command could not be captured") from error
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
    if returncode:
        detail = os.fsdecode(stderr).strip().splitlines()
        reason = detail[-1] if detail else f"exit {returncode}"
        raise BoundaryError(f"git identity command failed: {reason}")
    return stdout


def _single_line(output: bytes, label: str) -> str:
    value = os.fsdecode(output.rstrip(b"\r\n"))
    if not value or "\n" in value or "\r" in value or "\0" in value:
        raise BoundaryError(f"git returned an invalid {label}")
    return value


def _normalize_status(output: bytes) -> bytes:
    unix_lines = output.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    records = [record for record in unix_lines.split(b"\n") if record]
    return b"\n".join(sorted(records))


def _normalize_index_flags(output: bytes) -> bytes:
    if output and not output.endswith(b"\0"):
        raise BoundaryError("git returned invalid index flags")
    records = [record for record in output.split(b"\0") if record]
    for record in records:
        if len(record) < 3 or record[1:2] != b" ":
            raise BoundaryError("git returned invalid index flags")
    return b"\0".join(sorted(records))


def _reject_hidden_index_flags(index_flags: bytes) -> None:
    for record in index_flags.split(b"\0"):
        if not record:
            continue
        tag = record[0]
        if tag == ord("S") or ord("a") <= tag <= ord("z"):
            raise BoundaryError("assume-unchanged and skip-worktree are not allowed")


def _digest_field(digest: Any, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _tracked_content_fingerprint(cwd: Path) -> str:
    """Hash actual stage-zero tracked bytes without invoking Git content filters."""

    listing = _run_git(cwd, "ls-files", "--stage", "-z")
    if listing and not listing.endswith(b"\0"):
        raise BoundaryError("git returned an invalid tracked-file listing")
    records = [record for record in listing.split(b"\0") if record]
    digest = hashlib.sha256()
    deadline = time.monotonic() + _GIT_TIMEOUT_SECONDS
    flags = os.O_RDONLY | os.O_NOFOLLOW
    flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        root_descriptor = os.open(cwd, directory_flags)
    except OSError as error:
        raise BoundaryError("tracked content root cannot be opened safely") from error
    try:
        for record in sorted(records):
            try:
                header, relative_path = record.split(b"\t", 1)
                mode, object_id, stage = header.split(b" ")
            except ValueError as error:
                raise BoundaryError("git returned an invalid tracked-file record") from error
            if (
                not relative_path
                or relative_path.startswith(b"/")
                or any(part in {b"", b".", b".."} for part in relative_path.split(b"/"))
                or stage != b"0"
                or mode not in {b"100644", b"100755", b"120000", b"160000"}
                or len(object_id) not in {40, 64}
                or any(character not in b"0123456789abcdef" for character in object_id)
            ):
                raise BoundaryError("git returned an unsupported tracked-file record")
            if time.monotonic() > deadline:
                raise BoundaryError("tracked content inspection timed out")
            _digest_field(digest, record)
            path_text = os.fsdecode(relative_path)

            # A gitlink's tracked value is its stage-zero object id, already framed
            # above. Superproject status independently covers an initialized
            # submodule's worktree state.
            if mode == b"160000":
                _digest_field(digest, b"gitlink")
                continue
            if mode == b"120000":
                try:
                    before = os.stat(
                        path_text,
                        dir_fd=root_descriptor,
                        follow_symlinks=False,
                    )
                    target = os.readlink(path_text, dir_fd=root_descriptor)
                    after = os.stat(
                        path_text,
                        dir_fd=root_descriptor,
                        follow_symlinks=False,
                    )
                except OSError as error:
                    raise BoundaryError("tracked symlink cannot be read safely") from error
                if (
                    not stat.S_ISLNK(before.st_mode)
                    or (before.st_dev, before.st_ino, before.st_ctime_ns)
                    != (after.st_dev, after.st_ino, after.st_ctime_ns)
                ):
                    raise BoundaryError("tracked symlink changed during inspection")
                _digest_field(
                    digest,
                    target if isinstance(target, bytes) else os.fsencode(target),
                )
                continue

            descriptor = None
            try:
                descriptor = os.open(
                    path_text,
                    flags,
                    dir_fd=root_descriptor,
                )
                before = os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode):
                    raise BoundaryError("tracked path is not a regular file")
                content = hashlib.sha256()
                while True:
                    if time.monotonic() > deadline:
                        raise BoundaryError("tracked content inspection timed out")
                    chunk = os.read(descriptor, 65_536)
                    if not chunk:
                        break
                    content.update(chunk)
                held_after = os.fstat(descriptor)
                named_after = os.stat(
                    path_text,
                    dir_fd=root_descriptor,
                    follow_symlinks=False,
                )
                signature = (
                    before.st_dev,
                    before.st_ino,
                    before.st_mode,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                )
                if signature != (
                    held_after.st_dev,
                    held_after.st_ino,
                    held_after.st_mode,
                    held_after.st_size,
                    held_after.st_mtime_ns,
                    held_after.st_ctime_ns,
                ) or signature != (
                    named_after.st_dev,
                    named_after.st_ino,
                    named_after.st_mode,
                    named_after.st_size,
                    named_after.st_mtime_ns,
                    named_after.st_ctime_ns,
                ):
                    raise BoundaryError("tracked file changed during inspection")
                _digest_field(digest, content.digest())
            except OSError as error:
                raise BoundaryError("tracked file cannot be read safely") from error
            finally:
                if descriptor is not None:
                    os.close(descriptor)
    finally:
        os.close(root_descriptor)
    return digest.hexdigest()


def _sample_repo(cwd: Path) -> _RepoSnapshot:
    top_level_text = _single_line(
        _run_git(cwd, "rev-parse", "--show-toplevel"),
        "top level",
    )
    common_dir_text = _single_line(
        _run_git(cwd, "rev-parse", "--git-common-dir"),
        "common Git directory",
    )
    head = _single_line(
        _run_git(cwd, "rev-parse", "HEAD"),
        "HEAD",
    )
    status = _normalize_status(
        _run_git(
            cwd,
            "status",
            "--porcelain=v2",
            "--untracked-files=all",
        )
    )
    index_flags = _normalize_index_flags(
        _run_git(cwd, "ls-files", "-v", "-z")
    )
    tracked_content_fingerprint = _tracked_content_fingerprint(cwd)
    if not _OBJECT_ID.fullmatch(head):
        raise BoundaryError("git returned an invalid HEAD")
    return _RepoSnapshot(
        top_level=_resolve_git_path(top_level_text, cwd, "top level"),
        common_dir=_resolve_git_path(
            common_dir_text,
            cwd,
            "common Git directory",
        ),
        head=head,
        status=status,
        index_flags=index_flags,
        tracked_content_fingerprint=tracked_content_fingerprint,
    )


def _inspect_stable_repo(canonical_cwd: Path) -> tuple[RepoIdentity, _GitLayout]:
    layout_before = _inspect_git_layout(canonical_cwd)
    first = _sample_repo(canonical_cwd)
    layout_between = _inspect_git_layout(canonical_cwd)
    second = _sample_repo(canonical_cwd)
    layout_after = _inspect_git_layout(canonical_cwd)
    if layout_before != layout_between or layout_between != layout_after:
        raise BoundaryError("Git layout or configuration changed during inspection")
    if first != second:
        raise BoundaryError("Git identity changed during inspection")
    if (
        first.top_level != layout_before.top_level
        or first.common_dir != layout_before.common_dir
    ):
        raise BoundaryError("Git identity disagrees with canonical worktree metadata")
    _reject_hidden_index_flags(first.index_flags)
    identity = RepoIdentity(
        top_level=first.top_level,
        git_marker=layout_before.git_marker,
        git_dir=layout_before.git_dir,
        common_dir=first.common_dir,
        git_marker_content_hash=(
            None
            if layout_before.marker_pointer is None
            else hashlib.sha256(layout_before.marker_pointer).hexdigest()
        ),
        git_commondir_file=(
            None
            if layout_before.commondir_pointer is None
            else (layout_before.git_dir / "commondir").resolve(strict=True)
        ),
        git_commondir_content_hash=(
            None
            if layout_before.commondir_pointer is None
            else hashlib.sha256(layout_before.commondir_pointer).hexdigest()
        ),
        git_worktree_backpointer=(
            None
            if layout_before.worktree_backpointer is None
            else (layout_before.git_dir / "gitdir").resolve(strict=True)
        ),
        git_worktree_backpointer_content_hash=(
            None
            if layout_before.worktree_backpointer is None
            else hashlib.sha256(layout_before.worktree_backpointer).hexdigest()
        ),
        head=first.head,
        status_fingerprint=hashlib.sha256(first.status).hexdigest(),
        tracked_content_fingerprint=first.tracked_content_fingerprint,
        normalized_status=os.fsdecode(first.status),
    )
    return identity, layout_before


def inspect_repo(cwd: str | os.PathLike[str]) -> RepoIdentity:
    """Return a stable, twice-sampled canonical Git worktree baseline."""
    canonical_cwd = _resolve_input_path(cwd, "cwd")
    identity, _ = _inspect_stable_repo(canonical_cwd)
    return identity


def _resolve_pointer_target(data: bytes, base: Path, label: str) -> Path:
    target_text = _metadata_line(data, label)
    target = Path(target_text)
    if not target.is_absolute():
        target = base / target
    try:
        return target.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise BoundaryError(f"{label} cannot be resolved") from error


def _validate_external_worktree_registration(layout: _GitLayout) -> None:
    if layout.marker_pointer is None:
        raise BoundaryError("external repository is not a linked Git worktree")
    if layout.commondir_pointer is None:
        raise BoundaryError("linked worktree is missing its common-directory pointer")
    if layout.worktree_backpointer is None:
        raise BoundaryError("linked worktree is missing its worktree back-pointer")

    worktrees_dir = layout.git_dir.parent
    expected_common_dir = worktrees_dir.parent
    if worktrees_dir.name != "worktrees" or expected_common_dir != layout.common_dir:
        raise BoundaryError("linked worktree is not registered under its common Git directory")

    pointed_common_dir = _resolve_pointer_target(
        layout.commondir_pointer,
        layout.git_dir,
        "Git common-directory pointer",
    )
    if not pointed_common_dir.is_dir() or pointed_common_dir != layout.common_dir:
        raise BoundaryError("linked worktree common-directory pointer is inconsistent")

    pointed_marker = _resolve_pointer_target(
        layout.worktree_backpointer,
        layout.git_dir,
        "Git worktree back-pointer",
    )
    if not pointed_marker.is_file() or pointed_marker != layout.git_marker:
        raise BoundaryError("linked worktree back-pointer is inconsistent")


def _is_contained(path: Path, roots: Iterable[Path]) -> bool:
    return any(path.is_relative_to(root) for root in roots)


def validate_boundary(
    cwd: str | os.PathLike[str],
    launch_workspace: str | os.PathLike[str],
    *,
    extra_roots: Iterable[str | os.PathLike[str]] = (),
) -> RepoIdentity:
    """Admit a contained repo or an external linked worktree with contained Git data."""
    canonical_cwd = _resolve_input_path(cwd, "cwd")
    roots = [_resolve_input_path(launch_workspace, "launch workspace")]
    roots.extend(
        _resolve_input_path(root, "extra root")
        for root in extra_roots
    )
    identity, layout = _inspect_stable_repo(canonical_cwd)
    if _is_contained(identity.top_level, roots):
        if layout.marker_pointer is not None:
            _validate_external_worktree_registration(layout)
        return identity
    if _is_contained(identity.common_dir, roots):
        _validate_external_worktree_registration(layout)
        return identity
    raise BoundaryError("repository is outside the allowed workspace boundary")


def _validate_prompt(prompt: str) -> None:
    if not isinstance(prompt, str):
        raise BoundaryError("prompt must be a string")
    if not prompt:
        raise BoundaryError("prompt must not be empty")
    if "\0" in prompt:
        raise BoundaryError("prompt contains NUL")
    try:
        size = len(prompt.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise BoundaryError("prompt is not valid UTF-8") from error
    if size > MAX_PROMPT_BYTES:
        raise BoundaryError("prompt exceeds the Forge byte limit")


def _validate_expected_value(value: str, label: str, pattern: re.Pattern[str]) -> None:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise BoundaryError(f"{label} is invalid")


def validate_launch(
    cwd: str | os.PathLike[str],
    launch_workspace: str | os.PathLike[str],
    *,
    role: str,
    expected_head: str,
    prompt: str,
    expected_status_fingerprint: str | None = None,
    expected_tracked_content_fingerprint: str | None = None,
    extra_roots: Iterable[str | os.PathLike[str]] = (),
    allow_dirty_recovery: bool = False,
    allow_dirty_review: bool = False,
    allow_dirty_build: bool = False,
) -> RepoIdentity:
    """Validate a frozen Forge launch and return the current canonical identity."""
    _validate_prompt(prompt)
    if not isinstance(role, str) or role not in _LAUNCH_ROLES:
        raise BoundaryError("role is invalid")
    _validate_expected_value(expected_head, "expected HEAD", _OBJECT_ID)
    if expected_status_fingerprint is not None:
        _validate_expected_value(
            expected_status_fingerprint,
            "expected status fingerprint",
            _FINGERPRINT,
        )
    if expected_tracked_content_fingerprint is not None:
        _validate_expected_value(
            expected_tracked_content_fingerprint,
            "expected tracked-content fingerprint",
            _FINGERPRINT,
        )

    identity = validate_boundary(
        cwd,
        launch_workspace,
        extra_roots=extra_roots,
    )
    if identity.head != expected_head:
        raise BoundaryError("repository HEAD does not match the frozen baseline")
    if (
        expected_status_fingerprint is not None
        and identity.status_fingerprint != expected_status_fingerprint
    ):
        raise BoundaryError("repository status fingerprint changed")
    if (
        expected_tracked_content_fingerprint is not None
        and identity.tracked_content_fingerprint
        != expected_tracked_content_fingerprint
    ):
        raise BoundaryError("repository tracked content changed")

    if not isinstance(allow_dirty_recovery, bool):
        raise BoundaryError("dirty-recovery policy must be boolean")
    if allow_dirty_recovery and role != "recovery":
        raise BoundaryError("dirty-recovery policy is valid only for recovery")
    if not isinstance(allow_dirty_review, bool):
        raise BoundaryError("dirty-review policy must be boolean")
    if allow_dirty_review and role != "review":
        raise BoundaryError("dirty-review policy is valid only for review")
    if not isinstance(allow_dirty_build, bool):
        raise BoundaryError("dirty-build policy must be boolean")
    if allow_dirty_build and role != "build":
        raise BoundaryError("dirty-build policy is valid only for build")
    if not identity.is_clean and not (
        allow_dirty_recovery or allow_dirty_review or allow_dirty_build
    ):
        raise BoundaryError(f"dirty {role} launch is not allowed")
    return identity
