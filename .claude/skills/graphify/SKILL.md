---
name: graphify
description: "Use for non-trivial questions about the codebase, architecture, file relationships, dependencies, or cross-module behavior. Prefer the repository knowledge graph before broad source exploration."
---

# Graphify

Graphify is an architecture/dependency map for this repository. It is a navigation aid, not proof of correctness or security.

## Bootstrap in Claude Code

Claude Code may run in a fresh remote environment where the `graphify` executable is not installed. Do not assume the production server has it.

If `graphify` is unavailable, use the isolated runner supplied by the official package:

```bash
uvx --from graphifyy graphify --version
```

Use the same `uvx --from graphifyy graphify` prefix for Graphify commands in a fresh environment. Do not install Graphify on a production server just to make this repository skill work.

## First build

If `graphify-out/graph.json` does not exist, build a code-only graph from the repository root:

```bash
uvx --from graphifyy graphify . --code-only
```

A code-only graph does not require an API key.

## Existing graph

When `graphify-out/graph.json` exists, use the graph before broad raw-file exploration for architecture or dependency questions:

```bash
uvx --from graphifyy graphify query "<question>"
uvx --from graphifyy graphify path "<A>" "<B>"
uvx --from graphifyy graphify explain "<concept>"
```

Do not rebuild the graph for every ordinary question.

## Updating the graph

After substantial source changes, or when explicitly asked to refresh the architecture map:

```bash
uvx --from graphifyy graphify . --update --no-viz
```

## Project integration

The Graphify skill is committed to this repository so Claude Code can discover it from GitHub. The generated graph is intentionally local working state and must not be committed:

- `graphify-out/`
- generated HTML/report artifacts under `graphify-out/`

## Security-sensitive changes

For payment, YooKassa/webhook, balance, subscription renewal, authentication, authorization, idempotency, or device-limit changes:

1. Use Graphify to trace affected modules and dependencies before editing.
2. Verify important conclusions in source code and tests.
3. Check idempotency, replay behavior, authorization, ownership, and server-side amount validation.
4. Add regression tests for changed failure/retry paths.
