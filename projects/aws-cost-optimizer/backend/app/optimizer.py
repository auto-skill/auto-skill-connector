"""Turns raw cost + inventory data into the shapes the dashboard renders:
a spend summary, a trend series, a service breakdown, a commitment-coverage
snapshot, and a ranked list of savings recommendations.

Nothing here talks to AWS directly (see aws_client.py) — this module is pure
data transformation, which is what makes it unit-testable without any
network or credentials (see backend/tests/test_optimizer.py).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from .config import settings


def _parse(d: str) -> date:
    return datetime.strptime(d, "%Y-%m-%d").date()


def summary(daily_rows: list[dict]) -> dict:
    today = max(_parse(r["date"]) for r in daily_rows)
    month_start = today.replace(day=1)
    prev_month_end = month_start - timedelta(days=1)
    prev_month_start = prev_month_end.replace(day=1)

    mtd = sum(r["cost"] for r in daily_rows if _parse(r["date"]) >= month_start)
    days_elapsed = (today - month_start).days + 1

    prev_month_rows = [
        r for r in daily_rows if prev_month_start <= _parse(r["date"]) <= prev_month_end
    ]
    prev_month_total = sum(r["cost"] for r in prev_month_rows)
    prev_month_days = (prev_month_end - prev_month_start).days + 1
    prev_month_daily_avg = prev_month_total / prev_month_days if prev_month_days else 0

    daily_avg = mtd / days_elapsed if days_elapsed else 0
    days_in_month = _days_in_month(today)
    forecast = round(daily_avg * days_in_month, 2)

    trend_pct = (
        round((daily_avg - prev_month_daily_avg) / prev_month_daily_avg * 100, 1)
        if prev_month_daily_avg
        else 0.0
    )

    return {
        "mtd_spend": round(mtd, 2),
        "forecast_month_spend": forecast,
        "prev_month_spend": round(prev_month_total, 2),
        "trend_pct": trend_pct,
        "as_of": today.isoformat(),
    }


def _days_in_month(d: date) -> int:
    next_month = d.replace(day=28) + timedelta(days=4)
    return (next_month.replace(day=1) - timedelta(days=1)).day


def trend(daily_rows: list[dict], granularity: str = "daily") -> list[dict]:
    totals: dict[str, float] = defaultdict(float)
    for r in daily_rows:
        key = r["date"]
        if granularity == "monthly":
            key = key[:7]  # YYYY-MM
        totals[key] += r["cost"]
    return [
        {"date": k, "cost": round(v, 2)}
        for k, v in sorted(totals.items())
    ]


def by_service(daily_rows: list[dict], days: int = 30) -> list[dict]:
    if not daily_rows:
        return []
    latest = max(_parse(r["date"]) for r in daily_rows)
    cutoff = latest - timedelta(days=days - 1)
    totals: dict[str, float] = defaultdict(float)
    for r in daily_rows:
        if _parse(r["date"]) >= cutoff:
            totals[r["service"]] += r["cost"]
    grand_total = sum(totals.values()) or 1.0
    rows = [
        {"service": svc, "cost": round(cost, 2), "pct": round(cost / grand_total * 100, 1)}
        for svc, cost in totals.items()
    ]
    rows.sort(key=lambda r: r["cost"], reverse=True)
    return rows


def recommendations(idle: list[dict], coverage: dict) -> list[dict]:
    recs: list[dict] = []

    for r in idle:
        waste = r.get("monthly_waste")
        recs.append(
            {
                "id": f"idle::{r['resource_id']}",
                "type": "idle_resource",
                "service": r["service"],
                "resource_id": r["resource_id"],
                "severity": r["severity"],
                "monthly_savings": waste,
                "description": f"{r['resource_type']} {r['resource_id']} — {r['detail']}",
                "action": r["recommended_action"],
            }
        )

    gap_pct = settings.target_commitment_coverage_pct - coverage["covered_pct"]
    if gap_pct > 0:
        # On-demand spend that could move to a commitment, roughly discounted
        # at a typical 1yr no-upfront Savings Plan rate (~20%) to estimate
        # the monthly savings if coverage moved to target.
        addressable = coverage["on_demand_spend"] * min(1.0, gap_pct / 100 * 2)
        est_savings = round(addressable * 0.20, 2)
        if est_savings > 1:
            recs.append(
                {
                    "id": "commitment::savings-plan-gap",
                    "type": "commitment_gap",
                    "service": "Savings Plans",
                    "resource_id": None,
                    "severity": "high" if gap_pct > 25 else "medium",
                    "monthly_savings": est_savings,
                    "description": (
                        f"Savings Plan / RI coverage is {coverage['covered_pct']}%, "
                        f"{gap_pct:.0f} pts below the {settings.target_commitment_coverage_pct:.0f}% target"
                    ),
                    "action": "Purchase a 1yr no-upfront Compute Savings Plan sized to steady-state usage",
                }
            )

    recs.sort(key=lambda r: (r["monthly_savings"] or 0), reverse=True)
    return recs


def total_potential_savings(recs: list[dict]) -> float:
    return round(sum((r["monthly_savings"] or 0) for r in recs), 2)
