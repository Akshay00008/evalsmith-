# lib/capabilities/registry.py
# Capability registry. Modules under lib/capabilities/ register themselves
# at import time via @register_capability so we don't need a hardcoded list.

from __future__ import annotations
from typing import Callable, TypeVar

from .base import CapabilityBase

_REGISTRY: dict[str, type[CapabilityBase]] = {}

T = TypeVar("T", bound=CapabilityBase)


def register_capability(name: str) -> Callable[[type[T]], type[T]]:
    """Decorator. Registering twice is an error — capability names must be
    unique because they map 1:1 to Mission.composition.task_modality."""
    def deco(cls: type[T]) -> type[T]:
        if name in _REGISTRY:
            raise ValueError(f"Capability {name!r} already registered")
        cls.name = name
        _REGISTRY[name] = cls
        return cls
    return deco


def get_capability(name: str) -> CapabilityBase:
    """Instantiate the capability registered under `name`."""
    if name not in _REGISTRY:
        raise KeyError(f"Unknown capability {name!r}. Available: {sorted(_REGISTRY)}")
    return _REGISTRY[name]()


def list_capabilities() -> list[str]:
    return sorted(_REGISTRY)
