import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { ServiceCost } from "../types";

const SERIES_VARS = [
  "--series-1",
  "--series-2",
  "--series-3",
  "--series-4",
  "--series-5",
  "--series-6",
  "--series-7",
  "--series-8",
];

const currency = (n: number) =>
  n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });

const shortLabel = (service: string) => service.replace(/^(Amazon|AWS)\s+/, "");

/** Caps the series count at 8 (the palette's validated categorical set) by
 * folding everything past the top 7 into "Other" — see dataviz skill,
 * palette.md: "past three [for all-pairs forms]... fold to Other or facet".
 * A bar chart only needs adjacent-pair safety, so all 8 fixed-order slots
 * are usable here, with the 8th reserved for the Other bucket. */
function foldToTopN(rows: ServiceCost[], n = 7): ServiceCost[] {
  if (rows.length <= n + 1) return rows;
  const top = rows.slice(0, n);
  const rest = rows.slice(n);
  const otherCost = rest.reduce((sum, r) => sum + r.cost, 0);
  const otherPct = rest.reduce((sum, r) => sum + r.pct, 0);
  return [...top, { service: "Other", cost: Math.round(otherCost * 100) / 100, pct: Math.round(otherPct * 10) / 10 }];
}

function CustomTooltip({ active, payload }: any) {
  if (!active || !payload?.length) return null;
  const row = payload[0].payload as ServiceCost;
  return (
    <div className="tooltip-box">
      <div style={{ fontWeight: 650 }}>{row.service}</div>
      <div>
        {currency(row.cost)} · {row.pct}%
      </div>
    </div>
  );
}

export function ServiceBreakdown({ data }: { data: ServiceCost[] }) {
  const rows = foldToTopN(data);

  return (
    <>
      <ResponsiveContainer width="100%" height={220}>
        <BarChart data={rows} layout="vertical" margin={{ top: 4, right: 24, left: 8, bottom: 0 }}>
          <CartesianGrid stroke="var(--gridline)" horizontal={false} />
          <XAxis
            type="number"
            tick={{ fill: "var(--text-muted)", fontSize: 11 }}
            tickLine={false}
            axisLine={{ stroke: "var(--gridline)" }}
            tickFormatter={(v: number) => `$${Math.round(v)}`}
          />
          <YAxis
            type="category"
            dataKey="service"
            tickFormatter={shortLabel}
            tick={{ fill: "var(--text-secondary)", fontSize: 12 }}
            tickLine={false}
            axisLine={false}
            width={100}
          />
          <Tooltip content={<CustomTooltip />} cursor={{ fill: "var(--gridline)", opacity: 0.4 }} />
          <Bar dataKey="cost" radius={[0, 4, 4, 0]} barSize={16}>
            {rows.map((_, i) => (
              <Cell key={i} fill={`var(${SERIES_VARS[i % SERIES_VARS.length]})`} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
      <div className="panel-subtitle" style={{ marginTop: 4 }}>
        Trailing 30 days, by service
      </div>
    </>
  );
}
