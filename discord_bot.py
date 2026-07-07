"""ONAIR Discord 봇 — #명령 채널에서 슬래시 명령으로 ONAIR를 제어.
- 실행 시: 역할 6개 생성(관리자/부장/부원, 4/5/6기) + #알림 읽기전용 + 슬래시 명령 동기화.
- 권한: Discord 역할로 게이팅 (관리자·부장=제어, 부원=조회). ONAIR API는 localhost(=admin)로 호출.
- ONAIR 서버와 같은 머신(=tailnet 안)에서 실행 → 127.0.0.1:8000 API에 직접 접근, Discord로는 아웃바운드.
"""
import os
import logging
import requests
import discord
from discord import app_commands

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("onair-bot")


def _load_env(path: str):
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID", "0") or 0)
API = os.environ.get("ONAIR_API", "http://127.0.0.1:8000/api").rstrip("/")

CMD_CHANNEL = "명령"
ALERT_CHANNEL = "알림"
TIER_ROLES = ["관리자", "부장", "부원"]
COHORT_ROLES = ["4기", "5기", "6기"]
TIER_ORDER = {"부원": 0, "부장": 1, "관리자": 2}
ROLE_COLORS = {
    "관리자": discord.Color.red(), "부장": discord.Color.blue(), "부원": discord.Color.greyple(),
    "4기": discord.Color.orange(), "5기": discord.Color.green(), "6기": discord.Color.purple(),
}

intents = discord.Intents.default()  # 슬래시 명령엔 특권 인텐트 불필요
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


# ---------------- ONAIR API ----------------
def api_get(path: str):
    try:
        r = requests.get(f"{API}{path}", timeout=6)
        return r.json() if r.ok else None
    except Exception as e:
        log.warning(f"API GET 실패 {path}: {e}")
        return None


def api_post(path: str, json=None):
    try:
        r = requests.post(f"{API}{path}", json=json, timeout=10)
        return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else {})
    except Exception as e:
        log.warning(f"API POST 실패 {path}: {e}")
        return 0, {}


# ---------------- 권한 게이팅 ----------------
def user_tier(member) -> int:
    names = {r.name for r in getattr(member, "roles", [])}
    return max((TIER_ORDER[n] for n in names if n in TIER_ORDER), default=-1)


async def guard(interaction: discord.Interaction, min_tier: str) -> bool:
    """#명령 채널 + 역할 검사. 실패 시 ephemeral 안내 후 False."""
    if getattr(interaction.channel, "name", None) != CMD_CHANNEL:
        await interaction.response.send_message(
            f"이 명령은 **#{CMD_CHANNEL}** 채널에서만 사용할 수 있어요.", ephemeral=True)
        return False
    if user_tier(interaction.user) < TIER_ORDER[min_tier]:
        await interaction.response.send_message(
            f"권한이 부족합니다. **{min_tier}** 이상 역할이 필요해요.", ephemeral=True)
        return False
    return True


# ---------------- 슬래시 명령 ----------------
@tree.command(name="상태", description="ONAIR 전체 상태 조회 (스피커·송출·현수막·시보·매트릭스)")
async def status_cmd(interaction: discord.Interaction):
    if not await guard(interaction, "부원"):
        return
    await interaction.response.defer(thinking=True)

    sp = api_get("/speakers/status") or {}
    active = sp.get("active_devices", []) or []
    health = api_get("/display/health") or {}
    channels = health.get("channels", {}) or {}
    matrix = health.get("matrix", {}) or {}

    media = []
    for n in range(1, 6):
        st = api_get(f"/display/status?channel={n}") if n > 1 else api_get("/display/status")
        t = (st or {}).get("type", "standby")
        if t and t != "standby":
            media.append(f"CH{n}: {t}")

    banner = api_get("/banner/state") or {}
    scene = banner.get("scene", "blank")
    sched = api_get("/time/scheduler") or {}
    bells = sched.get("jobs", []) or []
    active_group = sched.get("activeGroupId")

    live_ch = [c for c, v in channels.items() if v.get("live")]

    embed = discord.Embed(title="📊 ONAIR 상태", color=0x3498DB)
    embed.add_field(name="🔊 켜진 스피커",
                    value=(", ".join(active) if active else "모두 꺼짐")[:1000], inline=False)
    embed.add_field(name="🎥 송출 중", value=("\n".join(media) if media else "없음"), inline=True)
    embed.add_field(name="🖼️ 현수막", value=scene, inline=True)
    embed.add_field(name="⏰ 시보",
                    value=f"{len(bells)}개 예약" + (f" (그룹 {active_group})" if active_group else ""), inline=True)
    embed.add_field(name="🟢 송출화면",
                    value=(f"라이브: {', '.join('CH'+c for c in live_ch)}" if live_ch else "연결된 화면 없음"),
                    inline=False)
    embed.add_field(name="🎛️ 스피커 매트릭스",
                    value=("정상 연결" if matrix.get("connected") else "⚠️ 연결 끊김"), inline=True)
    await interaction.followup.send(embed=embed)


@tree.command(name="tts", description="TTS 방송 송출 (부장 이상)")
@app_commands.describe(내용="방송할 텍스트", 대상="스피커 대상 (예: 전체, 3-2, 콤마로 여러개)")
async def tts_cmd(interaction: discord.Interaction, 내용: str, 대상: str = "전체"):
    if not await guard(interaction, "부장"):
        return
    await interaction.response.defer(thinking=True)
    targets = [t.strip() for t in 대상.split(",") if t.strip()]
    code, _ = api_post("/broadcast/execute", {
        "sourceType": "tts", "sourceId": 내용, "targets": targets, "restoreState": True,
    })
    if code == 200:
        await interaction.followup.send(f"🔴 TTS 송출 완료 · 대상: {', '.join(targets)}\n> {내용}")
    else:
        await interaction.followup.send(f"❌ 송출 실패 (HTTP {code})")


@tree.command(name="스피커", description="스피커 ON/OFF (부장 이상)")
@app_commands.describe(대상="스피커 대상 (예: 전체, 3-2, 콤마로 여러개)", 동작="켜기 또는 끄기")
@app_commands.choices(동작=[
    app_commands.Choice(name="켜기", value="on"),
    app_commands.Choice(name="끄기", value="off"),
])
async def speaker_cmd(interaction: discord.Interaction, 대상: str, 동작: app_commands.Choice[str]):
    if not await guard(interaction, "부장"):
        return
    await interaction.response.defer(thinking=True)
    targets = [t.strip() for t in 대상.split(",") if t.strip()]
    code, data = api_post("/speakers/control", {"targets": targets, "action": 동작.value})
    if code == 200 and data.get("success", True):
        emoji = "🔊" if 동작.value == "on" else "🔇"
        await interaction.followup.send(f"{emoji} 스피커 {동작.name} · 대상: {', '.join(targets)}")
    else:
        await interaction.followup.send(f"❌ 스피커 제어 실패 (HTTP {code})")


# ---------------- 서버 초기 설정 ----------------
async def ensure_roles(guild: discord.Guild):
    existing = {r.name for r in guild.roles}
    for name in TIER_ROLES + COHORT_ROLES:
        if name not in existing:
            try:
                await guild.create_role(name=name, colour=ROLE_COLORS.get(name, discord.Color.default()),
                                        mentionable=True, reason="ONAIR 봇 초기 역할 생성")
                log.info(f"역할 생성: {name}")
            except Exception as e:
                log.warning(f"역할 생성 실패 {name}: {e}")


async def lock_alert_channel(guild: discord.Guild):
    ch = discord.utils.get(guild.text_channels, name=ALERT_CHANNEL)
    if ch:
        try:
            await ch.set_permissions(guild.default_role, send_messages=False,
                                     reason="알림 채널 읽기전용(봇/웹훅만 게시)")
            log.info(f"#{ALERT_CHANNEL} 읽기전용 설정 완료")
        except Exception as e:
            log.warning(f"알림 채널 잠금 실패: {e}")


@client.event
async def on_ready():
    guild = client.get_guild(GUILD_ID) or (client.guilds[0] if client.guilds else None)
    if guild:
        await ensure_roles(guild)
        await lock_alert_channel(guild)
        tree.copy_global_to(guild=discord.Object(id=guild.id))
        await tree.sync(guild=discord.Object(id=guild.id))
        log.info(f"슬래시 명령 동기화 완료 (guild={guild.name})")
    log.info(f"✅ 봇 로그인: {client.user}")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN이 .env에 없습니다.")
    client.run(TOKEN)
