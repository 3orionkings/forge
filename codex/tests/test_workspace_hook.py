import concurrent.futures
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "hooks.json"
SCRIPT = ROOT / "scripts" / "forge_workspace_hook.py"
TOOLS = (
    "forge_preflight",
    "forge_start",
    "forge_status",
    "forge_wait",
    "forge_result",
    "forge_resume",
    "forge_steer",
    "forge_cancel",
    "forge_list",
)


class WorkspaceHookManifestTests(unittest.TestCase):
    def test_manifest_matches_only_the_nine_forge_tools(self):
        self.assertTrue(HOOKS.is_file(), "hooks.json is missing")
        manifest = json.loads(HOOKS.read_text(encoding="utf-8"))
        entries = manifest["hooks"]["PreToolUse"]
        self.assertEqual(len(entries), 1)
        matcher = re.compile(entries[0]["matcher"])

        for tool in TOOLS:
            self.assertIsNotNone(matcher.fullmatch(tool), tool)
            self.assertIsNotNone(matcher.fullmatch(f"mcp__forge__{tool}"), tool)
        for tool in (
            "forge_delete",
            "forge_start_extra",
            "prefix_forge_start",
            "mcp__other__forge_start",
            "mcp__forge__forge_delete",
            "Bash",
        ):
            self.assertIsNone(matcher.fullmatch(tool), tool)

        hooks = entries[0]["hooks"]
        self.assertEqual(
            hooks,
            [
                {
                    "type": "command",
                    "command": "python3 ./scripts/forge_workspace_hook.py",
                    "timeout": 5,
                    "statusMessage": "Binding Forge workspace",
                }
            ],
        )


class WorkspaceHookTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="forge-workspace-hook-")
        self.addCleanup(temporary.cleanup)
        self.temp_path = Path(temporary.name).resolve(strict=True)
        self.home = self.temp_path / "home"
        self.home.mkdir(mode=0o700)
        self.binding_root = self.temp_path / "bindings"
        self.workspace = self.temp_path / "workspace"
        self.workspace.mkdir()
        self.environment = {
            "HOME": str(self.home),
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "FORGE_WORKSPACE_BINDINGS": str(self.binding_root),
        }

    def payload(self, **updates):
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "mcp__forge__forge_start",
            "session_id": "session-1234",
            "cwd": str(self.workspace),
            "tool_input": {"prompt": "bounded work"},
        }
        payload.update(updates)
        return payload

    def invoke(self, payload, *, environment=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=self.environment if environment is None else environment,
            timeout=3,
        )

    def invoke_raw(self, payload, *, environment=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=payload,
            text=True,
            capture_output=True,
            env=self.environment if environment is None else environment,
            timeout=3,
        )

    def assert_denied(self, process):
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, "")
        output = json.loads(process.stdout)["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PreToolUse")
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertNotIn("updatedInput", output)
        self.assertEqual(
            output["permissionDecisionReason"],
            "Forge workspace binding denied",
        )
        return output

    def test_first_call_creates_private_binding_and_updates_input(self):
        process = self.invoke(self.payload())

        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, "")
        result = json.loads(process.stdout)
        output = result["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PreToolUse")
        self.assertEqual(output["permissionDecision"], "allow")
        updated_input = output["updatedInput"]
        self.assertEqual(updated_input["prompt"], "bounded work")
        workspace_binding = updated_input["_forge_workspace"]
        self.assertEqual(set(workspace_binding), {"session_id", "token"})
        self.assertEqual(workspace_binding["session_id"], "session-1234")
        self.assertRegex(workspace_binding["token"], r"[A-Za-z0-9_-]{43}\Z")

        expected_name = hashlib.sha256(b"session-1234").hexdigest() + ".json"
        binding_path = self.binding_root / expected_name
        self.assertTrue(binding_path.is_file())
        self.assertEqual(stat.S_IMODE(self.binding_root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(binding_path.stat().st_mode), 0o600)
        self.assertEqual(
            json.loads(binding_path.read_text(encoding="utf-8")),
            {
                "version": 1,
                "session_id": "session-1234",
                "cwd": str(self.workspace),
                "token": workspace_binding["token"],
            },
        )

    def test_same_session_and_workspace_reuses_one_token(self):
        first = self.invoke(self.payload())
        second = self.invoke(self.payload())

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        first_binding = json.loads(first.stdout)["hookSpecificOutput"][
            "updatedInput"
        ]["_forge_workspace"]
        second_binding = json.loads(second.stdout)["hookSpecificOutput"][
            "updatedInput"
        ]["_forge_workspace"]
        self.assertEqual(first_binding, second_binding)
        self.assertEqual(len(list(self.binding_root.glob("*.json"))), 1)

    def test_concurrent_first_calls_converge_on_one_binding(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            processes = list(
                executor.map(lambda _index: self.invoke(self.payload()), range(4))
            )

        for process in processes:
            self.assertEqual(process.returncode, 0, process.stderr)
        tokens = {
            json.loads(process.stdout)["hookSpecificOutput"]["updatedInput"][
                "_forge_workspace"
            ]["token"]
            for process in processes
        }
        self.assertEqual(len(tokens), 1)
        self.assertEqual(len(list(self.binding_root.glob("*.json"))), 1)

    def test_same_session_cannot_be_rebound_to_another_workspace(self):
        first = self.invoke(self.payload())
        original = next(self.binding_root.glob("*.json")).read_bytes()
        other_workspace = self.temp_path / "other-workspace"
        other_workspace.mkdir()

        second = self.invoke(self.payload(cwd=str(other_workspace)))

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assert_denied(second)
        self.assertEqual(next(self.binding_root.glob("*.json")).read_bytes(), original)

    def test_malformed_or_spoofed_payloads_are_denied(self):
        missing_workspace = self.temp_path / "missing-workspace"
        cases = {
            "wrong event": self.payload(hook_event_name="PostToolUse"),
            "wrong tool": self.payload(tool_name="mcp__other__forge_start"),
            "missing session": self.payload(session_id=None),
            "unsafe session": self.payload(session_id="../escape"),
            "relative cwd": self.payload(cwd="relative/workspace"),
            "missing cwd": self.payload(cwd=str(missing_workspace)),
            "non-object input": self.payload(tool_input=[]),
            "reserved input": self.payload(
                tool_input={
                    "prompt": "bounded work",
                    "_forge_workspace": {
                        "session_id": "spoofed",
                        "token": "x" * 43,
                    },
                }
            ),
        }
        alias = self.temp_path / "workspace-alias"
        alias.symlink_to(self.workspace, target_is_directory=True)
        cases["symlink cwd"] = self.payload(cwd=str(alias))

        for label, payload in cases.items():
            with self.subTest(label=label):
                self.assert_denied(self.invoke(payload))

    def test_malformed_json_is_denied(self):
        self.assert_denied(self.invoke_raw("{not-json"))

    def test_corrupt_existing_binding_is_denied_without_replacement(self):
        first = self.invoke(self.payload())
        self.assertEqual(first.returncode, 0, first.stderr)
        binding_path = next(self.binding_root.glob("*.json"))
        binding_path.write_text("{corrupt", encoding="utf-8")

        second = self.invoke(self.payload())

        self.assert_denied(second)
        self.assertEqual(binding_path.read_text(encoding="utf-8"), "{corrupt")

    def test_symlink_or_group_writable_binding_root_is_denied(self):
        real_root = self.temp_path / "real-bindings"
        real_root.mkdir(mode=0o700)
        alias = self.temp_path / "binding-alias"
        alias.symlink_to(real_root, target_is_directory=True)
        symlink_environment = dict(self.environment)
        symlink_environment["FORGE_WORKSPACE_BINDINGS"] = str(alias)
        self.assert_denied(
            self.invoke(self.payload(), environment=symlink_environment)
        )
        self.assertEqual(list(real_root.iterdir()), [])

        unsafe_root = self.temp_path / "unsafe-bindings"
        unsafe_root.mkdir(mode=0o700)
        unsafe_root.chmod(0o770)
        unsafe_environment = dict(self.environment)
        unsafe_environment["FORGE_WORKSPACE_BINDINGS"] = str(unsafe_root)
        self.assert_denied(self.invoke(self.payload(), environment=unsafe_environment))
        self.assertEqual(list(unsafe_root.iterdir()), [])

    def test_symlink_binding_file_is_denied_without_following_it(self):
        self.binding_root.mkdir(mode=0o700)
        outside = self.temp_path / "outside.json"
        outside.write_text("outside\n", encoding="utf-8")
        binding_name = hashlib.sha256(b"session-1234").hexdigest() + ".json"
        (self.binding_root / binding_name).symlink_to(outside)

        self.assert_denied(self.invoke(self.payload()))
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")

    def test_default_root_is_scoped_below_test_home(self):
        environment = dict(self.environment)
        environment.pop("FORGE_WORKSPACE_BINDINGS")

        process = self.invoke(self.payload(), environment=environment)

        self.assertEqual(process.returncode, 0, process.stderr)
        default_root = self.home / ".codex" / "forge" / "workspace-bindings"
        self.assertEqual(len(list(default_root.glob("*.json"))), 1)
        self.assertFalse(self.binding_root.exists())

    def test_missing_nofollow_or_directory_open_flag_fails_closed(self):
        spec = importlib.util.spec_from_file_location("forge_workspace_hook", SCRIPT)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        checker = getattr(module, "_require_secure_open_flags", None)
        self.assertIsNotNone(checker, "secure-open flag guard is missing")

        for flag in ("O_NOFOLLOW", "O_DIRECTORY"):
            with self.subTest(flag=flag), mock.patch.object(module, flag, 0):
                with self.assertRaises(module.BindingError):
                    checker()


if __name__ == "__main__":
    unittest.main()
