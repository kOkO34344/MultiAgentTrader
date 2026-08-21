"""``desk`` — the command line for the Multi-Agent Options Desk."""

from __future__ import annotations

import json

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from desk.utils.config_loader import get_settings, load_dotenv_once
from desk.utils.logging import setup_logging

app = typer.Typer(
    name="desk",
    help="Multi-Agent Options Desk — a Claude-orchestrated options desk on Alpaca PAPER trading.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _bootstrap() -> None:
    load_dotenv_once()
    settings = get_settings()
    setup_logging(settings.monitor.log_level, settings.monitor.log_dir)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@app.command()
def doctor(
    check_broker: bool = typer.Option(
        True, "--check-broker/--no-check-broker", help="Contact Alpaca to verify the paper account."
    ),
) -> None:
    """Pre-flight: configuration, credentials, paper-account status, and state."""
    _bootstrap()
    settings = get_settings()
    table = Table(title="Desk pre-flight", header_style="bold cyan")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")

    def row(name: str, ok: bool, detail: str, warn: bool = False) -> None:
        mark = "[green]PASS[/green]" if ok else ("[yellow]WARN[/yellow]" if warn else "[red]FAIL[/red]")
        table.add_row(name, mark, detail)

    row("Config", True, f"v{settings.config_version}, experiment '{settings.experiment_id}'")
    row(
        "Alpaca endpoint",
        settings.alpaca.is_paper_endpoint,
        settings.alpaca.base_url
        + ("" if settings.alpaca.is_paper_endpoint else "  <- NOT a paper endpoint"),
    )
    row(
        "Alpaca credentials",
        settings.alpaca.configured,
        "set" if settings.alpaca.configured else "ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY missing — copy .env.example to .env",
    )
    row(
        "Claude API key",
        settings.llm.enabled,
        f"agents will call {settings.llm.default_model}"
        if settings.llm.enabled
        else "not set — agents run deterministic mock reasoning (fully functional offline)",
        warn=not settings.llm.enabled,
    )
    row(
        "Execution mode",
        True,
        "DRY RUN — no orders will be sent" if settings.execution.dry_run else "LIVE PAPER ORDERS",
    )
    row("Universe", bool(settings.universe.all_tickers), ", ".join(settings.universe.all_tickers))
    row(
        "Risk limits",
        True,
        f"${settings.risk_limits.max_notional_per_trade:,.0f}/trade, "
        f"${settings.risk_limits.max_notional_total:,.0f} total, "
        f"{settings.risk_limits.max_trades_per_day} trades/day, "
        f"undefined risk {'ALLOWED' if settings.risk_limits.allow_undefined_risk else 'blocked'}",
    )

    try:
        from desk.utils.config_loader import load_playbooks

        playbooks = load_playbooks()
        count = sum(len(v.get("playbooks", [])) for v in playbooks.get("regimes", {}).values())
        row("Playbooks", count > 0, f"{count} playbooks across {len(playbooks.get('regimes', {}))} regimes")
    except Exception as exc:  # noqa: BLE001
        row("Playbooks", False, str(exc))

    try:
        from desk.monitor.state_store import get_state_store

        counts = get_state_store().counts()
        row("State store", True, ", ".join(f"{k}={v}" for k, v in counts.items()))
    except Exception as exc:  # noqa: BLE001
        row("State store", False, str(exc))

    from desk.monitor.heartbeat import check_heartbeat

    beat = check_heartbeat()
    row("Heartbeat", beat["healthy"], beat["alert"] or "recent", warn=not beat["healthy"])

    if check_broker and settings.alpaca.configured:
        try:
            from desk.alpaca.client import get_clients

            account = get_clients().verify_paper_account()
            row(
                "Paper account",
                account["looks_like_paper"] and not account["trading_blocked"],
                f"{account['account_number']} · {account['status']} · "
                f"equity ${account['equity']:,.2f} · buying power ${account['buying_power']:,.2f} · "
                f"options level {account['options_trading_level']}",
            )
        except Exception as exc:  # noqa: BLE001
            row("Paper account", False, str(exc)[:160])
    elif check_broker:
        row("Paper account", False, "skipped — no credentials", warn=True)

    console.print(table)

    from desk.mcp import ALL_TOOLS

    console.print(
        Panel(
            f"MCP tools available: {', '.join(t.name for t in ALL_TOOLS)}\n"
            f"Register with: [cyan]claude mcp add options-desk -- desk mcp-server[/cyan]",
            title="MCP",
            border_style="blue",
        )
    )


# ---------------------------------------------------------------------------
# run-cycle
# ---------------------------------------------------------------------------


@app.command("run-cycle")
def run_cycle(
    phase: str = typer.Option("morning", help="premarket | morning | midday | eod"),
    dry_run: bool | None = typer.Option(
        None, "--dry-run/--live", help="Override the configured execution mode."
    ),
    max_trades: int | None = typer.Option(None, help="Cap new trades for this cycle."),
    json_out: bool = typer.Option(False, "--json", help="Print the cycle result as JSON."),
) -> None:
    """Run one full decision cycle: research, committee, risk gate, execution."""
    _bootstrap()
    from desk.orchestrator.orchestrator_agent import Orchestrator

    if phase == "eod":
        journal(save=True, json_out=json_out)
        return

    orchestrator = Orchestrator(dry_run=dry_run)
    mode = "DRY RUN" if orchestrator.dry_run else "LIVE PAPER ORDERS"
    console.print(Panel(f"Cycle [bold]{phase}[/bold] — {mode}", border_style="blue"))

    result = orchestrator.run_cycle(phase=phase, max_new_trades=max_trades)

    if json_out:
        console.print_json(json.dumps(result.model_dump(), default=str))
        return

    console.print(
        Panel(
            f"[bold]{result.summary}[/bold]\n\n"
            f"Regime:      [magenta]{result.regime}[/magenta] "
            f"({result.regime_detail.get('resolution_note', '')})\n"
            f"Watchlist:   {', '.join(result.watchlist) or '—'}\n"
            f"Agents:      {', '.join(result.agents_consulted) or '—'}\n"
            f"Abstained:   {', '.join(result.agents_abstained) or 'none'}\n"
            f"Structures:  {result.structures_built} built, "
            f"{result.trades_approved} cleared, {result.trades_rejected} rejected\n"
            f"Orders:      {len(result.orders_submitted)}",
            title=f"Cycle {result.cycle_id}",
            border_style="green" if result.status == "complete" else "red",
        )
    )
    if result.rejection_reasons:
        console.print("[bold]Why trades were rejected[/bold]")
        for reason in result.rejection_reasons[:12]:
            console.print(f"  [dim]•[/dim] {reason}")
    for error in result.errors:
        console.print(f"[red]error:[/red] {error}")


# ---------------------------------------------------------------------------
# competition
# ---------------------------------------------------------------------------


@app.command()
def competition(
    cycle: str | None = typer.Option(None, help="Force a cycle: premarket|morning|midday|eod."),
    force: bool = typer.Option(False, "--force", help="Run even outside market hours."),
    show_plan: bool = typer.Option(False, "--plan", help="Print the run plan and exit."),
) -> None:
    """Run the competition-mode cycle that is due now."""
    _bootstrap()
    from desk.orchestrator.competition_mode import CompetitionRunner

    runner = CompetitionRunner()

    if show_plan:
        table = Table(title="Competition run plan (US/Eastern)", header_style="bold cyan")
        for column in ("Date", "Day", "Session", "Phase", "Max trades", "Size", "Prompts"):
            table.add_column(column)
        for row in runner.plan():
            table.add_row(
                row["date"],
                row["weekday"],
                str(row["competition_day"]) if row["trading_day"] else "[dim]closed[/dim]",
                row["phase"],
                str(row["max_trades"]),
                f"{row['size_multiplier']:.2f}x",
                "frozen" if row["prompts_frozen"] else "tunable",
            )
        console.print(table)
        return

    outcome = runner.run_scheduled_cycle(cycle=cycle, force=force)
    if not outcome["ran"]:
        console.print(Panel(outcome["reason"], title="No cycle run", border_style="yellow"))
        console.print_json(json.dumps(outcome["limits"], default=str))
        return

    limits = outcome["limits"]
    console.print(
        Panel(
            f"Competition day [bold]{limits['competition_day']}[/bold] "
            f"({limits['phase']} phase) — cycle [bold]{outcome['cycle']}[/bold]\n"
            f"Max trades today: {limits['max_trades_per_day']} · "
            f"size {limits['size_multiplier']:.2f}x · "
            f"prompts {'frozen' if limits['freeze_prompts'] else 'tunable'}",
            border_style="blue",
        )
    )
    if "result" in outcome:
        console.print(outcome["result"].get("summary", ""))
    if "journal" in outcome:
        console.print(f"Journal written: {outcome['journal'].get('post_path')}")


# ---------------------------------------------------------------------------
# backtest
# ---------------------------------------------------------------------------


@app.command()
def backtest(
    start: str | None = typer.Option(None, help="YYYY-MM-DD (defaults to config)."),
    end: str | None = typer.Option(None, help="YYYY-MM-DD (defaults to config)."),
    tickers: str | None = typer.Option(None, help="Comma-separated override of the universe."),
    rebalance_days: int = typer.Option(5, help="Trading days between rebalances."),
    log_experiment: bool = typer.Option(True, help="Record the result in the experiment registry."),
) -> None:
    """Replay the desk's logic against historical data."""
    _bootstrap()
    from desk.backtest.backtest_engine import BacktestEngine
    from desk.backtest.metrics import format_metrics

    symbols = [t.strip().upper() for t in tickers.split(",")] if tickers else None
    console.print("[dim]Loading history and replaying…[/dim]")
    result = BacktestEngine().run(start=start, end=end, tickers=symbols, rebalance_every_days=rebalance_days)

    console.print(
        Panel(
            format_metrics(result.metrics),
            title=f"Backtest {result.start} → {result.end}",
            border_style="green" if result.metrics.get("total_pnl", 0) >= 0 else "red",
        )
    )
    console.print(
        f"[dim]Pricing source: {result.pricing_source} — option prices are model-generated "
        f"where historical chains are unavailable, so results are indicative, not exact.[/dim]"
    )

    by_playbook = (result.metrics.get("options") or {}).get("by_playbook") or {}
    if by_playbook:
        table = Table(title="By playbook", header_style="bold cyan")
        for column in ("Playbook", "Trades", "Total P&L", "Hit rate", "Avg P&L"):
            table.add_column(column, justify="right" if column != "Playbook" else "left")
        for name, stats in by_playbook.items():
            table.add_row(
                name, str(stats["trades"]), f"${stats['total_pnl']:,.2f}",
                f"{stats['hit_rate']:.0%}", f"${stats['avg_pnl']:,.2f}",
            )
        console.print(table)

    for warning in result.warnings:
        console.print(f"[yellow]warning:[/yellow] {warning}")

    if log_experiment and result.trades:
        from desk.experiments.registry import get_registry

        run = get_registry().log_backtest_run(
            get_settings().experiment_id,
            result.metrics,
            {"start": result.start, "end": result.end},
        )
        console.print(f"[dim]Logged as {run['run_id']} under '{get_settings().experiment_id}'.[/dim]")


# ---------------------------------------------------------------------------
# dashboard / story / journal
# ---------------------------------------------------------------------------


@app.command()
def dashboard(
    web: bool = typer.Option(False, "--web", help="Serve the web dashboard instead of the TUI."),
    watch: bool = typer.Option(False, "--watch", help="Auto-refresh the terminal view."),
    interval: int = typer.Option(10, help="Refresh interval in seconds for --watch."),
    host: str | None = typer.Option(None, help="Bind host for --web."),
    port: int | None = typer.Option(None, help="Bind port for --web."),
) -> None:
    """Show P&L, positions, regime, and decision traces."""
    _bootstrap()
    if web:
        from desk.monitor.dashboard_web import serve

        settings = get_settings()
        url = f"http://{host or settings.monitor.dashboard_host}:{port or settings.monitor.dashboard_port}"
        console.print(Panel(f"Dashboard: [cyan]{url}[/cyan]  (Ctrl-C to stop)", border_style="blue"))
        serve(host, port)
        return

    from desk.monitor.dashboard_cli import render
    from desk.monitor.dashboard_cli import watch as watch_dashboard

    if watch:
        watch_dashboard(interval)
    else:
        render()


@app.command()
def story(
    save: bool = typer.Option(True, help="Write social/daily_posts/YYYY-MM-DD.md."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Generate the day's X/LinkedIn post from the logs."""
    journal(save=save, json_out=json_out)


@app.command()
def journal(
    save: bool = typer.Option(True, help="Write the social post to disk."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """End-of-day: post-trade review plus the social post."""
    _bootstrap()
    from desk.orchestrator.orchestrator_agent import Orchestrator

    result = Orchestrator().run_journal(save_post=save)

    if json_out:
        console.print_json(json.dumps(result, default=str))
        return

    review = result["coach"].get("review_report", {})
    console.print(
        Panel(
            f"{review.get('summary', '')}\n\n"
            f"Trades reviewed: {review.get('trades_reviewed', 0)}   "
            f"Realised: ${review.get('pnl_realised', 0):,.2f}   "
            f"Hit rate: {review.get('hit_rate', 0):.0%}   "
            f"Process score: {review.get('process_score', 0):.2f}",
            title=f"Coach review — {result['date']}",
            border_style="cyan",
        )
    )
    for lesson in result["coach"].get("lessons_for_tomorrow", []):
        console.print(f"  [dim]•[/dim] {lesson}")

    story_output = result["story"]
    console.print(
        Panel(
            story_output.get("post_text_x", ""),
            title=f"X post — {story_output.get('story_angle', '')} "
            f"({len(story_output.get('post_text_x', ''))} chars)",
            border_style="magenta",
        )
    )
    if result.get("post_path"):
        console.print(f"[dim]Saved: {result['post_path']}[/dim]")


# ---------------------------------------------------------------------------
# experiments / mcp
# ---------------------------------------------------------------------------


@app.command()
def experiments(
    create: str | None = typer.Option(None, help="Create a new experiment with this id."),
    description: str = typer.Option("", help="Description for --create."),
    activate: str | None = typer.Option(None, help="Set the active experiment id."),
) -> None:
    """List, create, or activate experiments."""
    _bootstrap()
    from desk.experiments.registry import get_registry

    registry = get_registry()
    if create:
        entry = registry.create(create, description or f"Experiment {create}")
        console.print(f"[green]Created[/green] '{entry['id']}'.")
    if activate:
        registry.set_active(activate)
        console.print(f"[green]Active experiment:[/green] {activate}")

    table = Table(title="Experiments", header_style="bold cyan")
    for column in ("ID", "Description", "Runs", "Backtest P&L", "Sharpe", "Max DD", "Live sessions", "Live P&L"):
        table.add_column(column, overflow="fold")
    for row in registry.compare():
        table.add_row(
            row["id"], row["description"], str(row["runs"]),
            f"${row['backtest_pnl']:,.2f}" if row["backtest_pnl"] is not None else "—",
            f"{row['backtest_sharpe']:.2f}" if row["backtest_sharpe"] is not None else "—",
            f"{row['backtest_max_dd']:.2%}" if row["backtest_max_dd"] is not None else "—",
            str(row["live_sessions"]),
            f"${row['live_cumulative_pnl']:,.2f}" if row["live_cumulative_pnl"] is not None else "—",
        )
    console.print(table)


@app.command("mcp-server")
def mcp_server() -> None:
    """Start the MCP server over stdio (for `claude mcp add`)."""
    from desk.mcp.server import main

    main()


@app.command()
def tools(json_out: bool = typer.Option(False, "--json", help="Print the raw JSON schemas.")) -> None:
    """List the MCP tools this desk exposes."""
    _bootstrap()
    from desk.mcp import ALL_TOOLS

    if json_out:
        console.print_json(
            json.dumps(
                [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in ALL_TOOLS],
                indent=2,
            )
        )
        return

    table = Table(title="MCP tools", header_style="bold cyan")
    table.add_column("Tool")
    table.add_column("Required")
    table.add_column("Description", overflow="fold")
    for spec in ALL_TOOLS:
        table.add_row(spec.name, ", ".join(spec.input_schema.get("required", [])) or "—", spec.description)
    console.print(table)


@app.command()
def version() -> None:
    """Print the desk version."""
    from desk import __version__

    console.print(f"Multi-Agent Options Desk {__version__}")


if __name__ == "__main__":
    app()
