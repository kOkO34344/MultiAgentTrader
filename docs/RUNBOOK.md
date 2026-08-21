# Runbook

## First-time setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Fill in `.env` with keys from a **dedicated paper account**
(<https://app.alpaca.markets/paper/dashboard/overview>). Options trading must be
enabled on it — level 3 or higher for multi-leg spreads.

```bash
desk doctor
```

Everything should read PASS except the Claude key (WARN is fine — agents fall
back to deterministic reasoning) and the heartbeat on a fresh install.

## Daily operation

```bash
desk competition            # runs whichever cycle is due, honouring today's caps
desk dashboard --web        # watch it at http://127.0.0.1:8787
```

Or drive cycles manually:

```bash
desk run-cycle --phase premarket
desk run-cycle --phase morning
desk run-cycle --phase midday --max-trades 1
desk journal                # coach review + social post
```

### Going live on paper

`--dry-run` is the default. To actually place paper orders:

```bash
desk run-cycle --phase morning --live
```

Or set `DESK_DRY_RUN=false` in `.env`. `desk doctor` always shows which mode is
active, and so does every dashboard header.

## Reading a decision

Every trade is explainable. From the dashboard, note the `cycle_id`, then:

```bash
curl -s localhost:8787/api/traces?cycle_id=2026-08-28-morning-140012 | python3 -m json.tool
```

Trace stages, in order: `account` → `snapshot` → `regime` → `research` (one per
agent) → `structures` → `critic` → `risk_guard` → `execution` → `cycle_result`.

The `risk_guard` stage carries the per-trade verdicts and reason codes. If a
trade you expected didn't happen, that stage says why — and if it never reached
that stage, the `critic` stage does.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `PaperOnlyError` on startup | `ALPACA_BASE_URL` isn't the paper endpoint. This guard is intentional — fix the URL, don't disable it. |
| `doctor` shows credentials FAIL | `.env` missing or not in the project root. |
| Cycle ends "No market data" | Bad credentials, market closed with no cached bars, or the `iex` feed lacks your tickers. Try `ALPACA_DATA_FEED=sip` if you have the subscription. |
| Cycle builds zero structures | Usually correct behaviour. Check the logs for `structure_rejected` and `playbook_conditions_unmet` — the chain failed a liquidity, width, or condition gate. |
| Every trade rejected `NEGATIVE_EXPECTANCY` | Implied vol is at or below realised vol, so there is no premium to sell. Standing aside is the right answer. |
| Every trade rejected `CIRCUIT_DAILY_LOSS` | The daily loss limit tripped. It resets tomorrow. Do not raise it to keep trading. |
| Orders submit but never fill | Widen `execution.marketable_edge_pct`, or check that the market is actually open. |
| Heartbeat alert | The last cycle failed or nothing has run. Check `logs/desk.jsonl`. |
| MCP tools return `not_configured` | The server started without credentials. Restart it after fixing `.env`. |

## Logs

```bash
tail -f logs/desk.jsonl | python3 -c "
import sys, json
for line in sys.stdin:
    r = json.loads(line)
    print(f\"{r['ts'][11:19]} {r['level']:7} {r.get('event', r['message'])}\")
"
```

One JSON object per line, so `grep` and `jq` both work. Useful events:
`risk_guard_check`, `risk_guard_halt`, `regime_override`, `structure_rejected`,
`playbook_conditions_unmet`, `order_submitted`, `agent_llm_failed`.

The MCP server logs to `logs/mcp.jsonl` and never to stdout — stdout is the
transport.

## Safety checklist before leaving it running

- [ ] `desk doctor` is clean and the account number starts with `PA`
- [ ] `execution.dry_run` is what you intend
- [ ] `risk_limits.max_notional_total` is a number you would accept losing
- [ ] `max_daily_loss_pct` and `max_drawdown_halt_pct` are set
- [ ] `universe` contains only liquid, options-rich names
- [ ] The heartbeat is being written
- [ ] `.env` is **not** in `git ls-files`
