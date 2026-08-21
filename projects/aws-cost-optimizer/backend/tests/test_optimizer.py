from datetime import date

from app import demo_data, optimizer


def test_daily_costs_shape():
    rows = demo_data.daily_costs(today=date(2026, 6, 15))
    assert len(rows) == len(demo_data.SERVICES) * demo_data.DAYS_OF_HISTORY
    assert {"date", "service", "cost"} <= rows[0].keys()
    assert all(r["cost"] > 0 for r in rows)


def test_summary_mtd_matches_manual_sum():
    rows = demo_data.daily_costs(today=date(2026, 6, 15))
    s = optimizer.summary(rows)
    expected_mtd = round(
        sum(r["cost"] for r in rows if r["date"].startswith("2026-06") and int(r["date"][8:10]) <= 15),
        2,
    )
    assert s["mtd_spend"] == expected_mtd
    assert s["forecast_month_spend"] >= s["mtd_spend"]


def test_by_service_percentages_sum_to_100():
    rows = demo_data.daily_costs(today=date(2026, 6, 15))
    breakdown = optimizer.by_service(rows, days=30)
    assert len(breakdown) == len(demo_data.SERVICES)
    assert abs(sum(r["pct"] for r in breakdown) - 100.0) < 0.5
    # sorted descending by cost
    costs = [r["cost"] for r in breakdown]
    assert costs == sorted(costs, reverse=True)


def test_trend_daily_vs_monthly_granularity():
    rows = demo_data.daily_costs(today=date(2026, 6, 15))
    daily = optimizer.trend(rows, "daily")
    monthly = optimizer.trend(rows, "monthly")
    assert len(daily) == demo_data.DAYS_OF_HISTORY
    assert len(monthly) < len(daily)
    # total spend is conserved by re-aggregation
    assert abs(sum(r["cost"] for r in daily) - sum(r["cost"] for r in monthly)) < 0.5


def test_recommendations_include_idle_and_commitment_gap():
    idle = demo_data.idle_resources(today=date(2026, 6, 1))
    coverage = demo_data.commitment_coverage(today=date(2026, 6, 1))
    recs = optimizer.recommendations(idle, coverage)

    assert len(recs) == len(idle) + (1 if coverage["covered_pct"] < 80 else 0)
    idle_ids = {r["resource_id"] for r in idle}
    rec_idle_ids = {r["resource_id"] for r in recs if r["type"] == "idle_resource"}
    assert rec_idle_ids == idle_ids

    savings = [r["monthly_savings"] or 0 for r in recs]
    assert savings == sorted(savings, reverse=True)

    total = optimizer.total_potential_savings(recs)
    assert total == round(sum(savings), 2)


def test_recommendations_no_gap_when_coverage_meets_target():
    idle = []
    coverage = {
        "on_demand_eligible_spend": 1000.0,
        "covered_spend": 900.0,
        "on_demand_spend": 100.0,
        "covered_pct": 90.0,
    }
    recs = optimizer.recommendations(idle, coverage)
    assert recs == []
