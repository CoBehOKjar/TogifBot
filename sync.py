"""
Одноразовая работа со слэш-командами на конкретном сервере.

Синхронизация (мгновенно, в отличие от глобальной - до часа на распространение):
    python sync_guild.py <guild_id> sync

Очистка дублей: если команды одновременно синхронизированы и глобально (bot.py),
и на сервере (этим скриптом) - Discord покажет их ДВАЖДЫ. Чтобы убрать
гильдийные копии и оставить только глобальные:
    python sync_guild.py <guild_id> clear

Guild id также можно задать через .env (GUILD_ID) и не передавать аргументом.

Как узнать ID сервера: в Discord включи Режим разработчика
(Настройки пользователя -> Расширенные -> Режим разработчика),
затем ПКМ по серверу -> Copy Server ID.

Скрипт подключается к Discord, выполняет операцию и сразу отключается -
постоянно держать его запущенным не нужно.
"""
from __future__ import annotations

import asyncio
import os
import sys

import discord

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# Импортируем bot.py как модуль - это выполнит регистрацию всех команд
# (в т.ч. /togif и Convert to GIF) на его bot.tree, но НЕ запустит самого бота
# (bot.run вызывается только внутри if __name__ == "__main__" в bot.py).
import main as bot_module

TOKEN = os.environ.get("DISCORD_TOKEN")
args = [a for a in sys.argv[1:] if not a.isspace()]
ACTION = "sync"
GUILD_ID = os.environ.get("GUILD_ID")

for a in args:
    if a in ("sync", "clear"):
        ACTION = a
    else:
        GUILD_ID = a

if not TOKEN:
    raise SystemExit("Не задан DISCORD_TOKEN (в .env или переменных окружения).")
if not GUILD_ID:
    raise SystemExit(
        "Не указан ID сервера.\n"
        "Использование: python sync_guild.py <guild_id> [sync|clear]\n"
        "Либо задай GUILD_ID в .env"
    )

bot = bot_module.bot


@bot.event
async def on_ready():
    guild = discord.Object(id=int(GUILD_ID))

    if ACTION == "clear":
        bot.tree.clear_commands(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        print(f"Гильдийные команды очищены на сервере {GUILD_ID}. Осталось: {len(synced)}")
    else:
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        names = ", ".join(cmd.name for cmd in synced)
        print(f"Синхронизировано {len(synced)} команд на сервере {GUILD_ID}: {names}")

    await bot.close()


if __name__ == "__main__":
    asyncio.run(bot.start(TOKEN))