import os
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import yt_dlp

# ============================================================
# 讀取 TOKEN
# ============================================================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# ============================================================
# Discord Bot 設定
# ============================================================
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
)

tree = bot.tree  # for slash commands

# ============================================================
# 音樂設定
# ============================================================
ffmpeg_path = "/usr/bin/ffmpeg"   # Railway 的 ffmpeg 路徑

YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
}

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
    "executable": ffmpeg_path,
}

music_queue = {}  # guild_id → list of songs
now_playing = {}  # guild_id → current song info


# ============================================================
# 播放函式
# ============================================================
async def play_next(guild_id, vc):
    if guild_id not in music_queue or len(music_queue[guild_id]) == 0:
        now_playing[guild_id] = None
        return

    url, title = music_queue[guild_id].pop(0)
    now_playing[guild_id] = title

    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(url, download=False)
        audio_url = info["url"]

    source = discord.FFmpegPCMAudio(audio_url, **FFMPEG_OPTS)

    def after_play(err):
        fut = asyncio.run_coroutine_threadsafe(play_next(guild_id, vc), bot.loop)
        try:
            fut.result()
        except:
            pass

    vc.play(source, after=after_play)


# ============================================================
# Slash 指令：play
# ============================================================
@tree.command(name="play", description="播放 YouTube 音樂")
async def play(interaction: discord.Interaction, url: str):
    await interaction.response.defer()

    guild_id = interaction.guild_id

    # 使用者不在語音房
    if not interaction.user.voice:
        return await interaction.followup.send("❌ 你需要先加入語音頻道！")

    voice_channel = interaction.user.voice.channel

    # 連線到語音房
    vc = interaction.guild.voice_client
    if vc is None:
        vc = await voice_channel.connect()

    # 用 yt-dlp 抓資訊
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(url, download=False)
        title = info["title"]

    # 放入 queue
    if guild_id not in music_queue:
        music_queue[guild_id] = []

    music_queue[guild_id].append((url, title))

    await interaction.followup.send(f"🎶 **已加入佇列：** `{title}`")

    # 如果沒在播放 → 播放
    if not vc.is_playing():
        await play_next(guild_id, vc)


# ============================================================
# Slash 指令：skip
# ============================================================
@tree.command(name="skip", description="跳過目前播放的歌曲")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        return await interaction.response.send_message("❌ 沒有歌曲正在播放")

    vc.stop()
    await interaction.response.send_message("⏭ 已跳過這首曲目！")


# ============================================================
# Slash 指令：queue
# ============================================================
@tree.command(name="queue", description="查看播放佇列")
async def queue(interaction: discord.Interaction):
    guild_id = interaction.guild_id

    if guild_id not in music_queue or len(music_queue[guild_id]) == 0:
        return await interaction.response.send_message("📭 佇列目前是空的！")

    msg = "🎵 **播放佇列：**\n"
    for i, (_, title) in enumerate(music_queue[guild_id]):
        msg += f"{i+1}. {title}\n"

    await interaction.response.send_message(msg)


# ============================================================
# Slash 指令：nowplaying
# ============================================================
@tree.command(name="nowplaying", description="查看目前播放的歌曲")
async def nowplaying(interaction: discord.Interaction):
    guild_id = interaction.guild_id

    if guild_id not in now_playing or not now_playing[guild_id]:
        return await interaction.response.send_message("🎧 目前沒有正在播放的歌曲")

    await interaction.response.send_message(f"🎶 **正在播放：** `{now_playing[guild_id]}`")


# ============================================================
# Bot 啟動
# ============================================================
@bot.event
async def on_ready():
    await tree.sync()
    print(f"🤖 已登入：{bot.user}")


bot.run(TOKEN)
