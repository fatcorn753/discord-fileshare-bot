import asyncio
from urllib.parse import quote

import pytest
from aiohttp.test_utils import TestClient, TestServer

from tailshare.config import parse_config
from tailshare.store import Store
from tailshare.web import create_app

TOKEN = "A" * 24 + "." + "B" * 6 + "." + "C" * 27


@pytest.fixture
def env(tmp_path):
    config = parse_config({"token": TOKEN, "base_url": "https://share.example.ts.net", "max_file_mb": 1, "min_free_disk_mb": 1})
    store = Store(tmp_path, keep_days=14)
    yield config, store
    store.close()


def run(coro):
    return asyncio.run(coro)


async def with_client(config, store, fn, announced=None):
    async def on_uploaded(shared):
        if announced is not None:
            announced.append(shared)

    app = create_app(config, store, on_uploaded=on_uploaded)
    async with TestClient(TestServer(app)) as client:
        result = await fn(client)
        await asyncio.sleep(0)  # 投稿のタスクを走らせる
        return result


def test_upload_then_download(env):
    config, store = env
    ticket = store.create_ticket(guild_id=1, channel_id=2, user_id=3, minutes=10)
    announced = []
    data = b"PK\x03\x04" + b"z" * 5000

    async def scenario(client):
        page = await client.get(f"/u/{ticket.token}")
        assert page.status == 200
        assert "ファイルを共有する" in await page.text()
        assert "no-referrer" == page.headers["Referrer-Policy"]

        resp = await client.put(f"/u/{ticket.token}", data=data, headers={"X-Filename": quote("写真 まとめ.zip")})
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body["filename"] == "写真 まとめ.zip"
        file_id = body["url"].rsplit("/", 1)[1]
        assert body["url"].startswith("https://share.example.ts.net/f/")

        # 同じリンクはもう使えない
        again = await client.put(f"/u/{ticket.token}", data=b"x", headers={"X-Filename": "b"})
        assert again.status == 410
        assert (await client.get(f"/u/{ticket.token}")).status == 404

        dl_page = await client.get(f"/f/{file_id}")
        assert dl_page.status == 200
        assert "写真 まとめ.zip" in await dl_page.text()

        dl = await client.get(f"/f/{file_id}/download")
        assert dl.status == 200
        assert await dl.read() == data
        assert dl.headers["Content-Type"] == "application/octet-stream"
        assert "filename*=UTF-8''" in dl.headers["Content-Disposition"]

        # 途中から(Range)も取れる
        part = await client.get(f"/f/{file_id}/download", headers={"Range": "bytes=4-9"})
        assert part.status == 206 and await part.read() == data[4:10]

    run(with_client(config, store, scenario, announced))
    assert len(announced) == 1 and announced[0].guild_id == 1


def test_upload_too_large_and_bad_ticket(env):
    config, store = env
    ticket = store.create_ticket(guild_id=1, channel_id=2, user_id=3, minutes=10)

    async def scenario(client):
        big = await client.put(f"/u/{ticket.token}", data=b"x" * (1024 * 1024 + 1))
        assert big.status == 413
        empty = await client.put(f"/u/{ticket.token}", data=b"")
        assert empty.status == 400
        # 失敗してもリンクは使えるまま
        ok = await client.put(f"/u/{ticket.token}", data=b"ok", headers={"X-Filename": "a.txt"})
        assert ok.status == 200
        unknown = await client.put("/u/nope", data=b"x")
        assert unknown.status == 410

    run(with_client(config, store, scenario))


def test_missing_and_html_escape(env):
    config, store = env
    file_id, part = store.new_part_path()
    part.write_bytes(b"<script>")
    shared = store.add_file(file_id, part, guild_id=1, channel_id=2, uploader_id=3, filename="<img src=x>.html", size=8)

    async def scenario(client):
        page = await client.get(f"/f/{shared.id}")
        text = await page.text()
        assert "<img src=x>" not in text and "&lt;img src=x&gt;" in text
        assert (await client.get("/f/doesnotexist")).status == 404
        assert (await client.get("/f/doesnotexist/download")).status == 404
        assert (await client.get("/static/../config.py")).status in (403, 404)
        robots = await client.get("/robots.txt")
        assert "Disallow: /" in await robots.text()

    run(with_client(config, store, scenario))


def test_private_upload(env):
    config, store = env
    ticket = store.create_ticket(guild_id=1, channel_id=2, user_id=3, minutes=10, recipients=(7, 8))
    announced = []

    async def scenario(client):
        page = await (await client.get(f"/u/{ticket.token}")).text()
        assert "指定した 2 人とあなたにだけ" in page and "チャンネルには投稿していません" in page
        resp = await client.put(f"/u/{ticket.token}", data=b"secret", headers={"X-Filename": "s.zip"})
        assert resp.status == 200

    run(with_client(config, store, scenario, announced))
    assert announced[0].recipients == (7, 8)
    assert store.list_files(1, 99) == []


def test_user_quota_and_headers(tmp_path):
    config = parse_config(
        {"token": TOKEN, "base_url": "https://x", "max_file_mb": 1, "min_free_disk_mb": 1, "max_user_total_mb": 1}
    )
    store = Store(tmp_path, keep_days=14)
    first = store.create_ticket(guild_id=1, channel_id=2, user_id=3, minutes=10)
    second = store.create_ticket(guild_id=1, channel_id=2, user_id=3, minutes=10)
    other = store.create_ticket(guild_id=1, channel_id=2, user_id=4, minutes=10)

    async def scenario(client):
        ok = await client.put(f"/u/{first.token}", data=b"x" * 700_000, headers={"X-Filename": "a"})
        assert ok.status == 200
        over = await client.put(f"/u/{second.token}", data=b"x" * 400_000, headers={"X-Filename": "b"})
        assert over.status == 413 and "上限" in (await over.json())["error"]
        # 別の人には関係ない
        assert (await client.put(f"/u/{other.token}", data=b"x" * 400_000, headers={"X-Filename": "c"})).status == 200
        missing = await client.get("/nope")
        assert missing.status == 404
        assert missing.headers["X-Content-Type-Options"] == "nosniff"
        assert missing.headers["Server"] == "tailshare"
        assert "Strict-Transport-Security" in missing.headers
        file_id = (await ok.json())["url"].rsplit("/", 1)[1]
        assert (await client.get(f"/f/{file_id}")).headers["Cache-Control"] == "private, no-store"

    run(with_client(config, store, scenario))
    assert store.user_usage(3) == 700_000 and store.open_ticket_count(3) == 1
    store.close()


def test_download_ws_ranges(env):
    config, store = env
    data = bytes(range(256)) * 4000  # 1,024,000 バイト。256KB の区切りをまたぐ
    file_id, part = store.new_part_path()
    part.write_bytes(data)
    shared = store.add_file(file_id, part, guild_id=1, channel_id=2, uploader_id=3, filename="a.zip", size=len(data))

    async def scenario(client):
        page = await (await client.get(f"/f/{shared.id}")).text()
        assert 'data-size="1024000"' in page and "download.js" in page

        ws = await client.ws_connect(f"/f/{shared.id}/ws")
        got = bytearray()
        for start, end in [(0, 300_000), (300_000, len(data))]:
            await ws.send_json({"start": start, "end": end, "passphrase": shared.passphrase})
            piece = bytearray()
            while len(piece) < end - start:
                piece += await ws.receive_bytes()
            assert len(piece) == end - start
            got += piece
        assert bytes(got) == data

        # 範囲の外を頼むと切られる
        await ws.send_json({"start": 0, "end": len(data) + 1, "passphrase": shared.passphrase})
        msg = await ws.receive()
        assert msg.type.name in ("CLOSE", "CLOSED") and ws.close_code == 4416

        bad = await client.ws_connect(f"/f/{shared.id}/ws")
        await bad.send_str("nope")
        await bad.receive()
        assert bad.close_code == 4400

        # 消したファイルは、つないだ後でも送らない
        alive = await client.ws_connect(f"/f/{shared.id}/ws")
        store.delete_file(shared.id)
        await alive.send_json({"start": 0, "end": 10, "passphrase": shared.passphrase})
        await alive.receive()
        assert alive.close_code == 4404
        assert (await client.get(f"/f/{shared.id}/ws")).status == 404

    run(with_client(config, store, scenario))


def test_passphrase_required_for_locked_file(env):
    config, store = env
    file_id, part = store.new_part_path()
    part.write_bytes(b"secret-zip")
    locked = store.add_file(file_id, part, guild_id=1, channel_id=2, uploader_id=3, filename="l.zip", size=10)
    file_id2, part2 = store.new_part_path()
    part2.write_bytes(b"open")
    free = store.add_file(file_id2, part2, guild_id=1, channel_id=2, uploader_id=3, filename="f.zip", size=4, locked=False)
    assert locked.locked and not free.locked

    async def scenario(client):
        page = await (await client.get(f"/f/{locked.id}")).text()
        assert 'id="dl-form"' in page and 'name="passphrase"' in page
        # GET では落とせず、ページへ戻される
        get = await client.get(f"/f/{locked.id}/download", allow_redirects=False)
        assert get.status == 303 and get.headers["Location"] == f"/f/{locked.id}"
        wrong = await client.post(f"/f/{locked.id}/download", data={"passphrase": "nope"})
        assert wrong.status == 403 and "合言葉が違います" in await wrong.text()
        # 大文字・空白・全角でも通る
        loose = locked.passphrase.upper().replace("-", " ")
        ok = await client.post(f"/f/{locked.id}/download", data={"passphrase": loose})
        assert ok.status == 200 and await ok.read() == b"secret-zip"

        assert (await client.post(f"/f/{locked.id}/check", json={"passphrase": "x"})).status == 403
        assert (await client.post(f"/f/{locked.id}/check", json={"passphrase": locked.passphrase})).status == 200
        assert (await client.post(f"/f/{locked.id}/check", data=b"[")).status == 400

        ws = await client.ws_connect(f"/f/{locked.id}/ws")
        await ws.send_json({"start": 0, "end": 5, "passphrase": "x"})
        await ws.receive()
        assert ws.close_code == 4403

        # 合言葉なしは今まで通り
        free_page = await (await client.get(f"/f/{free.id}")).text()
        assert 'id="dl-form"' not in free_page and "誰でもダウンロードできます" in free_page
        assert await (await client.get(f"/f/{free.id}/download")).read() == b"open"
        ws2 = await client.ws_connect(f"/f/{free.id}/ws")
        await ws2.send_json({"start": 0, "end": 4})
        assert await ws2.receive_bytes() == b"open"

    run(with_client(config, store, scenario))


def test_upload_with_and_without_passphrase(env):
    config, store = env
    on = store.create_ticket(guild_id=1, channel_id=2, user_id=3, minutes=10, locked=True)
    off = store.create_ticket(guild_id=1, channel_id=2, user_id=3, minutes=10)

    async def scenario(client):
        assert "合言葉とダウンロード用" in await (await client.get(f"/u/{on.token}")).text()
        assert "合言葉と" not in await (await client.get(f"/u/{off.token}")).text()
        a = await (await client.put(f"/u/{on.token}", data=b"a", headers={"X-Filename": "a"})).json()
        b = await (await client.put(f"/u/{off.token}", data=b"b", headers={"X-Filename": "b"})).json()
        assert a["passphrase"] and len(a["passphrase"].split("-")) == 4
        assert b["passphrase"] is None

    run(with_client(config, store, scenario))
