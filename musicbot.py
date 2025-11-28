import os
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import yt_dlp

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = False  # Slash 指令不需要 message content
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ===== YouTube / FFmpeg 設定 =====
YDL_OPTIONS = {
    "format": "bestaudio/best",
    "quiet": True,
    "default_search": "ytsearch1",
    "noplaylist": False,
}
FFMPEG_OPTIONS = {
    "options": "-vn"
}

# ===== 播放佇列 + 現在播放狀態 =====
queues = {}
now_playing = {}

def get_queue(gid):
    if gid not in queues:
        queues[gid] = []
    return queues[gid]


async def get_source(query: str):
    loop = asyncio.get_event_loop()
    with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
        data = await loop.run_in_executor(None, lambda: ydl.extract_info(query, download=False))
    if "entries" in data:
        data = data["entries"][0]
    return data["url"], data["title"]


async def play_next(interaction: discord.Interaction):
    gid = interaction.guild.id
    queue = get_queue(gid)

    if len(queue) == 0:
        now_playing[gid] = None
        return

    url, title = queue.pop(0)
    now_playing[gid] = title

    vc = interaction.guild.voice_client
    source = discord.FFmpegPCMAudio(url, **FFMPEG_OPTIONS)

    vc.play(
        source,
        after=lambda e: asyncio.run_coroutine_threadsafe(play_next(interaction), bot.loop)
    )

    await interaction.followup.send(f"🎵 現在播放： **{title}**")


# ===== Bot 啟動 =====
@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"✨ 已同步 Slash 指令：{len(synced)} 個")
    except Exception as e:
        print(e)

    print(f"🎧 音樂 Slash Bot 已啟動：{bot.user}")


# ===============================
#        Slash 指令開始
# ===============================

# ===== /join =====
@bot.tree.command(name="join", description="讓機器人加入你的語音頻道")
async def join(interaction: discord.Interaction):
    if interaction.user.voice is None:
        await interaction.response.send_message("你必須先加入語音頻道！", ephemeral=True)
        return

    channel = interaction.user.voice.channel
    await channel.connect()
    await interaction.response.send_message(f"已加入語音頻道：**{channel}**")


# ===== /leave =====
@bot.tree.command(name="leave", description="讓機器人離開語音頻道")
async def leave(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc:
        await vc.disconnect()
        await interaction.response.send_message("👋 已離開語音頻道")
    else:
        await interaction.response.send_message("我不在語音頻道中。", ephemeral=True)


# ===== /play =====
@bot.tree.command(name="play", description="播放音樂（支援關鍵字或 YouTube 連結）")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()

    if interaction.user.voice is None:
        await interaction.followup.send("你必須先加入語音頻道！")
        return

    vc = interaction.guild.voice_client
    if vc is None:
        vc = await interaction.user.voice.channel.connect()

    await interaction.followup.send(f"🔍 搜尋： `{query}` ...")

    url, title = await get_source(query)
    queue = get_queue(interaction.guild.id)

    if not vc.is_playing():
        queue.insert(0, (url, title))
        await play_next(interaction)
    else:
        queue.append((url, title))
        await interaction.followup.send(f"➕ 已加入佇列：**{title}**")


# ===== /queue =====
@bot.tree.command(name="queue", description="查看目前播放佇列")
async def queue_list(interaction: discord.Interaction):
    queue = get_queue(interaction.guild.id)

    if len(queue) == 0:
        await interaction.response.send_message("📭 播放佇列是空的。")
        return

    text = "\n".join([f"{i+1}. {title}" for i, (_, title) in enumerate(queue)])
    await interaction.response.send_message("📜 **目前佇列：**\n" + text)


# ===== /skip =====
@bot.tree.command(name="skip", description="跳過目前的歌曲")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client

    if not vc or not vc.is_playing():
        await interaction.response.send_message("沒有正在播放的音樂。", ephemeral=True)
        return

    vc.stop()
    await interaction.response.send_message("⏭ 已跳過歌曲")
    await play_next(interaction)


# ===== /nowplaying =====
@bot.tree.command(name="nowplaying", description="顯示目前播放的歌曲")
async def nowplaying(interaction: discord.Interaction):
    current = now_playing.get(interaction.guild.id)

    if current:
        await interaction.response.send_message(f"🎶 正在播放：**{current}**")
    else:
        await interaction.response.send_message("現在沒有正在播放的歌曲。")


if __name__ == "__main__":
    bot.run(TOKEN)
