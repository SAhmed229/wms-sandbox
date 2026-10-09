"""Prove suggestions execute the copied original optimizer pipeline."""

import copy
import importlib
import inspect
import math
import socket
import unittest
from pathlib import Path
from unittest.mock import patch

from warehouse_sandbox.engine import run_comparison
from warehouse_sandbox.importer import normalize_settings, validate_dataset
from tests_sandbox.fixtures import warehouse


ROOT = Path(__file__).resolve().parents[1]
ORIGINAL_COPY = ROOT / "warehouse-preposition-optimizer"


def candidate_warehouse():
    data = warehouse()
    data["orders"] = data["orders"][:2]
    for order in data["orders"]:
        order["scheduled_arrival_seconds"] = order["arrival_seconds"]
    data["locations"][2]["capacity_pallets"] = 2
    return validate_dataset(data)


class OriginalOptimizerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Import through the bridge first so its repository-path guard runs.
        bridge = importlib.import_module("warehouse_sandbox.optimizer_bridge")
        data = candidate_warehouse()
        bridge.OriginalOptimizerBridge(data, normalize_settings(None, data))
        cls.scheduler_module = importlib.import_module("src.optimizer.scheduler")
        cls.scoring_module = importlib.import_module("src.scoring.value_function")
        cls.feasibility_module = importlib.import_module("src.constraints.feasibility")
        cls.queue_module = importlib.import_module("src.dispatch.task_queue")

    def test_runtime_classes_come_from_the_desktop_original_copy(self):
        classes = (self.scheduler_module.PrePositionScheduler,
                   self.scoring_module.MovementScorer,
                   self.feasibility_module.FeasibilityEngine)
        for cls in classes:
            with self.subTest(cls=cls.__name__):
                source = Path(inspect.getsourcefile(cls)).resolve()
                self.assertTrue(source.is_relative_to(ORIGINAL_COPY.resolve()), source)
                self.assertTrue(source.is_file())
        result = run_comparison(candidate_warehouse())
        self.assertEqual(result["policy"]["id"], "original_optimizer_phase1")
        self.assertEqual(result["policy"]["clock"], "simulated")
        self.assertIs(result["policy"]["live_dispatch_enabled"], False)

    def test_original_generator_feasibility_and_scorer_actually_execute(self):
        calls = {"generator": 0, "feasibility": 0, "scorer": 0}
        scheduler = self.scheduler_module.PrePositionScheduler
        scorer = self.scoring_module.MovementScorer
        feasibility = self.feasibility_module.FeasibilityEngine
        original_generate = scheduler.generate_candidates
        original_score = scorer.score
        original_evaluate = feasibility.evaluate

        async def generate(instance):
            calls["generator"] += 1
            return await original_generate(instance)

        def score(instance, candidate, context):
            calls["scorer"] += 1
            return original_score(instance, candidate, context)

        def evaluate(instance, movement, state):
            calls["feasibility"] += 1
            return original_evaluate(instance, movement, state)

        with patch.object(scheduler, "generate_candidates", generate), \
             patch.object(scorer, "score", score), \
             patch.object(feasibility, "evaluate", evaluate):
            result = run_comparison(candidate_warehouse())
        self.assertTrue(result["proposed"]["suggestions"])
        for component, count in calls.items():
            self.assertGreater(count, 0, component)
        for suggestion in result["proposed"]["suggestions"]:
            self.assertEqual(suggestion["policy_source"], "src.optimizer.scheduler.PrePositionScheduler.generate_candidates")
            self.assertIn("p_load", suggestion["score_components"])
            self.assertIn("c_opportunity", suggestion["score_components"])

    def test_changing_original_scores_changes_the_recommendation_rank(self):
        data = candidate_warehouse()
        # Give the two original source locations opposite scoring preferences.
        # Updating candidate.score reflects the original scorer's API contract.
        def preferred_source(location_id):
            def score(instance, candidate, context):
                value = 100.0 if candidate.from_location.location_id == location_id else 1.0
                candidate.score = value
                candidate.score_components = {"test_preference": value}
                return value
            return score

        with patch.object(self.scoring_module.MovementScorer, "score", preferred_source("store-a")):
            a = run_comparison(data)
        with patch.object(self.scoring_module.MovementScorer, "score", preferred_source("store-b")):
            b = run_comparison(data)
        self.assertEqual(a["proposed"]["suggestions"][0]["pallet_id"], "P1")
        self.assertEqual(b["proposed"]["suggestions"][0]["pallet_id"], "P2")
        self.assertTrue(a["proposed"]["suggestions"][0]["adopted"])
        self.assertTrue(b["proposed"]["suggestions"][0]["adopted"])
        self.assertEqual(a["proposed"]["suggestions"][0]["score"], 100)
        self.assertEqual(b["proposed"]["suggestions"][0]["score"], 100)

    def test_shared_sku_tenants_have_scoped_original_candidate_contexts(self):
        contexts = []
        original = self.scheduler_module.PrePositionScheduler.generate_candidates

        async def capture(instance):
            state = await instance._wms.get_warehouse_state(instance._config.horizon_hours)
            contexts.append(state)
            return await original(instance)

        with patch.object(self.scheduler_module.PrePositionScheduler, "generate_candidates", capture):
            result = run_comparison(candidate_warehouse())
        self.assertTrue(contexts)
        observed_positions = set()
        for state in contexts:
            self.assertEqual(len(state.inventory_positions), 1)
            self.assertEqual(len(state.outbound_orders), 1)
            self.assertEqual(len(state.appointments), 1)
            position = state.inventory_positions[0]
            order = state.outbound_orders[0]
            self.assertEqual(position.sku.sku_id, "shared-sku")
            self.assertEqual(position.position_id, {"O1": "P1", "O2": "P2"}[order.order_id])
            # The model may not carry owner identity itself; isolation is proven
            # by one physical position and one matching order per invocation.
            observed_positions.add((position.location.location_id, order.order_id))
        self.assertIn(("store-a", "O1"), observed_positions)
        self.assertIn(("store-b", "O2"), observed_positions)
        owners = {s["pallet_id"]: s["owner_id"] for s in result["proposed"]["suggestions"]}
        self.assertEqual(owners["P1"], "tenant-a")
        self.assertEqual(owners["P2"], "tenant-b")

    def test_bridge_rejects_unreleased_or_wrong_owner_demand_even_after_cache_hit(self):
        bridge_type = importlib.import_module("warehouse_sandbox.optimizer_bridge").OriginalOptimizerBridge
        data = candidate_warehouse()
        physical = {"order": data["orders"][0], "pallet": data["inventory"][0], "destination": "stage"}
        for prefill_cache in (False, True):
            for invalid in ("unreleased", "wrong_owner", "no_pallet_demand"):
                with self.subTest(prefill_cache=prefill_cache, invalid=invalid):
                    bridge = bridge_type(data, normalize_settings(None, data))
                    if prefill_cache:
                        self.assertTrue(bridge.rank_candidates([physical], 0, 0, {"stage": 0}))
                    changed = copy.deepcopy(physical)
                    if invalid == "unreleased":
                        changed["order"]["release_seconds"] = 10
                    elif invalid == "wrong_owner":
                        changed["pallet"]["owner_id"] = "tenant-b"
                    else:
                        changed["order"]["lines"][0]["pallet_id"] = "P2"
                    with self.assertRaises(ValueError):
                        bridge.rank_candidates([changed], 0, 0, {"stage": 0})

    def test_original_score_threshold_changes_eligible_recommendations(self):
        data = candidate_warehouse()
        allowed = run_comparison(data, {"min_optimizer_score": 0})
        filtered = run_comparison(data, {"min_optimizer_score": 100})
        self.assertTrue(allowed["proposed"]["suggestions"])
        self.assertEqual(filtered["proposed"]["suggestions"], [])
        self.assertEqual(filtered["baseline"]["metrics"], filtered["proposed"]["metrics"])

    def test_original_feasibility_rejection_blocks_a_simulated_suggestion(self):
        constraints = importlib.import_module("src.models.constraints")
        rejected = constraints.FeasibilityResult(feasible=False, violations=[])
        with patch.object(self.feasibility_module.FeasibilityEngine, "evaluate", return_value=rejected), \
             patch.object(self.scoring_module.MovementScorer, "score", side_effect=AssertionError("infeasible candidate must not be scored")):
            result = run_comparison(candidate_warehouse())
        self.assertEqual(result["proposed"]["suggestions"], [])
        self.assertEqual(result["baseline"]["metrics"], result["proposed"]["metrics"])

    def test_full_adoption_cannot_enter_original_dispatch_or_external_connections(self):
        forbidden = AssertionError("production dispatch/access is forbidden")
        scheduler = self.scheduler_module.PrePositionScheduler
        queue = self.queue_module.TaskQueue
        with patch.object(scheduler, "run_cycle", side_effect=forbidden), \
             patch.object(scheduler, "dispatch_top_movements", side_effect=forbidden), \
             patch.object(queue, "push", side_effect=forbidden), \
             patch.object(queue, "__init__", side_effect=forbidden), \
             patch.object(socket.socket, "connect", side_effect=forbidden), \
             patch.object(socket.socket, "connect_ex", side_effect=forbidden), \
             patch.object(socket, "create_connection", side_effect=forbidden):
            result = run_comparison(candidate_warehouse(), {"adoption_percent": 100})
        self.assertTrue(any(s["adopted"] for s in result["proposed"]["suggestions"]))
        self.assertEqual(result["proposed"]["metrics"]["completed_units"], 14)

    def test_historical_calendar_shift_keeps_original_urgency_scores_and_no_overflow(self):
        data = candidate_warehouse()
        data["facility"]["start_time"] = "1960-01-01T08:00:00-04:00"
        shifted = copy.deepcopy(data)
        shifted["facility"]["start_time"] = "2090-01-01T08:00:00-04:00"
        a = run_comparison(data)["proposed"]["suggestions"]
        b = run_comparison(shifted)["proposed"]["suggestions"]
        self.assertTrue(a)
        self.assertEqual([(s["pallet_id"], s["score"], s["score_components"]) for s in a],
                         [(s["pallet_id"], s["score"], s["score_components"]) for s in b])
        for suggestion in a:
            self.assertTrue(math.isfinite(suggestion["score"]))
            self.assertTrue(all(math.isfinite(value) for value in suggestion["score_components"].values()))

    def test_original_urgency_advances_with_simulated_planning_time(self):
        early = candidate_warehouse()
        early["orders"] = early["orders"][:1]
        later = copy.deepcopy(early)
        later["orders"][0]["release_seconds"] = 600
        a = run_comparison(early)["proposed"]["suggestions"][0]
        b = run_comparison(later)["proposed"]["suggestions"][0]
        self.assertEqual(a["at_seconds"], 0)
        self.assertEqual(b["at_seconds"], 600)
        self.assertGreater(b["score_components"]["w_order"], a["score_components"]["w_order"])
        self.assertGreater(b["score"], a["score"])


if __name__ == "__main__":
    unittest.main()
