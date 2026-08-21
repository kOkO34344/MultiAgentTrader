# Risk Guard — deterministic, non-negotiable

**The Risk Guard is not an agent. It is deterministic Python.** It does not
reason, negotiate, or take context into account. It takes the portfolio, the
candidate trades, and the configured limits, and returns a verdict.

This file exists so every LLM agent on the desk understands one thing: **you
cannot talk your way past it.**

## Rules for every agent

1. Before any order reaches the broker, you **must** call `risk_guard_check`.
2. You submit **only** trades returned with `APPROVE`. If a trade returns
   `RESIZE`, you submit it at exactly the returned `approved_qty` — not one
   contract more.
3. `REJECT` is final for this cycle. Do not re-submit the same trade with
   cosmetic changes hoping for a different verdict. That is the behaviour this
   gate exists to prevent, and the Coach will flag it.
4. Never construct orders from a proposal that did not pass through the gate.
5. If the tool errors, **treat it as REJECT**. Fail closed, always.

## What it enforces

| Category | Checks |
|---|---|
| **Notional** | per-trade cap, portfolio total, per-ticker exposure |
| **Size** | contracts per trade, contracts per ticker, open-position count |
| **Greeks** | portfolio net delta, gamma, vega, theta |
| **Time** | minimum days to expiry |
| **Structure** | defined max loss required; naked short options rejected |
| **Capital** | cash buffer, buying-power utilisation |
| **Circuit breakers** | daily loss limit, peak-to-trough drawdown halt |
| **Universe** | ticker whitelist |
| **Throttles** | max trades per day, max new tickers per day |
| **Hygiene** | duplicate and offsetting position detection |

## Verdicts

- `APPROVE` — passes every check at the requested size.
- `RESIZE` — would breach a size or notional cap; approved at a smaller
  quantity, returned in `approved_qty`. Zero means rejected.
- `REJECT` — breaches a hard constraint. Not sizeable into compliance.

Every verdict carries machine-readable `reason_codes`. The overall response is
`APPROVE` only if at least one trade survives; if any circuit breaker trips, the
entire batch is rejected regardless of individual trades.

## Design note

The most dangerous failure mode of an LLM trading system is a persuasive
argument for an oversized position. Every genuinely irreversible action on this
desk is therefore gated by code that cannot be persuaded. The agents supply
judgement; the guard supplies limits. Neither is allowed to do the other's job.
