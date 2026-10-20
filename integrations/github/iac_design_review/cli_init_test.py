"""Unit tests for cli_init.py."""

import os
import subprocess
import tempfile
from unittest import mock

from absl.testing import absltest

from iac_design_review import cli_init
from iac_design_review import config


class CliInitTest(absltest.TestCase):
  """Tests `autocloud-github-init` repository scaffolding and remote setup."""

  def test_cli_init_generates_valid_config_and_workflow(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      cfg_path, wf_path = cli_init.initialize_repository(
          repo_dir=tmp_dir,
          project_id='my-gcp-project',
          agent_id='autocloud-operator-prod',
      )
      self.assertTrue(os.path.isfile(cfg_path))
      self.assertTrue(os.path.isfile(wf_path))
      loaded = config.load_config(cfg_path)
      self.assertEqual(loaded.project_id, 'my-gcp-project')
      self.assertEqual(loaded.agent_id, 'operator-prod')

    with tempfile.TemporaryDirectory() as tmp_dir_default:
      cfg_path, _ = cli_init.initialize_repository(
          repo_dir=tmp_dir_default,
          project_id='my-gcp-project',
      )
      loaded_default = config.load_config(cfg_path)
      self.assertEqual(loaded_default.agent_id, '')

  def test_run_interactive_wizard_collects_inputs(self):
    answers = iter([
        'my-org/brownfield-poc',
        'my-gcp-project',
        'operator-prod',
        (
            'GoogleCloudPlatform/gemini-cloud-assist-mcp/'
            'integrations/github/actions/iac-review@main'
        ),
        'y',
    ])
    repo, proj, agent, action_ref, enable_code = (
        cli_init.run_interactive_wizard(input_fn=lambda _: next(answers))
    )
    self.assertEqual(repo, 'my-org/brownfield-poc')
    self.assertEqual(proj, 'my-gcp-project')
    self.assertEqual(agent, 'operator-prod')
    self.assertEqual(
        action_ref,
        (
            'GoogleCloudPlatform/gemini-cloud-assist-mcp/'
            'integrations/github/actions/iac-review@main'
        ),
    )
    self.assertTrue(enable_code)

  @mock.patch.object(cli_init, 'ensure_adc_credentials')
  @mock.patch.object(subprocess, 'run')
  def test_onboard_remote_repository_executes_four_gh_steps(
      self, mock_run, mock_ensure_adc
  ):
    mock_ensure_adc.return_value = '{"refresh_token": "tok"}'
    # 1: gh secret set (check=True)
    # 2: gh api PUT .../actions/permissions/workflow (check=True)
    # 3: gh api GET .github/cloud-operator.yml (404 not found -> returncode 1)
    # 4: gh api PUT .github/cloud-operator.yml (check=True)
    # 5: gh api GET .github/workflows/cloud-operator.yml (existing sha)
    # 6: gh api PUT .github/workflows/cloud-operator.yml (with sha, check=True)
    mock_run.side_effect = [
        subprocess.CompletedProcess(args=[], returncode=0, stdout='', stderr=''),
        subprocess.CompletedProcess(args=[], returncode=0, stdout='', stderr=''),
        subprocess.CompletedProcess(
            args=[], returncode=1, stdout='', stderr='Not Found'
        ),
        subprocess.CompletedProcess(args=[], returncode=0, stdout='', stderr=''),
        subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"sha": "abc123sha"}', stderr=''
        ),
        subprocess.CompletedProcess(args=[], returncode=0, stdout='', stderr=''),
    ]

    cli_init.onboard_remote_repository(
        github_repo='my-org/brownfield-poc',
        project_id='my-gcp-project',
        agent_id='autocloud-operator-prod',
        action_ref=(
            'GoogleCloudPlatform/gemini-cloud-assist-mcp/'
            'integrations/github/actions/iac-review@main'
        ),
        adc_path='/fake/adc.json',
    )

    self.assertEqual(mock_run.call_count, 6)
    secret_cmd = mock_run.call_args_list[0].args[0]
    self.assertEqual(
        secret_cmd,
        [
            'gh',
            'secret',
            'set',
            'CLOUD_OPERATOR_OAUTH_CONFIG',
            '--repo',
            'my-org/brownfield-poc',
        ],
    )
    perms_cmd = mock_run.call_args_list[1].args[0]
    self.assertIn(
        'repos/my-org/brownfield-poc/actions/permissions/workflow',
        perms_cmd,
    )
    wf_put_cmd = mock_run.call_args_list[5].args[0]
    self.assertIn('sha=abc123sha', wf_put_cmd)


if __name__ == '__main__':
  absltest.main()
