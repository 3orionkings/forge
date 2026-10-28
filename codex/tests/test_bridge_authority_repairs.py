"""RED-to-GREEN contracts for shared scheduling and same-session repair."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from forge_lib.contracts import (  # noqa: E402
    TOOL_SCHEMAS,
    ContractError,
    validate_tool_arguments,
)
from forge_lib.state import (  # noqa: E402
    IdempotencyConflict,
    RevisionConflict,
    SCHEMA_VERSION,
    StateError,
    Store,
    canonical_json,
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class AuthorityRepairContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True)
        self.state = self.root / "state" / "state.sqlite3"

    def native_inputs(self, lease_id, *, weight="LIGHT", writer=None, files=None):
        receipt = self.root / f"{lease_id}.json"
        token = digest(f"token:{lease_id}")
        receipt.write_text(
            json.dumps({"lease_id": lease_id, "owner_token": token}) + "\n",
            encoding="utf-8",
        )
        receipt.chmod(0o600)
        info = receipt.stat()
        return {
            "lease_id": lease_id,
            "owner_token": token,
            "receipt_pin": {
                "path": str(receipt),
                "device": info.st_dev,
                "inode": info.st_ino,
                "owner": info.st_uid,
                "mode": stat.S_IMODE(info.st_mode),
                "links": info.st_nlink,
                "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
            },
            "reservation": {
                "repo_id": "d" * 64,
                "base_sha": "e" * 40,
                "lane": "feature",
                "weight": weight,
                "writer": writer or f"/private/{lease_id}",
                "files": files or [f"src/{lease_id}.py"],
            },
            "admission": {"result": "ADMIT", "lease_id": lease_id},
            "ttl_seconds": 300,
        }

    def seed_historical_native_lane(self, store, inputs, *, lane="recovery"):
        reservation = {**inputs["reservation"], "lane": lane}
        admission_json = canonical_json(inputs["admission"])
        now = "2026-08-26T00:00:00.000000+00:00"
        with store.connection() as connection:
            connection.execute(
                """
                INSERT INTO lane_reservations(
                    lease_id, kind, job_id, attempt_id, repo_id, base_sha, lane,
                    weight, writer, files_json, revision, owner_token_hash,
                    receipt_pin_json, admission_json, admission_hash,
                    last_operation_hash, last_command_id, ttl_seconds,
                    heartbeat_at, stale_at, released_at, created_at, updated_at
                ) VALUES (
                    :lease_id, 'native', NULL, NULL, :repo_id, :base_sha, :lane,
                    :weight, :writer, :files_json, 0, :owner_token_hash,
                    :receipt_pin_json, :admission_json, :admission_hash,
                    NULL, NULL, :ttl_seconds, :heartbeat_at, :stale_at, NULL,
                    :created_at, :updated_at
                )
                """,
                {
                    "lease_id": inputs["lease_id"],
                    "repo_id": reservation["repo_id"],
                    "base_sha": reservation["base_sha"],
                    "lane": reservation["lane"],
                    "weight": reservation["weight"],
                    "writer": reservation["writer"],
                    "files_json": canonical_json(reservation["files"]),
                    "owner_token_hash": Store._native_token_hash(
                        inputs["owner_token"]
                    ),
                    "receipt_pin_json": canonical_json(inputs["receipt_pin"]),
                    "admission_json": admission_json,
                    "admission_hash": hashlib.sha256(
                        admission_json.encode("utf-8")
                    ).hexdigest(),
                    "ttl_seconds": inputs["ttl_seconds"],
                    "heartbeat_at": now,
                    "stale_at": "9999-12-31T23:59:59.999999+00:00",
                    "created_at": now,
                    "updated_at": now,
                },
            )
        return store.list_lane_reservations()[0]

    @staticmethod
    def bridge_payload(candidate, request_hash):
        evidence = {
            "reservation": {
                name: candidate[name]
                for name in ("repo_id", "lane", "weight", "writer", "files")
            },
            "review_controls": {},
            "scheduler_snapshot": {"repository": {"dirty_paths": []}},
        }
        evidence["sha256"] = hashlib.sha256(
            canonical_json(evidence).encode("utf-8")
        ).hexdigest()
        return {
            "prompt": "offline bridge race fixture",
            "baseline": {"head": candidate["base_sha"]},
            "claude_executable": "/nonexistent/offline-fixture",
            "admission_evidence": evidence,
            "request_hash": request_hash,
        }

    def race_native_and_bridge(self, *, native, bridge):
        store = Store(self.state)
        barrier = threading.Barrier(3)

        def native_call():
            barrier.wait()
            return store.reserve_native_lane(**native)

        def bridge_call():
            barrier.wait()

            def build(_job_id, _attempt_id, active):
                Store._assert_lane_available(bridge, active)
                return self.bridge_payload(bridge, "a" * 64)

            return store.create_admitted_job(
                repo_id=bridge["repo_id"],
                idempotency_key="bridge-race-" + digest(canonical_json(bridge)),
                request_hash="a" * 64,
                build_payload=build,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(native_call), executor.submit(bridge_call)]
            barrier.wait()
            outcomes = []
            for future in futures:
                try:
                    outcomes.append(("ok", future.result(timeout=10)))
                except StateError as error:
                    outcomes.append(("error", str(error)))
        return store, outcomes

    def test_schema_v12_has_one_authoritative_scheduler_and_attempt_contexts(self):
        store = Store(self.state)
        with store.connection() as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertEqual(SCHEMA_VERSION, 12)
        self.assertIn("attempt_contexts", tables)
        self.assertIn("lane_reservations", tables)

    def test_resume_contract_carries_bounded_prompt_idempotency_and_refreeze(self):
        validated = validate_tool_arguments(
            "forge_resume",
            {
                "job_id": "job",
                "revision": 7,
                "prompt": "Apply the bounded fix and report proof.",
                "idempotency_key": "fix-round-1",
                "refreeze": {
                    "baseline_head": "a" * 40,
                    "dirty_files": [
                        {"path": "src/unit.py", "sha256": "b" * 64}
                    ],
                },
            },
        )
        self.assertEqual(validated["prompt"], "Apply the bounded fix and report proof.")
        self.assertIn("refreeze", TOOL_SCHEMAS["forge_resume"]["properties"])

    def test_review_start_declares_exact_pinned_control_artifacts(self):
        admission = {
            "weight": "LIGHT",
            "proof_scope": "targeted",
            "execution_profile": "sprint",
            "proof_argv": ["python3", "-m", "unittest", "tests.test_skill_contracts"],
            "files": [],
            "review_controls": {
                "plan": {"path": "/repo/PLAN.md", "sha256": "a" * 64},
                "review_log": {
                    "path": "/repo/PLAN-REVIEW-LOG.md",
                    "sha256": "b" * 64,
                },
            },
        }
        validated = validate_tool_arguments(
            "forge_start",
            {"prompt": "Review", "admission": admission},
        )
        self.assertEqual(validated["admission"]["review_controls"], admission["review_controls"])

    def test_review_contract_bounds_optional_domain_docs(self):
        controls = {
            "plan": {"path": "/repo/PLAN.md", "sha256": "a" * 64},
            "review_log": {
                "path": "/repo/PLAN-REVIEW-LOG.md",
                "sha256": "b" * 64,
            },
            "domain_docs": [
                {"path": f"docs/context-{index:02d}.md", "sha256": "c" * 64}
                for index in range(64)
            ],
        }
        admission = {
            "weight": "LIGHT",
            "proof_scope": "targeted",
            "execution_profile": "sprint",
            "proof_argv": ["python3", "-m", "unittest", "tests.test_skill_contracts"],
            "files": [],
            "review_controls": controls,
        }
        validated = validate_tool_arguments(
            "forge_start",
            {"prompt": "Review", "admission": admission},
        )
        self.assertEqual(
            validated["admission"]["review_controls"]["domain_docs"],
            controls["domain_docs"],
        )
        over_bound = copy.deepcopy(admission)
        over_bound["review_controls"]["domain_docs"].append(
            {"path": "docs/context-64.md", "sha256": "d" * 64}
        )
        with self.assertRaisesRegex(ContractError, "too many items"):
            validate_tool_arguments(
                "forge_resume",
                {
                    "job_id": "job",
                    "revision": 1,
                    "refreeze": {
                        "baseline_head": "e" * 40,
                        "review_controls": over_bound["review_controls"],
                    },
                },
            )

    def test_steer_contract_carries_full_revision_checked_expansion(self):
        value = validate_tool_arguments(
            "forge_steer",
            {
                "job_id": "job",
                "revision": 9,
                "prompt": "Continue inside the expanded exact set.",
                "admission_expansion": {
                    "lease_id": "bridge-job",
                    "lease_revision": 2,
                    "prior_files": ["src/a.py"],
                    "files": ["src/a.py", "src/b.py"],
                },
            },
        )
        self.assertEqual(value["admission_expansion"]["lease_revision"], 2)

    def test_native_lane_api_uses_private_capability_and_never_lists_token(self):
        store = Store(self.state)
        receipt = self.root / "native-receipt.json"
        token = "c" * 64
        receipt.write_text(
            json.dumps({"lease_id": "native-one", "owner_token": token}) + "\n",
            encoding="utf-8",
        )
        receipt.chmod(0o600)
        info = receipt.stat()
        reservation, replay = store.reserve_native_lane(
            lease_id="native-one",
            owner_token=token,
            receipt_pin={
                "path": str(receipt),
                "device": info.st_dev,
                "inode": info.st_ino,
                "owner": info.st_uid,
                "mode": stat.S_IMODE(info.st_mode),
                "links": info.st_nlink,
                "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
            },
            reservation={
                "repo_id": "d" * 64,
                "base_sha": "e" * 40,
                "lane": "feature",
                "weight": "LIGHT",
                "writer": "/private/repo",
                "files": ["src/a.py"],
            },
            admission={"result": "ADMIT", "sha256": "f" * 64},
            ttl_seconds=300,
        )
        self.assertFalse(replay)
        listed = store.list_lane_reservations()
        self.assertEqual(listed[0]["lease_id"], reservation["lease_id"])
        self.assertNotIn(token, json.dumps(listed))
        with self.assertRaises(StateError):
            store.release_native_lane(
                "native-one", owner_token="0" * 64, expected_revision=reservation["revision"]
            )
        released = store.release_native_lane(
            "native-one", owner_token=token, expected_revision=reservation["revision"]
        )
        self.assertEqual(released["status"], "RELEASED")

    def test_store_rejects_native_recovery_reserve_without_mutation(self):
        store = Store(self.state)
        inputs = self.native_inputs("store-recovery-reserve")
        inputs["reservation"]["lane"] = "recovery"
        before = store.list_lane_reservations()
        with self.assertRaisesRegex(StateError, "native recovery lanes are unsupported"):
            store.reserve_native_lane(**inputs)
        self.assertEqual(store.list_lane_reservations(), before)

    def test_store_rejects_native_recovery_expansion_without_mutation(self):
        store = Store(self.state)
        inputs = self.native_inputs(
            "store-recovery-expand",
            files=["src/recovery.py"],
        )
        historical = self.seed_historical_native_lane(store, inputs)
        before = store.list_lane_reservations()
        with self.assertRaisesRegex(StateError, "native recovery lanes are unsupported"):
            store.expand_native_lane(
                historical["lease_id"],
                owner_token=inputs["owner_token"],
                expected_revision=historical["revision"],
                prior_files=["src/recovery.py"],
                files=["src/recovery.py", "src/recovery-proof.py"],
                admission={"result": "ADMIT", "action": "expand"},
            )
        self.assertEqual(store.list_lane_reservations(), before)
        heartbeat = store.heartbeat_native_lane(
            historical["lease_id"],
            owner_token=inputs["owner_token"],
            expected_revision=historical["revision"],
        )
        self.assertEqual(heartbeat["lane"], "recovery")
        released = store.release_native_lane(
            historical["lease_id"],
            owner_token=inputs["owner_token"],
            expected_revision=heartbeat["revision"],
        )
        self.assertEqual(released["status"], "RELEASED")

    def test_native_lane_script_exists_and_is_bounded_local_control(self):
        script = ROOT / "scripts" / "forge-native-lane.py"
        self.assertTrue(script.is_file(), script)
        completed = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd=ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=5,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("reserve", completed.stdout)
        self.assertIn("expand", completed.stdout)
        self.assertIn("heartbeat", completed.stdout)
        self.assertIn("release", completed.stdout)
        self.assertIn("list", completed.stdout)

    def test_native_recovery_documents_fail_closed_handoff(self):
        admission = (
            ROOT / "skills" / "forge" / "references" / "admission.md"
        ).read_text(encoding="utf-8")
        normalized = " ".join(admission.split())
        for phrase in (
            "Native recovery lanes are unsupported",
            "no atomic filesystem snapshot",
            "automated bridge recovery is also disabled",
            "owner-approved manual handoff",
            "freshly admitted clean normal feature worktree",
        ):
            self.assertIn(phrase, normalized)

    def test_native_receipt_survives_restart_for_cli_heartbeat_and_release(self):
        script = ROOT / "scripts" / "forge-native-lane.py"
        spec = importlib.util.spec_from_file_location("forge_native_lane_test", script)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        receipt = self.root / "native-owner.json"
        owner_token, receipt_pin, created = module._create_or_load_receipt(
            receipt,
            lease_id="native-cli",
            state_path=self.state,
        )
        self.assertTrue(created)
        self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
        stored, _ = Store(self.state).reserve_native_lane(
            lease_id="native-cli",
            owner_token=owner_token,
            receipt_pin=receipt_pin,
            reservation={
                "repo_id": "d" * 64,
                "base_sha": "e" * 40,
                "lane": "feature",
                "weight": "LIGHT",
                "writer": "/private/native-cli",
                "files": ["src/native-cli.py"],
            },
            admission={"result": "ADMIT", "kind": "native"},
            ttl_seconds=300,
        )

        def invoke(*arguments):
            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--state",
                    str(self.state),
                    *arguments,
                ],
                cwd=ROOT,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertNotIn(owner_token, completed.stdout + completed.stderr)
            return json.loads(completed.stdout)

        listed = invoke("list")
        self.assertEqual(listed[0]["lease_id"], "native-cli")
        heartbeat = invoke(
            "heartbeat",
            "--lease-id",
            "native-cli",
            "--receipt",
            str(receipt),
            "--revision",
            str(stored["revision"]),
        )
        self.assertEqual(heartbeat["revision"], stored["revision"] + 1)
        released = invoke(
            "release",
            "--lease-id",
            "native-cli",
            "--receipt",
            str(receipt),
            "--revision",
            str(heartbeat["revision"]),
        )
        self.assertEqual(released["status"], "RELEASED")

    def test_native_cli_reserve_and_same_lane_expand_use_guarded_receipt(self):
        script = ROOT / "scripts" / "forge-native-lane.py"
        spec = importlib.util.spec_from_file_location("forge_native_lane_flow", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        writer = self.root / "writer"
        writer.mkdir()
        subprocess.run(["git", "init", "-q", str(writer)], check=True)
        subprocess.run(
            ["git", "-C", str(writer), "config", "user.email", "forge@example.test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(writer), "config", "user.name", "Forge Test"],
            check=True,
        )
        (writer / "src").mkdir()
        (writer / "src" / "a.py").write_text("a = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(writer), "add", "."], check=True)
        subprocess.run(["git", "-C", str(writer), "commit", "-qm", "baseline"], check=True)
        head = subprocess.run(
            ["git", "-C", str(writer), "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        repo_id = module._repo_id(module.validate_boundary(writer, writer))
        fake_guard = self.root / "fake-dispatch-guard.sh"
        fake_guard.write_text(
            "#!/bin/bash\n"
            "exec /usr/bin/python3 - \"$@\" <<'PY'\n"
            "import json, sys\n"
            "args=sys.argv[1:]; values={}; files=[]\n"
            "for index in range(0,len(args),2):\n"
            " flag,value=args[index:index+2]\n"
            " if flag=='--file': files.append(value)\n"
            " else: values[flag]=value\n"
            f"binding={{'repo_id':'{repo_id}','base_sha':values['--base-sha'],"
            "'lane':values['--lane-type'].lower(),'weight':values['--weight'].upper(),"
            "'writer':values['--worktree'],'files':files}\n"
            "print('ADMIT:'+json.dumps({'decision':'ADMIT','scheduler_binding':binding},"
            "sort_keys=True,separators=(',',':')))\n"
            "PY\n",
            encoding="utf-8",
        )
        fake_guard.chmod(0o700)
        module.GUARD = fake_guard
        guard_request = self.root / "guard-request.json"

        def write_request(files):
            request = [
                "--verdict", "APPROVED",
                "--worktree", str(writer),
                "--base-sha", head,
                "--lane-type", "feature",
                "--weight", "LIGHT",
            ]
            for path in files:
                request.extend(("--file", path))
            guard_request.write_text(json.dumps(request) + "\n", encoding="utf-8")
            guard_request.chmod(0o600)

        write_request(["src/a.py"])
        receipt = self.root / "native-flow-owner.json"

        def invoke(arguments):
            output = io.StringIO()
            with (
                mock.patch.object(sys, "stdout", output),
                mock.patch.object(module, "_machine_snapshot", return_value={
                    "core_count": 10,
                    "load_1": 1.0,
                    "free_swap_gb": 16.0,
                    "cpu_idle_pct": 80.0,
                    "memory_free_pct": 80.0,
                }),
                mock.patch.object(module, "_measured_wip", return_value=0),
            ):
                status = module.main(["--state", str(self.state), *arguments])
            self.assertEqual(status, 0)
            return json.loads(output.getvalue())

        reserved = invoke(
            [
                "reserve",
                "--lease-id",
                "native-flow",
                "--receipt",
                str(receipt),
                "--guard-request",
                str(guard_request),
                "--repo-id",
                repo_id,
                "--base-sha",
                head,
                "--lane",
                "feature",
                "--weight",
                "LIGHT",
                "--writer",
                str(writer),
                "--file",
                "src/a.py",
            ]
        )
        self.assertFalse(reserved["idempotent"])
        write_request(["src/a.py", "src/b.py"])
        expanded = invoke(
            [
                "expand",
                "--lease-id",
                "native-flow",
                "--receipt",
                str(receipt),
                "--guard-request",
                str(guard_request),
                "--revision",
                str(reserved["reservation"]["revision"]),
                "--prior-file",
                "src/a.py",
                "--file",
                "src/a.py",
                "--file",
                "src/b.py",
            ]
        )
        self.assertFalse(expanded["idempotent"])
        self.assertEqual(expanded["reservation"]["files"], ["src/a.py", "src/b.py"])
        self.assertEqual(
            expanded["reservation"]["revision"],
            reserved["reservation"]["revision"] + 1,
        )

    def test_cross_kind_atomic_lane_cap_cannot_race_past_three(self):
        store = Store(self.state)
        for index in range(2):
            store.reserve_native_lane(**self.native_inputs(f"seed-{index}"))
        native = self.native_inputs("cap-native")
        bridge = {
            "repo_id": "d" * 64,
            "base_sha": "e" * 40,
            "lane": "feature",
            "weight": "LIGHT",
            "writer": "/private/cap-bridge",
            "files": ["src/cap-bridge.py"],
        }
        store, outcomes = self.race_native_and_bridge(native=native, bridge=bridge)
        self.assertEqual([kind for kind, _ in outcomes].count("ok"), 1, outcomes)
        self.assertEqual(len(store.list_lane_reservations()), 3)
        self.assertIn("LANE_CAP", " ".join(str(value) for kind, value in outcomes if kind == "error"))

    def test_cross_kind_atomic_heavy_lease_has_one_winner(self):
        native = self.native_inputs("heavy-native", weight="HEAVY")
        bridge = {
            "repo_id": "d" * 64,
            "base_sha": "e" * 40,
            "lane": "feature",
            "weight": "HEAVY",
            "writer": "/private/heavy-bridge",
            "files": ["src/heavy-bridge.py"],
        }
        store, outcomes = self.race_native_and_bridge(native=native, bridge=bridge)
        self.assertEqual([kind for kind, _ in outcomes].count("ok"), 1, outcomes)
        active = store.list_lane_reservations()
        self.assertEqual(sum(item["weight"] == "HEAVY" for item in active), 1)
        self.assertIn("HEAVY_LEASE", " ".join(str(value) for kind, value in outcomes if kind == "error"))

    def test_cross_kind_atomic_file_overlap_has_one_winner(self):
        native = self.native_inputs("overlap-native", files=["src/shared.py"])
        bridge = {
            "repo_id": "d" * 64,
            "base_sha": "e" * 40,
            "lane": "feature",
            "weight": "LIGHT",
            "writer": "/private/overlap-bridge",
            "files": ["src/shared.py"],
        }
        store, outcomes = self.race_native_and_bridge(native=native, bridge=bridge)
        self.assertEqual([kind for kind, _ in outcomes].count("ok"), 1, outcomes)
        active = store.list_lane_reservations()
        self.assertEqual(sum("src/shared.py" in item["files"] for item in active), 1)
        self.assertIn("FILE_OVERLAP", " ".join(str(value) for kind, value in outcomes if kind == "error"))

    def test_stale_native_lane_still_blocks_until_authenticated_release(self):
        store = Store(self.state)
        owner, _ = store.reserve_native_lane(**self.native_inputs("stale-owner"))
        with store.connection() as connection:
            connection.execute(
                "UPDATE lane_reservations SET stale_at = ? WHERE lease_id = ?",
                ("2000-01-01T00:00:00+00:00", "stale-owner"),
            )
        contender = self.native_inputs(
            "stale-contender",
            files=["src/stale-owner.py"],
        )
        with self.assertRaisesRegex(StateError, "FILE_OVERLAP"):
            store.reserve_native_lane(**contender)
        with self.assertRaises(StateError):
            store.release_native_lane(
                "stale-owner",
                owner_token="0" * 64,
                expected_revision=owner["revision"],
            )
        token = digest("token:stale-owner")
        released = store.release_native_lane(
            "stale-owner",
            owner_token=token,
            expected_revision=owner["revision"],
        )
        self.assertEqual(released["status"], "RELEASED")

    def test_native_expansion_preserves_heavy_lease_and_exact_replay_identity(self):
        store = Store(self.state)
        launch = self.native_inputs(
            "native-expand",
            weight="HEAVY",
            files=["src/a.py"],
        )
        launch["operation_context_sha256"] = digest("native-launch-guard-request")
        original, _ = store.reserve_native_lane(**launch)
        with self.assertRaises(IdempotencyConflict):
            store.reserve_native_lane(
                **{
                    **launch,
                    "operation_context_sha256": digest("changed-launch-guard-request"),
                }
            )

        def admit(active):
            self.assertEqual(active, [])
            return {
                "result": "ADMIT",
                "action": "expand",
                "prior_files": ["src/a.py"],
                "files": ["src/a.py", "src/b.py"],
            }

        expanded, replay = store.expand_native_lane(
            original["lease_id"],
            owner_token=digest("token:native-expand"),
            expected_revision=original["revision"],
            prior_files=["src/a.py"],
            files=["src/a.py", "src/b.py"],
            operation_context_sha256=digest("native-expand-guard-request"),
            build_admission=admit,
        )
        self.assertFalse(replay)
        for name in ("lease_id", "kind", "repo_id", "base_sha", "lane", "weight", "writer"):
            self.assertEqual(expanded[name], original[name], name)
        self.assertEqual(expanded["weight"], "HEAVY")
        self.assertEqual(expanded["revision"], original["revision"] + 1)
        replayed, replay = store.expand_native_lane(
            original["lease_id"],
            owner_token=digest("token:native-expand"),
            expected_revision=original["revision"],
            prior_files=["src/a.py"],
            files=["src/a.py", "src/b.py"],
            operation_context_sha256=digest("native-expand-guard-request"),
            build_admission=lambda _active: self.fail("exact replay reran admission"),
        )
        self.assertTrue(replay)
        self.assertEqual(replayed, expanded)
        with self.assertRaises(RevisionConflict):
            store.expand_native_lane(
                original["lease_id"],
                owner_token=digest("token:native-expand"),
                expected_revision=original["revision"],
                prior_files=["src/a.py"],
                files=["src/a.py", "src/b.py"],
                operation_context_sha256=digest("changed-expand-guard-request"),
                build_admission=lambda _active: self.fail(
                    "changed request digest cannot replay admission"
                ),
            )
        with self.assertRaises(RevisionConflict):
            store.expand_native_lane(
                original["lease_id"],
                owner_token=digest("token:native-expand"),
                expected_revision=original["revision"],
                prior_files=["src/b.py"],
                files=["src/a.py", "src/b.py"],
                build_admission=lambda _active: {"result": "ADMIT"},
            )
        with self.assertRaises(RevisionConflict):
            store.expand_native_lane(
                original["lease_id"],
                owner_token=digest("token:native-expand"),
                expected_revision=original["revision"],
                prior_files=["src/a.py"],
                files=["src/a.py", "src/c.py"],
                build_admission=lambda _active: {"result": "ADMIT"},
            )

    def test_concurrent_divergent_native_expansions_publish_one_complete_set(self):
        store = Store(self.state)
        original, _ = store.reserve_native_lane(
            **self.native_inputs("native-expand-race", files=["src/a.py"])
        )
        barrier = threading.Barrier(3)

        def expand(extra):
            barrier.wait()
            return store.expand_native_lane(
                original["lease_id"],
                owner_token=digest("token:native-expand-race"),
                expected_revision=original["revision"],
                prior_files=["src/a.py"],
                files=["src/a.py", extra],
                build_admission=lambda _active: {
                    "result": "ADMIT",
                    "action": "expand",
                    "files": ["src/a.py", extra],
                },
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(expand, "src/b.py"),
                executor.submit(expand, "src/c.py"),
            ]
            barrier.wait()
            outcomes = []
            for future in futures:
                try:
                    outcomes.append(("ok", future.result(timeout=10)))
                except RevisionConflict as error:
                    outcomes.append(("error", error))
        self.assertEqual([kind for kind, _ in outcomes].count("ok"), 1, outcomes)
        self.assertEqual([kind for kind, _ in outcomes].count("error"), 1, outcomes)
        final = next(
            item
            for item in store.list_lane_reservations()
            if item["lease_id"] == original["lease_id"]
        )
        self.assertIn(final["files"], (["src/a.py", "src/b.py"], ["src/a.py", "src/c.py"]))
        self.assertEqual(final["revision"], original["revision"] + 1)
        self.assertEqual(final["weight"], original["weight"])

    def test_worker_applies_expansion_boundary_before_sending_prompt_only(self):
        from forge_lib import worker as worker_module
        from forge_lib.claude import encode_user_message

        expansion = {
            "lease_id": "bridge-job",
            "prior_lease_revision": 0,
            "lease_revision": 1,
            "prior_files": ["src/a.py"],
            "files": ["src/a.py", "src/b.py"],
            "admission_sha256": "a" * 64,
            "operation_sha256": "b" * 64,
        }
        command = SimpleNamespace(
            id="command-one",
            kind="STEER",
            attempt_id="attempt-one",
            payload={"prompt": "Continue inside the expanded set.", "admission_expansion": expansion},
        )
        delivery_order = []

        class FakeStore:
            @staticmethod
            def claim_next_command(*_args, **_kwargs):
                return command

            @staticmethod
            def mark_command_delivery_failed(*_args, **_kwargs):
                delivery_order.append("failed")

            @staticmethod
            def mark_command_delivered(*_args, **_kwargs):
                delivery_order.append("delivered")

        def prepare(claimed):
            self.assertIs(claimed, command)
            delivery_order.append("boundary")

        expected_frame = encode_user_message(command.payload["prompt"])

        def write_frame(_provider, frame, *, deadline):
            self.assertEqual(deadline, 10.0)
            self.assertEqual(delivery_order, ["boundary"])
            self.assertEqual(frame, expected_frame)
            self.assertNotIn(b"admission_expansion", frame)
            delivery_order.append("frame")
            return 5.0

        with mock.patch.object(worker_module, "_write_provider_frame", side_effect=write_frame):
            delivered = worker_module._deliver_queued_commands(
                object(),
                store=FakeStore(),
                job_id="job-one",
                dispatch_token="c" * 64,
                worker_pid=123,
                deadline=10.0,
                prepare_steer=prepare,
            )
        self.assertIs(delivered.command, command)
        self.assertEqual(delivery_order, ["boundary", "frame", "delivered"])

    def test_worker_expansion_boundary_is_bound_to_authoritative_receipt(self):
        from forge_lib import worker as worker_module

        source = self.root / "src"
        source.mkdir()
        (source / "a.py").write_text("a = 1\n", encoding="utf-8")
        (source / "b.py").write_text("b = 2\n", encoding="utf-8")
        fact_a = worker_module._output_absolute_path_fact(
            source / "a.py",
            deadline=__import__("time").monotonic() + 1,
        )
        fact_b = worker_module._output_absolute_path_fact(
            source / "b.py",
            deadline=__import__("time").monotonic() + 1,
        )
        snapshot = worker_module._OutputBoundarySnapshot(
            root=self.root,
            declared_files=("src/a.py",),
            workspace_facts={"src/a.py": fact_a, "src/b.py": fact_b},
            declared_facts={"src/a.py": fact_a},
            control_pins={},
            control_facts={},
            git_executable="/usr/bin/git",
            git_marker=self.root / ".git",
            git_dir=self.root / ".git",
            common_dir=self.root / ".git",
            git_metadata_facts={},
            git_semantic_facts={},
            git_index_fact=("missing",),
        )

        class Tracker:
            declared_files = ("src/a.py",)

            def expand_declared_files(inner_self, *, prior_files, files, new_facts):
                self.assertEqual(prior_files, ("src/a.py",))
                self.assertEqual(files, ("src/a.py", "src/b.py"))
                self.assertEqual(set(new_facts), {"src/b.py"})
                inner_self.declared_files = files

        expansion = {
            "lease_id": "bridge-job",
            "prior_lease_revision": 0,
            "lease_revision": 1,
            "prior_files": ["src/a.py"],
            "files": ["src/a.py", "src/b.py"],
            "admission_sha256": "a" * 64,
            "operation_sha256": "b" * 64,
        }
        reservation = {
            "lease_id": "bridge-job",
            "kind": "bridge",
            "status": "ACTIVE",
            "revision": 1,
            "files": ["src/a.py", "src/b.py"],
            "admission_sha256": "a" * 64,
        }
        tracker = Tracker()
        worker_module._apply_admission_expansion_to_output_boundary(
            snapshot=snapshot,
            objective_tracker=tracker,
            expansion=expansion,
            reservation=reservation,
        )
        self.assertEqual(snapshot.declared_files, ("src/a.py", "src/b.py"))
        self.assertIn("src/b.py", snapshot.declared_facts)

        refused_snapshot = worker_module._OutputBoundarySnapshot(
            **{**snapshot.__dict__, "declared_files": ("src/a.py",)}
        )
        refused_tracker = Tracker()
        with self.assertRaises(worker_module.WorkerError):
            worker_module._apply_admission_expansion_to_output_boundary(
                snapshot=refused_snapshot,
                objective_tracker=refused_tracker,
                expansion=expansion,
                reservation={**reservation, "admission_sha256": "c" * 64},
            )
        self.assertEqual(refused_snapshot.declared_files, ("src/a.py",))

        laundering_snapshot = worker_module._OutputBoundarySnapshot(
            **{
                **snapshot.__dict__,
                "declared_files": ("src/a.py",),
                "workspace_facts": {
                    **snapshot.workspace_facts,
                    "src/b.py": ("missing",),
                },
            }
        )
        laundering_tracker = Tracker()
        with self.assertRaisesRegex(worker_module.WorkerError, "changed before expansion"):
            worker_module._apply_admission_expansion_to_output_boundary(
                snapshot=laundering_snapshot,
                objective_tracker=laundering_tracker,
                expansion=expansion,
                reservation=reservation,
            )
        self.assertEqual(laundering_snapshot.declared_files, ("src/a.py",))


if __name__ == "__main__":
    unittest.main()
