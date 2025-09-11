import os
import asyncio
import logging
import random
from typing import Dict, List, Tuple, Optional
from time import time

from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes,
    CallbackQueryHandler, filters,
)
from telegram.error import BadRequest
import aiohttp

# ---------- базовая настройка ----------
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Хранилище триггеров: watches[chat_id] = {"above": [...], "below": [...]}
watches: Dict[int, Dict[str, List[float]]] = {}

# ---------- параметры (ENV-переменные) ----------
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "60"))   # период фоновой проверки
PRICE_TTL_SEC     = int(os.getenv("PRICE_TTL_SEC", "30"))       # валидность кэша
V3_COOLDOWN_SEC   = int(os.getenv("V3_COOLDOWN_SEC", "20"))
V2_COOLDOWN_SEC   = int(os.getenv("V2_COOLDOWN_SEC", "60"))
GECKO_COOLDOWN_SEC= int(os.getenv("GECKO_COOLDOWN_SEC", "120"))

# ---------- источники цен ----------
# v3 (The Graph / PancakeSwap Exchange v3, BSC)
GRAPH_API_KEY = os.getenv("GRAPH_API_KEY")  # если пусто — будет фоллбек
GRAPH_SUBGRAPH_ID = os.getenv(
    "GRAPH_SUBGRAPH_ID",
    "Hv1GncLY5docZoGtXjo4kwbTvxm3MAhVZqBZE4sUT9eZ"
)
GRAPH_URL = f"https://gateway.thegraph.com/api/subgraphs/id/{GRAPH_SUBGRAPH_ID}"

# v2 (Pancake Info API)
PANCAKE_API = os.getenv("PANCAKE_API", "https://api.pancakeswap.info/api/v2/tokens")

# CoinGecko (аварийный фоллбек)
COINGECKO = "https://api.coingecko.com/api/v3/simple/price"

# Адреса токенов на BSC (в нижнем регистре)
WBNB = os.getenv("PANCAKE_WBNB", "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c").lower()
SOL  = os.getenv("PANCAKE_SOL",  "0x570a5d26f7765ecb712c0924e4de545b89fd43df").lower()

# ---------- состояние кэша ----------
class PriceState:
    last_ratio: Optional[float] = None
    last_bnb: Optional[float] = None
    last_sol: Optional[float] = None
    last_source: Optional[str] = None
    last_updated: float = 0.0
    last_error: Optional[str] = None
    cooldowns: Dict[str, float] = {"v3": 0.0, "v2": 0.0, "gecko": 0.0}

state = PriceState()

class RateLimitError(Exception):
    pass

# ---------- утилиты ----------
async def _gql(session: aiohttp.ClientSession, query: str, variables: dict):
    if not GRAPH_API_KEY:
        raise RuntimeError("GRAPH_API_KEY не задан")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GRAPH_API_KEY}",
    }
    payload = {"query": query, "variables": variables}
    async with session.post(GRAPH_URL, json=payload, headers=headers, timeout=20) as resp:
        text = await resp.text()
        if resp.status == 429:
            raise RateLimitError("429 from The Graph")
        if resp.status >= 500:
            raise RuntimeError(f"The Graph HTTP {resp.status}")
        resp.raise_for_status()
        data = await resp.json()
        if data.get("errors"):
            raise RuntimeError(f"GraphQL error: {data['errors']} | body={text[:200]}")
        return data["data"]

async def _get_json_with_retries(session: aiohttp.ClientSession, url: str, *, attempts=3, **kwargs):
    """GET JSON с ретраями (0.5s, 1.5s, 3s), 429 -> RateLimitError, 5xx -> RuntimeError."""
    delays = [0.5, 1.5, 3.0]
    last_exc = None
    for i in range(attempts):
        try:
            async with session.get(url, timeout=15, **kwargs) as resp:
                if resp.status == 429:
                    raise RateLimitError("HTTP 429")
                if resp.status >= 500:
                    raise RuntimeError(f"HTTP {resp.status}")
                resp.raise_for_status()
                return await resp.json()
        except Exception as e:
            last_exc = e
            if i < attempts - 1:
                await asyncio.sleep(delays[min(i, len(delays)-1)])
            else:
                raise last_exc

# ---------- реализации источников ----------
GQL_QUERY = """
query Tokens($ids: [ID!]) {
  bundle(id: "1") { bnbPriceUSD }
  tokens(where: { id_in: $ids }) {
    id
    symbol
    derivedBNB
  }
}
"""

async def _prices_v3(session: aiohttp.ClientSession) -> Tuple[float, float]:
    d = await _gql(session, GQL_QUERY, {"ids": [WBNB, SOL]})
    bnb_usd = float(d["bundle"]["bnbPriceUSD"])
    tokens = {t["id"].lower(): t for t in d["tokens"]}
    if SOL not in tokens:
        raise RuntimeError("SOL не найден в subgraph v3")
    sol_usd = float(tokens[SOL]["derivedBNB"]) * bnb_usd
    return bnb_usd, sol_usd

async def _prices_v2(session: aiohttp.ClientSession) -> Tuple[float, float]:
    d1 = await _get_json_with_retries(session, f"{PANCAKE_API}/{WBNB}")
    d2 = await _get_json_with_retries(session, f"{PANCAKE_API}/{SOL}")
    bnb_usd = float(d1["data"]["price"])
    sol_usd = float(d2["data"]["price"])
    return bnb_usd, sol_usd

async def _prices_gecko(session: aiohttp.ClientSession) -> Tuple[float, float]:
    params = {"ids": "binancecoin,solana", "vs_currencies": "usd"}
    async with session.get(COINGECKO, params=params, timeout=15) as resp:
        if resp.status == 429:
            raise RateLimitError("429 from CoinGecko")
        if resp.status >= 500:
            raise RuntimeError(f"CoinGecko HTTP {resp.status}")
        resp.raise_for_status()
        data = await resp.json()
        bnb = float(data["binancecoin"]["usd"])
        sol = float(data["solana"]["usd"])
        return bnb, sol

def _cooldown_set(source: str, seconds: int):
    jitter = random.uniform(0, seconds * 0.25)
    state.cooldowns[source] = time() + seconds + jitter

def _cooldown_active(source: str) -> bool:
    return time() < state.cooldowns.get(source, 0.0)

async def _fetch_and_update() -> bool:
    async with aiohttp.ClientSession() as session:
        # 1) v3
        if GRAPH_API_KEY and not _cooldown_active("v3"):
            try:
                bnb, sol = await _prices_v3(session)
                state.last_ratio, state.last_bnb, state.last_sol = bnb / sol, bnb, sol
                state.last_source = "v3"
                state.last_updated = time()
                state.last_error = None
                return True
            except RateLimitError as e:
                logger.warning("RateLimited v3: %s", e)
                _cooldown_set("v3", V3_COOLDOWN_SEC)
                state.last_error = str(e)
            except Exception as e:
                logger.warning("v3 error: %s", e)
                _cooldown_set("v3", V3_COOLDOWN_SEC)
                state.last_error = str(e)

        # 2) v2
        if not _cooldown_active("v2"):
            try:
                bnb, sol = await _prices_v2(session)
                state.last_ratio, state.last_bnb, state.last_sol = bnb / sol, bnb, sol
                state.last_source = "v2"
                state.last_updated = time()
                state.last_error = None
                return True
            except RateLimitError as e:
                logger.warning("RateLimited v2: %s", e)
                _cooldown_set("v2", V2_COOLDOWN_SEC)
                state.last_error = str(e)
            except Exception as e:
                logger.warning("v2 error: %s", e)
                _cooldown_set("v2", V2_COOLDOWN_SEC)
                state.last_error = str(e)

        # 3) gecko
        if not _cooldown_active("gecko"):
            try:
                bnb, sol = await _prices_gecko(session)
                state.last_ratio, state.last_bnb, state.last_sol = bnb / sol, bnb, sol
                state.last_source = "gecko"
                state.last_updated = time()
                state.last_error = None
                return True
            except RateLimitError as e:
                logger.warning("RateLimited gecko: %s", e)
                _cooldown_set("gecko", GECKO_COOLDOWN_SEC)
                state.last_error = str(e)
            except Exception as e:
                logger.warning("gecko error: %s", e)
                _cooldown_set("gecko", GECKO_COOLDOWN_SEC)
                state.last_error = str(e)

    return False

async def ensure_price(force_refresh: bool = False):
    """Возвращает (ratio, bnb, sol, source, stale)."""
    stale = (time() - state.last_updated) > PRICE_TTL_SEC
    if force_refresh or stale or state.last_ratio is None:
        updated = await _fetch_and_update()
        stale = not updated and state.last_ratio is not None
        if state.last_ratio is None and not updated:
            raise RuntimeError(state.last_error or "нет данных от источников")
    return state.last_ratio, state.last_bnb, state.last_sol, state.last_source or "n/a", stale

# ---------- UI (кнопки) ----------
def _fmt_main_text(ratio: float, bnb: float, sol: float, source: str, stale: bool) -> str:
    stale_note = " (stale)" if stale else ""
    return (
        f"BNB/SOL = {ratio:.6f}{stale_note}\n"
        f"BNB={bnb:.4f} USD, SOL={sol:.4f} USD\n"
        f"source={source}"
    )

def _build_main_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📈 Обновить цену", callback_data="price:refresh")],
        [
            InlineKeyboardButton("➕ Вверх-алерт", callback_data="watch:menu_above"),
            InlineKeyboardButton("➖ Вниз-алерт", callback_data="watch:menu_below"),
        ],
        [
            InlineKeyboardButton("🔔 Список", callback_data="alerts:list"),
            InlineKeyboardButton("🧹 Сбросить", callback_data="alerts:clear"),
        ],
    ])

def _build_reply_kb() -> ReplyKeyboardMarkup:
    rows = [
        ["📈 Обновить цену"],
        ["➕ Вверх-алерт", "➖ Вниз-алерт"],
        ["🔔 Список", "🧹 Сбросить"],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def _preset_thresholds(ratio: float):
    above = [round(ratio * m, 4) for m in (1.02, 1.05, 1.10)]
    below = [round(ratio * m, 4) for m in (0.98, 0.95, 0.90)]
    return above, below

def _build_watch_kb(ratio: float, kind: str) -> InlineKeyboardMarkup:
    above, below = _preset_thresholds(ratio)
    if kind == "above":
        row = [InlineKeyboardButton(f"{v}", callback_data=f"watch:above:{v}") for v in above]
        extra = InlineKeyboardButton("✏️ Свой", callback_data="watch:custom_above")
    else:
        row = [InlineKeyboardButton(f"{v}", callback_data=f"watch:below:{v}") for v in below]
        extra = InlineKeyboardButton("✏️ Свой", callback_data="watch:custom_below")
    return InlineKeyboardMarkup([
        row,
        [extra],
        [InlineKeyboardButton("⬅️ Назад", callback_data="nav:back")],
    ])

def _format_alerts_text(chat_id: int, ratio: Optional[float]) -> str:
    cfg = watches.get(chat_id, {"above": [], "below": []})
    ab = sorted(cfg.get("above", []))
    bl = sorted(cfg.get("below", []))
    if not ab and not bl:
        return "Алертов нет. Добавь пороги кнопками или командами /watch_above и /watch_below."
    head = f"Текущий BNB/SOL: {ratio:.6f}\n" if ratio else ""
    s = [head + "Активные алерты:"]
    if ab:
        s.append("⤴️ ABOVE:")
        for i, v in enumerate(ab, 1):
            s.append(f"  {i}. ≥ {v}")
    if bl:
        s.append("⤵️ BELOW:")
        for i, v in enumerate(bl, 1):
            s.append(f"  {i}. ≤ {v}")
    s.append("\nНажми на кнопку с ❌ чтобы удалить конкретный порог.")
    return "\n".join(s)

def _build_alerts_kb(chat_id: int) -> InlineKeyboardMarkup:
    cfg = watches.get(chat_id, {"above": [], "below": []})
    kb: List[List[InlineKeyboardButton]] = []
    # ABOVE
    ab = sorted(cfg.get("above", []))
    if ab:
        for i in range(0, len(ab), 3):
            chunk = ab[i:i+3]
            kb.append([InlineKeyboardButton(f"❌ {v}", callback_data=f"alerts:del:above:{v}") for v in chunk])
    # BELOW
    bl = sorted(cfg.get("below", []))
    if bl:
        for i in range(0, len(bl), 3):
            chunk = bl[i:i+3]
            kb.append([InlineKeyboardButton(f"❌ {v}", callback_data=f"alerts:del:below:{v}") for v in chunk])
    # Низ
    kb.append([
        InlineKeyboardButton("🧹 Очистить все", callback_data="alerts:clear"),
        InlineKeyboardButton("⬅️ Назад", callback_data="nav:back"),
    ])
    return InlineKeyboardMarkup(kb)

# ---------- безопасное редактирование ----------
async def _safe_edit(q, text: str, **kwargs):
    try:
        await q.edit_message_text(text, **kwargs)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            await q.answer("Без изменений")
        else:
            raise

# ---------- мгновенная проверка добавленных порогов ----------
async def _maybe_fire_immediately(chat_id: int, app: Application) -> None:
    try:
        ratio, bnb, sol, source, stale = await ensure_price(force_refresh=False)
    except Exception:
        return
    cfg = watches.get(chat_id, {"above": [], "below": []})
    fired_above = [thr for thr in cfg.get("above", []) if ratio >= thr]
    fired_below = [thr for thr in cfg.get("below", []) if ratio <= thr]
    if not fired_above and not fired_below:
        return
    parts = []
    if fired_above:
        parts.append("⤴️ Достигнуты пороги (≥): " + ", ".join(map(str, fired_above)))
        watches[chat_id]["above"] = [x for x in cfg["above"] if x not in fired_above]
    if fired_below:
        parts.append("⤵️ Достигнуты пороги (≤): " + ", ".join(map(str, fired_below)))
        watches[chat_id]["below"] = [x for x in cfg["below"] if x not in fired_below]
    stale_note = " (stale)" if stale else ""
    text = "\n".join(parts) + f"\nBNB/SOL={ratio:.6f}{stale_note} (src={source})"
    await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=_build_main_inline())

# ---------- команды ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Клавиатура включена ↓", reply_markup=_build_reply_kb())
    try:
        ratio, bnb, sol, source, stale = await ensure_price(force_refresh=False)
        await update.message.reply_text(
            _fmt_main_text(ratio, bnb, sol, source, stale),
            reply_markup=_build_main_inline(),
        )
    except Exception:
        await update.message.reply_text(
            "Готов к работе. Нажми '📈 Обновить цену' или /price",
            reply_markup=_build_main_inline()
        )

async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        ratio, bnb, sol, source, stale = await ensure_price(force_refresh=False)
        await update.message.reply_text(
            _fmt_main_text(ratio, bnb, sol, source, stale),
            reply_markup=_build_main_inline(),
        )
    except Exception as e:
        logger.exception("price cmd failed")
        await update.message.reply_text(f"Не удалось получить цену: {e}", reply_markup=_build_main_inline())

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    try:
        ratio, *_ = await ensure_price(force_refresh=False)
    except Exception:
        ratio = None
    text = _format_alerts_text(chat_id, ratio)
    await update.message.reply_text(text, reply_markup=_build_alerts_kb(chat_id))

def _cooldown_left(name: str) -> int:
    from time import time as now
    left = int(state.cooldowns.get(name, 0) - now())
    return max(0, left)

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        ratio, bnb, sol, source, stale = await ensure_price(force_refresh=False)
        age = int(time() - state.last_updated) if state.last_updated else -1
        text = (
            f"BNB/SOL: {ratio:.6f} (src={source}{' stale' if stale else ''})\n"
            f"BNB={bnb:.4f} USD, SOL={sol:.4f} USD\n"
            f"age={age}s, TTL={PRICE_TTL_SEC}s\n"
            f"cooldowns: v3={_cooldown_left('v3')}s, v2={_cooldown_left('v2')}s, gecko={_cooldown_left('gecko')}s\n"
        )
    except Exception as e:
        text = f"status error: {e}"
    await update.message.reply_text(text, reply_markup=_build_main_inline())

# ---------- обработка callback-кнопок ----------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    chat_id = update.effective_chat.id

    try:
        if data == "price:refresh":
            ratio, bnb, sol, source, stale = await ensure_price(force_refresh=True)
            await _safe_edit(q, _fmt_main_text(ratio, bnb, sol, source, stale),
                             reply_markup=_build_main_inline())
            return

        if data == "alerts:list":
            try:
                ratio, *_ = await ensure_price(force_refresh=False)
            except Exception:
                ratio = None
            await _safe_edit(q, _format_alerts_text(chat_id, ratio),
                             reply_markup=_build_alerts_kb(chat_id))
            return

        if data == "alerts:clear":
            watches.pop(chat_id, None)
            await _safe_edit(q, "Все алерты сброшены.", reply_markup=_build_main_inline())
            return

        if data.startswith("alerts:del:"):
            _, _, kind, val = data.split(":", 3)
            try:
                thr = float(val)
            except ValueError:
                thr = None
            if thr is not None and chat_id in watches:
                if kind == "above" and thr in watches[chat_id]["above"]:
                    watches[chat_id]["above"].remove(thr)
                if kind == "below" and thr in watches[chat_id]["below"]:
                    watches[chat_id]["below"].remove(thr)
            try:
                r
