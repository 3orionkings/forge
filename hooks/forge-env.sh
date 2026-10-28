#!/bin/sh
# Lets Forge's skills and agents find their scripts when Forge is installed as a
# plugin: exports FORGE_SKILLS (this plugin's skills directory) to later Bash commands.
# A manual install in ~/.claude/skills needs no hook; the skills default to that path.
[ -n "${CLAUDE_ENV_FILE:-}" ] || exit 0
[ -n "${CLAUDE_PLUGIN_ROOT:-}" ] || exit 0
printf 'export FORGE_SKILLS="${FORGE_SKILLS:-%s}"\n' "${CLAUDE_PLUGIN_ROOT}/skills" >> "${CLAUDE_ENV_FILE}"
