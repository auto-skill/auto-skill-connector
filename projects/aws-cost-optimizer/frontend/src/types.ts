export interface CostSummary {
  mtd_spend: number;
  forecast_month_spend: number;
  prev_month_spend: number;
  trend_pct: number;
  as_of: string;
  source: "aws" | "demo";
}

export interface TrendPoint {
  date: string;
  cost: number;
}

export interface ServiceCost {
  service: string;
  cost: number;
  pct: number;
}

export interface CommitmentCoverage {
  on_demand_eligible_spend: number;
  covered_spend: number;
  on_demand_spend: number;
  covered_pct: number;
  target_pct: number;
  source: "aws" | "demo";
}

export type Severity = "low" | "medium" | "high";

export interface Recommendation {
  id: string;
  type: "idle_resource" | "commitment_gap";
  service: string;
  resource_id: string | null;
  severity: Severity;
  monthly_savings: number | null;
  description: string;
  action: string;
}

export interface RecommendationsResponse {
  items: Recommendation[];
  total_monthly_savings: number;
  source: "aws" | "demo";
}
