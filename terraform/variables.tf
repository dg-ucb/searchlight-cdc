variable "region" {
  description = "AWS region for this environment, supplied through Parameter Store."
  type        = string
}

variable "allowed_account_id" {
  description = "Expected target AWS account. The provider rejects credentials for other accounts."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.allowed_account_id))
    error_message = "allowed_account_id must contain exactly twelve digits."
  }
}

variable "function_name" {
  description = "Existing Lambda function name for this environment."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9_-]{1,64}$", var.function_name))
    error_message = "function_name must contain 1–64 letters, digits, underscores, or hyphens."
  }
}

variable "execution_role_name" {
  description = "Existing Lambda execution role name; this is separate from the CodeBuild role."
  type        = string
}

variable "execution_role_description" {
  description = "Description of the existing Lambda execution role. Set this to its current value when adopting existing state."
  type        = string
  default     = null
}

variable "execution_role_tags" {
  description = "Tags for the Lambda execution role."
  type        = map(string)
  default     = {}
}

variable "additional_execution_policy_arns" {
  description = "Additional existing managed policies for application services. Existing unmanaged attachments are retained."
  type        = set(string)
  default     = []
}

variable "database_secret_arn" {
  description = "Optional exact ARN of the existing database secret. When supplied, Terraform grants GetSecretValue on only this secret; it never retrieves the secret contents. Null preserves externally managed secret permissions."
  type        = string
  default     = null

  validation {
    condition     = var.database_secret_arn == null || can(regex("^arn:[^:]+:secretsmanager:[^:]+:[0-9]{12}:secret:[^*?]+$", var.database_secret_arn))
    error_message = "database_secret_arn must be an exact Secrets Manager secret ARN without wildcards."
  }
}

variable "database_secret_kms_key_arn" {
  description = "Optional exact customer-managed KMS key ARN used by the database secret. Requires database_secret_arn; AWS-managed secret keys do not need this input."
  type        = string
  default     = null

  validation {
    condition = var.database_secret_kms_key_arn == null || (
      var.database_secret_arn != null && can(regex("^arn:[^:]+:kms:[^:]+:[0-9]{12}:key/[^*?]+$", var.database_secret_kms_key_arn))
    )
    error_message = "A customer-managed key requires database_secret_arn and an exact KMS key ARN without wildcards."
  }
}

variable "environment_variables" {
  description = "Lambda environment configuration from Parameter Store. Supply database secret identifiers, not database passwords; the function reads credentials from Secrets Manager at runtime. Values are sensitive in Terraform output but remain in encrypted remote state."
  type        = map(string)
  sensitive   = true
}

variable "subnet_ids" {
  description = "Existing private subnet IDs for the Lambda."
  type        = set(string)

  validation {
    condition     = length(var.subnet_ids) > 0
    error_message = "At least one existing private subnet is required."
  }
}

variable "security_group_ids" {
  description = "Existing security group IDs for the Lambda."
  type        = set(string)

  validation {
    condition     = length(var.security_group_ids) > 0
    error_message = "At least one existing security group is required."
  }
}

variable "handler" {
  description = "Lambda entry point."
  type        = string
  default     = "handler.lambda_handler"
}

variable "runtime" {
  description = "Runtime supported by the repository's dependency packaging."
  type        = string
  default     = "python3.13"

  validation {
    condition     = var.runtime == "python3.13"
    error_message = "The current dependency build supports python3.13. Update the buildspec before changing the runtime."
  }
}

variable "architectures" {
  description = "Instruction architecture supported by the dependency ZIPs."
  type        = list(string)
  default     = ["x86_64"]

  validation {
    condition     = length(var.architectures) == 1 && alltrue([for architecture in var.architectures : architecture == "x86_64"])
    error_message = "The current dependency build supports x86_64. Update the buildspec before changing the architecture."
  }
}

variable "memory_size" {
  description = "Lambda memory in MiB; 128 preserves the original Terraform default."
  type        = number
  default     = 128

  validation {
    condition     = var.memory_size >= 128 && var.memory_size <= 10240 && floor(var.memory_size) == var.memory_size
    error_message = "memory_size must be an integer from 128 through 10240."
  }
}

variable "timeout" {
  description = "Lambda timeout in seconds; 3 preserves the original Terraform default."
  type        = number
  default     = 3

  validation {
    condition     = var.timeout >= 1 && var.timeout <= 900 && floor(var.timeout) == var.timeout
    error_message = "timeout must be an integer from 1 through 900."
  }
}

variable "lambda_tags" {
  description = "Tags for the Lambda function."
  type        = map(string)
  default     = {}
}

variable "artifacts_directory" {
  description = "Directory containing function.zip, boto3.zip, and psycopg2.zip. Null selects ../dist/release relative to this Terraform directory."
  type        = string
  default     = null
}

variable "dependency_layer_names" {
  description = "Optional boto3 and psycopg2 layer names. Defaults use the function name as a prefix."
  type        = map(string)
  default     = {}

  validation {
    condition = alltrue([
      for key, name in var.dependency_layer_names :
      contains(["boto3", "psycopg2"], key) && can(regex("^[A-Za-z0-9_-]{1,64}$", name))
    ])
    error_message = "Only boto3 and psycopg2 keys are allowed, with valid Lambda layer names of up to 64 characters."
  }
}

variable "queue_name" {
  description = "Existing FIFO queue name for the CDC event source."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9_-]{1,75}\\.fifo$", var.queue_name))
    error_message = "queue_name must be a valid FIFO queue name ending in .fifo."
  }
}

variable "queue_delay_seconds" {
  description = "SQS delivery delay in seconds, preserving the original setting by default."
  type        = number
  default     = 5
}

variable "queue_max_message_size" {
  description = "SQS maximum message size in bytes, preserving the original setting by default."
  type        = number
  default     = 1048576
}

variable "queue_receive_wait_time_seconds" {
  description = "SQS long polling wait in seconds, preserving the original setting by default."
  type        = number
  default     = 5
}

variable "queue_visibility_timeout_seconds" {
  description = "SQS visibility timeout in seconds; set to the existing environment's value."
  type        = number
  default     = 30
}

variable "queue_message_retention_seconds" {
  description = "SQS message retention in seconds; the default preserves the original provider default."
  type        = number
  default     = 345600
}

variable "queue_content_based_deduplication" {
  description = "Whether SQS derives deduplication IDs from message bodies."
  type        = bool
  default     = false
}

variable "queue_tags" {
  description = "Tags for the FIFO queue."
  type        = map(string)
  default     = {}
}

variable "batch_size" {
  description = "Number of SQS messages per invocation, preserving the original setting by default."
  type        = number
  default     = 1

  validation {
    condition     = var.batch_size >= 1 && var.batch_size <= 10 && floor(var.batch_size) == var.batch_size
    error_message = "FIFO batch_size must be an integer from 1 through 10."
  }
}

variable "event_source_enabled" {
  description = "Whether the CDC event source mapping is enabled."
  type        = bool
  default     = true
}

variable "function_response_types" {
  description = "Optional partial batch failure support. Match the existing mapping when first adopting its state."
  type        = set(string)
  default     = []

  validation {
    condition     = alltrue([for value in var.function_response_types : value == "ReportBatchItemFailures"])
    error_message = "The only supported function response type is ReportBatchItemFailures."
  }
}
