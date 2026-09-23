import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langgraph.checkpoint.base import BaseCheckpointSaver

from be_agent.agent.service import AgentService
from be_agent.api.v1.router import api_router
from be_agent.core.config import Settings, get_settings
from be_agent.core.observability import create_tracing
from be_agent.db.session import create_engine, create_sessionmaker, init_db
from be_agent.tools import BASIC_TOOLS, load_mcp_tools

logger = logging.getLogger(__name__)


async def _open_checkpointer(stack: AsyncExitStack, settings: Settings) -> BaseCheckpointSaver:
    if settings.is_sqlite:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        Path(settings.checkpoint_sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        saver = await stack.enter_async_context(AsyncSqliteSaver.from_conn_string(settings.checkpoint_sqlite_path))
    else:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        saver = await stack.enter_async_context(AsyncPostgresSaver.from_conn_string(settings.postgres_conninfo))
    await saver.setup()
    return saver


def create_app(settings: Settings | None = None, *, trace_exporter: Any = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database_url)
        await init_db(engine)
        async with AsyncExitStack() as stack:
            checkpointer = await _open_checkpointer(stack, settings)
            tools = [*BASIC_TOOLS, *await load_mcp_tools(settings.mcp_config_path)]
            tracing = create_tracing(settings, span_exporter=trace_exporter)
            app.state.tracing = tracing
            app.state.settings = settings
            app.state.sessionmaker = create_sessionmaker(engine)
            app.state.agent_service = AgentService(
                checkpointer=checkpointer,
                tools=tools,
                system_prompt=settings.system_prompt,
                callbacks=tracing.callbacks,
            )
            logger.info("Started with default model %s and tools %s", settings.default_model, [t.name for t in tools])
            yield
            tracing.shutdown()
        await engine.dispose()

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router)

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
