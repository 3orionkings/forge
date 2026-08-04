#!/bin/zsh

set -u

ROOT="${0:A:h}"
SKILLS="${ROOT:h}"
typeset -i cases=0 failures=0

check() {
  local name="$1"
  shift
  (( cases += 1 ))
  if "$@"; then
    print -r -- "PASS: ${name}"
  else
    print -r -- "FAIL: ${name}"
    (( failures += 1 ))
  fi
}

description_is_trigger_only() {
  local file="$1" desc
  desc="$(sed -n 's/^description: //p' "${file}" | head -n 1)"
  [[ "${desc}" == 'Use when '* ]] || return 1
  (( ${#desc} <= 500 )) || return 1
  [[ "${desc}" != *': '* ]] || return 1
  ! print -r -- "${desc}" | grep -Eiq \
    'full[- ]diff|proof rerun|gate 2|owner approval|waits for your approval|review each unit'
}

typeset -a entrypoints
entrypoints=(
  "${ROOT}/SKILL.md"
  "${SKILLS}/forge-build/SKILL.md"
  "${SKILLS}/forge-orchestrate/SKILL.md"
  "${SKILLS}/forge-review/SKILL.md"
  "${SKILLS}/forge-with-docs/SKILL.md"
  "${SKILLS}/forge-recovery/SKILL.md"
)

for file in "${entrypoints[@]}"; do
  check "trigger-only description: ${file:h:t}" description_is_trigger_only "${file}"
done

for file in "${SKILLS}"/forge-{build,orchestrate,review,with-docs}/SKILL.md; do
  check "entrypoint routes cadence instead of duplicating it: ${file:h:t}" \
    sh -c '! grep -Fq '\''| | `sprint` | `assured` |'\'' "$1"' sh "${file}"
done

check 'sprint has no unit review' grep -Fq '**Review** | **None.**' "${ROOT}/profiles/sprint.md"
check 'sprint file sets are binding' grep -Fq 'binding, not advisory' "${ROOT}/profiles/sprint.md"
check 'sprint has one end pass' grep -Fq 'Once, at sprint end' "${ROOT}/profiles/sprint.md"
check 'guard interface exposes proof scope' grep -Fq -- '--proof-scope targeted|broad' "${ROOT}/ENGINE.md"
check 'guard interface exposes expansion prior set' grep -Fq -- '--current-file PATH' "${ROOT}/ENGINE.md"
check 'guard interface exposes heavy-lease ownership' grep -Fq -- '--owns-heavy-lease true|false' "${ROOT}/ENGINE.md"
check 'Act 1-2 reference contains no partial build procedure' \
  sh -c '! grep -Fq '\''## ACT 3'\'' "$1"' sh "${ROOT}/references/acts-1-2.md"
check 'common invocation template branches commit check by profile' \
  grep -Fq 'Under `sprint`, the orchestrator commits after' "${ROOT}/references/invocation-templates.md"
check 'common liveness reference branches closeout by profile' \
  grep -Fq "that profile's commit check" "${ROOT}/references/liveness.md"
check 'admission reference recognizes targeted LIGHT proofs' \
  grep -Fq 'named file/path/pattern proof is targeted' "${ROOT}/references/admission.md"
check 'admission requires explicit oracle declaration in both profiles' \
  grep -Fq 'both profiles: explicit `--oracle-exact true|false`' "${ROOT}/references/admission.md"
check 'healthy runs do not load recovery playbook' \
  grep -Fq 'must not load this playbook pre-emptively' "${SKILLS}/forge-recovery/PLAYBOOK.md"

if (( failures == 0 )); then
  print -r -- "PASS: ${cases} context-policy cases"
  exit 0
fi

print -r -- "FAIL: ${failures} of ${cases} context-policy cases"
exit 1
