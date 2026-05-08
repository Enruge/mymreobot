import os
import json
import logging
from datetime import datetime, date
from urllib.parse import quote
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    CallbackQueryHandler, MessageHandler, filters
)

# ─── Логирование ────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── Переменные окружения ────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID  = int(os.environ.get("ADMIN_ID", "0"))

DATA_FILE = "users.json"
SITE_URL = "https://opendata.hsc.gov.ua/check-leisure-license-plates/"

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

waiting: dict[int, str] = {}

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
    key = str(user_id)
    if key not in users_data:
        users_data[key] = {
            "region": "21",
            "combos": [],
            "hour": 9,
            "minute": 0,
            "checked_today": "",  # дата последнего "проверил"
        }
        save_data(users_data)
    else:
        u = users_data[key]
        u.setdefault("hour", 9)
        u.setdefault("minute", 0)
        u.setdefault("checked_today", "")
    return users_data[key]


# ═══════════════════════════════════════════════════════════════
#                        НАПОМИНАНИЕ
# ═══════════════════════════════════════════════════════════════

async def send_reminder(app: Application, user_id: int):
    """Отправляет напоминание о проверке."""
    key = str(user_id)
    if key not in users_data:
        return
    data = users_data[key]
    combos = data.get("combos", [])

    if not combos:
        return  # нечего напоминать

    # Если уже отметил «проверил» сегодня — не дублируем
    today = date.today().isoformat()
    if data.get("checked_today") == today:
        logger.info(f"{user_id}: уже проверил сегодня, пропускаем")
        return

    region_name = REGIONS.get(data.get("region", "21"), "?")
    combos_str = "\n".join(f"  • <code>{c}</code>" for c in combos)

    msg = (
        f"☀️ <b>Доброе утро! Время проверить номера.</b>\n\n"
        f"📍 Регион: <b>{region_name}</b>\n"
        f"🔢 Ваши комбинации:\n{combos_str}\n\n"
        f"👉 Нажмите кнопку, чтобы открыть сайт:"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Открыть сайт МРЕО", url=SITE_URL)],
        [InlineKeyboardButton("✅ Проверил, всё ок", callback_data="checked:done")],
        [InlineKeyboardButton("⏰ Напомнить через час", callback_data="checked:later")],
    ])

    try:
        await app.bot.send_message(user_id, msg, parse_mode="HTML",
                                   reply_markup=kb, disable_web_page_preview=True)
        logger.info(f"Напоминание отправлено {user_id}")
    except Exception as e:
        logger.warning(f"Не смог отправить {user_id}: {e}")


# ─── Расписание ──────────────────────────────────────────────────
scheduler = AsyncIOScheduler(timezone="Europe/Kiev")


def schedule_user(app: Application, user_id: int):
    key = str(user_id)
    if key not in users_data:
        return
    data = users_data[key]
    job_id = f"user_{user_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(
        send_reminder,
        trigger="cron",
        hour=data.get("hour", 9),
        minute=data.get("minute", 0),
        args=[app, user_id],
        id=job_id,
        replace_existing=True,
    )

def schedule_all_users(app: Application):
    for user_id_str in users_data:
        schedule_user(app, int(user_id_str))


def schedule_remind_later(app: Application, user_id: int):
    """Разовое напоминание через 1 час."""
    from datetime import timedelta
    job_id = f"later_{user_id}_{datetime.now().timestamp()}"
    scheduler.add_job(
        send_reminder,
        trigger="date",
        run_date=datetime.now() + timedelta(hours=1),
        args=[app, user_id],
        id=job_id,
    )


# ═══════════════════════════════════════════════════════════════
#                          КЛАВИАТУРЫ
# ═══════════════════════════════════════════════════════════════

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Мои комбинации", callback_data="menu:list")],
        [
            InlineKeyboardButton("➕ Добавить", callback_data="menu:add"),
            InlineKeyboardButton("🗑 Удалить", callback_data="menu:remove"),
        ],
        [InlineKeyboardButton("📍 Сменить регион", callback_data="menu:region")],
        [InlineKeyboardButton("⏰ Время напоминания", callback_data="menu:time")],
        [InlineKeyboardButton("🌐 Открыть сайт МРЕО", url=SITE_URL)],
    ])

def remove_kb(user: dict) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"🗑 {c}", callback_data=f"del:{c}")] for c in user["combos"]]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)

def list_kb(user: dict) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"🗑 Удалить {c}", callback_data=f"del:{c}")] for c in user["combos"]]
    rows.append([
        InlineKeyboardButton("➕ Добавить", callback_data="menu:add"),
        InlineKeyboardButton("⬅️ Назад", callback_data="menu:home"),
    ])
    return InlineKeyboardMarkup(rows)

def regions_kb(current: str) -> InlineKeyboardMarkup:
    rows = []
    items = list(REGIONS.items())
    for i in range(0, len(items), 2):
        row = []
        for k, v in items[i:i+2]:
            mark = "✅ " if k == current else ""
            row.append(InlineKeyboardButton(f"{mark}{v}", callback_data=f"reg:{k}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)

def time_kb() -> InlineKeyboardMarkup:
    presets = ["07:00", "09:00", "12:00", "15:00", "18:00", "21:00"]
    rows = []
    for i in range(0, len(presets), 3):
        rows.append([InlineKeyboardButton(t, callback_data=f"time:{t}") for t in presets[i:i+3]])
    rows.append([InlineKeyboardButton("✏️ Ввести вручную", callback_data="time:custom")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)

def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")]])

def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="menu:home")]])


# ═══════════════════════════════════════════════════════════════
#                          ТЕКСТЫ
# ═══════════════════════════════════════════════════════════════

def home_text(user: dict) -> str:
    return (
        f"🤖 <b>МРЕО Помощник</b>\n\n"
        f"Я буду напоминать вам каждый день в установленное время "
        f"проверить нужные комбинации номеров на сайте МРЕО.\n\n"
        f"📍 Регион: <b>{REGIONS.get(user['region'], '?')}</b>\n"
        f"⏰ Напоминание: <b>{user['hour']:02d}:{user['minute']:02d}</b> (Киев)\n"
        f"🔢 Комбинаций: <b>{len(user['combos'])}</b>\n\n"
        f"Выберите действие:"
    )

def list_text(user: dict) -> str:
    if not user["combos"]:
        return (
            f"📋 <b>Список пуст</b>\n\n"
            f"📍 Регион: <b>{REGIONS.get(user['region'], '?')}</b>\n"
            f"⏰ Время: <b>{user['hour']:02d}:{user['minute']:02d}</b>\n\n"
            f"Нажмите «➕ Добавить», чтобы добавить комбинацию для отслеживания."
        )
    items = "\n".join(f"  • <code>{c}</code>" for c in user["combos"])
    return (
        f"📋 <b>Ваши комбинации ({len(user['combos'])}):</b>\n{items}\n\n"
        f"📍 <b>{REGIONS.get(user['region'], '?')}</b>   "
        f"⏰ <b>{user['hour']:02d}:{user['minute']:02d}</b>"
    )


# ═══════════════════════════════════════════════════════════════
#                          КОМАНДЫ
# ═══════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    save_data(users_data)
    schedule_user(ctx.application, update.effective_user.id)
    waiting.pop(update.effective_user.id, None)

    name = update.effective_user.first_name or "друг"
    await update.message.reply_text(
        f"👋 Привет, {name}!\n\n"
        + home_text(user),
        parse_mode="HTML",
        reply_markup=main_menu_kb()
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    waiting.pop(update.effective_user.id, None)
    await update.message.reply_text(
        home_text(user), parse_mode="HTML", reply_markup=main_menu_kb()
    )

async def cmd_remind(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Прислать напоминание прямо сейчас (для тестирования)."""
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user["combos"]:
        await update.message.reply_text(
            "📋 Список пуст. Сначала добавьте комбинацию.",
            reply_markup=main_menu_kb()
        )
        return
    # Сбрасываем "проверил сегодня" чтобы напоминание точно пришло
    user["checked_today"] = ""
    save_data(users_data)
    await send_reminder(ctx.application, user_id)

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
        f"Комбинаций всего: {total_combos}",
        parse_mode="HTML"
    )

async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
        h, m = data.get("hour", 9), data.get("minute", 0)
        try:
            chat = await ctx.bot.get_chat(uid)
            name = chat.first_name or ""
            if chat.last_name: name += f" {chat.last_name}"
            if chat.username:  name += f" (@{chat.username})"
            if not name.strip(): name = "—"
        except Exception:
            name = "— (нет доступа)"
        combos_str = ", ".join(f"<code>{c}</code>" for c in combos) if combos else "<i>пусто</i>"
        lines.append(
            f"<b>{i}. {name}</b>\n"
            f"   ID: <code>{uid}</code>\n"
            f"   📍 {region}   ⏰ {h:02d}:{m:02d}\n"
            f"   🔢 {combos_str}\n"
        )

    chunk = ""
    for line in lines:
        if len(chunk) + len(line) > 4000:
            await update.message.reply_text(chunk, parse_mode="HTML")
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await update.message.reply_text(chunk, parse_mode="HTML")

async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    reply = update.message.reply_to_message
    text_msg = " ".join(ctx.args) if ctx.args else None

    if not reply and not text_msg:
        await update.message.reply_text(
            "📢 <b>Рассылка</b>\n\n"
            "• <code>/broadcast Текст</code>\n"
            "• Reply на любое сообщение командой <code>/broadcast</code>",
            parse_mode="HTML"
        )
        return

    if not users_data:
        await update.message.reply_text("👥 Нет пользователей.")
        return

    sent = failed = blocked = 0
    status = await update.message.reply_text(f"📤 Отправляю {len(users_data)} пользователям...")

    for uid_str in list(users_data.keys()):
        uid = int(uid_str)
        try:
            if reply:
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
            err = str(e).lower()
            if any(x in err for x in ("blocked", "forbidden", "deactivated")):
                blocked += 1
            else:
                failed += 1

    await status.edit_text(
        f"📊 <b>Рассылка завершена</b>\n\n"
        f"✅ Доставлено: {sent}\n"
        f"🚫 Заблокировали: {blocked}\n"
        f"❌ Ошибки: {failed}",
        parse_mode="HTML"
    )


# ═══════════════════════════════════════════════════════════════
#                  ОБРАБОТЧИКИ КНОПОК
# ═══════════════════════════════════════════════════════════════

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    user = get_user(user_id)
    data = q.data

    if data == "menu:home":
        waiting.pop(user_id, None)
        await q.edit_message_text(home_text(user), parse_mode="HTML", reply_markup=main_menu_kb())

    elif data == "menu:list":
        await q.edit_message_text(
            list_text(user), parse_mode="HTML",
            reply_markup=list_kb(user) if user["combos"] else back_kb()
        )

    elif data == "menu:add":
        waiting[user_id] = "add"
        await q.edit_message_text(
            "➕ <b>Добавление комбинации</b>\n\n"
            "Просто отправьте искомую комбинацию следующим сообщением.\n"
            "<i>Например: 5000 или AA1234</i>",
            parse_mode="HTML",
            reply_markup=cancel_kb()
        )

    elif data == "menu:remove":
        if not user["combos"]:
            await q.edit_message_text("📋 Список пуст.", reply_markup=back_kb())
        else:
            await q.edit_message_text(
                "🗑 <b>Удаление</b>\n\nВыберите для удаления:",
                parse_mode="HTML", reply_markup=remove_kb(user)
            )

    elif data == "menu:region":
        await q.edit_message_text(
            f"📍 <b>Выбор региона</b>\n\nТекущий: <b>{REGIONS.get(user['region'], '?')}</b>",
            parse_mode="HTML", reply_markup=regions_kb(user["region"])
        )

    elif data == "menu:time":
        await q.edit_message_text(
            f"⏰ <b>Время напоминания</b>\n\n"
            f"Текущее: <b>{user['hour']:02d}:{user['minute']:02d}</b> (Киев)\n\n"
            f"Выберите готовое или введите своё:",
            parse_mode="HTML", reply_markup=time_kb()
        )

    elif data.startswith("del:"):
        combo = data[4:]
        if combo in user["combos"]:
            user["combos"].remove(combo)
            save_data(users_data)
            await q.answer(f"Удалено: {combo}", show_alert=False)
        if user["combos"]:
            await q.edit_message_text(
                "🗑 <b>Удаление</b>\n\nВыберите для удаления:",
                parse_mode="HTML", reply_markup=remove_kb(user)
            )
        else:
            await q.edit_message_text("✅ Все комбинации удалены.", reply_markup=back_kb())

    elif data.startswith("reg:"):
        region_id = data[4:]
        if region_id in REGIONS:
            user["region"] = region_id
            save_data(users_data)
            await q.answer(f"Регион: {REGIONS[region_id]}", show_alert=False)
            await q.edit_message_text(
                f"📍 <b>Выбор региона</b>\n\nТекущий: <b>{REGIONS[region_id]}</b>",
                parse_mode="HTML", reply_markup=regions_kb(region_id)
            )

    elif data.startswith("time:"):
        val = data[5:]
        if val == "custom":
            waiting[user_id] = "time"
            await q.edit_message_text(
                "✏️ <b>Своё время</b>\n\nОтправьте в формате <b>ЧЧ:ММ</b>.\n"
                "<i>Например: 18:30</i>",
                parse_mode="HTML", reply_markup=cancel_kb()
            )
        else:
            try:
                h, m = val.split(":")
                user["hour"] = int(h)
                user["minute"] = int(m)
                save_data(users_data)
                schedule_user(ctx.application, user_id)
                await q.answer(f"Время: {val}", show_alert=False)
                await q.edit_message_text(
                    f"⏰ Установлено: <b>{val}</b> (Киев)",
                    parse_mode="HTML", reply_markup=time_kb()
                )
            except Exception:
                await q.answer("Ошибка", show_alert=True)

    # ─── Обработка кнопок напоминания ──────────────
    elif data == "checked:done":
        user["checked_today"] = date.today().isoformat()
        save_data(users_data)
        await q.edit_message_text(
            "👍 <b>Отлично!</b>\n\n"
            "Отметил, что вы уже проверили сегодня. "
            "Следующее напоминание — завтра в установленное время.",
            parse_mode="HTML"
        )

    elif data == "checked:later":
        schedule_remind_later(ctx.application, user_id)
        await q.edit_message_text(
            "⏰ Хорошо, напомню через час.",
            parse_mode="HTML"
        )


# ═══════════════════════════════════════════════════════════════
#                  ТЕКСТОВЫЕ СООБЩЕНИЯ
# ═══════════════════════════════════════════════════════════════

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    text = (update.message.text or "").strip()
    state = waiting.get(user_id)

    if state == "add":
        combo = text.upper()
        if not combo:
            return
        if combo in user["combos"]:
            await update.message.reply_text(
                f"⚠️ <code>{combo}</code> уже в списке.",
                parse_mode="HTML", reply_markup=main_menu_kb()
            )
        else:
            user["combos"].append(combo)
            save_data(users_data)
            schedule_user(ctx.application, user_id)
            await update.message.reply_text(
                f"✅ Добавлено: <code>{combo}</code>\n\n" + home_text(user),
                parse_mode="HTML", reply_markup=main_menu_kb()
            )
        waiting.pop(user_id, None)
        return

    if state == "time":
        try:
            if ":" in text: h, m = text.split(":")
            elif "." in text: h, m = text.split(".")
            elif len(text) == 4 and text.isdigit(): h, m = text[:2], text[2:]
            else: raise ValueError
            hour, minute = int(h), int(m)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except Exception:
            await update.message.reply_text(
                "❌ Неверный формат. Пример: <code>18:30</code>",
                parse_mode="HTML", reply_markup=cancel_kb()
            )
            return
        user["hour"] = hour
        user["minute"] = minute
        save_data(users_data)
        schedule_user(ctx.application, user_id)
        waiting.pop(user_id, None)
        await update.message.reply_text(
            f"✅ Время напоминания: <b>{hour:02d}:{minute:02d}</b> (Киев)\n\n" + home_text(user),
            parse_mode="HTML", reply_markup=main_menu_kb()
        )
        return

    await update.message.reply_text(
        home_text(user), parse_mode="HTML", reply_markup=main_menu_kb()
    )


# ═══════════════════════════════════════════════════════════════
#                          ЗАПУСК
# ═══════════════════════════════════════════════════════════════

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("menu",   cmd_help))
    app.add_handler(CommandHandler("remind", cmd_remind))
    app.add_handler(CommandHandler("stats",  cmd_stats))
    app.add_handler(CommandHandler("users",  cmd_users))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))

    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    async def on_startup(app):
        scheduler.start()
        schedule_all_users(app)
        logger.info(f"Бот запущен. Задач: {len(scheduler.get_jobs())}")

    app.post_init = on_startup
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
