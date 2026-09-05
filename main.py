from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler

# load_dotenv() ОБЯЗАТЕЛЬНО до импорта togif/role_transfer - они читают os.environ
# на уровне модуля (при импорте), а не при вызове функций. Если .env загрузить
# позже, эти модули просто не увидят переменные и решат, что они не заданы.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import togif
import soyball
import renamer

TOKEN = os.environ.get("DISCORD_TOKEN")
COMMAND_PREFIX = "!"
LOG_DIR = os.environ.get("LOG_DIR", "logs")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# ---------------------------------------------------------------------------
# Настройка логирования: консоль + файл с ротацией (5 файлов по 5 МБ).
# Настраивается на root logger - модуль togif.py (и любые другие) автоматически
# наследуют эти хендлеры через стандартный механизм propagation logging.
# ---------------------------------------------------------------------------

os.makedirs(LOG_DIR, exist_ok=True)

log_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)

file_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, "bot.log"),
    maxBytes=5 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
file_handler.setFormatter(log_formatter)

root_logger = logging.getLogger()
root_logger.setLevel(LOG_LEVEL)
root_logger.addHandler(console_handler)
root_logger.addHandler(file_handler)

# discord.py сам по себе очень многословный на DEBUG/INFO - приглушаем его чуть выше нашего уровня
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("discord.http").setLevel(logging.WARNING)

log = logging.getLogger("main")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents, help_command=None)


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------

@bot.command(name="togif", aliases=["gif"])
async def togif_prefix(ctx: commands.Context, *, text: str = ""):
    """!togif [ссылка] [имя] — конвертирует изображение в GIF."""
    context = (
        f"prefix, user={ctx.author} ({ctx.author.id}), "
        f"guild={ctx.guild.id if ctx.guild else 'DM'}, channel={ctx.channel.id}"
    )
    log.info(f"Команда !togif вызвана: {context}")

    url = togif.extract_url_from_text(text)
    filename_hint = text.replace(url, "", 1).strip() if url else text.strip()

    async with ctx.typing(), aiohttp.ClientSession() as session:
        async def respond(error, file):
            if error:
                await ctx.reply(error, mention_author=False)
            else:
                await ctx.reply(file=file, mention_author=False)

        await togif.run_conversion(
            bot, session, respond,
            message=ctx.message,
            url_hint=url,
            filename_hint=filename_hint,
            context=context,
        )


@bot.tree.command(name="togif", description="Конвертировать изображение в GIF")
@app_commands.describe(
    attachment="Прикреплённая картинка",
    url="Ссылка на картинку или ссылка на сообщение с картинкой",
    filename="Название для гифки",
)
async def togif_slash(
    interaction: discord.Interaction,
    attachment: discord.Attachment | None = None,
    url: str | None = None,
    filename: str | None = None,
):
    context = (
        f"slash, user={interaction.user} ({interaction.user.id}), "
        f"guild={interaction.guild_id or 'DM'}, channel={interaction.channel_id}"
    )
    log.info(f"Команда /togif вызвана: {context}")
    await interaction.response.defer(thinking=True)
    async with aiohttp.ClientSession() as session:
        async def respond(error, file):
            if error:
                await interaction.followup.send(error)
            else:
                await interaction.followup.send(file=file)

        await togif.run_conversion(
            bot, session, respond,
            url_hint=url,
            attachment=attachment,
            filename_hint=filename,
            context=context,
        )


@bot.tree.context_menu(name="Convert to GIF")
async def togif_context_menu(interaction: discord.Interaction, message: discord.Message):
    """
    Контекстное меню сообщения (ПКМ по сообщению -> Apps -> Convert to GIF).
    В отличие от /togif, здесь есть прямой доступ к тому сообщению, по которому
    кликнули - reply-логика тут не нужна, картинка ищется прямо в нём.
    Название файла в контекстном меню всегда определяется через OCR (или "converted").
    """
    context = (
        f"context_menu, user={interaction.user} ({interaction.user.id}), "
        f"guild={interaction.guild_id or 'DM'}, channel={interaction.channel_id}, target_message={message.id}"
    )
    log.info(f"Контекстная команда Convert to GIF вызвана: {context}")
    await interaction.response.defer(thinking=True)
    async with aiohttp.ClientSession() as session:
        async def respond(error, file):
            if error:
                await interaction.followup.send(error)
            else:
                await interaction.followup.send(file=file)

        await togif.run_conversion(bot, session, respond, message=message, context=context)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    await bot.process_commands(message)
    await soyball.maybe_transfer_role(message)

    # Важно: проверяем именно ЯВНОЕ упоминание в тексте (<@id>), а не message.mentions —
    # Discord автоматически добавляет туда автора сообщения при обычном reply-пинге,
    # даже если в тексте нет буквального @упоминания.
    explicitly_mentioned = re.search(togif.MENTION_RE_TEMPLATE.format(bot.user.id), message.content) is not None
    if explicitly_mentioned and not message.content.startswith(COMMAND_PREFIX):
        context = (
            f"mention, user={message.author} ({message.author.id}), "
            f"guild={message.guild.id if message.guild else 'DM'}, channel={message.channel.id}"
        )
        log.info(f"Бот упомянут: {context}")

        text = togif.strip_mention(message.content, bot.user.id)
        url = togif.extract_url_from_text(text)
        filename_hint = text.replace(url, "", 1).strip() if url else text.strip()

        async with message.channel.typing(), aiohttp.ClientSession() as session:
            async def respond(error, file):
                if error:
                    await message.reply(error, mention_author=False)
                else:
                    await message.reply(file=file, mention_author=False)

            await togif.run_conversion(
                bot, session, respond,
                message=message,
                url_hint=url,
                filename_hint=filename_hint,
                context=context,
            )


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    """
    Discord часто добавляет embed НЕ сразу при отправке сообщения, а отдельным
    событием редактирования (пока сам подгружает превью ссылки на Tenor/GIPHY
    и т.п.). Поэтому передачу роли по гифке-ссылке нужно перепроверять и здесь,
    иначе такие сообщения (98% реальных гифок - это именно ссылки) будут пропущены.
    """
    if after.author.bot:
        return
    if not before.embeds and after.embeds:
        await soyball.maybe_transfer_role(after)


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    """Ловит ошибки !команд, которые не были обработаны внутри самой команды."""
    if isinstance(error, commands.CommandNotFound):
        return
    log.exception(f"Необработанная ошибка команды {ctx.command}: {error}", exc_info=error)
    try:
        await ctx.reply("Что-то пошло не так при выполнении команды.", mention_author=False)
    except discord.HTTPException:
        pass


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Ловит ошибки /команд, которые не были обработаны внутри самой команды."""
    log.exception(f"Необработанная ошибка слэш-команды {interaction.command}: {error}", exc_info=error)
    try:
        if interaction.response.is_done():
            await interaction.followup.send("Что-то пошло не так при выполнении команды.")
        else:
            await interaction.response.send_message("Что-то пошло не так при выполнении команды.")
    except discord.HTTPException:
        pass


@bot.event
async def on_disconnect():
    log.warning("Соединение с Discord потеряно, пытаюсь переподключиться...")


@bot.event
async def on_resumed():
    log.info("Соединение с Discord восстановлено")


@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        log.info(f"Синхронизировано слэш-команд глобально: {len(synced)}")
    except Exception:
        log.exception("Не удалось синхронизировать слэш-команды")
    log.info(
        f"Бот запущен как {bot.user} (id: {bot.user.id}), "
        f"на серверах: {len(bot.guilds)}, задержка: {bot.latency * 1000:.0f} мс"
    )
    renamer.start(bot)


def main():
    if not TOKEN:
        log.critical("Не задан токен бота (переменная окружения DISCORD_TOKEN).")
        raise SystemExit(
            "Не задан токен бота. Установите переменную окружения DISCORD_TOKEN "
            "(например, в файле .env)."
        )
    log.info("Запускаю бота...")
    try:
        bot.run(TOKEN, log_handler=None)  # логирование уже настроено вручную выше
    except discord.LoginFailure:
        log.critical("Неверный токен бота (LoginFailure). Проверь DISCORD_TOKEN в .env.")
        raise
    except Exception:
        log.critical("Бот аварийно завершил работу", exc_info=True)
        raise
    finally:
        log.info("Бот остановлен")


if __name__ == "__main__":
    main()