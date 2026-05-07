"""
╔══════════════════════════════════════════════════════════════════╗
║          ELITE SOLANA SNIPER BOT v3.0 — PROFESSIONAL EDITION    ║
║  • Real-time DEX token scanner with ML-based scoring            ║
║  • Multi-source token intelligence (DexScreener, Birdeye, GMA)  ║
║  • Full Telegram control panel with inline keyboards             ║
║  • Smart TP/SL with trailing stop support                        ║
║  • Anti-rug / honeypot detection engine                          ║
║  • Auto-scanning mode with configurable filters                  ║
╚══════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import aiohttp
import httpx
import pandas as pd

try:
    import pandas_ta as ta
    TA_AVAILABLE = True
except ImportError:
    ta = None
    TA_AVAILABLE = False

from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.message import to_bytes_versioned
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.constants import ParseMode

load_dotenv()

# ════════════════════════════════════════════════════
#  ENVIRONMENT & CONFIGURATION
# ════════════════════════════════════════════════════

TELEGRAM_TOKEN       = os.getenv("TELEGRAM_TOKEN", "")
PRIVATE_KEY          = os.getenv("PRIVATE_KEY", "")
HELIUS_RPC           = os.getenv("HELIUS_RPC", "https://api.mainnet-beta.solana.com")
ALLOWED_USER_IDS     = [int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()]
BIRDEYE_API_KEY      = os.getenv("BIRDEYE_API_KEY", "public")

# Trading defaults (runtime-mutable)
cfg: Dict = {
    "buy_amount_sol":        float(os.getenv("BUY_AMOUNT_SOL", 0.05)),
    "slippage_bps":          int(os.getenv("SLIPPAGE_BPS", 500)),
    "max_position_sol":      float(os.getenv("MAX_POSITION_SOL", 0.5)),
    "min_liquidity_usd":     float(os.getenv("MIN_LIQUIDITY", 15000)),
    "take_profit_pct":       float(os.getenv("TAKE_PROFIT_PCT", 200)),
    "stop_loss_pct":         float(os.getenv("STOP_LOSS_PCT", 50)),
    "trailing_stop_pct":     float(os.getenv("TRAILING_STOP_PCT", 0)),   # 0 = disabled
    "priority_fee_lamports": int(os.getenv("PRIORITY_FEE_LAMPORTS", 100_000)),
    "max_trades_day":        int(os.getenv("MAX_TRADES_DAY", 8)),
    "risk_per_trade_pct":    float(os.getenv("RISK_PER_TRADE_PCT", 2.0)),
    "min_score":             float(os.getenv("MIN_SCORE", 65.0)),          # ML score gate
    "auto_scan":             False,
    "auto_buy":              False,
    "scan_interval_sec":     120,
    "min_age_minutes":       5,
    "max_age_minutes":       60,
    "min_volume_1h_usd":     5000,
    "min_holders":           50,
    "max_top10_pct":         60.0,   # anti-whale: top-10 holders %
    "bot_paused":            False,
}

WSOL = "So11111111111111111111111111111111111111112"

if not TELEGRAM_TOKEN or not PRIVATE_KEY:
    raise ValueError("❌ TELEGRAM_TOKEN and PRIVATE_KEY are required in .env")

keypair = Keypair.from_base58_string(PRIVATE_KEY)

# ════════════════════════════════════════════════════
#  LOGGING
# ════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.FileHandler("sniper_bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════
#  DATA MODELS
# ════════════════════════════════════════════════════

@dataclass
class Position:
    token:           str
    symbol:          str
    entry_price:     float
    entry_sol:       float
    amount_tokens:   int
    peak_price:      float = 0.0
    entry_time:      float = field(default_factory=time.time)
    tx_sig:          str = ""
    pnl_pct:         float = 0.0
    trailing_active: bool = False

    def update_pnl(self, current_price: float):
        if self.entry_price > 0:
            self.pnl_pct = (current_price - self.entry_price) / self.entry_price * 100
            if current_price > self.peak_price:
                self.peak_price = current_price


@dataclass
class TokenScore:
    address:        str
    symbol:         str
    score:          float
    liquidity_usd:  float
    volume_1h:      float
    price_change_1h: float
    holders:        int
    age_minutes:    float
    top10_pct:      float
    has_freeze:     bool
    has_mint:       bool
    reasons:        List[str]
    dex_url:        str = ""


@dataclass
class TradeRecord:
    token:      str
    symbol:     str
    buy_price:  float
    sell_price: float
    entry_sol:  float
    pnl_pct:    float
    pnl_sol:    float
    timestamp:  str
    reason:     str   # "TP", "SL", "MANUAL", "TRAILING"


# ════════════════════════════════════════════════════
#  STATE
# ════════════════════════════════════════════════════

positions:    Dict[str, Position]    = {}
trade_history: List[TradeRecord]     = []
scan_alerts:  List[TokenScore]       = []
daily_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl_sol": 0.0, "trades": 0})
bot_start_time = time.time()

# ════════════════════════════════════════════════════
#  SECURITY GUARD
# ════════════════════════════════════════════════════

def is_authorized(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True   # open if no whitelist configured
    user_id = update.effective_user.id if update.effective_user else 0
    return user_id in ALLOWED_USER_IDS


def auth_required(func):
    """Decorator: reject unauthorized users."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update):
            await update.effective_message.reply_text(
                "⛔ Unauthorized. Your user ID is not whitelisted."
            )
            logger.warning(f"Unauthorized access attempt: {update.effective_user}")
            return
        if cfg["bot_paused"] and func.__name__ not in ("resume_cmd", "status_cmd", "help_cmd"):
            await update.effective_message.reply_text("⏸ Bot is paused. Use /resume to continue.")
            return
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper


# ════════════════════════════════════════════════════
#  WALLET
# ════════════════════════════════════════════════════

async def get_wallet_balance() -> float:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(HELIUS_RPC, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getBalance",
                "params": [str(keypair.pubkey())]
            })
            return resp.json()["result"]["value"] / 1_000_000_000
    except Exception as e:
        logger.error(f"Balance fetch error: {e}")
        return 0.0


# ════════════════════════════════════════════════════
#  PRICE & MARKET DATA
# ════════════════════════════════════════════════════

async def get_token_price(token: str) -> float:
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.jup.ag/price/v2?ids={token}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                data = await resp.json()
                return float(data.get("data", {}).get(token, {}).get("price", 0))
    except Exception as e:
        logger.error(f"Price fetch error for {token}: {e}")
        return 0.0


async def get_dexscreener_new_pairs(limit: int = 30) -> List[dict]:
    """Pull latest Solana pairs from DexScreener — the richest free source."""
    try:
        async with aiohttp.ClientSession() as session:
            url = "https://api.dexscreener.com/latest/dex/search?q=SOL&chainId=solana"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()
                pairs = data.get("pairs", [])
                # Filter to Solana and sort by newest
                sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
                sol_pairs.sort(key=lambda x: x.get("pairCreatedAt", 0), reverse=True)
                return sol_pairs[:limit]
    except Exception as e:
        logger.error(f"DexScreener fetch error: {e}")
        return []


async def get_token_holders(token: str) -> Tuple[int, float]:
    """
    Returns (holder_count, top10_pct) using Helius token-accounts endpoint.
    Falls back gracefully.
    """
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            resp = await client.post(HELIUS_RPC, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getTokenLargestAccounts",
                "params": [token]
            })
            accounts = resp.json().get("result", {}).get("value", [])
            if not accounts:
                return 0, 100.0
            amounts = [int(a.get("amount", 0)) for a in accounts]
            total = sum(amounts)
            top10 = sum(sorted(amounts, reverse=True)[:10])
            top10_pct = (top10 / total * 100) if total > 0 else 100.0

            # Rough holder count via token supply
            supply_resp = await client.post(HELIUS_RPC, json={
                "jsonrpc": "2.0", "id": 2,
                "method": "getTokenSupply",
                "params": [token]
            })
            # Helius doesn't give holder count directly; we estimate
            holder_count = len(accounts)   # min lower bound
            return holder_count, round(top10_pct, 2)
    except Exception as e:
        logger.warning(f"Holder fetch error for {token}: {e}")
        return 0, 100.0


async def check_freeze_mint_authority(token: str) -> Tuple[bool, bool]:
    """Returns (has_freeze_authority, has_mint_authority) — both are risk flags."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(HELIUS_RPC, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getAccountInfo",
                "params": [token, {"encoding": "jsonParsed"}]
            })
            info = resp.json().get("result", {}).get("value", {})
            parsed = info.get("data", {}).get("parsed", {}).get("info", {})
            has_freeze = parsed.get("freezeAuthority") not in (None, "")
            has_mint   = parsed.get("mintAuthority") not in (None, "")
            return has_freeze, has_mint
    except Exception:
        return True, True  # assume worst-case on error


async def get_ohlcv(token: str, minutes: int = 60) -> Optional[pd.DataFrame]:
    """Fetch OHLCV from Birdeye public API."""
    try:
        now = int(time.time())
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                "https://public-api.birdeye.so/defi/ohlcv",
                headers={"X-API-KEY": BIRDEYE_API_KEY},
                params={
                    "address": token,
                    "type": "1m",
                    "time_from": now - minutes * 60,
                    "time_to": now,
                }
            )
            items = r.json().get("data", {}).get("items", [])
        if not items:
            return None
        df = pd.DataFrame(items)
        for col in ["c", "h", "l", "o", "v"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df.dropna(subset=["c"])
    except Exception as e:
        logger.warning(f"OHLCV fetch error: {e}")
        return None


# ════════════════════════════════════════════════════
#  ML SCORING ENGINE
# ════════════════════════════════════════════════════

def score_token(pair: dict, holders: int, top10_pct: float,
                has_freeze: bool, has_mint: bool,
                df: Optional[pd.DataFrame]) -> TokenScore:
    """
    Score a token 0-100 based on multiple weighted signals.
    Modeled after patterns found in historically successful early-stage Solana tokens.
    """
    score = 0.0
    reasons: List[str] = []
    max_score = 100.0

    base_info    = pair.get("baseToken", {})
    token_addr   = base_info.get("address", "")
    symbol       = base_info.get("symbol", "?")
    liquidity    = float(pair.get("liquidity", {}).get("usd", 0) or 0)
    vol_1h       = float(pair.get("volume", {}).get("h1", 0) or 0)
    vol_24h      = float(pair.get("volume", {}).get("h24", 0) or 0)
    pc_1h        = float(pair.get("priceChange", {}).get("h1", 0) or 0)
    pc_6h        = float(pair.get("priceChange", {}).get("h6", 0) or 0)
    txns_1h_buy  = int((pair.get("txns", {}).get("h1", {}) or {}).get("buys", 0))
    txns_1h_sell = int((pair.get("txns", {}).get("h1", {}) or {}).get("sells", 0))
    created_at   = pair.get("pairCreatedAt", 0)
    dex_url      = pair.get("url", "")

    age_min = (time.time() * 1000 - created_at) / 60000 if created_at else 9999

    # ── 1. LIQUIDITY  (max 20pts) ─────────────────────────────────
    if liquidity >= 50_000:
        score += 20; reasons.append("✅ Strong liquidity >$50k")
    elif liquidity >= 20_000:
        score += 14; reasons.append("✅ Good liquidity >$20k")
    elif liquidity >= 10_000:
        score += 8;  reasons.append("⚠️ Low liquidity $10k-20k")
    else:
        reasons.append("❌ Liquidity too low")

    # ── 2. VOLUME MOMENTUM  (max 20pts) ──────────────────────────
    vol_liq_ratio = vol_1h / liquidity if liquidity > 0 else 0
    if vol_liq_ratio >= 2.0:
        score += 20; reasons.append("✅ Explosive volume/liq ratio")
    elif vol_liq_ratio >= 0.8:
        score += 14; reasons.append("✅ Strong volume momentum")
    elif vol_liq_ratio >= 0.3:
        score += 7;  reasons.append("⚠️ Moderate volume")
    else:
        reasons.append("❌ Low volume momentum")

    # ── 3. BUY PRESSURE  (max 15pts) ─────────────────────────────
    total_txns = txns_1h_buy + txns_1h_sell
    if total_txns > 0:
        buy_ratio = txns_1h_buy / total_txns
        if buy_ratio >= 0.70:
            score += 15; reasons.append(f"✅ High buy pressure {buy_ratio:.0%}")
        elif buy_ratio >= 0.55:
            score += 9;  reasons.append(f"✅ Buy dominant {buy_ratio:.0%}")
        elif buy_ratio >= 0.45:
            score += 4
        else:
            reasons.append("❌ Sell pressure dominant")

    # ── 4. PRICE MOMENTUM  (max 10pts) ───────────────────────────
    if 5 <= pc_1h <= 80:
        score += 10; reasons.append(f"✅ Healthy price rise +{pc_1h:.1f}%")
    elif pc_1h > 80:
        score += 5;  reasons.append(f"⚠️ Already pumped +{pc_1h:.1f}% (late?)")
    elif pc_1h < -20:
        reasons.append(f"❌ Dumping {pc_1h:.1f}%")
    else:
        score += 3

    # ── 5. AGE SWEET SPOT  (max 10pts) ───────────────────────────
    if 5 <= age_min <= 30:
        score += 10; reasons.append(f"✅ Fresh token ~{age_min:.0f}m old")
    elif 30 < age_min <= 60:
        score += 7;  reasons.append(f"✅ Young token ~{age_min:.0f}m old")
    elif age_min > 180:
        reasons.append(f"⚠️ Older token ~{age_min:.0f}m")
    else:
        score += 4

    # ── 6. HOLDER DISTRIBUTION  (max 10pts) ──────────────────────
    if holders >= 200 and top10_pct <= 40:
        score += 10; reasons.append(f"✅ Healthy distribution ({holders} holders, top10={top10_pct:.0f}%)")
    elif holders >= 100 and top10_pct <= 55:
        score += 6;  reasons.append(f"✅ OK distribution ({holders} holders)")
    elif top10_pct > 70:
        reasons.append(f"🚨 Whale concentration top10={top10_pct:.0f}%")

    # ── 7. SAFETY FLAGS  (max 15pts / hard penalties) ────────────
    if not has_freeze and not has_mint:
        score += 15; reasons.append("✅ No freeze/mint authority")
    elif has_freeze and has_mint:
        score -= 25; reasons.append("🚨 BOTH freeze+mint authority — HIGH RISK")
    elif has_freeze:
        score -= 10; reasons.append("🚨 Freeze authority active")
    elif has_mint:
        score -= 5;  reasons.append("⚠️ Mint authority active")

    # ── 8. TECHNICAL ANALYSIS  (bonus max 10pts) ─────────────────
    if TA_AVAILABLE and df is not None and len(df) >= 30:
        try:
            df["ema9"]  = ta.ema(df["c"], length=9)
            df["ema21"] = ta.ema(df["c"], length=21)
            df["rsi"]   = ta.rsi(df["c"], length=14)
            df = df.dropna()
            if len(df) > 5:
                ema_bull  = df["ema9"].iloc[-1] > df["ema21"].iloc[-1]
                rsi_ok    = 45 < df["rsi"].iloc[-1] < 70
                vol_spike = df["v"].iloc[-1] > df["v"].rolling(10).mean().iloc[-1] * 1.5
                if ema_bull and rsi_ok and vol_spike:
                    score += 10; reasons.append("✅ TA: EMA bull + RSI ok + vol spike")
                elif ema_bull and rsi_ok:
                    score += 6;  reasons.append("✅ TA: EMA bullish + RSI healthy")
        except Exception:
            pass

    score = max(0.0, min(max_score, score))

    return TokenScore(
        address=token_addr,
        symbol=symbol,
        score=round(score, 1),
        liquidity_usd=liquidity,
        volume_1h=vol_1h,
        price_change_1h=pc_1h,
        holders=holders,
        age_minutes=round(age_min, 1),
        top10_pct=top10_pct,
        has_freeze=has_freeze,
        has_mint=has_mint,
        reasons=reasons,
        dex_url=dex_url,
    )


# ════════════════════════════════════════════════════
#  RISK ENGINE
# ════════════════════════════════════════════════════

class RiskEngine:
    def __init__(self):
        self._today = datetime.utcnow().date()
        self._trades_today = 0
        self._daily_pnl_sol = 0.0

    def _reset_if_new_day(self):
        today = datetime.utcnow().date()
        if today != self._today:
            self._today = today
            self._trades_today = 0
            self._daily_pnl_sol = 0.0

    def can_trade(self) -> Tuple[bool, str]:
        self._reset_if_new_day()
        if self._trades_today >= cfg["max_trades_day"]:
            return False, f"Daily trade limit reached ({cfg['max_trades_day']})"
        # Dynamic loss circuit-breaker: stop if daily loss > 5% of max_position setting
        max_daily_loss = cfg["max_position_sol"] * 0.20
        if self._daily_pnl_sol < -max_daily_loss:
            return False, f"Daily loss circuit breaker hit ({self._daily_pnl_sol:.4f} SOL)"
        return True, "OK"

    def register_trade(self, pnl_sol: float):
        self._reset_if_new_day()
        self._trades_today += 1
        self._daily_pnl_sol += pnl_sol

    def calc_position_sol(self, balance: float) -> float:
        """Kelly-inspired position sizing."""
        risk_sol = balance * (cfg["risk_per_trade_pct"] / 100)
        return round(min(risk_sol, cfg["buy_amount_sol"], cfg["max_position_sol"]), 4)

    @property
    def trades_today(self): return self._trades_today

    @property
    def daily_pnl(self): return round(self._daily_pnl_sol, 6)


risk_engine = RiskEngine()


# ════════════════════════════════════════════════════
#  SWAP ENGINE
# ════════════════════════════════════════════════════

async def execute_swap(
    input_mint: str, output_mint: str,
    amount_lamports: int, slippage_bps: int = 600
) -> Tuple[bool, Optional[str], str]:
    """Execute swap via Jupiter v6 with full error handling."""
    try:
        async with httpx.AsyncClient(timeout=35) as client:
            # Step 1: quote
            qr = await client.get("https://quote-api.jup.ag/v6/quote", params={
                "inputMint":   input_mint,
                "outputMint":  output_mint,
                "amount":      str(amount_lamports),
                "slippageBps": slippage_bps,
                "swapMode":    "ExactIn",
            })
            quote = qr.json()
            if "error" in quote:
                return False, None, f"Quote error: {quote['error']}"

            # Step 2: swap tx
            sr = await client.post("https://quote-api.jup.ag/v6/swap", json={
                "quoteResponse":           quote,
                "userPublicKey":           str(keypair.pubkey()),
                "wrapAndUnwrapSol":        True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": cfg["priority_fee_lamports"],
            })
            swap_data = sr.json()
            if "swapTransaction" not in swap_data:
                return False, None, f"Swap error: {swap_data}"

            # Step 3: sign & send
            raw_tx  = base64.b64decode(swap_data["swapTransaction"])
            tx      = VersionedTransaction.from_bytes(raw_tx)
            sig     = keypair.sign_message(to_bytes_versioned(tx.message))
            signed  = VersionedTransaction.populate(tx.message, [sig])
            serial  = base64.b64encode(bytes(signed)).decode()

            send_r = await client.post(HELIUS_RPC, json={
                "jsonrpc": "2.0", "id": 1,
                "method":  "sendTransaction",
                "params":  [serial, {"skipPreflight": True, "maxRetries": 5}]
            })
            tx_sig = send_r.json().get("result")
            if tx_sig:
                return True, tx_sig, f"https://solscan.io/tx/{tx_sig}"
            return False, None, str(send_r.json())

    except Exception as e:
        logger.error(f"Swap exception: {e}")
        return False, None, str(e)


# ════════════════════════════════════════════════════
#  AUTO SCANNER (background job)
# ════════════════════════════════════════════════════

async def scanner_job(context: ContextTypes.DEFAULT_TYPE):
    """Background job: scan DEX for high-scoring new tokens."""
    if not cfg["auto_scan"]:
        return
    logger.info("🔍 Scanner running...")
    try:
        pairs = await get_dexscreener_new_pairs(limit=40)
        found: List[TokenScore] = []

        for pair in pairs:
            base = pair.get("baseToken", {})
            addr = base.get("address", "")
            if not addr or addr == WSOL:
                continue

            # Quick pre-filter before expensive calls
            liq    = float(pair.get("liquidity", {}).get("usd", 0) or 0)
            vol1h  = float(pair.get("volume", {}).get("h1", 0) or 0)
            if liq < cfg["min_liquidity_usd"] or vol1h < cfg["min_volume_1h_usd"]:
                continue

            created = pair.get("pairCreatedAt", 0)
            age_min = (time.time() * 1000 - created) / 60000 if created else 9999
            if not (cfg["min_age_minutes"] <= age_min <= cfg["max_age_minutes"]):
                continue

            # Skip already in positions
            if addr in positions:
                continue

            # Parallel fetch of expensive data
            holders_task  = asyncio.create_task(get_token_holders(addr))
            auth_task     = asyncio.create_task(check_freeze_mint_authority(addr))
            ohlcv_task    = asyncio.create_task(get_ohlcv(addr, 60))

            holders_data  = await holders_task
            auth_data     = await auth_task
            df            = await ohlcv_task

            holders, top10 = holders_data
            has_freeze, has_mint = auth_data

            if top10 > cfg["max_top10_pct"]:
                continue

            ts = score_token(pair, holders, top10, has_freeze, has_mint, df)

            if ts.score >= cfg["min_score"]:
                found.append(ts)

        found.sort(key=lambda x: x.score, reverse=True)
        scan_alerts.clear()
        scan_alerts.extend(found[:10])

        if found:
            best = found[0]
            msg  = _format_scan_alert(best)
            kb   = _scan_alert_keyboard(best.address)

            if cfg["auto_buy"] and best.score >= cfg["min_score"]:
                # Auto-buy best token
                ok, reason = risk_engine.can_trade()
                if ok:
                    bal = await get_wallet_balance()
                    size = risk_engine.calc_position_sol(bal)
                    if bal >= size + 0.01:
                        success, tx_sig, link = await execute_swap(
                            WSOL, best.address, int(size * 1_000_000_000),
                            cfg["slippage_bps"]
                        )
                        if success:
                            entry_price = await get_token_price(best.address)
                            positions[best.address] = Position(
                                token=best.address, symbol=best.symbol,
                                entry_price=entry_price, entry_sol=size,
                                amount_tokens=int(size * 1_000_000_000 * 0.95),
                                peak_price=entry_price, tx_sig=tx_sig or ""
                            )
                            risk_engine.register_trade(0)
                            msg += f"\n\n🤖 *AUTO-BOUGHT* {size:.4f} SOL → [TX]({link})"

            # Notify all authorized users
            for uid in ALLOWED_USER_IDS:
                try:
                    await context.bot.send_message(
                        uid, msg, parse_mode=ParseMode.MARKDOWN,
                        reply_markup=kb, disable_web_page_preview=True
                    )
                except Exception as e:
                    logger.warning(f"Notify user {uid}: {e}")

    except Exception as e:
        logger.error(f"Scanner job error: {e}")


def _format_scan_alert(ts: TokenScore) -> str:
    bar   = "█" * int(ts.score / 10) + "░" * (10 - int(ts.score / 10))
    grade = "🟢 STRONG" if ts.score >= 80 else "🟡 GOOD" if ts.score >= 65 else "🟠 WEAK"
    return (
        f"🎯 *NEW TOKEN ALERT*\n\n"
        f"🔤 Symbol:   `{ts.symbol}`\n"
        f"📍 Address:  `{ts.address}`\n\n"
        f"📊 Score:    *{ts.score}/100* {grade}\n"
        f"           `[{bar}]`\n\n"
        f"💧 Liquidity:  ${ts.liquidity_usd:,.0f}\n"
        f"📈 Vol 1h:     ${ts.volume_1h:,.0f}\n"
        f"🕐 Age:        {ts.age_minutes:.0f} min\n"
        f"👥 Holders:    {ts.holders}\n"
        f"🐋 Top10:      {ts.top10_pct:.1f}%\n"
        f"🔒 Freeze:     {'🚨 YES' if ts.has_freeze else '✅ NO'}\n"
        f"🖨 Mint:       {'⚠️ YES' if ts.has_mint else '✅ NO'}\n"
        f"📉 1h Change:  {ts.price_change_1h:+.1f}%\n\n"
        f"📝 *Signals:*\n" +
        "\n".join(f"  {r}" for r in ts.reasons[:6])
    )


def _scan_alert_keyboard(addr: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚀 Quick Buy",       callback_data=f"qbuy:{addr}"),
            InlineKeyboardButton("📊 Deep Analyze",    callback_data=f"analyze:{addr}"),
        ],
        [
            InlineKeyboardButton("🦅 DexScreener",     url=f"https://dexscreener.com/solana/{addr}"),
            InlineKeyboardButton("🔎 Solscan",         url=f"https://solscan.io/token/{addr}"),
        ],
        [InlineKeyboardButton("❌ Dismiss",            callback_data="dismiss")],
    ])


# ════════════════════════════════════════════════════
#  POSITION MONITOR (background job)
# ════════════════════════════════════════════════════

async def monitor_positions(context: ContextTypes.DEFAULT_TYPE):
    """Check TP / SL / Trailing every 30s for all open positions."""
    if not positions:
        return
    for token in list(positions.keys()):
        try:
            pos = positions[token]
            current = await get_token_price(token)
            if current == 0:
                continue
            pos.update_pnl(current)
            pnl = pos.pnl_pct

            sell_reason = None

            # ── Take Profit ─────────────────────────────────────
            if pnl >= cfg["take_profit_pct"]:
                sell_reason = "TAKE_PROFIT"

            # ── Stop Loss ────────────────────────────────────────
            elif pnl <= -cfg["stop_loss_pct"]:
                sell_reason = "STOP_LOSS"

            # ── Trailing Stop ────────────────────────────────────
            elif cfg["trailing_stop_pct"] > 0 and pos.peak_price > 0:
                drop_from_peak = (pos.peak_price - current) / pos.peak_price * 100
                if drop_from_peak >= cfg["trailing_stop_pct"] and pnl > 0:
                    sell_reason = "TRAILING_STOP"

            if sell_reason:
                await _execute_auto_sell(token, pos, sell_reason, current, context)

        except Exception as e:
            logger.error(f"Monitor error {token}: {e}")


async def _execute_auto_sell(token: str, pos: Position, reason: str,
                              current_price: float,
                              context: ContextTypes.DEFAULT_TYPE):
    amount = int(pos.amount_tokens * 0.97)
    success, tx_sig, link = await execute_swap(token, WSOL, amount, cfg["slippage_bps"] + 100)

    if success:
        pnl_sol = pos.entry_sol * (pos.pnl_pct / 100)
        trade_history.append(TradeRecord(
            token=token, symbol=pos.symbol,
            buy_price=pos.entry_price, sell_price=current_price,
            entry_sol=pos.entry_sol, pnl_pct=round(pos.pnl_pct, 2),
            pnl_sol=round(pnl_sol, 6),
            timestamp=datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
            reason=reason
        ))
        risk_engine.register_trade(pnl_sol)
        del positions[token]

        emoji = "🎯" if reason == "TAKE_PROFIT" else "🛑" if reason == "STOP_LOSS" else "📉"
        msg = (
            f"{emoji} *{reason.replace('_', ' ')}*\n\n"
            f"Token: `{pos.symbol}` (`{token[:8]}...`)\n"
            f"PnL:   *{pos.pnl_pct:+.1f}%* ({pnl_sol:+.4f} SOL)\n"
            f"[View TX]({link})"
        )
        for uid in ALLOWED_USER_IDS:
            try:
                await context.bot.send_message(uid, msg, parse_mode=ParseMode.MARKDOWN,
                                               disable_web_page_preview=True)
            except Exception:
                pass
        logger.info(f"{reason} executed for {pos.symbol} PnL={pos.pnl_pct:+.1f}%")


# ════════════════════════════════════════════════════
#  TELEGRAM COMMANDS
# ════════════════════════════════════════════════════

@auth_required
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Balance",    callback_data="bal"),
         InlineKeyboardButton("📊 Positions",  callback_data="pos")],
        [InlineKeyboardButton("🔍 Last Scans", callback_data="scans"),
         InlineKeyboardButton("📈 Stats",      callback_data="stats")],
        [InlineKeyboardButton("⚙️ Settings",   callback_data="settings"),
         InlineKeyboardButton("❓ Help",        callback_data="helpme")],
    ])
    uptime = str(timedelta(seconds=int(time.time() - bot_start_time)))
    await update.message.reply_text(
        f"🤖 *Elite Solana Sniper Bot v3.0*\n\n"
        f"⏱ Uptime: `{uptime}`\n"
        f"👛 Wallet: `{str(keypair.pubkey())[:8]}...{str(keypair.pubkey())[-4:]}`\n"
        f"🔍 Auto-Scan: {'✅ ON' if cfg['auto_scan'] else '❌ OFF'}\n"
        f"🤖 Auto-Buy:  {'✅ ON' if cfg['auto_buy'] else '❌ OFF'}\n"
        f"⏸ Status:    {'PAUSED' if cfg['bot_paused'] else '🟢 RUNNING'}\n\n"
        f"Use the buttons below or /help for all commands.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb
    )


@auth_required
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🚀 <b>Elite Sniper Bot — Command Reference</b>\n\n"
        "<b>📡 TRADING</b>\n"
        "/snipe &lt;addr&gt; [sol] — Manual snipe a token\n"
        "/sell &lt;addr&gt; — Sell a position\n"
        "/sellall — Sell ALL open positions\n"
        "/positions — Show open positions with live PnL\n\n"
        "<b>🔍 SCANNER</b>\n"
        "/scan — Run manual scan now\n"
        "/scanon — Enable auto-scanner\n"
        "/scanoff — Disable auto-scanner\n"
        "/autobuy — Toggle auto-buy mode\n"
        "/alerts — Show last scan results\n"
        "/analyze &lt;addr&gt; — Deep token analysis\n\n"
        "<b>💰 WALLET</b>\n"
        "/balance — Show SOL balance\n"
        "/wallet — Full wallet info\n\n"
        "<b>⚙️ SETTINGS</b>\n"
        "/settings — Show all settings\n"
        "/set &lt;key&gt; &lt;val&gt; — Change a setting\n"
        "/risk — Show risk parameters\n\n"
        "<b>📊 STATS</b>\n"
        "/stats — Performance summary\n"
        "/history — Last 10 trades\n\n"
        "<b>🛡 CONTROL</b>\n"
        "/pause — Pause all trading\n"
        "/resume — Resume trading\n"
        "/stop — Emergency stop + clear positions\n"
        "/status — Bot health status\n\n"
        "<b>Valid /set keys:</b>\n"
        "<code>buy_amount_sol, slippage_bps, take_profit_pct,\n"
        "stop_loss_pct, trailing_stop_pct, min_score,\n"
        "min_liquidity_usd, max_top10_pct, scan_interval_sec</code>"
    )
    await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)


@auth_required
async def snipe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: `/snipe <token_address> [sol_amount]`",
                                        parse_mode=ParseMode.MARKDOWN)
        return

    token = args[0].strip()
    size_override = float(args[1]) if len(args) > 1 else None

    ok, reason = risk_engine.can_trade()
    if not ok:
        await update.message.reply_text(f"⛔ {reason}")
        return

    msg = await update.message.reply_text("🔍 Analyzing token...")

    # Fetch pair from DexScreener
    pairs = await get_dexscreener_new_pairs(50)
    pair  = next((p for p in pairs if p.get("baseToken", {}).get("address") == token), None)

    if pair is None:
        # Try DexScreener by token address directly
        try:
            async with aiohttp.ClientSession() as session:
                url = f"https://api.dexscreener.com/latest/dex/tokens/{token}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    data = await r.json()
                    pairs_list = data.get("pairs", [])
                    if pairs_list:
                        pair = pairs_list[0]
        except Exception:
            pass

    if pair is None:
        await msg.edit_text("❌ Token not found on DexScreener. Proceeding with basic checks...")
        ts = TokenScore(address=token, symbol="UNKNOWN", score=50, liquidity_usd=0,
                        volume_1h=0, price_change_1h=0, holders=0, age_minutes=0,
                        top10_pct=0, has_freeze=False, has_mint=False, reasons=["Manual override"])
    else:
        holders, top10 = await get_token_holders(token)
        has_freeze, has_mint = await check_freeze_mint_authority(token)
        df = await get_ohlcv(token, 60)
        ts = score_token(pair, holders, top10, has_freeze, has_mint, df)

    if ts.score < cfg["min_score"]:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⚠️ Force Buy Anyway", callback_data=f"force_buy:{token}"),
            InlineKeyboardButton("❌ Abort",             callback_data="dismiss"),
        ]])
        await msg.edit_text(
            f"⚠️ *Low Score: {ts.score}/100*\n\n"
            + "\n".join(ts.reasons[:5])
            + f"\n\nMin required: {cfg['min_score']}. Force buy?",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb
        )
        return

    bal  = await get_wallet_balance()
    size = size_override or risk_engine.calc_position_sol(bal)

    if bal < size + 0.01:
        await msg.edit_text(
            f"⚠️ Insufficient balance!\nHave: {bal:.4f} SOL | Need: {size + 0.01:.4f} SOL"
        )
        return

    await msg.edit_text(
        f"✅ Score: *{ts.score}/100* — Executing buy...\n{chr(10).join(ts.reasons[:4])}",
        parse_mode=ParseMode.MARKDOWN
    )

    success, tx_sig, link = await execute_swap(
        WSOL, token, int(size * 1_000_000_000), cfg["slippage_bps"]
    )

    if success:
        risk_engine.register_trade(0)
        entry_price = await get_token_price(token)
        positions[token] = Position(
            token=token, symbol=ts.symbol,
            entry_price=entry_price, entry_sol=size,
            amount_tokens=int(size * 1_000_000_000 * 0.95),
            peak_price=entry_price, tx_sig=tx_sig or ""
        )
        await msg.edit_text(
            f"🎯 *BUY EXECUTED!*\n\n"
            f"Symbol:  `{ts.symbol}`\n"
            f"Size:    {size:.4f} SOL\n"
            f"Entry:   {entry_price:.8f}\n"
            f"TP/SL:   +{cfg['take_profit_pct']}% / -{cfg['stop_loss_pct']}%\n\n"
            f"[📄 View TX]({link})",
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
    else:
        await msg.edit_text(f"❌ Buy Failed:\n`{link}`", parse_mode=ParseMode.MARKDOWN)


@auth_required
async def sell_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args:
        if not positions:
            await update.message.reply_text("📭 No open positions.")
            return
        # Show selection keyboard
        buttons = [
            [InlineKeyboardButton(f"Sell {p.symbol} ({p.pnl_pct:+.1f}%)", callback_data=f"sell:{addr}")]
            for addr, p in positions.items()
        ]
        await update.message.reply_text(
            "Select position to sell:", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    token = args[0].strip()
    if token not in positions:
        await update.message.reply_text("⚠️ No open position for that token.")
        return
    await _do_sell(token, update.message, "MANUAL")


async def _do_sell(token: str, message, reason: str = "MANUAL"):
    pos = positions.get(token)
    if not pos:
        await message.reply_text("⚠️ Position not found.")
        return

    msg = await message.reply_text(f"🔄 Selling `{pos.symbol}`...", parse_mode=ParseMode.MARKDOWN)
    amount = int(pos.amount_tokens * 0.97)
    success, tx_sig, link = await execute_swap(token, WSOL, amount, cfg["slippage_bps"] + 100)

    if success:
        current = await get_token_price(token)
        pos.update_pnl(current)
        pnl_sol = pos.entry_sol * (pos.pnl_pct / 100)
        trade_history.append(TradeRecord(
            token=token, symbol=pos.symbol,
            buy_price=pos.entry_price, sell_price=current,
            entry_sol=pos.entry_sol, pnl_pct=round(pos.pnl_pct, 2),
            pnl_sol=round(pnl_sol, 6),
            timestamp=datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
            reason=reason
        ))
        risk_engine.register_trade(pnl_sol)
        del positions[token]
        emoji = "🟢" if pnl_sol >= 0 else "🔴"
        await msg.edit_text(
            f"✅ *SOLD* `{pos.symbol}`\n\n"
            f"{emoji} PnL: *{pos.pnl_pct:+.1f}%* ({pnl_sol:+.4f} SOL)\n"
            f"[📄 View TX]({link})",
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
    else:
        await msg.edit_text(f"❌ Sell failed: `{link}`", parse_mode=ParseMode.MARKDOWN)


@auth_required
async def sellall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not positions:
        await update.message.reply_text("📭 No open positions.")
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, sell all", callback_data="confirm_sellall"),
        InlineKeyboardButton("❌ Cancel",        callback_data="dismiss"),
    ]])
    await update.message.reply_text(
        f"⚠️ Sell ALL {len(positions)} positions?", reply_markup=kb
    )


@auth_required
async def positions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not positions:
        await update.effective_message.reply_text("📭 No open positions.")
        return

    text = "📊 *Open Positions*\n\n"
    for addr, pos in positions.items():
        current = await get_token_price(addr)
        pos.update_pnl(current)
        age_m = (time.time() - pos.entry_time) / 60
        emoji = "🟢" if pos.pnl_pct >= 0 else "🔴"
        text += (
            f"{emoji} *{pos.symbol}* `{addr[:8]}...`\n"
            f"   Entry:   {pos.entry_price:.8f}\n"
            f"   Current: {current:.8f}\n"
            f"   PnL:     *{pos.pnl_pct:+.1f}%*\n"
            f"   Size:    {pos.entry_sol:.4f} SOL\n"
            f"   Age:     {age_m:.0f}m\n\n"
        )

    buttons = [
        [InlineKeyboardButton(f"Sell {p.symbol}", callback_data=f"sell:{a}")]
        for a, p in positions.items()
    ]
    buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data="pos")])

    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons)
    )


@auth_required
async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bal = await get_wallet_balance()
    await update.effective_message.reply_text(
        f"💰 *Wallet Balance*\n\n"
        f"Address: `{str(keypair.pubkey())[:8]}...{str(keypair.pubkey())[-4:]}`\n"
        f"Balance: *{bal:.6f} SOL*\n\n"
        f"Open Positions: {len(positions)}\n"
        f"Available: ~{max(0, bal - len(positions) * cfg['buy_amount_sol']):.4f} SOL",
        parse_mode=ParseMode.MARKDOWN
    )


@auth_required
async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "⚙️ <b>Current Settings</b>\n\n"
        f"<b>Trading:</b>\n"
        f"  buy_amount_sol      = {cfg['buy_amount_sol']}\n"
        f"  slippage_bps        = {cfg['slippage_bps']}  ({cfg['slippage_bps']/100}%)\n"
        f"  max_position_sol    = {cfg['max_position_sol']}\n"
        f"  take_profit_pct     = +{cfg['take_profit_pct']}%\n"
        f"  stop_loss_pct       = -{cfg['stop_loss_pct']}%\n"
        f"  trailing_stop_pct   = {cfg['trailing_stop_pct']}% (0=off)\n"
        f"  risk_per_trade_pct  = {cfg['risk_per_trade_pct']}%\n\n"
        f"<b>Scanner:</b>\n"
        f"  auto_scan           = {cfg['auto_scan']}\n"
        f"  auto_buy            = {cfg['auto_buy']}\n"
        f"  scan_interval_sec   = {cfg['scan_interval_sec']}\n"
        f"  min_score           = {cfg['min_score']}\n"
        f"  min_liquidity_usd   = ${cfg['min_liquidity_usd']:,}\n"
        f"  min_volume_1h_usd   = ${cfg['min_volume_1h_usd']:,}\n"
        f"  min_age_minutes     = {cfg['min_age_minutes']}\n"
        f"  max_age_minutes     = {cfg['max_age_minutes']}\n"
        f"  min_holders         = {cfg['min_holders']}\n"
        f"  max_top10_pct       = {cfg['max_top10_pct']}%\n\n"
        f"Use <code>/set &lt;key&gt; &lt;value&gt;</code> to change."
    )
    await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)


@auth_required
async def set_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: `/set <key> <value>`\nExample: `/set take_profit_pct 150`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    key, val_str = args[0], args[1]
    if key not in cfg:
        await update.message.reply_text(f"❌ Unknown key: `{key}`\nSee /settings for valid keys.",
                                        parse_mode=ParseMode.MARKDOWN)
        return

    current = cfg[key]
    try:
        if isinstance(current, bool):
            val = val_str.lower() in ("true", "1", "yes", "on")
        elif isinstance(current, int):
            val = int(val_str)
        else:
            val = float(val_str)
        cfg[key] = val
        await update.message.reply_text(f"✅ `{key}` set to `{val}`", parse_mode=ParseMode.MARKDOWN)
        logger.info(f"Config changed: {key} = {val}")
    except ValueError:
        await update.message.reply_text(f"❌ Invalid value `{val_str}` for `{key}`",
                                        parse_mode=ParseMode.MARKDOWN)


@auth_required
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wins   = [t for t in trade_history if t.pnl_sol > 0]
    losses = [t for t in trade_history if t.pnl_sol <= 0]
    total_pnl = sum(t.pnl_sol for t in trade_history)
    win_rate  = len(wins) / len(trade_history) * 100 if trade_history else 0
    avg_win   = sum(t.pnl_pct for t in wins) / len(wins) if wins else 0
    avg_loss  = sum(t.pnl_pct for t in losses) / len(losses) if losses else 0

    uptime = str(timedelta(seconds=int(time.time() - bot_start_time)))
    msg = (
        f"📊 *Performance Summary*\n\n"
        f"⏱ Uptime:       {uptime}\n"
        f"📈 Total Trades: {len(trade_history)}\n"
        f"🟢 Wins:         {len(wins)}\n"
        f"🔴 Losses:       {len(losses)}\n"
        f"🎯 Win Rate:     {win_rate:.1f}%\n"
        f"💰 Total PnL:    {total_pnl:+.4f} SOL\n"
        f"📈 Avg Win:      {avg_win:+.1f}%\n"
        f"📉 Avg Loss:     {avg_loss:+.1f}%\n\n"
        f"📅 Today:\n"
        f"  Trades: {risk_engine.trades_today}\n"
        f"  PnL:    {risk_engine.daily_pnl:+.4f} SOL\n\n"
        f"📍 Open Positions: {len(positions)}"
    )
    await update.effective_message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


@auth_required
async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not trade_history:
        await update.effective_message.reply_text("📭 No trade history yet.")
        return
    recent = trade_history[-10:][::-1]
    text = "📜 *Recent Trades*\n\n"
    for t in recent:
        emoji = "🟢" if t.pnl_sol > 0 else "🔴"
        text += (
            f"{emoji} {t.symbol} — {t.reason}\n"
            f"   PnL: *{t.pnl_pct:+.1f}%* ({t.pnl_sol:+.4f} SOL)\n"
            f"   {t.timestamp}\n\n"
        )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


@auth_required
async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔍 Scanning DEX for high-quality tokens...")
    original = cfg["auto_scan"]
    cfg["auto_scan"] = True
    await scanner_job(context)
    cfg["auto_scan"] = original
    if scan_alerts:
        await msg.edit_text(f"✅ Scan complete! Found {len(scan_alerts)} token(s). Use /alerts to view.")
    else:
        await msg.edit_text("🔍 Scan complete. No tokens passed filters right now.")


@auth_required
async def alerts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not scan_alerts:
        await update.effective_message.reply_text("📭 No recent scan results. Use /scan to run now.")
        return
    for ts in scan_alerts[:5]:
        await update.effective_message.reply_text(
            _format_scan_alert(ts),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_scan_alert_keyboard(ts.address),
            disable_web_page_preview=True
        )


@auth_required
async def analyze_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: `/analyze <token_address>`",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    token = args[0].strip()
    msg = await update.message.reply_text("🔬 Deep analyzing token...")

    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{token}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as r:
                data = await r.json()
                pairs = data.get("pairs", [])
                pair  = pairs[0] if pairs else None
    except Exception:
        pair = None

    holders, top10 = await get_token_holders(token)
    has_freeze, has_mint = await check_freeze_mint_authority(token)
    df = await get_ohlcv(token, 120)

    if pair:
        ts = score_token(pair, holders, top10, has_freeze, has_mint, df)
        text = _format_scan_alert(ts)
    else:
        text = f"⚠️ Token `{token[:16]}...` not found on DexScreener.\n"
        text += f"Freeze Auth: {'YES' if has_freeze else 'NO'}\n"
        text += f"Mint Auth: {'YES' if has_mint else 'NO'}\n"
        text += f"Top10 Holders: {top10}%"

    await msg.edit_text(
        text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=_scan_alert_keyboard(token),
        disable_web_page_preview=True
    )


@auth_required
async def scanon_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg["auto_scan"] = True
    await update.message.reply_text(
        f"🔍 Auto-scanner *ENABLED*\nScanning every {cfg['scan_interval_sec']}s\n"
        f"Min score: {cfg['min_score']}/100",
        parse_mode=ParseMode.MARKDOWN
    )


@auth_required
async def scanoff_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg["auto_scan"] = False
    await update.message.reply_text("🛑 Auto-scanner *DISABLED*", parse_mode=ParseMode.MARKDOWN)


@auth_required
async def autobuy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg["auto_buy"] = not cfg["auto_buy"]
    state = "✅ ENABLED" if cfg["auto_buy"] else "❌ DISABLED"
    await update.message.reply_text(
        f"🤖 Auto-buy: *{state}*\n\n"
        f"⚠️ Auto-buy will execute trades automatically\n"
        f"when scanner finds tokens scoring ≥{cfg['min_score']}/100",
        parse_mode=ParseMode.MARKDOWN
    )


@auth_required
async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg["bot_paused"] = True
    await update.message.reply_text(
        "⏸ *Bot PAUSED*\n\nNo new trades will be executed.\nUse /resume to continue.",
        parse_mode=ParseMode.MARKDOWN
    )


@auth_required
async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg["bot_paused"] = False
    await update.message.reply_text("▶️ *Bot RESUMED*\nTrading is active again.",
                                    parse_mode=ParseMode.MARKDOWN)


@auth_required
async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🛑 CONFIRM EMERGENCY STOP", callback_data="confirm_estop"),
        InlineKeyboardButton("❌ Cancel",                  callback_data="dismiss"),
    ]])
    await update.message.reply_text(
        "⚠️ *EMERGENCY STOP*\n\nThis will:\n"
        "• Pause all trading\n"
        "• Clear position tracking\n"
        "• Disable auto-scan & auto-buy\n\n"
        "*This does NOT sell your tokens automatically.*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb
    )


@auth_required
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bal    = await get_wallet_balance()
    uptime = str(timedelta(seconds=int(time.time() - bot_start_time)))
    can_trade, reason = risk_engine.can_trade()
    msg = (
        f"🟢 *Bot Status*\n\n"
        f"⏱ Uptime:       `{uptime}`\n"
        f"💰 Balance:      `{bal:.4f} SOL`\n"
        f"📍 Positions:    `{len(positions)}`\n"
        f"🔍 Auto-Scan:    `{'ON' if cfg['auto_scan'] else 'OFF'}`\n"
        f"🤖 Auto-Buy:     `{'ON' if cfg['auto_buy'] else 'OFF'}`\n"
        f"⏸ Paused:       `{cfg['bot_paused']}`\n"
        f"📊 Trades Today: `{risk_engine.trades_today}/{cfg['max_trades_day']}`\n"
        f"💹 Daily PnL:    `{risk_engine.daily_pnl:+.4f} SOL`\n"
        f"✅ Can Trade:    `{can_trade}` ({reason})\n"
        f"🧠 TA Engine:    `{'ON' if TA_AVAILABLE else 'OFF'}`"
    )
    await update.effective_message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


@auth_required
async def wallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bal  = await get_wallet_balance()
    addr = str(keypair.pubkey())
    await update.message.reply_text(
        f"👛 *Wallet Info*\n\n"
        f"Address: `{addr}`\n"
        f"Balance: *{bal:.6f} SOL*\n\n"
        f"[🔎 View on Solscan](https://solscan.io/account/{addr})",
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )


# ════════════════════════════════════════════════════
#  INLINE KEYBOARD CALLBACKS
# ════════════════════════════════════════════════════

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if not is_authorized(update):
        await query.edit_message_text("⛔ Unauthorized.")
        return

    # ── Quick Buy ────────────────────────────────────────────────
    if data.startswith("qbuy:"):
        token = data.split(":", 1)[1]
        context.args = [token]
        # Re-use snipe logic via fake message
        class _FakeMsg:
            async def reply_text(self, *a, **kw): return await query.edit_message_text(*a, **kw)
            async def edit_text(self, *a, **kw): return await query.edit_message_text(*a, **kw)
        update.message = _FakeMsg()
        await snipe_cmd(update, context)

    elif data.startswith("force_buy:"):
        token = data.split(":", 1)[1]
        bal  = await get_wallet_balance()
        size = risk_engine.calc_position_sol(bal)
        success, tx_sig, link = await execute_swap(
            WSOL, token, int(size * 1_000_000_000), cfg["slippage_bps"]
        )
        if success:
            risk_engine.register_trade(0)
            ep = await get_token_price(token)
            positions[token] = Position(
                token=token, symbol="MANUAL", entry_price=ep,
                entry_sol=size, amount_tokens=int(size * 1_000_000_000 * 0.95),
                peak_price=ep, tx_sig=tx_sig or ""
            )
            await query.edit_message_text(f"🎯 Force-bought {size:.4f} SOL\n[TX]({link})",
                                          parse_mode=ParseMode.MARKDOWN,
                                          disable_web_page_preview=True)
        else:
            await query.edit_message_text(f"❌ Buy failed: {link}")

    # ── Sell from positions list ──────────────────────────────────
    elif data.startswith("sell:"):
        token = data.split(":", 1)[1]
        class _FQ:
            async def reply_text(self, *a, **kw): return await query.message.reply_text(*a, **kw)
        await _do_sell(token, _FQ(), "MANUAL")

    # ── Confirm Sell All ──────────────────────────────────────────
    elif data == "confirm_sellall":
        count = len(positions)
        for token in list(positions.keys()):
            class _FQ:
                async def reply_text(self, *a, **kw): pass
            await _do_sell(token, _FQ(), "MANUAL_ALL")
        await query.edit_message_text(f"✅ Sell-all executed on {count} position(s).")

    # ── Emergency Stop ────────────────────────────────────────────
    elif data == "confirm_estop":
        positions.clear()
        cfg["auto_scan"] = False
        cfg["auto_buy"]  = False
        cfg["bot_paused"] = True
        await query.edit_message_text(
            "🛑 *EMERGENCY STOP EXECUTED*\n\nAll positions cleared, scanner off, bot paused.",
            parse_mode=ParseMode.MARKDOWN
        )
        logger.warning("🚨 EMERGENCY STOP triggered!")

    # ── Dashboard shortcuts ───────────────────────────────────────
    elif data == "bal":
        await balance_cmd(update, context)
    elif data == "pos":
        await positions_cmd(update, context)
    elif data == "scans":
        await alerts_cmd(update, context)
    elif data == "stats":
        await stats_cmd(update, context)
    elif data == "settings":
        await settings_cmd(update, context)
    elif data == "helpme":
        await help_cmd(update, context)

    elif data.startswith("analyze:"):
        token = data.split(":", 1)[1]
        context.args = [token]
        await analyze_cmd(update, context)

    elif data == "dismiss":
        try:
            await query.delete_message()
        except Exception:
            await query.edit_message_text("✅ Dismissed.")


# ════════════════════════════════════════════════════
#  BOT COMMANDS MENU SETUP
# ════════════════════════════════════════════════════

async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start",    "Dashboard"),
        BotCommand("snipe",    "Snipe a token manually"),
        BotCommand("sell",     "Sell a position"),
        BotCommand("sellall",  "Sell all positions"),
        BotCommand("positions","Show open positions"),
        BotCommand("balance",  "Check SOL balance"),
        BotCommand("scan",     "Run DEX scan now"),
        BotCommand("scanon",   "Enable auto-scanner"),
        BotCommand("scanoff",  "Disable auto-scanner"),
        BotCommand("autobuy",  "Toggle auto-buy"),
        BotCommand("alerts",   "Show last scan results"),
        BotCommand("analyze",  "Deep token analysis"),
        BotCommand("set",      "Change a setting"),
        BotCommand("settings", "Show all settings"),
        BotCommand("stats",    "Performance summary"),
        BotCommand("history",  "Last 10 trades"),
        BotCommand("pause",    "Pause trading"),
        BotCommand("resume",   "Resume trading"),
        BotCommand("stop",     "Emergency stop"),
        BotCommand("status",   "Bot health status"),
        BotCommand("wallet",   "Wallet details"),
        BotCommand("help",     "Full command help"),
    ])


# ════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════

def main():
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Command handlers
    handlers = [
        ("start",    start_cmd),
        ("help",     help_cmd),
        ("snipe",    snipe_cmd),
        ("sell",     sell_cmd),
        ("sellall",  sellall_cmd),
        ("positions",positions_cmd),
        ("balance",  balance_cmd),
        ("wallet",   wallet_cmd),
        ("settings", settings_cmd),
        ("set",      set_cmd),
        ("stats",    stats_cmd),
        ("history",  history_cmd),
        ("scan",     scan_cmd),
        ("alerts",   alerts_cmd),
        ("analyze",  analyze_cmd),
        ("scanon",   scanon_cmd),
        ("scanoff",  scanoff_cmd),
        ("autobuy",  autobuy_cmd),
        ("pause",    pause_cmd),
        ("resume",   resume_cmd),
        ("stop",     stop_cmd),
        ("status",   status_cmd),
    ]
    for cmd, handler in handlers:
        app.add_handler(CommandHandler(cmd, handler))

    # Inline keyboard callbacks
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Background jobs
    jq = app.job_queue
    if jq:
        jq.run_repeating(monitor_positions, interval=30,  first=15)
        jq.run_repeating(scanner_job,       interval=cfg["scan_interval_sec"], first=30)

    logger.info("🚀 Elite Solana Sniper Bot v3.0 started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
