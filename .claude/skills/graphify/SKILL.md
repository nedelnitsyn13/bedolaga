---
name: graphify
description: "Use for any question about a codebase, its architecture, file relationships, or project content — especially when graphify-out/ exists, where the question should be treated as a graphify query first. Turns any input into a persistent knowledge graph with god nodes, community detection, and query/path/explain tools."
---

# Graphify

Use the official Graphify CLI to inspect repository architecture before broad code exploration.

## Existing graph

If `graphify-out/graph.json` exists in the repository root and the user is asking an architecture/codebase question, start with:

```bash
graphify query "<question>"
```

Use:

```bash
graphify path "<A>" "<B>"
graphify explain "<concept>"
```

for dependency paths and focused node explanations.

Do not rebuild the graph for ordinary questions when `graphify-out/graph.json` already exists.

## Rebuild / update

For explicit refreshes or after substantial repository changes:

```bash
graphify . --update --no-viz
```

For a first build:

```bash
graphify . --code-only
```

Graphify is structural and does not require an API key for code-only extraction.

## Working rules

- Treat Graphify as an architecture map, not as proof that an implementation is correct or secure.
- Verify important conclusions in the source code and tests.
- For payment, webhook, authentication, authorization, idempotency, subscription, and device-limit changes, use Graphify to trace affected modules before editing.
- Do not commit generated `graphify-out/` artifacts.
