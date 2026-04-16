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
        "models": {
            "entry": {
                "current_model": None,
                "candidate_model": None,
                "previous_current_model": None,
            },
            "exit": {
                "current_model": None,
                "candidate_model": None,
                "previous_current_model": None,
            },
        },
        "model_type": None,
        "requested_model_type": None,
        "base_estimator_class": None,
        "model_selection": {},
        "feature_version": FEATURE_VERSION,
        "train_rows": 0,
        "validation_rows": 0,
        "metrics": {},
        "trading_metrics": {},
        "evaluation": {},
        "notes": "",
    }


def _ensure_model_sections(payload: dict[str, Any]) -> dict[str, Any]:
    payload.setdefault("models", {})
    for purpose in ("entry", "exit"):
        payload["models"].setdefault(
            purpose,
            {
                "current_model": None,
                "candidate_model": None,
                "previous_current_model": None,
            },
        )
    if payload.get("current_model") is not None:
        payload["models"]["entry"]["current_model"] = payload.get("current_model")
    if payload.get("candidate_model") is not None:
        payload["models"]["entry"]["candidate_model"] = payload.get("candidate_model")
    if payload.get("previous_current_model") is not None:
        payload["models"]["entry"]["previous_current_model"] = payload.get("previous_current_model")
    return payload


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
    return _ensure_model_sections(payload)


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
    trading_metrics: dict[str, Any] | None = None,
    notes: str = "",
    model_purpose: str = "entry",
    requested_model_type: str | None = None,
    base_estimator_class: str | None = None,
    model_selection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    registry = load_registry(path)
    candidate_payload = {
        "path": model_path,
        "created_at": _utc_now(),
        "model_type": model_type,
        "feature_version": feature_version,
        "train_rows": train_rows,
        "validation_rows": validation_rows,
        "metrics": metrics,
        "trading_metrics": trading_metrics or {},
        "notes": notes,
    }
    if requested_model_type is not None:
        candidate_payload["requested_model_type"] = requested_model_type
    if base_estimator_class is not None:
        candidate_payload["base_estimator_class"] = base_estimator_class
    if model_selection is not None:
        candidate_payload["model_selection"] = model_selection
    purpose = model_purpose.strip().lower()
    registry["models"][purpose]["candidate_model"] = candidate_payload
    if purpose == "entry":
        registry["candidate_model"] = candidate_payload
        registry["model_type"] = model_type
        registry["requested_model_type"] = requested_model_type or model_type
        registry["base_estimator_class"] = base_estimator_class
        registry["model_selection"] = model_selection or {}
        registry["feature_version"] = feature_version
        registry["train_rows"] = train_rows
        registry["validation_rows"] = validation_rows
        registry["metrics"] = metrics
        registry["trading_metrics"] = trading_metrics or {}
        registry["notes"] = notes
    registry["promoted"] = False
    return save_registry(path, registry)


def promote_candidate(
    path: str | Path,
    *,
    current_model_path: str | Path,
    candidate_model_path: str | Path,
    notes: str = "",
    model_purpose: str = "entry",
) -> dict[str, Any]:
    registry = load_registry(path)
    purpose = model_purpose.strip().lower()
    candidate = registry["models"].get(purpose, {}).get("candidate_model")
    if not candidate:
        return registry

    current_path = Path(current_model_path)
    candidate_path = Path(candidate_model_path)
    current_path.parent.mkdir(parents=True, exist_ok=True)
    if candidate_path.exists():
        shutil.copy2(candidate_path, current_path)

    promoted_payload = {
        **candidate,
        "path": str(current_path),
        "promoted_at": _utc_now(),
        "notes": notes or candidate.get("notes") or "",
    }
    registry["models"][purpose]["previous_current_model"] = registry["models"].get(purpose, {}).get("current_model")
    registry["models"][purpose]["current_model"] = promoted_payload
    registry["models"][purpose]["candidate_model"] = None
    if purpose == "entry":
        registry["previous_current_model"] = registry.get("current_model")
        registry["current_model"] = promoted_payload
        registry["candidate_model"] = None
        registry["model_type"] = promoted_payload.get("model_type")
        registry["requested_model_type"] = promoted_payload.get("requested_model_type") or promoted_payload.get("model_type")
        registry["base_estimator_class"] = promoted_payload.get("base_estimator_class")
        registry["model_selection"] = promoted_payload.get("model_selection") or {}
    registry["promoted"] = True
    registry["notes"] = notes or registry.get("notes") or ""
    return save_registry(path, registry)


def rollback_candidate(path: str | Path, *, notes: str = "", model_purpose: str | None = None) -> dict[str, Any]:
    registry = load_registry(path)
    if model_purpose is None:
        registry["candidate_model"] = None
        registry["models"]["entry"]["candidate_model"] = None
        registry["models"]["exit"]["candidate_model"] = None
    else:
        purpose = model_purpose.strip().lower()
        registry["models"][purpose]["candidate_model"] = None
        if purpose == "entry":
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
        registry["models"]["entry"]["current_model"] = previous_current
    registry["promoted"] = False
    if notes:
        registry["notes"] = notes
    return save_registry(path, registry)
