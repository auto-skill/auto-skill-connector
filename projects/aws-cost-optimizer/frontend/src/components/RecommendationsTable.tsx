import { useState } from "react";
import type { Recommendation } from "../types";

const currency = (n: number) =>
  n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });

const SEVERITY_LABEL: Record<Recommendation["severity"], string> = {
  high: "High",
  medium: "Medium",
  low: "Low",
};

function SeverityPill({ severity }: { severity: Recommendation["severity"] }) {
  return <span className={`severity-pill severity-${severity}`}>{SEVERITY_LABEL[severity]}</span>;
}

export function RecommendationsTable({ items }: { items: Recommendation[] }) {
  const [applied, setApplied] = useState<Set<string>>(new Set());

  if (items.length === 0) {
    return <p className="panel-subtitle">No recommendations right now — spend looks well-optimized.</p>;
  }

  return (
    <div style={{ overflowX: "auto" }}>
      <table className="rec-table">
        <thead>
          <tr>
            <th>Severity</th>
            <th>Recommendation</th>
            <th>Service</th>
            <th style={{ textAlign: "right" }}>Est. monthly savings</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {items.map((r) => (
            <tr key={r.id}>
              <td>
                <SeverityPill severity={r.severity} />
              </td>
              <td>
                <div className="rec-desc">{r.description}</div>
                <div className="rec-action">{r.action}</div>
              </td>
              <td style={{ color: "var(--text-secondary)" }}>{r.service}</td>
              <td className="savings-cell" style={{ textAlign: "right", color: "var(--success-text)" }}>
                {r.monthly_savings != null ? currency(r.monthly_savings) : "—"}
              </td>
              <td>
                <button
                  className="apply-btn"
                  disabled={applied.has(r.id)}
                  onClick={() => setApplied((prev) => new Set(prev).add(r.id))}
                >
                  {applied.has(r.id) ? "✓ Applied" : "Apply"}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="panel-subtitle" style={{ marginTop: 10 }}>
        "Apply" is a UI mock in this demo — a production build would call back into AWS (e.g. stop/rightsize the
        instance, purchase the Savings Plan) and record the change for audit.
      </p>
    </div>
  );
}
