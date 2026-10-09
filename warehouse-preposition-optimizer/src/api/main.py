"""FastAPI application entry point with lifespan management."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from src.api.routes import config as config_router
from src.api.routes import health as health_router
from src.api.routes import movements as movements_router
from src.api.routes import scheduler as scheduler_router
from src.api.routes import scoring as scoring_router
from src.api import websocket as websocket_module
from src.config import load_config
from src.constraints.capacity import CapacityConstraint
from src.constraints.feasibility import FeasibilityEngine
from src.constraints.hazmat import HazmatConstraint
from src.constraints.temperature import TemperatureConstraint
from src.api.websocket import ConnectionManager
from src.dispatch.agv_interface import AGVInterface
from src.dispatch.rejection_store import RejectionStore
from src.dispatch.task_queue import TaskQueue
from src.ingestion.adapters.generic_db import GenericDBAdapter
from src.ingestion.schema import load_wms_schema
from src.optimizer.scheduler import PrePositionScheduler, SchedulerConfig
from src.scoring.value_function import MovementScorer
from src.scoring.weights import ScoringWeights

logger = structlog.get_logger(__name__)


def _configure_structlog() -> None:
    """Configure structlog for JSON logging."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(20),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Initialize and tear down application resources.

    Connects to Redis and Postgres, builds the scheduler pipeline,
    and stores everything on app.state for route handlers to access.

    Args:
        app: FastAPI application instance.

    Yields:
        None (passes control to the application).
    """
    settings = load_config()
    _configure_structlog()
    logger.info("app.startup", log_level=settings.log_level)

    # Redis
    try:
        import redis.asyncio as aioredis

        redis_client = aioredis.from_url(
            settings.redis_url, encoding="utf-8", decode_responses=True
        )
        await redis_client.ping()
        logger.info("app.redis_connected", url=settings.redis_url)
    except Exception as exc:
        logger.warning("app.redis_unavailable", error=str(exc))
        redis_client = None

    # WMS adapter (with optional customer schema mapping)
    wms_schema = load_wms_schema(settings.wms.schema_path)
    if settings.wms.schema_path:
        logger.info("app.wms_schema_loaded", path=settings.wms.schema_path)
    wms_adapter = GenericDBAdapter(
        database_url=settings.database_url,
        redis_client=redis_client,
        cache_ttl_seconds=settings.wms.cache_ttl_seconds,
        schema=wms_schema,
    )
    try:
        await wms_adapter.connect()
        logger.info("app.wms_connected")
    except Exception as exc:
        logger.warning("app.wms_unavailable", error=str(exc))

    # Scoring
    from src.config import ResourceConfig

    resource_cfg = settings.resources
    resource_config = ResourceConfig(
        forklift_speed_mps=resource_cfg.forklift_speed_mps,
        agv_speed_mps=resource_cfg.agv_speed_mps,
        handling_time_seconds=resource_cfg.handling_time_seconds,
        max_utilization=resource_cfg.max_utilization,
    )
    weights = ScoringWeights(
        time_saved=settings.scoring.weights.time_saved,
        load_probability=settings.scoring.weights.load_probability,
        order_priority=settings.scoring.weights.order_priority,
        movement_cost=settings.scoring.weights.movement_cost,
        opportunity_cost=settings.scoring.weights.opportunity_cost,
        decay_constant_seconds=settings.scoring.decay_constant_seconds,
    )

    # Phase 2: wire ML InferenceEngine when enabled and model artifact exists
    ml_inference = None
    if settings.use_ml_prediction:
        model_path = Path(settings.prediction.model_path)
        if model_path.exists():
            try:
                from src.prediction.features import FeatureBuilder
                from src.prediction.inference import InferenceEngine
                from src.prediction.trainer import MLDemandPredictor
                from src.scoring.demand_predictor import DemandPredictor

                ml_predictor = MLDemandPredictor()
                ml_predictor.load(model_path)
                ml_inference = InferenceEngine(
                    ml_predictor=ml_predictor,
                    fallback=DemandPredictor(),
                    feature_builder=FeatureBuilder(),
                    cache_ttl_seconds=float(settings.prediction.prediction_cache_ttl_seconds),
                )
                logger.info("app.ml_inference_loaded", model_path=str(model_path))
            except Exception as exc:
                logger.warning(
                    "app.ml_inference_load_failed",
                    error=str(exc),
                    model_path=str(model_path),
                )
        else:
            logger.warning(
                "app.ml_inference_model_missing",
                model_path=str(model_path),
                hint="Run scripts/generate_training_data.py then train MLDemandPredictor",
            )

    scorer = MovementScorer(weights=weights, config=resource_config, ml_inference=ml_inference)

    # Constraints
    feasibility = FeasibilityEngine(
        filters=[
            TemperatureConstraint(),
            HazmatConstraint(),
            CapacityConstraint(max_utilization=settings.resources.max_utilization),
        ]
    )

    # Task queue
    task_queue = TaskQueue(
        redis_client=redis_client,
        task_expiry_minutes=settings.scheduling.task_expiry_minutes,
    )

    # Phase 5: rejection store + WebSocket manager
    rejection_store = RejectionStore(redis_client=redis_client)
    ws_manager = ConnectionManager()

    # Scheduler
    scheduler_config = SchedulerConfig(
        cycle_interval_seconds=settings.scheduling.cycle_interval_seconds,
        dispatch_batch_size=settings.scheduling.dispatch_batch_size,
        horizon_hours=settings.scheduling.horizon_hours,
        max_candidates=settings.scoring.max_candidates_per_cycle,
        min_score_threshold=settings.scoring.min_score_threshold,
    )
    scheduler = PrePositionScheduler(
        scorer=scorer,
        feasibility=feasibility,
        wms=wms_adapter,
        task_queue=task_queue,
        config=scheduler_config,
        rejection_store=rejection_store,
    )

    # Store on app state
    app.state.settings = settings
    app.state.redis = redis_client
    app.state.wms_adapter = wms_adapter
    app.state.scorer = scorer
    app.state.task_queue = task_queue
    app.state.scheduler = scheduler
    app.state.agv = AGVInterface()
    app.state.ml_inference = ml_inference
    app.state.rejection_store = rejection_store
    app.state.ws_manager = ws_manager

    # Background scheduler loop — runs run_cycle() every cycle_interval_seconds.
    loop_task = asyncio.create_task(
        _scheduler_loop(scheduler, scheduler_config.cycle_interval_seconds, ws_manager)
    )
    app.state.scheduler_loop_task = loop_task

    logger.info("app.ready")
    yield

    # Shutdown
    logger.info("app.shutdown")
    loop_task.cancel()
    try:
        await loop_task
    except asyncio.CancelledError:
        pass
    try:
        await wms_adapter.disconnect()
    except Exception as exc:
        logger.warning("app.wms_disconnect_error", error=str(exc))
    if redis_client:
        await redis_client.aclose()


async def _scheduler_loop(
    scheduler: PrePositionScheduler,
    interval_seconds: int,
    ws_manager: ConnectionManager | None = None,
) -> None:
    """Run the scheduler in a continuous background loop.

    Calls run_cycle() every interval_seconds. Exceptions are logged and the loop
    continues so a single WMS poll failure doesn't stop all future cycles.

    Args:
        scheduler: The pre-positioning scheduler to drive.
        interval_seconds: Seconds to sleep between cycles.
        ws_manager: Optional WebSocket manager for broadcasting cycle events.
    """
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            candidates, tasks = await scheduler.run_cycle()
            logger.info(
                "scheduler_loop.cycle_done",
                candidates=len(candidates),
                dispatched=len(tasks),
            )
            if ws_manager is not None:
                await ws_manager.broadcast("cycle_complete", {
                    "candidates_scored": len(candidates),
                    "tasks_dispatched": len(tasks),
                    "reason": "scheduled",
                })
        except asyncio.CancelledError:
            logger.info("scheduler_loop.cancelled")
            raise
        except Exception as exc:
            logger.error("scheduler_loop.cycle_error", error=str(exc))


def create_app() -> FastAPI:
    """Build and configure the FastAPI application.

    Returns:
        Configured FastAPI application instance.
    """
    app = FastAPI(
        title="Warehouse Pre-Positioning Optimizer",
        description="Advisory system for pre-staging inventory near loading bays.",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # API key middleware
    @app.middleware("http")
    async def api_key_middleware(request: Request, call_next: Any) -> Response:
        """Require X-API-Key header on all non-health endpoints.

        Args:
            request: Incoming HTTP request.
            call_next: Next middleware or handler.

        Returns:
            HTTP response.
        """
        # Allow health check and WebSocket upgrade without HTTP auth
        # (WebSocket auth is handled inside the endpoint via query param)
        if request.url.path in ("/api/v1/health", "/api/v1/metrics", "/docs", "/openapi.json") \
                or request.url.path.startswith("/api/v1/ws/"):
            return await call_next(request)

        api_key = request.headers.get("X-API-Key", "")
        settings = getattr(request.app.state, "settings", None)
        expected_key = settings.api_key if settings else "change-me-in-production"

        if api_key != expected_key:
            from fastapi.responses import JSONResponse

            return JSONResponse(status_code=401, content={"detail": "Invalid or missing API key."})

        return await call_next(request)

    # Request logging middleware
    @app.middleware("http")
    async def request_logging_middleware(request: Request, call_next: Any) -> Response:
        """Log each request with method, path, and duration.

        Args:
            request: Incoming HTTP request.
            call_next: Next middleware or handler.

        Returns:
            HTTP response.
        """
        t0 = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - t0) * 1000
        logger.info(
            "http.request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round(duration_ms, 1),
        )
        return response

    # Routers
    prefix = "/api/v1"
    app.include_router(health_router.router, prefix=prefix)
    app.include_router(movements_router.router, prefix=prefix)
    app.include_router(scoring_router.router, prefix=prefix)
    app.include_router(config_router.router, prefix=prefix)
    app.include_router(scheduler_router.router, prefix=prefix)
    app.include_router(websocket_module.router, prefix=prefix)

    return app


app = create_app()
