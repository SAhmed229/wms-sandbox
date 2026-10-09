#!/usr/bin/env python3
"""Multi-agent MAPPO training using Ray RLlib.

Requires: pip install "ray[rllib]" torch
(Not in pyproject.toml — Ray RLlib is very large and only needed for production RL training.)

Architecture:
- Each AGV/forklift is an independent agent with a shared policy.
- Centralized critic, decentralized execution (CTDE pattern).
- Domain randomization: order volumes and appointment times vary per episode.

Usage:
    uv run python scripts/train_marl.py --agents 3 --timesteps 10000000 --gpus 1
    uv run python scripts/train_marl.py --agents 2 --timesteps 100000  # quick smoke test
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main() -> None:
    parser = argparse.ArgumentParser(description="Train MAPPO agents for warehouse pre-positioning")
    parser.add_argument("--agents", type=int, default=3,
                        help="Number of AGV/forklift agents (shared policy)")
    parser.add_argument("--timesteps", type=int, default=10_000_000,
                        help="Total environment steps across all agents")
    parser.add_argument("--gpus", type=float, default=0,
                        help="Number of GPUs per trainer (fractional allowed)")
    parser.add_argument("--cpus", type=int, default=4,
                        help="Number of CPU workers for rollout")
    parser.add_argument("--out", type=str, default="models/mappo_prepos",
                        help="Output directory for checkpoints")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    try:
        import ray
        from ray import tune
        from ray.rllib.algorithms.ppo import PPOConfig
        from ray.rllib.policy.policy import PolicySpec
    except ImportError:
        print(
            "ERROR: ray[rllib] is not installed.\n"
            "Install it with: pip install 'ray[rllib]' torch\n"
            "(Not included in pyproject.toml — only needed for MARL training.)"
        )
        sys.exit(1)

    try:
        from src.simulation.warehouse_env import WarehousePrePositionEnv, EnvConfig
        from src.simulation.digital_twin import SimConfig
    except ImportError as exc:
        print(f"ERROR importing simulation modules: {exc}")
        sys.exit(1)

    ray.init(ignore_reinit_error=True)

    # Each agent gets its own environment instance but shares the policy weights.
    # RLlib requires the env to be registered before use.
    from ray.tune.registry import register_env

    def _env_creator(env_config: dict) -> WarehousePrePositionEnv:
        sim_cfg = SimConfig(
            shift_duration_seconds=env_config.get("shift_duration_seconds", 28_800.0),
            forklift_count=args.agents,
            random_seed=env_config.get("seed", args.seed),
        )
        return WarehousePrePositionEnv(
            env_config=EnvConfig(sim_config=sim_cfg, seed=env_config.get("seed", args.seed))
        )

    register_env("warehouse_prepos", _env_creator)

    # Shared policy across all agents (parameter sharing = implicit centralised training).
    shared_policy = PolicySpec()

    config = (
        PPOConfig()
        .environment(
            env="warehouse_prepos",
            env_config={"shift_duration_seconds": 28_800.0, "seed": args.seed},
        )
        .multi_agent(
            policies={"shared_policy": shared_policy},
            policy_mapping_fn=lambda agent_id, episode, worker, **kw: "shared_policy",
        )
        .training(
            gamma=0.99,
            lr=3e-4,
            train_batch_size=max(2048, args.agents * 512),
            sgd_minibatch_size=64,
            num_sgd_iter=10,
            lambda_=0.95,
            use_gae=True,
            clip_param=0.2,
            entropy_coeff=0.01,
        )
        .resources(
            num_gpus=args.gpus,
            num_cpus_per_worker=1,
        )
        .rollouts(num_rollout_workers=args.cpus)
        .framework("torch")
        .debugging(seed=args.seed)
    )

    print(
        f"Starting MAPPO training: {args.agents} agents, {args.timesteps:,} timesteps, "
        f"{args.gpus} GPU(s), {args.cpus} rollout workers"
    )

    tuner = tune.Tuner(
        "PPO",
        param_space=config.to_dict(),
        run_config=tune.RunConfig(
            stop={"timesteps_total": args.timesteps},
            storage_path=str(Path(args.out).resolve()),
            checkpoint_config=tune.CheckpointConfig(
                checkpoint_frequency=10,
                checkpoint_at_end=True,
            ),
        ),
    )

    results = tuner.fit()
    best = results.get_best_result(metric="episode_reward_mean", mode="max")
    print(f"Best result checkpoint: {best.checkpoint}")
    print(f"Best episode_reward_mean: {best.metrics.get('episode_reward_mean', 'N/A')}")

    ray.shutdown()


if __name__ == "__main__":
    main()
