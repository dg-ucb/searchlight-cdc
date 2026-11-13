"""CodeBuild entry point: check a PR, or plan/apply a trusted environment release."""

# ruff: noqa: CPY001, T201
import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

import package_lambda

TF_VERSION = '1.15.8'
TF_SHA256 = 'd25ce7b6902013ad905db3d2eab0be4cd905887fe88b81a6171b8d5503c31f3d'
WORK = Path('/tmp/searchlight-release')
CORE = {
    'aws_lambda_function.cdc_handler',
    'aws_iam_role.lambda_execution_role',
    'aws_iam_role_policy_attachment.lambda_execution_policy',
    'aws_iam_role_policy_attachment.lambda_sqs_policy_attachment',
    'aws_sqs_queue.cdc_events_q',
    'aws_lambda_event_source_mapping.sqs_mapping',
}
LAYERS = {f'aws_lambda_layer_version.dependency["{name}"]' for name in ('boto3', 'psycopg2')}
QUEUES = {'aws_sqs_queue.cdc_events_q', 'aws_sqs_queue.cdc_events_q[0]'}


def run(*args, cwd=None, capture=False):
    return subprocess.run(args, cwd=cwd, check=True, text=True, stdout=subprocess.PIPE if capture else None).stdout


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def install_terraform():
    """Verify the pinned Linux binary before every job; never execute an ambient binary."""
    target = Path(tempfile.mkdtemp(prefix='searchlight-terraform-'))
    archive = target / 'terraform.zip'
    url = f'https://releases.hashicorp.com/terraform/{TF_VERSION}/terraform_{TF_VERSION}_linux_amd64.zip'
    with urlopen(url, timeout=60) as response, archive.open('wb') as output:
        shutil.copyfileobj(response, output)
    require(digest(archive) == TF_SHA256, 'Terraform download checksum mismatch')
    with zipfile.ZipFile(archive) as bundle:
        (target / 'terraform').write_bytes(bundle.read('terraform'))
    (target / 'terraform').chmod(0o700)
    return str(target / 'terraform')


def approved_layer_bases():
    """Keep approved layer names stable while selecting versions at release time."""
    raw = os.environ.get('ALLOWED_SHARED_LAYER_ARNS')
    if raw is None:
        return None  # Compatibility with the earlier exact-version pipeline.
    bases = json.loads(raw)
    pattern = rf'arn:aws:lambda:{re.escape(os.environ["AWS_DEFAULT_REGION"])}:{os.environ["EXPECTED_ACCOUNT_ID"]}:layer:[A-Za-z0-9_-]+'
    require(
        isinstance(bases, dict)
        and (not bases or set(bases) == {'boto3', 'psycopg2'})
        and all(isinstance(arn, str) and re.fullmatch(pattern, arn) for arn in bases.values()),
        'The private pipeline must approve both shared layer names in this account and Region',
    )
    return bases


def validate_layer_pins(layers):
    require(isinstance(layers, dict), 'Shared layer version ARNs must be a JSON object')
    pattern = rf'arn:aws:lambda:{re.escape(os.environ["AWS_DEFAULT_REGION"])}:{os.environ["EXPECTED_ACCOUNT_ID"]}:layer:[A-Za-z0-9_-]+:[1-9][0-9]*'
    require(
        not layers or (set(layers) == {'boto3', 'psycopg2'} and all(isinstance(arn, str) and re.fullmatch(pattern, arn) for arn in layers.values())),
        'Pin both shared layers to numbered version ARNs in this pipeline account and Region',
    )
    bases = approved_layer_bases()
    approved = (
        layers == json.loads(os.environ.get('EXPECTED_SHARED_LAYER_VERSION_ARNS', '{}'))
        if bases is None
        else {name: arn.rsplit(':', 1)[0] for name, arn in layers.items()} == bases
    )
    require(approved, 'Shared layer versions do not match the private pipeline permissions')


def layer_overrides():
    return {name: os.environ.get(f'{name.upper()}_LAYER_VERSION', 'configured') for name in ('boto3', 'psycopg2')}


def latest_layer_version(base):
    result = json.loads(run('aws', 'lambda', 'list-layer-versions', '--layer-name', base, '--output', 'json', capture=True))
    versions = result.get('LayerVersions') if isinstance(result, dict) else None
    require(isinstance(versions, list) and versions, 'No published version is available for the approved shared layer')
    require(
        all(
            isinstance(item, dict)
            and type(item.get('Version')) is int
            and item['Version'] > 0
            and item.get('LayerVersionArn') == f'{base}:{item["Version"]}'
            for item in versions
        ),
        'AWS returned an invalid version for the approved shared layer',
    )
    return str(max(item['Version'] for item in versions))


def resolve_layer_versions(tfvars):
    """Resolve defaults/one-run overrides once, before building or planning."""
    bases = approved_layer_bases()
    overrides = layer_overrides()
    require(
        all(isinstance(value, str) and re.fullmatch(r'configured|latest|[1-9][0-9]*', value) for value in overrides.values()),
        'Layer version overrides must be configured, latest, or a positive version number',
    )
    if not bases:
        require(all(value == 'configured' for value in overrides.values()), 'Runtime layer selection requires the updated shared-layer pipeline')
        return tfvars
    defaults = json.loads(os.environ.pop('LAYER_VERSIONS_JSON'))
    require(isinstance(defaults, dict) and set(defaults) == {'boto3', 'psycopg2'}, 'Layer version defaults must select both dependencies')
    defaults = {name: str(value) if type(value) is int else value for name, value in defaults.items()}
    require(
        all(isinstance(value, str) and re.fullmatch(r'latest|[1-9][0-9]*', value) for value in defaults.values()),
        'Layer version defaults must select both dependencies using positive numbers or latest',
    )
    resolved = {}
    for name, base in bases.items():
        selected = defaults[name] if overrides[name] == 'configured' else overrides[name]
        resolved[name] = f'{base}:{latest_layer_version(base) if selected == "latest" else selected}'
    validate_layer_pins(resolved)
    return tfvars | {'shared_layer_version_arns': resolved}


def inputs():
    """Validate SSM documents against private pipeline settings without logging their contents."""
    tfvars = json.loads(os.environ.pop('TFVARS_JSON'))
    backend = json.loads(os.environ.pop('BACKEND_JSON'))
    require(isinstance(tfvars, dict) and isinstance(backend, dict), 'SSM inputs must be JSON objects')
    require(os.environ['ENVIRONMENT'] in ('dev', 'qa', 'prod'), 'Unsupported environment')
    expected = {
        'allowed_account_id': os.environ['EXPECTED_ACCOUNT_ID'],
        'region': os.environ['AWS_DEFAULT_REGION'],
        'function_name': os.environ['EXPECTED_FUNCTION_NAME'],
        'execution_role_name': os.environ['EXPECTED_EXECUTION_ROLE_NAME'],
        'queue_name': os.environ['EXPECTED_QUEUE_NAME'],
    }
    require(
        all(tfvars.get(key) == value for key, value in expected.items()),
        "Lambda inputs do not match this pipeline's account, region, function, role, or queue",
    )
    queue_mode = tfvars.get('manage_queue', True)
    expected_queue_mode = os.environ.get('EXPECTED_MANAGE_QUEUE', 'true')
    require(
        type(queue_mode) is bool and expected_queue_mode in ('true', 'false') and queue_mode == (expected_queue_mode == 'true'),
        'Queue ownership mode does not match the private pipeline permissions',
    )
    require(tfvars.get('artifacts_directory') is None, 'Artifact path overrides are prohibited')
    shared_layers = tfvars.get('shared_layer_version_arns', {})
    validate_layer_pins(shared_layers)
    names = {name: f'{expected["function_name"]}-{name}' for name in ('boto3', 'psycopg2')}
    layer_names = tfvars.get('dependency_layer_names', {})
    require(
        isinstance(layer_names, dict)
        and (not shared_layers or not layer_names)
        and all(key in names and names[key] == value for key, value in layer_names.items()),
        'Layer names must match the scoped pipeline permissions',
    )
    require(
        sorted(tfvars.get('additional_execution_policy_arns', [])) == sorted(json.loads(os.environ['EXPECTED_ADDITIONAL_EXECUTION_POLICY_ARNS'])),
        'Runtime policy inputs do not match the private pipeline permissions',
    )
    require(
        tfvars.get('runtime', 'python3.13') == 'python3.13' and tfvars.get('architectures', ['x86_64']) == ['x86_64'],
        'Unsupported runtime or architecture',
    )
    require(
        str(tfvars.get('environment_variables', {}).get('LOCAL_DEV', 'false')).lower() != 'true',
        'LOCAL_DEV must not be enabled in a deployed Lambda',
    )
    require(set(backend) <= {'bucket', 'key', 'region', 'dynamodb_table', 'kms_key_id', 'encrypt', 'use_lockfile'}, 'Unsupported backend options')
    require(
        backend.get('bucket') == os.environ['STATE_BUCKET']
        and backend.get('key') == os.environ['STATE_KEY']
        and backend.get('region') == expected['region']
        and backend.get('dynamodb_table', '') == os.environ['DYNAMODB_TABLE']
        and backend.get('kms_key_id', '') == os.environ['STATE_KMS_KEY_ID']
        and backend.get('encrypt') is True
        and backend.get('use_lockfile') is True,
        "Backend inputs do not match this pipeline's encrypted, locked state target",
    )
    return tfvars, backend


def configuration(tfvars, backend):
    return {
        'tfvars': hashlib.sha256(canonical(tfvars)).hexdigest(),
        'backend': hashlib.sha256(canonical(backend)).hexdigest(),
        'source_sha': os.environ['SOURCE_SHA'],
        'execution_id': os.environ['EXECUTION_ID'],
        'environment': os.environ['ENVIRONMENT'],
        'shared_layer_version_arns': tfvars.get('shared_layer_version_arns', {}),
        'allowed_shared_layer_arns': approved_layer_bases(),
        'layer_overrides': layer_overrides(),
    }


def guard_plan(plan, tfvars):
    """Allow updates and additions; require a separate migration for destructive changes."""
    allowed = (
        (CORE - QUEUES)
        | (QUEUES if tfvars.get('manage_queue', True) else set())
        | LAYERS
        | {
            'aws_iam_role_policy_attachment.lambda_vpc_policy_attachment',
            'aws_iam_role_policy.database_secret[0]',
        }
        | {
            f'aws_iam_role_policy_attachment.additional_execution_policy[{json.dumps(arn)}]'
            for arn in tfvars.get('additional_execution_policy_arns', [])
        }
    )
    require(not plan.get('errored') and plan.get('complete', True), 'Terraform plan is incomplete')
    shared_layers = tfvars.get('shared_layer_version_arns', {})
    for resource in plan.get('resource_changes', []):
        if resource.get('mode') == 'data':
            continue
        address, change = resource['address'], resource['change']
        require(address in allowed, f'Unexpected managed resource in plan: {address}')
        if shared_layers and address == 'aws_lambda_function.cdc_handler':
            unknown_layers = (change.get('after_unknown') or {}).get('layers', False)
            # Terraform can represent a fully known list with per-element false flags.
            known_layers = unknown_layers is False or (
                isinstance(unknown_layers, list) and len(unknown_layers) == len(shared_layers) and all(unknown is False for unknown in unknown_layers)
            )
            require(
                (change.get('after') or {}).get('layers') == [shared_layers[name] for name in ('boto3', 'psycopg2')] and known_layers,
                'The Lambda plan must attach the exact approved shared layer versions',
            )
        if shared_layers and address in LAYERS and change['actions'] != ['no-op']:
            dependency = json.loads(address.split('[', 1)[1][:-1])
            before = change.get('before') or {}
            layer_name = f'{tfvars["function_name"]}-{dependency}'
            arn = rf'arn:aws:lambda:{re.escape(tfvars["region"])}:{tfvars["allowed_account_id"]}:layer:{re.escape(layer_name)}:[1-9][0-9]*'
            require(
                change['actions'] == ['delete']
                and change.get('after') is None
                and before.get('skip_destroy') is True
                and before.get('layer_name') == layer_name
                and isinstance(before.get('arn'), str)
                and re.fullmatch(arn, before['arn']),
                'Shared-layer deployments may only retire retained function-specific layer records',
            )
            # The AWS provider honors skip_destroy by removing only the state
            # record. The physical immutable version is kept for other consumers.
            continue
        if change['actions'] not in (['no-op'], ['create'], ['update']):
            retained_layer = (
                address in LAYERS
                and change['actions'] == ['create', 'delete']
                and (change.get('before') or {}).get('skip_destroy') is True
                and (change.get('after') or {}).get('skip_destroy') is True
            )
            require(retained_layer, f'Destruction/replacement requires a separate migration: {address}')


def payload_files(root):
    paths = [
        *sorted((root / 'terraform').glob('*.tf')),
        root / 'terraform/.terraform.lock.hcl',
        root / 'scripts/deploy.py',
        root / 'scripts/package_lambda.py',
        *(root / f'dist/release/{name}.zip' for name in package_lambda.COMPONENTS),
        *(root / f'dist/plan/{name}' for name in ('approved.tfplan', 'plan.txt', 'configuration.sha256')),
    ]
    require(all(path.is_file() and not path.is_symlink() for path in paths), 'Release payload is incomplete')
    return paths


def seal(root):
    manifest = root / 'dist/plan/checksums.sha256'
    manifest.write_text(''.join(f'{digest(path)}  {path.relative_to(root).as_posix()}\n' for path in payload_files(root)))
    return digest(manifest)


def verify(root):
    manifest = root / 'dist/plan/checksums.sha256'
    require(digest(manifest) == os.environ['APPROVED_CHECKSUM_SHA256'], 'Approved manifest checksum mismatch')
    expected = {str(path.relative_to(root)): digest(path) for path in payload_files(root)}
    actual = {}
    for line in manifest.read_text().splitlines():
        checksum, path = line.split('  ', 1)
        require(path not in actual, 'Duplicate manifest entry')
        actual[path] = checksum
    require(actual == expected, 'Approved release payload has changed')


def tests(root):
    run(sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', '-r', 'requirements.txt', cwd=root)
    (root / 'dist').mkdir(exist_ok=True)
    run(sys.executable, '-m', 'pytest', '-q', 'test/unit', 'test/deployment', '--junitxml=dist/unit-results.xml', cwd=root)


def check(root):
    tests(root)
    package_lambda.build(root)
    package_lambda.smoke(root, os.environ.get('LAMBDA_RUNTIME_IMAGE', package_lambda.RUNTIME_IMAGE))
    tf = install_terraform()
    run(tf, '-chdir=terraform', 'fmt', '-check', '-recursive', cwd=root)
    for directory in ('terraform', 'terraform/shared-layers'):
        run(tf, f'-chdir={directory}', 'init', '-backend=false', '-input=false', '-lockfile=readonly', cwd=root)
        run(tf, f'-chdir={directory}', 'validate', '-no-color', cwd=root)


def deploy(mode, source):
    require(re.fullmatch(r'[0-9a-f]{40}', os.environ['SOURCE_SHA']), 'Invalid source revision')
    if mode == 'plan':
        require(os.environ['CODEBUILD_RESOLVED_SOURCE_VERSION'] == os.environ['SOURCE_SHA'], 'Source revision mismatch')
    account = run('aws', 'sts', 'get-caller-identity', '--query', 'Account', '--output', 'text', capture=True).strip()
    require(account == os.environ['EXPECTED_ACCOUNT_ID'], 'Wrong AWS account')
    require(not any((source / 'terraform').rglob('*.tfvars*')), 'Do not commit Terraform environment inputs')
    require(not any((source / 'terraform').rglob('*.tf.json')), 'Use sealed .tf files rather than .tf.json configuration')
    if mode == 'apply':
        verify(source)
    tfvars, backend = inputs()
    if mode == 'apply':
        saved = json.loads((source / 'dist/plan/configuration.sha256').read_text())
        approved_layers = saved.get('shared_layer_version_arns', {})
        validate_layer_pins(approved_layers)
        if approved_layers or 'shared_layer_version_arns' in tfvars:
            tfvars = tfvars | {'shared_layer_version_arns': approved_layers}
        require(
            saved == configuration(tfvars, backend),
            'Environment inputs or release identity changed after approval; create a new plan',
        )
    else:
        tfvars = resolve_layer_versions(tfvars)
    require(not WORK.exists(), f'Use a fresh build container; release directory already exists: {WORK}')
    shutil.copytree(source, WORK, ignore=shutil.ignore_patterns('.git', '.terraform', '__pycache__', '.env*'))
    with tempfile.TemporaryDirectory(prefix='searchlight-config-') as config:
        config = Path(config)
        for name, value in (('tfvars', tfvars), ('backend', backend)):
            (config / f'{name}.json').write_bytes(canonical(value))
        tf = install_terraform()
        tfcmd = (tf, '-chdir=terraform')
        run(*tfcmd, 'init', '-input=false', '-lockfile=readonly', f'-backend-config={config}/backend.json', cwd=WORK)
        if mode == 'apply':
            run(*tfcmd, 'apply', '-input=false', '-lock-timeout=5m', '../dist/plan/approved.tfplan', cwd=WORK)
            print('Applied the approved Terraform plan and Lambda packages.')
            return
        if os.environ.get('REQUIRE_EXISTING_STATE', 'true') == 'true':
            state = set(run(*tfcmd, 'state', 'list', cwd=WORK, capture=True).splitlines())
            require(
                state >= CORE - QUEUES and (not tfvars.get('manage_queue', True) or bool(state & QUEUES)),
                'Existing Lambda state is missing expected resources; verify the backend or import once',
            )
        tests(WORK)
        package_lambda.build(WORK, tfvars.get('shared_layer_version_arns', {}))
        package_lambda.smoke(WORK, os.environ.get('LAMBDA_RUNTIME_IMAGE', package_lambda.RUNTIME_IMAGE))
        run(*tfcmd, 'validate', '-no-color', cwd=WORK)
        plan_dir = WORK / 'dist/plan'
        plan_dir.mkdir(parents=True, exist_ok=True)
        run(
            *tfcmd,
            'plan',
            '-no-color',
            '-input=false',
            '-lock-timeout=5m',
            f'-var-file={config}/tfvars.json',
            '-out=../dist/plan/approved.tfplan',
            cwd=WORK,
        )
        guard_plan(json.loads(run(*tfcmd, 'show', '-json', '../dist/plan/approved.tfplan', cwd=WORK, capture=True)), tfvars)
        text = run(*tfcmd, 'show', '-no-color', '../dist/plan/approved.tfplan', cwd=WORK, capture=True)
        (plan_dir / 'plan.txt').write_text(text)
        (plan_dir / 'configuration.sha256').write_bytes(canonical(configuration(tfvars, backend)))
        checksum = seal(WORK)
        key = f'reviews/{os.environ["EXECUTION_ID"]}/plan.txt'
        run(
            'aws',
            's3',
            'cp',
            str(plan_dir / 'plan.txt'),
            f's3://{os.environ["ARTIFACT_BUCKET"]}/{key}',
            '--sse',
            'aws:kms',
            '--sse-kms-key-id',
            os.environ['ARTIFACT_KMS_KEY'],
            '--only-show-errors',
        )
        summary = next((line for line in reversed(text.splitlines()) if line.startswith(('Plan:', 'No changes.'))), 'Review the saved Terraform plan')
        review_url = (
            f'https://s3.console.aws.amazon.com/s3/object/{os.environ["ARTIFACT_BUCKET"]}'
            f'?region={os.environ["AWS_DEFAULT_REGION"]}&prefix={quote(key, safe="/")}'
        )
        exports = {'PLAN_CHECKSUM_SHA256': checksum, 'PLAN_SUMMARY': summary, 'PLAN_REVIEW_URL': review_url}
        (plan_dir / 'exports.sh').write_text(''.join(f'export {key}={shlex.quote(value)}\n' for key, value in exports.items()))
        shutil.copytree(WORK / 'dist/release', source / 'dist/release', dirs_exist_ok=True)
        shutil.copytree(plan_dir, source / 'dist/plan', dirs_exist_ok=True)
        shutil.copy2(WORK / 'dist/unit-results.xml', source / 'dist/unit-results.xml')
        print(summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('check', 'plan', 'apply'))
    args = parser.parse_args()
    os.umask(0o077)
    for name in list(os.environ):
        if name.startswith(('TF_LOG', 'TF_CLI_ARGS', 'TF_VAR_')) or name == 'TF_WORKSPACE':
            del os.environ[name]
    os.environ.update(
        TF_IN_AUTOMATION='true',
        CHECKPOINT_DISABLE='1',
        AWS_PAGER='',
        AWS_EC2_METADATA_DISABLED='true',
        LOCAL_DEV='true',
        PYTHONDONTWRITEBYTECODE='1',
    )
    root = Path(__file__).resolve().parents[1]
    try:
        check(root) if args.command == 'check' else deploy(args.command, root)
    except (ValueError, KeyError, json.JSONDecodeError, zipfile.BadZipFile, subprocess.CalledProcessError) as error:
        parser.exit(1, f'Deployment stopped: {error}\n')


if __name__ == '__main__':
    main()
