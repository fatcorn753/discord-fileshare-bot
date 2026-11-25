"""サーバーで使う管理コマンド。Discord の DM ボタンが使えないときの予備。

    .venv/bin/python manage.py guilds              Bot を入れたサーバーと許可状態の一覧
    .venv/bin/python manage.py allow <サーバーID>   許可する
    .venv/bin/python manage.py deny <サーバーID>    取り消す(コマンドが使えなくなる。退出は Bot の次回起動時)

Bot が動いたままでも実行できる(どちらも同じ SQLite を見る)。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from tailshare.store import Store

BASE_DIR = Path(__file__).resolve().parent
USAGE = "使い方: manage.py guilds | allow <サーバーID> | deny <サーバーID>"


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("guilds", "allow", "deny"):
        print(USAGE, file=sys.stderr)
        return 2
    store = Store(BASE_DIR / "data", keep_days=14)  # keep_days は一覧と許可の操作には使わない
    try:
        if argv[0] == "guilds":
            rows = store.list_guilds()
            if not rows:
                print("まだどのサーバーにも入っていません。")
            for r in rows:
                when = datetime.fromtimestamp(r["updated_at"]).strftime("%Y-%m-%d %H:%M")
                print(f"{r['guild_id']}\t{r['state']}\t{when}\t{r['name']}")
            return 0
        if len(argv) != 2 or not argv[1].isdigit():
            print(USAGE, file=sys.stderr)
            return 2
        state = "allowed" if argv[0] == "allow" else "denied"
        store.set_guild_state(int(argv[1]), state)
        print(f"{argv[1]} を {state} にしました。")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
