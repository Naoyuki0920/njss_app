from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"

# 実データ（案件情報_カーボンニュートラル_新着案件_2026-08-09.csv）と同じ30列構成。
# 内容は実案件をもとにした検証用データ。
ROW_HIROSHIMA = [
    "カーボンニュートラル",
    "33807648",
    "一般競争入札",
    "一般県道南観音観音線道路照明灯新設工事(8-1)",
    "https://www2.njss.info/offers/view/33807648",
    "広島市役所",
    "広島県",
    "広島県\n西区観音新町四丁目",
    "2026/08/07",
    "2026/08/27",
    "",
    "2026/08/19",
    "広島市競争入札参加資格 建設・土木・工事系 A\n広島市競争入札参加資格 建設・土木・工事系 B",
    "土木工事\n電気工事",
    "本工事は、道路照明灯を新設する工事である。\n1．道路照明灯新設工事 一式",
    "",
    "",
    "-",
    *[""] * 11,
    "無",
]

ROW_FUKUOKA = [
    "カーボンニュートラル",
    "33807436",
    "一般競争入札",
    "青果市場蛍光灯器具LED化工事",
    "https://www2.njss.info/offers/view/33807436",
    "福岡市役所",
    "福岡県",
    "福岡県\n福岡市東区みなと香椎三丁目１－１",
    "2026/08/06",
    "2026/08/28",
    "",
    "2026/08/17",
    "福岡市競争入札参加資格 建設・土木・工事系 B",
    "電気工事",
    "公告の日から配布する設計図書のとおり",
    "",
    "",
    "-",
    *[""] * 11,
    "無",
]


def write_csv(path: Path, rows: list[list[str]], encoding: str = "cp932") -> Path:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    path.write_bytes(buf.getvalue().encode(encoding))
    return path


@pytest.fixture
def mapping():
    from njss_digest.mapping import load_mapping

    return load_mapping(CONFIG_DIR / "csv_mapping.yaml")


@pytest.fixture
def sample_rows() -> list[list[str]]:
    return [list(ROW_HIROSHIMA), list(ROW_FUKUOKA)]
