"""Optional, read-only game metadata (plan §20). The application is fully
functional with titles alone; providers only add covers, icons and platforms."""
from __future__ import annotations

from abc import ABC, abstractmethod


class GameMetadataProvider(ABC):
    name = "metadata"

    @abstractmethod
    def get_title(self, title: str) -> str | None: ...

    def get_cover(self, title: str) -> str | None:
        """Path to an image file, or None."""
        return None

    def get_icon(self, title: str) -> str | None:
        return None

    def get_platform(self, title: str) -> str | None:
        return None

    def get_ids(self, title: str) -> dict:
        return {}


class CompositeMetadataProvider(GameMetadataProvider):
    """First provider with an answer wins; a failing provider is skipped."""
    name = "composite"

    def __init__(self, providers):
        self.providers = [p for p in providers if p is not None]

    def _first(self, method, title):
        for provider in self.providers:
            try:
                value = getattr(provider, method)(title)
            except Exception:
                continue
            if value:
                return value
        return None

    def get_title(self, title):
        return self._first("get_title", title) or title

    def get_cover(self, title):
        return self._first("get_cover", title)

    def get_icon(self, title):
        return self._first("get_icon", title)

    def get_platform(self, title):
        return self._first("get_platform", title)

    def get_ids(self, title):
        out = {}
        for provider in reversed(self.providers):
            try:
                out.update(provider.get_ids(title) or {})
            except Exception:
                continue
        return out
