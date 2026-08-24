"""Configuration and secret loading.

WHERE SECRETS LIVE — one code path, three delivery mechanisms
-------------------------------------------------------------
This module always reads plain environment variables. What differs per
environment is only *how those variables get set*:

  local dev     `.env` file at poc/.env, gitignored, loaded by pydantic-settings
  docker local  `docker run --env-file .env ...`  (never ENV/ARG in the Dockerfile —
                anything baked into an image layer is readable by anyone who
                pulls it, including after you "remove" it in a later layer)
  azure         Key Vault secret + App Service Key Vault reference:

                    az webapp config appsettings set ... --settings \
                      FOUNDRY_API_KEY="@Microsoft.KeyVault(SecretUri=https://<vault>\
                      .vault.azure.net/secrets/foundry-key/)"

                App Service resolves the reference at container start using the
                app's managed identity and injects it as an ordinary env var.
                The application code below does not change, and never sees the
                vault. Rotating the key is a vault operation plus a restart —
                no rebuild, no redeploy.

NEVER put the key in: the repo, the Docker image, appsettings committed to git,
CI logs, or anything the React bundle can reach. A key in frontend code is
public the moment the page is served, regardless of minification.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """All configuration. Secrets are never logged — see `safe_summary()`."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Foundry / Claude --------------------------------------------------
    # The SDK accepts EITHER a resource name (which it expands to
    # https://{resource}.services.ai.azure.com/anthropic/) OR a full base_url.
    # They are mutually exclusive — passing both raises. Set whichever you have.
    foundry_api_key: str = Field(min_length=8)
    foundry_resource: str | None = None
    foundry_api_endpoint: str | None = None
    model_id: str = "claude-opus-4-7"

    @model_validator(mode="after")
    def _one_of_resource_or_endpoint(self) -> "Settings":
        if not self.foundry_resource and not self.foundry_api_endpoint:
            raise ValueError(
                "set either FOUNDRY_RESOURCE (e.g. my-resource) or "
                "FOUNDRY_API_ENDPOINT (the full https://... URL)"
            )
        return self

    def foundry_client_kwargs(self) -> dict[str, str]:
        """Exactly one of resource/base_url, as the SDK requires."""
        if self.foundry_api_endpoint:
            url = self.foundry_api_endpoint.strip().rstrip("/")
            # The Claude surface lives under /anthropic on a Foundry resource.
            # Accept a bare resource URL and complete it rather than failing at
            # the first request with a confusing 404.
            if "/anthropic" not in url:
                url = f"{url}/anthropic"
            return {"base_url": url + "/"}
        return {"resource": self.foundry_resource or ""}

    # --- App ---------------------------------------------------------------
    app_env: Literal["local", "docker", "azure"] = "local"
    jwt_secret: str = Field(min_length=16)

    # A gate in front of the whole app, separate from the sign-in screen --
    # for a deployed demo that is otherwise a bare public URL. Optional and
    # both-or-nothing: unset (the default, e.g. local dev) means no gate at
    # all, so this can never lock out a developer who has never heard of it.
    basic_auth_username: str | None = None
    basic_auth_password: str | None = None

    # Absolute by default, and deliberately so. A relative sqlite URL resolves
    # against the current working directory, which means `python scripts/x.py`
    # and `cd backend && pytest` would quietly use two different databases and
    # each would look empty to the other. Override DATABASE_URL to move it.
    database_url: str = f"sqlite:///{(REPO_ROOT / 'backend' / 'data' / 'poc.db').as_posix()}"

    def resolved_database_url(self) -> str:
        """The database URL with any relative sqlite path anchored at poc/.

        A relative sqlite URL resolves against the current working directory,
        so `python scripts/init_db.py` from poc/ and `cd backend && pytest`
        would open two different files and each would look empty to the other.
        Anchoring here means an existing `DATABASE_URL=sqlite:///./data/poc.db`
        keeps working and always means the same file, from any directory.

        In-memory URLs and non-sqlite drivers pass through untouched.
        """
        url = self.database_url
        if not url.startswith("sqlite:"):
            return url
        path = url.split("///", 1)[-1] if "///" in url else ""
        if not path or path.startswith(":memory:"):
            return url
        p = Path(path)
        if p.is_absolute() or (len(path) > 1 and path[1] == ":"):
            return url
        return f"sqlite:///{(REPO_ROOT / p).resolve().as_posix()}"

    # --- Per-run budgets ---------------------------------------------------
    max_tool_calls_per_run: int = 30
    max_tokens_per_run: int = 150_000
    max_run_seconds: int = 120
    max_usd_per_run: float = 1.50

    # --- Model pricing, USD per 1M tokens (Opus 4.7, standard rates) -------
    # Foundry bills through Microsoft Marketplace at these same rates.
    price_input_per_mtok: float = 5.00
    price_output_per_mtok: float = 25.00

    def safe_summary(self) -> dict[str, str]:
        """Loggable view of config. Never includes a secret value."""
        target = self.foundry_client_kwargs()
        return {
            "app_env": self.app_env,
            "model_id": self.model_id,
            "foundry_target": next(iter(target.values())),
            "foundry_api_key": _redact(self.foundry_api_key),
            "jwt_secret": _redact(self.jwt_secret),
            "database_url": self.resolved_database_url(),
        }


def _redact(secret: str) -> str:
    """Show enough to identify which key is loaded, never enough to use it."""
    if not secret:
        return "<empty>"
    if len(secret) <= 8:
        return "<set>"
    return f"{secret[:4]}…{secret[-2:]} ({len(secret)} chars)"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load once. Fail loudly and early rather than at the first API call."""
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        missing = [
            ".".join(str(p) for p in e["loc"])
            for e in exc.errors()
            if e["type"] in {"missing", "string_too_short"}
        ]
        print(
            "\n  Configuration error — the app cannot start.\n\n"
            f"  Problem with: {', '.join(missing) or 'see below'}\n\n"
            "  Fix for local dev:\n"
            "      cd poc && cp .env.example .env    # then fill in the values\n\n"
            "  Fix on Azure: confirm the App Service setting exists and that the\n"
            "  Key Vault reference resolved — an unresolved reference arrives as\n"
            "  the literal '@Microsoft.KeyVault(...)' string, which usually means\n"
            "  the app's managed identity lacks 'Key Vault Secrets User'.\n",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
