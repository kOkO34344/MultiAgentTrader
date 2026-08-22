"""Volatility & Options Strategist — turns a view into a priceable structure.

The structuring math here is **deterministic**, not generated. Given a playbook
and a live chain, the desk selects strikes by delta targeting and then derives
the full risk profile numerically from the expiry payoff curve.

Computing max loss, max profit, and breakevens from the payoff curve rather than
from per-structure formulas means one code path handles verticals, condors,
butterflies, straddles, diagonals, and anything else the playbooks add later —
and it detects *unbounded* risk directly rather than trusting a label.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from desk.agents.base import LLMAgent
from desk.utils.config_loader import Settings, get_settings, playbooks_for_regime
from desk.utils.logging import get_logger
from desk.utils.math_utils import probability_itm, round_to_tick

logger = get_logger("agents.vol")

CONTRACT_MULTIPLIER = 100
PAYOFF_GRID_POINTS = 1200

#: Playbook ``type`` values that open a net-short-premium position. Sold premium
#: is the desk's edge only when implied vol is rich, so the offline selector gates
#: on this rather than on substrings in the playbook name.
PREMIUM_SELLING_TYPES = frozenset(
    {"vertical_credit", "iron_condor", "iron_butterfly", "butterfly"}
)


# ---------------------------------------------------------------------------
# Payoff engine
# ---------------------------------------------------------------------------


def intrinsic_value(spot: float, strike: float, right: str) -> float:
    """Value of one option at expiry."""
    return max(spot - strike, 0.0) if right == "call" else max(strike - spot, 0.0)


def net_cost(legs: list[dict[str, Any]], multiplier: int = CONTRACT_MULTIPLIER) -> float:
    """Cash flow to open. Positive is a net debit paid, negative a net credit."""
    total = 0.0
    for leg in legs:
        price = float(leg.get("mid_price") or leg.get("limit_price") or 0.0)
        qty = abs(float(leg.get("qty", 1)))
        sign = -1.0 if str(leg.get("side", "buy")).lower().startswith("s") else 1.0
        total += sign * price * qty * multiplier
    return total


def payoff_at(legs: list[dict[str, Any]], spot: float, multiplier: int = CONTRACT_MULTIPLIER) -> float:
    """Profit/loss of the structure at expiry for a terminal price of ``spot``."""
    value = 0.0
    for leg in legs:
        qty = abs(float(leg.get("qty", 1)))
        sign = -1.0 if str(leg.get("side", "buy")).lower().startswith("s") else 1.0
        value += sign * intrinsic_value(spot, float(leg["strike"]), leg["right"]) * qty * multiplier
    return value - net_cost(legs, multiplier)


def risk_profile(
    legs: list[dict[str, Any]], spot: float, multiplier: int = CONTRACT_MULTIPLIER
) -> dict[str, Any]:
    """Derive max loss, max profit, and breakevens from the expiry payoff curve.

    Scans terminal prices from zero to three times spot. Unbounded profit or
    loss is detected by checking whether the payoff is still moving at the edges
    of the grid — the Risk Guard rejects anything with unbounded loss.
    """
    if not legs or spot <= 0:
        return {
            "max_loss": None,
            "max_profit": None,
            "breakevens": [],
            "unbounded_loss": True,
            "unbounded_profit": False,
            "net_cost": 0.0,
        }

    upper = max(spot * 3.0, max(float(leg["strike"]) for leg in legs) * 1.5)
    step = upper / PAYOFF_GRID_POINTS
    grid = [i * step for i in range(PAYOFF_GRID_POINTS + 1)]
    payoffs = [payoff_at(legs, price, multiplier) for price in grid]

    worst, best = min(payoffs), max(payoffs)

    # Slope at the extremes reveals unbounded exposure.
    left_slope = payoffs[1] - payoffs[0]
    right_slope = payoffs[-1] - payoffs[-2]
    unbounded_loss = right_slope < -1e-6 or left_slope > 1e-6
    unbounded_profit = right_slope > 1e-6 or left_slope < -1e-6

    breakevens: list[float] = []
    for i in range(1, len(grid)):
        previous, current = payoffs[i - 1], payoffs[i]
        if previous == 0.0:
            breakevens.append(round(grid[i - 1], 2))
        elif (previous < 0) != (current < 0):
            span = current - previous
            crossing = grid[i - 1] + (0 - previous) / span * step if span else grid[i - 1]
            breakevens.append(round(crossing, 2))

    return {
        "max_loss": round(abs(worst), 2) if worst < 0 else 0.0,
        "max_profit": None if unbounded_profit else round(max(best, 0.0), 2),
        "breakevens": sorted(set(breakevens)),
        "unbounded_loss": unbounded_loss,
        "unbounded_profit": unbounded_profit,
        "net_cost": round(net_cost(legs, multiplier), 2),
    }


def probability_of_profit(
    legs: list[dict[str, Any]],
    spot: float,
    breakevens: list[float],
    implied_vol: float,
    years: float,
    rate: float = 0.043,
    multiplier: int = CONTRACT_MULTIPLIER,
) -> float | None:
    """Risk-neutral probability the structure finishes profitable.

    Breakevens partition the terminal price line into intervals; the payoff sign
    at each interval's midpoint says whether it is a winning region, and the
    lognormal CDF gives that region's probability. This is what makes a low
    risk/reward structure judgeable: a 0.25 R:R iron condor is a *good* trade at
    an 80% win rate and a bad one at 50%, and only this number distinguishes them.
    """
    if not breakevens or spot <= 0 or implied_vol <= 0 or years <= 0:
        return None

    bounds = sorted(breakevens)
    edges = [0.0, *bounds, bounds[-1] * 3.0]
    total = 0.0

    for low, high in zip(edges, edges[1:], strict=False):
        midpoint = (low + high) / 2.0 if low > 0 else high * 0.5
        if payoff_at(legs, midpoint, multiplier) <= 0:
            continue
        # P(low < S_T <= high) = N(d2 at low) - N(d2 at high)
        p_above_low = probability_itm(spot, low, years, rate, implied_vol, "call") if low > 0 else 1.0
        p_above_high = probability_itm(spot, high, years, rate, implied_vol, "call")
        total += max(p_above_low - p_above_high, 0.0)

    return round(min(max(total, 0.0), 1.0), 4)


def expectancy(max_profit: float | None, max_loss: float | None, pop: float | None) -> float | None:
    """Expected value per contract: ``pop * profit - (1 - pop) * loss``."""
    if pop is None or max_loss is None:
        return None
    if max_profit is None:
        # Unbounded upside: value it at the breakeven-neutral point so a
        # convexity trade is never scored as though it had infinite edge.
        max_profit = max_loss
    return round(pop * max_profit - (1.0 - pop) * max_loss, 2)


def net_greeks(legs: list[dict[str, Any]], multiplier: int = CONTRACT_MULTIPLIER) -> dict[str, float]:
    """Position-level Greeks, already multiplied by contract size."""
    totals = {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0}
    for leg in legs:
        qty = abs(float(leg.get("qty", 1)))
        sign = -1.0 if str(leg.get("side", "buy")).lower().startswith("s") else 1.0
        greeks = leg.get("greeks") or {}
        for greek in totals:
            value = greeks.get(greek)
            if value is not None:
                totals[greek] += sign * float(value) * qty * multiplier
    return {greek: round(value, 4) for greek, value in totals.items()}


# ---------------------------------------------------------------------------
# Structure construction
# ---------------------------------------------------------------------------


class StructureBuilder:
    """Builds a concrete options structure from a playbook and a live chain."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def select_expiration(self, chain: list[dict[str, Any]], playbook: dict[str, Any]) -> str | None:
        """The listed expiry closest to the playbook's target, inside its window."""
        dte_window = playbook.get("dte") or {}
        low = int(dte_window.get("min", self.settings.options.min_days_to_expiry))
        high = int(dte_window.get("max", self.settings.options.max_days_to_expiry))
        target = self.settings.options.target_days_to_expiry

        eligible = {c["expiration"]: c["days_to_expiry"] for c in chain if low <= c["days_to_expiry"] <= high}
        if not eligible:
            return None
        return min(eligible.items(), key=lambda kv: abs(kv[1] - target))[0]

    def pick_by_delta(
        self, chain: list[dict[str, Any]], expiration: str, right: str, target_delta: float
    ) -> dict[str, Any] | None:
        """Closest tradeable contract to a target delta on a given expiry."""
        candidates = [
            c
            for c in chain
            if c["expiration"] == expiration
            and c["right"] == right
            and (c.get("greeks") or {}).get("delta") is not None
            and c.get("mid")
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda c: abs(abs(c["greeks"]["delta"]) - abs(target_delta)))

    def conditions_met(
        self, playbook: dict[str, Any], context: dict[str, Any]
    ) -> tuple[bool, list[str]]:
        """Evaluate a playbook's ``conditions`` block against market context.

        Playbooks declare when they are appropriate — a protective put needs
        long exposure to protect, an iron condor needs a quiet tape and rich
        premium. Unevaluated conditions mean a hedge fires with nothing to
        hedge, so every declared condition is checked here and any it cannot
        evaluate is reported rather than silently passed.
        """
        conditions = playbook.get("conditions") or {}
        if not conditions:
            return True, []

        unmet: list[str] = []
        checks: dict[str, Any] = {
            "iv_rank_min": lambda v: (context.get("iv_rank") is None) or context["iv_rank"] >= v,
            "iv_rank_max": lambda v: (context.get("iv_rank") is None) or context["iv_rank"] <= v,
            "adx_max": lambda v: (context.get("adx") is None) or context["adx"] <= v,
            "adx_min": lambda v: (context.get("adx") is None) or context["adx"] >= v,
            "requires_long_exposure": lambda v: bool(context.get("has_long_exposure")) or not v,
            "event_within_days": lambda v: (
                context.get("days_to_event") is not None and 0 <= context["days_to_event"] <= v
            ),
            "near_support": lambda v: bool(context.get("near_support")) or not v,
            "term_structure": lambda v: context.get("term_structure") in (None, v),
        }

        for key, expected in conditions.items():
            check = checks.get(key)
            if check is None:
                unmet.append(f"unrecognised condition '{key}'")
                continue
            if not check(expected):
                unmet.append(f"{key}={expected!r} not satisfied")

        return (not unmet), unmet

    def build(
        self,
        ticker: str,
        playbook: dict[str, Any],
        chain: list[dict[str, Any]],
        spot: float,
        realised_vol: float | None = None,
    ) -> dict[str, Any] | None:
        """Build one structure. Returns ``None`` when the chain cannot support it.

        ``realised_vol`` matters more than it looks. The structure is *priced* at
        implied volatility, but its win probability is estimated under the
        *realised* distribution. That gap is the variance risk premium — the
        actual source of edge in premium selling. Estimating both under implied
        vol would make every structure score at zero expectancy by construction,
        which is true under risk-neutral pricing and useless as a decision rule.
        """
        if playbook.get("type") == "no_trade" or not playbook.get("legs"):
            return None

        expiration = self.select_expiration(chain, playbook)
        if not expiration:
            logger.info(
                "structure_skipped",
                extra={
                    "event": "structure_skipped",
                    "ticker": ticker,
                    "playbook": playbook.get("name"),
                    "reason": "no expiry inside the playbook window",
                },
            )
            return None

        legs: list[dict[str, Any]] = []
        for spec in playbook["legs"]:
            contract = self.pick_by_delta(
                chain, expiration, spec["right"], float(spec.get("target_delta", 0.3))
            )
            if contract is None:
                logger.info(
                    "structure_skipped",
                    extra={
                        "event": "structure_skipped",
                        "ticker": ticker,
                        "playbook": playbook.get("name"),
                        "reason": f"no {spec['right']} near delta {spec.get('target_delta')}",
                    },
                )
                return None
            if any(existing["contract_symbol"] == contract["symbol"] for existing in legs):
                # Two legs collapsing onto the same strike is a degenerate
                # structure with no risk profile — reject rather than "fix".
                logger.info(
                    "structure_skipped",
                    extra={
                        "event": "structure_skipped",
                        "ticker": ticker,
                        "playbook": playbook.get("name"),
                        "reason": "duplicate strike across legs",
                    },
                )
                return None

            legs.append(
                {
                    "contract_symbol": contract["symbol"],
                    "side": spec["side"],
                    "right": contract["right"],
                    "strike": contract["strike"],
                    "qty": float(spec.get("quantity_multiple", 1)),
                    "target_delta": spec.get("target_delta"),
                    "delta": contract["greeks"].get("delta"),
                    "mid_price": contract["mid"],
                    "bid": contract["bid"],
                    "ask": contract["ask"],
                    "spread_pct": contract["spread_pct"],
                    "implied_volatility": contract.get("implied_volatility"),
                    "greeks": contract.get("greeks", {}),
                }
            )

        profile = risk_profile(legs, spot)
        greeks = net_greeks(legs)
        cost = profile["net_cost"]
        dte = next((c["days_to_expiry"] for c in chain if c["expiration"] == expiration), 0)

        avg_iv = [leg.get("implied_volatility") for leg in legs if leg.get("implied_volatility")]
        implied_vol = sum(avg_iv) / len(avg_iv) if avg_iv else 0.0
        forecast_vol = realised_vol or implied_vol
        pop = probability_of_profit(
            legs,
            spot,
            profile["breakevens"],
            forecast_vol,
            max(dte, 1) / 365.0,
            self.settings.options.risk_free_rate,
        )
        edge = expectancy(profile["max_profit"], profile["max_loss"], pop)
        vol_premium = (
            round(implied_vol - forecast_vol, 4) if implied_vol and realised_vol else None
        )

        structure = {
            "structure_id": f"{ticker.lower()}-{playbook['name']}-{expiration}",
            "ticker": ticker,
            "playbook": playbook["name"],
            "playbook_type": playbook.get("type", ""),
            "expiration": expiration,
            "dte": dte,
            "legs": legs,
            "net_price": round(abs(cost) / CONTRACT_MULTIPLIER, 2),
            "net_side": "debit" if cost > 0 else "credit",
            "risk_profile": {
                "max_loss": profile["max_loss"],
                "max_profit": profile["max_profit"],
                "breakevens": profile["breakevens"],
                "risk_reward": (
                    round(profile["max_profit"] / profile["max_loss"], 3)
                    if profile["max_profit"] and profile["max_loss"]
                    else None
                ),
                "probability_of_profit": pop,
                "expectancy": edge,
                "implied_volatility": round(implied_vol, 4) if implied_vol else None,
                "forecast_volatility": round(forecast_vol, 4) if forecast_vol else None,
                "variance_risk_premium": vol_premium,
                "unbounded_loss": profile["unbounded_loss"],
                "unbounded_profit": profile["unbounded_profit"],
                "net_delta": greeks["delta"],
                "net_gamma": greeks["gamma"],
                "net_vega": greeks["vega"],
                "net_theta": greeks["theta"],
            },
            "liquidity": {
                "worst_spread_pct": max((leg["spread_pct"] for leg in legs), default=0.0),
                "min_open_interest": None,
            },
            "exit_plan": playbook.get("exits", {}),
            "sizing": playbook.get("sizing", {}),
            "spot": spot,
        }
        return structure

    def validate(self, structure: dict[str, Any]) -> tuple[bool, list[str]]:
        """Reject structures the desk must never propose."""
        problems: list[str] = []
        profile = structure["risk_profile"]
        options = self.settings.options

        if profile["unbounded_loss"]:
            problems.append("unbounded loss — the payoff curve never flattens")
        if not profile["max_loss"]:
            problems.append("max loss is zero or unknown")
        if structure["dte"] < options.min_days_to_expiry:
            problems.append(f"{structure['dte']}d to expiry is inside the minimum window")
        if structure["liquidity"]["worst_spread_pct"] > options.max_bid_ask_spread_pct:
            problems.append(
                f"widest leg spread {structure['liquidity']['worst_spread_pct']:.1%} "
                f"exceeds the {options.max_bid_ask_spread_pct:.1%} limit"
            )
        if structure["net_price"] <= 0:
            problems.append("structure prices at zero — the chain data is unusable")

        sizing = structure.get("sizing", {}) or {}
        max_contracts = int(sizing.get("max_contracts", 0) or 0)
        if max_contracts == 0:
            problems.append("playbook sizing permits zero contracts")

        # Playbooks declare their own width and cost ceilings. Nothing else
        # checks them, so a wide chain would otherwise produce a "bull call
        # spread" with a $50 width and a five-figure max loss.
        width = self.strike_width(structure)
        spot = float(structure.get("spot") or 0.0)
        defaults = self.playbook_defaults()

        max_width = sizing.get("max_width")
        if max_width and width > float(max_width) + 1e-9:
            problems.append(
                f"strike width {width:g} exceeds the playbook's max_width of {max_width:g}"
            )

        # Width caps are expressed as a fraction of spot so the same playbook
        # works on a $30 stock and a $600 index ETF.
        max_width_pct = sizing.get("max_width_pct", defaults.get("max_width_pct"))
        if max_width_pct and spot and width > spot * float(max_width_pct) + 1e-9:
            problems.append(
                f"strike width {width:g} exceeds {float(max_width_pct):.0%} of the "
                f"{spot:.2f} spot"
            )

        cost = structure["net_price"] * CONTRACT_MULTIPLIER
        if structure["net_side"] == "debit":
            for key in ("max_debit_per_spread", "max_debit"):
                cap = sizing.get(key)
                if cap and cost > float(cap) + 1e-9:
                    problems.append(f"net debit ${cost:,.0f} exceeds the playbook's {key} of ${float(cap):,.0f}")
        elif width > 0:
            # Credit structures must collect enough premium to be worth the risk.
            min_ratio = float(defaults.get("min_credit_to_width", 0.0))
            if min_ratio and structure["net_price"] < width * min_ratio:
                problems.append(
                    f"credit {structure['net_price']:.2f} is below {min_ratio:.0%} of the "
                    f"{width:g}-wide spread"
                )

        return (not problems), problems

    @staticmethod
    def strike_width(structure: dict[str, Any]) -> float:
        """Widest same-right long/short strike gap — the risk width of the structure."""
        widths = []
        for right in ("call", "put"):
            strikes = [
                float(leg["strike"]) for leg in structure.get("legs", []) if leg.get("right") == right
            ]
            if len(strikes) >= 2:
                widths.append(max(strikes) - min(strikes))
        return max(widths) if widths else 0.0

    @staticmethod
    def playbook_defaults() -> dict[str, Any]:
        """Shared defaults from ``config/strategies/playbooks.yaml``."""
        from desk.utils.config_loader import load_playbooks

        try:
            return load_playbooks().get("defaults", {}) or {}
        except (FileNotFoundError, OSError):
            return {}

    def limit_price(self, structure: dict[str, Any]) -> float:
        """Signed net limit price for a multi-leg order.

        Alpaca takes a positive limit for a net debit and a negative one for a
        net credit, so the sign carries the direction of the cash flow.
        """
        price = structure["net_price"]
        edge = self.settings.execution.marketable_edge_pct
        tick = self.settings.execution.round_limit_to
        if structure["net_side"] == "debit":
            return round_to_tick(price * (1 + edge), tick)
        return -round_to_tick(price * (1 - edge), tick)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class SurfaceNote(BaseModel):
    ticker: str = ""
    iv_rank: float | None = None
    iv_percentile: float | None = None
    term_structure: str = "flat"
    skew: str = "flat"
    iv_vs_realised: str = "fair"


class VolOutput(BaseModel):
    vol_regime_overview: str = ""
    surface_notes: list[SurfaceNote] = Field(default_factory=list)
    selected_playbooks: list[str] = Field(default_factory=list)
    commentary: str = ""


class VolOptionsStrategist(LLMAgent):
    """Chooses which playbooks fit the vol environment; the builder does the math."""

    name = "vol_options_strategist"
    prompt_name = "vol_options_strategist"
    output_model = VolOutput

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.builder = StructureBuilder(self.settings)

    @staticmethod
    def _resolve_playbooks(regime: Any, playbooks: Any) -> list[dict[str, Any]]:
        """Normalise the ``playbooks`` kwarg to full dicts.

        The orchestrator fans out playbook *names* while ``build_structures``
        takes the full dicts. Accept either — a shape change must not make the
        strategist abstain — and re-read the regime's playbooks when only names
        arrive, so the declared ``type`` is available for classification.
        """
        entries = list(playbooks or [])
        if not entries:
            return []
        if not any(isinstance(entry, str) for entry in entries):
            return [entry for entry in entries if isinstance(entry, dict)]

        names = {entry if isinstance(entry, str) else entry.get("name") for entry in entries}
        known = {p["name"]: p for p in playbooks_for_regime(str(regime or ""))}
        return [known.get(name) or {"name": name} for name in sorted(names) if name]

    def build_context(self, **kwargs: Any) -> dict[str, Any]:
        playbooks = self._resolve_playbooks(kwargs.get("regime"), kwargs.get("playbooks"))
        return {
            "regime": kwargs.get("regime"),
            "as_of": kwargs.get("as_of"),
            "playbooks_for_regime": [p["name"] for p in playbooks],
            # Playbooks declare an authoritative ``type``; the offline selector uses
            # it instead of sniffing the name for "credit"/"condor"/"butterfly",
            # which mislabelled `short_put_spread_at_support` (a vertical_credit).
            "playbook_types": {p["name"]: p.get("type", "") for p in playbooks},
            "vol_surfaces": kwargs.get("vol_surfaces", {}),
            "indicators": {
                ticker: data.get("indicators", {})
                for ticker, data in (kwargs.get("snapshot") or {}).items()
            },
            "guidance": (
                "Cheap IV + a directional view -> debit spreads. Rich IV + a directional view -> "
                "credit spreads. Rich IV + no direction -> condors. Cheap IV + an event -> long "
                "premium. Never propose undefined risk."
            ),
        }

    def mock_output(self, context: dict[str, Any]) -> dict[str, Any]:
        """Offline: pick playbooks by IV rank using the same rules as the persona."""
        surfaces = context.get("vol_surfaces") or {}
        available = context.get("playbooks_for_regime") or []
        types = context.get("playbook_types") or {}
        notes, selected = [], []

        for ticker, surface in sorted(surfaces.items()):
            rank = surface.get("iv_rank")
            notes.append(
                {
                    "ticker": ticker,
                    "iv_rank": rank,
                    "iv_percentile": surface.get("iv_percentile"),
                    "term_structure": surface.get("term_structure", "flat"),
                    "skew": surface.get("skew", "flat"),
                    "iv_vs_realised": self._iv_vs_realised(surface, context, ticker),
                }
            )
            if rank is None:
                continue
            rich = rank >= 0.5
            for name in available:
                if types.get(name) == "no_trade":
                    continue  # `stand_aside` is the absence of a structure, not a view
                if self._sells_premium(name, types.get(name)) == rich:
                    if name not in selected:
                        selected.append(name)

        if not selected:
            tradeable = [n for n in available if types.get(n) != "no_trade"]
            selected = tradeable[:1]

        return {
            "vol_regime_overview": (
                f"Sampled {len(notes)} surface(s). Playbook selection follows the IV-rank rule: "
                "sell premium when rich, buy it when cheap."
            ),
            "surface_notes": notes,
            "selected_playbooks": selected,
            "commentary": "Deterministic offline selection — no Claude key configured.",
        }

    @staticmethod
    def _sells_premium(name: str, playbook_type: str | None) -> bool:
        """Whether a playbook is net short premium.

        Driven by the playbook's declared ``type``. The name heuristic survives
        only as a fallback for a playbook that declares no type — reading intent
        out of a name is what mislabelled a `vertical_credit` as premium-buying.
        """
        if playbook_type:
            return playbook_type in PREMIUM_SELLING_TYPES
        return "credit" in name or "condor" in name or "butterfly" in name

    @staticmethod
    def _iv_vs_realised(surface: dict[str, Any], context: dict[str, Any], ticker: str) -> str:
        """Compare implied to realised vol, when both are available."""
        implied = surface.get("atm_iv")
        realised = (context.get("indicators", {}).get(ticker) or {}).get("realised_vol_20d")
        if not implied or not realised:
            return "fair"
        if implied > realised * 1.15:
            return "rich"
        if implied < realised * 0.85:
            return "cheap"
        return "fair"

    def build_structures(
        self,
        snapshot: dict[str, Any],
        playbooks: list[dict[str, Any]],
        selected: list[str] | None = None,
        max_per_ticker: int = 1,
        positions: list[dict[str, Any]] | None = None,
        days_to_event: int | None = None,
    ) -> list[dict[str, Any]]:
        """Build and validate every viable structure across the watchlist."""
        allowed = set(selected) if selected else {p["name"] for p in playbooks}
        structures: list[dict[str, Any]] = []
        long_exposure = {
            str(p.get("underlying", "")).upper()
            for p in (positions or [])
            if float(p.get("qty", 0) or 0) > 0
        }

        for ticker, data in sorted(snapshot.items()):
            chain = data.get("chain") or []
            spot = data.get("spot") or (data.get("indicators") or {}).get("last_close")
            if not chain or not spot:
                continue

            context = {
                "iv_rank": (data.get("vol_surface") or {}).get("iv_rank"),
                "term_structure": (data.get("vol_surface") or {}).get("term_structure"),
                "adx": (data.get("indicators") or {}).get("adx"),
                "has_long_exposure": ticker in long_exposure,
                "days_to_event": days_to_event,
                "near_support": self._near_support(data),
            }

            built = 0
            for playbook in playbooks:
                if built >= max_per_ticker:
                    break
                if playbook["name"] not in allowed:
                    continue
                permitted, unmet = self.builder.conditions_met(playbook, context)
                if not permitted:
                    logger.info(
                        "playbook_conditions_unmet",
                        extra={
                            "event": "playbook_conditions_unmet",
                            "ticker": ticker,
                            "playbook": playbook["name"],
                            "unmet": unmet,
                        },
                    )
                    continue
                structure = self.builder.build(
                    ticker,
                    playbook,
                    chain,
                    float(spot),
                    realised_vol=(data.get("indicators") or {}).get("realised_vol_20d"),
                )
                if structure is None:
                    continue
                valid, problems = self.builder.validate(structure)
                structure["valid"] = valid
                structure["validation_problems"] = problems
                if valid:
                    structure["limit_price"] = self.builder.limit_price(structure)
                    structures.append(structure)
                    built += 1
                else:
                    logger.info(
                        "structure_rejected",
                        extra={
                            "event": "structure_rejected",
                            "ticker": ticker,
                            "playbook": playbook["name"],
                            "problems": problems,
                        },
                    )
        return structures


    @staticmethod
    def _near_support(data: dict[str, Any]) -> bool:
        """True when spot sits in the lower third of its 52-week range."""
        position = (data.get("indicators") or {}).get("pct_of_52w_range")
        return position is not None and position <= 0.35
