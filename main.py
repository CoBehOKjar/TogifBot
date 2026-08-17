from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import time
from logging.handlers import RotatingFileHandler

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

TOKEN = os.environ.get("DISCORD_TOKEN")
COMMAND_PREFIX = "!"
LOG_DIR = os.environ.get("LOG_DIR", "logs")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# ---------------------------------------------------------------------------
# Настройка логирования: консоль + файл с ротацией (5 файлов по 5 МБ)
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

log = logging.getLogger("gif-bot")

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
    log.debug(f"Скачиваю файл по ссылке: {url}")
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                log.warning(f"Скачивание не удалось ({resp.status}): {url}")
                raise ImageNotFound(f"Не удалось скачать файл по ссылке (код {resp.status}).")
            ctype = resp.headers.get("Content-Type", "")
            if not ctype.startswith("image/") and not IMAGE_EXT_RE.search(url):
                log.warning(f"Ссылка не похожа на изображение (Content-Type={ctype!r}): {url}")
                raise ImageNotFound("По указанной ссылке нет изображения.")
            data = await resp.read()
            log.debug(f"Скачано {len(data)} байт, Content-Type={ctype!r}")
            return data
    except asyncio.TimeoutError:
        log.warning(f"Таймаут при скачивании: {url}")
        raise ImageNotFound("Превышено время ожидания при скачивании файла.")
    except aiohttp.ClientError as e:
        log.warning(f"Ошибка сети при скачивании {url}: {e}")
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
            log.info(f"Найдено вложение в сообщении {message.id}: {att.filename} ({att.size} байт)")
            return await att.read()

    for embed in message.embeds:
        if embed.image and embed.image.url:
            log.info(f"Найдена картинка в embed сообщения {message.id}")
            return await download_bytes(session, embed.image.url)
        if embed.thumbnail and embed.thumbnail.url:
            log.info(f"Найден thumbnail в embed сообщения {message.id}")
            return await download_bytes(session, embed.thumbnail.url)

    url = extract_url_from_text(message.content)
    if url:
        log.info(f"Найдена ссылка на картинку в тексте сообщения {message.id}: {url}")
        return await download_bytes(session, url)

    return None


async def image_from_link(bot: commands.Bot, session: aiohttp.ClientSession, link) -> bytes | None:
    _, channel_id, message_id = link
    log.info(f"Перехожу по ссылке на сообщение: channel={channel_id}, message={message_id}")
    try:
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        target_message = await channel.fetch_message(message_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        log.warning(f"Не удалось получить сообщение по ссылке (channel={channel_id}, message={message_id}): {e}")
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
        log.info(f"Источник изображения: явное вложение параметра ({attachment.filename})")
        return await attachment.read()

    if url_hint:
        link = extract_message_link(url_hint)
        if link:
            log.info(f"Источник изображения: явная ссылка на сообщение ({url_hint})")
            img = await image_from_link(bot, session, link)
            if img:
                return img
        else:
            log.info(f"Источник изображения: явная ссылка на файл ({url_hint})")
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
            log.info(f"Проверяю сообщение, на которое ответили (message.reference)")
            ref = message.reference.resolved
            if ref is None or isinstance(ref, discord.DeletedReferencedMessage):
                try:
                    ref = await message.channel.fetch_message(message.reference.message_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                    log.warning(f"Не удалось получить сообщение из reply: {e}")
                    ref = None
            if ref:
                img = await image_from_message(ref, session)
                if img:
                    return img

    log.info("Изображение не найдено ни по одному из источников")
    raise ImageNotFound(
        "Не нашёл изображение\n"
        "Прикрепи картинку, укажи ссылку на неё, вставь ссылку на сообщение с картинкой "
        "или ответь (не работает для слеш-команд) на сообщение с картинкой."
    )


# ---------------------------------------------------------------------------
# Конвертация в GIF
# ---------------------------------------------------------------------------

# GIF не поддерживает плавную прозрачность - только бинарную (пиксель либо
# виден, либо полностью прозрачен). Порог: alpha ниже этого значения = прозрачный.
ALPHA_THRESHOLD = 128


def _to_p_frame_with_transparency(frame: Image.Image) -> Image.Image:
    """
    Переводит RGBA-кадр в палитровый режим P, сохраняя альфа-канал как
    бинарную GIF-прозрачность вместо заливки фона сплошным цветом.
    """
    frame = frame.convert("RGBA")
    alpha = frame.split()[3]
    transparent_mask = alpha.point(lambda a: 255 if a < ALPHA_THRESHOLD else 0)

    # оставляем один свободный слот в палитре (индекс 255) под прозрачный цвет
    p_frame = frame.convert("RGB").convert("P", palette=Image.ADAPTIVE, colors=255)
    p_frame.paste(255, transparent_mask)
    p_frame.info["transparency"] = 255
    return p_frame


def convert_to_gif(data: bytes) -> io.BytesIO:

    start = time.monotonic()
    src = Image.open(io.BytesIO(data))
    output = io.BytesIO()

    try:
        n_frames = getattr(src, "n_frames", 1)
    except Exception:
        n_frames = 1

    log.info(f"Конвертирую изображение: формат={src.format}, размер={src.size}, кадров={n_frames}, вход={len(data)} байт")

    if n_frames > 1:
        # анимированный источник (gif/webp) — переносим все кадры с прозрачностью
        frames = []
        durations = []
        for frame in ImageSequence.Iterator(src):
            durations.append(frame.info.get("duration", 100))
            frames.append(_to_p_frame_with_transparency(frame))
        frames[0].save(
            output,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=0,
            disposal=2,
            transparency=255,
        )
    else:
        # статичное изображение — один кадр GIF с сохранённой прозрачностью
        p_frame = _to_p_frame_with_transparency(src)
        p_frame.save(output, format="GIF", transparency=255, disposal=2)

    output.seek(0)
    elapsed = time.monotonic() - start
    size = output.getbuffer().nbytes
    log.info(f"Конвертация завершена за {elapsed:.2f} сек, выход={size} байт")
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
    context: str = "",
) -> None:
    log.info(f"Запрос на конвертацию [{context}]: url_hint={url_hint!r}, attachment={bool(attachment)}")
    try:
        data = await resolve_image(bot, session, message=message, url_hint=url_hint, attachment=attachment)
    except ImageNotFound as e:
        log.info(f"Изображение не найдено [{context}]")
        await respond(str(e), None)
        return

    try:
        gif_buffer = await asyncio.to_thread(convert_to_gif, data)
    except Exception as e:
        log.exception(f"Ошибка при конвертации изображения [{context}]")
        await respond(f"Не получилось сконвертировать изображение: {e}", None)
        return

    log.info(f"Успешно отправляю результат [{context}]")
    await respond(None, discord.File(gif_buffer, filename="converted.gif"))


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------

@bot.command(name="togif", aliases=["gif"])
async def togif_prefix(ctx: commands.Context, url: str | None = None):
    """!togif [ссылка] — конвертирует изображение в GIF."""
    context = f"prefix, user={ctx.author} ({ctx.author.id}), guild={ctx.guild.id if ctx.guild else 'DM'}, channel={ctx.channel.id}"
    log.info(f"Команда !togif вызвана: {context}")
    async with ctx.typing(), aiohttp.ClientSession() as session:
        async def respond(error, file):
            if error:
                await ctx.reply(error, mention_author=False)
            else:
                await ctx.reply(file=file, mention_author=False)

        await run_conversion(session, respond, message=ctx.message, url_hint=url, context=context)


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

        await run_conversion(session, respond, url_hint=url, attachment=attachment, context=context)


@bot.tree.context_menu(name="Convert to GIF")
async def togif_context_menu(interaction: discord.Interaction, message: discord.Message):
    """
    Контекстное меню сообщения (ПКМ по сообщению -> Apps -> Convert to GIF).
    В отличие от /togif, здесь есть прямой доступ к тому сообщению, по которому
    кликнули - reply-логика тут не нужна, картинка ищется прямо в нём.
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

        await run_conversion(session, respond, message=message, context=context)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    await bot.process_commands(message)

    # Важно: проверяем именно ЯВНОЕ упоминание в тексте (<@id>), а не message.mentions —
    # Discord автоматически добавляет туда автора сообщения при обычном reply-пинге,
    # даже если в тексте нет буквального @упоминания. Из-за этого бот реагировал
    # на любой ответ на своё собственное сообщение.
    explicitly_mentioned = re.search(MENTION_RE_TEMPLATE.format(bot.user.id), message.content) is not None
    if explicitly_mentioned and not message.content.startswith(COMMAND_PREFIX):
        context = (
            f"mention, user={message.author} ({message.author.id}), "
            f"guild={message.guild.id if message.guild else 'DM'}, channel={message.channel.id}"
        )
        log.info(f"Бот упомянут: {context}")
        async with message.channel.typing(), aiohttp.ClientSession() as session:
            async def respond(error, file):
                if error:
                    await message.reply(error, mention_author=False)
                else:
                    await message.reply(file=file, mention_author=False)

            await run_conversion(session, respond, message=message, context=context)


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