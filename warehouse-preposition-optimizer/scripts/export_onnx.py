#!/usr/bin/env python3
"""Export a trained SB3 PPO policy to ONNX for production inference.

Requires: pip install stable-baselines3[extra] onnx onnxruntime
(Not in pyproject.toml — only needed for RL model export.)

The exported ONNX model accepts a flat float32 observation vector and returns
action logits. At inference time the scheduler calls the model, picks the
argmax action, and falls back to the Phase 3 OR-Tools solver when:
  - The model is unavailable.
  - The selected action index is NO_OP (0) or exceeds the candidate count.
  - The ONNX runtime raises an exception.

Usage:
    uv run python scripts/export_onnx.py --model models/ppo_prepos.zip --out models/policy.onnx
    uv run python scripts/export_onnx.py --model models/ppo_prepos.zip --out models/policy.onnx --verify
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))


def main() -> None:
    parser = argparse.ArgumentParser(description="Export PPO policy to ONNX")
    parser.add_argument("--model", type=str, required=True,
                        help="Path to saved SB3 PPO .zip model")
    parser.add_argument("--out", type=str, default="models/policy.onnx",
                        help="Output path for the ONNX model")
    parser.add_argument("--verify", action="store_true",
                        help="Run a forward pass to verify the exported model")
    parser.add_argument("--opset", type=int, default=17,
                        help="ONNX opset version (default: 17)")
    args = parser.parse_args()

    try:
        import torch
        from stable_baselines3 import PPO
    except ImportError:
        print(
            "ERROR: stable-baselines3 and torch are required.\n"
            "Install: pip install stable-baselines3[extra]"
        )
        sys.exit(1)

    try:
        import onnx
        import onnxruntime as ort
    except ImportError:
        print(
            "ERROR: onnx and onnxruntime are required.\n"
            "Install: pip install onnx onnxruntime"
        )
        sys.exit(1)

    model_path = Path(args.model)
    if not model_path.exists():
        print(f"ERROR: Model file not found: {model_path}")
        sys.exit(1)

    print(f"Loading SB3 PPO model from {model_path} ...")
    model = PPO.load(str(model_path))

    # Extract the underlying PyTorch policy network.
    policy = model.policy
    policy.eval()

    # Determine observation dimension from the policy.
    obs_dim: int = model.observation_space.shape[0]  # type: ignore[index]
    print(f"Observation dimension: {obs_dim}")

    dummy_obs = torch.zeros(1, obs_dim, dtype=torch.float32)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Exporting to ONNX (opset {args.opset}) → {out_path} ...")
    torch.onnx.export(
        policy,
        dummy_obs,
        str(out_path),
        opset_version=args.opset,
        input_names=["observation"],
        output_names=["action_logits", "value"],
        dynamic_axes={
            "observation": {0: "batch_size"},
            "action_logits": {0: "batch_size"},
            "value": {0: "batch_size"},
        },
        export_params=True,
        do_constant_folding=True,
    )

    # Validate the exported graph.
    onnx_model = onnx.load(str(out_path))
    onnx.checker.check_model(onnx_model)
    print("ONNX model graph check: OK")

    if args.verify:
        print("Verifying ONNX runtime forward pass ...")
        session = ort.InferenceSession(str(out_path))
        dummy_np = np.zeros((1, obs_dim), dtype=np.float32)
        outputs = session.run(None, {"observation": dummy_np})
        logits = outputs[0]
        print(f"  Action logits shape: {logits.shape}")
        print(f"  Selected action (argmax): {int(np.argmax(logits[0]))}")
        print("Verification: PASSED")

    print(f"\nExport complete: {out_path}")
    print(
        "\nTo use in production, load the ONNX model in the scheduler:\n"
        "  from src.optimizer.rl_policy import RLPolicyInference\n"
        "  policy = RLPolicyInference(onnx_path='models/policy.onnx')\n"
        "  scheduler = PrePositionScheduler(..., rl_policy=policy)"
    )


if __name__ == "__main__":
    main()
