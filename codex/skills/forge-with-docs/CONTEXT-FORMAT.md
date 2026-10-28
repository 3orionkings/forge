# CONTEXT.md format

This format is adapted from Matt Pocock's `grill-with-docs` under the MIT License.

## Single-context repository

Use one root `CONTEXT.md`:

```md
# {Context name}

{One or two sentences describing why this legal or business context exists.}

## Language

**Authority**:
One or two sentences defining the term in this project's legal or business domain.
_Avoid_: Precedent, source, case
```

Be opinionated: choose one canonical term and list competing vocabulary under
`_Avoid_`. Keep each definition to one or two sentences and define what the concept
is. Include only concepts specific to the project's legal or business domain, not
general programming terms.

## Multiple contexts

Use a root `CONTEXT-MAP.md` to identify each context, its document, and its
relationships:

```md
# Context Map

## Contexts

- [Authorities](./src/authorities/CONTEXT.md) — governs cited legal materials
- [Evidence](./src/evidence/CONTEXT.md) — governs facts and supporting material

## Relationships

- **Evidence → Authorities**: disputed facts generate authority-research questions
```

If `CONTEXT-MAP.md` exists, read it first and follow its links. If only a root
`CONTEXT.md` exists, use the single-context form. If neither exists, create a root `CONTEXT.md` lazily only when the first domain term is settled. When the applicable
context cannot be inferred from documents and code, ask one owner question.

Group terms under descriptive headings only when natural clusters exist. Preserve
the repository's established format when it already expresses the same information.
