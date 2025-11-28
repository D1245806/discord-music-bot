import os
import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Optional

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

# ffmpeg 路徑（Railway / Docker 預設）
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "/usr/bin/ffmpeg")

# ============================================================
# Bot Intents
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
    "extractor_args": {"youtube": {"player_client": "default"}},  # 去除 JS 警告
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
# 活躍更新
# ============================================================
def touch_active(guild_id: int):
    last_active[guild_id] = datetime.now(timezone.utc)

# ============================================================
# Spotify 轉換（目前保留接口）
# ============================================================
def maybe_convert_spotify_to_search(query: str) -> str:
    if "open.spotify.com/track" not in query:
        return query
    return query  # 未實作的介面

# ============================================================
# 取得歌曲資訊（不下載）
# ============================================================
def get_track_info(query: str) -> Track:
    q = maybe_convert_spotify_to_search(query)

    if not (q.startswith("http://") or q.startswith("https://")):
        q = f"ytsearch1:{q}"

    ydl_opts = dict(YDL_OPTS_BASE)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(q, download=False)

    if "entries" in info:
        info = info["entries"][0]

    webpage_url = info.get("webpage_url") or info.get("url") or ""
    if webpage_url and not webpage_url.startswith("http"):
        webpage_url = f"https://www.youtube.com/watch?v={webpage_url}"

    return {
        "webpage_url": webpage_url,
        "title": info.get("title", "未知標題"),
        "duration": str(info.get("duration") or 0),
        "thumbnail": info.get("thumbnail"),
        "uploader": info.get("uploader"),
    }

# ============================================================
# 抓音訊 URL
# ============================================================
def get_audio_url(webpage_url: str) -> str:
    with yt_dlp.YoutubeDL(YDL_OPTS_BASE) as ydl:
        info = ydl.extract_info(webpage_url, download=False)
    return info["url"]

# ============================================================
# 播放下一首
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

        # 播放紀錄
        if guild_id not in history:
            history[guild_id] = []
        history[guild_id].append(track)
        history[guild_id] = history[guild_id][-50:]

        # 次數統計
        if guild_id not in play_counts:
            play_counts[guild_id] = {}
        title = track.get("title") or "未知標題"
        play_counts[guild_id][title] = play_counts[guild_id].get(title, 0) + 1

    if not track:
        return

    audio_url = get_audio_url(track["webpage_url"])
    source = discord.FFmpegPCMAudio(audio_url, **FFMPEG_OPTS)

    vol = volume_settings.get(guild_id, 1.0)
    source = discord.PCMVolumeTransformer(source, volume=vol)

    start_times[guild_id] = datetime.now(timezone.utc)
    touch_active(guild_id)

    def after_play(error: Exception):
        if error:
            print("播放錯誤:", error)

        fut = asyncio.run_coroutine_threadsafe(
            play_next(guild_id, vc), bot.loop
        )
        try:
            fut.result()
        except Exception as e:
            print("after_play error:", e)

    vc.play(source, after=after_play)

# ============================================================
# 自動斷線
# ============================================================
async def auto_disconnect_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(timezone.utc)

        for guild in bot.guilds:
            vc: discord.VoiceClient = guild.voice_client
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

            non_bot = [m for m in channel.members if not m.bot]

            if (not non_bot or (not vc.is_playing() and not queues.get(guild_id))) and idle_seconds > 300:
                try:
                    await vc.disconnect()
                    now_playing[guild_id] = None
                    queues[guild_id] = []
                    loop_flags[guild_id] = False
                    start_times[guild_id] = None
                except Exception as e:
                    print("自動斷線錯誤:", e)

        await asyncio.sleep(60)

# ============================================================
# 必須在語音頻道
# ============================================================
async def ensure_voice(interaction: discord.Interaction):
    if not interaction.user.voice:
        await interaction.response.send_message("❌ 你需要先加入語音頻道！")
        return None

    voice_channel = interaction.user.voice.channel
    vc = interaction.guild.voice_client

    if vc is None:
        vc = await voice_channel.connect()
    elif vc.channel != voice_channel:
        await vc.move_to(voice_channel)

    touch_active(interaction.guild_id)
    return vc

# ============================================================
# 進度條
# ============================================================
def build_progress_bar(elapsed, duration, length=20):
    if duration <= 0:
        return "🔘" + "▬" * (length - 1)

    ratio = min(max(elapsed / duration, 0), 1)
    pos = int(length * ratio)

    bar = ""
    for i in range(length):
        bar += "🔘" if i == pos else "▬"
    return bar


def fmt_time(sec):
    return f"{sec // 60:02d}:{sec % 60:02d}"
# ============================================================
# /play 指令
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

    queues.setdefault(guild_id, []).append(track)

    embed = discord.Embed(
        title="🎶 已加入佇列",
        description=f"**{track['title']}**",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="來源", value=track["webpage_url"], inline=False)

    if track.get("duration"):
        d = int(track["duration"])
        embed.add_field(name="長度", value=fmt_time(d), inline=True)

    if track.get("uploader"):
        embed.add_field(name="頻道", value=track["uploader"], inline=True)

    if track.get("thumbnail"):
        embed.set_thumbnail(url=track["thumbnail"])

    await interaction.followup.send(embed=embed)

    if not vc.is_playing():
        await play_next(guild_id, vc)

# ============================================================
# /search 指令（可選多個）
# ============================================================
class SearchView(discord.ui.View):
    def __init__(self, user_id: int, results: List[Track]):
        super().__init__(timeout=30)
        self.user_id = user_id
        self.results = results

        for i, t in enumerate(results[:5], start=1):
            self.add_item(SearchButton(str(i), t))

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ 此選單不是給你的。", ephemeral=True)
            return False
        return True


class SearchButton(discord.ui.Button):
    def __init__(self, label: str, track: Track):
        super().__init__(label=label, style=discord.ButtonStyle.primary)
        self.track = track

    async def callback(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        vc = await ensure_voice(interaction)
        if vc is None:
            return

        queues.setdefault(guild_id, []).append(self.track)

        if not vc.is_playing():
            await play_next(guild_id, vc)

        await interaction.response.edit_message(
            content=f"已加入：**{self.track['title']}**",
            view=None
        )


@tree.command(name="search", description="搜尋歌曲並選擇播放")
async def search_cmd(interaction: discord.Interaction, keyword: str):
    await interaction.response.defer(ephemeral=True)

    with yt_dlp.YoutubeDL(YDL_OPTS_BASE) as ydl:
        info = ydl.extract_info(f"ytsearch5:{keyword}", download=False)

    entries = info.get("entries", [])[:5]
    if not entries:
        await interaction.followup.send("❌ 找不到歌曲。", ephemeral=True)
        return

    results = []
    desc = []

    for i, e in enumerate(entries, start=1):
        url = e.get("webpage_url") or e.get("url")
        if url and not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={url}"

        t = {
            "webpage_url": url,
            "title": e.get("title", "未知"),
            "duration": str(e.get("duration") or 0),
            "thumbnail": e.get("thumbnail"),
            "uploader": e.get("uploader"),
        }
        results.append(t)

        d = int(t["duration"])
        desc.append(f"`{i}.` {t['title']}（{fmt_time(d)}）")

    embed = discord.Embed(
        title=f"🔍 搜尋結果：{keyword}",
        description="\n".join(desc),
        color=discord.Color.green(),
    )

    await interaction.followup.send(
        embed=embed,
        view=SearchView(interaction.user.id, results),
        ephemeral=True
    )

# ============================================================
# /queue
# ============================================================
@tree.command(name="queue", description="查看播放佇列")
async def queue_cmd(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    q = queues.get(guild_id, [])

    if not q:
        await interaction.response.send_message("📭 佇列是空的")
        return

    lines = []
    for i, t in enumerate(q, start=1):
        d = int(t["duration"])
        lines.append(f"`{i}.` {t['title']}（{fmt_time(d)}）")

    embed = discord.Embed(
        title="📜 播放佇列",
        description="\n".join(lines),
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed)

# ============================================================
# /clearqueue
# ============================================================
@tree.command(name="clearqueue", description="清空佇列")
async def clearqueue_cmd(interaction: discord.Interaction):
    queues[interaction.guild_id] = []
    await interaction.response.send_message("🧹 已清空佇列")

# ============================================================
# 播放控制相關（skip / loop / pause / resume / stop / leave）
# ============================================================
@tree.command(name="skip", description="跳過目前歌曲")
async def skip_cmd(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        await interaction.response.send_message("❌ 沒有正在播放的歌曲")
        return

    vc.stop()
    touch_active(interaction.guild_id)
    await interaction.response.send_message("⏭ 已跳過")

@tree.command(name="loop", description="單曲循環 on/off")
async def loop_cmd(interaction: discord.Interaction, enabled: bool):
    loop_flags[interaction.guild_id] = enabled
    await interaction.response.send_message("🔁 單曲循環已設定為：" + ("開啟" if enabled else "關閉"))

@tree.command(name="pause", description="暫停播放")
async def pause_cmd(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        await interaction.response.send_message("❌ 沒有歌曲在播放")
        return
    vc.pause()
    await interaction.response.send_message("⏸ 已暫停")

@tree.command(name="resume", description="繼續播放")
async def resume_cmd(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_paused():
        await interaction.response.send_message("❌ 沒有暫停中的歌曲")
        return
    vc.resume()
    await interaction.response.send_message("▶ 已繼續")

@tree.command(name="stop", description="停止播放並清空佇列")
async def stop_cmd(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    queues[guild_id] = []
    now_playing[guild_id] = None
    loop_flags[guild_id] = False

    vc = interaction.guild.voice_client
    if vc:
        vc.stop()

    await interaction.response.send_message("⏹ 已停止播放並清空佇列")

@tree.command(name="leave", description="離開語音頻道")
async def leave_cmd(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc:
        await vc.disconnect()
        await interaction.response.send_message("👋 已離開")
    else:
        await interaction.response.send_message("❌ 我不在語音頻道中")

# ============================================================
# /nowplaying
# ============================================================
@tree.command(name="nowplaying", description="顯示目前播放")
async def nowplaying_cmd(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    track = now_playing.get(guild_id)

    if not track:
        await interaction.response.send_message("🎧 沒有正在播的歌曲")
        return

    duration = int(track["duration"])
    started = start_times.get(guild_id)
    elapsed = int((datetime.now(timezone.utc) - started).total_seconds()) if started else 0
    elapsed = min(elapsed, duration)

    bar = build_progress_bar(elapsed, duration)

    embed = discord.Embed(
        title="🎧 正在播放",
        description=f"**[{track['title']}]({track['webpage_url']})**",
        color=discord.Color.orange(),
    )
    embed.add_field(
        name="進度",
        value=f"`{fmt_time(elapsed)} / {fmt_time(duration)}`\n{bar}",
        inline=False
    )
    if track.get("thumbnail"):
        embed.set_thumbnail(url=track["thumbnail"])

    await interaction.response.send_message(embed=embed)

# ============================================================
# /volume
# ============================================================
@tree.command(name="volume", description="調整音量（0~200）")
async def volume_cmd(interaction: discord.Interaction, volume: int):
    if volume < 0 or volume > 200:
        await interaction.response.send_message("❌ 範圍為 0~200")
        return

    guild_id = interaction.guild_id
    volume_settings[guild_id] = volume / 100

    vc = interaction.guild.voice_client
    if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = volume_settings[guild_id]

    await interaction.response.send_message(f"🔊 音量已設定為 {volume}%")

# ============================================================
# /playlist（加入播放清單）
# ============================================================
@tree.command(name="playlist", description="加入整個 YouTube 播放清單")
async def playlist_cmd(interaction: discord.Interaction, url: str, limit: int = 50):
    await interaction.response.defer()

    vc = await ensure_voice(interaction)
    if vc is None:
        return

    try:
        ydl_opts = dict(YDL_OPTS_BASE)
        ydl_opts["extract_flat"] = True
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        await interaction.followup.send(f"❌ 播放清單讀取失敗：{e}")
        return

    entries = info.get("entries", [])[:limit]

    guild_id = interaction.guild_id
    queues.setdefault(guild_id, [])

    for e in entries:
        url = e.get("url") or e.get("webpage_url")
        if not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={url}"

        queues[guild_id].append({
            "webpage_url": url,
            "title": e.get("title", "未知標題"),
            "duration": str(e.get("duration") or 0),
            "thumbnail": e.get("thumbnail"),
            "uploader": e.get("uploader"),
        })

    await interaction.followup.send(f"📑 已加入 {len(entries)} 首歌曲")

    if not vc.is_playing():
        await play_next(guild_id, vc)

# ============================================================
# /lyrics
# ============================================================
@tree.command(name="lyrics", description="搜尋歌詞")
async def lyrics_cmd(interaction: discord.Interaction):
    track = now_playing.get(interaction.guild_id)
    if not track:
        await interaction.response.send_message("❌ 目前沒有播放中的歌曲")
        return

    title = track["title"]
    url = f"https://www.google.com/search?q={title}+歌詞"
    embed = discord.Embed(
        title="📖 歌詞搜尋",
        description=url,
        color=discord.Color.purple(),
    )
    await interaction.response.send_message(embed=embed)

# ============================================================
# /history
# ============================================================
@tree.command(name="history", description="播放紀錄")
async def history_cmd(interaction: discord.Interaction):
    h = history.get(interaction.guild_id, [])
    if not h:
        await interaction.response.send_message("📭 尚無紀錄")
        return

    lines = [f"`{i+1}.` {t['title']}" for i, t in enumerate(h[-20:])]

    embed = discord.Embed(
        title="📚 最近播放紀錄",
        description="\n".join(lines),
        color=discord.Color.teal(),
    )
    await interaction.response.send_message(embed=embed)

# ============================================================
# /top
# ============================================================
@tree.command(name="top", description="TOP 10 常播歌曲")
async def top_cmd(interaction: discord.Interaction):
    pc = play_counts.get(interaction.guild_id, {})
    if not pc:
        await interaction.response.send_message("📭 尚無資料")
        return

    items = sorted(pc.items(), key=lambda x: x[1], reverse=True)[:10]
    lines = [f"`{i+1}.` {t}（{c} 次）" for i, (t, c) in enumerate(items)]

    embed = discord.Embed(
        title="🏆 TOP 10",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )
    await interaction.response.send_message(embed=embed)

# ============================================================
# /recommend
# ============================================================
@tree.command(name="recommend", description="依播放次數推薦一首")
async def recommend_cmd(interaction: discord.Interaction):
    import random

    pc = play_counts.get(interaction.guild_id, {})
    if not pc:
        await interaction.response.send_message("📭 尚無紀錄")
        return

    titles = list(pc.keys())
    weights = list(pc.values())
    chosen = random.choices(titles, weights=weights, k=1)[0]

    await interaction.response.send_message(f"🤖 推薦：**{chosen}**")

# ============================================================
# 管理指令（公開版本：任何伺服器都能用，但只有你能成功）
# ============================================================
BOT_OWNER_ID = 477325882881605635  # <<< 這裡用你的 ID

def is_owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id == BOT_OWNER_ID

@tree.command(name="servers", description="顯示 Bot 加入的所有伺服器（僅限 Bot Owner）")
async def servers_cmd(interaction: discord.Interaction):
    if not is_owner(interaction):
        await interaction.response.send_message("❌ 只有 Bot 擁有者能使用此指令。", ephemeral=True)
        return

    guilds = bot.guilds
    if not guilds:
        await interaction.response.send_message("🤖 Bot 未加入任何伺服器。")
        return

    lines = [f"**{g.name}**（ID: `{g.id}`）" for g in guilds]

    embed = discord.Embed(
        title="📋 Bot 所在伺服器列表",
        description="\n".join(lines),
        color=discord.Color.green(),
    )
    await interaction.response.send_message(embed=embed)

# ============================================================
# /servercount（只有你能成功）
# ============================================================
@tree.command(name="servercount", description="顯示 bot 加入的伺服器數量（owner 限定）")
async def servercount_cmd(interaction: discord.Interaction):
    if not is_owner(interaction):
        await interaction.response.send_message("❌ 你不是 Bot Owner。", ephemeral=True)
        return

    await interaction.response.send_message(f"📊 伺服器數量：{len(bot.guilds)}")

# ============================================================
# /stats（只有你能成功）
# ============================================================
@tree.command(name="stats", description="查看所有伺服器當前播放（owner 限定）")
async def stats_cmd(interaction: discord.Interaction):
    if not is_owner(interaction):
        await interaction.response.send_message("❌ 你不是 Bot Owner。", ephemeral=True)
        return

    lines = []
    for g in bot.guilds:
        t = now_playing.get(g.id)
        if t:
            lines.append(f"🎧 **{g.name}**：{t['title']}")
        else:
            lines.append(f"📭 **{g.name}**：無播放")

    embed = discord.Embed(
        title="📊 所有伺服器播放狀態",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )
    await interaction.response.send_message(embed=embed)

# ============================================================
# Bot 啟動
# ============================================================
@bot.event
async def on_ready():
    await tree.sync()
    print(f"🤖 已登入：{bot.user}（ID: {bot.user.id}）")

    if not hasattr(bot, "auto_dc_task"):
        bot.auto_dc_task = bot.loop.create_task(auto_disconnect_loop())

# ============================================================
# 啟動 Bot
# ============================================================
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("❌ 未在 .env 找到 DISCORD_TOKEN")
    bot.run(TOKEN)
