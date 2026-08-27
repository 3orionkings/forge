import json
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MARKETPLACE_PATH = Path.home() / ".agents" / "plugins" / "marketplace.json"
MIT_GRANT_TEXT = """\
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE."""


class PluginContractTests(unittest.TestCase):
    def test_license_contains_full_mit_grant_text(self):
        license_text = (PLUGIN_ROOT / "LICENSE").read_text(encoding="utf-8")

        self.assertIn(MIT_GRANT_TEXT, license_text)

    def test_license_preserves_upstream_copyright_attribution(self):
        license_text = (PLUGIN_ROOT / "LICENSE").read_text(encoding="utf-8")

        self.assertIn("Copyright (c) 2026 Chase AI", license_text)
        self.assertIn("Copyright (c) 2026 Matt Pocock", license_text)

    def test_third_party_notice_captures_provenance_contract(self):
        notice_path = PLUGIN_ROOT / "THIRD-PARTY-NOTICES.md"
        self.assertTrue(notice_path.is_file(), notice_path)
        notice = notice_path.read_text(encoding="utf-8")
        normalized_notice = " ".join(notice.split())

        self.assertIn(
            "canonical and comprehensive third-party notice for Forge",
            normalized_notice,
        )
        self.assertIn("Chase AI", normalized_notice)
        self.assertIn("Matt Pocock", normalized_notice)
        self.assertIn("local Forge adaptation", normalized_notice)
        self.assertIn("Codex-first role reversal", normalized_notice)
        self.assertIn("stdio bridge", normalized_notice)
        self.assertIn(
            "OpenAI's Apache-2.0 `codex-plugin-cc` was used solely as an architectural reference",
            normalized_notice,
        )
        self.assertIn(
            "no source code from `codex-plugin-cc` was copied into this Forge adaptation",
            normalized_notice,
        )
        self.assertIn(
            "No endorsement by OpenAI, Anthropic, Matt Pocock, Chase AI, "
            "Peter Steinberger, or their affiliates is implied.",
            normalized_notice,
        )

    def test_extensionless_notice_is_only_a_compatibility_pointer(self):
        compatibility_notice = (PLUGIN_ROOT / "THIRD-PARTY-NOTICES").read_text(
            encoding="utf-8"
        )

        self.assertEqual(
            " ".join(compatibility_notice.split()),
            "Forge third-party notice compatibility pointer "
            "=============================================== "
            "The canonical and comprehensive third-party notice for Forge is "
            "`THIRD-PARTY-NOTICES.md`. This extensionless file is retained only "
            "for compatibility with existing dispatch-guard and packaging "
            "references. Update `THIRD-PARTY-NOTICES.md`; do not duplicate "
            "provenance here.",
        )

    def test_peter_steinberger_is_classified_as_conceptual_inspiration(self):
        notice = (PLUGIN_ROOT / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
        mit_section = notice.partition("## MIT-licensed upstream works")[2].partition(
            "## "
        )[0]
        conceptual_section = notice.partition("## Conceptual inspiration")[2].partition(
            "## "
        )[0]

        self.assertNotIn("Peter Steinberger", mit_section)
        self.assertIn("Peter Steinberger", conceptual_section)
        self.assertIn("conceptual attribution only", conceptual_section)

    def test_dispatch_guard_attribution_references_canonical_notice(self):
        guard_test = (PLUGIN_ROOT / "tests" / "test_dispatch_guard.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "See ../THIRD-PARTY-NOTICES.md and ../LICENSE for the shipped source notice.",
            guard_test,
        )
        self.assertNotIn("See ../THIRD-PARTY-NOTICES and", guard_test)

    def test_plugin_manifest_matches_forge_contract(self):
        manifest_path = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
        self.assertTrue(manifest_path.is_file(), manifest_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["name"], "forge")
        self.assertEqual(manifest["version"], "0.1.0")
        self.assertEqual(
            manifest["description"],
            "Coordinate role-reversed Codex and Claude planning, review, execution, and orchestration; recovery requests fail closed.",
        )
        self.assertEqual(
            manifest["author"],
            {"name": "3orionkings", "url": "https://github.com/3orionkings"},
        )
        self.assertEqual(manifest["license"], "MIT")
        self.assertEqual(
            manifest["keywords"],
            ["codex", "claude", "orchestration", "review", "execution"],
        )
        self.assertEqual(manifest["skills"], "./skills/")
        self.assertEqual(manifest["mcpServers"], "./.mcp.json")
        self.assertNotIn("hooks", manifest)
        self.assertNotIn("apps", manifest)
        self.assertFalse(
            set(manifest)
            - {
                "id",
                "name",
                "version",
                "description",
                "author",
                "homepage",
                "repository",
                "license",
                "keywords",
                "skills",
                "mcpServers",
                "apps",
                "interface",
            }
        )

        interface = manifest["interface"]
        self.assertEqual(interface["displayName"], "Forge")
        self.assertEqual(
            interface["shortDescription"],
            "Coordinate reviewed Codex and Claude builds",
        )
        self.assertIn("Codex remains", interface["longDescription"])
        self.assertIn("Claude performs bounded review/execution", interface["longDescription"])
        self.assertEqual(interface["developerName"], "3orionkings")
        self.assertEqual(interface["category"], "Productivity")
        self.assertEqual(interface["capabilities"], ["Interactive", "Read", "Write"])
        self.assertEqual(len(interface["defaultPrompt"]), 3)
        self.assertTrue(all(isinstance(prompt, str) and prompt for prompt in interface["defaultPrompt"]))
        self.assertLessEqual(
            set(interface),
            {
                "displayName",
                "shortDescription",
                "longDescription",
                "developerName",
                "category",
                "capabilities",
                "websiteURL",
                "privacyPolicyURL",
                "termsOfServiceURL",
                "brandColor",
                "composerIcon",
                "logo",
                "logoDark",
                "screenshots",
                "defaultPrompt",
                "default_prompt",
            },
        )
        self.assertFalse(
            {"composerIcon", "logo", "logoDark", "screenshots"} & set(interface)
        )

    def test_mcp_manifest_matches_stdio_bridge_contract(self):
        mcp_path = PLUGIN_ROOT / ".mcp.json"
        self.assertTrue(mcp_path.is_file(), mcp_path)
        mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
        server = mcp["mcpServers"]["forge"]
        self.assertEqual(server["cwd"], ".")
        self.assertEqual(server["command"], "python3")
        self.assertEqual(server["args"], ["-u", "./scripts/forge_bridge.py"])
        self.assertEqual(server["startup_timeout_sec"], 10)
        self.assertEqual(server["tool_timeout_sec"], 60)
        self.assertNotIn("url", server)
        self.assertNotIn("transport", server)

    def test_workspace_hook_is_auto_discovered_without_manifest_field(self):
        manifest = json.loads(
            (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertNotIn("hooks", manifest)

        hooks_path = PLUGIN_ROOT / "hooks.json"
        self.assertTrue(hooks_path.is_file(), hooks_path)
        self.assertFalse((PLUGIN_ROOT / ".codex-plugin" / "hooks.json").exists())
        hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
        entries = hooks["hooks"]["PreToolUse"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(
            entries[0]["matcher"],
            "^(?:mcp__forge__)?(?:forge_(?:preflight|start|status|wait|result|resume|steer|cancel|list))$",
        )
        self.assertEqual(
            entries[0]["hooks"],
            [
                {
                    "type": "command",
                    "command": "python3 ./scripts/forge_workspace_hook.py",
                    "timeout": 5,
                    "statusMessage": "Binding Forge workspace",
                }
            ],
        )

        script_path = PLUGIN_ROOT / "scripts" / "forge_workspace_hook.py"
        self.assertTrue(script_path.is_file(), script_path)

    def test_six_skill_directories_are_present_and_exact(self):
        skills_path = PLUGIN_ROOT / "skills"
        expected_skills = {
            "forge",
            "forge-review",
            "forge-build",
            "forge-orchestrate",
            "forge-with-docs",
            "forge-recovery",
        }
        skill_dirs = {path.name: path for path in skills_path.iterdir() if path.is_dir()}
        self.assertEqual(set(skill_dirs), expected_skills)
        for skill_name in expected_skills:
            self.assertTrue((skill_dirs[skill_name] / "SKILL.md").is_file(), skill_name)

        recovery = (skill_dirs["forge-recovery"] / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Automated recovery is not enabled", recovery)
        self.assertIn("existing work and state were left unchanged", recovery)
        self.assertIn("read-only `forge_status`", recovery)
        self.assertNotIn("Resume, transfer, or stop stalled work", recovery)

    def test_personal_marketplace_entry_matches_contract(self):
        self.assertTrue(MARKETPLACE_PATH.is_file(), MARKETPLACE_PATH)
        marketplace = json.loads(MARKETPLACE_PATH.read_text(encoding="utf-8"))
        entries = [plugin for plugin in marketplace["plugins"] if plugin["name"] == "forge"]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(set(entry), {"name", "source", "policy", "category"})
        self.assertEqual(entry["source"], {"source": "local", "path": "./plugins/forge"})
        self.assertEqual(
            entry["policy"],
            {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        )
        self.assertNotIn("products", entry["policy"])
        self.assertEqual(entry["category"], "Productivity")


if __name__ == "__main__":
    unittest.main()
