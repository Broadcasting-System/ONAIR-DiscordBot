"""말로 하는 조작(ONAIR /api/assist) — 디스코드와 상관없는 작은 함수들. tests/test_assist_client.py 가 검사한다.

흐름: 메시지 → POST /assist/plan → 확인이 필요 없으면 바로 /assist/execute,
필요하면 [실행] [취소] 버튼(요청한 사람만, 2분) → /assist/execute → 결과 답장.
"""
import os

import requests

# 봇 출력은 이모지 없이 (README 규칙)
OK_MARK = "완료"
FAIL_MARK = "실패"
CONFIRM_TIMEOUT_S = 120
PLAN_TIMEOUT_S = 8
EXEC_TIMEOUT_S = 20
MAX_REPLY = 1900
TIER_NAMES = ("관리자", "부장", "부원")


def env_flag(name: str, default: bool = False) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on", "켜기")


def channel_names(raw: str) -> set:
    """ASSIST_CHANNELS='명령,방송실' → {'명령', '방송실'}"""
    return {c.strip().lstrip("#") for c in (raw or "").split(",") if c.strip()}


def api_headers(env=None) -> dict:
    """ONAIR API 요청에 붙일 헤더. Cloudflare Access 서비스 토큰이 있으면 같이 보낸다."""
    env = os.environ if env is None else env
    h = {}
    cid, secret = (env.get("CF_ACCESS_CLIENT_ID") or "").strip(), (env.get("CF_ACCESS_CLIENT_SECRET") or "").strip()
    if cid and secret:
        h["CF-Access-Client-Id"] = cid
        h["CF-Access-Client-Secret"] = secret
    return h


def top_tier(role_names) -> str:
    """디스코드 역할 이름들 중 가장 높은 등급 (관리자 > 부장 > 부원). 없으면 ''."""
    names = set(role_names)
    return next((t for t in TIER_NAMES if t in names), "")


def requester_of(user_id, display_name: str, role_names) -> dict:
    return {"discordId": str(user_id), "name": display_name or "", "discordRole": top_tier(role_names)}


def should_handle(content: str) -> bool:
    """봇이 볼 메시지인지 — 빈 말, 슬래시·느낌표 명령, 너무 긴 글은 넘긴다."""
    t = (content or "").strip()
    return bool(t) and not t.startswith(("/", "!")) and len(t) <= 300


def strip_mention(content: str, bot_id) -> str:
    t = content or ""
    for m in (f"<@{bot_id}>", f"<@!{bot_id}>"):
        t = t.replace(m, " ")
    return " ".join(t.split())


def is_quiet(plan: dict) -> bool:
    """ONAIR 와 상관없는 잡담 — 봇을 부르지 않았으면 대답하지 않는다.
    조작하려는 말인데 공간·대상이 애매한 것(kind=ambiguous)은 되묻는다."""
    if plan.get("actions") or plan.get("refused"):
        return False
    return all(u.get("kind", "unknown") == "unknown" for u in plan.get("unresolved", []))


def _lines(items, prefix="- "):
    return [f"{prefix}{x}" for x in items]


def format_plan(plan: dict) -> str:
    """확인을 받을 때 보여 줄 글."""
    out = ["이렇게 할까요?"]
    out += _lines(a["label"] for a in plan.get("actions", []))
    for u in plan.get("unresolved", []):
        out.append(f"- (못 알아들음) {u['reason']}")
    for r in plan.get("refused", []):
        out.append(f"- (거절) {r['reason']}")
    reasons = plan.get("confirmReasons") or []
    if reasons:
        out.append("")
        out.append("확인이 필요한 이유")
        out += _lines(reasons, "· ")
    if plan.get("denied"):
        out.append("")
        out.append("권한이 없어 실행되지 않을 것: " + ", ".join(plan["denied"]))
    out.append("")
    out.append(f"요청한 사람만 누를 수 있어요. {CONFIRM_TIMEOUT_S // 60}분 뒤 사라집니다.")
    return "\n".join(out)[:MAX_REPLY]


def format_not_understood(plan: dict) -> str:
    out = []
    for r in plan.get("refused", []):
        out.append(r["reason"])
    for u in plan.get("unresolved", []):
        out.append(u["reason"])
    return ("\n".join(out) or "무슨 작업인지 모르겠어요. 예) 강당 조회 모드로 바꿔줘, 1번 마이크 꺼")[:MAX_REPLY]


def format_results(res: dict, plan: dict | None = None) -> str:
    """'완료: 강당 '행사' 씬, 강당 MIC 1 70→80' + 실패한 것 + 못 알아들은 것."""
    results = res.get("results") or []
    ok = [r["message"] for r in results if r.get("ok") and r.get("action") != "status_query"]
    status = [r["message"] for r in results if r.get("ok") and r.get("action") == "status_query"]
    bad = [r["message"] for r in results if not r.get("ok")]
    out = []
    if ok:
        out.append(f"{OK_MARK}: " + ", ".join(ok))
    out += status  # 상태 확인은 그대로 보여 준다
    for b in bad:
        out.append(f"{FAIL_MARK}: {b}")
    if plan:
        for u in plan.get("unresolved", []):
            out.append(f"못 알아들음: {u['reason']}")
        for r in plan.get("refused", []):
            out.append(f"거절: {r['reason']}")
    return ("\n".join(out) or "할 일이 없었어요.")[:MAX_REPLY]


def can_press(presser_id, requester_id) -> bool:
    return str(presser_id) == str(requester_id)


def fail_text(what: str, code: int, data) -> str:
    detail = data.get("detail") if isinstance(data, dict) else None
    if isinstance(detail, str) and detail:
        return f"{what} 실패 — {detail}"
    if code == 0:
        return f"{what} 실패 — ONAIR 서버에 연결하지 못했어요"
    if code == 403:
        return f"{what} 실패 — 봇 PC 에 ONAIR 조작 권한이 없어요 (HTTP 403)"
    return f"{what} 실패 (HTTP {code})"


# ---------------- HTTP (블로킹 — 봇에서는 asyncio.to_thread 로 부른다) ----------------

def post(api: str, path: str, body: dict, timeout: float, headers: dict | None = None, session=None):
    """(상태코드, JSON). 연결 실패는 (0, {})."""
    s = session or requests
    try:
        r = s.post(f"{api}{path}", json=body, timeout=timeout, headers=headers or {})
        ctype = r.headers.get("content-type", "")
        return r.status_code, (r.json() if ctype.startswith("application/json") else {})
    except Exception:
        return 0, {}


def plan(api: str, text: str, requester: dict, headers: dict | None = None, session=None):
    return post(api, "/assist/plan", {"text": text, "requester": requester}, PLAN_TIMEOUT_S, headers, session)


def execute(api: str, plan_id: str, requester: dict, headers: dict | None = None, session=None):
    return post(api, "/assist/execute", {"planId": plan_id, "requester": requester}, EXEC_TIMEOUT_S, headers, session)
