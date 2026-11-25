#!/usr/bin/env bash
# サーバーで実行する。仮想環境を作って依存を入れ、systemd に登録して起動する。
# 何度実行してもよい(2 回目以降は更新と再起動になる)。
#   ./deploy/install.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(id -un)"
cd "$DIR"

if [[ ! -f config.json ]]; then
  echo "config.json がありません。config.example.json をコピーして、token と base_url を書いてから再実行してください。" >&2
  exit 1
fi
chmod 600 config.json
mkdir -p data

test -d .venv || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

sed -e "s|__USER__|$USER_NAME|g" -e "s|__DIR__|$DIR|g" deploy/tailshare.service \
  | sudo tee /etc/systemd/system/tailshare.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable tailshare
sudo systemctl restart tailshare

sleep 4
journalctl -u tailshare -n 15 --no-pager
