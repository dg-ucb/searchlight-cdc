output "function_name" {
  description = "Lambda managed by this environment's Terraform state."
  value       = aws_lambda_function.cdc_handler.function_name
}

output "function_arn" {
  description = "ARN of the deployed Lambda."
  value       = aws_lambda_function.cdc_handler.arn
}

output "dependency_layer_arns" {
  description = "Immutable dependency layer versions attached to the function."
  value       = local.dependency_layer_arns
}

output "queue_url" {
  description = "URL of the CDC FIFO queue, managed here or referenced from its owning stack."
  value       = local.queue_url
}
