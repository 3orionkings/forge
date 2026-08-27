import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from forge_lib.state import SCHEMA_VERSION, StateError, Store  # noqa: E402


IDENTITY_A = "a" * 64
IDENTITY_B = "b" * 64


class BridgeObservationContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / "state" / "state.sqlite3")

    def job(self, key="job"):
        job = self.store.create_job(
            repo_id="repo",
            idempotency_key=key,
            request_hash="f" * 64,
            payload={
                "prompt": "provider-free fixture",
                "cwd": str(self.root),
                "role": "review",
                "model": "claude-opus-4-1",
                "effort": "high",
                "budget": {"max_seconds": 300, "max_tokens": 1000},
                "max_budget_usd": "1",
                "baseline": {"head": "0" * 40, "status_fingerprint": "1" * 64},
                "claude_executable": "/fixture/claude",
            },
        )
        return job

    def running_job(self, key="running"):
        job = self.job(key)
        self.store.transition(job.id, expected_revision=0, new_status="PREFLIGHT")
        self.store.transition(job.id, expected_revision=1, new_status="RUNNING")
        return self.store.get_job(job.id)

    def test_schema_v11_persists_provider_birth_identity(self):
        self.assertEqual(SCHEMA_VERSION, 12)
        with self.store.connection() as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(attempts)")
            }
        self.assertIn("provider_identity", columns)

        job = self.running_job("provider-identity")
        with mock.patch(
            "forge_lib.state.process_instance_identity", return_value=IDENTITY_A
        ):
            self.store.record_provider_identity(
                job.id,
                provider_pgid=321,
                provider_identity=IDENTITY_A,
            )
        attempt = self.store.get_attempt(job.current_attempt_id)
        self.assertEqual(attempt.provider_pgid, 321)
        self.assertEqual(attempt.provider_identity, IDENTITY_A)

    def test_provider_identity_rejects_pid_reuse_and_is_immutable(self):
        job = self.running_job("provider-reuse")
        with mock.patch(
            "forge_lib.state.process_instance_identity", return_value=IDENTITY_B
        ):
            with self.assertRaisesRegex(StateError, "identity"):
                self.store.record_provider_identity(
                    job.id,
                    provider_pgid=322,
                    provider_identity=IDENTITY_A,
                )
        with mock.patch(
            "forge_lib.state.process_instance_identity", return_value=IDENTITY_A
        ):
            self.store.record_provider_identity(
                job.id,
                provider_pgid=322,
                provider_identity=IDENTITY_A,
            )
            with self.assertRaisesRegex(StateError, "identity"):
                self.store.record_provider_identity(
                    job.id,
                    provider_pgid=322,
                    provider_identity=IDENTITY_B,
                )

    def test_liveness_snapshot_is_atomic_attempt_and_revision_scoped(self):
        job = self.running_job("snapshot")
        with mock.patch(
            "forge_lib.state.process_instance_identity", return_value=IDENTITY_A
        ):
            self.store.record_provider_identity(
                job.id,
                provider_pgid=323,
                provider_identity=IDENTITY_A,
            )
        current = self.store.get_job(job.id)
        snapshot = self.store.get_liveness_snapshot(
            job.id,
            attempt_id=current.current_attempt_id,
            expected_revision=current.revision,
        )
        self.assertEqual(snapshot["job_id"], job.id)
        self.assertEqual(snapshot["attempt_id"], current.current_attempt_id)
        self.assertEqual(snapshot["revision"], current.revision)
        self.assertEqual(snapshot["provider_identity"], IDENTITY_A)
        with self.assertRaisesRegex(StateError, "revision"):
            self.store.get_liveness_snapshot(
                job.id,
                attempt_id=current.current_attempt_id,
                expected_revision=current.revision + 1,
            )

    def test_repeated_stderr_is_durable_hash_only_and_never_raw(self):
        secret = "client_secret=CANARY-NEVER-PERSIST-123456"
        job = self.running_job("stderr-hash")
        with mock.patch(
            "forge_lib.state.process_instance_identity", return_value=IDENTITY_A
        ):
            self.store.record_provider_identity(
                job.id,
                provider_pgid=324,
                provider_identity=IDENTITY_A,
            )
            event = self.store.record_stderr_signature(
                job.id,
                provider_pgid=324,
                provider_identity=IDENTITY_A,
                signature_sha256="c" * 64,
                repeat_count=2,
                byte_count=len(secret.encode()),
            )
        self.assertEqual(event.kind, "STDERR_SIGNATURE")
        self.assertEqual(
            set(event.payload),
            {"signature_sha256", "repeat_count", "byte_count"},
        )
        serialized = json.dumps(event.payload, sort_keys=True)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("client_secret", serialized)

    def test_stderr_signature_requires_running_exact_provider_identity(self):
        job = self.running_job("stderr-authority")
        with mock.patch(
            "forge_lib.state.process_instance_identity", return_value=IDENTITY_A
        ):
            self.store.record_provider_identity(
                job.id,
                provider_pgid=325,
                provider_identity=IDENTITY_A,
            )
        with mock.patch(
            "forge_lib.state.process_instance_identity", return_value=IDENTITY_B
        ):
            with self.assertRaisesRegex(StateError, "identity"):
                self.store.record_stderr_signature(
                    job.id,
                    provider_pgid=325,
                    provider_identity=IDENTITY_A,
                    signature_sha256="d" * 64,
                    repeat_count=2,
                    byte_count=10,
                )

    def test_worker_declared_artifact_change_records_monotonic_objective_progress(self):
        from forge_lib.worker import _ObjectiveArtifactTracker

        job = self.running_job("artifact-progress")
        declared = self.root / "declared.txt"
        declared.write_text("before\n", encoding="utf-8")
        with mock.patch(
            "forge_lib.state.process_instance_identity", return_value=IDENTITY_A
        ):
            self.store.record_provider_identity(
                job.id,
                provider_pgid=327,
                provider_identity=IDENTITY_A,
            )
            tracker = _ObjectiveArtifactTracker.from_declared_paths(
                store=self.store,
                job_id=job.id,
                provider_pgid=327,
                provider_identity=IDENTITY_A,
                root=self.root,
                declared_files=("declared.txt",),
            )
            self.assertFalse(tracker.poll(force=True))
            declared.touch()
            self.assertFalse(tracker.poll(force=True), "mtime-only drift is not progress")
            declared.write_text("after\n", encoding="utf-8")
            self.assertTrue(tracker.poll(force=True))
            self.assertFalse(tracker.poll(force=True), "same digest is idempotent")
        events = self.store.list_events(job.id)
        objective = [event for event in events if event.kind == "OBJECTIVE_PROGRESS"]
        self.assertEqual(len(objective), 1)
        self.assertEqual(
            set(objective[0].payload),
            {"source", "evidence_sha256", "changed_file_count"},
        )
        self.assertEqual(objective[0].payload["source"], "declared_artifact")

    def test_worker_tracker_emits_only_after_second_equal_redacted_line(self):
        from forge_lib.worker import _RepeatedStderrTracker

        job = self.running_job("tracker")
        with mock.patch(
            "forge_lib.state.process_instance_identity", return_value=IDENTITY_A
        ):
            self.store.record_provider_identity(
                job.id,
                provider_pgid=326,
                provider_identity=IDENTITY_A,
            )
            tracker = _RepeatedStderrTracker(
                store=self.store,
                job_id=job.id,
                provider_pgid=326,
                provider_identity=IDENTITY_A,
            )
            tracker.feed(b"api_key=CANARY-TRACKER-SECRET\n")
            self.assertEqual(
                [event.kind for event in self.store.list_events(job.id)], []
            )
            tracker.feed(b"api_key=CANARY-TRACKER-SECRET\n")
        events = self.store.list_events(job.id)
        self.assertEqual([event.kind for event in events], ["STDERR_SIGNATURE"])
        persisted = json.dumps(events[0].payload, sort_keys=True)
        self.assertNotIn("CANARY-TRACKER-SECRET", persisted)
        self.assertLessEqual(events[0].payload["byte_count"], 4096)

    def test_stderr_tracker_stops_observing_after_expected_cancel_transition(self):
        from forge_lib.state import InvalidTransition
        from forge_lib.worker import _RepeatedStderrTracker

        class TransitionedStore:
            def __init__(self):
                self.calls = 0

            def record_stderr_signature(self, *_args, **_kwargs):
                self.calls += 1
                raise InvalidTransition("attempt is interrupting")

        transitioned = TransitionedStore()
        tracker = _RepeatedStderrTracker(
            store=transitioned,
            job_id="job",
            provider_pgid=326,
            provider_identity=IDENTITY_A,
        )
        tracker.feed(b"same line\nsame line\n")
        tracker.feed(b"same line\nsame line\n")
        self.assertEqual(transitioned.calls, 1)

    def test_service_status_exposes_bounded_attempt_scoped_liveness(self):
        from forge_lib.service import ForgeService

        job = self.job("service-liveness")
        service = ForgeService.__new__(ForgeService)
        service.store = self.store
        body = service._status_body(job)
        self.assertEqual(
            set(body["liveness"]),
            {
                "attempt_id",
                "revision",
                "worker_identity_recorded",
                "provider_identity_recorded",
                "last_objective_progress_at",
                "last_stderr_signature_sha256",
                "last_stderr_repeat_count",
            },
        )
        self.assertEqual(body["liveness"]["attempt_id"], job.current_attempt_id)
        self.assertFalse(body["liveness"]["provider_identity_recorded"])
        self.assertNotIn("worker_pid", body["liveness"])
        self.assertNotIn("provider_pgid", body["liveness"])

    def test_exact_cpu_sampler_fails_closed_across_identity_change(self):
        from forge_lib.processes import ProcessError, sample_process_cpu_seconds

        values = iter([IDENTITY_A, IDENTITY_B])
        with mock.patch(
            "forge_lib.processes.process_instance_identity",
            side_effect=lambda _pid: next(values),
        ):
            with self.assertRaisesRegex(ProcessError, "identity"):
                sample_process_cpu_seconds(os.getpid(), IDENTITY_A)

    def test_liveness_rejects_schema_v10_without_mutating_it(self):
        job = self.running_job("readonly-schema")
        state_path = self.store.path
        with closing(sqlite3.connect(state_path)) as connection:
            connection.execute("ALTER TABLE attempts DROP COLUMN provider_identity")
            connection.execute(
                "UPDATE schema_version SET version=10 WHERE singleton=1"
            )
            connection.commit()

        completed = subprocess.run(
            [
                str(ROOT / "scripts" / "forge-liveness.sh"),
                "--state",
                str(state_path),
                "--job",
                job.id,
                "--attempt",
                job.current_attempt_id,
                "--revision",
                str(job.revision),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(json.loads(completed.stdout)["status"], "INVALID")
        with closing(sqlite3.connect(state_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT version FROM schema_version WHERE singleton=1"
                ).fetchone()[0],
                10,
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(attempts)")
            }
        self.assertNotIn("provider_identity", columns)


if __name__ == "__main__":
    unittest.main()
