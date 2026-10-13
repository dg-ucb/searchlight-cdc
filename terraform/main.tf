locals {
  artifacts_directory = coalesce(var.artifacts_directory, "${path.module}/../dist/release")
  dependency_names = merge({
    boto3    = "${var.function_name}-boto3"
    psycopg2 = "${var.function_name}-psycopg2"
  }, var.dependency_layer_names)
}

data "aws_partition" "current" {}

resource "aws_lambda_layer_version" "dependency" {
  for_each = toset(["boto3", "psycopg2"])

  filename                 = "${local.artifacts_directory}/${each.key}.zip"
  source_code_hash         = filebase64sha256("${local.artifacts_directory}/${each.key}.zip")
  layer_name               = local.dependency_names[each.key]
  description              = "${each.key} dependencies for ${var.function_name}"
  compatible_runtimes      = [var.runtime]
  compatible_architectures = var.architectures

  # Preserve previous immutable versions for rollback and other consumers.
  # Retention cleanup is an explicit infrastructure maintenance task.
  skip_destroy = true

  lifecycle {
    create_before_destroy = true

    precondition {
      condition     = can(regex("^[A-Za-z0-9_-]{1,64}$", local.dependency_names[each.key]))
      error_message = "The dependency layer name exceeds Lambda's name limit. Set dependency_layer_names to shorter names."
    }
  }
}

resource "aws_lambda_function" "cdc_handler" {
  filename         = "${local.artifacts_directory}/function.zip"
  source_code_hash = filebase64sha256("${local.artifacts_directory}/function.zip")
  function_name    = var.function_name
  role             = aws_iam_role.lambda_execution_role.arn
  handler          = var.handler
  runtime          = var.runtime
  architectures    = var.architectures
  memory_size      = var.memory_size
  timeout          = var.timeout

  layers = [
    aws_lambda_layer_version.dependency["boto3"].arn,
    aws_lambda_layer_version.dependency["psycopg2"].arn,
  ]

  environment {
    variables = var.environment_variables
  }

  vpc_config {
    security_group_ids = var.security_group_ids
    subnet_ids         = var.subnet_ids
  }

  lifecycle {
    create_before_destroy = true
  }

  tags = var.lambda_tags

  depends_on = [
    aws_iam_role_policy_attachment.lambda_execution_policy,
    aws_iam_role_policy_attachment.lambda_sqs_policy_attachment,
    aws_iam_role_policy_attachment.lambda_vpc_policy_attachment,
    aws_iam_role_policy_attachment.additional_execution_policy,
    aws_iam_role_policy.database_secret,
  ]
}

data "aws_iam_policy_document" "assume_role" {
  version = "2012-10-17"

  statement {
    actions = ["sts:AssumeRole"]
    effect  = "Allow"

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda_execution_role" {
  name               = var.execution_role_name
  assume_role_policy = data.aws_iam_policy_document.assume_role.json
  description        = var.execution_role_description
  tags               = var.execution_role_tags
}

resource "aws_iam_role_policy_attachment" "lambda_execution_policy" {
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
  role       = aws_iam_role.lambda_execution_role.name
}

resource "aws_iam_role_policy_attachment" "lambda_vpc_policy_attachment" {
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
  role       = aws_iam_role.lambda_execution_role.name
}

resource "aws_iam_role_policy" "database_secret" {
  count = var.database_secret_arn == null ? 0 : 1

  name = "${var.function_name}-database-secret"
  role = aws_iam_role.lambda_execution_role.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [var.database_secret_arn]
      }
      ], var.database_secret_kms_key_arn == null ? [] : [
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = [var.database_secret_kms_key_arn]
      }
    ])
  })
}

resource "aws_iam_role_policy_attachment" "additional_execution_policy" {
  for_each = var.additional_execution_policy_arns

  policy_arn = each.value
  role       = aws_iam_role.lambda_execution_role.name
}

resource "aws_sqs_queue" "cdc_events_q" {
  name                        = var.queue_name
  delay_seconds               = var.queue_delay_seconds
  fifo_queue                  = true
  content_based_deduplication = var.queue_content_based_deduplication
  max_message_size            = var.queue_max_message_size
  receive_wait_time_seconds   = var.queue_receive_wait_time_seconds
  visibility_timeout_seconds  = var.queue_visibility_timeout_seconds
  message_retention_seconds   = var.queue_message_retention_seconds
  sqs_managed_sse_enabled     = true
  tags                        = var.queue_tags
}

# Preserve the original data address. The existing deployment attaches the AWS
# managed SQS policy below; this document remains available for a later migration.
data "aws_iam_policy_document" "sqs_policy" {
  version = "2012-10-17"

  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    effect    = "Allow"
    resources = [aws_sqs_queue.cdc_events_q.arn]
  }
}

resource "aws_iam_role_policy_attachment" "lambda_sqs_policy_attachment" {
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaSQSQueueExecutionRole"
  role       = aws_iam_role.lambda_execution_role.name
}

resource "aws_lambda_event_source_mapping" "sqs_mapping" {
  event_source_arn        = aws_sqs_queue.cdc_events_q.arn
  function_name           = aws_lambda_function.cdc_handler.function_name
  batch_size              = var.batch_size
  enabled                 = var.event_source_enabled
  function_response_types = var.function_response_types

  depends_on = [aws_iam_role_policy_attachment.lambda_sqs_policy_attachment]
}
