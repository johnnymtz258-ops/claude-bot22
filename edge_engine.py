"""v15 SOL EDGE ENGINE helpers.

Pure/stateless calculations live here so the trading loop can be regression-tested
without network calls.  Nothing in this module places or signs an order.
"""
from __future__ import annotations

import math
import statistics
from typing import Iterable, Mapping, Sequence


def _f(v, default=0.0):
    try:
        return float(default if v is None else v)
    except Exception:
        return float(default)


def setup_path_quality(history: Sequence[Mapping], current: Mapping,
                       min_observations: int = 3, min_span_seconds: int = 45,
                       max_chase_pct: float = 4.0, max_peak_drawdown_pct: float = 8.0,
                       max_liquidity_drop_pct: float = 5.0,
                       min_current_buy_sell: float = 1.15) -> dict:
    """Evaluate a candidate as a short path, not a one-frame screenshot.

    `history` is expected oldest->newest and may include the current quote.  The
    function deliberately does not decide whether a token is safe; it only measures
    path shape / persistence after the existing safety and stability layers.
    """
    rows=[dict(r) for r in history if _f(r.get("price")) > 0]
    price=_f(current.get("price"))
    liquidity=_f(current.get("liquidity"))
    buys=_f(current.get("buys")); sells=_f(current.get("sells"))
    ratio=buys/max(sells,1.0)
    if price<=0:
        return {"ok":False,"blocks":["no live price for path confirmation"],"mode":"UNKNOWN"}
    if not rows:
        return {"ok":False,"blocks":[f"setup path 0/{min_observations} observations"],"mode":"UNKNOWN"}

    # Avoid double-counting an identical current row while making the current price
    # part of the shape calculation.
    first_ts=int(_f(rows[0].get("ts")))
    last_ts=int(_f(rows[-1].get("ts")))
    span=max(0,last_ts-first_ts)
    prices=[_f(r.get("price")) for r in rows if _f(r.get("price"))>0]
    prices.append(price)
    first_price=prices[0]
    peak=max(prices); low=min(prices)
    move=(price/max(first_price,1e-18)-1)*100
    draw=(price/max(peak,1e-18)-1)*100
    recovery=(price/max(low,1e-18)-1)*100
    path_range=(peak/max(low,1e-18)-1)*100

    liqs=[_f(r.get("liquidity")) for r in rows if _f(r.get("liquidity"))>0]
    liq_base=statistics.median(liqs[:min(3,len(liqs))]) if liqs else liquidity
    liq_change=(liquidity/max(liq_base,1e-18)-1)*100 if liquidity>0 and liq_base>0 else 0.0

    ratios=[]
    for r in rows[-4:]:
        ratios.append(_f(r.get("buys5"))/max(_f(r.get("sells5")),1.0))
    ratios.append(ratio)
    persistent=sum(1 for x in ratios[-3:] if x>=1.05)

    blocks=[]
    if len(rows) < min_observations:
        blocks.append(f"setup path {len(rows)}/{min_observations} observations")
    if span < min_span_seconds:
        blocks.append(f"setup path {span}s/{min_span_seconds}s")
    if move > max_chase_pct:
        blocks.append(f"setup path already ran {move:+.1f}% from first confirmation")
    if draw < -abs(max_peak_drawdown_pct):
        blocks.append(f"setup path is {draw:.1f}% below its recent peak")
    if liq_change < -abs(max_liquidity_drop_pct):
        blocks.append(f"setup liquidity weakened {liq_change:+.1f}%")
    if ratio < min_current_buy_sell:
        blocks.append(f"current buyers/sellers {ratio:.2f}x < {min_current_buy_sell:.2f}x")
    if persistent < 2:
        blocks.append("buyer pressure was not persistent across the last three path snapshots")

    # Two acceptable shapes: controlled continuation or a modest pullback/reclaim.
    # This intentionally does not reward huge rebounds after a crash; the crash-memory
    # gate handles those separately.
    reclaim = path_range >= 2.0 and recovery >= 0.7 and draw >= -4.0
    continuation = path_range < 8.0 and draw >= -3.0 and -1.5 <= move <= max_chase_pct
    mode="RECLAIM" if reclaim else ("CONTINUATION" if continuation else "UNRESOLVED")
    if not (reclaim or continuation):
        blocks.append(f"setup has no controlled continuation/reclaim shape (range {path_range:.1f}%, recovery {recovery:.1f}%)")

    return {
        "ok":not blocks,"blocks":blocks,"mode":mode,"observations":len(rows),"span_seconds":span,
        "move_pct":move,"drawdown_from_peak_pct":draw,"recovery_from_low_pct":recovery,
        "range_pct":path_range,"liquidity_change_pct":liq_change,"buy_sell":ratio,
        "persistent_buy_snapshots":persistent,
    }


def breadth_regime(rows: Iterable[Mapping], min_tokens: int = 8) -> dict:
    """Classify broad meme-token breadth from each token's latest observation."""
    data=[dict(r) for r in rows]
    if len(data)<min_tokens:
        return {"label":"UNKNOWN","risk_off":False,"n":len(data),"reason":"not enough breadth observations"}
    pc5=[_f(r.get("pc5")) for r in data]
    median=statistics.median(pc5)
    positive=sum(x>0 for x in pc5)/len(pc5)
    flush=sum(x<=-8 for x in pc5)/len(pc5)
    hot=sum(x>=5 for x in pc5)/len(pc5)
    # Require multiple independent stress symptoms before halting alerts.  One red
    # median alone is not enough in a noisy meme universe.
    stress_votes=sum([median<=-1.5, positive<0.35, flush>=0.15])
    risk_off=stress_votes>=2
    if risk_off:
        label="RISK_OFF"
    elif positive>=0.62 and median>=0.5 and flush<0.10:
        label="RISK_ON"
    else:
        label="MIXED"
    return {"label":label,"risk_off":risk_off,"n":len(data),"median_pc5":median,
            "positive_rate":positive,"flush_rate":flush,"hot_rate":hot,
            "reason":f"{len(data)} tokens: median 5m {median:+.1f}%, {positive:.0%} green, {flush:.0%} <=-8%"}


def analog_distance(current: Mapping, row: Mapping) -> float:
    """Scale-aware distance for historical analog vetoes.

    This is deliberately a transparent nearest-neighbour distance, not a fitted ML
    model.  It can veto a repeatedly dangerous shape, but never create a BUY signal.
    """
    def slog(x): return math.log10(max(_f(x),1.0))
    cur_turn=max(_f(current.get("turnover_pct")),1e-4)
    row_turn=max(_f(row.get("turnover_pct")),1e-4)
    cur_bs=max(_f(current.get("buy_sell")),0.01); row_bs=max(_f(row.get("buy_sell")),0.01)
    cur_sw=max(_f(current.get("swaps")),1); row_sw=max(_f(row.get("swaps")),1)
    parts=[
        (_f(current.get("pc5"))-_f(row.get("pc5")))/4.0,
        (_f(current.get("pc1"))-_f(row.get("pc1")))/10.0,
        (slog(current.get("mcap"))-slog(row.get("mcap")))/0.8,
        (slog(current.get("liquidity"))-slog(row.get("liquidity")))/0.8,
        (math.log10(cur_turn)-math.log10(row_turn))/1.0,
        (math.log(cur_bs)-math.log(row_bs))/0.7,
        (math.log(cur_sw)-math.log(row_sw))/1.5,
    ]
    return math.sqrt(sum(x*x for x in parts))


def analog_summary(current: Mapping, rows: Sequence[Mapping], nearest: int = 40, min_neighbors: int = 20) -> dict:
    scored=sorted(((analog_distance(current,r),r) for r in rows),key=lambda x:x[0])[:max(nearest,min_neighbors)]
    if not scored:
        return {"n":0,"veto":False,"reason":"no completed historical analogs"}
    # Do not use distant points merely to satisfy a sample count.
    close=[r for d,r in scored if d<=2.25][:nearest]
    if len(close)<min_neighbors:
        return {"n":len(close),"veto":False,"reason":f"only {len(close)}/{min_neighbors} close historical analogs"}
    maxs=[_f(r.get("max_return_pct")) for r in close]
    mins=[_f(r.get("min_return_pct")) for r in close]
    finals=[_f(r.get("final_return_pct")) for r in close]
    clean10=sum(mx>=10 and mn>-10 for mx,mn in zip(maxs,mins))/len(close)
    severe8=sum(mn<=-8 for mn in mins)/len(close)
    severe15=sum(mn<=-15 for mn in mins)/len(close)
    medmax=statistics.median(maxs); medfinal=statistics.median(finals)
    # A historical analog is only a veto when downside recurrence is strong AND
    # fee-worthy upside is scarce, or the median endpoint is materially negative.
    veto=(severe8>=0.50 and clean10<=0.25) or (severe8>=0.45 and medfinal<=-3.0 and medmax<10)
    return {"n":len(close),"veto":veto,"clean10_rate":clean10,"severe8_rate":severe8,
            "severe15_rate":severe15,"median_max":medmax,"median_final":medfinal,
            "reason":f"{len(close)} analogs: clean +10 {clean10:.0%}, <=-8 {severe8:.0%}, median max {medmax:+.1f}%, final {medfinal:+.1f}%"}


def risk_position_size(bankroll: float, stop_pct: float = 8.0, risk_pct: float = 0.5,
                       min_usd: float = 1.0, max_usd: float = 10.0) -> dict:
    bankroll=max(0.0,_f(bankroll)); stop=max(1.0,abs(_f(stop_pct,8))); risk=max(0.05,_f(risk_pct,0.5))
    if bankroll<=0:
        size=min(max_usd,max(min_usd,5.0))
        return {"suggested_usd":size,"bankroll":0.0,"risk_usd":None,"risk_pct":risk,"stop_pct":stop,
                "reason":"bankroll not set; conservative fixed-size guidance"}
    risk_usd=bankroll*(risk/100.0)
    raw=risk_usd/(stop/100.0)
    size=max(min_usd,min(max_usd,raw,bankroll*0.10))
    return {"suggested_usd":size,"bankroll":bankroll,"risk_usd":risk_usd,"risk_pct":risk,"stop_pct":stop,
            "reason":f"risk budget ${risk_usd:.2f} ({risk:.2f}% bankroll) at {stop:.1f}% stop"}


def proof_metrics(rows: Sequence[Mapping], min_paths: int = 12, min_unique_tokens: int = 6,
                  min_net_roi_pct: float = 1.0, min_t_stat: float = 1.3,
                  require_ex_best_positive: bool = True) -> dict:
    """Grade only the explicitly proof-eligible shadow cohort.

    v15.2 adds two safeguards that the original proof gate lacked: token diversity and
    a minimum net ROI after simulated friction.  Repeated trades in one hot contract
    therefore cannot prove the entire strategy, and a statistically positive but
    economically tiny edge cannot unlock live guidance.

    v16.2 reliability patch adds two noise guards. Memecoin returns are fat-tailed, so a
    handful of trades with one lucky runner can satisfy PF/win-rate thresholds while the
    strategy has no edge:
    - the one-sided t-statistic of per-trade net ROI must reach ``min_t_stat``
      (~90% confidence at 1.3), so small samples need consistent results;
    - net P/L must stay positive after removing the single best trade.
    A cohort that is profitable but not yet convincing stays in PROBATION (keep
    collecting evidence) rather than QUARANTINED.
    """
    rows=[dict(r) for r in rows]
    min_unique_tokens=max(1,int(min_unique_tokens))
    if not rows:
        return {"status":"PROBATION","paused":True,"reason":"no completed proof paths","t_stat":None,"pnl_ex_best":0.0,"n":0,"needed":min_paths,"pnl":0.0,"profit_factor":None,
                "win_rate":None,"unique_tokens":0,"unique_needed":min_unique_tokens,"net_roi_pct":0.0}
    pnls=[_f(r.get("realized_pnl")) for r in rows]
    wins=[x for x in pnls if x>0]; losses=[x for x in pnls if x<0]
    pf=sum(wins)/abs(sum(losses)) if losses else (float("inf") if wins else None)
    win=len(wins)/len(pnls)
    pnl=sum(pnls)
    invested=sum(max(0.0,_f(r.get("amount_usd"))) for r in rows)
    net_roi=(pnl/invested*100.0) if invested>0 else 0.0
    unique=len({str(r.get("token") or "") for r in rows if r.get("token")})
    enough=(len(pnls)>=min_paths and unique>=min_unique_tokens)
    quality=(pnl>0 and win>=0.45 and (pf is not None and pf>=1.20) and net_roi>=float(min_net_roi_pct))
    rois=[_f(r.get("realized_pnl"))/_f(r.get("amount_usd")) for r in rows if _f(r.get("amount_usd"))>0]
    t_stat=None
    if len(rois)>=2:
        sd=statistics.stdev(rois); mean=statistics.mean(rois)
        t_stat=(mean/(sd/math.sqrt(len(rois)))) if sd>0 else (float("inf") if mean>0 else 0.0)
    ex_best=pnl-max(pnls)
    confident=(t_stat is not None and t_stat>=float(min_t_stat))
    robust=(ex_best>0) or not require_ex_best_positive
    reason=""
    if not enough:
        status="PROBATION"; paused=True
        reason=f"need {min_paths} paths / {min_unique_tokens} tokens (have {len(pnls)} / {unique})"
    elif not quality:
        status="QUARANTINED"; paused=True
        reason="net quality below thresholds (P/L, win rate, profit factor or ROI)"
    elif not confident:
        status="PROBATION"; paused=True
        reason=f"edge not yet distinguishable from noise (t={0.0 if t_stat is None else t_stat:.2f} < {float(min_t_stat):.2f})"
    elif not robust:
        status="PROBATION"; paused=True
        reason=f"profit depends on a single trade (net without best trade {ex_best:+.2f})"
    else:
        status="ACTIVE"; paused=False
        reason="proof thresholds and noise guards passed"
    return {"status":status,"paused":paused,"reason":reason,"t_stat":t_stat,"pnl_ex_best":ex_best,
            "n":len(pnls),"needed":min_paths,"pnl":pnl,
            "profit_factor":pf,"win_rate":win,"avg_win":statistics.mean(wins) if wins else 0.0,
            "avg_loss":statistics.mean(losses) if losses else 0.0,"unique_tokens":unique,
            "unique_needed":min_unique_tokens,"net_roi_pct":net_roi,"min_net_roi_pct":float(min_net_roi_pct)}
