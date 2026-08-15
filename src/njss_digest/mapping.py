"""列マッピングの適用と自己診断。

NJSSのCSVにはヘッダ行が無いため、列の意味は「位置」でしか特定できない。
NJSS側が列順を変更した場合、素朴な実装は「壊れたことに気づかないまま
別の列を案件名として読む」という最悪の失敗をする。

これを防ぐため、読み込みのたびに以下を検証し、失敗したら例外で停止する:
  - 案件IDが案件詳細URLの末尾の数値と一致すること（位置マッピング全体の妥当性）
  - URLのホストがNJSSであること
  - 公示日 <= 締切日 の関係が成り立つこと
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from .ingest import RawCsv
from .models import Case

log = logging.getLogger(__name__)

_URL_TAIL_DIGITS = re.compile(r"/(\d+)/?$")


class MappingError(Exception):
    """列マッピングが実データと整合しない。"""


@dataclass(frozen=True, slots=True)
class ColumnSpec:
    index: int
    field: str
    label: str
    multi: bool = False
    type: str | None = None


@dataclass(frozen=True, slots=True)
class Mapping:
    columns: list[ColumnSpec]
    encoding_candidates: list[str]
    has_header: bool
    expected_columns: int | None
    strict_column_count: bool
    date_formats: list[str]
    null_markers: set[str]
    required_fields: list[str]
    validators: list[dict[str, Any]]

    @property
    def mapped_indices(self) -> set[int]:
        return {c.index for c in self.columns}


def load_mapping(path: Path) -> Mapping:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    fmt = data.get("format", {})
    cols = [
        ColumnSpec(
            index=c["index"],
            field=c["field"],
            label=c.get("label", ""),
            multi=bool(c.get("multi", False)),
            type=c.get("type"),
        )
        for c in data.get("columns", [])
        if c.get("field")
    ]
    if not cols:
        raise MappingError(f"columns が空です: {path}")

    return Mapping(
        columns=cols,
        encoding_candidates=list(fmt.get("encoding_candidates", ["cp932", "utf-8-sig", "utf-8"])),
        has_header=bool(fmt.get("has_header", False)),
        expected_columns=fmt.get("expected_columns"),
        strict_column_count=bool(fmt.get("strict_column_count", False)),
        date_formats=list(data.get("date_formats", ["%Y/%m/%d", "%Y-%m-%d"])),
        null_markers={s.strip() for s in data.get("null_markers", [])},
        required_fields=list(data.get("required_fields", [])),
        validators=list(data.get("validate", [])),
    )


def _clean(value: str, null_markers: set[str]) -> str:
    """ゼロ幅文字・BOM・制御文字を除去して前後を整える。

    実データの案件名に U+200B(ZERO WIDTH SPACE) の混入を確認しているため、
    取り込み時点で落としておく（後段の突合で表記ゆれの原因になる）。
    """
    v = value.replace("​", "").replace("﻿", "").replace(" ", " ")
    v = "".join(ch for ch in v if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    v = v.strip()
    return "" if v in null_markers else v


def _parse_date(value: str, formats: list[str]) -> date | None:
    for f in formats:
        try:
            return datetime.strptime(value, f).date()
        except ValueError:
            continue
    return None


@dataclass
class MappingReport:
    """自己診断の結果。人が読んで列マッピングの妥当性を判断するためのもの。"""

    rows: int = 0
    columns_seen: set[int] = field(default_factory=set)
    unmapped_nonempty: dict[int, str] = field(default_factory=dict)
    date_parse_failures: dict[str, int] = field(default_factory=dict)
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(passed for _, passed, _ in self.checks)


def to_cases(raw: RawCsv, m: Mapping) -> tuple[list[Case], MappingReport]:
    """CSVの行を Case に変換し、あわせて自己診断を行う。

    検証に失敗した場合は MappingError を送出する（空データで正常終了させない）。
    """
    report = MappingReport(rows=raw.row_count)

    if m.expected_columns is not None:
        actual = raw.column_counts
        if actual != {m.expected_columns}:
            msg = (
                f"列数が想定と異なります: 想定={m.expected_columns} 実際={sorted(actual)}. "
                "NJSSがCSVの形式を変更した可能性があります。"
            )
            if m.strict_column_count:
                raise MappingError(msg)
            log.warning("%s 既知の列のみ読み込みます。", msg)

    by_index = {c.index: c for c in m.columns}
    cases: list[Case] = []

    for row_no, row in enumerate(raw.rows, start=1):
        values: dict[str, Any] = {}
        extra: dict[str, str] = {}

        for i, cell in enumerate(row):
            report.columns_seen.add(i)
            v = _clean(cell, m.null_markers)
            spec = by_index.get(i)

            if spec is None:
                if v:
                    extra[f"col{i}"] = v
                    report.unmapped_nonempty.setdefault(i, v[:60])
                continue

            if not v:
                continue

            if spec.type == "date":
                d = _parse_date(v, m.date_formats)
                if d is None:
                    report.date_parse_failures[spec.field] = (
                        report.date_parse_failures.get(spec.field, 0) + 1
                    )
                    log.warning("行%d: %s を日付として解釈できません: %r", row_no, spec.label, v)
                    continue
                values[spec.field] = d
            elif spec.multi:
                values[spec.field] = [s.strip() for s in v.split("\n") if s.strip()]
            else:
                values[spec.field] = v

        missing = [f for f in m.required_fields if not values.get(f)]
        if missing:
            raise MappingError(
                f"行{row_no}: 必須フィールドが取得できません: {missing}\n"
                f"  行の内容(先頭5列): {row[:5]}\n"
                "  列の位置がずれている可能性があります。"
                "`njss-digest inspect <CSV>` で列構成を確認し、"
                "config/csv_mapping.yaml を修正してください。"
            )

        cases.append(Case(extra=extra, source_file=raw.path.name, **values))

    _run_validators(cases, m, report)

    failed = [(n, d) for n, passed, d in report.checks if not passed]
    if failed:
        detail = "\n".join(f"  - {n}: {d}" for n, d in failed)
        raise MappingError(
            "列マッピングの自己診断に失敗しました。CSVの形式が変わった可能性があります。\n"
            f"{detail}\n"
            "`njss-digest inspect <CSV>` で実際の列構成を確認してください。"
        )

    return cases, report


def _run_validators(cases: list[Case], m: Mapping, report: MappingReport) -> None:
    for v in m.validators:
        kind = v.get("type")

        if kind == "case_id_matches_url_tail":
            bad = []
            for c in cases:
                tail = _URL_TAIL_DIGITS.search(c.url)
                if not tail or tail.group(1) != c.case_id:
                    bad.append(f"{c.case_id!r} vs {c.url!r}")
            report.checks.append(
                (
                    "案件IDとURL末尾の一致",
                    not bad,
                    "全件一致" if not bad else f"{len(bad)}件不一致: {bad[:3]}",
                )
            )

        elif kind == "url_host_is":
            want = v.get("value", "")
            bad = [c.url for c in cases if f"//{want}/" not in c.url]
            report.checks.append(
                (
                    f"URLホストが {want}",
                    not bad,
                    "全件一致" if not bad else f"{len(bad)}件不一致: {bad[:3]}",
                )
            )

        elif kind == "date_order":
            e_name, l_name = v.get("earlier"), v.get("later")
            bad = []
            for c in cases:
                e, l = getattr(c, e_name, None), getattr(c, l_name, None)
                if e and l and e > l:
                    bad.append(f"{c.case_id}: {e} > {l}")
            report.checks.append(
                (
                    f"{e_name} <= {l_name}",
                    not bad,
                    "整合" if not bad else f"{len(bad)}件が逆転: {bad[:3]}",
                )
            )

        else:
            log.warning("未知のバリデータをスキップします: %r", kind)
