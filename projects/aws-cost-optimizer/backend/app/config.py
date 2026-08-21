"""Runtime configuration.

Everything here is env-driven so the same image runs three ways:
  1. `DEMO_MODE=true` (default)         -> synthetic data, no AWS creds needed.
  2. `DEMO_MODE=false` + AWS creds      -> live Cost Explorer / EC2 / CloudWatch.
  3. `DEMO_MODE=false`, creds missing   -> falls back to demo data automatically
     (see aws_client.get_source()), so a misconfigured deployment degrades
     gracefully instead of 500ing.
"""
from __future__ import annotations

import os


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class Settings:
    demo_mode: bool = _bool_env("DEMO_MODE", True)
    aws_region: str = os.environ.get("AWS_REGION", "us-east-1")
    # Target Savings Plan / RI coverage nOps-style tooling nudges accounts toward.
    target_commitment_coverage_pct: float = float(
        os.environ.get("TARGET_COMMITMENT_COVERAGE_PCT", "80")
    )
    cors_origins: list[str] = os.environ.get(
        "CORS_ORIGINS", "http://localhost:5173,http://localhost:4173"
    ).split(",")


settings = Settings()
