"""Discord 側。/アップ /受け取り /削除 /一覧 /help の 5 つのコマンドと、サーバーの許可。

/アップ で対象を「個別」にすると相手を選ぶメニューが出て、合言葉と URL は選んだ人と本人にだけ DM で届く。

Bot は誰でもサーバーに入れられるが、持ち主が許可したサーバーでだけコマンドが使える。
新しいサーバーに入ると、持ち主に DM で「許可」「拒否して退出」のボタンを送る。
"""

from __future__ import annotations

import logging
import re

import discord
from discord import app_commands

from .config import Config
from .store import SharedFile, Store
from .util import clean_filename, format_size
from .web import DISK_FULL, MAX_OPEN_TICKETS, file_url, has_room, over_user_quota, upload_url

log = logging.getLogger("tailshare.bot")

# 投稿にユーザー名を出しても通知(ピン)は鳴らさない
NO_PING = discord.AllowedMentions.none()


def describe(config: Config, shared: SharedFile) -> str:
    return (
        f"<@{shared.uploader_id}> が **{discord.utils.escape_markdown(shared.filename)}**"
        f"({format_size(shared.size)})を共有しました。\n"
        f"合言葉: `{shared.passphrase}`\n"
        f"ダウンロード: {file_url(config, shared.id)}\n"
        f"期限: <t:{shared.expires_at}:f>(<t:{shared.expires_at}:R>)"
    )


def describe_private(config: Config, shared: SharedFile, guild_name: str) -> str:
    return (
        f"<@{shared.uploader_id}> から、**{discord.utils.escape_markdown(guild_name)}** で "
        f"**{discord.utils.escape_markdown(shared.filename)}**({format_size(shared.size)})が個別に届きました。\n"
        f"合言葉: `{shared.passphrase}`(そのサーバーで /受け取り に使えます)\n"
        f"ダウンロード: {file_url(config, shared.id)}\n"
        f"期限: <t:{shared.expires_at}:f>(<t:{shared.expires_at}:R>)"
    )


def pick_recipients(uploader_id: int, *users: discord.abc.User | None) -> tuple[int, ...]:
    """宛先の欄から、空欄・Bot・本人を除いた ID を重複なしで返す。"""
    ids = [u.id for u in users if u is not None and not u.bot and u.id != uploader_id]
    return tuple(dict.fromkeys(ids))


def help_text(config: Config) -> str:
    """導入したての人向けの使い方。上限や日数は設定から出す。"""
    max_size = format_size(config.max_file_bytes)
    return "\n".join(
        [
            "**ファイル共有 Bot の使い方**",
            "",
            "**共有する** `/アップ`",
            "・10MB 以下: `ファイル` に添付すると、そのまま共有します。",
            f"・大きいファイル(最大 {max_size}): 何も添付せずに実行すると、アップロード用のリンクが届きます。"
            f"ブラウザで開いて送ってください。リンクはあなた専用で、{config.upload_link_minutes} 分以内に 1 回だけ使えます。",
            "・`対象` を「個別」にすると相手を選べます。"
            "合言葉と URL は選んだ人とあなたにだけ DM で届き、チャンネルには投稿しません。",
            "",
            "**受け取る** `/受け取り 合言葉`",
            "・共有すると「kuma-sakura-42」のような合言葉が出ます。打つと、あなたにだけファイルか URL が届きます。",
            "・共有の投稿にある URL をブラウザで開いても受け取れます。",
            "",
            "**見る・消す** `/一覧` `/削除 合言葉`",
            "・消せるのは、共有した本人とサーバー管理の権限がある人です。",
            "",
            "**気をつけること**",
            f"・ファイルは {config.keep_days} 日たつと自動で消えます。",
            f"・1 人が置ける合計は {format_size(config.max_user_total_bytes)} までです。",
            "・URL を知っている人は誰でもダウンロードできます。サーバーの外に貼らないでください。",
            "・合言葉は、このサーバーの中だけで通じます。",
            "・「このサーバーではまだ使えません」と出たら、Bot の持ち主の許可待ちです。",
        ]
    )


def can_delete(shared: SharedFile, member: discord.Member | discord.User) -> bool:
    if shared.uploader_id == member.id:
        return True
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and perms.manage_guild)


def guild_label(guild: discord.Guild | None, guild_id: int) -> str:
    return f"**{discord.utils.escape_markdown(guild.name)}**(ID {guild_id})" if guild else f"ID {guild_id} のサーバー"


class GuildDecision(discord.ui.DynamicItem[discord.ui.Button], template=r"tailshare:guild:(?P<action>allow|deny):(?P<gid>\d+)"):
    """持ち主への DM に付けるボタン。custom_id に判断の中身を入れ、Bot を再起動しても押せるようにする。"""

    def __init__(self, action: str, guild_id: int) -> None:
        allow = action == "allow"
        super().__init__(
            discord.ui.Button(
                label="許可" if allow else "拒否して退出",
                style=discord.ButtonStyle.success if allow else discord.ButtonStyle.danger,
                custom_id=f"tailshare:guild:{action}:{guild_id}",
            )
        )
        self.action = action
        self.guild_id = guild_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(match["action"], int(match["gid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        assert isinstance(bot, ShareBot)
        if interaction.user.id != bot.owner_id:
            await interaction.response.send_message("この操作は Bot の持ち主だけができます。", ephemeral=True)
            return
        guild = bot.get_guild(self.guild_id)
        label = guild_label(guild, self.guild_id)
        if self.action == "allow":
            bot.store.set_guild_state(self.guild_id, "allowed", guild.name if guild else None)
            text = f"{label} を許可しました。このサーバーでコマンドが使えます。"
        else:
            bot.store.set_guild_state(self.guild_id, "denied", guild.name if guild else None)
            if guild:
                await guild.leave()
            text = f"{label} を拒否し、Bot を退出させました。"
        log.info("サーバー %s: %s", self.guild_id, self.action)
        await interaction.response.edit_message(content=text, view=None)


class RecipientPicker(discord.ui.View):
    """個別共有の相手を選ぶメニュー。/アップ 対象:個別 のあと、本人にだけ見える形で出す。"""

    def __init__(self, origin: discord.Interaction, on_pick) -> None:
        super().__init__(timeout=300)
        self.origin = origin
        self.on_pick = on_pick
        self.select: discord.ui.UserSelect = discord.ui.UserSelect(
            placeholder="届ける相手を選ぶ(複数可)", min_values=1, max_values=25
        )
        self.select.callback = self._picked  # type: ignore[method-assign]
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.origin.user.id

    async def _picked(self, interaction: discord.Interaction) -> None:
        recipients = pick_recipients(self.origin.user.id, *self.select.values)
        if not recipients:
            await interaction.response.send_message("自分と Bot 以外の人を選んでください。", ephemeral=True)
            return
        self.stop()
        await self.on_pick(interaction, recipients)

    async def on_timeout(self) -> None:
        try:
            await self.origin.edit_original_response(
                content="相手が選ばれないまま時間切れになりました。もう一度 /アップ を実行してください。", view=None
            )
        except discord.DiscordException:
            pass


class ShareBot(discord.Client):
    def __init__(self, config: Config, store: Store) -> None:
        super().__init__(intents=discord.Intents.default(), allowed_mentions=NO_PING)
        self.config = config
        self.store = store
        self.owner_id: int | None = None
        self._scanned = False
        self.tree = app_commands.CommandTree(self)
        self.tree.interaction_check = self._check_guild  # type: ignore[method-assign]
        register_commands(self)

    async def setup_hook(self) -> None:
        self.add_dynamic_items(GuildDecision)
        app = await self.application_info()
        self.owner_id = app.team.owner_id if app.team else app.owner.id
        await self.tree.sync()

    def is_allowed(self, guild_id: int) -> bool:
        return guild_id in self.config.allowed_guild_ids or self.store.guild_state(guild_id) == "allowed"

    async def on_ready(self) -> None:
        log.info("ログイン: %s (%s)", self.user, self.user.id if self.user else "?")
        if self._scanned:
            return
        self._scanned = True
        # 止まっている間に入れられたサーバーも、ここで判断を仰ぐ
        for guild in self.guilds:
            await self.on_guild_join(guild)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if self.is_allowed(guild.id):
            return
        state = self.store.guild_state(guild.id)
        if state == "denied":
            log.info("拒否済みのサーバー %s から退出", guild.id)
            await guild.leave()
            return
        if state == "pending":
            return  # 通知済み
        self.store.set_guild_state(guild.id, "pending", guild.name)
        log.info("新しいサーバー %s (%s)。持ち主の判断待ち", guild.name, guild.id)
        await self.ask_owner(guild)

    async def ask_owner(self, guild: discord.Guild) -> None:
        if self.owner_id is None:
            return
        view = discord.ui.View(timeout=None)
        view.add_item(GuildDecision("allow", guild.id))
        view.add_item(GuildDecision("deny", guild.id))
        owner_name = f"<@{guild.owner_id}>" if guild.owner_id else "不明"
        try:
            owner = self.get_user(self.owner_id) or await self.fetch_user(self.owner_id)
            await owner.send(
                f"{guild_label(guild, guild.id)} に Bot が追加されました(管理者 {owner_name}・{guild.member_count or '?'} 人)。\n"
                "このサーバーでファイル共有を使えるようにしますか。許可するまで、コマンドは使えません。",
                view=view,
            )
        except discord.DiscordException:
            log.warning("持ち主に DM を送れませんでした。manage.py allow %s で許可できます", guild.id, exc_info=True)

    async def _check_guild(self, interaction: discord.Interaction) -> bool:
        if interaction.type is discord.InteractionType.component:
            return True  # 持ち主への DM のボタン。持ち主かどうかはボタン側で確かめる
        if interaction.command is not None and interaction.command.name == "help":
            return True  # 使い方は、許可前のサーバーや DM でも見られるようにする
        if interaction.guild_id is None or not self.is_allowed(interaction.guild_id):
            # 入力候補の問い合わせには文で返せないので、黙って断る
            if interaction.type is not discord.InteractionType.autocomplete:
                await interaction.response.send_message(
                    "このサーバーではまだ使えません。Bot の持ち主が許可すると使えるようになります。", ephemeral=True
                )
            return False
        return True

    async def deliver_private(self, shared: SharedFile) -> list[int]:
        """個別の相手に DM で届ける。届けられなかった相手の ID を返す。"""
        guild = self.get_guild(shared.guild_id)
        text = describe_private(self.config, shared, guild.name if guild else "サーバー")
        failed = []
        for user_id in shared.recipients:
            try:
                user = self.get_user(user_id) or await self.fetch_user(user_id)
                await user.send(text)
            except discord.DiscordException:
                # 相手が DM を受け付けない設定など
                log.info("個別送信できず %s -> %s", shared.id[:6], user_id)
                failed.append(user_id)
        return failed

    def private_report(self, shared: SharedFile, failed: list[int]) -> str:
        sent = [u for u in shared.recipients if u not in failed]
        lines = [
            f"**{discord.utils.escape_markdown(shared.filename)}** を個別に共有しました。チャンネルには投稿していません。",
            f"合言葉: `{shared.passphrase}`",
            f"ダウンロード: {file_url(self.config, shared.id)}",
        ]
        if sent:
            lines.append("DM で届けた相手: " + " ".join(f"<@{u}>" for u in sent))
        if failed:
            lines.append(
                "DM を届けられなかった相手: "
                + " ".join(f"<@{u}>" for u in failed)
                + "(DM を受け付けない設定のようです。合言葉か URL を直接伝えてください)"
            )
        return "\n".join(lines)

    async def announce(self, shared: SharedFile) -> None:
        """Web から上がったファイルを知らせる。全員向けはチャンネルに、個別は相手と本人に DM で。"""
        if shared.is_private:
            failed = await self.deliver_private(shared)
            try:
                uploader = self.get_user(shared.uploader_id) or await self.fetch_user(shared.uploader_id)
                await uploader.send(self.private_report(shared, failed))
            except discord.DiscordException:
                log.info("本人に結果を DM できず %s", shared.id[:6])
            return
        try:
            channel = self.get_channel(shared.channel_id) or await self.fetch_channel(shared.channel_id)
            await channel.send(describe(self.config, shared))  # type: ignore[union-attr]
        except discord.DiscordException:
            # 権限がないなど。送った本人には Web の完了画面で同じ内容が見えている
            log.warning("チャンネル %s に投稿できませんでした", shared.channel_id, exc_info=True)


def register_commands(bot: ShareBot) -> None:
    config, store, tree = bot.config, bot.store, bot.tree

    async def passphrase_choices(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        files = store.list_files(interaction.guild_id or 0, interaction.user.id)
        current = current.lower()
        return [
            app_commands.Choice(name=f"{f.passphrase}({f.filename})"[:100], value=f.passphrase)
            for f in files
            if current in f.passphrase or current in f.filename.lower()
        ][:25]

    def upload_link_text(interaction: discord.Interaction, recipients: tuple[int, ...]) -> str:
        assert interaction.guild_id is not None and interaction.channel_id is not None
        ticket = store.create_ticket(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            user_id=interaction.user.id,
            minutes=config.upload_link_minutes,
            recipients=recipients,
        )
        after = (
            f"送り終えると、選んだ {len(recipients)} 人とあなたに DM で合言葉と URL を届けます。"
            if recipients
            else "送り終えると、このチャンネルに合言葉と URL を投稿します。"
        )
        return (
            f"下のページからファイルを送ってください({format_size(config.max_file_bytes)} まで)。\n"
            f"{upload_url(config, ticket.token)}\n"
            f"このリンクはあなた専用で、{config.upload_link_minutes} 分以内に 1 回だけ使えます。{after}"
        )

    async def save_attachment(
        interaction: discord.Interaction, attachment: discord.Attachment, recipients: tuple[int, ...]
    ) -> SharedFile | None:
        assert interaction.guild_id is not None and interaction.channel_id is not None
        file_id, part = store.new_part_path()
        try:
            await attachment.save(part)
        except (discord.HTTPException, OSError):
            part.unlink(missing_ok=True)
            log.warning("添付の保存に失敗", exc_info=True)
            return None
        return store.add_file(
            file_id,
            part,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            uploader_id=interaction.user.id,
            filename=clean_filename(attachment.filename),
            size=part.stat().st_size,
            recipients=recipients,
        )

    async def share_privately(
        interaction: discord.Interaction, attachment: discord.Attachment | None, recipients: tuple[int, ...]
    ) -> None:
        """相手を選び終えたあとの処理。選択メニューのメッセージを結果に置き換える。"""
        if attachment is None:
            await interaction.response.edit_message(content=upload_link_text(interaction, recipients), view=None)
            return
        await interaction.response.edit_message(content="ファイルを受け取っています…", view=None)
        shared = await save_attachment(interaction, attachment, recipients)
        if shared is None:
            await interaction.edit_original_response(content="ファイルを受け取れませんでした。もう一度送ってください。")
            return
        failed = await bot.deliver_private(shared)
        await interaction.edit_original_response(content=bot.private_report(shared, failed))

    @tree.command(name="アップ", description="ファイルを共有する。添付しなければ、大きなファイル用のアップロードページを出す")
    @app_commands.describe(
        ファイル="10MB 以下ならここに添付してもよい",
        対象="全員(チャンネルに投稿)か、個別(選んだ人にだけ DM で届ける)か。省略すると全員",
    )
    @app_commands.choices(対象=[app_commands.Choice(name="全員", value="all"), app_commands.Choice(name="個別", value="private")])
    @app_commands.guild_only()
    async def up(
        interaction: discord.Interaction,
        ファイル: discord.Attachment | None = None,
        対象: app_commands.Choice[str] | None = None,
    ) -> None:
        if ファイル is not None:
            if ファイル.size > config.max_file_bytes:
                await interaction.response.send_message(
                    f"ファイルが大きすぎます。{format_size(config.max_file_bytes)} 以下にしてください。", ephemeral=True
                )
                return
            if not has_room(store, config, ファイル.size):
                await interaction.response.send_message(DISK_FULL, ephemeral=True)
                return
            quota_error = over_user_quota(store, config, interaction.user.id, ファイル.size)
            if quota_error:
                await interaction.response.send_message(quota_error, ephemeral=True)
                return
        elif store.open_ticket_count(interaction.user.id) >= MAX_OPEN_TICKETS:
            await interaction.response.send_message(
                "まだ使っていないアップロード用リンクが多すぎます。届いたリンクを使うか、"
                f"{config.upload_link_minutes} 分待ってからもう一度実行してください。",
                ephemeral=True,
            )
            return

        if 対象 is not None and 対象.value == "private":
            picker = RecipientPicker(interaction, lambda i, r: share_privately(i, ファイル, r))
            await interaction.response.send_message(
                "届ける相手を選んでください(複数選べます)。選んだ人とあなたにだけ DM で届き、チャンネルには投稿しません。",
                view=picker,
                ephemeral=True,
            )
            return

        if ファイル is None:
            await interaction.response.send_message(upload_link_text(interaction, ()), ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        shared = await save_attachment(interaction, ファイル, ())
        if shared is None:
            await interaction.followup.send("ファイルを受け取れませんでした。もう一度送ってください。", ephemeral=True)
            return
        await interaction.followup.send(describe(config, shared))

    @tree.command(name="受け取り", description="合言葉でファイルを受け取る")
    @app_commands.describe(合言葉="共有したときに出た合言葉(例: kuma-sakura-42)")
    @app_commands.autocomplete(合言葉=passphrase_choices)
    @app_commands.guild_only()
    async def get(interaction: discord.Interaction, 合言葉: str) -> None:
        assert interaction.guild_id is not None
        shared = store.find_by_passphrase(interaction.guild_id, 合言葉)
        # 個別に送られたものは、宛先の人以外には「ない」と答える(あることも知らせない)
        if shared is None or not shared.visible_to(interaction.user.id):
            await interaction.response.send_message(
                f"その合言葉のファイルはありません。合言葉を確かめてください。{config.keep_days} 日を過ぎたものは消えています。",
                ephemeral=True,
            )
            return
        url = file_url(config, shared.id)
        limit = interaction.guild.filesize_limit if interaction.guild else 10 * 1024 * 1024
        # Discord に添付できる大きさなら、そのまま渡す。超えるなら URL だけ
        if shared.size <= limit:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                await interaction.followup.send(
                    f"**{discord.utils.escape_markdown(shared.filename)}** です。URL: {url}",
                    file=discord.File(store.path_of(shared.id), filename=shared.filename),
                    ephemeral=True,
                )
                return
            except discord.HTTPException:
                log.warning("添付で返せなかったので URL で返す", exc_info=True)
                await interaction.followup.send(f"**{discord.utils.escape_markdown(shared.filename)}**\n{url}", ephemeral=True)
                return
        await interaction.response.send_message(
            f"**{discord.utils.escape_markdown(shared.filename)}**({format_size(shared.size)})\n"
            f"Discord に添付できる大きさを超えているので、こちらから落としてください。\n{url}",
            ephemeral=True,
        )

    @tree.command(name="削除", description="共有したファイルを消す(共有した本人か、サーバー管理の権限がある人だけ)")
    @app_commands.describe(合言葉="消すファイルの合言葉")
    @app_commands.autocomplete(合言葉=passphrase_choices)
    @app_commands.guild_only()
    async def delete(interaction: discord.Interaction, 合言葉: str) -> None:
        assert interaction.guild_id is not None
        shared = store.find_by_passphrase(interaction.guild_id, 合言葉)
        if shared is None or not (shared.visible_to(interaction.user.id) or can_delete(shared, interaction.user)):
            await interaction.response.send_message("その合言葉のファイルはありません。", ephemeral=True)
            return
        if not can_delete(shared, interaction.user):
            await interaction.response.send_message(
                "消せるのは、共有した本人とサーバー管理の権限がある人だけです。", ephemeral=True
            )
            return
        store.delete_file(shared.id)
        log.info("削除 %s by %s", shared.id[:6], interaction.user.id)
        await interaction.response.send_message(
            f"**{discord.utils.escape_markdown(shared.filename)}** を削除しました。URL ももう開けません。",
            ephemeral=shared.is_private,
        )

    @tree.command(name="help", description="このBotの使い方を見る")
    async def help_(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(help_text(config), ephemeral=True)

    @tree.command(name="一覧", description="このサーバーで共有中のファイルを見る")
    @app_commands.guild_only()
    async def list_(interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        files = store.list_files(interaction.guild_id, interaction.user.id)
        if not files:
            await interaction.response.send_message("共有中のファイルはありません。/アップ で共有できます。", ephemeral=True)
            return
        lines = [
            f"`{f.passphrase}` **{discord.utils.escape_markdown(f.filename)}**"
            f"({'個別・' if f.is_private else ''}{format_size(f.size)}・<@{f.uploader_id}>・<t:{f.expires_at}:R>に消える)"
            for f in files
        ]
        text = "\n".join(lines)
        if len(text) > 1900:
            text = text[:1900].rsplit("\n", 1)[0] + "\n…(新しい順に表示しています)"
        await interaction.response.send_message(text, ephemeral=True)
