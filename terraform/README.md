# Application Terraform

This directory contains the Lambda Terraform supplied for the existing deployment, with its environment-specific values replaced by variables. It manages the function, execution role, original policy attachments, FIFO queue, and SQS event source mapping. Dependencies can use pinned shared layer versions or two function-specific layers. It does not create databases, VPCs, subnets, security groups, pipelines, or a state backend.

The pipeline builds `dist/release/function.zip` and either downloads the exact shared `boto3.zip` and `psycopg2.zip` versions or builds the function-specific layers, then runs Terraform from this directory. Code hashes and selected layer versions are managed by Terraform; there is no code-change ignore rule and no SAM deployment.

## Release process

The private infrastructure repository owns CodePipeline, CodeBuild, their short buildspecs, IAM permissions, the artifact bucket, and actual environment values. It publishes the Lambda inputs and backend configuration to Parameter Store. This public repository contains no pipeline infrastructure or environment values.

Only two Python helpers are needed:

- `scripts/package_lambda.py` builds the function ZIP and prepares dependency ZIPs from exact shared layer versions or hash-locked Linux requirements, then checks those exact ZIPs in the pinned Lambda runtime container.
- `scripts/deploy.py check` runs PR tests, packaging, runtime imports and Terraform validation without AWS credentials. `plan` reads the CodeBuild-injected SSM inputs, tests, packages, initializes remote state and saves a Terraform plan. `apply` verifies the approved release and applies that saved plan.

Pull requests use `.github/workflows/validate-lambda.yml` with read-only GitHub access and no AWS credentials. After merge to the configured environment branch, CodePipeline runs Plan → manual approval → Apply. Apply neither rebuilds packages nor creates a new plan. A checksum passed independently through CodePipeline binds the Terraform files, provider lock, both helpers, three ZIPs, readable and binary plans, and configuration fingerprints. The private apply buildspec verifies these files before executing the public helper. Changed Lambda/backend configuration requires a new plan and approval. Operational layer-version defaults are resolved in Plan and frozen in the approved artifacts; later default edits affect only new runs.

For local PR checks on Linux with Python 3.13 and Docker, run `python scripts/deploy.py check`. It downloads the checksum-verified Terraform binary and locked provider; it does not connect to an AWS account or initialize a remote backend. Build only the packages on other development platforms with `python scripts/package_lambda.py --smoke`.

## Environment configuration

The pipeline reads its JSON tfvars document from `/searchlight/<environment>/terraform/tfvars` and its S3 backend configuration from `/searchlight/<environment>/terraform/backend`, for dev, qa, or production. Actual values are not checked into this public repository. Use a SecureString parameter for the tfvars document. The pipeline writes decrypted input files only into its ephemeral build workspace and does not print them or include them in artifacts.

Required tfvars keys are `region`, `allowed_account_id`, `function_name`, `execution_role_name`, `queue_name`, `subnet_ids`, `security_group_ids`, and `environment_variables`. Set optional settings and tags to the deployed environment's current values before the first plan, so adopting this directory does not reset them to defaults. The default memory (128 MiB), timeout (3 seconds), queue settings, and batch size preserve the supplied Terraform's defaults.

Set `function_response_types` to `["ReportBatchItemFailures"]`: the handler returns individual failed SQS records for retry. If the existing mapping does not have this enabled, review its addition in the first plan. Leave `LOCAL_DEV` unset in deployed environment variables.

Use `DB_SECRET_NAME` to reference an existing Secrets Manager secret. The application retrieves database credentials at runtime. Do not put a database password in a tfvars document or Lambda environment variables. The `environment_variables` input is marked sensitive, which hides its contents from normal Terraform output; Terraform still records configuration in remote state. Restrict and encrypt the state bucket and saved plan artifacts accordingly.

The two original AWS-managed basic/SQS policy attachments are preserved. A separate VPC policy attachment supplies the network interface permissions required by this function. Supply `database_secret_arn` to grant runtime `GetSecretValue` access to that exact secret; supply `database_secret_kms_key_arn` as well if it uses a customer-managed KMS key. Terraform never retrieves the secret value. Leave these optional inputs null if another infrastructure component manages secret access. `additional_execution_policy_arns` can attach existing managed policies for other application services. Existing policies managed elsewhere remain attached.

## Existing state and first deployment

Use the **same remote state key** that currently manages this Lambda deployment, and retire its old Terraform working directory before enabling the pipeline. Never let two Terraform roots own the same resources. The original resource addresses remain unchanged:

- `aws_lambda_function.cdc_handler`
- `aws_iam_role.lambda_execution_role`
- `aws_iam_role_policy_attachment.lambda_execution_policy`
- `aws_iam_role_policy_attachment.lambda_sqs_policy_attachment`
- `aws_sqs_queue.cdc_events_q`
- `aws_lambda_event_source_mapping.sqs_mapping`

The original `data.aws_iam_policy_document.assume_role` and `data.aws_iam_policy_document.sqs_policy` addresses are also retained. The former archive data source is unnecessary because CodeBuild prepares the ZIPs before planning. Account-specific import blocks and the local AWS profile were removed.

If any existing resource is missing from that state, import it once through your infrastructure administration process before enabling the pipeline. Do not run an empty-state plan against an already deployed Lambda. A normal release should update code and layer versions while retaining the existing role, queue, and mapping. Inspect the first approved plan for unintended replacement, deletion, tag removal, or environment changes.

The pipeline requires all six original managed addresses in existing state by default and rejects destructive changes, unexpected resources and state-only removal. A narrowly checked exception retires this function's own retained layer records when switching to pinned shared layers; their physical versions remain. The private infrastructure can disable the existing-state requirement for an intentional new installation. Migration from an older experimental deployment with hash-keyed layer resources needs a separately reviewed state migration; do not assume those layer addresses match this root.

Function-specific layers use `aws_lambda_layer_version.dependency["boto3"]` and `["psycopg2"]`. Each changed ZIP creates an immutable version, then Terraform attaches it to the Lambda. Previous versions have `skip_destroy = true` so a deployment cannot remove versions still used by another function or needed for rollback. Cleanup of old versions is a separate maintenance task. This retention also applies when destroying this Terraform root. In shared mode, this root only reads the exact versions resolved by Plan and never publishes or deletes them. Version defaults and one-run overrides are deployment inputs, so routine version changes do not require reapplying pipeline infrastructure.

Terraform is pinned to 1.15.8 and the AWS provider to 6.65.0. CodeBuild uses its own scoped IAM credentials; this directory has no AWS profile or credentials.


For developer-owned, version-pinned shared dependency layers, see [shared layer deployment](../docs/shared-layers.md).
