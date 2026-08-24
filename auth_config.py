"""
Anthropic credential provisioning for the Inspection Agent Service.

The Claude Agent SDK shells out to the bundled Claude Code CLI, which reads
credentials from environment variables - there is no credentials= argument
on ClaudeAgentOptions. So "provisioning the API key" means: get the right
env vars set, correctly, before any ClaudeSDKClient is constructed, and fail
loudly and early if that didn't happen - never let a job silently run
unauthenticated and discover the problem 40 tool calls in.

Supports three auth backends (mirrors what Claude Code itself supports):
  - Direct Anthropic API key  (ANTHROPIC_API_KEY)
  - Amazon Bedrock            (CLAUDE_CODE_USE_BEDROCK=1 + AWS credentials)
  - Google Vertex AI          (CLAUDE_CODE_USE_VERTEX=1 + GCP credentials)

Direct API key is default. Bedrock/Vertex are selected by env flag because
that's how the underlying CLI picks them - this module doesn't invent new
config surface, it just validates what's already there before use.
"""

import os
from dataclasses import dataclass
from enum import Enum


class AuthBackend(str, Enum):
    API_KEY = "api_key"
    BEDROCK = "bedrock"
    VERTEX = "vertex"


class CredentialError(RuntimeError):
    """Raised when credentials are missing or malformed. Fail fast on this -
    never let a job start against an agent that can't authenticate."""


@dataclass
class ResolvedCredentials:
    backend: AuthBackend
    detail: str  # human-readable, safe to log (never contains the secret itself)


def _mask(secret: str) -> str:
    if len(secret) <= 8:
        return "****"
    return f"{secret[:6]}...{secret[-4:]}"


def get_secret_from_manager(secret_name: str) -> str | None:
    """
    Extension point: wire this to your actual secrets manager (AWS Secrets
    Manager, GCP Secret Manager, HashiCorp Vault, etc.) instead of relying
    on a plaintext env var in production. Called only as a fallback when the
    env var itself isn't set - see resolve_credentials() below.

    Returns None if not implemented / not found, so callers fall through to
    the "missing" error path rather than crashing here.
    """
    # Example (commented, not wired - fill in for your actual provider):
    #
    # import boto3
    # client = boto3.client("secretsmanager")
    # resp = client.get_secret_value(SecretId=secret_name)
    # return resp["SecretString"]
    return None


def resolve_credentials() -> ResolvedCredentials:
    """
    Call this once at service startup (see api_service.py's startup hook)
    and again defensively before each job if you rotate keys frequently.
    Raises CredentialError with a specific, actionable message on failure -
    never returns a partially-valid state.
    """

    if os.environ.get("CLAUDE_CODE_USE_BEDROCK") == "1":
        missing = [
            v for v in ("AWS_REGION",)
            if not os.environ.get(v)
        ]
        # AWS SDK credential chain (env vars, instance role, etc.) handles
        # the actual AWS auth - we just confirm the region is set, since a
        # missing region is the single most common misconfiguration here.
        if missing:
            raise CredentialError(
                f"CLAUDE_CODE_USE_BEDROCK=1 but missing required env var(s): {missing}. "
                "Also confirm AWS credentials are available via the standard AWS "
                "credential chain (env vars, ~/.aws/credentials, or instance role)."
            )
        return ResolvedCredentials(AuthBackend.BEDROCK, "Amazon Bedrock (region-scoped)")

    if os.environ.get("CLAUDE_CODE_USE_VERTEX") == "1":
        missing = [
            v for v in ("ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION")
            if not os.environ.get(v)
        ]
        if missing:
            raise CredentialError(
                f"CLAUDE_CODE_USE_VERTEX=1 but missing required env var(s): {missing}."
            )
        return ResolvedCredentials(AuthBackend.VERTEX, "Google Vertex AI (project-scoped)")

    # Default path: direct Anthropic API key
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")

    if not api_key:
        # Fall back to a secrets manager before giving up.
        fetched = get_secret_from_manager("anthropic-api-key")
        if fetched:
            os.environ["ANTHROPIC_API_KEY"] = fetched
            api_key = fetched

    if not api_key:
        raise CredentialError(
            "No Anthropic credentials found. Set ANTHROPIC_API_KEY (or "
            "ANTHROPIC_AUTH_TOKEN) in the environment, wire "
            "get_secret_from_manager() to your secrets store, or set "
            "CLAUDE_CODE_USE_BEDROCK=1 / CLAUDE_CODE_USE_VERTEX=1 with the "
            "corresponding cloud credentials."
        )

    if not api_key.startswith("sk-ant-"):
        raise CredentialError(
            f"ANTHROPIC_API_KEY does not match the expected 'sk-ant-...' format "
            f"(got {_mask(api_key)}). Check for a copy-paste error or a stray "
            "quote/whitespace character in your env file."
        )

    return ResolvedCredentials(AuthBackend.API_KEY, f"Direct API key ({_mask(api_key)})")


def require_credentials_at_startup() -> ResolvedCredentials:
    """Call this from the FastAPI startup event. Crashes the process on
    failure - deliberately, since a service that can't authenticate should
    not come up and start silently failing jobs one at a time."""
    creds = resolve_credentials()
    print(f"[inspection-service] Authenticated via {creds.detail}")
    return creds
