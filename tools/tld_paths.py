"""Resolve The Long Dark / TerrainDumper paths from the environment.

Environment variables (no hardcoded install paths in the repo):

  TLD_PATH
      Game install root, e.g. .../steamapps/common/TheLongDark

  TERRAIN_DUMPER_ROOT
      Dump folder, e.g. .../Mods/TerrainDumper
      Optional when TLD_PATH is set (defaults to $TLD_PATH/Mods/TerrainDumper).
"""

from __future__ import annotations

import os
from pathlib import Path

_MISSING = (
    "Set TLD_PATH to your The Long Dark install folder "
    "(e.g. .../steamapps/common/TheLongDark), or TERRAIN_DUMPER_ROOT to the "
    "Mods/TerrainDumper dump folder."
)


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip().strip('"')
    return Path(raw) if raw else None


def tld_path(*, required: bool = False) -> Path | None:
    """Game install root from TLD_PATH."""
    p = _env_path("TLD_PATH")
    if p is not None:
        return p
    if required:
        raise SystemExit(_MISSING)
    return None


def dump_root(*, required: bool = False) -> Path | None:
    """TerrainDumper dump folder from TERRAIN_DUMPER_ROOT or TLD_PATH."""
    p = _env_path("TERRAIN_DUMPER_ROOT")
    if p is not None:
        return p
    game = tld_path(required=False)
    if game is not None:
        return game / "Mods" / "TerrainDumper"
    if required:
        raise SystemExit(_MISSING)
    return None


def require_dump_root() -> Path:
    return dump_root(required=True)  # type: ignore[return-value]


def require_tld_path() -> Path:
    return tld_path(required=True)  # type: ignore[return-value]


def resolve_dump_root(cli: Path | None = None, *, config_value: str | None = None) -> Path:
    """CLI > non-empty config > environment."""
    if cli is not None:
        return cli
    if config_value and str(config_value).strip():
        return Path(str(config_value).strip())
    return require_dump_root()
