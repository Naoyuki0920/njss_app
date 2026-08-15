"""CSV取り込みと列マッピングの検証。

最重要の観点は「壊れたときに黙って通らないこと」。
NJSSが列順を変えたのに空データで正常終了するのが最悪の失敗モードなので、
そのケースで確実に例外になることを確認する。
"""

from __future__ import annotations

import datetime as dt

import pytest
from conftest import write_csv

from njss_digest.ingest import IngestError, detect_encoding, read_csv
from njss_digest.mapping import MappingError, to_cases


def _load(path, mapping):
    raw = read_csv(path, mapping.encoding_candidates, mapping.has_header)
    return to_cases(raw, mapping)


# --- 文字コード -----------------------------------------------------------


@pytest.mark.parametrize("encoding", ["cp932", "utf-8", "utf-8-sig"])
def test_各文字コードで文字化けしない(tmp_path, mapping, sample_rows, encoding):
    path = write_csv(tmp_path / "t.csv", sample_rows, encoding=encoding)
    mapping_with_enc = mapping
    if encoding != "cp932":
        # 候補の先頭にその文字コードを置いて確実に選ばせる
        object.__setattr__(
            mapping_with_enc, "encoding_candidates", [encoding, "cp932"]
        )
    cases, _ = _load(path, mapping_with_enc)
    assert cases[0].name == "一般県道南観音観音線道路照明灯新設工事(8-1)"
    assert cases[0].agency == "広島市役所"


def test_判定できない文字コードはエラーになる():
    with pytest.raises(IngestError):
        detect_encoding(b"\xff\xfe\x00\x00\xff", ["ascii"])


def test_空ファイルはエラーになる(tmp_path, mapping):
    path = tmp_path / "empty.csv"
    path.write_bytes(b"")
    with pytest.raises(IngestError):
        read_csv(path, mapping.encoding_candidates, mapping.has_header)


# --- マッピング -----------------------------------------------------------


def test_基本的な項目が取れる(tmp_path, mapping, sample_rows):
    path = write_csv(tmp_path / "t.csv", sample_rows)
    cases, report = _load(path, mapping)

    assert len(cases) == 2
    c = cases[0]
    assert c.case_id == "33807648"
    assert c.url.endswith("/33807648")
    assert c.bid_type == "一般競争入札"
    assert c.announced_on == dt.date(2026, 8, 7)
    assert c.deadline_on == dt.date(2026, 8, 27)
    assert c.docs_due_on == dt.date(2026, 8, 19)
    # 改行区切りの複数値がリストに展開される
    assert c.industry == ["土木工事", "電気工事"]
    assert len(c.qualification) == 2
    assert report.ok


def test_予定価格のハイフンはnull扱いになる(tmp_path, mapping, sample_rows):
    """入札前の案件は列[17]が "-" になる。値として持ち回らない。"""
    path = write_csv(tmp_path / "t.csv", sample_rows)
    cases, _ = _load(path, mapping)
    assert cases[0].price_text is None


def test_等級が抽出される(tmp_path, mapping, sample_rows):
    path = write_csv(tmp_path / "t.csv", sample_rows)
    cases, _ = _load(path, mapping)
    assert cases[0].grades == {"A", "B"}
    assert cases[1].grades == {"B"}


def test_定型句の概要は中身なしと判定される(tmp_path, mapping, sample_rows):
    """「公告の日から配布する設計図書のとおり」は情報を持たない。

    これを中身ありと誤判定すると、公告文の外部取得をスキップしてしまう。
    """
    path = write_csv(tmp_path / "t.csv", sample_rows)
    cases, _ = _load(path, mapping)
    assert cases[0].has_summary is True
    assert cases[1].has_summary is False


def test_ゼロ幅スペースが除去される(tmp_path, mapping, sample_rows):
    """実データの案件名に U+200B の混入を確認しているため。"""
    rows = [list(sample_rows[0])]
    rows[0][3] = "照明灯新設工事​"
    path = write_csv(tmp_path / "t.csv", rows, encoding="utf-8")
    object.__setattr__(mapping, "encoding_candidates", ["utf-8", "cp932"])
    cases, _ = _load(path, mapping)
    assert cases[0].name == "照明灯新設工事"


# --- 自己診断（壊れたときに止まること）------------------------------------


def test_必須列が空だとエラーで止まる(tmp_path, mapping, sample_rows):
    rows = [list(sample_rows[0])]
    rows[0][3] = ""  # 案件名を空に
    path = write_csv(tmp_path / "t.csv", rows)
    with pytest.raises(MappingError, match="必須フィールド"):
        _load(path, mapping)


def test_列がずれたら自己診断で止まる(tmp_path, mapping, sample_rows):
    """列を1つ挿入して全体をずらす。

    案件IDとURL末尾の一致チェックが働き、空データで正常終了させない。
    """
    rows = [["ダミー"] + list(sample_rows[0])[:-1]]
    path = write_csv(tmp_path / "t.csv", rows)
    with pytest.raises(MappingError):
        _load(path, mapping)


def test_URLホストが違うと止まる(tmp_path, mapping, sample_rows):
    rows = [list(sample_rows[0])]
    rows[0][4] = "https://example.com/offers/view/33807648"
    path = write_csv(tmp_path / "t.csv", rows)
    with pytest.raises(MappingError, match="自己診断"):
        _load(path, mapping)


def test_公示日が締切日より後だと止まる(tmp_path, mapping, sample_rows):
    rows = [list(sample_rows[0])]
    rows[0][8] = "2026/09/30"  # 公示日 > 締切日
    path = write_csv(tmp_path / "t.csv", rows)
    with pytest.raises(MappingError, match="自己診断"):
        _load(path, mapping)
