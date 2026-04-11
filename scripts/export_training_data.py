from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401

from app.config.settings import get_settings


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


def export_dataset(output_path: Path) -> int:
    settings = get_settings()
    outcomes_path = Path(settings.log_dir) / "outcomes.jsonl"
    rows = _load_jsonl(outcomes_path)
    dataset_rows = []
    for row in rows:
        feature_snapshot = row.get("feature_snapshot")
        if not isinstance(feature_snapshot, dict):
            continue
        if feature_snapshot.get("label") not in {0, 1}:
            continue
        dataset_rows.append(feature_snapshot)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for row in dataset_rows:
            handle.write(json.dumps(row, default=str))
            handle.write("\n")

    print(f"exported_rows={len(dataset_rows)} source={outcomes_path} output={output_path}")
    if not dataset_rows:
        print("No labeled outcome rows were available. This is safe; nightly retrain can skip.")
    return len(dataset_rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export outcome logs into a JSONL training dataset.")
    parser.add_argument("--output", default="models/training_data.jsonl", help="Output JSONL dataset path.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    export_dataset(Path(args.output))


if __name__ == "__main__":
    main()
