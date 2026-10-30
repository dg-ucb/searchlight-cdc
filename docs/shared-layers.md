# Pinned shared dependency layers

Normal Lambda deployments can use existing developer-owned boto3 and psycopg2
layer versions. Keep the layer code and dependency requirements in this public
repository; keep the actual account identifiers, version ARNs and backend inputs
in the private infrastructure repository and Parameter Store.

## Configure an environment

Update the private pipeline `main.tf` from the matching handoff, then add this
map to the environment's private `config/<environment>.tfvars.json`, preserving
all its other settings:

```json
"shared_layer_version_arns": {
  "boto3": "arn:aws:lambda:REGION:ACCOUNT_ID:layer:boto3-python313:VERSION",
  "psycopg2": "arn:aws:lambda:REGION:ACCOUNT_ID:layer:psycopg2-python313:VERSION"
}
```

These are placeholders: use full version ARNs from the Lambda console, including
the trailing positive version number. Both keys are required. The versions must
be in this pipeline's account and Region and compatible with Python 3.13 and
x86_64. Do not set `dependency_layer_names` overrides at the same time.

The private pipeline derives its exact shared-layer read permissions and the
build's expected ARNs from this JSON. There is no second list to maintain in
`dev.tfvars` or `qa.tfvars`. Apply the private infrastructure to update Parameter
Store and permissions, then start a NEW pipeline execution and review its plan.
Keep each environment's own pipeline and application state keys.

The environment pipeline:

1. Tests the application and builds only its function ZIP.
2. Downloads the two exact layer ZIPs using authenticated Lambda API metadata.
3. Verifies version identities, AWS-reported SHA256 hashes and package limits.
4. Imports the actual downloaded layers with the function in the pinned Lambda
   runtime container, without network access or AWS credentials in the container.
5. Plans against the exact configured layer versions, waits for manual approval,
   and applies the saved plan and sealed artifacts.

Apply does not rebuild or download packages. The environment pipeline has no
permission to publish or delete shared layer versions. It retains read access
to its former function-specific layers so Terraform can refresh/migrate state.

Omitting the map or setting it to `{}` preserves the original behavior: build
and manage two function-specific layers. PR validation remains AWS-free and
builds dependency ZIPs from the hash-locked requirements files.

## Existing function-specific layer state

If the Lambda state already manages function-specific dependency layers,
switching to shared versions removes those two records from the environment's
state. The supplied layers have `skip_destroy = true`: the pinned AWS provider
retains their actual versions. The plan guard allows only this narrowly checked
retirement of the environment's own retained layer records; it still rejects
Lambda replacement, queue destruction and deletion of unrelated resources.

A Terraform plan can label these two retained records as deletions even though
the provider does not delete their AWS versions. Confirm their names are the
old function-specific layer names and `skip_destroy` is true. If older state
has `skip_destroy = false`, migrate it separately; the pipeline will stop.
Existing shared versions are data lookups and are never imported into every
environment's state. See the [pinned provider retention implementation](https://github.com/hashicorp/terraform-provider-aws/blob/v6.65.0/internal/service/lambda/layer_version.go#L271-L288).

## Publishing a future shared version

The separate `terraform/shared-layers/` root is for the single shared-layer
owner. It is NOT called by ordinary environment deployments. Its backend must
use its own state key, separate from Lambda and pipeline states. Do not point
that root at a Lambda application's existing state or a SAM/CloudFormation
stack's resources.

For an intentional dependency release, the owner updates the hash-locked files
under `requirements-layers/`, builds the packages using the existing helper,
then reviews and applies the publisher root with private inputs:

```bash
python scripts/package_lambda.py --smoke
terraform -chdir=terraform/shared-layers init -backend-config=/PRIVATE/PATH/layers.backend.hcl
terraform -chdir=terraform/shared-layers plan -var-file=/PRIVATE/PATH/layers.tfvars -out=/PRIVATE/PATH/layers.tfplan
terraform -chdir=terraform/shared-layers apply /PRIVATE/PATH/layers.tfplan
terraform -chdir=terraform/shared-layers output -json shared_layer_version_arns
```

The publisher creates new immutable versions and retains previous versions.
It does not take over the existing shared version numbers merely because their
layer names match. Ensure one publishing workflow owns future releases.

Promote the returned version ARNs by selecting them in private Dev inputs first,
then QA and production after testing. Publishing a version or changing a
requirements file does not automatically advance other environments' pinned
versions. No environment resolves "latest".

The migration to already-published shared versions does not require this
publisher root, a new layer pipeline, or application business-logic changes.
