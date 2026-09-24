"""Optional AI second opinion on WIDE alerts (Claude via the official Anthropic SDK).

Off unless ANTHROPIC_API_KEY is set. Runs after the alert is sent, as a separate
follow-up message, so it never delays or blocks an alert. Any error -> no message.
Cost at low effort is roughly half a cent per alert.
"""
from __future__ import annotations

import asyncio
import os

AI_MODEL = os.getenv("AI_VERDICT_MODEL", "claude-opus-5").strip() or "claude-opus-5"
SYSTEM = (
    "You give a skeptical second opinion on a Solana memecoin buy alert for a small manual trader. "
    "Base it only on the data provided. Fees are about 5% round trip, most such coins do not profit, "
    "and rugs are common. Reply with exactly one line: 'BUY SMALL', 'WAIT' or 'SKIP', then ' — ' and "
    "the single most important reason in at most 25 words."
)
_client = None


def enabled() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY", "").strip()) and \
        os.getenv("AI_VERDICT_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


async def ai_verdict(summary: str, timeout: float = 20.0) -> str | None:
    if not enabled():
        return None
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        return None
    global _client
    if _client is None:
        _client = AsyncAnthropic(timeout=timeout, max_retries=1)
    try:
        resp = await asyncio.wait_for(_client.beta.messages.create(
            model=AI_MODEL, max_tokens=2048,
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            output_config={"effort": "low"}, system=SYSTEM,
            messages=[{"role": "user", "content": summary}]), timeout + 5)
    except Exception as exc:  # network, auth, rate limit: the alert already went out
        print(f"[ai-verdict] skipped: {type(exc).__name__}")
        return None
    if resp.stop_reason == "refusal":
        return None
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    return text.splitlines()[0][:300] if text else None
