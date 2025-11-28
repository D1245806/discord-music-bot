import os
import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import yt_dlp

from flask import Flask
from threading import Thread

# ============================================================
# 讀取環境變數
# ============================================================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Railway / Docker 預設 ffmpeg 路徑（可以用環境變數覆蓋）
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
# 狀態儲存（依 guild 分開）
# ============================================================
Track = Dict[str, Optional[str]]

queues: Dict[int, List[Track]] = {}          # guild_id -> [track, ...]
now_playing: Dict[int, Optional[Track]] = {} # guild_id -> track
loop_flags: Dict[int, bool] = {}             # guild_id -> 是否單曲循環
start_times: Dict[int, Optional[datetime]] = {}
volume_settings: Dict[int, float] = {}       # 0.0 ~ 2.0，預設 1.0
last_active: Dict[int, datetime] = {}        # 最後活躍時間

# 播放歷史 & 次數統計
history: Dict[int, List[Track]] = {}         # guild_id -> 最近播放列表
play_counts: Dict[int, Dict[str, int]] = {}  # guild_id -> title -> count

# ============================================================
# Owner 設定（只有你能用管理指令）
# ============================================================
BOT_OWNER_ID = 477325882881605635  # 你的 Discord 使用者 ID

# ============================================================
# 小工具：更新最後活躍時間
# ============================================================
def touch_active(guild_id: int):
    last_active[guild_id] = datetime.now(timezone.utc)

# ============================================================
# 小工具：Spotify 連結轉搜尋（目前只是保留介面）
# ============================================================
def maybe_convert_spotify_to_search(query: str) -> str:
    if "open.spotify.com/track" not in query:
        return query
    # 之後可以自己加 Spotify → 歌名轉換
    return query

# ============================================================
# 小工具：取得單首歌曲資訊（不下載）
# ============================================================
def get_track_info(query: str) -> Track:
    q = maybe_convert_spotify_to_search(query)

    # 如果不是網址，就當成關鍵字搜尋
    if not (q.startswith("http://") or q.startswith("https://")):
        q = f"ytsearch1:{q}"

    ydl_opts = dict(YDL_OPTS_BASE)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(q, download=False)

    if "entries" in info:
        info = info["entries"][0]

    webpage_url = info.get("webpage_url") or info.get("url") or ""
    if webpage_url and not webpage_url.startswith("http"):
        # 有些時候只給 id
        webpage_url = f"https://www.youtube.com/watch?v={webpage_url}"

    return {
        "webpage_url": webpage_url,
        "title": info.get("title", "未知標題"),
        "duration": str(info.get("duration") or 0),
        "thumbnail": info.get("thumbnail"),
        "uploader": info.get("uploader"),
    }

# ============================================================
# 小工具：從 URL 取得實際音訊串流 URL
# ============================================================
def get_audio_url(webpage_url: str) -> str:
    with yt_dlp.YoutubeDL(YDL_OPTS_BASE) as ydl:
        info = ydl.extract_info(webpage_url, download=False)
    return info["url"]

# ============================================================
# 核心：播放下一首
# ============================================================
async def play_next(guild_id: int, vc: discord.VoiceClient):
    if guild_id not in queues:
        queues[guild_id] = []
    if guild_id not in loop_flags:
        loop_flags[guild_id] = False

    track: Optional[Track] = None

    if loop_flags[guild_id] and now_playing.get(guild_id):
        # 單曲循環：重播目前這首
        track = now_playing[guild_id]
    else:
        if not queues[guild_id]:
            now_playing[guild_id] = None
            start_times[guild_id] = None
            return
        track = queues[guild_id].pop(0)
        now_playing[guild_id] = track

        # 更新播放歷史
        if guild_id not in history:
            history[guild_id] = []
        history[guild_id].append(track)
        history[guild_id] = history[guild_id][-50:]  # 只留最近 50 首

        if guild_id not in play_counts:
            play_counts[guild_id] = {}
        title = track.get("title") or "未知標題"
        play_counts[guild_id][title] = play_counts[guild_id].get(title, 0) + 1

    if not track:
        return

    audio_url = get_audio_url(track["webpage_url"])  # type: ignore
    source = discord.FFmpegPCMAudio(audio_url, **FFMPEG_OPTS)

    vol = volume_settings.get(guild_id, 1.0)
    source = discord.PCMVolumeTransformer(source, volume=vol)

    start_times[guild_id] = datetime.now(timezone.utc)
    touch_active(guild_id)

    def after_play(err: Optional[Exception]):
        if err:
            print("播放錯誤:", err)
        fut = asyncio.run_coroutine_threadsafe(
            play_next(guild_id, vc), bot.loop
        )
        try:
            fut.result()
        except Exception as e:
            print("after_play 發生錯誤:", e)

    vc.play(source, after=after_play)

# ============================================================
# 自動斷線背景任務（沒人聽 or 閒置太久）
# ============================================================
async def auto_disconnect_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(timezone.utc)
        for guild in bot.guilds:
            vc: discord.VoiceClient = guild.voice_client  # type: ignore
            if not vc or not vc.is_connected():
                continue

            guild_id = guild.id
            last = last_active.get(guild_id)
            if not last:
                continue

            idle_seconds = (now - last).total_seconds()
            channel = vc.channel
            if not channel:
                continue

            non_bot_members = [m for m in channel.members if not m.bot]

            if (not non_bot_members or (not vc.is_playing() and not queues.get(guild_id))) and idle_seconds > 300:
                try:
                    await vc.disconnect()
                    now_playing[guild_id] = None
                    queues[guild_id] = []
                    loop_flags[guild_id] = False
                    start_times[guild_id] = None
                    print(f"自動斷線：guild {guild_id}")
                except Exception as e:
                    print("自動斷線錯誤:", e)
        await asyncio.sleep(60)

# ============================================================
# 工具：確保使用者 & 機器人在同一語音頻道
# ============================================================
async def ensure_voice(interaction: discord.Interaction) -> Optional[discord.VoiceClient]:
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("❌ 你需要先加入一個語音頻道！", ephemeral=True)
        return None

    voice_channel = interaction.user.voice.channel
    vc: discord.VoiceClient = interaction.guild.voice_client  # type: ignore

    if vc is None:
        vc = await voice_channel.connect()
    elif vc.channel != voice_channel:
        await vc.move_to(voice_channel)

    touch_active(interaction.guild_id)
    return vc

# ============================================================
# 進度條工具
# ============================================================
def build_progress_bar(elapsed: int, duration: int, length: int = 20) -> str:
    if duration <= 0:
        return "🔘" + "▬" * (length - 1)

    ratio = min(max(elapsed / duration, 0.0), 1.0)
    pos = int(length * ratio)
    bar = ""
    for i in range(length):
        if i == pos:
            bar += "🔘"
        else:
            bar += "▬"
    return bar

def fmt_time(sec: int) -> str:
    return f"{sec // 60:02d}:{sec % 60:02d}"

# ============================================================
# Slash 指令：/play
# ============================================================
@tree.command(name="play", description="播放音樂（支援 YouTube / 關鍵字 / Spotify 單曲連結）")
async def play_cmd(interaction: discord.Interaction, query: str):
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

    embed = discord.Embed(
        title="🎶 已加入佇列",
        description=f"**{track['title']}**",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="來源", value=track["webpage_url"], inline=False)
    if track.get("duration"):
        d = int(track["duration"])  # type: ignore
        embed.add_field(name="長度", value=f"{fmt_time(d)}", inline=True)
    if track.get("uploader"):
        embed.add_field(name="頻道", value=track["uploader"], inline=True)
    if track.get("thumbnail"):
        embed.set_thumbnail(url=track["thumbnail"])

    await interaction.followup.send(embed=embed)

    if not vc.is_playing():
        await play_next(guild_id, vc)

# ============================================================
# Slash 指令：/search（多結果選歌）
# ============================================================
class SearchView(discord.ui.View):
    def __init__(self, user_id: int, results: List[Track], guild_id: int, timeout: int = 30):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.results = results
        self.guild_id = guild_id
        for i, track in enumerate(results[:5], start=1):
            self.add_item(SearchButton(label=str(i), track=track, parent_view=self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("這個選單只限原指令發送者使用。", ephemeral=True)
            return False
        return True

class SearchButton(discord.ui.Button):
    def __init__(self, label: str, track: Track, parent_view: SearchView):
        super().__init__(style=discord.ButtonStyle.primary, label=label)
        self.track = track
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        vc = await ensure_voice(interaction)
        if vc is None:
            return

        if guild_id not in queues:
            queues[guild_id] = []
        queues[guild_id].append(self.track)

        if not vc.is_playing():
            await play_next(guild_id, vc)

        await interaction.response.edit_message(
            content=f"✅ 已選擇並加入佇列：**{self.track['title']}**",
            view=None
        )

@tree.command(name="search", description="搜尋歌曲並從多個結果中選擇播放")
async def search_cmd(interaction: discord.Interaction, keyword: str):
    await interaction.response.defer(ephemeral=True)

    q = f"ytsearch5:{keyword}"
    with yt_dlp.YoutubeDL(YDL_OPTS_BASE) as ydl:
        info = ydl.extract_info(q, download=False)

    entries = info.get("entries", [])[:5]
    if not entries:
        await interaction.followup.send("❌ 找不到相關歌曲。", ephemeral=True)
        return

    results: List[Track] = []
    desc_lines = []
    for i, e in enumerate(entries, start=1):
        webpage_url = e.get("webpage_url") or e.get("url") or ""
        if webpage_url and not webpage_url.startswith("http"):
            webpage_url = f"https://www.youtube.com/watch?v={webpage_url}"

        t = {
            "webpage_url": webpage_url,
            "title": e.get("title", "未知標題"),
            "duration": str(e.get("duration") or 0),
            "thumbnail": e.get("thumbnail"),
            "uploader": e.get("uploader"),
        }
        results.append(t)
        d = int(t["duration"]) if t["duration"] else 0
        desc_lines.append(f"`{i}.` {t['title']} （{fmt_time(d)}）")

    embed = discord.Embed(
        title=f"🔍 搜尋結果：{keyword}",
        description="\n".join(desc_lines),
        color=discord.Color.green(),
    )

    view = SearchView(interaction.user.id, results, interaction.guild_id)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)

# ============================================================
# Slash 指令：/queue & /clearqueue
# ============================================================
@tree.command(name="queue", description="查看目前播放佇列")
async def queue_cmd(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    q = queues.get(guild_id, [])

    if not q:
        await interaction.response.send_message("📭 目前佇列是空的。")
        return

    lines = []
    for i, t in enumerate(q, start=1):
        d = int(t["duration"]) if t.get("duration") else 0
        lines.append(f"`{i}.` {t['title']} （{fmt_time(d)}）")

    embed = discord.Embed(
        title="📜 播放佇列",
        description="\n".join(lines),
        color=discord.Color.teal(),
    )
    await interaction.response.send_message(embed=embed)

@tree.command(name="clearqueue", description="清空佇列（不影響目前播放）")
async def clearqueue_cmd(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    queues[guild_id] = []
    await interaction.response.send_message("🧹 已清空佇列（目前播放中的歌曲不受影響）。")

# ============================================================
# Slash 指令：/skip /loop /pause /resume /stop /leave
# ============================================================
@tree.command(name="skip", description="跳過目前這首歌")
async def skip_cmd(interaction: discord.Interaction):
    vc: discord.VoiceClient = interaction.guild.voice_client  # type: ignore
    if not vc or not vc.is_playing():
        await interaction.response.send_message("❌ 目前沒有正在播放的歌曲。")
        return
    vc.stop()
    touch_active(interaction.guild_id)
    await interaction.response.send_message("⏭ 已跳過目前歌曲。")

@tree.command(name="loop", description="設定是否開啟單曲循環（true=開 / false=關）")
async def loop_cmd(interaction: discord.Interaction, enabled: bool):
    guild_id = interaction.guild_id
    loop_flags[guild_id] = enabled
    msg = "🔁 已開啟單曲循環。" if enabled else "⏹ 已關閉單曲循環。"
    await interaction.response.send_message(msg)

@tree.command(name="pause", description="暫停播放")
async def pause_cmd(interaction: discord.Interaction):
    vc: discord.VoiceClient = interaction.guild.voice_client  # type: ignore
    if not vc or not vc.is_playing():
        await interaction.response.send_message("❌ 目前沒有正在播放的歌曲。")
        return
    vc.pause()
    touch_active(interaction.guild_id)
    await interaction.response.send_message("⏸ 已暫停播放。")

@tree.command(name="resume", description="繼續播放")
async def resume_cmd(interaction: discord.Interaction):
    vc: discord.VoiceClient = interaction.guild.voice_client  # type: ignore
    if not vc or not vc.is_paused():
        await interaction.response.send_message("❌ 沒有暫停中的歌曲。")
        return
    vc.resume()
    touch_active(interaction.guild_id)
    await interaction.response.send_message("▶ 已繼續播放。")

@tree.command(name="stop", description="停止播放並清空佇列")
async def stop_cmd(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    queues[guild_id] = []
    loop_flags[guild_id] = False
    now_playing[guild_id] = None
    start_times[guild_id] = None

    vc: discord.VoiceClient = interaction.guild.voice_client  # type: ignore
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()

    await interaction.response.send_message("⏹ 已停止播放並清空佇列。")

@tree.command(name="leave", description="讓機器人離開語音頻道")
async def leave_cmd(interaction: discord.Interaction):
    vc: discord.VoiceClient = interaction.guild.voice_client  # type: ignore
    if not vc:
        await interaction.response.send_message("❌ 我目前不在任何語音頻道裡。")
        return
    await vc.disconnect()
    await interaction.response.send_message("👋 已離開語音頻道。")

# ============================================================
# Slash 指令：/nowplaying（進度條 + 封面）
# ============================================================
@tree.command(name="nowplaying", description="顯示目前正在播放的歌曲")
async def nowplaying_cmd(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    track = now_playing.get(guild_id)
    if not track:
        await interaction.response.send_message("🎧 目前沒有正在播放的歌曲。")
        return

    duration = int(track.get("duration") or 0)
    started = start_times.get(guild_id)
    if started:
        elapsed = int((datetime.now(timezone.utc) - started).total_seconds())
    else:
        elapsed = 0

    if duration > 0:
        elapsed = max(0, min(elapsed, duration))

    bar = build_progress_bar(elapsed, duration)
    embed = discord.Embed(
        title="🎧 正在播放",
        description=f"**[{track['title']}]({track['webpage_url']})**",
        color=discord.Color.orange(),
    )
    if duration > 0:
        embed.add_field(
            name="進度",
            value=f"`{fmt_time(elapsed)} / {fmt_time(duration)}`\n{bar}",
            inline=False,
        )
    if track.get("uploader"):
        embed.add_field(name="頻道", value=track["uploader"], inline=True)
    if track.get("thumbnail"):
        embed.set_thumbnail(url=track["thumbnail"])

    await interaction.response.send_message(embed=embed)

# ============================================================
# Slash 指令：/volume（0~200）
# ============================================================
@tree.command(name="volume", description="調整音量（0~200）")
async def volume_cmd(interaction: discord.Interaction, volume: int):
    if volume < 0 or volume > 200:
        await interaction.response.send_message("❌ 音量範圍為 0 ~ 200。", ephemeral=True)
        return

    guild_id = interaction.guild_id
    volume_settings[guild_id] = volume / 100.0

    vc: discord.VoiceClient = interaction.guild.voice_client  # type: ignore
    if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = volume_settings[guild_id]

    await interaction.response.send_message(f"🔊 已將音量設定為 {volume}%。")

# ============================================================
# Slash 指令：/playlist（加入 YouTube 播放清單）
# ============================================================
@tree.command(name="playlist", description="加入整個 YouTube 播放清單（預設最多 50 首）")
async def playlist_cmd(interaction: discord.Interaction, url: str, limit: int = 50):
    await interaction.response.defer()

    guild_id = interaction.guild_id
    vc = await ensure_voice(interaction)
    if vc is None:
        return

    try:
        ydl_opts = dict(YDL_OPTS_BASE)
        ydl_opts["extract_flat"] = True
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        await interaction.followup.send(f"❌ 讀取播放清單失敗：{e}")
        return

    entries = info.get("entries", [])[:max(1, min(limit, 100))]
    if not entries:
        await interaction.followup.send("❌ 播放清單中沒有可用的音樂。")
        return

    if guild_id not in queues:
        queues[guild_id] = []

    count = 0
    for e in entries:
        webpage_url = e.get("url") or e.get("webpage_url")
        if not webpage_url:
            continue
        if not webpage_url.startswith("http"):
            webpage_url = f"https://www.youtube.com/watch?v={webpage_url}"

        t = {
            "webpage_url": webpage_url,
            "title": e.get("title", "未知標題"),
            "duration": str(e.get("duration") or 0),
            "thumbnail": e.get("thumbnail"),
            "uploader": e.get("uploader"),
        }
        queues[guild_id].append(t)
        count += 1

    await interaction.followup.send(f"📑 已從播放清單加入 {count} 首歌曲到佇列。")

    if not vc.is_playing():
        await play_next(guild_id, vc)

# ============================================================
# Slash 指令：/lyrics（給目前歌曲的歌詞搜尋連結）
# ============================================================
@tree.command(name="lyrics", description="顯示目前歌曲的歌詞搜尋連結")
async def lyrics_cmd(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    track = now_playing.get(guild_id)
    if not track:
        await interaction.response.send_message("🎧 目前沒有正在播放的歌曲。")
        return

    title = track.get("title") or ""
    if not title:
        await interaction.response.send_message("❌ 找不到歌曲標題，無法搜尋歌詞。")
        return

    query = f"{title} 歌詞"
    url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
    embed = discord.Embed(
        title="📖 歌詞搜尋",
        description=f"點此搜尋 **{title}** 的歌詞：\n{url}",
        color=discord.Color.purple(),
    )
    await interaction.response.send_message(embed=embed)

# ============================================================
# Slash 指令：/history /top /recommend
# ============================================================
@tree.command(name="history", description="顯示最近播放紀錄（最多 20 首）")
async def history_cmd(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    h = history.get(guild_id, [])
    if not h:
        await interaction.response.send_message("📭 尚無播放紀錄。")
        return

    lines = []
    for i, t in enumerate(h[-20:], start=1):
        lines.append(f"`{i}.` {t.get('title', '未知標題')}")
    embed = discord.Embed(
        title="📚 最近播放紀錄",
        description="\n".join(lines),
        color=discord.Color.blue(),
    )
    await interaction.response.send_message(embed=embed)

@tree.command(name="top", description="顯示本伺服器最常播放的前 10 首歌")
async def top_cmd(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    pc = play_counts.get(guild_id, {})
    if not pc:
        await interaction.response.send_message("📭 尚無統計資料。")
        return

    sorted_items = sorted(pc.items(), key=lambda x: x[1], reverse=True)[:10]
    lines = []
    for i, (title, cnt) in enumerate(sorted_items, start=1):
        lines.append(f"`{i}.` {title}（播放 {cnt} 次）")

    embed = discord.Embed(
        title="🏆 最常播放 TOP 10",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )
    await interaction.response.send_message(embed=embed)

@tree.command(name="recommend", description="根據歷史播放推薦一首常播放的歌曲")
async def recommend_cmd(interaction: discord.Interaction):
    import random

    guild_id = interaction.guild_id
    pc = play_counts.get(guild_id, {})
    if not pc:
        await interaction.response.send_message("📭 尚無播放紀錄可以推薦。")
        return

    titles = list(pc.keys())
    weights = [pc[t] for t in titles]
    chosen_title = random.choices(titles, weights=weights, k=1)[0]

    await interaction.response.send_message(f"🤖 推薦你再聽一次：**{chosen_title}**（依照播放次數推薦）")

# ============================================================
# 管理工具：檢查是否允許使用管理指令
#   👉 只看 user.id，不看 guild
# ============================================================
def is_admin_allowed(interaction: discord.Interaction) -> bool:
    return interaction.user.id == BOT_OWNER_ID

# ============================================================
# /servercount → 顯示 bot 加了幾個伺服器
# ============================================================
@tree.command(name="servercount", description="（管理）顯示 Bot 加了多少個伺服器")
async def servercount_cmd(interaction: discord.Interaction):
    if not is_admin_allowed(interaction):
        await interaction.response.send_message(
            "❌ 你沒有權限使用這個管理指令。",
            ephemeral=True
        )
        return

    count = len(bot.guilds)
    await interaction.response.send_message(
        f"📊 我目前加入了 **{count}** 個伺服器。",
        ephemeral=True
    )

# ============================================================
# /stats → 顯示每個伺服器正在播放什麼歌
# ============================================================
@tree.command(name="stats", description="（管理）查看所有伺服器目前正在播放的歌曲")
async def stats_cmd(interaction: discord.Interaction):
    if not is_admin_allowed(interaction):
        await interaction.response.send_message(
            "❌ 你沒有權限使用這個管理指令。",
            ephemeral=True
        )
        return

    lines = []
    for g in bot.guilds:
        track = now_playing.get(g.id)
        if track:
            lines.append(f"🎧 **{g.name}**：{track.get('title', '未知標題')}")
        else:
            lines.append(f"📭 **{g.name}**：目前沒有播放音樂")

    embed = discord.Embed(
        title="📊 所有伺服器播放狀態",
        description="\n".join(lines) if lines else "目前沒有任何伺服器。",
        color=discord.Color.gold()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ============================================================
# /leave_server <guild_id> → 讓 bot 離開伺服器（遠端操作）
# ============================================================
@tree.command(name="leave_server", description="（管理）讓機器人離開指定伺服器")
async def leave_server_cmd(interaction: discord.Interaction, guild_id: str):
    if not is_admin_allowed(interaction):
        await interaction.response.send_message(
            "❌ 你沒有權限使用這個管理指令。",
            ephemeral=True
        )
        return

    try:
        gid = int(guild_id)
    except:
        await interaction.response.send_message("❌ guild_id 格式錯誤，必須是數字。", ephemeral=True)
        return

    guild = bot.get_guild(gid)
    if not guild:
        await interaction.response.send_message("❌ 找不到這個伺服器，也許我不在裡面。", ephemeral=True)
        return

    try:
        await guild.leave()
        await interaction.response.send_message(f"👋 已成功離開伺服器：**{guild.name}**", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ 離開伺服器時發生錯誤：{e}", ephemeral=True)

# ============================================================
# /servers → 顯示加入的伺服器清單
#   👉 你在哪一個伺服器打都可以
# ============================================================
@tree.command(name="servers", description="（管理）查看機器人目前加入的所有伺服器")
async def servers_cmd(interaction: discord.Interaction):
    if not is_admin_allowed(interaction):
        await interaction.response.send_message(
            "❌ 你沒有權限使用這個管理指令。",
            ephemeral=True
        )
        return

    guilds = bot.guilds
    if not guilds:
        await interaction.response.send_message("🤖 我目前沒有加入任何伺服器。", ephemeral=True)
        return

    lines = [f"**{g.name}**（ID: `{g.id}`）" for g in guilds]
    embed = discord.Embed(
        title="📋 我加入的伺服器列表",
        description="\n".join(lines),
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ============================================================
# Flask Keep-Alive（讓 Railway 看到有 HTTP 服務）
# ============================================================
flask_app = Flask("musicbot")

@flask_app.route("/")
def index():
    return "Discord music bot is running!", 200

def run_flask():
    port = int(os.environ.get("PORT", 3000))
    flask_app.run(host="0.0.0.0", port=port, debug=False)

def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()

# ============================================================
# Bot 啟動事件
# ============================================================
@bot.event
async def on_ready():
    await tree.sync()
    print(f"🤖 已登入：{bot.user} (ID: {bot.user.id})")

    if not hasattr(bot, "auto_dc_task"):
        bot.auto_dc_task = bot.loop.create_task(auto_disconnect_loop())

# ============================================================
# Main
# ============================================================
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("沒有在環境變數或 .env 中找到 DISCORD_TOKEN")

    # 啟動 Flask keep-alive（背景執行）
    keep_alive()

    # 啟動 Discord Bot
    bot.run(TOKEN)
