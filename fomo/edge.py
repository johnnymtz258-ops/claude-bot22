"""Trade only what's proven: the autopilot takes a hype coin only when it matches a setup that made money.

Your Oct 14 review showed why this is needed. Over 859 coins the hype scanner saw in a week (the ones it alerted and
the ones a rule held back), buying the moment a coin was spotted lost money in EVERY slice — by score, size, age,
launchpad, hour of day — and with every exit plan, entry delay or dip-buy tried, after a 3% cost each way. Most coins
drift down after they start trending. Yet the paper autopilot kept buying every alert.

So, every 6 hours, a fixed menu of plain setups (7 coin types × 4 exit plans) is replayed on the last 7 days of coins
the scanner saw — the same honest replay as the self-tuner (liquidity-aware, rugs are losses, slippage both ways).
Two steps, because a week of replays can still be luck (in your data the best one rested on 2 coins of 58):
  1. CANDIDATE — made money on BOTH halves of the week (each half on its own: 12+ coins, +2% a trade or better) and
     still does without its single best coin. The PAPER autopilot then trades only coins matching it, with its exit
     plan: a forward test on coins it hasn't seen. With no candidate it stands aside and keeps measuring.
  2. CONFIRMED — the candidate's own paper trades since it was picked made money too (15+ trades, +2% a trade or
     better, and without the best one). Only then does real money (the live autopilot) follow it.
A candidate whose paper trades lose (8+ trades, -3% a trade or worse) is benched for 3 days.

Fixed menu, both halves, no single-coin wins, then a forward test: guards against trading whatever happened to work
by chance. /edge shows every setup's numbers.
"""
from __future__ import annotations

import asyncio
import json
import statistics
import time

from .tuner import replay
from .util import esc, num

EVERY = 6 * 3600
WINDOW_DAYS = 7
TRACK = 130 * 60
DEDUPE = 6 * 3600          # a coin seen again within 6 hours is the same opportunity, counted once
MIN_HALF = 12
MIN_HALF_AVG = 1.02
FORWARD_MIN, FORWARD_BENCH, BENCH_DAYS = 8, 0.97, 3
CONFIRM_N, CONFIRM_AVG = 15, 1.02

SETUPS = [
    ("alerts", "Hype alerts (the filter's picks)", lambda f: f["kind"] == "alert"),
    ("score70", "Strong hype score (70+)", lambda f: f["score"] >= 70),
    ("deep", "Deep, settled pool (liquidity $20K+, pool 6h+ old)", lambda f: f["liq"] >= 20_000 and f["age_h"] >= 6),
    ("trend_reload", "Trending on Jupiter and buyers reloading", lambda f: {"trending", "reload"} <= f["keys"]),
    ("organic", "Real buyers (Jupiter organic score)", lambda f: "organic" in f["keys"]),
    ("whales", "Your whales are in", lambda f: "whales" in f["keys"]),
    ("liq_heavy", "Liquidity at least half the market cap, pool 1h+ old",
     lambda f: f["mc"] > 0 and f["liq"] >= 0.5 * f["mc"] and f["age_h"] >= 1),
]
PLANS = [
    ("quick", "all out at +20%, -25% stop, 30 min", {"tp": 1.2, "stop": 0.75, "hold": 1800, "half": False, "trail": 35}),
    ("balanced", "all out at +30%, -20% stop, 60 min", {"tp": 1.3, "stop": 0.8, "hold": 3600, "half": False, "trail": 35}),
    ("runner", "all out at +50%, -30% stop, 2h, trail 25%", {"tp": 1.5, "stop": 0.7, "hold": 7200, "half": False,
                                                              "trail": 25}),
    ("ladder", "half at +30%, the rest rides (-25% stop, trail 25%), 2h", {"tp": 1.3, "stop": 0.75, "hold": 7200,
                                                                            "half": True, "trail": 25}),
]
SETUP_LABEL = {k: label for k, label, _ in SETUPS}
PLAN_OF = {k: (label, plan) for k, label, plan in PLANS}


def features(kind: str, info: dict, r: dict, now: float) -> dict:
    """What a setup can look at, the same way at decision time and in the replay."""
    created = num(info.get("pair_created_ts"))
    return {"kind": kind, "score": num(r.get("score")), "mc": num(info.get("mc_usd")),
            "liq": num(info.get("liquidity_usd")), "age_h": (now - created) / 3600 if created else 0.0,
            "keys": {k for k, *_ in r.get("signals") or []} | set(r.get("keyset") or ())}


def _stats(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0, "avg": 0.0, "won": 0.0, "wo_best": 0.0}
    s = sorted(xs)
    return {"n": len(xs), "avg": statistics.mean(xs), "won": sum(x > 1 for x in xs) / len(xs),
            "wo_best": statistics.mean(s[:-1]) if len(s) > 1 else s[0]}


class Edge:
    def __init__(self, cfg, db, notify=None):
        self.cfg, self.db, self.notify = cfg, db, notify
        self.report: dict = {}
        self.last_error = ""
        db.run("create table if not exists edge_trades(trade_id integer primary key, setup text, plan text, ts integer)")
        try:
            self.report = json.loads(db.get_meta("edge:report") or "{}")
        except ValueError:
            self.report = {}

    # -- measuring -------------------------------------------------------------------------------
    def _rows(self, now: int) -> list[dict]:
        rows, last = [], {}
        for h in self.db.rows("""select h.*, t.pair_created_ts from hype_seen h left join tokens t on t.mint=h.mint
                where h.kind in ('alert','shadow') and h.ts>=? and h.ts<=? and h.price>0 order by h.ts""",
                              (now - WINDOW_DAYS * 86400, now - TRACK)):
            if h["mint"] in last and h["ts"] - last[h["mint"]] < DEDUPE:
                continue
            last[h["mint"]] = h["ts"]
            path = [(int(m["ts"]), num(m["price"]), m["liq"]) for m in self.db.rows(
                "select ts, price, liq from price_marks where mint=? and ts>? and ts<=? order by ts",
                (h["mint"], h["ts"], h["ts"] + TRACK)) if num(m["price"]) > 0]
            if len(path) < 20:
                continue   # not priced (enough) after it was seen: can't be judged
            liq = next((p[2] for p in path if p[2] is not None and p[2] > 0), 0.0)
            created = num(h["pair_created_ts"])
            rows.append({"ts": int(h["ts"]), "price": num(h["price"]), "path": path,
                         "f": {"kind": h["kind"], "score": num(h["score"]), "mc": num(h["mc"]), "liq": liq,
                               "age_h": (h["ts"] - created) / 3600 if created else 0.0,
                               "keys": set(str(h["keys"] or "").split(","))}})
        return rows

    def _compute(self, rows: list[dict], slip: float, now: int) -> dict:
        mid = rows[len(rows) // 2]["ts"] if rows else now
        table = []
        for skey, slabel, test in SETUPS:
            picked = [r for r in rows if test(r["f"])]
            for pkey, plabel, plan in PLANS:
                xa, xb = [], []
                for r in picked:
                    x = replay(r["price"], r["path"], r["ts"], plan, slip, plan["trail"])
                    if x is not None:
                        (xa if r["ts"] < mid else xb).append(x)
                a, b, both = _stats(xa), _stats(xb), _stats(xa + xb)
                proven = (a["n"] >= MIN_HALF and b["n"] >= MIN_HALF and a["avg"] >= MIN_HALF_AVG
                          and b["avg"] >= MIN_HALF_AVG and both["wo_best"] > 1.0)
                table.append({"setup": skey, "setup_label": slabel, "plan": pkey, "plan_label": plabel,
                              "first": a, "second": b, "all": both, "proven": proven,
                              "worst_half": min(a["avg"], b["avg"]) if a["n"] and b["n"] else 0.0})
        table.sort(key=lambda t: -t["worst_half"])
        return {"ts": now, "coins": len(rows), "slip": slip, "split_ts": mid, "table": table}

    async def evaluate(self, now: int | None = None) -> dict:
        now = int(now or time.time())
        rows = self._rows(now)
        slip = self.cfg.get("PAPER_SLIPPAGE_PCT") / 100
        rep = await asyncio.to_thread(self._compute, rows, slip, now)
        benched = self.benched(now)
        live = [t for t in rep["table"] if t["proven"] and f"{t['setup']}:{t['plan']}" not in benched]
        rep["active"] = ({"setup": live[0]["setup"], "plan": live[0]["plan"], "label": live[0]["setup_label"],
                          "plan_label": live[0]["plan_label"], "avg": live[0]["all"]["avg"], "n": live[0]["all"]["n"],
                          "worst_half": live[0]["worst_half"]} if live else None)
        rep["benched"] = sorted(benched)
        if rep["active"]:
            same = (self.report.get("active") or {}) if self.report else {}
            since = int(num(self.db.get_meta("edge:active_since", "0"))) if (
                same.get("setup"), same.get("plan")) == (rep["active"]["setup"], rep["active"]["plan"]) else now
            fw = self.forward(rep["active"]["setup"], rep["active"]["plan"], since)
            rep["forward"] = {**fw, "confirmed": fw["n"] >= CONFIRM_N and fw["avg"] >= CONFIRM_AVG and fw["wo_best"] > 1.0}
        self.report = rep
        self.db.set_meta("edge:report", json.dumps({**rep, "table": rep["table"][:40]}))
        return rep

    def benched(self, now: int) -> set[str]:
        out = set()
        for r in self.db.rows("select key, value from meta where key like 'edge:bench:%'"):
            if now - int(num(r["value"])) < BENCH_DAYS * 86400:
                out.add(r["key"].split("edge:bench:", 1)[1])
        return out

    def forward(self, setup: str, plan: str, since: int = 0) -> dict:
        """How the setup's REAL paper trades did after it was proven."""
        rows = self.db.rows("""select p.size_usd, p.proceeds_usd from edge_trades e join paper_trades p on p.id=e.trade_id
            where e.setup=? and e.plan=? and p.status='closed' and coalesce(p.interrupted,0)=0 and e.ts>=?""",
                            (setup, plan, since))
        return _stats([num(r["proceeds_usd"]) / num(r["size_usd"]) for r in rows if num(r["size_usd"]) > 0])

    async def tick(self, now: int | None = None) -> None:
        now = int(now or time.time())
        if now - int(num(self.db.get_meta("edge:last_run", "0"))) < EVERY:
            return
        self.db.set_meta("edge:last_run", now)
        before = (self.report.get("active") or {}) if self.report else {}
        try:
            act = before
            if act:   # a proven setup that loses in real paper trading is benched
                fw = self.forward(act["setup"], act["plan"], int(num(self.db.get_meta("edge:active_since", "0"))))
                if fw["n"] >= FORWARD_MIN and fw["avg"] <= FORWARD_BENCH:
                    self.db.set_meta(f"edge:bench:{act['setup']}:{act['plan']}", now)
            rep = await self.evaluate(now)
        except Exception as exc:
            self.last_error = f"{time.strftime('%H:%M:%S')} {type(exc).__name__}: {exc}"
            return
        after = rep.get("active") or {}
        key = lambda a: f"{a.get('setup')}:{a.get('plan')}" if a else ""
        if key(after) != key(before):
            self.db.set_meta("edge:active_since", now)
            if self.notify:
                await self.notify(format_change(after, rep), silent=True, kind="INFO")

    # -- deciding ----------------------------------------------------------------------------------
    def active(self) -> dict | None:
        if not self.cfg.flag("EDGE_GATE"):
            return None
        return (self.report or {}).get("active")

    def match(self, f: dict) -> dict | None:
        """The proven setup this coin fits (with its exit plan), or None."""
        act = (self.report or {}).get("active")
        if not act:
            return None
        test = next((t for k, _, t in SETUPS if k == act["setup"]), None)
        if not test or not test(f):
            return None
        label, plan = PLAN_OF[act["plan"]]
        return {**act, "exit": {**plan, "label": label}}

    def allows(self, f: dict) -> tuple[bool, dict | None]:
        """(may the autopilot trade this hype coin?, the matched setup). Without EDGE_GATE everything goes, as before."""
        if not self.cfg.flag("EDGE_GATE"):
            return True, None
        m = self.match(f)
        return m is not None, m

    def confirmed(self, trade_id: int) -> tuple[bool, str]:
        """May real money follow this paper trade? Only when its setup's forward paper trades made money."""
        row = self.db.row("select setup, plan from edge_trades where trade_id=?", (trade_id,))
        if not row:
            return False, "it fits no setup that's being tested (/edge)"
        since = int(num(self.db.get_meta("edge:active_since", "0")))
        fw = self.forward(row["setup"], row["plan"], since)
        label = SETUP_LABEL.get(row["setup"], row["setup"])
        if fw["n"] >= CONFIRM_N and fw["avg"] >= CONFIRM_AVG and fw["wo_best"] > 1.0:
            return True, ""
        return False, (f"'{label}' is still being tested on paper: {fw['n']} of {CONFIRM_N} trades so far"
                       + (f", {_pct(fw['avg'])} a trade" if fw["n"] else "")
                       + f" — real money follows once it's {_pct(CONFIRM_AVG)} a trade or better over {CONFIRM_N}+")

    def record(self, trade_id: int, m: dict | None) -> None:
        if trade_id and m:
            self.db.run("insert or replace into edge_trades(trade_id, setup, plan, ts) values(?,?,?,?)",
                        (trade_id, m["setup"], m["plan"], int(time.time())))


def _pct(x: float) -> str:
    return f"{(x - 1) * 100:+.1f}%"


def format_change(active: dict, rep: dict) -> str:
    if active:
        return (f"🧪 <b>A setup passed the replays — paper trades it now</b>\n{esc(active['label'])} · exit: "
                f"{esc(active['plan_label'])}\nOn the last {WINDOW_DAYS} days of coins the scanner saw: "
                f"{_pct(active['avg'])} a trade over {active['n']} coins, at least {_pct(active['worst_half'])} on each half "
                f"of the week. Real money follows only once its own paper trades make money ({CONFIRM_N}+). /edge")
    return ("🛑 <b>No setup is making money right now — the autopilot stands aside</b>\nNo coin type made money after costs on both "
            f"halves of the last {WINDOW_DAYS} days ({rep.get('coins', 0)} coins replayed). Hype alerts keep coming "
            "(marked), the measuring goes on, and trading resumes by itself when something is proven. /edge")


def format_report(rep: dict, cfg=None) -> str:
    if not rep or not rep.get("table"):
        return ("🎯 <b>Edge</b>: not measured yet — it needs a few hours of hype coins with prices (every 6 hours, "
                f"over the last {WINDOW_DAYS} days).")
    act = rep.get("active")
    gate = cfg is None or cfg.flag("EDGE_GATE")
    lines = [f"<b>🎯 Edge — what made money after costs</b> ({rep['coins']} coins, last {WINDOW_DAYS} days, "
             f"{rep['slip'] * 100:.1f}% cost each way)"]
    if act:
        lines.append(f"🧪 Paper is testing: <b>{esc(act['label'])}</b> · {esc(act['plan_label'])} · replays "
                     f"{_pct(act['avg'])} a trade ({act['n']} coins)" + ("" if gate else
                                                                       " — EDGE_GATE is off: paper trades every alert"))
        fw = rep.get("forward") or {}
        lines.append(f"Its paper trades since: {fw.get('n', 0)}" + (f", {_pct(fw['avg'])} a trade, "
                                                                     f"{fw['won'] * 100:.0f}% won" if fw.get("n") else "")
                     + (" — ✅ confirmed: real money may follow it" if fw.get("confirmed") else
                        f" — real money waits for {CONFIRM_N}+ trades at {_pct(CONFIRM_AVG)} or better"))
    else:
        lines.append("🛑 Nothing proven — the autopilot stands aside" + ("" if gate else
                                                                      " (EDGE_GATE is off: it trades every alert anyway)"))
    lines.append("\nBest setups (first half / second half of the week, a trade):")
    for t in rep["table"][:8]:
        mark = "✅" if t["proven"] else "·"
        lines.append(f"{mark} {esc(t['setup_label'])} · {esc(t['plan_label'])}: {_pct(t['first']['avg'])} "
                     f"({t['first']['n']}) / {_pct(t['second']['avg'])} ({t['second']['n']})")
    if rep.get("benched"):
        lines.append("Benched (lost in real paper trading after being proven): " + ", ".join(rep["benched"]))
    lines.append(f"\nPasses = {MIN_HALF}+ coins and {_pct(MIN_HALF_AVG)} a trade or better on EACH half, and still in "
                 f"profit without its single best coin. Then it's tested forward on paper; real money only after "
                 f"{CONFIRM_N}+ of those paper trades made money.")
    return "\n".join(lines)
