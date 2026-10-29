from .base import FieldSpec, LendingModule, LoanTypeSpec, MetricSpec, RequiredItem
from .commercial import CommercialModule

_REGISTRY: dict[str, LendingModule] = {}


def register(module: LendingModule) -> None:
    _REGISTRY[module.key] = module


def get_module(key: str) -> LendingModule:
    try:
        return _REGISTRY[key]
    except KeyError as exc:
        raise KeyError(f"Unknown lending module {key!r}") from exc


def all_modules() -> dict[str, LendingModule]:
    return dict(_REGISTRY)


register(CommercialModule())

__all__ = [
    "FieldSpec", "LendingModule", "LoanTypeSpec", "MetricSpec", "RequiredItem",
    "CommercialModule", "get_module", "all_modules", "register",
]
