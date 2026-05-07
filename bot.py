import os
import json
import logging
import asyncio
import httpx
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)

# ─── Логирование ────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── Переменные окружения ────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID   = int(os.environ["CHAT_ID"])

# ─── Файл для хранения списка комбинаций ────────────────────────
DATA_FILE = "watchlist.json"

# ID Харьковской области на сайте ГСЦ
# (определяется по порядку в выпадающем списке: Харківська = 21)
REGION_ID = "21"

# ─── Загрузка / сохранение списка ───────────────────────────────
def load_watchlist() -> list[str]:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return ["5000", "5055"]   # значения по умолчанию

def save_watchlist(lst: list[str]):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(lst, f, ensure_ascii=False, indent=2)

watchlist: list[str] = load_watchlist()

# ─── Запрос к сайту ГСЦ ─────────────────────────────────────────
async def check_combination(combo: str) -> list[str]:
    """
    Возвращает список найденных номеров, содержащих комбинацию combo.
    Сайт использует WordPress AJAX — action=get_license_plates.
    """
    url = "https://opendata.hsc.gov.ua/wp-admin/admin-ajax.php"
    payload = {
        "action": "get_license_plates",
        "region": REGION_ID,
        "tsc": "",            # все ТСЦ области
        "vehicle_type": "",   # любой тип
        "combination": combo,
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://opendata.hsc.gov.ua/check-leisure-license-plates/",
        "X-Requested-With": "XMLHttpRequest",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, data=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            # Ответ сервера: {"success": true, "data": ["АА5000ВВ", ...]}
            # или {"success": false, "data": []}
            if data.get("success") and isinstance(data.get("data"), list):
                return data["data"]
            return []
    except Exception as e:
        logger.error(f"Ошибка запроса для '{combo}': {e}")
        return []

# ─── Ежедневная проверка ─────────────────────────────────────────
async def daily_check(app: Application):
    if not watchlist:
        return

    logger.info("Запускаю ежедневную проверку...")
    found_any = False

    for combo in watchlist:
        results = await check_combination(combo)
        if results:
            found_any = True
            plates_str = "\n".join(f"  • {p}" for p in results)
            msg = (
                f"🎉 <b>Найдены номера с комбинацией <code>{combo}</code>!</b>\n\n"
                f"{plates_str}\n\n"
                f"🔗 <a href='https://opendata.hsc.gov.ua/check-leisure-license-plates/'>Открыть сайт</a>\n"
                f"⏰ {datetime.now().strftime('%d.%m.%Y %H:%M')}"
            )
            await app.bot.send_message(CHAT_ID, msg, parse_mode="HTML",
                                       disable_web_page_preview=True)
        else:
            logger.info(f"Комбинация '{combo}' — не найдена")

    if not found_any:
        # тихое уведомление: всё проверено, ничего нет
        await app.bot.send_message(
            CHAT_ID,
            f"🔍 Ежедневная проверка завершена — ни одна из {len(watchlist)} "
            f"комбинаций пока не доступна.\n"
            f"⏰ {datetime.now().strftime('%d.%m.%Y %H:%M')}",
            parse_mode="HTML"
        )

# ─── Команды Telegram ────────────────────────────────────────────

def only_owner(func):
    """Декоратор: разрешает только владельцу (CHAT_ID)"""
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != CHAT_ID:
            await update.message.reply_text("⛔ Нет доступа.")
            return
        return await func(update, ctx)
    return wrapper

@only_owner
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я слежу за номерами на сайте ГСЦ МВС (Харьков).\n\n"
        "📋 <b>Команды:</b>\n"
        "/list — показать список комбинаций\n"
        "/add 1234 — добавить комбинацию\n"
        "/remove 1234 — удалить комбинацию\n"
        "/check — проверить прямо сейчас\n"
        "/help — помощь",
        parse_mode="HTML"
    )

@only_owner
async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not watchlist:
        await update.message.reply_text("📋 Список пуст. Добавьте: /add 1234")
        return
    items = "\n".join(f"  • <code>{c}</code>" for c in watchlist)
    await update.message.reply_text(
        f"📋 <b>Отслеживаемые комбинации ({len(watchlist)}):</b>\n{items}",
        parse_mode="HTML"
    )

@only_owner
async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Укажите комбинацию: /add 1234")
        return
    combo = ctx.args[0].upper().strip()
    if combo in watchlist:
        await update.message.reply_text(f"⚠️ <code>{combo}</code> уже в списке.", parse_mode="HTML")
        return
    watchlist.append(combo)
    save_watchlist(watchlist)
    await update.message.reply_text(f"✅ Добавлено: <code>{combo}</code>", parse_mode="HTML")

@only_owner
async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Укажите комбинацию: /remove 1234")
        return
    combo = ctx.args[0].upper().strip()
    if combo not in watchlist:
        await update.message.reply_text(f"❌ <code>{combo}</code> не найдено в списке.", parse_mode="HTML")
        return
    watchlist.remove(combo)
    save_watchlist(watchlist)
    await update.message.reply_text(f"🗑 Удалено: <code>{combo}</code>", parse_mode="HTML")

@only_owner
async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Запускаю проверку, подождите...")
    await daily_check(ctx.application)

@only_owner
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 <b>Справка</b>\n\n"
        "Бот проверяет сайт ГСЦ МВС раз в день (в 09:00) и сообщает, "
        "если нужная комбинация цифр появилась в Харьковской области.\n\n"
        "<b>Команды:</b>\n"
        "/list — список комбинаций\n"
        "/add 5000 — добавить комбинацию\n"
        "/remove 5000 — удалить комбинацию\n"
        "/check — проверить прямо сейчас",
        parse_mode="HTML"
    )

# ─── Запуск ──────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Регистрируем команды
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("list",   cmd_list))
    app.add_handler(CommandHandler("add",    cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("check",  cmd_check))
    app.add_handler(CommandHandler("help",   cmd_help))

    # Планировщик: проверка каждый день в 09:00
    scheduler = AsyncIOScheduler(timezone="Europe/Kiev")
    scheduler.add_job(
        daily_check,
        trigger="cron",
        hour=9, minute=0,
        args=[app]
    )

    async def on_startup(app):
        scheduler.start()
        logger.info("Бот запущен. Ежедневная проверка в 09:00 (Киев).")

    app.post_init = on_startup
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
