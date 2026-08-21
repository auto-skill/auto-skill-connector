# Infra (reference, not applied)

This Terraform stack documents one reasonable way to deploy the backend to
AWS. It is **not run by anything in this repo** — treat it as a reviewable
sketch, not infra you'd `terraform apply` straight into production (no
remote state backend, no custom domain, no WAF in front of the Function
URL).

## What's here

- `iam-policy.json` — the least-privilege read-only policy the backend
  needs for live mode (Cost Explorer, EC2/EBS/RDS inventory, CloudWatch
  metrics). This is the important file to read even if you never touch
  Terraform: it's the actual permission boundary the app operates under.
- `main.tf` / `variables.tf` / `outputs.tf` — an ECR repo, a Lambda
  execution role scoped to that policy, and a container-image Lambda
  fronted by a Function URL.

## A note on the Lambda path

`backend/Dockerfile` runs `uvicorn` directly, which is correct for any
plain-container host (Docker locally, ECS/Fargate, App Runner, a VM) but
**not** sufficient on its own for AWS Lambda — Lambda needs a runtime
adapter in front of the ASGI app. The two standard options:

1. Add the [AWS Lambda Web Adapter](https://github.com/awslabs/aws-lambda-web-adapter)
   layer/extension to the image (no code changes, just a Dockerfile
   `COPY --from=` line and an env var) — the least invasive route.
2. Wrap `app.main:app` with [Mangum](https://mangum.io/) and point the
   Lambda handler at that instead of running uvicorn.

If you'd rather skip that step entirely, deploy `backend/Dockerfile`
as-is to **App Runner** or **ECS Fargate** — both run the container
verbatim and are a smaller conceptual jump from `docker run` than Lambda
is.

## Frontend hosting

The frontend is a static Vite build (`npm run build` → `frontend/dist/`).
Push it to S3 + CloudFront, Amplify Hosting, or Vercel/Netlify — any of
those is a better fit than hand-rolled Terraform for a handful of static
files, so it's deliberately left out of `main.tf`. Whichever you pick, set
`VITE_API_BASE_URL` at build time to the backend's URL (the
`backend_function_url` output here, or your API Gateway/ALB URL if you put
one in front of it).

## Usage sketch

```bash
terraform init
terraform apply                       # creates the ECR repo + IAM role first
docker build -t backend ../backend
docker tag backend "$(terraform output -raw ecr_repository_url):latest"
aws ecr get-login-password | docker login --username AWS --password-stdin "$(terraform output -raw ecr_repository_url)"
docker push "$(terraform output -raw ecr_repository_url):latest"
terraform apply                       # picks up the new image
```
