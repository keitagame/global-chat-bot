"""
Global Chat Bot - Discord <-> Discord & IRC Bridge
複数のDiscordサーバーとIRCサーバーを繋ぐグローバルチャットBot
"""

import asyncio
import logging
import re
import ssl
from datetime import datetime
from typing import Optional

import discord
from discord.ext import commands
"""
IRC Client - asyncioベースの非同期IRCクライアント
"""

import asyncio
import logging
import ssl
from typing import Callable, Awaitable, Optional

logger = logging.getLogger("IRCClient")

# IRC メッセージの最大バイト数
IRC_MAX_BYTES = 512


class IRCClient:
    """
    非同期IRCクライアント。
    接続・認証・チャンネル参加・メッセージ送受信・自動再接続を担当する。
    """

    def __init__(
        self,
        host: str,
        port: int,
        nickname: str,
        channels: list[str],
        use_ssl: bool = False,
        password: Optional[str] = None,
        realname: str = "GlobalChatBot",
        on_message_callback: Optional[Callable[[str, str, str], Awaitable[None]]] = None,
        reconnect_delay: float = 10.0,
    ):
        self.host = host
        self.port = port
        self.nickname = nickname
        self.channels = channels
        self.use_ssl = use_ssl
        self.password = password
        self.realname = realname
        self.on_message_callback = on_message_callback
        self.reconnect_delay = reconnect_delay

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._connected = False
        self._running = True

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self):
        """IRCサーバーに接続し、切断時は自動再接続する"""
        while self._running:
            try:
                await self._do_connect()
                await self._read_loop()
            except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as e:
                logger.warning(f"IRC connection failed: {e}. Retrying in {self.reconnect_delay}s...")
            except Exception as e:
                logger.error(f"Unexpected IRC error: {e}", exc_info=True)
            finally:
                self._connected = False

            if self._running:
                await asyncio.sleep(self.reconnect_delay)

    async def _do_connect(self):
        """実際の接続処理"""
        ssl_ctx = ssl.create_default_context() if self.use_ssl else None
        logger.info(f"Connecting to IRC: {self.host}:{self.port} (SSL={self.use_ssl})")

        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port, ssl=ssl_ctx),
            timeout=30,
        )
        logger.info("IRC TCP connection established")

        # 認証
        if self.password:
            await self._send_raw(f"PASS {self.password}")
        await self._send_raw(f"NICK {self.nickname}")
        await self._send_raw(f"USER {self.nickname} 0 * :{self.realname}")

    async def _read_loop(self):
        """サーバーからの受信ループ"""
        while self._running:
            try:
                line = await asyncio.wait_for(self._reader.readline(), timeout=300)
            except asyncio.TimeoutError:
                # PINGを送って接続確認
                await self._send_raw("PING :keepalive")
                continue

            if not line:
                logger.warning("IRC connection closed by server")
                break

            decoded = line.decode("utf-8", errors="replace").rstrip("\r\n")
            logger.debug(f"IRC ← {decoded}")
            await self._handle_line(decoded)

    async def _handle_line(self, line: str):
        """1行のIRCメッセージを解析して処理する"""
        # PING応答
        if line.startswith("PING"):
            token = line.split(":", 1)[1] if ":" in line else ""
            await self._send_raw(f"PONG :{token}")
            return

        parts = line.split(" ", 3)
        if len(parts) < 2:
            return

        prefix = ""
        if parts[0].startswith(":"):
            prefix = parts[0][1:]
            parts = parts[1:]

        command = parts[0]

        # 001 = 接続完了
        if command == "001":
            logger.info("IRC registered successfully")
            self._connected = True
            for channel in self.channels:
                await self._join_channel(channel)

        # PRIVMSG = チャンネルまたはDMのメッセージ
        elif command == "PRIVMSG" and len(parts) >= 3:
            target = parts[1]
            message = parts[2].lstrip(":") if len(parts) > 2 else ""
            nickname = prefix.split("!")[0] if "!" in prefix else prefix

            # チャンネルへのメッセージのみ処理
            if target.startswith("#") and self.on_message_callback:
                try:
                    await self.on_message_callback(target, nickname, message)
                except Exception as e:
                    logger.error(f"Error in on_message_callback: {e}", exc_info=True)

        # KICK / PART で追放された場合は再参加
        elif command in ("KICK", "PART"):
            channel = parts[1] if len(parts) > 1 else ""
            if channel in self.channels:
                await asyncio.sleep(3)
                await self._join_channel(channel)

        # 433 = ニックネーム使用中
        elif command == "433":
            self.nickname += "_"
            await self._send_raw(f"NICK {self.nickname}")

        # ERROR
        elif command == "ERROR":
            logger.error(f"IRC ERROR: {line}")

    async def _join_channel(self, channel: str):
        if not channel.startswith("#"):
            channel = f"#{channel}"
        logger.info(f"Joining IRC channel: {channel}")
        await self._send_raw(f"JOIN {channel}")

    async def _send_raw(self, message: str):
        """生のIRCコマンドを送信する"""
        if not self._writer:
            return
        data = f"{message}\r\n".encode("utf-8")
        # IRC の最大バイト数を超えないようにトリミング
        if len(data) > IRC_MAX_BYTES:
            data = data[: IRC_MAX_BYTES - 2] + b"\r\n"
        try:
            self._writer.write(data)
            await self._writer.drain()
            logger.debug(f"IRC → {message}")
        except (ConnectionResetError, BrokenPipeError) as e:
            logger.warning(f"IRC send error: {e}")
            self._connected = False

    async def send_message(self, channel: str, message: str):
        """チャンネルにメッセージを送信する（長いメッセージは分割）"""
        if not self._connected:
            logger.warning("IRC not connected, cannot send message")
            return

        if not channel.startswith("#"):
            channel = f"#{channel}"

        # 長いメッセージを分割して送信
        header = f"PRIVMSG {channel} :"
        max_msg_bytes = IRC_MAX_BYTES - len(header.encode("utf-8")) - 2

        encoded = message.encode("utf-8")
        chunks = [encoded[i : i + max_msg_bytes] for i in range(0, len(encoded), max_msg_bytes)]

        for chunk in chunks:
            decoded_chunk = chunk.decode("utf-8", errors="replace")
            await self._send_raw(f"{header}{decoded_chunk}")
            await asyncio.sleep(0.5)  # レート制限対策

    async def disconnect(self):
        """IRCから切断する"""
        self._running = False
        self._connected = False
        if self._writer:
            try:
                await self._send_raw("QUIT :GlobalChatBot shutting down")
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        logger.info("IRC disconnected")
"""
設定管理 - 環境変数または config.json から読み込む
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class BotConfig:
    # Discord
    discord_token: str
    prefix: str = "!"
    discord_relay_channel_ids: list[int] = field(default_factory=list)

    # IRC
    irc_enabled: bool = False
    irc_host: str = "irc.libera.chat"
    irc_port: int = 6697
    irc_nickname: str = "GlobalChatBot"
    irc_channels: list[str] = field(default_factory=lambda: ["#global-chat"])
    irc_use_ssl: bool = True
    irc_password: Optional[str] = None

    @classmethod
    def from_env(cls) -> "BotConfig":
        """環境変数から設定を読み込む。config.json があれば優先的にマージする"""
        # まずデフォルト値で作成
        config = cls(discord_token="")

        # config.json が存在すれば読み込む
        config_path = Path("config.json")
        if config_path.exists():
            with open(config_path, encoding="utf-8") as f:
                data = json.load(f)
            _apply_json(config, data)

        # 環境変数で上書き（優先度高）
        token = ""
        if token:
            config.discord_token = token

        prefix = "!"
        if prefix:
            config.prefix = prefix

        channel_ids_raw = os.getenv("DISCORD_RELAY_CHANNEL_IDS", "")
        if channel_ids_raw:
            config.discord_relay_channel_ids = [
                int(x.strip()) for x in channel_ids_raw.split(",") if x.strip().isdigit()
            ]

        irc_enabled = os.getenv("IRC_ENABLED", "")
        if irc_enabled:
            config.irc_enabled = irc_enabled.lower() in ("true", "1", "yes")

        irc_host = "irc.keitagames.com"
        if irc_host:
            config.irc_host = irc_host

        irc_port = os.getenv("IRC_PORT", "")
        if irc_port.isdigit():
            config.irc_port = int(irc_port)

        irc_nick = os.getenv("IRC_NICKNAME", "")
        if irc_nick:
            config.irc_nickname = irc_nick

        irc_channels = os.getenv("IRC_CHANNELS", "")
        if irc_channels:
            config.irc_channels = [c.strip() for c in irc_channels.split(",") if c.strip()]

        irc_ssl = os.getenv("IRC_USE_SSL", "")
        if irc_ssl:
            config.irc_use_ssl = irc_ssl.lower() in ("true", "1", "yes")

        irc_password = os.getenv("IRC_PASSWORD", "")
        if irc_password:
            config.irc_password = irc_password

        # バリデーション
        if not config.discord_token:
            raise ValueError(
                "DISCORD_TOKEN が設定されていません。\n"
                "環境変数 DISCORD_TOKEN を設定するか、config.json に discord_token を記載してください。"
            )

        return config


def _apply_json(config: BotConfig, data: dict):
    """JSONデータをconfigに適用する"""
    if "discord_token" in data:
        config.discord_token = data["discord_token"]
    if "prefix" in data:
        config.prefix = data["prefix"]
    if "discord_relay_channel_ids" in data:
        config.discord_relay_channel_ids = [int(x) for x in data["discord_relay_channel_ids"]]

    irc = data.get("irc", {})
    if "enabled" in irc:
        config.irc_enabled = bool(irc["enabled"])
    if "host" in irc:
        config.irc_host = irc["host"]
    if "port" in irc:
        config.irc_port = int(irc["port"])
    if "nickname" in irc:
        config.irc_nickname = irc["nickname"]
    if "channels" in irc:
        config.irc_channels = list(irc["channels"])
    if "use_ssl" in irc:
        config.irc_use_ssl = bool(irc["use_ssl"])
    if "password" in irc:
        config.irc_password = irc["password"] or None
##from config import BotConfig
##from irc_client import IRCClient

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("global_chat.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("GlobalChatBot")


class GlobalChatBot(commands.Bot):
    def __init__(self, config: BotConfig):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True

        super().__init__(
            command_prefix=config.prefix,
            intents=intents,
            description="Global Chat Bot - IRC & Discord Bridge",
        )

        self.config = config
        self.irc_client: Optional[IRCClient] = None
        self.relay_channels: dict[int, str] = {}  # {discord_channel_id: "label"}
        self._load_relay_channels()

    def _load_relay_channels(self):
        """設定ファイルからリレーチャンネルを読み込む"""
        for ch_id in self.config.discord_relay_channel_ids:
            self.relay_channels[ch_id] = f"channel_{ch_id}"

    async def setup_hook(self):
        """Bot起動時の初期設定"""
        await self.add_cog(GlobalChatCog(self))
        logger.info("Cogs loaded successfully")

        # IRCが有効な場合のみ接続
        if self.config.irc_enabled:
            self.irc_client = IRCClient(
                host=self.config.irc_host,
                port=self.config.irc_port,
                nickname=self.config.irc_nickname,
                channels=self.config.irc_channels,
                use_ssl=self.config.irc_use_ssl,
                on_message_callback=self.on_irc_message,
            )
            self.loop.create_task(self.irc_client.connect())
            logger.info(f"IRC client initialized: {self.config.irc_host}")

    async def on_ready(self):
        logger.info(f"Bot ready: {self.user} (ID: {self.user.id})")
        logger.info(f"Connected guilds: {[g.name for g in self.guilds]}")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Global Chat 🌐",
            )
        )

    async def on_irc_message(self, channel: str, nickname: str, message: str):
        """IRCからのメッセージをDiscordに転送"""
        if nickname == self.config.irc_nickname:
            return  # 自分のメッセージはスキップ

        formatted = f"**[IRC/{channel}] <{nickname}>** {message}"
        logger.info(f"IRC→Discord: [{channel}] <{nickname}> {message}")

        for ch_id in self.relay_channels:
            discord_channel = self.get_channel(ch_id)
            if discord_channel:
                try:
                    await discord_channel.send(formatted)
                except discord.HTTPException as e:
                    logger.error(f"Failed to send to Discord channel {ch_id}: {e}")

    async def relay_to_irc(self, channel: str, author: str, message: str):
        """DiscordメッセージをIRCに転送"""
        if self.irc_client and self.irc_client.is_connected:
            irc_msg = f"<{author}> {message}"
            await self.irc_client.send_message(channel, irc_msg)

    async def relay_to_discord(
        self,
        source_channel_id: int,
        source_guild: str,
        author: str,
        message: str,
        avatar_url: Optional[str] = None,
    ):
        """あるDiscordチャンネルのメッセージを他の全リレーチャンネルに転送"""
        embed = discord.Embed(
            description=message,
            color=discord.Color.blurple(),
            timestamp=datetime.utcnow(),
        )
        embed.set_author(
            name=f"{author} @ {source_guild}",
            icon_url=avatar_url,
        )
        embed.set_footer(text="🌐 Global Chat")

        for ch_id in self.relay_channels:
            if ch_id == source_channel_id:
                continue  # 送信元はスキップ
            discord_channel = self.get_channel(ch_id)
            if discord_channel:
                try:
                    await discord_channel.send(embed=embed)
                except discord.HTTPException as e:
                    logger.error(f"Failed to relay to Discord channel {ch_id}: {e}")


class GlobalChatCog(commands.Cog):
    def __init__(self, bot: GlobalChatBot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Discordメッセージを受信してリレー"""
        if message.author.bot:
            return
        if message.channel.id not in self.bot.relay_channels:
            return

        content = message.clean_content
        if not content and not message.attachments:
            return

        # 添付ファイルがある場合URLも含める
        if message.attachments:
            urls = " ".join(a.url for a in message.attachments)
            content = f"{content} {urls}".strip()

        guild_name = message.guild.name if message.guild else "DM"
        author = str(message.author.display_name)
        avatar_url = str(message.author.display_avatar.url)

        logger.info(f"Discord→Relay: [{guild_name}#{message.channel.name}] <{author}> {content}")

        # 他のDiscordチャンネルへ転送
        await self.bot.relay_to_discord(
            source_channel_id=message.channel.id,
            source_guild=guild_name,
            author=author,
            message=content,
            avatar_url=avatar_url,
        )

        # IRCへ転送
        if self.bot.config.irc_enabled:
            for irc_channel in self.bot.config.irc_channels:
                await self.bot.relay_to_irc(irc_channel, author, content)

    # ---- スラッシュコマンド ----

    @discord.app_commands.command(name="globalchat_add", description="このチャンネルをグローバルチャットに追加します")
    @discord.app_commands.checks.has_permissions(manage_channels=True)
    async def add_channel(self, interaction: discord.Interaction):
        ch_id = interaction.channel_id
        if ch_id in self.bot.relay_channels:
            await interaction.response.send_message("⚠️ すでにグローバルチャットに登録されています。", ephemeral=True)
            return
        self.bot.relay_channels[ch_id] = f"channel_{ch_id}"
        logger.info(f"Added relay channel: {ch_id} ({interaction.channel.name})")
        await interaction.response.send_message(
            f"✅ **#{interaction.channel.name}** をグローバルチャットに追加しました！", ephemeral=False
        )

    @discord.app_commands.command(name="globalchat_remove", description="このチャンネルをグローバルチャットから削除します")
    @discord.app_commands.checks.has_permissions(manage_channels=True)
    async def remove_channel(self, interaction: discord.Interaction):
        ch_id = interaction.channel_id
        if ch_id not in self.bot.relay_channels:
            await interaction.response.send_message("⚠️ このチャンネルは登録されていません。", ephemeral=True)
            return
        del self.bot.relay_channels[ch_id]
        logger.info(f"Removed relay channel: {ch_id}")
        await interaction.response.send_message(
            f"✅ **#{interaction.channel.name}** をグローバルチャットから削除しました。", ephemeral=False
        )

    @discord.app_commands.command(name="globalchat_status", description="グローバルチャットの状態を表示します")
    async def status(self, interaction: discord.Interaction):
        irc_status = "🟢 接続中" if (self.bot.irc_client and self.bot.irc_client.is_connected) else "🔴 未接続/無効"
        channel_count = len(self.bot.relay_channels)

        embed = discord.Embed(title="🌐 Global Chat Status", color=discord.Color.green())
        embed.add_field(name="IRC", value=irc_status, inline=True)
        embed.add_field(name="リレーチャンネル数", value=str(channel_count), inline=True)
        embed.add_field(
            name="Discordサーバー数",
            value=str(len(self.bot.guilds)),
            inline=True,
        )
        if self.bot.config.irc_enabled:
            embed.add_field(
                name="IRCサーバー",
                value=f"{self.bot.config.irc_host}:{self.bot.config.irc_port}",
                inline=False,
            )
            embed.add_field(
                name="IRCチャンネル",
                value=", ".join(self.bot.config.irc_channels),
                inline=False,
            )
        await interaction.response.send_message(embed=embed)

    @discord.app_commands.command(name="globalchat_irc_send", description="IRCチャンネルに直接メッセージを送信します")
    @discord.app_commands.describe(channel="IRCチャンネル名 (例: #general)", message="送信するメッセージ")
    async def irc_send(self, interaction: discord.Interaction, channel: str, message: str):
        if not self.bot.config.irc_enabled or not self.bot.irc_client:
            await interaction.response.send_message("⚠️ IRCは無効です。", ephemeral=True)
            return
        if not self.bot.irc_client.is_connected:
            await interaction.response.send_message("⚠️ IRCに接続されていません。", ephemeral=True)
            return
        author = interaction.user.display_name
        await self.bot.irc_client.send_message(channel, f"<{author}> {message}")
        await interaction.response.send_message(f"✅ IRCの `{channel}` に送信しました: {message}", ephemeral=True)


async def main():
    config = BotConfig.from_env()
    bot = GlobalChatBot(config)

    # スラッシュコマンドを同期
    async with bot:
        await bot.load_extension.__func__  # noqa - setup_hook内でcog追加済み
        try:
            await bot.start(config.discord_token)
        except KeyboardInterrupt:
            logger.info("Shutting down...")
        finally:
            if bot.irc_client:
                await bot.irc_client.disconnect()
            await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
