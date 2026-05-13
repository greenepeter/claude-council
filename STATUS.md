# trade_council - STATUS

**Project:** Autonomous FX swing-trading council. v2 of claude_council.
**Last updated:** 2026-05-11 (paper-trading registry wired)

## Current state

**v2.0 Phase 3 complete + paper-trading registry.** End-to-end autonomous loop is wired: a seed debate goes into `schedule.sqlite`, `scripts/run_autonomous.py` fires due debates, the moderator's `next_review` decision becomes a follow-up schedule row, and the loop continues. Every debate's emitted plan YAML is now auto-enrolled into a first-class paper-trading registry (`runtime/paper_trading_registry.json`) and an initial Engine cycle fires immediately at debate-end time. The autonomous loop iterates this registry on each tick instead of globbing `trade_plans/*.yaml`. Cost cap + KILL file safety rails in place.

What still gates real operation: ANTHROPIC_API_KEY in `.env` (subscription mode also works for testing, but rate limits will halt the loop). Phase 4 is the end-to-end live smoke test once the key is plugged in.

### What's built (Phase 1)

- Copy of claude_council v0.3 with non-trading roles stripped, package renamed to `trade_council`.
- `plan_generator.py`: post-debate -> paper_trader plan YAML.

### What's built (Phase 2)

- `builtin_decisions.py`: auto-prepended `should_act`, `next_review`, `confidence_level` plus guidance text embedded in moderator structured-output instruction.
- `debate.py`: orchestrator now auto-prepends built-ins and auto-fires `plan_generator` at end.
- `backends/api.py`: full anthropic SDK implementation with model alias map and per-token pricing for Sonnet 4.6 / Opus 4.6 / Haiku 4.5.
- `transcript.py`: `Transcript.from_dict()` classmethod for roundtrip from saved JSON.
- `overseer.py` + `overseers/portfolio_overseer.md`: PortfolioOverseer with deterministic pre-check (correlation + account-risk thresholds) and LLM-on-flag intervention. Soft-override veto authority.
- `formats/council.py`: accepts `overseer` kwarg, consults at each substantive decide action.
- `moderators/fx_swing_chair.md`: appended v2 addendum with built-in decision rules, default cadence table (Reading A asymmetry), plan-emission awareness, overseer awareness.

### What's built (Phase 3)

- `scheduler.py`: SQLite-backed `Scheduler` class. Per-instrument debate rows, cost ledger, status state machine (pending -> running -> completed | error | cost_capped).
- `scheduler.resolve_next_review()`: converts moderator's `next_review` value into ISO UTC timestamp. Accepts ISO datetimes, relative forms ("in 4 hours", "in 2 days", "in 6h"), and event forms ("after USD CPI") - the latter uses paper_trader's CalendarSource.
- `scripts/seed_debate.py`: CLI to insert the first row into the schedule. Bootstraps the autonomous loop.
- `scripts/run_autonomous.py`: stateless invocation - reads schedule, fires due debates, parses each moderator's next_review into a follow-up row, records cost, handles errors. Designed to be called periodically by a Windows Task Scheduler entry / Linux cron / GitHub Actions workflow.
- Cost cap (default $20/day, overridable via `COST_CAP_USD_PER_DAY` env). Excess debates marked `cost_capped` rather than firing.
- KILL file: `KILL` in project root halts the loop immediately (re-checked before each debate).

### What's built (paper-trading registry — 2026-05-11)

- `src/trade_council/paper_trader/registry.py`: `PaperTradingRegistry`, JSON-backed at `runtime/paper_trading_registry.json`. Tracks every enrolled plan with `status` (active | expired | invalidated | closed | errored), `last_cycle_at`, `last_cycle_summary`, `last_error`. Atomic tmp-then-replace writes; tolerant of corrupt files (auto-backup and start fresh).
- `src/trade_council/paper_trader/enroll.py`: `enroll_plan()` (used by `debate.py` post-debate), `enroll_all_plans_in_dir()` (backfill for stray YAMLs), `run_cycle_for_entry()` (per-tick eval used by the autonomous loop). All paths catch engine/load exceptions onto the registry instead of raising.
- `debate.py`: after `plan_generator.generate_plan()` succeeds, the emitted YAML is enrolled and one Engine cycle fires immediately. Failures are persisted to `debates/<…>/paper_trader_enroll_error.txt` and never block the debate flow.
- `scripts/run_autonomous.py::poll_paper_traders()`: iterates `registry.list_active()` rather than `trade_plans/*.yaml`. Backfills any pre-existing YAML on first poll. Plans the engine marks expired/invalidated transition their registry status and are skipped on subsequent polls.
- Avoids prior crimson-dashboard mistakes: strategy-agnostic naming (no color in module/class names), no MT5 coupling, idempotent enrollment, no silent `save_state(None)` ambiguity, self-describing state files.

### What's NOT built (Phase 4+)

- End-to-end live smoke test (seed -> let it run -> verify follow-up fires). Requires ANTHROPIC_API_KEY.
- ~~Per-plan paper_trader cycle polling inside `run_autonomous.py`~~ — DONE 2026-05-11. Each autonomous tick now runs one Engine cycle per active registry entry.
- Web search for API-mode specialists (currently `tools_allowed` is ignored on the API backend; specialists in API mode reason from the research packet only).
- Migrate from local Windows to cloud VM when 24/7 availability is required.

### Carried over from claude_council v0.3 (untouched)

- 6 FX specialists, research packet builder, council format, transcript, ClaudeCodeBackend, paper_trader subpackage (plan loader, ForexFactory calendar, yfinance prices, JSON ledger, engine, notifier).

## Outputs produced

- `trade_plans/<plan_id>.yaml` per debate (auto-emitted by `plan_generator`).
- `runtime/paper_trading_registry.json`: registry of enrolled plans with cycle status.
- `runtime/<plan_id>_ledger.json` + `runtime/<plan_id>_alerts.log` per enrolled plan.
- `schedule.sqlite` (gitignored): persistent schedule + cost ledger.
- `debates/<date>_<slug>/` per debate: transcript.md, transcript.json, decisions.md.

## OOS dates touched

Not applicable.

## Next concrete steps (Phase 4)

1. Plug ANTHROPIC_API_KEY into `.env` and switch fx_swing_chair to `mode: api` (or keep on `claude_code` if you want to use subscription auth - it works, just rate-limited).
2. Run the live smoke test:
   ```powershell
   python scripts/seed_debate.py --symbol EURUSD `
     --topic "EUR/USD weekly check-in" `
     --decisions "direction,entry,stop,target,size" `
     --schedule-for now
   python scripts/run_autonomous.py
   ```
3. Wire a Task Scheduler entry to run `run_autonomous.py` every hour.
4. (Optional) Add `paper_trader` cycle polling to `run_autonomous.py` so the engine evaluates open plans on the same hourly tick.

## Open issues

- ~~`run_autonomous.py` doesn't yet evaluate open paper_trader plans on the same poll~~ — DONE 2026-05-11 via the paper-trading registry. `scripts/run_paper_trader.py` still exists for manual one-shot CLI use, but the autonomous loop no longer subprocesses to it.
- API backend's `tools_allowed` parameter accepted but unused. Specialists in API mode see only the research packet. Wiring native web_search would be a v2.1 enhancement.
