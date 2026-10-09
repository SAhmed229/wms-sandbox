"""Configuration management for the warehouse pre-positioning optimizer."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings


class ScoringWeightsConfig(BaseSettings):
    """Weights configuration for the scoring function."""

    time_saved: float = Field(default=1.0, ge=0)
    load_probability: float = Field(default=1.0, ge=0)
    order_priority: float = Field(default=1.0, ge=0)
    movement_cost: float = Field(default=1.0, ge=0)
    opportunity_cost: float = Field(default=1.0, ge=0)


class ScoringConfig(BaseSettings):
    """Full scoring configuration including weights and parameters."""

    weights: ScoringWeightsConfig = Field(default_factory=ScoringWeightsConfig)
    decay_constant_seconds: float = Field(default=3600.0, gt=0)
    max_candidates_per_cycle: int = Field(default=50, gt=0)
    min_score_threshold: float = Field(default=0.1, ge=0)


class SchedulingConfig(BaseSettings):
    """Scheduling and cycle configuration."""

    cycle_interval_seconds: int = Field(default=60, gt=0)
    dispatch_batch_size: int = Field(default=5, gt=0)
    task_expiry_minutes: int = Field(default=15, gt=0)
    horizon_hours: float = Field(default=24.0, gt=0)


class ResourceConfig(BaseSettings):
    """Physical resource parameters."""

    forklift_speed_mps: float = Field(default=2.2, gt=0)
    agv_speed_mps: float = Field(default=1.3, gt=0)
    handling_time_seconds: float = Field(default=45.0, ge=0)
    max_utilization: float = Field(default=0.95, gt=0, le=1.0)
    base_opportunity_seconds: float = Field(default=60.0, gt=0)


class ConstraintsConfig(BaseSettings):
    """Constraint enforcement flags."""

    enforce_temperature: bool = True
    enforce_hazmat: bool = True
    enforce_capacity: bool = True
    max_staging_distance_meters: float = Field(default=50.0, gt=0)


class PredictionConfig(BaseSettings):
    """ML prediction configuration (Phase 2)."""

    enabled: bool = False
    model_path: str = "models/demand_lgbm.pkl"
    fallback_on_error: bool = True
    prediction_cache_ttl_seconds: int = Field(default=300, gt=0)


class OptimizationConfig(BaseSettings):
    """OR optimization configuration (Phase 3)."""

    enabled: bool = False
    solver_timeout_seconds: int = Field(default=10, gt=0)
    route_optimization: bool = False


class WMSConfig(BaseSettings):
    """WMS adapter configuration."""

    adapter: str = "generic_db"
    poll_interval_seconds: int = Field(default=30, gt=0)
    cache_ttl_seconds: int = Field(default=60, gt=0)
    connection_string: str = ""
    schema_path: str | None = None


class Settings(BaseSettings):
    """Root application settings loaded from environment and config.yml."""

    database_url: str = Field(default="postgresql+asyncpg://wms:wms@localhost:5432/wms")
    redis_url: str = Field(default="redis://localhost:6379/0")
    api_key: str = Field(default="change-me-in-production")
    log_level: str = Field(default="INFO")
    config_path: str = Field(default="config.yml")

    # Feature flags
    use_ml_prediction: bool = False
    use_or_optimization: bool = False

    # Nested config (populated by load_config)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    scheduling: SchedulingConfig = Field(default_factory=SchedulingConfig)
    resources: ResourceConfig = Field(default_factory=ResourceConfig)
    constraints: ConstraintsConfig = Field(default_factory=ConstraintsConfig)
    prediction: PredictionConfig = Field(default_factory=PredictionConfig)
    optimization: OptimizationConfig = Field(default_factory=OptimizationConfig)
    wms: WMSConfig = Field(default_factory=WMSConfig)

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override into base dict.

    Args:
        base: Base dictionary.
        override: Dictionary with values to override.

    Returns:
        Merged dictionary with override values taking precedence.
    """
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(config_path: str | None = None) -> Settings:
    """Load full settings from environment variables and config.yml.

    Args:
        config_path: Path to config.yml. Defaults to CONFIG_PATH env var or 'config.yml'.

    Returns:
        Fully populated Settings instance.
    """
    # Start with env-based settings
    env_settings = Settings()

    resolved_path = config_path or env_settings.config_path
    yaml_data: dict[str, Any] = {}

    if Path(resolved_path).exists():
        with open(resolved_path) as f:
            loaded = yaml.safe_load(f)
            if isinstance(loaded, dict):
                yaml_data = loaded

    # Resolve any ${VAR} substitutions in yaml values
    yaml_str = yaml.dump(yaml_data)
    for key, value in os.environ.items():
        yaml_str = yaml_str.replace(f"${{{key}}}", value)
    yaml_data = yaml.safe_load(yaml_str) or {}

    # Build nested config objects from yaml
    scoring_data = yaml_data.get("scoring", {})
    scheduling_data = yaml_data.get("scheduling", {})
    resources_data = yaml_data.get("resources", {})
    constraints_data = yaml_data.get("constraints", {})
    prediction_data = yaml_data.get("prediction", {})
    optimization_data = yaml_data.get("optimization", {})
    wms_data = yaml_data.get("wms", {})

    weights_data = scoring_data.pop("weights", {}) if scoring_data else {}

    scoring_config = ScoringConfig(
        weights=ScoringWeightsConfig(**weights_data),
        **{k: v for k, v in scoring_data.items() if k != "weights"},
    )

    return Settings(
        database_url=env_settings.database_url,
        redis_url=env_settings.redis_url,
        api_key=env_settings.api_key,
        log_level=env_settings.log_level,
        config_path=resolved_path,
        use_ml_prediction=prediction_data.get("enabled", False),
        use_or_optimization=optimization_data.get("enabled", False),
        scoring=scoring_config,
        scheduling=SchedulingConfig(**scheduling_data) if scheduling_data else SchedulingConfig(),
        resources=ResourceConfig(**resources_data) if resources_data else ResourceConfig(),
        constraints=ConstraintsConfig(**constraints_data) if constraints_data else ConstraintsConfig(),
        prediction=PredictionConfig(**prediction_data) if prediction_data else PredictionConfig(),
        optimization=OptimizationConfig(**optimization_data) if optimization_data else OptimizationConfig(),
        wms=WMSConfig(**wms_data) if wms_data else WMSConfig(),
    )
