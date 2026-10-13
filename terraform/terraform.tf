terraform {
  required_version = "= 1.15.8"

  required_providers {
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
