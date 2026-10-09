# MVP interface contract

The replay harness imports the copied original Phase 1 optimizer via optimizer_bridge.py and calls PrePositionScheduler.generate_candidates, FeasibilityEngine and MovementScorer. It never calls live dispatch. Launch: .venv-sandbox/bin/python -m warehouse_sandbox.server --port 8876. HTTP binds only to 127.0.0.1.

## Dataset

JSON keys:
- schema_version: "1.0"
- facility: {name: string, start_time: timezone-aware ISO datetime}
- locations: [{id, x, y, kind: "storage"|"staging"|"dock", capacity_pallets: integer >=1, temperature: "ambient"|"chilled"|"frozen" (optional default ambient)}]
- docks: [{id, location_id, staging_location_id}]
- forklifts: [{id, start_location_id}]
- inventory: [{pallet_id, sku_id, owner_id, location_id, quantity: positive integer, temperature: optional default ambient}]
- orders: [{order_id, owner_id, release_seconds: nonnegative float, scheduled_arrival_seconds: optional nonnegative planned arrival, arrival_seconds: nonnegative actual arrival, deadline_seconds: nonnegative float, dock_id, appointment_id: optional string, lines: [{pallet_id, quantity: positive integer}], observed_departure_seconds: optional nonnegative float}]
- historical_moves: [{at_seconds: nonnegative float, pallet_id, to_location_id}] (optional)

Coordinates are metres. Times are seconds since facility.start_time. Each inventory record is a unique physical pallet; quantity is units on that pallet. Each order line is one forklift loading visit, including partial quantity. Orders sharing appointment_id form one truck; their dock, actual/planned arrival, deadline and any supplied observed departures must agree. The policy sees scheduled arrivals only after release; actual arrivals are simulator events. Missing scheduled arrivals fall back to actual arrivals with an explicit hindsight warning. Historical records are customer-authorized observations, never advance optimizer inputs. Validation checks ownership, references, total requested quantity, temperatures, finite numbers, timeline, unique IDs and sensible limits. A history move is an observed completed relocation attempted in both branches; its labor remains unmeasured. Conflicts with consumed/busy pallets or destination capacity are reported per branch. Zero adoption preserves the baseline's operations. Simulated results are model estimates, not causal proof or guaranteed ROI.

## Run request

POST /api/run body {dataset: Dataset, settings: Settings}
Settings: forklift_count (1..20, default dataset forklifts length), speed_mps (0.2..8, default 2), handling_seconds (0..300, default 30), loading_seconds (0..300, default 25), lookahead_minutes (1..180, default 30), min_benefit_seconds (0..600, default 5), min_optimizer_score (0..100, default 0.1), adoption_percent (0..100 default 100), seed (integer default 42).

Engine function run_comparison(dataset: dict, settings: dict|None = None) -> dict. Original optimizer imports execute calculation-only against in-memory snapshots; no live IO, HTTP or DB path runs. Always clone inventory per branch. Simulator sees orders only at release_seconds, never future releases or observed departures. Generate suggestions from available pallets for known orders within lookahead, currently idle forklifts, staging capacity and temperature eligibility. Baseline and suggestion branches share inputs/settings. Movement time derives from actual Manhattan travel plus handling; no preset staging speedup. Model forklift current location, pallet quantity consumption, mutual exclusion, dock queues, loading priorities, travel back from dock, staging reservations and releases. Do not begin suggestions if loading work is waiting. Deterministic fixed-seed adoption applies per proposal. Both branches finish after the final shipment, bounded safety guard.

## Run response

{
 run_id: string (server adds), model_version: string, policy: {id: "original_optimizer_phase1", source_modules: [...], live_dispatch_enabled: false, clock: "simulated"}, settings: Settings,
 baseline_label: "Historical replay" if historical_moves else "Current policy model",
 baseline: {metrics: {truck_count, avg_dwell_seconds, p95_dwell_seconds, late_trucks, total_lateness_seconds, travel_meters, forklift_busy_seconds, makespan_seconds, completed_units, remaining_units, historical_moves_applied, historical_move_conflicts, historical_move_labor_unmeasured}, trucks: [{order_id, owner_id, dock_id, arrival_seconds, departure_seconds, deadline_seconds, dwell_seconds, late_seconds, observed_departure_seconds?}], events: [{at_seconds, type, resource_id?, order_id?, pallet_id?, from_location_id?, to_location_id?, duration_seconds?, distance_meters?, quantity?}], final_inventory: [...], suggestions: []},
 proposed: same branch shape, suggestions: [{id, at_seconds, pallet_id, sku_id, owner_id, order_id, from_location_id, to_location_id, dock_id, estimated_load_travel_saved_seconds, move_cost_seconds, score: original V(m), score_components: original component mapping, policy_source: original generator module, optimizer_reason: original reason, adopted: bool, reason}],
 comparison: {avg_dwell_saved_seconds, avg_dwell_saved_percent, late_trucks_avoided, travel_delta_meters, extra_reposition_seconds},
 warnings: [string], assumptions: [string],
 calibration: {observed_trucks, mae_departure_seconds: float|null}
}

## HTTP API

GET /api/health -> {status:"ok", mode:"historical_simulation", production_connections:false}
GET /api/demo -> dataset JSON
GET /api/schema -> {dataset_example: dataset JSON, csv_tables: metadata, settings: defaults, limits: metadata}
POST /api/import -> {format:"json", data: dict|string} OR {format:"csv", files:{"locations.csv":csv_text,"docks.csv":csv_text,"forklifts.csv":csv_text,"inventory.csv":csv_text,"orders.csv":csv_text,"historical_moves.csv":csv_text optional}, facility_name:string optional,start_time:ISO optional}
CSV orders are flattened order lines, repeats allowed: order_id,owner_id,release_seconds,arrival_seconds,deadline_seconds,dock_id,pallet_id,quantity,observed_departure_seconds(optional),scheduled_arrival_seconds(optional),appointment_id(optional).
JSON import accepts a canonical dataset or a full exported run wrapper containing dataset and settings, including UTF-8 BOM text. Import response {dataset, settings?: restored normalized scenario settings, summary:{facility_name,pallets,orders,locations,docks,forklifts,history_moves,units},warnings:[]}. Validation errors HTTP 400 {error:string,details:[string]}.
POST /api/run -> RunResponse (server validates)
GET /api/runs/<id> -> RunResponse
GET /api/runs/<id>/export?format=json -> downloadable full JSON
GET /api/runs/<id>/export?format=csv -> downloadable suggestions CSV
Server stores runs under local runs/; local datasets uploaded are not retained until a run is saved. Limits: 5MB request, 500 inventory pallets, 200 orders, 2000 order lines, 500 historical moves. No external network calls, warehouse credentials, equipment dispatch, database reads, webhooks or writeback endpoints. Standard library HTTP server is a local prototype, not public hosting.

Optional order priority is an integer1..10/default1; CSV priority is optional. Candidate snapshots are scoped to one physical pallet and its earliest released matching order/appointment to preserve owner identity despite the original SKU deduplication. Source scores determine rank; simulator guards remain responsible for current slots, reservations and loading priority.
