output "ecr_repository_url" {
  description = "Push the backend image here (docker build/tag/push), then set image_tag and apply again."
  value       = aws_ecr_repository.backend.repository_url
}

output "backend_function_url" {
  description = "Public HTTPS URL for the API. Set this as the frontend's VITE_API_BASE_URL."
  value       = aws_lambda_function_url.backend.function_url
}
