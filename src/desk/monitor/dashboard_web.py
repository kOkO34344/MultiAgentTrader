"""FastAPI dashboard — a single self-contained page polling JSON endpoints.

No build step and no CDN: the page is one inline HTML/CSS/JS document with a
hand-drawn SVG equity chart, so ``desk dashboard --web`` works on a laptop with
no network access.
"""

from __future__ import annotations

from typing import Any

from desk.monitor.dashboard_cli import collect
from desk.monitor.heartbeat import check_heartbeat
from desk.monitor.state_store import get_state_store
from desk.utils.config_loader import get_settings
from desk.utils.logging import get_logger

logger = get_logger("monitor.dashboard_web")

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Multi-Agent Options Desk</title>
<style>
  :root {
    --bg:#0d1117; --panel:#161b22; --line:#30363d; --text:#e6edf3;
    --muted:#8b949e; --green:#3fb950; --red:#f85149; --blue:#58a6ff;
    --amber:#d29922; --violet:#bc8cff;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; }
  header { padding:18px 24px; border-bottom:1px solid var(--line);
           display:flex; align-items:baseline; gap:18px; flex-wrap:wrap; }
  h1 { font-size:17px; margin:0; letter-spacing:.4px; }
  .tag { font-size:11px; padding:3px 9px; border-radius:11px; border:1px solid var(--line); }
  .dry { color:var(--amber); border-color:var(--amber); }
  .live { color:var(--red); border-color:var(--red); }
  .regime { color:var(--violet); border-color:var(--violet); }
  main { padding:20px 24px; display:grid; gap:18px;
         grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); }
  .panel { background:var(--panel); border:1px solid var(--line);
           border-radius:8px; padding:14px 16px; }
  .panel.wide { grid-column:1/-1; }
  h2 { font-size:12px; text-transform:uppercase; letter-spacing:1px;
       color:var(--muted); margin:0 0 12px; font-weight:600; }
  .stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); gap:12px; }
  .stat .label { font-size:10px; color:var(--muted); text-transform:uppercase; letter-spacing:.7px; }
  .stat .value { font-size:19px; margin-top:3px; }
  .pos { color:var(--green); } .neg { color:var(--red); } .zero { color:var(--muted); }
  .scroll { overflow-x:auto; }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  th { text-align:left; color:var(--muted); font-weight:600; padding:6px 10px;
       border-bottom:1px solid var(--line); text-transform:uppercase; font-size:10px;
       letter-spacing:.6px; white-space:nowrap; }
  td { padding:6px 10px; border-bottom:1px solid rgba(48,54,61,.5); white-space:nowrap; }
  tr:last-child td { border-bottom:none; }
  .num { text-align:right; font-variant-numeric:tabular-nums; }
  .empty { color:var(--muted); font-style:italic; padding:14px 10px; }
  .alert { border-color:var(--red); color:var(--red); }
  .okbar { border-color:var(--green); color:var(--green); }
  .banner { grid-column:1/-1; padding:9px 14px; border-radius:6px; border:1px solid; font-size:12px; }
  code { color:var(--blue); font-size:11px; }
  footer { padding:14px 24px; color:var(--muted); font-size:11px;
           border-top:1px solid var(--line); }
</style>
</head>
<body>
<header>
  <h1>Multi-Agent Options Desk</h1>
  <span class="tag" id="mode">…</span>
  <span class="tag regime" id="regime">…</span>
  <span class="tag" id="experiment">…</span>
  <span style="margin-left:auto;color:var(--muted);font-size:11px" id="updated"></span>
</header>
<main id="root">
  <div class="panel wide"><h2>Loading</h2><div class="empty">Fetching desk state…</div></div>
</main>
<footer>
  Paper trading only. Refreshes every 10s ·
  <code>desk dashboard</code> for the terminal view ·
  <code>desk story</code> to generate the daily post.
</footer>
<script>
const money = n => (n<0?"-":"") + "$" + Math.abs(Number(n)||0).toLocaleString("en-US",
  {minimumFractionDigits:2, maximumFractionDigits:2});
const pct = n => ((Number(n)||0)*100).toFixed(2) + "%";
const cls = n => Number(n)>0 ? "pos" : Number(n)<0 ? "neg" : "zero";
const esc = s => String(s??"").replace(/[&<>"]/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function chart(values){
  if(!values || values.length < 2) return '<div class="empty">Not enough snapshots yet.</div>';
  const w=900, h=200, pad=8;
  const lo=Math.min(...values), hi=Math.max(...values), span=(hi-lo)||1;
  const x=i=>pad+(i/(values.length-1))*(w-2*pad);
  const y=v=>h-pad-((v-lo)/span)*(h-2*pad);
  const line=values.map((v,i)=>`${i?"L":"M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const area=`${line} L${x(values.length-1).toFixed(1)},${h-pad} L${pad},${h-pad} Z`;
  const up=values[values.length-1] >= values[0];
  const col=up?"var(--green)":"var(--red)";
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"
    style="width:100%;height:200px;overflow:visible">
    <path d="${area}" fill="${col}" opacity=".12"/>
    <path d="${line}" fill="none" stroke="${col}" stroke-width="2"
      stroke-linejoin="round" vector-effect="non-scaling-stroke"/>
  </svg>
  <div style="display:flex;justify-content:space-between;color:var(--muted);font-size:11px;margin-top:6px">
    <span>low ${money(lo)}</span><span>${values.length} snapshots</span><span>high ${money(hi)}</span>
  </div>`;
}

function table(cols, rows, emptyText){
  if(!rows.length) return `<div class="empty">${emptyText}</div>`;
  return `<div class="scroll"><table><thead><tr>${
    cols.map(c=>`<th class="${c.num?'num':''}">${c.label}</th>`).join("")
  }</tr></thead><tbody>${
    rows.map(r=>`<tr>${cols.map(c=>{
      const v=c.render(r);
      return `<td class="${c.num?'num ':''}${c.cls?c.cls(r):''}">${v}</td>`;
    }).join("")}</tr>`).join("")
  }</tbody></table></div>`;
}

async function refresh(){
  let d;
  try { d = await (await fetch("/api/state")).json(); }
  catch(e){ document.getElementById("root").innerHTML =
    `<div class="banner alert">Cannot reach the desk API: ${esc(e.message)}</div>`; return; }

  const s = d.summary || {};
  const mode = document.getElementById("mode");
  mode.textContent = s.dry_run ? "DRY RUN" : "LIVE PAPER ORDERS";
  mode.className = "tag " + (s.dry_run ? "dry" : "live");
  document.getElementById("regime").textContent = "regime: " + (d.regime || "unknown");
  document.getElementById("experiment").textContent = s.experiment_id || "";
  document.getElementById("updated").textContent = "updated " + new Date().toLocaleTimeString();

  const hb = d.heartbeat || {};
  const banner = hb.healthy
    ? `<div class="banner okbar">Heartbeat OK — last cycle ${Math.round((hb.age_seconds||0)/60)} min ago.</div>`
    : `<div class="banner alert">ALERT — ${esc(hb.alert||"heartbeat unavailable")}</div>`;

  const stat = (label,value,klass="") =>
    `<div class="stat"><div class="label">${label}</div>
     <div class="value ${klass}">${value}</div></div>`;

  document.getElementById("root").innerHTML = banner + `
    <div class="panel wide"><h2>Account</h2><div class="stats">
      ${stat("Equity", money(s.equity))}
      ${stat("Cash", money(s.cash))}
      ${stat("Buying power", money(s.buying_power))}
      ${stat("Day P&L", money(s.day_pnl)+" ("+pct(s.day_pnl_pct)+")", cls(s.day_pnl))}
      ${stat("Unrealised", money(s.unrealised_pnl), cls(s.unrealised_pnl))}
      ${stat("Realised", money(s.realised_pnl), cls(s.realised_pnl))}
      ${stat("Max drawdown", money(s.max_drawdown)+" ("+pct(s.max_drawdown_pct)+")")}
      ${stat("Hit rate", pct(s.hit_rate)+" of "+(s.closed_trades||0))}
    </div></div>

    <div class="panel wide"><h2>Equity curve</h2>${chart(d.equity_curve||[])}</div>

    <div class="panel wide"><h2>Open positions (${(d.positions||[]).length})</h2>
      ${table([
        {label:"Symbol", render:r=>esc(r.symbol)},
        {label:"Underlying", render:r=>esc(r.underlying)},
        {label:"Class", render:r=>esc(r.asset_class)},
        {label:"Qty", num:true, render:r=>Number(r.qty)},
        {label:"Market value", num:true, render:r=>money(r.market_value)},
        {label:"Unrealised", num:true, render:r=>money(r.unrealized_pl), cls:r=>cls(r.unrealized_pl)},
      ], d.positions||[], "No open positions.")}
    </div>

    <div class="panel wide"><h2>Recent trades &amp; their decisions</h2>
      ${table([
        {label:"Trade", render:r=>esc(String(r.trade_id).slice(0,26))},
        {label:"Ticker", render:r=>esc(r.ticker)},
        {label:"Playbook", render:r=>esc(r.playbook)},
        {label:"Status", render:r=>esc(r.status)},
        {label:"P&L", num:true, render:r=>r.pnl==null?"—":money(r.pnl), cls:r=>cls(r.pnl)},
        {label:"Risk codes", render:r=>esc((r.risk_reason_codes||[]).join(", "))},
        {label:"Thesis", render:r=>esc(String(r.thesis||"").slice(0,70))},
      ], d.trades||[], "No trades recorded yet.")}
    </div>

    <div class="panel"><h2>Recent cycles</h2>
      ${table([
        {label:"Cycle", render:r=>esc(r.cycle_id)},
        {label:"Phase", render:r=>esc(r.phase)},
        {label:"Regime", render:r=>esc(r.regime)},
        {label:"Status", render:r=>esc(r.status)},
      ], d.cycles||[], "No cycles run yet.")}
    </div>

    <div class="panel"><h2>State store</h2><div class="stats">
      ${Object.entries(d.counts||{}).map(([k,v])=>stat(k.replace(/_/g," "), v)).join("")}
    </div></div>`;
}
refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>
"""


def create_app() -> Any:
    """Build the FastAPI application."""
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse

    app = FastAPI(
        title="Multi-Agent Options Desk",
        description="Observability for a Claude-orchestrated options desk on Alpaca paper trading.",
        version="0.1.0",
    )

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> str:
        return PAGE

    @app.get("/api/state")
    def state() -> Any:
        """Everything the dashboard renders, in one request."""
        return JSONResponse(collect())

    @app.get("/api/account")
    def account() -> Any:
        return JSONResponse(get_state_store().latest_account() or {})

    @app.get("/api/positions")
    def positions() -> Any:
        return JSONResponse(get_state_store().latest_positions())

    @app.get("/api/pnl")
    def pnl() -> Any:
        store = get_state_store()
        return JSONResponse(
            {"equity_curve": store.equity_curve(), "daily_metrics": store.get_daily_metrics()}
        )

    @app.get("/api/trades")
    def trades(limit: int = 50) -> Any:
        return JSONResponse(get_state_store().get_trades(limit=limit))

    @app.get("/api/traces")
    def traces(cycle_id: str | None = None, limit: int = 100) -> Any:
        """Full decision traces — how any given trade came to be."""
        return JSONResponse(get_state_store().get_traces(cycle_id, limit))

    @app.get("/api/regime")
    def regime() -> Any:
        cycles = get_state_store().get_cycles(limit=1)
        return JSONResponse(
            {
                "regime": cycles[0].get("regime") if cycles else None,
                "cycle": cycles[0] if cycles else None,
            }
        )

    @app.get("/api/experiments")
    def experiments() -> Any:
        from desk.experiments.registry import get_registry

        return JSONResponse(get_registry().compare())

    @app.get("/health")
    def health() -> Any:
        beat = check_heartbeat()
        return JSONResponse(beat, status_code=200 if beat["healthy"] else 503)

    return app


def serve(host: str | None = None, port: int | None = None) -> None:
    """Run the dashboard with uvicorn."""
    import uvicorn

    settings = get_settings()
    host = host or settings.monitor.dashboard_host
    port = port or settings.monitor.dashboard_port
    logger.info("dashboard_start", extra={"event": "dashboard_start", "host": host, "port": port})
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")
