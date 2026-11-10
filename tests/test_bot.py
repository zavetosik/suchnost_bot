import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiogram.methods import AnswerCallbackQuery, SendMessage, SendPhoto

import database as db
import reg_bot
import utils
from helpers import TelegramSession, dispatch_callback, dispatch_message, make_bot


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = TelegramSession()
        self.bot = make_bot(self.session)
        self.bot_patch = patch.object(reg_bot, "bot", self.bot)
        self.bot_patch.start()

    async def asyncTearDown(self):
        self.bot_patch.stop()
        await self.bot.session.close()

    async def test_missing_database_url_fails_before_connecting(self):
        with patch.object(db, "DATABASE_URL", ""), patch.object(db.pool, "open", new_callable=AsyncMock) as open_pool:
            with self.assertRaisesRegex(RuntimeError, "DATABASE_URL"):
                await db.init_db()
            open_pool.assert_not_awaited()

    async def test_permissions_are_checked_in_postgres(self):
        message = SimpleNamespace(from_user=SimpleNamespace(id=8_000_000_000))
        for check in (utils.is_user, utils.is_admin, utils.is_owner):
            with self.subTest(check=check.__name__):
                with patch.object(db, "fetchone", new_callable=AsyncMock) as query:
                    query.return_value = None
                    self.assertFalse(await check(message))
                    query.return_value = (1,)
                    self.assertTrue(await check(message))
                    self.assertEqual(query.await_args.args[1], (message.from_user.id,))

    async def test_first_profile_registers_without_start(self):
        with patch.object(db, "execute", new_callable=AsyncMock) as register:
            await dispatch_message(self.bot, 8000000000, "/profile")
            self.assertEqual(register.await_args.args[1], (8000000000, "test_user", "Тест"))
            self.assertIn("id: 8000000000", self.session.requests[-1].text)
            self.assertNotIn("/start", self.session.requests[-1].text)

    async def test_first_admin_command_registers_without_granting_permissions(self):
        with patch.object(db, "execute", new_callable=AsyncMock) as register, patch.object(
            db, "fetchone", new_callable=AsyncMock, return_value=None
        ):
            await dispatch_message(self.bot, 8000000000, "/users")
            register.assert_awaited_once()
            self.assertEqual(self.session.requests[-1].text, "У тебя нету прав!")

    async def test_plain_text_submits_with_two_moderation_buttons(self):
        with patch.object(db, "execute", new_callable=AsyncMock), patch.object(
            db, "fetchone", new_callable=AsyncMock, return_value=(42,)
        ) as insert, patch.object(db, "fetchall", new_callable=AsyncMock, return_value=[(9000000000,)]):
            await dispatch_message(self.bot, 8000000000, "Текст без команды")
            self.assertEqual(insert.await_args.args[1], (8000000000, "Текст без команды", "pending", None, "test_user", "Тест"))
            delivery = self.session.requests[0]
            self.assertIsInstance(delivery, SendMessage)
            buttons = delivery.reply_markup.inline_keyboard[0]
            self.assertEqual([button.text for button in buttons], ["✅ Принять", "❌ Отклонить"])
            self.assertEqual([reg_bot.ModeratePost.unpack(button.callback_data).post_id for button in buttons], [42, 42])
            self.assertEqual(self.session.requests[-1].text, "Пост отправлен на модерацию")

    async def test_photo_submits_caption_without_send(self):
        with patch.object(db, "execute", new_callable=AsyncMock), patch.object(
            db, "fetchone", new_callable=AsyncMock, return_value=(43,)
        ) as insert, patch.object(db, "fetchall", new_callable=AsyncMock, return_value=[(9000000000,)]):
            await dispatch_message(self.bot, 8000000000, photo=True, caption="Фото и подпись")
            self.assertEqual(insert.await_args.args[1], (8000000000, "Фото и подпись", "pending", "test-photo", "test_user", "Тест"))
            delivery = self.session.requests[0]
            self.assertIsInstance(delivery, SendPhoto)
            self.assertEqual(len(delivery.reply_markup.inline_keyboard[0]), 2)

    async def test_post_saves_full_sender_name_without_username(self):
        with patch.object(db, "execute", new_callable=AsyncMock), patch.object(
            db, "fetchone", new_callable=AsyncMock, return_value=(45,)
        ) as insert, patch.object(db, "fetchall", new_callable=AsyncMock, return_value=[]):
            await dispatch_message(
                self.bot, 8000000000, "Новый пост", username=None, name="Ім'я", last_name="Прізвище"
            )
            self.assertEqual(insert.await_args.args[1], (8000000000, "Новый пост", "pending", None, None, "Ім'я Прізвище"))

    async def test_long_caption_preserves_full_content(self):
        with patch.object(db, "execute", new_callable=AsyncMock), patch.object(
            db, "fetchone", new_callable=AsyncMock, return_value=(44,)
        ), patch.object(db, "fetchall", new_callable=AsyncMock, return_value=[(9000000000,)]):
            caption = "я" * 1024
            await dispatch_message(self.bot, 8000000000, photo=True, caption=caption)
            self.assertIsInstance(self.session.requests[0], SendMessage)
            self.assertIsInstance(self.session.requests[1], SendPhoto)
            self.assertEqual(self.session.requests[1].caption, caption)
            self.assertIsNotNone(self.session.requests[1].reply_markup)

    async def test_unknown_command_is_not_a_post(self):
        with patch.object(db, "execute", new_callable=AsyncMock) as register, patch.object(
            db, "fetchone", new_callable=AsyncMock
        ) as insert:
            await dispatch_message(self.bot, 8000000000, "/unknown")
            register.assert_awaited_once()
            insert.assert_not_awaited()
            self.assertIn("Неизвестная команда", self.session.requests[-1].text)

    async def test_callback_requires_current_admin_permissions(self):
        with patch.object(db, "execute", new_callable=AsyncMock), patch.object(
            db, "fetchone", new_callable=AsyncMock, return_value=None
        ), patch.object(db, "pool", SimpleNamespace(connection=Mock())) as pool:
            await dispatch_callback(self.bot, 8000000000, 42, "accept")
            pool.connection.assert_not_called()
            self.assertIsInstance(self.session.requests[-1], AnswerCallbackQuery)
            self.assertTrue(self.session.requests[-1].show_alert)
            self.assertEqual(self.session.requests[-1].text, "У тебя нету прав!")

    async def test_shutdown_closes_pool_when_startup_fails(self):
        bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
        with patch.object(reg_bot, "bot", bot), patch.object(
            db, "init_db", new_callable=AsyncMock
        ) as init, patch.object(db, "close_db", new_callable=AsyncMock) as close:
            init.side_effect = RuntimeError("No database")
            with self.assertRaisesRegex(RuntimeError, "No database"):
                await reg_bot.main()
            close.assert_awaited_once()
            bot.session.close.assert_awaited_once()

    async def test_shutdown_closes_pool_when_polling_fails(self):
        bot = SimpleNamespace(delete_webhook=AsyncMock(), session=SimpleNamespace(close=AsyncMock()))
        dispatcher = SimpleNamespace(start_polling=AsyncMock(side_effect=RuntimeError("Stopped")))
        with patch.object(reg_bot, "bot", bot), patch.object(
            reg_bot, "dp", dispatcher
        ), patch.object(db, "init_db", new_callable=AsyncMock), patch.object(
            db, "close_db", new_callable=AsyncMock
        ) as close:
            with self.assertRaisesRegex(RuntimeError, "Stopped"):
                await reg_bot.main()
            bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
            close.assert_awaited_once()
            bot.session.close.assert_awaited_once()

    async def test_failed_webhook_removal_prevents_polling(self):
        bot = SimpleNamespace(
            delete_webhook=AsyncMock(side_effect=RuntimeError("Webhook unavailable")),
            session=SimpleNamespace(close=AsyncMock()),
        )
        with patch.object(reg_bot, "bot", bot), patch.object(
            reg_bot.dp, "start_polling", new_callable=AsyncMock
        ) as poll, patch.object(db, "init_db", new_callable=AsyncMock), patch.object(
            db, "close_db", new_callable=AsyncMock
        ) as close:
            with self.assertRaisesRegex(RuntimeError, "Webhook unavailable"):
                await reg_bot.main()
            poll.assert_not_awaited()
            close.assert_awaited_once()
            bot.session.close.assert_awaited_once()
