"""Load config.yaml and .env. Keys are never logged or printed."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True, slots=True)
class Box:
    west: float
    south: float
    east: float
    north: float

    def contains(self, lat: float, lon: float) -> bool:
        return self.south <= lat <= self.north and self.west <= lon <= self.east

    @property
    def bbox_param(self) -> str:
        """minLon,minLat,maxLon,maxLat as TomTom's bbox parameter wants it."""
        return f"{self.west},{self.south},{self.east},{self.north}"


def load_dotenv(path: str | os.PathLike = ".env") -> None:
    """Minimal .env loader: KEY=VALUE lines, '#' comments, no export keyword needed.
    Values already in the environment win."""
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_config(path: str | os.PathLike = "config.yaml") -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    box = cfg.get("box") or {}
    cfg["box_obj"] = Box(float(box["west"]), float(box["south"]), float(box["east"]), float(box["north"]))
    if cfg["box_obj"].west >= cfg["box_obj"].east or cfg["box_obj"].south >= cfg["box_obj"].north:
        raise ValueError("config box is inverted: west<east and south<north are required")
    cfg["roads"] = [str(r).upper() for r in (cfg.get("roads") or [])]
    return cfg


def secret(name: str) -> str | None:
    """Read a secret from the environment. Returns None when unset or empty."""
    v = os.environ.get(name, "").strip()
    return v or None


def redact(text: str, *secrets: str | None) -> str:
    """Replace any secret value that appears in text with '***'."""
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text
