# TODO

Tracks implementation progress across all phases. Update this file whenever a step is completed or a new gap is discovered.

---

## Phase 1: Weighted Scoring MVP — COMPLETE

All Phase 1 steps implemented, tested, passing (`uv run pytest` → 56 passed total).
Coverage: `src/scoring/` ≥ 98%, `src/constraints/` ≥ 95% (meets ≥ 90% requirement).

| Step | File(s) | Status |
|------|---------|--------|
| 1 — Data models | `src/models/{inventory,orders,movements,constraints}.py` | ✅ |
| 2 — WMS adapter | `src/ingestion/wms_adapter.py`, `adapters/generic_db.py`, `dock_schedule.py` | ✅ |
| 3 — Constraint engine | `src/constraints/{feasibility,temperature,hazmat,capacity}.py` | ✅ |
| 4 — Scoring engine | `src/scoring/{value_function,demand_predictor,weights}.py` | ✅ |
| 5 — Scheduler & task queue | `src/optimizer/scheduler.py`, `src/dispatch/{task_queue,agv_interface}.py` | ✅ |
| 6 — API layer | `src/api/main.py`, `src/api/routes/{movements,scoring,config,health}.py` | ✅ |
| 7 — Docker + integration tests | `docker-compose.yml`, `scripts/init_db.sql`, `tests/test_integration.py` | ✅ |

**Validation checklist** (from IMPLEMENTATION.md — all verified by automated tests):
- [x] SKU with confirmed order scores higher than SKU with no order — `test_no_matching_order_returns_zero_score`
- [x] Closer staging location scores higher — `test_farther_from_dock_scores_higher_when_staged_closer`
- [x] Frozen location rejects ambient SKU — `test_ambient_sku_to_frozen_location_fails`
- [x] 95% utilization drives opportunity cost up — `test_utilization_at_cap_raises_opportunity_cost`
- [x] Zero movements when no appointments — `test_integration.py` zero-appointment scenario
- [x] Score explanations populated on all candidates — `test_score_components_stored_on_candidate`
- [x] Task expiration logic implemented — `TaskQueue.expire_old_tasks()` in `task_queue.py`

---

## Phase 2: ML Demand Prediction — COMPLETE

Replaces binary `P_load` with LightGBM probabilistic model. Gated by `use_ml_prediction` feature flag.
26 Phase 2 tests pass. Coverage: `src/prediction/` ≥ 88%.

| Step | File(s) | Status |
|------|---------|--------|
| 8 — Feature engineering | `src/prediction/features.py` | ✅ |
| 9 — Model training + data gen | `src/prediction/trainer.py`, `scripts/generate_training_data.py` | ✅ |
| 10 — Scoring integration | `src/scoring/value_function.py`, `src/api/routes/scoring.py` | ✅ |
| 10a — API startup wires InferenceEngine | `src/api/main.py` (loads model if `use_ml_prediction=True` and model file exists) | ✅ |
| 10b — Scheduler populates `inventory_by_sku` | `src/optimizer/scheduler.py` | ✅ |

**How to activate:**
1. `scripts/generate_training_data.py --db-url ... --out data/training.csv`
2. Train and save model (see HUMAN_TODO.md item 12)
3. Set `prediction.enabled: true` and `prediction.model_path: models/demand_lgbm.pkl` in `config.yml`

---

## Phase 3: OR-Based Optimization — COMPLETE

Replaces greedy top-N dispatch with CP-SAT assignment solver + VRPTW route planner.
27 Phase 3 tests pass. Total test suite: 83 passed.

| Step | File(s) | Status |
|------|---------|--------|
| 11 — Assignment solver | `src/optimizer/assignment.py` | ✅ |
| 12 — Route optimization | `src/optimizer/routing.py` | ✅ |
| 12a — `WarehouseGraph` model | `src/optimizer/routing.py` (`WarehouseGraph`, `GraphEdge`) | ✅ |
| 12b — Wire feature flag in scheduler | `src/optimizer/scheduler.py` (`use_or_optimization` flag + `_run_or_cycle`) | ✅ |
| 12c — Tests | `tests/test_optimizer.py` (27 tests) | ✅ |

**How to activate:**
1. Set `optimization.enabled: true` in `config.yml`
2. Optionally set `optimization.solver_timeout_seconds` (default 10) and `optimization.route_optimization: true`
3. Populate `WarehouseGraph` edges from facility floor plan (see HUMAN_TODO.md item 17)

**What changed in the scheduler:**
- `SchedulerConfig` gains `use_or_optimization`, `available_resources`, `solver_timeout_seconds`, `max_staging_distance_meters`
- `run_cycle()` branches: OR path calls `StagingAssignmentSolver` then pushes assigned tasks; greedy path unchanged
- Lazy import of `StagingAssignmentSolver` so module loads cleanly when OR-Tools absent

---

## Phase 4: Reinforcement Learning — COMPLETE

Optional. For large multi-AGV deployments where coordination is the binding constraint.
19 Phase 4 tests pass. Total test suite: 102 passed.

| Step | File(s) | Status |
|------|---------|--------|
| 13 — Simulation environment | `src/simulation/{warehouse_env,digital_twin,reward}.py` | ✅ |
| 14 — Single-agent PPO prototype | `scripts/train_rl.py` | ✅ |
| 14a — Multi-agent MAPPO (Ray RLlib) | `scripts/train_marl.py` | ✅ |
| 14b — ONNX export for production inference | `scripts/export_onnx.py` | ✅ |
| 14c — OR fallback policy + RL inference wrapper | `src/optimizer/rl_policy.py`, `src/optimizer/scheduler.py` | ✅ |

**How to activate:**
1. Train a model: `uv run python scripts/train_rl.py --timesteps 1000000 --out models/ppo_prepos.zip`
   (requires `pip install stable-baselines3[extra]`)
2. Export to ONNX: `uv run python scripts/export_onnx.py --model models/ppo_prepos.zip --out models/policy.onnx --verify`
   (requires `pip install onnx onnxruntime`)
3. Wire at startup in `src/api/main.py` — see HUMAN_TODO.md item 26
4. Set `use_rl_policy: true` in `config.yml` (falls back to OR-Tools or greedy automatically)

**Dispatch priority order in scheduler:**
1. Phase 4 RL policy (when `use_rl_policy=True` and policy loaded) — OR-Tools fallback on NO_OP/failure
2. Phase 3 OR-Tools (when `use_or_optimization=True`)
3. Phase 1/2 greedy top-N

**Pre-requisites:** Phase 3 in production with baseline metrics; accurate digital twin validated against recorded real-world shift data.

---

## Phase 5: Operational Hardening — COMPLETE

Real-time operator interfaces that close the four major Known Gaps.
35 Phase 5 tests pass. Total test suite: 178 passed.

| Step | File(s) | Status |
|------|---------|--------|
| 15 — Rejection store | `src/dispatch/rejection_store.py` | ✅ |
| 16 — Scheduler trigger + status | `src/api/routes/scheduler.py` | ✅ |
| 17 — WebSocket real-time feed | `src/api/websocket.py` | ✅ |
| 18 — Acknowledge + complete endpoints | `src/api/routes/movements.py` | ✅ |

**What changed:**
- `reject_movement` now persists to Redis with configurable TTL; suppresses SKU from next cycle
- `POST /api/v1/movements/{id}/acknowledge` — PENDING → IN_PROGRESS; broadcasts WS event
- `POST /api/v1/movements/{id}/complete` — IN_PROGRESS → COMPLETED; broadcasts WS event
- `GET /api/v1/movements/rejected` — rejection history
- `DELETE /api/v1/movements/{id}/rejection` — lift an active rejection
- `POST /api/v1/scheduler/trigger?reason=...` — manual cycle + broadcasts cycle_complete WS event
- `GET /api/v1/scheduler/status` — cycle count, avg duration, last results
- `GET /api/v1/ws/movements?api_key=...` — WebSocket stream (cycle_complete, task_dispatched, task_status_changed, movement_rejected)
- `TaskQueue.get_task(movement_id)` — retrieve single task by ID
- Scheduler tracks cycle count, last run time, avg duration; filters suppressed SKUs before dispatch
- Background scheduler loop now broadcasts cycle_complete events

---

## Ongoing / Infrastructure

| Task | Priority | Status |
|------|----------|--------|
| DXF floor plan import | — | ✅ Done — `scripts/import_floor_plan.py` + `src/ingestion/floor_plan_parser.py`; 41 tests |
| Calibrate scoring weights with ops team (AHP) | High — affects score quality from day 1 | ⬜ Blocked on human input (`calibrate_weights.py` ready) |
| Backtest on historical data | High — validates scoring before full deploy | ⬜ Blocked on data export (`backtest.py` ready) |
| Wire Prometheus metrics (increment counters in scheduler/task_queue) | High — metrics defined but never incremented | ✅ Done (Phase 2 gap fix) |
| Add `src/dispatch/human_interface.py` (RF gun / tablet push) | Medium | ⬜ Not started |
| Add `src/monitoring/dashboard.py` (Grafana dashboard JSON) | Medium | ⬜ Not started |
| Add `scripts/simulate.py` scenario runner | Low | ⬜ Not started |
| Kubernetes manifests (`deploy/k8s/`) | Low — post-containerisation | ⬜ Not started |
| Terraform (`deploy/terraform/`) | Low — post-containerisation | ⬜ Not started |
| Restrict CORS `allow_origins=["*"]` for production | High security | ⬜ Not started (set via env var) |
| Increase test coverage on `src/ingestion/adapters/generic_db.py` (0%) | Medium | ⬜ Needs DB test fixture (pytest-asyncio + testcontainers) |
| Increase test coverage on `src/monitoring/metrics.py` (0%) | Low | ⬜ Blocked on metric increment wiring |

---

## Known Gaps (code exists, wire-up incomplete)

These are implemented but not yet fully connected end-to-end.

| Gap | Location | Impact | Fix |
|-----|----------|--------|-----|
| Prometheus counters defined but never incremented | `src/monitoring/metrics.py` + all callers | Metrics endpoint returns zero counts | Import and call in `scheduler.py`, `task_queue.py` |
| `reject_movement` endpoint doesn't persist rejections | `src/api/routes/movements.py` | Rejected moves reappear in next cycle | ✅ Fixed — `RejectionStore` persists with TTL; scheduler filters suppressed SKUs |
| `AGVInterface.get_resource_utilization()` returns 0.0 stub | `src/dispatch/agv_interface.py` | C_opportunity always uses WMS-reported util, not real AGV util | Implement real fleet manager API call |
| `TaskQueue` operations not logged to Prometheus | `src/dispatch/task_queue.py` | `queue_depth` and `movements_completed_total` always 0 | ✅ Fixed (Phase 2 gap fix) |
| `GenericDBAdapter` column mappings not validated at startup | `src/ingestion/adapters/generic_db.py` | Silent data gaps if WMS schema differs | Add schema validation on `connect()` |
| Dock door x/y coordinates are a stub (`door * 5m`) | `src/scoring/value_function.py:_dock_door_coords` | T_saved calculations are approximate | ✅ Configurable via `MovementScorer(dock_door_coords={...})` and `_DEFAULT_DOCK_DOOR_COORDS`; placeholder logs warning (see HUMAN_TODO.md item 3) |
