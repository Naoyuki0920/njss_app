"""L3: 官公需情報ポータルサイト検索API クライアント（中小企業庁）。

  エンドポイント: https://www.kkj.go.jp/api/
  仕様書: https://www.kkj.go.jp/doc/ja/api_guide.pdf (V1.1)

このAPIは公式に「情報を自動的に取得できるようにAPIを提供しています」と明記された
オープンAPIであり、NJSSとは別サービス。認証は不要。

とはいえ公共のサービスに負荷をかけないよう、逐次実行・リクエスト間の待機・
指数バックオフ・取得結果のキャッシュを行う。
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, timedelta

import httpx

log = logging.getLogger(__name__)

API_URL = "https://www.kkj.go.jp/api/"

# www.kkj.go.jp のサーバ証明書は JPRS を発行元とし、certifi のCAバンドルには
# そのルートが含まれない（macOS/Windows のシステムトラストストアには含まれる）。
# truststore を使ってOSのトラストストアで検証する。
# 証明書検証そのものは有効なまま — verify=False は使わない。
try:
    import truststore

    truststore.inject_into_ssl()
    log.debug("truststore を有効化しました（OSのトラストストアで証明書を検証します）")
except ImportError:  # pragma: no cover
    log.warning(
        "truststore が未インストールです。www.kkj.go.jp への接続が "
        "CERTIFICATE_VERIFY_FAILED で失敗する場合は `pip install truststore` してください。"
    )

# JIS X0401 都道府県コード（APIの LG_Code パラメータ用）
PREF_CODES = {
    "北海道": "01", "青森県": "02", "岩手県": "03", "宮城県": "04", "秋田県": "05",
    "山形県": "06", "福島県": "07", "茨城県": "08", "栃木県": "09", "群馬県": "10",
    "埼玉県": "11", "千葉県": "12", "東京都": "13", "神奈川県": "14", "新潟県": "15",
    "富山県": "16", "石川県": "17", "福井県": "18", "山梨県": "19", "長野県": "20",
    "岐阜県": "21", "静岡県": "22", "愛知県": "23", "三重県": "24", "滋賀県": "25",
    "京都府": "26", "大阪府": "27", "兵庫県": "28", "奈良県": "29", "和歌山県": "30",
    "鳥取県": "31", "島根県": "32", "岡山県": "33", "広島県": "34", "山口県": "35",
    "徳島県": "36", "香川県": "37", "愛媛県": "38", "高知県": "39", "福岡県": "40",
    "佐賀県": "41", "長崎県": "42", "熊本県": "43", "大分県": "44", "宮崎県": "45",
    "鹿児島県": "46", "沖縄県": "47",
}


class KkjError(Exception):
    """官公需APIがエラーを返した、または応答を解釈できない。"""


@dataclass(frozen=True, slots=True)
class KkjResult:
    """検索結果1件。APIの <SearchResult> に対応する。"""

    project_name: str
    organization_name: str = ""
    cft_issue_date: date | None = None
    procedure_type: str = ""
    category: str = ""
    location: str = ""
    certification: str = ""
    external_document_uri: str = ""
    file_type: str = ""
    prefecture_name: str = ""
    city_name: str = ""
    project_description: str = ""
    attachments: list[dict[str, str]] = field(default_factory=list)


def _text(el: ET.Element, tag: str) -> str:
    child = el.find(tag)
    return (child.text or "").strip() if child is not None and child.text else ""


def _parse_date(s: str) -> date | None:
    """ISO8601 (例: 2026-08-07T00:00:00+09:00) の日付部分を取る。"""
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


class KkjClient:
    """官公需APIクライアント。

    min_interval 秒あけて逐次リクエストする。並列化はしない。
    """

    def __init__(
        self,
        *,
        min_interval: float = 1.0,
        timeout: float = 60.0,
        max_retries: int = 3,
        user_agent: str = "njss-digest/0.1 (internal bid screening tool)",
    ) -> None:
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._last_request_at = 0.0
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": user_agent},
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> KkjClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _wait(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

    def _get(self, params: dict[str, str]) -> str:
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            self._wait()
            try:
                r = self._client.get(API_URL, params=params)
                self._last_request_at = time.monotonic()
                r.raise_for_status()
                return r.text
            except httpx.HTTPError as e:
                last_err = e
                self._last_request_at = time.monotonic()
                backoff = 2.0**attempt
                log.warning(
                    "官公需API リクエスト失敗 (%d/%d): %s — %.0f秒待機",
                    attempt + 1,
                    self.max_retries,
                    e,
                    backoff,
                )
                time.sleep(backoff)
        raise KkjError(f"官公需APIへのリクエストに失敗しました: {last_err}")

    def search(
        self,
        *,
        query: str | None = None,
        project_name: str | None = None,
        organization_name: str | None = None,
        lg_code: str | None = None,
        cft_issue_from: date | None = None,
        cft_issue_to: date | None = None,
        count: int = 1000,
    ) -> list[KkjResult]:
        """検索を実行する。

        Query / Project_Name / Organization_Name / LG_Code のいずれか1つは必須
        （複数指定時はAND条件）。
        """
        params: dict[str, str] = {"Count": str(min(count, 1000))}
        if query:
            params["Query"] = query
        if project_name:
            params["Project_Name"] = project_name
        if organization_name:
            params["Organization_Name"] = organization_name
        if lg_code:
            params["LG_Code"] = lg_code
        if cft_issue_from or cft_issue_to:
            params["CFT_Issue_Date"] = (
                f"{cft_issue_from.isoformat() if cft_issue_from else ''}"
                f"/{cft_issue_to.isoformat() if cft_issue_to else ''}"
            )

        if not any(k in params for k in ("Query", "Project_Name", "Organization_Name", "LG_Code")):
            raise KkjError(
                "Query / Project_Name / Organization_Name / LG_Code のいずれか1つは必須です"
            )

        body = self._get(params)

        try:
            root = ET.fromstring(body)
        except ET.ParseError as e:
            raise KkjError(f"応答XMLを解釈できません: {e}\n先頭200字: {body[:200]!r}") from e

        # エラー時は <Results><Error>…</Error></Results> が返る
        err = root.find("Error")
        if err is not None:
            raise KkjError(f"官公需APIがエラーを返しました: {(err.text or '').strip()}")

        results = [self._to_result(sr) for sr in root.iter("SearchResult")]
        hits = root.findtext(".//SearchHits") or "?"
        log.debug("官公需API %s -> hits=%s returned=%d", params, hits, len(results))
        return results

    @staticmethod
    def _to_result(sr: ET.Element) -> KkjResult:
        attachments = [
            {"name": _text(a, "Name"), "uri": _text(a, "Uri")}
            for a in sr.iter("Attachment")
        ]
        return KkjResult(
            project_name=_text(sr, "ProjectName"),
            organization_name=_text(sr, "OrganizationName"),
            cft_issue_date=_parse_date(_text(sr, "CftIssueDate")),
            procedure_type=_text(sr, "ProcedureType"),
            category=_text(sr, "Category"),
            location=_text(sr, "Location"),
            certification=_text(sr, "Certification"),
            external_document_uri=_text(sr, "ExternalDocumentURI"),
            file_type=_text(sr, "FileType"),
            prefecture_name=_text(sr, "PrefectureName"),
            city_name=_text(sr, "CityName"),
            project_description=_text(sr, "ProjectDescription"),
            attachments=attachments,
        )


def date_window(d: date | None, days: int) -> tuple[date | None, date | None]:
    """公示日の前後 days 日を検索期間にする。

    NJSSと官公需ポータルで公示日の記録日がずれることがあるため幅を持たせる。
    """
    if d is None:
        return None, None
    return d - timedelta(days=days), d + timedelta(days=days)
