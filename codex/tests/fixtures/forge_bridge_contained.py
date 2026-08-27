#!/usr/bin/env python3
"""Test-only bridge entrypoint with an explicit containment dependency."""

from __future__ import annotations

import sys
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import forge_bridge  # noqa: E402
from forge_lib import processes, service  # noqa: E402
from tests.admission_fixture import machine_headroom  # noqa: E402


TEST_CONTAINMENT = processes._test_cancellation_namespace(
    Path(os.environ["TMPDIR"]) / "descendant-manifest.jsonl"
)
_production_from_environment = service.ForgeService.from_environment.__func__
_production_run_worker = forge_bridge.run_worker


def _contained_from_environment(cls):
    instance = _production_from_environment(cls)
    instance.bridge_path = Path(__file__).resolve(strict=True)
    instance.cancellation_containment = TEST_CONTAINMENT
    instance._admission_machine_probe = machine_headroom
    return instance


def _contained_run_worker(
    job_id,
    *,
    dispatch_token=None,
    state_path=None,
    launch_gate_fd=None,
    dispatch_role=None,
):
    return _production_run_worker(
        job_id,
        dispatch_token=dispatch_token,
        state_path=state_path,
        launch_gate_fd=launch_gate_fd,
        dispatch_role=dispatch_role,
        cancellation_containment=TEST_CONTAINMENT,
    )


service.ForgeService.from_environment = classmethod(_contained_from_environment)
forge_bridge.run_worker = _contained_run_worker


if __name__ == "__main__":
    raise SystemExit(forge_bridge.main())
