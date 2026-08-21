"""API entrypoint.

    uvicorn app.main:app --reload --port 8000

Every route composes `aws_client.fetch_*` (which decides demo vs. live) with
`optimizer.py` (pure transforms). Routes never branch on demo-vs-live
themselves — that keeps the fallback behavior in exactly one place.
"""
from __future__ import annotations

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from . import aws_client, optimizer
from .config import settings
from .models import (
    CommitmentCoverage,
    CostSummary,
    IdleResourcesResponse,
    RecommendationsResponse,
    ServiceCost,
    TrendPoint,
)

app = FastAPI(
    title="AWS Cost Optimizer",
    description="Cost visibility and savings recommendations for an AWS account, nOps-style.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "source": aws_client.get_source()}


@app.get("/api/costs/summary", response_model=CostSummary)
def costs_summary() -> CostSummary:
    rows, source = aws_client.fetch_daily_costs()
    data = optimizer.summary(rows)
    return CostSummary(**data, source=source)


@app.get("/api/costs/trend", response_model=list[TrendPoint])
def costs_trend(
    granularity: str = Query("daily", pattern="^(daily|monthly)$")
) -> list[TrendPoint]:
    rows, _source = aws_client.fetch_daily_costs()
    return [TrendPoint(**r) for r in optimizer.trend(rows, granularity)]


@app.get("/api/costs/by-service", response_model=list[ServiceCost])
def costs_by_service(days: int = Query(30, ge=1, le=395)) -> list[ServiceCost]:
    rows, _source = aws_client.fetch_daily_costs()
    return [ServiceCost(**r) for r in optimizer.by_service(rows, days)]


@app.get("/api/savings-plans/coverage", response_model=CommitmentCoverage)
def savings_plan_coverage() -> CommitmentCoverage:
    coverage, source = aws_client.fetch_commitment_coverage()
    return CommitmentCoverage(
        **coverage,
        target_pct=settings.target_commitment_coverage_pct,
        source=source,
    )


@app.get("/api/resources/idle", response_model=IdleResourcesResponse)
def idle_resources() -> IdleResourcesResponse:
    items, source = aws_client.fetch_idle_resources()
    return IdleResourcesResponse(items=items, source=source)


@app.get("/api/recommendations", response_model=RecommendationsResponse)
def recommendations() -> RecommendationsResponse:
    idle, idle_source = aws_client.fetch_idle_resources()
    coverage, coverage_source = aws_client.fetch_commitment_coverage()
    recs = optimizer.recommendations(idle, coverage)
    source = "aws" if idle_source == "aws" and coverage_source == "aws" else "demo"
    return RecommendationsResponse(
        items=recs,
        total_monthly_savings=optimizer.total_potential_savings(recs),
        source=source,
    )
