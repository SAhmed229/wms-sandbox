# Warehouse Replay MVP

This desktop copy adds a local historical replay sandbox to the original project. Import authorized warehouse exports, generate pallet staging suggestions, and simulate what could have happened if those suggestions had been adopted. Compare truck dwell, deadline misses, forklift travel and added staging effort against a baseline under the same assumptions.

The application generates suggestions only. The running sandbox has no WMS connector, warehouse credentials, database adapter, production writeback, webhook, or equipment dispatch. Recommendations now call the copied repository's actual Phase 1 candidate generator, feasibility filters and movement scorer; the replay engine models their hypothetical consequences. All uploads, simulations and saved results stay on this computer.

## Start

Double-click **Launch Sandbox.command**, then open **http://127.0.0.1:8876**. Keep the terminal open while using the application. Press Control-C in that terminal to stop it.

Alternatively, from this folder using the installed isolated Python 3.12 environment:

```sh
./.venv-sandbox/bin/python -m warehouse_sandbox.server --port 8876
```

The original optimizer dependencies are installed in .venv-sandbox using Python 3.12. No database, Redis service, Docker, API key or runtime internet connection is required. To set up another copy, create .venv-sandbox with Python 3.12 and run .venv-sandbox/bin/python -m pip install -r requirements-sandbox.txt. The server binds to this computer's loopback address and is intended for local use.

## First scenario

1. Load the synthetic demo. It contains 24 uniquely identified pallets, eight shipments, two inventory owners, two dock doors and three forklifts.
2. Select **Run comparison** with the default settings. The **Results** tab compares the baseline and recommendation branch, including added equipment work alongside truck dwell. Use **Replay** for the warehouse timeline, **Recommendations** for staging decisions and reasons, and **Model & data** for imports, calibration and assumptions.
3. Change adoption percentage, available forklifts, travel speed, handling time or lookahead. Run a new scenario and compare outcomes.
4. Download the complete result JSON or the suggestions CSV. Runs are saved under `runs/` in this folder.

The JSON export contains the imported dataset. Treat exported and locally saved results as confidential customer data. Delete local run JSON files when they are no longer needed.

## Import historical data

Use **Import data** in the browser to select a JSON dataset or select the CSV tables together. Download the CSV template ZIP from the application, or inspect `examples/sandbox/csv/` and `examples/sandbox/demo.json` locally. All timestamps are seconds since the timezone-aware facility start time; all coordinates are metres.

Required CSV tables are `locations.csv`, `docks.csv`, `forklifts.csv`, `inventory.csv` and `orders.csv`. The `historical_moves.csv` table is optional. Each inventory record is one physical pallet with a unique `pallet_id`; `quantity` is units on that pallet. Orders can consume a partial quantity, including across multiple shipments. The flattened orders CSV repeats order metadata for each order line.

Use an initial inventory snapshot from the beginning of the replay period. Orders include the time the order became known, planned truck arrival (`scheduled_arrival_seconds`), actual arrival (`arrival_seconds`), deadline and required pallet quantities. The policy uses planned arrivals; actual arrivals only drive simulator events. Without planned arrivals the app warns that the fallback can introduce hindsight. Optional `appointment_id` groups multiple customer orders onto one truck. Optional observed departure times are used for calibration only and never influence suggestions. Historical relocations describe completed movements attempted in both branches, with intervention conflicts reported.

You can import a complete downloaded run JSON as well as a raw dataset. A full run restores its scenario settings. The command line also accepts its own saved output; an explicit settings file overrides restored values.

When historical relocations have no measured duration, their labor is unknown. The app flags that limitation; forklift effort comparisons cannot be treated as a complete accounting of actual historical labor. Without relocation history, the baseline is a modeled loading policy evaluated on historical demand. It is not a replay of every action taken by the actual warehouse.

## Model and interpretation

Both branches begin with independent copies of the same inventory and attempt the same recorded historical relocations. Zero adoption reproduces baseline operations. Loading and repositioning compete for the same forklift fleet. The model tracks each forklift's location, Manhattan travel distance, handling and loading time, pallet quantities, staging reservations, dock queues and customer ownership. Suggestions can use only orders released by the current replay time. The model adds no automatic speedup just because a pallet has been staged.

The suggestion branch uses idle time to stage eligible pallets before a known truck arrives. Current loading work takes priority over starting a new staging task. Adoption is deterministic at a fixed seed. A partially completed staging movement is still non-preemptive and may delay later work; that cost remains in the simulated outcome.

Results are counterfactual model estimates, not causal proof or guaranteed savings. Validate the baseline against observed departure times and perform sensitivity checks before using results for operational decisions. The model covers outbound pallet staging and loading. It does not model inbound receipts, picking and packing, replenishment, aisle congestion, human breaks, changing dock assignments, hazmat/weight constraints, robot-specific routing or exact proprietary WMS behavior. Temperature compatibility is enforced. Travel uses Manhattan coordinates, so physical walls and one-way paths are not represented.

MVP input limits: 500 inventory pallets, 200 shipments, 2,000 order lines, 500 historical relocations, 20 forklifts, a 48-hour arrival horizon and a 5 MB browser request.

## Offline command line

```sh
./.venv-sandbox/bin/python -m warehouse_sandbox.cli --input examples/sandbox/demo.json --output runs/demo-result.json
./.venv-sandbox/bin/python -m warehouse_sandbox.cli --csv-dir examples/sandbox/csv --output runs/csv-result.json
./.venv-sandbox/bin/python -m unittest discover -s tests_sandbox -v
```

Frontend source and helper regressions are available separately:

```sh
node warehouse_sandbox/static/frontend-regression.cjs
```

These checks cover controls, tab state, setting restoration and data flow; rendered browser verification remains a separate check.

## Architecture and source snapshot

Read `docs/sandbox/ARCHITECTURE.md` for component boundaries and extension seams, and `docs/sandbox/CONTRACT.md` for the dataset and API contracts. `warehouse_sandbox/` contains the new standalone implementation. The copied `warehouse-preposition-optimizer/` provides the actual recommendation code, imported by `warehouse_sandbox/optimizer_bridge.py`. Only its candidate-generation path runs; its live dispatch methods are never called. Phase 2 ML, OR assignment, RL and hazmat filters are not enabled by this MVP.

The source snapshot came from `owenstuckman/AReasonableWMS`, commit `485008c7d62e18b533e0a0e1bed277bfc14ca0ee`. Git metadata was not copied. The original checkout was not edited. A local source hash manifest is kept in `docs/sandbox/SOURCE_MANIFEST.json`.

The source repository's commercial reuse rights have not been established by this MVP. Confirm code ownership, contributor assignments and applicable licenses before redistribution. Import only customer data you are authorized to use, and check each vendor agreement before implementing a future connector.

## Original optimizer integration

The Phase 1 scorer ranks suggestions with the repository value function V(m) = (T_saved * P_load * W_order) / (C_move + C_opportunity). Its source movement cost excludes empty travel to reach the pallet; full replay task cost includes it. The original scheduler threshold defaults to 0.1 and is adjustable as Minimum optimizer score. Optional order priority is an integer from 1 to 10, default 1.

The desktop copy of src/scoring/value_function.py has two intentional replay adaptations: an optional injected clock and an overflow guard for its urgency exponential. The original checkout remains unchanged. Integration tests verify the actual generator, feasibility engine and scorer execute and that changing original source scores changes recommendation ranking.

## Vercel hosting

Import this repository with root directory `.` and the Other framework preset. The `api/index.py` entrypoint, root `app.py`, `requirements.txt`, `.python-version`, and `vercel.json` configure the hosted API and UI. Git pushes redeploy the connected project.

The hosted app executes the same original Phase 1 optimizer. Runs remain in the current page; download JSON or CSV before refreshing or closing. JSON can be imported again to rerun a scenario. There is no shared server run history or persistent uploaded-data storage. Hosted uploads are limited to 4 MB. The desktop launcher continues saving results locally.

Use Vercel Deployment Protection for a private pilot. This MVP has no customer accounts or tenant access controls; use synthetic data on an unprotected public deployment.
