import unittest
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendDocument, SendMessage, SendPhoto

import database as db
import reg_bot
from helpers import TelegramSession, dispatch_message, make_bot


def post(post_id, *, text=None, photo=None):
    return (post_id, "writer", 8000000001, "Автор", text or f"Текст {post_id}", photo)


class PostHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = TelegramSession()
        self.bot = make_bot(self.session)
        self.patches = [
            patch.object(reg_bot, "bot", self.bot),
            patch.object(db, "execute", new_callable=AsyncMock),
            patch.object(db, "fetchone", new_callable=AsyncMock, return_value=(1,)),
            patch.object(db, "fetchall", new_callable=AsyncMock, return_value=[]),
        ]
        self.mocks = [item.start() for item in self.patches]
        self.permission, self.query = self.mocks[2:]

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        await self.bot.session.close()

    async def test_last_posts_are_separate_messages_in_requested_format(self):
        self.query.return_value = [post(31), post(30, photo="test-photo")]
        await dispatch_message(self.bot, 9000000000, "/posts 2")
        self.assertEqual(len(self.session.requests), 2)
        self.assertEqual(self.query.await_args.args[1], (2,))
        text, photo = self.session.requests
        self.assertIsInstance(text, SendMessage)
        self.assertEqual(text.text, "id: 31\nusername: @writer\nid_account: 8000000001\nname: Автор\ntext: Текст 31\nphoto: -")
        self.assertIsInstance(photo, SendPhoto)
        self.assertEqual(photo.photo, "test-photo")
        self.assertEqual(photo.caption, "id: 30\nusername: @writer\nid_account: 8000000001\nname: Автор\ntext: Текст 30\nphoto: test-photo")

    async def test_all_posts_are_loaded_in_pages_without_duplicates(self):
        self.query.side_effect = [[post(i) for i in range(101, 1, -1)], [post(1)]]
        await dispatch_message(self.bot, 9000000000, "/all_posts")
        shown = [int(request.text.splitlines()[0].split(": ")[1]) for request in self.session.requests]
        self.assertEqual(shown, list(range(101, 0, -1)))
        self.assertEqual(self.query.await_count, 2)
        self.assertEqual(self.query.await_args_list[0].args[1], (100,))
        self.assertEqual(self.query.await_args_list[1].args[1], (2, 100))

    async def test_limited_history_stops_at_exact_count_across_pages(self):
        self.query.side_effect = [[post(i) for i in range(105, 5, -1)], [post(5)]]
        await dispatch_message(self.bot, 9000000000, "/posts 101")
        self.assertEqual(len(self.session.requests), 101)
        self.assertEqual(self.query.await_args_list[-1].args[1], (6, 1))

    async def test_count_accepts_any_positive_number(self):
        self.query.return_value = [post(9)]
        for count in (1, 3, 25, 1000, 10 ** 30):
            with self.subTest(count=count):
                self.session.requests.clear()
                self.query.reset_mock()
                await dispatch_message(self.bot, 9000000000, f"/posts {count}")
                self.assertEqual(len(self.session.requests), 1)
                self.assertEqual(self.query.await_args.args[1], (min(100, count),))

    async def test_history_is_admin_only(self):
        self.permission.return_value = None
        for command in ("/posts 10", "/all_posts"):
            with self.subTest(command=command):
                await dispatch_message(self.bot, 9000000000, command)
                self.assertEqual(self.session.requests[-1].text, "У тебя нету прав!")
        self.query.assert_not_awaited()

    async def test_invalid_count_does_not_query_posts(self):
        for command in ("/posts", "/posts 0", "/posts -1", "/posts abc", "/posts 1.5", "/posts 1 2"):
            with self.subTest(command=command):
                await dispatch_message(self.bot, 9000000000, command)
                self.assertIn("/posts 10", self.session.requests[-1].text)
        self.query.assert_not_awaited()

    async def test_empty_history_returns_clear_message(self):
        await dispatch_message(self.bot, 9000000000, "/all_posts")
        self.assertEqual(len(self.session.requests), 1)
        self.assertEqual(self.session.requests[0].text, "Постов пока нет")

    async def test_missing_author_and_empty_text_have_placeholders(self):
        self.query.return_value = [(9, None, 8000000001, None, None, "test-photo")]
        await dispatch_message(self.bot, 9000000000, "/posts 1")
        self.assertEqual(self.session.requests[0].caption, "id: 9\nusername: -\nid_account: 8000000001\nname: -\ntext: -\nphoto: test-photo")

    async def test_long_record_is_one_file_with_full_text(self):
        for text, photo in (("я" * 4096, None), ("я" * 1024, "test-photo")):
            with self.subTest(photo=photo):
                self.session.requests.clear()
                self.query.return_value = [post(9, text=text, photo=photo)]
                await dispatch_message(self.bot, 9000000000, "/posts 1")
                self.assertEqual(len(self.session.requests), 1)
                result = self.session.requests[0]
                self.assertIsInstance(result, SendDocument)
                self.assertEqual(result.document.filename, "post_9.txt")
                self.assertIn("text: " + text + "\nphoto: ", result.document.data.decode())

    async def test_rate_limit_waits_then_continues_without_skipping_post(self):
        self.query.return_value = [post(9)]
        original_request = self.session.make_request
        first_request = True

        async def request(bot, method, timeout=None):
            nonlocal first_request
            if first_request:
                first_request = False
                raise TelegramRetryAfter(method=method, message="Rate limit", retry_after=1)
            return await original_request(bot, method, timeout)

        with patch.object(self.session, "make_request", side_effect=request), patch.object(
            reg_bot.asyncio, "sleep", new_callable=AsyncMock
        ) as wait:
            await dispatch_message(self.bot, 9000000000, "/posts 1")
            wait.assert_awaited_once_with(1)
        self.assertEqual(len(self.session.requests), 1)
        self.assertTrue(self.session.requests[0].text.startswith("id: 9\n"))

    async def test_admin_help_lists_both_history_commands(self):
        await dispatch_message(self.bot, 9000000000, "/admin")
        self.assertIn("/posts количество", self.session.requests[0].text)
        self.assertIn("/all_posts", self.session.requests[0].text)
