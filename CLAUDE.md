# Warehouse Pre-Positioning Optimizer

## Project Context
External reasoning system that reads WMS state (inventory positions, outbound orders, dock
schedules) and generates scored repositioning movements to pre-stage product near loading bays.
Does NOT modify WMS data. Read-only ingest, write-only task dispatch.

## Architecture Principles
- WMS is system of record. This system is advisory/task-generating only.
- All WMS interaction goes through src/ingestion/wms_adapter.py abstract interface.
- Hard constraints (temperature, hazmat, capacity) are NEVER relaxed. They filter before scoring.
- Scoring function V(m) lives in src/scoring/value_function.py. All terms are documented.
- Feature flags in src/config.py gate ML prediction and OR optimization layers.

## Tech Stack
- Python 3.12+, uv for dependency management
- FastAPI for API, Pydantic v2 for all data models
- PostgreSQL for state persistence, Redis for task queue and caching
- LightGBM for demand prediction (Phase 2)
- Google OR-Tools for assignment/routing optimization (Phase 3)
- SimPy + Gymnasium for RL training environment (Phase 4)

## Code Conventions
- Type hints on all function signatures. No Any types except in adapter raw responses.
- Pydantic models for all data crossing module boundaries.
- Each module has a single public interface function or class. Internal helpers are prefixed _.
- Tests mirror src/ structure. Every scoring formula term has a unit test.
- Docstrings: one-line summary, then Args/Returns in Google style.

## Key Formulas
Value function: V(m) = (T_saved * P_load * W_order) / (C_move + C_opportunity)
- T_saved: estimated seconds saved at load-out (distance-based)
- P_load: probability SKU loads in window [0.0, 1.0]
- W_order: order priority weight [0.1, 10.0]
- C_move: movement cost in seconds (travel + handling)
- C_opportunity: opportunity cost of resource unavailability in seconds

## Testing
Run: uv run pytest
Minimum coverage: 90% on src/scoring/, src/constraints/
Integration tests use docker-compose test profile with seeded Postgres.

## Common Tasks
- Add new WMS adapter: subclass WMSAdapter in src/ingestion/wms_adapter.py
- Add new constraint type: implement ConstraintFilter in src/constraints/feasibility.py
- Tune scoring weights: run scripts/calibrate_weights.py
- Backtest on historical data: run scripts/backtest.py --date-range 2024-01-01:2024-03-31