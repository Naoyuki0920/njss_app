"""L3: NJSS案件 ⇄ 官公需ポータル案件 の突合。

同じ案件でも両サービスで表記が揺れる。実データで確認した揺れ:
  - 案件名にゼロ幅スペース(U+200B)が混入する
  - 全角/半角、括弧の種類、スペースの有無
  - 機関名が NJSS「広島市役所」/ 官公需「広島県広島市」

誤マッチは誤判定に直結する（別案件の予定価格で判定してしまう）ため、
確信が持てない場合はマッチさせず L4(Web検索) に送る方針を取る。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Literal
from collections.abc import Iterable

from .kkj import KkjResult
from .models import Case

log = logging.getLogger(__name__)

# 突合前に落とす記号類（表記揺れの主因）
_PUNCT = re.compile(r"[\s　()（）\[\]［］{}｛｝「」『』<>＜＞・,，、.．/／\-‐‑–—―ー_＿:：;；'\"”’]+")
_ZERO_WIDTH = re.compile(r"[​-‏﻿⁠]")

# 自治体の行政区分の接尾辞。これで終わる機関名は末尾一致で厳密に判定する
# （「広島県」が「広島県広島市」に部分一致してしまうのを防ぐ）
_ADMIN_SUFFIX = ("都", "道", "府", "県", "市", "区", "町", "村")

# NJSS側の機関名を官公需側の表記に寄せる
_AGENCY_NORMALIZE = [
    (re.compile(r"市役所$"), "市"),
    (re.compile(r"区役所$"), "区"),
    (re.compile(r"町役場$"), "町"),
    (re.compile(r"村役場$"), "村"),
    (re.compile(r"county?庁$"), ""),
    (re.compile(r"(都|道|府|県)庁$"), r"\1"),
]

MatchMethod = Literal["exact", "fuzzy"]


def normalize(s: str | None) -> str:
    """比較用の正規化: NFKC → ゼロ幅文字除去 → 記号/空白除去 → 小文字化。"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = _ZERO_WIDTH.sub("", s)
    s = _PUNCT.sub("", s)
    return s.lower()


def normalize_agency(name: str | None) -> str:
    """NJSSの機関名を官公需ポータルの表記に寄せる。

    例: 広島市役所 -> 広島市 / かほく市役所 -> かほく市 / 大阪府庁 -> 大阪府
    """
    s = normalize(name)
    for pat, repl in _AGENCY_NORMALIZE:
        s2 = pat.sub(repl, s)
        if s2 != s:
            return s2
    return s


def agency_query_variants(name: str | None) -> list[str]:
    """官公需APIの Organization_Name に渡す検索語の候補を、確度の高い順に返す。

    APIは前後方・途中一致だが、NJSSにしか無い修飾（郡名など）が付いていると
    ヒットしない。実データで確認した例:
        NJSS「南条郡南越前町役場」-> 「南条郡南越前町」では0件、「南越前町」なら8件

    そのため0件だった場合に順に試すための候補列を用意する。
    """
    base = normalize_agency(name)
    if not base:
        return []

    variants = [base]

    # 郡名を落とす。「南条郡南越前町」-> 「南越前町」
    # 郡が付くのは町村のみなので、末尾が町/村のときだけ適用する
    # (「郡山市」のような地名の一部を誤って削らないため)
    if base.endswith(("町", "村")):
        stripped = re.sub(r"^.*?郡", "", base)
        if stripped and stripped != base:
            variants.append(stripped)

    # 先頭の都道府県名を落とす。「大阪府高槻市」-> 「高槻市」
    m = re.match(r"^(.{2,4}?[都道府県])(.+[市区町村])$", base)
    if m:
        variants.append(m.group(2))

    seen: set[str] = set()
    return [v for v in variants if not (v in seen or seen.add(v))]


def agency_matches(njss_agency: str | None, kkj_org: str | None) -> bool:
    """機関名が同一の組織を指しているとみなせるか。

    NJSS側の表記に郡名などの修飾が付いていることがあるため、
    agency_query_variants と同じ候補を順に照合する。
    """
    b = normalize(kkj_org)
    if not b:
        return False

    for a in agency_query_variants(njss_agency):
        if a == b:
            return True
        if a.endswith(_ADMIN_SUFFIX):
            # 自治体は末尾一致で厳密に。「広島市」⊂「広島県広島市」は真、
            # 「広島県」⊂「広島県広島市」は偽にしたい。
            if b.endswith(a):
                return True
        # 省庁・独法などは包含関係を許す（「経済産業省」⊂「経済産業省北海道経済産業局」）
        elif a in b or b in a:
            return True
    return False


def _bigrams(s: str) -> set[str]:
    if len(s) < 2:
        return {s} if s else set()
    return {s[i : i + 2] for i in range(len(s) - 1)}


def name_similarity(a: str, b: str) -> float:
    """案件名の文字bi-gram Jaccard係数（0.0〜1.0）。"""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ga, gb = _bigrams(na), _bigrams(nb)
    union = ga | gb
    return len(ga & gb) / len(union) if union else 0.0


@dataclass(frozen=True, slots=True)
class Match:
    result: KkjResult
    method: MatchMethod
    score: float


def best_match(
    case: Case,
    candidates: Iterable[KkjResult],
    *,
    threshold: float = 0.70,
    require_agency: bool = True,
) -> Match | None:
    """候補の中から案件に対応するものを1件選ぶ。見つからなければ None。

    threshold 未満は「わからない」として捨てる。無理に拾わない。
    """
    best: Match | None = None

    for r in candidates:
        if require_agency and not agency_matches(case.agency, r.organization_name):
            continue

        score = name_similarity(case.name, r.project_name)
        if score >= 1.0:
            return Match(result=r, method="exact", score=1.0)
        if score >= threshold and (best is None or score > best.score):
            best = Match(result=r, method="fuzzy", score=score)

    if best:
        log.debug(
            "fuzzy一致 [%s] %r <-> %r (score=%.2f)",
            case.case_id,
            case.name,
            best.result.project_name,
            best.score,
        )
    return best
