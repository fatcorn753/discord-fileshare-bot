"""開発用: Bot なしで Web だけを動かし、画面を確かめる。

アップロード用リンクと見本のファイルを 1 つずつ作り、URL を表示する。
データは tools/.devdata に置く(本番の data/ には触れない)。
    .venv/bin/python tools/dev_web.py [ポート]
"""

import logging
import sys
from pathlib import Path

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tailshare.config import parse_config  # noqa: E402
from tailshare.store import Store  # noqa: E402
from tailshare.web import create_app  # noqa: E402

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
base = f"http://127.0.0.1:{port}"
config = parse_config({"token": "A" * 24 + "." + "B" * 6 + "." + "C" * 27, "base_url": base, "min_free_disk_mb": 1})
store = Store(Path(__file__).resolve().parent / ".devdata", keep_days=14)


async def on_uploaded(shared):
    print(f"[Discord に投稿するはずの内容] {shared.filename} 合言葉 {shared.passphrase}", flush=True)


ticket = store.create_ticket(guild_id=1, channel_id=1, user_id=1, minutes=600)
file_id, part = store.new_part_path()
part.write_bytes(b"sample" * 1000)
sample = store.add_file(
    file_id, part, guild_id=1, channel_id=1, uploader_id=1, filename="合宿の写真まとめ_2026.zip", size=6000, locked=False
)
# 分割ダウンロードを試すための 40MB の見本(中身は位置ごとに違うので、組み立て違いが分かる)
big_id, big_part = store.new_part_path()
big_part.write_bytes(bytes(i % 251 for i in range(40 * 1024 * 1024)))
big = store.add_file(big_id, big_part, guild_id=1, channel_id=1, uploader_id=1, filename="動画まとめ.zip", size=40 * 1024 * 1024)
print(f"アップロード: {base}/u/{ticket.token}\nダウンロード(合言葉なし): {base}/f/{sample.id}", flush=True)
print(f"大きい見本(合言葉 {big.passphrase}): {base}/f/{big.id}", flush=True)
logging.basicConfig(level=logging.INFO)
web.run_app(create_app(config, store, on_uploaded=on_uploaded), host="127.0.0.1", port=port)
