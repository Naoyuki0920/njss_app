"""手元に置いた公示書PDFの取り込み。

予定価格はNJSSの案件詳細ページには表示されず、そこから遷移する
**案件公示書のPDF**に書かれていることが多い。NJSSへの自動アクセスは
規約で禁止されているため、公示書の取得は人が行う。

運用:
  1. NJSSの案件詳細から公示書PDFをダウンロードする
  2. ファイル名を「案件ID.pdf」にして inbox に置く
     （案件IDは案件詳細URLの末尾の数字。digestの【リンク】から分かる）
  3. 次回の実行でPDFが読み込まれ、予定価格が抽出される

ファイル名は前方一致で判定するので「33807436_公告.pdf」のように
説明を付け足しても構わない。
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

from .match import name_similarity, normalize
from .models import Case, Notice

log = logging.getLogger(__name__)

_CASE_ID_RE = re.compile(r"^(\d{6,})")


class LocalDocError(Exception):
    pass


def extract_pdf_text(path: Path) -> str:
    """PDFからテキストを取り出す。

    pdftotext（poppler）があればレイアウトを保った抽出ができるので優先する。
    無ければ pypdf にフォールバックする。
    どちらも使えない場合は例外にせず空文字を返し、呼び出し側で扱う。
    """
    if shutil.which("pdftotext"):
        try:
            r = subprocess.run(
                ["pdftotext", "-layout", str(path), "-"],
                check=True,
                capture_output=True,
                timeout=120,
            )
            text = r.stdout.decode("utf-8", errors="replace").strip()
            if text:
                return text
            log.warning("pdftotext が空を返しました（画像PDFの可能性）: %s", path.name)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            log.warning("pdftotext に失敗しました（pypdfで再試行）: %s", e)

    try:
        from pypdf import PdfReader
    except ImportError:
        log.error(
            "PDFを読むには pdftotext か pypdf が必要です。"
            "`brew install poppler` または `pip install pypdf` を実行してください。"
        )
        return ""

    try:
        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages).strip()
    except Exception as e:
        log.error("PDFを読めませんでした: %s (%s)", path.name, e)
        return ""


def case_id_from_filename(path: Path) -> str | None:
    """ファイル名の先頭の数字列を案件IDとみなす。"""
    m = _CASE_ID_RE.match(path.stem)
    return m.group(1) if m else None


def match_by_content(text: str, cases: list[Case], *, threshold: float = 0.45) -> Case | None:
    """PDFの本文から、どの案件のものかを判別する。

    ファイル名をリネームさせずに済ませるための処理。
    公示書には案件名がそのまま書かれているので、本文の先頭部分と
    各案件名の類似度を測って最も高いものを選ぶ。

    公告文はPDF抽出でレイアウトが崩れるため、案件名がそのままの形で
    現れるとは限らない。そこで完全一致ではなく類似度で判定し、
    確信が持てない場合は None を返す（誤って別案件の価格を使わないため）。
    """
    if not cases:
        return None

    # 案件名は先頭付近に書かれている。全文で測ると本文の量に埋もれるため、
    # 先頭の一定量に絞って比較する。
    head = text[:3000]

    best: tuple[float, Case] | None = None
    for case in cases:
        norm_name = normalize(case.name)
        if not norm_name:
            continue
        # 案件名が本文にそのまま含まれていれば確実
        if norm_name in normalize(head):
            return case
        score = name_similarity(case.name, head[: max(len(case.name) * 3, 120)])
        if best is None or score > best[0]:
            best = (score, case)

    if best and best[0] >= threshold:
        log.debug("PDF本文から案件を推定: %r (類似度 %.2f)", best[1].name, best[0])
        return best[1]
    return None


def collect(
    inbox: Path, *, cases: list[Case] | None = None, min_chars: int = 200
) -> dict[str, Notice]:
    """inbox内のPDFを読み、案件IDごとの Notice にして返す。

    案件の特定は次の順で行う:
      1. ファイル名の先頭の数字列（例: 33807436.pdf）
      2. PDF本文と案件名の照合（cases が渡されている場合）

    2があるので、ダウンロードしたPDFをリネームせずそのまま置ける。

    読めなかったファイルは戻り値に含めず、必ず警告を出す。
    黙って無視すると「置いたのに反映されない」原因が分からなくなるため。
    """
    if not inbox.is_dir():
        return {}

    out: dict[str, Notice] = {}
    for path in sorted(inbox.glob("*.pdf")):
        text = extract_pdf_text(path)
        if len(text) < min_chars:
            log.warning(
                "PDFから十分なテキストを取り出せませんでした（%d字）: %s "
                "画像として保存されたPDFの可能性があります。",
                len(text),
                path.name,
            )
            continue

        case_id = case_id_from_filename(path)
        how = "ファイル名"
        if case_id is None and cases:
            matched = match_by_content(text, cases)
            if matched is not None:
                case_id = matched.case_id
                how = f"本文照合（{matched.name[:24]}…）"

        if case_id is None:
            log.warning(
                "PDFがどの案件のものか判別できません: %s\n"
                "  ファイル名を「案件ID.pdf」にするか、対象案件が判定対象に"
                "含まれているか確認してください。",
                path.name,
            )
            continue

        log.info(
            "公示書PDFを取り込みました: %s -> 案件%s (%d字, %s)",
            path.name,
            case_id,
            len(text),
            how,
        )
        out[case_id] = Notice(
            case_id=case_id,
            source="manual_pdf",
            text=text,
            source_url=None,
            matched_by="manual",
            raw={"filename": path.name, "matched_how": how},
        )
    return out
