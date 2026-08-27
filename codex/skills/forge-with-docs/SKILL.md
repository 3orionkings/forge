---
name: forge-with-docs
description: Use when planning a non-trivial change in a repository whose domain glossary, context model, or ADRs must remain authoritative.
---

# Forge With Docs

Forge With Docs is the **docs-aware Act 1** variant of shared
[Forge](../forge/SKILL.md). Codex remains the requirements interviewer, plan
arbiter, and integration authority. Owner/repository rules bind; no product work.

## Load the domain canon first

Complete this load before the first owner question:

1. Read `CONTEXT-MAP.md` and each relevant referenced `CONTEXT.md`, or the root
   `CONTEXT.md`, plus any authoritative glossary or domain model.
2. Read the relevant existing ADRs, repository instructions, and the
   [context](CONTEXT-FORMAT.md) and [ADR](ADR-FORMAT.md) formats.
3. Start a **terminology ledger** containing the established term, definition,
   avoided synonyms, source document, observed code usage, proposed meaning, and
   status (`open`, `resolved`, or `rejected`).
4. First inspect code, tests, schemas, and call sites for discoverable local facts.
   Record canon/code drift instead of asking what inspection can answer.

Present canon/code drift as evidence; confirmed intent decides which side changes.

## Resolve one decision at a time

For every open requirement or terminology collision:

1. state the decision and cite the domain documents and code already inspected;
2. test it with concrete legal-domain scenarios, not only abstractions;
3. recommend one answer and explain its material tradeoff;
4. ask **one question at a time** and wait for the answer;
5. record the answer and resolve or reject the ledger entry before asking the next
   question.

Never batch unresolved decisions. Challenge reuse of an established term with a new
meaning; sharpen fuzzy terms and name preferred and avoided vocabulary.

## Keep the docs living

Update the glossary, `CONTEXT.md`, or `CONTEXT-MAP.md` as each decision settles;
never defer it all until after implementation.
Create a dated or sequential ADR only when the choice meets the threshold in
[ADR-FORMAT.md](ADR-FORMAT.md), and record its context, decision, and consequences.
Do not write speculative entries, unrelated docs, or product code. Act-1 writes are
settled domain docs, qualifying ADRs, `PLAN.md`, and `PLAN-REVIEW-LOG.md` only.

## Lock and hand off

Lock `PLAN.md` and initialize `PLAN-REVIEW-LOG.md` exactly as shared
[Acts 1–2](../forge/references/acts-1-2.md) requires, recording resolved terminology,
doc/ADR identities, acceptance, risks, and exclusions.

For the Act-2 start, pin the absolute plan and review-log controls and declare every
relevant glossary, context document, and ADR through `review_controls.domain_docs`.
Those domain declarations are a **canonical sorted unique** list of at most 64
**worktree-relative** `{path, sha256}` records. On each same-session review resume,
repeat the **same domain-document path set** with **current hashes**, together with
the current plan/review-log hashes and baseline. A changed set is a new contract.

Then **hand the locked plan unchanged into shared Acts 2–4**. The shared
[Acts 3–4](../forge/references/acts-3-4.md), [engine](../forge/ENGINE.md),
[routing policy](../forge/ROUTING-POLICY.md), and selected
[Sprint](../forge/profiles/sprint.md) or [Assured](../forge/profiles/assured.md)
profile remain the only execution authorities.

This variant **adds no gate**, **adds no approval**, **adds no routing**, **adds no
review**, **adds no build**, **adds no proof**, and **adds no commit cadence**.
It never transfers Codex integration authority or executor Git authority.

## Source

The docs-aware Act-1 method and formats are adapted from Matt Pocock's
`grill-with-docs` under the MIT License.
