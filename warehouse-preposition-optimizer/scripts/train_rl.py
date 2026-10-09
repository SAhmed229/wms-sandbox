#!/usr/bin/env python3
"""Single-agent PPO prototype using Stable Baselines3.

Requires: pip install stable-baselines3[extra]
(Not in pyproject.toml — SB3 is large and only needed for RL training.)

Usage:
    uv run python scripts/train_rl.py --timesteps 1000000 --out models/ppo_prepos.zip
    uv run python scripts/train_rl.py --timesteps 50000 --eval-freq 10000 --out models/ppo_prepos.zip
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Ensure src/ is importable when running as a script.
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models.inventory import (
    ABCClass,
    InventoryPosition,
    Location,
    SKU,
    TemperatureZone,
)
from src.models.orders import AppointmentStatus, CarrierAppointment, OrderLine, OutboundOrder
from src.simulation.digital_twin import SimConfig
from src.simulation.reward import RewardWeights
from src.simulation.warehouse_env import EnvConfig, WarehousePrePositionEnv


def _make_demo_env(seed: int = 42) -> WarehousePrePositionEnv:
    """Build a small demo environment with synthetic data for training.

    Args:
        seed: RNG seed.

    Returns:
        Configured WarehousePrePositionEnv.
    """
    now = datetime.now(UTC)

    # 10 locations: 8 bulk storage + 2 staging
    locations = [
        Location(location_id=f"LOC-{i}", zone="BULK", aisle=i % 3 + 1, bay=1, level=0,
                 x=float(i * 5), y=0.0, temperature_zone=TemperatureZone.AMBIENT,
                 is_staging=False, nearest_dock_door=1)
        for i in range(8)
    ] + [
        Location(location_id="STAGE-1", zone="STAGING", aisle=1, bay=1, level=0,
                 x=2.0, y=0.0, temperature_zone=TemperatureZone.AMBIENT,
                 is_staging=True, nearest_dock_door=1),
        Location(location_id="STAGE-2", zone="STAGING", aisle=1, bay=2, level=0,
                 x=4.0, y=0.0, temperature_zone=TemperatureZone.AMBIENT,
                 is_staging=True, nearest_dock_door=1),
    ]

    skus = [
        SKU(sku_id=f"SKU-{i}", description=f"Product {i}", weight_kg=10.0, volume_m3=0.1,
            abc_class=ABCClass.A)
        for i in range(5)
    ]

    inventory = [
        InventoryPosition(
            position_id=f"POS-{i}",
            sku=skus[i],
            location=locations[i],
            quantity=10,
        )
        for i in range(5)
    ]

    appt = CarrierAppointment(
        appointment_id="APPT-1",
        carrier="FedEx",
        dock_door=1,
        scheduled_arrival=now + timedelta(hours=1),
        scheduled_departure=now + timedelta(hours=3),
        status=AppointmentStatus.SCHEDULED,
    )

    orders = [
        OutboundOrder(
            order_id=f"ORD-{i}",
            appointment=appt,
            lines=[OrderLine(line_id=f"LINE-{i}", sku_id=f"SKU-{i}", quantity=2)],
            priority=5,
            cutoff_time=now + timedelta(hours=2),
        )
        for i in range(3)
    ]

    sim_config = SimConfig(
        shift_duration_seconds=3600.0,  # 1-hour episode for fast training
        forklift_count=2,
        random_seed=seed,
    )

    return WarehousePrePositionEnv(
        env_config=EnvConfig(sim_config=sim_config, seed=seed),
        inventory=inventory,
        appointments=[appt],
        orders=orders,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Train PPO agent for warehouse pre-positioning")
    parser.add_argument("--timesteps", type=int, default=1_000_000,
                        help="Total training timesteps")
    parser.add_argument("--eval-freq", type=int, default=50_000,
                        help="Evaluation frequency in timesteps")
    parser.add_argument("--out", type=str, default="models/ppo_prepos.zip",
                        help="Output path for the trained model")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    try:
        from stable_baselines3 import PPO
        from stable_baselines3.common.callbacks import EvalCallback
        from stable_baselines3.common.env_checker import check_env
        from stable_baselines3.common.monitor import Monitor
    except ImportError:
        print(
            "ERROR: stable-baselines3 is not installed.\n"
            "Install it with: pip install stable-baselines3[extra]\n"
            "(Not included in pyproject.toml — only needed for RL training.)"
        )
        sys.exit(1)

    print(f"Building environment (seed={args.seed})...")
    env = Monitor(_make_demo_env(seed=args.seed))

    print("Checking environment compatibility...")
    check_env(env, warn=True)

    eval_env = Monitor(_make_demo_env(seed=args.seed + 1))

    print(f"Training PPO for {args.timesteps:,} timesteps...")
    model = PPO(
        policy="MlpPolicy",
        env=env,
        learning_rate=3e-4,
        n_steps=2048,
        batch_size=64,
        n_epochs=10,
        gamma=0.99,
        gae_lambda=0.95,
        verbose=1,
        seed=args.seed,
        tensorboard_log="logs/ppo_prepos/",
    )

    eval_callback = EvalCallback(
        eval_env,
        best_model_save_path=str(Path(args.out).parent / "best_ppo"),
        log_path="logs/ppo_eval/",
        eval_freq=args.eval_freq,
        deterministic=True,
        render=False,
    )

    model.learn(total_timesteps=args.timesteps, callback=eval_callback)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    model.save(str(out_path))
    print(f"Model saved to {out_path}")


if __name__ == "__main__":
    main()
