import os
import base64
import asyncio
import uuid

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from openai import OpenAI

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton
)

from database import get_connection, get_company_info

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FOLDER_ID = os.getenv("YANDEX_CLOUD_FOLDER_ID")
API_KEY = os.getenv("YANDEX_CLOUD_API_KEY")
MODEL = os.getenv("YANDEX_CLOUD_MODEL")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGE_DIR = os.path.join(BASE_DIR, "generated")
os.makedirs(IMAGE_DIR, exist_ok=True)

client = OpenAI(
    api_key=API_KEY,
    base_url="https://ai.api.cloud.yandex.net/v1",
    project=FOLDER_ID
)

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Временные состояния диалогов пользователей
user_states = {}

INSTRUCTIONS = """
Ты — профессиональный AI-маркетолог компании.

Помогай разрабатывать маркетинговые стратегии,
рекламные кампании, посты, контент-планы и идеи продвижения.

Правила:
1. Учитывай информацию о компании.
2. Не отклоняйся от задачи пользователя.
3. Сохраняй контекст предыдущего диалога.
4. Не придумывай неподтверждённые факты.
5. Разделяй подтверждённые факты и предложения.
6. Предлагай практические решения.
7. Если информации недостаточно, уточняй её.
8. Отвечай на русском языке.

СТИЛЬ:
- Используй строгий, современный и профессиональный язык.
- Пиши понятными предложениями и короткими абзацами.
- Используй жирное выделение только для важных заголовков.
- Курсив применяй для пояснений, когда он действительно полезен.
- Не добавляй случайные кавычки, звёздочки, Markdown-разметку
  и декоративные символы.
- Не перегружай ответы эмодзи.
- Не используй оформление ради оформления.
- Подбирай тон под задачу: деловой, рекламный,
  официальный или живой для социальных сетей.
- По умолчанию отвечай достаточно кратко и содержательно.
"""

MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="✍️ Создать публикацию"),
            KeyboardButton(text="📅 Контент-план")
        ],
        [
            KeyboardButton(text="🗓 Расписание"),
            KeyboardButton(text="📁 Мои материалы")
        ],
        [
            KeyboardButton(text="📊 Аналитика"),
            KeyboardButton(text="🏢 Моя компания")
        ]
    ],
    resize_keyboard=True
)

# -------------------------
# Работа с пользователями
# -------------------------

def get_user_context(telegram_id, username):
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, company_id FROM users WHERE telegram_id = %s",
                (telegram_id,)
            )
            user = cur.fetchone()

            if not user:
                cur.execute(
                    "INSERT INTO companies (name) VALUES (%s) RETURNING id",
                    (f"Компания {telegram_id}",)
                )
                company_id = cur.fetchone()[0]

                cur.execute(
                    """INSERT INTO users (telegram_id, username, company_id)
                       VALUES (%s, %s, %s) RETURNING id""",
                    (telegram_id, username, company_id)
                )
                user_id = cur.fetchone()[0]

            else:
                user_id, company_id = user

                cur.execute(
                    "UPDATE users SET username = %s WHERE id = %s",
                    (username, user_id)
                )

                if company_id is None:
                    cur.execute(
                        "INSERT INTO companies (name) VALUES (%s) RETURNING id",
                        (f"Компания {telegram_id}",)
                    )
                    company_id = cur.fetchone()[0]

                    cur.execute(
                        "UPDATE users SET company_id = %s WHERE id = %s",
                        (company_id, user_id)
                    )

            cur.execute(
                """SELECT id FROM conversations
                   WHERE user_id = %s
                   ORDER BY created_at DESC LIMIT 1""",
                (user_id,)
            )
            conversation = cur.fetchone()

            if conversation:
                conversation_id = conversation[0]
            else:
                cur.execute(
                    """INSERT INTO conversations (user_id, title)
                       VALUES (%s, %s) RETURNING id""",
                    (user_id, "Основной диалог")
                )
                conversation_id = cur.fetchone()[0]

            cur.execute(
                """SELECT role, content FROM messages
                   WHERE conversation_id = %s
                   ORDER BY created_at DESC LIMIT 10""",
                (conversation_id,)
            )
            history = list(reversed(cur.fetchall()))

        conn.commit()
        return user_id, company_id, conversation_id, history

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

def save_message(conversation_id, role, content):
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO messages (conversation_id, role, content)
                   VALUES (%s, %s, %s)""",
                (conversation_id, role, content)
            )
        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

# -------------------------
# Работа с материалами
# -------------------------

def save_content(company_id, user_id, content_text, image_path=None):
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO content
                   (company_id, user_id, text, image_path, status)
                   VALUES (%s, %s, %s, %s, 'pending')
                   RETURNING id""",
                (company_id, user_id, content_text, image_path)
            )

            content_id = cur.fetchone()[0]

            cur.execute(
                """INSERT INTO approvals (content_id, status)
                   VALUES (%s, 'pending')
                   RETURNING id""",
                (content_id,)
            )

            approval_id = cur.fetchone()[0]

        conn.commit()
        return content_id, approval_id

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

def get_content_text(content_id):
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT text FROM content WHERE id = %s",
                (content_id,)
            )
            result = cur.fetchone()
            return result[0] if result else None

    finally:
        conn.close()

def get_content_info(content_id, company_id):
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, text, image_path, status
                   FROM content
                   WHERE id = %s AND company_id = %s""",
                (content_id, company_id)
            )
            return cur.fetchone()

    finally:
        conn.close()

def get_approval_info(approval_id, company_id):
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT a.id, a.content_id
                   FROM approvals a
                   JOIN content c ON c.id = a.content_id
                   WHERE a.id = %s
                   AND c.company_id = %s
                   AND a.status = 'pending'""",
                (approval_id, company_id)
            )
            return cur.fetchone()

    finally:
        conn.close()

def update_approval(approval_id, new_status, company_id):
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT a.content_id
                   FROM approvals a
                   JOIN content c ON c.id = a.content_id
                   WHERE a.id = %s
                   AND c.company_id = %s
                   AND a.status = 'pending'
                   FOR UPDATE OF a""",
                (approval_id, company_id)
            )

            result = cur.fetchone()

            if not result:
                return None

            content_id = result[0]

            cur.execute(
                """UPDATE approvals
                   SET status = %s,
                       approved_at = CASE
                           WHEN %s = 'approved'
                           THEN CURRENT_TIMESTAMP
                           ELSE NULL
                       END
                   WHERE id = %s""",
                (new_status, new_status, approval_id)
            )

            cur.execute(
                "UPDATE content SET status = %s WHERE id = %s",
                (new_status, content_id)
            )

        conn.commit()
        return content_id

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

def get_scheduled_time(content_id):
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT MIN(publish_at)
                   FROM scheduled_posts
                   WHERE content_id = %s AND published = FALSE""",
                (content_id,)
            )
            result = cur.fetchone()
            return result[0] if result else None

    finally:
        conn.close()

# -------------------------
# Расписание
# -------------------------

def create_schedule(content_id, publish_at):
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO scheduled_posts
                   (content_id, publish_at, published)
                   VALUES (%s, %s, FALSE)
                   RETURNING id""",
                (content_id, publish_at)
            )
            schedule_id = cur.fetchone()[0]

        conn.commit()
        return schedule_id

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

def get_company_timezone(company_id):
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT timezone FROM companies WHERE id = %s",
                (company_id,)
            )
            result = cur.fetchone()
            return result[0] if result else "Asia/Yekaterinburg"

    finally:
        conn.close()

def set_company_timezone(company_id, timezone_name):
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE companies SET timezone = %s WHERE id = %s",
                (timezone_name, company_id)
            )
        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

def get_company_schedule(company_id):
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT s.id, s.publish_at, c.id, c.status
                   FROM scheduled_posts s
                   JOIN content c ON c.id = s.content_id
                   WHERE c.company_id = %s
                   AND s.published = FALSE
                   ORDER BY s.publish_at
                   LIMIT 30""",
                (company_id,)
            )
            return cur.fetchall()

    finally:
        conn.close()

def get_company_materials(company_id):
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, status, created_at
                   FROM content
                   WHERE company_id = %s
                   ORDER BY created_at DESC
                   LIMIT 15""",
                (company_id,)
            )
            return cur.fetchall()

    finally:
        conn.close()

def get_company_analytics(company_id):
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT
                    COUNT(*),
                    COUNT(*) FILTER (WHERE status = 'approved'),
                    COUNT(*) FILTER (WHERE status = 'pending'),
                    COUNT(*) FILTER (WHERE status = 'rejected'),
                    COUNT(*) FILTER (WHERE status = 'revision_requested')
                   FROM content
                   WHERE company_id = %s""",
                (company_id,)
            )
            return cur.fetchone()

    finally:
        conn.close()

# -------------------------
# AI
# -------------------------

def generate_response(prompt, generate_image=False):
    tools = []

    if generate_image:
        tools = [{
            "type": "image_generation",
            "size": "1024x1024"
        }]

    response = client.responses.create(
        model=f"gpt://{FOLDER_ID}/{MODEL}",
        instructions=INSTRUCTIONS,
        input=prompt,
        tools=tools,
        temperature=0.3,
        max_output_tokens=8000
    )

    image_data = None

    for item in response.output:
        if item.type == "image_generation_call":
            if item.status == "completed" and item.result:
                image_data = base64.b64decode(item.result)

    return response.output_text, image_data

async def send_long_message(message, text):
    for start in range(0, len(text), 4000):
        await message.answer(text[start:start + 4000])

async def publish_content(content_id, company_id):
    info = await asyncio.to_thread(
        get_content_info, content_id, company_id
    )

    if not info:
        raise ValueError("Материал не найден.")

    _, text, image_path, content_status = info

    if content_status != "approved":
        raise ValueError("Материал ещё не одобрен.")

    if not CHANNEL_ID:
        raise ValueError("Не настроен Telegram-канал.")

    if image_path and os.path.isfile(image_path):
        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=FSInputFile(image_path),
            caption=text[:1000]
        )

        if len(text) > 1000:
            for start in range(1000, len(text), 4000):
                await bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=text[start:start + 4000]
                )
    else:
        for start in range(0, len(text), 4000):
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=text[start:start + 4000]
            )

# -------------------------
# Планировщик публикаций
# -------------------------

def get_due_posts():
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT s.id, s.content_id, c.company_id
                   FROM scheduled_posts s
                   JOIN content c ON c.id = s.content_id
                   WHERE s.published = FALSE
                   AND s.publish_at <= CURRENT_TIMESTAMP
                   AND c.status = 'approved'
                   ORDER BY s.publish_at
                   LIMIT 10"""
            )
            return cur.fetchall()

    finally:
        conn.close()

def mark_schedule_published(schedule_id):
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE scheduled_posts
                   SET published = TRUE, last_error = NULL
                   WHERE id = %s""",
                (schedule_id,)
            )
        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

def mark_schedule_error(schedule_id, error):
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE scheduled_posts
                   SET attempts = attempts + 1,
                       last_error = %s
                   WHERE id = %s""",
                (error[:1000], schedule_id)
            )
        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

async def scheduler():
    while True:
        try:
            due_posts = await asyncio.to_thread(get_due_posts)

            for schedule_id, content_id, company_id in due_posts:
                try:
                    await publish_content(content_id, company_id)

                    await asyncio.to_thread(
                        mark_schedule_published, schedule_id
                    )

                except Exception as e:
                    await asyncio.to_thread(
                        mark_schedule_error, schedule_id, str(e)
                    )

                    print(
                        f"Ошибка расписания {schedule_id}: {e}"
                    )

        except Exception as e:
            print(f"Ошибка планировщика: {e}")

        await asyncio.sleep(15)

# -------------------------
# Клавиатуры согласования
# -------------------------

def approval_keyboard(approval_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Одобрить",
                    callback_data=f"approve:{approval_id}"
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"reject:{approval_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Доработать",
                    callback_data=f"revise:{approval_id}"
                )
            ]
        ]
    )

# -------------------------
# Главное меню
# -------------------------

@dp.message(CommandStart())
async def start(message: Message):
    try:
        await asyncio.to_thread(
            get_user_context,
            message.from_user.id,
            message.from_user.username
        )

        await message.answer(
            "AI-маркетолог готов к работе.\n\n"
            "Создавай рекламные материалы, разрабатывай "
            "контент-планы и управляй публикациями.\n\n"
            "Выбери нужный раздел в меню.",
            reply_markup=MAIN_MENU
        )

    except Exception as e:
        await message.answer(f"Ошибка: {str(e)[:500]}")

@dp.message(F.text == "✍️ Создать публикацию")
async def menu_create(message: Message):
    user_states[message.from_user.id] = {"mode": "post"}
    await message.answer(
        "Опиши, какую публикацию необходимо создать."
    )

@dp.message(F.text == "📅 Контент-план")
async def menu_plan(message: Message):
    user_states[message.from_user.id] = {"mode": "plan"}
    await message.answer(
        "Укажи период и пожелания к контент-плану. "
        "Например: план на ближайшие две недели для B2B-аудитории."
    )

@dp.message(F.text == "🗓 Расписание")
async def menu_schedule(message: Message):
    user_states[message.from_user.id] = {
        "mode": "schedule_content"
    }
    await message.answer(
        "Введи номер материала, который нужно запланировать.\n\n"
        "Материал может ожидать согласования, но до одобрения "
        "он не будет опубликован."
    )

@dp.message(F.text == "📁 Мои материалы")
async def menu_materials(message: Message):
    try:
        _, company_id, _, _ = await asyncio.to_thread(
            get_user_context,
            message.from_user.id,
            message.from_user.username
        )

        materials = await asyncio.to_thread(
            get_company_materials, company_id
        )

        if not materials:
            await message.answer("Материалов пока нет.")
            return

        labels = {
            "pending": "Ожидает согласования",
            "approved": "Одобрен",
            "rejected": "Отклонён",
            "revision_requested": "На доработке",
            "draft": "Черновик"
        }

        lines = ["Последние материалы компании:\n"]

        for item_id, status, created_at in materials:
            lines.append(
                f"№{item_id} — {labels.get(status, status)}\n"
                f"{created_at.strftime('%d.%m.%Y %H:%M')}"
            )

        await message.answer("\n\n".join(lines))

    except Exception as e:
        await message.answer(f"Ошибка: {str(e)[:500]}")

@dp.message(F.text == "📊 Аналитика")
async def menu_analytics(message: Message):
    try:
        _, company_id, _, _ = await asyncio.to_thread(
            get_user_context,
            message.from_user.id,
            message.from_user.username
        )

        stats = await asyncio.to_thread(
            get_company_analytics, company_id
        )

        total, approved, pending, rejected, revision = stats

        await message.answer(
            "Аналитика материалов\n\n"
            f"Всего создано: {total}\n"
            f"Одобрено: {approved}\n"
            f"Ожидает согласования: {pending}\n"
            f"Отклонено: {rejected}\n"
            f"На доработке: {revision}"
        )

    except Exception as e:
        await message.answer(f"Ошибка: {str(e)[:500]}")

@dp.message(F.text == "🏢 Моя компания")
async def menu_company(message: Message):
    try:
        _, company_id, _, _ = await asyncio.to_thread(
            get_user_context,
            message.from_user.id,
            message.from_user.username
        )

        company = await asyncio.to_thread(
            get_company_info, company_id
        )

        timezone_name = await asyncio.to_thread(
            get_company_timezone, company_id
        )

        await message.answer(
            f"Компания: {company['name'] if company else 'Не указана'}\n\n"
            f"Описание:\n"
            f"{company['description'] if company else 'Не заполнено'}\n\n"
            f"Часовой пояс: {timezone_name}\n\n"
            "Чтобы изменить часовой пояс, отправь команду:\n"
            "/timezone Europe/Moscow\n\n"
            "Используй название часового пояса из базы IANA."
        )

    except Exception as e:
        await message.answer(f"Ошибка: {str(e)[:500]}")

# -------------------------
# Команды
# -------------------------

@dp.message(Command("timezone"))
async def timezone_command(message: Message):
    parts = message.text.split(maxsplit=1)

    if len(parts) < 2:
        await message.answer(
            "Укажи часовой пояс.\n"
            "Например: /timezone Asia/Yekaterinburg"
        )
        return

    timezone_name = parts[1].strip()

    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        await message.answer(
            "Не удалось распознать часовой пояс. "
            "Проверь название."
        )
        return

    try:
        _, company_id, _, _ = await asyncio.to_thread(
            get_user_context,
            message.from_user.id,
            message.from_user.username
        )

        await asyncio.to_thread(
            set_company_timezone, company_id, timezone_name
        )

        await message.answer(
            f"Часовой пояс компании изменён: {timezone_name}"
        )

    except Exception as e:
        await message.answer(f"Ошибка: {str(e)[:500]}")

@dp.message(Command("post"))
async def post_command(message: Message):
    prompt = message.text.replace("/post", "", 1).strip()

    if not prompt:
        user_states[message.from_user.id] = {"mode": "post"}
        await message.answer("Опиши, какую публикацию создать.")
        return

    await process_ai_request(message, prompt, is_post=True)

@dp.message(Command("image"))
async def image_command(message: Message):
    prompt = message.text.replace("/image", "", 1).strip()

    if not prompt:
        await message.answer("Опиши изображение, которое нужно создать.")
        return

    await process_ai_request(
        message, prompt, generate_image=True
    )

# -------------------------
# Согласование
# -------------------------

@dp.callback_query(F.data.startswith("approve:"))
async def approve_content(callback: CallbackQuery):
    approval_id = int(callback.data.split(":")[1])

    try:
        _, company_id, _, _ = await asyncio.to_thread(
            get_user_context,
            callback.from_user.id,
            callback.from_user.username
        )

        info = await asyncio.to_thread(
            get_approval_info, approval_id, company_id
        )

        if not info:
            await callback.answer(
                "Решение уже обработано или материал недоступен.",
                show_alert=True
            )
            return

        content_id = info[1]

        result = await asyncio.to_thread(
            update_approval, approval_id, "approved", company_id
        )

        if result is None:
            await callback.answer("Материал уже обработан.", show_alert=True)
            return

        scheduled_at = await asyncio.to_thread(
            get_scheduled_time, content_id
        )

        if scheduled_at and scheduled_at > datetime.now(timezone.utc):
            await callback.message.edit_reply_markup(
                reply_markup=None
            )
            await callback.message.answer(
                "Материал одобрен. Он будет опубликован "
                "в назначенное время."
            )

        else:
            await publish_content(content_id, company_id)

            await callback.message.edit_reply_markup(
                reply_markup=None
            )
            await callback.message.answer(
                "Материал одобрен и опубликован в канале."
            )

        await callback.answer()

    except Exception as e:
        await callback.answer(
            f"Ошибка: {str(e)[:150]}",
            show_alert=True
        )

@dp.callback_query(F.data.startswith("reject:"))
async def reject_content(callback: CallbackQuery):
    approval_id = int(callback.data.split(":")[1])

    try:
        _, company_id, _, _ = await asyncio.to_thread(
            get_user_context,
            callback.from_user.id,
            callback.from_user.username
        )

        result = await asyncio.to_thread(
            update_approval, approval_id, "rejected", company_id
        )

        if result is None:
            await callback.answer(
                "Материал уже обработан.",
                show_alert=True
            )
            return

        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer("Материал отклонён.")
        await callback.answer()

    except Exception as e:
        await callback.answer(
            f"Ошибка: {str(e)[:150]}",
            show_alert=True
        )

@dp.callback_query(F.data.startswith("revise:"))
async def revise_content(callback: CallbackQuery):
    approval_id = int(callback.data.split(":")[1])

    try:
        _, company_id, _, _ = await asyncio.to_thread(
            get_user_context,
            callback.from_user.id,
            callback.from_user.username
        )

        info = await asyncio.to_thread(
            get_approval_info, approval_id, company_id
        )

        if not info:
            await callback.answer(
                "Материал уже обработан.",
                show_alert=True
            )
            return

        content_id = info[1]

        original = await asyncio.to_thread(
            get_content_info, content_id, company_id
        )

        await asyncio.to_thread(
            update_approval, approval_id,
            "revision_requested", company_id
        )

        user_states[callback.from_user.id] = {
            "mode": "revise",
            "original_text": original[1]
        }

        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            "Напиши, что именно необходимо изменить. "
            "Я подготовлю новую версию на согласование."
        )
        await callback.answer()

    except Exception as e:
        await callback.answer(
            f"Ошибка: {str(e)[:150]}",
            show_alert=True
        )

# -------------------------
# Генерация и сохранение
# -------------------------

async def process_ai_request(
    message,
    prompt,
    is_post=False,
    generate_image=False,
    extra_context=""
):
    status_message = await message.answer("Готовлю ответ...")

    try:
        user_id, company_id, conversation_id, history = (
            await asyncio.to_thread(
                get_user_context,
                message.from_user.id,
                message.from_user.username
            )
        )

        company = await asyncio.to_thread(
            get_company_info, company_id
        )

        company_name = company["name"] if company else "Неизвестная компания"
        company_description = company["description"] if company else ""

        await asyncio.to_thread(
            save_message, conversation_id, "user", prompt
        )

        history_text = "\n".join(
            f"{role}: {content}" for role, content in history
        )

        full_prompt = f"""
ИНФОРМАЦИЯ О КОМПАНИИ:
Название: {company_name}
Описание: {company_description}

ИСТОРИЯ ДИАЛОГА:
{history_text}

ДОПОЛНИТЕЛЬНЫЙ КОНТЕКСТ:
{extra_context}

НОВОЕ ЗАДАНИЕ:
{prompt}

Создавай конкретный, полезный результат.
Не выдумывай сведения о компании.
"""

        text, image = await asyncio.to_thread(
            generate_response, full_prompt, generate_image
        )

        if not text and not image:
            await message.answer("Модель не вернула результат.")
            return

        if text:
            await asyncio.to_thread(
                save_message, conversation_id, "assistant", text
            )

        image_path = None

        if image:
            image_path = os.path.join(
                IMAGE_DIR, f"ad_{uuid.uuid4().hex}.png"
            )

            with open(image_path, "wb") as file:
                file.write(image)

        if image_path:
            await message.answer_photo(
                FSInputFile(image_path),
                caption=text[:1000] if text else "Изображение"
            )

            if text and len(text) > 1000:
                await send_long_message(message, text[1000:])

        elif text:
            await send_long_message(message, text)

        if is_post and text:
            content_id, approval_id = await asyncio.to_thread(
                save_content,
                company_id,
                user_id,
                text,
                image_path
            )

            await message.answer(
                f"Материал №{content_id} создан.\n"
                "Он ожидает твоего решения.",
                reply_markup=approval_keyboard(approval_id)
            )

    except Exception as e:
        await message.answer(f"Ошибка: {str(e)[:1000]}")

    finally:
        await status_message.delete()

# -------------------------
# Обработка расписания
# -------------------------

async def process_schedule_input(message, state, text):
    mode = state["mode"]

    if mode == "schedule_content":
        try:
            content_id = int(text)
        except ValueError:
            await message.answer("Введи номер материала цифрами.")
            return

        _, company_id, _, _ = await asyncio.to_thread(
            get_user_context,
            message.from_user.id,
            message.from_user.username
        )

        info = await asyncio.to_thread(
            get_content_info, content_id, company_id
        )

        if not info:
            await message.answer(
                "Материал не найден в твоей компании."
            )
            return

        user_states[message.from_user.id] = {
            "mode": "schedule_datetime",
            "content_id": content_id
        }

        await message.answer(
            "Теперь введи дату и время публикации.\n\n"
            "Формат: ДД.ММ.ГГГГ ЧЧ:ММ\n"
            "Например: 20.11.2026 14:35"
        )
        return

    if mode == "schedule_datetime":
        content_id = state["content_id"]

        try:
            local_datetime = datetime.strptime(
                text, "%d.%m.%Y %H:%M"
            )
        except ValueError:
            await message.answer(
                "Неверный формат.\n"
                "Пример: 20.11.2026 14:35"
            )
            return

        _, company_id, _, _ = await asyncio.to_thread(
            get_user_context,
            message.from_user.id,
            message.from_user.username
        )

        timezone_name = await asyncio.to_thread(
            get_company_timezone, company_id
        )

        try:
            local_zone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            await message.answer(
                "Ошибка часового пояса компании."
            )
            return

        publish_at = local_datetime.replace(
            tzinfo=local_zone
        ).astimezone(timezone.utc)

        if publish_at <= datetime.now(timezone.utc):
            await message.answer(
                "Дата и время должны быть в будущем."
            )
            return

        schedule_id = await asyncio.to_thread(
            create_schedule, content_id, publish_at
        )

        user_states.pop(message.from_user.id, None)

        await message.answer(
            f"Публикация №{content_id} добавлена в расписание.\n"
            f"Задача №{schedule_id}\n"
            f"Дата: {text}\n"
            f"Часовой пояс: {timezone_name}\n\n"
            "Публикация выйдет только после одобрения."
        )

# -------------------------
# Обработка обычных сообщений
# -------------------------

@dp.message()
async def handle_message(message: Message):
    if not message.text:
        return

    text = message.text.strip()
    telegram_id = message.from_user.id

    state = user_states.get(telegram_id)

    if state:
        mode = state["mode"]

        if mode in ("schedule_content", "schedule_datetime"):
            await process_schedule_input(message, state, text)
            return

        if mode == "timezone":
            try:
                ZoneInfo(text)
            except (ZoneInfoNotFoundError, ValueError):
                await message.answer(
                    "Неизвестный часовой пояс. Попробуй ещё раз."
                )
                return

            _, company_id, _, _ = await asyncio.to_thread(
                get_user_context,
                telegram_id,
                message.from_user.username
            )

            await asyncio.to_thread(
                set_company_timezone, company_id, text
            )

            user_states.pop(telegram_id, None)
            await message.answer(
                f"Часовой пояс изменён: {text}"
            )
            return

        if mode == "post":
            user_states.pop(telegram_id, None)
            await process_ai_request(message, text, is_post=True)
            return

        if mode == "plan":
            user_states.pop(telegram_id, None)
            await process_ai_request(
                message,
                "Составь подробный контент-план. " + text
            )
            return

        if mode == "revise":
            original_text = state["original_text"]
            user_states.pop(telegram_id, None)

            await process_ai_request(
                message,
                text,
                is_post=True,
                extra_context=(
                    "Это новая версия ранее созданного материала. "
                    "Исходный текст:\n" + original_text
                )
            )
            return

    await process_ai_request(message, text)

# -------------------------
# Запуск
# -------------------------

async def main():
    scheduler_task = asyncio.create_task(scheduler())

    try:
        await dp.start_polling(bot)
    finally:
        scheduler_task.cancel()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
