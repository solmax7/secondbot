
import os
import asyncio
import logging
import random
from typing import Dict, List, Tuple, Optional
from time import time

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters,
)
import aiohttp

# ---------- базовая настройка ----------
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Памятка триггеров: watches[chat_id] = {"above": [...], "below": [...]}
watches: Dict[int, Dict[str, List[float]]] = {}

# ---------- параметры (можно настраивать через ENV) ----------
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "60"))  # реже опрашиваем
PRICE_TTL_SEC     = int(os.getenv("PRICE_TTL_SEC", "30"))       # сколько кэш валиден
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

# ---------- исключение для 429 ----------
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
    """(bnb_usd, sol_usd) через Pancake v3 subgraph."""
    d = await _gql(session, GQL_QUERY, {"ids": [WBNB, SOL]})
    bnb_usd = float(d["bundle"]["bnbPriceUSD"])
    tokens = {t["id"].lower(): t for t in d["tokens"]}
    if SOL not in tokens:
        raise RuntimeError("SOL не найден в subgraph v3")
    sol_usd = float(tokens[SOL]["derivedBNB"]) * bnb_usd
    return bnb_usd, sol_usd

async def _prices_v2(session: aiohttp.ClientSession) -> Tuple[float, float]:
    """(bnb_usd, sol_usd) через Pancake Info API v2 с ретраями."""
    d1 = await _get_json_with_retries(session, f"{PANCAKE_API}/{WBNB}")
    d2 = await _get_json_with_retries(session, f"{PANCAKE_API}/{SOL}")
    bnb_usd = float(d1["data"]["price"])
    sol_usd = float(d2["data"]["price"])
    return bnb_usd, sol_usd

async def _prices_gecko(session: aiohttp.ClientSession) -> Tuple[float, float]:
    """(bnb_usd, sol_usd) через CoinGecko (аварийный фоллбек)."""
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
    # добавим небольшой джиттер, чтобы не бить в ровные секунды
    jitter = random.uniform(0, seconds * 0.25)
    state.cooldowns[source] = time() + seconds + jitter

def _cooldown_active(source: str) -> bool:
    return time() < state.cooldowns.get(source, 0.0)

async def _fetch_and_update() -> bool:
    """
    Пытаемся обновить цену в state по порядку источников, учитывая cooldown.
    Возвращает True, если обновили.
    """
    async with aiohttp.ClientSession() as session:
        # 1) v3
        if GRAPH_API_KEY and not _cooldown_active("v3"):
            try:
                bnb, sol = await _prices_v3(session)
                state.last_ratio, state.last_bnb, state.last_sol = bnb/sol, bnb, sol
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
                state.last_ratio, state.last_bnb, state.last_sol = bnb/sol, bnb, sol
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
                state.last_ratio, state.last_bnb, state.last_sol = bnb/sol, bnb, sol
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

async def ensure_price(force_refresh: bool = False) -> Tuple[float, float, float, str, bool]:
    """
    Возвращает (ratio, bnb, sol, source, stale).
    Если кэш старше PRICE_TTL_SEC или force_refresh=True — пытаемся обновить.
    """
    stale = (time() - state.last_updated) > PRICE_TTL_SEC
    if force_refresh or stale or state.last_ratio is None:
        updated = await _fetch_and_update()
        stale = not updated and state.last_ratio is not None
        if state.last_ratio is None and not updated:
            # вообще нет данных
            raise RuntimeError(state.last_error or "нет данных от источников")
    return state.last_ratio, state.last_bnb, state.last_sol, state.last_source or "n/a", stale

# ---------- команды ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Привет! Я слежу за курсом BNB/SOL и шлю сигналы.\n\n"
        "Команды:\n"
        "/price — текущий BNB/SOL\n"
        "/watch_above <число> — алерт, когда BNB/SOL ≥ порога\n"
        "/watch_below <число> — алерт, когда BNB/SOL ≤ порога\n"
        "/unwatch — снять все алерты для этого чата\n"
        "/list — показать активные алерты"
    )
    await update.message.reply_text(text)

def _ensure_chat_entry(chat_id: int) -> None:
    if chat_id not in watches:
        watches[chat_id] = {"above": [], "below": []}

def _parse_threshold(arg_list: List[str]) -> float:
    if not arg_list:
        raise ValueError("Укажи число, например: 3.2")
    try:
        return float(arg_list[0].replace(",", "."))
    except ValueError:
        raise ValueError("Неверный формат. Пример: 3.2")

async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        ratio, bnb, sol, source, stale = await ensure_price(force_refresh=False)
        stale_note = " (stale)" if stale else ""
        await update.message.reply_text(
            f"BNB/SOL = {ratio:.6f}{stale_note}\n"
            f"BNB={bnb:.4f} USD, SOL={sol:.4f} USD\n"
            f"source={source}"
        )
    except Exception as e:
        logger.exception("price cmd failed")
        await update.message.reply_text(f"Не удалось получить цену: {e}")

async def watch_above_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        thr = _parse_threshold(context.args)
        chat_id = update.effective_chat.id
        _ensure_chat_entry(chat_id)
        watches[chat_id]["above"].append(thr)
        await update.message.reply_text(f"Ок! Сообщу, когда BNB/SOL ≥ {thr}")
    except Exception as e:
        await update.message.reply_text(str(e))

async def watch_below_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        thr = _parse_threshold(context.args)
        chat_id = update.effective_chat.id
        _ensure_chat_entry(chat_id)
        watches[chat_id]["below"].append(thr)
        await update.message.reply_text(f"Ок! Сообщу, когда BNB/SOL ≤ {thr}")
    except Exception as e:
        await update.message.reply_text(str(e))

async def unwatch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id in watches:
        watches.pop(chat_id)
        await update.message.reply_text("Все алерты для этого чата сброшены.")
    else:
        await update.message.reply_text("Алертов не было.")

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    _ensure_chat_entry(chat_id)
    above = watches[chat_id]["above"]
    below = watches[chat_id]["below"]
    if not above and not below:
        await update.message.reply_text("Алертов пока нет. Используй /watch_above или /watch_below.")
        return
    lines = []
    if above:
        lines.append("⤴️ ABOVE: " + ", ".join(str(x) for x in sorted(above)))
    if below:
        lines.append("⤵️ BELOW: " + ", ".join(str(x) for x in sorted(below)))
    await update.message.reply_text("\n".join(lines))

# ---------- проверка & уведомления ----------
async def _check_and_alert(app: Application) -> None:
    try:
        ratio, bnb, sol, source, stale = await ensure_price(force_refresh=False)
    except Exception as e:
        logger.warning("Не удалось обновить цены для алертов: %s", e)
        return

    to_remove: Dict[int, Dict[str, List[float]]] = {}
    for chat_id, cfg in list(watches.items()):
        hit_msgs: List[str] = []
        fired_above = [thr for thr in cfg["above"] if ratio >= thr]
        if fired_above:
            hit_msgs.append("⤴️ Достигнуты пороги (≥): " + ", ".join(str(x) for x in fired_above))
            to_remove.setdefault(chat_id, {}).setdefault("above", []).extend(fired_above)
        fired_below = [thr for thr in cfg["below"] if ratio <= thr]
        if fired_below:
            hit_msgs.append("⤵️ Достигнуты пороги (≤): " + ", ".join(str(x) for x in fired_below))
            to_remove.setdefault(chat_id, {}).setdefault("below", []).extend(fired_below)
        if hit_msgs:
            stale_note = " (stale)" if stale else ""
            text = "\n".join(hit_msgs) + f"\nBNB/SOL={ratio:.6f}{stale_note} (src={source})"
            try:
                await app.bot.send_message(chat_id=chat_id, text=text)
            except Exception as e:
                logger.warning("Не удалось отправить сообщение в %s: %s", chat_id, e)

    for chat_id, rem in to_remove.items():
        if "above" in rem:
            watches[chat_id]["above"] = [x for x in watches[chat_id]["above"] if x not in rem["above"]]
        if "below" in rem:
            watches[chat_id]["below"] = [x for x in watches[chat_id]["below"] if x not in rem["below"]]

async def _background_loop(app: Application, interval: int) -> None:
    await asyncio.sleep(5)
    logger.info("Запущен asyncio-таймер: интервал %s сек", interval)
    while True:
        await _check_and_alert(app)
        await asyncio.sleep(interval)

# ---------- init & main ----------
async def _post_init(app: Application) -> None:
    await app.bot.delete_webhook(drop_pending_updates=True)

def main() -> None:
    app = Application.builder().token(TOKEN).post_init(_post_init).build()

    # команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("watch_above", watch_above_cmd))
    app.add_handler(CommandHandler("watch_below", watch_below_cmd))
    app.add_handler(CommandHandler("unwatch", unwatch_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    # на обычный текст просто показываем текущий курс (не форсим обновление)
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

if __name__ == "__main__":
    main()
