"""常駐監視。ダウンロードフォルダに新しいCSVが置かれたら自動で実行する。

これにより日々の操作は「NJSSでダウンロードボタンを押す」だけになる。
処理が終わると通知センターに結果を出す。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from collections.abc import Callable

log = logging.getLogger(__name__)


def notify(title: str, message: str) -> None:
    """macOSの通知センターに表示する。失敗しても処理は続ける。"""
    import subprocess

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{esc(message)}" with title "{esc(title)}"',
            ],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except Exception as e:  # 通知は補助機能なので失敗しても止めない
        log.debug("通知を出せませんでした: %s", e)


def wait_until_stable(path: Path, *, checks: int = 3, interval: float = 0.5) -> bool:
    """ファイルの書き込みが終わるのを待つ。

    ダウンロード途中のファイルを読むと、途中までのCSVを処理してしまう。
    サイズが連続して変化しなくなるまで待つ。
    """
    last = -1
    stable = 0
    for _ in range(60):
        if not path.exists():
            return False
        size = path.stat().st_size
        if size == last and size > 0:
            stable += 1
            if stable >= checks:
                return True
        else:
            stable = 0
            last = size
        time.sleep(interval)
    log.warning("ファイルサイズが安定しませんでした: %s", path)
    return path.exists()


def watch(
    watch_dir: Path,
    pattern: str,
    on_csv: Callable[[Path], None],
    *,
    poll_interval: float = 2.0,
) -> None:
    """watch_dir を監視し、パターンに一致するCSVが現れたら on_csv を呼ぶ。

    watchdog が使えない環境ではポーリングにフォールバックする。
    """
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        log.info("watchdog が無いためポーリングで監視します（%.0f秒間隔）", poll_interval)
        _poll(watch_dir, pattern, on_csv, poll_interval)
        return

    class Handler(FileSystemEventHandler):
        def _handle(self, raw_path: str) -> None:
            path = Path(raw_path)
            if path.is_dir() or not path.match(pattern):
                return
            log.info("CSVを検知: %s", path.name)
            if not wait_until_stable(path):
                return
            try:
                on_csv(path)
            except Exception as e:
                log.exception("処理中にエラーが発生しました: %s", e)
                notify("NJSS digest", f"エラー: {e}")

        def on_created(self, event) -> None:
            self._handle(event.src_path)

        def on_moved(self, event) -> None:
            self._handle(event.dest_path)

    observer = Observer()
    observer.schedule(Handler(), str(watch_dir), recursive=False)
    observer.start()
    log.info("監視を開始しました: %s (%s)", watch_dir, pattern)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("監視を終了します")
    finally:
        observer.stop()
        observer.join()


def _poll(
    watch_dir: Path, pattern: str, on_csv: Callable[[Path], None], interval: float
) -> None:
    seen: set[Path] = set(watch_dir.glob(pattern))
    log.info("監視を開始しました(ポーリング): %s (%s)", watch_dir, pattern)
    try:
        while True:
            time.sleep(interval)
            for path in watch_dir.glob(pattern):
                if path in seen:
                    continue
                seen.add(path)
                log.info("CSVを検知: %s", path.name)
                if not wait_until_stable(path):
                    continue
                try:
                    on_csv(path)
                except Exception as e:
                    log.exception("処理中にエラーが発生しました: %s", e)
                    notify("NJSS digest", f"エラー: {e}")
    except KeyboardInterrupt:
        log.info("監視を終了します")
