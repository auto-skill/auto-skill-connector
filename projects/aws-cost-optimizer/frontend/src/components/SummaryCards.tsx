import type { CommitmentCoverage, CostSummary, RecommendationsResponse } from "../types";

const currency = (n: number) =>
  n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });

export function SummaryCards({
  summary,
  coverage,
  recs,
}: {
  summary: CostSummary;
  coverage: CommitmentCoverage;
  recs: RecommendationsResponse;
}) {
  const trendUp = summary.trend_pct > 0;
  return (
    <div className="stat-grid">
      <div className="card stat-card">
        <div className="label">Month-to-date spend</div>
        <div className="value">{currency(summary.mtd_spend)}</div>
        <div className={`delta ${trendUp ? "up" : "down"}`}>
          {trendUp ? "▲" : "▼"} {Math.abs(summary.trend_pct)}% vs. last month's pace
        </div>
      </div>
      <div className="card stat-card">
        <div className="label">Forecasted month</div>
        <div className="value">{currency(summary.forecast_month_spend)}</div>
        <div className="delta">as of {summary.as_of}</div>
      </div>
      <div className="card stat-card">
        <div className="label">Savings Plan coverage</div>
        <div className="value">{coverage.covered_pct}%</div>
        <div className="delta">target {coverage.target_pct}%</div>
      </div>
      <div className="card stat-card">
        <div className="label">Potential monthly savings</div>
        <div className="value" style={{ color: "var(--success-text)" }}>
          {currency(recs.total_monthly_savings)}
        </div>
        <div className="delta">across {recs.items.length} recommendations</div>
      </div>
    </div>
  );
}
