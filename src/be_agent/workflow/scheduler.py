"""예약 실행. 서버 안에서 30초마다 예정 시각이 지난 워크플로우를 찾아 실행한다.

- 같은 예정 시각은 한 번만 돈다: 실행 전에 schedule_last_at 을 조건부로 바꿔 "맡는다" (서버가 여러 개여도 안전).
- 서버가 꺼져 있어 놓친 실행은 1시간 안이면 켜지자마자 돌리고, 그보다 오래됐으면 건너뛴다.
- 실행 결과는 화면에서 누른 실행과 똑같이 실행 기록(runs)에 남는다 (trigger = "schedule").
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from be_agent.core.config import Settings
from be_agent.core.model_registry import load_registry
from be_agent.core.observability import Tracing
from be_agent.db.models import Run, User, Workflow
from be_agent.workflow.runner import WorkflowInvalid, WorkflowRunner
from be_agent.workflow.schedule import last_due

logger = logging.getLogger(__name__)

GRACE = timedelta(hours=1)


@dataclass
class WorkflowScheduler:
    sessionmaker: async_sessionmaker
    runner: WorkflowRunner
    settings: Settings
    interval: float = 30.0
    _running: set[asyncio.Task[None]] = field(default_factory=set)

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("Scheduler tick failed")
            await asyncio.sleep(self.interval)

    async def tick(self, now: datetime | None = None) -> list[asyncio.Task[None]]:
        """예정 시각이 된 워크플로우를 맡아 실행을 시작한다. 시작한 작업들을 돌려준다 (테스트용)."""
        now = now or datetime.now(UTC)
        started: list[asyncio.Task[None]] = []
        async with self.sessionmaker() as db:
            workflows = await db.scalars(select(Workflow).where(Workflow.schedule.is_not(None)))
            for workflow in workflows:
                schedule = workflow.schedule or {}
                if not schedule.get("enabled"):
                    continue
                due = last_due(schedule, now)
                if due is None or now - due > GRACE:
                    continue
                claimed = await db.execute(
                    update(Workflow)
                    .where(
                        Workflow.id == workflow.id,
                        or_(Workflow.schedule_last_at.is_(None), Workflow.schedule_last_at < due),
                    )
                    .values(schedule_last_at=due)
                )
                await db.commit()
                if claimed.rowcount != 1:  # type: ignore[attr-defined]
                    continue  # 이미 돌았다
                task = asyncio.create_task(self._run(workflow.id, schedule.get("input", "")))
                self._running.add(task)
                task.add_done_callback(self._running.discard)
                started.append(task)
        return started

    async def _run(self, workflow_id: str, input: str) -> None:
        async with self.sessionmaker() as db:
            workflow = await db.get(Workflow, workflow_id)
            user = await db.get(User, workflow.user_id) if workflow else None
            if workflow is None or user is None:
                return
            registry = await load_registry(db, user, self.settings)
            try:
                _, events = await self.runner.start(
                    db, user=user, workflow=workflow, registry=registry, input=input, trigger="schedule"
                )
            except WorkflowInvalid as exc:
                # 실행 기록에 실패로 남겨 화면에서 보이게 한다
                logger.warning("Scheduled workflow %s is invalid: %s", workflow_id, exc)
                run_id = uuid.uuid4().hex
                db.add(
                    Run(
                        id=run_id,
                        user_id=user.id,
                        kind="workflow",
                        trigger="schedule",
                        workflow_id=workflow.id,
                        trace_id=Tracing.trace_id_for(run_id),
                        status="error",
                        input=input,
                        error="실행 전 검사에 걸렸습니다: " + " / ".join(exc.errors),
                        steps=[],
                        finished_at=datetime.now(UTC),
                    )
                )
                await db.commit()
                return
        logger.info("Scheduled run started: workflow=%s", workflow_id)
        try:
            async for _ in events():
                pass  # 결과는 실행 기록에 저장된다
        except Exception:
            logger.exception("Scheduled run failed: workflow=%s", workflow_id)

    async def shutdown(self) -> None:
        for task in self._running:
            task.cancel()
        await asyncio.gather(*self._running, return_exceptions=True)
