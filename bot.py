import os
import asyncio
import logging
import random
import json
import re
from pathlib import Path
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

# ---------- хранение алертов ----------
# Где хранить JSON (подключи Railway Volume и укажи DATA_FILE=/data/watches.json)
DATA_FILE = os.getenv("DATA_FILE", "watches.json")

# Удалять пороги после срабатывания? (1/true/yes = да, иначе — оставить)
REMOVE_AFTER_FIRE = os.getenv("REMOVE_AFTER_FIRE", "1").lower() in ("1", "true", "yes")

# Хранилище триггеров: watches[chat_id] = {"above": [...], "below": [...]}
watches: Dict[int, Dict[str, List[float]]] = {}
# Ожидание ввода числа от пользователя: pending_input[chat_id] = "above" | "below"
pending_input: Dict[int, str] = {}

def _save_state():
    """Атомарно сохраняем алерты в JSON."""
    try:
        p = Path(DATA_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {"watches": watches}
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False))
        tmp.replace(p)
        logger.info("Сохранены алерты в %s", p)
    except Exception as e:
        logger.warning("Не удалось сохранить алерты: %s", e)

def _load_state():
    """Загружаем алерты из JSON (если есть)."""
    global watches
    try:
        p = Path(DATA_FILE)
        if not p.exists():
            logger.info("Файл алертов не найден (%s) — пропускаем загрузку", p)
            return
        data = json.loads(p.read_text())
        raw = data.get("watches", {})
        watches.clear()
        for k, v in raw.items():
            watches[int(k)] = {
                "above": [float(x) for x in v.get("above", [])],
                "below": [float(x) for x in v.get("below", [])],
            }
        logger.info("Загружены алерты: %d чатов", len(watches))
    except Exception as e:
        logger.warning("Не удалось загрузить алерты: %s", e)

# ---------- параметры (ENV-переменные) ----------
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "60"))   # период фоновой проверки
PRICE_TTL_SEC     = int(os.getenv("PRICE_TTL_SEC", "30"))       # валидность кэша
V3_COOLDOWN_SEC   = int(os.getenv("V3_COOLDOWN_SEC", "20"))
V2_COOLDOWN_SEC   = int(os.getenv("V2_COOLDOWN_SEC", "60"))
GECKO_COOLDOWN_SEC= int(os.getenv("GECKO_COOLDOWN_SEC", "120"))

# ---------- источники цен ----------
GRAPH_API_KEY = os.getenv("GRAPH_API_KEY")  # если пусто — будет фоллбек
GRAPH_SUBGRAPH_ID = os.getenv("GRAPH_SUBGRAPH_ID", "Hv1GncLY5docZoGtXjo4kwbTvxm3MAhVZqBZE4sUT9eZ")
GRAPH_URL = f"https://gateway.thegraph.com/api/subgraphs/id/{GRAPH_SUBGRAPH_ID}"
PANCAKE_API = os.getenv("PANCAKE_API", "https://api.pancakeswap.info/api/v2/tokens")
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
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {GRAPH_API_KEY}"}
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
    ab = sorted(cfg.get("above", []))
    if ab:
        for i in range(0, len(ab), 3):
            chunk = ab[i:i+3]
            kb.append([InlineKeyboardButton(f"❌ {v}", callback_data=f"alerts:del:above:{v}") for v in chunk])
    bl = sorted(cfg.get("below", []))
    if bl:
        for i in range(0, len(bl), 3):
            chunk = bl[i:i+3]
            kb.append([InlineKeyboardButton(f"❌ {v}", callback_data=f"alerts:del:below:{v}") for v in chunk])
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

# ---------- вспомогательные ----------
def _ensure_chat_entry(chat_id: int) -> None:
    if chat_id not in watches:
        watches[chat_id] = {"above": [], "below": []}

def _parse_threshold_text(text: str) -> float:
    t = text.strip().replace(",", ".")
    return float(t)

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
        if REMOVE_AFTER_FIRE:
            watches[chat_id]["above"] = [x for x in cfg["above"] if x not in fired_above]
    if fired_below:
        parts.append("⤵️ Достигнуты пороги (≤): " + ", ".join(map(str, fired_below)))
        if REMOVE_AFTER_FIRE:
            watches[chat_id]["below"] = [x for x in cfg["below"] if x not in fired_below]
    stale_note = " (stale)" if stale else ""
    text = "\n".join(parts) + f"\nBNB/SOL={ratio:.6f}{stale_note} (src={source})"
    await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=_build_main_inline())
    _save_state()  # могли изменить списки

# ---------- команды ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Клавиатура включена ↓", reply_markup=_build_reply_kb())
    try:
        ratio, bnb, sol, source, stale = await ensure_price(force_refresh=False)
        await update.message.reply_text(_fmt_main_text(ratio, bnb, sol, source, stale),
                                        reply_markup=_build_main_inline())
    except Exception:
        await update.message.reply_text("Готов к работе. Нажми '📈 Обновить цену' или /price",
                                        reply_markup=_build_main_inline())

async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        ratio, bnb, sol, source, stale = await ensure_price(force_refresh=False)
        await update.message.reply_text(_fmt_main_text(ratio, bnb, sol, source, stale),
                                        reply_markup=_build_main_inline())
    except Exception as e:
        logger.exception("price cmd failed")
        await update.message.reply_text(f"Не удалось получить цену: {e}",
                                        reply_markup=_build_main_inline())

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    try:
        ratio, *_ = await ensure_price(force_refresh=False)
    except Exception:
        ratio = None
    await update.message.reply_text(_format_alerts_text(chat_id, ratio),
                                    reply_markup=_build_alerts_kb(chat_id))

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
            f"REMOVE_AFTER_FIRE={REMOVE_AFTER_FIRE}\n"
            f"DATA_FILE={DATA_FILE}\n"
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
            pending_input.pop(chat_id, None)
            _save_state()
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
                _save_state()
            try:
                ratio, *_ = await ensure_price(force_refresh=False)
            except Exception:
                ratio = None
            await _safe_edit(q, _format_alerts_text(chat_id, ratio),
                             reply_markup=_build_alerts_kb(chat_id))
            return

        if data in ("watch:menu_above", "watch:menu_below"):
            kind = "above" if data.endswith("above") else "below"
            ratio, bnb, sol, source, stale = await ensure_price(force_refresh=False)
            await _safe_edit(q, f"Выбери порог для {('⤴️ ABOVE' if kind=='above' else '⤵️ BELOW')}",
                             reply_markup=_build_watch_kb(ratio, kind))
            return

        if data.startswith("watch:above:"):
            thr = float(data.split(":", 2)[2])
            _ensure_chat_entry(chat_id)
            watches[chat_id]["above"].append(thr)
            _save_state()
            await _safe_edit(q, f"Ок! Сообщу, когда BNB/SOL ≥ {thr}", reply_markup=_build_main_inline())
            await _maybe_fire_immediately(chat_id, context.application)
            return

        if data.startswith("watch:below:"):
            thr = float(data.split(":", 2)[2])
            _ensure_chat_entry(chat_id)
            watches[chat_id]["below"].append(thr)
            _save_state()
            await _safe_edit(q, f"Ок! Сообщу, когда BNB/SOL ≤ {thr}", reply_markup=_build_main_inline())
            await _maybe_fire_immediately(chat_id, context.application)
            return

        if data == "watch:custom_above":
            pending_input[chat_id] = "above"
            await _safe_edit(q, "Введи свой порог для ⤴️ ABOVE числом (например, 3.2)",
                             reply_markup=_build_main_inline())
            return

        if data == "watch:custom_below":
            pending_input[chat_id] = "below"
            await _safe_edit(q, "Введи свой порог для ⤵️ BELOW числом (например, 3.2)",
                             reply_markup=_build_main_inline())
            return

        if data == "nav:back":
            ratio, bnb, sol, source, stale = await ensure_price(force_refresh=False)
            await _safe_edit(q, _fmt_main_text(ratio, bnb, sol, source, stale),
                             reply_markup=_build_main_inline())
            return

        await _safe_edit(q, "Неизвестная команда.", reply_markup=_build_main_inline())

    except Exception as e:
        logger.exception("callback failed")
        await _safe_edit(q, f"Ошибка: {e}", reply_markup=_build_main_inline())

# ---------- ручные команды /watch_* и /unwatch ----------
async def watch_above_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not context.args:
        pending_input[chat_id] = "above"
        await update.message.reply_text("Введи свой порог для ⤴️ ABOVE числом (например, 3.2)",
                                        reply_markup=_build_main_inline())
        return
    try:
        thr = float(context.args[0].replace(",", "."))
        _ensure_chat_entry(chat_id)
        watches[chat_id]["above"].append(thr)
        _save_state()
        await update.message.reply_text(f"Ок! Сообщу, когда BNB/SOL ≥ {thr}",
                                        reply_markup=_build_main_inline())
        await _maybe_fire_immediately(chat_id, context.application)
    except Exception as e:
        await update.message.reply_text(str(e), reply_markup=_build_main_inline())

async def watch_below_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not context.args:
        pending_input[chat_id] = "below"
        await update.message.reply_text("Введи свой порог для ⤵️ BELOW числом (например, 3.2)",
                                        reply_markup=_build_main_inline())
        return
    try:
        thr = float(context.args[0].replace(",", "."))
        _ensure_chat_entry(chat_id)
        watches[chat_id]["below"].append(thr)
        _save_state()
        await update.message.reply_text(f"Ок! Сообщу, когда BNB/SOL ≤ {thr}",
                                        reply_markup=_build_main_inline())
        await _maybe_fire_immediately(chat_id, context.application)
    except Exception as e:
        await update.message.reply_text(str(e), reply_markup=_build_main_inline())

async def unwatch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    watches.pop(chat_id, None)
    pending_input.pop(chat_id, None)
    _save_state()
    await update.message.reply_text("Все алерты для этого чата сброшены.",
                                    reply_markup=_build_main_inline())

async def list_cmd_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await list_cmd(update, context)

# ---------- обработчики reply-клавиатуры ----------
async def open_above_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ratio, bnb, sol, source, stale = await ensure_price(force_refresh=False)
    await update.message.reply_text("Выбери порог для ⤴️ ABOVE",
                                    reply_markup=_build_watch_kb(ratio, "above"))

async def open_below_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ratio, bnb, sol, source, stale = await ensure_price(force_refresh=False)
    await update.message.reply_text("Выбери порог для ⤵️ BELOW",
                                    reply_markup=_build_watch_kb(ratio, "below"))

# ---------- обработчик числового ввода ----------
NUM_RE = re.compile(r"^\s*\d+([.,]\d+)?\s*$")
async def numeric_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id not in pending_input:
        return await price_cmd(update, context)

    kind = pending_input.pop(chat_id)
    try:
        thr = _parse_threshold_text(update.message.text)
        _ensure_chat_entry(chat_id)
        watches[chat_id][kind].append(thr)
        _save_state()
        sign = "≥" if kind == "above" else "≤"
        await update.message.reply_text(f"Ок! Сообщу, когда BNB/SOL {sign} {thr}",
                                        reply_markup=_build_main_inline())
        await _maybe_fire_immediately(chat_id, context.application)
    except Exception as e:
        await update.message.reply_text(f"Не понял число. Пример: 3.2\nОшибка: {e}",
                                        reply_markup=_build_main_inline())

# ---------- алерты (фон) ----------
async def _check_and_alert(app: Application) -> None:
    try:
        ratio, bnb, sol, source, stale = await ensure_price(force_refresh=True)
    except Exception as e:
        logger.warning("Не удалось обновить цены для алертов: %s", e)
        return

    to_remove: Dict[int, Dict[str, List[float]]] = {}
    for chat_id, cfg in list(watches.items()):
        hit_msgs: List[str] = []
        fired_above = [thr for thr in cfg["above"] if ratio >= thr]
        if fired_above:
            hit_msgs.append("⤴️ Достигнуты пороги (≥): " + ", ".join(str(x) for x in fired_above))
            if REMOVE_AFTER_FIRE:
                to_remove.setdefault(chat_id, {}).setdefault("above", []).extend(fired_above)
        fired_below = [thr for thr in cfg["below"] if ratio <= thr]
        if fired_below:
            hit_msgs.append("⤵️ Достигнуты пороги (≤): " + ", ".join(str(x) for x in fired_below))
            if REMOVE_AFTER_FIRE:
                to_remove.setdefault(chat_id, {}).setdefault("below", []).extend(fired_below)
        if hit_msgs:
            stale_note = " (stale)" if stale else ""
            text = "\n".join(hit_msgs) + f"\nBNB/SOL={ratio:.6f}{stale_note} (src={source})"
            try:
                await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=_build_main_inline())
            except Exception as e:
                logger.warning("Не удалось отправить сообщение в %s: %s", chat_id, e)

    # снимаем сработавшие пороги и сохраняем
    changed = False
    for chat_id, rem in to_remove.items():
        if "above" in rem:
            watches[chat_id]["above"] = [x for x in watches[chat_id]["above"] if x not in rem["above"]]
            changed = True
        if "below" in rem:
            watches[chat_id]["below"] = [x for x in watches[chat_id]["below"] if x not in rem["below"]]
            changed = True
    if changed:
        _save_state()

# ---------- init & main ----------
async def _post_init(app: Application) -> None:
    _load_state()
    await app.bot.delete_webhook(drop_pending_updates=True)

def main() -> None:
    app = Application.builder().token(TOKEN).post_init(_post_init).build()

    # команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("watch_above", watch_above_cmd))
    app.add_handler(CommandHandler("watch_below", watch_below_cmd))
    app.add_handler(CommandHandler("unwatch", unwatch_cmd))
    app.add_handler(CommandHandler("list", list_cmd_cmd))
    app.add_handler(CommandHandler("status", status_cmd))

    # инлайн-кнопки
    app.add_handler(CallbackQueryHandler(on_callback))

    # reply-клавиатура (кнопки у поля ввода)
    app.add_handler(MessageHandler(filters.Regex("^📈 Обновить цену$"), price_cmd))
    app.add_handler(MessageHandler(filters.Regex("^🔔 Список$"), list_cmd))
    app.add_handler(MessageHandler(filters.Regex("^🧹 Сбросить$"), unwatch_cmd))
    app.add_handler(MessageHandler(filters.Regex("^➕ Вверх-алерт$"), open_above_menu))
    app.add_handler(MessageHandler(filters.Regex("^➖ Вниз-алерт$"), open_below_menu))

    # числовой ввод — ставим выше fallback
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(NUM_RE), numeric_input_handler))

    # на любой другой текст — просто текущий курс
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, price_cmd))

    # Планировщик/таймер
    jq = getattr(app, "job_queue", None)
    if jq is not None:
        jq.run_repeating(lambda ctx: _check_and_alert(app), interval=POLL_INTERVAL_SEC, first=5)
        logger.info("JobQueue активен (интервал %s сек)", POLL_INTERVAL_SEC)
    else:
        try:
            app.create_task(_background_loop(app, POLL_INTERVAL_SEC))
        except Exception:
            asyncio.get_event_loop().create_task(_background_loop(app, POLL_INTERVAL_SEC))
        logger.warning("JobQueue не обнаружен — используем asyncio fallback")

    app.run_polling(close_loop=False)

async def _background_loop(app: Application, interval: int) -> None:
    await asyncio.sleep(5)
    logger.info("Запущен asyncio-таймер: интервал %s сек", interval)
    while True:
        await _check_and_alert(app)
        await asyncio.sleep(interval)

if __name__ == "__main__":
    main()
