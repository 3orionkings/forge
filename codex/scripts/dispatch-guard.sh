#!/bin/bash
#
# Forge dispatch admission guard.
#
# This is a role-neutral Python admission engine in a Bash 3-compatible launcher,
# adapted from the /forge dispatch guard by Emmanuel, itself derived from:
# - grill-me-codex/codex-review/codex-build, Copyright (c) 2026 Chase AI
# - grill-me/grill-with-docs, Copyright (c) 2026 Matt Pocock
#
# MIT License
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE.

set -u
set -o pipefail

resolve_guard_path() {
  local source="${BASH_SOURCE[0]}"
  local directory
  while [[ -h "${source}" ]]; do
    directory="$(cd -P "$(dirname "${source}")" >/dev/null 2>&1 && pwd)" || return 1
    source="$(readlink "${source}")" || return 1
    [[ "${source}" = /* ]] || source="${directory}/${source}"
  done
  directory="$(cd -P "$(dirname "${source}")" >/dev/null 2>&1 && pwd)" || return 1
  printf '%s/%s\n' "${directory}" "$(basename "${source}")"
}

if [[ -n "${FORGE_GUARD_PINNED_CANONICAL_PATH:-}" ]]; then
  FORGE_GUARD_CANONICAL_PATH="${FORGE_GUARD_PINNED_CANONICAL_PATH}"
else
  FORGE_GUARD_CANONICAL_PATH="$(resolve_guard_path)" || exit 70
fi
export FORGE_GUARD_CANONICAL_PATH
FORGE_GUARD_PYTHON="${FORGE_GUARD_PYTHON:-/usr/bin/python3}"
FORGE_GUARD_GIT="${FORGE_GUARD_GIT:-/usr/bin/git}"
export FORGE_GUARD_GIT
exec "${FORGE_GUARD_PYTHON}" - "$@" <<'PY'
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import stat
import subprocess
import sys
import unicodedata
from pathlib import Path

argv = sys.argv[1:]
SCALAR = {
    "--verdict": "verdict",
    "--active-lanes": "active_lanes",
    "--heavy-leases": "heavy_leases",
    "--weight": "weight",
    "--lane-type": "lane_type",
    "--mode": "mode",
    "--worktree": "worktree",
    "--clean-tree": "clean_tree",
    "--builder": "builder",
    "--executor-policy": "executor_policy",
    "--base-sha": "base_sha",
    "--plan-file": "plan_file",
    "--act2-review-log": "review_log",
    "--execution-contract": "execution_contract",
    "--project-overlay": "project_overlay",
    "--engine-contract": "engine_contract",
    "--routing-policy": "routing_policy",
    "--liveness-fixture": "liveness_fixture",
    "--watchdog-fixture": "watchdog_fixture",
    "--watchdog-tests-fixture": "watchdog_tests_fixture",
    "--freeze-manifest": "freeze_manifest",
    "--freeze-manifest-sha256": "freeze_manifest_hash",
    "--oracle-exact": "oracle_exact",
    "--capability-receipt": "capability_receipt",
    "--capability-receipt-sha256": "capability_receipt_hash",
    "--proof-command": "proof_command",
    "--work-log": "work_log",
    "--report": "report",
    "--predecessor": "predecessor",
    "--predecessor-builder": "predecessor_builder",
    "--proof-scope": "proof_scope",
    "--execution-profile": "execution_profile",
    "--admission-action": "admission_action",
    "--owns-heavy-lease": "owns_heavy_lease",
    "--report-receipt": "report_receipt",
    "--review-receipt": "review_receipt",
    "--instruction": "instruction",
    "--unmerged-branches": "unmerged_branches",
}
REPEAT = {
    "--backlog-id": "backlog_ids",
    "--file": "files",
    "--active-writer-worktree": "active_writers",
    "--parallel-file": "parallel_files",
    "--current-file": "current_files",
    "--review-domain-doc": "review_domain_docs",
}


def reject(code: str, reason: str) -> "NoReturn":
    print(f"REJECT: {code}: {reason}")
    raise SystemExit(1)


values = {
    "executor_policy": "any",
    "proof_scope": "broad",
    "execution_profile": "assured",
    "admission_action": "launch",
    "owns_heavy_lease": "false",
}
for name in REPEAT.values():
    values[name] = []
index = 0
while index < len(argv):
    flag = argv[index]
    if flag in {"-h", "--help"}:
        print("Usage: dispatch-guard.sh [declaration flags]")
        raise SystemExit(0)
    if flag not in SCALAR and flag not in REPEAT:
        reject("ARGUMENT_UNKNOWN", f"unknown flag: {flag}")
    if index + 1 >= len(argv):
        reject("ARGUMENT_VALUE", f"{flag} requires a value")
    value = argv[index + 1]
    if flag in REPEAT:
        values[REPEAT[flag]].append(value)
    else:
        values[SCALAR[flag]] = value
    index += 2

for name in ("weight",):
    values[name] = values.get(name, "").upper()
for name in (
    "lane_type", "mode", "clean_tree", "executor_policy", "oracle_exact",
    "proof_scope", "execution_profile", "admission_action", "owns_heavy_lease",
):
    values[name] = values.get(name, "").lower()


def nonnegative_int(name: str, code: str) -> int:
    raw = values.get(name, "")
    if not isinstance(raw, str) or re.fullmatch(r"[0-9]+", raw) is None:
        reject(code, f"{name} must be a non-negative integer")
    return int(raw)


def load_pinned_inputs() -> dict[str, bytes]:
    raw = os.environ.get("FORGE_GUARD_PINNED_INPUTS")
    if raw is None:
        return {}
    try:
        mapping = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        reject("PINNED_INPUTS", "pinned input descriptor map is malformed")
    if (
        not isinstance(mapping, dict)
        or len(mapping) > 128
        or any(
            not isinstance(path, str)
            or not path
            or "\0" in path
            or not Path(path).is_absolute()
            or ".." in Path(path).parts
            or isinstance(descriptor, bool)
            or not isinstance(descriptor, int)
            or descriptor < 3
            for path, descriptor in mapping.items()
        )
        or len(set(mapping.values())) != len(mapping)
    ):
        reject("PINNED_INPUTS", "pinned input descriptor map is malformed")
    result: dict[str, bytes] = {}
    for path, descriptor in mapping.items():
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o400
                or before.st_size > 1024 * 1024
            ):
                raise OSError
            os.lseek(descriptor, 0, os.SEEK_SET)
            data = bytearray()
            while True:
                chunk = os.read(descriptor, min(65_536, 1024 * 1024 + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > 1024 * 1024:
                    raise OSError
            after = os.fstat(descriptor)
            if (
                len(data) != before.st_size
                or (before.st_dev, before.st_ino, before.st_mode, before.st_size)
                != (after.st_dev, after.st_ino, after.st_mode, after.st_size)
            ):
                raise OSError
        except OSError:
            reject("PINNED_INPUTS", f"pinned input snapshot is invalid: {path}")
        result[path] = bytes(data)
    return result


PINNED_INPUTS = load_pinned_inputs()


def file_bytes(path: Path, maximum: int = 1024 * 1024) -> bytes:
    pinned = PINNED_INPUTS.get(str(path))
    if pinned is not None:
        if len(pinned) > maximum:
            reject("FREEZE_CONTENT_MISMATCH", f"frozen file exceeds its bound: {path}")
        return pinned
    try:
        info = os.stat(path, follow_symlinks=False)
        if not path.is_file() or path.is_symlink() or info.st_size > maximum:
            raise OSError
        return path.read_bytes()
    except OSError:
        reject("FREEZE_CONTENT_MISMATCH", f"frozen file cannot be read: {path}")


def file_text(path: Path, *, errors: str = "strict") -> str:
    try:
        return file_bytes(path).decode("utf-8", errors=errors)
    except UnicodeError:
        reject("FREEZE_CONTENT_MISMATCH", f"frozen file is not UTF-8: {path}")


def file_is_regular(path: Path) -> bool:
    if str(path) in PINNED_INPUTS:
        return True
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


DIRECTORY_ENTRY_CACHE: dict[tuple[int, int, int, int], frozenset[str]] = {}


def exact_directory_entries(directory_fd: int) -> frozenset[str]:
    """Cache one held directory listing only while its identity facts match."""

    information = os.fstat(directory_fd)
    key = (
        information.st_dev,
        information.st_ino,
        information.st_mtime_ns,
        information.st_ctime_ns,
    )
    cached = DIRECTORY_ENTRY_CACHE.get(key)
    if cached is not None:
        return cached
    entries = frozenset(os.listdir(directory_fd))
    if len(DIRECTORY_ENTRY_CACHE) >= 4096:
        DIRECTORY_ENTRY_CACHE.clear()
    DIRECTORY_ENTRY_CACHE[key] = entries
    return entries


def exact_review_control(
    raw: str,
    label: str,
) -> tuple[Path, tuple[int, int]]:
    """Resolve no aliases and return one single-link regular-file identity."""

    path = Path(raw)
    if (
        not raw
        or "\0" in raw
        or not path.is_absolute()
        or ".." in path.parts
        or str(path) != raw
    ):
        reject("REVIEW_CONTROL_ALIAS", f"{label} path must be canonical and absolute")
    try:
        canonical = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        reject("REVIEW_CONTROL_MISSING", f"{label} must be an exact worktree file")
    if str(canonical) != raw:
        reject("REVIEW_CONTROL_ALIAS", f"{label} path traverses an alias")

    directory_fd: int | None = None
    descriptor: int | None = None
    try:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        directory_fd = os.open(os.path.sep, directory_flags)
        components = path.parts[1:]
        if not components:
            raise OSError
        for component in components[:-1]:
            try:
                directory_entries = exact_directory_entries(directory_fd)
            except OSError:
                reject(
                    "REVIEW_CONTROL_ALIAS",
                    f"{label} path spelling cannot be inspected",
                )
            if component not in directory_entries:
                aliases = [
                    entry
                    for entry in directory_entries
                    if unicodedata.normalize("NFC", entry).casefold()
                    == unicodedata.normalize("NFC", component).casefold()
                ]
                reject(
                    "REVIEW_CONTROL_ALIAS" if aliases else "REVIEW_CONTROL_MISSING",
                    f"{label} path component spelling is not exact",
                )
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        try:
            directory_entries = exact_directory_entries(directory_fd)
        except OSError:
            reject(
                "REVIEW_CONTROL_ALIAS",
                f"{label} parent spelling cannot be inspected",
            )
        if components[-1] not in directory_entries:
            aliases = [
                entry
                for entry in directory_entries
                if unicodedata.normalize("NFC", entry).casefold()
                == unicodedata.normalize("NFC", components[-1]).casefold()
            ]
            reject(
                "REVIEW_CONTROL_ALIAS" if aliases else "REVIEW_CONTROL_MISSING",
                f"{label} file spelling is not exact",
            )
        file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        file_flags |= getattr(os, "O_NONBLOCK", 0)
        file_flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(components[-1], file_flags, dir_fd=directory_fd)
        information = os.fstat(descriptor)
        if not stat.S_ISREG(information.st_mode) or information.st_nlink != 1:
            reject(
                "REVIEW_CONTROL_ALIAS",
                f"{label} must be a single-link regular file",
            )
        return path, (information.st_dev, information.st_ino)
    except OSError:
        reject(
            "REVIEW_CONTROL_ALIAS",
            f"{label} path cannot be traversed without following links",
        )
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory_fd is not None:
            os.close(directory_fd)


def file_size(path: Path) -> int:
    pinned = PINNED_INPUTS.get(str(path))
    if pinned is not None:
        return len(pinned)
    try:
        return os.stat(path, follow_symlinks=False).st_size
    except OSError:
        return -1


def sha256_file(path: Path, maximum: int = 1024 * 1024) -> str:
    return hashlib.sha256(file_bytes(path, maximum)).hexdigest()


def git(args: list[str], cwd: str) -> str:
    completed = subprocess.run(
        [os.environ["FORGE_GUARD_GIT"], "-C", cwd, *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        shell=False,
        timeout=5,
    )
    if completed.returncode != 0:
        raise ValueError
    # Preserve leading bytes: porcelain status uses its first two columns as
    # data, and `.strip()` would erase the leading space from an unstaged
    # first entry.  Git's textual commands only need their record terminator
    # removed here.
    return completed.stdout.rstrip("\r\n")


def git_bytes(args: list[str], cwd: str) -> bytes:
    completed = subprocess.run(
        [os.environ["FORGE_GUARD_GIT"], "-C", cwd, *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=False,
        shell=False,
        timeout=5,
    )
    if completed.returncode != 0 or len(completed.stdout) > 1024 * 1024:
        raise ValueError
    return completed.stdout


def porcelain_v1_z_paths(payload: bytes) -> list[str]:
    """Decode exact dirty names, including both sides of rename/copy records."""

    if not payload:
        return []
    if not payload.endswith(b"\0"):
        raise ValueError
    records = payload.split(b"\0")
    if records.pop() != b"":
        raise ValueError
    paths: list[str] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4 or record[2:3] != b" " or not record[3:]:
            raise ValueError
        status_code = record[:2]
        paths.append(os.fsdecode(record[3:]))
        if b"R" in status_code or b"C" in status_code:
            if index >= len(records) or not records[index]:
                raise ValueError
            paths.append(os.fsdecode(records[index]))
            index += 1
    return sorted(set(paths))


active_lanes = nonnegative_int("active_lanes", "FIELD_ACTIVE_LANES")
profile = values["execution_profile"]
if profile not in {"sprint", "assured"}:
    reject("FIELD_EXECUTION_PROFILE", "execution profile must be sprint or assured")
action = values["admission_action"]
if action not in {"launch", "expand", "resume"}:
    reject("FIELD_ADMISSION_ACTION", "admission action must be launch, resume, or expand")
owns_heavy = values["owns_heavy_lease"]
if owns_heavy not in {"true", "false"}:
    reject("FIELD_HEAVY_LEASE_OWNERSHIP", "owns-heavy-lease must be true or false")
if action == "launch" and owns_heavy == "true":
    reject("HEAVY_LEASE_OWNERSHIP", "a launch cannot own an existing lease")
if action in {"launch", "resume"} and active_lanes >= 3:
    reject("LANE_CAP", "maximum three active lanes")

heavy_leases = nonnegative_int("heavy_leases", "FIELD_HEAVY_LEASES")
if heavy_leases > 1:
    reject("HEAVY_LEASE_STATE", "at most one HEAVY lease may exist")
weight = values["weight"]
if weight not in {"LIGHT", "HEAVY"}:
    reject("FIELD_WEIGHT", "weight must be LIGHT or HEAVY")
if owns_heavy == "true" and not (
    action == "expand" and weight == "HEAVY" and heavy_leases == 1
):
    reject("HEAVY_LEASE_OWNERSHIP", "invalid HEAVY lease ownership")
if weight == "HEAVY" and heavy_leases >= 1 and owns_heavy != "true":
    reject("HEAVY_LEASE", "a HEAVY lease is already held")

lane = values["lane_type"]
if lane not in {"feature", "integration", "recovery", "control-plane", "review"}:
    reject("FIELD_LANE_TYPE", "invalid lane type")
domain_docs = values["review_domain_docs"]
if len(domain_docs) > 64 or len(domain_docs) != len(set(domain_docs)):
    reject("REVIEW_CONTROL_ALIAS", "review domain docs must be bounded and unique")
if domain_docs and lane != "review":
    reject("REVIEW_CONTROL_ROLE", "review domain docs are valid only on a review lane")
product = lane in {"feature", "integration", "recovery"}
if product:
    if not values.get("verdict"):
        reject("VERDICT_MISSING", "product lanes require a verdict")
    if values["verdict"] != "APPROVED":
        reject("VERDICT_NOT_APPROVED", "product lanes require APPROVED")
mode = values["mode"]
if mode not in {"normal", "recovery"}:
    reject("FIELD_MODE", "mode must be normal or recovery")
if (mode == "recovery") != (lane == "recovery"):
    reject("RECOVERY_MODE", "recovery mode and lane must match")

worktree_raw = values.get("worktree", "")
if not worktree_raw.startswith("/"):
    reject("FIELD_WORKTREE", "worktree must be absolute")
worktree = str(Path(worktree_raw).resolve())
active_writers = [str(Path(item).resolve()) for item in values["active_writers"]]
writer_active = worktree in active_writers
if action in {"launch", "resume"} and writer_active:
    reject("DUPLICATE_WRITER", "worktree already has a writer")
if action == "expand" and not writer_active:
    reject("EXPAND_NOT_ACTIVE", "expansion requires an active writer")

clean_declared = values["clean_tree"]
if clean_declared not in {"true", "false"}:
    reject("FIELD_CLEAN_TREE", "clean-tree must be true or false")
if (
    clean_declared == "false"
    and mode != "recovery"
    and action not in {"expand", "resume"}
    and lane != "review"
):
    reject("DIRTY_TREE", "dirty tree requires recovery or expansion")
if not values.get("builder"):
    reject("FIELD_BUILDER", "builder is required")

policy = values["executor_policy"]
if policy not in {"any", "codex-only", "claude-only"}:
    reject("FIELD_EXECUTOR_POLICY", "invalid executor policy")


def family(value: str) -> str:
    lowered = value.casefold()
    if any(item in lowered for item in ("claude", "opus", "fable")):
        return "claude"
    if any(item in lowered for item in ("codex", "sol", "terra", "gpt-5.")):
        return "codex"
    return "unknown"


if product:
    overlay_raw = values.get("project_overlay", "")
    if not overlay_raw:
        reject("PROJECT_OVERLAY_MISSING", "product policy requires a project overlay")
    overlay = Path(overlay_raw)
    try:
        declarations = [
            line.split("=", 1)[1].strip().casefold()
            for line in file_text(overlay).splitlines()
            if line.startswith("executor_policy=")
        ]
    except (OSError, UnicodeError):
        reject("PROJECT_OVERLAY_MISSING", "project overlay cannot be read")
    if declarations != [policy]:
        reject("OVERLAY_POLICY_MISMATCH", "overlay policy differs from declaration")
    if policy == "codex-only" and family(values["builder"]) != "codex":
        reject("BUILDER_POLICY", "builder violates codex-only policy")
    if policy == "claude-only" and family(values["builder"]) != "claude":
        reject("BUILDER_POLICY", "builder violates claude-only policy")

files = values["files"]
current_files = values["current_files"]
parallel_files = set(values["parallel_files"])


def exact_relative(path: str) -> bool:
    if not path or path.startswith(("/", "./", "../")) or path in {".", ".."}:
        return False
    if "//" in path or path.endswith("/") or "/./" in path or "/../" in path:
        return False
    return "*" not in path and "?" not in path


for declared in files + current_files + list(parallel_files):
    if not exact_relative(declared):
        reject("FIELD_FILE", f"file path is not exact: {declared}")
    cursor = Path(worktree)
    for part in Path(declared).parts:
        cursor = cursor / part
        try:
            if stat.S_ISLNK(os.lstat(cursor).st_mode):
                reject("FIELD_FILE", f"file path traverses a symlink: {declared}")
        except FileNotFoundError:
            break
        except OSError:
            reject("FIELD_FILE", f"file path cannot be inspected: {declared}")
    candidate = Path(worktree) / declared
    if candidate.resolve(strict=False) != candidate.absolute():
        reject("FIELD_FILE", f"file path is aliased: {declared}")
if any(path == ".ai/HANDOFF.md" or path.endswith("/.ai/HANDOFF.md") for path in files):
    reject("HANDOFF_FORBIDDEN", "HANDOFF is driver-owned")
if len(files) != len(set(files)):
    reject("FILE_DUPLICATE", "declared files must be unique")
if action in {"launch", "resume"} and current_files:
    reject("CURRENT_SET_ON_LAUNCH", "launch/resume cannot claim a current set")
if action == "expand":
    if not current_files:
        reject("EXPAND_CURRENT_SET_MISSING", "expansion requires current set")
    if not set(current_files).issubset(files):
        reject("EXPAND_NOT_SUPERSET", "expanded set must contain current set")
if set(files) & parallel_files:
    reject("FILE_OVERLAP", "declared files overlap another lane")

# Repository and frozen-product checks.
actual_clean = True
dirty_paths: list[str] = []
repository_head = ""
repository_common_dir = ""
try:
    top = git(["rev-parse", "--show-toplevel"], worktree)
    if str(Path(top).resolve()) != worktree:
        reject("WORKTREE_NOT_ROOT", "worktree is not the repository root")
    repository_head = git(["rev-parse", "HEAD"], worktree)
    common_raw = git(["rev-parse", "--git-common-dir"], worktree)
    common_path = Path(common_raw)
    if not common_path.is_absolute():
        common_path = Path(worktree) / common_path
    repository_common_dir = str(common_path.resolve(strict=True))
    status_bytes = git_bytes(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        worktree,
    )
    dirty_paths = porcelain_v1_z_paths(status_bytes)
    actual_clean = not dirty_paths
except (OSError, RuntimeError, ValueError):
    reject("WORKTREE_NOT_GIT", "worktree is not a Git repository")

if actual_clean and clean_declared == "false":
    reject("CLEAN_TREE_MISMATCH", "declared cleanliness differs from Git")
if not actual_clean and clean_declared == "true":
    reject("WORKTREE_DIRTY", "worktree has observed changes")

review_paths: set[str] = set()
if lane == "review" and (
    domain_docs
    or not actual_clean
    or values.get("plan_file")
    or values.get("review_log")
):
    review_identities: set[tuple[int, int]] = set()
    for label in ("plan_file", "review_log"):
        raw = values.get(label, "")
        try:
            control, identity = exact_review_control(raw, label)
            relative = str(control.relative_to(Path(worktree)))
        except ValueError:
            reject("REVIEW_CONTROL_MISSING", "review controls must be exact worktree files")
        if identity in review_identities:
            reject("REVIEW_CONTROL_ALIAS", "review control roles must be distinct")
        review_identities.add(identity)
        review_paths.add(relative)
    for index, raw in enumerate(domain_docs):
        try:
            control, identity = exact_review_control(
                raw,
                f"review domain doc {index}",
            )
            relative = str(control.relative_to(Path(worktree)))
        except ValueError:
            reject("REVIEW_CONTROL_MISSING", "review domain docs must be exact worktree files")
        if identity in review_identities:
            reject("REVIEW_CONTROL_ALIAS", "review controls must use distinct inodes")
        review_identities.add(identity)
        review_paths.add(relative)
    if len(review_paths) != 2 + len(domain_docs):
        reject("REVIEW_CONTROL_ALIAS", "review control roles must be distinct")

if not actual_clean:
    if mode == "recovery":
        pass
    elif action == "expand" or (action == "resume" and lane != "review"):
        if not set(dirty_paths).issubset(files):
            reject("DIRTY_PATH_UNDECLARED", "dirty expansion path is undeclared")
    elif lane == "review":
        if not set(dirty_paths).issubset(review_paths):
            reject(
                "DIRTY_PATH_UNDECLARED",
                "dirty review path is not a pinned control: "
                f"dirty={dirty_paths!r} controls={sorted(review_paths)!r}",
            )
    else:
        reject("WORKTREE_DIRTY", "worktree has observed changes")

if product:
    base = values.get("base_sha", "")
    if re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", base) is None:
        reject("FIELD_BASE_SHA", "base SHA is invalid")
    try:
        resolved = git(["rev-parse", "--verify", f"{base}^{{commit}}"], worktree)
    except ValueError:
        reject("BASE_SHA_UNRESOLVED", "base SHA cannot be resolved")
    try:
        head = repository_head
    except ValueError:
        reject("WORKTREE_NOT_GIT", "HEAD cannot be read")
    if resolved != head:
        reject("BASE_HEAD_MISMATCH", "base SHA does not equal HEAD")
    plan = values.get("plan_file", "")
    if not plan or not file_is_regular(Path(plan)):
        reject("PLAN_FILE_MISSING", "governing plan is missing")
    review_log = values.get("review_log", "")
    if not review_log or not file_is_regular(Path(review_log)):
        reject("ACT2_REVIEW_LOG_MISSING", "Act-2 review log is missing")
    contract_raw = values.get("execution_contract", "")
    if not contract_raw or not file_is_regular(Path(contract_raw)):
        reject("EXECUTION_CONTRACT_MISSING", "execution contract is missing")
    if not values.get("project_overlay") or not file_is_regular(Path(values["project_overlay"])):
        reject("PROJECT_OVERLAY_MISSING", "project overlay is missing")
    if not values["backlog_ids"]:
        reject("BACKLOG_ID_MISSING", "a backlog id is required")
    if not files:
        reject("FILE_MISSING", "a product lane requires files")
    if not values.get("work_log"):
        reject("WORK_LOG_MISSING", "work log is required")
    if not values.get("report"):
        reject("REPORT_MISSING", "report path is required")

oracle_exact = values["oracle_exact"]
if (product or oracle_exact) and oracle_exact not in {"true", "false"}:
    reject("FIELD_ORACLE_EXACT", "oracle-exact must be true or false")
capability = values.get("capability_receipt", "")
capability_hash = values.get("capability_receipt_hash", "")
if oracle_exact == "true":
    if not capability or not capability_hash:
        reject("CAPABILITY_RECEIPT_MISSING", "exact oracle requires capability receipt")
    capability_path = Path(capability)
    if sha256_file(capability_path) != capability_hash:
        reject("CAPABILITY_RECEIPT_HASH_MISMATCH", "capability receipt hash differs")
    try:
        receipt_pairs = {}
        for line in file_text(capability_path).splitlines():
            key, value = line.split("=", 1)
            if key in receipt_pairs:
                raise ValueError
            receipt_pairs[key] = value
    except (OSError, UnicodeError, ValueError):
        reject("CAPABILITY_RECEIPT_FORMAT", "capability receipt is malformed")
    required = {
        "version", "receipt_id", "contract_sha256", "mechanism",
        "disposable_target", "exit", "oracle_sha256", "observed_sha256",
    }
    if set(receipt_pairs) != required:
        reject("CAPABILITY_RECEIPT_FORMAT", "capability receipt fields differ")
    if (
        receipt_pairs["version"] != "1"
        or receipt_pairs["exit"] != "0"
        or not receipt_pairs["receipt_id"]
        or not receipt_pairs["mechanism"]
        or receipt_pairs["receipt_id"] != receipt_pairs["receipt_id"].strip()
        or receipt_pairs["mechanism"] != receipt_pairs["mechanism"].strip()
        or len(receipt_pairs["receipt_id"].encode("utf-8")) > 256
        or len(receipt_pairs["mechanism"].encode("utf-8")) > 512
        or not receipt_pairs["receipt_id"].isascii()
        or not receipt_pairs["mechanism"].isascii()
        or any(
            ord(character) < 0x20 or ord(character) == 0x7F
            for character in receipt_pairs["receipt_id"] + receipt_pairs["mechanism"]
        )
    ):
        reject("CAPABILITY_RECEIPT_FORMAT", "capability receipt identity differs")
    for digest_name in ("contract_sha256", "oracle_sha256", "observed_sha256"):
        if re.fullmatch(r"[0-9a-f]{64}", receipt_pairs[digest_name]) is None:
            reject("CAPABILITY_RECEIPT_FORMAT", "capability receipt hash is malformed")
    contract_hash = sha256_file(Path(values.get("execution_contract", "")))
    if receipt_pairs["contract_sha256"] != contract_hash:
        reject("CAPABILITY_CONTRACT_MISMATCH", "capability receipt contract differs")
    if receipt_pairs["oracle_sha256"] != receipt_pairs["observed_sha256"]:
        reject("CAPABILITY_MISMATCH", "oracle bytes were not reproduced")
    target_raw = receipt_pairs["disposable_target"]
    if (
        not target_raw
        or len(target_raw.encode("utf-8")) > 4096
        or "\0" in target_raw
        or not Path(target_raw).is_absolute()
        or ".." in Path(target_raw).parts
    ):
        reject("CAPABILITY_TARGET_MISSING", "capability target path is malformed")
    target_path = Path(target_raw)
    try:
        target_canonical = target_path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        reject("CAPABILITY_TARGET_MISSING", "capability target does not exist")
    if not file_is_regular(target_path):
        reject("CAPABILITY_TARGET_MISSING", "capability target is not regular")
    worktree_path = Path(worktree)
    if target_canonical == worktree_path or target_canonical.is_relative_to(worktree_path):
        reject("CAPABILITY_TARGET_NOT_DISPOSABLE", "capability target is inside the worktree")
    if sha256_file(target_path) != receipt_pairs["observed_sha256"]:
        reject("CAPABILITY_TARGET_MISMATCH", "capability target bytes differ")

if product and profile == "assured":
    manifest_raw = values.get("freeze_manifest", "")
    manifest_hash = values.get("freeze_manifest_hash", "")
    if not manifest_raw:
        reject("FREEZE_MANIFEST_MISSING", "assured product lane requires a manifest")
    manifest = Path(manifest_raw)
    if sha256_file(manifest) != manifest_hash:
        reject("FREEZE_MANIFEST_HASH_MISMATCH", "freeze manifest hash differs")
    entries: list[tuple[str, str]] = []
    try:
        for line in file_text(manifest).splitlines():
            match = re.fullmatch(r"([0-9A-Fa-f]{64}) [ *](.+)", line)
            if match is None:
                raise ValueError
            entries.append((match.group(1).lower(), match.group(2)))
    except (OSError, UnicodeError, ValueError):
        reject("FREEZE_MANIFEST_FORMAT", "freeze manifest is malformed")
    paths = [path for _hash, path in entries]
    if str(manifest) in paths:
        reject("FREEZE_MANIFEST_SELF_REFERENCE", "freeze manifest lists itself")
    if paths.count(values["execution_contract"]) != 1:
        reject("FREEZE_CONTRACT_MISSING", "manifest must contain the execution contract once")
    guard_path = os.environ.get("FORGE_GUARD_CANONICAL_PATH", "")
    guard_test = str(Path(guard_path).resolve().parents[0].parent / "tests" / "test_dispatch_guard.sh")
    required_controls = [
        values["plan_file"], values["review_log"], values["execution_contract"],
        values["project_overlay"], values.get("engine_contract", ""),
        values.get("routing_policy", ""), values.get("liveness_fixture", ""),
        values.get("watchdog_fixture", ""),
        values.get("watchdog_tests_fixture", ""), guard_path, guard_test,
    ]
    if "" in required_controls:
        reject(
            "FREEZE_REQUIRED_CONTROL_MISSING",
            "assured admission omits a named Task-11/Task-12 frozen control",
        )
    if len(set(required_controls)) != len(required_controls):
        reject("FREEZE_CONTROL_ALIAS", "frozen control roles must be distinct")
    if any(paths.count(path) != 1 for path in required_controls):
        reject("FREEZE_REQUIRED_CONTROL_MISSING", "manifest omits a required Task-11 control")
    for expected_hash, raw_path in entries:
        path = Path(raw_path)
        if sha256_file(path) != expected_hash:
            reject("FREEZE_CONTENT_MISMATCH", f"frozen content differs: {path}")
    if oracle_exact == "true" and paths.count(capability) != 1:
        reject("FREEZE_REQUIRED_CONTROL_MISSING", "manifest omits capability receipt")

proof = values.get("proof_command", "")
proof_scope = values["proof_scope"]
if proof_scope not in {"targeted", "broad"}:
    reject("FIELD_PROOF_SCOPE", "proof scope must be targeted or broad")
segments = re.split(r"&&|;|\|", proof)
broad = False
for segment in segments:
    lowered = segment.casefold()
    if re.search(r"\b(gate|typecheck|coverage|db:(?:reset|rebuild)|database rebuild)\b", lowered):
        broad = True
    if re.search(r"\b(?:pnpm|npm|yarn|bun)\s+(?:run\s+)?(?:test|build)\b", lowered):
        targeted = bool(
            re.search(r"(?:\s-t\s|--test-name-pattern|\.test\.|\.spec\.|vitest\s+run\s+\S+)", lowered)
        )
        if not targeted:
            broad = True
    if re.search(r"\bgo\s+test\s+\./\.\.\.", lowered):
        broad = True
    if re.search(r"\b(build|compile)\b", lowered) and "--filter" not in lowered:
        broad = True
if proof_scope == "targeted" and broad:
    reject("PROOF_SCOPE_UNDERDECLARED", "broad proof declared targeted")
if proof_scope == "broad" and weight == "LIGHT":
    reject("WEIGHT_UNDERDECLARED", "broad proof requires HEAVY")

verbs = (
    r"(?:commit(?:s|ted|ting)?|merge(?:s|d|ing)?|push(?:es|ed|ing)?|"
    r"cherry-?pick(?:s|ed|ing)?|rebase(?:s|d|ing)?|checkpoint(?:s|ed|ing)?)"
)
negation = r"(?<!\w)(?:do not|does not|don't|never|must not|shall not|should not|cannot|can't|without|no)"
horizontal = r"[ \t]"
negative_list_separator = (
    r"(?:"
    + horizontal
    + r"*,"
    + horizontal
    + r"*(?:(?:or|and|nor)"
    + horizontal
    + r"+)?|"
    + horizontal
    + r"+(?:or|and|nor)"
    + horizontal
    + r"+)"
)
negative_safety_lead = (
    r"(?:(?:stage|staging|publish|publishing)"
    + negative_list_separator
    + r")*"
)
mutating_git_subcommands = frozenset(
    {
        "add",
        "am",
        "apply",
        "bisect",
        "branch",
        "checkout",
        "cherry-pick",
        "clean",
        "commit",
        "commit-tree",
        "config",
        "fast-import",
        "fetch",
        "filter-branch",
        "gc",
        "init",
        "maintenance",
        "merge",
        "mktree",
        "mv",
        "notes",
        "pack-refs",
        "prune",
        "pull",
        "push",
        "read-tree",
        "rebase",
        "reflog",
        "remote",
        "replace",
        "reset",
        "restore",
        "revert",
        "rm",
        "send-pack",
        "sparse-checkout",
        "stash",
        "submodule",
        "switch",
        "symbolic-ref",
        "tag",
        "unpack-objects",
        "update-index",
        "update-ref",
        "worktree",
    }
)
git_authority = "(?:" + "|".join(
    re.escape(name) for name in sorted(mutating_git_subcommands, key=len, reverse=True)
) + ")"


def mutating_git_subcommand(segment: str) -> str | None:
    """Structurally locate the Git subcommand after bounded global options."""

    try:
        tokens = shlex.split(segment, comments=False, posix=True)
    except ValueError:
        reject("EXECUTOR_GIT_AUTHORITY", "executor Git command is malformed")
    if not tokens or tokens[0].casefold() != "git":
        return None
    takes_value = {
        "-C",
        "-c",
        "--config-env",
        "--exec-path",
        "--git-dir",
        "--namespace",
        "--super-prefix",
        "--work-tree",
    }
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            break
        if token in takes_value:
            index += 2
            continue
        if (
            (token.startswith("-C") or token.startswith("-c"))
            and len(token) > 2
        ) or any(
            token.startswith(prefix)
            for prefix in (
                "--config-env=",
                "--exec-path=",
                "--git-dir=",
                "--namespace=",
                "--super-prefix=",
                "--work-tree=",
            )
        ):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        candidate = token.casefold().strip(".,:()[]{}")
        return candidate if candidate in mutating_git_subcommands else None
    if index < len(tokens):
        candidate = tokens[index].casefold().strip(".,:()[]{}")
        return candidate if candidate in mutating_git_subcommands else None
    return None


def reject_executor_authority(parts: list[str]) -> None:
    lowered = "\n".join(parts).casefold()
    safe = re.sub(
        negation
        + horizontal
        + r"+"
        + negative_safety_lead
        + verbs
        + r"(?:"
        + negative_list_separator
        + verbs
        + r")*",
        " ",
        lowered,
    )
    safe = re.sub(
        negation
        + horizontal
        + r"+(?:run"
        + horizontal
        + r"+)?git(?:"
        + horizontal
        + r"+-[cC]"
        + horizontal
        + r"+[^ \t\r\n]+)*"
        + horizontal
        + r"+"
        + git_authority,
        " ",
        safe,
    )
    if re.search(r"(?<![\w])" + verbs + r"(?![\w])", safe):
        reject(
            "EXECUTOR_COMMIT_INSTRUCTION",
            "executor cannot commit, merge, push, rebase, or checkpoint",
        )
    for command in re.finditer(r"(?<![\w-])git\b", safe):
        tail = safe[command.start():]
        boundaries = [
            position
            for delimiter in ("\n", "\r", ";", "&", "|", "`")
            if (position := tail.find(delimiter)) >= 0
        ]
        segment = tail[: min(boundaries)] if boundaries else tail
        if mutating_git_subcommand(segment) is not None:
            reject(
                "EXECUTOR_GIT_AUTHORITY",
                "executor cannot perform driver-owned Git authority",
            )


authority_parts = [values.get("instruction", ""), proof]
for authority_name in (
    "plan_file",
    "review_log",
    "execution_contract",
    "project_overlay",
    "engine_contract",
    "routing_policy",
    "liveness_fixture",
    "watchdog_fixture",
    "watchdog_tests_fixture",
):
    authority_path = values.get(authority_name, "")
    if authority_path and file_is_regular(Path(authority_path)):
        authority_parts.append(file_text(Path(authority_path)))
# Review domain documents are hash-pinned evidence, not governing instructions.
# Source and patch evidence routinely contains API names such as `push`, domain
# terms such as `Merge`, and SQL transaction literals such as `COMMIT`; treating
# those bytes as executor authority makes exact implementation review impossible.
# Plan, review log, execution controls, overlay, and the explicit instruction
# remain authority-scanned above.
reject_executor_authority(authority_parts)

measured_raw = os.environ.get("FORGE_GUARD_WIP_MEASURED")
if measured_raw is None or re.fullmatch(r"[0-9]+", measured_raw) is None:
    reject("WIP_UNMEASURABLE", "trusted WIP measurement is unavailable")
measured_wip = int(measured_raw)
declared_wip = values.get("unmerged_branches", "")
if declared_wip:
    if re.fullmatch(r"[0-9]+", declared_wip) is None:
        reject("FIELD_UNMERGED_BRANCHES", "declared WIP must be non-negative")
    if int(declared_wip) != measured_wip:
        reject("DECLARED_WIP_MISMATCH", "declared WIP differs from measurement")
if action == "launch" and lane == "feature" and measured_wip > 10:
    reject("WIP_CAP", "feature WIP exceeds ten")

predecessor = values.get("predecessor", "")
if not predecessor:
    reject("FIELD_PREDECESSOR", "predecessor is required")
if predecessor.casefold() != "none":
    predecessor_builder = values.get("predecessor_builder", "")
    if not predecessor_builder:
        reject("RECEIPT_PREDECESSOR_BUILDER", "predecessor builder is required")
    report_receipt = values.get("report_receipt", "")
    if not report_receipt:
        reject("RECEIPT_REPORT_MISSING", "successor requires report receipt")
    review_receipt = values.get("review_receipt", "")
    if profile == "assured" and not review_receipt:
        reject("RECEIPT_REVIEW_MISSING", "assured successor requires review receipt")

    def receipt(raw: str) -> dict[str, str]:
        result = {}
        for pair in raw.split(";"):
            if "=" not in pair:
                continue
            key, value = pair.split("=", 1)
            result[key] = value
        return result

    report_values = receipt(report_receipt)
    report_path = report_values.get("report", "")
    if (
        report_values.get("predecessor") != predecessor
        or not report_values.get("consumed_by")
        or not report_values.get("receipt_id")
    ):
        reject("RECEIPT_REPORT_FORMAT", "report receipt is malformed")
    if not report_path.startswith("/") or not file_is_regular(Path(report_path)) or file_size(Path(report_path)) == 0:
        reject("RECEIPT_REPORT_FILE", "report receipt file is invalid")
    report_hash = report_values.get("report_sha256")
    if report_hash and sha256_file(Path(report_path)) != report_hash:
        reject("RECEIPT_REPORT_FILE", "report receipt hash differs")
    if family(report_values["consumed_by"]) != "codex":
        reject("RECEIPT_REPORT_DRIVER", "predecessor reports must be consumed by the Codex driver")
    reject_executor_authority([file_text(Path(report_path))])
    if review_receipt:
        review = receipt(review_receipt)
        required_review = {"predecessor", "builder", "reviewer", "thread", "verdict", "receipt_id"}
        if not required_review.issubset(review) or any(not review[key] for key in required_review):
            reject("RECEIPT_REVIEW_FORMAT", "review receipt is malformed")
        if review["predecessor"] != predecessor:
            reject("RECEIPT_REVIEW_PREDECESSOR", "review predecessor differs")
        if review["builder"].casefold() != predecessor_builder.casefold():
            reject("RECEIPT_REVIEW_BUILDER", "review builder differs")
        if family(review["builder"]) == "unknown" or family(review["reviewer"]) == "unknown":
            reject("RECEIPT_REVIEW_IDENTITY", "review identities are unknown")
        if family(review["builder"]) == family(review["reviewer"]):
            reject("RECEIPT_REVIEW_NOT_CROSS_MODEL", "review is not cross-model")
        if review["verdict"] != "APPROVED":
            reject("RECEIPT_REVIEW_VERDICT", "review verdict is not APPROVED")

# Trusted machine probes. Direct guard tests supply deterministic overrides; the
# bridge scrubs ambient FORGE_GUARD_* and writes its own private snapshot.
def env_number(name: str, default: str | None = None) -> float:
    raw = os.environ.get(name, default)
    try:
        result = float(raw) if raw is not None else math.nan
    except (TypeError, ValueError):
        result = math.nan
    if not math.isfinite(result) or result < 0:
        reject("MACHINE_PROBE", f"invalid machine value {name}")
    return result

cores = env_number("FORGE_GUARD_CORE_COUNT")
if cores <= 0:
    reject("MACHINE_VALUE_CORES", "core count must be positive")
load = env_number("FORGE_GUARD_LOAD_1")
swap = env_number("FORGE_GUARD_FREE_SWAP_GB")
cpu_idle = env_number("FORGE_GUARD_CPU_IDLE_PCT")
memory_free = env_number("FORGE_GUARD_MEMORY_FREE_PCT")
if cpu_idle > 100 or memory_free > 100:
    reject("MACHINE_VALUE_HEADROOM", "headroom percentages must be <= 100")
if weight == "HEAVY":
    factor = env_number("FORGE_GUARD_HEAVY_MAX_LOAD_PER_CORE", "1")
    minimum_swap = env_number("FORGE_GUARD_HEAVY_MIN_SWAP_GB", "4")
    cpu_min = env_number("FORGE_GUARD_HEAVY_HIGH_LOAD_MIN_CPU_IDLE_PCT", "50")
    memory_min = env_number("FORGE_GUARD_HEAVY_HIGH_LOAD_MIN_MEMORY_FREE_PCT", "50")
    load_code, memory_code = "MACHINE_LOAD_HEAVY", "MACHINE_MEMORY_HEAVY"
else:
    factor = env_number("FORGE_GUARD_LIGHT_MAX_LOAD_PER_CORE", "4")
    minimum_swap = env_number("FORGE_GUARD_LIGHT_MIN_SWAP_GB", "1")
    cpu_min = env_number("FORGE_GUARD_LIGHT_HIGH_LOAD_MIN_CPU_IDLE_PCT", "50")
    memory_min = env_number("FORGE_GUARD_LIGHT_HIGH_LOAD_MIN_MEMORY_FREE_PCT", "50")
    load_code, memory_code = "MACHINE_LOAD_LIGHT", "MACHINE_MEMORY_LIGHT"
maximum_load = cores * factor
if load > maximum_load:
    if memory_free < memory_min:
        reject(memory_code, "high load lacks memory headroom")
    if cpu_idle < cpu_min:
        reject(load_code, "high load lacks CPU headroom")
if swap < minimum_swap:
    reject("MACHINE_SWAP_" + weight, "swap headroom is below threshold")

guard_path = Path(os.environ["FORGE_GUARD_CANONICAL_PATH"])
guard_hash = hashlib.sha256(file_bytes(guard_path)).hexdigest()
argv_json = json.dumps(argv, ensure_ascii=False, separators=(",", ":"))
result = {
    "decision": "ADMIT",
    "version": 1,
    "guard_sha256": guard_hash,
    "argv_sha256": hashlib.sha256(argv_json.encode("utf-8")).hexdigest(),
    "action": action,
    "profile": profile,
    "proof_scope": proof_scope,
    "lane": lane,
    "weight": weight,
    "executor_policy": policy,
    "active_before": active_lanes,
    "heavy_leases_before": heavy_leases,
    "machine": {
        "cores": cores,
        "load_1": load,
        "free_swap_gb": swap,
        "cpu_idle_pct": cpu_idle,
        "memory_free_pct": memory_free,
        "max_load": maximum_load,
        "min_swap_gb": minimum_swap,
    },
    "measured_wip": measured_wip,
    "repository_clean": actual_clean,
    "scheduler_binding": {
        "repo_id": hashlib.sha256(
            json.dumps(
                {"common_dir": repository_common_dir},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "base_sha": repository_head,
        "lane": lane,
        "weight": weight,
        "writer": worktree if product else None,
        "files": files,
    },
}
print("ADMIT:" + json.dumps(result, sort_keys=True, separators=(",", ":")))
PY
