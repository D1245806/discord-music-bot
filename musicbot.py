import os
import asyncio
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import yt_dlp

# ============================================================
# 讀取環境變數
# ============================================================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# 可選：如果你自己在本機或 Railway 想改 ffmpeg 路徑，可以設 FFMPEG_PATH
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "/usr/bin/ffmpeg")

# ============================================================
# Bot & Intents 設定
# ============================================================
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ============================================================
# yt-dlp & ffmpeg 設定
# ============================================================
YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "nocheckcertificate": True,
}

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
    "executable": FFMPEG_PATH,
}

# ============================================================
# 狀態儲存（依 guild 分開）
# ============================================================
queues: dict[int, list[dict]] = {}         # guild_id -> [track, ...]
now_playing: dict[int, dict | None] = {}   # guild_id -> track
loop_flags: dict[int, bool] = {}           # guild_id -> 是否單曲循環
start_times: dict[int, datetime | None] = {}  # guild_id -> 播放開始時間 (UTC)


# ============================================================
# 工具：Spotify 連結轉成 YouTube 搜尋
# ============================================================
def maybe_convert_spotify_to_search(query: str) -> str:
    """
    如果是 Spotify 歌曲連結，就用 yt-dlp 抓歌名，轉成 ytsearch: 查 YouTube。
    （不直接從 Spotify 播放，只用來查歌名）
    """
    if "open.spotify.com/track" not in query:
        return query

    try:
        with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
            info = ydl.extract_info(query, download=False)
        title = info.get("title")
        artist = info.get("artist") or ""
        if title:
            search_q = f"ytsearch1:{title} {artist}"
            return search_q
    except Exception:
        # 抓不到就當一般文字搜尋處理
        pass

    return query


# ============================================================
# 工具：用 yt-dlp 抓 metadata（不下載檔案）
# ============================================================
def get_track_info(url_or_query: str) -> dict:
    """
    傳回：
    {
      "webpage_url": 原始頁面或搜尋結果URL,
      "title": 標題,
      "duration": 秒數(int) 或 None,
      "thumbnail": 圖片URL 或 None
    }
    """
    query = maybe_convert_spotify_to_search(url_or_query)

    # 如果不是網址，就當成 ytsearch 搜尋
    if not (query.startswith("http://") or query.startswith("https://")):
        query = f"ytsearch1:{query}"

    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(query, download=False)

    # 如果是搜尋結果，會在 "entries" 裡
    if "entries" in info:
        info = info["entries"][0]

    return {
        "webpage_url": info.get("webpage_url") or info.get("url"),
        "title": info.get("title", "Unknown Title"),
        "duration": info.get("duration") or 0,
        "thumbnail": info.get("thumbnail"),
    }


# ============================================================
# 核心：播放下一首
# ============================================================
async def play_next(guild_id: int, vc: discord.VoiceClient):
    if guild_id not in queues:
        queues[guild_id] = []
    if guild_id not in loop_flags:
        loop_flags[guild_id] = False

    track = None

    if loop_flags[guild_id] and now_playing.get(guild_id):
        # 單曲循環：再播一次現在這首
        track = now_playing[guild_id]
    else:
        if not queues[guild_id]:
            now_playing[guild_id] = None
            start_times[guild_id] = None
            return
        track = queues[guild_id].pop(0)
        now_playing[guild_id] = track

    # 用 yt-dlp 取得實際音訊串流 URL
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(track["webpage_url"], download=False)
    audio_url = info["url"]

    source = discord.FFmpegPCMAudio(audio_url, **FFMPEG_OPTS)
    start_times[guild_id] = datetime.now(timezone.utc)

    def after_play(err: Exception | None):
        if err:
            print("Player error:", err)
        fut = asyncio.run_coroutine_threadsafe(
            play_next(guild_id, vc), bot.loop
        )
        try:
            fut.result()
        except Exception as e:
            print("Error in after_play:", e)

    vc.play(source, after=after_play)


# ============================================================
# 工具：確保連線到語音房
# ============================================================
async def ensure_voice(interaction: discord.Interaction) -> discord.VoiceClient | None:
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("❌ 你需要先加入一個語音頻道！", ephemeral=True)
        return None

    voice_channel = interaction.user.voice.channel
    vc = interaction.guild.voice_client

    if vc is None:
        vc = await voice_channel.connect()
    elif vc.channel != voice_channel:
        await vc.move_to(voice_channel)

    return vc


# ============================================================
# 指令： /play
# ============================================================
@tree.command(name="play", description="播放音樂（支援 YouTube 連結、關鍵字搜尋、Spotify 歌曲連結）")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()

    guild_id = interaction.guild_id
    vc = await ensure_voice(interaction)
    if vc is None:
        return

    try:
        track = get_track_info(query)
    except Exception as e:
        await interaction.followup.send(f"❌ 取得音樂資訊失敗：{e}")
        return

    if guild_id not in queues:
        queues[guild_id] = []
    queues[guild_id].append(track)

    # 建立排隊 Embed
    embed = discord.Embed(
        title="🎶 已加入佇列",
        description=f"**{track['title']}**",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="來源", value=track["webpage_url"], inline=False)
    if track["duration"]:
        mins = track["duration"] // 60
        secs = track["duration"] % 60
        embed.add_field(name="長度", value=f"{mins:02d}:{secs:02d}", inline=True)
    if track["thumbnail"]:
        embed.set_thumbnail(url=track["thumbnail"])

    await interaction.followup.send(embed=embed)

    if not vc.is_playing():
        await play_next(guild_id, vc)


# ============================================================
# 指令： /queue
# ============================================================
@tree.command(name="queue", description="查看目前播放佇列")
async def queue_cmd(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    q = queues.get(guild_id, [])

    if not q:
        await interaction.response.send_message("📭 目前佇列是空的！")
        return

    desc_lines = []
    for i, t in enumerate(q, start=1):
        desc_lines.append(f"`{i}.` {t['title']}")

    embed = discord.Embed(
        title="📜 播放佇列",
        description="\n".join(desc_lines),
        color=discord.Color.green(),
    )
    await interaction.response.send_message(embed=embed)


# ============================================================
# 工具：進度條 & /nowplaying
# ============================================================
def build_progress_bar(elapsed: int, duration: int, bar_len: int = 20) -> str:
    if duration <= 0:
        return "🔘" + "▬" * (bar_len - 1)

    ratio = min(max(elapsed / duration, 0), 1)
    pos = int(bar_len * ratio)
    bar = ""
    for i in range(bar_len):
        if i == pos:
            bar += "🔘"
        else:
            bar += "▬"
    return bar


@tree.command(name="nowplaying", description="顯示目前播放中的歌曲")
async def nowplaying(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    track = now_playing.get(guild_id)

    if not track:
        await interaction.response.send_message("🎧 目前沒有正在播放的歌曲")
        return

    duration = track.get("duration") or 0
    started = start_times.get(guild_id)
    if started:
        elapsed = int((datetime.now(timezone.utc) - started).total_seconds())
    else:
        elapsed = 0

    elapsed = max(0, min(elapsed, duration if duration > 0 else elapsed))

    # 時間字串
    def fmt(t: int) -> str:
        return f"{t // 60:02d}:{t % 60:02d}"

    bar = build_progress_bar(elapsed, duration)

    embed = discord.Embed(
        title="🎧 正在播放",
        description=f"**[{track['title']}]({track['webpage_url']})**",
        color=discord.Color.orange(),
    )
    if duration > 0:
        embed.add_field(
            name="進度",
            value=f"`{fmt(elapsed)} / {fmt(duration)}`\n{bar}",
            inline=False,
        )
    if track.get("thumbnail"):
        embed.set_thumbnail(url=track["thumbnail"])

    await interaction.response.send_message(embed=embed)


# ============================================================
# 指令： /skip
# ============================================================
@tree.command(name="skip", description="跳過目前這首歌")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        await interaction.response.send_message("❌ 目前沒有正在播放的歌曲")
        return

    vc.stop()
    await interaction.response.send_message("⏭ 已跳過！")


# ============================================================
# 指令： /loop
# ============================================================
@tree.command(name="loop", description="切換單曲循環（true=開 / false=關）")
async def loop(interaction: discord.Interaction, enabled: bool):
    guild_id = interaction.guild_id
    loop_flags[guild_id] = enabled
    status = "✅ 已開啟單曲循環" if enabled else "⏹ 已關閉單曲循環"
    await interaction.response.send_message(status)


# ============================================================
# 指令： /pause /resume /stop /leave
# ============================================================
@tree.command(name="pause", description="暫停播放")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        await interaction.response.send_message("❌ 沒有正在播放的歌曲")
        return
    vc.pause()
    await interaction.response.send_message("⏸ 已暫停")


@tree.command(name="resume", description="繼續播放")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_paused():
        await interaction.response.send_message("❌ 沒有暫停中的歌曲")
        return
    vc.resume()
    await interaction.response.send_message("▶ 已繼續播放")


@tree.command(name="stop", description="停止播放並清空佇列")
async def stop(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    queues[guild_id] = []
    loop_flags[guild_id] = False
    now_playing[guild_id] = None
    start_times[guild_id] = None

    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()

    await interaction.response.send_message("⏹ 已停止播放並清空佇列")


@tree.command(name="leave", description="讓機器人離開語音頻道")
async def leave(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc:
        await interaction.response.send_message("❌ 我現在不在任何語音頻道")
        return
    await vc.disconnect()
    await interaction.response.send_message("👋 已離開語音頻道")


# ============================================================
# Bot 啟動
# ============================================================
@bot.event
async def on_ready():
    await tree.sync()
    print(f"🤖 已登入：{bot.user} (ID: {bot.user.id})")


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("沒有在環境變數或 .env 中找到 DISCORD_TOKEN")
    bot.run(TOKEN)
