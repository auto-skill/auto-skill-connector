# AWS Cost Optimizer

A small full-stack app that surfaces AWS spend visibility and savings
recommendations: cost trend over time, cost by service, Savings Plan / RI
coverage against a target, and a ranked list of waste (idle EC2/RDS,
unattached EBS, unassociated EIPs, and commitment-coverage gaps) with an
estimated dollar impact for each.

It runs with **zero AWS setup** — a synthetic but realistic dataset ships
by default — and the exact same API can be pointed at a real AWS account
by handing it a read-only IAM role.

## Why this project

Built as a portfolio piece for [nOps'](https://www.nops.io) Full Stack
Builder/Engineer Intern role. nOps is an AWS cost-optimization platform —
cost visibility, idle-resource detection, and automated Savings
Plan/Reserved Instance commitment management, at the scale of billions of
dollars of AWS spend under management. This project is a small-scale,
single-account version of that same shape end to end:

- **Backend** talks to the real AWS billing/inventory APIs a cost platform
  is built on (Cost Explorer, EC2/EBS/RDS inventory, CloudWatch metrics),
  the same surface nOps' own product operates against.
- **Recommendations logic** mirrors the two things nOps leads with: idle/
  underutilized resource waste, and Savings Plan coverage gaps, both
  reduced to a "here's the dollar amount and what to do about it" list.
- **Full-stack, not a script**: a typed FastAPI backend and a React/
  TypeScript dashboard, the kind of pairing the role's title points at.

## Architecture

```
frontend/  React + TypeScript + Vite + Recharts — the dashboard
backend/   FastAPI + boto3 — cost/inventory API, demo-mode fallback
infra/     Terraform + IAM policy — reference deployment, not applied
```

The backend has one seam that matters: `app/aws_client.py`. Every route in
`app/main.py` goes through it, and it decides — per call, with a graceful
fallback on any error or missing credential — whether to hit real AWS or
return the synthetic dataset in `app/demo_data.py`. Nothing downstream
(the transform logic in `app/optimizer.py`, the API schema, or the
frontend) knows or cares which one it got; a `source: "aws" | "demo"` field
on every response is the only tell, and the dashboard surfaces it as a
badge.

## Running it

**Backend** (demo mode, no AWS account needed):

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

**Frontend**, in another terminal:

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173 — you should see ~13 months of synthetic daily
spend across a realistic AWS service mix (with a deliberate cost anomaly
planted near the end of the series), a Savings Plan coverage bar sitting
below target, and a handful of idle-resource recommendations.

Or with Docker:

```bash
docker compose up --build
```

### Pointing it at a real AWS account

Set `DEMO_MODE=false` on the backend and give it credentials for a role
with the permissions in [`infra/iam-policy.json`](infra/iam-policy.json)
(read-only: Cost Explorer, EC2/EBS/RDS describe calls, CloudWatch metrics —
no write access anywhere). If a live call fails for any reason (missing
permission, no credentials found, API error), that one endpoint falls back
to demo data automatically rather than the app breaking.

## Backend tests

```bash
cd backend && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Tests cover the pure transform logic in `optimizer.py` against the
synthetic dataset — spend aggregation, trend re-aggregation, and
recommendation ranking — deliberately without any AWS mocking, since that
logic doesn't know or care where the numbers came from.

## What's deliberately out of scope

This is a portfolio-scale demo, not a production cost platform. Left out
on purpose: multi-account/Organizations support, real commitment
purchasing (the "Apply" button is a UI mock), auth, and a real Pricing-API-
backed dollar figure for live-mode idle resources (that needs the Pricing
API in addition to CloudWatch, and is stubbed as `null` in
`aws_client._live_idle_resources`). Each is a natural next step, not an
oversight.
