"""Slackへの投稿。

2方式に対応する。

  webhook : Incoming Webhook のURLにPOSTする。設定が最も簡単。
            ただし**URL自体が秘密**（URLを知っていれば誰でも投稿できる）なので、
            Keychainに保管し、設定ファイルには書かない。
  bot     : ボットトークン(xoxb-)で chat.postMessage を呼ぶ。
            投稿先チャンネルを実行時に選べ、スレッド返信もできる。
            トークンはAuthorizationヘッダで送るため、URLに秘密が乗らない。

digestは長くなるためSlackの1メッセージ上限に収まらないことがある。
案件の区切りで分割し、2通目以降はスレッド返信にしてチャンネルを埋めないようにする。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import httpx

from .secrets import keychain_get

log = logging.getLogger(__name__)

# httpx はリクエストURLをINFOで出力する。Incoming Webhook は
# URL自体が秘密のため、このモジュールを読み込んだ時点で抑止しておく
# （CLI以外から使われた場合の保険）。
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def redact(text: str) -> str:
    """Webhook URLなどの秘密がメッセージに混じった場合に伏せる。"""
    return re.sub(
        r"https://hooks\.slack\.com/services/\S+",
        "https://hooks.slack.com/services/***",
        text,
    )

# Slackのtextフィールドの上限は40,000字だが、実用上は読みやすさのため短く区切る。
# 案件ブロック単位で切るので厳密な上限ではなく目安。
CHUNK_LIMIT = 2800

POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"


class SlackError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class SlackConfig:
    enabled: bool = False
    mode: str = "webhook"  # webhook | bot
    channel: str = ""
    keychain_service: str = "njss-digest-slack"
    username: str = ""
    icon_emoji: str = ""


def setup_hint(cfg: SlackConfig) -> str:
    if cfg.mode == "bot":
        what = "ボットトークン（xoxb- で始まる文字列）"
        extra = (
            "  Slack App に chat:write スコープを付与し、投稿先チャンネルに\n"
            "  そのAppを招待しておいてください（/invite @アプリ名）。\n"
            "  config/settings.yaml の output.slack.channel も設定してください。"
        )
    else:
        what = "Incoming Webhook のURL（https://hooks.slack.com/services/... ）"
        extra = (
            "  Webhook URL自体が秘密です。URLを知っていれば誰でも投稿できるため、\n"
            "  設定ファイルやSlackのメッセージに貼らないでください。"
        )
    return (
        f"Slackの{what}をKeychainに登録してください:\n"
        f"  security add-generic-password -a \"$USER\" -s {cfg.keychain_service} -U -w\n"
        "  （-w は値を書かずに末尾に置きます。プロンプトに貼り付けてください）\n"
        f"{extra}"
    )


def split_message(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """案件の区切り（区切り線＋空行）を優先して分割する。

    案件ブロックの途中で切れると読めなくなるため、行単位で積み上げ、
    上限を超えたところで切る。1ブロックが上限を超える場合はそのまま送る
    （途中で切るより、長くても1件が完結している方が実用的）。
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        # +1 は改行分
        if size + len(line) + 1 > limit and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


class SlackPoster:
    def __init__(self, config: SlackConfig, *, timeout: float = 30.0) -> None:
        self.config = config
        self._client = httpx.Client(timeout=timeout)

        self.secret = keychain_get(config.keychain_service)
        if not self.secret:
            raise SlackError(
                f"Slackの認証情報がKeychainにありません。\n\n{setup_hint(config)}"
            )
        if config.mode == "bot" and not config.channel:
            raise SlackError(
                "bot モードでは output.slack.channel の指定が必要です"
                "（例: '#nyusatsu' または チャンネルID）"
            )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SlackPoster:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def post(self, text: str) -> list[str]:
        """digestを投稿する。投稿したメッセージのts（bot時）を返す。

        長い場合は分割し、2通目以降はスレッド返信にする。
        """
        chunks = split_message(text)
        log.info("Slackへ投稿します（%d通に分割）", len(chunks))

        if self.config.mode == "bot":
            return self._post_bot(chunks)
        return self._post_webhook(chunks)

    # --- webhook ---------------------------------------------------------

    def _post_webhook(self, chunks: list[str]) -> list[str]:
        # Incoming Webhook はスレッド返信を指定できないため、順に投稿する
        for i, chunk in enumerate(chunks, 1):
            payload: dict[str, object] = {"text": chunk}
            if self.config.username:
                payload["username"] = self.config.username
            if self.config.icon_emoji:
                payload["icon_emoji"] = self.config.icon_emoji
            r = self._client.post(self.secret, json=payload)
            if r.status_code != 200 or r.text.strip() != "ok":
                raise SlackError(
                    redact(
                        f"Webhookへの投稿に失敗しました（{i}/{len(chunks)}通目）: "
                        f"HTTP {r.status_code} {r.text[:200]}"
                    )
                )
        return []

    # --- bot -------------------------------------------------------------

    def _post_bot(self, chunks: list[str]) -> list[str]:
        headers = {
            "Authorization": f"Bearer {self.secret}",
            "Content-Type": "application/json; charset=utf-8",
        }
        thread_ts: str | None = None
        tss: list[str] = []

        for i, chunk in enumerate(chunks, 1):
            payload: dict[str, object] = {
                "channel": self.config.channel,
                "text": chunk,
                # リンクの自動展開を切る。案件が多いと展開だらけで読めなくなるため
                "unfurl_links": False,
                "unfurl_media": False,
            }
            if thread_ts:
                payload["thread_ts"] = thread_ts
            if self.config.username:
                payload["username"] = self.config.username
            if self.config.icon_emoji:
                payload["icon_emoji"] = self.config.icon_emoji

            r = self._client.post(
                POST_MESSAGE_URL,
                headers=headers,
                content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            )
            data = r.json()
            if not data.get("ok"):
                raise SlackError(
                    f"chat.postMessage に失敗しました（{i}/{len(chunks)}通目）: "
                    f"{data.get('error')}\n"
                    "（channel_not_found ならチャンネル名かApp招待を、"
                    "invalid_auth ならトークンを確認してください）"
                )
            ts = data.get("ts", "")
            tss.append(ts)
            if thread_ts is None:
                thread_ts = ts  # 2通目以降はスレッドにぶら下げる
        return tss
