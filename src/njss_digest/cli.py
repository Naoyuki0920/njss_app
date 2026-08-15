"""コマンドラインインタフェース。"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import ConfigError, load_settings
from .enrich import fetch_notices
from .ingest import IngestError, read_csv
from .kkj import KkjClient, KkjError
from .mapping import MappingError, load_mapping, to_cases
from .pipeline import RunOptions
from .pipeline import run as pipeline_run
from .rules import RulesError, load_rules
from .watch import notify as watch_notify
from .watch import watch

DEFAULT_MAPPING = Path("config/csv_mapping.yaml")
DEFAULT_SETTINGS = Path("config/settings.yaml")
DEFAULT_RULES = Path("config/rules.yaml")
DEFAULT_CRITERIA = Path("config/criteria.md")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
        stream=sys.stderr,
    )
    # httpx はリクエストURLをINFOで記録する。SlackのIncoming Webhookは
    # URL自体が秘密なので、そのままではログファイルに秘密が残ってしまう。
    # 通信ログが必要なときは -v ではなく httpx のロガーを個別に上げること。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _load(csv_path: Path, mapping_path: Path):
    m = load_mapping(mapping_path)
    raw = read_csv(csv_path, m.encoding_candidates, m.has_header)
    return m, raw


def cmd_inspect(args: argparse.Namespace) -> int:
    """CSVの列構成を人が読める形で表示する。

    config/csv_mapping.yaml を書く/直すための補助。
    マッピングの適用も試み、自己診断の結果を出す。
    """
    m, raw = _load(args.csv, args.mapping)

    print(f"ファイル : {raw.path}")
    print(f"文字コード: {raw.encoding}")
    print(f"データ行数: {raw.row_count}  (ヘッダ行: {'あり' if m.has_header else 'なし'})")
    print(f"列数     : {sorted(raw.column_counts)}")
    print()

    by_index = {c.index: c for c in m.columns}
    width = max(len(c.label) for c in m.columns) if m.columns else 10

    print("=== 列ごとの内容 ===")
    ncols = max(raw.column_counts)
    for i in range(ncols):
        vals = [r[i] if i < len(r) else "" for r in raw.rows]
        nonempty = [v.strip() for v in vals if v.strip()]
        spec = by_index.get(i)
        label = spec.label if spec else "(未マッピング)"
        field = f" -> {spec.field}" if spec else ""

        if not nonempty:
            print(f"[{i:2}] {label:<{width}} 全行空")
            continue

        print(f"[{i:2}] {label:<{width}}{field}  非空 {len(nonempty)}/{len(vals)}")
        for v in nonempty[:2]:
            shown = v.replace("\n", " / ")
            print(f"       {shown[:100]}")

    print()
    print("=== マッピング適用と自己診断 ===")
    try:
        cases, report = to_cases(raw, m)
    except MappingError as e:
        print("NG 失敗しました:\n", e)
        return 1

    for name, passed, detail in report.checks:
        print(f"  {'OK' if passed else 'NG'} {name}: {detail}")
    if report.date_parse_failures:
        print(f"  ! 日付パース失敗: {report.date_parse_failures}")
    if report.unmapped_nonempty:
        print("  ! 未マッピングだが値がある列:")
        for i, sample in sorted(report.unmapped_nonempty.items()):
            print(f"      [{i}] {sample}")

    print()
    print(f"=== Case に変換: {len(cases)}件 ===")
    for c in cases[: args.limit]:
        print(f"- [{c.case_id}] {c.name}")
        print(f"    機関: {c.agency} ({c.agency_pref}) / 形式: {c.bid_type}")
        print(f"    公示: {c.announced_on} / 締切: {c.deadline_on} / 資料提出: {c.docs_due_on}")
        print(f"    等級: {sorted(c.grades) or '(なし)'} / 業種: {c.industry}")
        print(f"    概要あり: {c.has_summary}")
    return 0


def cmd_coverage(args: argparse.Namespace) -> int:
    """官公需APIで公告文をどれだけ取得できるかを測定する。

    L3(官公需API)が実用に足るかを判断するためのコマンド。
    ヒット率が低ければ L4(Web検索) の比重を上げる設計に切り替える。
    """
    m, raw = _load(args.csv, args.mapping)
    cases, _ = to_cases(raw, m)

    print(f"対象: {len(cases)}件  ({raw.path.name})")
    print(f"突合しきい値: {args.threshold}  公示日の前後: ±{args.window}日")
    print()

    with KkjClient(min_interval=args.interval) as client:
        result = fetch_notices(
            cases, client, window_days=args.window, threshold=args.threshold
        )

    s = result.stats
    print()
    print("=== 突合結果 ===")
    for c in cases:
        match = result.matches.get(c.case_id)
        notice = result.notices.get(c.case_id)
        if match is None:
            print(f"  --   [{c.case_id}] {c.name[:44]}")
            print(f"       {c.agency} — 官公需ポータルに該当なし → L4(Web検索)へ")
            continue
        chars = len(notice.text) if notice else 0
        print(f"  {match.method:<5}[{c.case_id}] {c.name[:44]}  (類似度 {match.score:.2f})")
        print(f"       -> {match.result.project_name[:60]}")
        print(f"       公告文 {chars:,}字 / 添付 {len(match.result.attachments)}件")
        if chars == 0:
            print("       ! 公告文が空。判定には使えないため L4 送りが妥当")

    print()
    print("=== 集計 ===")
    print(f"  完全一致 : {s.exact}")
    print(f"  類似一致 : {s.fuzzy}")
    print(f"  該当なし : {s.missed}")
    print(f"  ヒット率 : {s.hit}/{s.total} = {s.hit_rate:.0%}")
    print(f"  API呼出  : {s.api_calls}回 (エラー {s.api_errors}回)")

    usable = sum(
        1
        for cid, n in result.notices.items()
        if len(n.text) >= args.min_chars
    )
    print(f"  判定に使える公告文({args.min_chars:,}字以上): {usable}/{s.total} = {usable / s.total:.0%}")
    print()
    if usable / s.total >= 0.5 if s.total else False:
        print("判定: L3(官公需API)は主経路として使えます。")
    else:
        print("判定: L3のカバレッジが低いため、L4(Web検索)を主経路にする必要があります。")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """CSVを読み込んで判定し、メール下書きを作る。"""
    settings = load_settings(args.settings)
    mapping = load_mapping(args.mapping)
    rules = load_rules(args.rules)

    options = RunOptions(
        dry_run=args.dry_run,
        use_llm=not args.no_llm,
        use_kkj=not args.no_kkj,
        use_web=not args.no_web,
        include_seen=args.include_seen,
        files=[args.file] if args.file else [],
    )

    result = pipeline_run(
        settings, mapping, rules, options, criteria_path=args.criteria
    )

    print()
    print(result.text_body)
    print()
    o = result.output
    if o and o.text_path:
        print(f"貼り付け用テキスト: {o.text_path}")
    if o and o.slack_posted:
        print("Slack: 投稿しました")
    elif o and o.slack_skipped_reason:
        print(f"Slack: 投稿していません（{o.slack_skipped_reason}）")
    if args.dry_run:
        print("(dry-run のため成果物は保存していません)")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """監視ディレクトリを見張り、CSVが置かれたら自動で run を実行する。"""
    settings = load_settings(args.settings)
    mapping = load_mapping(args.mapping)
    rules = load_rules(args.rules)
    settings.ensure_dirs()

    def on_csv(path: Path) -> None:
        result = pipeline_run(
            settings,
            mapping,
            rules,
            RunOptions(files=[path]),
            criteria_path=args.criteria,
        )
        s = result.stats
        watch_notify(
            "NJSS digest",
            f"{path.name}: 掲載{s.by_verdict.get('採用', 0)}件 "
            f"要確認{s.by_verdict.get('要確認', 0) + s.by_verdict.get('資料未取得', 0)}件 "
            f"除外{s.by_verdict.get('除外', 0)}件",
        )

    watch(settings.watch_dir, settings.csv_pattern, on_csv)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="njss-digest", description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true", help="デバッグログを出す")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("inspect", help="CSVの列構成を表示して列マッピングを検証する")
    sp.add_argument("csv", type=Path, help="NJSSからダウンロードしたCSV")
    sp.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    sp.add_argument("--limit", type=int, default=5, help="表示する案件数")
    sp.set_defaults(func=cmd_inspect)

    sp = sub.add_parser(
        "coverage", help="官公需APIで公告文をどれだけ取得できるか測定する"
    )
    sp.add_argument("csv", type=Path, help="NJSSからダウンロードしたCSV")
    sp.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    sp.add_argument("--threshold", type=float, default=0.70, help="案件名の類似度しきい値")
    sp.add_argument("--window", type=int, default=14, help="公示日の前後何日を検索対象にするか")
    sp.add_argument("--interval", type=float, default=1.0, help="APIリクエストの最小間隔(秒)")
    sp.add_argument(
        "--min-chars", type=int, default=500, help="判定に使える公告文とみなす最小文字数"
    )
    sp.set_defaults(func=cmd_coverage)

    sp = sub.add_parser("run", help="CSVを判定してメール下書きを作る")
    sp.add_argument("--file", type=Path, help="対象CSVを明示指定する（既定は監視ディレクトリを走査）")
    sp.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    sp.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    sp.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    sp.add_argument("--criteria", type=Path, default=DEFAULT_CRITERIA)
    sp.add_argument(
        "--dry-run",
        action="store_true",
        help="判定のみ。下書きを作らず、DBも更新せず、CSVも移動しない",
    )
    sp.add_argument("--no-llm", action="store_true", help="LLM判定を使わない（L2まで）")
    sp.add_argument("--no-kkj", action="store_true", help="官公需APIを使わない")
    sp.add_argument("--no-web", action="store_true", help="Web検索フォールバックを使わない")
    sp.add_argument(
        "--include-seen", action="store_true", help="過去に判定済みの案件も対象に含める"
    )
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("watch", help="監視ディレクトリを見張り、CSV検知で自動実行する")
    sp.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    sp.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    sp.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    sp.add_argument("--criteria", type=Path, default=DEFAULT_CRITERIA)
    sp.set_defaults(func=cmd_watch)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except (IngestError, MappingError, KkjError, ConfigError, RulesError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
