"""
Передача роли "по цепочке": кто-то отвечает (reply) на чужое сообщение
конкретной гифкой - и если у отвечающего есть роль ROLE_ID, а у автора
сообщения, которому ответили, её нет, роль переходит от одного к другому.
Ботам роль передавать нельзя (ни как отправителям, ни как получателям).

Сравнение гифки идёт через ПЕРЦЕПТИВНЫЙ хэш (imagehash.phash), а не точный
хэш файла (SHA256) - это специально: если гифку пересохранили, слегка
пережали или Discord отдал её в чуть другом виде, байты файла изменятся,
а perceptual hash останется практически тем же самым (сравнение по
допустимому порогу "расстояния", а не на побитовое совпадение).

ОТКУДА БЕРЁТСЯ ГИФКА (в 99% случаев это ссылка, а не файл-вложение):
  1. Файл-вложение (загруженный вручную .gif) - как и раньше.
  2. Embed картинка/превью - когда гифку кидают через встроенный пикер Discord
     (Tenor/GIPHY) или просто ссылкой, Discord сам разворачивает её в embed.
     Для анимации через Tenor реальное видео лежит в embed.video как .mp4,
     который PIL декодировать не умеет - поэтому берём embed.thumbnail
     (статичное превью, обычно первый кадр) - для сравнения по phash этого
     достаточно, картинка визуально та же.
  3. Прямая ссылка на .gif-файл в тексте сообщения (например, CDN-ссылка).

ВАЖНЫЙ НЮАНС: Discord часто добавляет embed НЕ сразу при отправке сообщения,
а отдельным событием редактирования (спустя доли секунды, пока сам Discord
подгружает превью ссылки). Поэтому main.py должен вызывать maybe_transfer_role
и на on_message, и на on_message_edit (когда в отредактированной версии
появился embed, которого не было до этого).

Настройка через .env:
    TARGET_HASH=<phash гифки-триггера, hex-строка>
    ROLE_ID=<id роли, которая передаётся>
    HASH_THRESHOLD=<необязательно, макс. допустимая разница хэшей, по умолчанию 8>

Хэш нужной гифки получается локальным файлом через hash_gif.py (см. рядом).

Если TARGET_HASH или ROLE_ID не заданы - модуль ничего не делает (тихо
пропускает проверку), чтобы не ломать бота, если фича не используется.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re

import aiohttp
import discord
import imagehash
from PIL import Image

log = logging.getLogger(__name__)

_target_hash_raw = os.environ.get("TARGET_HASH", "").strip()
_role_id_raw = os.environ.get("ROLE_ID", "").strip()
ROLE_ID = int(_role_id_raw) if _role_id_raw.isdigit() else None
HASH_THRESHOLD = int(os.environ.get("HASH_THRESHOLD", "8"))

TARGET_HASH: imagehash.ImageHash | None = None
if _target_hash_raw:
    try:
        TARGET_HASH = imagehash.hex_to_hash(_target_hash_raw)
    except ValueError:
        log.error(f"role_transfer: TARGET_HASH некорректен ({_target_hash_raw!r}), модуль отключён")

if TARGET_HASH is None or ROLE_ID is None:
    log.warning(
        "role_transfer: TARGET_HASH или ROLE_ID не заданы (или некорректны) в .env - "
        "передача роли по гифке отключена."
    )

GIF_URL_RE = re.compile(r"https?://\S+?\.gif(?:\?\S*)?", re.IGNORECASE)


def _is_gif_attachment(att: discord.Attachment) -> bool:
    if att.content_type and att.content_type.startswith("image/gif"):
        return True
    return (att.filename or "").lower().endswith(".gif")


def compute_perceptual_hash(data: bytes) -> imagehash.ImageHash:
    """Считает phash первого кадра изображения (для gif - первый кадр анимации)."""
    img = Image.open(io.BytesIO(data))
    if getattr(img, "n_frames", 1) > 1:
        img.seek(0)
    return imagehash.phash(img.convert("RGB"))


async def _download(session: aiohttp.ClientSession, url: str) -> bytes:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        resp.raise_for_status()
        return await resp.read()


def _collect_candidate_sources(message: discord.Message) -> list[tuple[str, str | None, discord.Attachment | None]]:
    """
    Собирает все потенциальные источники гифки в сообщении.
    Возвращает список (метка_для_лога, url_или_None, attachment_или_None).
    """
    sources: list[tuple[str, str | None, discord.Attachment | None]] = []

    for att in message.attachments:
        if _is_gif_attachment(att):
            sources.append((f"вложение {att.filename}", None, att))

    for embed in message.embeds:
        # embed.video - это mp4 у Tenor-гифок, PIL такое не откроет, пропускаем.
        # embed.thumbnail обычно есть даже для видео-embed'ов и визуально
        # совпадает с первым кадром - этого достаточно для сравнения по phash.
        if embed.thumbnail and embed.thumbnail.url:
            sources.append((f"embed thumbnail ({embed.thumbnail.url})", embed.thumbnail.url, None))
        elif embed.image and embed.image.url:
            sources.append((f"embed image ({embed.image.url})", embed.image.url, None))

    for m in GIF_URL_RE.finditer(message.content or ""):
        url = m.group(0)
        sources.append((f"ссылка в тексте ({url})", url, None))

    return sources


async def _matches_target_gif(message: discord.Message) -> bool:
    sources = _collect_candidate_sources(message)
    if not sources:
        return False

    async with aiohttp.ClientSession() as session:
        for label, url, attachment in sources:
            try:
                data = await attachment.read() if attachment is not None else await _download(session, url)
            except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning(f"role_transfer: не удалось получить {label}: {e}")
                continue

            try:
                file_hash = await asyncio.to_thread(compute_perceptual_hash, data)
            except Exception:
                log.exception(f"role_transfer: не удалось вычислить хэш для {label}")
                continue

            diff = file_hash - TARGET_HASH  # расстояние Хэмминга между хэшами
            log.debug(f"role_transfer: разница хэшей для {label}: {diff} (порог {HASH_THRESHOLD})")
            if diff <= HASH_THRESHOLD:
                return True

    return False


async def _get_reply_target(message: discord.Message) -> discord.Message | None:
    ref = message.reference.resolved
    if ref is None or isinstance(ref, discord.DeletedReferencedMessage):
        try:
            ref = await message.channel.fetch_message(message.reference.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"role_transfer: не удалось получить сообщение из reply: {e}")
            return None
    return ref


async def maybe_transfer_role(message: discord.Message) -> None:
    """
    Проверяет сообщение и, если все условия совпали, передаёт роль.
    Вызывается из on_message И on_message_edit в main.py (см. пояснение
    про отложенные embed'ы в начале файла).
    """
    if TARGET_HASH is None or ROLE_ID is None:
        return

    if message.guild is None:
        return  # роли существуют только на сервере, не в ЛС

    if message.reference is None:
        return  # нужен именно reply на чьё-то сообщение

    if not await _matches_target_gif(message):
        return

    giver = message.author  # discord.Member в контексте сервера
    if giver.bot:
        log.info("role_transfer: гифка-триггер отправлена ботом - игнорирую")
        return

    ref_message = await _get_reply_target(message)
    if ref_message is None:
        return

    receiver = ref_message.author
    if receiver.bot:
        log.info("role_transfer: получатель роли - бот, передача запрещена")
        return

    if receiver.id == giver.id:
        log.info("role_transfer: пользователь ответил сам себе - передавать нечего")
        return

    guild = message.guild
    role = guild.get_role(ROLE_ID)
    if role is None:
        log.warning(f"role_transfer: роль с ID {ROLE_ID} не найдена на сервере {guild.id}")
        return

    try:
        giver_member = guild.get_member(giver.id) or await guild.fetch_member(giver.id)
        receiver_member = guild.get_member(receiver.id) or await guild.fetch_member(receiver.id)
    except discord.NotFound:
        log.warning("role_transfer: не удалось получить участников сервера")
        return

    if role not in giver_member.roles:
        log.debug("role_transfer: у отправителя гифки нет этой роли - передача не требуется")
        return

    if role in receiver_member.roles:
        log.debug("role_transfer: у получателя роль уже есть - передача не требуется")
        return

    try:
        await giver_member.remove_roles(role, reason="Передача роли через гифку-триггер")
        await receiver_member.add_roles(role, reason="Передача роли через гифку-триггер")
    except discord.Forbidden:
        log.error(
            "role_transfer: не хватает прав для передачи роли. Проверь, что у бота есть "
            "право Manage Roles и его собственная роль стоит ВЫШЕ передаваемой в иерархии."
        )
        return
    except discord.HTTPException:
        log.exception("role_transfer: ошибка Discord API при передаче роли")
        return

    log.info(f"role_transfer: роль {role.name} ({role.id}) передана: {giver_member} -> {receiver_member}")