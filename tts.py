"""
TTS-озвучка на Piper (piper1-gpl): бот заходит в голосовой канал по /join и
читает сообщения из текстового чата этого канала.

Подключение в main.py:
    tts.register(bot)            - один раз после создания бота (добавляет слэш-команды)
    tts.start()                  - из on_ready (фоновое слежение за папкой голосов)
    tts.handle_message(message)  - из on_message
    tts.handle_voice_state_update(bot, member, before, after) - из on_voice_state_update

Слэш-команды: /join, /leave, /said, /ignore, /voice, /emoji, /speed.

Настройка через .env (всё необязательно):
    VOICES_DIR=voices                    - папка с голосами (*.onnx + *.onnx.json)
    DEFAULT_VOICE=ru_RU-denis-medium     - голос по умолчанию
    DATA_FILE=data.json                  - файл с настройками пользователей/серверов
    MAX_CHARS=400                        - макс. длина одного сообщения
    MAX_QUEUE=30                         - макс. очередь озвучки на сервер
    MAX_CACHED_VOICES=2                  - сколько голосов держать в памяти
    ONLY_FROM_VOICE_MEMBERS=false        - читать только тех, кто сидит в голосовом канале
    TTS_MALLOC_TRIM=true                 - возвращать освобождённую память системе (против разрастания RAM)

Требуется: pip install "discord.py[voice]" piper-tts emoji, а в системе - ffmpeg.
Если piper/emoji не установлены, модуль отключается, остальной бот работает.
"""
from __future__ import annotations

import asyncio
import contextlib
import ctypes
import gc
import importlib.util
import io
import json
import logging
import os
import re
import shutil
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands

try:
    import emoji as emoji_lib
    from piper import PiperVoice, SynthesisConfig

    ENABLED = True
    _import_error = None
except ImportError as e:  # модуль отключается, но бот при этом работает
    emoji_lib = None
    PiperVoice = None
    SynthesisConfig = None
    ENABLED = False
    _import_error = e

try:
    import onnxruntime as _ort

    _orig_session_options = _ort.SessionOptions

    def _lean_session_options():
        opts = _orig_session_options()
        opts.enable_cpu_mem_arena = False   # не копить память в arena
        opts.enable_mem_pattern = False     # не кэшировать паттерны под разные длины входа
        return opts

    _ort.SessionOptions = _lean_session_options
except ImportError:
    pass

log = logging.getLogger(__name__)

VOICES_DIR = Path(os.getenv("VOICES_DIR", "voices"))
DEFAULT_VOICE = os.getenv("DEFAULT_VOICE", "ru_RU-denis-medium")
DATA_FILE = Path(os.getenv("DATA_FILE", "data.json"))
MAX_CHARS = int(os.getenv("MAX_CHARS", "400"))
MAX_QUEUE = int(os.getenv("MAX_QUEUE", "30"))
MAX_CACHED_VOICES = int(os.getenv("MAX_CACHED_VOICES", "2"))
ONLY_FROM_VOICE_MEMBERS = os.getenv("ONLY_FROM_VOICE_MEMBERS", "false").lower() == "true"
# Возвращать освобождённую память системе (glibc malloc_trim) после синтеза и выгрузки голосов
TRIM_MEMORY = os.getenv("TTS_MALLOC_TRIM", "true").lower() == "true"


def rss_mb() -> Optional[int]:
    """Текущая память процесса в МБ (только Linux), для диагностики утечек."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None


_libc = None


def trim_memory() -> None:
    """Просит glibc вернуть системе освобождённую, но не отданную память (фрагментация)."""
    global _libc
    if not TRIM_MEMORY:
        return
    try:
        if _libc is None:
            _libc = ctypes.CDLL("libc.so.6")
        _libc.malloc_trim(0)
    except (OSError, AttributeError):
        pass  # не glibc - ничего страшного


# ───────────────────────── Хранилище настроек ─────────────────────────

class Store:
    USER_DEFAULTS = {"voice": None, "emoji": False, "speed": 1.0}
    GUILD_DEFAULTS = {"said": False, "ignored": []}

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = json.loads(path.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}
        self.data.setdefault("users", {})
        self.data.setdefault("guilds", {})

    def user(self, uid: int) -> dict:
        return {**self.USER_DEFAULTS, **self.data["users"].get(str(uid), {})}

    def set_user(self, uid: int, **kwargs):
        self.data["users"].setdefault(str(uid), {}).update(kwargs)
        self.save()

    def guild(self, gid: int) -> dict:
        g = self.data["guilds"].setdefault(str(gid), {})
        for k, v in self.GUILD_DEFAULTS.items():
            g.setdefault(k, type(v)() if isinstance(v, list) else v)
        return g

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, self.path)


# ───────────────────────── Библиотека голосов ─────────────────────────

class VoiceLibrary:
    def __init__(self, folder: Path):
        self.folder = folder
        self.voices: dict[str, tuple[Path, float]] = {}
        self._cache: OrderedDict = OrderedDict()

    def scan(self) -> bool:
        """Пересканирует папку. Возвращает True, если список изменился."""
        found: dict[str, tuple[Path, float]] = {}
        now = time.time()
        for onnx in self.folder.glob("*.onnx"):
            cfg = onnx.with_name(onnx.name + ".json")
            try:
                mtime = max(onnx.stat().st_mtime, cfg.stat().st_mtime)
            except OSError:
                continue  # нет конфига или файл удалён
            if now - mtime < 2:
                continue  # файл ещё копируется
            found[onnx.stem] = (onnx, mtime)
        changed = found.keys() != self.voices.keys() or any(
            found[k][1] != self.voices[k][1] for k in found
        )
        if changed:
            self.voices = found
        return changed

    async def watch(self):
        while True:
            try:
                if self.scan():
                    log.info("Доступные голоса: %s", ", ".join(self.names()) or "—")
            except Exception:
                log.exception("Ошибка сканирования папки голосов")
            await asyncio.sleep(1)

    def names(self) -> list[str]:
        return sorted(self.voices)

    def resolve(self, preferred: Optional[str]) -> Optional[str]:
        """Выбранный голос -> голос по умолчанию -> первый доступный."""
        if preferred in self.voices:
            return preferred
        if DEFAULT_VOICE in self.voices:
            return DEFAULT_VOICE
        return next(iter(self.names()), None)

    def load(self, name: str):
        """Вызывать только из потока синтеза."""
        for key in [k for k in self._cache if k not in self.voices]:
            del self._cache[key]
        path, mtime = self.voices[name]
        cached = self._cache.pop(name, None)
        if cached and cached[0] == mtime:
            self._cache[name] = cached
            return cached[1]
        log.info(f"tts: загружаю голос {name} с диска (в кэше: {list(self._cache)}, память {rss_mb()} МБ)")
        voice = PiperVoice.load(path)
        self._cache[name] = (mtime, voice)
        evicted = False
        while len(self._cache) > MAX_CACHED_VOICES:
            old, _ = self._cache.popitem(last=False)
            log.info(f"tts: выгружаю голос {old} из кэша (MAX_CACHED_VOICES={MAX_CACHED_VOICES})")
            evicted = True
        if evicted:
            gc.collect()
            trim_memory()
        log.info(f"tts: голос {name} загружен, память {rss_mb()} МБ")
        return voice


store = Store(DATA_FILE)
library = VoiceLibrary(VOICES_DIR)
# espeak внутри Piper не потокобезопасен, поэтому синтез идёт в одном потоке
SYNTH_POOL = ThreadPoolExecutor(max_workers=1)


def synthesize(voice_name: str, text: str, speed: float) -> tuple[bytes, int]:
    voice = library.load(voice_name)
    base = getattr(voice.config, "length_scale", None) or 1.0
    # length_scale меняет длительность на уровне модели, высота голоса не меняется
    cfg = SynthesisConfig(length_scale=base / speed)
    pcm = b"".join(c.audio_int16_bytes for c in voice.synthesize(text, syn_config=cfg))
    trim_memory()
    return pcm, voice.config.sample_rate


# ───────────────────────── Очистка текста ─────────────────────────

CODE_BLOCK_RE = re.compile(r"```.*?```", re.S)
URL_RE = re.compile(r"https?://\S+")
CUSTOM_EMOJI_RE = re.compile(r"<a?:(\w+):\d+>")
MD_RE = re.compile(r"[*~|`>]+")
SPACE_RE = re.compile(r"\s+")


def handle_unicode_emoji(text: str, read: bool) -> str:
    if read:
        text = emoji_lib.demojize(text, language="ru", delimiters=(" ", " "))
        return text.replace("_", " ")
    return emoji_lib.replace_emoji(text, "")


def clean_message(message: discord.Message, read_emoji: bool) -> str:
    text = message.clean_content  # упоминания уже превращены в имена
    text = CODE_BLOCK_RE.sub(" код ", text)
    text = URL_RE.sub(" ссылка ", text)
    text = CUSTOM_EMOJI_RE.sub((lambda m: f" {m.group(1)} ") if read_emoji else " ", text)
    text = MD_RE.sub(" ", text)
    text = handle_unicode_emoji(text, read_emoji)
    if message.attachments:
        text += " вложение"
    return SPACE_RE.sub(" ", text).strip()[:MAX_CHARS]


# ───────────────────────── Плеер сервера ─────────────────────────

class GuildPlayer:
    def __init__(self, vc: discord.VoiceClient, channel_id: int):
        self.vc = vc
        self.channel_id = channel_id
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
        self.task = asyncio.create_task(self.run())

    async def run(self):
        loop = asyncio.get_running_loop()
        while True:
            voice_name, text, speed = await self.queue.get()
            try:
                started = time.monotonic()
                pcm, rate = await loop.run_in_executor(
                    SYNTH_POOL, synthesize, voice_name, text, speed
                )
                log.info(
                    f"tts: синтез готов за {time.monotonic() - started:.2f} сек "
                    f"(голос={voice_name}, {len(pcm)} байт, {rate} Гц, память {rss_mb()} МБ)"
                )
                if not pcm:
                    log.warning("tts: синтез вернул пустое аудио - пропускаю")
                    continue
                if not self.vc.is_connected():
                    log.warning("tts: голосовое соединение не активно (is_connected=False) - пропускаю")
                    continue
                done = asyncio.Event()
                source = discord.FFmpegPCMAudio(
                    io.BytesIO(pcm),
                    pipe=True,
                    before_options=f"-f s16le -ar {rate} -ac 1",
                    options="-loglevel error",
                )
                def _after(error):
                    if error:
                        log.error(f"tts: ошибка воспроизведения: {error}")
                    loop.call_soon_threadsafe(done.set)

                problems = voice_problems(self.vc.guild, self.vc.channel)
                if problems:
                    log.warning("tts: бота, скорее всего, не будет слышно: " + "; ".join(problems))
                log.info("tts: начинаю воспроизведение")
                self.vc.play(source, after=_after)
                await done.wait()  # следующее сообщение - только после конца текущего
                log.info("tts: воспроизведение завершено")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Ошибка озвучки")


def voice_problems(guild: discord.Guild, channel) -> list:
    """Причины, по которым бота не будет слышно, даже если озвучка технически работает."""
    problems = []
    me = guild.me
    if me is None:
        return problems
    if not channel.permissions_for(me).speak:
        problems.append("у бота нет права «Говорить» (Speak) в этом канале (проверь и роль, и права самого канала)")
    state = me.voice
    if state is not None:
        if state.mute:
            problems.append("бот замьючен сервером (server mute)")
        if state.suppress:
            problems.append("бот подавлен (suppress): это Stage-канал, бота нужно пригласить выступать")
    return problems


players: dict[int, GuildPlayer] = {}


async def drop_player(guild: discord.Guild):
    player = players.pop(guild.id, None)
    if player:
        player.task.cancel()
    vc = guild.voice_client
    if vc:
        with contextlib.suppress(Exception):
            await vc.disconnect(force=True)


# ───────────────────────── Обработчики событий (вызываются из main.py) ─────────────────────────

async def handle_message(message: discord.Message) -> None:
    if not ENABLED:
        return
    if message.author.bot or message.guild is None:
        return
    if message.type not in (discord.MessageType.default, discord.MessageType.reply):
        return
    player = players.get(message.guild.id)
    if not player:
        return  # бот не в войсе на этом сервере - обычная ситуация, не логируем
    if message.channel.id != player.channel_id:
        log.debug(
            f"tts: сообщение не из чата голосового канала "
            f"(канал сообщения {message.channel.id}, нужен {player.channel_id})"
        )
        return

    g = store.guild(message.guild.id)
    if message.author.id in g["ignored"]:
        log.debug(f"tts: автор {message.author.id} в списке игнорируемых")
        return
    if ONLY_FROM_VOICE_MEMBERS:
        vs = getattr(message.author, "voice", None)
        if not (vs and vs.channel and vs.channel.id == player.channel_id):
            log.info("tts: пропускаю - автор не сидит в голосовом канале (ONLY_FROM_VOICE_MEMBERS=true)")
            return

    settings = store.user(message.author.id)
    text = clean_message(message, settings["emoji"])
    if not text:
        log.info("tts: пропускаю - после очистки текст пустой")
        return
    if g["said"]:
        name = emoji_lib.replace_emoji(message.author.display_name, "").strip()
        text = f"{name} говорит: {text}" if name else text

    voice_name = library.resolve(settings["voice"])
    if voice_name is None:
        log.warning(
            f"tts: нет ни одного доступного голоса - читать нечем. Папка: {VOICES_DIR.resolve()}, "
            "нужны пары файлов <имя>.onnx + <имя>.onnx.json"
        )
        return
    try:
        player.queue.put_nowait((voice_name, text, settings["speed"]))
    except asyncio.QueueFull:
        log.warning(f"tts: очередь озвучки переполнена (MAX_QUEUE={MAX_QUEUE}) - сообщение пропущено")
        return
    log.info(f"tts: в очереди ({len(text)} симв., голос={voice_name}, в очереди сейчас {player.queue.qsize()})")


async def handle_voice_state_update(
    bot: commands.Bot,
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    if not ENABLED:
        return
    guild = member.guild
    player = players.get(guild.id)
    if not player:
        return
    if member.id == bot.user.id:
        if after.channel is None:           # бота отключили
            await drop_player(guild)
            return
        player.channel_id = after.channel.id  # бота переместили
    vc = guild.voice_client
    if vc and vc.channel and not any(not m.bot for m in vc.channel.members):
        await drop_player(guild)            # в канале никого не осталось


# ───────────────────────── Вспомогательное для команд ─────────────────────────

def author_voice_channel(interaction: discord.Interaction):
    user = interaction.user
    if isinstance(user, discord.Member) and user.voice:
        return user.voice.channel
    return None


async def require_voice(interaction: discord.Interaction):
    """Если автор не в голосовом канале, команда молча игнорируется."""
    channel = author_voice_channel(interaction)
    if channel is None:
        await interaction.response.defer(ephemeral=True)
        with contextlib.suppress(discord.HTTPException):
            await interaction.delete_original_response()
    return channel


# ───────────────────────── Команды ─────────────────────────

@app_commands.command(name="join", description="Войти в голосовой канал и читать его чат")
@app_commands.guild_only()
async def join(interaction: discord.Interaction):
    channel = await require_voice(interaction)
    if channel is None:
        return
    if interaction.channel_id != channel.id:
        return await interaction.response.send_message(
            "Вызовите /join в чате вашего голосового канала.", ephemeral=True
        )

    guild = interaction.guild
    vc = guild.voice_client
    if vc and vc.is_connected() and vc.channel.id != channel.id:
        return await interaction.response.send_message(
            f"Бот уже работает в канале **{vc.channel.name}**.", ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)
    try:
        if not (vc and vc.is_connected()):
            vc = await channel.connect(self_deaf=True)
    except (discord.ClientException, asyncio.TimeoutError):
        log.exception("Не удалось подключиться")
        return await interaction.followup.send("Не удалось подключиться.", ephemeral=True)

    player = players.get(guild.id)
    if player:
        player.vc, player.channel_id = vc, channel.id
    else:
        players[guild.id] = GuildPlayer(vc, channel.id)
    log.info(f"tts: подключился к каналу {channel.name} ({channel.id}) на сервере {guild.id}")
    reply = f"Подключился к **{channel.name}**."
    problems = voice_problems(guild, channel)
    if problems:
        log.warning("tts: после /join бота не будет слышно: " + "; ".join(problems))
        reply += "\n⚠️ Но бота не будет слышно: " + "; ".join(problems) + "."
    await interaction.followup.send(reply, ephemeral=True)


@app_commands.command(name="leave", description="Выйти из голосового канала")
@app_commands.guild_only()
async def leave(interaction: discord.Interaction):
    channel = await require_voice(interaction)
    if channel is None:
        return
    vc = interaction.guild.voice_client
    if not vc or vc.channel.id != channel.id:
        return await interaction.response.send_message(
            "Бот сейчас не в вашем канале.", ephemeral=True
        )
    await drop_player(interaction.guild)
    await interaction.response.send_message("Отключился.", ephemeral=True)


@app_commands.command(name="said", description="Читать ли никнейм автора перед сообщением")
@app_commands.guild_only()
async def said(interaction: discord.Interaction):
    if await require_voice(interaction) is None:
        return
    g = store.guild(interaction.guild_id)
    g["said"] = not g["said"]
    store.save()
    state = "включено" if g["said"] else "выключено"
    await interaction.response.send_message(f"Чтение никнейма: **{state}**.", ephemeral=True)


@app_commands.command(name="ignore", description="Игнорировать сообщения пользователя")
@app_commands.guild_only()
@app_commands.describe(
    action="status - показать, add - добавить, remove - убрать",
    user="Пользователь (для add/remove, для status - необязательно)",
)
async def ignore(
    interaction: discord.Interaction,
    action: Literal["status", "add", "remove"],
    user: Optional[discord.Member] = None,
):
    if await require_voice(interaction) is None:
        return
    g = store.guild(interaction.guild_id)
    ignored: list = g["ignored"]

    if action == "status":
        if user:
            state = "игнорируется" if user.id in ignored else "не игнорируется"
            msg = f"{user.mention} {state}."
        elif ignored:
            msg = "Игнорируются: " + ", ".join(f"<@{i}>" for i in ignored)
        else:
            msg = "Список игнорируемых пуст."
        return await interaction.response.send_message(msg, ephemeral=True)

    if user is None:
        return await interaction.response.send_message("Укажите пользователя.", ephemeral=True)

    if action == "add":
        if user.id not in ignored:
            ignored.append(user.id)
        msg = f"{user.mention} теперь игнорируется."
    else:
        if user.id in ignored:
            ignored.remove(user.id)
        msg = f"{user.mention} больше не игнорируется."
    store.save()
    await interaction.response.send_message(msg, ephemeral=True)


@app_commands.command(name="voice", description="Выбрать свой голос")
@app_commands.describe(name="Название голоса")
async def voice_cmd(interaction: discord.Interaction, name: str):
    if await require_voice(interaction) is None:
        return
    if name not in library.voices:
        available = ", ".join(library.names()) or "—"
        return await interaction.response.send_message(
            f"Голос не найден. Доступные: {available}", ephemeral=True
        )
    store.set_user(interaction.user.id, voice=name)
    await interaction.response.send_message(f"Ваш голос: **{name}**.", ephemeral=True)


@voice_cmd.autocomplete("name")
async def voice_autocomplete(interaction: discord.Interaction, current: str):
    current = current.lower()
    return [
        app_commands.Choice(name=n, value=n)
        for n in library.names()
        if current in n.lower()
    ][:25]


@app_commands.command(name="emoji", description="Читать эмодзи или игнорировать их")
@app_commands.describe(mode="read - читать, ignore - пропускать")
async def emoji_cmd(interaction: discord.Interaction, mode: Literal["read", "ignore"]):
    if await require_voice(interaction) is None:
        return
    store.set_user(interaction.user.id, emoji=(mode == "read"))
    msg = "Эмодзи будут читаться." if mode == "read" else "Эмодзи будут пропускаться."
    await interaction.response.send_message(msg, ephemeral=True)


@app_commands.command(name="speed", description="Скорость чтения (высота голоса не меняется)")
@app_commands.describe(multiplier="От 0.5 до 2.0, обычная скорость - 1.0")
async def speed_cmd(
    interaction: discord.Interaction,
    multiplier: app_commands.Range[float, 0.5, 2.0],
):
    if await require_voice(interaction) is None:
        return
    store.set_user(interaction.user.id, speed=round(multiplier, 2))
    await interaction.response.send_message(f"Скорость: **×{multiplier:.2f}**.", ephemeral=True)


_COMMANDS = (join, leave, said, ignore, voice_cmd, emoji_cmd, speed_cmd)
_watch_task: Optional[asyncio.Task] = None


# ───────────────────────── Подключение к боту ─────────────────────────

def register(bot: commands.Bot) -> None:
    """Добавляет слэш-команды TTS в bot.tree. Вызывать один раз при создании бота."""
    if not ENABLED:
        log.warning(
            f"tts: не удалось импортировать зависимости ({_import_error}) - озвучка отключена. "
            "Нужно: pip install piper-tts emoji"
        )
        return
    if importlib.util.find_spec("nacl") is None:
        log.warning("tts: не установлен PyNaCl - голосовые каналы не заработают. Нужно: pip install \"discord.py[voice]\"")
    if shutil.which("ffmpeg") is None:
        log.warning("tts: ffmpeg не найден в PATH - озвучка не заработает. Нужно: apt install ffmpeg")
    for cmd in _COMMANDS:
        bot.tree.add_command(cmd)
    log.info("tts: слэш-команды зарегистрированы")


def start() -> None:
    """
    Запускает фоновое слежение за папкой с голосами. Вызывать из on_ready.
    Безопасно вызывать повторно (при переподключениях) - задача не задублируется.
    """
    global _watch_task
    if not ENABLED:
        return
    if _watch_task is not None and not _watch_task.done():
        return
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    library.scan()
    log.info(
        f"tts: папка голосов {VOICES_DIR.resolve()}, найдено голосов: {len(library.voices)} "
        f"({', '.join(library.names()) or 'пусто'})"
    )
    _watch_task = asyncio.create_task(library.watch())