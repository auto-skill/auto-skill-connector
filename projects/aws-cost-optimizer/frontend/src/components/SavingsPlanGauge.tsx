import type { CommitmentCoverage } from "../types";

const currency = (n: number) =>
  n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });

export function SavingsPlanGauge({ coverage }: { coverage: CommitmentCoverage }) {
  const covered = Math.max(0, Math.min(100, coverage.covered_pct));
  return (
    <div>
      <div className="coverage-bar-track">
        <div
          className="coverage-bar-fill"
          style={{ width: `${covered}%`, background: "var(--status-good)" }}
        />
        <div
          className="coverage-bar-fill"
          style={{ width: `${100 - covered}%`, background: "var(--series-1)" }}
        />
        <div className="coverage-target-marker" style={{ left: `${coverage.target_pct}%` }} />
      </div>
      <div className="legend-row">
        <span className="legend-item">
          <span className="legend-swatch" style={{ background: "var(--status-good)" }} />
          Covered ({currency(coverage.covered_spend)})
        </span>
        <span className="legend-item">
          <span className="legend-swatch" style={{ background: "var(--series-1)" }} />
          On-demand ({currency(coverage.on_demand_spend)})
        </span>
        <span className="legend-item">
          <span className="legend-swatch" style={{ background: "var(--text-primary)", width: 2, borderRadius: 0 }} />
          {coverage.target_pct}% target
        </span>
      </div>
      <div className="coverage-numbers">
        <span>{covered}% covered</span>
        <span>{currency(coverage.on_demand_eligible_spend)} eligible spend</span>
      </div>
    </div>
  );
}
