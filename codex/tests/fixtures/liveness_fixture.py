#!/usr/bin/env python3
"""Create bounded, provider-free bridge state for Task 12 shell tests."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from forge_lib.processes import process_instance_identity  # noqa: E402
from forge_lib.state import Store  # noqa: E402


def timestamp(seconds_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--state", required=True)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--worker-pid", type=int)
    arguments = parser.parse_args()

    store = Store(Path(arguments.state))
    job = store.create_job(
        repo_id="fixture-repo",
        idempotency_key=f"fixture-{arguments.mode}",
        request_hash="f" * 64,
        payload={"model": "claude-opus-4-1", "effort": "high"},
    )
    store.transition(job.id, expected_revision=0, new_status="PREFLIGHT")
    store.transition(job.id, expected_revision=1, new_status="RUNNING")
    attempt = store.get_attempt(job.current_attempt_id)
    provider_pid = arguments.pid or 2_147_000_000
    provider_identity = (
        process_instance_identity(provider_pid)
        if arguments.pid is not None
        else "0" * 64
    )
    worker_pid = arguments.worker_pid or provider_pid
    worker_identity = (
        process_instance_identity(worker_pid)
        if arguments.worker_pid is not None
        else provider_identity
    )
    now = timestamp(0)
    old = timestamp(1300)
    attempt_timestamp = now if arguments.mode == "alive" else old

    with store.connection() as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(attempts)")
        }
        if "provider_identity" not in columns:
            connection.execute("ALTER TABLE attempts ADD COLUMN provider_identity TEXT")
            connection.execute(
                "UPDATE schema_version SET version=11 WHERE singleton=1"
            )
        connection.execute(
            "UPDATE attempts SET worker_pid=?, worker_identity=?, provider_pgid=?, "
            "provider_identity=?, created_at=?, updated_at=? WHERE id=?",
            (
                worker_pid,
                worker_identity,
                provider_pid,
                provider_identity,
                attempt_timestamp,
                old,
                attempt.id,
            ),
        )
        connection.execute(
            "UPDATE jobs SET updated_at=? WHERE id=?", (old, job.id)
        )

    if arguments.mode in {"pause_provider", "bad_pause_provider"}:
        receipt = b'{"pause":"provider_access"}\n'
        receipt_path = store.create_attempt_artifact(
            job.id, attempt.id, "receipt.json", receipt
        )
        receipt_hash = hashlib.sha256(receipt).hexdigest()
        if arguments.mode == "bad_pause_provider":
            receipt_path.write_bytes(b"substituted\n")
        with store.connection() as connection:
            connection.execute(
                "UPDATE attempts SET status='PAUSED_PROVIDER_ACCESS', "
                "terminal_receipt_name='receipt.json', terminal_receipt_hash=? WHERE id=?",
                (receipt_hash, attempt.id),
            )
            connection.execute(
                "UPDATE jobs SET status='PAUSED_PROVIDER_ACCESS' WHERE id=?",
                (job.id,),
            )
    elif arguments.mode == "pause_policy":
        receipt = b'{"pause":"policy"}\n'
        store.create_attempt_artifact(job.id, attempt.id, "receipt.json", receipt)
        receipt_hash = hashlib.sha256(receipt).hexdigest()
        with store.connection() as connection:
            connection.execute(
                "UPDATE attempts SET status='PAUSED_POLICY', "
                "terminal_receipt_name='receipt.json', terminal_receipt_hash=? WHERE id=?",
                (receipt_hash, attempt.id),
            )
            connection.execute(
                "UPDATE jobs SET status='PAUSED_POLICY' WHERE id=?", (job.id,)
            )
    elif arguments.mode in {"objective", "stale_objective"}:
        store.append_event(
            job.id,
            "OBJECTIVE_PROGRESS",
            {"evidence_hash": "1" * 64, "source": "artifact"},
        )
        if arguments.mode == "stale_objective":
            with store.connection() as connection:
                connection.execute(
                    "UPDATE events SET created_at=? WHERE job_id=? AND kind='OBJECTIVE_PROGRESS'",
                    (old, job.id),
                )
    elif arguments.mode == "chatter":
        store.append_event(
            job.id,
            "CLAUDE_EVENT",
            {"type": "assistant", "hash": "2" * 64},
        )
        with store.connection() as connection:
            connection.execute(
                "UPDATE events SET created_at=? WHERE job_id=?", (old, job.id)
            )
    elif arguments.mode == "repeated":
        store.append_event(
            job.id,
            "STDERR_SIGNATURE",
            {
                "signature_sha256": "3" * 64,
                "repeat_count": 2,
                "byte_count": 17,
            },
        )
    elif arguments.mode == "repeated_then_objective":
        store.append_event(
            job.id,
            "STDERR_SIGNATURE",
            {
                "signature_sha256": "3" * 64,
                "repeat_count": 2,
                "byte_count": 17,
            },
        )
        store.append_event(
            job.id,
            "OBJECTIVE_PROGRESS",
            {"evidence_hash": "1" * 64, "source": "artifact"},
        )
    elif arguments.mode in {"done", "done_pause", "bad_receipt"}:
        receipt = b'{"terminal":true}\n'
        receipt_path = store.create_attempt_artifact(
            job.id, attempt.id, "receipt.json", receipt
        )
        receipt_hash = hashlib.sha256(receipt).hexdigest()
        if arguments.mode == "bad_receipt":
            receipt_path.write_bytes(b"substituted\n")
        with store.connection() as connection:
            connection.execute(
                "UPDATE attempts SET status='SUCCEEDED', terminal_receipt_name='receipt.json', "
                "terminal_receipt_hash=? WHERE id=?",
                (receipt_hash, attempt.id),
            )
            connection.execute(
                "UPDATE jobs SET status='SUCCEEDED' WHERE id=?", (job.id,)
            )
            if arguments.mode == "done_pause":
                connection.execute(
                    "INSERT INTO events(job_id,attempt_id,sequence,kind,payload_json,"
                    "previous_hash,event_hash,revision,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        job.id,
                        attempt.id,
                        1,
                        "PROVIDER_PAUSED",
                        json.dumps({"status": "PAUSED_POLICY"}),
                        None,
                        "4" * 64,
                        3,
                        now,
                    ),
                )

    current_revision = store.get_job(job.id).revision
    print(
        json.dumps(
            {
                "job_id": job.id,
                "attempt_id": attempt.id,
                "revision": current_revision,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
