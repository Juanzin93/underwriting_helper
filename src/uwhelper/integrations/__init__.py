"""Opt-in external integrations (none enabled by default).

Rules for any future adapter (credit bureaus, lender portals, e-signature, bank data, cloud backup…):
* It must be registered here with a name and a plain-language description of what data leaves the machine.
* It runs only if BOTH ``external_integrations_allowed`` is true AND its name is in ``enabled_integrations``.
* Every call that sends borrower documents, financial data or personal information must be started by the user
  and must show exactly what will be shared and ask for confirmation (``require_share_confirmation``).
* Adapters live behind this interface so the core never imports a provider SDK.
Milestone 1 ships the guard rails only — no provider is implemented.
"""

from __future__ import annotations

from dataclasses import dataclass

from uwhelper.config import Settings


class IntegrationDisabled(PermissionError):
    pass


@dataclass(frozen=True)
class IntegrationSpec:
    name: str
    description: str
    data_shared: str
    external: bool = True


REGISTRY: dict[str, IntegrationSpec] = {}


def register(spec: IntegrationSpec) -> None:
    REGISTRY[spec.name] = spec


def is_enabled(settings: Settings, name: str) -> bool:
    spec = REGISTRY.get(name)
    if spec is None:
        return False
    if spec.external and not settings.external_integrations_allowed:
        return False
    return name in settings.integration_names


def require_enabled(settings: Settings, name: str) -> IntegrationSpec:
    if name not in REGISTRY:
        raise IntegrationDisabled(f"Integration {name!r} is not registered.")
    if not is_enabled(settings, name):
        raise IntegrationDisabled(f"Integration {name!r} is disabled. Enable it explicitly in settings "
                                  "(external_integrations_allowed + enabled_integrations).")
    return REGISTRY[name]


def require_share_confirmation(spec: IntegrationSpec, *, user_confirmed: bool, summary_shown: str) -> None:
    if not user_confirmed or not summary_shown.strip():
        raise IntegrationDisabled(f"Sending data to {spec.name} requires the user to review what will be shared "
                                  f"({spec.data_shared}) and confirm.")


def status(settings: Settings) -> dict[str, object]:
    return {
        "external_integrations_allowed": settings.external_integrations_allowed,
        "registered": sorted(REGISTRY),
        "enabled": sorted(n for n in REGISTRY if is_enabled(settings, n)),
    }
