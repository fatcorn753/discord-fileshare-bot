import json

import pytest

from tailshare.config import parse_config
from tailshare.store import Store
from tailshare.util import clean_filename, format_size, normalize_passphrase

TOKEN = "A" * 24 + "." + "B" * 6 + "." + "C" * 27


class Clock:
    def __init__(self) -> None:
        self.t = 1_700_000_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    s = Store(tmp_path, keep_days=14, clock=clock)
    yield s
    s.close()


def put(store: Store, data: bytes = b"hello", guild_id: int = 1, name: str = "a.zip"):
    file_id, part = store.new_part_path()
    part.write_bytes(data)
    return store.add_file(file_id, part, guild_id=guild_id, channel_id=2, uploader_id=3, filename=name, size=len(data))


def test_add_and_find(store):
    shared = put(store)
    assert store.path_of(shared.id).read_bytes() == b"hello"
    assert store.get_file(shared.id) == shared
    # 全角・大文字・空白でも見つかる
    loose = shared.passphrase.upper().replace("-", " ")
    assert store.find_by_passphrase(1, loose) == shared
    # 別のサーバーからは見えない
    assert store.find_by_passphrase(99, shared.passphrase) is None


def test_expire_and_cleanup(store, clock):
    shared = put(store)
    clock.t += 14 * 86400 + 1
    assert store.get_file(shared.id) is None
    assert store.find_by_passphrase(1, shared.passphrase) is None
    assert store.cleanup() == 1
    assert not store.path_of(shared.id).exists()


def test_cleanup_removes_stale_part_and_orphan(store, clock):
    _, part = store.new_part_path()
    part.write_bytes(b"x")
    orphan = store.files_dir / "orphan"
    orphan.write_bytes(b"x")
    fresh_part_id, fresh_part = store.new_part_path()
    fresh_part.write_bytes(b"x")
    import os

    os.utime(part, (clock.t - 2 * 86400, clock.t - 2 * 86400))
    os.utime(fresh_part, (clock.t, clock.t))
    store.cleanup()
    assert not part.exists()
    assert not orphan.exists()
    assert fresh_part.exists()  # 送信中かもしれないので残す


def test_ticket_lifecycle(store, clock):
    t = store.create_ticket(guild_id=1, channel_id=2, user_id=3, minutes=10)
    assert store.ticket_usable(t)
    assert store.claim_ticket(t.token) is not None
    assert store.claim_ticket(t.token) is None  # 同時に 2 本は送れない
    store.release_ticket(t.token)
    assert store.claim_ticket(t.token) is not None
    store.finish_ticket(t.token)
    assert not store.ticket_usable(store.get_ticket(t.token))

    t2 = store.create_ticket(guild_id=1, channel_id=2, user_id=3, minutes=10)
    clock.t += 601
    assert store.claim_ticket(t2.token) is None


def test_delete(store):
    shared = put(store)
    store.delete_file(shared.id)
    assert store.get_file(shared.id) is None
    assert not store.path_of(shared.id).exists()


def test_clean_filename():
    assert clean_filename("../../etc/passwd") == "passwd"
    assert clean_filename("C:\\Users\\x\\写真.zip") == "写真.zip"
    assert clean_filename("a\x00b\n.zip") == "ab.zip"
    assert clean_filename("") == "file"
    assert clean_filename("..") == "file"
    long = clean_filename("あ" * 300 + ".zip")
    assert len(long) <= 200 and long.endswith(".zip")


def test_normalize_passphrase():
    assert normalize_passphrase("  ＫＵＭＡ　sakura_４２ ") == "kuma-sakura-42"


def test_format_size():
    assert format_size(10) == "10 B"
    assert format_size(1536) == "1.5 KB"
    assert format_size(2 * 1024**3) == "2.0 GB"


def test_parse_config():
    c = parse_config({"token": TOKEN, "base_url": "https://x.tail1.ts.net/"})
    assert c.base_url == "https://x.tail1.ts.net"
    assert c.allowed_guild_ids == frozenset()
    c2 = parse_config({"token": TOKEN, "base_url": "https://x", "allowed_guild_ids": [5]})
    assert c2.allowed_guild_ids == {5}
    with pytest.raises(ValueError):
        parse_config({"token": "bad", "base_url": "https://x"})
    with pytest.raises(ValueError):
        parse_config({"token": TOKEN, "base_url": ""})
    with pytest.raises(ValueError):
        parse_config({"token": TOKEN, "base_url": "https://x", "max_file_mb": 0})


def test_example_config_is_valid_shape(tmp_path):
    from pathlib import Path

    raw = json.loads((Path(__file__).parent.parent / "config.example.json").read_text(encoding="utf-8"))
    raw["token"] = TOKEN
    parse_config(raw)


def test_guild_state(store):
    assert store.guild_state(10) is None
    store.set_guild_state(10, "pending", "友達の鯖")
    assert store.guild_state(10) == "pending"
    store.set_guild_state(10, "allowed")  # 名前を渡さなければ前の名前を残す
    rows = store.list_guilds()
    assert rows[0]["name"] == "友達の鯖" and rows[0]["state"] == "allowed"
    with pytest.raises(ValueError):
        store.set_guild_state(10, "yes")


def test_private_visibility(store):
    pub = put(store, name="pub.zip")
    file_id, part = store.new_part_path()
    part.write_bytes(b"x")
    priv = store.add_file(
        file_id, part, guild_id=1, channel_id=2, uploader_id=3, filename="priv.zip", size=1, recipients=(7, 8, 7)
    )
    assert priv.recipients == (7, 8) and priv.is_private and not pub.is_private
    assert priv.visible_to(3) and priv.visible_to(7) and not priv.visible_to(9)
    assert [f.filename for f in store.list_files(1, 9)] == ["pub.zip"]
    assert {f.filename for f in store.list_files(1, 7)} == {"pub.zip", "priv.zip"}
    assert len(store.list_files(1, 3, limit=1)) == 1


def test_migrate_old_db(tmp_path):
    import sqlite3

    db = sqlite3.connect(tmp_path / "tailshare.db")
    db.executescript(
        """CREATE TABLE files (id TEXT PRIMARY KEY, passphrase TEXT NOT NULL, guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL, uploader_id INTEGER NOT NULL, filename TEXT NOT NULL, size INTEGER NOT NULL,
        created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL);
        CREATE TABLE tickets (token TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL, expires_at INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'open');
        INSERT INTO files VALUES ('old', 'a-b-10', 1, 2, 3, 'old.zip', 1, 0, 9999999999);"""
    )
    db.commit()
    db.close()
    s = Store(tmp_path, keep_days=14)
    old = s.get_file("old")
    assert old is not None and old.recipients == () and old.visible_to(99)
    # それまでのファイルは合言葉が出ていたので、合言葉あり扱い
    assert old.locked and not old.check_passphrase("x") and old.check_passphrase("A B 10")
    t = s.create_ticket(guild_id=1, channel_id=2, user_id=3, minutes=10, recipients=(5,))
    assert t.recipients == (5,)
    s.close()


def test_new_passphrase_shape():
    from tailshare.util import WORDS, new_passphrase

    *words, num = new_passphrase().split("-")
    assert len(words) == 3 and all(w in WORDS for w in words) and 1000 <= int(num) <= 9999
