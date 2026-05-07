import os
import json
import logging
import httpx
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes
)

# ─── Логирование ────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── Переменные окружения ────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID  = int(os.environ.get("ADMIN_ID", "0"))   # ваш ID для админ-команд

# ─── Хранилище: { user_id: {"region": "21", "combos": ["5000", ...]} }
DATA_FILE = "users.json"

# Список областей по индексу на сайте ГСЦ
REGIONS = {
    "1":  "АР Крим",
    "2":  "Вінницька",
    "3":  "Волинська",
    "4":  "Дніпропетровська",
    "5":  "Донецька",
    "6":  "Житомирська",
    "7":  "Закарпатська",
    "8":  "Запорізька",
    "9":  "Івано-Франківська",
    "10": "м. Київ",
    "11": "Київська",
    "12": "Кіровоградська",
    "13": "Луганська",
    "14": "Львівська",
    "15": "Миколаївська",
    "16": "Одеська",
    "17": "Полтавська",
    "18": "Рівненська",
    "19": "Сумська",
    "20": "Тернопільська",
    "21": "Харківська",
    "22": "Херсонська",
    "23": "Хмельницька",
    "24": "Черкаська",
    "25": "Чернівецька",
    "26": "Чернігівська",
}

# ─── Загрузка / сохранение данных ───────────────────────────────
def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

users_data: dict = load_data()


def get_user(user_id: int) -> dict:
    """Возвращает (создаёт при необходимости) запись пользователя."""
    key = str(user_id)
    if key not in users_data:
        users_data[key] = {"region": "21", "combos": []}  # Харьков по умолчанию
        save_data(users_data)
    return users_data[key]

# ─── Запрос к сайту ГСЦ ─────────────────────────────────────────
async def check_combination(region: str, combo: str) -> list[str]:
    url = "https://opendata.hsc.gov.ua/wp-admin/admin-ajax.php"
    payload = {
        "action": "get_license_plates",
        "region": region,
        "tsc": "",
        "vehicle_type": "",
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
            if data.get("success") and isinstance(data.get("data"), list):
                return data["data"]
            return []
    except Exception as e:
        logger.error(f"Ошибка запроса для '{combo}' (регион {region}): {e}")
        return []

# ─── Ежедневная проверка для всех пользователей ─────────────────
async def daily_check_all(app: Application):
    logger.info(f"Запускаю ежедневную проверку для {len(users_data)} пользователей")

    for user_id_str, data in users_data.items():
        user_id = int(user_id_str)
        combos = data.get("combos", [])
        region = data.get("region", "21")

        if not combos:
            continue

        found_any = False
        for combo in combos:
            results = await check_combination(region, combo)
            if results:
                found_any = True
                plates_str = "\n".join(f"  • {p}" for p in results)
                msg = (
                    f"🎉 <b>Найдены номера с комбинацией <code>{combo}</code>!</b>\n"
                    f"📍 Область: {REGIONS.get(region, '?')}\n\n"
                    f"{plates_str}\n\n"
                    f"🔗 <a href='https://opendata.hsc.gov.ua/check-leisure-license-plates/'>Открыть сайт</a>\n"
                    f"⏰ {datetime.now().strftime('%d.%m.%Y %H:%M')}"
                )
                try:
                    await app.bot.send_message(user_id, msg, parse_mode="HTML",
                                               disable_web_page_preview=True)
                except Exception as e:
                    logger.warning(f"Не смог отправить пользователю {user_id}: {e}")

        if not found_any:
            try:
                await app.bot.send_message(
                    user_id,
                    f"🔍 Ежедневная проверка ({REGIONS.get(region, '?')}) — "
                    f"ни одна из {len(combos)} комбинаций пока не доступна."
                )
            except Exception as e:
                logger.warning(f"Не смог отправить пользователю {user_id}: {e}")

# ─── Команды Telegram ────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    name = update.effective_user.first_name or "друг"
    await update.message.reply_text(
        f"👋 Привет, {name}!\n\n"
        "Я слежу за доступными номерными знаками на сайте ГСЦ МВС Украины "
        "и сообщу, как только появится нужная вам комбинация.\n\n"
        f"📍 Текущий регион: <b>{REGIONS.get(user['region'], '?')}</b>\n\n"
        "📋 <b>Команды:</b>\n"
        "/list — мои комбинации\n"
        "/add 1234 — добавить комбинацию\n"
        "/remove 1234 — удалить комбинацию\n"
        "/region — сменить область\n"
        "/check — проверить прямо сейчас\n"
        "/help — помощь",
        parse_mode="HTML"
    )

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    combos = user["combos"]
    region_name = REGIONS.get(user["region"], "?")

    if not combos:
        await update.message.reply_text(
            f"📋 Список пуст.\n📍 Регион: <b>{region_name}</b>\n\n"
            "Добавьте: /add 1234",
            parse_mode="HTML"
        )
        return

    items = "\n".join(f"  • <code>{c}</code>" for c in combos)
    await update.message.reply_text(
        f"📋 <b>Ваши комбинации ({len(combos)}):</b>\n{items}\n\n"
        f"📍 Регион: <b>{region_name}</b>",
        parse_mode="HTML"
    )

async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if not ctx.args:
        await update.message.reply_text("Укажите комбинацию: /add 1234")
        return
    combo = ctx.args[0].upper().strip()
    if combo in user["combos"]:
        await update.message.reply_text(f"⚠️ <code>{combo}</code> уже в списке.", parse_mode="HTML")
        return
    user["combos"].append(combo)
    save_data(users_data)
    await update.message.reply_text(f"✅ Добавлено: <code>{combo}</code>", parse_mode="HTML")

async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if not ctx.args:
        await update.message.reply_text("Укажите комбинацию: /remove 1234")
        return
    combo = ctx.args[0].upper().strip()
    if combo not in user["combos"]:
        await update.message.reply_text(f"❌ <code>{combo}</code> не найдено.", parse_mode="HTML")
        return
    user["combos"].remove(combo)
    save_data(users_data)
    await update.message.reply_text(f"🗑 Удалено: <code>{combo}</code>", parse_mode="HTML")

async def cmd_region(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)

    if not ctx.args:
        regions_list = "\n".join(f"  <code>{k}</code> — {v}" for k, v in REGIONS.items())
        await update.message.reply_text(
            f"📍 Текущий регион: <b>{REGIONS.get(user['region'], '?')}</b>\n\n"
            f"Чтобы сменить — отправьте номер из списка:\n"
            f"<i>пример: /region 21 (Харківська)</i>\n\n"
            f"{regions_list}",
            parse_mode="HTML"
        )
        return

    region_id = ctx.args[0].strip()
    if region_id not in REGIONS:
        await update.message.reply_text(
            "❌ Неверный номер. Используйте /region без параметров чтобы увидеть список."
        )
        return

    user["region"] = region_id
    save_data(users_data)
    await update.message.reply_text(
        f"✅ Регион изменён на: <b>{REGIONS[region_id]}</b>",
        parse_mode="HTML"
    )

async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    user_id = update.effective_user.id

    if not user["combos"]:
        await update.message.reply_text("📋 Список пуст. Добавьте: /add 1234")
        return

    await update.message.reply_text("🔍 Проверяю, подождите...")

    found_any = False
    region = user["region"]
    for combo in user["combos"]:
        results = await check_combination(region, combo)
        if results:
            found_any = True
            plates_str = "\n".join(f"  • {p}" for p in results)
            await ctx.bot.send_message(
                user_id,
                f"🎉 <b>Найдены номера с комбинацией <code>{combo}</code>!</b>\n"
                f"📍 {REGIONS.get(region, '?')}\n\n{plates_str}",
                parse_mode="HTML"
            )

    if not found_any:
        await update.message.reply_text(
            f"😕 Ничего не найдено в регионе <b>{REGIONS.get(region, '?')}</b>.\n"
            f"Проверено комбинаций: {len(user['combos'])}",
            parse_mode="HTML"
        )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 <b>Справка</b>\n\n"
        "Я проверяю сайт ГСЦ МВС каждый день в 09:00 (Киев) "
        "и сообщаю, если нужная комбинация цифр появилась в выбранной области.\n\n"
        "<b>Команды:</b>\n"
        "/list — мои комбинации\n"
        "/add 5000 — добавить комбинацию\n"
        "/remove 5000 — удалить\n"
        "/region — сменить область\n"
        "/check — проверить сейчас",
        parse_mode="HTML"
    )

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    total = len(users_data)
    active = sum(1 for u in users_data.values() if u.get("combos"))
    total_combos = sum(len(u.get("combos", [])) for u in users_data.values())
    await update.message.reply_text(
        f"📊 <b>Статистика</b>\n"
        f"Всего пользователей: {total}\n"
        f"Активных: {active}\n"
        f"Всего комбинаций: {total_combos}",
        parse_mode="HTML"
    )

# ─── Запуск ──────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("list",   cmd_list))
    app.add_handler(CommandHandler("add",    cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("region", cmd_region))
    app.add_handler(CommandHandler("check",  cmd_check))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("stats",  cmd_stats))

    scheduler = AsyncIOScheduler(timezone="Europe/Kiev")
    scheduler.add_job(
        daily_check_all,
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
