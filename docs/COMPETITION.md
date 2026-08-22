# Competition plan — Aug 28 to Sep 4

## Daily schedule (US/Eastern)

| Time | Cycle | Actions | New trades |
|---|---|---|---|
| 08:45 | `premarket` | Load config, classify regime, select watchlist | 0 |
| 10:00 | `morning` | Research → committee → risk gate → execute | up to 3 |
| 13:30 | `midday` | Monitor, adjust, hedge | up to 1 |
| 15:45 | `eod` | Flatten expiring, snapshot, coach, storyteller | 0 |

Waiting until 10:00 for the main cycle is deliberate: the opening range is noisy,
option spreads are widest at the bell, and a regime read on the first ten minutes
of tape is mostly noise.

## Weekly arc

Phases count **trading days**, not calendar days.

| Sessions | Phase | Size | Max trades/day | Prompts |
|---|---|---|---|---|
| 1–2 | Exploration | 0.50x | 4 | Tunable |
| 3–5 | Concentration | 1.00x | 3 | Tunable |
| 6–8 | Freeze | 0.75x | 2 | **Frozen** |

- **Exploration** — run the full playbook library at half size. The goal is
  coverage and information, not P&L. Expect a flat-to-slightly-negative first
  two sessions and do not react to them.
- **Concentration** — read the Coach's per-playbook breakdown, narrow to what is
  actually working, and size up to full.
- **Freeze** — stop changing things. Consistent, explainable operation over the
  final sessions is worth more than one more tweak, and a broken run on the last
  day is unrecoverable.

```bash
desk competition --plan   # exact dates and caps
```

## Daily checklist

**Morning**
1. `desk doctor` — clean?
2. `desk competition` at 08:45 (premarket) and 10:00 (morning)
3. Read the cycle summary. If zero trades, read *why* — usually it is correct.

**Midday**
4. `desk dashboard` — any position past its stop or time stop?
5. `desk competition --cycle midday` if a hedge or adjustment is warranted

**End of day**
6. `desk competition --cycle eod` — writes the coach review and the social post
7. Read `lessons_for_tomorrow`. Apply config changes **only** during exploration
   and concentration phases, never during freeze.
8. Review `social/daily_posts/YYYY-MM-DD.md`, verify the numbers, post it.

## Storytelling

The hackathon allows up to five links to X/LinkedIn posts about the building
journey. `desk story` drafts one per day from the actual logs.

What makes a post worth reading:

- **Agent disagreement.** Nobody has seen six AI analysts argue about an iron
  condor. When the Critic records a `notable_disagreement`, lead with it.
- **A Risk Guard save.** "The committee approved it, the guard resized it to one
  contract, here's the reason code" is a genuinely interesting engineering story.
- **A regime flip** and how the playbook set changed with it.
- **A coach lesson** that changed a specific config value, with the evidence.
- **Losses, plainly.** A desk that only posts wins is not believable.

The Storyteller is forbidden from inventing numbers — every figure comes from the
logs it was handed. **Verify the P&L against the Alpaca dashboard before posting
anyway.**

## Submission checklist

- [ ] Repo is accessible to judges, README renders, Mermaid diagram displays
- [ ] `desk doctor` is clean on a fresh clone with only `.env` added
- [ ] Paper account is the dedicated hackathon account
- [ ] `desk backtest` results logged in the experiment registry
- [ ] Dashboard screenshots captured (P&L curve, a decision trace)
- [ ] Up to 5 X/LinkedIn links collected from `social/daily_posts/`
- [ ] MCP registration documented and verified: `claude mcp add options-desk -- "$PWD/.venv/bin/desk" mcp-server` (absolute path — the server is spawned outside the venv), then `claude mcp list` shows `✔ Connected`
- [ ] The pitch: *the agents supply judgement, the guard supplies limits, and neither is allowed to do the other's job*

## Judging criteria, and where to point

| Criterion | What to show |
|---|---|
| **P&L performance** | Dashboard equity curve; the experiment registry comparing configs |
| **Technology implementation** | 8 MCP tools with exact schemas; multi-leg `mleg` orders; paper-only guard; 185 offline tests |
| **Creativity & originality** | Deterministic-first regime classification; expectancy-under-realised-vol scoring; payoff-curve risk derivation; a Coach that scores process separately from outcome |
| **Presentation & execution** | Full decision traces; two dashboards; honest daily build logs |
