"""Trade only what's proven: the autopilot takes a coin only when it matches a setup that made money.

Your Oct 14 review showed why this is needed. Over 859 coins the hype scanner saw in a week (the ones it alerted and
the ones a rule held back), buying the moment a coin was spotted lost money in EVERY slice — by score, size, age,
launchpad, hour of day — and with every exit plan, entry delay or dip-buy tried, after a 3% cost each way. Copying
whale buys lost too. Yet the paper autopilot kept buying every alert.

So, every 6 hours, a fixed menu of plain setups is replayed on the last 7 days — for hype coins (every coin the scanner
alerted or held back) and for whale buys (every buy your whales made into a settled pool, sent or not) — with the same
honest replay as the self-tuner (liquidity-aware, rugs are losses, slippage both ways). Two steps, because a week of
replays can still be luck (in your data the best hype setup rested on 2 coins of 58):
  1. CANDIDATE — made money on BOTH halves of the week (each half on its own: 12+ trades, +2% a trade or better) and
     still does without its single best coin. The PAPER autopilot then trades only what matches it, with its exit
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
TRACK = 130 * 60           # hype coins (held-back ones are priced for ~2 hours)
TRACK_WHALE = 370 * 60     # whale buys are priced longer: room for a 6-hour swing
DEDUPE = 6 * 3600          # the same coin again within 6 hours is the same opportunity, counted once
MIN_HALF = 12
MIN_HALF_AVG = 1.02
FORWARD_MIN, FORWARD_BENCH, BENCH_DAYS = 8, 0.97, 3
CONFIRM_N, CONFIRM_AVG = 15, 1.02
WHALE_RECORD_MIN = 5       # "a whale whose copies make money": 5+ earlier buys in the window, profitable on average

HYPE_SETUPS = [
    ("alerts", "Hype alerts (the filter's picks)", lambda f: f["kind"] == "alert"),
    ("score70", "Strong hype score (70+)", lambda f: f["score"] >= 70),
    ("deep", "Deep, settled pool (liquidity $20K+, pool 6h+ old)", lambda f: f["liq"] >= 20_000 and f["age_h"] >= 6),
    ("trend_reload", "Trending on Jupiter and buyers reloading", lambda f: {"trending", "reload"} <= f["keys"]),
    ("organic", "Real buyers (Jupiter organic score)", lambda f: "organic" in f["keys"]),
    ("whales", "Your whales are in", lambda f: "whales" in f["keys"]),
    ("liq_heavy", "Liquidity at least half the market cap, pool 1h+ old",
     lambda f: f["mc"] > 0 and f["liq"] >= 0.5 * f["mc"] and f["age_h"] >= 1),
]
WHALE_SETUPS = [
    ("w_all", "Whale buys into settled pools", lambda f: True),
    ("w_big", "Whale buys into $100K+ coins a day+ old", lambda f: f["mc"] >= 100_000 and f["age_h"] >= 24),
    ("w_size", "Whale buys of $1,000+", lambda f: f["usd"] >= 1_000),
    ("w_confluence", "2+ of your whales in the same coin", lambda f: f["confluence"] >= 2),
    ("w_proven", "Whales whose recent copies made money",
     lambda f: f["whale_n"] >= WHALE_RECORD_MIN and f["whale_avg"] > 1.0),
]
PLANS = [
    ("quick", "all out at +20%, -25% stop, 30 min", {"tp": 1.2, "stop": 0.75, "hold": 1800, "half": False, "trail": 35}),
    ("balanced", "all out at +30%, -20% stop, 60 min", {"tp": 1.3, "stop": 0.8, "hold": 3600, "half": False, "trail": 35}),
    ("runner", "all out at +50%, -30% stop, 2h, trail 25%", {"tp": 1.5, "stop": 0.7, "hold": 7200, "half": False,
                                                              "trail": 25}),
    ("ladder", "half at +30%, the rest rides (-25% stop, trail 25%), 2h", {"tp": 1.3, "stop": 0.75, "hold": 7200,
                                                                            "half": True, "trail": 25}),
]
WHALE_PLANS = PLANS + [
    ("swing", "half at 2x, the rest rides (-40% stop, trail 35%), 6h", {"tp": 2.0, "stop": 0.6, "hold": 21600,
                                                                        "half": True, "trail": 35}),
]
FAMILIES = {"hype": (HYPE_SETUPS, PLANS), "whale": (WHALE_SETUPS, WHALE_PLANS)}
SETUP_LABEL = {k: label for k, label, _ in HYPE_SETUPS + WHALE_SETUPS}
SETUP_LABEL["s_second_leg"] = "Second legs (pumped, pulled back, held a floor, buyers back)"
PLAN_OF = {k: (label, plan) for k, label, plan in WHALE_PLANS}


def family_of(setup: str) -> str:
    return "whale" if setup.startswith("w_") else "swing" if setup.startswith("s_") else "hype"


def features(kind: str, info: dict, r: dict, now: float) -> dict:
    """A hype coin, the way a setup sees it — the same at decision time and in the replay."""
    created = num(info.get("pair_created_ts"))
    return {"family": "hype", "kind": kind, "score": num(r.get("score")), "mc": num(info.get("mc_usd")),
            "liq": num(info.get("liquidity_usd")), "age_h": (now - created) / 3600 if created else 0.0,
            "keys": {k for k, *_ in r.get("signals") or []} | set(r.get("keyset") or ())}


def _stats(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0, "avg": 0.0, "won": 0.0, "wo_best": 0.0}
    s = sorted(xs)
    return {"n": len(xs), "avg": statistics.mean(xs), "won": sum(x > 1 for x in xs) / len(xs),
            "wo_best": statistics.mean(s[:-1]) if len(s) > 1 else s[0]}


def _active_of(fam: dict) -> dict | None:
    return (fam or {}).get("active")


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
    def _path(self, mint: str, ts: int, span: int) -> list[tuple]:
        return [(int(m["ts"]), num(m["price"]), m["liq"]) for m in self.db.rows(
            "select ts, price, liq from price_marks where mint=? and ts>? and ts<=? order by ts", (mint, ts, ts + span))
            if num(m["price"]) > 0]

    def _rows(self, now: int) -> list[dict]:
        """Hype coins: every coin the scanner alerted or held back (that it may still trade under today's floor)."""
        rows, last = [], {}
        floor_h = self.cfg.get("HYPE_MIN_POOL_MINUTES") / 60
        for h in self.db.rows("""select h.*, t.pair_created_ts from hype_seen h left join tokens t on t.mint=h.mint
                where h.kind in ('alert','shadow') and h.ts>=? and h.ts<=? and h.price>0 order by h.ts""",
                              (now - WINDOW_DAYS * 86400, now - TRACK)):
            if h["mint"] in last and h["ts"] - last[h["mint"]] < DEDUPE:
                continue
            created = num(h["pair_created_ts"])
            age_h = (h["ts"] - created) / 3600 if created else 0.0
            if floor_h > 0 and (not created or age_h < floor_h):
                continue   # a pool that young is never traded any more: it can't count for or against a setup
            last[h["mint"]] = h["ts"]
            path = self._path(h["mint"], int(h["ts"]), TRACK)
            if len(path) < 20:
                continue   # not priced (enough) after it was seen: can't be judged
            liq = next((p[2] for p in path if p[2] is not None and p[2] > 0), 0.0)
            rows.append({"ts": int(h["ts"]), "price": num(h["price"]), "path": path,
                         "f": {"family": "hype", "kind": h["kind"], "score": num(h["score"]), "mc": num(h["mc"]),
                               "liq": liq, "age_h": age_h, "keys": set(str(h["keys"] or "").split(","))}})
        return rows

    def _whale_rows(self, now: int) -> list[dict]:
        """Whale buys: every buy a followed whale made into a pool old enough to trade (sent, or held back for any
        reason), priced from the moment the bot judged it."""
        rows, last = [], {}
        min_h = self.cfg.get("WHALE_MIN_POOL_HOURS")
        for a in self.db.rows("""select a.ts, a.mint, a.wallet, a.price_usd, a.mc_usd, a.confluence,
                coalesce(s.usd_value, 0) usd, t.pair_created_ts from alerts a left join swaps s on s.id=a.swap_id
                left join tokens t on t.mint=a.mint where a.kind='BUY' and a.price_usd>0 and a.ts>=? and a.ts<=?
                order by a.ts""", (now - WINDOW_DAYS * 86400, now - TRACK_WHALE)):
            created = num(a["pair_created_ts"])
            age_h = (a["ts"] - created) / 3600 if created else -1.0
            if age_h < max(min_h, 0.0) or (min_h > 0 and not created):
                continue   # fresh launches are never traded: they can't count for or against a setup
            if a["mint"] in last and a["ts"] - last[a["mint"]] < DEDUPE:
                continue
            last[a["mint"]] = a["ts"]
            path = self._path(a["mint"], int(a["ts"]), TRACK_WHALE)
            if len(path) < 20:
                continue
            liq = next((p[2] for p in path if p[2] is not None and p[2] > 0), 0.0)
            rows.append({"ts": int(a["ts"]), "price": num(a["price_usd"]), "path": path, "wallet": a["wallet"],
                         "f": {"family": "whale", "mc": num(a["mc_usd"]), "liq": liq, "age_h": age_h,
                               "usd": num(a["usd"]), "confluence": int(num(a["confluence"])),
                               "whale_n": 0, "whale_avg": 0.0}})
        # each whale's record so far, from its EARLIER buys only (a buy's result is known once its 2h are over)
        ref = PLAN_OF["balanced"][1]
        done: dict[str, list[tuple[int, float]]] = {}
        for r in rows:
            past = [x for t, x in done.get(r["wallet"], []) if t + TRACK <= r["ts"]]
            r["f"]["whale_n"], r["f"]["whale_avg"] = len(past), (statistics.mean(past) if past else 0.0)
            x = replay(r["price"], r["path"], r["ts"], ref, self.cfg.get("PAPER_SLIPPAGE_PCT") / 100, ref["trail"])
            if x is not None:
                done.setdefault(r["wallet"], []).append((r["ts"], x))
        self._wallet_records = {w: {"n": len(v), "avg": statistics.mean(x for _, x in v)} for w, v in done.items() if v}
        return rows

    @staticmethod
    def _table(rows: list[dict], setups: list, plans: list, slip: float, now: int) -> tuple[list, int]:
        mid = rows[len(rows) // 2]["ts"] if rows else now
        table = []
        for skey, slabel, test in setups:
            picked = [r for r in rows if test(r["f"])]
            for pkey, plabel, plan in plans:
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
        return table, mid

    def _compute(self, rows: list[dict], wrows: list[dict], slip: float, now: int) -> dict:
        table, mid = self._table(rows, HYPE_SETUPS, PLANS, slip, now)
        wtable, wmid = self._table(wrows, WHALE_SETUPS, WHALE_PLANS, slip, now)
        return {"ts": now, "coins": len(rows), "slip": slip, "split_ts": mid, "table": table,
                "whale": {"coins": len(wrows), "split_ts": wmid, "table": wtable}}

    def _pick(self, fam: dict, prev: dict | None, since_key: str, now: int, benched: set) -> None:
        live = [t for t in fam["table"] if t["proven"] and f"{t['setup']}:{t['plan']}" not in benched]
        fam["active"] = ({"setup": live[0]["setup"], "plan": live[0]["plan"], "label": live[0]["setup_label"],
                          "plan_label": live[0]["plan_label"], "avg": live[0]["all"]["avg"], "n": live[0]["all"]["n"],
                          "worst_half": live[0]["worst_half"]} if live else None)
        if fam["active"]:
            prev = prev or {}
            same = (prev.get("setup"), prev.get("plan")) == (fam["active"]["setup"], fam["active"]["plan"])
            since = int(num(self.db.get_meta(since_key, "0"))) if same else now
            fw = self.forward(fam["active"]["setup"], fam["active"]["plan"], since)
            fam["forward"] = {**fw, "confirmed": fw["n"] >= CONFIRM_N and fw["avg"] >= CONFIRM_AVG and fw["wo_best"] > 1.0}

    async def evaluate(self, now: int | None = None) -> dict:
        now = int(now or time.time())
        rows, wrows = self._rows(now), self._whale_rows(now)
        slip = self.cfg.get("PAPER_SLIPPAGE_PCT") / 100
        rep = await asyncio.to_thread(self._compute, rows, wrows, slip, now)
        benched = self.benched(now)
        prev = self.report or {}
        self._pick(rep, prev.get("active"), "edge:active_since", now, benched)
        self._pick(rep["whale"], (prev.get("whale") or {}).get("active"), "edge:whale:active_since", now, benched)
        rep["whale"]["wallets"] = getattr(self, "_wallet_records", {})
        rep["benched"] = sorted(benched)
        self.report = rep
        self.db.set_meta("edge:report", json.dumps({**rep, "table": rep["table"][:40],
                                                    "whale": {**rep["whale"], "table": rep["whale"]["table"][:40]}}))
        return rep

    def benched(self, now: int) -> set[str]:
        out = set()
        for r in self.db.rows("select key, value from meta where key like 'edge:bench:%'"):
            if now - int(num(r["value"])) < BENCH_DAYS * 86400:
                out.add(r["key"].split("edge:bench:", 1)[1])
        return out

    def forward(self, setup: str, plan: str, since: int = 0) -> dict:
        """How the setup's REAL paper trades did after it was picked."""
        rows = self.db.rows("""select p.size_usd, p.proceeds_usd from edge_trades e join paper_trades p on p.id=e.trade_id
            where e.setup=? and e.plan=? and p.status='closed' and coalesce(p.interrupted,0)=0 and e.ts>=?""",
                            (setup, plan, since))
        return _stats([num(r["proceeds_usd"]) / num(r["size_usd"]) for r in rows if num(r["size_usd"]) > 0])

    async def tick(self, now: int | None = None) -> None:
        now = int(now or time.time())
        if now - int(num(self.db.get_meta("edge:last_run", "0"))) < EVERY:
            return
        self.db.set_meta("edge:last_run", now)
        prev = self.report or {}
        before = {"hype": prev.get("active") or {}, "whale": (prev.get("whale") or {}).get("active") or {}}
        since_keys = {"hype": "edge:active_since", "whale": "edge:whale:active_since"}
        try:
            for fam, act in before.items():   # a candidate that loses in real paper trading is benched
                if act:
                    fw = self.forward(act["setup"], act["plan"], int(num(self.db.get_meta(since_keys[fam], "0"))))
                    if fw["n"] >= FORWARD_MIN and fw["avg"] <= FORWARD_BENCH:
                        self.db.set_meta(f"edge:bench:{act['setup']}:{act['plan']}", now)
            rep = await self.evaluate(now)
        except Exception as exc:
            self.last_error = f"{time.strftime('%H:%M:%S')} {type(exc).__name__}: {exc}"
            return
        after = {"hype": rep.get("active") or {}, "whale": rep["whale"].get("active") or {}}
        key = lambda a: f"{a.get('setup')}:{a.get('plan')}" if a else ""
        for fam in ("hype", "whale"):
            if key(after[fam]) != key(before[fam]):
                self.db.set_meta(since_keys[fam], now)
                if self.notify:
                    await self.notify(format_change(after[fam], rep if fam == "hype" else rep["whale"], fam),
                                      silent=True, kind="INFO")

    # -- deciding ----------------------------------------------------------------------------------
    def active(self, family: str = "hype") -> dict | None:
        if not self.cfg.flag("EDGE_GATE"):
            return None
        rep = self.report or {}
        return rep.get("active") if family == "hype" else (rep.get("whale") or {}).get("active")

    def whale_features(self, wallet: str, info: dict, usd: float, confluence: int, now: float) -> dict:
        created = num(info.get("pair_created_ts"))
        rec = ((self.report or {}).get("whale") or {}).get("wallets", {}).get(wallet) or {}
        return {"family": "whale", "mc": num(info.get("mc_usd")), "liq": num(info.get("liquidity_usd")),
                "age_h": (now - created) / 3600 if created else 0.0, "usd": num(usd), "confluence": int(confluence),
                "whale_n": int(rec.get("n", 0)), "whale_avg": num(rec.get("avg"))}

    def match(self, f: dict) -> dict | None:
        """The candidate setup this coin fits (with its exit plan), or None."""
        family = f.get("family", "hype")
        rep = self.report or {}
        act = rep.get("active") if family == "hype" else (rep.get("whale") or {}).get("active")
        if not act:
            return None
        setups = HYPE_SETUPS if family == "hype" else WHALE_SETUPS
        test = next((t for k, _, t in setups if k == act["setup"]), None)
        try:
            if not test or not test(f):
                return None
        except KeyError:
            return None
        label, plan = PLAN_OF[act["plan"]]
        return {**act, "exit": {**plan, "label": label}}

    def allows(self, f: dict) -> tuple[bool, dict | None]:
        """(may the autopilot trade this?, the matched setup). Without EDGE_GATE everything goes, as before."""
        if not self.cfg.flag("EDGE_GATE"):
            return True, None
        m = self.match(f)
        return m is not None, m

    def confirmed(self, trade_id: int) -> tuple[bool, str]:
        """May real money follow this paper trade? Only when its setup's forward paper trades made money."""
        row = self.db.row("select setup, plan from edge_trades where trade_id=?", (trade_id,))
        if not row:
            return False, "it fits no setup that's being tested (/edge)"
        family = family_of(row["setup"])
        key = {"whale": "edge:whale:active_since", "hype": "edge:active_since"}.get(family)
        # second legs can't be replayed (they need days of candles), so they're judged on paper alone, from the start
        fw = self.forward(row["setup"], row["plan"], int(num(self.db.get_meta(key, "0"))) if key else 0)
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


def format_change(active: dict, rep: dict, family: str = "hype") -> str:
    what = "whale buys" if family == "whale" else "hype coins"
    if active:
        return (f"🧪 <b>A {('whale' if family == 'whale' else 'hype')} setup passed the replays — paper trades it now</b>\n"
                f"{esc(active['label'])} · exit: {esc(active['plan_label'])}\nOn the last {WINDOW_DAYS} days of {what}: "
                f"{_pct(active['avg'])} a trade over {active['n']}, at least {_pct(active['worst_half'])} on each half "
                f"of the week. Real money follows only once its own paper trades make money ({CONFIRM_N}+). /edge")
    return (f"🛑 <b>No {('whale' if family == 'whale' else 'hype')} setup is making money right now — the autopilot "
            f"stands aside</b>\nNothing made money after costs on both halves of the last {WINDOW_DAYS} days "
            f"({rep.get('coins', 0)} {what} replayed). Alerts keep coming (marked), the measuring goes on, and trading "
            "resumes by itself when something is proven. /edge")


def _section(lines: list, title: str, fam: dict, gate: bool) -> None:
    lines.append(f"\n<b>{title}</b> ({fam.get('coins', 0)} replayed)")
    act = fam.get("active")
    if act:
        fw = fam.get("forward") or {}
        lines.append(f"🧪 Paper is testing: <b>{esc(act['label'])}</b> · {esc(act['plan_label'])} · replays "
                     f"{_pct(act['avg'])} a trade ({act['n']})")
        lines.append(f"Its paper trades since: {fw.get('n', 0)}" + (f", {_pct(fw['avg'])} a trade, "
                                                                     f"{fw['won'] * 100:.0f}% won" if fw.get("n") else "")
                     + (" — ✅ confirmed: real money may follow it" if fw.get("confirmed") else
                        f" — real money waits for {CONFIRM_N}+ trades at {_pct(CONFIRM_AVG)} or better"))
    else:
        lines.append("🛑 Nothing proven — the autopilot stands aside" + ("" if gate else
                                                                      " (EDGE_GATE is off: it trades every alert anyway)"))
    for t in (fam.get("table") or [])[:5]:
        mark = "✅" if t["proven"] else "·"
        lines.append(f"{mark} {esc(t['setup_label'])} · {esc(t['plan_label'])}: {_pct(t['first']['avg'])} "
                     f"({t['first']['n']}) / {_pct(t['second']['avg'])} ({t['second']['n']})")


def format_report(rep: dict, cfg=None) -> str:
    if not rep or not rep.get("table") and not (rep.get("whale") or {}).get("table"):
        return ("🎯 <b>Edge</b>: not measured yet — it needs a few hours of coins with prices (every 6 hours, over the "
                f"last {WINDOW_DAYS} days).")
    gate = cfg is None or cfg.flag("EDGE_GATE")
    lines = [f"<b>🎯 Edge — what made money after costs</b> (last {WINDOW_DAYS} days, {rep['slip'] * 100:.1f}% cost "
             "each way; first half / second half of the week, a trade)"]
    _section(lines, "🔥 Hype coins", rep, gate)
    _section(lines, "🐋 Whale buys (settled pools only)", rep.get("whale") or {}, gate)
    if rep.get("benched"):
        lines.append("\nBenched (lost in real paper trading after being picked): " + ", ".join(rep["benched"]))
    lines.append(f"\nPasses = {MIN_HALF}+ trades and {_pct(MIN_HALF_AVG)} a trade or better on EACH half, and still in "
                 f"profit without its single best one. Then it's tested forward on paper; real money only after "
                 f"{CONFIRM_N}+ of those paper trades made money.")
    return "\n".join(lines)
