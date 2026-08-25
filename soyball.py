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


async def _matches_target_gif(message: discord.Message) -> bool:
    gif_attachments = [a for a in message.attachments if _is_gif_attachment(a)]
    for att in gif_attachments:
        try:
            data = await att.read()
        except discord.HTTPException as e:
            log.warning(f"role_transfer: не удалось скачать вложение {att.filename}: {e}")
            continue
        try:
            file_hash = await asyncio.to_thread(compute_perceptual_hash, data)
        except Exception:
            log.exception(f"role_transfer: не удалось вычислить хэш вложения {att.filename}")
            continue
        diff = file_hash - TARGET_HASH  # расстояние Хэмминга между хэшами
        log.debug(f"role_transfer: разница хэшей для {att.filename}: {diff} (порог {HASH_THRESHOLD})")
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
    Вызывается из on_message в main.py на каждое сообщение (кроме от ботов).
    """
    if TARGET_HASH is None or ROLE_ID is None:
        return

    if message.guild is None:
        return  # роли существуют только на сервере, не в ЛС

    if message.reference is None:
        return  # нужен именно reply на чьё-то сообщение

    if not message.attachments:
        return  # быстрый выход без скачивания, если вложений вообще нет

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