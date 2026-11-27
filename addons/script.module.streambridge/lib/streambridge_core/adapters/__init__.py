"""Provider adapter package for StreamBridge."""

from .base import (
    AdapterCapabilities,
    AdapterHealth,
    CatalogBatch,
    CatalogItem,
    ProviderAdapter,
)
from .fixture import FixtureAdapter
from .netflix_catalog import NetflixCatalogAdapter

__all__ = [
    "AdapterCapabilities",
    "AdapterHealth",
    "CatalogBatch",
    "CatalogItem",
    "FixtureAdapter",
    "NetflixCatalogAdapter",
    "ProviderAdapter",
]
