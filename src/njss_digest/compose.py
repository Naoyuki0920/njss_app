"""digest（チーム配信用メール本文）の組み立て。

案件ごとの表示は運用中のフォーマットに合わせている:

    ------
    【業務名】
    【リンク】
    【入札形式】
    【公示日】
    【入札日】
    【予定価格】
    【その他】工事種別と主な参加資格、主な業務内容の3点のサマリ
    ------

除外条件に該当した案件は本文に載せない（件数と内訳だけサマリに出す）。
ただし「公告文が取れず判定できなかった案件」は載せる。黙って落とすと
取りこぼしが見えなくなるため、【要確認】として別セクションに置く。

公告書・仕様書の全文転載はしない。要約と一次情報へのリンクに留める
（NJSS利用規約 第18条1項(2)(19) の「内部利用の範囲」を明確に超えないため）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .models import Case, Judgement, Notice

SEPARATOR = "-" * 30

# 【その他】に並べる3点。抽出できなかった項目は「—」で埋める。
OTHER_FIELDS = ("工事種別", "主な参加資格", "主な業務内容")


@dataclass
class DigestStats:
    total: int = 0
    by_verdict: dict[str, int] = field(default_factory=dict)
    notice_from_pdf: int = 0
    notice_from_kkj: int = 0
    notice_from_web: int = 0
    notice_missing: int = 0
    excluded_by_rules: dict[str, int] = field(default_factory=dict)
    excluded_by_llm: dict[str, int] = field(default_factory=dict)
    csv_files: list[str] = field(default_factory=list)
    api_cost_usd: float = 0.0
    llm_calls: int = 0
    budget_exceeded: bool = False


@dataclass
class DigestItem:
    case: Case
    judgement: Judgement
    notice: Notice | None = None


def _fmt_date(d: date | None) -> str:
    return d.strftime("%Y/%m/%d") if d else "—"


def _days_note(case: Case, today: date) -> str:
    d = case.days_until_deadline(today)
    if d is None:
        return ""
    return "（締切済み）" if d < 0 else f"（あと{d}日）"


def _price(item: DigestItem) -> str:
    """【予定価格】欄の値。

    取得できなかった場合は空欄にせず、その理由を書く。
    金額が無いことと、調べられなかったことは意味が違うため。
    """
    v = item.judgement.extracted.get("予定価格")
    if v:
        return v
    if item.notice is None:
        return "取得できず（NJSSの案件詳細でご確認ください）"
    return "公告文に記載なし"


def _other(item: DigestItem) -> str:
    parts = [f"{k}: {item.judgement.extracted.get(k) or '—'}" for k in OTHER_FIELDS]
    return " ／ ".join(parts)


def build_subject(template: str, stats: DigestStats, today: date) -> str:
    return template.format(
        date=today.strftime("%Y-%m-%d"),
        adopt=stats.by_verdict.get("採用", 0),
        review=stats.by_verdict.get("要確認", 0),
        missing=stats.by_verdict.get("資料未取得", 0),
        total=stats.total,
    )


def _summary_lines(stats: DigestStats) -> list[str]:
    adopt = stats.by_verdict.get("採用", 0)
    review = stats.by_verdict.get("要確認", 0) + stats.by_verdict.get("資料未取得", 0)
    excluded = stats.by_verdict.get("除外", 0)

    lines = [
        f"対象 {stats.total}件 → 掲載 {adopt}件 / 要確認 {review}件 / 除外 {excluded}件",
        f"公告文の取得: 公示書PDF {stats.notice_from_pdf}件"
        f" / 官公需API {stats.notice_from_kkj}件"
        f" / Web検索 {stats.notice_from_web}件"
        f" / 取得できず {stats.notice_missing}件",
    ]
    reasons = {**stats.excluded_by_rules, **stats.excluded_by_llm}
    if reasons:
        top = sorted(reasons.items(), key=lambda kv: kv[1], reverse=True)[:5]
        lines.append("除外の内訳: " + " / ".join(f"{r}（{n}件）" for r, n in top))
    if stats.budget_exceeded:
        lines.append(
            "※ LLMのトークン上限に達したため、一部の案件は判定していません。"
        )
    return lines


def _case_block_text(item: DigestItem, today: date) -> list[str]:
    c = item.case
    return [
        SEPARATOR,
        f"【業務名】{c.name}",
        f"【リンク】{c.url}",
        f"【入札形式】{c.bid_type or '—'}",
        f"【公示日】{_fmt_date(c.announced_on)}",
        f"【入札日】{_fmt_date(c.deadline_on)} {_days_note(c, today)}".rstrip(),
        f"【予定価格】{_price(item)}",
        f"【その他】{_other(item)}",
    ]


def render_text(items: list[DigestItem], stats: DigestStats, today: date) -> str:
    out: list[str] = [f"NJSS 入札案件 {today.strftime('%Y/%m/%d')}", ""]
    out += _summary_lines(stats)
    out.append("")

    by_verdict: dict[str, list[DigestItem]] = {}
    for it in items:
        by_verdict.setdefault(it.judgement.verdict, []).append(it)

    def sorted_group(name: str) -> list[DigestItem]:
        return sorted(
            by_verdict.get(name, []), key=lambda i: i.judgement.score, reverse=True
        )

    adopted = sorted_group("採用")
    out.append(f"■ 掲載案件（{len(adopted)}件）")
    out.append("")
    if not adopted:
        out.append("該当なし")
        out.append("")
    for it in adopted:
        out += _case_block_text(it, today)
        out.append(SEPARATOR)
        out.append("")

    review = sorted_group("要確認") + sorted_group("資料未取得")
    if review:
        out.append("")
        out.append(f"■ 要確認（{len(review)}件） — 自動判定できなかった案件")
        out.append("　 予定価格などをNJSSの案件詳細でご確認ください。")
        out.append("")
        for it in review:
            out += _case_block_text(it, today)
            out.append(f"【要確認の理由】{it.judgement.reason}")
            out.append(SEPARATOR)
            out.append("")

    out.append("")
    out.append(f"元CSV: {', '.join(stats.csv_files) or '(なし)'}")
    out.append(
        f"LLM呼び出し {stats.llm_calls}回 / 概算費用 約 ${stats.api_cost_usd:.2f}"
    )
    out.append(
        "※ 本メールは要約です。応札判断の前にNJSSの案件詳細と公告文の原本をご確認ください。"
    )
    return "\n".join(out)


def render_mail_text(
    items: list[DigestItem], stats: DigestStats, today: date, subject: str
) -> str:
    """社内OAのメールソフトへ貼り付けるためのテキスト。

    件名と本文を1ファイルにまとめる。このPCから送信はしないので、
    宛先の埋め込みや送信可能な形式（.eml）にはしない。
    """
    return "\n".join(
        [
            "=" * 60,
            f"件名: {subject}",
            "=" * 60,
            "",
            render_text(items, stats, today),
        ]
    )


def _slack_escape(s: str) -> str:
    """Slackが制御文字として扱う & < > を無害化する。

    Slack側で元の文字に戻して表示されるため、見た目は変わらない。
    案件名に「&」や「<」が含まれていても崩れないようにするための処理。
    """
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_slack(items: list[DigestItem], stats: DigestStats, today: date) -> str:
    """Slack投稿用の本文。

    ターミナルに表示される内容（render_text）と同一の形式にしている。
    貼り付け用テキスト・画面表示・Slackで見た目が食い違うと、
    どれが正なのか分からなくなるため、意図的に1つの形式に揃えている。

    装飾（太字・絵文字・案件名へのリンク埋め込み）は行わない。
    【リンク】行のURLはSlackが自動でリンク化する。
    """
    return _slack_escape(render_text(items, stats, today))
