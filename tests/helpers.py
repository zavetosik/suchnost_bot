import itertools

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetMe, SendDocument, SendMessage, SendPhoto
from aiogram.types import Message, Update, User

from config import CHANNEL_ID
import reg_bot

_ids = itertools.count(1)


class TelegramSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests = []
        self.fail_publication = False

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, GetMe):
            return User(id=bot.id, is_bot=True, first_name="Test bot", username="test_proposal_bot")
        if self.fail_publication and isinstance(method, (SendMessage, SendPhoto)) and str(method.chat_id) == str(CHANNEL_ID):
            raise TelegramBadRequest(method=method, message="Test publication failure")
        if isinstance(method, (SendDocument, SendMessage, SendPhoto)):
            return Message.model_validate({
                "message_id": next(_ids), "date": 1700000000,
                "chat": {"id": method.chat_id if isinstance(method.chat_id, int) else -100123, "type": "private"},
                "text": getattr(method, "text", None),
                "caption": getattr(method, "caption", None),
            }, context={"bot": bot})
        return True

    async def stream_content(self, url, **kwargs):
        if False:
            yield b""


def make_bot(session):
    return Bot(token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi", session=session)


def user(user_id):
    return {"id": user_id, "is_bot": False, "first_name": "Тест", "username": "test_user"}


async def dispatch_message(bot, user_id, text=None, *, photo=False, caption=None, username="test_user", name="Тест", last_name=None):
    sender = user(user_id)
    sender.update(username=username, first_name=name)
    if last_name is not None:
        sender["last_name"] = last_name
    message = {
        "message_id": next(_ids), "date": 1700000000,
        "chat": {"id": user_id, "type": "private"}, "from": sender,
    }
    if text is not None:
        message["text"] = text
        if text.startswith("/"):
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    if photo:
        message["photo"] = [{"file_id": "test-photo", "file_unique_id": "test-unique", "width": 100, "height": 100}]
        if caption is not None:
            message["caption"] = caption
    update = Update.model_validate({"update_id": next(_ids), "message": message})
    await reg_bot.dp.feed_update(bot, update)


async def dispatch_callback(bot, user_id, post_id, action):
    update = Update.model_validate({
        "update_id": next(_ids),
        "callback_query": {
            "id": str(next(_ids)), "from": user(user_id), "chat_instance": "test-chat",
            "data": reg_bot.ModeratePost(action=action, post_id=post_id).pack(),
            "message": {
                "message_id": next(_ids), "date": 1700000000,
                "chat": {"id": user_id, "type": "private"}, "text": "Новый пост",
            },
        },
    })
    await reg_bot.dp.feed_update(bot, update)
