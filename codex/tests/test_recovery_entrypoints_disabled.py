"""Offline proofs that every shipped recovery entrypoint refuses without effects."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SHELL_ENTRYPOINT = ROOT / "scripts" / "recovery-watchdog.sh"
PYTHON_ENTRYPOINT = ROOT / "scripts" / "forge_lib" / "recovery.py"
DISABLED_MESSAGE = "Automated Forge recovery is disabled in this build"

sys.path.insert(0, str(ROOT / "scripts"))

from forge_lib import recovery


class RecoveryEntrypointsDisabledTests(unittest.TestCase):
    def test_shell_entrypoint_refuses_without_python_or_state_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(strict=True)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            python_marker = root / "python-invoked"
            fake_python = fake_bin / "python3"
            fake_python.write_text(
                "#!/bin/sh\nprintf invoked > \"$FORGE_RECOVERY_PYTHON_MARKER\"\nexit 97\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o700)
            state_root = root / "must-not-exist"
            environment = {
                "HOME": str(root),
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "FORGE_RECOVERY_PYTHON_MARKER": str(python_marker),
            }

            completed = subprocess.run(
                [
                    "/bin/bash",
                    str(SHELL_ENTRYPOINT),
                    "init",
                    "--state",
                    str(state_root),
                    "--run",
                    "r",
                    "--unit",
                    "u",
                    "--owner",
                    "claude",
                    "--peer",
                    "sol",
                    "--integration-authority",
                    "codex",
                ],
                cwd=ROOT,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, recovery.RC_ERROR)
            self.assertEqual(completed.stdout, "")
            self.assertIn(DISABLED_MESSAGE, completed.stderr)
            self.assertFalse(python_marker.exists())
            self.assertFalse(state_root.exists())

    def test_python_run_refuses_before_parser_or_store_effect(self) -> None:
        with (
            mock.patch.object(
                recovery,
                "_parser",
                side_effect=AssertionError("disabled recovery parsed a command"),
            ) as parser,
            mock.patch.object(
                recovery,
                "Store",
                side_effect=AssertionError("disabled recovery opened state"),
            ) as store,
        ):
            with self.assertRaisesRegex(recovery.WatchdogError, DISABLED_MESSAGE):
                recovery.run(["status", "--state", "/must/not/exist", "--run", "r"])
        parser.assert_not_called()
        store.assert_not_called()

    def test_python_script_refuses_without_creating_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(strict=True)
            state_root = root / "must-not-exist"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(PYTHON_ENTRYPOINT),
                    "init",
                    "--state",
                    str(state_root),
                    "--run",
                    "r",
                    "--unit",
                    "u",
                    "--owner",
                    "claude",
                    "--peer",
                    "sol",
                    "--integration-authority",
                    "codex",
                ],
                cwd=ROOT,
                env={
                    "HOME": str(root),
                    "PATH": "/usr/bin:/bin",
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                },
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, recovery.RC_ERROR)
            self.assertEqual(completed.stdout, "")
            self.assertIn(DISABLED_MESSAGE, completed.stderr)
            self.assertFalse(state_root.exists())


if __name__ == "__main__":
    unittest.main()
