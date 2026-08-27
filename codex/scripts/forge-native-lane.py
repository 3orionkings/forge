#!/usr/bin/env python3
"""Owner-private local control for Codex-native Forge lane reservations."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import secrets
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from forge_lib.state import (  # noqa: E402
    NATIVE_RECOVERY_UNSUPPORTED,
    StateError,
    Store,
    canonical_json,
)
from forge_lib.boundaries import BoundaryError, validate_boundary  # noqa: E402
from forge_lib.service import (  # noqa: E402
    _git_dirty_paths,
    _machine_snapshot,
    _measured_wip,
    _repo_id,
)


GUARD = SCRIPT_DIR / "dispatch-guard.sh"
MAX_CONTROL_BYTES = 1024 * 1024
_DYNAMIC_GUARD_FLAGS = {
    "--active-lanes",
    "--heavy-leases",
    "--admission-action",
    "--owns-heavy-lease",
    "--clean-tree",
    "--unmerged-branches",
}
_REPEATED_DYNAMIC_GUARD_FLAGS = {
    "--active-writer-worktree",
    "--parallel-file",
    "--current-file",
    "--file",
}
_SCHEDULER_BINDING_FLAGS = {
    "--base-sha",
    "--lane-type",
    "--weight",
    "--worktree",
}


def _absolute_path(raw: str, label: str) -> Path:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise StateError(f"{label} path is invalid")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate.absolute()


def _read_private_file(path: Path, label: str) -> tuple[bytes, os.stat_result]:
    directory_descriptor: int | None = None
    descriptor: int | None = None
    try:
        parent = path.parent.resolve(strict=True)
        if parent != path.parent:
            raise StateError(f"{label} parent is not canonical")
        directory_descriptor = os.open(
            parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        descriptor = os.open(
            path.name,
            os.O_RDONLY
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory_descriptor,
        )
        information = os.fstat(descriptor)
        current = os.stat(
            path.name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(information.st_mode)
            or information.st_uid != os.geteuid()
            or stat.S_IMODE(information.st_mode) != 0o600
            or information.st_nlink != 1
            or information.st_size > MAX_CONTROL_BYTES
            or (information.st_dev, information.st_ino, information.st_uid)
            != (current.st_dev, current.st_ino, current.st_uid)
        ):
            raise StateError(f"{label} must be an owner-private single-link file")
        chunks: list[bytes] = []
        remaining = MAX_CONTROL_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        rebound = os.stat(
            path.name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if len(data) != information.st_size or (
            information.st_dev,
            information.st_ino,
            information.st_uid,
            information.st_mode,
            information.st_nlink,
            information.st_size,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_uid,
            after.st_mode,
            after.st_nlink,
            after.st_size,
        ) or (after.st_dev, after.st_ino, after.st_uid) != (
            rebound.st_dev,
            rebound.st_ino,
            rebound.st_uid,
        ):
            raise StateError(f"{label} changed while it was read")
        return data, information
    except StateError:
        raise
    except OSError as error:
        raise StateError(f"{label} cannot be read safely") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory_descriptor is not None:
            os.close(directory_descriptor)


def _receipt_pin(path: Path, information: os.stat_result, data: bytes) -> dict[str, Any]:
    return {
        "path": str(path),
        "device": information.st_dev,
        "inode": information.st_ino,
        "owner": information.st_uid,
        "mode": stat.S_IMODE(information.st_mode),
        "links": information.st_nlink,
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _load_receipt(
    path: Path,
    *,
    lease_id: str,
    state_path: Path,
) -> tuple[str, dict[str, Any]]:
    data, information = _read_private_file(path, "native owner receipt")
    try:
        value = json.loads(data)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise StateError("native owner receipt is malformed") from error
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "lease_id", "owner_token", "state_path"}
        or value["version"] != 1
        or value["lease_id"] != lease_id
        or value["state_path"] != str(state_path)
        or not isinstance(value["owner_token"], str)
    ):
        raise StateError("native owner receipt does not match this lease and state")
    return value["owner_token"], _receipt_pin(path, information, data)


def _create_or_load_receipt(
    path: Path,
    *,
    lease_id: str,
    state_path: Path,
) -> tuple[str, dict[str, Any], bool]:
    try:
        return (*_load_receipt(path, lease_id=lease_id, state_path=state_path), False)
    except StateError:
        if path.exists() or path.is_symlink():
            raise
    parent = path.parent.resolve(strict=True)
    if parent != path.parent:
        raise StateError("native owner receipt parent is not canonical")
    token = secrets.token_hex(32)
    encoded = (
        canonical_json(
            {
                "version": 1,
                "lease_id": lease_id,
                "owner_token": token,
                "state_path": str(state_path),
            }
        ).encode("utf-8")
        + b"\n"
    )
    directory_descriptor: int | None = None
    descriptor: int | None = None
    try:
        directory_descriptor = os.open(
            parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        directory_information = os.fstat(directory_descriptor)
        if (
            not stat.S_ISDIR(directory_information.st_mode)
            or directory_information.st_uid != os.geteuid()
        ):
            raise StateError("native owner receipt parent is not owner-controlled")
        descriptor = os.open(
            path.name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=directory_descriptor,
        )
        os.fchmod(descriptor, 0o600)
        if os.write(descriptor, encoded) != len(encoded):
            raise StateError("native owner receipt write was incomplete")
        os.fsync(descriptor)
        published = os.stat(
            path.name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        held = os.fstat(descriptor)
        if (published.st_dev, published.st_ino, published.st_uid) != (
            held.st_dev,
            held.st_ino,
            held.st_uid,
        ):
            raise StateError("native owner receipt pathname changed during creation")
    except StateError:
        raise
    except OSError as error:
        raise StateError("native owner receipt cannot be created safely") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory_descriptor is not None:
            os.close(directory_descriptor)
    owner_token, pin = _load_receipt(path, lease_id=lease_id, state_path=state_path)
    return owner_token, pin, True


def _load_guard_template(path: Path) -> tuple[list[str], str]:
    data, _information = _read_private_file(path, "native guard request")
    try:
        value = json.loads(data)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise StateError("native guard request is malformed") from error
    if (
        not isinstance(value, list)
        or not value
        or len(value) > 1024
        or len(value) % 2
        or any(
            not isinstance(item, str)
            or not item
            or "\x00" in item
            or len(item.encode("utf-8")) > 524_288
            for item in value
        )
        or any(not value[index].startswith("--") for index in range(0, len(value), 2))
    ):
        raise StateError("native guard request must be bounded flag/value pairs")
    return value, hashlib.sha256(data).hexdigest()


def _exact_guard_file(root: Path, raw: str) -> str:
    if (
        not raw
        or raw.startswith(("/", "./", "../"))
        or raw in {".", ".."}
        or "//" in raw
        or raw.endswith("/")
        or "/./" in raw
        or "/../" in raw
        or "*" in raw
        or "?" in raw
    ):
        raise StateError("guard scheduler binding file path is not exact")
    candidate = root.joinpath(*Path(raw).parts)
    cursor = root
    for part in Path(raw).parts:
        cursor = cursor / part
        try:
            information = os.lstat(cursor)
        except FileNotFoundError:
            break
        if stat.S_ISLNK(information.st_mode):
            raise StateError("guard scheduler binding file path traverses a symlink")
    try:
        if candidate.resolve(strict=False) != candidate.absolute():
            raise StateError("guard scheduler binding file path is aliased")
    except (OSError, RuntimeError, ValueError) as error:
        raise StateError("guard scheduler binding file path is invalid") from error
    return raw


def _scheduler_binding_from_guard_request(
    template: list[str],
    *,
    expected: dict[str, Any],
) -> dict[str, Any]:
    scalars: dict[str, str] = {}
    files: list[str] = []
    for index in range(0, len(template), 2):
        flag, value = template[index : index + 2]
        if flag == "--file":
            files.append(value)
        elif flag in _SCHEDULER_BINDING_FLAGS:
            if flag in scalars:
                raise StateError("guard scheduler binding field is duplicated")
            scalars[flag] = value
    if set(scalars) != _SCHEDULER_BINDING_FLAGS:
        raise StateError("guard request omits an exact scheduler binding")
    try:
        worktree = Path(scalars["--worktree"]).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise StateError("guard scheduler binding worktree is unavailable") from error
    if str(worktree) != scalars["--worktree"] or not worktree.is_dir():
        raise StateError("guard scheduler binding worktree is not canonical")
    identity = validate_boundary(worktree, worktree)
    lane = scalars["--lane-type"].lower()
    weight = scalars["--weight"].upper()
    if lane not in {"review", "feature", "integration", "recovery"}:
        raise StateError("guard scheduler binding lane is invalid")
    if weight not in {"LIGHT", "HEAVY"}:
        raise StateError("guard scheduler binding weight is invalid")
    base_sha = scalars["--base-sha"].lower()
    if base_sha != identity.head.lower():
        raise StateError("guard scheduler binding baseline differs from repository HEAD")
    exact_files = [_exact_guard_file(worktree, path) for path in files]
    if len(exact_files) != len(set(exact_files)):
        raise StateError("guard scheduler binding file set contains duplicates")
    if lane in {"feature", "integration", "recovery"} and not exact_files:
        raise StateError("guard scheduler binding product lane has no files")
    if lane == "review" and exact_files:
        raise StateError("guard scheduler binding review lane cannot reserve product files")
    binding = {
        "repo_id": _repo_id(identity),
        "base_sha": identity.head,
        "lane": lane,
        "weight": weight,
        "writer": str(worktree) if lane in {"feature", "integration", "recovery"} else None,
        "files": exact_files,
    }
    if not isinstance(expected, dict) or set(expected) != set(binding):
        raise StateError("expected guard scheduler binding assertions are malformed")
    for name, value in binding.items():
        if expected[name] != value:
            raise StateError(
                f"guard scheduler binding {name} does not match expected assertion"
            )
    return binding


def _validated_machine_snapshot() -> dict[str, float | int]:
    machine = _machine_snapshot()
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
        raise StateError("native machine admission snapshot is malformed")
    return dict(machine)


def _guard_admission_builder(
    template: list[str],
    *,
    guard_request_sha256: str,
    reservation: dict[str, Any],
    action: str,
    prior_files: list[str],
):
    def build(active: list[dict[str, Any]]) -> dict[str, Any]:
        worktree = Path(reservation["writer"] or template[template.index("--worktree") + 1])
        machine = _validated_machine_snapshot()
        measured_wip = _measured_wip(worktree)
        dirty_paths = _git_dirty_paths(worktree)
        argv: list[str] = []
        for index in range(0, len(template), 2):
            flag, value = template[index : index + 2]
            if flag in _DYNAMIC_GUARD_FLAGS | _REPEATED_DYNAMIC_GUARD_FLAGS:
                continue
            argv.extend((flag, value))
        existing_heavy = sum(item["weight"] == "HEAVY" for item in active)
        active_count = len(active)
        if action == "expand":
            active_count += 1
            existing_heavy += reservation["weight"] == "HEAVY"
        argv.extend(
            (
                "--active-lanes",
                str(active_count),
                "--heavy-leases",
                str(existing_heavy),
                "--admission-action",
                action,
                "--owns-heavy-lease",
                "true" if action == "expand" and reservation["weight"] == "HEAVY" else "false",
                "--clean-tree",
                "true" if not dirty_paths else "false",
                "--unmerged-branches",
                str(measured_wip),
            )
        )
        for item in active:
            if item["writer"] is not None:
                argv.extend(("--active-writer-worktree", item["writer"]))
            if item["repo_id"] == reservation["repo_id"]:
                for file_path in item["files"]:
                    argv.extend(("--parallel-file", file_path))
        if action == "expand":
            if reservation["writer"] is not None:
                argv.extend(("--active-writer-worktree", reservation["writer"]))
            for file_path in prior_files:
                argv.extend(("--current-file", file_path))
        for file_path in reservation["files"]:
            argv.extend(("--file", file_path))
        completed = subprocess.run(
            [str(GUARD), *argv],
            cwd=reservation["writer"] or None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
            shell=False,
            env={
                "PATH": "/usr/bin:/bin",
                "LC_ALL": "C",
                "FORGE_GUARD_PYTHON": "/usr/bin/python3",
                "FORGE_GUARD_GIT": "/usr/bin/git",
                "FORGE_GUARD_PINNED_CANONICAL_PATH": str(GUARD),
                "FORGE_GUARD_WIP_MEASURED": str(measured_wip),
                "FORGE_GUARD_CORE_COUNT": str(machine["core_count"]),
                "FORGE_GUARD_LOAD_1": str(machine["load_1"]),
                "FORGE_GUARD_FREE_SWAP_GB": str(machine["free_swap_gb"]),
                "FORGE_GUARD_CPU_IDLE_PCT": str(machine["cpu_idle_pct"]),
                "FORGE_GUARD_MEMORY_FREE_PCT": str(machine["memory_free_pct"]),
            },
        )
        if completed.returncode != 0 or completed.stderr or not completed.stdout.startswith("ADMIT:"):
            detail = (completed.stdout + completed.stderr).strip()
            raise StateError(detail or "native dispatch guard rejected admission")
        try:
            result = json.loads(completed.stdout.removeprefix("ADMIT:"))
        except json.JSONDecodeError as error:
            raise StateError("native dispatch guard returned a malformed receipt") from error
        if result.get("scheduler_binding") != reservation:
            raise StateError("native dispatch guard scheduler binding differs")
        return {
            "version": 1,
            "kind": "native",
            "action": action,
            "argv": argv,
            "argv_sha256": hashlib.sha256(canonical_json(argv).encode("utf-8")).hexdigest(),
            "result": result,
            "result_sha256": hashlib.sha256(canonical_json(result).encode("utf-8")).hexdigest(),
            "guard_sha256": hashlib.sha256(GUARD.read_bytes()).hexdigest(),
            "guard_request_sha256": guard_request_sha256,
            "scheduler_binding": dict(reservation),
            "machine": machine,
            "measured_wip": measured_wip,
        }

    return build


def _print(value: Any) -> None:
    sys.stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reserve and manage Codex-native lanes in Forge's shared scheduler"
    )
    parser.add_argument(
        "--state",
        default=os.environ.get(
            "FORGE_STATE_PATH",
            str(Path.home() / ".codex" / "forge" / "state.sqlite3"),
        ),
        help="shared Forge SQLite state path",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    reserve = subparsers.add_parser("reserve", help="atomically admit a native lane")
    reserve.add_argument("--lease-id", required=True)
    reserve.add_argument("--receipt", required=True)
    reserve.add_argument("--guard-request", required=True)
    reserve.add_argument("--repo-id", required=True)
    reserve.add_argument("--base-sha", required=True)
    reserve.add_argument("--lane", choices=("review", "feature", "integration", "recovery"), required=True)
    reserve.add_argument("--weight", choices=("LIGHT", "HEAVY"), required=True)
    reserve.add_argument("--writer")
    reserve.add_argument("--file", action="append", default=[])
    reserve.add_argument("--ttl-seconds", type=int, default=300)

    for name in ("heartbeat", "release"):
        control = subparsers.add_parser(name, help=f"{name} an owned native lane")
        control.add_argument("--lease-id", required=True)
        control.add_argument("--receipt", required=True)
        control.add_argument("--revision", type=int, required=True)

    expand = subparsers.add_parser("expand", help="re-admit an exact same-lane file-set expansion")
    expand.add_argument("--lease-id", required=True)
    expand.add_argument("--receipt", required=True)
    expand.add_argument("--guard-request", required=True)
    expand.add_argument("--revision", type=int, required=True)
    expand.add_argument("--prior-file", action="append", default=[])
    expand.add_argument("--file", action="append", default=[])

    listing = subparsers.add_parser("list", help="list shared lane reservations without capabilities")
    listing.add_argument("--include-released", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        state_path = _absolute_path(args.state, "state")
        store = Store(state_path)
        if args.command == "list":
            _print(store.list_lane_reservations(include_released=args.include_released))
            return 0
        receipt_path = _absolute_path(args.receipt, "receipt")
        if args.command == "reserve":
            guard, guard_request_sha256 = _load_guard_template(
                _absolute_path(args.guard_request, "guard request")
            )
            reservation = _scheduler_binding_from_guard_request(
                guard,
                expected={
                    "repo_id": args.repo_id,
                    "base_sha": args.base_sha,
                    "lane": args.lane,
                    "weight": args.weight,
                    "writer": args.writer,
                    "files": args.file,
                },
            )
            if reservation["lane"] == "recovery":
                raise StateError(NATIVE_RECOVERY_UNSUPPORTED)
            owner_token, receipt_pin, _created = _create_or_load_receipt(
                receipt_path,
                lease_id=args.lease_id,
                state_path=state_path,
            )
            value, replay = store.reserve_native_lane(
                lease_id=args.lease_id,
                owner_token=owner_token,
                receipt_pin=receipt_pin,
                reservation=reservation,
                ttl_seconds=args.ttl_seconds,
                operation_context_sha256=guard_request_sha256,
                build_admission=_guard_admission_builder(
                    guard,
                    guard_request_sha256=guard_request_sha256,
                    reservation=reservation,
                    action="launch",
                    prior_files=[],
                ),
            )
            _print({"reservation": value, "idempotent": replay})
            return 0
        owner_token, _receipt_pin_value = _load_receipt(
            receipt_path,
            lease_id=args.lease_id,
            state_path=state_path,
        )
        if args.command == "heartbeat":
            _print(
                store.heartbeat_native_lane(
                    args.lease_id,
                    owner_token=owner_token,
                    expected_revision=args.revision,
                )
            )
            return 0
        if args.command == "release":
            _print(
                store.release_native_lane(
                    args.lease_id,
                    owner_token=owner_token,
                    expected_revision=args.revision,
                )
            )
            return 0
        current = next(
            (
                item
                for item in store.list_lane_reservations(include_released=True)
                if item["lease_id"] == args.lease_id and item["kind"] == "native"
            ),
            None,
        )
        if current is None:
            raise StateError("native lane does not exist")
        if current["lane"] == "recovery":
            raise StateError(NATIVE_RECOVERY_UNSUPPORTED)
        reservation = {
            name: current[name]
            for name in ("repo_id", "base_sha", "lane", "weight", "writer")
        }
        guard, guard_request_sha256 = _load_guard_template(
            _absolute_path(args.guard_request, "guard request")
        )
        reservation["files"] = args.file
        reservation = _scheduler_binding_from_guard_request(
            guard,
            expected=reservation,
        )
        value, replay = store.expand_native_lane(
            args.lease_id,
            owner_token=owner_token,
            expected_revision=args.revision,
            prior_files=args.prior_file,
            files=args.file,
            operation_context_sha256=guard_request_sha256,
            build_admission=_guard_admission_builder(
                guard,
                guard_request_sha256=guard_request_sha256,
                reservation=reservation,
                action="expand",
                prior_files=args.prior_file,
            ),
        )
        _print({"reservation": value, "idempotent": replay})
        return 0
    except (BoundaryError, OSError, StateError, subprocess.SubprocessError) as error:
        print(f"forge-native-lane: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
