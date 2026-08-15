"""ルールベース判定の検証。

ここで除外した案件は公告文の取得もLLM判定も行わないため、
誤って除外すると案件を取りこぼす。特に「条件が空のルールが全件に該当して
全滅する」事故を防げているかを確認する。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import yaml

from njss_digest.models import Case
from njss_digest.rules import load_rules

TODAY = dt.date(2026, 8, 9)


def _case(**kw) -> Case:
    base = dict(
        case_id="1",
        name="道路照明灯新設工事",
        url="https://x/1",
        agency="広島市役所",
        agency_pref="広島県",
        deadline_on=dt.date(2026, 8, 27),
        industry=["電気工事"],
        qualification=["広島市競争入札参加資格 建設・土木・工事系 B"],
    )
    base.update(kw)
    return Case(**base)


def _rules(tmp_path: Path, data: dict):
    p = tmp_path / "rules.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return load_rules(p)


def test_空のルールでは何も除外されない(tmp_path):
    """条件が書かれていないルールが全件に該当すると全滅する。"""
    r = _rules(
        tmp_path,
        {
            "exclude": [
                {"field": "name", "contains_any": [], "reason": "x"},
                {"field": "industry", "contains_none_of": [], "reason": "y"},
            ],
            "promote": [],
            "grades": {"owned": []},
        },
    )
    assert r.evaluate(_case(), TODAY) is None


def test_締切が近い案件を除外する(tmp_path):
    r = _rules(
        tmp_path,
        {
            "exclude": [
                {"field": "deadline_on", "within_days": 3, "reason": "締切が近い"}
            ]
        },
    )
    assert r.evaluate(_case(deadline_on=dt.date(2026, 8, 11)), TODAY).outcome == "除外"
    assert r.evaluate(_case(deadline_on=dt.date(2026, 8, 27)), TODAY) is None


def test_締切日が無い案件は除外されない(tmp_path):
    """日付が取れないことを理由に落としてしまわないこと。"""
    r = _rules(
        tmp_path,
        {"exclude": [{"field": "deadline_on", "within_days": 3, "reason": "締切が近い"}]},
    )
    assert r.evaluate(_case(deadline_on=None), TODAY) is None


def test_NGキーワードで除外する(tmp_path):
    r = _rules(
        tmp_path,
        {"exclude": [{"field": "name", "contains_any": ["清掃業務"], "reason": "対象外"}]},
    )
    assert r.evaluate(_case(name="庁舎清掃業務委託"), TODAY).outcome == "除外"
    assert r.evaluate(_case(), TODAY) is None


def test_対象地域外を除外する(tmp_path):
    r = _rules(
        tmp_path,
        {
            "exclude": [
                {
                    "field": "agency_pref",
                    "not_in": ["広島県", "岡山県"],
                    "reason": "対象地域外",
                }
            ]
        },
    )
    assert r.evaluate(_case(agency_pref="福岡県"), TODAY).outcome == "除外"
    assert r.evaluate(_case(agency_pref="広島県"), TODAY) is None


def test_保有等級と重ならなければ除外する(tmp_path):
    r = _rules(tmp_path, {"grades": {"owned": ["A"], "reason": "等級不足"}})
    # 案件はB等級のみ要求 -> 保有Aと重ならない
    assert r.evaluate(_case(), TODAY).outcome == "除外"
    # 案件がA,Bを許容 -> 重なる
    ok = _case(
        qualification=[
            "広島市競争入札参加資格 建設・土木・工事系 A",
            "広島市競争入札参加資格 建設・土木・工事系 B",
        ]
    )
    assert r.evaluate(ok, TODAY) is None


def test_等級表記が無い案件は等級で除外しない(tmp_path):
    """等級を要求していない案件を、等級不明を理由に落とさないこと。"""
    r = _rules(tmp_path, {"grades": {"owned": ["A"], "reason": "等級不足"}})
    assert r.evaluate(_case(qualification=["電気工事業登録"]), TODAY) is None


def test_採用ルールは除外ルールより優先される(tmp_path):
    r = _rules(
        tmp_path,
        {
            "promote": [
                {"field": "name", "contains_any": ["照明灯"], "reason": "重点分野"}
            ],
            "exclude": [
                {"field": "agency_pref", "not_in": ["東京都"], "reason": "対象地域外"}
            ],
        },
    )
    hit = r.evaluate(_case(), TODAY)
    assert hit.outcome == "採用" and hit.reason == "重点分野"
