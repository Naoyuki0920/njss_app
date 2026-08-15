"""L4: Web検索による公告文の取得（官公需APIで見つからなかった案件のみ）。

実測では官公需ポータルのカバレッジは3割程度で、掲載していない自治体がある
（例: 福岡市・天草市は0件）。そうした案件はこの層で発注機関の公式サイトから拾う。

検索対象ドメインを lg.jp / go.jp に限定し、公式の一次情報だけを根拠にする。
まとめサイトや入札情報の二次配信を掴むと、古い情報や別案件を根拠に
判定してしまうため。

戻り値は Notice。この後は官公需API経由の場合とまったく同じ判定経路に合流する。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .llm import Usage, make_client
from .models import Case, Notice

log = logging.getLogger(__name__)

# サーバーツールのバージョン（動的フィルタリング対応版）
WEB_SEARCH_TOOL = "web_search_20260209"
WEB_FETCH_TOOL = "web_fetch_20260209"

_SOURCE_RE = re.compile(r"^\s*SOURCE_URL:\s*(\S+)\s*$", re.MULTILINE)
_NOT_FOUND = "該当なし"


@dataclass
class WebSearchConfig:
    keychain_service: str = "njss-digest-anthropic"
    allow_env: bool = True
    # 検索と本文の書き出しが主で深い推論は要らないため、既定を安価なモデルにする。
    model: str = "claude-sonnet-5"
    allowed_domains: tuple[str, ...] = ("lg.jp", "go.jp")
    max_uses: int = 3
    max_tokens: int = 8000
    max_continuations: int = 2
    # web_fetch が1ページから取り込む本文の上限トークン数
    max_content_tokens: int = 20000
    # 思考の深さ。探索タスクなので低めで足りる
    effort: str = "low"


class WebNoticeFinder:
    """発注機関の公式サイトから公告文を探して取得する。"""

    def __init__(self, config: WebSearchConfig) -> None:
        self.client = make_client(
            keychain_service=config.keychain_service, allow_env=config.allow_env
        )
        self.config = config
        self.usage = Usage()

    def _tools(self) -> list[dict]:
        domains = list(self.config.allowed_domains)
        return [
            {
                "type": WEB_SEARCH_TOOL,
                "name": "web_search",
                "max_uses": self.config.max_uses,
                "allowed_domains": domains,
                # 既定では programmatic tool calling 経由の呼び出しも許可されるが、
                # それに対応しないモデル(haiku等)では400になる。直接呼び出しに限定して
                # 安価なモデルでも使えるようにする。
                "allowed_callers": ["direct"],
            },
            {
                "type": WEB_FETCH_TOOL,
                "name": "web_fetch",
                "max_uses": self.config.max_uses,
                "allowed_domains": domains,
                "allowed_callers": ["direct"],
                # 取得ページの本文をこのトークン数で打ち切る。
                # 上限を設けないと大きなPDFで入力トークンが跳ね上がる。
                "max_content_tokens": self.config.max_content_tokens,
            },
        ]

    def find(self, case: Case) -> Notice | None:
        """案件の公告文を探す。見つからなければ None。"""
        system = (
            "あなたは入札公告の一次情報を探す担当です。\n"
            "発注機関の公式サイト上の入札公告ページ・公告文PDFのみを情報源にしてください。\n"
            "入札情報のまとめサイトや第三者による転載は使わないでください。\n\n"
            "手順:\n"
            "1. 案件名と発注機関で検索する\n"
            "2. 公式の公告ページが見つかったら fetch して本文を読む\n"
            "3. 予定価格・入札参加資格・工期・履行場所・業務内容が書かれた部分を"
            "**原文のまま**書き出す（要約・言い換えをしない）\n\n"
            "出力形式は厳密に次のとおり:\n"
            "SOURCE_URL: <根拠にしたページのURL>\n"
            "---\n"
            "<公告文の該当部分の原文>\n\n"
            f"公式サイト上に該当案件が見つからない場合は、{_NOT_FOUND} とだけ出力してください。\n"
            "見つからないのに推測で内容を書いてはいけません。"
        )
        user = (
            f"案件名: {case.name}\n"
            f"発注機関: {case.agency}（{case.agency_pref}）\n"
            f"公示日: {case.announced_on}\n"
            f"締切日: {case.deadline_on}\n"
            f"入札形式: {case.bid_type}\n"
        )

        messages: list[dict] = [{"role": "user", "content": user}]
        text = ""

        for _ in range(self.config.max_continuations):
            resp = self.client.messages.create(
                model=self.config.model,
                max_tokens=self.config.max_tokens,
                system=system,
                output_config={"effort": self.config.effort},
                tools=self._tools(),
                messages=messages,
            )
            self.usage.add(resp.usage)
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

            # サーバーツールが反復上限に達した場合は、そのまま再送すると再開される
            if resp.stop_reason != "pause_turn":
                break
            messages = [
                {"role": "user", "content": user},
                {"role": "assistant", "content": resp.content},
            ]
        else:
            log.warning("[%s] pause_turn が続いたため打ち切りました", case.case_id)

        if not text or _NOT_FOUND in text[:200]:
            log.info("[%s] 公式サイトに公告文が見つかりませんでした", case.case_id)
            return None

        m = _SOURCE_RE.search(text)
        source_url = m.group(1) if m else None
        body = text[m.end() :] if m else text
        body = body.lstrip().removeprefix("---").lstrip()

        if not body.strip():
            return None

        log.info(
            "[%s] Web検索で公告文を取得: %s (%d字)", case.case_id, source_url, len(body)
        )
        return Notice(
            case_id=case.case_id,
            source="websearch",
            text=body,
            source_url=source_url,
            matched_by="websearch",
        )
