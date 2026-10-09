"""ONNX-based RL policy inference with OR-Tools fallback."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import structlog

from src.models.inventory import Location
from src.models.movements import CandidateMovement, MovementTask

logger = structlog.get_logger(__name__)

# Action index 0 is always NO_OP in the Gymnasium env.
_NO_OP_INDEX = 0


class RLPolicyInference:
    """Runs ONNX-exported PPO policy to select pre-positioning candidates.

    Falls back to the Phase 3 CP-SAT assignment solver when:
    - The ONNX model file is absent or fails to load.
    - The selected action is NO_OP (index 0).
    - The selected index exceeds the candidate count.
    - The ONNX runtime raises any exception.

    Args:
        onnx_path: Path to the exported ONNX policy model.
        fallback_resources: Resource budget passed to the OR fallback solver.
        fallback_timeout_seconds: Solver timeout for the OR fallback.
        max_staging_distance_meters: Max distance for OR fallback.
    """

    def __init__(
        self,
        onnx_path: str | Path,
        fallback_resources: int = 5,
        fallback_timeout_seconds: int = 10,
        max_staging_distance_meters: float = 50.0,
    ) -> None:
        self._onnx_path = Path(onnx_path)
        self._fallback_resources = fallback_resources
        self._fallback_timeout = fallback_timeout_seconds
        self._max_distance = max_staging_distance_meters
        self._session: Any = None
        self._load_model()

    def _load_model(self) -> None:
        """Load the ONNX model if the file exists."""
        if not self._onnx_path.exists():
            logger.warning("rl_policy.model_not_found", path=str(self._onnx_path))
            return
        try:
            import onnxruntime as ort  # noqa: PLC0415
            self._session = ort.InferenceSession(str(self._onnx_path))
            logger.info("rl_policy.model_loaded", path=str(self._onnx_path))
        except Exception as exc:
            logger.warning("rl_policy.load_failed", error=str(exc))
            self._session = None

    @property
    def available(self) -> bool:
        """Return True if the ONNX model is loaded and ready."""
        return self._session is not None

    def select(
        self,
        observation: npt.NDArray[np.float32],
        candidates: list[CandidateMovement],
        staging_locations: list[Location],
    ) -> list[MovementTask]:
        """Select movements using the RL policy, falling back to OR-Tools.

        Args:
            observation: Flat float32 observation vector from the Gymnasium env.
            candidates: Scored candidate movements.
            staging_locations: Available staging locations.

        Returns:
            List of MovementTask instances to dispatch this cycle.
        """
        if self._session is not None and len(candidates) > 0:
            try:
                return self._rl_select(observation, candidates, staging_locations)
            except Exception as exc:
                logger.warning("rl_policy.inference_failed", error=str(exc))

        return self._or_fallback(candidates, staging_locations)

    def _rl_select(
        self,
        observation: npt.NDArray[np.float32],
        candidates: list[CandidateMovement],
        staging_locations: list[Location],
    ) -> list[MovementTask]:
        """Run ONNX forward pass and convert action to MovementTask list.

        Args:
            observation: Flat float32 observation vector.
            candidates: Scored candidates.
            staging_locations: Available staging locations.

        Returns:
            Selected MovementTask list (empty list triggers OR fallback upstream).
        """
        obs_batch = observation.reshape(1, -1).astype(np.float32)
        outputs = self._session.run(None, {"observation": obs_batch})
        logits: npt.NDArray[np.float32] = outputs[0][0]

        # Mask out actions beyond the available candidates.
        mask = np.zeros(len(logits), dtype=bool)
        mask[0] = True  # NO_OP always valid
        for i in range(min(len(candidates), len(logits) - 1)):
            mask[i + 1] = True

        masked_logits = np.where(mask, logits, -np.inf)
        action = int(np.argmax(masked_logits))

        if action == _NO_OP_INDEX:
            logger.debug("rl_policy.no_op_selected")
            return self._or_fallback(candidates, staging_locations)

        candidate_idx = action - 1
        if candidate_idx >= len(candidates):
            return self._or_fallback(candidates, staging_locations)

        # RL selected a single candidate; convert to a task using its pre-assigned
        # to_location. The OR solver is skipped — the policy chose directly.
        from datetime import UTC, datetime  # noqa: PLC0415

        cand = candidates[candidate_idx]
        task = MovementTask(
            movement_id=cand.movement_id,
            sku_id=cand.sku_id,
            from_location=cand.from_location,
            to_location=cand.to_location,
            score=cand.score,
            score_components=cand.score_components,
            reason=cand.reason + " [RL-selected]",
            estimated_duration_seconds=cand.estimated_duration_seconds,
            assigned_resource="UNASSIGNED",
            dispatched_at=datetime.now(UTC),
        )
        logger.info(
            "rl_policy.action_selected",
            action=action,
            sku_id=cand.sku_id,
            score=round(cand.score, 4),
        )
        return [task]

    def _or_fallback(
        self,
        candidates: list[CandidateMovement],
        staging_locations: list[Location],
    ) -> list[MovementTask]:
        """Fall back to CP-SAT assignment solver.

        Args:
            candidates: Scored candidates.
            staging_locations: Available staging locations.

        Returns:
            MovementTask list from the OR solver.
        """
        if not candidates:
            return []

        from src.optimizer.assignment import StagingAssignmentSolver  # noqa: PLC0415

        solver = StagingAssignmentSolver(
            solver_timeout_seconds=self._fallback_timeout,
            max_staging_distance_meters=self._max_distance,
        )
        result = solver.solve(
            candidates=candidates,
            staging_locations=staging_locations,
            available_resources=self._fallback_resources,
        )
        logger.info(
            "rl_policy.or_fallback",
            status=result.solver_status,
            tasks=len(result.tasks),
        )
        return result.tasks
