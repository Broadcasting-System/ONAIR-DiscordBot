"""말로 하는 조작 — 봇 쪽 작은 함수들 (디스코드 없이)."""

import assist_client as assist

PLAN = {
    "planId": "p1",
    "actions": [{"label": "강당 믹서 씬 → '조회'"}, {"label": "강당 MIC 1 끄기(음소거)"}],
    "unresolved": [{"reason": "어떤 씬인가요?", "kind": "ambiguous"}],
    "refused": [],
    "confirmReasons": ["강당 MIC 1 끄기(음소거) — 확실하지 않음 (60%)"],
    "denied": [],
    "needsConfirm": True,
}


def test_설정값_읽기():
    assert assist.env_flag("X_NOT_SET_ANYWHERE") is False
    assert assist.channel_names("명령, #방송실,,") == {"명령", "방송실"}
    assert assist.api_headers({}) == {}
    assert assist.api_headers({"CF_ACCESS_CLIENT_ID": "id", "CF_ACCESS_CLIENT_SECRET": "s"}) == {
        "CF-Access-Client-Id": "id", "CF-Access-Client-Secret": "s"}
    assert assist.api_headers({"CF_ACCESS_CLIENT_ID": "id"}) == {}  # 반쪽이면 안 보낸다


def test_요청한_사람_정보와_가장_높은_역할():
    r = assist.requester_of(123, "김부장", ["5기", "부원", "부장"])
    assert r == {"discordId": "123", "name": "김부장", "discordRole": "부장"}
    assert assist.top_tier(["6기"]) == ""


def test_볼_메시지만_본다():
    assert assist.should_handle("강당 조회 모드로 바꿔줘")
    assert not assist.should_handle("  ")
    assert not assist.should_handle("/상태")
    assert not assist.should_handle("가" * 301)
    assert assist.strip_mention("<@42> 1번 마이크 꺼", 42) == "1번 마이크 꺼"
    assert assist.strip_mention("<@!42>  녹음 시작", 42) == "녹음 시작"


def test_잡담은_조용히_애매한_조작은_되묻는다():
    assert assist.is_quiet({"actions": [], "refused": [], "unresolved": [{"reason": "x", "kind": "unknown"}]})
    assert not assist.is_quiet({"actions": [], "refused": [], "unresolved": [{"reason": "x", "kind": "ambiguous"}]})
    assert not assist.is_quiet({"actions": [], "refused": [{"reason": "설정은 안 돼요"}], "unresolved": []})
    assert not assist.is_quiet(PLAN)


def test_확인_글():
    t = assist.format_plan(PLAN)
    assert t.startswith("이렇게 할까요?")
    assert "- 강당 믹서 씬 → '조회'" in t and "(못 알아들음) 어떤 씬인가요?" in t
    assert "확인이 필요한 이유" in t and "2분" in t


def test_결과_글():
    res = {"results": [{"ok": True, "message": "강당 '조회' 씬"}, {"ok": True, "message": "강당 MIC 1 70→80"},
                       {"ok": False, "message": "녹음 기능이 아직 서버에 없어요"}]}
    t = assist.format_results(res, PLAN)
    assert t.splitlines()[0] == "완료: 강당 '조회' 씬, 강당 MIC 1 70→80"
    assert "실패: 녹음 기능이 아직 서버에 없어요" in t and "못 알아들음: 어떤 씬인가요?" in t
    assert assist.format_results({"results": []}) == "할 일이 없었어요."
    st = {"results": [{"ok": True, "action": "status_query", "message": "강당: 콘솔 연결됨"}]}
    assert assist.format_results(st) == "강당: 콘솔 연결됨"


def test_요청한_사람만_누른다():
    assert assist.can_press(5, "5")
    assert not assist.can_press(6, "5")


def test_실패_글():
    assert assist.fail_text("실행", 404, {"detail": "계획이 없거나 2분이 지나 사라졌어요."}).endswith("사라졌어요.")
    assert "연결하지 못했어요" in assist.fail_text("실행", 0, {})
    assert "권한" in assist.fail_text("요청 해석", 403, {"detail": None})


class FakeResp:
    def __init__(self, code, data):
        self.status_code = code
        self._data = data
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._data


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, json=None, timeout=None, headers=None):
        self.calls.append((url, json, headers))
        if url.endswith("/plan"):
            return FakeResp(200, {"planId": "p1", "actions": [], "needsConfirm": False})
        raise ConnectionError("down")


def test_HTTP_요청_모양():
    s = FakeSession()
    req = {"discordId": "1", "name": "a", "discordRole": "부장"}
    code, data = assist.plan("http://onair/api", "녹음 시작", req, {"CF-Access-Client-Id": "i"}, session=s)
    assert code == 200 and data["planId"] == "p1"
    assert s.calls[0] == ("http://onair/api/assist/plan", {"text": "녹음 시작", "requester": req},
                          {"CF-Access-Client-Id": "i"})
    assert assist.execute("http://onair/api", "p1", req, session=s) == (0, {})
    assert s.calls[1][1] == {"planId": "p1", "requester": req}
