# tailshare

<img src="assets/icon.svg" width="96" height="96" alt="">

Discord から使う、仲間内向けのファイル共有 Bot です。
ファイルは自宅のサーバーに置き、[Tailscale Funnel](https://tailscale.com/kb/1223/funnel) で公開します。
ドメインもルーターのポート開放もいりません。

- `/アップ` と打つと、あなた専用のアップロード用ページが届きます(2GB まで)
- 送り終えると、チャンネルに **合言葉** と **ダウンロード用 URL** が投稿されます。対象を「個別」にすれば、選んだ人にだけ DM で届けることもできます
- `/受け取り 合言葉` で受け取れます。Discord に添付できる大きさなら添付で、超えるなら URL で返します
- 7 日たつと自動で消えます。`/削除 合言葉` ですぐに消すこともできます

## 仕組み

```
Discord ──(コマンド)──▶ Bot ┐
                            ├─ 1 つのプロセス(main.py) ─ data/files/ と data/tailshare.db
ブラウザ ─▶ Tailscale Funnel ─▶ Web(127.0.0.1:8080) ┘
           https://<マシン名>.<tailnet名>.ts.net
```

- Bot は Discord に外向きにつなぐだけなので、Bot のためにポートを開ける必要はありません。
- Web は `127.0.0.1` だけで待ち受けます。外から届くのは Funnel を通ったものだけです。SSH などほかのサービスは公開されません。
- Discord に直接ファイルを添付できるのは 10MB まで(ブーストなしのサーバー)なので、大きなファイルは Web で受け渡します。

## 必要なもの

- 常時動かせる Linux マシン(Ubuntu を想定しています)と Python 3.11 以上
- [Tailscale](https://tailscale.com/download) のアカウント(無料プランで使えます)と、そのマシンへの Tailscale の導入
- Discord の Bot(下の手順で作ります)

## 導入手順

### 1. Discord の Bot を作る

1. [Discord Developer Portal](https://discord.com/developers/applications) で **New Application** を押して、アプリを作ります。
2. 左の **Bot** を開き、**Reset Token** でトークンを発行してコピーします(あとで `config.json` に書きます)。
3. 同じ画面の **Public Bot** を決めます。
   - 自分のサーバーだけで使うなら、オフにします。
   - 友達が管理するサーバーにも入れてもらうなら、オンのままにします。誰でも Bot をサーバーに入れられますが、あなたが許可するまでコマンドは使えません(下の「サーバーの許可」)。
4. 特権インテント(Privileged Gateway Intents)は、どれもオンにする必要はありません。
   アイコンは **General Information** の App Icon に `assets/discord-icon.png` を設定できます。
5. 次の URL の `<アプリID>` を、**General Information** にある Application ID に置き換えて開き、Bot をサーバーに招待します。

   ```
   https://discord.com/oauth2/authorize?client_id=<アプリID>&scope=bot+applications.commands&permissions=52224
   ```

   付与される権限は、チャンネルを見る・メッセージを送る・埋め込みリンク・ファイルを添付、の 4 つです。

### 2. Tailscale Funnel を有効にする

1. 管理画面の [DNS](https://login.tailscale.com/admin/dns) で **MagicDNS** と **HTTPS Certificates** を有効にします。
2. サーバーで Tailscale のバージョンを確かめます(1.52 以上が必要です)。

   ```bash
   tailscale version
   ```

3. Funnel を有効にします。`8080` は `config.json` の `listen_port` と同じ番号にします。

   ```bash
   sudo tailscale funnel --bg 8080
   ```

   Funnel が許可されていないと、許可用の URL が表示されます。その URL を開いて許可してから、同じコマンドをもう一度実行します。

4. 公開 URL を確かめます。表示された `https://...ts.net` を `config.json` の `base_url` に書きます。

   ```bash
   tailscale funnel status
   ```

公開をやめるときは、`sudo tailscale funnel reset` を実行します。

### 3. サーバーに置いて起動する

```bash
git clone https://github.com/fatcorn753/tailshare.git
cd tailshare
cp config.example.json config.json
nano config.json   # token と base_url を書く
./deploy/install.sh
```

`install.sh` は、仮想環境を作って依存を入れ、systemd に `tailshare` として登録して起動します。
更新するときは、`git pull` のあとに同じスクリプトをもう一度実行します。

ログは次のコマンドで見られます。

```bash
journalctl -u tailshare -f
```

## 設定(config.json)

| 項目 | 既定値 | 意味 |
|---|---|---|
| `token` | なし | Bot のトークン |
| `base_url` | なし | 公開 URL(`tailscale funnel status` で表示されるもの) |
| `listen_host` | `127.0.0.1` | Web の待ち受けアドレス。変えないでください |
| `listen_port` | `8080` | Web の待ち受けポート。Funnel のコマンドと同じ番号にします |
| `max_file_mb` | `2048` | 1 ファイルの上限(MB) |
| `keep_days` | `7` | 自動で消すまでの日数 |
| `upload_link_minutes` | `10` | アップロード用リンクの有効時間(分) |
| `min_free_disk_mb` | `2048` | ディスクの空きがこれを下回るアップロードは断ります(MB) |
| `max_user_total_mb` | `10240` | 1 人が同時に置けるファイルの合計(MB)。超えるアップロードは断ります |
| `allowed_guild_ids` | `[]` | 許可を待たずに最初から使えるサーバーの ID。それ以外は DM で許可します |

## サーバーの許可

Bot が新しいサーバーに入ると、Bot の持ち主(Developer Portal でアプリを作った人)に DM が届きます。
**許可** を押すと、そのサーバーでコマンドが使えるようになります。**拒否して退出** を押すと、Bot がそのサーバーから抜けます。
許可するまでは、コマンドを打っても「まだ使えません」と返すだけです。

DM が届かないとき(Bot とサーバーを共有していない、DM を受け付けない設定など)は、サーバーで次のコマンドを使います。

```bash
.venv/bin/python manage.py guilds        # 入っているサーバーと状態の一覧
.venv/bin/python manage.py allow <サーバーID>
.venv/bin/python manage.py deny <サーバーID>
```

`config.json` の `allowed_guild_ids` に書いたサーバーは、許可を待たずに最初から使えます。

## コマンド

| コマンド | できること |
|---|---|
| `/アップ` | アップロード用ページのリンクを、本人にだけ送ります。10MB 以下ならファイルを添付して直接共有もできます |
| `/アップ 対象:個別` | 個別共有です。相手を選ぶメニュー(25 人まで)が出て、合言葉と URL は選んだ人と本人にだけ DM で届きます。チャンネルには投稿しません |
| `/受け取り 合言葉` | ファイルを受け取ります(結果は本人にだけ見えます) |
| `/削除 合言葉` | ファイルを消します。消せるのは共有した本人と、サーバー管理の権限がある人です |
| `/一覧` | そのサーバーで共有中のファイルを見ます |

合言葉は、そのサーバーの中だけで通じます。別のサーバーの合言葉では受け取れません。
個別共有のファイルは、`/一覧` や入力候補にも、共有した本人と相手にしか出ません。ほかの人が合言葉を打っても「ありません」と返します。ただし、ダウンロード用 URL を知っていれば誰でも落とせる点は、全員向けと同じです。

## セキュリティ

### 入っている対策

- **公開する範囲を絞っています。** Web は `127.0.0.1` でだけ待ち受け、外からは Tailscale Funnel を通った https だけが届きます。SSH などほかのサービスは公開されません。
- **URL は推測できません。** ダウンロード用 URL とアップロード用リンクには、144 bit のランダムな文字列を使っています。
- **アップロード用リンクは本人専用です。** `/アップ` を実行した人にだけ届き、1 回だけ、10 分以内しか使えません。未使用のリンクは 1 人 5 本までです。
- **容量を守ります。** 1 ファイルの上限、1 人あたりの合計の上限、ディスクの空きの下限を超えるアップロードは断ります。
- **ブラウザでは開かせません。** ダウンロードは必ず保存させます(`application/octet-stream` と `Content-Disposition: attachment`)。上げられたファイルが HTML でも、このサイトの中で動くことはありません。
- **ブラウザ向けのヘッダーを付けています。** CSP、`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、HSTS を付けています。`Referrer-Policy: no-referrer` で、URL がほかのサイトへ漏れないようにしています。検索エンジンには載せません(`robots.txt` と `X-Robots-Tag`)。
- **URL をログに残しません。** アクセスログは記録しません。応答にサーバーのバージョンも出しません。
- **ファイルの名前と置き場を分けています。** ファイルは推測できない ID の名前で置き、元の名前は DB にだけ持ちます。名前に `../` などが入っていても、置き場の外へは書けません。置いたファイルと DB は、サーバーのほかのユーザーからは読めません(権限 600)。
- **Discord 側も対策しています。** ファイル名に `@everyone` が入っていても通知は鳴らしません。個別共有のファイルは、本人と相手以外には存在も知らせません。
- **systemd の制限:** `NoNewPrivileges`・`ProtectSystem=strict`・`PrivateTmp` を付け、書き込める場所を `data/` だけにしています。

### 使う人が気をつけること

- **ダウンロード用 URL を知っている人は、Discord のメンバーでなくても誰でもダウンロードできます。** URL を外に貼らないでください。合言葉だけでは、Discord の外からは受け取れません。
- **Bot のトークンは、`config.json` だけに置いてください。** チャットや issue に貼ってしまったら、Developer Portal で Reset Token をしてください。
- ファイルはサーバーに暗号化せずに置きます。サーバーの持ち主は中身を見られます。
- 著作権のあるものなど、共有してはいけないものを置かないでください。ファイルの扱いは、サーバーを動かす人の責任です。
- Tailscale Funnel の帯域には上限があります(具体的な値は公開されていません)。大きなファイルを頻繁にやり取りすると、遅くなることがあります。

## 開発

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
.venv/bin/ruff check .
```

Bot なしで画面だけを確かめるときは、次のコマンドで起動し、表示された URL を開きます。

```bash
.venv/bin/python tools/dev_web.py
```

## ライセンス

MIT
