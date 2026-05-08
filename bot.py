import os
import json
import logging
import asyncio
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    CallbackQueryHandler, MessageHandler, filters
)
from playwright.async_api import async_playwright

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

# Названия областей (точно как на сайте — для выбора в dropdown)
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
        users_data[key] = {"region": "21", "combos": [], "hour": 9, "minute": 0}
        save_data(users_data)
    else:
        u = users_data[key]
        if "hour" not in u: u["hour"] = 9
        if "minute" not in u: u["minute"] = 0
    return users_data[key]

# ═══════════════════════════════════════════════════════════════
#                     ПРОВЕРКА ЧЕРЕЗ PLAYWRIGHT
# ═══════════════════════════════════════════════════════════════

# Глобальный браузер - запускаем один раз и переиспользуем
_browser = None
_playwright = None
_browser_lock = asyncio.Lock()


async def get_browser():
    """Возвращает браузер, запуская его при первом обращении."""
    global _browser, _playwright
    if _browser is None or not _browser.is_connected():
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ]
        )
        logger.info("Браузер запущен")
    return _browser


async def check_combination(region_id: str, combination: str) -> tuple[list[str] | None, str]:
    """
    Открывает сайт, выбирает область и комбинацию, читает результаты.
    Возвращает (список_номеров, статус).
    """
    region_name = REGIONS.get(region_id, "")
    if not region_name:
        return None, "❌ Неверный регион"

    async with _browser_lock:
        try:
            browser = await get_browser()
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0.0.0 Safari/537.36",
                locale="uk-UA",
                viewport={"width": 1280, "height": 800},
            )
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )

            page = await context.new_page()
            try:
                logger.info(f"Открываю сайт для {region_name} / {combination}")
                await page.goto(SITE_URL, wait_until="networkidle", timeout=60000)
                await page.wait_for_timeout(5000)

                # ─── 1. ВЫБОР ОБЛАСТИ ──────────────────────────────
                # Пробуем разные варианты: реальный select, кастомный dropdown, datalist
                selected = False

                # Вариант A: настоящий <select>
                try:
                    sel = await page.query_selector("select")
                    if sel:
                        await sel.select_option(label=region_name)
                        selected = True
                        logger.info("Выбор области: через <select>")
                except Exception as e:
                    logger.info(f"select не сработал: {e}")

                # Вариант B: кастомный dropdown — кликаем по элементу с текстом
                if not selected:
                    try:
                        # Ищем кликабельный элемент рядом с заголовком "Оберіть область"
                        # Часто это div, который при клике открывает список
                        await page.evaluate("""() => {
                            const headers = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6,label,div,span')];
                            const h = headers.find(el => el.textContent.includes('Оберіть область'));
                            if (h) {
                                let next = h.nextElementSibling;
                                while (next && !next.click) next = next.nextElementSibling;
                                if (next) next.click();
                            }
                        }""")
                        await page.wait_for_timeout(1000)

                        # После клика должен появиться список — кликаем по нужной области
                        clicked = await page.evaluate(f"""(name) => {{
                            const items = [...document.querySelectorAll('li, div, span, a, option')];
                            const item = items.find(el =>
                                el.textContent.trim() === name &&
                                el.offsetParent !== null
                            );
                            if (item) {{ item.click(); return true; }}
                            return false;
                        }}""", region_name)

                        if clicked:
                            selected = True
                            logger.info("Выбор области: через клик по элементу")
                    except Exception as e:
                        logger.info(f"клик-вариант не сработал: {e}")

                if not selected:
                    # Сохраняем HTML страницы для диагностики
                    html = await page.content()
                    logger.error(f"Не нашли выбор области. HTML начало: {html[:2000]}")
                    return None, "⚠️ Не удалось найти выбор области (структура сайта изменилась)"

                await page.wait_for_timeout(2000)

                # ─── 2. ВВОД КОМБИНАЦИИ ──────────────────────────
                filled = False
                # Ищем последнее поле для ввода (обычно это и есть "Бажана комбінація")
                try:
                    inputs = await page.query_selector_all("input[type='text'], input:not([type])")
                    if inputs:
                        await inputs[-1].fill(combination)
                        filled = True
                        logger.info(f"Заполнили поле через query_selector ({len(inputs)} inputs)")
                except Exception as e:
                    logger.info(f"fill не сработал: {e}")

                if not filled:
                    return None, "⚠️ Не удалось ввести комбинацию"

                await page.wait_for_timeout(1000)

                # ─── 3. КЛИК ПО КНОПКЕ ПОИСКА ─────────────────────
                clicked = False
                # Пробуем разные варианты кнопки
                for selector in [
                    "button[type='submit']",
                    "input[type='submit']",
                    "button:has-text('Пошук')",
                    "button:has-text('Знайти')",
                    ".btn-search",
                    "#search",
                ]:
                    try:
                        btn = await page.query_selector(selector)
                        if btn and await btn.is_visible():
                            await btn.click()
                            clicked = True
                            logger.info(f"Кликнули кнопку: {selector}")
                            break
                    except Exception:
                        continue

                if not clicked:
                    # Пробуем найти любую кнопку с подходящим текстом
                    try:
                        await page.evaluate("""() => {
                            const btns = [...document.querySelectorAll('button, input[type=button], a.btn, div.btn')];
                            const b = btns.find(el => /пошук|знайти|шукати|search/i.test(el.textContent));
                            if (b) b.click();
                        }""")
                        clicked = True
                        logger.info("Клик через JavaScript-поиск")
                    except Exception:
                        pass

                if not clicked:
                    await page.keyboard.press("Enter")

                # ─── 4. ОЖИДАНИЕ И ЧТЕНИЕ РЕЗУЛЬТАТОВ ──────────────
                await page.wait_for_timeout(7000)

                plates = []
                # Пробуем таблицу
                rows = await page.query_selector_all("table tbody tr")
                for row in rows:
                    cells = await row.query_selector_all("td")
                    for cell in cells:
                        text = (await cell.inner_text()).strip()
                        # Похоже на номер: содержит буквы или цифры, длина >= 4
                        if text and 4 <= len(text) <= 20 and not text.startswith("№"):
                            plates.append(text)

                # Если таблицы нет — ищем текст похожий на номера
                if not plates:
                    page_text = await page.inner_text("body")
                    import re
                    # Украинские номера: 2 буквы + 4 цифры + 2 буквы или похожие форматы
                    matches = re.findall(r'\b[А-ЯA-Z]{2}\d{4}[А-ЯA-Z]{2}\b', page_text)
                    plates.extend(matches)

                # Уникальные
                seen = set()
                unique = []
                for p in plates:
                    if p not in seen:
                        seen.add(p)
                        unique.append(p)

                logger.info(f"Найдено номеров: {len(unique)}")
                return unique, "ok"

            finally:
                await context.close()

        except Exception as e:
            logger.exception(f"Playwright ошибка: {e}")
            return None, f"⚠️ Ошибка: {type(e).__name__}: {str(e)[:100]}"


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
    errors = 0
    for combo in combos:
        results, status = await check_combination(region, combo)
        if status != "ok":
            errors += 1
            continue
        if results:
            found_any = True
            plates_str = "\n".join(f"  • {p}" for p in results[:30])
            more = f"\n<i>...и ещё {len(results)-30}</i>" if len(results) > 30 else ""
            msg = (
                f"🎉 <b>Найдены номера с комбинацией <code>{combo}</code>!</b>\n"
                f"📍 Область: {REGIONS.get(region, '?')}\n\n"
                f"{plates_str}{more}\n\n"
                f"🔗 <a href='{SITE_URL}'>Открыть сайт</a>\n"
                f"⏰ {datetime.now().strftime('%d.%m.%Y %H:%M')}"
            )
            try:
                await app.bot.send_message(user_id, msg, parse_mode="HTML",
                                           disable_web_page_preview=True)
            except Exception as e:
                logger.warning(f"Не смог отправить {user_id}: {e}")

    if not found_any and errors == 0:
        try:
            await app.bot.send_message(
                user_id,
                f"🔍 Ежедневная проверка ({REGIONS.get(region, '?')}) — "
                f"ни одна из {len(combos)} комбинаций пока не доступна."
            )
        except Exception as e:
            logger.warning(f"Не смог отправить {user_id}: {e}")
    elif errors:
        try:
            await app.bot.send_message(
                user_id,
                f"⚠️ Проверка завершилась с ошибками ({errors} из {len(combos)}). "
                f"Сайт мог временно блокировать автоматические запросы. Попробуйте позже."
            )
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
        check_for_user,
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
        [InlineKeyboardButton("⏰ Время уведомлений", callback_data="menu:time")],
        [InlineKeyboardButton("🔍 Проверить сейчас", callback_data="menu:check")],
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
#                           ТЕКСТЫ
# ═══════════════════════════════════════════════════════════════

def home_text(user: dict) -> str:
    return (
        f"🤖 <b>МРЕО Монитор</b>\n\n"
        f"📍 Регион: <b>{REGIONS.get(user['region'], '?')}</b>\n"
        f"⏰ Уведомления: <b>{user['hour']:02d}:{user['minute']:02d}</b> (Киев)\n"
        f"🔢 Отслеживается комбинаций: <b>{len(user['combos'])}</b>\n\n"
        f"Выберите действие:"
    )

def list_text(user: dict) -> str:
    if not user["combos"]:
        return (
            f"📋 <b>Список пуст</b>\n\n"
            f"📍 Регион: <b>{REGIONS.get(user['region'], '?')}</b>\n"
            f"⏰ Время: <b>{user['hour']:02d}:{user['minute']:02d}</b>\n\n"
            f"Нажмите «➕ Добавить» чтобы добавить комбинацию."
        )
    items = "\n".join(f"  • <code>{c}</code>" for c in user["combos"])
    return (
        f"📋 <b>Ваши комбинации ({len(user['combos'])}):</b>\n{items}\n\n"
        f"📍 <b>{REGIONS.get(user['region'], '?')}</b>   "
        f"⏰ <b>{user['hour']:02d}:{user['minute']:02d}</b>\n\n"
        f"Нажмите на кнопку, чтобы удалить:"
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
        f"Я слежу за номерными знаками на сайте ГСЦ МВС Украины.\n\n"
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

async def cmd_diag(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Диагностика — открывает сайт и присылает скриншот. Только для админа."""
    if update.effective_user.id != ADMIN_ID:
        return

    region_id = ctx.args[0] if ctx.args else "21"
    combo = ctx.args[1] if len(ctx.args) > 1 else "0320"

    region_name = REGIONS.get(region_id, "")
    if not region_name:
        await update.message.reply_text(f"❌ Неверный регион: {region_id}")
        return

    await update.message.reply_text(f"🔬 Диагностика: {region_name} / {combo}\nЭто займёт ~30 сек...")

    async with _browser_lock:
        try:
            browser = await get_browser()
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0.0.0 Safari/537.36",
                locale="uk-UA",
                viewport={"width": 1280, "height": 1600},
            )
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = await context.new_page()
            try:
                await page.goto(SITE_URL, wait_until="networkidle", timeout=60000)
                await page.wait_for_timeout(5000)

                # Сохраняем скриншот ДО любых действий
                screenshot1 = await page.screenshot(full_page=True)
                await ctx.bot.send_photo(
                    update.effective_user.id,
                    photo=screenshot1,
                    caption="📸 Скриншот 1: страница сразу после загрузки"
                )

                # Считаем элементы
                stats = await page.evaluate("""() => {
                    return {
                        selects: document.querySelectorAll('select').length,
                        inputs: document.querySelectorAll('input').length,
                        buttons: document.querySelectorAll('button').length,
                        url: window.location.href,
                        title: document.title,
                    };
                }""")

                # Текст body (первые 500 символов)
                body_text = await page.inner_text("body")
                body_preview = body_text[:500].replace("\n", " | ")

                # Структура формы
                form_html = await page.evaluate("""() => {
                    const inputs = [...document.querySelectorAll('input, select, button, textarea')];
                    return inputs.slice(0, 20).map(el => {
                        return el.outerHTML.substring(0, 200);
                    }).join('\\n---\\n');
                }""")

                report = (
                    f"📊 <b>Диагностика страницы:</b>\n\n"
                    f"URL: <code>{stats['url']}</code>\n"
                    f"Заголовок: {stats['title']}\n"
                    f"Selects: {stats['selects']}\n"
                    f"Inputs: {stats['inputs']}\n"
                    f"Buttons: {stats['buttons']}\n\n"
                    f"<b>Превью текста:</b>\n<i>{body_preview[:400]}</i>"
                )
                await ctx.bot.send_message(update.effective_user.id, report, parse_mode="HTML")

                # Шлём HTML формы кусками
                if form_html:
                    chunks = [form_html[i:i+3500] for i in range(0, min(len(form_html), 7000), 3500)]
                    for i, chunk in enumerate(chunks):
                        await ctx.bot.send_message(
                            update.effective_user.id,
                            f"<b>HTML формы (часть {i+1}):</b>\n<pre>{chunk}</pre>",
                            parse_mode="HTML"
                        )

            finally:
                await context.close()

        except Exception as e:
            logger.exception("Diag error")
            await update.message.reply_text(f"❌ Ошибка: {type(e).__name__}: {str(e)[:200]}")

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
            f"⏰ <b>Время уведомлений</b>\n\n"
            f"Текущее: <b>{user['hour']:02d}:{user['minute']:02d}</b> (Киев)\n\n"
            f"Выберите готовое или введите своё:",
            parse_mode="HTML", reply_markup=time_kb()
        )

    elif data == "menu:check":
        if not user["combos"]:
            await q.edit_message_text(
                "📋 Список пуст. Сначала добавьте комбинацию.",
                reply_markup=back_kb()
            )
            return

        await q.edit_message_text(
            "🔍 Открываю сайт и проверяю... Это может занять до 1 минуты."
        )

        found_any = False
        errors = 0
        region = user["region"]
        for combo in user["combos"]:
            results, status = await check_combination(region, combo)
            if status != "ok":
                errors += 1
                await ctx.bot.send_message(user_id, f"⚠️ Ошибка для <code>{combo}</code>: {status}", parse_mode="HTML")
                continue
            if results:
                found_any = True
                plates_str = "\n".join(f"  • {p}" for p in results[:30])
                more = f"\n<i>...и ещё {len(results)-30}</i>" if len(results) > 30 else ""
                await ctx.bot.send_message(
                    user_id,
                    f"🎉 <b>Найдены номера с <code>{combo}</code>!</b>\n"
                    f"📍 {REGIONS.get(region, '?')}\n\n{plates_str}{more}",
                    parse_mode="HTML"
                )

        if not found_any and errors == 0:
            await ctx.bot.send_message(
                user_id,
                f"😕 Ничего не найдено в <b>{REGIONS.get(region, '?')}</b>.\n"
                f"Проверено комбинаций: {len(user['combos'])}",
                parse_mode="HTML"
            )

        await ctx.bot.send_message(user_id, home_text(user), parse_mode="HTML", reply_markup=main_menu_kb())

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
            f"✅ Время: <b>{hour:02d}:{minute:02d}</b> (Киев)\n\n" + home_text(user),
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

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CommandHandler("menu",  cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("diag", cmd_diag))

    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    async def on_startup(app):
        scheduler.start()
        schedule_all_users(app)
        # Прогреваем браузер
        try:
            await get_browser()
        except Exception as e:
            logger.error(f"Не удалось запустить браузер: {e}")
        logger.info(f"Бот запущен. Задач: {len(scheduler.get_jobs())}")

    app.post_init = on_startup
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
