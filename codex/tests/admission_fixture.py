"""Shared declaration-only Task-11 admission fixture for legacy bridge tests."""

from __future__ import annotations

import subprocess
from pathlib import Path


def seed_wip_base(repository: Path) -> None:
    """Publish the canonical WIP base required by production admission."""

    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(repository),
            "update-ref",
            "refs/remotes/origin/HEAD",
            "HEAD",
        ],
        check=True,
    )


def review_admission(*, module: str) -> dict[str, object]:
    """Return a fresh, caller-safe review admission declaration."""

    return {
        "weight": "LIGHT",
        "proof_scope": "targeted",
        "execution_profile": "sprint",
        "proof_argv": ["python3", "-m", "unittest", module],
        "files": [],
    }


def machine_headroom() -> dict[str, float | int]:
    """Opaque deterministic probe injected only by test entrypoints."""

    return {
        "core_count": 10,
        "load_1": 1.0,
        "free_swap_gb": 16.0,
        "cpu_idle_pct": 80.0,
        "memory_free_pct": 80.0,
    }
