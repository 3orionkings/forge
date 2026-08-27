import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.forge_lib.contracts import ContractError, validate_tool_arguments
from scripts.forge_lib.security import SecurityError
from scripts.forge_lib.service import get_request_service


SESSION_ID = "019c1234-5678-7abc-8def-0123456789ab"
TOKEN = "A" * 43
ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "scripts" / "forge_bridge.py"
HOOK = ROOT / "scripts" / "forge_workspace_hook.py"


class WorkspaceBindingTests(unittest.TestCase):
    def _repository(self, root: Path) -> Path:
        repository = root / "project"
        repository.mkdir()
        subprocess.run(
            ["git", "init", "-q", str(repository)],
            check=True,
            capture_output=True,
        )
        (repository / "tracked.txt").write_text("workspace binding\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(repository), "add", "tracked.txt"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.email=forge@example.test",
                "-c",
                "user.name=Forge Workspace Test",
                "-c",
                "commit.gpgsign=false",
                "-C",
                str(repository),
                "commit",
                "-qm",
                "workspace fixture",
            ],
            check=True,
            capture_output=True,
        )
        return repository.resolve(strict=True)

    def _binding(self, root: Path, repository: Path) -> Path:
        binding_root = root / "bindings"
        binding_root.mkdir(mode=0o700)
        binding_root.chmod(0o700)
        name = hashlib.sha256(SESSION_ID.encode("utf-8")).hexdigest()
        binding = binding_root / f"{name}.json"
        binding.write_text(
            json.dumps(
                {
                    "version": 1,
                    "session_id": SESSION_ID,
                    "cwd": str(repository),
                    "token": TOKEN,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        binding.chmod(0o600)
        return binding_root

    def _environment(self, root: Path, binding_root: Path) -> dict[str, str]:
        return {
            "HOME": str(root / "home"),
            "PATH": "/usr/bin:/bin",
            "FORGE_STATE_PATH": str(root / "state" / "state.sqlite3"),
            "FORGE_CLAUDE_EXECUTABLE": "/usr/bin/false",
            "FORGE_WORKSPACE_BINDINGS": str(binding_root),
        }

    def test_all_tool_contracts_accept_only_the_reserved_hook_context_shape(self):
        context = {"session_id": SESSION_ID, "token": TOKEN}
        validate_tool_arguments("forge_list", {"_forge_workspace": context})

        with self.assertRaises(ContractError):
            validate_tool_arguments(
                "forge_list",
                {"_forge_workspace": {**context, "cwd": "/tmp/spoof"}},
            )
        with self.assertRaises(ContractError):
            validate_tool_arguments(
                "forge_list",
                {"_forge_workspace": {"session_id": SESSION_ID, "token": "short"}},
            )

    def test_bound_request_uses_hook_workspace_and_strips_the_capability(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(strict=True)
            repository = self._repository(root)
            binding_root = self._binding(root, repository)
            environment = self._environment(root, binding_root)
            (root / "home").mkdir()

            arguments = {
                "cwd": str(repository),
                "_forge_workspace": {"session_id": SESSION_ID, "token": TOKEN},
            }
            with mock.patch.dict(os.environ, environment, clear=True):
                service, sanitized = get_request_service(arguments)

            self.assertEqual(service.launch_workspace, repository)
            self.assertEqual(sanitized, {"cwd": str(repository)})
            self.assertTrue((root / "state" / "state.sqlite3").is_file())

    def test_missing_or_wrong_capability_fails_before_state_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(strict=True)
            repository = self._repository(root)
            binding_root = self._binding(root, repository)
            environment = self._environment(root, binding_root)
            (root / "home").mkdir()

            for context in (None, {"session_id": SESSION_ID, "token": "B" * 43}):
                arguments = {"cwd": str(repository)}
                if context is not None:
                    arguments["_forge_workspace"] = context
                with self.subTest(context=context):
                    with mock.patch.dict(os.environ, environment, clear=True):
                        with self.assertRaises(SecurityError):
                            get_request_service(arguments)

            self.assertFalse((root / "state" / "state.sqlite3").exists())

    def test_explicit_launch_workspace_remains_available_for_controlled_harnesses(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(strict=True)
            repository = self._repository(root)
            environment = {
                "HOME": str(root / "home"),
                "PATH": "/usr/bin:/bin",
                "FORGE_STATE_PATH": str(root / "state" / "state.sqlite3"),
                "FORGE_LAUNCH_WORKSPACE": str(repository),
                "FORGE_CLAUDE_EXECUTABLE": "/usr/bin/false",
            }
            (root / "home").mkdir()

            with mock.patch.dict(os.environ, environment, clear=True):
                service, sanitized = get_request_service({"limit": 1})

            self.assertEqual(service.launch_workspace, repository)
            self.assertEqual(sanitized, {"limit": 1})

    def test_installed_style_hook_to_mcp_journey_binds_the_active_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(strict=True)
            repository = self._repository(root)
            binding_root = root / "bindings"
            home = root / "home"
            home.mkdir()
            environment = {
                "HOME": str(home),
                "PATH": "/usr/bin:/bin",
                "LANG": "C",
                "LC_ALL": "C",
                "FORGE_STATE_PATH": str(root / "state" / "state.sqlite3"),
                "FORGE_CLAUDE_EXECUTABLE": "/usr/bin/false",
                "FORGE_WORKSPACE_BINDINGS": str(binding_root),
            }
            hook_payload = {
                "hook_event_name": "PreToolUse",
                "tool_name": "mcp__forge__forge_list",
                "session_id": SESSION_ID,
                "cwd": str(repository),
                "tool_input": {"limit": 1},
            }
            hooked = subprocess.run(
                [sys.executable, str(HOOK)],
                input=json.dumps(hook_payload),
                text=True,
                capture_output=True,
                env=environment,
                check=True,
            )
            arguments = json.loads(hooked.stdout)["hookSpecificOutput"][
                "updatedInput"
            ]

            bridge = subprocess.Popen(
                [sys.executable, "-u", str(BRIDGE)],
                cwd=ROOT,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            try:
                request = {
                    "jsonrpc": "2.0",
                    "id": "bound-list",
                    "method": "tools/call",
                    "params": {"name": "forge_list", "arguments": arguments},
                }
                assert bridge.stdin is not None
                assert bridge.stdout is not None
                bridge.stdin.write(json.dumps(request).encode("utf-8") + b"\n")
                bridge.stdin.flush()
                response = json.loads(bridge.stdout.readline())
            finally:
                if bridge.stdin is not None:
                    bridge.stdin.close()
                try:
                    bridge.wait(timeout=3)
                finally:
                    for stream in (bridge.stdout, bridge.stderr):
                        if stream is not None:
                            stream.close()

            self.assertTrue(bridge.stdout.closed)
            self.assertTrue(bridge.stderr.closed)

            result = response["result"]
            self.assertFalse(result["isError"], result)
            self.assertEqual(result["structuredContent"]["status"], "ok")
            self.assertEqual(result["structuredContent"]["jobs"], [])
            self.assertNotEqual(repository, ROOT)


if __name__ == "__main__":
    unittest.main()
