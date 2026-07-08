"""ONAIR Discord 봇 — #명령(슬래시)과 #chatops(자연어 LLM)로 ONAIR를 제어.
- 실행 시: 역할 6개 생성(관리자/부장/부원, 4/5/6기) + #알림 읽기전용 + 슬래시 명령 동기화.
- #명령: 슬래시 명령(/상태·/tts·/스피커).
- #chatops: 자연어 → Groq LLM function calling → 기능/파라미터 판별 → 부족하면 되묻고, 충분하면 실행.
- 권한: Discord 역할로 게이팅 (관리자·부장=제어, 부원=조회). ONAIR API는 localhost(=admin)로 호출.
- ONAIR 서버와 같은 머신(=tailnet 안)에서 실행 → 127.0.0.1:8000 API에 직접 접근, Discord로는 아웃바운드.
"""
import os
import json
import asyncio
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

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

CMD_CHANNEL = "명령"
ALERT_CHANNEL = "알림"
CHATOPS_CHANNEL = "chatops"
TIER_ROLES = ["관리자", "부장", "부원"]
COHORT_ROLES = ["4기", "5기", "6기"]
TIER_ORDER = {"부원": 0, "부장": 1, "관리자": 2}
ROLE_COLORS = {
    "관리자": discord.Color.red(), "부장": discord.Color.blue(), "부원": discord.Color.greyple(),
    "4기": discord.Color.orange(), "5기": discord.Color.green(), "6기": discord.Color.purple(),
}

intents = discord.Intents.default()
# #chatops 자연어 처리에는 메시지 본문(privileged intent)이 필요.
# GROQ 키가 있을 때만 켜서, ChatOps 미사용 시엔 개발자 포털 토글 없이도 봇이 뜬다.
if GROQ_API_KEY:
    intents.message_content = True
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


# ---------------- ChatOps (#chatops, 자연어 LLM) ----------------
# 스피커 대상 접지용 목록. 서버 /speakers/devices에서 받아오되 실패 시 폴백 사용(서버 미배포 대비).
_FALLBACK_DEVICES = [
    "1-1", "1-2", "1-3", "1-4", "2-1", "2-2", "2-3", "2-4", "3-1", "3-2", "3-3", "3-4",
    "교행연회", "교사연구", "협동조합", "보건/학", "컴퓨터12", "과학준비", "창의준비", "남여휴게",
    "교무실", "학생식당", "위클/회", "프로그12", "교무2지", "진로연구", "영어/모", "창의공작",
    "B1층복도", "A1층복도", "B2층복도", "A2층복도", "A3층복도", "강당", "방송실",
    "SRC1-1", "SRC1-2", "SRC1-3", "SRC2-1", "창의관", "운동장", "옥외",
]
_FALLBACK_GRADES = ["1학년", "2학년", "3학년"]
_devices_cache = {"names": None, "grades": None}


def load_devices():
    """스피커 이름/학년그룹을 서버에서 1회 로드해 캐시. 실패 시 폴백."""
    if _devices_cache["names"] is None:
        data = api_get("/speakers/devices") or {}
        _devices_cache["names"] = data.get("devices") or _FALLBACK_DEVICES
        _devices_cache["grades"] = data.get("grades") or _FALLBACK_GRADES
    return _devices_cache


CHATOPS_TOOLS = [
    {"type": "function", "function": {
        "name": "control_speaker",
        "description": "교실/구역 스피커를 켜거나 끈다.",
        "parameters": {"type": "object", "properties": {
            "targets": {"type": "array", "items": {"type": "string"},
                        "description": "대상 이름 목록. 교실은 '학년-반'(3학년 2반→'3-2'). 전체는 '전체', 학년 전체는 'N학년'."},
            "action": {"type": "string", "enum": ["on", "off"], "description": "켜기(on)/끄기(off)"},
        }, "required": ["targets", "action"]},
    }},
    {"type": "function", "function": {
        "name": "broadcast_tts",
        "description": "TTS로 문장을 스피커에 방송한다.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "방송할 내용"},
            "targets": {"type": "array", "items": {"type": "string"}, "description": "대상. 전체는 '전체'."},
        }, "required": ["text", "targets"]},
    }},
    {"type": "function", "function": {
        "name": "get_status",
        "description": "ONAIR 전체 상태(스피커/송출/현수막/시보/매트릭스)를 조회한다.",
        "parameters": {"type": "object", "properties": {}},
    }},
]

TOOL_TIER = {"control_speaker": "부장", "broadcast_tts": "부장", "get_status": "부원"}


def build_system_prompt() -> str:
    d = load_devices()
    return (
        "너는 학교 방송 시스템 ONAIR의 제어 비서다. 사용자의 한국어 요청을 도구(함수) 호출로 바꾼다.\n"
        f"유효한 스피커 이름: {', '.join(d['names'])}\n"
        f"그룹 키워드: 전체(모든 스피커), {', '.join(d['grades'])}.\n\n"
        "[변환 규칙]\n"
        "- 'N학년 M반'은 'N-M'으로 변환한다 (예: 3학년 2반 → 3-2).\n"
        "- 사용자가 '전체/모두/전교/전부/다 켜/다 꺼'처럼 명시적으로 전체를 말한 경우에만 '전체'로 처리한다.\n\n"
        "[되물어야 하는 경우 — 도구를 호출하지 말고 짧은 한국어로 질문한다]\n"
        "- 대상이 아예 없을 때(\"꺼줘\", \"켜줘\")는 반드시 어디인지 되묻는다. 절대 '전체'로 가정하지 않는다.\n"
        "- 대상이 애매할 때(\"3반 켜줘\"처럼 학년이 빠짐)는 몇 학년인지 되묻는다.\n"
        "- 유효 이름/그룹에 없는 대상이면 임의로 만들지 말고 되묻는다.\n"
        "- 방송(TTS)인데 문구나 대상이 없으면 되묻는다.\n\n"
        "[실행]\n"
        "- 대상과 동작(켜기/끄기)이 모두 확정되면 곧바로 도구를 호출한다.\n"
        "- 상태/현황 질문은 get_status를 호출한다.\n"
        "간결한 한국어로 답한다."
    )


def groq_chat(messages: list) -> dict:
    """Groq chat completions 호출 → assistant 메시지(dict) 반환. (블로킹)"""
    r = requests.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        json={"model": GROQ_MODEL, "messages": messages, "tools": CHATOPS_TOOLS,
              "tool_choice": "auto", "temperature": 0.1},
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"Groq {r.status_code}: {r.text[:200]}")
    return r.json()["choices"][0]["message"]


def _chatops_status() -> str:
    """get_status 도구용 짧은 상태 요약(텍스트)."""
    active = (api_get("/speakers/status") or {}).get("active_devices", []) or []
    health = api_get("/display/health") or {}
    matrix = (health.get("matrix") or {}).get("connected")
    live = [c for c, v in (health.get("channels", {}) or {}).items() if v.get("live")]
    banner = (api_get("/banner/state") or {}).get("scene", "blank")
    bells = len((api_get("/time/scheduler") or {}).get("jobs", []) or [])
    return "📊 ONAIR 상태\n" + "\n".join([
        f"🔊 켜진 스피커: {', '.join(active) if active else '없음'}",
        f"🖼️ 현수막: {banner}  ·  ⏰ 시보 예약: {bells}개",
        f"🟢 송출화면: {('CH' + ', CH'.join(live)) if live else '없음'}",
        f"🎛️ 매트릭스: {'정상' if matrix else '⚠️ 끊김'}",
    ])


def run_tool(name: str, args: dict, tier: int) -> str:
    """도구 실행(권한 확인 포함). 블로킹(스레드에서 호출)."""
    need = TOOL_TIER.get(name, "관리자")
    if tier < TIER_ORDER[need]:
        return f"⛔ 권한 부족 — 이 작업은 **{need}** 이상이 필요해요."

    if name == "control_speaker":
        targets = [t for t in (args.get("targets") or []) if t]
        action = args.get("action")
        if not targets or action not in ("on", "off"):
            return "대상이나 동작(켜기/끄기)이 불명확해요. 다시 알려줄래요?"
        code, _ = api_post("/speakers/control", {"targets": targets, "action": action})
        if code == 200:
            emoji = "🔊" if action == "on" else "🔇"
            return f"{emoji} 스피커 {'켜기' if action == 'on' else '끄기'} · {', '.join(targets)}"
        return f"❌ 스피커 제어 실패 (HTTP {code})"

    if name == "broadcast_tts":
        text = (args.get("text") or "").strip()
        targets = [t for t in (args.get("targets") or []) if t]
        if not text or not targets:
            return "방송할 내용이나 대상이 빠졌어요."
        code, _ = api_post("/broadcast/execute", {
            "sourceType": "tts", "sourceId": text, "targets": targets, "restoreState": True})
        if code == 200:
            return f"🔴 TTS 송출 · 대상 {', '.join(targets)}\n> {text}"
        return f"❌ 송출 실패 (HTTP {code})"

    if name == "get_status":
        return _chatops_status()

    return f"알 수 없는 작업: {name}"


def handle_chatops(convo: list, tier: int) -> str:
    """대화(convo=[{role,content}...]) → LLM 판별 → 실행/되물음. 블로킹."""
    messages = [{"role": "system", "content": build_system_prompt()}] + convo
    msg = groq_chat(messages)
    calls = msg.get("tool_calls") or []
    if not calls:
        return (msg.get("content") or "무슨 작업을 할까요?").strip()
    out = []
    for tc in calls:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            args = {}
        out.append(run_tool(fn.get("name", ""), args, tier))
    return "\n".join(out)


@client.event
async def on_message(message: discord.Message):
    if message.author.bot or not GROQ_API_KEY:
        return
    if getattr(message.channel, "name", None) != CHATOPS_CHANNEL:
        return
    content = (message.content or "").strip()
    if not content:
        return
    tier = user_tier(message.author)
    if tier < TIER_ORDER["부원"]:
        await message.channel.send("⛔ ChatOps는 **부원** 이상만 사용할 수 있어요.")
        return

    # 되물음 연속성을 위해 최근 대화 몇 개를 문맥으로 포함(시간순, user로 시작).
    convo = []
    async for m in message.channel.history(limit=8):
        c = (m.content or "").strip()
        if c:
            convo.append({"role": "assistant" if m.author.bot else "user", "content": c})
    convo.reverse()
    while convo and convo[0]["role"] == "assistant":
        convo.pop(0)

    try:
        async with message.channel.typing():
            reply = await asyncio.to_thread(handle_chatops, convo, tier)
    except Exception as e:
        log.warning(f"ChatOps 오류: {e}")
        reply = f"⚠️ 처리 중 오류가 났어요: {e}"
    await message.channel.send(reply[:1900])


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
    if GROQ_API_KEY:
        log.info(f"🤖 ChatOps 활성화 (#{CHATOPS_CHANNEL}, 모델={GROQ_MODEL})")
    else:
        log.info("🤖 ChatOps 비활성 (GROQ_API_KEY 없음 → 슬래시 명령만)")
    log.info(f"✅ 봇 로그인: {client.user}")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN이 .env에 없습니다.")
    client.run(TOKEN)
