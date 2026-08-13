"""Load a CuratorConfig from a facet-style scoring_config.json `curator` block.

Keeps per-album tuning in config (the genericity goal): a new album overrides
only what it needs; anything absent falls back to CuratorConfig's defaults.
Accepts the nested shape documented in the design spec (§6) and flattens the
`dedup`/`coverage` groups onto CuratorConfig's flat fields.
"""
from __future__ import annotations

import json
from pathlib import Path

from .select import CuratorConfig

# curator-block key -> CuratorConfig field (top-level keys map 1:1 and are implicit)
_NESTED = {
    "dedup": {"phash_max": "phash_max", "scene_cos": "scene_cos", "same_scene_minutes": "same_scene_minutes"},
    "coverage": {
        "min_shots_per_person": "min_shots_per_person",
        "min_face_quality": "min_face_quality",
        "min_eyes_open": "min_eyes_open",
        "max_swap_cost": "max_swap_cost",
    },
}


def config_from_dict(block: dict, **overrides) -> CuratorConfig:
    fields = {f for f in CuratorConfig.__dataclass_fields__}
    kwargs: dict = {}
    for k, v in block.items():
        if k in _NESTED and isinstance(v, dict):
            for sub_k, field_name in _NESTED[k].items():
                if sub_k in v:
                    kwargs[field_name] = v[sub_k]
        elif k in fields:
            kwargs[k] = v
    kwargs.update({k: v for k, v in overrides.items() if v is not None})
    return CuratorConfig(**kwargs)


def load_config(config_path: str | None, **overrides) -> CuratorConfig:
    block: dict = {}
    if config_path and Path(config_path).exists():
        data = json.loads(Path(config_path).read_text())
        block = data.get("curator", {}) or {}
    return config_from_dict(block, **overrides)
