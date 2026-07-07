# ONAIR Discord 봇

BSSM 방송부 Discord 서버의 `#명령` 채널에서 **슬래시 명령**으로 ONAIR를 제어하는 봇.
ONAIR 서버 코드에 의존하지 않고 **HTTP(REST)로만** 통신하므로 독립 실행된다.

## 하는 일

- 시작 시: 역할 6개 자동 생성(`관리자`/`부장`/`부원`, `4기`/`5기`/`6기`), `#알림` 채널 읽기전용 잠금, 슬래시 명령 길드 동기화.
- 슬래시 명령 (모두 `#명령` 채널에서만, 역할로 게이팅):
  - `/상태` — 부원 이상. 스피커·송출·현수막·시보·매트릭스 전체 상태 조회.
  - `/tts [내용] [대상]` — 부장 이상. TTS 방송 송출.
  - `/스피커 [대상] [동작]` — 부장 이상. 스피커 ON/OFF.

> 알림(웹훅) 기능은 봇이 아니라 **ONAIR 서버**(`notification_service`)가 담당한다. 이 저장소는 명령 봇만.

## 설치 & 실행

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env            # Windows: copy .env.example .env
# .env 에 DISCORD_BOT_TOKEN 등을 채운다

python discord_bot.py
```

## ⚠️ 인스턴스는 딱 하나만 (중요)

**같은 봇 토큰으로 여러 PC에서 동시에 띄우지 말 것.** 봇은 토큰 하나 = Discord 상 하나의 정체성이라, 인스턴스를 여러 개 띄우면:

1. 슬래시 명령 하나를 **모든 인스턴스가 동시에 수신**해서 서로 응답하려고 경쟁한다. 먼저 응답한 하나만 성공하고 나머지는 "이미 처리된 상호작용" 에러.
2. 더 나쁜 건, **각 인스턴스가 자기 `.env`의 `ONAIR_API`로 독립적으로 요청**을 쏜다 → 같은 명령이 여러 번 실행될 수 있다(예: TTS가 두 번 나감).

즉 "어느 서버를 기준으로 가는가"는 **각 봇 인스턴스의 `.env` 안 `ONAIR_API`가 결정**한다. 기본값 `127.0.0.1:8000`은 "봇이 돌고 있는 그 PC의 서버"를 뜻한다.

**권장 구성:** ONAIR 서버가 있는 PC(또는 그 서버에 tailnet으로 닿는 PC) **한 곳에서만** 봇을 실행한다.
- 봇을 서버와 같은 PC에서 → `ONAIR_API=http://127.0.0.1:8000/api`
- 봇을 다른 PC에서 → `ONAIR_API=http://<서버_Tailscale_IP>:8000/api`

가용성이 걱정되면 인스턴스를 늘리지 말고 **자동 재시작**(Windows: 작업 스케줄러/nssm, Linux: systemd)으로 단일 인스턴스를 살려두는 방식을 쓴다. discord.py는 게이트웨이가 끊겨도 스스로 재접속한다.
