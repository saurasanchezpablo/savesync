"""Optional, read-only Playnite metadata (plan §20).

Playnite's internal library database is not a stable integration point, so
this adapter reads a JSON export instead — a list of games as written by a
Playnite export script/extension, e.g.:

    [{"Name": "Hades", "Platforms": ["PC (Windows)"], "CoverImage": "…/cover.jpg",
      "Icon": "…/icon.png", "GameId": "1145360", "Source": "Steam"}]

Relative image paths are resolved against `library_files_dir` (Playnite's
`library\\files` folder) or the export's own folder. Without the export the
provider simply answers nothing; Save Sync never depends on it.
"""
from __future__ import annotations

import json
import os

from .interfaces import GameMetadataProvider
from .ludusavi import fold


def _first(value):
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                item = item.get("Name")
            if item:
                return str(item)
        return None
    if isinstance(value, dict):
        return value.get("Name")
    return str(value) if value else None


class PlayniteMetadataProvider(GameMetadataProvider):
    name = "playnite"

    def __init__(self, export_path: str, library_files_dir: str | None = None):
        self.export_path = export_path
        self.library_files_dir = library_files_dir
        self._games = None
        self._mtime = None

    def _load(self) -> dict:
        try:
            mtime = os.path.getmtime(self.export_path)
        except (OSError, TypeError):
            return {}
        if self._games is not None and mtime == self._mtime:
            return self._games
        try:
            with open(self.export_path, encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        if isinstance(data, dict):
            data = data.get("Games") or data.get("games") or []
        games = {}
        for game in data if isinstance(data, list) else []:
            if isinstance(game, dict) and game.get("Name"):
                games.setdefault(fold(game["Name"]), game)
        self._games, self._mtime = games, mtime
        return games

    def _game(self, title):
        return self._load().get(fold(title))

    def _image(self, value):
        if not value:
            return None
        value = str(value)
        candidates = [value] if os.path.isabs(value) else []
        for base in (self.library_files_dir, os.path.dirname(self.export_path or "")):
            if base and not os.path.isabs(value):
                candidates.append(os.path.join(base, value))
        return next((c for c in candidates if os.path.isfile(c)), None)

    def get_title(self, title):
        game = self._game(title)
        return game.get("Name") if game else None

    def get_cover(self, title):
        game = self._game(title)
        return self._image(game.get("CoverImage")) if game else None

    def get_icon(self, title):
        game = self._game(title)
        return self._image(game.get("Icon")) if game else None

    def get_platform(self, title):
        game = self._game(title)
        return _first(game.get("Platforms") or game.get("Platform")) if game else None

    def get_ids(self, title):
        game = self._game(title)
        if not game:
            return {}
        ids = {}
        if game.get("GameId"):
            source = _first(game.get("Source")) or "playnite"
            ids[str(source).lower()] = str(game["GameId"])
        if game.get("Id"):
            ids["playnite"] = str(game["Id"])
        return ids
