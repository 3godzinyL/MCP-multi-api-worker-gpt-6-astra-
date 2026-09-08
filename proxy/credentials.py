"""Only environment variables or the Windows Credential Manager hold secrets."""
from __future__ import annotations

import os

SERVICE = "CodexLocalResponsesProxy"
LOCAL_TOKEN_ID = "local-proxy-token"


class CredentialError(RuntimeError):
    pass


def _vault():
    if os.name != "nt":
        raise CredentialError("Credential Manager requires Windows; use environment variables on other systems")
    # Select the OS vault explicitly; never silently fall back to a plaintext keyring.
    from keyring.backends.Windows import WinVaultKeyring
    return WinVaultKeyring()


def get_secret(name: str, env_name: str = "") -> str | None:
    value = os.environ.get(env_name, "").strip() if env_name else ""
    if value:
        return value
    if os.name != "nt":
        return None
    try:
        value = _vault().get_password(SERVICE, name)
    except Exception:
        raise CredentialError("Cannot read Windows Credential Manager") from None
    return value.strip() if value else None


def set_secret(name: str, value: str) -> None:
    if not value or "\n" in value or "\r" in value:
        raise CredentialError("The secret must be a nonempty single line")
    try:
        _vault().set_password(SERVICE, name, value)
    except Exception:
        raise CredentialError("Cannot write to Windows Credential Manager") from None


def delete_secret(name: str) -> None:
    try:
        vault = _vault()
        if vault.get_password(SERVICE, name) is not None:
            vault.delete_password(SERVICE, name)
    except Exception:
        raise CredentialError("Cannot remove credential from Windows Credential Manager") from None
