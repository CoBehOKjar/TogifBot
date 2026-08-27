"""
Раз в 24 часа берёт случайное слово из файла (RUS_WORDS) и переименовывает
канал ADM_CHAT_ID на сервере ADM_GUILD_ID в ADM_CHAT_PREFIX + слово, склеенные
БЕЗ пробелов/тире/прочих разделителей.

Пример: ADM_CHAT_PREFIX="👑-термо" + слово "абажур" -> "👑-термоабажур".

Настройка через .env:
    RUS_WORDS=russian.txt      - путь к файлу со словами (по одному на строку)
    ADM_GUILD_ID=<id сервера>
    ADM_CHAT_ID=<id канала>
    ADM_CHAT_PREFIX="👑-термо" - к этому префиксу приклеивается случайное слово

Если что-то из настроек не задано, некорректно, или файл со словами не найден -
модуль просто не запускает задачу и пишет об этом в лог, не ломая остального бота.
"""
from __future__ import annotations

import logging
import os
import random

import discord
from discord.ext import commands, tasks

log = logging.getLogger(__name__)

WORDS_FILE = os.environ.get("RUS_WORDS", "").strip()
_guild_id_raw = os.environ.get("ADM_GUILD_ID", "").strip()
_channel_id_raw = os.environ.get("ADM_CHAT_ID", "").strip()
GUILD_ID = int(_guild_id_raw) if _guild_id_raw.isdigit() else None
CHANNEL_ID = int(_channel_id_raw) if _channel_id_raw.isdigit() else None
CHANNEL_PREFIX = os.environ.get("ADM_CHAT_PREFIX", "").strip()

MAX_CHANNEL_NAME_LENGTH = 100  # ограничение Discord на длину имени канала


def _config_is_valid() -> bool:
    if not WORDS_FILE:
        log.warning("channel_renamer: RUS_WORDS не задан в .env - модуль отключён")
        return False
    if GUILD_ID is None or CHANNEL_ID is None:
        log.warning("channel_renamer: ADM_GUILD_ID/ADM_CHAT_ID не заданы (или некорректны) - модуль отключён")
        return False
    if not CHANNEL_PREFIX:
        log.warning("channel_renamer: ADM_CHAT_PREFIX не задан - модуль отключён")
        return False
    if not os.path.isfile(WORDS_FILE):
        log.warning(f"channel_renamer: файл со словами не найден: {WORDS_FILE} - модуль отключён")
        return False
    return True


def pick_random_word(path: str) -> str | None:
    """Читает файл целиком и берёт случайную непустую строку (по одному слову на строку)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            words = [line.strip() for line in f if line.strip()]
    except OSError as e:
        log.error(f"channel_renamer: не удалось прочитать файл {path}: {e}")
        return None

    if not words:
        log.warning(f"channel_renamer: файл {path} пуст")
        return None

    return random.choice(words)


def build_channel_name(prefix: str, word: str) -> str:
    """Склеивает префикс и слово БЕЗ разделителей, обрезает до лимита Discord (100 символов)."""
    name = f"{prefix}{word}"
    if len(name) > MAX_CHANNEL_NAME_LENGTH:
        name = name[:MAX_CHANNEL_NAME_LENGTH]
    return name


@tasks.loop(hours=24)
async def _rename_channel_loop(bot: commands.Bot) -> None:
    word = pick_random_word(WORDS_FILE)
    if word is None:
        log.warning("channel_renamer: не удалось выбрать слово, пропускаю переименование в этот раз")
        return

    new_name = build_channel_name(CHANNEL_PREFIX, word)

    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        log.error(f"channel_renamer: сервер {GUILD_ID} не найден (бот не состоит в нём?)")
        return

    channel = guild.get_channel(CHANNEL_ID)
    if channel is None:
        log.error(f"channel_renamer: канал {CHANNEL_ID} не найден на сервере {GUILD_ID}")
        return

    try:
        await channel.edit(name=new_name, reason="Плановое переименование (случайное слово из RUS_WORDS)")
    except discord.Forbidden:
        log.error(
            "channel_renamer: не хватает прав для переименования канала. "
            "Проверь, что у бота есть право Manage Channels на этом канале/сервере."
        )
        return
    except discord.HTTPException:
        log.exception("channel_renamer: ошибка Discord API при переименовании канала")
        return

    log.info(f"channel_renamer: канал {CHANNEL_ID} переименован в {new_name!r}")


def start(bot: commands.Bot) -> None:
    """
    Запускает фоновую задачу переименования. Вызывать один раз из on_ready в main.py.
    Безопасно вызывать повторно (например, при переподключении) - задача не
    запустится дважды благодаря проверке is_running().
    """
    if not _config_is_valid():
        return
    if _rename_channel_loop.is_running():
        return
    log.info("channel_renamer: запускаю задачу переименования канала (раз в 24 часа)")
    _rename_channel_loop.start(bot)