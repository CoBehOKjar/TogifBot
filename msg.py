"""
Одноразовая отправка сообщения от имени бота - без запуска основного bot.py.

Способы указать, куда писать:
  1. Ссылка на канал (просто отправит сообщение в канал):
       python send_message.py --link https://discord.com/channels/111/222 --text "Привет"

  2. Ссылка на сообщение (отправит ОТВЕТ / reply на это сообщение):
       python send_message.py --link https://discord.com/channels/111/222/333 --text "Привет"

     Дополнительно можно поставить реакцию на исходное сообщение (только для reply,
     если сообщение не указано - аргумент игнорируется):
       python send_message.py --link https://discord.com/channels/111/222/333 \
           --text "Привет" --react "<:SAJ:1417083618240495666>"

     Текст необязателен - можно поставить только реакцию, без отправки сообщения:
       python send_message.py --link https://discord.com/channels/111/222/333 \
           --react "<:SAJ:1417083618240495666>"

  3. Через ID по отдельности (guild можно не указывать для ЛС-канала):
       python send_message.py --guild 111 --channel 222 --text "Привет"
       python send_message.py --guild 111 --channel 222 --message 333 --text "Привет"
       python send_message.py --channel 222 --text "Привет в ЛС"

Текст можно передать без флага --text (как обычные аргументы после ID/ссылки)
либо через пайп (echo "текст" | python send_message.py --link ...). Текст
необязателен: если не передан ни текст, ни --react - скрипт завершится с ошибкой,
но если указан хотя бы --react (на reply) - можно обойтись вообще без текста.

Если указано --message (или ссылка с message id) - бот отвечает (reply) на это
сообщение. Если нет - просто отправляет обычное сообщение в канал.

Скрипт подключается к Discord, отправляет одно сообщение и сразу отключается -
постоянно держать его запущенным не нужно.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys

import discord

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

LINK_RE = re.compile(
    r"https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/"
    r"(?P<guild>\d+|@me)/(?P<channel>\d+)(?:/(?P<message>\d+))?"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Отправить сообщение (или ответ на сообщение) от имени бота.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--link", help="Ссылка на канал или сообщение Discord")
    parser.add_argument("--guild", type=int, help="ID сервера (не нужен для ЛС-канала)")
    parser.add_argument("--channel", type=int, help="ID канала")
    parser.add_argument("--message", type=int, help="ID сообщения, на которое нужно ответить (reply)")
    parser.add_argument(
        "--react",
        help=(
            "Эмодзи для реакции на сообщение, формат <:name:id> (или <a:name:id> для анимированных), "
            "либо обычный юникод-эмодзи. Учитывается только если это reply (указано --message "
            "или ссылка с ID сообщения) - иначе игнорируется."
        ),
    )
    parser.add_argument("--text", help="Текст сообщения")
    parser.add_argument("text_words", nargs="*", help="Текст сообщения (альтернатива --text)")
    return parser.parse_args()


def resolve_target(args: argparse.Namespace) -> tuple[int, int | None]:
    """Возвращает (channel_id, message_id) на основе ссылки и/или явных ID."""
    channel_id = args.channel
    message_id = args.message

    if args.link:
        m = LINK_RE.search(args.link)
        if not m:
            raise SystemExit(f"Не удалось распознать ссылку: {args.link}")
        if channel_id is None:
            channel_id = int(m.group("channel"))
        if message_id is None and m.group("message"):
            message_id = int(m.group("message"))

    if channel_id is None:
        raise SystemExit(
            "Не указан канал. Передай --link с ссылкой на канал/сообщение, "
            "либо --channel <id>."
        )

    return channel_id, message_id


def resolve_text(args: argparse.Namespace) -> str | None:
    if args.text:
        return args.text
    if args.text_words:
        return " ".join(args.text_words)
    if not sys.stdin.isatty():
        piped = sys.stdin.read().strip()
        if piped:
            return piped
    return None


async def run(channel_id: int, message_id: int | None, text: str | None, react: str | None) -> None:
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Не задан DISCORD_TOKEN (в .env или переменных окружения).")

    if react and not message_id:
        print("Аргумент --react проигнорирован: сообщение не указано (это не reply).")
        react = None

    if not text and not react:
        raise SystemExit(
            "Нечего делать: не указан ни текст сообщения (--text), ни реакция (--react на reply)."
        )

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)

            target_message = None
            if message_id:
                try:
                    target_message = await channel.fetch_message(message_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                    print(f"Не удалось найти сообщение {message_id} в канале {channel_id}: {e}")
                    return

            if text:
                if target_message is not None:
                    sent = await target_message.reply(text, mention_author=False)
                    print(f"Отправлен ответ (reply) на сообщение {message_id}: {sent.jump_url}")
                else:
                    sent = await channel.send(text)
                    print(f"Сообщение отправлено в канал {channel_id}: {sent.jump_url}")

            if react and target_message is not None:
                try:
                    emoji = discord.PartialEmoji.from_str(react)
                    await target_message.add_reaction(emoji)
                    print(f"Реакция {react} добавлена на сообщение {message_id}")
                except (discord.HTTPException, discord.Forbidden) as e:
                    print(f"Не удалось поставить реакцию {react}: {e}")
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"Ошибка при отправке: {e}")
        finally:
            await client.close()

    await client.start(token)


if __name__ == "__main__":
    parsed_args = parse_args()
    resolved_channel_id, resolved_message_id = resolve_target(parsed_args)
    message_text = resolve_text(parsed_args)
    asyncio.run(run(resolved_channel_id, resolved_message_id, message_text, parsed_args.react))