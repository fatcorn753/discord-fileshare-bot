import asyncio

from tailshare.bot import GuildDecision, ShareBot, can_delete, describe, help_text, pick_recipients
from tailshare.config import parse_config
from tailshare.store import Store

TOKEN = "A" * 24 + "." + "B" * 6 + "." + "C" * 27


def test_commands_register(tmp_path):
    config = parse_config({"token": TOKEN, "base_url": "https://share.example.ts.net"})
    store = Store(tmp_path, keep_days=14)

    async def build():
        bot = ShareBot(config, store)
        payload = [c.to_dict(bot.tree) for c in bot.tree.get_commands()]
        await bot.close()
        return payload

    payload = asyncio.run(build())

    async def allowed():
        bot = ShareBot(parse_config({"token": TOKEN, "base_url": "https://x", "allowed_guild_ids": [7]}), store)
        try:
            return bot.is_allowed(7), bot.is_allowed(8), (store.set_guild_state(8, "allowed"), bot.is_allowed(8))[1]
        finally:
            await bot.close()

    assert asyncio.run(allowed()) == (True, False, True)
    button = GuildDecision("deny", 123)
    assert button.item.custom_id == "tailshare:guild:deny:123"
    assert GuildDecision.__discord_ui_compiled_template__.fullmatch("tailshare:guild:allow:5")
    names = {c["name"] for c in payload}
    assert names == {"アップ", "受け取り", "削除", "一覧", "help"}
    up = next(c for c in payload if c["name"] == "アップ")
    assert [o["name"] for o in up["options"]] == ["ファイル", "対象", "合言葉"]
    assert [c["name"] for c in up["options"][1]["choices"]] == ["全員", "個別"]
    assert [c["name"] for c in up["options"][2]["choices"]] == ["あり", "なし"]
    assert all(o["required"] is False for o in up["options"])

    file_id, part = store.new_part_path()
    part.write_bytes(b"x")
    shared = store.add_file(file_id, part, guild_id=1, channel_id=2, uploader_id=3, filename="a_b*.zip", size=1)
    text = describe(config, shared)
    assert shared.passphrase in text and "https://share.example.ts.net/f/" in text and "\\_" in text

    class U:
        id = 3

    class Other:
        id = 4
        guild_permissions = type("P", (), {"manage_guild": False})()

    assert can_delete(shared, U()) and not can_delete(shared, Other())
    store.close()


def test_pick_recipients():
    class U:
        def __init__(self, id, bot=False):
            self.id, self.bot = id, bot

    assert pick_recipients(1, U(2), None, U(2)) == (2,)
    assert pick_recipients(1, U(1), U(3, bot=True)) == ()
    assert pick_recipients(1, U(4), U(5), U(6)) == (4, 5, 6)


def test_recipient_picker():
    from tailshare.bot import RecipientPicker

    class Origin:
        user = type("U", (), {"id": 1})()

    async def build():
        view = RecipientPicker(Origin(), lambda i, r: None)
        return view.select.max_values, view.timeout

    assert asyncio.run(build()) == (25, 300)


def test_help_text_uses_config():
    raw = {"token": TOKEN, "base_url": "https://x", "keep_days": 3, "max_file_mb": 512, "upload_link_minutes": 15}
    config = parse_config(raw)
    text = help_text(config)
    assert "3 日" in text and "512.0 MB" in text and "15 分" in text
    assert all(f"/{name}" in text for name in ["アップ", "受け取り", "一覧", "削除"])
    assert len(text) <= 2000  # Discord の 1 メッセージの上限


def test_describe_without_passphrase(tmp_path):
    config = parse_config({"token": TOKEN, "base_url": "https://share.example.ts.net"})
    store = Store(tmp_path, keep_days=14)
    file_id, part = store.new_part_path()
    part.write_bytes(b"x")
    shared = store.add_file(file_id, part, guild_id=1, channel_id=2, uploader_id=3, filename="a.zip", size=1, locked=False)
    text = describe(config, shared)
    assert "合言葉: なし" in text and shared.passphrase not in text
    store.close()
