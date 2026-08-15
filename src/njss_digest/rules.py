"""L2: ルールベース判定。

CSVの列だけで機械的に判定できるものをここで片付ける。
ここで除外できれば公告文の取得(L3/L4)とLLM判定を丸ごとスキップできるため、
実行時間とAPIコストが直接下がる。

判定は3値:
  "除外"   -> exclude ルールに該当
  "採用"   -> promote ルールに該当（LLM判定をスキップ）
  None     -> ルールでは決まらない。L3/L4 に送る
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml

from .models import Case

log = logging.getLogger(__name__)

RuleOutcome = Literal["採用", "除外"]


class RulesError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class RuleHit:
    outcome: RuleOutcome
    reason: str


@dataclass(frozen=True, slots=True)
class Rules:
    exclude: list[dict[str, Any]]
    promote: list[dict[str, Any]]
    owned_grades: set[str]
    grade_reason: str

    def evaluate(self, case: Case, today: date) -> RuleHit | None:
        # promote を先に見る。明示的に採用と決めたものは除外条件より優先する。
        for rule in self.promote:
            if _matches(rule, case, today):
                return RuleHit("採用", rule.get("reason") or "採用ルールに該当")

        for rule in self.exclude:
            if _matches(rule, case, today):
                return RuleHit("除外", rule.get("reason") or "除外ルールに該当")

        # 入札参加資格の等級。案件が等級を要求していて、自社の保有等級と
        # 1つも重ならない場合のみ除外する（等級表記が無い案件は判定しない）。
        if self.owned_grades:
            required = case.grades
            if required and not (required & self.owned_grades):
                return RuleHit(
                    "除外",
                    f"{self.grade_reason}（案件の要求等級 {sorted(required)} / "
                    f"保有 {sorted(self.owned_grades)}）",
                )

        return None


def load_rules(path: Path) -> Rules:
    if not path.is_file():
        raise RulesError(f"ルールファイルがありません: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    grades = data.get("grades") or {}
    return Rules(
        exclude=list(data.get("exclude") or []),
        promote=list(data.get("promote") or []),
        owned_grades={g.strip().upper() for g in (grades.get("owned") or []) if g.strip()},
        grade_reason=grades.get("reason", "保有等級では参加できない"),
    )


def _values(case: Case, field: str) -> list[str]:
    """比較対象の値を文字列のリストで取り出す（単一値も1要素のリストにする）。"""
    v = getattr(case, field, None)
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    return [str(v)]


def _matches(rule: dict[str, Any], case: Case, today: date) -> bool:
    """1つのルールが案件に該当するか。

    ルールに書かれた条件はすべて満たす必要がある(AND)。
    条件が1つも書かれていないルールは「該当しない」とする
    （空のルールが全件に該当して全滅する事故を防ぐ）。
    """
    field = rule.get("field")
    if not field:
        return False

    conditions_present = False

    if "within_days" in rule:
        conditions_present = True
        d = getattr(case, field, None)
        if not isinstance(d, date):
            return False
        if (d - today).days > int(rule["within_days"]):
            return False

    if "after_days" in rule:
        conditions_present = True
        d = getattr(case, field, None)
        if not isinstance(d, date):
            return False
        if (d - today).days <= int(rule["after_days"]):
            return False

    values = _values(case, field)

    if rule.get("contains_any"):
        conditions_present = True
        needles = rule["contains_any"]
        if not any(n in v for v in values for n in needles):
            return False

    if rule.get("contains_none_of"):
        conditions_present = True
        needles = rule["contains_none_of"]
        if any(n in v for v in values for n in needles):
            return False

    if rule.get("in"):
        conditions_present = True
        allowed = set(rule["in"])
        if not any(v in allowed for v in values):
            return False

    if rule.get("not_in"):
        conditions_present = True
        denied = set(rule["not_in"])
        if any(v in denied for v in values):
            return False
        # not_in は「候補のどれでもない」ことを求めるルールなので、
        # 値が空の場合は判断できないものとして該当させない
        if not values:
            return False

    if not conditions_present:
        log.debug("条件が空のルールをスキップします: %r", rule)
        return False

    return True
