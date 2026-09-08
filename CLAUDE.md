# Bedolaga — Claude Code instructions

## Project workflow

- Work only in the current repository and feature branches.
- Never modify production servers, production databases, or production configuration directly.
- For non-trivial changes, first inspect the affected architecture and dependencies with Graphify when `graphify-out/graph.json` exists.
- Prefer `graphify query`, `graphify path`, and `graphify explain` for cross-module architecture questions instead of broad raw-file searches.
- Before changing behavior, inspect existing tests for the affected component and extend them when the behavior changes.
- After changes, run the narrowest relevant tests first, then the broader test suite when practical.
- Keep payment, webhook, authentication, authorization, idempotency, and subscription/device-limit changes especially conservative.
- Do not weaken existing security checks or bypass payment validation merely to make a test pass.
- Use a feature branch and prepare a PR; do not merge or deploy unless explicitly requested.

## Graphify

Graphify is the repository architecture map. If `graphify-out/graph.json` exists, use it before exploring a large or cross-module part of the codebase.

Typical commands:

```bash
graphify query "how does <concept> work?"
graphify path "<A>" "<B>"
graphify explain "<concept>"
graphify . --update --no-viz
```

If Graphify is not installed in the current environment, install the official package with:

```bash
uv tool install graphifyy
```

The CLI command is `graphify`.

Generated Graphify data is local working state and must not be committed:

- `graphify-out/`
- `.claudeignore` should exclude `graphify-out/` from Claude context when present.

## Python / tests

- Python target: 3.13.
- Dependencies are locked with `uv.lock`.
- Use `uv run ...` for project commands.
- Test suite: `uv run pytest -q`.
- PostgreSQL tests: `uv run pytest -m postgres -q`.
- Lint: `uv run ruff check .`.

## Security-sensitive areas

For payment, YooKassa/webhook, balance, subscription renewal, device-limit, authentication, admin authorization, or external API changes:

1. Trace all callers and downstream side effects.
2. Check idempotency and replay behavior.
3. Validate server-side amounts and ownership; never trust client-supplied payment state.
4. Preserve authorization and signature checks.
5. Add regression tests for duplicate delivery, invalid input, and failure/retry paths where applicable.
