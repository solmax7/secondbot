
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

# ---------- PancakeSwap v3 (Subgraph on The Graph) ----------
# Источник: https://developer.pancakeswap.finance/apis/subgraph  (Exchange v3 / BSC)
# Для запроса к dGraph нужен API-ключ The Graph (бесплатно получить в Studio).
GRAPH_API_KEY = os.getenv("GRAPH_API_KEY")  # ОБЯЗАТЕЛЬНО задать
GRAPH_SUBGRAPH_ID = os.getenv(
    "GRAPH_SUBGRAPH_ID",
    "Hv1GncLY5docZoGtXjo4kwbTvxm3MAhVZqBZE4sUT9eZ"  # Exchange (v3) BSC
)
GRAPH_URL = f"https://gateway.thegraph.com/api/subgraphs/id/{GRAPH_SUBGRAPH_ID}"

# Адреса токенов на BSC (в нижнем регистре)
WBNB = os.getenv("PANCAKE_WBNB", "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c").lower()
SOL  = os.getenv("PANCAKE_SOL",  "0x570a5d26f7765ecb712c0924e4de545b89fd43df").lower()

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

async def _gql(session: aiohttp.ClientSession, query: str, variables: dict):
    if not GRAPH_API_KEY:
        raise RuntimeError("Не задан GRAPH_API_KEY для The Graph")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GRAPH_API_KEY}",
    }
    payload = {"query": query, "variables": variables}
    async with session.post(GRAPH_URL, json=payload, headers=headers, timeout=20) as resp:
        txt = await resp.text()
        resp.raise_for_status()
        data = await resp.json()
        if "errors" in data and data["errors"]:
            raise RuntimeError(f"GraphQL error: {data['errors']} | body={txt[:200]}")
        return data["data"]

async def get_bnb_sol_ratio() -> Tuple[float, float, float]:
    """
    Возвращает (ratio, bnb_usd, sol_usd) через PancakeSwap v3 subgraph:
    ratio = BNB_USD / SOL_USD
    """
    async with aiohttp.ClientSession() as session:
        d = await _gql(session, GQL_QUERY, {"ids": [WBNB, SOL]})
        bnb_usd = float(d["bundle"]["bnbPriceUSD"])
        tokens = {t["id"].lower(): t for t in d["tokens"]}
        if SOL not in tokens:
            raise RuntimeError("SOL не найден в v3 subgraph")
        sol_derived_bnb = float(tokens[SOL]["derivedBNB"])
        sol_usd = sol_derived_bnb * bnb_usd
        ratio = bnb_usd / sol_usd
        return ratio, bnb_usd, sol_usd

# ---------- команды ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Привет! Я слежу за курсом BNB/SOL (данные PancakeSwap v3 / The Graph) и шлю сигналы.\n\n"
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
            f"BNB/SOL = {ratio:.6f}\nBNB={bnb:.4f} USD, SOL={sol:.4f} USD (Pancake v3)"
        )
    except Exception as e:
        logger.exception("price cmd failed")
        await update.message.reply_text(f"Не удалось получить цену (Pancake v3): {e}")

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
            text = "\n".join(hit_msgs) + f"\nBNB/SOL={ratio:.6f} (BNB={bnb:.4f} USD, SOL={sol:.4f} USD, Pancake v3)"
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
