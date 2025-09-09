import os
import math
import asyncio
import logging
from typing import Dict, List, Tuple

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters,
)

import aiohttp

# ----- базовая настройка -----
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Хранилище триггеров в памяти:
watches: Dict[int, Dict[str, List[float]]] = {}

BINANCE_BASE = "https://api.binance.com"

async def fetch_price(session: aiohttp.ClientSession, symbol: str) -> float:
    url = f"{BINANCE_BASE}/api/v3/ticker/price"
    params = {"symbol": symbol}
    async with session.get(url, params=params, timeout=10) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return float(data["price"])

async def get_bnb_sol_ratio() -> Tuple[float, float, float]:
    async with aiohttp.ClientSession() as session:
        bnb = await fetch_price(session, "BNBUSDT")
        sol = await fetch_price(session, "SOLUSDT")
        ratio = bnb / sol
        return ratio, bnb, sol

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Привет! Я слежу за курсом BNB/SOL и шлю сигналы по порогам.\n\n"
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
            f"BNB/SOL = {ratio:.6f}\nBNB={bnb:.4f} USDT, SOL={sol:.4f} USDT"
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

async def _check_prices_and_alert(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        ratio, bnb, sol = await get_bnb_sol_ratio()
    except Exception as e:
        logger.warning("Не удалось получить цены: %s", e)
        return
    to_remove: Dict[int, Dict[str, List[float]]] = {}
    for chat_id, cfg in watches.items():
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
            text = "\n".join(hit_msgs) + f"\nТекущий BNB/SOL={ratio:.6f} (BNB={bnb:.4f} USDT, SOL={sol:.4f} USDT)"
            try:
                await context.bot.send_message(chat_id=chat_id, text=text)
            except Exception as e:
                logger.warning("Не удалось отправить сообщение в %s: %s", chat_id, e)
    for chat_id, rem in to_remove.items():
        if "above" in rem:
            watches[chat_id]["above"] = [x for x in watches[chat_id]["above"] if x not in rem["above"]]
        if "below" in rem:
            watches[chat_id]["below"] = [x for x in watches[chat_id]["below"] if x not in rem["below"]]

async def _post_init(app: Application) -> None:
    await app.bot.delete_webhook(drop_pending_updates=True)

def main() -> None:
    app = Application.builder().token(TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("watch_above", watch_above_cmd))
    app.add_handler(CommandHandler("watch_below", watch_below_cmd))
    app.add_handler(CommandHandler("unwatch", unwatch_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, price_cmd))
    interval = int(os.getenv("POLL_INTERVAL_SEC", "30"))
    app.job_queue.run_repeating(_check_prices_and_alert, interval=interval, first=5)
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
