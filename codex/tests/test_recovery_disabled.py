"""Focused fail-closed tests for the shipped recovery policy."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from forge_lib.service import ForgeService
from forge_lib.protocol import _dispatch
from forge_lib.state import Store
from forge_lib.worker import WorkerError, run_worker
from tests.admission_fixture import machine_headroom, seed_reconciled_base


RECOVERY_DISABLED_MESSAGE = "Automated Forge recovery is disabled in this build"


class RecoveryDisabledTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["/usr/bin/git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(
            [
                "/usr/bin/git",
                "-C",
                str(self.repo),
                "config",
                "user.email",
                "forge@example.test",
            ],
            check=True,
        )
        subprocess.run(
            [
                "/usr/bin/git",
                "-C",
                str(self.repo),
                "config",
                "user.name",
                "Forge Test",
            ],
            check=True,
        )
        (self.repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
        subprocess.run(
            ["/usr/bin/git", "-C", str(self.repo), "add", "tracked.txt"],
            check=True,
        )
        subprocess.run(
            ["/usr/bin/git", "-C", str(self.repo), "commit", "-qm", "baseline"],
            check=True,
        )
        seed_reconciled_base(self.repo)

    def service(self, label: str) -> ForgeService:
        return ForgeService(
            state_path=self.root / label / "state.sqlite3",
            launch_workspace=self.repo,
            claude_executable=str(self.root / "provider-must-not-run"),
            bridge_path=ROOT / "scripts" / "forge_bridge.py",
            environment={
                "HOME": str(self.root),
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            },
            _admission_machine_probe=machine_headroom,
        )

    @staticmethod
    def durable_snapshot(store: Store) -> tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]:
        tables = (
            "jobs",
            "attempts",
            "attempt_contexts",
            "attempt_admissions",
            "attempt_evidence_locks",
            "commands",
            "events",
            "lane_reservations",
        )
        with store.connection() as connection:
            return tuple(
                (
                    table,
                    tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table}")),
                )
                for table in tables
            )

    @staticmethod
    def filesystem_snapshot(root: Path) -> tuple[tuple[str, int, bytes | None], ...]:
        snapshot = []
        for path in sorted(root.rglob("*")):
            relative = str(path.relative_to(root))
            mode = path.lstat().st_mode
            snapshot.append(
                (
                    relative,
                    mode,
                    path.read_bytes() if path.is_file() else None,
                )
            )
        return tuple(snapshot)

    def legacy_recovery_job(
        self,
        service: ForgeService,
        label: str,
        *,
        role: str = "recovery",
    ):
        payload = {
            "prompt": "Historical recovery attempt.",
            "cwd": str(self.repo),
            "role": role,
            "model": "fable",
            "effort": "max",
            "budget": {
                "max_seconds": 60,
                "max_tokens": 4096,
                "max_turns": 4,
                "max_cost_usd": "1",
            },
            "baseline": {},
            "claude_executable": str(self.root / "provider-must-not-run"),
        }
        return service.store.create_job(
            repo_id=service._launch_repo_id,
            idempotency_key=label,
            request_hash="a" * 64,
            payload=payload,
        )

    def assert_disabled(self, result) -> None:
        self.assertTrue(result.is_error, result)
        self.assertEqual(result.body["status"], "rejected")
        self.assertIn(RECOVERY_DISABLED_MESSAGE, result.message)

    def test_new_recovery_start_refuses_before_state_or_worker_effect(self) -> None:
        service = self.service("new-start")
        before = self.durable_snapshot(service.store)
        with (
            mock.patch.object(
                service.store,
                "create_admitted_job",
                side_effect=AssertionError("recovery start touched durable state"),
            ) as create_job,
            mock.patch(
                "forge_lib.service.spawn_detached_worker",
                side_effect=AssertionError("recovery start launched a worker"),
            ) as spawn_worker,
        ):
            result = service.execute(
                "forge_start",
                {
                    "prompt": "Recover the prior attempt.",
                    "cwd": str(self.repo),
                    "role": "recovery",
                    "admission": {},
                },
            )
        self.assert_disabled(result)
        create_job.assert_not_called()
        spawn_worker.assert_not_called()
        self.assertEqual(self.durable_snapshot(service.store), before)

    def test_protocol_recovery_start_refuses_before_store_initialization(self) -> None:
        state_path = self.root / "protocol-state" / "state.sqlite3"
        home = self.root / "protocol-home"
        home.mkdir()
        environment = {
            "HOME": str(home),
            "PATH": "/usr/bin:/bin",
            "FORGE_STATE_PATH": str(state_path),
            "FORGE_LAUNCH_WORKSPACE": str(self.repo),
            "FORGE_CLAUDE_EXECUTABLE": "/usr/bin/false",
        }
        request = {
            "jsonrpc": "2.0",
            "id": "recovery-before-store",
            "method": "tools/call",
            "params": {
                "name": "forge_start",
                "arguments": {
                    "prompt": "Do not run recovery.",
                    "role": "recovery",
                    "admission": {
                        "weight": "LIGHT",
                        "proof_scope": "targeted",
                        "execution_profile": "sprint",
                        "proof_argv": ["python3", "-m", "unittest"],
                        "files": ["tracked.txt"],
                    },
                },
            },
        }

        with mock.patch.dict(os.environ, environment, clear=True):
            response = _dispatch(request, request["id"])

        self.assertTrue(response["result"]["isError"], response)
        self.assertFalse(state_path.exists())
        self.assertFalse(state_path.parent.exists())

    def test_legacy_recovery_dispatch_refuses_before_claim_or_worker_effect(self) -> None:
        service = self.service("legacy-dispatch")
        job = self.legacy_recovery_job(service, "legacy-dispatch")
        before = self.durable_snapshot(service.store)
        with (
            mock.patch.object(
                service.store,
                "claim_dispatch",
                side_effect=AssertionError("recovery dispatch mutated its claim"),
            ) as claim_dispatch,
            mock.patch(
                "forge_lib.service.spawn_detached_worker",
                side_effect=AssertionError("recovery dispatch launched a worker"),
            ) as spawn_worker,
        ):
            with self.assertRaisesRegex(Exception, RECOVERY_DISABLED_MESSAGE):
                service._dispatch_job(job)
        claim_dispatch.assert_not_called()
        spawn_worker.assert_not_called()
        self.assertEqual(self.durable_snapshot(service.store), before)

    def test_mutating_controls_refuse_legacy_recovery_without_state_effect(self) -> None:
        calls = {
            "forge_steer": lambda job: {
                "job_id": job.id,
                "revision": job.revision,
                "prompt": "Continue recovery.",
            },
            "forge_cancel": lambda job: {
                "job_id": job.id,
                "revision": job.revision,
            },
            "forge_resume": lambda job: {
                "job_id": job.id,
                "revision": job.revision,
            },
        }
        for name, arguments_for in calls.items():
            with self.subTest(tool=name):
                service = self.service(name)
                job = self.legacy_recovery_job(service, name)
                before = self.durable_snapshot(service.store)
                with mock.patch.object(
                    service,
                    "_dispatch_job",
                    side_effect=AssertionError("recovery control launched a worker"),
                ) as dispatch:
                    result = service.execute(name, arguments_for(job))
                self.assert_disabled(result)
                dispatch.assert_not_called()
                self.assertEqual(self.durable_snapshot(service.store), before)

    def test_exact_job_observation_of_legacy_recovery_remains_read_only(self) -> None:
        service = self.service("legacy-read")
        job = self.legacy_recovery_job(service, "legacy-read")
        before = self.durable_snapshot(service.store)
        status = service.execute("forge_status", {"job_id": job.id})
        listed = service.execute("forge_list", {})
        self.assertFalse(status.is_error, status)
        self.assertFalse(listed.is_error, listed)
        self.assertEqual(status.body["job_id"], job.id)
        self.assertEqual(listed.body["jobs"][0]["role"], "recovery")
        self.assertEqual(self.durable_snapshot(service.store), before)

    def test_worker_refuses_recovery_before_authority_state_or_provider_effect(self) -> None:
        service = self.service("worker")
        job = self.legacy_recovery_job(service, "worker")
        claim = service.store.claim_dispatch(job.id, owner_pid=os.getpid())
        self.assertIsNotNone(claim)
        service.store.record_worker_pid(
            job.id,
            os.getpid(),
            _dispatch_claim=claim,
        )
        before = self.durable_snapshot(service.store)
        before_files = self.filesystem_snapshot(service.store.root_path)
        with (
            mock.patch(
                "forge_lib.worker.Store",
                side_effect=AssertionError("recovery worker initialized mutable state"),
            ) as store_initialization,
            mock.patch.object(
                Store,
                "claim_worker_authority",
                side_effect=AssertionError("recovery worker claimed authority"),
            ) as claim_authority,
            mock.patch(
                "forge_lib.worker.preflight",
                side_effect=AssertionError("recovery worker called provider preflight"),
            ) as provider_preflight,
            mock.patch(
                "forge_lib.worker.spawn_provider",
                side_effect=AssertionError("recovery worker launched provider"),
            ) as spawn_provider,
        ):
            with self.assertRaisesRegex(WorkerError, RECOVERY_DISABLED_MESSAGE):
                run_worker(
                    job.id,
                    dispatch_token=claim.token,
                    state_path=service.store.path,
                    dispatch_role="recovery",
                )
        claim_authority.assert_not_called()
        store_initialization.assert_not_called()
        provider_preflight.assert_not_called()
        spawn_provider.assert_not_called()
        self.assertEqual(self.durable_snapshot(service.store), before)
        self.assertEqual(
            self.filesystem_snapshot(service.store.root_path),
            before_files,
        )

    def test_trusted_build_role_remains_visible_with_a_live_wal_reader(self) -> None:
        service = self.service("wal-build")
        reader = sqlite3.connect(str(service.store.path), isolation_level=None)
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM jobs").fetchone()

        job = self.legacy_recovery_job(service, "wal-build", role="build")
        claim = service.store.claim_dispatch(job.id, owner_pid=os.getpid())
        self.assertIsNotNone(claim)
        assert claim is not None
        service.store.record_worker_pid(
            job.id,
            os.getpid(),
            _dispatch_claim=claim,
        )
        self.assertTrue(service.store.path.with_name("state.sqlite3-wal").exists())

        with (
            mock.patch(
                "forge_lib.worker.preflight",
                side_effect=AssertionError("incomplete fixture reached provider preflight"),
            ) as provider_preflight,
            mock.patch(
                "forge_lib.worker.spawn_provider",
                side_effect=AssertionError("incomplete fixture launched provider"),
            ) as provider_spawn,
            mock.patch("forge_lib.worker._fail_job") as fail_job,
        ):
            result = run_worker(
                job.id,
                dispatch_token=claim.token,
                state_path=service.store.path,
                dispatch_role="build",
            )

        self.assertEqual(result, 1)
        provider_preflight.assert_not_called()
        provider_spawn.assert_not_called()
        fail_job.assert_called_once()

    def test_active_bridge_docs_describe_refusal_and_manual_handoff(self) -> None:
        bridge_tools = (ROOT / "skills/forge/references/bridge-tools.md").read_text(
            encoding="utf-8"
        )
        admission = (ROOT / "skills/forge/references/admission.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Automated recovery is disabled", bridge_tools)
        self.assertIn("manual handoff", bridge_tools)
        self.assertNotIn("start one detached review, build, or recovery job", bridge_tools)
        self.assertIn("Automated recovery is disabled", admission)
        self.assertIn("manual handoff", admission)
        self.assertNotIn("Product roles are build and recovery", admission)


if __name__ == "__main__":
    unittest.main()
