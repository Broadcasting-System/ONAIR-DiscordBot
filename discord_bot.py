"""ONAIR Discord 봇 — #명령(슬래시)과 #chatops(자연어 LLM)로 ONAIR를 제어.
- 실행 시: 역할 6개 생성(관리자/부장/부원, 4/5/6기) + #알림 읽기전용 + 슬래시 명령 동기화.
- #명령: 슬래시 명령(/상태·/tts·/스피커).
- #chatops: 자연어 → Groq LLM function calling → 기능/파라미터 판별 → 부족하면 되묻고, 충분하면 실행.
- 권한: Discord 역할로 게이팅 (관리자·부장=제어, 부원=조회). ONAIR API는 localhost(=admin)로 호출.
- ONAIR 서버와 같은 머신(=tailnet 안)에서 실행 → 127.0.0.1:8000 API에 직접 접근, Discord로는 아웃바운드.
"""
import os
import io
import json
import time
import asyncio
import logging
import unicodedata
from urllib.parse import urlparse
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
ONAIR_BASE = API[:-4] if API.endswith("/api") else API  # http://127.0.0.1:8000 (이미지 fetch용)

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


# ---------------- 공용: 파일 캐시 / 상태 요약 ----------------
_MEDIA_TYPE_KO = {"video": "영상", "image": "이미지", "presentation": "PPT", "audio": "오디오"}
_files_cache = {"ts": 0.0, "data": []}
_default_banner = {"payload": None}  # 마지막으로 본 기본(default) 현수막 payload


def _norm(s: str) -> str:
    """검색 비교용 정규화. 한글 NFD(맥 파일명)/NFC 차이를 흡수하도록 NFC로 통일 + 소문자."""
    return unicodedata.normalize("NFC", s or "").lower()


async def get_files_cached(ttl: float = 5.0) -> list:
    """업로드 파일 목록(자동완성용). 짧은 TTL 캐시로 키 입력마다 서버를 때리지 않게."""
    now = time.time()
    if ttl <= 0 or now - _files_cache["ts"] > ttl or not _files_cache["data"]:
        _files_cache["data"] = (await asyncio.to_thread(api_get, "/files/")) or []
        _files_cache["ts"] = now
    return _files_cache["data"]


def _name_from_id(fid: str) -> str:
    """파일 id에서 표시용 이름. file_<hash>_<name> → <name>."""
    if not fid:
        return "?"
    if fid.startswith("file_"):
        parts = fid.split("_", 2)
        return parts[2] if len(parts) >= 3 else fid
    if fid.startswith("presentation_"):
        return "프레젠테이션"
    return fid


def _display_label(st: dict, by_id: dict) -> str:
    """한 채널의 현재 송출 내용을 사람이 읽을 문자열로."""
    t = st.get("type", "standby")
    if t in ("video", "image", "presentation", "audio"):
        fid = st.get("fileId", "")
        return by_id.get(fid) or _name_from_id(fid)
    if t == "youtube":
        return f"유튜브 {st.get('videoId', '')}"
    if t == "screen":
        return "화면 공유"
    if t == "timer":
        return "타이머"
    return t


def _banner_label(bstate: dict, by_url: dict) -> str:
    scene = bstate.get("scene", "blank")
    payload = bstate.get("payload") or {}
    if scene == "blank":
        return "빈 화면 (없음)"
    if scene == "default":
        main = payload.get("mainText")
        return f"기본 배너 ({main})" if main else "기본 배너"
    if scene in ("image", "gif"):
        url = payload.get("url", "") or ""
        name = by_url.get(url) or by_url.get(url.rsplit("/", 1)[-1]) or _name_from_id(url.rsplit("/", 1)[-1])
        return f"{'이미지' if scene == 'image' else 'GIF'}: {name}"
    if scene == "scoreboard":
        return "스코어보드"
    if scene == "timer":
        return "타이머"
    return scene


def gather_status() -> dict:
    """스피커·송출(파일명)·현수막·시보(그룹)·매트릭스를 한 번에. 블로킹."""
    files = api_get("/files/") or []
    by_id = {f.get("id"): f.get("fileName") for f in files}
    by_url = {}
    for f in files:
        u = f.get("fileUrl") or ""
        if u:
            by_url[u] = f.get("fileName")
            by_url[u.rsplit("/", 1)[-1]] = f.get("fileName")

    active = (api_get("/speakers/status") or {}).get("active_devices", []) or []
    _order = {n: i for i, n in enumerate(load_devices()["names"])}
    active = sorted(active, key=lambda n: _order.get(n, 10 ** 6))  # 매트릭스(교실) 순서대로 정렬
    health = api_get("/display/health") or {}
    matrix_ok = (health.get("matrix") or {}).get("connected")
    live = [c for c, v in (health.get("channels", {}) or {}).items() if v.get("live")]

    sending = []
    for n in range(1, 6):
        st = api_get(f"/display/status?channel={n}") if n > 1 else api_get("/display/status")
        st = st or {}
        if st.get("type", "standby") not in (None, "", "standby"):
            sending.append(f"CH{n}: {_display_label(st, by_id)}")

    bstate = api_get("/banner/state") or {}
    if bstate.get("scene") == "default" and bstate.get("payload"):
        _default_banner["payload"] = bstate["payload"]  # 기본 payload 캐시(복원용)

    sched = api_get("/time/scheduler") or {}
    jobs = sched.get("jobs", []) or []
    grp = sched.get("activeGroupId")

    return {
        "speakers": ", ".join(active) if active else "모두 꺼짐",
        "sending": sending,
        "banner": _banner_label(bstate, by_url),
        "scheduler": (f"그룹 {grp} · {len(jobs)}개 예약" if grp else f"활성 그룹 없음 · {len(jobs)}개 예약"),
        "live": [f"CH{c}" for c in live],
        "matrix": "정상 연결" if matrix_ok else "연결 끊김",
    }


def _fetch_image(url: str):
    """ONAIR 서버에서 이미지 바이트를 가져온다(봇=tailnet 안). 저장된 host는 무시하고 경로만 사용."""
    try:
        path = urlparse(url).path if "://" in (url or "") else (url or "")
        if not path:
            return None
        r = requests.get(ONAIR_BASE + path, timeout=10)
        return r.content if r.ok else None
    except Exception as e:
        log.warning(f"이미지 fetch 실패: {e}")
        return None


def collect_onair_images():
    """지금 송출 중인 이미지 수집 → [(라벨, 바이트, 파일명), ...]. 블로킹."""
    out = []
    bstate = api_get("/banner/state") or {}
    if bstate.get("scene") in ("image", "gif"):
        url = (bstate.get("payload") or {}).get("url", "")
        data = _fetch_image(url)
        if data:
            ext = os.path.splitext(urlparse(url).path)[1] or ".png"
            out.append(("현수막", data, f"banner{ext}"))
    for n in range(1, 6):
        st = api_get(f"/display/status?channel={n}") if n > 1 else api_get("/display/status")
        st = st or {}
        if st.get("type") == "image":
            url = st.get("url", "")
            data = _fetch_image(url)
            if data:
                ext = os.path.splitext(urlparse(url).path)[1] or ".png"
                out.append((f"CH{n}", data, f"ch{n}{ext}"))
    return out


# ---------------- 슬래시 명령 ----------------
@tree.command(name="상태", description="ONAIR 전체 상태 조회 (스피커·송출·현수막·시보·매트릭스)")
async def status_cmd(interaction: discord.Interaction):
    if not await guard(interaction, "부원"):
        return
    await interaction.response.defer(thinking=True)
    s = await asyncio.to_thread(gather_status)
    embed = discord.Embed(title="ONAIR 상태", color=0x3498DB)
    embed.add_field(name="스피커", value=s["speakers"][:1000], inline=False)
    embed.add_field(name="송출 중",
                    value=("\n".join(s["sending"]) if s["sending"] else "없음"), inline=False)
    embed.add_field(name="현수막", value=s["banner"][:1000], inline=False)
    embed.add_field(name="시보", value=s["scheduler"], inline=True)
    embed.add_field(name="매트릭스", value=s["matrix"], inline=True)
    if s["live"]:
        embed.add_field(name="연결된 송출화면", value=", ".join(s["live"]), inline=True)
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
        await interaction.followup.send(f"TTS 송출 완료 · 대상: {', '.join(targets)}\n> {내용}")
    else:
        await interaction.followup.send(f"송출 실패 (HTTP {code})")


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
        await interaction.followup.send(f"스피커 {동작.name} · 대상: {', '.join(targets)}")
    else:
        await interaction.followup.send(f"스피커 제어 실패 (HTTP {code})")


@tree.command(name="미리보기", description="지금 송출 중인 이미지(현수막/채널)를 디코에서 보기")
async def preview_cmd(interaction: discord.Interaction):
    if not await guard(interaction, "부원"):
        return
    await interaction.response.defer(thinking=True)
    imgs = await asyncio.to_thread(collect_onair_images)
    if not imgs:
        await interaction.followup.send("현재 송출 중인 이미지가 없어요.")
        return
    files = [discord.File(io.BytesIO(data), filename=fn) for (_, data, fn) in imgs[:10]]
    labels = ", ".join(lbl for (lbl, _, _) in imgs[:10])
    await interaction.followup.send(f"송출 중 이미지: {labels}", files=files)


def format_schedule() -> str:
    """시보(예약) 목록 텍스트. /시보 슬래시와 ChatOps get_schedule 공용. (블로킹)"""
    sched = api_get("/time/scheduler") or {}
    jobs = sched.get("jobs", []) or []
    if not jobs:
        return "등록된 시보가 없습니다."
    lines = []
    for j in jobs:
        spk = ", ".join(j.get("speakers", [])) or "-"
        lines.append(f"[{j.get('dayLabel','')}] {j.get('time','')}  {j.get('label','')}  —  {spk}")
    grp = sched.get("activeGroupId")
    head = f"시보 목록 ({len(jobs)}개)" + (f" · 활성그룹 {grp}" if grp else "")
    return f"{head}\n```\n" + "\n".join(lines)[:1850] + "\n```"


@tree.command(name="시보", description="등록된 시보(예약) 목록 조회")
async def bell_cmd(interaction: discord.Interaction):
    if not await guard(interaction, "부원"):
        return
    await interaction.response.defer(thinking=True)
    await interaction.followup.send(await asyncio.to_thread(format_schedule))


async def group_autocomplete(interaction: discord.Interaction, current: str):
    """시보 그룹 검색 자동완성 (그룹번호 + 종 개수)."""
    data = ((await asyncio.to_thread(api_get, "/time")) or {}).get("data") or {}
    q = _norm(current)
    out = []
    for gid, v in sorted(data.items()):
        nbells = sum(len(s.get("bells", [])) for s in v.get("schedules", []))
        label = f"그룹 {gid} — 종 {nbells}개" + (" · 특별" if v.get("isSpecialActive") else "")
        if not q or q in _norm(label):
            out.append(app_commands.Choice(name=label[:100], value=gid))
        if len(out) >= 25:
            break
    return out


@tree.command(name="시보그룹", description="송출할 시보 그룹(활성 그룹) 변경 (부장 이상)")
@app_commands.describe(그룹="활성으로 만들 시보 그룹", 특별="특별(P) 모드 (기본: 유지)")
@app_commands.autocomplete(그룹=group_autocomplete)
@app_commands.choices(특별=[
    app_commands.Choice(name="유지", value="keep"),
    app_commands.Choice(name="켜기", value="on"),
    app_commands.Choice(name="끄기", value="off"),
])
async def bell_group_cmd(interaction: discord.Interaction, 그룹: str,
                         특별: app_commands.Choice[str] = None):
    if not await guard(interaction, "부장"):
        return
    await interaction.response.defer(thinking=True)
    data = ((await asyncio.to_thread(api_get, "/time")) or {}).get("data") or {}
    if 그룹 not in data:
        await interaction.followup.send(f"시보 그룹 '{그룹}'을(를) 찾지 못했어요.")
        return
    cur_special = bool(data[그룹].get("isSpecialActive"))
    sp = 특별.value if 특별 else "keep"
    active = cur_special if sp == "keep" else (sp == "on")
    # /time/special 은 해당 그룹을 활성으로 만들고 특별모드도 설정한다(=활성 그룹 전환에 사용)
    code, _ = await asyncio.to_thread(api_post, "/time/special", {"groupId": int(그룹), "active": active})
    if code == 200:
        nbells = sum(len(s.get("bells", [])) for s in data[그룹].get("schedules", []))
        await interaction.followup.send(
            f"시보 활성 그룹: {그룹} (종 {nbells}개, 특별모드 {'켜짐' if active else '꺼짐'})")
    else:
        await interaction.followup.send(f"시보 그룹 변경 실패 (HTTP {code})")


async def media_autocomplete(interaction: discord.Interaction, current: str):
    """파일 이름으로 검색되는 자동완성 (매칭 상위 25개)."""
    q = _norm(current)
    out = []
    for f in await get_files_cached():
        if f.get("type") not in ("video", "image", "presentation"):
            continue
        name = f.get("fileName") or f.get("id") or "?"
        if q and q not in _norm(name):
            continue
        fid = f.get("id") or ""
        if len(fid) > 100:
            continue
        out.append(app_commands.Choice(
            name=f"{name} · {_MEDIA_TYPE_KO.get(f.get('type'), f.get('type'))}"[:100], value=fid))
        if len(out) >= 25:
            break
    return out


@tree.command(name="미디어", description="업로드된 파일을 검색해 송출 (부장 이상)")
@app_commands.describe(파일="파일 이름 검색", 채널="송출 채널 1~5 (기본 1)")
@app_commands.autocomplete(파일=media_autocomplete)
async def media_cmd(interaction: discord.Interaction, 파일: str, 채널: int = 1):
    if not await guard(interaction, "부장"):
        return
    if not 1 <= 채널 <= 5:
        await interaction.response.send_message("채널은 1~5 사이여야 해요.", ephemeral=True)
        return
    await interaction.response.defer()
    f = next((x for x in await get_files_cached(ttl=0) if x.get("id") == 파일), None)
    if not f:
        await interaction.followup.send("파일을 찾지 못했어요. 이름을 입력하고 목록에서 골라줘요.")
        return
    payload = {"type": f.get("type"), "fileId": f.get("id"),
               "url": f.get("fileUrl"), "hlsUrl": f.get("hlsUrl"), "urls": f.get("urls")}
    code, _ = await asyncio.to_thread(api_post, f"/display/show?channel={채널}", payload)
    name = f.get("fileName") or f.get("id")
    await interaction.followup.send(
        f"송출: {name} (채널 {채널})" if code == 200 else f"송출 실패 (HTTP {code})")


async def banner_autocomplete(interaction: discord.Interaction, current: str):
    """빈화면/기본 + 업로드 이미지 검색 자동완성."""
    q = _norm(current)
    out = []
    for label, val in (("빈 화면 (끄기)", "__clear__"), ("기본 배너", "__default__")):
        if not q or q in _norm(label):
            out.append(app_commands.Choice(name=label, value=val))
    for f in await get_files_cached():
        if f.get("type") != "image":
            continue
        name = f.get("fileName") or f.get("id") or "?"
        if q and q not in _norm(name):
            continue
        fid = f.get("id") or ""
        if len(fid) > 100:
            continue
        out.append(app_commands.Choice(name=f"이미지: {name}"[:100], value=fid))
        if len(out) >= 25:
            break
    return out


@tree.command(name="현수막", description="현수막 변경: 빈화면/기본/업로드 이미지 검색 (부장 이상)")
@app_commands.describe(대상="빈화면 / 기본 / 이미지 이름 검색")
@app_commands.autocomplete(대상=banner_autocomplete)
async def banner_cmd(interaction: discord.Interaction, 대상: str):
    if not await guard(interaction, "부장"):
        return
    await interaction.response.defer()
    if 대상 == "__clear__":
        code, _ = await asyncio.to_thread(api_post, "/banner/clear", None)
        msg = "현수막 끔 (빈 화면)"
    elif 대상 == "__default__":
        if _default_banner["payload"] is None:
            st = (await asyncio.to_thread(api_get, "/banner/state")) or {}
            if st.get("scene") == "default" and st.get("payload"):
                _default_banner["payload"] = st["payload"]
        code, _ = await asyncio.to_thread(
            api_post, "/banner/update", {"scene": "default", "payload": _default_banner["payload"] or {}})
        msg = "기본 배너로 전환"
    else:
        f = next((x for x in await get_files_cached(ttl=0) if x.get("id") == 대상), None)
        if not f:
            await interaction.followup.send("이미지를 찾지 못했어요. 이름을 입력하고 목록에서 골라줘요.")
            return
        code, _ = await asyncio.to_thread(
            api_post, "/banner/update", {"scene": "image", "payload": {"url": f.get("fileUrl"), "fit": "cover"}})
        msg = f"현수막: {f.get('fileName') or f.get('id')}"
    await interaction.followup.send(msg if code == 200 else f"현수막 변경 실패 (HTTP {code})")


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
        "description": "ONAIR 전체 상태(스피커/송출/현수막/시보 요약/매트릭스)를 조회한다.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_schedule",
        "description": "등록된 시보(예약) 목록을 요일·시간·이름·대상까지 자세히 조회한다.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "show_images",
        "description": "지금 송출 중인 이미지(현수막 이미지, 채널에 띄운 이미지)를 디코에 사진으로 보여준다.",
        "parameters": {"type": "object", "properties": {}},
    }},
]

TOOL_TIER = {
    "control_speaker": "부장", "broadcast_tts": "부장",
    "get_status": "부원", "get_schedule": "부원", "show_images": "부원",
}


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
        "- 방송(TTS)인데 문구나 대상이 없으면 되묻는다.\n"
        "- 되물을 때는 반드시 물음표(?)로 끝나는 짧은 한 문장으로만 답한다.\n\n"
        "[실행]\n"
        "- 대상과 동작(켜기/끄기)이 모두 확정되면 곧바로 도구를 호출한다.\n"
        "- 조회 요청에 '시보'라는 단어가 들어가면(시보 현황/목록/예약/스케줄/일정/몇 시에 등) 반드시 get_schedule을 호출한다. 이때 get_status는 쓰지 않는다.\n"
        "- '사진/이미지/미리보기 보여줘', '지금 송출/현수막 이미지 보여줘'처럼 이미지를 보여달라면 show_images를 호출한다.\n"
        "- 그 외 전반적인 상태/현황(스피커·송출·현수막 등)은 get_status를 호출한다.\n"
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
    """get_status 도구용 상태 요약(텍스트, 이모지 없음)."""
    s = gather_status()
    return "\n".join([
        "ONAIR 상태",
        f"스피커: {s['speakers']}",
        f"송출 중: {'; '.join(s['sending']) if s['sending'] else '없음'}",
        f"현수막: {s['banner']}",
        f"시보: {s['scheduler']}",
        f"매트릭스: {s['matrix']}",
    ])


def run_tool(name: str, args: dict, tier: int):
    """도구 실행(권한 확인 포함). 문자열 또는 {'text','images'} 반환. 블로킹."""
    need = TOOL_TIER.get(name, "관리자")
    if tier < TIER_ORDER[need]:
        return f"권한 부족 — 이 작업은 {need} 이상이 필요해요."

    if name == "control_speaker":
        targets = [t for t in (args.get("targets") or []) if t]
        action = args.get("action")
        if not targets or action not in ("on", "off"):
            return "대상이나 동작(켜기/끄기)이 불명확해요. 다시 알려줄래요?"
        code, _ = api_post("/speakers/control", {"targets": targets, "action": action})
        if code == 200:
            return f"스피커 {'켜기' if action == 'on' else '끄기'} · {', '.join(targets)}"
        return f"스피커 제어 실패 (HTTP {code})"

    if name == "broadcast_tts":
        text = (args.get("text") or "").strip()
        targets = [t for t in (args.get("targets") or []) if t]
        if not text or not targets:
            return "방송할 내용이나 대상이 빠졌어요."
        code, _ = api_post("/broadcast/execute", {
            "sourceType": "tts", "sourceId": text, "targets": targets, "restoreState": True})
        if code == 200:
            return f"TTS 송출 · 대상 {', '.join(targets)}\n> {text}"
        return f"송출 실패 (HTTP {code})"

    if name == "get_status":
        return _chatops_status()

    if name == "get_schedule":
        return format_schedule()

    if name == "show_images":
        imgs = collect_onair_images()
        if not imgs:
            return "현재 송출 중인 이미지가 없어요."
        labels = ", ".join(l for l, _, _ in imgs)
        return {"text": f"송출 중 이미지: {labels}", "images": [(b, fn) for _, b, fn in imgs]}

    return f"알 수 없는 작업: {name}"


def handle_chatops(convo: list, tier: int) -> dict:
    """대화(convo=[{role,content}...]) → LLM 판별 → 실행/되물음.
    반환: {"text": str, "images": [(bytes, filename), ...]}. 블로킹."""
    messages = [{"role": "system", "content": build_system_prompt()}] + convo
    msg = groq_chat(messages)
    calls = msg.get("tool_calls") or []
    if not calls:
        return {"text": (msg.get("content") or "무슨 작업을 할까요?").strip(), "images": []}
    texts, images = [], []
    for tc in calls:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            args = {}
        res = run_tool(fn.get("name", ""), args, tier)
        if isinstance(res, dict):
            if res.get("text"):
                texts.append(res["text"])
            images.extend(res.get("images", []))
        else:
            texts.append(res)
    return {"text": "\n".join(t for t in texts if t), "images": images}


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
        await message.channel.send("ChatOps는 부원 이상만 사용할 수 있어요.")
        return

    # 되물음 연속성을 위해 최근 대화를 문맥으로 포함(시간순, user로 시작).
    # 봇 메시지는 '되묻는 질문'(물음표로 끝남)만 포함 — 실행결과/상태 메시지 속 스피커 이름 등이
    # 다음 명령의 대상으로 새는 것을 방지.
    convo = []
    async for m in message.channel.history(limit=8):
        c = (m.content or "").strip()
        if not c:
            continue
        if m.author.bot and not c.endswith("?"):
            continue
        convo.append({"role": "assistant" if m.author.bot else "user", "content": c})
    convo.reverse()
    while convo and convo[0]["role"] == "assistant":
        convo.pop(0)

    try:
        async with message.channel.typing():
            result = await asyncio.to_thread(handle_chatops, convo, tier)
    except Exception as e:
        log.warning(f"ChatOps 오류: {e}")
        result = {"text": f"처리 중 오류가 났어요: {e}", "images": []}
    text = (result.get("text") or "")[:1900]
    files = [discord.File(io.BytesIO(b), filename=fn) for (b, fn) in (result.get("images") or [])[:10]]
    if text or files:
        await message.channel.send(content=text or None, files=files)


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
        log.info(f"ChatOps 활성화 (#{CHATOPS_CHANNEL}, 모델={GROQ_MODEL})")
    else:
        log.info("ChatOps 비활성 (GROQ_API_KEY 없음 → 슬래시 명령만)")
    log.info(f"봇 로그인: {client.user}")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN이 .env에 없습니다.")
    client.run(TOKEN)
