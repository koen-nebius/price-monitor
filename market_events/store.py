"""Append-only market-event state, review audit, and raw-document storage."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .models import CONFIDENCE_VALUES, MarketEvent, event_from_dict, stable_hash
from .tavily import RawDocument


class AppendOnlyStore:
    def __init__(
        self,
        state_dir: str | Path,
        *,
        raw_dir: str | Path | None = None,
    ):
        self.state_dir = Path(state_dir)
        self.events_path = self.state_dir / "events.jsonl"
        self.changes_path = self.state_dir / "changes.jsonl"
        self.coverage_path = self.state_dir / "coverage_history.jsonl"
        self.reviews_path = self.state_dir / "reviews.jsonl"
        self.raw_dir = Path(raw_dir) if raw_dir is not None else self.state_dir
        self.raw_path = self.raw_dir / "raw_documents.jsonl"

    @staticmethod
    def _read(path: Path) -> list[dict]:
        if not path.exists():
            return []
        with path.open() as handle:
            return [json.loads(line) for line in handle if line.strip()]

    @staticmethod
    def _append(path: Path, record: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")

    def append_raw(self, document: RawDocument) -> bool:
        record = document.to_dict()
        identity = (record["source_id"], record["url"], record["raw_document_hash"])
        existing = {
            (row["source_id"], row["url"], row["raw_document_hash"])
            for row in self._read(self.raw_path)
        }
        if identity in existing:
            return False
        self._append(self.raw_path, record)
        return True

    def record(self, event: MarketEvent) -> dict:
        event.validate()
        data = event.to_dict()
        versions = [
            row for row in self._read(self.events_path) if row["semantic_key"] == data["semantic_key"]
        ]
        previous = versions[-1] if versions else None
        now = datetime.now(timezone.utc).isoformat()
        if previous and previous["content_hash"] == data["content_hash"]:
            change = {
                "change_id": stable_hash(
                    {
                        "semantic_key": data["semantic_key"],
                        "content_hash": data["content_hash"],
                        "recorded_at": now,
                    }
                )[:24],
                "recorded_at": now,
                "semantic_key": data["semantic_key"],
                "change_type": "unchanged",
                "from_version_id": previous["version_id"],
                "to_version_id": previous["version_id"],
                "changed_fields": [],
            }
            self._append(self.changes_path, change)
            return change

        version_number = len(versions) + 1
        version_id = stable_hash(
            {
                "semantic_key": data["semantic_key"],
                "content_hash": data["content_hash"],
                "version_number": version_number,
            }
        )[:24]
        version = {
            **data,
            "version_id": version_id,
            "version_number": version_number,
            "supersedes_version_id": previous["version_id"] if previous else "",
            "recorded_at": now,
        }
        self._append(self.events_path, version)

        ignored = {
            "semantic_key",
            "content_hash",
            "version_id",
            "version_number",
            "supersedes_version_id",
            "recorded_at",
            "observed_at",
            "raw_document_hash",
        }
        changed_fields = []
        if previous:
            keys = (set(previous) | set(version)) - ignored
            changed_fields = sorted(key for key in keys if previous.get(key) != version.get(key))
        change = {
            "change_id": stable_hash({"version_id": version_id, "recorded_at": now})[:24],
            "recorded_at": now,
            "semantic_key": data["semantic_key"],
            "change_type": "changed" if previous else "new",
            "from_version_id": previous["version_id"] if previous else "",
            "to_version_id": version_id,
            "changed_fields": changed_fields,
        }
        self._append(self.changes_path, change)
        return change

    def versions(self) -> list[dict]:
        return self._read(self.events_path)

    def changes(self) -> list[dict]:
        return self._read(self.changes_path)

    def coverage_history(self) -> list[dict]:
        return self._read(self.coverage_path)

    def reviews(self) -> list[dict]:
        return self._read(self.reviews_path)

    def current_version_rows(self, *, include_health: bool = True) -> list[dict]:
        latest: dict[str, dict] = {}
        for row in self._read(self.events_path):
            latest[row["semantic_key"]] = row
        rows = sorted(latest.values(), key=lambda row: row["semantic_key"])
        if not include_health:
            rows = [row for row in rows if row["event_type"] != "source_health"]
        return rows

    def current(self, *, include_health: bool = True) -> list[MarketEvent]:
        return [
            event_from_dict(row)
            for row in self.current_version_rows(include_health=include_health)
        ]

    def append_coverage_snapshot(
        self,
        *,
        run_id: str,
        mode: str,
        metrics: list[dict],
    ) -> dict:
        """Persist one immutable, applicability-aware coverage snapshot per run."""

        if mode not in {"daily", "weekly"}:
            raise ValueError("coverage mode must be daily or weekly")
        if not run_id.strip():
            raise ValueError("coverage run_id is required")
        existing = {
            row["run_id"] for row in self._read(self.coverage_path) if "run_id" in row
        }
        if run_id in existing:
            raise ValueError(f"coverage snapshot already exists for run_id: {run_id}")
        record = {
            "run_id": run_id,
            "mode": mode,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "metrics": metrics,
            "metrics_hash": stable_hash(metrics),
        }
        self._append(self.coverage_path, record)
        return record

    def export_review_queue(self, output_path: str | Path) -> int:
        """Export current candidates without copying full raw page bodies."""

        rows = [
            row
            for row in self.current_version_rows(include_health=False)
            if row["review_status"] == "candidate"
        ]
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        with temporary.open("w") as handle:
            for row in rows:
                generated_fields = {
                    "semantic_key",
                    "content_hash",
                    "version_id",
                    "version_number",
                    "supersedes_version_id",
                    "recorded_at",
                }
                queue_row = {
                    "semantic_key": row["semantic_key"],
                    "expected_version_id": row["version_id"],
                    "event_type": row["event_type"],
                    "provider": row["provider"],
                    "source_id": row["source_id"],
                    "source_url": row["source_url"],
                    "source_title": row["source_title"],
                    "event_date": row["event_date"],
                    "region_scope": row["region_scope"],
                    "gpu_model": row["gpu_model"],
                    "confidence": row["confidence"],
                    "evidence_text": row["evidence_text"],
                    "raw_document_hash": row["raw_document_hash"],
                    "candidate": {
                        key: value
                        for key, value in row.items()
                        if key not in generated_fields
                    },
                }
                handle.write(
                    json.dumps(queue_row, sort_keys=True, ensure_ascii=False) + "\n"
                )
        temporary.replace(output)
        return len(rows)

    def promote_candidate(
        self,
        *,
        semantic_key: str,
        expected_version_id: str,
        reviewer: str,
        review_note: str,
        confidence: str | None = None,
    ) -> dict:
        """Append a verified event version plus an immutable human-review record."""

        if not reviewer.strip():
            raise ValueError("reviewer is required")
        if not review_note.strip():
            raise ValueError("review_note is required")
        if confidence is not None and confidence not in CONFIDENCE_VALUES:
            raise ValueError(f"confidence must be one of {sorted(CONFIDENCE_VALUES)}")
        current = {
            row["semantic_key"]: row
            for row in self.current_version_rows(include_health=False)
        }.get(semantic_key)
        if current is None:
            raise KeyError(f"unknown semantic_key: {semantic_key}")
        if current["version_id"] != expected_version_id:
            raise ValueError(
                "candidate version changed after queue export; refresh the review queue"
            )
        if current["review_status"] != "candidate":
            raise ValueError("only a current candidate can be promoted")

        candidate = event_from_dict(current)
        verified = replace(
            candidate,
            review_status="verified",
            confidence=confidence or candidate.confidence,
            extraction_method=f"{candidate.extraction_method}+human_verified",
        )
        change = self.record(verified)
        reviewed_at = datetime.now(timezone.utc).isoformat()
        review = {
            "review_id": stable_hash(
                {
                    "semantic_key": semantic_key,
                    "candidate_version_id": expected_version_id,
                    "verified_version_id": change["to_version_id"],
                    "reviewer": reviewer,
                    "reviewed_at": reviewed_at,
                }
            )[:24],
            "reviewed_at": reviewed_at,
            "semantic_key": semantic_key,
            "candidate_version_id": expected_version_id,
            "verified_version_id": change["to_version_id"],
            "decision": "verified",
            "reviewer": reviewer,
            "review_note": review_note,
        }
        self._append(self.reviews_path, review)
        return {"change": change, "review": review}

    def raw_documents(self) -> list[dict]:
        return self._read(self.raw_path)
