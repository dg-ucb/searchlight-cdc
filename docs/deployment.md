# Deployment

CodePipeline deploys the Lambda with Terraform and either pins existing shared dependency layers or manages function-specific layers. The pipeline infrastructure and actual environment inputs live in a separate private infrastructure repository. This application repository contains the handler, `terraform/`, two Python deployment helpers, locked layer dependencies, and PR validation.

```text
Developer PR → tests and packaging checks → merge to environment branch
  → CodePipeline → test + package + Terraform plan → approval → Terraform apply
```

PR checks run without AWS credentials. The trusted environment branch starts the deployment pipeline. Dev, QA and production each use their own pipeline, Parameter Store paths, existing Lambda resources and state key.

## Application release

1. Change `lambda/` and submit a PR. The GitHub check runs unit and deployment-helper tests, builds Linux-compatible ZIPs, checks imports in the Lambda runtime container, and validates Terraform without a backend.
2. Merge into the branch configured for the environment. CodePipeline fetches that commit through CodeConnections. The Plan CodeBuild project receives private inputs through native Parameter Store environment variables.
3. `scripts/deploy.py plan` tests the application, builds `function.zip` and prepares the two dependency ZIPs, checks imports, then runs Terraform init, validate, plan and show. Shared mode downloads and verifies the pinned layer versions; dedicated mode builds the dependency ZIPs from locked requirements.
4. Review the linked Terraform plan in the private artifact bucket and approve it in CodePipeline.
5. The Apply project verifies the approved artifacts and current configuration, then runs `scripts/deploy.py apply`. It applies the saved plan with the already-built ZIPs. It does not rebuild or replan.

Changing Lambda/backend inputs after planning requires a fresh plan and approval. Operational layer-version defaults are resolved only in Plan; edits after planning affect subsequent runs while the approved concrete layer pins remain unchanged. Destruction or replacement of the existing function, runtime role, queue or mapping requires a separate reviewed migration. Dedicated dependency layers publish new immutable versions and retain the previous versions. Shared consumers use only the exact configured versions and cannot publish or delete shared layers.

## Configuration and existing resources

Infrastructure administrators publish two SecureString documents per environment:

```text
/searchlight/<environment>/terraform/tfvars
/searchlight/<environment>/terraform/backend
```

The private infrastructure Terraform owns these parameters. Developers do not add tfvars, backend values, credentials, account details or secrets to this repository. The pipeline writes temporary input files outside the source artifact and removes them when the helper exits. Saved Terraform plans still contain sensitive inputs and must remain private and encrypted.

Reuse the existing Lambda Terraform state and import any missing existing resources once before enabling releases. Retire the previous root managing those same resources. This Terraform manages the Lambda, selected layer references, execution role/policies, FIFO queue and SQS mapping; it uses existing networking, database and Secrets Manager resources. SQL migrations are a separate operation.

Set `DB_NAME` and `DB_SECRET_NAME` in the private environment configuration. Passwords remain in Secrets Manager. Enable `function_response_types = ["ReportBatchItemFailures"]` so failed CDC messages are retried, and leave deployed `LOCAL_DEV` unset.

See [the application Terraform guide](../terraform/README.md) for inputs, existing resource addresses and state migration. The private pipeline README describes the infrastructure setup and its separate state.

## Local checks

With Python 3.13 and Docker on Linux:

```sh
python scripts/deploy.py check
```

To build and smoke-test the ZIPs from another development platform:

```sh
python scripts/package_lambda.py --smoke
```

Layer dependencies are pinned with hashes under `requirements-layers/`. Update those locks through a reviewed PR when upgrading dependencies. The established local development tools can still be used locally; the deployment pipeline does not invoke SAM or CloudFormation.


For developer-owned, version-pinned shared dependency layers, see [shared layer deployment](shared-layers.md).
