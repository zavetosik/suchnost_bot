from datetime import datetime

from aiogram.types import CallbackQuery, Message

import database as db
from config import BASE_DIR


async def is_admin(message: Message | CallbackQuery):
    result = await db.fetchone(
        "SELECT 1 FROM admins WHERE telegram_id = %s",
        (message.from_user.id,),
    )
    return result is not None


async def is_owner(message: Message | CallbackQuery):
    result = await db.fetchone(
        "SELECT 1 FROM owners WHERE telegram_id = %s",
        (message.from_user.id,),
    )
    return result is not None


async def is_user(message: Message):
    result = await db.fetchone(
        "SELECT 1 FROM users WHERE telegram_id = %s",
        (message.from_user.id,),
    )
    return result is not None


def write_logs(admin_id, username, text):
    with open(BASE_DIR / "logs.csv", "a", encoding="utf-8") as file:
        file.write(f"{datetime.now()};{admin_id};{username};{text}\n")


def write_admin_logs(action, admin_id, target_id):
    with open(BASE_DIR / "admin_logs.csv", "a", encoding="utf-8") as file:
        file.write(f"{datetime.now()};{action};{admin_id};{target_id}\n")
