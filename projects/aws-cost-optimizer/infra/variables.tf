variable "project_name" {
  description = "Prefix used for all created resource names."
  type        = string
  default     = "aws-cost-optimizer"
}

variable "aws_region" {
  description = "Region to deploy the backend Lambda and its ECR repo into."
  type        = string
  default     = "us-east-1"
}

variable "image_tag" {
  description = "Tag of the backend image (pushed to the ECR repo this stack creates) to deploy."
  type        = string
  default     = "latest"
}

variable "cors_origins" {
  description = "Origins allowed to call the API (your CloudFront/S3 frontend URL, plus localhost for dev)."
  type        = list(string)
  default     = ["http://localhost:5173"]
}
