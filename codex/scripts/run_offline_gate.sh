#!/usr/bin/env bash

set -euo pipefail
# Keep fixture modes deterministic. Security-sensitive Forge paths set their
# own restrictive modes, while several boundary tests intentionally create
# ordinary 0755 roots and verify that rejected writes leave them untouched.
umask 022

resolve_script_path() {
  local source_path="${BASH_SOURCE[0]}"
  local source_dir

  while [[ -h "${source_path}" ]]; do
    source_dir="$(cd -P "$(dirname "${source_path}")" >/dev/null 2>&1 && pwd)" || return 1
    source_path="$(readlink "${source_path}")" || return 1
    [[ "${source_path}" = /* ]] || source_path="${source_dir}/${source_path}"
  done

  source_dir="$(cd -P "$(dirname "${source_path}")" >/dev/null 2>&1 && pwd)" || return 1
  printf '%s/%s\n' "${source_dir}" "$(basename "${source_path}")"
}

if ! GATE_PATH="$(resolve_script_path)"; then
  printf 'failed to resolve the physical offline-gate path\n' >&2
  exit 1
fi
readonly GATE_PATH
if ! SCRIPT_DIR="$(dirname "${GATE_PATH}")"; then
  printf 'failed to resolve the offline-gate script directory\n' >&2
  exit 1
fi
readonly SCRIPT_DIR
if ! REPO_ROOT="$(cd -P "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd)"; then
  printf 'failed to resolve the physical Forge repository root\n' >&2
  exit 1
fi
readonly REPO_ROOT
readonly CODEX_SYSTEM_SKILLS="${CODEX_HOME:-${HOME}/.codex}/skills/.system"
readonly QUICK_VALIDATE="${FORGE_QUICK_VALIDATE:-${CODEX_SYSTEM_SKILLS}/skill-creator/scripts/quick_validate.py}"
readonly PLUGIN_VALIDATE="${FORGE_PLUGIN_VALIDATE:-${CODEX_SYSTEM_SKILLS}/plugin-creator/scripts/validate_plugin.py}"

TEMP_ROOT=""
BRIDGE_PID=""
PROVIDER_SENTINEL=""
PROVIDER_STUB=""

cleanup() {
  local status=$?
  trap - EXIT

  if [[ -n "${BRIDGE_PID}" ]] && kill -0 "${BRIDGE_PID}" 2>/dev/null; then
    kill "${BRIDGE_PID}" 2>/dev/null || true
    wait "${BRIDGE_PID}" 2>/dev/null || true
  fi

  if [[ -n "${PROVIDER_SENTINEL}" ]] && [[ -e "${PROVIDER_SENTINEL}" ]]; then
    printf 'FAIL: the offline gate blocked an attempted real-provider invocation\n' >&2
    [[ ${status} -ne 0 ]] || status=97
  fi

  if [[ -n "${TEMP_ROOT}" ]] && [[ -d "${TEMP_ROOT}" ]] && [[ "${TEMP_ROOT}" != / ]]; then
    rm -rf -- "${TEMP_ROOT}"
  fi

  exit "${status}"
}

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

stage() {
  local title="$1"
  shift
  printf '\n==> %s\n' "${title}"
  "$@"
  printf '<== PASS: %s\n' "${title}"
}

require_command() {
  local command_name="$1"
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    printf 'required command is unavailable: %s\n' "${command_name}" >&2
    return 127
  fi
}

prepare_offline_environment() {
  local deny_bin="${TEMP_ROOT}/deny-bin"

  mkdir -p \
    "${deny_bin}" \
    "${TEMP_ROOT}/xdg-cache" \
    "${TEMP_ROOT}/xdg-config"
  PROVIDER_SENTINEL="${TEMP_ROOT}/provider-invoked"
  PROVIDER_STUB="${deny_bin}/claude"

  {
    printf '%s\n' '#!/bin/sh'
    printf '%s\n' 'printf "Forge offline gate: provider execution is forbidden\\n" >&2'
    printf '%s\n' ': > "${FORGE_PROVIDER_CALL_SENTINEL:?}"'
    printf '%s\n' 'exit 97'
  } > "${PROVIDER_STUB}"
  chmod 0700 "${PROVIDER_STUB}"

  export PATH="${deny_bin}:${PATH}"
  unset FORGE_CLAUDE_EXECUTABLE
  unset FORGE_STATE_PATH FORGE_LAUNCH_WORKSPACE
  export FORGE_PROVIDER_CALL_SENTINEL="${PROVIDER_SENTINEL}"
  export UV_OFFLINE=1
  export PIP_NO_INDEX=1
  export PIP_DISABLE_PIP_VERSION_CHECK=1
  export GIT_TERMINAL_PROMPT=0

  unset ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN CLAUDE_API_KEY
  unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
  unset GOOGLE_API_KEY OPENAI_API_KEY
}

verify_prerequisites() {
  local dependency
  for dependency in bash git lsof mkfifo mktemp pgrep python3 rg sed uv; do
    require_command "${dependency}"
  done
  [[ -f "${QUICK_VALIDATE}" ]] || {
    printf 'skill validator is unavailable: %s\n' "${QUICK_VALIDATE}" >&2
    return 1
  }
  [[ -f "${PLUGIN_VALIDATE}" ]] || {
    printf 'plugin validator is unavailable: %s\n' "${PLUGIN_VALIDATE}" >&2
    return 1
  }
}

verify_stdio_manifest() {
  python3 - "${REPO_ROOT}" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
manifest = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))
if set(manifest) != {"mcpServers"}:
    raise SystemExit(".mcp.json must contain only mcpServers")
servers = manifest["mcpServers"]
if set(servers) != {"forge"}:
    raise SystemExit(".mcp.json must define exactly the Forge server")
forge = servers["forge"]
if not isinstance(forge, dict):
    raise SystemExit("Forge MCP configuration must be an object")
if any(key in forge for key in ("url", "transport", "host", "port")):
    raise SystemExit("Forge MCP configuration must not declare a network transport")
if forge.get("cwd") != "." or forge.get("command") != "python3":
    raise SystemExit("Forge MCP configuration must launch from the plugin root with python3")
if forge.get("args") != ["-u", "./scripts/forge_bridge.py"]:
    raise SystemExit("Forge MCP configuration must launch the stdio bridge entrypoint")
if not (root / "scripts" / "forge_bridge.py").is_file():
    raise SystemExit("Forge stdio bridge entrypoint is missing")
print("Forge manifest selects the local stdio bridge")
PY
}

collect_process_tree() {
  local root_pid="$1"
  local queue="${root_pid}"
  local seen=""
  local current
  local children
  local child
  local pgrep_status
  local pgrep_error="${TEMP_ROOT}/pgrep.err"

  while [[ -n "${queue}" ]]; do
    current="${queue%% *}"
    if [[ "${queue}" = *" "* ]]; then
      queue="${queue#* }"
    else
      queue=""
    fi
    case " ${seen} " in
      *" ${current} "*) continue ;;
    esac
    seen="${seen:+${seen} }${current}"

    if children="$(pgrep -P "${current}" 2>"${pgrep_error}")"; then
      pgrep_status=0
    else
      pgrep_status=$?
    fi
    if [[ ${pgrep_status} -gt 1 ]] || [[ -s "${pgrep_error}" ]]; then
      printf 'cannot establish the Forge process tree for PID %s\n' "${current}" >&2
      [[ ! -s "${pgrep_error}" ]] || sed 's/^/  /' "${pgrep_error}" >&2
      return 1
    fi
    for child in ${children}; do
      queue="${queue:+${queue} }${child}"
    done
  done

  printf '%s\n' ${seen}
}

assert_pid_has_no_tcp_listener() {
  local pid="$1"
  local inventory="${TEMP_ROOT}/lsof-${pid}.inventory"
  local inventory_error="${TEMP_ROOT}/lsof-${pid}.inventory.err"
  local listeners="${TEMP_ROOT}/lsof-${pid}.listeners"
  local listener_error="${TEMP_ROOT}/lsof-${pid}.listeners.err"
  local listener_status

  if ! kill -0 "${pid}" 2>/dev/null; then
    printf 'cannot inspect vanished Forge process PID %s\n' "${pid}" >&2
    return 1
  fi
  if ! lsof -nP -a -p "${pid}" >"${inventory}" 2>"${inventory_error}"; then
    printf 'lsof could not establish visibility into Forge process PID %s\n' "${pid}" >&2
    [[ ! -s "${inventory_error}" ]] || sed 's/^/  /' "${inventory_error}" >&2
    return 1
  fi

  if lsof -nP -a -p "${pid}" -iTCP -sTCP:LISTEN >"${listeners}" 2>"${listener_error}"; then
    printf 'Forge process PID %s owns a forbidden TCP listener:\n' "${pid}" >&2
    sed 's/^/  /' "${listeners}" >&2
    return 1
  else
    listener_status=$?
  fi
  if [[ ${listener_status} -ne 1 ]] || [[ -s "${listeners}" ]] || [[ -s "${listener_error}" ]]; then
    printf 'lsof could not conclusively establish no TCP listener for Forge PID %s\n' "${pid}" >&2
    [[ ! -s "${listeners}" ]] || sed 's/^/  /' "${listeners}" >&2
    [[ ! -s "${listener_error}" ]] || sed 's/^/  /' "${listener_error}" >&2
    return 1
  fi
}

verify_stdio_runtime_and_no_listener() {
  local bridge_in="${TEMP_ROOT}/bridge.stdin"
  local bridge_out="${TEMP_ROOT}/bridge.stdout"
  local bridge_err="${TEMP_ROOT}/bridge.stderr"
  local bridge_state="${TEMP_ROOT}/forge-state.sqlite3"
  local process_ids
  local pid
  local attempt
  local bridge_status

  mkfifo "${bridge_in}"
  XDG_CACHE_HOME="${TEMP_ROOT}/xdg-cache" \
  XDG_CONFIG_HOME="${TEMP_ROOT}/xdg-config" \
  FORGE_STATE_PATH="${bridge_state}" \
  FORGE_LAUNCH_WORKSPACE="${REPO_ROOT}" \
    python3 -u "${REPO_ROOT}/scripts/forge_bridge.py" \
      <"${bridge_in}" >"${bridge_out}" 2>"${bridge_err}" &
  BRIDGE_PID=$!
  exec 9>"${bridge_in}"
  printf '%s\n' '{"jsonrpc":"2.0","id":"offline-gate","method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{}}}' >&9

  attempt=0
  while [[ ! -s "${bridge_out}" ]] && [[ ${attempt} -lt 100 ]]; do
    if ! kill -0 "${BRIDGE_PID}" 2>/dev/null; then
      printf 'Forge bridge exited before completing the stdio probe\n' >&2
      [[ ! -s "${bridge_err}" ]] || sed 's/^/  /' "${bridge_err}" >&2
      return 1
    fi
    sleep 0.05
    attempt=$((attempt + 1))
  done
  if [[ ! -s "${bridge_out}" ]]; then
    printf 'Forge bridge did not answer the stdio initialization probe\n' >&2
    return 1
  fi

  python3 - "${bridge_out}" <<'PY'
import json
import sys
from pathlib import Path

response = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()[0])
if response.get("jsonrpc") != "2.0" or response.get("id") != "offline-gate":
    raise SystemExit("Forge stdio initialization returned a mismatched response")
result = response.get("result")
if not isinstance(result, dict) or result.get("serverInfo", {}).get("name") != "forge":
    raise SystemExit("Forge stdio initialization did not identify the Forge server")
print("Forge answered JSON-RPC initialization over stdin/stdout")
PY

  process_ids="$(collect_process_tree "${BRIDGE_PID}")"
  [[ -n "${process_ids}" ]] || {
    printf 'Forge process tree inspection returned no PIDs\n' >&2
    return 1
  }
  for pid in ${process_ids}; do
    assert_pid_has_no_tcp_listener "${pid}"
  done

  sleep 0.10
  process_ids="$(collect_process_tree "${BRIDGE_PID}")"
  for pid in ${process_ids}; do
    assert_pid_has_no_tcp_listener "${pid}"
  done
  printf 'Forge stdio process tree owns no TCP listener (PIDs: %s)\n' "${process_ids//$'\n'/ }"

  exec 9>&-
  if wait "${BRIDGE_PID}"; then
    bridge_status=0
  else
    bridge_status=$?
  fi
  BRIDGE_PID=""
  if [[ ${bridge_status} -ne 0 ]]; then
    printf 'Forge bridge failed after clean stdio EOF (exit %s)\n' "${bridge_status}" >&2
    [[ ! -s "${bridge_err}" ]] || sed 's/^/  /' "${bridge_err}" >&2
    return "${bridge_status}"
  fi
}

run_skill_validators() {
  local skill
  for skill in forge forge-review forge-build forge-orchestrate forge-with-docs forge-recovery; do
    printf '  validating skill: %s\n' "${skill}"
    uv run --offline --no-project --with PyYAML python \
      "${QUICK_VALIDATE}" "${REPO_ROOT}/skills/${skill}"
  done
}

run_plugin_validator() {
  uv run --offline --no-project --with PyYAML python \
    "${PLUGIN_VALIDATE}" "${REPO_ROOT}"
}

scan_forbidden_patterns() {
  local scan_pattern
  local scan_output="${TEMP_ROOT}/forbidden-patterns.jsonl"
  local scan_status

  scan_pattern="$(printf '%s' 'TO''DO|TB''D|Claude-side driver|Claude commits|dangerously-skip|--fallback-model')"
  if rg --hidden --json -n "${scan_pattern}" . \
    -g '!.git/**' \
    -g '!docs/superpowers/plans/**' \
    -g '!docs/superpowers/specs/**' \
    -g '!**/__pycache__/**' \
    -g '!scripts/run_offline_gate.sh' >"${scan_output}"; then
    scan_status=0
  else
    scan_status=$?
  fi
  if [[ ${scan_status} -gt 1 ]]; then
    printf 'forbidden-pattern scan failed with rg exit %s\n' "${scan_status}" >&2
    return "${scan_status}"
  fi

  python3 - "${scan_output}" <<'PY'
import json
import sys
from pathlib import Path

failures = []
allowed = []
for raw in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    event = json.loads(raw)
    if event.get("type") != "match":
        continue
    data = event["data"]
    path = data["path"]["text"]
    if path.startswith("./"):
        path = path[2:]
    line_number = data["line_number"]
    line = data["lines"]["text"].rstrip("\n")
    matched = [part["match"]["text"] for part in data["submatches"]]
    is_explicit_negative_test = path.startswith("tests/") and (
        any(value.startswith("--") for value in matched) or "assert" in line
    )
    record = f"{path}:{line_number}:{line}"
    if is_explicit_negative_test:
        allowed.append(record)
    else:
        failures.append(record)

for record in allowed:
    print(f"ALLOWED forbidden-pattern test: {record}")
if failures:
    print("Forbidden production/control patterns found:", file=sys.stderr)
    for record in failures:
        print(f"  {record}", file=sys.stderr)
    raise SystemExit(1)
if not allowed:
    print("No forbidden patterns found")
PY
}

TEMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/forge-offline-gate.XXXXXX")"
cd "${REPO_ROOT}"
prepare_offline_environment

printf 'Forge full offline gate\n'
printf 'repository: %s\n' "${REPO_ROOT}"
printf 'provider execution: hard-denied by %s\n' "${PROVIDER_STUB}"
printf 'uv package resolution: offline\n'

stage 'prerequisites and local validator paths' verify_prerequisites
stage 'stdio manifest contract' verify_stdio_manifest
stage 'stdio runtime and PID-scoped no-listener proof' verify_stdio_runtime_and_no_listener
stage 'Python unittest discovery' python3 -m unittest discover -s tests -p 'test_*.py' -v
stage 'dispatch guard shell suite' bash tests/test_dispatch_guard.sh
stage 'recovery watchdog shell suite' bash tests/test_recovery_watchdog.sh
stage 'Forge liveness shell suite' bash tests/test_forge_liveness.sh
stage 'six skill quick validators' run_skill_validators
stage 'official plugin validator' run_plugin_validator
stage 'Git whitespace/error diff check' git diff --check
stage 'classified forbidden-pattern scan' scan_forbidden_patterns

[[ ! -e "${PROVIDER_SENTINEL}" ]] || exit 97
printf '\nPASS: Forge offline gate completed with no provider invocation.\n'
