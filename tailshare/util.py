"""合言葉・表示用の整形など、Discord と Web の両方で使う小物。"""

from __future__ import annotations

import secrets
import unicodedata
from datetime import datetime

# 口で伝えても聞き違えにくい、短いローマ字の語
WORDS = (
    "aki ame ari asa ashi ayu buta chou ebi eki fune fuyu gake goma hachi hana hare hashi hata "
    "hebi hiru hoshi ika ike inu iwa kaba kaki kame kani kasa kawa kaze kiji kiku kinu kitsune "
    "koi kuma kumo kuri kusa mame matsu mimi mizu momo mori mugi mura nami nashi natsu neko "
    "niji nori nuno ocha oka oni ringo sakana sake saru sasa semi shika shio sora suna taiko "
    "take tako tanuki tara tori tsuki tsuru uma ume umi usagi uri washi yama yuki yuzu zou"
).split()


def new_passphrase() -> str:
    return f"{secrets.choice(WORDS)}-{secrets.choice(WORDS)}-{secrets.randbelow(90) + 10}"


def normalize_passphrase(text: str) -> str:
    # 全角や大文字で打たれても同じ合言葉として扱う
    text = unicodedata.normalize("NFKC", text).strip().lower()
    return "-".join(part for part in text.replace(" ", "-").replace("_", "-").split("-") if part)


def new_id() -> str:
    # URL に入る秘密の部分。推測されないよう 128 bit 以上にする
    return secrets.token_urlsafe(18)


def clean_filename(name: str) -> str:
    """表示とダウンロード名に使うファイル名。パスの部分と制御文字を落とす。"""
    name = unicodedata.normalize("NFC", name).replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C").strip().strip(".")
    if len(name) > 200:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:190] + dot + ext[:9]) if dot and len(ext) <= 9 else name[:200]
    return name or "file"


def format_size(size: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError  # pragma: no cover


def format_time(ts: int) -> str:
    # Web 画面用。サーバーの時刻帯で出す(Discord では <t:...> で見る人の時刻帯になる)
    return datetime.fromtimestamp(ts).strftime("%-m/%-d %H:%M")
