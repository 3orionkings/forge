"""Bounded process primitives for detached Forge workers and Claude children."""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import selectors
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .boundaries import BoundaryError, inspect_repo


PRIVATE_FILE_MODE = 0o600
TERMINATE_GRACE_SECONDS = 0.5
KILL_GRACE_SECONDS = 1.0
PROCESS_GROUP_POLL_SECONDS = 0.01
GIT_METADATA_LIMIT_BYTES = 1024 * 1024
PROCESS_SNAPSHOT_LIMIT_BYTES = 4 * 1024 * 1024
PROCESS_SNAPSHOT_TIMEOUT_SECONDS = 1.0
PROCESS_CPU_TIMEOUT_SECONDS = 1.0
TEST_DESCENDANT_MANIFEST_LIMIT_BYTES = 64 * 1024
TEST_DESCENDANT_MANIFEST_MAX_ENTRIES = 128
TEST_DESCENDANT_MANIFEST_SEALED_MODE = 0o400
WORKER_LAUNCH_HANDSHAKE_TIMEOUT_SECONDS = 15.0


class ProcessError(RuntimeError):
    """Raised when a Forge process cannot be launched or stopped safely."""


class ProviderLaunchError(ProcessError):
    """A post-Popen launch failure whose provider may still be live."""

    def __init__(self, message: str, *, process: subprocess.Popen[bytes]):
        super().__init__(message)
        self.process = process


class ProviderLeaderExitedBeforeSignal(ProcessError):
    """The retained provider leader exited before cancellation could signal."""


class BoundWorkerLaunchError(ProcessError):
    """A launch-gate failure after the exact bound child was reaped."""

    def __init__(self, message: str, *, worker_pid: int):
        super().__init__(message)
        self.worker_pid = worker_pid


@dataclass(frozen=True, slots=True)
class PathPin:
    path: Path
    kind: str
    device: int
    inode: int
    owner: int
    content_hash: str | None = None


@dataclass(frozen=True, slots=True)
class RepositoryLaunchPin:
    worktree: PathPin
    git_marker: PathPin
    git_dir: PathPin
    common_dir: PathPin
    head: str
    status_fingerprint: str
    tracked_content_fingerprint: str
    metadata_files: tuple[PathPin, ...] = ()


@dataclass(frozen=True, slots=True)
class RetainedProcessGroupScope:
    """Unreaped leader authority that pins one newly-created process group."""

    leader_pid: int
    process_group_id: int
    leader_identity: str | None


@dataclass(frozen=True, slots=True)
class CancellationEvidence:
    """Bounded timestamps proving one provider group reached quiescence."""

    provider_pgid: int
    term_sent_at: str | None
    kill_sent_at: str | None
    quiescent_at: str
    provider_exited_at: str | None = None
    termination_mode: str = "signal"
    non_zombie_descendants: int = 0
    stage_failures: tuple["CancellationStageFailure", ...] = ()


@dataclass(frozen=True, slots=True)
class CancellationStageFailure:
    """Bounded evidence that one post-effect receipt could not be persisted."""

    stage: str
    error_type: str
    message: str


def _bounded_cancellation_error(error: BaseException) -> str:
    """Return non-empty UTF-8 text whose encoded form is at most 512 bytes."""

    encoded = str(error).encode("utf-8", errors="replace")
    if len(encoded) > 512:
        encoded = encoded[:512]
        message = encoded.decode("utf-8", errors="ignore")
    else:
        message = encoded.decode("utf-8", errors="strict")
    return message or error.__class__.__name__[:128]


@dataclass(frozen=True, slots=True)
class _ProcessEntry:
    pid: int
    parent_pid: int
    process_group_id: int
    state: str


@dataclass(slots=True)
class _CancellationScope:
    leader_pid: int
    process_group_id: int
    tracked_identities: dict[int, str]
    containment: _TestCancellationContainment
    manifest_pin: dict[str, int | str]


class _TestCancellationContainment:
    """Opaque fake-fixture oracle used only by the non-production test bridge."""

    __slots__ = ("manifest_path", "_seal")

    def __init__(self, manifest_path: Path, seal: object):
        self.manifest_path = manifest_path
        self._seal = seal


class _TestCancellationNamespace:
    """Opaque base path whose exact manifest is derived from one durable job."""

    __slots__ = ("base_path", "_seal")

    def __init__(self, base_path: Path, seal: object):
        self.base_path = base_path
        self._seal = seal


_TEST_CANCELLATION_SEAL = object()


def _test_cancellation_containment(
    manifest_path: str | os.PathLike[str],
) -> object:
    """Create an opaque test oracle; no MCP argument/environment can mint it."""

    try:
        path = Path(manifest_path).expanduser()
    except (TypeError, ValueError) as error:
        raise ProcessError("test descendant manifest path is invalid") from error
    if not path.is_absolute() or ".." in path.parts:
        raise ProcessError("test descendant manifest path must be absolute")
    return _TestCancellationContainment(path, _TEST_CANCELLATION_SEAL)


def _test_cancellation_namespace(
    manifest_base_path: str | os.PathLike[str],
) -> object:
    """Create a fake-only namespace that cannot be minted through MCP or env."""

    try:
        path = Path(manifest_base_path).expanduser()
    except (TypeError, ValueError) as error:
        raise ProcessError("test descendant manifest base path is invalid") from error
    if not path.is_absolute() or ".." in path.parts:
        raise ProcessError("test descendant manifest base path must be absolute")
    return _TestCancellationNamespace(path, _TEST_CANCELLATION_SEAL)


def _bind_test_cancellation_containment(
    containment: object | None,
    job_id: str,
) -> object | None:
    """Bind a fake namespace to one unguessable, job-specific manifest path."""

    if isinstance(containment, _TestCancellationContainment):
        return containment
    if not (
        isinstance(containment, _TestCancellationNamespace)
        and containment._seal is _TEST_CANCELLATION_SEAL
    ):
        return containment
    if (
        not isinstance(job_id, str)
        or not job_id
        or "\x00" in job_id
        or len(job_id.encode("utf-8")) > 256
    ):
        raise ProcessError("test cancellation job identity is invalid")
    capability = hashlib.sha256(
        b"forge-test-cancellation-manifest\0" + job_id.encode("utf-8")
    ).hexdigest()
    manifest_path = containment.base_path.with_name(
        f"{containment.base_path.name}.{capability}"
    )
    return _TestCancellationContainment(manifest_path, _TEST_CANCELLATION_SEAL)


def _test_cancellation_manifest_path(
    containment: object | None,
) -> Path | None:
    """Expose a bound fake path only to the in-repository fake provider adapter."""

    if _is_test_cancellation_containment(containment):
        assert isinstance(containment, _TestCancellationContainment)
        return containment.manifest_path
    return None


def _is_test_cancellation_containment(containment: object | None) -> bool:
    return (
        isinstance(containment, _TestCancellationContainment)
        and containment._seal is _TEST_CANCELLATION_SEAL
    )


def _is_test_cancellation_dependency(containment: object | None) -> bool:
    return _is_test_cancellation_containment(containment) or (
        isinstance(containment, _TestCancellationNamespace)
        and containment._seal is _TEST_CANCELLATION_SEAL
    )


def cancellation_containment_preflight(
    containment: object | None = None,
) -> dict[str, object | None]:
    """Describe whether an OS-enforced recursive cancellation boundary exists."""

    if _is_test_cancellation_dependency(containment):
        return {
            "capability": "recursive_provider_containment",
            "supported": True,
            "authority": "test-injected-descendant-manifest",
            "reason": None,
            "cleanup_scope": "test-authoritative-descendant-manifest",
        }
    return {
        "capability": "recursive_provider_containment",
        "supported": False,
        "authority": None,
        "reason": "unsupported_recursive_containment",
        "cleanup_scope": "process-group-only",
    }


def _path_kind(information: os.stat_result) -> str:
    if stat.S_ISDIR(information.st_mode):
        return "directory"
    if stat.S_ISREG(information.st_mode):
        return "regular"
    raise ProcessError("repository launch path has an unsupported type")


def _validate_path_pin(
    pin: PathPin,
    *,
    label: str,
    allowed_kinds: frozenset[str],
) -> PathPin:
    if not isinstance(pin, PathPin):
        raise ProcessError(f"{label} launch identity is invalid")
    path = pin.path
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or ".." in path.parts
    ):
        raise ProcessError(f"{label} launch path is invalid")
    try:
        canonical = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise ProcessError(f"{label} launch path cannot be resolved") from error
    if canonical != path:
        raise ProcessError(f"{label} launch path is not canonical")
    if pin.kind not in allowed_kinds:
        raise ProcessError(f"{label} launch path type is invalid")
    if any(
        type(value) is not int or value < 0
        for value in (pin.device, pin.inode, pin.owner)
    ):
        raise ProcessError(f"{label} launch identity is invalid")
    if pin.owner != os.geteuid():
        raise ProcessError(f"{label} launch path is not owned by the current user")
    if pin.content_hash is not None and (
        not isinstance(pin.content_hash, str)
        or len(pin.content_hash) != 64
        or any(character not in "0123456789abcdef" for character in pin.content_hash)
    ):
        raise ProcessError(f"{label} content identity is invalid")
    if pin.kind == "directory" and pin.content_hash is not None:
        raise ProcessError(f"{label} directory cannot have a content identity")
    if pin.kind == "regular" and pin.content_hash is None:
        raise ProcessError(f"{label} regular file lacks a content identity")
    return pin


def _matches_path_pin(information: os.stat_result, pin: PathPin) -> bool:
    try:
        kind = _path_kind(information)
    except ProcessError:
        return False
    return (
        kind == pin.kind
        and information.st_dev == pin.device
        and information.st_ino == pin.inode
        and information.st_uid == pin.owner
        and information.st_uid == os.geteuid()
    )


def _descriptor_matches_content(descriptor: int, pin: PathPin) -> bool:
    if pin.content_hash is None:
        return pin.kind == "directory"
    try:
        information = os.fstat(descriptor)
        if not stat.S_ISREG(information.st_mode):
            return False
        if information.st_size > GIT_METADATA_LIMIT_BYTES:
            return False
        data = bytearray()
        offset = 0
        while True:
            chunk = os.pread(
                descriptor,
                min(65_536, GIT_METADATA_LIMIT_BYTES + 1 - len(data)),
                offset,
            )
            if not chunk:
                break
            data.extend(chunk)
            offset += len(chunk)
            if len(data) > GIT_METADATA_LIMIT_BYTES:
                return False
        return hashlib.sha256(data).hexdigest() == pin.content_hash
    except OSError:
        return False


def _open_pinned_path(
    pin: PathPin,
    *,
    dir_fd: int | None = None,
    relative_name: str | None = None,
) -> int:
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    if pin.kind == "directory":
        flags |= os.O_DIRECTORY
    else:
        flags |= getattr(os, "O_NONBLOCK", 0)
    target: str | Path = pin.path if relative_name is None else relative_name
    try:
        descriptor = os.open(target, flags, dir_fd=dir_fd)
    except OSError as error:
        raise ProcessError("repository launch path could not be opened safely") from error
    try:
        held = os.fstat(descriptor)
        current = os.stat(target, dir_fd=dir_fd, follow_symlinks=False)
        if (
            not _matches_path_pin(held, pin)
            or not _matches_path_pin(current, pin)
            or not _descriptor_matches_content(descriptor, pin)
        ):
            raise ProcessError("repository launch path does not match admission")
        if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
            raise ProcessError("repository launch path changed while opened")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_repository_descriptors(
    repository: RepositoryLaunchPin,
    *,
    worktree: Path,
    expected_cwd_identity: tuple[int, int] | None,
) -> dict[str, tuple[int, PathPin]]:
    if not isinstance(repository, RepositoryLaunchPin):
        raise ProcessError("repository launch identity is invalid")
    if (
        not isinstance(repository.head, str)
        or len(repository.head) not in {40, 64}
        or any(character not in "0123456789abcdef" for character in repository.head)
    ):
        raise ProcessError("repository launch HEAD identity is invalid")
    for label, fingerprint in (
        ("status", repository.status_fingerprint),
        ("tracked content", repository.tracked_content_fingerprint),
    ):
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
        ):
            raise ProcessError(f"repository launch {label} identity is invalid")
    worktree_pin = _validate_path_pin(
        repository.worktree,
        label="worktree",
        allowed_kinds=frozenset({"directory"}),
    )
    marker_pin = _validate_path_pin(
        repository.git_marker,
        label="Git marker",
        allowed_kinds=frozenset({"directory", "regular"}),
    )
    git_dir_pin = _validate_path_pin(
        repository.git_dir,
        label="Git directory",
        allowed_kinds=frozenset({"directory"}),
    )
    common_pin = _validate_path_pin(
        repository.common_dir,
        label="Git common directory",
        allowed_kinds=frozenset({"directory"}),
    )
    if worktree_pin.path != worktree or marker_pin.path != worktree / ".git":
        raise ProcessError("repository launch paths do not match the provider cwd")
    if not isinstance(repository.metadata_files, tuple) or len(
        repository.metadata_files
    ) > 2:
        raise ProcessError("repository metadata launch identity is invalid")
    metadata_pins: dict[str, PathPin] = {}
    for index, candidate in enumerate(repository.metadata_files):
        metadata_pin = _validate_path_pin(
            candidate,
            label=f"Git metadata file {index + 1}",
            allowed_kinds=frozenset({"regular"}),
        )
        if (
            metadata_pin.path.parent != git_dir_pin.path
            or metadata_pin.path.name not in {"commondir", "gitdir"}
            or metadata_pin.path.name in metadata_pins
        ):
            raise ProcessError("repository metadata launch path is inconsistent")
        metadata_pins[metadata_pin.path.name] = metadata_pin
    admitted_cwd = _expected_worktree_identity(
        expected_cwd_identity,
        (worktree_pin.device, worktree_pin.inode),
    )
    if admitted_cwd != (worktree_pin.device, worktree_pin.inode):
        raise ProcessError("provider cwd identity does not match repository admission")

    held: dict[str, tuple[int, PathPin]] = {}
    try:
        worktree_descriptor = _open_pinned_path(worktree_pin)
        held["worktree"] = (worktree_descriptor, worktree_pin)
        marker_descriptor = _open_pinned_path(
            marker_pin,
            dir_fd=worktree_descriptor,
            relative_name=".git",
        )
        held["git_marker"] = (marker_descriptor, marker_pin)

        def duplicate_or_open(pin: PathPin) -> int:
            for descriptor, held_pin in held.values():
                if (
                    pin.device,
                    pin.inode,
                    pin.kind,
                ) == (
                    held_pin.device,
                    held_pin.inode,
                    held_pin.kind,
                ):
                    duplicate = os.dup(descriptor)
                    if (
                        not _matches_path_pin(os.fstat(duplicate), pin)
                        or not _descriptor_matches_content(duplicate, pin)
                    ):
                        os.close(duplicate)
                        raise ProcessError(
                            "repository duplicate path does not match admission"
                        )
                    return duplicate
            return _open_pinned_path(pin)

        git_dir_descriptor = duplicate_or_open(git_dir_pin)
        held["git_dir"] = (git_dir_descriptor, git_dir_pin)
        common_descriptor = duplicate_or_open(common_pin)
        held["common_dir"] = (common_descriptor, common_pin)
        for name, metadata_pin in sorted(metadata_pins.items()):
            metadata_descriptor = _open_pinned_path(
                metadata_pin,
                dir_fd=git_dir_descriptor,
                relative_name=name,
            )
            held[f"metadata:{name}"] = (metadata_descriptor, metadata_pin)
        return held
    except BaseException:
        for descriptor, _pin in reversed(tuple(held.values())):
            os.close(descriptor)
        raise


def _validate_held_repository(
    held: Mapping[str, tuple[int, PathPin]],
    *,
    require_worktree_name: bool,
) -> None:
    required_names = {"worktree", "git_marker", "git_dir", "common_dir"}
    if not required_names.issubset(held) or any(
        name not in required_names
        and name not in {"metadata:commondir", "metadata:gitdir"}
        for name in held
    ):
        raise ProcessError("held repository launch identity is incomplete")
    for descriptor, pin in held.values():
        if (
            not _matches_path_pin(os.fstat(descriptor), pin)
            or not _descriptor_matches_content(descriptor, pin)
        ):
            raise ProcessError("held repository path identity changed")
    worktree_descriptor, worktree_pin = held["worktree"]
    _marker_descriptor, marker_pin = held["git_marker"]
    git_dir_descriptor, git_dir_pin = held["git_dir"]
    _common_descriptor, common_pin = held["common_dir"]
    if require_worktree_name:
        worktree_current = os.stat(worktree_pin.path, follow_symlinks=False)
        if not _matches_path_pin(worktree_current, worktree_pin):
            raise ProcessError("provider cwd name changed during launch")
    marker_current = os.stat(
        ".git",
        dir_fd=worktree_descriptor,
        follow_symlinks=False,
    )
    if not _matches_path_pin(marker_current, marker_pin):
        raise ProcessError("Git marker changed during provider launch")
    git_dir_current = os.stat(git_dir_pin.path, follow_symlinks=False)
    if not _matches_path_pin(git_dir_current, git_dir_pin):
        raise ProcessError("Git directory changed during provider launch")
    common_current = os.stat(common_pin.path, follow_symlinks=False)
    if not _matches_path_pin(common_current, common_pin):
        raise ProcessError("Git common directory changed during provider launch")
    for name in ("commondir", "gitdir"):
        key = f"metadata:{name}"
        if key not in held:
            continue
        _descriptor, metadata_pin = held[key]
        metadata_current = os.stat(
            name,
            dir_fd=git_dir_descriptor,
            follow_symlinks=False,
        )
        if not _matches_path_pin(metadata_current, metadata_pin):
            raise ProcessError("Git metadata file changed during provider launch")


def _validate_frozen_repository_state(repository: RepositoryLaunchPin) -> None:
    """Re-sample the complete admitted Git state at the Popen boundary."""

    try:
        identity = inspect_repo(repository.worktree.path)
    except BoundaryError as error:
        raise ProcessError("repository state could not be inspected during launch") from error
    if (
        identity.top_level != repository.worktree.path
        or identity.git_marker != repository.git_marker.path
        or identity.git_dir != repository.git_dir.path
        or identity.common_dir != repository.common_dir.path
        or identity.head != repository.head
        or identity.status_fingerprint != repository.status_fingerprint
        or identity.tracked_content_fingerprint
        != repository.tracked_content_fingerprint
    ):
        raise ProcessError("repository state changed during provider launch")


def _validate_held_frozen_repository(
    held: Mapping[str, tuple[int, PathPin]],
    repository: RepositoryLaunchPin,
) -> None:
    _validate_held_repository(held, require_worktree_name=True)
    _validate_frozen_repository_state(repository)
    _validate_held_repository(held, require_worktree_name=True)


def _worktree_identity(worktree: Path) -> tuple[int, int]:
    """Return a canonical directory identity without following a final symlink."""

    try:
        information = os.stat(worktree, follow_symlinks=False)
        resolved = worktree.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise ProcessError("provider cwd could not be inspected safely") from error
    if not stat.S_ISDIR(information.st_mode) or resolved != worktree:
        raise ProcessError("provider cwd must be a canonical directory")
    return information.st_dev, information.st_ino


def _open_worktree_descriptor(
    worktree: Path,
    expected_identity: tuple[int, int] | None,
) -> tuple[int, tuple[int, int]]:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(worktree, flags)
    except OSError as error:
        raise ProcessError("provider cwd could not be opened without following links") from error
    try:
        information = os.fstat(descriptor)
        if not stat.S_ISDIR(information.st_mode):
            raise ProcessError("provider cwd descriptor is not a directory")
        observed = (information.st_dev, information.st_ino)
        admitted = _expected_worktree_identity(expected_identity, observed)
        if observed != admitted:
            raise ProcessError("provider cwd identity does not match admission")
        current = _worktree_identity(worktree)
        if current != observed:
            raise ProcessError("provider cwd changed while its descriptor was opened")
        return descriptor, admitted
    except BaseException:
        os.close(descriptor)
        raise


def _expected_worktree_identity(
    expected: tuple[int, int] | None,
    observed: tuple[int, int],
) -> tuple[int, int]:
    if expected is None:
        return observed
    if (
        not isinstance(expected, tuple)
        or len(expected) != 2
        or any(type(component) is not int or component < 0 for component in expected)
    ):
        raise ProcessError("provider cwd identity is invalid")
    return expected


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as error:
        raise ProcessError("provider process group could not be inspected") from error
    return True


def _signal_process_group(process_group_id: int, signal_number: int) -> None:
    try:
        os.killpg(process_group_id, signal_number)
    except ProcessLookupError:
        pass
    except OSError as error:
        if error.errno == errno.ESRCH:
            return
        raise ProcessError(
            f"provider process group could not be signalled (errno={error.errno})"
        ) from error


def _signal_cancellation_process_group(
    process_group_id: int,
    signal_number: int,
) -> bool:
    """Signal cancellation scope and report an OS-proven absent target."""

    try:
        os.killpg(process_group_id, signal_number)
        return True
    except ProcessLookupError:
        return False
    except OSError as error:
        if error.errno in {errno.ESRCH, errno.EPERM}:
            return False
        raise ProcessError(
            f"provider process group could not be signalled (errno={error.errno})"
        ) from error


def _wait_for_process_group(
    process: subprocess.Popen[bytes],
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        process.poll()
        if not _process_group_exists(process.pid):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(PROCESS_GROUP_POLL_SECONDS, remaining))


def _close_process_pipes(process: subprocess.Popen[bytes]) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            stream.close()


class _ProviderProcess(subprocess.Popen[bytes]):
    """Provider leader whose wait includes whole-group quiescence."""

    def wait(self, timeout: float | None = None) -> int:
        return_code = super().wait(timeout=timeout)
        terminate_process_group(self)
        return return_code


def _regular_private_append(path: str | os.PathLike[str]):
    try:
        candidate = Path(path)
    except (TypeError, ValueError) as error:
        raise ProcessError("private process log path is invalid") from error
    if not candidate.name or ".." in candidate.parts:
        raise ProcessError("private process log path is invalid")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    file_flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW
    file_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    if candidate.is_absolute():
        anchor = Path(candidate.anchor)
        parts = candidate.parts[1:]
    else:
        anchor = Path.cwd()
        parts = candidate.parts

    directories: list[int] = []
    descriptor: int | None = None
    try:
        directories.append(os.open(anchor, directory_flags))
        for component in parts[:-1]:
            directories.append(
                os.open(component, directory_flags, dir_fd=directories[-1])
            )
        descriptor = os.open(parts[-1], file_flags, dir_fd=directories[-1])
        information = os.fstat(descriptor)
        if not stat.S_ISREG(information.st_mode):
            raise ProcessError("process log is not a regular file")
        if information.st_uid != os.geteuid():
            raise ProcessError("process log is not owned by the current user")
        if stat.S_IMODE(information.st_mode) != PRIVATE_FILE_MODE:
            raise ProcessError("process log is not private")
        if information.st_nlink != 1:
            raise ProcessError("process log must have exactly one hard link")

        current = os.stat(
            parts[-1],
            dir_fd=directories[-1],
            follow_symlinks=False,
        )
        if (current.st_dev, current.st_ino) != (
            information.st_dev,
            information.st_ino,
        ):
            raise ProcessError("process log changed while it was opened")
        stream = os.fdopen(descriptor, "ab", buffering=0)
        descriptor = None
        return stream
    except ProcessError:
        raise
    except (OSError, ValueError) as error:
        raise ProcessError("could not open private process log") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        for directory in reversed(directories):
            os.close(directory)


def spawn_detached_worker(
    bridge_path: str | os.PathLike[str],
    job_id: str,
    *,
    job_role: str,
    dispatch_token: str,
    worker_log: str | os.PathLike[str],
    environment: Mapping[str, str],
    bind_before_release: Callable[[int], None],
) -> int:
    """Spawn one detached worker behind a parent-owned launch gate.

    The child cannot inspect or mutate durable job state until the launcher has
    sampled and persisted its non-reusable process identity. Closing the gate
    without the exact release byte makes the child exit before worker startup.
    """

    bridge = Path(bridge_path)
    if not bridge.is_absolute() or not bridge.is_file():
        raise ProcessError("worker bridge path must be an absolute regular file")
    if not isinstance(job_id, str) or not job_id or any(ch.isspace() for ch in job_id):
        raise ProcessError("worker job id is invalid")
    if (
        not isinstance(dispatch_token, str)
        or len(dispatch_token) != 32
        or any(character not in "0123456789abcdef" for character in dispatch_token)
    ):
        raise ProcessError("worker dispatch token is invalid")
    role_codes = {"review": b"V", "build": b"B", "recovery": b"R"}
    if job_role not in role_codes:
        raise ProcessError("worker job role is invalid")
    if not callable(bind_before_release):
        raise ProcessError("worker launch binding callback is required")
    log = _regular_private_append(worker_log)
    gate_read = -1
    gate_write = -1
    process: subprocess.Popen[bytes] | None = None
    binding_succeeded = False
    try:
        gate_read, gate_write = os.pipe()
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    str(bridge),
                    "--worker",
                    job_id,
                    "--dispatch-token",
                    dispatch_token,
                    "--launch-gate-fd",
                    str(gate_read),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log,
                env=dict(environment),
                start_new_session=True,
                close_fds=True,
                pass_fds=(gate_read,),
                shell=False,
            )
        except OSError as error:
            raise ProcessError("could not start detached Forge worker") from error
        finally:
            os.close(gate_read)
            gate_read = -1
            log.close()

        try:
            bind_before_release(process.pid)
            binding_succeeded = True
            if process.poll() is not None:
                raise ProcessError(
                    "detached worker exited before launch binding release"
                )
            release = b"\x01" + role_codes[job_role]
            if os.write(gate_write, release) != len(release):
                raise ProcessError(
                    "detached worker launch gate release was incomplete"
                )
        except BaseException as launch_error:
            os.close(gate_write)
            gate_write = -1
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                # The child has not passed the gate and therefore has no
                # descendants. A Popen-owned, unreaped child cannot suffer PID
                # reuse before this direct termination and wait.
                process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1.0)
            if binding_succeeded:
                raise BoundWorkerLaunchError(
                    "bound detached worker failed its launch-gate release",
                    worker_pid=process.pid,
                ) from launch_error
            raise
        os.close(gate_write)
        gate_write = -1
        worker_pid = process.pid
        try:
            threading.Thread(
                target=process.wait,
                name=f"forge-worker-reaper-{worker_pid}",
                daemon=True,
            ).start()
        except RuntimeError:
            # Launch success must not be rewritten after the gate opens. Popen
            # retains its ordinary interpreter-exit cleanup as a fallback.
            pass
        return worker_pid
    finally:
        if gate_read >= 0:
            os.close(gate_read)
        if gate_write >= 0:
            os.close(gate_write)
        if not log.closed:
            log.close()


def spawn_provider(
    command: Sequence[str],
    *,
    cwd: str | os.PathLike[str],
    expected_cwd_identity: tuple[int, int] | None = None,
    expected_repository_identity: RepositoryLaunchPin | None = None,
    environment: Mapping[str, str],
    stderr_path: str | os.PathLike[str],
    before_popen: Callable[[], None] | None = None,
) -> subprocess.Popen[bytes]:
    """Spawn Claude in its own process group with structured stdio."""

    if not command or any(not isinstance(argument, str) for argument in command):
        raise ProcessError("provider command must be a non-empty string sequence")
    try:
        worktree = Path(cwd)
    except (TypeError, ValueError) as error:
        raise ProcessError("provider cwd must be an absolute directory") from error
    if not worktree.is_absolute():
        raise ProcessError("provider cwd must be an absolute directory")
    if not hasattr(os, "fchdir"):
        raise ProcessError("provider cwd descriptor launch is unavailable")
    if before_popen is not None and not callable(before_popen):
        raise ProcessError("provider pre-Popen callback is invalid")
    held_repository: dict[str, tuple[int, PathPin]] | None = None
    if expected_repository_identity is None:
        worktree_descriptor, admitted_identity = _open_worktree_descriptor(
            worktree,
            expected_cwd_identity,
        )
        held_descriptors = (worktree_descriptor,)
    else:
        held_repository = _open_repository_descriptors(
            expected_repository_identity,
            worktree=worktree,
            expected_cwd_identity=expected_cwd_identity,
        )
        worktree_descriptor = held_repository["worktree"][0]
        admitted_identity = (
            held_repository["worktree"][1].device,
            held_repository["worktree"][1].inode,
        )
        held_descriptors = tuple(
            descriptor for descriptor, _pin in held_repository.values()
        )

    # The worker owns bounded stderr persistence. Retain this path validation for
    # compatibility, but never let a provider stream unbounded bytes into the file.
    try:
        error_log = _regular_private_append(stderr_path)
        error_log.close()
    except BaseException:
        for descriptor in reversed(held_descriptors):
            os.close(descriptor)
        raise

    def enter_admitted_worktree() -> None:
        try:
            if held_repository is not None:
                _validate_held_repository(
                    held_repository,
                    require_worktree_name=False,
                )
            elif (
                os.fstat(worktree_descriptor).st_dev,
                os.fstat(worktree_descriptor).st_ino,
            ) != admitted_identity:
                raise ProcessError("held provider cwd identity changed")
            os.fchdir(worktree_descriptor)
        finally:
            for descriptor in reversed(held_descriptors):
                os.close(descriptor)

    provider_argv = list(command)
    provider_environment = dict(environment)
    provider_pass_fds = tuple(held_descriptors)
    process: subprocess.Popen[bytes] | None = None
    try:
        try:
            if before_popen is not None:
                before_popen()
            if held_repository is not None:
                _validate_held_frozen_repository(
                    held_repository,
                    expected_repository_identity,
                )
            process = _ProviderProcess(
                provider_argv,
                cwd=None,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=provider_environment,
                start_new_session=True,
                close_fds=True,
                pass_fds=provider_pass_fds,
                preexec_fn=enter_admitted_worktree,
                shell=False,
                bufsize=0,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ProcessError("could not start Claude provider process") from error

        try:
            if held_repository is not None:
                _validate_held_frozen_repository(
                    held_repository,
                    expected_repository_identity,
                )
            else:
                launched_identity = _worktree_identity(worktree)
                if launched_identity != admitted_identity:
                    raise ProcessError("provider cwd identity changed during launch")
        except (OSError, ProcessError) as identity_error:
            cleanup_error = None
            try:
                terminate_process_group(process)
            except ProcessError as error:
                cleanup_error = error
            finally:
                _close_process_pipes(process)
            if cleanup_error is not None:
                raise ProviderLaunchError(
                    "repository changed during launch and process group cleanup failed",
                    process=process,
                ) from cleanup_error
            raise ProcessError("repository changed during provider launch") from identity_error
        return process
    finally:
        for descriptor in reversed(held_descriptors):
            try:
                os.close(descriptor)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise


def terminate_process_group(
    process: subprocess.Popen[bytes],
    *,
    grace_seconds: float = TERMINATE_GRACE_SECONDS,
) -> int | None:
    """Boundedly TERM/KILL a provider group and prove whole-group quiescence."""

    try:
        term_grace = float(grace_seconds)
    except (TypeError, ValueError) as error:
        raise ProcessError("provider termination grace is invalid") from error
    if not math.isfinite(term_grace) or term_grace < 0:
        raise ProcessError("provider termination grace is invalid")

    # The process-group id is pinned to the provider leader by start_new_session.
    # Never return merely because that leader has already exited: descendants can
    # retain the group after Popen.poll()/wait() reports a terminal return code.
    # Reap an exited leader before signalling. On macOS a group containing only
    # that unreaped zombie can reject killpg with EPERM even for the same user.
    process.poll()
    _signal_process_group(process.pid, signal.SIGTERM)
    if _wait_for_process_group(process, term_grace):
        return process.poll()

    _signal_process_group(process.pid, signal.SIGKILL)
    if _wait_for_process_group(process, KILL_GRACE_SECONDS):
        return process.poll()
    raise ProcessError("provider process group did not stop")


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _darwin_process_start(pid: int) -> str:
    """Read Darwin's microsecond process birth identity through libproc."""

    import ctypes

    class ProcBSDInfo(ctypes.Structure):
        _fields_ = [
            ("pbi_flags", ctypes.c_uint32),
            ("pbi_status", ctypes.c_uint32),
            ("pbi_xstatus", ctypes.c_uint32),
            ("pbi_pid", ctypes.c_uint32),
            ("pbi_ppid", ctypes.c_uint32),
            ("pbi_uid", ctypes.c_uint32),
            ("pbi_gid", ctypes.c_uint32),
            ("pbi_ruid", ctypes.c_uint32),
            ("pbi_rgid", ctypes.c_uint32),
            ("pbi_svuid", ctypes.c_uint32),
            ("pbi_svgid", ctypes.c_uint32),
            ("pbi_rfu_1", ctypes.c_uint32),
            ("pbi_comm", ctypes.c_char * 16),
            ("pbi_name", ctypes.c_char * 32),
            ("pbi_nfiles", ctypes.c_uint32),
            ("pbi_pgid", ctypes.c_uint32),
            ("pbi_pjobc", ctypes.c_uint32),
            ("e_tdev", ctypes.c_uint32),
            ("e_tpgid", ctypes.c_uint32),
            ("pbi_nice", ctypes.c_int32),
            ("pbi_start_tvsec", ctypes.c_uint64),
            ("pbi_start_tvusec", ctypes.c_uint64),
        ]

    try:
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        function = library.proc_pidinfo
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        function.restype = ctypes.c_int
        information = ProcBSDInfo()
        size = ctypes.sizeof(information)
        returned = function(pid, 3, 0, ctypes.byref(information), size)
    except (AttributeError, OSError) as error:
        raise ProcessError("worker process identity is unavailable") from error
    if (
        returned != size
        or information.pbi_pid != pid
        or information.pbi_status == 5  # SZOMB
    ):
        raise ProcessError("worker process identity is not live")
    return f"darwin:{information.pbi_start_tvsec}:{information.pbi_start_tvusec}"


def _linux_process_start(pid: int) -> str:
    """Read Linux's boot-scoped start ticks without trusting process text."""

    try:
        stat_payload = Path(f"/proc/{pid}/stat").read_bytes()
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="ascii"
        ).strip()
    except (OSError, UnicodeError) as error:
        raise ProcessError("worker process identity is not live") from error
    if len(stat_payload) > 64 * 1024:
        raise ProcessError("worker process identity exceeded its byte bound")
    close = stat_payload.rfind(b")")
    if close < 0:
        raise ProcessError("worker process identity is malformed")
    fields = stat_payload[close + 2 :].split()
    if (
        len(fields) < 20
        or fields[0].upper().startswith(b"Z")
        or not boot_id
        or len(boot_id) > 128
    ):
        raise ProcessError("worker process identity is malformed")
    try:
        start_ticks = int(fields[19])
    except ValueError as error:
        raise ProcessError("worker process identity is malformed") from error
    if start_ticks < 0:
        raise ProcessError("worker process identity is malformed")
    return f"linux:{boot_id}:{start_ticks}"


def process_instance_identity(pid: int) -> str:
    """Return a bounded boot/process-birth identity that survives no PID reuse."""

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ProcessError("worker process id is invalid")
    if sys.platform == "darwin":
        birth = _darwin_process_start(pid)
    elif sys.platform.startswith("linux"):
        birth = _linux_process_start(pid)
    else:
        raise ProcessError("worker process identity is unsupported on this platform")
    return hashlib.sha256(f"forge-process\0{pid}\0{birth}".encode("ascii")).hexdigest()


def _read_test_descendant_manifest(
    containment: _TestCancellationContainment,
    *,
    expected_pin: Mapping[str, object] | None = None,
) -> tuple[dict[int, tuple[str, int]], dict[str, int | str]]:
    """Read the fake provider's private, bounded authoritative descendant set."""

    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(containment.manifest_path, flags)
    except OSError as error:
        raise ProcessError("authoritative descendant manifest is unavailable") from error
    try:
        information = os.fstat(descriptor)
        if (
            not stat.S_ISREG(information.st_mode)
            or information.st_uid != os.geteuid()
            or information.st_size < 0
            or information.st_size > TEST_DESCENDANT_MANIFEST_LIMIT_BYTES
            or stat.S_IMODE(information.st_mode)
            != TEST_DESCENDANT_MANIFEST_SEALED_MODE
        ):
            raise ProcessError(
                "authoritative descendant manifest is not sealed and private"
            )
        data = bytearray()
        while True:
            remaining = TEST_DESCENDANT_MANIFEST_LIMIT_BYTES + 1 - len(data)
            if remaining <= 0:
                raise ProcessError("authoritative descendant manifest is too large")
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            data.extend(chunk)
        final_information = os.fstat(descriptor)
        if (
            final_information.st_dev != information.st_dev
            or final_information.st_ino != information.st_ino
            or final_information.st_size != information.st_size
            or final_information.st_mtime_ns != information.st_mtime_ns
            or stat.S_IMODE(final_information.st_mode)
            != TEST_DESCENDANT_MANIFEST_SEALED_MODE
        ):
            raise ProcessError(
                "authoritative descendant manifest changed while it was read"
            )
    finally:
        os.close(descriptor)
    if len(data) > TEST_DESCENDANT_MANIFEST_LIMIT_BYTES:
        raise ProcessError("authoritative descendant manifest is too large")
    try:
        lines = bytes(data).decode("utf-8", errors="strict").splitlines()
    except UnicodeDecodeError as error:
        raise ProcessError("authoritative descendant manifest is malformed") from error
    if len(lines) > TEST_DESCENDANT_MANIFEST_MAX_ENTRIES:
        raise ProcessError("authoritative descendant manifest has too many entries")
    manifest_pin: dict[str, int | str] = {
        "device": information.st_dev,
        "inode": information.st_ino,
        "mode": stat.S_IMODE(information.st_mode),
        "size": information.st_size,
        "modified_ns": information.st_mtime_ns,
        "content_hash": hashlib.sha256(bytes(data)).hexdigest(),
    }
    if expected_pin is not None:
        if not isinstance(expected_pin, Mapping) or dict(expected_pin) != manifest_pin:
            raise ProcessError(
                "authoritative descendant manifest does not match its admission seal"
            )
    identities: dict[int, tuple[str, int]] = {}
    for line in lines:
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, RecursionError) as error:
            raise ProcessError("authoritative descendant manifest is malformed") from error
        if not isinstance(entry, dict) or set(entry) != {
            "identity",
            "pid",
            "process_group_id",
        }:
            raise ProcessError("authoritative descendant manifest entry is invalid")
        pid = entry["pid"]
        identity = entry["identity"]
        process_group_id = entry["process_group_id"]
        if (
            isinstance(pid, bool)
            or not isinstance(pid, int)
            or pid <= 0
            or not isinstance(identity, str)
            or len(identity) != 64
            or any(character not in "0123456789abcdef" for character in identity)
            or isinstance(process_group_id, bool)
            or not isinstance(process_group_id, int)
            or process_group_id <= 0
            or pid in identities
        ):
            raise ProcessError("authoritative descendant manifest entry is invalid")
        identities[pid] = (identity, process_group_id)
    return identities, manifest_pin


def _process_snapshot() -> dict[int, _ProcessEntry]:
    """Return one bounded process/ancestry snapshot without invoking a shell."""

    process: subprocess.Popen[bytes] | None = None
    streams: dict[Any, bytearray] = {}
    selector = selectors.DefaultSelector()
    try:
        process = subprocess.Popen(
            ["/bin/ps", "-axo", "pid=,ppid=,pgid=,state="],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
            shell=False,
            bufsize=0,
        )
        if process.stdout is None or process.stderr is None:
            raise ProcessError("provider process group inspection lacks bounded pipes")
        streams = {process.stdout: bytearray(), process.stderr: bytearray()}
        selector.register(process.stdout, selectors.EVENT_READ)
        selector.register(process.stderr, selectors.EVENT_READ)
        deadline = time.monotonic() + PROCESS_SNAPSHOT_TIMEOUT_SECONDS
        total = 0
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProcessError("provider process group inspection timed out")
            ready = selector.select(remaining)
            if not ready:
                raise ProcessError("provider process group inspection timed out")
            for key, _mask in ready:
                try:
                    chunk = os.read(key.fileobj.fileno(), 65_536)
                except OSError as error:
                    raise ProcessError(
                        "provider process group inspection could not be read"
                    ) from error
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total += len(chunk)
                if total > PROCESS_SNAPSHOT_LIMIT_BYTES:
                    raise ProcessError(
                        "provider process group inspection exceeded its byte bound"
                    )
                streams[key.fileobj].extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProcessError("provider process group inspection timed out")
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            raise ProcessError("provider process group inspection timed out") from error
        if return_code != 0:
            raise ProcessError("provider process group inspection failed")
        stdout = bytes(streams[process.stdout])
    except ProcessError:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        raise
    except (OSError, subprocess.SubprocessError) as error:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=1)
        raise ProcessError("provider process group could not be inspected safely") from error
    finally:
        selector.close()
        for stream in streams:
            if not stream.closed:
                stream.close()
    try:
        lines = stdout.decode("ascii", errors="strict").splitlines()
    except UnicodeDecodeError as error:
        raise ProcessError("provider process group inspection was malformed") from error
    snapshot: dict[int, _ProcessEntry] = {}
    for line in lines:
        fields = line.split()
        if len(fields) != 4:
            continue
        try:
            pid = int(fields[0])
            parent_pid = int(fields[1])
            process_group_id = int(fields[2])
        except ValueError as error:
            raise ProcessError("provider process group inspection was malformed") from error
        state = fields[3]
        if (
            pid <= 0
            or parent_pid < 0
            or process_group_id <= 0
            or not state
            or len(state) > 16
            or pid in snapshot
        ):
            raise ProcessError("provider process group inspection was malformed")
        snapshot[pid] = _ProcessEntry(
            pid=pid,
            parent_pid=parent_pid,
            process_group_id=process_group_id,
            state=state,
        )
    return snapshot


def process_instance_is_live(pid: int, expected_identity: str) -> bool:
    """Check one non-zombie process against its non-reusable birth identity."""

    if (
        not isinstance(expected_identity, str)
        or len(expected_identity) != 64
        or any(character not in "0123456789abcdef" for character in expected_identity)
    ):
        return False
    try:
        entry = _process_snapshot().get(pid)
        return (
            entry is not None
            and not _is_zombie(entry)
            and process_instance_identity(pid) == expected_identity
        )
    except ProcessError:
        return False


def _parse_process_cpu_time(value: str) -> float:
    """Parse portable ps(1) cumulative CPU output without locale assumptions."""

    if not isinstance(value, str):
        raise ProcessError("process CPU evidence is malformed")
    text = value.strip()
    if not text or len(text) > 64 or any(character not in "0123456789-:." for character in text):
        raise ProcessError("process CPU evidence is malformed")
    day_count = 0
    if "-" in text:
        day_text, text = text.split("-", 1)
        if not day_text.isdigit():
            raise ProcessError("process CPU evidence is malformed")
        day_count = int(day_text)
    parts = text.split(":")
    if len(parts) == 2:
        hour_count = 0
        minute_text, second_text = parts
    elif len(parts) == 3:
        hour_text, minute_text, second_text = parts
        if not hour_text.isdigit():
            raise ProcessError("process CPU evidence is malformed")
        hour_count = int(hour_text)
    else:
        raise ProcessError("process CPU evidence is malformed")
    if not minute_text.isdigit():
        raise ProcessError("process CPU evidence is malformed")
    try:
        minute_count = int(minute_text)
        second_count = float(second_text)
    except ValueError as error:
        raise ProcessError("process CPU evidence is malformed") from error
    if (
        day_count < 0
        or hour_count < 0
        or minute_count < 0
        or minute_count >= 60
        or not math.isfinite(second_count)
        or second_count < 0
        or second_count >= 60
    ):
        raise ProcessError("process CPU evidence is malformed")
    return day_count * 86400.0 + hour_count * 3600.0 + minute_count * 60.0 + second_count


def sample_process_cpu_seconds(pid: int, expected_identity: str) -> float:
    """Read cumulative CPU for one exact process birth or fail closed."""

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ProcessError("process CPU identity is invalid")
    if (
        not isinstance(expected_identity, str)
        or len(expected_identity) != 64
        or any(character not in "0123456789abcdef" for character in expected_identity)
    ):
        raise ProcessError("process CPU identity is invalid")
    before = process_instance_identity(pid)
    if before != expected_identity:
        raise ProcessError("process CPU identity changed before sampling")
    try:
        completed = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "time="],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=PROCESS_CPU_TIMEOUT_SECONDS,
            check=False,
            shell=False,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ProcessError("process CPU evidence is unavailable") from error
    if (
        completed.returncode != 0
        or len(completed.stdout) > 128
        or len(completed.stderr) > 1024
    ):
        raise ProcessError("process CPU evidence is unavailable")
    try:
        result = _parse_process_cpu_time(completed.stdout.decode("ascii", errors="strict"))
    except UnicodeDecodeError as error:
        raise ProcessError("process CPU evidence is malformed") from error
    after = process_instance_identity(pid)
    if after != expected_identity:
        raise ProcessError("process CPU identity changed during sampling")
    return result


def process_instance_is_definitively_dead(
    pid: int,
    expected_identity: str,
) -> bool:
    """Prove that one exact process birth can no longer execute.

    Unlike ``process_instance_is_live``, inspection failures remain errors so a
    cold dispatcher cannot mistake unavailable OS evidence for worker death.
    PID absence, zombie state, and a different live birth identity are safe
    proofs that the recorded worker instance is gone.
    """

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ProcessError("worker process id is invalid")
    if (
        not isinstance(expected_identity, str)
        or len(expected_identity) != 64
        or any(character not in "0123456789abcdef" for character in expected_identity)
    ):
        raise ProcessError("worker process identity is invalid")
    entry = _process_snapshot().get(pid)
    if entry is None or _is_zombie(entry):
        return True
    return process_instance_identity(pid) != expected_identity


def _process_group_members(process_group_id: int) -> tuple[tuple[int, str], ...]:
    """Return a bounded OS snapshot of one process group, including zombies."""

    if (
        isinstance(process_group_id, bool)
        or not isinstance(process_group_id, int)
        or process_group_id <= 0
    ):
        raise ProcessError("provider process-group identity is invalid")
    return tuple(
        (entry.pid, entry.state)
        for entry in _process_snapshot().values()
        if entry.process_group_id == process_group_id
    )


def _is_zombie(entry: _ProcessEntry) -> bool:
    return entry.state.upper().startswith("Z")


def retain_process_group_scope(
    process: subprocess.Popen[bytes],
) -> RetainedProcessGroupScope:
    """Capture an unreaped, start-new-session leader as non-reusable authority."""

    if not isinstance(process, subprocess.Popen):
        raise ProcessError("retained process-group handle is invalid")
    leader_pid = process.pid
    if (
        isinstance(leader_pid, bool)
        or not isinstance(leader_pid, int)
        or leader_pid <= 0
        or process.returncode is not None
    ):
        raise ProcessError("retained process-group leader is not available")
    entry = _process_snapshot().get(leader_pid)
    if entry is None or entry.process_group_id != leader_pid:
        raise ProcessError("retained process is not its process-group leader")
    identity: str | None = None
    if not _is_zombie(entry):
        try:
            identity = process_instance_identity(leader_pid)
        except ProcessError as error:
            resampled = _process_snapshot().get(leader_pid)
            if (
                resampled is None
                or resampled.process_group_id != leader_pid
                or not _is_zombie(resampled)
            ):
                raise ProcessError(
                    "retained process-group leader identity is unavailable"
                ) from error
    return RetainedProcessGroupScope(
        leader_pid=leader_pid,
        process_group_id=leader_pid,
        leader_identity=identity,
    )


def _retained_group_live_members(
    process: subprocess.Popen[bytes],
    scope: RetainedProcessGroupScope,
) -> tuple[_ProcessEntry, ...]:
    if (
        not isinstance(scope, RetainedProcessGroupScope)
        or scope.leader_pid != process.pid
        or scope.process_group_id != process.pid
        or process.returncode is not None
    ):
        raise ProcessError("retained process-group authority is invalid")
    flags = os.WEXITED | os.WNOHANG | os.WNOWAIT
    for _attempt in range(2):
        try:
            exit_result = os.waitid(os.P_PID, scope.leader_pid, flags)
        except InterruptedError:
            continue
        except (ChildProcessError, OSError) as error:
            raise ProcessError(
                "retained process leader exit could not be observed"
            ) from error
        if exit_result is not None and exit_result.si_pid != scope.leader_pid:
            raise ProcessError("retained process exit identity changed")

        # Sample only after the exact child-exit observation. If waitid reports
        # the retained child exited, that unreaped child still pins its PID and
        # PGID even on platforms whose process listing omits the zombie leader.
        snapshot = _process_snapshot()
        leader = snapshot.get(scope.leader_pid)
        if exit_result is not None:
            return tuple(
                entry
                for entry in snapshot.values()
                if entry.pid != scope.leader_pid
                and entry.process_group_id == scope.process_group_id
                and not _is_zombie(entry)
            )
        if (
            leader is None
            or leader.process_group_id != scope.process_group_id
            or _is_zombie(leader)
        ):
            # The leader may have exited between waitid and the snapshot. Retry
            # from a new waitid observation instead of trusting mixed samples.
            continue
        if scope.leader_identity is None:
            raise ProcessError("retained live leader lacks a birth identity")
        try:
            observed_identity = process_instance_identity(scope.leader_pid)
        except ProcessError:
            # Identity inspection can race the same live-to-zombie transition.
            # Discard this snapshot completely and resample from waitid.
            continue
        if observed_identity != scope.leader_identity:
            raise ProcessError("retained process-group leader identity changed")
        return tuple(
            entry
            for entry in snapshot.values()
            if entry.process_group_id == scope.process_group_id
            and not _is_zombie(entry)
        )
    raise ProcessError("retained process-group leader transition was unstable")


def _signal_retained_process_group(
    process: subprocess.Popen[bytes],
    scope: RetainedProcessGroupScope,
    signal_number: int,
) -> None:
    """Signal only while a fresh retained-scope sample still has live members."""

    if not _retained_group_live_members(process, scope):
        return
    try:
        _signal_process_group(scope.process_group_id, signal_number)
    except ProcessError:
        # macOS can return EPERM for a zombie-only group after a leader exits
        # between the live sample and killpg. Swallow only when a new exact
        # waitid-plus-snapshot sample proves that no group member can execute.
        if _retained_group_live_members(process, scope):
            raise


def _wait_for_retained_group_quiescence(
    process: subprocess.Popen[bytes],
    scope: RetainedProcessGroupScope,
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        if not _retained_group_live_members(process, scope):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(PROCESS_GROUP_POLL_SECONDS, remaining))


def wait_for_retained_process_exit(
    process: subprocess.Popen[bytes],
    scope: RetainedProcessGroupScope,
    timeout_seconds: float,
) -> bool:
    """Observe leader exit with WNOWAIT so its PID/PGID cannot be reused."""

    try:
        timeout = float(timeout_seconds)
    except (TypeError, ValueError) as error:
        raise ProcessError("retained process exit timeout is invalid") from error
    if not math.isfinite(timeout) or timeout < 0:
        raise ProcessError("retained process exit timeout is invalid")
    _retained_group_live_members(process, scope)
    deadline = time.monotonic() + timeout
    flags = os.WEXITED | os.WNOHANG | os.WNOWAIT
    while True:
        try:
            result = os.waitid(os.P_PID, scope.leader_pid, flags)
        except InterruptedError:
            result = None
        except (ChildProcessError, OSError) as error:
            raise ProcessError(
                "retained process leader exit could not be observed"
            ) from error
        if result is not None:
            if result.si_pid != scope.leader_pid:
                raise ProcessError("retained process exit identity changed")
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(PROCESS_GROUP_POLL_SECONDS, remaining))


def terminate_retained_process_group(
    process: subprocess.Popen[bytes],
    scope: RetainedProcessGroupScope,
    *,
    grace_seconds: float = TERMINATE_GRACE_SECONDS,
) -> int:
    """TERM/KILL a retained group without ever signalling a reusable PGID."""

    try:
        term_grace = float(grace_seconds)
    except (TypeError, ValueError) as error:
        raise ProcessError("retained process termination grace is invalid") from error
    if not math.isfinite(term_grace) or term_grace < 0:
        raise ProcessError("retained process termination grace is invalid")

    # Do not poll or wait here. The exact Popen leader remains unreaped, so its
    # PID and process-group id cannot be reused while either signal is sent.
    if _retained_group_live_members(process, scope):
        _signal_retained_process_group(process, scope, signal.SIGTERM)
        if not _wait_for_retained_group_quiescence(process, scope, term_grace):
            _signal_retained_process_group(process, scope, signal.SIGKILL)
            if not _wait_for_retained_group_quiescence(
                process,
                scope,
                KILL_GRACE_SECONDS,
            ):
                raise ProcessError("retained process group did not stop")

    try:
        return_code = process.wait(timeout=KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired as error:
        raise ProcessError("retained process leader could not be reaped") from error

    # No further signals occur after reaping. Waiting for absence can therefore
    # only fail closed if a lingering zombie or a newly-reused group is seen.
    deadline = time.monotonic() + KILL_GRACE_SECONDS
    while _process_group_exists(scope.process_group_id):
        members = _process_group_members(scope.process_group_id)
        if any(not state.upper().startswith("Z") for _pid, state in members):
            raise ProcessError("retained process-group id was reused before absence")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProcessError("retained process group did not become absent")
        time.sleep(min(PROCESS_GROUP_POLL_SECONDS, remaining))
    return return_code


def _tracked_process_remains_live(
    pid: int,
    expected_identity: str,
    expected_process_group_id: int,
) -> bool:
    """Verify identity, accepting only a resampled absent/zombie exit race."""

    try:
        observed_identity = process_instance_identity(pid)
    except ProcessError as error:
        resampled = _process_snapshot().get(pid)
        if resampled is None or _is_zombie(resampled):
            return False
        if resampled.process_group_id != expected_process_group_id:
            raise ProcessError(
                "provider descendant escaped its recorded process group"
            ) from error
        try:
            observed_identity = process_instance_identity(pid)
        except ProcessError as repeat_error:
            raise ProcessError(
                "provider descendant process identity could not be verified"
            ) from repeat_error
    if observed_identity != expected_identity:
        raise ProcessError("provider descendant process identity changed")
    return True


def _refresh_cancellation_scope(
    scope: _CancellationScope,
) -> tuple[_ProcessEntry, ...]:
    manifest, _manifest_pin = _read_test_descendant_manifest(
        scope.containment,
        expected_pin=scope.manifest_pin,
    )
    for pid, (identity, recorded_group) in manifest.items():
        if recorded_group != scope.process_group_id:
            raise ProcessError(
                "provider descendant escaped its recorded process group"
            )
        prior = scope.tracked_identities.get(pid)
        if prior is not None and prior != identity:
            raise ProcessError("provider descendant identity changed in its manifest")
        scope.tracked_identities[pid] = identity
    snapshot = _process_snapshot()
    unregistered = [
        entry
        for entry in snapshot.values()
        if entry.process_group_id == scope.process_group_id
        and entry.pid != scope.leader_pid
        and entry.pid not in scope.tracked_identities
        and not _is_zombie(entry)
    ]
    if unregistered:
        raise ProcessError("provider descendant is absent from its authoritative manifest")
    live: list[_ProcessEntry] = []
    for pid, identity in sorted(scope.tracked_identities.items()):
        entry = snapshot.get(pid)
        if entry is None or _is_zombie(entry):
            continue
        if not _tracked_process_remains_live(
            pid,
            identity,
            scope.process_group_id,
        ):
            continue
        if entry.process_group_id != scope.process_group_id:
            raise ProcessError("provider descendant escaped its recorded process group")
        live.append(entry)
    return tuple(live)


def _wait_for_cancellation_quiescence(
    scope: _CancellationScope,
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        if not _refresh_cancellation_scope(scope):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(PROCESS_GROUP_POLL_SECONDS, remaining))


def _assert_cancellation_group_identity(
    process: subprocess.Popen[bytes],
    expected_process_group_id: int,
    containment: _TestCancellationContainment,
    admission_manifest_pin: Mapping[str, object],
) -> _CancellationScope:
    if (
        isinstance(expected_process_group_id, bool)
        or not isinstance(expected_process_group_id, int)
        or expected_process_group_id <= 0
        or process.pid != expected_process_group_id
    ):
        raise ProcessError("provider process-group identity does not match cancellation")
    if process.poll() is not None:
        raise ProviderLeaderExitedBeforeSignal(
            "provider leader exited before cancellation signalling"
        )
    try:
        observed_process_group = os.getpgid(process.pid)
    except ProcessLookupError as error:
        if process.poll() is not None:
            raise ProviderLeaderExitedBeforeSignal(
                "provider leader exited before cancellation signalling"
            ) from error
        raise ProcessError("provider leader identity is no longer live") from error
    except OSError as error:
        raise ProcessError("provider leader identity could not be inspected") from error
    if observed_process_group != expected_process_group_id:
        raise ProcessError("provider process-group identity changed before cancellation")
    snapshot = _process_snapshot()
    leader = snapshot.get(process.pid)
    if leader is None:
        if process.poll() is not None:
            raise ProviderLeaderExitedBeforeSignal(
                "provider leader exited before cancellation signalling"
            )
        raise ProcessError("provider leader is not bound to its recorded process group")
    if leader.process_group_id != expected_process_group_id:
        raise ProcessError("provider leader is not bound to its recorded process group")
    try:
        leader_identity = process_instance_identity(process.pid)
    except ProcessError as error:
        if process.poll() is not None:
            raise ProviderLeaderExitedBeforeSignal(
                "provider leader exited before cancellation signalling"
            ) from error
        raise
    scope = _CancellationScope(
        leader_pid=process.pid,
        process_group_id=expected_process_group_id,
        tracked_identities={
            process.pid: leader_identity,
        },
        containment=containment,
        manifest_pin=dict(admission_manifest_pin),
    )
    _refresh_cancellation_scope(scope)
    return scope


def cancellation_admission_preflight(
    containment: object | None,
    *,
    expected_process_group_id: int,
) -> dict[str, object | None]:
    """Verify the injected oracle before a cancellation may be admitted."""

    base = cancellation_containment_preflight(containment)
    if base["supported"] is not True:
        return base
    assert isinstance(containment, _TestCancellationContainment)
    try:
        if (
            isinstance(expected_process_group_id, bool)
            or not isinstance(expected_process_group_id, int)
            or expected_process_group_id <= 0
        ):
            raise ProcessError("provider process-group identity is invalid")
        scope = _CancellationScope(
            leader_pid=expected_process_group_id,
            process_group_id=expected_process_group_id,
            tracked_identities={
                expected_process_group_id: process_instance_identity(
                    expected_process_group_id
                )
            },
            containment=containment,
            manifest_pin={},
        )
        leader = _process_snapshot().get(expected_process_group_id)
        if (
            leader is None
            or _is_zombie(leader)
            or leader.process_group_id != expected_process_group_id
        ):
            raise ProcessError("provider leader identity is not live")
        _manifest, manifest_pin = _read_test_descendant_manifest(containment)
        scope.manifest_pin = manifest_pin
        _refresh_cancellation_scope(scope)
    except ProcessError:
        return {
            **base,
            "supported": False,
            "reason": "recursive_containment_scope_invalid",
        }
    return {**base, "_test_manifest_pin": manifest_pin}


def cancel_process_group(
    process: subprocess.Popen[bytes],
    *,
    expected_process_group_id: int,
    containment: object | None = None,
    admission_manifest_pin: Mapping[str, object] | None = None,
    grace_seconds: float = TERMINATE_GRACE_SECONDS,
    stage_callback: Callable[[str, str], None] | None = None,
) -> CancellationEvidence:
    """Interrupt one live, pinned provider group and prove non-zombie quiescence.

    Unlike generic cleanup, this path refuses to signal a numeric PID/PGID after
    losing the live leader identity. The worker must retain the original Popen
    handle; a restarted bridge or worker cannot reconstruct signal authority from
    SQLite alone.
    """

    if not _is_test_cancellation_containment(containment):
        raise ProcessError(
            "recursive provider containment is unsupported; cancellation is paused by policy"
        )
    if admission_manifest_pin is None:
        raise ProcessError(
            "authoritative descendant manifest admission seal is required"
        )
    try:
        term_grace = float(grace_seconds)
    except (TypeError, ValueError) as error:
        raise ProcessError("provider cancellation grace is invalid") from error
    if not math.isfinite(term_grace) or term_grace < 0:
        raise ProcessError("provider cancellation grace is invalid")
    if stage_callback is not None and not callable(stage_callback):
        raise ProcessError("provider cancellation stage callback is invalid")

    stage_failures: list[CancellationStageFailure] = []

    def publish_stage(stage: str, recorded_at: str) -> None:
        if stage_callback is None:
            return
        try:
            stage_callback(stage, recorded_at)
        except Exception as error:
            stage_failures.append(
                CancellationStageFailure(
                    stage=stage,
                    error_type=error.__class__.__name__[:128],
                    message=_bounded_cancellation_error(error),
                )
            )

    assert isinstance(containment, _TestCancellationContainment)
    scope = _assert_cancellation_group_identity(
        process,
        expected_process_group_id,
        containment,
        admission_manifest_pin,
    )
    term_delivered = _signal_cancellation_process_group(
        expected_process_group_id,
        signal.SIGTERM,
    )
    if not term_delivered:
        if process.poll() is not None:
            raise ProviderLeaderExitedBeforeSignal(
                "provider leader exited before cancellation signalling"
            )
        raise ProcessError("provider process group rejected cancellation signalling")
    term_sent_at = _utc_timestamp()
    publish_stage("TERM_SENT", term_sent_at)

    kill_sent_at = None
    if not _wait_for_cancellation_quiescence(
        scope,
        term_grace,
    ):
        # The unreaped provider leader remains present (normally as a zombie)
        # and pins its numeric PID/PGID, preventing reuse before escalation.
        snapshot = _process_snapshot()
        if process.pid not in snapshot:
            raise ProcessError(
                "provider leader identity disappeared before cancellation escalation"
            )
        if snapshot[process.pid].process_group_id != expected_process_group_id:
            raise ProcessError(
                "provider leader identity changed before cancellation escalation"
            )
        kill_delivered = _signal_cancellation_process_group(
            expected_process_group_id,
            signal.SIGKILL,
        )
        if kill_delivered:
            kill_sent_at = _utc_timestamp()
            publish_stage("KILL_SENT", kill_sent_at)
        if not _wait_for_cancellation_quiescence(
            scope,
            KILL_GRACE_SECONDS,
        ):
            raise ProcessError("provider process group retained live descendants")

    quiescent_at = _utc_timestamp()
    # Publish the quiescence marker on the live Popen handle before any callback.
    # If durable stage persistence fails, the worker can reap and close pipes
    # without issuing a second numeric-PGID signal.
    setattr(process, "_forge_cancellation_quiescent", True)
    publish_stage("QUIESCENT", quiescent_at)
    # Reap only after the non-zombie group proof and stage publication. Keeping
    # the leader unreaped until this point prevents PID/PGID reuse between signals.
    process.poll()
    return CancellationEvidence(
        provider_pgid=expected_process_group_id,
        term_sent_at=term_sent_at,
        kill_sent_at=kill_sent_at,
        quiescent_at=quiescent_at,
        stage_failures=tuple(stage_failures),
    )


def prove_provider_exit_after_cancellation_ack(
    process: subprocess.Popen[bytes],
    *,
    expected_process_group_id: int,
    containment: object | None = None,
    admission_manifest_pin: Mapping[str, object] | None = None,
    stage_callback: Callable[[str, str], None] | None = None,
) -> CancellationEvidence:
    """Prove an ACKed provider exited before signalling without reusing its PGID.

    The retained ``Popen`` is the authority for the original leader's exit. The
    fake-only sealed manifest is the authority for the complete descendant set.
    No numeric PID or process group is signalled on this path.
    """

    if not _is_test_cancellation_containment(containment):
        raise ProcessError(
            "recursive provider containment is unsupported; cancellation is paused by policy"
        )
    if admission_manifest_pin is None:
        raise ProcessError(
            "authoritative descendant manifest admission seal is required"
        )
    if (
        isinstance(expected_process_group_id, bool)
        or not isinstance(expected_process_group_id, int)
        or expected_process_group_id <= 0
        or process.pid != expected_process_group_id
    ):
        raise ProcessError("provider process-group identity does not match cancellation")
    if stage_callback is not None and not callable(stage_callback):
        raise ProcessError("provider cancellation stage callback is invalid")
    if process.poll() is None:
        raise ProcessError("provider leader is still live")

    assert isinstance(containment, _TestCancellationContainment)
    manifest, _manifest_pin = _read_test_descendant_manifest(
        containment,
        expected_pin=admission_manifest_pin,
    )
    if expected_process_group_id in manifest:
        raise ProcessError("provider leader is duplicated in its descendant manifest")
    snapshot = _process_snapshot()
    unregistered = [
        entry
        for entry in snapshot.values()
        if entry.process_group_id == expected_process_group_id
        and entry.pid not in manifest
        and not _is_zombie(entry)
    ]
    if unregistered:
        raise ProcessError("provider descendant is absent from its authoritative manifest")
    for pid, (identity, recorded_group) in sorted(manifest.items()):
        if recorded_group != expected_process_group_id:
            raise ProcessError("provider descendant escaped its recorded process group")
        entry = snapshot.get(pid)
        if entry is None or _is_zombie(entry):
            continue
        if not _tracked_process_remains_live(
            pid,
            identity,
            expected_process_group_id,
        ):
            continue
        if entry.process_group_id != expected_process_group_id:
            raise ProcessError("provider descendant escaped its recorded process group")
        raise ProcessError("provider process group retained live descendants")

    stage_failures: list[CancellationStageFailure] = []

    def publish_stage(stage: str, recorded_at: str) -> None:
        if stage_callback is None:
            return
        try:
            stage_callback(stage, recorded_at)
        except Exception as error:
            stage_failures.append(
                CancellationStageFailure(
                    stage=stage,
                    error_type=error.__class__.__name__[:128],
                    message=_bounded_cancellation_error(error),
                )
            )

    provider_exited_at = _utc_timestamp()
    quiescent_at = _utc_timestamp()
    setattr(process, "_forge_cancellation_quiescent", True)
    publish_stage("PROVIDER_EXITED", provider_exited_at)
    publish_stage("QUIESCENT", quiescent_at)
    return CancellationEvidence(
        provider_pgid=expected_process_group_id,
        term_sent_at=None,
        kill_sent_at=None,
        provider_exited_at=provider_exited_at,
        quiescent_at=quiescent_at,
        termination_mode="provider_exit_after_ack",
        stage_failures=tuple(stage_failures),
    )


__all__ = [
    "CancellationEvidence",
    "CancellationStageFailure",
    "BoundWorkerLaunchError",
    "PathPin",
    "ProcessError",
    "ProviderLeaderExitedBeforeSignal",
    "ProviderLaunchError",
    "RepositoryLaunchPin",
    "RetainedProcessGroupScope",
    "cancel_process_group",
    "cancellation_admission_preflight",
    "cancellation_containment_preflight",
    "process_instance_identity",
    "process_instance_is_definitively_dead",
    "process_instance_is_live",
    "retain_process_group_scope",
    "prove_provider_exit_after_cancellation_ack",
    "spawn_detached_worker",
    "spawn_provider",
    "terminate_process_group",
    "terminate_retained_process_group",
    "wait_for_retained_process_exit",
]
