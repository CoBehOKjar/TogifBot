"""
Вся логика превращения изображения в GIF: поиск источника картинки,
конвертация (с сохранением прозрачности и анимации), OCR для авто-названия
и транслитерация в slug для имени файла.

main.py регистрирует команды и вызывает функции отсюда - в этом модуле нет
ничего, что зависит от конкретного способа вызова (!команда/слэш/меню).
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
import time

import aiohttp
import discord
from discord.ext import commands
from PIL import Image, ImageSequence

try:
    import pytesseract
except ImportError:
    pytesseract = None

try:
    from unidecode import unidecode
except ImportError:
    def unidecode(text: str) -> str:
        # запасной вариант без пакета unidecode: просто отбрасывает не-ASCII
        # символы вместо транслитерации. Для нормальной работы установи unidecode.
        return text.encode("ascii", "ignore").decode("ascii")

log = logging.getLogger(__name__)

IMAGE_EXT_RE = re.compile(r"\.(png|jpe?g|webp|bmp|gif|tiff?)(\?.*)?$", re.IGNORECASE)
URL_RE = re.compile(r"https?://\S+")
MESSAGE_LINK_RE = re.compile(
    r"https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/"
    r"(?P<guild>\d+|@me)/(?P<channel>\d+)/(?P<message>\d+)"
)
MENTION_RE_TEMPLATE = r"<@!?{0}>"

ALPHA_THRESHOLD = 128
MAX_SLUG_LENGTH = 60
DEFAULT_FILENAME = "converted"


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
    6. Сообщение, на которое отвечает (reply) сообщение с командой (не работает для слэш-команд).
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
            log.info("Проверяю сообщение, на которое ответили (message.reference)")
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
        "или ответь (не работает для слэш-команд) на сообщение с картинкой."
    )


# ---------------------------------------------------------------------------
# Конвертация в GIF
# ---------------------------------------------------------------------------

def _to_p_frame_with_transparency(frame: Image.Image) -> Image.Image:
    """
    Переводит RGBA-кадр в палитровый режим P, сохраняя альфа-канал как
    бинарную GIF-прозрачность вместо заливки фона сплошным цветом.
    """
    frame = frame.convert("RGBA")
    alpha = frame.split()[3]
    transparent_mask = alpha.point(lambda a: 255 if a < ALPHA_THRESHOLD else 0)

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

    log.info(
        f"Конвертирую изображение: формат={src.format}, размер={src.size}, "
        f"кадров={n_frames}, вход={len(data)} байт"
    )

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
# Название файла: явное имя -> OCR -> "converted", всегда в виде slug
# ---------------------------------------------------------------------------

def make_slug(text: str, max_length: int = MAX_SLUG_LENGTH) -> str:
    """
    Приводит произвольный текст (в т.ч. кириллицу) к slug-формату для имени файла:
    транслит в латиницу, нижний регистр, пробелы/спецсимволы -> "_".
    Возвращает пустую строку, если после очистки ничего не осталось.
    """
    if not text:
        return ""
    transliterated = unidecode(text)
    lowered = transliterated.lower()
    slug = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
    if not slug:
        return ""
    if len(slug) > max_length:
        slug = slug[:max_length].rstrip("_")
    return slug


def extract_text_from_image(data: bytes) -> str | None:
    """
    Пытается распознать текст на изображении (первый кадр, если анимированное)
    через Tesseract OCR. Возвращает None, если распознавание недоступно или
    ничего не нашлось - вызывающий код в этом случае просто использует "converted".
    """
    if pytesseract is None:
        log.debug("pytesseract не установлен - OCR для имени файла недоступен")
        return None

    try:
        img = Image.open(io.BytesIO(data))
        if getattr(img, "n_frames", 1) > 1:
            img.seek(0)
        img = img.convert("RGB")
        raw_text = pytesseract.image_to_string(img, lang="rus+eng").strip()
    except Exception:
        log.exception("Не удалось распознать текст на изображении (OCR)")
        return None

    if not raw_text:
        return None

    first_line = raw_text.splitlines()[0].strip()
    log.info(f"OCR распознал текст для имени файла: {first_line!r}")
    return first_line or None


# ---------------------------------------------------------------------------
# Общая логика запуска конвертации
# ---------------------------------------------------------------------------

async def run_conversion(
    bot: commands.Bot,
    session: aiohttp.ClientSession,
    respond,
    *,
    message: discord.Message | None = None,
    url_hint: str | None = None,
    attachment: discord.Attachment | None = None,
    filename_hint: str | None = None,
    context: str = "",
) -> None:
    log.info(
        f"Запрос на конвертацию [{context}]: url_hint={url_hint!r}, "
        f"attachment={bool(attachment)}, filename={filename_hint!r}"
    )
    try:
        data = await resolve_image(bot, session, message=message, url_hint=url_hint, attachment=attachment)
    except ImageNotFound as e:
        log.info(f"Изображение не найдено [{context}]")
        await respond(str(e), None)
        return

    # 1. Явно указанное название -> slug
    final_filename = ""
    if filename_hint:
        final_filename = make_slug(filename_hint)

    # 2. Название не дали (или после слага ничего не осталось) - пробуем OCR
    if not final_filename:
        ocr_text = await asyncio.to_thread(extract_text_from_image, data)
        if ocr_text:
            final_filename = make_slug(ocr_text)

    # 3. OCR тоже не помог - "converted"
    if not final_filename:
        final_filename = DEFAULT_FILENAME

    try:
        gif_buffer = await asyncio.to_thread(convert_to_gif, data)
    except Exception as e:
        log.exception(f"Ошибка при конвертации изображения [{context}]")
        await respond(f"Не получилось сконвертировать изображение: {e}", None)
        return

    log.info(f"Успешно отправляю результат [{context}] как {final_filename}.gif")
    await respond(None, discord.File(gif_buffer, filename=f"{final_filename}.gif"))