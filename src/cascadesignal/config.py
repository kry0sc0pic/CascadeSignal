from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class DataPaths:
 root: Path
 raw: Path
 curated: Path
 features: Path
 benchmark: Path


@dataclass(frozen=True)
class ProjectConfig:
 name: str
 package: str
 data: DataPaths


def load_config(path: str | Path = "configs/project.yaml") -> ProjectConfig:
 config_path = Path(path)
 payload = yaml.safe_load(config_path.read_text) or {}
 if not isinstance(payload, dict):
 raise ValueError(f"Config must be a mapping: {config_path}")

 project = _mapping(payload.get("project"), "project")
 data = _mapping(payload.get("data"), "data")

 return ProjectConfig(
 name=str(project["name"]),
 package=str(project["package"]),
 data=DataPaths(
 root=Path(str(data["root"])),
 raw=Path(str(data["raw"])),
 curated=Path(str(data["curated"])),
 features=Path(str(data["features"])),
 benchmark=Path(str(data["benchmark"])),
 ),
 )


def _mapping(value: Any, key: str) -> dict[str, Any]:
 if not isinstance(value, dict):
 raise ValueError(f"Config key must be a mapping: {key}")
 return value
