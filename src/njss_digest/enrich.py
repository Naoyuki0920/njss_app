"""L3: 案件に公告文を付ける（官公需API経由）。

API呼び出しを減らすため、案件ごとに引くのではなく
「機関」単位でまとめて候補を取得し、突合はローカルで行う。
7案件が3機関に属するなら、リクエストは3回で済む。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from .kkj import KkjClient, KkjError, KkjResult
from .match import Match, agency_query_variants, best_match, normalize_agency
from .models import Case, Notice

log = logging.getLogger(__name__)


@dataclass
class EnrichStats:
    total: int = 0
    exact: int = 0
    fuzzy: int = 0
    missed: int = 0
    api_calls: int = 0
    api_errors: int = 0
    candidates_by_agency: dict[str, int] = field(default_factory=dict)

    @property
    def hit(self) -> int:
        return self.exact + self.fuzzy

    @property
    def hit_rate(self) -> float:
        return self.hit / self.total if self.total else 0.0


@dataclass
class EnrichResult:
    notices: dict[str, Notice]
    matches: dict[str, Match]
    stats: EnrichStats


def _agency_key(case: Case) -> str:
    return normalize_agency(case.agency) or "(不明)"


def fetch_notices(
    cases: list[Case],
    client: KkjClient,
    *,
    window_days: int = 14,
    threshold: float = 0.70,
) -> EnrichResult:
    """官公需APIから公告文を取得し、案件に突合する。"""
    stats = EnrichStats(total=len(cases))
    notices: dict[str, Notice] = {}
    matches: dict[str, Match] = {}

    groups: dict[str, list[Case]] = defaultdict(list)
    for c in cases:
        groups[_agency_key(c)].append(c)

    for agency, members in groups.items():
        dates = [c.announced_on for c in members if c.announced_on]
        start = min(dates) - timedelta(days=window_days) if dates else None
        end = max(dates) + timedelta(days=window_days) if dates else None

        # 機関名の表記ゆれで0件になることがあるため、候補語を順に試す
        # （例: 「南条郡南越前町」は0件、「南越前町」なら8件）
        candidates: list[KkjResult] = []
        used_query = agency
        for query in agency_query_variants(members[0].agency) or [agency]:
            try:
                candidates = client.search(
                    organization_name=query,
                    cft_issue_from=start,
                    cft_issue_to=end,
                    count=1000,
                )
                stats.api_calls += 1
            except KkjError as e:
                stats.api_errors += 1
                log.warning("機関 %r の候補取得に失敗: %s", query, e)
                continue
            if candidates:
                used_query = query
                break

        stats.candidates_by_agency[agency] = len(candidates)
        log.info(
            "機関 %-14s 案件%2d件 / 官公需候補%4d件 (検索語 %r, 期間 %s〜%s)",
            agency,
            len(members),
            len(candidates),
            used_query,
            start,
            end,
        )

        for case in members:
            m = best_match(case, candidates, threshold=threshold)
            if m is None:
                stats.missed += 1
                continue

            matches[case.case_id] = m
            if m.method == "exact":
                stats.exact += 1
            else:
                stats.fuzzy += 1

            notices[case.case_id] = Notice(
                case_id=case.case_id,
                source="kkj",
                text=m.result.project_description,
                source_url=m.result.external_document_uri or None,
                attachments=m.result.attachments,
                matched_by=m.method,
                match_score=m.score,
                raw={
                    "project_name": m.result.project_name,
                    "organization_name": m.result.organization_name,
                    "cft_issue_date": (
                        m.result.cft_issue_date.isoformat() if m.result.cft_issue_date else ""
                    ),
                    "category": m.result.category,
                    "procedure_type": m.result.procedure_type,
                },
            )

    return EnrichResult(notices=notices, matches=matches, stats=stats)
