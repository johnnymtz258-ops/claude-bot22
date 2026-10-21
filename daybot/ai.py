"""Claude, for the things a model is good at: reading a chart screenshot, answering a question about your trading
with your live context, and writing the coach's notes. The numbers always come from the bot (market data, your
fills, the backtest); Claude explains and reads pictures — it never invents prices.

Optional: set ANTHROPIC_API_KEY in .env (console.anthropic.com) and install the `anthropic` package
(needs Python 3.10+). Without it everything else works; these three features say they're off.
"""
from __future__ import annotations

import base64
import json
import time

from .util import dumps, num

CHART_SCHEMA = {
    "type": "object",
    "properties": {
        "ticker": {"type": "string", "description": "Ticker if readable, else empty"},
        "timeframe": {"type": "string", "description": "e.g. 5-min, 1-day, or empty if unreadable"},
        "trend": {"type": "string"},
        "pattern": {"type": "string", "description": "the structure on the chart, or 'none clear'"},
        "bias": {"type": "string", "enum": ["long", "short", "wait", "unclear"]},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "levels": {"type": "array", "items": {
            "type": "object",
            "properties": {"price": {"type": "number"}, "kind": {"type": "string"}},
            "required": ["price", "kind"], "additionalProperties": False}},
        "entry_idea": {"type": "string"},
        "stop_idea": {"type": "string"},
        "targets": {"type": "string"},
        "invalidation": {"type": "string"},
        "risks": {"type": "array", "items": {"type": "string"}},
        "answer": {"type": "string", "description": "Direct answer to the trader's question, or empty"},
    },
    "required": ["ticker", "timeframe", "trend", "pattern", "bias", "confidence", "levels", "entry_idea", "stop_idea",
                 "targets", "invalidation", "risks", "answer"],
    "additionalProperties": False,
}

CHART_SYSTEM = """You read stock and options chart screenshots for a retail day trader and give a plain, practical read.

Describe only what is visible in the image. If the ticker, timeframe or prices can't be read, say so instead of \
guessing, and leave a field empty rather than filling it with something you can't see. Give price levels only when \
you can read them from the axis, labels or price boxes.

Judge it the way a disciplined day trader would: the trend and structure, where VWAP and the key levels sit if they \
are shown, where a stop belongs, what would prove the idea wrong, and whether the setup is clean or messy. "wait" is \
a good answer when the picture is unclear or the trade is extended. Never promise an outcome or say a trade will work.

The trader's rules: {rules}. Keep each text field to one or two short sentences."""

ASK_SYSTEM = """You are the assistant inside DayBot, a day-trading tool for one retail trader (stocks and options on \
Robinhood / Chase). You answer their questions using the live context the bot gives you: their account, open \
positions with plans, today's trades and P&L, their rules, the watchlist with levels, recent setup alerts and each \
setup's measured record.

Use only numbers that appear in the context. If something isn't in it, say you don't have it rather than estimating. \
If the trader is about to break one of their own rules, say that first. Be direct and practical; no guarantees, no \
hype. Plain text for a phone screen: short paragraphs or a few "•" bullets, no tables, no markdown headers, under \
200 words."""

COACH_SYSTEM = """You are a trading coach writing three to five short bullet points ("• ") for a retail day trader at \
the end of the day, from the statistics the bot computed (given below). Focus on behaviour the numbers show — what to \
keep doing, the one habit costing the most money, one concrete thing to do tomorrow. Use the numbers given; don't \
invent any. Kind but honest. Under 120 words, plain text."""


def sniff_image(data: bytes) -> str:
    """The real image type from its first bytes (a screenshot sent as a file is often PNG, not JPEG)."""
    head = bytes(data[:12])
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:4] == b"GIF8":
        return "image/gif"
    return ""


class AI:
    def __init__(self, cfg, db):
        self.cfg, self.db = cfg, db
        self.client = None
        self.anthropic = None
        self.last_error = ""
        if not cfg.anthropic_key:
            self.why_off = "no ANTHROPIC_API_KEY in .env"
            return
        try:
            import anthropic
        except ImportError:
            self.why_off = "the anthropic package isn't installed (it needs Python 3.10+): pip install anthropic"
            return
        self.anthropic = anthropic
        self.client = anthropic.AsyncAnthropic(api_key=cfg.anthropic_key, max_retries=2, timeout=120)
        self.why_off = ""

    @property
    def ok(self) -> bool:
        return bool(self.client and self.cfg.flag("AI"))

    def status(self) -> str:
        if self.ok:
            return f"on ({self.cfg.ai_model})"
        return f"off — {self.why_off or 'AI=0'}"

    def _log(self, kind: str, resp, ok: bool, note: str = "") -> None:
        usage = getattr(resp, "usage", None)
        self.db.insert("insert into ai_log(ts, kind, input_tokens, output_tokens, ok, note) values(?,?,?,?,?,?)",
                       (int(time.time()), kind, int(getattr(usage, "input_tokens", 0) or 0),
                        int(getattr(usage, "output_tokens", 0) or 0), int(ok), note[:200]))

    async def _call(self, kind: str, system: str, content, *, schema: dict | None = None, max_tokens: int = 4000,
                    effort: str = "medium"):
        """One request. Returns the text (or parsed JSON with a schema), or None with last_error set."""
        if not self.ok:
            self.last_error = self.why_off or "AI is off"
            return None
        a = self.anthropic
        output_config: dict = {"effort": effort}
        if schema:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        params = dict(model=self.cfg.ai_model, max_tokens=max_tokens, system=system,
                      messages=[{"role": "user", "content": content}], output_config=output_config)
        resp = None
        try:
            try:
                # if Claude declines, the API retries on Anthropic's recommended fallback model
                resp = await self.client.beta.messages.create(**params, betas=["server-side-fallback-2026-07-01"],
                                                              fallbacks="default")
            except (a.BadRequestError, TypeError) as exc:
                if "fallback" not in str(exc).lower() and not isinstance(exc, TypeError):
                    raise
                resp = await self.client.messages.create(**params)
        except a.AuthenticationError:
            self.last_error = "the Anthropic API key was rejected — check ANTHROPIC_API_KEY"
        except a.PermissionDeniedError:
            self.last_error = "the Anthropic API key can't use this model"
        except a.RateLimitError:
            self.last_error = "Claude is rate-limited right now — try again in a minute"
        except a.BadRequestError as exc:
            self.last_error = f"request rejected: {getattr(exc, 'message', exc)}"[:200]
        except a.APIStatusError as exc:
            self.last_error = f"Claude API error {exc.status_code}"
        except a.APIConnectionError:
            self.last_error = "couldn't reach the Claude API"
        if resp is None:
            self._log(kind, None, False, self.last_error)
            return None
        if resp.stop_reason == "refusal":
            self.last_error = "Claude declined this one"
            self._log(kind, resp, False, "refusal")
            return None
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text").strip()
        if resp.stop_reason == "max_tokens" and schema:
            self.last_error = "the answer was cut off"
            self._log(kind, resp, False, "max_tokens")
            return None
        self._log(kind, resp, True)
        if schema:
            try:
                return json.loads(text)
            except ValueError:
                self.last_error = "couldn't read Claude's answer"
                return None
        return text

    def rules_text(self) -> str:
        c = self.cfg
        return (f"risk {c.get('RISK_PER_TRADE_PCT'):g}% of the account per trade, stop for the day at "
                f"-{c.get('DAILY_MAX_LOSS_PCT'):g}%, at most {c.get('MAX_TRADES_PER_DAY'):.0f} trades a day, a clear "
                "stop on every trade and at least 2:1 reward to risk")

    async def read_chart(self, image: bytes, media_type: str = "image/jpeg", question: str = "") -> dict | None:
        media_type = sniff_image(image) or media_type
        prompt = "Read this chart."
        if question.strip():
            prompt += f" The trader asks: {question.strip()[:500]}"
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                         "data": base64.standard_b64encode(image).decode()}},
            {"type": "text", "text": prompt},
        ]
        return await self._call("chart", CHART_SYSTEM.format(rules=self.rules_text()), content, schema=CHART_SCHEMA,
                                max_tokens=6000, effort="medium")

    async def ask(self, question: str, context: dict) -> str | None:
        content = f"Context (live, from the bot):\n{dumps(context)}\n\nQuestion: {question.strip()[:1500]}"
        return await self._call("ask", ASK_SYSTEM, content, max_tokens=4000, effort="medium")

    async def coach(self, stats_text: str) -> str | None:
        return await self._call("coach", COACH_SYSTEM, stats_text[:12000], max_tokens=2000, effort="low")

    def usage_today(self) -> dict:
        start = int(time.time()) - 86400
        row = self.db.row("""select count(*) n, coalesce(sum(input_tokens),0) i, coalesce(sum(output_tokens),0) o
                             from ai_log where ts>=?""", (start,)) or {}
        return {"calls": int(num(row.get("n"))), "input": int(num(row.get("i"))), "output": int(num(row.get("o")))}


def format_chart_read(r: dict, bot_lines: list[str] | None = None) -> str:
    from .util import esc
    head = "🔍 <b>Chart read"
    if r.get("ticker"):
        head += f" · {esc(r['ticker'])}"
    if r.get("timeframe"):
        head += f" ({esc(r['timeframe'])})"
    head += f"</b> — {esc(r.get('bias', '?'))}, {esc(r.get('confidence', '?'))} confidence"
    lines = [head]
    for key, name in (("trend", "Trend"), ("pattern", "Pattern")):
        if r.get(key):
            lines.append(f"{name}: {esc(r[key])}")
    levels = [f"{num(lv.get('price')):.2f} {esc(lv.get('kind', ''))}" for lv in (r.get("levels") or [])[:6]]
    if levels:
        lines.append("Levels: " + " · ".join(levels))
    idea = [x for x in (r.get("entry_idea"), r.get("stop_idea") and f"stop {r['stop_idea']}",
                        r.get("targets") and f"targets {r['targets']}") if x]
    if idea:
        lines.append("Idea: " + esc(" · ".join(idea)))
    if r.get("invalidation"):
        lines.append(f"Wrong if: {esc(r['invalidation'])}")
    if r.get("risks"):
        lines.append("⚠️ " + esc("; ".join(r["risks"][:3])))
    if r.get("answer"):
        lines.append(f"💬 {esc(r['answer'])}")
    if bot_lines:
        lines.append("")
        lines.extend(bot_lines)
    lines.append("<i>An AI read of the picture — an opinion, not a signal. The bot's own numbers are above when it "
                 "knows the ticker.</i>")
    return "\n".join(lines)
