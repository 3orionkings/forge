export const meta = {
  name: 'forge-decompose',
  description: 'Forge Act 4 setup: split an APPROVED plan into units and waves, challenge every parallel claim, and route the units at the active profile',
  whenToUse: 'After Gate 1, before any build; its result feeds Gate 2 under assured and forge-build-wave under either profile',
  phases: [
    { title: 'Plan', detail: 'forge-unit-planner proposes units, files, proofs and dependencies' },
    { title: 'Challenge', detail: 'one skeptic per multi-unit wave looks for hidden coupling' },
    { title: 'Route', detail: 'assured only: Codex argues its side of the split' },
  ],
}

// args: { repo, slug, profile, plan?, overlay?, agentNamespace? }
// Read-only: nothing is written; the driver records the routing table and decides Gate 2.
const A = args && typeof args === 'object' ? args : {}
if (!A.repo || !A.slug) throw new Error('forge-decompose needs args.repo (absolute path) and args.slug')
if (A.profile !== 'sprint' && A.profile !== 'assured') throw new Error('forge-decompose needs args.profile: "sprint" or "assured"')
const NS = typeof A.agentNamespace === 'string' ? A.agentNamespace : 'forge:'
const PLAN = A.plan || 'PLAN.md'

const UNIT = {
  type: 'object',
  properties: {
    id: { type: 'string' },
    step: { type: 'string' },
    goal: { type: 'string' },
    done_condition: { type: 'string' },
    files: { type: 'array', items: { type: 'string' } },
    proof: { type: 'string' },
    proof_scope: { type: 'string', enum: ['targeted', 'broad'] },
    weight: { type: 'string', enum: ['LIGHT', 'HEAVY'] },
    lane_type: { type: 'string', enum: ['feature', 'integration', 'control-plane', 'review'] },
    depends_on: { type: 'array', items: { type: 'string' } },
    executor: { type: 'string', enum: ['codex', 'claude'] },
    reason: { type: 'string' },
  },
  required: ['id', 'goal', 'done_condition', 'files', 'proof', 'proof_scope', 'weight', 'lane_type', 'depends_on', 'executor', 'reason'],
}
const UNIT_PLAN = {
  type: 'object',
  properties: {
    plan_verdict: { type: 'string' },
    units: { type: 'array', items: UNIT },
    unmapped: { type: 'array', items: { type: 'string' } },
    open_questions: { type: 'array', items: { type: 'string' } },
  },
  required: ['plan_verdict', 'units'],
}
const COUPLING = {
  type: 'object',
  properties: {
    couplings: {
      type: 'array',
      items: {
        type: 'object',
        properties: { earlier: { type: 'string' }, later: { type: 'string' }, evidence: { type: 'string' } },
        required: ['earlier', 'later', 'evidence'],
      },
    },
  },
  required: ['couplings'],
}
const ROUTING_RECEIPT = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['APPROVED', 'REVISE', 'FAILED'] },
    thread_id: { type: 'string' },
    reviewer: { type: 'string' },
    verdict_file: { type: 'string' },
    findings: { type: 'array', items: { type: 'string' } },
    failure: { type: 'string' },
  },
  required: ['verdict', 'reviewer', 'findings'],
}

phase('Plan')
const proposal = await agent(
  [
    `repo: ${A.repo}`,
    `plan: ${PLAN}`,
    `profile: ${A.profile}`,
    A.overlay ? `project overlay: ${A.overlay}` : 'project overlay: none (global default executor_policy=any)',
    'Decompose the plan as your instructions say and return the unit plan fields.',
  ].join('\n'),
  { label: 'decompose', phase: 'Plan', agentType: `${NS}forge-unit-planner`, schema: UNIT_PLAN },
)
if (!proposal) throw new Error('forge-unit-planner returned nothing')
if (proposal.plan_verdict !== 'APPROVED') {
  return { status: 'BLOCKED', reason: `plan verdict is ${proposal.plan_verdict}, not APPROVED` }
}

// Deterministic checks the planner's word is not enough for.
const units = proposal.units
const byId = new Map(units.map(u => [u.id, u]))
if (byId.size !== units.length) throw new Error('unit ids are not unique')
const problems = []
for (const u of units) {
  for (const f of u.files) {
    if (/[*?[\]]/.test(f)) problems.push(`${u.id}: glob in file set (${f})`)
    if (f === '.ai/HANDOFF.md') problems.push(`${u.id}: declares .ai/HANDOFF.md, which only the integration owner writes`)
  }
  for (const d of u.depends_on) if (!byId.has(d)) problems.push(`${u.id}: depends on unknown unit ${d}`)
}
// Any shared file is a dependency, whatever the planner said: serial is the safe default.
for (let i = 0; i < units.length; i++) {
  for (let j = i + 1; j < units.length; j++) {
    const shared = units[i].files.filter(f => units[j].files.includes(f))
    if (shared.length && !units[j].depends_on.includes(units[i].id) && !units[i].depends_on.includes(units[j].id)) {
      units[j].depends_on.push(units[i].id)
      log(`${units[j].id} now waits for ${units[i].id}: both write ${shared.join(', ')}`)
    }
  }
}

function waves() {
  const depth = new Map()
  const visiting = new Set()
  const visit = id => {
    if (depth.has(id)) return depth.get(id)
    if (visiting.has(id)) throw new Error(`dependency cycle through ${id}`)
    visiting.add(id)
    const d = 1 + Math.max(0, ...byId.get(id).depends_on.map(visit))
    visiting.delete(id)
    depth.set(id, d)
    return d
  }
  units.forEach(u => visit(u.id))
  const out = []
  for (const u of units) {
    const d = depth.get(u.id) - 1
    ;(out[d] = out[d] || []).push(u.id)
  }
  return out
}

function wavesOrProblem() {
  try {
    return waves()
  } catch (error) {
    problems.push(error.message)
    return null
  }
}

let plannedWaves = wavesOrProblem()
if (!plannedWaves) return { status: 'NEEDS_FIXES', profile: A.profile, problems, units }

phase('Challenge')
// Parallel claims are the expensive ones to get wrong, so each multi-unit wave gets
// one skeptic. A found coupling becomes an edge and the waves are recomputed.
const multi = plannedWaves.filter(w => w.length > 1)
const challenges = await parallel(multi.map((wave, k) => () => agent(
  [
    `You are a skeptic checking a claim that these Forge units can be built in parallel, in ${A.repo}. Read-only: do not edit, stage or create anything.`,
    `Plan: ${PLAN}. Units:\n${JSON.stringify(wave.map(id => byId.get(id)), null, 2)}`,
    'Look for hidden coupling: a file one unit generates or another reads, a shared schema, migration, config, lockfile or fixture, an import of something the other creates, or a proof that exercises the other unit. Read the code; do not guess.',
    'Report each coupling as earlier -> later with the evidence (path:line). Report none if there is none.',
  ].join('\n'),
  { label: `challenge wave ${k + 1}`, phase: 'Challenge', schema: COUPLING },
)))
let added = 0
for (const c of challenges.filter(Boolean).flatMap(r => r.couplings)) {
  const later = byId.get(c.later)
  if (!later || !byId.has(c.earlier) || c.earlier === c.later) continue
  if (!later.depends_on.includes(c.earlier)) {
    later.depends_on.push(c.earlier)
    added++
    log(`${c.later} now waits for ${c.earlier}: ${c.evidence}`)
  }
}
if (added) {
  plannedWaves = wavesOrProblem()
  if (!plannedWaves) return { status: 'NEEDS_FIXES', profile: A.profile, problems, units }
}

const heavyWaves = plannedWaves.filter(w => w.filter(id => byId.get(id).weight === 'HEAVY').length > 1)
for (const w of heavyWaves) log(`wave ${plannedWaves.indexOf(w) + 1} holds more than one HEAVY unit; the guard runs them one at a time`)
for (const w of plannedWaves.filter(w => w.length > 3)) log(`wave ${plannedWaves.indexOf(w) + 1} has ${w.length} units; at most three lanes run at once`)

let codexRouting = null
if (A.profile === 'assured') {
  phase('Route')
  codexRouting = await agent(
    [
      'mode: routing',
      'round: 1',
      `slug: ${A.slug}-routing`,
      `plan: ${PLAN}`,
      `repo: ${A.repo}`,
      `proposed split:\n${units.map(u => `- ${u.id} -> ${u.executor}: ${u.reason}`).join('\n')}`,
      'Run exactly one routing round as your instructions say and return the receipt fields.',
    ].join('\n'),
    { label: 'codex argues the split', phase: 'Route', agentType: `${NS}forge-codex-reviewer`, schema: ROUTING_RECEIPT },
  )
}

return {
  status: problems.length ? 'NEEDS_FIXES' : 'READY',
  profile: A.profile,
  problems,
  units,
  waves: plannedWaves,
  routing: A.profile === 'sprint'
    ? { mode: 'applied', note: 'sprint applies the overlay or default executor; record one line per unit, no Gate 2' }
    : { mode: 'argued', claude_proposal: units.map(u => ({ id: u.id, executor: u.executor, reason: u.reason })), codex: codexRouting },
  gate2_required: A.profile === 'assured' && units.length > 1,
  unmapped: proposal.unmapped || [],
  open_questions: proposal.open_questions || [],
}
