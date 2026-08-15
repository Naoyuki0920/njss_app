"""実行パイプライン。各層を順に通して digest を作る。

  L0 CSV読込  ->  重複排除  ->  L2 ルール判定
     -> L3 官公需APIで公告文取得 -> L4 Web検索フォールバック
     -> LLM抽出・判定 -> digest生成 -> 下書き出力

各層は前段の結果を減らしていく。ルールで除外できた案件は公告文の取得もLLM判定も
行わないため、rules.yaml を詰めるほど実行時間とAPIコストが下がる。
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .compose import (
    DigestItem,
    DigestStats,
    build_subject,
    render_mail_text,
    render_slack,
    render_text,
)
from .config import Settings
from .enrich import fetch_notices
from .ingest import find_csv_files, read_csv
from .kkj import KkjClient
from .llm import Judge, JudgeConfig, LlmUnavailable
from .localdocs import collect as collect_local_pdfs
from .mapping import Mapping, to_cases
from .models import Case, Judgement, Notice
from .output import OutputResult, deliver
from .rules import Rules
from .store import Store
from .websearch import WebNoticeFinder, WebSearchConfig

log = logging.getLogger(__name__)


@dataclass
class RunOptions:
    dry_run: bool = False
    use_llm: bool = True
    use_kkj: bool = True
    use_web: bool = True
    include_seen: bool = False
    files: list[Path] = field(default_factory=list)
    today: date = field(default_factory=date.today)


@dataclass
class RunResult:
    items: list[DigestItem]
    stats: DigestStats
    subject: str
    text_body: str      # 画面表示用（本文のみ）
    mail_text: str      # 貼り付け用（件名＋本文）
    slack_text: str     # Slack投稿用（mrkdwn）
    output: OutputResult | None = None


def run(
    settings: Settings,
    mapping: Mapping,
    rules: Rules,
    options: RunOptions,
    *,
    criteria_path: Path,
) -> RunResult:
    settings.ensure_dirs()
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")

    paths = options.files or find_csv_files(
        settings.watch_dir,
        name_pattern=settings.csv_pattern,
        processed_dir=settings.processed_dir,
    )
    if not paths:
        log.warning(
            "処理対象のCSVがありません（%s の %s）", settings.watch_dir, settings.csv_pattern
        )

    stats = DigestStats(csv_files=[p.name for p in paths])

    # --- L0: 読み込み ---------------------------------------------------
    all_cases: list[Case] = []
    for p in paths:
        raw = read_csv(p, mapping.encoding_candidates, mapping.has_header)
        cases, _ = to_cases(raw, mapping)
        all_cases.extend(cases)
        log.info("%s から %d件", p.name, len(cases))

    # 同一実行内の重複（複数の検索条件に同じ案件が現れる）を先に潰す
    unique: dict[str, Case] = {}
    for c in all_cases:
        unique.setdefault(c.case_id, c)
    cases = list(unique.values())
    if len(all_cases) != len(cases):
        log.info("実行内の重複を除外: %d件 -> %d件", len(all_cases), len(cases))

    with Store(settings.db_path, settings.notices_dir) as store:
        store.start_run(run_id, [p.name for p in paths])

        # --- 過去に見た案件を除外 --------------------------------------
        if not options.include_seen:
            known = store.known_case_ids([c.case_id for c in cases])
            if known:
                log.info("既知の案件を除外: %d件", len(known))
            cases = [c for c in cases if c.case_id not in known]

        if not options.dry_run:
            for c in cases:
                store.upsert_case(c)

        stats.total = len(cases)
        log.info("判定対象: %d件", stats.total)

        judgements: dict[str, Judgement] = {}
        notices: dict[str, Notice] = {}

        # --- L2: ルール判定 ---------------------------------------------
        pending: list[Case] = []
        for c in cases:
            hit = rules.evaluate(c, options.today)
            if hit is None:
                pending.append(c)
                continue
            judgements[c.case_id] = Judgement(
                case_id=c.case_id,
                verdict=hit.outcome,
                score=90 if hit.outcome == "採用" else 0,
                reason=hit.reason,
                source="rules",
            )
            if hit.outcome == "除外":
                stats.excluded_by_rules[hit.reason] = (
                    stats.excluded_by_rules.get(hit.reason, 0) + 1
                )
        log.info(
            "ルール判定: 確定 %d件 / 公告文が必要 %d件",
            len(judgements),
            len(pending),
        )

        # --- 人が置いた公示書PDFを最優先で取り込む ------------------------
        # 予定価格はNJSSの案件詳細に表示されず、公示書PDFにしか無いことが多い。
        # NJSSへの自動アクセスは規約で禁止されているため、取得は人が行い、
        # 読み取りだけを自動化する。
        pending_ids = {c.case_id for c in pending}
        # 判定に使える資料の基準を官公需API経由のものと揃える
        for cid, notice in collect_local_pdfs(
            settings.pdf_inbox, cases=pending, min_chars=settings.min_notice_chars
        ).items():
            if cid not in pending_ids:
                log.info("[%s] 判定対象にない案件のPDFです（スキップ）", cid)
                continue
            notices[cid] = notice
            if not options.dry_run:
                store.save_notice(notice)

        # --- L3: 官公需APIで公告文取得 -----------------------------------
        need_notice = [
            c for c in pending
            if c.case_id not in notices and store.get_notice(c.case_id) is None
        ]
        for c in pending:
            if c.case_id in notices:
                continue
            cached = store.get_notice(c.case_id)
            if cached is not None:
                notices[c.case_id] = cached
                log.debug("[%s] 公告文をキャッシュから取得", c.case_id)

        if options.use_kkj and settings.kkj.enabled and need_notice:
            with KkjClient(min_interval=settings.kkj.min_interval) as client:
                enriched = fetch_notices(
                    need_notice,
                    client,
                    window_days=settings.kkj.window_days,
                    threshold=settings.kkj.threshold,
                )
            for cid, n in enriched.notices.items():
                if len(n.text) >= settings.min_notice_chars:
                    notices[cid] = n
                    if not options.dry_run:
                        store.save_notice(n)
                else:
                    log.info("[%s] 官公需APIの公告文が短すぎます(%d字)", cid, len(n.text))

        # --- L4: Web検索フォールバック -----------------------------------
        missing = [c for c in pending if c.case_id not in notices]
        if options.use_web and settings.websearch.enabled and missing and options.use_llm:
            try:
                finder = WebNoticeFinder(
                    WebSearchConfig(
                        keychain_service=settings.llm.api_key_keychain_service,
                        allow_env=settings.llm.allow_env_api_key,
                        model=settings.websearch.model,
                        effort=settings.websearch.effort,
                        max_content_tokens=settings.websearch.max_content_tokens,
                        allowed_domains=tuple(settings.websearch.allowed_domains),
                        max_uses=settings.websearch.max_uses,
                    )
                )
            except LlmUnavailable as e:
                log.warning("Web検索フォールバックを使えません: %s", e)
            else:
                log.info("Web検索フォールバック: %d件", len(missing))
                for c in missing:
                    n = finder.find(c)
                    if n and len(n.text) >= settings.min_notice_chars:
                        notices[c.case_id] = n
                        if not options.dry_run:
                            store.save_notice(n)
                stats.api_cost_usd += finder.usage.estimate_usd()
                stats.llm_calls += finder.usage.calls

        for n in notices.values():
            if n.source == "manual_pdf":
                stats.notice_from_pdf += 1
            elif n.source == "kkj":
                stats.notice_from_kkj += 1
            else:
                stats.notice_from_web += 1
        stats.notice_missing = len([c for c in pending if c.case_id not in notices])

        # --- LLM: 抽出と判定 ---------------------------------------------
        judged_ok: list[tuple[Case, Notice | None, object | None]] = []
        if options.use_llm and settings.llm.enabled:
            try:
                judge = Judge(
                    criteria_path,
                    JudgeConfig(
                        keychain_service=settings.llm.api_key_keychain_service,
                        allow_env=settings.llm.allow_env_api_key,
                        model=settings.llm.model,
                        extract_model=settings.llm.extract_model,
                        max_notice_chars=settings.llm.max_notice_chars,
                        output_token_budget=settings.llm.daily_output_token_budget,
                        effort=settings.llm.effort,
                    ),
                )
            except LlmUnavailable as e:
                log.warning("LLM判定を実行できません: %s", e)
                judge = None
        else:
            judge = None

        if judge is not None:
            for c in pending:
                n = notices.get(c.case_id)
                if n is None:
                    # 公告文が無くてもCSVの情報だけで判定できることがある
                    # （LED案件でない / 指名競争入札 など）。判定対象から外さない。
                    judged_ok.append((c, None, None))
                    continue
                if judge.budget_exceeded:
                    stats.budget_exceeded = True
                    log.warning("トークン上限に達したため [%s] の判定を打ち切ります", c.case_id)
                    break
                try:
                    ex = judge.extract(c, n)
                except Exception as e:  # 1件の失敗で全体を止めない
                    # 抽出に失敗しても判定対象から外さない。CSVの情報だけで
                    # 判定できることがあるため（LED案件でない、指名競争入札 など）。
                    log.error(
                        "[%s] 公告文の抽出に失敗: %s — CSVの情報のみで判定します",
                        c.case_id,
                        e,
                    )
                    judged_ok.append((c, n, None))
                    continue
                judged_ok.append((c, n, ex))

            if judged_ok:
                try:
                    judgements.update(judge.judge(judged_ok))
                except Exception as e:
                    log.error("判定に失敗しました: %s", e)
            stats.api_cost_usd += judge.usage.estimate_usd()
            stats.llm_calls += judge.usage.calls

        # --- 判定が付かなかったものは「資料未取得」------------------------
        for c in pending:
            if c.case_id in judgements:
                continue
            n = notices.get(c.case_id)
            if n is None:
                reason = "公告文を官公需API・Web検索のいずれでも取得できませんでした"
            elif judge is None:
                reason = "LLM判定が無効のため未判定です"
            else:
                reason = "公告文は取得できましたが判定できませんでした"
            judgements[c.case_id] = Judgement(
                case_id=c.case_id,
                verdict="資料未取得",
                reason=reason + "。NJSSの案件詳細で直接ご確認ください。",
                source="none",
                source_url=n.source_url if n else None,
            )

        # --- 集計と保存 ---------------------------------------------------
        by_id = {c.case_id: c for c in cases}
        items = [
            DigestItem(
                case=by_id[cid], judgement=j, notice=notices.get(cid)
            )
            for cid, j in judgements.items()
            if cid in by_id
        ]
        for it in items:
            v = it.judgement.verdict
            stats.by_verdict[v] = stats.by_verdict.get(v, 0) + 1
            if v == "除外" and it.judgement.source == "llm":
                r = it.judgement.reason or "（理由なし）"
                stats.excluded_by_llm[r] = stats.excluded_by_llm.get(r, 0) + 1
            if not options.dry_run:
                store.save_verdict(run_id, it.judgement)

        store.finish_run(
            run_id,
            {
                "total": stats.total,
                "by_verdict": stats.by_verdict,
                "notice_from_kkj": stats.notice_from_kkj,
                "notice_from_web": stats.notice_from_web,
                "notice_missing": stats.notice_missing,
                "llm_calls": stats.llm_calls,
                "api_cost_usd": round(stats.api_cost_usd, 4),
                "dry_run": options.dry_run,
            },
        )

    # --- digest生成 --------------------------------------------------------
    # 「除外」はメールに載せない（ノイズになるため）。件数だけサマリに出す。
    visible = [it for it in items if it.judgement.verdict != "除外"]
    subject = build_subject(settings.output.subject_template, stats, options.today)
    text_body = render_text(visible, stats, options.today)
    mail_text = render_mail_text(visible, stats, options.today, subject)
    slack_text = render_slack(visible, stats, options.today)

    result = RunResult(
        items=items,
        stats=stats,
        subject=subject,
        text_body=text_body,
        mail_text=mail_text,
        slack_text=slack_text,
    )

    # 対象案件が1件も無い場合は成果物を作らない。
    # CSVが無い、または新着が無い日の実行で、空のdigestがSlackに流れ続けるのを防ぐ。
    # （案件を処理した結果すべて除外された場合は total > 0 なので通常どおり出力する）
    if stats.total == 0:
        log.info(
            "新規の案件がないため、テキスト出力とSlack投稿は行いません"
            "（CSV %d件を処理）",
            len(paths),
        )
        result.output = OutputResult(slack_skipped_reason="新規案件なし")
        if not options.dry_run:
            _archive_csvs(paths, settings.processed_dir)
        return result

    result.output = deliver(
        mail_text=mail_text,
        slack_text=slack_text,
        drafts_dir=settings.drafts_dir,
        today=options.today,
        write_text_file=settings.output.text,
        clipboard=settings.output.clipboard,
        reveal_in_finder=settings.output.reveal_in_finder,
        slack=settings.output.slack,
        slack_confirm=settings.output.slack_confirm,
        dry_run=options.dry_run,
    )

    if options.dry_run:
        log.info("dry-run のため成果物は保存せず、CSVも移動しません")
        return result

    _archive_csvs(paths, settings.processed_dir)
    return result


def _archive_csvs(paths: list[Path], processed_dir: Path) -> None:
    """処理済みCSVを退避する。次回実行時に再処理しないため。"""
    for p in paths:
        dest = processed_dir / p.name
        try:
            shutil.move(str(p), str(dest))
            log.info("処理済みへ移動: %s", dest)
        except OSError as e:
            log.warning("CSVを移動できませんでした: %s", e)
