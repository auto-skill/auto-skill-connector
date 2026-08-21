import type {
  CommitmentCoverage,
  CostSummary,
  RecommendationsResponse,
  ServiceCost,
  TrendPoint,
} from "./types";

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`);
  if (!res.ok) {
    throw new Error(`${path} failed: ${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  summary: () => get<CostSummary>("/api/costs/summary"),
  trend: (granularity: "daily" | "monthly" = "daily") =>
    get<TrendPoint[]>(`/api/costs/trend?granularity=${granularity}`),
  byService: (days = 30) => get<ServiceCost[]>(`/api/costs/by-service?days=${days}`),
  coverage: () => get<CommitmentCoverage>("/api/savings-plans/coverage"),
  recommendations: () => get<RecommendationsResponse>("/api/recommendations"),
};
