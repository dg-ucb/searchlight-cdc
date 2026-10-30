"""Approval must bind the package, Terraform, source revision and private inputs."""

# ruff: noqa: CPY001, PT009, PT027
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import deploy

ENV = {
    'ENVIRONMENT': 'dev',
    'EXPECTED_ACCOUNT_ID': '123456789012',
    'AWS_DEFAULT_REGION': 'us-east-1',
    'EXPECTED_FUNCTION_NAME': 'example-dev',
    'EXPECTED_EXECUTION_ROLE_NAME': 'example-dev-runtime',
    'EXPECTED_QUEUE_NAME': 'example-dev.fifo',
    'EXPECTED_ADDITIONAL_EXECUTION_POLICY_ARNS': '[]',
    'STATE_BUCKET': 'example-private-state',
    'STATE_KEY': 'lambda/dev.tfstate',
    'DYNAMODB_TABLE': '',
    'STATE_KMS_KEY_ID': 'alias/aws/s3',
    'SOURCE_SHA': 'a' * 40,
    'EXECUTION_ID': 'example-execution',
}
TFVARS = {
    'allowed_account_id': ENV['EXPECTED_ACCOUNT_ID'],
    'region': ENV['AWS_DEFAULT_REGION'],
    'function_name': ENV['EXPECTED_FUNCTION_NAME'],
    'execution_role_name': ENV['EXPECTED_EXECUTION_ROLE_NAME'],
    'queue_name': ENV['EXPECTED_QUEUE_NAME'],
    'environment_variables': {'DB_SECRET_NAME': 'example-secret'},
}
BACKEND = {
    'bucket': ENV['STATE_BUCKET'],
    'key': ENV['STATE_KEY'],
    'region': ENV['AWS_DEFAULT_REGION'],
    'kms_key_id': 'alias/aws/s3',
    'encrypt': True,
    'use_lockfile': True,
}
SHARED = {name: f'arn:aws:lambda:us-east-1:123456789012:layer:shared-{name}:2' for name in ('boto3', 'psycopg2')}


class DeploymentTests(unittest.TestCase):
    def load(self, tfvars=None, backend=None, extra=None):
        env = (
            ENV
            | {'TFVARS_JSON': json.dumps(TFVARS if tfvars is None else tfvars), 'BACKEND_JSON': json.dumps(BACKEND if backend is None else backend)}
            | (extra or {})
        )
        with patch.dict(os.environ, env, clear=True):
            result = deploy.inputs()
            self.assertNotIn('TFVARS_JSON', os.environ)
            self.assertNotIn('BACKEND_JSON', os.environ)
            return result

    def test_private_target_settings_are_required(self):
        self.assertEqual(self.load(), (TFVARS, BACKEND))
        for key in ('allowed_account_id', 'region', 'function_name', 'execution_role_name', 'queue_name'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.load(TFVARS | {key: 'wrong-target'})

    def test_backend_cannot_disable_locking_redirect_state_or_inject_credentials(self):
        for change in (
            {'key': 'other.tfstate'},
            {'bucket': 'other-state'},
            {'encrypt': False},
            {'use_lockfile': False},
            {'profile': 'admin'},
            {'kms_key_id': 'other-key'},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.load(backend=BACKEND | change)

    def test_runtime_paths_layers_policies_and_local_mode_are_constrained(self):
        for change in (
            {'artifacts_directory': '/other'},
            {'dependency_layer_names': {'boto3': 'other'}},
            {'runtime': 'python3.12'},
            {'architectures': ['arm64']},
            {'additional_execution_policy_arns': ['arn:aws:iam::aws:policy/AdministratorAccess']},
            {'environment_variables': {'LOCAL_DEV': 'true'}},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.load(TFVARS | change)

    def test_shared_layers_require_both_exact_version_pins_and_private_permission_match(self):
        configured = TFVARS | {'shared_layer_version_arns': SHARED}
        self.assertEqual(self.load(configured, extra={'EXPECTED_SHARED_LAYER_VERSION_ARNS': json.dumps(SHARED)})[0], configured)
        for pins in (
            None,
            [],
            {'boto3': SHARED['boto3']},
            SHARED | {'other': SHARED['boto3']},
            SHARED | {'boto3': SHARED['boto3'].removesuffix(':2')},
            SHARED | {'boto3': SHARED['boto3'].replace(':2', ':latest')},
            SHARED | {'boto3': SHARED['boto3'].replace(':2', ':0')},
            SHARED | {'boto3': SHARED['boto3'].replace('us-east-1', 'us-west-2')},
            SHARED | {'boto3': SHARED['boto3'].replace('123456789012', '999999999999')},
        ):
            with self.subTest(pins=pins), self.assertRaises(ValueError):
                self.load(TFVARS | {'shared_layer_version_arns': pins}, extra={'EXPECTED_SHARED_LAYER_VERSION_ARNS': json.dumps(pins)})
        with self.assertRaisesRegex(ValueError, 'private pipeline permissions'):
            self.load(configured)
        with self.assertRaisesRegex(ValueError, 'Layer names'):
            self.load(
                configured | {'dependency_layer_names': {'boto3': 'example-dev-boto3'}},
                extra={'EXPECTED_SHARED_LAYER_VERSION_ARNS': json.dumps(SHARED)},
            )

    def test_configuration_seal_is_canonical_and_binds_execution_and_environment(self):
        with patch.dict(os.environ, ENV, clear=True):
            seal = deploy.configuration(TFVARS, BACKEND)
            self.assertEqual(seal, deploy.configuration(dict(reversed(list(TFVARS.items()))), BACKEND))
            self.assertNotEqual(seal, deploy.configuration(TFVARS | {'timeout': 60}, BACKEND))
            os.environ['EXECUTION_ID'] = 'another-execution'
            self.assertNotEqual(seal, deploy.configuration(TFVARS, BACKEND))

    def test_plan_accepts_lambda_updates_and_retained_layer_replacements(self):
        changes = [
            {'address': 'aws_lambda_function.cdc_handler', 'change': {'actions': ['update']}},
            {
                'address': 'aws_lambda_layer_version.dependency["boto3"]',
                'change': {'actions': ['create', 'delete'], 'before': {'skip_destroy': True}, 'after': {'skip_destroy': True}},
            },
        ]
        deploy.guard_plan({'resource_changes': changes}, TFVARS)
        for address in ('aws_iam_role_policy_attachment.lambda_vpc_policy_attachment', 'aws_iam_role_policy.database_secret[0]'):
            deploy.guard_plan({'resource_changes': [{'address': address, 'change': {'actions': ['create']}}]}, TFVARS)

    def test_plan_rejects_destructive_unexpected_and_unretained_resources(self):
        for address, change in (
            ('aws_lambda_function.cdc_handler', {'actions': ['delete', 'create']}),
            ('aws_sqs_queue.cdc_events_q', {'actions': ['delete']}),
            ('aws_sqs_queue.cdc_events_q', {'actions': ['forget']}),
            ('aws_db_instance.unexpected', {'actions': ['create']}),
            ('aws_lambda_layer_version.dependency["boto3"]', {'actions': ['delete']}),
            (
                'aws_lambda_layer_version.dependency["boto3"]',
                {'actions': ['create', 'delete'], 'before': {'skip_destroy': False}, 'after': {'skip_destroy': True}},
            ),
        ):
            with self.subTest(address=address, change=change), self.assertRaises(ValueError):
                deploy.guard_plan({'resource_changes': [{'address': address, 'change': change}]}, TFVARS)
        with self.assertRaises(ValueError):
            deploy.guard_plan({'complete': False}, TFVARS)

    def test_shared_mode_only_retires_retained_layers_owned_by_this_function(self):
        configured = TFVARS | {'shared_layer_version_arns': SHARED}
        before = {
            'skip_destroy': True,
            'layer_name': 'example-dev-boto3',
            'arn': 'arn:aws:lambda:us-east-1:123456789012:layer:example-dev-boto3:3',
        }
        resource = {
            'address': 'aws_lambda_layer_version.dependency["boto3"]',
            'change': {'actions': ['delete'], 'before': before, 'after': None},
        }
        deploy.guard_plan({'resource_changes': [resource]}, configured)
        for change in (
            {'actions': ['create']},
            {'actions': ['update']},
            {'actions': ['create', 'delete']},
            {'before': before | {'skip_destroy': False}},
            {'before': before | {'layer_name': 'shared-boto3'}},
            {'before': before | {'arn': SHARED['boto3']}},
            {'before': before | {'arn': before['arn'].replace('123456789012', '999999999999')}},
            {'after': before},
        ):
            invalid = resource | {'change': resource['change'] | change}
            with self.subTest(change=change), self.assertRaises(ValueError):
                deploy.guard_plan({'resource_changes': [invalid]}, configured)

    def test_shared_lambda_plan_cannot_select_other_or_unknown_layer_versions(self):
        configured = TFVARS | {'shared_layer_version_arns': SHARED}
        change = {'actions': ['update'], 'after': {'layers': [SHARED['boto3'], SHARED['psycopg2']]}}
        resource = {'address': 'aws_lambda_function.cdc_handler', 'change': change}
        deploy.guard_plan({'resource_changes': [resource]}, configured)
        for invalid in (
            change | {'after': {'layers': [SHARED['boto3']]}},
            change | {'after': {'layers': [SHARED['boto3'].removesuffix(':2') + ':3', SHARED['psycopg2']]}},
            change | {'after_unknown': {'layers': True}},
            change | {'after': None},
        ):
            with self.subTest(change=invalid), self.assertRaisesRegex(ValueError, 'exact approved'):
                deploy.guard_plan({'resource_changes': [resource | {'change': invalid}]}, configured)

    def fixture(self, root):
        names = [
            'terraform/main.tf',
            'terraform/.terraform.lock.hcl',
            'scripts/deploy.py',
            'scripts/package_lambda.py',
            'dist/release/function.zip',
            'dist/release/boto3.zip',
            'dist/release/psycopg2.zip',
            'dist/plan/approved.tfplan',
            'dist/plan/plan.txt',
            'dist/plan/configuration.sha256',
        ]
        for name in names:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('example payload')

    def test_nested_publisher_environment_inputs_are_rejected_before_deployment(self):
        for name in ('private.tfvars', 'private.tfvars.json'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                private_input = root / 'terraform/shared-layers' / name
                private_input.parent.mkdir(parents=True)
                private_input.write_text('not-for-the-public-repository')
                with (
                    patch.dict(os.environ, ENV | {'CODEBUILD_RESOLVED_SOURCE_VERSION': ENV['SOURCE_SHA']}, clear=True),
                    patch.object(deploy, 'run', return_value=ENV['EXPECTED_ACCOUNT_ID']) as commands,
                    patch.object(deploy, 'inputs') as inputs,
                    patch.object(deploy, 'install_terraform') as terraform,
                    patch.object(deploy.package_lambda, 'build') as packages,
                    self.assertRaisesRegex(ValueError, 'Do not commit Terraform environment inputs'),
                ):
                    deploy.deploy('plan', root)
                self.assertEqual(commands.call_count, 1)
                self.assertEqual(commands.call_args.args[:2], ('aws', 'sts'))
                inputs.assert_not_called()
                terraform.assert_not_called()
                packages.assert_not_called()

    def test_any_package_plan_configuration_or_helper_change_invalidates_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            for path in deploy.payload_files(root):
                with self.subTest(file=path):
                    checksum = deploy.seal(root)
                    with patch.dict(os.environ, {'APPROVED_CHECKSUM_SHA256': checksum}):
                        deploy.verify(root)
                        before = path.read_bytes()
                        path.write_bytes(before + b'tampered')
                        with self.assertRaises(ValueError):
                            deploy.verify(root)
                        path.write_bytes(before)

    def test_resealed_payload_cannot_substitute_the_independent_approval_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            approved = deploy.seal(root)
            (root / 'scripts/deploy.py').write_text('tampered verifier')
            deploy.seal(root)
            with patch.dict(os.environ, {'APPROVED_CHECKSUM_SHA256': approved}), self.assertRaises(ValueError):
                deploy.verify(root)

    def test_apply_only_initializes_and_applies_saved_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'source'
            self.fixture(root)
            with patch.dict(os.environ, ENV | {'TFVARS_JSON': json.dumps(TFVARS), 'BACKEND_JSON': json.dumps(BACKEND)}, clear=True):
                (root / 'dist/plan/configuration.sha256').write_bytes(deploy.canonical(deploy.configuration(TFVARS, BACKEND)))
                os.environ['APPROVED_CHECKSUM_SHA256'] = deploy.seal(root)
                commands = []

                def fake_run(*args, **kwargs):
                    commands.append(args)
                    return ENV['EXPECTED_ACCOUNT_ID'] if args[:2] == ('aws', 'sts') else ''

                with (
                    patch.object(deploy, 'WORK', Path(directory) / 'work'),
                    patch.object(deploy, 'run', fake_run),
                    patch.object(deploy, 'install_terraform', return_value='pinned-terraform'),
                    patch.object(deploy.package_lambda, 'build') as build,
                    patch.object(deploy, 'tests') as tests,
                ):
                    deploy.deploy('apply', root)
                self.assertEqual([args[2] for args in commands if args[0] == 'pinned-terraform'], ['init', 'apply'])
                self.assertEqual(commands[-1][-1], '../dist/plan/approved.tfplan')
                build.assert_not_called()
                tests.assert_not_called()

    def test_shared_apply_does_not_download_rebuild_or_retest_layer_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'source'
            self.fixture(root)
            configured = TFVARS | {'shared_layer_version_arns': SHARED}
            env = ENV | {
                'TFVARS_JSON': json.dumps(configured), 'BACKEND_JSON': json.dumps(BACKEND),
                'EXPECTED_SHARED_LAYER_VERSION_ARNS': json.dumps(SHARED),
            }
            with patch.dict(os.environ, env, clear=True):
                (root / 'dist/plan/configuration.sha256').write_bytes(deploy.canonical(deploy.configuration(configured, BACKEND)))
                os.environ['APPROVED_CHECKSUM_SHA256'] = deploy.seal(root)
                commands = []

                def fake_run(*args, **kwargs):
                    commands.append(args)
                    return ENV['EXPECTED_ACCOUNT_ID'] if args[:2] == ('aws', 'sts') else ''

                with (
                    patch.object(deploy, 'WORK', Path(directory) / 'work'),
                    patch.object(deploy, 'run', fake_run),
                    patch.object(deploy, 'install_terraform', return_value='pinned-terraform'),
                    patch.object(deploy.package_lambda, 'build') as build,
                    patch.object(deploy.package_lambda, 'shared_layer') as download,
                    patch.object(deploy.package_lambda, 'smoke') as smoke,
                    patch.object(deploy, 'tests') as tests,
                ):
                    deploy.deploy('apply', root)
                self.assertEqual([args[2] for args in commands if args[0] == 'pinned-terraform'], ['init', 'apply'])
                build.assert_not_called()
                download.assert_not_called()
                smoke.assert_not_called()
                tests.assert_not_called()


if __name__ == '__main__':
    unittest.main()
