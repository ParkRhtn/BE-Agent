import asyncio
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from be_agent.workflow.schedule import SCHEDULE_TZ, last_due, next_due
from be_agent.workflow.scheduler import WorkflowScheduler
from tests.test_workflows import _create, edge, node

WEEKDAYS = {"enabled": True, "time": "08:00", "weekdays": [0, 1, 2, 3, 4], "input": ""}


def kst(*args: int) -> datetime:
    return datetime(*args, tzinfo=SCHEDULE_TZ)  # type: ignore[misc]


def test_due_times_follow_korean_time_and_weekdays() -> None:
    # 2026-09-25 금요일 07:59 → 다음은 금요일 08:00, 지난 것은 목요일 08:00
    assert next_due(WEEKDAYS, kst(2026, 9, 25, 7, 59)) == kst(2026, 9, 25, 8, 0)
    assert last_due(WEEKDAYS, kst(2026, 9, 25, 7, 59)) == kst(2026, 9, 24, 8, 0)
    # 금요일 09:00 이후면 주말을 건너뛰고 월요일
    assert next_due(WEEKDAYS, kst(2026, 9, 25, 9, 0)) == kst(2026, 9, 28, 8, 0)
    # UTC 로 받아도 한국 시간으로 계산한다 (UTC 23:30 = 한국 다음날 08:30)
    assert last_due(WEEKDAYS, datetime(2026, 9, 24, 23, 30, tzinfo=UTC)) == kst(2026, 9, 25, 8, 0)
    assert next_due({**WEEKDAYS, "enabled": False}, kst(2026, 9, 25, 7, 0)) is None


def test_schedule_is_saved_with_next_run(client: TestClient) -> None:
    workflow_id = _create(client, [node("start", "start"), node("end", "end")], [edge("start", "end")])
    response = client.patch(f"/api/v1/workflows/{workflow_id}", json={"schedule": WEEKDAYS})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schedule"] == WEEKDAYS
    next_run = datetime.fromisoformat(body["next_run_at"]).astimezone(SCHEDULE_TZ)
    assert (next_run.hour, next_run.minute) == (8, 0) and next_run.weekday() < 5

    assert (
        client.patch(f"/api/v1/workflows/{workflow_id}", json={"schedule": {**WEEKDAYS, "time": "25:00"}}).status_code
        == 422
    )
    assert (
        client.patch(f"/api/v1/workflows/{workflow_id}", json={"schedule": {**WEEKDAYS, "weekdays": []}}).status_code
        == 422
    )

    cleared = client.patch(f"/api/v1/workflows/{workflow_id}", json={"schedule": None}).json()
    assert cleared["schedule"] is None and cleared["next_run_at"] is None


def _tick(client: TestClient, now: datetime) -> int:
    state = client.app.state  # type: ignore[attr-defined]
    scheduler = WorkflowScheduler(state.sessionmaker, state.workflow_runner, state.settings)

    async def tick_and_wait() -> int:
        tasks = await scheduler.tick(now)
        await asyncio.gather(*tasks)
        return len(tasks)

    return client.portal.call(tick_and_wait)  # type: ignore[union-attr]


def test_scheduler_runs_each_due_time_once(client: TestClient) -> None:
    workflow_id = _create(
        client,
        [node("start", "start"), node("llm_1", "llm", prompt="{{input}}", model="fake:echo"), node("end", "end")],
        [edge("start", "llm_1"), edge("llm_1", "end")],
    )
    schedule = {**WEEKDAYS, "weekdays": list(range(7)), "input": "아침 뉴스"}
    client.patch(f"/api/v1/workflows/{workflow_id}", json={"schedule": schedule})

    now = datetime.now(UTC)
    assert _tick(client, now) == 0  # 예약을 켜기 전에 지난 시각은 돌지 않는다

    due = next_due(schedule, now)
    assert due is not None
    assert _tick(client, due - timedelta(minutes=1)) == 0
    assert _tick(client, due + timedelta(minutes=5)) == 1
    assert _tick(client, due + timedelta(minutes=6)) == 0  # 같은 시각은 한 번만
    # 서버가 꺼져 있어 한참 늦었으면 건너뛴다
    assert _tick(client, due + timedelta(days=1, hours=2)) == 0

    runs = client.get(f"/api/v1/workflows/{workflow_id}/runs").json()
    assert len(runs) == 1
    assert runs[0]["trigger"] == "schedule" and runs[0]["status"] == "done"
    assert runs[0]["input"] == "아침 뉴스" and "아침 뉴스" in runs[0]["output"]


def test_scheduled_invalid_workflow_is_recorded(client: TestClient) -> None:
    # 종료 노드가 없어 검증에 걸리는 워크플로우
    workflow_id = _create(client, [node("start", "start")], [])
    schedule = {**WEEKDAYS, "weekdays": list(range(7))}
    client.patch(f"/api/v1/workflows/{workflow_id}", json={"schedule": schedule})
    due = next_due(schedule, datetime.now(UTC))
    assert due is not None
    assert _tick(client, due + timedelta(minutes=1)) == 1
    runs = client.get(f"/api/v1/workflows/{workflow_id}/runs").json()
    assert runs[0]["status"] == "error" and "검사" in runs[0]["error"]
