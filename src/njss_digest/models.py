"""案件の正規化表現。

CSV由来の情報(Case)と、公告文の取得結果(Notice)、判定結果(Verdict)を分けている。
入口(NJSSのCSV)を将来差し替えても Case から先は変わらないようにするための境界。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

# 入札参加資格の等級。「…建設・土木・工事系 A」のような末尾1文字を拾う。
_GRADE_RE = re.compile(r"(?:^|[\s　])([A-Da-d])\s*$")


@dataclass(frozen=True, slots=True)
class Case:
    """NJSSのCSV 1行 = 入札案件1件。"""

    case_id: str
    name: str
    url: str

    condition_name: str | None = None  # ヒットしたNJSS検索条件の登録名
    bid_type: str | None = None
    agency: str | None = None
    agency_pref: str | None = None
    delivery_place: list[str] = field(default_factory=list)
    announced_on: date | None = None
    deadline_on: date | None = None
    briefing_on: date | None = None
    docs_due_on: date | None = None
    qualification: list[str] = field(default_factory=list)
    industry: list[str] = field(default_factory=list)
    summary: str | None = None
    note: str | None = None
    price_text: str | None = None
    cert: str | None = None

    # マッピング対象外の列も捨てずに保持する（後から使いたくなったときCSVを取り直さずに済む）
    extra: dict[str, str] = field(default_factory=dict)
    source_file: str | None = None

    @property
    def grades(self) -> set[str]:
        """入札参加資格から等級(A〜D)を抽出する。

        例: 「広島市競争入札参加資格 建設・土木・工事系 A」-> {"A"}
        等級表記が無い資格しか無ければ空集合。
        """
        out: set[str] = set()
        for q in self.qualification:
            m = _GRADE_RE.search(q.strip())
            if m:
                out.add(m.group(1).upper())
        return out

    @property
    def has_summary(self) -> bool:
        """案件概要に実質的な中身があるか。

        「公告の日から配布する設計図書のとおり」のような、内容を持たない定型句を除く。
        中身が無い案件は公告文の外部取得(L3/L4)が必要になる。
        """
        s = (self.summary or "").strip()
        if len(s) < 20:
            return False
        placeholders = ("設計図書のとおり", "仕様書のとおり", "公告のとおり", "別紙のとおり")
        return not any(p in s for p in placeholders)

    def days_until_deadline(self, today: date) -> int | None:
        if self.deadline_on is None:
            return None
        return (self.deadline_on - today).days


@dataclass(frozen=True, slots=True)
class Notice:
    """外部から取得した公告情報（官公需API または Web検索）。"""

    case_id: str
    source: Literal["kkj", "websearch", "manual_pdf"]
    text: str  # 公告文全文
    source_url: str | None = None
    attachments: list[dict[str, str]] = field(default_factory=list)  # {name, uri}
    matched_by: Literal["exact", "fuzzy", "websearch", "manual"] | None = None
    match_score: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


Verdict = Literal["採用", "要確認", "除外", "資料未取得"]


@dataclass(frozen=True, slots=True)
class Judgement:
    """1案件の最終判定。"""

    case_id: str
    verdict: Verdict
    score: int = 0
    reason: str = ""
    # 公告文から抽出した項目。値と、その根拠となる原文の引用をペアで持つ。
    extracted: dict[str, str] = field(default_factory=dict)
    evidence: dict[str, str] = field(default_factory=dict)
    source: Literal["rules", "llm", "none"] = "none"
    source_url: str | None = None
