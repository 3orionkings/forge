import json
import os
import select
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "scripts" / "forge_bridge.py"
MCP_CONFIG = ROOT / ".mcp.json"
TOOLS = ["forge_preflight", "forge_start", "forge_status", "forge_wait", "forge_result", "forge_resume", "forge_steer", "forge_cancel", "forge_list"]


def _isolated_test_environment(root):
    workspace = root / "workspace"
    state_directory = root / "state"
    home = root / "home"
    temporary = root / "tmp"
    deny_bin = root / "deny-bin"
    for directory in (workspace, state_directory, home, temporary, deny_bin):
        directory.mkdir(parents=True, exist_ok=True)

    provider_sentinel = root / "provider-invoked"
    provider = deny_bin / "claude"
    provider.write_text(
        "#!/bin/sh\n"
        "printf 'Forge protocol tests forbid provider execution\\n' >&2\n"
        ': > "${FORGE_PROVIDER_CALL_SENTINEL:?}"\n'
        "exit 97\n",
        encoding="utf-8",
    )
    provider.chmod(0o700)

    return {
        "HOME": str(home),
        "PATH": f"{deny_bin}:/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "LC_CTYPE": "C",
        "TMPDIR": str(temporary),
        "SHELL": "/bin/sh",
        "USER": "forge-test",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "UV_OFFLINE": "1",
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "FORGE_STATE_PATH": str(state_directory / "state.sqlite3"),
        "FORGE_LAUNCH_WORKSPACE": str(workspace),
        "FORGE_CLAUDE_EXECUTABLE": str(provider),
        "FORGE_PROVIDER_CALL_SENTINEL": str(provider_sentinel),
    }


def _initialize_test_repository(workspace, environment):
    subprocess.run(
        ["git", "init", "-q", str(workspace)],
        check=True,
        capture_output=True,
        env=environment,
    )
    (workspace / "tracked.txt").write_text("protocol fixture\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(workspace), "add", "tracked.txt"],
        check=True,
        capture_output=True,
        env=environment,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=forge@example.test",
            "-c",
            "user.name=Forge Protocol Test",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(workspace),
            "commit",
            "-qm",
            "protocol fixture",
        ],
        check=True,
        capture_output=True,
        env=environment,
    )


class TestProtocolEnvironmentIsolation(unittest.TestCase):
    def test_mcp_server_pins_no_machine_specific_environment(self):
        config = json.loads(MCP_CONFIG.read_text(encoding="utf-8"))
        forge = config["mcpServers"]["forge"]

        self.assertNotIn("env", forge)

    def test_environment_replaces_inherited_forge_state_and_provider_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(strict=True)
            inherited_state = root / "inherited-live-state.sqlite3"
            inherited_workspace = root / "inherited-live-workspace"
            inherited_workspace.mkdir()
            with mock.patch.dict(
                os.environ,
                {
                    "FORGE_STATE_PATH": str(inherited_state),
                    "FORGE_LAUNCH_WORKSPACE": str(inherited_workspace),
                    "FORGE_CLAUDE_EXECUTABLE": "/inherited/claude",
                    "ANTHROPIC_API_KEY": "must-not-cross-test-boundary",
                    "CLAUDE_CODE_OAUTH_TOKEN": "must-not-cross-test-boundary",
                    "OPENAI_API_KEY": "must-not-cross-test-boundary",
                },
            ):
                factory = globals().get("_isolated_test_environment")
                self.assertIsNotNone(factory, "isolated test environment is missing")
                environment = factory(root)

            self.assertEqual(
                environment["FORGE_STATE_PATH"],
                str(root / "state" / "state.sqlite3"),
            )
            self.assertEqual(
                environment["FORGE_LAUNCH_WORKSPACE"],
                str(root / "workspace"),
            )
            self.assertEqual(
                environment["FORGE_CLAUDE_EXECUTABLE"],
                str(root / "deny-bin" / "claude"),
            )
            self.assertNotIn("ANTHROPIC_API_KEY", environment)
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", environment)
            self.assertNotIn("OPENAI_API_KEY", environment)

    def test_bridge_harness_passes_the_explicit_environment_to_popen(self):
        environment = {"FORGE_STATE_PATH": "/test-owned/state.sqlite3"}
        with mock.patch.object(subprocess, "Popen") as popen:
            try:
                BridgeHarness(environment)
            except TypeError:
                self.fail("BridgeHarness must require an explicit environment")

        self.assertEqual(popen.call_args.kwargs["env"], environment)


class BridgeHarness:
    def __init__(self, environment):
        self.process = subprocess.Popen(
            [sys.executable, "-u", str(BRIDGE)],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(environment),
        )

    def send(self, message):
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        self.process.stdin.flush()

    def read(self, timeout=2):
        ready, _, _ = select.select([self.process.stdout], [], [], timeout)
        if not ready:
            return None
        line = self.process.stdout.readline()
        return json.loads(line) if line else None

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            self.process.wait(timeout=2)
        self.process.stdout.close()
        self.process.stderr.close()


class TestMCPProtocol(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._temporary = tempfile.TemporaryDirectory(prefix="forge-protocol-")
        cls.addClassCleanup(cls._temporary.cleanup)
        cls.temp_path = Path(cls._temporary.name).resolve(strict=True)
        cls.environment = _isolated_test_environment(cls.temp_path)
        cls.workspace = Path(cls.environment["FORGE_LAUNCH_WORKSPACE"])
        cls.state_path = Path(cls.environment["FORGE_STATE_PATH"])
        cls.provider_sentinel = Path(
            cls.environment["FORGE_PROVIDER_CALL_SENTINEL"]
        )
        _initialize_test_repository(cls.workspace, cls.environment)

    def setUp(self):
        self.bridge = BridgeHarness(self.environment)

    def tearDown(self):
        self.bridge.close()
        self.assertFalse(
            self.provider_sentinel.exists(),
            "protocol test attempted to invoke a provider",
        )

    def test_initialize_negotiates_protocol_and_advertises_tools(self):
        self.bridge.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
        response = self.bridge.read()
        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["id"], 1)
        self.assertEqual(response["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(response["result"]["serverInfo"], {"name": "forge", "version": "0.1.0"})
        self.assertIn("tools", response["result"]["capabilities"])

    def test_initialized_notification_emits_no_reply_and_ping_works(self):
        self.bridge.send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        self.assertIsNone(self.bridge.read(timeout=0.15))
        self.bridge.send({"jsonrpc": "2.0", "id": "p", "method": "ping", "params": {}})
        self.assertEqual(self.bridge.read(), {"jsonrpc": "2.0", "id": "p", "result": {}})

    def test_invalid_notification_emits_no_reply_and_keeps_stream_aligned(self):
        self.bridge.send({"jsonrpc": "2.0", "method": "ping", "params": 42})
        self.assertIsNone(self.bridge.read(timeout=0.15))
        self.bridge.send({"jsonrpc": "2.0", "id": "aligned", "method": "ping", "params": {}})
        self.assertEqual(self.bridge.read(), {"jsonrpc": "2.0", "id": "aligned", "result": {}})

    def test_malformed_no_id_objects_return_invalid_request_and_keep_stream_aligned(self):
        for malformed in ({}, {"jsonrpc": "1.0", "method": "ping"}):
            self.bridge.send(malformed)
            response = self.bridge.read()
            self.assertEqual(response["error"]["code"], -32600)
            self.assertIsNone(response["id"])
            self.bridge.send({"jsonrpc": "2.0", "id": "aligned", "method": "ping", "params": {}})
            self.assertEqual(self.bridge.read(), {"jsonrpc": "2.0", "id": "aligned", "result": {}})

    def test_id_bearing_canonical_initialized_method_returns_unknown_method(self):
        self.bridge.send({"jsonrpc": "2.0", "id": "canonical", "method": "notifications/initialized", "params": {}})
        response = self.bridge.read()
        self.assertEqual(response["id"], "canonical")
        self.assertEqual(response["error"]["code"], -32601)

    def test_legacy_initialized_request_with_id_is_unknown_method(self):
        self.bridge.send({"jsonrpc": "2.0", "id": "legacy", "method": "initialized", "params": {}})
        response = self.bridge.read()
        self.assertEqual(response["id"], "legacy")
        self.assertEqual(response["error"]["code"], -32601)

    def test_tools_list_has_exact_strict_tool_schemas(self):
        self.bridge.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = self.bridge.read()["result"]["tools"]
        self.assertEqual([tool["name"] for tool in listed], TOOLS)
        for tool in listed:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertFalse(tool["inputSchema"]["additionalProperties"])

    def test_task_nine_controls_are_routed(self):
        arguments = {
            "forge_resume": {"job_id": "job-1", "revision": 1},
            "forge_steer": {"job_id": "job-1", "revision": 1, "prompt": "change"},
            "forge_cancel": {"job_id": "job-1", "revision": 1},
        }
        for index, name in enumerate(arguments, start=10):
            self.bridge.send({"jsonrpc": "2.0", "id": index, "method": "tools/call", "params": {"name": name, "arguments": arguments[name]}})
            result = self.bridge.read()["result"]
            self.assertTrue(result["isError"], name)
            self.assertEqual(result["structuredContent"]["status"], "rejected")
            self.assertEqual(result["content"][0]["type"], "text")

    def test_json_rpc_errors_are_structured(self):
        cases = [(b"{not-json}\n", -32700, None), (json.dumps({"jsonrpc": "1.0", "id": 1, "method": "ping"}).encode() + b"\n", -32600, 1), (json.dumps({"jsonrpc": "2.0", "id": {"bad": True}, "method": "ping"}).encode() + b"\n", -32600, None), (json.dumps({"jsonrpc": "2.0", "id": 2, "method": "missing"}).encode() + b"\n", -32601, 2), (json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping", "params": []}).encode() + b"\n", -32602, 3)]
        for payload, code, request_id in cases:
            self.bridge.process.stdin.write(payload)
            self.bridge.process.stdin.flush()
            response = self.bridge.read()
            self.assertEqual(response["error"]["code"], code)
            self.assertEqual(response.get("id"), request_id)

    def test_oversized_line_is_rejected_without_killing_stream(self):
        self.bridge.process.stdin.write(b"{" + b"a" * 1_048_576 + b"}\n")
        self.bridge.process.stdin.flush()
        self.assertEqual(self.bridge.read()["error"]["code"], -32600)
        self.bridge.send({"jsonrpc": "2.0", "id": 4, "method": "ping", "params": {}})
        self.assertEqual(self.bridge.read()["result"], {})

    def test_clean_eof_exits_without_stdout_diagnostics(self):
        self.bridge.process.stdin.close()
        self.bridge.process.wait(timeout=2)
        self.assertEqual(self.bridge.process.returncode, 0)
        self.assertEqual(self.bridge.process.stdout.read(), b"")

    def test_worker_mode_requires_configured_durable_state(self):
        process = subprocess.run(
            [sys.executable, str(BRIDGE), "--worker", "missing-job"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            env=dict(self.environment),
        )
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("failed to initialize", process.stderr.lower())
