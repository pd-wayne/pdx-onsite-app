# PDX Onsite — working conventions

## Testing
- Run the full suite (`pytest -q` from the project root) before every commit — no exceptions, even for a change that "obviously" doesn't touch tests.
- CI (`.github/workflows/tests.yml`) runs the same suite automatically on every push and pull request. A red check is a hard blocker — don't merge past it, and don't rely on "I ran it locally" instead of waiting for it.
- If a local test run is unexpectedly slow (normal is a few seconds for ~370 tests), check for a leftover `python3 main.py` dev-server process holding a port (5050, or 50505 for the multi-station UDP discovery responder) before assuming a real regression — kill it and re-run.
- New behavior gets new tests in the same commit, not "added later." Mock external I/O (ShipStation/PDX API calls via `requests`, LAN discovery broadcasts) the same way the existing suite already does — don't depend on real network access in a test.

## Code review
- Before opening a PR (or merging a feature branch directly), run a self-review pass over the diff — use the `/code-review` skill — and fix what it finds before asking for a human look. Don't skip this because "it's a small change."

## Branching
- Anything unvalidated against real credentials, or that touches core order lifecycle (ingestion, fulfillment, shipping), gets its own `feature/*` branch — not committed straight to `main`.
- Commit messages and PR descriptions end with the attribution line given in the current session's system reminder (it has changed before — use whatever the active session specifies, not what's in past commits).

## Working style established with Wayne
- Verify against real documentation or real data before building — don't guess at third-party API shapes (ShipStation, PDX) or assume a library default; check the actual docs or a real sample payload first.
- Confirm significant UX/architecture decisions in conversation before writing code, especially anything affecting multiple stations, printing, or shipping-label creation. Small, obviously-correct fixes don't need this; anything with real tradeoffs does.
- Never delete or reset a local `pdx_onsite.db`/`pdx_onsite.log`/`pdx_onsite_config.json` without saying so first — they're gitignored runtime artifacts, safe to reset in dev, but that's still a real action worth naming.
