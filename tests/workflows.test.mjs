// Offline checks for the Forge workflow scripts: each script body runs with a mocked
// agent() so its control flow is tested without a single model call.
// Usage: node tests/workflows.test.mjs [workflows-dir]
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'

const dir = process.argv[2] || fileURLToPath(new URL('../workflows', import.meta.url))
const AsyncFunction = (async () => {}).constructor
let failures = 0
const results = []

function load(name) {
  const src = readFileSync(`${dir}/${name}.js`, 'utf8')
  assert.ok(src.startsWith('export const meta = {'), `${name}: first statement must be export const meta`)
  for (const banned of ['Date.now(', 'Math.random(', 'new Date()', 'import(', 'require(']) {
    assert.ok(!src.includes(banned), `${name}: uses ${banned}`)
  }
  const metaEnd = src.indexOf('\n}\n')
  const metaText = src.slice('export const meta = '.length, metaEnd + 2)
  assert.ok(!metaText.includes('${'), `${name}: meta must not interpolate`)
  const meta = Function(`"use strict"; return (${metaText})`)()
  assert.equal(meta.name, name)
  assert.ok(meta.description && meta.description.length < 300, `${name}: description`)
  const body = src.replace('export const meta =', 'const meta =')
  const fn = new AsyncFunction('agent', 'parallel', 'pipeline', 'phase', 'log', 'args', 'budget', 'workflow', body)
  const phaseTitles = new Set((meta.phases || []).map(p => p.title))
  return { meta, fn, phaseTitles }
}

async function run(name, args, respond) {
  const { meta, fn, phaseTitles } = load(name)
  const calls = []
  const logs = []
  const ctx = { active: 0, maxActive: 0, heavy: 0, maxHeavy: 0 }
  const agent = async (prompt, opts = {}) => {
    if (opts.phase) assert.ok(phaseTitles.has(opts.phase), `${name}: phase "${opts.phase}" missing from meta.phases`)
    const call = { prompt, opts }
    calls.push(call)
    return respond(call, ctx, calls)
  }
  const parallel = async thunks => Promise.all(thunks.map(t => Promise.resolve().then(t).catch(() => null)))
  const pipeline = async () => { throw new Error('pipeline not expected') }
  const phase = title => assert.ok(phaseTitles.has(title), `${name}: phase("${title}") missing from meta.phases`)
  const log = m => logs.push(m)
  const budget = { total: null, spent: () => 0, remaining: () => Infinity }
  const value = await fn(agent, parallel, pipeline, phase, log, args, budget, async () => null)
  return { meta, value, calls, logs, ctx }
}

async function check(label, body) {
  try {
    await body()
    results.push(`PASS: ${label}`)
  } catch (error) {
    failures++
    results.push(`FAIL: ${label}\n  ${error.stack.split('\n').slice(0, 3).join('\n  ')}`)
  }
}

const wait = ms => new Promise(r => setTimeout(r, ms))
const jitter = () => wait(Math.floor(Math.random() * 15))
const type = call => (call.opts.agentType || 'default').replace(/^forge:/, '')

// ---------------------------------------------------------------- plan review
const reviewArgs = { repo: '/r', slug: 's1', maxRounds: 3 }
await check('plan review: REVISE then APPROVED resumes the same thread', async () => {
  let round = 0
  const { value, calls } = await run('forge-plan-review', reviewArgs, async call => {
    await jitter()
    if (type(call) === 'forge-codex-reviewer') {
      round++
      if (round > 1) assert.match(call.prompt, /thread_id: T-1/)
      return { verdict: round === 1 ? 'REVISE' : 'APPROVED', thread_id: 'T-1', reviewer: 'codex gpt-5.6-sol @ ultra', verdict_file: `/tmp/v${round}`, findings: round === 1 ? ['race in X'] : [] }
    }
    return { accepted: ['race in X'], rejected: [], plan_changed: true, improvements: ['closed race'] }
  })
  assert.equal(value.status, 'APPROVED')
  assert.equal(value.rounds, 2)
  assert.equal(calls.filter(c => type(c) === 'forge-codex-reviewer').length, 2)
  assert.equal(calls.filter(c => type(c) === 'default').length, 2, 'the approving round is still logged')
})
await check('plan review: deadlock at the round cap never fakes convergence', async () => {
  const { value } = await run('forge-plan-review', reviewArgs, async call =>
    type(call) === 'forge-codex-reviewer'
      ? { verdict: 'REVISE', thread_id: 'T-9', reviewer: 'codex', verdict_file: '/tmp/v', findings: ['still wrong'] }
      : { accepted: [], rejected: [{ finding: 'still wrong', reason: 'out of scope' }], plan_changed: false })
  assert.equal(value.status, 'DEADLOCK')
  assert.equal(value.rounds, 3)
  assert.deepEqual(value.unresolved, ['still wrong'])
  assert.equal(value.claude_positions[0].reason, 'out of scope')
})
await check('plan review: a resume that lands on another thread fails', async () => {
  let round = 0
  const { value } = await run('forge-plan-review', reviewArgs, async call => {
    if (type(call) === 'forge-codex-reviewer') {
      round++
      return { verdict: 'REVISE', thread_id: round === 1 ? 'T-1' : 'T-OTHER', reviewer: 'codex', verdict_file: '/tmp/v', findings: ['f'] }
    }
    return { accepted: ['f'], rejected: [], plan_changed: true }
  })
  assert.equal(value.status, 'FAILED')
  assert.match(value.failure, /expected T-1/)
})
await check('plan review: a failed round stops the loop', async () => {
  const { value, calls } = await run('forge-plan-review', reviewArgs, async () => ({ verdict: 'FAILED', thread_id: '', reviewer: 'codex', verdict_file: '', findings: [], failure: 'auth expired' }))
  assert.equal(value.status, 'FAILED')
  assert.equal(calls.length, 1)
})
await check('plan review: missing slug is refused', async () => {
  await assert.rejects(run('forge-plan-review', { repo: '/r' }, async () => ({})), /slug/)
})

// ---------------------------------------------------------------- decompose
const unit = (id, files, extra = {}) => ({ id, step: '1', goal: `g ${id}`, done_condition: 'd', files, proof: `test ${id}`, proof_scope: 'targeted', weight: 'LIGHT', lane_type: 'feature', depends_on: [], executor: 'codex', reason: 'cost', ...extra })
await check('decompose: shared files and found couplings become dependencies', async () => {
  const { value } = await run('forge-decompose', { repo: '/r', slug: 's2', profile: 'sprint' }, async call => {
    await jitter()
    if (type(call) === 'forge-unit-planner') {
      return { plan_verdict: 'APPROVED', units: [unit('a', ['x.ts']), unit('b', ['x.ts', 'y.ts']), unit('c', ['z.ts']), unit('d', ['w.ts'])] }
    }
    return { couplings: call.prompt.includes('"id": "d"') ? [{ earlier: 'c', later: 'd', evidence: 'd imports c.ts:3' }] : [] }
  })
  assert.equal(value.status, 'READY')
  assert.deepEqual(value.waves, [['a', 'c'], ['b', 'd']])
  assert.equal(value.routing.mode, 'applied')
  assert.equal(value.gate2_required, false)
})
await check('decompose: assured collects the Codex routing argument and flags Gate 2', async () => {
  const { value, calls } = await run('forge-decompose', { repo: '/r', slug: 's3', profile: 'assured' }, async call => {
    if (type(call) === 'forge-unit-planner') return { plan_verdict: 'APPROVED', units: [unit('a', ['x']), unit('b', ['y'], { executor: 'claude', reason: 'needs judgement' })] }
    if (type(call) === 'forge-codex-reviewer') { assert.match(call.prompt, /mode: routing/); return { verdict: 'REVISE', reviewer: 'codex', findings: ['give b to codex'] } }
    return { couplings: [] }
  })
  assert.equal(value.routing.mode, 'argued')
  assert.equal(value.routing.codex.verdict, 'REVISE')
  assert.equal(value.gate2_required, true)
  assert.equal(calls.filter(c => type(c) === 'forge-codex-reviewer').length, 1)
})
await check('decompose: a cycle or HANDOFF declaration is reported, not built', async () => {
  const { value } = await run('forge-decompose', { repo: '/r', slug: 's4', profile: 'sprint' }, async call =>
    type(call) === 'forge-unit-planner'
      ? { plan_verdict: 'APPROVED', units: [unit('a', ['x'], { depends_on: ['b'] }), unit('b', ['.ai/HANDOFF.md'], { depends_on: ['a'] })] }
      : { couplings: [] })
  assert.equal(value.status, 'NEEDS_FIXES')
  assert.ok(value.problems.some(p => /cycle/.test(p)))
  assert.ok(value.problems.some(p => /HANDOFF/.test(p)))
})
await check('decompose: an unapproved plan is blocked', async () => {
  const { value } = await run('forge-decompose', { repo: '/r', slug: 's5', profile: 'sprint' }, async () => ({ plan_verdict: 'REVISE', units: [] }))
  assert.equal(value.status, 'BLOCKED')
})

// ---------------------------------------------------------------- build wave
const SHA = 'a'.repeat(40)
const waveArgs = (units, extra = {}) => ({ repo: '/r', slug: 's6', profile: 'sprint', baseSha: SHA, plan: '/r/PLAN.md', act2Log: '/r/LOG.md', verdict: 'APPROVED', units, ...extra })
const wunit = (id, files, extra = {}) => ({ id, goal: `g ${id}`, done_condition: 'd', files, proof: `test ${id}`, proof_scope: 'targeted', weight: 'LIGHT', executor: 'codex', ...extra })

function waveResponder(options = {}) {
  const state = { admissions: [], executions: 0, verifies: {} }
  const respond = async (call, ctx) => {
    const t = type(call)
    if (t === 'default') {
      await jitter()
      if (call.opts.label === 'prepare wave') return { ok: true, forge_skills: '/skills', overlay: '/tmp/o.md', plan: '/r/PLAN.md', act2_log: '/r/LOG.md', worktree_root: '/wt' }
      const id = call.opts.label.replace(/^(freeze|fix contract) /, '').split(' ')[0]
      return { ok: true, worktree: `/wt/${id}`, branch: `sol/${id}`, contract: call.prompt.match(/\/tmp\/forge-contract-[^ ]+\.md/)[0], contract_sha256: 'c'.repeat(64), work_log: `/tmp/w-${id}`, report: `/tmp/r-${id}`, manifest: `/tmp/m-${id}`, manifest_sha256: 'm'.repeat(64) }
    }
    if (t === 'forge-admission') {
      const decl = JSON.parse(call.prompt.slice(call.prompt.indexOf('{')))
      state.admissions.push(decl)
      await jitter()
      if (options.reject && decl.action === 'launch' && decl.files.includes(options.reject)) return { result: 'REJECT', guard_output: 'REJECT: LOAD_HIGH: machine busy' }
      return { result: 'ADMIT', guard_output: `ADMIT: action=${decl.action}` }
    }
    if (t === 'forge-claude-executor' || t === 'forge-codex-executor') {
      const heavy = /declared files: .*heavy/.test(call.prompt)
      ctx.active++; ctx.maxActive = Math.max(ctx.maxActive, ctx.active)
      if (heavy) { ctx.heavy++; ctx.maxHeavy = Math.max(ctx.maxHeavy, ctx.heavy) }
      await wait(5 + Math.floor(Math.random() * 25))
      ctx.active--; if (heavy) ctx.heavy--
      state.executions++
      const id = call.prompt.match(/unit: (\S+)/)[1]
      if (options.expand === id && !call.prompt.includes('extra.ts')) return { status: 'EXPANSION_REQUEST', changed_files: [], proof_exit: '', expansion_files: ['extra.ts'], expansion_reason: 'needs helper' }
      if (options.expandSame === id) return { status: 'EXPANSION_REQUEST', changed_files: [], proof_exit: '', expansion_files: ['a.ts'], expansion_reason: 'already declared' }
      if (options.expandForever === id) return { status: 'EXPANSION_REQUEST', changed_files: [], proof_exit: '', expansion_files: [`more-${state.executions}.ts`], expansion_reason: 'one more file' }
      return { status: 'DONE', thread_id: t === 'forge-codex-executor' ? `TH-${id}` : '', changed_files: [], proof_exit: '0' }
    }
    if (t === 'forge-unit-verifier') {
      const id = call.prompt.match(/unit: (\S+)/)[1]
      state.verifies[id] = (state.verifies[id] || 0) + 1
      const fail = options.failOnce === id && state.verifies[id] === 1
      return { result: fail ? 'FAIL' : 'PASS', fix_list: fail ? ['fix the edge case'] : [] }
    }
    if (t === 'forge-codex-reviewer') return { verdict: 'APPROVED', thread_id: 'RV-1', reviewer: 'codex gpt-5.6-sol @ ultra', verdict_file: '/tmp/v', findings: [] }
    throw new Error(`unexpected agent ${t}`)
  }
  return { state, respond }
}

await check('build wave: never more than three lanes or one HEAVY at once', async () => {
  const units = [wunit('a', ['a.ts']), wunit('b', ['b-heavy.ts'], { weight: 'HEAVY', proof_scope: 'broad' }), wunit('c', ['c.ts']), wunit('d', ['d-heavy.ts'], { weight: 'HEAVY', proof_scope: 'broad' }), wunit('e', ['e.ts'], { executor: 'claude' }), wunit('f', ['f.ts'])]
  for (let i = 0; i < 5; i++) {
    const { state, respond } = waveResponder()
    const { value, ctx } = await run('forge-build-wave', waveArgs(units), respond)
    assert.equal(value.status, 'READY', JSON.stringify(value.units.filter(u => u.status !== 'READY')))
    assert.ok(ctx.maxActive <= 3, `max lanes ${ctx.maxActive}`)
    assert.ok(ctx.maxHeavy <= 1, `max heavy ${ctx.maxHeavy}`)
    for (const d of state.admissions) {
      assert.ok(d.active_lanes < 3, 'guard saw a fourth lane')
      if (d.weight === 'HEAVY') assert.equal(d.heavy_leases, 0, 'guard saw a second HEAVY')
      assert.equal(d.clean_tree, true)
      assert.equal(d.freeze_manifest, null, 'sprint declares no freeze')
      assert.ok(!d.parallel_files.some(f => d.files.includes(f)), 'own files listed as another lane')
      assert.ok(!d.active_writer_worktrees.includes(d.worktree), 'own worktree listed as another writer on launch')
    }
  }
})
await check('build wave: lanes outside the wave count against the cap', async () => {
  const { respond } = waveResponder()
  const units = [wunit('a', ['a.ts']), wunit('b', ['b.ts']), wunit('c', ['c.ts'])]
  const { value, ctx } = await run('forge-build-wave', waveArgs(units, { snapshot: { activeLanes: 2, heavyLeases: 0, writerWorktrees: ['/other'], files: ['o.ts'] } }), respond)
  assert.equal(value.status, 'READY')
  assert.equal(ctx.maxActive, 1)
})
await check('build wave: a REJECT stops the line for units not yet launched', async () => {
  const { respond } = waveResponder({ reject: 'c.ts' })
  const units = [wunit('a', ['a.ts']), wunit('b', ['b.ts']), wunit('c', ['c.ts']), wunit('d', ['d.ts']), wunit('e', ['e.ts'])]
  const { value } = await run('forge-build-wave', waveArgs(units), respond)
  assert.equal(value.status, 'STOPPED')
  assert.match(value.stop_the_line, /LOAD_HIGH/)
  assert.deepEqual(value.units.filter(u => u.status === 'REJECTED').map(u => u.id), ['c'], 'only the refused lane is REJECTED')
  const later = value.units.filter(u => u.id === 'd' || u.id === 'e')
  assert.ok(later.every(u => u.status === 'NOT_LAUNCHED' || u.status === 'READY'), JSON.stringify(later))
  assert.ok(later.some(u => u.status === 'NOT_LAUNCHED'), 'something queued after the reject was launched anyway')
})
await check('build wave: an expansion request is re-admitted in the same lane', async () => {
  const { state, respond } = waveResponder({ expand: 'a' })
  const { value } = await run('forge-build-wave', waveArgs([wunit('a', ['a.ts'])]), respond)
  assert.equal(value.status, 'READY')
  const expand = state.admissions.find(d => d.action === 'expand')
  assert.deepEqual(expand.current_files, ['a.ts'])
  assert.deepEqual(expand.files, ['a.ts', 'extra.ts'])
  assert.equal(expand.clean_tree, false)
  assert.ok(expand.active_writer_worktrees.includes('/wt/a'), 'expanding writer lists itself as the active writer')
})
await check('build wave: an expansion that names no new path is blocked, not looped', async () => {
  const { state, respond } = waveResponder({ expandSame: 'a' })
  const { value } = await run('forge-build-wave', waveArgs([wunit('a', ['a.ts'])]), respond)
  assert.equal(value.units[0].status, 'BLOCKED')
  assert.match(value.units[0].problem, /no new path/)
  assert.equal(state.admissions.filter(d => d.action === 'expand').length, 0)
})
await check('build wave: endless expansion requests stop after three', async () => {
  const { state, respond } = waveResponder({ expandForever: 'a' })
  const { value } = await run('forge-build-wave', waveArgs([wunit('a', ['a.ts'])]), respond)
  assert.equal(value.units[0].status, 'BLOCKED')
  assert.match(value.units[0].problem, /more than three expansions/)
  assert.equal(state.admissions.filter(d => d.action === 'expand').length, 3)
})
await check('build wave: sprint fix round reuses the lane; assured re-freezes and re-admits', async () => {
  for (const profile of ['sprint', 'assured']) {
    const { state, respond } = waveResponder({ failOnce: 'a' })
    const { value } = await run('forge-build-wave', waveArgs([wunit('a', ['a.ts'])], { profile }), respond)
    assert.equal(value.status, 'READY', profile)
    assert.equal(value.units[0].fix_rounds, 1)
    const expands = state.admissions.filter(d => d.action === 'expand')
    assert.equal(expands.length, profile === 'assured' ? 1 : 0, profile)
    if (profile === 'assured') {
      assert.match(expands[0].execution_contract, /fix1\.md$/)
      assert.equal(state.admissions[0].freeze_manifest, '/tmp/m-a')
    }
  }
})
await check('build wave: assured Claude units get a Codex review and both receipts', async () => {
  const { respond } = waveResponder()
  const units = [wunit('a', ['a.ts'], { executor: 'claude' }), wunit('b', ['b.ts'])]
  const { value, calls } = await run('forge-build-wave', waveArgs(units, { profile: 'assured' }), respond)
  assert.equal(value.status, 'READY')
  assert.equal(calls.filter(c => type(c) === 'forge-codex-reviewer').length, 1, 'only the Claude-built unit goes to Codex')
  const a = value.units.find(u => u.id === 'a').successor_receipts
  assert.equal(a.builder, 'fable-5')
  assert.match(a.review_receipt, /^predecessor=a;builder=fable-5;reviewer=codex gpt-5\.6-sol @ ultra;thread=RV-1;verdict=APPROVED;receipt_id=s6-a-review$/)
  const b = value.units.find(u => u.id === 'b').successor_receipts
  assert.match(b.review_receipt, /builder=sol-5\.6;reviewer=claude-forge-unit-verifier;/)
  assert.match(b.report_receipt, /^predecessor=b;consumed_by=claude-forge-driver;report=\/tmp\/r-b;receipt_id=s6-b-report$/)
})
await check('build wave: the executor instruction keeps only the safe negation', async () => {
  const { state, respond } = waveResponder()
  await run('forge-build-wave', waveArgs([wunit('a', ['a.ts'])]), respond)
  const text = state.admissions[0].instruction.toLowerCase()
  const stripped = text.replace(/(do not|don't|never|must not|shall not|should not|cannot|can't|without)\s+(commits|commit|merges|merge|pushes|push)(,?\s+(or|and|nor)?\s*(commits|commit|merges|merge|pushes|push))*/g, '')
  assert.ok(!/(^|[^a-z0-9_])(commit|commits|committed|merge|merges|merged|push|pushes|rebase|cherry-pick|checkpoint)([^a-z0-9_]|$)/.test(stripped), stripped)
})
await check('build wave: malformed waves are refused before any agent runs', async () => {
  const cases = [
    [[wunit('a', ['x.ts']), wunit('b', ['x.ts'])], /both declare x\.ts/],
    [[wunit('a', ['src/*.ts'])], /not an exact/],
    [[wunit('a', ['.ai/HANDOFF.md'])], /HANDOFF/],
    [[wunit('a', ['a.ts']), wunit('b', ['b.ts'], { depends_on: ['a'] })], /same wave/],
    [[wunit('a', ['a.ts'], { executor: 'claude' })], /codex-only/, { executorPolicy: 'codex-only' }],
  ]
  for (const [units, pattern, extra] of cases) {
    let ran = 0
    await assert.rejects(run('forge-build-wave', waveArgs(units, extra), async () => { ran++; return {} }), pattern)
    assert.equal(ran, 0)
  }
  await assert.rejects(run('forge-build-wave', waveArgs([wunit('a', ['a.ts'])], { baseSha: 'abc123' }), async () => ({})), /full commit SHA/)
  await assert.rejects(run('forge-build-wave', waveArgs([wunit('a', ['a.ts'])], { verdict: 'REVISE' }), async () => ({})), /APPROVED/)
})

// ---------------------------------------------------------------- recovery audit
await check('recovery audit: inventories in parallel, preserves once, checks the gate', async () => {
  const { value, calls } = await run('forge-recovery-audit', { repo: '/r', run: 'recovery-1', lanes: [{ id: 'L1', work_log: '/tmp/w', worktree: '/wt/1' }] }, async call => {
    const t = type(call)
    if (t === 'forge-liveness') return { status: 'STALLED', evidence: 'log 900s old, cpu 0' }
    if (t === 'forge-recovery-auditor') return { ledger: '/tmp/forge-recovery-recovery-1/STATE-LEDGER.md', gate_met: true, missing: [] }
    if (call.opts.label === 'gate check') return { gate_met: false, gaps: ['tip abc has no safety ref'] }
    return { items: ['x'] }
  })
  assert.equal(value.status, 'GAPS')
  assert.deepEqual(value.gaps, ['tip abc has no safety ref'])
  assert.equal(value.inventory.lanes[0].status, 'STALLED')
  assert.equal(calls.filter(c => type(c) === 'forge-recovery-auditor').length, 1)
  assert.ok(calls.filter(c => type(c) !== 'forge-recovery-auditor' && c.opts.label !== 'gate check').every(c => /Read-only|Report only/.test(c.prompt)))
})

// ---------------------------------------------------------------- agent namespace
await check('agentNamespace "" targets copied-in agents', async () => {
  const { calls } = await run('forge-plan-review', { ...reviewArgs, agentNamespace: '' }, async call =>
    call.opts.agentType ? { verdict: 'APPROVED', thread_id: 'T', reviewer: 'codex', verdict_file: '/v', findings: [] } : { accepted: [], rejected: [], plan_changed: false })
  assert.equal(calls[0].opts.agentType, 'forge-codex-reviewer')
})

console.log(results.join('\n'))
console.log(failures ? `FAIL: ${failures} of ${results.length} workflow checks` : `PASS: ${results.length} workflow checks`)
process.exit(failures ? 1 : 0)
