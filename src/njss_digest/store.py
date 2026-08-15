"""SQLiteによる永続化。

役割は3つ:
  1. 重複排除 — 同じ案件が複数の検索条件CSVに現れる/翌日も再ダウンロードされる
  2. 公告文キャッシュ — 一度取得した公告文を再取得しない（外部APIへの負荷とコストの削減）
  3. 判定履歴 — 後から突合精度や判定の妥当性を検証できるようにする
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .models import Case, Judgement, Notice

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id       TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    url           TEXT NOT NULL,
    agency        TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    source_file   TEXT,
    payload_json  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notices (
    case_id     TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    source_url  TEXT,
    matched_by  TEXT,
    match_score REAL,
    char_count  INTEGER NOT NULL,
    text_path   TEXT NOT NULL,
    attachments_json TEXT,
    raw_json    TEXT,
    fetched_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verdicts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id    TEXT NOT NULL,
    run_id     TEXT NOT NULL,
    verdict    TEXT NOT NULL,
    score      INTEGER NOT NULL,
    reason     TEXT,
    extracted_json TEXT,
    evidence_json  TEXT,
    source     TEXT,
    source_url TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verdicts_case ON verdicts(case_id);
CREATE INDEX IF NOT EXISTS idx_verdicts_run  ON verdicts(run_id);

CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    csv_files   TEXT,
    stats_json  TEXT
);
"""


def _json_default(o: Any) -> Any:
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    raise TypeError(f"JSONに変換できません: {type(o)}")


class Store:
    def __init__(self, db_path: Path, notices_dir: Path) -> None:
        self.db_path = db_path
        self.notices_dir = notices_dir
        db_path.parent.mkdir(parents=True, exist_ok=True)
        notices_dir.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.conn.commit()
        self.close()

    # ---- 案件 -------------------------------------------------------------

    def known_case_ids(self, case_ids: list[str]) -> set[str]:
        if not case_ids:
            return set()
        marks = ",".join("?" * len(case_ids))
        rows = self.conn.execute(
            f"SELECT case_id FROM cases WHERE case_id IN ({marks})", case_ids
        ).fetchall()
        return {r["case_id"] for r in rows}

    def upsert_case(self, case: Case) -> bool:
        """案件を登録する。初めて見た案件なら True を返す。"""
        now = datetime.now().isoformat(timespec="seconds")
        payload = json.dumps(asdict(case), ensure_ascii=False, default=_json_default)
        self.conn.execute(
            """
            INSERT INTO cases (case_id, name, url, agency, first_seen_at, last_seen_at,
                               source_file, payload_json)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(case_id) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                payload_json = excluded.payload_json
            """,
            (
                case.case_id,
                case.name,
                case.url,
                case.agency,
                now,
                now,
                case.source_file,
                payload,
            ),
        )
        # rowcount は INSERT/UPDATE どちらでも1になるため、first_seen で新規判定する
        row = self.conn.execute(
            "SELECT first_seen_at, last_seen_at FROM cases WHERE case_id = ?",
            (case.case_id,),
        ).fetchone()
        return bool(row and row["first_seen_at"] == row["last_seen_at"])

    # ---- 公告文 -----------------------------------------------------------

    def get_notice(self, case_id: str) -> Notice | None:
        row = self.conn.execute(
            "SELECT * FROM notices WHERE case_id = ?", (case_id,)
        ).fetchone()
        if row is None:
            return None
        text_path = Path(row["text_path"])
        text = text_path.read_text(encoding="utf-8") if text_path.is_file() else ""
        return Notice(
            case_id=row["case_id"],
            source=row["source"],
            text=text,
            source_url=row["source_url"],
            attachments=json.loads(row["attachments_json"] or "[]"),
            matched_by=row["matched_by"],
            match_score=row["match_score"],
            raw=json.loads(row["raw_json"] or "{}"),
        )

    def save_notice(self, notice: Notice) -> None:
        path = self.notices_dir / f"{notice.case_id}.txt"
        path.write_text(notice.text, encoding="utf-8")
        self.conn.execute(
            """
            INSERT INTO notices (case_id, source, source_url, matched_by, match_score,
                                 char_count, text_path, attachments_json, raw_json, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(case_id) DO UPDATE SET
                source=excluded.source, source_url=excluded.source_url,
                matched_by=excluded.matched_by, match_score=excluded.match_score,
                char_count=excluded.char_count, text_path=excluded.text_path,
                attachments_json=excluded.attachments_json, raw_json=excluded.raw_json,
                fetched_at=excluded.fetched_at
            """,
            (
                notice.case_id,
                notice.source,
                notice.source_url,
                notice.matched_by,
                notice.match_score,
                len(notice.text),
                str(path),
                json.dumps(notice.attachments, ensure_ascii=False),
                json.dumps(notice.raw, ensure_ascii=False),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )

    # ---- 判定 -------------------------------------------------------------

    def save_verdict(self, run_id: str, j: Judgement) -> None:
        self.conn.execute(
            """
            INSERT INTO verdicts (case_id, run_id, verdict, score, reason,
                                  extracted_json, evidence_json, source, source_url, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                j.case_id,
                run_id,
                j.verdict,
                j.score,
                j.reason,
                json.dumps(j.extracted, ensure_ascii=False),
                json.dumps(j.evidence, ensure_ascii=False),
                j.source,
                j.source_url,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )

    # ---- 実行 -------------------------------------------------------------

    def start_run(self, run_id: str, csv_files: list[str]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO runs (run_id, started_at, csv_files) VALUES (?,?,?)",
            (
                run_id,
                datetime.now().isoformat(timespec="seconds"),
                json.dumps(csv_files, ensure_ascii=False),
            ),
        )
        self.conn.commit()

    def finish_run(self, run_id: str, stats: dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, stats_json = ? WHERE run_id = ?",
            (
                datetime.now().isoformat(timespec="seconds"),
                json.dumps(stats, ensure_ascii=False, default=_json_default),
                run_id,
            ),
        )
        self.conn.commit()
