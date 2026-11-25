"""config.json の読み込みと検証。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

MB = 1024 * 1024


@dataclass(frozen=True)
class Config:
    token: str
    base_url: str
    listen_host: str = "127.0.0.1"
    listen_port: int = 8080
    max_file_mb: int = 2048
    keep_days: int = 7
    upload_link_minutes: int = 10
    min_free_disk_mb: int = 2048
    # 1 人が同時に置ける合計。仲間の 1 人がディスクを使い切らないようにする
    max_user_total_mb: int = 10240
    # ここに書いたサーバーは、DM での許可を待たずに最初から使える
    allowed_guild_ids: frozenset[int] = field(default_factory=frozenset)

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * MB

    @property
    def min_free_disk_bytes(self) -> int:
        return self.min_free_disk_mb * MB

    @property
    def max_user_total_bytes(self) -> int:
        return self.max_user_total_mb * MB


TOKEN_RE = re.compile(r"[\w-]{20,}\.[\w-]{4,}\.[\w-]{20,}")


def parse_config(raw: dict) -> Config:
    token = str(raw.get("token", "")).strip()
    if not TOKEN_RE.fullmatch(token):
        raise ValueError("config.json の token が未設定、または Bot トークンの形式ではありません。")
    base_url = str(raw.get("base_url", "")).strip().rstrip("/")
    if not re.fullmatch(r"https?://[^\s/]+(/[^\s]*)?", base_url):
        raise ValueError("config.json の base_url に、公開 URL(例: https://myserver.tailxxxx.ts.net)を書いてください。")

    def positive_int(key: str, default: int) -> int:
        value = raw.get(key, default)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"config.json の {key} は 1 以上の整数にしてください。")
        return value

    guild_ids = raw.get("allowed_guild_ids", [])
    if not isinstance(guild_ids, list) or not all(isinstance(g, int) for g in guild_ids):
        raise ValueError("config.json の allowed_guild_ids は、サーバー ID(数値)のリストにしてください。")

    return Config(
        token=token,
        base_url=base_url,
        listen_host=str(raw.get("listen_host", "127.0.0.1")),
        listen_port=positive_int("listen_port", 8080),
        max_file_mb=positive_int("max_file_mb", 2048),
        keep_days=positive_int("keep_days", 7),
        upload_link_minutes=positive_int("upload_link_minutes", 10),
        min_free_disk_mb=positive_int("min_free_disk_mb", 2048),
        max_user_total_mb=positive_int("max_user_total_mb", 10240),
        allowed_guild_ids=frozenset(guild_ids),
    )


def load_config(path: Path) -> Config:
    if not path.exists():
        raise SystemExit(f"{path.name} が見つかりません。config.example.json をコピーして設定してください。")
    try:
        return parse_config(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, json.JSONDecodeError) as e:
        raise SystemExit(str(e)) from e
