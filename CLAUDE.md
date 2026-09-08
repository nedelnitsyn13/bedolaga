# Bedolaga — Claude Code instructions

## Project workflow

- Work only in the current repository and feature branches.
- Never modify production servers, production databases, or production configuration directly.
- For non-trivial changes, inspect the affected architecture and dependencies with Graphify before broad raw-file exploration.
- Before changing behavior, inspect existing tests for the affected component and extend them when the behavior changes.
- After changes, run the narrowest relevant tests first, then the broader test suite when practical.
- Keep payment, webhook, authentication, authorization, idempotency, and subscription/device-limit changes especially conservative.
- Do not weaken existing security checks or bypass payment validation merely to make a test pass.
- Use a feature branch and prepare a PR; do not merge or deploy unless explicitly requested.

## Graphify

Graphify is the repository architecture map. It runs in the Claude Code working environment; the production server is not part of this workflow.

If `graphify-out/graph.json` does not exist, bootstrap Graphify without requiring a global installation:

```bash
uvx --from graphifyy graphify . --code-only
```

For an existing graph, use:

```bash
uvx --from graphifyy graphify query "how does <concept> work?"
uvx --from graphifyy graphify path "<A>" "<B>"
uvx --from graphifyy graphify explain "<concept>"
```

After substantial source changes or an explicit refresh:

```bash
uvx --from graphifyy graphify . --update --no-viz
```

`graphifyy` is the official package name; the CLI command is `graphify`. Using `uvx --from graphifyy` is preferred in remote/fresh Claude Code environments because it does not require installing Graphify on the production server.

Generated Graphify data is local working state and must not be committed:

- `graphify-out/`
- generated HTML/report artifacts under `graphify-out/`

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
