import concurrent.futures
import hashlib
import json
import os
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.admission_fixture import (
    machine_headroom,
    review_admission,
    seed_reconciled_base,
)
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "tests" / "fixtures" / "forge_bridge_admission.py"
FAKE_CLAUDE = ROOT / "tests" / "fixtures" / "fake_claude.py"
FAKE_CONTROL_NAME = "forge-fake-claude-control.json"


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _process_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _bound_worker_launch(*_args, **kwargs):
    """Test double for the parent side of the detached-worker launch gate."""

    worker_pid = os.getpid()
    kwargs["bind_before_release"](worker_pid)
    return worker_pid


class BridgeHarness:
    def __init__(self, environment):
        self.process = subprocess.Popen(
            [sys.executable, "-u", str(BRIDGE)],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            start_new_session=True,
        )
        self._next_id = 1

    def tool(self, name, arguments, timeout=5):
        request_id = self._next_id
        self._next_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
        self.process.stdin.flush()
        assert self.process.stdout is not None
        ready, _, _ = select.select([self.process.stdout], [], [], timeout)
        if not ready:
            raise TimeoutError(f"no bridge response for {name}")
        line = self.process.stdout.readline()
        if not line:
            stderr = self.process.stderr.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"bridge exited during {name}: {stderr}")
        response = json.loads(line)
        if "error" in response:
            raise AssertionError(response)
        return response["result"]

    def terminate_bridge(self):
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=2)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()


class BridgeJobTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temp_path = Path(temporary.name).resolve(strict=True)
        self.repo = self.temp_path / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.email", "forge@example.test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "Forge Test"],
            check=True,
        )
        (self.repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "tracked.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qm", "baseline"],
            check=True,
        )
        seed_reconciled_base(self.repo)

        self.state_path = self.temp_path / "state" / "state.sqlite3"
        self.argv_log = self.temp_path / "argv.jsonl"
        self.stdin_log = self.temp_path / "stdin.jsonl"
        self.environment_log = self.temp_path / "environment.jsonl"
        self.control_path = self.temp_path / FAKE_CONTROL_NAME
        self.control_path.write_text(
            json.dumps(
                {
                    "argv_log": str(self.argv_log),
                    "stdin_log": str(self.stdin_log),
                    "environment_log": str(self.environment_log),
                    "assistant_text": "bounded fake report",
                    "parent_tool_use_id": "parent-tool-7",
                    "control_window_seconds": 0.5,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        self.environment = {
            "HOME": str(self.temp_path),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "LC_CTYPE": "C.UTF-8",
            "TMPDIR": str(self.temp_path),
            "SHELL": "/bin/sh",
            "USER": "forge-test",
            "FORGE_STATE_PATH": str(self.state_path),
            "FORGE_LAUNCH_WORKSPACE": str(self.repo),
            "FORGE_CLAUDE_EXECUTABLE": str(FAKE_CLAUDE),
        }
        self.bridges = []
        self.addCleanup(self._cleanup_bridges)

    def write_control(self, **updates):
        current = json.loads(self.control_path.read_text(encoding="utf-8"))
        current.update(updates)
        self.control_path.write_text(
            json.dumps(current, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    def make_repo(self, name):
        repository = self.temp_path / name
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        subprocess.run(
            ["git", "-C", str(repository), "config", "user.email", "forge@example.test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repository), "config", "user.name", "Forge Test"],
            check=True,
        )
        (repository / "tracked.txt").write_text(f"{name}\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repository), "add", "tracked.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(repository), "commit", "-qm", "baseline"],
            check=True,
        )
        seed_reconciled_base(repository)
        return repository

    def make_service(self, *, launch_workspace=None, state_path=None):
        sys.path.insert(0, str(ROOT / "scripts"))
        from forge_lib.service import ForgeService

        return ForgeService(
            state_path=state_path or self.state_path,
            launch_workspace=launch_workspace or self.repo,
            claude_executable=str(FAKE_CLAUDE),
            bridge_path=BRIDGE,
            environment=self.environment,
            _admission_machine_probe=machine_headroom,
        )

    def _cleanup_bridges(self):
        for bridge in reversed(self.bridges):
            bridge.terminate_bridge()

    def bridge(self, *, launch_workspace=None):
        environment = dict(self.environment)
        if launch_workspace is not None:
            environment["FORGE_LAUNCH_WORKSPACE"] = str(launch_workspace)
        bridge = BridgeHarness(environment)
        self.bridges.append(bridge)
        return bridge

    def start_arguments(self, **overrides):
        arguments = {
            "prompt": "Review the frozen plan.",
            "cwd": str(self.repo),
            "role": "review",
            "model": "fable",
            "effort": "max",
            "idempotency_key": "task-7-job",
            "budget": {
                "max_seconds": 5,
                "max_tokens": 4096,
                "max_turns": 4096,
                "max_cost_usd": "1.25",
            },
            "admission": review_admission(module="tests.test_bridge_jobs"),
        }
        arguments.update(overrides)
        return arguments

    def wait_terminal(self, bridge, job_id, timeout=8):
        deadline = time.monotonic() + timeout
        revision = None
        last = None
        while time.monotonic() < deadline:
            arguments = {"job_id": job_id, "timeout_ms": 1000}
            if revision is not None:
                arguments["revision"] = revision
            reply = bridge.tool("forge_wait", arguments, timeout=3)
            self.assertFalse(reply["isError"], reply)
            last = reply["structuredContent"]
            revision = last["revision"]
            if last["status"] in {
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
                "PAUSED_PROVIDER_ACCESS",
                "PAUSED_POLICY",
            }:
                return last
        self.fail(f"job did not terminate: {last}")

    def test_fake_job_survives_bridge_restart_and_returns_bounded_artifact_receipts(self):
        first = self.bridge()
        preflight = first.tool("forge_preflight", {"cwd": str(self.repo)})
        self.assertFalse(preflight["isError"], preflight)
        self.assertEqual(preflight["structuredContent"]["status"], "ready")
        self.assertEqual(preflight["structuredContent"]["claude"]["version"], "2.1.248 (Claude Code)")

        started_at = time.monotonic()
        started = first.tool("forge_start", self.start_arguments())
        elapsed = time.monotonic() - started_at
        self.assertFalse(started["isError"], started)
        self.assertLess(elapsed, 1.0, "forge_start waited for the provider turn")
        job_id = started["structuredContent"]["job_id"]
        first.terminate_bridge()

        second = self.bridge()
        terminal = self.wait_terminal(second, job_id)
        self.assertEqual(terminal["status"], "SUCCEEDED", terminal)
        status = second.tool("forge_status", {"job_id": job_id})
        self.assertFalse(status["isError"], status)
        summary = status["structuredContent"]
        self.assertTrue(summary["session_id"])
        self.assertEqual(summary["parent_tool_use_ids"], ["parent-tool-7"])
        self.assertEqual(summary["requested_model"], "fable")
        self.assertEqual(summary["observed_model"], "fable")

        result = second.tool("forge_result", {"job_id": job_id})
        self.assertFalse(result["isError"], result)
        body = result["structuredContent"]
        self.assertEqual(body["report"], "bounded fake report")
        self.assertNotIn("events", body)
        self.assertNotIn("raw_stream", body)
        self.assertLess(len(json.dumps(result)), 32_768)
        self.assertEqual(
            set(body["artifacts"]),
            {"events", "stderr", "report", "receipt", "worker_log"},
        )
        for artifact in body["artifacts"].values():
            path = Path(artifact["path"])
            self.assertTrue(path.is_file(), artifact)
            self.assertEqual(_mode(path), 0o600)
            self.assertEqual(_mode(path.parent), 0o700)
            self.assertEqual(artifact["sha256"], _sha256(path))

        events = [json.loads(line) for line in Path(body["artifacts"]["events"]["path"]).read_text(encoding="utf-8").splitlines()]
        self.assertEqual([event["type"] for event in events], ["system", "assistant", "result"])
        self.assertEqual(events[1]["parent_tool_use_id"], "parent-tool-7")

        receipt = json.loads(Path(body["artifacts"]["receipt"]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(receipt["session_id"], summary["session_id"])
        self.assertEqual(receipt["parent_tool_use_ids"], ["parent-tool-7"])
        self.assertEqual(receipt["event_hash"], summary["event_hash"])

        observed_environments = [
            json.loads(line)
            for line in self.environment_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(observed_environments), 5)
        for child_environment in observed_environments:
            self.assertFalse(
                any(name.startswith("FORGE_") for name in child_environment),
                child_environment,
            )
        settings_files = list((self.state_path.parent / "runs" / job_id).glob("*/claude-settings.json"))
        self.assertEqual(len(settings_files), 1)
        self.assertEqual(_mode(settings_files[0]), 0o600)
        self.assertTrue(json.loads(settings_files[0].read_text(encoding="utf-8"))["sandbox"]["enabled"])

        listed = second.tool("forge_list", {"limit": 10})
        self.assertFalse(listed["isError"], listed)
        self.assertEqual([item["job_id"] for item in listed["structuredContent"]["jobs"]], [job_id])

        sys.path.insert(0, str(ROOT / "scripts"))
        from forge_lib.state import Store

        stored_events = Store(self.state_path).list_events(job_id)
        self.assertGreaterEqual(len(stored_events), 4)
        previous_hash = None
        for sequence, event in enumerate(stored_events, start=1):
            self.assertEqual(event.sequence, sequence)
            self.assertEqual(event.previous_hash, previous_hash)
            envelope = json.dumps(
                {
                    "kind": event.kind,
                    "payload": event.payload,
                    "previous_hash": previous_hash,
                    "sequence": sequence,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            self.assertEqual(event.event_hash, hashlib.sha256(envelope.encode("utf-8")).hexdigest())
            previous_hash = event.event_hash

        report_path = Path(body["artifacts"]["report"]["path"])
        report_path.unlink()
        report_path.symlink_to("receipt.json")
        tampered = second.tool("forge_result", {"job_id": job_id})
        self.assertTrue(tampered["isError"], tampered)

    def test_fake_job_persists_all_2_1_248_lifecycle_events_without_identity_text(self):
        self.write_control(emit_lifecycle_events=True, control_window_seconds=0)
        bridge = self.bridge()
        started = bridge.tool("forge_start", self.start_arguments())
        self.assertFalse(started["isError"], started)

        job_id = started["structuredContent"]["job_id"]
        terminal = self.wait_terminal(bridge, job_id)
        self.assertEqual(terminal["status"], "SUCCEEDED", terminal)
        result = bridge.tool("forge_result", {"job_id": job_id})
        self.assertFalse(result["isError"], result)
        events_path = Path(
            result["structuredContent"]["artifacts"]["events"]["path"]
        )
        encoded = events_path.read_text(encoding="utf-8")
        events = [json.loads(line) for line in encoded.splitlines()]
        lifecycle_subtypes = {
            event.get("subtype")
            for event in events
            if event.get("type") == "system"
        }

        self.assertTrue(
            {
                "task_started",
                "background_tasks_changed",
                "task_progress",
                "task_updated",
                "task_notification",
                "thinking_tokens",
            }
            <= lifecycle_subtypes
        )
        self.assertNotIn("identity-canary", encoded)

    def test_start_rejects_dirty_or_out_of_boundary_repo_without_launching_fake(self):
        bridge = self.bridge()
        (self.repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        dirty = bridge.tool("forge_start", self.start_arguments(idempotency_key="dirty"))
        self.assertTrue(dirty["isError"], dirty)
        self.assertEqual(dirty["structuredContent"]["status"], "rejected")
        self.assertFalse(self.argv_log.exists())

        (self.repo / "dirty.txt").unlink()
        outside = self.temp_path / "outside"
        outside.mkdir()
        subprocess.run(["git", "init", "-q", str(outside)], check=True)
        subprocess.run(["git", "-C", str(outside), "config", "user.email", "forge@example.test"], check=True)
        subprocess.run(["git", "-C", str(outside), "config", "user.name", "Forge Test"], check=True)
        (outside / "x").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(outside), "add", "x"], check=True)
        subprocess.run(["git", "-C", str(outside), "commit", "-qm", "outside"], check=True)
        rejected = bridge.tool(
            "forge_start",
            self.start_arguments(cwd=str(outside), idempotency_key="outside"),
        )
        self.assertTrue(rejected["isError"], rejected)
        self.assertEqual(rejected["structuredContent"]["status"], "rejected")
        self.assertFalse(self.argv_log.exists())

    def test_list_is_scoped_to_the_bridge_repository_identity(self):
        first = self.bridge()
        first_start = first.tool("forge_start", self.start_arguments(idempotency_key="repo-one"))
        self.assertFalse(first_start["isError"], first_start)
        first_job = first_start["structuredContent"]["job_id"]
        self.wait_terminal(first, first_job)
        first.terminate_bridge()

        other = self.temp_path / "other-repo"
        other.mkdir()
        subprocess.run(["git", "init", "-q", str(other)], check=True)
        subprocess.run(["git", "-C", str(other), "config", "user.email", "forge@example.test"], check=True)
        subprocess.run(["git", "-C", str(other), "config", "user.name", "Forge Test"], check=True)
        (other / "other.txt").write_text("other\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(other), "add", "other.txt"], check=True)
        subprocess.run(["git", "-C", str(other), "commit", "-qm", "other"], check=True)
        seed_reconciled_base(other)

        second = self.bridge(launch_workspace=other)
        other_start = second.tool(
            "forge_start",
            self.start_arguments(cwd=str(other), idempotency_key="repo-two"),
        )
        self.assertFalse(other_start["isError"], other_start)
        other_job = other_start["structuredContent"]["job_id"]
        self.wait_terminal(second, other_job)
        listed = second.tool("forge_list", {"limit": 10})
        self.assertEqual(
            [item["job_id"] for item in listed["structuredContent"]["jobs"]],
            [other_job],
        )

    def test_budget_contract_rejects_before_service_or_provider_execution(self):
        bridge = self.bridge()
        response_id = 99
        payload = {
            "jsonrpc": "2.0",
            "id": response_id,
            "method": "tools/call",
            "params": {
                "name": "forge_start",
                "arguments": self.start_arguments(
                    budget={
                        "max_seconds": 0,
                        "max_tokens": 1,
                        "max_turns": 1,
                        "max_cost_usd": "0.01",
                    }
                ),
            },
        }
        bridge.process.stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
        bridge.process.stdin.flush()
        line = bridge.process.stdout.readline()
        response = json.loads(line)
        self.assertEqual(response["error"]["code"], -32602)
        self.assertFalse(self.argv_log.exists())

    def test_wait_hard_caps_a_larger_contract_timeout_at_thirty_seconds(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        from forge_lib.service import ForgeService, _repo_id
        from forge_lib.state import canonical_json

        clock = [0.0]

        def advance(seconds):
            clock[0] += seconds

        service = ForgeService(
            state_path=self.temp_path / "wait-state" / "state.sqlite3",
            launch_workspace=self.repo,
            claude_executable=str(FAKE_CLAUDE),
            bridge_path=BRIDGE,
            environment=self.environment,
            _admission_machine_probe=machine_headroom,
            monotonic=lambda: clock[0],
            sleep=advance,
        )
        identity, payload = service._normalized_start(self.start_arguments())
        request_hash = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        job = service.store.create_job(
            repo_id=_repo_id(identity),
            idempotency_key="wait-cap",
            request_hash=request_hash,
            payload=payload,
        )

        reply = service.wait({"job_id": job.id, "timeout_ms": 86_400_000})

        self.assertFalse(reply.is_error)
        self.assertTrue(reply.body["wait_timed_out"])
        self.assertEqual(reply.body["wait_cap_ms"], 30_000)
        self.assertGreaterEqual(clock[0], 30.0)
        self.assertLess(clock[0], 30.1)

    def test_job_scoped_reads_are_denied_across_repository_identities(self):
        owner = self.bridge()
        started = owner.tool(
            "forge_start",
            self.start_arguments(idempotency_key="owned-job"),
        )
        job_id = started["structuredContent"]["job_id"]
        self.assertEqual(self.wait_terminal(owner, job_id)["status"], "SUCCEEDED")
        owner.terminate_bridge()

        other_repo = self.make_repo("other-owner")
        other = self.bridge(launch_workspace=other_repo)
        for tool_name, arguments in (
            ("forge_status", {"job_id": job_id}),
            ("forge_wait", {"job_id": job_id, "timeout_ms": 0}),
            ("forge_result", {"job_id": job_id}),
        ):
            with self.subTest(tool=tool_name):
                reply = other.tool(tool_name, arguments)
                self.assertTrue(reply["isError"], reply)
                self.assertEqual(reply["structuredContent"]["error_type"], "JobNotFound")
                self.assertEqual(reply["structuredContent"]["message"], "job not found")

    def test_success_waits_for_provider_descendant_process_group_quiescence(self):
        descendant_log = self.temp_path / "descendant.jsonl"
        self.write_control(
            spawn_descendant=True,
            descendant_ignore_term=True,
            descendant_log=str(descendant_log),
            control_window_seconds=0,
        )
        bridge = self.bridge()
        started = bridge.tool(
            "forge_start",
            self.start_arguments(idempotency_key="descendant-success"),
        )
        job_id = started["structuredContent"]["job_id"]
        terminal = self.wait_terminal(bridge, job_id)
        self.assertEqual(terminal["status"], "SUCCEEDED", terminal)
        descendant_pid = json.loads(descendant_log.read_text(encoding="utf-8").splitlines()[0])["pid"]
        self.addCleanup(
            lambda: os.kill(descendant_pid, signal.SIGKILL)
            if _process_exists(descendant_pid)
            else None
        )
        deadline = time.monotonic() + 2
        while _process_exists(descendant_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(_process_exists(descendant_pid), "terminal job left a provider descendant")

    def test_group_cleanup_handles_a_leader_that_exits_before_its_descendant(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        from forge_lib.processes import terminate_process_group

        pid_path = self.temp_path / "leader-first-child.pid"
        code = (
            "import os,pathlib,signal\n"
            "child=os.fork()\n"
            "if child == 0:\n"
            "    signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
            "    signal.pause()\n"
            "    os._exit(0)\n"
            f"pathlib.Path({str(pid_path)!r}).write_text(str(child))\n"
        )
        leader = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
        leader.wait(timeout=2)
        child_pid = int(pid_path.read_text(encoding="utf-8"))
        self.addCleanup(
            lambda: os.kill(child_pid, signal.SIGKILL) if _process_exists(child_pid) else None
        )

        terminate_process_group(leader)

        deadline = time.monotonic() + 2
        while _process_exists(child_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(_process_exists(child_pid))

    def test_artifact_writers_and_readers_reject_parent_symlink_hardlink_and_substitution(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        from forge_lib.processes import ProcessError, _regular_private_append
        from forge_lib.service import _repo_id
        from forge_lib.state import StateError
        from forge_lib.worker import WorkerError, _append_private_jsonl

        external = self.temp_path / "external"
        external.mkdir(mode=0o700)
        external_event = external / "events.jsonl"
        external_event.write_text("sentinel\n", encoding="utf-8")
        external_event.chmod(0o600)
        symlink_parent = self.temp_path / "symlink-parent"
        symlink_parent.symlink_to(external, target_is_directory=True)
        with self.assertRaises((StateError, WorkerError, OSError)):
            _append_private_jsonl(
                symlink_parent / "events.jsonl",
                {"type": "assistant"},
            )
        self.assertEqual(external_event.read_text(encoding="utf-8"), "sentinel\n")

        external_log = external / "external.log"
        external_log.write_bytes(b"outside")
        external_log.chmod(0o600)
        linked_log = self.temp_path / "linked.log"
        os.link(external_log, linked_log)
        with self.assertRaises(ProcessError):
            stream = _regular_private_append(linked_log)
            stream.close()
        self.assertEqual(external_log.read_bytes(), b"outside")

        service = self.make_service()
        identity, payload = service._normalized_start(self.start_arguments())
        request_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        job = service.store.create_job(
            repo_id=_repo_id(identity),
            idempotency_key="artifact-substitution",
            request_hash=request_hash,
            payload=payload,
        )
        report = service.store.create_attempt_artifact(
            job.id,
            job.current_attempt_id,
            "report.txt",
            b"original",
        )
        replacement = report.parent / "replacement.txt"
        replacement.write_bytes(b"replacement")
        replacement.chmod(0o600)
        with self.assertRaises(StateError):
            with service._open_artifact(
                job.id,
                "report.txt",
                attempt_id=job.current_attempt_id,
            ):
                os.replace(replacement, report)

    def test_result_rejects_hardlinked_and_oversized_report_artifacts(self):
        bridge = self.bridge()
        started = bridge.tool(
            "forge_start",
            self.start_arguments(idempotency_key="artifact-result-hardening"),
        )
        job_id = started["structuredContent"]["job_id"]
        self.assertEqual(self.wait_terminal(bridge, job_id)["status"], "SUCCEEDED")
        result = bridge.tool("forge_result", {"job_id": job_id})
        report = Path(result["structuredContent"]["artifacts"]["report"]["path"])

        external = self.temp_path / "external-report.txt"
        external.write_text("external secret", encoding="utf-8")
        external.chmod(0o600)
        report.unlink()
        os.link(external, report)
        hardlinked = bridge.tool("forge_result", {"job_id": job_id})
        self.assertTrue(hardlinked["isError"], hardlinked)

        report.unlink()
        report.write_bytes(b"X" * (1024 * 1024 + 1))
        report.chmod(0o600)
        oversized = bridge.tool("forge_result", {"job_id": job_id})
        self.assertTrue(oversized["isError"], oversized)

    def test_result_cross_checks_every_terminal_artifact_against_pinned_receipt(self):
        bridge = self.bridge()
        started = bridge.tool(
            "forge_start",
            self.start_arguments(idempotency_key="artifact-receipt-cross-check"),
        )
        job_id = started["structuredContent"]["job_id"]
        self.assertEqual(self.wait_terminal(bridge, job_id)["status"], "SUCCEEDED")
        result = bridge.tool("forge_result", {"job_id": job_id})
        report = Path(result["structuredContent"]["artifacts"]["report"]["path"])
        report.write_text("tampered in place", encoding="utf-8")
        report.chmod(0o600)

        tampered = bridge.tool("forge_result", {"job_id": job_id})

        self.assertTrue(tampered["isError"], tampered)

        self.write_control(provider_error="billing", control_window_seconds=0)
        failed = bridge.tool(
            "forge_start",
            self.start_arguments(idempotency_key="failed-artifact-required"),
        )
        failed_job = failed["structuredContent"]["job_id"]
        self.assertEqual(
            self.wait_terminal(bridge, failed_job)["status"],
            "PAUSED_PROVIDER_ACCESS",
        )
        failed_result = bridge.tool("forge_result", {"job_id": failed_job})
        events = Path(
            failed_result["structuredContent"]["artifacts"]["events"]["path"]
        )
        events.unlink()
        missing = bridge.tool("forge_result", {"job_id": failed_job})
        self.assertTrue(missing["isError"], missing)

    def test_provider_launch_rejects_validation_to_spawn_worktree_replacement(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        from forge_lib.processes import ProcessError, spawn_provider

        identity = os.stat(self.repo, follow_symlinks=False)
        admitted = self.temp_path / "admitted-moved"
        self.repo.rename(admitted)
        outside = self.temp_path / "outside-cwd"
        outside.mkdir()
        self.repo.symlink_to(outside, target_is_directory=True)
        stderr_path = self.temp_path / "provider-stderr.log"
        stderr_path.write_bytes(b"")
        stderr_path.chmod(0o600)

        with self.assertRaises(ProcessError):
            spawn_provider(
                [sys.executable, "-c", "pass"],
                cwd=self.repo,
                expected_cwd_identity=(identity.st_dev, identity.st_ino),
                environment=self.environment,
                stderr_path=stderr_path,
            )

    def test_start_pins_worktree_inode_across_launch_validation(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import forge_lib.service as service_module
        from forge_lib.boundaries import BoundaryError

        service = self.make_service()
        replacement = self.make_repo("replacement-worktree")
        admitted_moved = self.temp_path / "admitted-after-validation"
        real_validate_launch = service_module.validate_launch

        def replace_after_validation(*args, **kwargs):
            identity = real_validate_launch(*args, **kwargs)
            self.repo.rename(admitted_moved)
            replacement.rename(self.repo)
            return identity

        with mock.patch(
            "forge_lib.service.validate_launch",
            side_effect=replace_after_validation,
        ), self.assertRaises(BoundaryError):
            service._normalized_start(self.start_arguments())

    def test_provider_launch_uses_held_worktree_directory_during_popen(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import forge_lib.processes as process_module

        expected = os.stat(self.repo, follow_symlinks=False)
        admitted_moved = self.temp_path / "admitted-during-popen"
        outside = self.temp_path / "outside-during-popen"
        outside.mkdir()
        stderr_path = self.temp_path / "provider-held-cwd-stderr.log"
        stderr_path.write_bytes(b"")
        stderr_path.chmod(0o600)
        real_provider_process = process_module._ProviderProcess

        def swap_only_during_popen(*args, **kwargs):
            self.repo.rename(admitted_moved)
            self.repo.symlink_to(outside, target_is_directory=True)
            try:
                return real_provider_process(*args, **kwargs)
            finally:
                self.repo.unlink()
                admitted_moved.rename(self.repo)

        with mock.patch(
            "forge_lib.processes._ProviderProcess",
            side_effect=swap_only_during_popen,
        ):
            provider = process_module.spawn_provider(
                [sys.executable, "-c", "import os; print(os.getcwd())"],
                cwd=self.repo,
                expected_cwd_identity=(expected.st_dev, expected.st_ino),
                environment=self.environment,
                stderr_path=stderr_path,
            )
        assert provider.stdout is not None
        observed_cwd = provider.stdout.readline().decode("utf-8").strip()
        provider.stdout.close()
        if provider.stdin is not None:
            provider.stdin.close()
        if provider.stderr is not None:
            provider.stderr.close()
        process_module.terminate_process_group(provider)

        self.assertEqual(observed_cwd, str(self.repo.resolve(strict=True)))

    def test_provider_launch_rejects_git_marker_replacement_at_popen_boundary(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import forge_lib.processes as process_module
        from forge_lib.boundaries import inspect_repo

        def path_pin(path, kind):
            canonical = path.resolve(strict=True)
            information = os.stat(canonical, follow_symlinks=False)
            return process_module.PathPin(
                path=canonical,
                kind=kind,
                device=information.st_dev,
                inode=information.st_ino,
                owner=information.st_uid,
            )

        marker = self.repo / ".git"
        identity = inspect_repo(self.repo)
        repository_pin = process_module.RepositoryLaunchPin(
            worktree=path_pin(self.repo, "directory"),
            git_marker=path_pin(marker, "directory"),
            git_dir=path_pin(marker, "directory"),
            common_dir=path_pin(marker, "directory"),
            head=identity.head,
            status_fingerprint=identity.status_fingerprint,
            tracked_content_fingerprint=identity.tracked_content_fingerprint,
        )
        stderr_path = self.temp_path / "provider-metadata-boundary-stderr.log"
        stderr_path.write_bytes(b"")
        stderr_path.chmod(0o600)
        displaced = self.temp_path / "admitted-git-metadata"
        sentinel = self.temp_path / "provider-executed-after-metadata-swap"
        real_provider_process = process_module._ProviderProcess
        callback_count = 0

        def mark_popen_attempt():
            nonlocal callback_count
            callback_count += 1

        def replace_marker_at_constructor(*args, **kwargs):
            marker.rename(displaced)
            shutil.copytree(displaced, marker, symlinks=True)
            return real_provider_process(*args, **kwargs)

        try:
            with mock.patch(
                "forge_lib.processes._ProviderProcess",
                side_effect=replace_marker_at_constructor,
            ), self.assertRaises(process_module.ProcessError):
                process_module.spawn_provider(
                    [
                        sys.executable,
                        "-c",
                        f"from pathlib import Path; Path({str(sentinel)!r}).write_text('ran')",
                    ],
                    cwd=self.repo,
                    expected_repository_identity=repository_pin,
                    environment=self.environment,
                    stderr_path=stderr_path,
                    before_popen=mark_popen_attempt,
                )
        finally:
            if displaced.exists():
                if marker.exists():
                    shutil.rmtree(marker)
                displaced.rename(marker)

        self.assertEqual(callback_count, 1)
        self.assertFalse(sentinel.exists())

    def test_provider_pre_popen_callback_failure_releases_held_worktree(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import forge_lib.processes as process_module

        expected = os.stat(self.repo, follow_symlinks=False)
        stderr_path = self.temp_path / "provider-callback-stderr.log"
        stderr_path.write_bytes(b"")
        stderr_path.chmod(0o600)
        held_descriptor = os.open(self.repo, os.O_RDONLY | os.O_DIRECTORY)

        def fail_before_popen():
            raise RuntimeError("local callback failure")

        with mock.patch(
            "forge_lib.processes._open_worktree_descriptor",
            return_value=(
                held_descriptor,
                (expected.st_dev, expected.st_ino),
            ),
        ), self.assertRaisesRegex(RuntimeError, "local callback failure"):
            process_module.spawn_provider(
                [sys.executable, "-c", "pass"],
                cwd=self.repo,
                expected_cwd_identity=(expected.st_dev, expected.st_ino),
                environment=self.environment,
                stderr_path=stderr_path,
                before_popen=fail_before_popen,
            )

        with self.assertRaises(OSError):
            os.fstat(held_descriptor)

    def test_attempt_event_count_and_byte_limits_fail_closed(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        from forge_lib import state as state_module
        from forge_lib.state import StateError, Store

        store = Store(self.temp_path / "event-bounds" / "state.sqlite3")
        job = store.create_job(
            repo_id="repo",
            idempotency_key="count-bound",
            request_hash="hash",
            payload={},
        )
        event_limit = getattr(state_module, "MAX_ATTEMPT_EVENTS", 1024)
        for index in range(event_limit):
            store.append_event(job.id, "EVENT", {"index": index})
        with self.assertRaises(StateError):
            store.append_event(job.id, "EVENT", {"index": event_limit})

        bytes_job = store.create_job(
            repo_id="repo",
            idempotency_key="byte-bound",
            request_hash="hash",
            payload={},
        )
        large = "X" * 900_000
        byte_limit = getattr(state_module, "MAX_ATTEMPT_EVENT_BYTES", 16 * 1024 * 1024)
        admitted = byte_limit // len(large)
        for index in range(admitted):
            store.append_event(bytes_job.id, "EVENT", {"index": index, "data": large})
        with self.assertRaises(StateError):
            store.append_event(bytes_job.id, "EVENT", {"data": large})

    def test_stderr_and_event_floods_fail_the_job_with_bounded_artifacts(self):
        self.write_control(stderr_bytes=1024 * 1024 + 1, control_window_seconds=0)
        bridge = self.bridge()
        started = bridge.tool(
            "forge_start",
            self.start_arguments(idempotency_key="stderr-flood"),
        )
        stderr_job = started["structuredContent"]["job_id"]
        terminal = self.wait_terminal(bridge, stderr_job, timeout=12)
        self.assertEqual(terminal["status"], "FAILED", terminal)
        result = bridge.tool("forge_result", {"job_id": stderr_job})
        stderr_path = Path(result["structuredContent"]["artifacts"]["stderr"]["path"])
        self.assertLessEqual(stderr_path.stat().st_size, 1024 * 1024)
        self.assertEqual(
            stderr_path.read_text(encoding="utf-8"),
            "[REDACTED: provider stderr exceeded byte bound]\n",
        )

        self.write_control(stderr_bytes=0, extra_assistant_events=1100)
        started = bridge.tool(
            "forge_start",
            self.start_arguments(idempotency_key="event-flood"),
        )
        event_job = started["structuredContent"]["job_id"]
        terminal = self.wait_terminal(bridge, event_job, timeout=15)
        self.assertEqual(terminal["status"], "FAILED", terminal)

    def test_provider_canaries_are_redacted_from_status_results_and_all_artifacts(self):
        report_secret = "TOPSECRETVALUE1234567890"
        parent_secret = "ABCDEFGHIJKLMNOPQRSTUVWX"
        usage_secret = "USAGESECRETVALUE1234567890"
        stderr_secret = "STDERRSECRETVALUE1234567890"
        self.write_control(
            assistant_text=f"api_key={report_secret}",
            parent_tool_use_id=f"authorization=Bearer {parent_secret}",
            usage_extension={"diagnostic_token": usage_secret},
            stderr_text=f"client_secret={stderr_secret}\n",
            control_window_seconds=0,
        )
        bridge = self.bridge()
        started = bridge.tool(
            "forge_start",
            self.start_arguments(
                prompt=f"Echo api_key={report_secret}",
                idempotency_key="redaction-canaries",
            ),
        )
        job_id = started["structuredContent"]["job_id"]
        self.assertEqual(self.wait_terminal(bridge, job_id)["status"], "SUCCEEDED")
        status = bridge.tool("forge_status", {"job_id": job_id})
        result = bridge.tool("forge_result", {"job_id": job_id})
        published = json.dumps({"status": status, "result": result}, sort_keys=True)
        for secret in (report_secret, parent_secret, usage_secret, stderr_secret):
            self.assertNotIn(secret, published)
        self.assertIn("[REDACTED]", published)
        for artifact in result["structuredContent"]["artifacts"].values():
            data = Path(artifact["path"]).read_text(encoding="utf-8", errors="replace")
            for secret in (report_secret, parent_secret, usage_secret, stderr_secret):
                self.assertNotIn(secret, data, artifact)

    def test_repeated_and_concurrent_idempotent_starts_dispatch_only_once(self):
        service = self.make_service()
        arguments = self.start_arguments(idempotency_key="dispatch-once")
        with mock.patch(
            "forge_lib.service.spawn_detached_worker",
            side_effect=_bound_worker_launch,
        ) as spawn:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                replies = list(executor.map(lambda _: service.start(arguments), range(8)))
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(len({reply.body["job_id"] for reply in replies}), 1)
        self.assertEqual(sum(not reply.body["idempotent"] for reply in replies), 1)

    def test_idempotent_start_after_progress_or_terminal_never_redispatches(self):
        states = {
            "PREFLIGHT": ("PREFLIGHT",),
            "SUCCEEDED": ("PREFLIGHT", "RUNNING", "SUCCEEDED"),
        }
        for index, (expected_status, transitions) in enumerate(states.items()):
            with self.subTest(status=expected_status):
                service = self.make_service(
                    state_path=(
                        self.temp_path
                        / f"dispatch-repeat-{expected_status.lower()}"
                        / "state.sqlite3"
                    )
                )
                arguments = self.start_arguments(
                    idempotency_key=f"dispatch-repeat-{index}"
                )
                with mock.patch(
                    "forge_lib.service.spawn_detached_worker",
                    side_effect=_bound_worker_launch,
                ):
                    first = service.start(arguments)
                job_id = first.body["job_id"]
                for new_status in transitions:
                    current = service.store.get_job(job_id)
                    service.store.transition(
                        job_id,
                        expected_revision=current.revision,
                        new_status=new_status,
                    )

                with mock.patch(
                    "forge_lib.service.spawn_detached_worker",
                    return_value=910 + index,
                ) as spawn:
                    repeated = service.start(arguments)

                self.assertEqual(spawn.call_count, 0)
                self.assertTrue(repeated.body["idempotent"])
                self.assertEqual(repeated.body["job_id"], job_id)
                self.assertEqual(repeated.body["status"], expected_status)

    def test_failed_worker_spawn_releases_dispatch_for_one_safe_retry(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        from forge_lib.processes import ProcessError

        service = self.make_service()
        arguments = self.start_arguments(idempotency_key="dispatch-spawn-retry")

        launch_attempts = iter((False, True))

        def fail_then_launch(*args, **kwargs):
            if not next(launch_attempts):
                raise ProcessError("offline spawn failure")
            return _bound_worker_launch(*args, **kwargs)

        with mock.patch(
            "forge_lib.service.spawn_detached_worker",
            side_effect=fail_then_launch,
        ) as spawn:
            with self.assertRaises(ProcessError):
                service.start(arguments)
            retry = service.start(arguments)

        self.assertEqual(spawn.call_count, 2)
        self.assertFalse(retry.is_error)
        self.assertTrue(retry.body["idempotent"])
        attempt = service.store.get_attempt(retry.body["attempt_id"])
        self.assertEqual(attempt.dispatch_state, "DISPATCHED")
        self.assertEqual(attempt.worker_pid, os.getpid())

    def test_failed_launch_preparation_releases_only_its_dispatch_for_retry(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        from forge_lib.state import StateError

        failure_points = ("artifact", "environment")
        for index, failure_point in enumerate(failure_points):
            with self.subTest(failure_point=failure_point):
                service = self.make_service(
                    state_path=(
                        self.temp_path
                        / f"dispatch-preparation-{failure_point}"
                        / "state.sqlite3"
                    )
                )
                arguments = self.start_arguments(
                    idempotency_key=f"dispatch-preparation-{index}"
                )
                target = (
                    mock.patch.object(
                        service.store,
                        "create_artifact",
                        side_effect=StateError("offline artifact preparation failure"),
                    )
                    if failure_point == "artifact"
                    else mock.patch.object(
                        service,
                        "_worker_environment",
                        side_effect=StateError("offline environment preparation failure"),
                    )
                )
                with target:
                    with self.assertRaises(StateError):
                        service.start(arguments)

                job_id = service.list_jobs({}).body["jobs"][0]["job_id"]
                stranded_job = service.store.get_job(job_id)
                stranded = service.store.get_attempt(
                    stranded_job.current_attempt_id
                )
                self.assertEqual(stranded.dispatch_state, "UNCLAIMED")
                self.assertIsNone(stranded.worker_pid)

                with mock.patch(
                    "forge_lib.service.spawn_detached_worker",
                    side_effect=_bound_worker_launch,
                ) as spawn:
                    retry = service.start(arguments)
                self.assertEqual(spawn.call_count, 1)
                self.assertTrue(retry.body["idempotent"])
                attempt = service.store.get_attempt(retry.body["attempt_id"])
                self.assertEqual(attempt.worker_pid, os.getpid())

    def test_dispatch_claim_returns_its_token_without_a_fallible_handoff_read(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        from forge_lib.state import StateError

        service = self.make_service(
            state_path=self.temp_path / "dispatch-handoff" / "state.sqlite3"
        )
        arguments = self.start_arguments(idempotency_key="dispatch-handoff")
        original_get_attempt = service.store.get_attempt
        launch_started = False

        def reject_prelaunch_token_read(attempt_id):
            if not launch_started:
                raise StateError("offline token read failure")
            return original_get_attempt(attempt_id)

        def launch_with_claim(*_args, **_kwargs):
            nonlocal launch_started
            launch_started = True
            return _bound_worker_launch(*_args, **_kwargs)

        with (
            mock.patch.object(
                service.store,
                "get_attempt",
                side_effect=reject_prelaunch_token_read,
            ),
            mock.patch(
                "forge_lib.service.spawn_detached_worker",
                side_effect=launch_with_claim,
            ) as spawn,
        ):
            started = service.start(arguments)

        self.assertEqual(spawn.call_count, 1)
        self.assertFalse(started.body["idempotent"])
        attempt = original_get_attempt(started.body["attempt_id"])
        self.assertEqual(attempt.worker_pid, os.getpid())
        self.assertIsNotNone(attempt.dispatch_token)

    def test_stale_worker_token_cannot_execute_after_crash_reclaim(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        from forge_lib.service import _repo_id
        from forge_lib.state import canonical_json

        service = self.make_service(
            state_path=self.temp_path / "dispatch-fence" / "state.sqlite3"
        )
        arguments = self.start_arguments(idempotency_key="dispatch-fence")
        identity, payload = service._normalized_start(arguments)
        request_hash = hashlib.sha256(
            canonical_json(payload).encode("utf-8")
        ).hexdigest()
        job = service.store.create_job(
            repo_id=_repo_id(identity),
            idempotency_key=arguments["idempotency_key"],
            request_hash=request_hash,
            payload=payload,
        )
        self.assertTrue(
            service.store.claim_dispatch(job.id, owner_pid=2_147_483_647)
        )
        stale_token = service.store.get_attempt(job.current_attempt_id).dispatch_token
        self.assertIsNotNone(stale_token)

        self.assertTrue(service.store.claim_dispatch(job.id, owner_pid=os.getpid()))
        current = service.store.get_attempt(job.current_attempt_id)
        self.assertIsNotNone(current.dispatch_token)
        self.assertNotEqual(current.dispatch_token, stale_token)

        self.assertFalse(
            service.store.claim_worker_execution(job.id, stale_token, worker_pid=701)
        )
        self.assertTrue(
            service.store.claim_worker_execution(
                job.id,
                current.dispatch_token,
                worker_pid=702,
            )
        )
        self.assertFalse(
            service.store.claim_worker_execution(job.id, stale_token, worker_pid=701)
        )
        self.assertEqual(
            service.store.get_attempt(job.current_attempt_id).worker_pid,
            702,
        )

    def test_dead_dispatch_owner_is_recoverable_after_bridge_crash(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        from forge_lib.service import _repo_id
        from forge_lib.state import canonical_json

        service = self.make_service()
        arguments = self.start_arguments(idempotency_key="dispatch-owner-crash")
        request_declaration = {
            "prompt": arguments["prompt"],
            "cwd": arguments["cwd"],
            "role": arguments["role"],
            "model": arguments["model"],
            "effort": arguments["effort"],
            "budget": arguments["budget"],
            "admission": arguments["admission"],
        }
        request_hash = hashlib.sha256(
            canonical_json(request_declaration).encode("utf-8")
        ).hexdigest()
        job, replay = service.store.create_admitted_job(
            repo_id=service._launch_repo_id,
            idempotency_key=arguments["idempotency_key"],
            request_hash=request_hash,
            build_payload=lambda job_id, attempt_id, active_reservations: service._admitted_payload(
                arguments,
                job_id=job_id,
                attempt_id=attempt_id,
                active_reservations=active_reservations,
                request_hash=request_hash,
            ),
        )
        self.assertFalse(replay)
        self.assertTrue(
            service.store.claim_dispatch(job.id, owner_pid=2_147_483_647)
        )

        with mock.patch(
            "forge_lib.service.spawn_detached_worker",
            side_effect=_bound_worker_launch,
        ) as spawn:
            recovered = service.start(arguments)

        self.assertEqual(spawn.call_count, 1)
        self.assertTrue(recovered.body["idempotent"])
        attempt = service.store.get_attempt(recovered.body["attempt_id"])
        self.assertEqual(attempt.worker_pid, os.getpid())
        self.assertEqual(attempt.dispatch_owner_pid, os.getpid())

    def test_list_public_statuses_map_to_every_internal_state(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        from forge_lib.service import PUBLIC_STATUSES, _repo_id

        service = self.make_service(state_path=self.temp_path / "list-state" / "state.sqlite3")
        identity, payload = service._normalized_start(self.start_arguments())
        repo_id = _repo_id(identity)

        def create(name, transitions):
            job = service.store.create_job(
                repo_id=repo_id,
                idempotency_key=name,
                request_hash=name,
                payload=payload,
            )
            for new_status in transitions:
                current = service.store.get_job(job.id)
                service.store.transition(
                    job.id,
                    expected_revision=current.revision,
                    new_status=new_status,
                )
            return service.store.get_job(job.id)

        expected = {
            "queued": {
                create("created", []).status,
                create("preflight", ["PREFLIGHT"]).status,
            },
            "running": {
                create("running", ["PREFLIGHT", "RUNNING"]).status,
                create("interrupting", ["PREFLIGHT", "RUNNING", "INTERRUPTING"]).status,
            },
            "completed": {
                create("succeeded", ["PREFLIGHT", "RUNNING", "SUCCEEDED"]).status,
            },
            "failed": {
                create("failed", ["FAILED"]).status,
                create("paused-policy", ["PREFLIGHT", "PAUSED_POLICY"]).status,
                create("paused-provider", ["PREFLIGHT", "PAUSED_PROVIDER_ACCESS"]).status,
            },
        }
        for public_status, internal_statuses in expected.items():
            with self.subTest(status=public_status):
                listed = service.list_jobs({"status": public_status, "limit": 50})
                self.assertFalse(listed.is_error)
                self.assertEqual(
                    {job["internal_status"] for job in listed.body["jobs"]},
                    internal_statuses,
                )
                self.assertEqual(
                    {job["status"] for job in listed.body["jobs"]},
                    {public_status},
                )
        self.assertEqual(PUBLIC_STATUSES["cancelled"], ("CANCELLED",))

    def test_session_model_and_parent_identity_survive_more_than_512_events(self):
        self.write_control(extra_assistant_events=514, control_window_seconds=0)
        bridge = self.bridge()
        started = bridge.tool(
            "forge_start",
            self.start_arguments(idempotency_key="identity-window"),
        )
        job_id = started["structuredContent"]["job_id"]
        terminal = self.wait_terminal(bridge, job_id, timeout=15)
        self.assertEqual(terminal["status"], "SUCCEEDED", terminal)
        status = bridge.tool("forge_status", {"job_id": job_id})
        body = status["structuredContent"]
        self.assertTrue(body["session_id"])
        self.assertEqual(body["observed_model"], "fable")
        self.assertEqual(body["parent_tool_use_ids"], ["parent-tool-7"])
        self.assertGreater(body["progress"]["event_count"], 514)

    def test_paused_provider_job_has_a_bounded_referenced_receipt(self):
        self.write_control(provider_error="billing", control_window_seconds=0)
        bridge = self.bridge()
        started = bridge.tool(
            "forge_start",
            self.start_arguments(idempotency_key="failed-receipt"),
        )
        job_id = started["structuredContent"]["job_id"]
        terminal = self.wait_terminal(bridge, job_id)
        self.assertEqual(terminal["status"], "PAUSED_PROVIDER_ACCESS", terminal)
        self.assertEqual(
            terminal["pause"],
            {"classification": "billing", "reason": "billing_access_required"},
        )
        result = bridge.tool("forge_result", {"job_id": job_id})
        self.assertFalse(result["isError"], result)
        artifacts = result["structuredContent"]["artifacts"]
        self.assertIn("receipt", artifacts)
        receipt_path = Path(artifacts["receipt"]["path"])
        self.assertLessEqual(receipt_path.stat().st_size, 256 * 1024)
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["terminal_status"], "PAUSED_PROVIDER_ACCESS")
        self.assertEqual(
            receipt["pause"],
            {"classification": "billing", "reason": "billing_access_required"},
        )
        self.assertEqual(
            result["structuredContent"]["terminal_receipt"]["sha256"],
            artifacts["receipt"]["sha256"],
        )

    def test_model_and_idempotency_are_synchronously_nonempty_and_nul_safe(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        from forge_lib.contracts import ContractError, validate_tool_arguments

        cases = (
            {"prompt": "p", "model": ""},
            {"prompt": "p", "model": "fable\x00escape"},
            {"prompt": "p", "idempotency_key": ""},
            {"prompt": "p", "idempotency_key": "key\x00escape"},
        )
        for arguments in cases:
            with self.subTest(arguments=arguments), self.assertRaises(ContractError):
                validate_tool_arguments("forge_start", arguments)


if __name__ == "__main__":
    unittest.main()
