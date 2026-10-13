terraform {
  required_version = "= 1.15.8"

  required_providers {
    # Existing Lambda state can still reference the former ZIP data source.
    archive = {
      source  = "hashicorp/archive"
      version = "= 2.8.0"
    }

    aws = {
      source  = "hashicorp/aws"
      version = "= 6.65.0"
    }
  }

  # The buildspec supplies the existing environment's backend configuration
  # from Parameter Store. No account, bucket, key, or credentials belong here.
  backend "s3" {}
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.allowed_account_id]
}
