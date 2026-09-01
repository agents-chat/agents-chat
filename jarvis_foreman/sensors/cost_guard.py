"""cost_guard — Junior watches the daily token/cost budget.

Sums *today's* model usage from the live SQLite `token_usage` table (read-only,
since local midnight) and compares it to a budget:

  * If ``daily_budget_usd`` is configured, the meter is DOLLARS. The table has no
    cost column, so spend is derived from token counts via a per-provider price
    map (USD per million tokens); see ``prices_usd_per_mtok`` / ``default_price_usd_per_mtok``.
  * Otherwise the meter is TOKENS, against ``daily_token_budget`` (default 5M).

Severity: red at >= 100% of budget, amber at >= ``amber_pct`` (default 80) %, else green.

Dedupe discipline (see sensors/base.Signal): the dollar/token number churns every
poll, so the key names the *condition* — "cost:over_budget" / "cost:approaching" /
"cost:ok" — never the amount. Which agents are burning the budget lives in
``detail.offenders`` for the HUD card + Senior context.
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

from .base import Sensor, Signal

# Reuse the automation sensor's DB resolver so every sensor reads the same store.
try:
    from .automation_pulse import _default_db  # type: ignore
except Exception:  # pragma: no cover - defensive
    import os

    def _default_db() -> Path:
        env = os.environ.get("AGENT_CHAT_DB")
        if env:
            return Path(env)
        root = Path(__file__).resolve().parent.parent.parent
        for name in ("data/neuroblend_v5_15.sqlite3", "data/neuroblend_v5_13.sqlite3"):
            p = root / name
            if p.is_file() and p.stat().st_size > 0:
                return p
        return root / "data" / "neuroblend_v5_15.sqlite3"


# Rough public list prices (USD per MILLION tokens), keyed by lowercased provider.
# Only consulted when a dollar budget is set; config `prices_usd_per_mtok` overrides
# any entry, and unknown providers fall back to `default_price_usd_per_mtok`. These
# are approximations for a spend *estimate*, not billing truth.
_DEFAULT_PRICES: dict[str, dict[str, float]] = {
    "openai": {"in": 2.50, "out": 10.00},
    "anthropic": {"in": 3.00, "out": 15.00},
    "google": {"in": 0.30, "out": 2.50},
    "xai": {"in": 2.00, "out": 10.00},
}


def _local_midnight_ms() -> int:
    """Epoch milliseconds at the start of today in the host's local timezone."""
    now = datetime.datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp() * 1000)


def _fmt_tokens(n: float) -> str:
    n = float(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{int(n)}"


class CostGuardSensor(Sensor):
    id = "cost_guard"

    def poll(self) -> list[Signal]:
        sc = self.cfg.sensor_cfg(self.id)
        db = Path(sc.get("db_path") or _default_db())
        if not db.is_file():
            return [Signal(self.id, "green", "cost_guard: no db yet",
                           dedupe_key="cost:ok", detail={"note": "no db yet", "db": str(db)})]

        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
            conn.row_factory = sqlite3.Row
            try:
                return self._evaluate(conn, sc)
            finally:
                conn.close()
        except Exception as e:  # never raise out of poll(): degrade to a single amber
            return [Signal(self.id, "amber", "cost_guard check failed",
                           dedupe_key="cost:check_failed", detail={"error": str(e)})]

    def _evaluate(self, conn, sc) -> list[Signal]:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='token_usage'"
        ).fetchone()
        if not row:
            return [Signal(self.id, "green", "cost_guard: no usage table",
                           dedupe_key="cost:ok",
                           detail={"note": "token_usage table absent"})]

        since_ms = _local_midnight_ms()
        # `ts` is epoch MILLISECONDS in this DB, but normalize defensively: a seconds
        # value (~1.7e9) is < 1e11 and would silently miss the window, so scale it up.
        rows = conn.execute(
            """
            SELECT COALESCE(NULLIF(agent_id, ''), 'unknown') AS agent_id,
                   lower(COALESCE(provider, ''))             AS provider,
                   SUM(COALESCE(input_tokens, 0))            AS input_tokens,
                   SUM(COALESCE(output_tokens, 0))           AS output_tokens,
                   SUM(COALESCE(cached_tokens, 0))           AS cached_tokens,
                   SUM(COALESCE(reasoning_tokens, 0))        AS reasoning_tokens
            FROM token_usage
            WHERE (CASE WHEN ts < 100000000000 THEN ts * 1000 ELSE ts END) >= ?
            GROUP BY agent_id, provider
            """,
            (since_ms,),
        ).fetchall()

        # Pricing (USD per million tokens); config overrides the module defaults.
        prices = dict(_DEFAULT_PRICES)
        prices.update({str(k).lower(): v for k, v in (sc.get("prices_usd_per_mtok") or {}).items()})
        default_rate = float(sc.get("default_price_usd_per_mtok", 1.0))

        def _cost(provider: str, inp: float, out: float, cached: float, reason: float) -> float:
            p = prices.get(provider) or prices.get("default") or {}
            in_rate = float(p.get("in", default_rate))
            out_rate = float(p.get("out", p.get("in", default_rate)))
            cached_rate = float(p.get("cached", in_rate))
            # `cached` is a SUBSET of `inp` (the portion served from prompt cache), so
            # bill the fresh part at in_rate and the cached part at the discounted rate.
            # reasoning is billed at output rate.
            fresh_in = max(inp - cached, 0.0)
            return (fresh_in * in_rate + (out + reason) * out_rate + cached * cached_rate) / 1_000_000.0

        per_agent: dict[str, dict] = {}
        total_tokens = 0.0
        total_cost = 0.0
        for r in rows:
            inp = float(r["input_tokens"] or 0)
            out = float(r["output_tokens"] or 0)
            cached = float(r["cached_tokens"] or 0)
            reason = float(r["reasoning_tokens"] or 0)
            # Core meter = FRESH input + output + reasoning. `cached` is a subset of
            # `input_tokens` (prompt-cache reads, ~free), so subtract it — otherwise a
            # large-context resume that is 95%+ cache reads inflates the meter several-fold
            # and trips a false RED even though almost nothing was actually spent.
            fresh_in = max(inp - cached, 0.0)
            toks = fresh_in + out + reason
            cost = _cost(r["provider"], inp, out, cached, reason)
            total_tokens += toks
            total_cost += cost
            a = r["agent_id"]
            slot = per_agent.setdefault(a, {"agent": a, "tokens": 0.0, "cost": 0.0})
            slot["tokens"] += toks
            slot["cost"] += cost

        # Budget mode: dollars if a USD budget is set, else tokens.
        budget_usd = sc.get("daily_budget_usd", None)
        amber_pct = float(sc.get("amber_pct", 80))
        top_n = int(sc.get("top_n", 5))

        if budget_usd is not None:
            mode = "usd"
            budget = float(budget_usd)
            actual = total_cost
            actual_str = f"${actual:,.2f}"
            budget_str = f"${budget:,.2f}"
        else:
            mode = "tokens"
            budget = float(sc.get("daily_token_budget", 5_000_000))
            actual = total_tokens
            actual_str = _fmt_tokens(actual)
            budget_str = _fmt_tokens(budget)

        # A zero/negative budget is a misconfiguration, not an incident — stay green.
        if budget <= 0:
            return [Signal(self.id, "green", "cost_guard: no budget set",
                           dedupe_key="cost:ok",
                           detail={"note": "budget <= 0", "mode": mode,
                                   "spend_usd": round(total_cost, 4),
                                   "total_tokens": int(total_tokens)})]

        pct = actual / budget
        if pct >= 1.0:
            sev, dedupe_key = "red", "cost:over_budget"
        elif pct >= amber_pct / 100.0:
            sev, dedupe_key = "amber", "cost:approaching"
        else:
            sev, dedupe_key = "green", "cost:ok"

        # Offenders ranked by whatever the budget meters (cost in USD mode, tokens else).
        rank_key = "cost" if mode == "usd" else "tokens"
        ranked = sorted(per_agent.values(), key=lambda d: d[rank_key], reverse=True)
        offenders: list[str] = []
        for a in ranked[:top_n]:
            if a["tokens"] <= 0:
                continue
            offenders.append(
                f"{a['agent']} — ${a['cost']:,.2f} ({_fmt_tokens(a['tokens'])} tok)"
            )

        metrics = {
            "mode": mode,
            "budget": budget,
            "actual": round(actual, 4),
            "pct": round(pct * 100.0, 1),
            "amber_pct": amber_pct,
            "spend_usd": round(total_cost, 4),
            "total_tokens": int(total_tokens),
            "since_ms": since_ms,
            "per_agent": [
                {"agent": a["agent"], "tokens": int(a["tokens"]), "cost": round(a["cost"], 4)}
                for a in ranked
            ],
        }

        headline = f"cost {actual_str} / {budget_str} ({pct * 100.0:.0f}% of daily budget)"
        return [Signal(
            self.id, sev, headline[:220], dedupe_key=dedupe_key,
            detail={"metrics": metrics, "offenders": offenders},
        )]
