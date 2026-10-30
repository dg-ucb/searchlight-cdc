# Developer-owned layer publisher. Use one separate remote state for this root;
# environment Lambda deployments consume the resulting numbered version ARNs.
terraform {
  required_version = "= 1.15.8"
  required_providers {
    aws = { source = "hashicorp/aws", version = "= 6.65.0" }
  }
  backend "s3" {}
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.allowed_account_id]
}

variable "region" { type = string }
variable "allowed_account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.allowed_account_id))
    error_message = "Supply the twelve-digit account ID that owns these shared layers."
  }
}
variable "layer_names" {
  type = map(string)
  default = {
    boto3    = "boto3-python313"
    psycopg2 = "psycopg2-python313"
  }
  nullable = false
  validation {
    condition = (
      toset(keys(var.layer_names)) == toset(["boto3", "psycopg2"]) &&
      length(distinct(values(var.layer_names))) == 2 &&
      alltrue([for name in values(var.layer_names) : can(regex("^[A-Za-z0-9_-]{1,140}$", name))])
    )
    error_message = "Provide distinct valid boto3 and psycopg2 layer names."
  }
}
variable "artifacts_directory" {
  description = "Directory with the two dependency ZIPs built by scripts/package_lambda.py. Null selects dist/release in this repository."
  type        = string
  default     = null
}

locals {
  artifacts_directory = coalesce(var.artifacts_directory, "${path.module}/../../dist/release")
}

resource "aws_lambda_layer_version" "dependency" {
  for_each = var.layer_names

  filename                 = "${local.artifacts_directory}/${each.key}.zip"
  source_code_hash         = filebase64sha256("${local.artifacts_directory}/${each.key}.zip")
  layer_name               = each.value
  description              = "Shared Searchlight ${each.key} dependencies"
  compatible_runtimes      = ["python3.13"]
  compatible_architectures = ["x86_64"]
  skip_destroy             = true

  lifecycle { create_before_destroy = true }
}

output "shared_layer_version_arns" {
  description = "Pinned layer versions to select privately for Dev, QA and production after review. Publishing does not automatically change those consumers."
  value       = { for dependency, layer in aws_lambda_layer_version.dependency : dependency => layer.arn }
}
