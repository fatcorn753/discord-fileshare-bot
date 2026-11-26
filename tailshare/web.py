"""アップロード用ページとダウンロード用ページ(aiohttp)。

公開されるのはここだけ。ログインはなく、URL に入った推測できない ID を知っている人だけが使える。
- /u/<リンクの ID>   アップロード用ページ(Discord の /アップ で本人にだけ渡す。1 回だけ使える)
- /f/<ファイルの ID> ダウンロード用ページ
- /f/<ファイルの ID>/ws  分割ダウンロード用の WebSocket(download.js が何本も同時に開く)
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import quote, unquote

from aiohttp import web

from .config import Config
from .store import SharedFile, Store
from .util import clean_filename, format_size, format_time

log = logging.getLogger("tailshare.web")

STATIC_DIR = Path(__file__).parent / "static"
CHUNK = 1024 * 1024

OnUploaded = Callable[[SharedFile], Awaitable[None]]

ICON_ERROR = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5"/><path d="M12 16.5h.01"/></svg>'
ICON_BOX = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 13h5l1.5 2.5h5L16 13h5"/><path d="M5.5 5h13L21 13v6H3v-6L5.5 5Z"/></svg>'

CONFIG_KEY = web.AppKey("config", Config)
STORE_KEY = web.AppKey("store", Store)
ON_UPLOADED_KEY = web.AppKey("on_uploaded", object)
TASKS_KEY = web.AppKey("tasks", set)


DISK_FULL = "サーバーの空き容量が足りません。古いファイルを /削除 するか、期限切れを待ってください。"


MAX_OPEN_TICKETS = 5

# 分割ダウンロードで 1 回に頼める大きさの上限と、1 本の WebSocket で送る 1 通の大きさ
WS_MAX_RANGE = 16 * 1024 * 1024
WS_FRAME = 256 * 1024


def has_room(store: Store, config: Config, size: int) -> bool:
    return shutil.disk_usage(store.files_dir).free - size >= config.min_free_disk_bytes


def over_user_quota(store: Store, config: Config, user_id: int, size: int) -> str | None:
    """1 人あたりの上限を超えるなら、伝える文を返す。"""
    if store.user_usage(user_id) + size > config.max_user_total_bytes:
        return (
            f"あなたが共有中のファイルの合計が上限({format_size(config.max_user_total_bytes)})を超えます。"
            "/一覧 で確かめて、要らないものを /削除 してください。"
        )
    return None


def file_url(config: Config, file_id: str) -> str:
    return f"{config.base_url}/f/{file_id}"


def upload_url(config: Config, token: str) -> str:
    return f"{config.base_url}/u/{token}"


def page(title: str, body: str, *, script: str | None = None, status: int = 200) -> web.Response:
    script_tag = f'<script src="/static/{script}" defer></script>' if script else ""
    full_title = title if title == "tailshare" else f"{title} - tailshare"
    text = f"""<!doctype html>
<html lang="ja" data-accent="koke">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{html.escape(full_title)}</title>
<link rel="icon" href="/static/icon.svg" type="image/svg+xml">
<link rel="stylesheet" href="/static/tokens.css">
<link rel="stylesheet" href="/static/base.css">
<link rel="stylesheet" href="/static/components.css">
<link rel="stylesheet" href="/static/app.css">
{script_tag}
</head>
<body>
<main class="ts-main">
{body}
</main>
</body>
</html>
"""
    return web.Response(text=text, content_type="text/html", status=status)


def gone_page(title: str, message: str, status: int = 404) -> web.Response:
    body = f"""<div class="td-empty">
  {ICON_BOX}
  <p class="td-empty-title">{html.escape(title)}</p>
  <p>{html.escape(message)}</p>
</div>"""
    return page(title, body, status=status)


# --- アップロード ---


async def upload_page(request: web.Request) -> web.Response:
    store = request.app[STORE_KEY]
    config = request.app[CONFIG_KEY]
    ticket = store.get_ticket(request.match_info["token"])
    if not store.ticket_usable(ticket):
        return gone_page(
            "このリンクは使えません",
            "期限が切れたか、すでに使われています。Discord でもう一度 /アップ を実行してください。",
        )
    max_label = format_size(config.max_file_bytes)
    assert ticket is not None
    if ticket.recipients:
        n = len(ticket.recipients)
        lead_to = f"指定した {n} 人とあなたにだけ、Discord の DM で合言葉とダウンロード用の URL が届き"
        done_note = f"指定した {n} 人とあなたに、DM で同じ内容を送りました。チャンネルには投稿していません。"
    else:
        lead_to = "Discord のチャンネルに合言葉とダウンロード用の URL が投稿され"
        done_note = "Discord のチャンネルにも同じ内容を投稿しました。"
    body = f"""<h1>ファイルを共有する</h1>
<p class="ts-lead">共有したいファイルを 1 つ選んでください。{max_label} まで送れます。送ると{lead_to}、{config.keep_days} 日後に自動で消えます。</p>

<form id="upload" class="ts-stack" data-max-bytes="{config.max_file_bytes}" data-max-label="{max_label}">
  <label class="ts-drop" id="drop" for="file">
    <span class="ts-drop-title" id="drop-title">ここにファイルをドロップ</span>
    <span class="ts-drop-sub" id="drop-sub">またはクリックして選ぶ</span>
    <input class="ts-visually-hidden" id="file" name="file" type="file">
  </label>
  <p class="td-error" id="error" role="alert" hidden>{ICON_ERROR}<span id="error-text"></span></p>
  <div class="ts-progress" id="progress" hidden>
    <progress id="bar" max="100" value="0" aria-label="送信の進み具合"></progress>
    <p class="ts-meta num" id="progress-text" aria-live="polite"></p>
  </div>
  <div class="ts-actions">
    <button class="td-btn" type="button" id="cancel" hidden>送信をやめる</button>
    <button class="td-btn" data-variant="primary" type="submit" id="send">送信</button>
  </div>
</form>

<section class="ts-stack" id="done" hidden aria-labelledby="done-title">
  <div class="td-notice" data-tone="success" role="status">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="m8.5 12.5 2.5 2.5 5-5.5"/></svg>
    <div><p class="td-notice-title" id="done-title" tabindex="-1">共有しました</p><p>{done_note}このページは閉じてかまいません。</p></div>
  </div>
  <dl class="ts-facts">
    <dt>ファイル</dt><dd id="done-name"></dd>
    <dt>合言葉</dt><dd><code id="done-pass"></code></dd>
    <dt>URL</dt><dd><a id="done-url"></a></dd>
    <dt>期限</dt><dd class="num" id="done-expires"></dd>
  </dl>
  <div class="ts-actions"><button class="td-btn" type="button" id="copy">URL をコピー</button></div>
</section>
"""
    resp = page("ファイルを共有する", body, script="upload.js")
    resp.headers["Cache-Control"] = "no-store"
    return resp


def json_error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


async def receive_upload(request: web.Request) -> web.Response:
    store = request.app[STORE_KEY]
    config = request.app[CONFIG_KEY]
    token = request.match_info["token"]

    if not store.ticket_usable(store.get_ticket(token)):
        return json_error(
            410, "このリンクは期限が切れたか、すでに使われています。Discord でもう一度 /アップ を実行してください。"
        )
    length = request.content_length
    if length is None:
        return json_error(411, "ファイルの大きさが分かりませんでした。別のブラウザで試してください。")
    if length == 0:
        return json_error(400, "空のファイルは送れません。")
    if length > config.max_file_bytes:
        return json_error(413, f"ファイルが大きすぎます。{format_size(config.max_file_bytes)} 以下にしてください。")
    if not has_room(store, config, length):
        return json_error(507, DISK_FULL)
    pending = store.get_ticket(token)
    quota_error = over_user_quota(store, config, pending.user_id, length) if pending else None
    if quota_error:
        return json_error(413, quota_error)

    filename = clean_filename(unquote(request.headers.get("X-Filename", "")))
    ticket = store.claim_ticket(token)
    if ticket is None:
        return json_error(409, "このリンクでは、ほかのファイルを送信中か送信済みです。")

    file_id, part = store.new_part_path()
    received = 0
    try:
        with part.open("wb") as f:
            async for chunk in request.content.iter_chunked(CHUNK):
                received += len(chunk)
                if received > length:
                    raise ValueError("申告より大きい")
                f.write(chunk)
        if received != length:
            raise ValueError("途中で切れた")
    except Exception as e:  # 接続が切れた・申告と大きさが違う など
        part.unlink(missing_ok=True)
        store.release_ticket(token)
        log.info("アップロード中断 %s: %r", token[:6], e)
        return json_error(400, "送信が途中で止まりました。もう一度送ってください。")
    except BaseException:  # キャンセル(停止時など)。片付けてから上へ伝える
        part.unlink(missing_ok=True)
        store.release_ticket(token)
        raise

    shared = store.add_file(
        file_id,
        part,
        guild_id=ticket.guild_id,
        channel_id=ticket.channel_id,
        uploader_id=ticket.user_id,
        recipients=ticket.recipients,
        filename=filename,
        size=received,
    )
    store.finish_ticket(token)
    log.info("アップロード %s (%s)", shared.id[:6], format_size(shared.size))

    on_uploaded = request.app[ON_UPLOADED_KEY]
    if on_uploaded is not None:
        # Discord への投稿が遅れても、送った人の画面は待たせない
        task = asyncio.create_task(on_uploaded(shared))
        request.app[TASKS_KEY].add(task)
        task.add_done_callback(request.app[TASKS_KEY].discard)

    return web.json_response(
        {
            "filename": shared.filename,
            "passphrase": shared.passphrase,
            "url": file_url(config, shared.id),
            "expires": format_time(shared.expires_at),
        }
    )


# --- ダウンロード ---


async def download_page(request: web.Request) -> web.Response:
    store = request.app[STORE_KEY]
    shared = store.get_file(request.match_info["file_id"])
    if shared is None:
        return gone_page("ファイルが見つかりません", "期限が切れて消えたか、削除されました。共有した人に聞いてみてください。")
    name = html.escape(shared.filename)
    body = f"""<h1 class="ts-filename">{name}</h1>
<dl class="ts-facts">
  <dt>大きさ</dt><dd class="num">{format_size(shared.size)}</dd>
  <dt>期限</dt><dd class="num">{format_time(shared.expires_at)} まで</dd>
</dl>
<div class="ts-actions">
  <button class="td-btn" type="button" id="dl-cancel" hidden>やめる</button>
  <a class="td-btn" data-variant="primary" id="dl" href="/f/{shared.id}/download" download
     data-size="{shared.size}" data-name="{name}">ダウンロード</a>
</div>
<p class="td-error" id="dl-error" role="alert" hidden>{ICON_ERROR}<span id="dl-error-text"></span></p>
<div class="ts-progress" id="dl-progress" hidden>
  <progress id="dl-bar" max="100" value="0" aria-label="ダウンロードの進み具合"></progress>
  <p class="ts-meta num" id="dl-text" aria-live="polite"></p>
</div>
<p class="ts-meta">この URL を知っている人は、誰でもダウンロードできます。</p>
"""
    resp = page(shared.filename, body, script="download.js")
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


async def download_file(request: web.Request) -> web.StreamResponse:
    store = request.app[STORE_KEY]
    shared = store.get_file(request.match_info["file_id"])
    path = store.path_of(shared.id) if shared else None
    if shared is None or path is None or not path.exists():
        return gone_page("ファイルが見つかりません", "期限が切れて消えたか、削除されました。共有した人に聞いてみてください。")
    # 中身が HTML でもブラウザに表示させず、必ず保存させる
    ascii_name = shared.filename.encode("ascii", "replace").decode().replace('"', "_").replace("?", "_")
    return web.FileResponse(
        path,
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(shared.filename)}",
        },
    )


async def download_ws(request: web.Request) -> web.StreamResponse:
    """分割ダウンロード。{"start": n, "end": m} を受け取るたびに、その範囲を 256KB ずつ送る。

    ブラウザは同じサイトへの fetch を 1 本の HTTP/2 接続にまとめるが、Funnel は 1 接続あたりの速さが
    頭打ちになる。Funnel は HTTP/2 の上で WebSocket を張らせない(RFC 8441 を有効にしていない)ため、
    WebSocket は 1 本ずつ別の接続になり、並べると速くなる。
    """
    store = request.app[STORE_KEY]
    file_id = request.match_info["file_id"]
    if store.get_file(file_id) is None:
        raise web.HTTPNotFound()
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=4096)
    await ws.prepare(request)
    loop = asyncio.get_running_loop()
    async for msg in ws:
        if msg.type is not web.WSMsgType.TEXT:
            break
        shared = store.get_file(file_id)  # 送っている間に期限切れ・削除になったら止める
        try:
            req = json.loads(msg.data)
            start, end = int(req["start"]), int(req["end"])
        except (ValueError, KeyError, TypeError):
            await ws.close(code=4400, message=b"bad request")
            break
        if shared is None:
            await ws.close(code=4404, message=b"gone")
            break
        if not (0 <= start < end <= shared.size and end - start <= WS_MAX_RANGE):
            await ws.close(code=4416, message=b"bad range")
            break
        with open(store.path_of(file_id), "rb") as f:
            f.seek(start)
            left = end - start
            while left > 0:
                data = await loop.run_in_executor(None, f.read, min(WS_FRAME, left))
                if not data:
                    break
                await ws.send_bytes(data)
                left -= len(data)
    return ws


async def index(_: web.Request) -> web.Response:
    return gone_page(
        "tailshare",
        "このサイトは Discord の Bot から使います。共有するときは、サーバーで /アップ を実行してください。",
        status=200,
    )


async def robots(_: web.Request) -> web.Response:
    return web.Response(text="User-agent: *\nDisallow: /\n")


@web.middleware
async def security_headers(request: web.Request, handler):
    try:
        resp = await handler(request)
    except web.HTTPException as e:
        # 404 など、例外で返る応答にも同じヘッダーを付ける
        resp = e
        _set_security_headers(resp)
        raise
    _set_security_headers(resp)
    return resp


def _set_security_headers(resp: web.StreamResponse) -> None:
    # URL そのものが合言葉の代わりなので、よそのサイトへ Referer で漏らさない
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    )
    # 公開は Funnel の https だけなので、常に https で開かせる
    resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")


async def hide_server_version(_: web.Request, resp: web.StreamResponse) -> None:
    # 既定では Python と aiohttp のバージョンが出るので、名前だけにする
    resp.headers["Server"] = "tailshare"


def create_app(config: Config, store: Store, on_uploaded: OnUploaded | None = None) -> web.Application:
    app = web.Application(middlewares=[security_headers])
    app.on_response_prepare.append(hide_server_version)
    app[CONFIG_KEY] = config
    app[STORE_KEY] = store
    app[ON_UPLOADED_KEY] = on_uploaded
    app[TASKS_KEY] = set()
    app.router.add_get("/u/{token}", upload_page)
    app.router.add_put("/u/{token}", receive_upload)
    app.router.add_get("/f/{file_id}", download_page)
    app.router.add_get("/f/{file_id}/download", download_file)
    app.router.add_get("/f/{file_id}/ws", download_ws)
    app.router.add_get("/", index)
    app.router.add_get("/robots.txt", robots)
    app.router.add_static("/static/", STATIC_DIR)
    return app
