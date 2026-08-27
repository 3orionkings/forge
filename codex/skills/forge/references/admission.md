# Admission and atomic reservation

Admission maps to the existing executable
[`scripts/dispatch-guard.sh`](../../../scripts/dispatch-guard.sh) and the
atomic `forge_start` admission and reservation implemented by the bridge. The guard's
`ADMIT` receipt plus durable reservation is enforcement; **prose is not enforcement**.

Do not run a provider first and validate later. Do not treat a hand-authored
declaration, successful preflight, or native model promise as admission.

## Atomic boundary

For an external Claude lane, the bridge:

1. resolves idempotent replay before creating work;
2. holds the private cross-process admission lock;
3. reads all nonterminal durable reservations;
4. revalidates repository identity, exact baseline, dirty state, controls, receipts,
   file ownership, lane count, heavy lease, and machine headroom;
5. executes a pinned snapshot of the guard with a scrubbed environment;
6. accepts only one canonical `ADMIT:<json>` line, exit zero, and no stderr;
7. publishes job, attempt, admission evidence, and reservation in one transaction;
8. releases the lock, then dispatches the detached worker.

A rejection is `REJECT: CODE: reason` with nonzero exit and launches nothing. Never
override, edit around, or race a rejection.

## Shared start declaration

Every structured admission requires:

| Field | Contract |
|---|---|
| `weight` | `LIGHT` or `HEAVY` |
| `proof_scope` | `targeted` or `broad`; the proof content must agree |
| `execution_profile` | `sprint` or `assured` |
| `proof_argv` | nonempty structured argument array; never a shell string |
| `files` | exact repository-relative paths, maximum 256; no duplicates or globs |

The bridge derives runtime work-log/report paths, active lanes, heavy leases, active
writers, parallel files, WIP count, repository identity, and machine metrics. Callers
do not get to under-report those facts.

Review admission reserves no product files and carries none of the product-only
fields below. The executable product role in this release is build.
**Automated recovery is disabled** before admission can create a row or reservation.

## Product declaration

A product unit also declares:

- `base_sha`: the full exact current `HEAD`;
- `verdict`: exact `APPROVED` from Gate 1;
- nonempty `backlog_ids` including the unit;
- `controls`: pinned `{path, sha256}` artifacts;
- `oracle`: exactness plus capability receipt only when exactness is required;
- optional `predecessor` report/consumption/review receipts.

All product lanes require exact files. Build requires a clean repository. Initial
Act-1 to Act-2 review always starts from the two distinct pinned plan/review-log
controls and may carry only its pinned review controls as dirty paths; later review
resumes may evolve only those declared same-path controls, and unrelated dirt fails
closed. Historical recovery declarations and `preserved_paths` are recognized only
for fail-closed compatibility; they cannot launch, resume, steer, or cancel work.
Preserve existing evidence unchanged and use a manual handoff into a newly admitted,
clean build unit when the owner authorizes it.
`.ai/HANDOFF.md` is Codex-driver owned and cannot be reserved by an executor lane.

### Review control declaration

A role=`review` start may declare `review_controls` with the two required absolute
pins `plan` and `review_log`, plus optional `domain_docs`. `domain_docs` is a
canonical sorted unique list of at most 64 `{path, sha256}` entries. Each path is an
exact worktree-relative regular file: no absolute path, traversal, glob, case alias,
symlink component, or multiple hard link. Every domain document must be distinct by
path and inode from every other domain document and from the plan/review log.
The plan, review log, and every domain document must each be a single-link regular
file. Every raw path component must exactly match its directory entry's case and
Unicode spelling; a normalization-insensitive filesystem lookup is not canonical
authority.

Use domain documents only for settled, review-governing context such as a glossary,
context brief, or accepted ADR. They are immutable read-only controls, never product
files, writable outputs, or objective-progress artifacts. Their full pins are bound
into the guard input, admission evidence, attempt context, worker output boundary,
and terminal admission-hash receipt. The worker binds each captured output-boundary
fact back to its admitted device, inode, owner, mode, content hash, and one-link
state, then repeats those checks in the immediate pre-provider-spawn callback.

The observed dirty set may be any subset of the complete pinned review-control set;
undeclared dirt fails before a job or child attempt is published. A review refreeze
must repeat the exact same plan, review-log, and domain-document path set. Content
and hashes may evolve only through that typed refreeze; missing, changed-pin,
symlinked, aliased, added, removed, reordered, or over-bound documents fail closed.
An absent `domain_docs` field remains the compatible plan/review-log-only contract.

## Profile-specific controls

Sprint product controls are exactly:

- plan;
- Act-2 review receipt/log;
- execution contract;
- project overlay.

Assured adds:

- engine contract;
- routing policy;
- liveness fixture;
- watchdog fixture and watchdog tests fixture;
- freeze manifest containing every required control exactly once.

Controls are exact pinned artifacts, distinct by path and inode, and outside the
executor's declared product files. An exact-output oracle additionally requires a
validated disposable capability receipt; the disposable target is outside the
product worktree. Do not claim an output mechanism can satisfy a byte/hash oracle
without that receipt.

## Global invariants

The runtime preserves:

- maximum **three active lanes** across Forge, not per model;
- **one machine-wide heavy lease** for broad proof/build/gate work;
- **one writer per worktree**;
- **disjoint file ownership** across concurrent lanes;
- exact immutable baseline and clean/declared dirty state;
- active profile semantics;
- executor instruction free of commit, merge, push, rebase, checkpoint, publish, or
  other mutating Git authority;
- report-consumption and, under Assured, cross-model review receipts before succession;
- an **execution receipt** binding route, proof, artifacts, admission, and terminal
  outcome.

The guard rejects broad proof declared targeted, broad proof assigned LIGHT weight,
a fourth lane, a second heavy lease, duplicate worktree writer, file overlap,
insufficient machine headroom, product work without `APPROVED`, invalid controls or
receipts, review-domain documents outside a review lane, and executor Git authority.

## Proof and weight classification

A named file/path/pattern proof may be targeted and LIGHT when its actual resource use
fits that declaration. Whole-suite tests, broad lint/typecheck, production build,
coverage, database rebuild, and integrated gate are broad and HEAVY. A label cannot
make a broad command targeted. When uncertain, freeze the real command first and let
the guard classify/reject rather than weakening the declaration.

## Predecessors and receipts

Both profiles require a dependent unit to consume its predecessor's nonempty report
through a pinned receipt. Assured also requires a review artifact whose reviewer is
from the other model family and whose verdict is `APPROVED`. Sprint does not invent a
per-unit review receipt; if one is supplied, it still must be genuine.

The report-consumption receipt means the successor actually read the predecessor's
contract, not merely that the file exists. Each receipt identifies unit, builder,
consumer/reviewer, exact artifact, and receipt ID.

## Codex-native lanes

The MCP bridge controls Claude only. Before spawning a native Codex subagent, Codex
must use the bounded local
[`scripts/forge-native-lane.py`](../../../scripts/forge-native-lane.py) `reserve`
operation. It executes the same dispatch guard inside the same private admission lock
and SQLite transaction used by bridge starts. The single `lane_reservations` ledger
therefore counts native and Claude work together; there is no second prose ledger and
no double counting.

Construct `--guard-request` from the frozen unit before calling `reserve`. It is an
owner-controlled absolute regular mode-`0600`, single-link file containing a bounded
JSON array of alternating flag/value strings. Include canonical `--worktree`, exact
`--base-sha`, `--lane-type`, `--weight`, the complete repeated `--file` set, and all
profile, proof, control, receipt, instruction, work-log, and report flags required by
the dispatch guard. Do not put active-lane counts, other writers/files, WIP, or
machine facts under caller authority.

The native controller derives repository identity, baseline, lane, weight, writer,
and files from that private artifact. The matching CLI fields are expected assertions
only; any omission, alias, symlink, or mismatch is rejected before a reservation row
is published. The real guard returns the same canonical scheduler binding in its
hash-bound `ADMIT` result, and the controller requires it to equal the derived
reservation. Inside the shared admission lock and transaction, the controller
replaces scheduler-active facts and supplies trusted WIP and machine probes. The
guard-request digest is part of reserve/expand idempotency identity.

Native recovery lanes are unsupported by both `reserve` and `expand`, and automated
bridge recovery is also disabled in this release. A mutable worktree has no atomic
filesystem snapshot that can be committed with the scheduler's SQLite transaction;
repeated path recapture is not enforcement. Preserve it unchanged and use an
owner-approved manual handoff into a freshly admitted clean normal feature worktree.
Ordinary clean native feature admission and expansion are unchanged.

Always reserve before native delegation and persist the owner-private receipt. It is a
mode-`0600`, single-link capability file that survives a Codex task restart. The raw
owner token never appears in `list`, logs, status, or command arguments. Use the
receipt to heartbeat, expand, or release. A stale native lease remains counted and
continues blocking overlap/capacity until authenticated release or explicit manual
owner action; age alone never grants ownership.

After the native subtree and every descendant are quiescent, use `release`. Release
after quiescence, not merely after the root reports completion. Any Claude review or
execution associated with the unit still goes through
[bridge-tools.md](bridge-tools.md).

## Guarded same-lane expansion

Expansion is an exceptional re-admission, not ordinary steering. For an active
Claude job, `forge_steer` carries a revision-checked `admission_expansion`; for a
native lane, use `scripts/forge-native-lane.py expand` with its private receipt.
Both paths declare the **prior exact file set** and the **full exact file set**.
Native expansion uses a fresh guard request constructed the same way, with the same
repository, baseline, lane, weight, and writer plus the full strict-superset repeated
`--file` set. Its `--prior-file` arguments must exactly equal the current receipted
set; neither caller assertions nor an earlier request may fabricate the new binding.

The shared transaction requires a strict superset, the same lease, repository,
baseline, lane, writer, and weight; checks dirty-path containment; rechecks every
other lane for overlap; and publishes a fresh `ADMIT` receipt before the expanded
instruction can take effect. A same-weight expansion retains an existing HEAVY lease
without acquiring a second lease. Exact replay is idempotent; a stale divergent
revision is refused.

Expansion cannot change proof scope, route, budget, permissions, profile, or weight.
A **proof or weight escalation** is a separate frozen unit and full re-admission, not
an expansion. Do not use expansion to adopt an undeclared dirty path or union two
racing requests.

## Rejection response

On rejection, record the exact code and reason. Change only the cause: reduce
parallelism, release a heavy lease, correct the baseline/files/controls/receipts,
repair the proof declaration, or return to planning. Re-run admission from fresh
state. Never edit product code before a later `ADMIT`.
