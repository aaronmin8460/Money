from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.ml.schema import FEATURE_VERSION


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _default_registry() -> dict[str, Any]:
    return {
        "created_at": _utc_now(),
        "promoted": False,
        "current_model": None,
        "candidate_model": None,
        "previous_current_model": None,
        "model_type": None,
        "feature_version": FEATURE_VERSION,
        "train_rows": 0,
        "validation_rows": 0,
        "metrics": {},
        "notes": "",
    }


def initialize_registry(path: str | Path) -> dict[str, Any]:
    registry_path = Path(path)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    if registry_path.exists():
        return load_registry(registry_path)
    payload = _default_registry()
    registry_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def load_registry(path: str | Path) -> dict[str, Any]:
    registry_path = Path(path)
    if not registry_path.exists():
        return initialize_registry(registry_path)
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = _default_registry()
    for key, value in _default_registry().items():
        payload.setdefault(key, value)
    return payload


def save_registry(path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    registry_path = Path(path)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def update_candidate(
    path: str | Path,
    *,
    model_path: str,
    model_type: str,
    feature_version: str,
    train_rows: int,
    validation_rows: int,
    metrics: dict[str, Any],
    notes: str = "",
) -> dict[str, Any]:
    registry = load_registry(path)
    registry["candidate_model"] = {
        "path": model_path,
        "created_at": _utc_now(),
        "model_type": model_type,
        "feature_version": feature_version,
        "train_rows": train_rows,
        "validation_rows": validation_rows,
        "metrics": metrics,
        "notes": notes,
    }
    registry["model_type"] = model_type
    registry["feature_version"] = feature_version
    registry["train_rows"] = train_rows
    registry["validation_rows"] = validation_rows
    registry["metrics"] = metrics
    registry["notes"] = notes
    registry["promoted"] = False
    return save_registry(path, registry)


def promote_candidate(
    path: str | Path,
    *,
    current_model_path: str | Path,
    candidate_model_path: str | Path,
    notes: str = "",
) -> dict[str, Any]:
    registry = load_registry(path)
    candidate = registry.get("candidate_model")
    if not candidate:
        return registry

    current_path = Path(current_model_path)
    candidate_path = Path(candidate_model_path)
    current_path.parent.mkdir(parents=True, exist_ok=True)
    if candidate_path.exists():
        shutil.copy2(candidate_path, current_path)

    registry["previous_current_model"] = registry.get("current_model")
    registry["current_model"] = {
        **candidate,
        "path": str(current_path),
        "promoted_at": _utc_now(),
        "notes": notes or candidate.get("notes") or "",
    }
    registry["candidate_model"] = None
    registry["promoted"] = True
    registry["notes"] = notes or registry.get("notes") or ""
    return save_registry(path, registry)


def rollback_candidate(path: str | Path, *, notes: str = "") -> dict[str, Any]:
    registry = load_registry(path)
    registry["candidate_model"] = None
    registry["promoted"] = False
    if notes:
        registry["notes"] = notes
    return save_registry(path, registry)


def rollback_current(path: str | Path, *, notes: str = "") -> dict[str, Any]:
    registry = load_registry(path)
    previous_current = registry.get("previous_current_model")
    if previous_current:
        registry["current_model"] = previous_current
    registry["promoted"] = False
    if notes:
        registry["notes"] = notes
    return save_registry(path, registry)
