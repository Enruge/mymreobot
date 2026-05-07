import os
import json
import logging
import httpx
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ─── Логирование ────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── Переменные окружения ────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID  = int(os.environ.get("ADMIN_ID", "0"))

# ─── Хранилище данных ──────────────────────────────────────────────
# { user_id: {"region": "21", "combos": [...], "hour": 9, "minute": 0} }
DATA_FILE = "users.json"

REGIONS = {
    "1":  "АР Крим",            "2":  "Вінницька",       "3":  "Волинська",
    "4":  "Дніпропетровська",   "5":  "Донецька",        "6":  "Житомирська",
    "7":  "Закарпатська",       "8":  "Запорізька",      "9":  "Івано-Франківська",
    "10": "м. Київ",            "11": "Київська",        "12": "Кіровоградська",
    "13": "Луганська",          "14": "Львівська",       "15": "Миколаївська",
    "16": "Одеська",            "17": "Полтавська",      "18": "Рівненська",
    "19": "Сумська",            "20": "Тернопільська",   "21": "Харківська",
    "22": "Херсонська",         "23": "Хмельницька",     "24": "Черкаська",
    "25": "Чернівецька",        "26": "Чернігівська",
}

# ─── Загрузка / сохранение ──────────────────────────────────────
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
        users_data[key] = {
            "region": "21",
            "combos": [],
            "hour": 9,
            "minute": 0,
        }
        save_data(users_data)
    else:
        # Миграция старых записей — добавляем поля если их нет
        u = users_data[key]
        if "hour" not in u:
            u["hour"] = 9
        if "minute" not in u:
            u["minute"] = 0
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
        logger.error(f"Ошибка запроса для '{combo}': {e}")
        return []

# ─── Проверка для одного пользователя ────────────────────────────
async def check_for_user(app: Application, user_id: int):
    key = str(user_id)
    if key not in users_data:
        return
    data = users_data[key]
    combos = data.get("combos", [])
    region = data.get("region", "21")

    if not combos:
        return

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
                logger.warning(f"Не смог отправить {user_id}: {e}")

    if not found_any:
        try:
            await app.bot.send_message(
                user_id,
                f"🔍 Ежедневная проверка ({REGIONS.get(region, '?')}) — "
                f"ни одна из {len(combos)} комбинаций пока не доступна."
            )
        except Exception as e:
            logger.warning(f"Не смог отправить {user_id}: {e}")

# ─── Управление расписанием ─────────────────────────────────────
scheduler = AsyncIOScheduler(timezone="Europe/Kiev")


def schedule_user(app: Application, user_id: int):
    """Создаёт/обновляет ежедневную задачу для пользователя."""
    key = str(user_id)
    if key not in users_data:
        return
    data = users_data[key]
    hour = data.get("hour", 9)
    minute = data.get("minute", 0)

    job_id = f"user_{user_id}"
    # Удаляем старую задачу если была
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    scheduler.add_job(
        check_for_user,
        trigger="cron",
        hour=hour, minute=minute,
        args=[app, user_id],
        id=job_id,
        replace_existing=True,
    )
    logger.info(f"Расписание для {user_id}: каждый день в {hour:02d}:{minute:02d}")


def schedule_all_users(app: Application):
    """Создаёт задачи для всех пользователей при старте."""
    for user_id_str in users_data:
        schedule_user(app, int(user_id_str))

# ─── Команды Telegram ────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    save_data(users_data)
    schedule_user(ctx.application, update.effective_user.id)

    name = update.effective_user.first_name or "друг"
    await update.message.reply_text(
        f"👋 Привет, {name}!\n\n"
        "Я слежу за доступными номерными знаками на сайте ГСЦ МВС "
        "и сообщу, как только появится нужная вам комбинация.\n\n"
        f"📍 Регион: <b>{REGIONS.get(user['region'], '?')}</b>\n"
        f"⏰ Время уведомлений: <b>{user['hour']:02d}:{user['minute']:02d}</b> (Киев)\n\n"
        "📋 <b>Команды:</b>\n"
        "/list — мои комбинации\n"
        "/add 1234 — добавить комбинацию\n"
        "/remove 1234 — удалить комбинацию\n"
        "/region — сменить область\n"
        "/time 18:30 — установить время уведомлений\n"
        "/check — проверить прямо сейчас\n"
        "/help — помощь",
        parse_mode="HTML"
    )

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    combos = user["combos"]
    region_name = REGIONS.get(user["region"], "?")
    time_str = f"{user['hour']:02d}:{user['minute']:02d}"

    if not combos:
        await update.message.reply_text(
            f"📋 Список пуст.\n"
            f"📍 Регион: <b>{region_name}</b>\n"
            f"⏰ Время: <b>{time_str}</b>\n\n"
            "Добавьте: /add 1234",
            parse_mode="HTML"
        )
        return

    items = "\n".join(f"  • <code>{c}</code>" for c in combos)
    await update.message.reply_text(
        f"📋 <b>Ваши комбинации ({len(combos)}):</b>\n{items}\n\n"
        f"📍 Регион: <b>{region_name}</b>\n"
        f"⏰ Время: <b>{time_str}</b> (Киев)",
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
    # Создаём расписание если ещё не было
    schedule_user(ctx.application, update.effective_user.id)
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
        await update.message.reply_text("❌ Неверный номер. Используйте /region без параметров.")
        return

    user["region"] = region_id
    save_data(users_data)
    await update.message.reply_text(
        f"✅ Регион изменён на: <b>{REGIONS[region_id]}</b>",
        parse_mode="HTML"
    )

async def cmd_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)

    if not ctx.args:
        await update.message.reply_text(
            f"⏰ Текущее время уведомлений: <b>{user['hour']:02d}:{user['minute']:02d}</b> (Киев)\n\n"
            "Чтобы изменить, отправьте время в формате <b>ЧЧ:ММ</b>.\n"
            "<i>Примеры:</i>\n"
            "  /time 09:00\n"
            "  /time 18:30\n"
            "  /time 22:15",
            parse_mode="HTML"
        )
        return

    time_str = ctx.args[0].strip()
    # Допускаем форматы 9:00, 09:00, 9.00, 0900
    try:
        if ":" in time_str:
            h, m = time_str.split(":")
        elif "." in time_str:
            h, m = time_str.split(".")
        elif len(time_str) == 4 and time_str.isdigit():
            h, m = time_str[:2], time_str[2:]
        else:
            raise ValueError("неверный формат")

        hour = int(h)
        minute = int(m)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("значения вне диапазона")
    except Exception:
        await update.message.reply_text(
            "❌ Неверный формат времени.\n"
            "Используйте ЧЧ:ММ — например <code>/time 18:30</code>",
            parse_mode="HTML"
        )
        return

    user["hour"] = hour
    user["minute"] = minute
    save_data(users_data)
    schedule_user(ctx.application, update.effective_user.id)

    await update.message.reply_text(
        f"✅ Время уведомлений установлено: <b>{hour:02d}:{minute:02d}</b> (Киев)\n"
        f"Буду писать вам каждый день в это время.",
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
        "Я проверяю сайт ГСЦ МВС каждый день в установленное вами время "
        "и сообщаю, если нужная комбинация цифр появилась в выбранной области.\n\n"
        "<b>Команды:</b>\n"
        "/list — мои комбинации\n"
        "/add 5000 — добавить комбинацию\n"
        "/remove 5000 — удалить\n"
        "/region — сменить область\n"
        "/time 18:30 — установить время уведомлений\n"
        "/check — проверить прямо сейчас",
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
        f"Пользователей всего: {total}\n"
        f"Активных: {active}\n"
        f"Комбинаций всего: {total_combos}",
        parse_mode="HTML"
    )

async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Список всех пользователей с настройками — только для админа."""
    if update.effective_user.id != ADMIN_ID:
        return

    if not users_data:
        await update.message.reply_text("👥 Пока нет пользователей.")
        return

    lines = [f"👥 <b>Пользователи ({len(users_data)}):</b>\n"]

    for i, (uid_str, data) in enumerate(users_data.items(), start=1):
        uid = int(uid_str)
        combos = data.get("combos", [])
        region = REGIONS.get(data.get("region", "21"), "?")
        h = data.get("hour", 9)
        m = data.get("minute", 0)

        # Пытаемся узнать имя пользователя
        try:
            chat = await ctx.bot.get_chat(uid)
            name = chat.first_name or ""
            if chat.last_name:
                name += f" {chat.last_name}"
            if chat.username:
                name += f" (@{chat.username})"
            if not name.strip():
                name = "—"
        except Exception:
            name = "— (нет доступа)"

        combos_str = ", ".join(f"<code>{c}</code>" for c in combos) if combos else "<i>пусто</i>"

        lines.append(
            f"<b>{i}. {name}</b>\n"
            f"   ID: <code>{uid}</code>\n"
            f"   📍 {region}\n"
            f"   ⏰ {h:02d}:{m:02d}\n"
            f"   🔢 Комбинации: {combos_str}\n"
        )

    # Telegram ограничивает сообщения 4096 символами — разбиваем при необходимости
    text = "\n".join(lines)
    if len(text) <= 4000:
        await update.message.reply_text(text, parse_mode="HTML")
    else:
        # Отправляем частями
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) > 4000:
                await update.message.reply_text(chunk, parse_mode="HTML")
                chunk = ""
            chunk += line + "\n"
        if chunk:
            await update.message.reply_text(chunk, parse_mode="HTML")

async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Рассылка сообщения всем пользователям бота.
    Только для админа.

    Способы использования:
      1) /broadcast Привет всем!
      2) Ответом (reply) на любое сообщение — отправит его как есть (с фото/стикером и т.д.)
    """
    if update.effective_user.id != ADMIN_ID:
        return

    # Случай 1: reply на сообщение — пересылаем его копию
    reply = update.message.reply_to_message
    text_msg = " ".join(ctx.args) if ctx.args else None

    if not reply and not text_msg:
        await update.message.reply_text(
            "📢 <b>Рассылка</b>\n\n"
            "Способы:\n"
            "• <code>/broadcast Текст сообщения</code>\n"
            "• Ответьте (reply) на любое сообщение командой <code>/broadcast</code>\n"
            "  — будет разослано как есть (фото, видео, файлы тоже работают)",
            parse_mode="HTML"
        )
        return

    if not users_data:
        await update.message.reply_text("👥 Нет пользователей для рассылки.")
        return

    sent = 0
    failed = 0
    blocked = 0

    status_msg = await update.message.reply_text(
        f"📤 Отправляю {len(users_data)} пользователям..."
    )

    for uid_str in list(users_data.keys()):
        uid = int(uid_str)
        try:
            if reply:
                # Копируем оригинальное сообщение (с любым типом контента)
                await ctx.bot.copy_message(
                    chat_id=uid,
                    from_chat_id=update.effective_chat.id,
                    message_id=reply.message_id,
                )
            else:
                await ctx.bot.send_message(
                    uid,
                    f"📢 <b>Сообщение от администратора:</b>\n\n{text_msg}",
                    parse_mode="HTML"
                )
            sent += 1
        except Exception as e:
            err_text = str(e).lower()
            if "blocked" in err_text or "forbidden" in err_text or "deactivated" in err_text:
                blocked += 1
            else:
                failed += 1
            logger.warning(f"Не отправлено {uid}: {e}")

    await status_msg.edit_text(
        f"📊 <b>Рассылка завершена</b>\n\n"
        f"✅ Доставлено: {sent}\n"
        f"🚫 Заблокировали бота: {blocked}\n"
        f"❌ Ошибки: {failed}",
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
    app.add_handler(CommandHandler("time",   cmd_time))
    app.add_handler(CommandHandler("check",  cmd_check))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("stats",  cmd_stats))
    app.add_handler(CommandHandler("users",  cmd_users))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))

    async def on_startup(app):
        scheduler.start()
        schedule_all_users(app)
        logger.info(f"Бот запущен. Создано задач: {len(scheduler.get_jobs())}")

    app.post_init = on_startup
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
