# ADR format

This format is adapted from Matt Pocock's `grill-with-docs` under the MIT License.

ADRs live in `docs/adr/` by default and use sequential numbering such as
`0001-authority-boundary.md`. Preserve another repository-declared location or
naming convention. Create the directory lazily only when the first qualifying ADR
is required.

## Minimum record

```md
# {Short decision title}

{A concise paragraph recording the context, decision, and consequences, including
why the selected trade-off fits the confirmed domain model.}
```

Optional status, considered-options, or expanded consequences sections belong only
when they help a future reader. Do not turn the format into a form-filling exercise.

## ADR threshold

Offer an ADR only when all three conditions hold:

1. **Hard to reverse** — changing the choice later has meaningful cost.
2. **Surprising without context** — code alone will not explain why the choice exists.
3. **The result of a real trade-off** — credible alternatives existed and one was
   selected for specific reasons.

Qualifying examples include legal-domain ownership boundaries, cross-context
integration patterns, durable technology lock-in, constraints invisible in code,
and deliberately rejected alternatives whose rationale would otherwise be lost.
Ordinary library selection, easy-to-reverse details, and obvious implementation do
not need an ADR.

For sequential numbering, scan the configured ADR directory, increment the highest
existing number, and never renumber prior decisions.
