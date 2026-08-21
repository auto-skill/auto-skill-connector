"""Pydantic response models — kept separate from the transformation logic
in optimizer.py so the API's contract is explicit and typed, and so
`main.py` stays thin.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

Severity = Literal["low", "medium", "high"]
Source = Literal["aws", "demo"]


class CostSummary(BaseModel):
    mtd_spend: float
    forecast_month_spend: float
    prev_month_spend: float
    trend_pct: float
    as_of: str
    source: Source


class TrendPoint(BaseModel):
    date: str
    cost: float


class ServiceCost(BaseModel):
    service: str
    cost: float
    pct: float


class CommitmentCoverage(BaseModel):
    on_demand_eligible_spend: float
    covered_spend: float
    on_demand_spend: float
    covered_pct: float
    target_pct: float
    source: Source


class Recommendation(BaseModel):
    id: str
    type: Literal["idle_resource", "commitment_gap"]
    service: str
    resource_id: Optional[str] = None
    severity: Severity
    monthly_savings: Optional[float] = None
    description: str
    action: str


class RecommendationsResponse(BaseModel):
    items: list[Recommendation]
    total_monthly_savings: float
    source: Source


class IdleResource(BaseModel):
    resource_id: str
    resource_type: str
    service: str
    region: str
    detail: str
    monthly_waste: Optional[float] = None
    severity: Severity
    recommended_action: str


class IdleResourcesResponse(BaseModel):
    items: list[IdleResource]
    source: Source
