"""成果物の出力。

このPCからメールは送らない。社内OAに接続できる別PCから送信するため、
**貼り付け用のテキストファイル**を出すのが主目的。

  text  : var/drafts/*.txt      件名＋本文。メールソフトに貼り付ける
  slack : Slackチャンネルへ投稿   （任意・要設定）

Slackへの投稿は「送信」にあたるため、既定では確認プロンプトを挟む。
自動化する場合は設定の output.slack.confirm を false にする。
"""

from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .slack import SlackConfig, SlackError, SlackPoster

log = logging.getLogger(__name__)


class OutputError(Exception):
    pass


@dataclass
class OutputResult:
    text_path: Path | None = None
    slack_posted: bool = False
    slack_skipped_reason: str | None = None


def write_text(body: str, drafts_dir: Path, today: date) -> Path:
    """貼り付け用のテキストを保存する。

    同じ日に複数回実行しても上書きにならないよう連番を付ける。
    前回の内容を消してしまわないため。
    """
    drafts_dir.mkdir(parents=True, exist_ok=True)
    base = today.strftime("%Y-%m-%d")
    path = drafts_dir / f"njss-digest_{base}.txt"
    n = 2
    while path.exists():
        path = drafts_dir / f"njss-digest_{base}_{n}.txt"
        n += 1
    path.write_text(body, encoding="utf-8")
    log.info("テキストを保存しました: %s", path)
    return path


def copy_to_clipboard(text: str) -> bool:
    """macOSのクリップボードへコピーする。失敗しても処理は続ける。"""
    try:
        subprocess.run(
            ["pbcopy"], input=text.encode("utf-8"), check=True, timeout=15
        )
    except Exception as e:
        log.debug("クリップボードへコピーできませんでした: %s", e)
        return False
    log.info("クリップボードにコピーしました")
    return True


def reveal(path: Path) -> None:
    """Finderで場所を開く（テキストを取り出しやすくするため）。"""
    try:
        subprocess.run(["open", "-R", str(path)], check=False, capture_output=True)
    except Exception as e:
        log.debug("Finderで開けませんでした: %s", e)


def _confirm(prompt: str) -> bool:
    """対話的に確認する。非対話（cronやlaunchd）では投稿しない。"""
    if not sys.stdin.isatty():
        log.warning("非対話実行のためSlack投稿を見送りました（confirm が有効）")
        return False
    try:
        answer = input(f"{prompt} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


def deliver(
    *,
    mail_text: str,
    slack_text: str,
    drafts_dir: Path,
    today: date,
    write_text_file: bool = True,
    clipboard: bool = False,
    reveal_in_finder: bool = False,
    slack: SlackConfig | None = None,
    slack_confirm: bool = True,
    dry_run: bool = False,
) -> OutputResult:
    result = OutputResult()

    if write_text_file and not dry_run:
        result.text_path = write_text(mail_text, drafts_dir, today)
        if clipboard:
            copy_to_clipboard(mail_text)
        if reveal_in_finder:
            reveal(result.text_path)

    if slack is None or not slack.enabled:
        result.slack_skipped_reason = "設定で無効"
        return result

    if dry_run:
        result.slack_skipped_reason = "dry-run"
        log.info("dry-run のためSlackには投稿しません")
        return result

    if slack_confirm and not _confirm(f"Slack（{slack.channel or 'Webhook先'}）に投稿しますか？"):
        result.slack_skipped_reason = "確認で見送り"
        log.info("Slackへの投稿を見送りました")
        return result

    try:
        with SlackPoster(slack) as poster:
            poster.post(slack_text)
        result.slack_posted = True
        log.info("Slackへ投稿しました")
    except SlackError as e:
        # 投稿に失敗してもテキストは残っているので、処理は続行する
        result.slack_skipped_reason = str(e)
        log.error("Slackへの投稿に失敗しました: %s", e)

    return result
