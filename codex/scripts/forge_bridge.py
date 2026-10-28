#!/usr/bin/env python3
"""Forge MCP stdio bridge entrypoint."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from forge_lib.protocol import serve_stdio
from forge_lib.worker import run_worker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Forge MCP bridge")
    parser.add_argument("--worker", metavar="JOB_ID", help="run one detached Forge worker")
    parser.add_argument(
        "--dispatch-token",
        help="fence one detached worker to its durable dispatch claim",
    )
    parser.add_argument(
        "--launch-gate-fd",
        type=int,
        help="inherited parent/child launch-binding gate descriptor",
    )
    args = parser.parse_args(argv)
    if args.worker is not None:
        try:
            return run_worker(
                args.worker,
                dispatch_token=args.dispatch_token,
                launch_gate_fd=args.launch_gate_fd,
            )
        except Exception:
            print("Forge worker failed to initialize", file=sys.stderr)
            return 1
    serve_stdio()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
