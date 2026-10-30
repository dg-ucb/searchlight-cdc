# Shared layers with runtime version inputs

The public Lambda Terraform accepts exact layer-version ARNs. CodePipeline's
Plan build resolves operational version choices into those ARNs. Layer version
changes do not require editing Terraform or redeploying pipeline infrastructure.
The Lambda still deploys through its normal plan, manual approval and apply.

## One-time setup

Install the matching public overlay and private pipeline `main.tf`. In the
private environment `config/<environment>.tfvars.json`, preserve the existing
settings and supply the approved shared layers and initial version defaults:

```json
"shared_layer_version_arns": {
  "boto3": "arn:aws:lambda:REGION:ACCOUNT_ID:layer:boto3-python313:2",
  "psycopg2": "arn:aws:lambda:REGION:ACCOUNT_ID:layer:psycopg2-python313:2"
}
```

Use real ARNs privately. Both keys are required in this account/Region, and
`dependency_layer_names` overrides must be empty. This map establishes approved
layer names and seeds defaults on first installation. After installation, do
not edit its version suffixes to select releases; use the operational parameter
below. All account, backend, networking and secret inputs stay private.

Apply the private pipeline infrastructure once with the correct environment's
backend and tfvars. It creates the environment's SecureString parameter at
`<ssm-prefix>/<environment>/terraform/layer-versions`, initially:

```json
{"boto3":"2","psycopg2":"2"}
```

Terraform owns that parameter's name, encryption and lifecycle. Operators own
its value after creation: `ignore_changes = [value]` prevents later pipeline
Terraform applies from resetting release choices to the bootstrap values.
Get the exact parameter name from `terraform output -raw layer_versions_parameter_name`.

## Persistent version changes

Keep a private release-selection file, for example
`config/dev.layer-versions.json`, with the desired defaults:

```json
{"boto3":"3","psycopg2":"2"}
```

Update the operational Parameter Store value, then start the pipeline or let
the next source-code push start it:

```bash
aws ssm put-parameter \
  --name /searchlight/dev/terraform/layer-versions \
  --type SecureString --overwrite \
  --value file://config/dev.layer-versions.json \
  --region YOUR_REGION --profile YOUR_WORK_PROFILE

aws codepipeline start-pipeline-execution \
  --name YOUR_DEV_PIPELINE \
  --region YOUR_REGION --profile YOUR_WORK_PROFILE
```

Use the output parameter name if your prefix differs. If the private pipeline
uses a customer-managed Parameter Store key, also pass its matching `--key-id`
to `put-parameter`; do not change the encryption key during a version update.
Version values can be positive integers/digit strings or `latest`. Keep explicit
numbers when you want reproducible defaults. No Terraform commands are needed
for these routine version changes.

## One-execution overrides

The CodePipeline variables `Boto3LayerVersion` and `Psycopg2LayerVersion` default
to `configured`, meaning the Parameter Store defaults loaded by the Plan build.
When starting a manual execution, either input can instead be a positive version
number or `latest`:

```bash
aws codepipeline start-pipeline-execution \
  --name YOUR_DEV_PIPELINE \
  --variables name=Boto3LayerVersion,value=3 name=Psycopg2LayerVersion,value=2 \
  --region YOUR_REGION --profile YOUR_WORK_PROFILE
```

These overrides affect only that execution. They do not persist as defaults for
future automatic deployments. Update Parameter Store to make a selection stick.
New layer versions must already be published; this workflow consumes them.

`latest` means the highest currently published version for the approved layer
name. The Plan build resolves it once, then verifies compatibility and package
contents. An incompatible latest version fails; the build does not silently
fall back to an older version. A new publication during approval cannot change
the version in the approved plan.

## Approval and permissions

The existing helpers download and hash-verify the exact resolved layer ZIPs,
smoke-import them with the function in the pinned Python 3.13/x86_64 runtime,
and save a Terraform plan. The existing configuration artifact records the
resolved pins, approved names and one-run overrides, and is checksum-bound to
the code, packages, source revision and execution.

Apply verifies the seal first and uses the approved concrete pins. It never
reads mutable version defaults, resolves latest, downloads/rebuilds packages or
replans. Later default-version edits affect new Plan builds, not an already
approved release. Unrelated Lambda/backend inputs, allowed layer names, source,
execution or approval artifacts still cannot change after planning.

Layer ZIP reads are scoped to versions of the two approved names. Only Plan can
read the operational defaults or call `ListLayerVersions`. AWS requires a
wildcard resource for that metadata-list operation, so it is restricted to the
pipeline Region. Shared-mode environment jobs cannot publish or delete layers.
See the [AWS IAM action reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_lambda.html).

## Migration and future publishing

Keep the existing Lambda/application state and each environment's separate
pipeline state. Old function-specific layer records may be retired only when
`skip_destroy=true` and names/ARNs match this function; the pinned provider
retains their physical versions. The guard still rejects Lambda replacement,
queue destruction and unrelated removals. Existing shared versions are readonly
lookups and are never owned by every environment state.

The optional `terraform/shared-layers/` root remains a separate owner-controlled
publisher for future dependency releases. It is not run by normal environment
pipelines. Keep its backend inputs private and its state key separate. To build
and publish intentionally:

```bash
python scripts/package_lambda.py --smoke
terraform -chdir=terraform/shared-layers init -backend-config=/PRIVATE/PATH/layers.backend.hcl
terraform -chdir=terraform/shared-layers plan -var-file=/PRIVATE/PATH/layers.tfvars -out=/PRIVATE/PATH/layers.tfplan
terraform -chdir=terraform/shared-layers apply /PRIVATE/PATH/layers.tfplan
terraform -chdir=terraform/shared-layers output -json shared_layer_version_arns
```

Omitting `shared_layer_version_arns` keeps the original dedicated-layer mode.
PR checks remain AWS-free. No Lambda business-logic changes are required.
