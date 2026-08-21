"""Thin wrapper over the real AWS APIs, with an automatic fall back to
`demo_data` whenever DEMO_MODE is on, boto3 isn't configured, or a call
fails (e.g. the caller's IAM role is missing a permission).

This is the boundary the rest of the app depends on: `optimizer.py` and the
FastAPI routes only ever call the functions below, and never know whether
the numbers came from Cost Explorer or from the synthetic generator. That
keeps the "runs with zero AWS setup" demo path and the "point it at a real
account" path identical from the API down to the frontend.

Required IAM permissions for live mode (read-only; see ../infra/iam-policy.json):
  ce:GetCostAndUsage, ce:GetReservationUtilization, ce:GetSavingsPlansUtilization
  ec2:DescribeInstances, ec2:DescribeVolumes, ec2:DescribeAddresses
  cloudwatch:GetMetricData
  rds:DescribeDBInstances
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from functools import lru_cache
from typing import Literal

from . import demo_data
from .config import settings

logger = logging.getLogger("aws_cost_optimizer.aws_client")

Source = Literal["aws", "demo"]


@lru_cache(maxsize=1)
def _boto3_session():
    try:
        import boto3

        return boto3.session.Session(region_name=settings.aws_region)
    except Exception:  # pragma: no cover - boto3 always installed here
        return None


def _live_mode_available() -> bool:
    if settings.demo_mode:
        return False
    session = _boto3_session()
    if session is None:
        return False
    creds = session.get_credentials()
    return creds is not None


def get_source() -> Source:
    return "aws" if _live_mode_available() else "demo"


def fetch_daily_costs() -> tuple[list[dict], Source]:
    if _live_mode_available():
        try:
            return _live_daily_costs(), "aws"
        except Exception:
            logger.exception("Cost Explorer call failed, falling back to demo data")
    return demo_data.daily_costs(), "demo"


def fetch_idle_resources() -> tuple[list[dict], Source]:
    if _live_mode_available():
        try:
            return _live_idle_resources(), "aws"
        except Exception:
            logger.exception("EC2/CloudWatch inventory scan failed, falling back to demo data")
    return demo_data.idle_resources(), "demo"


def fetch_commitment_coverage() -> tuple[dict, Source]:
    if _live_mode_available():
        try:
            return _live_commitment_coverage(), "aws"
        except Exception:
            logger.exception("Cost Explorer reservation/SP call failed, falling back to demo data")
    return demo_data.commitment_coverage(), "demo"


# --- Live AWS calls -------------------------------------------------------
# Kept separate from the demo path so they're easy to unit test with a
# mocked boto3 client, and so a permission gap in any one of them only
# degrades that one endpoint's fallback rather than the whole app.

def _live_daily_costs() -> list[dict]:
    session = _boto3_session()
    ce = session.client("ce")
    end = date.today()
    start = end - timedelta(days=demo_data.DAYS_OF_HISTORY - 1)
    rows: list[dict] = []
    next_token = None
    while True:
        kwargs = dict(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
        if next_token:
            kwargs["NextPageToken"] = next_token
        resp = ce.get_cost_and_usage(**kwargs)
        for period in resp["ResultsByTime"]:
            d = period["TimePeriod"]["Start"]
            for group in period["Groups"]:
                svc = group["Keys"][0]
                cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
                rows.append({"date": d, "service": svc, "cost": round(cost, 2)})
        next_token = resp.get("NextPageToken")
        if not next_token:
            break
    return rows


def _live_idle_resources() -> list[dict]:
    session = _boto3_session()
    ec2 = session.client("ec2")
    cw = session.client("cloudwatch")
    resources: list[dict] = []

    instances = ec2.describe_instances(
        Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
    )
    for reservation in instances["Reservations"]:
        for inst in reservation["Instances"]:
            instance_id = inst["InstanceId"]
            metric = cw.get_metric_statistics(
                Namespace="AWS/EC2",
                MetricName="CPUUtilization",
                Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
                StartTime=date.today() - timedelta(days=14),
                EndTime=date.today(),
                Period=86400,
                Statistics=["Average"],
            )
            points = metric.get("Datapoints", [])
            if not points:
                continue
            avg_cpu = sum(p["Average"] for p in points) / len(points)
            if avg_cpu < 5.0:
                resources.append(
                    {
                        "resource_id": instance_id,
                        "resource_type": "EC2 Instance",
                        "service": "Amazon EC2",
                        "region": settings.aws_region,
                        "detail": f"{inst['InstanceType']}, avg CPU {avg_cpu:.1f}% over 14d",
                        "monthly_waste": None,  # would need Pricing API for an exact $ figure
                        "severity": "high" if avg_cpu < 2 else "medium",
                        "recommended_action": "Rightsize or stop if unused",
                    }
                )

    volumes = ec2.describe_volumes(
        Filters=[{"Name": "status", "Values": ["available"]}]  # available == unattached
    )
    for vol in volumes["Volumes"]:
        resources.append(
            {
                "resource_id": vol["VolumeId"],
                "resource_type": "EBS Volume",
                "service": "Amazon EBS",
                "region": settings.aws_region,
                "detail": f"{vol['Size']} GiB {vol['VolumeType']}, unattached",
                "monthly_waste": None,
                "severity": "medium",
                "recommended_action": "Snapshot and delete",
            }
        )

    addresses = ec2.describe_addresses()
    for addr in addresses.get("Addresses", []):
        if "InstanceId" not in addr and "AssociationId" not in addr:
            resources.append(
                {
                    "resource_id": addr.get("AllocationId", addr.get("PublicIp")),
                    "resource_type": "Elastic IP",
                    "service": "Amazon EC2",
                    "region": settings.aws_region,
                    "detail": "Unassociated",
                    "monthly_waste": None,
                    "severity": "low",
                    "recommended_action": "Release the address",
                }
            )

    return resources


def _live_commitment_coverage() -> dict:
    session = _boto3_session()
    ce = session.client("ce")
    end = date.today()
    start = end.replace(day=1)
    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        Filter={
            "Dimensions": {
                "Key": "RECORD_TYPE",
                "Values": ["Usage"],
            }
        },
    )
    on_demand_eligible_spend = sum(
        float(p["Total"]["UnblendedCost"]["Amount"]) for p in resp["ResultsByTime"]
    )

    util = ce.get_reservation_utilization(
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()}
    )
    covered_pct = float(util["Total"]["UtilizationPercentage"])
    covered_spend = round(on_demand_eligible_spend * covered_pct / 100, 2)
    return {
        "on_demand_eligible_spend": round(on_demand_eligible_spend, 2),
        "covered_spend": covered_spend,
        "on_demand_spend": round(on_demand_eligible_spend - covered_spend, 2),
        "covered_pct": round(covered_pct, 1),
    }
