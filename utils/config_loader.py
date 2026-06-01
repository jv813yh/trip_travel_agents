"""Loads trip parameters from config.yaml.

All agents read trip parameters through this module. Never hardcode trip
values (dates, budgets, addresses) in agent code — add them to config.yaml.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

# Project root = parent of the utils/ package directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


@lru_cache(maxsize=None)
def load_config(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Load and return the trip configuration as a dict.

    Result is cached per path so repeated calls across agents are cheap.
    """
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(
            f"config not found at {config_path}. "
            "Copy config.yaml.example to config.yaml and edit it."
        )
    with config_path.open("r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh)
    if not isinstance(config, dict):
        raise ValueError(f"config at {config_path} did not parse to a mapping")
    return config


if __name__ == "__main__":
    import json

    print(json.dumps(load_config(), indent=2, ensure_ascii=False))
