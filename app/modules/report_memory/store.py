"""Report memory: questions that produced reports, their plans and how well they worked.

One row per distinct question (normalised) and scope shape. Outcomes and user feedback move its score:
a report generated, saved, exported or liked raises it; a failure or dislike lowers it. Only rows with a
positive score are reused, as an exact plan (when the plan is ``cacheable``) or as examples for the
planner. Questions arrive already privacy-filtered; plans hold field names and filter values, no records.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import numpy as np

from .embed import Embedder, lexical_vector, normalize, similarity

# Outcome or feedback signal -> score change. Each signal counts once per report.
SCORES = {"generated": 1.0, "failed": -3.0, "clarification": -1.0, "saved": 2.0, "exported": 2.0,
          "shared": 2.0, "rerun": 1.0, "up": 3.0, "down": -5.0,
          # Star rating asked when the user leaves the report: one star stops reuse at once.
          "rating:1": -5.0, "rating:2": -3.0, "rating:3": 0.0, "rating:4": 2.0, "rating:5": 3.0}
# The user's verdict on a report: a new one replaces the previous one instead of adding to it.
VERDICTS = frozenset({"up", "down", "rating:1", "rating:2", "rating:3", "rating:4", "rating:5"})
MIN_REUSE_SCORE = 1.0


class MemoryStore:
    def __init__(self, path: Path, embedder: Embedder, min_similarity: float = 0.45) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.embedder, self.min_similarity = embedder, min_similarity
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS examples (
                id TEXT PRIMARY KEY, norm TEXT NOT NULL, scope_key TEXT NOT NULL, prompt TEXT NOT NULL,
                plan TEXT NOT NULL, cacheable INTEGER NOT NULL, score REAL NOT NULL, uses INTEGER NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, lex BLOB NOT NULL, sem BLOB,
                UNIQUE (norm, scope_key));
            CREATE TABLE IF NOT EXISTS reports (
                report_id TEXT PRIMARY KEY, example_id TEXT NOT NULL, signals TEXT NOT NULL);
        """)
        self._vectors: dict[str, tuple[np.ndarray, np.ndarray | None]] = {
            row[0]: (np.frombuffer(row[1], dtype=np.float32), np.frombuffer(row[2], dtype=np.float32) if row[2] else None)
            for row in self._db.execute("SELECT id, lex, sem FROM examples")}

    def close(self) -> None:
        self._db.close()

    def _vectors_for(self, prompt: str):
        return lexical_vector(prompt), self.embedder.semantic_vector(prompt)

    def record(self, prompt: str, scope_key: str, plan: dict[str, Any], report_id: str | None, outcome: str,
               cacheable: bool) -> str:
        norm, now = normalize(prompt), time.time()
        lex, sem = self._vectors_for(prompt)
        with self._lock, self._db:
            row = self._db.execute("SELECT id, score FROM examples WHERE norm = ? AND scope_key = ?",
                                   (norm, scope_key)).fetchone()
            if row:
                example_id = row[0]
                updates = {"score": row[1] + SCORES[outcome], "updated": now}
                if outcome == "generated":
                    updates.update(plan=json.dumps(plan, ensure_ascii=False), cacheable=int(cacheable), prompt=prompt)
                sets = ", ".join(f"{k} = ?" for k in updates)
                self._db.execute(f"UPDATE examples SET {sets}, uses = uses + 1 WHERE id = ?", (*updates.values(), example_id))
            else:
                example_id = uuid.uuid4().hex
                self._db.execute("INSERT INTO examples VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
                                 (example_id, norm, scope_key, prompt, json.dumps(plan, ensure_ascii=False),
                                  int(cacheable), SCORES[outcome], now, now, lex.tobytes(),
                                  sem.tobytes() if sem is not None else None))
                self._vectors[example_id] = (lex, sem)
            if report_id:
                self._db.execute("INSERT OR REPLACE INTO reports VALUES (?, ?, ?)",
                                 (report_id, example_id, json.dumps([outcome])))
        return example_id

    def feedback(self, report_id: str, signal: str) -> bool:
        """Apply ``signal`` to the question behind ``report_id``; each signal counts once per report."""
        with self._lock, self._db:
            row = self._db.execute("SELECT example_id, signals FROM reports WHERE report_id = ?", (report_id,)).fetchone()
            if not row:
                return False
            signals = json.loads(row[1])
            if signal in signals:
                return True
            # A like, dislike or star rating replaces the user's earlier verdict instead of adding to it.
            replaced = [s for s in signals if s in VERDICTS] if signal in VERDICTS else []
            delta = SCORES[signal] - sum(SCORES[s] for s in replaced)
            signals = [s for s in signals if s not in replaced] + [signal]
            self._db.execute("UPDATE examples SET score = score + ?, updated = ? WHERE id = ?",
                             (delta, time.time(), row[0]))
            self._db.execute("UPDATE reports SET signals = ? WHERE report_id = ?", (json.dumps(signals), report_id))
        return True

    def lookup(self, prompt: str, scope_key: str, k: int = 3) -> dict[str, Any]:
        norm = normalize(prompt)
        with self._lock:
            rows = self._db.execute("SELECT id, norm, scope_key, prompt, plan, cacheable, score FROM examples "
                                    "WHERE score >= ?", (MIN_REUSE_SCORE,)).fetchall()
        cached = next((json.loads(r[4]) for r in rows if r[1] == norm and r[2] == scope_key and r[5]), None)
        if not rows:
            return {"cached_plan": cached, "examples": []}
        lex, sem = self._vectors_for(prompt)
        scored = []
        for row in rows:
            if row[1] == norm and row[2] == scope_key:
                continue
            row_lex, row_sem = self._vectors[row[0]]
            value = similarity(lex, sem, row_lex, row_sem)
            if value >= self.min_similarity:
                scored.append((value, row))
        scored.sort(key=lambda item: (item[0], item[1][6]), reverse=True)
        examples = [{"prompt": r[3], "plan": json.loads(r[4]), "similarity": round(v, 3), "score": r[6]}
                    for v, r in scored[:k]]
        return {"cached_plan": cached, "examples": examples}

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total, reusable = self._db.execute(
                "SELECT COUNT(*), SUM(score >= ?) FROM examples", (MIN_REUSE_SCORE,)).fetchone()
            signals = [json.loads(r[0]) for r in self._db.execute("SELECT signals FROM reports")]
        stars = [int(s.split(":")[1]) for report in signals for s in report if s.startswith("rating:")]
        return {"questions": total, "reusable": reusable or 0, "semantic_model_loaded": self.embedder.semantic_ready,
                "ratings": len(stars), "average_rating": round(sum(stars) / len(stars), 2) if stars else None}
