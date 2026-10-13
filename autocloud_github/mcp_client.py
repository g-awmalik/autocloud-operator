"""HTTPS MCP client and prompt builder for `ask_cloud_operator`."""

from collections.abc import Mapping
import json
import uuid

from absl import logging
import requests

from autocloud_github import auth
from autocloud_github import parser

DEFAULT_PROD_MCP_URL = 'https://geminicloudassist.googleapis.com/mcp'
DEFAULT_STAGING_MCP_URL = (
    'https://staging-geminicloudassist.sandbox.googleapis.com/mcp'
)
DEFAULT_AUTOPUSH_MCP_URL = (
    'https://autopush-geminicloudassist.sandbox.googleapis.com/mcp'
)

_CONNECT_TIMEOUT_SECONDS = 15
_READ_TIMEOUT_SECONDS = 1500


class McpTransportError(RuntimeError):
  """Raised on HTTP or transport-level failures calling the MCP endpoint."""

  def __init__(self, message: str, status_code: int | None = None):
    """Initializes `McpTransportError`.

    Args:
      message: Human-readable transport error description.
      status_code: Optional HTTP status code if an HTTP response was received.
    """
    self.status_code = status_code
    super().__init__(message)


def build_pr_review_prompt(
    *,
    project_id: str,
    repo_full_name: str,
    pr_number: int,
    pr_title: str,
    changed_files: Mapping[str, str],
    pr_mode: str = 'inline_comments',
) -> str:
  """Constructs the Option 1 IaC & code review prompt for `ask_cloud_operator`.

  Bundles modified repository files inline and instructs the AutoCloud
  Operator agent to evaluate changes using progressive disclosure (`docs/`) and
  emit inline review comments and/or complete remediated files.

  Args:
    project_id: Target GCP project ID (e.g., `g4g-operate-demo`).
    repo_full_name: GitHub `owner/repo` identifier.
    pr_number: Pull Request number.
    pr_title: Pull Request title.
    changed_files: Mapping of relative file paths to full file contents.
    pr_mode: Delivery mode (`inline_comments`, `companion_pr`, `comment_only`).

  Returns:
    Formatted prompt string for `ask_cloud_operator`.
  """
  file_sections: list[str] = []
  for rel_path, file_content in changed_files.items():
    lines = file_content.splitlines()
    numbered = '\n'.join(
        f'{idx}: {line}' for idx, line in enumerate(lines, start=1)
    )
    file_sections.append(
        f'### Existing File: `{rel_path}` ({len(lines)} lines)\n'
        f'```\n{numbered}\n```'
    )
  bundled_files_str = '\n\n'.join(file_sections)

  inline_instr = ''
  if pr_mode == 'inline_comments':
    inline_instr = (
        '3. A fenced JSON block (` ```json title="autocloud-inline-comments"`) '
        'containing a JSON array of line-anchored inline comments for the PR '
        'diff, where each item has `{"path": "<relative_path>", "line": '
        '<1_based_end_line>, "start_line": <optional_1_based_start_line>, '
        '"body": "<explanation>", "suggestion": "<optional_replacement_code>"}`'
        '. Each input file line below is prefixed with `<line_number>: ` for '
        'exact 1-based line referencing—do NOT include `<line_number>: ` '
        'prefixes inside your remediated file blocks or `suggestion` code.\n'
    )

  return (
      f'Review Pull Request #{pr_number} ("{pr_title}") in GitHub repository '
      f'`{repo_full_name}` against Google Cloud project `{project_id}`.\n\n'
      'Follow your progressive disclosure documentation under `docs/AGENTS.md` '
      '(`docs/effective-cloud/iac-review.md` and `terraform.md`) to assess IaC '
      'best practices, security posture, and organization policies.\n\n'
      'If any issues or best-practice violations are found, provide:\n'
      '1. A concise markdown summary of findings and recommendations.\n'
      '2. The COMPLETE remediated content for each modified file that needs '
      'changes, formatted inside a fenced code block with `title="<path>"` '
      '(for example: ```hcl title="envs/prod/main.tf").\n'
      f'{inline_instr}\n'
      f'## Modified Files in PR #{pr_number}\n\n{bundled_files_str}'
  )


def ask_cloud_operator(
    *,
    tokens: auth.TokenSource,
    mcp_url: str,
    project_id: str,
    agent_id: str = '',
    user_query: str,
    context_id: str | None = None,
    correlation_id: str | None = None,
) -> parser.McpAnswer:
  """Invokes `ask_cloud_operator` via JSON-RPC over HTTPS/SSE.

  Args:
    tokens: Human Google EUC token source.
    mcp_url: Full HTTPS URL to the Gemini Cloud Assist MCP endpoint.
    project_id: Target GCP project ID (without `projects/` prefix).
    agent_id: Optional target AutoCloud Operator agent ID. When empty, defaults
      to the caller's personal agent.
    user_query: Prompt string sent to the operator agent.
    context_id: Optional conversation context ID for multi-turn continuity.
    correlation_id: Optional request ID for log correlation.

  Returns:
    Parsed `parser.McpAnswer` containing `content` and `context_id`.

  Raises:
    ValueError: If `mcp_url` does not use `https://`.
    McpTransportError: On network, non-200 HTTP, or invalid JSON responses.
    parser.McpError: On tool-level or JSON-RPC errors inside HTTP 200.
  """
  if not mcp_url.startswith('https://'):
    raise ValueError(f'mcp_url must start with https://, got {mcp_url!r}')

  req_id = correlation_id or str(uuid.uuid4())
  arguments: dict[str, str] = {
      'project': f'projects/{project_id}',
      'userQuery': user_query,
  }
  if agent_id:
    arguments['agentId'] = agent_id
  if context_id:
    arguments['contextId'] = context_id

  rpc_body = {
      'jsonrpc': '2.0',
      'id': req_id,
      'method': 'tools/call',
      'params': {
          'name': 'ask_cloud_operator',
          'arguments': arguments,
      },
  }

  headers = {
      'Authorization': f'Bearer {tokens.token()}',
      'Content-Type': 'application/json',
      'Accept': 'application/json, text/event-stream',
      'X-Goog-User-Project': project_id,
  }

  logging.info(
      'Calling ask_cloud_operator (project=%s, agent_id=%s, id=%s, chars=%d)',
      project_id,
      agent_id or '<personal>',
      req_id,
      len(user_query),
  )

  try:
    response = requests.post(
        mcp_url,
        json=rpc_body,
        headers=headers,
        timeout=(_CONNECT_TIMEOUT_SECONDS, _READ_TIMEOUT_SECONDS),
    )
  except requests.RequestException as err:
    raise McpTransportError(
        'Could not reach the AutoCloud MCP endpoint.'
    ) from err

  if response.status_code != 200:
    raise McpTransportError(
        f'AutoCloud MCP endpoint returned HTTP {response.status_code}.',
        status_code=response.status_code,
    )

  json_text = parser.parse_sse(response.text)
  try:
    payload = json.loads(json_text)
  except json.JSONDecodeError as err:
    raise McpTransportError(
        'AutoCloud MCP endpoint returned non-JSON payload.'
    ) from err

  if not isinstance(payload, Mapping):
    raise McpTransportError('AutoCloud MCP payload root is not a JSON object.')

  return parser.extract_answer(payload)
