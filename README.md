# BE-Agent

[![CI](https://github.com/ParkRhtn/BE-Agent/actions/workflows/ci.yml/badge.svg)](https://github.com/ParkRhtn/BE-Agent/actions/workflows/ci.yml)

LangGraph 기반 멀티 모델 에이전트 백엔드. FE(`../FE-Agent`, Next.js + Vercel AI SDK)와 SSE 로 연동한다.

## 스택

uv · FastAPI · LangGraph (`create_agent`) · `init_chat_model` 기반 멀티 모델 · MCP 도구 · SQLAlchemy 2.0 (async) · LangGraph 체크포인트 (SQLite / Postgres) · Langfuse

## 빠른 시작

```bash
cp .env.example .env
uv sync
uv run be-agent            # http://localhost:8000 , 문서: /docs
```

기본 모델은 `fake:echo` 라서 API 키 없이 동작한다. "지금 몇 시야?" 라고 보내면 도구 호출 흐름까지 확인할 수 있다.
실제 모델은 `.env` 에 API 키를 넣고 `DEFAULT_MODEL=anthropic:claude-sonnet-5` 처럼 바꾼다.

## 구조

```
src/be_agent/
├── main.py              # 앱 생성, lifespan (DB, 체크포인터, 도구 초기화)
├── core/
│   ├── config.py        # 환경변수 설정 (pydantic-settings)
│   ├── llm.py           # 모델 팩토리 — 모델 생성은 반드시 여기를 거친다
│   ├── fake_model.py    # 개발용 fake 모델
│   ├── observability.py # Langfuse 콜백
│   ├── usage.py         # 모델 호출마다 토큰·비용·과금 주체 기록, 사용량 집계, 크레딧 차감
│   ├── credits.py       # 크레딧 단위 변환, 잔액
│   └── pricing.py       # 모델별 토큰 가격표 (비용 계산)
├── agent/
│   ├── service.py       # 모델별 에이전트 그래프 캐시, 스레드 단위 실행
│   └── stream.py        # LangGraph 스트림 → 내부 이벤트
├── streaming/
│   ├── events.py        # 내부 공통 이벤트 (FE 프로토콜과 무관)
│   └── ai_sdk.py        # 내부 이벤트 → Vercel AI SDK UI Message Stream
├── tools/               # 기본 도구 + MCP 도구 로더
├── api/v1/              # REST API
├── db/                  # SQLAlchemy 모델 (스레드 메타데이터)
└── schemas/             # 요청/응답 Pydantic 스키마
```

스레드는 `agent_id` 로 에이전트에 연결된다. 에이전트가 없으면 `.env` 의 기본 프롬프트와 모든 도구를 쓴다.
컴파일된 그래프는 `(모델, 프롬프트, 도구)` 조합별로 캐시된다.

스트림은 `LangGraph → 내부 이벤트 → 프로토콜 어댑터` 두 단계로 변환한다.
다른 FE 프로토콜(AG-UI 등)이 필요하면 `streaming/` 에 어댑터만 추가하면 된다.

## API

| Method | Path | 설명 |
|---|---|---|
| POST | `/api/v1/auth/signup` · `/api/v1/auth/login` | 가입 / 로그인 → JWT 발급 |
| GET | `/api/v1/auth/me` | 현재 사용자 |
| GET/POST | `/api/v1/threads` | 스레드 목록 / 생성 |
| GET/PATCH/DELETE | `/api/v1/threads/{id}` | 스레드 조회 / 수정 / 삭제 (체크포인트 포함) |
| GET | `/api/v1/threads/{id}/messages` | 대화 이력 (AI SDK `UIMessage[]` 형식) |
| POST | `/api/v1/threads/{id}/chat` | 메시지 전송, SSE 스트리밍 응답 |
| GET/POST | `/api/v1/agents` | 에이전트 목록 / 생성 (이름·시스템 프롬프트·모델·도구) |
| GET/PATCH/DELETE | `/api/v1/agents/{id}` | 에이전트 조회 / 수정 / 삭제 (연결된 스레드는 기본 에이전트로 전환) |
| GET/POST | `/api/v1/workflows` | 워크플로우 목록 / 생성 (그래프는 React Flow 형식) |
| GET/PATCH/DELETE | `/api/v1/workflows/{id}` | 워크플로우 조회 / 저장 (미완성도 저장 가능) / 삭제 |
| POST | `/api/v1/workflows/{id}/run` | 검증 후 실행, 노드별 이벤트를 SSE 로 스트리밍 |
| GET | `/api/v1/tools` | 에이전트에 붙일 수 있는 도구 목록 (기본 + MCP) |
| GET | `/api/v1/models` | 이 사용자가 쓸 수 있는 모델 목록 / 기본 모델 |
| PUT | `/api/v1/models/default` | 기본 모델 변경 |
| GET/POST | `/api/v1/providers` | 모델 제공사 목록 / 추가 (실제 API 로 키 확인 후 저장) |
| PATCH/DELETE | `/api/v1/providers/{id}` | 이름·키·주소 변경 (다시 확인), 쓸 모델 켜기/끄기 / 삭제 |
| POST | `/api/v1/providers/{id}/verify` | 저장된 키로 다시 확인, 모델 목록 갱신 |
| POST | `/api/v1/providers/{id}/test` | 모델에 짧은 요청을 실제로 보내 응답 확인 (토큰 몇 개 비용) |
| GET/POST/DELETE | `/api/v1/api-keys` | 외부 API 키 목록 / 발급 (원문은 한 번만) / 폐기 |
| POST/DELETE | `/api/v1/workflows/{id}/publish` | 배포 / 배포 내림 |
| GET/PUT/DELETE | `/api/v1/workflows/{id}/embed` | 공개 링크(iframe) 조회 / 만들기·설정 / 없애기 |
| POST | `/api/v1/ext/workflows/{id}/run` | (API 키) 배포본 실행. 결과 JSON, `stream: true` 면 SSE |
| GET/POST | `/api/v1/public/embeds/{token}` · `/run` | (로그인 없이) 공개 링크 정보 / 실행 |
| GET | `/api/v1/credits` | 내 크레딧 잔액과 충전·사용 내역 |
| GET/POST | `/api/v1/admin/credits` | (관리자) 사용자 크레딧 조회 / 충전·조정 |

## 모델 설정

FE 의 **설정** 화면에서 Anthropic · OpenAI · OpenAI 호환 서버(Ollama 등)를 연결한다.

- 키는 저장 전에 제공사의 모델 목록 API 로 확인하고, 통과한 키만 암호화(Fernet)해 저장한다. 화면에는 끝 4자리만 보인다
- 확인 때 받은 모델 중 켠 모델만 대화·에이전트·워크플로우의 모델 목록에 나온다. 모델 ID 는 `<제공사ID>:<모델>`
- 설정 화면에서 등록한 모델이 없으면 `.env` 의 `ALLOWED_MODELS` 중 키가 있는 것(`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`)을 대신 쓴다. `fake:echo` 는 `ENVIRONMENT=local` 에서만
- 암호화 키는 `ENCRYPTION_KEY`, 없으면 `JWT_SECRET` 에서 만든다. 이 값을 바꾸면 저장된 키를 다시 입력해야 한다

## 워크플로우

`workflow/engine.py` 가 그래프를 검증하고, **LangGraph StateGraph 로 컴파일해** 실행한다.
노드: `start` · `llm` · `agent` · `tool` · `condition` · `end`.

- 캔버스 노드 하나 = LangGraph 노드 하나. 앞 노드가 여럿이면 모두 끝난 뒤 한 번 실행한다 (순환 금지)
- 서로 무관한 갈래는 같은 단계에서 동시에 실행된다
- 모든 노드가 자기 차례에 실행되고, 활성 연결이 없으면 건너뜀만 남긴다 (조건으로 건너뛴 갈래가 합류해도 멈추지 않게)
- `{{노드ID}}` 는 그 노드보다 앞 단계에서 끝난 노드만 확실히 참조할 수 있다 (병렬 갈래끼리는 서로의 출력을 못 본다)
- 조건 노드는 `"true"`/`"false"` 를 출력하고, 같은 이름의 `sourceHandle` 연결만 활성화된다
- 노드 설정의 `{{input}}`, `{{노드ID}}` 는 사용자 입력 / 해당 노드 출력으로 치환된다
- 에이전트 노드는 대화 이력을 남기지 않고 한 번 실행한다
- 실행 이벤트: `run_start` · `node_start` · `node_delta`(LLM 스트리밍) · `node_finish` · `node_skip` · `node_error` · `run_finish`

## 배포 (편집본 / 배포본)

워크플로우 화면에서 저장한 것은 **편집본**이고, '배포'를 눌러야 **배포본**이 된다.
외부 API·공개 링크는 배포본만 실행하므로, 운영 중인 연동을 두고 화면에서 마음껏 고쳐도 된다.
화면 실행과 예약 실행은 편집본을 쓴다. 배포할 때 실행과 같은 검증을 한다.

- `POST /api/v1/workflows/{id}/publish` 배포, `DELETE` 로 배포 내림 (외부 호출이 바로 거부됨)
- `published_at`, `has_unpublished_changes` 로 상태를 보여 준다

## 외부 API (간편 API)

다른 서비스(내 서버, 앱, Zapier·n8n 등)에서 배포된 워크플로우를 실행한다. 설정 → API 키에서 키를 만들고,
워크플로우 목록 카드 메뉴 → 'API로 호출'에서 curl·Python·JavaScript 예시를 복사한다.

```bash
curl -X POST http://localhost:8000/api/v1/ext/workflows/<ID>/run \
  -H "Authorization: Bearer sk-be-..." -H "Content-Type: application/json" \
  -d '{"input": "안녕하세요"}'
# → {"run_id": "...", "status": "done", "output": "...", "error": null}
```

- `"stream": true` 면 노드별 이벤트를 SSE 로 받는다 (화면 실행과 같은 형식).
- 실행 기록에는 `trigger: api` 로 남는다. 크레딧·사용량은 키 소유자 기준으로 똑같이 적용된다.
- API 키는 `/api/v1/ext/*` 에서만 받는다. 키가 유출돼도 새 키 발급·제공사 키·계정 정보에는 접근할 수 없다.
- 키는 해시만 저장하고 원문은 발급할 때 한 번만 보여 준다. 지우면 바로 거부된다.
- 키 하나당 1분에 `EXT_RATE_LIMIT_PER_MINUTE`(기본 60)번. 서버 프로세스 메모리에서 세므로 여러 대로 늘리면 Redis 로 옮긴다.
- 키마다 부를 수 있는 워크플로우를 고를 수 있다 (`workflow_ids`, 없으면 전부). 고객에게 넘길 키는 그 고객 것만.
- 배포 전이면 409.

## 공개 링크 (iframe)

카드 메뉴 → '외부에서 쓰기' → '웹사이트에 붙이기'에서 링크를 만들면 `<iframe src=".../embed/<token>">` 코드를 준다.
방문자는 로그인 없이 배포본을 실행하고, 비용은 링크를 만든 사람 크레딧·키로 나간다.

- 허용 사이트(`allowed_origins`)를 적으면 그 사이트에서만 iframe 이 뜬다 (FE 가 `frame-ancestors` 로 막는다). 비우면 어디서나.
- 링크마다 하루 실행 한도(한국 날짜)와 1분 한도(`EMBED_RATE_LIMIT_PER_MINUTE`, 기본 20).
- 꺼져 있거나, 배포 전이거나, 없는 링크는 모두 404. 실패 원인은 방문자에게 보이지 않고 소유자 실행 기록에 남는다.
- 실행 기록에는 `trigger: embed` 로 남는다.
- FE 는 공개 링크 말고 모든 페이지에 `frame-ancestors 'self'` 를 붙여 다른 사이트 iframe 에 뜨지 않게 한다.

## 텔레그램 알림

설정 화면에서 내 텔레그램 봇 토큰(@BotFather 에서 발급)을 넣으면, 봇에게 먼저 말을 건 내 채팅을 찾아 연결한다.
토큰은 암호화해 저장한다. 워크플로우·에이전트의 `send_telegram`(텔레그램 보내기) 도구가 이 채팅으로 보낸다.
마크다운은 텔레그램 HTML 로 바꿔 보내고, 4096자를 넘는 글은 나눠 보낸다.

## 예약 실행

워크플로우마다 요일·시각(한국 시간)과 입력을 정해 두면 서버 안의 스케줄러(`workflow/scheduler.py`)가 30초마다 확인해 실행한다.
결과는 실행 기록에 `trigger = "schedule"` 로 남는다. 같은 예정 시각은 한 번만 돌고(DB 조건부 갱신으로 맡음),
서버가 꺼져 있어 1시간 넘게 놓친 실행은 건너뛴다. `SCHEDULER_ENABLED=false` 로 끌 수 있다.

## 사용량

모델을 부를 때마다 토큰 수를 `usage_records` 표에 남기고, `core/pricing.py` 의 가격표로 비용(USD)을 계산한다.
`GET /api/v1/usage?days=7|30|90` 이 모델별·워크플로우/대화별·날짜별(한국 시간)로 모아 준다. Langfuse 없이도 동작한다.
가격표에 없는 모델은 호출·토큰만 세고 비용은 "가격 정보 없음"으로 따로 센다. 새 모델을 쓰면 가격표에 한 줄 추가한다.
호출마다 누구 키로 불렀는지(`billing`: 서버 키 / 내 API 키 / 개발용)도 남겨 `by_billing` 으로 나눠 보여 준다.

## 크레딧

서버 키(`.env` 의 `ANTHROPIC_API_KEY` 등) 모델은 운영자가 비용을 내므로 크레딧으로 과금한다.
사용자가 설정 화면에서 등록한 자기 키의 모델은 비용이 사용자 계정에서 바로 나가므로 차감하지 않는다.

- 차감: 대화·워크플로우 실행이 끝나면 서버 키로 부른 호출의 실측 원가 × `CREDITS_PER_USD` 를 한 번에 차감한다.
  사용량 기록과 차감은 한 트랜잭션이라 기록만 남고 차감이 빠지는 일은 없다.
- 차단 (`CREDITS_ENFORCED=true`): 잔액이 0 이하면 서버 키 모델 대화는 402, 워크플로우는 시작 전에 402.
  사용자 키 모델은 계속 쓸 수 있다. 가격표에 없는 서버 키 모델은 원가를 알 수 없어 잔액과 무관하게 막는다.
- 실행 도중에는 막지 않으므로 마지막 실행은 잔액을 넘어 음수가 될 수 있다 (그 다음 실행부터 막힘).
- 충전: 결제 연동 전에는 관리자(`ADMIN_EMAILS`)가 `POST /api/v1/admin/credits {email, amount, note}` 로 넣는다. 음수는 회수.
- 잔액은 `credit_transactions` 합계다. 기록은 고치지 않고 조정 기록을 추가한다.

## 추적 (Langfuse)

`.env` 에 `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` (필요하면 `LANGFUSE_HOST`) 를 넣으면 켜진다. 없으면 아무것도 보내지 않는다.

- 대화: 대화 한 번이 트레이스 하나. 사용자 ID, 세션 = 대화 ID, 태그 `chat` · `agent:<이름>`
- 워크플로우: 실행 한 번이 트레이스 하나 (`워크플로우: <이름>`). 세션 = `workflow-<ID>` 라 같은 워크플로우의 실행이 모인다.
  노드마다 하위 기록(`<노드ID> (<종류>)`)이 생기고, 그 안의 모델 호출(토큰·비용)·도구 호출·에이전트 실행이 붙는다.
  실패한 노드와 실행은 ERROR 로 표시된다
- 사용자는 이메일이 아니라 내부 ID 로 보낸다
- 구현: `core/observability.py`. 테스트(`tests/test_tracing.py`)는 기록을 메모리로 받아 구조를 검사한다

## 인증

`auth` 를 제외한 모든 API 는 `Authorization: Bearer <JWT>` 가 필요하고, 스레드·에이전트는 사용자별로 분리된다.
FE 는 토큰을 httpOnly 쿠키에 두고 BFF 프록시에서 헤더로 옮긴다.

- `JWT_SECRET`: `openssl rand -hex 32` 로 생성. `ENVIRONMENT` 가 `local` 이 아니면 필수
- `ALLOW_SIGNUP=false`: 가입 차단 (개인용이면 첫 계정을 만든 뒤 끈다)
- 인증 도입 전에 만든 스레드·에이전트는 **처음 가입한 계정**에 귀속된다

## 개발

```bash
uv run pytest               # 테스트
uv run ruff check . && uv run ruff format .
uv run pyright
uv run python scripts/export_openapi.py ../FE-Agent/openapi.json   # FE 타입 생성용 스펙
```

### Postgres 로 전환

```bash
docker compose up -d postgres
# .env
DATABASE_URL=postgresql+asyncpg://agent:agent@localhost:5432/agent
```

스레드 테이블과 LangGraph 체크포인트가 같은 Postgres 에 저장된다.

### MCP 도구 추가

`mcp_servers.example.json` 을 `mcp_servers.json` 으로 복사해 수정하고 `.env` 에 `MCP_CONFIG_PATH=./mcp_servers.json` 을 설정한다.

## 다음 단계 (TODO)

- Alembic 마이그레이션 (현재는 시작 시 `create_all`)
- Human-in-the-loop: LangGraph `interrupt` ↔ AI SDK `tool-approval-request`
- 긴 작업용 백그라운드 실행 + 재연결 가능한 스트림 (Redis)
