
## 2026-08-06 — LIGHT lanes get the existing high-load headroom escape

**Owner-authorised, narrow.** `dispatch-guard.sh` refused every LIGHT lane on a desktop
whose load average was inflated by process count rather than compute.

Measured at the time of the refusal: load 564 on 10 cores, 391 Chrome renderer
processes with 352 in running state — yet total Chrome CPU 20.8%, no single renderer
above 6%, CPU idle 63.3%, memory free 70%. The machine had real headroom; the load
number was the wrong instrument, exactly as this script's own header anticipates.

HEAVY already had the escape: above the ceiling it may still enter if CPU idle AND
memory-free are both at or above threshold (default 50/50). LIGHT had a bare load
ceiling and no escape, so a light lane was refused on a machine a heavy lane could
enter. That asymmetry was the defect.

**Changed:** the same two-condition test now applies to LIGHT, with its own overrides
(`FORGE_GUARD_LIGHT_HIGH_LOAD_MIN_CPU_IDLE_PCT`,
`FORGE_GUARD_LIGHT_HIGH_LOAD_MIN_MEMORY_FREE_PCT`, both default 50).

**Deliberately NOT changed:** ordinary load thresholds, lane caps, overlap checks, WIP
limits, clean-tree checks, leases, swap thresholds, or any other refusal. No temporary
override was used and none is available.

**Tests:** three added — high-load LIGHT admits when both headroom conditions pass;
rejects when CPU idle is below 50; rejects when memory free is below 50. Suite: 135
cases, 0 failures. The pre-existing case "a light lane above four times core-count load"
still rejects, which is the proof that the ordinary ceiling is intact — it fails on
default idle 20%, below the 50% escape.
