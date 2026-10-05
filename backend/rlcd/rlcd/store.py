"""Durable state under the artifacts directory (env RLCD_HOME, default backend/rlcd/var):

    registry.json            versions -> which artifact backs which head, plus holdout summaries
    models/<version>/*.joblib   one artifact per trained model (joblib; loaded only from inside RLCD_HOME)
    feedback.jsonl           scored outcomes and corrections posted to /v1/feedback (the reinforcement loop)
    decisions.jsonl          what /v1/decide answered, so feedback can refer to a decision_id
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import joblib

from . import LATEST, MODEL_PREFIX

DEFAULT_HOME = Path(__file__).resolve().parent.parent / "var"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def home_dir() -> Path:
    return Path(os.environ.get("RLCD_HOME") or DEFAULT_HOME).expanduser().resolve()


class UnknownModel(Exception):
    pass


class Store:
    def __init__(self, home: Optional[Path] = None):
        self.home = Path(home).expanduser().resolve() if home else home_dir()
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "models").mkdir(exist_ok=True)
        self._lock = threading.RLock()
        self._cache: dict[str, Any] = {}
        from .bernoulli import Ledger
        self.ledger = Ledger(self.home / "bernoulli.json")

    # ------------------------------------------------------------------ registry
    @property
    def _registry_path(self) -> Path:
        return self.home / "registry.json"

    def registry(self) -> dict:
        with self._lock:
            if self._registry_path.exists():
                return json.loads(self._registry_path.read_text())
            return {"latest": None, "next": 1, "versions": {}}

    def _write_registry(self, reg: dict) -> None:
        tmp = self._registry_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(reg, indent=1))
        tmp.replace(self._registry_path)

    def latest_id(self) -> str:
        """The versioned id `rlcd-latest` points at. `rlcd-0.1.0` is the untrained service."""
        return self.registry()["latest"] or f"{MODEL_PREFIX}0"

    def resolve(self, model: Optional[str]) -> str:
        """Alias or versioned id -> versioned id. Raises UnknownModel."""
        reg = self.registry()
        latest = reg["latest"] or f"{MODEL_PREFIX}0"
        if model in (None, "", LATEST, latest):
            return latest
        if model in reg["versions"]:
            return model
        raise UnknownModel(model)

    def version(self, version_id: str) -> dict:
        return self.registry()["versions"].get(version_id) or {"trained_at": None, "artifacts": {}, "heads": {}}

    def commit(self, artifacts: dict[str, dict], notes: Optional[list[str]] = None) -> str:
        """Save freshly trained artifacts as a new version. Heads that were not retrained are carried over
        from the previous version, so a version always describes the whole model."""
        with self._lock:
            reg = self.registry()
            n = int(reg["next"])
            vid = f"{MODEL_PREFIX}{n}"
            prev = reg["versions"].get(reg["latest"] or "", {})
            entry = {"trained_at": now_iso(), "artifacts": dict(prev.get("artifacts", {})),
                     "heads": dict(prev.get("heads", {})), "retrained": sorted(artifacts), "notes": notes or []}
            vdir = self.home / "models" / vid
            vdir.mkdir(parents=True, exist_ok=True)
            for key, art in artifacts.items():
                rel = f"models/{vid}/{key.replace(':', '_')}.joblib"
                art = {**art, "model_version": vid}
                joblib.dump(art, self.home / rel)
                entry["artifacts"][key] = rel
                entry["heads"][key] = {"trained_at": art.get("trained_at"), "n_train": art.get("n_train"),
                                       "metrics": art.get("metrics", {}), "model_version": vid}
                if art.get("spec"):   # a head discovered from the data: its description travels with it
                    entry["heads"][key]["spec"] = art["spec"]
            reg["versions"][vid] = entry
            reg["latest"], reg["next"] = vid, n + 1
            self._write_registry(reg)
            return vid

    def artifact(self, version_id: str, key: str) -> Optional[dict]:
        """The trained artifact backing `key` in a version, or None if that head is untrained."""
        rel = self.version(version_id).get("artifacts", {}).get(key)
        if not rel:
            return None
        path = (self.home / rel).resolve()
        if not path.is_relative_to(self.home):  # registry.json is data: never load a pickle from elsewhere
            raise RuntimeError(f"artifact path escapes RLCD_HOME: {rel}")
        with self._lock:
            if rel not in self._cache:
                if not path.exists():
                    return None
                self._cache[rel] = joblib.load(path)
            return self._cache[rel]

    # ------------------------------------------------------------------ jsonl logs
    def _append(self, name: str, row: dict) -> None:
        with self._lock, open(self.home / name, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")

    def _read(self, name: str) -> Iterator[dict]:
        path = self.home / name
        if not path.exists():
            return
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue  # a torn last line must not break training

    def add_feedback(self, row: dict) -> dict:
        row = {"id": uuid.uuid4().hex, "received_at": now_iso(), **row}
        self._append("feedback.jsonl", row)
        return row

    def feedback(self, head: Optional[str] = None) -> list[dict]:
        return [r for r in self._read("feedback.jsonl") if head is None or r.get("head") == head]

    def feedback_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self._read("feedback.jsonl"):
            out[r.get("head", "?")] = out.get(r.get("head", "?"), 0) + 1
        return out

    def add_decision(self, row: dict) -> str:
        did = uuid.uuid4().hex
        self._append("decisions.jsonl", {"decision_id": did, "created_at": now_iso(), **row})
        return did

    def decision(self, decision_id: str) -> Optional[dict]:
        found = None
        for r in self._read("decisions.jsonl"):
            if r.get("decision_id") == decision_id:
                found = r
        return found
