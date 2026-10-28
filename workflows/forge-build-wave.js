export const meta = {
  name: 'forge-build-wave',
  description: 'Forge Acts 3-4: build one wave of independent units, each admitted by the dispatch guard, executed in its own worktree and verified at the active profile; the driver integrates afterwards',
  whenToUse: 'After Gate 1 (and Gate 2 when required), once per wave of independent units frozen on one base commit. A single unit is a wave of one.',
  phases: [
    { title: 'Prepare', detail: 'overlay, worktrees, frozen contracts and (assured) freeze manifests' },
    { title: 'Admit', detail: 'forge-admission runs the dispatch guard, one call at a time' },
    { title: 'Build', detail: 'forge-claude-executor or forge-codex-executor, at most three lanes and one HEAVY' },
    { title: 'Verify', detail: 'forge-unit-verifier, plus forge-codex-reviewer for Claude-built units under assured' },
  ],
}

// args: {
//   repo, slug, profile: 'sprint'|'assured', baseSha, plan, act2Log, verdict: 'APPROVED',
//   units: [{ id, goal, done_condition, files, proof, proof_scope, weight, lane_type?,
//             executor: 'claude'|'codex', backlog_ids?, route?, spec?, predecessor? }],
//   overlay?, executorPolicy?: 'any'|'codex-only'|'claude-only', worktreeRoot?,
//   snapshot?: { activeLanes, heavyLeases, writerWorktrees, files },   // lanes outside this wave
//   maxFixRounds?: 2, agentNamespace?: 'forge:'
// }
// A unit's predecessor (from an earlier wave) is { id, builder, report_receipt, review_receipt? },
// exactly as this workflow returned it in that unit's successor_receipts.
const A = args && typeof args === 'object' ? args : {}
const stop = message => { throw new Error(`forge-build-wave: ${message}`) }
const NS = typeof A.agentNamespace === 'string' ? A.agentNamespace : 'forge:'
const PROFILE = A.profile
if (PROFILE !== 'sprint' && PROFILE !== 'assured') stop('args.profile must be "sprint" or "assured"')
for (const key of ['repo', 'slug', 'baseSha', 'plan', 'act2Log']) if (!A[key]) stop(`args.${key} is required`)
if (!/^([0-9a-f]{40}|[0-9a-f]{64})$/i.test(A.baseSha)) stop('args.baseSha must be a full commit SHA from git rev-parse')
if (A.verdict !== 'APPROVED') stop('the governing plan verdict must be APPROVED')
const POLICY = A.executorPolicy || 'any'
if (!['any', 'codex-only', 'claude-only'].includes(POLICY)) stop('args.executorPolicy must be any, codex-only or claude-only')
const MAX_FIX = Number.isInteger(A.maxFixRounds) && A.maxFixRounds >= 0 ? A.maxFixRounds : 2
const units = Array.isArray(A.units) && A.units.length ? A.units : stop('args.units must be a non-empty list')
const snapshot = A.snapshot || {}
const LANE_CAP = 3
const HEAVY_CAP = 1

// The wave must be independent units with disjoint, exact file sets.
const PRODUCT = new Set(['feature', 'integration', 'recovery'])
const ids = new Set()
const owners = new Map()
for (const u of units) {
  if (!u.id || ids.has(u.id)) stop(`unit ids must be present and unique (${u.id})`)
  ids.add(u.id)
  u.lane_type = u.lane_type || 'feature'
  if (!['claude', 'codex'].includes(u.executor)) stop(`${u.id}: executor must be claude or codex`)
  if (!['LIGHT', 'HEAVY'].includes(u.weight)) stop(`${u.id}: weight must be LIGHT or HEAVY`)
  if (!['targeted', 'broad'].includes(u.proof_scope)) stop(`${u.id}: proof_scope must be targeted or broad`)
  if (!u.proof) stop(`${u.id}: a proof command is required`)
  if (u.oracle_exact) stop(`${u.id}: oracle-exact units need the capability probe; run them by hand`)
  if (PRODUCT.has(u.lane_type) && POLICY === 'codex-only' && u.executor !== 'codex') stop(`${u.id}: the overlay is codex-only`)
  if (PRODUCT.has(u.lane_type) && POLICY === 'claude-only' && u.executor !== 'claude') stop(`${u.id}: the overlay is claude-only`)
  if (!Array.isArray(u.files) || !u.files.length) stop(`${u.id}: an exact file set is required`)
  for (const f of u.files) {
    if (/[*?[\]]/.test(f) || f.startsWith('/') || f.split('/').includes('..')) stop(`${u.id}: ${f} is not an exact repo-relative path`)
    if (f === '.ai/HANDOFF.md') stop(`${u.id}: only the integration owner writes .ai/HANDOFF.md`)
    if (owners.has(f)) stop(`${u.id} and ${owners.get(f)} both declare ${f}; they belong in different waves`)
    owners.set(f, u.id)
  }
}
for (const u of units) {
  for (const d of u.depends_on || []) if (ids.has(d)) stop(`${u.id} depends on ${d} in the same wave; split the wave`)
}
if ((snapshot.activeLanes || 0) >= LANE_CAP) stop('three lanes are already active outside this wave')

const builderId = u => u.builder || (u.executor === 'codex' ? 'sol-5.6' : 'fable-5')
const branchPrefix = u => u.branch_prefix || (u.executor === 'codex' ? 'sol' : 'fable')
const unitSlug = u => `${A.slug}-${u.id}`
const instruction = u =>
  `Implement unit ${u.id} exactly as its frozen contract specifies, change only the declared files, run the proof and write the report. The executor never commits or merges.`

const RECEIPT_TEXT = { type: 'string' }
const SETUP = {
  type: 'object',
  properties: {
    ok: { type: 'boolean' },
    problem: RECEIPT_TEXT,
    forge_skills: RECEIPT_TEXT,
    overlay: RECEIPT_TEXT,
    plan: RECEIPT_TEXT,
    act2_log: RECEIPT_TEXT,
    worktree_root: RECEIPT_TEXT,
  },
  required: ['ok', 'forge_skills', 'overlay', 'plan', 'act2_log', 'worktree_root'],
}
const PREP = {
  type: 'object',
  properties: {
    ok: { type: 'boolean' },
    problem: RECEIPT_TEXT,
    worktree: RECEIPT_TEXT,
    branch: RECEIPT_TEXT,
    contract: RECEIPT_TEXT,
    contract_sha256: RECEIPT_TEXT,
    work_log: RECEIPT_TEXT,
    report: RECEIPT_TEXT,
    manifest: RECEIPT_TEXT,
    manifest_sha256: RECEIPT_TEXT,
    rephrased: { type: 'array', items: RECEIPT_TEXT },
  },
  required: ['ok', 'worktree', 'contract', 'work_log', 'report'],
}
const ADMISSION = {
  type: 'object',
  properties: {
    result: { type: 'string', enum: ['ADMIT', 'REJECT'] },
    exit: { type: 'integer' },
    guard_output: RECEIPT_TEXT,
    declaration_mismatches: RECEIPT_TEXT,
  },
  required: ['result', 'guard_output'],
}
const EXECUTION = {
  type: 'object',
  properties: {
    status: { type: 'string', enum: ['DONE', 'FAILED', 'BLOCKED', 'EXPANSION_REQUEST', 'STALLED'] },
    executor: RECEIPT_TEXT,
    thread_id: RECEIPT_TEXT,
    changed_files: { type: 'array', items: RECEIPT_TEXT },
    undeclared_changes: { type: 'array', items: RECEIPT_TEXT },
    proof_exit: RECEIPT_TEXT,
    work_log: RECEIPT_TEXT,
    report: RECEIPT_TEXT,
    expansion_files: { type: 'array', items: RECEIPT_TEXT },
    expansion_reason: RECEIPT_TEXT,
    deviations: { type: 'array', items: RECEIPT_TEXT },
  },
  required: ['status', 'changed_files', 'proof_exit'],
}
const VERIFICATION = {
  type: 'object',
  properties: {
    result: { type: 'string', enum: ['PASS', 'FAIL'] },
    executor_proof_exit: RECEIPT_TEXT,
    proof_rerun: RECEIPT_TEXT,
    diff_review: RECEIPT_TEXT,
    fix_list: { type: 'array', items: RECEIPT_TEXT },
  },
  required: ['result', 'fix_list'],
}
const REVIEW = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['APPROVED', 'REVISE', 'FAILED'] },
    thread_id: RECEIPT_TEXT,
    reviewer: RECEIPT_TEXT,
    verdict_file: RECEIPT_TEXT,
    findings: { type: 'array', items: RECEIPT_TEXT },
    failure: RECEIPT_TEXT,
  },
  required: ['verdict', 'reviewer', 'findings'],
}

// ---------------------------------------------------------------------------
// Prepare: the overlay, then one worktree and frozen contract per unit, in order.
// `git worktree add` calls on one repository are kept serial.
phase('Prepare')
const setup = await agent(
  [
    `Prepare a Forge build wave in ${A.repo}. Create nothing inside the repository except what step 4 says.`,
    '1. Resolve the forge skills directory with the snippet in the forge-admission agent instructions, or: the first of $FORGE_SKILLS, ~/.claude/skills, or the installed plugin\'s skills directory that contains forge/dispatch-guard.sh. Report its physical path (realpath).',
    `2. Confirm ${A.baseSha} resolves to a commit (git -C ${A.repo} rev-parse --verify ${A.baseSha}^{commit}).`,
    `3. Report the physical absolute paths of the plan (${A.plan}) and the Act-2 review log (${A.act2Log}); both must exist and be non-empty.`,
    A.overlay
      ? `4. The project overlay is ${A.overlay}. Confirm it contains exactly one line executor_policy=${POLICY} and report its physical path.`
      : `4. No owner overlay was supplied, so the global default applies. Write /tmp/forge-overlay-${A.slug}.md containing exactly two lines: "# No owner project overlay; the global Forge default applies." and "executor_policy=${POLICY}". Report its path.`,
    `5. The worktree root is ${A.worktreeRoot || `a sibling directory of the repository named "<repo name>-forge-worktrees"`}. Create it if missing and report its physical path.`,
    'Set ok false and explain in problem if any step fails.',
  ].join('\n'),
  { label: 'prepare wave', phase: 'Prepare', schema: SETUP },
)
if (!setup || !setup.ok) return { status: 'BLOCKED', stage: 'prepare', problem: setup ? setup.problem : 'setup agent did not return' }

const FREEZE_LIST = skills => [
  `${skills}/forge/SKILL.md`, `${skills}/forge/ENGINE.md`, `${skills}/forge/ROUTING-POLICY.md`,
  `${skills}/forge/dispatch-guard.sh`, `${skills}/forge/dispatch-guard.test.sh`, `${skills}/forge/sol-liveness.sh`,
  `${skills}/forge-recovery/recovery-watchdog.sh`, `${skills}/forge-recovery/recovery-watchdog.test.sh`,
]

function contractPrompt(u, contractPath, fixList) {
  const manifestPath = `/tmp/forge-freeze-${unitSlug(u)}${fixList ? `-fix${fixList.round}` : ''}.sha256`
  return [
    fixList
      ? `Write the fix-round contract for Forge unit ${u.id} (round ${fixList.round}) to ${contractPath}. Keep every field of the original contract and add a FIX LIST section:\n${fixList.items.map(i => `- ${i}`).join('\n')}`
      : `Freeze Forge unit ${u.id} in ${A.repo}.`,
    fixList ? '' : [
      `1. Create the worktree: if AGENTS.md or .ai/COLLAB_PROTOCOL.md in the repository names a branch/worktree convention, follow it; otherwise run git -C ${A.repo} worktree add -b ${branchPrefix(u)}/${unitSlug(u)} ${setup.worktree_root}/${unitSlug(u)} ${A.baseSha}.`,
      `2. Confirm git -C <worktree> rev-parse HEAD equals ${A.baseSha} exactly and the tree is clean. Report the worktree's physical path and the branch.`,
      `3. Create the empty work log /tmp/forge-work-${unitSlug(u)}.log. The report path is ${u.executor === 'codex' ? `/tmp/forge-build-${unitSlug(u)}.txt` : `/tmp/forge-report-${unitSlug(u)}.md`}; do not create it.`,
      `4. Write the frozen contract to ${contractPath} with these sections: GOAL, SPEC (implement exactly; report deviations, not redesigns), IMMUTABLE BASE SHA (${A.baseSha}), BACKLOG ITEM IDs, EXACT DECLARED FILE SET, CONSTRAINTS, NON-GOALS, PROOF (the command and its scope), WORK-LOG PATH, REPORT PATH, OUTPUT (files-changed report, proof output, deviations), and the sentence "The executor never commits or merges." Tell the executor to tee its proof and build output to the work-log path.`,
      `Unit:\n${JSON.stringify(u, null, 2)}`,
    ].join('\n'),
    'The dispatch guard scans the contract. Apart from that one sentence, never use the words commit, merge, push, rebase, cherry-pick or checkpoint (in any form), and never name git reset, clean, restore, checkout, switch, stash, tag, branch -d or pull. If the unit itself needs such a word (for example "merge two config files"), rephrase it ("combine") and list each rephrasing.',
    PROFILE === 'assured'
      ? `5. Freeze: write ${manifestPath} with shasum -a 256 over these physical absolute paths, one line each and never the manifest itself: ${[setup.plan, setup.act2_log, contractPath, setup.overlay, ...FREEZE_LIST(setup.forge_skills)].join(', ')}. Report the manifest path and its own SHA-256.`
      : 'Sprint: no freeze manifest.',
    'Report the contract path and its SHA-256. Set ok false and explain in problem if any step fails.',
  ].filter(Boolean).join('\n')
}

const prepared = new Map()
for (const u of units) {
  const prep = await agent(contractPrompt(u, `/tmp/forge-contract-${unitSlug(u)}.md`, null), {
    label: `freeze ${u.id}`,
    phase: 'Prepare',
    schema: PREP,
  })
  prepared.set(u.id, prep && prep.ok ? prep : { ok: false, problem: prep ? prep.problem : 'prepare agent did not return' })
}

// ---------------------------------------------------------------------------
// Lanes: reservations keep at most three lanes and one HEAVY lease, counting lanes
// already active outside this wave. Admission is serialized so every guard call
// sees one current snapshot, and a REJECT stops the line for every unit not yet
// launched. Running lanes are never killed.
const lanes = { slots: snapshot.activeLanes || 0, heavy: snapshot.heavyLeases || 0, waiting: [], stopped: null }
const active = {
  running: snapshot.activeLanes || 0,
  heavyRunning: snapshot.heavyLeases || 0,
  writers: new Map((snapshot.writerWorktrees || []).map((w, i) => [`outside-${i}`, w])),
  files: new Map((snapshot.files || []).map(f => [f, 'outside'])),
}
const fits = u => lanes.slots < LANE_CAP && (u.weight !== 'HEAVY' || lanes.heavy < HEAVY_CAP)
const take = u => { lanes.slots++; if (u.weight === 'HEAVY') lanes.heavy++ }
function pump() {
  for (let i = 0; i < lanes.waiting.length; ) {
    const w = lanes.waiting[i]
    if (lanes.stopped) { lanes.waiting.splice(i, 1); w.resolve(false); continue }
    if (fits(w.u)) { lanes.waiting.splice(i, 1); take(w.u); w.resolve(true); continue }
    i++
  }
}
function acquire(u) {
  if (lanes.stopped) return Promise.resolve(false)
  if (u.weight === 'HEAVY' && (snapshot.heavyLeases || 0) >= HEAVY_CAP) return Promise.resolve(false)
  if (fits(u) && lanes.waiting.length === 0) { take(u); return Promise.resolve(true) }
  return new Promise(resolve => lanes.waiting.push({ u, resolve }))
}
function release(u) {
  lanes.slots--
  if (u.weight === 'HEAVY') lanes.heavy--
  pump()
}
function stopTheLine(reason) {
  if (!lanes.stopped) {
    lanes.stopped = reason
    log(`Stop-the-line: ${reason}`)
  }
  pump()
}

let admissionQueue = Promise.resolve()
function serially(task) {
  const run = admissionQueue.then(task)
  admissionQueue = run.catch(() => {})
  return run
}

function declaration(u, prep, action, files, priorFiles) {
  const others = [...active.files].filter(([, owner]) => owner !== u.id).map(([f]) => f)
  const writers = [...active.writers].filter(([owner]) => action === 'expand' || owner !== u.id).map(([, w]) => w)
  return {
    action,
    verdict: 'APPROVED',
    active_lanes: active.running,
    heavy_leases: active.heavyRunning,
    weight: u.weight,
    proof_scope: u.proof_scope,
    lane_type: u.lane_type,
    mode: 'normal',
    worktree: prep.worktree,
    clean_tree: action === 'launch',
    builder: builderId(u),
    executor_policy: POLICY,
    execution_profile: PROFILE,
    base_sha: A.baseSha,
    plan_file: setup.plan,
    act2_review_log: setup.act2_log,
    execution_contract: prep.contract,
    project_overlay: setup.overlay,
    freeze_manifest: PROFILE === 'assured' ? prep.manifest : null,
    freeze_manifest_sha256: PROFILE === 'assured' ? prep.manifest_sha256 : null,
    oracle_exact: false,
    backlog_ids: u.backlog_ids && u.backlog_ids.length ? u.backlog_ids : [u.id],
    files,
    current_files: action === 'expand' ? priorFiles : null,
    owns_heavy_lease: action === 'expand' ? u.weight === 'HEAVY' : null,
    proof_command: u.proof,
    work_log: prep.work_log,
    report: prep.report,
    predecessor: u.predecessor ? u.predecessor.id : 'none',
    predecessor_builder: u.predecessor ? u.predecessor.builder : null,
    report_receipt: u.predecessor ? u.predecessor.report_receipt : null,
    review_receipt: u.predecessor ? u.predecessor.review_receipt || null : null,
    instruction: instruction(u),
    active_writer_worktrees: writers,
    parallel_files: others,
  }
}

const admit = (u, prep, action, files, priorFiles) => serially(async () => {
  // A lane that was waiting for its turn when another lane was refused never reaches
  // the guard; that is "not launched", not a guard REJECT.
  if (action === 'launch' && lanes.stopped) return { result: 'STOPPED', guard_output: null }
  const decl = declaration(u, prep, action, files, priorFiles)
  const receipt = await agent(
    `Ask the dispatch guard about this lane and return the result fields.\nDeclaration (null means omit the flag):\n${JSON.stringify(decl, null, 2)}`,
    { label: `${action} ${u.id}`, phase: 'Admit', agentType: `${NS}forge-admission`, schema: ADMISSION },
  )
  if (!receipt || receipt.result !== 'ADMIT') {
    const line = receipt ? receipt.guard_output : 'admission agent did not return'
    if (action === 'launch') stopTheLine(`${u.id}: ${line}`)
    return receipt || { result: 'REJECT', guard_output: line }
  }
  if (action === 'launch') {
    active.running++
    if (u.weight === 'HEAVY') active.heavyRunning++
    active.writers.set(u.id, prep.worktree)
  }
  for (const [f, owner] of [...active.files]) if (owner === u.id) active.files.delete(f)
  for (const f of files) active.files.set(f, u.id)
  return receipt
})

function leave(u) {
  if (active.writers.has(u.id)) {
    active.running--
    if (u.weight === 'HEAVY') active.heavyRunning--
    active.writers.delete(u.id)
  }
  for (const [f, owner] of [...active.files]) if (owner === u.id) active.files.delete(f)
}

function execute(u, prep, admitLine, files, fix) {
  const common = [
    `unit: ${u.id}`,
    `contract: ${fix ? fix.contract : prep.contract}`,
    `worktree: ${prep.worktree}`,
    `declared files: ${files.join(', ')}`,
    `slug: ${unitSlug(u)}${fix ? `-fix${fix.round}` : ''}`,
    `work log: ${prep.work_log}`,
    `report: ${prep.report}`,
    `admission: ${admitLine}`,
  ]
  if (u.executor === 'codex') {
    return agent(
      [...common, `route: ${u.route || '$SOL_ROUTE'}`, fix ? `fix round ${fix.round}: resume thread_id ${fix.threadId} with the fix-round contract` : 'fresh launch', 'Launch and supervise the build as your instructions say and return the receipt fields.'].join('\n'),
      { label: `${fix ? `fix${fix.round}` : 'build'} ${u.id} (codex)`, phase: 'Build', agentType: `${NS}forge-codex-executor`, schema: EXECUTION },
    )
  }
  return agent(
    [...common, fix ? `fix round ${fix.round}: same worktree, same rules; the fix list is in the contract` : 'first round', 'Build the unit as your instructions say and return the receipt fields.'].join('\n'),
    { label: `${fix ? `fix${fix.round}` : 'build'} ${u.id} (claude)`, phase: 'Build', agentType: `${NS}forge-claude-executor`, schema: EXECUTION },
  )
}

async function verify(u, prep, files, run) {
  const verdict = await agent(
    [
      `profile: ${PROFILE}`,
      `unit: ${u.id}`,
      `contract: ${prep.contract}`,
      `worktree: ${prep.worktree}`,
      `base_sha: ${A.baseSha}`,
      `admitted files: ${files.join(', ')}`,
      `other active lanes' files: ${[...active.files].filter(([, o]) => o !== u.id).map(([f]) => f).join(', ') || 'none'}`,
      `executor receipt: ${JSON.stringify(run)}`,
      `report: ${prep.report}`,
      `work log: ${prep.work_log}`,
      `proof: ${u.proof}`,
      `slug: ${unitSlug(u)}`,
      'Check the unit as your instructions say for this profile and return the receipt fields.',
    ].join('\n'),
    { label: `verify ${u.id}`, phase: 'Verify', agentType: `${NS}forge-unit-verifier`, schema: VERIFICATION },
  )
  let crossModel = null
  if (PROFILE === 'assured' && u.executor === 'claude' && verdict && verdict.result === 'PASS') {
    crossModel = await agent(
      [
        'mode: unit',
        'round: 1',
        `slug: ${unitSlug(u)}-review${run.fixRound ? `-fix${run.fixRound}` : ''}`,
        `unit: ${u.id}`,
        `worktree: ${prep.worktree}`,
        `base_sha: ${A.baseSha}`,
        `declared files: ${files.join(', ')}`,
        `contract: ${prep.contract}`,
        `report: ${prep.report}`,
        'Run exactly one unit review round as your instructions say and return the receipt fields.',
      ].join('\n'),
      { label: `codex reviews ${u.id}`, phase: 'Verify', agentType: `${NS}forge-codex-reviewer`, schema: REVIEW },
    )
  }
  return { verdict, crossModel }
}

async function runUnit(u) {
  const prep = prepared.get(u.id)
  const result = { id: u.id, executor: u.executor, builder: builderId(u), status: 'NOT_LAUNCHED', admissions: [], fix_rounds: 0 }
  if (!prep.ok) return { ...result, status: 'BLOCKED', problem: prep.problem }
  Object.assign(result, { worktree: prep.worktree, branch: prep.branch, contract: prep.contract, report: prep.report, work_log: prep.work_log, rephrased: prep.rephrased || [] })

  if (!(await acquire(u))) return { ...result, problem: lanes.stopped || 'no HEAVY lease is free outside this wave' }
  try {
    let files = [...u.files]
    const launch = await admit(u, prep, 'launch', files, null)
    if (launch.result === 'STOPPED') return { ...result, problem: lanes.stopped }
    result.admissions.push(launch.guard_output)
    if (launch.result !== 'ADMIT') return { ...result, status: 'REJECTED', problem: launch.guard_output }

    let run = await execute(u, prep, launch.guard_output, files, null)
    while (run && run.status === 'EXPANSION_REQUEST' && Array.isArray(run.expansion_files)) {
      const widened = [...new Set([...files, ...run.expansion_files])]
      const expansion = await admit(u, prep, 'expand', widened, files)
      result.admissions.push(expansion.guard_output)
      if (expansion.result !== 'ADMIT') return { ...result, status: 'BLOCKED', problem: `expansion refused: ${expansion.guard_output}`, run }
      files = widened
      run = await execute(u, prep, expansion.guard_output, files, null)
    }
    if (!run || run.status !== 'DONE') return { ...result, status: run ? run.status : 'FAILED', run }

    let threadId = run.thread_id || null
    let checks = await verify(u, prep, files, run)
    const passed = c => c.verdict && c.verdict.result === 'PASS' && (!c.crossModel || c.crossModel.verdict === 'APPROVED')
    while (!passed(checks) && result.fix_rounds < MAX_FIX) {
      result.fix_rounds++
      const items = [
        ...((checks.verdict && checks.verdict.fix_list) || []),
        ...((checks.crossModel && checks.crossModel.verdict === 'REVISE' && checks.crossModel.findings) || []),
      ]
      const fixContract = `/tmp/forge-contract-${unitSlug(u)}-fix${result.fix_rounds}.md`
      const refrozen = await agent(contractPrompt(u, fixContract, { round: result.fix_rounds, items }), {
        label: `fix contract ${u.id} r${result.fix_rounds}`,
        phase: 'Prepare',
        schema: PREP,
      })
      if (!refrozen || !refrozen.ok) return { ...result, status: 'FAILED', problem: 'could not write the fix contract', checks }
      let line = 'continuing in the admitted lane (sprint fix round)'
      if (PROFILE === 'assured') {
        const readmit = await admit(u, { ...prep, contract: refrozen.contract, manifest: refrozen.manifest, manifest_sha256: refrozen.manifest_sha256 }, 'expand', files, files)
        result.admissions.push(readmit.guard_output)
        if (readmit.result !== 'ADMIT') return { ...result, status: 'BLOCKED', problem: `fix round refused: ${readmit.guard_output}`, checks }
        line = readmit.guard_output
      }
      run = await execute(u, prep, line, files, { round: result.fix_rounds, contract: refrozen.contract, threadId })
      if (!run || run.status !== 'DONE') return { ...result, status: run ? run.status : 'FAILED', run, checks }
      run.fixRound = result.fix_rounds
      threadId = run.thread_id || threadId
      checks = await verify(u, prep, files, run)
    }

    Object.assign(result, { files, run, checks })
    if (!passed(checks)) return { ...result, status: 'FAILED', problem: `still failing after ${result.fix_rounds} fix round(s); the driver re-routes or pauses` }

    const reviewer = checks.crossModel
      ? { id: checks.crossModel.reviewer, thread: checks.crossModel.thread_id }
      : { id: 'claude-forge-unit-verifier', thread: `verify-${unitSlug(u)}` }
    return {
      ...result,
      status: 'READY',
      successor_receipts: {
        id: u.id,
        builder: builderId(u),
        report_receipt: `predecessor=${u.id};consumed_by=claude-forge-driver;report=${prep.report};receipt_id=${unitSlug(u)}-report`,
        review_receipt: PROFILE === 'assured'
          ? `predecessor=${u.id};builder=${builderId(u)};reviewer=${reviewer.id};thread=${reviewer.thread};verdict=APPROVED;receipt_id=${unitSlug(u)}-review`
          : null,
      },
    }
  } finally {
    leave(u)
    release(u)
  }
}

const results = (await parallel(units.map(u => () => runUnit(u)))).map((r, i) => r || { id: units[i].id, status: 'ERROR', problem: 'the unit chain threw; see the run transcript' })

const ready = results.filter(r => r.status === 'READY').map(r => r.id)
log(`${ready.length}/${units.length} unit(s) ready to integrate${lanes.stopped ? `; stopped: ${lanes.stopped}` : ''}`)
return {
  status: lanes.stopped ? 'STOPPED' : ready.length === units.length ? 'READY' : 'PARTIAL',
  stop_the_line: lanes.stopped,
  profile: PROFILE,
  base_sha: A.baseSha,
  ready,
  units: results,
  next: 'Read every receipt and report in full. Integrate READY units in dependency order and apply the profile commit check; only the driver commits. No executor committed.',
}
