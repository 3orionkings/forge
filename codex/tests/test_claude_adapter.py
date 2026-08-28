import copy
import io
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from decimal import Decimal
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from forge_lib.claude import (  # noqa: E402
    MAX_EVENT_BYTES,
    AdapterError,
    ClaudeProtocolError,
    JobSpec,
    PreflightError,
    ProviderPolicyTracker,
    build_command,
    build_user_message,
    encode_stream_message,
    encode_user_message,
    get_auth_status,
    get_version,
    iter_events,
    parse_event,
    preflight,
)
from forge_lib.processes import process_instance_is_live  # noqa: E402


FAKE_CLAUDE = ROOT / "tests" / "fixtures" / "fake_claude.py"
FAKE_CONTROL_NAME = "forge-fake-claude-control.json"
CLAUDE_2_1_248_STANDARD_USAGE = {
    "input_tokens": 7,
    "output_tokens": 3,
    "cache_read_input_tokens": 1,
    "cache_creation_input_tokens": 2,
    "server_tool_use": {
        "web_search_requests": 0,
        "web_fetch_requests": 0,
    },
    "cache_creation": {
        "ephemeral_5m_input_tokens": 2,
        "ephemeral_1h_input_tokens": 0,
    },
    "service_tier": "standard",
    "iterations": [],
    "speed": "standard",
}
CLAUDE_2_1_248_ZERO_TURN_USAGE = {
    "input_tokens": 0,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
    "output_tokens": 0,
    "server_tool_use": {
        "web_search_requests": 0,
        "web_fetch_requests": 0,
    },
    "service_tier": "standard",
    "cache_creation": {
        "ephemeral_1h_input_tokens": 0,
        "ephemeral_5m_input_tokens": 0,
    },
    "inference_geo": "",
    "iterations": [],
    "speed": "standard",
}


def option_value(command, option):
    return command[command.index(option) + 1]


def valid_init_payload(*, session_id="session-1", model="fable", uuid_value="event-init-1"):
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


def read_json_lines(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def process_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def read_process_json_line(process, timeout=1):
    ready, _, _ = select.select([process.stdout], [], [], timeout)
    if not ready:
        return None
    line = process.stdout.readline()
    return json.loads(line) if line else None


class ClaudeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temp_path = Path(self.temporary_directory.name).resolve(strict=True)
        self.settings = self.temp_path / "claude-settings.json"
        self.settings.write_text("{}\n", encoding="utf-8")
        self.argv_log = self.temp_path / "argv.jsonl"
        self.stdin_log = self.temp_path / "stdin.jsonl"
        self.control_path = self.temp_path / FAKE_CONTROL_NAME
        self.control = {
            "argv_log": str(self.argv_log),
            "stdin_log": str(self.stdin_log),
        }
        self.environment = {
            "HOME": str(self.temp_path),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "LC_CTYPE": "C.UTF-8",
            "TMPDIR": str(self.temp_path),
            "SHELL": "/bin/sh",
            "USER": "forge-test",
        }
        self.write_control()

    def write_control(self, **updates):
        self.control.update(updates)
        self.control_path.write_text(
            json.dumps(self.control, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    def spec(self, **overrides):
        values = {
            "role": "review",
            "model": "fable",
            "effort": "max",
            "max_budget_usd": Decimal("1.25"),
            "settings_path": self.settings,
            "prompt": "Review the frozen plan.",
        }
        values.update(overrides)
        return JobSpec(**values)

    def run_fake(self, spec=None, *, environment=None, controls=()):
        job = self.spec() if spec is None else spec
        if controls:
            self.write_control(control_window_seconds=0.25)
        command = build_command(job, executable=str(FAKE_CLAUDE))
        payload = bytearray(encode_user_message(job.prompt))
        for control in controls:
            payload.extend(encode_stream_message(control))
        completed = subprocess.run(
            command,
            input=bytes(payload),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.environment if environment is None else environment,
            check=False,
            timeout=5,
        )
        return completed, list(iter_events(io.BytesIO(completed.stdout)))

    def test_new_session_command_is_the_exact_bounded_argv_contract(self):
        generated = uuid.UUID("12345678-1234-4678-9234-567812345678")
        with mock.patch("forge_lib.claude.uuid.uuid4", return_value=generated):
            command = build_command(self.spec())

        self.assertIsInstance(command, list)
        self.assertEqual(
            command,
            [
                "claude",
                "-p",
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
                "--verbose",
                "--session-id",
                str(generated),
                "--model",
                "fable",
                "--effort",
                "max",
                "--max-budget-usd",
                "1.25",
                "--settings",
                str(self.settings),
                "--setting-sources",
                "",
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
                "--disable-slash-commands",
                "--no-chrome",
                "--permission-mode",
                "dontAsk",
                "--tools",
                "Read,Glob,Grep,Agent",
                "--allowedTools",
                "Read,Glob,Grep,Agent",
            ],
        )
        self.assertNotIn("--fallback-model", command)
        self.assertNotIn("--dangerously-skip-permissions", command)
        self.assertNotIn(self.spec().prompt, command)

    def test_resume_uses_only_the_exact_recorded_session(self):
        with mock.patch(
            "forge_lib.claude.uuid.uuid4",
            side_effect=AssertionError("resume generated a new session id"),
        ):
            command = build_command(self.spec(resume_session_id="recorded-id"))

        self.assertEqual(command[7:9], ["--resume", "recorded-id"])
        self.assertNotIn("--session-id", command)
        self.assertNotIn("--continue", command)
        self.assertNotIn("latest", command)

    def test_review_and_build_have_exact_fixed_tool_contracts(self):
        review = build_command(self.spec(role="review"))
        build = build_command(self.spec(role="build"))

        self.assertEqual(option_value(review, "--tools"), "Read,Glob,Grep,Agent")
        self.assertEqual(option_value(review, "--allowedTools"), "Read,Glob,Grep,Agent")
        self.assertNotIn("Bash", option_value(review, "--tools"))
        self.assertEqual(
            option_value(build, "--tools"),
            "Read,Edit,Write,Glob,Grep,Bash,Agent",
        )
        self.assertEqual(
            option_value(build, "--allowedTools"),
            "Read,Edit,Write,Glob,Grep,Bash,Agent",
        )

    def test_recovery_requires_and_preserves_an_explicit_minimum(self):
        with self.assertRaises(AdapterError):
            build_command(self.spec(role="recovery"))

        command = build_command(
            self.spec(role="recovery", recovery_tools=("Read", "Grep", "Edit"))
        )
        self.assertEqual(option_value(command, "--tools"), "Read,Grep,Edit")
        self.assertEqual(option_value(command, "--allowedTools"), "Read,Grep,Edit")

    def test_invalid_specs_fail_closed_before_argv_construction(self):
        invalid = (
            {"role": "admin"},
            {"model": ""},
            {"effort": "ultra"},
            {"max_budget_usd": True},
            {"max_budget_usd": Decimal("NaN")},
            {"max_budget_usd": Decimal("Infinity")},
            {"max_budget_usd": float("-inf")},
            {"max_budget_usd": Decimal("0")},
            {"resume_session_id": "latest"},
            {"resume_session_id": "--continue"},
            {"settings_path": "relative-settings.json"},
            {"role": "review", "recovery_tools": ("Read",)},
            {"role": "recovery", "recovery_tools": ("Read", "Unknown")},
            {"role": "recovery", "recovery_tools": ("Read", "Read")},
        )
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(AdapterError):
                build_command(self.spec(**change))

    def test_budget_rejects_textual_inputs_over_sixty_four_bytes(self):
        oversized_values = (
            "0" * 64 + "1",
            "1." + "0" * 63,
        )

        for value in oversized_values:
            with self.subTest(length=len(value)), self.assertRaises(AdapterError):
                build_command(self.spec(max_budget_usd=value))

    def test_budget_rejects_huge_coefficients_and_exponents_before_expansion(self):
        huge_values = (
            Decimal("9" * 100_000),
            Decimal("1e100000"),
            Decimal("1e-100000"),
            "1e100000",
            "1e-100000",
        )

        for value in huge_values:
            with self.subTest(value_type=type(value).__name__), self.assertRaises(
                AdapterError
            ):
                build_command(self.spec(max_budget_usd=value))

    def test_budget_rejects_more_than_six_fractional_places(self):
        excessive_precision = (
            Decimal("0.1234567"),
            Decimal("0.0000001"),
            "999999.0000001",
        )

        for value in excessive_precision:
            with self.subTest(value=value), self.assertRaises(AdapterError):
                build_command(self.spec(max_budget_usd=value))

    def test_budget_accepts_exact_bounds_and_rejects_values_above_maximum(self):
        cases = (
            (Decimal("0.000001"), "0.000001"),
            (Decimal("1.250000"), "1.25"),
            ("1e3", "1000"),
            ("0" * 63 + "1", "1"),
            (Decimal("1000000.000000"), "1000000"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                command = build_command(self.spec(max_budget_usd=value))
                rendered = option_value(command, "--max-budget-usd")
                self.assertEqual(rendered, expected)
                self.assertLessEqual(len(rendered), 32)

        for value in (Decimal("1000000.000001"), "1000001", 1_000_001):
            with self.subTest(value=value), self.assertRaises(AdapterError):
                build_command(self.spec(max_budget_usd=value))

    def test_canonical_user_message_matches_the_frozen_stream_contract(self):
        prompt = "literal $(touch never) `echo no` ; VERDICT: APPROVED"
        expected = {
            "type": "user",
            "session_id": "",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            },
            "parent_tool_use_id": None,
            "origin": {"kind": "coordinator"},
        }

        self.assertEqual(build_user_message(prompt), expected)
        encoded = encode_user_message(prompt)
        self.assertTrue(encoded.endswith(b"\n"))
        self.assertEqual(json.loads(encoded), expected)

    def test_stream_encoder_accepts_only_canonical_user_and_interrupt_objects(self):
        prompt = "strict user frame"
        user = build_user_message(prompt)
        interrupt = {
            "type": "control_request",
            "request_id": "control-1",
            "request": {"subtype": "interrupt"},
        }
        self.assertEqual(json.loads(encode_stream_message(user)), user)
        self.assertEqual(json.loads(encode_stream_message(interrupt)), interrupt)

        invalid = [
            [],
            {},
            {"type": "assistant"},
            {"type": "control_response", "response": {}},
            {**user, "session_id": "chosen-session"},
            {**user, "parent_tool_use_id": "parent"},
            {**user, "origin": {"kind": "user"}},
            {**user, "extra": True},
            {**user, "message": {**user["message"], "role": "assistant"}},
            {
                **user,
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": prompt, "extra": True}],
                },
            },
            {
                "type": "control_request",
                "request_id": "control-1",
                "request": {"subtype": "set_model", "model": "other"},
            },
            {
                "type": "control_request",
                "request_id": "control-1",
                "request": {"subtype": "set_permission_mode"},
            },
            {**interrupt, "extra": True},
            {**interrupt, "request_id": ""},
            {**interrupt, "request_id": "r" * 257},
        ]
        for invalid_message in invalid:
            with self.subTest(invalid=invalid_message), self.assertRaises(AdapterError):
                encode_stream_message(invalid_message)

    def test_prompt_metacharacters_are_stdin_data_and_never_evaluated(self):
        marker = self.temp_path / "prompt-was-evaluated"
        prompt = f"$(touch {marker}) ; `touch {marker}` && touch {marker}"
        job = self.spec(prompt=prompt)

        completed, events = self.run_fake(job)

        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertFalse(marker.exists())
        self.assertNotIn(prompt, read_json_lines(self.argv_log)[0])
        self.assertEqual(read_json_lines(self.stdin_log)[0], build_user_message(prompt))
        self.assertEqual([event.type for event in events], ["system", "assistant", "result"])

    def test_fake_accepts_steering_and_interrupt_without_waiting_for_eof(self):
        job = self.spec()
        self.write_control(control_window_seconds=1)
        process = subprocess.Popen(
            build_command(job, executable=str(FAKE_CLAUDE)),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.environment,
            start_new_session=True,
            bufsize=0,
        )
        control_request = {
            "type": "control_request",
            "request_id": "control-1",
            "request": {"subtype": "interrupt"},
        }
        steering = build_user_message("steer the active turn")
        try:
            process.stdin.write(encode_user_message(job.prompt))
            process.stdin.flush()
            init = read_process_json_line(process, timeout=1)
            assistant = read_process_json_line(process, timeout=1)
            self.assertEqual(init["subtype"], "init")
            self.assertEqual(assistant["type"], "assistant")

            process.stdin.write(encode_stream_message(steering))
            process.stdin.write(encode_stream_message(control_request))
            process.stdin.flush()
            result = read_process_json_line(process, timeout=1)
            self.assertEqual(result["type"], "result")
            process.stdin.close()
            process.wait(timeout=2)
            self.assertEqual(process.returncode, 0, process.stderr.read().decode())
            self.assertEqual(
                read_json_lines(self.stdin_log),
                [build_user_message(job.prompt), steering, control_request],
            )
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()

    def test_preflight_uses_only_injected_version_and_auth_commands(self):
        result = preflight(
            str(FAKE_CLAUDE),
            environment=self.environment,
            timeout_seconds=2,
            output_limit_bytes=4096,
        )

        self.assertEqual(result.version, "2.1.248 (Claude Code)")
        self.assertEqual(result.auth_status["loggedIn"], True)
        self.assertEqual(
            read_json_lines(self.argv_log),
            [["--version"], ["auth", "status", "--json"]],
        )
        self.assertNotIn("model", json.dumps(read_json_lines(self.argv_log)))

    def test_preflight_filters_default_and_supplied_parent_environments(self):
        environment_log = self.temp_path / "environment.jsonl"
        self.write_control(environment_log=str(environment_log))
        parent = dict(
            self.environment,
            ARBITRARY_CANARY="arbitrary-secret-canary",
            AWS_SECRET_ACCESS_KEY="aws-secret-canary",
            FORGE_BRIDGE_TOKEN="forge-secret-canary",
            ANTHROPIC_API_KEY="anthropic-secret-canary",
            CLAUDE_CODE_OAUTH_TOKEN="oauth-secret-canary",
        )

        with mock.patch.dict(os.environ, parent, clear=True):
            preflight(str(FAKE_CLAUDE), timeout_seconds=2)
        default_children = read_json_lines(environment_log)
        self.assertEqual(len(default_children), 2)
        for child in default_children:
            self.assertEqual(
                {name: child[name] for name in self.environment},
                self.environment,
            )
            self.assertNotIn("ARBITRARY_CANARY", child)
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", child)
            self.assertNotIn("FORGE_BRIDGE_TOKEN", child)
            self.assertNotIn("ANTHROPIC_API_KEY", child)
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", child)

        environment_log.unlink()
        preflight(
            str(FAKE_CLAUDE),
            environment=parent,
            allowed_auth_names=("ANTHROPIC_API_KEY",),
            timeout_seconds=2,
        )
        explicit_children = read_json_lines(environment_log)
        self.assertEqual(len(explicit_children), 2)
        for child in explicit_children:
            self.assertEqual(child["ANTHROPIC_API_KEY"], "anthropic-secret-canary")
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", child)
            self.assertNotIn("ARBITRARY_CANARY", child)
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", child)
            self.assertNotIn("FORGE_BRIDGE_TOKEN", child)

    def test_preflight_timeout_and_output_limits_fail_closed(self):
        self.write_control(delay_seconds=1)
        with self.assertRaises(PreflightError):
            get_version(
                str(FAKE_CLAUDE),
                environment=self.environment,
                timeout_seconds=0.05,
                output_limit_bytes=4096,
            )

        self.write_control(delay_seconds=0, version_bytes=2048)
        with self.assertRaises(PreflightError):
            get_version(
                str(FAKE_CLAUDE),
                environment=self.environment,
                timeout_seconds=2,
                output_limit_bytes=64,
            )

    def test_preflight_output_failure_reaps_every_retained_leader(self):
        original_popen = subprocess.Popen
        retained_leaders = []

        class RecordingPopen(original_popen):
            def __init__(self, arguments, *args, **kwargs):
                super().__init__(arguments, *args, **kwargs)
                if list(arguments) == [str(FAKE_CLAUDE), "--version"]:
                    retained_leaders.append(self)

        self.write_control(delay_seconds=0, version_bytes=2048)
        with mock.patch.object(subprocess, "Popen", RecordingPopen):
            for _ in range(8):
                with self.assertRaisesRegex(
                    PreflightError,
                    "output exceeded its byte limit",
                ):
                    get_version(
                        str(FAKE_CLAUDE),
                        environment=self.environment,
                        timeout_seconds=2,
                        output_limit_bytes=64,
                    )

        self.assertEqual(len(retained_leaders), 8)
        self.assertTrue(
            all(process.returncode is not None for process in retained_leaders)
        )

    def test_preflight_timeout_quiesces_double_fork_after_leader_exit(self):
        descendant_log = self.temp_path / "preflight-descendant.jsonl"
        self.write_control(
            preflight_double_fork_pipe_holder=True,
            preflight_descendant_log=str(descendant_log),
        )

        try:
            with self.assertRaises(PreflightError):
                get_version(
                    str(FAKE_CLAUDE),
                    environment=self.environment,
                    timeout_seconds=0.2,
                    output_limit_bytes=4096,
                )
            descendants = read_json_lines(descendant_log)
            self.assertEqual(len(descendants), 1)
            descendant = descendants[0]
            deadline = time.monotonic() + 1
            while (
                process_instance_is_live(descendant["pid"], descendant["identity"])
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            self.assertFalse(
                process_instance_is_live(descendant["pid"], descendant["identity"]),
                "preflight descendant survived leader-exit cleanup",
            )
        finally:
            if descendant_log.exists():
                for descendant in read_json_lines(descendant_log):
                    if process_instance_is_live(
                        descendant["pid"], descendant["identity"]
                    ):
                        os.kill(descendant["pid"], signal.SIGKILL)

    def test_successful_preflight_quiesces_detached_child_before_reaping_leader(self):
        descendant_log = self.temp_path / "preflight-success-descendant.jsonl"
        self.write_control(
            preflight_double_fork_pipe_holder=True,
            preflight_descendant_close_pipes=True,
            preflight_descendant_log=str(descendant_log),
        )

        try:
            version = get_version(
                str(FAKE_CLAUDE),
                environment=self.environment,
                timeout_seconds=2,
                output_limit_bytes=4096,
            )
            self.assertEqual(version, "2.1.248 (Claude Code)")
            descendants = read_json_lines(descendant_log)
            self.assertEqual(len(descendants), 1)
            descendant = descendants[0]
            deadline = time.monotonic() + 1
            while (
                process_instance_is_live(descendant["pid"], descendant["identity"])
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            self.assertFalse(
                process_instance_is_live(descendant["pid"], descendant["identity"]),
                "successful preflight reaped its leader before group quiescence",
            )
        finally:
            if descendant_log.exists():
                for descendant in read_json_lines(descendant_log):
                    if process_instance_is_live(
                        descendant["pid"], descendant["identity"]
                    ):
                        os.kill(descendant["pid"], signal.SIGKILL)

    def test_auth_status_requires_a_successful_json_object(self):
        self.write_control(auth_json="not-json")
        with self.assertRaises(PreflightError):
            get_auth_status(str(FAKE_CLAUDE), environment=self.environment)

        self.write_control(auth_json="[]")
        with self.assertRaises(PreflightError):
            get_auth_status(str(FAKE_CLAUDE), environment=self.environment)

        self.write_control(auth_json=None, auth_exit=7)
        with self.assertRaises(PreflightError):
            get_auth_status(str(FAKE_CLAUDE), environment=self.environment)

    def test_auth_status_accepts_captured_signed_in_first_party_shape(self):
        captured = {
            "loggedIn": True,
            "authMethod": "claude.ai",
            "apiProvider": "firstParty",
        }
        self.write_control(auth_json=json.dumps(captured))

        self.assertEqual(
            get_auth_status(str(FAKE_CLAUDE), environment=self.environment),
            captured,
        )

    def test_preflight_summary_omits_account_identity_fields(self):
        captured = {
            "loggedIn": True,
            "authMethod": "claude.ai",
            "apiProvider": "firstParty",
            "subscriptionType": "pro",
            "email": "identity-canary@example.test",
            "orgId": "organization-identity-canary",
            "orgName": "Identity Canary Organization",
            "futureIdentityField": "future-identity-canary",
        }
        self.write_control(auth_json=json.dumps(captured))

        summary = preflight(
            str(FAKE_CLAUDE),
            environment=self.environment,
        ).redacted_summary()

        self.assertEqual(
            summary["auth_status"],
            {
                "loggedIn": True,
                "authMethod": "claude.ai",
                "apiProvider": "firstParty",
                "subscriptionType": "pro",
            },
        )
        encoded = json.dumps(summary)
        self.assertNotIn("identity-canary", encoded)
        self.assertNotIn("futureIdentityField", encoded)

    def test_auth_status_requires_logged_in_to_be_exact_boolean_true(self):
        invalid_statuses = (
            {"loggedIn": False},
            {},
            {"loggedIn": None},
            {"loggedIn": "true"},
            {"loggedIn": 1},
        )

        for status in invalid_statuses:
            with self.subTest(status=status):
                self.write_control(auth_json=json.dumps(status))
                with self.assertRaisesRegex(PreflightError, "not logged in"):
                    get_auth_status(str(FAKE_CLAUDE), environment=self.environment)

    def test_preflight_classifies_logged_out_auth_status_as_authentication_failure(self):
        self.write_control(auth_json=json.dumps({"loggedIn": False}))

        with self.assertRaises(PreflightError) as raised:
            preflight(str(FAKE_CLAUDE), environment=self.environment)

        self.assertEqual(
            raised.exception.provider_access_classification,
            "authentication",
        )
        self.assertEqual(
            raised.exception.provider_access_reason,
            "authentication_failed",
        )

    def test_parse_extracts_init_assistant_and_result_receipts(self):
        init_payload = valid_init_payload()
        init_payload["memory_paths"] = {
            "auto": "/Users/identity-canary/private-memory"
        }
        init_payload["messaging_socket_path"] = "/tmp/identity-canary.sock"
        init_payload["mcp_server_errors"] = [
            {
                "name": "identity-canary-server",
                "type": "invalid_config",
                "message": "identity-canary@example.test was rejected",
            }
        ]
        assistant_payload = {
            "type": "assistant",
            "session_id": "session-1",
            "uuid": "event-assistant-1",
            "parent_tool_use_id": "tool-parent-1",
            "message": {
                "id": "message-1",
                "model": "fable",
                "content": [{"type": "text", "text": "answer"}],
            },
        }
        result_payload = {
            "type": "result",
            "subtype": "success",
            "session_id": "session-1",
            "uuid": "event-result-1",
            "is_error": False,
            "duration_ms": 5,
            "duration_api_ms": 4,
            "modelUsage": {
                "fable": {
                    "inputTokens": 7,
                    "outputTokens": 3,
                    "cacheReadInputTokens": 1,
                    "cacheCreationInputTokens": 2,
                    "webSearchRequests": 0,
                    "costUSD": 0.25,
                    "contextWindow": 200000,
                    "maxOutputTokens": 32000,
                }
            },
            "num_turns": 2,
            "permission_denials": [],
            "result": "answer",
            "stop_reason": None,
            "total_cost_usd": 0.25,
            "usage": copy.deepcopy(CLAUDE_2_1_248_STANDARD_USAGE),
        }

        init = parse_event(json.dumps(init_payload))
        assistant = parse_event(json.dumps(assistant_payload).encode())
        result = parse_event(json.dumps(result_payload))

        self.assertEqual(init.model, "fable")
        self.assertEqual(init.data, init_payload)
        self.assertEqual(assistant.model, "fable")
        self.assertEqual(assistant.parent_tool_use_id, "tool-parent-1")
        self.assertEqual(result.is_error, False)
        self.assertEqual(result.succeeded, True)
        self.assertEqual(result.model_usage, result_payload["modelUsage"])
        self.assertEqual(result.num_turns, 2)
        self.assertEqual(result.total_cost_usd, Decimal("0.25"))
        self.assertEqual(result.usage, result_payload["usage"])

        summary = init.redacted_summary()
        self.assertEqual(summary["mcp_server_error_count"], 1)
        self.assertEqual(summary["fast_mode_disabled_reason"], "sdk_opt_in_required")
        self.assertNotIn("cwd", summary)
        self.assertNotIn(str(ROOT), json.dumps(summary))
        self.assertNotIn("memory_paths", summary)
        self.assertNotIn("messaging_socket_path", summary)
        self.assertNotIn("mcp_server_errors", summary)
        self.assertIn("identity-canary", json.dumps(init.data))
        self.assertNotIn("identity-canary", json.dumps(summary))

    def test_init_rejects_unknown_or_malformed_2_1_248_fields(self):
        malformed_values = (
            {"agents": "claude"},
            {"analytics_disabled": 0},
            {"capabilities": {}},
            {"betas": {}},
            {"terminal_slash_commands": {}},
            {"effort": []},
            {"fast_mode_disabled_reason": []},
            {"fast_mode_state": ""},
            {"memory_paths": []},
            {"memory_paths": {"auto": 7}},
            {"messaging_socket_path": ""},
            {"product_feedback_disabled": "false"},
            {"mcp_server_errors": {}},
            {"mcp_server_errors": [{"name": "server"}]},
            {
                "mcp_server_errors": [
                    {"name": "server", "type": "invalid_config", "message": 7}
                ]
            },
            {"unexpected_init_identity": "must-fail-closed"},
        )

        for replacement in malformed_values:
            payload = valid_init_payload()
            payload.update(replacement)
            with self.subTest(replacement=replacement), self.assertRaises(
                ClaudeProtocolError
            ):
                parse_event(json.dumps(payload))

    def test_init_permission_mode_malformed_types_fail_as_protocol_errors(self):
        for value in ([], {}):
            payload = valid_init_payload()
            payload["permissionMode"] = value

            with self.subTest(value=value), self.assertRaises(ClaudeProtocolError):
                parse_event(json.dumps(payload))

    def test_provider_controlled_key_names_never_enter_protocol_errors(self):
        canary = "/Users/identity-canary/private@example.test"

        init = valid_init_payload()
        init[canary] = "unexpected"
        with self.assertRaises(ClaudeProtocolError) as raised:
            parse_event(json.dumps(init))
        self.assertNotIn(canary, str(raised.exception))
        self.assertNotIn("identity-canary", str(raised.exception))

        init = valid_init_payload()
        init["memory_paths"] = {canary: 7}
        with self.assertRaises(ClaudeProtocolError) as raised:
            parse_event(json.dumps(init))
        self.assertNotIn(canary, str(raised.exception))
        self.assertNotIn("identity-canary", str(raised.exception))

        task_updated = {
            "type": "system",
            "subtype": "task_updated",
            "task_id": "agent-bounded-4",
            "patch": {canary: "unexpected"},
            "uuid": "event-task-updated-1",
            "session_id": "session-1",
        }
        with self.assertRaises(ClaudeProtocolError) as raised:
            parse_event(json.dumps(task_updated))
        self.assertNotIn(canary, str(raised.exception))
        self.assertNotIn("identity-canary", str(raised.exception))

    def test_init_accepts_documented_optional_2_1_248_fields(self):
        payload = valid_init_payload()
        for name in (
            "agents",
            "analytics_disabled",
            "capabilities",
            "fast_mode_disabled_reason",
            "fast_mode_state",
            "memory_paths",
            "messaging_socket_path",
            "product_feedback_disabled",
        ):
            payload.pop(name)
        payload.update(
            {
                "betas": ["context-1m-2025-08-07"],
                "terminal_slash_commands": ["exit", "statusline"],
                "effort": "max",
            }
        )

        event = parse_event(json.dumps(payload))

        summary = event.redacted_summary()
        self.assertEqual(summary["agent_count"], 0)
        self.assertEqual(summary["capability_count"], 0)
        self.assertEqual(summary["effort"], "max")

    def test_task_notification_uses_bounded_lifecycle_usage_not_result_usage(self):
        payload = {
            "type": "system",
            "subtype": "task_notification",
            "task_id": "agent-bounded-1",
            "tool_use_id": "toolu-bounded-1",
            "status": "completed",
            "output_file": "/tmp/identity-canary-agent-output.txt",
            "summary": "identity-canary background audit complete",
            "usage": {
                "total_tokens": 44_883,
                "tool_uses": 3,
                "duration_ms": 8_424,
            },
            "uuid": "event-task-notification-1",
            "session_id": "session-1",
        }

        event = parse_event(json.dumps(payload))

        self.assertIsNone(event.usage)
        summary = event.redacted_summary()
        self.assertEqual(
            summary,
            {
                "type": "system",
                "subtype": "task_notification",
                "task_id": "agent-bounded-1",
                "tool_use_id": "toolu-bounded-1",
                "status": "completed",
                "usage": payload["usage"],
                "uuid": "event-task-notification-1",
                "session_id": "session-1",
            },
        )
        self.assertNotIn("identity-canary", json.dumps(summary))

    def test_task_notification_accepts_optional_usage_and_hides_output_path(self):
        payload = {
            "type": "system",
            "subtype": "task_notification",
            "task_id": "agent-bounded-2",
            "status": "stopped",
            "output_file": "/tmp/identity-canary-agent-output.txt",
            "summary": "identity-canary stopped",
            "skip_transcript": True,
            "ambient": True,
            "uuid": "event-task-notification-2",
            "session_id": "session-1",
        }

        event = parse_event(json.dumps(payload))

        self.assertIsNone(event.usage)
        summary = event.redacted_summary()
        self.assertEqual(summary["status"], "stopped")
        self.assertNotIn("output_file", summary)
        self.assertNotIn("summary", summary)
        self.assertNotIn("identity-canary", json.dumps(summary))

    def test_lifecycle_plain_string_content_fields_allow_empty_text(self):
        notification = {
            "type": "system",
            "subtype": "task_notification",
            "task_id": "agent-bounded-empty",
            "status": "completed",
            "output_file": "",
            "summary": "",
            "uuid": "event-task-notification-empty",
            "session_id": "session-1",
        }
        updated = {
            "type": "system",
            "subtype": "task_updated",
            "task_id": "agent-bounded-empty",
            "patch": {"description": "", "error": ""},
            "uuid": "event-task-updated-empty",
            "session_id": "session-1",
        }

        started = {
            "type": "system",
            "subtype": "task_started",
            "task_id": "agent-bounded-empty",
            "description": "",
            "prompt": "",
            "task_type": "",
            "subagent_type": "",
            "workflow_name": "",
            "uuid": "event-task-started-empty",
            "session_id": "session-1",
        }
        progress = {
            "type": "system",
            "subtype": "task_progress",
            "task_id": "agent-bounded-empty",
            "description": "",
            "summary": "",
            "subagent_type": "",
            "last_tool_name": "",
            "usage": {"total_tokens": 0, "tool_uses": 0, "duration_ms": 0},
            "uuid": "event-task-progress-empty",
            "session_id": "session-1",
        }
        background = {
            "type": "system",
            "subtype": "background_tasks_changed",
            "tasks": [
                {
                    "task_id": "agent-bounded-empty",
                    "task_type": "",
                    "description": "",
                }
            ],
            "uuid": "event-background-tasks-empty",
            "session_id": "session-1",
        }

        notification_event = parse_event(json.dumps(notification))
        updated_event = parse_event(json.dumps(updated))
        started_event = parse_event(json.dumps(started))
        progress_event = parse_event(json.dumps(progress))
        background_event = parse_event(json.dumps(background))

        self.assertEqual(notification_event.redacted_summary()["status"], "completed")
        self.assertEqual(updated_event.redacted_summary()["patch"], {})
        self.assertNotIn("description", started_event.redacted_summary())
        self.assertNotIn("summary", progress_event.redacted_summary())
        self.assertEqual(
            background_event.redacted_summary()["tasks"],
            [{"task_id": "agent-bounded-empty", "ambient": False}],
        )

    def test_lifecycle_descriptive_fields_use_the_whole_event_bound(self):
        large = "x" * 8_192
        payloads = (
            {
                "type": "system",
                "subtype": "task_notification",
                "task_id": "agent-large",
                "status": "completed",
                "output_file": large,
                "summary": large,
                "uuid": "event-notification-large",
                "session_id": "session-1",
            },
            {
                "type": "system",
                "subtype": "task_started",
                "task_id": "agent-large",
                "description": large,
                "prompt": large,
                "uuid": "event-started-large",
                "session_id": "session-1",
            },
            {
                "type": "system",
                "subtype": "task_progress",
                "task_id": "agent-large",
                "description": large,
                "summary": large,
                "usage": {"total_tokens": 0, "tool_uses": 0, "duration_ms": 0},
                "uuid": "event-progress-large",
                "session_id": "session-1",
            },
            {
                "type": "system",
                "subtype": "task_updated",
                "task_id": "agent-large",
                "patch": {"description": large, "error": large},
                "uuid": "event-updated-large",
                "session_id": "session-1",
            },
            {
                "type": "system",
                "subtype": "background_tasks_changed",
                "tasks": [
                    {
                        "task_id": "agent-large",
                        "task_type": "review",
                        "description": large,
                    }
                ],
                "uuid": "event-background-large",
                "session_id": "session-1",
            },
        )

        for payload in payloads:
            with self.subTest(subtype=payload["subtype"]):
                parse_event(json.dumps(payload))

    def test_task_progress_uses_lifecycle_usage_and_hides_descriptive_content(self):
        payload = {
            "type": "system",
            "subtype": "task_progress",
            "task_id": "agent-bounded-3",
            "tool_use_id": "toolu-bounded-3",
            "description": "identity-canary review",
            "subagent_type": "identity-canary-agent",
            "usage": {
                "total_tokens": 12_345,
                "tool_uses": 2,
                "duration_ms": 4_321,
            },
            "last_tool_name": "mcp__identity-canary__search",
            "summary": "identity-canary is reviewing files",
            "uuid": "event-task-progress-1",
            "session_id": "session-1",
        }

        event = parse_event(json.dumps(payload))

        self.assertIsNone(event.usage)
        summary = event.redacted_summary()
        self.assertEqual(summary["usage"], payload["usage"])
        self.assertNotIn("last_tool_name", summary)
        self.assertNotIn("subagent_type", summary)
        self.assertNotIn("description", summary)
        self.assertNotIn("summary", summary)
        self.assertNotIn("identity-canary", json.dumps(summary))

    def test_task_updated_validates_patch_and_hides_error_text(self):
        payload = {
            "type": "system",
            "subtype": "task_updated",
            "task_id": "agent-bounded-4",
            "patch": {
                "status": "failed",
                "description": "identity-canary review",
                "end_time": 1_780_405_729_183,
                "total_paused_ms": 40,
                "error": "identity-canary failure details",
                "is_backgrounded": True,
            },
            "uuid": "event-task-updated-1",
            "session_id": "session-1",
        }

        event = parse_event(json.dumps(payload))

        summary = event.redacted_summary()
        self.assertEqual(summary["patch"], {"status": "failed", "end_time": 1_780_405_729_183, "total_paused_ms": 40, "is_backgrounded": True})
        self.assertNotIn("identity-canary", json.dumps(summary))

    def test_task_started_background_snapshot_and_thinking_tokens_are_redacted(self):
        payloads = (
            {
                "type": "system",
                "subtype": "task_started",
                "task_id": "agent-bounded-5",
                "tool_use_id": "toolu-bounded-5",
                "description": "identity-canary description",
                "subagent_type": "identity-canary-agent",
                "task_type": "identity-canary-task",
                "prompt": "identity-canary prompt",
                "is_backgrounded": True,
                "spawn_depth": 1,
                "uuid": "event-task-started-1",
                "session_id": "session-1",
            },
            {
                "type": "system",
                "subtype": "background_tasks_changed",
                "tasks": [
                    {
                        "task_id": "agent-bounded-5",
                        "task_type": "identity-canary-task",
                        "description": "identity-canary description",
                        "ambient": False,
                    }
                ],
                "uuid": "event-background-tasks-1",
                "session_id": "session-1",
            },
            {
                "type": "system",
                "subtype": "thinking_tokens",
                "estimated_tokens": 100,
                "estimated_tokens_delta": 25,
                "uuid": "event-thinking-tokens-1",
                "session_id": "session-1",
            },
        )

        summaries = [
            parse_event(json.dumps(payload)).redacted_summary()
            for payload in payloads
        ]

        self.assertEqual(summaries[0]["task_id"], "agent-bounded-5")
        self.assertEqual(
            summaries[1]["tasks"],
            [{"task_id": "agent-bounded-5", "ambient": False}],
        )
        self.assertEqual(summaries[2]["estimated_tokens"], 100)
        self.assertNotIn("identity-canary", json.dumps(summaries))

    def test_lifecycle_events_fail_closed_on_malformed_shapes(self):
        valid = {
            "task_notification": {
                "type": "system",
                "subtype": "task_notification",
                "task_id": "task-1",
                "status": "completed",
                "output_file": "/tmp/bounded-output",
                "summary": "bounded",
                "usage": {"total_tokens": 1, "tool_uses": 1, "duration_ms": 1},
                "uuid": "event-1",
                "session_id": "session-1",
            },
            "task_started": {
                "type": "system",
                "subtype": "task_started",
                "task_id": "task-1",
                "description": "bounded",
                "uuid": "event-2",
                "session_id": "session-1",
            },
            "task_progress": {
                "type": "system",
                "subtype": "task_progress",
                "task_id": "task-1",
                "description": "bounded",
                "usage": {"total_tokens": 1, "tool_uses": 1, "duration_ms": 1},
                "uuid": "event-3",
                "session_id": "session-1",
            },
            "task_updated": {
                "type": "system",
                "subtype": "task_updated",
                "task_id": "task-1",
                "patch": {"status": "completed"},
                "uuid": "event-4",
                "session_id": "session-1",
            },
            "background_tasks_changed": {
                "type": "system",
                "subtype": "background_tasks_changed",
                "tasks": [],
                "uuid": "event-5",
                "session_id": "session-1",
            },
            "thinking_tokens": {
                "type": "system",
                "subtype": "thinking_tokens",
                "estimated_tokens": 1,
                "estimated_tokens_delta": 1,
                "uuid": "event-6",
                "session_id": "session-1",
            },
        }
        malformed = []
        for subtype, payload in valid.items():
            extra = copy.deepcopy(payload)
            extra["unexpected"] = True
            malformed.append((f"{subtype}-unexpected", extra))
        bad_status = copy.deepcopy(valid["task_notification"])
        bad_status["status"] = "unknown"
        malformed.append(("task_notification-status", bad_status))
        bad_usage = copy.deepcopy(valid["task_progress"])
        bad_usage["usage"]["unexpected"] = 1
        malformed.append(("task_progress-usage", bad_usage))
        bad_time = copy.deepcopy(valid["task_updated"])
        bad_time["patch"] = {"end_time": 9_007_199_254_740_992}
        malformed.append(("task_updated-time", bad_time))

        for label, payload in malformed:
            with self.subTest(label=label), self.assertRaises(ClaudeProtocolError):
                parse_event(json.dumps(payload))

        empty_patch = copy.deepcopy(valid["task_updated"])
        empty_patch["patch"] = {}
        self.assertEqual(
            parse_event(json.dumps(empty_patch)).redacted_summary()["patch"],
            {},
        )

    def test_non_result_non_lifecycle_top_level_usage_is_rejected(self):
        assistant = {
            "type": "assistant",
            "session_id": "session-1",
            "uuid": "event-assistant-usage",
            "parent_tool_use_id": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "message": {
                "id": "message-usage",
                "model": "fable",
                "content": [{"type": "text", "text": "answer"}],
            },
        }
        retry = {
            "type": "system",
            "subtype": "api_retry",
            "session_id": "session-1",
            "uuid": "event-retry-usage",
            "attempt": 1,
            "max_retries": 2,
            "retry_delay_ms": 1,
            "error_status": 503,
            "error": "server_error",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

        for payload in (assistant, retry):
            with self.subTest(event=payload["type"]), self.assertRaises(
                ClaudeProtocolError
            ):
                parse_event(json.dumps(payload))

    def test_init_accepts_no_api_key_source_after_provider_free_auth(self):
        payload = valid_init_payload()
        payload["apiKeySource"] = "none"

        event = parse_event(json.dumps(payload))

        self.assertEqual(event.data["apiKeySource"], "none")

    def test_init_rejects_unknown_or_malformed_api_key_sources(self):
        invalid_sources = ("", "NONE", "unknown", None, [], {})

        for source in invalid_sources:
            payload = valid_init_payload()
            payload["apiKeySource"] = source
            with self.subTest(source=source), self.assertRaises(ClaudeProtocolError):
                parse_event(json.dumps(payload))

    def test_result_success_handles_canonical_success_api_error_case(self):
        ordinary_success = self.valid_result_payload()
        success_with_api_error = self.valid_result_payload()
        success_with_api_error["is_error"] = True
        success_with_api_error["api_error_status"] = 500

        self.assertTrue(parse_event(json.dumps(ordinary_success)).succeeded)
        self.assertFalse(parse_event(json.dumps(success_with_api_error)).succeeded)

        invalid_error_union = self.valid_result_payload()
        invalid_error_union["subtype"] = "error_during_execution"
        invalid_error_union["errors"] = ["provider failed"]
        del invalid_error_union["result"]
        with self.assertRaises(ClaudeProtocolError):
            parse_event(json.dumps(invalid_error_union))

    def test_api_retry_is_identified_and_preserved(self):
        payload = {
            "type": "system",
            "subtype": "api_retry",
            "session_id": "session-1",
            "uuid": "event-retry-1",
            "attempt": 1,
            "max_retries": 2,
            "retry_delay_ms": 10,
            "error_status": 503,
            "error": "server_error",
        }
        event = parse_event(json.dumps(payload))

        self.assertTrue(event.is_api_retry)
        self.assertEqual(event.data, payload)

    def test_api_retry_accepts_the_captured_fractional_millisecond_delay(self):
        payload = {
            "type": "system",
            "subtype": "api_retry",
            "attempt": 1,
            "max_retries": 10,
            "retry_delay_ms": 577.9826161079551,
            "error_status": 401,
            "error": "authentication_failed",
            "uuid": "event-retry-fractional-1",
            "session_id": "session-1",
        }

        event = parse_event(json.dumps(payload))

        self.assertEqual(event.api_retry_delay_ms, payload["retry_delay_ms"])
        self.assertEqual(event.api_retry_error_status, 401)
        self.assertEqual(event.api_retry_error, "authentication_failed")

        tracker = ProviderPolicyTracker(
            model_requested="fable",
            max_tokens=1000,
            max_turns=2,
            max_cost_usd=Decimal("0.25"),
        )
        init = valid_init_payload(model="fable")
        init["apiKeySource"] = "none"
        self.assertIsNone(tracker.observe(parse_event(json.dumps(init))))

        decision = tracker.observe(event)

        self.assertEqual(decision.status, "PAUSED_PROVIDER_ACCESS")
        self.assertEqual(decision.classification, "authentication")
        self.assertEqual(decision.reason, "authentication_failed")
        self.assertFalse(decision.allow_substitution)
        self.assertEqual(tracker.retry_count, 0)

    def test_api_retry_accepts_delay_boundary_values(self):
        for delay in (0, 86_400_000):
            payload = {
                "type": "system",
                "subtype": "api_retry",
                "attempt": 1,
                "max_retries": 10,
                "retry_delay_ms": delay,
                "error_status": 503,
                "error": "server_error",
                "uuid": "event-retry-boundary-1",
                "session_id": "session-1",
            }
            with self.subTest(delay=delay):
                self.assertEqual(
                    parse_event(json.dumps(payload)).api_retry_delay_ms,
                    delay,
                )

    def test_api_retry_rejects_non_numeric_or_out_of_range_delays(self):
        invalid_delays = (True, "1.5", None, -0.1, 86_400_000.1)

        for delay in invalid_delays:
            payload = {
                "type": "system",
                "subtype": "api_retry",
                "attempt": 1,
                "max_retries": 10,
                "retry_delay_ms": delay,
                "error_status": 401,
                "error": "authentication_failed",
                "uuid": "event-retry-invalid-delay-1",
                "session_id": "session-1",
            }
            with self.subTest(delay=delay), self.assertRaises(ClaudeProtocolError):
                parse_event(json.dumps(payload))

        missing = payload.copy()
        missing.pop("retry_delay_ms")
        with self.assertRaises(ClaudeProtocolError):
            parse_event(json.dumps(missing))

    def test_receipt_models_and_sessions_are_required_nonempty_and_bounded(self):
        invalid = (
            {"type": "system", "subtype": "init", "model": "fable"},
            {
                "type": "system",
                "subtype": "init",
                "session_id": "",
                "model": "fable",
            },
            {
                "type": "system",
                "subtype": "init",
                "session_id": " ",
                "model": "fable",
            },
            {
                "type": "system",
                "subtype": "init",
                "session_id": "s" * 257,
                "model": "fable",
            },
            {
                "type": "system",
                "subtype": "init",
                "session_id": "session-1",
                "model": "m" * 257,
            },
            {
                "type": "system",
                "subtype": "init",
                "session_id": "session-1",
                "model": "not a model id",
            },
            {
                "type": "assistant",
                "message": {"model": "fable", "content": []},
            },
            {
                "type": "assistant",
                "session_id": "session-1",
                "message": {"model": "", "content": []},
            },
            {"type": "result", "is_error": False},
            {"type": "result", "session_id": "", "is_error": True},
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ClaudeProtocolError):
                parse_event(json.dumps(payload))

    def valid_result_payload(self):
        return {
            "type": "result",
            "subtype": "success",
            "session_id": "session-1",
            "uuid": "event-result-1",
            "is_error": False,
            "duration_ms": 5,
            "duration_api_ms": 4,
            "num_turns": 2,
            "permission_denials": [],
            "result": "answer",
            "stop_reason": None,
            "total_cost_usd": 0.000000123456,
            "modelUsage": {
                "fable": {
                    "inputTokens": 7,
                    "outputTokens": 3,
                    "cacheReadInputTokens": 1,
                    "cacheCreationInputTokens": 2,
                    "webSearchRequests": 0,
                    "contextWindow": 200000,
                    "maxOutputTokens": 32000,
                    "costUSD": 0.000000123456,
                }
            },
            "usage": copy.deepcopy(CLAUDE_2_1_248_STANDARD_USAGE),
        }

    def test_result_receipt_replays_installed_2_1_248_standard_usage(self):
        payload = self.valid_result_payload()
        event = parse_event(json.dumps(payload))
        self.assertEqual(event.model_usage, payload["modelUsage"])
        self.assertEqual(event.usage, CLAUDE_2_1_248_STANDARD_USAGE)
        self.assertEqual(event.num_turns, 2)
        self.assertEqual(event.total_cost_usd, Decimal("0.000000123456"))

    def test_result_accepts_provider_js_double_cost_precision(self):
        payload = self.valid_result_payload()
        payload["total_cost_usd"] = 0.00006180525000000001
        payload["modelUsage"]["fable"]["costUSD"] = 0.5164254999999999

        event = parse_event(json.dumps(payload))

        self.assertEqual(event.total_cost_usd, Decimal("0.00006180525000000001"))
        self.assertEqual(event.model_usage["fable"]["costUSD"], 0.5164254999999999)

    def test_result_accepts_and_validates_2_1_248_model_usage_provenance(self):
        payload = self.valid_result_payload()
        payload["modelUsage"]["fable"].update(
            {
                "canonicalModel": "claude-fable-5",
                "provider": "firstParty",
                "costBasis": "list",
            }
        )

        event = parse_event(json.dumps(payload))

        self.assertEqual(
            event.model_usage["fable"]["canonicalModel"], "claude-fable-5"
        )
        for replacement in (
            {"costBasis": "unbounded"},
            {"provider": "p" * 257},
            {"canonicalModel": []},
            {"unexpected": "identity-canary"},
        ):
            malformed = self.valid_result_payload()
            malformed["modelUsage"]["fable"].update(replacement)
            with self.subTest(replacement=replacement), self.assertRaises(
                ClaudeProtocolError
            ):
                parse_event(json.dumps(malformed))

    def test_zero_turn_result_allows_empty_model_usage_and_preserves_usage(self):
        payload = self.valid_result_payload()
        payload["num_turns"] = 0
        payload["modelUsage"] = {}
        payload["total_cost_usd"] = 0
        payload["usage"] = copy.deepcopy(CLAUDE_2_1_248_ZERO_TURN_USAGE)

        event = parse_event(json.dumps(payload))

        self.assertEqual(event.num_turns, 0)
        self.assertEqual(event.model_usage, {})
        self.assertEqual(event.total_cost_usd, Decimal("0"))
        self.assertEqual(event.usage, CLAUDE_2_1_248_ZERO_TURN_USAGE)

    def test_fake_replays_zero_turn_no_provider_receipt(self):
        self.write_control(num_turns=0, empty_model_usage=True)

        completed, events = self.run_fake()

        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        result = events[-1]
        self.assertEqual(result.num_turns, 0)
        self.assertEqual(result.model_usage, {})
        self.assertEqual(result.total_cost_usd, Decimal("0"))
        self.assertEqual(result.usage, CLAUDE_2_1_248_ZERO_TURN_USAGE)

    def test_only_inference_geo_allows_empty_known_usage_text(self):
        for field in ("service_tier", "speed"):
            payload = self.valid_result_payload()
            payload["usage"][field] = ""
            with self.subTest(field=field), self.assertRaises(ClaudeProtocolError):
                parse_event(json.dumps(payload))

        payload = self.valid_result_payload()
        payload["usage"]["inference_geo"] = "g" * 129
        with self.assertRaises(ClaudeProtocolError):
            parse_event(json.dumps(payload))

    def test_nonzero_turn_result_allows_empty_model_usage(self):
        payload = self.valid_result_payload()
        payload["num_turns"] = 1
        payload["modelUsage"] = {}

        event = parse_event(json.dumps(payload))

        self.assertEqual(event.num_turns, 1)
        self.assertEqual(event.model_usage, {})

    def test_fake_replays_standard_usage_and_full_canonical_model_usage(self):
        completed, events = self.run_fake()

        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        result = events[-1]
        self.assertEqual(result.usage, CLAUDE_2_1_248_STANDARD_USAGE)
        self.assertEqual(
            set(result.model_usage["fable"]),
            {
                "inputTokens",
                "outputTokens",
                "cacheReadInputTokens",
                "cacheCreationInputTokens",
                "webSearchRequests",
                "costUSD",
                "contextWindow",
                "maxOutputTokens",
                "canonicalModel",
                "provider",
                "costBasis",
            },
        )

    def test_result_usage_preserves_bounded_unknown_extensions_without_interpretation(self):
        payload = self.valid_result_payload()
        payload["usage"].update(
            {
                "future_tokens": -7,
                "future_cost": -0.25,
                "future_extension": {
                    "enabled": True,
                    "label": "bounded",
                    "items": [None, 1, 1.25, {"nested": False}],
                },
            }
        )
        payload["usage"]["server_tool_use"]["future_tool_receipt"] = {
            "status": "ok"
        }

        event = parse_event(json.dumps(payload))

        self.assertEqual(event.usage, payload["usage"])
        self.assertEqual(event.usage["future_tokens"], -7)
        self.assertEqual(event.usage["future_cost"], -0.25)

    def test_result_usage_rejects_unbounded_unknown_extensions(self):
        too_deep = "leaf"
        for _ in range(8):
            too_deep = {"nested": too_deep}
        too_many_nodes = [
            {f"field-{field}": field for field in range(4)} for _ in range(64)
        ]
        extensions = (
            {"future": {"k" * 129: True}},
            {"future": "s" * 4097},
            {"future": list(range(65))},
            {"future": too_deep},
            {"future": too_many_nodes},
            {"future": 1_000_000_001},
            {"future": -1_000_000_001},
        )
        for extension in extensions:
            payload = self.valid_result_payload()
            payload["usage"].update(extension)
            with self.subTest(extension=extension), self.assertRaises(
                ClaudeProtocolError
            ):
                parse_event(json.dumps(payload))

    def test_iterations_and_speed_use_generic_extension_bounds(self):
        payload = self.valid_result_payload()
        payload["usage"]["iterations"] = list(range(65))
        with self.assertRaisesRegex(ClaudeProtocolError, "container bound"):
            parse_event(json.dumps(payload))

        payload = self.valid_result_payload()
        payload["usage"]["speed"] = "s" * 4097
        with self.assertRaisesRegex(ClaudeProtocolError, "text bound"):
            parse_event(json.dumps(payload))

    def test_result_receipt_schemas_reject_unbounded_or_invalid_known_values(self):
        mutations = []

        payload = self.valid_result_payload()
        payload["modelUsage"] = {
            f"model-{index}": {"inputTokens": 1, "outputTokens": 1, "costUSD": 0}
            for index in range(17)
        }
        mutations.append(payload)
        for field, value in (
            ("inputTokens", True),
            ("outputTokens", -1),
            ("contextWindow", 1_000_000_001),
            ("costUSD", 1e-25),
            ("costUSD", 1_000_000.000001),
            ("unknownCount", 1),
        ):
            payload = self.valid_result_payload()
            payload["modelUsage"]["fable"][field] = value
            mutations.append(payload)
        payload = self.valid_result_payload()
        del payload["modelUsage"]["fable"]["outputTokens"]
        mutations.append(payload)

        for field, value in (
            ("input_tokens", True),
            ("output_tokens", -1),
        ):
            payload = self.valid_result_payload()
            payload["usage"][field] = value
            mutations.append(payload)
        payload = self.valid_result_payload()
        payload["usage"]["server_tool_use"]["web_search_requests"] = True
        mutations.append(payload)
        payload = self.valid_result_payload()
        del payload["usage"]["output_tokens"]
        mutations.append(payload)

        for field, value in (
            ("num_turns", 1_000_001),
            ("total_cost_usd", 1e-25),
            ("total_cost_usd", 1_000_000.000001),
        ):
            payload = self.valid_result_payload()
            payload[field] = value
            mutations.append(payload)

        for payload in mutations:
            with self.subTest(payload=payload), self.assertRaises(ClaudeProtocolError):
                parse_event(json.dumps(payload))

    def test_iter_events_binds_expected_session_and_requires_newline_frames(self):
        payloads = (
            valid_init_payload(),
            {
                "type": "assistant",
                "session_id": "session-1",
                "uuid": "event-assistant-1",
                "parent_tool_use_id": None,
                "message": {"id": "message-1", "model": "fable", "content": []},
            },
            self.valid_result_payload(),
        )
        stream = b"".join(
            json.dumps(payload, separators=(",", ":")).encode() + b"\n"
            for payload in payloads
        )
        self.assertEqual(
            len(list(iter_events(io.BytesIO(stream), expected_session_id="session-1"))),
            3,
        )
        with self.assertRaises(ClaudeProtocolError):
            list(iter_events(io.BytesIO(stream), expected_session_id="other-session"))
        with self.assertRaises(ClaudeProtocolError):
            list(iter_events(io.BytesIO(stream.rstrip(b"\n"))))

    def test_malformed_and_oversized_events_fail_closed(self):
        malformed = (
            b"",
            b"{not-json}",
            b"[]",
            b'{"subtype":"init"}',
            b'{"type":3}',
            b'{"type":"result","subtype":"success"}',
            b'{"type":"result","is_error":"false"}',
            b'{"type":"system","subtype":"init","model":3}',
            b'{"type":"assistant","message":[]}',
            b'{"type":"assistant","message":{"model":3}}',
            b'{"type":"assistant","message":{},"parent_tool_use_id":3}',
            b'{"type":"result","is_error":false,"num_turns":true}',
            b'{"type":"result","is_error":false,"modelUsage":[]}',
            b'{"type":"result","is_error":false,"usage":[]}',
            b'{"type":"result","is_error":false,"total_cost_usd":"secret"}',
            b'{"type":"system","type":"result","is_error":false}',
            b'{"type":"system","value":NaN}',
            b'"\xff"',
        )
        for payload in malformed:
            with self.subTest(payload=payload), self.assertRaises(ClaudeProtocolError):
                parse_event(payload)

        with self.assertRaises(ClaudeProtocolError):
            parse_event(b"X" * (MAX_EVENT_BYTES + 1))

    def test_iter_events_stops_on_first_malformed_or_oversized_line(self):
        valid = b'{"type":"system","subtype":"status"}\n'
        with self.assertRaises(ClaudeProtocolError):
            list(iter_events(io.BytesIO(valid + b"{bad}\n" + valid)))
        with self.assertRaises(ClaudeProtocolError):
            list(iter_events(io.BytesIO(b"X" * (MAX_EVENT_BYTES + 1) + b"\n")))

    def test_fake_can_emit_every_provider_error_for_later_classification(self):
        error_names = {
            "authentication",
            "billing",
            "credits",
            "rate_limit",
            "model_unavailable",
            "permission",
            "sandbox_unavailable",
            "transient_transport",
            "protocol",
            "cancellation",
            "internal",
        }
        for error_name in sorted(error_names):
            with self.subTest(error_name=error_name):
                self.write_control(provider_error=error_name)
                completed, events = self.run_fake()
                self.assertEqual(completed.returncode, 0, completed.stderr.decode())
                self.assertTrue(events[-1].is_error)
                canonical_api_errors = [
                    event.provider_error for event in events if event.provider_error
                ]
                if error_name in {
                    "authentication",
                    "billing",
                    "credits",
                    "rate_limit",
                    "model_unavailable",
                    "transient_transport",
                    "protocol",
                    "internal",
                }:
                    self.assertEqual(len(canonical_api_errors), 1)
                    self.assertEqual(events[-1].subtype, "success")
                    self.assertNotIn("errors", events[-1].data)
                else:
                    self.assertEqual(canonical_api_errors, [])
                    self.assertEqual(events[-1].subtype, "error_during_execution")
                    self.assertTrue(events[-1].errors)
                if error_name == "transient_transport":
                    self.assertTrue(any(event.is_api_retry for event in events))

    def test_fake_can_spawn_a_same_process_group_descendant(self):
        descendant_log = self.temp_path / "descendant.jsonl"
        self.write_control(
            spawn_descendant=True,
            descendant_log=str(descendant_log),
        )
        job = self.spec()
        process = subprocess.Popen(
            build_command(job, executable=str(FAKE_CLAUDE)),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.environment,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(encode_user_message(job.prompt), timeout=5)
            self.assertEqual(process.returncode, 0, stderr.decode())
            self.assertTrue(list(iter_events(io.BytesIO(stdout))))
            descendant_pid = read_json_lines(descendant_log)[0]["pid"]
            self.assertTrue(process_exists(descendant_pid))
            self.assertEqual(os.getpgid(descendant_pid), process.pid)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 1
            while "descendant_pid" in locals() and process_exists(descendant_pid):
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.01)


if __name__ == "__main__":
    unittest.main()
