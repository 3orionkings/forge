// End-to-end check of forge-build-wave against the real dispatch guard. The agents are
// deterministic stand-ins that do what their instructions say (create the worktree,
// write the contract and freeze manifest, map the declaration to guard flags, edit the
// declared file), so every admission the workflow asks for is decided by
// skills/forge/dispatch-guard.sh itself. A second wave is admitted with the first
// wave's successor receipts. No model is called.
// Usage: node tests/workflow-admission.test.mjs
import { execFileSync } from 'node:child_process'
import { mkdtempSync, writeFileSync, readFileSync, realpathSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'

const ROOT = fileURLToPath(new URL('..', import.meta.url))
const SKILLS = realpathSync(join(ROOT, 'skills'))
const GUARD = join(SKILLS, 'forge', 'dispatch-guard.sh')
const AsyncFunction = (async () => {}).constructor
const sh = (cmd, args, opts = {}) => execFileSync(cmd, args, { encoding: 'utf8', ...opts })
const sha256 = path => sh('shasum', ['-a', '256', path]).split(' ')[0]
// The workflow names its temp files under /tmp; the stand-ins keep them in the test folder.
const local = (path, work) => (path.startsWith('/tmp/') ? `${work}/${path.slice(5)}` : path)
const MACHINE = { FORGE_GUARD_LOAD_1: '1', FORGE_GUARD_CORE_COUNT: '10', FORGE_GUARD_FREE_SWAP_GB: '16', FORGE_GUARD_CPU_IDLE_PCT: '80', FORGE_GUARD_MEMORY_FREE_PCT: '80' }

// forge-admission's mapping: each key is the flag of the same name, except these.
const FLAG = {
  action: '--admission-action', backlog_ids: '--backlog-id', files: '--file', current_files: '--current-file',
  active_writer_worktrees: '--active-writer-worktree', parallel_files: '--parallel-file',
}
function guardArgs(decl) {
  const out = []
  for (const [key, value] of Object.entries(decl)) {
    if (value === null || value === undefined) continue
    const flag = FLAG[key] || `--${key.replace(/_/g, '-')}`
    for (const v of Array.isArray(value) ? value : [value]) out.push(flag, String(v))
  }
  return out
}
function runGuard(decl) {
  try {
    return { ok: true, line: sh('zsh', [GUARD, ...guardArgs(decl)], { env: { ...process.env, ...MACHINE }, stdio: ['ignore', 'pipe', 'pipe'] }).trim() }
  } catch (error) {
    return { ok: false, line: `${error.stdout || ''}${error.stderr || ''}`.trim() }
  }
}

const work = realpathSync(mkdtempSync(join(tmpdir(), 'forge-wave-e2e-')))
const repo = join(work, 'repo')
sh('mkdir', ['-p', join(repo, 'src')])
sh('git', ['-C', repo, 'init', '-q', '-b', 'main'])
sh('git', ['-C', repo, 'config', 'user.name', 'Forge E2E'])
sh('git', ['-C', repo, 'config', 'user.email', 'forge-e2e@example.invalid'])
for (const f of ['a', 'b', 'c']) writeFileSync(join(repo, 'src', `${f}.ts`), `${f} one\n`)
sh('git', ['-C', repo, 'add', '-A'])
sh('git', ['-C', repo, 'commit', '-qm', 'base'])
writeFileSync(join(work, 'PLAN.md'), '# Plan\napproved\n')
writeFileSync(join(work, 'LOG.md'), 'VERDICT: APPROVED\n')

const declarations = []
function stand_in(profile, slug) {
  return async (prompt, opts) => {
    const type = (opts.agentType || 'default').replace(/^forge:/, '')
    if (opts.label === 'prepare wave') {
      const overlay = join(work, `overlay-${slug}.md`)
      writeFileSync(overlay, '# No owner project overlay; the global Forge default applies.\nexecutor_policy=any\n')
      sh('mkdir', ['-p', join(work, 'wt')])
      return { ok: true, forge_skills: SKILLS, overlay, plan: join(work, 'PLAN.md'), act2_log: join(work, 'LOG.md'), worktree_root: join(work, 'wt') }
    }
    if (type === 'default') {
      const contract = local(prompt.match(/\/tmp\/forge-contract-[^ ]+\.md/)[0], work)
      const id = opts.label.replace(/^(freeze|fix contract) /, '').split(' ')[0]
      const unitSlug = `${slug}-${id}`
      const worktree = join(work, 'wt', unitSlug)
      if (opts.label.startsWith('freeze')) {
        const branch = prompt.match(/worktree add -b (\S+)/)[1]
        sh('git', ['-C', repo, 'worktree', 'add', '-q', '-b', branch, worktree, prompt.match(/IMMUTABLE BASE SHA \(([0-9a-f]+)\)/)[1]])
      }
      writeFileSync(contract, `GOAL: change src/${id}.ts\nEXACT DECLARED FILE SET: src/${id}.ts\nPROOF: grep -q two src/${id}.ts\nThe executor never commits or merges.\n`)
      const result = { ok: true, worktree, branch: '', contract, contract_sha256: sha256(contract), work_log: join(work, `work-${unitSlug}.log`), report: join(work, `report-${unitSlug}.txt`) }
      if (profile === 'assured') {
        const listed = prompt.match(/one line each and never the manifest itself: (.+)\. Report the manifest/)[1].split(', ')
          .map(p => local(p, work))
        const manifest = join(work, `freeze-${unitSlug}-${opts.label.startsWith('fix') ? 'fix' : 'base'}.sha256`)
        writeFileSync(manifest, sh('shasum', ['-a', '256', ...listed]))
        Object.assign(result, { manifest, manifest_sha256: sha256(manifest) })
      }
      return result
    }
    if (type === 'forge-admission') {
      const decl = JSON.parse(prompt.slice(prompt.indexOf('{')))
      decl.execution_contract = local(decl.execution_contract, work)
      declarations.push(decl)
      const verdict = runGuard(decl)
      return verdict.ok ? { result: 'ADMIT', exit: 0, guard_output: verdict.line } : { result: 'REJECT', exit: 1, guard_output: verdict.line }
    }
    if (type === 'forge-claude-executor' || type === 'forge-codex-executor') {
      const id = prompt.match(/unit: (\S+)/)[1]
      const worktree = prompt.match(/worktree: (\S+)/)[1]
      const report = prompt.match(/report: (\S+)/)[1]
      writeFileSync(join(worktree, 'src', `${id}.ts`), `${id} two\n`)
      writeFileSync(report, `changed src/${id}.ts; proof exit 0\n`)
      return { status: 'DONE', thread_id: type === 'forge-codex-executor' ? `TH-${id}` : '', changed_files: [`src/${id}.ts`], proof_exit: '0' }
    }
    if (type === 'forge-unit-verifier') return { result: 'PASS', fix_list: [] }
    if (type === 'forge-codex-reviewer') return { verdict: 'APPROVED', thread_id: 'RV-1', reviewer: 'codex gpt-5.6-sol @ ultra', verdict_file: '/tmp/v', findings: [] }
    throw new Error(`unexpected agent ${type}`)
  }
}

async function wave(profile, slug, units) {
  const src = readFileSync(join(ROOT, 'workflows', 'forge-build-wave.js'), 'utf8').replace('export const meta =', 'const meta =')
  const fn = new AsyncFunction('agent', 'parallel', 'pipeline', 'phase', 'log', 'args', 'budget', 'workflow', src)
  const parallel = async thunks => Promise.all(thunks.map(t => Promise.resolve().then(t).catch(e => { console.error(e); return null })))
  const base = sh('git', ['-C', repo, 'rev-parse', 'HEAD']).trim()
  return fn(stand_in(profile, slug), parallel, null, () => {}, () => {}, {
    repo, slug, profile, baseSha: base, plan: join(work, 'PLAN.md'), act2Log: join(work, 'LOG.md'), verdict: 'APPROVED', units,
  }, { total: null, spent: () => 0, remaining: () => Infinity }, null)
}

let failures = 0
async function check(label, body) {
  try { await body(); console.log(`PASS: ${label}`) } catch (error) { failures++; console.log(`FAIL: ${label}\n  ${error.message.split('\n').slice(0, 6).join('\n  ')}`) }
}
const u = (id, executor, extra = {}) => ({ id, goal: `change ${id}`, done_condition: 'proof passes', files: [`src/${id}.ts`], proof: `grep -q two src/${id}.ts`, proof_scope: 'targeted', weight: 'LIGHT', executor, ...extra })

for (const profile of ['sprint', 'assured']) {
  let first
  await check(`${profile}: the real guard admits every lane the wave declares`, async () => {
    first = await wave(profile, `${profile}1`, [u('a', 'codex'), u('b', 'claude')])
    assert.equal(first.status, 'READY', JSON.stringify(first.units.map(x => [x.id, x.status, x.problem, x.admissions]), null, 1))
    for (const unit of first.units) assert.match(unit.admissions[0], new RegExp(`^ADMIT: action=launch profile=${profile} `))
  })
  await check(`${profile}: a successor is admitted with the first wave's receipts`, async () => {
    assert.ok(first, 'first wave did not run')
    const receipts = first.units.find(x => x.id === 'a').successor_receipts
    writeFileSync(join(repo, 'src', 'c.ts'), 'c one\n')
    const second = await wave(profile, `${profile}2`, [u('c', 'claude', { predecessor: receipts })])
    assert.equal(second.status, 'READY', JSON.stringify(second.units.map(x => [x.id, x.status, x.problem, x.admissions]), null, 1))
  })
}
await check('the same successor is refused when a non-Claude identity consumed the report', async () => {
  const successor = declarations.filter(d => d.predecessor === 'a' && d.action === 'launch').pop()
  assert.ok(successor, 'no successor declaration was captured')
  sh('git', ['-C', successor.worktree, 'restore', '--', 'src/c.ts'])
  assert.ok(runGuard(successor).ok, 'the captured declaration should admit on a clean tree')
  const forged = { ...successor, report_receipt: successor.report_receipt.replace('consumed_by=claude-forge-driver', 'consumed_by=forge-driver') }
  assert.match(runGuard(forged).line, /RECEIPT_REPORT_ORCHESTRATOR/)
})

console.log(failures ? `FAIL: ${failures} workflow-admission checks` : 'PASS: workflow-admission checks')
process.exit(failures ? 1 : 0)
