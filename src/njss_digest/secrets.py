"""認証情報の取得。

APIキーを環境変数（~/.zshenv など）に置くと、あなたが起動する**すべての**
プロセスにキーが継承される。npmのpostinstallスクリプトでも読めてしまう。

そこで既定では macOS Keychain から取得し、このアプリの実行時にだけ
メモリ上に載せる。環境変数は明示的に指定されたときのフォールバックとしてのみ使う。

注意（正確に理解しておくべきこと）:
  Keychainは「暗号化して保存される」「他プロセスの環境変数一覧に現れない」
  「平文ファイルとして誤ってコミットしない」という利点があるが、
  同じユーザで動く別プロセスが `security` コマンドを呼べば読み出せる。
  完全な隔離ではなく、環境変数より格段にマシ、という位置づけ。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

log = logging.getLogger(__name__)

DEFAULT_SERVICE = "njss-digest-anthropic"
ENV_VAR = "ANTHROPIC_API_KEY"


class SecretsError(Exception):
    pass


def keychain_get(service: str, account: str | None = None) -> str | None:
    """Keychainから汎用パスワードを取り出す。無ければ None。"""
    if not shutil.which("security"):
        return None
    cmd = ["security", "find-generic-password", "-s", service, "-w"]
    if account:
        cmd[2:2] = ["-a", account]
    try:
        r = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=15)
    except subprocess.CalledProcessError:
        return None  # 未登録
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.warning("Keychainへのアクセスに失敗しました: %s", e)
        return None
    return r.stdout.strip() or None


def setup_hint(service: str = DEFAULT_SERVICE) -> str:
    # -w を値なしで「最後に」置くと入力プロンプトになる。
    # -w に値を続けて書くとシェル履歴とプロセス一覧にキーが残るので避けること。
    return (
        "APIキーをKeychainに登録してください（ターミナルで実行。キーは画面に残りません）:\n"
        f"  security add-generic-password -a \"$USER\" -s {service} -U -w\n"
        "  （-w は値を書かずに末尾に置きます。実行すると password: と表示されるので"
        "そこにAPIキーを貼り付けます）\n\n"
        "確認:\n"
        f"  security find-generic-password -s {service} >/dev/null && echo 登録済み\n\n"
        "削除:\n"
        f"  security delete-generic-password -s {service}"
    )


def resolve_api_key(
    *, service: str = DEFAULT_SERVICE, allow_env: bool = True
) -> tuple[str, str]:
    """APIキーと、その取得元の名前を返す。

    Keychainを先に見る。環境変数に古いキーが残っていても、そちらに
    引きずられないようにするため。
    """
    key = keychain_get(service)
    if key:
        return key, f"Keychain({service})"

    if allow_env:
        key = os.environ.get(ENV_VAR)
        if key:
            log.warning(
                "APIキーを環境変数 %s から読み込みました。"
                "この方式では起動するすべてのプロセスにキーが渡ります。"
                "Keychainへの移行を推奨します。",
                ENV_VAR,
            )
            return key, f"環境変数({ENV_VAR})"

    raise SecretsError(
        "Anthropic APIキーが見つかりません。\n\n"
        + setup_hint(service)
        + "\n\nキーなしで動かす場合は `njss-digest run --no-llm --no-web` を使ってください。"
    )
