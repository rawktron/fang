"""Compatibility shims installed by Fang before user code runs."""

from .metadata import (
    DISTRIBUTIONS_MANIFEST,
    FangDistributionFinder,
    install,
    uninstall,
)

__all__ = [
    "DISTRIBUTIONS_MANIFEST",
    "FangDistributionFinder",
    "install",
    "uninstall",
]
