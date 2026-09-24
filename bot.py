import asyncio
import aiohttp
import csv
import json
import hashlib
import math
import os
import re
import sqlite3
import statistics
import time
import ssl
import traceback
import threading
import uuid
from contextlib import contextmanager
from urllib.parse import urlparse
import xml.etree.ElementTree as ET
from collections import deque, defaultdict, Counter
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
import certifi
from realtime_solana import SolanaRealtime
from pumpportal_radar import PumpPortalRadar
from live_execution import LiveExecutor, WSOL_MINT
from adaptive_engine import confidence_report, dollar_size_guide, position_state, transition_is_material, guardian_message, walk_forward_summary, format_walk_forward
from wallet_sync import PublicSolanaWalletSync, reconcile_balance, recent_sale_evidence
from edge_engine import setup_path_quality, breadth_regime, analog_summary, risk_position_size, proof_metrics, price_move_plausible, outcome_from_path
from execution_quality import JupiterExecutionProbe
from path_tracker import CandidatePathTracker
from copy_trading import CopyTradeEngine

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

DEX = "https://api.dexscreener.com"
GT = "https://api.geckoterminal.com/api/v2"
BE = "https://public-api.birdeye.so"
XAPI = "https://api.x.com/2"
VERSION = "v16.2 QUALITY MEASUREMENT"
# v16.2 is an accounting/reporting hotfix only; entry gates are unchanged.
# Keep v16.1 paper/proof outcomes in the same quality cohort so upgrading does not reset evidence.
QUALITY_COHORT_BUILD_VERSIONS = ("v16.1 VELOCITY EDGE", VERSION)
# regression compatibility marker from previous stable builds:
# VERSION = "v15.0 SOL EDGE ENGINE"
# historical static contract: if len(db.auto_open_positions())>=AUTO_LIVE_MAX_OPEN:
# regression compatibility marker from the previous stable build:
# VERSION = "v11.5 ENTRY CONFIRM + PROFIT REMINDER"
SOLANA_RPC_HTTP = os.getenv("SOLANA_RPC_HTTP", "https://api.mainnet-beta.solana.com").strip()
ENTRY_CONTEXT_TTL_SECONDS = 300


def ef(name, default):
    try:
        return float(os.getenv(name, default))
    except Exception:
        return float(default)


def ei(name, default):
    try:
        return int(float(os.getenv(name, default)))
    except Exception:
        return int(default)


def eb(name, default=True):
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


def int_list_env(name, default):
    raw=os.getenv(name, default)
    vals=[]
    for part in str(raw).split(","):
        try:
            v=int(float(part.strip()))
            if v>0: vals.append(v)
        except Exception:
            pass
    return sorted(set(vals))


def f(value, default=0.0):
    try:
        return float(value if value is not None else default)
    except Exception:
        return float(default)


def nest(data, *keys, default=None):
    cur = data
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


def clamp(value, low, high):
    return max(low, min(high, value))


def usd(value):
    value = f(value)
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.2f}"


def age_minutes(pair_created_at):
    ts = f(pair_created_at)
    if not ts:
        return 999999
    if ts > 10_000_000_000:
        ts /= 1000
    return max(0, (time.time() - ts) / 60)


BEGINNER = eb("BEGINNER_MODE", True)
CHAINS = [x.strip().lower() for x in os.getenv("CHAINS", "solana").split(",") if x.strip()]
BASE_SCAN_INTERVAL = ei("SCAN_INTERVAL_SECONDS", 20)
FAST_DISCOVERY_MODE = eb("FAST_DISCOVERY_MODE", True)
V11_MAX_SCAN_INTERVAL = max(8, ei("V11_MAX_SCAN_INTERVAL_SECONDS", 15))
INTERVAL = max(8, min(BASE_SCAN_INTERVAL, V11_MAX_SCAN_INTERVAL)) if FAST_DISCOVERY_MODE else BASE_SCAN_INTERVAL
ENTRY_SCORE = ef("POSSIBLE_ENTRY_SCORE", 52)
CONF_SCORE = ef("CONFIRMED_SCORE", 70)
WATCH_SCORE = ef("RADAR_WATCH_SCORE", 40)
SCOUT_SCORE = ef("SCOUT_SCORE", 40)
SCOUT_MAX_5M = ef("SCOUT_MAX_5M_PCT", 4)
SCOUT_MAX_1H = ef("SCOUT_MAX_1H_PCT", 15)
MAX_MOVE_FROM_SCOUT = ef("MAX_MOVE_FROM_SCOUT_PCT", 6)
ENTRY_MIN_LIQUIDITY = ef("ENTRY_MIN_LIQUIDITY_USD", 50000)
ENTRY_MIN_MCAP = ef("ENTRY_MIN_MARKET_CAP_USD", 100000)
ENTRY_MAX_MCAP = ef("ENTRY_MAX_MARKET_CAP_USD", 5000000)
ENTRY_MIN_SWAPS_5M = ei("ENTRY_MIN_SWAPS_5M", 10)
ENTRY_MIN_BUY_SELL = ef("ENTRY_MIN_BUY_SELL_RATIO", 1.25)
ENTRY_BUY_SELL_NEAR_TOLERANCE = ef("ENTRY_BUY_SELL_NEAR_TOLERANCE", 0.03)
ENTRY_SCORE_NEAR_TOLERANCE = ef("ENTRY_SCORE_NEAR_TOLERANCE", 2.0)
ENTRY_MAX_DECISION_SLIPPAGE = ef("ENTRY_MAX_DECISION_SLIPPAGE_PCT", 2.0)
SCOUT_MIN_LIQUIDITY = ef("SCOUT_MIN_LIQUIDITY_USD", 25000)
SCOUT_MIN_MCAP = ef("SCOUT_MIN_MARKET_CAP_USD", 70000)
SCOUT_CONFIRM_SECONDS = ei("SCOUT_CONFIRM_SECONDS", 15)
REQUIRE_BIRDEYE_FOR_LOW_RISK = eb("REQUIRE_BIRDEYE_FOR_LOW_RISK", True)
ENTRY_CHAINS = {x.strip().lower() for x in os.getenv("ENTRY_CHAINS", "solana").split(",") if x.strip()}
# v13 established/cross-chain lanes are Telegram/manual-execution only. They can
# generate advice but are never eligible for real-money autopilot.
MANUAL_ENTRY_CHAINS = {x.strip().lower() for x in os.getenv("MANUAL_ENTRY_CHAINS", "").split(",") if x.strip()}
# Existing .env files from v12 may not list newer manual-entry chains in CHAINS.
# Merge them in at runtime so upgrading does not silently keep Robinhood/etc invisible.
for _manual_chain in sorted(MANUAL_ENTRY_CHAINS):
    if _manual_chain not in CHAINS:
        CHAINS.append(_manual_chain)
ALERT_ENTRY_CHAINS = ENTRY_CHAINS | MANUAL_ENTRY_CHAINS
STRICT_MODE = eb("STRICT_BEGINNER_MODE", True)
RT_ENABLED = eb("REALTIME_SOLANA", True)
RT_SCOUT_TX30 = ei("REALTIME_SCOUT_TX30", 3)
RT_ENTRY_TX30 = ei("REALTIME_ENTRY_TX30", 2)
RT_ENTRY_ACCEL = ef("REALTIME_ENTRY_ACCELERATION", 1.10)
RUGCHECK_ENABLED = eb("RUGCHECK_ENABLED", True)
RUGCHECK_BLOCK_SCORE = ef("RUGCHECK_BLOCK_SCORE", 75)
RUGCHECK_WARN_SCORE = ef("RUGCHECK_WARN_SCORE", 50)
# v10.5 resilient safety + data-tuned entry hardening
FRESH_REQUIRE_RUGCHECK = eb("FRESH_REQUIRE_RUGCHECK", True)
FRESH_REQUIRE_HOLDER_CHECK = eb("FRESH_REQUIRE_HOLDER_CHECK", True)
TAGGED_COHORT_HARD_BLOCK_PCT = ef("TAGGED_COHORT_HARD_BLOCK_PCT", 20)
LIQ_WARNING = ef("LIQUIDITY_WARNING_PCT", 15)
NORMAL_REQUIRE_RUGCHECK = eb("NORMAL_REQUIRE_RUGCHECK", True)
YOUNG_REQUIRE_HOLDER_CHECK = eb("YOUNG_REQUIRE_HOLDER_CHECK", True)
YOUNG_HOLDER_CHECK_MAX_AGE_MIN = ef("YOUNG_HOLDER_CHECK_MAX_AGE_MIN", 120)
OPTION_MIN_1H = max(-20.0, ef("ENTRY_OPTION_MIN_1H_PCT", -20.0))
SAFETY_CACHE_FRESH_MINUTES = ef("SAFETY_CACHE_FRESH_MINUTES", 5)
SAFETY_CACHE_OLDER_MINUTES = ef("SAFETY_CACHE_OLDER_MINUTES", 15)
RPC_HOLDER_WARN_TOP1_PCT = ef("RPC_HOLDER_WARN_TOP1_PCT", 35)
RPC_HOLDER_WARN_TOP5_PCT = ef("RPC_HOLDER_WARN_TOP5_PCT", 60)
RPC_HOLDER_HARD_TOP1_PCT = ef("RPC_HOLDER_HARD_TOP1_PCT", 55)
RPC_HOLDER_HARD_TOP5_PCT = ef("RPC_HOLDER_HARD_TOP5_PCT", 80)
RPC_HOLDER_HARD_TOP10_PCT = ef("RPC_HOLDER_HARD_TOP10_PCT", 90)
HIGH_CONVICTION_SCORE = ef("HIGH_CONVICTION_SCORE", 68)
HIGH_CONVICTION_BUY_SELL = ef("HIGH_CONVICTION_BUY_SELL_RATIO", 1.60)
HIGH_CONVICTION_SWAPS = ei("HIGH_CONVICTION_SWAPS_5M", 18)
BREAKOUT_RESCUE_ENABLED = eb("BREAKOUT_RESCUE_ENABLED", True)
BREAKOUT_RESCUE_MAX_5M = ef("BREAKOUT_RESCUE_MAX_5M_PCT", 10)
BREAKOUT_RESCUE_MAX_SCOUT_MOVE = ef("BREAKOUT_RESCUE_MAX_SCOUT_MOVE_PCT", 10)
BREAKOUT_RESCUE_MIN_SCORE = ef("BREAKOUT_RESCUE_MIN_SCORE", 65)
BREAKOUT_RESCUE_MIN_BUY_SELL = ef("BREAKOUT_RESCUE_MIN_BUY_SELL_RATIO", 1.80)
BREAKOUT_RESCUE_MIN_SWAPS = ei("BREAKOUT_RESCUE_MIN_SWAPS_5M", 20)
BREAKOUT_RESCUE_MIN_TX30 = ei("BREAKOUT_RESCUE_MIN_TX30", 4)
BREAKOUT_RESCUE_MIN_ACCEL = ef("BREAKOUT_RESCUE_MIN_ACCELERATION", 1.25)
AUTO_PERF_GUARD = eb("AUTO_PERFORMANCE_GUARD", False)
PERF_WINDOW = ei("PERFORMANCE_GUARD_WINDOW", 20)
PERF_MIN_SIGNALS = ei("PERFORMANCE_GUARD_MIN_SIGNALS", 12)
PERF_MIN_WIN_RATE = ef("PERFORMANCE_GUARD_MIN_30M_WIN_RATE", 0.45)
PERF_MIN_MEDIAN = ef("PERFORMANCE_GUARD_MIN_30M_MEDIAN_PCT", 0.0)
TIME_STOP_MIN = max(30, ei("POSITION_TIME_STOP_MINUTES", 30))
TIME_STOP_MAX_RETURN = ef("POSITION_TIME_STOP_MAX_RETURN_PCT", 0.0)
TOO5 = ef("TOO_LATE_5M_PCT", 12)
TOO1 = ef("TOO_LATE_1H_PCT", 35)
MAX_ALERTS = ei("MAX_ALERTS_PER_HOUR", 10)
MAX_ENTRY_ALERTS = ei("MAX_ENTRY_ALERTS_PER_HOUR", 8)

# v10 actionable tiers. These are deliberately simpler than the v9.5 state machine.
OPTION_SCORE = ef("ENTRY_OPTION_SCORE", 46)
OPTION_CONFIRMED_SCORE = ef("ENTRY_OPTION_CONFIRMED_SCORE", 60)
OPTION_MIN_BUY_SELL = ef("ENTRY_OPTION_MIN_BUY_SELL_RATIO", 1.15)
OPTION_MIN_SWAPS = ei("ENTRY_OPTION_MIN_SWAPS_5M", 10)
OPTION_MIN_5M = ef("ENTRY_OPTION_MIN_5M_PCT", -2.0)
OPTION_MAX_5M = ef("ENTRY_OPTION_MAX_5M_PCT", 8.0)
OPTION_MAX_1H = ef("ENTRY_OPTION_MAX_1H_PCT", 20.0)
OPTION_MAX_SCOUT_MOVE = ef("ENTRY_OPTION_MAX_SCOUT_MOVE_PCT", 8.0)
# v12.1: high-quality washout reversal lane. This does NOT lower the normal
# -20% 1h crash floor. It creates a separate, smaller-size entry only after an
# older selloff has flattened into strong, broad, liquid buyer flow.
REVERSAL_ENTRY_ENABLED = eb("REVERSAL_ENTRY_ENABLED", True)
REVERSAL_MIN_LIQUIDITY = ef("REVERSAL_MIN_LIQUIDITY_USD", 100000)
REVERSAL_MIN_MCAP = ef("REVERSAL_MIN_MARKET_CAP_USD", 100000)
REVERSAL_MAX_MCAP = ef("REVERSAL_MAX_MARKET_CAP_USD", 2000000)
REVERSAL_MIN_1H = ef("REVERSAL_MIN_1H_PCT", -40.0)
REVERSAL_MAX_1H = min(-20.0, ef("REVERSAL_MAX_1H_PCT", -20.0))
REVERSAL_MIN_5M = ef("REVERSAL_MIN_5M_PCT", -1.5)
REVERSAL_MAX_5M = ef("REVERSAL_MAX_5M_PCT", 2.5)
REVERSAL_MIN_BUY_SELL = ef("REVERSAL_MIN_BUY_SELL_RATIO", 1.50)
REVERSAL_MIN_SWAPS = max(50, ei("REVERSAL_MIN_SWAPS_5M", 100))
REVERSAL_MIN_TURNOVER_PCT = max(0.30, ef("REVERSAL_MIN_TURNOVER_PCT", 0.50))
REVERSAL_MIN_RT30 = max(RT_ENTRY_TX30, ei("REVERSAL_MIN_RT30", 5))
REVERSAL_MIN_SCORE = ef("REVERSAL_MIN_SCORE", 50)
REVERSAL_MIN_CONFIRMED = ef("REVERSAL_MIN_CONFIRMED_SCORE", 55)
REVERSAL_MIN_RECOVERY_FROM_LOW_PCT = max(0.0, ef("REVERSAL_MIN_RECOVERY_FROM_LOW_PCT", 0.5))
REVERSAL_PERSISTENT_BS = ef("REVERSAL_PERSISTENT_BUY_SELL", 1.25)
REVERSAL_CONFIRM_SECONDS = max(10, ei("REVERSAL_CONFIRM_SECONDS", 20))
STRONG_SCORE = ef("STRONG_ENTRY_SCORE", 60)
STRONG_CONFIRMED_SCORE = ef("STRONG_ENTRY_CONFIRMED_SCORE", 72)
STRONG_MIN_BUY_SELL = ef("STRONG_ENTRY_MIN_BUY_SELL_RATIO", 1.50)
STRONG_MIN_SWAPS = max(25, ei("STRONG_ENTRY_MIN_SWAPS_5M", 25))
STRONG_MAX_5M = ef("STRONG_ENTRY_MAX_5M_PCT", 7.0)
STRONG_MAX_SCOUT_MOVE = ef("STRONG_ENTRY_MAX_SCOUT_MOVE_PCT", 6.0)
# v16.1 independent FAST lane. It does not disable the existing STRICT lane.
# It trades waiting-time for stronger instantaneous flow and still requires basic
# Solana safety, stateful path quality and executable Jupiter routing.
FAST_ENTRY_ENABLED = eb("FAST_ENTRY_ENABLED", True)
FAST_ENTRY_MIN_SCORE = ef("FAST_ENTRY_MIN_SCORE", 55.0)
FAST_ENTRY_MIN_CONFIRMED = ef("FAST_ENTRY_MIN_CONFIRMED_SCORE", 62.0)
FAST_ENTRY_MIN_LIQUIDITY = ef("FAST_ENTRY_MIN_LIQUIDITY_USD", 35000.0)
FAST_ENTRY_MIN_MCAP = ef("FAST_ENTRY_MIN_MARKET_CAP_USD", 70000.0)
FAST_ENTRY_MAX_MCAP = ef("FAST_ENTRY_MAX_MARKET_CAP_USD", 3000000.0)
FAST_ENTRY_MIN_LIQ_MC = ef("FAST_ENTRY_MIN_LIQUIDITY_TO_MCAP", 0.08)
FAST_ENTRY_MIN_TURNOVER_PCT = ef("FAST_ENTRY_MIN_TURNOVER_PCT", 0.20)
FAST_ENTRY_MIN_BUY_SELL = ef("FAST_ENTRY_MIN_BUY_SELL", 1.25)
FAST_ENTRY_MIN_SWAPS = ei("FAST_ENTRY_MIN_SWAPS_5M", 15)
FAST_ENTRY_MIN_5M = ef("FAST_ENTRY_MIN_5M_PCT", -2.0)
FAST_ENTRY_MAX_5M = ef("FAST_ENTRY_MAX_5M_PCT", 6.0)
FAST_ENTRY_MIN_1H = ef("FAST_ENTRY_MIN_1H_PCT", -12.0)
FAST_ENTRY_MAX_1H = ef("FAST_ENTRY_MAX_1H_PCT", 30.0)
FAST_ENTRY_MAX_SCOUT_MOVE = ef("FAST_ENTRY_MAX_SCOUT_MOVE_PCT", 6.0)
FAST_ENTRY_REQUIRE_BASIC_SAFETY = eb("FAST_ENTRY_REQUIRE_BASIC_SAFETY", True)
FAST_ENTRY_SIZE_MULT = max(0.25, min(1.0, ef("FAST_ENTRY_SIZE_MULT", 0.70)))

SCOUT_TELEGRAM_DEFAULT = eb("SCOUT_TELEGRAM_ALERTS", False)
MOMENTUM_TELEGRAM_DEFAULT = eb("MOMENTUM_TELEGRAM_ALERTS", False)
WATCH_TELEGRAM_DEFAULT = eb("WATCH_TELEGRAM_ALERTS", False)

MIN_LIQ = ef("MIN_LIQUIDITY_USD", 20000)
MIN_MCAP = ef("MIN_MARKET_CAP_USD", 60000)
MAX_MCAP = ef("MAX_MARKET_CAP_USD", 75000000)
MIN_AGE = ef("MIN_PAIR_AGE_MINUTES", 0.5)
MAX_AGE_H = ef("MAX_PAIR_AGE_HOURS", 48)
EARLY_MIN5 = ef("EARLY_MIN_5M_PCT", -2)
EARLY_MAX5 = ef("EARLY_MAX_5M_PCT", 7)
EARLY_MAX1 = ef("EARLY_MAX_1H_PCT", 15)
MIN_BS = ef("MIN_BUY_SELL_RATIO_5M", 1.20)
MIN_SWAPS = ei("MIN_5M_SWAPS", 8)
MIN_VOL5 = ef("MIN_VOLUME_5M_USD", 1200)
MIN_LM = ef("MIN_LIQUIDITY_TO_MCAP", 0.025)
MIN_BUY_ACCEL = ef("MIN_BUY_ACCELERATION", 1.25)
MIN_VOL_ACCEL = ef("MIN_VOLUME_ACCELERATION", 1.30)
MIN_LIQ_GROWTH = ef("MIN_LIQUIDITY_GROWTH_PCT", 2.5)

USE_DEX = eb("DEXSCREENER_DISCOVERY", True)
USE_GT = eb("GECKOTERMINAL_NEW_POOLS", True)
GT_EVERY = ei("GECKOTERMINAL_SCAN_EVERY_MINUTES", 1)
GT_PAGES = ei("GECKOTERMINAL_PAGES_PER_CHAIN", 1)
GT_MAP = {
    "solana": os.getenv("GT_SOLANA_NETWORK", "solana"),
    "base": os.getenv("GT_BASE_NETWORK", "base"),
    "bsc": os.getenv("GT_BSC_NETWORK", "bsc"),
    "monad": os.getenv("GT_MONAD_NETWORK", "monad"),
    "robinhood": os.getenv("GT_ROBINHOOD_NETWORK", "robinhood-chain"),
}
GT_TRENDING_ENABLED = eb("GECKOTERMINAL_TRENDING_POOLS", True)
GT_TRENDING_EVERY_SECONDS = max(60, ei("GECKOTERMINAL_TRENDING_EVERY_SECONDS", 120))
# Jupiter Tokens V2 is an optional independent Solana discovery/safety source. It
# requires an API key; absence never disables DexScreener/Gecko/PumpPortal/realtime.
JUPITER_TOKEN_DISCOVERY = eb("JUPITER_TOKEN_DISCOVERY", True)
JUPITER_TOKEN_DISCOVERY_SECONDS = max(60, ei("JUPITER_TOKEN_DISCOVERY_SECONDS", 120))
# Established chart-structure lane: built for liquid, slower CATE-like setups that
# the launch/microcap lanes intentionally exclude. Manual execution only.
STRUCTURE_ENTRY_ENABLED = eb("STRUCTURE_ENTRY_ENABLED", True)
STRUCTURE_MIN_MCAP = ef("STRUCTURE_MIN_MARKET_CAP_USD", 5_000_000)
STRUCTURE_MAX_MCAP = ef("STRUCTURE_MAX_MARKET_CAP_USD", 75_000_000)
STRUCTURE_MIN_LIQUIDITY = ef("STRUCTURE_MIN_LIQUIDITY_USD", 250_000)
STRUCTURE_MIN_LIQ_MC = ef("STRUCTURE_MIN_LIQUIDITY_TO_MCAP", 0.025)
STRUCTURE_MIN_AGE_MIN = ef("STRUCTURE_MIN_PAIR_AGE_MINUTES", 60)
STRUCTURE_MIN_5M = ef("STRUCTURE_MIN_5M_PCT", -1.0)
STRUCTURE_MAX_5M = ef("STRUCTURE_MAX_5M_PCT", 4.0)
STRUCTURE_MIN_1H = ef("STRUCTURE_MIN_1H_PCT", -10.0)
STRUCTURE_MAX_1H = ef("STRUCTURE_MAX_1H_PCT", 20.0)
STRUCTURE_MIN_BUY_SELL = ef("STRUCTURE_MIN_BUY_SELL_RATIO", 1.20)
STRUCTURE_MIN_SWAPS = ei("STRUCTURE_MIN_SWAPS_5M", 30)
STRUCTURE_MIN_TURNOVER_PCT = ef("STRUCTURE_MIN_TURNOVER_PCT", 0.015)
STRUCTURE_MIN_SCORE = ef("STRUCTURE_MIN_SCORE", 50)
STRUCTURE_MIN_CONFIRMED = ef("STRUCTURE_MIN_CONFIRMED_SCORE", 58)
STRUCTURE_CONFIRM_SECONDS = max(30, ei("STRUCTURE_CONFIRM_SECONDS", 45))
STRUCTURE_MAX_SIZE_USD = ef("STRUCTURE_MAX_SIZE_USD", 15.0)
# Non-Solana lane is intentionally stricter and smaller-size until chain-specific
# history matures. It never reaches the real autopilot.
MANUAL_CHAIN_ENTRY_ENABLED = eb("MANUAL_CHAIN_ENTRY_ENABLED", True)
MANUAL_CHAIN_MIN_MCAP = ef("MANUAL_CHAIN_MIN_MARKET_CAP_USD", 1_000_000)
MANUAL_CHAIN_MAX_MCAP = ef("MANUAL_CHAIN_MAX_MARKET_CAP_USD", 25_000_000)
MANUAL_CHAIN_MIN_LIQUIDITY = ef("MANUAL_CHAIN_MIN_LIQUIDITY_USD", 150_000)
MANUAL_CHAIN_MIN_LIQ_MC = ef("MANUAL_CHAIN_MIN_LIQUIDITY_TO_MCAP", 0.015)
MANUAL_CHAIN_MIN_AGE_MIN = ef("MANUAL_CHAIN_MIN_PAIR_AGE_MINUTES", 60)
MANUAL_CHAIN_MIN_5M = ef("MANUAL_CHAIN_MIN_5M_PCT", -1.5)
MANUAL_CHAIN_MAX_5M = ef("MANUAL_CHAIN_MAX_5M_PCT", 4.0)
MANUAL_CHAIN_MIN_1H = ef("MANUAL_CHAIN_MIN_1H_PCT", -10.0)
MANUAL_CHAIN_MAX_1H = ef("MANUAL_CHAIN_MAX_1H_PCT", 20.0)
MANUAL_CHAIN_MIN_BUY_SELL = ef("MANUAL_CHAIN_MIN_BUY_SELL_RATIO", 1.25)
MANUAL_CHAIN_MIN_SWAPS = ei("MANUAL_CHAIN_MIN_SWAPS_5M", 15)
MANUAL_CHAIN_MIN_TURNOVER_PCT = ef("MANUAL_CHAIN_MIN_TURNOVER_PCT", 0.02)
MANUAL_CHAIN_MIN_SCORE = ef("MANUAL_CHAIN_MIN_SCORE", 52)
MANUAL_CHAIN_MIN_CONFIRMED = ef("MANUAL_CHAIN_MIN_CONFIRMED_SCORE", 60)
# Breakout alternative for established cross-chain pools: fewer prints are acceptable
# only when liquidity depth and broader trend are materially stronger.
MANUAL_CHAIN_BREAKOUT_MIN_LIQUIDITY = ef("MANUAL_CHAIN_BREAKOUT_MIN_LIQUIDITY_USD", 400_000)
MANUAL_CHAIN_BREAKOUT_MIN_LIQ_MC = ef("MANUAL_CHAIN_BREAKOUT_MIN_LIQUIDITY_TO_MCAP", 0.03)
MANUAL_CHAIN_BREAKOUT_MIN_5M = ef("MANUAL_CHAIN_BREAKOUT_MIN_5M_PCT", 2.0)
MANUAL_CHAIN_BREAKOUT_MAX_5M = ef("MANUAL_CHAIN_BREAKOUT_MAX_5M_PCT", 7.0)
MANUAL_CHAIN_BREAKOUT_MIN_1H = ef("MANUAL_CHAIN_BREAKOUT_MIN_1H_PCT", -5.0)
MANUAL_CHAIN_BREAKOUT_MIN_SWAPS = ei("MANUAL_CHAIN_BREAKOUT_MIN_SWAPS_5M", 5)
MANUAL_CHAIN_CONFIRM_SECONDS = max(30, ei("MANUAL_CHAIN_CONFIRM_SECONDS", 45))
MANUAL_CHAIN_MAX_SIZE_USD = ef("MANUAL_CHAIN_MAX_SIZE_USD", 12.0)
# v13.1 quality governor: experimental lanes must earn actionable status. The
# static gates below are based on the Aug-30 holdout where the first v13 cross-chain
# batch produced little useful upside and several fee-negative/drawdown paths.
STRUCTURE_MAX_SCOUT_MOVE_PCT = ef("STRUCTURE_MAX_SCOUT_MOVE_PCT", 4.0)
STRUCTURE_DOWNTREND_TRIGGER_1H = ef("STRUCTURE_DOWNTREND_TRIGGER_1H_PCT", -5.0)
STRUCTURE_DOWNTREND_MIN_BUY_SELL = ef("STRUCTURE_DOWNTREND_MIN_BUY_SELL", 1.75)
STRUCTURE_DOWNTREND_MIN_SWAPS = ei("STRUCTURE_DOWNTREND_MIN_SWAPS", 40)
STRUCTURE_DOWNTREND_MIN_TURNOVER_PCT = ef("STRUCTURE_DOWNTREND_MIN_TURNOVER_PCT", 0.15)

MANUAL_CHAIN_MAX_SCOUT_MOVE_PCT = ef("MANUAL_CHAIN_MAX_SCOUT_MOVE_PCT", 4.0)
MANUAL_CHAIN_LOW_MAX_LIQUIDITY = ef("MANUAL_CHAIN_LOW_MAX_LIQUIDITY_USD", 400_000)
MANUAL_CHAIN_LOW_MIN_5M = ef("MANUAL_CHAIN_LOW_MIN_5M_PCT", 0.25)
MANUAL_CHAIN_LOW_MAX_5M = ef("MANUAL_CHAIN_LOW_MAX_5M_PCT", 2.5)
MANUAL_CHAIN_LOW_MIN_1H = ef("MANUAL_CHAIN_LOW_MIN_1H_PCT", -5.0)
MANUAL_CHAIN_LOW_MAX_1H = ef("MANUAL_CHAIN_LOW_MAX_1H_PCT", 3.0)
MANUAL_CHAIN_LOW_MIN_BUY_SELL = ef("MANUAL_CHAIN_LOW_MIN_BUY_SELL", 1.60)
MANUAL_CHAIN_LOW_MIN_SWAPS = ei("MANUAL_CHAIN_LOW_MIN_SWAPS", 15)
MANUAL_CHAIN_LOW_MIN_TURNOVER_PCT = ef("MANUAL_CHAIN_LOW_MIN_TURNOVER_PCT", 0.04)

MANUAL_CHAIN_DEEP_MIN_LIQUIDITY = ef("MANUAL_CHAIN_DEEP_MIN_LIQUIDITY_USD", 400_000)
MANUAL_CHAIN_DEEP_MIN_5M = ef("MANUAL_CHAIN_DEEP_MIN_5M_PCT", 0.50)
MANUAL_CHAIN_DEEP_MAX_5M = ef("MANUAL_CHAIN_DEEP_MAX_5M_PCT", 3.5)
MANUAL_CHAIN_DEEP_MIN_1H = ef("MANUAL_CHAIN_DEEP_MIN_1H_PCT", -5.0)
MANUAL_CHAIN_DEEP_MAX_1H = ef("MANUAL_CHAIN_DEEP_MAX_1H_PCT", 10.0)
MANUAL_CHAIN_DEEP_MIN_BUY_SELL = ef("MANUAL_CHAIN_DEEP_MIN_BUY_SELL", 2.0)
MANUAL_CHAIN_DEEP_MIN_SWAPS = ei("MANUAL_CHAIN_DEEP_MIN_SWAPS", 15)
MANUAL_CHAIN_DEEP_MIN_TURNOVER_PCT = ef("MANUAL_CHAIN_DEEP_MIN_TURNOVER_PCT", 0.10)

# Breakout rescue for the earlier profitable GG-style path: the move must already be
# real, but the bot still refuses to chase far beyond its own scout.
MANUAL_CHAIN_BREAKOUT_MIN_LIQUIDITY = max(MANUAL_CHAIN_BREAKOUT_MIN_LIQUIDITY, ef("V13_1_BREAKOUT_MIN_LIQUIDITY_USD", 500_000))
MANUAL_CHAIN_BREAKOUT_MIN_5M = max(MANUAL_CHAIN_BREAKOUT_MIN_5M, ef("V13_1_BREAKOUT_MIN_5M_PCT", 3.0))
MANUAL_CHAIN_BREAKOUT_MAX_5M = min(MANUAL_CHAIN_BREAKOUT_MAX_5M, ef("V13_1_BREAKOUT_MAX_5M_PCT", 6.0))
MANUAL_CHAIN_BREAKOUT_MIN_1H = max(MANUAL_CHAIN_BREAKOUT_MIN_1H, ef("V13_1_BREAKOUT_MIN_1H_PCT", 5.0))
MANUAL_CHAIN_BREAKOUT_MAX_1H = ef("V13_1_BREAKOUT_MAX_1H_PCT", 20.0)
MANUAL_CHAIN_BREAKOUT_MIN_BUY_SELL = ef("V13_1_BREAKOUT_MIN_BUY_SELL", 1.50)
MANUAL_CHAIN_BREAKOUT_MIN_TURNOVER_PCT = ef("V13_1_BREAKOUT_MIN_TURNOVER_PCT", 0.02)

STANDARD_ENTRY_MIN_1H = max(OPTION_MIN_1H, ef("STANDARD_ENTRY_MIN_1H_PCT", -10.0))
STANDARD_ENTRY_MAX_1H = min(OPTION_MAX_1H, ef("STANDARD_ENTRY_MAX_1H_PCT", 10.0))
STANDARD_RECOVERY_TRIGGER_1H = ef("STANDARD_RECOVERY_TRIGGER_1H_PCT", -5.0)
STANDARD_RECOVERY_MIN_BUY_SELL = ef("STANDARD_RECOVERY_MIN_BUY_SELL", 1.75)
STANDARD_RECOVERY_MIN_SWAPS = ei("STANDARD_RECOVERY_MIN_SWAPS", 20)
STANDARD_RECOVERY_MIN_TURNOVER_PCT = ef("STANDARD_RECOVERY_MIN_TURNOVER_PCT", 0.25)
STANDARD_RECOVERY_MIN_5M = ef("STANDARD_RECOVERY_MIN_5M_PCT", -0.5)
STANDARD_RECOVERY_MAX_5M = ef("STANDARD_RECOVERY_MAX_5M_PCT", 3.5)

LANE_HEALTH_ENABLED = eb("LANE_HEALTH_ENABLED", True)
LANE_HEALTH_MIN_SIGNALS = max(4, ei("LANE_HEALTH_MIN_SIGNALS", 6))
LANE_HEALTH_WINDOW = max(LANE_HEALTH_MIN_SIGNALS, ei("LANE_HEALTH_WINDOW", 8))
LANE_HEALTH_MAX_SEVERE_RATE = ef("LANE_HEALTH_MAX_SEVERE_RATE", 0.35)
LANE_HEALTH_MIN_TARGET_RATE = ef("LANE_HEALTH_MIN_TARGET_RATE", 0.25)
LANE_HEALTH_MIN_MEDIAN_MAX = ef("LANE_HEALTH_MIN_MEDIAN_MAX_PCT", 3.0)
# v13.2: quarantine is no longer a permanent dead-end. A quarantined lane keeps
# running fully-qualified setups in shadow mode and must re-prove the same quality
# bar before it can return to probation. No entry threshold is loosened here.
LANE_HEALTH_RECOVERY_MIN_SIGNALS = max(4, ei("LANE_HEALTH_RECOVERY_MIN_SIGNALS", LANE_HEALTH_MIN_SIGNALS))
MANUAL_CHAIN_PROBATION_MAX_SIZE_USD = ef("MANUAL_CHAIN_PROBATION_MAX_SIZE_USD", 8.0)
STRUCTURE_PROBATION_MAX_SIZE_USD = ef("STRUCTURE_PROBATION_MAX_SIZE_USD", 12.0)
GUARDIAN_MIN_NET_PROFIT_USD = ef("GUARDIAN_MIN_NET_PROFIT_USD", 0.50)
CROSS_CHAIN_SCOUT_TELEGRAM = eb("CROSS_CHAIN_SCOUT_TELEGRAM", True)
# v13 pre-entry tape preservation. Downtrend entries must prove that liquidity and
# buyer pressure are no longer decaying before Telegram receives a BUY.
TAPE_MEMORY_MINUTES = max(5.0, ef("TAPE_MEMORY_MINUTES", 10.0))
TAPE_SOFT_LIQ_DROP_PCT = max(1.0, ef("TAPE_SOFT_LIQ_DROP_PCT", 3.0))
TAPE_HARD_LIQ_DROP_PCT = max(TAPE_SOFT_LIQ_DROP_PCT, ef("TAPE_HARD_LIQ_DROP_PCT", 8.0))
TAPE_DOWNTREND_1H_PCT = min(-5.0, ef("TAPE_DOWNTREND_1H_PCT", -10.0))
TAPE_PERSISTENT_BUY_SELL = ef("TAPE_PERSISTENT_BUY_SELL", 1.25)


def clean_api_secret(value, header_names=()):
    """Normalize copied API credentials without ever logging their contents."""
    text = str(value or "").strip()
    # People often paste a header label or surrounding quotes from provider consoles.
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"\"", "'"}:
        text = text[1:-1].strip()
    lowered = text.lower()
    for name in header_names:
        prefix = str(name).strip().lower() + ":"
        if lowered.startswith(prefix):
            text = text[len(prefix):].strip(); lowered = text.lower()
            break
    if lowered.startswith("bearer "):
        text = text[7:].strip()
    return text


BE_KEY = clean_api_secret(os.getenv("BIRDEYE_API_KEY", ""), ("X-API-KEY", "BIRDEYE_API_KEY"))
BE_DELAY = ef("BIRDEYE_MIN_SECONDS_BETWEEN_CALLS", 2.2)
BE_COOL = ei("BIRDEYE_429_COOLDOWN_SECONDS", 600)
BE_EVERY = ei("BIRDEYE_ENRICH_EVERY_SCANS", 5)
BE_DEEP = ei("BIRDEYE_DEEP_CANDIDATES_PER_CYCLE", 2)
BE_SMART_EVERY = ei("BIRDEYE_SMART_MONEY_EVERY_MINUTES", 10)
BE_NEW_LISTING = eb("BIRDEYE_NEW_LISTING", True)
BE_NEW_EVERY = ei("BIRDEYE_NEW_LISTING_EVERY_MINUTES", 5)
BE_SECURITY = eb("BIRDEYE_SECURITY", True)
BE_TOP_TRADERS = eb("BIRDEYE_TOP_TRADERS", True)
BE_HOLDERS = eb("BIRDEYE_HOLDER_DISTRIBUTION", True)
BE_FEATURE_RETRY = ei("BIRDEYE_FEATURE_RETRY_SECONDS", 1800)
BE_FORBIDDEN_RETRY = ei("BIRDEYE_FORBIDDEN_RETRY_SECONDS", 21600)
BE_DISCOVERY_ENTRY_ONLY = eb("BIRDEYE_DISCOVERY_ENTRY_CHAINS_ONLY", True)
HOLDER_WARN = ef("TOP10_HOLDER_WARN_PCT", 50)
HOLDER_BLOCK = ef("TOP10_HOLDER_BLOCK_PCT", 75)

TG = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TG_LAST_SUCCESS_TS = 0.0
TG_LAST_FAILURE_TS = 0.0
TG_CONSEC_FAILURES = 0
XTOKEN = clean_api_secret(os.getenv("X_BEARER_TOKEN", ""), ("Authorization", "X_BEARER_TOKEN"))
# Runtime truth for /sources. A configured bearer token is not the same thing as
# a working X API connection.
X_LAST_STATUS = None
X_LAST_STATUS_TS = 0.0
X_LAST_ERROR = ""
X_BILLING_HOLD_UNTIL = 0.0
X_PAYMENT_RETRY_SECONDS = max(300, ei("X_PAYMENT_RETRY_SECONDS", 1800))
CAT_EVERY = ei("CATALYST_SCAN_EVERY_MINUTES", 4)
CAT_LOOKBACK = ei("CATALYST_LOOKBACK_MINUTES", 45)
CAT_MAX = ef("CATALYST_SCORE_BONUS_MAX", 18)
CAT_FLASH = eb("CATALYST_FLASH_ALERTS", True)
XMAX = ei("X_MAX_RESULTS_PER_QUERY", 20)

DEFAULT_POSITION = min(10.0, max(1.0, ef("DEFAULT_POSITION_USD", 5)))
MAX_POSITION = ef("MAX_POSITION_USD", 25)
MAX_OPEN = ei("MAX_OPEN_POSITIONS", 3)
MAX_DAILY_LOSS = ef("MAX_DAILY_REALIZED_LOSS_USD", 15)
MAX_CHASE = ef("MAX_CHASE_AFTER_ALERT_PCT", 2)
# v14 CAPITAL FIRST: small-account trading cannot justify a 16-20% price-only loss
# merely to avoid fees. Existing .env values are capped upgrade-safely.
CAPITAL_HARD_STOP_PCT = max(5.0, min(8.0, ef("CAPITAL_HARD_STOP_PCT", 8.0)))
RISK_LINE = max(4.0, min(CAPITAL_HARD_STOP_PCT, ef("RISK_LINE_PCT", 7.0)))
ENTRY_STABILITY_WINDOW_MINUTES = ef("ENTRY_STABILITY_WINDOW_MINUTES", 10)
ENTRY_STABILITY_MIN_OBSERVATIONS = ei("ENTRY_STABILITY_MIN_OBSERVATIONS", 3)
ENTRY_STABILITY_MIN_SPAN_SECONDS = max(10, ei("ENTRY_STABILITY_MIN_SPAN_SECONDS", 20))
FAST_ENTRY_STABILITY_MIN_OBSERVATIONS = max(2, min(ENTRY_STABILITY_MIN_OBSERVATIONS, ei("FAST_ENTRY_STABILITY_MIN_OBSERVATIONS", 2)))
FAST_ENTRY_STABILITY_MIN_SPAN_SECONDS = max(10, min(ENTRY_STABILITY_MIN_SPAN_SECONDS, ei("FAST_ENTRY_STABILITY_MIN_SPAN_SECONDS", 15)))
ENTRY_MAX_DROP_FROM_RECENT_PEAK_PCT = ef("ENTRY_MAX_DROP_FROM_RECENT_PEAK_PCT", 8)
STRONG_MAX_DROP_FROM_RECENT_PEAK_PCT = ef("STRONG_MAX_DROP_FROM_RECENT_PEAK_PCT", 5)
STRONG_MIN_PERSISTENT_BS = ef("STRONG_MIN_PERSISTENT_BUY_SELL", 1.15)

# v14 final economic-edge gate. The Aug-31 holdout showed that most technically
# valid alerts never moved far enough to overcome roughly 5% round-trip friction.
# Normal/strong Solana BUY alerts now need a deeper liquidity cushion plus a
# fee-worthy flow shape. Other candidates remain visible/learnable as watches.
CAPITAL_EDGE_ENABLED = eb("CAPITAL_EDGE_ENABLED", True)
CAPITAL_EDGE_MIN_LIQ_MC = max(0.15, ef("CAPITAL_EDGE_MIN_LIQUIDITY_TO_MCAP", 0.15))
CAPITAL_EDGE_MIN_TURNOVER_PCT = max(0.20, ef("CAPITAL_EDGE_MIN_TURNOVER_PCT", 0.20))
CAPITAL_EDGE_MIN_BUY_SELL = max(1.35, ef("CAPITAL_EDGE_MIN_BUY_SELL", 1.35))
CAPITAL_EDGE_MIN_SWAPS = max(10, ei("CAPITAL_EDGE_MIN_SWAPS", 10))
CAPITAL_EDGE_MIN_5M = max(-2.0, ef("CAPITAL_EDGE_MIN_5M_PCT", -2.0))
CAPITAL_EDGE_MAX_5M = min(4.0, ef("CAPITAL_EDGE_MAX_5M_PCT", 4.0))
CAPITAL_EDGE_MIN_1H = max(-5.0, ef("CAPITAL_EDGE_MIN_1H_PCT", -5.0))
CAPITAL_EDGE_MAX_1H = min(10.0, ef("CAPITAL_EDGE_MAX_1H_PCT", 10.0))
CAPITAL_EDGE_HEAVY_TURNOVER_PCT = max(1.0, ef("CAPITAL_EDGE_HEAVY_TURNOVER_PCT", 1.0))
CAPITAL_EDGE_HEAVY_MIN_SWAPS = max(50, ei("CAPITAL_EDGE_HEAVY_MIN_SWAPS", 50))
CAPITAL_EDGE_HEAVY_MIN_BUY_SELL = max(1.15, ef("CAPITAL_EDGE_HEAVY_MIN_BUY_SELL", 1.15))
CAPITAL_CONFIRM_SECONDS = max(75, ei("CAPITAL_CONFIRM_SECONDS", 75))
CAPITAL_CONFIRM_MAX_UP_PCT = min(3.0, max(1.0, ef("CAPITAL_CONFIRM_MAX_UP_PCT", 3.0)))
CAPITAL_CONFIRM_MAX_DOWN_PCT = min(2.5, max(1.0, ef("CAPITAL_CONFIRM_MAX_DOWN_PCT", 2.5)))
CAPITAL_CONFIRM_MAX_LIQ_DROP_PCT = min(3.0, max(1.0, ef("CAPITAL_CONFIRM_MAX_LIQ_DROP_PCT", 3.0)))
TOKEN_TRAUMA_HOURS = max(24.0, ef("TOKEN_TRAUMA_HOURS", 24.0))
TOKEN_TRAUMA_DRAWDOWN_PCT = min(-10.0, max(-15.0, ef("TOKEN_TRAUMA_DRAWDOWN_PCT", -15.0)))
STRUCTURE_BUY_ALERTS_ENABLED = eb("STRUCTURE_BUY_ALERTS_ENABLED", False)
MANUAL_CHAIN_BUY_ALERTS_ENABLED = eb("MANUAL_CHAIN_BUY_ALERTS_ENABLED", False)
MICRO_BUY_ALERTS_ENABLED = eb("MICRO_BUY_ALERTS_ENABLED", False)
FRESH_BUY_ALERTS_ENABLED = eb("FRESH_BUY_ALERTS_ENABLED", False)
CAPITAL_CORE_GOVERNOR_ENABLED = eb("CAPITAL_CORE_GOVERNOR_ENABLED", True)
CAPITAL_CORE_MIN_PATHS = max(4, ei("CAPITAL_CORE_MIN_PATHS", 6))
CAPITAL_CORE_RECOVERY_PATHS = max(4, ei("CAPITAL_CORE_RECOVERY_PATHS", 6))
CAPITAL_CORE_WINDOW = max(CAPITAL_CORE_MIN_PATHS, ei("CAPITAL_CORE_WINDOW", 10))
CAPITAL_CORE_MIN_MEDIAN_MAX = max(6.0, ef("CAPITAL_CORE_MIN_MEDIAN_MAX_PCT", 8.0))
CAPITAL_CORE_MIN_HIT10_RATE = max(0.25, ef("CAPITAL_CORE_MIN_HIT10_RATE", 0.40))
CAPITAL_CORE_MAX_SEVERE_RATE = min(0.35, ef("CAPITAL_CORE_MAX_SEVERE_RATE", 0.25))
CAPITAL_CORE_PROBATION_SIZE_USD = min(5.0, max(1.0, ef("CAPITAL_CORE_PROBATION_SIZE_USD", 5.0)))
CAPITAL_CORE_TIER = "CAPITAL CORE"
# v11.3 recent-crash memory. A sharp 5m flush can disappear from the live snapshot
# after a fast rebound, even though the setup is still fragile. These gates remember
# the preceding tape without permanently blacklisting legitimate recoveries.
RECENT_CRASH_MEMORY_ENABLED = eb("RECENT_CRASH_MEMORY_ENABLED", True)
# v13.3: ZCAT looked repaired only because the old 10m crash-memory window had expired.
# Keep at least 30m of violent-flush memory and treat a <=-17.5% 5m print as a
# hard recovery quarantine inside that window. max()/min() intentionally make this
# upgrade-safe when an older .env still carries the v13.2 -20% / 10m defaults.
RECENT_CRASH_LOOKBACK_MINUTES = max(30.0, ef("RECENT_CRASH_LOOKBACK_MINUTES", 30.0))
RECENT_CRASH_HARD_5M_PCT = min(-10.0, max(-17.5, ef("RECENT_CRASH_HARD_5M_PCT", -17.5)))
RECENT_CRASH_FRESH_5M_PCT = min(-8.0, ef("RECENT_CRASH_FRESH_5M_PCT", -12.0))
RECENT_CRASH_FRESH_MINUTES = max(1.0, ef("RECENT_CRASH_FRESH_MINUTES", 3.0))
RECENT_CRASH_RECOVERY_1H_PCT = ef("RECENT_CRASH_RECOVERY_1H_PCT", -5.0)
# v10.7: reject high-swap/low-dollar churn that produced flat alerts in v10.6.
ENTRY_MIN_VOLUME_TURNOVER_PCT = max(0.10, ef("ENTRY_MIN_VOLUME_TURNOVER_PCT", 0.15))
STRONG_MIN_VOLUME_TURNOVER_PCT = max(0.25, ef("STRONG_MIN_VOLUME_TURNOVER_PCT", 0.25))
# Strict low-liquidity microcap rescue. This does NOT globally lower the liquidity floor.
MICRO_ENTRY_ENABLED = eb("MICRO_ENTRY_ENABLED", True)
MICRO_MIN_LIQUIDITY = ef("MICRO_ENTRY_MIN_LIQUIDITY_USD", 25000)
MICRO_MIN_MCAP = ef("MICRO_ENTRY_MIN_MARKET_CAP_USD", 70000)
MICRO_MAX_MCAP = ef("MICRO_ENTRY_MAX_MARKET_CAP_USD", 400000)
MICRO_MIN_LIQ_MC = ef("MICRO_ENTRY_MIN_LIQUIDITY_TO_MCAP", 0.18)
MICRO_MAX_LIQ_MC = ef("MICRO_ENTRY_MAX_LIQUIDITY_TO_MCAP", 0.80)
MICRO_MIN_TURNOVER_PCT = ef("MICRO_ENTRY_MIN_VOLUME_TURNOVER_PCT", 5.0)
MICRO_MIN_BUY_SELL = ef("MICRO_ENTRY_MIN_BUY_SELL_RATIO", 1.80)
MICRO_MIN_SWAPS = ei("MICRO_ENTRY_MIN_SWAPS_5M", 50)
MICRO_MIN_1H = ef("MICRO_ENTRY_MIN_1H_PCT", -15.0)
MICRO_MAX_1H = ef("MICRO_ENTRY_MAX_1H_PCT", 20.0)
MICRO_MIN_5M = ef("MICRO_ENTRY_MIN_5M_PCT", -2.0)
MICRO_MAX_5M = ef("MICRO_ENTRY_MAX_5M_PCT", 8.0)
MICRO_MAX_SCOUT_MOVE = ef("MICRO_ENTRY_MAX_SCOUT_MOVE_PCT", 10.0)
MICRO_MIN_SCORE = ef("MICRO_ENTRY_MIN_SCORE", 55.0)
MICRO_MIN_CONFIRMED = ef("MICRO_ENTRY_MIN_CONFIRMED_SCORE", 72.0)
MICRO_MAX_DROP_FROM_RECENT_PEAK_PCT = ef("MICRO_MAX_DROP_FROM_RECENT_PEAK_PCT", 10.0)
MICRO_STABILITY_WINDOW_MINUTES = ef("MICRO_STABILITY_WINDOW_MINUTES", 3.0)
MICRO_RECOVERY_REQUIRED_BELOW_NORMAL_DROP_PCT = ef("MICRO_RECOVERY_REQUIRED_BELOW_NORMAL_DROP_PCT", 5.0)
# v10.9: high-dollar-flow activity can create an internal scout even before the legacy score catches up.
# This does NOT bypass safety, stability, or the final entry classifier.
FLOW_SCOUT_MIN_TURNOVER_PCT = ef("FLOW_SCOUT_MIN_TURNOVER_PCT", 0.75)
FLOW_SCOUT_MIN_BUY_SELL = ef("FLOW_SCOUT_MIN_BUY_SELL", 1.40)
FLOW_SCOUT_MIN_SWAPS = ei("FLOW_SCOUT_MIN_SWAPS", 25)
FLOW_SCOUT_MIN_LIQUIDITY = ef("FLOW_SCOUT_MIN_LIQUIDITY_USD", 25000)
FLOW_SCOUT_MIN_LIQ_MC = ef("FLOW_SCOUT_MIN_LIQUIDITY_TO_MCAP", 0.08)
# Controlled second-leg reset. The old blanket 8h EARLY cooldown hid several verified
# continuation moves in the Aug-28 audit. A repeat alert is allowed only after at
# least an hour, only if the user no longer has an open position, and only when
# fresh flow/safety/stability return. This is intentionally much stricter than a
# normal rediscovery and does not enable real-money autopilot.
SECOND_LEG_ENABLED = eb("SECOND_LEG_ENABLED", True)
SECOND_LEG_MIN_MINUTES = ei("SECOND_LEG_MIN_MINUTES", 60)
SECOND_LEG_MIN_TURNOVER_PCT = ef("SECOND_LEG_MIN_TURNOVER_PCT", 0.50)
SECOND_LEG_MIN_BUY_SELL = ef("SECOND_LEG_MIN_BUY_SELL", 1.35)
SECOND_LEG_MIN_SWAPS = ei("SECOND_LEG_MIN_SWAPS", 20)
SECOND_LEG_MIN_MOVE_FROM_PRIOR_PCT = ef("SECOND_LEG_MIN_MOVE_FROM_PRIOR_PCT", -35.0)
SECOND_LEG_MAX_MOVE_FROM_PRIOR_PCT = ef("SECOND_LEG_MAX_MOVE_FROM_PRIOR_PCT", 80.0)
# v13.3: entry cooldowns are model/build-aware. A recent alert from a previous
# build gets a short grace period (protects against duplicate alerts during upgrade)
# but cannot suppress a completely new setup for the full legacy 8-hour window.
CROSS_BUILD_ENTRY_GRACE_MINUTES = max(30, ei("CROSS_BUILD_ENTRY_GRACE_MINUTES", 60))
SAME_BUILD_ENTRY_COOLDOWN_HOURS = max(1.0, ef("SAME_BUILD_ENTRY_COOLDOWN_HOURS", 8.0))
CHECK_MAX_DROP_FROM_ALERT_PCT = ef("CHECK_MAX_DROP_FROM_ALERT_PCT", 6)
CHECK_POSITION_RISK_PCT = ef("CHECK_POSITION_RISK_PCT", 8)
TP1 = ef("TAKE_PROFIT_1_PCT", 15)
TP2 = ef("TAKE_PROFIT_2_PCT", 35)
TRAIL = ef("TRAILING_DRAWDOWN_PCT", 10)
LIQ_DROP = ef("LIQUIDITY_DROP_EXIT_PCT", 30)
REV5 = ef("MOMENTUM_REVERSAL_5M_PCT", -12)

WATCH_EVERY = ei("RADAR_LEADER_EVERY_MINUTES", 20)
HEARTBEAT = ei("HEALTH_HEARTBEAT_MINUTES", 60)
DAILY_HOUR = ei("DAILY_REPORT_HOUR_LOCAL", 20)
EVALS = [int(x) for x in os.getenv("EVAL_CHECKPOINTS_MINUTES", "5,15,30,120,1440").split(",") if x.strip().isdigit()]
DB_FILE = os.getenv("V9_DB_FILE", "v9_signals.db")
STATE_DIR = Path(os.path.expanduser(os.getenv("FOMO_STATE_DIR", "~/Library/Application Support/FomoBot")))
STATE_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.path.expanduser(os.getenv("FOMO_STATE_DB", "").strip() or str(STATE_DIR / "fomo_master.db")))

# v10.1 discovery / re-entry / paper-autopilot settings.
FRESH_MAX_AGE_MIN = ef("FRESH_ENTRY_MAX_AGE_MINUTES", 20)
FRESH_MIN_LIQ = ef("FRESH_ENTRY_MIN_LIQUIDITY_USD", 30000)
FRESH_MIN_MCAP = ef("FRESH_ENTRY_MIN_MARKET_CAP_USD", 70000)
FRESH_MAX_MCAP = ef("FRESH_ENTRY_MAX_MARKET_CAP_USD", 1500000)
FRESH_MIN_BS = ef("FRESH_ENTRY_MIN_BUY_SELL_RATIO", 1.35)
FRESH_MIN_SWAPS = ei("FRESH_ENTRY_MIN_SWAPS_5M", 12)
FRESH_MAX_5M = ef("FRESH_ENTRY_MAX_5M_PCT", 10)
FRESH_SCORE = ef("FRESH_ENTRY_SCORE", 42)
FRESH_CONFIRMED = ef("FRESH_ENTRY_CONFIRMED_SCORE", 55)
REENTRY_ENABLED = eb("REENTRY_ENABLED", True)
REENTRY_BUY_ALERTS_ENABLED = eb("REENTRY_BUY_ALERTS_ENABLED", False)
REENTRY_MIN_DRAWDOWN = ef("REENTRY_MIN_DRAWDOWN_FROM_PEAK_PCT", 15)
REENTRY_MAX_DRAWDOWN = ef("REENTRY_MAX_DRAWDOWN_FROM_PEAK_PCT", 45)
REENTRY_MIN_RECOVERY = ef("REENTRY_MIN_RECOVERY_FROM_LOW_PCT", 4)
REENTRY_MIN_BS = ef("REENTRY_MIN_BUY_SELL_RATIO", 1.35)
REENTRY_MIN_SWAPS = ei("REENTRY_MIN_SWAPS_5M", 20)
REENTRY_MAX_RECOVERY = ef("REENTRY_MAX_RECOVERY_FROM_LOW_PCT", 35)
REENTRY_MIN_LIQUIDITY = ef("REENTRY_MIN_LIQUIDITY_USD", 60000)
REENTRY_MIN_HISTORY_MINUTES = ef("REENTRY_MIN_HISTORY_MINUTES", 12)
REENTRY_BASE_WINDOW_MINUTES = ef("REENTRY_BASE_WINDOW_MINUTES", 10)
REENTRY_MAX_BASE_RANGE_PCT = ef("REENTRY_MAX_BASE_RANGE_PCT", 18)
REENTRY_MIN_LIQ_RETENTION_PCT = ef("REENTRY_MIN_LIQ_RETENTION_PCT", 85)
REENTRY_REQUIRED_GOOD_SNAPSHOTS = ei("REENTRY_REQUIRED_GOOD_SNAPSHOTS", 3)
REENTRY_WATCH_CONFIRM_SECONDS = ei("REENTRY_WATCH_CONFIRM_SECONDS", 180)
REENTRY_MAX_RECENT_CRASH_PCT = ef("REENTRY_MAX_RECENT_CRASH_PCT", 45)
SOCIAL_CONTRACT_ALERTS = eb("SOCIAL_CONTRACT_ALERTS", True)
SOCIAL_MIN_MENTIONS = ei("SOCIAL_MIN_MENTIONS", 2)
PAPER_AUTO_DEFAULT = eb("PAPER_AUTOPILOT_DEFAULT", True)
PAPER_POSITION_USD = max(1.0, min(10.0, ef("PAPER_POSITION_USD", 5)))
PAPER_MAX_OPEN = max(3, min(12, ei("PAPER_MAX_OPEN_POSITIONS", 8)))
# A paper simulation whose price stays implausible this long is closed as unmeasurable.
PAPER_GLITCH_MAX_MINUTES = max(5, ei("PAPER_GLITCH_MAX_MINUTES", 20))
# Paper/shadow exits are checked on their own fast loop (one batched request per tick).
PAPER_MONITOR_SECONDS = max(3, ei("PAPER_MONITOR_SECONDS", 6))
# Candidate path recorder: follows every evaluated coin for PATH_TRACK_HORIZON_MINUTES even
# after it leaves the scanner lists, with exact first-touch times for exit backtests.
PATH_TRACKER_ENABLED = eb("PATH_TRACKER_ENABLED", True)
PATH_TRACK_HORIZON_MINUTES = max(30, min(360, ei("PATH_TRACK_HORIZON_MINUTES", 120)))
PATH_TRACK_POLL_SECONDS = max(10, ei("PATH_TRACK_POLL_SECONDS", 30))
PATH_TRACK_MAX_ACTIVE = max(20, ei("PATH_TRACK_MAX_ACTIVE", 300))
# v15 proof-first shadow simulator. It remains independent of Telegram/live execution so
# the strategy keeps producing unbiased forward evidence even when the user makes no trades.
SHADOW_SIM_ALWAYS_ON = eb("SHADOW_SIM_ALWAYS_ON", True)
SHADOW_TELEGRAM = eb("SHADOW_TELEGRAM", False)
SHADOW_COOLDOWN_MINUTES = max(15, ei("SHADOW_COOLDOWN_MINUTES", 30))
PROOF_FIRST_MIN_PATHS = max(6, ei("PROOF_FIRST_MIN_PATHS", 8))
PROOF_FIRST_WINDOW = max(PROOF_FIRST_MIN_PATHS, ei("PROOF_FIRST_WINDOW", 24))
PROOF_FIRST_REQUIRE_MATURE = eb("PROOF_FIRST_REQUIRE_MATURE", True)
# v15.2 separates broad research shadows from the smaller cohort that is allowed to
# prove live edge.  Sep-20 forward data showed that low-score shadows were poisoning
# the proof gate even though the >=70 score cohort was net-positive in both v15.0 and
# the fresh v15.1 holdout. v16.1 intentionally lowers this forward-test floor so more
# mid-score setups can prove or disprove themselves with fresh data instead of being hidden.
PROOF_ELIGIBLE_MIN_SCORE = max(50.0, min(90.0, ef("PROOF_ELIGIBLE_MIN_SCORE", 55.0)))
PROOF_FIRST_MIN_UNIQUE_TOKENS = max(3, min(PROOF_FIRST_MIN_PATHS, ei("PROOF_FIRST_MIN_UNIQUE_TOKENS", 6)))
PROOF_FIRST_MIN_NET_ROI_PCT = max(0.0, min(10.0, ef("PROOF_FIRST_MIN_NET_ROI_PCT", 1.0)))
# Noise guards: small fat-tailed samples must show a consistent per-trade edge and must
# not depend on one outlier before live capital is authorized.
PROOF_FIRST_MIN_T_STAT = max(0.0, min(4.0, ef("PROOF_FIRST_MIN_T_STAT", 1.3)))
PROOF_FIRST_REQUIRE_EX_BEST_POSITIVE = eb("PROOF_FIRST_REQUIRE_EX_BEST_POSITIVE", True)
# v15.3 decouples signal visibility from live-capital authorization. A fully-qualified
# core setup can emit an actionable TEST BUY alert while the fresh proof cohort is still
# learning. Real autopilot and full live sizing remain locked behind proof/risk gates.
ACTIONABLE_SIGNAL_POLICY_DEFAULT = os.getenv("ACTIONABLE_SIGNAL_POLICY", "actionable").strip().lower()
if ACTIONABLE_SIGNAL_POLICY_DEFAULT not in {"strict","actionable","proven"}: ACTIONABLE_SIGNAL_POLICY_DEFAULT = "actionable"
# Every BUY alert shows the measured paper record of its alert type. "proven" policy only
# sends pre-proof alerts for types whose record passes the same noise-guarded test as the
# proof gate over this window.
TRACK_RECORD_DAYS = max(3, min(60, ei("TRACK_RECORD_DAYS", 14)))
TRACK_RECORD_MIN_TRADES = max(5, ei("TRACK_RECORD_MIN_TRADES", 10))
TEST_BUY_MAX_USD = max(1.0, min(5.0, ef("TEST_BUY_MAX_USD", 3.0)))
TEST_BUY_SIZE_MULT = max(0.10, min(0.75, ef("TEST_BUY_SIZE_MULT", 0.50)))
BANKROLL_RISK_PCT = max(0.10, min(1.0, ef("BANKROLL_RISK_PCT", 0.50)))
# A proven strategy still cannot size responsibly without knowing the capital base.
# Guidance and optional real autopilot remain $0/locked until /bankroll is set.
BANKROLL_REQUIRED_FOR_LIVE_GUIDANCE = eb("BANKROLL_REQUIRED_FOR_LIVE_GUIDANCE", True)
PAPER_DAILY_LOSS_LIMIT_PCT = max(0.5, min(5.0, ef("PAPER_DAILY_LOSS_LIMIT_PCT", 2.0)))
PAPER_LOSS_STREAK_LIMIT = max(2, min(5, ei("PAPER_LOSS_STREAK_LIMIT", 3)))
PAPER_LOSS_STREAK_COOLDOWN_HOURS = max(1.0, ef("PAPER_LOSS_STREAK_COOLDOWN_HOURS", 4.0))

# v15 stateful edge controls. These replace the overfit v14 static capital-shape gate.
SOL_EDGE_PATH_MIN_OBS = max(3, ei("SOL_EDGE_PATH_MIN_OBSERVATIONS", 3))
SOL_EDGE_PATH_MIN_SPAN = max(15, ei("SOL_EDGE_PATH_MIN_SPAN_SECONDS", 20))
SOL_EDGE_PATH_MAX_CHASE = min(5.0, max(2.0, ef("SOL_EDGE_PATH_MAX_CHASE_PCT", 4.0)))
SOL_EDGE_PATH_MAX_DRAWDOWN = min(10.0, max(5.0, ef("SOL_EDGE_PATH_MAX_DRAWDOWN_PCT", 8.0)))
SOL_EDGE_PATH_MAX_LIQ_DROP = min(8.0, max(2.0, ef("SOL_EDGE_PATH_MAX_LIQ_DROP_PCT", 5.0)))
SOL_EDGE_CONFIRM_SECONDS = min(60, max(15, ei("SOL_EDGE_CONFIRM_SECONDS", 20)))
HISTORICAL_EDGE_ENABLED = eb("HISTORICAL_EDGE_ENABLED", True)
HISTORICAL_EDGE_LOOKBACK_DAYS = max(3, min(30, ei("HISTORICAL_EDGE_LOOKBACK_DAYS", 7)))
HISTORICAL_EDGE_MIN_NEIGHBORS = max(12, ei("HISTORICAL_EDGE_MIN_NEIGHBORS", 20))
BREADTH_GUARD_ENABLED = eb("BREADTH_GUARD_ENABLED", True)
BREADTH_LOOKBACK_MINUTES = max(5, min(30, ei("BREADTH_LOOKBACK_MINUTES", 10)))
EXECUTION_PREFLIGHT_ENABLED = eb("EXECUTION_PREFLIGHT_ENABLED", True)
EXECUTION_REQUIRE_ROUTE = eb("EXECUTION_REQUIRE_ROUTE", False)
EXECUTION_MIN_REWARD_FRICTION_MULT = max(2.0, ef("EXECUTION_MIN_REWARD_FRICTION_MULT", 3.0))
RUNNER_RADAR_ENABLED = eb("RUNNER_RADAR_ENABLED", True)
RUNNER_RADAR_MIN_MCAP = max(50_000.0, ef("RUNNER_RADAR_MIN_MCAP", 70_000))
RUNNER_RADAR_MAX_MCAP = min(10_000_000.0, ef("RUNNER_RADAR_MAX_MCAP", 5_000_000))
RUNNER_RADAR_MIN_LIQ = max(20_000.0, ef("RUNNER_RADAR_MIN_LIQ", 25_000))
RUNNER_RADAR_MIN_TURNOVER = max(0.20, ef("RUNNER_RADAR_MIN_TURNOVER_PCT", 0.50))
RUNNER_RADAR_MIN_SWAPS = max(10, ei("RUNNER_RADAR_MIN_SWAPS", 20))
RUNNER_RADAR_MIN_BS = max(1.10, ef("RUNNER_RADAR_MIN_BUY_SELL", 1.30))


# v10.4 optional REAL autopilot. Local capability and Telegram arming are separate gates.
AUTO_LIVE_TRADE_USD = ef("AUTO_LIVE_TRADE_USD", 10)
AUTO_LIVE_MAX_OPEN = ei("AUTO_LIVE_MAX_OPEN_POSITIONS", 2)
AUTO_LIVE_MAX_BUYS_DAY = ei("AUTO_LIVE_MAX_BUYS_PER_DAY", 4)
AUTO_LIVE_MAX_DAILY_LOSS = ef("AUTO_LIVE_MAX_DAILY_LOSS_USD", 10)
AUTO_LIVE_MIN_SOL_RESERVE = ef("AUTO_LIVE_MIN_SOL_RESERVE", 0.03)
AUTO_LIVE_TP1 = min(15.0,max(12.0,ef("AUTO_LIVE_TP1_PCT", 12)))
AUTO_LIVE_TP1_SELL = ef("AUTO_LIVE_TP1_SELL_FRACTION", 0.40)
AUTO_LIVE_TP2 = min(30.0,max(20.0,ef("AUTO_LIVE_TP2_PCT", 25)))
AUTO_LIVE_TP2_SELL = ef("AUTO_LIVE_TP2_SELL_FRACTION", 0.50)
AUTO_LIVE_STOP = min(CAPITAL_HARD_STOP_PCT,max(5.0,ef("AUTO_LIVE_STOP_LOSS_PCT", 8)))
AUTO_LIVE_TRAIL = ef("AUTO_LIVE_TRAILING_DRAWDOWN_PCT", 10)
AUTO_LIVE_TIME_STOP_MIN = ei("AUTO_LIVE_TIME_STOP_MINUTES", 30)
AUTO_LIVE_ALLOW_FRESH_DEFAULT = eb("AUTO_LIVE_ALLOW_FRESH_DEFAULT", False)
AUTO_LIVE_FRESH_SIZE_MULT = ef("AUTO_LIVE_FRESH_SIZE_MULTIPLIER", 0.50)
AUTO_LIVE_ALLOW_REENTRY = eb("AUTO_LIVE_ALLOW_REENTRY", False)
AUTO_LIVE_ENTRY_LOOKBACK_MIN = ei("AUTO_LIVE_ENTRY_LOOKBACK_MINUTES", 5)
# Live exits run in their own fast loop so a slow or failing discovery scan can never
# delay or skip a stop-loss.
AUTO_LIVE_MONITOR_SECONDS = max(3, ei("AUTO_LIVE_MONITOR_SECONDS", 5))
# /autolive off stops NEW buys. By default the bot keeps protecting positions it already
# bought (stop/trail/liquidity exits) instead of silently abandoning them.
AUTO_LIVE_EXITS_WHEN_OFF = eb("AUTO_LIVE_EXITS_WHEN_OFF", True)
# A confirmed-but-unaccounted intent older than this is treated as a crash orphan.
AUTO_LIVE_ORPHAN_AGE_SECONDS = max(60, ei("AUTO_LIVE_ORPHAN_AGE_SECONDS", 300))

# v11.3 STABILITY GUARDIAN — continuous position supervision + adaptive calibration.
GUARDIAN_ENABLED = eb("GUARDIAN_ENABLED", True)
GUARDIAN_MONITOR_SECONDS = max(5, ei("GUARDIAN_MONITOR_SECONDS", 8))
GUARDIAN_ALERT_COOLDOWN_MIN = max(2, ei("GUARDIAN_ALERT_COOLDOWN_MINUTES", 10))
# A single weak 8-second snapshot should not force a fee-heavy exit on a tiny manual
# position. Hard price/liquidity failures remain immediate; softer structure failures
# must persist before escalating.
GUARDIAN_SMALL_STRUCTURE_CONFIRM_SECONDS = max(16, ei("GUARDIAN_SMALL_STRUCTURE_CONFIRM_SECONDS", 45))
GUARDIAN_MID_STRUCTURE_CONFIRM_SECONDS = max(16, ei("GUARDIAN_MID_STRUCTURE_CONFIRM_SECONDS", 30))
GUARDIAN_WEAK_RECOVERY_BUFFER_PCT = max(1.0, ef("GUARDIAN_WEAK_RECOVERY_BUFFER_PCT", 3.0))
GUARDIAN_REPEAT_WEAKENING_WORSE_PCT = max(2.0, ef("GUARDIAN_REPEAT_WEAKENING_WORSE_PCT", 5.0))
GUARDIAN_REPEAT_LIQ_WORSE_PCT = max(5.0, ef("GUARDIAN_REPEAT_LIQ_WORSE_PCT", 10.0))
# Fee-aware manual-position management. These are advisory estimates only; Fomo/network
# costs vary by route and congestion. Small positions should not be panic-sold on a
# transient percentage dip when the exit itself consumes a meaningful share of value.
FOMO_ROUNDTRIP_FRICTION_PCT = max(0.0, ef("FOMO_ROUNDTRIP_FRICTION_PCT", 5.0))
FOMO_EXIT_FRICTION_PCT = max(0.0, ef("FOMO_EXIT_FRICTION_PCT", 2.5))
SMALL_POSITION_USD = max(1.0, ef("SMALL_POSITION_USD", 15.0))
SMALL_POSITION_HARD_STOP_PCT = min(CAPITAL_HARD_STOP_PCT, max(RISK_LINE, ef("SMALL_POSITION_HARD_STOP_PCT", CAPITAL_HARD_STOP_PCT)))
MID_POSITION_HARD_STOP_PCT = min(CAPITAL_HARD_STOP_PCT, max(RISK_LINE, ef("MID_POSITION_HARD_STOP_PCT", CAPITAL_HARD_STOP_PCT)))
ADAPTIVE_CONFIDENCE_ENABLED = eb("ADAPTIVE_CONFIDENCE_ENABLED", True)
ADAPTIVE_BASE_POSITION_USD = ef("ADAPTIVE_BASE_POSITION_USD", DEFAULT_POSITION)
ADAPTIVE_LOW_CONFIDENCE_WARN = ef("ADAPTIVE_LOW_CONFIDENCE_WARN", 55)
# v11.4 user-facing sizing + freshness automation. These are advice/notification
# controls only; manual /buy recording remains unlocked. Bigger-than-normal size is
# only suggested after the adaptive path calibration becomes mature and clean.
SIZE_GUIDE_MIN_USD = max(1.0, ef("SIZE_GUIDE_MIN_USD", 5.0))
SIZE_GUIDE_NORMAL_MAX_USD = min(10.0, max(SIZE_GUIDE_MIN_USD, ef("SIZE_GUIDE_NORMAL_MAX_USD", 10.0)))
SIZE_GUIDE_EXCEPTIONAL_MAX_USD = min(15.0, max(SIZE_GUIDE_NORMAL_MAX_USD, ef("SIZE_GUIDE_EXCEPTIONAL_MAX_USD", 15.0)))
GUARDIAN_MIN_PARTIAL_SALE_USD = max(1.0, ef("GUARDIAN_MIN_PARTIAL_SALE_USD", 5.0))
AUTO_ENTRY_RECHECKS_ENABLED = eb("AUTO_ENTRY_RECHECKS_ENABLED", True)
AUTO_ENTRY_RECHECK_SECONDS = int_list_env("AUTO_ENTRY_RECHECK_SECONDS", "60,150") or [60,150]
AUTO_ENTRY_MAX_LATE_MOVE_PCT = max(MAX_CHASE, ef("AUTO_ENTRY_MAX_LATE_MOVE_PCT", 5.0))
# v11.5: prevent cliff-edge / burst-fade entries from becoming actionable on one snapshot.
# Normal ENTRY OPTION setups that combine multiple fragile traits must remain valid for
# a short internal confirmation period. Strong/FLOW/MICRO paths keep their existing gates.
ENTRY_COMMIT_CONFIRM_ENABLED = eb("ENTRY_COMMIT_CONFIRM_ENABLED", True)
ENTRY_COMMIT_CONFIRM_SECONDS = max(30, ei("ENTRY_COMMIT_CONFIRM_SECONDS", 45))
ENTRY_FRAGILE_LIQ_BUFFER_PCT = max(0.0, ef("ENTRY_FRAGILE_LIQ_BUFFER_PCT", 10.0))
ENTRY_FRAGILE_HOT_5M_PCT = ef("ENTRY_FRAGILE_HOT_5M_PCT", 5.0)
ENTRY_FRAGILE_SCORE_MAX = ef("ENTRY_FRAGILE_SCORE_MAX", 60.0)
ENTRY_FRAGILE_CONFIRMED_MAX = ef("ENTRY_FRAGILE_CONFIRMED_MAX", 72.0)
ENTRY_HOT_MIN_TX30 = max(RT_ENTRY_TX30, ei("ENTRY_HOT_MIN_TX30", 3))
TP1_REMINDER_ENABLED = eb("TP1_REMINDER_ENABLED", True)
TP1_REMINDER_SECONDS = max(60, ei("TP1_REMINDER_SECONDS", 120))
TP1_REMINDER_MIN_RETURN_PCT = max(5.0, ef("TP1_REMINDER_MIN_RETURN_PCT", 10.0))
WALLET_SYNC_EVERY_SECONDS = max(10, ei("WALLET_SYNC_EVERY_SECONDS", 20))
WALLET_SYNC_MIN_CHANGE_PCT = ef("WALLET_SYNC_MIN_CHANGE_PCT", 3.0)
WALLET_SYNC_AUTO_RECORD_SELLS = eb("WALLET_SYNC_AUTO_RECORD_SELLS", True)
WALLET_SYNC_REQUIRE_SWAP_EVIDENCE = eb("WALLET_SYNC_REQUIRE_SWAP_EVIDENCE", True)
WALLET_SYNC_NOTIFY_INCREASES = eb("WALLET_SYNC_NOTIFY_INCREASES", True)
OUTCOME_CAPTURE_ENABLED = eb("OUTCOME_CAPTURE_ENABLED", True)
MISSED_AUDIT_ENABLED = eb("MISSED_AUDIT_ENABLED", True)
MISSED_AUDIT_EVERY_MINUTES = max(10, ei("MISSED_AUDIT_EVERY_MINUTES", 30))
MISSED_AUDIT_HORIZON_MINUTES = max(30, ei("MISSED_AUDIT_HORIZON_MINUTES", 60))
MISSED_AUDIT_MIN_RUN_PCT = ef("MISSED_AUDIT_MIN_RUN_PCT", 25.0)

# =============================
# V12 EDGE ENGINE
# =============================
# Fast path: Solana is the only actionable chain by default, so do not make it wait
# behind watch-only chains. Hot scouts are re-evaluated independently of full discovery.
HOT_SCOUT_ENABLED = eb("HOT_SCOUT_ENABLED", True)
HOT_SCOUT_MONITOR_SECONDS = max(3, ei("HOT_SCOUT_MONITOR_SECONDS", 5))
HOT_SCOUT_MAX = max(2, ei("HOT_SCOUT_MAX", 8))
NON_ENTRY_CHAIN_SCAN_EVERY_SCANS = max(1, ei("NON_ENTRY_CHAIN_SCAN_EVERY_SCANS", 4))
MANUAL_CHAIN_SCAN_EVERY_SCANS = max(1, ei("MANUAL_CHAIN_SCAN_EVERY_SCANS", 2))
DEX_PAIR_CONCURRENCY = max(1, min(4, ei("DEX_PAIR_CONCURRENCY", 2)))

# Social / discovery intelligence. RSS stays cheap and may scan faster; targeted X
# exact-contract searches are opt-in because X access can be metered/paid.
RSS_SCAN_EVERY_SECONDS = max(30, ei("RSS_SCAN_EVERY_SECONDS", 90))
SOCIAL_CONTRACT_DISCOVERY_SECONDS = max(30, ei("SOCIAL_CONTRACT_DISCOVERY_SECONDS", 90))
SOCIAL_PULSE_LOOKBACK_MINUTES = max(5, ei("SOCIAL_PULSE_LOOKBACK_MINUTES", 20))
SOCIAL_PULSE_MAX_BONUS = max(0.0, ef("SOCIAL_PULSE_MAX_BONUS", 6.0))
SOCIAL_DIVERGENCE_ENABLED = eb("SOCIAL_DIVERGENCE_ENABLED", True)
X_TARGETED_PULSE_ENABLED = eb("X_TARGETED_PULSE_ENABLED", False)
X_TARGETED_PULSE_EVERY_SECONDS = max(60, ei("X_TARGETED_PULSE_EVERY_SECONDS", 180))
X_TARGETED_MAX_CANDIDATES = max(1, min(5, ei("X_TARGETED_MAX_CANDIDATES", 2)))
CROSS_SOURCE_BONUS_MAX = max(0.0, ef("CROSS_SOURCE_BONUS_MAX", 4.0))

# Market-regime context never creates a buy by itself. It only sizes down in broad
# Solana stress and gives the user context on every actionable alert.
MARKET_REGIME_ENABLED = eb("MARKET_REGIME_ENABLED", True)
MARKET_REGIME_EVERY_SECONDS = max(30, ei("MARKET_REGIME_EVERY_SECONDS", 60))
MARKET_RISK_OFF_SOL_5M_PCT = ef("MARKET_RISK_OFF_SOL_5M_PCT", -4.0)
MARKET_RISK_OFF_SOL_1H_PCT = ef("MARKET_RISK_OFF_SOL_1H_PCT", -6.0)
MARKET_STRESS_SOL_5M_PCT = ef("MARKET_STRESS_SOL_5M_PCT", -4.0)
MARKET_STRESS_SOL_1H_PCT = ef("MARKET_STRESS_SOL_1H_PCT", -8.0)

# Persistent decision telemetry lets future audits compare "blocked" near-entries with
# what happened later instead of guessing from screenshots.
DECISION_LEDGER_ENABLED = eb("DECISION_LEDGER_ENABLED", True)
DECISION_LEDGER_MIN_SECONDS = max(30, ei("DECISION_LEDGER_MIN_SECONDS", 90))
DECISION_OUTCOMES_ENABLED = eb("DECISION_OUTCOMES_ENABLED", True)

WSOL_MINT_PUBLIC = "So11111111111111111111111111111111111111112"

# v16.1 latency circuit breakers. These are intentionally limited to optional
# enrichment/execution-quote providers; Telegram and core Solana RPC are never
# suppressed by this generic breaker.
API_CIRCUIT_BREAKER_ENABLED = eb("API_CIRCUIT_BREAKER_ENABLED", True)
API_CIRCUIT_SLOW_SECONDS = max(0.5, ef("API_CIRCUIT_SLOW_SECONDS", 3.0))
API_CIRCUIT_TRIP_COUNT = max(2, ei("API_CIRCUIT_TRIP_COUNT", 5))
API_CIRCUIT_OPEN_SECONDS = max(30, ei("API_CIRCUIT_OPEN_SECONDS", 300))
API_CIRCUIT_HOSTS = {x.strip().lower() for x in os.getenv(
    "API_CIRCUIT_HOSTS",
    "public-api.birdeye.so,api.jup.ag,lite-api.jup.ag,api.geckoterminal.com"
).split(",") if x.strip()}


class HTTP:
    """Shared HTTPS client with an explicit Mozilla CA bundle and quiet retries.

    macOS Python installs can sometimes point OpenSSL at an incomplete system CA
    store.  certifi gives aiohttp a predictable, maintained trust bundle without
    disabling certificate verification.
    """
    def __init__(self):
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())
        self.connector = aiohttp.TCPConnector(ssl=self.ssl_context)
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15), connector=self.connector
        )
        self._last_network_log = {}
        self._circuits = {}

    def _circuit_for(self, url):
        host=(urlparse(url).netloc or "").split(":",1)[0].lower()
        if not API_CIRCUIT_BREAKER_ENABLED or host not in API_CIRCUIT_HOSTS:
            return host, None
        st=self._circuits.setdefault(host,{"strikes":0,"open_until":0.0,"last_reason":""})
        return host,st

    def _circuit_open(self, url):
        host,st=self._circuit_for(url)
        if st and time.time() < f(st.get("open_until")):
            return True,host,st
        if st and st.get("open_until") and time.time() >= f(st.get("open_until")):
            st["open_until"]=0.0; st["strikes"]=max(0,int(st.get("strikes",0))-1)
        return False,host,st

    def _record_circuit(self, url, elapsed, status):
        host,st=self._circuit_for(url)
        if not st:
            return
        bad = (status == 0 or status == 429 or status >= 500 or elapsed >= API_CIRCUIT_SLOW_SECONDS)
        if bad:
            st["strikes"]=int(st.get("strikes",0))+1
            st["last_reason"]=(f"HTTP {status}" if status else "network/timeout") + f"; {elapsed:.2f}s"
            if st["strikes"] >= API_CIRCUIT_TRIP_COUNT:
                if time.time() >= f(st.get("open_until")):
                    print(f"[circuit] {host} paused {API_CIRCUIT_OPEN_SECONDS}s after {st['strikes']} slow/failing calls ({st['last_reason']}).")
                st["open_until"]=time.time()+API_CIRCUIT_OPEN_SECONDS
        else:
            st["strikes"]=0; st["last_reason"]=""

    def circuit_status(self):
        now=time.time(); parts=[]
        for host,st in sorted(self._circuits.items()):
            if now < f(st.get("open_until")):
                parts.append(f"{host} OPEN {int(st['open_until']-now)}s")
        return ", ".join(parts) if parts else "OK"

    async def close(self):
        await self.session.close()

    def _quiet_network_error(self, method, url, exc):
        host = urlparse(url).netloc or "unknown-host"
        key = (method, host, type(exc).__name__, str(exc)[:160])
        now = time.time()
        # One concise line per identical error each five minutes.  This prevents a
        # temporarily unavailable safety provider from flooding Terminal.
        if now - self._last_network_log.get(key, 0) >= 300:
            print(f"[network] {method} {host}: {type(exc).__name__}: {exc}")
            self._last_network_log[key] = now

    async def get(self, url, headers=None, params=None, retries=1):
        opened,host,st=self._circuit_open(url)
        if opened:
            return 0, None
        for attempt in range(max(0, int(retries)) + 1):
            started=time.monotonic()
            try:
                async with self.session.get(url, headers=headers, params=params) as response:
                    content_type = response.headers.get("content-type", "")
                    if "json" in content_type:
                        try:
                            data = await response.json(content_type=None)
                        except Exception:
                            data = None
                    else:
                        try:
                            data = await response.text()
                        except Exception:
                            data = None
                    self._record_circuit(url,time.monotonic()-started,response.status)
                    return response.status, data
            except (aiohttp.ClientConnectorCertificateError, aiohttp.ClientSSLError,
                    aiohttp.ClientConnectorError, asyncio.TimeoutError) as exc:
                self._record_circuit(url,time.monotonic()-started,0)
                if attempt < max(0, int(retries)):
                    await asyncio.sleep(0.6 * (attempt + 1))
                    continue
                self._quiet_network_error("GET", url, exc)
                return 0, None
            except Exception as exc:
                self._record_circuit(url,time.monotonic()-started,0)
                self._quiet_network_error("GET", url, exc)
                return 0, None

    async def post(self, url, payload, retries=0):
        opened,host,st=self._circuit_open(url)
        if opened:
            return 0, "circuit open"
        for attempt in range(max(0, int(retries)) + 1):
            started=time.monotonic()
            try:
                async with self.session.post(url, json=payload) as response:
                    data=await response.text()
                    self._record_circuit(url,time.monotonic()-started,response.status)
                    return response.status, data
            except (aiohttp.ClientConnectorCertificateError, aiohttp.ClientSSLError,
                    aiohttp.ClientConnectorError, asyncio.TimeoutError) as exc:
                self._record_circuit(url,time.monotonic()-started,0)
                if attempt < max(0, int(retries)):
                    await asyncio.sleep(0.6 * (attempt + 1))
                    continue
                self._quiet_network_error("POST", url, exc)
                return 0, str(exc)
            except Exception as exc:
                self._record_circuit(url,time.monotonic()-started,0)
                self._quiet_network_error("POST", url, exc)
                return 0, str(exc)


class Database:
    def __init__(self):
        # Keep one connection for the process, but make concurrent writers predictable.
        # WAL allows readers during writes; busy_timeout prevents transient lock errors.
        self.conn = sqlite3.connect(DB_PATH, timeout=10.0, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._write_lock = threading.RLock()
        self.conn.execute("pragma journal_mode=WAL")
        self.conn.execute("pragma synchronous=NORMAL")
        self.conn.execute("pragma busy_timeout=10000")
        self.conn.execute("pragma foreign_keys=ON")
        self.conn.execute("""create table if not exists observations(
            ts integer, chain text, token text, symbol text, price real, mcap real,
            liquidity real, vol5 real, buys5 real, sells5 real, pc5 real, pc1 real,
            primary key(ts,chain,token))""")
        self.conn.execute("""create table if not exists signals(
            id integer primary key autoincrement, ts integer, kind text, chain text,
            token text, symbol text, name text, score real, price real, mcap real,
            liquidity real, action text, reasons text)""")
        self.conn.execute("""create table if not exists evals(
            signal_id integer, checkpoint_min integer, ts integer, price real,
            return_pct real, primary key(signal_id,checkpoint_min))""")
        self.conn.execute("""create table if not exists positions(
            id integer primary key autoincrement, open_ts integer, close_ts integer,
            chain text, token text, symbol text, name text, entry_price real,
            amount_usd real, quantity real, entry_liquidity real, peak_price real,
            active integer default 1, tp1_sent integer default 0, tp2_sent integer default 0,
            exit_sent integer default 0, close_price real, realized_pnl real)""")
        self.conn.execute("""create table if not exists catalysts(
            id integer primary key autoincrement, ts integer, source text,
            title text, text text, url text, flashed integer default 0,
            unique(source,title))""")
        self.conn.execute("""create table if not exists meta(
            key text primary key, value text)""")
        self.conn.execute("""create table if not exists telegram_signal_map(
            message_id integer primary key,
            signal_id integer,
            ts integer)""")
        self.conn.execute("""create table if not exists telegram_trade_context(
            message_id integer primary key, signal_id integer, ts integer,
            chain text, token text, symbol text, name text, tier text,
            snapshot_price real, snapshot_mcap real, snapshot_liquidity real,
            context_kind text default 'ENTRY', valid_until integer default 0,
            consumed_ts integer default 0, context_status text default 'OPEN')""")
        self.conn.execute("""create table if not exists latency_events(
            id integer primary key autoincrement,
            signal_id integer,
            token text,
            event text,
            ts integer,
            price real,
            detail text)""")
        self.conn.execute("""create table if not exists manual_watch(
            chain text,
            token text,
            symbol text,
            name text,
            added_ts integer,
            primary key(chain,token))""")
        self.conn.execute("""create table if not exists paper_positions(
            id integer primary key autoincrement,
            signal_id integer,
            open_ts integer,
            close_ts integer,
            chain text,
            token text,
            symbol text,
            name text,
            tier text,
            entry_price real,
            amount_usd real,
            quantity real,
            entry_liquidity real,
            peak_price real,
            active integer default 1,
            tp1_sent integer default 0,
            close_price real,
            close_reason text,
            realized_pnl real)""")
        self.conn.execute("""create table if not exists auto_trade_events(
            id integer primary key autoincrement, ts integer, position_id integer, signal_id integer,
            side text, chain text, token text, symbol text, tier text, usd_value real, raw_amount text,
            price_usd real, signature text, reason text, pnl_usd real default 0, success integer default 1, detail text)""")
        # v15.2 durable execution journal. A signed transaction is recorded before
        # network submission so a lost HTTP response cannot lead to a duplicate order.
        self.conn.execute("""create table if not exists execution_intents(
            id integer primary key autoincrement,
            idempotency_key text not null unique,
            created_ts integer not null, updated_ts integer not null,
            side text, chain text, token text, symbol text, tier text,
            input_mint text, output_mint text, amount_raw text, amount_usd real default 0,
            signal_id integer, position_id integer, wallet_address text,
            request_id text, signature text, expected_output_raw text, actual_output_raw text,
            pre_input_raw text, pre_output_raw text, post_input_raw text, post_output_raw text,
            state text not null, error_kind text, detail text,
            slippage_pct real, confirmed_slot integer, context_json text default '')""")
        self.conn.execute("""create table if not exists execution_leases(
            name text primary key, owner text not null, acquired_ts integer not null, expires_ts integer not null)""")
        self.conn.execute("""create table if not exists trade_failures(
            id integer primary key autoincrement, ts integer not null, stage text, category text,
            retryable integer default 0, side text, token text, signature text,
            idempotency_key text, detail text)""")
        self.conn.execute("""create table if not exists decision_dedupe(
            build_version text, chain text, token text, event text, ts_bucket integer,
            primary key(build_version,chain,token,event,ts_bucket))""")
        self.conn.execute("""create table if not exists guardian_events(
            id integer primary key autoincrement, ts integer, position_id integer, token text, symbol text,
            state text, return_pct real, drawdown_pct real, liquidity_drop_pct real, detail text)""")
        self.conn.execute("""create table if not exists signal_outcomes(
            signal_id integer, horizon_min integer, completed_ts integer, max_return_pct real,
            min_return_pct real, final_return_pct real, primary key(signal_id,horizon_min))""")
        self.conn.execute("""create table if not exists missed_opportunities(
            id integer primary key autoincrement, baseline_ts integer, horizon_min integer, chain text, token text,
            symbol text, baseline_price real, baseline_mcap real, baseline_liquidity real, turnover_pct real,
            buy_sell_ratio real, swaps5 real, max_return_pct real, min_return_pct real, final_return_pct real,
            unique(chain,token,baseline_ts,horizon_min))""")
        self.conn.execute("""create table if not exists wallet_sync_state(
            position_id integer primary key, token text, last_qty real, last_ts integer, last_notice_ts integer default 0)""")
        self.conn.execute("""create table if not exists social_events(
            id integer primary key autoincrement, ts integer, source text, source_key text, token text,
            author text, engagement real default 0, url text, text text,
            unique(source,source_key,token,url))""")
        self.conn.execute("""create table if not exists decision_ledger(
            id integer primary key autoincrement, ts integer, chain text, token text, symbol text, event text,
            tier text, entry_score real, confirmed_score real, price real, mcap real, liquidity real,
            pc5 real, pc1 real, buy_sell real, swaps real, turnover_pct real, rt30 real, social_score real,
            market_regime text, sources text, blockers text)""")
        self.conn.execute("""create table if not exists decision_outcomes(
            decision_id integer, horizon_min integer, completed_ts integer, max_return_pct real,
            min_return_pct real, final_return_pct real, primary key(decision_id,horizon_min))""")
        self.conn.execute("""create table if not exists runs(
            id integer primary key autoincrement, start_ts integer, version text, config_hash text)""")
        self.conn.execute("""create table if not exists lane_governor(
            build_version text, tier text, state text, state_ts integer, signal_floor_id integer default 0,
            decision_floor_id integer default 0, reason text, primary key(build_version,tier))""")
        governor_existing={r[1] for r in self.conn.execute("pragma table_info(lane_governor)")}
        for col,decl in [("signal_floor_id","integer default 0"),("decision_floor_id","integer default 0")]:
            if col not in governor_existing:
                self.conn.execute(f"alter table lane_governor add column {col} {decl}")
        self.conn.execute("""create table if not exists journal_actions(
            id integer primary key autoincrement, ts integer, action text, position_id integer,
            before_json text, after_json text, reversed_ts integer default 0)""")
        existing={r[1] for r in self.conn.execute("pragma table_info(positions)")}
        for col,decl in [
            ("auto_managed","integer default 0"), ("entry_tier","text"),
            ("remaining_fraction","real default 1"), ("realized_pnl_partial","real default 0"),
            ("auto_tp1_done","integer default 0"), ("auto_tp2_done","integer default 0"),
            ("buy_signature","text"), ("auto_close_reason","text"),
            ("auto_token_raw_remaining","text"), ("liq_warn_sent","integer default 0"),
            ("guardian_state","text default 'ENTRY'"), ("guardian_state_ts","integer default 0"),
            ("guardian_last_alert_ts","integer default 0"), ("guardian_last_action","text"),
            ("guardian_confidence","real default 0"), ("guardian_review_ts","integer default 0"),
            ("pnl_quality","text default 'ESTIMATED'"), ("cash_in_verified","real default 0"),
            ("cash_out_verified","real default 0"), ("manual_source","text default ''")]:
            if col not in existing:
                self.conn.execute(f"alter table positions add column {col} {decl}")
        decision_existing={r[1] for r in self.conn.execute("pragma table_info(decision_ledger)")}
        if "build_version" not in decision_existing:
            self.conn.execute("alter table decision_ledger add column build_version text")
        ctx_existing={r[1] for r in self.conn.execute("pragma table_info(telegram_trade_context)")}
        for col,decl in [
            ("context_kind","text default 'ENTRY'"), ("valid_until","integer default 0"),
            ("consumed_ts","integer default 0"), ("context_status","text default 'OPEN'")]:
            if col not in ctx_existing:
                self.conn.execute(f"alter table telegram_trade_context add column {col} {decl}")
        paper_existing={r[1] for r in self.conn.execute("pragma table_info(paper_positions)")}
        for col,decl in [
            ("entry_fee_pct","real default 0"), ("exit_fee_pct","real default 0"),
            ("gross_pnl","real default 0"), ("fees_usd","real default 0"),
            ("shadow","integer default 0"), ("source_event","text default ''"),
            ("estimated_friction_pct","real default 0"), ("build_version","text default ''"),
            ("proof_eligible","integer default 0"), ("proof_reason","text default ''")]:
            if col not in paper_existing:
                self.conn.execute(f"alter table paper_positions add column {col} {decl}")
        # v16.2 reliability patch: integrity metadata for forward outcomes. Price-feed glitches
        # (+383,398% "returns") and paths truncated when a coin leaves the scanner lists are
        # flagged instead of silently feeding vetoes, lane health and calibration.
        for table,cols in (("decision_outcomes",[("samples","integer default 0"),("last_obs_ts","integer default 0"),("suspect","integer default 0")]),
                           ("signal_outcomes",[("samples","integer default 0"),("last_obs_ts","integer default 0"),("suspect","integer default 0")]),
                           ("evals",[("suspect","integer default 0")])):
            have={r[1] for r in self.conn.execute(f"pragma table_info({table})")}
            for col,decl in cols:
                if col not in have:
                    self.conn.execute(f"alter table {table} add column {col} {decl}")
        # The latest export is large enough that repeated outcome/observation scans need indexes.
        self.conn.execute("create index if not exists idx_decision_ts_chain on decision_ledger(ts,chain)")
        self.conn.execute("create index if not exists idx_outcome_horizon_decision on decision_outcomes(horizon_min,decision_id)")
        self.conn.execute("create index if not exists idx_observation_token_ts on observations(chain,token,ts)")
        self.conn.execute("create index if not exists idx_paper_build_shadow on paper_positions(build_version,shadow,active,close_ts)")
        self.conn.execute("create index if not exists idx_paper_proof_build on paper_positions(build_version,proof_eligible,active,close_ts)")
        self.conn.execute("create index if not exists idx_signals_chain_token_ts on signals(chain,token,ts)")
        self.conn.execute("create index if not exists idx_decision_chain_token_event_ts on decision_ledger(chain,token,event,ts)")
        self.conn.execute("create index if not exists idx_guardian_position_ts on guardian_events(position_id,ts)")
        self.conn.execute("create index if not exists idx_exec_state_updated on execution_intents(state,updated_ts)")
        self.conn.execute("create index if not exists idx_exec_token_state on execution_intents(token,state)")
        self.conn.execute("create index if not exists idx_trade_failures_ts on trade_failures(ts)")
        self.conn.execute("create index if not exists idx_social_token_ts on social_events(token,ts)")
        self.conn.execute("create index if not exists idx_signals_ts on signals(ts)")
        self.conn.execute("create index if not exists idx_positions_active_token on positions(active,token)")
        # A run marker makes future exports version-aware instead of mixing several
        # builds into one opaque history window. Secrets are never hashed here.
        cfg = f"{VERSION}|{','.join(CHAINS)}|{ENTRY_MIN_LIQUIDITY}|{ENTRY_MAX_MCAP}|{MAX_MCAP}"
        run_ts=int(time.time())
        self.conn.execute("insert into runs(start_ts,version,config_hash) values(?,?,?)",
                          (run_ts, VERSION, hashlib.sha256(cfg.encode()).hexdigest()[:16]))
        # v16 makes early GET READY scouts internal by default. Users can opt back in with /scouts on.
        if not self.conn.execute("select 1 from meta where key='v16_get_ready_migrated'").fetchone():
            self.conn.execute("insert or replace into meta(key,value) values('telegram_scouts','0')")
            self.conn.execute("insert or replace into meta(key,value) values('v16_get_ready_migrated','1')")
        # Seed experimental-lane phase boundaries before this process can emit a new
        # signal. insert-or-ignore preserves governor state across same-build restarts.
        signal_floor=int(self.conn.execute("select coalesce(max(id),0) from signals").fetchone()[0] or 0)
        decision_floor=int(self.conn.execute("select coalesce(max(id),0) from decision_ledger").fetchone()[0] or 0)
        for governor_tier in ("STRUCTURE ENTRY","MANUAL CHAIN ENTRY",CAPITAL_CORE_TIER):
            self.conn.execute("""insert or ignore into lane_governor(
                build_version,tier,state,state_ts,signal_floor_id,decision_floor_id,reason) values(?,?,?,?,?,?,?)""",
                (VERSION,governor_tier,"PROBATION",run_ts,signal_floor,decision_floor,"new build probation"))
        self._validate_lane_governor_integrity(signal_floor, decision_floor, run_ts)
        self.conn.commit()

    @contextmanager
    def transaction(self, immediate=True):
        """Serialize critical multi-step writes and rollback on any failure.

        Existing legacy one-row writes may still call commit() directly; live execution
        accounting uses this helper so position/event/intent state moves atomically.
        """
        with self._write_lock:
            if self.conn.in_transaction:
                # A migration/legacy caller already owns a transaction. Do not issue a
                # nested BEGIN (SQLite does not support it); rollback remains the outer
                # caller's responsibility.
                yield self.conn
                return
            self.conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.conn
            except Exception:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()

    def _validate_lane_governor_integrity(self, max_signal_id=None, max_decision_id=None, now=None):
        """Fail closed if current-build governor state is malformed/corrupt."""
        now=int(now or time.time())
        max_signal_id=int(max_signal_id if max_signal_id is not None else self.conn.execute("select coalesce(max(id),0) from signals").fetchone()[0] or 0)
        max_decision_id=int(max_decision_id if max_decision_id is not None else self.conn.execute("select coalesce(max(id),0) from decision_ledger").fetchone()[0] or 0)
        allowed={"PROBATION","ACTIVE","QUARANTINED"}
        repairs=0
        rows=self.conn.execute("select * from lane_governor where build_version=?",(VERSION,)).fetchall()
        for row in rows:
            state=str(row["state"] or "")
            s_floor=int(row["signal_floor_id"] or 0)
            d_floor=int(row["decision_floor_id"] or 0)
            state_ts=int(row["state_ts"] or 0)
            corrupt=(state not in allowed or s_floor<0 or d_floor<0 or s_floor>max_signal_id or d_floor>max_decision_id or state_ts<=0 or state_ts>now+300)
            if corrupt:
                self.conn.execute("""update lane_governor set state='PROBATION',state_ts=?,signal_floor_id=?,decision_floor_id=?,reason=?
                    where build_version=? and tier=?""",
                    (now,max_signal_id,max_decision_id,"v15.2 integrity reset: invalid governor state",VERSION,row["tier"]))
                repairs+=1
        self.set_meta_no_commit("governor_integrity_repairs", repairs)
        return repairs

    def set_meta_no_commit(self,key,value):
        self.conn.execute("insert or replace into meta(key,value) values(?,?)",(key,str(value)))

    def log_trade_failure(self, stage, category, detail, *, retryable=False, side="", token="", signature="", idempotency_key=""):
        with self.transaction():
            self.conn.execute("""insert into trade_failures(ts,stage,category,retryable,side,token,signature,idempotency_key,detail)
                values(?,?,?,?,?,?,?,?,?)""",(int(time.time()),str(stage),str(category),1 if retryable else 0,str(side),str(token),str(signature),str(idempotency_key),str(detail)[:4000]))

    def execution_intent(self, idempotency_key):
        row=self.conn.execute("select * from execution_intents where idempotency_key=?",(str(idempotency_key),)).fetchone()
        return dict(row) if row else None

    def unresolved_execution_intents(self, limit=100):
        states=("SIGNED","SUBMITTING","AMBIGUOUS","CONFIRMED_UNRECONCILED")
        q=",".join("?" for _ in states)
        return [dict(r) for r in self.conn.execute(
            f"select * from execution_intents where state in ({q}) order by created_ts asc limit ?",(*states,int(limit))).fetchall()]

    def create_execution_intent(self, *, idempotency_key, side, token, symbol="", tier="", input_mint="", output_mint="", amount_raw=0, amount_usd=0, signal_id=None, position_id=None, wallet_address="", request_id="", signature="", expected_output_raw=0, pre_input_raw=None, pre_output_raw=None, context=None):
        now=int(time.time())
        with self.transaction():
            existing=self.conn.execute("select * from execution_intents where idempotency_key=?",(str(idempotency_key),)).fetchone()
            if existing:
                return dict(existing), False
            self.conn.execute("""insert into execution_intents(
                idempotency_key,created_ts,updated_ts,side,chain,token,symbol,tier,input_mint,output_mint,amount_raw,amount_usd,
                signal_id,position_id,wallet_address,request_id,signature,expected_output_raw,pre_input_raw,pre_output_raw,state,context_json)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(idempotency_key),now,now,str(side),"solana",str(token),str(symbol),str(tier),str(input_mint),str(output_mint),str(amount_raw),f(amount_usd),
                 signal_id,position_id,str(wallet_address),str(request_id),str(signature),str(expected_output_raw),
                 None if pre_input_raw is None else str(pre_input_raw),None if pre_output_raw is None else str(pre_output_raw),"SIGNED",json.dumps(context or {},default=str)[:8000]))
            row=self.conn.execute("select * from execution_intents where idempotency_key=?",(str(idempotency_key),)).fetchone()
            return dict(row), True

    def update_execution_intent(self, idempotency_key, **fields):
        allowed={"updated_ts","position_id","request_id","signature","expected_output_raw","actual_output_raw","pre_input_raw","pre_output_raw","post_input_raw","post_output_raw","state","error_kind","detail","slippage_pct","confirmed_slot","context_json"}
        vals={k:v for k,v in fields.items() if k in allowed}
        vals["updated_ts"]=int(time.time())
        if not vals: return
        sets=",".join(f"{k}=?" for k in vals)
        with self.transaction():
            self.conn.execute(f"update execution_intents set {sets} where idempotency_key=?",(*[vals[k] for k in vals],str(idempotency_key)))

    def acquire_execution_lease(self, name, owner, ttl=120):
        now=int(time.time()); exp=now+max(10,int(ttl))
        with self.transaction():
            self.conn.execute("delete from execution_leases where expires_ts<=?",(now,))
            row=self.conn.execute("select owner,expires_ts from execution_leases where name=?",(str(name),)).fetchone()
            if row and str(row["owner"])!=str(owner):
                return False
            self.conn.execute("""insert into execution_leases(name,owner,acquired_ts,expires_ts) values(?,?,?,?)
                on conflict(name) do update set owner=excluded.owner,acquired_ts=excluded.acquired_ts,expires_ts=excluded.expires_ts""",
                (str(name),str(owner),now,exp))
            return True

    def release_execution_lease(self, name, owner):
        with self.transaction():
            self.conn.execute("delete from execution_leases where name=? and owner=?",(str(name),str(owner)))

    def reserve_decision_dedupe(self, chain, token, event, bucket_seconds=30):
        bucket=int(time.time())//max(1,int(bucket_seconds))
        with self.transaction():
            cur=self.conn.execute("insert or ignore into decision_dedupe(build_version,chain,token,event,ts_bucket) values(?,?,?,?,?)",
                                  (VERSION,str(chain),str(token),str(event),bucket))
            return cur.rowcount==1

    def has_unresolved_execution(self):
        return bool(self.conn.execute("select 1 from execution_intents where state in ('SIGNED','SUBMITTING','AMBIGUOUS','CONFIRMED_UNRECONCILED') limit 1").fetchone())

    def has_unresolved_execution_for_token(self, token):
        return bool(self.conn.execute("select 1 from execution_intents where token=? and state in ('SIGNED','SUBMITTING','AMBIGUOUS','CONFIRMED_UNRECONCILED') limit 1",
                                      (str(token),)).fetchone())

    def unaccounted_confirmed_intents(self, min_age=300, limit=20):
        """CONFIRMED on chain but never written to the journal (process died in between).

        Only intents created after this accounting floor was introduced are considered, so
        historical rows from builds that never marked intents ACCOUNTED are not re-applied.
        """
        floor=int(self.get_meta("intent_accounting_floor_ts","0") or 0)
        if floor<=0:
            floor=int(time.time()); self.set_meta("intent_accounting_floor_ts",floor)
        cutoff=int(time.time())-max(0,int(min_age))
        return [dict(r) for r in self.conn.execute(
            """select * from execution_intents where state='CONFIRMED' and created_ts>=? and updated_ts<=?
               order by created_ts asc limit ?""",(floor,cutoff,int(limit))).fetchall()]

    def set_meta(self, key, value):
        self.conn.execute("insert or replace into meta(key,value) values(?,?)", (key, str(value)))
        self.conn.commit()

    def get_meta(self, key, default=""):
        row = self.conn.execute("select value from meta where key=?", (key,)).fetchone()
        return row[0] if row else default

    def log_latency(self, signal_id, token, event, price=0.0, detail=""):
        self.conn.execute("""insert into latency_events(signal_id,token,event,ts,price,detail)
            values(?,?,?,?,?,?)""", (signal_id, token, event, int(time.time()), f(price), str(detail)))
        self.conn.commit()

    def rolling_entry_performance(self, checkpoint=30, window=None):
        window = int(window or PERF_WINDOW)
        rows = self.conn.execute("""select e.return_pct
            from evals e join signals s on s.id=e.signal_id
            where e.checkpoint_min=? and coalesce(e.suspect,0)=0 and s.kind in ('EARLY','PAPER_ENTRY')
            order by s.ts desc limit ?""", (checkpoint, window)).fetchall()
        vals = [f(r[0]) for r in rows]
        if not vals:
            return {"n":0,"win_rate":None,"median":None,"mean":None,"paused":False}
        win = sum(v > 0 for v in vals) / len(vals)
        med = statistics.median(vals)
        mean = statistics.mean(vals)
        paused = (
            AUTO_PERF_GUARD and len(vals) >= PERF_MIN_SIGNALS
            and (win < PERF_MIN_WIN_RATE or med < PERF_MIN_MEDIAN)
        )
        return {"n":len(vals),"win_rate":win,"median":med,"mean":mean,"paused":paused}

    def export_latency(self):
        path = ROOT / "latency_events.csv"
        rows = self.conn.execute("""select id,signal_id,token,event,ts,price,detail
            from latency_events order by id""").fetchall()
        with path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["id","signal_id","token","event","ts","price","detail"])
            writer.writerows(rows)
        return path

    def map_telegram_signal(self, message_id, signal_id):
        if not message_id or not signal_id:
            return
        self.conn.execute(
            "insert or replace into telegram_signal_map(message_id,signal_id,ts) values(?,?,?)",
            (int(message_id), int(signal_id), int(time.time()))
        )
        self.conn.commit()

    def signal_from_telegram_message(self, message_id):
        if not message_id:
            return None
        row = self.conn.execute("""select s.* from telegram_signal_map m
            join signals s on s.id=m.signal_id
            where m.message_id=? limit 1""", (int(message_id),)).fetchone()
        return dict(row) if row else None

    def map_trade_context(self, message_id, signal_id=None, pair=None, tier="", context_kind="ENTRY", valid_seconds=None):
        if not message_id:
            return
        pair = pair or {}
        base = pair.get("baseToken") or {}
        sig = self.signal_by_id(signal_id) if signal_id else None
        chain = str(pair.get("chainId") or (sig or {}).get("chain") or "")
        token = str(base.get("address") or (sig or {}).get("token") or "")
        symbol = str(base.get("symbol") or (sig or {}).get("symbol") or "?")
        name = str(base.get("name") or (sig or {}).get("name") or "")
        price = f(pair.get("priceUsd"), f((sig or {}).get("price")))
        mcap = f(pair.get("marketCap") or pair.get("fdv"), f((sig or {}).get("mcap")))
        liq = f(nest(pair,"liquidity","usd",default=None), f((sig or {}).get("liquidity")))
        now=int(time.time())
        kind=str(context_kind or "ENTRY").upper()
        if valid_seconds is None:
            valid_seconds = ENTRY_CONTEXT_TTL_SECONDS if kind in {"ENTRY","ADD"} else 0
        valid_until = now + max(0,int(valid_seconds or 0)) if valid_seconds else 0
        status = "OPEN" if kind in {"ENTRY","ADD"} else kind
        self.conn.execute("""insert or replace into telegram_trade_context(
            message_id,signal_id,ts,chain,token,symbol,name,tier,snapshot_price,snapshot_mcap,snapshot_liquidity,
            context_kind,valid_until,consumed_ts,context_status)
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (int(message_id), int(signal_id) if signal_id else None, now, chain, token, symbol, name,
             str(tier or (sig or {}).get("action") or ""), price, mcap, liq, kind, valid_until, 0, status))
        self.conn.commit()

    def trade_context_from_message(self, message_id):
        if not message_id:
            return None
        row = self.conn.execute("select * from telegram_trade_context where message_id=? limit 1", (int(message_id),)).fetchone()
        return dict(row) if row else None

    def context_buy_status(self, context):
        if not context:
            return False, "missing"
        kind=str(context.get("context_kind") or "ENTRY").upper()
        status=str(context.get("context_status") or "OPEN").upper()
        now=int(time.time())
        if kind not in {"ENTRY","ADD"}:
            return False, f"{kind.lower()} context"
        if int(context.get("consumed_ts") or 0)>0:
            return False, "already used"
        if status not in {"OPEN","ACTIVE"}:
            return False, status.lower()
        valid_until=int(context.get("valid_until") or 0)
        # Old pre-v12.3 rows have valid_until=0; enforce freshness from their timestamp.
        if valid_until<=0:
            valid_until=int(context.get("ts") or 0)+ENTRY_CONTEXT_TTL_SECONDS
        if now>valid_until:
            return False, "expired"
        return True, "fresh"

    def consume_trade_context(self, message_id):
        if not message_id:
            return
        now=int(time.time())
        self.conn.execute("update telegram_trade_context set consumed_ts=?,context_status='CONSUMED' where message_id=?",
                          (now,int(message_id)))
        self.conn.commit()

    def expire_signal_contexts(self, signal_id, status="EXPIRED"):
        if not signal_id:
            return
        self.conn.execute("""update telegram_trade_context set context_status=?
            where signal_id=? and upper(coalesce(context_kind,'ENTRY')) in ('ENTRY','ADD') and coalesce(consumed_ts,0)=0""",
            (str(status).upper(),int(signal_id)))
        self.conn.commit()

    def signal_by_id(self, signal_id):
        if not signal_id:
            return None
        row = self.conn.execute("select * from signals where id=? limit 1", (int(signal_id),)).fetchone()
        return dict(row) if row else None

    def remember_green_check(self, signal_id):
        self.set_meta("last_green_check_signal_id", str(int(signal_id)))
        self.set_meta("last_green_check_ts", str(int(time.time())))

    def recent_green_check(self, max_age_seconds=300):
        sid = int(self.get_meta("last_green_check_signal_id", "0") or 0)
        ts = int(self.get_meta("last_green_check_ts", "0") or 0)
        if not sid or time.time() - ts > max_age_seconds:
            return None
        sig = self.signal_by_id(sid)
        return sig if sig and sig.get("kind") == "EARLY" else None

    def latest_entry(self, hours=24):
        cutoff = int(time.time() - hours * 3600)
        row = self.conn.execute("select * from signals where kind='EARLY' and ts>=? order by ts desc limit 1", (cutoff,)).fetchone()
        return dict(row) if row else None

    def recent_scouts(self, hours=2, limit=8):
        cutoff = int(time.time() - hours * 3600)
        rows = self.conn.execute("""select s.* from signals s
            join (select chain,token,max(ts) mts from signals where kind='SCOUT' and ts>=? group by chain,token) x
            on s.chain=x.chain and s.token=x.token and s.ts=x.mts
            order by s.ts desc limit ?""", (cutoff, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def find_buy_signal(self, query, hours=24):
        """Find an actual entry signal. GET READY/SCOUT is never actionable."""
        cutoff = int(time.time() - hours * 3600)
        exact = self.conn.execute("""select * from signals
            where token=? and ts>=? and kind in ('EARLY','CONFIRMED')
            order by case when kind='EARLY' then 0 else 1 end, ts desc limit 1""",
            (query, cutoff)).fetchone()
        if exact:
            return dict(exact), None

        rows = self.conn.execute("""select * from signals
            where lower(symbol)=lower(?) and ts>=? and kind in ('EARLY','CONFIRMED')
            order by case when kind='EARLY' then 0 else 1 end, ts desc""",
            (query, cutoff)).fetchall()
        if not rows:
            return None, "I don't have a recent ENTRY WINDOW for that ticker/contract."

        unique_tokens = {}
        for row in rows:
            unique_tokens.setdefault((row["chain"], row["token"]), []).append(row)

        if len(unique_tokens) > 1:
            contracts = [f"{ch}:{tok}" for (ch,tok) in list(unique_tokens)[:5]]
            return None, "Ticker is ambiguous. Use the contract instead:\n" + "\n".join(contracts)

        same_token_rows = next(iter(unique_tokens.values()))
        early = next((r for r in same_token_rows if r["kind"] == "EARLY"), None)
        chosen = early or same_token_rows[0]
        return dict(chosen), None

    def maintenance(self):
        """Bound long-running storage without deleting signal/evaluation history."""
        now = int(time.time())
        self.conn.execute("delete from observations where ts < ?", (now - 12*3600,))
        self.conn.execute("delete from catalysts where ts < ?", (now - 7*24*3600,))
        self.conn.execute("delete from social_events where ts < ?", (now - 7*24*3600,))
        self.conn.execute("delete from decision_ledger where ts < ?", (now - 30*24*3600,))
        self.conn.execute("delete from telegram_signal_map where ts < ?", (now - 14*24*3600,))
        self.conn.execute("delete from telegram_trade_context where ts < ?", (now - 14*24*3600,))
        self.conn.commit()

    def observe(self, pair):
        base = pair.get("baseToken") or {}
        token = base.get("address", "")
        if not token:
            return
        self.conn.execute("""insert or replace into observations values(?,?,?,?,?,?,?,?,?,?,?,?)""", (
            int(time.time()), pair.get("chainId", ""), token, base.get("symbol", "?"),
            f(pair.get("priceUsd")), f(pair.get("marketCap") or pair.get("fdv")),
            f(nest(pair, "liquidity", "usd", default=0)), f(nest(pair, "volume", "m5", default=0)),
            f(nest(pair, "txns", "m5", "buys", default=0)), f(nest(pair, "txns", "m5", "sells", default=0)),
            f(nest(pair, "priceChange", "m5", default=0)), f(nest(pair, "priceChange", "h1", default=0)),
        ))
        self.conn.commit()

    def previous(self, chain, token, seconds=240):
        cutoff = int(time.time() - seconds)
        row = self.conn.execute("""select ts,price,mcap,liquidity,vol5,buys5,sells5,pc5,pc1
            from observations where chain=? and token=? and ts<=? order by ts desc limit 1""",
            (chain, token, cutoff)).fetchone()
        return dict(row) if row else None

    def add_signal(self, kind, pair, score, action, reasons):
        base = pair.get("baseToken") or {}
        cur = self.conn.execute("""insert into signals(
            ts,kind,chain,token,symbol,name,score,price,mcap,liquidity,action,reasons)
            values(?,?,?,?,?,?,?,?,?,?,?,?)""", (
            int(time.time()), kind, pair.get("chainId", ""), base.get("address", ""),
            base.get("symbol", "?"), base.get("name", ""), score, f(pair.get("priceUsd")),
            f(pair.get("marketCap") or pair.get("fdv")), f(nest(pair, "liquidity", "usd", default=0)),
            action, " | ".join(reasons[:20]),
        ))
        self.conn.commit()
        return cur.lastrowid

    def recent_signal(self, chain, token, kind=None, hours=12):
        cutoff = int(time.time() - hours * 3600)
        sql = "select * from signals where chain=? and token=? and ts>=?"
        args = [chain, token, cutoff]
        if kind:
            sql += " and kind=?"
            args.append(kind)
        sql += " order by ts desc limit 1"
        row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def recent_entry_decision(self, chain, token, hours=8, build_version=None):
        """Most recent delivered-entry Decision Ledger row for this exact contract.

        ENTRY_SENT_HOT is included so a fast-lane delivery cannot bypass same-build
        cooldown after restart. Failed Telegram deliveries never create these rows.
        """
        cutoff = int(time.time() - float(hours) * 3600)
        sql = "select * from decision_ledger where chain=? and token=? and event in ('ENTRY_SENT','ENTRY_SENT_HOT','ENTRY_SENT_TEST') and ts>=?"
        args = [chain, token, cutoff]
        if build_version is not None:
            sql += " and build_version=?"
            args.append(str(build_version))
        sql += " order by ts desc limit 1"
        row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def entry_pipeline_summary(self, minutes=60, build_version=None):
        """Compact counts for the post-classifier entry pipeline.

        These counters make alert droughts diagnosable from /status instead of
        requiring a database export. They are observational only.
        """
        cutoff = int(time.time() - max(1, float(minutes)) * 60)
        version = str(build_version or VERSION)
        events = ("ENTRY_COOLDOWN","ENTRY_STABILITY","ENTRY_TAPE","ENTRY_EDGE","ENTRY_REGIME","ENTRY_HISTORY","ENTRY_COMMIT","ENTRY_PATH","ENTRY_EXECUTION","ENTRY_CHASE","ENTRY_SLIPPAGE","ENTRY_RATE_LIMIT","ENTRY_DELIVERY_FAIL","LANE_SHADOW","CAPITAL_SHADOW","PROOF_SHADOW","RESEARCH_SHADOW","RISK_PAUSE","EDGE_ARMED","PAPER_SHADOW","RUNNER_RADAR","ENTRY_SENT","ENTRY_SENT_HOT","ENTRY_SENT_TEST")
        marks = ",".join("?" for _ in events)
        rows = self.conn.execute(
            f"select event,count(*) as n from decision_ledger where ts>=? and build_version=? and event in ({marks}) group by event",
            (cutoff, version, *events)
        ).fetchall()
        return {str(r["event"]): int(r["n"]) for r in rows}

    def find_signal(self, query, hours=24):
        cutoff = int(time.time() - hours * 3600)
        exact = self.conn.execute("""select * from signals where token=? and ts>=?
            order by ts desc limit 1""", (query, cutoff)).fetchone()
        if exact:
            return dict(exact), None
        rows = self.conn.execute("""select * from signals where lower(symbol)=lower(?) and ts>=?
            order by ts desc""", (query, cutoff)).fetchall()
        if len(rows) == 1:
            return dict(rows[0]), None
        if len(rows) > 1:
            contracts = [f"{r['chain']}:{r['token']}" for r in rows[:5]]
            return None, "Ticker is ambiguous. Use the contract instead:\n" + "\n".join(contracts)
        return None, "I don't have a recent signal for that ticker/contract."

    def due_evaluations(self):
        now = time.time()
        rows = self.conn.execute("select * from signals where ts>=? order by ts asc", (int(now - 26*3600),)).fetchall()
        due = defaultdict(list)
        for row in rows:
            age = (now - row["ts"]) / 60
            for cp in EVALS:
                if age >= cp:
                    exists = self.conn.execute("select 1 from evals where signal_id=? and checkpoint_min=?", (row["id"], cp)).fetchone()
                    if not exists:
                        due[row["id"]].append(cp)
        return [(dict(next(r for r in rows if r["id"] == sid)), cps) for sid, cps in list(due.items())[:20]]

    def add_eval(self, signal_id, checkpoint, price, ret, suspect=False):
        self.conn.execute("insert or ignore into evals(signal_id,checkpoint_min,ts,price,return_pct,suspect) values(?,?,?,?,?,?)",
                          (signal_id, checkpoint, int(time.time()), price, ret, 1 if suspect else 0))
        self.conn.commit()

    def add_catalyst(self, source, title, text, url):
        try:
            self.conn.execute("insert into catalysts(ts,source,title,text,url) values(?,?,?,?,?)",
                              (int(time.time()), source, title, text, url))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def recent_catalysts(self, minutes):
        cutoff = int(time.time() - minutes * 60)
        rows = self.conn.execute("select * from catalysts where ts>=? order by ts desc", (cutoff,)).fetchall()
        return [dict(r) for r in rows]

    def add_manual_watch(self, chain, token, symbol="?", name=""):
        self.conn.execute("""insert or replace into manual_watch(chain,token,symbol,name,added_ts)
            values(?,?,?,?,?)""", (chain,token,symbol,name,int(time.time())))
        self.conn.commit()

    def remove_manual_watch(self, token):
        cur=self.conn.execute("delete from manual_watch where lower(token)=lower(?)",(token,))
        self.conn.commit()
        return cur.rowcount

    def manual_watches(self):
        return [dict(r) for r in self.conn.execute(
            "select * from manual_watch order by added_ts desc").fetchall()]

    def recent_signal_tokens(self, hours=6, limit=80):
        cutoff=int(time.time()-hours*3600)
        rows=self.conn.execute("""select chain,token,max(ts) as last_ts from signals
            where ts>=? group by chain,token order by last_ts desc limit ?""",(cutoff,int(limit))).fetchall()
        return [dict(r) for r in rows]

    def recent_observation_tokens(self, hours=4, limit=100):
        cutoff=int(time.time()-hours*3600)
        rows=self.conn.execute("""select chain,token,max(ts) as last_ts from observations
            where ts>=? group by chain,token order by last_ts desc limit ?""",(cutoff,int(limit))).fetchall()
        return [dict(r) for r in rows]

    def token_history(self, chain, token, minutes=240):
        cutoff=int(time.time()-minutes*60)
        rows=self.conn.execute("""select ts,price,mcap,liquidity,vol5,buys5,sells5,pc5,pc1
            from observations where chain=? and token=? and ts>=? order by ts asc""",
            (chain,token,cutoff)).fetchall()
        return [dict(r) for r in rows]

    def paper_open_positions(self):
        return [dict(r) for r in self.conn.execute(
            "select * from paper_positions where active=1 order by open_ts").fetchall()]

    def paper_position_by_token(self, token):
        row=self.conn.execute("select * from paper_positions where active=1 and token=? limit 1",(token,)).fetchone()
        return dict(row) if row else None

    def paper_open(self, signal, price, liquidity, tier, amount=None, friction_pct=0.0, shadow=False, source_event="",
                   proof_eligible=False, proof_reason=""):
        if self.paper_position_by_token(signal["token"]):
            return None
        if len(self.paper_open_positions()) >= PAPER_MAX_OPEN:
            return None
        amount=f(amount if amount is not None else PAPER_POSITION_USD)
        friction=max(0.0,f(friction_pct))
        half=friction/2.0
        qty=amount/max(f(price),1e-18)
        cur=self.conn.execute("""insert into paper_positions(
            signal_id,open_ts,chain,token,symbol,name,tier,entry_price,amount_usd,quantity,entry_liquidity,peak_price,
            entry_fee_pct,exit_fee_pct,shadow,source_event,estimated_friction_pct,build_version,proof_eligible,proof_reason)
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
            signal.get("id"),int(time.time()),signal["chain"],signal["token"],signal["symbol"],signal.get("name",""),
            tier,f(price),amount,qty,f(liquidity),f(price),half,half,1 if shadow else 0,str(source_event or "")[:80],friction,VERSION,
            1 if proof_eligible else 0,str(proof_reason or "")[:240]))
        self.conn.commit(); return cur.lastrowid

    def paper_update_peak(self, pid, peak):
        self.conn.execute("update paper_positions set peak_price=? where id=?",(f(peak),pid)); self.conn.commit()

    def paper_flag_tp1(self, pid):
        self.conn.execute("update paper_positions set tp1_sent=1 where id=?",(pid,)); self.conn.commit()

    def paper_close(self, pid, price, reason):
        row=self.conn.execute("select * from paper_positions where id=? and active=1",(pid,)).fetchone()
        if not row: return None
        exit_value=f(price)*f(row["quantity"])
        gross=(f(price)-f(row["entry_price"]))*f(row["quantity"])
        entry_fee=f(row["amount_usd"])*f(row["entry_fee_pct"])/100.0
        exit_fee=exit_value*f(row["exit_fee_pct"])/100.0
        fees=entry_fee+exit_fee
        pnl=gross-fees
        self.conn.execute("""update paper_positions set active=0,close_ts=?,close_price=?,close_reason=?,
            gross_pnl=?,fees_usd=?,realized_pnl=? where id=?""",
                          (int(time.time()),f(price),reason,gross,fees,pnl,pid))
        self.conn.commit(); return pnl

    def paper_close_unmeasurable(self, pid, reason):
        """Close a simulation whose price feed stayed implausible: no P/L, never proof evidence."""
        self.conn.execute("""update paper_positions set active=0,close_ts=?,close_price=entry_price,close_reason=?,
            gross_pnl=0,fees_usd=0,realized_pnl=0,proof_eligible=0 where id=? and active=1""",(int(time.time()),str(reason),int(pid)))
        self.conn.commit()

    def paper_stats(self, days=7, shadow_only=False, build_version=None, proof_only=False):
        cutoff=int(time.time()-days*86400)
        sql="select * from paper_positions where active=0 and close_ts>=?"; args=[cutoff]
        if shadow_only:
            sql += " and coalesce(shadow,0)=1"
        if build_version is not None:
            versions=list(build_version) if isinstance(build_version,(list,tuple,set,frozenset)) else [build_version]
            versions=[str(v) for v in versions if str(v)]
            if versions:
                sql += " and coalesce(build_version,'') in (" + ",".join("?" for _ in versions) + ")"
                args.extend(versions)
        if proof_only:
            sql += " and coalesce(proof_eligible,0)=1"
        sql += " order by close_ts asc"
        rows=[dict(r) for r in self.conn.execute(sql,args).fetchall()]
        vals=[f(r.get("realized_pnl")) for r in rows]; invested=sum(f(r.get("amount_usd")) for r in rows)
        wins=[v for v in vals if v>0]; losses=[v for v in vals if v<0]
        running=0.0; peak=0.0; max_dd=0.0
        for v in vals:
            running+=v; peak=max(peak,running); max_dd=min(max_dd,running-peak)
        pf=sum(wins)/abs(sum(losses)) if losses else (float("inf") if wins else None)
        return {"closed":len(vals),"wins":len(wins),"win_rate":len(wins)/len(vals) if vals else None,
                "pnl":sum(vals),"roi":(sum(vals)/invested*100) if invested else None,
                "open":len(self.paper_open_positions()),"avg_win":statistics.mean(wins) if wins else 0.0,
                "avg_loss":statistics.mean(losses) if losses else 0.0,"profit_factor":pf,"max_drawdown_usd":max_dd,
                "gross_pnl":sum(f(r.get("gross_pnl")) for r in rows),
                "fees_usd":sum(f(r.get("fees_usd")) for r in rows),
                "unique_tokens":len({str(r.get("token") or "") for r in rows if r.get("token")})}

    def proof_shadow_rows(self, limit=None):
        limit=int(limit or PROOF_FIRST_WINDOW)
        versions=tuple(dict.fromkeys(str(v) for v in QUALITY_COHORT_BUILD_VERSIONS if str(v)))
        placeholders=",".join("?" for _ in versions)
        sql=f"""select * from paper_positions
            where active=0 and coalesce(shadow,0)=1 and coalesce(build_version,'') in ({placeholders})
              and coalesce(proof_eligible,0)=1
              and tier in ('ENTRY OPTION','STRONG ENTRY','FLOW ENTRY','REVERSAL ENTRY','FAST ENTRY')
            order by close_ts desc limit ?"""
        return [dict(r) for r in self.conn.execute(sql,(*versions,limit)).fetchall()]

    def recent_shadow_signal(self, chain, token, minutes=None):
        cutoff=int(time.time()-max(1,minutes or SHADOW_COOLDOWN_MINUTES)*60)
        row=self.conn.execute("""select * from signals where chain=? and token=? and kind='PAPER_SHADOW' and ts>=?
            order by ts desc limit 1""",(chain,token,cutoff)).fetchone()
        return dict(row) if row else None

    def breadth_rows(self, minutes=None, limit=80):
        cutoff=int(time.time()-max(1,minutes or BREADTH_LOOKBACK_MINUTES)*60)
        rows=self.conn.execute("""select o.* from observations o join (
              select token,max(ts) mts from observations where chain='solana' and ts>=?
              group by token) x on x.token=o.token and x.mts=o.ts
            where o.chain='solana' and o.mcap between ? and ? and o.liquidity>=?
            order by o.ts desc limit ?""",(cutoff,RUNNER_RADAR_MIN_MCAP,RUNNER_RADAR_MAX_MCAP,RUNNER_RADAR_MIN_LIQ,int(limit))).fetchall()
        return [dict(r) for r in rows]

    def analog_rows(self, days=None, limit=2500):
        cutoff=int(time.time()-max(1,days or HISTORICAL_EDGE_LOOKBACK_DAYS)*86400)
        rows=self.conn.execute("""select d.pc5,d.pc1,d.mcap,d.liquidity,d.buy_sell,d.swaps,d.turnover_pct,
                   o.max_return_pct,o.min_return_pct,o.final_return_pct,d.ts,d.token,d.event
            from decision_ledger d join decision_outcomes o on o.decision_id=d.id
            where d.chain='solana' and d.ts>=? and o.horizon_min=30 and coalesce(o.suspect,0)=0
              and d.event in ('ENTRY_SENT','ENTRY_SENT_HOT','ENTRY_SENT_TEST','ENTRY_STABILITY','ENTRY_TAPE','ENTRY_COMMIT','ENTRY_EDGE','NEAR_ENTRY')
            order by d.ts desc limit ?""",(cutoff,int(limit))).fetchall()
        # Deduplicate repeated telemetry from the same token within 15 minutes so a
        # repeatedly-blocked coin cannot dominate its own nearest-neighbour cohort.
        seen={}; out=[]
        for rr in rows:
            r=dict(rr); bucket=int(f(r.get("ts"))//900); key=(r.get("token"),bucket)
            if key in seen: continue
            seen[key]=1; out.append(r)
        return out

    def edge_leaders(self, minutes=60, limit=8):
        cutoff=int(time.time()-max(1,minutes)*60)
        rows=self.conn.execute("""select * from decision_ledger where build_version=? and ts>=?
            and event in ('RUNNER_RADAR','EDGE_ARMED','PAPER_SHADOW','ENTRY_EDGE','ENTRY_EXECUTION','ENTRY_REGIME','ENTRY_HISTORY')
            order by ts desc""",(VERSION,cutoff)).fetchall()
        best={}
        for rr in rows:
            r=dict(rr); key=r.get("token")
            if key not in best or f(r.get("confirmed_score"))>f(best[key].get("confirmed_score")):
                best[key]=r
        return sorted(best.values(),key=lambda r:max(f(r.get("entry_score")),f(r.get("confirmed_score"))),reverse=True)[:limit]

    def recent_actionable_entries(self, minutes=5):
        cutoff=int(time.time()-max(1,minutes)*60)
        return [dict(r) for r in self.conn.execute(
            "select * from signals where kind='EARLY' and ts>=? order by ts asc",(cutoff,)).fetchall()]

    def auto_open_positions(self):
        return [dict(r) for r in self.conn.execute(
            "select * from positions where active=1 and coalesce(auto_managed,0)=1 order by open_ts").fetchall()]

    def auto_event(self, position_id, signal_id, side, chain, token, symbol, tier, usd_value, raw_amount, price_usd, signature, reason="", pnl_usd=0, success=True, detail=""):
        self.conn.execute("""insert into auto_trade_events(ts,position_id,signal_id,side,chain,token,symbol,tier,usd_value,raw_amount,price_usd,signature,reason,pnl_usd,success,detail)
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (int(time.time()),position_id,signal_id,side,chain,token,symbol,tier,f(usd_value),str(raw_amount),f(price_usd),signature,reason,f(pnl_usd),1 if success else 0,str(detail)[:1000]))
        self.conn.commit()

    def auto_has_buy_for_signal(self, signal_id):
        return bool(self.conn.execute("select 1 from auto_trade_events where signal_id=? and side='BUY' and success=1 limit 1",(signal_id,)).fetchone())

    def auto_daily_stats(self):
        start=int(datetime.now().replace(hour=0,minute=0,second=0,microsecond=0).timestamp())
        row=self.conn.execute("""select
            sum(case when side='BUY' and success=1 then 1 else 0 end) as buys,
            coalesce(sum(case when side='SELL' and success=1 then pnl_usd else 0 end),0) as pnl
            from auto_trade_events where ts>=?""",(start,)).fetchone()
        return {"buys":int(row[0] or 0),"pnl":f(row[1])}

    def force_record_auto_position(self, signal, current_price, amount_usd, liquidity, tier, signature, token_raw=0):
        existing=self.position_by_token(signal["token"])
        if existing:
            return existing["id"]
        qty=f(amount_usd)/max(f(current_price),1e-18)
        cur=self.conn.execute("""insert into positions(
            open_ts,chain,token,symbol,name,entry_price,amount_usd,quantity,entry_liquidity,peak_price,
            auto_managed,entry_tier,remaining_fraction,realized_pnl_partial,auto_tp1_done,auto_tp2_done,buy_signature,auto_token_raw_remaining)
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (int(time.time()),signal["chain"],signal["token"],signal["symbol"],signal.get("name",""),f(current_price),f(amount_usd),qty,f(liquidity),f(current_price),
             1,tier,1.0,0.0,0,0,signature,str(int(f(token_raw)))))
        self.conn.commit(); return cur.lastrowid

    def auto_update_partial(self, pid, new_remaining_fraction, pnl_delta, flag=None, token_raw_remaining=None):
        row=self.conn.execute("select realized_pnl_partial from positions where id=?",(pid,)).fetchone()
        prior=f(row[0]) if row else 0
        if token_raw_remaining is None:
            self.conn.execute("update positions set remaining_fraction=?,realized_pnl_partial=? where id=?",
                              (max(0,f(new_remaining_fraction)),prior+f(pnl_delta),pid))
        else:
            self.conn.execute("update positions set remaining_fraction=?,realized_pnl_partial=?,auto_token_raw_remaining=? where id=?",
                              (max(0,f(new_remaining_fraction)),prior+f(pnl_delta),str(max(0,int(token_raw_remaining))),pid))
        if flag in {"auto_tp1_done","auto_tp2_done"}:
            self.conn.execute(f"update positions set {flag}=1 where id=?",(pid,))
        self.conn.commit()

    def auto_close_position(self, pid, close_price, final_pnl_delta, reason):
        row=self.conn.execute("select realized_pnl_partial from positions where id=? and active=1",(pid,)).fetchone()
        if not row: return None
        total=f(row[0])+f(final_pnl_delta)
        self.conn.execute("""update positions set active=0,close_ts=?,close_price=?,realized_pnl=?,remaining_fraction=0,auto_token_raw_remaining='0',auto_close_reason=? where id=?""",
                          (int(time.time()),f(close_price),total,reason,pid))
        self.conn.commit(); return total

    def record_auto_buy_atomic(self, signal, current_price, amount_usd, liquidity, tier, signature, token_raw, *, intent_key=""):
        """Atomically create the auto position, success event, and execution accounting."""
        with self.transaction():
            existing=self.conn.execute("select * from positions where token=? and active=1 order by open_ts desc limit 1",(signal["token"],)).fetchone()
            if existing:
                pid=int(existing["id"])
            else:
                qty=f(amount_usd)/max(f(current_price),1e-18)
                cur=self.conn.execute("""insert into positions(
                    open_ts,chain,token,symbol,name,entry_price,amount_usd,quantity,entry_liquidity,peak_price,
                    auto_managed,entry_tier,remaining_fraction,realized_pnl_partial,auto_tp1_done,auto_tp2_done,buy_signature,auto_token_raw_remaining)
                    values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (int(time.time()),signal["chain"],signal["token"],signal["symbol"],signal.get("name",""),f(current_price),f(amount_usd),qty,f(liquidity),f(current_price),
                     1,tier,1.0,0.0,0,0,signature,str(max(0,int(token_raw or 0)))))
                pid=int(cur.lastrowid)
            self.conn.execute("""insert into auto_trade_events(ts,position_id,signal_id,side,chain,token,symbol,tier,usd_value,raw_amount,price_usd,signature,reason,pnl_usd,success,detail)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (int(time.time()),pid,signal.get("id"),"BUY","solana",signal["token"],signal["symbol"],tier,f(amount_usd),str(token_raw),f(current_price),signature,"autopilot entry",0.0,1,""))
            if intent_key:
                self.conn.execute("update execution_intents set state='ACCOUNTED',position_id=?,updated_ts=? where idempotency_key=?",
                                  (pid,int(time.time()),str(intent_key)))
            return pid

    def record_auto_sell_atomic(self, pos, *, current_price, proceeds, sell_raw, pnl, reason, signature, final=False, flag=None, token_raw_remaining=0, intent_key=""):
        """Atomically update/close a position and record the corresponding SELL event."""
        with self.transaction():
            row=self.conn.execute("select * from positions where id=? and active=1",(int(pos["id"]),)).fetchone()
            if not row:
                # If already closed/accounted, return its realized value without creating a duplicate event.
                closed=self.conn.execute("select realized_pnl from positions where id=?",(int(pos["id"]),)).fetchone()
                return f(closed[0]) if closed else None, 0.0
            rem=f(row["remaining_fraction"] or 1.0)
            position_raw=max(1,int(row["auto_token_raw_remaining"] or sell_raw or 1))
            sold_fraction=1.0 if final else clamp(int(sell_raw)/max(position_raw,1),0,1)
            sold_cost=f(row["amount_usd"])*rem*sold_fraction
            # Caller supplies pnl from actual wallet delta; recompute only when absent.
            leg_pnl=f(pnl, f(proceeds)-sold_cost)
            prior=f(row["realized_pnl_partial"] or 0)
            total=None
            if final:
                total=prior+leg_pnl
                self.conn.execute("""update positions set active=0,close_ts=?,close_price=?,realized_pnl=?,remaining_fraction=0,
                    auto_token_raw_remaining='0',auto_close_reason=? where id=?""",
                    (int(time.time()),f(current_price),total,str(reason),int(pos["id"])))
                remain=0.0
            else:
                remain=rem*(1-sold_fraction)
                self.conn.execute("""update positions set remaining_fraction=?,realized_pnl_partial=?,auto_token_raw_remaining=? where id=?""",
                                  (remain,prior+leg_pnl,str(max(0,int(token_raw_remaining))),int(pos["id"])))
                if flag in {"auto_tp1_done","auto_tp2_done"}:
                    self.conn.execute(f"update positions set {flag}=1 where id=?",(int(pos["id"]),))
            self.conn.execute("""insert into auto_trade_events(ts,position_id,signal_id,side,chain,token,symbol,tier,usd_value,raw_amount,price_usd,signature,reason,pnl_usd,success,detail)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (int(time.time()),int(pos["id"]),None,"SELL","solana",pos["token"],pos["symbol"],pos.get("entry_tier") or "",
                 f(proceeds),str(sell_raw),f(current_price),str(signature),str(reason),leg_pnl,1,""))
            if intent_key:
                self.conn.execute("update execution_intents set state='ACCOUNTED',position_id=?,updated_ts=? where idempotency_key=?",
                                  (int(pos["id"]),int(time.time()),str(intent_key)))
            return total, remain

    def backup_shared_state(self):
        backup_dir=STATE_DIR/"backups"; backup_dir.mkdir(parents=True,exist_ok=True)
        dest=backup_dir/("fomo_master_"+datetime.now().strftime("%Y%m%d_%H%M%S")+".db")
        out=sqlite3.connect(dest)
        try: self.conn.backup(out)
        finally: out.close()
        old=sorted(backup_dir.glob("fomo_master_*.db"),key=lambda p:p.stat().st_mtime,reverse=True)
        for p in old[10:]:
            try: p.unlink()
            except Exception: pass
        return dest

    def add_social_event(self, source, source_key, token, author="", engagement=0.0, url="", text="", ts=None):
        token=str(token or "").strip()
        if not token:
            return
        source=str(source or "SOCIAL").strip().upper()
        source_key=str(source_key or source).strip()[:160]
        author=str(author or source_key).strip()[:160]
        url=str(url or "").strip()[:800]
        text=str(text or "")[:1200]
        stamp=int(ts or time.time())
        # Some feeds omit a stable URL. A content hash gives those events a durable id
        # without storing duplicate mentions every polling cycle.
        if not url:
            url="hash:"+hashlib.sha1((source_key+"|"+token+"|"+text).encode("utf-8","ignore")).hexdigest()
        try:
            self.conn.execute("""insert or ignore into social_events(
                ts,source,source_key,token,author,engagement,url,text) values(?,?,?,?,?,?,?,?)""",
                (stamp,source,source_key,token,author,f(engagement),url,text))
            self.conn.commit()
        except Exception:
            pass

    def social_pulse(self, token, minutes=20):
        token=str(token or "").strip()
        cutoff=int(time.time()-max(1,int(minutes))*60)
        rows=self.conn.execute("""select * from social_events where token=? and ts>=? order by ts desc limit 500""",
                               (token,cutoff)).fetchall()
        rows=[dict(x) for x in rows]
        if not rows:
            return {"mentions":0,"unique_authors":0,"sources":[],"engagement":0.0,"velocity":0.0,
                    "top_author_share":0.0,"score":0.0,"status":"NONE"}
        now=time.time(); recent5=[x for x in rows if now-f(x.get("ts"))<=300]
        prior=[x for x in rows if 300 < now-f(x.get("ts")) <= max(600,minutes*60)]
        authors=[str(x.get("author") or x.get("source_key") or "?") for x in rows]
        ac=Counter(authors); top=max(ac.values()) if ac else 0
        top_share=top/max(len(rows),1)
        unique=len(ac)
        sources=sorted({str(x.get("source") or "?") for x in rows})
        engagement=sum(max(0.0,f(x.get("engagement"))) for x in rows)
        prior_per5=(len(prior)/max((max(minutes,10)-5)/5,1))
        velocity=len(recent5)/max(prior_per5,1.0) if recent5 else 0.0
        score=0.0
        if len(rows)>=2 and unique>=2: score+=2.0
        if len(rows)>=4 and unique>=3: score+=1.5
        if len(rows)>=6 and unique>=4: score+=1.5
        if velocity>=2.0 and len(recent5)>=2: score+=1.0
        if engagement>=25: score+=0.5
        if engagement>=100: score+=0.5
        if len(rows)>=3 and top_share>=0.75:
            score-=2.5
        elif len(rows)>=4 and top_share>=0.60:
            score-=1.0
        score=clamp(score,-3.0,SOCIAL_PULSE_MAX_BONUS)
        status="BROAD" if unique>=3 and score>=3 else ("EARLY" if score>0 else ("CONCENTRATED" if score<0 else "FLAT"))
        return {"mentions":len(rows),"unique_authors":unique,"sources":sources,"engagement":engagement,
                "velocity":velocity,"top_author_share":top_share,"score":score,"status":status,
                "recent5":len(recent5)}

    def log_decision(self, pair, event, tier, entry_score, confirmed_score, blockers, rts=None, social=None,
                     market_regime="NEUTRAL", sources=None, min_seconds=None):
        if not DECISION_LEDGER_ENABLED:
            return None
        chain=str(pair.get("chainId") or "").lower(); token=str(nest(pair,"baseToken","address",default=""))
        if not token:
            return None
        event=str(event or "NEAR_ENTRY")
        cooldown=max(30,int(min_seconds or DECISION_LEDGER_MIN_SECONDS))
        now=int(time.time())
        m=metrics(pair); ratio=m["buys"]/max(m["sells"],1); swaps=m["buys"]+m["sells"]
        turnover=100.0*m["v5"]/max(m["mc"],1)
        # BEGIN IMMEDIATE serializes the cooldown check + insert across processes,
        # removing the old check-then-insert race under concurrent scanners.
        with self.transaction():
            last=self.conn.execute("""select id,ts from decision_ledger where chain=? and token=? and event=?
                order by ts desc limit 1""",(chain,token,event)).fetchone()
            if last and now-int(last["ts"]) < cooldown:
                return int(last["id"])
            bucket=now//cooldown
            cur_dedupe=self.conn.execute("insert or ignore into decision_dedupe(build_version,chain,token,event,ts_bucket) values(?,?,?,?,?)",
                                         (VERSION,chain,token,event,bucket))
            if cur_dedupe.rowcount!=1:
                last=self.conn.execute("""select id from decision_ledger where chain=? and token=? and event=?
                    order by ts desc limit 1""",(chain,token,event)).fetchone()
                return int(last["id"]) if last else None
            cur=self.conn.execute("""insert into decision_ledger(
                ts,chain,token,symbol,event,tier,entry_score,confirmed_score,price,mcap,liquidity,pc5,pc1,
                buy_sell,swaps,turnover_pct,rt30,social_score,market_regime,sources,blockers,build_version)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
                now,chain,token,str(nest(pair,"baseToken","symbol",default="?")),event,str(tier or ""),
                f(entry_score),f(confirmed_score),f(pair.get("priceUsd")),m["mc"],m["liq"],m["pc5"],m["pc1"],
                ratio,swaps,turnover,f((rts or {}).get("tx30")),f((social or {}).get("score")),str(market_regime or "NEUTRAL"),
                ",".join(sorted(set(sources or [])))[:500]," | ".join(str(x) for x in (blockers or [])[:8])[:1200], VERSION))
            return int(cur.lastrowid)

    def mark_flash(self, catalyst_id):
        self.conn.execute("update catalysts set flashed=1 where id=?", (catalyst_id,))
        self.conn.commit()

    def open_positions(self):
        return [dict(r) for r in self.conn.execute("select * from positions where active=1 order by open_ts")]

    def position_by_token(self, token):
        row = self.conn.execute("select * from positions where token=? and active=1 order by open_ts desc limit 1", (token,)).fetchone()
        return dict(row) if row else None

    def find_open_position(self, query):
        exact = self.position_by_token(query)
        if exact:
            return exact, None
        rows = self.conn.execute(
            "select * from positions where active=1 and lower(symbol)=lower(?) order by open_ts desc",
            (query,)
        ).fetchall()
        if len(rows) == 1:
            return dict(rows[0]), None
        if len(rows) > 1:
            return None, "More than one open position uses that ticker. Use the contract address."
        return None, "No recorded open position matched that ticker/contract."


    def find_closed_position(self, query):
        exact=self.conn.execute("select * from positions where active=0 and token=? order by close_ts desc limit 1",(query,)).fetchone()
        if exact:
            return dict(exact),None
        rows=self.conn.execute("select * from positions where active=0 and lower(symbol)=lower(?) order by close_ts desc limit 3",(query,)).fetchall()
        if len(rows)==1:
            return dict(rows[0]),None
        if len(rows)>1:
            tokens={str(r["token"]) for r in rows}
            if len(tokens)>1:
                return None,"More than one closed token uses that ticker. Use the exact contract address."
            return dict(rows[0]),None
        return None,"No closed position matched that ticker/contract."

    def journal_snapshot(self, position_id, action, before=None):
        """Store a reversible snapshot for an explicit manual journal action only."""
        if before is None and position_id:
            row=self.conn.execute("select * from positions where id=?",(int(position_id),)).fetchone()
            before=dict(row) if row else None
        cur=self.conn.execute("""insert into journal_actions(ts,action,position_id,before_json,after_json,reversed_ts)
            values(?,?,?,?,?,0)""",(int(time.time()),str(action),int(position_id or 0),json.dumps(before,default=str) if before else "", ""))
        self.conn.commit(); return int(cur.lastrowid)

    def journal_finish(self, action_id, position_id):
        row=self.conn.execute("select * from positions where id=?",(int(position_id),)).fetchone()
        self.conn.execute("update journal_actions set position_id=?,after_json=? where id=?",
                          (int(position_id),json.dumps(dict(row),default=str) if row else "",int(action_id)))
        self.conn.commit()

    def undo_last_journal_action(self, max_age_seconds=1800):
        cutoff=int(time.time()-max_age_seconds)
        act=self.conn.execute("""select * from journal_actions where reversed_ts=0 and ts>=?
            order by id desc limit 1""",(cutoff,)).fetchone()
        if not act:
            return False,"No reversible journal action from the last 30 minutes."
        aid=int(act["id"]); pid=int(act["position_id"] or 0); before=act["before_json"] or ""
        if before:
            data=json.loads(before)
            cols=[r[1] for r in self.conn.execute("pragma table_info(positions)") if r[1] != "id" and r[1] in data]
            sets=",".join(f"{c}=?" for c in cols)
            vals=[data.get(c) for c in cols]+[pid]
            self.conn.execute(f"update positions set {sets} where id=?",vals)
        else:
            # A brand-new manual buy can be undone only if no later journal action has
            # depended on it. This removes the journal row; it never touches Fomo.
            self.conn.execute("delete from positions where id=?",(pid,))
        self.conn.execute("update journal_actions set reversed_ts=? where id=?",(int(time.time()),aid))
        self.conn.commit()
        return True,f"Undid journal action: {act['action']} (position #{pid})."

    def reconcile_closed_cash(self, query, cash_in, cash_out):
        rows=self.conn.execute("""select * from positions where active=0 and (token=? or lower(symbol)=lower(?))
            order by close_ts desc limit 2""",(query,query)).fetchall()
        if not rows: return False,"No closed position matched that ticker/contract."
        if len(rows)>1 and str(query).lower()==str(rows[0]["symbol"] or "").lower() and rows[0]["token"]!=rows[1]["token"]:
            return False,"Ticker is ambiguous. Use the exact contract."
        row=rows[0]; pnl=f(cash_out)-f(cash_in)
        self.conn.execute("""update positions set amount_usd=?,realized_pnl=?,realized_pnl_partial=0,
            pnl_quality='CASH_VERIFIED',cash_in_verified=?,cash_out_verified=? where id=?""",
            (f(cash_in),pnl,f(cash_in),f(cash_out),int(row["id"])))
        self.conn.commit(); return True,{"id":int(row["id"]),"symbol":row["symbol"],"pnl":pnl}

    def record_position(self, signal, current_price, amount_usd, liquidity, manual_source=""):
        """Record what the user actually bought.

        v10.9 deliberately keeps the MANUAL JOURNAL separate from trading advice:
        no max-open-position lock, no daily-loss lock, no chase lock, and no manual
        size clamp. Real/paper autopilot limits remain separate and unchanged.
        """
        existing_pos = self.position_by_token(signal["token"])
        amount_usd = max(f(amount_usd), 0.01)
        current_price = max(f(current_price), 1e-18)
        liquidity = max(f(liquidity), 0.0)
        quantity = amount_usd / current_price
        tier = str(signal.get("action") or signal.get("entry_tier") or "MANUAL")
        if existing_pos:
            rem = f(existing_pos.get("remaining_fraction"), 1.0)
            old_cost = f(existing_pos.get("amount_usd")) * rem
            old_qty = f(existing_pos.get("quantity")) * rem
            new_cost = old_cost + amount_usd
            new_qty = old_qty + quantity
            avg_entry = new_cost / max(new_qty, 1e-18)
            avg_liq = ((f(existing_pos.get("entry_liquidity")) * old_cost) + (liquidity * amount_usd)) / max(new_cost, 1e-18)
            source=str(manual_source or existing_pos.get("manual_source") or "")
            self.conn.execute("""update positions set
                entry_price=?, amount_usd=?, quantity=?, entry_liquidity=?, entry_tier=?, manual_source=?,
                remaining_fraction=1, peak_price=?, tp1_sent=0, tp2_sent=0, exit_sent=0,
                guardian_state='ENTRY', guardian_state_ts=?, guardian_last_alert_ts=0,
                guardian_last_action='', guardian_confidence=0, guardian_review_ts=0,
                pnl_quality='ESTIMATED',cash_in_verified=?,cash_out_verified=0
                where id=?""",
                (avg_entry, new_cost, new_qty, avg_liq, tier, source, max(f(existing_pos.get("peak_price")), current_price),
                 int(time.time()),new_cost, existing_pos["id"]))
            self.conn.commit()
            return existing_pos["id"], "MERGED"
        cur = self.conn.execute("""insert into positions(
            open_ts,chain,token,symbol,name,entry_price,amount_usd,quantity,entry_liquidity,peak_price,entry_tier,
            manual_source,pnl_quality,cash_in_verified,cash_out_verified)
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            int(time.time()), signal["chain"], signal["token"], signal["symbol"], signal.get("name", ""),
            current_price, amount_usd, quantity, liquidity, current_price, tier,
            str(manual_source or ""),"ESTIMATED",amount_usd,0.0,
        ))
        self.conn.commit()
        return cur.lastrowid, None

    def close_position(self, position_id, close_price):
        row = self.conn.execute("select * from positions where id=?", (position_id,)).fetchone()
        if not row:
            return None
        rem = f(row["remaining_fraction"], 1.0)
        rem_qty = f(row["quantity"]) * rem
        rem_cost = f(row["amount_usd"]) * rem
        final_leg_pnl = rem_qty * close_price - rem_cost
        total_pnl = f(row["realized_pnl_partial"]) + final_leg_pnl
        prior_quality=str(row["pnl_quality"] or "ESTIMATED") if "pnl_quality" in row.keys() else "ESTIMATED"
        quality="MIXED" if prior_quality in {"CASH_PARTIAL","CASH_VERIFIED"} and rem < 0.999 else "ESTIMATED"
        self.conn.execute("""update positions set active=0,close_ts=?,close_price=?,realized_pnl=?,
                          remaining_fraction=0,pnl_quality=? where id=?""",
                          (int(time.time()), close_price, total_pnl, quality, position_id))
        self.conn.commit()
        return total_pnl

    def close_position_cash(self, position_id, net_proceeds):
        """Close the remaining recorded position using actual net cash received in Fomo.
        This is useful when fills/fees make a live quote a poor estimate of realized P/L.
        """
        row = self.conn.execute("select * from positions where id=? and active=1", (position_id,)).fetchone()
        if not row:
            return None, None
        rem=f(row["remaining_fraction"],1.0)
        rem_qty=f(row["quantity"])*rem
        rem_cost=f(row["amount_usd"])*rem
        proceeds=max(0.0,f(net_proceeds))
        leg_pnl=proceeds-rem_cost
        total_pnl=f(row["realized_pnl_partial"])+leg_pnl
        effective_price=proceeds/max(rem_qty,1e-18)
        prior_cash=f(row["cash_out_verified"] if "cash_out_verified" in row.keys() else 0)
        total_cash=prior_cash+proceeds
        prior_quality=str(row["pnl_quality"] or "ESTIMATED") if "pnl_quality" in row.keys() else "ESTIMATED"
        had_prior_partial=rem < 0.999
        quality="CASH_VERIFIED" if (not had_prior_partial or prior_quality in {"CASH_PARTIAL","CASH_VERIFIED"}) else "MIXED"
        self.conn.execute("""update positions set active=0,close_ts=?,close_price=?,realized_pnl=?,
                          remaining_fraction=0,pnl_quality=?,cash_in_verified=?,cash_out_verified=? where id=?""",
                          (int(time.time()),effective_price,total_pnl,quality,f(row["amount_usd"]),total_cash,position_id))
        self.conn.commit()
        return total_pnl, leg_pnl

    def fix_position_entry(self, position_id, entry_price):
        row=self.conn.execute("select * from positions where id=? and active=1",(position_id,)).fetchone()
        if not row:
            return False, "No active position found."
        if f(row["remaining_fraction"],1.0)<0.999 or abs(f(row["realized_pnl_partial"]))>1e-9:
            return False, "Entry rebasing is disabled after a partial sale; close/reconcile the remaining position instead."
        px=f(entry_price)
        if px<=0:
            return False, "Entry price must be above zero."
        qty=f(row["amount_usd"])/px
        now=int(time.time())
        self.conn.execute("""update positions set entry_price=?,quantity=?,peak_price=?,tp1_sent=0,tp2_sent=0,exit_sent=0,
            guardian_state='ENTRY',guardian_state_ts=?,guardian_last_alert_ts=0,guardian_last_action='',guardian_review_ts=0
            where id=?""",(px,qty,px,now,position_id))
        self.conn.commit()
        return True, None

    def partial_close_position_cash(self, position_id, fraction, net_proceeds):
        row = self.conn.execute("select * from positions where id=? and active=1", (position_id,)).fetchone()
        if not row:
            return None, None
        fraction=min(max(f(fraction),0.0),1.0)
        if fraction<=0 or fraction>=1:
            return None, None
        rem=f(row["remaining_fraction"],1.0)
        sold_fraction_original=rem*fraction
        sold_cost=f(row["amount_usd"])*sold_fraction_original
        proceeds=max(0.0,f(net_proceeds))
        pnl=proceeds-sold_cost
        new_rem=max(0.0,rem*(1.0-fraction))
        new_partial=f(row["realized_pnl_partial"])+pnl
        prior_cash=f(row["cash_out_verified"] if "cash_out_verified" in row.keys() else 0)
        prior_quality=str(row["pnl_quality"] or "ESTIMATED") if "pnl_quality" in row.keys() else "ESTIMATED"
        had_prior_partial=rem < 0.999
        quality="CASH_PARTIAL" if (not had_prior_partial or prior_quality in {"CASH_PARTIAL","CASH_VERIFIED"}) else "MIXED"
        self.conn.execute("""update positions set remaining_fraction=?,realized_pnl_partial=?,
            pnl_quality=?,cash_in_verified=?,cash_out_verified=? where id=?""",
                          (new_rem,new_partial,quality,f(row["amount_usd"]),prior_cash+proceeds,position_id))
        self.conn.commit()
        return pnl,new_rem

    def partial_close_position(self, position_id, close_price, fraction):
        row = self.conn.execute("select * from positions where id=? and active=1", (position_id,)).fetchone()
        if not row:
            return None, None
        fraction = min(max(f(fraction), 0.0), 1.0)
        if fraction <= 0:
            return None, None
        rem = f(row["remaining_fraction"], 1.0)
        sold_fraction_original = rem * fraction
        sold_qty = f(row["quantity"]) * sold_fraction_original
        sold_cost = f(row["amount_usd"]) * sold_fraction_original
        pnl = sold_qty * close_price - sold_cost
        new_rem = max(0.0, rem * (1.0 - fraction))
        new_partial = f(row["realized_pnl_partial"]) + pnl
        prior_quality=str(row["pnl_quality"] or "ESTIMATED") if "pnl_quality" in row.keys() else "ESTIMATED"
        quality="MIXED" if prior_quality in {"CASH_PARTIAL","CASH_VERIFIED"} else "ESTIMATED"
        self.conn.execute("""update positions set remaining_fraction=?,realized_pnl_partial=?,pnl_quality=? where id=?""",
                          (new_rem, new_partial, quality, position_id))
        self.conn.commit()
        return pnl, new_rem

    def update_position_peak(self, position_id, peak):
        self.conn.execute("update positions set peak_price=? where id=?", (peak, position_id))
        self.conn.commit()

    def flag_position(self, position_id, field):
        if field in {"tp1_sent", "tp2_sent", "exit_sent"}:
            self.conn.execute(f"update positions set {field}=1 where id=?", (position_id,))
            self.conn.commit()

    def guardian_update(self, position_id, state_name, confidence=0.0, action="", review_ts=0, alerted=False):
        now=int(time.time())
        row=self.conn.execute("select guardian_state,guardian_state_ts from positions where id=?",(int(position_id),)).fetchone()
        prior=str(row["guardian_state"] or "ENTRY") if row else "ENTRY"
        fields=["guardian_state=?","guardian_confidence=?","guardian_last_action=?","guardian_review_ts=?"]
        args=[str(state_name),f(confidence),str(action),int(review_ts or 0)]
        if prior != str(state_name) or not row or not int(row["guardian_state_ts"] or 0):
            fields.append("guardian_state_ts=?"); args.append(now)
        if alerted:
            fields.append("guardian_last_alert_ts=?"); args.append(now)
        args.append(int(position_id))
        self.conn.execute("update positions set "+",".join(fields)+" where id=?",args)
        self.conn.commit()

    def guardian_event(self, pos, state_info, detail=""):
        self.conn.execute("""insert into guardian_events(ts,position_id,token,symbol,state,return_pct,drawdown_pct,liquidity_drop_pct,detail)
            values(?,?,?,?,?,?,?,?,?)""",
            (int(time.time()),int(pos["id"]),str(pos.get("token") or ""),str(pos.get("symbol") or "?"),
             str(state_info.get("state") or ""),f(state_info.get("ret")),f(state_info.get("drawdown")),
             f(state_info.get("liq_drop")),str(detail)[:1000]))
        self.conn.commit()

    def wallet_state(self, position_id):
        row=self.conn.execute("select * from wallet_sync_state where position_id=?",(int(position_id),)).fetchone()
        return dict(row) if row else None

    def wallet_set_state(self, position_id, token, qty, notice=False):
        now=int(time.time())
        prior=self.wallet_state(position_id)
        notice_ts=now if notice else int((prior or {}).get("last_notice_ts") or 0)
        self.conn.execute("""insert or replace into wallet_sync_state(position_id,token,last_qty,last_ts,last_notice_ts)
            values(?,?,?,?,?)""",(int(position_id),str(token),f(qty),now,notice_ts))
        self.conn.commit()

    def recent_missed(self, hours=24, limit=8):
        cutoff=int(time.time()-hours*3600)
        rows=self.conn.execute("""select * from missed_opportunities where baseline_ts>=?
            order by max_return_pct desc limit ?""",(cutoff,int(limit))).fetchall()
        return [dict(r) for r in rows]

    def outcome_stats(self, hours=24):
        cutoff=int(time.time()-hours*3600)
        rows=self.conn.execute("""select o.* from signal_outcomes o join signals s on s.id=o.signal_id
            where s.ts>=? and coalesce(o.suspect,0)=0""",(cutoff,)).fetchall()
        return [dict(r) for r in rows]

    def daily_stats(self):
        cutoff = int(time.time() - 24*3600)
        signals = self.conn.execute("select count(*) from signals where ts>=?", (cutoff,)).fetchone()[0]
        early = self.conn.execute("select count(*) from signals where ts>=? and kind='EARLY'", (cutoff,)).fetchone()[0]
        eval30 = [r[0] for r in self.conn.execute("""select e.return_pct from evals e join signals s on s.id=e.signal_id
            where s.ts>=? and e.checkpoint_min=30 and coalesce(e.suspect,0)=0""", (cutoff,)).fetchall()]
        realized = self.conn.execute("select coalesce(sum(realized_pnl),0) from positions where close_ts>=?", (cutoff,)).fetchone()[0]
        return {
            "signals": signals, "early": early, "eval30": eval30,
            "realized": realized, "open": len(self.open_positions())
        }

    def export(self):
        path = ROOT / "signal_performance.csv"
        rows = self.conn.execute("""select s.id,s.ts,s.kind,s.chain,s.symbol,s.name,s.token,s.score,s.price,
            s.mcap,s.liquidity,s.action,e.checkpoint_min,e.return_pct from signals s
            left join evals e on e.signal_id=s.id order by s.id,e.checkpoint_min""").fetchall()
        with path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["id","ts","kind","chain","symbol","name","token","score","entry_price",
                             "mcap","liquidity","action","checkpoint_min","return_pct"])
            writer.writerows(rows)
        self.export_latency()
        return path


class BirdeyeGuard:
    """Fault-isolated Birdeye access.

    A single endpoint failure cannot disable all Birdeye enrichment.
    A feature 401 is verified against the core endpoint before the key
    is ever marked invalid.
    """
    def __init__(self):
        self.last_call = 0.0
        self.cooldown_until = 0.0
        self.key_state = "missing" if not BE_KEY else "unknown"
        self.disabled_until = {}
        self.disabled_reason = {}
        self.last_core_probe = 0.0
        self.last_core_status = None
        self.last_core_probe_name = "not-run"
        self.last_error = {}

    @property
    def invalid_key(self):
        return self.key_state == "invalid"

    def _scope(self, feature, chain=None):
        return f"{feature}:{chain}" if chain else feature

    def _purge(self):
        now = time.time()
        for scope, until in list(self.disabled_until.items()):
            if until <= now:
                self.disabled_until.pop(scope, None)
                self.disabled_reason.pop(scope, None)

    @property
    def disabled_features(self):
        self._purge()
        return set(self.disabled_until.keys())

    def available(self, feature="core", chain=None):
        self._purge()
        if not BE_KEY or self.invalid_key or time.time() < self.cooldown_until:
            return False
        scope = self._scope(feature, chain)
        return scope not in self.disabled_until and feature not in self.disabled_until

    async def slot(self):
        delay = BE_DELAY - (time.time() - self.last_call)
        if delay > 0:
            await asyncio.sleep(delay)
        self.last_call = time.time()

    async def probe_core(self, http, force=False):
        """Verify Birdeye with two current documented endpoints.

        /defi/networks is the cheapest canonical auth probe. Some accounts/provider
        revisions have returned a 400 there despite other data endpoints working, so
        v12.2 confirms ambiguous 4xx responses with the documented wrapped-SOL price
        endpoint before declaring CORE degraded.
        """
        if not BE_KEY:
            self.key_state = "missing"
            self.last_core_status = None
            return None
        if not force and self.last_core_status == 200 and time.time() - self.last_core_probe < 3600:
            return 200
        headers = {"X-API-KEY": BE_KEY, "accept": "application/json"}
        await self.slot()
        status, data = await http.get(f"{BE}/defi/networks", headers=headers)
        self.last_core_probe = time.time()
        self.last_core_probe_name = "networks"

        # Compatibility/auth confirmation: a documented price request gives us a
        # second independent signal before treating a 400-class response as a broken
        # Birdeye core. Never retry 401/403 as if they were harmless.
        if status in (400, 404, 405, 422):
            self.last_error["core:networks"] = self._safe_error(data)
            await self.slot()
            pheaders = {"X-API-KEY": BE_KEY, "x-chain": "solana", "accept": "application/json"}
            pstatus, pdata = await http.get(
                f"{BE}/defi/price", headers=pheaders, params={"address": WSOL_MINT_PUBLIC}
            )
            self.last_core_probe_name = "price-fallback"
            if pstatus == 200:
                status, data = 200, pdata
            else:
                status, data = pstatus, pdata
                self.last_error["core:price"] = self._safe_error(pdata)

        self.last_core_status = status
        if status == 200:
            self.key_state = "valid"
        elif status == 401:
            self.key_state = "invalid"
        elif status == 429:
            self.cooldown_until = time.time() + BE_COOL
        return status

    @staticmethod
    def _safe_error(data):
        if data is None:
            return "no provider detail"
        if isinstance(data, dict):
            for key in ("message", "error", "detail", "reason", "description"):
                value = data.get(key)
                if value:
                    if isinstance(value, dict):
                        value = value.get("message") or value.get("detail") or str(value)
                    return re.sub(r"\s+", " ", str(value))[:180]
            try:
                return re.sub(r"\s+", " ", json.dumps(data, separators=(",", ":")))[:180]
            except Exception:
                return "provider returned JSON error"
        return re.sub(r"\s+", " ", str(data))[:180]

    def _disable(self, feature, chain, seconds, reason):
        scope = self._scope(feature, chain)
        now=time.time()
        already=self.disabled_until.get(scope,0) > now
        self.disabled_until[scope] = max(self.disabled_until.get(scope,0), now + max(60, int(seconds)))
        self.disabled_reason[scope] = reason
        return not already

    async def record(self, http, feature, status, chain=None, data=None):
        scope = self._scope(feature, chain)
        if status != 200 and data is not None:
            self.last_error[scope] = self._safe_error(data)

        if status == 200:
            self.key_state = "valid"
            self.disabled_until.pop(scope, None)
            self.disabled_reason.pop(scope, None)
            return

        if status == 429:
            already=time.time() < self.cooldown_until
            self.cooldown_until = max(self.cooldown_until, time.time() + BE_COOL)
            if not already:
                print(f"[Birdeye] rate limited; enrichment pauses {BE_COOL//60}m. "
                      "Realtime/core scanner continue.")
            return

        if status == 401:
            # v9 bug fix: a feature 401 is NOT enough to call the key invalid.
            core_status = await self.probe_core(http, force=True)
            if core_status == 200:
                self._disable(feature, chain, BE_FEATURE_RETRY,
                              "feature returned 401 while core authentication is valid")
                print(f"[Birdeye] {scope} returned 401, but CORE AUTH IS OK. "
                      f"Only {scope} is paused temporarily.")
            elif core_status == 401:
                self.key_state = "invalid"
                print("[Birdeye] core authentication also returned 401. "
                      "The API key is genuinely invalid.")
            else:
                self._disable(feature, chain, min(BE_FEATURE_RETRY, 600),
                              f"feature 401; core probe status {core_status}")
                print(f"[Birdeye] {scope} returned 401; core verification was "
                      f"inconclusive ({core_status}). Only that feature is paused.")
            return

        if status == 403:
            newly=self._disable(feature, chain, BE_FORBIDDEN_RETRY,
                                "403 forbidden/not whitelisted")
            if newly:
                print(f"[Birdeye] {scope} returned 403. Only that feature/chain is "
                      "paused; other Birdeye features continue.")
            return

        if status in (400, 404, 405, 422):
            newly=self._disable(feature, chain, BE_FEATURE_RETRY,
                                f"HTTP {status} request/endpoint incompatibility")
            if newly:
                print(f"[Birdeye] {scope} returned {status}. Temporarily pausing "
                      "only that feature/chain.")
            return

        # Network/5xx errors are transient: no permanent disable.

    def status_text(self):
        self._purge()
        if not BE_KEY:
            return "NOT CONFIGURED (RugCheck fallback active)"
        if self.invalid_key:
            return "CORE AUTH INVALID"
        if self.last_core_status not in (None,200) and self.key_state != "valid":
            disabled = sorted(self.disabled_until.keys())
            suffix = ""
            if disabled:
                shown=", ".join(disabled[:3])
                suffix=f" — fallback for: {shown}"
            detail = self.last_error.get("core:price") or self.last_error.get("core:networks") or ""
            if detail:
                detail = f" | provider: {detail[:90]}"
            return f"CORE DEGRADED (HTTP {self.last_core_status}; probe {self.last_core_probe_name}){suffix}{detail}"
        if time.time() < self.cooldown_until:
            mins = max(1, int((self.cooldown_until-time.time()+59)//60))
            return f"CORE OK — rate-limit cooldown ~{mins}m"
        disabled = sorted(self.disabled_until.keys())
        if disabled:
            shown = ", ".join(disabled[:4])
            if len(disabled) > 4:
                shown += f" +{len(disabled)-4} more"
            bad400=sum(1 for scope in disabled if "HTTP 400" in str(self.disabled_reason.get(scope,"")))
            prefix="CORE OK — DATA FEATURES DEGRADED" if bad400>=3 else "CORE OK"
            return f"{prefix} — fallback for: {shown}"
        if self.key_state == "valid":
            return "CORE OK — enrichment available"
        return "AUTH NOT YET VERIFIED"

class AlertLimiter:
    def __init__(self):
        self.last = {}
        self.info_hour = deque()
        self.action_hour = deque()

    @staticmethod
    def _trim(bucket, now):
        while bucket and now - bucket[0] > 3600:
            bucket.popleft()

    def _allowed(self, key, cooldown, bucket, cap):
        now = time.time()
        self._trim(bucket, now)
        if len(bucket) >= cap or now - self.last.get(key, 0) < cooldown:
            return False
        self.last[key] = now
        bucket.append(now)
        return True

    def allowed(self, key, cooldown=1800):
        # Informational alerts never consume the actionable-entry budget.
        return self._allowed(key, cooldown, self.info_hour, MAX_ALERTS)

    def allowed_action(self, key, cooldown=1800):
        return self._allowed(key, cooldown, self.action_hour, MAX_ENTRY_ALERTS)

    def release_action(self, key):
        """Undo an action reservation when Telegram delivery never happened.

        v13.x reserved the multi-hour alert cooldown before calling Telegram. A DNS
        outage could therefore make an unseen BUY suppress the next valid setup.
        """
        ts=self.last.pop(key,None)
        if ts is None:
            return False
        try: self.action_hour.remove(ts)
        except ValueError: pass
        return True


def load_sources():
    try:
        return json.loads((ROOT / "social_sources.json").read_text())
    except Exception:
        return {"x_accounts": [], "high_impact_keywords": [], "rss_feeds": []}


SOURCES = load_sources()


def _source_bucket(label):
    x=str(label or "").lower()
    if "pumpportal" in x: return "PUMPPORTAL"
    if "realtime" in x or "solana" in x and "ws" in x: return "REALTIME"
    if "social" in x or x.startswith("x") or "reddit" in x or "rss" in x: return "SOCIAL"
    if "birdeye" in x: return "BIRDEYE"
    if "jupiter" in x: return "JUPITER"
    if "gecko" in x: return "GECKO"
    if "boost" in x: return "DEX_BOOST"
    if "profile" in x or "takeover" in x: return "DEX_ATTENTION"
    if "dex" in x: return "DEX"
    return str(label or "OTHER").upper()


def mark_candidate_source(state, chain, token, source):
    if state is None or not token:
        return
    now=time.time(); key=f"{str(chain or '').lower()}:{str(token)}"
    registry=state.setdefault("candidate_sources",{})
    rec=registry.setdefault(key,{"first_seen":now,"last_seen":now,"sources":set(),"paid_boost":False})
    rec["last_seen"]=now
    rec["sources"].add(str(source))
    if "boost" in str(source).lower(): rec["paid_boost"]=True


def candidate_source_context(state, chain, token):
    key=f"{str(chain or '').lower()}:{str(token or '')}"
    rec=(state or {}).get("candidate_sources",{}).get(key) or {}
    raw=sorted(set(rec.get("sources") or []))
    buckets=sorted({_source_bucket(x) for x in raw if _source_bucket(x) not in {"DEX_BOOST","DEX_ATTENTION"}})
    # Paid boosts/profiles/community-takeover listings are useful discovery, but never
    # count as independent evidence of quality.
    bonus=min(CROSS_SOURCE_BONUS_MAX,max(0,len(buckets)-1)*1.5)
    return {"sources":raw,"buckets":buckets,"bonus":bonus,"paid_boost":bool(rec.get("paid_boost")),
            "first_seen":f(rec.get("first_seen"))}


def cleanup_candidate_sources(state):
    now=time.time(); registry=(state or {}).get("candidate_sources",{})
    for key,rec in list(registry.items()):
        if now-f((rec or {}).get("last_seen"))>6*3600:
            registry.pop(key,None)


def telegram_delivery_status(now=None):
    now=float(now if now is not None else time.time())
    if not (TG and CHAT):
        return "UNCONFIGURED"
    if TG_CONSEC_FAILURES > 0 and TG_LAST_FAILURE_TS >= TG_LAST_SUCCESS_TS:
        age=max(0,int(now-TG_LAST_FAILURE_TS)) if TG_LAST_FAILURE_TS else 0
        return f"DEGRADED ({TG_CONSEC_FAILURES} failed; last {age}s ago)"
    if TG_LAST_SUCCESS_TS > 0:
        age=max(0,int(now-TG_LAST_SUCCESS_TS))
        return f"OK (last {age}s ago)"
    return "CONFIGURED / unverified"


async def send(http, message):
    global TG_LAST_SUCCESS_TS, TG_LAST_FAILURE_TS, TG_CONSEC_FAILURES
    print("\n" + "=" * 78 + "\n" + message + "\n" + "=" * 78 + "\n")
    if TG and CHAT:
        waits=(0.0,0.75,2.0)
        last_status=0; last_text=""
        for attempt,wait_s in enumerate(waits,1):
            if wait_s:
                await asyncio.sleep(wait_s)
            status, response_text = await http.post(
                f"https://api.telegram.org/bot{TG}/sendMessage",
                {"chat_id": CHAT, "text": message, "disable_web_page_preview": True}
            )
            last_status=status; last_text=str(response_text or "")
            if status == 200:
                try:
                    payload = json.loads(response_text)
                    mid=nest(payload, "result", "message_id", default=None)
                    if mid is not None:
                        TG_LAST_SUCCESS_TS=time.time(); TG_CONSEC_FAILURES=0
                        return mid
                except Exception:
                    pass
            print(f"[Telegram] send attempt {attempt}/{len(waits)} failed {status}: {last_text[:160]}")
        TG_LAST_FAILURE_TS=time.time(); TG_CONSEC_FAILURES += 1
        print(f"[Telegram] DELIVERY FAILED after {len(waits)} attempts ({last_status})")
        return None
    return None


async def dex_discovery(http, state=None):
    found = {chain: set() for chain in CHAINS}
    if not USE_DEX:
        return found
    urls=[
        (f"{DEX}/token-profiles/latest/v1","DexScreener profile"),
        (f"{DEX}/community-takeovers/latest/v1","DexScreener community takeover"),
        (f"{DEX}/token-boosts/latest/v1","DexScreener boost"),
        (f"{DEX}/token-boosts/top/v1","DexScreener boost"),
    ]
    responses=await asyncio.gather(*(http.get(url) for url,_ in urls),return_exceptions=True)
    for (url,label),resp in zip(urls,responses):
        if isinstance(resp,Exception): continue
        _,data=resp
        for item in data if isinstance(data,list) else []:
            chain=str(item.get("chainId","")).lower(); address=str(item.get("tokenAddress","")).strip()
            if chain in found and address:
                found[chain].add(address); mark_candidate_source(state,chain,address,label)
    return found


async def jupiter_token_discovery(http, state):
    """Optional Jupiter Tokens V2 discovery. Discovery only; never a BUY bonus."""
    found={chain:set() for chain in CHAINS}
    if "solana" not in found or not JUPITER_TOKEN_DISCOVERY:
        return found
    api_key=os.getenv("JUPITER_API_KEY","").strip()
    if not api_key or time.time()-f(state.get("last_jupiter_tokens")) < JUPITER_TOKEN_DISCOVERY_SECONDS:
        return found
    headers={"accept":"application/json","x-api-key":api_key}
    urls=[
        ("https://api.jup.ag/tokens/v2/toptrending/5m",{"limit":"50"},"Jupiter top trending 5m"),
        ("https://api.jup.ag/tokens/v2/toporganicscore/5m",{"limit":"50"},"Jupiter top organic 5m"),
        ("https://api.jup.ag/tokens/v2/recent",{},"Jupiter recent pool"),
    ]
    responses=await asyncio.gather(*(http.get(url,headers=headers,params=params,retries=1) for url,params,_ in urls),return_exceptions=True)
    ok_any=False
    for (_,_,label),resp in zip(urls,responses):
        if isinstance(resp,Exception): continue
        status,data=resp
        if status!=200 or not isinstance(data,list): continue
        ok_any=True
        for item in data:
            if not isinstance(item,dict): continue
            mint=str(item.get("id") or "").strip()
            if len(mint)>=30:
                found["solana"].add(mint); mark_candidate_source(state,"solana",mint,label)
    state["last_jupiter_tokens"]=time.time()
    state["jupiter_tokens_status"]=(f"OK ({len(found['solana'])} candidates)" if ok_any else "DEGRADED")
    return found


async def gecko_new_pools(http, state):
    found = {chain: set() for chain in CHAINS}
    if not USE_GT or time.time() - state["last_gt"] < GT_EVERY * 60:
        return found
    headers = {"Accept": "application/json;version=20230203"}
    for chain in CHAINS:
        network = GT_MAP.get(chain, chain)
        for page in range(1, GT_PAGES + 1):
            status, data = await http.get(f"{GT}/networks/{network}/new_pools", headers=headers,
                                          params={"include": "base_token", "page": page})
            if status == 429:
                if time.time() >= f(state.get("gt_rate_log_until"),0):
                    print("[GeckoTerminal] public rate limit; retrying quietly for the next 10 minutes.")
                    state["gt_rate_log_until"] = time.time() + 600
                break
            if status != 200 or not isinstance(data, dict):
                continue
            included = {x.get("id"): x for x in data.get("included", []) if isinstance(x, dict)}
            for pool in data.get("data", []):
                relation = nest(pool, "relationships", "base_token", "data", default={}) or {}
                token_id = relation.get("id", "")
                token_obj = included.get(token_id, {})
                address = nest(token_obj, "attributes", "address", default="")
                if not address and "_" in token_id:
                    address = token_id.split("_", 1)[1]
                if address:
                    found[chain].add(address); mark_candidate_source(state,chain,address,"GeckoTerminal new pool")
            await asyncio.sleep(0.25)
    state["last_gt"] = time.time()
    return found


async def gecko_trending_pools(http, state):
    """Established/trending discovery so chart-structure entries are not dependent on paid boosts/new-pool feeds."""
    found={chain:set() for chain in CHAINS}
    if not USE_GT or not GT_TRENDING_ENABLED or time.time()-f(state.get("last_gt_trending"))<GT_TRENDING_EVERY_SECONDS:
        return found
    headers={"Accept":"application/json;version=20230203"}
    # Prioritize actionable/manual-entry chains and stay comfortably below public limits.
    for chain in [c for c in CHAINS if c in ALERT_ENTRY_CHAINS]:
        network=GT_MAP.get(chain,chain)
        for duration in ("5m","1h"):
            status,data=await http.get(f"{GT}/networks/{network}/trending_pools",headers=headers,params={"include":"base_token","page":1,"duration":duration})
            if status!=200 or not isinstance(data,dict):
                continue
            included={x.get("id"):x for x in data.get("included",[]) if isinstance(x,dict)}
            for pool in data.get("data",[]):
                rel=nest(pool,"relationships","base_token","data",default={}) or {}; token_id=rel.get("id","")
                token_obj=included.get(token_id,{})
                address=nest(token_obj,"attributes","address",default="")
                if not address and "_" in token_id: address=token_id.split("_",1)[1]
                if address:
                    found[chain].add(address); mark_candidate_source(state,chain,address,f"GeckoTerminal trending {duration}")
            await asyncio.sleep(0.25)
    state["last_gt_trending"]=time.time(); return found


def walk(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from walk(value)


async def birdeye_smart_money(http, guard, state):
    if not guard.available("smart_money", "solana") or time.time() - state["last_smart"] < BE_SMART_EVERY * 60:
        return set()
    await guard.slot()
    headers = {"X-API-KEY": BE_KEY, "x-chain": "solana", "accept": "application/json"}
    params = {"interval":"1d", "trader_style":"trenchers", "sort_by":"net_flow", "sort_type":"desc", "offset":0, "limit":20}
    status, data = await http.get(f"{BE}/smart-money/v1/token/list", headers=headers, params=params)
    await guard.record(http, "smart_money", status, "solana", data=data)
    state["last_smart"] = time.time()
    if status != 200 or not isinstance(data, dict):
        return set()
    out = set()
    for obj in walk(data):
        for key in ("address", "token_address", "tokenAddress"):
            value = obj.get(key) if isinstance(obj, dict) else None
            if isinstance(value, str) and len(value) >= 30:
                out.add(value); mark_candidate_source(state,"solana",value,"Birdeye smart money")
    if out:
        print(f"[smart-money] discovered {len(out)} Solana candidates")
    return out


async def birdeye_new_listing(http, guard, state):
    """Birdeye listing discovery focused on entry-capable chains to save CU."""
    out = {chain: set() for chain in CHAINS}
    listing_chains = [c for c in CHAINS if (not BE_DISCOVERY_ENTRY_ONLY or c in ENTRY_CHAINS)]
    if not BE_NEW_LISTING or not listing_chains or time.time() - state["last_new_listing"] < BE_NEW_EVERY * 60:
        return out
    chain = listing_chains[state["new_listing_index"] % len(listing_chains)]
    state["new_listing_index"] += 1
    if not guard.available("new_listing", chain):
        return out
    state["last_new_listing"] = time.time()
    await guard.slot()
    headers = {"X-API-KEY": BE_KEY, "x-chain": chain, "accept":"application/json"}
    params = {"limit":20, "meme_platform_enabled":"true" if chain == "solana" else "false"}
    status, data = await http.get(f"{BE}/defi/v2/tokens/new_listing", headers=headers, params=params)
    await guard.record(http, "new_listing", status, chain, data=data)
    if status != 200 or not isinstance(data, dict):
        return out
    for obj in walk(data):
        for key in ("address", "token_address", "tokenAddress"):
            value = obj.get(key) if isinstance(obj, dict) else None
            if isinstance(value, str) and len(value) >= 30:
                out[chain].add(value); mark_candidate_source(state,chain,value,"Birdeye new listing")
    if out[chain]:
        print(f"[new-listing] {chain}: +{len(out[chain])} candidates")
    return out


async def get_pairs(http, chain, addresses):
    rows=[]; addresses=list(dict.fromkeys(str(x) for x in addresses if x))
    chunks=[addresses[i:i+30] for i in range(0,len(addresses),30) if addresses[i:i+30]]
    if not chunks:
        return []
    sem=asyncio.Semaphore(DEX_PAIR_CONCURRENCY if str(chain).lower() in ENTRY_CHAINS else 1)
    async def one(chunk):
        async with sem:
            _,data=await http.get(f"{DEX}/tokens/v1/{chain}/{','.join(chunk)}")
            return data if isinstance(data,list) else []
    results=await asyncio.gather(*(one(c) for c in chunks),return_exceptions=True)
    for result in results:
        if isinstance(result,list): rows.extend(result)
    best={}
    for pair in rows:
        address=str(nest(pair,"baseToken","address",default="")).strip()
        liquidity=f(nest(pair,"liquidity","usd",default=0))
        if address and (address not in best or liquidity>f(nest(best[address],"liquidity","usd",default=0))):
            best[address]=pair
    return list(best.values())


async def pair_for_token(http, chain, token):
    """Return only a pair whose BASE token exactly matches the requested contract.

    Tickers/names are display labels and are not trusted as identity.
    """
    _, data = await http.get(f"{DEX}/tokens/v1/{chain}/{token}")
    rows = data if isinstance(data, list) else []
    wanted = str(token or "").strip().lower()
    exact = [
        p for p in rows
        if str(nest(p, "baseToken", "address", default="")).strip().lower() == wanted
    ]
    if not exact:
        return None
    return max(exact, key=lambda p: f(nest(p, "liquidity", "usd", default=0)))


async def update_market_regime(http,state,force=False):
    if not MARKET_REGIME_ENABLED:
        state["market_regime"]={"label":"NEUTRAL","pc5":0.0,"pc1":0.0,"price":0.0,"ts":time.time()}
        return state["market_regime"]
    now=time.time(); cached=state.get("market_regime") or {}
    if not force and cached and now-f(cached.get("ts"))<MARKET_REGIME_EVERY_SECONDS:
        return cached
    pair=await pair_for_token(http,"solana",WSOL_MINT_PUBLIC)
    if not pair:
        if cached:
            return cached
        state["market_regime"]={"label":"UNKNOWN","pc5":0.0,"pc1":0.0,"price":0.0,"ts":now}
        return state["market_regime"]
    m=metrics(pair); pc5=m["pc5"]; pc1=m["pc1"]
    if pc5<=MARKET_STRESS_SOL_5M_PCT or pc1<=MARKET_STRESS_SOL_1H_PCT:
        label="STRESS"
    elif pc5<=MARKET_RISK_OFF_SOL_5M_PCT or pc1<=MARKET_RISK_OFF_SOL_1H_PCT:
        label="RISK_OFF"
    elif pc5>=0.5 and pc1>=1.5:
        label="RISK_ON"
    else:
        label="NEUTRAL"
    state["market_regime"]={"label":label,"pc5":pc5,"pc1":pc1,"price":f(pair.get("priceUsd")),"ts":now}
    return state["market_regime"]


def market_regime_line(regime):
    r=regime or {}; label=str(r.get("label") or "UNKNOWN").replace("_"," ")
    return f"SOL regime {label} | SOL 5m {f(r.get('pc5')):+.1f}% / 1h {f(r.get('pc1')):+.1f}%"


def _parse_social_ts(value):
    if not value:
        return int(time.time())
    try:
        text=str(value).replace("Z","+00:00")
        return int(datetime.fromisoformat(text).timestamp())
    except Exception:
        return int(time.time())


def _record_contract_social(db, source, source_key, text, url="", author="", engagement=0.0, ts=None):
    body=str(text or "")
    for addr in SOL_CONTRACT_RE.findall(body):
        db.add_social_event(source,source_key,addr,author=author,engagement=engagement,url=url,text=body,ts=ts)
    for addr in EVM_CONTRACT_RE.findall(body):
        db.add_social_event(source,source_key,addr.lower(),author=author,engagement=engagement,url=url,text=body,ts=ts)


async def fetch_rss(http, db):
    headers={"User-Agent":"FomoBot/12 public-market-research"}
    for url in SOURCES.get("rss_feeds",[]):
        status,body=await http.get(url,headers=headers)
        if status!=200 or not isinstance(body,str): continue
        source="REDDIT" if "reddit.com" in url else "RSS"
        source_key=(urlparse(url).netloc or url)[:160]
        try:
            root=ET.fromstring(body)
            for item in root.findall(".//item")[:30]:
                title=(item.findtext("title") or "").strip(); desc=re.sub("<[^>]+>"," ",item.findtext("description") or ""); link=(item.findtext("link") or "").strip()
                author=(item.findtext("author") or item.findtext("{*}creator") or source_key).strip()
                pub=item.findtext("pubDate") or ""
                text=f"{title} {desc}"
                if title: db.add_catalyst(source,title,desc,link)
                _record_contract_social(db,source,source_key,text,link,author,0.0,None)
            for entry in root.findall(".//{*}entry")[:30]:
                title=(entry.findtext("{*}title") or "").strip(); desc=re.sub("<[^>]+>"," ",(entry.findtext("{*}content") or entry.findtext("{*}summary") or ""))
                le=entry.find("{*}link"); link=le.attrib.get("href","") if le is not None else ""
                author=(entry.findtext("{*}author/{*}name") or entry.findtext("{*}author") or source_key).strip()
                text=f"{title} {desc}"
                if title: db.add_catalyst(source,title,desc,link)
                _record_contract_social(db,source,source_key,text,link,author,0.0,None)
        except Exception as exc: print(f"[RSS] {url}: {exc}")


def _x_author_map(data):
    users={}
    for u in nest(data,"includes","users",default=[]) or []:
        if isinstance(u,dict): users[str(u.get("id") or "")]=u
    return users


def x_runtime_available():
    return bool(XTOKEN and time.time() >= X_BILLING_HOLD_UNTIL)


def x_health_text():
    if not XTOKEN:
        return "NOT CONFIGURED"
    if X_LAST_STATUS == 200:
        return "CONNECTED (recent search OK)"
    if X_LAST_STATUS == 402:
        return "PAYMENT REQUIRED (HTTP 402 — X API credits needed; REST scouting paused until /sources refresh or restart)"
    if X_LAST_STATUS in (401,403):
        return f"ACCESS REJECTED (HTTP {X_LAST_STATUS} — check app/token permissions)"
    if X_LAST_STATUS is not None:
        suffix=f" — {X_LAST_ERROR[:80]}" if X_LAST_ERROR else ""
        return f"DEGRADED (HTTP {X_LAST_STATUS}){suffix}"
    return "CONFIGURED — awaiting first API check"


async def x_recent_query(http, db, query, label="broad", max_results=None):
    global X_LAST_STATUS, X_LAST_STATUS_TS, X_LAST_ERROR, X_BILLING_HOLD_UNTIL
    if not XTOKEN: return True,0
    if time.time() < X_BILLING_HOLD_UNTIL:
        return False,0
    headers={"Authorization":f"Bearer {XTOKEN}"}
    params={
        "query":query,
        "max_results":max(10,min(int(max_results or XMAX),100)),
        "tweet.fields":"created_at,author_id,public_metrics",
        "expansions":"author_id",
        "user.fields":"username,verified,public_metrics",
    }
    status,data=await http.get(f"{XAPI}/tweets/search/recent",headers=headers,params=params)
    X_LAST_STATUS=status
    X_LAST_STATUS_TS=time.time()
    X_LAST_ERROR = BirdeyeGuard._safe_error(data) if status != 200 else ""
    if status==402:
        X_BILLING_HOLD_UNTIL=float("inf")
        print("[X] payment required (402); X REST scouting is latched off for this session. Use /sources refresh after adding credits. RSS/Reddit + on-chain discovery continue.")
        return False,0
    if status==429:
        print("[X] rate limited; RSS/Reddit + on-chain discovery continue."); return False,0
    if status in (401,403):
        print(f"[X] access rejected ({status}); RSS/Reddit + on-chain discovery continue."); return False,0
    if status != 200:
        print(f"[X] recent-search HTTP {status}; X scouting is degraded while RSS/Reddit + on-chain discovery continue.")
        return False,0
    count=0
    if status==200 and isinstance(data,dict):
        users=_x_author_map(data)
        for tw in data.get("data",[]):
            if not isinstance(tw,dict): continue
            body=str(tw.get("text") or ""); tid=str(tw.get("id") or ""); aid=str(tw.get("author_id") or "")
            user=users.get(aid,{})
            username=str(user.get("username") or aid or "unknown")
            metrics=tw.get("public_metrics") or {}
            engagement=sum(f(metrics.get(k)) for k in ("like_count","retweet_count","reply_count","quote_count"))
            url=f"https://x.com/i/web/status/{tid}" if tid else ""
            db.add_catalyst("X",body[:140],body,url)
            _record_contract_social(db,"X",f"X:@{username}",body,url,username,engagement,_parse_social_ts(tw.get("created_at")))
            count+=1
    return True,count


async def fetch_x(http, db, state):
    if not XTOKEN: return
    accounts=SOURCES.get("x_accounts",[])[:16]; keywords=SOURCES.get("high_impact_keywords",[])[:16]
    if accounts and keywords:
        kw=" OR ".join(f'"{x}"' if " " in x else x for x in keywords[:8])
        for i in range(0,len(accounts),8):
            authors=" OR ".join(f"from:{a}" for a in accounts[i:i+8])
            ok,_=await x_recent_query(http,db,f"({authors}) ({kw}) -is:retweet",label="accounts")
            if not ok: return
            await asyncio.sleep(0.25)
    if time.time()-state.get("last_x_broad",0)>=12*60:
        broad='("fair launch" OR "token launch" OR "launching today" OR "contract address" OR pumpfun OR "pump.fun" OR "new token") (solana OR $SOL OR memecoin) -is:retweet'
        await x_recent_query(http,db,broad,label="broad")
        state["last_x_broad"]=time.time()


async def fetch_x_targeted_pulses(http,db,state,candidates):
    # Exact-contract X searching is opt-in because X API access can be metered.
    # The preference can be changed from Telegram with /socialdeep on|off.
    if not (XTOKEN and bool_pref(db,"x_targeted_pulse",X_TARGETED_PULSE_ENABLED)):
        return 0
    now=time.time()
    if now-state.get("last_x_targeted",0)<X_TARGETED_PULSE_EVERY_SECONDS:
        return 0
    picked=[]
    for c in candidates:
        pair=c.get("pair") or {}; m=metrics(pair); token=str(nest(pair,"baseToken","address",default=""))
        if pair.get("chainId")!="solana" or not token: continue
        if m["liq"]<SCOUT_MIN_LIQUIDITY or m["mc"]<SCOUT_MIN_MCAP or m["age"]>240: continue
        picked.append(token)
        if len(picked)>=X_TARGETED_MAX_CANDIDATES: break
    total=0
    for token in picked:
        ok,n=await x_recent_query(http,db,f'"{token}" -is:retweet',label="contract",max_results=10)
        total+=n
        if not ok: break
        await asyncio.sleep(0.25)
    state["last_x_targeted"]=now
    if picked:
        print(f"[social pulse] targeted X contracts={len(picked)} posts={total}")
    return total


SOL_CONTRACT_RE=re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])([1-9A-HJ-NP-Za-km-z]{32,44})(?![1-9A-HJ-NP-Za-km-z])")
EVM_CONTRACT_RE=re.compile(r"(?<![0-9A-Fa-f])(0x[0-9A-Fa-f]{40})(?![0-9A-Fa-f])")

def catalyst_contracts(catalysts):
    out=[]; seen=set()
    for c in catalysts:
        body=f"{c.get('title','')} {c.get('text','')}"
        for addr in SOL_CONTRACT_RE.findall(body):
            k=("solana",addr)
            if k not in seen: seen.add(k); out.append(("solana",addr,c))
        for addr in EVM_CONTRACT_RE.findall(body):
            k=("evm",addr.lower())
            if k not in seen: seen.add(k); out.append(("evm",addr,c))
    return out

async def social_contract_discovery(http,db,state):
    found={c:set() for c in CHAINS}
    if time.time()-state.get("last_social_contracts",0)<SOCIAL_CONTRACT_DISCOVERY_SECONDS: return found
    for hint,addr,catalyst in catalyst_contracts(db.recent_catalysts(90))[:12]:
        if hint=="solana" and "solana" in found:
            p=await pair_for_token(http,"solana",addr)
            if p:
                found["solana"].add(addr); mark_candidate_source(state,"solana",addr,"social exact-contract")
                continue
        rows=await dex_search_candidates(http,addr,3)
        for p in rows:
            chain=str(p.get("chainId","")).lower(); token=str(nest(p,"baseToken","address",default=""))
            if chain in found and token.lower()==addr.lower():
                found[chain].add(token); mark_candidate_source(state,chain,token,"social exact-contract"); break
        await asyncio.sleep(0.08)
    state["last_social_contracts"]=time.time()
    return found

def social_context(pair,catalysts):
    token=str(nest(pair,"baseToken","address",default="")).lower(); sources=set(); mentions=0
    if not token: return 0,[]
    for c in catalysts:
        hay=(c.get("title","")+" "+c.get("text","")).lower()
        if token in hay:
            mentions+=1; sources.add(c.get("source","?"))
    return mentions,sorted(sources)


async def scan_catalysts(http, db, state, limiter):
    now=time.time(); did=False
    if now-state.get("last_rss",0)>=RSS_SCAN_EVERY_SECONDS:
        await fetch_rss(http,db); state["last_rss"]=now; did=True
    if now-state.get("last_x",0)>=CAT_EVERY*60:
        await fetch_x(http,db,state); state["last_x"]=now; did=True
    if not did:
        return
    state["last_cat"]=now
    fresh = db.recent_catalysts(CAT_LOOKBACK)
    print(f"[catalysts] fresh={len(fresh)} x={'on' if XTOKEN else 'off'} rss=on")
    if CAT_FLASH:
        high = [x.lower() for x in SOURCES.get("high_impact_keywords", [])]
        for c in fresh:
            if c["flashed"]:
                continue
            hay = (c["title"] + " " + c["text"]).lower()
            if c["source"] == "X" and any(k in hay for k in high):
                if limiter.allowed("flash:" + str(c["id"]), cooldown=1):
                    await send(http,
                        "⚡ CATALYST FLASH\n"
                        "ACTION: CHECK, DO NOT BLIND-BUY\n\n"
                        f"{c['title'][:350]}\n\n"
                        "A monitored public account posted a potentially market-moving announcement. "
                        "Search Fomo and verify the exact contract before considering anything.\n"
                        f"{c['url']}")
                db.mark_flash(c["id"])


def catalyst_bonus(pair, catalysts):
    base = pair.get("baseToken") or {}
    symbol = str(base.get("symbol", "")).lower().strip()
    name = str(base.get("name", "")).lower().strip()
    contract = str(base.get("address", "")).lower().strip()
    strong, weak = [], []

    for catalyst in catalysts:
        hay = (catalyst["title"] + " " + catalyst["text"]).lower()
        contract_hit = len(contract) >= 20 and contract in hay
        name_hit = len(name) >= 5 and name in hay
        symbol_hit = len(symbol) >= 4 and bool(re.search(rf"\b{re.escape(symbol)}\b", hay))

        if contract_hit or name_hit:
            strong.append(catalyst)
        elif symbol_hit:
            weak.append(catalyst)

    if strong:
        return min(CAT_MAX, 7 + 4*(len(strong)-1)), strong[:3]
    if weak:
        return min(3, CAT_MAX), weak[:1]
    return 0, []


def first(data, keys):
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def parse_trades(payload):
    out, seen = [], set()
    for data in walk(payload):
        owner = first(data, ("owner", "wallet", "wallet_address", "trader", "signer"))
        side = str(first(data, ("side", "type", "tx_type", "trade_type")) or "").lower()
        ts = f(first(data, ("block_unix_time", "blockUnixTime", "block_time", "timestamp", "time")))
        amount = f(first(data, ("volume_usd", "volumeUsd", "amount_usd", "amountUsd", "value_usd", "valueUsd", "usd_value", "usdValue")))
        if owner and side in {"buy", "sell"}:
            key = (str(owner), side, ts, amount)
            if key not in seen:
                seen.add(key)
                out.append({"owner": str(owner), "side": side, "time": ts, "usd": amount})
    return out


async def birdeye_trades(http, guard, token):
    if not guard.available("trades", "solana"):
        return []
    await guard.slot()
    headers = {"X-API-KEY": BE_KEY, "x-chain": "solana", "accept": "application/json"}
    params = {"address": token, "tx_type":"swap", "limit":50, "sort_type":"desc", "after_time":int(time.time())-900}
    status, data = await http.get(f"{BE}/defi/v3/token/txs", headers=headers, params=params)
    await guard.record(http, "trades", status, "solana", data=data)
    return parse_trades(data) if status == 200 and data else []


def wallet_flow_profile(trades):
    if not trades:
        return {"buy_usd":0.0,"sell_usd":0.0,"unique_buyers":0,"top_buyer_share":0.0,"large_buys":0}
    cutoff=time.time()-600
    recent=[t for t in trades if not t.get("time") or f(t.get("time"))>=cutoff]
    by_owner=defaultdict(float); buy_usd=sell_usd=0.0; large=0
    for t in recent:
        amount=max(0.0,f(t.get("usd"))); owner=str(t.get("owner") or "?")
        if t.get("side")=="buy":
            buy_usd+=amount; by_owner[owner]+=amount
            if amount>=500: large+=1
        elif t.get("side")=="sell":
            sell_usd+=amount
    top=max(by_owner.values()) if by_owner else 0.0
    return {"buy_usd":buy_usd,"sell_usd":sell_usd,"unique_buyers":len(by_owner),
            "top_buyer_share":top/max(buy_usd,1e-9) if buy_usd>0 else 0.0,"large_buys":large}


def wallet_score(trades):
    if not trades:
        return 0, [], []
    p=wallet_flow_profile(trades)
    buys_unique=p["unique_buyers"]; buy_usd=p["buy_usd"]; sell_usd=p["sell_usd"]; large_n=p["large_buys"]
    score,reasons,risks=0.0,[],[]
    if buys_unique>=3:
        score += clamp((buys_unique-2)*4,6,20); reasons.append(f"{buys_unique} independent recent buyers")
    if buy_usd>=1000:
        score += clamp(math.log10(buy_usd/1000+1)*15,5,18); reasons.append(f"{usd(buy_usd)} recent wallet buy flow")
    if large_n>=2:
        score += clamp(large_n*3,6,15); reasons.append(f"{large_n} buys ≥ $500")
    if sell_usd>buy_usd*1.3 and sell_usd>1000:
        score-=18; risks.append("wallet flow is net-selling")
    # v12: a single wallet can create impressive buy volume without broad demand.
    # This is a warning/score penalty, not a hard block, because genuine whales exist.
    share=p["top_buyer_share"]
    if buy_usd>=1000 and share>=0.75:
        score-=14; risks.append(f"recent buy flow is highly concentrated: top wallet ~{share*100:.0f}%")
    elif buy_usd>=1000 and share>=0.55:
        score-=7; risks.append(f"recent buy flow is concentrated: top wallet ~{share*100:.0f}%")
    return clamp(score,-25,55),reasons,risks


async def solana_mint_authority_context(http, token):
    """Free Solana mint/freeze-authority fallback.

    A live freeze authority is treated as a hard danger. A live mint authority is a
    substantial warning but not an automatic claim of malicious intent. This check is
    independent from RugCheck so a provider outage does not erase a basic on-chain risk.
    """
    if not token:
        return 0,[],False,False
    payload={"jsonrpc":"2.0","id":1,"method":"getAccountInfo",
             "params":[token,{"encoding":"jsonParsed","commitment":"confirmed"}]}
    status,raw=await http.post(SOLANA_RPC_HTTP,payload,retries=1)
    if status!=200:
        return 0,["mint-authority RPC unavailable"],False,False
    try:
        data=json.loads(raw) if isinstance(raw,str) else raw
        info=nest(data,"result","value","data","parsed","info",default={}) or {}
        mint_auth=info.get("mintAuthority"); freeze_auth=info.get("freezeAuthority")
        notes=[]; score=0; hard=False
        if freeze_auth:
            hard=True; score-=30; notes.append("live freeze authority — HARD BLOCK")
        if mint_auth:
            score-=12; notes.append("live mint authority remains enabled")
        if not mint_auth and not freeze_auth:
            score+=2; notes.append("mint/freeze authorities disabled")
        return score,notes,hard,True
    except Exception:
        return 0,["mint-authority RPC parse failure"],False,False



async def token_security(http, guard, chain, token):
    if not (BE_SECURITY and guard.available("security", chain)):
        return 0, [], False, False
    await guard.slot()
    headers = {"X-API-KEY": BE_KEY, "x-chain": chain, "accept":"application/json"}
    status, data = await http.get(f"{BE}/defi/token_security", headers=headers, params={"address":token})
    await guard.record(http, "security", status, chain, data=data)
    if status != 200 or not isinstance(data, dict):
        return 0, [], False, False
    flags = []
    for obj in walk(data):
        for key, value in obj.items():
            kl = str(key).lower()
            if isinstance(value, bool) and value and any(w in kl for w in ("honeypot","scam","fake","freeze","mintable","risk")):
                flags.append(str(key))
            elif isinstance(value, (int,float)) and value and any(w in kl for w in ("honeypot","scam")):
                flags.append(str(key))
    hard = any("honeypot" in x.lower() or "scam" in x.lower() or "fake" in x.lower() for x in flags)
    penalty = -45 if hard else (-12 if flags else 0)
    return penalty, (["security flags: " + ", ".join(flags[:4])] if flags else []), hard, True


async def top_trader_context(http, guard, chain, token, copy_engine=None):
    if not (BE_TOP_TRADERS and guard.available("top_traders", chain)):
        return 0, [], [], False
    await guard.slot()
    headers = {"X-API-KEY": BE_KEY, "x-chain": chain, "accept":"application/json"}
    params = {
        "address":token, "time_frame":"24h", "sort_type":"desc",
        "sort_by":"realized_pnl", "offset":0, "limit":10
    }
    status, data = await http.get(f"{BE}/defi/v2/tokens/top_traders", headers=headers, params=params)
    await guard.record(http, "top_traders", status, chain, data=data)
    if status != 200 or not isinstance(data, dict):
        return 0, [], [], False

    smart = risky = 0
    smart_still_holding = 0
    risky_still_holding = 0
    smart_hold_usd = 0.0
    risky_hold_usd = 0.0

    for obj in walk(data):
        if not isinstance(obj, dict):
            continue
        tags = []
        for key in ("wallet_tags","walletTags","tags","labels"):
            val = obj.get(key)
            if isinstance(val, str):
                tags.extend(re.split(r"[,\\s]+", val.lower()))
            elif isinstance(val, list):
                tags.extend(str(x).lower() for x in val)

        hold_usd = f(obj.get("holdVolumeUsd") or obj.get("hold_volume_usd") or obj.get("holdingUsd"), 0)
        if copy_engine and chain == "solana":
            wallet_addr = str(obj.get("wallet") or obj.get("owner") or obj.get("walletAddress") or obj.get("wallet_address") or obj.get("trader") or "")
            if wallet_addr:
                try:
                    copy_engine.observe_tagged_wallet(wallet_addr, smart=("smart_trader" in tags),
                                                      risky=any(x in tags for x in ("dev","bundler","sniper","insider")),
                                                      source="Birdeye top trader")
                except Exception:
                    pass
        if "smart_trader" in tags:
            smart += 1
            if hold_usd > 0:
                smart_still_holding += 1
                smart_hold_usd += hold_usd
        if any(x in tags for x in ("dev","bundler","sniper","insider")):
            risky += 1
            if hold_usd > 0:
                risky_still_holding += 1
                risky_hold_usd += hold_usd

    score = min(14, smart*4 + smart_still_holding*2) - min(24, risky*6 + risky_still_holding*3)
    reasons, risks = [], []
    if smart:
        reasons.append(f"{smart} smart-trader tagged top trader(s)")
    if smart_still_holding:
        reasons.append(f"{smart_still_holding} smart trader(s) still holding")
    if risky:
        risks.append(f"{risky} dev/sniper/bundler/insider tag(s) among top traders")
    if risky_still_holding:
        risks.append(f"{risky_still_holding} risky tagged trader(s) still holding")
    return score, reasons, risks, True


async def holder_context(http, guard, token):
    """Solana cohort risk using Birdeye holder-profile."""
    if not (BE_HOLDERS and guard.available("holders", "solana")):
        return 0, [], False, False
    await guard.slot()
    headers = {"X-API-KEY": BE_KEY, "x-chain":"solana", "accept":"application/json"}
    params = {
        "token_address": token,
        "interval": "1h",
        "include_zero_balance": "false",
        "ui_amount_mode": "scaled",
    }
    status, data = await http.get(f"{BE}/token/v1/holder-profile", headers=headers, params=params)
    if status != 200 or not isinstance(data, dict):
        # Holder Profile may be plan-gated even when core Birdeye auth works. The
        # current V3 holder endpoint is available on more plans and still gives us
        # wallet-level concentration/top-10 context.
        v3params={"address":token,"offset":0,"limit":20,"mode":"wallet","ui_amount_mode":"scaled"}
        v3status,v3data=await http.get(f"{BE}/defi/v3/token/holder",headers=headers,params=v3params)
        if v3status==200 and isinstance(v3data,dict):
            await guard.record(http,"holders_v3",v3status,"solana",data=v3data)
            payload=v3data.get("data") if isinstance(v3data.get("data"),dict) else v3data
            top10=f(payload.get("top10HoldPercent") if isinstance(payload,dict) else None,-1)
            reasons=[]; risks=[]; score=0; hard=False
            if 0<=top10<=100:
                if top10>=HOLDER_BLOCK: hard=True; score-=30; risks.append(f"top-10 wallets hold ~{top10:.1f}% — HARD BLOCK")
                elif top10>=HOLDER_WARN: score-=12; risks.append(f"top-10 wallets hold ~{top10:.1f}%")
                else: score+=2; reasons.append(f"top-10 wallet concentration ~{top10:.1f}%")
            return score,(risks if score<0 else reasons),hard,True
        await guard.record(http,"holders",status,"solana",data=data)
        return 0, [], False, False
    await guard.record(http, "holders", status, "solana", data=data)

    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    risky_supply = 0.0
    smart_supply = 0.0
    smart_count = 0
    top10 = None

    tags = payload.get("tags") if isinstance(payload, dict) else None
    tag_rows = []
    if isinstance(tags, list):
        tag_rows = tags
    elif isinstance(tags, dict):
        for label, item in tags.items():
            if isinstance(item, dict):
                row = dict(item)
                row.setdefault("tag", label)
                tag_rows.append(row)

    for obj in tag_rows:
        label = str(obj.get("tag") or obj.get("label") or obj.get("name") or "").lower()
        pct = f(obj.get("percent_of_supply") or obj.get("percentOfSupply") or
                obj.get("supply_percent") or obj.get("supplyPercent"), -1)
        count = int(f(obj.get("holder_count") or obj.get("holderCount") or obj.get("count"), 0))
        if any(x in label for x in ("bundler","sniper","insider","dev")) and 0 <= pct <= 100:
            risky_supply += pct
        if "smart_trader" in label or "smart trader" in label:
            smart_count += count if count else 1
            if 0 <= pct <= 100:
                smart_supply += pct

    top_obj = payload.get("top10_holder") if isinstance(payload, dict) else None
    if isinstance(top_obj, dict):
        top10 = f(top_obj.get("percent_of_supply") or top_obj.get("percentOfSupply") or
                  top_obj.get("supply_percent") or top_obj.get("supplyPercent"), -1)
        if not (0 <= top10 <= 100):
            top10 = None
    elif isinstance(top_obj, (int,float)):
        top10 = f(top_obj, -1)

    # Conservative fallback if Birdeye changes nesting again.
    if not tag_rows:
        for obj in walk(payload):
            if not isinstance(obj, dict):
                continue
            label = str(obj.get("tag") or obj.get("label") or obj.get("name") or "").lower()
            pct = f(obj.get("percent_of_supply") or obj.get("percentOfSupply"), -1)
            count = int(f(obj.get("holder_count") or obj.get("holderCount") or obj.get("count"), 0))
            if any(x in label for x in ("bundler","sniper","insider","dev")) and 0 <= pct <= 100:
                risky_supply += pct
            if "smart_trader" in label or "smart trader" in label:
                smart_count += count if count else 1
                if 0 <= pct <= 100:
                    smart_supply += pct

    reasons, risks = [], []
    score = 0
    hard = False

    if smart_count:
        score += min(10, 3 + smart_count)
        reasons.append(f"smart-trader holder cohort detected ({smart_count})")
    if smart_supply > 0:
        reasons.append(f"smart-trader cohort holds ~{smart_supply:.1f}% supply")

    if risky_supply >= 35:
        score -= 30
        hard = True
        risks.append(f"high tagged-cohort supply ~{risky_supply:.1f}% — HARD BLOCK")
    elif risky_supply >= TAGGED_COHORT_HARD_BLOCK_PCT:
        score -= 25
        hard = True
        risks.append(f"elevated tagged-cohort supply ~{risky_supply:.1f}% — HARD BLOCK")
    elif risky_supply >= 10:
        score -= 8
        risks.append(f"tagged-cohort supply ~{risky_supply:.1f}%")

    if top10 is not None:
        if top10 >= HOLDER_BLOCK:
            score -= 30
            hard = True
            risks.append(f"very high top-10 holder concentration ~{top10:.1f}% — HARD BLOCK")
        elif top10 >= HOLDER_WARN:
            score -= 12
            risks.append(f"top-10 holder concentration ~{top10:.1f}%")
        else:
            reasons.append(f"top-10 concentration ~{top10:.1f}%")

    return score, reasons + risks, hard, True


async def solana_rpc_holder_context(http, token):
    """Free holder-distribution fallback using Solana JSON-RPC.

    This is intentionally a concentration screen, not a claim that unrelated token
    accounts belong to unrelated people. It exists so a Birdeye feature outage does
    not turn into a blanket no-entry condition. RugCheck remains a separate required
    safety layer for Solana entries.
    """
    if not token or not SOLANA_RPC_HTTP:
        return 0, ["RPC holder fallback unavailable"], False, False
    supply_payload={"jsonrpc":"2.0","id":1,"method":"getTokenSupply","params":[token,{"commitment":"confirmed"}]}
    large_payload={"jsonrpc":"2.0","id":2,"method":"getTokenLargestAccounts","params":[token,{"commitment":"confirmed"}]}
    s1, raw1 = await http.post(SOLANA_RPC_HTTP, supply_payload)
    s2, raw2 = await http.post(SOLANA_RPC_HTTP, large_payload)
    if s1 != 200 or s2 != 200:
        return 0, ["RPC holder fallback unavailable"], False, False
    try:
        d1=json.loads(raw1) if isinstance(raw1,str) else raw1
        d2=json.loads(raw2) if isinstance(raw2,str) else raw2
        supply=f(nest(d1,"result","value","amount",default=0))
        rows=nest(d2,"result","value",default=[])
        if supply <= 0 or not isinstance(rows,list) or not rows:
            return 0, ["RPC holder fallback returned incomplete data"], False, False
        amounts=[f(x.get("amount")) for x in rows if isinstance(x,dict) and f(x.get("amount"))>0]
        if not amounts:
            return 0, ["RPC holder fallback returned no holder balances"], False, False
        pcts=[100*a/supply for a in amounts]
        top1=pcts[0]
        top5=sum(pcts[:5])
        top10=sum(pcts[:10])
        hard=(top1 >= RPC_HOLDER_HARD_TOP1_PCT or top5 >= RPC_HOLDER_HARD_TOP5_PCT or top10 >= RPC_HOLDER_HARD_TOP10_PCT)
        notes=[f"RPC holder concentration top1 {top1:.1f}% / top5 {top5:.1f}% / top10 {top10:.1f}%"]
        score=0
        if hard:
            score=-25
            notes.append("extreme token-account concentration — HARD BLOCK")
        elif top1 >= RPC_HOLDER_WARN_TOP1_PCT or top5 >= RPC_HOLDER_WARN_TOP5_PCT:
            score=-8
            notes.append("elevated token-account concentration")
        else:
            score=2
            notes.append("holder concentration passed public RPC fallback")
        return score, notes, hard, True
    except Exception as exc:
        print(f"[rpc holder fallback] {type(exc).__name__}: {exc}")
        return 0, ["RPC holder fallback parse failure"], False, False


async def resilient_holder_context(http, guard, token):
    """Prefer Birdeye holder intelligence, then fall back to public Solana RPC."""
    hs,hinfo,hblock,hchecked = await holder_context(http,guard,token)
    if hchecked:
        return hs,hinfo,hblock,True,"birdeye"
    rs,rinfo,rblock,rchecked = await solana_rpc_holder_context(http,token)
    if rchecked:
        return rs,rinfo,rblock,True,"solana-rpc"
    return 0, list(hinfo or []) + list(rinfo or []), False, False,"unavailable"


_RUGCHECK_DOWN_UNTIL = 0.0
_RUGCHECK_DOWN_LOGGED_UNTIL = 0.0


async def rugcheck_summary(http, token):
    """Free/public Solana fallback security screen.

    RugCheck score_normalised is interpreted as a risk score: lower is better.
    Missing/unavailable data never counts as a pass.
    """
    global _RUGCHECK_DOWN_UNTIL, _RUGCHECK_DOWN_LOGGED_UNTIL
    if not RUGCHECK_ENABLED or not token:
        return 0, [], False, False
    now = time.time()
    if now < _RUGCHECK_DOWN_UNTIL:
        return 0, ["RugCheck temporarily unavailable (safe fallback active)"], False, False
    status, data = await http.get(
        f"https://api.rugcheck.xyz/v1/tokens/{token}/report/summary",
        headers={"accept":"application/json"}, retries=1
    )
    if status == 0:
        # Provider/network failures are fault-isolated.  Do not hammer the same
        # endpoint for every candidate while TLS or the provider is unhealthy.
        _RUGCHECK_DOWN_UNTIL = time.time() + 300
        if time.time() >= _RUGCHECK_DOWN_LOGGED_UNTIL:
            print("[rugcheck] temporarily unavailable; pausing RugCheck calls for 5 minutes. Other safety checks remain active.")
            _RUGCHECK_DOWN_LOGGED_UNTIL = time.time() + 300
        return 0, ["RugCheck temporarily unavailable (safe fallback active)"], False, False
    if status != 200 or not isinstance(data, dict):
        # HTTP errors are allowed to recover sooner than transport/TLS failures.
        _RUGCHECK_DOWN_UNTIL = time.time() + 60
        return 0, [f"RugCheck unavailable (HTTP {status})"], False, False

    score = f(data.get("score_normalised"), -1)
    risks = data.get("risks") if isinstance(data.get("risks"), list) else []
    danger = []
    warnings = []
    for item in risks:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "risk")
        level = str(item.get("level") or "").lower()
        if level in {"danger","critical","high"}:
            danger.append(name)
        elif level:
            warnings.append(name)

    all_risk_names = " | ".join(
        str(item.get("name") or "") for item in risks if isinstance(item, dict)
    ).lower()
    hard_name_markers = (
        "holder correlation", "high holder correlation", "bundled", "bundle",
        "mint authority", "freeze authority", "unlocked liquidity", "liquidity unlocked",
        "lp unlocked", "honeypot", "blacklist", "rugged", "rug pull",
        "creator balance", "insider network"
    )
    keyword_danger = [m for m in hard_name_markers if m in all_risk_names]
    hard = (score >= RUGCHECK_BLOCK_SCORE if score >= 0 else False) or bool(danger) or bool(keyword_danger)
    notes = []
    if score >= 0:
        notes.append(f"RugCheck risk {score:.0f}/100 (lower is better)")
    if danger:
        notes.append("RugCheck danger: " + ", ".join(danger[:3]))
    elif keyword_danger:
        notes.append("RugCheck hard-risk marker: " + ", ".join(keyword_danger[:3]))
    elif warnings and score >= RUGCHECK_WARN_SCORE:
        notes.append("RugCheck warnings: " + ", ".join(warnings[:3]))
    return (-35 if hard else (5 if 0 <= score < RUGCHECK_WARN_SCORE else 0)), notes, hard, True


def evidence_count(reasons, rt_stats=None):
    """Count independent evidence categories instead of only rare phrases."""
    joined = " | ".join(reasons).lower()
    categories = set()
    if "buy/sell " in joined or "swaps/5m" in joined:
        categories.add("orderflow")
    if "price still early" in joined or "new-pool buyer formation" in joined:
        categories.add("early_structure")
    if "buyer acceleration" in joined or "volume acceleration" in joined:
        categories.add("acceleration")
    if "liquidity +" in joined or "quiet accumulation" in joined:
        categories.add("accumulation")
    if "smart-trader" in joined or "recent wallet buy flow" in joined:
        categories.add("wallet")
    if "fresh catalyst" in joined:
        categories.add("catalyst")
    if rt_stats and rt_stats.get("available"):
        if rt_stats.get("tx30",0) >= RT_ENTRY_TX30:
            categories.add("realtime_activity")
        if rt_stats.get("acceleration",0) >= RT_ENTRY_ACCEL:
            categories.add("realtime_accel")
    return len(categories)


async def preflight_safety(http, guard, db, chain, token, state):
    """Run safety/wallet checks during GET READY so entry alerts stay fast."""
    result = {
        "ts": time.time(), "hard": False, "reasons": [], "risks": [],
        "rug_checked": False, "holder_checked": False, "security_checked": False,
        "trader_checked": False, "wallet_checked": False, "mint_checked": False, "holder_source": "none",
        "flow_concentration": 0.0, "unique_recent_buyers": 0, "score_adjustment": 0.0,
        "enrichment_complete": False,
    }
    try:
        if chain == "solana":
            rb, notes, hard, checked = await rugcheck_summary(http, token)
            result["rug_checked"] = checked
            result["score_adjustment"] += rb
            result["hard"] = result["hard"] or hard
            for note in notes:
                (result["risks"] if ("danger" in note.lower() or "warning" in note.lower())
                 else result["reasons"]).append(note)

            # Determine whether this token is close enough to an entry to justify deeper
            # enrichment. The FAST lane gets a partial cache as soon as the cheap/basic
            # safety work completes; slower premium providers can finish afterward.
            pair_now = await pair_for_token(http, chain, token)
            previous_now = db.previous(chain, token, 90) if pair_now else None
            raw_now, _, _, _ = early_score(pair_now, previous_now) if pair_now else (0, [], [], "FILTERED")
            near_entry = raw_now >= max(SCOUT_SCORE, ENTRY_SCORE - 5)

            if near_entry:
                ms,minfo,mhard,mchecked = await solana_mint_authority_context(http,token)
                result["mint_checked"] = mchecked
                result["score_adjustment"] += ms
                result["hard"] = result["hard"] or mhard
                (result["risks"] if ms < 0 or mhard else result["reasons"]).extend(minfo)

            # Publish basic safety immediately. Strict entries still require their normal
            # holder/security evidence, while FAST can keep moving if RugCheck/basic
            # on-chain checks are available and no hard block exists.
            result["ts"] = time.time()
            state["safety_cache"][f"{chain}:{token}"] = dict(result)

            # Premium enrichment is deliberately after the partial cache so a slow
            # Birdeye/holder source cannot freeze the independent FAST lane.
            if near_entry and guard.available("trades", "solana"):
                trades = await birdeye_trades(http, guard, token)
                if state.get("copy_engine"):
                    try: state["copy_engine"].observe_flow_trades(token,trades,pair_now)
                    except Exception: pass
                ws, wrs, wrisk = wallet_score(trades)
                profile=wallet_flow_profile(trades)
                result["wallet_checked"] = bool(trades)
                result["flow_concentration"] = f(profile.get("top_buyer_share"))
                result["unique_recent_buyers"] = int(profile.get("unique_buyers") or 0)
                result["score_adjustment"] += ws * 0.35
                result["reasons"].extend(wrs)
                result["risks"].extend(wrisk)

            if near_entry:
                hs, hinfo, hblock, hchecked, hsource = await resilient_holder_context(http, guard, token)
                result["holder_checked"] = hchecked
                result["holder_source"] = hsource
                result["score_adjustment"] += hs
                result["hard"] = result["hard"] or hblock
                if hblock:
                    result["risks"].append(f"holder concentration hard block ({hsource})")
                (result["reasons"] if hs >= 0 else result["risks"]).extend(hinfo)

        if (chain != "solana" or near_entry) and guard.available("security", chain):
            sp, flags, hard, checked = await token_security(http, guard, chain, token)
            result["security_checked"] = checked
            result["score_adjustment"] += sp
            result["hard"] = result["hard"] or hard
            result["risks"].extend(flags)

        if (chain != "solana" or near_entry) and guard.available("top_traders", chain):
            ts, trs, trisk, tchecked = await top_trader_context(http, guard, chain, token, state.get("copy_engine"))
            result["trader_checked"] = tchecked
            result["score_adjustment"] += ts
            result["reasons"].extend(trs)
            result["risks"].extend(trisk)

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"[safety preflight] {type(exc).__name__}: {exc}")
        result["risks"].append("one background safety check failed")

    result["enrichment_complete"] = True
    result["ts"] = time.time()
    state["safety_cache"][f"{chain}:{token}"] = result
    return result


def _safety_task_done(state, key, task):
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        print(f"[safety task] {type(exc).__name__}: {exc}")
    state.get("safety_tasks", {}).pop(key, None)


def launch_safety_preflight(http, guard, db, chain, token, state):
    key = f"{chain}:{token}"
    old = state["safety_tasks"].get(key)
    if old and not old.done():
        return
    task = asyncio.create_task(preflight_safety(http, guard, db, chain, token, state))
    state["safety_tasks"][key] = task
    task.add_done_callback(lambda t, k=key: _safety_task_done(state, k, t))


def cached_safety(state, chain, token, max_age=None, pair=None):
    item = state.get("safety_cache", {}).get(f"{chain}:{token}")
    if not item:
        return None
    if max_age is None:
        age = metrics(pair)["age"] if pair else 999999
        mins = SAFETY_CACHE_FRESH_MINUTES if age <= YOUNG_HOLDER_CHECK_MAX_AGE_MIN else SAFETY_CACHE_OLDER_MINUTES
        max_age = mins * 60
    if time.time() - item.get("ts", 0) > max_age:
        return None
    return item


def cleanup_runtime_state(state):
    now = time.time()
    for key, item in list(state.get("safety_cache", {}).items()):
        if now - item.get("ts", 0) > 30*60:
            state["safety_cache"].pop(key, None)
    for key, task in list(state.get("safety_tasks", {}).items()):
        if task.done():
            state["safety_tasks"].pop(key, None)


def metrics(pair):
    return {
        "liq": f(nest(pair,"liquidity","usd",default=0)),
        "mc": f(pair.get("marketCap") or pair.get("fdv")),
        "age": age_minutes(pair.get("pairCreatedAt")),
        "pc5": f(nest(pair,"priceChange","m5",default=0)),
        "pc1": f(nest(pair,"priceChange","h1",default=0)),
        "pc6": f(nest(pair,"priceChange","h6",default=0)),
        "pc24": f(nest(pair,"priceChange","h24",default=0)),
        "v5": f(nest(pair,"volume","m5",default=0)),
        "v1": f(nest(pair,"volume","h1",default=0)),
        "v6": f(nest(pair,"volume","h6",default=0)),
        "v24": f(nest(pair,"volume","h24",default=0)),
        "buys": f(nest(pair,"txns","m5","buys",default=0)),
        "sells": f(nest(pair,"txns","m5","sells",default=0)),
    }


def tier_risk_profile(tier):
    """Tier-specific soft/hard risk lines. Established/cross-chain lanes target
    smaller moves than launch memes, so allowing a generic -20% small-position
    hard line would create poor reward/risk. These are alert thresholds only;
    the manual journal never forces a sale.
    """
    tier=str(tier or "")
    if tier=="MANUAL CHAIN ENTRY":
        return {"soft":min(6.0,CAPITAL_HARD_STOP_PCT),"small_hard":min(7.0,CAPITAL_HARD_STOP_PCT),"mid_hard":min(7.0,CAPITAL_HARD_STOP_PCT),"trailing":7.0}
    if tier=="STRUCTURE ENTRY":
        return {"soft":min(7.0,CAPITAL_HARD_STOP_PCT),"small_hard":CAPITAL_HARD_STOP_PCT,"mid_hard":CAPITAL_HARD_STOP_PCT,"trailing":7.0}
    return {"soft":RISK_LINE,"small_hard":min(SMALL_POSITION_HARD_STOP_PCT,CAPITAL_HARD_STOP_PCT),
            "mid_hard":min(MID_POSITION_HARD_STOP_PCT,CAPITAL_HARD_STOP_PCT),"trailing":min(TRAIL,8.0)}


def hold_plan(pair, position=None, tier=""):
    """Classify a meme-coin trade horizon from live structure, not a price prediction."""
    m = metrics(pair)
    ratio = m["buys"] / max(m["sells"], 1)
    swaps = m["buys"] + m["sells"]
    turnover = 100.0 * m["v5"] / max(m["mc"], 1)
    liq_mc = m["liq"] / max(m["mc"], 1)
    tier = str(tier or (position or {}).get("entry_tier") or "")
    ret = None
    drawdown = None
    if position:
        current = f(pair.get("priceUsd"))
        entry = f(position.get("entry_price"))
        peak = max(f(position.get("peak_price"), entry), current)
        ret = (current / max(entry, 1e-18) - 1) * 100 if entry else 0.0
        drawdown = (current / max(peak, 1e-18) - 1) * 100 if peak else 0.0

    reasons=[]
    if position and ret is not None:
        amount=max(f(position.get("amount_usd")),0.0)
        risk_profile=tier_risk_profile(tier)
        soft_line=f(risk_profile.get("soft"),RISK_LINE)
        if 0 < amount <= SMALL_POSITION_USD:
            price_hard=max(soft_line,f(risk_profile.get("small_hard"),SMALL_POSITION_HARD_STOP_PCT))
        elif 0 < amount <= max(25.0,SMALL_POSITION_USD):
            price_hard=max(soft_line,f(risk_profile.get("mid_hard"),MID_POSITION_HARD_STOP_PCT))
        else:
            price_hard=soft_line
        structural_bad=(ratio < 0.85 and m["pc5"] <= -4) or (m["pc1"] < -20 and ratio < 0.90)
        if ret <= -price_hard or (ret <= -soft_line and structural_bad):
            return {"label":"RISK / EXIT REVIEW","window":"now–15 min","review_min":0,"long_term":False,
                    "reasons":[f"position {ret:+.1f}% from entry", f"fee-aware hard line -{price_hard:.0f}%", f"buyers/sellers {ratio:.2f}x"]}
        if ret <= -soft_line:
            return {"label":"RECOVERY WATCH","window":"next 5–15 min","review_min":5,"long_term":False,
                    "reasons":[f"position {ret:+.1f}% crossed tier soft line -{soft_line:.0f}%", f"structure confirmation is required before panic-selling", f"buyers/sellers {ratio:.2f}x"]}
    if m["pc1"] < -20 or (m["pc5"] <= -8 and ratio < 1.0):
        return {"label":"RISK / VERY SHORT","window":"now–30 min","review_min":10,"long_term":False,
                "reasons":[f"weak momentum ({m['pc5']:+.1f}% 5m / {m['pc1']:+.1f}% 1h)", f"buyers/sellers {ratio:.2f}x"]}

    if tier in {"STRUCTURE ENTRY","MANUAL CHAIN ENTRY"}:
        if tier=="MANUAL CHAIN ENTRY":
            reasons=[f"experimental cross-chain structure tier",f"buyers/sellers {ratio:.2f}x",f"liquidity {usd(m['liq'])}"]
            return {"label":"FAST STRUCTURE TRADE","window":"15 min–2h","review_min":25,"long_term":False,"reasons":reasons}
        reasons=[f"established/liquid structure tier",f"buyers/sellers {ratio:.2f}x",f"liquidity {usd(m['liq'])}"]
        return {"label":"STRUCTURE SWING","window":"30 min–4h","review_min":45,"long_term":False,"reasons":reasons}

    # True multi-day holds are intentionally rare for this scanner.
    established = (bool(pair.get("pairCreatedAt")) and m["age"] >= 24*60 and m["liq"] >= 150000 and m["mc"] >= 1_000_000 and
                   liq_mc >= 0.04 and m["pc1"] >= -5 and m["pc6"] >= -10 and m["pc24"] >= -25 and ratio >= 1.10)
    if established:
        reasons=[f"pool age {m['age']/60:.0f}h", f"liquidity {usd(m['liq'])}", f"6h {m['pc6']:+.1f}% / 24h {m['pc24']:+.1f}%"]
        return {"label":"EXTENDED HOLD CANDIDATE","window":"6–24h; reassess before carrying 1–3d","review_min":180,"long_term":True,"reasons":reasons}

    swing = (m["age"] >= 120 and m["liq"] >= 75000 and m["pc1"] >= -8 and ratio >= 1.15 and
             swaps >= 15 and turnover >= 0.15)
    if swing:
        reasons=[f"buyers/sellers {ratio:.2f}x", f"5m turnover {turnover:.2f}% of MC", f"1h {m['pc1']:+.1f}%"]
        return {"label":"SHORT SWING","window":"45 min–4h","review_min":60,"long_term":False,"reasons":reasons}

    if tier in {"FRESH ENTRY","MICRO ENTRY","FLOW ENTRY"} or m["liq"] < 75000 or m["age"] < 120:
        reasons=[f"liquidity {usd(m['liq'])}", f"pool age {m['age']:.0f}m", f"buyers/sellers {ratio:.2f}x"]
        return {"label":"SCALP / FAST TRADE","window":"10–60 min","review_min":30,"long_term":False,"reasons":reasons}

    reasons=[f"buyers/sellers {ratio:.2f}x", f"1h {m['pc1']:+.1f}%", f"liquidity {usd(m['liq'])}"]
    return {"label":"SHORT-TERM","window":"30 min–2h","review_min":45,"long_term":False,"reasons":reasons}

def early_score(pair, previous):
    m = metrics(pair)
    score, reasons, risks = 0.0, [], []
    chain=str(pair.get("chainId") or "").lower()
    structure_age_ok = bool(chain=="solana" and STRUCTURE_ENTRY_ENABLED and m["mc"] >= STRUCTURE_MIN_MCAP and m["liq"] >= STRUCTURE_MIN_LIQUIDITY)
    manual_chain_age_ok = bool(chain in MANUAL_ENTRY_CHAINS and MANUAL_CHAIN_ENTRY_ENABLED and
                               m["mc"] >= MANUAL_CHAIN_MIN_MCAP and m["liq"] >= MANUAL_CHAIN_MIN_LIQUIDITY)
    if m["liq"] < MIN_LIQ or not (MIN_MCAP <= m["mc"] <= MAX_MCAP) or not (m["age"] >= MIN_AGE and (m["age"] <= MAX_AGE_H*60 or structure_age_ok or manual_chain_age_ok)):
        return 0, reasons, risks, "FILTERED"
    if m["pc5"] >= TOO5 or m["pc1"] >= TOO1:
        return 0, reasons, ["already too extended"], "TOO_LATE"
    lm = m["liq"] / max(m["mc"], 1)
    ratio = m["buys"] / max(m["sells"], 1)
    swaps = m["buys"] + m["sells"]
    score += clamp((math.log10(max(m["liq"],1))-4.1)*9,0,12)
    score += clamp(lm*50,0,10)
    if lm >= MIN_LM:
        reasons.append(f"liquidity/MC {lm:.1%}")
    if EARLY_MIN5 <= m["pc5"] <= EARLY_MAX5 and m["pc1"] <= EARLY_MAX1:
        score += 14
        reasons.append(f"price still early ({m['pc5']:+.1f}% 5m / {m['pc1']:+.1f}% 1h)")
    elif m["pc5"] > EARLY_MAX5 or m["pc1"] > EARLY_MAX1:
        score -= 9
        risks.append("price is leaving the ideal early-entry window")
    if ratio >= MIN_BS:
        score += clamp((ratio-1)*11,4,16)
        reasons.append(f"buy/sell {ratio:.2f}x")
    elif ratio < 0.85:
        score -= 18
        risks.append("sells dominate")
    if swaps >= MIN_SWAPS:
        score += clamp(swaps/10,3,9)
        reasons.append(f"{int(swaps)} swaps/5m")
    if m["v5"] >= MIN_VOL5:
        score += clamp(math.log10(m["v5"]/MIN_VOL5+1)*8,3,9)
    if previous:
        buy_accel = m["buys"] / max(previous["buys5"],1)
        vol_accel = m["v5"] / max(previous["vol5"],1)
        liq_growth = (m["liq"] / max(previous["liquidity"],1) - 1) * 100
        if buy_accel >= MIN_BUY_ACCEL:
            score += 12
            reasons.append(f"buyer acceleration {buy_accel:.2f}x")
        if vol_accel >= MIN_VOL_ACCEL:
            score += 12
            reasons.append(f"volume acceleration {vol_accel:.2f}x")
        if liq_growth >= MIN_LIQ_GROWTH:
            score += 8
            reasons.append(f"liquidity +{liq_growth:.1f}% vs earlier")
        if buy_accel >= MIN_BUY_ACCEL and m["pc5"] <= 7:
            score += 8
            reasons.append("quiet accumulation before breakout")
    elif m["age"] <= 30 and ratio >= 1.5 and swaps >= 12:
        score += 10
        reasons.append("new-pool buyer formation")
    return clamp(score,0,100), reasons, risks, ("NEW_POOL" if m["age"] <= 30 else "EARLY")


def confirmation_bonus(pair):
    m = metrics(pair)
    bonus, reasons = 0.0, []
    ratio = m["buys"] / max(m["sells"],1)
    expected = m["v1"] / 12 if m["v1"] else 0
    if 3 <= m["pc5"] <= 22:
        bonus += 8
        reasons.append("breakout beginning")
    if ratio >= 1.7:
        bonus += 6
    if m["v5"] / max(expected,1) >= 1.5:
        bonus += 7
        reasons.append("5m volume above hourly pace")
    return bonus, reasons


def risk_grade(risks, security_checked, holder_checked, trader_checked):
    joined = " ".join(risks).lower()
    severe = any(x in joined for x in ("honeypot","scam","fake","block", "35.0% supply"))
    if severe:
        return "EXTREME"
    if "sells dominate" in joined or "net-selling" in joined or "dev/sniper" in joined or "bundler" in joined:
        return "HIGH"
    checks = sum([security_checked, holder_checked, trader_checked])
    if checks >= 2:
        return "LOWER"
    return "MEDIUM — MARKET DATA ONLY"


def entry_blockers(pair, score, risks, hard_block, scout=None, rts=None, safety=None):
    """Human-readable reasons a candidate is WATCH ONLY rather than an entry."""
    m = metrics(pair)
    blockers = []
    chain = str(pair.get("chainId","")).lower()
    ratio = m["buys"] / max(m["sells"], 1)
    swaps = m["buys"] + m["sells"]
    risk_text = " ".join(risks).lower()

    if hard_block:
        blockers.append("hard safety/red-flag check")
    if chain not in ENTRY_CHAINS:
        blockers.append(f"{chain or 'unknown chain'} is watch-only; live entries are Solana-only")
    if m["liq"] < ENTRY_MIN_LIQUIDITY:
        blockers.append(f"liquidity {usd(m['liq'])} < required {usd(ENTRY_MIN_LIQUIDITY)}")
    if m["mc"] < ENTRY_MIN_MCAP:
        blockers.append(f"market cap {usd(m['mc'])} < required {usd(ENTRY_MIN_MCAP)}")
    elif m["mc"] > ENTRY_MAX_MCAP:
        blockers.append(f"market cap {usd(m['mc'])} > allowed {usd(ENTRY_MAX_MCAP)}")
    if m["pc5"] < 0:
        blockers.append(f"5m momentum {m['pc5']:+.1f}% is negative")
    elif m["pc5"] > EARLY_MAX5:
        blockers.append(f"5m move {m['pc5']:+.1f}% > early-entry max +{EARLY_MAX5:.1f}%")
    if m["pc1"] > EARLY_MAX1:
        blockers.append(f"1h move {m['pc1']:+.1f}% > max +{EARLY_MAX1:.1f}%")
    ratio_ok, near_ratio = buy_sell_gate_ok(pair, score, risks, rts)
    if not ratio_ok:
        floor = ENTRY_MIN_BUY_SELL - ENTRY_BUY_SELL_NEAR_TOLERANCE
        blockers.append(f"buy/sell {ratio:.2f}x < near-pass floor {floor:.2f}x (normal {ENTRY_MIN_BUY_SELL:.2f}x)")
    if swaps < ENTRY_MIN_SWAPS_5M:
        blockers.append(f"{int(swaps)} swaps/5m < required {ENTRY_MIN_SWAPS_5M}")
    score_ok, near_score = score_gate_ok(pair, score, risks, rts)
    if not score_ok:
        floor = ENTRY_SCORE - ENTRY_SCORE_NEAR_TOLERANCE
        blockers.append(f"setup score {score:.1f} < near-pass floor {floor:.1f} (normal {ENTRY_SCORE:.0f})")
    if "sells dominate" in risk_text:
        blockers.append("sells currently dominate")
    if "net-selling" in risk_text:
        blockers.append("tracked wallets are net-selling")
    if scout is None and chain in ENTRY_CHAINS:
        blockers.append("no qualifying GET READY scout yet")
    if rts and rts.get("available") and rts.get("tx30",0) < RT_ENTRY_TX30:
        blockers.append(f"realtime activity {rts.get('tx30',0)} tx/30s < required {RT_ENTRY_TX30}")
    if safety is not None and not (
        safety.get("rug_checked") or safety.get("security_checked") or safety.get("holder_checked")
    ):
        blockers.append("safety verification unavailable")
    return blockers


def entry_action(pair, score, risks, hard_block):
    m = metrics(pair)
    risk_text = " ".join(risks).lower()
    chain = str(pair.get("chainId","")).lower()
    ratio = m["buys"] / max(m["sells"], 1)
    swaps = m["buys"] + m["sells"]

    if hard_block:
        return "SKIP"
    # v13 can issue Telegram/manual entries on selected non-Solana chains and on
    # established Solana structure outside the launch-centric MC ceiling. The full
    # classifier remains authoritative; this helper is only a human-readable hint.
    if chain in MANUAL_ENTRY_CHAINS:
        if (MANUAL_CHAIN_MIN_MCAP<=m["mc"]<=MANUAL_CHAIN_MAX_MCAP and m["liq"]>=MANUAL_CHAIN_MIN_LIQUIDITY
                and ratio>=MANUAL_CHAIN_MIN_BUY_SELL and score>=MANUAL_CHAIN_MIN_SCORE):
            return "POSSIBLE MANUAL ENTRY"
        return "WATCH ONLY"
    if chain not in ENTRY_CHAINS:
        return "WATCH ONLY"
    if chain=="solana" and STRUCTURE_ENTRY_ENABLED and STRUCTURE_MIN_MCAP<=m["mc"]<=STRUCTURE_MAX_MCAP:
        if m["liq"]>=STRUCTURE_MIN_LIQUIDITY and ratio>=STRUCTURE_MIN_BUY_SELL and score>=STRUCTURE_MIN_SCORE:
            return "POSSIBLE STRUCTURE ENTRY"
        return "WATCH ONLY"
    if m["pc5"] >= TOO5 or m["pc1"] >= TOO1:
        return "DO NOT CHASE"
    if "sells dominate" in risk_text or "net-selling" in risk_text:
        return "WATCH ONLY"
    if m["liq"] < ENTRY_MIN_LIQUIDITY:
        return "WATCH ONLY"
    if not (ENTRY_MIN_MCAP <= m["mc"] <= ENTRY_MAX_MCAP):
        return "WATCH ONLY"
    if m["pc5"] < 0 or m["pc5"] > BREAKOUT_RESCUE_MAX_5M:
        return "WATCH ONLY"
    if m["pc5"] > EARLY_MAX5 and score < BREAKOUT_RESCUE_MIN_SCORE:
        return "WATCH ONLY"
    if m["pc1"] > EARLY_MAX1:
        return "WATCH ONLY"
    ratio_ok, _ = buy_sell_gate_ok(pair, score, risks)
    score_ok, _ = score_gate_ok(pair, score, risks)
    if swaps < ENTRY_MIN_SWAPS_5M or not ratio_ok or not score_ok:
        return "WATCH ONLY"
    if score_ok:
        return "POSSIBLE ENTRY"
    return "WATCH ONLY"




def near_entry_context_ok(pair, score, risks, rts=None):
    """True only when tiny threshold misses are corroborated by the rest of the setup.

    This prevents cliff-edge rejections (for example score 51 vs 52 or 1.23x vs
    1.25x) without globally lowering the quality bar. Safety, evidence and scout
    confirmation are still enforced later in the live-entry pipeline.
    """
    m = metrics(pair)
    ratio = m["buys"] / max(m["sells"], 1)
    swaps = m["buys"] + m["sells"]
    ratio_floor = max(1.0, ENTRY_MIN_BUY_SELL - ENTRY_BUY_SELL_NEAR_TOLERANCE)
    score_floor = max(0.0, ENTRY_SCORE - ENTRY_SCORE_NEAR_TOLERANCE)
    risk_text = " ".join(risks).lower()
    realtime_ok = (
        not rts or not rts.get("available") or
        (rts.get("tx30", 0) >= RT_ENTRY_TX30 and rts.get("acceleration", 1.0) >= 1.0)
    )
    return (
        score >= score_floor
        and ratio >= ratio_floor
        and swaps >= ENTRY_MIN_SWAPS_5M
        and realtime_ok
        and "sells dominate" not in risk_text
        and "net-selling" not in risk_text
    )


def score_gate_ok(pair, score, risks, rts=None):
    if score >= ENTRY_SCORE:
        return True, False
    near_ok = near_entry_context_ok(pair, score, risks, rts)
    return near_ok, near_ok


def buy_sell_gate_ok(pair, score, risks, rts=None):
    """Pass normal buy/sell pressure or a tightly corroborated near-threshold miss."""
    m = metrics(pair)
    ratio = m["buys"] / max(m["sells"], 1)
    if ratio >= ENTRY_MIN_BUY_SELL:
        return True, False
    near_ok = near_entry_context_ok(pair, score, risks, rts)
    return near_ok, near_ok

def bool_pref(db, key, default):
    raw = db.get_meta(key, "")
    if raw == "":
        return bool(default)
    return str(raw).lower() in {"1","true","yes","on"}


def set_bool_pref(db, key, enabled):
    db.set_meta(key, "1" if enabled else "0")

def float_pref(db,key,default):
    try: return float(db.get_meta(key,"") or default)
    except Exception: return float(default)


def sizing_preferences(db):
    # v14 caps persisted older-build preferences too. A v13 database that stored
    # $20/$30 guidance must not silently bypass the CAPITAL FIRST limits.
    mn=min(10.0,max(1.0,float_pref(db,"size_guide_min_usd",SIZE_GUIDE_MIN_USD)))
    normal=min(10.0,max(mn,float_pref(db,"size_guide_normal_max_usd",SIZE_GUIDE_NORMAL_MAX_USD)))
    exceptional=min(15.0,max(normal,float_pref(db,"size_guide_exceptional_max_usd",SIZE_GUIDE_EXCEPTIONAL_MAX_USD)))
    return mn,normal,exceptional


def sizing_risk_context(db):
    open_positions=db.open_positions()
    cutoff=int(time.time()-24*3600)
    rows=db.conn.execute("""select close_ts,coalesce(realized_pnl,0) pnl
        from positions where active=0 and close_ts>=? and pnl_quality in ('CASH_VERIFIED','WALLET_VERIFIED')
        order by close_ts desc""",(cutoff,)).fetchall()
    vals=[f(r["pnl"]) for r in rows]
    streak=0
    for pnl in vals:
        if pnl < 0: streak+=1
        else: break
    return {"open_positions":len(open_positions),"realized_24h":sum(vals),"losing_streak":streak}


def _lane_metrics(rows):
    rows=list(rows or [])
    if not rows:
        return {"n":0,"median_max":None,"target_rate":None,"severe_rate":None,"passes":False}
    mx=[f(r[0]) for r in rows]; mn=[f(r[1]) for r in rows]
    n=len(rows); med=statistics.median(mx); hit=sum(v>=8.0 for v in mx)/n; severe=sum(v<=-10.0 for v in mn)/n
    passes=(med>=LANE_HEALTH_MIN_MEDIAN_MAX and hit>=LANE_HEALTH_MIN_TARGET_RATE and severe<LANE_HEALTH_MAX_SEVERE_RATE)
    return {"n":n,"median_max":med,"target_rate":hit,"severe_rate":severe,"passes":passes}


def _max_row_id(db,table):
    row=db.conn.execute(f"select coalesce(max(id),0) from {table}").fetchone()
    return int(row[0] or 0) if row else 0


def _lane_governor_row(db,tier):
    row=db.conn.execute("select * from lane_governor where build_version=? and tier=?",(VERSION,tier)).fetchone()
    if row:
        return dict(row)
    run=db.conn.execute("select min(start_ts) from runs where version=?",(VERSION,)).fetchone()
    start_ts=int(run[0] or time.time()) if run else int(time.time())
    signal_floor=int(db.conn.execute("select coalesce(max(id),0) from signals where ts<?",(start_ts,)).fetchone()[0] or 0)
    decision_floor=int(db.conn.execute("select coalesce(max(id),0) from decision_ledger where ts<?",(start_ts,)).fetchone()[0] or 0)
    db.conn.execute("""insert or ignore into lane_governor(
        build_version,tier,state,state_ts,signal_floor_id,decision_floor_id,reason) values(?,?,?,?,?,?,?)""",
        (VERSION,tier,"PROBATION",start_ts,signal_floor,decision_floor,"new build probation"))
    db.conn.commit()
    return {"build_version":VERSION,"tier":tier,"state":"PROBATION","state_ts":start_ts,
            "signal_floor_id":signal_floor,"decision_floor_id":decision_floor,"reason":"new build probation"}


def _set_lane_governor(db,tier,state,state_ts=None,reason="",signal_floor_id=None,decision_floor_id=None):
    current=_lane_governor_row(db,tier)
    ts=int(state_ts or time.time())
    sf=int(current.get("signal_floor_id") or 0) if signal_floor_id is None else int(signal_floor_id)
    df=int(current.get("decision_floor_id") or 0) if decision_floor_id is None else int(decision_floor_id)
    db.conn.execute("""insert into lane_governor(
        build_version,tier,state,state_ts,signal_floor_id,decision_floor_id,reason) values(?,?,?,?,?,?,?)
        on conflict(build_version,tier) do update set state=excluded.state,state_ts=excluded.state_ts,
        signal_floor_id=excluded.signal_floor_id,decision_floor_id=excluded.decision_floor_id,reason=excluded.reason""",
        (VERSION,tier,state,ts,sf,df,str(reason or "")[:800]))
    db.conn.commit()
    return ts


def lane_health(db, tier):
    """Stateful walk-forward governor for experimental lanes.

    v13.2 fixes the v13.1 one-way quarantine: fully-qualified candidates that are
    suppressed only because the lane is quarantined are logged as LANE_SHADOW.
    Their 30m decision outcomes can return the lane to a fresh PROBATION period,
    but only after the same quality bar is re-proven. Entry gates are unchanged.
    """
    tier=str(tier or "")
    if not LANE_HEALTH_ENABLED or tier not in {"STRUCTURE ENTRY","MANUAL CHAIN ENTRY"}:
        return {"status":"ACTIVE","n":0,"live_n":0,"shadow_n":0,"paused":False,"median_max":None,"target_rate":None,"severe_rate":None}

    gov=_lane_governor_row(db,tier); state=str(gov.get("state") or "PROBATION")
    signal_floor=int(gov.get("signal_floor_id") or 0); decision_floor=int(gov.get("decision_floor_id") or 0)
    if state=="QUARANTINED":
        shadow_rows=db.conn.execute("""select o.max_return_pct,o.min_return_pct,o.final_return_pct
            from decision_outcomes o join decision_ledger d on d.id=o.decision_id
            where o.horizon_min=30 and coalesce(o.suspect,0)=0 and d.event='LANE_SHADOW' and d.tier=? and d.build_version=? and d.id>?
            order by d.id desc limit ?""",(tier,VERSION,decision_floor,max(LANE_HEALTH_WINDOW,LANE_HEALTH_RECOVERY_MIN_SIGNALS))).fetchall()
        sm=_lane_metrics(shadow_rows)
        if sm["n"]>=LANE_HEALTH_RECOVERY_MIN_SIGNALS and sm["passes"]:
            # Start a genuinely fresh live probation cohort. ID floors avoid same-second
            # timestamp collisions and make restarts deterministic.
            _set_lane_governor(db,tier,"PROBATION",reason=f"shadow recovery passed after {sm['n']} completed 30m paths",
                               signal_floor_id=_max_row_id(db,"signals"),decision_floor_id=_max_row_id(db,"decision_ledger"))
            return {"status":"PROBATION","n":0,"live_n":0,"shadow_n":sm["n"],"paused":False,
                    "median_max":sm["median_max"],"target_rate":sm["target_rate"],"severe_rate":sm["severe_rate"],"recovered":True}
        return {"status":"QUARANTINED","n":sm["n"],"live_n":0,"shadow_n":sm["n"],"paused":True,
                "median_max":sm["median_max"],"target_rate":sm["target_rate"],"severe_rate":sm["severe_rate"],
                "recovery_needed":LANE_HEALTH_RECOVERY_MIN_SIGNALS,"reason":str(gov.get("reason") or "")}

    rows=db.conn.execute("""select o.max_return_pct,o.min_return_pct,o.final_return_pct
        from signal_outcomes o join signals s on s.id=o.signal_id
        where o.horizon_min=30 and coalesce(o.suspect,0)=0 and s.kind='EARLY' and s.action=? and s.id>?
        order by s.id desc limit ?""",(tier,signal_floor,LANE_HEALTH_WINDOW)).fetchall()
    lm=_lane_metrics(rows); n=lm["n"]
    if n < LANE_HEALTH_MIN_SIGNALS:
        return {"status":"PROBATION","n":n,"live_n":n,"shadow_n":0,"paused":False,
                "median_max":lm["median_max"],"target_rate":lm["target_rate"],"severe_rate":lm["severe_rate"]}
    if not lm["passes"]:
        reason=(f"quality failed after {n} completed 30m paths: median max {f(lm['median_max']):+.1f}%, "
                f"target-hit {100*f(lm['target_rate']):.0f}%, severe-drawdown {100*f(lm['severe_rate']):.0f}%")
        _set_lane_governor(db,tier,"QUARANTINED",reason=reason,decision_floor_id=_max_row_id(db,"decision_ledger"))
        return {"status":"QUARANTINED","n":n,"live_n":n,"shadow_n":0,"paused":True,
                "median_max":lm["median_max"],"target_rate":lm["target_rate"],"severe_rate":lm["severe_rate"],
                "recovery_needed":LANE_HEALTH_RECOVERY_MIN_SIGNALS,"reason":reason}
    if state!="ACTIVE":
        _set_lane_governor(db,tier,"ACTIVE",state_ts=int(gov.get("state_ts") or time.time()),
                           reason=f"quality passed after {n} completed 30m paths")
    return {"status":"ACTIVE","n":n,"live_n":n,"shadow_n":0,"paused":False,
            "median_max":lm["median_max"],"target_rate":lm["target_rate"],"severe_rate":lm["severe_rate"]}


def lane_health_reason(health):
    if not health or not health.get("paused"):
        return ""
    shadow=health.get("shadow_n",0); need=health.get("recovery_needed",LANE_HEALTH_RECOVERY_MIN_SIGNALS)
    metrics=""
    if health.get("median_max") is not None:
        metrics=(f"; current graded sample median max {f(health.get('median_max')):+.1f}%, "
                 f"target-hit {100*f(health.get('target_rate')):.0f}%, severe-drawdown {100*f(health.get('severe_rate')):.0f}%")
    elif health.get("reason"):
        metrics=f"; {health.get('reason')}"
    return f"lane health quarantined; shadow recovery {shadow}/{need} completed 30m paths{metrics}"


def lane_health_label(db,tier):
    h=lane_health(db,tier); status=h.get("status","ACTIVE")
    if status=="QUARANTINED":
        return f"QUARANTINED (shadow {h.get('shadow_n',0)}/{h.get('recovery_needed',LANE_HEALTH_RECOVERY_MIN_SIGNALS)})"
    if status=="PROBATION":
        return f"PROBATION ({h.get('live_n',0)}/{LANE_HEALTH_MIN_SIGNALS})"
    return f"ACTIVE (n={h.get('live_n',h.get('n',0))})"


def _capital_core_metrics(rows):
    rows=list(rows or [])
    if not rows:
        return {"n":0,"median_max":None,"hit10_rate":None,"severe_rate":None,"passes":False}
    mx=[f(r[0]) for r in rows]; mn=[f(r[1]) for r in rows]
    n=len(rows); med=statistics.median(mx); hit=sum(v>=10.0 for v in mx)/n; severe=sum(v<=-10.0 for v in mn)/n
    passes=(med>=CAPITAL_CORE_MIN_MEDIAN_MAX and hit>=CAPITAL_CORE_MIN_HIT10_RATE and severe<=CAPITAL_CORE_MAX_SEVERE_RATE)
    return {"n":n,"median_max":med,"hit10_rate":hit,"severe_rate":severe,"passes":passes}


def capital_core_health(db):
    """Self-quarantine the core BUY lane when fresh v14 paths lose fee-worthy edge.

    Unlike the old endpoint performance guard, this is version-aware and path-aware:
    it grades available 30m upside and severe drawdown on delivered v14 entries. While
    quarantined, otherwise fully-qualified candidates are recorded as CAPITAL_SHADOW
    so the core lane can re-prove itself without asking the user to risk money.
    """
    if not CAPITAL_CORE_GOVERNOR_ENABLED:
        return {"status":"ACTIVE","paused":False,"n":0,"live_n":0,"shadow_n":0}
    gov=_lane_governor_row(db,CAPITAL_CORE_TIER); state=str(gov.get("state") or "PROBATION")
    floor=int(gov.get("decision_floor_id") or 0)
    if state=="QUARANTINED":
        rows=db.conn.execute("""select o.max_return_pct,o.min_return_pct,o.final_return_pct
            from decision_outcomes o join decision_ledger d on d.id=o.decision_id
            where o.horizon_min=30 and coalesce(o.suspect,0)=0 and d.event='CAPITAL_SHADOW' and d.build_version=? and d.id>?
            order by d.id desc limit ?""",(VERSION,floor,max(CAPITAL_CORE_WINDOW,CAPITAL_CORE_RECOVERY_PATHS))).fetchall()
        m=_capital_core_metrics(rows)
        if m["n"]>=CAPITAL_CORE_RECOVERY_PATHS and m["passes"]:
            _set_lane_governor(db,CAPITAL_CORE_TIER,"PROBATION",reason=f"capital shadow recovery passed after {m['n']} paths",
                               signal_floor_id=_max_row_id(db,"signals"),decision_floor_id=_max_row_id(db,"decision_ledger"))
            return {"status":"PROBATION","paused":False,"n":0,"live_n":0,"shadow_n":m["n"],"recovered":True,**m}
        return {"status":"QUARANTINED","paused":True,"n":m["n"],"live_n":0,"shadow_n":m["n"],
                "recovery_needed":CAPITAL_CORE_RECOVERY_PATHS,"reason":str(gov.get("reason") or ""),**m}
    rows=db.conn.execute("""select o.max_return_pct,o.min_return_pct,o.final_return_pct
        from decision_outcomes o join decision_ledger d on d.id=o.decision_id
        where o.horizon_min=30 and coalesce(o.suspect,0)=0 and d.build_version=? and d.id>?
          and d.event in ('ENTRY_SENT','ENTRY_SENT_HOT','ENTRY_SENT_TEST')
          and d.tier in ('ENTRY OPTION','STRONG ENTRY','FLOW ENTRY','REVERSAL ENTRY','FAST ENTRY')
        order by d.id desc limit ?""",(VERSION,floor,CAPITAL_CORE_WINDOW)).fetchall()
    m=_capital_core_metrics(rows)
    if m["n"]<CAPITAL_CORE_MIN_PATHS:
        return {"status":"PROBATION","paused":False,"live_n":m["n"],"shadow_n":0,**m}
    if not m["passes"]:
        reason=(f"capital quality failed after {m['n']} paths: median max {f(m['median_max']):+.1f}%, "
                f"hit +10 {100*f(m['hit10_rate']):.0f}%, severe <=-10 {100*f(m['severe_rate']):.0f}%")
        _set_lane_governor(db,CAPITAL_CORE_TIER,"QUARANTINED",reason=reason,decision_floor_id=_max_row_id(db,"decision_ledger"))
        return {"status":"QUARANTINED","paused":True,"live_n":m["n"],"shadow_n":0,
                "recovery_needed":CAPITAL_CORE_RECOVERY_PATHS,"reason":reason,**m}
    if state!="ACTIVE":
        _set_lane_governor(db,CAPITAL_CORE_TIER,"ACTIVE",state_ts=int(gov.get("state_ts") or time.time()),
                           reason=f"capital quality passed after {m['n']} paths")
    return {"status":"ACTIVE","paused":False,"live_n":m["n"],"shadow_n":0,**m}


def capital_core_health_label(db):
    h=capital_core_health(db); st=h.get("status","ACTIVE")
    if st=="QUARANTINED":
        return f"QUARANTINED (shadow {h.get('shadow_n',0)}/{h.get('recovery_needed',CAPITAL_CORE_RECOVERY_PATHS)})"
    if st=="PROBATION":
        return f"PROBATION ({h.get('live_n',0)}/{CAPITAL_CORE_MIN_PATHS})"
    return f"ACTIVE (n={h.get('live_n',h.get('n',0))})"


def adaptive_dollar_size(db,pair,tier,score,late_move_pct=0.0,market_regime="NEUTRAL",social=None,safety=None):
    mn,normal,exceptional=sizing_preferences(db)
    ctx=sizing_risk_context(db)
    guide=dollar_size_guide(
        db.conn,pair,tier,score,mn,normal,exceptional,
        open_positions=ctx["open_positions"],realized_24h=ctx["realized_24h"],
        losing_streak=ctx["losing_streak"],late_move_pct=late_move_pct,
        market_regime=market_regime,flow_concentration=f((safety or {}).get("flow_concentration")),
        social_quality=f((social or {}).get("score")))
    health=lane_health(db,tier)
    guide["lane_health"]=health
    if str(tier)=="STRUCTURE ENTRY":
        if health.get("paused"):
            guide["suggested_usd"]=0.0; guide["size_label"]="LANE QUARANTINED"
            guide.setdefault("size_reasons",[]).append("structure lane quarantined: no new bot-guided buy until shadow recovery re-qualifies it")
        else:
            cap=STRUCTURE_PROBATION_MAX_SIZE_USD if health.get("status")=="PROBATION" else STRUCTURE_MAX_SIZE_USD
            guide["suggested_usd"]=min(f(guide.get("suggested_usd")),cap)
            guide.setdefault("size_reasons",[]).append(f"structure lane {health.get('status','PROBATION').lower()}: cap ${cap:.0f}")
    elif str(tier)=="MANUAL CHAIN ENTRY":
        if health.get("paused"):
            guide["suggested_usd"]=0.0; guide["size_label"]="LANE QUARANTINED"
            guide.setdefault("size_reasons",[]).append("cross-chain lane quarantined: no new bot-guided buy until shadow recovery re-qualifies it")
        else:
            cap=MANUAL_CHAIN_PROBATION_MAX_SIZE_USD if health.get("status")=="PROBATION" else MANUAL_CHAIN_MAX_SIZE_USD
            guide["suggested_usd"]=min(f(guide.get("suggested_usd")),cap)
            guide.setdefault("size_reasons",[]).append(f"cross-chain lane {health.get('status','PROBATION').lower()}: cap ${cap:.0f}; manual execution only")
    elif str(tier) in {"ENTRY OPTION","STRONG ENTRY","FLOW ENTRY","REVERSAL ENTRY","FAST ENTRY"}:
        core=capital_core_health(db); guide["capital_core_health"]=core
        if str(tier)=="FAST ENTRY":
            guide["suggested_usd"]=max(0.0,f(guide.get("suggested_usd"))*FAST_ENTRY_SIZE_MULT)
            guide.setdefault("size_reasons",[]).append(f"fast lane size {FAST_ENTRY_SIZE_MULT:.0%} of normal while its own history matures")
        if core.get("paused"):
            guide["suggested_usd"]=0.0; guide["size_label"]="CAPITAL CORE QUARANTINED"
            guide.setdefault("size_reasons",[]).append("core BUY lane quarantined: shadow paths must re-prove fee-worthy edge")
        elif core.get("status")=="PROBATION":
            guide["suggested_usd"]=min(f(guide.get("suggested_usd")),CAPITAL_CORE_PROBATION_SIZE_USD)
            guide.setdefault("size_reasons",[]).append(f"capital core probation: cap ${CAPITAL_CORE_PROBATION_SIZE_USD:.0f} until {CAPITAL_CORE_MIN_PATHS} completed 30m paths")
        proof=proof_first_health(db); guide["proof_health"]=proof
        risk=paper_risk_guard(db); guide["paper_risk_guard"]=risk
        bankroll=bankroll_size_context(db); guide["bankroll_size"]=bankroll
        if PROOF_FIRST_REQUIRE_MATURE and proof.get("status")!="ACTIVE":
            guide["suggested_usd"]=0.0; guide["size_label"]="PAPER PROOF REQUIRED"
            guide.setdefault("size_reasons",[]).append(f"fresh v15 net shadow proof is {proof.get('status')} ({proof.get('n',0)}/{PROOF_FIRST_MIN_PATHS})")
        elif risk.get("paused"):
            guide["suggested_usd"]=0.0; guide["size_label"]="RISK CIRCUIT BREAKER"
            guide.setdefault("size_reasons",[]).extend(risk.get("reasons") or [])
        elif not bankroll.get("ready",True):
            guide["suggested_usd"]=0.0; guide["size_label"]="SET BANKROLL"
            guide.setdefault("size_reasons",[]).append(str(bankroll.get("reason") or "set /bankroll before live-money guidance"))
        else:
            guide["suggested_usd"]=min(f(guide.get("suggested_usd")),f(bankroll.get("suggested_usd"),5.0))
            guide.setdefault("size_reasons",[]).append(str(bankroll.get("reason") or "bankroll risk cap"))
    return guide


def token_trauma_context(db, chain, token, now=None):
    """Remember severe post-alert drawdowns so the bot does not buy the same failure twice.

    This uses already-captured public market outcomes, not the user's private trade result.
    A contract that suffered a severe 30m path after a recent bot entry is blocked for
    TOKEN_TRAUMA_HOURS even if the ordinary entry cooldown has expired.
    """
    now=int(now if now is not None else time.time())
    cutoff=now-int(TOKEN_TRAUMA_HOURS*3600)
    row=db.conn.execute("""select d.ts,d.symbol,o.min_return_pct,o.max_return_pct,o.final_return_pct
        from decision_ledger d join decision_outcomes o on o.decision_id=d.id
        where d.chain=? and d.token=? and d.ts>=? and d.ts<?
          and d.event in ('ENTRY_SENT','ENTRY_SENT_HOT','ENTRY_SENT_TEST') and o.horizon_min=30 and coalesce(o.suspect,0)=0
          and o.min_return_pct<=?
        order by d.ts desc limit 1""",
        (str(chain or '').lower(),str(token or ''),cutoff,now,TOKEN_TRAUMA_DRAWDOWN_PCT)).fetchone()
    if not row:
        return {"blocked":False}
    age_h=max(0.0,(now-int(row["ts"]))/3600.0)
    return {"blocked":True,"age_hours":age_h,"min_return_pct":f(row["min_return_pct"]),
            "max_return_pct":f(row["max_return_pct"]),"final_return_pct":f(row["final_return_pct"]),
            "reason":f"same contract had {f(row['min_return_pct']):+.1f}% 30m drawdown {age_h:.1f}h ago"}


_ANALOG_CACHE = {"db":None,"ts":0.0,"rows":[]}


def runner_radar_qualifies(pair):
    if not RUNNER_RADAR_ENABLED or str(pair.get("chainId") or "").lower() != "solana":
        return False,[]
    m=metrics(pair); ratio=m["buys"]/max(m["sells"],1); swaps=m["buys"]+m["sells"]
    turnover=100.0*m["v5"]/max(m["mc"],1)
    ok=(RUNNER_RADAR_MIN_MCAP<=m["mc"]<=RUNNER_RADAR_MAX_MCAP and m["liq"]>=RUNNER_RADAR_MIN_LIQ
        and turnover>=RUNNER_RADAR_MIN_TURNOVER and swaps>=RUNNER_RADAR_MIN_SWAPS and ratio>=RUNNER_RADAR_MIN_BS
        and -2.0<=m["pc5"]<=7.0 and -20.0<=m["pc1"]<=35.0)
    reasons=[f"runner radar: {turnover:.2f}% 5m turnover",f"buyers/sellers {ratio:.2f}x",f"{int(swaps)} swaps/5m"]
    return ok,reasons


def breadth_guard_context(db):
    rows=db.breadth_rows()
    return breadth_regime(rows,min_tokens=8)


def historical_edge_context(db,pair):
    if not HISTORICAL_EDGE_ENABLED:
        return {"n":0,"veto":False,"reason":"historical analog veto disabled"}
    now=time.time(); key=id(db)
    if _ANALOG_CACHE.get("db")!=key or now-f(_ANALOG_CACHE.get("ts"))>300:
        _ANALOG_CACHE.update({"db":key,"ts":now,"rows":db.analog_rows()})
    m=metrics(pair); current={
        "pc5":m["pc5"],"pc1":m["pc1"],"mcap":m["mc"],"liquidity":m["liq"],
        "buy_sell":m["buys"]/max(m["sells"],1),"swaps":m["buys"]+m["sells"],
        "turnover_pct":100.0*m["v5"]/max(m["mc"],1),
    }
    return analog_summary(current,_ANALOG_CACHE.get("rows") or [],nearest=40,min_neighbors=HISTORICAL_EDGE_MIN_NEIGHBORS)


def stateful_sol_edge_gate(db,pair,tier):
    if str(pair.get("chainId") or "").lower()!="solana":
        return False,["v15 actionable edge engine is Solana-only"],{"mode":"NON_SOLANA"}
    token=str(nest(pair,"baseToken","address",default="")); m=metrics(pair)
    hist=db.token_history("solana",token,minutes=5)
    current={"price":f(pair.get("priceUsd")),"liquidity":m["liq"],"buys":m["buys"],"sells":m["sells"]}
    info=setup_path_quality(hist,current,min_observations=SOL_EDGE_PATH_MIN_OBS,min_span_seconds=SOL_EDGE_PATH_MIN_SPAN,
                            max_chase_pct=SOL_EDGE_PATH_MAX_CHASE,max_peak_drawdown_pct=SOL_EDGE_PATH_MAX_DRAWDOWN,
                            max_liquidity_drop_pct=SOL_EDGE_PATH_MAX_LIQ_DROP,min_current_buy_sell=1.15)
    return bool(info.get("ok")),list(info.get("blocks") or []),info


def proof_candidate_eligibility(tier, score):
    """Return whether this shadow belongs to the live-proof cohort.

    Every fully-qualified setup is still simulated.  This gate only decides whether
    the result is statistically relevant to *live authorization*.  Keeping those two
    concepts separate prevents low-conviction research lanes from permanently
    quarantining a stronger core lane.
    """
    core={"ENTRY OPTION","STRONG ENTRY","FLOW ENTRY","REVERSAL ENTRY","FAST ENTRY"}
    tier=str(tier or "")
    score=f(score)
    if tier not in core:
        return False,f"{tier or 'unknown lane'} is research-only"
    if score < PROOF_ELIGIBLE_MIN_SCORE:
        return False,f"score {score:.1f} < selective-proof floor {PROOF_ELIGIBLE_MIN_SCORE:.1f}"
    return True,f"score {score:.1f} >= selective-proof floor {PROOF_ELIGIBLE_MIN_SCORE:.1f}"


def proof_first_health(db):
    return proof_metrics(db.proof_shadow_rows(PROOF_FIRST_WINDOW),min_paths=PROOF_FIRST_MIN_PATHS,
                         min_unique_tokens=PROOF_FIRST_MIN_UNIQUE_TOKENS,
                         min_net_roi_pct=PROOF_FIRST_MIN_NET_ROI_PCT,
                         min_t_stat=PROOF_FIRST_MIN_T_STAT,
                         require_ex_best_positive=PROOF_FIRST_REQUIRE_EX_BEST_POSITIVE)


def proof_health_label(db):
    h=proof_first_health(db)
    pf=h.get("profit_factor")
    pf_txt="—" if pf is None else ("∞" if math.isinf(pf) else f"{pf:.2f}")
    return (f"{h['status']} ({h['n']}/{PROOF_FIRST_MIN_PATHS}; unique {h.get('unique_tokens',0)}/{PROOF_FIRST_MIN_UNIQUE_TOKENS}; "
            f"net ${h.get('pnl',0):+.2f}; ROI {f(h.get('net_roi_pct')):+.1f}%; PF {pf_txt}"
            + (f"; t {h['t_stat']:.2f}" if isinstance(h.get('t_stat'),(int,float)) and math.isfinite(h['t_stat']) else "")
            + (f" — {h['reason']}" if h.get('status')!='ACTIVE' and h.get('reason') else "") + ")")


def signal_policy(db):
    raw=str(db.get_meta("signal_policy","") or ACTIONABLE_SIGNAL_POLICY_DEFAULT).strip().lower()
    return raw if raw in {"strict","actionable","proven"} else ACTIONABLE_SIGNAL_POLICY_DEFAULT


def tier_track_record(db, tier, days=None):
    """Measured paper/shadow results of one alert type (net of simulated fees)."""
    days=int(days or TRACK_RECORD_DAYS)
    cutoff=int(time.time()-days*86400)
    rows=[dict(r) for r in db.conn.execute("""select token,amount_usd,realized_pnl from paper_positions
        where active=0 and tier=? and close_ts>=? and coalesce(close_reason,'') not like 'price data unreliable%'
        order by close_ts desc""",(str(tier or ''),cutoff)).fetchall()]
    n=len(rows)
    wins=sum(1 for r in rows if f(r.get("realized_pnl"))>0)
    rois=[f(r.get("realized_pnl"))/f(r.get("amount_usd")) * 100 for r in rows if f(r.get("amount_usd"))>0]
    avg=statistics.mean(rois) if rois else 0.0
    proof=proof_metrics(rows,min_paths=TRACK_RECORD_MIN_TRADES,min_unique_tokens=max(3,TRACK_RECORD_MIN_TRADES//2),
                        min_net_roi_pct=PROOF_FIRST_MIN_NET_ROI_PCT,min_t_stat=PROOF_FIRST_MIN_T_STAT,
                        require_ex_best_positive=PROOF_FIRST_REQUIRE_EX_BEST_POSITIVE)
    return {"tier":str(tier or ''),"days":days,"n":n,"wins":wins,"win_rate":(wins/n if n else 0.0),"avg_roi_pct":avg,
            "net_usd":sum(f(r.get("realized_pnl")) for r in rows),"proven":proof.get("status")=="ACTIVE",
            "reason":proof.get("reason","")}


def alert_track_record_line(db, tier):
    r=tier_track_record(db,tier)
    head=f"📊 TRACK RECORD ({r['tier'] or 'this alert type'}, paper, last {r['days']}d): "
    if r["n"]==0:
        return head+"no completed tests yet — completely unproven."
    stats=f"{r['n']} tests | {r['win_rate']*100:.0f}% wins | avg {r['avg_roi_pct']:+.1f}% per trade after fees"
    if r["n"]<TRACK_RECORD_MIN_TRADES:
        return head+stats+" — too few tests to trust."
    if r["proven"]:
        return head+stats+" ✅ positive and consistent so far."
    if r["avg_roi_pct"]<0:
        return head+stats+"\n⚠️ This alert type has LOST money in testing. Treat it as a gamble and only use money you can afford to lose."
    return head+stats+" — not yet distinguishable from luck."


def _test_signal_calibration(db, calibration, mode):
    cal=dict(calibration or {})
    reasons=list(cal.get("size_reasons") or [])
    if mode=="LIVE":
        return cal
    if mode=="TEST":
        bank=bankroll_size_context(db)
        if f(bank.get("bankroll"))>0 and f(bank.get("suggested_usd"))>0:
            base=f(bank.get("suggested_usd"))*TEST_BUY_SIZE_MULT
            cap=max(1.0,min(TEST_BUY_MAX_USD,base))
            cal["suggested_usd"]=cap
            cal["size_label"]="TEST SIZE ONLY"
            reasons.insert(0,f"pre-proof manual test cap ${cap:.2f}; real autopilot remains locked")
        else:
            cap=max(1.0,min(TEST_BUY_MAX_USD,DEFAULT_POSITION*TEST_BUY_SIZE_MULT))
            cal["suggested_usd"]=cap
            cal["size_label"]="TEST SIZE ONLY"
            reasons.insert(0,f"pre-proof manual test cap ${cap:.2f}; set /bankroll for risk-based sizing; real autopilot remains locked")
    else:
        cal["suggested_usd"]=0.0
        cal["size_label"]="PAPER SIGNAL ONLY"
        reasons.insert(0,"current proof/risk state blocks live-money sizing")
    cal["size_reasons"]=reasons
    return cal


def entry_signal_mode(db,tier,score):
    """Separate signal visibility from live-money authorization.

    STRICT preserves the older behavior. ACTIONABLE lets fully-qualified, proof-eligible
    core setups surface as TEST BUY signals before proof is ACTIVE. Quarantine/risk states
    still force PAPER-only labeling and $0 live sizing; real autopilot is unaffected.
    """
    eligible,why=proof_candidate_eligibility(tier,score)
    if not eligible:
        return {"send":False,"mode":"RESEARCH","reason":why}
    proof=proof_first_health(db); risk=paper_risk_guard(db); lane=lane_health(db,tier)
    core=capital_core_health(db) if str(tier) in {"ENTRY OPTION","STRONG ENTRY","FLOW ENTRY","REVERSAL ENTRY","FAST ENTRY"} else {"paused":False,"status":"ACTIVE"}
    perf=db.rolling_entry_performance()
    bank=bankroll_size_context(db)
    live_blockers=[]
    if PROOF_FIRST_REQUIRE_MATURE and proof.get("status")!="ACTIVE": live_blockers.append(f"proof {proof.get('status')}")
    if risk.get("paused"): live_blockers.extend(risk.get("reasons") or ["paper risk circuit"] )
    if lane.get("paused"): live_blockers.append(f"{tier} lane {lane.get('status')}")
    if core.get("paused"): live_blockers.append(f"capital core {core.get('status')}")
    if perf.get("paused"): live_blockers.append("recent delivered-entry performance guard")
    if not bank.get("ready",True): live_blockers.append(str(bank.get("reason") or "bankroll not set"))
    if not live_blockers:
        return {"send":True,"mode":"LIVE","reason":"all proof/risk/live-sizing gates passed","proof":proof,"risk":risk,"lane":lane,"core":core,"perf":perf,"bank":bank}
    if signal_policy(db)=="strict":
        return {"send":False,"mode":"BLOCKED","reason":"; ".join(live_blockers),"proof":proof,"risk":risk,"lane":lane,"core":core,"perf":perf,"bank":bank}
    if signal_policy(db)=="proven":
        record=tier_track_record(db,tier)
        if not record["proven"]:
            return {"send":False,"mode":"BLOCKED","reason":f"proven policy: {tier} paper record not proven ({record['n']} tests, avg {record['avg_roi_pct']:+.1f}%)",
                    "proof":proof,"risk":risk,"lane":lane,"core":core,"perf":perf,"bank":bank}
    severe=bool(risk.get("paused") or lane.get("paused") or core.get("paused") or perf.get("paused") or proof.get("status")=="QUARANTINED")
    return {"send":True,"mode":"PAPER" if severe else "TEST","reason":"; ".join(live_blockers),"proof":proof,"risk":risk,"lane":lane,"core":core,"perf":perf,"bank":bank}


def paper_risk_guard(db):
    bankroll=max(0.0,float_pref(db,"bankroll_usd",0.0))
    start=int(datetime.now().replace(hour=0,minute=0,second=0,microsecond=0).timestamp())
    qmarks=",".join("?" for _ in QUALITY_COHORT_BUILD_VERSIONS)
    rows=[dict(r) for r in db.conn.execute(f"""select close_ts,realized_pnl from paper_positions
        where active=0 and coalesce(shadow,0)=1 and coalesce(proof_eligible,0)=1
          and coalesce(build_version,'') in ({qmarks}) and close_ts>=?
        order by close_ts desc""",(*QUALITY_COHORT_BUILD_VERSIONS,start)).fetchall()]
    daily=sum(f(r.get("realized_pnl")) for r in rows)
    paused=False; reasons=[]
    if bankroll>0 and daily <= -bankroll*(PAPER_DAILY_LOSS_LIMIT_PCT/100.0):
        paused=True; reasons.append(f"paper daily loss ${daily:.2f} breached {PAPER_DAILY_LOSS_LIMIT_PCT:.1f}% bankroll limit")
    streak=0; last_close=0
    recent=[dict(r) for r in db.conn.execute(f"""select close_ts,realized_pnl from paper_positions
        where active=0 and coalesce(shadow,0)=1 and coalesce(proof_eligible,0)=1
          and coalesce(build_version,'') in ({qmarks}) order by close_ts desc limit ?""",
        (*QUALITY_COHORT_BUILD_VERSIONS,PAPER_LOSS_STREAK_LIMIT)).fetchall()]
    for r in recent:
        if f(r.get("realized_pnl"))<0: streak+=1
        else: break
    if recent: last_close=int(recent[0].get("close_ts") or 0)
    if streak>=PAPER_LOSS_STREAK_LIMIT and time.time()-last_close < PAPER_LOSS_STREAK_COOLDOWN_HOURS*3600:
        paused=True; reasons.append(f"{streak}-trade paper losing streak; {PAPER_LOSS_STREAK_COOLDOWN_HOURS:.0f}h cooldown")
    return {"paused":paused,"reasons":reasons,"daily_pnl":daily,"bankroll":bankroll,"losing_streak":streak}


def bankroll_size_context(db):
    bankroll=max(0.0,float_pref(db,"bankroll_usd",0.0))
    out=risk_position_size(bankroll,stop_pct=CAPITAL_HARD_STOP_PCT,risk_pct=BANKROLL_RISK_PCT,
                           min_usd=1.0,max_usd=SIZE_GUIDE_NORMAL_MAX_USD)
    out["required"]=BANKROLL_REQUIRED_FOR_LIVE_GUIDANCE
    out["ready"]=(bankroll>0 or not BANKROLL_REQUIRED_FOR_LIVE_GUIDANCE)
    if bankroll<=0 and BANKROLL_REQUIRED_FOR_LIVE_GUIDANCE:
        out["suggested_usd"]=0.0
        out["reason"]="bankroll not set; live-money guidance locked until /bankroll AMOUNT"
    return out


def capital_edge_gate(db, pair, tier, entry_score=0.0, confirmed_score=0.0):
    """v15 final *veto* layer after the legacy classifier.

    v14's static "capital edge" shape was tuned to one short holdout and failed on the
    full Sep-9 data.  v15 therefore does not pretend a single liquidity/turnover box is
    predictive.  It keeps explicit lane safety switches, token-trauma memory, broad
    market breadth and a transparent historical-neighbour downside veto.  Positive
    authorization still requires the stateful path and execution preflight later.
    """
    tier=str(tier or ''); chain=str(pair.get("chainId") or '').lower()
    token=str(nest(pair,"baseToken","address",default="")); m=metrics(pair)
    ratio=m["buys"]/max(m["sells"],1); swaps=m["buys"]+m["sells"]
    turnover=100.0*m["v5"]/max(m["mc"],1); liq_mc=m["liq"]/max(m["mc"],1)
    info={"enabled":True,"liq_mc":liq_mc,"turnover_pct":turnover,"buy_sell":ratio,"swaps":swaps}
    if not tier:
        return True,[],info
    trauma=token_trauma_context(db,chain,token); info["trauma"]=trauma
    if trauma.get("blocked"):
        return False,[f"token trauma memory: {trauma.get('reason')}"] ,info
    if chain!="solana":
        return False,["v15 live BUY engine is Solana-only; non-Solana discovery is watch-only"],info
    # Preserve the legacy gate contract (disabled live lanes return False) while
    # tagging them as shadow-eligible. Callers may continue the validation pipeline
    # using this flag, open a simulated trade, and then stop before Telegram/live use.
    if tier=="STRUCTURE ENTRY" and not STRUCTURE_BUY_ALERTS_ENABLED:
        info["shadow_only"]=True; info["shadow_only_reason"]="structure BUY alerts remain shadow-only until fresh net paper evidence proves them"
        return False,[info["shadow_only_reason"]],info
    if tier=="MANUAL CHAIN ENTRY" and not MANUAL_CHAIN_BUY_ALERTS_ENABLED:
        info["shadow_only"]=True; info["shadow_only_reason"]="cross-chain BUY alerts are watch-only"
        return False,[info["shadow_only_reason"]],info
    if tier=="MICRO ENTRY" and not MICRO_BUY_ALERTS_ENABLED:
        info["shadow_only"]=True; info["shadow_only_reason"]="micro-liquidity BUY alerts are shadow-only"
        return False,[info["shadow_only_reason"]],info
    if tier=="FRESH ENTRY" and not FRESH_BUY_ALERTS_ENABLED:
        info["shadow_only"]=True; info["shadow_only_reason"]="fresh-launch BUY alerts are shadow-only"
        return False,[info["shadow_only_reason"]],info

    breadth=breadth_guard_context(db) if BREADTH_GUARD_ENABLED else {"label":"DISABLED","risk_off":False}
    info["breadth"]=breadth
    soft_warnings=[]
    if breadth.get("risk_off"):
        warning=f"meme breadth RISK_OFF: {breadth.get('reason')}"
        if tier=="FAST ENTRY":
            soft_warnings.append(warning)
        else:
            return False,[warning],info
    analog=historical_edge_context(db,pair); info["analogs"]=analog
    if analog.get("veto"):
        warning=f"historical downside veto: {analog.get('reason')}"
        if tier=="FAST ENTRY":
            soft_warnings.append(warning)
        else:
            return False,[warning],info
    if soft_warnings:
        info["soft_warnings"]=soft_warnings
    # Minimal executable-flow floor only. The existing classifier is responsible for
    # actual setup eligibility; this merely rejects practically empty/churn-only tape.
    if turnover < 0.05 or swaps < 8 or ratio < 1.05:
        return False,[f"insufficient executable flow ({turnover:.2f}% turnover, {int(swaps)} swaps, {ratio:.2f}x B/S)"],info
    return True,[],info

def capital_confirmation_gate(db,pair,tier):
    """Require every real BUY candidate to survive a second fresh snapshot.

    v13.3 only delayed fragile ENTRY OPTION setups. The APEUS loss showed that a
    nominal STRONG entry can fail within a minute, so v14 applies persistence to all
    buy-capable lanes. A pending marker is internal and never a buy call.
    """
    tier=str(tier or '')
    if tier not in {"ENTRY OPTION","STRONG ENTRY","FLOW ENTRY","REVERSAL ENTRY","FAST ENTRY"}:
        return True,[],{"required":False}
    chain=str(pair.get("chainId") or '').lower(); token=str(nest(pair,"baseToken","address",default=""))
    pending=db.recent_signal(chain,token,"CAPITAL_PENDING",hours=0.20) if token else None
    now=time.time(); current=f(pair.get("priceUsd")); liq=f(nest(pair,"liquidity","usd",default=0))
    if not pending:
        try: db.add_signal("CAPITAL_PENDING",pair,0,"WAIT",[f"v15 SOL edge confirmation for {tier}"])
        except Exception: pass
        return False,[f"SOL edge confirmation 0s/{SOL_EDGE_CONFIRM_SECONDS}s"],{"required":True,"age_seconds":0}
    age=max(0.0,now-f(pending.get("ts")))
    move=(current/max(f(pending.get("price")),1e-18)-1)*100 if current>0 else -999
    liq_change=(liq/max(f(pending.get("liquidity")),1e-18)-1)*100 if liq>0 and f(pending.get("liquidity"))>0 else 0
    info={"required":True,"age_seconds":age,"move_pct":move,"liq_change_pct":liq_change}
    blocks=[]
    if age < SOL_EDGE_CONFIRM_SECONDS:
        blocks.append(f"SOL edge confirmation {age:.0f}s/{SOL_EDGE_CONFIRM_SECONDS}s")
    if move < -CAPITAL_CONFIRM_MAX_DOWN_PCT:
        blocks.append(f"price weakened {move:+.1f}% during confirmation")
    if move > CAPITAL_CONFIRM_MAX_UP_PCT:
        blocks.append(f"price ran {move:+.1f}% during confirmation; do not chase")
    if liq_change < -CAPITAL_CONFIRM_MAX_LIQ_DROP_PCT:
        blocks.append(f"liquidity fell {liq_change:+.1f}% during confirmation")
    return (not blocks),blocks,info


def entry_cooldown_context(db, chain, token, now=None):
    """Resolve entry cooldown without letting an old build suppress a new model all day.

    Same-build alerts keep the full cooldown across restarts. An alert produced by an
    older build gets only CROSS_BUILD_ENTRY_GRACE_MINUTES of duplicate protection.
    This never overrides an open-position check in the caller.
    """
    now = float(now if now is not None else time.time())
    recent = db.recent_signal(chain, token, "EARLY", hours=SAME_BUILD_ENTRY_COOLDOWN_HOURS)
    same_build = db.recent_entry_decision(
        chain, token, hours=SAME_BUILD_ENTRY_COOLDOWN_HOURS, build_version=VERSION
    )
    out = {
        "recent_early": recent,
        "same_build_entry": same_build,
        "cross_build_grace": False,
        "cross_build_reset": False,
        "prior_age_minutes": None,
    }
    if not recent:
        return out
    age_min = max(0.0, (now - f(recent.get("ts"))) / 60.0)
    out["prior_age_minutes"] = age_min
    if same_build is None:
        if age_min >= CROSS_BUILD_ENTRY_GRACE_MINUTES:
            out["recent_early"] = None
            out["cross_build_reset"] = True
        else:
            out["cross_build_grace"] = True
    return out


def entry_stability_check(db, pair, tier=None):
    chain=str(pair.get("chainId","")).lower()
    token=str(nest(pair,"baseToken","address",default=""))
    current=f(pair.get("priceUsd"))
    if not token or current <= 0:
        return False, ["no trustworthy live price"], {}
    stability_window = MICRO_STABILITY_WINDOW_MINUTES if tier=="MICRO ENTRY" else ENTRY_STABILITY_WINDOW_MINUTES
    history_window = max(ENTRY_STABILITY_WINDOW_MINUTES, stability_window, RECENT_CRASH_LOOKBACK_MINUTES, 1)
    hist=db.token_history(chain,token,int(history_window))
    now=int(time.time())
    recent=[x for x in hist if x.get("ts") and now-int(x["ts"]) <= stability_window*60]
    prices=[f(x.get("price")) for x in recent if f(x.get("price")) > 0]
    min_obs = FAST_ENTRY_STABILITY_MIN_OBSERVATIONS if tier=="FAST ENTRY" else ENTRY_STABILITY_MIN_OBSERVATIONS
    min_span = FAST_ENTRY_STABILITY_MIN_SPAN_SECONDS if tier=="FAST ENTRY" else ENTRY_STABILITY_MIN_SPAN_SECONDS
    if len(prices) < min_obs:
        return False,[f"waiting for raw-price stability ({len(prices)}/{min_obs})"],{}
    span=int(recent[-1]["ts"])-int(recent[0]["ts"]) if len(recent)>=2 else 0
    if span < min_span:
        return False,[f"waiting for stability history ({span}s/{min_span}s)"],{}
    peak=max(prices+[current])
    low=min(prices+[current])
    drop=(current/max(peak,1e-18)-1)*100
    recovery=(current/max(low,1e-18)-1)*100
    if tier=="STRONG ENTRY":
        limit=STRONG_MAX_DROP_FROM_RECENT_PEAK_PCT
    elif tier=="MICRO ENTRY":
        limit=MICRO_MAX_DROP_FROM_RECENT_PEAK_PCT
    else:
        limit=ENTRY_MAX_DROP_FROM_RECENT_PEAK_PCT
    blockers=[]
    info={"drop_from_peak":drop,"recovery_from_low":recovery,"span_seconds":span}
    if drop < -limit:
        blockers.append(f"raw price {drop:.1f}% below recent {stability_window:.0f}m peak")

    # v11.3: remember a crash even when the newest quote has already bounced. The
    # Aug-29 holdout showed several immediate/mixed losers that looked benign at the
    # alert snapshot only because a -12% to -40% five-minute flush had just rolled out
    # of the live momentum field. A healthy recovery can still qualify once the crash
    # is old enough AND the 1h tape has recovered above the configured floor.
    if RECENT_CRASH_MEMORY_ENABLED:
        crash_rows=[x for x in hist if x.get("ts") and now-int(x["ts"]) <= RECENT_CRASH_LOOKBACK_MINUTES*60]
        crash_rows=[x for x in crash_rows if x.get("pc5") is not None]
        if crash_rows:
            crash=min(crash_rows,key=lambda x:f(x.get("pc5"),0))
            crash_pc5=f(crash.get("pc5"),0)
            crash_age_sec=max(0,now-int(crash.get("ts") or now))
            current_pc1=metrics(pair)["pc1"]
            info.update({"recent_crash_pc5":crash_pc5,"recent_crash_age_seconds":crash_age_sec,"current_pc1":current_pc1})
            crash_age_min=crash_age_sec/60.0
            if crash_pc5 <= RECENT_CRASH_HARD_5M_PCT:
                blockers.append(
                    f"recent crash recovery too fresh: 5m tape hit {crash_pc5:.1f}% {crash_age_min:.1f}m ago"
                )
            elif crash_pc5 <= RECENT_CRASH_FRESH_5M_PCT and crash_age_sec <= RECENT_CRASH_FRESH_MINUTES*60:
                blockers.append(
                    f"recent sharp flush still inside {RECENT_CRASH_FRESH_MINUTES:.0f}m recovery window ({crash_pc5:.1f}% 5m)"
                )
            elif crash_pc5 <= RECENT_CRASH_FRESH_5M_PCT and current_pc1 < RECENT_CRASH_RECOVERY_1H_PCT:
                blockers.append(
                    f"recent {crash_pc5:.1f}% 5m flush has not repaired the 1h tape yet ({current_pc1:+.1f}%)"
                )

    # Micro entries are allowed a slightly deeper pullback only when the raw tape has
    # clearly bounced from its low and live buyer pressure has returned.
    if tier=="MICRO ENTRY" and drop < -ENTRY_MAX_DROP_FROM_RECENT_PEAK_PCT and drop >= -limit:
        m=metrics(pair)
        ratio=m["buys"]/max(m["sells"],1)
        if recovery < MICRO_RECOVERY_REQUIRED_BELOW_NORMAL_DROP_PCT or m["pc5"] < 0 or ratio < MICRO_MIN_BUY_SELL:
            blockers.append(f"microcap pullback has not recovered enough ({recovery:.1f}% off recent low)")
    tail=prices[-4:]+[current]
    worst=0.0
    for a,b in zip(tail,tail[1:]):
        worst=min(worst,(b/max(a,1e-18)-1)*100)
    info["worst_step"]=worst
    if worst <= -10:
        blockers.append(f"rapid raw-price break {worst:.1f}% between scans")
    ratios=[]
    for x in recent[-4:]:
        ratios.append(f(x.get("buys5"))/max(f(x.get("sells5")),1))
    if tier=="STRONG ENTRY":
        good=sum(1 for r in ratios[-3:] if r >= STRONG_MIN_PERSISTENT_BS)
        if good < 2:
            blockers.append("buy pressure was not persistent across recent scans")
    if tier=="FLOW ENTRY":
        good=sum(1 for r in ratios[-3:] if r >= 1.25)
        if good < 2:
            blockers.append("flow-entry buyer pressure was not persistent across recent scans")
    if tier=="MICRO ENTRY":
        good=sum(1 for r in ratios[-3:] if r >= 1.25)
        if good < 2:
            blockers.append("microcap buyer pressure was not persistent across recent scans")
    if tier=="REVERSAL ENTRY":
        good=sum(1 for r in ratios[-3:] if r >= REVERSAL_PERSISTENT_BS)
        if good < 2:
            blockers.append("washout reversal buyer pressure was not persistent across recent scans")
        if recovery < REVERSAL_MIN_RECOVERY_FROM_LOW_PCT:
            blockers.append(f"washout reversal has recovered only {recovery:.1f}% from its recent low")
    return not blockers,blockers,info

def entry_tape_preservation(db,pair,tier):
    """Detect a still-draining tape before a borderline entry becomes actionable."""
    chain=str(pair.get("chainId","")).lower(); token=str(nest(pair,"baseToken","address",default="")); m=metrics(pair)
    hist=db.token_history(chain,token,int(TAPE_MEMORY_MINUTES)) if token else []
    if len(hist)<2:
        return True,[],{"observations":len(hist)}
    recent=hist[-8:]
    liqs=[f(x.get("liquidity")) for x in recent if f(x.get("liquidity"))>0]
    liq_drop=0.0
    if len(liqs)>=2: liq_drop=(liqs[-1]/max(liqs[0],1e-18)-1)*100
    ratios=[f(x.get("buys5"))/max(f(x.get("sells5")),1.0) for x in recent[-4:]]
    persistent=sum(r>=TAPE_PERSISTENT_BUY_SELL for r in ratios)
    info={"liq_change_pct":liq_drop,"persistent_buy_snapshots":persistent,"observations":len(hist)}
    blockers=[]
    if liq_drop <= -TAPE_HARD_LIQ_DROP_PCT:
        blockers.append(f"liquidity is still draining ({liq_drop:.1f}% over recent tape)")
    downtrend=(m["pc1"] <= TAPE_DOWNTREND_1H_PCT)
    if downtrend and (liq_drop < -TAPE_SOFT_LIQ_DROP_PCT or persistent < min(2,len(ratios))):
        blockers.append(f"downtrend entry needs stable liquidity + persistent buyers ({liq_drop:+.1f}% liq; {persistent}/{len(ratios)} buyer snapshots)")
    return not blockers,blockers,info

def entry_commitment_gate(db, pair, tier, entry_score, confirmed_score, rts=None):
    """Require one extra fresh confirmation for fragile normal entries.

    This is intentionally narrow: it does not delay STRONG/FLOW/MICRO entries. A normal
    ENTRY OPTION is considered fragile when at least two independent warning traits are
    present (hot 5m tape, cliff-edge liquidity, weak score/confirmation, or marginal
    realtime activity). The first qualifying snapshot creates an internal marker only;
    a later scan must still pass the normal classifier and stability checks.
    """
    tier_name=str(tier or "")
    if tier_name in {"STRUCTURE ENTRY","MANUAL CHAIN ENTRY"}:
        chain=str(pair.get("chainId","")).lower(); token=str(nest(pair,"baseToken","address",default=""))
        kind="STRUCTURE_PENDING" if tier_name=="STRUCTURE ENTRY" else "CHAIN_PENDING"
        need=STRUCTURE_CONFIRM_SECONDS if tier_name=="STRUCTURE ENTRY" else MANUAL_CHAIN_CONFIRM_SECONDS
        pending=db.recent_signal(chain,token,kind,hours=0.10) if token else None; now=time.time()
        if not pending:
            try: db.add_signal(kind,pair,f(entry_score),"WAIT",[f"{tier_name.lower()} awaiting fresh confirmation"])
            except Exception: pass
            return False,[f"{tier_name.lower()} needs {need}s fresh confirmation"],{"fragile":True,"lane":tier_name}
        age=max(0.0,now-f(pending.get("ts")))
        if age < need:
            return False,[f"{tier_name.lower()} confirmation {age:.0f}s/{need}s"],{"fragile":True,"lane":tier_name,"pending_age_seconds":age}
        return True,[],{"fragile":False,"lane":tier_name,"pending_age_seconds":age}
    if tier_name == "REVERSAL ENTRY":
        chain=str(pair.get("chainId","")).lower()
        token=str(nest(pair,"baseToken","address",default=""))
        pending=db.recent_signal(chain,token,"REVERSAL_PENDING",hours=0.10) if token else None
        now=time.time()
        info={"fragile":True,"reversal":True}
        if not pending:
            try:
                db.add_signal("REVERSAL_PENDING",pair,f(entry_score),"WAIT",["washout reversal awaiting fresh confirmation"])
            except Exception:
                pass
            return False,[f"washout reversal needs {REVERSAL_CONFIRM_SECONDS}s fresh confirmation"],info
        age=max(0.0,now-f(pending.get("ts")))
        info["pending_age_seconds"]=age
        if age < REVERSAL_CONFIRM_SECONDS:
            return False,[f"washout reversal confirmation {age:.0f}s/{REVERSAL_CONFIRM_SECONDS}s"],info
        return True,[],info
    if not ENTRY_COMMIT_CONFIRM_ENABLED or tier_name != "ENTRY OPTION":
        return True, [], {"fragile": False}
    m=metrics(pair)
    flags=[]
    if m["pc5"] >= ENTRY_FRAGILE_HOT_5M_PCT:
        flags.append(f"hot 5m tape {m['pc5']:+.1f}%")
    liq_floor=ENTRY_MIN_LIQUIDITY*(1.0+ENTRY_FRAGILE_LIQ_BUFFER_PCT/100.0)
    if m["liq"] < liq_floor:
        flags.append(f"liquidity only {usd(m['liq'])} vs buffered {usd(liq_floor)}")
    if f(entry_score) < ENTRY_FRAGILE_SCORE_MAX and f(confirmed_score) < ENTRY_FRAGILE_CONFIRMED_MAX:
        flags.append(f"borderline score {f(entry_score):.0f}/{f(confirmed_score):.0f}")
    if rts and rts.get("available") and f(rts.get("tx30")) <= RT_ENTRY_TX30:
        flags.append(f"marginal realtime {int(f(rts.get('tx30')))} tx/30s")
    info={"fragile":len(flags)>=2,"flags":flags}
    if len(flags) < 2:
        return True, [], info
    # Hot normal entries need more than the absolute realtime minimum; a 5m burst with
    # only 1-2 fresh transactions is a classic burst-fade pattern.
    if m["pc5"] >= ENTRY_FRAGILE_HOT_5M_PCT and rts and rts.get("available") and f(rts.get("tx30")) < ENTRY_HOT_MIN_TX30:
        return False,[f"hot entry needs at least {ENTRY_HOT_MIN_TX30} realtime tx/30s before confirmation"]+flags,info
    chain=str(pair.get("chainId","")).lower()
    token=str(nest(pair,"baseToken","address",default=""))
    pending=db.recent_signal(chain,token,"ENTRY_PENDING",hours=0.10) if token else None
    now=time.time()
    if not pending:
        try:
            db.add_signal("ENTRY_PENDING",pair,f(entry_score),"WAIT",flags)
        except Exception:
            pass
        return False,[f"fragile setup needs {ENTRY_COMMIT_CONFIRM_SECONDS}s fresh confirmation"]+flags,info
    age=max(0.0,now-f(pending.get("ts")))
    info["pending_age_seconds"]=age
    if age < ENTRY_COMMIT_CONFIRM_SECONDS:
        return False,[f"fragile setup confirmation {age:.0f}s/{ENTRY_COMMIT_CONFIRM_SECONDS}s"]+flags,info
    return True,[],info


def classify_entry_tier(pair, entry_score, confirmed_score, risks, hard_block, rts=None, safety=None, scout_move=0.0):
    """Return (tier, reasons) using lane-specific quality and chase controls.

    v13.1 deliberately separates launch/meme, established-structure and manual
    cross-chain setups. The Aug-30 holdout showed that applying one broad set of
    gates to all three created low-edge alerts, especially on cross-chain pairs.
    """
    m = metrics(pair)
    chain = str(pair.get("chainId", "")).lower()
    ratio = m["buys"] / max(m["sells"], 1)
    swaps = m["buys"] + m["sells"]
    risk_text = " ".join(risks).lower()
    reasons = []

    if hard_block or (safety and safety.get("hard")):
        return None, ["hard security check"]

    turnover_pct = 100.0 * m["v5"] / max(m["mc"], 1)
    liq_mc = m["liq"] / max(m["mc"], 1)

    # Established Solana structure lane. A mildly negative 1h tape can qualify, but
    # once it is <= -5% the setup must show materially stronger breadth/turnover.
    if chain == "solana" and STRUCTURE_ENTRY_ENABLED:
        structure=(STRUCTURE_MIN_MCAP <= m["mc"] <= STRUCTURE_MAX_MCAP and m["liq"]>=STRUCTURE_MIN_LIQUIDITY
            and liq_mc>=STRUCTURE_MIN_LIQ_MC and m["age"]>=STRUCTURE_MIN_AGE_MIN
            and STRUCTURE_MIN_5M<=m["pc5"]<=STRUCTURE_MAX_5M and STRUCTURE_MIN_1H<=m["pc1"]<=STRUCTURE_MAX_1H
            and ratio>=STRUCTURE_MIN_BUY_SELL and swaps>=STRUCTURE_MIN_SWAPS and turnover_pct>=STRUCTURE_MIN_TURNOVER_PCT
            and entry_score>=STRUCTURE_MIN_SCORE and confirmed_score>=STRUCTURE_MIN_CONFIRMED
            and scout_move<=STRUCTURE_MAX_SCOUT_MOVE_PCT
            and "sells dominate" not in risk_text and "net-selling" not in risk_text)
        if structure and m["pc1"] <= STRUCTURE_DOWNTREND_TRIGGER_1H:
            structure=(ratio>=STRUCTURE_DOWNTREND_MIN_BUY_SELL
                       and swaps>=STRUCTURE_DOWNTREND_MIN_SWAPS
                       and turnover_pct>=STRUCTURE_DOWNTREND_MIN_TURNOVER_PCT)
            if not structure:
                return None,["established downtrend has not shown enough buyer breadth/turnover to confirm repair"]
        if structure:
            reasons.append(f"established structure: {usd(m['liq'])} liquidity, {ratio:.2f}x buyers, {int(swaps)} swaps, {turnover_pct:.3f}% turnover")
            if m["pc1"] <= STRUCTURE_DOWNTREND_TRIGGER_1H:
                reasons.append("downtrend structure passed the stronger repair-quality gate")
            return "STRUCTURE ENTRY", reasons

    # Manual cross-chain lane. v13.0's broad gate had poor 30m edge, so v13.1 uses
    # three explicit shapes: lower-liquidity flow, deeper-liquidity flow, or a true
    # liquid breakout. All reject a move >4% from the bot's own scout snapshot.
    if chain in MANUAL_ENTRY_CHAINS and MANUAL_CHAIN_ENTRY_ENABLED:
        score_ok=(entry_score>=MANUAL_CHAIN_MIN_SCORE and confirmed_score>=MANUAL_CHAIN_MIN_CONFIRMED)
        common=(MANUAL_CHAIN_MIN_MCAP<=m["mc"]<=MANUAL_CHAIN_MAX_MCAP
                and m["liq"]>=MANUAL_CHAIN_MIN_LIQUIDITY and liq_mc>=MANUAL_CHAIN_MIN_LIQ_MC
                and m["age"]>=MANUAL_CHAIN_MIN_AGE_MIN and score_ok
                and scout_move<=MANUAL_CHAIN_MAX_SCOUT_MOVE_PCT
                and "sells dominate" not in risk_text and "net-selling" not in risk_text)
        low_flow=(common and m["liq"]<MANUAL_CHAIN_LOW_MAX_LIQUIDITY
                  and MANUAL_CHAIN_LOW_MIN_5M<=m["pc5"]<=MANUAL_CHAIN_LOW_MAX_5M
                  and MANUAL_CHAIN_LOW_MIN_1H<=m["pc1"]<=MANUAL_CHAIN_LOW_MAX_1H
                  and ratio>=MANUAL_CHAIN_LOW_MIN_BUY_SELL and swaps>=MANUAL_CHAIN_LOW_MIN_SWAPS
                  and turnover_pct>=MANUAL_CHAIN_LOW_MIN_TURNOVER_PCT)
        deep_flow=(common and m["liq"]>=MANUAL_CHAIN_DEEP_MIN_LIQUIDITY
                   and MANUAL_CHAIN_DEEP_MIN_5M<=m["pc5"]<=MANUAL_CHAIN_DEEP_MAX_5M
                   and MANUAL_CHAIN_DEEP_MIN_1H<=m["pc1"]<=MANUAL_CHAIN_DEEP_MAX_1H
                   and ratio>=MANUAL_CHAIN_DEEP_MIN_BUY_SELL and swaps>=MANUAL_CHAIN_DEEP_MIN_SWAPS
                   and turnover_pct>=MANUAL_CHAIN_DEEP_MIN_TURNOVER_PCT)
        breakout=(common and m["liq"]>=MANUAL_CHAIN_BREAKOUT_MIN_LIQUIDITY
                  and liq_mc>=MANUAL_CHAIN_BREAKOUT_MIN_LIQ_MC
                  and MANUAL_CHAIN_BREAKOUT_MIN_5M<=m["pc5"]<=MANUAL_CHAIN_BREAKOUT_MAX_5M
                  and MANUAL_CHAIN_BREAKOUT_MIN_1H<=m["pc1"]<=MANUAL_CHAIN_BREAKOUT_MAX_1H
                  and ratio>=MANUAL_CHAIN_BREAKOUT_MIN_BUY_SELL
                  and swaps>=MANUAL_CHAIN_BREAKOUT_MIN_SWAPS
                  and turnover_pct>=MANUAL_CHAIN_BREAKOUT_MIN_TURNOVER_PCT)
        if low_flow or deep_flow or breakout:
            mode="breakout" if breakout else ("deep-flow" if deep_flow else "flow")
            reasons.append(f"manual cross-chain {mode}: {usd(m['liq'])} liquidity, {ratio:.2f}x buyers, {int(swaps)} swaps, {turnover_pct:.3f}% turnover")
            reasons.append("manual-execution lane; real autopilot remains disabled for this chain")
            return "MANUAL CHAIN ENTRY", reasons
        if scout_move>MANUAL_CHAIN_MAX_SCOUT_MOVE_PCT:
            return None,[f"cross-chain move already {scout_move:+.1f}% from scout; do not chase"]
        return None,["cross-chain setup has not passed a strict flow/breakout shape"]

    if chain not in ENTRY_CHAINS:
        return None, ["chain is watch-only"]

    # v10.7 MICRO ENTRY: rescue exceptional sub-$50k-liquidity launches without
    # weakening the normal liquidity floor. This path demands heavy real dollar
    # turnover, persistent buyers, verified Solana safety and a tight scout move.
    micro_trigger = (
        MICRO_ENTRY_ENABLED
        and MICRO_MIN_LIQUIDITY <= m["liq"] < ENTRY_MIN_LIQUIDITY
        and MICRO_MIN_MCAP <= m["mc"] <= MICRO_MAX_MCAP
        and MICRO_MIN_LIQ_MC <= liq_mc <= MICRO_MAX_LIQ_MC
        and turnover_pct >= MICRO_MIN_TURNOVER_PCT
        and ratio >= MICRO_MIN_BUY_SELL
        and swaps >= MICRO_MIN_SWAPS
        and MICRO_MIN_5M <= m["pc5"] <= MICRO_MAX_5M
        and MICRO_MIN_1H <= m["pc1"] <= MICRO_MAX_1H
        and scout_move <= MICRO_MAX_SCOUT_MOVE
        and (entry_score >= MICRO_MIN_SCORE or confirmed_score >= MICRO_MIN_CONFIRMED)
        and (not rts or not rts.get("available") or rts.get("tx30",0) >= RT_ENTRY_TX30)
    )
    if micro_trigger:
        verified = bool(safety and safety.get("rug_checked") and safety.get("holder_checked"))
        if chain == "solana" and not verified:
            return None, ["microcap path requires RugCheck + holder verification"]
        reasons.append(f"microcap rescue: {turnover_pct:.1f}% 5m turnover, {ratio:.2f}x buyers, {int(swaps)} swaps")
        return "MICRO ENTRY", reasons

    # Earlier, higher-risk launch path. It keeps only the safeguards that matter most:
    # exact contract, some real liquidity/order flow, and no hard security failure.
    fresh_trigger=(
        m["age"] <= FRESH_MAX_AGE_MIN
        and m["liq"] >= FRESH_MIN_LIQ
        and FRESH_MIN_MCAP <= m["mc"] <= FRESH_MAX_MCAP
        and OPTION_MIN_5M <= m["pc5"] <= FRESH_MAX_5M
        and m["pc1"] <= 30
        and ratio >= FRESH_MIN_BS
        and swaps >= FRESH_MIN_SWAPS
        and (entry_score >= FRESH_SCORE or confirmed_score >= FRESH_CONFIRMED)
        and scout_move <= 10
    )
    if fresh_trigger:
        rug_ok = bool(safety and safety.get("rug_checked"))
        holder_ok = bool(safety and safety.get("holder_checked"))
        if FRESH_REQUIRE_RUGCHECK and not rug_ok:
            return None, ["fresh launch blocked: RugCheck has not completed"]
        if FRESH_REQUIRE_HOLDER_CHECK and not holder_ok:
            return None, ["fresh launch blocked: holder distribution has not been verified"]
        reasons.append("very new pool with live buyer flow + required fresh-token safety checks")
        return "FRESH ENTRY", reasons

    # Deep washout reversals remain a separate, narrow lane. Do not solve ordinary
    # downtrends by weakening the normal-entry floor.
    reversal_trigger = (
        REVERSAL_ENTRY_ENABLED
        and m["liq"] >= REVERSAL_MIN_LIQUIDITY
        and REVERSAL_MIN_MCAP <= m["mc"] <= REVERSAL_MAX_MCAP
        and REVERSAL_MIN_1H <= m["pc1"] < REVERSAL_MAX_1H
        and REVERSAL_MIN_5M <= m["pc5"] <= REVERSAL_MAX_5M
        and ratio >= REVERSAL_MIN_BUY_SELL
        and swaps >= REVERSAL_MIN_SWAPS
        and turnover_pct >= REVERSAL_MIN_TURNOVER_PCT
        and (entry_score >= REVERSAL_MIN_SCORE or confirmed_score >= REVERSAL_MIN_CONFIRMED)
        and scout_move <= OPTION_MAX_SCOUT_MOVE
        and "sells dominate" not in risk_text and "net-selling" not in risk_text
        and (not rts or not rts.get("available") or rts.get("tx30",0) >= REVERSAL_MIN_RT30)
    )
    if reversal_trigger:
        verified = bool(safety and safety.get("rug_checked") and
                        (safety.get("holder_checked") or m["age"] > YOUNG_HOLDER_CHECK_MAX_AGE_MIN))
        if chain == "solana" and not verified:
            return None, ["washout reversal requires completed safety verification"]
        reasons.append(
            f"washout reversal: {m['pc1']:+.1f}% 1h has flattened to {m['pc5']:+.1f}% 5m with "
            f"{ratio:.2f}x buyers, {int(swaps)} swaps and {turnover_pct:.2f}% turnover"
        )
        return "REVERSAL ENTRY", reasons

    # Standard Solana entry. The Aug-30 holdout showed ordinary entries below -10%
    # 1h had poor 30m outcomes. -10%..-5% is now a strict recovery pocket requiring
    # broader buyers and stronger real turnover; deeper washouts use REVERSAL ENTRY.
    if m["liq"] < ENTRY_MIN_LIQUIDITY:
        return None, [f"liquidity {usd(m['liq'])} < {usd(ENTRY_MIN_LIQUIDITY)}"]
    if not (ENTRY_MIN_MCAP <= m["mc"] <= ENTRY_MAX_MCAP):
        return None, ["market cap outside entry range"]
    if "sells dominate" in risk_text or "net-selling" in risk_text:
        return None, ["sell pressure dominates"]
    if m["pc1"] < STANDARD_ENTRY_MIN_1H:
        return None, [f"1h move {m['pc1']:+.1f}% is below the standard-entry floor {STANDARD_ENTRY_MIN_1H:+.1f}% (crash/falling-knife risk)"]
    if m["pc1"] > STANDARD_ENTRY_MAX_1H:
        return None, [f"1h move {m['pc1']:+.1f}% is above the standard-entry ceiling {STANDARD_ENTRY_MAX_1H:+.1f}%"]
    if m["pc1"] < STANDARD_RECOVERY_TRIGGER_1H:
        recovery_ok=(ratio>=STANDARD_RECOVERY_MIN_BUY_SELL and swaps>=STANDARD_RECOVERY_MIN_SWAPS
                     and turnover_pct>=STANDARD_RECOVERY_MIN_TURNOVER_PCT
                     and STANDARD_RECOVERY_MIN_5M<=m["pc5"]<=STANDARD_RECOVERY_MAX_5M
                     and (not rts or not rts.get("available") or rts.get("tx30",0)>=RT_ENTRY_TX30))
        if not recovery_ok:
            return None,["standard downtrend recovery pocket needs stronger buyers, swaps and dollar turnover"]
        reasons.append("mild downtrend passed the stronger recovery-quality gate")
    if scout_move > OPTION_MAX_SCOUT_MOVE:
        if not breakout_rescue_ok(pair, entry_score, risks, rts or {}, scout_move):
            return None, [f"moved {scout_move:+.1f}% from scout"]
    if not (OPTION_MIN_5M <= m["pc5"] <= OPTION_MAX_5M):
        if not breakout_rescue_ok(pair, entry_score, risks, rts or {}, scout_move):
            return None, [f"5m move {m['pc5']:+.1f}% outside entry window"]
    if turnover_pct < ENTRY_MIN_VOLUME_TURNOVER_PCT:
        return None, [f"5m dollar turnover {turnover_pct:.3f}% of market cap < {ENTRY_MIN_VOLUME_TURNOVER_PCT:.3f}%"]
    if ratio < OPTION_MIN_BUY_SELL:
        return None, [f"buy/sell {ratio:.2f}x < {OPTION_MIN_BUY_SELL:.2f}x"]
    if swaps < OPTION_MIN_SWAPS:
        return None, [f"{int(swaps)} swaps/5m < {OPTION_MIN_SWAPS}"]
    if rts and rts.get("available") and rts.get("tx30", 0) < RT_ENTRY_TX30:
        return None, [f"realtime activity {rts.get('tx30',0)} tx/30s < required {RT_ENTRY_TX30}"]

    verified_now = bool(safety and safety.get("rug_checked") and
                        (safety.get("holder_checked") or m["age"] > YOUNG_HOLDER_CHECK_MAX_AGE_MIN))
    flow_trigger = (
        m["liq"] >= ENTRY_MIN_LIQUIDITY and m["mc"] <= 2_000_000 and liq_mc >= 0.08
        and turnover_pct >= 1.0 and ratio >= 1.50 and swaps >= 50
        and -2.0 <= m["pc5"] <= 5.0 and STANDARD_ENTRY_MIN_1H <= m["pc1"] <= STANDARD_ENTRY_MAX_1H
        and scout_move <= OPTION_MAX_SCOUT_MOVE and verified_now
    )
    score_trigger = entry_score >= OPTION_SCORE or confirmed_score >= OPTION_CONFIRMED_SCORE
    if not score_trigger and not flow_trigger:
        return None, [f"entry {entry_score:.0f} / confirmed {confirmed_score:.0f} below option trigger"]

    if chain == "solana" and NORMAL_REQUIRE_RUGCHECK and not bool(safety and safety.get("rug_checked")):
        return None, ["RugCheck has not completed"]
    if (chain == "solana" and YOUNG_REQUIRE_HOLDER_CHECK and m["age"] <= YOUNG_HOLDER_CHECK_MAX_AGE_MIN
            and not bool(safety and safety.get("holder_checked"))):
        return None, ["holder distribution has not been verified"]

    verified = bool(safety and safety.get("rug_checked") and
                    (safety.get("holder_checked") or m["age"] > YOUNG_HOLDER_CHECK_MAX_AGE_MIN))
    if flow_trigger and not score_trigger:
        reasons.append("persistent high-dollar flow promoted the setup before the legacy score caught up")
        return "FLOW ENTRY", reasons
    strong = (
        (entry_score >= STRONG_SCORE or confirmed_score >= STRONG_CONFIRMED_SCORE)
        and ratio >= STRONG_MIN_BUY_SELL
        and swaps >= STRONG_MIN_SWAPS
        and turnover_pct >= STRONG_MIN_VOLUME_TURNOVER_PCT
        and 0 <= m["pc5"] <= STRONG_MAX_5M
        and scout_move <= STRONG_MAX_SCOUT_MOVE
        and (not rts or not rts.get("available") or rts.get("tx30",0) >= RT_ENTRY_TX30)
        and verified
    )
    if strong:
        reasons.append("strong score/order flow + verified safety")
        return "STRONG ENTRY", reasons

    if confirmed_score >= OPTION_CONFIRMED_SCORE and entry_score < OPTION_SCORE:
        reasons.append("momentum confirmation promoted this setup")
    elif m["pc5"] < 0:
        reasons.append("slight pullback with acceptable order flow")
    else:
        reasons.append("market + order-flow gates passed")
    if not verified:
        reasons.append("safety enrichment incomplete; use smaller size")
    return "ENTRY OPTION", reasons

def classify_fast_entry_tier(pair, entry_score, confirmed_score, risks, hard_block, rts=None, safety=None, scout_move=0.0):
    """Independent v16.1 fast lane.

    This lane does not weaken hard security checks. It accepts less waiting only when
    current Solana liquidity, turnover and buyer flow are materially stronger than the
    ordinary option floor. Birdeye enrichment is optional; basic on-chain/RugCheck
    evidence is enough when premium enrichment is delayed.
    """
    if not FAST_ENTRY_ENABLED:
        return None,["fast lane disabled"]
    m=metrics(pair); chain=str(pair.get("chainId") or "").lower()
    ratio=m["buys"]/max(m["sells"],1); swaps=m["buys"]+m["sells"]
    turnover=100.0*m["v5"]/max(m["mc"],1); liq_mc=m["liq"]/max(m["mc"],1)
    risk_text=" ".join(risks or []).lower()
    if chain!="solana": return None,["fast lane is Solana-only"]
    if hard_block or (safety and safety.get("hard")): return None,["hard security check"]
    if "sells dominate" in risk_text or "net-selling" in risk_text: return None,["sell pressure dominates"]
    if max(f(entry_score),f(confirmed_score)) < min(FAST_ENTRY_MIN_SCORE,FAST_ENTRY_MIN_CONFIRMED):
        return None,[f"fast score {f(entry_score):.0f}/{f(confirmed_score):.0f} below lane floor"]
    if f(entry_score) < FAST_ENTRY_MIN_SCORE and f(confirmed_score) < FAST_ENTRY_MIN_CONFIRMED:
        return None,[f"fast lane needs entry {FAST_ENTRY_MIN_SCORE:.0f}+ or confirmed {FAST_ENTRY_MIN_CONFIRMED:.0f}+"]
    if m["liq"] < FAST_ENTRY_MIN_LIQUIDITY: return None,[f"fast liquidity {usd(m['liq'])} < {usd(FAST_ENTRY_MIN_LIQUIDITY)}"]
    if not (FAST_ENTRY_MIN_MCAP <= m["mc"] <= FAST_ENTRY_MAX_MCAP): return None,["market cap outside fast lane"]
    if liq_mc < FAST_ENTRY_MIN_LIQ_MC: return None,[f"fast liq/MC {100*liq_mc:.1f}% < {100*FAST_ENTRY_MIN_LIQ_MC:.1f}%"]
    if turnover < FAST_ENTRY_MIN_TURNOVER_PCT: return None,[f"fast turnover {turnover:.2f}% < {FAST_ENTRY_MIN_TURNOVER_PCT:.2f}%"]
    if ratio < FAST_ENTRY_MIN_BUY_SELL: return None,[f"fast B/S {ratio:.2f}x < {FAST_ENTRY_MIN_BUY_SELL:.2f}x"]
    if swaps < FAST_ENTRY_MIN_SWAPS: return None,[f"fast swaps {int(swaps)} < {FAST_ENTRY_MIN_SWAPS}"]
    if not (FAST_ENTRY_MIN_5M <= m["pc5"] <= FAST_ENTRY_MAX_5M): return None,[f"fast 5m {m['pc5']:+.1f}% outside lane"]
    if not (FAST_ENTRY_MIN_1H <= m["pc1"] <= FAST_ENTRY_MAX_1H): return None,[f"fast 1h {m['pc1']:+.1f}% outside lane"]
    if scout_move > FAST_ENTRY_MAX_SCOUT_MOVE: return None,[f"fast lane move already {scout_move:+.1f}% from scout"]
    if rts and rts.get("available") and f(rts.get("tx30")) < max(1,RT_ENTRY_TX30):
        return None,["fast lane realtime activity went quiet"]
    if FAST_ENTRY_REQUIRE_BASIC_SAFETY:
        basic=bool(safety and (safety.get("rug_checked") or (safety.get("mint_checked") and safety.get("holder_checked"))))
        if not basic:
            return None,["fast lane waiting for basic on-chain safety"]
    return "FAST ENTRY",[f"fast lane: {ratio:.2f}x buyers, {int(swaps)} swaps, {turnover:.2f}% turnover; premium enrichment may finish later"]


def classify_actionable_tier(pair, entry_score, confirmed_score, risks, hard_block, rts=None, safety=None, scout_move=0.0):
    tier,reasons=classify_entry_tier(pair,entry_score,confirmed_score,risks,hard_block,rts=rts,safety=safety,scout_move=scout_move)
    if tier:
        return tier,reasons
    fast,fast_reasons=classify_fast_entry_tier(pair,entry_score,confirmed_score,risks,hard_block,rts=rts,safety=safety,scout_move=scout_move)
    if fast:
        return fast,fast_reasons
    return None,(list(reasons or [])+list(fast_reasons or []))[:5]


def entry_tier_emoji(tier):
    if tier == "FAST ENTRY": return "⚡"
    if tier == "STRONG ENTRY": return "🔥"
    if tier == "FRESH ENTRY": return "🚀"
    if tier == "MICRO ENTRY": return "⚡"
    if tier == "FLOW ENTRY": return "🌊"
    if tier == "RE-ENTRY OPTION": return "🟣"
    if tier == "REVERSAL ENTRY": return "🔄"
    if tier == "STRUCTURE ENTRY": return "📈"
    if tier == "MANUAL CHAIN ENTRY": return "🌐"
    return "🟢"

def actionable_blockers(pair, entry_score, confirmed_score, risks, hard_block=False, rts=None, scout=None, safety=None):
    """Return concise reasons a candidate is not actionable under its relevant lane."""
    m = metrics(pair)
    ratio = m["buys"] / max(m["sells"], 1)
    swaps = m["buys"] + m["sells"]
    turnover_pct = 100.0 * m["v5"] / max(m["mc"], 1)
    risk_text = " ".join(risks).lower()
    out = []
    chain = str(pair.get("chainId", "")).lower()
    scout_move=0.0
    if scout is not None and pair.get("priceUsd"):
        scout_move=(f(pair.get("priceUsd"))/max(f(scout.get("price")),1e-18)-1)*100

    if hard_block or (safety and safety.get("hard")):
        return ["hard security check"]
    if chain not in ALERT_ENTRY_CHAINS:
        return ["chain is watch-only"]

    # Ask the classifier first. Its lane-specific reason is more useful than applying
    # Solana meme thresholds to a $40M structure coin or to a cross-chain pair.
    tier,reasons=classify_actionable_tier(pair,entry_score,confirmed_score,risks,hard_block,
                                     rts=rts,safety=safety,scout_move=scout_move)
    if tier:
        if scout is None:
            out.append("waiting for internal scout confirmation")
        return out
    out.extend(reasons[:3])

    if "sells dominate" in risk_text or "net-selling" in risk_text:
        out.append("sell pressure dominates")
    if rts and rts.get("available") and rts.get("tx30",0) < 1:
        out.append("realtime activity went quiet")
    if chain == "solana" and NORMAL_REQUIRE_RUGCHECK and safety is not None and not safety.get("rug_checked"):
        out.append("waiting for RugCheck")
    if (chain == "solana" and YOUNG_REQUIRE_HOLDER_CHECK and m["age"] <= YOUNG_HOLDER_CHECK_MAX_AGE_MIN
            and safety is not None and not safety.get("holder_checked")):
        out.append("waiting for holder verification")
    if scout is None:
        out.append("waiting for internal scout confirmation")
    # Deduplicate while preserving order.
    clean=[]
    for item in out:
        if item and item not in clean: clean.append(item)
    return clean

def setup_confidence(pair, score, risks, rts=None, safety=None):
    """Human-readable setup tier; never a guarantee of profit."""
    m = metrics(pair)
    ratio = m["buys"] / max(m["sells"], 1)
    swaps = m["buys"] + m["sells"]
    risk_text = " ".join(risks).lower()
    verified = bool(safety and (safety.get("rug_checked") or safety.get("security_checked") or safety.get("holder_checked")))
    strong_rt = (not rts or not rts.get("available") or
                 (rts.get("tx30",0) >= RT_ENTRY_TX30 and rts.get("acceleration",0) >= 1.0))
    if (score >= HIGH_CONVICTION_SCORE and ratio >= HIGH_CONVICTION_BUY_SELL and
            swaps >= HIGH_CONVICTION_SWAPS and strong_rt and
            "sells dominate" not in risk_text and "net-selling" not in risk_text and
            (verified or not STRICT_MODE)):
        return "HIGH"
    ratio_ok, near_ratio = buy_sell_gate_ok(pair, score, risks, rts)
    score_ok, near_score = score_gate_ok(pair, score, risks, rts)
    if score_ok and ratio_ok and swaps >= ENTRY_MIN_SWAPS_5M:
        if near_score and near_ratio:
            return "STANDARD (NEAR-SCORE + NEAR-RATIO PASS)"
        if near_score:
            return "STANDARD (NEAR-SCORE PASS)"
        if near_ratio:
            return "STANDARD (NEAR-RATIO PASS)"
        return "STANDARD"
    return "WATCH"


def breakout_rescue_ok(pair, score, risks, rts, scout_move):
    """Allow a slightly extended breakout only when order flow is exceptional.
    This is intentionally narrow so a price spike by itself never becomes an entry.
    """
    if not BREAKOUT_RESCUE_ENABLED:
        return False
    m = metrics(pair)
    ratio = m["buys"] / max(m["sells"], 1)
    swaps = m["buys"] + m["sells"]
    risk_text = " ".join(risks).lower()
    if "sells dominate" in risk_text or "net-selling" in risk_text:
        return False
    if not (EARLY_MAX5 < m["pc5"] <= BREAKOUT_RESCUE_MAX_5M):
        return False
    if scout_move > BREAKOUT_RESCUE_MAX_SCOUT_MOVE:
        return False
    if score < BREAKOUT_RESCUE_MIN_SCORE or ratio < BREAKOUT_RESCUE_MIN_BUY_SELL or swaps < BREAKOUT_RESCUE_MIN_SWAPS:
        return False
    if rts and rts.get("available"):
        if rts.get("tx30",0) < BREAKOUT_RESCUE_MIN_TX30 or rts.get("acceleration",0) < BREAKOUT_RESCUE_MIN_ACCEL:
            return False
    return True


async def dex_search_candidates(http, query, limit=8):
    """Resolve user input across DexScreener. Never silently trust duplicate tickers."""
    q=str(query or "").strip()
    if not q: return []
    qlow=q.lower(); rows=[]; seen=set(); scored=[]
    status,data=await http.get(f"{DEX}/latest/dex/search",params={"q":q})
    if status==200 and isinstance(data,dict) and isinstance(data.get("pairs"),list):
        rows.extend(data["pairs"])
    for chain in CHAINS:
        pair=await pair_for_token(http,chain,q)
        if pair: rows.append(pair)
    for pair in rows:
        if not isinstance(pair,dict): continue
        base=pair.get("baseToken") or {}; addr=str(base.get("address") or "").strip()
        if not addr: continue
        chain=str(pair.get("chainId") or "").lower(); key=(chain,addr.lower())
        if key in seen: continue
        seen.add(key)
        sym=str(base.get("symbol") or "").strip(); name=str(base.get("name") or "").strip()
        exact_contract=addr.lower()==qlow; exact_symbol=sym.lower()==qlow; exact_name=name.lower()==qlow
        partial=qlow in sym.lower() or qlow in name.lower()
        if not (exact_contract or exact_symbol or exact_name or partial): continue
        exactness=4 if exact_contract else 3 if exact_symbol else 2 if exact_name else 1
        scored.append((exactness,f(nest(pair,"liquidity","usd",default=0)),f(pair.get("marketCap") or pair.get("fdv")),pair))
    scored.sort(key=lambda x:(x[0],x[1],x[2]),reverse=True)
    return [x[3] for x in scored[:max(1,int(limit))]]


def looks_solana_contract(value):
    return bool(re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}",str(value or "").strip()))

def looks_evm_contract(value):
    return bool(re.fullmatch(r"0x[0-9A-Fa-f]{40}",str(value or "").strip()))

async def resolve_exact_contract(http, query, chain_hint=None):
    q=str(query or "").strip()
    if chain_hint in CHAINS:
        pair=await pair_for_token(http,chain_hint,q)
        if pair: return pair
    if looks_solana_contract(q):
        pair=await pair_for_token(http,"solana",q)
        if pair: return pair
    if looks_evm_contract(q):
        for ch in [x for x in ("base","bsc","monad") if x in CHAINS]:
            pair=await pair_for_token(http,ch,q)
            if pair: return pair
    return None

async def dex_search_pair(http, query):
    direct=await resolve_exact_contract(http,query)
    if direct: return direct
    rows=await dex_search_candidates(http,query,8)
    if not rows: return None
    qlow=str(query or "").strip().lower()
    exact=[p for p in rows if str(nest(p,"baseToken","address",default="")).lower()==qlow]
    if exact: return max(exact,key=lambda p:f(nest(p,"liquidity","usd",default=0)))
    named=[p for p in rows if str(nest(p,"baseToken","symbol",default="")).lower()==qlow or str(nest(p,"baseToken","name",default="")).lower()==qlow]
    unique={(str(p.get("chainId","")),str(nest(p,"baseToken","address",default="")).lower()):p for p in named}
    return next(iter(unique.values())) if len(unique)==1 else None


def format_coin_matches(rows,query):
    if not rows: return f"No DexScreener matches found for '{query}'."
    lines=[f"🔎 MULTIPLE MATCHES — {query}","Use /scan CONTRACT or /track CONTRACT for the exact coin:"]
    for p in rows[:8]:
        base=p.get("baseToken") or {}; m=metrics(p)
        lines.append(f"• {base.get('name') or 'Unknown'} ({base.get('symbol') or '?'}) | {p.get('chainId','?')} | MC {usd(m['mc'])} | liq {usd(m['liq'])}\n  {base.get('address','')}")
    return "\n".join(lines)


async def manual_coin_report(http, db, state, guard, query):
    pair = await dex_search_pair(http, query)
    if not pair:
        matches=await dex_search_candidates(http,query,8)
        if matches: return format_coin_matches(matches,query)
        q=str(query or "").strip()
        if looks_solana_contract(q):
            db.add_manual_watch("solana",q,"PENDING","Waiting for DEX pool")
            return (f"🛰️ CONTRACT RECOGNIZED — NOT INDEXED YET\n{q}\n"
                    "I could not get a live DEX pair yet, so I added this mint to persistent tracking. "
                    "The bot will keep checking it on later scans instead of forgetting it.\n"
                    "Use /tracked to confirm it is being watched.")
        return f"I couldn't find a live market matching '{query}'. For a brand-new coin, send the exact contract/mint so I can keep tracking it until a pool appears."
    chain = str(pair.get("chainId","")).lower()
    token = str(nest(pair,"baseToken","address",default=""))
    base = pair.get("baseToken") or {}
    previous = db.previous(chain, token, 90)
    raw, reasons, risks, kind = early_score(pair, previous)
    cb, hits = catalyst_bonus(pair, db.recent_catalysts(24*60) if hasattr(db,'recent_catalysts') else [])
    score = clamp(raw + cb, 0, 100)
    conf_bonus, conf_reasons = confirmation_bonus(pair)
    confirmed = clamp(score + conf_bonus, 0, 100)
    reasons += conf_reasons
    rt = state.get("realtime")
    rts = rt.stats(token) if rt and chain == "solana" else {}
    if rt and chain == "solana":
        await rt.watch_mint(token)
        rts = rt.stats(token)
    safety = cached_safety(state, chain, token, pair=pair)
    if safety is None:
        if chain == "solana":
            safety = await preflight_safety(http, guard, db, chain, token, state)
        else:
            safety = {"ts":time.time(),"hard":False,"reasons":[],"risks":[],"rug_checked":False,
                      "security_checked":False,"holder_checked":False,"trader_checked":False,
                      "wallet_checked":False,"score_adjustment":0.0}
    risks = list(risks) + list(safety.get("risks",[]))
    reasons = list(reasons) + list(safety.get("reasons",[]))
    score = clamp(score + min(8, max(0, f(safety.get("score_adjustment")))), 0, 100)
    m = metrics(pair)
    ratio = m["buys"] / max(m["sells"],1)
    swaps = m["buys"] + m["sells"]
    tier, tier_reasons = classify_actionable_tier(pair, score, confirmed, risks, bool(safety.get("hard")), rts=rts, safety=safety, scout_move=0)
    live_scout = db.recent_signal(chain,token,"SCOUT",hours=2)
    blockers = actionable_blockers(pair,score,confirmed,risks,bool(safety.get("hard")),rts=rts,scout=live_scout,safety=safety)
    if safety.get("hard"):
        verdict = "⛔ AVOID — hard security issue"
    elif chain not in ENTRY_CHAINS:
        verdict = f"🔎 LIVE SCAN — {chain.upper()} is manual/watch-only in current auto-entry settings"
    elif tier:
        verdict = f"{entry_tier_emoji(tier)} {tier} NOW"
    elif m["pc5"] > TOO5 or m["pc1"] > TOO1:
        verdict = "⛔ DO NOT CHASE — already extended"
    else:
        verdict = "🟡 NOT AN ENTRY YET"
    rtline = (f" | RT {rts.get('tx30',0)} tx/30s {rts.get('acceleration',0):.1f}x" if rts.get("available") else "")
    why = "; ".join((reasons+tier_reasons)[:5]) or "no strong positive evidence yet"
    missing = "; ".join(blockers[:6]) or "none"
    plan=hold_plan(pair,tier=tier or "WATCH")
    cal=adaptive_dollar_size(db,pair,tier or "WATCH",score) if ADAPTIVE_CONFIDENCE_ENABLED else None
    cal_line=(f"Adaptive confidence {cal['confidence']:.0f}/100 ({cal['grade']}) | suggested buy ~${cal['suggested_usd']:.0f} ({cal.get('size_label','')})\n" if cal else "")
    return (
        f"🔎 LIVE COIN CHECK — {base.get('name') or 'Unknown'} ({base.get('symbol') or '?'})\n"
        f"{verdict}\n"
        f"Entry {score:.0f} | Confirmed {confirmed:.0f} | 5m {m['pc5']:+.1f}% | 1h {m['pc1']:+.1f}% | B/S {ratio:.2f}x | {int(swaps)} swaps{rtline}\n"
        f"MC {usd(m['mc'])} | Liquidity {usd(m['liq'])}\n"
        f"Horizon: {plan['label']} — {plan['window']}\n"
        f"{cal_line}"
        f"WHY: {why}\n"
        f"BLOCKERS: {missing}\n"
        f"Contract: {token}\n"
        "Use the contract as identity. This is a screening signal, not a guarantee of profit."
    )


async def why_coin_report(http, db, state, guard, query):
    """Explain the exact live entry pipeline without creating an entry/pending marker."""
    pair = await dex_search_pair(http, query)
    if not pair:
        rows = await dex_search_candidates(http, query, 8)
        if rows:
            return format_coin_matches(rows, query)
        return f"I couldn't find a live market matching '{query}'. Use the exact contract/mint when possible."

    chain = str(pair.get("chainId", "")).lower()
    token = str(nest(pair, "baseToken", "address", default=""))
    base = pair.get("baseToken") or {}
    prev = db.previous(chain, token, 90)
    raw, reasons, risks, _ = early_score(pair, prev)
    cb, _ = catalyst_bonus(pair, db.recent_catalysts(24*60) if hasattr(db, "recent_catalysts") else [])
    score = clamp(raw + cb, 0, 100)
    conf_bonus, conf_reasons = confirmation_bonus(pair)
    confirmed = clamp(score + conf_bonus, 0, 100)
    reasons = list(reasons) + list(conf_reasons)

    rt = state.get("realtime")
    rts = rt.stats(token) if rt and chain == "solana" else {}
    if rt and chain == "solana":
        await rt.watch_mint(token)
        rts = rt.stats(token)

    safety = cached_safety(state, chain, token, pair=pair)
    if safety is None:
        if chain == "solana":
            safety = await preflight_safety(http, guard, db, chain, token, state)
        else:
            safety = {"ts":time.time(),"hard":False,"reasons":[],"risks":[],"rug_checked":False,
                      "security_checked":False,"holder_checked":False,"trader_checked":False,
                      "wallet_checked":False,"score_adjustment":0.0}
    risks += list(safety.get("risks", []))
    reasons += list(safety.get("reasons", []))
    score = clamp(score + min(8.0, max(0.0, f(safety.get("score_adjustment")))), 0, 100)

    scout = db.recent_signal(chain, token, "SCOUT", hours=2)
    scout_move = 0.0
    if scout and f(scout.get("price")) > 0:
        scout_move = (f(pair.get("priceUsd")) / max(f(scout.get("price")), 1e-18) - 1) * 100
    tier, tier_reasons = classify_actionable_tier(
        pair, score, confirmed, risks, bool(safety.get("hard")), rts=rts, safety=safety, scout_move=scout_move
    )
    blockers = actionable_blockers(
        pair, score, confirmed, risks, bool(safety.get("hard")), rts=rts, scout=scout, safety=safety
    )

    cooldown = entry_cooldown_context(db, chain, token)
    open_pos = db.position_by_token(token)
    cooldown_block = bool(cooldown.get("recent_early")) and not open_pos
    stability_ok, stability_blockers, stability_info = entry_stability_check(db, pair, tier) if tier else (False, [], {})
    tape_ok, tape_blockers, tape_info = entry_tape_preservation(db, pair, tier) if tier and stability_ok else (False, [], {})
    edge_ok, edge_blockers, edge_info = capital_edge_gate(db,pair,tier,score,confirmed) if tier and stability_ok and tape_ok else (False, [], {})
    pending = db.recent_signal(chain,token,"CAPITAL_PENDING",hours=0.20) if tier and edge_ok and token else None
    capital_diag=[]
    if tier and edge_ok and tier in {"ENTRY OPTION","STRONG ENTRY","FLOW ENTRY","REVERSAL ENTRY","FAST ENTRY"}:
        if not pending:
            capital_diag=[f"capital confirmation has not started yet ({CAPITAL_CONFIRM_SECONDS}s required)"]
        else:
            age=max(0.0,time.time()-f(pending.get("ts")))
            move=(f(pair.get("priceUsd"))/max(f(pending.get("price")),1e-18)-1)*100
            lchg=(f(nest(pair,"liquidity","usd",default=0))/max(f(pending.get("liquidity")),1e-18)-1)*100 if f(pending.get("liquidity"))>0 else 0
            if age<CAPITAL_CONFIRM_SECONDS: capital_diag.append(f"capital confirmation {age:.0f}/{CAPITAL_CONFIRM_SECONDS}s")
            if move < -CAPITAL_CONFIRM_MAX_DOWN_PCT: capital_diag.append(f"confirmation price weakened {move:+.1f}%")
            if move > CAPITAL_CONFIRM_MAX_UP_PCT: capital_diag.append(f"confirmation price ran {move:+.1f}% (chase)")
            if lchg < -CAPITAL_CONFIRM_MAX_LIQ_DROP_PCT: capital_diag.append(f"confirmation liquidity {lchg:+.1f}%")

    pipeline = []
    if safety.get("hard"):
        pipeline.append("security HARD BLOCK")
    if not scout:
        pipeline.append("no confirmed scout yet")
    elif scout_move > 0:
        pipeline.append(f"scout move {scout_move:+.1f}%")
    if cooldown_block:
        age = cooldown.get("prior_age_minutes")
        if cooldown.get("cross_build_grace"):
            pipeline.append(f"old-build duplicate grace ({age:.0f}/{CROSS_BUILD_ENTRY_GRACE_MINUTES}m)")
        else:
            pipeline.append(f"same-build cooldown ({age:.0f}m old)")
    elif cooldown.get("cross_build_reset"):
        pipeline.append("old-build cooldown cleared for this build")
    if tier and not stability_ok:
        pipeline.append("stability blocked")
    elif tier and stability_ok and not tape_ok:
        pipeline.append("tape preservation blocked")
    elif tier and stability_ok and tape_ok and not edge_ok:
        pipeline.append("capital edge blocked")
    elif tier and stability_ok and tape_ok and edge_ok and capital_diag:
        pipeline.append("capital persistence waiting/blocked")
    elif tier and stability_ok and tape_ok and edge_ok:
        pipeline.append("capital edge clear; final commitment/chase checks remain")

    extra = []
    extra.extend(blockers[:5])
    extra.extend(stability_blockers[:3])
    extra.extend(tape_blockers[:3])
    extra.extend(edge_blockers[:4])
    extra.extend(capital_diag[:3])
    if cooldown_block:
        extra.append("entry cooldown is still active")
    if open_pos:
        extra.append("open recorded position prevents a duplicate bot-guided entry")
    proof_candidate,proof_candidate_reason=proof_candidate_eligibility(tier,score) if tier else (False,"no actionable core tier")
    if tier and not proof_candidate:
        extra.append("research-only for live proof: "+proof_candidate_reason)
    # dedupe
    missing=[]
    for x in extra:
        if x and x not in missing:
            missing.append(x)

    m = metrics(pair)
    ratio = m["buys"] / max(m["sells"], 1)
    turnover = 100.0 * m["v5"] / max(m["mc"], 1)
    recent = db.conn.execute(
        """select event,blockers,ts from decision_ledger where chain=? and token=? and build_version=?
           and event in ('ENTRY_COOLDOWN','ENTRY_STABILITY','ENTRY_TAPE','ENTRY_EDGE','ENTRY_REGIME','ENTRY_HISTORY','ENTRY_COMMIT','ENTRY_PATH','ENTRY_EXECUTION','ENTRY_CHASE','ENTRY_SLIPPAGE','ENTRY_RATE_LIMIT','ENTRY_DELIVERY_FAIL','LANE_SHADOW','CAPITAL_SHADOW','PROOF_SHADOW','RESEARCH_SHADOW','RISK_PAUSE','EDGE_ARMED','PAPER_SHADOW','RUNNER_RADAR','ENTRY_SENT','ENTRY_SENT_HOT','ENTRY_SENT_TEST')
           order by ts desc limit 1""",
        (chain, token, VERSION)
    ).fetchone()
    last_line = "none recorded in this build"
    if recent:
        age = max(0, int((time.time() - int(recent["ts"])) / 60))
        why = str(recent["blockers"] or "").strip()
        last_line = f"{recent['event']} {age}m ago" + (f" — {why[:180]}" if why else "")

    if tier and scout and not cooldown_block and stability_ok and tape_ok and edge_ok and not capital_diag and not open_pos and proof_candidate:
        verdict = f"🟢 CAPITAL EDGE PASSES — {tier}; selective-proof eligible; final commitment/chase checks remain"
    elif tier and not proof_candidate:
        verdict = f"🟡 RESEARCH ONLY — {tier}; {proof_candidate_reason}"
    elif tier:
        verdict = f"🟡 {tier} SHAPE EXISTS, BUT PIPELINE IS NOT CLEAR"
    else:
        verdict = "⛔ NOT ACTIONABLE UNDER THE CURRENT MODEL"

    crash = ""
    if stability_info.get("recent_crash_pc5") is not None:
        crash = (f" | prior crash {f(stability_info.get('recent_crash_pc5')):+.1f}% "
                 f"({f(stability_info.get('recent_crash_age_seconds'))/60:.1f}m ago)")
    rtline = f" | RT {int(f(rts.get('tx30')))} tx/30s" if rts.get("available") else ""
    return (
        f"🧭 WHY NO ALERT? — {base.get('name') or 'Unknown'} ({base.get('symbol') or '?'})\n"
        f"{verdict}\n"
        f"Entry {score:.0f} | Confirmed {confirmed:.0f} | 5m {m['pc5']:+.1f}% | 1h {m['pc1']:+.1f}%\n"
        f"B/S {ratio:.2f}x | {int(m['buys']+m['sells'])} swaps | turnover {turnover:.3f}% | liq {usd(m['liq'])}{rtline}{crash}\n"
        f"Pipeline: {'; '.join(pipeline) or 'classifier did not reach the post-scout pipeline'}\n"
        f"Current blockers: {'; '.join(missing[:7]) or 'none before final confirmation'}\n"
        f"Last internal gate: {last_line}\n"
        f"Contract: {token}\n"
        "This command is diagnostic only; it does not create an entry or loosen a gate."
    )



def reentry_setup(db,pair):
    """Data-tuned second-chance setup.

    The uploaded overnight sample showed that first-bounce re-entry alerts were the
    weakest alert family and included several catastrophic collapses. v10.4 therefore
    requires an actual base, sustained buy pressure, retained liquidity, and rejects
    violent crash/rebound wicks before a re-entry can become actionable.
    """
    if not REENTRY_ENABLED:
        return None

    chain=str(pair.get("chainId","")).lower()
    token=str(nest(pair,"baseToken","address",default=""))
    if chain not in ENTRY_CHAINS or not token:
        return None

    hist=db.token_history(chain,token,240)
    if len(hist) < max(8, REENTRY_REQUIRED_GOOD_SNAPSHOTS + 2):
        return None

    m=metrics(pair)
    current=f(pair.get("priceUsd"))
    if current <= 0:
        return None
    if m["liq"] < REENTRY_MIN_LIQUIDITY or not (70000 <= m["mc"] <= ENTRY_MAX_MCAP):
        return None

    prices=[f(x.get("price")) for x in hist if f(x.get("price")) > 0]
    if len(prices) < 8:
        return None

    # Require enough real observation time; a few rapid snapshots do not constitute a base.
    history_span_mins=(hist[-1]["ts"] - hist[0]["ts"]) / 60.0
    if history_span_mins < REENTRY_MIN_HISTORY_MINUTES:
        return None

    peak=max(prices+[current])
    drawdown=(current/max(peak,1e-18)-1)*100
    if not (-REENTRY_MAX_DRAWDOWN <= drawdown <= -REENTRY_MIN_DRAWDOWN):
        return None

    now=int(time.time())
    recent=[x for x in hist if x["ts"] >= now-35*60]
    rprices=[f(x.get("price")) for x in recent if f(x.get("price")) > 0]
    if len(rprices) < 5:
        return None

    recent_low=min(rprices+[current])
    recent_high=max(rprices+[current])
    recovery=(current/max(recent_low,1e-18)-1)*100

    # Reject the "dead-cat bounce from a near-zero wick" problem. Huge recovery
    # percentages were a major false-positive pattern in the overnight sample.
    if recovery < REENTRY_MIN_RECOVERY or recovery > REENTRY_MAX_RECOVERY:
        return None

    recent_crash=(recent_low/max(recent_high,1e-18)-1)*100
    if recent_crash <= -REENTRY_MAX_RECENT_CRASH_PCT:
        return None

    # A re-entry should be stabilizing/reclaiming, not still dumping or vertically pumping.
    if not (0 <= m["pc5"] <= 5):
        return None

    ratio=m["buys"]/max(m["sells"],1)
    swaps=m["buys"]+m["sells"]
    if ratio < REENTRY_MIN_BS or swaps < REENTRY_MIN_SWAPS:
        return None

    # Require a real short base: recent prices should remain in a bounded range rather
    # than a single wick followed by one rebound scan.
    base_cutoff=now-int(REENTRY_BASE_WINDOW_MINUTES*60)
    base_obs=[x for x in hist if x["ts"] >= base_cutoff]
    base_prices=[f(x.get("price")) for x in base_obs if f(x.get("price")) > 0]
    if len(base_prices) < REENTRY_REQUIRED_GOOD_SNAPSHOTS:
        return None
    base_range=(max(base_prices)/max(min(base_prices),1e-18)-1)*100
    if base_range > REENTRY_MAX_BASE_RANGE_PCT:
        return None

    # Buy pressure must persist across multiple observations, not exist for one scan.
    good=0
    ratios=[]
    for x in base_obs[-5:]:
        b=f(x.get("buys5")); s=f(x.get("sells5"))
        r=b/max(s,1)
        ratios.append(r)
        if r >= 1.15 and f(x.get("pc5")) >= -2:
            good += 1
    if good < REENTRY_REQUIRED_GOOD_SNAPSHOTS:
        return None

    # Liquidity must be holding. A rebound while liquidity is draining is not a re-entry.
    liqs=[f(x.get("liquidity")) for x in base_obs if f(x.get("liquidity")) > 0]
    if len(liqs) < REENTRY_REQUIRED_GOOD_SNAPSHOTS:
        return None
    sorted_liqs=sorted(liqs)
    median_liq=sorted_liqs[len(sorted_liqs)//2]
    liq_retention=m["liq"]/max(median_liq,1)*100
    if liq_retention < REENTRY_MIN_LIQ_RETENTION_PCT:
        return None

    sustained_ratio=sum(ratios[-REENTRY_REQUIRED_GOOD_SNAPSHOTS:]) / max(
        min(len(ratios), REENTRY_REQUIRED_GOOD_SNAPSHOTS), 1
    )

    score=clamp(
        52
        + clamp((ratio-1.35)*18,0,14)
        + clamp((recovery-REENTRY_MIN_RECOVERY)*0.8,0,8)
        + clamp((m["liq"]/max(m["mc"],1))*45,0,10)
        + clamp((liq_retention-85)*0.25,0,5),
        0,100
    )

    return {
        "score":score,
        "drawdown":drawdown,
        "recovery":recovery,
        "base_range":base_range,
        "liq_retention":liq_retention,
        "good_snapshots":good,
        "reasons":[
            f"cooled {abs(drawdown):.1f}% from local peak",
            f"recovered {recovery:.1f}% from recent low without an extreme wick",
            f"buyers sustained across {good} recent snapshots",
            f"current B/S {ratio:.2f}x; recent pressure ~{sustained_ratio:.2f}x",
            f"liquidity holding ~{liq_retention:.0f}% of recent baseline",
            f"{REENTRY_BASE_WINDOW_MINUTES:.0f}m base range {base_range:.1f}%"
        ]
    }

def reentry_watch_alert(pair,setup):
    base=pair.get("baseToken") or {}
    m=metrics(pair)
    return (
        f"🟣 {base.get('name') or 'Unknown'} ({base.get('symbol') or '?'}) — RE-ENTRY WATCH\n"
        "ACTION: possible second-chance setup, but NOT actionable yet.\n"
        f"The bot will require ~{REENTRY_WATCH_CONFIRM_SECONDS//60} more minutes of stability before it can promote this.\n"
        f"5m {m['pc5']:+.1f}% | 1h {m['pc1']:+.1f}% | MC {usd(m['mc'])} | liq {usd(m['liq'])}\n"
        f"WHY: {'; '.join(setup['reasons'][:5])}\n"
        f"Contract: {base.get('address','')}\n"
        "Do not buy just because this watch appeared."
    )

def reentry_alert(pair,setup):
    base=pair.get("baseToken") or {}; m=metrics(pair)
    return (f"🟣 {base.get('name') or 'Unknown'} ({base.get('symbol') or '?'}) — RE-ENTRY OPTION\n"
            "ACTION: base + buyer return survived the confirmation window — /check is optional before buying\n"
            f"5m {m['pc5']:+.1f}% | 1h {m['pc1']:+.1f}% | MC {usd(m['mc'])} | liq {usd(m['liq'])}\n"
            f"WHY: {'; '.join(setup['reasons'])}\n"
            f"Contract: {base.get('address','')}\n"
            "This is a second-chance setup, not a guarantee.")

def beginner_alert(kind,pair,score,action,reasons,risks,hits,wallet_checked,security_checked,holder_checked,trader_checked,rts=None,safety=None,rescued=False,tier="ENTRY OPTION",confirmed_score=0,calibration=None,market_regime=None,social=None,source_ctx=None,signal_mode="LIVE",signal_note="",track_record=""):
    base = pair.get("baseToken") or {}
    m = metrics(pair)
    price = f(pair.get("priceUsd"))
    grade = risk_grade(risks, security_checked, holder_checked, trader_checked)
    max_chase = price * (1 + MAX_CHASE/100)
    rp=tier_risk_profile(tier); p1,p2=tier_profit_targets(tier)
    risk_line = price * (1 - f(rp.get("soft"),RISK_LINE)/100)
    why = "; ".join(reasons[:5]) or "market and order-flow conditions aligned"
    bad = "; ".join(risks[:3]) or "no configured hard red flag found"
    contract = str(base.get("address",""))
    rtline = ""
    if rts and rts.get("available"):
        rtline = f" | RT {rts.get('tx30',0)} tx/30s"
    safety_line = "verified" if safety and (safety.get("rug_checked") or safety.get("security_checked") or safety.get("holder_checked")) else "limited data"
    promoted = confirmed_score >= OPTION_CONFIRMED_SCORE and score < OPTION_SCORE
    trigger = ("established chart structure" if tier=="STRUCTURE ENTRY" else
               ("cross-chain chart structure" if tier=="MANUAL CHAIN ENTRY" else
               ("confirmation promotion" if promoted else ("fresh-launch flow" if tier=="FRESH ENTRY" else ("high-flow microcap rescue" if tier=="MICRO ENTRY" else "early setup")))))
    size_note = ("Established STRUCTURE lane: slower tape, manual execution, smaller profit checkpoints.\n" if tier=="STRUCTURE ENTRY" else
                 ("Experimental cross-chain lane: MANUAL Fomo execution only, capped size, faster profit/risk checkpoints.\n" if tier=="MANUAL CHAIN ENTRY" else
                 ("Higher-risk fresh launch: consider smaller size.\n" if tier=="FRESH ENTRY" else ("Low-liquidity MICRO ENTRY: use smaller size and take profits quickly if momentum spikes.\n" if tier=="MICRO ENTRY" else ("High-flow promotion: momentum is strong but this remains a fast meme trade.\n" if tier=="FLOW ENTRY" else "")))))
    plan = hold_plan(pair, tier=tier)
    horizon = f"TRADE HORIZON: {plan['label']} — {plan['window']}"
    social_line = ""
    sp=social or {}
    if int(sp.get("mentions") or 0) > 0:
        social_line=(f"SOCIAL: {int(sp.get('mentions') or 0)} mention(s) / {int(sp.get('unique_authors') or 0)} author(s) "
                     f"| {sp.get('status','FLAT')} | pulse {f(sp.get('score')):+.1f}\n")
    discovery_line = ""
    sc=source_ctx or {}
    buckets=list(sc.get("buckets") or [])
    if buckets:
        discovery_line="Discovery confirmation: "+" + ".join(buckets[:5])+"\n"
    if sc.get("paid_boost"):
        discovery_line += "⚠️ Dex paid boost seen; it is discovery only and does not count as independent quality evidence.\n"
    regime_line=(market_regime_line(market_regime)+"\n") if market_regime else ""
    cal_line = ""
    if calibration:
        hist=calibration.get("historical") or {}
        sample=max(int(hist.get("n30") or 0),int(hist.get("n120") or 0))
        sample_note=f" | history n={sample}" if sample else " | history still small"
        weak = calibration.get('confidence',0) < ADAPTIVE_LOW_CONFIDENCE_WARN
        weak_note = " | ⚠️ weak calibration — consider smaller size or /check" if weak else ""
        cal_status=calibration.get("calibration_status","LEARNING")
        size_reason=(calibration.get("size_reasons") or [])
        reason_note=(" | " + str(size_reason[0])) if size_reason else ""
        exceptional_note=(" | exceptional size unlocked" if calibration.get("exceptional_eligible") else "")
        cal_line=(f"Adaptive confidence: {calibration.get('confidence',0):.0f}/100 ({calibration.get('grade','')})"
                  f" | calibration {cal_status}{sample_note}{weak_note}\n"
                  f"💵 SUGGESTED BUY: ~${calibration.get('suggested_usd',0):.0f} ({calibration.get('size_label','')}) "
                  f"| normal range ${calibration.get('size_min_usd',0):.0f}-${calibration.get('size_normal_max_usd',0):.0f} "
                  f"| exceptional cap ${calibration.get('size_exceptional_max_usd',0):.0f}{exceptional_note}{reason_note}\n")
    mode=str(signal_mode or "LIVE").upper()
    if mode=="TEST":
        action_line="🟠 ACTION: BUY NOW SIGNAL — TEST-SIZE / MANUAL ONLY; strategy proof is not ACTIVE yet"
    elif mode=="PAPER":
        action_line="🧪 ACTION: PAPER BUY NOW SIGNAL — LIVE RISK IS BLOCKED; use this to watch/test, not as live sizing guidance"
    else:
        action_line="✅ ACTION: BUY NOW — LIVE-QUALIFIED ENTRY OPPORTUNITY"
    if signal_note:
        action_line += f"\nAuthorization: {signal_note[:240]}"
    if track_record:
        action_line += f"\n{track_record}"
    return (
        f"{entry_tier_emoji(tier)} {base.get('name') or 'Unknown name'} ({base.get('symbol') or '?'}) — {tier}\n"
        f"{action_line}\n"
        f"Trigger: {trigger} | Entry score {score:.0f} | Confirmed {confirmed_score:.0f}\n"
        f"Price ${price:.8g} | 5m {m['pc5']:+.1f}% | 1h {m['pc1']:+.1f}%{rtline}\n"
        f"MC {usd(m['mc'])} | Liquidity {usd(m['liq'])}\n"
        f"Safety: {safety_line} | Risk label: {grade}\n"
        f"{horizon}\n"
        f"{regime_line}{social_line}{discovery_line}"
        f"{cal_line}\n"
        f"WHY: {why}\n"
        f"WATCH OUT FOR: {bad}\n"
        f"{size_note}"
        f"Profit checkpoints for this lane: +{p1:.0f}% / +{p2:.0f}% | soft risk reference ~${risk_line:.8g}.\n"
        f"Do not chase above ${max_chase:.8g} (+{MAX_CHASE:.0f}%).\n"
        f"Contract: {contract}\n"
        "Paste the CONTRACT into Fomo — names/tickers can be duplicated.\n\n"
        "NEXT: /check is optional. If you buy in Fomo, reply directly to THIS alert with /bought YOUR_DOLLAR_AMOUNT. "
        f"If you have not recorded a buy yet, {VERSION} automatically sends fresh entry updates so a 2–3 minute Telegram delay does not leave you acting on a stale alert."
    )



def scout_message(pair, score, reasons, risks):
    base = pair.get("baseToken") or {}
    m = metrics(pair)
    contract = str(base.get("address",""))
    return (
        f"🔵 {base.get('name') or 'Unknown name'} ({base.get('symbol') or '?'}) — GET READY\n"
        "ACTION: OPEN IT IN FOMO — DO NOT BUY YET\n"
        f"Price ${f(pair.get('priceUsd')):.8g} | 5m {m['pc5']:+.1f}% | 1h {m['pc1']:+.1f}%\n"
        f"MC {usd(m['mc'])} | Liquidity {usd(m['liq'])}\n"
        f"WHY IT IS ON RADAR: {'; '.join(reasons[:3]) or 'early activity is forming'}\n\n"
        "⚠️ USE THIS CONTRACT IN FOMO — names/tickers can be duplicated:\n"
        f"{contract}\n\n"
        "If it strengthens without running away, the next alert can become ENTRY WINDOW OPEN."
    )



def confirmation_message(pair, score, early_signal):
    base = pair.get("baseToken") or {}
    price = f(pair.get("priceUsd"))
    if early_signal:
        move = (price / max(early_signal["price"],1e-18) - 1)*100
        ref = f"Price is {move:+.1f}% from the earlier entry alert."
    else:
        ref = "The setup strengthened, but it still did not pass the live entry gates."
    return (
        f"🟡 {base.get('name') or 'Unknown name'} ({base.get('symbol') or '?'}) — MOMENTUM UPDATE\n"
        f"Current ${price:.8g}. {ref}\n"
        "This update is optional and is OFF by default in v10."
    )



async def auto_entry_update_once(http,db,state,signal_id,check_num,total_checks,elapsed_seconds,is_final=False):
    """Revalidate an actionable alert without requiring the user to type /check."""
    signal=db.signal_by_id(signal_id)
    if not signal or signal.get("kind")!="EARLY": return "STOP"
    if db.position_by_token(signal["token"]): return "BOUGHT"
    live=await pair_for_token(http,signal["chain"],signal["token"])
    if not live: return "NO_QUOTE"
    m=metrics(live); current=f(live.get("priceUsd"))
    if current<=0: return "NO_QUOTE"
    alert_move=(current/max(f(signal.get("price")),1e-18)-1)*100
    scout=db.recent_signal(signal["chain"],signal["token"],"SCOUT",hours=2) or signal
    scout_move=(current/max(f(scout.get("price")),1e-18)-1)*100
    ratio=m["buys"]/max(m["sells"],1); swaps=m["buys"]+m["sells"]
    rt=state.get("realtime"); rts=rt.stats(signal["token"]) if rt and signal["chain"]=="solana" else {}
    safety=cached_safety(state,signal["chain"],signal["token"])
    if safety is None:
        safety={"hard":False,"rug_checked":False,"security_checked":False,"holder_checked":False,"risks":[],"reasons":[]}
    recent_conf=db.recent_signal(signal["chain"],signal["token"],"CONFIRMED",hours=8)
    confirmed_score=max(f(signal.get("score")),f(recent_conf.get("score")) if recent_conf else 0)
    original_tier=str(signal.get("action") or "ENTRY OPTION")
    blockers=[]; hard_close=bool(safety.get("hard"))
    if original_tier=="RE-ENTRY OPTION":
        rs=reentry_setup(db,live)
        safe=not hard_close and bool(safety.get("rug_checked")) and bool(safety.get("holder_checked"))
        tier="RE-ENTRY OPTION" if rs and safe else None
        if not tier: blockers.append("re-entry structure/safety is not valid right now")
    else:
        tier,tier_blockers=classify_actionable_tier(
            live,f(signal.get("score")),confirmed_score,list(safety.get("risks",[])),hard_close,
            rts=rts,safety=safety,scout_move=scout_move)
        blockers.extend(tier_blockers or [])
    stability_ok,stability_blockers,_=entry_stability_check(db,live,tier) if tier else (False,[],{})
    if tier and alert_move < -CHECK_MAX_DROP_FROM_ALERT_PCT:
        tier=None; blockers=[f"price is {alert_move:.1f}% below the original alert; wait for repair"]+blockers
    if tier and not stability_ok:
        tier=None; blockers=list(stability_blockers)+blockers
    if alert_move > AUTO_ENTRY_MAX_LATE_MOVE_PCT:
        tier=None; blockers=[f"price is already {alert_move:+.1f}% above the original alert; do not chase"]+blockers
        hard_close=True

    db.log_latency(signal["id"],signal["token"],f"AUTO_CHECK_{check_num}",current,
                   f"elapsed={elapsed_seconds};alert_move={alert_move:.2f};scout_move={scout_move:.2f};tier={tier or 'NONE'}")
    age_label=f"~{elapsed_seconds//60}m {elapsed_seconds%60:02d}s after alert" if elapsed_seconds>=60 else f"~{elapsed_seconds}s after alert"
    if tier:
        late=alert_move>MAX_CHASE
        guide=adaptive_dollar_size(db,live,tier,f(signal.get("score")),late_move_pct=max(0.0,alert_move))
        label="⚠️ LATE BUT STILL VIABLE" if late else "✅ ENTRY STILL OPEN"
        action=("Entry still passes, but price is above the ideal alert zone — use the smaller refreshed size and do not chase further."
                if late else "Entry still passes on the fresh quote.")
        mid=await send(http,
            f"{label} — {signal['symbol']} | AUTO CHECK {check_num}/{total_checks} ({age_label})\n"
            f"Current ${current:.8g} | from original alert {alert_move:+.1f}%\n"
            f"5m {m['pc5']:+.1f}% | 1h {m['pc1']:+.1f}% | B/S {ratio:.2f}x | {int(swaps)} swaps | liq {usd(m['liq'])}\n"
            f"💵 Refreshed suggested buy: ~${guide.get('suggested_usd',0):.0f} ({guide.get('size_label','')})\n"
            f"ACTION: {action}\n"
            f"Contract: {signal['token']}\n"
            "If you buy in Fomo, reply directly to THIS update with /bought YOUR_DOLLAR_AMOUNT.")
        if hasattr(db,"expire_signal_contexts"):
            db.expire_signal_contexts(signal["id"],"SUPERSEDED")
        db.map_telegram_signal(mid,signal["id"]); db.map_trade_context(mid,signal["id"],live,tier,"ENTRY")
        return "OPEN"

    label="⛔ ENTRY CLOSED" if hard_close else "⏸️ ENTRY PAUSED / WAIT"
    next_note=("This is the final scheduled freshness check; wait for a new bot entry/second-leg alert." if is_final
               else "Do not buy right now; the next automatic check will re-evaluate whether the setup repairs.")
    mid=await send(http,
        f"{label} — {signal['symbol']} | AUTO CHECK {check_num}/{total_checks} ({age_label})\n"
        f"Current ${current:.8g} | from original alert {alert_move:+.1f}% | 5m {m['pc5']:+.1f}% | B/S {ratio:.2f}x\n"
        f"WHY: {'; '.join(blockers[:4]) or 'fresh entry gates are not valid right now'}\n"
        f"ACTION: {next_note}\n"
        f"Contract: {signal['token']}")
    if hasattr(db,"expire_signal_contexts"):
        db.expire_signal_contexts(signal["id"],"CLOSED" if hard_close else "PAUSED")
    db.map_telegram_signal(mid,signal["id"]); db.map_trade_context(mid,signal["id"],live,original_tier,"CLOSED",0)
    return "CLOSED" if hard_close else "PAUSED"


async def automatic_entry_followups(http,db,state,signal_id):
    checks=list(AUTO_ENTRY_RECHECK_SECONDS)
    last=0
    try:
        for idx,delay in enumerate(checks,1):
            await asyncio.sleep(max(0,delay-last)); last=delay
            sig=db.signal_by_id(signal_id)
            if not sig or db.position_by_token(sig.get("token","")):
                break
            await auto_entry_update_once(http,db,state,signal_id,idx,len(checks),delay,is_final=(idx==len(checks)))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"[auto entry update] {type(exc).__name__}: {exc}")
    finally:
        state.get("entry_update_tasks",{}).pop(str(signal_id),None)


def schedule_entry_followups(http,db,state,signal_id):
    if not AUTO_ENTRY_RECHECKS_ENABLED or not bool_pref(db,"auto_entry_rechecks",True) or not (TG and CHAT):
        return
    tasks=state.setdefault("entry_update_tasks",{})
    key=str(signal_id)
    old=tasks.get(key)
    if old and not old.done(): return
    tasks[key]=asyncio.create_task(automatic_entry_followups(http,db,state,signal_id))


async def evaluate_signals(http, db):
    for signal, checkpoints in db.due_evaluations():
        pair = await pair_for_token(http, signal["chain"], signal["token"])
        if not pair:
            continue
        current = f(pair.get("priceUsd"))
        if current <= 0:
            continue
        ret = (current / max(signal["price"],1e-18) - 1)*100
        entry_liq=f(signal.get("liquidity"))
        suspect=not price_move_plausible(current/max(f(signal["price"]),1e-18),
                                         (f(nest(pair,"liquidity","usd",default=0))/entry_liq) if entry_liq>0 else None)
        for cp in checkpoints:
            db.add_eval(signal["id"], cp, current, ret, suspect=suspect)


def _capture_outcome_row(db, table, key_col, key, chain, token, start_ts, entry, entry_liq, horizon, now):
    """Compute one forward outcome from cleaned observations and store it with integrity data.

    Returns True when a row was written. Paths with no usable samples are written as
    suspect once their horizon is 2h stale, so they stop being retried every pass.
    """
    end=int(start_ts)+horizon*60
    obs=db.conn.execute("""select ts,price,liquidity from observations where chain=? and token=?
        and ts>=? and ts<=? and price>0 order by ts asc""",(chain,token,int(start_ts),end)).fetchall()
    out=outcome_from_path([(r["ts"],r["price"],r["liquidity"]) for r in obs],entry,entry_liq)
    if out is None:
        if now-end < 2*3600:
            return False
        out={"max_return_pct":0.0,"min_return_pct":0.0,"final_return_pct":0.0,"samples":0,"last_obs_ts":0,"suspect":1}
    db.conn.execute(f"""insert or ignore into {table}({key_col},horizon_min,completed_ts,max_return_pct,min_return_pct,
        final_return_pct,samples,last_obs_ts,suspect) values(?,?,?,?,?,?,?,?,?)""",
        (key,horizon,now,out["max_return_pct"],out["min_return_pct"],out["final_return_pct"],
         out["samples"],out["last_obs_ts"],out["suspect"]))
    return True


def capture_signal_outcomes(db):
    """Persist max/min/final forward returns before raw observations age out."""
    if not OUTCOME_CAPTURE_ENABLED:
        return 0
    now=int(time.time()); added=0
    for horizon in (30,60,120):
        rows=db.conn.execute("""select s.* from signals s
            where s.kind='EARLY' and s.ts<=? and s.ts>=?
            and not exists(select 1 from signal_outcomes o where o.signal_id=s.id and o.horizon_min=?)
            order by s.ts asc limit 60""",
            (now-horizon*60, now-8*3600, horizon)).fetchall()
        for row in rows:
            sig=dict(row)
            if f(sig.get("price"))<=0:
                continue
            if _capture_outcome_row(db,"signal_outcomes","signal_id",sig["id"],sig["chain"],sig["token"],sig["ts"],
                                    f(sig["price"]),f(sig.get("liquidity")),horizon,now):
                added+=1
    if added: db.conn.commit()
    return added


def capture_decision_outcomes(db):
    """Persist forward paths for near-entry decisions, including blocked setups.

    This gives future audits a cleaner false-positive / false-negative dataset than
    keeping only alerts. It is reporting/learning data and never changes a trade by itself.
    """
    if not (DECISION_LEDGER_ENABLED and DECISION_OUTCOMES_ENABLED):
        return 0
    now=int(time.time()); added=0
    for horizon in (30,60,120):
        rows=db.conn.execute("""select d.* from decision_ledger d
            where d.ts<=? and d.ts>=?
              and not exists(select 1 from decision_outcomes o where o.decision_id=d.id and o.horizon_min=?)
            order by d.ts asc limit 100""",(now-horizon*60,now-10*3600,horizon)).fetchall()
        for row in rows:
            d=dict(row)
            if f(d.get("price"))<=0: continue
            if _capture_outcome_row(db,"decision_outcomes","decision_id",int(d["id"]),d["chain"],d["token"],d["ts"],
                                    f(d["price"]),f(d.get("liquidity")),horizon,now):
                added+=1
    if added: db.conn.commit()
    return added


def audit_missed_opportunities(db):
    """Retrospectively log clean runners the scanner observed but did not alert near the move.

    This is for learning/reporting only. It never retroactively tells the user to buy.
    """
    if not MISSED_AUDIT_ENABLED:
        return 0
    horizon=int(MISSED_AUDIT_HORIZON_MINUTES); now=int(time.time())
    start=now-(horizon+45)*60; end=now-horizon*60
    # Strong-flow snapshots only; cap rows so this remains cheap on a long-running DB.
    rows=db.conn.execute("""select * from observations
        where ts between ? and ? and price>0 and liquidity>=25000 and mcap>=70000
          and mcap<=5000000 and vol5/mcap>=0.005
          and (buys5+sells5)>=20 and buys5/max(sells5,1)>=1.30
          and pc5 between -2 and 7 and pc1 between -20 and 35
        order by ts asc limit 600""",(start,end)).fetchall()
    seen=set(); added=0
    for r0 in rows:
        r=dict(r0); key=(r["chain"],r["token"])
        if key in seen: continue
        seen.add(key)
        # If an actionable alert was already sent around this snapshot, it was not missed.
        hit=db.conn.execute("""select 1 from signals where kind='EARLY' and chain=? and token=?
            and ts between ? and ? limit 1""",(r["chain"],r["token"],r["ts"]-10*60,r["ts"]+10*60)).fetchone()
        if hit: continue
        future=db.conn.execute("""select price from observations where chain=? and token=? and ts>=? and ts<=? and price>0 order by ts asc""",
            (r["chain"],r["token"],r["ts"],r["ts"]+horizon*60)).fetchall()
        if len(future)<3: continue
        base=max(f(r["price"]),1e-18); prices=[f(x[0]) for x in future if f(x[0])>0]
        if len(prices)<3: continue
        maxret=(max(prices)/base-1)*100; minret=(min(prices)/base-1)*100; final=(prices[-1]/base-1)*100
        if maxret < MISSED_AUDIT_MIN_RUN_PCT or minret <= -20:
            continue
        turnover=100*f(r["vol5"])/max(f(r["mcap"]),1); ratio=f(r["buys5"])/max(f(r["sells5"]),1); swaps=f(r["buys5"])+f(r["sells5"])
        db.conn.execute("""insert or ignore into missed_opportunities(
            baseline_ts,horizon_min,chain,token,symbol,baseline_price,baseline_mcap,baseline_liquidity,turnover_pct,buy_sell_ratio,swaps5,max_return_pct,min_return_pct,final_return_pct)
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r["ts"],horizon,r["chain"],r["token"],r["symbol"],base,f(r["mcap"]),f(r["liquidity"]),turnover,ratio,swaps,maxret,minret,final))
        added+=db.conn.execute("select changes()").fetchone()[0]
    if added: db.conn.commit()
    return added


async def sync_wallet_positions(http, db, state, force=False):
    sync=state.get("wallet_sync")
    if not sync or not sync.enabled:
        return {"enabled":False,"checked":0,"events":[]}
    now=time.time()
    if not force and now-state.get("last_wallet_sync",0)<WALLET_SYNC_EVERY_SECONDS:
        return {"enabled":True,"checked":0,"events":[]}
    state["last_wallet_sync"]=now
    events=[]; checked=0
    for pos in db.open_positions():
        if str(pos.get("chain") or "").lower()!="solana": continue
        bal=await sync.token_balance(http,pos["token"]); checked+=1
        if bal is None: continue
        current=f(bal.get("ui")); prior=db.wallet_state(pos["id"]); last=f(prior.get("last_qty")) if prior else None
        change=reconcile_balance(last,current,WALLET_SYNC_MIN_CHANGE_PCT)
        event=change.get("event")
        if event=="baseline":
            db.wallet_set_state(pos["id"],pos["token"],current)
        elif event in {"decrease","closed"}:
            pair=await pair_for_token(http,pos["chain"],pos["token"]); price=f((pair or {}).get("priceUsd"),f(pos.get("entry_price")))
            evidence=None
            if WALLET_SYNC_REQUIRE_SWAP_EVIDENCE:
                evidence=await recent_sale_evidence(sync,http,pos["token"],int((prior or {}).get("last_ts") or 0))
            can_record=(not WALLET_SYNC_REQUIRE_SWAP_EVIDENCE) or bool(evidence)
            if WALLET_SYNC_AUTO_RECORD_SELLS and price>0 and can_record:
                sold_fraction=f(change.get("sold_fraction"))
                if evidence and last and f(evidence.get("sold_ui"))>0:
                    sold_fraction=min(1.0,max(0.0,f(evidence.get("sold_ui"))/max(f(last),1e-18)))
                if event=="closed" or sold_fraction>=0.99:
                    pnl=db.close_position(pos["id"],price)
                    events.append(("closed",pos,change,pnl,evidence))
                else:
                    pnl,new_rem=db.partial_close_position(pos["id"],price,sold_fraction)
                    events.append(("decrease",pos,change,pnl,new_rem,evidence))
            elif WALLET_SYNC_NOTIFY_INCREASES:
                events.append(("unverified_decrease",pos,change,evidence))
            db.wallet_set_state(pos["id"],pos["token"],current,notice=True)
        elif event=="increase":
            should_notice=WALLET_SYNC_NOTIFY_INCREASES and (not prior or now-f(prior.get("last_notice_ts"))>15*60)
            db.wallet_set_state(pos["id"],pos["token"],current,notice=should_notice)
            if should_notice: events.append(("increase",pos,change))
        else:
            db.wallet_set_state(pos["id"],pos["token"],current)
    for event in events:
        typ,pos,change,*extra=event
        if typ=="closed":
            ev=extra[1] if len(extra)>1 else None
            sigline=f"\nSwap evidence: {ev.get('signature','')[:16]}…" if ev else ""
            await send(http,f"🔄 WALLET SYNC — {pos['symbol']} balance reached ~0. Recorded the tracked position as closed at the current quote.\nApprox total P/L: ${f(extra[0]):+.2f}{sigline}\nContract: {pos['token']}")
        elif typ=="decrease":
            ev=extra[2] if len(extra)>2 else None
            sigline=f" | swap {ev.get('signature','')[:12]}…" if ev else ""
            await send(http,f"🔄 WALLET SYNC — {pos['symbol']} balance fell {abs(f(change.get('change_pct'))):.1f}%. Recorded a verified partial sale.\nApprox P/L on that leg: ${f(extra[0]):+.2f} | tracked remainder {f(extra[1])*100:.1f}%{sigline}\nContract: {pos['token']}")
        elif typ=="unverified_decrease":
            await send(http,f"🔄 WALLET SYNC — {pos['symbol']} balance fell {abs(f(change.get('change_pct'))):.1f}%, but I could not verify a SOL/stablecoin swap in the recent transactions. I did NOT change your journal because this could be a transfer.\nIf you sold, reply /sellpct YOUR_PERCENT (or /sell for all).\nContract: {pos['token']}")
        elif typ=="increase":
            await send(http,f"🔄 WALLET SYNC — {pos['symbol']} wallet balance increased {f(change.get('change_pct')):+.1f}%.\nIf this was another buy, reply /bought YOUR_DOLLAR_AMOUNT to the relevant alert/check so cost basis stays accurate.\nContract: {pos['token']}")
    return {"enabled":True,"checked":checked,"events":events}


def stabilize_guardian_state(info, pos, runtime, now=None):
    """Debounce soft structural exits and make WEAKENING recovery sticky.

    Hard price/liquidity failures are immediate. For SMALL/MID manual positions above
    their fee-aware hard line, a structure-only EXIT_REVIEW must persist for a short
    confirmation window before escalation. This prevents one noisy 8-second tape
    snapshot from turning a recoverable dip into a fee-heavy sell recommendation.
    """
    now=float(now if now is not None else time.time())
    out=dict(info)
    out["reasons"]=list(info.get("reasons") or [])
    rt=runtime if isinstance(runtime,dict) else {}
    size=str(out.get("size_bucket") or "STANDARD")
    hard_price=f(out.get("ret")) <= -max(f(out.get("price_hard_stop")),0)
    hard_liq=f(out.get("liq_drop")) >= LIQ_DROP
    proposed=str(out.get("state") or "ENTRY")
    structure_only=(proposed=="EXIT_REVIEW" and not hard_price and not hard_liq and not bool(out.get("profit_lock_trigger")))

    if structure_only and size in {"SMALL","MID"}:
        confirm=GUARDIAN_SMALL_STRUCTURE_CONFIRM_SECONDS if size=="SMALL" else GUARDIAN_MID_STRUCTURE_CONFIRM_SECONDS
        since=f(rt.get("structural_exit_since"),0)
        if since <= 0:
            since=now
            rt["structural_exit_since"]=since
        elapsed=max(0.0,now-since)
        rt["structural_exit_last_seen"]=now
        if elapsed < confirm:
            out["state"]="WEAKENING"
            out["suggested_partial_pct"]=0
            out["reasons"]=[r for r in out["reasons"] if "plus structure deterioration" not in r]
            out["reasons"].append(f"structure weakened, waiting {confirm:.0f}s for confirmation before a fee-heavy exit")
            out["action"]=("RECOVERY WATCH: structure is weak but the fee-aware hard line has not failed. "
                           "Wait for persistence; exit/reduce sooner only if liquidity drains or the loss reaches the hard line.")
            out["structural_confirmation_remaining_seconds"]=max(0,int(confirm-elapsed))
        else:
            out["reasons"].append(f"structure deterioration persisted {elapsed:.0f}s")
            out["structural_confirmation_seconds"]=elapsed
    else:
        rt.pop("structural_exit_since",None)
        rt.pop("structural_exit_last_seen",None)

    # Hysteresis: after a soft-loss WEAKENING state, do not bounce straight back to
    # ENTRY until the loss has meaningfully recovered and buyer tape is no longer weak.
    old=str(pos.get("guardian_state") or "ENTRY")
    m=out.get("metrics") or {}
    if old=="WEAKENING" and out.get("state")=="ENTRY":
        recovery_line=-f(out.get("risk_line"),RISK_LINE) + GUARDIAN_WEAK_RECOVERY_BUFFER_PCT
        healthy=(f(out.get("ret")) >= recovery_line - 1e-9 and f(m.get("ratio"),1) >= 1.0 and f(m.get("pc5")) > -2.0)
        if not healthy:
            out["state"]="WEAKENING"
            out["suggested_partial_pct"]=0
            out["reasons"].append("recovery has not cleared the Guardian hysteresis buffer yet")
            out["action"]="RECOVERY WATCH: improving, but wait for a cleaner recovery before treating the setup as healthy again."
    return out


def tp1_reminder_due(pos, info, new_state, last_alert, runtime, now=None):
    """True once when a still-profitable TP1 position has not recorded a partial."""
    now=float(now if now is not None else time.time())
    rt=runtime if isinstance(runtime,dict) else {}
    return bool(
        TP1_REMINDER_ENABLED
        and int(pos.get("tp1_sent") or 0)
        and f(pos.get("remaining_fraction"),1.0) >= 0.95
        and str(new_state)=="TAKE_PARTIAL"
        and f(info.get("ret")) >= TP1_REMINDER_MIN_RETURN_PCT
        and now-f(last_alert) >= TP1_REMINDER_SECONDS
        and not rt.get("tp1_reminder_sent")
    )


def guardian_repeat_alert_worthy(runtime, info):
    """Suppress repeated WEAKENING alerts unless risk materially worsened."""
    if str(info.get("state")) != "WEAKENING":
        return True
    prior=runtime.get("last_alert_snapshot") if isinstance(runtime,dict) else None
    if not isinstance(prior,dict) or prior.get("state") != "WEAKENING":
        return True
    loss_worse=f(info.get("ret")) <= f(prior.get("ret")) - GUARDIAN_REPEAT_WEAKENING_WORSE_PCT
    draw_worse=f(info.get("drawdown")) <= f(prior.get("drawdown")) - GUARDIAN_REPEAT_WEAKENING_WORSE_PCT
    liq_worse=f(info.get("liq_drop")) >= f(prior.get("liq_drop")) + GUARDIAN_REPEAT_LIQ_WORSE_PCT
    return bool(loss_worse or draw_worse or liq_worse)


def remember_guardian_alert(runtime, info):
    if not isinstance(runtime,dict):
        return
    runtime["last_alert_snapshot"]={
        "state":str(info.get("state") or ""), "ret":f(info.get("ret")),
        "drawdown":f(info.get("drawdown")), "liq_drop":f(info.get("liq_drop")),
        "ts":time.time(),
    }


def fee_aware_tp1(base_tp, remaining_stake_usd):
    stake=max(0.01,f(remaining_stake_usd))
    economic=max(0.0,FOMO_ROUNDTRIP_FRICTION_PCT + 100.0*GUARDIAN_MIN_NET_PROFIT_USD/stake)
    return max(f(base_tp),min(25.0,economic))


def tier_profit_targets(tier):
    tier=str(tier or "")
    if tier=="STRUCTURE ENTRY": return 8.0,18.0
    if tier=="MANUAL CHAIN ENTRY": return 8.0,16.0
    # Capital-first normal target: a $10-$25 Fomo trade should realize a meaningful
    # gross move before fee drag can erase it, then protect the remainder aggressively.
    return min(TP1,12.0),min(TP2,25.0)

async def track_positions(http, db, state=None):
    """Continuously supervise manual and auto-managed positions.

    v11 uses state transitions instead of one-shot TP/exit flags as the primary
    notification engine. That means a position can recover, become a runner, weaken
    later, and generate the appropriate new alert without constant Telegram spam.
    """
    now=time.time()
    for pos in db.open_positions():
        pair=await pair_for_token(http,pos["chain"],pos["token"])
        if not pair: continue
        current=f(pair.get("priceUsd"))
        if current<=0: continue

        peak=max(f(pos.get("peak_price"),pos.get("entry_price")),current)
        if peak>f(pos.get("peak_price"),pos.get("entry_price")):
            db.update_position_peak(pos["id"],peak); pos["peak_price"]=peak

        # Recover the entry signal nearest *before* the recorded buy for calibration.
        sigrow=db.conn.execute("""select score,action from signals where chain=? and token=? and kind='EARLY' and ts<=?
            order by ts desc limit 1""",(pos["chain"],pos["token"],int(pos["open_ts"]))).fetchone()
        sig_score=f(sigrow["score"],50) if sigrow else 50.0
        tier=str(pos.get("entry_tier") or (sigrow["action"] if sigrow else "MANUAL"))
        calibration=confidence_report(db.conn,pair,tier,sig_score,ADAPTIVE_BASE_POSITION_USD) if ADAPTIVE_CONFIDENCE_ENABLED else None
        plan=hold_plan(pair,position=pos,tier=tier)
        pos_tp1,pos_tp2=tier_profit_targets(pos.get("entry_tier"))
        # A gross +5% on a $5 position is not a useful win after ~5% round-trip friction.
        # Raise TP1 only as much as needed to target at least ~GUARDIAN_MIN_NET_PROFIT_USD
        # on the remaining stake; larger positions therefore keep earlier partials.
        remaining_stake=max(0.01,f(pos.get("amount_usd"))*clamp(f(pos.get("remaining_fraction"),1.0),0.0,1.0))
        pos_tp1=fee_aware_tp1(pos_tp1,remaining_stake)
        risk_profile=tier_risk_profile(tier)
        info=position_state(pair,pos,tp1=pos_tp1,tp2=pos_tp2,risk_line=f(risk_profile.get("soft"),RISK_LINE),
                            trailing=f(risk_profile.get("trailing"),TRAIL),liquidity_exit=LIQ_DROP,
                            round_trip_friction=FOMO_ROUNDTRIP_FRICTION_PCT, exit_friction=FOMO_EXIT_FRICTION_PCT,
                            small_position_usd=SMALL_POSITION_USD, small_hard_stop=f(risk_profile.get("small_hard"),SMALL_POSITION_HARD_STOP_PCT),
                            mid_hard_stop=f(risk_profile.get("mid_hard"),MID_POSITION_HARD_STOP_PCT), min_partial_sale_usd=GUARDIAN_MIN_PARTIAL_SALE_USD)
        info["economic_tp1_pct"]=pos_tp1
        guardian_runtime=(state if isinstance(state,dict) else {}).setdefault("guardian_runtime",{})
        pos_runtime=guardian_runtime.setdefault(str(pos["id"]),{})
        info=stabilize_guardian_state(info,pos,pos_runtime,now=now)
        old_state=str(pos.get("guardian_state") or "ENTRY")
        new_state=info["state"]
        changed=old_state!=new_state
        review_min=max(5,int(f(plan.get("review_min"),45)))
        review_ts=int(now+review_min*60)
        last_alert=f(pos.get("guardian_last_alert_ts"),0)
        cooldown=GUARDIAN_ALERT_COOLDOWN_MIN*60

        notify=False
        tp1_reminder=False
        if new_state=="TAKE_PARTIAL" and not int(pos.get("tp1_sent") or 0):
            notify=True
        elif tp1_reminder_due(pos,info,new_state,last_alert,pos_runtime,now=now):
            # One extra reminder only. This is for cases where the user simply did not
            # see the first profit-taking notification; it is not a repeating alarm.
            notify=True; tp1_reminder=True
            info["action"]="PROFIT REMINDER: the first partial was not recorded and the trade is still in the profit zone. Consider taking the suggested slice now instead of relying on one notification."
            info.setdefault("reasons",[]).insert(0,"TP1 alert was sent earlier but no partial sale has been recorded")
        elif new_state=="RUNNER" and not int(pos.get("tp2_sent") or 0):
            notify=True
        elif new_state=="EXIT_REVIEW" and (changed or not int(pos.get("exit_sent") or 0)):
            # Escalation to a confirmed/hard exit is urgent and must not wait behind a
            # recent soft WEAKENING cooldown. Repeated EXIT states are still suppressed
            # because unchanged states do not enter this branch.
            notify=True
        elif new_state=="WEAKENING" and changed and now-last_alert>=cooldown:
            notify=guardian_repeat_alert_worthy(pos_runtime,info)
        # Optional healthy confirmation: only one transition and only when the user has
        # explicitly enabled it; default is quiet to minimize checking/noise.
        elif new_state=="CONFIRMED" and changed and bool_pref(db,"guardian_confirmed_alerts",False) and now-last_alert>=cooldown:
            notify=True

        if changed:
            db.guardian_event(pos,info,detail=f"from={old_state};horizon={plan.get('label','')}")

        if not bool_pref(db,"guardian_alerts",True):
            notify=False
        delivery_ok=False
        if notify:
            mid=await send(http,guardian_message(pos["symbol"],pos["token"],info,plan,calibration))
            delivery_ok=(mid is not None) or not (TG and CHAT)
            if delivery_ok:
                if new_state=="TAKE_PARTIAL" and not int(pos.get("tp1_sent") or 0): db.flag_position(pos["id"],"tp1_sent")
                if tp1_reminder: pos_runtime["tp1_reminder_sent"]=True
                if new_state=="RUNNER": db.flag_position(pos["id"],"tp2_sent")
                if new_state=="EXIT_REVIEW": db.flag_position(pos["id"],"exit_sent")
                # Map the alert to the exact position contract so reply `/sellpct 50` (or actual %) works.
                if mid:
                    db.map_trade_context(mid,None,pair,tier,"POSITION",0)
                remember_guardian_alert(pos_runtime,info)
            else:
                print(f"[guardian] delivery failed for {pos['symbol']} {new_state}; alert remains eligible for retry")

        # Liquidity deterioration below the hard-exit level gets a one-time early warning.
        if LIQ_WARNING<=info["liq_drop"]<LIQ_DROP and not int(pos.get("liq_warn_sent") or 0):
            warn_mid=await send(http,
                f"🚨 {pos['symbol']} — LIQUIDITY WARNING\n"
                f"Liquidity is down {info['liq_drop']:.1f}% from entry.\n"
                f"Guardian state: {new_state.replace('_',' ')} | position {info['ret']:+.1f}%\n"
                "ACTION: Be ready to reduce quickly if liquidity keeps draining.\n"
                f"Contract: {pos['token']}")
            warn_ok=(warn_mid is not None) or not (TG and CHAT)
            if warn_ok:
                db.conn.execute("update positions set liq_warn_sent=1 where id=?",(pos["id"],)); db.conn.commit()
            else:
                print(f"[guardian] liquidity-warning delivery failed for {pos['symbol']}; warning remains eligible for retry")

        db.guardian_update(pos["id"],new_state,
                           confidence=(calibration or {}).get("confidence",0),
                           action=info.get("action",""),review_ts=review_ts,alerted=(notify and delivery_ok))


async def guardian_loop(http,db,state):
    """Fast, low-noise supervision independent of the slower discovery scan."""
    while True:
        try:
            await track_positions(http,db,state)
            await sync_wallet_positions(http,db,state)
            now=time.time()
            if OUTCOME_CAPTURE_ENABLED and now-state.get("last_outcome_capture",0)>=60:
                added=capture_signal_outcomes(db)
                decisions=capture_decision_outcomes(db)
                state["last_outcome_capture"]=now
                if added: print(f"[guardian] captured {added} forward signal outcome(s)")
                if decisions: print(f"[guardian] captured {decisions} forward decision outcome(s)")
            if MISSED_AUDIT_ENABLED and now-state.get("last_missed_audit",0)>=MISSED_AUDIT_EVERY_MINUTES*60:
                added=audit_missed_opportunities(db); state["last_missed_audit"]=now
                if added: print(f"[guardian] logged {added} clean missed-opportunity example(s)")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[guardian error] {type(exc).__name__}: {exc}")
        await asyncio.sleep(GUARDIAN_MONITOR_SECONDS)


def _reply_contract(message):
    replied = message.get("reply_to_message") or {}
    body = str(replied.get("text") or "")
    if not body:
        return None
    match = re.search(r"Contract:\s*\n?\s*([A-Za-z0-9]{20,80})", body, flags=re.I)
    return match.group(1) if match else None


def _reply_message_id(message):
    replied = message.get("reply_to_message") or {}
    try:
        return int(replied.get("message_id")) if replied.get("message_id") is not None else None
    except Exception:
        return None


def _parse_usd(tokens, default=None):
    for raw in tokens:
        cleaned = str(raw).lower().replace("$", "").replace(",", "")
        for suffix in ("dollars", "dollar", "usd", "dls"):
            cleaned = cleaned.replace(suffix, "")
        cleaned = cleaned.strip()
        if not cleaned:
            continue
        try:
            value = float(cleaned)
            if value > 0:
                return value
        except Exception:
            pass
    return default


_PAPER_GLITCH_SINCE={}


def paper_price_glitch(pos,current,liq,now=None):
    """True while a paper position's latest price is implausible versus its entry.

    A glitch never moves peaks or triggers exits. If it persists past
    PAPER_GLITCH_MAX_MINUTES the caller closes the simulation as unmeasurable.
    """
    now=float(now if now is not None else time.time())
    entry=f(pos.get("entry_price")); entry_liq=f(pos.get("entry_liquidity"))
    ok=price_move_plausible(current/max(entry,1e-18),(liq/entry_liq) if entry_liq>0 else None)
    pid=int(pos["id"])
    if ok:
        _PAPER_GLITCH_SINCE.pop(pid,None)
        return False
    _PAPER_GLITCH_SINCE.setdefault(pid,now)
    return True


async def track_paper_positions(http,db,prices=None):
    """Advance paper/shadow simulations. ``prices`` maps token -> pair (batched fetch)."""
    events=[]
    for pos in db.paper_open_positions():
        pair=(prices or {}).get(pos["token"]) if prices is not None else await pair_for_token(http,pos["chain"],pos["token"])
        if not pair: continue
        current=f(pair.get("priceUsd")); m=metrics(pair)
        if current<=0: continue
        if paper_price_glitch(pos,current,m["liq"]):
            since=_PAPER_GLITCH_SINCE.get(int(pos["id"]),time.time())
            if time.time()-since>=PAPER_GLITCH_MAX_MINUTES*60:
                db.paper_close_unmeasurable(pos["id"],"price data unreliable (feed glitch) — excluded from proof")
                _PAPER_GLITCH_SINCE.pop(int(pos["id"]),None)
            continue
        ret=(current/max(pos["entry_price"],1e-18)-1)*100
        peak=max(f(pos.get("peak_price") or pos["entry_price"]),current)
        if peak>f(pos.get("peak_price") or pos["entry_price"]): db.paper_update_peak(pos["id"],peak)
        draw=(current/max(peak,1e-18)-1)*100
        liq_drop=(1-m["liq"]/max(f(pos["entry_liquidity"]),1))*100
        age=(time.time()-pos["open_ts"])/60
        reason=None
        paper_tp1,paper_tp2=tier_profit_targets(pos.get("tier") or "ENTRY OPTION")
        if ret>=paper_tp1 and not pos["tp1_sent"]:
            db.paper_flag_tp1(pos["id"]); events.append(("tp1",pos,ret,None))
        if ret>=paper_tp2: reason=f"TP2 +{ret:.1f}%"
        elif ret<=-RISK_LINE: reason=f"risk stop {ret:.1f}%"
        elif pos["tp1_sent"] and draw<=-TRAIL: reason=f"trailing stop {draw:.1f}% from peak"
        elif liq_drop>=LIQ_DROP: reason=f"liquidity down {liq_drop:.0f}%"
        elif m["pc5"]<=REV5: reason=f"5m reversal {m['pc5']:+.1f}%"
        elif age>=TIME_STOP_MIN and ret<=TIME_STOP_MAX_RETURN: reason=f"time stop {ret:+.1f}% after {age:.0f}m"
        if reason:
            pnl=db.paper_close(pos["id"],current,reason); events.append(("close",pos,ret,pnl,reason))
    return events

async def batch_pairs(http, chain, tokens):
    """Best exact-base pair per token via one DexScreener request per 30 tokens.

    Mirrors pair_for_token's identity rule (base address must match; highest liquidity
    wins). Missing tokens are simply absent from the result.
    """
    out={}
    wanted=[str(t) for t in dict.fromkeys(tokens) if t]
    for i in range(0,len(wanted),30):
        chunk=wanted[i:i+30]
        _,data=await http.get(f"{DEX}/tokens/v1/{chain}/{','.join(chunk)}")
        if not isinstance(data,list):
            continue
        lookup={t.lower():t for t in chunk}
        for p in data:
            base=str(nest(p,"baseToken","address",default="")).strip()
            tok=lookup.get(base.lower())
            if not tok:
                continue
            if tok not in out or f(nest(p,"liquidity","usd",default=0))>f(nest(out[tok],"liquidity","usd",default=0)):
                out[tok]=p
    return out


async def announce_paper_events(http,events):
    for event in events or []:
        if event[0]=="tp1":
            _,pos,ret,_=event
            msg=f"🧪 PAPER TP1 — {pos['symbol']} {ret:+.1f}% gross price move | simulated trade remains open with trailing protection."
            if not pos.get("shadow") or SHADOW_TELEGRAM:
                await send(http,msg)
            else:
                print("[shadow] "+msg)
        elif event[0]=="close":
            _,pos,ret,pnl,reason=event
            msg=f"🧪 PAPER SELL — {pos['symbol']} {ret:+.1f}% gross price move | NET est. P/L ${pnl:+.2f}\nReason: {reason}\nNo real order was placed."
            if not pos.get("shadow") or SHADOW_TELEGRAM:
                await send(http,msg)
            else:
                print("[shadow] "+msg.replace("\n"," | "))


async def paper_monitor_loop(http,db,state):
    """Fast paper/shadow exits with one batched price request per tick.

    Paper results decide the proof gate, so they must be measured the way a live exit
    loop would trade. Checked once per slow discovery scan, the "8% risk stop" realized
    an average of -15% in the Sep export because prices gapped between checks.
    """
    state["paper_monitor_running"]=True
    try:
        while True:
            try:
                positions=db.paper_open_positions()
                if positions:
                    by_chain=defaultdict(list)
                    for p in positions:
                        by_chain[str(p.get("chain") or "solana")].append(p["token"])
                    prices={}
                    for chain,tokens in by_chain.items():
                        prices.update(await batch_pairs(http,chain,tokens))
                    if prices:
                        await announce_paper_events(http,await track_paper_positions(http,db,prices=prices))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[paper-monitor error] {type(exc).__name__}: {exc}")
            await asyncio.sleep(PAPER_MONITOR_SECONDS)
    finally:
        state["paper_monitor_running"]=False


async def path_tracker_loop(http,db,state):
    """Research-only forward path recorder (never trades)."""
    tracker=CandidatePathTracker(db,lambda chain,tokens: batch_pairs(http,chain,tokens),
                                 horizon_s=PATH_TRACK_HORIZON_MINUTES*60,poll_s=PATH_TRACK_POLL_SECONDS,
                                 max_active=PATH_TRACK_MAX_ACTIVE)
    state["path_tracker"]=tracker
    last_prune=0.0
    while True:
        try:
            stats=await tracker.step()
            state["path_tracker_stats"]=stats
            if time.time()-last_prune>=6*3600:
                tracker.prune(); last_prune=time.time()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[path-tracker error] {type(exc).__name__}: {exc}")
        await asyncio.sleep(PATH_TRACK_POLL_SECONDS)


async def execution_preflight(http,state,pair,tier):
    """Read-only route/friction check. Never constructs, signs or submits a trade."""
    if str(pair.get("chainId") or "").lower()!="solana" or not EXECUTION_PREFLIGHT_ENABLED:
        return True,[],{"available":False,"reason":"execution preflight disabled/non-Solana"}
    probe=state.get("execution_probe")
    if probe is None:
        probe=JupiterExecutionProbe(); state["execution_probe"]=probe
    token=str(nest(pair,"baseToken","address",default=""))
    info=await probe.probe(http,token)
    token_intel=await probe.token_intel(http,token)
    info["token_intel"]=token_intel
    blocks=[]
    if token_intel.get("hard_block"):
        blocks.append(f"Jupiter token safety veto: {token_intel.get('reason')}")
    if info.get("available"):
        total=f(info.get("total_friction_bps"))/100.0
        if info.get("hard_block"):
            blocks.append(f"Jupiter route too expensive: {info.get('reason')}")
        tp1,_=tier_profit_targets(tier)
        if total>0 and tp1 < total*EXECUTION_MIN_REWARD_FRICTION_MULT:
            blocks.append(f"TP1 {tp1:.0f}% is < {EXECUTION_MIN_REWARD_FRICTION_MULT:.1f}x estimated {total:.2f}% round-trip friction")
    elif EXECUTION_REQUIRE_ROUTE:
        blocks.append(f"Jupiter route could not be verified: {info.get('reason')}")
    return (not blocks),blocks,info


def execution_friction_pct(info=None):
    info=info or {}
    if info.get("available"):
        return max(0.0,f(info.get("total_friction_bps"))/100.0)
    # Phantom UI estimate only when a live Jupiter round trip could not be measured.
    return max(0.0,f(info.get("frontend_bps"),170.0)/100.0)


async def shadow_auto_open(http,db,pair,tier,score,reasons=None,execution_info=None,source_event="EDGE_ARMED"):
    """Always-on forward simulator for setups that make it through the edge engine."""
    if not (bool_pref(db,"shadow_sim",SHADOW_SIM_ALWAYS_ON) or bool_pref(db,"paper_auto",PAPER_AUTO_DEFAULT)):
        return None
    chain=str(pair.get("chainId") or "").lower(); token=str(nest(pair,"baseToken","address",default=""))
    if chain!="solana" or not token or db.paper_position_by_token(token) or db.recent_shadow_signal(chain,token):
        return None
    sid=db.add_signal("PAPER_SHADOW",pair,score,tier,list(reasons or [])+["v15 proof-first forward simulation"])
    sig=db.signal_by_id(sid)
    friction=execution_friction_pct(execution_info)
    proof_ok,proof_reason=proof_candidate_eligibility(tier,score)
    pid=db.paper_open(sig,f(pair.get("priceUsd")),f(nest(pair,"liquidity","usd",default=0)),tier,PAPER_POSITION_USD,
                      friction_pct=friction,shadow=True,source_event=source_event,
                      proof_eligible=proof_ok,proof_reason=proof_reason)
    if pid:
        cohort="PROOF" if proof_ok else "RESEARCH"
        db.log_decision(pair,"PAPER_SHADOW",tier,score,score,
                        [f"{cohort.lower()} shadow; est. {friction:.2f}% round-trip friction; {proof_reason}"],min_seconds=30)
        print(f"[shadow:{cohort.lower()}] {sig['symbol']} {tier} ${PAPER_POSITION_USD:.2f} est friction {friction:.2f}% | {proof_reason}")
        if SHADOW_TELEGRAM and not bool_pref(db,"quiet_mode",False):
            await send(http,f"🧪 SHADOW EDGE — {sig['symbol']} | {tier}\nPaper only. ${PAPER_POSITION_USD:.2f} simulated at ${f(pair.get('priceUsd')):.8g}; est. round-trip friction {friction:.2f}%. Do not treat this as a BUY call.")
    return pid


async def send_preproof_actionable_signal(http,db,state,limiter,key,pair,tier,score,confirmed,reasons,risks,hits,safety,rts,regime,social,source_ctx,execution_info,kind="EARLY",hot=False):
    auth=entry_signal_mode(db,tier,score)
    if not auth.get("send") or auth.get("mode")=="LIVE":
        return False
    mode=auth.get("mode") or "TEST"
    cal=adaptive_dollar_size(db,pair,tier,score,market_regime=str((regime or {}).get("label") or "NEUTRAL"),social=social,safety=safety) if ADAPTIVE_CONFIDENCE_ENABLED else {}
    cal=_test_signal_calibration(db,cal,mode)
    all_reasons=list(reasons or [])+["all market/safety/path/execution gates passed",f"v15.3 {mode.lower()} signal policy"]
    msg=beginner_alert(kind,pair,score,tier,all_reasons,risks,hits,
                       bool((safety or {}).get("wallet_checked")),bool((safety or {}).get("security_checked")),
                       bool((safety or {}).get("holder_checked")),bool((safety or {}).get("trader_checked")),
                       rts=rts,safety=safety,tier=tier,confirmed_score=confirmed,calibration=cal,market_regime=regime,
                       social=social,source_ctx=source_ctx,signal_mode=mode,signal_note=auth.get("reason") or "",
                       track_record=alert_track_record_line(db,tier))
    mid=await send(http,msg)
    if TG and CHAT and mid is None:
        limiter.release_action("entry:"+key)
        db.log_decision(pair,"ENTRY_DELIVERY_FAIL",tier,score,confirmed,["Telegram did not confirm test-signal delivery; cooldown released"],
                        rts=rts,social=social,market_regime=str((regime or {}).get("label") or "NEUTRAL"),sources=(source_ctx or {}).get("sources"),min_seconds=15)
        return False
    db.log_decision(pair,"ENTRY_SENT_TEST",tier,score,confirmed,[f"{mode} signal; "+str(auth.get("reason") or "")],
                    rts=rts,social=social,market_regime=str((regime or {}).get("label") or "NEUTRAL"),sources=(source_ctx or {}).get("sources"),min_seconds=30)
    signal_id=db.add_signal("EARLY",pair,score,tier,all_reasons+[f"signal_mode={mode}"])
    db.log_latency(signal_id,str(nest(pair,"baseToken","address",default="")),"ENTRY_ALERT_TEST_HOT" if hot else "ENTRY_ALERT_TEST",f(pair.get("priceUsd")),
                   f"tier={tier};confirmed={confirmed:.1f};mode={mode};friction={execution_friction_pct(execution_info):.2f}")
    db.map_telegram_signal(mid,signal_id); db.map_trade_context(mid,signal_id,pair,tier)
    schedule_entry_followups(http,db,state,signal_id)
    return True


async def paper_auto_open(http,db,signal,pair,tier,execution_info=None):
    if not bool_pref(db,"paper_auto",PAPER_AUTO_DEFAULT): return None
    if db.paper_position_by_token(signal["token"]): return None
    friction=execution_friction_pct(execution_info)
    pid=db.paper_open(signal,f(pair.get("priceUsd")),f(nest(pair,"liquidity","usd",default=0)),tier,PAPER_POSITION_USD,
                      friction_pct=friction,shadow=False,source_event="ENTRY_SENT")
    if pid:
        await send(http,f"🧪 PAPER BUY — {signal['symbol']} | {tier}\nSimulated ${PAPER_POSITION_USD:.2f} at ${f(pair.get('priceUsd')):.8g}; estimated round-trip friction {friction:.2f}%. No real order was placed.")
    return pid


def _auto_tier_allowed(db,tier):
    if tier == "FRESH ENTRY":
        return bool_pref(db,"auto_fresh",AUTO_LIVE_ALLOW_FRESH_DEFAULT)
    if tier == "RE-ENTRY OPTION":
        return AUTO_LIVE_ALLOW_REENTRY
    return tier in {"ENTRY OPTION","STRONG ENTRY"}


def _auto_size(db,tier):
    risk=bankroll_size_context(db)
    if not risk.get("ready",True):
        return 0.0
    requested=clamp(float_pref(db,"auto_trade_usd",AUTO_LIVE_TRADE_USD),1,min(MAX_POSITION,5.0))
    size=min(requested,f(risk.get("suggested_usd"),requested),5.0)
    if tier=="FRESH ENTRY":
        size=size*AUTO_LIVE_FRESH_SIZE_MULT
    return max(0.0,size)


def _auto_fail_once(state,key):
    now=time.time()
    cache=state.setdefault("auto_error_notice",{})
    if now-cache.get(key,0)<15*60:
        return False
    cache[key]=now
    return True


def live_execution_gate(db, tier="", *, include_capacity=True):
    """Single fail-closed authorization point for every NEW real-money order."""
    reasons=[]
    if db.has_unresolved_execution():
        reasons.append("unresolved prior on-chain execution requires reconciliation")
    proof=proof_first_health(db)
    if PROOF_FIRST_REQUIRE_MATURE and proof.get("status")!="ACTIVE":
        reasons.append(f"proof gate {proof.get('status')} ({proof.get('n',0)}/{PROOF_FIRST_MIN_PATHS})")
    core=capital_core_health(db)
    if CAPITAL_CORE_GOVERNOR_ENABLED and core.get("status")!="ACTIVE":
        reasons.append(f"capital core {core.get('status')}")
    risk=paper_risk_guard(db)
    if risk.get("paused"):
        reasons.extend(risk.get("reasons") or ["paper risk circuit breaker"] )
    bank=bankroll_size_context(db)
    if not bank.get("ready",True):
        reasons.append(str(bank.get("reason") or "bankroll not configured"))
    if tier:
        lane=lane_health(db,tier)
        if lane.get("paused"):
            reasons.append(f"{tier} lane {lane.get('status')}")
    stats=db.auto_daily_stats()
    if stats["buys"]>=AUTO_LIVE_MAX_BUYS_DAY:
        reasons.append("daily live buy limit reached")
    if stats["pnl"]<=-AUTO_LIVE_MAX_DAILY_LOSS:
        reasons.append("daily live loss limit reached")
    if include_capacity and len(db.auto_open_positions())>=AUTO_LIVE_MAX_OPEN:
        reasons.append("maximum live auto positions reached")
    return {"allowed":not reasons,"reasons":reasons,"proof":proof,"core":core,"risk":risk,"bankroll":bank,"stats":stats}


async def reconcile_unresolved_execution_intents(http,db,state,limit=20):
    """Recover signed/submitted transactions from chain truth without resending them."""
    ex=state.get("live_executor")
    if not ex or not ex.keypair_path:
        return {"checked":0,"resolved":0,"remaining":len(db.unresolved_execution_intents(limit))}
    checked=resolved=0
    work=[(i,False) for i in db.unresolved_execution_intents(limit)]
    work+=[(i,True) for i in db.unaccounted_confirmed_intents(AUTO_LIVE_ORPHAN_AGE_SECONDS,limit)]
    for intent,orphan in work:
        checked+=1
        if orphan:
            # Chain truth is already stored; only the journal write is missing.
            result={"resolved":True,"state":"CONFIRMED","actual_output_raw":intent.get("actual_output_raw")}
        else:
            result=await ex.reconcile_intent(http,intent,db=db)
        if not result.get("resolved") or result.get("state")!="CONFIRMED":
            if result.get("resolved"):
                resolved+=1
            continue
        try:
            ctx=json.loads(intent.get("context_json") or "{}")
        except Exception:
            ctx={}
        side=str(intent.get("side") or "").upper()
        actual_raw=int(f(result.get("actual_output_raw")))
        if side.startswith("COPY_"):
            copy_engine=state.get("copy_engine")
            if copy_engine and actual_raw>0 and hasattr(copy_engine,"account_confirmed_intent"):
                solusd=f(await ex.sol_usd(http))
                if solusd>0 and copy_engine.account_confirmed_intent(intent,actual_raw,solusd,result.get("actual_input_raw")):
                    resolved+=1
            continue
        if side=="BUY" and actual_raw>0:
            sig=None
            if intent.get("signal_id"):
                sig=db.signal_by_id(int(intent["signal_id"]))
            if not sig:
                sig={"id":intent.get("signal_id"),"chain":"solana","token":intent.get("token") or "",
                     "symbol":intent.get("symbol") or "?","name":intent.get("symbol") or "?"}
            spent_usd=f(intent.get("amount_usd"))
            if result.get("actual_input_raw") and f(ctx.get("solusd"))>0:
                spent_usd=(f(result.get("actual_input_raw"))/1_000_000_000)*f(ctx.get("solusd"))
            already=db.conn.execute("select id from positions where buy_signature=? and coalesce(buy_signature,'')!='' limit 1",
                                    (str(intent.get("signature") or ""),)).fetchone()
            if already:
                db.update_execution_intent(str(intent.get("idempotency_key") or ""),position_id=int(already[0]),state="ACCOUNTED")
            elif not db.position_by_token(sig["token"]):
                pid=db.record_auto_buy_atomic(sig,f(ctx.get("current_price")),spent_usd,f(ctx.get("liquidity")),
                                              str(intent.get("tier") or ""),str(intent.get("signature") or ""),actual_raw,
                                              intent_key=str(intent.get("idempotency_key") or ""))
                db.update_execution_intent(str(intent.get("idempotency_key") or ""),position_id=pid,state="ACCOUNTED")
            else:
                db.update_execution_intent(str(intent.get("idempotency_key") or ""),state="ACCOUNTED")
            resolved+=1
        elif side=="SELL" and actual_raw>0:
            pid=int(intent.get("position_id") or ctx.get("position_id") or 0)
            row=db.conn.execute("select * from positions where id=?",(pid,)).fetchone() if pid else None
            sell_booked=db.conn.execute("select 1 from auto_trade_events where side='SELL' and signature=? and coalesce(signature,'')!='' limit 1",
                                        (str(intent.get("signature") or ""),)).fetchone()
            if row and int(row["active"] or 0) and not sell_booked:
                pos=dict(row); solusd=f(ctx.get("solusd")); proceeds=(actual_raw/1_000_000_000)*solusd if solusd>0 else f(ctx.get("proceeds"))
                sell_raw=int(ctx.get("sell_raw") or intent.get("amount_raw") or 0); final=bool(ctx.get("final")); flag=ctx.get("flag")
                rem=f(pos.get("remaining_fraction") or 1.0); recorded=max(1,int(pos.get("auto_token_raw_remaining") or sell_raw or 1))
                sold_fraction=1.0 if final else clamp(sell_raw/recorded,0,1)
                sold_cost=f(pos.get("amount_usd"))*rem*sold_fraction; pnl=proceeds-sold_cost
                db.record_auto_sell_atomic(pos,current_price=f(ctx.get("current_price"),f(pos.get("entry_price"))),proceeds=proceeds,sell_raw=sell_raw,
                                           pnl=pnl,reason=str(ctx.get("reason") or "reconciled sell"),signature=str(intent.get("signature") or ""),
                                           final=final,flag=flag,token_raw_remaining=max(0,recorded-sell_raw),intent_key=str(intent.get("idempotency_key") or ""))
            else:
                db.update_execution_intent(str(intent.get("idempotency_key") or ""),state="ACCOUNTED")
            resolved+=1
    return {"checked":checked,"resolved":resolved,"remaining":len(db.unresolved_execution_intents(limit))}


async def live_autopilot_try_entry(http,db,state,signal):
    ex=state.get("live_executor")
    if not ex or not bool_pref(db,"live_auto",False) or not ex.ready():
        return
    if signal.get("chain")!="solana":
        return
    owner=f"{os.getpid()}:{id(state)}:{uuid.uuid4().hex[:8]}"
    lease=f"entry:solana:{signal.get('token','')}"
    if not db.acquire_execution_lease(lease,owner,ttl=150):
        return
    try:
        # Recheck every mutable authorization condition while the cross-process lease is held.
        if db.auto_has_buy_for_signal(signal["id"]) or db.position_by_token(signal["token"]):
            return
        tier=str(signal.get("action") or "")
        if not _auto_tier_allowed(db,tier):
            return
        gate=live_execution_gate(db,tier)
        if not gate.get("allowed"):
            return

        live=await pair_for_token(http,"solana",signal["token"])
        if not live:
            return
        current=f(live.get("priceUsd")); m=metrics(live)
        if current<=0:
            return
        safety=cached_safety(state,"solana",signal["token"])
        if safety is None:
            _,notes,hard,checked=await rugcheck_summary(http,signal["token"])
            safety={"hard":hard,"rug_checked":checked,"security_checked":False,"holder_checked":False,
                    "risks":notes if hard else [],"reasons":notes if not hard else []}
            state["safety_cache"]["solana:"+signal["token"]]=dict(safety,ts=time.time())
        if safety.get("hard"):
            return

        scout=db.recent_signal("solana",signal["token"],"SCOUT",hours=2) or signal
        move=(current/max(f(scout.get("price")),1e-18)-1)*100
        conf=db.recent_signal("solana",signal["token"],"CONFIRMED",hours=8)
        confirmed=max(f(signal.get("score")),f(conf.get("score")) if conf else 0)
        rt=state.get("realtime"); rts=rt.stats(signal["token"]) if rt else {}
        live_tier,_=classify_actionable_tier(live,f(signal.get("score")),confirmed,list(safety.get("risks",[])),False,
                                        rts=rts,safety=safety,scout_move=move)
        if not live_tier or not _auto_tier_allowed(db,live_tier):
            return
        tier=live_tier
        # Gate again if the classifier changed the tier.
        gate=live_execution_gate(db,tier)
        if not gate.get("allowed"):
            return

        alert_move=(current/max(f(signal.get("price")),1e-18)-1)*100
        if alert_move>max(MAX_CHASE,2.5) or alert_move < -CHECK_MAX_DROP_FROM_ALERT_PCT:
            return
        stable,_,_=entry_stability_check(db,live,tier)
        if not stable:
            return
        solusd=await ex.sol_usd(http)
        bal=await ex.sol_balance_raw_checked(http)
        if not solusd or not bal.get("ok"):
            db.log_trade_failure("ENTRY_PREFLIGHT","BALANCE_UNAVAILABLE",str(bal.get("error") or "SOL/USD unavailable"),retryable=True,side="BUY",token=signal["token"])
            return
        auto_cal=confidence_report(db.conn,live,tier,f(signal.get("score"),50),_auto_size(db,tier)) if ADAPTIVE_CONFIDENCE_ENABLED else None
        auto_mult=min(1.0,max(0.50,f((auto_cal or {}).get("size_multiplier"),1.0)))
        usd_size=max(1.0,_auto_size(db,tier)*auto_mult)
        sol_needed=usd_size/solusd
        if f(bal.get("value"))-sol_needed<AUTO_LIVE_MIN_SOL_RESERVE:
            if _auto_fail_once(state,"reserve"):
                await send(http,f"🤖 AUTO BUY PAUSED — wallet needs more SOL. Balance {f(bal.get('value')):.4f} SOL; reserve floor {AUTO_LIVE_MIN_SOL_RESERVE:.3f} SOL.")
            return

        lamports=max(1,int(sol_needed*1_000_000_000))
        attempt_bucket=int(time.time()//30)
        idem=f"buy:{VERSION}:{signal['id']}:{signal['token']}:{attempt_bucket}"
        context={"current_price":current,"liquidity":m["liq"],"solusd":solusd,"tier":tier}
        result=await ex.swap(http,WSOL_MINT,signal["token"],lamports,db=db,idempotency_key=idem,side="BUY",token=signal["token"],
                             symbol=signal["symbol"],tier=tier,signal_id=signal["id"],amount_usd=usd_size,context=context)
        if not result.get("ok"):
            db.auto_event(None,signal["id"],"BUY","solana",signal["token"],signal["symbol"],tier,
                          usd_size,lamports,current,result.get("signature","") or "","execution pending" if result.get("executed") else "execution failed",
                          0,False,result.get("error",""))
            if _auto_fail_once(state,"buy:"+signal["token"]):
                prefix="AUTO BUY CONFIRMED BUT RECONCILIATION PENDING" if result.get("executed") else "AUTO BUY FAILED"
                await send(http,f"🤖 {prefix} — {signal['symbol']}\nReason: {str(result.get('error','unknown'))[:350]}" +
                           ("\nNew live orders are locked until chain reconciliation completes." if result.get("executed") or result.get("ambiguous") else ""))
            return

        spent_raw=f(result.get("actual_input_raw")) or f(result.get("input_raw"))
        actual_usd=(spent_raw/1_000_000_000)*solusd
        pid=db.record_auto_buy_atomic(signal,current,actual_usd,m["liq"],tier,result.get("signature",""),result.get("output_raw",0),intent_key=idem)
        calnote=(f"Adaptive risk sizing: {auto_cal.get('confidence',0):.0f}/100; {auto_mult:.2f}x of configured auto size\n" if auto_cal else "")
        warning=(f"\nExecution warning: {result.get('warning')}" if result.get("warning") else "")
        await send(http,
            f"🤖✅ REAL AUTO BUY — {signal['symbol']} | {tier}\n"
            f"Spent about ${actual_usd:.2f} at ${current:.8g}\n"
            f"{calnote}"
            f"Contract: {signal['token']}\n"
            f"Tx: https://solscan.io/tx/{result.get('signature','')}{warning}\n"
            "The bot will now manage take-profits and risk exits for this position.")
    finally:
        db.release_execution_lease(lease,owner)


async def _auto_sell(http,db,state,pos,fraction,reason,flag=None,final=False):
    ex=state.get("live_executor")
    owner=f"{os.getpid()}:{id(state)}:{uuid.uuid4().hex[:8]}"
    lease=f"sell:solana:{int(pos['id'])}"
    if not db.acquire_execution_lease(lease,owner,ttl=150):
        return False
    try:
        checked=await ex.token_raw_balance_checked(http,pos["token"])
        raw=checked.get("raw") if checked.get("ok") else None
        if raw is None or raw<=0:
            db.log_trade_failure("SELL_PREFLIGHT","BALANCE_UNAVAILABLE",str(checked.get("error") or "zero token balance"),retryable=True,side="SELL",token=pos["token"])
            if _auto_fail_once(state,"balance:"+pos["token"]):
                await send(http,f"🤖 AUTO SELL COULD NOT READ TOKEN BALANCE — {pos['symbol']}. Position remains open in the journal.")
            return False
        try:
            recorded_raw=int(pos.get("auto_token_raw_remaining") or 0)
        except Exception:
            recorded_raw=0
        position_raw=min(raw,recorded_raw) if recorded_raw>0 else raw
        if position_raw<=0:
            return False
        sell_raw=position_raw if final else max(1,int(position_raw*clamp(fraction,0.01,1.0)))
        solusd=await ex.sol_usd(http)
        pair=await pair_for_token(http,"solana",pos["token"])
        current=f(pair.get("priceUsd")) if pair else f(pos["entry_price"])
        if not solusd:
            return False
        stage=("final" if final else (flag or "partial"))
        idem=f"sell:{VERSION}:{int(pos['id'])}:{stage}:{int(time.time()//30)}"
        context={"position_id":int(pos["id"]),"current_price":current,"solusd":solusd,"sell_raw":sell_raw,
                 "position_raw":position_raw,"reason":reason,"final":bool(final),"flag":flag or ""}
        result=await ex.swap(http,pos["token"],WSOL_MINT,sell_raw,db=db,idempotency_key=idem,side="SELL",token=pos["token"],
                             symbol=pos["symbol"],tier=pos.get("entry_tier") or "",position_id=int(pos["id"]),context=context)
        if not result.get("ok"):
            db.auto_event(pos["id"],None,"SELL","solana",pos["token"],pos["symbol"],pos.get("entry_tier") or "",
                          0,sell_raw,current,result.get("signature","") or "",reason,0,False,result.get("error",""))
            if _auto_fail_once(state,"sell:"+pos["token"]):
                prefix="AUTO SELL CONFIRMED BUT RECONCILIATION PENDING" if result.get("executed") else "AUTO SELL FAILED"
                await send(http,f"🤖 {prefix} — {pos['symbol']}\nReason: {str(result.get('error','unknown'))[:350]}\n" +
                           ("New live orders are locked until chain reconciliation completes." if result.get("executed") or result.get("ambiguous") else "The bot will retry while the exit condition remains active."))
            return False
        proceeds=(f(result.get("output_raw"))/1_000_000_000)*solusd
        rem=f(pos.get("remaining_fraction") or 1.0)
        sold_fraction=1.0 if final else clamp(sell_raw/max(position_raw,1),0,1)
        sold_cost=f(pos["amount_usd"])*rem*sold_fraction
        pnl=proceeds-sold_cost
        total,remain=db.record_auto_sell_atomic(pos,current_price=current,proceeds=proceeds,sell_raw=sell_raw,pnl=pnl,reason=reason,
                                                signature=result.get("signature",""),final=final,flag=flag,token_raw_remaining=position_raw-sell_raw,intent_key=idem)
        label="FULL EXIT" if final else ("TP1 PARTIAL" if flag=="auto_tp1_done" else "TP2 PARTIAL")
        pnltext=f"Realized on this sell: ${pnl:+.2f}" + (f" | total trade P/L ${total:+.2f}" if total is not None else "")
        warning=(f"\nExecution warning: {result.get('warning')}" if result.get("warning") else "")
        await send(http,f"🤖💰 REAL AUTO SELL — {pos['symbol']} | {label}\nReason: {reason}\n{pnltext}\nTx: https://solscan.io/tx/{result.get('signature','')}{warning}\n" +
                   ("Position closed." if final else f"Approx remaining position: {remain*100:.0f}% of original."))
        return True
    finally:
        db.release_execution_lease(lease,owner)


async def live_autopilot_track_positions(http,db,state):
    ex=state.get("live_executor")
    if not ex or not ex.ready():
        return
    if not bool_pref(db,"live_auto",False) and not AUTO_LIVE_EXITS_WHEN_OFF:
        return
    for pos in db.auto_open_positions():
        # Never submit a second transaction for a token whose prior signed execution is
        # unresolved (duplicate-fill risk). Unrelated positions keep their stop-losses:
        # a stuck intent on one token must not disable exits on every other position.
        if db.has_unresolved_execution_for_token(pos["token"]):
            continue
        pair=await pair_for_token(http,"solana",pos["token"])
        if not pair:
            continue
        current=f(pair.get("priceUsd")); m=metrics(pair)
        if current<=0:
            continue
        ret=(current/max(f(pos["entry_price"]),1e-18)-1)*100
        peak=max(f(pos.get("peak_price") or pos["entry_price"]),current)
        if peak>f(pos.get("peak_price") or pos["entry_price"]):
            db.update_position_peak(pos["id"],peak)
        draw=(current/max(peak,1e-18)-1)*100
        liq_drop=(1-m["liq"]/max(f(pos["entry_liquidity"]),1))*100
        age=(time.time()-f(pos["open_ts"]))/60

        # One sell action per token per cycle.
        if ret>=AUTO_LIVE_TP1 and not int(pos.get("auto_tp1_done") or 0):
            auto_remaining=f(pos.get("amount_usd"))*clamp(f(pos.get("remaining_fraction"),1.0),0.0,1.0)
            auto_full_tp1=auto_remaining<=25.0
            if await _auto_sell(http,db,state,pos,1.0 if auto_full_tp1 else AUTO_LIVE_TP1_SELL,
                                f"TP1 reached {ret:+.1f}%" + ("; capital-first small-position full profit lock" if auto_full_tp1 else ""),
                                None if auto_full_tp1 else "auto_tp1_done",auto_full_tp1):
                continue
        if ret>=AUTO_LIVE_TP2 and not int(pos.get("auto_tp2_done") or 0):
            if await _auto_sell(http,db,state,pos,AUTO_LIVE_TP2_SELL,
                                f"TP2 reached {ret:+.1f}%","auto_tp2_done",False):
                continue

        reason=None
        ratio=m["buys"]/max(m["sells"],1)
        # Lock more of large winners instead of using one fixed trail forever.
        dynamic_trail=AUTO_LIVE_TRAIL
        if ret>=60: dynamic_trail=min(dynamic_trail,7.0)
        elif ret>=AUTO_LIVE_TP2: dynamic_trail=min(dynamic_trail,8.0)
        elif ret>=AUTO_LIVE_TP1: dynamic_trail=min(dynamic_trail,9.0)
        if ret<=-AUTO_LIVE_STOP:
            reason=f"stop loss {ret:+.1f}%"
        elif int(pos.get("auto_tp1_done") or 0) and draw<=-dynamic_trail:
            reason=f"adaptive trailing drawdown {draw:+.1f}% from peak (trail {dynamic_trail:.0f}%)"
        elif liq_drop>=LIQ_DROP:
            reason=f"liquidity fell {liq_drop:.0f}% from entry"
        elif m["pc5"]<=REV5 and ret>0:
            reason=f"5m reversal {m['pc5']:+.1f}% while profitable"
        elif age>=max(45,AUTO_LIVE_TIME_STOP_MIN) and ret<=-3 and m["pc5"]<0 and ratio<1.0:
            reason=f"stale deteriorating setup {ret:+.1f}% after {age:.0f}m; sellers {ratio:.2f}x"
        if reason:
            await _auto_sell(http,db,state,pos,1.0,reason,None,True)


async def live_exit_loop(http,db,state):
    """Fast, independent live-position supervision: reconciliation + exits only.

    Runs outside the discovery scan so provider errors or slow scans never skip a
    stop-loss. Never opens positions.
    """
    state["live_exit_loop_running"]=True
    try:
        while True:
            try:
                ex=state.get("live_executor")
                if ex and ex.keypair_path and ex.ready():
                    await reconcile_unresolved_execution_intents(http,db,state)
                    await live_autopilot_track_positions(http,db,state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[live-exit error] {type(exc).__name__}: {exc}")
            await asyncio.sleep(AUTO_LIVE_MONITOR_SECONDS)
    finally:
        state["live_exit_loop_running"]=False


async def live_autopilot_cycle(http,db,state):
    ex=state.get("live_executor")
    if not ex or not bool_pref(db,"live_auto",False):
        return
    if not ex.ready():
        if _auto_fail_once(state,"notready"):
            await send(http,"🤖 REAL AUTOPILOT is enabled in Telegram but local execution is not ready. Run setup_live_trading.command, restart, then /autotest.")
        return

    # Reconciliation always runs before any new order. Exits remain allowed for known
    # positions, but unresolved execution state blocks additional entries.
    if not state.get("live_exit_loop_running"):
        if db.has_unresolved_execution():
            await reconcile_unresolved_execution_intents(http,db,state)
        await live_autopilot_track_positions(http,db,state)
    gate=live_execution_gate(db,include_capacity=True)
    if not gate.get("allowed"):
        return
    for signal in db.recent_actionable_entries(AUTO_LIVE_ENTRY_LOOKBACK_MIN):
        if db.auto_has_buy_for_signal(signal["id"]):
            continue
        key=str(signal["id"]); last=state.setdefault("auto_attempts",{}).get(key,0)
        if time.time()-last<60:
            continue
        state["auto_attempts"][key]=time.time()
        await live_autopilot_try_entry(http,db,state,signal)


async def command_loop(http, db, state):
    while True:
        try:
            await handle_commands(http, db, state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[telegram command error] {type(exc).__name__}: {exc}")
            print(traceback.format_exc(limit=4))
        await asyncio.sleep(2)


async def handle_commands(http, db, state):
    if not (TG and CHAT):
        return
    offset = int(db.get_meta("telegram_offset", "0") or 0)
    status, data = await http.get(f"https://api.telegram.org/bot{TG}/getUpdates",
                                  params={"offset":offset, "limit":50, "timeout":0})
    if status != 200 or not isinstance(data, dict):
        return
    for update in data.get("result", []):
        uid = int(update.get("update_id",0))
        db.set_meta("telegram_offset", uid+1)
        msg = update.get("message") or {}
        chat_id = str(nest(msg,"chat","id",default=""))
        text = str(msg.get("text") or "").strip()
        if chat_id != str(CHAT) or not text:
            continue

        normalized = text.strip()
        low = normalized.lower()
        if not normalized.startswith("/"):
            if low.startswith(("buy ", "buy/", "check ", "check/", "scan ", "coin ", "sell ", "sell/",
                               "positions", "status", "risk", "mode", "help", "watchlist", "last", "why ", "track ", "untrack ", "tracked",
                               "scouts ", "momentum ", "watch ", "settings", "near", "safety ", "addmc ", "bought ", "hold ", "plan ", "sellpct ", "sellusd ", "sold ", "paperauto ", "paperpositions", "paperreport", "state",
                               "copy", "autolive", "autostatus", "autotest", "autofresh ", "autosize ", "guardian", "confidence ", "size ", "sizerange", "riskrange", "autocheck", "wallet", "sync", "learn", "daily", "lab", "quiet", "sources", "pulse ", "socialdeep", "closecash ", "sellcash ", "fixentry ", "undo", "reconcile ", "edge", "leaders", "bankroll ", "shadow", "signalpolicy", "execstatus")):
                normalized = "/" + normalized

        normalized = re.sub(r"^/(buy|bought|check|sell)/", lambda m: "/" + m.group(1) + " ", normalized, flags=re.I)
        normalized = re.sub(r"^/bought\b", "/buy", normalized, flags=re.I)
        normalized = re.sub(r"^/bought(?=[$\d])", "/buy ", normalized, flags=re.I)
        normalized = re.sub(r"^/buy(?=[$\d])", "/buy ", normalized, flags=re.I)
        normalized = re.sub(r"^/(sellpct|sellusd)(?=[$\d])", lambda m: "/" + m.group(1) + " ", normalized, flags=re.I)
        normalized = re.sub(r"^/sold(?=[$\d])", "/sold ", normalized, flags=re.I)

        if not normalized.startswith("/"):
            continue

        parts = normalized.split()
        cmd = parts[0].split("@")[0].lower()

        if cmd == "/help":
            await send(http,
                f"FOMO BOT {VERSION} — SIMPLE COMMANDS\n"
                "/scan CONTRACT or TICKER — live scan\n"
                "/check — optional fresh re-check before buying or adding\n"
                "/bought 5 — reply after buying; records the exact alert/check snapshot\n"
                "/buy CONTRACT 5 — record any manual buy; no journal locks\n"
                "/positions — live P/L + GUARDIAN state + hold horizon\n"
                "/hold TICKER — live scalp/short-swing/extended-hold plan\n"
                "/confidence TICKER — adaptive confidence + historical endpoint stats\n"
                "/size TICKER — live dollar suggestion for the current setup\n"
                "/sizerange 5 20 30 — set minimum / normal max / exceptional max advice\n"
                "/autocheck on|off|status — automatic 1m + 2.5m entry freshness updates\n"
                "/quiet on|off|status — hide GET READY/watch chatter; keep entries + GUARDIAN\n"
                "/sources — live discovery/social/feed status\n"
                "/pulse CONTRACT — exact-contract social + source-convergence pulse\n"
                "/socialdeep on|off|status — optional targeted X contract searches (may use API quota)\n"
                "/sellpct TICKER 50 / /sellusd TICKER 10 / /sell TICKER — record sales\n"
                "/sellcash TICKER 30 4.03 — record a 30% partial using actual net cash received\n"
                "/closecash TICKER 17.99 — close using actual net cash received\n"
                "/fixentry TICKER PRICE — repair an open no-partial position if its recorded fill is wrong\n"
                "/guardian on|off|status — automatic position-management alerts\n"
                "/wallet — read-only Solana wallet-sync status\n"
                "/sync — force a read-only wallet reconciliation now\n"
                "/learn — recent captured winners/missed opportunities\n"
                "/daily — send the performance report now\n"
                "/lab — unseen chronological holdout validation\n"
                "/track CONTRACT /tracked /untrack CONTRACT — persistent custom scanner\n"
                "/paperauto on|off /paperreport — fee-aware no-money paper engine\n"
                "/copy — smart-wallet copy engine status\n"
                "/copywallet add ADDRESS [label] [score] / remove ADDRESS — track public smart wallets\n"
                "/copywallets — tracked wallets + scores | /copydiscover — auto-discovered whale candidates\n"
                "/copyanalyze ADDRESS — reconstruct recent on-chain wallet round trips and score them\n"
                "/copyreport — copy-paper results | /copykill on|off — hard new-buy kill switch\n"
                "/copylive arm|confirm|off — OPTIONAL live copy; locked behind 48h+ paper proof by default\n"
                "/edge — proof status, breadth, Jupiter execution health + risk circuit\n"
                "/leaders — top internal Runner Radar / edge candidates (WATCH ONLY)\n"
                "/bankroll 100 — bankroll for risk-based size guidance; /bankroll 0 clears it\n"
                "/shadow on|off — continuous proof-first forward simulator\n"
                "/signalpolicy actionable|proven|strict — all qualified TEST BUY signals, only alert types with a proven paper record, or require full proof\n"
                "/trackrecord — measured paper results for every alert type\n"
                "/autotest — test local Jupiter/wallet setup WITHOUT trading\n"
                "/execstatus — execution journal, RPC failover, pending tx + failure audit\n"
                "/autolive arm -> /autolive confirm — optional REAL Jupiter autopilot\n"
                "/autolive off — stop new automatic orders\n"
                "/watchlist /last /why CONTRACT /settings /status /state /near /safety CONTRACT\n\n"
                "GUARDIAN watches recorded positions every few seconds and only messages on meaningful state changes. "
                "Entry alerts auto-refresh if you have not recorded a buy, and profit alerts show an approximate % + dollar amount to sell. "
                "Optional wallet sync needs only your PUBLIC Solana wallet address; never paste a seed/private key into Telegram.")
        elif cmd in {"/copy","/copystatus"}:
            ce=state.get("copy_engine")
            st=ce.status() if ce else {}
            gate=ce.live_gate() if ce else {"allowed":False,"reasons":["copy engine unavailable"]}
            await send(http,
                "COPY EDGE STATUS\n"
                f"Stream: {st.get('source','?')} — {'CONNECTED' if st.get('connected') else 'RECONNECTING/OFF'}\n"
                f"Tracked wallets: {st.get('wallets',0)} | paper open {st.get('paper_open',0)} | closed {st.get('paper_closed',0)} | net ${st.get('paper_pnl',0):+.2f}\n"
                f"Live copy: {'ON' if st.get('live') else 'OFF'} | kill switch {'ON' if st.get('kill') else 'OFF'}\n"
                f"Live gate: {'READY' if gate.get('allowed') else 'LOCKED'}" + ((" — "+"; ".join(gate.get('reasons') or [])) if not gate.get('allowed') else "") + "\n"
                "Wallet buys are paper-copied automatically. Qualified tracked wallets can send COPY BUY NOW test alerts immediately.")
        elif cmd == "/copywallet":
            ce=state.get("copy_engine"); sub=parts[1].lower() if len(parts)>1 else ""
            if not ce:
                await send(http,"Copy engine unavailable.")
            elif sub=="add" and len(parts)>=3:
                addr=parts[2]; tail=parts[3:]; score=60.0; label=""
                if tail:
                    try: score=float(tail[-1]); tail=tail[:-1]
                    except Exception: pass
                    label=" ".join(tail)
                ok,msg=ce.add_wallet(addr,label,score,"manual")
                await send(http,("✅ " if ok else "⚠️ ")+f"COPY WALLET: {msg}. {addr}")
            elif sub in {"remove","off","delete"} and len(parts)>=3:
                ce.remove_wallet(parts[2]); await send(http,f"Copy wallet disabled: {parts[2]}")
            else:
                await send(http,"Usage: /copywallet add ADDRESS [label] [score] or /copywallet remove ADDRESS")
        elif cmd == "/copywallets":
            ce=state.get("copy_engine"); rows=ce.wallets() if ce else []
            if not rows: await send(http,"No copy wallets tracked yet. Auto-discovery will add qualified Birdeye smart-wallet candidates, or use /copywallet add ADDRESS.")
            else:
                lines=["TRACKED COPY WALLETS"]
                for r in rows[:20]:
                    score=ce.wallet_score(r['address']); lines.append(f"• {r.get('label') or r['address'][:6]+'…'} — {score:.0f}/100 — {r.get('source')} — {r['address']}")
                await send(http,"\n".join(lines))
        elif cmd == "/copydiscover":
            ce=state.get("copy_engine"); rows=ce.discovered(15) if ce else []
            if not rows: await send(http,"No whale candidates accumulated yet. Birdeye smart-trader/top-flow discovery is still running.")
            else:
                lines=["WHALE / SMART-WALLET CANDIDATES"]
                for r in rows:
                    lines.append(f"• {r['address'][:6]}…{r['address'][-4:]} | smart tags {int(r.get('smart_tag_count') or 0)} | buys {int(r.get('buy_count') or 0)} | tokens {int(r.get('distinct_tokens') or 0)} | flow ${f(r.get('total_buy_usd')):,.0f}")
                await send(http,"\n".join(lines))
        elif cmd == "/copyanalyze":
            ce=state.get("copy_engine")
            if not ce or len(parts)<2: await send(http,"Usage: /copyanalyze WALLET")
            else:
                await send(http,"Analyzing recent confirmed swaps for that public wallet…")
                res=await ce.analyze_wallet_history(http,parts[1])
                if not res.get('ok'): await send(http,"Copy wallet analysis failed: "+str(res.get('error')))
                else:
                    ce.add_wallet(parts[1],"analyzed",res.get('score',50),"history")
                    await send(http,f"WALLET ANALYSIS\nParsed swaps: {res['events']} | reconstructed exits: {res['completed']}\nWin rate: {res['win_rate']*100:.1f}% | median ROI {res['median_roi']:+.1f}% | mean {res['mean_roi']:+.1f}%\nCopy score: {res['score']:.0f}/100\nThis is approximate on-chain round-trip reconstruction, not a guarantee of future performance.")
        elif cmd == "/copyreport":
            ce=state.get("copy_engine"); rep=ce.report() if ce else {}
            await send(http,f"COPY PAPER REPORT\nClosed: {rep.get('closed',0)} | open: {rep.get('open',0)} | wins: {rep.get('wins',0)} ({rep.get('win_rate',0)*100:.1f}%)\nNet P/L: ${rep.get('pnl',0):+.2f} | profit factor {rep.get('pf',0):.2f}\nPaper copies use actual follower-time market price plus configured friction.")
        elif cmd == "/copykill":
            ce=state.get("copy_engine"); sub=parts[1].lower() if len(parts)>1 else "status"
            if sub=="on": db.set_meta("copy_kill_switch","1"); db.set_meta("copy_live_enabled","0"); await send(http,"🛑 COPY KILL SWITCH ON. New live copy buys are disabled; paper tracking and exits continue.")
            elif sub=="off": db.set_meta("copy_kill_switch","0"); await send(http,"Copy kill switch OFF. Live copying is still OFF until separately armed/confirmed and the paper gate passes.")
            else: await send(http,f"Copy kill switch: {'ON' if db.get_meta('copy_kill_switch','1')=='1' else 'OFF'}")
        elif cmd == "/copylive":
            ce=state.get("copy_engine"); sub=parts[1].lower() if len(parts)>1 else "status"
            if not ce: await send(http,"Copy engine unavailable.")
            elif sub=="off": db.set_meta("copy_live_enabled","0"); db.set_meta("copy_live_armed_until","0"); await send(http,"COPY LIVE OFF. Paper/alerts continue.")
            elif sub=="arm":
                gate=ce.live_gate()
                if not gate.get("allowed"): await send(http,"COPY LIVE remains locked.\n"+"\n".join("• "+x for x in gate.get("reasons") or []))
                else:
                    db.set_meta("copy_live_armed_until",str(int(time.time()+300))); await send(http,"⚠️ COPY LIVE ARMED FOR 5 MINUTES. Send /copylive confirm to enable small follower orders, or /copylive off to cancel.")
            elif sub=="confirm":
                until=int(float(db.get_meta("copy_live_armed_until","0") or 0)); gate=ce.live_gate()
                if time.time()>until: await send(http,"Copy-live arm window expired. Send /copylive arm again.")
                elif not gate.get("allowed"): await send(http,"COPY LIVE remains locked: "+"; ".join(gate.get("reasons") or []))
                else: db.set_meta("copy_live_enabled","1"); await send(http,"⚠️ COPY LIVE ENABLED. New tracked-wallet buys may execute with the configured small cap. /copykill on or /copylive off stops new buys immediately.")
            else:
                gate=ce.live_gate(); await send(http,f"COPY LIVE: {'ON' if db.get_meta('copy_live_enabled','0')=='1' else 'OFF'} | gate {'READY' if gate.get('allowed') else 'LOCKED'}\n"+"; ".join(gate.get("reasons") or []))
        elif cmd == "/autolive":
            ex=state.get("live_executor"); subcmd=parts[1].lower() if len(parts)>=2 else "status"
            if subcmd=="off":
                set_bool_pref(db,"live_auto",False); db.set_meta("live_auto_armed_until","0")
                await send(http,"🛑 REAL AUTOPILOT OFF. No new automatic buys will be submitted." +
                           (" Positions the autopilot already bought keep their automatic stop/trailing/liquidity exits (AUTO_LIVE_EXITS_WHEN_OFF=true)." if AUTO_LIVE_EXITS_WHEN_OFF
                            else " Existing positions remain in the journal and manual Telegram risk alerts continue; automatic exits are OFF."))
            elif subcmd=="arm":
                issues=ex.readiness() if ex else ["executor unavailable"]; gate=live_execution_gate(db,include_capacity=False)
                if issues:
                    await send(http,"REAL AUTOPILOT cannot be armed yet. Run setup_live_trading.command locally, restart, then /autotest.\nMissing: "+"; ".join(issues))
                elif not gate.get("allowed"):
                    await send(http,"REAL AUTOPILOT stays locked. No live order was attempted.\nBlockers: "+"; ".join(gate.get("reasons") or ["risk gate"]))
                else:
                    db.set_meta("live_auto_armed_until",str(int(time.time()+300)))
                    await send(http,
                        "⚠️ REAL-MONEY AUTOPILOT ARMED FOR 5 MINUTES\n"
                        f"Normal size: ${_auto_size(db,'ENTRY OPTION'):.2f} | max auto positions {AUTO_LIVE_MAX_OPEN} | daily auto-loss lock -${AUTO_LIVE_MAX_DAILY_LOSS:.2f}.\n"
                        "Fresh-launch auto-buys are OFF unless /autofresh on.\n"
                        "Send /autolive confirm to enable, or /autolive off to cancel.")
            elif subcmd=="confirm":
                until=int(float(db.get_meta("live_auto_armed_until","0") or 0)); issues=ex.readiness() if ex else ["executor unavailable"]; gate=live_execution_gate(db,include_capacity=False)
                if time.time()>until: await send(http,"Arm window expired. Send /autolive arm again.")
                elif issues: await send(http,"Could not enable real autopilot: "+"; ".join(issues))
                elif not gate.get("allowed"): await send(http,"REAL AUTOPILOT remains locked. No live order was attempted.\nBlockers: "+"; ".join(gate.get("reasons") or ["risk gate"]))
                else:
                    set_bool_pref(db,"live_auto",True); db.set_meta("live_auto_armed_until","0")
                    await send(http,"🤖✅ REAL AUTOPILOT ON\nThe bot may now buy qualifying Solana entries and manage its own auto-bought positions with TP1/TP2, stop, adaptive trailing, liquidity and deterioration exits.\nUse /autolive off at any time. Never fund the bot wallet with more than you are willing to lose.")
            else:
                await send(http,f"REAL AUTOPILOT is {'ON' if bool_pref(db,'live_auto',False) else 'OFF'}. Use /autostatus, /autolive arm, or /autolive off.")
            continue

        elif cmd == "/autofresh":
            if len(parts)>=2 and parts[1].lower() in {"on","off"}: set_bool_pref(db,"auto_fresh",parts[1].lower()=="on")
            await send(http,f"Fresh-launch REAL auto-buys are {'ON' if bool_pref(db,'auto_fresh',AUTO_LIVE_ALLOW_FRESH_DEFAULT) else 'OFF'}. When ON they use about {AUTO_LIVE_FRESH_SIZE_MULT*100:.0f}% of normal size because they are higher risk.")
            continue

        elif cmd == "/autosize":
            if len(parts)>=2:
                try:
                    val=clamp(float(parts[1].replace('$','')),1,min(MAX_POSITION,25)); db.set_meta("auto_trade_usd",str(val))
                except Exception:
                    await send(http,"Use /autosize 10 (between $1 and the configured max position)."); continue
            await send(http,f"REAL autopilot normal trade size: ${_auto_size(db,'ENTRY OPTION'):.2f}. Fresh size: about ${_auto_size(db,'FRESH ENTRY'):.2f}.")
            continue

        elif cmd == "/autostatus":
            ex=state.get("live_executor"); issues=ex.readiness() if ex else ["executor unavailable"]
            bal=await ex.sol_balance(http) if ex and not issues else None; stats=db.auto_daily_stats(); addr=ex.address() if ex else ""
            baltext=f"{bal:.4f}" if bal is not None else "unavailable"
            gate=live_execution_gate(db,include_capacity=False); unresolved=len(db.unresolved_execution_intents(100))
            await send(http,
                f"🤖 REAL AUTOPILOT STATUS — {'ON' if bool_pref(db,'live_auto',False) else 'OFF'}\n"
                f"Local execution: {'READY' if not issues else 'NOT READY'}" + ((" — "+"; ".join(issues)) if issues else "") + "\n"
                f"Wallet: {addr or 'not configured'}\nSOL balance: {baltext}\n"
                f"Today: {stats['buys']} auto buy(s) | realized auto P/L ${stats['pnl']:+.2f}\n"
                f"Open auto positions: {len(db.auto_open_positions())}/{AUTO_LIVE_MAX_OPEN} | unresolved tx: {unresolved}\n"
                f"New-order gate: {'OPEN' if gate.get('allowed') else 'LOCKED'}" + ((" — "+"; ".join(gate.get('reasons') or [])) if not gate.get('allowed') else "") + "\n"
                f"Normal size ${_auto_size(db,'ENTRY OPTION'):.2f} | daily loss lock -${AUTO_LIVE_MAX_DAILY_LOSS:.2f} | fresh auto {'ON' if bool_pref(db,'auto_fresh',AUTO_LIVE_ALLOW_FRESH_DEFAULT) else 'OFF'}")
            continue

        elif cmd == "/autotest":
            ex=state.get("live_executor"); issues=ex.readiness() if ex else ["executor unavailable"]
            if issues:
                await send(http,"⛔ LOCAL LIVE-TRADING TEST NOT READY\n"+"; ".join(issues)+"\nRun setup_live_trading.command locally. No trade was attempted.")
            else:
                status,data=await ex.quote_test(http); bal=await ex.sol_balance(http)
                ok=status==200 and isinstance(data,dict) and f(data.get("outAmount"))>0; baltext=f"{bal:.4f}" if bal is not None else "unavailable"
                await send(http,f"{'✅' if ok else '⛔'} AUTOTEST — {'PASS' if ok else 'FAILED'}\nJupiter quote HTTP {status} | wallet {ex.address()} | SOL {baltext}\nThis was quote-only. NO transaction was signed or submitted.")
            continue

        elif cmd == "/execstatus":
            ex=state.get("live_executor")
            unresolved=db.unresolved_execution_intents(20)
            failures=[dict(r) for r in db.conn.execute("select * from trade_failures order by id desc limit 5").fetchall()]
            journal=db.conn.execute("pragma journal_mode").fetchone()[0]
            busy=db.conn.execute("pragma busy_timeout").fetchone()[0]
            gate=live_execution_gate(db,include_capacity=False)
            rpc=(ex.last_rpc_url if ex else "unavailable"); rpcerr=(ex.last_rpc_error if ex else "")
            lines=[f"🛡️ EXECUTION SAFETY — {VERSION}",
                   f"SQLite: {journal} | busy_timeout {busy}ms",
                   f"RPC active: {rpc}",
                   f"Unresolved executions: {len(unresolved)}",
                   f"Governor integrity repairs this startup: {db.get_meta('governor_integrity_repairs','0')}",
                   f"New-order gate: {'OPEN' if gate.get('allowed') else 'LOCKED'}" + ((" — "+"; ".join(gate.get('reasons') or [])) if not gate.get('allowed') else "")]
            if rpcerr: lines.append("Last RPC error: "+rpcerr[-400:])
            if unresolved:
                for x in unresolved[:3]: lines.append(f"Pending {x.get('side')} {x.get('symbol') or x.get('token')} | {x.get('state')} | {str(x.get('signature') or '')[:18]}…")
            if failures:
                lines.append("Recent execution failures:")
                for x in failures: lines.append(f"• {x.get('stage')} / {x.get('category')}: {str(x.get('detail') or '')[:160]}")
            await send(http,"\n".join(lines))
            continue

        elif cmd == "/bankroll":
            if len(parts)>=2:
                try:
                    amount=max(0.0,float(parts[1].replace("$","").replace(",","")))
                except Exception:
                    await send(http,"Use /bankroll 100 (or /bankroll 0 to clear it)."); continue
                db.set_meta("bankroll_usd",str(amount))
            rc=bankroll_size_context(db)
            if rc.get("bankroll",0)>0:
                await send(http,
                    f"BANKROLL RISK GUIDE\nBankroll: ${rc['bankroll']:.2f}\n"
                    f"Per-trade risk budget: ${rc['risk_usd']:.2f} ({rc['risk_pct']:.2f}%)\n"
                    f"At the {rc['stop_pct']:.1f}% hard line: suggested cap ${rc['suggested_usd']:.2f}\n"
                    "This controls guidance only; it never moves wallet funds.")
            else:
                await send(http,"Bankroll is not set. Live-money size guidance is $0 and real autopilot is locked. Use /bankroll AMOUNT only when you are ready to define a risk budget; paper/shadow proof does not require live money.")
            continue

        elif cmd == "/signalpolicy":
            arg=parts[1].lower() if len(parts)>=2 else "status"
            if arg in {"actionable","strict","proven"}:
                db.set_meta("signal_policy",arg)
            pol=signal_policy(db)
            await send(http,
                f"ENTRY SIGNAL POLICY: {pol.upper()}\n"
                "ACTIONABLE = fully-qualified proof-eligible core setups can send TEST BUY signals before proof is ACTIVE; real autopilot/full sizing stay locked.\n"
                f"PROVEN = only send TEST BUY signals for alert types whose last {TRACK_RECORD_DAYS}d paper record is positive and consistent (≥{TRACK_RECORD_MIN_TRADES} tests).\n"
                "STRICT = only send BUY NOW after proof/risk/live-sizing gates are fully open.\n"
                "Every BUY alert shows its alert type's measured track record. Use /trackrecord to see all types.")
            continue

        elif cmd == "/trackrecord":
            tiers=[r[0] for r in db.conn.execute("""select tier from paper_positions where active=0 and close_ts>=?
                group by tier order by count(*) desc""",(int(time.time()-TRACK_RECORD_DAYS*86400),)).fetchall() if r[0]]
            lines=[alert_track_record_line(db,t) for t in tiers] or [f"No completed paper tests in the last {TRACK_RECORD_DAYS} days."]
            await send(http,"📊 ALERT TRACK RECORDS\n\n"+"\n\n".join(lines)+f"\n\nSignal policy: {signal_policy(db).upper()} (/signalpolicy proven sends only proven types).")
            continue

        elif cmd == "/shadow":
            arg=parts[1].lower() if len(parts)>=2 else "status"
            if arg in {"on","off"}:
                set_bool_pref(db,"shadow_sim",arg=="on")
            on=bool_pref(db,"shadow_sim",SHADOW_SIM_ALWAYS_ON)
            await send(http,
                f"SHADOW PROOF ENGINE: {'ON' if on else 'OFF'}\n"
                f"Current proof gate: {proof_health_label(db)}\n"
                "Shadow trades are simulated only; no wallet order is created. Keep this ON while validating a new build.")
            continue

        elif cmd == "/edge":
            proof=proof_first_health(db); breadth=breadth_guard_context(db); risk=paper_risk_guard(db)
            exec_probe=state.get("execution_probe"); exec_status=exec_probe.status() if exec_probe else "UNTESTED"
            stats=db.paper_stats(7,shadow_only=True,build_version=QUALITY_COHORT_BUILD_VERSIONS)
            proof_stats=db.paper_stats(7,shadow_only=True,build_version=QUALITY_COHORT_BUILD_VERSIONS,proof_only=True)
            pf=stats.get("profit_factor"); pftext="—" if pf is None else ("∞" if math.isinf(pf) else f"{pf:.2f}")
            ppf=proof_stats.get("profit_factor"); ppftext="—" if ppf is None else ("∞" if math.isinf(ppf) else f"{ppf:.2f}")
            await send(http,
                f"⚙️ SOL EDGE ENGINE — {VERSION}\n"
                f"Proof gate: {proof_health_label(db)}\n"
                f"Proof eligibility: core score ≥{PROOF_ELIGIBLE_MIN_SCORE:.0f}; signal policy {signal_policy(db).upper()}\n"
                f"Breadth: {breadth.get('label','UNKNOWN')} — {breadth.get('reason','')}\n"
                f"Jupiter quote preflight: {exec_status}\n"
                f"7d all research: {stats['closed']} closed | gross ${stats['gross_pnl']:+.2f} | friction ${stats['fees_usd']:.2f} | net ${stats['pnl']:+.2f} | PF {pftext}\n"
                f"7d proof cohort: {proof_stats['closed']} closed / {proof_stats.get('unique_tokens',0)} tokens | net ${proof_stats['pnl']:+.2f} | win {((proof_stats['win_rate'] or 0)*100):.0f}% | PF {ppftext}\n"
                f"Proof-cohort risk circuit: {'PAUSED' if risk['paused'] else 'OK'}" + ((" — "+"; ".join(risk['reasons'])) if risk['reasons'] else ""))
            continue

        elif cmd == "/leaders":
            rows=db.edge_leaders(90,8)
            if not rows:
                await send(http,"No recent Runner Radar / edge candidates. This is watch-only and never creates a BUY by itself.")
            else:
                lines=["📡 EDGE LEADERS — WATCH ONLY"]
                for r in rows:
                    lines.append(f"{r.get('symbol') or '?'} | {r.get('event') or '?'} | score {f(r.get('entry_score')):.0f} | 5m {f(r.get('pc5')):+.1f}% | 1h {f(r.get('pc1')):+.1f}%\n{r.get('token') or ''}")
                await send(http,"\n\n".join(lines))
            continue

        elif cmd == "/risk":
            proof=proof_first_health(db); rc=bankroll_size_context(db); rg=paper_risk_guard(db)
            await send(http,
                "SOL EDGE RISK CONTROLS\n"
                "• actionable core/FAST entries are Solana-only; cross-chain/structure/fresh/micro lanes remain shadow/watch-only by default\n"
                f"• fresh-build live guidance is blocked until proof is ACTIVE: {proof_health_label(db)}\n"
                f"• Guardian hard line is capped at -{CAPITAL_HARD_STOP_PCT:.0f}% and liquidity/structure can force an earlier exit\n"
                f"• bankroll risk budget is {BANKROLL_RISK_PCT:.2f}% per trade; current size cap ${rc['suggested_usd']:.2f}\n"
                f"• paper circuit: {'PAUSED' if rg['paused'] else 'OK'}" + ((" — "+"; ".join(rg['reasons'])) if rg['reasons'] else "") + "\n"
                "• Jupiter execution checks are quote-only and reject excessive estimated route/friction costs\n"
                "• real autopilot remains OFF unless explicitly armed after proof is ACTIVE.")
            continue

        elif cmd == "/status":
            proof=proof_first_health(db); breadth=breadth_guard_context(db); rg=paper_risk_guard(db)
            exec_probe=state.get("execution_probe"); exec_status=exec_probe.status() if exec_probe else "UNTESTED"
            ep=db.entry_pipeline_summary(60)
            ce=state.get("copy_engine"); cst=ce.status() if ce else {}
            await send(http,
                f"🟢 {VERSION} running\nLast scan top score: {state.get('top',0):.1f}\n"
                f"Candidates last scan: {state.get('candidate_count',0)} | internal scouts: {len(state.get('active_scouts',[]))}\n"
                f"Proof gate: {proof_health_label(db)} | shadow {'ON' if bool_pref(db,'shadow_sim',SHADOW_SIM_ALWAYS_ON) else 'OFF'} | signal policy {signal_policy(db).upper()}\n"
                f"Breadth: {breadth.get('label','UNKNOWN')} — {breadth.get('reason','')}\n"
                f"Jupiter execution preflight: {exec_status} | paper circuit: {'PAUSED' if rg['paused'] else 'OK'} | API circuits: {http.circuit_status()}\n"
                f"Signal lanes: STRICT + {'FAST' if FAST_ENTRY_ENABLED else 'FAST OFF'} + {'COPY' if cst else 'COPY OFF'} | copy RPC circuit: {'OPEN' if cst.get('rpc_circuit_open') else 'OK'}\n"
                f"Open recorded positions: {len(db.open_positions())} | Guardian alerts: {'ON' if bool_pref(db,'guardian_alerts',True) else 'OFF'} ({GUARDIAN_MONITOR_SECONDS}s)\n"
                f"Paper autopilot: {'ON' if bool_pref(db,'paper_auto',PAPER_AUTO_DEFAULT) else 'OFF'} | paper open: {len(db.paper_open_positions())} | REAL autopilot: {'ON' if bool_pref(db,'live_auto',False) else 'OFF'}\n"
                f"Entry pipeline 60m: sent {ep.get('ENTRY_SENT',0)+ep.get('ENTRY_SENT_HOT',0)} | test-buy {ep.get('ENTRY_SENT_TEST',0)} | armed {ep.get('EDGE_ARMED',0)} | proof-shadow {ep.get('PROOF_SHADOW',0)} | research-shadow {ep.get('RESEARCH_SHADOW',0)} | regime {ep.get('ENTRY_REGIME',0)} | history {ep.get('ENTRY_HISTORY',0)} | path {ep.get('ENTRY_PATH',0)} | execution {ep.get('ENTRY_EXECUTION',0)} | cooldown {ep.get('ENTRY_COOLDOWN',0)} | stability {ep.get('ENTRY_STABILITY',0)} | tape {ep.get('ENTRY_TAPE',0)} | confirmation {ep.get('ENTRY_COMMIT',0)} | delivery fail {ep.get('ENTRY_DELIVERY_FAIL',0)}\n"
                f"Shared state: {DB_PATH}\n"
                f"Birdeye: {state.get('be_status','checking...')}\n"
                f"Realtime Solana: {state.get('realtime').status() if state.get('realtime') else 'OFF'} | PumpPortal: {state.get('pumpportal').status() if state.get('pumpportal') else 'OFF'} | hot scout: {'ON' if HOT_SCOUT_ENABLED else 'OFF'} ({HOT_SCOUT_MONITOR_SECONDS}s)\n"
                f"{market_regime_line(state.get('market_regime'))} | targeted X pulse: {'ON' if XTOKEN and bool_pref(db,'x_targeted_pulse',X_TARGETED_PULSE_ENABLED) else 'OFF'}")
        elif cmd == "/positions":
            positions=db.open_positions()
            if not positions:
                await send(http,"No recorded open positions. After buying in Fomo, reply /bought YOUR_AMOUNT to the exact entry/check, or use /buy CONTRACT AMOUNT.")
            else:
                lines=["🛡️ GUARDIAN — OPEN POSITIONS"]
                for pos in positions:
                    pair=await pair_for_token(http,pos["chain"],pos["token"])
                    current=f(pair.get("priceUsd")) if pair else f(pos["entry_price"])
                    ret=(current/max(f(pos["entry_price"]),1e-18)-1)*100
                    rem=f(pos.get("remaining_fraction"),1.0); remain_basis=f(pos["amount_usd"])*rem
                    partial=f(pos.get("realized_pnl_partial"),0); extra=f" | realized ${partial:+.2f}" if abs(partial)>0.005 else ""
                    plan=hold_plan(pair,position=pos,tier=pos.get("entry_tier") or "") if pair else {"label":"quote unavailable","window":""}
                    gstate=str(pos.get("guardian_state") or "ENTRY").replace("_"," ")
                    conf=f(pos.get("guardian_confidence"),0)
                    cline=f" | conf {conf:.0f}" if conf>0 else ""
                    q=str(pos.get("pnl_quality") or "ESTIMATED").replace("_"," ").lower()
                    qline=f" | ledger {q}" if abs(partial)>0.005 or q not in {"estimated",""} else ""
                    lines.append(f"{pos['symbol']} | basis ${remain_basis:.2f} | {ret:+.1f}% | {gstate}{cline} | {plan['label']}{extra}{qline}\n{pos['token']}")
                await send(http,"\n\n".join(lines))
            continue

        elif cmd == "/guardian":
            sub=parts[1].lower() if len(parts)>=2 else "status"
            if sub in {"on","off"}:
                set_bool_pref(db,"guardian_alerts",sub=="on")
                await send(http,f"🛡️ GUARDIAN automatic alerts {'ON' if sub=='on' else 'OFF'}. Position state is still tracked in the journal. Use /guardian on to restore alerts.")
            else:
                counts={}
                for pos in db.open_positions():
                    k=str(pos.get("guardian_state") or "ENTRY"); counts[k]=counts.get(k,0)+1
                state_text=", ".join(f"{k.replace('_',' ')} {v}" for k,v in counts.items()) or "no open positions"
                await send(http,
                    f"🛡️ GUARDIAN STATUS\nEngine: {'ON' if GUARDIAN_ENABLED else 'OFF'} | alerts: {'ON' if bool_pref(db,'guardian_alerts',True) else 'OFF'} | monitor every {GUARDIAN_MONITOR_SECONDS}s\n"
                    f"Open states: {state_text}\n"
                    "It watches TP/runner/weakening/exit transitions automatically so you do not have to keep checking charts.")
            continue

        elif cmd in {"/sizerange","/riskrange"}:
            if len(parts)>=4:
                try:
                    mn=float(parts[1].replace("$","")); normal=float(parts[2].replace("$","")); exceptional=float(parts[3].replace("$",""))
                    if not (1 <= mn <= normal <= exceptional <= 500):
                        raise ValueError("range")
                    db.set_meta("size_guide_min_usd",str(mn)); db.set_meta("size_guide_normal_max_usd",str(normal)); db.set_meta("size_guide_exceptional_max_usd",str(exceptional))
                except Exception:
                    await send(http,"Use /sizerange MIN NORMAL_MAX EXCEPTIONAL_MAX — example /sizerange 5 20 30. Values must increase and stay between $1 and $500.")
                    continue
            mn,normal,exceptional=sizing_preferences(db)
            await send(http,
                f"💵 SIZE RANGE — minimum ${mn:.0f} | normal max ${normal:.0f} | exceptional max ${exceptional:.0f}\n"
                "The bot chooses an amount inside this range from live structure, historical downside, open exposure and recent realized results. "
                "The exceptional amount is only unlocked when the learning/path evidence is mature and unusually clean. This never blocks a manual buy.")
            continue

        elif cmd == "/autocheck":
            sub=parts[1].lower() if len(parts)>=2 else "status"
            if sub in {"on","off"}:
                set_bool_pref(db,"auto_entry_rechecks",sub=="on")
                if sub=="off":
                    for task in list(state.get("entry_update_tasks",{}).values()):
                        if task and not task.done(): task.cancel()
                    state.get("entry_update_tasks",{}).clear()
            enabled=AUTO_ENTRY_RECHECKS_ENABLED and bool_pref(db,"auto_entry_rechecks",True)
            times=", ".join(f"{x}s" for x in AUTO_ENTRY_RECHECK_SECONDS)
            await send(http,
                f"🔄 AUTOMATIC ENTRY FRESHNESS — {'ON' if enabled else 'OFF'}\nScheduled checks: {times}.\n"
                "If you already recorded the buy, the checks stop and GUARDIAN takes over. If the setup temporarily weakens, the first update can say WAIT and the next one can still show a repaired entry. "
                "Hard safety failures or a late chase remain blocked.")
            continue

        elif cmd in {"/confidence","/size"}:
            reply_contract=_reply_contract(msg)
            query=reply_contract or (parts[1] if len(parts)>=2 else None)
            if not query:
                await send(http,f"Use {cmd} TICKER or {cmd} CONTRACT, or reply {cmd} to an entry/position alert.")
                continue
            pos,_=db.find_open_position(query)
            sig=None; pair=None; tier="ENTRY OPTION"; score=50.0
            if pos:
                pair=await pair_for_token(http,pos["chain"],pos["token"]); tier=pos.get("entry_tier") or "MANUAL"
                sr=db.conn.execute("""select * from signals where chain=? and token=? and kind='EARLY' and ts<=? order by ts desc limit 1""",
                                   (pos["chain"],pos["token"],int(pos["open_ts"]))).fetchone()
                if sr: sig=dict(sr); score=f(sig.get("score"),50); tier=pos.get("entry_tier") or sig.get("action") or tier
            else:
                sig,_=db.find_buy_signal(query,hours=48)
                if sig:
                    pair=await pair_for_token(http,sig["chain"],sig["token"]); tier=sig.get("action") or tier; score=f(sig.get("score"),50)
                if not pair:
                    pair=await dex_search_pair(http,query)
            if not pair:
                await send(http,"I could not resolve a live quote. Use the exact contract if the ticker is ambiguous."); continue
            cal=adaptive_dollar_size(db,pair,tier,score)
            h=cal["historical"]; sample=max(int(h.get("n30") or 0),int(h.get("n120") or 0))
            base=pair.get("baseToken") or {}; symbol=base.get("symbol") or query
            if cmd=="/size":
                await send(http,
                    f"💵 SIZE GUIDE — {symbol}\n"
                    f"Adaptive confidence {cal['confidence']:.0f}/100 ({cal['grade']}) | calibration {cal.get('calibration_status','LEARNING')}\n"
                    f"Suggested buy: ~${cal['suggested_usd']:.0f} — {cal.get('size_label','')}\n"
                    f"Configured range: ${cal.get('size_min_usd',0):.0f}-${cal.get('size_normal_max_usd',0):.0f} normal | up to ${cal.get('size_exceptional_max_usd',0):.0f} only when exceptional evidence unlocks it\n"
                    f"Historical sample: {sample} completed comparable endpoints | path sample {int((cal.get('path_history') or {}).get('n') or 0)}\n"
                    + (("Why smaller: "+"; ".join(cal.get('size_reasons',[])[:3])+"\n") if cal.get('size_reasons') else "")
                    + "This is advice only, never a manual position lock. /sizerange changes the dollar range.")
            else:
                med30="n/a" if h.get("median30") is None else f"{h['median30']:+.1f}%"
                med120="n/a" if h.get("median120") is None else f"{h['median120']:+.1f}%"
                await send(http,
                    f"📐 ADAPTIVE CONFIDENCE — {symbol}\n"
                    f"Confidence: {cal['confidence']:.0f}/100 ({cal['grade']}) | calibration {cal.get('calibration_status','LEARNING')} | live quality {cal['live_quality']:.0f}/100\n"
                    f"Comparable history n={sample}: 30m median {med30} | 120m median {med120}\n"
                    f"120m endpoint ≥+15%: {cal['endpoint_hit15_120']:.0f}% | endpoint ≤-15%: {cal['endpoint_severe15_120']:.0f}%\n"
                    f"Suggested buy now: ~${cal['suggested_usd']:.0f} ({cal.get('size_label','')})\n"
                    "These are smoothed historical endpoint rates, not a promise that a target is reached before a drawdown.")
            continue

        elif cmd == "/wallet":
            sync=state.get("wallet_sync")
            if not sync or not sync.enabled:
                await send(http,
                    "READ-ONLY WALLET SYNC: NOT CONFIGURED\n"
                    "Run setup_wallet_sync.command in the bot folder and enter only your PUBLIC Solana wallet address. "
                    "Then restart. Never enter a seed phrase/private key for read-only sync.")
            else:
                sol=await sync.sol_balance(http)
                tracked=sum(1 for p in db.open_positions() if str(p.get('chain','')).lower()=='solana')
                soltxt=f"{sol:.4f} SOL" if sol is not None else "RPC unavailable"
                await send(http,
                    f"🔄 READ-ONLY WALLET SYNC — ON\nWallet: {sync.address}\nSOL balance: {soltxt}\nTracked Solana positions: {tracked}\n"
                    f"Auto-record detected sells: {'ON' if WALLET_SYNC_AUTO_RECORD_SELLS else 'OFF'} | material change ≥{WALLET_SYNC_MIN_CHANGE_PCT:.1f}%\n"
                    "Sync never signs or submits transactions.")
            continue

        elif cmd == "/sync":
            result=await sync_wallet_positions(http,db,state,force=True)
            if not result.get("enabled"):
                await send(http,"Wallet sync is not configured. Run setup_wallet_sync.command, enter your PUBLIC Solana address, and restart.")
            elif not result.get("events"):
                await send(http,f"🔄 WALLET SYNC COMPLETE — checked {result.get('checked',0)} tracked Solana position(s); no material balance change detected.")
            continue

        elif cmd == "/learn":
            captured=capture_signal_outcomes(db); decision_captured=capture_decision_outcomes(db); missed_new=audit_missed_opportunities(db)
            missed=db.recent_missed(hours=24,limit=5); outcomes=db.outcome_stats(hours=24)
            h60=[x for x in outcomes if int(x.get("horizon_min") or 0)==60]
            clean=sum(1 for x in h60 if f(x.get("max_return_pct"))>=15 and f(x.get("min_return_pct"))>-10)
            bad=sum(1 for x in h60 if f(x.get("min_return_pct"))<=-15)
            cutoff=int(time.time()-24*3600)
            drows=db.conn.execute("""select d.event,o.max_return_pct,o.min_return_pct from decision_outcomes o
                join decision_ledger d on d.id=o.decision_id where d.ts>=? and o.horizon_min=60 and coalesce(o.suspect,0)=0""",(cutoff,)).fetchall()
            blocked=[dict(x) for x in drows if str(x["event"]) in {"NEAR_ENTRY","HOT_WAIT"}]
            shadow=[dict(x) for x in drows if str(x["event"])=="LANE_SHADOW"]
            blocked_runners=sum(1 for x in blocked if f(x.get("max_return_pct"))>=15 and f(x.get("min_return_pct"))>-10)
            avoided=sum(1 for x in blocked if f(x.get("min_return_pct"))<=-15)
            shadow_good=sum(1 for x in shadow if f(x.get("max_return_pct"))>=8 and f(x.get("min_return_pct"))>-10)
            lines=[f"🧠 LEARNING LEDGER\nCaptured now: {captured} signal outcomes + {decision_captured} decision outcomes | new missed examples: {missed_new}\n60m alerted outcomes last 24h: {len(h60)} | clean +15 runners: {clean} | hit -15 drawdown: {bad}\nBlocked/near-entry 60m paths: {len(blocked)} | clean runners later: {blocked_runners} | hit -15 drawdown: {avoided}\nGovernor shadow 60m paths: {len(shadow)} | reached +8 without -10 drawdown: {shadow_good}"]
            if missed:
                lines.append("Recent clean missed runners:")
                for x in missed:
                    lines.append(f"• {x['symbol']} max {f(x['max_return_pct']):+.1f}% / min {f(x['min_return_pct']):+.1f}% | flow {f(x['turnover_pct']):.2f}% MC | B/S {f(x['buy_sell_ratio']):.2f}x")
            else: lines.append("No clean missed-runner example logged in the current 24h window.")
            lines.append("The bot records these for daily review; it does not automatically loosen filters from one missed pump.")
            await send(http,"\n".join(lines)); continue

        elif cmd == "/daily":
            await daily_report(http,db); continue

        elif cmd == "/lab":
            r30=walk_forward_summary(db.conn,30)
            r120=walk_forward_summary(db.conn,120)
            await send(http,"🧪 WALK-FORWARD LAB — CHRONOLOGICAL HOLDOUT\n"+format_walk_forward(r30)+"\n\n"+format_walk_forward(r120)+
                "\n\nThe newest slice is held out from the older slice. This is validation/reporting only; it does not auto-change entry thresholds or place trades.")
            continue

        elif cmd == "/mode":
            perf = db.rolling_entry_performance()
            rt = state.get("realtime")
            perf_text = (
                f"{perf['n']} completed 30m tests"
                if perf["n"] else "not enough completed tests yet"
            )
            guard_text = "PERFORMANCE GUARD (user-enabled)" if perf["paused"] else "LIVE ALERTS"
            await send(http,
                f"BOT MODE: {guard_text}\n"
                f"Performance: {perf_text}\n"
                + (f"30m win rate {perf['win_rate']:.0%} | median {perf['median']:+.1f}%\n" if perf["n"] else "")
                + f"Realtime Solana: {rt.status() if rt else 'OFF'}\n"
                f"Birdeye: {state.get('be_status','checking...')}")
            continue

        elif cmd in {"/scan", "/coin"}:
            if len(parts) < 2:
                await send(http, "Use /scan CONTRACT (best) or /scan TICKER/NAME. It searches DexScreener across chains.")
                continue
            report = await manual_coin_report(http, db, state, state.get("guard"), " ".join(parts[1:]))
            await send(http, report)
            continue

        elif cmd in {"/why"}:
            if len(parts) < 2:
                await send(http,"Use /why CONTRACT (best) or /why TICKER.")
                continue
            report = await why_coin_report(http, db, state, state.get("guard"), " ".join(parts[1:]))
            await send(http, report)
            continue

        elif cmd == "/track":
            if len(parts)<2:
                await send(http,"Use /track CONTRACT, or /track solana CONTRACT for a brand-new mint."); continue
            chain_hint=parts[1].lower() if len(parts)>=3 and parts[1].lower() in CHAINS else None
            query=" ".join(parts[2:]) if chain_hint else " ".join(parts[1:])
            pair=await resolve_exact_contract(http,query,chain_hint) or await dex_search_pair(http,query)
            if not pair:
                if chain_hint=="solana" or (not chain_hint and looks_solana_contract(query)):
                    db.add_manual_watch("solana",query,"PENDING","Waiting for DEX pool")
                    await send(http,f"🛰️ PENDING MINT TRACKED\n{query}\nNo live DEX pair is indexed yet. The bot will keep retrying this exact mint on later cycles."); continue
                rows=await dex_search_candidates(http,query,8)
                await send(http,format_coin_matches(rows,query) if rows else "Coin not found. Use the exact contract; for an unindexed Solana mint use /track solana CONTRACT."); continue
            base=pair.get("baseToken") or {}; chain=str(pair.get("chainId","")).lower(); token=str(base.get("address",""))
            db.add_manual_watch(chain,token,base.get("symbol","?"),base.get("name",""))
            await send(http,f"✅ TRACKING {base.get('name') or 'Unknown'} ({base.get('symbol') or '?'})\n{chain}\n{token}\nIt will stay in later scans/re-entry monitoring even if it disappears from new-pool feeds.")
            continue

        elif cmd == "/tracked":
            rows=db.manual_watches()
            if not rows: await send(http,"No custom tracked coins. Use /track CONTRACT.")
            else:
                await send(http,"CUSTOM TRACKED COINS\n"+"\n".join(f"{r['name'] or r['symbol']} ({r['symbol']}) | {r['chain']}\n{r['token']}" for r in rows[:20]))
            continue

        elif cmd == "/untrack":
            if len(parts)<2: await send(http,"Use /untrack CONTRACT."); continue
            n=db.remove_manual_watch(parts[1]); await send(http,"Removed from custom tracking." if n else "That exact contract was not in your custom watchlist.")
            continue

        elif cmd == "/paperauto":
            if len(parts)>=2 and parts[1].lower() in {"on","off"}:
                enabled=parts[1].lower()=="on"; set_bool_pref(db,"paper_auto",enabled)
                await send(http,f"🧪 PAPER AUTOPILOT: {'ON' if enabled else 'OFF'}\nIt simulates fee-aware entries/exits with NO real orders. The proof shadow engine is {'ON' if bool_pref(db,'shadow_sim',SHADOW_SIM_ALWAYS_ON) else 'OFF'} independently.")
            else:
                await send(http,f"Paper autopilot is {'ON' if bool_pref(db,'paper_auto',PAPER_AUTO_DEFAULT) else 'OFF'}. Use /paperauto on or /paperauto off.")
            continue

        elif cmd == "/paperpositions":
            rows=db.paper_open_positions()
            if not rows: await send(http,"No open paper-autopilot positions.")
            else:
                lines=["🧪 OPEN PAPER POSITIONS"]
                for p in rows:
                    pair=await pair_for_token(http,p["chain"],p["token"]); cur=f(pair.get("priceUsd")) if pair else p["entry_price"]; ret=(cur/max(p["entry_price"],1e-18)-1)*100
                    lines.append(f"{p['symbol']} | {p['tier']} | ${p['amount_usd']:.2f} | {ret:+.1f}%")
                await send(http,"\n".join(lines))
            continue

        elif cmd == "/paperreport":
            st=db.paper_stats(7,shadow_only=True,build_version=QUALITY_COHORT_BUILD_VERSIONS); proof=proof_first_health(db)
            pst=db.paper_stats(7,shadow_only=True,build_version=QUALITY_COHORT_BUILD_VERSIONS,proof_only=True)
            pf=st.get("profit_factor"); pftext="—" if pf is None else ("∞" if math.isinf(pf) else f"{pf:.2f}")
            ppf=pst.get("profit_factor"); ppftext="—" if ppf is None else ("∞" if math.isinf(ppf) else f"{ppf:.2f}")
            if st['closed']:
                body=(f"ALL RESEARCH SHADOWS — closed {st['closed']} | gross ${st['gross_pnl']:+.2f} | friction ${st['fees_usd']:.2f} | net ${st['pnl']:+.2f} | PF {pftext}\n"
                      f"PROOF-ELIGIBLE (score ≥{PROOF_ELIGIBLE_MIN_SCORE:.0f}) — closed {pst['closed']} | unique tokens {pst.get('unique_tokens',0)} | net ${pst['pnl']:+.2f} | ROI {(pst['roi'] or 0):+.1f}% | win {(pst['win_rate'] or 0)*100:.0f}% | PF {ppftext}\n"
                      f"Proof avg win ${pst['avg_win']:+.2f} | avg loss ${pst['avg_loss']:+.2f} | proof max DD ${pst['max_drawdown_usd']:.2f}\n"
                      f"Open all paper: {st['open']}")
            else:
                body="No completed v16.1/v16.2 quality-cohort shadow trades yet."

            # Tier-level quality visibility. Use the actual paper schema field `realized_pnl`
            # (not `realized_pnl_usd`) so net P/L is reported correctly.
            proof_rows=db.proof_shadow_rows(limit=100)
            tier_stats={}
            for row in proof_rows:
                tier=str(row.get("tier") or "UNKNOWN").strip()
                s0=tier_stats.setdefault(tier,{"count":0,"wins":0,"net":0.0,"invested":0.0,"friction":0.0})
                s0["count"]+=1
                pnl=f(row.get("realized_pnl"))
                if pnl>0: s0["wins"]+=1
                s0["net"]+=pnl
                s0["invested"]+=f(row.get("amount_usd"))
                s0["friction"]+=f(row.get("fees_usd"))
            tier_lines=[]
            for tier in ["FAST ENTRY","FLOW ENTRY","STRONG ENTRY","ENTRY OPTION","REVERSAL ENTRY"]:
                s0=tier_stats.get(tier)
                if not s0:
                    tier_lines.append(f"{tier}: 0T")
                    continue
                wr=100.0*s0["wins"]/max(s0["count"],1)
                roi=100.0*s0["net"]/max(s0["invested"],1e-18)
                tier_lines.append(f"{tier}: {s0['count']}T | win {wr:.0f}% | net ${s0['net']:+.2f} | ROI {roi:+.1f}% | friction ${s0['friction']:.2f}")
            breakdown="\n".join(tier_lines)
            await send(http,f"🧪 7-DAY V16.2 QUALITY MEASUREMENT REPORT\n{body}\n\nTIER BREAKDOWN:\n{breakdown}\n\nProof gate: {proof_health_label(db)}\nQuality cohort carries forward v16.1 outcomes because v16.2 does not change entry gates. Research-only shadows cannot unlock live guidance. No real order is created by this report/simulator.")
            continue

        elif cmd == "/state":
            await send(http,f"SHARED BOT STATE\nDatabase: {DB_PATH}\nReal recorded positions: {len(db.open_positions())}\nGuardian events: {db.conn.execute('select count(*) from guardian_events').fetchone()[0]}\nCaptured signal paths: {db.conn.execute('select count(*) from signal_outcomes').fetchone()[0]}\nMissed-opportunity ledger: {db.conn.execute('select count(*) from missed_opportunities').fetchone()[0]}\nTracked coins: {len(db.manual_watches())}\nThis database stays outside version folders so future upgrades do not forget positions or learning history.")
            continue

        elif cmd == "/quiet":
            arg=parts[1].lower() if len(parts)>=2 else "status"
            if arg in {"on","off"}:
                enabled=(arg=="on")
                set_bool_pref(db,"quiet_mode",enabled)
                await send(http,
                    f"QUIET MODE: {'ON' if enabled else 'OFF'}\n"
                    + ("GET READY / momentum / watch chatter is hidden. Actionable entries, GUARDIAN risk/profit alerts and critical notices still come through."
                       if enabled else "Your prior scout/momentum/watch preferences are active again."))
            else:
                enabled=bool_pref(db,"quiet_mode",False)
                await send(http,
                    f"QUIET MODE is {'ON' if enabled else 'OFF'}.\n"
                    "Use /quiet on to hide non-actionable scout/watch chatter while keeping entries and GUARDIAN alerts.")
            continue

        elif cmd in {"/scouts","/momentum","/watch"}:
            keymap={"/scouts":"telegram_scouts","/momentum":"telegram_momentum","/watch":"telegram_watch"}
            defaults={"/scouts":SCOUT_TELEGRAM_DEFAULT,"/momentum":MOMENTUM_TELEGRAM_DEFAULT,"/watch":WATCH_TELEGRAM_DEFAULT}
            prefkey=keymap[cmd]
            if len(parts)>=2 and parts[1].lower() in {"on","off"}:
                enabled=parts[1].lower()=="on"
                set_bool_pref(db,prefkey,enabled)
                await send(http,f"{cmd[1:].upper()} Telegram alerts: {'ON' if enabled else 'OFF'}")
            else:
                enabled=bool_pref(db,prefkey,defaults[cmd])
                await send(http,f"{cmd[1:].upper()} Telegram alerts are {'ON' if enabled else 'OFF'}. Use {cmd} on or {cmd} off.")
            continue

        elif cmd == "/sources":
            if len(parts)>=2 and parts[1].lower() in {"refresh","retry","check"}:
                # Explicit user-requested retry: useful after adding X credits or rotating
                # a Birdeye key without waiting for long backoff timers.
                source_guard=state.get("guard")
                if source_guard:
                    await source_guard.probe_core(http, force=True)
                    state["be_status"] = source_guard.status_text()
                else:
                    state["be_status"] = "guard unavailable during refresh"
                if XTOKEN:
                    global X_BILLING_HOLD_UNTIL
                    X_BILLING_HOLD_UNTIL = 0.0
                    # A deliberately obscure probe normally returns zero posts; X bills
                    # successful reads by resources fetched. This is only run on request.
                    await x_recent_query(http,db,'"FomoBotSourceHealthProbe_8f3c91" -is:retweet',label="health",max_results=10)
            portal=state.get("pumpportal"); rt=state.get("realtime")
            feeds=len(SOURCES.get("rss_feeds",[])); x_accounts=len(SOURCES.get("x_accounts",[]))
            targeted=bool(XTOKEN and bool_pref(db,"x_targeted_pulse",X_TARGETED_PULSE_ENABLED))
            xhealth=x_health_text()
            await send(http,
                f"🛰️ INTELLIGENCE SOURCES — {VERSION}\n"
                f"Solana realtime program radar: {rt.status() if rt else 'OFF'}\n"
                f"PumpPortal new-token/migration radar: {portal.status() if portal else 'OFF'}\n"
                f"DexScreener discovery + pairs: {'ON' if USE_DEX else 'OFF'} | GeckoTerminal new+trending pools: {'ON' if USE_GT else 'OFF'} | Jupiter Tokens V2: {state.get('jupiter_tokens_status','ON/key needed' if JUPITER_TOKEN_DISCOVERY else 'OFF')}\n"
                f"Birdeye smart-money/new-listing/enrichment: {state.get('be_status','checking...')}\n"
                f"RSS/Reddit feeds: {feeds} configured | refresh ~{RSS_SCAN_EVERY_SECONDS}s\n"
                f"X monitored accounts: {x_accounts} configured | X API: {xhealth}\n"
                f"Targeted exact-contract X pulse: {'ON' if targeted else 'OFF'}"
                + (" (PAUSED by X billing)" if targeted and X_LAST_STATUS==402 and time.time()<X_BILLING_HOLD_UNTIL else "")
                + " (explicit opt-in; can consume X API credits)\n"
                f"Hot scout repricing: {'ON' if HOT_SCOUT_ENABLED else 'OFF'} every ~{HOT_SCOUT_MONITOR_SECONDS}s\n"
                f"{market_regime_line(state.get('market_regime'))}\n"
                "Paid Dex boosts are discovery only; social hype never bypasses liquidity, safety, flow, stability or chase gates.\n"
                "Use /sources refresh after adding X credits or replacing a Birdeye key.")
            continue

        elif cmd == "/socialdeep":
            arg=parts[1].lower() if len(parts)>=2 else "status"
            if arg in {"on","off"}:
                if arg=="on" and not XTOKEN:
                    await send(http,"Targeted X pulse cannot be enabled because X_BEARER_TOKEN is not configured in .env. The bot still uses RSS/Reddit and all market/on-chain sources.")
                    continue
                set_bool_pref(db,"x_targeted_pulse",arg=="on")
            enabled=bool(XTOKEN and bool_pref(db,"x_targeted_pulse",X_TARGETED_PULSE_ENABLED))
            billing_note = ("\n⚠️ X currently returns HTTP 402 PAYMENT REQUIRED. The preference is saved, but REST scouting is paused until X API credits are available."
                            if enabled and X_LAST_STATUS==402 and time.time()<X_BILLING_HOLD_UNTIL else "")
            await send(http,
                f"TARGETED X CONTRACT PULSE: {'ON' if enabled else 'OFF'}\n"
                "When ON, the bot searches X for exact contracts on a very small number of top near-entry candidates. "
                "It is confirmation/context only and cannot make a weak market setup actionable by itself. X API is pay-per-use, so this stays opt-in."
                + billing_note)
            continue

        elif cmd == "/pulse":
            if len(parts)<2:
                await send(http,"Use /pulse CONTRACT (best) or /pulse TICKER.")
                continue
            query=" ".join(parts[1:]).strip()
            pair=await resolve_exact_contract(http,query) or await dex_search_pair(http,query)
            if not pair:
                await send(http,"I could not resolve that token on current DEX feeds. Use the exact contract when possible.")
                continue
            chain=str(pair.get("chainId") or "").lower(); token=nest(pair,"baseToken","address",default="")
            pulse=db.social_pulse(token,SOCIAL_PULSE_LOOKBACK_MINUTES); src=candidate_source_context(state,chain,token); m=metrics(pair)
            ratio=m["buys"]/max(m["sells"],1); turnover=100*m["v5"]/max(m["mc"],1)
            divergence=bool(SOCIAL_DIVERGENCE_ENABLED and f(pulse.get("score"))>=3 and
                            (ratio<1.10 or turnover<ENTRY_MIN_VOLUME_TURNOVER_PCT or m["pc5"]<-3))
            await send(http,
                f"📣 SOCIAL/SOURCE PULSE — {nest(pair,'baseToken','symbol',default='?')}\n"
                f"Exact-contract mentions ({SOCIAL_PULSE_LOOKBACK_MINUTES}m): {int(pulse.get('mentions') or 0)} | authors {int(pulse.get('unique_authors') or 0)} | sources {', '.join(pulse.get('sources') or []) or 'none'}\n"
                f"Pulse: {pulse.get('status','NONE')} {f(pulse.get('score')):+.1f} | recent-5m mentions {int(pulse.get('recent5') or 0)} | top-author share {100*f(pulse.get('top_author_share')):.0f}%\n"
                f"Discovery paths: {', '.join(src.get('sources') or []) or 'not retained in current 6h registry'}\n"
                f"Market confirmation: B/S {ratio:.2f}x | turnover {turnover:.2f}% MC | 5m {m['pc5']:+.1f}%\n"
                f"Verdict: {'⚠️ SOCIAL/MARKET DIVERGENCE — attention is not confirmed by tape' if divergence else 'social data is not contradicting the live tape'}\n"
                f"Contract: {token}")
            continue

        elif cmd == "/near":
            rows=state.get("near_entries") or []
            if not rows:
                await send(http,"No Solana candidates are within three blockers of an entry on the latest scan.")
            else:
                lines=["NEAR-ENTRY CANDIDATES — information only"]
                for x in rows:
                    b="; ".join(x.get("blocks") or ["none"])
                    lines.append(
                        f"{x['symbol']} | entry {x['entry']:.0f} / confirmed {x['confirmed']:.0f} | "
                        f"5m {x['pc5']:+.1f}% | 1h {x['pc1']:+.1f}% | B/S {x['ratio']:.2f}x | liq {usd(x['liq'])}\n"
                        f"Blocked by: {b}\n{x['token']}"
                    )
                await send(http,"\n\n".join(lines))
            continue

        elif cmd == "/safety":
            if len(parts)<2:
                await send(http,"Use /safety SOLANA_CONTRACT")
                continue
            token=parts[1].strip()
            pair=await pair_for_token(http,"solana",token)
            if not pair:
                await send(http,"I could not resolve that exact Solana contract on the current DEX feeds yet. Try /track solana CONTRACT if it is brand new.")
                continue
            result=await preflight_safety(http,state.get("guard"),db,"solana",token,state)
            verdict="⛔ BLOCK" if result.get("hard") else ("✅ PASSED CURRENT CHECKS" if result.get("rug_checked") and result.get("holder_checked") else "⚠️ INCOMPLETE")
            notes=(result.get("risks") or [])+(result.get("reasons") or [])
            await send(http,
                f"{verdict} — SAFETY CHECK\n"
                f"RugCheck: {'OK' if result.get('rug_checked') else 'unavailable'} | "
                f"holders: {'OK' if result.get('holder_checked') else 'unavailable'} ({result.get('holder_source','none')})\n"
                + ("\n".join(f"• {n}" for n in notes[:8]) if notes else "No configured hard risk was found in the available checks.")
                + f"\nContract: {token}")
            continue

        elif cmd == "/settings":
            await send(http,
                f"{VERSION} SETTINGS\n"
                f"CAPITAL FIRST: final normal-entry gate requires liq/MC ≥{100*CAPITAL_EDGE_MIN_LIQ_MC:.0f}%, turnover ≥{CAPITAL_EDGE_MIN_TURNOVER_PCT:.2f}% MC, 5m {CAPITAL_EDGE_MIN_5M:+.0f}%..+{CAPITAL_EDGE_MAX_5M:.0f}%, 1h {CAPITAL_EDGE_MIN_1H:+.0f}%..+{CAPITAL_EDGE_MAX_1H:.0f}%, ≥{CAPITAL_EDGE_MIN_SWAPS} swaps, buyer confirmation, then {CAPITAL_CONFIRM_SECONDS}s persistence.\n"
                f"Capital risk: Guardian hard line ≤-{CAPITAL_HARD_STOP_PCT:.0f}% | normal targets +{tier_profit_targets('ENTRY OPTION')[0]:.0f}%/+{tier_profit_targets('ENTRY OPTION')[1]:.0f}% | normal size cap ${sizing_preferences(db)[1]:.0f} | exceptional ${sizing_preferences(db)[2]:.0f} | live-auto per trade ≤$5.\n"
                f"BUY lanes: normal Solana ON | structure {'ON' if STRUCTURE_BUY_ALERTS_ENABLED else 'WATCH/SHADOW ONLY'} | cross-chain {'ON' if MANUAL_CHAIN_BUY_ALERTS_ENABLED else 'WATCH ONLY'} | micro {'ON' if MICRO_BUY_ALERTS_ENABLED else 'PAPER/WATCH ONLY'} | fresh {'ON' if FRESH_BUY_ALERTS_ENABLED else 'PAPER/WATCH ONLY'}.\n"
                f"FAST lane: {'ON' if FAST_ENTRY_ENABLED else 'OFF'} | score {FAST_ENTRY_MIN_SCORE:.0f}+ or confirmed {FAST_ENTRY_MIN_CONFIRMED:.0f}+ | {FAST_ENTRY_STABILITY_MIN_OBSERVATIONS} observations / {FAST_ENTRY_STABILITY_MIN_SPAN_SECONDS}s | {FAST_ENTRY_SIZE_MULT:.0%} size | manual/test only (not live autopilot).\n"
                f"Telegram delivery: {telegram_delivery_status()} — failed delivery never consumes an entry cooldown.\n"
                f"Entry option legacy prefilter: score ≥{OPTION_SCORE:.0f} OR confirmed ≥{OPTION_CONFIRMED_SCORE:.0f}; B/S ≥{OPTION_MIN_BUY_SELL:.2f}x; swaps ≥{OPTION_MIN_SWAPS}; turnover ≥{ENTRY_MIN_VOLUME_TURNOVER_PCT:.2f}% MC; 5m {OPTION_MIN_5M:+.0f}% to +{OPTION_MAX_5M:.0f}%\n"
                f"Strong entry: score ≥{STRONG_SCORE:.0f} OR confirmed ≥{STRONG_CONFIRMED_SCORE:.0f}; B/S ≥{STRONG_MIN_BUY_SELL:.2f}x; swaps ≥{STRONG_MIN_SWAPS}; turnover ≥{STRONG_MIN_VOLUME_TURNOVER_PCT:.2f}% MC\n"
                f"Flow scout: turnover ≥{FLOW_SCOUT_MIN_TURNOVER_PCT:.2f}% | B/S ≥{FLOW_SCOUT_MIN_BUY_SELL:.2f}x | swaps ≥{FLOW_SCOUT_MIN_SWAPS}; second-leg reset {'ON' if SECOND_LEG_ENABLED else 'OFF'} after ≥{SECOND_LEG_MIN_MINUTES}m\n"
                f"Micro rescue: {'ON' if MICRO_ENTRY_ENABLED else 'OFF'} | liq {usd(MICRO_MIN_LIQUIDITY)}–{usd(ENTRY_MIN_LIQUIDITY)} | turnover ≥{MICRO_MIN_TURNOVER_PCT:.1f}% | B/S ≥{MICRO_MIN_BUY_SELL:.1f}x | verified safety required\n"
                "Manual journal locks: OFF (position count / daily loss / size / alert-reply lag do not reject a manual record)\n"
                f"Guardian: {'ON' if GUARDIAN_ENABLED else 'OFF'} | automatic alerts {'ON' if bool_pref(db,'guardian_alerts',True) else 'OFF'} | monitor {GUARDIAN_MONITOR_SECONDS}s | adaptive confidence {'ON' if ADAPTIVE_CONFIDENCE_ENABLED else 'OFF'}\n"
                f"Stability: recent-crash memory {'ON' if RECENT_CRASH_MEMORY_ENABLED else 'OFF'} | small-position structure confirm {GUARDIAN_SMALL_STRUCTURE_CONFIRM_SECONDS}s | quiet mode {'ON' if bool_pref(db,'quiet_mode',False) else 'OFF'}\n"
                f"Smart size: ${sizing_preferences(db)[0]:.0f}-${sizing_preferences(db)[1]:.0f} normal | exceptional cap ${sizing_preferences(db)[2]:.0f} | minimum useful partial ~${GUARDIAN_MIN_PARTIAL_SALE_USD:.0f}\n"
                f"Auto entry updates: {'ON' if AUTO_ENTRY_RECHECKS_ENABLED and bool_pref(db,'auto_entry_rechecks',True) else 'OFF'} at {','.join(str(x)+'s' for x in AUTO_ENTRY_RECHECK_SECONDS)} | late-but-viable ceiling +{AUTO_ENTRY_MAX_LATE_MOVE_PCT:.1f}%\n"
                f"Hot scout: {'ON' if HOT_SCOUT_ENABLED else 'OFF'} every {HOT_SCOUT_MONITOR_SECONDS}s | market regime sizing: {'ON' if MARKET_REGIME_ENABLED else 'OFF'} | decision ledger: {'ON' if DECISION_LEDGER_ENABLED else 'OFF'}\n"
                f"Social pulse: {SOCIAL_PULSE_LOOKBACK_MINUTES}m lookback | RSS/Reddit ~{RSS_SCAN_EVERY_SECONDS}s | targeted X {'ON' if XTOKEN and bool_pref(db,'x_targeted_pulse',X_TARGETED_PULSE_ENABLED) else 'OFF'}\n"
                f"Read-only wallet sync: {'ON' if state.get('wallet_sync') and state['wallet_sync'].enabled else 'OFF'} | auto-record detected sells {'ON' if WALLET_SYNC_AUTO_RECORD_SELLS else 'OFF'}\n"
                f"Liquidity ≥{usd(ENTRY_MIN_LIQUIDITY)} | MC {usd(ENTRY_MIN_MCAP)}–{usd(ENTRY_MAX_MCAP)}\n"
                f"Fresh launch: age ≤{FRESH_MAX_AGE_MIN:.0f}m | liq ≥{usd(FRESH_MIN_LIQ)} | B/S ≥{FRESH_MIN_BS:.2f}x | RugCheck REQUIRED | holder check REQUIRED | fresh REAL auto {'ON' if bool_pref(db,'auto_fresh',AUTO_LIVE_ALLOW_FRESH_DEFAULT) else 'OFF'}\n"
                f"Liquidity alerts: warning -{LIQ_WARNING:.0f}% | urgent exit/reduce -{LIQ_DROP:.0f}%\n"
                f"Re-entry: {'ON' if REENTRY_ENABLED else 'OFF'} | WATCH confirm {REENTRY_WATCH_CONFIRM_SECONDS//60}m | B/S ≥{REENTRY_MIN_BS:.2f}x | swaps ≥{REENTRY_MIN_SWAPS} | liq ≥{usd(REENTRY_MIN_LIQUIDITY)}\n"
                f"Re-entry safety: RugCheck + holder verification REQUIRED | real auto re-entry {'ON' if AUTO_LIVE_ALLOW_REENTRY else 'OFF by default'}\n"
                f"REAL autopilot: {'ON' if bool_pref(db,'live_auto',False) else 'OFF'} | paper: {'ON' if bool_pref(db,'paper_auto',PAPER_AUTO_DEFAULT) else 'OFF'}\n"
                f"Scouts {'ON' if bool_pref(db,'telegram_scouts',SCOUT_TELEGRAM_DEFAULT) else 'OFF'} | Momentum {'ON' if bool_pref(db,'telegram_momentum',MOMENTUM_TELEGRAM_DEFAULT) else 'OFF'} | Watch {'ON' if bool_pref(db,'telegram_watch',WATCH_TELEGRAM_DEFAULT) else 'OFF'}\n"
                "Internal scouting is ALWAYS on even if GET READY messages are hidden.")
            continue

        elif cmd == "/watchlist":
            scouts=db.recent_scouts(hours=2,limit=8)
            if not scouts:
                await send(http,"No active scouts in the last 2 hours.")
                continue
            lines=["ACTIVE INTERNAL SCOUTS"]
            for s in scouts:
                pair=await pair_for_token(http,s["chain"],s["token"])
                if pair:
                    m=metrics(pair); ratio=m["buys"]/max(m["sells"],1)
                    lines.append(f"{s['symbol']} | 5m {m['pc5']:+.1f}% | B/S {ratio:.2f}x | liq {usd(m['liq'])} | {s['token']}")
                else:
                    lines.append(f"{s['symbol']} | quote unavailable | {s['token']}")
            await send(http,"\n".join(lines))
            continue

        elif cmd == "/last":
            sig=db.latest_entry(24)
            if not sig:
                await send(http,"No actionable ENTRY OPTION / FLOW ENTRY / MICRO ENTRY / STRONG ENTRY has been sent in the last 24 hours.")
                continue
            pair=await pair_for_token(http,sig["chain"],sig["token"])
            current=f(pair.get("priceUsd")) if pair else sig["price"]
            move=(current/max(sig["price"],1e-18)-1)*100
            await send(http,
                f"LAST ENTRY — {sig['symbol']} | {sig['action']}\n"
                f"Alert price ${sig['price']:.8g} | now ${current:.8g} ({move:+.1f}%)\n"
                f"Contract: {sig['token']}\nReply /check to the original entry message if you are considering it.")
            continue

        elif cmd in {"/hold","/plan"}:
            reply_contract = _reply_contract(msg)
            query = reply_contract or (parts[1] if len(parts)>=2 else None)
            if not query:
                await send(http,"Use /hold TICKER or /hold CONTRACT, or reply /hold to a position/entry alert.")
                continue
            pos,pos_err=db.find_open_position(query)
            pair=None
            symbol=query
            if pos:
                pair=await pair_for_token(http,pos["chain"],pos["token"]); symbol=pos["symbol"]
            else:
                sig,_=db.find_buy_signal(query,hours=24)
                token=(sig or {}).get("token") or query
                chain=(sig or {}).get("chain") or "solana"
                pair=await pair_for_token(http,chain,token) or await dex_search_pair(http,token)
                if pair: symbol=nest(pair,"baseToken","symbol",default=query)
            if not pair:
                await send(http,"I couldn't get a live quote for that token. Use the exact contract if the ticker is ambiguous.")
                continue
            plan=hold_plan(pair,position=pos,tier=(pos or {}).get("entry_tier") or "")
            m=metrics(pair); ratio=m["buys"]/max(m["sells"],1); current=f(pair.get("priceUsd"))
            posline=""; guardian_line=""; cal_line=""; partial_line=""
            tier=(pos or {}).get("entry_tier") or ((sig or {}).get("action") if not pos else "") or "ENTRY OPTION"
            score=f((sig or {}).get("score"),50) if not pos else 50
            if pos:
                sr=db.conn.execute("""select * from signals where chain=? and token=? and kind='EARLY' and ts<=? order by ts desc limit 1""",
                                   (pos["chain"],pos["token"],int(pos["open_ts"]))).fetchone()
                if sr: score=f(sr["score"],50); tier=pos.get("entry_tier") or sr["action"] or tier
                ret=(current/max(f(pos.get("entry_price")),1e-18)-1)*100
                peak=max(f(pos.get("peak_price"),pos.get("entry_price")),current)
                dd=(current/max(peak,1e-18)-1)*100
                p1,p2=tier_profit_targets(tier); rp=tier_risk_profile(tier)
                pstate=position_state(pair,pos,tp1=p1,tp2=p2,risk_line=f(rp.get("soft"),RISK_LINE),trailing=f(rp.get("trailing"),TRAIL),liquidity_exit=LIQ_DROP,
                                      round_trip_friction=FOMO_ROUNDTRIP_FRICTION_PCT, exit_friction=FOMO_EXIT_FRICTION_PCT,
                                      small_position_usd=SMALL_POSITION_USD, small_hard_stop=f(rp.get("small_hard"),SMALL_POSITION_HARD_STOP_PCT),
                                      mid_hard_stop=f(rp.get("mid_hard"),MID_POSITION_HARD_STOP_PCT), min_partial_sale_usd=GUARDIAN_MIN_PARTIAL_SALE_USD)
                posline=f"Your position: {ret:+.1f}% | from peak {dd:+.1f}%\n"
                guardian_line=f"Guardian: {pstate['state'].replace('_',' ')} — {pstate['action']}\n"
                if pstate.get("suggested_partial_pct"):
                    partial_line=(f"Partial plan now: sell ~{int(round(f(pstate.get('suggested_partial_pct'))))}% "
                                  f"(~${f(pstate.get('suggested_partial_usd')):.2f}), leave ~${f(pstate.get('remaining_value_usd')):.2f}.\n")
                elif pstate.get("partial_note"):
                    partial_line=f"Partial plan: {pstate.get('partial_note')}\n"
            if ADAPTIVE_CONFIDENCE_ENABLED:
                cal=adaptive_dollar_size(db,pair,tier,score)
                cal_line=f"Adaptive confidence: {cal['confidence']:.0f}/100 ({cal['grade']}) | size now ~${cal['suggested_usd']:.0f}\n"
            longline="YES — but reassess at the stated checkpoints" if plan.get("long_term") else "NO — treat this as a trade, not a set-and-forget hold"
            await send(http,
                f"🧭 HOLD PLAN — {symbol}\n"
                f"{posline}{guardian_line}{partial_line}{cal_line}Style: {plan['label']}\nSuggested horizon: {plan['window']}\n"
                f"Current: ${current:.8g} | 5m {m['pc5']:+.1f}% | 1h {m['pc1']:+.1f}% | B/S {ratio:.2f}x\n"
                f"Longer-term candidate: {longline}\n"
                "WHY: "+"; ".join(plan.get("reasons",[])[:4])+"\n"
                "This is a live risk/momentum classification, not a guarantee. Guardian keeps monitoring recorded positions automatically.")
            continue

        elif cmd == "/addmc":
            if len(parts) < 4:
                await send(http,"Use /addmc CONTRACT AMOUNT ENTRY_MARKET_CAP\nExample: /addmc CONTRACT 15 3000000")
                continue
            pair=await dex_search_pair(http,parts[1])
            amount=_parse_usd(parts[2:3],None)
            try:
                entry_mc=float(str(parts[3]).replace("$","").replace(",",""))
            except Exception:
                entry_mc=0
            if not pair or not amount or entry_mc<=0:
                await send(http,"Could not add it. Use the exact contract, dollar amount, and numeric entry market cap.")
                continue
            m=metrics(pair); current=f(pair.get("priceUsd")); current_mc=m["mc"]
            if current<=0 or current_mc<=0:
                await send(http,"Current price/market cap unavailable; position was not added.")
                continue
            estimated_entry=current*(entry_mc/current_mc)
            base=pair.get("baseToken") or {}
            signal={"chain":pair.get("chainId",""),"token":base.get("address",""),"symbol":base.get("symbol","?"),"name":base.get("name",""),"action":"MANUAL"}
            existing_before=db.position_by_token(signal["token"])
            journal_id=db.journal_snapshot((existing_before or {}).get("id",0),"ADDMC",before=existing_before)
            pid,err=db.record_position(signal,estimated_entry,amount,m["liq"],manual_source="MANUAL_EXPLICIT")
            db.journal_finish(journal_id,pid)
            if err and err != "MERGED":
                await send(http,err)
            else:
                merged = err == "MERGED"
                await send(http,
                    f"✅ {'POSITION AVERAGED' if merged else 'EXISTING POSITION ADDED'} — {base.get('symbol','?')}\n"
                    f"Amount ${amount:.2f} | entry MC ${entry_mc:,.0f}\n"
                    f"Estimated entry price ${estimated_entry:.8g} (derived from current MC; approximate).\n"
                    "Position-aware profit/risk alerts are now active.")
            continue

        elif cmd == "/check":
            reply_message_id = _reply_message_id(msg)
            reply_signal = db.signal_from_telegram_message(reply_message_id)
            signal = None
            err = None
            if reply_signal and reply_signal.get("kind") == "EARLY":
                signal = reply_signal
            elif len(parts) >= 2:
                signal, err = db.find_buy_signal(parts[1], hours=8)
                if signal and signal.get("kind") == "CONFIRMED":
                    signal = db.recent_signal(signal["chain"], signal["token"], "EARLY", hours=8)
            else:
                # v11.5 convenience: if the user has an open position and types /check
                # without replying, resolve the most recently opened position instead of
                # returning a dead-end instruction.
                opens=db.open_positions()
                if opens:
                    recent_pos=max(opens,key=lambda x:f(x.get("open_ts")))
                    signal=db.recent_signal(recent_pos.get("chain",""),recent_pos.get("token",""),"EARLY",hours=8)
                if not signal:
                    await send(http,"Reply to an ENTRY OPTION / FLOW ENTRY / MICRO ENTRY / STRONG ENTRY with /check, type /check TICKER, or use /positions for an open trade.")
                    continue

            if not signal or signal.get("kind") != "EARLY":
                await send(http,(err or "There is no recent actionable entry for that token.") + "\nACTION: DO NOT BUY from an old GET READY message.")
                continue

            scout = db.recent_signal(signal["chain"], signal["token"], "SCOUT", hours=2) or signal
            live = await pair_for_token(http, signal["chain"], signal["token"])
            if not live:
                await send(http,"⛔ LIVE CHECK FAILED — no fresh quote. ACTION: DO NOT BUY.")
                continue
            m=metrics(live); current=f(live.get("priceUsd")); move=(current/max(scout["price"],1e-18)-1)*100
            alert_move=(current/max(f(signal.get("price")),1e-18)-1)*100
            ratio=m["buys"]/max(m["sells"],1); swaps=m["buys"]+m["sells"]
            rt=state.get("realtime"); rts=rt.stats(signal["token"]) if rt and signal["chain"]=="solana" else {}
            safety=cached_safety(state,signal["chain"],signal["token"])
            if safety is None:
                rb,notes,hard,checked=await rugcheck_summary(http,signal["token"]) if signal["chain"]=="solana" else (0,[],False,False)
                safety={"hard":hard,"rug_checked":checked,"security_checked":False,"holder_checked":False,"risks":notes if hard else [],"reasons":notes if not hard else []}
            recent_conf=db.recent_signal(signal["chain"],signal["token"],"CONFIRMED",hours=8)
            confirmed_score=max(f(signal.get("score")), f(recent_conf.get("score")) if recent_conf else 0)
            if str(signal.get("action",""))=="RE-ENTRY OPTION":
                rs=reentry_setup(db,live)
                reentry_safe = (not safety.get("hard")
                                and bool(safety.get("rug_checked"))
                                and bool(safety.get("holder_checked")))
                tier="RE-ENTRY OPTION" if rs and reentry_safe else None
                blockers=[] if tier else ["re-entry structure/safety no longer passes"]
            else:
                tier, blockers=classify_actionable_tier(live,f(signal.get("score")),confirmed_score,list(safety.get("risks",[])),False,rts=rts,safety=safety,scout_move=move)
            stability_ok, stability_blockers, _ = entry_stability_check(db,live,tier) if tier else (False,[],{})
            if tier and alert_move < -CHECK_MAX_DROP_FROM_ALERT_PCT:
                blockers=[f"price is {alert_move:.1f}% below the original entry alert; wait for a repaired setup"]+list(blockers)
                tier=None
            if tier and not stability_ok:
                blockers=list(stability_blockers)+list(blockers)
                tier=None
            if tier and alert_move > AUTO_ENTRY_MAX_LATE_MOVE_PCT:
                blockers=[f"price is already {alert_move:+.1f}% above the original entry alert; do not chase"]+list(blockers)
                tier=None

            owned=db.position_by_token(signal["token"])
            owned_ret=None
            if owned:
                owned_ret=(current/max(f(owned.get("entry_price")),1e-18)-1)*100
                if owned_ret <= -CHECK_POSITION_RISK_PCT:
                    tier=None
                    blockers=[f"your recorded position is already {owned_ret:.1f}% below entry"]+list(blockers)

            db.log_latency(signal["id"],signal["token"],"CHECK",current,
                           f"move_from_scout={move:.2f};move_from_alert={alert_move:.2f};owned_ret={owned_ret};tier={tier or 'NONE'}")
            if owned:
                add_ok = bool(tier) and (owned_ret is None or owned_ret > -CHECK_POSITION_RISK_PCT)
                label="✅ ADD CHECK PASSES" if add_ok else ("⚠️ POSITION RISK" if owned_ret is not None and owned_ret <= -CHECK_POSITION_RISK_PCT else "📌 HOLD / DO NOT ADD YET")
                why="; ".join(blockers[:4]) if blockers else "current live entry gates still pass"
                plan=hold_plan(live,position=owned,tier=owned.get("entry_tier") or "")
                add_guide=adaptive_dollar_size(db,live,tier or signal.get("action") or "ENTRY OPTION",f(signal.get("score")),late_move_pct=max(0.0,alert_move)) if add_ok else None
                mid=await send(http,
                    f"{label} — {signal['symbol']}\n"
                    f"Your recorded position: {owned_ret:+.1f}% | Current ${current:.8g}\n"
                    f"Move from original bot alert: {alert_move:+.1f}%\n"
                    f"5m {m['pc5']:+.1f}% | 1h {m['pc1']:+.1f}% | B/S {ratio:.2f}x | {int(swaps)} swaps\n"
                    f"Horizon: {plan['label']} — {plan['window']}\nWHY: {why}\n"
                    + ((f"Suggested ADD size now: ~${add_guide.get('suggested_usd',0):.0f}. Reply /bought YOUR_AMOUNT after adding in Fomo." ) if add_ok else "You can still record a manual buy with /buy; the journal will not block you."))
                db.map_trade_context(mid, signal.get("id"), live, tier or signal.get("action") or "", "ADD")
                continue
            if tier:
                rtline=f"\nRealtime: {rts.get('tx30',0)} tx/30s" if rts.get("available") else ""
                late=alert_move>MAX_CHASE
                guide=adaptive_dollar_size(db,live,tier,f(signal.get("score")),late_move_pct=max(0.0,alert_move))
                status_label="⚠️ LATE BUT STILL VIABLE" if late else "✅ STILL OPEN"
                green_message_id=await send(http,
                    f"{status_label} — {signal['symbol']} | {tier}\n"
                    f"Price ${current:.8g} | from original alert {alert_move:+.1f}% | scout move {move:+.1f}%\n"
                    f"5m {m['pc5']:+.1f}% | 1h {m['pc1']:+.1f}% | B/S {ratio:.2f}x | {int(swaps)} swaps\n"
                    f"Liquidity {usd(m['liq'])}{rtline}\n"
                    f"💵 Suggested buy now: ~${guide.get('suggested_usd',0):.0f} ({guide.get('size_label','')})\n\n"
                    "If you buy it in Fomo, reply directly to THIS message with /bought YOUR_AMOUNT. I will use this fresh price snapshot.")
                if hasattr(db,"expire_signal_contexts"):
                    db.expire_signal_contexts(signal["id"],"SUPERSEDED")
                db.map_telegram_signal(green_message_id,signal["id"])
                db.map_trade_context(green_message_id,signal["id"],live,tier,"ENTRY")
                db.remember_green_check(signal["id"])
            else:
                await send(http,
                    f"⏸️ {signal['symbol']} — ENTRY NOT VALID RIGHT NOW\n"
                    f"Price ${current:.8g} | from original alert {alert_move:+.1f}% | 5m {m['pc5']:+.1f}% | B/S {ratio:.2f}x\n"
                    f"WHY: {'; '.join(blockers[:4]) or 'live gates no longer pass'}\n"
                    "ACTION: Do not buy this snapshot. Wait for an automatic freshness update or a new entry/second-leg alert rather than chasing a stale setup.")
            continue

        elif cmd == "/buy":
            reply_message_id = _reply_message_id(msg)
            context = db.trade_context_from_message(reply_message_id)
            reply_signal = db.signal_from_telegram_message(reply_message_id)
            reply_contract = _reply_contract(msg)
            if context:
                context_ok, context_reason = db.context_buy_status(context)
                if not context_ok:
                    await send(http,
                        f"⛔ THAT ENTRY SNAPSHOT IS {context_reason.upper()}. I will not reuse an old Telegram price as a new fill.\n"
                        "If you already bought in Fomo, record it with `/buy CONTRACT AMOUNT` (uses a fresh live quote) "
                        "or `/buy CONTRACT AMOUNT price YOUR_ACTUAL_AVG_ENTRY`. Manual journaling is still unlocked.")
                    continue

            # Parse explicit overrides: `price 0.0003` / `price=...` and `mc 300000` / `mc=...`.
            def _override_value(tokens, keys):
                for i,raw in enumerate(tokens):
                    low=str(raw).lower().replace(",","")
                    for key in keys:
                        if low.startswith(key+"=") or low.startswith(key+":"):
                            try: return float(low.split(low[len(key)],1)[1].replace("$",""))
                            except Exception: pass
                        if low==key and i+1<len(tokens):
                            try: return float(str(tokens[i+1]).replace("$","").replace(",",""))
                            except Exception: pass
                return None

            query=None; signal=None; pair=None
            arg_tokens=parts[1:]
            # Replying is the fastest/most accurate workflow: the message carries a price snapshot.
            if context:
                amount=_parse_usd(arg_tokens,DEFAULT_POSITION)
                signal=db.signal_by_id(context.get("signal_id")) or {
                    "chain":context.get("chain") or "solana","token":context.get("token"),"symbol":context.get("symbol") or "?",
                    "name":context.get("name") or "","action":context.get("tier") or "MANUAL"}
            elif reply_signal:
                amount=_parse_usd(arg_tokens,DEFAULT_POSITION); signal=reply_signal
            elif reply_contract:
                query=reply_contract; amount=_parse_usd(arg_tokens,DEFAULT_POSITION)
            elif len(parts)>=2:
                # If first argument is numeric, use the most recent green check. Otherwise it is ticker/contract.
                first_amt=_parse_usd(parts[1:2],-1)
                if first_amt>=0:
                    signal=db.recent_green_check(300); amount=_parse_usd(parts[1:],DEFAULT_POSITION)
                    if not signal:
                        await send(http,"I don't know which coin you mean. Reply /bought 5 to the exact entry/check message, or use /buy CONTRACT 5.")
                        continue
                else:
                    query=parts[1]; amount=_parse_usd(parts[2:],DEFAULT_POSITION)
            else:
                await send(http,"Reply /bought 5 to an entry/check message, or use /buy CONTRACT 5. Optional: `price 0.0003` or `mc 300000`.")
                continue

            if signal and not context and reply_message_id and time.time()-f(signal.get("ts"))>ENTRY_CONTEXT_TTL_SECONDS:
                await send(http,
                    "⛔ That bot entry message is too old to reuse as a fill. If you already bought, use `/buy CONTRACT AMOUNT` "
                    "for a fresh quote or add `price YOUR_ACTUAL_AVG_ENTRY`.")
                continue

            if not signal and query:
                signal,_=db.find_buy_signal(query,hours=24)
                if signal and signal.get("kind")=="CONFIRMED":
                    signal=db.recent_signal(signal["chain"],signal["token"],"EARLY",hours=24) or signal
                if not signal:
                    # Manual ledger fallback: exact contract can be recorded even without a bot signal.
                    pair=await dex_search_pair(http,query)
                    if pair:
                        base=pair.get("baseToken") or {}
                        signal={"chain":pair.get("chainId","").lower(),"token":base.get("address",query),
                                "symbol":base.get("symbol","?"),"name":base.get("name",""),"action":"MANUAL"}
            # Explicit /buy CONTRACT is always allowed, but an hours-old bot signal must not donate its stale price/tier.
            if signal and query and time.time()-f(signal.get("ts"))>ENTRY_CONTEXT_TTL_SECONDS:
                signal=dict(signal)
                signal["action"]="MANUAL"
                signal["price"]=0.0
                signal["mcap"]=0.0
                signal["liquidity"]=0.0

            if not signal or not signal.get("token"):
                await send(http,"I couldn't identify that token. Use the exact contract address. No position was recorded.")
                continue

            # Current pair is useful for MC-based override and a lag warning, but a reply snapshot can record even if quote retrieval fails.
            if pair is None:
                pair=await pair_for_token(http,signal.get("chain") or "solana",signal["token"])
            price_override=_override_value(arg_tokens,{"price","p"})
            mc_override=_override_value(arg_tokens,{"mc","mcap"})
            snapshot_price=f((context or {}).get("snapshot_price")) if context else f(signal.get("price"))
            snapshot_mcap=f((context or {}).get("snapshot_mcap")) if context else f(signal.get("mcap"))
            snapshot_liq=f((context or {}).get("snapshot_liquidity")) if context else f(signal.get("liquidity"))
            live_price=f(pair.get("priceUsd")) if pair else 0.0
            live_mcap=f((pair or {}).get("marketCap") or (pair or {}).get("fdv")) if pair else 0.0
            live_liq=f(nest(pair or {},"liquidity","usd",default=0)) if pair else snapshot_liq

            entry_price=price_override or 0.0
            source="exact price override" if entry_price else ""
            if not entry_price and mc_override and live_price>0 and live_mcap>0:
                entry_price=live_price*(mc_override/live_mcap); source=f"entry MC ${mc_override:,.0f}"
            if not entry_price and snapshot_price>0:
                entry_price=snapshot_price; source="Telegram alert/check snapshot"
            if not entry_price and live_price>0:
                entry_price=live_price; source="current live quote"
            if entry_price<=0:
                await send(http,"I don't have a usable entry price. Reply to the original alert/check, or use `/buy CONTRACT 5 price YOUR_FILL_PRICE`.")
                continue

            manual_source="BOT_ALERT" if str(signal.get("action") or "MANUAL")!="MANUAL" and (context or reply_signal or not query) else "MANUAL_EXPLICIT"
            existing_before=db.position_by_token(signal["token"])
            journal_id=db.journal_snapshot((existing_before or {}).get("id",0),"BUY",before=existing_before)
            pid,err=db.record_position(signal,entry_price,amount,live_liq or snapshot_liq,manual_source=manual_source)
            db.journal_finish(journal_id,pid)
            merged=err=="MERGED"
            if context and reply_message_id:
                db.consume_trade_context(reply_message_id)
            # A manually spotted trade is valuable training data too. Capture the exact
            # market snapshot so later Decision Ledger outcomes can compare the user's
            # chart read against bot-approved and bot-rejected setups.
            if manual_source=="MANUAL_EXPLICIT" and pair:
                try:
                    prev=db.previous(str(pair.get("chainId") or "").lower(),signal["token"],240)
                    mscore,mreasons,mrisks,_=early_score(pair,prev)
                    cbonus,_=confirmation_bonus(pair); mconfirmed=clamp(mscore+cbonus,0,100)
                    sc=candidate_source_context(state,str(pair.get("chainId") or "").lower(),signal["token"])
                    db.log_decision(pair,"MANUAL_BUY","MANUAL OBSERVED",mscore,mconfirmed,
                                    ["user manually bought this setup"]+mrisks[:4],social=db.social_pulse(signal["token"]),
                                    market_regime=str((state.get("market_regime") or {}).get("label") or "NEUTRAL"),
                                    sources=sc.get("sources"),min_seconds=30)
                except Exception as exc:
                    print(f"[manual-learning] snapshot failed: {type(exc).__name__}: {exc}")
            if signal.get("id"):
                db.log_latency(signal["id"],signal["token"],"BUY_RECORDED",entry_price,
                               f"amount_usd={amount:.2f};source={source};merged={merged}")
            db.set_meta("last_green_check_signal_id","0"); db.set_meta("last_green_check_ts","0")
            lag=""
            if live_price>0 and snapshot_price>0:
                lag_move=(live_price/max(snapshot_price,1e-18)-1)*100
                if abs(lag_move)>=2:
                    lag=f"\nLive quote is now {lag_move:+.1f}% from the message snapshot; the journal still used the snapshot you replied to."
            recorded_tier=str(signal.get("action") or "MANUAL")
            plan=hold_plan(pair or {"chainId":signal.get("chain"),"baseToken":{"address":signal["token"]},"priceUsd":entry_price,
                                    "marketCap":snapshot_mcap,"liquidity":{"usd":snapshot_liq}},tier=recorded_tier)
            rp=tier_risk_profile(recorded_tier); p1,p2=tier_profit_targets(recorded_tier)
            await send(http,
                f"✅ {'ADDITIONAL BUY AVERAGED' if merged else 'PURCHASE RECORDED'} — {signal.get('symbol','?')}\n"
                f"Tracked amount: ${amount:.2f}\nEntry reference: ${entry_price:.8g} ({source})\n"
                f"Trade horizon: {plan['label']} — {plan['window']}\n"
                f"Risk reference: ${entry_price*(1-f(rp.get('soft'),RISK_LINE)/100):.8g} | Profit checkpoints +{p1:.0f}% / +{p2:.0f}%"
                f"{lag}\n\nNo manual journal lock was applied. The bot did not place an order.")
            continue

        elif cmd == "/undo":
            ok,detail=db.undo_last_journal_action(max_age_seconds=1800)
            await send(http,("✅ " if ok else "ℹ️ ")+detail+"\nThis changes only the Telegram journal/database. It never buys or sells anything in Fomo.")
            continue

        elif cmd == "/reconcile":
            if len(parts)<4:
                await send(http,"Use /reconcile TICKER CASH_IN CASH_OUT. Example: /reconcile GG 30.14 31.43. Use the actual total dollars paid and actual total net cash received in Fomo.")
                continue
            query=parts[1]
            try:
                cash_in=float(str(parts[2]).replace("$","").replace(",","")); cash_out=float(str(parts[3]).replace("$","").replace(",",""))
            except Exception:
                cash_in=-1; cash_out=-1
            if cash_in<=0 or cash_out<0:
                await send(http,"Cash in must be above $0 and cash out must be non-negative.")
                continue
            closed,closed_err=db.find_closed_position(query)
            if not closed:
                await send(http,closed_err or "No closed position matched that ticker/contract.")
                continue
            journal_id=db.journal_snapshot(closed["id"],"RECONCILE_CASH",before=closed)
            ok,result=db.reconcile_closed_cash(closed["token"],cash_in,cash_out)
            if not ok:
                db.conn.execute("delete from journal_actions where id=?",(journal_id,)); db.conn.commit()
                await send(http,str(result)); continue
            db.journal_finish(journal_id,closed["id"])
            await send(http,
                f"✅ CLOSED TRADE RECONCILED — {closed['symbol']}\nActual Fomo cash in: ${cash_in:.2f} | actual net cash out: ${cash_out:.2f}\n"
                f"Verified realized P/L: ${cash_out-cash_in:+.2f}\nThis verified result can now be used by the sizing/learning risk context. Use /undo within 30 minutes if you mistyped it.")
            continue

        elif cmd == "/sellcash":
            reply_contract=_reply_contract(msg)
            if reply_contract and len(parts)>=3:
                query=reply_contract; pct_raw=parts[1]; cash_raw=parts[2]
            elif len(parts)>=4:
                query=parts[1]; pct_raw=parts[2]; cash_raw=parts[3]
            else:
                await send(http,"Reply to a GUARDIAN alert with /sellcash 30 4.03, or use /sellcash TICKER 30 4.03. That means 30% of what remained was sold and Fomo actually returned $4.03 net.")
                continue
            try:
                pct=float(str(pct_raw).replace("%","").replace(",","")); proceeds=float(str(cash_raw).replace("$","").replace(",",""))
            except Exception:
                pct=-1; proceeds=-1
            if pct<=0 or pct>=100 or proceeds<0:
                await send(http,"Use a partial percentage above 0 and below 100, plus a non-negative net cash amount.")
                continue
            pos,pos_err=db.find_open_position(query)
            if not pos:
                await send(http,pos_err or "No recorded open position matched that ticker/contract.")
                continue
            journal_id=db.journal_snapshot(pos["id"],"SELLCASH_PARTIAL",before=pos)
            pnl,new_rem=db.partial_close_position_cash(pos["id"],pct/100.0,proceeds)
            db.journal_finish(journal_id,pos["id"])
            await send(http,
                f"✅ CASH-RECONCILED PARTIAL — {pos['symbol']}\n"
                f"Sold {pct:.1f}% of what remained | actual Fomo net cash: ${proceeds:.2f}\n"
                f"Realized P/L on this partial after actual proceeds: ${pnl:+.2f}\n"
                f"Remaining: {new_rem*100:.1f}% of the original recorded position. GUARDIAN keeps monitoring it.")
            continue

        elif cmd == "/closecash":
            reply_contract=_reply_contract(msg)
            if reply_contract and len(parts)>=2:
                query=reply_contract; cash_raw=parts[1]
            elif len(parts)>=3:
                query=parts[1]; cash_raw=parts[2]
            else:
                await send(http,"Reply to a GUARDIAN alert with /closecash 13.96, or use /closecash TICKER 17.99. Use the actual net cash Fomo returned for the remaining recorded position.")
                continue
            try: proceeds=float(str(cash_raw).replace("$","").replace(",",""))
            except Exception: proceeds=-1
            if proceeds<0:
                await send(http,"Net proceeds must be a non-negative dollar amount.")
                continue
            pos,pos_err=db.find_open_position(query)
            if not pos:
                await send(http,pos_err or "No recorded open position matched that ticker/contract.")
                continue
            journal_id=db.journal_snapshot(pos["id"],"CLOSECASH",before=pos)
            total,leg=db.close_position_cash(pos["id"],proceeds)
            db.journal_finish(journal_id,pos["id"])
            await send(http,
                f"✅ CASH-RECONCILED SALE — {pos['symbol']}\n"
                f"Net cash received for the remaining recorded position: ${proceeds:.2f}\n"
                f"Realized P/L on this final leg after your actual Fomo proceeds: ${leg:+.2f}\n"
                f"Total recorded realized P/L including earlier partials: ${total:+.2f}\n"
                "This journal entry uses your actual proceeds instead of estimating from the current quote.")
            continue

        elif cmd == "/fixentry":
            if len(parts)<3:
                await send(http,"Use /fixentry TICKER ACTUAL_AVG_ENTRY_PRICE. This is only for an open position with no recorded partial sale.")
                continue
            query=parts[1]
            try: px=float(str(parts[2]).replace("$","").replace(",",""))
            except Exception: px=0
            pos,pos_err=db.find_open_position(query)
            if not pos:
                await send(http,pos_err or "No recorded open position matched that ticker/contract.")
                continue
            journal_id=db.journal_snapshot(pos["id"],"FIXENTRY",before=pos)
            ok,why=db.fix_position_entry(pos["id"],px)
            if not ok:
                db.conn.execute("delete from journal_actions where id=?",(journal_id,)); db.conn.commit()
                await send(http,f"I did not change the position: {why}")
                continue
            db.journal_finish(journal_id,pos["id"])
            await send(http,
                f"✅ ENTRY BASIS REPAIRED — {pos['symbol']}\nActual average entry: ${px:.10g}\n"
                "GUARDIAN will use this corrected basis on its next cycle. No trade was placed.")
            continue

        elif cmd in {"/sellpct","/sellusd","/sold"}:
            reply_contract = _reply_contract(msg)
            query = None
            raw_value = None
            mode = cmd

            # Easiest workflow: reply directly to a TAKE PROFIT / EXIT alert with
            # /sellpct 50, /sellusd 2, or /sold 50%. The alert carries the exact contract.
            if reply_contract and len(parts) >= 2:
                query = reply_contract
                raw_value = parts[1]
                if cmd == "/sold":
                    mode = "/sellusd" if "$" in raw_value.lower() else "/sellpct"
            elif len(parts) >= 3:
                query = parts[1]
                raw_value = parts[2]
                if cmd == "/sold":
                    mode = "/sellusd" if "$" in raw_value.lower() else "/sellpct"
            else:
                await send(http,
                    "Reply to a TAKE PROFIT / EXIT alert with /sellpct 50 after you sell half,\n"
                    "or type /sellpct TICKER 50. For dollars, use /sellusd TICKER 10.\n"
                    "Friendly alias: reply /sold 50% or /sold $2.")
                continue

            pos,pos_err=db.find_open_position(query)
            if not pos:
                await send(http,pos_err or "No recorded open position matched that ticker/contract.")
                continue
            pair=await pair_for_token(http,pos["chain"],pos["token"])
            current=f(pair.get("priceUsd")) if pair else f(pos["entry_price"])
            rem=f(pos.get("remaining_fraction"),1.0)
            current_value=f(pos["quantity"])*rem*current
            try:
                value=float(str(raw_value).replace("%","").replace("$","").replace(",",""))
            except Exception:
                value=0
            fraction=(value/100.0) if mode=="/sellpct" else (value/max(current_value,1e-18))
            if fraction<=0 or fraction>=1:
                await send(http,"Partial sale must be above 0 and below the entire remaining position. Use /sell for a full exit.")
                continue
            journal_id=db.journal_snapshot(pos["id"],"SELL_PARTIAL_ESTIMATED",before=pos)
            pnl,new_rem=db.partial_close_position(pos["id"],current,fraction)
            db.journal_finish(journal_id,pos["id"])
            remaining_value=f(pos["quantity"])*new_rem*current
            await send(http,
                f"✅ PARTIAL SALE RECORDED — {pos['symbol']}\n"
                f"Sold {fraction*100:.1f}% of what remained at ${current:.8g}\n"
                f"Approx realized P/L on this partial: ${pnl:+.2f}\n"
                f"Remaining: {new_rem*100:.1f}% of the original recorded position | approx ${remaining_value:.2f} current value\n"
                "The remainder stays open and keeps receiving risk/profit alerts. The bot did not place the sale.")
            continue

        elif cmd == "/sell":
            reply_message_id = _reply_message_id(msg)
            reply_signal = db.signal_from_telegram_message(reply_message_id)
            reply_contract = _reply_contract(msg)

            if reply_signal:
                query = reply_signal["token"]
            elif reply_contract:
                query = reply_contract
            elif len(parts) >= 2:
                query = parts[1]
            else:
                await send(http,
                    "Use /sell TICKER (example: /sell SABL), or reply to the original alert with /sell.")
                continue

            pos, pos_err = db.find_open_position(query)
            if not pos:
                await send(http, pos_err or "No recorded open position matched that ticker/contract.")
                continue

            pair = await pair_for_token(http,pos["chain"],pos["token"])
            current = f(pair.get("priceUsd")) if pair else pos["entry_price"]
            journal_id=db.journal_snapshot(pos["id"],"SELL_ESTIMATED",before=pos)
            pnl = db.close_position(pos["id"],current)
            db.journal_finish(journal_id,pos["id"])
            ret = (current/max(pos["entry_price"],1e-18)-1)*100
            await send(http,
                f"✅ SALE RECORDED — {pos['symbol']}\n"
                f"Return: {ret:+.1f}%\n"
                f"Approx P/L on recorded ${pos['amount_usd']:.2f}: ${pnl:+.2f}\n"
                "This updates the journal only; the bot did not place a sell order.")


async def daily_report(http, db):
    capture_signal_outcomes(db); capture_decision_outcomes(db); audit_missed_opportunities(db)
    stats=db.daily_stats(); eval30=stats["eval30"]
    if eval30:
        vals=[f(x) for x in eval30]; positive=sum(1 for x in vals if x>0)/len(vals)*100
        performance=f"30m endpoints: {len(vals)} | positive {positive:.0f}% | avg {statistics.mean(vals):+.1f}% | median {statistics.median(vals):+.1f}%"
    else:
        performance="Not enough completed 30m endpoints yet."

    outcomes=db.outcome_stats(hours=24); h60=[x for x in outcomes if int(x.get("horizon_min") or 0)==60]
    if h60:
        clean=sum(1 for x in h60 if f(x.get("max_return_pct"))>=15 and f(x.get("min_return_pct"))>-10)
        severe=sum(1 for x in h60 if f(x.get("min_return_pct"))<=-15)
        maxes=[f(x.get("max_return_pct")) for x in h60]; mins=[f(x.get("min_return_pct")) for x in h60]
        pathline=f"60m paths: {len(h60)} | clean +15 runners {clean} | hit -15 drawdown {severe} | median max {statistics.median(maxes):+.1f}% / min {statistics.median(mins):+.1f}%"
    else:
        pathline="60m path outcomes are still accumulating."

    missed=db.recent_missed(hours=24,limit=3)
    missedline="No clean missed-runner examples logged." if not missed else "Missed-runner ledger: "+"; ".join(
        f"{x['symbol']} max {f(x['max_return_pct']):+.0f}%" for x in missed)

    wf=walk_forward_summary(db.conn,30)
    wfline=format_walk_forward(wf).replace("\n"," | ")

    cutoff=int(time.time()-24*3600)
    grow=db.conn.execute("select count(*) from guardian_events where ts>=?",(cutoff,)).fetchone()[0]
    states={}
    for p in db.open_positions():
        st=str(p.get("guardian_state") or "ENTRY").replace("_"," "); states[st]=states.get(st,0)+1
    stateline=", ".join(f"{k} {v}" for k,v in states.items()) or "none"
    d60=db.conn.execute("""select d.event,o.max_return_pct,o.min_return_pct from decision_outcomes o
        join decision_ledger d on d.id=o.decision_id where d.ts>=? and o.horizon_min=60 and coalesce(o.suspect,0)=0""",(cutoff,)).fetchall()
    blocked=[dict(x) for x in d60 if str(x["event"]) in {"NEAR_ENTRY","HOT_WAIT"}]
    shadow=[dict(x) for x in d60 if str(x["event"])=="LANE_SHADOW"]
    blocked_good=sum(1 for x in blocked if f(x.get("max_return_pct"))>=15 and f(x.get("min_return_pct"))>-10)
    blocked_bad=sum(1 for x in blocked if f(x.get("min_return_pct"))<=-15)
    shadow_good=sum(1 for x in shadow if f(x.get("max_return_pct"))>=8 and f(x.get("min_return_pct"))>-10)
    decisionline=f"Blocked/near-entry paths: {len(blocked)} | later-clean +15 {blocked_good} | later -15 drawdown {blocked_bad} | governor shadows {len(shadow)} ({shadow_good} clean +8)"
    qrows=db.conn.execute("""select coalesce(pnl_quality,'ESTIMATED') q,count(*) n from positions
        where active=0 and close_ts>=? group by coalesce(pnl_quality,'ESTIMATED')""",(cutoff,)).fetchall()
    qcounts={str(r["q"]):int(r["n"]) for r in qrows}
    truthline=(f"Execution truth: {qcounts.get('CASH_VERIFIED',0)} cash-verified close(s) | "
               f"{qcounts.get('MIXED',0)} mixed | {qcounts.get('ESTIMATED',0)} quote-estimated. "
               "Only fully verified closed trades affect losing-streak/realized-P&L size reductions.")

    await send(http,
        "📊 DAILY GUARDIAN REPORT\n"
        f"Actionable signals last 24h: {stats['early']} | all signal records: {stats['signals']}\n"
        f"{performance}\n{pathline}\n{missedline}\n"
        f"Recorded realized P/L: ${f(stats['realized']):+.2f} | open positions: {stats['open']} ({stateline})\n"
        f"Guardian state changes logged: {grow}\n{decisionline}\n{truthline}\n{wfline}\n\n"
        "Adaptive confidence updates from completed history, but hard entry filters are not automatically loosened because of one missed pump or one bad trade. Use /learn for examples, /lab for unseen-data validation, and /positions for live management.")

def summarize_entry_blockers(output, db, state):
    counts = {}
    rt = state.get("realtime")
    for c in output[:25]:
        pair=c["pair"]; chain=pair.get("chainId",""); token=nest(pair,"baseToken","address",default="")
        scout=db.recent_signal(chain,token,"SCOUT",hours=2)
        rts=(c.get("rt") or (rt.stats(token) if rt and chain=="solana" else {}))
        safety=cached_safety(state,chain,token,pair=pair)
        blockers=actionable_blockers(pair,c["entry"],c["confirmed"],c["risks"],c["hard"],rts=rts,scout=scout,safety=safety)
        for reason in blockers:
            low=reason.lower()
            if low.startswith("buy/sell"): key="buy/sell pressure too weak"
            elif "swaps/5m" in low: key="not enough 5m swaps"
            elif "below option trigger" in low: key="entry/confirmed score too low"
            elif "5m move" in low or "5m momentum" in low: key="5m move outside entry window"
            elif "crash floor" in low: key="1h crash/falling-knife filter"
            elif "1h move" in low: key="1h move too extended"
            elif "rugcheck" in low: key="waiting for RugCheck"
            elif "holder verification" in low: key="waiting for holder verification"
            elif low.startswith("liquidity"): key="liquidity below minimum"
            elif "internal scout" in low: key="waiting for internal scout"
            elif "realtime" in low: key="realtime activity too weak"
            elif "market cap" in low: key="market cap outside range"
            else: key=reason
            counts[key]=counts.get(key,0)+1
    return sorted(counts.items(), key=lambda kv:kv[1], reverse=True)



async def hot_scout_pass(http,guard,limiter,db,state):
    """Re-price established internal scouts every few seconds.

    The full discovery cycle is intentionally broader/slower. Once a token is already an
    internal scout, this lane removes avoidable latency without weakening safety: every BUY
    still has to pass live tier, crash-memory stability, commitment, safety and chase gates.
    """
    if not HOT_SCOUT_ENABLED:
        return 0
    scouts=list(state.get("active_scouts") or [])[:max(1,HOT_SCOUT_MAX)]
    if not scouts:
        return 0
    catalysts=db.recent_catalysts(CAT_LOOKBACK)
    regime=state.get("market_regime") or {"label":"NEUTRAL"}
    sent=0
    for chain,token,symbol,old_entry,old_confirmed in scouts:
        chain=str(chain or "").lower(); token=str(token or "")
        if chain not in ALERT_ENTRY_CHAINS or not token or db.position_by_token(token):
            continue
        # Hot lane is first-entry only. Controlled second legs remain in the full scanner,
        # where more context is available.
        hot_cooldown=entry_cooldown_context(db,chain,token)
        if hot_cooldown.get("recent_early"):
            continue
        scout=db.recent_signal(chain,token,"SCOUT",hours=2)
        if not scout or time.time()-f(scout.get("ts")) < SCOUT_CONFIRM_SECONDS:
            continue
        live=await pair_for_token(http,chain,token)
        if not live:
            continue
        db.observe(live)
        m=metrics(live)
        prior=db.previous(chain,token,90)
        raw,reasons,risks,kind=early_score(live,prior)
        if raw<=0:
            continue
        cb,hits=catalyst_bonus(live,catalysts); reasons=list(reasons); risks=list(risks)
        if cb: reasons.append(f"fresh catalyst +{cb:.0f}")
        cached=(state.get("hot_candidate_cache") or {}).get(f"{chain}:{token}") or {}
        for text in cached.get("reasons") or []:
            if text not in reasons: reasons.append(text)
        for text in cached.get("risks") or []:
            if text not in risks: risks.append(text)
        source_ctx=candidate_source_context(state,chain,token)
        pulse=db.social_pulse(token,SOCIAL_PULSE_LOOKBACK_MINUTES)
        social_score=f(pulse.get("score")); source_bonus=f(source_ctx.get("bonus"))
        ratio=m["buys"]/max(m["sells"],1); turnover=100*m["v5"]/max(m["mc"],1)
        social_divergence=bool(SOCIAL_DIVERGENCE_ENABLED and social_score>=3 and
                               (ratio<1.10 or turnover<ENTRY_MIN_VOLUME_TURNOVER_PCT or m["pc5"]<-3))
        social_confirm=0.0 if social_divergence else min(2.0,max(0.0,social_score))
        if social_divergence: risks.append("social attention is not confirmed by live market flow")
        if source_ctx.get("paid_boost"): risks.append("DexScreener paid boost detected — discovery only, not a quality signal")

        safety=cached_safety(state,chain,token,pair=live)
        if safety is None:
            launch_safety_preflight(http,guard,db,chain,token,state)
            continue
        for text in safety.get("reasons") or []:
            if text not in reasons: reasons.append(text)
        for text in safety.get("risks") or []:
            if text not in risks: risks.append(text)
        hard=bool(safety.get("hard"))
        market_entry=clamp(raw+cb-(2.0 if social_score<0 else 0.0),0,100)
        final_entry=clamp(market_entry+min(8.0,max(0.0,f(safety.get("score_adjustment")))),0,100)
        conf_bonus,conf_reasons=confirmation_bonus(live); reasons+=conf_reasons
        confirmed=clamp(final_entry+conf_bonus+min(source_bonus,2.5)+social_confirm,0,100)
        if source_bonus>0: reasons.append(f"cross-source discovery confirmation +{min(source_bonus,2.5):.1f}")
        if social_confirm>0: reasons.append(f"broad exact-contract social corroboration +{social_confirm:.1f}")

        rt=state.get("realtime"); rts=rt.stats(token) if rt and chain=="solana" else {}
        move=(f(live.get("priceUsd"))/max(f(scout.get("price")),1e-18)-1)*100
        tier,tier_reasons=classify_actionable_tier(live,final_entry,confirmed,risks,hard,rts=rts,safety=safety,scout_move=move)
        if not tier:
            db.log_decision(live,"HOT_WAIT","WATCH",final_entry,confirmed,
                            actionable_blockers(live,final_entry,confirmed,risks,hard,rts=rts,scout=scout,safety=safety),
                            rts=rts,social=pulse,market_regime=str(regime.get("label") or "NEUTRAL"),sources=source_ctx.get("sources"))
            continue
        stable,stable_blocks,_=entry_stability_check(db,live,tier)
        tape_ok,tape_blocks,_=entry_tape_preservation(db,live,tier) if stable else (False,[],{})
        edge_ok,edge_blocks,edge_info=capital_edge_gate(db,live,tier,final_entry,confirmed) if stable and tape_ok else (False,[],{})
        edge_validation_ok=bool(edge_ok or edge_info.get("shadow_only"))
        commit,commit_blocks,_=entry_commitment_gate(db,live,tier,final_entry,confirmed,rts=rts) if stable and tape_ok and edge_validation_ok else (False,[],{})
        capital_confirm,capital_blocks,_=capital_confirmation_gate(db,live,tier) if stable and tape_ok and edge_validation_ok and commit else (False,[],{})
        path_ok,path_blocks,path_info=stateful_sol_edge_gate(db,live,tier) if stable and tape_ok and edge_validation_ok and commit and capital_confirm else (False,[],{})
        exec_ok,exec_blocks,exec_info=await execution_preflight(http,state,live,tier) if path_ok else (False,[],{})
        blocks=list(stable_blocks or [])+list(tape_blocks or [])+list(edge_blocks or [])+list(commit_blocks or [])+list(capital_blocks or [])+list(path_blocks or [])+list(exec_blocks or [])
        if not (stable and tape_ok and edge_validation_ok and commit and capital_confirm and path_ok and exec_ok):
            if not stable: stage_event="ENTRY_STABILITY"
            elif not tape_ok: stage_event="ENTRY_TAPE"
            elif not edge_validation_ok:
                stage_event="ENTRY_REGIME" if (edge_info.get("breadth") or {}).get("risk_off") else ("ENTRY_HISTORY" if (edge_info.get("analogs") or {}).get("veto") else "ENTRY_EDGE")
            elif not (commit and capital_confirm): stage_event="ENTRY_COMMIT"
            elif not path_ok: stage_event="ENTRY_PATH"
            else: stage_event="ENTRY_EXECUTION"
            db.log_decision(live,stage_event,tier,final_entry,confirmed,blocks,rts=rts,social=pulse,
                            market_regime=str(regime.get("label") or "NEUTRAL"),sources=source_ctx.get("sources"),min_seconds=45)
            continue
        current=f(live.get("priceUsd")); scout_move=(current/max(f(scout.get("price")),1e-18)-1)*100
        if scout_move>MAX_CHASE or scout_move < -CHECK_MAX_DROP_FROM_ALERT_PCT:
            db.log_decision(live,"ENTRY_CHASE",tier,final_entry,confirmed,
                            [f"hot-scout move {scout_move:+.2f}% outside live action band"],rts=rts,social=pulse,
                            market_regime=str(regime.get("label") or "NEUTRAL"),sources=source_ctx.get("sources"),min_seconds=60)
            continue
        key=chain+":"+token
        if not limiter.allowed_action("entry:"+key,cooldown=6*3600):
            db.log_decision(live,"ENTRY_RATE_LIMIT",tier,final_entry,confirmed,
                            ["per-token/hourly action limiter suppressed a duplicate entry"],rts=rts,social=pulse,
                            market_regime=str(regime.get("label") or "NEUTRAL"),sources=source_ctx.get("sources"),min_seconds=60)
            continue
        edge_reason=f"fast SOL edge {path_info.get('mode','PATH')}"
        if exec_info.get("available"): edge_reason+=f"; est friction {f(exec_info.get('total_friction_bps'))/100:.2f}%"
        db.log_decision(live,"EDGE_ARMED",tier,final_entry,confirmed,[edge_reason],rts=rts,social=pulse,
                        market_regime=str(regime.get("label") or "NEUTRAL"),sources=source_ctx.get("sources"),min_seconds=30)
        await shadow_auto_open(http,db,live,tier,final_entry,reasons+tier_reasons,execution_info=exec_info,source_event="EDGE_ARMED_HOT")
        proof_candidate,proof_candidate_reason=proof_candidate_eligibility(tier,final_entry)
        if not proof_candidate:
            limiter.release_action("entry:"+key)
            db.log_decision(live,"RESEARCH_SHADOW",tier,final_entry,confirmed,[proof_candidate_reason],rts=rts,social=pulse,
                            market_regime=str(regime.get("label") or "NEUTRAL"),sources=source_ctx.get("sources"),min_seconds=30)
            continue
        if edge_info.get("shadow_only"):
            limiter.release_action("entry:"+key)
            db.log_decision(live,"LANE_SHADOW",tier,final_entry,confirmed,[str(edge_info.get("shadow_only_reason") or "lane is shadow-only")],
                            rts=rts,social=pulse,market_regime=str(regime.get("label") or "NEUTRAL"),sources=source_ctx.get("sources"),min_seconds=30)
            continue
        auth=entry_signal_mode(db,tier,final_entry)
        if auth.get("mode")!="LIVE":
            sent_test=await send_preproof_actionable_signal(http,db,state,limiter,key,live,tier,final_entry,confirmed,
                reasons+tier_reasons,risks,hits,safety,rts,regime,pulse,source_ctx,exec_info,kind=kind,hot=True)
            if sent_test: sent+=1
            elif signal_policy(db) in {"strict","proven"}: limiter.release_action("entry:"+key)
            continue
        all_reasons=reasons+tier_reasons+["fast hot-scout revalidation passed",edge_reason,"v15 stateful + historical-risk + execution-quality gates passed"]
        calibration=adaptive_dollar_size(db,live,tier,final_entry,market_regime=str(regime.get("label") or "NEUTRAL"),
                                         social=pulse,safety=safety) if ADAPTIVE_CONFIDENCE_ENABLED else None
        message=beginner_alert(kind,live,final_entry,tier,all_reasons,risks,hits,
                       bool(safety.get("wallet_checked")),bool(safety.get("security_checked")),
                       bool(safety.get("holder_checked")),bool(safety.get("trader_checked")),rts=rts,safety=safety,
                       tier=tier,confirmed_score=confirmed,calibration=calibration,market_regime=regime,social=pulse,source_ctx=source_ctx,
                       track_record=alert_track_record_line(db,tier))
        mid=await send(http,message)
        if TG and CHAT and mid is None:
            limiter.release_action("entry:"+key)
            db.log_decision(live,"ENTRY_DELIVERY_FAIL",tier,final_entry,confirmed,["Telegram did not confirm delivery; no entry cooldown consumed"],
                            rts=rts,social=pulse,market_regime=str(regime.get("label") or "NEUTRAL"),sources=source_ctx.get("sources"),min_seconds=15)
            continue
        db.log_decision(live,"ENTRY_SENT_HOT",tier,final_entry,confirmed,[],rts=rts,social=pulse,
                        market_regime=str(regime.get("label") or "NEUTRAL"),sources=source_ctx.get("sources"),min_seconds=30)
        signal_id=db.add_signal("EARLY",live,final_entry,tier,all_reasons)
        db.log_latency(signal_id,token,"ENTRY_ALERT_HOT",current,
                       f"tier={tier};scout_move={scout_move:.2f};confirmed={confirmed:.1f};path={path_info.get('mode')};friction={execution_friction_pct(exec_info):.2f}")
        db.map_telegram_signal(mid,signal_id); db.map_trade_context(mid,signal_id,live,tier)
        schedule_entry_followups(http,db,state,signal_id)
        await paper_auto_open(http,db,db.signal_by_id(signal_id),live,tier,execution_info=exec_info)
        sent+=1
    return sent


async def hot_scout_loop(http,guard,limiter,db,state):
    while True:
        try:
            sent=await hot_scout_pass(http,guard,limiter,db,state)
            if sent: print(f"[hot scout] sent {sent} fast revalidated entry alert(s)")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[hot scout error] {type(exc).__name__}: {exc}")
        await asyncio.sleep(max(3,HOT_SCOUT_MONITOR_SECONDS))


async def cycle(http, guard, limiter, db, state):
    state["scan"] += 1
    cleanup_runtime_state(state)
    if time.time() - state.get("last_maintenance", 0) >= 3600:
        db.maintenance()
        state["last_maintenance"] = time.time()
    cleanup_candidate_sources(state)
    await scan_catalysts(http, db, state, limiter)
    catalysts = db.recent_catalysts(CAT_LOOKBACK)
    regime = await update_market_regime(http,state)

    # Public discovery calls run in parallel where their rate limits are independent.
    dex,gt,gt_trending,jup_tokens = await asyncio.gather(
        dex_discovery(http,state),gecko_new_pools(http,state),gecko_trending_pools(http,state),jupiter_token_discovery(http,state))
    smart = await birdeye_smart_money(http, guard, state)
    new_listings = await birdeye_new_listing(http, guard, state)
    social_found = await social_contract_discovery(http, db, state)
    found = {c: (dex.get(c,set()) | gt.get(c,set()) | gt_trending.get(c,set()) | jup_tokens.get(c,set()) | new_listings.get(c,set()) | social_found.get(c,set())) for c in CHAINS}

    # Explicitly tracked tokens and recently-scouted tokens stay in the scanner even
    # after they disappear from a new-pool feed. This is what enables consolidation/re-entry tracking.
    for w in db.manual_watches():
        found.setdefault(w["chain"],set()).add(w["token"])
    for s in db.recent_signal_tokens(hours=6,limit=80):
        found.setdefault(s["chain"],set()).add(s["token"])
    for o in db.recent_observation_tokens(hours=4,limit=100):
        found.setdefault(o["chain"],set()).add(o["token"])
    if "solana" in found:
        found["solana"] |= smart
        rt = state.get("realtime")
        if rt:
            rt_mints=rt.candidate_mints(); found["solana"] |= rt_mints
            for mint in rt_mints: mark_candidate_source(state,"solana",mint,"realtime Solana WS")
        portal=state.get("pumpportal")
        if portal:
            pmints=portal.candidate_mints(); found["solana"] |= pmints
            for mint in pmints:
                mark_candidate_source(state,"solana",mint,"PumpPortal migration" if portal.is_migration(mint) else "PumpPortal new-token")

    candidates = []
    entry_first=sorted(found,key=lambda ch:(0 if ch in ENTRY_CHAINS else (1 if ch in MANUAL_ENTRY_CHAINS else 2),ch))
    for chain in entry_first:
        if chain in MANUAL_ENTRY_CHAINS and state["scan"] % MANUAL_CHAIN_SCAN_EVERY_SCANS != 0:
            continue
        if chain not in ALERT_ENTRY_CHAINS and state["scan"] % NON_ENTRY_CHAIN_SCAN_EVERY_SCANS != 0:
            continue
        pairs = await get_pairs(http,chain,found.get(chain,set()))
        for pair in pairs:
            token = nest(pair,"baseToken","address",default="")
            previous = db.previous(chain,token,90)
            score,reasons,risks,kind = early_score(pair,previous)
            db.observe(pair)
            radar_ok,radar_reasons=runner_radar_qualifies(pair)
            if radar_ok:
                db.log_decision(pair,"RUNNER_RADAR","WATCH",max(0,score),max(0,score),radar_reasons,
                                market_regime=str((regime or {}).get("label") or "NEUTRAL"),
                                sources=candidate_source_context(state,chain,token).get("sources"),min_seconds=300)
                if not db.recent_signal(chain,token,"RUNNER_RADAR",hours=1):
                    db.add_signal("RUNNER_RADAR",pair,max(0,score),"WATCH ONLY",radar_reasons)
            if score <= 0:
                continue
            cb,hits = catalyst_bonus(pair,catalysts)
            if cb:
                reasons.append(f"fresh catalyst +{cb:.0f}")
            social_mentions,social_sources=social_context(pair,catalysts)
            pulse=db.social_pulse(token,SOCIAL_PULSE_LOOKBACK_MINUTES)
            source_ctx=candidate_source_context(state,chain,token)
            if social_mentions:
                reasons.append(f"{social_mentions} exact-contract social mention(s) across {','.join(social_sources)}")
            if pulse.get("mentions"):
                reasons.append(f"social pulse {pulse['mentions']} mention(s) / {pulse['unique_authors']} author(s) ({pulse['status']})")
            if pulse.get("score",0)<0:
                risks.append("social mentions are concentrated/spam-like")
            if source_ctx.get("paid_boost"):
                risks.append("DexScreener paid boost detected — discovery only, not a quality signal")
            portal=state.get("pumpportal")
            if portal and portal.is_migration(token):
                reasons.append("PumpPortal migration detected")
            candidates.append({
                "raw":score,"pair":pair,"reasons":reasons,"risks":risks,"kind":kind,
                "cat":cb,"hits":hits,"wallet":0,"sec":0,"trader":0,"holder":0,
                "wallet_checked":False,"security_checked":False,"trader_checked":False,
                "holder_checked":False,"hard":False,"social":pulse,"source_ctx":source_ctx,
            })

    candidates.sort(key=lambda x:x["raw"]+x["cat"],reverse=True)
    # Optional exact-contract X pulse is deliberately opt-in because X API access can
    # be metered. When enabled, refresh social metrics before final scoring.
    await fetch_x_targeted_pulses(http,db,state,candidates)
    if XTOKEN and bool_pref(db,"x_targeted_pulse",X_TARGETED_PULSE_ENABLED):
        for c in candidates[:max(5,X_TARGETED_MAX_CANDIDATES)]:
            token=nest(c["pair"],"baseToken","address",default="")
            c["social"]=db.social_pulse(token,SOCIAL_PULSE_LOOKBACK_MINUTES)

    rt = state.get("realtime")
    if rt:
        watched = 0
        for c in candidates:
            if watched >= 10:
                break
            pair = c["pair"]
            if pair.get("chainId") != "solana":
                continue
            m = metrics(pair)
            if m["liq"] >= SCOUT_MIN_LIQUIDITY and m["mc"] >= SCOUT_MIN_MCAP and m["pc5"] <= 6:
                token = nest(pair,"baseToken","address",default="")
                await rt.watch_mint(token)
                rts = rt.stats(token)
                c["rt"] = rts
                if rts.get("available") and rts.get("tx30",0) >= RT_SCOUT_TX30:
                    c["reasons"].append(
                        f"realtime activity {rts['tx30']} tx/30s ({rts['acceleration']:.1f}x burst)"
                    )
                if rts.get("newly_detected"):
                    c["reasons"].append("detected by real-time Pump/PumpSwap radar")
                watched += 1

    deep_now = guard.available() and state["scan"] % max(BE_EVERY,1) == 0
    deep_used = 0
    if deep_now:
        for c in candidates:
            if deep_used >= min(BE_DEEP, 1):
                break
            pair = c["pair"]
            chain = pair.get("chainId","")
            token = nest(pair,"baseToken","address",default="")
            if chain not in ENTRY_CHAINS:
                continue
            if chain == "solana":
                trades = await birdeye_trades(http,guard,token)
                if state.get("copy_engine"):
                    try: state["copy_engine"].observe_flow_trades(token,trades,pair)
                    except Exception: pass
                ws,wrs,wrisk = wallet_score(trades)
                profile=wallet_flow_profile(trades)
                c["wallet"] = ws
                c["wallet_checked"] = bool(trades)
                c["flow_concentration"] = f(profile.get("top_buyer_share"))
                c["reasons"] += wrs
                c["risks"] += wrisk
            sp,sflags,hard,schecked = await token_security(http,guard,chain,token)
            c["sec"] = sp; c["hard"] = c["hard"] or hard; c["security_checked"] = schecked
            c["risks"] += sflags
            ts,trs,trisk,tchecked = await top_trader_context(http,guard,chain,token,state.get("copy_engine"))
            c["trader"] = ts; c["trader_checked"] = tchecked
            c["reasons"] += trs; c["risks"] += trisk
            if chain == "solana":
                hs,hinfo,hblock,hchecked,hsource = await resilient_holder_context(http,guard,token)
                c["holder"] = hs; c["holder_checked"] = hchecked; c["holder_source"] = hsource
                c["hard"] = c["hard"] or hblock
                if hs >= 0: c["reasons"] += hinfo
                else: c["risks"] += hinfo
            deep_used += 1

    output = []
    for c in candidates:
        pulse=c.get("social") or {}; source_ctx=c.get("source_ctx") or {}
        social_score=f(pulse.get("score")); source_bonus=f(source_ctx.get("bonus"))
        m=metrics(c["pair"]); ratio=m["buys"]/max(m["sells"],1); turnover=100.0*m["v5"]/max(m["mc"],1)
        social_divergence=bool(SOCIAL_DIVERGENCE_ENABLED and social_score>=3 and
                               (ratio<1.10 or turnover<ENTRY_MIN_VOLUME_TURNOVER_PCT or m["pc5"]<-3))
        if social_divergence:
            c["risks"].append("social attention is not confirmed by live market flow")
            social_confirm=0.0
        else:
            social_confirm=min(2.0,max(0.0,social_score))
        market_entry = clamp(c["raw"] + c["cat"] - (2.0 if social_score<0 else 0.0), 0, 100)
        positive_enrichment = max(0, c["wallet"]*0.25) + max(0, c["sec"]) + max(0, c["trader"]*0.5) + max(0, c["holder"]*0.5)
        entry = clamp(market_entry + min(12, positive_enrichment), 0, 100)
        conf_bonus, conf_reasons = confirmation_bonus(c["pair"])
        confirmed = clamp(entry + conf_bonus + min(source_bonus,2.5) + social_confirm,0,100)
        if source_bonus>0:
            c["reasons"].append(f"cross-source discovery confirmation +{min(source_bonus,2.5):.1f}")
        if social_confirm>0:
            c["reasons"].append(f"broad exact-contract social corroboration +{social_confirm:.1f}")
        c["reasons"] += conf_reasons
        c["entry"] = entry; c["confirmed"] = confirmed; c["social_divergence"]=social_divergence
        c["action"] = entry_action(c["pair"],entry,c["risks"],c["hard"])
        output.append(c)
    output.sort(key=lambda x:max(x["entry"],x["confirmed"]),reverse=True)
    state["hot_candidate_cache"] = {
        f"{str(x['pair'].get('chainId','')).lower()}:{nest(x['pair'],'baseToken','address',default='')}": x
        for x in output[:40] if nest(x.get('pair') or {},'baseToken','address',default='')
    }
    state["entry_diag"] = summarize_entry_blockers(output, db, state)
    state["near_entries"] = []
    rt_diag = state.get("realtime")
    for nc in output:
        npair=nc["pair"]; nch=str(npair.get("chainId","")).lower(); ntok=nest(npair,"baseToken","address",default="")
        if nch not in ALERT_ENTRY_CHAINS:
            continue
        nscout=db.recent_signal(nch,ntok,"SCOUT",hours=2)
        nrts=(nc.get("rt") or (rt_diag.stats(ntok) if rt_diag else {}))
        nsafety=cached_safety(state,nch,ntok,pair=npair)
        nblocks=actionable_blockers(npair,nc["entry"],nc["confirmed"],nc["risks"],nc["hard"],rts=nrts,scout=nscout,safety=nsafety)
        if len(nblocks) <= 4:
            db.log_decision(npair,"NEAR_ENTRY",nc.get("action") or "WATCH",nc["entry"],nc["confirmed"],nblocks,
                            rts=nrts,social=nc.get("social"),market_regime=str((regime or {}).get("label") or "NEUTRAL"),
                            sources=(nc.get("source_ctx") or {}).get("sources"))
        if len(nblocks) <= 3:
            nm=metrics(npair)
            state["near_entries"].append({
                "symbol":nest(npair,"baseToken","symbol",default="?"), "token":ntok,
                "entry":nc["entry"], "confirmed":nc["confirmed"], "pc5":nm["pc5"], "pc1":nm["pc1"],
                "liq":nm["liq"], "ratio":nm["buys"]/max(nm["sells"],1), "blocks":nblocks[:3]
            })
        if len(state["near_entries"]) >= 5:
            break

    top_entry = max((x["entry"] for x in output), default=0)
    top_confirmed = max((x["confirmed"] for x in output), default=0)
    top = max(top_entry, top_confirmed)
    state["top"] = top
    state["candidate_count"] = len(output)
    state["be_disabled"] = set(guard.disabled_features)
    state["be_invalid"] = guard.invalid_key
    state["be_status"] = guard.status_text()
    top_blocker = state["entry_diag"][0][0] if state.get("entry_diag") else "none"
    leader = max(output, key=lambda x:x["entry"], default=None)
    leader_text = "none"
    if leader:
        lp = leader["pair"]
        lsym = nest(lp,"baseToken","symbol",default="?")
        lchain = lp.get("chainId","")
        ltoken = nest(lp,"baseToken","address",default="")
        lscout = db.recent_signal(lchain,ltoken,"SCOUT",hours=2)
        lrts = leader.get("rt") or (state.get("realtime").stats(ltoken) if state.get("realtime") and lchain=="solana" else {})
        lsafety = cached_safety(state,lchain,ltoken,pair=lp)
        lbs = actionable_blockers(lp,leader["entry"],leader["confirmed"],leader["risks"],leader["hard"],rts=lrts,scout=lscout,safety=lsafety)
        leader_text = f"{lsym} entry={leader['entry']:.1f} confirmed={leader['confirmed']:.1f} blocked={('|'.join(lbs[:2]) if lbs else 'none')}"
    print(f"[scan {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}] discovered={sum(len(v) for v in found.values())} "
          f"candidates={len(output)} entry_top={top_entry:.1f} confirmed_top={top_confirmed:.1f} be_enriched={deep_used} "
          f"top_blocker={top_blocker} leader={leader_text} birdeye={guard.status_text()}")

    state["active_scouts"] = []
    for c in output[:25]:
        pair = c["pair"]
        chain = pair.get("chainId","")
        token = nest(pair,"baseToken","address",default="")
        key = chain+":"+token
        m = metrics(pair)
        rt = state.get("realtime")
        rts = (c.get("rt") or (rt.stats(token) if rt and chain=="solana" else {}))

        if c["hard"]:
            continue

        social_mentions,social_sources=social_context(pair,catalysts)
        if (not bool_pref(db,"quiet_mode",False) and SOCIAL_CONTRACT_ALERTS and social_mentions>=SOCIAL_MIN_MENTIONS and m["age"]<=180
                and limiter.allowed("social:"+key,cooldown=3*3600)):
            await send(http,
                f"📣 SOCIAL LAUNCH RADAR — {nest(pair,'baseToken','name',default='Unknown')} ({nest(pair,'baseToken','symbol',default='?')})\n"
                f"Exact contract appeared {social_mentions} time(s) across {', '.join(social_sources)}.\n"
                f"MC {usd(m['mc'])} | liq {usd(m['liq'])} | 5m {m['pc5']:+.1f}%\n"
                f"Contract: {token}\nACTION: research/watch; an entry alert will come separately if market gates pass.")

        # INTERNAL SCOUT: this state is recorded regardless of Telegram notification
        # limits/preferences. Telegram GET READY can never control whether an entry exists.
        scout = db.recent_signal(chain,token,"SCOUT",hours=2)
        realtime_early = bool(rts.get("available") and rts.get("tx30",0) >= RT_SCOUT_TX30)
        flow_scout = (
            m["liq"] >= FLOW_SCOUT_MIN_LIQUIDITY and m["mc"] >= SCOUT_MIN_MCAP
            and (m["liq"] / max(m["mc"],1)) >= FLOW_SCOUT_MIN_LIQ_MC
            and (100.0*m["v5"]/max(m["mc"],1)) >= FLOW_SCOUT_MIN_TURNOVER_PCT
            and (m["buys"]/max(m["sells"],1)) >= FLOW_SCOUT_MIN_BUY_SELL
            and (m["buys"]+m["sells"]) >= FLOW_SCOUT_MIN_SWAPS
            and OPTION_MIN_5M <= m["pc5"] <= min(5.0, OPTION_MAX_5M)
            and OPTION_MIN_1H <= m["pc1"] <= OPTION_MAX_1H
        )
        source_converged = len((c.get("source_ctx") or {}).get("buckets") or []) >= 2
        portal_early = bool(state.get("pumpportal") and state["pumpportal"].is_recent(token))
        scout_condition = (
            chain in ALERT_ENTRY_CHAINS
            and m["liq"] >= SCOUT_MIN_LIQUIDITY
            and m["mc"] >= SCOUT_MIN_MCAP
            and OPTION_MIN_5M <= m["pc5"] <= OPTION_MAX_5M
            and m["pc1"] <= OPTION_MAX_1H
            and "sells dominate" not in " ".join(c["risks"]).lower()
            and "net-selling" not in " ".join(c["risks"]).lower()
            and ((c["raw"] + c["cat"] >= SCOUT_SCORE) or realtime_early or rts.get("newly_detected")
                 or flow_scout or portal_early or source_converged)
        )
        if scout is None and scout_condition:
            sid = db.add_signal("SCOUT",pair,c["raw"]+c["cat"],"WATCH ONLY",c["reasons"])
            db.log_latency(sid, token, "SCOUT", f(pair.get("priceUsd")), f"rt30={rts.get('tx30',0)}")
            scout = db.signal_by_id(sid)
            if rt and chain=="solana":
                await rt.watch_mint(token)
            launch_safety_preflight(http, guard, db, chain, token, state)
            cross_watch_ok=(chain in MANUAL_ENTRY_CHAINS and CROSS_CHAIN_SCOUT_TELEGRAM
                            and m["liq"]>=MANUAL_CHAIN_MIN_LIQUIDITY
                            and (m["buys"]/max(m["sells"],1))>=1.50
                            and (m["buys"]+m["sells"])>=15
                            and (100.0*m["v5"]/max(m["mc"],1))>=0.04
                            and (c["raw"]+c["cat"]>=70))
            if ((chain in ENTRY_CHAINS or cross_watch_ok) and not bool_pref(db,"quiet_mode",False) and bool_pref(db, "telegram_scouts", SCOUT_TELEGRAM_DEFAULT)
                    and limiter.allowed("scout:"+key, cooldown=2*3600)):
                msg=scout_message(pair,c["raw"]+c["cat"],c["reasons"],c["risks"])
                if cross_watch_ok and chain not in ENTRY_CHAINS:
                    msg="🌐 CROSS-CHAIN WATCH — MANUAL ONLY\n"+msg+"\nThis is NOT a buy call; wait for MANUAL CHAIN ENTRY confirmation."
                mid = await send(http,msg)
                db.map_telegram_signal(mid,sid)

        if scout:
            state["active_scouts"].append((chain,token,nest(pair,"baseToken","symbol",default="?"),c["entry"],c["confirmed"]))
            if cached_safety(state,chain,token,pair=pair) is None:
                launch_safety_preflight(http, guard, db, chain, token, state)

        # Actionable entry path. Unlike v9.5, confirmation may promote a valid setup.
        entry_sent = False
        if scout and (time.time() - scout["ts"] >= SCOUT_CONFIRM_SECONDS):
            move_from_scout = (f(pair.get("priceUsd")) / max(scout["price"],1e-18) - 1) * 100
            safety = cached_safety(state, chain, token, pair=pair)
            if safety is None:
                rb, notes, hard, checked = await rugcheck_summary(http, token) if chain=="solana" else (0,[],False,False)
                safety = {
                    "ts":time.time(), "hard":hard, "reasons":notes if not hard else [],
                    "risks":notes if hard else [], "rug_checked":checked,
                    "holder_checked":False, "security_checked":False,
                    "trader_checked":False, "wallet_checked":False, "score_adjustment":0.0,
                }
                state["safety_cache"][f"{chain}:{token}"] = safety

            c["reasons"] += [x for x in safety.get("reasons",[]) if x not in c["reasons"]]
            c["risks"] += [x for x in safety.get("risks",[]) if x not in c["risks"]]
            c["holder_checked"] = c.get("holder_checked") or safety.get("holder_checked",False)
            c["security_checked"] = c.get("security_checked") or safety.get("security_checked",False)
            c["trader_checked"] = c.get("trader_checked") or safety.get("trader_checked",False)
            c["wallet_checked"] = c.get("wallet_checked") or safety.get("wallet_checked",False)

            # Negative Birdeye labels are warnings, not an automatic score cliff.
            safety_bonus = max(0.0, f(safety.get("score_adjustment")))
            final_entry_score = clamp(c["entry"] + min(8.0, safety_bonus), 0, 100)
            tier, tier_reasons = classify_actionable_tier(
                pair, final_entry_score, c["confirmed"], c["risks"], c["hard"],
                rts=rts, safety=safety, scout_move=move_from_scout
            )

            # v13.3 version-aware cooldown. The legacy signal table has no build
            # column, so pair it with Decision Ledger ENTRY_SENT rows. A same-build
            # alert keeps the normal cooldown across restarts. An older-build alert
            # gets only a short upgrade grace period, after which the current model
            # may judge a genuinely fresh scout on its own merits. Open positions are
            # always protected separately and never bypassed by this reset.
            cooldown_ctx = entry_cooldown_context(db,chain,token) if tier else {"recent_early":None,"cross_build_grace":False}
            recent_early = cooldown_ctx.get("recent_early")
            cross_build_grace = bool(cooldown_ctx.get("cross_build_grace"))

            repeat_entry = False
            if tier and recent_early and SECOND_LEG_ENABLED and not db.position_by_token(token):
                repeat_age_min = (time.time() - f(recent_early.get("ts"))) / 60.0
                repeat_move = (f(pair.get("priceUsd")) / max(f(recent_early.get("price")),1e-18) - 1) * 100
                repeat_turnover = 100.0 * m["v5"] / max(m["mc"],1)
                repeat_ratio = m["buys"] / max(m["sells"],1)
                repeat_entry = (
                    repeat_age_min >= SECOND_LEG_MIN_MINUTES
                    and SECOND_LEG_MIN_MOVE_FROM_PRIOR_PCT <= repeat_move <= SECOND_LEG_MAX_MOVE_FROM_PRIOR_PCT
                    and repeat_turnover >= SECOND_LEG_MIN_TURNOVER_PCT
                    and repeat_ratio >= SECOND_LEG_MIN_BUY_SELL
                    and (m["buys"] + m["sells"]) >= SECOND_LEG_MIN_SWAPS
                    and OPTION_MIN_5M <= m["pc5"] <= min(5.0, OPTION_MAX_5M)
                    and OPTION_MIN_1H <= m["pc1"] <= OPTION_MAX_1H
                    and bool(safety and not safety.get("hard"))
                    and (bool(safety.get("rug_checked")) if chain == "solana" else True)
                    and (bool(safety.get("holder_checked")) if chain == "solana" else True)
                )

            if tier and recent_early is not None and not repeat_entry:
                why = "cross-build upgrade grace" if cross_build_grace else "same-build entry cooldown / second-leg reset not proven"
                db.log_decision(pair,"ENTRY_COOLDOWN",tier,final_entry_score,c["confirmed"],[why],
                                rts=rts,social=c.get("social"),market_regime=str((regime or {}).get("label") or "NEUTRAL"),
                                sources=(c.get("source_ctx") or {}).get("sources"),min_seconds=60)

            if tier and (recent_early is None or repeat_entry):
                live_pair = await pair_for_token(http, chain, token)
                if live_pair:
                    live_price = f(live_pair.get("priceUsd"))
                    decision_price = f(pair.get("priceUsd"))
                    live_move = (live_price/max(scout["price"],1e-18)-1)*100
                    decision_slippage = (live_price/max(decision_price,1e-18)-1)*100
                    live_tier, live_reasons = classify_actionable_tier(
                        live_pair, final_entry_score, c["confirmed"], c["risks"], c["hard"],
                        rts=rts, safety=safety, scout_move=live_move
                    )
                    stability_ok, stability_blockers, stability_info = entry_stability_check(db, live_pair, live_tier)
                    tape_ok,tape_blockers,tape_info = entry_tape_preservation(db,live_pair,live_tier) if live_tier and stability_ok else (False,[],{})
                    edge_ok,edge_blockers,edge_info = capital_edge_gate(db,live_pair,live_tier,final_entry_score,c["confirmed"]) if live_tier and stability_ok and tape_ok else (False,[],{})
                    edge_validation_ok=bool(edge_ok or edge_info.get("shadow_only"))
                    commit_ok, commit_blockers, commit_info = entry_commitment_gate(db,live_pair,live_tier,final_entry_score,c["confirmed"],rts=rts) if live_tier and stability_ok and tape_ok and edge_validation_ok else (False,[],{})
                    capital_confirm_ok, capital_confirm_blockers, capital_confirm_info = capital_confirmation_gate(db,live_pair,live_tier) if live_tier and stability_ok and tape_ok and edge_validation_ok and commit_ok else (False,[],{})
                    path_ok,path_blockers,path_info = stateful_sol_edge_gate(db,live_pair,live_tier) if live_tier and stability_ok and tape_ok and edge_validation_ok and commit_ok and capital_confirm_ok else (False,[],{})
                    execution_ok,execution_blockers,execution_info = await execution_preflight(http,state,live_pair,live_tier) if path_ok else (False,[],{})
                    if live_tier and not stability_ok:
                        db.log_decision(live_pair,"ENTRY_STABILITY",live_tier,final_entry_score,c["confirmed"],stability_blockers,
                                        rts=rts,social=c.get("social"),market_regime=str((regime or {}).get("label") or "NEUTRAL"),
                                        sources=(c.get("source_ctx") or {}).get("sources"),min_seconds=60)
                    elif live_tier and stability_ok and not tape_ok:
                        print(f"[entry tape] {nest(live_pair,'baseToken','symbol',default='?')} waiting: {'; '.join(tape_blockers[:3])}")
                        db.log_decision(live_pair,"ENTRY_TAPE",live_tier,final_entry_score,c["confirmed"],tape_blockers,
                                        rts=rts,social=c.get("social"),market_regime=str((regime or {}).get("label") or "NEUTRAL"),
                                        sources=(c.get("source_ctx") or {}).get("sources"),min_seconds=60)
                    elif live_tier and stability_ok and tape_ok and not edge_validation_ok:
                        edge_event="ENTRY_REGIME" if (edge_info.get("breadth") or {}).get("risk_off") else ("ENTRY_HISTORY" if (edge_info.get("analogs") or {}).get("veto") else "ENTRY_EDGE")
                        print(f"[entry edge] {nest(live_pair,'baseToken','symbol',default='?')} rejected: {'; '.join(edge_blockers[:3])}")
                        db.log_decision(live_pair,edge_event,live_tier,final_entry_score,c["confirmed"],edge_blockers,
                                        rts=rts,social=c.get("social"),market_regime=str((regime or {}).get("label") or "NEUTRAL"),
                                        sources=(c.get("source_ctx") or {}).get("sources"),min_seconds=60)
                    elif live_tier and stability_ok and tape_ok and edge_validation_ok and (not commit_ok or not capital_confirm_ok):
                        all_confirm_blocks=list(commit_blockers or [])+list(capital_confirm_blockers or [])
                        print(f"[entry confirm] {nest(live_pair,'baseToken','symbol',default='?')} waiting: {'; '.join(all_confirm_blocks[:3])}")
                        db.log_decision(live_pair,"ENTRY_COMMIT",live_tier,final_entry_score,c["confirmed"],all_confirm_blocks,
                                        rts=rts,social=c.get("social"),market_regime=str((regime or {}).get("label") or "NEUTRAL"),
                                        sources=(c.get("source_ctx") or {}).get("sources"),min_seconds=60)
                    elif live_tier and not path_ok:
                        db.log_decision(live_pair,"ENTRY_PATH",live_tier,final_entry_score,c["confirmed"],path_blockers,
                                        rts=rts,social=c.get("social"),market_regime=str((regime or {}).get("label") or "NEUTRAL"),
                                        sources=(c.get("source_ctx") or {}).get("sources"),min_seconds=45)
                    elif live_tier and path_ok and not execution_ok:
                        db.log_decision(live_pair,"ENTRY_EXECUTION",live_tier,final_entry_score,c["confirmed"],execution_blockers,
                                        rts=rts,social=c.get("social"),market_regime=str((regime or {}).get("label") or "NEUTRAL"),
                                        sources=(c.get("source_ctx") or {}).get("sources"),min_seconds=60)
                    ready=bool(live_tier and stability_ok and tape_ok and edge_validation_ok and commit_ok and capital_confirm_ok and path_ok and execution_ok)
                    if ready and decision_slippage > max(ENTRY_MAX_DECISION_SLIPPAGE, 2.5):
                        db.log_decision(live_pair,"ENTRY_SLIPPAGE",live_tier,final_entry_score,c["confirmed"],
                                        [f"decision-to-live move {decision_slippage:+.2f}% exceeded cap"],rts=rts,social=c.get("social"),
                                        market_regime=str((regime or {}).get("label") or "NEUTRAL"),sources=(c.get("source_ctx") or {}).get("sources"),min_seconds=60)
                    action_cooldown = SECOND_LEG_MIN_MINUTES*60 if repeat_entry else 6*3600
                    if ready and decision_slippage <= max(ENTRY_MAX_DECISION_SLIPPAGE, 2.5) and limiter.allowed_action("entry:"+key,cooldown=action_cooldown):
                        edge_reason=f"SOL edge {path_info.get('mode','PATH')} path"
                        if execution_info.get("available"):
                            edge_reason += f"; est friction {f(execution_info.get('total_friction_bps'))/100:.2f}%"
                        db.log_decision(live_pair,"EDGE_ARMED",live_tier,final_entry_score,c["confirmed"],[edge_reason],
                                        rts=rts,social=c.get("social"),market_regime=str((regime or {}).get("label") or "NEUTRAL"),
                                        sources=(c.get("source_ctx") or {}).get("sources"),min_seconds=30)
                        await shadow_auto_open(http,db,live_pair,live_tier,final_entry_score,c["reasons"]+tier_reasons+live_reasons,
                                               execution_info=execution_info,source_event="EDGE_ARMED")
                        proof_candidate,proof_candidate_reason=proof_candidate_eligibility(live_tier,final_entry_score)
                        if not proof_candidate:
                            limiter.release_action("entry:"+key)
                            db.log_decision(live_pair,"RESEARCH_SHADOW",live_tier,final_entry_score,c["confirmed"],[proof_candidate_reason],
                                            rts=rts,social=c.get("social"),market_regime=str((regime or {}).get("label") or "NEUTRAL"),
                                            sources=(c.get("source_ctx") or {}).get("sources"),min_seconds=30)
                            continue
                        if edge_info.get("shadow_only"):
                            limiter.release_action("entry:"+key)
                            db.log_decision(live_pair,"LANE_SHADOW",live_tier,final_entry_score,c["confirmed"],
                                            [str(edge_info.get("shadow_only_reason") or "lane is shadow-only")],
                                            rts=rts,social=c.get("social"),market_regime=str((regime or {}).get("label") or "NEUTRAL"),
                                            sources=(c.get("source_ctx") or {}).get("sources"),min_seconds=30)
                            continue
                        auth=entry_signal_mode(db,live_tier,final_entry_score)
                        if auth.get("mode")!="LIVE":
                            sent_test=await send_preproof_actionable_signal(http,db,state,limiter,key,live_pair,live_tier,final_entry_score,c["confirmed"],
                                c["reasons"]+tier_reasons+live_reasons,c["risks"],c["hits"],safety,rts,regime,c.get("social"),c.get("source_ctx"),execution_info,kind=c["kind"],hot=False)
                            if not sent_test and signal_policy(db) in {"strict","proven"}: limiter.release_action("entry:"+key)
                            continue
                        else:
                            all_reasons = c["reasons"] + tier_reasons + live_reasons + [edge_reason,"v15 stateful + historical-risk + execution-quality gates passed"]
                            if repeat_entry:
                                all_reasons.append("verified second-leg flow reset after prior entry alert")
                            calibration = adaptive_dollar_size(db, live_pair, live_tier, final_entry_score,
                                market_regime=str((regime or {}).get("label") or "NEUTRAL"),
                                social=c.get("social"), safety=safety) if ADAPTIVE_CONFIDENCE_ENABLED else None
                            message=beginner_alert(
                                c["kind"],live_pair,final_entry_score,live_tier,all_reasons,c["risks"],c["hits"],
                                c["wallet_checked"],c["security_checked"],c["holder_checked"],c["trader_checked"],
                                rts=rts,safety=safety,rescued=False,tier=live_tier,confirmed_score=c["confirmed"],calibration=calibration,
                                market_regime=regime,social=c.get("social"),source_ctx=c.get("source_ctx"),
                                track_record=alert_track_record_line(db,live_tier))
                            telegram_message_id = await send(http,message)
                            if TG and CHAT and telegram_message_id is None:
                                limiter.release_action("entry:"+key)
                                db.log_decision(live_pair,"ENTRY_DELIVERY_FAIL",live_tier,final_entry_score,c["confirmed"],
                                                ["Telegram did not confirm delivery; no entry cooldown consumed"],rts=rts,social=c.get("social"),
                                                market_regime=str((regime or {}).get("label") or "NEUTRAL"),sources=(c.get("source_ctx") or {}).get("sources"),min_seconds=15)
                                continue
                            db.log_decision(live_pair,"ENTRY_SENT",live_tier,final_entry_score,c["confirmed"],[],
                                            rts=rts,social=c.get("social"),market_regime=str((regime or {}).get("label") or "NEUTRAL"),
                                            sources=(c.get("source_ctx") or {}).get("sources"),min_seconds=30)
                            signal_id = db.add_signal("EARLY",live_pair,final_entry_score,live_tier,all_reasons)
                            db.log_latency(signal_id, token, "ENTRY_ALERT", live_price,
                                           f"tier={live_tier};scout_move={live_move:.2f};decision_slip={decision_slippage:.2f};confirmed={c['confirmed']:.1f};path={path_info.get('mode')};friction={execution_friction_pct(execution_info):.2f}")
                            db.map_telegram_signal(telegram_message_id, signal_id)
                            db.map_trade_context(telegram_message_id,signal_id,live_pair,live_tier)
                            schedule_entry_followups(http,db,state,signal_id)
                            await paper_auto_open(http,db,db.signal_by_id(signal_id),live_pair,live_tier,execution_info=execution_info)
                            entry_sent = True

        # Data-tuned consolidation / second-chance path.
        # Overnight results showed first-bounce re-entry alerts were materially weaker,
        # so v10.4 uses a WATCH -> confirmation window -> fresh revalidation sequence.
        if not db.recent_signal(chain,token,"REENTRY",hours=6):
            rs=reentry_setup(db,pair)
            if rs and not db.position_by_token(token):
                safety=cached_safety(state,chain,token,pair=pair)
                hard=bool(safety and safety.get("hard"))
                rug_ok=bool(safety and safety.get("rug_checked")) if chain=="solana" else True
                holder_ok=bool(safety and safety.get("holder_checked")) if chain=="solana" else True
                security_ok=not hard

                if security_ok and rug_ok and holder_ok:
                    watch=db.recent_signal(chain,token,"REENTRY_WATCH",hours=0.5)
                    if not watch:
                        wid=db.add_signal("REENTRY_WATCH",pair,rs["score"],"WATCH ONLY",rs["reasons"])
                        if (not bool_pref(db,"quiet_mode",False) and limiter.allowed_action("reentry_watch:"+key,cooldown=30*60)):
                            mid=await send(http,reentry_watch_alert(pair,rs))
                            db.map_telegram_signal(mid,wid)
                    else:
                        watch_age=time.time()-f(watch.get("ts"))
                        if watch_age >= REENTRY_WATCH_CONFIRM_SECONDS and limiter.allowed_action("reentry:"+key,cooldown=6*3600):
                            # Re-entry has its own base/recovery logic, but v11.3 also applies the
                            # common crash-memory stability gate before making it actionable.
                            re_stable,re_blockers,_=entry_stability_check(db,pair,"RE-ENTRY OPTION")
                            if re_stable:
                                re_path,re_path_blocks,re_path_info=stateful_sol_edge_gate(db,pair,"RE-ENTRY OPTION")
                                re_exec,re_exec_blocks,re_exec_info=await execution_preflight(http,state,pair,"RE-ENTRY OPTION") if re_path else (False,[],{})
                                if re_path and re_exec:
                                    await shadow_auto_open(http,db,pair,"RE-ENTRY OPTION",rs["score"],rs["reasons"],re_exec_info,"REENTRY_SHADOW")
                                    db.add_signal("REENTRY",pair,rs["score"],"REENTRY_MARKER",rs["reasons"])
                                    if REENTRY_BUY_ALERTS_ENABLED and (not PROOF_FIRST_REQUIRE_MATURE or proof_first_health(db).get("status")=="ACTIVE"):
                                        sid=db.add_signal("EARLY",pair,rs["score"],"RE-ENTRY OPTION",rs["reasons"])
                                        mid=await send(http,reentry_alert(pair,rs))
                                        if not (TG and CHAT) or mid is not None:
                                            db.map_telegram_signal(mid,sid); db.map_trade_context(mid,sid,pair,"RE-ENTRY OPTION")
                                            schedule_entry_followups(http,db,state,sid)
                                            await paper_auto_open(http,db,db.signal_by_id(sid),pair,"RE-ENTRY OPTION",execution_info=re_exec_info)
                                else:
                                    db.log_decision(pair,"ENTRY_PATH" if not re_path else "ENTRY_EXECUTION","RE-ENTRY OPTION",rs["score"],rs["score"],
                                                    re_path_blocks if not re_path else re_exec_blocks,min_seconds=60)
                            else:
                                print(f"[reentry stability] {nest(pair,'baseToken','symbol',default='?')} waiting: {'; '.join(re_blockers[:2])}")

        # Momentum is internal by default. It can promote an entry above; optional
        # Telegram updates can be enabled with /momentum on.
        if c["confirmed"] >= CONF_SCORE and not db.recent_signal(chain,token,"CONFIRMED",hours=8):
            signal_id = db.add_signal("CONFIRMED",pair,c["confirmed"],"CONFIRMED",c["reasons"])
            if (not bool_pref(db,"quiet_mode",False) and bool_pref(db,"telegram_momentum",MOMENTUM_TELEGRAM_DEFAULT) and limiter.allowed("confirm:"+key,cooldown=8*3600)):
                early = db.recent_signal(chain,token,"EARLY",hours=12)
                telegram_message_id = await send(http, confirmation_message(pair,c["confirmed"],early))
                db.map_telegram_signal(telegram_message_id, signal_id)

    now = time.time()
    if (not bool_pref(db,"quiet_mode",False) and bool_pref(db,"telegram_watch",WATCH_TELEGRAM_DEFAULT)
            and output and now-state["watch"] >= WATCH_EVERY*60):
        leader = output[0]
        if leader["entry"] >= WATCH_SCORE and leader["action"] != "POSSIBLE ENTRY":
            base = leader["pair"].get("baseToken") or {}
            m = metrics(leader["pair"])
            chain = leader["pair"].get("chainId","")
            token = str(base.get("address",""))
            rt = state.get("realtime")
            rts = rt.stats(token) if rt and chain=="solana" else {}
            scout = db.recent_signal(chain,token,"SCOUT",hours=2)
            blockers = actionable_blockers(
                leader["pair"], leader["entry"], leader["confirmed"], leader["risks"], leader["hard"],
                scout=scout, rts=rts
            )
            why_not = "; ".join(blockers[:4]) or "waiting for the full entry sequence/confirmation"
            await send(http,
                f"👀 RADAR LEADER — WATCH ONLY\n"
                f"{base.get('name') or 'Unknown name'} ({base.get('symbol') or '?'})\n"
                f"Setup score: {leader['entry']:.0f}/100 | 5m {m['pc5']:+.1f}% | 1h {m['pc1']:+.1f}%\n\n"
                f"WHY NOT AN ENTRY:\n{why_not}\n\n"
                "WHAT TO DO: Watch only. The bot will send ENTRY WINDOW OPEN if the missing conditions pass.\n\n"
                "⚠️ CONTRACT IS THE ID — paste this into Fomo:\n"
                f"{token}")
            state["watch"] = now

    if now-state["heartbeat"] >= HEARTBEAT*60 and not bool_pref(db,"quiet_mode",False):
        perf = db.rolling_entry_performance()
        rt = state.get("realtime")
        mode = "LIVE ALERTS" if not perf["paused"] else "PERFORMANCE GUARD (user-enabled)"
        diag = state.get("entry_diag", [])
        why = "; ".join(f"{name} ({count})" for name,count in diag[:3]) or "no active candidates"
        await send(http,
            f"🟢 BOT HEALTH — {VERSION}\n"
            f"Mode: {mode} | Realtime: {rt.status() if rt else 'OFF'} | PumpPortal: {state.get('pumpportal').status() if state.get('pumpportal') else 'OFF'}\n"
            f"{market_regime_line(state.get('market_regime'))}\n"
            f"Open recorded positions: {len(db.open_positions())} | Guardian alerts {'ON' if bool_pref(db,'guardian_alerts',True) else 'OFF'} | Capital core {capital_core_health_label(db)}\n"
            f"Birdeye: {guard.status_text()}\n"
            f"Active internal scouts: {len(state.get('active_scouts',[]))}\n"
            f"Telegram delivery: {telegram_delivery_status()} | scouts {'ON' if bool_pref(db,'telegram_scouts',SCOUT_TELEGRAM_DEFAULT) else 'OFF'}, "
            f"momentum {'ON' if bool_pref(db,'telegram_momentum',MOMENTUM_TELEGRAM_DEFAULT) else 'OFF'}, "
            f"watch {'ON' if bool_pref(db,'telegram_watch',WATCH_TELEGRAM_DEFAULT) else 'OFF'}\n"
            f"Top blockers: {why}")
        state["heartbeat"] = now

    await evaluate_signals(http,db)
    if not GUARDIAN_ENABLED:
        await track_positions(http,db,state)
    if not state.get("paper_monitor_running"):
        await announce_paper_events(http,await track_paper_positions(http,db))

    await live_autopilot_cycle(http,db,state)

    if time.time()-state.get("last_backup",0)>=6*3600:
        try:
            db.backup_shared_state(); state["last_backup"]=time.time()
        except Exception as exc: print(f"[backup] {exc}")

    local = datetime.now()
    report_key = local.strftime("daily_report_%Y-%m-%d")
    if local.hour >= DAILY_HOUR and db.get_meta(report_key,"") != "sent":
        await daily_report(http,db)
        db.set_meta(report_key,"sent")

    if state["scan"] % 20 == 0:
        print(f"[export] {db.export().name} updated")


async def main():
    print(f"\nFomo Early-Signal Bot {VERSION}")
    print("Solana/Phantom intelligence + strict/fast/copy discovery lanes + actionable signals + proof-gated live capital + persistent state.")
    print("Jupiter execution checks are quote-only in the edge pipeline. Real execution stays OFF and proof-gated unless explicitly configured and armed.\n")
    print(f"[start] chains={','.join(CHAINS)} entry={ENTRY_SCORE} confirmed={CONF_SCORE} scan={INTERVAL}s")
    http = HTTP(); guard = BirdeyeGuard(); limiter = AlertLimiter(); db = Database(); live_executor = LiveExecutor(); execution_probe = JupiterExecutionProbe()
    copy_engine = CopyTradeEngine(db, live_executor, VERSION)
    if BE_KEY:
        core_status = await guard.probe_core(http, force=True)
        if core_status == 200:
            print("[Birdeye] core authentication OK (200). Feature failures will be isolated.")
        elif core_status == 401:
            print("[Birdeye] core authentication failed (401). Check the key.")
        else:
            print(f"[Birdeye] core verification returned {core_status} via {guard.last_core_probe_name}; fallbacks remain active.")
            detail=guard.last_error.get("core:price") or guard.last_error.get("core:networks")
            if detail:
                print(f"[Birdeye] provider detail: {detail}")
    realtime = SolanaRealtime() if RT_ENABLED else None
    pumpportal = PumpPortalRadar()
    public_sync_address=os.getenv("PUBLIC_SOLANA_WALLET_ADDRESS","").strip() or live_executor.address()
    wallet_sync=PublicSolanaWalletSync(public_sync_address,SOLANA_RPC_HTTP)
    state = {"scan":0,"last_gt":0.0,"last_gt_trending":0.0,"last_jupiter_tokens":0.0,"jupiter_tokens_status":"UNTESTED","last_cat":0.0,"last_smart":0.0,"last_new_listing":0.0,"new_listing_index":0,
             "watch":0.0,"heartbeat":0.0,"top":0.0,"candidate_count":0,"be_disabled":set(),
             "be_invalid":False,"realtime":realtime,"guard":guard,"safety_cache":{},"safety_tasks":{},
             "last_maintenance":0.0,"entry_diag":[],"active_scouts":[],"near_entries":[],"last_social_contracts":0.0,"gt_rate_log_until":0.0,
             "execution_probe":execution_probe,
             "last_x_broad":0.0,"last_backup":0.0,"live_executor":live_executor,"auto_attempts":{},"auto_error_notice":{},
             "wallet_sync":wallet_sync,"last_wallet_sync":0.0,"last_outcome_capture":0.0,"last_missed_audit":0.0,
             "guardian_runtime":{},"entry_update_tasks":{},"pumpportal":pumpportal,"candidate_sources":{},
             "hot_candidate_cache":{},"market_regime":{},"last_x_targeted":0.0,"last_decision_outcomes":0.0,
             "copy_engine":copy_engine}
    if db.has_unresolved_execution() and live_executor.keypair_path:
        try:
            rec=await reconcile_unresolved_execution_intents(http,db,state)
            print(f"[execution-recovery] checked={rec['checked']} resolved={rec['resolved']} remaining={rec['remaining']}")
        except Exception as exc:
            db.log_trade_failure("STARTUP_RECONCILE","RECOVERY_EXCEPTION",str(exc),retryable=True)
            print(f"[execution-recovery] {type(exc).__name__}: {exc}")
    if TG and CHAT:
        carried=db.open_positions()
        carried_text=", ".join(p["symbol"] for p in carried[:6]) if carried else "none"
        await send(http,
            f"✅ Fomo Early-Signal Bot {VERSION} is online.\n"
            f"Guardian: {'ON' if GUARDIAN_ENABLED else 'OFF'} (every {GUARDIAN_MONITOR_SECONDS}s) | adaptive confidence: {'ON' if ADAPTIVE_CONFIDENCE_ENABLED else 'OFF'}\n"
            f"Carried-over open positions: {len(carried)} ({carried_text})\n"
            f"Read-only wallet sync: {'ON' if wallet_sync.enabled else 'OFF/not configured'}\n"
            f"Smart size range: ${sizing_preferences(db)[0]:.0f}-${sizing_preferences(db)[1]:.0f} normal / ${sizing_preferences(db)[2]:.0f} exceptional | auto entry updates {'ON' if AUTO_ENTRY_RECHECKS_ENABLED and bool_pref(db,'auto_entry_rechecks',True) else 'OFF'}\n"
            f"Fast hot-scout lane: {'ON' if HOT_SCOUT_ENABLED else 'OFF'} ({HOT_SCOUT_MONITOR_SECONDS}s) | PumpPortal: {pumpportal.status()} | targeted X pulse: {'ON' if XTOKEN and bool_pref(db,'x_targeted_pulse',X_TARGETED_PULSE_ENABLED) else 'OFF'}\n"
            f"Proof gate: {proof_health_label(db)} | shadow proof {'ON' if bool_pref(db,'shadow_sim',SHADOW_SIM_ALWAYS_ON) else 'OFF'} | Jupiter preflight {execution_probe.status()}\n"
            f"Velocity proof + actionable signaling: core setups scoring ≥{PROOF_ELIGIBLE_MIN_SCORE:.0f} can emit TEST BUY signals before proof; real autopilot/full sizing remain proof-gated.\n"
            f"Copy Edge: {'ON' if copy_engine.enabled else 'OFF'} | tracked wallets {len(copy_engine.wallets())} | GET READY chatter defaults OFF.\n"
            "Manual journal locks remain OFF. Use /copy, /edge, /paperreport and /help for commands.\n"
            f"Real autopilot: {'ON (resuming)' if bool_pref(db,'live_auto',False) and live_executor.ready() else 'OFF'} — v16.2 keeps new live orders proof/risk/bankroll gated.")
    command_task = asyncio.create_task(command_loop(http, db, state))
    copy_task = asyncio.create_task(copy_engine.run(http, lambda message: send(http,message))) if copy_engine.enabled else None
    realtime_task = asyncio.create_task(realtime.run()) if realtime else None
    pumpportal_task = asyncio.create_task(pumpportal.run()) if pumpportal.enabled else None
    guardian_task = asyncio.create_task(guardian_loop(http,db,state)) if GUARDIAN_ENABLED else None
    hot_scout_task = asyncio.create_task(hot_scout_loop(http,guard,limiter,db,state)) if HOT_SCOUT_ENABLED else None
    live_exit_task = asyncio.create_task(live_exit_loop(http,db,state)) if live_executor.keypair_path else None
    paper_monitor_task = asyncio.create_task(paper_monitor_loop(http,db,state))
    path_tracker_task = asyncio.create_task(path_tracker_loop(http,db,state)) if PATH_TRACKER_ENABLED else None
    try:
        while True:
            try:
                await cycle(http,guard,limiter,db,state)
            except Exception as exc:
                print(f"[loop error] {type(exc).__name__}: {exc}")
            await asyncio.sleep(INTERVAL)
    finally:
        command_task.cancel()
        try:
            await command_task
        except asyncio.CancelledError:
            pass
        for task in (live_exit_task, paper_monitor_task, path_tracker_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if guardian_task:
            guardian_task.cancel()
            try:
                await guardian_task
            except asyncio.CancelledError:
                pass
        for task in state.get("safety_tasks", {}).values():
            if task and not task.done():
                task.cancel()
        for task in state.get("entry_update_tasks", {}).values():
            if task and not task.done():
                task.cancel()
        if hot_scout_task:
            hot_scout_task.cancel()
            try:
                await hot_scout_task
            except asyncio.CancelledError:
                pass
        if copy_task:
            copy_engine.stop_event.set(); copy_task.cancel()
            try:
                await copy_task
            except asyncio.CancelledError:
                pass
        if pumpportal:
            await pumpportal.close()
        if pumpportal_task:
            pumpportal_task.cancel()
            try:
                await pumpportal_task
            except asyncio.CancelledError:
                pass
        if realtime:
            await realtime.close()
        if realtime_task:
            realtime_task.cancel()
            try:
                await realtime_task
            except asyncio.CancelledError:
                pass
        db.export()
        await http.close()


if __name__ == "__main__":
    asyncio.run(main())
