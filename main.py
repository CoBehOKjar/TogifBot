from __future__ import annotations

import asyncio
import io
import logging
import os
import re

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageSequence

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("gif-bot")

TOKEN = os.environ.get("DISCORD_TOKEN")
COMMAND_PREFIX = "!"

IMAGE_EXT_RE = re.compile(r"\.(png|jpe?g|webp|bmp|gif|tiff?)(\?.*)?$", re.IGNORECASE)
URL_RE = re.compile(r"https?://\S+")
MESSAGE_LINK_RE = re.compile(
    r"https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/"
    r"(?P<guild>\d+|@me)/(?P<channel>\d+)/(?P<message>\d+)"
)
MENTION_RE_TEMPLATE = r"<@!?{0}>"

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents, help_command=None)


class ImageNotFound(Exception):
    """Не удалось найти изображение по указанным источникам."""


# ---------------------------------------------------------------------------
# Поиск и загрузка изображения
# ---------------------------------------------------------------------------

async def download_bytes(session: aiohttp.ClientSession, url: str) -> bytes:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                raise ImageNotFound(f"Не удалось скачать файл по ссылке (код {resp.status}).")
            ctype = resp.headers.get("Content-Type", "")
            if not ctype.startswith("image/") and not IMAGE_EXT_RE.search(url):
                raise ImageNotFound("По указанной ссылке нет изображения.")
            return await resp.read()
    except asyncio.TimeoutError:
        raise ImageNotFound("Превышено время ожидания при скачивании файла.")
    except aiohttp.ClientError as e:
        raise ImageNotFound(f"Ошибка сети при скачивании файла: {e}")


def strip_mention(text: str, bot_id: int) -> str:
    return re.sub(MENTION_RE_TEMPLATE.format(bot_id), "", text or "").strip()


def extract_url_from_text(text: str) -> str | None:
    if not text:
        return None
    candidates = [m.group(0) for m in URL_RE.finditer(text) if not MESSAGE_LINK_RE.match(m.group(0))]
    for url in candidates:
        if IMAGE_EXT_RE.search(url):
            return url
    # ссылки на Discord CDN часто не имеют расширения в конце - берём первую оставшуюся
    return candidates[0] if candidates else None


def extract_message_link(text: str):
    if not text:
        return None
    m = MESSAGE_LINK_RE.search(text)
    if not m:
        return None
    guild_raw = m.group("guild")
    guild_id = None if guild_raw == "@me" else int(guild_raw)
    return guild_id, int(m.group("channel")), int(m.group("message"))


async def image_from_message(message: discord.Message, session: aiohttp.ClientSession) -> bytes | None:
    """Ищет картинку внутри конкретного сообщения: вложение, embed или ссылка в тексте."""
    for att in message.attachments:
        if (att.content_type and att.content_type.startswith("image/")) or IMAGE_EXT_RE.search(att.filename or ""):
            return await att.read()

    for embed in message.embeds:
        if embed.image and embed.image.url:
            return await download_bytes(session, embed.image.url)
        if embed.thumbnail and embed.thumbnail.url:
            return await download_bytes(session, embed.thumbnail.url)

    url = extract_url_from_text(message.content)
    if url:
        return await download_bytes(session, url)

    return None


async def image_from_link(bot: commands.Bot, session: aiohttp.ClientSession, link) -> bytes | None:
    _, channel_id, message_id = link
    try:
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        target_message = await channel.fetch_message(message_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None
    return await image_from_message(target_message, session)


async def resolve_image(
    bot: commands.Bot,
    session: aiohttp.ClientSession,
    *,
    message: discord.Message | None = None,
    url_hint: str | None = None,
    attachment: discord.Attachment | None = None,
) -> bytes:
    """
    Порядок поиска изображения:
    1. Явно переданное вложение (параметр слэш-команды).
    2. Явно переданная ссылка (параметр слэш-команды или аргумент !команды) —
       может быть как прямой ссылкой на картинку, так и ссылкой на сообщение Discord.
    3. Вложение к самому сообщению с командой/упоминанием.
    4. Ссылка на сообщение Discord, указанная в тексте команды.
    5. Обычная ссылка на картинку в тексте команды.
    6. Сообщение, на которое отвечает (reply) сообщение с командой.
    """
    if attachment is not None:
        return await attachment.read()

    if url_hint:
        link = extract_message_link(url_hint)
        if link:
            img = await image_from_link(bot, session, link)
            if img:
                return img
        else:
            return await download_bytes(session, url_hint)

    if message is not None:
        img = await image_from_message(message, session)
        if img:
            return img

        link = extract_message_link(message.content)
        if link:
            img = await image_from_link(bot, session, link)
            if img:
                return img

        if message.reference is not None:
            ref = message.reference.resolved
            if ref is None or isinstance(ref, discord.DeletedReferencedMessage):
                try:
                    ref = await message.channel.fetch_message(message.reference.message_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    ref = None
            if ref:
                img = await image_from_message(ref, session)
                if img:
                    return img

    raise ImageNotFound(
        "Не нашёл изображение 🤔\n"
        "Прикрепи картинку, укажи ссылку на неё, вставь ссылку на сообщение с картинкой "
        "или ответь (reply) на сообщение с картинкой."
    )


# ---------------------------------------------------------------------------
# Конвертация в GIF
# ---------------------------------------------------------------------------

def convert_to_gif(data: bytes) -> io.BytesIO:
    src = Image.open(io.BytesIO(data))
    output = io.BytesIO()

    try:
        n_frames = getattr(src, "n_frames", 1)
    except Exception:
        n_frames = 1

    if n_frames > 1:
        # анимированный источник (gif/webp) — переносим все кадры
        frames = []
        durations = []
        for frame in ImageSequence.Iterator(src):
            frames.append(frame.convert("RGBA"))
            durations.append(frame.info.get("duration", 100))
        frames[0].save(
            output,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=0,
            disposal=2,
        )
    else:
        # статичное изображение — один кадр GIF
        frame = src.convert("RGBA")
        background = Image.new("RGBA", frame.size, (255, 255, 255, 0))
        background.paste(frame, (0, 0), frame)
        background.convert("P", palette=Image.ADAPTIVE).save(output, format="GIF")

    output.seek(0)
    return output


# ---------------------------------------------------------------------------
# Общая логика запуска конвертации
# ---------------------------------------------------------------------------

async def run_conversion(
    session: aiohttp.ClientSession,
    respond,
    *,
    message: discord.Message | None = None,
    url_hint: str | None = None,
    attachment: discord.Attachment | None = None,
) -> None:
    try:
        data = await resolve_image(bot, session, message=message, url_hint=url_hint, attachment=attachment)
    except ImageNotFound as e:
        await respond(str(e), None)
        return

    try:
        gif_buffer = await asyncio.to_thread(convert_to_gif, data)
    except Exception as e:
        log.exception("Ошибка при конвертации изображения")
        await respond(f"Не получилось сконвертировать изображение: {e}", None)
        return

    await respond(None, discord.File(gif_buffer, filename="converted.gif"))


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------

@bot.command(name="togif", aliases=["gif"])
async def togif_prefix(ctx: commands.Context, url: str | None = None):
    """!togif [ссылка] — конвертирует изображение в GIF."""
    async with ctx.typing(), aiohttp.ClientSession() as session:
        async def respond(error, file):
            if error:
                await ctx.reply(error, mention_author=False)
            else:
                await ctx.reply(file=file, mention_author=False)

        await run_conversion(session, respond, message=ctx.message, url_hint=url)


@bot.tree.command(name="togif", description="Конвертировать изображение в GIF")
@app_commands.describe(
    attachment="Прикреплённая картинка",
    url="Ссылка на картинку или ссылка на сообщение с картинкой",
)
async def togif_slash(
    interaction: discord.Interaction,
    attachment: discord.Attachment | None = None,
    url: str | None = None,
):
    await interaction.response.defer(thinking=True)
    async with aiohttp.ClientSession() as session:
        async def respond(error, file):
            if error:
                await interaction.followup.send(error)
            else:
                await interaction.followup.send(file=file)

        await run_conversion(session, respond, url_hint=url, attachment=attachment)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    await bot.process_commands(message)

    if bot.user in message.mentions and not message.content.startswith(COMMAND_PREFIX):
        async with message.channel.typing(), aiohttp.ClientSession() as session:
            async def respond(error, file):
                if error:
                    await message.reply(error, mention_author=False)
                else:
                    await message.reply(file=file, mention_author=False)

            await run_conversion(session, respond, message=message)


@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        log.info(f"Синхронизировано слэш-команд: {len(synced)}")
    except Exception:
        log.exception("Не удалось синхронизировать слэш-команды")
    log.info(f"Бот запущен как {bot.user} (id: {bot.user.id})")


def main():
    if not TOKEN:
        raise SystemExit(
            "Не задан токен бота. Установите переменную окружения DISCORD_TOKEN "
            "(например, в файле .env)."
        )
    bot.run(TOKEN)


if __name__ == "__main__":
    main()