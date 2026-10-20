"""Unit tests for config.py."""

from absl.testing import absltest

from iac_design_review import config


class ConfigTest(absltest.TestCase):
  """Tests `.github/cloud-operator.yml` schema parsing and event routing."""

  def test_parse_valid_minimal_config_defaults_to_personal_agent(self):
    yaml_str = """
    version: 1
    project_id: "my-gcp-project"
    """
    parsed = config.parse_config_yaml(yaml_str)
    self.assertEqual(parsed.version, 1)
    self.assertEqual(parsed.project_id, 'my-gcp-project')
    self.assertEqual(parsed.agent_id, '')
    self.assertTrue(parsed.scenarios.code_changes.enabled)
    self.assertEqual(
        parsed.scenarios.code_changes.trigger_command, '/cloud-operator review'
    )
    self.assertEqual(parsed.scenarios.code_changes.pr_mode, 'inline_comments')
    self.assertEqual(
        parsed.scenarios.code_changes.allowed_author_associations,
        ('OWNER', 'MEMBER', 'COLLABORATOR'),
    )
    self.assertFalse(parsed.scenarios.code_changes.allow_fork_prs)
    self.assertFalse(parsed.scenarios.issue_implementation.enabled)

  def test_normalize_agent_id_strips_reserved_autocloud_prefix(self):
    yaml_str = """
    version: 1
    project_id: "my-gcp-project"
    agent_id: "autocloud-operator-prod"
    """
    parsed = config.parse_config_yaml(yaml_str)
    self.assertEqual(parsed.agent_id, 'operator-prod')

    with self.assertRaises(config.ConfigValidationError):
      config.parse_config_yaml(
          'version: 1\nproject_id: "my-gcp-project"\nagent_id: "autocloud-"\n'
      )

  def test_rejects_invalid_project_id_or_version(self):
    with self.assertRaises(config.ConfigValidationError):
      config.parse_config_yaml('version: 2\nproject_id: "my-gcp-project"\n')
    with self.assertRaises(config.ConfigValidationError):
      config.parse_config_yaml('version: 1\nproject_id: "BAD_PROJECT!"\n')

  def test_rejects_unsupported_code_trigger(self):
    yaml_str = """
    version: 1
    project_id: "my-gcp-project"
    scenarios:
      code_changes:
        triggers:
          - push.main
    """
    with self.assertRaises(config.ConfigValidationError):
      config.parse_config_yaml(yaml_str)

  def test_rejects_empty_trigger_command(self):
    code_empty_yaml = """
    version: 1
    project_id: "my-gcp-project"
    scenarios:
      code_changes:
        trigger_command: "   "
    """
    with self.assertRaisesRegex(
        config.ConfigValidationError,
        'scenarios.code_changes.trigger_command must be a non-empty string',
    ):
      config.parse_config_yaml(code_empty_yaml)

    issue_empty_yaml = """
    version: 1
    project_id: "my-gcp-project"
    scenarios:
      issue_implementation:
        trigger_command: ""
    """
    with self.assertRaisesRegex(
        config.ConfigValidationError,
        'scenarios.issue_implementation.trigger_command must be a non-empty',
    ):
      config.parse_config_yaml(issue_empty_yaml)

  def test_rejects_non_string_or_non_boolean_config_types(self):
    for invalid_yaml in (
        'version: true\nproject_id: "my-gcp-project"\n',
        'version: 1\nproject_id: "my-gcp-project"\nagent_id: true\n',
        'version: 1\nproject_id: "my-gcp-project"\nscenarios: []\n',
        'version: 1\nproject_id: "my-gcp-project"\nscenarios: false\n',
        (
            'version: 1\nproject_id: "my-gcp-project"\n'
            'scenarios:\n  code_changes:\n    enabled: "false"\n'
        ),
        (
            'version: 1\nproject_id: "my-gcp-project"\n'
            'scenarios:\n  code_changes:\n    trigger_command: null\n'
        ),
        (
            'version: 1\nproject_id: "my-gcp-project"\n'
            'scenarios:\n  issue_implementation:\n    trigger_labels: [null]\n'
        ),
    ):
      with self.assertRaises(config.ConfigValidationError):
        config.parse_config_yaml(invalid_yaml)

  def test_rejects_invalid_author_associations_or_allow_fork_prs(self):
    bad_assoc_yaml = """
    version: 1
    project_id: "my-gcp-project"
    scenarios:
      code_changes:
        allowed_author_associations:
          - INVALID_ROLE
    """
    with self.assertRaises(config.ConfigValidationError):
      config.parse_config_yaml(bad_assoc_yaml)

    bad_fork_yaml = """
    version: 1
    project_id: "my-gcp-project"
    scenarios:
      code_changes:
        allow_fork_prs: "yes"
    """
    with self.assertRaises(config.ConfigValidationError):
      config.parse_config_yaml(bad_fork_yaml)

  def test_rejects_enabled_issue_implementation_as_work_in_progress(self):
    yaml_str = """
    version: 1
    project_id: "my-gcp-project"
    scenarios:
      issue_implementation:
        enabled: true
    """
    with self.assertRaisesRegex(
        config.ConfigValidationError, 'work in progress'
    ):
      config.parse_config_yaml(yaml_str)

  def test_should_trigger_code_review_pr_events_and_fork_policy(self):
    cfg = config.parse_config_yaml(
        'version: 1\nproject_id: "my-gcp-project"\n'
    )
    valid_payload = {
        'action': 'opened',
        'pull_request': {
            'number': 42,
            'draft': False,
            'author_association': 'MEMBER',
            'head': {
                'ref': 'feat/vpc-subnet',
                'repo': {'fork': False, 'full_name': 'org/repo'},
            },
            'base': {
                'repo': {'full_name': 'org/repo'},
            },
        },
    }
    self.assertTrue(
        config.should_trigger_code_review(cfg, 'pull_request', valid_payload)
    )

    draft_payload = {
        'action': 'opened',
        'pull_request': {
            'number': 42,
            'draft': True,
            'head': {'ref': 'feat/vpc-subnet'},
        },
    }
    self.assertFalse(
        config.should_trigger_code_review(cfg, 'pull_request', draft_payload)
    )

    companion_loop_payload = {
        'action': 'opened',
        'pull_request': {
            'number': 43,
            'draft': False,
            'head': {'ref': 'cloud-operator/remediate-pr-42'},
        },
    }
    self.assertFalse(
        config.should_trigger_code_review(
            cfg, 'pull_request', companion_loop_payload
        )
    )

    fork_payload = {
        'action': 'opened',
        'pull_request': {
            'number': 44,
            'draft': False,
            'author_association': 'OWNER',
            'head': {
                'ref': 'feat/from-fork',
                'repo': {'fork': True, 'full_name': 'attacker/repo'},
            },
            'base': {
                'repo': {'full_name': 'org/repo'},
            },
        },
    }
    self.assertFalse(
        config.should_trigger_code_review(cfg, 'pull_request', fork_payload)
    )

    cfg_allow_forks = config.parse_config_yaml(
        'version: 1\nproject_id: "my-gcp-project"\n'
        'scenarios:\n  code_changes:\n    allow_fork_prs: true\n'
    )
    self.assertTrue(
        config.should_trigger_code_review(
            cfg_allow_forks, 'pull_request', fork_payload
        )
    )

    untrusted_pr_author_payload = {
        'action': 'opened',
        'pull_request': {
            'number': 45,
            'draft': False,
            'author_association': 'NONE',
            'head': {'ref': 'feat/untrusted'},
        },
    }
    self.assertFalse(
        config.should_trigger_code_review(
            cfg, 'pull_request', untrusted_pr_author_payload
        )
    )

    missing_pr_assoc_payload = {
        'action': 'opened',
        'pull_request': {
            'number': 46,
            'draft': False,
            'head': {'ref': 'feat/missing-assoc'},
        },
    }
    self.assertFalse(
        config.should_trigger_code_review(
            cfg, 'pull_request', missing_pr_assoc_payload
        )
    )

  def test_should_trigger_code_review_issue_comment_author_and_first_token(
      self,
  ):
    cfg = config.parse_config_yaml(
        'version: 1\nproject_id: "my-gcp-project"\n'
    )
    valid_comment_payload = {
        'action': 'created',
        'issue': {'number': 42, 'pull_request': {'url': 'https://api.github'}},
        'comment': {
            'author_association': 'COLLABORATOR',
            'body': 'Looks good.\n  /cloud-operator review now\nThanks!',
        },
    }
    self.assertTrue(
        config.should_trigger_code_review(
            cfg, 'issue_comment', valid_comment_payload
        )
    )

    mid_sentence_payload = {
        'action': 'created',
        'issue': {'number': 42, 'pull_request': {'url': 'https://api.github'}},
        'comment': {
            'author_association': 'OWNER',
            'body': 'Please do not run /cloud-operator review on this PR.',
        },
    }
    self.assertFalse(
        config.should_trigger_code_review(
            cfg, 'issue_comment', mid_sentence_payload
        )
    )

    fenced_code_block_payload = {
        'action': 'created',
        'issue': {'number': 42, 'pull_request': {'url': 'https://api.github'}},
        'comment': {
            'author_association': 'OWNER',
            'body': 'Example usage:\n```\n/cloud-operator review\n```\n',
        },
    }
    self.assertFalse(
        config.should_trigger_code_review(
            cfg, 'issue_comment', fenced_code_block_payload
        )
    )

    untrusted_commenter_payload = {
        'action': 'created',
        'issue': {'number': 42, 'pull_request': {'url': 'https://api.github'}},
        'comment': {
            'author_association': 'NONE',
            'body': '/cloud-operator review',
        },
    }
    self.assertFalse(
        config.should_trigger_code_review(
            cfg, 'issue_comment', untrusted_commenter_payload
        )
    )

    plain_issue_comment_payload = {
        'action': 'created',
        'issue': {'number': 10},
        'comment': {
            'author_association': 'OWNER',
            'body': '/cloud-operator review',
        },
    }
    self.assertFalse(
        config.should_trigger_code_review(
            cfg, 'issue_comment', plain_issue_comment_payload
        )
    )


if __name__ == '__main__':
  absltest.main()
