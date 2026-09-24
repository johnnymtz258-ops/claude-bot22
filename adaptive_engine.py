"""Adaptive decision support for Fomo Bot v11.

This module is deliberately dependency-free. It does not predict the future or place
orders. It turns the bot's own historical signal/evaluation database plus current
market structure into calibrated, explainable confidence, sizing guidance and a
position-state machine.
"""
from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass
from typing import Any


def f(value: Any, default: float = 0.0) -> float:
    try:
        return float(default if value is None else value)
    except Exception:
        return float(default)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def nest(data: dict, *keys, default=None):
    cur = data
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


def pair_metrics(pair: dict) -> dict:
    mcap = f(pair.get("marketCap") or pair.get("fdv"))
    liq = f(nest(pair, "liquidity", "usd", default=0))
    v5 = f(nest(pair, "volume", "m5", default=0))
    buys = f(nest(pair, "txns", "m5", "buys", default=0))
    sells = f(nest(pair, "txns", "m5", "sells", default=0))
    return {
        "price": f(pair.get("priceUsd")),
        "mcap": mcap,
        "liq": liq,
        "v5": v5,
        "buys": buys,
        "sells": sells,
        "swaps": buys + sells,
        "ratio": buys / max(sells, 1.0),
        "turnover": 100.0 * v5 / max(mcap, 1.0),
        "liq_mc": liq / max(mcap, 1.0),
        "pc5": f(nest(pair, "priceChange", "m5", default=0)),
        "pc1": f(nest(pair, "priceChange", "h1", default=0)),
        "pc6": f(nest(pair, "priceChange", "h6", default=0)),
        "pc24": f(nest(pair, "priceChange", "h24", default=0)),
    }


def _bayes_rate(successes: int, n: int, prior: float = 0.50, strength: float = 6.0) -> float:
    """Small-sample-safe empirical rate."""
    return (successes + prior * strength) / max(n + strength, 1.0)


def _query_returns(conn, where_sql: str, params: tuple, checkpoint: int, limit: int = 160) -> list[float]:
    sql = f"""select e.return_pct
        from evals e join signals s on s.id=e.signal_id
        where s.kind='EARLY' and e.checkpoint_min=? and {where_sql}
        order by s.ts desc limit ?"""
    rows = conn.execute(sql, (checkpoint, *params, int(limit))).fetchall()
    return [f(r[0]) for r in rows]


def empirical_cohort(conn, tier: str, score: float, chain: str = "solana") -> dict:
    """Use only *previously completed* signal endpoints to calibrate current confidence.

    We blend exact action/tier history with a score-band cohort. The values are endpoint
    statistics, not claims that a target was reached before a stop.
    """
    tier = str(tier or "ENTRY OPTION")
    chain = str(chain or "").lower()
    score = f(score)
    lo, hi = max(0.0, score - 10.0), min(100.0, score + 10.0)

    tier30 = _query_returns(conn, "s.action=? and lower(s.chain)=?", (tier, chain), 30)
    tier120 = _query_returns(conn, "s.action=? and lower(s.chain)=?", (tier, chain), 120)
    band30 = _query_returns(conn, "s.score between ? and ? and lower(s.chain)=?", (lo, hi, chain), 30)
    band120 = _query_returns(conn, "s.score between ? and ? and lower(s.chain)=?", (lo, hi, chain), 120)

    # Prefer tier history but gracefully backfill with score-band history.
    vals30 = tier30 if len(tier30) >= 8 else (tier30 + band30)[:160]
    vals120 = tier120 if len(tier120) >= 8 else (tier120 + band120)[:160]

    n30 = len(vals30)
    n120 = len(vals120)
    positive30 = _bayes_rate(sum(v > 0 for v in vals30), n30)
    positive120 = _bayes_rate(sum(v > 0 for v in vals120), n120)
    hit15_120 = _bayes_rate(sum(v >= 15 for v in vals120), n120, prior=0.20, strength=8)
    severe15_120 = _bayes_rate(sum(v <= -15 for v in vals120), n120, prior=0.25, strength=8)
    ruglike120 = _bayes_rate(sum(v <= -60 for v in vals120), n120, prior=0.08, strength=12)

    med30 = statistics.median(vals30) if vals30 else None
    med120 = statistics.median(vals120) if vals120 else None
    return {
        "n30": n30,
        "n120": n120,
        "positive30": positive30,
        "positive120": positive120,
        "hit15_120": hit15_120,
        "severe15_120": severe15_120,
        "ruglike120": ruglike120,
        "median30": med30,
        "median120": med120,
        "sample": max(n30, n120),
    }



def path_cohort(conn, tier: str, chain: str = "solana", horizon: int = 60) -> dict:
    """Path-aware outcomes when v11 has accumulated them.

    Unlike endpoint evals, this can distinguish a clean runner from a coin that touched
    profit but also suffered a severe drawdown inside the same horizon.
    """
    try:
        exists=conn.execute("select 1 from sqlite_master where type='table' and name='signal_outcomes'").fetchone()
        if not exists:
            return {"n":0,"clean15":0.5,"severe15":0.25,"ruglike":0.08,"median_max":None,"median_min":None}
        rows=conn.execute("""select o.max_return_pct,o.min_return_pct
            from signal_outcomes o join signals s on s.id=o.signal_id
            where s.kind='EARLY' and s.action=? and lower(s.chain)=? and o.horizon_min=?
            order by s.ts desc limit 160""",(str(tier or 'ENTRY OPTION'),str(chain or '').lower(),int(horizon))).fetchall()
    except Exception:
        rows=[]
    vals=[(f(r[0]),f(r[1])) for r in rows]
    n=len(vals)
    clean=sum(mx>=15 and mn>-10 for mx,mn in vals)
    severe=sum(mn<=-15 for _,mn in vals)
    rug=sum(mn<=-60 for _,mn in vals)
    return {
        "n":n,
        "clean15":_bayes_rate(clean,n,prior=0.25,strength=8),
        "severe15":_bayes_rate(severe,n,prior=0.25,strength=8),
        "ruglike":_bayes_rate(rug,n,prior=0.08,strength=12),
        "median_max":statistics.median([x[0] for x in vals]) if vals else None,
        "median_min":statistics.median([x[1] for x in vals]) if vals else None,
    }


def live_quality(pair: dict, tier: str = "") -> float:
    m = pair_metrics(pair)
    # 0–100. Each component is bounded so an absurd buy/sell ratio cannot dominate.
    q = 0.0
    q += clamp(math.log10(max(m["liq"], 1.0) / 25000.0 + 1.0) * 22.0, 0, 22)
    q += clamp((m["liq_mc"] - 0.04) / 0.22 * 15.0, 0, 15)
    q += clamp((m["ratio"] - 1.0) / 1.5 * 18.0, 0, 18)
    q += clamp(math.log10(max(m["swaps"], 1.0)) / math.log10(500.0) * 15.0, 0, 15)
    q += clamp(math.log10(max(m["turnover"], 0.01) / 0.08 + 1.0) * 12.0, 0, 12)

    # Reward positive-but-not-euphoric motion; penalize falling knives and late chases.
    if -2 <= m["pc5"] <= 7:
        q += 10
    elif m["pc5"] < -8 or m["pc5"] > 15:
        q -= 10
    if -8 <= m["pc1"] <= 25:
        q += 8
    elif m["pc1"] < -20 or m["pc1"] > 60:
        q -= 12

    if str(tier).upper() in {"MICRO ENTRY", "FRESH ENTRY"}:
        q -= 4
    return clamp(q, 0, 100)


def confidence_report(conn, pair: dict, tier: str, score: float, base_position_usd: float = 5.0) -> dict:
    m = pair_metrics(pair)
    chain=str(pair.get("chainId") or "solana")
    cohort = empirical_cohort(conn, tier, score, chain)
    paths = path_cohort(conn, tier, chain, 60)
    live = live_quality(pair, tier)
    raw_score = clamp(f(score), 0, 100)

    # The legacy score is a screening rank, not a probability. Chronological holdout
    # data has not shown a stable monotonic relationship at every horizon, so v11
    # intentionally weights live structure more heavily than the raw score.
    endpoint_sample_weight = clamp(cohort["sample"] / 35.0, 0.0, 1.0)
    path_sample_weight = clamp(paths["n"] / 30.0, 0.0, 1.0)
    endpoint_quality = 100.0 * (
        0.40 * cohort["positive30"]
        + 0.30 * cohort["positive120"]
        + 0.30 * (1.0 - cohort["severe15_120"])
    )
    path_quality = 100.0 * (0.55*paths["clean15"] + 0.45*(1.0-paths["severe15"]))
    empirical_quality=(endpoint_quality*(1.0-path_sample_weight*0.45) + path_quality*(path_sample_weight*0.45))
    static = 0.25 * raw_score + 0.75 * live
    hist_weight=0.38*endpoint_sample_weight
    confidence = (1.0 - hist_weight) * static + hist_weight * empirical_quality
    confidence = clamp(confidence, 5, 95)

    blended_severe=(cohort["severe15_120"]*(1.0-path_sample_weight*0.45) + paths["severe15"]*(path_sample_weight*0.45))
    downside = clamp(100.0 * blended_severe, 2, 95)
    hit15 = clamp(100.0 * cohort["hit15_120"], 1, 90)

    if confidence >= 82:
        grade, mult = "HIGH", 1.25
    elif confidence >= 70:
        grade, mult = "GOOD", 1.00
    elif confidence >= 58:
        grade, mult = "MODERATE", 0.75
    else:
        grade, mult = "SPECULATIVE", 0.50

    tier_u = str(tier or "").upper()
    if tier_u in {"MICRO ENTRY", "FRESH ENTRY", "RE-ENTRY OPTION", "REVERSAL ENTRY"}:
        mult = min(mult, 0.75)
    if downside >= 40:
        mult = min(mult, 0.50)
    elif downside >= 30:
        mult = min(mult, 0.75)

    # v11.3: the Aug-29 holdout showed that the adaptive number is still a learning
    # signal, not a calibrated probability. Until both endpoint and path cohorts are
    # reasonably populated, sizing guidance is reduction-only and can never recommend
    # more than the configured base amount.
    calibration_mature = bool(cohort["sample"] >= 35 and paths["n"] >= 30)
    calibration_status = "MATURE" if calibration_mature else "LEARNING"
    if not calibration_mature:
        mult = min(mult, 1.00)

    base_position_usd = max(f(base_position_usd, 5.0), 0.01)
    suggested = round(base_position_usd * mult, 2)
    return {
        "confidence": confidence,
        "grade": grade,
        "live_quality": live,
        "historical": cohort,
        "path_history": paths,
        "endpoint_hit15_120": hit15,
        "endpoint_severe15_120": downside,
        "size_multiplier": mult,
        "suggested_usd": suggested,
        "base_usd": base_position_usd,
        "calibration_status": calibration_status,
        "calibration_mature": calibration_mature,
        "features": m,
    }


def _round_size_usd(value: float) -> float:
    """Human-friendly dollar sizing: whole dollars below $50, $5 steps above."""
    value=max(0.0,f(value))
    if value < 50:
        return float(max(1, round(value)))
    return float(max(5, round(value/5.0)*5.0))


def dollar_size_guide(conn, pair: dict, tier: str, score: float,
                      min_usd: float = 5.0, normal_max_usd: float = 20.0,
                      exceptional_max_usd: float = 30.0, open_positions: int = 0,
                      realized_24h: float = 0.0, losing_streak: int = 0,
                      late_move_pct: float = 0.0, market_regime: str = "NEUTRAL",
                      flow_concentration: float = 0.0, social_quality: float = 0.0) -> dict:
    """Convert adaptive evidence into an explainable *advisory* dollar amount.

    The user normally trades a small dollar range, so this avoids abstract multipliers.
    It never blocks a manual buy. Bigger-than-normal sizing is deliberately difficult:
    it requires mature path calibration plus unusually clean live structure. This keeps
    one flashy score from turning into oversized exposure while the model is learning.
    """
    min_usd=max(1.0,f(min_usd,5.0))
    normal_max_usd=max(min_usd,f(normal_max_usd,20.0))
    exceptional_max_usd=max(normal_max_usd,f(exceptional_max_usd,30.0))
    cal=confidence_report(conn,pair,tier,score,min_usd)
    m=cal.get('features') or pair_metrics(pair)
    conf=f(cal.get('confidence'))
    live=f(cal.get('live_quality'))
    downside=f(cal.get('endpoint_severe15_120'),50.0)
    path=cal.get('path_history') or {}
    path_n=int(path.get('n') or 0)
    clean=f(path.get('clean15'),0.25)
    severe=f(path.get('severe15'),0.25)
    quality=0.68*live+0.32*conf
    reasons=[]

    # Dollar buckets match the user's practical $5-$20 workflow. All actionable
    # alerts already passed the hard scanner, so even the lowest bucket stays at min.
    span=max(normal_max_usd-min_usd,0.0)
    if quality < 52:
        target=min_usd; label='STARTER'
    elif quality < 60:
        target=min_usd+0.20*span; label='LIGHT'
    elif quality < 68:
        target=min_usd+0.45*span; label='NORMAL'
    elif quality < 76:
        target=min_usd+0.70*span; label='UPPER RANGE'
    else:
        target=normal_max_usd; label='MAX NORMAL'

    # Historical downside caps sizing even when live flow looks exciting. v11.3 data
    # showed that STRONG/FLOW labels alone were not reliable enough to justify size-up.
    if downside >= 48:
        target=min(target,min_usd+0.20*span); reasons.append('comparable history has high severe-drawdown rate')
    elif downside >= 40:
        target=min(target,min_usd+0.40*span); reasons.append('comparable history still has elevated downside')
    elif downside >= 32:
        target=min(target,min_usd+0.65*span)

    tier_u=str(tier or '').upper()
    if tier_u in {'MICRO ENTRY','FRESH ENTRY','RE-ENTRY OPTION','REVERSAL ENTRY','FAST ENTRY'}:
        target=min(target,min_usd+0.35*span); reasons.append(f'{tier_u.lower()} stays smaller by design')
    elif tier_u == 'FLOW ENTRY' and path_n < 20:
        target=min(target,min_usd+0.45*span); reasons.append('FLOW history is still immature')

    # Late-but-still-valid entries get smaller, never larger. This is used by the
    # automatic freshness updates when the user sees Telegram a couple minutes late.
    late=max(0.0,f(late_move_pct))
    if late > 2.0:
        late_mult=clamp(1.0-(late-2.0)*0.16,0.55,1.0)
        target*=late_mult; reasons.append(f'price is {late:+.1f}% above the original alert')

    # Portfolio/daily context is advisory only: no lock, no martingale size-up.
    if int(open_positions) >= 5:
        target*=0.70; reasons.append('several positions are already open')
    elif int(open_positions) >= 3:
        target*=0.82; reasons.append('multiple positions are already open')
    elif int(open_positions) >= 2:
        target*=0.92
    if f(realized_24h) <= -10:
        target*=0.70; reasons.append('recent realized P/L is in a drawdown')
    elif f(realized_24h) <= -5:
        target*=0.85; reasons.append('recent realized P/L is negative')
    if int(losing_streak) >= 3:
        target*=0.80; reasons.append('recent losing streak: size down, do not chase losses')
    elif int(losing_streak) >= 2:
        target*=0.90

    # v16.1 regime-aware sizing. Regime changes SIZE, not signal eligibility.
    # RISK_ON gets only a modest increase and never bypasses configured dollar caps;
    # broad weakness sizes down instead of silencing otherwise valid opportunities.
    regime=str(market_regime or 'NEUTRAL').upper()
    if regime == 'STRESS':
        target*=0.60; reasons.append('Solana market regime is stressed: 0.60x size')
    elif regime == 'RISK_OFF':
        target*=0.78; reasons.append('Solana market regime is risk-off: 0.78x size')
    elif regime == 'RISK_ON':
        target*=1.10; reasons.append('Solana market regime is risk-on: 1.10x size')
    elif regime == 'UNKNOWN':
        target*=0.85; reasons.append('Solana market regime is unknown: 0.85x size')

    concentration=clamp(f(flow_concentration),0.0,1.0)
    if concentration >= 0.75:
        target*=0.72; reasons.append('recent buy dollars are dominated by one wallet')
    elif concentration >= 0.55:
        target*=0.85; reasons.append('recent buy flow is somewhat concentrated')

    # Exact-contract social breadth is useful corroboration, but social hype alone
    # never increases the normal/exceptional cap. It can only prevent an otherwise
    # strong setup from being undersized by a tiny amount once calibration is mature.
    social=f(social_quality)
    if social < 0:
        target*=0.90; reasons.append('social mentions are concentrated/spam-like')

    # Exceptional > normal-max is enabled only when the bot has enough path evidence
    # and that evidence is actually cleaner than severe. This is the safe answer to
    # "go above $20 when it is really strong" rather than trusting a legacy score.
    exceptional_live=(
        cal.get('calibration_mature') and path_n >= 30 and clean > severe
        and quality >= 82 and downside <= 25
        and f(m.get('liq')) >= 100000 and f(m.get('liq_mc')) >= 0.10
        and f(m.get('ratio')) >= 1.40 and f(m.get('swaps')) >= 25
        and f(m.get('turnover')) >= 0.20
        and -1.0 <= f(m.get('pc5')) <= 6.0 and -5.0 <= f(m.get('pc1')) <= 22.0
        and tier_u not in {'MICRO ENTRY','FRESH ENTRY','RE-ENTRY OPTION','REVERSAL ENTRY','FLOW ENTRY','FAST ENTRY'}
        and late <= 2.0 and int(open_positions) < 3 and f(realized_24h) > -5
        and regime not in {'RISK_OFF','STRESS'} and concentration < 0.55
    )
    if exceptional_live:
        strength=clamp((quality-82.0)/10.0,0.0,1.0)
        target=max(target,normal_max_usd+(exceptional_max_usd-normal_max_usd)*(0.45+0.55*strength))
        label='EXCEPTIONAL'
        reasons.append('mature history + unusually clean live structure')
    elif target >= normal_max_usd*0.95:
        label='MAX NORMAL'

    target=clamp(_round_size_usd(target),min_usd,exceptional_max_usd if exceptional_live else normal_max_usd)
    if not exceptional_live:
        frac=(target-min_usd)/max(normal_max_usd-min_usd,1e-9)
        if frac <= 0.10: label='STARTER'
        elif frac <= 0.32: label='LIGHT'
        elif frac <= 0.62: label='NORMAL'
        elif frac <= 0.88: label='UPPER RANGE'
        else: label='MAX NORMAL'
    # Keep compatibility fields from confidence_report while replacing the old abstract
    # base-multiplier suggestion with the user-facing dollar recommendation.
    cal.update({
        'suggested_usd': target,
        'size_label': label,
        'size_min_usd': min_usd,
        'size_normal_max_usd': normal_max_usd,
        'size_exceptional_max_usd': exceptional_max_usd,
        'exceptional_eligible': bool(exceptional_live),
        'open_positions_context': int(open_positions),
        'realized_24h_context': f(realized_24h),
        'losing_streak_context': int(losing_streak),
        'late_move_pct': late,
        'market_regime': regime,
        'flow_concentration': concentration,
        'social_quality': social,
        'size_reasons': reasons,
    })
    cal['size_multiplier']=target/max(min_usd,1e-9)
    cal['base_usd']=min_usd
    return cal


STATE_ORDER = {
    "ENTRY": 0,
    "CONFIRMED": 1,
    "PROFITABLE": 2,
    "TAKE_PARTIAL": 3,
    "RUNNER": 4,
    "WEAKENING": 5,
    "EXIT_REVIEW": 6,
}


def position_state(pair: dict, position: dict, tp1: float = 15.0, tp2: float = 35.0,
                   risk_line: float = 10.0, trailing: float = 10.0,
                   liquidity_exit: float = 30.0, round_trip_friction: float = 5.0,
                   exit_friction: float = 2.5, small_position_usd: float = 15.0,
                   small_hard_stop: float = 20.0, mid_hard_stop: float = 16.0,
                   min_partial_sale_usd: float = 5.0, capital_hard_stop: float = 8.0) -> dict:
    """Fee-aware manual position state.

    v14 CAPITAL FIRST treats fees as a reason to demand better entries/profit targets,
    not as a reason to tolerate a 16-20% loss. Small positions use the caller's capped
    hard line, and profitable positions get an explicit high-water profit lock.
    """
    m = pair_metrics(pair)
    current = m["price"]
    entry = max(f(position.get("entry_price")), 1e-18)
    peak = max(f(position.get("peak_price"), entry), current)
    ret = (current / entry - 1.0) * 100.0
    drawdown = (current / max(peak, 1e-18) - 1.0) * 100.0
    peak_ret = (peak / entry - 1.0) * 100.0
    entry_liq = max(f(position.get("entry_liquidity")), 1.0)
    liq_drop = (1.0 - m["liq"] / entry_liq) * 100.0
    age_min = max(0.0, (time.time() - f(position.get("open_ts"))) / 60.0)
    original_amount = max(f(position.get("amount_usd")), 0.0)
    remaining_fraction = clamp(f(position.get("remaining_fraction"),1.0),0.0,1.0)
    amount = original_amount * remaining_fraction

    # Approximate all-in result if the user exits now. For a partially sold position,
    # all dollar guidance is based on the remaining stake rather than the original size. This is intentionally labeled
    # an estimate because route/network fees and slippage vary.
    net_ret_est = ret - max(0.0, f(round_trip_friction))
    gross_pnl_usd = amount * ret / 100.0
    net_pnl_est_usd = amount * net_ret_est / 100.0
    exit_cost_est_usd = max(0.0, amount * (1.0 + ret / 100.0)) * max(0.0, f(exit_friction)) / 100.0

    # v14 reverses the older fee logic: fees are NOT permission to tolerate a larger
    # percentage loss. Even if an older caller passes the legacy 16-20% hard lines,
    # the state machine caps price risk at the capital-first hard limit.
    capital_cap=clamp(f(capital_hard_stop,8.0),5.0,8.0)
    effective_risk_line=min(max(0.0,f(risk_line)),capital_cap)
    if amount > 0 and amount <= small_position_usd:
        price_hard_stop = min(capital_cap,max(effective_risk_line,f(small_hard_stop)))
        size_bucket = "SMALL"
    elif amount > 0 and amount <= max(25.0, small_position_usd):
        price_hard_stop = min(capital_cap,max(effective_risk_line,f(mid_hard_stop)))
        size_bucket = "MID"
    else:
        price_hard_stop = effective_risk_line
        size_bucket = "STANDARD"

    # Dynamic trail: as profits increase, tolerate less giveback.
    if ret >= 60:
        trail_now = min(trailing, 7.0)
    elif ret >= tp2:
        trail_now = min(trailing, 8.0)
    elif ret >= tp1:
        trail_now = min(trailing, 10.0)
    else:
        trail_now = trailing

    sellers_control = m["ratio"] < 0.85
    fast_down = m["pc5"] <= -4.0
    severe_fast_down = m["pc5"] <= -10.0 and m["ratio"] < 1.0
    broad_breakdown = m["pc1"] < -20.0 and m["ratio"] < 0.90
    structural_bad = sellers_control and fast_down
    emergency_structure = liq_drop >= liquidity_exit or severe_fast_down or (liq_drop >= 20 and sellers_control)
    # Capital-first high-water protection. A manual trader can always choose not to act,
    # but the bot must never describe a +20-35% winner that has broken its trail as a
    # mere recovery watch. This directly addresses the SOLLY giveback observed Aug-31.
    profit_lock_trigger = bool(
        (peak_ret >= 10.0 and drawdown <= -6.0)
        or (peak_ret >= 25.0 and drawdown <= -8.0)
        or (peak_ret >= max(10.0,tp1) and ret <= 2.0)
    )

    reasons = []
    # Structural danger is always urgent, regardless of a $5 or $500 position.
    if liq_drop >= liquidity_exit:
        state = "EXIT_REVIEW"; reasons.append(f"liquidity -{liq_drop:.1f}% vs entry")
    elif ret <= -price_hard_stop:
        state = "EXIT_REVIEW"; reasons.append(f"position {ret:+.1f}% beyond fee-aware hard line -{price_hard_stop:.0f}%")
    elif ret <= -effective_risk_line and (structural_bad or broad_breakdown or liq_drop >= 20):
        state = "EXIT_REVIEW"; reasons.append(f"position {ret:+.1f}% plus structure deterioration")
    elif ret <= -effective_risk_line:
        state = "WEAKENING"
        reasons.append(f"position {ret:+.1f}% crossed soft risk line, but structure has not confirmed an exit")
        reasons.append("recovery watch instead of panic-selling on price alone")
    elif age_min <= 15 and ret <= -5 and ((m["pc5"] <= -4 and m["ratio"] < 0.85) or liq_drop >= 10):
        state = "WEAKENING"
        reasons.append(f"early entry thesis weakening: {ret:+.1f}% with " +
                       (f"5m {m['pc5']:+.1f}% / B/S {m['ratio']:.2f}x" if m["pc5"] <= -4 and m["ratio"] < 0.85 else f"liquidity -{liq_drop:.1f}%"))
    elif profit_lock_trigger:
        state = "EXIT_REVIEW"
        reasons.append(f"capital-first profit lock: peak {peak_ret:+.1f}% / now {ret:+.1f}% / drawdown {drawdown:+.1f}%")
    elif ret > 0 and drawdown <= -trail_now:
        state = "WEAKENING"; reasons.append(f"{drawdown:+.1f}% from peak")
    elif severe_fast_down:
        state = "WEAKENING"; reasons.append(f"5m {m['pc5']:+.1f}% with B/S {m['ratio']:.2f}x")
    elif ret >= tp2:
        state = "RUNNER"; reasons.append(f"position {ret:+.1f}%")
    elif ret >= tp1:
        state = "TAKE_PARTIAL"; reasons.append(f"position {ret:+.1f}%")
    elif ret >= 5 and m["ratio"] >= 1.0:
        state = "PROFITABLE"; reasons.append(f"position {ret:+.1f}%")
    elif ret >= 1.5 and m["ratio"] >= 1.1 and m["pc5"] > -3:
        state = "CONFIRMED"; reasons.append(f"buyers/sellers {m['ratio']:.2f}x")
    else:
        state = "ENTRY"; reasons.append(f"position {ret:+.1f}%")

    # Aging + weak tape can escalate, but a small position in the soft-loss zone still
    # needs more than one weak statistic to trigger a hard exit.
    if state in {"ENTRY", "CONFIRMED"} and age_min >= 45 and ret < -5 and m["ratio"] < 0.95 and m["pc5"] < 0:
        state = "WEAKENING"
        reasons.append("aging trade with sellers controlling the tape")
    if state in {"ENTRY","CONFIRMED"} and age_min >= 30 and net_ret_est <= 0 and m["ratio"] < 1.10 and m["pc5"] <= 0:
        state = "WEAKENING"
        reasons.append(f"30m+ trade still below estimated break-even after friction ({net_ret_est:+.1f}% net est.)")
    if state == "WEAKENING" and ret > -effective_risk_line and (liq_drop >= 20 or broad_breakdown):
        state = "EXIT_REVIEW"
        reasons.append("multiple deterioration signals")

    # v12: if TP1 was reached but the user never recorded a partial, explicitly
    # protect against giving the entire profit window back. This is based on the live
    # ANTSEM pattern from the Aug-29 audit. Tiny positions that were too small for an
    # economical partial are excluded from this reminder.
    missed_partial_giveback=(
        int(f(position.get("tp1_sent"))) > 0 and remaining_fraction >= 0.95
        and amount >= max(1.0,f(min_partial_sale_usd,5.0))*1.5
        and peak_ret >= tp1 and ret < max(5.0,tp1*0.55) and drawdown <= -6.0
    )
    if missed_partial_giveback and state not in {"EXIT_REVIEW"}:
        state="WEAKENING"
        reasons.insert(0,f"TP1 was reached (~{peak_ret:+.1f}% peak) but no partial was recorded; most of the profit window has been given back")

    current_value=max(0.0,amount*(1.0+ret/100.0))
    min_partial=max(1.0,f(min_partial_sale_usd,5.0))
    partial_note=""

    # v11.4: partials are sized in both percentage AND approximate dollars. Tiny $1-$3
    # partials are often not worth the user's Fomo/network friction, so a healthy small
    # winner can wait for the next checkpoint instead of mechanically selling dust.
    strong_tape=(m["ratio"] >= 1.30 and m["pc5"] >= -1.0 and liq_drop < 12)
    if state == "TAKE_PARTIAL":
        if 0 < amount <= 25.0:
            partial = 100
            action = "CAPITAL-FIRST PROFIT LOCK: for a small Fomo position, close the full remainder once the fee-aware first target is reached rather than risking a round trip back through fees."
        else:
            partial = 50 if strong_tape else 60
            action = "Lock at least half the position at the first target; GUARDIAN will supervise the reduced runner."
    elif state == "RUNNER":
        if ret >= 60:
            # Approximate percentage required to pull the original dollars back out.
            partial = clamp(100.0/max(1.0+ret/100.0,1e-9),40,65)
            action = "Runner is large enough to consider pulling roughly your original stake back out and freerolling the rest."
        else:
            partial = 45 if strong_tape else 55
            action = "Lock more profit while keeping a meaningful runner."
    elif state == "WEAKENING" and ret <= -effective_risk_line and not emergency_structure:
        action = ("RECOVERY WATCH: do not panic-sell on the percentage dip alone. Reassess the next few minutes; "
                  "exit/reduce if sellers stay in control, momentum worsens, or liquidity starts draining.")
        partial = 0
    elif state == "WEAKENING":
        if missed_partial_giveback:
            action = "PROFIT GIVEBACK: TP1 was reached without a recorded partial. Consider reducing meaningfully now rather than letting the former winner turn into a loss."
            partial = 50
        else:
            action = "Tighten risk or reduce; do not let a winner/flat trade become a large loss."
            partial = 40 if ret > 0 else 25
    elif state == "EXIT_REVIEW":
        if profit_lock_trigger:
            action = "PROFIT LOCK TRIGGERED: the high-water trail broke. Consider closing the remaining position now instead of letting a former winner become a loss."
            partial = 100
        else:
            action = "Strongly consider exiting or materially reducing risk now; structure and/or the capital-first hard line has failed."
            partial = 100 if (ret <= -price_hard_stop or liq_drop >= liquidity_exit or emergency_structure) else 60
    elif state in {"PROFITABLE", "CONFIRMED"}:
        action = "Hold with active monitoring; no need to force an exit while structure is healthy."
        partial = 0
    else:
        action = "Give the setup room only while the original structure remains intact."
        partial = 0

    if partial and current_value > 0:
        # Round to a readable 5% increment and ensure a recommended partial is large
        # enough to be economically meaningful. For TP1, skip a partial if reaching
        # that minimum would require dumping most of a still-healthy tiny position.
        partial=5*round(float(partial)/5.0)
        needed=100.0*min_partial/current_value
        if state == "TAKE_PARTIAL" and needed > 60 and strong_tape:
            # On a tiny position the old behavior said "keep monitoring" because a
            # conventional 25-35% slice was economically pointless. The Aug-30 run
            # showed that this can turn a real +15% window back into a fee-negative
            # trade. Once the caller's fee-aware TP has been reached, banking the whole
            # small position is clearer than pretending a $1 partial is useful.
            partial_note=(f"Position value is only ~${current_value:.2f}; a normal partial is too small after fees. "
                          "The fee-aware profit threshold has been reached, so closing the small position is the cleaner realization choice.")
            partial=100
            action="Small-position profit lock: consider closing the full remaining position rather than taking a fee-heavy tiny partial."
        else:
            partial=max(float(partial),5.0*math.ceil(max(0.0,needed)/5.0))
            partial=clamp(partial,5,100)
    partial_usd=current_value*partial/100.0 if partial else 0.0
    remaining_value=max(0.0,current_value-partial_usd)

    return {
        "state": state, "ret": ret, "peak_ret": peak_ret, "drawdown": drawdown, "liq_drop": liq_drop,
        "age_min": age_min, "trail": trail_now, "action": action,
        "suggested_partial_pct": partial, "suggested_partial_usd": partial_usd,
        "remaining_value_usd": remaining_value, "current_value_usd": current_value,
        "partial_note": partial_note, "min_partial_sale_usd": min_partial,
        "reasons": reasons, "metrics": m,
        "amount_usd": amount, "original_amount_usd": original_amount, "remaining_fraction": remaining_fraction,
        "size_bucket": size_bucket, "price_hard_stop": price_hard_stop, "risk_line": effective_risk_line,
        "round_trip_friction_pct": max(0.0, f(round_trip_friction)),
        "exit_friction_pct": max(0.0, f(exit_friction)), "net_ret_est": net_ret_est,
        "gross_pnl_usd": gross_pnl_usd, "net_pnl_est_usd": net_pnl_est_usd,
        "exit_cost_est_usd": exit_cost_est_usd, "structural_bad": structural_bad,
        "missed_partial_giveback": missed_partial_giveback,
        "profit_lock_trigger": profit_lock_trigger,
    }

def transition_is_material(old_state: str | None, new_state: str) -> bool:
    old = str(old_state or "ENTRY")
    new = str(new_state or "ENTRY")
    if old == new:
        return False
    # Profit transitions and deterioration transitions are meaningful. A move back to
    # ENTRY after WEAKENING is not spam-worthy; CONFIRMED recovery is.
    important = {"TAKE_PARTIAL", "RUNNER", "WEAKENING", "EXIT_REVIEW", "CONFIRMED"}
    return new in important


def guardian_message(symbol: str, token: str, state_info: dict, horizon: dict | None = None,
                     confidence: dict | None = None) -> str:
    s = state_info
    horizon = horizon or {}
    confidence = confidence or {}
    icon = {
        "CONFIRMED": "✅", "PROFITABLE": "🟢", "TAKE_PARTIAL": "💰",
        "RUNNER": "🔥", "WEAKENING": "⚠️", "EXIT_REVIEW": "🚨", "ENTRY": "👀",
    }.get(s["state"], "👀")
    lines = [
        f"{icon} {symbol} — GUARDIAN: {s['state'].replace('_',' ')}",
        f"Position {s['ret']:+.1f}% | from peak {s['drawdown']:+.1f}% | liquidity change {-s['liq_drop']:+.1f}%",
    ]
    if s.get("amount_usd", 0) > 0 and s.get("round_trip_friction_pct", 0) > 0:
        lines.append(
            f"Fee-aware estimate: ~{s.get('net_ret_est', s['ret']):+.1f}% all-in "
            f"(~${s.get('net_pnl_est_usd',0):+.2f}) if closed near this quote; "
            f"assumed round-trip drag {s.get('round_trip_friction_pct',0):.1f}%"
        )
        if s.get("size_bucket") in {"SMALL", "MID"}:
            lines.append(f"Price-only hard line for this ${s.get('amount_usd',0):.2f} position: -{s.get('price_hard_stop',0):.0f}% (structural danger can override sooner)")
    if horizon:
        lines.append(f"Horizon: {horizon.get('label','')} — {horizon.get('window','')}")
    if confidence:
        lines.append(f"Setup confidence: {confidence.get('confidence',0):.0f}/100 ({confidence.get('grade','')})")
    lines.append("WHY: " + "; ".join(s.get("reasons", [])[:4]))
    lines.append("ACTION: " + s.get("action", "Review the position."))
    lines.append(f"Contract: {token}")
    if s.get("suggested_partial_pct"):
        pct=int(round(f(s.get('suggested_partial_pct'))))
        sale=f(s.get('suggested_partial_usd'))
        remain=f(s.get('remaining_value_usd'))
        if pct >= 100:
            lines.append(f"Suggested sale: FULL EXIT (~${sale:.2f} at this quote). After selling in Fomo, reply /sell {symbol}.")
        else:
            lines.append(f"Suggested partial: ~{pct}% (~${sale:.2f}); leave ~${remain:.2f} as the runner.")
            lines.append(f"After selling in Fomo, reply /sellpct {pct} (or the actual percent you sold).")
    elif s.get('partial_note'):
        lines.append("PARTIAL PLAN: " + str(s.get('partial_note')))
    return "\n".join(lines)



def _endpoint_summary(values: list[float]) -> dict:
    vals=[f(x) for x in values]
    if not vals:
        return {"n":0,"positive":None,"median":None,"mean":None,"hit15":None,"severe15":None}
    return {
        "n":len(vals),
        "positive":100.0*sum(v>0 for v in vals)/len(vals),
        "median":statistics.median(vals),
        "mean":statistics.mean(vals),
        "hit15":100.0*sum(v>=15 for v in vals)/len(vals),
        "severe15":100.0*sum(v<=-15 for v in vals)/len(vals),
    }


def walk_forward_summary(conn, checkpoint: int = 30, train_fraction: float = 0.70) -> dict:
    """Chronological holdout report for completed EARLY-signal endpoints.

    This deliberately does not tune live thresholds. It answers a narrower question:
    do the broad performance characteristics seen in older data persist in the newest,
    unseen slice? Keeping the holdout chronological makes daily review less vulnerable
    to fitting yesterday's winners.
    """
    rows=conn.execute("""select s.ts,s.action,s.score,e.return_pct
        from signals s join evals e on e.signal_id=s.id
        where s.kind='EARLY' and e.checkpoint_min=? and e.return_pct is not null
        order by s.ts asc""",(int(checkpoint),)).fetchall()
    rows=[(int(r[0]),str(r[1] or ''),f(r[2]),f(r[3])) for r in rows]
    n=len(rows)
    if n < 12:
        return {"checkpoint":int(checkpoint),"n":n,"ready":False,"reason":"need at least 12 completed chronological endpoints"}
    cut=max(6,min(n-4,int(n*clamp(train_fraction,0.5,0.85))))
    train=rows[:cut]; test=rows[cut:]
    train_s=_endpoint_summary([r[3] for r in train]); test_s=_endpoint_summary([r[3] for r in test])

    # Score segmentation is learned only from the training slice, then applied unchanged
    # to the later holdout. This is a sanity check, not an execution gate.
    train_scores=sorted(r[2] for r in train)
    score_cut=train_scores[max(0,min(len(train_scores)-1,int(0.65*(len(train_scores)-1))))]
    test_hi=[r[3] for r in test if r[2]>=score_cut]
    test_lo=[r[3] for r in test if r[2]<score_cut]
    hi_s=_endpoint_summary(test_hi); lo_s=_endpoint_summary(test_lo)

    # Current-action holdout summaries when sample size is useful.
    by_action={}
    for action in sorted(set(r[1] for r in test)):
        vals=[r[3] for r in test if r[1]==action]
        if len(vals)>=3:
            by_action[action]=_endpoint_summary(vals)

    drift=None
    if train_s["median"] is not None and test_s["median"] is not None:
        drift=test_s["median"]-train_s["median"]
    return {
        "checkpoint":int(checkpoint),"n":n,"ready":True,"train_n":len(train),"test_n":len(test),
        "train":train_s,"test":test_s,"score_cut":score_cut,"test_high_score":hi_s,
        "test_low_score":lo_s,"by_action":by_action,"median_drift":drift,
    }


def format_walk_forward(report: dict) -> str:
    if not report.get("ready"):
        return f"Walk-forward lab: not ready ({report.get('n',0)} endpoints; {report.get('reason','insufficient data')})."
    tr=report['train']; te=report['test']; hi=report['test_high_score']; lo=report['test_low_score']
    def fmt(x): return 'n/a' if x is None else f"{x:+.1f}%"
    lines=[
        f"Walk-forward {report['checkpoint']}m: train {report['train_n']} / unseen test {report['test_n']}",
        f"Older median {fmt(tr['median'])} → unseen median {fmt(te['median'])} | unseen positive {te['positive']:.0f}% | ≥+15 {te['hit15']:.0f}% | ≤-15 {te['severe15']:.0f}%",
    ]
    if hi.get('n',0)>=2:
        lines.append(f"Unseen score ≥{report['score_cut']:.0f}: n={hi['n']} median {fmt(hi['median'])} vs lower-score n={lo.get('n',0)} median {fmt(lo.get('median'))}")
    return "\n".join(lines)
