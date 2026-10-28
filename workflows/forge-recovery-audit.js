export const meta = {
  name: 'forge-recovery-audit',
  description: 'Forge recovery Acts 1-2: inventory a wrecked run in parallel, preserve every dirty tree and tip, build the State Ledger, then check the gate independently. Destroys nothing.',
  whenToUse: 'Only when a Forge run has actually crashed, stalled or stranded work; never for a healthy run',
  phases: [
    { title: 'Inventory', detail: 'worktrees, branches and commit union, lane liveness, claims, all read-only' },
    { title: 'Preserve', detail: 'forge-recovery-auditor writes patches, archives, safety refs and the ledger' },
    { title: 'Check', detail: 'an independent critic tests the Act 1 and Act 2 gates' },
  ],
}

// args: { repo, run, recoveryDir?, lanes?: [{ id, work_log, jsonl, err, worktree }],
//         reports?: [paths], execLog?, plan?, reviewLog?, agentNamespace? }
// run is the recovery run id the caller stamped once (e.g. recovery-20261028).
const A = args && typeof args === 'object' ? args : {}
if (!A.repo || !A.run) throw new Error('forge-recovery-audit needs args.repo (absolute path) and args.run')
const NS = typeof A.agentNamespace === 'string' ? A.agentNamespace : 'forge:'
const DIR = A.recoveryDir || `/tmp/forge-recovery-${A.run}`
const lanes = Array.isArray(A.lanes) ? A.lanes : []
const READ_ONLY = `Read-only: do not edit, stage, stash, reset, clean, checkout or delete anything, and do not create refs. Repository: ${A.repo}.`

const LIST = { type: 'object', properties: { items: { type: 'array', items: { type: 'string' } }, notes: { type: 'string' } }, required: ['items'] }
const LIVENESS = {
  type: 'object',
  properties: { status: { type: 'string', enum: ['DONE', 'ALIVE', 'STALLED'] }, evidence: { type: 'string' } },
  required: ['status', 'evidence'],
}
const AUDIT = {
  type: 'object',
  properties: {
    ledger: { type: 'string' },
    receipts: { type: 'string' },
    preserved_worktrees: { type: 'array', items: { type: 'string' } },
    safety_refs: { type: 'array', items: { type: 'string' } },
    unique_commits: { type: 'string' },
    claims: { type: 'string' },
    current_failure: { type: 'string' },
    gate_met: { type: 'boolean' },
    missing: { type: 'array', items: { type: 'string' } },
  },
  required: ['ledger', 'gate_met'],
}
const CRITIC = {
  type: 'object',
  properties: { gate_met: { type: 'boolean' }, gaps: { type: 'array', items: { type: 'string' } } },
  required: ['gate_met', 'gaps'],
}

phase('Inventory')
const sweeps = [
  () => agent(`${READ_ONLY}\nList every worktree (git worktree list --porcelain) with its HEAD, branch or detached state, and whether it is dirty (staged, unstaged, untracked counts). One item per worktree.`, { label: 'worktrees', phase: 'Inventory', schema: LIST }),
  () => agent(`${READ_ONLY}\nList every local branch and every detached tip not reachable from a branch, with its SHA and how far ahead it is of the default branch. Then compute the union of unique commits across them by SHA (git rev-list over all of them, deduplicated), and report it beside the naive sum of ahead-counts. One item per branch or tip, plus one summary item for the union.`, { label: 'branches', phase: 'Inventory', schema: LIST }),
  () => agent(`${READ_ONLY}\nList every claim the run made about its own progress: lane reports${A.reports ? ` (${A.reports.join(', ')})` : ''}, ${A.execLog || 'the execution log'}, ${A.reviewLog || 'the review log'} and ${A.plan || 'the plan'}. For each claim say whether an artifact backs it (a commit SHA, a proof output, a reviewer thread id with a returned verdict) and cite it, or mark it unreceipted. Read every report in full.`, { label: 'claims', phase: 'Inventory', schema: LIST }),
  ...lanes.map(l => () => agent(
    [`lane: ${l.id}`, `work log: ${l.work_log}`, `jsonl: ${l.jsonl || 'none'}`, `err: ${l.err || 'none'}`, `worktree: ${l.worktree}`, 'Judge liveness as your instructions say. Report only; stop nothing.'].join('\n'),
    { label: `liveness ${l.id}`, phase: 'Inventory', agentType: `${NS}forge-liveness`, schema: LIVENESS },
  ).then(r => (r ? { lane: l.id, ...r } : null))),
]
const found = await parallel(sweeps)
const [worktrees, branches, claims, ...liveness] = found
const inventory = {
  worktrees: worktrees ? worktrees.items : ['(worktree sweep failed)'],
  branches: branches ? branches.items : ['(branch sweep failed)'],
  claims: claims ? claims.items : ['(claims sweep failed)'],
  lanes: liveness.filter(Boolean),
}
if (lanes.length && inventory.lanes.length < lanes.length) log(`${lanes.length - inventory.lanes.length} lane(s) could not be classified; the gate stays unmet until they are`)

phase('Preserve')
const audit = await agent(
  [
    `repo: ${A.repo}`,
    `RUN: ${A.run}`,
    `recovery directory: ${DIR}`,
    `Inventory gathered so far:\n${JSON.stringify(inventory, null, 2)}`,
    'Carry out Act 1 (halt and preserve) and Act 2 (reconstruct) as your instructions say, using this inventory as claims to verify, not as facts. Write the State Ledger and return the audit fields.',
  ].join('\n'),
  { label: 'preserve and reconstruct', phase: 'Preserve', agentType: `${NS}forge-recovery-auditor`, schema: AUDIT },
)
if (!audit) return { status: 'FAILED', stage: 'preserve', inventory }

phase('Check')
const critic = await agent(
  [
    `You are an independent critic of a Forge recovery audit. ${READ_ONLY} You may read the recovery directory ${DIR}.`,
    `Ledger: ${audit.ledger}. Receipts: ${audit.receipts || `${DIR}/receipts.txt`}.`,
    'Act 1 gate: every live lane classified by evidence; every dirty worktree has a patch, an untracked-file archive and recorded hashes; every detached or unmerged tip has a safety ref under refs/forge-recovery/; nothing was deleted.',
    'Act 2 gate: the ledger names the true act reached, the verdict state, the SHA-deduplicated commit union, each artifact as land-now, rework or dead, the current failing check at HEAD, and every claim as verified or refuted with an artifact citation.',
    'Check each item against the files and the repository, not against the ledger\'s own words. List every gap.',
  ].join('\n'),
  { label: 'gate check', phase: 'Check', schema: CRITIC },
)

const gaps = [...(audit.missing || []), ...(critic ? critic.gaps : ['the gate critic did not return'])]
return {
  status: audit.gate_met && critic && critic.gate_met ? 'LEDGER_READY' : 'GAPS',
  recovery_dir: DIR,
  ledger: audit.ledger,
  audit,
  inventory,
  gaps,
  next: 'Dispatch stays frozen until the ledger gate is met. Then Act 3 (repair the control plane) under forge-recovery. Anything destructive needs the owner\'s explicit approval.',
}
