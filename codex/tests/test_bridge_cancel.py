import concurrent.futures
import json
import os
import select
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path

from tests.admission_fixture import (
    machine_headroom,
    review_admission,
    seed_reconciled_base,
)
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_BRIDGE = ROOT / "scripts" / "forge_bridge.py"
NO_CONTAINMENT_BRIDGE = ROOT / "tests" / "fixtures" / "forge_bridge_admission.py"
BRIDGE = ROOT / "tests" / "fixtures" / "forge_bridge_contained.py"
FAKE_CLAUDE = ROOT / "tests" / "fixtures" / "fake_claude.py"
FAKE_CONTROL_NAME = "forge-fake-claude-control.json"
sys.path.insert(0, str(ROOT / "scripts"))

from forge_lib.processes import ProcessError  # noqa: E402
from forge_lib.service import ForgeService  # noqa: E402
from forge_lib.state import (  # noqa: E402
    MAX_ATTEMPT_EVENTS,
    InvalidTransition,
    RevisionConflict,
    StateError,
    Store,
)


def _process_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _non_zombie_process_exists(pid):
    if not _process_exists(pid):
        return False
    status = subprocess.run(
        ["/bin/ps", "-o", "state=", "-p", str(pid)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        text=True,
    ).stdout.strip()
    return bool(status) and not status.upper().startswith("Z")


class BridgeHarness:
    def __init__(self, environment, *, bridge_path=BRIDGE):
        self.process = subprocess.Popen(
            [sys.executable, "-u", str(bridge_path)],
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
            assert self.process.stderr is not None
            stderr = self.process.stderr.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"bridge exited during {name}: {stderr}")
        response = json.loads(line)
        if "error" in response:
            raise AssertionError(response)
        return response["result"]

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=2)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()


class BridgeCancelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temp_path = Path(temporary.name).resolve(strict=True)
        self.repo = self.make_repo("repo")
        self.other_repo = self.make_repo("other-repo")
        self.state_path = self.temp_path / "state" / "state.sqlite3"
        self.stdin_log = self.temp_path / "stdin.jsonl"
        self.descendant_log = self.temp_path / "descendant.jsonl"
        self.descendant_manifest = self.temp_path / "descendant-manifest.jsonl"
        self.double_fork_log = self.temp_path / "double-fork.jsonl"
        self.control_path = self.temp_path / FAKE_CONTROL_NAME
        self.write_control(
            control_window_seconds=5,
            emit_interrupt_ack=True,
            spawn_descendant=True,
            descendant_ignore_term=True,
            descendant_log=str(self.descendant_log),
            descendant_manifest=str(self.descendant_manifest),
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
        self._containment_index = 0
        self.addCleanup(self.close_bridges)

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
        (repository / "tracked.txt").write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repository), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(repository), "commit", "-qm", "baseline"], check=True)
        seed_reconciled_base(repository)
        return repository.resolve(strict=True)

    def write_control(self, **updates):
        current = {}
        if hasattr(self, "control_path") and self.control_path.exists():
            current = json.loads(self.control_path.read_text(encoding="utf-8"))
        current.update(
            {
                "stdin_log": str(getattr(self, "stdin_log", self.temp_path / "stdin.jsonl")),
                "assistant_text": "bounded fake report",
                **updates,
            }
        )
        path = getattr(self, "control_path", self.temp_path / FAKE_CONTROL_NAME)
        path.write_text(
            json.dumps(current, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    def bridge(self, *, repository=None):
        environment = dict(self.environment)
        if repository is not None:
            environment["FORGE_LAUNCH_WORKSPACE"] = str(repository)
        bridge = BridgeHarness(environment)
        self.bridges.append(bridge)
        return bridge

    def _containment(self, *descendant_pids):
        from forge_lib import processes

        self._containment_index += 1
        path = self.temp_path / f"direct-manifest-{self._containment_index}.jsonl"
        entries = [
            {
                "identity": processes.process_instance_identity(pid),
                "pid": pid,
                "process_group_id": os.getpgid(pid),
            }
            for pid in descendant_pids
        ]
        path.write_text(
            "".join(
                json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n"
                for entry in entries
            ),
            encoding="utf-8",
        )
        path.chmod(0o400)
        return processes._test_cancellation_containment(path)

    def _admission_manifest_pin(self, containment, process_group_id):
        from forge_lib import processes

        admission = processes.cancellation_admission_preflight(
            containment,
            expected_process_group_id=process_group_id,
        )
        self.assertTrue(admission["supported"], admission)
        return admission["_test_manifest_pin"]

    def _prepare_delivered_cancellation(self, name):
        """Build a live, delivered cancellation without launching a provider."""

        state_path = self.temp_path / name / "state.sqlite3"
        owner = Store(state_path)
        job = owner.create_job(
            repo_id=f"{name}-repo",
            idempotency_key=name,
            request_hash="hash",
            payload={},
        )
        claim = owner.claim_dispatch(job.id, owner_pid=os.getpid())
        assert claim is not None
        owner.record_worker_pid(job.id, os.getpid(), _dispatch_claim=claim)
        authority = owner.claim_worker_authority(job.id, claim.token)
        assert authority is not None
        current = owner.get_job(job.id)
        owner.transition(
            job.id,
            expected_revision=current.revision,
            new_status="PREFLIGHT",
        )
        owner.record_provider_identity(
            job.id,
            session_id=f"{name}-session",
            provider_pgid=os.getpid(),
        )
        current = owner.get_job(job.id)
        owner.transition(
            job.id,
            expected_revision=current.revision,
            new_status="RUNNING",
        )
        current = owner.get_job(job.id)
        attempt = owner.get_attempt(current.current_attempt_id)
        admission = owner.request_cancellation(
            job.id,
            expected_revision=current.revision,
            live_worker_identity=attempt.worker_identity,
        )
        assert admission.command is not None
        command = owner.claim_next_command(
            job.id,
            dispatch_token=claim.token,
            worker_pid=os.getpid(),
        )
        assert command is not None
        owner.mark_command_delivered(
            job.id,
            command.id,
            dispatch_token=claim.token,
            worker_pid=os.getpid(),
        )
        return owner, job, claim, authority, admission.command

    def _prepare_claimed_cancellation(self, name, *, claim_command=True):
        """Build a live cancellation with a QUEUED or DELIVERING command."""

        state_path = self.temp_path / name / "state.sqlite3"
        owner = Store(state_path)
        job = owner.create_job(
            repo_id=f"{name}-repo",
            idempotency_key=name,
            request_hash="hash",
            payload={},
        )
        claim = owner.claim_dispatch(job.id, owner_pid=os.getpid())
        assert claim is not None
        owner.record_worker_pid(job.id, os.getpid(), _dispatch_claim=claim)
        authority = owner.claim_worker_authority(job.id, claim.token)
        assert authority is not None
        for status in ("PREFLIGHT", "RUNNING"):
            current = owner.get_job(job.id)
            owner.transition(
                job.id,
                expected_revision=current.revision,
                new_status=status,
            )
        owner.record_provider_identity(
            job.id,
            session_id=f"{name}-session",
            provider_pgid=os.getpid(),
        )
        running = owner.get_job(job.id)
        attempt = owner.get_attempt(running.current_attempt_id)
        admission = owner.request_cancellation(
            job.id,
            expected_revision=running.revision,
            live_worker_identity=attempt.worker_identity,
        )
        assert admission.command is not None
        if claim_command:
            command = owner.claim_next_command(
                job.id,
                dispatch_token=claim.token,
                worker_pid=os.getpid(),
            )
            assert command is not None
        else:
            command = admission.command
        return owner, job, claim, authority, command

    def close_bridges(self):
        for bridge in reversed(self.bridges):
            bridge.close()

    def start_arguments(self, **overrides):
        arguments = {
            "prompt": "Review the frozen cancellation plan.",
            "cwd": str(self.repo),
            "role": "review",
            "model": "fable",
            "effort": "max",
            "idempotency_key": "task-9-job",
            "budget": {
                "max_seconds": 12,
                "max_tokens": 4096,
                "max_turns": 4096,
                "max_cost_usd": "1.25",
            },
            "admission": review_admission(module="tests.test_bridge_cancel"),
        }
        arguments.update(overrides)
        return arguments

    def create_admitted_job(self, service, arguments):
        declaration = {
            "prompt": arguments["prompt"],
            "cwd": arguments["cwd"],
            "role": arguments["role"],
            "model": arguments["model"],
            "effort": arguments["effort"],
            "budget": arguments["budget"],
            "admission": arguments["admission"],
        }
        request_hash = __import__("hashlib").sha256(
            json.dumps(
                declaration,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
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
        return job

    def wait_status(self, bridge, job_id, statuses, timeout=12):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            reply = bridge.tool("forge_status", {"job_id": job_id})
            self.assertFalse(reply["isError"], reply)
            last = reply["structuredContent"]
            if last["status"] in statuses:
                return last
            time.sleep(0.02)
        self.fail(f"job did not reach {statuses}: {last}")

    def start_running(self, bridge, **overrides):
        started = bridge.tool("forge_start", self.start_arguments(**overrides))
        self.assertFalse(started["isError"], started)
        job_id = started["structuredContent"]["job_id"]
        running = self.wait_status(bridge, job_id, {"RUNNING"})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            events = Store(self.state_path).list_events(job_id)
            if (
                self.stdin_log.exists()
                and self.descendant_log.exists()
                and sum(event.kind == "CLAUDE_EVENT" for event in events) >= 2
            ):
                self.addCleanup(self.force_stop_job, job_id)
                refreshed = bridge.tool(
                    "forge_status", {"job_id": job_id}
                )["structuredContent"]
                self.assertEqual(refreshed["status"], "RUNNING")
                return job_id, refreshed
            time.sleep(0.01)
        self.fail("fake provider did not publish its launch receipts")

    def force_stop_job(self, job_id):
        try:
            store = Store(self.state_path)
            job = store.get_job(job_id)
            attempt = store.get_attempt(job.current_attempt_id)
            process_group_id = attempt.provider_pgid
            worker_pid = attempt.worker_pid
        except Exception:
            return
        if process_group_id is None:
            return
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 2
        while _process_exists(process_group_id) and time.monotonic() < deadline:
            time.sleep(0.01)
        if worker_pid is not None:
            deadline = time.monotonic() + 2
            while _process_exists(worker_pid) and time.monotonic() < deadline:
                time.sleep(0.01)
            if _process_exists(worker_pid):
                try:
                    os.killpg(worker_pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    def cancel(self, bridge, job_id, revision):
        return bridge.tool(
            "forge_cancel",
            {"job_id": job_id, "revision": revision},
        )

    def test_cancel_is_receipted_acknowledged_and_quiescent_before_cancelled(self):
        bridge = self.bridge()
        job_id, running = self.start_running(bridge)
        descendant_pid = json.loads(
            self.descendant_log.read_text(encoding="utf-8").splitlines()[0]
        )["pid"]
        self.addCleanup(
            lambda: os.kill(descendant_pid, signal.SIGKILL)
            if _process_exists(descendant_pid)
            else None
        )

        accepted = self.cancel(bridge, job_id, running["revision"])
        self.assertFalse(accepted["isError"], accepted)
        admission = accepted["structuredContent"]
        self.assertEqual(admission["status"], "INTERRUPTING")
        self.assertFalse(admission["idempotent"])
        self.assertTrue(admission["request_id"].startswith("cancel-"))

        terminal = self.wait_status(bridge, job_id, {"CANCELLED"})
        self.assertEqual(terminal["status"], "CANCELLED")
        listed = bridge.tool(
            "forge_list",
            {"status": "cancelled", "limit": 50},
        )
        self.assertFalse(listed["isError"], listed)
        self.assertEqual(
            [job["job_id"] for job in listed["structuredContent"]["jobs"]],
            [job_id],
        )
        deadline = time.monotonic() + 2
        while _process_exists(descendant_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(_process_exists(descendant_pid))

        store = Store(self.state_path)
        events = store.list_events(job_id)
        kinds = [event.kind for event in events]
        required_order = [
            "CANCELLATION_ACCEPTED",
            "COMMAND_DELIVERY_CLAIMED",
            "COMMAND_DELIVERED",
            "CANCELLATION_ACKNOWLEDGED",
            "CANCELLATION_TERM_SENT",
            "CANCELLATION_KILL_SENT",
            "CANCELLATION_QUIESCENT",
            "CANCELLATION_INBOX_CLOSED",
        ]
        positions = [kinds.index(kind) for kind in required_order]
        self.assertEqual(positions, sorted(positions), kinds)
        commands = store.list_commands(job_id)
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0].kind, "CANCEL")
        self.assertEqual(commands[0].status, "ACKNOWLEDGED")
        self.assertEqual(commands[0].payload["request_id"], admission["request_id"])

        frames = [
            json.loads(line)
            for line in self.stdin_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual([frame["type"] for frame in frames], ["user", "control_request"])
        self.assertEqual(
            frames[1],
            {
                "type": "control_request",
                "request_id": admission["request_id"],
                "request": {"subtype": "interrupt"},
            },
        )
        acknowledged = next(
            event for event in events if event.kind == "CANCELLATION_ACKNOWLEDGED"
        )
        self.assertEqual(acknowledged.payload["request_id"], admission["request_id"])

        result = bridge.tool("forge_result", {"job_id": job_id})
        self.assertFalse(result["isError"], result)
        receipt_path = Path(
            result["structuredContent"]["artifacts"]["receipt"]["path"]
        )
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        cancellation = receipt["cancellation"]
        attempt = store.get_attempt(terminal["attempt_id"])
        self.assertEqual(receipt["terminal_status"], "CANCELLED")
        self.assertEqual(cancellation["request_id"], admission["request_id"])
        self.assertEqual(cancellation["provider_pgid"], attempt.provider_pgid)
        self.assertTrue(cancellation["control_acknowledged_at"])
        self.assertTrue(cancellation["term_sent_at"])
        self.assertTrue(cancellation["kill_sent_at"])
        self.assertTrue(cancellation["quiescent_at"])
        self.assertEqual(cancellation["non_zombie_descendants"], 0)
        self.assertEqual(receipt["event_hash"], events[-1].event_hash)
        self.assertEqual(
            store.validate_cancellation_receipt(job_id, cancellation),
            cancellation,
        )
        contradicted = dict(cancellation)
        contradicted["term_sent_at"] = cancellation["quiescent_at"]
        with self.assertRaises(StateError):
            store.validate_cancellation_receipt(job_id, contradicted)

    def test_production_runs_but_cancel_rejects_without_recursive_containment(self):
        production = BridgeHarness(
            self.environment,
            bridge_path=NO_CONTAINMENT_BRIDGE,
        )
        self.bridges.append(production)
        preflight = production.tool(
            "forge_preflight",
            {"cwd": str(self.repo)},
        )
        self.assertFalse(preflight["isError"], preflight)
        self.assertEqual(preflight["structuredContent"]["status"], "ready")
        self.assertEqual(
            preflight["structuredContent"]["containment"],
            {
                "capability": "recursive_provider_containment",
                "supported": False,
                "authority": None,
                "reason": "unsupported_recursive_containment",
                "cleanup_scope": "process-group-only",
            },
        )
        job_id, running = self.start_running(
            production,
            idempotency_key="production-containment-policy",
        )
        store = Store(self.state_path)
        events_before = store.list_events(job_id)
        stale = self.cancel(production, job_id, running["revision"] - 1)
        self.assertTrue(stale["isError"], stale)
        self.assertEqual(
            stale["structuredContent"]["error_type"],
            "RevisionConflict",
        )
        rejected = self.cancel(production, job_id, running["revision"])
        self.assertTrue(rejected["isError"], rejected)
        self.assertEqual(rejected["structuredContent"]["status"], "PAUSED_POLICY")
        self.assertEqual(
            rejected["structuredContent"]["reason"],
            "unsupported_recursive_containment",
        )
        current = production.tool("forge_status", {"job_id": job_id})[
            "structuredContent"
        ]
        self.assertEqual(current["status"], "RUNNING")
        self.assertEqual(current["revision"], running["revision"])
        self.assertEqual(store.list_events(job_id), events_before)
        descendant_pid = json.loads(
            self.descendant_log.read_text(encoding="utf-8").splitlines()[0]
        )["pid"]
        self.assertTrue(_process_exists(descendant_pid))
        frames = [
            json.loads(line)
            for line in self.stdin_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual([frame["type"] for frame in frames], ["user"])
        self.assertNotIn(
            "CANCELLATION_ACCEPTED",
            [event.kind for event in store.list_events(job_id)],
        )

    def test_cancel_rejects_stale_revision_without_interrupting(self):
        bridge = self.bridge()
        job_id, running = self.start_running(bridge, idempotency_key="cancel-stale")
        rejected = self.cancel(bridge, job_id, running["revision"] - 1)
        self.assertTrue(rejected["isError"], rejected)
        self.assertNotEqual(
            rejected["structuredContent"].get("status"),
            "not_implemented",
            rejected,
        )
        self.assertEqual(
            rejected["structuredContent"]["error_type"],
            "RevisionConflict",
        )
        current = bridge.tool("forge_status", {"job_id": job_id})["structuredContent"]
        self.assertEqual(current["status"], "RUNNING")
        self.assertEqual(Store(self.state_path).list_commands(job_id), [])
        cleanup = self.cancel(bridge, job_id, current["revision"])
        self.assertFalse(cleanup["isError"], cleanup)
        self.wait_status(bridge, job_id, {"CANCELLED"})

    def test_cancel_is_repository_owned(self):
        bridge = self.bridge()
        job_id, running = self.start_running(bridge, idempotency_key="cancel-owner")
        other = self.bridge(repository=self.other_repo)
        rejected = self.cancel(other, job_id, running["revision"])
        self.assertTrue(rejected["isError"], rejected)
        self.assertNotEqual(
            rejected["structuredContent"].get("status"),
            "not_implemented",
            rejected,
        )
        self.assertEqual(rejected["structuredContent"]["error_type"], "JobNotFound")
        current = bridge.tool("forge_status", {"job_id": job_id})["structuredContent"]
        cleanup = self.cancel(bridge, job_id, current["revision"])
        self.assertFalse(cleanup["isError"], cleanup)
        self.wait_status(bridge, job_id, {"CANCELLED"})

    def test_repeated_cancel_is_idempotent_and_delivered_once(self):
        bridge = self.bridge()
        job_id, running = self.start_running(bridge, idempotency_key="cancel-repeat")
        first = self.cancel(bridge, job_id, running["revision"])
        self.assertFalse(first["isError"], first)
        first_body = first["structuredContent"]
        original_revision_retry = self.cancel(
            bridge,
            job_id,
            running["revision"],
        )
        self.assertFalse(original_revision_retry["isError"], original_revision_retry)
        self.assertTrue(original_revision_retry["structuredContent"]["idempotent"])
        self.assertEqual(
            original_revision_retry["structuredContent"]["request_id"],
            first_body["request_id"],
        )
        unrelated_stale = self.cancel(bridge, job_id, 0)
        self.assertTrue(unrelated_stale["isError"], unrelated_stale)
        self.assertEqual(
            unrelated_stale["structuredContent"]["error_type"],
            "RevisionConflict",
        )
        second = self.cancel(bridge, job_id, first_body["revision"])
        self.assertFalse(second["isError"], second)
        self.assertTrue(second["structuredContent"]["idempotent"])
        self.assertEqual(second["structuredContent"]["request_id"], first_body["request_id"])
        terminal = self.wait_status(bridge, job_id, {"CANCELLED"})
        before = Store(self.state_path).get_attempt(terminal["attempt_id"])
        events_before = Store(self.state_path).list_events(job_id)
        third = self.cancel(bridge, job_id, terminal["revision"])
        self.assertFalse(third["isError"], third)
        self.assertTrue(third["structuredContent"]["idempotent"])
        after = Store(self.state_path).get_attempt(terminal["attempt_id"])
        self.assertEqual(after.terminal_receipt_hash, before.terminal_receipt_hash)
        self.assertEqual(Store(self.state_path).list_events(job_id), events_before)
        frames = [
            json.loads(line)
            for line in self.stdin_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(sum(frame["type"] == "control_request" for frame in frames), 1)

    def test_bridge_restart_does_not_duplicate_the_interrupt(self):
        first_bridge = self.bridge()
        job_id, running = self.start_running(
            first_bridge,
            idempotency_key="cancel-bridge-restart",
        )
        first_bridge.close()
        second_bridge = self.bridge()
        accepted = self.cancel(second_bridge, job_id, running["revision"])
        self.assertFalse(accepted["isError"], accepted)
        self.wait_status(second_bridge, job_id, {"CANCELLED"})
        frames = [
            json.loads(line)
            for line in self.stdin_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(sum(frame["type"] == "control_request" for frame in frames), 1)

    def test_already_terminal_requires_current_revision_and_never_mutates(self):
        self.write_control(
            control_window_seconds=0,
            spawn_descendant=False,
            emit_interrupt_ack=False,
        )
        bridge = self.bridge()
        started = bridge.tool(
            "forge_start",
            self.start_arguments(idempotency_key="cancel-terminal-race"),
        )
        job_id = started["structuredContent"]["job_id"]
        terminal = self.wait_status(bridge, job_id, {"SUCCEEDED"})
        store = Store(self.state_path)
        before_attempt = store.get_attempt(terminal["attempt_id"])
        before_events = store.list_events(job_id)
        stale = self.cancel(bridge, job_id, max(terminal["revision"] - 1, 0))
        self.assertTrue(stale["isError"], stale)
        self.assertEqual(stale["structuredContent"]["error_type"], "RevisionConflict")
        self.assertEqual(store.list_events(job_id), before_events)
        no_op = self.cancel(bridge, job_id, terminal["revision"])
        self.assertFalse(no_op["isError"], no_op)
        self.assertTrue(no_op["structuredContent"]["idempotent"])
        self.assertEqual(no_op["structuredContent"]["status"], "SUCCEEDED")
        after_attempt = store.get_attempt(terminal["attempt_id"])
        self.assertEqual(after_attempt.terminal_receipt_hash, before_attempt.terminal_receipt_hash)
        self.assertEqual(store.list_events(job_id), before_events)

    def test_running_completion_and_cancel_cas_leave_one_terminal_direction(self):
        store = Store(self.temp_path / "race-state" / "state.sqlite3")
        job = store.create_job(
            repo_id="race-repo",
            idempotency_key="cancel-cas-race",
            request_hash="hash",
            payload={},
        )
        claim = store.claim_dispatch(job.id, owner_pid=os.getpid())
        assert claim is not None
        store.record_worker_pid(job.id, os.getpid(), _dispatch_claim=claim)
        authority = store.claim_worker_authority(job.id, claim.token)
        self.assertIsNotNone(authority)
        current = store.get_job(job.id)
        store.transition(
            job.id,
            expected_revision=current.revision,
            new_status="PREFLIGHT",
        )
        current = store.get_job(job.id)
        store.transition(job.id, expected_revision=current.revision, new_status="RUNNING")
        running = store.get_job(job.id)
        self.assertTrue(hasattr(store, "request_cancellation"))

        barrier = threading.Barrier(2)

        def cancel():
            barrier.wait()
            return store.request_cancellation(
                job.id,
                expected_revision=running.revision,
                live_worker_identity=store.get_attempt(
                    running.current_attempt_id
                ).worker_identity,
            )

        def complete():
            barrier.wait()
            return store.transition(
                job.id,
                expected_revision=running.revision,
                new_status="SUCCEEDED",
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = [executor.submit(cancel), executor.submit(complete)]
            results = []
            for outcome in outcomes:
                try:
                    results.append(outcome.result(timeout=5))
                except RevisionConflict:
                    results.append("revision-conflict")
        final = store.get_job(job.id)
        self.assertIn(final.status, {"SUCCEEDED", "INTERRUPTING"})
        if final.status == "SUCCEEDED":
            self.assertEqual(store.list_commands(job.id), [])
        else:
            commands = store.list_commands(job.id)
            self.assertEqual([(command.kind, command.status) for command in commands], [("CANCEL", "QUEUED")])
            self.assertIn("revision-conflict", results)

    def test_process_cancellation_rejects_identity_mismatch_without_signalling(self):
        from forge_lib import processes

        self.assertTrue(hasattr(processes, "cancel_process_group"))

        process = subprocess.Popen(
            [sys.executable, "-c", "import signal; signal.pause()"],
            start_new_session=True,
        )
        def cleanup_process():
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

        self.addCleanup(cleanup_process)
        containment = self._containment()
        manifest_pin = self._admission_manifest_pin(containment, process.pid)
        with mock.patch.object(processes.os, "killpg", wraps=os.killpg) as killpg:
            with self.assertRaises(ProcessError):
                processes.cancel_process_group(
                    process,
                    expected_process_group_id=process.pid + 1,
                    containment=containment,
                    admission_manifest_pin=manifest_pin,
                    grace_seconds=0.01,
                )
        killpg.assert_not_called()
        self.assertIsNone(process.poll())

    def test_signal_stages_are_persisted_immediately_after_each_effect(self):
        from forge_lib import processes

        process = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-c",
                (
                    "import signal,time;"
                    "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                    "print('ready',flush=True);"
                    "time.sleep(30)"
                ),
            ],
            stdout=subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdout is not None
        self.assertEqual(process.stdout.readline(), b"ready\n")

        def cleanup_process():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
            process.stdout.close()

        self.addCleanup(cleanup_process)
        stages = []
        waits = 0
        containment = self._containment()
        manifest_pin = self._admission_manifest_pin(containment, process.pid)

        def observe_wait(_process_group_id, _timeout_seconds):
            nonlocal waits
            waits += 1
            expected = ["TERM_SENT"] if waits == 1 else ["TERM_SENT", "KILL_SENT"]
            self.assertEqual([stage for stage, _timestamp in stages], expected)
            return waits == 2

        with mock.patch.object(
            processes,
            "_wait_for_cancellation_quiescence",
            side_effect=observe_wait,
        ):
            processes.cancel_process_group(
                process,
                expected_process_group_id=process.pid,
                containment=containment,
                admission_manifest_pin=manifest_pin,
                grace_seconds=0,
                stage_callback=lambda stage, timestamp: stages.append(
                    (stage, timestamp)
                ),
            )
        process.wait(timeout=2)
        self.assertEqual(
            [stage for stage, _timestamp in stages],
            ["TERM_SENT", "KILL_SENT", "QUIESCENT"],
        )

    def test_leader_exit_after_identity_check_uses_no_signal_proof(self):
        from forge_lib import processes

        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        self.addCleanup(
            lambda: process.kill() if process.poll() is None else None
        )
        containment = self._containment()
        manifest_pin = self._admission_manifest_pin(containment, process.pid)
        original_assert = processes._assert_cancellation_group_identity

        def exit_after_identity(*args, **kwargs):
            scope = original_assert(*args, **kwargs)
            os.kill(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
            return scope

        stages = []
        with (
            mock.patch.object(
                processes,
                "_assert_cancellation_group_identity",
                side_effect=exit_after_identity,
            ),
            self.assertRaises(processes.ProviderLeaderExitedBeforeSignal),
        ):
            processes.cancel_process_group(
                process,
                expected_process_group_id=process.pid,
                containment=containment,
                admission_manifest_pin=manifest_pin,
                stage_callback=lambda stage, timestamp: stages.append(
                    (stage, timestamp)
                ),
            )
        self.assertEqual(stages, [])

        evidence = processes.prove_provider_exit_after_cancellation_ack(
            process,
            expected_process_group_id=process.pid,
            containment=containment,
            admission_manifest_pin=manifest_pin,
            stage_callback=lambda stage, timestamp: stages.append(
                (stage, timestamp)
            ),
        )
        self.assertEqual(evidence.termination_mode, "provider_exit_after_ack")
        self.assertEqual(
            [stage for stage, _timestamp in stages],
            ["PROVIDER_EXITED", "QUIESCENT"],
        )

    def test_signal_quiescence_resamples_descendant_exit_during_identity_check(self):
        from forge_lib import processes

        child_path = self.temp_path / "signal-resample-child.pid"
        child_ready = self.temp_path / "signal-resample-child.ready"
        child_code = (
            "import pathlib,signal,time;"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            f"pathlib.Path({str(child_ready)!r}).write_text('ready');"
            "time.sleep(30)"
        )
        leader_code = (
            "import pathlib,subprocess,sys,time;"
            f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
            f"pathlib.Path({str(child_path)!r}).write_text(str(child.pid));"
            "time.sleep(30)"
        )
        leader = subprocess.Popen(
            [sys.executable, "-u", "-c", leader_code],
            start_new_session=True,
        )
        deadline = time.monotonic() + 2
        while (
            (not child_path.exists() or not child_ready.exists())
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        child_pid = int(child_path.read_text(encoding="utf-8"))

        def cleanup_tree():
            for pid in (child_pid, leader.pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            leader.wait(timeout=2)

        self.addCleanup(cleanup_tree)
        containment = self._containment(child_pid)
        manifest_pin = self._admission_manifest_pin(containment, leader.pid)
        original_identity = processes.process_instance_identity
        exited = False

        def exit_during_identity(pid):
            nonlocal exited
            if pid == child_pid and not exited:
                exited = True
                os.kill(child_pid, signal.SIGKILL)
                raise ProcessError("injected descendant exit race")
            return original_identity(pid)

        with mock.patch.object(
            processes,
            "process_instance_identity",
            side_effect=exit_during_identity,
        ):
            evidence = processes.cancel_process_group(
                leader,
                expected_process_group_id=leader.pid,
                containment=containment,
                admission_manifest_pin=manifest_pin,
                grace_seconds=0.1,
            )
        self.assertTrue(exited)
        self.assertEqual(evidence.non_zombie_descendants, 0)

    def test_no_signal_quiescence_resamples_descendant_exit_during_identity_check(self):
        from forge_lib import processes

        child_path = self.temp_path / "exit-resample-child.pid"
        leader_code = (
            "import os,pathlib,time;"
            "child=os.fork();"
            "\nif child == 0:\n time.sleep(30);os._exit(0)\n"
            f"pathlib.Path({str(child_path)!r}).write_text(str(child))"
        )
        leader = subprocess.Popen(
            [sys.executable, "-u", "-c", leader_code],
            start_new_session=True,
        )
        leader.wait(timeout=2)
        child_pid = int(child_path.read_text(encoding="utf-8"))

        def cleanup_child():
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        self.addCleanup(cleanup_child)
        containment = self._containment(child_pid)
        _manifest, manifest_pin = processes._read_test_descendant_manifest(
            containment
        )
        original_identity = processes.process_instance_identity
        exited = False

        def exit_during_identity(pid):
            nonlocal exited
            if pid == child_pid and not exited:
                exited = True
                os.kill(child_pid, signal.SIGKILL)
                raise ProcessError("injected descendant exit race")
            return original_identity(pid)

        with mock.patch.object(
            processes,
            "process_instance_identity",
            side_effect=exit_during_identity,
        ):
            evidence = processes.prove_provider_exit_after_cancellation_ack(
                leader,
                expected_process_group_id=leader.pid,
                containment=containment,
                admission_manifest_pin=manifest_pin,
            )
        self.assertTrue(exited)
        self.assertEqual(evidence.termination_mode, "provider_exit_after_ack")
        self.assertEqual(evidence.non_zombie_descendants, 0)

    def test_process_cancellation_rejects_an_escaped_descendant_before_signalling(self):
        from forge_lib import processes

        child_path = self.temp_path / "escaped-child.pid"
        leader_code = (
            "import pathlib,signal,subprocess,sys,time;"
            "child=subprocess.Popen([sys.executable,'-c',"
            "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)'],"
            "start_new_session=True);"
            f"pathlib.Path({str(child_path)!r}).write_text(str(child.pid));"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)"
        )
        leader = subprocess.Popen(
            [sys.executable, "-u", "-c", leader_code],
            start_new_session=True,
        )
        deadline = time.monotonic() + 2
        while not child_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        child_pid = int(child_path.read_text(encoding="utf-8"))

        def cleanup_tree():
            for pid in (child_pid, leader.pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            leader.wait(timeout=2)

        self.addCleanup(cleanup_tree)
        containment = self._containment(child_pid)
        _manifest, manifest_pin = processes._read_test_descendant_manifest(
            containment
        )
        with mock.patch.object(processes.os, "killpg", wraps=os.killpg) as killpg:
            with self.assertRaisesRegex(ProcessError, "escaped"):
                processes.cancel_process_group(
                    leader,
                    expected_process_group_id=leader.pid,
                    containment=containment,
                    admission_manifest_pin=manifest_pin,
                    grace_seconds=0.01,
                )
        killpg.assert_not_called()
        self.assertTrue(_process_exists(child_pid))

    def test_environment_marker_cannot_enable_recursive_containment(self):
        from forge_lib import processes

        marker = "1" * 64
        scoped_environment = dict(os.environ)
        scoped_environment["FORGE_PROCESS_SCOPE"] = marker
        daemon_path = self.temp_path / "reparented-daemon.pid"
        ready_path = self.temp_path / "reparented-ready"
        helper_code = (
            "import os,pathlib,signal,time;"
            "daemon=os.fork();"
            "\nif daemon == 0:\n os.setsid();signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30);os._exit(0)\n"
            f"pathlib.Path({str(daemon_path)!r}).write_text(str(daemon))"
        )
        leader_code = (
            "import os,pathlib,time;"
            "helper=os.fork();"
            f"\nif helper == 0:\n exec({helper_code!r});os._exit(0)\n"
            "os.waitpid(helper,0);"
            f"pathlib.Path({str(ready_path)!r}).write_text('ready');"
            "time.sleep(30)"
        )
        leader = subprocess.Popen(
            [sys.executable, "-u", "-c", leader_code],
            env=scoped_environment,
            start_new_session=True,
        )
        setattr(leader, "_forge_process_scope", marker)
        deadline = time.monotonic() + 3
        while not ready_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        daemon_pid = int(daemon_path.read_text(encoding="utf-8"))

        def cleanup_tree():
            for pid in (daemon_pid, leader.pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            leader.wait(timeout=2)

        self.addCleanup(cleanup_tree)
        with mock.patch.object(processes.os, "killpg", wraps=os.killpg) as killpg:
            with self.assertRaisesRegex(ProcessError, "unsupported"):
                processes.cancel_process_group(
                    leader,
                    expected_process_group_id=leader.pid,
                    grace_seconds=0.01,
                )
        killpg.assert_not_called()
        self.assertTrue(_process_exists(daemon_pid))

    def test_cancellation_reservation_survives_interrupting_and_counts_ack_frame(self):
        def running_store(name):
            store = Store(self.temp_path / name / "state.sqlite3")
            job = store.create_job(
                repo_id="capacity-repo",
                idempotency_key=name,
                request_hash="hash",
                payload={},
            )
            claim = store.claim_dispatch(job.id, owner_pid=os.getpid())
            assert claim is not None
            store.record_worker_pid(job.id, os.getpid(), _dispatch_claim=claim)
            authority = store.claim_worker_authority(job.id, claim.token)
            self.assertIsNotNone(authority)
            current = store.get_job(job.id)
            store.transition(
                job.id,
                expected_revision=current.revision,
                new_status="PREFLIGHT",
            )
            current = store.get_job(job.id)
            store.transition(
                job.id,
                expected_revision=current.revision,
                new_status="RUNNING",
            )
            return store, store.get_job(job.id), claim, authority

        too_full, job, _claim, _authority = running_store("capacity-off-by-one")
        for index in range(MAX_ATTEMPT_EVENTS - 8):
            too_full.append_event(job.id, "CAPACITY", {"index": index})
        current = too_full.get_job(job.id)
        with self.assertRaises(StateError):
            too_full.request_cancellation(
                job.id,
                expected_revision=current.revision,
                live_worker_identity=too_full.get_attempt(
                    current.current_attempt_id
                ).worker_identity,
            )

        store, job, claim, _authority = running_store("capacity-reserved")
        for index in range(MAX_ATTEMPT_EVENTS - 9):
            store.append_event(job.id, "CAPACITY", {"index": index})
        current = store.get_job(job.id)
        admission = store.request_cancellation(
            job.id,
            expected_revision=current.revision,
            live_worker_identity=store.get_attempt(
                current.current_attempt_id
            ).worker_identity,
        )
        self.assertEqual(admission.status, "INTERRUPTING")
        with self.assertRaises(StateError):
            store.append_event(
                job.id,
                "CLAUDE_EVENT",
                {"type": "assistant", "data": {}},
            )
        claimed = store.claim_next_command(
            job.id,
            dispatch_token=claim.token,
            worker_pid=os.getpid(),
        )
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.kind, "CANCEL")

    def test_generic_transition_cannot_publish_cancelled_without_evidence(self):
        store = Store(self.temp_path / "synthetic-cancel" / "state.sqlite3")
        job = store.create_job(
            repo_id="synthetic-repo",
            idempotency_key="synthetic",
            request_hash="hash",
            payload={},
        )
        claim = store.claim_dispatch(job.id, owner_pid=os.getpid())
        assert claim is not None
        store.record_worker_pid(job.id, os.getpid(), _dispatch_claim=claim)
        authority = store.claim_worker_authority(job.id, claim.token)
        self.assertIsNotNone(authority)
        current = store.get_job(job.id)
        store.transition(job.id, expected_revision=current.revision, new_status="PREFLIGHT")
        current = store.get_job(job.id)
        store.transition(job.id, expected_revision=current.revision, new_status="RUNNING")
        current = store.get_job(job.id)
        store.request_cancellation(
            job.id,
            expected_revision=current.revision,
            live_worker_identity=store.get_attempt(
                current.current_attempt_id
            ).worker_identity,
        )
        store.close_cancellation_inbox(
            job.id,
            _worker_authority=authority,
            require_quiescence=False,
        )
        current = store.get_job(job.id)
        with self.assertRaises(InvalidTransition):
            store.transition(
                job.id,
                expected_revision=current.revision,
                new_status="CANCELLED",
            )

    def test_mismatched_interrupt_ack_fails_closed_without_unreceipted_signals(self):
        self.write_control(interrupt_ack_request_id="not-the-request")
        bridge = self.bridge()
        job_id, running = self.start_running(
            bridge,
            idempotency_key="cancel-bad-ack",
        )
        descendant_pid = json.loads(
            self.descendant_log.read_text(encoding="utf-8").splitlines()[0]
        )["pid"]
        accepted = self.cancel(bridge, job_id, running["revision"])
        self.assertFalse(accepted["isError"], accepted)
        deadline = time.monotonic() + 4
        kinds = []
        current = None
        while time.monotonic() < deadline:
            kinds = [event.kind for event in Store(self.state_path).list_events(job_id)]
            current = bridge.tool(
                "forge_status",
                {"job_id": job_id},
            )["structuredContent"]
            attempt = Store(self.state_path).get_attempt(current["attempt_id"])
            if "CANCELLATION_ACK_FAILED" in kinds:
                current = bridge.tool("forge_status", {"job_id": job_id})[
                    "structuredContent"
                ]
                break
            time.sleep(0.01)
        assert current is not None
        self.assertEqual(current["status"], "RUNNING")
        self.assertTrue(_process_exists(descendant_pid))
        self.assertIn("CANCELLATION_ACK_FAILED", kinds)
        self.assertNotIn("CANCELLATION_ACKNOWLEDGED", kinds)
        self.assertNotIn("CANCELLATION_TERM_SENT", kinds)
        self.assertNotIn("CANCELLATION_KILL_SENT", kinds)
        self.assertNotIn("CANCELLATION_QUIESCENT", kinds)

    def test_mismatched_ack_does_not_discard_authority_before_exact_ack(self):
        self.write_control(emit_mismatched_interrupt_ack_before_exact=True)
        bridge = self.bridge()
        job_id, running = self.start_running(
            bridge,
            idempotency_key="cancel-bad-then-exact-ack",
        )
        accepted = self.cancel(bridge, job_id, running["revision"])
        self.assertFalse(accepted["isError"], accepted)

        terminal = self.wait_status(bridge, job_id, {"CANCELLED"})
        self.assertEqual(terminal["status"], "CANCELLED")
        events = Store(self.state_path).list_events(job_id)
        control_responses = [
            event
            for event in events
            if event.kind == "CLAUDE_EVENT"
            and event.payload.get("type") == "control_response"
        ]
        self.assertEqual(len(control_responses), 2)
        self.assertEqual(
            sum(event.kind == "CANCELLATION_ACKNOWLEDGED" for event in events),
            1,
        )

    def test_provider_eof_without_exact_ack_never_reaps_into_generic_teardown(self):
        self.write_control(
            emit_interrupt_ack=False,
            wait_for_stdin_eof_after_result=False,
        )
        bridge = self.bridge()
        job_id, running = self.start_running(
            bridge,
            idempotency_key="cancel-no-ack-eof",
        )
        descendant_pid = json.loads(
            self.descendant_log.read_text(encoding="utf-8").splitlines()[0]
        )["pid"]
        accepted = self.cancel(bridge, job_id, running["revision"])
        self.assertFalse(accepted["isError"], accepted)

        deadline = time.monotonic() + 4
        attempt = None
        kinds = []
        while time.monotonic() < deadline:
            current = bridge.tool("forge_status", {"job_id": job_id})[
                "structuredContent"
            ]
            attempt = Store(self.state_path).get_attempt(current["attempt_id"])
            kinds = [
                event.kind for event in Store(self.state_path).list_events(job_id)
            ]
            if "CANCELLATION_ACK_FAILED" in kinds:
                current = bridge.tool("forge_status", {"job_id": job_id})[
                    "structuredContent"
                ]
                attempt = Store(self.state_path).get_attempt(current["attempt_id"])
                break
            time.sleep(0.01)
        assert attempt is not None
        self.assertEqual(current["status"], "RUNNING")
        self.assertIsNotNone(attempt.worker_pid)
        self.assertTrue(_non_zombie_process_exists(attempt.worker_pid))
        self.assertTrue(_process_exists(descendant_pid))
        self.assertIn("CANCELLATION_ACK_FAILED", kinds)
        self.assertNotIn("CANCELLATION_ACKNOWLEDGED", kinds)
        self.assertNotIn("CANCELLATION_TERM_SENT", kinds)
        self.assertNotIn("CANCELLATION_KILL_SENT", kinds)
        self.assertNotIn("CANCELLATION_QUIESCENT", kinds)

    def test_eof_fences_cancel_admission_before_provider_wait_cleanup(self):
        from forge_lib import processes, worker as worker_module

        store = Store(self.temp_path / "eof-fence" / "state.sqlite3")
        job = store.create_job(
            repo_id="eof-fence-repo",
            idempotency_key="eof-fence",
            request_hash="hash",
            payload={},
        )
        claim = store.claim_dispatch(job.id, owner_pid=os.getpid())
        assert claim is not None
        store.record_worker_pid(job.id, os.getpid(), _dispatch_claim=claim)
        authority = store.claim_worker_authority(job.id, claim.token)
        self.assertIsNotNone(authority)
        current = store.get_job(job.id)
        store.transition(
            job.id,
            expected_revision=current.revision,
            new_status="PREFLIGHT",
        )
        current = store.get_job(job.id)
        store.transition(
            job.id,
            expected_revision=current.revision,
            new_status="RUNNING",
        )
        provider = processes._ProviderProcess(
            [sys.executable, "-c", "pass"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

        def cleanup_provider():
            if provider.poll() is None:
                provider.wait(timeout=2)
            for stream in (provider.stdin, provider.stdout, provider.stderr):
                if stream is not None and not stream.closed:
                    stream.close()

        self.addCleanup(cleanup_provider)
        events_path = self.temp_path / "eof-fence-events.jsonl"
        stderr_path = self.temp_path / "eof-fence-stderr.log"
        events_descriptor = os.open(
            events_path,
            os.O_RDWR | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        stderr_descriptor = os.open(
            stderr_path,
            os.O_RDWR | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        self.addCleanup(os.close, stderr_descriptor)
        self.addCleanup(os.close, events_descriptor)
        original_wait = provider.wait
        admissions = []

        def admit_at_wait(*, timeout=None):
            current_job = store.get_job(job.id)
            admissions.append(
                store.request_cancellation(
                    job.id,
                    expected_revision=current_job.revision,
                    live_worker_identity=store.get_attempt(
                        current_job.current_attempt_id
                    ).worker_identity,
                )
            )
            return original_wait(timeout=timeout)

        with (
            mock.patch.object(provider, "wait", side_effect=admit_at_wait),
            mock.patch.object(
                processes,
                "terminate_process_group",
                return_value=0,
            ),
        ):
            worker_module._collect_provider(
                provider,
                store=store,
                job_id=job.id,
                session_id="eof-fence-session",
                dispatch_token=claim.token,
                worker_pid=os.getpid(),
                events_descriptor=events_descriptor,
                stderr_descriptor=stderr_descriptor,
                deadline=time.monotonic() + 5,
            )
        self.assertEqual(len(admissions), 1)
        self.assertEqual(admissions[0].outcome, "natural_completion_won")
        self.assertEqual(store.get_job(job.id).status, "RUNNING")
        self.assertEqual(store.list_commands(job.id), [])

    def test_exception_cleanup_fence_lets_admitted_cancel_veto_generic_signal(self):
        from forge_lib import processes, worker as worker_module

        service = ForgeService(
            state_path=self.temp_path / "cleanup-fence" / "state.sqlite3",
            launch_workspace=self.repo,
            claude_executable=str(FAKE_CLAUDE),
            bridge_path=BRIDGE,
            environment=self.environment,
            cancellation_containment=self._containment(),
            _admission_machine_probe=machine_headroom,
        )
        job = self.create_admitted_job(
            service,
            self.start_arguments(idempotency_key="cleanup-fence"),
        )
        claim = service.store.claim_dispatch(job.id, owner_pid=os.getpid())
        assert claim is not None
        service.store.record_worker_pid(
            job.id,
            os.getpid(),
            _dispatch_claim=claim,
        )
        provider = mock.Mock(pid=424242, stdin=object())
        worker_birth = service.store.get_attempt(job.current_attempt_id).worker_identity
        assert worker_birth is not None

        def fake_process_identity(process_id):
            if process_id == provider.pid:
                return "a" * 64
            if process_id == os.getpid():
                return worker_birth
            raise AssertionError(f"unexpected process identity lookup: {process_id}")

        original_close = Store.close_command_inbox
        admitted = []

        def spawn(*_args, **kwargs):
            kwargs["before_popen"]()
            return provider

        def close_after_cancel(store_instance, job_id, **kwargs):
            if not admitted:
                current = store_instance.get_job(job_id)
                admitted.append(
                    store_instance.request_cancellation(
                        job_id,
                        expected_revision=current.revision,
                        live_worker_identity=store_instance.get_attempt(
                            current.current_attempt_id
                        ).worker_identity,
                    )
                )
            return original_close(store_instance, job_id, **kwargs)

        with (
            mock.patch.dict(os.environ, self.environment, clear=True),
            mock.patch.object(worker_module, "spawn_provider", side_effect=spawn),
            mock.patch.object(
                worker_module,
                "process_instance_identity",
                side_effect=fake_process_identity,
            ),
            mock.patch(
                "forge_lib.state.process_instance_identity",
                side_effect=fake_process_identity,
            ),
            mock.patch.object(worker_module, "_write_provider_frame"),
            mock.patch.object(
                worker_module,
                "_collect_provider",
                side_effect=worker_module.WorkerError("forced collection failure"),
            ),
            mock.patch.object(
                Store,
                "close_command_inbox",
                autospec=True,
                side_effect=close_after_cancel,
            ),
            mock.patch.object(worker_module, "terminate_process_group") as terminate,
        ):
            result = worker_module.run_worker(
                job.id,
                dispatch_token=claim.token,
                state_path=service.store.path,
                dispatch_role=service.store.get_job(job.id).payload["role"],
                cancellation_containment=(
                    self._containment()
                ),
            )
        self.assertEqual(result, 1)
        self.assertEqual(len(admitted), 1)
        self.assertEqual(admitted[0].outcome, "accepted")
        self.assertEqual(service.store.get_job(job.id).status, "INTERRUPTING")
        terminate.assert_not_called()

    def test_interrupt_acknowledgement_has_a_cancellation_specific_deadline(self):
        self.write_control(interrupt_ack_delay_seconds=2.5)
        bridge = self.bridge()
        job_id, running = self.start_running(
            bridge,
            idempotency_key="cancel-ack-deadline",
            budget={
                "max_seconds": 10,
                "max_tokens": 4096,
                "max_turns": 4096,
                "max_cost_usd": "1.25",
            },
        )
        descendant_pid = json.loads(
            self.descendant_log.read_text(encoding="utf-8").splitlines()[0]
        )["pid"]
        started_at = time.monotonic()
        accepted = self.cancel(bridge, job_id, running["revision"])
        self.assertFalse(accepted["isError"], accepted)

        deadline = time.monotonic() + 4
        attempt = None
        kinds = []
        while time.monotonic() < deadline:
            current = bridge.tool("forge_status", {"job_id": job_id})[
                "structuredContent"
            ]
            attempt = Store(self.state_path).get_attempt(current["attempt_id"])
            kinds = [
                event.kind for event in Store(self.state_path).list_events(job_id)
            ]
            if "CANCELLATION_ACK_FAILED" in kinds:
                break
            time.sleep(0.01)
        assert attempt is not None
        current = bridge.tool("forge_status", {"job_id": job_id})[
            "structuredContent"
        ]
        self.assertLess(time.monotonic() - started_at, 2.25)
        self.assertEqual(current["status"], "RUNNING")
        self.assertIsNotNone(attempt.worker_pid)
        self.assertTrue(_non_zombie_process_exists(attempt.worker_pid))
        self.assertTrue(_process_exists(descendant_pid))
        self.assertIn("CANCELLATION_ACK_FAILED", kinds)
        self.assertNotIn("CANCELLATION_ACKNOWLEDGED", kinds)
        self.assertNotIn("CANCELLATION_TERM_SENT", kinds)
        self.assertNotIn("CANCELLATION_KILL_SENT", kinds)
        self.assertNotIn("CANCELLATION_QUIESCENT", kinds)

    def test_ack_deadline_is_rechecked_after_acquiring_the_sqlite_write_fence(self):
        owner, job, _claim, authority, command = (
            self._prepare_delivered_cancellation("ack-write-lock-deadline")
        )
        request_id = command.payload["request_id"]
        lock = sqlite3.connect(
            str(owner.path),
            timeout=5,
            isolation_level=None,
        )
        lock.execute("BEGIN IMMEDIATE")
        started = threading.Event()

        def acknowledge_after_lock():
            started.set()
            return owner.acknowledge_cancellation(
                job.id,
                request_id=request_id,
                acknowledged_at="2026-08-26T12:00:00.000000+00:00",
                acknowledgement_deadline=time.monotonic() + 0.1,
                _worker_authority=authority,
            )

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(acknowledge_after_lock)
                self.assertTrue(started.wait(timeout=1))
                time.sleep(0.2)
                self.assertFalse(
                    future.done(),
                    "acknowledgement bypassed the held SQLite writer fence",
                )
                lock.rollback()
                with self.assertRaisesRegex(StateError, "deadline"):
                    future.result(timeout=2)
        finally:
            try:
                lock.rollback()
            except sqlite3.OperationalError:
                pass
            lock.close()

        persisted = owner.list_commands(job.id)
        self.assertEqual(persisted[-1].status, "DELIVERED")
        self.assertNotIn(
            "CANCELLATION_ACKNOWLEDGED",
            [event.kind for event in owner.list_events(job.id)],
        )

    def test_ack_failure_and_natural_result_inbox_closure_are_atomic(self):
        owner, job, _claim, authority, command = (
            self._prepare_delivered_cancellation("ack-failure-natural-result")
        )
        rollback = owner.fail_cancellation_acknowledgement(
            job.id,
            request_id=command.payload["request_id"],
            reason="deadline_exceeded",
            close_inbox_for_terminal_result=True,
            _worker_authority=authority,
        )

        self.assertTrue(rollback.rolled_back)
        self.assertTrue(rollback.inbox_closed)
        self.assertEqual(rollback.failed_commands, ())
        resumed = owner.get_job(job.id)
        attempt = owner.get_attempt(resumed.current_attempt_id)
        self.assertEqual(resumed.status, "RUNNING")
        self.assertEqual(attempt.command_inbox_state, "CLOSED")

        raced = Store(owner.path).request_cancellation(
            job.id,
            expected_revision=resumed.revision,
            live_worker_identity=attempt.worker_identity,
        )
        self.assertEqual(raced.outcome, "natural_completion_won")
        self.assertIsNone(raced.command)
        self.assertEqual(len(owner.list_commands(job.id)), 1)

    def test_unsent_cancel_frame_rolls_back_before_provider_cleanup(self):
        from forge_lib import worker as worker_module

        owner, job, claim, authority, _command = self._prepare_claimed_cancellation(
            "cancel-frame-unsent",
            claim_command=False,
        )
        read_descriptor, write_descriptor = os.pipe()
        os.close(read_descriptor)
        stream = os.fdopen(write_descriptor, "wb", buffering=0)
        provider = mock.Mock(stdin=stream)
        try:
            with self.assertRaises(worker_module.WorkerError):
                worker_module._deliver_queued_commands(
                    provider,
                    store=owner,
                    job_id=job.id,
                    dispatch_token=claim.token,
                    worker_pid=os.getpid(),
                    deadline=time.monotonic() + 1,
                    worker_authority=authority,
                )
        finally:
            stream.close()

        resumed = owner.get_job(job.id)
        attempt = owner.get_attempt(resumed.current_attempt_id)
        self.assertEqual(resumed.status, "RUNNING")
        self.assertEqual(owner.list_commands(job.id)[0].status, "DELIVERY_FAILED")
        failures = [
            event
            for event in owner.list_events(job.id)
            if event.kind == "COMMAND_DELIVERY_FAILED"
        ]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].payload["reason"], "frame_not_written")
        self.assertFalse(
            any(
                event.kind.startswith("CANCELLATION_")
                and event.kind != "CANCELLATION_ACCEPTED"
                for event in owner.list_events(job.id)
            )
        )
        retried = owner.request_cancellation(
            job.id,
            expected_revision=resumed.revision,
            live_worker_identity=attempt.worker_identity,
        )
        self.assertEqual(retried.outcome, "accepted")

    def test_partial_cancel_frame_is_never_rewritten_and_rolls_back(self):
        from forge_lib import worker as worker_module

        owner, job, claim, authority, _command = self._prepare_claimed_cancellation(
            "cancel-frame-partial",
            claim_command=False,
        )
        read_descriptor, write_descriptor = os.pipe()
        stream = os.fdopen(write_descriptor, "wb", buffering=0)
        provider = mock.Mock(stdin=stream)
        actual_write = os.write
        writes = 0

        def partial_then_break(descriptor, data):
            nonlocal writes
            writes += 1
            if writes == 1:
                return actual_write(descriptor, bytes(data[:7]))
            raise BrokenPipeError("injected partial control frame")

        try:
            with (
                mock.patch.object(worker_module.os, "write", side_effect=partial_then_break),
                self.assertRaises(worker_module.WorkerError),
            ):
                worker_module._deliver_queued_commands(
                    provider,
                    store=owner,
                    job_id=job.id,
                    dispatch_token=claim.token,
                    worker_pid=os.getpid(),
                    deadline=time.monotonic() + 1,
                    worker_authority=authority,
                )
            prefix = actual_write  # keep the unpatched callable live through the read
            del prefix
            received = os.read(read_descriptor, 4096)
        finally:
            stream.close()
            os.close(read_descriptor)

        self.assertEqual(writes, 2)
        self.assertEqual(len(received), 7)
        self.assertNotIn(b"\n", received)
        self.assertEqual(owner.get_job(job.id).status, "RUNNING")
        self.assertEqual(owner.list_commands(job.id)[0].status, "DELIVERY_FAILED")
        failure = next(
            event
            for event in owner.list_events(job.id)
            if event.kind == "COMMAND_DELIVERY_FAILED"
        )
        self.assertEqual(
            failure.payload["reason"],
            "partial_frame_channel_poisoned",
        )

    def test_complete_cancel_frame_write_exception_is_receipted_without_rewrite(self):
        from forge_lib import worker as worker_module

        owner, job, claim, authority, command = self._prepare_claimed_cancellation(
            "cancel-frame-complete-exception",
            claim_command=False,
        )
        read_descriptor, write_descriptor = os.pipe()
        stream = os.fdopen(write_descriptor, "wb", buffering=0)
        provider = mock.Mock(stdin=stream)

        def write_complete_then_raise(target_provider, frame, *, deadline):
            del deadline
            written = os.write(target_provider.stdin.fileno(), frame)
            self.assertEqual(written, len(frame))
            raise worker_module._ProviderFrameWriteError(
                "injected post-full-write failure",
                bytes_written=written,
                frame_sent_at=17.25,
            )

        try:
            with (
                mock.patch.object(
                    worker_module,
                    "_write_provider_frame",
                    side_effect=write_complete_then_raise,
                ),
                mock.patch.object(worker_module.time, "monotonic", return_value=99.0),
            ):
                delivery = worker_module._deliver_queued_commands(
                    provider,
                    store=owner,
                    job_id=job.id,
                    dispatch_token=claim.token,
                    worker_pid=os.getpid(),
                    deadline=time.monotonic() + 1,
                    worker_authority=authority,
                )
            assert delivery is not None
            written = os.read(read_descriptor, 4096)
        finally:
            stream.close()
            os.close(read_descriptor)

        self.assertFalse(delivery.receipt_pending)
        self.assertEqual(delivery.frame_sent_at, 17.25)
        self.assertEqual(owner.get_job(job.id).status, "INTERRUPTING")
        self.assertEqual(owner.list_commands(job.id)[0].status, "DELIVERED")
        frames = written.decode("utf-8").splitlines()
        self.assertEqual(len(frames), 1)
        self.assertEqual(
            json.loads(frames[0])["request_id"],
            command.payload["request_id"],
        )
        self.assertNotIn(
            "COMMAND_DELIVERY_FAILED",
            [event.kind for event in owner.list_events(job.id)],
        )

    def test_complete_cancel_frame_receipt_retry_never_rewrites_the_frame(self):
        from forge_lib import worker as worker_module

        owner, job, claim, authority, command = self._prepare_claimed_cancellation(
            "cancel-frame-confirm-retry",
            claim_command=False,
        )
        read_descriptor, write_descriptor = os.pipe()
        stream = os.fdopen(write_descriptor, "wb", buffering=0)
        provider = mock.Mock(stdin=stream)
        try:
            with mock.patch.object(
                owner,
                "confirm_cancellation_delivery",
                side_effect=StateError("injected receipt failure"),
            ):
                delivery = worker_module._deliver_queued_commands(
                    provider,
                    store=owner,
                    job_id=job.id,
                    dispatch_token=claim.token,
                    worker_pid=os.getpid(),
                    deadline=time.monotonic() + 1,
                    worker_authority=authority,
                )
            assert delivery is not None
            self.assertTrue(delivery.receipt_pending)
            written = os.read(read_descriptor, 4096)
        finally:
            stream.close()
            os.close(read_descriptor)

        frames = written.decode("utf-8").splitlines()
        self.assertEqual(len(frames), 1)
        frame = json.loads(frames[0])
        self.assertEqual(frame["request_id"], command.payload["request_id"])
        owner.confirm_cancellation_delivery(
            job.id,
            command.id,
            _worker_authority=authority,
        )
        control_event = {
            "type": "control_response",
            "subtype": None,
            "model": None,
            "parent_tool_use_id": None,
            "data": {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": command.payload["request_id"],
                    "response": {},
                },
            },
        }
        owner.acknowledge_cancellation_control_event(
            job.id,
            request_id=command.payload["request_id"],
            control_event=control_event,
            acknowledged_at="2026-08-26T12:00:00.000000+00:00",
            acknowledgement_deadline=time.monotonic() + 2,
            _worker_authority=authority,
        )
        events = owner.list_events(job.id)
        self.assertEqual(sum(event.kind == "COMMAND_DELIVERED" for event in events), 1)
        self.assertEqual(sum(event.kind == "CLAUDE_EVENT" for event in events), 1)
        self.assertEqual(
            sum(event.kind == "CANCELLATION_ACKNOWLEDGED" for event in events),
            1,
        )

    def test_unconfirmed_written_cancel_times_out_without_signalling_provider(self):
        from forge_lib import worker as worker_module

        owner, job, claim, authority, command = self._prepare_claimed_cancellation(
            "cancel-frame-confirm-timeout",
            claim_command=False,
        )
        provider = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys,time; sys.stdin.buffer.readline(); time.sleep(30)",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        def cleanup_provider():
            if provider.poll() is None:
                provider.kill()
            provider.wait(timeout=2)
            for stream in (provider.stdin, provider.stdout, provider.stderr):
                if stream is not None and not stream.closed:
                    stream.close()

        self.addCleanup(cleanup_provider)
        with mock.patch.object(
            owner,
            "confirm_cancellation_delivery",
            side_effect=StateError("injected durable receipt outage"),
        ):
            delivery = worker_module._deliver_queued_commands(
                provider,
                store=owner,
                job_id=job.id,
                dispatch_token=claim.token,
                worker_pid=os.getpid(),
                deadline=time.monotonic() + 1,
                worker_authority=authority,
            )
        assert delivery is not None
        self.assertTrue(delivery.receipt_pending)
        owner.append_event(
            job.id,
            "CLAUDE_EVENT",
            {
                "type": "control_response",
                "subtype": None,
                "model": None,
                "parent_tool_use_id": None,
                "data": {
                    "type": "control_response",
                    "response": {
                        "subtype": "error",
                        "request_id": command.payload["request_id"],
                        "response": {},
                    },
                },
            },
        )
        rollback = owner.fail_cancellation_acknowledgement(
            job.id,
            request_id=command.payload["request_id"],
            reason="deadline_exceeded",
            _worker_authority=authority,
        )
        self.assertTrue(rollback.rolled_back)
        self.assertIsNone(provider.poll())
        self.assertEqual(owner.get_job(job.id).status, "RUNNING")
        kinds = [event.kind for event in owner.list_events(job.id)]
        self.assertIn("CANCELLATION_ACK_FAILED", kinds)
        self.assertNotIn("CANCELLATION_ACKNOWLEDGED", kinds)
        self.assertNotIn("CANCELLATION_TERM_SENT", kinds)
        self.assertNotIn("CANCELLATION_KILL_SENT", kinds)

    def test_combined_ack_transaction_rolls_back_fully_and_retries_once(self):
        owner, job, _claim, authority, command = self._prepare_claimed_cancellation(
            "cancel-combined-ack-retry"
        )
        owner.confirm_cancellation_delivery(
            job.id,
            command.id,
            _worker_authority=authority,
        )
        control_event = {
            "type": "control_response",
            "subtype": None,
            "model": None,
            "parent_tool_use_id": None,
            "data": {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": command.payload["request_id"],
                    "response": {},
                },
            },
        }
        original_append = owner._append_command_event
        injected = False

        def fail_ack_receipt_once(connection, job_row, attempt_row, kind, payload):
            nonlocal injected
            if kind == "CANCELLATION_ACKNOWLEDGED" and not injected:
                injected = True
                raise StateError("injected ACK receipt failure")
            return original_append(connection, job_row, attempt_row, kind, payload)

        with mock.patch.object(
            owner,
            "_append_command_event",
            side_effect=fail_ack_receipt_once,
        ):
            with self.assertRaisesRegex(StateError, "injected ACK"):
                owner.acknowledge_cancellation_control_event(
                    job.id,
                    request_id=command.payload["request_id"],
                    control_event=control_event,
                    acknowledged_at="2026-08-26T12:00:00.000000+00:00",
                    acknowledgement_deadline=time.monotonic() + 2,
                    _worker_authority=authority,
                )

        self.assertEqual(owner.list_commands(job.id)[0].status, "DELIVERED")
        self.assertNotIn(
            "CLAUDE_EVENT",
            [event.kind for event in owner.list_events(job.id)],
        )
        owner.acknowledge_cancellation_control_event(
            job.id,
            request_id=command.payload["request_id"],
            control_event=control_event,
            acknowledged_at="2026-08-26T12:00:00.000000+00:00",
            acknowledgement_deadline=time.monotonic() + 2,
            _worker_authority=authority,
        )
        events = owner.list_events(job.id)
        self.assertEqual(sum(event.kind == "CLAUDE_EVENT" for event in events), 1)
        self.assertEqual(
            sum(event.kind == "CANCELLATION_ACKNOWLEDGED" for event in events),
            1,
        )

    def test_buffered_ack_is_parsed_after_delivery_receipt_reconciliation(self):
        self.write_control(
            wait_for_stdin_eof_after_result=False,
            spawn_descendant=False,
        )
        bridge = self.bridge()
        started = bridge.tool(
            "forge_start",
            self.start_arguments(
                idempotency_key="buffered-ack-after-receipt-retry"
            ),
        )
        self.assertFalse(started["isError"], started)
        job_id = started["structuredContent"]["job_id"]
        running = self.wait_status(bridge, job_id, {"RUNNING"})
        self.addCleanup(self.force_stop_job, job_id)
        launch_deadline = time.monotonic() + 5
        while time.monotonic() < launch_deadline:
            events = Store(self.state_path).list_events(job_id)
            if (
                self.stdin_log.exists()
                and sum(event.kind == "CLAUDE_EVENT" for event in events) >= 2
            ):
                break
            time.sleep(0.01)
        else:
            self.fail("fake provider did not publish its launch receipts")
        running = bridge.tool(
            "forge_status",
            {"job_id": job_id},
        )["structuredContent"]
        with closing(
            sqlite3.connect(str(self.state_path), isolation_level=None)
        ) as connection:
            connection.execute(
                """
                CREATE TRIGGER block_cancel_delivery_receipt
                BEFORE UPDATE OF status ON commands
                WHEN OLD.kind = 'CANCEL'
                    AND OLD.status = 'DELIVERING'
                    AND NEW.status = 'DELIVERED'
                BEGIN
                    SELECT RAISE(ABORT, 'injected delivery receipt outage');
                END
                """
            )
        accepted = self.cancel(bridge, job_id, running["revision"])
        self.assertFalse(accepted["isError"], accepted)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            frames = [
                json.loads(line)
                for line in self.stdin_log.read_text(encoding="utf-8").splitlines()
            ]
            if any(frame.get("type") == "control_request" for frame in frames):
                break
            time.sleep(0.01)
        else:
            self.fail("fake provider did not receive the interrupt frame")
        time.sleep(0.15)
        with closing(
            sqlite3.connect(str(self.state_path), isolation_level=None)
        ) as connection:
            connection.execute("DROP TRIGGER block_cancel_delivery_receipt")

        terminal = self.wait_status(bridge, job_id, {"CANCELLED"}, timeout=5)
        self.assertEqual(terminal["status"], "CANCELLED")
        events = Store(self.state_path).list_events(job_id)
        self.assertEqual(sum(event.kind == "COMMAND_DELIVERED" for event in events), 1)
        self.assertEqual(
            sum(event.kind == "CANCELLATION_ACKNOWLEDGED" for event in events),
            1,
        )
        kinds = [event.kind for event in events]
        self.assertNotIn("CANCELLATION_TERM_SENT", kinds)
        self.assertNotIn("CANCELLATION_KILL_SENT", kinds)
        self.assertEqual(kinds.count("CANCELLATION_PROVIDER_EXITED"), 1)
        self.assertEqual(kinds.count("CANCELLATION_QUIESCENT"), 1)

        result = bridge.tool("forge_result", {"job_id": job_id})
        self.assertFalse(result["isError"], result)
        receipt = json.loads(
            Path(
                result["structuredContent"]["artifacts"]["receipt"]["path"]
            ).read_text(encoding="utf-8")
        )
        cancellation = receipt["cancellation"]
        self.assertEqual(cancellation["termination_mode"], "provider_exit_after_ack")
        self.assertIsNone(cancellation["term_sent_at"])
        self.assertIsNone(cancellation["kill_sent_at"])
        self.assertTrue(cancellation["provider_exited_at"])

    def test_malformed_event_after_cancel_rolls_back_before_generic_cleanup(self):
        self.write_control(
            interrupt_malformed_event=True,
            emit_interrupt_ack=False,
        )
        bridge = self.bridge()
        job_id, running = self.start_running(
            bridge,
            idempotency_key="malformed-after-cancel",
        )
        accepted = self.cancel(bridge, job_id, running["revision"])
        self.assertFalse(accepted["isError"], accepted)

        terminal = self.wait_status(bridge, job_id, {"FAILED"}, timeout=5)
        self.assertEqual(terminal["status"], "FAILED")
        events = Store(self.state_path).list_events(job_id)
        failure = next(
            event
            for event in events
            if event.kind == "CANCELLATION_ACK_FAILED"
        )
        self.assertEqual(failure.payload["reason"], "supervision_error")
        kinds = [event.kind for event in events]
        self.assertNotIn("CANCELLATION_ACKNOWLEDGED", kinds)
        self.assertNotIn("CANCELLATION_TERM_SENT", kinds)
        self.assertNotIn("CANCELLATION_KILL_SENT", kinds)

    def test_pending_receipt_then_malformed_event_still_rolls_back(self):
        self.write_control(
            interrupt_malformed_event=True,
            emit_interrupt_ack=False,
        )
        bridge = self.bridge()
        job_id, running = self.start_running(
            bridge,
            idempotency_key="pending-receipt-malformed-after-cancel",
        )
        with closing(
            sqlite3.connect(str(self.state_path), isolation_level=None)
        ) as connection:
            connection.execute(
                """
                CREATE TRIGGER block_pending_cancel_receipt
                BEFORE UPDATE OF status ON commands
                WHEN OLD.kind = 'CANCEL'
                    AND OLD.status = 'DELIVERING'
                    AND NEW.status = 'DELIVERED'
                BEGIN
                    SELECT RAISE(ABORT, 'injected pending receipt');
                END
                """
            )
        accepted = self.cancel(bridge, job_id, running["revision"])
        self.assertFalse(accepted["isError"], accepted)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            command = Store(self.state_path).list_commands(job_id)[-1]
            if command.status == "DELIVERING":
                break
            time.sleep(0.01)
        time.sleep(0.1)
        with closing(
            sqlite3.connect(str(self.state_path), isolation_level=None)
        ) as connection:
            connection.execute("DROP TRIGGER block_pending_cancel_receipt")

        terminal = self.wait_status(bridge, job_id, {"FAILED"}, timeout=5)
        self.assertEqual(terminal["status"], "FAILED")
        events = Store(self.state_path).list_events(job_id)
        self.assertEqual(
            next(
                event.payload["reason"]
                for event in events
                if event.kind == "CANCELLATION_ACK_FAILED"
            ),
            "supervision_error",
        )
        self.assertNotIn(
            "CANCELLATION_TERM_SENT",
            [event.kind for event in events],
        )

    def test_near_wall_budget_cancel_rolls_back_before_worker_cleanup(self):
        self.write_control(
            control_window_seconds=5,
            emit_interrupt_ack=False,
            spawn_descendant=True,
        )
        bridge = self.bridge()
        job_id, running = self.start_running(
            bridge,
            idempotency_key="cancel-near-wall-deadline",
            budget={
                "max_seconds": 4,
                "max_tokens": 4096,
                "max_turns": 4096,
                "max_cost_usd": "1.25",
            },
        )
        # Task 10 starts the wall clock before provider-free preflight. Leave
        # enough of that absolute budget to prove the interrupt frame was
        # delivered, while keeping the missing-ACK rollback near the wall.
        time.sleep(2.5)
        accepted = self.cancel(bridge, job_id, running["revision"])
        self.assertFalse(accepted["isError"], accepted)

        terminal = self.wait_status(bridge, job_id, {"FAILED"}, timeout=4)
        self.assertEqual(terminal["status"], "FAILED")
        events = Store(self.state_path).list_events(job_id)
        kinds = [event.kind for event in events]
        self.assertIn(
            "CANCELLATION_ACK_FAILED",
            kinds,
            [(event.kind, event.created_at, event.payload) for event in events],
        )
        self.assertNotIn("CANCELLATION_ACKNOWLEDGED", kinds)
        self.assertNotIn("CANCELLATION_TERM_SENT", kinds)
        self.assertNotIn("CANCELLATION_KILL_SENT", kinds)

    def test_ack_transaction_uses_the_earlier_job_wall_deadline(self):
        from forge_lib import processes, worker as worker_module
        from forge_lib.state import CancellationAcknowledgementExpired

        state_path = self.temp_path / "ack-wall-fence" / "state.sqlite3"
        store = Store(state_path)
        job = store.create_job(
            repo_id="ack-wall-fence-repo",
            idempotency_key="ack-wall-fence",
            request_hash="hash",
            payload={},
        )
        claim = store.claim_dispatch(job.id, owner_pid=os.getpid())
        assert claim is not None
        store.record_worker_pid(job.id, os.getpid(), _dispatch_claim=claim)
        authority = store.claim_worker_authority(job.id, claim.token)
        assert authority is not None
        current = store.get_job(job.id)
        store.transition(
            job.id,
            expected_revision=current.revision,
            new_status="PREFLIGHT",
        )
        child_code = "\n".join(
            (
                "import json,sys,time",
                "request=json.loads(sys.stdin.buffer.readline())",
                "print(json.dumps({'type':'control_response','response':{'subtype':'success','request_id':request['request_id'],'response':{}}},separators=(',',':')),flush=True)",
                "time.sleep(30)",
            )
        )
        provider = subprocess.Popen(
            [sys.executable, "-u", "-c", child_code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            bufsize=0,
        )

        def cleanup_provider():
            if provider.poll() is None:
                try:
                    os.killpg(provider.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            provider.wait(timeout=2)
            for stream in (provider.stdin, provider.stdout, provider.stderr):
                if stream is not None and not stream.closed:
                    stream.close()

        self.addCleanup(cleanup_provider)
        store.record_provider_identity(
            job.id,
            session_id="ack-wall-fence-session",
            provider_pgid=provider.pid,
        )
        current = store.get_job(job.id)
        store.transition(
            job.id,
            expected_revision=current.revision,
            new_status="RUNNING",
        )
        containment = self._containment()
        admission_containment = processes.cancellation_admission_preflight(
            containment,
            expected_process_group_id=provider.pid,
        )
        self.assertTrue(admission_containment["supported"])
        current = store.get_job(job.id)
        attempt = store.get_attempt(current.current_attempt_id)
        store.request_cancellation(
            job.id,
            expected_revision=current.revision,
            live_worker_identity=attempt.worker_identity,
            cancellation_manifest_pin=admission_containment["_test_manifest_pin"],
        )
        events_path = self.temp_path / "ack-wall-events.jsonl"
        stderr_path = self.temp_path / "ack-wall-stderr.log"
        events_descriptor = os.open(
            events_path,
            os.O_RDWR | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        stderr_descriptor = os.open(
            stderr_path,
            os.O_RDWR | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        self.addCleanup(os.close, stderr_descriptor)
        self.addCleanup(os.close, events_descriptor)
        wall_deadline = time.monotonic() + 0.5
        observed_deadlines = []

        def expire_at_durable_fence(*args, **kwargs):
            observed_deadlines.append(kwargs["acknowledgement_deadline"])
            raise CancellationAcknowledgementExpired(
                "injected expiry at the durable fence"
            )

        with mock.patch.object(
            store,
            "acknowledge_cancellation_control_event",
            side_effect=expire_at_durable_fence,
        ):
            with self.assertRaisesRegex(worker_module.WorkerError, "wall-time"):
                worker_module._collect_provider(
                    provider,
                    store=store,
                    job_id=job.id,
                    session_id="ack-wall-fence-session",
                    dispatch_token=claim.token,
                    worker_pid=os.getpid(),
                    worker_authority=authority,
                    events_descriptor=events_descriptor,
                    stderr_descriptor=stderr_descriptor,
                    deadline=wall_deadline,
                    include_cancellation=True,
                    cancellation_containment=containment,
                )

        self.assertEqual(observed_deadlines, [wall_deadline])
        kinds = [event.kind for event in store.list_events(job.id)]
        self.assertIn("CANCELLATION_ACK_FAILED", kinds)
        self.assertNotIn("CANCELLATION_ACKNOWLEDGED", kinds)
        self.assertNotIn("CANCELLATION_TERM_SENT", kinds)

    def test_new_store_cannot_replay_worker_scalars_to_acknowledge_cancellation(self):
        state_path = self.temp_path / "authority-replay" / "state.sqlite3"
        owner = Store(state_path)
        job = owner.create_job(
            repo_id="authority-replay-repo",
            idempotency_key="authority-replay",
            request_hash="hash",
            payload={},
        )
        claim = owner.claim_dispatch(job.id, owner_pid=os.getpid())
        assert claim is not None
        owner.record_worker_pid(job.id, os.getpid(), _dispatch_claim=claim)
        authority = owner.claim_worker_authority(job.id, claim.token)
        self.assertIsNotNone(authority)
        current = owner.get_job(job.id)
        owner.transition(
            job.id,
            expected_revision=current.revision,
            new_status="PREFLIGHT",
        )
        owner.record_provider_identity(
            job.id,
            session_id="authority-session",
            provider_pgid=os.getpid(),
        )
        current = owner.get_job(job.id)
        owner.transition(
            job.id,
            expected_revision=current.revision,
            new_status="RUNNING",
        )
        current = owner.get_job(job.id)
        admission = owner.request_cancellation(
            job.id,
            expected_revision=current.revision,
            live_worker_identity=owner.get_attempt(
                current.current_attempt_id
            ).worker_identity,
        )
        assert admission.command is not None
        command = owner.claim_next_command(
            job.id,
            dispatch_token=claim.token,
            worker_pid=os.getpid(),
        )
        assert command is not None
        owner.mark_command_delivered(
            job.id,
            command.id,
            dispatch_token=claim.token,
            worker_pid=os.getpid(),
        )

        replay = Store(state_path)
        with self.assertRaisesRegex(StateError, "authority"):
            replay.acknowledge_cancellation(
                job.id,
                request_id=admission.command.payload["request_id"],
                acknowledged_at="2026-08-26T12:00:00.000000+00:00",
                acknowledgement_deadline=time.monotonic() + 5,
                dispatch_token=claim.token,
                worker_pid=os.getpid(),
            )

        read_descriptor, write_descriptor = os.pipe()
        replay_pid = os.fork()
        if replay_pid == 0:
            os.close(read_descriptor)
            outcome = b"accepted"
            try:
                Store(state_path).acknowledge_cancellation(
                    job.id,
                    request_id=admission.command.payload["request_id"],
                    acknowledged_at="2026-08-26T12:00:00.000000+00:00",
                    acknowledgement_deadline=time.monotonic() + 5,
                    dispatch_token=claim.token,
                    worker_pid=os.getppid(),
                )
            except StateError:
                outcome = b"rejected"
            try:
                os.write(write_descriptor, outcome)
            finally:
                os.close(write_descriptor)
                os._exit(0)
        os.close(write_descriptor)
        try:
            process_outcome = os.read(read_descriptor, 64)
        finally:
            os.close(read_descriptor)
            _waited_pid, status = os.waitpid(replay_pid, 0)
        self.assertEqual(status, 0)
        self.assertEqual(process_outcome, b"rejected")

    def test_unlaunched_process_cannot_mint_authority_from_dispatch_scalars(self):
        state_path = self.temp_path / "authority-impostor" / "state.sqlite3"
        launcher = Store(state_path)
        job = launcher.create_job(
            repo_id="authority-impostor-repo",
            idempotency_key="authority-impostor",
            request_hash="hash",
            payload={},
        )
        claim = launcher.claim_dispatch(job.id, owner_pid=os.getpid())
        assert claim is not None
        read_descriptor, write_descriptor = os.pipe()
        impostor_pid = os.fork()
        if impostor_pid == 0:
            os.close(read_descriptor)
            outcome = b"rejected"
            try:
                authority = Store(state_path).claim_worker_authority(
                    job.id,
                    claim.token,
                )
                if authority is not None:
                    outcome = b"minted"
            except StateError:
                pass
            try:
                os.write(write_descriptor, outcome)
            finally:
                os.close(write_descriptor)
                os._exit(0)
        os.close(write_descriptor)
        try:
            outcome = os.read(read_descriptor, 64)
        finally:
            os.close(read_descriptor)
            _waited_pid, status = os.waitpid(impostor_pid, 0)
        self.assertEqual(status, 0)
        self.assertEqual(outcome, b"rejected")
        attempt = launcher.get_attempt(job.current_attempt_id)
        self.assertIsNone(attempt.worker_pid)
        self.assertIsNone(attempt.worker_identity)

    def test_fresh_store_cannot_terminalize_valid_cancellation_chain(self):
        from forge_lib import worker

        path = self.temp_path / "terminal-authority" / "state.sqlite3"
        service = ForgeService(
            state_path=path,
            launch_workspace=self.repo,
            claude_executable=str(FAKE_CLAUDE),
            bridge_path=BRIDGE,
            environment=self.environment,
            _admission_machine_probe=machine_headroom,
        )
        owner = service.store
        job = self.create_admitted_job(
            service,
            self.start_arguments(idempotency_key="terminal-authority"),
        )
        claim = owner.claim_dispatch(job.id, owner_pid=os.getpid())
        assert claim is not None
        owner.record_worker_pid(job.id, os.getpid(), _dispatch_claim=claim)
        authority = owner.claim_worker_authority(job.id, claim.token)
        self.assertIsNotNone(authority)
        current = owner.get_job(job.id)
        owner.transition(
            job.id,
            expected_revision=current.revision,
            new_status="PREFLIGHT",
        )
        owner.record_provider_identity(
            job.id,
            session_id="s" * 32,
            provider_pgid=os.getpid(),
            observed_model=job.payload["model"],
        )
        current = owner.get_job(job.id)
        owner.transition(
            job.id,
            expected_revision=current.revision,
            new_status="RUNNING",
        )
        current = owner.get_job(job.id)
        attempt = owner.get_attempt(current.current_attempt_id)
        admission = owner.request_cancellation(
            job.id,
            expected_revision=current.revision,
            live_worker_identity=attempt.worker_identity,
        )
        assert admission.command is not None
        request_id = admission.command.payload["request_id"]
        command = owner.claim_next_command(
            job.id,
            dispatch_token=claim.token,
            worker_pid=os.getpid(),
        )
        assert command is not None
        owner.mark_command_delivered(
            job.id,
            command.id,
            dispatch_token=claim.token,
            worker_pid=os.getpid(),
        )
        owner.append_event(
            job.id,
            "CLAUDE_EVENT",
            {
                "type": "control_response",
                "subtype": None,
                "model": None,
                "parent_tool_use_id": None,
                "data": {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": request_id,
                        "response": {},
                    },
                },
            },
        )
        acknowledged_at = "2026-08-26T12:00:00.000001+00:00"
        term_at = "2026-08-26T12:00:00.000002+00:00"
        quiescent_at = "2026-08-26T12:00:00.000003+00:00"
        owner.acknowledge_cancellation(
            job.id,
            request_id=request_id,
            acknowledged_at=acknowledged_at,
            acknowledgement_deadline=time.monotonic() + 5,
            _worker_authority=authority,
        )
        owner.record_cancellation_stage(
            job.id,
            request_id=request_id,
            provider_pgid=os.getpid(),
            stage="TERM_SENT",
            recorded_at=term_at,
            _worker_authority=authority,
        )
        owner.record_cancellation_stage(
            job.id,
            request_id=request_id,
            provider_pgid=os.getpid(),
            stage="QUIESCENT",
            recorded_at=quiescent_at,
            _worker_authority=authority,
        )
        owner.close_cancellation_inbox(
            job.id,
            _worker_authority=authority,
        )
        cancellation = {
            "request_id": request_id,
            "provider_pgid": os.getpid(),
            "control_acknowledged_at": acknowledged_at,
            "term_sent_at": term_at,
            "kill_sent_at": None,
            "quiescent_at": quiescent_at,
            "non_zombie_descendants": 0,
        }
        self.assertEqual(
            owner.validate_cancellation_receipt(job.id, cancellation),
            cancellation,
        )

        replay = Store(path)
        with self.assertRaisesRegex(StateError, "authority"):
            worker._terminalize(
                replay,
                job.id,
                "CANCELLED",
                success_fields={"cancellation": cancellation},
                report="forged",
            )
        self.assertEqual(replay.get_job(job.id).status, "INTERRUPTING")

        owner.record_cancellation_cleanup_failure(
            job.id,
            request_id=request_id,
            provider_pgid=os.getpid(),
            failures=[
                {
                    "stage": "TERM_SENT",
                    "error_type": "StateError",
                    "message": "forced durable receipt failure",
                }
            ],
            _worker_authority=authority,
        )
        with self.assertRaisesRegex(StateError, "cleanup"):
            owner.validate_cancellation_receipt(job.id, cancellation)
        receipt_path = owner.attempt_artifact_path(
            job.id,
            attempt.id,
            "receipt.json",
        )
        receipt_hash = __import__("hashlib").sha256(receipt_path.read_bytes()).hexdigest()
        current = owner.get_job(job.id)
        with self.assertRaisesRegex(StateError, "cleanup"):
            owner.finalize_with_receipt(
                job.id,
                expected_revision=current.revision,
                new_status="CANCELLED",
                receipt_name="receipt.json",
                receipt_hash=receipt_hash,
                _worker_authority=authority,
            )
        self.assertEqual(owner.get_job(job.id).status, "INTERRUPTING")

    def test_stage_persistence_failure_does_not_abandon_term_kill_quiescence(self):
        from forge_lib import processes, worker as worker_module

        process = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-c",
                (
                    "import signal,time;"
                    "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                    "print('ready',flush=True);"
                    "time.sleep(30)"
                ),
            ],
            stdout=subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdout is not None
        self.assertEqual(process.stdout.readline(), b"ready\n")

        def cleanup_process():
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=2)
            process.stdout.close()

        self.addCleanup(cleanup_process)
        attempted = []
        containment = self._containment()
        manifest_pin = self._admission_manifest_pin(containment, process.pid)

        def persist(stage, _timestamp):
            attempted.append(stage)
            if stage == "TERM_SENT":
                raise StateError("界" * 300)

        try:
            evidence = processes.cancel_process_group(
                process,
                expected_process_group_id=process.pid,
                containment=containment,
                admission_manifest_pin=manifest_pin,
                grace_seconds=0.01,
                stage_callback=persist,
            )
        except StateError:
            evidence = None

        self.assertIsNotNone(evidence)
        self.assertEqual(attempted, ["TERM_SENT", "KILL_SENT", "QUIESCENT"])
        self.assertIsNotNone(process.poll())
        self.assertEqual(
            [failure.stage for failure in evidence.stage_failures],
            ["TERM_SENT"],
        )
        self.assertLessEqual(
            len(evidence.stage_failures[0].message.encode("utf-8")),
            512,
        )
        bounded_redacted = worker_module._bounded_redacted_text(
            "token=x" + ("界" * 300),
            maximum_bytes=512,
        )
        self.assertIn("[REDACTED]", bounded_redacted)
        self.assertLessEqual(len(bounded_redacted.encode("utf-8")), 512)

    def test_manifest_replacement_after_admission_cannot_hide_escaped_descendant(self):
        from forge_lib import processes

        child_pid_path = self.temp_path / "mutable-manifest-child.pid"
        escape_trigger = self.temp_path / "mutable-manifest-escape"
        escaped_ready = self.temp_path / "mutable-manifest-escaped"
        child_code = (
            "import os,signal,time,pathlib;"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            f"trigger=pathlib.Path({str(escape_trigger)!r});"
            f"ready=pathlib.Path({str(escaped_ready)!r});"
            "\nwhile not trigger.exists(): time.sleep(0.005)\n"
            "os.setsid();ready.write_text('ready');\n"
            "while True: signal.pause()\n"
        )
        leader_code = (
            "import signal,subprocess,sys,pathlib;"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
            "stderr=subprocess.DEVNULL);"
            f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid));"
            "\nwhile True: signal.pause()\n"
        )
        leader = subprocess.Popen(
            [sys.executable, "-u", "-c", leader_code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        child_pid = None

        def cleanup_tree():
            for process_group_id in (child_pid, leader.pid):
                if process_group_id is None:
                    continue
                try:
                    os.killpg(process_group_id, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            try:
                leader.wait(timeout=2)
            except subprocess.TimeoutExpired:
                leader.kill()
                leader.wait(timeout=2)

        self.addCleanup(cleanup_tree)
        deadline = time.monotonic() + 3
        while not child_pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(child_pid_path.exists())
        child_pid = int(child_pid_path.read_text(encoding="ascii"))
        containment = self._containment(child_pid)
        admission = processes.cancellation_admission_preflight(
            containment,
            expected_process_group_id=leader.pid,
        )
        self.assertTrue(admission["supported"], admission)
        self.assertIn("_test_manifest_pin", admission)
        manifest_pin = admission["_test_manifest_pin"]

        escape_trigger.touch()
        deadline = time.monotonic() + 3
        while not escaped_ready.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(escaped_ready.exists())
        replacement = containment.manifest_path.with_suffix(".replacement")
        replacement.write_text("", encoding="utf-8")
        replacement.chmod(0o600)
        os.replace(replacement, containment.manifest_path)
        attempted = []

        with self.assertRaisesRegex(ProcessError, "manifest"):
            processes.cancel_process_group(
                leader,
                expected_process_group_id=leader.pid,
                containment=containment,
                admission_manifest_pin=manifest_pin,
                grace_seconds=0.01,
                stage_callback=lambda stage, _timestamp: attempted.append(stage),
            )

        self.assertEqual(attempted, [])
        self.assertTrue(_non_zombie_process_exists(leader.pid))
        self.assertTrue(_non_zombie_process_exists(child_pid))

    def test_dead_worker_cancel_rejects_before_admission_without_mutation(self):
        bridge = self.bridge()
        job_id, running = self.start_running(
            bridge,
            idempotency_key="cancel-dead-worker",
        )
        store = Store(self.state_path)
        attempt = store.get_attempt(running["attempt_id"])
        assert attempt.worker_pid is not None
        os.killpg(attempt.worker_pid, signal.SIGKILL)
        deadline = time.monotonic() + 3
        while (
            _non_zombie_process_exists(attempt.worker_pid)
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        self.assertFalse(_non_zombie_process_exists(attempt.worker_pid))
        before_events = store.list_events(job_id)
        before_commands = store.list_commands(job_id)

        rejected = self.cancel(bridge, job_id, running["revision"])

        self.assertTrue(rejected["isError"], rejected)
        self.assertEqual(rejected["structuredContent"]["status"], "PAUSED_POLICY")
        self.assertEqual(
            rejected["structuredContent"]["reason"],
            "worker_identity_unavailable",
        )
        current = bridge.tool("forge_status", {"job_id": job_id})[
            "structuredContent"
        ]
        self.assertEqual(current["status"], "RUNNING")
        self.assertEqual(current["revision"], running["revision"])
        self.assertEqual(store.list_events(job_id), before_events)
        self.assertEqual(store.list_commands(job_id), before_commands)

    def test_detached_worker_waits_for_parent_launch_binding_handshake(self):
        self.write_control(
            control_window_seconds=0,
            spawn_descendant=False,
        )
        service = ForgeService(
            state_path=self.state_path,
            launch_workspace=self.repo,
            claude_executable=str(FAKE_CLAUDE),
            bridge_path=BRIDGE,
            environment=self.environment,
            cancellation_containment=self._containment(),
            _admission_machine_probe=machine_headroom,
        )
        original_record = service.store.record_worker_pid
        binding_started = threading.Event()
        allow_binding = threading.Event()

        def delayed_binding(*args, **kwargs):
            binding_started.set()
            if not allow_binding.wait(timeout=5):
                raise AssertionError("test did not release delayed launch binding")
            return original_record(*args, **kwargs)

        with mock.patch.object(
            service.store,
            "record_worker_pid",
            side_effect=delayed_binding,
        ):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    service.start,
                    self.start_arguments(
                        idempotency_key="delayed-launch-binding",
                        budget={
                            "max_seconds": 5,
                            "max_tokens": 4096,
                            "max_turns": 4096,
                            "max_cost_usd": "1.25",
                        },
                    ),
                )
                self.assertTrue(binding_started.wait(timeout=2))
                time.sleep(2.2)
                self.assertFalse(future.done())
                allow_binding.set()
                started = future.result(timeout=4)

        self.assertFalse(started.is_error)
        job_id = started.body["job_id"]
        deadline = time.monotonic() + 5
        status = None
        while time.monotonic() < deadline:
            status = service.store.get_job(job_id).status
            if status == "SUCCEEDED":
                break
            time.sleep(0.02)
        self.assertEqual(status, "SUCCEEDED")

    def test_post_bind_launch_gate_failure_releases_only_the_reaped_worker(self):
        from forge_lib import processes

        self.write_control(
            control_window_seconds=0,
            spawn_descendant=False,
        )
        service = ForgeService(
            state_path=self.state_path,
            launch_workspace=self.repo,
            claude_executable=str(FAKE_CLAUDE),
            bridge_path=BRIDGE,
            environment=self.environment,
            cancellation_containment=self._containment(),
            _admission_machine_probe=machine_headroom,
        )
        with mock.patch.object(
            processes.os,
            "write",
            side_effect=BrokenPipeError("injected launch-gate release failure"),
        ):
            with self.assertRaises(processes.BoundWorkerLaunchError):
                service.start(
                    self.start_arguments(idempotency_key="post-bind-gate-failure")
                )

        job_id = service.list_jobs({}).body["jobs"][0]["job_id"]
        job = service.store.get_job(job_id)
        attempt = service.store.get_attempt(job.current_attempt_id)
        self.assertEqual(job.status, "CREATED")
        self.assertEqual(attempt.dispatch_state, "UNCLAIMED")
        self.assertIsNone(attempt.dispatch_owner_pid)
        self.assertIsNone(attempt.dispatch_token)
        self.assertIsNone(attempt.worker_pid)
        self.assertIsNone(attempt.worker_identity)

        retried = service.start(
            self.start_arguments(idempotency_key="post-bind-gate-failure")
        )
        self.assertFalse(retried.is_error)
        rebound = service.store.get_attempt(retried.body["attempt_id"])
        self.assertIsNotNone(rebound.worker_pid)
        self.assertIsNotNone(rebound.worker_identity)

    def test_post_commit_binding_exception_also_releases_the_reaped_worker(self):
        service = ForgeService(
            state_path=self.temp_path / "post-commit-binding" / "state.sqlite3",
            launch_workspace=self.repo,
            claude_executable=str(FAKE_CLAUDE),
            bridge_path=BRIDGE,
            environment=self.environment,
            cancellation_containment=self._containment(),
            _admission_machine_probe=machine_headroom,
        )
        original_record = service.store.record_worker_pid

        def commit_then_raise(*args, **kwargs):
            original_record(*args, **kwargs)
            raise StateError("injected post-commit binding failure")

        with mock.patch.object(
            service.store,
            "record_worker_pid",
            side_effect=commit_then_raise,
        ):
            with self.assertRaisesRegex(StateError, "post-commit binding"):
                service.start(
                    self.start_arguments(
                        idempotency_key="post-commit-binding-failure"
                    )
                )

        job_id = service.list_jobs({}).body["jobs"][0]["job_id"]
        job = service.store.get_job(job_id)
        attempt = service.store.get_attempt(job.current_attempt_id)
        self.assertEqual(job.status, "CREATED")
        self.assertEqual(attempt.dispatch_state, "UNCLAIMED")
        self.assertIsNone(attempt.dispatch_owner_pid)
        self.assertIsNone(attempt.dispatch_token)
        self.assertIsNone(attempt.worker_pid)
        self.assertIsNone(attempt.worker_identity)

    def test_launcher_crash_after_binding_before_gate_release_reclaims_dead_worker(self):
        from forge_lib import processes

        state_path = self.temp_path / "launcher-crash" / "state.sqlite3"
        arguments = self.start_arguments(
            idempotency_key="launcher-crash-after-binding"
        )
        ready_read, ready_write = os.pipe()
        launcher_pid = os.fork()
        worker_pid = None
        worker_identity = None
        launcher_reaped = False

        def cleanup_processes():
            if not launcher_reaped:
                try:
                    os.kill(launcher_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(launcher_pid, 0)
                except ChildProcessError:
                    pass
            if worker_pid is not None and worker_identity is not None:
                try:
                    if (
                        processes.process_instance_identity(worker_pid)
                        == worker_identity
                    ):
                        os.kill(worker_pid, signal.SIGKILL)
                except (ProcessError, ProcessLookupError):
                    pass

        self.addCleanup(cleanup_processes)
        if launcher_pid == 0:
            os.close(ready_read)
            try:
                launcher = ForgeService(
                    state_path=state_path,
                    launch_workspace=self.repo,
                    claude_executable=str(FAKE_CLAUDE),
                    bridge_path=BRIDGE,
                    environment={**self.environment, "FORGE_STATE_PATH": str(state_path)},
                    _admission_machine_probe=machine_headroom,
                )
                actual_spawn = processes.spawn_detached_worker

                def spawn_into_crash_window(*args, **kwargs):
                    bind_before_release = kwargs["bind_before_release"]

                    def bind_then_block(spawned_pid):
                        bind_before_release(spawned_pid)
                        job_id = launcher.list_jobs({}).body["jobs"][0]["job_id"]
                        os.write(
                            ready_write,
                            f"{job_id} {spawned_pid}\n".encode("ascii"),
                        )
                        while True:
                            signal.pause()

                    return actual_spawn(
                        *args,
                        **{**kwargs, "bind_before_release": bind_then_block},
                    )

                with mock.patch(
                    "forge_lib.service.spawn_detached_worker",
                    side_effect=spawn_into_crash_window,
                ):
                    launcher.start(arguments)
                os._exit(3)
            except BaseException:
                os._exit(2)

        os.close(ready_write)
        try:
            ready, _, _ = select.select([ready_read], [], [], 5)
            self.assertTrue(ready, "launcher never reached the post-binding crash window")
            job_id_text, worker_pid_text = os.read(ready_read, 256).split()
            job_id = job_id_text.decode("ascii")
            worker_pid = int(worker_pid_text)
        finally:
            os.close(ready_read)

        stranded_store = Store(state_path)
        stranded_job = stranded_store.get_job(job_id)
        stranded_attempt = stranded_store.get_attempt(
            stranded_job.current_attempt_id
        )
        worker_identity = stranded_attempt.worker_identity
        self.assertEqual(stranded_job.status, "CREATED")
        self.assertEqual(stranded_attempt.dispatch_state, "DISPATCHED")
        self.assertEqual(stranded_attempt.worker_pid, worker_pid)
        self.assertIsNotNone(worker_identity)
        old_token = stranded_attempt.dispatch_token

        os.kill(launcher_pid, signal.SIGKILL)
        os.waitpid(launcher_pid, 0)
        launcher_reaped = True
        deadline = time.monotonic() + 5
        while (
            processes.process_instance_is_live(worker_pid, worker_identity)
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        self.assertFalse(
            processes.process_instance_is_live(worker_pid, worker_identity),
            "launch-gated worker remained live after its parent crashed",
        )

        restarted = ForgeService(
            state_path=state_path,
            launch_workspace=self.repo,
            claude_executable=str(FAKE_CLAUDE),
            bridge_path=BRIDGE,
            environment={**self.environment, "FORGE_STATE_PATH": str(state_path)},
            _admission_machine_probe=machine_headroom,
        )

        def bind_retry(*_args, **kwargs):
            retry_pid = os.getpid()
            kwargs["bind_before_release"](retry_pid)
            return retry_pid

        with mock.patch(
            "forge_lib.service.spawn_detached_worker",
            side_effect=bind_retry,
        ) as retry_spawn:
            retried = restarted.start(arguments)

        self.assertTrue(retried.body["idempotent"])
        self.assertEqual(retry_spawn.call_count, 1)
        self.assertEqual(retried.body["job_id"], stranded_job.id)
        self.assertEqual(retried.body["attempt_id"], stranded_attempt.id)
        rebound = restarted.store.get_attempt(stranded_attempt.id)
        self.assertNotEqual(rebound.dispatch_token, old_token)
        self.assertEqual(rebound.worker_pid, os.getpid())
        self.assertIsNotNone(rebound.worker_identity)

    def test_dead_bound_worker_is_reclaimed_before_preflight_with_live_owner(self):
        store = Store(self.temp_path / "dead-bound-worker" / "state.sqlite3")
        job = store.create_job(
            repo_id="dead-bound-worker-repo",
            idempotency_key="dead-bound-worker",
            request_hash="hash",
            payload={},
        )
        first_claim = store.claim_dispatch(job.id, owner_pid=os.getpid())
        self.assertIsNotNone(first_claim)
        assert first_claim is not None
        worker = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        try:
            store.record_worker_pid(
                job.id,
                worker.pid,
                _dispatch_claim=first_claim,
            )
            worker.kill()
            worker.wait(timeout=2)

            recovered = Store(store.path).claim_dispatch(
                job.id,
                owner_pid=os.getpid(),
            )
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=2)

        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered.attempt_id, first_claim.attempt_id)
        self.assertNotEqual(recovered.token, first_claim.token)
        rebound = store.get_attempt(first_claim.attempt_id)
        self.assertEqual(rebound.dispatch_state, "DISPATCHED")
        self.assertEqual(rebound.dispatch_owner_pid, os.getpid())
        self.assertEqual(rebound.dispatch_token, recovered.token)
        self.assertIsNone(rebound.worker_pid)
        self.assertIsNone(rebound.worker_identity)

    def test_bound_worker_reclaim_fails_closed_when_os_evidence_is_unavailable(self):
        store = Store(self.temp_path / "ambiguous-bound-worker" / "state.sqlite3")
        job = store.create_job(
            repo_id="ambiguous-bound-worker-repo",
            idempotency_key="ambiguous-bound-worker",
            request_hash="hash",
            payload={},
        )
        claim = store.claim_dispatch(job.id, owner_pid=os.getpid())
        self.assertIsNotNone(claim)
        assert claim is not None
        store.record_worker_pid(job.id, os.getpid(), _dispatch_claim=claim)
        before = store.get_attempt(claim.attempt_id)

        with mock.patch(
            "forge_lib.state.process_instance_is_definitively_dead",
            side_effect=ProcessError("process snapshot unavailable"),
        ):
            recovered = Store(store.path).claim_dispatch(
                job.id,
                owner_pid=os.getpid(),
            )

        self.assertIsNone(recovered)
        self.assertEqual(store.get_attempt(claim.attempt_id), before)

    def test_worker_popen_failure_closes_both_launch_gate_descriptors(self):
        from forge_lib import processes

        worker_log = self.temp_path / "popen-failure-worker.log"
        worker_log.touch(mode=0o600)
        actual_pipe = os.pipe
        actual_log_open = processes._regular_private_append
        opened_pipes = []
        opened_logs = []

        def tracked_pipe():
            descriptors = actual_pipe()
            opened_pipes.append(descriptors)
            return descriptors

        def tracked_log(path):
            stream = actual_log_open(path)
            opened_logs.append(stream)
            return stream

        with (
            mock.patch.object(processes.os, "pipe", side_effect=tracked_pipe),
            mock.patch.object(
                processes,
                "_regular_private_append",
                side_effect=tracked_log,
            ),
            mock.patch.object(
                processes.subprocess,
                "Popen",
                side_effect=OSError("injected Popen failure"),
            ),
        ):
            for index in range(25):
                with self.assertRaises(processes.ProcessError):
                    processes.spawn_detached_worker(
                        BRIDGE,
                        f"popen-failure-{index}",
                        job_role="build",
                        dispatch_token="a" * 32,
                        worker_log=worker_log,
                        environment=self.environment,
                        bind_before_release=lambda _pid: self.fail(
                            "binding callback ran after Popen failure"
                        ),
                    )
        self.assertEqual(len(opened_pipes), 25)
        self.assertEqual(len(opened_logs), 25)
        self.assertTrue(all(stream.closed for stream in opened_logs))
        for descriptors in opened_pipes:
            for descriptor in descriptors:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)

    def test_transactional_admission_rejects_an_unreaped_zombie_worker(self):
        store = Store(self.temp_path / "zombie-worker" / "state.sqlite3")
        job = store.create_job(
            repo_id="zombie-worker-repo",
            idempotency_key="zombie-worker",
            request_hash="hash",
            payload={},
        )
        claim = store.claim_dispatch(job.id, owner_pid=os.getpid())
        assert claim is not None
        worker_process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(0.2)"],
            start_new_session=True,
        )
        self.addCleanup(
            lambda: worker_process.wait(timeout=2)
            if worker_process.returncode is None
            else None
        )
        store.record_worker_pid(
            job.id,
            worker_process.pid,
            _dispatch_claim=claim,
        )
        current = store.get_job(job.id)
        store.transition(
            job.id,
            expected_revision=current.revision,
            new_status="PREFLIGHT",
        )
        current = store.get_job(job.id)
        store.transition(
            job.id,
            expected_revision=current.revision,
            new_status="RUNNING",
        )
        running = store.get_job(job.id)
        attempt = store.get_attempt(running.current_attempt_id)
        assert attempt.worker_identity is not None
        deadline = time.monotonic() + 3
        while (
            _non_zombie_process_exists(worker_process.pid)
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        self.assertTrue(_process_exists(worker_process.pid))
        self.assertFalse(_non_zombie_process_exists(worker_process.pid))
        before_events = store.list_events(job.id)

        with mock.patch(
            "forge_lib.state.process_instance_identity",
            return_value=attempt.worker_identity,
        ):
            with self.assertRaisesRegex(StateError, "not live"):
                store.request_cancellation(
                    job.id,
                    expected_revision=running.revision,
                    live_worker_identity=attempt.worker_identity,
                )

        self.assertEqual(store.get_job(job.id).status, "RUNNING")
        self.assertEqual(store.list_events(job.id), before_events)

    def test_dead_worker_with_ambiguous_delivering_cancel_pauses_without_replay(self):
        service = ForgeService(
            state_path=self.temp_path / "ambiguous-dead-worker" / "state.sqlite3",
            launch_workspace=self.repo,
            claude_executable=str(FAKE_CLAUDE),
            bridge_path=BRIDGE,
            environment=self.environment,
            cancellation_containment=self._containment(),
        )
        _identity, payload = service._normalized_start(
            self.start_arguments(idempotency_key="ambiguous-dead-worker")
        )
        job = service.store.create_job(
            repo_id=service._launch_repo_id,
            idempotency_key="ambiguous-dead-worker",
            request_hash="hash",
            payload=payload,
        )
        claim = service.store.claim_dispatch(job.id, owner_pid=os.getpid())
        assert claim is not None
        worker_process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        service.store.record_worker_pid(
            job.id,
            worker_process.pid,
            _dispatch_claim=claim,
        )
        for status in ("PREFLIGHT", "RUNNING"):
            current = service.store.get_job(job.id)
            service.store.transition(
                job.id,
                expected_revision=current.revision,
                new_status=status,
            )
        service.store.record_provider_identity(
            job.id,
            provider_pgid=worker_process.pid,
            session_id="ambiguous-worker-session",
        )
        running = service.store.get_job(job.id)
        attempt = service.store.get_attempt(running.current_attempt_id)
        admission = service.store.request_cancellation(
            job.id,
            expected_revision=running.revision,
            live_worker_identity=attempt.worker_identity,
        )
        assert admission.command is not None
        claimed = service.store.claim_next_command(
            job.id,
            dispatch_token=claim.token,
            worker_pid=worker_process.pid,
        )
        assert claimed is not None
        worker_process.kill()
        worker_process.wait(timeout=2)
        interrupting = service.store.get_job(job.id)
        before_events = service.store.list_events(job.id)
        before_commands = service.store.list_commands(job.id)

        paused = service.cancel(
            {"job_id": job.id, "revision": interrupting.revision}
        )

        self.assertTrue(paused.is_error)
        self.assertEqual(paused.body["status"], "PAUSED_POLICY")
        self.assertEqual(paused.body["reason"], "delivery_ambiguous")
        self.assertEqual(service.store.get_job(job.id).status, "INTERRUPTING")
        self.assertEqual(service.store.list_events(job.id), before_events)
        self.assertEqual(service.store.list_commands(job.id), before_commands)
        fresh = Store(service.store.path)
        self.assertIsNone(
            fresh.claim_next_command(
                job.id,
                dispatch_token=claim.token,
                worker_pid=worker_process.pid,
            )
        )

    def test_worker_death_during_liveness_sample_rejects_inside_admission_fence(self):
        from forge_lib import processes

        bridge = self.bridge()
        job_id, running = self.start_running(
            bridge,
            idempotency_key="cancel-worker-dies-in-admission-gap",
        )
        store = Store(self.state_path)
        attempt = store.get_attempt(running["attempt_id"])
        assert attempt.worker_pid is not None
        before_events = store.list_events(job_id)
        before_commands = store.list_commands(job_id)
        local_service = ForgeService(
            state_path=self.state_path,
            launch_workspace=self.repo,
            claude_executable=str(FAKE_CLAUDE),
            bridge_path=BRIDGE,
            environment=self.environment,
            cancellation_containment=processes._test_cancellation_namespace(
                self.descendant_manifest
            ),
        )

        def kill_then_report_live(worker_pid, _worker_identity):
            os.killpg(worker_pid, signal.SIGKILL)
            deadline = time.monotonic() + 3
            while _non_zombie_process_exists(worker_pid) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(_non_zombie_process_exists(worker_pid))
            return True

        with mock.patch(
            "forge_lib.service.process_instance_is_live",
            side_effect=kill_then_report_live,
        ):
            rejected = local_service.execute(
                "forge_cancel",
                {"job_id": job_id, "revision": running["revision"]},
            )

        self.assertTrue(rejected.is_error, rejected)
        self.assertEqual(rejected.body["status"], "PAUSED_POLICY")
        self.assertEqual(rejected.body["reason"], "worker_identity_unavailable")
        current = store.get_job(job_id)
        self.assertEqual(current.status, "RUNNING")
        self.assertEqual(current.revision, running["revision"])
        self.assertEqual(store.list_events(job_id), before_events)
        self.assertEqual(store.list_commands(job_id), before_commands)

    def test_late_ready_ack_is_rejected_and_cancel_rolls_back_to_running(self):
        self.write_control(
            interrupt_ack_delay_seconds=1.2,
            control_window_seconds=5,
        )
        bridge = self.bridge()
        job_id, running = self.start_running(
            bridge,
            idempotency_key="cancel-late-ready-ack",
        )
        accepted = self.cancel(bridge, job_id, running["revision"])
        self.assertFalse(accepted["isError"], accepted)

        deadline = time.monotonic() + 4
        current = None
        events = []
        while time.monotonic() < deadline:
            current = bridge.tool("forge_status", {"job_id": job_id})[
                "structuredContent"
            ]
            events = Store(self.state_path).list_events(job_id)
            if any(event.kind == "CANCELLATION_ACK_FAILED" for event in events):
                current = bridge.tool("forge_status", {"job_id": job_id})[
                    "structuredContent"
                ]
                break
            time.sleep(0.01)
        assert current is not None
        self.assertEqual(current["status"], "RUNNING")
        self.assertIn("CANCELLATION_ACK_FAILED", [event.kind for event in events])
        self.assertNotIn("CANCELLATION_ACKNOWLEDGED", [event.kind for event in events])
        self.assertNotIn("CANCELLATION_TERM_SENT", [event.kind for event in events])

    def test_missing_ack_rolls_back_and_a_fresh_cancel_can_retry(self):
        self.write_control(
            interrupt_ack_modes=["missing", "exact"],
            control_window_seconds=5,
        )
        bridge = self.bridge()
        job_id, running = self.start_running(
            bridge,
            idempotency_key="cancel-missing-ack-retry",
        )
        first = self.cancel(bridge, job_id, running["revision"])
        self.assertFalse(first["isError"], first)

        deadline = time.monotonic() + 4
        rolled_back = None
        while time.monotonic() < deadline:
            candidate = bridge.tool("forge_status", {"job_id": job_id})[
                "structuredContent"
            ]
            events = Store(self.state_path).list_events(job_id)
            if (
                candidate["status"] == "RUNNING"
                and any(event.kind == "CANCELLATION_ACK_FAILED" for event in events)
            ):
                rolled_back = candidate
                break
            time.sleep(0.01)
        self.assertIsNotNone(rolled_back)

        second = self.cancel(bridge, job_id, rolled_back["revision"])
        self.assertFalse(second["isError"], second)
        self.assertNotEqual(
            first["structuredContent"]["request_id"],
            second["structuredContent"]["request_id"],
        )
        terminal = self.wait_status(bridge, job_id, {"CANCELLED"})
        self.assertEqual(terminal["status"], "CANCELLED")
        commands = Store(self.state_path).list_commands(job_id)
        self.assertEqual(
            [(command.kind, command.status) for command in commands],
            [("CANCEL", "DELIVERY_FAILED"), ("CANCEL", "ACKNOWLEDGED")],
        )

    def test_fake_manifest_rejects_real_double_fork_escape_before_admission(self):
        self.write_control(
            spawn_double_fork_descendant=True,
            double_fork_log=str(self.double_fork_log),
        )
        bridge = self.bridge()
        job_id, running = self.start_running(
            bridge,
            idempotency_key="cancel-double-fork-manifest",
        )
        deadline = time.monotonic() + 3
        while not self.double_fork_log.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.double_fork_log.exists())
        daemon_pid = json.loads(
            self.double_fork_log.read_text(encoding="utf-8").splitlines()[-1]
        )["pid"]
        self.addCleanup(
            lambda: os.kill(daemon_pid, signal.SIGKILL)
            if _process_exists(daemon_pid)
            else None
        )
        store = Store(self.state_path)
        events_before = store.list_events(job_id)

        rejected = self.cancel(bridge, job_id, running["revision"])

        self.assertTrue(rejected["isError"], rejected)
        self.assertEqual(rejected["structuredContent"]["status"], "PAUSED_POLICY")
        self.assertEqual(
            rejected["structuredContent"]["reason"],
            "recursive_containment_scope_invalid",
        )
        self.assertEqual(store.list_events(job_id), events_before)
        self.assertEqual(store.list_commands(job_id), [])
        self.assertTrue(_process_exists(daemon_pid))
        frames = [
            json.loads(line)
            for line in self.stdin_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual([frame["type"] for frame in frames], ["user"])

    def test_concurrent_job_cannot_replace_another_jobs_descendant_manifest(self):
        self.write_control(
            spawn_descendant=False,
            spawn_double_fork_descendant=True,
            double_fork_log=str(self.double_fork_log),
        )
        bridge = self.bridge()
        started_a = bridge.tool(
            "forge_start",
            self.start_arguments(idempotency_key="manifest-owner-a"),
        )
        self.assertFalse(started_a["isError"], started_a)
        job_a = started_a["structuredContent"]["job_id"]
        self.addCleanup(self.force_stop_job, job_a)
        running_a = self.wait_status(bridge, job_a, {"RUNNING"})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            events = Store(self.state_path).list_events(job_a)
            if (
                self.double_fork_log.exists()
                and sum(event.kind == "CLAUDE_EVENT" for event in events) >= 2
            ):
                break
            time.sleep(0.01)
        else:
            self.fail("first fake provider did not publish its escaped descendant")
        daemon_pid = json.loads(
            self.double_fork_log.read_text(encoding="utf-8").splitlines()[-1]
        )["pid"]
        self.addCleanup(
            lambda: os.kill(daemon_pid, signal.SIGKILL)
            if _process_exists(daemon_pid)
            else None
        )

        self.write_control(
            spawn_descendant=False,
            spawn_double_fork_descendant=False,
        )
        started_b = bridge.tool(
            "forge_start",
            self.start_arguments(idempotency_key="manifest-owner-b"),
        )
        self.assertFalse(started_b["isError"], started_b)
        job_b = started_b["structuredContent"]["job_id"]
        self.addCleanup(self.force_stop_job, job_b)
        self.wait_status(bridge, job_b, {"RUNNING"})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            events = Store(self.state_path).list_events(job_b)
            if sum(event.kind == "CLAUDE_EVENT" for event in events) >= 2:
                break
            time.sleep(0.01)
        else:
            self.fail("second fake provider did not publish its launch receipts")

        running_a = bridge.tool(
            "forge_status",
            {"job_id": job_a},
        )["structuredContent"]
        events_before = Store(self.state_path).list_events(job_a)
        rejected = self.cancel(bridge, job_a, running_a["revision"])

        self.assertTrue(rejected["isError"], rejected)
        self.assertEqual(rejected["structuredContent"]["status"], "PAUSED_POLICY")
        self.assertEqual(
            rejected["structuredContent"]["reason"],
            "recursive_containment_scope_invalid",
        )
        self.assertEqual(Store(self.state_path).list_events(job_a), events_before)
        self.assertEqual(Store(self.state_path).list_commands(job_a), [])
        self.assertTrue(_process_exists(daemon_pid))

    def test_term_receipt_failure_finishes_cleanup_but_never_cancels(self):
        bridge = self.bridge()
        job_id, running = self.start_running(
            bridge,
            idempotency_key="cancel-term-receipt-failure",
        )
        descendant_pid = json.loads(
            self.descendant_log.read_text(encoding="utf-8").splitlines()[0]
        )["pid"]
        with closing(sqlite3.connect(self.state_path)) as connection:
            connection.execute(
                """
                CREATE TRIGGER reject_cancellation_term_receipt
                BEFORE INSERT ON events
                WHEN NEW.kind = 'CANCELLATION_TERM_SENT'
                BEGIN
                    SELECT RAISE(ABORT, 'forced TERM receipt failure');
                END
                """
            )
            connection.commit()
        accepted = self.cancel(bridge, job_id, running["revision"])
        self.assertFalse(accepted["isError"], accepted)

        terminal = self.wait_status(bridge, job_id, {"FAILED"})
        self.assertEqual(terminal["status"], "FAILED")
        deadline = time.monotonic() + 2
        while _process_exists(descendant_pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(_process_exists(descendant_pid))
        kinds = [event.kind for event in Store(self.state_path).list_events(job_id)]
        self.assertIn("CANCELLATION_CLEANUP_FAILED", kinds)
        self.assertNotIn("CANCELLATION_TERM_SENT", kinds)
        self.assertNotIn("CANCELLED", kinds)


if __name__ == "__main__":
    unittest.main()
