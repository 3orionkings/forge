#!/usr/bin/env python3
"""Test-only bridge entrypoint with an opaque admission headroom probe."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import forge_bridge  # noqa: E402
from forge_lib import service  # noqa: E402
from tests.admission_fixture import machine_headroom  # noqa: E402


_production_from_environment = service.ForgeService.from_environment.__func__


def _admission_from_environment(cls):
    instance = _production_from_environment(cls)
    instance.bridge_path = Path(__file__).resolve(strict=True)
    instance._admission_machine_probe = machine_headroom
    return instance


service.ForgeService.from_environment = classmethod(_admission_from_environment)


if __name__ == "__main__":
    raise SystemExit(forge_bridge.main())
