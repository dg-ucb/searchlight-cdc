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
  value       = { for dependency, layer in aws_lambda_layer_version.dependency : dependency => layer.arn }
}

output "queue_url" {
  description = "URL of the managed CDC FIFO queue."
  value       = aws_sqs_queue.cdc_events_q.url
}
