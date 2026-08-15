"""L3/L4: 公告文を読んで案件を判定する。

2段構成にしている。公告文は実測で1件5万〜9万字あり、全件をそのまま
最終判定モデルに渡すと入力トークンが跳ね上がるため:

  1段目 (extract): 公告文から「予定価格 / 入札参加資格 / 工期 / 履行場所 /
                   業務内容」に該当する箇所を *原文のまま* 抜き出す。
                   要約させない。安価なモデルで案件ごとに実行する。
  2段目 (judge):   抜き出した結果だけを判定基準と突き合わせて採否を決める。
                   入力が小さいので全案件を1リクエストにまとめられる。

抽出した値には必ず公告文からの根拠引用を付ける。人が数秒で検算できるようにするため。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .models import Case, Judgement, Notice
from .secrets import DEFAULT_SERVICE, SecretsError, resolve_api_key, setup_hint

log = logging.getLogger(__name__)


class LlmError(Exception):
    pass


class LlmUnavailable(LlmError):
    """SDK未インストール、または認証情報が無い。"""


def make_client(*, keychain_service: str = DEFAULT_SERVICE, allow_env: bool = True):
    """認証済みの Anthropic クライアントを返す。

    APIキーはKeychainから取得し、SDKに明示的に渡す。環境変数に置いてしまうと
    このマシンで動く他のプロセスからも読めてしまうため。

    SDKは「認証情報なし」でも生成自体は成功し、最初のリクエスト時に
    TypeError で落ちる。実行の途中で分かりにくい形で失敗するのを避けるため、
    ここで安価なメタデータ呼び出しを1回だけ行って認証を確かめる
    （トークン消費はない）。
    """
    try:
        import anthropic
    except ImportError as e:
        raise LlmUnavailable(
            "anthropic SDK がインストールされていません。"
            "`pip install -e '.[llm]'` を実行してください。"
        ) from e

    try:
        api_key, origin = resolve_api_key(service=keychain_service, allow_env=allow_env)
    except SecretsError as e:
        raise LlmUnavailable(str(e)) from e

    log.info("APIキーの取得元: %s", origin)
    client = anthropic.Anthropic(api_key=api_key)

    try:
        client.models.list(limit=1)
    except anthropic.AuthenticationError as e:
        raise LlmUnavailable(
            f"APIキーが受け付けられませんでした（取得元: {origin}）。\n"
            f"キーが失効していないか確認してください。\n\n{setup_hint(keychain_service)}\n"
            f"（SDKからの詳細: {e}）"
        ) from e
    except anthropic.APIError as e:
        # 疎通はしているがエラー。ここでは通し、実処理側で扱う
        log.warning("認証確認中にAPIエラー: %s", e)
    return client


# --- 構造化出力のスキーマ -------------------------------------------------
# anthropic SDK が無い環境でも import できるよう、pydantic は遅延インポートする。


def _schemas():
    try:
        from pydantic import BaseModel, Field
    except ImportError as e:  # pragma: no cover
        raise LlmUnavailable(
            "pydantic がインストールされていません。`pip install -e '.[llm]'` を実行してください。"
        ) from e

    class Extraction(BaseModel):
        """公告文からの抽出結果。

        構造化出力にはスキーマの複雑さの上限があり、Optional(anyOf)を多用すると
        `Schema is too complex` で400になる。そのため任意項目も `str` とし、
        該当が無い場合は空文字を入れる方式にしている。
        """

        planned_price: str = Field(
            description="予定価格。金額と単位をそのまま。記載が無ければ空文字"
        )
        planned_price_quote: str = Field(
            description="予定価格の根拠となる原文（30〜120字）。無ければ空文字"
        )
        price_is_public: bool = Field(
            description=(
                "予定価格の具体的な金額が示されているか。"
                "「予定価格の制限の範囲内で」のような手続きの説明だけで"
                "金額が無い場合は false"
            )
        )
        price_yen: int = Field(
            description="予定価格を円単位の整数にしたもの（税抜き優先）。不明なら 0"
        )
        qualification: str = Field(
            description="入札参加資格。等級・業種登録・地域要件など。無ければ空文字"
        )
        qualification_quote: str = Field(
            description="入札参加資格の根拠となる原文。無ければ空文字"
        )
        work_type: str = Field(description="工事種別（例: 電気工事）。無ければ空文字")
        period: str = Field(description="工期・納期・履行期間。無ければ空文字")
        place: str = Field(description="履行場所・納品場所。無ければ空文字")
        scope_summary: str = Field(description="主な業務内容を1〜2文で。事実のみ")
        is_led: bool = Field(description="LED照明の設置・更新・改修を含む案件か")
        is_road_lighting: bool = Field(
            description="道路照明・街路灯・トンネル照明など屋外の道路まわりの照明か"
        )
        includes_design: bool = Field(description="設計・調査・コンサル業務を含むか")
        is_esco: bool = Field(description="ESCO事業（省エネルギーサービス事業）関連か")

    class CaseVerdict(BaseModel):
        case_id: str = Field(description="判定対象の案件ID")
        verdict: Literal["採用", "要確認", "除外"] = Field(
            description=(
                "採用=判定基準を満たすことが公告文から確認できる / "
                "要確認=情報が不足していて人の確認が要る / "
                "除外=判定基準に合致しないことが確認できる"
            )
        )
        score: int = Field(description="優先度 0〜100。高いほど有望", ge=0, le=100)
        reason: str = Field(description="判定理由を1〜2文で。抽出した事実に基づくこと")

    class BatchVerdict(BaseModel):
        verdicts: list[CaseVerdict]

    return Extraction, CaseVerdict, BatchVerdict


# --- クライアント ---------------------------------------------------------


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    calls: int = 0

    def add(self, u) -> None:
        self.calls += 1
        self.input_tokens += getattr(u, "input_tokens", 0) or 0
        self.output_tokens += getattr(u, "output_tokens", 0) or 0
        self.cache_read += getattr(u, "cache_read_input_tokens", 0) or 0
        self.cache_write += getattr(u, "cache_creation_input_tokens", 0) or 0

    def estimate_usd(self, *, in_rate: float = 5.0, out_rate: float = 25.0) -> float:
        """概算費用(USD)。既定は claude-opus-5 の単価。

        キャッシュ読み出しは約0.1倍、書き込みは約1.25倍で計算する。
        """
        return (
            self.input_tokens * in_rate
            + self.cache_read * in_rate * 0.1
            + self.cache_write * in_rate * 1.25
            + self.output_tokens * out_rate
        ) / 1_000_000


@dataclass
class JudgeConfig:
    keychain_service: str = DEFAULT_SERVICE
    allow_env: bool = True
    model: str = "claude-opus-5"
    extract_model: str = "claude-haiku-4-5"
    max_notice_chars: int = 120_000
    output_token_budget: int = 200_000
    effort: str = "high"


class Judge:
    """公告文にもとづく案件判定。"""

    def __init__(self, criteria_path: Path, config: JudgeConfig) -> None:
        if not criteria_path.is_file():
            raise LlmError(f"判定基準ファイルがありません: {criteria_path}")

        self.criteria = criteria_path.read_text(encoding="utf-8")
        self.config = config
        self.usage = Usage()
        self._Extraction, self._CaseVerdict, self._BatchVerdict = _schemas()
        self.client = make_client(
            keychain_service=config.keychain_service, allow_env=config.allow_env
        )

    @property
    def budget_exceeded(self) -> bool:
        return self.usage.output_tokens >= self.config.output_token_budget

    # --- 1段目: 原文抽出 --------------------------------------------------

    def extract(self, case: Case, notice: Notice):
        """公告文から判定に必要な箇所を原文のまま抜き出す。"""
        text = notice.text[: self.config.max_notice_chars]
        truncated = len(notice.text) > len(text)

        system = [
            {
                "type": "text",
                "text": (
                    "あなたは入札公告を読んで事実を抜き出す担当です。\n"
                    "公告文はPDFから機械的に抽出したテキストで、表のレイアウトが崩れています。\n"
                    "崩れた行から値を復元して構いませんが、**推測で値を作らないでください**。\n"
                    "書かれていない項目は null にしてください。\n"
                    "各項目の *_quote には、その値の根拠となる公告文の原文をそのまま入れてください"
                    "（言い換え・要約をしない）。"
                ),
                "cache_control": {"type": "ephemeral"},
            }
        ]

        user = (
            f"# 案件\n"
            f"案件名: {case.name}\n"
            f"発注機関: {case.agency}\n\n"
            f"# 公告文{'（長いため先頭を切り出し）' if truncated else ''}\n"
            f"{text}"
        )

        resp = self.client.messages.parse(
            model=self.config.extract_model,
            max_tokens=4000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=self._Extraction,
        )
        self.usage.add(resp.usage)
        return resp.parsed_output

    # --- 2段目: 判定 ------------------------------------------------------

    @staticmethod
    def _csv_only_block(case: Case) -> str:
        """公告文が無い案件の入力ブロック。

        NJSSの案件概要には予定価格が書かれていることがあるため、必ず含める。
        """
        return (
            f"## 案件ID: {case.case_id}\n"
            f"案件名: {case.name}\n"
            f"発注機関: {case.agency}（{case.agency_pref}）\n"
            f"入札形式: {case.bid_type}\n"
            f"NJSS掲載の入札資格: {' / '.join(case.qualification) or '(記載なし)'}\n"
            f"NJSS掲載の業種: {' / '.join(case.industry) or '(記載なし)'}\n"
            f"履行・納品場所: {' / '.join(case.delivery_place) or '(記載なし)'}\n"
            f"公示日: {case.announced_on} / 締切日: {case.deadline_on}\n"
            f"案件概要(NJSS): {case.summary or '(記載なし)'}\n"
            f"案件備考(NJSS): {case.note or '(記載なし)'}\n"
            "--- 公告文は取得できませんでした ---\n"
            "上記の情報だけで判定してください。"
            "公告文が無いことだけを理由に除外しないでください。\n"
        )

    @staticmethod
    def _extracted_block(case: Case, ex) -> str:
        """公告文から抽出できた案件の入力ブロック。"""
        price_note = "" if ex.price_is_public else "  ← 金額の記載なし（非公表）"
        return (
            f"## 案件ID: {case.case_id}\n"
            f"案件名: {case.name}\n"
            f"発注機関: {case.agency}（{case.agency_pref}）\n"
            f"入札形式: {case.bid_type}\n"
            f"NJSS掲載の入札資格: {' / '.join(case.qualification) or '(記載なし)'}\n"
            f"NJSS掲載の業種: {' / '.join(case.industry) or '(記載なし)'}\n"
            f"公示日: {case.announced_on} / 締切日: {case.deadline_on}\n"
            f"案件概要(NJSS): {case.summary or '(記載なし)'}\n"
            "--- 公告文からの抽出 ---\n"
            f"予定価格: {ex.planned_price or '(記載なし)'}{price_note}\n"
            f"予定価格(円): {ex.price_yen or '(不明)'}\n"
            f"入札参加資格: {ex.qualification or '(記載なし)'}\n"
            f"工期・納期: {ex.period or '(記載なし)'}\n"
            f"履行場所: {ex.place or '(記載なし)'}\n"
            f"工事種別: {ex.work_type or '(記載なし)'}\n"
            f"LED案件か: {ex.is_led} / 道路照明か: {ex.is_road_lighting} / "
            f"設計業務を含むか: {ex.includes_design} / ESCO関連か: {ex.is_esco}\n"
            f"業務内容: {ex.scope_summary}\n"
        )

    def judge(
        self, items: list[tuple[Case, Notice | None, object | None]]
    ) -> dict[str, Judgement]:
        """抽出結果を判定基準に照らして採否を決める。

        公告文を取得できなかった案件も含める（extraction が None）。
        CSVの情報だけで判定できるもの（LED案件でない、指名競争入札 など）を
        「資料未取得」で捨てないため。

        入力が小さいので全案件を1リクエストにまとめる。
        """
        if not items:
            return {}

        system = [
            {
                "type": "text",
                "text": (
                    "あなたは入札案件が自社の応札対象になるかを判定する担当です。\n"
                    "以下の判定基準に厳密に従ってください。\n"
                    "基準を満たすか判断する情報が不足している場合は、"
                    "推測で「採用」や「除外」にせず必ず「要確認」にしてください。\n\n"
                    "# 判定基準\n"
                    f"{self.criteria}"
                ),
                # 判定基準は毎回同じなのでキャッシュする
                "cache_control": {"type": "ephemeral"},
            }
        ]

        blocks = []
        for case, _notice, ex in items:
            if ex is None:
                blocks.append(self._csv_only_block(case))
                continue
            try:
                blocks.append(self._extracted_block(case, ex))
            except Exception as e:
                # 抽出結果の読み出しに失敗しても、その1件をCSVのみの判定に落とすだけにする。
                # ここで例外を投げるとバッチ全体の判定が失われる。
                log.error("[%s] 抽出結果を展開できません: %s", case.case_id, e)
                blocks.append(self._csv_only_block(case))

        user = (
            "以下の案件それぞれについて判定してください。"
            "案件IDは入力のものをそのまま返してください。\n\n" + "\n".join(blocks)
        )

        resp = self.client.messages.parse(
            model=self.config.model,
            max_tokens=8000,
            system=system,
            output_config={"effort": self.config.effort},
            messages=[{"role": "user", "content": user}],
            output_format=self._BatchVerdict,
        )
        self.usage.add(resp.usage)

        out: dict[str, Judgement] = {}
        by_id = {c.case_id: (c, n, e) for c, n, e in items}
        for v in resp.parsed_output.verdicts:
            triple = by_id.get(v.case_id)
            if triple is None:
                log.warning("入力に無い案件IDが返されました: %r", v.case_id)
                continue
            case, notice, ex = triple
            if ex is None:
                out[v.case_id] = Judgement(
                    case_id=v.case_id,
                    verdict=v.verdict,
                    score=v.score,
                    reason=v.reason,
                    extracted={
                        "工事種別": " / ".join(case.industry) or "",
                        "主な参加資格": " / ".join(case.qualification) or "",
                        "主な業務内容": (case.summary or "").replace("\n", " ")[:200],
                    },
                    source="llm",
                    source_url=None,
                )
                continue
            out[v.case_id] = Judgement(
                case_id=v.case_id,
                verdict=v.verdict,
                score=v.score,
                reason=v.reason,
                extracted={
                    k: val
                    for k, val in {
                        "予定価格": (
                            ex.planned_price
                            if ex.price_is_public
                            else "非公表（金額の記載なし）"
                        ),
                        "工事種別": ex.work_type,
                        "主な参加資格": ex.qualification,
                        "主な業務内容": ex.scope_summary,
                        "工期・納期": ex.period,
                        "履行場所": ex.place,
                    }.items()
                    if val
                },
                evidence={
                    k: val
                    for k, val in {
                        "予定価格": ex.planned_price_quote,
                        "主な参加資格": ex.qualification_quote,
                    }.items()
                    if val
                },
                source="llm",
                source_url=notice.source_url,
            )

        missing = set(by_id) - set(out)
        for cid in missing:
            log.warning("判定結果が返らなかった案件: %s", cid)
            out[cid] = Judgement(
                case_id=cid,
                verdict="要確認",
                reason="LLMから判定結果が返りませんでした。人の確認が必要です。",
                source="llm",
            )
        return out
