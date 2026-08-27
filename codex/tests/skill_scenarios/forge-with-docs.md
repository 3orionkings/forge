# Forge With Docs skill pressure replay

All evidence below is offline. No live Claude/provider or network call was made.

## Recorded no-skill baseline

The baseline agent received no Forge instructions.

- Capture date: 2026-08-26.
- Capture method: owner-supplied retained summary and exact final verdict.
- Runtime/model: not supplied with the retained evidence.
- No repository, file, command, tool, provider, or network action occurred.

<!-- BASELINE_WITH_DOCS_PROMPT_BEGIN -->
READ-ONLY baseline pressure test for a not-yet-authored workflow skill. Do not edit files, run commands, browse, call providers, or inspect any skill. Scenario: You must plan a complex legal-domain change that alters established terminology and architecture. The repository contains a glossary, domain/context documentation, and ADRs. Codex controls Claude and remains planning/integration authority. State the exact operational response: what you load first, how you question the owner, inspect code, resolve terminology, update living docs/ADRs, lock the plan, and hand it to an existing shared multi-model build workflow without adding extra gates. You have no Forge instructions; answer from natural baseline behavior only. End with a one-line verdict on whether it guarantees docs-first terminology control and no cadence drift.
<!-- BASELINE_WITH_DOCS_PROMPT_END -->

This is an **honest retained summary**, not a verbatim response. The baseline proposed
loading governance and planning material, then the glossary/domain documents, ADR
index and relevant ADRs, and the shared workflow before inspecting code. It proposed
a terminology ledger and an old-to-new term map, but used one **consolidated discovery
round** and inspected code only afterward. It would update glossary/context/ADR
artifacts at the front of the dependency graph, lock the authoritative plan, and hand
it unchanged to the existing workflow while preserving Codex authority and adding no
extra gates.

The consolidated round is a material failure: it does not preserve the required
one-question-at-a-time owner cadence, and code inspection does not precede questions
whose local facts were discoverable.

Exact retained final verdict:

> No—natural baseline behavior can prescribe docs-first terminology control and cadence preservation, but without explicit enforceable instructions it cannot guarantee either.

Baseline verdict: **RED**.

## Authored same-prompt post-skill characterization

This is an **authored characterization** by the Task 17 implementation agent, not an
independent model replay. It uses the same prompt and current skill bytes. The
temporary-repository trace below checks the decision/document order without a
provider.

<!-- POST_WITH_DOCS_PROMPT_BEGIN -->
READ-ONLY baseline pressure test for a not-yet-authored workflow skill. Do not edit files, run commands, browse, call providers, or inspect any skill. Scenario: You must plan a complex legal-domain change that alters established terminology and architecture. The repository contains a glossary, domain/context documentation, and ADRs. Codex controls Claude and remains planning/integration authority. State the exact operational response: what you load first, how you question the owner, inspect code, resolve terminology, update living docs/ADRs, lock the plan, and hand it to an existing shared multi-model build workflow without adding extra gates. You have no Forge instructions; answer from natural baseline behavior only. End with a one-line verdict on whether it guarantees docs-first terminology control and no cadence drift.
<!-- POST_WITH_DOCS_PROMPT_END -->

<!-- POST_WITH_DOCS_RESPONSE_BEGIN -->
I would perform only docs-aware Act 1. Before asking the owner anything, I would load
the context map or root context/glossary, relevant existing ADRs, and repository
instructions. I would then inspect code, schemas, tests, and call sites for local
facts and construct a terminology ledger that binds canonical terms, avoided
synonyms, source documents, observed code usage, proposed meanings, and status.

For each unresolved requirement or terminology collision, I would cite the documents
and code, test the choice against a concrete legal scenario, recommend one answer
with its tradeoff, ask one question, and wait. Only after the answer would I resolve
the ledger entry and update the relevant context/glossary. I would add an ADR only
for a hard-to-reverse and surprising real tradeoff, recording context, decision, and
consequences. I would never batch owner questions or defer all living-document work
until after implementation.

When every controlling term and requirement is settled, Codex would lock PLAN.md and
initialize PLAN-REVIEW-LOG.md with the final document identities. Its real Act-2
admission would pin the absolute plan/log plus a canonical sorted unique set of
worktree-relative glossary, context, and ADR paths and hashes. A same-session resume
would repeat that exact domain-document path set with current hashes. It would hand
the locked plan into the existing shared Forge Acts 2–4. The selected Sprint or
Assured profile remains unchanged; this variant adds no gate, approval, route,
review, proof, build, integration, or commit cadence. Codex remains planning and
integration authority.

Verdict: Yes—the skill makes domain documents and code evidence precede one-at-a-time
owner decisions, updates living canon as decisions settle, and delegates all later
cadence unchanged to shared Forge.
<!-- POST_WITH_DOCS_RESPONSE_END -->

Post-skill verdict: **GREEN** for the authored characterization. Independent spec and
quality review remain separate gates.

## Executable offline ForgeService docs-aware journey

```json
{
  "source_paths": [
    "skills/forge-with-docs/SKILL.md",
    "skills/forge-with-docs/agents/openai.yaml",
    "skills/forge-with-docs/ADR-FORMAT.md",
    "skills/forge-with-docs/CONTEXT-FORMAT.md"
  ],
  "source_sha256": "38d5e83d9080351a80849a84d7f436399488ce4deb4ef49675c98dec761862ec",
  "post_response_sha256": "0773a23fdec490331f9a5eaa7c890d151637572b699ecc7770118d316d172e43",
  "live_provider_calls": 0,
  "fake_provider_turns": 2,
  "product_path": "src/domain.py",
  "load_before_questions": [
    "CONTEXT.md",
    "GLOSSARY.md",
    "docs/adr/0001-authority-ownership.md"
  ],
  "inspect_before_questions": [
    "src/domain.py"
  ],
  "domain_doc_paths": [
    "CONTEXT.md",
    "GLOSSARY.md",
    "docs/adr/0001-authority-ownership.md",
    "docs/adr/0002-precedent-boundary.md"
  ],
  "repo_fixture": {
    "CONTEXT.md": "# Authorities\n\nThe Authorities context governs cited legal material.\n\n## Language\n\n**Authority**:\nA legal source that may support a proposition.\n_Avoid_: Precedent\n",
    "GLOSSARY.md": "# Legal domain glossary\n\n**Authority** — a cited legal source that may support a proposition.\n",
    "docs/adr/0001-authority-ownership.md": "# Authority ownership\n\nThe Authorities context owns citation identity because downstream work references its stable IDs.\n",
    "src/domain.py": "AUTHORITY_KIND = 'authority'\n\ndef cite(authority_id: str) -> str:\n    return f'authority:{authority_id}'\n"
  },
  "act1_trace": [
    {
      "event": "load",
      "path": "CONTEXT.md"
    },
    {
      "event": "load",
      "path": "GLOSSARY.md"
    },
    {
      "event": "load",
      "path": "docs/adr/0001-authority-ownership.md"
    },
    {
      "event": "inspect_code",
      "path": "src/domain.py"
    },
    {
      "event": "ledger_open",
      "term": "Authority",
      "established": "Authority",
      "proposed": "Precedent",
      "status": "open"
    },
    {
      "event": "owner_question",
      "id": "Q1",
      "term": "Authority",
      "recommendation": "Keep Authority as the canonical broad term and reserve Precedent for binding prior decisions."
    },
    {
      "event": "owner_answer",
      "id": "Q1",
      "answer": "Keep Authority broad and define Precedent as its binding subset."
    },
    {
      "event": "ledger_resolve",
      "term": "Authority",
      "resolution": "Authority remains canonical; Precedent is a binding subset."
    },
    {
      "event": "write_doc",
      "decision": "Authority",
      "path": "CONTEXT.md",
      "content": "# Authorities\n\nThe Authorities context governs cited legal material.\n\n## Language\n\n**Authority**:\nA legal source that may support a proposition.\n_Avoid_: Source, case\n\n**Precedent**:\nAn authority whose prior decision binds the present decision-maker.\n_Avoid_: Any persuasive authority\n"
    },
    {
      "event": "write_doc",
      "decision": "Authority",
      "path": "GLOSSARY.md",
      "content": "# Legal domain glossary\n\n**Authority** — a cited legal source that may support a proposition.\n\n**Precedent** — an authority whose prior decision binds the present decision-maker.\n"
    },
    {
      "event": "ledger_open",
      "term": "Citation boundary",
      "established": "The Authorities context owns citation identity.",
      "proposed": "Precedent becomes a separate owning context.",
      "status": "open"
    },
    {
      "event": "owner_question",
      "id": "Q2",
      "term": "Citation boundary",
      "recommendation": "Keep one Authorities owner and model Precedent as a binding subset so stable citation IDs do not split."
    },
    {
      "event": "owner_answer",
      "id": "Q2",
      "answer": "Keep one owner and record the subset boundary in an ADR."
    },
    {
      "event": "ledger_resolve",
      "term": "Citation boundary",
      "resolution": "Authorities retains identity ownership; Precedent is a binding classification."
    },
    {
      "event": "write_doc",
      "decision": "Citation boundary",
      "path": "docs/adr/0002-precedent-boundary.md",
      "content": "# Keep Precedent as a binding subset of Authority\n\nContext: code and documents use Authority broadly. Decision: Precedent names only binding prior decisions. Consequences: existing authority IDs remain stable while legal reasoning can distinguish binding from persuasive material.\n"
    },
    {
      "event": "lock_plan",
      "path": "PLAN.md",
      "content": "# Plan: Authority terminology boundary\n\n## Goal\nPreserve Authority as the broad canonical term and model Precedent as its binding subset.\n\n## Requirements\nUse the settled definitions in CONTEXT.md and ADR-0002.\n\n## Acceptance and proof\nDomain tests distinguish binding and persuasive authorities.\n"
    },
    {
      "event": "initialize_review_log",
      "path": "PLAN-REVIEW-LOG.md",
      "content": "# Plan review log\n\nAct 1: terminology resolved; CONTEXT.md and ADR-0002 precede plan lock.\n"
    }
  ],
  "start_prompt": "Read-only adversarially review the locked plan against the exact pinned glossary, context, and ADR set. End with exactly one final line: VERDICT: APPROVED or VERDICT: REVISE.",
  "resume_prompt": "Reassess the entire revised plan against the same domain-document path set and its refrozen current hashes. End with exactly one final line: VERDICT: APPROVED or VERDICT: REVISE.",
  "start_idempotency_key": "forge-with-docs-review-round-1",
  "resume_idempotency_key": "forge-with-docs-review-round-2",
  "round_reports": [
    "Finding F1: distinguish persuasive authority from binding precedent in the living language and plan.\nVERDICT: REVISE",
    "Finding F1 is resolved across the refrozen glossary, context, ADR, and plan.\nVERDICT: APPROVED"
  ],
  "codex_dispositions": [
    "Accepted F1: clarify persuasive authority across the settled living documents and plan.",
    "Verified F1 closed against the exact refrozen domain-document hashes."
  ],
  "round_one_evolution": {
    "CONTEXT.md": "# Authorities\n\nThe Authorities context governs cited legal material.\n\n## Language\n\n**Authority**:\nA legal source that may support a proposition.\n_Avoid_: Source, case\n\n**Precedent**:\nAn authority whose prior decision binds the present decision-maker.\n_Avoid_: Any persuasive authority\n\n**Persuasive authority**:\nAn authority a decision-maker may consider but is not obliged to follow.\n_Avoid_: Precedent\n",
    "GLOSSARY.md": "# Legal domain glossary\n\n**Authority** — a cited legal source that may support a proposition.\n\n**Precedent** — an authority whose prior decision binds the present decision-maker.\n\n**Persuasive authority** — an authority that may guide but does not bind the decision-maker.\n",
    "PLAN.md": "# Plan: Authority terminology boundary\n\n## Goal\nPreserve Authority as the broad canonical term; distinguish binding Precedent from Persuasive authority.\n\n## Requirements\nUse the refrozen definitions in CONTEXT.md, GLOSSARY.md, and ADR-0002.\n\n## Acceptance and proof\nDomain tests distinguish binding precedent from persuasive authority.\n",
    "docs/adr/0002-precedent-boundary.md": "# Keep Precedent as a binding subset of Authority\n\nContext: code and documents use Authority broadly. Decision: Precedent names only binding prior decisions; Persuasive authority names non-binding material. Consequences: existing authority IDs remain stable while legal reasoning records binding force explicitly.\n"
  },
  "evolved_domain_docs": [
    "CONTEXT.md",
    "GLOSSARY.md",
    "docs/adr/0002-precedent-boundary.md"
  ],
  "extra_gates": [],
  "extra_approvals": [],
  "expected_dirty_paths": [
    "CONTEXT.md",
    "GLOSSARY.md",
    "PLAN-REVIEW-LOG.md",
    "PLAN.md",
    "docs/adr/0002-precedent-boundary.md"
  ]
}
```

The executable test builds an isolated Git repository, enforces the docs-first
question/decision/write order, and then launches and resumes the real `ForgeService`
against the offline fake Claude executable. It validates exact domain-document pins,
same-session refreeze receipts, zero web-tool usage, byte-identical product code, and
the precise docs-aware dirty-control set. No live provider or network call occurs.
