import { useEffect, useState } from "react";
import { api } from "./api";
import { CostTrendChart } from "./components/CostTrendChart";
import { RecommendationsTable } from "./components/RecommendationsTable";
import { SavingsPlanGauge } from "./components/SavingsPlanGauge";
import { ServiceBreakdown } from "./components/ServiceBreakdown";
import { SummaryCards } from "./components/SummaryCards";
import type {
  CommitmentCoverage,
  CostSummary,
  RecommendationsResponse,
  ServiceCost,
  TrendPoint,
} from "./types";

type Theme = "light" | "dark";

function useTheme(): [Theme | "system", (t: Theme | "system") => void] {
  const [theme, setThemeState] = useState<Theme | "system">(
    () => (localStorage.getItem("theme") as Theme | "system") ?? "system"
  );

  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") {
      root.removeAttribute("data-theme");
    } else {
      root.setAttribute("data-theme", theme);
    }
  }, [theme]);

  const setTheme = (t: Theme | "system") => {
    localStorage.setItem("theme", t);
    setThemeState(t);
  };

  return [theme, setTheme];
}

interface DashboardData {
  summary: CostSummary;
  trend: TrendPoint[];
  byService: ServiceCost[];
  coverage: CommitmentCoverage;
  recommendations: RecommendationsResponse;
}

export default function App() {
  const [theme, setTheme] = useTheme();
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      api.summary(),
      api.trend("daily"),
      api.byService(30),
      api.coverage(),
      api.recommendations(),
    ])
      .then(([summary, trend, byService, coverage, recommendations]) => {
        if (!cancelled) setData({ summary, trend, byService, coverage, recommendations });
      })
      .catch((e: Error) => {
        if (!cancelled) setError(e.message);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const cycleTheme = () => setTheme(theme === "system" ? "light" : theme === "light" ? "dark" : "system");

  return (
    <div className="app">
      <header className="app-header">
        <div>
          <h1>AWS Cost Optimizer</h1>
          <div className="subtitle">Spend visibility &amp; savings recommendations, nOps-style</div>
        </div>
        <div className="header-right">
          {data && (
            <span className="badge">
              <span
                className="badge-dot"
                style={{ background: data.summary.source === "aws" ? "var(--status-good)" : "var(--series-4)" }}
              />
              {data.summary.source === "aws" ? "Live AWS account" : "Demo data"}
            </span>
          )}
          <button className="theme-toggle" onClick={cycleTheme}>
            Theme: {theme}
          </button>
        </div>
      </header>

      {error && (
        <div className="error-box">
          Couldn't reach the API at the configured backend URL ({error}). Is the backend running? See the project
          README for `uvicorn app.main:app --reload`.
        </div>
      )}

      {!data && !error && <div className="loading">Loading cost data…</div>}

      {data && (
        <>
          <SummaryCards summary={data.summary} coverage={data.coverage} recs={data.recommendations} />

          <div className="panel-grid">
            <div className="card">
              <div className="panel-title">Daily spend, last 90 days</div>
              <CostTrendChart data={data.trend} />
            </div>
            <div className="card">
              <div className="panel-title">Cost by service</div>
              <ServiceBreakdown data={data.byService} />
            </div>
          </div>

          <div className="card" style={{ marginBottom: 16 }}>
            <div className="panel-title">Savings Plan / Reserved Instance coverage</div>
            <SavingsPlanGauge coverage={data.coverage} />
          </div>

          <div className="card">
            <div className="panel-title">Recommendations</div>
            <RecommendationsTable items={data.recommendations.items} />
          </div>
        </>
      )}

      <div className="footer-note">
        Demo data is synthetic. Point <code>DEMO_MODE=false</code> at a read-only AWS role to see your own account —
        see backend/README for the IAM policy.
      </div>
    </div>
  );
}
