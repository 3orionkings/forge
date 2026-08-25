import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from forge_lib.security import (  # noqa: E402
    ANTHROPIC_AUTH_NAMES,
    BASE_ENV,
    NEVER_INHERIT_PREFIXES,
    REDACTION,
    SecurityError,
    build_child_environment,
    build_claude_settings,
    redact_sensitive,
    redact_text,
    write_private_settings,
)


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def run_crashing_settings_writer(settings_path, private_root, settings, stage):
    source = (
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
        "from forge_lib import security\n"
        f"settings_path = Path({str(settings_path)!r})\n"
        f"private_root = Path({str(private_root)!r})\n"
        f"settings = {settings!r}\n"
        f"stage = {stage!r}\n"
        "if stage == 'during_write':\n"
        "    real_write = security.os.write\n"
        "    def crash_write(file_descriptor, data):\n"
        "        chunk = bytes(data[:max(1, len(data) // 3)])\n"
        "        real_write(file_descriptor, chunk)\n"
        "        os._exit(73)\n"
        "    security.os.write = crash_write\n"
        "elif stage == 'after_publish':\n"
        "    real_link = security.os.link\n"
        "    def crash_link(*args, **kwargs):\n"
        "        real_link(*args, **kwargs)\n"
        "        os._exit(74)\n"
        "    security.os.link = crash_link\n"
        "else:\n"
        "    raise RuntimeError('unknown crash stage')\n"
        "security.write_private_settings(\n"
        "    settings_path, settings, allowed_root=private_root\n"
        ")\n"
    )
    return subprocess.run(
        [sys.executable, "-c", source],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=10,
    )


class BridgeSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temp_path = Path(self.temporary_directory.name).resolve(strict=True)
        self.worktree = self.temp_path / "admitted-worktree"
        self.worktree.mkdir(mode=0o700)

    def test_security_constant_sets_match_the_frozen_policy(self):
        self.assertEqual(
            BASE_ENV,
            {
                "HOME",
                "PATH",
                "LANG",
                "LC_ALL",
                "LC_CTYPE",
                "TMPDIR",
                "SHELL",
                "USER",
            },
        )
        self.assertEqual(
            NEVER_INHERIT_PREFIXES,
            ("FORGE_", "AWS_", "GITHUB_", "GOOGLE_", "AZURE_", "OPENAI_"),
        )
        self.assertEqual(
            ANTHROPIC_AUTH_NAMES,
            {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"},
        )

    def test_default_child_environment_inherits_only_the_baseline(self):
        parent = {
            "HOME": "/safe/home",
            "PATH": "/safe/bin",
            "LANG": "en_US.UTF-8",
            "LC_ALL": "C.UTF-8",
            "LC_CTYPE": "UTF-8",
            "TMPDIR": "/safe/tmp",
            "SHELL": "/bin/zsh",
            "USER": "forge-user",
            "TERM": "xterm-256color",
            "PYTHONPATH": "/not/inherited",
            "AWS_SECRET_ACCESS_KEY": "aws-secret-canary",
            "FORGE_BRIDGE_TOKEN": "forge-secret-canary",
            "ANTHROPIC_API_KEY": "anthropic-secret-canary",
        }

        child = build_child_environment(parent)

        self.assertEqual(child, {name: parent[name] for name in BASE_ENV})
        self.assertNotIn("aws-secret-canary", child.values())
        self.assertNotIn("forge-secret-canary", child.values())
        self.assertNotIn("anthropic-secret-canary", child.values())

    def test_anthropic_auth_requires_an_explicit_exact_name_allowlist(self):
        parent = {
            "PATH": "/safe/bin",
            "ANTHROPIC_API_KEY": "anthropic-api-canary",
            "CLAUDE_CODE_OAUTH_TOKEN": "claude-oauth-canary",
            "anthropic_api_key": "wrong-case-canary",
        }

        child = build_child_environment(
            parent,
            allowed_auth_names={"ANTHROPIC_API_KEY"},
        )

        self.assertEqual(
            child,
            {
                "PATH": "/safe/bin",
                "ANTHROPIC_API_KEY": "anthropic-api-canary",
            },
        )
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", child)
        self.assertNotIn("anthropic_api_key", child)

    def test_bridge_and_cloud_secrets_are_removed_even_when_requested(self):
        parent = {
            "HOME": "/safe/home",
            "FORGE_BRIDGE_TOKEN": "forge-secret-canary",
            "AWS_SECRET_ACCESS_KEY": "aws-secret-canary",
            "GITHUB_TOKEN": "github-secret-canary",
            "GOOGLE_API_KEY": "google-secret-canary",
            "AZURE_CLIENT_SECRET": "azure-secret-canary",
            "OPENAI_API_KEY": "openai-secret-canary",
            "ANTHROPIC_API_KEY": "anthropic-secret-canary",
        }
        requested = set(parent) | {"CLAUDE_CODE_OAUTH_TOKEN"}

        child = build_child_environment(parent, allowed_auth_names=requested)

        self.assertEqual(
            child,
            {
                "HOME": "/safe/home",
                "ANTHROPIC_API_KEY": "anthropic-secret-canary",
            },
        )
        for prefix in NEVER_INHERIT_PREFIXES:
            self.assertFalse(any(name.startswith(prefix) for name in child))

    def test_redact_text_covers_bearer_assignments_and_common_key_shapes(self):
        secrets = {
            "bearer": "bearer-token-canary-1234567890",
            "token": "plain-token-canary-1234567890",
            "api_key": "api-key-canary-1234567890",
            "anthropic": "sk-ant-api03-abcdefghijklmnopqrstuvwxyz123456",
            "openai": "sk-proj-abcdefghijklmnopqrstuvwxyz123456",
            "aws_access": "AKIAIOSFODNN7EXAMPLE",
            "github": "ghp_abcdefghijklmnopqrstuvwxyz1234567890",
        }
        message = (
            f"Authorization: Bearer {secrets['bearer']}\n"
            f"token={secrets['token']}\n"
            f'{{"api_key": "{secrets["api_key"]}"}}\n'
            f"anthropic={secrets['anthropic']} openai={secrets['openai']}\n"
            f"aws={secrets['aws_access']} github={secrets['github']}"
        )

        redacted = redact_text(message)

        self.assertGreaterEqual(redacted.count(REDACTION), len(secrets))
        for secret in secrets.values():
            self.assertNotIn(secret, redacted)
        self.assertIn("Authorization: Bearer", redacted)
        self.assertIn("token=", redacted)

    def test_redact_sensitive_recurses_through_summary_and_receipt_values(self):
        bearer = "receipt-bearer-canary-1234567890"
        api_key = "receipt-api-key-canary-1234567890"
        token = "receipt-token-canary-1234567890"
        receipt = {
            "status": "FAILED",
            "summary": f"transport rejected Bearer {bearer}",
            "provider": {
                "api_key": api_key,
                "events": [f"token={token}", {"password": "nested-password"}],
            },
            "counts": (1, 2),
        }

        redacted = redact_sensitive(receipt)

        self.assertEqual(redacted["status"], "FAILED")
        self.assertEqual(redacted["provider"]["api_key"], REDACTION)
        self.assertEqual(redacted["provider"]["events"][1]["password"], REDACTION)
        self.assertEqual(redacted["counts"], (1, 2))
        serialized = json.dumps(redacted)
        for secret in (bearer, api_key, token, "nested-password"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(receipt["provider"]["api_key"], api_key)

    def test_structured_redaction_normalizes_realistic_sensitive_key_styles(self):
        sensitive_names = (
            "accessToken",
            "access_token",
            "access-token",
            "refreshToken",
            "refresh_token",
            "refresh-token",
            "idToken",
            "id_token",
            "id-token",
            "oauthToken",
            "oauth_token",
            "oauth-token",
            "authorization",
            "clientSecret",
            "client_secret",
            "client-secret",
            "privateKey",
            "private_key",
            "private-key",
            "secret",
            "password",
            "passphrase",
        )
        receipt = {
            name: f"structured-canary-{index}-abcdefghijklmnopqrstuvwxyz"
            for index, name in enumerate(sensitive_names)
        }

        redacted = redact_sensitive(receipt)

        for name, original in receipt.items():
            with self.subTest(name=name):
                self.assertEqual(redacted[name], REDACTION)
                self.assertNotIn(original, json.dumps(redacted))

    def test_structured_redaction_preserves_non_sensitive_whole_names(self):
        receipt = {
            "notpassword": "negative-notpassword-canary",
            "tokenCount": 3,
            "secretary": "assigned",
            "monkey": "matter-record-key",
            "authorizationStatus": "pending",
            "privateKeyboardLayout": "dvorak",
        }

        self.assertEqual(redact_sensitive(receipt), receipt)

    def test_text_redaction_handles_normalized_assignments_authorization_and_bearer(self):
        sensitive_assignments = (
            "accessToken={secret}",
            "refresh_token: {secret}",
            '"id-token": "{secret}"',
            "oauthToken={secret}",
            "authorization: {secret}",
            "authorization={secret}",
            "Authorization: Bearer {secret}",
            'authorization="Basic {secret}"',
            "clientSecret={secret}",
            "private-key: {secret}",
            "secret={secret}",
            "password: {secret}",
            "passphrase={secret}",
            "token={secret}",
            "bearer={secret}",
            "Bearer {secret}",
            "key={secret}",
            "apiKey={secret}",
        )

        for index, template in enumerate(sensitive_assignments):
            secret = f"text-canary-{index}-abcdefghijklmnopqrstuvwxyz"
            original = template.format(secret=secret)
            with self.subTest(original=original):
                redacted = redact_text(original)
                self.assertIn(REDACTION, redacted)
                self.assertNotIn(secret, redacted)

    def test_text_redaction_handles_nested_and_complete_logical_secret_values(self):
        cases = (
            (
                "message: accessToken=supersecret123456789",
                f"message: accessToken={REDACTION}",
            ),
            (
                "Cookie: sessionToken=supersecret123456789",
                f"Cookie: sessionToken={REDACTION}",
            ),
            (
                'authorization="abc def"',
                f'authorization="{REDACTION}"',
            ),
            (
                'Authorization: Digest username="alice", realm="forge", '
                'response="digest-secret-tail"',
                f"Authorization: Digest {REDACTION}",
            ),
            (
                r'{"password":"abc\\\"def-secret-tail"}',
                f'{{"password":"{REDACTION}"}}',
            ),
        )

        for original, expected in cases:
            with self.subTest(original=original):
                redacted = redact_text(original)
                self.assertEqual(redacted, expected)
                for canary in (
                    "supersecret123456789",
                    "abc def",
                    "username",
                    "alice",
                    "realm",
                    "response",
                    "digest-secret-tail",
                    "def-secret-tail",
                ):
                    self.assertNotIn(canary, redacted)

    def test_text_redaction_treats_authorization_aliases_as_complete_values(self):
        cases = (
            (
                'Proxy-Authorization: Digest username="Mufasa", '
                'response="proxysecret123456789"',
                f"Proxy-Authorization: Digest {REDACTION}",
            ),
            (
                'HTTP_AUTHORIZATION=Digest username="Mufasa", '
                'response="cgisecret123456789"',
                f"HTTP_AUTHORIZATION=Digest {REDACTION}",
            ),
        )

        for original, expected in cases:
            with self.subTest(original=original):
                redacted = redact_text(original)
                self.assertEqual(redacted, expected)
                for canary in (
                    "username",
                    "Mufasa",
                    "response",
                    "proxysecret123456789",
                    "cgisecret123456789",
                ):
                    self.assertNotIn(canary, redacted)

        self.assertEqual(
            redact_text("authorizationStatus=pending"),
            "authorizationStatus=pending",
        )

    def test_text_redaction_preserves_non_sensitive_outer_assignments(self):
        originals = (
            "notpassword=ordinary-value",
            "message: ordinary status text",
            "Cookie: theme=dark; session=ordinary",
        )

        for original in originals:
            with self.subTest(original=original):
                self.assertEqual(redact_text(original), original)

    def test_text_redaction_requires_a_complete_sensitive_assignment_name(self):
        non_sensitive_assignments = (
            "notpassword=negative-password-canary",
            "tokenCount=3",
            "secretary=assigned",
            "monkey=matter-record-key",
            "authorizationStatus=pending",
            "privateKeyboardLayout=dvorak",
        )

        for original in non_sensitive_assignments:
            with self.subTest(original=original):
                self.assertEqual(redact_text(original), original)

    def test_review_settings_match_the_frozen_fail_closed_shape(self):
        settings = build_claude_settings("review", self.worktree)

        self.assertEqual(
            settings,
            {
                "sandbox": {
                    "enabled": True,
                    "failIfUnavailable": True,
                    "autoAllowBashIfSandboxed": True,
                    "allowUnsandboxedCommands": False,
                    "excludedCommands": [],
                    "filesystem": {
                        "allowWrite": [],
                        "denyWrite": [],
                        "denyRead": [],
                    },
                    "network": {
                        "allowedDomains": [],
                        "allowUnixSockets": [],
                        "allowAllUnixSockets": False,
                        "allowLocalBinding": False,
                    },
                }
            },
        )

    def test_build_settings_allow_exactly_the_admitted_canonical_worktree(self):
        settings = build_claude_settings("build", self.worktree)

        self.assertEqual(
            settings["sandbox"]["filesystem"]["allowWrite"],
            [str(self.worktree)],
        )

    def test_recovery_settings_require_explicit_minimum_write_paths(self):
        recovery_artifact = self.worktree / ".forge" / "recovery"
        recovery_artifact.mkdir(parents=True)

        default_settings = build_claude_settings("recovery", self.worktree)
        explicit_settings = build_claude_settings(
            "recovery",
            self.worktree,
            recovery_write_paths=(recovery_artifact,),
        )

        self.assertEqual(
            default_settings["sandbox"]["filesystem"]["allowWrite"],
            [],
        )
        self.assertEqual(
            explicit_settings["sandbox"]["filesystem"]["allowWrite"],
            [str(recovery_artifact)],
        )

    def test_write_paths_cannot_escape_or_alias_the_admitted_worktree(self):
        outside = self.temp_path / "outside"
        outside.mkdir()
        alias = self.temp_path / "worktree-alias"
        alias.symlink_to(self.worktree, target_is_directory=True)

        with self.assertRaises(SecurityError):
            build_claude_settings("build", alias)
        with self.assertRaises(SecurityError):
            build_claude_settings(
                "recovery",
                self.worktree,
                recovery_write_paths=(outside,),
            )

    def test_network_domains_are_empty_by_default_and_strict_when_frozen(self):
        settings = build_claude_settings(
            "build",
            self.worktree,
            allowed_network_domains=("packages.example.com", "files.example.org"),
        )
        self.assertEqual(
            settings["sandbox"]["network"]["allowedDomains"],
            ["packages.example.com", "files.example.org"],
        )

        invalid_domains = (
            "https://example.com",
            "example.com/path",
            "example.com:443",
            "*.example.com",
            "EXAMPLE.com",
            "127.0.0.1",
            "localhost",
            "example.com.",
            "exa mple.com",
            "example..com",
        )
        for domain in invalid_domains:
            with self.subTest(domain=domain):
                with self.assertRaises(SecurityError):
                    build_claude_settings(
                        "build",
                        self.worktree,
                        allowed_network_domains=(domain,),
                    )

    def test_network_domains_reject_all_os_numeric_ipv4_alias_forms(self):
        numeric_aliases = (
            ("abbreviated loopback", "127.1"),
            ("three-part loopback", "127.0.1"),
            ("hex component loopback", "0x7f.0.0.1"),
            ("abbreviated link local", "169.254.1"),
            ("single integer loopback", "2130706433"),
            ("single hexadecimal loopback", "0x7f000001"),
            ("octal component loopback", "0177.0.0.1"),
            ("single octal loopback", "017700000001"),
        )

        for description, domain in numeric_aliases:
            with self.subTest(description=description, domain=domain):
                with self.assertRaises(SecurityError):
                    build_claude_settings(
                        "build",
                        self.worktree,
                        allowed_network_domains=(domain,),
                    )

    def test_private_settings_writer_tightens_all_private_modes_and_writes_exact_json(self):
        private_root = self.temp_path / "state"
        private_root.mkdir(mode=0o755)
        settings_path = private_root / "attempt-1" / "claude-settings.json"
        settings = build_claude_settings("review", self.worktree)

        written = write_private_settings(
            settings_path,
            settings,
            allowed_root=private_root,
        )

        self.assertEqual(written, settings_path)
        self.assertEqual(mode(private_root), 0o700)
        self.assertEqual(mode(settings_path.parent), 0o700)
        self.assertEqual(mode(settings_path), 0o600)
        self.assertEqual(json.loads(settings_path.read_text(encoding="utf-8")), settings)

    def test_private_settings_writer_rejects_unowned_root_before_mutation(self):
        private_root = self.temp_path / "foreign-state"
        private_root.mkdir(mode=0o755)
        settings_path = private_root / "attempt-1" / "claude-settings.json"
        settings = build_claude_settings("review", self.worktree)
        real_fstat = os.fstat

        def foreign_owner(file_descriptor):
            result = real_fstat(file_descriptor)
            return SimpleNamespace(
                st_dev=result.st_dev,
                st_ino=result.st_ino,
                st_mode=result.st_mode,
                st_uid=os.geteuid() + 1,
            )

        with mock.patch(
            "forge_lib.security.os.fstat",
            side_effect=foreign_owner,
        ):
            with self.assertRaises(SecurityError):
                write_private_settings(
                    settings_path,
                    settings,
                    allowed_root=private_root,
                )

        self.assertEqual(mode(private_root), 0o755)
        self.assertFalse(settings_path.parent.exists())

    def test_private_settings_writer_rejects_symlink_and_non_directory_roots(self):
        actual_root = self.temp_path / "actual-state"
        actual_root.mkdir(mode=0o755)
        root_alias = self.temp_path / "state-alias"
        root_alias.symlink_to(actual_root, target_is_directory=True)
        non_directory = self.temp_path / "state-file"
        non_directory.write_text("not a directory", encoding="utf-8")
        settings = build_claude_settings("review", self.worktree)

        hostile_roots = (
            (root_alias, root_alias / "attempt-1" / "claude-settings.json"),
            (non_directory, non_directory / "attempt-1" / "claude-settings.json"),
        )
        for root, settings_path in hostile_roots:
            with self.subTest(root=root):
                with self.assertRaises(SecurityError):
                    write_private_settings(
                        settings_path,
                        settings,
                        allowed_root=root,
                    )

        self.assertEqual(mode(actual_root), 0o755)
        self.assertFalse((actual_root / "attempt-1").exists())

    def test_private_settings_writer_fails_without_no_follow_directory_primitives(self):
        settings = build_claude_settings("review", self.worktree)

        for index, required_flag in enumerate(("O_NOFOLLOW", "O_DIRECTORY")):
            with self.subTest(required_flag=required_flag):
                private_root = self.temp_path / f"unsupported-state-{index}"
                private_root.mkdir(mode=0o755)
                settings_path = private_root / "attempt-1" / "claude-settings.json"
                with mock.patch(
                    f"forge_lib.security.os.{required_flag}",
                    0,
                ):
                    with self.assertRaises(SecurityError):
                        write_private_settings(
                            settings_path,
                            settings,
                            allowed_root=private_root,
                        )

                self.assertEqual(mode(private_root), 0o755)
                self.assertFalse(settings_path.parent.exists())

    def test_private_settings_writer_rejects_escape_symlink_and_replacement(self):
        private_root = self.temp_path / "state"
        private_root.mkdir(mode=0o700)
        outside = self.temp_path / "outside"
        outside.mkdir(mode=0o700)
        alias = private_root / "attempt-alias"
        alias.symlink_to(outside, target_is_directory=True)
        symlink_file = private_root / "settings-link.json"
        symlink_file.symlink_to(outside / "captured.json")
        settings = build_claude_settings("review", self.worktree)

        for hostile_path in (
            private_root / ".." / "escaped.json",
            alias / "claude-settings.json",
            symlink_file,
        ):
            with self.subTest(path=hostile_path):
                with self.assertRaises(SecurityError):
                    write_private_settings(
                        hostile_path,
                        settings,
                        allowed_root=private_root,
                    )

        settings_path = private_root / "attempt-1" / "claude-settings.json"
        write_private_settings(settings_path, settings, allowed_root=private_root)
        original = settings_path.read_bytes()
        with self.assertRaises(SecurityError):
            write_private_settings(
                settings_path,
                {"sandbox": {"enabled": False}},
                allowed_root=private_root,
            )
        self.assertEqual(settings_path.read_bytes(), original)

    def test_process_death_during_temp_write_never_publishes_partial_final(self):
        private_root = self.temp_path / "crash-before-state"
        private_root.mkdir(mode=0o700)
        settings_path = private_root / "attempt-1" / "claude-settings.json"
        settings = {
            "sandbox": {"enabled": True},
            "canary": "before-publish-" + ("x" * 32_768),
        }

        result = run_crashing_settings_writer(
            settings_path,
            private_root,
            settings,
            "during_write",
        )

        self.assertEqual(result.returncode, 73, result.stderr.decode())
        self.assertFalse(settings_path.exists())
        stale_pattern = f".{settings_path.name}.forge-tmp-*"
        self.assertEqual(len(list(settings_path.parent.glob(stale_pattern))), 1)

        write_private_settings(settings_path, settings, allowed_root=private_root)
        self.assertEqual(json.loads(settings_path.read_text(encoding="utf-8")), settings)
        self.assertEqual(list(settings_path.parent.glob(stale_pattern)), [])

    def test_process_death_after_publish_leaves_complete_create_once_final(self):
        private_root = self.temp_path / "crash-after-state"
        private_root.mkdir(mode=0o700)
        settings_path = private_root / "attempt-1" / "claude-settings.json"
        settings = {
            "sandbox": {"enabled": True},
            "canary": "after-publish-" + ("y" * 32_768),
        }

        result = run_crashing_settings_writer(
            settings_path,
            private_root,
            settings,
            "after_publish",
        )

        self.assertEqual(result.returncode, 74, result.stderr.decode())
        self.assertEqual(json.loads(settings_path.read_text(encoding="utf-8")), settings)
        self.assertEqual(mode(settings_path), 0o600)
        original = settings_path.read_bytes()
        stale_pattern = f".{settings_path.name}.forge-tmp-*"
        self.assertEqual(len(list(settings_path.parent.glob(stale_pattern))), 1)

        with self.assertRaises(SecurityError):
            write_private_settings(
                settings_path,
                {"sandbox": {"enabled": False}},
                allowed_root=private_root,
            )
        self.assertEqual(settings_path.read_bytes(), original)
        self.assertEqual(list(settings_path.parent.glob(stale_pattern)), [])

    def test_private_settings_publication_fsyncs_temp_before_atomic_link(self):
        private_root = self.temp_path / "publication-state"
        private_root.mkdir(mode=0o700)
        settings_path = private_root / "attempt-1" / "claude-settings.json"
        settings = build_claude_settings("review", self.worktree)
        events = []
        real_fsync = os.fsync
        real_link = os.link

        def recording_fsync(file_descriptor):
            file_type = "file" if stat.S_ISREG(os.fstat(file_descriptor).st_mode) else "dir"
            events.append(f"fsync-{file_type}")
            return real_fsync(file_descriptor)

        def recording_link(*args, **kwargs):
            self.assertFalse(settings_path.exists())
            events.append("link")
            return real_link(*args, **kwargs)

        with (
            mock.patch("forge_lib.security.os.fsync", side_effect=recording_fsync),
            mock.patch("forge_lib.security.os.link", side_effect=recording_link),
        ):
            write_private_settings(settings_path, settings, allowed_root=private_root)

        self.assertEqual(
            events,
            ["fsync-file", "link", "fsync-dir", "fsync-dir"],
        )

    def test_private_settings_writer_rejects_unsafe_stale_temp_entries(self):
        private_root = self.temp_path / "hostile-temp-state"
        private_root.mkdir(mode=0o700)
        attempt = private_root / "attempt-1"
        attempt.mkdir(mode=0o700)
        settings_path = attempt / "claude-settings.json"
        outside = self.temp_path / "outside-temp-target"
        outside.write_text("do not touch", encoding="utf-8")
        hostile_temp = attempt / f".{settings_path.name}.forge-tmp-hostile"
        hostile_temp.symlink_to(outside)
        settings = build_claude_settings("review", self.worktree)

        with self.assertRaises(SecurityError):
            write_private_settings(
                settings_path,
                settings,
                allowed_root=private_root,
            )

        self.assertFalse(settings_path.exists())
        self.assertTrue(hostile_temp.is_symlink())
        self.assertEqual(outside.read_text(encoding="utf-8"), "do not touch")


if __name__ == "__main__":
    unittest.main()
