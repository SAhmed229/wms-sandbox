# Warehouse Pre-Positioning Optimizer: Claude Code Implementation Guide

## System Overview

This guide walks through building an external reasoning system that reads WMS state, scores internal repositioning movements, and dispatches tasks to AGVs or human forklift drivers to pre-stage inventory near outbound loading bays. The system is WMS-agnostic, constraint-aware, and deployable in four phases.

The build order is strict: Phase 1 delivers value in days, each subsequent phase layers on top without replacing prior work.

---

## Project Structure

```
warehouse-preposition-optimizer/
├── CLAUDE.md                          # Claude Code project instructions
├── pyproject.toml                     # Python project config (uv/poetry)
├── docker-compose.yml                 # Local dev: Postgres + Redis + API
├── .env.example                       # Environment variable template
├── src/
│   ├── __init__.py
│   ├── config.py                      # Pydantic settings, feature flags
│   ├── models/
│   │   ├── __init__.py
│   │   ├── inventory.py               # SKU, Location, InventoryPosition
│   │   ├── orders.py                  # OutboundOrder, CarrierAppointment
│   │   ├── movements.py               # CandidateMovement, MovementTask
│   │   └── constraints.py             # TemperatureZone, WeightLimit, HazmatClass
│   ├── ingestion/
│   │   ├── __init__.py
│   │   ├── wms_adapter.py             # Abstract WMS interface
│   │   ├── adapters/
│   │   │   ├── sap_ewm.py             # SAP EWM adapter
│   │   │   ├── manhattan.py           # Manhattan Associates adapter
│   │   │   ├── blue_yonder.py         # Blue Yonder adapter
│   │   │   └── generic_db.py          # Direct database polling adapter
│   │   └── dock_schedule.py           # Carrier appointment ingestion
│   ├── scoring/
│   │   ├── __init__.py
│   │   ├── value_function.py          # V(m) scoring engine
│   │   ├── demand_predictor.py        # P(load) probability estimator
│   │   └── weights.py                 # Weight calibration (AHP + grid search)
│   ├── constraints/
│   │   ├── __init__.py
│   │   ├── feasibility.py             # Hard constraint filter
│   │   ├── temperature.py             # Refrigeration/frozen zone enforcement
│   │   ├── hazmat.py                  # Hazmat segregation rules
│   │   └── capacity.py               # Rack weight limits, lane capacity
│   ├── optimizer/
│   │   ├── __init__.py
│   │   ├── assignment.py              # SKU-to-staging-location assignment (OR-Tools)
│   │   ├── routing.py                 # Forklift/AGV route optimization (VRPTW)
│   │   └── scheduler.py              # Movement sequencing and dispatch
│   ├── dispatch/
│   │   ├── __init__.py
│   │   ├── task_queue.py              # Priority queue for movement tasks
│   │   ├── agv_interface.py           # AGV fleet manager API client
│   │   └── human_interface.py         # RF gun / tablet task push
│   ├── prediction/                    # Phase 2: ML layer
│   │   ├── __init__.py
│   │   ├── features.py                # Feature engineering pipeline
│   │   ├── trainer.py                 # LightGBM model training
│   │   └── inference.py               # Real-time prediction serving
│   ├── simulation/                    # Phase 4: RL training environment
│   │   ├── __init__.py
│   │   ├── warehouse_env.py           # Gymnasium environment
│   │   ├── digital_twin.py            # SimPy discrete-event model
│   │   └── reward.py                  # Reward function definitions
│   ├── api/
│   │   ├── __init__.py
│   │   ├── main.py                    # FastAPI application
│   │   ├── routes/
│   │   │   ├── movements.py           # GET /movements, POST /movements/{id}/approve
│   │   │   ├── scoring.py             # GET /scoring/candidates
│   │   │   ├── config.py              # PUT /config/weights
│   │   │   └── health.py              # GET /health, GET /metrics
│   │   └── websocket.py              # Real-time movement feed
│   └── monitoring/
│       ├── __init__.py
│       ├── metrics.py                 # Prometheus metrics
│       └── dashboard.py              # Grafana dashboard definitions
├── tests/
│   ├── conftest.py                    # Fixtures: mock warehouse, sample orders
│   ├── test_scoring.py
│   ├── test_constraints.py
│   ├── test_optimizer.py
│   └── test_integration.py
├── scripts/
│   ├── calibrate_weights.py           # AHP weight calibration wizard
│   ├── backtest.py                    # Score historical movements
│   └── simulate.py                   # Run simulation scenarios
└── deploy/
    ├── Dockerfile
    ├── k8s/
    └── terraform/
```

---

## CLAUDE.md

Place this file at the project root. Claude Code reads it automatically on every session.

```markdown
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
```

---

## Phase 1: Weighted Scoring MVP

This phase delivers a working system with no ML dependencies. Estimated build time with Claude Code: 3-5 days.

### Step 1: Data Models

Prompt Claude Code:

```
Create Pydantic v2 models in src/models/ for:

1. inventory.py:
   - Location: zone (str), aisle (int), bay (int), level (int), lat/lon or x/y coordinates,
     temperature_zone (enum: AMBIENT, CHILLED, FROZEN), max_weight_kg (float),
     is_staging (bool), nearest_dock_door (int)
   - SKU: sku_id (str), description (str), weight_kg (float), volume_m3 (float),
     hazmat_class (optional enum), requires_temperature_zone (enum),
     abc_class (enum: A, B, C)
   - InventoryPosition: sku (SKU), location (Location), quantity (int),
     lot_number (optional str), expiry_date (optional datetime)

2. orders.py:
   - CarrierAppointment: appointment_id (str), carrier (str), dock_door (int),
     scheduled_arrival (datetime), scheduled_departure (datetime),
     status (enum: SCHEDULED, CHECKED_IN, LOADING, DEPARTED)
   - OutboundOrder: order_id (str), appointment (CarrierAppointment),
     lines (list of OrderLine), priority (int 1-10), cutoff_time (datetime)
   - OrderLine: sku_id (str), quantity (int), picked (bool)

3. movements.py:
   - CandidateMovement: movement_id (uuid), sku_id (str),
     from_location (Location), to_location (Location),
     score (float), score_components (dict mapping term name to float),
     reason (str human-readable), estimated_duration_seconds (int)
   - MovementTask: extends CandidateMovement with
     assigned_resource (str), status (enum: PENDING, IN_PROGRESS, COMPLETED, CANCELLED),
     dispatched_at (datetime), completed_at (optional datetime)

4. constraints.py:
   - ConstraintViolation: constraint_type (str), description (str), severity (enum: HARD, SOFT)
   - FeasibilityResult: feasible (bool), violations (list of ConstraintViolation)

All models must have model_config with from_attributes=True for ORM compatibility.
```

### Step 2: WMS Adapter Interface

```
Create src/ingestion/wms_adapter.py with an abstract base class WMSAdapter:

Methods (all async):
- get_inventory_positions(zone: str | None) -> list[InventoryPosition]
- get_outbound_orders(horizon_hours: float = 24) -> list[OutboundOrder]
- get_carrier_appointments(horizon_hours: float = 24) -> list[CarrierAppointment]
- get_staging_locations(dock_door: int | None) -> list[Location]
- get_location_utilization() -> dict[str, float]  # location_id -> percent full

Then create src/ingestion/adapters/generic_db.py that implements WMSAdapter
by polling a PostgreSQL database. Use SQLAlchemy async with configurable table
names and column mappings via a YAML config file. The adapter should:
- Poll on a configurable interval (default 30 seconds)
- Cache results in Redis with configurable TTL
- Emit metrics on poll duration and record counts
```

### Step 3: Constraint Engine

```
Create src/constraints/feasibility.py:

class ConstraintFilter(ABC):
    @abstractmethod
    def check(self, movement: CandidateMovement, warehouse_state: WarehouseState) -> FeasibilityResult

class FeasibilityEngine:
    def __init__(self, filters: list[ConstraintFilter])
    def evaluate(self, movement: CandidateMovement, state: WarehouseState) -> FeasibilityResult
        """Runs all filters. Returns infeasible on first HARD violation. Collects SOFT violations."""

Then implement these filters in separate files:
1. temperature.py - TemperatureConstraint: SKU.requires_temperature_zone must match
   Location.temperature_zone. HARD constraint, zero exceptions.
2. hazmat.py - HazmatConstraint: enforce DOT segregation table. Two incompatible
   hazmat classes cannot share the same bay. HARD constraint.
3. capacity.py - CapacityConstraint: target location must have available capacity
   (weight and volume). HARD constraint.

Each filter must be independently testable. Write tests in tests/test_constraints.py
with at least: ambient SKU -> frozen location (fail), frozen SKU -> frozen location (pass),
hazmat class 3 next to class 5.1 (fail), overweight pallet (fail), within-weight pallet (pass).
```

### Step 4: Scoring Engine

```
Create src/scoring/value_function.py:

class MovementScorer:
    def __init__(self, weights: ScoringWeights, warehouse_layout: WarehouseLayout)

    def score(self, candidate: CandidateMovement, context: ScoringContext) -> float:
        """
        V(m) = (T_saved * P_load * W_order) / (C_move + C_opportunity)

        Each term is computed by a private method and stored in
        candidate.score_components for explainability.
        """

    def _compute_time_saved(self, from_loc: Location, to_loc: Location, dock_door: int) -> float:
        """
        T_saved = distance(from_loc, dock_door) - distance(to_loc, dock_door)
        Distance uses Manhattan distance on the warehouse grid in seconds,
        factoring in aisle traversal speed (loaded forklift ~5 mph, AGV ~3 mph).
        """

    def _compute_load_probability(self, sku_id: str, appointment: CarrierAppointment,
                                    orders: list[OutboundOrder]) -> float:
        """
        Phase 1: P_load = 1.0 if SKU appears on an order linked to this appointment,
        0.0 otherwise. Phase 2 replaces this with ML prediction.
        """

    def _compute_order_weight(self, order: OutboundOrder) -> float:
        """
        W_order = priority * urgency_multiplier
        urgency_multiplier = exp(-time_until_cutoff / decay_constant)
        decay_constant default: 3600 (1 hour). Configurable.
        """

    def _compute_movement_cost(self, from_loc: Location, to_loc: Location) -> float:
        """
        C_move = travel_time(from, to) + handling_time
        handling_time: 45 seconds for forklift (approach + lift + place), configurable.
        """

    def _compute_opportunity_cost(self, resource_utilization: float) -> float:
        """
        C_opportunity = base_opportunity * (1 / (1 - utilization))
        Approaches infinity as utilization -> 1.0, preventing dispatch
        when resources are fully committed. Clamped at utilization=0.95.
        """

Also create src/scoring/weights.py:
- ScoringWeights: Pydantic model with w1-w5 floats and decay_constant
- Default weights: all 1.0
- Load from config file, overridable via API

Write comprehensive tests for each term including edge cases:
- zero distance (T_saved = 0, score should be 0)
- cutoff time in the past (urgency very high)
- utilization at 0.95 cap
- no matching orders (P_load = 0, score = 0)
```

### Step 5: Candidate Generator and Dispatcher

```
Create src/optimizer/scheduler.py:

class PrePositionScheduler:
    def __init__(self, scorer: MovementScorer, feasibility: FeasibilityEngine,
                 wms: WMSAdapter, config: SchedulerConfig)

    async def generate_candidates(self) -> list[CandidateMovement]:
        """
        1. Get all outbound orders within horizon
        2. For each order line, find current inventory position
        3. For each inventory position, find candidate staging locations
           near the assigned dock door
        4. Create CandidateMovement for each (inventory_position, staging_location) pair
        5. Filter through FeasibilityEngine (discard infeasible)
        6. Score remaining candidates
        7. Sort by score descending
        8. Deduplicate: if same SKU appears multiple times, keep highest-scored only
        9. Return top N candidates (configurable, default 50)
        """

    async def dispatch_top_movements(self, n: int = 5) -> list[MovementTask]:
        """
        Take top N candidates, convert to MovementTask, push to task queue.
        Each dispatch reduces available resources, which feeds back into
        C_opportunity for remaining candidates via re-scoring.
        """

Create src/dispatch/task_queue.py:
- Redis-backed priority queue
- Tasks expire if not started within configurable window (default 15 min)
- Status transitions: PENDING -> IN_PROGRESS -> COMPLETED | CANCELLED
- Completion callback triggers re-scoring of remaining candidates

The scheduler runs on a configurable loop (default every 60 seconds) or
on trigger events (new order, appointment check-in, task completion).
```

### Step 6: API Layer

```
Create a FastAPI application in src/api/main.py with these routes:

GET  /api/v1/movements/candidates
     Query params: limit (int), min_score (float), dock_door (int optional)
     Returns scored candidate list with explanations

POST /api/v1/movements/{movement_id}/approve
     Manually approve a candidate, converting it to a dispatched task

POST /api/v1/movements/{movement_id}/reject
     Reject a candidate with reason, feeds learning loop

GET  /api/v1/movements/active
     Currently dispatched tasks with status

GET  /api/v1/scoring/explain/{movement_id}
     Detailed breakdown of all score components for a single movement

PUT  /api/v1/config/weights
     Update scoring weights at runtime without restart
     Body: ScoringWeights model

GET  /api/v1/config/weights
     Current weights

GET  /api/v1/health
     System health: WMS connectivity, Redis, queue depth, last poll time

GET  /api/v1/metrics
     Prometheus-format metrics: movements_scored_total, movements_dispatched_total,
     avg_score, avg_time_saved_seconds, queue_depth

WebSocket /api/v1/ws/movements
     Real-time feed of scored candidates and task status changes

Add CORS middleware, API key auth, request logging.
Include OpenAPI docs with detailed descriptions.
```

### Step 7: Docker Compose and Integration Test

```
Create docker-compose.yml with:
- postgres:16 with init script seeding sample warehouse data
  (100 locations across 3 zones, 50 SKUs, 10 pending orders, 4 dock appointments)
- redis:7
- app service building from Dockerfile

Create tests/test_integration.py that:
1. Starts with seeded database
2. Runs one scheduling cycle
3. Asserts: candidates are generated, scored > 0, feasibility checked,
   top candidate has highest score, no constraint violations in output
4. Dispatches top 3 tasks
5. Asserts: tasks in queue, resource utilization increased,
   re-scored candidates have higher C_opportunity
6. Completes one task
7. Asserts: task marked completed, resource freed, metrics updated
```

---

## Phase 2: ML Demand Prediction

Replaces the binary `P_load` with a probabilistic model. Estimated build time: 1-2 weeks.

### Step 8: Feature Engineering

```
Create src/prediction/features.py:

class FeatureBuilder:
    def build_features(self, sku_id: str, dock_door: int,
                        window_start: datetime, window_end: datetime,
                        historical_data: HistoricalData) -> dict:
        """
        Features for predicting P(SKU_i loaded at dock_j in [window_start, window_end]):

        Temporal:
        - hour_of_day (cyclical: sin/cos encoding)
        - day_of_week (cyclical)
        - days_until_month_end
        - is_holiday (bool)

        SKU-level:
        - abc_class (ordinal: A=3, B=2, C=1)
        - avg_daily_demand_30d
        - demand_coefficient_of_variation_30d
        - days_since_last_shipment
        - current_on_hand_quantity
        - pending_order_quantity (confirmed orders not yet shipped)

        Dock-level:
        - carrier_id (categorical, label encoded)
        - carrier_historical_sku_frequency (how often this carrier ships this SKU)
        - appointment_duration_minutes
        - dock_door_zone_match (1 if dock door is near SKU's storage zone)

        Order pipeline:
        - order_exists_for_sku (binary: is there a confirmed order?)
        - order_priority (0 if no order, else 1-10)
        - minutes_until_cutoff (0 if no order)
        - order_fill_rate (fraction of order lines already picked)

        Return as flat dict with float values only. No nulls; impute with 0 or median.
        """
```

### Step 9: Model Training

```
Create src/prediction/trainer.py:

class DemandPredictor:
    def train(self, training_data: pd.DataFrame, target_col: str = "was_loaded") -> None:
        """
        Train LightGBM binary classifier.

        training_data: one row per (SKU, dock_door, time_window) triple,
        with features from FeatureBuilder and binary target was_loaded.

        Use TimeSeriesSplit (5 folds) for cross-validation.
        Log AUC-ROC, precision@k (k=50 top candidates per cycle), and calibration curve.

        Hyperparameter search: Optuna with 50 trials over:
        - num_leaves: [15, 63]
        - learning_rate: [0.01, 0.3]
        - min_child_samples: [5, 50]
        - feature_fraction: [0.5, 1.0]
        - reg_alpha: [0, 10]
        - reg_lambda: [0, 10]

        Save model artifact, feature importance (SHAP), and calibration params.
        """

    def predict(self, features: dict) -> float:
        """Return calibrated probability [0.0, 1.0]."""

    def explain(self, features: dict) -> dict:
        """Return SHAP values for each feature."""

Create scripts to generate training data from historical WMS exports:
- Extract all (SKU, dock_door, 2-hour window) combinations for past 90 days
- Label: 1 if SKU was loaded at that dock in that window, 0 otherwise
- Handle class imbalance: most combinations are 0. Use SMOTE or class_weight.
```

### Step 10: Integration with Scoring

```
Modify src/scoring/value_function.py:

Add a feature flag USE_ML_PREDICTION (from config). When enabled:
- _compute_load_probability calls DemandPredictor.predict instead of binary lookup
- Score explanation includes SHAP-based feature contributions
- Fallback to binary lookup if prediction service is unavailable (circuit breaker)

The scorer interface does not change. Tests must cover both code paths.
```

---

## Phase 3: OR-Based Optimization

Replaces greedy top-N dispatch with globally optimal assignment. Estimated build time: 2-4 weeks.

### Step 11: Assignment Solver

```
Create src/optimizer/assignment.py using Google OR-Tools CP-SAT solver:

class StagingAssignmentSolver:
    def solve(self, candidates: list[CandidateMovement],
              staging_locations: list[Location],
              available_resources: int,
              time_horizon_minutes: int = 120) -> list[MovementTask]:
        """
        Solve: which SKUs go to which staging locations to maximize total value?

        Decision variables:
        - x[i][j] = 1 if candidate i assigned to staging location j

        Objective: maximize sum(x[i][j] * score[i] for all i,j)

        Constraints:
        - Each candidate assigned to at most one location
        - Each location holds at most one pallet (or capacity-based)
        - Total movements <= available_resources * time_horizon / avg_move_time
        - Temperature zone compatibility (redundant with feasibility but enforced here too)
        - No two hazmat-incompatible SKUs in adjacent staging slots
        - Staging location must be within max_distance of assigned dock door

        Returns ordered list of MovementTasks with assigned staging locations.
        Solver timeout: 10 seconds (configurable). Return best solution found.
        """
```

### Step 12: Route Optimization

```
Create src/optimizer/routing.py using OR-Tools routing library:

class MovementRoutePlanner:
    def plan_routes(self, tasks: list[MovementTask],
                     resources: list[Resource],
                     warehouse_graph: WarehouseGraph) -> list[Route]:
        """
        Solve VRPTW (Vehicle Routing Problem with Time Windows):
        Given assigned movements, what is the optimal sequence for each forklift/AGV?

        Each resource starts at its current position.
        Each task has a pickup location, a dropoff location, and a time window
        (must start before appointment arrival minus buffer).

        Minimize total travel time across all resources.
        Constraint: resource can carry one pallet at a time (capacitated).

        WarehouseGraph encodes:
        - Aisle connectivity (which aisles connect to which)
        - One-way aisles (if any)
        - Speed zones (slower in pedestrian areas)
        - Blocked paths (temporary obstructions)

        Returns list of Route objects, each with ordered stops and estimated times.
        """
```

---

## Phase 4: Reinforcement Learning

Optional phase for large-scale multi-AGV deployments. Estimated build time: 2-4 months.

### Step 13: Simulation Environment

```
Create src/simulation/warehouse_env.py:

class WarehousePrePositionEnv(gymnasium.Env):
    """
    Gymnasium environment wrapping a SimPy discrete-event warehouse simulation.

    State space (Box):
    - Inventory grid: (num_locations, num_skus) matrix of quantities
    - Order queue: (max_orders, features_per_order) matrix
    - Dock schedule: (num_docks, schedule_features) matrix
    - Resource positions: (num_resources, 2) x/y coordinates
    - Time features: hour_of_day, minutes_until_next_appointment

    Action space (MultiDiscrete):
    - For each available resource: (candidate_movement_index) or NO_OP
    - Action masking via action_masks() method to prevent infeasible actions

    Reward function (defined in src/simulation/reward.py):
    - +R1 for each second saved during actual truck loading (primary signal)
    - -R2 for each movement executed (cost of work)
    - +R3 bonus for truck departing before scheduled_departure
    - -R4 penalty for truck departing after scheduled_departure
    - Shaped reward: small positive for reducing average distance-to-dock
      of ordered inventory (guides exploration)

    Episode: one 8-hour shift. Terminates at shift end.
    Step: one scheduling decision (variable time between steps,
    triggered by resource availability or new order/appointment event).
    """

Create src/simulation/digital_twin.py using SimPy:
    """
    Discrete-event simulation of warehouse operations:
    - Forklifts/AGVs as SimPy resources with travel time based on distance
    - Orders arrive according to historical inter-arrival distribution
    - Trucks arrive/depart per carrier appointment schedule
    - Loading process: for each order line, forklift travels from staging/storage
      to truck, load time depends on distance
    - Pre-positioned inventory: if SKU is in staging near dock, loading is faster
    - Metrics tracked: total loading time per truck, dock dwell time,
      resource utilization, movements executed
    """
```

### Step 14: Training Pipeline

```
Create a training script using Stable Baselines3 (prototyping) or Ray RLlib (production):

Stable Baselines3 (single-agent prototype):
- Algorithm: PPO with MLP policy
- Training: 1M timesteps with evaluation every 50K
- Hyperparameters: learning_rate=3e-4, n_steps=2048, batch_size=64,
  n_epochs=10, gamma=0.99, gae_lambda=0.95

Ray RLlib (multi-agent production):
- Algorithm: MAPPO (multi-agent PPO)
- Each AGV/forklift is an agent with shared policy
- Centralized critic, decentralized execution
- Training: distributed across GPUs, 10M+ timesteps
- Domain randomization: vary order volumes, appointment times,
  SKU distributions to improve generalization

Export trained policy to ONNX for production inference.
Maintain OR-based solver as fallback: if RL policy produces infeasible
action or confidence is low, fall back to Phase 3 solver.
```

---

## Deployment Sequence

Each command block below is a Claude Code prompt you can run in sequence.

### Initial Setup

```
Initialize a Python project using uv with Python 3.12.
Add dependencies: fastapi, uvicorn, pydantic>=2.0, sqlalchemy[asyncio],
asyncpg, redis, httpx, structlog, prometheus-client.
Add dev dependencies: pytest, pytest-asyncio, pytest-cov, ruff, mypy.
Create the project structure from the guide. Set up ruff config targeting
Python 3.12 with I (isort), E, W, F rules. Set up mypy strict mode.
```

### Phase 1 Build Sequence

```
1. "Implement the Pydantic models as specified in Step 1."
2. "Implement WMSAdapter ABC and generic_db adapter as specified in Step 2."
3. "Implement constraint filters and FeasibilityEngine as specified in Step 3. Include all tests."
4. "Implement MovementScorer with all five terms as specified in Step 4. Include all tests."
5. "Implement PrePositionScheduler and Redis task queue as specified in Step 5."
6. "Implement FastAPI routes as specified in Step 6."
7. "Create docker-compose.yml with seed data and integration tests as specified in Step 7."
8. "Run all tests and fix any failures. Target 90%+ coverage on scoring and constraints."
```

### Validation Checklist

After Phase 1, verify these properties hold:

- A SKU with a confirmed order for an approaching appointment scores higher than a SKU with no order.
- A staging location 50 feet from the dock door scores higher than one 200 feet away for the same SKU.
- A movement to a frozen location for an ambient-only SKU is rejected by the constraint engine.
- When resource utilization reaches 95%, opportunity cost drives all scores below the dispatch threshold.
- The system produces zero movements when there are no upcoming appointments within the horizon.
- Score explanations in the API correctly attribute each component's contribution.
- Task expiration works: a PENDING task not started within 15 minutes auto-cancels and triggers re-scoring.

---

## Configuration Reference

```yaml
# config.yml
scoring:
  weights:
    time_saved: 1.0
    load_probability: 1.0
    order_priority: 1.0
    movement_cost: 1.0
    opportunity_cost: 1.0
  decay_constant_seconds: 3600
  max_candidates_per_cycle: 50
  min_score_threshold: 0.1

scheduling:
  cycle_interval_seconds: 60
  dispatch_batch_size: 5
  task_expiry_minutes: 15
  horizon_hours: 24

resources:
  forklift_speed_mps: 2.2       # ~5 mph
  agv_speed_mps: 1.3            # ~3 mph
  handling_time_seconds: 45
  max_utilization: 0.95

constraints:
  enforce_temperature: true
  enforce_hazmat: true
  enforce_capacity: true
  max_staging_distance_meters: 50

prediction:
  enabled: false                 # Phase 2 feature flag
  model_path: models/demand_lgbm.pkl
  fallback_on_error: true
  prediction_cache_ttl_seconds: 300

optimization:
  enabled: false                 # Phase 3 feature flag
  solver_timeout_seconds: 10
  route_optimization: false

wms:
  adapter: generic_db
  poll_interval_seconds: 30
  cache_ttl_seconds: 60
  connection_string: ${DATABASE_URL}
```

---

## Metrics to Track from Day 1

Instrument these from the start. They validate ROI and guide weight calibration.

| Metric | Formula | Target |
|---|---|---|
| Avg truck loading time | `sum(loading_duration) / num_trucks` | 30%+ reduction |
| Pre-stage hit rate | `loads_from_staging / total_loads` | 60%+ |
| Dock dwell time | `truck_departure - truck_arrival` | 20%+ reduction |
| Movement ROI | `total_time_saved / total_movement_cost` | > 2.0 |
| Constraint violation rate | `violations_caught / candidates_generated` | Monitoring only |
| Score prediction accuracy | `correlation(predicted_score, actual_time_saved)` | Phase 2+ |
| Resource utilization | `active_time / available_time` | 70-85% |
| False positive rate | `movements_executed_but_SKU_not_loaded / total_movements` | < 20% |