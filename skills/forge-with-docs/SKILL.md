---
name: forge-with-docs
description: Use when the user wants Forge planning grounded in an existing domain model, glossary, or ADR set, with those living documents updated as decisions settle. Use plain forge when no documentation alignment is needed. Do not use for review of already-written code or trivial changes.
---

# FORGE-WITH-DOCS — Grill Against the Domain Model, Then Build

Act 1 variant: the same relentless interview as `/forge`, but stress-tested against the
project's **existing domain model and glossary** — and the docs are updated *as
decisions crystallise*, not afterwards.

## Unique to this skill

1. **Load the domain.** Read `CONTEXT.md` (or the project's equivalent glossary/domain
   model) and any ADRs before the first question.
2. **Grill with the docs in hand.** One question at a time, recommending an answer for
   each; explore the codebase rather than asking what the code can answer. Additionally:
   - challenge each proposal against **established terminology** — if the plan reuses a
     term differently, resolve it before proceeding;
   - sharpen fuzzy terms into definitions the glossary can hold;
   - stress-test with **concrete scenarios**, not abstractions;
   - cross-reference the code for drift between canon and reality.
3. **Update the docs inline.** As a decision settles, write it: new or changed glossary
   entries into `CONTEXT.md`, and a dated ADR for each contestable architectural choice
   (context · decision · consequences).
4. **Lock the plan** into `PLAN.md` exactly as `/forge` Act 1 does, then hand to Act 2
   (`/forge-review` mechanics) for adversarial review.

## Then: execution is NOT this skill's business

Acts 2–4 run exactly as in [`../forge/SKILL.md`](../forge/SKILL.md). **This skill adds
no gate of its own.** In particular it does **not** impose a routing approval step:
routing follows the active profile like everywhere else — under `sprint` the overlay or
default executor is applied automatically with one line recorded and **no Gate-2 wait**.

## Canonical references

[`../forge/ROUTING-POLICY.md`](../forge/ROUTING-POLICY.md) (routing) ·
[`../forge/profiles/`](../forge/profiles/) (cadence) ·
[`../forge/ENGINE.md`](../forge/ENGINE.md) (engine, templates, admission) ·
[`../forge/references/`](../forge/references/) (detail, on demand)


## What NOT to do

- Don't skip the doc load — grilling without the domain model is just `/forge`.
- Don't let docs drift from decisions; write them as they settle.
- Don't add a routing or commit gate here. The profile owns cadence.
- Don't restate Act-3/4 procedure.
