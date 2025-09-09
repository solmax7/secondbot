
import os
import asyncio
import logging
from typing import Dict, List, Tuple

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

# ---------- PancakeSwap источники ----------
# v3 (The Graph)
GRAPH_API_KEY = os.getenv("GRAPH_API_KEY")  # если не задан, пойдём в v2-фоллбек
GRAPH_SUBGRAPH_ID = os.getenv(
    "GRAPH_SUBGRAPH_ID",
    "Hv1GncLY5docZoGtXjo4kwbTvxm3MAhVZqBZE4sUT9eZ"  # PancakeSwap Exchange v3 (BSC)
)
GRAPH_URL = f"https://gateway.thegraph.com/api/subgraphs/id/{GRAPH_SUBGRAPH_ID}"

# v2 (info API)
PANCAKE_API = os.getenv("PANCAKE_API", "https://api.pancakeswap.info/api/v2/tokens")

# Адреса токенов на BSC (в нижнем регистре)
WBNB = os.getenv("PANCAKE_WBNB", "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c").lower()
SOL  = os.getenv("PANCAKE_SOL",  "0x570a5d26f7765ecb712c0924e4de545b89fd43df").lower()

# ---------- источники цен ----------
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
        resp.raise_for_status()
        data = await resp.json()
        if "errors" in data and data["errors"]:
            raise RuntimeError(f"GraphQL error: {data['errors']} | body={text[:200]}")
        return data["data"]

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
    """Возвращает (bnb_usd, sol_usd) через v3 subgraph."""
    d = await _gql(session, GQL_QUERY, {"ids": [WBNB, SOL]})
    bnb_usd = float(d["bundle"]["bnbPriceUSD"])
    tokens = {t["id"].lower(): t for t in d["tokens"]}
    if SOL not in tokens:
        raise RuntimeError("SOL не найден в subgraph v3")
    sol_usd = float(tokens[SOL]["derivedBNB"]) * bnb_usd
    return bnb_usd, sol_usd

async def _prices_v2(session: aiohttp.ClientSession) -> Tuple[float, float]:
    """Возвращает (bnb_usd, sol_usd) через v2 info API."""
    async with session.get(f"{PANCAKE_API}/{WBNB}", timeout=15) as r1:
        r1.raise_for_status()
        d1 = await r1.json()
        bnb_usd = float(d1["data"]["price"])
    async with session.get(f"{PANCAKE_API}/{SOL}", timeout=15) as r2:
        r2.raise_for_status()
        d2 = await r2.json()
        sol_usd = float(d2["data"]["price"])
    return bnb_usd, sol_usd

async def get_bnb_sol_ratio() -> Tuple[float, float, float]:
    """Пытаемся v3 → если не вышло — падаем в v2. Возвращаем (ratio, bnb_usd, sol_usd)."""
    async with aiohttp.ClientSession() as session:
        # 1) v3 (если есть ключ)
        if GRAPH_API_KEY:
            try:
                bnb, sol = await _prices_v3(session)
                ratio = bnb / sol
                return ratio, bnb, sol
            except Exception as e:
                logger.warning("v3 (The Graph) не сработал: %s — пробуем v2", e)
        # 2) v2 fallback
        bnb, sol = await _prices_v2(session)
        ratio = bnb / sol
        return ratio, bnb, sol

# ---------- команды ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Привет! Я слежу за курсом BNB/SOL (PancakeSwap). Использую v3 (The Graph) с фоллбеком на v2.\n\n"
        "Команды:\n"
        "/price — текущий BNB/SOL\n"
        "/watch_above <число> — алерт, когда BNB/SOL ≥ порога\n"
        "/watch_below <число> — алерт, когда BNB/SOL ≤ порога\n"
        "/unwatch — снять все алерты для этого чата\n"
        "/list — показать активные алерты"
    )
    await update.message.reply_text(text)

async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        ratio, bnb, sol = await get_bnb_sol_ratio()
        await update.message.reply_text(
            f"BNB/SOL = {ratio:.6f}\nBNB={bnb:.4f} USD, SOL={sol:.4f} USD (Pancake v3→v2)"
        )
    except Exception as e:
        logger.exception("price cmd failed")
        await update.message.reply_text(f"Не удалось получить цену: {e}")

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
async def _alert_all(app: Application) -> None:
    try:
        ratio, bnb, sol = await get_bnb_sol_ratio()
    except Exception as e:
        logger.warning("Не удалось получить цены: %s", e)
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
            text = "\n".join(hit_msgs) + f"\nBNB/SOL={ratio:.6f} (BNB={bnb:.4f} USD, SOL={sol:.4f} USD, Pancake v3→v2)"
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
    logger.warning("Запущен asyncio-таймер: интервал %s сек", interval)
    while True:
        await _alert_all(app)
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
    # на обычный текст просто показываем текущий курс
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, price_cmd))

    interval = int(os.getenv("POLL_INTERVAL_SEC", "30"))

    # Если установлен extra job-queue — используем его, иначе fallback на asyncio
    jq = getattr(app, "job_queue", None)
    if jq is not None:
        jq.run_repeating(lambda ctx: _alert_all(app), interval=interval, first=5)
        logger.info("JobQueue активен (интервал %s сек)", interval)
    else:
        try:
            app.create_task(_background_loop(app, interval))
        except Exception:
            asyncio.get_event_loop().create_task(_background_loop(app, interval))
        logger.warning("JobQueue не обнаружен — используем asyncio fallback")

    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
