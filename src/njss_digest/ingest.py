"""L0: CSVファイルの検出と読み込み。

NJSSの案件ダウンロードCSVは CP932・ヘッダ行なし。
文字コードの取り違えは静かにデータを壊すので、判定結果をログに残し、
判定できなかった場合は推測で続行せずエラーにする。
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


class IngestError(Exception):
    """CSVを読み込めなかった。"""


@dataclass(frozen=True, slots=True)
class RawCsv:
    path: Path
    encoding: str
    rows: list[list[str]]

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def column_counts(self) -> set[int]:
        return {len(r) for r in self.rows}


def detect_encoding(raw: bytes, candidates: list[str]) -> str:
    """候補を順に試し、最初にデコードできたものを返す。

    候補順が重要。CP932とUTF-8は多くのバイト列で相互に「読めてしまう」ことがあるが、
    UTF-8として不正なバイト列はCP932では有効なことが多いため、
    UTF-8を先に試して失敗したらCP932、という順序が安全側に働く。
    ただしNJSSのCSVはCP932固定なので設定ファイル側で cp932 を先頭に置いている。
    """
    for enc in candidates:
        try:
            raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        return enc
    raise IngestError(
        f"文字コードを判定できませんでした。試した候補: {candidates}. "
        "config/csv_mapping.yaml の format.encoding_candidates に追加してください。"
    )


def read_csv(path: Path, encoding_candidates: list[str], has_header: bool) -> RawCsv:
    raw = path.read_bytes()
    if not raw.strip():
        raise IngestError(f"ファイルが空です: {path}")

    encoding = detect_encoding(raw, encoding_candidates)
    text = raw.decode(encoding)
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]

    if has_header:
        if not rows:
            raise IngestError(f"ヘッダ行しかありません: {path}")
        rows = rows[1:]

    if not rows:
        raise IngestError(f"データ行がありません: {path}")

    log.info("読込 %s: encoding=%s rows=%d", path.name, encoding, len(rows))
    return RawCsv(path=path, encoding=encoding, rows=rows)


def find_csv_files(
    watch_dir: Path,
    *,
    name_pattern: str = "案件情報*.csv",
    processed_dir: Path | None = None,
    modified_within_days: int | None = None,
) -> list[Path]:
    """未処理のNJSS CSVを新しい順に返す。

    処理済みは processed_dir に移動される運用なので、
    watch_dir に残っているものを未処理とみなす。
    """
    if not watch_dir.is_dir():
        raise IngestError(f"監視ディレクトリがありません: {watch_dir}")

    processed_names = set()
    if processed_dir and processed_dir.is_dir():
        processed_names = {p.name for p in processed_dir.iterdir()}

    now = datetime.now().timestamp()
    out: list[Path] = []
    for p in watch_dir.glob(name_pattern):
        if not p.is_file() or p.name in processed_names:
            continue
        if modified_within_days is not None:
            age_days = (now - p.stat().st_mtime) / 86400
            if age_days > modified_within_days:
                continue
        out.append(p)

    return sorted(out, key=lambda p: p.stat().st_mtime, reverse=True)
