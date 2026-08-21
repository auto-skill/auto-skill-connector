"""Synthetic AWS billing + resource-inventory dataset.

Generates a deterministic (seeded) 13-month daily cost series across a
realistic AWS service mix, plus a small inventory of "wasteful" resources
(idle EC2, unattached EBS, unused Elastic IPs) and a Savings Plan coverage
snapshot. This is what the API serves when there are no AWS credentials, or
when DEMO_MODE=true, so the app is fully explorable with zero setup.

The shape mirrors what you'd actually get back from Cost Explorer / EC2 /
CloudWatch, so swapping demo_data.py for aws_client.py's live calls doesn't
change anything downstream (optimizer.py, the API schema, or the frontend).
"""
from __future__ import annotations

import hashlib
import math
import random
from datetime import date, timedelta

SEED = 20260101
DAYS_OF_HISTORY = 395  # ~13 months, so month-over-month trend has context

SERVICES = [
    # (name, baseline daily $, weekday amplitude, growth per day, volatility)
    ("Amazon EC2", 420.0, 0.18, 0.28, 0.06),
    ("Amazon S3", 96.0, 0.05, 0.09, 0.03),
    ("Amazon RDS", 210.0, 0.08, 0.05, 0.04),
    ("AWS Lambda", 34.0, 0.22, 0.12, 0.10),
    ("Amazon EBS", 88.0, 0.03, 0.04, 0.02),
    ("Amazon CloudFront", 52.0, 0.15, 0.03, 0.08),
    ("Amazon ElastiCache", 61.0, 0.04, 0.02, 0.03),
    ("Data Transfer", 74.0, 0.20, 0.06, 0.09),
    ("Amazon CloudWatch", 18.0, 0.05, 0.03, 0.05),
    ("Other", 40.0, 0.05, 0.02, 0.07),
]

# A deliberate "someone left a GPU fleet running" anomaly, the kind of thing
# nOps-style anomaly detection exists to catch. Starts day 340, tapers off
# after being "caught" 9 days later.
ANOMALY_START_DAY = 340
ANOMALY_SERVICE = "Amazon EC2"
ANOMALY_PEAK = 640.0
ANOMALY_DURATION = 9


def _rng_for(*parts: str) -> random.Random:
    key = "|".join(parts).encode()
    seed = int(hashlib.sha256(key).hexdigest()[:8], 16)
    return random.Random(seed)


def _daily_series(start: date, n_days: int) -> list[dict]:
    rows: list[dict] = []
    for svc, base, weekday_amp, growth, vol in SERVICES:
        rng = _rng_for("series", svc)
        for i in range(n_days):
            d = start + timedelta(days=i)
            weekday_factor = 1 - weekday_amp * (1 if d.weekday() >= 5 else 0)
            growth_factor = 1 + growth * (i / n_days)
            seasonal = 1 + 0.05 * math.sin(2 * math.pi * i / 30)
            noise = 1 + rng.uniform(-vol, vol)
            cost = base * weekday_factor * growth_factor * seasonal * noise

            if svc == ANOMALY_SERVICE and ANOMALY_START_DAY <= i < ANOMALY_START_DAY + ANOMALY_DURATION:
                progress = (i - ANOMALY_START_DAY) / ANOMALY_DURATION
                bump = ANOMALY_PEAK * math.sin(progress * math.pi)  # ramps up then down
                cost += bump

            rows.append({"date": d.isoformat(), "service": svc, "cost": round(cost, 2)})
    return rows


def daily_costs(today: date | None = None) -> list[dict]:
    today = today or date.today()
    start = today - timedelta(days=DAYS_OF_HISTORY - 1)
    return _daily_series(start, DAYS_OF_HISTORY)


def idle_resources(today: date | None = None) -> list[dict]:
    """A handful of resources sitting there burning money for no reason."""
    today = today or date.today()
    rng = _rng_for("idle", today.isoformat()[:7])  # stable within a month
    resources = [
        {
            "resource_id": "i-0a3f9c2e7b1d4f6aa",
            "resource_type": "EC2 Instance",
            "service": "Amazon EC2",
            "region": "us-east-1",
            "detail": "m5.2xlarge, avg CPU 2.1% over 14d",
            "monthly_waste": round(rng.uniform(210, 260), 2),
            "severity": "high",
            "recommended_action": "Downsize to m5.large or stop if unused",
        },
        {
            "resource_id": "i-0d88e21c4a9b7f001",
            "resource_type": "EC2 Instance",
            "service": "Amazon EC2",
            "region": "us-west-2",
            "detail": "r5.xlarge, avg CPU 4.6% over 14d",
            "monthly_waste": round(rng.uniform(120, 160), 2),
            "severity": "medium",
            "recommended_action": "Rightsize to r5.large",
        },
        {
            "resource_id": "vol-0f2a8c91de7b4c33e",
            "resource_type": "EBS Volume",
            "service": "Amazon EBS",
            "region": "us-east-1",
            "detail": "500 GiB gp3, unattached for 41 days",
            "monthly_waste": round(rng.uniform(38, 52), 2),
            "severity": "medium",
            "recommended_action": "Snapshot and delete",
        },
        {
            "resource_id": "vol-0b71c4e9a2f8d015c",
            "resource_type": "EBS Volume",
            "service": "Amazon EBS",
            "region": "us-east-1",
            "detail": "200 GiB gp2, unattached for 12 days",
            "monthly_waste": round(rng.uniform(14, 20), 2),
            "severity": "low",
            "recommended_action": "Snapshot and delete",
        },
        {
            "resource_id": "eipalloc-0c9d3f7b1a4e6021d",
            "resource_type": "Elastic IP",
            "service": "Amazon EC2",
            "region": "eu-west-1",
            "detail": "Unassociated for 30+ days",
            "monthly_waste": round(rng.uniform(3, 4), 2),
            "severity": "low",
            "recommended_action": "Release the address",
        },
        {
            "resource_id": "db-idle-0e4a7c1f9b2d8e33",
            "resource_type": "RDS Instance",
            "service": "Amazon RDS",
            "region": "us-east-1",
            "detail": "db.r5.large, 0 connections over 21d",
            "monthly_waste": round(rng.uniform(180, 230), 2),
            "severity": "high",
            "recommended_action": "Stop or snapshot-and-terminate",
        },
    ]
    return resources


def commitment_coverage(today: date | None = None) -> dict:
    today = today or date.today()
    rng = _rng_for("commitment", today.isoformat()[:7])
    on_demand_eligible_spend = round(rng.uniform(11500, 13200), 2)
    covered_pct = round(rng.uniform(52, 61), 1)
    covered_spend = round(on_demand_eligible_spend * covered_pct / 100, 2)
    on_demand_spend = round(on_demand_eligible_spend - covered_spend, 2)
    return {
        "on_demand_eligible_spend": on_demand_eligible_spend,
        "covered_spend": covered_spend,
        "on_demand_spend": on_demand_spend,
        "covered_pct": covered_pct,
    }
