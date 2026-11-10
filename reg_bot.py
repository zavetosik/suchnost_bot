import asyncio
import logging
from typing import Literal

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message
from aiogram.utils.formatting import BlockQuote, Bold, Italic, Text, TextLink

import database as db
from config import TEST_TOKEN, CHANNEL_ID
from utils import is_admin, is_owner, write_logs, write_admin_logs

bot = Bot(token=TEST_TOKEN)
dp = Dispatcher()
logger = logging.getLogger(__name__)


class ModeratePost(CallbackData, prefix="moderate"):
    action: Literal["accept", "reject"]
    post_id: int


@dp.message.outer_middleware()
@dp.callback_query.outer_middleware()
async def register_user(handler, event, data):
    user = event.from_user
    if user is None or user.is_bot:
        return
    await db.execute(
        """
        INSERT INTO users (telegram_id, username, name)
        VALUES (%s, %s, %s)
        ON CONFLICT (telegram_id) DO UPDATE
        SET username = EXCLUDED.username, name = EXCLUDED.name
        """,
        (user.id, user.username, user.first_name),
    )
    return await handler(event, data)


def moderation_keyboard(post_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Принять",
            callback_data=ModeratePost(action="accept", post_id=post_id).pack(),
        ),
        InlineKeyboardButton(
            text="❌ Отклонить",
            callback_data=ModeratePost(action="reject", post_id=post_id).pack(),
        ),
    ]])


@dp.message(CommandStart())
async def do_start(message: Message):
    greeting = Text(
        Bold("✦ ПРЕДЛОЖКА"), "\n\n",
        f"Привет, {message.from_user.first_name}! 👋\n",
        "Здесь твои мысли могут стать постом в канале.\n\n",
        BlockQuote("Есть история, вопрос или просто мысль?\nПоделись — это может откликнуться другим."),
        "\n\n", Bold("От идеи до публикации"), "\n",
        "✍️ Отправь текст или фото с подписью.\n",
        "💬 Администраторы посмотрят твой пост.\n",
        "🔔 Бот сообщит, когда будет принято решение.\n\n",
        Italic("Начни с пары слов. Мы уже слушаем."),
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✍️ Предложить пост", callback_data="compose_post"),
    ]])
    await message.answer(**greeting.as_kwargs(), reply_markup=keyboard)


@dp.callback_query(F.data == "compose_post")
async def prompt_post(callback: CallbackQuery):
    await callback.answer()
    await bot.send_message(
        callback.from_user.id,
        "✍️ Отправь сюда текст или фото с подписью.\n\n"
        "Сообщение сразу поступит на модерацию. Когда появится решение, я напишу тебе.",
    )


@dp.message(Command("profile"))
async def show_profile(message: Message):
    user = message.from_user
    await message.answer(f"id: {user.id}\nusername: @{user.username or '-'}\nname: {user.first_name}")


@dp.message(Command("users"))
async def get_users(message: Message):
    if not await is_admin(message):
        await message.answer("У тебя нету прав!")
        return
    count = (await db.fetchone("SELECT COUNT(*) FROM users"))[0]
    await message.answer(f"В боте сейчас {count} зарегистрированных пользователей")


@dp.message(Command("all_users"))
async def get_all_users(message: Message):
    if not await is_admin(message):
        await message.answer("У тебя нету прав!")
        return
    users = await db.fetchall("SELECT id, telegram_id, username, name FROM users ORDER BY id")
    text = "\n\n".join(
        f"ID DB: {user[0]}\nID: {user[1]}\nUsername: @{user[2] or '-'}\nName: {user[3]}"
        for user in users
    )
    for start in range(0, len(text), 4096):
        await message.answer(text[start:start + 4096])
    if not text:
        await message.answer("Пользователей пока нет")


def command_id(text):
    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        return None
    try:
        value = int(parts[1])
    except ValueError:
        return None
    return value if 0 < value <= 9_223_372_036_854_775_807 else None


@dp.message(Command("add_admin"))
async def add_admin(message: Message):
    if not await is_owner(message):
        await message.answer("У тебя нету прав!")
        return
    admin_id = command_id(message.text)
    if admin_id is None:
        await message.answer("Укажи числовой ID пользователя: /add_admin ID")
        return
    added = await db.fetchone(
        "INSERT INTO admins (telegram_id) VALUES (%s) ON CONFLICT (telegram_id) DO NOTHING RETURNING id",
        (admin_id,),
    )
    if added is None:
        await message.answer("Этот пользователь уже админ")
        return
    write_admin_logs("add_admin", message.from_user.id, admin_id)
    await message.answer("Админ успешно добавлен")


@dp.message(Command("remove_admin"))
async def remove_admin(message: Message):
    if not await is_owner(message):
        await message.answer("У тебя нету прав!")
        return
    admin_id = command_id(message.text)
    if admin_id is None:
        await message.answer("Укажи числовой ID админа: /remove_admin ID")
        return
    removed = await db.execute("DELETE FROM admins WHERE telegram_id = %s", (admin_id,))
    if not removed:
        await message.answer("Такой админ не найден")
        return
    write_admin_logs("remove_admin", message.from_user.id, admin_id)
    await message.answer("Админ успешно удален")


@dp.message(Command("broadcast"))
async def make_broadcast(message: Message):
    if not await is_admin(message):
        await message.answer("У тебя нету прав!")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Напиши текст для рассылки")
        return
    text = parts[1].strip()
    users = await db.fetchall("SELECT telegram_id FROM users")
    for user in users:
        try:
            await bot.send_message(user[0], text)
        except TelegramAPIError:
            logger.exception("Не удалось отправить рассылку пользователю %s", user[0])
    write_logs(message.from_user.id, message.from_user.username, text)


@dp.message(Command("admin"))
async def get_admin_info(message: Message):
    if not await is_admin(message):
        await message.answer("У тебя нету прав!")
        return
    await message.answer(
        "/users — число пользователей\n"
        "/all_users — список пользователей\n"
        "/all_admins — список администраторов\n"
        "/posts количество — последние посты (любое положительное число)\n"
        "/all_posts — все посты\n"
        "/broadcast текст — рассылка\n\n"
        "Новые посты приходят с кнопками «Принять» и «Отклонить»."
    )


@dp.message(Command("all_admins"))
async def get_all_admins(message: Message):
    if not await is_admin(message):
        await message.answer("У тебя нету прав!")
        return
    admins = await db.fetchall("SELECT id, telegram_id FROM admins ORDER BY id")
    text = "\n\n".join(f"ID DB: {admin[0]}\nID: {admin[1]}" for admin in admins)
    for start in range(0, len(text), 4096):
        await message.answer(text[start:start + 4096])
    if not text:
        await message.answer("Администраторов пока нет")


async def send_post_record(message: Message, post):
    post_id, username, account_id, name, text, photo = post
    username = f"@{username}" if username else "-"
    details = (
        f"id: {post_id}\n"
        f"username: {username}\n"
        f"id_account: {account_id}\n"
        f"name: {name or '-'}\n"
        f"text: {text or '-'}\n"
        f"photo: {photo or '-'}"
    )
    limit = 1024 if photo else 4096
    if len(Text(details)) > limit:
        # Keep each post in a single message without cutting off its full record.
        method = message.answer_document(
            BufferedInputFile(details.encode("utf-8"), filename=f"post_{post_id}.txt"),
            caption=f"id: {post_id}\nПолная запись поста — в файле.",
            parse_mode=None,
        )
    elif photo:
        method = message.answer_photo(photo, caption=details, parse_mode=None)
    else:
        method = message.answer(
            details, parse_mode=None, link_preview_options=LinkPreviewOptions(is_disabled=True)
        )
    while True:
        try:
            await method
            return
        except TelegramRetryAfter as error:
            await asyncio.sleep(error.retry_after)


async def send_post_history(message: Message, count=None):
    last_id = None
    sent = 0
    while count is None or sent < count:
        page_size = 100 if count is None else min(100, count - sent)
        where = "WHERE p.id < %s" if last_id is not None else ""
        params = (last_id, page_size) if last_id is not None else (page_size,)
        posts = await db.fetchall(
            f"""
            SELECT p.id, p.author_username, p.user_id, p.author_name, p.text, p.photo
            FROM posts AS p
            {where}
            ORDER BY p.id DESC
            LIMIT %s
            """,
            params,
        )
        if not posts:
            break
        for post in posts:
            await send_post_record(message, post)
            sent += 1
        last_id = posts[-1][0]
        if len(posts) < page_size:
            break
    if not sent:
        await message.answer("Постов пока нет")


@dp.message(Command("posts"))
async def get_last_posts(message: Message):
    if not await is_admin(message):
        await message.answer("У тебя нету прав!")
        return
    try:
        count = int(message.text.split(maxsplit=1)[1])
    except (IndexError, ValueError):
        count = 0
    if count <= 0:
        await message.answer("Укажи положительное целое количество: /posts 10")
        return
    await send_post_history(message, count)


@dp.message(Command("all_posts"))
async def get_all_posts(message: Message):
    if not await is_admin(message):
        await message.answer("У тебя нету прав!")
        return
    if len(message.text.split()) != 1:
        await message.answer("Используй /all_posts без аргументов или /posts 10 для последних постов")
        return
    await send_post_history(message)


@dp.message(F.text | F.photo)
async def send_confession(message: Message):
    text = message.text if message.text is not None else message.caption
    if text and text.lstrip().startswith("/"):
        await message.answer("Неизвестная команда. Чтобы предложить пост, просто отправь текст или фото без команды.")
        return
    photo = message.photo[-1].file_id if message.photo else None
    if not photo and (not text or not text.strip()):
        await message.answer("Отправь текст или фото для поста")
        return
    user = message.from_user
    post_id = (await db.fetchone(
        """
        INSERT INTO posts (user_id, text, status, photo, author_username, author_name)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (user.id, text, "pending", photo, user.username, user.full_name),
    ))[0]
    admins = await db.fetchall("SELECT telegram_id FROM admins")
    header = f"📩 Новый пост\nPOST ID: {post_id}\nUsername: @{user.username or '-'}\nID: {user.id}\nName: {user.full_name}"
    content = f"{header}\n\n{text}" if text else header
    keyboard = moderation_keyboard(post_id)
    delivered = 0
    for admin in admins:
        try:
            limit = 1024 if photo else 4096
            if len(content.encode("utf-16-le")) // 2 > limit:
                await bot.send_message(admin[0], header)
                content_for_admin = text
            else:
                content_for_admin = content
            if photo:
                await bot.send_photo(admin[0], photo=photo, caption=content_for_admin, reply_markup=keyboard)
            else:
                await bot.send_message(admin[0], content_for_admin, reply_markup=keyboard)
            delivered += 1
        except TelegramAPIError:
            logger.exception("Не удалось доставить пост %s администратору %s", post_id, admin[0])
    if delivered:
        await message.answer("Пост отправлен на модерацию")
    else:
        await message.answer("Пост сохранён, но пока нет доступных администраторов для модерации.")


@dp.message()
async def unsupported_message(message: Message):
    await message.answer("Для поста отправь текст или фото с подписью.")


async def publish_post(text, photo):
    me = await bot.me()
    footer = TextLink("Предложка", url=f"https://t.me/{me.username}")
    body = (text or "").rstrip()
    content = Text(body, "\n\n", footer) if body else Text(footer)
    if photo:
        if len(content) <= 1024:
            await bot.send_photo(CHANNEL_ID, photo=photo, **content.as_caption_kwargs())
            return
        # A full caption may have no room for the footer: keep all text below the photo.
        await bot.send_photo(CHANNEL_ID, photo=photo)

    max_body = 4096 - len(Text("\n\n", footer))
    while len(Text(body)) > max_body:
        chunk = body.encode("utf-16-le")[:max_body * 2].decode("utf-16-le", errors="ignore")
        await bot.send_message(
            CHANNEL_ID, chunk, parse_mode=None,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        body = body[len(chunk):]
    content = Text(body, "\n\n", footer) if body else Text(footer)
    await bot.send_message(
        CHANNEL_ID, **content.as_kwargs(),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


@dp.callback_query(ModeratePost.filter())
async def moderate_post(callback: CallbackQuery, callback_data: ModeratePost):
    if not await is_admin(callback):
        await callback.answer("У тебя нету прав!", show_alert=True)
        return

    post_id = callback_data.post_id
    status = "accepted" if callback_data.action == "accept" else "rejected"
    try:
        async with db.pool.connection() as conn:
            async with conn.transaction():
                cursor = await conn.execute(
                    "SELECT id, user_id, text, status, photo FROM posts WHERE id = %s FOR UPDATE",
                    (post_id,),
                )
                post = await cursor.fetchone()
                if post is None:
                    await callback.answer("Пост не найден", show_alert=True)
                    return
                if post[3] != "pending":
                    result = "принят" if post[3] == "accepted" else "отклонён"
                    await callback.answer(f"Пост уже {result}", show_alert=True)
                    await remove_moderation_buttons(callback)
                    return
                if status == "accepted":
                    await publish_post(post[2], post[4])
                await conn.execute("UPDATE posts SET status = %s WHERE id = %s", (status, post_id))
    except TelegramAPIError:
        logger.exception("Не удалось опубликовать пост %s", post_id)
        await callback.answer("Не удалось опубликовать пост. Попробуй ещё раз.", show_alert=True)
        return

    result = "Пост опубликован" if status == "accepted" else "Пост отклонён"
    await callback.answer(result)
    await remove_moderation_buttons(callback)
    notice = "✅ Ваш пост был одобрен" if status == "accepted" else "❌ Ваш пост был отклонен"
    try:
        await bot.send_message(post[1], notice)
    except TelegramAPIError:
        logger.exception("Не удалось уведомить автора поста %s", post_id)


async def remove_moderation_buttons(callback: CallbackQuery):
    if callback.message is not None:
        try:
            await bot.edit_message_reply_markup(
                chat_id=callback.message.chat.id,
                message_id=callback.message.message_id,
                reply_markup=None,
            )
        except TelegramAPIError:
            logger.exception("Не удалось убрать кнопки у поста")


async def main():
    try:
        await db.init_db()
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot)
    finally:
        await db.close_db()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
