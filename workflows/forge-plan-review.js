export const meta = {
  name: 'forge-plan-review',
  description: 'Forge Act 2: Codex reviews PLAN.md read-only, a Claude arbiter answers each round, until APPROVED or the round cap',
  whenToUse: 'After Act 1 has locked PLAN.md and before any product code, for the cross-model plan review loop',
  phases: [
    { title: 'Review', detail: 'one read-only Codex round through forge-codex-reviewer' },
    { title: 'Arbitrate', detail: 'Claude logs the critique, decides what to act on, revises the plan' },
  ],
}

// args: { repo, slug, plan?, log?, maxRounds?, route?, agentNamespace? }
// repo is the absolute repository root; slug is the run's unique stamp, made by the
// caller (scripts cannot read the clock). agentNamespace is 'forge:' for a plugin
// install and '' when the agents were copied into ~/.claude/agents.
const A = args && typeof args === 'object' ? args : {}
if (!A.repo || !A.slug) throw new Error('forge-plan-review needs args.repo (absolute path) and args.slug')
const NS = typeof A.agentNamespace === 'string' ? A.agentNamespace : 'forge:'
const PLAN = A.plan || 'PLAN.md'
const LOG = A.log || 'PLAN-REVIEW-LOG.md'
const MAX_ROUNDS = Number.isInteger(A.maxRounds) && A.maxRounds > 0 ? A.maxRounds : 5

const REVIEW_RECEIPT = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['APPROVED', 'REVISE', 'FAILED'] },
    thread_id: { type: 'string' },
    reviewer: { type: 'string' },
    verdict_file: { type: 'string' },
    err_file: { type: 'string' },
    findings: { type: 'array', items: { type: 'string' } },
    failure: { type: 'string' },
  },
  required: ['verdict', 'thread_id', 'reviewer', 'verdict_file', 'findings'],
}

const ARBITRATION = {
  type: 'object',
  properties: {
    accepted: { type: 'array', items: { type: 'string' } },
    rejected: {
      type: 'array',
      items: {
        type: 'object',
        properties: { finding: { type: 'string' }, reason: { type: 'string' } },
        required: ['finding', 'reason'],
      },
    },
    plan_changed: { type: 'boolean' },
    improvements: { type: 'array', items: { type: 'string' } },
  },
  required: ['accepted', 'rejected', 'plan_changed'],
}

const history = []
let threadId = null
let reviewer = null
let finalVerdict = null

for (let round = 1; round <= MAX_ROUNDS; round++) {
  const receipt = await agent(
    [
      'mode: plan',
      `round: ${round}`,
      `slug: ${A.slug}`,
      threadId ? `thread_id: ${threadId}` : null,
      `plan: ${PLAN}`,
      `repo: ${A.repo}`,
      A.route ? `route: ${A.route}` : null,
      'Run exactly one review round as your instructions say and return the receipt fields.',
    ].filter(Boolean).join('\n'),
    { label: `codex review r${round}`, phase: 'Review', agentType: `${NS}forge-codex-reviewer`, schema: REVIEW_RECEIPT },
  )

  if (!receipt || receipt.verdict === 'FAILED') {
    log(`Round ${round} failed: ${receipt ? receipt.failure || 'no detail' : 'reviewer did not return'}`)
    return { status: 'FAILED', round, thread_id: threadId, failure: receipt ? receipt.failure : 'reviewer did not return', history }
  }
  if (round === 1) {
    threadId = receipt.thread_id
    reviewer = receipt.reviewer
  } else if (receipt.thread_id !== threadId) {
    // A resume that lands on another session looks exactly like a success.
    return { status: 'FAILED', round, thread_id: threadId, failure: `round ${round} reported thread ${receipt.thread_id}, expected ${threadId}`, history }
  }

  const arbitration = await agent(
    [
      `You are the Claude arbiter for Forge Act 2, working in ${A.repo}. Write no product code and touch no file except ${PLAN} and ${LOG}.`,
      `1. Append "## Round ${round} — ${receipt.reviewer}" and the full critique from ${receipt.verdict_file} to ${LOG}, verbatim.`,
      receipt.verdict === 'APPROVED'
        ? '2. The verdict is APPROVED. Do not change the plan. Return empty accepted and rejected lists, plan_changed false, and in improvements up to three bullets on what the review rounds improved.'
        : [
            '2. The verdict is REVISE. You are the final arbiter; the reviewer advises. Decide which findings are worth acting on.',
            `3. Revise ${PLAN} for the accepted findings, keeping its sections (Goal, Approach, Key decisions & tradeoffs, Risks / open questions, Out of scope).`,
            `4. Append "### Claude's response" to ${LOG}: what changed, and each rejected finding with its reason.`,
          ].join('\n'),
      `Findings this round:\n${receipt.findings.map(f => `- ${f}`).join('\n')}`,
    ].join('\n'),
    { label: `arbitrate r${round}`, phase: 'Arbitrate', schema: ARBITRATION },
  )

  history.push({ round, verdict: receipt.verdict, findings: receipt.findings, arbitration })
  if (receipt.verdict === 'APPROVED') {
    finalVerdict = 'APPROVED'
    break
  }
  if (!arbitration || (!arbitration.plan_changed && arbitration.accepted.length === 0)) {
    log(`Round ${round}: REVISE with nothing accepted; the next round re-argues the same plan`)
  }
}

if (finalVerdict === 'APPROVED') {
  const last = history[history.length - 1]
  log(`Gate 1: APPROVED after ${history.length} round(s)`)
  return {
    status: 'APPROVED',
    rounds: history.length,
    thread_id: threadId,
    reviewer,
    improvements: last.arbitration ? last.arbitration.improvements || [] : [],
    history,
  }
}

// Deadlock: never fake convergence. The driver lists each point with its
// counter-position and hands the tie to the owner.
const last = history[history.length - 1]
log(`Deadlock at ${MAX_ROUNDS} round(s); the owner breaks the tie`)
return {
  status: 'DEADLOCK',
  rounds: history.length,
  thread_id: threadId,
  reviewer,
  unresolved: last ? last.findings : [],
  claude_positions: last && last.arbitration ? last.arbitration.rejected : [],
  history,
}
