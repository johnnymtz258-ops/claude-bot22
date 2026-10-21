"""What's behind a move: headlines sorted into catalysts that matter for a day trade."""
from __future__ import annotations

import re

from .alpaca import parse_ts

# (key, words, label, tone) — tone: -1 bad for buyers, +1 good, 0 depends
CATALYSTS = (
    ("offering", ("public offering", "proposed offering", "priced offering", "pricing of", "registered direct",
                  "private placement", "at-the-market", "atm offering", "shelf registration", "dilution",
                  "warrants", "convertible notes", "stock offering", "share offering"),
     "share offering / dilution — the company is selling stock; gaps on offerings usually fade", -1),
    ("bankruptcy", ("chapter 11", "bankruptcy", "going concern", "delisting", "delisted"), "bankruptcy / delisting risk", -1),
    ("earnings", ("earnings", "quarterly results", "q1 ", "q2 ", "q3 ", "q4 ", "eps", "revenue", "guidance",
                  "outlook", "beats", "misses", "fiscal"), "earnings / guidance", 0),
    ("fda", ("fda", "approval", "phase 3", "phase 2", "phase iii", "clinical trial", "pdufa", "topline"),
     "FDA / trial news — moves fast both ways", 0),
    ("upgrade", ("upgrade", "upgraded", "raises price target", "raised to buy", "outperform", "overweight"),
     "analyst upgrade", 1),
    ("downgrade", ("downgrade", "downgraded", "cuts price target", "lowers price target", "underperform",
                   "underweight", "cut to sell"), "analyst downgrade", -1),
    ("deal", ("to acquire", "acquisition", "merger", "buyout", "takeover", "to be acquired", "definitive agreement"),
     "M&A / deal", 0),
    ("contract", ("contract", "partnership", "collaboration", "awarded", "selected by", "agreement with"),
     "contract / partnership", 1),
    ("legal", ("lawsuit", "sec investigation", "probe", "subpoena", "fraud", "class action", "short report",
               "short seller"), "legal trouble / short report", -1),
    ("halt", ("trading halt", "halted"), "trading halt", 0),
)


def classify(headline: str) -> list[tuple[str, str, int]]:
    text = " " + re.sub(r"\s+", " ", str(headline or "").lower()) + " "
    out = []
    for key, words, label, tone in CATALYSTS:
        if any(w in text for w in words):
            out.append((key, label, tone))
    return out


def catalysts(items: list[dict], symbol: str = "") -> list[dict]:
    """Headlines with their catalyst tags, newest first."""
    out = []
    for n in items or []:
        if symbol and symbol not in (n.get("symbols") or [symbol]):
            continue
        tags = classify(f"{n.get('headline', '')} {n.get('summary', '')[:300]}")
        out.append({"headline": str(n.get("headline") or "").strip(), "ts": parse_ts(n.get("created_at")),
                    "source": n.get("source") or "", "url": n.get("url") or "", "tags": tags})
    out.sort(key=lambda x: -x["ts"])
    return out


def main_catalyst(items: list[dict]) -> dict | None:
    """The headline that most likely explains the move (a tagged one first)."""
    tagged = [i for i in items if i["tags"]]
    return (tagged or items or [None])[0]


def warning(items: list[dict]) -> str:
    """A red flag for buyers in recent headlines (offering, bankruptcy, legal, downgrade) or ''."""
    for i in items:
        for key, label, tone in i["tags"]:
            if tone < 0:
                return f"{label}: “{i['headline'][:120]}”"
    return ""
