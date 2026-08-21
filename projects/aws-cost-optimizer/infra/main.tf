# Reference infrastructure for the backend half of this project: a
# container-image Lambda (the same image built from ../backend/Dockerfile)
# behind a public Function URL, with a least-privilege read-only role for
# the Cost Explorer / EC2 / CloudWatch calls in app/aws_client.py.
#
# This is intentionally the backend only. The frontend is a static Vite
# build (`npm run build` -> dist/) — host it on S3 + CloudFront, Amplify
# Hosting, or Vercel/Netlify; any of those is a better fit than Terraform
# boilerplate for a handful of static files, so it's left as ops choice
# rather than encoded here.
#
# This stack is a reference, not applied by anything in this repo: it
# documents the deployment shape rather than standing in for real
# infra-as-code review before use in a real account (e.g. you'd want a
# remote state backend, tags/cost-allocation, and probably API Gateway +
# a custom domain in front of the Function URL).

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}

# --- ECR repo for the backend image --------------------------------------

resource "aws_ecr_repository" "backend" {
  name                 = "${var.project_name}-backend"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
}

# --- IAM: Lambda execution role, least-privilege read-only cost access ---

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "backend_lambda" {
  name               = "${var.project_name}-backend-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "basic_execution" {
  role       = aws_iam_role.backend_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "cost_read_only" {
  name   = "${var.project_name}-cost-read-only"
  role   = aws_iam_role.backend_lambda.id
  policy = file("${path.module}/iam-policy.json")
}

# --- Lambda (container image) + public Function URL ----------------------

resource "aws_lambda_function" "backend" {
  function_name = "${var.project_name}-backend"
  role          = aws_iam_role.backend_lambda.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.backend.repository_url}:${var.image_tag}"
  timeout       = 15
  memory_size   = 512

  environment {
    variables = {
      DEMO_MODE    = "false"
      CORS_ORIGINS = join(",", var.cors_origins)
    }
  }
}

resource "aws_lambda_function_url" "backend" {
  function_name      = aws_lambda_function.backend.function_name
  authorization_type = "NONE" # put API Gateway + auth in front of this for anything beyond a demo

  cors {
    allow_origins = var.cors_origins
    allow_methods = ["GET"]
    allow_headers = ["content-type"]
  }
}
