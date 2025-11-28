import os
import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Optional
import threading
from flask import Flask

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import yt_dlp

# ============================================================
# Flask Keep-Alive（讓 Railway 不會自動停止）
# ============================================================
app = Flask(__name__)

@app.route("/")
def alive():
    return "Bot is alive!"

def run_web():
    app.run(host="0.0.0.0", port=3000)

threading.Thread(target=run_web).start()

# ============================================================
# 讀取環境變數
# ============================================================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "/usr/bin/ffmpeg")

# ============================================================
# Bot & Intents 設定
# ============================================================
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ============================================================
# yt-dlp & ffmpeg 設定
# ============================================================
YDL_OPTS_BASE = {
    "format": "bestaudio/best",
    "quiet": True,
    "nocheckcertificate": True,
}

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
    "executable": FFMPEG_PATH,
}

# ============================================================
# 狀態儲存
# ============================================================
Track = Dict[str, Optional[str]]

queues: Dict[int, List[Track]] = {}
now_playing: Dict[int, Optional[Track]] = {}
loop_flags: Dict[int, bool] = {}
start_times: Dict[int, Optional[datetime]] = {}
volume_settings: Dict[int, float] = {}
last_active: Dict[int, datetime] = {}
history: Dict[int, List[Track]] = {}
play_counts: Dict[int, Dict[str, int]] = {}

# ============================================================
# Owner / 管理伺服器 限制設定
# ============================================================
BOT_OWNER_ID = 477325882881605635       
ADMIN_SERVER_ID = 1191733505839865927   

# ============================================================
def touch_active(guild_id: int):
    last_active[guild_id] = datetime.now(timezone.utc)

def maybe_convert_spotify_to_search(query: str) -> str:
    if "open.spotify.com/track" not in query:
        return query
    return query

def get_track_info(query: str) -> Track:
    q = maybe_convert_spotify_to_search(query)

    if not (q.startswith("http://") or q.startswith("https://")):
        q = f"ytsearch1:{q}"

    with yt_dlp.YoutubeDL(YDL_OPTS_BASE) as ydl:
        info = ydl.extract_info(q, download=False)

    if "entries" in info:
        info = info["entries"][0]

    url = info.get("webpage_url") or info.get("url") or ""
    if url and not url.startswith("http"):
        url = f"https://www.youtube.com/watch?v={url}"

    return {
        "webpage_url": url,
        "title": info.get("title", "未知標題"),
        "duration": str(info.get("duration") or 0),
        "thumbnail": info.get("thumbnail"),
        "uploader": info.get("uploader"),
    }

def get_audio_url(url: str) -> str:
    with yt_dlp.YoutubeDL(YDL_OPTS_BASE) as ydl:
        info = ydl.extract_info(url, download=False)
    return info["url"]

# ============================================================
async def play_next(guild_id: int, vc: discord.VoiceClient):
    if guild_id not in queues:
        queues[guild_id] = []
    if guild_id not in loop_flags:
        loop_flags[guild_id] = False

    track = None

    if loop_flags[guild_id] and now_playing.get(guild_id):
        track = now_playing[guild_id]
    else:
        if not queues[guild_id]:
            now_playing[guild_id] = None
            start_times[guild_id] = None
            return

        track = queues[guild_id].pop(0)
        now_playing[guild_id] = track

        history.setdefault(guild_id, []).append(track)
        history[guild_id] = history[guild_id][-50:]

        title = track.get("title") or "未知標題"
        play_counts.setdefault(guild_id, {})
        play_counts[guild_id][title] = play_counts[guild_id].get(title, 0) + 1

    audio_url = get_audio_url(track["webpage_url"])
    source = discord.FFmpegPCMAudio(audio_url, **FFMPEG_OPTS)

    volume = volume_settings.get(guild_id, 1.0)
    source = discord.PCMVolumeTransformer(source, volume)

    start_times[guild_id] = datetime.now(timezone.utc)
    touch_active(guild_id)

    def after(err):
        fut = asyncio.run_coroutine_threadsafe(play_next(guild_id, vc), bot.loop)
        try:
            fut.result()
        except Exception as e:
            print("播放錯誤:", e)

    vc.play(source, after=after)

# ============================================================
async def auto_disconnect_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(timezone.utc)
        for guild in bot.guilds:
            vc: discord.VoiceClient = guild.voice_client
            if not vc:
                continue

            guild_id = guild.id
            last = last_active.get(guild_id)
            if not last:
                continue

            idle = (now - last).total_seconds()
            members = [m for m in vc.channel.members if not m.bot]

            if (not members or (not vc.is_playing() and not queues.get(guild_id))) and idle > 300:
                await vc.disconnect()
                queues[guild_id] = []
        await asyncio.sleep(30)

# ============================================================
async def ensure_voice(interaction: discord.Interaction):
    if not interaction.user.voice:
        await interaction.response.send_message("❌ 你需要先加入語音頻道！", ephemeral=True)
        return None

    vc = interaction.guild.voice_client
    if not vc:
        vc = await interaction.user.voice.channel.connect()
    elif vc.channel != interaction.user.voice.channel:
        await vc.move_to(interaction.user.voice.channel)

    touch_active(interaction.guild_id)
    return vc

# ============================================================
def fmt_time(s: int):
    return f"{s//60:02d}:{s%60:02d}"

def progress_bar(elapsed, duration, length=20):
    if duration <= 0:
        return "🔘" + "▬" * (length - 1)
    pos = int(length * (elapsed / duration))
    return "".join("🔘" if i == pos else "▬" for i in range(length))

# ============================================================
# 🔊 音樂播放指令（原樣全部保留）
# ============================================================
@tree.command(name="play", description="播放音樂")
async def play_cmd(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    vc = await ensure_voice(interaction)
    if vc is None:
        return

    track = get_track_info(query)
    queues.setdefault(interaction.guild_id, []).append(track)

    embed = discord.Embed(
        title="🎶 已加入佇列",
        description=f"**{track['title']}**",
        color=discord.Color.blurple()
    )
    embed.add_field(name="來源", value=track["webpage_url"], inline=False)
    if track.get("thumbnail"):
        embed.set_thumbnail(url=track["thumbnail"])

    await interaction.followup.send(embed=embed)

    if not vc.is_playing():
        await play_next(interaction.guild_id, vc)

# ============================================================
# 🔐 管理權限檢查
# ============================================================
def is_admin_allowed(interaction: discord.Interaction):
    return (
        interaction.user.id == BOT_OWNER_ID
        and interaction.guild_id == ADMIN_SERVER_ID
    )

# ============================================================
@tree.command(name="servers", description="（管理）查看 Bot 加入的伺服器")
async def servers_cmd(interaction: discord.Interaction):
    if not is_admin_allowed(interaction):
        return await interaction.response.send_message("❌ 你沒有權限。", ephemeral=True)

    lines = [f"**{g.name}**（ID: `{g.id}`）" for g in bot.guilds]
    embed = discord.Embed(
        title="📋 我加入的伺服器列表",
        description="\n".join(lines),
        color=discord.Color.green(),
    )
    await interaction.response.send_message(embed=embed)

@tree.command(name="servercount", description="（管理）顯示加入伺服器數量")
async def servercount_cmd(interaction: discord.Interaction):
    if not is_admin_allowed(interaction):
        return await interaction.response.send_message("❌ 你沒有權限。", ephemeral=True)

    await interaction.response.send_message(f"📊 Bot 加入 **{len(bot.guilds)}** 個伺服器")

@tree.command(name="stats", description="（管理）查看各伺服器播放狀態")
async def stats_cmd(interaction: discord.Interaction):
    if not is_admin_allowed(interaction):
        return await interaction.response.send_message("❌ 你沒有權限。", ephemeral=True)

    lines = []
    for g in bot.guilds:
        track = now_playing.get(g.id)
        if track:
            lines.append(f"🎧 **{g.name}**：{track['title']}")
        else:
            lines.append(f"📭 **{g.name}**：無播放")

    embed = discord.Embed(
        title="📊 播放狀態",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )
    await interaction.response.send_message(embed=embed)

@tree.command(name="leave_server", description="（管理）讓 Bot 離開伺服器")
async def leave_server_cmd(interaction: discord.Interaction, guild_id: str):
    if not is_admin_allowed(interaction):
        return await interaction.response.send_message("❌ 你沒有權限。", ephemeral=True)

    try:
        gid = int(guild_id)
    except:
        return await interaction.response.send_message("❌ ID 格式錯誤")

    guild = bot.get_guild(gid)
    if not guild:
        return await interaction.response.send_message("❌ 找不到伺服器")

    await guild.leave()
    await interaction.response.send_message(f"👋 已離開 **{guild.name}**")

# ============================================================
# Bot 啟動
# ============================================================
@bot.event
async def on_ready():
    await tree.sync()
    print(f"🤖 Bot 已登入：{bot.user} (ID: {bot.user.id})")
    bot.loop.create_task(auto_disconnect_loop())

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("❌ DISCORD_TOKEN 缺失！")
    bot.run(TOKEN)
