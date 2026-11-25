"""tailshare — Discord から使う、仲間内のファイル共有。

Bot と Web(アップロード・ダウンロード用ページ)を 1 つのプロセスで動かす。
Web は 127.0.0.1 だけで待ち受け、外へは Tailscale Funnel で公開する。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from aiohttp import web

from tailshare.bot import ShareBot
from tailshare.config import load_config
from tailshare.store import Store
from tailshare.web import create_app

BASE_DIR = Path(__file__).resolve().parent
CLEANUP_INTERVAL = 600  # 秒

log = logging.getLogger("tailshare")


async def cleanup_loop(store: Store) -> None:
    while True:
        try:
            removed = store.cleanup()
            if removed:
                log.info("期限切れのファイルを %d 件消しました", removed)
        except Exception:
            log.exception("掃除に失敗")
        await asyncio.sleep(CLEANUP_INTERVAL)


async def run() -> None:
    # 置くファイルと DB を、サーバーのほかのユーザーから読めないようにする(600 / 700)
    os.umask(0o077)
    config = load_config(Path(os.environ.get("TAILSHARE_CONFIG", BASE_DIR / "config.json")))
    store = Store(BASE_DIR / "data", keep_days=config.keep_days)
    bot = ShareBot(config, store)

    app = create_app(config, store, on_uploaded=bot.announce)
    runner = web.AppRunner(app, access_log=None)  # アクセスログに秘密の URL を残さない
    await runner.setup()
    await web.TCPSite(runner, config.listen_host, config.listen_port).start()
    log.info("Web: http://%s:%d / 公開 URL: %s", config.listen_host, config.listen_port, config.base_url)

    cleaner = asyncio.create_task(cleanup_loop(store))
    try:
        await bot.start(config.token)
    finally:
        cleaner.cancel()
        await runner.cleanup()
        if not bot.is_closed():
            await bot.close()
        store.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
