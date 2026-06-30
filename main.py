"""
Discord Anticrash / Verification / Partners bot
Prefix commands: !verify, !partners, !partnersadd, !anticrash

Token is NOT stored in code. Use DISCORD_TOKEN / BOT_TOKEN / TOKEN env variable,
or put the token into token.txt on the hosting panel if env variables are unavailable.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord.ext import commands

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent
DB_DIR = BASE_DIR / "database"
DB_DIR.mkdir(exist_ok=True)

SETTINGS_PATH = DB_DIR / "settings.json"
USERS_PATH = DB_DIR / "users.json"
PARTNERS_PATH = DB_DIR / "partners.json"
LOCALBAN_PATH = DB_DIR / "localban.json"
VERIFY_PATH = DB_DIR / "verify.json"

DEFAULT_PERMISSIONS = {
    "can_commands": False,
    "can_manage_users": False,
    "can_ban": False,
    "can_mute": False,
    "can_kick": False,
    "can_links": False,
}

PERMISSION_LABELS = {
    "can_commands": "Команды",
    "can_manage_users": "Пользователи/права",
    "can_ban": "Бан",
    "can_mute": "Мут/таймаут",
    "can_kick": "Кик",
    "can_links": "Ссылки",
}

DEFAULT_SETTINGS = {
    "prefix": "!",
    "owner_id": 1389945313225080953,
    "verified_role_id": 1521582337651904714,
    "log_channel_id": 1521582423702376591,
    "anti_crash_image_url": "https://cdn.discordapp.com/attachments/1521582430925099048/1521586410484928674/07ad4a7b-f1b0-4e9f-9bc4-8c23c71e43ef.png?ex=6a455f45&is=6a440dc5&hm=1afac22d06183e7d206fd57a2c48be34583d485de3fb5f62099c50dab08dac43&",
    "embed_color": 0x2C2F33,
    "spam_timeout_minutes": 60,
    "max_message_length": 1600,
    "max_mentions": 6,
    "max_special_chars": 18,
    "message_burst_count": 6,
    "message_burst_seconds": 6,
}

message_burst: Dict[Tuple[int, int], deque] = defaultdict(deque)
deleted_by_bot: set[int] = set()
json_lock = asyncio.Lock()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        write_json_sync(path, default)
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        backup = path.with_suffix(path.suffix + f".broken-{int(datetime.now().timestamp())}")
        try:
            path.rename(backup)
        except Exception:
            pass
        write_json_sync(path, default)
        return default


def write_json_sync(path: Path, data: Any) -> None:
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


async def update_json(path: Path, default: Any, mutator) -> Any:
    async with json_lock:
        data = read_json(path, default)
        result = mutator(data)
        write_json_sync(path, data)
        return result


settings: Dict[str, Any] = {**DEFAULT_SETTINGS, **read_json(SETTINGS_PATH, DEFAULT_SETTINGS)}
OWNER_ID = int(settings["owner_id"])
PREFIX = str(settings.get("prefix", "!"))
EMBED_COLOR = int(settings.get("embed_color", 0x2C2F33))


def ensure_database() -> None:
    write_json_sync(SETTINGS_PATH, {**DEFAULT_SETTINGS, **read_json(SETTINGS_PATH, DEFAULT_SETTINGS)})
    users = read_json(USERS_PATH, {})
    owner_key = str(OWNER_ID)
    owner_record = users.get(owner_key, {})
    users[owner_key] = {
        "name": owner_record.get("name", "OWNER"),
        "added_by": owner_record.get("added_by", "system"),
        "created_at": owner_record.get("created_at", "system"),
        "permissions": {key: True for key in DEFAULT_PERMISSIONS},
    }
    write_json_sync(USERS_PATH, users)
    if not PARTNERS_PATH.exists():
        write_json_sync(PARTNERS_PATH, [])
    if not LOCALBAN_PATH.exists():
        write_json_sync(LOCALBAN_PATH, {})
    if not VERIFY_PATH.exists():
        write_json_sync(VERIFY_PATH, {"image_url": ""})


ensure_database()


def get_token() -> str:
    token = (
        os.getenv("DISCORD_TOKEN")
        or os.getenv("BOT_TOKEN")
        or os.getenv("TOKEN")
        or os.getenv("DISCORD_BOT_TOKEN")
    )
    if token:
        return token.strip()
    token_file = BASE_DIR / "token.txt"
    if token_file.exists():
        value = token_file.read_text(encoding="utf-8").strip()
        if value and "PASTE" not in value.upper():
            return value
    raise RuntimeError(
        "Bot token not found. Set DISCORD_TOKEN/BOT_TOKEN/TOKEN env variable or put token into token.txt"
    )


def make_embed(title: Optional[str] = None, description: Optional[str] = None) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=EMBED_COLOR)
    embed.set_footer(text="Anticrash System • black/grey")
    return embed


def trim_text(text: Optional[str], limit: int = 950) -> str:
    if not text:
        return "[пусто / вложение / embed]"
    text = text.replace("`", "ʼ")
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def is_owner(user_id: int) -> bool:
    return int(user_id) == OWNER_ID


def get_user_record(user_id: int) -> Optional[Dict[str, Any]]:
    if is_owner(user_id):
        return {
            "name": "OWNER",
            "permissions": {key: True for key in DEFAULT_PERMISSIONS},
        }
    users = read_json(USERS_PATH, {})
    return users.get(str(user_id))


def has_perm(user_id: int, permission: str) -> bool:
    if is_owner(user_id):
        return True
    record = get_user_record(user_id)
    if not record:
        return False
    perms = record.get("permissions", {})
    return bool(perms.get(permission, False))


def has_any_access(user_id: int) -> bool:
    return is_owner(user_id) or str(user_id) in read_json(USERS_PATH, {})


def clean_id(raw: str) -> Optional[int]:
    raw = raw.strip()
    raw = raw.replace("<@", "").replace(">", "").replace("!", "")
    if not raw.isdigit():
        return None
    return int(raw)


def normalise_for_spam(text: str) -> str:
    low = text.lower()
    low = low.replace("е", "e").replace("ё", "e").replace("а", "a").replace("о", "o")
    low = re.sub(r"(.)\1{1,}", r"\1", low)  # diiiscord -> discord
    return re.sub(r"[\s_\-`'*|\\\[\]{}()<>:;,.]+", "", low)


def detect_violation(message: discord.Message) -> Optional[Tuple[str, str]]:
    content = message.content or ""
    compact = normalise_for_spam(content)
    lower = content.lower()

    if len(content) > int(settings.get("max_message_length", 1600)):
        return ("spam", "слишком длинный текст")

    total_mentions = len(message.mentions) + len(message.role_mentions)
    if message.mention_everyone or total_mentions >= int(settings.get("max_mentions", 6)):
        return ("spam", "массовые упоминания")

    if re.search(r"(.)\1{13,}", content):
        return ("spam", "слишком много повторяющихся символов")

    special_chars = re.findall(r"[#$%^&*/\\|<>~`+=\[\]{}]", content)
    if len(special_chars) >= int(settings.get("max_special_chars", 18)):
        return ("spam", "слишком много спецсимволов")
    if len(content) >= 25 and len(special_chars) / max(1, len(content)) > 0.35:
        return ("spam", "спам спецсимволами")

    # Discord/Telegram/invite/link patterns. Compact catches: d i s c o r d . g g, diiiscord, t . me.
    link_patterns = [
        "discordgg",
        "discordcom/invite",
        "discordappcom/invite",
        "dscgg",
        "discord",
        "diiscord",
        "telegram",
        "telegramme",
        "tme",
        "dsgg",
    ]
    raw_patterns = [
        r"(?:https?://)?(?:www\.)?discord(?:app)?\.com/invite/",
        r"(?:https?://)?(?:www\.)?discord\.gg/",
        r"(?:https?://)?(?:www\.)?dsc\.gg/",
        r"(?:https?://)?(?:www\.)?t\.me/",
        r"\.gg/",
        r"\btg\b",
        r"\bds\b",
    ]
    if any(p in compact for p in link_patterns) or any(re.search(p, lower) for p in raw_patterns):
        return ("link", "запрещённая ссылка/упоминание Discord или Telegram")

    key = (message.guild.id, message.author.id) if message.guild else (0, message.author.id)
    now = datetime.now().timestamp()
    dq = message_burst[key]
    dq.append(now)
    burst_seconds = int(settings.get("message_burst_seconds", 6))
    burst_count = int(settings.get("message_burst_count", 6))
    while dq and now - dq[0] > burst_seconds:
        dq.popleft()
    if len(dq) >= burst_count:
        dq.clear()
        return ("spam", f"частый спам: {burst_count}+ сообщений за {burst_seconds} сек.")

    return None


async def send_log(
    guild: Optional[discord.Guild],
    title: str,
    description: Optional[str] = None,
    fields: Optional[List[Tuple[str, str, bool]]] = None,
) -> None:
    if guild is None:
        return
    channel_id = int(settings.get("log_channel_id", 0))
    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except Exception:
            return
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return
    embed = make_embed(title=title, description=description)
    if fields:
        for name, value, inline in fields[:20]:
            embed.add_field(name=name, value=trim_text(value, 1000), inline=inline)
    embed.timestamp = datetime.now(timezone.utc)
    try:
        await channel.send(embed=embed)
    except Exception:
        pass


async def safe_delete_message(message: discord.Message) -> None:
    try:
        deleted_by_bot.add(message.id)
        await message.delete()
    except Exception:
        pass


async def timeout_member(member: discord.Member, minutes: int, reason: str) -> bool:
    if is_owner(member.id) or member.guild.me is None:
        return False
    try:
        until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        await member.edit(timed_out_until=until, reason=reason)
        return True
    except Exception:
        return False


async def localban_member(
    member: discord.Member,
    reason: str,
    message: Optional[discord.Message] = None,
    actor: Optional[discord.abc.User] = None,
) -> None:
    if is_owner(member.id) or member.bot:
        return
    guild = member.guild
    me = guild.me
    if me is None:
        return

    verified_role_id = int(settings.get("verified_role_id", 0))
    previous_roles = [role.id for role in member.roles if role != guild.default_role]
    roles_to_remove: List[discord.Role] = []
    for role in member.roles:
        if role == guild.default_role:
            continue
        if role.id == verified_role_id:
            continue
        if role.managed:
            continue
        if role >= me.top_role:
            continue
        roles_to_remove.append(role)

    async def mutator(data: Dict[str, Any]):
        data[str(member.id)] = {
            "id": member.id,
            "name": str(member),
            "display_name": member.display_name,
            "guild_id": guild.id,
            "roles": previous_roles,
            "removed_roles": [role.id for role in roles_to_remove],
            "reason": reason,
            "actor": str(actor) if actor else "system",
            "created_at": utc_now_iso(),
        }

    await update_json(LOCALBAN_PATH, {}, mutator)

    if message is not None:
        await safe_delete_message(message)

    if roles_to_remove:
        try:
            await member.remove_roles(*roles_to_remove, reason=f"LocalBan: {reason}")
        except Exception:
            pass

    await send_log(
        guild,
        "🛡️ Anticrash: LocalBan",
        f"Пользователь занесён в localban и лишён ролей, которые бот смог снять.",
        [
            ("Пользователь", f"{member.mention} (`{member.id}`)", False),
            ("Причина", reason, False),
            ("Сохранённые роли", ", ".join(map(str, previous_roles)) or "нет", False),
        ],
    )


async def restore_localban(interaction: discord.Interaction, user_id: int) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("Это работает только на сервере.", ephemeral=True)
        return

    localban = read_json(LOCALBAN_PATH, {})
    entry = localban.get(str(user_id))
    if not entry:
        await interaction.response.send_message("Пользователь уже не находится в localban.", ephemeral=True)
        return

    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except Exception:
            member = None

    restored_roles: List[str] = []
    failed_roles: List[str] = []
    if member:
        me = guild.me
        roles = []
        for role_id in entry.get("roles", []):
            role = guild.get_role(int(role_id))
            if role is None or role == guild.default_role or role.managed:
                continue
            if me and role >= me.top_role:
                failed_roles.append(str(role_id))
                continue
            roles.append(role)
        if roles:
            try:
                await member.add_roles(*roles, reason=f"LocalBan restore by {interaction.user}")
                restored_roles = [str(role.id) for role in roles]
            except Exception:
                failed_roles = [str(role.id) for role in roles]

    async def mutator(data: Dict[str, Any]):
        data.pop(str(user_id), None)

    await update_json(LOCALBAN_PATH, {}, mutator)

    await send_log(
        guild,
        "✅ Anticrash: LocalBan снят",
        f"{interaction.user.mention} восстановил пользователя `{user_id}`.",
        [
            ("Вернули роли", ", ".join(restored_roles) or "нет/пользователь не найден", False),
            ("Не удалось вернуть", ", ".join(failed_roles) or "нет", False),
        ],
    )
    await interaction.response.send_message(
        f"Готово. Пользователь `{user_id}` удалён из localban. Вернул ролей: `{len(restored_roles)}`.",
        ephemeral=True,
    )


def member_label(member: Optional[discord.Member], user_id: int) -> str:
    if member:
        return f"{member.display_name} ({user_id})"
    return str(user_id)


class AnticrashBot(commands.Bot):
    async def setup_hook(self) -> None:
        # Persistent views keep buttons alive after restart, provided custom_id is set and timeout=None.
        self.add_view(VerificationButtonView())
        self.add_view(PartnersPanelView())
        self.add_view(AnticrashPanelView())


intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True
intents.voice_states = True
if hasattr(intents, "moderation"):
    intents.moderation = True
elif hasattr(intents, "guild_moderation"):
    intents.guild_moderation = True

bot = AnticrashBot(command_prefix=PREFIX, intents=intents, help_command=None)


async def require_commands(ctx: commands.Context) -> bool:
    if not isinstance(ctx.author, discord.Member):
        return False
    if has_perm(ctx.author.id, "can_commands"):
        return True
    try:
        await ctx.reply("Нет доступа к командам Anticrash.", delete_after=8, mention_author=False)
    except Exception:
        pass
    return False


async def require_interaction_perm(interaction: discord.Interaction, permission: str) -> bool:
    if interaction.user and has_perm(interaction.user.id, permission):
        return True
    try:
        await interaction.response.send_message("Нет доступа к этому действию.", ephemeral=True)
    except discord.InteractionResponded:
        await interaction.followup.send("Нет доступа к этому действию.", ephemeral=True)
    return False


class VerificationButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(emoji="🔑", style=discord.ButtonStyle.secondary, custom_id="ac:verify:grant")
    async def grant_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Это работает только на сервере.", ephemeral=True)
            return
        role_id = int(settings.get("verified_role_id", 0))
        role = interaction.guild.get_role(role_id)
        if role is None:
            await interaction.response.send_message("Роль верификации не найдена. Проверь ID роли в settings.json.", ephemeral=True)
            return
        try:
            await interaction.user.add_roles(role, reason="Verification button")
            await interaction.response.send_message("Верификация пройдена. Роль выдана.", ephemeral=True)
            await send_log(
                interaction.guild,
                "🔑 Верификация",
                f"{interaction.user.mention} получил роль {role.mention}.",
            )
        except discord.Forbidden:
            await interaction.response.send_message("Не могу выдать роль: роль бота должна быть выше роли верификации.", ephemeral=True)
        except Exception as exc:
            await interaction.response.send_message(f"Ошибка выдачи роли: `{exc}`", ephemeral=True)


class VerifySetupModal(discord.ui.Modal, title="Настройка verify"):
    image_url = discord.ui.TextInput(
        label="Ссылка на картинку",
        placeholder="https://cdn.discordapp.com/attachments/.../image.png",
        max_length=1000,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_interaction_perm(interaction, "can_commands"):
            return
        url = str(self.image_url.value).strip()
        await update_json(VERIFY_PATH, {"image_url": ""}, lambda data: data.update({"image_url": url}))
        embed = make_embed()
        embed.set_image(url=url)
        await interaction.channel.send(embed=embed, view=VerificationButtonView())
        await interaction.response.send_message("Готово. Сообщение верификации отправлено.", ephemeral=True)


class VerifySetupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Открыть настройку", emoji="⚙️", style=discord.ButtonStyle.secondary)
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_interaction_perm(interaction, "can_commands"):
            return
        await interaction.response.send_modal(VerifySetupModal())


class PartnersPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Перейти к партнёрам", style=discord.ButtonStyle.secondary, custom_id="ac:partners:list")
    async def partners_list(self, interaction: discord.Interaction, button: discord.ui.Button):
        partners = read_json(PARTNERS_PATH, [])
        if not partners:
            await interaction.response.send_message("Список партнёров пока пуст.", ephemeral=True)
            return
        await interaction.response.send_message("Выберите партнёра из списка:", view=PartnerSelectView(partners), ephemeral=True)


class PartnersSetupModal(discord.ui.Modal, title="Настройка партнёров"):
    image_url = discord.ui.TextInput(
        label="Ссылка на картинку",
        placeholder="https://cdn.discordapp.com/attachments/.../partners.png",
        max_length=1000,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_interaction_perm(interaction, "can_commands"):
            return
        url = str(self.image_url.value).strip()
        embed = make_embed(
            title="Партнёры",
            description="Для того что бы попасть к нам в партнёры, вам нужно связаться с одним из овнеров.",
        )
        embed.set_image(url=url)
        embed.add_field(name="Список партнёров", value="Чтобы посмотреть список партнёров, нажмите кнопку ниже.", inline=False)
        await interaction.channel.send(embed=embed, view=PartnersPanelView())
        await interaction.response.send_message("Готово. Сообщение партнёров отправлено.", ephemeral=True)


class PartnersSetupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Открыть настройку", emoji="⚙️", style=discord.ButtonStyle.secondary)
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_interaction_perm(interaction, "can_commands"):
            return
        await interaction.response.send_modal(PartnersSetupModal())


class PartnerAddModal(discord.ui.Modal, title="Добавить партнёра"):
    partner_name = discord.ui.TextInput(label="Название партнёра", max_length=90, required=True)
    partner_url = discord.ui.TextInput(label="Ссылка на партнёра", max_length=1000, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_interaction_perm(interaction, "can_commands"):
            return
        name = str(self.partner_name.value).strip()
        url = str(self.partner_url.value).strip()
        await add_partner(name, url, interaction.user)
        await interaction.response.send_message(f"Партнёр `{name}` добавлен.", ephemeral=True)


class PartnerAddView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Добавить партнёра", emoji="➕", style=discord.ButtonStyle.secondary)
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_interaction_perm(interaction, "can_commands"):
            return
        await interaction.response.send_modal(PartnerAddModal())


class PartnerSelectView(discord.ui.View):
    def __init__(self, partners: List[Dict[str, Any]]):
        super().__init__(timeout=120)
        self.partners = partners[:25]
        options = []
        for idx, partner in enumerate(self.partners):
            name = str(partner.get("name", f"Partner {idx+1}"))[:100]
            options.append(discord.SelectOption(label=name, value=str(idx), emoji="🔗"))
        select = discord.ui.Select(placeholder="Выберите партнёра", options=options, min_values=1, max_values=1)
        select.callback = self.on_select  # type: ignore
        self.select = select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        idx = int(self.select.values[0])
        partner = self.partners[idx]
        name = str(partner.get("name", "Партнёр"))
        url = str(partner.get("url", ""))
        await interaction.response.send_message(f"**{name}**\n{url}", ephemeral=True)


async def add_partner(name: str, url: str, user: discord.abc.User) -> None:
    async def mutator(data: List[Dict[str, Any]]):
        data.append({
            "name": name,
            "url": url,
            "added_by": str(user.id),
            "created_at": utc_now_iso(),
        })

    await update_json(PARTNERS_PATH, [], mutator)


class AnticrashPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Добавить пользователя", emoji="➕", style=discord.ButtonStyle.secondary, custom_id="ac:anticrash:add_user")
    async def add_user_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_interaction_perm(interaction, "can_manage_users"):
            return
        await interaction.response.send_modal(AddUserModal())

    @discord.ui.button(label="Выбрать пользователя", emoji="👤", style=discord.ButtonStyle.secondary, custom_id="ac:anticrash:select_user")
    async def select_user_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_interaction_perm(interaction, "can_manage_users"):
            return
        users = read_json(USERS_PATH, {})
        if not users:
            await interaction.response.send_message("Список Anticrash пуст.", ephemeral=True)
            return
        await interaction.response.send_message("Выберите пользователя:", view=AnticrashUserSelectView(users), ephemeral=True)

    @discord.ui.button(label="LocalBan", emoji="🧩", style=discord.ButtonStyle.danger, custom_id="ac:anticrash:localban")
    async def localban_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_interaction_perm(interaction, "can_manage_users"):
            return
        localban = read_json(LOCALBAN_PATH, {})
        if not localban:
            await interaction.response.send_message("LocalBan список пуст.", ephemeral=True)
            return
        await interaction.response.send_message("Выберите пользователя для восстановления:", view=LocalBanSelectView(localban), ephemeral=True)


class AddUserModal(discord.ui.Modal, title="Добавить пользователя"):
    user_id = discord.ui.TextInput(label="ID пользователя", placeholder="1389945313225080953", max_length=30, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_interaction_perm(interaction, "can_manage_users"):
            return
        uid = clean_id(str(self.user_id.value))
        if uid is None:
            await interaction.response.send_message("Неверный ID. Нужно вставить числовой Discord ID.", ephemeral=True)
            return
        guild = interaction.guild
        member = guild.get_member(uid) if guild else None
        name = str(member) if member else str(uid)

        async def mutator(data: Dict[str, Any]):
            if str(uid) not in data:
                perms = {**DEFAULT_PERMISSIONS, "can_commands": True}
                data[str(uid)] = {
                    "name": name,
                    "added_by": str(interaction.user.id),
                    "created_at": utc_now_iso(),
                    "permissions": perms,
                }
            else:
                data[str(uid)].setdefault("permissions", {**DEFAULT_PERMISSIONS, "can_commands": True})
                data[str(uid)]["name"] = name

        await update_json(USERS_PATH, {}, mutator)
        await interaction.response.send_message(
            f"Пользователь `{uid}` добавлен в Anticrash. По умолчанию включено только право `Команды`.",
            ephemeral=True,
        )
        await send_log(interaction.guild, "➕ Anticrash: пользователь добавлен", f"{interaction.user.mention} добавил `{uid}`.")


class AnticrashUserSelectView(discord.ui.View):
    def __init__(self, users: Dict[str, Any]):
        super().__init__(timeout=120)
        self.users = list(users.items())[:25]
        options = []
        for uid, record in self.users:
            label = str(record.get("name", uid))[:90]
            description = "OWNER" if str(uid) == str(OWNER_ID) else uid
            options.append(discord.SelectOption(label=label, description=description[:100], value=str(uid), emoji="👤"))
        select = discord.ui.Select(placeholder="Выберите пользователя", options=options, min_values=1, max_values=1)
        select.callback = self.on_select  # type: ignore
        self.select = select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        if not await require_interaction_perm(interaction, "can_manage_users"):
            return
        uid = int(self.select.values[0])
        await interaction.response.edit_message(embed=user_permissions_embed(uid), view=UserPermView(uid))


class UserPermView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.build_buttons()

    def build_buttons(self):
        users = read_json(USERS_PATH, {})
        record = users.get(str(self.user_id), {})
        perms = record.get("permissions", {})
        for i, (perm, label) in enumerate(PERMISSION_LABELS.items()):
            enabled = bool(perms.get(perm, False)) or is_owner(self.user_id)
            btn = discord.ui.Button(
                label=f"{label}: {'✅' if enabled else '❌'}",
                style=discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary,
                row=0 if i < 3 else 1,
                disabled=is_owner(self.user_id),
            )

            async def callback(interaction: discord.Interaction, p=perm):
                if not await require_interaction_perm(interaction, "can_manage_users"):
                    return
                if is_owner(self.user_id):
                    await interaction.response.send_message("Права владельца нельзя изменять.", ephemeral=True)
                    return

                async def mutator(data: Dict[str, Any]):
                    rec = data.setdefault(str(self.user_id), {"permissions": DEFAULT_PERMISSIONS.copy()})
                    rec.setdefault("permissions", DEFAULT_PERMISSIONS.copy())
                    current = bool(rec["permissions"].get(p, False))
                    rec["permissions"][p] = not current

                await update_json(USERS_PATH, {}, mutator)
                await interaction.response.edit_message(embed=user_permissions_embed(self.user_id), view=UserPermView(self.user_id))

            btn.callback = callback  # type: ignore
            self.add_item(btn)

        delete_btn = discord.ui.Button(label="Удалить из Anticrash", emoji="🗑️", style=discord.ButtonStyle.danger, row=2, disabled=is_owner(self.user_id))

        async def delete_callback(interaction: discord.Interaction):
            if not await require_interaction_perm(interaction, "can_manage_users"):
                return
            if is_owner(self.user_id):
                await interaction.response.send_message("Владельца нельзя удалить.", ephemeral=True)
                return

            async def mutator(data: Dict[str, Any]):
                data.pop(str(self.user_id), None)

            await update_json(USERS_PATH, {}, mutator)
            await send_log(interaction.guild, "🗑️ Anticrash: пользователь удалён", f"{interaction.user.mention} удалил `{self.user_id}`.")
            await interaction.response.edit_message(content=f"Пользователь `{self.user_id}` удалён из Anticrash.", embed=None, view=None)

        delete_btn.callback = delete_callback  # type: ignore
        self.add_item(delete_btn)


def user_permissions_embed(user_id: int) -> discord.Embed:
    users = read_json(USERS_PATH, {})
    record = users.get(str(user_id), {})
    perms = record.get("permissions", {})
    embed = make_embed(title="Настройка прав Anticrash", description=f"Пользователь: `{user_id}`")
    lines = []
    for perm, label in PERMISSION_LABELS.items():
        enabled = True if is_owner(user_id) else bool(perms.get(perm, False))
        lines.append(f"{'✅' if enabled else '❌'} **{label}**")
    embed.add_field(name="Права", value="\n".join(lines), inline=False)
    if is_owner(user_id):
        embed.add_field(name="Статус", value="Владелец. Все права включены автоматически.", inline=False)
    return embed


class LocalBanSelectView(discord.ui.View):
    def __init__(self, entries: Dict[str, Any]):
        super().__init__(timeout=120)
        self.entries = list(entries.items())[:25]
        options = []
        for uid, entry in self.entries:
            label = str(entry.get("display_name") or entry.get("name") or uid)[:90]
            desc = str(entry.get("reason", "localban"))[:100]
            options.append(discord.SelectOption(label=label, description=desc, value=str(uid), emoji="🧩"))
        select = discord.ui.Select(placeholder="Кого вернуть из LocalBan", options=options, min_values=1, max_values=1)
        select.callback = self.on_select  # type: ignore
        self.select = select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        if not await require_interaction_perm(interaction, "can_manage_users"):
            return
        uid = int(self.select.values[0])
        await restore_localban(interaction, uid)


async def get_audit_executor(guild: discord.Guild, action: discord.AuditLogAction, target_id: Optional[int] = None) -> Optional[discord.User]:
    try:
        async for entry in guild.audit_logs(limit=6, action=action):
            if target_id is not None:
                target = getattr(entry, "target", None)
                if not target or getattr(target, "id", None) != target_id:
                    continue
            age = datetime.now(timezone.utc) - entry.created_at
            if age.total_seconds() <= 20:
                return entry.user
    except Exception:
        return None
    return None


async def punish_spam(message: discord.Message, reason: str) -> None:
    member = message.author if isinstance(message.author, discord.Member) else None
    await safe_delete_message(message)
    if member and not is_owner(member.id):
        minutes = int(settings.get("spam_timeout_minutes", 60))
        timed_out = await timeout_member(member, minutes, f"Anticrash spam: {reason}")
        await send_log(
            message.guild,
            "⚠️ Anticrash: spam удалён",
            f"Сообщение удалено. Таймаут: {'да' if timed_out else 'не удалось'}.",
            [
                ("Пользователь", f"{member.mention} (`{member.id}`)", False),
                ("Причина", reason, False),
                ("Текст", trim_text(message.content), False),
            ],
        )


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} | ID: {bot.user.id if bot.user else 'unknown'}")
    print(f"Prefix: {PREFIX}")


@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    if not await require_commands(ctx):
        return
    embed = make_embed(title="Anticrash Bot — команды")
    embed.add_field(name="!verify", value="Создать сообщение верификации с картинкой и кнопкой-ключом.", inline=False)
    embed.add_field(name="!partners", value="Создать сообщение партнёров с картинкой и кнопкой списка.", inline=False)
    embed.add_field(name="!partnersadd", value="Добавить партнёра через меню. Также можно: `!partnersadd Имя | ссылка`.", inline=False)
    embed.add_field(name="!anticrash", value="Отправить панель Anticrash: пользователи, права, localban.", inline=False)
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="verify")
async def verify_cmd(ctx: commands.Context):
    if not await require_commands(ctx):
        return
    embed = make_embed(title="Настройка верификации", description="Нажми кнопку ниже и вставь ссылку на картинку. После завершения бот отправит verify-сообщение с кнопкой 🔑.")
    await ctx.reply(embed=embed, view=VerifySetupView(), mention_author=False)


@bot.command(name="partners")
async def partners_cmd(ctx: commands.Context):
    if not await require_commands(ctx):
        return
    embed = make_embed(title="Настройка партнёров", description="Нажми кнопку ниже и вставь ссылку на картинку для блока партнёров.")
    await ctx.reply(embed=embed, view=PartnersSetupView(), mention_author=False)


@bot.command(name="partnersadd")
async def partnersadd_cmd(ctx: commands.Context, *, raw: Optional[str] = None):
    if not await require_commands(ctx):
        return
    if raw and "|" in raw:
        name, url = [part.strip() for part in raw.split("|", 1)]
        if not name or not url:
            await ctx.reply("Формат: `!partnersadd Название | ссылка`", mention_author=False)
            return
        await add_partner(name, url, ctx.author)
        await ctx.reply(f"Партнёр `{name}` добавлен.", mention_author=False)
        return
    embed = make_embed(title="Добавить партнёра", description="Нажми кнопку ниже и введи название + ссылку.")
    await ctx.reply(embed=embed, view=PartnerAddView(), mention_author=False)


@bot.command(name="anticrash")
async def anticrash_cmd(ctx: commands.Context):
    if not await require_commands(ctx):
        return
    embed = make_embed(title="Anticrash Panel", description="Управление доступами, правами пользователей и LocalBan.")
    embed.set_image(url=str(settings.get("anti_crash_image_url", "")))
    embed.add_field(name="Кнопки", value="➕ Добавить пользователя\n👤 Выбрать пользователя\n🧩 LocalBan", inline=False)
    await ctx.send(embed=embed, view=AnticrashPanelView())


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply("Не хватает аргументов команды.", mention_author=False)
        return
    await ctx.reply(f"Ошибка команды: `{error}`", mention_author=False)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return

    if message.content.startswith(PREFIX):
        await bot.process_commands(message)
        return

    violation = detect_violation(message)
    if violation:
        kind, reason = violation
        member = message.author if isinstance(message.author, discord.Member) else None
        if kind == "link" and member and not has_perm(member.id, "can_links"):
            await localban_member(member, f"Нет права отправлять ссылки: {reason}", message=message)
            return
        await punish_spam(message, reason)
        return

    await bot.process_commands(message)


@bot.event
async def on_message_delete(message: discord.Message):
    if message.guild is None or message.author.bot:
        return
    if message.id in deleted_by_bot:
        deleted_by_bot.discard(message.id)
        return
    await send_log(
        message.guild,
        "🗑️ Сообщение удалено",
        None,
        [
            ("Автор", f"{message.author} (`{message.author.id}`)", False),
            ("Канал", getattr(message.channel, "mention", str(message.channel)), True),
            ("Текст", trim_text(message.content), False),
        ],
    )


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.guild is None or before.author.bot:
        return
    if before.content == after.content:
        return
    await send_log(
        before.guild,
        "✏️ Сообщение изменено",
        None,
        [
            ("Автор", f"{before.author} (`{before.author.id}`)", False),
            ("Канал", getattr(before.channel, "mention", str(before.channel)), True),
            ("Было", trim_text(before.content), False),
            ("Стало", trim_text(after.content), False),
        ],
    )


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot:
        return
    if before.channel == after.channel:
        return
    if before.channel is None and after.channel is not None:
        title = "🎙️ Вход в voice"
        desc = f"{member.mention} зашёл в `{after.channel.name}`."
    elif before.channel is not None and after.channel is None:
        title = "🎙️ Выход из voice"
        desc = f"{member.mention} вышел из `{before.channel.name}`."
    else:
        title = "🎙️ Переход voice"
        desc = f"{member.mention}: `{before.channel.name}` → `{after.channel.name}`."
    await send_log(member.guild, title, desc)


@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    executor = await get_audit_executor(guild, discord.AuditLogAction.ban, user.id)
    await send_log(guild, "⛔ Бан пользователя", f"Забанен: `{user}` (`{user.id}`)\nИсполнитель: `{executor}`")
    if executor and not has_perm(executor.id, "can_ban") and not executor.bot:
        try:
            await guild.unban(user, reason="Anticrash: unauthorized ban rollback")
        except Exception:
            pass
        member = guild.get_member(executor.id)
        if isinstance(member, discord.Member):
            await localban_member(member, f"Unauthorized ban of {user.id}", actor=executor)


@bot.event
async def on_member_remove(member: discord.Member):
    executor = await get_audit_executor(member.guild, discord.AuditLogAction.kick, member.id)
    if executor:
        await send_log(member.guild, "👢 Кик пользователя", f"Кикнут: `{member}` (`{member.id}`)\nИсполнитель: `{executor}`")
        if not has_perm(executor.id, "can_kick") and not executor.bot:
            actor_member = member.guild.get_member(executor.id)
            if isinstance(actor_member, discord.Member):
                await localban_member(actor_member, f"Unauthorized kick of {member.id}", actor=executor)


@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    guild = channel.guild
    executor = await get_audit_executor(guild, discord.AuditLogAction.channel_delete, channel.id)
    await send_log(guild, "🧨 Канал удалён", f"Канал: `{channel.name}` (`{channel.id}`)\nИсполнитель: `{executor}`")
    if executor and not has_perm(executor.id, "can_manage_users") and not executor.bot:
        actor_member = guild.get_member(executor.id)
        if isinstance(actor_member, discord.Member):
            await localban_member(actor_member, f"Unauthorized channel delete: {channel.name}", actor=executor)
        try:
            cloned = await channel.clone(reason="Anticrash: channel restore")
            if hasattr(channel, "position"):
                await cloned.edit(position=channel.position, reason="Anticrash: channel position restore")
            await send_log(guild, "✅ Канал восстановлен", f"Восстановлен клон: `{cloned.name}`.")
        except Exception as exc:
            await send_log(guild, "⚠️ Канал не удалось восстановить", f"Ошибка: `{exc}`")


@bot.event
async def on_guild_role_delete(role: discord.Role):
    guild = role.guild
    executor = await get_audit_executor(guild, discord.AuditLogAction.role_delete, role.id)
    await send_log(guild, "🧨 Роль удалена", f"Роль: `{role.name}` (`{role.id}`)\nИсполнитель: `{executor}`")
    if executor and not has_perm(executor.id, "can_manage_users") and not executor.bot:
        actor_member = guild.get_member(executor.id)
        if isinstance(actor_member, discord.Member):
            await localban_member(actor_member, f"Unauthorized role delete: {role.name}", actor=executor)
        try:
            new_role = await guild.create_role(
                name=role.name,
                permissions=role.permissions,
                colour=role.colour,
                hoist=role.hoist,
                mentionable=role.mentionable,
                reason="Anticrash: role restore",
            )
            await send_log(guild, "✅ Роль восстановлена", f"Создана новая роль: `{new_role.name}` (`{new_role.id}`).")
        except Exception as exc:
            await send_log(guild, "⚠️ Роль не удалось восстановить", f"Ошибка: `{exc}`")


if __name__ == "__main__":
    bot.run(get_token())
