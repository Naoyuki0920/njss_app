"""設定ファイルの読み込み。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .slack import SlackConfig


class ConfigError(Exception):
    pass


def _expand(p: str | Path) -> Path:
    return Path(p).expanduser()


@dataclass(frozen=True, slots=True)
class OutputConfig:
    subject_template: str = "[NJSS] {date} 入札案件 掲載{adopt}件 / 要確認{review}件"
    text: bool = True
    clipboard: bool = True
    reveal_in_finder: bool = False
    slack: SlackConfig = field(default_factory=SlackConfig)
    slack_confirm: bool = True


@dataclass(frozen=True, slots=True)
class KkjConfig:
    enabled: bool = True
    window_days: int = 14
    threshold: float = 0.70
    min_interval: float = 1.0


@dataclass(frozen=True, slots=True)
class WebSearchConfig:
    enabled: bool = False  # 実測で効果が確認できないため既定は無効
    model: str = "claude-sonnet-5"
    effort: str = "low"
    max_content_tokens: int = 20000
    allowed_domains: list[str] = field(default_factory=lambda: ["lg.jp", "go.jp"])
    max_uses: int = 3


@dataclass(frozen=True, slots=True)
class LlmConfig:
    enabled: bool = True
    api_key_keychain_service: str = "njss-digest-anthropic"
    allow_env_api_key: bool = True
    model: str = "claude-opus-5"
    extract_model: str = "claude-haiku-4-5"
    max_notice_chars: int = 120_000
    daily_output_token_budget: int = 200_000
    effort: str = "high"


@dataclass(frozen=True, slots=True)
class Settings:
    watch_dir: Path
    csv_pattern: str
    var_dir: Path
    after_processing: str  # delete | archive
    output: OutputConfig
    kkj: KkjConfig
    websearch: WebSearchConfig
    llm: LlmConfig
    min_notice_chars: int = 500

    @property
    def db_path(self) -> Path:
        return self.var_dir / "njss.sqlite3"

    @property
    def pdf_inbox(self) -> Path:
        """人が置いた公示書PDFの取り込み先。"""
        return self.var_dir / "inbox_pdf"

    @property
    def notices_dir(self) -> Path:
        return self.var_dir / "notices"

    @property
    def drafts_dir(self) -> Path:
        return self.var_dir / "drafts"

    @property
    def processed_dir(self) -> Path:
        return self.var_dir / "processed"

    @property
    def keeps_processed_csv(self) -> bool:
        return self.after_processing == "archive"

    def ensure_dirs(self) -> None:
        dirs = [self.var_dir, self.notices_dir, self.drafts_dir, self.pdf_inbox]
        if self.keeps_processed_csv:
            dirs.append(self.processed_dir)
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)


def load_settings(path: Path) -> Settings:
    if not path.is_file():
        raise ConfigError(f"設定ファイルがありません: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    paths = data.get("paths", {})
    enrich = data.get("enrich", {})
    out = data.get("output", {})
    slack_cfg = (out.get("slack") or {})
    llm = data.get("llm", {})

    return Settings(
        watch_dir=_expand(paths.get("watch_dir", "~/Documents/njss/inbox")),
        csv_pattern=paths.get("csv_pattern", "案件情報*.csv"),
        var_dir=_expand(paths.get("var_dir", "var")),
        after_processing=paths.get("after_processing", "delete"),
        output=OutputConfig(
            subject_template=out.get(
                "subject_template", "[NJSS] {date} 入札案件 掲載{adopt}件 / 要確認{review}件"
            ),
            text=bool(out.get("text", True)),
            clipboard=bool(out.get("clipboard", True)),
            reveal_in_finder=bool(out.get("reveal_in_finder", False)),
            slack=SlackConfig(
                enabled=bool(slack_cfg.get("enabled", False)),
                mode=slack_cfg.get("mode", "webhook"),
                channel=slack_cfg.get("channel", ""),
                keychain_service=slack_cfg.get(
                    "keychain_service", "njss-digest-slack"
                ),
                username=slack_cfg.get("username", ""),
                icon_emoji=slack_cfg.get("icon_emoji", ""),
            ),
            slack_confirm=bool(slack_cfg.get("confirm", True)),
        ),
        kkj=KkjConfig(**{k: v for k, v in (enrich.get("kkj") or {}).items()}),
        websearch=WebSearchConfig(
            **{k: v for k, v in (enrich.get("websearch") or {}).items()}
        ),
        llm=LlmConfig(**{k: v for k, v in llm.items()}),
        min_notice_chars=(data.get("judge") or {}).get("min_notice_chars", 500),
    )
