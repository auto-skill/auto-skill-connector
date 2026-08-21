import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { TrendPoint } from "../types";

const currency = (n: number) =>
  n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });

function CustomTooltip({ active, payload, label }: any) {
  if (!active || !payload?.length) return null;
  return (
    <div className="tooltip-box">
      <div style={{ color: "var(--text-muted)" }}>{label}</div>
      <div style={{ fontWeight: 650 }}>{currency(payload[0].value)}</div>
    </div>
  );
}

export function CostTrendChart({ data }: { data: TrendPoint[] }) {
  // Last 90 days keeps the line readable; the API still returns the full
  // 13-month history for anyone who wants to fetch(granularity=monthly).
  const recent = data.slice(-90);
  const tickEvery = Math.ceil(recent.length / 6);

  return (
    <ResponsiveContainer width="100%" height={220}>
      <AreaChart data={recent} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
        <defs>
          <linearGradient id="trendFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--series-1)" stopOpacity={0.25} />
            <stop offset="100%" stopColor="var(--series-1)" stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke="var(--gridline)" vertical={false} />
        <XAxis
          dataKey="date"
          tick={{ fill: "var(--text-muted)", fontSize: 11 }}
          tickLine={false}
          axisLine={{ stroke: "var(--gridline)" }}
          interval={tickEvery}
          tickFormatter={(d: string) => d.slice(5)}
        />
        <YAxis
          tick={{ fill: "var(--text-muted)", fontSize: 11 }}
          tickLine={false}
          axisLine={false}
          width={56}
          tickFormatter={(v: number) => `$${Math.round(v / 100) / 10}k`}
        />
        <Tooltip content={<CustomTooltip />} cursor={{ stroke: "var(--text-muted)", strokeDasharray: "3 3" }} />
        <Area
          type="monotone"
          dataKey="cost"
          stroke="var(--series-1)"
          strokeWidth={2}
          fill="url(#trendFill)"
          activeDot={{ r: 4 }}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}
