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
│   └── observability.py # Langfuse 콜백
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
