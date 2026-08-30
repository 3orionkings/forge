import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from forge_lib import boundaries as boundaries_module  # noqa: E402
from forge_lib.boundaries import (  # noqa: E402
    BoundaryError,
    inspect_repo,
    validate_boundary,
    validate_launch,
)
from forge_lib.contracts import MAX_PROMPT_BYTES  # noqa: E402


def run_git(*arguments):
    return subprocess.run(
        ["git", *(str(argument) for argument in arguments)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.rstrip("\r\n")


def create_repository(path):
    path.mkdir(parents=True)
    run_git("init", "--quiet", path)
    run_git("-C", path, "config", "user.name", "Forge Tests")
    run_git("-C", path, "config", "user.email", "forge-tests@example.invalid")
    (path / "tracked.txt").write_text("committed\n", encoding="utf-8")
    run_git("-C", path, "add", "tracked.txt")
    run_git("-C", path, "commit", "--quiet", "-m", "fixture")


def write_executable(path, source):
    path.write_text(source, encoding="utf-8")
    path.chmod(0o700)


def terminate_process_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)


def run_python_bounded(source, *, environment=None, timeout=2):
    process = subprocess.Popen(
        [sys.executable, "-c", source],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        terminate_process_group(process)
        raise AssertionError("inspection subprocess exceeded its test deadline") from error
    return process.returncode, stdout, stderr


def process_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def kill_recorded_processes(pid_file):
    if not pid_file.exists():
        return
    pids = [int(value) for value in pid_file.read_text().split()]
    if pids:
        try:
            os.killpg(pids[0], signal.SIGKILL)
        except ProcessLookupError:
            pass
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class SecurityBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        # macOS exposes /var as a /private/var alias; fixture inputs are canonical
        # so alias rejection is tested only by the explicit symlink cases below.
        self.temp_path = Path(self.temporary_directory.name).resolve(strict=True)
        self.launch_workspace = self.temp_path / "launch"
        self.primary = self.launch_workspace / "primary"
        self.linked_worktree = self.temp_path / "linked-worktree"
        self.unrelated = self.temp_path / "launch-unrelated"

        create_repository(self.primary)
        create_repository(self.unrelated)
        run_git(
            "-C",
            self.primary,
            "worktree",
            "add",
            "--quiet",
            "-b",
            "linked-fixture",
            self.linked_worktree,
        )

    def test_inspect_repo_uses_bounded_structured_git_identity_commands(self):
        real_popen = subprocess.Popen
        calls = []

        def recording_popen(arguments, **kwargs):
            calls.append((arguments, kwargs))
            return real_popen(arguments, **kwargs)

        with mock.patch(
            "forge_lib.boundaries.subprocess.Popen",
            side_effect=recording_popen,
        ):
            inspect_repo(self.primary)

        cwd = str(self.primary.resolve(strict=True))
        snapshot_commands = [
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            ["git", "-C", cwd, "rev-parse", "--git-common-dir"],
            ["git", "-C", cwd, "rev-parse", "HEAD"],
            [
                "git",
                "-C",
                cwd,
                "status",
                "--porcelain=v2",
                "--untracked-files=all",
            ],
            ["git", "-C", cwd, "ls-files", "-v", "-z"],
            ["git", "-C", cwd, "ls-files", "--stage", "-z"],
        ]
        self.assertEqual(
            [list(arguments) for arguments, _ in calls],
            snapshot_commands * 2,
        )
        for _, kwargs in calls:
            self.assertFalse(kwargs.get("shell", False))
            self.assertTrue(kwargs["start_new_session"])
            self.assertEqual(kwargs["env"]["GIT_OPTIONAL_LOCKS"], "0")

    def test_inspect_repo_disables_repository_fsmonitor_commands(self):
        marker = self.temp_path / "fsmonitor-executed"
        hook = self.temp_path / "fsmonitor-hook.py"
        hook.write_text(
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).touch()\n",
            encoding="utf-8",
        )
        hook.chmod(0o700)
        run_git("-C", self.primary, "config", "core.fsmonitor", hook)

        inspect_repo(self.primary)

        self.assertFalse(marker.exists())

    def test_inspect_repo_rejects_local_config_includes_before_git(self):
        included = self.temp_path / "included.config"
        included.write_text("[core]\n\tfsmonitor = false\n", encoding="utf-8")

        for index, section in enumerate(
            ("[include]", '[includeIf "gitdir:**"]'),
        ):
            with self.subTest(section=section):
                repository = self.temp_path / f"include-repo-{index}"
                create_repository(repository)
                with (repository / ".git" / "config").open(
                    "a",
                    encoding="utf-8",
                ) as config:
                    config.write(f"\n{section}\n\tpath = {included}\n")

                with mock.patch(
                    "forge_lib.boundaries._run_git",
                    side_effect=AssertionError("Git ran before config rejection"),
                ):
                    with self.assertRaises(BoundaryError):
                        inspect_repo(repository)

    def test_inspect_repo_rejects_bom_prefixed_config_include_before_git(self):
        repository = self.temp_path / "bom-include-repo"
        create_repository(repository)
        included = self.temp_path / "bom-included.config"
        included.write_text("[core]\n\tfsmonitor = false\n", encoding="utf-8")
        (repository / ".git" / "config").write_bytes(
            b"\xef\xbb\xbf[include]\n\tpath = "
            + os.fsencode(included)
            + b"\n"
        )

        with mock.patch(
            "forge_lib.boundaries._run_git",
            side_effect=AssertionError("Git ran before BOM include rejection"),
        ):
            with self.assertRaises(BoundaryError):
                inspect_repo(repository)

    def test_inspect_repo_rejects_include_fifo_without_hanging(self):
        include_fifo = self.temp_path / "hostile-include.fifo"
        os.mkfifo(include_fifo)
        with (self.primary / ".git" / "config").open("a", encoding="utf-8") as config:
            config.write(f"\n[include]\n\tpath = {include_fifo}\n")

        source = (
            "import sys\n"
            f"sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
            "from forge_lib.boundaries import BoundaryError, inspect_repo\n"
            "try:\n"
            f"    inspect_repo({str(self.primary)!r})\n"
            "except BoundaryError:\n"
            "    pass\n"
            "else:\n"
            "    raise SystemExit('hostile include was admitted')\n"
        )
        returncode, _, stderr = run_python_bounded(source)
        self.assertEqual(returncode, 0, os.fsdecode(stderr))

    def test_inspect_repo_rejects_head_change_during_identity_sampling(self):
        real_run_git = boundaries_module._run_git
        injected = False

        def run_git_then_commit(cwd, *arguments):
            nonlocal injected
            output = real_run_git(cwd, *arguments)
            if arguments[0] == "status" and not injected:
                injected = True
                (self.primary / "tracked.txt").write_text(
                    "new commit\n",
                    encoding="utf-8",
                )
                run_git("-C", self.primary, "add", "tracked.txt")
                run_git(
                    "-C",
                    self.primary,
                    "commit",
                    "--quiet",
                    "-m",
                    "racing commit",
                )
            return output

        with mock.patch(
            "forge_lib.boundaries._run_git",
            side_effect=run_git_then_commit,
        ):
            with self.assertRaises(BoundaryError):
                inspect_repo(self.primary)
        self.assertTrue(injected)

    def test_inspect_repo_rejects_assume_unchanged_and_skip_worktree_flags(self):
        flag_cases = (
            ("--assume-unchanged", "--no-assume-unchanged"),
            ("--skip-worktree", "--no-skip-worktree"),
        )
        for enable, disable in flag_cases:
            with self.subTest(flag=enable):
                run_git("-C", self.primary, "update-index", enable, "tracked.txt")
                try:
                    with self.assertRaises(BoundaryError):
                        inspect_repo(self.primary)
                finally:
                    run_git("-C", self.primary, "update-index", disable, "tracked.txt")

    def test_inspect_repo_does_not_refresh_index_bytes_or_mtime(self):
        index = self.primary / ".git" / "index"
        tracked = self.primary / "tracked.txt"
        before_bytes = index.read_bytes()
        before_mtime = index.stat().st_mtime_ns
        tracked_stat = tracked.stat()
        os.utime(
            tracked,
            ns=(tracked_stat.st_atime_ns, tracked_stat.st_mtime_ns + 2_000_000_000),
        )

        inspect_repo(self.primary)

        self.assertEqual(index.read_bytes(), before_bytes)
        self.assertEqual(index.stat().st_mtime_ns, before_mtime)

    def test_inspect_repo_timeout_kills_the_git_process_group(self):
        fake_bin = self.temp_path / "timeout-bin"
        fake_bin.mkdir()
        pid_file = self.temp_path / "timeout-pids"
        self.addCleanup(kill_recorded_processes, pid_file)
        write_executable(
            fake_bin / "git",
            "#!/usr/bin/env python3\n"
            "import os, signal, subprocess, sys, time\n"
            "from pathlib import Path\n"
            "child = subprocess.Popen([\n"
            "    sys.executable, '-c',\n"
            "    'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)',\n"
            "])\n"
            "Path(os.environ['FORGE_PID_FILE']).write_text(\n"
            "    f'{os.getpid()} {child.pid}', encoding='utf-8'\n"
            ")\n"
            "time.sleep(60)\n",
        )
        environment = os.environ.copy()
        environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
        environment["FORGE_PID_FILE"] = str(pid_file)
        source = (
            "import sys\n"
            f"sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
            "from forge_lib import boundaries\n"
            # Give the fake interpreter enough time to start its descendant and
            # publish the PID receipt even when the full suite is under load. The
            # command still has to time out, and the assertions below still prove
            # that the complete process group is quiescent afterward.
            "boundaries._GIT_TIMEOUT_SECONDS = 1.0\n"
            "boundaries._GIT_TERMINATE_GRACE_SECONDS = 0.05\n"
            "try:\n"
            f"    boundaries.inspect_repo({str(self.primary)!r})\n"
            "except boundaries.BoundaryError:\n"
            "    pass\n"
            "else:\n"
            "    raise SystemExit('hung Git command was admitted')\n"
        )

        returncode, _, stderr = run_python_bounded(
            source,
            environment=environment,
        )

        self.assertEqual(returncode, 0, os.fsdecode(stderr))
        self.assertTrue(pid_file.exists())
        pids = [int(value) for value in pid_file.read_text().split()]
        deadline = time.monotonic() + 1
        while any(process_exists(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(all(not process_exists(pid) for pid in pids))

    def test_inspect_repo_bounds_stdout_and_stderr_capture(self):
        fake_bin = self.temp_path / "output-bin"
        fake_bin.mkdir()
        write_executable(
            fake_bin / "git",
            "#!/usr/bin/env python3\n"
            "import os, time\n"
            "from pathlib import Path\n"
            "Path(os.environ['FORGE_PID_FILE']).write_text(\n"
            "    str(os.getpid()), encoding='utf-8'\n"
            ")\n"
            "descriptor = int(os.environ['FORGE_OUTPUT_DESCRIPTOR'])\n"
            "for _ in range(32):\n"
            "    os.write(descriptor, b'x' * 65536)\n"
            "time.sleep(60)\n",
        )

        for descriptor in (1, 2):
            with self.subTest(descriptor=descriptor):
                pid_file = self.temp_path / f"output-pid-{descriptor}"
                self.addCleanup(kill_recorded_processes, pid_file)
                environment = os.environ.copy()
                environment["PATH"] = (
                    f"{fake_bin}{os.pathsep}{environment['PATH']}"
                )
                environment["FORGE_PID_FILE"] = str(pid_file)
                environment["FORGE_OUTPUT_DESCRIPTOR"] = str(descriptor)
                source = (
                    "import sys\n"
                    f"sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
                    "from forge_lib import boundaries\n"
                    "boundaries._GIT_CAPTURE_LIMIT_BYTES = 4096\n"
                    "boundaries._GIT_TIMEOUT_SECONDS = 5\n"
                    "try:\n"
                    f"    boundaries.inspect_repo({str(self.primary)!r})\n"
                    "except boundaries.BoundaryError:\n"
                    "    pass\n"
                    "else:\n"
                    "    raise SystemExit('oversized Git output was admitted')\n"
                )

                returncode, _, stderr = run_python_bounded(
                    source,
                    environment=environment,
                )
                self.assertEqual(returncode, 0, os.fsdecode(stderr))

    def test_inspect_repo_returns_canonical_immutable_worktree_identity(self):
        primary = inspect_repo(self.primary)
        linked = inspect_repo(self.linked_worktree)

        self.assertEqual(primary.top_level, self.primary.resolve(strict=True))
        self.assertEqual(linked.top_level, self.linked_worktree.resolve(strict=True))
        self.assertEqual(primary.common_dir, linked.common_dir)
        self.assertEqual(primary.head, linked.head)
        self.assertEqual(primary.normalized_status, "")
        self.assertEqual(
            primary.status_fingerprint,
            hashlib.sha256(b"").hexdigest(),
        )
        with self.assertRaises(FrozenInstanceError):
            primary.head = "0" * len(primary.head)

    def test_inspect_repo_fingerprints_actual_tracked_content(self):
        tracked = self.primary / "tracked.txt"
        tracked.write_text("dirty version one\n", encoding="utf-8")
        first = inspect_repo(self.primary)
        tracked.write_text("dirty version two\n", encoding="utf-8")
        second = inspect_repo(self.primary)

        self.assertEqual(second.head, first.head)
        self.assertEqual(second.status_fingerprint, first.status_fingerprint)
        self.assertNotEqual(
            second.tracked_content_fingerprint,
            first.tracked_content_fingerprint,
        )

    def test_boundary_accepts_contained_repo_and_linked_worktree(self):
        contained = validate_boundary(
            self.primary,
            self.launch_workspace,
            extra_roots=[],
        )
        linked = validate_boundary(
            self.linked_worktree,
            self.launch_workspace,
            extra_roots=[],
        )

        self.assertEqual(contained.top_level, self.primary.resolve(strict=True))
        self.assertEqual(linked.common_dir, contained.common_dir)

    def test_boundary_rejects_counterfeit_gitfile_pointing_at_allowed_common_dir(self):
        counterfeit = self.temp_path / "counterfeit-worktree"
        counterfeit.mkdir()
        (counterfeit / "tracked.txt").write_bytes(
            (self.primary / "tracked.txt").read_bytes()
        )
        (counterfeit / ".git").write_text(
            f"gitdir: {self.primary / '.git'}\n",
            encoding="utf-8",
        )

        with self.assertRaises(BoundaryError):
            validate_boundary(
                counterfeit,
                self.launch_workspace,
                extra_roots=[],
            )

    def test_boundary_rejects_contained_counterfeit_linked_worktree_marker(self):
        counterfeit = self.launch_workspace / "counterfeit-worktree"
        counterfeit.mkdir()
        (counterfeit / "tracked.txt").write_bytes(
            (self.primary / "tracked.txt").read_bytes()
        )
        (counterfeit / ".git").write_bytes(
            (self.linked_worktree / ".git").read_bytes()
        )

        with self.assertRaises(BoundaryError):
            validate_boundary(
                counterfeit,
                self.launch_workspace,
                extra_roots=[],
            )

    def test_boundary_rejects_missing_or_mismatched_worktree_backpointer(self):
        marker = self.linked_worktree / ".git"
        git_dir_text = marker.read_text(encoding="utf-8").removeprefix("gitdir: ")
        git_dir = Path(git_dir_text.strip()).resolve(strict=True)
        backpointer = git_dir / "gitdir"
        original = backpointer.read_bytes()
        mutations = (
            None,
            f"{self.primary / '.git'}\n".encode(),
        )

        for mutation in mutations:
            with self.subTest(mutation=mutation):
                if mutation is None:
                    backpointer.unlink()
                else:
                    backpointer.write_bytes(mutation)
                try:
                    with self.assertRaises(BoundaryError):
                        validate_boundary(
                            self.linked_worktree,
                            self.launch_workspace,
                            extra_roots=[],
                        )
                finally:
                    backpointer.write_bytes(original)

    def test_boundary_rejects_worktree_commondir_outside_registration_root(self):
        marker = self.linked_worktree / ".git"
        git_dir_text = marker.read_text(encoding="utf-8").removeprefix("gitdir: ")
        git_dir = Path(git_dir_text.strip()).resolve(strict=True)
        commondir = git_dir / "commondir"
        original = commondir.read_bytes()
        alternate_common = self.launch_workspace / "alternate-common"
        shutil.copytree(self.primary / ".git", alternate_common)
        commondir.write_text(f"{alternate_common}\n", encoding="utf-8")

        try:
            with self.assertRaises(BoundaryError):
                validate_boundary(
                    self.linked_worktree,
                    self.launch_workspace,
                    extra_roots=[],
                )
        finally:
            commondir.write_bytes(original)

    def test_boundary_rejects_allowed_subdirectory_of_an_outside_repository(self):
        outside_repo = self.temp_path / "outside-parent"
        create_repository(outside_repo)
        allowed_subdirectory = outside_repo / "allowed-subdirectory"
        allowed_subdirectory.mkdir()

        with self.assertRaises(BoundaryError):
            validate_boundary(
                allowed_subdirectory,
                allowed_subdirectory,
                extra_roots=[],
            )

    def test_boundary_accepts_repo_under_explicit_extra_root(self):
        extra_root = self.temp_path / "approved-extra"
        extra_repo = extra_root / "repo"
        create_repository(extra_repo)

        identity = validate_boundary(
            extra_repo,
            self.launch_workspace,
            extra_roots=[extra_root],
        )

        self.assertEqual(identity.top_level, extra_repo.resolve(strict=True))

    def test_boundary_rejects_unrelated_repo_with_similar_path_prefix(self):
        with self.assertRaises(BoundaryError):
            validate_boundary(
                self.unrelated,
                self.launch_workspace,
                extra_roots=[],
            )

    def test_boundary_rejects_nul_and_parent_traversal(self):
        invalid_paths = (
            f"{self.primary}\0suffix",
            str(self.primary / ".." / "primary"),
        )
        for invalid_path in invalid_paths:
            with self.subTest(path=repr(invalid_path)):
                with self.assertRaises(BoundaryError):
                    validate_boundary(
                        invalid_path,
                        self.launch_workspace,
                        extra_roots=[],
                    )

    def test_boundary_rejects_symlink_aliases_and_escapes(self):
        contained_alias = self.launch_workspace / "contained-alias"
        escape_alias = self.launch_workspace / "escape-alias"
        contained_alias.symlink_to(self.primary, target_is_directory=True)
        escape_alias.symlink_to(self.unrelated, target_is_directory=True)

        for alias in (contained_alias, escape_alias):
            with self.subTest(alias=alias):
                with self.assertRaises(BoundaryError):
                    validate_boundary(
                        alias,
                        self.launch_workspace,
                        extra_roots=[],
                    )

    def test_boundary_rejects_a_non_repository(self):
        non_repository = self.launch_workspace / "ordinary-directory"
        non_repository.mkdir()

        with self.assertRaises(BoundaryError):
            validate_boundary(
                non_repository,
                self.launch_workspace,
                extra_roots=[],
            )

    def test_clean_build_requires_the_exact_expected_head(self):
        identity = inspect_repo(self.primary)
        validated = validate_launch(
            self.primary,
            self.launch_workspace,
            role="build",
            expected_head=identity.head,
            prompt="implement the frozen unit",
            extra_roots=[],
        )
        self.assertEqual(validated, identity)

        with self.assertRaises(BoundaryError):
            validate_launch(
                self.primary,
                self.launch_workspace,
                role="build",
                expected_head="0" * len(identity.head),
                prompt="implement the frozen unit",
                extra_roots=[],
            )

    def test_build_rejects_dirty_status_even_when_fingerprint_matches(self):
        (self.primary / "untracked.txt").write_text("dirty\n", encoding="utf-8")
        dirty = inspect_repo(self.primary)

        with self.assertRaises(BoundaryError):
            validate_launch(
                self.primary,
                self.launch_workspace,
                role="build",
                expected_head=dirty.head,
                expected_status_fingerprint=dirty.status_fingerprint,
                prompt="implement the frozen unit",
                extra_roots=[],
            )

    def test_review_rejects_dirty_status_even_when_fingerprint_matches(self):
        (self.primary / "review-notes.txt").write_text("first\n", encoding="utf-8")
        frozen = inspect_repo(self.primary)

        with self.assertRaises(BoundaryError):
            validate_launch(
                self.primary,
                self.launch_workspace,
                role="review",
                expected_head=frozen.head,
                expected_status_fingerprint=frozen.status_fingerprint,
                prompt="review the frozen plan",
                extra_roots=[],
            )

        with self.assertRaises(BoundaryError):
            validate_launch(
                self.primary,
                self.launch_workspace,
                role="review",
                expected_head=frozen.head,
                prompt="review the frozen plan",
                extra_roots=[],
            )

    def test_review_rejects_tracked_content_drift_hidden_by_same_status_fingerprint(self):
        tracked = self.primary / "tracked.txt"
        tracked.write_text("dirty version one\n", encoding="utf-8")
        frozen = inspect_repo(self.primary)
        tracked.write_text("dirty version two\n", encoding="utf-8")
        changed = inspect_repo(self.primary)

        self.assertEqual(changed.status_fingerprint, frozen.status_fingerprint)
        with self.assertRaises(BoundaryError):
            validate_launch(
                self.primary,
                self.launch_workspace,
                role="review",
                expected_head=frozen.head,
                expected_status_fingerprint=frozen.status_fingerprint,
                prompt="review the frozen plan",
                extra_roots=[],
            )

    def test_review_rejects_untracked_content_drift_hidden_by_same_status_fingerprint(self):
        untracked = self.primary / "untracked.txt"
        untracked.write_text("dirty version one\n", encoding="utf-8")
        frozen = inspect_repo(self.primary)
        untracked.write_text("dirty version two\n", encoding="utf-8")
        changed = inspect_repo(self.primary)

        self.assertEqual(changed.status_fingerprint, frozen.status_fingerprint)
        with self.assertRaises(BoundaryError):
            validate_launch(
                self.primary,
                self.launch_workspace,
                role="review",
                expected_head=frozen.head,
                expected_status_fingerprint=frozen.status_fingerprint,
                prompt="review the frozen plan",
                extra_roots=[],
            )

    def test_launch_rejects_prompt_over_the_utf8_byte_limit(self):
        identity = inspect_repo(self.primary)
        oversized_prompt = "é" * (MAX_PROMPT_BYTES // 2 + 1)

        with self.assertRaises(BoundaryError):
            validate_launch(
                self.primary,
                self.launch_workspace,
                role="build",
                expected_head=identity.head,
                prompt=oversized_prompt,
                extra_roots=[],
            )


if __name__ == "__main__":
    unittest.main()
