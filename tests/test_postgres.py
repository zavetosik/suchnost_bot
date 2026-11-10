import os
import secrets
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

from aiogram.methods import EditMessageReplyMarkup, SendDocument, SendMessage, SendPhoto
from psycopg import Rollback

import database as db
import reg_bot
from config import BASE_DIR, CHANNEL_ID
from helpers import TelegramSession, dispatch_callback, dispatch_message, make_bot


@unittest.skipUnless(os.getenv("RUN_POSTGRES_TESTS") == "1", "PostgreSQL test is opt-in")
class PostgresTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_workflow_with_rollback(self):
        # Real PostgreSQL, real dispatcher/middleware, fake Telegram transport.
        # Every query is inside one transaction which is rolled back at the end.
        await db.init_db()
        real_pool = db.pool
        owner_id = 8_000_000_000_000 + secrets.randbelow(1_000_000_000)
        user_id, admin_id = owner_id + 1, owner_id + 2
        session = TelegramSession()
        bot = make_bot(session)

        def publications():
            return [request for request in session.requests if isinstance(request, (SendMessage, SendPhoto)) and str(request.chat_id) == str(CHANNEL_ID)]

        async def latest_post():
            return await db.fetchone(
                "SELECT id, text, status, photo FROM posts WHERE user_id = %s ORDER BY id DESC LIMIT 1",
                (user_id,),
            )

        async def status(post_id):
            return (await db.fetchone("SELECT status FROM posts WHERE id = %s", (post_id,)))[0]

        try:
            await db.init_db()
            async with real_pool.connection() as conn:
                async with conn.transaction():
                    @asynccontextmanager
                    async def connection():
                        yield conn

                    with patch.object(db, "pool", SimpleNamespace(connection=connection)), patch.object(
                        reg_bot, "bot", bot
                    ), patch.object(reg_bot, "write_logs") as logs, patch.object(
                        reg_bot, "write_admin_logs"
                    ) as admin_logs:
                        await db.execute("INSERT INTO owners (telegram_id) VALUES (%s)", (owner_id,))
                        await dispatch_message(bot, owner_id, f"/add_admin {admin_id}")

                        # First contact is plain text: auto-register and immediately submit.
                        await dispatch_message(bot, user_id, "Текст із 'лапками' та %s", username="quote'_;--")
                        saved = await db.fetchone("SELECT username FROM users WHERE telegram_id = %s", (user_id,))
                        self.assertEqual(saved, ("quote'_;--",))
                        text_post = await latest_post()
                        self.assertEqual(text_post[1:], ("Текст із 'лапками' та %s", "pending", None))
                        moderation = [request for request in session.requests if isinstance(request, SendMessage) and request.chat_id == admin_id][-1]
                        self.assertEqual(len(moderation.reply_markup.inline_keyboard[0]), 2)

                        await dispatch_message(bot, user_id, "/profile", name="Оновлене ім'я")
                        saved = await db.fetchone("SELECT name FROM users WHERE telegram_id = %s", (user_id,))
                        self.assertEqual(saved, ("Оновлене ім'я",))
                        snapshot = await db.fetchone(
                            "SELECT user_id, author_username, author_name FROM posts WHERE id = %s", (text_post[0],)
                        )
                        self.assertEqual(snapshot, (user_id, "quote'_;--", "Тест"))
                        for command in ("/admin", "/users", "/all_users", "/all_admins"):
                            await dispatch_message(bot, admin_id, command)

                        await dispatch_callback(bot, admin_id, text_post[0], "accept")
                        self.assertEqual(await status(text_post[0]), "accepted")
                        published = publications()[-1]
                        self.assertEqual(published.text, text_post[1] + "\n\nПредложка")
                        self.assertEqual(published.entities[-1].type, "text_link")
                        self.assertEqual(published.entities[-1].url, "https://t.me/podzemkasaltbot")
                        self.assertIsInstance(session.requests[-2], EditMessageReplyMarkup)
                        sent = len(publications())
                        # A stale reject button must not overwrite an accepted post.
                        await dispatch_callback(bot, admin_id, text_post[0], "reject")
                        self.assertEqual(await status(text_post[0]), "accepted")
                        self.assertEqual(len(publications()), sent)

                        await dispatch_message(bot, user_id, photo=True, caption="Фото без /send")
                        photo_post = await latest_post()
                        self.assertEqual(photo_post[1:], ("Фото без /send", "pending", "test-photo"))
                        await dispatch_callback(bot, admin_id, photo_post[0], "reject")
                        self.assertEqual(await status(photo_post[0]), "rejected")
                        await dispatch_callback(bot, admin_id, photo_post[0], "accept")
                        self.assertEqual(await status(photo_post[0]), "rejected")
                        self.assertEqual(len(publications()), sent)

                        await dispatch_message(bot, user_id, photo=True)
                        photo_post = await latest_post()
                        await dispatch_callback(bot, admin_id, photo_post[0], "accept")
                        self.assertEqual(await status(photo_post[0]), "accepted")
                        self.assertIsInstance(publications()[-1], SendPhoto)
                        self.assertEqual(publications()[-1].photo, "test-photo")
                        self.assertEqual(publications()[-1].caption, "Предложка")
                        self.assertEqual(publications()[-1].caption_entities[-1].url, "https://t.me/podzemkasaltbot")

                        # Failed publication leaves the post pending and the button usable.
                        await dispatch_message(bot, user_id, "Повтор після помилки")
                        retry_post = await latest_post()
                        session.fail_publication = True
                        with self.assertLogs(reg_bot.logger, level="ERROR"):
                            await dispatch_callback(bot, admin_id, retry_post[0], "accept")
                        self.assertEqual(await status(retry_post[0]), "pending")
                        session.fail_publication = False
                        await dispatch_callback(bot, admin_id, retry_post[0], "accept")
                        self.assertEqual(await status(retry_post[0]), "accepted")

                        start = len(session.requests)
                        await dispatch_message(bot, admin_id, "/posts 2")
                        records = [request for request in session.requests[start:] if isinstance(request, (SendDocument, SendMessage, SendPhoto))]
                        self.assertEqual(len(records), 2)
                        ids = [int((getattr(request, "text", None) or request.caption).splitlines()[0].split(": ")[1]) for request in records]
                        self.assertGreater(ids[0], ids[1])
                        for request in records:
                            record = getattr(request, "text", None) or request.caption
                            for field in ("id:", "username:", "id_account:", "name:", "text:", "photo:"):
                                self.assertIn(field, record)

                        start = len(session.requests)
                        await dispatch_message(bot, admin_id, "/all_posts")
                        records = [request for request in session.requests[start:] if isinstance(request, (SendDocument, SendMessage, SendPhoto))]
                        ids = [int((getattr(request, "text", None) or request.caption).splitlines()[0].split(": ")[1]) for request in records]
                        self.assertEqual(ids, sorted(set(ids), reverse=True))
                        self.assertTrue({text_post[0], photo_post[0], retry_post[0]}.issubset(ids))
                        original = next(request for request in records if (getattr(request, "text", None) or request.caption).startswith(f"id: {text_post[0]}\n"))
                        original_text = getattr(original, "text", None) or original.caption
                        self.assertIn("username: @quote'_;--\n", original_text)
                        self.assertIn("name: Тест\n", original_text)

                        await dispatch_message(bot, user_id, "Пост без username", username=None, name="Ім'я", last_name="Прізвище")
                        anonymous_username_post = await latest_post()
                        await dispatch_message(bot, user_id, "/profile", username="changed_username", name="Нове ім'я")
                        # Re-running startup migrations must not overwrite the saved profile.
                        await conn.execute((BASE_DIR / "schema.sql").read_text(encoding="utf-8"))
                        snapshot = await db.fetchone(
                            "SELECT author_username, author_name FROM posts WHERE id = %s", (anonymous_username_post[0],)
                        )
                        self.assertEqual(snapshot, (None, "Ім'я Прізвище"))

                        before = (await db.fetchone("SELECT COUNT(*) FROM posts WHERE user_id = %s", (user_id,)))[0]
                        await dispatch_message(bot, user_id, "/unknown")
                        after = (await db.fetchone("SELECT COUNT(*) FROM posts WHERE user_id = %s", (user_id,)))[0]
                        self.assertEqual(before, after)
                        await dispatch_message(bot, admin_id, "/broadcast Перевірка")
                        logs.assert_called_once()
                        await dispatch_message(bot, owner_id, f"/remove_admin {admin_id}")
                        self.assertEqual(admin_logs.call_count, 2)
                        await dispatch_message(bot, admin_id, "/all_posts")
                        self.assertEqual(session.requests[-1].text, "У тебя нету прав!")
                        await dispatch_message(bot, user_id, "Пост після видалення адміна")
                        pending = await latest_post()
                        await dispatch_callback(bot, admin_id, pending[0], "accept")
                        self.assertEqual(await status(pending[0]), "pending")
                        self.assertEqual(session.requests[-1].text, "У тебя нету прав!")
                        raise Rollback()

            for table, column in (("users", "telegram_id"), ("admins", "telegram_id"), ("owners", "telegram_id"), ("posts", "user_id")):
                result = await db.fetchone(
                    f"SELECT COUNT(*) FROM {table} WHERE {column} IN (%s, %s, %s)",
                    (owner_id, user_id, admin_id),
                )
                self.assertEqual(result, (0,))
        finally:
            await bot.session.close()
            await real_pool.close()
