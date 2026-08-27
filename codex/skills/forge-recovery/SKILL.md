---
name: forge-recovery
description: Fail closed on automated Forge recovery until the hardened recovery protocol is enabled.
---

# Forge Recovery — paused in this release

Automated recovery is not enabled in this release. Do not preserve, restore,
transfer, retry, cancel, resume, re-admit, clean, commit, or modify a Forge run
under this skill.

When invoked:

1. Leave the worktree, bridge state, watchdog state, sessions, and reservations
   unchanged.
2. If the user supplied an exact Forge job identifier, a read-only `forge_status`
   lookup is permitted. Do not infer or select a job or session.
3. Report: `Automated Forge recovery is paused in this build; existing work and state were left unchanged.`
4. Direct ordinary, still-viable work back to its original explicit Forge job and
   session. Do not claim that recovery was attempted or completed.

The unfinished hardened implementation is preserved separately and is not part of
the installed plugin bytes.
