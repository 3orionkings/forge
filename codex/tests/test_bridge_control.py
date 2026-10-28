import concurrent.futures
import fcntl
import hashlib
import json
import os
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tests.admission_fixture import (
    machine_headroom,
    review_admission,
    seed_wip_base,
)
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "tests" / "fixtures" / "forge_bridge_admission.py"
FAKE_CLAUDE = ROOT / "tests" / "fixtures" / "fake_claude.py"
FAKE_CONTROL_NAME = "forge-fake-claude-control.json"
sys.path.insert(0, str(ROOT / "scripts"))

from forge_lib.service import ForgeService, _repo_id  # noqa: E402
from forge_lib.processes import ProcessError  # noqa: E402
from forge_lib.state import (  # noqa: E402
    IdempotencyConflict,
    InvalidTransition,
    MAX_ATTEMPT_EVENTS,
    RevisionConflict,
    StateError,
    Store,
    canonical_json,
)


def _canonical_result(session_id, result="complete", *, model="fable"):
    return {
        "type": "result",
        "subtype": "success",
        "session_id": session_id,
        "uuid": f"result-{session_id}",
        "is_error": False,
        "duration_ms": 5,
        "duration_api_ms": 4,
        "num_turns": 1,
        "permission_denials": [],
        "result": result,
        "stop_reason": None,
        "total_cost_usd": 0.01,
        "modelUsage": {
            model: {
                "inputTokens": 1,
                "outputTokens": 1,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
                "webSearchRequests": 0,
                "costUSD": 0.01,
                "contextWindow": 200000,
                "maxOutputTokens": 32000,
            }
        },
        "usage": {
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "server_tool_use": {
                "web_search_requests": 0,
                "web_fetch_requests": 0,
            },
            "cache_creation": {
                "ephemeral_5m_input_tokens": 0,
                "ephemeral_1h_input_tokens": 0,
            },
            "service_tier": "standard",
            "iterations": [],
            "speed": "standard",
        },
    }


def _canonical_init(session_id, *, model="fable", uuid_value="event-init"):
    return {
        "type": "system",
        "subtype": "init",
        "apiKeySource": "none",
        "agents": ["claude", "Explore", "general-purpose", "Plan"],
        "analytics_disabled": False,
        "capabilities": ["interrupt_receipt_v1", "msg_lifecycle_v1"],
        "claude_code_version": "2.1.248",
        "cwd": str(ROOT),
        "fast_mode_disabled_reason": "sdk_opt_in_required",
        "fast_mode_state": "off",
        "memory_paths": {"auto": "/bounded/claude/memory"},
        "messaging_socket_path": "/tmp/cc-socks/bounded.sock",
        "tools": ["Read", "Glob", "Grep", "Agent"],
        "mcp_servers": [],
        "model": model,
        "permissionMode": "dontAsk",
        "product_feedback_disabled": False,
        "slash_commands": [],
        "output_style": "default",
        "skills": [],
        "plugins": [],
        "uuid": uuid_value,
        "session_id": session_id,
    }


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


class BridgeControlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temp_path = Path(temporary.name).resolve(strict=True)
        self.repo = self.make_repo("repo")
        self.state_path = self.temp_path / "state" / "state.sqlite3"
        self.argv_log = self.temp_path / "argv.jsonl"
        self.stdin_log = self.temp_path / "stdin.jsonl"
        self.stdin_eof_log = self.temp_path / "stdin-eof.jsonl"
        self.control_path = self.temp_path / FAKE_CONTROL_NAME
        self.write_control(control_window_seconds=0.1)
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
        seed_wip_base(repository)
        return repository.resolve(strict=True)

    def write_control(self, **updates):
        current = {}
        if hasattr(self, "control_path") and self.control_path.exists():
            current = json.loads(self.control_path.read_text(encoding="utf-8"))
        current.update(
            {
                "argv_log": str(getattr(self, "argv_log", self.temp_path / "argv.jsonl")),
                "stdin_log": str(getattr(self, "stdin_log", self.temp_path / "stdin.jsonl")),
                "stdin_eof_log": str(
                    getattr(self, "stdin_eof_log", self.temp_path / "stdin-eof.jsonl")
                ),
                "assistant_text": "bounded fake report",
                **updates,
            }
        )
        path = getattr(self, "control_path", self.temp_path / FAKE_CONTROL_NAME)
        path.write_text(
            json.dumps(current, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    def bridge(self):
        bridge = BridgeHarness(dict(self.environment))
        self.bridges.append(bridge)
        return bridge

    def close_bridges(self):
        for bridge in reversed(self.bridges):
            bridge.close()

    def make_service(self, *, repo=None, state_path=None):
        repository = repo or self.repo
        environment = dict(self.environment)
        environment["FORGE_LAUNCH_WORKSPACE"] = str(repository)
        return ForgeService(
            state_path=state_path or self.state_path,
            launch_workspace=repository,
            claude_executable=str(FAKE_CLAUDE),
            bridge_path=BRIDGE,
            environment=environment,
            _admission_machine_probe=machine_headroom,
        )

    def start_arguments(self, **overrides):
        arguments = {
            "prompt": "Review the frozen plan.",
            "cwd": str(self.repo),
            "role": "review",
            "model": "fable",
            "effort": "max",
            "idempotency_key": "task-8-job",
            "budget": {
                "max_seconds": 8,
                "max_tokens": 4096,
                "max_turns": 4096,
                "max_cost_usd": "1.25",
            },
            "admission": review_admission(module="tests.test_bridge_control"),
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
        request_hash = hashlib.sha256(
            canonical_json(declaration).encode("utf-8")
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

    def create_admitted_resume(self, service, terminal):
        attempt_context = service.store.get_attempt_context(
            terminal.current_attempt_id
        )
        attempt_context.pop("sha256")
        lane_reservation = service.store.get_bridge_lane_reservation(terminal.id)
        return service.store.create_admitted_resume_attempt(
            terminal.id,
            expected_revision=terminal.revision,
            recorded_session_id="recorded-session",
            model_override=None,
            validate_reservation=lambda attempt_id, active_reservations: service._resume_admission_evidence(
                terminal.payload,
                job_id=terminal.id,
                attempt_id=attempt_id,
                parent_attempt_id=terminal.current_attempt_id,
                parent_revision=terminal.revision,
                parent_admission_hash=service.store.get_attempt_admission(
                    terminal.current_attempt_id
                )["sha256"],
                model_override=None,
                attempt_context=attempt_context,
                lane_reservation=lane_reservation,
                active_reservations=active_reservations,
            ),
            attempt_context=attempt_context,
        )

    def wait_status(self, bridge, job_id, statuses, timeout=8):
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

    def wait_terminal(self, bridge, job_id, timeout=10):
        return self.wait_status(
            bridge,
            job_id,
            {"SUCCEEDED", "FAILED", "CANCELLED"},
            timeout=timeout,
        )

    def write_terminal_receipt(self, store, job_id, terminal_status):
        job = store.get_job(job_id)
        attempt = store.get_attempt(job.current_attempt_id)
        last_event = store.get_last_attempt_event(job_id, attempt.id)
        payload = {
            "job_id": job_id,
            "attempt_id": attempt.id,
            "event_hash": None if last_event is None else last_event.event_hash,
            "session_id": attempt.session_id,
            "observed_model": attempt.observed_model,
            "parent_tool_use_ids": list(attempt.parent_tool_use_ids),
            "terminal_status": terminal_status,
        }
        receipt = store.create_attempt_artifact(
            job_id,
            attempt.id,
            "receipt.json",
            canonical_json(payload) + "\n",
        )
        return hashlib.sha256(receipt.read_bytes()).hexdigest()

    def make_terminal_fixture(self, label):
        store = Store(self.temp_path / label / "state.sqlite3")
        job = store.create_job(
            repo_id="repo",
            idempotency_key=label,
            request_hash="hash",
            payload={},
        )
        return store, job, self.write_terminal_receipt(store, job.id, "FAILED")

    @staticmethod
    def terminal_call(operation, store, job, receipt_hash):
        if operation == "transition":
            return store.transition(
                job.id,
                expected_revision=job.revision,
                new_status="FAILED",
            )
        return store.finalize_with_receipt(
            job.id,
            expected_revision=job.revision,
            new_status="FAILED",
            receipt_name="receipt.json",
            receipt_hash=receipt_hash,
        )

    def test_steering_is_revision_checked_receipted_and_delivered_as_one_user_frame(self):
        self.write_control(control_window_seconds=4.0)
        bridge = self.bridge()
        started = bridge.tool("forge_start", self.start_arguments())
        self.assertFalse(started["isError"], started)
        job_id = started["structuredContent"]["job_id"]
        self.wait_status(bridge, job_id, {"RUNNING"})
        # The fake emits init + one assistant event before entering its bounded
        # input window. Wait for both durable receipts, then fetch the exact CAS
        # revision while no unrelated provider event can advance it.
        event_deadline = time.monotonic() + 4
        while time.monotonic() < event_deadline:
            events = Store(self.state_path).list_events(job_id)
            if sum(event.kind == "CLAUDE_EVENT" for event in events) >= 2:
                break
            time.sleep(0.01)
        else:
            self.fail("fake provider did not enter its steering input window")
        running_reply = bridge.tool("forge_status", {"job_id": job_id})
        self.assertFalse(running_reply["isError"], running_reply)
        running = running_reply["structuredContent"]
        self.assertEqual(running["status"], "RUNNING")

        accepted = bridge.tool(
            "forge_steer",
            {
                "job_id": job_id,
                "revision": running["revision"],
                "prompt": "Check the authority chain as well.",
            },
        )
        self.assertFalse(accepted["isError"], accepted)
        command_receipt = accepted["structuredContent"]
        self.assertEqual(command_receipt["status"], "accepted")
        self.assertEqual(command_receipt["sequence"], 1)
        self.assertTrue(command_receipt["command_id"])

        duplicate = bridge.tool(
            "forge_steer",
            {
                "job_id": job_id,
                "revision": running["revision"],
                "prompt": "Check the authority chain as well.",
            },
        )
        self.assertTrue(duplicate["isError"], duplicate)
        self.assertIn(
            duplicate["structuredContent"]["error_type"],
            {"RevisionConflict", "IdempotencyConflict"},
        )

        terminal = self.wait_terminal(bridge, job_id)
        self.assertEqual(terminal["status"], "SUCCEEDED", terminal)
        frames = [
            json.loads(line)
            for line in self.stdin_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual([frame["type"] for frame in frames], ["user", "user"])
        self.assertEqual(
            frames[1],
            {
                "type": "user",
                "session_id": "",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Check the authority chain as well."}
                    ],
                },
                "parent_tool_use_id": None,
                "origin": {"kind": "coordinator"},
            },
        )

        store = Store(self.state_path)
        commands = store.list_commands(job_id)
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0].status, "DELIVERED")
        attempt = store.get_attempt(store.get_job(job_id).current_attempt_id)
        self.assertEqual(attempt.command_inbox_state, "CLOSED")
        kinds = [event.kind for event in store.list_events(job_id)]
        self.assertLess(kinds.index("COMMAND_QUEUED"), kinds.index("COMMAND_DELIVERED"))

    def test_steering_is_not_starved_by_a_continuously_readable_output_burst(self):
        import forge_lib.worker as worker_module

        store = Store(self.temp_path / "burst-steering" / "state.sqlite3")
        job = store.create_job(
            repo_id="repo",
            idempotency_key="burst-steering",
            request_hash="hash",
            payload={},
        )
        claim = store.claim_dispatch(job.id, owner_pid=os.getpid())
        self.assertIsNotNone(claim)
        assert claim is not None
        self.assertTrue(
            store.claim_worker_execution(job.id, claim.token, worker_pid=os.getpid())
        )
        for status_name in ("PREFLIGHT", "RUNNING"):
            current = store.get_job(job.id)
            store.transition(
                job.id,
                expected_revision=current.revision,
                new_status=status_name,
            )
        running = store.get_job(job.id)
        command = store.queue_command(
            job.id,
            expected_revision=running.revision,
            kind="STEER",
            payload={"prompt": "deliver during the nonterminal output burst"},
            idempotency_key="burst-steering-command",
        )
        attempt_id = store.get_job(job.id).current_attempt_id
        store.create_attempt_artifact(job.id, attempt_id, "events.jsonl")
        store.create_attempt_artifact(job.id, attempt_id, "stderr.log")
        session_id = "burst-steering-session"
        init_payload = _canonical_init(
            session_id, uuid_value="burst-init"
        )
        result_payload = _canonical_result(session_id, result="RESULT_COUNT")
        child_code = "\n".join(
            (
                "import json, os, sys, threading",
                "def emit(value):",
                "    remaining = memoryview((json.dumps(value, separators=(',', ':')) + '\\n').encode())",
                "    while remaining:",
                "        remaining = remaining[os.write(1, remaining):]",
                f"session_id = {session_id!r}",
                "stop = threading.Event()",
                "produced = [0]",
                "def write_until_steered():",
                f"    emit({init_payload!r})",
                "    while not stop.is_set():",
                "        index = produced[0]",
                "        emit({'type':'assistant','session_id':session_id,'parent_tool_use_id':None,'uuid':f'burst-event-{index}','message':{'id':f'burst-{index}','type':'message','role':'assistant','model':'fable','content':[{'type':'text','text':'x' * 4096}],'usage':{'input_tokens':1,'output_tokens':1}}})",
                "        produced[0] += 1",
                "writer = threading.Thread(target=write_until_steered)",
                "writer.start()",
                "if not sys.stdin.buffer.readline():",
                "    raise SystemExit(3)",
                "stop.set()",
                "writer.join()",
                f"result_payload = {result_payload!r}",
                "result_payload['result'] = str(produced[0])",
                "emit(result_payload)",
                "sys.stdin.buffer.read()",
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
        assert provider.stdout is not None
        ready, _, _ = select.select([provider.stdout], [], [], 2)
        self.assertTrue(ready, "provider output burst was not ready for the probe")
        try:
            with store.open_attempt_artifact(
                job.id,
                attempt_id,
                "events.jsonl",
                writable=True,
            ) as (events_descriptor, _events_path), store.open_attempt_artifact(
                job.id,
                attempt_id,
                "stderr.log",
                writable=True,
            ) as (stderr_descriptor, _stderr_path):
                result, observed_model, _parents = worker_module._collect_provider(
                    provider,
                    store=store,
                    job_id=job.id,
                    session_id=session_id,
                    dispatch_token=claim.token,
                    worker_pid=os.getpid(),
                    events_descriptor=events_descriptor,
                    stderr_descriptor=stderr_descriptor,
                    deadline=time.monotonic() + 5,
                )
        finally:
            if provider.poll() is None:
                provider.kill()
            provider.wait(timeout=2)
            for stream in (provider.stdin, provider.stdout, provider.stderr):
                if stream is not None and not stream.closed:
                    stream.close()

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(observed_model, "fable")
        self.assertLess(
            int(result.data["result"]),
            worker_module.MAX_PROVIDER_EVENTS,
            "steering was not delivered while provider output stayed continuous",
        )
        commands = store.list_commands(job.id)
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0].id, command.id)
        self.assertEqual(commands[0].status, "DELIVERED")

    def test_steering_is_not_starved_behind_a_partial_nonterminal_frame(self):
        import forge_lib.worker as worker_module

        store = Store(self.temp_path / "partial-frame-steering" / "state.sqlite3")
        job = store.create_job(
            repo_id="repo",
            idempotency_key="partial-frame-steering",
            request_hash="hash",
            payload={},
        )
        claim = store.claim_dispatch(job.id, owner_pid=os.getpid())
        self.assertIsNotNone(claim)
        assert claim is not None
        self.assertTrue(
            store.claim_worker_execution(job.id, claim.token, worker_pid=os.getpid())
        )
        for status_name in ("PREFLIGHT", "RUNNING"):
            current = store.get_job(job.id)
            store.transition(
                job.id,
                expected_revision=current.revision,
                new_status=status_name,
            )
        running = store.get_job(job.id)
        command = store.queue_command(
            job.id,
            expected_revision=running.revision,
            kind="STEER",
            payload={"prompt": "release the partial frame"},
            idempotency_key="partial-frame-command",
        )
        attempt_id = store.get_job(job.id).current_attempt_id
        store.create_attempt_artifact(job.id, attempt_id, "events.jsonl")
        store.create_attempt_artifact(job.id, attempt_id, "stderr.log")
        session_id = "partial-frame-session"
        init_frame = json.dumps(
            _canonical_init(session_id, uuid_value="partial-init"),
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        assistant_frame = json.dumps(
            {
                "type": "assistant",
                "session_id": session_id,
                "uuid": "partial-assistant",
                "parent_tool_use_id": None,
                "message": {
                    "id": "partial-message",
                    "type": "message",
                    "role": "assistant",
                    "model": "fable",
                    "content": [{"type": "text", "text": "continued"}],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            },
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        result_frame = json.dumps(
            _canonical_result(session_id),
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        split = len(assistant_frame) // 2
        child_code = (
            "import os,sys;"
            f"os.write(1,{(init_frame + assistant_frame[:split])!r});"
            "sys.stdin.buffer.readline();"
            f"os.write(1,{(assistant_frame[split:] + result_frame)!r});"
            "sys.stdin.buffer.read()"
        )
        provider = subprocess.Popen(
            [sys.executable, "-u", "-c", child_code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            bufsize=0,
        )
        assert provider.stdout is not None
        ready, _, _ = select.select([provider.stdout], [], [], 2)
        self.assertTrue(ready, "partial provider frame was not ready for the probe")
        try:
            with store.open_attempt_artifact(
                job.id,
                attempt_id,
                "events.jsonl",
                writable=True,
            ) as (events_descriptor, _events_path), store.open_attempt_artifact(
                job.id,
                attempt_id,
                "stderr.log",
                writable=True,
            ) as (stderr_descriptor, _stderr_path):
                result, observed_model, _parents = worker_module._collect_provider(
                    provider,
                    store=store,
                    job_id=job.id,
                    session_id=session_id,
                    dispatch_token=claim.token,
                    worker_pid=os.getpid(),
                    events_descriptor=events_descriptor,
                    stderr_descriptor=stderr_descriptor,
                    deadline=time.monotonic() + 2,
                )
        finally:
            if provider.poll() is None:
                provider.kill()
            provider.wait(timeout=2)
            for stream in (provider.stdin, provider.stdout, provider.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        self.assertIsNotNone(result)
        self.assertEqual(observed_model, "fable")
        self.assertEqual(store.list_commands(job.id)[0].id, command.id)
        self.assertEqual(store.list_commands(job.id)[0].status, "DELIVERED")

    def test_terminal_result_closes_provider_stdin_before_drain_and_reap(self):
        self.write_control(
            control_window_seconds=0,
            wait_for_stdin_eof_after_result=True,
            stdin_eof_timeout_seconds=4,
        )
        bridge = self.bridge()
        started = bridge.tool(
            "forge_start",
            self.start_arguments(
                idempotency_key="terminal-eof",
                budget={
                    "max_seconds": 3,
                    "max_tokens": 4096,
                    "max_turns": 4096,
                    "max_cost_usd": "1.25",
                },
            ),
        )
        self.assertFalse(started["isError"], started)
        terminal = self.wait_terminal(
            bridge,
            started["structuredContent"]["job_id"],
            timeout=6,
        )
        self.assertEqual(terminal["status"], "SUCCEEDED", terminal)
        eof_receipts = [
            json.loads(line)
            for line in self.stdin_eof_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(eof_receipts, [{"stdin_eof_after_result": True}])

    def test_command_claim_is_monotonic_idempotent_and_not_redelivered_after_restart(self):
        store = Store(self.temp_path / "command-state" / "state.sqlite3")
        job = store.create_job(
            repo_id="repo",
            idempotency_key="job",
            request_hash="hash",
            payload={},
        )
        claim = store.claim_dispatch(job.id, owner_pid=os.getpid())
        self.assertIsNotNone(claim)
        assert claim is not None
        self.assertTrue(
            store.claim_worker_execution(
                job.id,
                claim.token,
                worker_pid=os.getpid(),
            )
        )
        current = store.get_job(job.id)
        store.transition(job.id, expected_revision=current.revision, new_status="PREFLIGHT")
        current = store.get_job(job.id)
        store.transition(job.id, expected_revision=current.revision, new_status="RUNNING")
        running = store.get_job(job.id)

        first = store.queue_command(
            job.id,
            expected_revision=running.revision,
            kind="STEER",
            payload={"prompt": "first"},
            idempotency_key="steer-1",
        )
        self.assertEqual(first.sequence, 1)
        self.assertEqual(store.list_events(job.id)[-1].kind, "COMMAND_QUEUED")
        claimed = store.claim_next_command(
            job.id,
            dispatch_token=claim.token,
            worker_pid=os.getpid(),
        )
        self.assertEqual(claimed.id, first.id)
        self.assertEqual(claimed.status, "DELIVERING")

        restarted = Store(store.path)
        self.assertIsNone(
            restarted.claim_next_command(
                job.id,
                dispatch_token=claim.token,
                worker_pid=os.getpid(),
            )
        )
        delivered = restarted.mark_command_delivered(
            job.id,
            first.id,
            dispatch_token=claim.token,
            worker_pid=os.getpid(),
        )
        self.assertEqual(delivered.status, "DELIVERED")

        current = restarted.get_job(job.id)
        second = restarted.queue_command(
            job.id,
            expected_revision=current.revision,
            kind="STEER",
            payload={"prompt": "second"},
            idempotency_key="steer-2",
        )
        self.assertEqual(second.sequence, 2)
        current = restarted.get_job(job.id)
        with self.assertRaises(IdempotencyConflict):
            restarted.queue_command(
                job.id,
                expected_revision=current.revision,
                kind="STEER",
                payload={"prompt": "duplicate"},
                idempotency_key="steer-1",
            )
        with self.assertRaises(RevisionConflict):
            restarted.queue_command(
                job.id,
                expected_revision=running.revision,
                kind="STEER",
                payload={"prompt": "stale"},
                idempotency_key="steer-stale",
            )

        failed_at_close = restarted.close_command_inbox(
            job.id,
            dispatch_token=claim.token,
            worker_pid=os.getpid(),
        )
        self.assertEqual([command.id for command in failed_at_close], [second.id])
        self.assertEqual(
            restarted.list_commands(job.id)[1].status,
            "DELIVERY_FAILED",
        )
        current = restarted.get_job(job.id)
        with self.assertRaises(InvalidTransition):
            restarted.queue_command(
                job.id,
                expected_revision=current.revision,
                kind="STEER",
                payload={"prompt": "after close"},
                idempotency_key="steer-after-close",
            )

        created = restarted.create_job(
            repo_id="repo",
            idempotency_key="not-running",
            request_hash="hash-2",
            payload={},
        )
        with self.assertRaises(InvalidTransition):
            restarted.queue_command(
                created.id,
                expected_revision=created.revision,
                kind="STEER",
                payload={"prompt": "too early"},
                idempotency_key="steer-early",
            )

    def test_resume_creates_a_child_and_uses_only_the_recorded_session(self):
        bridge = self.bridge()
        started = bridge.tool("forge_start", self.start_arguments())
        self.assertFalse(started["isError"], started)
        job_id = started["structuredContent"]["job_id"]
        first_terminal = self.wait_terminal(bridge, job_id)
        self.assertEqual(first_terminal["status"], "SUCCEEDED", first_terminal)
        recorded_session = first_terminal["session_id"]
        first_attempt = first_terminal["attempt_id"]

        revised_prompt = "Apply the bounded review fix from the durable attempt context."
        resumed = bridge.tool(
            "forge_resume",
            {
                "job_id": job_id,
                "revision": first_terminal["revision"],
                "prompt": revised_prompt,
                "idempotency_key": "resume-provider-prompt-v1",
            },
        )
        self.assertFalse(resumed["isError"], resumed)
        self.assertNotEqual(resumed["structuredContent"]["attempt_id"], first_attempt)
        second_terminal = self.wait_terminal(bridge, job_id)
        self.assertEqual(second_terminal["status"], "SUCCEEDED", second_terminal)
        self.assertEqual(second_terminal["session_id"], recorded_session)

        store = Store(self.state_path)
        attempts = store.list_attempts(job_id)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[1].parent_attempt_id, attempts[0].id)
        self.assertEqual(attempts[1].resume_session_id, recorded_session)

        invocations = [
            json.loads(line)
            for line in self.argv_log.read_text(encoding="utf-8").splitlines()
        ]
        provider_commands = [argv for argv in invocations if "-p" in argv]
        self.assertEqual(len(provider_commands), 2)
        resumed_argv = provider_commands[1]
        self.assertEqual(
            resumed_argv[resumed_argv.index("--resume") + 1],
            recorded_session,
        )
        for forbidden in ("--session-id", "--continue", "--last"):
            self.assertNotIn(forbidden, resumed_argv)
        frames = [
            json.loads(line)
            for line in self.stdin_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(frames), 2)
        self.assertEqual(
            frames[1]["message"]["content"],
            [{"type": "text", "text": revised_prompt}],
        )

    def test_resume_preserves_parent_attempt_evidence_and_result_resolves_child(self):
        bridge = self.bridge()
        started = bridge.tool(
            "forge_start",
            self.start_arguments(idempotency_key="attempt-evidence"),
        )
        self.assertFalse(started["isError"], started)
        job_id = started["structuredContent"]["job_id"]
        first_terminal = self.wait_terminal(bridge, job_id)
        self.assertEqual(first_terminal["status"], "SUCCEEDED", first_terminal)
        first_attempt_id = first_terminal["attempt_id"]
        first_result = bridge.tool("forge_result", {"job_id": job_id})
        self.assertFalse(first_result["isError"], first_result)
        first_artifacts = first_result["structuredContent"]["artifacts"]
        first_paths = {
            label: Path(receipt["path"])
            for label, receipt in first_artifacts.items()
        }
        for path in first_paths.values():
            self.assertEqual(path.parent.name, first_attempt_id)
        parent_bytes = {
            label: path.read_bytes() for label, path in first_paths.items()
        }
        parent_receipt_hash = Store(self.state_path).get_attempt(
            first_attempt_id
        ).terminal_receipt_hash
        with self.assertRaises(InvalidTransition):
            Store(self.state_path).create_attempt_artifact(
                job_id,
                first_attempt_id,
                "report.txt",
                "must remain immutable",
            )

        resumed = bridge.tool(
            "forge_resume",
            {"job_id": job_id, "revision": first_terminal["revision"]},
        )
        self.assertFalse(resumed["isError"], resumed)
        second_terminal = self.wait_terminal(bridge, job_id)
        self.assertEqual(second_terminal["status"], "SUCCEEDED", second_terminal)
        second_attempt_id = second_terminal["attempt_id"]
        self.assertNotEqual(second_attempt_id, first_attempt_id)
        second_result = bridge.tool("forge_result", {"job_id": job_id})
        self.assertFalse(second_result["isError"], second_result)
        second_paths = {
            label: Path(receipt["path"])
            for label, receipt in second_result["structuredContent"]["artifacts"].items()
        }
        for label, path in second_paths.items():
            self.assertEqual(path.parent.name, second_attempt_id)
            self.assertNotEqual(path, first_paths[label])
        for label, path in first_paths.items():
            self.assertEqual(path.read_bytes(), parent_bytes[label])
        parent_attempt = Store(self.state_path).get_attempt(first_attempt_id)
        self.assertEqual(parent_attempt.terminal_receipt_hash, parent_receipt_hash)
        self.assertEqual(
            hashlib.sha256(first_paths["receipt"].read_bytes()).hexdigest(),
            parent_receipt_hash,
        )
        run_root = self.state_path.parent / "runs" / job_id
        self.assertTrue((run_root / first_attempt_id / "claude-settings.json").is_file())
        self.assertTrue((run_root / second_attempt_id / "claude-settings.json").is_file())

    def test_resume_spawn_failure_retries_the_same_recorded_child_attempt(self):
        service, terminal = self._seed_terminal_job(self.repo, "resume-retry")
        with mock.patch(
            "forge_lib.service.spawn_detached_worker",
            side_effect=ProcessError("offline spawn failure"),
        ):
            failed = service.execute(
                "forge_resume",
                {"job_id": terminal.id, "revision": terminal.revision},
            )
        self.assertTrue(failed.is_error, failed)
        after_failure = service.store.get_job(terminal.id)
        attempts = service.store.list_attempts(terminal.id)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(after_failure.status, "CREATED")
        self.assertEqual(attempts[1].dispatch_state, "UNCLAIMED")
        self.assertEqual(attempts[1].resume_session_id, "recorded-session")

        with mock.patch(
            "forge_lib.service.spawn_detached_worker",
            side_effect=_bound_worker_launch,
        ) as spawn:
            retried = service.execute(
                "forge_resume",
                {"job_id": terminal.id, "revision": after_failure.revision},
            )
        self.assertFalse(retried.is_error, retried)
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(len(service.store.list_attempts(terminal.id)), 2)
        self.assertEqual(
            retried.body["attempt_id"],
            attempts[1].id,
        )

    def test_resume_local_preparation_failure_redispatches_same_child_without_provider_effect(self):
        import forge_lib.worker as worker_module

        service, terminal = self._seed_terminal_job(self.repo, "resume-preparation-retry")
        child = self.create_admitted_resume(service, terminal)
        claim = service.store.claim_dispatch(terminal.id, owner_pid=os.getpid())
        self.assertIsNotNone(claim)
        assert claim is not None
        service.store.record_worker_pid(
            terminal.id,
            os.getpid(),
            _dispatch_claim=claim,
        )
        with (
            mock.patch.dict(os.environ, self.environment, clear=True),
            mock.patch.object(
                worker_module,
                "preflight",
                side_effect=RuntimeError("offline local preparation failure"),
            ),
            mock.patch.object(worker_module, "spawn_provider") as provider_spawn,
        ):
            worker_result = worker_module.run_worker(
                terminal.id,
                dispatch_token=claim.token,
                state_path=service.store.path,
                dispatch_role=service.store.get_job(terminal.id).payload["role"],
            )
        self.assertEqual(worker_result, 1)
        self.assertEqual(provider_spawn.call_count, 0)
        after_failure = service.store.get_job(terminal.id)
        current_child = service.store.get_attempt(after_failure.current_attempt_id)
        self.assertEqual(after_failure.status, "CREATED")
        self.assertEqual(current_child.id, child.id)
        self.assertEqual(current_child.dispatch_state, "UNCLAIMED")
        self.assertEqual(current_child.resume_session_id, "recorded-session")
        self.assertIsNone(current_child.session_id)
        self.assertIsNone(current_child.terminal_receipt_hash)

        with mock.patch(
            "forge_lib.service.spawn_detached_worker",
            side_effect=_bound_worker_launch,
        ) as spawn:
            retried = service.execute(
                "forge_resume",
                {"job_id": terminal.id, "revision": after_failure.revision},
            )
        self.assertFalse(retried.is_error, retried)
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(len(service.store.list_attempts(terminal.id)), 2)
        self.assertEqual(retried.body["attempt_id"], child.id)

    def test_resume_pre_popen_launch_failure_redispatches_the_same_child(self):
        import forge_lib.worker as worker_module

        service, terminal = self._seed_terminal_job(self.repo, "resume-pre-popen-retry")
        child = self.create_admitted_resume(service, terminal)
        claim = service.store.claim_dispatch(terminal.id, owner_pid=os.getpid())
        self.assertIsNotNone(claim)
        assert claim is not None
        service.store.record_worker_pid(
            terminal.id,
            os.getpid(),
            _dispatch_claim=claim,
        )

        def fail_before_popen(*_args, **kwargs):
            self.assertIn("before_popen", kwargs)
            raise ProcessError("offline failure before Popen")

        with (
            mock.patch.dict(os.environ, self.environment, clear=True),
            mock.patch.object(
                worker_module,
                "spawn_provider",
                side_effect=fail_before_popen,
            ) as provider_spawn,
        ):
            worker_result = worker_module.run_worker(
                terminal.id,
                dispatch_token=claim.token,
                state_path=service.store.path,
                dispatch_role=service.store.get_job(terminal.id).payload["role"],
            )
        self.assertEqual(worker_result, 1)
        self.assertEqual(provider_spawn.call_count, 1)
        after_failure = service.store.get_job(terminal.id)
        current_child = service.store.get_attempt(after_failure.current_attempt_id)
        self.assertEqual(after_failure.status, "CREATED")
        self.assertEqual(current_child.id, child.id)
        self.assertEqual(current_child.dispatch_state, "UNCLAIMED")
        self.assertEqual(current_child.resume_session_id, "recorded-session")
        self.assertIsNone(current_child.session_id)
        self.assertIsNone(current_child.provider_pgid)
        self.assertEqual(len(service.store.list_attempts(terminal.id)), 2)

    def test_post_popen_cleanup_failure_keeps_the_provider_handle_and_job_nonterminal(self):
        import forge_lib.processes as process_module
        import forge_lib.worker as worker_module

        service = self.make_service(
            state_path=self.temp_path / "post-popen-cleanup" / "state.sqlite3"
        )
        arguments = self.start_arguments(idempotency_key="post-popen-cleanup")
        job = self.create_admitted_job(service, arguments)
        claim = service.store.claim_dispatch(job.id, owner_pid=os.getpid())
        self.assertIsNotNone(claim)
        assert claim is not None
        service.store.record_worker_pid(
            job.id,
            os.getpid(),
            _dispatch_claim=claim,
        )
        provider = mock.Mock(name="post-popen-provider")

        def fail_after_popen(*_args, **kwargs):
            kwargs["before_popen"]()
            raise process_module.ProviderLaunchError(
                "repository changed and cleanup is unproven",
                process=provider,
            )

        with (
            mock.patch.dict(os.environ, self.environment, clear=True),
            mock.patch.object(
                worker_module,
                "spawn_provider",
                side_effect=fail_after_popen,
            ),
            mock.patch.object(
                worker_module,
                "terminate_process_group",
                side_effect=ProcessError("still live"),
            ) as terminate,
        ):
            result = worker_module.run_worker(
                job.id,
                dispatch_token=claim.token,
                state_path=service.store.path,
                dispatch_role=service.store.get_job(job.id).payload["role"],
            )

        self.assertEqual(result, 1)
        terminate.assert_called_once_with(provider)
        current = service.store.get_job(job.id)
        self.assertNotIn(current.status, {"SUCCEEDED", "FAILED", "CANCELLED"})
        attempt = service.store.get_attempt(current.current_attempt_id)
        self.assertIsNone(attempt.terminal_receipt_hash)

    def test_worker_revalidates_repository_after_preflight_before_provider_spawn(self):
        import forge_lib.worker as worker_module

        service = self.make_service(
            state_path=self.temp_path / "prelaunch-drift" / "state.sqlite3"
        )
        arguments = self.start_arguments(idempotency_key="prelaunch-drift")
        job = self.create_admitted_job(service, arguments)
        claim = service.store.claim_dispatch(job.id, owner_pid=os.getpid())
        self.assertIsNotNone(claim)
        assert claim is not None
        service.store.record_worker_pid(
            job.id,
            os.getpid(),
            _dispatch_claim=claim,
        )
        real_preflight = worker_module.preflight

        def dirty_after_preflight(*args, **kwargs):
            receipt = real_preflight(*args, **kwargs)
            (self.repo / "tracked.txt").write_text("changed during preflight\n", encoding="utf-8")
            return receipt

        with (
            mock.patch.dict(os.environ, self.environment, clear=True),
            mock.patch.object(
                worker_module,
                "preflight",
                side_effect=dirty_after_preflight,
            ),
            mock.patch.object(worker_module, "spawn_provider") as spawn,
        ):
            result = worker_module.run_worker(
                job.id,
                dispatch_token=claim.token,
                state_path=service.store.path,
                dispatch_role=service.store.get_job(job.id).payload["role"],
            )
        self.assertEqual(result, 1)
        self.assertEqual(spawn.call_count, 0)

    def test_start_pins_complete_repository_path_identities(self):
        service = self.make_service(
            state_path=self.temp_path / "identity-baseline" / "state.sqlite3"
        )
        _identity, payload = service._normalized_start(
            self.start_arguments(idempotency_key="identity-baseline")
        )
        baseline = payload["baseline"]
        self.assertEqual(
            set(baseline),
            {
                "head",
                "status_fingerprint",
                "tracked_content_fingerprint",
                "top_level",
                "common_dir",
                "git_marker",
                "git_dir",
                "git_marker_content_hash",
                "git_commondir_file",
                "git_commondir_file_device",
                "git_commondir_file_inode",
                "git_commondir_file_owner",
                "git_commondir_file_type",
                "git_commondir_file_content_hash",
                "git_worktree_backpointer",
                "git_worktree_backpointer_device",
                "git_worktree_backpointer_inode",
                "git_worktree_backpointer_owner",
                "git_worktree_backpointer_type",
                "git_worktree_backpointer_content_hash",
                "worktree_device",
                "worktree_inode",
                "worktree_owner",
                "worktree_type",
                "git_marker_device",
                "git_marker_inode",
                "git_marker_owner",
                "git_marker_type",
                "git_dir_device",
                "git_dir_inode",
                "git_dir_owner",
                "git_dir_type",
                "common_dir_device",
                "common_dir_inode",
                "common_dir_owner",
                "common_dir_type",
            },
        )
        self.assertEqual(baseline["git_dir"], baseline["git_marker"])
        self.assertIsNone(baseline["git_marker_content_hash"])
        self.assertIsNone(baseline["git_commondir_file"])
        self.assertIsNone(baseline["git_worktree_backpointer"])
        self.assertEqual(len(baseline["tracked_content_fingerprint"]), 64)

    def test_attempt_lock_identity_is_persisted_when_the_attempt_is_created(self):
        store = Store(self.temp_path / "durable-lock-identity" / "state.sqlite3")
        job = store.create_job(
            repo_id="repo",
            idempotency_key="durable-lock-identity",
            request_hash="hash",
            payload={},
        )
        with store.connection() as connection:
            identity = connection.execute(
                """
                SELECT device_hex, inode_hex, owner
                FROM attempt_evidence_locks
                WHERE attempt_id = ?
                """,
                (job.current_attempt_id,),
            ).fetchone()
        self.assertIsNotNone(identity)
        lock_path = (
            store.root_path
            / "attempt-locks"
            / f"{job.id}-{job.current_attempt_id}.lock"
        )
        information = lock_path.stat()
        self.assertEqual(
            tuple(identity),
            (
                format(information.st_dev, "x"),
                format(information.st_ino, "x"),
                information.st_uid,
            ),
        )
        self.assertEqual(stat.S_IMODE(information.st_mode), 0o600)
        self.assertEqual(information.st_nlink, 1)

    def test_resume_and_generic_child_attempts_receive_distinct_lock_identities(self):
        store = Store(self.temp_path / "child-lock-identities" / "state.sqlite3")
        job = store.create_job(
            repo_id="repo",
            idempotency_key="child-lock-identities",
            request_hash="hash",
            payload={},
        )
        session_id = "11111111-1111-4111-8111-111111111111"
        store.record_provider_identity(job.id, session_id=session_id)
        first_terminal_revision = store.transition(
            job.id,
            expected_revision=job.revision,
            new_status="FAILED",
        )
        resumed = store.create_resume_attempt(
            job.id,
            expected_revision=first_terminal_revision,
            recorded_session_id=session_id,
        )
        second_terminal_revision = store.transition(
            job.id,
            expected_revision=resumed.revision,
            new_status="FAILED",
        )
        generic = store.create_new_attempt(
            job.id,
            expected_revision=second_terminal_revision,
        )

        attempts = store.list_attempts(job.id)
        self.assertEqual([attempt.id for attempt in attempts], [
            job.current_attempt_id,
            resumed.id,
            generic.id,
        ])
        with store.connection() as connection:
            rows = connection.execute(
                """
                SELECT attempt_id, device_hex, inode_hex, owner
                FROM attempt_evidence_locks
                WHERE attempt_id IN (?, ?, ?)
                ORDER BY attempt_id
                """,
                tuple(attempt.id for attempt in attempts),
            ).fetchall()
        self.assertEqual(len(rows), 3)
        self.assertEqual(
            len({(row["device_hex"], row["inode_hex"]) for row in rows}),
            3,
        )
        for row in rows:
            lock_path = (
                store.root_path
                / "attempt-locks"
                / f"{job.id}-{row['attempt_id']}.lock"
            )
            information = lock_path.stat()
            self.assertEqual(row["device_hex"], format(information.st_dev, "x"))
            self.assertEqual(row["inode_hex"], format(information.st_ino, "x"))
            self.assertEqual(row["owner"], information.st_uid)

    def test_lock_path_replacement_cannot_split_writer_and_finalizer_leases(self):
        store = Store(self.temp_path / "lock-split-race" / "state.sqlite3")
        job = store.create_job(
            repo_id="repo",
            idempotency_key="lock-split-race",
            request_hash="hash",
            payload={},
        )
        attempt_id = job.current_attempt_id
        receipt_hash = self.write_terminal_receipt(store, job.id, "FAILED")
        lock_path = (
            store.root_path
            / "attempt-locks"
            / f"{job.id}-{attempt_id}.lock"
        )
        # Task-8 implementations before durable lock pinning created this lazily.
        with store.attempt_evidence_lease(job.id, attempt_id, exclusive=False):
            pass
        displaced = lock_path.with_name(lock_path.name + ".displaced")
        source = (
            "import sys\n"
            f"sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
            "from forge_lib.state import StateError, Store\n"
            f"store = Store({str(store.path)!r})\n"
            "try:\n"
            f"    store.finalize_with_receipt({job.id!r}, "
            f"expected_revision={job.revision!r}, new_status='FAILED', "
            f"receipt_name='receipt.json', receipt_hash={receipt_hash!r})\n"
            "except StateError:\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(7)\n"
        )
        try:
            with self.assertRaises(StateError):
                with store.attempt_evidence_lease(
                    job.id,
                    attempt_id,
                    exclusive=False,
                ):
                    lock_path.rename(displaced)
                    lock_path.write_bytes(b"")
                    lock_path.chmod(0o600)
                    completed = subprocess.run(
                        [sys.executable, "-c", source],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=5,
                        check=False,
                    )
                    self.assertEqual(
                        completed.returncode,
                        0,
                        completed.stderr.decode("utf-8", errors="replace"),
                    )
        finally:
            if lock_path.exists() or lock_path.is_symlink():
                lock_path.unlink()
            if displaced.exists():
                displaced.rename(lock_path)
        self.assertEqual(store.get_job(job.id).status, "CREATED")

    def test_attempt_lock_symlink_and_hardlink_substitution_fail_closed(self):
        store = Store(self.temp_path / "hostile-lock-path" / "state.sqlite3")
        job = store.create_job(
            repo_id="repo",
            idempotency_key="hostile-lock-path",
            request_hash="hash",
            payload={},
        )
        attempt_id = job.current_attempt_id
        with store.attempt_evidence_lease(job.id, attempt_id, exclusive=False):
            pass
        lock_path = (
            store.root_path
            / "attempt-locks"
            / f"{job.id}-{attempt_id}.lock"
        )
        hardlink = lock_path.with_name(lock_path.name + ".hardlink")
        os.link(lock_path, hardlink)
        try:
            with self.assertRaises(StateError):
                with store.attempt_evidence_lease(
                    job.id,
                    attempt_id,
                    exclusive=False,
                ):
                    pass
        finally:
            hardlink.unlink()

        displaced = lock_path.with_name(lock_path.name + ".original")
        lock_path.rename(displaced)
        lock_path.symlink_to(displaced.name)
        try:
            with self.assertRaises(StateError):
                with store.attempt_evidence_lease(
                    job.id,
                    attempt_id,
                    exclusive=False,
                ):
                    pass
        finally:
            lock_path.unlink()
            displaced.rename(lock_path)

    def test_attempt_lock_release_detects_in_lease_hardlink_and_symlink_races(self):
        for substitution in ("hardlink", "symlink"):
            with self.subTest(substitution=substitution):
                store = Store(
                    self.temp_path / f"held-lock-{substitution}" / "state.sqlite3"
                )
                job = store.create_job(
                    repo_id="repo",
                    idempotency_key=f"held-lock-{substitution}",
                    request_hash="hash",
                    payload={},
                )
                attempt_id = job.current_attempt_id
                lock_path = (
                    store.root_path
                    / "attempt-locks"
                    / f"{job.id}-{attempt_id}.lock"
                )
                displaced = lock_path.with_name(lock_path.name + ".held")
                external = lock_path.with_name(lock_path.name + ".external")
                external.write_bytes(b"external")
                external.chmod(0o600)
                try:
                    with self.assertRaises(StateError):
                        with store.attempt_evidence_lease(
                            job.id,
                            attempt_id,
                            exclusive=False,
                        ):
                            lock_path.rename(displaced)
                            if substitution == "hardlink":
                                os.link(external, lock_path)
                            else:
                                lock_path.symlink_to(external.name)
                finally:
                    if lock_path.exists() or lock_path.is_symlink():
                        lock_path.unlink()
                    if displaced.exists():
                        displaced.rename(lock_path)
                    if external.exists():
                        external.unlink()

    def test_lock_replacement_immediately_before_terminal_mutation_rolls_back(self):
        for operation in ("transition", "receipt"):
            with self.subTest(operation=operation):
                store = Store(
                    self.temp_path / f"terminal-lock-{operation}" / "state.sqlite3"
                )
                job = store.create_job(
                    repo_id="repo",
                    idempotency_key=f"terminal-lock-{operation}",
                    request_hash="hash",
                    payload={},
                )
                lock_path = (
                    store.root_path
                    / "attempt-locks"
                    / f"{job.id}-{job.current_attempt_id}.lock"
                )
                displaced = lock_path.with_name(lock_path.name + ".held")
                receipt_hash = self.write_terminal_receipt(store, job.id, "FAILED")

                def replace_lock(_connection, _job_id, _attempt):
                    lock_path.rename(displaced)
                    lock_path.write_bytes(b"replacement")
                    lock_path.chmod(0o600)

                try:
                    with mock.patch.object(
                        store,
                        "_require_terminal_inbox_settled",
                        side_effect=replace_lock,
                    ), self.assertRaises(StateError):
                        if operation == "transition":
                            store.transition(
                                job.id,
                                expected_revision=job.revision,
                                new_status="FAILED",
                            )
                        else:
                            store.finalize_with_receipt(
                                job.id,
                                expected_revision=job.revision,
                                new_status="FAILED",
                                receipt_name="receipt.json",
                                receipt_hash=receipt_hash,
                            )
                finally:
                    if lock_path.exists() or lock_path.is_symlink():
                        lock_path.unlink()
                    if displaced.exists():
                        displaced.rename(lock_path)
                self.assertEqual(store.get_job(job.id).status, "CREATED")

    def test_lock_replacement_after_validation_before_commit_rolls_back(self):
        for operation in ("transition", "receipt"):
            with self.subTest(operation=operation):
                store = Store(
                    self.temp_path
                    / f"post-validation-terminal-lock-{operation}"
                    / "state.sqlite3"
                )
                job = store.create_job(
                    repo_id="repo",
                    idempotency_key=f"post-validation-terminal-lock-{operation}",
                    request_hash="hash",
                    payload={},
                )
                attempt_id = job.current_attempt_id
                receipt_hash = self.write_terminal_receipt(store, job.id, "FAILED")
                receipt_path = store.attempt_artifact_path(
                    job.id,
                    attempt_id,
                    "receipt.json",
                )
                before_job = store.get_job(job.id)
                before_attempt = store.get_attempt(attempt_id)
                before_receipt = receipt_path.read_bytes()
                lock_path = (
                    store.root_path
                    / "attempt-locks"
                    / f"{job.id}-{attempt_id}.lock"
                )
                displaced = lock_path.with_name(lock_path.name + ".validated")
                real_validate = store._validate_evidence_lease
                validations = 0

                def validate_then_replace(*args, **kwargs):
                    nonlocal validations
                    result = real_validate(*args, **kwargs)
                    validations += 1
                    if validations == 1:
                        lock_path.rename(displaced)
                        lock_path.write_bytes(b"replacement")
                        lock_path.chmod(0o600)
                    return result

                try:
                    with mock.patch.object(
                        store,
                        "_validate_evidence_lease",
                        side_effect=validate_then_replace,
                    ), self.assertRaises(StateError):
                        if operation == "transition":
                            store.transition(
                                job.id,
                                expected_revision=before_job.revision,
                                new_status="FAILED",
                            )
                        else:
                            store.finalize_with_receipt(
                                job.id,
                                expected_revision=before_job.revision,
                                new_status="FAILED",
                                receipt_name="receipt.json",
                                receipt_hash=receipt_hash,
                            )
                finally:
                    if lock_path.exists() or lock_path.is_symlink():
                        lock_path.unlink()
                    if displaced.exists():
                        displaced.rename(lock_path)
                self.assertEqual(validations, 1)
                self.assertEqual(store.get_job(job.id), before_job)
                self.assertEqual(store.get_attempt(attempt_id), before_attempt)
                self.assertEqual(receipt_path.read_bytes(), before_receipt)

    def test_terminal_commit_never_reports_a_post_commit_lock_replacement(self):
        for operation in ("transition", "receipt"):
            with self.subTest(operation=operation):
                store, job, receipt_hash = self.make_terminal_fixture(
                    f"post-commit-terminal-lock-{operation}"
                )
                attempt_id = job.current_attempt_id
                lock_path = (
                    store.root_path
                    / "attempt-locks"
                    / f"{job.id}-{attempt_id}.lock"
                )
                displaced = lock_path.with_name(lock_path.name + ".committing")
                real_validate = store._validate_evidence_lease
                validations = 0

                def replace_after_final_validation(*args, **kwargs):
                    nonlocal validations
                    result = real_validate(*args, **kwargs)
                    validations += 1
                    if validations == 2:
                        lock_path.rename(displaced)
                        lock_path.write_bytes(b"replacement")
                        lock_path.chmod(0o600)
                    return result

                reported_error = None
                new_revision = None
                try:
                    with mock.patch.object(
                        store,
                        "_validate_evidence_lease",
                        side_effect=replace_after_final_validation,
                    ):
                        try:
                            new_revision = self.terminal_call(
                                operation,
                                store,
                                job,
                                receipt_hash,
                            )
                        except StateError as error:
                            reported_error = error
                finally:
                    if lock_path.exists() or lock_path.is_symlink():
                        lock_path.unlink()
                    if displaced.exists():
                        displaced.rename(lock_path)

                self.assertEqual(validations, 2)
                self.assertIsNone(
                    reported_error,
                    "a committed terminal mutation must not report a release error",
                )
                self.assertEqual(new_revision, job.revision + 1)
                self.assertEqual(store.get_job(job.id).status, "FAILED")
                self.assertEqual(store.get_attempt(attempt_id).status, "FAILED")

    def test_lock_directory_replacement_after_final_check_rolls_back(self):
        for operation in ("transition", "receipt"):
            with self.subTest(operation=operation):
                store, job, receipt_hash = self.make_terminal_fixture(
                    f"terminal-lock-directory-{operation}"
                )
                attempt_id = job.current_attempt_id
                receipt_path = store.attempt_artifact_path(
                    job.id,
                    attempt_id,
                    "receipt.json",
                )
                before_job = store.get_job(job.id)
                before_attempt = store.get_attempt(attempt_id)
                before_receipt = receipt_path.read_bytes()
                lock_directory = store.root_path / "attempt-locks"
                displaced = store.root_path / "attempt-locks.validated"
                real_validate = store._validate_evidence_lease
                validations = 0

                def replace_directory_after_final_validation(*args, **kwargs):
                    nonlocal validations
                    result = real_validate(*args, **kwargs)
                    validations += 1
                    if validations == 2:
                        lock_directory.rename(displaced)
                        lock_directory.mkdir(mode=0o700)
                    return result

                try:
                    with mock.patch.object(
                        store,
                        "_validate_evidence_lease",
                        side_effect=replace_directory_after_final_validation,
                    ), self.assertRaises(StateError):
                        self.terminal_call(operation, store, job, receipt_hash)
                finally:
                    if lock_directory.exists():
                        lock_directory.rmdir()
                    if displaced.exists():
                        displaced.rename(lock_directory)

                self.assertEqual(validations, 2)
                self.assertEqual(store.get_job(job.id), before_job)
                self.assertEqual(store.get_attempt(attempt_id), before_attempt)
                self.assertEqual(receipt_path.read_bytes(), before_receipt)

    def test_writer_waits_for_terminal_commit_after_exclusive_lease_release(self):
        for operation in ("transition", "receipt"):
            with self.subTest(operation=operation):
                store, job, receipt_hash = self.make_terminal_fixture(
                    f"unlock-before-terminal-commit-{operation}"
                )
                attempt_id = job.current_attempt_id
                exclusive_unlocked = threading.Event()
                allow_commit = threading.Event()
                shared_acquired = threading.Event()
                real_flock = fcntl.flock
                exclusive_descriptor = None

                def synchronize_unlock(descriptor, operation_flag):
                    nonlocal exclusive_descriptor
                    result = real_flock(descriptor, operation_flag)
                    if operation_flag == fcntl.LOCK_EX:
                        exclusive_descriptor = descriptor
                    elif operation_flag == fcntl.LOCK_SH:
                        shared_acquired.set()
                    elif (
                        operation_flag == fcntl.LOCK_UN
                        and descriptor == exclusive_descriptor
                    ):
                        exclusive_unlocked.set()
                        if not allow_commit.wait(timeout=3):
                            raise AssertionError("terminal commit was not released")
                    return result

                def finalize():
                    return self.terminal_call(operation, store, job, receipt_hash)

                def write_after_unlock():
                    with store.attempt_evidence_lease(
                        job.id,
                        attempt_id,
                        exclusive=False,
                    ) as lease:
                        return store.create_attempt_artifact(
                            job.id,
                            attempt_id,
                            "after-unlock.txt",
                            "must not publish",
                            _lease=lease,
                        )

                with mock.patch(
                    "forge_lib.state.fcntl.flock",
                    side_effect=synchronize_unlock,
                ), concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                    finalizer = executor.submit(finalize)
                    self.assertTrue(exclusive_unlocked.wait(timeout=2))
                    writer = executor.submit(write_after_unlock)
                    self.assertTrue(shared_acquired.wait(timeout=2))
                    writer_was_blocked = False
                    try:
                        writer.result(timeout=0.2)
                    except concurrent.futures.TimeoutError:
                        writer_was_blocked = True
                    except InvalidTransition:
                        pass
                    finally:
                        allow_commit.set()

                    self.assertTrue(
                        writer_was_blocked,
                        "the shared writer did not wait on the open terminal transaction",
                    )
                    self.assertEqual(finalizer.result(timeout=2), job.revision + 1)
                    with self.assertRaises(InvalidTransition):
                        writer.result(timeout=2)

                self.assertEqual(store.get_job(job.id).status, "FAILED")
                self.assertFalse(
                    store.attempt_artifact_path(
                        job.id,
                        attempt_id,
                        "after-unlock.txt",
                    ).exists()
                )

    def test_terminal_commit_failure_rolls_back_after_unlock_and_unblocks_writer(self):
        for operation in ("transition", "receipt"):
            with self.subTest(operation=operation):
                store, job, receipt_hash = self.make_terminal_fixture(
                    f"terminal-commit-failure-{operation}"
                )
                attempt_id = job.current_attempt_id
                before_job = store.get_job(job.id)
                before_attempt = store.get_attempt(attempt_id)
                fail_next_commit = False
                real_connect = store._connect
                real_release = store._release_evidence_lease

                class CommitFailingConnection:
                    def __init__(self, connection):
                        self.connection = connection

                    def commit(self):
                        nonlocal fail_next_commit
                        if fail_next_commit:
                            fail_next_commit = False
                            raise RuntimeError("injected terminal commit failure")
                        return self.connection.commit()

                    def __getattr__(self, name):
                        return getattr(self.connection, name)

                def connect(**kwargs):
                    return CommitFailingConnection(real_connect(**kwargs))

                def release_then_fail_commit(lease, **kwargs):
                    nonlocal fail_next_commit
                    was_held = lease.descriptor is not None
                    result = real_release(lease, **kwargs)
                    if was_held and lease.exclusive:
                        fail_next_commit = True
                    return result

                with mock.patch.object(
                    store,
                    "_connect",
                    side_effect=connect,
                ), mock.patch.object(
                    store,
                    "_release_evidence_lease",
                    side_effect=release_then_fail_commit,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "injected terminal commit failure",
                    ):
                        self.terminal_call(operation, store, job, receipt_hash)

                    self.assertEqual(store.get_job(job.id), before_job)
                    self.assertEqual(store.get_attempt(attempt_id), before_attempt)
                    with store.attempt_evidence_lease(
                        job.id,
                        attempt_id,
                        exclusive=False,
                    ) as lease:
                        artifact = store.create_attempt_artifact(
                            job.id,
                            attempt_id,
                            "after-rollback.txt",
                            "writer proceeded",
                            _lease=lease,
                        )

                self.assertEqual(artifact.read_text(encoding="utf-8"), "writer proceeded")

    def test_resume_rejects_git_metadata_copy_replacement_before_spawning(self):
        service, terminal = self._seed_terminal_job(self.repo, "git-copy-replacement")
        original_git = self.temp_path / "original-git-metadata"
        self.repo.joinpath(".git").rename(original_git)
        shutil.copytree(original_git, self.repo / ".git", symlinks=True)
        with mock.patch("forge_lib.service.spawn_detached_worker", return_value=777) as spawn:
            rejected = service.execute(
                "forge_resume",
                {"job_id": terminal.id, "revision": terminal.revision},
            )
        self.assertTrue(rejected.is_error, rejected)
        self.assertEqual(rejected.body["status"], "rejected")
        self.assertEqual(spawn.call_count, 0)
        self.assertEqual(len(service.store.list_attempts(terminal.id)), 1)

    def test_resume_rejects_unowned_linked_worktree_git_marker_before_spawning(self):
        linked = self.temp_path / "linked-worktree"
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "add", "-q", "-b", "linked", str(linked)],
            check=True,
        )
        service, terminal = self._seed_terminal_job(linked, "unowned-git-marker")
        marker = linked / ".git"
        real_stat = os.stat

        def unowned_marker(path, *args, **kwargs):
            information = real_stat(path, *args, **kwargs)
            if Path(path) != marker:
                return information
            fields = list(information)
            fields[4] = os.geteuid() + 1
            return os.stat_result(fields)

        with (
            mock.patch("forge_lib.service.os.stat", side_effect=unowned_marker),
            mock.patch("forge_lib.service.spawn_detached_worker", return_value=777) as spawn,
        ):
            rejected = service.execute(
                "forge_resume",
                {"job_id": terminal.id, "revision": terminal.revision},
            )
        self.assertTrue(rejected.is_error, rejected)
        self.assertEqual(rejected.body["status"], "rejected")
        self.assertEqual(spawn.call_count, 0)
        self.assertEqual(len(service.store.list_attempts(terminal.id)), 1)

    def test_resume_rejects_in_place_linked_worktree_pointer_retarget(self):
        first = self.temp_path / "linked-first"
        second = self.temp_path / "linked-second"
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "add", "-q", "-b", "linked-first", str(first)],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "add", "-q", "-b", "linked-second", str(second)],
            check=True,
        )
        service, terminal = self._seed_terminal_job(
            first,
            "linked-pointer-retarget",
        )
        first_marker = first / ".git"
        second_marker = second / ".git"
        original_marker = first_marker.read_bytes()
        second_pointer = second_marker.read_bytes()
        second_git_dir = Path(
            second_pointer.removeprefix(b"gitdir: ").strip().decode("utf-8")
        )
        second_backpointer = second_git_dir / "gitdir"
        original_backpointer = second_backpointer.read_bytes()
        admitted_marker_stat = first_marker.stat()
        try:
            first_marker.write_bytes(second_pointer)
            second_backpointer.write_text(str(first_marker) + "\n", encoding="utf-8")
            current_marker_stat = first_marker.stat()
            self.assertEqual(
                (current_marker_stat.st_dev, current_marker_stat.st_ino),
                (admitted_marker_stat.st_dev, admitted_marker_stat.st_ino),
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(first), "status", "--porcelain"],
                    check=True,
                    stdout=subprocess.PIPE,
                ).stdout,
                b"",
            )
            with mock.patch(
                "forge_lib.service.spawn_detached_worker",
                return_value=777,
            ) as spawn:
                rejected = service.execute(
                    "forge_resume",
                    {"job_id": terminal.id, "revision": terminal.revision},
                )
        finally:
            first_marker.write_bytes(original_marker)
            second_backpointer.write_bytes(original_backpointer)

        self.assertTrue(rejected.is_error, rejected)
        self.assertEqual(rejected.body["status"], "rejected")
        self.assertEqual(spawn.call_count, 0)
        self.assertEqual(len(service.store.list_attempts(terminal.id)), 1)

    def test_linked_worktree_pointer_content_is_held_across_popen(self):
        import forge_lib.processes as process_module
        import forge_lib.worker as worker_module

        first = self.temp_path / "launch-linked-first"
        second = self.temp_path / "launch-linked-second"
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "add", "-q", "-b", "launch-linked-first", str(first)],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "add", "-q", "-b", "launch-linked-second", str(second)],
            check=True,
        )
        service = self.make_service(
            repo=first,
            state_path=self.temp_path / "launch-pointer-state" / "state.sqlite3",
        )
        _identity, payload = service._normalized_start(
            self.start_arguments(
                cwd=str(first),
                idempotency_key="launch-pointer-hold",
            )
        )
        with mock.patch.dict(
            os.environ,
            {**self.environment, "FORGE_LAUNCH_WORKSPACE": str(first)},
            clear=True,
        ):
            identity, repository_pin = worker_module._validate_frozen_repository(payload)
        first_marker = first / ".git"
        second_pointer = (second / ".git").read_bytes()
        second_git_dir = Path(
            second_pointer.removeprefix(b"gitdir: ").strip().decode("utf-8")
        )
        second_backpointer = second_git_dir / "gitdir"
        original_marker = first_marker.read_bytes()
        original_backpointer = second_backpointer.read_bytes()
        stderr_path = self.temp_path / "linked-launch-stderr.log"
        stderr_path.write_bytes(b"")
        stderr_path.chmod(0o600)
        sentinel = self.temp_path / "linked-retarget-provider-executed"
        real_provider_process = process_module._ProviderProcess

        def retarget_before_constructor(*args, **kwargs):
            first_marker.write_bytes(second_pointer)
            second_backpointer.write_text(str(first_marker) + "\n", encoding="utf-8")
            return real_provider_process(*args, **kwargs)

        try:
            with mock.patch(
                "forge_lib.processes._ProviderProcess",
                side_effect=retarget_before_constructor,
            ), self.assertRaises(ProcessError):
                process_module.spawn_provider(
                    [
                        sys.executable,
                        "-c",
                        f"from pathlib import Path; Path({str(sentinel)!r}).write_text('ran')",
                    ],
                    cwd=identity.top_level,
                    expected_repository_identity=repository_pin,
                    environment=self.environment,
                    stderr_path=stderr_path,
                )
        finally:
            first_marker.write_bytes(original_marker)
            second_backpointer.write_bytes(original_backpointer)

        self.assertFalse(sentinel.exists())

    def test_full_frozen_baseline_is_rechecked_after_provider_constructor(self):
        import forge_lib.processes as process_module
        import forge_lib.worker as worker_module

        for case in ("tracked", "untracked", "head"):
            with self.subTest(case=case):
                repo = self.make_repo(f"constructor-drift-{case}")
                service = self.make_service(
                    repo=repo,
                    state_path=(
                        self.temp_path
                        / f"constructor-drift-state-{case}"
                        / "state.sqlite3"
                    ),
                )
                _identity, payload = service._normalized_start(
                    self.start_arguments(
                        cwd=str(repo),
                        idempotency_key=f"constructor-drift-{case}",
                    )
                )
                environment = {
                    **self.environment,
                    "FORGE_LAUNCH_WORKSPACE": str(repo),
                }
                with mock.patch.dict(os.environ, environment, clear=True):
                    identity, repository_pin = (
                        worker_module._validate_frozen_repository(payload)
                    )
                stderr_path = self.temp_path / f"constructor-drift-{case}.log"
                stderr_path.write_bytes(b"")
                stderr_path.chmod(0o600)
                launched = []
                real_provider_process = process_module._ProviderProcess

                def mutate_inside_constructor(*args, **kwargs):
                    if case == "tracked":
                        (repo / "tracked.txt").write_text(
                            "changed during constructor\n",
                            encoding="utf-8",
                        )
                    elif case == "untracked":
                        (repo / "new-during-constructor.txt").write_text(
                            "untracked\n",
                            encoding="utf-8",
                        )
                    else:
                        (repo / "tracked.txt").write_text(
                            "new committed head\n",
                            encoding="utf-8",
                        )
                        subprocess.run(
                            ["git", "-C", str(repo), "add", "tracked.txt"],
                            check=True,
                        )
                        subprocess.run(
                            ["git", "-C", str(repo), "commit", "-qm", "drift"],
                            check=True,
                        )
                    process = real_provider_process(*args, **kwargs)
                    launched.append(process)
                    return process

                try:
                    with mock.patch(
                        "forge_lib.processes._ProviderProcess",
                        side_effect=mutate_inside_constructor,
                    ), self.assertRaises(ProcessError):
                        process_module.spawn_provider(
                            [
                                sys.executable,
                                "-u",
                                "-c",
                                "import sys; sys.stdin.buffer.read()",
                            ],
                            cwd=identity.top_level,
                            expected_repository_identity=repository_pin,
                            environment=environment,
                            stderr_path=stderr_path,
                        )
                finally:
                    for process in launched:
                        if process.poll() is None:
                            process_module.terminate_process_group(process)
                        for stream in (
                            process.stdin,
                            process.stdout,
                            process.stderr,
                        ):
                            if stream is not None and not stream.closed:
                                stream.close()
                self.assertTrue(launched)
                self.assertTrue(all(process.poll() is not None for process in launched))

    def test_steering_near_event_limit_is_rejected_before_it_can_wedge(self):
        store = Store(self.temp_path / "command-capacity" / "state.sqlite3")
        job = store.create_job(
            repo_id="repo",
            idempotency_key="near-cap",
            request_hash="hash",
            payload={},
        )
        claim = store.claim_dispatch(job.id, owner_pid=os.getpid())
        self.assertIsNotNone(claim)
        assert claim is not None
        self.assertTrue(
            store.claim_worker_execution(job.id, claim.token, worker_pid=os.getpid())
        )
        for status_name in ("PREFLIGHT", "RUNNING"):
            current = store.get_job(job.id)
            store.transition(
                job.id,
                expected_revision=current.revision,
                new_status=status_name,
            )
        for index in range(MAX_ATTEMPT_EVENTS - 1):
            store.append_event(job.id, "PROVIDER_EVENT", {"index": index})
        running = store.get_job(job.id)
        with self.assertRaises(StateError):
            store.queue_command(
                job.id,
                expected_revision=running.revision,
                kind="STEER",
                payload={"prompt": "must not strand"},
                idempotency_key="near-cap-steer",
            )
        self.assertEqual(store.list_commands(job.id), [])
        attempt = store.get_attempt(store.get_job(job.id).current_attempt_id)
        self.assertEqual(attempt.command_inbox_state, "OPEN")

    def test_reserved_command_capacity_covers_claim_delivery_and_terminal_close(self):
        store = Store(self.temp_path / "command-reserve" / "state.sqlite3")
        job = store.create_job(
            repo_id="repo",
            idempotency_key="exact-reserve",
            request_hash="hash",
            payload={},
        )
        claim = store.claim_dispatch(job.id, owner_pid=os.getpid())
        self.assertIsNotNone(claim)
        assert claim is not None
        self.assertTrue(
            store.claim_worker_execution(job.id, claim.token, worker_pid=os.getpid())
        )
        for status_name in ("PREFLIGHT", "RUNNING"):
            current = store.get_job(job.id)
            store.transition(
                job.id,
                expected_revision=current.revision,
                new_status=status_name,
            )
        for index in range(MAX_ATTEMPT_EVENTS - 4):
            store.append_event(job.id, "PROVIDER_EVENT", {"index": index})
        running = store.get_job(job.id)
        command = store.queue_command(
            job.id,
            expected_revision=running.revision,
            kind="STEER",
            payload={"prompt": "uses reserved receipts"},
            idempotency_key="reserved-steer",
        )
        claimed = store.claim_next_command(
            job.id,
            dispatch_token=claim.token,
            worker_pid=os.getpid(),
        )
        self.assertEqual(claimed.id, command.id)
        store.mark_command_delivered(
            job.id,
            command.id,
            dispatch_token=claim.token,
            worker_pid=os.getpid(),
        )
        self.assertEqual(
            store.close_command_inbox(
                job.id,
                dispatch_token=claim.token,
                worker_pid=os.getpid(),
            ),
            [],
        )
        final_attempt = store.get_attempt(store.get_job(job.id).current_attempt_id)
        self.assertEqual(final_attempt.event_count, MAX_ATTEMPT_EVENTS)
        self.assertEqual(final_attempt.command_inbox_state, "CLOSED")
        self.assertEqual(store.list_commands(job.id)[0].status, "DELIVERED")

    def test_near_capacity_queue_terminal_race_never_leaves_open_queued_work(self):
        store = Store(self.temp_path / "command-terminal-race" / "state.sqlite3")
        job = store.create_job(
            repo_id="repo",
            idempotency_key="near-cap-race",
            request_hash="hash",
            payload={},
        )
        claim = store.claim_dispatch(job.id, owner_pid=os.getpid())
        self.assertIsNotNone(claim)
        assert claim is not None
        self.assertTrue(
            store.claim_worker_execution(job.id, claim.token, worker_pid=os.getpid())
        )
        for status_name in ("PREFLIGHT", "RUNNING"):
            current = store.get_job(job.id)
            store.transition(
                job.id,
                expected_revision=current.revision,
                new_status=status_name,
            )
        for index in range(MAX_ATTEMPT_EVENTS - 4):
            store.append_event(job.id, "PROVIDER_EVENT", {"index": index})
        running = store.get_job(job.id)
        barrier = threading.Barrier(2)

        def queue_at_terminal_edge():
            barrier.wait()
            try:
                return store.queue_command(
                    job.id,
                    expected_revision=running.revision,
                    kind="STEER",
                    payload={"prompt": "race terminal closure"},
                    idempotency_key="race-steer",
                )
            except (InvalidTransition, RevisionConflict, StateError):
                return None

        def close_at_terminal_edge():
            barrier.wait()
            return store.close_command_inbox(
                job.id,
                dispatch_token=claim.token,
                worker_pid=os.getpid(),
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            queued_future = executor.submit(queue_at_terminal_edge)
            closed_future = executor.submit(close_at_terminal_edge)
            queued_future.result(timeout=5)
            closed_future.result(timeout=5)

        attempt = store.get_attempt(store.get_job(job.id).current_attempt_id)
        self.assertEqual(attempt.command_inbox_state, "CLOSED")
        commands = store.list_commands(job.id)
        self.assertFalse(
            any(command.status in {"QUEUED", "DELIVERING"} for command in commands)
        )
        self.assertEqual(store.list_events(job.id)[-1].kind, "COMMAND_INBOX_CLOSED")

    def _seed_terminal_job(self, repo, state_name):
        service = self.make_service(
            repo=repo,
            state_path=self.temp_path / state_name / "state.sqlite3",
        )
        arguments = self.start_arguments(
            cwd=str(repo),
            idempotency_key=f"seed-{state_name}",
        )
        job = self.create_admitted_job(service, arguments)
        for status in ("PREFLIGHT", "RUNNING"):
            current = service.store.get_job(job.id)
            service.store.transition(
                job.id,
                expected_revision=current.revision,
                new_status=status,
            )
        service.store.record_provider_identity(job.id, session_id="recorded-session")
        service.store.append_event(job.id, "PARENT_EVENT", {"attempt": 1})
        current = service.store.get_job(job.id)
        service.store.transition(
            job.id,
            expected_revision=current.revision,
            new_status="SUCCEEDED",
        )
        return service, service.store.get_job(job.id)

    def test_new_resume_child_status_does_not_leak_parent_progress(self):
        service, terminal = self._seed_terminal_job(self.repo, "child-status-isolation")
        child = self.create_admitted_resume(service, terminal)
        body = service._status_body(service.store.get_job(terminal.id))
        self.assertEqual(body["attempt_id"], child.id)
        self.assertEqual(body["progress"]["event_count"], 0)
        self.assertIsNone(body["progress"]["last_sequence"])
        self.assertIsNone(body["progress"]["last_kind"])
        self.assertIsNone(body["event_hash"])

    def test_generic_artifact_writes_cannot_bypass_terminal_attempt_immutability(self):
        store = Store(self.temp_path / "terminal-artifact-guard" / "state.sqlite3")
        job = store.create_job(
            repo_id="repo",
            idempotency_key="terminal-artifact-guard",
            request_hash="hash",
            payload={},
        )
        attempt_id = job.current_attempt_id
        store.create_attempt_artifact(job.id, attempt_id, "report.txt", "original")
        current = store.get_job(job.id)
        store.transition(
            job.id,
            expected_revision=current.revision,
            new_status="FAILED",
        )
        raw_name = f"{attempt_id}/report.txt"
        with self.assertRaises(InvalidTransition):
            store.create_artifact(job.id, raw_name, "mutated")
        with self.assertRaises(InvalidTransition):
            with store.open_artifact(job.id, raw_name, writable=True):
                pass
        with store.open_artifact(job.id, raw_name) as (descriptor, _path):
            self.assertEqual(os.read(descriptor, 64), b"original")

    def test_finalize_rejects_an_open_inbox_with_accepted_steering(self):
        store = Store(self.temp_path / "finalize-inbox-guard" / "state.sqlite3")
        job = store.create_job(
            repo_id="repo",
            idempotency_key="finalize-inbox-guard",
            request_hash="hash",
            payload={},
        )
        claim = store.claim_dispatch(job.id, owner_pid=os.getpid())
        self.assertIsNotNone(claim)
        assert claim is not None
        self.assertTrue(
            store.claim_worker_execution(job.id, claim.token, worker_pid=os.getpid())
        )
        for status_name in ("PREFLIGHT", "RUNNING"):
            current = store.get_job(job.id)
            store.transition(
                job.id,
                expected_revision=current.revision,
                new_status=status_name,
            )
        running = store.get_job(job.id)
        command = store.queue_command(
            job.id,
            expected_revision=running.revision,
            kind="STEER",
            payload={"prompt": "must settle before terminal receipt"},
            idempotency_key="finalize-inbox-command",
        )
        attempt_id = store.get_job(job.id).current_attempt_id
        receipt_hash = self.write_terminal_receipt(store, job.id, "SUCCEEDED")
        current = store.get_job(job.id)

        with self.assertRaises(InvalidTransition):
            store.finalize_with_receipt(
                job.id,
                expected_revision=current.revision,
                new_status="SUCCEEDED",
                receipt_name="receipt.json",
                receipt_hash=receipt_hash,
            )

        unchanged = store.get_job(job.id)
        unchanged_attempt = store.get_attempt(unchanged.current_attempt_id)
        self.assertEqual(unchanged.status, "RUNNING")
        self.assertEqual(unchanged_attempt.command_inbox_state, "OPEN")
        self.assertEqual(store.list_commands(job.id)[0].status, "QUEUED")
        self.assertEqual(store.list_commands(job.id)[0].id, command.id)

        claimed = store.claim_next_command(
            job.id,
            dispatch_token=claim.token,
            worker_pid=os.getpid(),
        )
        self.assertEqual(claimed.id, command.id)
        receipt_hash = self.write_terminal_receipt(store, job.id, "SUCCEEDED")
        delivering = store.get_job(job.id)
        with self.assertRaises(InvalidTransition):
            store.finalize_with_receipt(
                job.id,
                expected_revision=delivering.revision,
                new_status="SUCCEEDED",
                receipt_name="receipt.json",
                receipt_hash=receipt_hash,
            )

        failed = store.close_command_inbox(
            job.id,
            dispatch_token=claim.token,
            worker_pid=os.getpid(),
        )
        self.assertEqual([item.id for item in failed], [command.id])
        receipt_hash = self.write_terminal_receipt(store, job.id, "SUCCEEDED")
        settled = store.get_job(job.id)
        store.finalize_with_receipt(
            job.id,
            expected_revision=settled.revision,
            new_status="SUCCEEDED",
            receipt_name="receipt.json",
            receipt_hash=receipt_hash,
        )
        terminal_attempt = store.get_attempt(store.get_job(job.id).current_attempt_id)
        self.assertEqual(terminal_attempt.command_inbox_state, "CLOSED")
        self.assertEqual(store.list_commands(job.id)[0].status, "DELIVERY_FAILED")

    def test_direct_terminal_transition_rejects_pending_commands(self):
        store = Store(self.temp_path / "transition-inbox-guard" / "state.sqlite3")
        job = store.create_job(
            repo_id="repo",
            idempotency_key="transition-inbox-guard",
            request_hash="hash",
            payload={},
        )
        claim = store.claim_dispatch(job.id, owner_pid=os.getpid())
        self.assertIsNotNone(claim)
        assert claim is not None
        self.assertTrue(
            store.claim_worker_execution(job.id, claim.token, worker_pid=os.getpid())
        )
        for status_name in ("PREFLIGHT", "RUNNING"):
            current = store.get_job(job.id)
            store.transition(
                job.id,
                expected_revision=current.revision,
                new_status=status_name,
            )
        running = store.get_job(job.id)
        store.queue_command(
            job.id,
            expected_revision=running.revision,
            kind="STEER",
            payload={"prompt": "block direct terminal transition"},
            idempotency_key="transition-inbox-command",
        )
        queued = store.get_job(job.id)
        with self.assertRaises(InvalidTransition):
            store.transition(
                job.id,
                expected_revision=queued.revision,
                new_status="FAILED",
            )
        self.assertEqual(store.get_job(job.id).status, "RUNNING")
        store.close_command_inbox(
            job.id,
            dispatch_token=claim.token,
            worker_pid=os.getpid(),
        )
        settled = store.get_job(job.id)
        store.transition(
            job.id,
            expected_revision=settled.revision,
            new_status="FAILED",
        )
        attempt = store.get_attempt(store.get_job(job.id).current_attempt_id)
        self.assertEqual(attempt.command_inbox_state, "CLOSED")

    def test_command_delivery_cannot_mutate_a_terminal_attempt_or_event_chain(self):
        store = Store(self.temp_path / "terminal-command-guard" / "state.sqlite3")
        job = store.create_job(
            repo_id="repo",
            idempotency_key="terminal-command-guard",
            request_hash="hash",
            payload={},
        )
        claim = store.claim_dispatch(job.id, owner_pid=os.getpid())
        self.assertIsNotNone(claim)
        assert claim is not None
        self.assertTrue(
            store.claim_worker_execution(job.id, claim.token, worker_pid=os.getpid())
        )
        for status_name in ("PREFLIGHT", "RUNNING"):
            current = store.get_job(job.id)
            store.transition(
                job.id,
                expected_revision=current.revision,
                new_status=status_name,
            )
        running = store.get_job(job.id)
        command = store.queue_command(
            job.id,
            expected_revision=running.revision,
            kind="STEER",
            payload={"prompt": "claimed before forced terminal state"},
            idempotency_key="terminal-command",
        )
        claimed = store.claim_next_command(
            job.id,
            dispatch_token=claim.token,
            worker_pid=os.getpid(),
        )
        self.assertEqual(claimed.id, command.id)
        with store._transaction() as connection:
            connection.execute(
                "UPDATE attempts SET status = 'SUCCEEDED' WHERE id = ?",
                (job.current_attempt_id,),
            )
            connection.execute(
                "UPDATE jobs SET status = 'SUCCEEDED' WHERE id = ?",
                (job.id,),
            )
        terminal_revision = store.get_job(job.id).revision
        terminal_events = store.list_events(job.id)

        with self.assertRaises(InvalidTransition):
            store.mark_command_delivered(
                job.id,
                command.id,
                dispatch_token=claim.token,
                worker_pid=os.getpid(),
            )
        with store._transaction() as connection:
            terminal_job = store._job_row(connection, job.id)
            terminal_attempt = store._current_attempt_row(connection, terminal_job)
            with self.assertRaises(InvalidTransition):
                store._append_command_event(
                    connection,
                    terminal_job,
                    terminal_attempt,
                    "COMMAND_DELIVERED",
                    {
                        "command_id": command.id,
                        "command_sequence": command.sequence,
                        "command_kind": command.kind,
                    },
                )
        self.assertEqual(store.get_job(job.id).revision, terminal_revision)
        self.assertEqual(store.list_events(job.id), terminal_events)

    def test_finalize_waits_for_held_writable_attempt_artifact_lease(self):
        store = Store(self.temp_path / "artifact-finalize-lease" / "state.sqlite3")
        job = store.create_job(
            repo_id="repo",
            idempotency_key="artifact-finalize-lease",
            request_hash="hash",
            payload={},
        )
        attempt_id = job.current_attempt_id
        report = store.create_attempt_artifact(
            job.id,
            attempt_id,
            "report.txt",
            "before-terminal",
        )
        receipt_hash = self.write_terminal_receipt(store, job.id, "FAILED")
        finalizer_started = threading.Event()

        def finalize():
            finalizer_started.set()
            return store.finalize_with_receipt(
                job.id,
                expected_revision=job.revision,
                new_status="FAILED",
                receipt_name="receipt.json",
                receipt_hash=receipt_hash,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            with store.open_attempt_artifact(
                job.id,
                attempt_id,
                "report.txt",
                writable=True,
            ) as (descriptor, _path):
                future = executor.submit(finalize)
                self.assertTrue(finalizer_started.wait(timeout=2))
                time.sleep(0.1)
                self.assertFalse(
                    future.done(),
                    "terminalization crossed a held writable artifact descriptor",
                )
                os.ftruncate(descriptor, 0)
                os.write(descriptor, b"last-pre-terminal-write")
                os.fsync(descriptor)
            future.result(timeout=2)

        self.assertEqual(store.get_job(job.id).status, "FAILED")
        self.assertEqual(report.read_bytes(), b"last-pre-terminal-write")
        with self.assertRaises(InvalidTransition):
            with store.open_attempt_artifact(
                job.id,
                attempt_id,
                "report.txt",
                writable=True,
            ):
                pass

    def test_finalize_rejects_a_receipt_behind_the_durable_event_chain(self):
        store = Store(self.temp_path / "stale-event-receipt" / "state.sqlite3")
        job = store.create_job(
            repo_id="repo",
            idempotency_key="stale-event-receipt",
            request_hash="hash",
            payload={},
        )
        first = store.append_event(job.id, "FIRST", {"sequence": 1})
        attempt = store.get_attempt(job.current_attempt_id)
        receipt_payload = {
            "job_id": job.id,
            "attempt_id": attempt.id,
            "event_hash": first.event_hash,
            "session_id": None,
            "observed_model": None,
            "parent_tool_use_ids": [],
            "terminal_status": "FAILED",
        }
        receipt = store.create_attempt_artifact(
            job.id,
            attempt.id,
            "receipt.json",
            canonical_json(receipt_payload) + "\n",
        )
        receipt_hash = hashlib.sha256(receipt.read_bytes()).hexdigest()
        store.append_event(job.id, "LATE_EVENT", {"sequence": 2})
        current = store.get_job(job.id)

        with self.assertRaises(StateError):
            store.finalize_with_receipt(
                job.id,
                expected_revision=current.revision,
                new_status="FAILED",
                receipt_name="receipt.json",
                receipt_hash=receipt_hash,
            )
        self.assertEqual(store.get_job(job.id).status, "CREATED")

    def test_already_readable_terminal_result_settles_before_queued_steering(self):
        import forge_lib.worker as worker_module

        store = Store(self.temp_path / "result-before-steer" / "state.sqlite3")
        job = store.create_job(
            repo_id="repo",
            idempotency_key="result-before-steer",
            request_hash="hash",
            payload={},
        )
        claim = store.claim_dispatch(job.id, owner_pid=os.getpid())
        self.assertIsNotNone(claim)
        assert claim is not None
        self.assertTrue(
            store.claim_worker_execution(job.id, claim.token, worker_pid=os.getpid())
        )
        for status_name in ("PREFLIGHT", "RUNNING"):
            current = store.get_job(job.id)
            store.transition(
                job.id,
                expected_revision=current.revision,
                new_status=status_name,
            )
        running = store.get_job(job.id)
        command = store.queue_command(
            job.id,
            expected_revision=running.revision,
            kind="STEER",
            payload={"prompt": "must not arrive after result"},
            idempotency_key="result-before-steer-command",
        )
        attempt_id = store.get_job(job.id).current_attempt_id
        store.create_attempt_artifact(job.id, attempt_id, "events.jsonl")
        store.create_attempt_artifact(job.id, attempt_id, "stderr.log")
        result_payload = _canonical_result(
            "session-ready-result", result="already complete"
        )
        child_code = (
            "import json,sys;"
            f"print(json.dumps({result_payload!r}),flush=True);"
            "sys.stdin.buffer.read()"
        )
        provider = subprocess.Popen(
            [sys.executable, "-u", "-c", child_code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            bufsize=0,
        )
        assert provider.stdout is not None
        ready, _, _ = select.select([provider.stdout], [], [], 2)
        self.assertTrue(ready, "terminal result was not ready for the ordering probe")
        try:
            with store.open_attempt_artifact(
                job.id,
                attempt_id,
                "events.jsonl",
                writable=True,
            ) as (events_descriptor, _events_path), store.open_attempt_artifact(
                job.id,
                attempt_id,
                "stderr.log",
                writable=True,
            ) as (stderr_descriptor, _stderr_path):
                with self.assertRaises(worker_module.WorkerError):
                    worker_module._collect_provider(
                        provider,
                        store=store,
                        job_id=job.id,
                        session_id="session-ready-result",
                        dispatch_token=claim.token,
                        worker_pid=os.getpid(),
                        events_descriptor=events_descriptor,
                        stderr_descriptor=stderr_descriptor,
                        deadline=time.monotonic() + 5,
                    )
        finally:
            if provider.poll() is None:
                provider.kill()
            provider.wait(timeout=2)
            for stream in (provider.stdin, provider.stdout, provider.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        stored_command = store.list_commands(job.id)[0]
        self.assertEqual(stored_command.id, command.id)
        self.assertEqual(stored_command.status, "DELIVERY_FAILED")
        attempt = store.get_attempt(attempt_id)
        self.assertEqual(attempt.command_inbox_state, "CLOSED")

    def test_resume_rejects_repository_and_baseline_drift_before_spawning(self):
        cases = (
            "dirty",
            "head",
            "moved",
            "replacement",
            "baseline-tamper",
            "implicit-session",
        )
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                repo = self.make_repo(f"resume-{index}")
                service, job = self._seed_terminal_job(repo, f"resume-state-{index}")

                if case == "dirty":
                    (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
                elif case == "head":
                    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
                    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
                    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "changed"], check=True)
                elif case == "moved":
                    repo.rename(repo.with_name(repo.name + "-moved"))
                elif case == "replacement":
                    original = repo.with_name(repo.name + "-original")
                    repo.rename(original)
                    subprocess.run(["git", "clone", "-q", str(original), str(repo)], check=True)
                elif case == "baseline-tamper":
                    with service.store._transaction() as connection:
                        payload = dict(job.payload)
                        payload["baseline"] = dict(payload["baseline"])
                        payload["baseline"]["head"] = "0" * 40
                        connection.execute(
                            "UPDATE jobs SET payload_json = ? WHERE id = ?",
                            (canonical_json(payload), job.id),
                        )
                else:
                    with service.store._transaction() as connection:
                        connection.execute(
                            "UPDATE attempts SET session_id = 'latest' WHERE id = ?",
                            (job.current_attempt_id,),
                        )

                with mock.patch("forge_lib.service.spawn_detached_worker") as spawn:
                    rejected = service.execute(
                        "forge_resume",
                        {"job_id": job.id, "revision": job.revision},
                    )
                self.assertTrue(rejected.is_error, rejected)
                self.assertEqual(rejected.body["status"], "rejected")
                self.assertEqual(spawn.call_count, 0)
                self.assertEqual(len(service.store.list_attempts(job.id)), 1)


if __name__ == "__main__":
    unittest.main()
