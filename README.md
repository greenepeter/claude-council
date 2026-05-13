# trade_council

Autonomous FX swing-trading council. v2 of `claude_council`, specialized for trading: each debate produces a paper_trader plan YAML, the orchestrator schedules follow-up debates per the moderator's decision, and a portfolio overseer enforces cross-debate risk limits.

## What it does

1. **Pre-debate research packet** - a Claude turn with WebSearch + WebFetch builds a market-state briefing (spot, rates, calendar, vol, positioning).
2. **Structured debate** - 6 FX specialists (macro, sentiment, intermarket, technical, vol, risk) debate the topic, chaired by `fx_swing_chair`.
3. **Decisions committed** - chair commits decisions one at a time, asking follow-ups when needed. Decisions include direction, entries, stops, targets, sizing, event gates, plus meta-decisions like `should_act` and `next_review`.
4. **Plan emission** *(NEW in v2)* - a plan-writer agent converts the transcript + decisions into a YAML matching the `paper_trader` schema. Lands in `trade_plans/`.
5. **Paper execution** - the `paper_trader.engine` polls ForexFactory + yfinance, evaluates entries/stops/targets per the plan, writes fills to a JSON ledger.
6. **Reconvene scheduling** *(Phase 3)* - moderator's `next_review` decision becomes a row in `schedule.sqlite`. The autonomous runner fires the follow-up debate at that time. Time-based or event-based.
7. **Portfolio overseer** *(Phase 2)* - silent observer with veto power. Reads the live ledger; only speaks if a debate's proposed action would violate cross-debate correlation or account-risk caps.

## Quick start (Phase 1 - manual debate)

```powershell
cd D:\trade_council
pip install -r requirements.txt

python scripts/run_debate.py `
  --topic "EUR/USD this week - long, short, or stand aside?" `
  --decisions "direction,entry,stop,target,size" `
  --specialists all `
  --moderator fx_swing_chair
```

To generate a plan from a finished debate (Phase 1 - manual invocation):

```python
from pathlib import Path
from trade_council import run_debate, generate_plan

transcript = run_debate(topic="...", decisions=["..."], specialist_names=["all"],
                        moderator_name="fx_swing_chair")
generate_plan(transcript=transcript, project_root=Path("D:/trade_council"),
              debate_ref="debates/<dir>")
```

In Phase 2 this fires automatically at end of `run_debate()` when `should_act` is yes.

## Specialists (6 FX)

| File | Persona | Tools |
|---|---|---|
| `macro_fundamentalist.md` | Marcus - macro, rate differentials, central bank thinking | WebSearch, WebFetch |
| `sentiment_positioning.md` | Sage - COT, retail sentiment, options skew | WebSearch, WebFetch |
| `intermarket_correlation.md` | Iris - DXY, US10Y, commodities, cross-asset linkages | WebSearch, WebFetch |
| `technical_chartist.md` | Theo - levels, structure, momentum, patterns | WebSearch, WebFetch |
| `quant_volatility.md` | Quinn - IV, ATR, expected moves, options-implied paths | WebSearch, WebFetch |
| `risk_manager.md` | Rhea - sizing, drawdown, regime, sunk-cost discipline | (none) |

Add a new specialist by dropping a `<name>.md` file with frontmatter. No code changes.

## Moderator

`fx_swing_chair.md` - domain-aware chair that weights specialists by regime and drives toward concrete decisions. Add new moderators by dropping a `<name>.md` file.

## Folder layout

```
trade_council/
├── specialists/        6 FX specialists + _template
├── moderators/         fx_swing_chair
├── overseers/          (Phase 2) portfolio_overseer
├── src/trade_council/
│   ├── debate.py / research.py / plan_generator.py
│   ├── specialist.py / moderator.py / transcript.py / formats/
│   ├── backends/             # claude_code (default) and api (Phase 2)
│   └── paper_trader/         # plan loader, calendar, prices, ledger, engine
├── scripts/
│   ├── run_debate.py         # CLI: one-off debate
│   ├── run_paper_trader.py   # CLI: poll one plan
│   └── run_autonomous.py     # (Phase 3) scheduler-driven runner
├── trade_plans/        # auto-generated YAMLs (gitignored)
├── debates/            # debate outputs (gitignored)
└── tests/
```

See `STATUS.md` for current build phase and what's wired vs. deferred.
