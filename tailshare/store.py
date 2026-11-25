"""共有ファイルとアップロード用リンクの記録(SQLite)と、ファイル本体の置き場。

ファイル本体は files/<id> に置き、元のファイル名は DB にだけ持つ。
利用者が付けた名前をパスに使わないため、名前に何が入っていても置き場の外へは書かない。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .util import new_id, new_passphrase, normalize_passphrase

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id          TEXT PRIMARY KEY,
    passphrase  TEXT NOT NULL,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    uploader_id INTEGER NOT NULL,
    filename    TEXT NOT NULL,
    size        INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    recipients  TEXT NOT NULL DEFAULT ''  -- 個別に送った相手のユーザー ID(カンマ区切り)。空なら全員向け
);
CREATE UNIQUE INDEX IF NOT EXISTS files_guild_passphrase ON files (guild_id, passphrase);
CREATE INDEX IF NOT EXISTS files_expires ON files (expires_at);

-- Bot を入れたサーバーの許可状態。pending(持ち主の判断待ち) / allowed / denied
CREATE TABLE IF NOT EXISTS guilds (
    guild_id   INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    state      TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

-- アップロード用リンク。open → busy(送信中) → used と進む。送信に失敗したら open に戻す
CREATE TABLE IF NOT EXISTS tickets (
    token      TEXT PRIMARY KEY,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    state      TEXT NOT NULL DEFAULT 'open',
    recipients TEXT NOT NULL DEFAULT ''
);
"""


GUILD_STATES = ("pending", "allowed", "denied")


@dataclass(frozen=True)
class SharedFile:
    id: str
    passphrase: str
    guild_id: int
    channel_id: int
    uploader_id: int
    filename: str
    size: int
    created_at: int
    expires_at: int
    recipients: tuple[int, ...] = ()

    @property
    def is_private(self) -> bool:
        return bool(self.recipients)

    def visible_to(self, user_id: int) -> bool:
        """全員向けなら誰でも、個別なら共有した本人と相手だけが見られる。"""
        return not self.recipients or user_id == self.uploader_id or user_id in self.recipients


@dataclass(frozen=True)
class Ticket:
    token: str
    guild_id: int
    channel_id: int
    user_id: int
    expires_at: int
    state: str
    recipients: tuple[int, ...] = ()


def pack_ids(ids) -> str:
    return ",".join(str(i) for i in dict.fromkeys(ids))


def unpack_ids(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in text.split(",") if x)


def to_file(row: sqlite3.Row) -> SharedFile:
    return SharedFile(**{**dict(row), "recipients": unpack_ids(row["recipients"])})


def to_ticket(row: sqlite3.Row) -> Ticket:
    return Ticket(**{**dict(row), "recipients": unpack_ids(row["recipients"])})


class Store:
    def __init__(self, data_dir: Path, keep_days: int, clock=time.time) -> None:
        self.files_dir = data_dir / "files"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.keep_seconds = keep_days * 86400
        self._clock = clock
        self._db = sqlite3.connect(data_dir / "tailshare.db")
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._migrate()
        self._db.commit()

    def _migrate(self) -> None:
        # 宛先の列がない古い DB(0.1.0)に列を足す
        for table in ("files", "tickets"):
            cols = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}
            if "recipients" not in cols:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN recipients TEXT NOT NULL DEFAULT ''")

    def now(self) -> int:
        return int(self._clock())

    def close(self) -> None:
        self._db.close()

    # --- ファイル本体の置き場 ---

    def path_of(self, file_id: str) -> Path:
        return self.files_dir / file_id

    def new_part_path(self) -> tuple[str, Path]:
        """書き込み途中のファイルの置き場。書き終えたら add_file で正式な名前に移す。"""
        file_id = new_id()
        return file_id, self.files_dir / f"{file_id}.part"

    # --- 共有ファイル ---

    def add_file(
        self,
        file_id: str,
        part_path: Path,
        *,
        guild_id: int,
        channel_id: int,
        uploader_id: int,
        filename: str,
        size: int,
        recipients: tuple[int, ...] = (),
    ) -> SharedFile:
        part_path.rename(self.path_of(file_id))
        now = self.now()
        for _ in range(20):
            passphrase = new_passphrase()
            try:
                with self._db:
                    self._db.execute(
                        "INSERT INTO files (id, passphrase, guild_id, channel_id, uploader_id, filename, size,"
                        " created_at, expires_at, recipients) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            file_id,
                            passphrase,
                            guild_id,
                            channel_id,
                            uploader_id,
                            filename,
                            size,
                            now,
                            now + self.keep_seconds,
                            pack_ids(recipients),
                        ),
                    )
            except sqlite3.IntegrityError:
                continue  # 同じサーバーで合言葉が重なった。引き直す
            return self.get_file(file_id)  # type: ignore[return-value]
        self.path_of(file_id).unlink(missing_ok=True)
        raise RuntimeError("合言葉を割り当てられませんでした")

    def get_file(self, file_id: str) -> SharedFile | None:
        row = self._db.execute("SELECT * FROM files WHERE id = ? AND expires_at > ?", (file_id, self.now())).fetchone()
        return to_file(row) if row else None

    def find_by_passphrase(self, guild_id: int, text: str) -> SharedFile | None:
        row = self._db.execute(
            "SELECT * FROM files WHERE guild_id = ? AND passphrase = ? AND expires_at > ?",
            (guild_id, normalize_passphrase(text), self.now()),
        ).fetchone()
        return to_file(row) if row else None

    def list_files(self, guild_id: int, viewer_id: int, limit: int = 25) -> list[SharedFile]:
        """viewer_id の人に見えるものだけを、新しい順に返す。"""
        rows = self._db.execute(
            "SELECT * FROM files WHERE guild_id = ? AND expires_at > ? ORDER BY created_at DESC",
            (guild_id, self.now()),
        )
        visible = (f for f in map(to_file, rows) if f.visible_to(viewer_id))
        return [f for _, f in zip(range(limit), visible, strict=False)]

    def user_usage(self, user_id: int) -> int:
        """その人が上げて、まだ消えていないファイルの合計バイト数。"""
        row = self._db.execute(
            "SELECT COALESCE(SUM(size), 0) FROM files WHERE uploader_id = ? AND expires_at > ?", (user_id, self.now())
        ).fetchone()
        return int(row[0])

    def open_ticket_count(self, user_id: int) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) FROM tickets WHERE user_id = ? AND state = 'open' AND expires_at > ?", (user_id, self.now())
        ).fetchone()
        return int(row[0])

    def delete_file(self, file_id: str) -> None:
        with self._db:
            self._db.execute("DELETE FROM files WHERE id = ?", (file_id,))
        self.path_of(file_id).unlink(missing_ok=True)

    # --- サーバーの許可 ---

    def guild_state(self, guild_id: int) -> str | None:
        row = self._db.execute("SELECT state FROM guilds WHERE guild_id = ?", (guild_id,)).fetchone()
        return row["state"] if row else None

    def set_guild_state(self, guild_id: int, state: str, name: str | None = None) -> None:
        if state not in GUILD_STATES:
            raise ValueError(f"state は {', '.join(GUILD_STATES)} のどれかにしてください")
        with self._db:
            self._db.execute(
                "INSERT INTO guilds (guild_id, name, state, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (guild_id) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at, "
                "name = CASE WHEN ? IS NULL THEN guilds.name ELSE excluded.name END",
                (guild_id, name or "", state, self.now(), name),
            )

    def list_guilds(self) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM guilds ORDER BY updated_at DESC").fetchall()

    # --- アップロード用リンク ---

    def create_ticket(
        self, *, guild_id: int, channel_id: int, user_id: int, minutes: int, recipients: tuple[int, ...] = ()
    ) -> Ticket:
        token = new_id()
        with self._db:
            self._db.execute(
                "INSERT INTO tickets (token, guild_id, channel_id, user_id, expires_at, recipients) VALUES (?, ?, ?, ?, ?, ?)",
                (token, guild_id, channel_id, user_id, self.now() + minutes * 60, pack_ids(recipients)),
            )
        return self.get_ticket(token)  # type: ignore[return-value]

    def get_ticket(self, token: str) -> Ticket | None:
        row = self._db.execute("SELECT * FROM tickets WHERE token = ?", (token,)).fetchone()
        return to_ticket(row) if row else None

    def ticket_usable(self, ticket: Ticket | None) -> bool:
        return ticket is not None and ticket.state == "open" and ticket.expires_at > self.now()

    def claim_ticket(self, token: str) -> Ticket | None:
        """送信を始める。同じリンクで同時に 2 つ送られないよう、1 つだけが成功する。"""
        with self._db:
            cur = self._db.execute(
                "UPDATE tickets SET state = 'busy' WHERE token = ? AND state = 'open' AND expires_at > ?",
                (token, self.now()),
            )
        return self.get_ticket(token) if cur.rowcount == 1 else None

    def release_ticket(self, token: str) -> None:
        with self._db:
            self._db.execute("UPDATE tickets SET state = 'open' WHERE token = ? AND state = 'busy'", (token,))

    def finish_ticket(self, token: str) -> None:
        with self._db:
            self._db.execute("UPDATE tickets SET state = 'used' WHERE token = ?", (token,))

    # --- 掃除 ---

    def cleanup(self) -> int:
        """期限切れのファイル・リンクと、置き去りの書きかけを消す。消したファイル数を返す。"""
        now = self.now()
        expired = [r["id"] for r in self._db.execute("SELECT id FROM files WHERE expires_at <= ?", (now,))]
        for file_id in expired:
            self.delete_file(file_id)
        with self._db:
            # 送信中(busy)のリンクは、期限を過ぎても送信が続いている可能性があるので 1 日は残す
            self._db.execute(
                "DELETE FROM tickets WHERE (state != 'busy' AND expires_at <= ?) OR expires_at <= ?",
                (now, now - 86400),
            )
        known = {r["id"] for r in self._db.execute("SELECT id FROM files")}
        for path in self.files_dir.iterdir():
            stale_part = path.suffix == ".part" and path.stat().st_mtime < now - 86400
            orphan = path.suffix != ".part" and path.name not in known
            if stale_part or orphan:
                path.unlink(missing_ok=True)
        return len(expired)
