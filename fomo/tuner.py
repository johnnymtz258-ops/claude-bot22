"""Self-tuning hype filter: every rule is measured on what it BLOCKED, and moves toward what made money.

A filter that only sees the coins it lets through can't tell whether a rule is saving you money or killing good
entries. So when a coin passes everything except ONE adjustable rule, the bot keeps a "shadow" of it: no alert, no
trade, but its price is recorded for the next two hours and replayed with the hype exit plan exactly like a real
alert (same slippage, take-profit, stop and time limit). Real hype alerts are replayed the same way.

Every 6 hours, over the last 14 days:
  • a rule whose blocked coins would have made money (12+ of them, avg +3% a trade or better, half or more won) is
    loosened one step. If the coins it then lets in lose (12+ trades, avg -3% or worse), it's tightened back.
  • the score threshold moves 5 points down when coins just under it would have won, and 5 up when alerts just over
    it lose.
  • the exit plan (take-profit / stop / time limit) is replayed on every hype alert under 72 combinations (all-out or half-and-ride); one that
    beats the current plan by 2%+ a trade (not counting the single best coin) replaces it.
Each setting changes at most once every 3 days and stays inside fixed bounds, and every change comes with a Telegram
note saying why. A setting you change yourself (/set or the dashboard) is left alone for 7 days.
HYPE_AUTOTUNE 0 stops the changes (the measuring goes on, /tuning shows it).
"""
from __future__ import annotations

import json
import statistics
import time

from .hype import COOLDOWN, alertable, exit_plan
from .util import esc, mc, num

WINDOW_DAYS = 14
TRACK = 130 * 60            # price a shadow this long: enough for the longest time limit the exit grid tries
EVERY = 6 * 3600
MIN_N = 12
LOOSEN_AVG, LOOSEN_WON = 1.03, 0.5
TIGHTEN_AVG = 0.97
COOLDOWN_DAYS = 3
MANUAL_DAYS = 7
SHADOWS_PER_HOUR = 20
EXIT_MIN_N = 25
EXIT_GAIN = 0.02
MAX_ALERTS, MAX_SHADOWS = 400, 1500
LATE_STEP, LATE_MAX = 50, 300
SCORE_STEP, SCORE_MIN, SCORE_MAX = 5, 35, 60
GRID = [(tp, stop, minutes, half) for tp in (20, 30, 50, 80) for stop in (15, 25, 35) for minutes in (30, 60, 120)
        for half in (0, 1)]   # half=1: sell half at the take-profit, ride the rest (a two-step ladder)
SETTINGS = {"zone": ("HYPE_AVOID_MC_HI",), "profile": ("HYPE_SKIP_PROFILE",), "late": ("HYPE_MAX_H1_PCT",),
            "score": ("HYPE_MIN_SCORE",), "exit": ("HYPE_TP_PCT", "HYPE_STOP_PCT", "HYPE_MAX_MINUTES", "HYPE_HALF_AT_TP")}


def replay(price: float, path: list[tuple[int, float]], start: int, plan: dict, slip: float,
           trail_pct: float = 35.0) -> float | None:
    """Multiple of your money for one coin bought at `price` (plus slippage) and run through the hype exit plan on its
    recorded price path — what the paper autopilot does each tick. None when the path doesn't cover the trade."""
    if price <= 0 or not path:
        return None
    entry, peak, got, left = price * (1 + slip), 1.0, 0.0, 1.0
    half = plan.get("half")
    for ts, p in path:
        x = p / entry
        peak = max(peak, x)
        if half and left == 1.0 and x >= plan["tp"]:
            got, left = 0.5 * x * (1 - slip), 0.5          # the ladder's first step: half at the take-profit
            continue
        if (ts - start >= plan["hold"] or (not half and x >= plan["tp"]) or x <= plan["stop"]
                or (peak >= 1.5 and x <= peak * (1 - trail_pct / 100))):
            return got + left * x * (1 - slip)
    if path[-1][0] - start >= plan["hold"] - 120:
        return got + left * path[-1][1] / entry * (1 - slip)
    return None


def stats(results: list[float]) -> dict:
    r = sorted(x for x in results if x is not None)
    if not r:
        return {"n": 0, "avg": 0.0, "won": 0.0, "median": 0.0, "avg_wo_best": 0.0}
    return {"n": len(r), "avg": statistics.mean(r), "won": sum(x > 1 for x in r) / len(r),
            "median": statistics.median(r), "avg_wo_best": statistics.mean(r[:-1]) if len(r) > 1 else r[0]}


def _pct(x: float) -> str:
    return f"{(x - 1) * 100:+.1f}%"


class Tuner:
    def __init__(self, cfg, db, notify, set_setting=None):
        self.cfg = cfg
        self.db = db
        self.notify = notify
        self.set_setting = set_setting or (lambda name, value: cfg.set(name, value))
        self.report: dict = {}
        self.last_error = ""
        db.run("""create table if not exists hype_seen(id integer primary key autoincrement, ts integer, mint text,
            kind text, rule text default '', score real default 0, price real default 0, mc real default 0,
            h1 real default 0, keys text default '', alert_id integer default 0)""")
        db.run("create index if not exists idx_hype_seen_ts on hype_seen(kind, ts)")
        db.run("create index if not exists idx_hype_seen_mint on hype_seen(mint, ts)")
        self.backfill()

    def backfill(self) -> int:
        """Once: the hype alerts sent before the tuner existed, so the exit plan is judged on weeks of your alerts
        from day one (their price paths are already recorded; score and hourly change weren't kept, so the score and
        "late" rules only learn from new ones)."""
        if self.db.get_meta("tune:backfilled") == "1":
            return 0
        n = 0
        lo, hi = self.cfg.get("HYPE_AVOID_MC_LO"), self.cfg.get("HYPE_AVOID_MC_HI")
        for a in self.db.rows("""select id, ts, mint, price_usd, mc_usd, reasons from alerts where kind='HYPE'
                and price_usd>0 and ts>=?""", (int(time.time()) - WINDOW_DAYS * 86400,)):
            keys = str(a["reasons"] or "").replace("signals:", "")
            ks = set(keys.split(","))
            if "whales" not in ks and ((self.cfg.flag("HYPE_SKIP_PROFILE") and "profile" in ks)
                                       or lo <= num(a["mc_usd"]) < hi):
                continue   # sent under an older, looser filter: today's filter wouldn't send it, so it can't judge today's exits
            if self.db.scalar("select 1 from hype_seen where alert_id=?", (a["id"],)):
                continue
            n += bool(self.db.insert("""insert into hype_seen(ts,mint,kind,rule,score,price,mc,h1,keys,alert_id)
                values(?,?,'alert','',0,?,?,0,?,?)""", (a["ts"], a["mint"], a["price_usd"], a["mc_usd"], keys, a["id"])))
        self.db.set_meta("tune:backfilled", "1")
        return n

    # -- recording ------------------------------------------------------------------------------
    def _insert(self, kind: str, mint: str, info: dict, r: dict, rule: str, now: int, alert_id: int = 0) -> int:
        return self.db.insert("""insert into hype_seen(ts,mint,kind,rule,score,price,mc,h1,keys,alert_id)
            values(?,?,?,?,?,?,?,?,?,?)""", (int(now), mint, kind, rule, r["score"], num(info.get("price_usd")),
                                             num(info.get("mc_usd")), num(info.get("change_h1")),
                                             ",".join(k for k, _, _ in r["signals"]), alert_id))

    def seen_alert(self, mint: str, info: dict, r: dict, alert_id: int, now: int) -> None:
        self._insert("alert", mint, info, r, "", now, alert_id)

    def shadow_rule(self, mint: str, info: dict, r: dict, now: int) -> str:
        """The one adjustable rule that kept this coin from an alert, or '' (not a shadow: safety blocks, quiet coins,
        already alerted or shadowed lately, or this hour's shadows used up)."""
        rule = r.get("rule", "")
        if rule in ("zone", "profile", "late"):
            if not alertable({**r, "blocked": ""}, self.cfg):
                return ""
            if rule == "late" and num(info.get("change_h1")) > self.cfg.get("HYPE_MAX_H1_PCT") + 2 * LATE_STEP:
                return ""
        elif (not r["blocked"] and self.cfg.get("HYPE_MIN_SCORE") - 2 * SCORE_STEP <= r["score"] < self.cfg.get("HYPE_MIN_SCORE")
              and alertable(r, _Lower(self.cfg, r["score"]))):
            rule = "score"
        else:
            return ""
        if self.db.scalar("select 1 from hype_seen where mint=? and ts>=?", (mint, int(now) - COOLDOWN)):
            return ""
        if (self.db.scalar("select count(*) from hype_seen where kind='shadow' and ts>=?", (int(now) - 3600,), default=0)
                >= SHADOWS_PER_HOUR):
            return ""
        return rule

    def shadow(self, mint: str, info: dict, r: dict, rule: str, now: int) -> None:
        self._insert("shadow", mint, info, r, rule, now)

    def tracked_mints(self, now: int) -> list[str]:
        """Shadows still being priced (the tracker records their price every minute, like an alert's)."""
        return [r["mint"] for r in self.db.rows("select distinct mint from hype_seen where kind='shadow' and ts>=?",
                                                (int(now) - TRACK,))]

    # -- measuring -----------------------------------------------------------------------------
    def _rows(self, now: int, plan: dict) -> list[dict]:
        """Alerts and shadows of the last 14 days, each with its replayed result `x` (alerts keep their price path for
        the exit-plan grid; capped so a measurement stays well under a second)."""
        rows = []
        for kind, limit in (("alert", MAX_ALERTS), ("shadow", MAX_SHADOWS)):
            rows += self.db.rows("""select * from hype_seen where kind=? and ts>=? and ts<=? order by ts desc limit ?""",
                                 (kind, int(now) - WINDOW_DAYS * 86400, int(now) - TRACK, limit))
        for row in rows:
            row["path"] = [(int(m["ts"]), num(m["price"])) for m in self.db.rows(
                "select ts, price from price_marks where mint=? and ts>? and ts<=? order by ts",
                (row["mint"], row["ts"], row["ts"] + TRACK)) if num(m["price"]) > 0]
            row["keyset"] = set(str(row["keys"] or "").split(","))
            row["x"] = self._run([row], plan)[0]
            if row["kind"] == "shadow":
                del row["path"]
        return rows

    def _run(self, rows: list[dict], plan: dict) -> list[float | None]:
        slip, trail = self.cfg.get("PAPER_SLIPPAGE_PCT") / 100, self.cfg.get("PROTECT_TRAIL_PCT")
        return [replay(num(r["price"]), r.get("path") or [], int(r["ts"]), plan, slip, trail) for r in rows]

    def base(self, name: str) -> float:
        """The value before the tuner first moved it (your own setting or the default)."""
        saved = self.db.get_meta(f"tune:base:{name}")
        return num(saved) if saved else self.cfg.get(name)

    def _since(self, name: str) -> int:
        return int(num(self.db.get_meta(f"tune:changed:{name}", "0")))

    def _free(self, rule: str, now: int) -> tuple[bool, str]:
        for name in SETTINGS[rule]:
            if now - int(num(self.db.get_meta(f"tune:manual:{name}", "0"))) < MANUAL_DAYS * 86400:
                return False, f"you set {name} yourself lately — left alone for {MANUAL_DAYS} days"
            if now - self._since(name) < COOLDOWN_DAYS * 86400:
                return False, f"changed lately — waiting {COOLDOWN_DAYS} days to see the effect"
        return True, ""

    def evaluate(self, now: int | None = None) -> dict:
        """What every rule blocked and let in, replayed with the exit plan, and the change each one calls for."""
        now = int(now or time.time())
        plan = exit_plan(self.cfg)
        rows = self._rows(now, plan)
        alerts = [r for r in rows if r["kind"] == "alert"]
        shadows = [r for r in rows if r["kind"] == "shadow"]
        cfg = self.cfg
        lo, hi = cfg.get("HYPE_AVOID_MC_LO"), cfg.get("HYPE_AVOID_MC_HI")
        b_hi = self.base("HYPE_AVOID_MC_HI")
        late, b_late = cfg.get("HYPE_MAX_H1_PCT"), self.base("HYPE_MAX_H1_PCT")
        score, b_score = cfg.get("HYPE_MIN_SCORE"), self.base("HYPE_MIN_SCORE")
        no_whales = lambda r: "whales" not in r["keyset"]  # noqa: E731
        specs = [
            ("zone", f"{mc(lo)}–{mc(b_hi if hi <= lo else hi)} market-cap zone",
             hi > lo, [r for r in shadows if r["rule"] == "zone"],
             [r for r in alerts if hi <= lo < b_hi and lo <= num(r["mc"]) < b_hi and no_whales(r)
              and r["ts"] >= self._since("HYPE_AVOID_MC_HI")],
             ("HYPE_AVOID_MC_HI", lo, "off") if hi > lo else None,
             ("HYPE_AVOID_MC_HI", b_hi, "back on") if hi <= lo < b_hi else None),
            ("profile", "paid DexScreener profile / socials", cfg.flag("HYPE_SKIP_PROFILE"),
             [r for r in shadows if r["rule"] == "profile"],
             [r for r in alerts if not cfg.flag("HYPE_SKIP_PROFILE") and self.base("HYPE_SKIP_PROFILE") >= 0.5
              and "profile" in r["keyset"] and no_whales(r) and r["ts"] >= self._since("HYPE_SKIP_PROFILE")],
             ("HYPE_SKIP_PROFILE", 0, "off") if cfg.flag("HYPE_SKIP_PROFILE") else None,
             ("HYPE_SKIP_PROFILE", 1, "back on") if self.base("HYPE_SKIP_PROFILE") >= 0.5 else None),
            ("late", f"already up more than {late:.0f}% in the hour", True,
             [r for r in shadows if r["rule"] == "late" and num(r["h1"]) <= late + LATE_STEP],
             [r for r in alerts if late > b_late and num(r["h1"]) > late - LATE_STEP
              and r["ts"] >= self._since("HYPE_MAX_H1_PCT")],
             ("HYPE_MAX_H1_PCT", min(LATE_MAX, late + LATE_STEP), f"{min(LATE_MAX, late + LATE_STEP):.0f}%")
             if late < LATE_MAX else None,
             ("HYPE_MAX_H1_PCT", max(b_late, late - LATE_STEP), f"{max(b_late, late - LATE_STEP):.0f}%")
             if late > b_late else None),
            ("score", f"score under {score:.0f}", True,
             [r for r in shadows if r["rule"] == "score" and score - SCORE_STEP <= num(r["score"]) < score],
             [r for r in alerts if score <= num(r["score"]) < score + SCORE_STEP],
             ("HYPE_MIN_SCORE", max(SCORE_MIN, score - SCORE_STEP), f"{max(SCORE_MIN, score - SCORE_STEP):.0f}")
             if score > SCORE_MIN else None,
             ("HYPE_MIN_SCORE", min(SCORE_MAX, score + SCORE_STEP), f"{min(SCORE_MAX, score + SCORE_STEP):.0f}")
             if score < SCORE_MAX else None),
        ]
        out_rules = []
        for rule, label, active, blocked, admitted, loosen, tighten in specs:
            s, a = stats([r["x"] for r in blocked]), stats([r["x"] for r in admitted])
            change = None
            if loosen and active and s["n"] >= MIN_N and s["avg"] >= LOOSEN_AVG and s["won"] >= LOOSEN_WON:
                change = ("loosen",) + loosen
            elif tighten and a["n"] >= MIN_N and (
                    a["avg"] <= TIGHTEN_AVG if rule != "score" or score < b_score else
                    a["avg"] < 0.95 and a["won"] < 0.45):
                change = ("tighten",) + tighten
            out_rules.append({"rule": rule, "label": label, "active": bool(active), "blocked": s, "admitted": a,
                              "change": change})
        exit_res = self._exit_grid(alerts, plan)
        everything = stats([r["x"] for r in alerts])
        self.report = {"ts": now, "days": WINDOW_DAYS, "alerts": everything, "shadows": len(shadows),
                       "rules": out_rules, "exit": exit_res, "log": self.log(),
                       "auto": self.cfg.flag("HYPE_AUTOTUNE")}
        return self.report

    def _exit_grid(self, alerts: list[dict], plan: dict) -> dict:
        combos = [(tp, stop, minutes, half, {"tp": 1 + tp / 100, "stop": 1 - stop / 100, "hold": minutes * 60,
                                             "half": bool(half)})
                  for tp, stop, minutes, half in GRID]
        runs = {c[:4]: self._run(alerts, c[4]) for c in combos}
        current = self._run(alerts, plan)
        keep = [i for i in range(len(alerts)) if current[i] is not None and all(v[i] is not None for v in runs.values())]
        cur = stats([current[i] for i in keep])
        now_tp, now_stop, now_min = self.cfg.get("HYPE_TP_PCT"), self.cfg.get("HYPE_STOP_PCT"), self.cfg.get("HYPE_MAX_MINUTES")
        now_half = int(self.cfg.flag("HYPE_HALF_AT_TP"))
        best_key, best, best_rank = None, None, None
        for key, res in runs.items():
            s = stats([res[i] for i in keep])
            # equal results: the combination closest to today's plan wins (no change for nothing)
            rank = (round(s["avg_wo_best"], 4), -(abs(key[0] - now_tp) / 10 + abs(key[1] - now_stop) / 10
                                                  + abs(key[2] - now_min) / 30 + abs(key[3] - now_half)))
            if best_rank is None or rank > best_rank:
                best_key, best, best_rank = key, s, rank
        out = {"current": {**cur, "tp": now_tp, "stop": now_stop, "minutes": now_min, "half": now_half}, "change": None}
        if best_key:
            out["best"] = {**best, "tp": best_key[0], "stop": best_key[1], "minutes": best_key[2], "half": best_key[3]}
            if (cur["n"] >= EXIT_MIN_N and best["avg_wo_best"] >= cur["avg_wo_best"] + EXIT_GAIN
                    and best["avg"] >= cur["avg"] and best_key != (now_tp, now_stop, now_min, now_half)):
                out["change"] = best_key
        return out

    def current(self, now: int | None = None) -> dict:
        """The latest measurement (refreshed when over an hour old) — for /tuning and the dashboard."""
        now = int(now or time.time())
        if not self.report or now - self.report.get("ts", 0) > 3600:
            try:
                self.evaluate(now)
            except Exception as exc:
                self.last_error = f"{time.strftime('%H:%M:%S')} {type(exc).__name__}: {exc}"
        return self.report

    # -- acting ---------------------------------------------------------------------------------
    def log(self) -> list[dict]:
        try:
            return json.loads(self.db.get_meta("tune:log", "[]"))
        except ValueError:
            return []

    def _apply(self, name: str, value: float, now: int) -> None:
        if not self.db.get_meta(f"tune:base:{name}"):
            self.db.set_meta(f"tune:base:{name}", self.cfg.get(name))
        self.set_setting(name, value)
        self.db.set_meta(f"tune:changed:{name}", now)

    def _note(self, text: str, now: int) -> None:
        log = (self.log() + [{"ts": now, "text": text}])[-30:]
        self.db.set_meta("tune:log", json.dumps(log))

    async def tick(self, now: int | None = None) -> list[str]:
        """Every 6 hours: measure, and (HYPE_AUTOTUNE on) make the changes the numbers call for."""
        now = int(now or time.time())
        if now - int(num(self.db.get_meta("tune:last_run", "0"))) < EVERY:
            return []
        self.db.set_meta("tune:last_run", now)
        self.db.run("delete from hype_seen where ts<?", (now - 30 * 86400,))
        try:
            report = self.evaluate(now)
        except Exception as exc:
            self.last_error = f"{time.strftime('%H:%M:%S')} {type(exc).__name__}: {exc}"
            return []
        if not self.cfg.flag("HYPE_AUTOTUNE"):
            return []
        notes = []
        for r in report["rules"]:
            if not r["change"] or not self._free(r["rule"], now)[0]:
                continue
            how, name, value, shown = r["change"]
            self._apply(name, value, now)
            if how == "loosen":
                s = r["blocked"]
                text = (f"Coins held back by the {r['label']} rule would have made {_pct(s['avg'])} a trade with the exit "
                        f"plan ({s['n']} coins, {s['won'] * 100:.0f}% won, last {WINDOW_DAYS} days) — {name} → {shown}, "
                        "so they're alerted and traded now. It goes back by itself if they start losing.")
            else:
                a = r["admitted"]
                text = (f"The coins the {r['label']} rule let in lost: {_pct(a['avg'])} a trade ({a['n']} trades, "
                        f"{a['won'] * 100:.0f}% won) — {name} → {shown}.")
            notes.append(text)
        ex = report["exit"]
        if ex.get("change") and self._free("exit", now)[0]:
            tp, stop, minutes, half = ex["change"]
            cur, best = ex["current"], ex["best"]
            for name, value in (("HYPE_TP_PCT", tp), ("HYPE_STOP_PCT", stop), ("HYPE_MAX_MINUTES", minutes),
                                ("HYPE_HALF_AT_TP", half)):
                if self.cfg.get(name) != value:
                    self._apply(name, value, now)
            notes.append(f"Hype exit plan: {'half' if half else 'all'} out at +{tp}%{', ride the rest' if half else ''}, "
                         f"-{stop}% stop, out after {minutes} min — on your last {best['n']} hype alerts it averaged "
                         f"{_pct(best['avg'])} a trade ({best['won'] * 100:.0f}% won) vs {_pct(cur['avg'])} for "
                         f"{'half' if cur['half'] else 'all'} at +{cur['tp']:.0f}% / -{cur['stop']:.0f}% / {cur['minutes']:.0f} min.")
        for text in notes:
            self._note(text, now)
        if notes:
            self.report["log"] = self.log()
            await self.notify("🧠 <b>Self-tuning: the hype filter adjusted itself</b>\n" + "\n".join(f"• {esc(t)}" for t in notes)
                              + "\n/tuning shows what every rule blocked and what it would have made · "
                                "/set HYPE_AUTOTUNE 0 stops the changes", silent=True, kind="INFO")
        return notes


class _Lower:
    """The config with HYPE_MIN_SCORE lowered (is this coin alertable apart from its score?)."""

    def __init__(self, cfg, min_score: float):
        self.cfg, self.min_score = cfg, min_score

    def get(self, name: str) -> float:
        return self.min_score if name == "HYPE_MIN_SCORE" else self.cfg.get(name)

    def flag(self, name: str) -> bool:
        return self.get(name) >= 0.5


def format_report(report: dict) -> str:
    """/tuning: what every rule blocked, what it would have made, and what changed."""
    if not report:
        return "🧠 The self-tuner hasn't measured anything yet — it needs a few hours of hype scanning."
    a = report["alerts"]
    lines = [f"<b>🧠 Self-tuning hype filter</b> — last {report['days']} days "
             f"({'ON' if report['auto'] else 'measuring only: /set HYPE_AUTOTUNE 1 to let it adjust'})",
             f"Hype alerts replayed with the exit plan: {a['n']} · avg {_pct(a['avg'])} · {a['won'] * 100:.0f}% won"
             if a["n"] else "Hype alerts replayed with the exit plan: none finished yet"]
    lines.append("\n<b>What each rule blocked</b> (would-have-been trades)")
    for r in report["rules"]:
        s = r["blocked"]
        state = "on" if r["active"] else "off (loosened)"
        line = f"• {esc(r['label'])} [{state}]: "
        line += (f"{s['n']} blocked · would have made {_pct(s['avg'])} · {s['won'] * 100:.0f}% won" if s["n"]
                 else "nothing blocked yet")
        if r["admitted"]["n"]:
            ad = r["admitted"]
            line += f" · let in: {ad['n']} at {_pct(ad['avg'])}"
        if r["change"]:
            line += f" → {'loosen' if r['change'][0] == 'loosen' else 'tighten'}: {r['change'][1]} {r['change'][3]}"
        lines.append(line)
    ex = report.get("exit") or {}
    cur, best = ex.get("current") or {}, ex.get("best") or {}
    if cur.get("n"):
        lines.append(f"\n<b>Exit plan</b> {'half' if cur.get('half') else 'all'} at +{cur['tp']:.0f}% / -{cur['stop']:.0f}% / {cur['minutes']:.0f} min: "
                     f"{_pct(cur['avg'])} a trade on {cur['n']} alerts")
        if best:
            lines.append(f"Best of {len(GRID)} tried: {'half' if best.get('half') else 'all'} at +{best['tp']}% / -{best['stop']}% / {best['minutes']} min: {_pct(best['avg'])} "
                         f"({_pct(best['avg_wo_best'])} without its best coin)")
    log = report.get("log") or []
    if log:
        lines.append("\n<b>Changes made</b>")
        for item in log[-5:]:
            lines.append(f"{time.strftime('%b %d %H:%M', time.localtime(item['ts']))}: {esc(item['text'])}")
    return "\n".join(lines)
