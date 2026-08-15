"""NJSS案件と官公需ポータル案件の突合の検証。

誤マッチは別案件の予定価格で判定してしまうことを意味するため、
「拾えること」と同じくらい「拾い過ぎないこと」を確認する。
"""

from __future__ import annotations


from njss_digest.kkj import KkjResult
from njss_digest.match import (
    agency_matches,
    agency_query_variants,
    best_match,
    name_similarity,
    normalize,
    normalize_agency,
)
from njss_digest.models import Case


def _case(name: str, agency: str = "広島市役所") -> Case:
    return Case(case_id="1", name=name, url="https://x/1", agency=agency)


def _result(name: str, org: str = "広島県広島市") -> KkjResult:
    return KkjResult(project_name=name, organization_name=org)


# --- 正規化 ---------------------------------------------------------------


def test_全角半角と括弧の揺れを吸収する():
    """実データで確認した揺れ:

    NJSS 「…工事(8-1)」/ 官公需「…工事（８－１）」
    """
    a = "一般県道南観音観音線道路照明灯新設工事(8-1)"
    b = "一般県道南観音観音線道路照明灯新設工事（８－１）"
    assert normalize(a) == normalize(b)
    assert name_similarity(a, b) == 1.0


def test_ゼロ幅スペースを無視する():
    assert normalize("照明灯工事​") == normalize("照明灯工事")


def test_機関名を官公需側の表記に寄せる():
    assert normalize_agency("広島市役所") == "広島市"
    assert normalize_agency("かほく市役所") == "かほく市"
    assert normalize_agency("南条郡南越前町役場") == "南条郡南越前町"


# --- 機関名の照合 ---------------------------------------------------------


def test_市区町村は末尾一致で照合する():
    assert agency_matches("広島市役所", "広島県広島市")
    assert agency_matches("かほく市役所", "石川県かほく市")


def test_県と県内市町村を取り違えない():
    """「広島県」が「広島県広島市」に部分一致して誤マッチするのを防ぐ。"""
    assert not agency_matches("広島県庁", "広島県広島市")


def test_別の自治体には一致しない():
    assert not agency_matches("広島市役所", "福岡県福岡市")


def test_省庁は包含関係を許す():
    assert agency_matches("経済産業省", "経済産業省北海道経済産業局")


def test_郡名つきでも照合できる():
    """実データ: NJSS「南条郡南越前町役場」/ 官公需「福井県南越前町」"""
    assert agency_matches("南条郡南越前町役場", "福井県南越前町")


# --- 検索語の候補 ---------------------------------------------------------


def test_郡名を落とした候補が作られる():
    """「南条郡南越前町」では0件、「南越前町」なら8件ヒットする実測に対応。"""
    variants = agency_query_variants("南条郡南越前町役場")
    assert variants[0] == "南条郡南越前町"
    assert "南越前町" in variants


def test_郡山市の郡を誤って削らない():
    """郡が付くのは町村のみ。市名の一部を削ってはいけない。"""
    assert agency_query_variants("郡山市役所") == ["郡山市"]


def test_都道府県名を落とした候補も作られる():
    assert "高槻市" in agency_query_variants("大阪府高槻市")


# --- 突合 -----------------------------------------------------------------


def test_完全一致を優先する():
    m = best_match(_case("照明灯新設工事"), [_result("照明灯新設工事")])
    assert m is not None and m.method == "exact" and m.score == 1.0


def test_しきい値未満はマッチさせない():
    """確信が持てないものは拾わず、Web検索フォールバックに送る。"""
    case = _case("杣山トンネル照明施設更新工事", agency="南条郡南越前町役場")
    candidates = [
        _result("一般競争入札公告（デジタル航空写真撮影業務委託）", "福井県南越前町"),
        _result("南越前町出会いの場創出支援事業", "福井県南越前町"),
    ]
    assert best_match(case, candidates) is None


def test_機関が違えば案件名が同じでもマッチしない():
    case = _case("道路照明灯新設工事", agency="広島市役所")
    assert best_match(case, [_result("道路照明灯新設工事", "福岡県福岡市")]) is None


def test_類似一致は最も高いものを選ぶ():
    case = _case("中央区管内道路照明灯建替LED化工事その1")
    candidates = [
        _result("中央区管内道路照明灯建替LED化工事 その2"),
        _result("中央区管内道路照明灯建替LED化工事 その1"),
    ]
    m = best_match(case, candidates, threshold=0.6)
    assert m is not None
    assert m.result.project_name.endswith("その1")
