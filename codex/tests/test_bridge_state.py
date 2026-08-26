import concurrent.futures
import errno
import hashlib
import json
import sqlite3
import stat
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from forge_lib.state import (  # noqa: E402
    SCHEMA_VERSION,
    TRANSITIONS,
    IdempotencyConflict,
    InvalidTransition,
    RevisionConflict,
    Store,
)


EXPECTED_TRANSITIONS = {
    "CREATED": {"PREFLIGHT", "FAILED"},
    "PREFLIGHT": {
        "RUNNING",
        "PAUSED_PROVIDER_ACCESS",
        "PAUSED_POLICY",
        "FAILED",
    },
    "RUNNING": {
        "SUCCEEDED",
        "PAUSED_PROVIDER_ACCESS",
        "PAUSED_POLICY",
        "INTERRUPTING",
        "FAILED",
    },
    "INTERRUPTING": {"CANCELLED", "FAILED"},
    "SUCCEEDED": set(),
    "CANCELLED": set(),
    "FAILED": set(),
    "PAUSED_PROVIDER_ACCESS": set(),
    "PAUSED_POLICY": set(),
}


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


class TracedStore(Store):
    def __init__(self, path):
        self.statements = []
        super().__init__(path)

    def _connect(self, *, busy_timeout_ms=10_000):
        connection = super()._connect(busy_timeout_ms=busy_timeout_ms)
        connection.set_trace_callback(self.statements.append)
        return connection


class BridgeStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temp_path = Path(self.temporary_directory.name)

    def make_store(self, name="state"):
        return Store(self.temp_path / name / "state.sqlite3")

    def test_store_uses_private_modes_wal_and_autocommit_connection_settings(self):
        store = self.make_store()
        job = store.create_job(
            repo_id="repo",
            idempotency_key="private-job",
            request_hash="hash",
            payload={},
        )
        artifact = store.create_artifact(job.id, "report.json", b"{}")

        with artifact.open("rb") as reader_of_original_inode:
            replaced = store.create_artifact(job.id, "report.json", b'{"done":true}')
            reader_of_original_inode.seek(0)
            self.assertEqual(reader_of_original_inode.read(), b"{}")
        self.assertEqual(replaced.read_bytes(), b'{"done":true}')

        self.assertEqual(mode(store.path.parent), 0o700)
        self.assertEqual(mode(store.runs_path), 0o700)
        self.assertEqual(mode(artifact.parent), 0o700)
        self.assertEqual(mode(store.path), 0o600)
        self.assertEqual(mode(replaced), 0o600)

        with store.connection() as connection:
            self.assertIsNone(connection.isolation_level)
            self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 10_000)
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")

    def test_concurrent_readers_tolerate_wal_sidecar_teardown_after_read_succeeds(self):
        store = self.make_store()
        job = store.create_job(
            repo_id="repo",
            idempotency_key="reader-race",
            request_hash="hash",
            payload={},
        )
        wal_path = store.path.with_name(store.path.name + "-wal")
        original_exists = Path.exists
        original_chmod = Path.chmod
        race_injected = threading.Event()

        def racing_exists(path):
            if path == wal_path and not race_injected.is_set():
                return True
            return original_exists(path)

        def racing_chmod(path, permissions):
            if path == wal_path and not race_injected.is_set():
                race_injected.set()
                raise FileNotFoundError(errno.ENOENT, "sidecar removed", str(path))
            return original_chmod(path, permissions)

        reader_count = 16
        start = threading.Barrier(reader_count)

        def read_job(_):
            start.wait(timeout=5)
            return store.get_job(job.id).id

        with (
            mock.patch.object(Path, "exists", new=racing_exists),
            mock.patch.object(Path, "chmod", new=racing_chmod),
            concurrent.futures.ThreadPoolExecutor(max_workers=reader_count) as executor,
        ):
            results = list(executor.map(read_job, range(reader_count)))

        self.assertTrue(race_injected.is_set())
        self.assertEqual(results, [job.id] * reader_count)

    def test_schema_version_migrates_from_zero_and_creates_all_state_tables(self):
        root = self.temp_path / "migration"
        root.mkdir()
        database = root / "state.sqlite3"
        connection = sqlite3.connect(database)
        connection.execute(
            "CREATE TABLE schema_version ("
            "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
            "version INTEGER NOT NULL)"
        )
        connection.execute("INSERT INTO schema_version(singleton, version) VALUES (1, 0)")
        connection.commit()
        connection.close()

        store = Store(database)
        with store.connection() as connection:
            version = connection.execute(
                "SELECT version FROM schema_version WHERE singleton = 1"
            ).fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }

        self.assertEqual(version, SCHEMA_VERSION)
        self.assertTrue(
            {
                "schema_version",
                "jobs",
                "attempts",
                "attempt_evidence_locks",
                "events",
                "commands",
            }
            <= tables
        )
        # Reopening must be an idempotent migration, not a destructive rebuild.
        Store(database)
        self.assertEqual(mode(database), 0o600)

    def test_v6_attempt_lock_is_durably_pinned_before_reopened_store_is_usable(self):
        store = self.make_store("v6-lock-bootstrap")
        job = store.create_job(
            repo_id="repo",
            idempotency_key="v6-lock-bootstrap",
            request_hash="hash",
            payload={},
        )
        lock_path = (
            store.root_path
            / "attempt-locks"
            / f"{job.id}-{job.current_attempt_id}.lock"
        )
        original = lock_path.stat()
        with store.connection() as connection:
            connection.execute("DROP TABLE attempt_evidence_locks")
            connection.execute("ALTER TABLE attempts DROP COLUMN worker_identity")
            connection.execute("ALTER TABLE attempts DROP COLUMN provider_identity")
            for column in (
                "model_override",
                "model_cli_accepted",
                "models_observed_json",
                "verification_level",
            ):
                connection.execute(f"ALTER TABLE attempts DROP COLUMN {column}")
            connection.execute(
                "UPDATE schema_version SET version = 6 WHERE singleton = 1"
            )

        reopened = Store(store.path)
        with reopened.connection() as connection:
            identity = connection.execute(
                """
                SELECT device_hex, inode_hex, owner
                FROM attempt_evidence_locks
                WHERE attempt_id = ?
                """,
                (job.current_attempt_id,),
            ).fetchone()
        self.assertEqual(
            tuple(identity),
            (format(original.st_dev, "x"), format(original.st_ino, "x"), original.st_uid),
        )
        with reopened.attempt_evidence_lease(
            job.id,
            job.current_attempt_id,
            exclusive=False,
        ):
            pass

    def test_concurrent_v7_bootstrap_converges_on_one_lock_identity(self):
        store = self.make_store("concurrent-lock-bootstrap")
        job = store.create_job(
            repo_id="repo",
            idempotency_key="concurrent-lock-bootstrap",
            request_hash="hash",
            payload={},
        )
        with store.connection() as connection:
            connection.execute(
                "DELETE FROM attempt_evidence_locks WHERE attempt_id = ?",
                (job.current_attempt_id,),
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            reopened = list(executor.map(lambda _index: Store(store.path), range(8)))
        self.assertEqual(len(reopened), 8)
        with store.connection() as connection:
            rows = connection.execute(
                """
                SELECT device_hex, inode_hex, owner
                FROM attempt_evidence_locks
                WHERE attempt_id = ?
                """,
                (job.current_attempt_id,),
            ).fetchall()
        self.assertEqual(len(rows), 1)
        information = (
            store.root_path
            / "attempt-locks"
            / f"{job.id}-{job.current_attempt_id}.lock"
        ).stat()
        self.assertEqual(
            tuple(rows[0]),
            (
                format(information.st_dev, "x"),
                format(information.st_ino, "x"),
                information.st_uid,
            ),
        )

    def test_committed_lock_identity_survives_an_interruption_before_context_exit(self):
        store = self.make_store("lock-publication-interrupt")
        real_transaction = store._transaction

        @contextmanager
        def interrupt_after_commit():
            with real_transaction() as connection:
                yield connection
            raise KeyboardInterrupt("after durable commit")

        with mock.patch.object(store, "_transaction", interrupt_after_commit):
            with self.assertRaises(KeyboardInterrupt):
                store.create_job(
                    repo_id="repo",
                    idempotency_key="lock-publication-interrupt",
                    request_hash="hash",
                    payload={},
                )

        with store.connection() as connection:
            row = connection.execute(
                """
                SELECT jobs.id AS job_id, attempts.id AS attempt_id,
                       locks.device_hex, locks.inode_hex, locks.owner
                FROM jobs
                JOIN attempts ON attempts.id = jobs.current_attempt_id
                JOIN attempt_evidence_locks AS locks
                    ON locks.attempt_id = attempts.id
                WHERE jobs.idempotency_key = ?
                """,
                ("lock-publication-interrupt",),
            ).fetchone()
        self.assertIsNotNone(row)
        lock_path = (
            store.root_path
            / "attempt-locks"
            / f"{row['job_id']}-{row['attempt_id']}.lock"
        )
        information = lock_path.stat()
        self.assertEqual(
            (row["device_hex"], row["inode_hex"], row["owner"]),
            (
                format(information.st_dev, "x"),
                format(information.st_ino, "x"),
                information.st_uid,
            ),
        )
        reopened = Store(store.path)
        with reopened.attempt_evidence_lease(
            row["job_id"],
            row["attempt_id"],
            exclusive=False,
        ):
            pass

    def test_committed_child_lock_survives_an_interruption_before_context_exit(self):
        store = self.make_store("child-lock-publication-interrupt")
        job = store.create_job(
            repo_id="repo",
            idempotency_key="child-lock-publication-interrupt",
            request_hash="hash",
            payload={},
        )
        terminal_revision = store.transition(
            job.id,
            expected_revision=job.revision,
            new_status="FAILED",
        )
        real_transaction = store._transaction

        @contextmanager
        def interrupt_after_commit():
            with real_transaction() as connection:
                yield connection
            raise KeyboardInterrupt("after child durable commit")

        with mock.patch.object(store, "_transaction", interrupt_after_commit):
            with self.assertRaises(KeyboardInterrupt):
                store.create_new_attempt(
                    job.id,
                    expected_revision=terminal_revision,
                )

        current = store.get_job(job.id)
        self.assertNotEqual(current.current_attempt_id, job.current_attempt_id)
        lock_path = (
            store.root_path
            / "attempt-locks"
            / f"{job.id}-{current.current_attempt_id}.lock"
        )
        self.assertTrue(lock_path.is_file())
        reopened = Store(store.path)
        with reopened.attempt_evidence_lease(
            job.id,
            current.current_attempt_id,
            exclusive=False,
        ):
            pass

    def test_committed_bootstrap_lock_survives_constructor_interruption(self):
        store = self.make_store("bootstrap-lock-publication-interrupt")
        job = store.create_job(
            repo_id="repo",
            idempotency_key="bootstrap-lock-publication-interrupt",
            request_hash="hash",
            payload={},
        )
        lock_path = (
            store.root_path
            / "attempt-locks"
            / f"{job.id}-{job.current_attempt_id}.lock"
        )
        with store.connection() as connection:
            connection.execute(
                "DELETE FROM attempt_evidence_locks WHERE attempt_id = ?",
                (job.current_attempt_id,),
            )
        lock_path.unlink()

        class InterruptingBootstrapStore(Store):
            @contextmanager
            def _transaction(self):
                with super()._transaction() as connection:
                    yield connection
                raise KeyboardInterrupt("after bootstrap durable commit")

        with self.assertRaises(KeyboardInterrupt):
            InterruptingBootstrapStore(store.path)

        self.assertTrue(lock_path.is_file())
        reopened = Store(store.path)
        with reopened.attempt_evidence_lease(
            job.id,
            job.current_attempt_id,
            exclusive=False,
        ):
            pass

    def test_concurrent_store_constructors_retry_journal_mode_lock_races(self):
        constructor_count = 16
        database = self.temp_path / "constructors" / "state.sqlite3"
        connections_ready = threading.Barrier(constructor_count)
        injection_lock = threading.Lock()
        journal_lock_injected = False

        class JournalModeRaceConnection:
            def __init__(self, connection):
                self.connection = connection
                self.first_journal_mode = True

            def execute(self, statement, *args):
                nonlocal journal_lock_injected
                if (
                    statement.strip().upper() == "PRAGMA JOURNAL_MODE=WAL"
                    and self.first_journal_mode
                ):
                    self.first_journal_mode = False
                    connections_ready.wait(timeout=5)
                    with injection_lock:
                        if not journal_lock_injected:
                            journal_lock_injected = True
                            raise sqlite3.OperationalError("database is locked")
                return self.connection.execute(statement, *args)

            def __getattr__(self, name):
                return getattr(self.connection, name)

        class SynchronizedStore(Store):
            def _connect(self, *, busy_timeout_ms=10_000):
                return JournalModeRaceConnection(
                    super()._connect(busy_timeout_ms=busy_timeout_ms)
                )

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=constructor_count
        ) as executor:
            futures = [
                executor.submit(SynchronizedStore, database)
                for _ in range(constructor_count)
            ]
            errors = [future.exception() for future in futures]

        self.assertTrue(journal_lock_injected)
        self.assertEqual([error for error in errors if error is not None], [])

    def test_create_job_is_idempotent_only_for_the_same_repo_key_hash_and_payload(self):
        store = self.make_store()
        payload = {"role": "review", "budget": {"turns": 2, "seconds": 30}}
        job = store.create_job(
            repo_id="repo",
            idempotency_key="key",
            request_hash="request-a",
            payload=payload,
        )
        reused = store.create_job(
            repo_id="repo",
            idempotency_key="key",
            request_hash="request-a",
            payload={"budget": {"seconds": 30, "turns": 2}, "role": "review"},
        )

        self.assertEqual(reused.id, job.id)
        self.assertEqual(len(store.list_attempts(job.id)), 1)

        conflict_cases = (
            ("request-b", payload),
            ("request-a", {"role": "build"}),
        )
        for request_hash, conflicting_payload in conflict_cases:
            with self.subTest(request_hash=request_hash, payload=conflicting_payload):
                with self.assertRaises(IdempotencyConflict):
                    store.create_job(
                        repo_id="repo",
                        idempotency_key="key",
                        request_hash=request_hash,
                        payload=conflicting_payload,
                    )

        other_repo = store.create_job(
            repo_id="other-repo",
            idempotency_key="key",
            request_hash="request-a",
            payload=payload,
        )
        self.assertNotEqual(other_repo.id, job.id)

    def test_transitions_use_the_exact_allowlist_and_compare_and_swap_revision(self):
        self.assertEqual(TRANSITIONS, EXPECTED_TRANSITIONS)
        store = self.make_store()
        job = store.create_job(
            repo_id="repo",
            idempotency_key="transition",
            request_hash="hash",
            payload={},
        )

        revision = store.transition(
            job.id,
            expected_revision=0,
            new_status="PREFLIGHT",
        )
        self.assertEqual(revision, 1)
        self.assertEqual(store.get_job(job.id).status, "PREFLIGHT")

        with self.assertRaises(RevisionConflict):
            store.transition(
                job.id,
                expected_revision=0,
                new_status="RUNNING",
            )
        with self.assertRaises(InvalidTransition):
            store.transition(
                job.id,
                expected_revision=1,
                new_status="SUCCEEDED",
            )
        unchanged = store.get_job(job.id)
        self.assertEqual((unchanged.status, unchanged.revision), ("PREFLIGHT", 1))

    def test_begin_immediate_serializes_concurrent_compare_and_swap_updates(self):
        store = TracedStore(self.temp_path / "traced" / "state.sqlite3")
        store.statements.clear()
        job = store.create_job(
            repo_id="repo",
            idempotency_key="concurrent",
            request_hash="hash",
            payload={},
        )
        self.assertIn(
            "BEGIN IMMEDIATE",
            {statement.strip().upper() for statement in store.statements},
        )

        def update():
            try:
                return store.transition(
                    job.id,
                    expected_revision=0,
                    new_status="PREFLIGHT",
                )
            except RevisionConflict as exc:
                return exc

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: update(), range(2)))

        self.assertEqual(results.count(1), 1)
        self.assertEqual(
            sum(isinstance(result, RevisionConflict) for result in results),
            1,
        )
        self.assertEqual(store.get_job(job.id).revision, 1)

    def test_terminal_attempt_is_immutable_and_resume_creates_a_child_attempt(self):
        store = self.make_store()
        job = store.create_job(
            repo_id="repo",
            idempotency_key="terminal",
            request_hash="hash",
            payload={},
        )
        original = store.get_attempt(job.current_attempt_id)
        revision = store.transition(
            job.id,
            expected_revision=0,
            new_status="FAILED",
        )

        with self.assertRaises(InvalidTransition):
            store.transition(
                job.id,
                expected_revision=revision,
                new_status="RUNNING",
            )

        child = store.create_new_attempt(job.id, expected_revision=revision)
        current = store.get_job(job.id)
        self.assertEqual(child.parent_attempt_id, original.id)
        self.assertEqual(child.number, 2)
        self.assertEqual(child.status, "CREATED")
        self.assertEqual(child.revision, revision + 1)
        self.assertEqual(store.get_attempt(original.id).status, "FAILED")
        self.assertEqual(current.current_attempt_id, child.id)
        self.assertEqual((current.status, current.revision), ("CREATED", revision + 1))
        self.assertEqual(
            store.transition(
                job.id,
                expected_revision=current.revision,
                new_status="PREFLIGHT",
            ),
            revision + 2,
        )

    def test_artifact_rejects_symlinked_parent_without_touching_external_directory(self):
        store = self.make_store()
        job = store.create_job(
            repo_id="repo",
            idempotency_key="artifact-boundary",
            request_hash="hash",
            payload={},
        )
        store.create_artifact(job.id, "seed.txt", b"seed")
        external = self.temp_path / "external"
        external.mkdir(mode=0o755)
        external.chmod(0o755)
        escape = store.runs_path / job.id / "escape"
        escape.symlink_to(external, target_is_directory=True)

        with self.assertRaises(ValueError):
            store.create_artifact(job.id, "escape/report.txt", b"escaped")

        self.assertFalse((external / "report.txt").exists())
        self.assertEqual(mode(external), 0o755)

    def test_events_have_monotonic_sequence_previous_hash_and_canonical_payload_hash(self):
        store = self.make_store()
        job = store.create_job(
            repo_id="repo",
            idempotency_key="events",
            request_hash="hash",
            payload={},
        )
        first_payload = {"z": [3, 2, 1], "a": {"unicode": "café"}}
        first_canonical = json.dumps(
            first_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        first_envelope = json.dumps(
            {
                "kind": "progress",
                "payload": first_payload,
                "previous_hash": None,
                "sequence": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        first = store.append_event(job.id, "progress", first_payload)
        second = store.append_event(job.id, "progress", {"step": 2})

        self.assertEqual(first.sequence, 1)
        self.assertIsNone(first.previous_hash)
        self.assertEqual(first.payload_json, first_canonical)
        self.assertEqual(
            first.event_hash,
            hashlib.sha256(first_envelope.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(second.sequence, 2)
        self.assertEqual(second.previous_hash, first.event_hash)
        second_envelope = json.dumps(
            {
                "kind": "progress",
                "payload": {"step": 2},
                "previous_hash": first.event_hash,
                "sequence": 2,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        self.assertEqual(
            second.event_hash,
            hashlib.sha256(second_envelope.encode("utf-8")).hexdigest(),
        )
        self.assertEqual([event.id for event in store.list_events(job.id)], [first.id, second.id])
        self.assertEqual(store.get_job(job.id).revision, 2)


if __name__ == "__main__":
    unittest.main()
