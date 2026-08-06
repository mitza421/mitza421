import discord
from redbot.core import commands, Config, checks
from redbot.core.bot import Red
from typing import Optional

# Preset bitrate options offered on the panel (in kbps). Whatever the user
# picks gets clamped down to whatever the server's boost tier actually
# allows, so this list can just stay generous.
BITRATE_PRESETS = [64, 96, 128, 256, 384]


def room_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="🔊 Private Rooms",
        description=(
            "Join the **hub voice channel** to instantly get your own private room.\n\n"
            "Use the buttons below to manage **your own** room from anywhere — "
            "you don't need to be in the room's own chat to use these.\n\n"
            "🔒 **Lock** — stop new people from joining\n"
            "🔓 **Unlock** — let anyone join again\n"
            "🙈 **Hide** — hide the room from the channel list\n"
            "👁️ **Unhide** — make the room visible again\n"
            "✏️ **Rename** — change your room's name\n"
            "👥 **Limit** — set a max number of people (0 = unlimited)\n"
            "🦵 **Kick** — remove someone from your room\n"
            "👑 **Claim** — take ownership of an empty-of-owner room\n"
            "🎚️ **Quality** — set the voice bitrate (capped by server boost level)"
        ),
        colour=discord.Colour.blurple(),
    )
    embed.set_footer(text="You must be the owner of a room to manage it (except Claim).")
    return embed


class RenameModal(discord.ui.Modal, title="Rename your room"):
    name = discord.ui.TextInput(
        label="New room name", max_length=95, min_length=1, required=True
    )

    def __init__(self, cog: "PrivateRooms", channel: discord.VoiceChannel):
        super().__init__()
        self.cog = cog
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await self.channel.edit(name=str(self.name), reason=f"Renamed by {interaction.user}")
            await interaction.response.send_message(
                f"Room renamed to **{self.name}**.", ephemeral=True
            )
        except discord.HTTPException as e:
            await interaction.response.send_message(f"Couldn't rename the room: {e}", ephemeral=True)


class LimitModal(discord.ui.Modal, title="Set user limit"):
    limit = discord.ui.TextInput(
        label="Max users (0 = unlimited, max 99)", max_length=2, min_length=1, required=True
    )

    def __init__(self, cog: "PrivateRooms", channel: discord.VoiceChannel):
        super().__init__()
        self.cog = cog
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.limit).strip()
        if not raw.isdigit():
            await interaction.response.send_message("That's not a valid number.", ephemeral=True)
            return
        value = int(raw)
        if value > 99:
            value = 99
        try:
            await self.channel.edit(user_limit=value, reason=f"Limit set by {interaction.user}")
            shown = "unlimited" if value == 0 else str(value)
            await interaction.response.send_message(f"User limit set to **{shown}**.", ephemeral=True)
        except discord.HTTPException as e:
            await interaction.response.send_message(f"Couldn't set the limit: {e}", ephemeral=True)


class KickSelectView(discord.ui.View):
    def __init__(self, cog: "PrivateRooms", channel: discord.VoiceChannel):
        super().__init__(timeout=60)
        self.cog = cog
        self.channel = channel
        self.add_item(self.KickSelect(channel))

    class KickSelect(discord.ui.UserSelect):
        def __init__(self, channel: discord.VoiceChannel):
            super().__init__(placeholder="Choose a member to remove from your room…", min_values=1, max_values=1)
            self.channel = channel

        async def callback(self, interaction: discord.Interaction):
            member = self.values[0]
            if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel != self.channel:
                await interaction.response.send_message("That member isn't in your room.", ephemeral=True)
                return
            try:
                await member.move_to(None, reason=f"Kicked by room owner {interaction.user}")
                await interaction.response.send_message(f"Removed **{member.display_name}** from your room.", ephemeral=True)
            except discord.HTTPException as e:
                await interaction.response.send_message(f"Couldn't remove them: {e}", ephemeral=True)


class BitrateSelect(discord.ui.Select):
    def __init__(self, cog: "PrivateRooms"):
        options = [
            discord.SelectOption(label=f"{kbps} kbps", value=str(kbps))
            for kbps in BITRATE_PRESETS
        ]
        super().__init__(
            placeholder="🎚️ Set voice quality (bitrate) for your room…",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="prooms:bitrate",
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        channel = await self.cog.get_owned_channel(interaction)
        if channel is None:
            await interaction.response.send_message(
                "You don't currently own a private room.", ephemeral=True
            )
            return
        requested_bps = int(self.values[0]) * 1000
        max_bps = interaction.guild.bitrate_limit
        actual_bps = min(requested_bps, max_bps)
        try:
            await channel.edit(bitrate=actual_bps, reason=f"Quality set by {interaction.user}")
            await interaction.response.send_message(
                f"Room bitrate set to **{actual_bps // 1000} kbps**"
                + (" (capped by your server's boost level)." if actual_bps < requested_bps else "."),
                ephemeral=True,
            )
        except discord.HTTPException as e:
            await interaction.response.send_message(f"Couldn't change quality: {e}", ephemeral=True)


class ControlPanelView(discord.ui.View):
    """A single, persistent view. One instance is registered globally in
    cog_load and works for every guild/message it's attached to, since every
    callback resolves the acting member's room dynamically."""

    def __init__(self, cog: "PrivateRooms"):
        super().__init__(timeout=None)
        self.cog = cog
        self.add_item(BitrateSelect(cog))

    async def _get_channel_or_warn(self, interaction: discord.Interaction) -> Optional[discord.VoiceChannel]:
        channel = await self.cog.get_owned_channel(interaction)
        if channel is None:
            await interaction.response.send_message(
                "You don't currently own a private room. Join the hub voice channel to create one.",
                ephemeral=True,
            )
        return channel

    @discord.ui.button(label="Lock", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="prooms:lock", row=0)
    async def lock(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = await self._get_channel_or_warn(interaction)
        if channel is None:
            return
        overwrite = channel.overwrites_for(channel.guild.default_role)
        overwrite.connect = False
        await channel.set_permissions(channel.guild.default_role, overwrite=overwrite, reason=f"Locked by {interaction.user}")
        await interaction.response.send_message("🔒 Room locked. Only existing members and anyone you allow can join.", ephemeral=True)

    @discord.ui.button(label="Unlock", emoji="🔓", style=discord.ButtonStyle.success, custom_id="prooms:unlock", row=0)
    async def unlock(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = await self._get_channel_or_warn(interaction)
        if channel is None:
            return
        overwrite = channel.overwrites_for(channel.guild.default_role)
        overwrite.connect = None
        await channel.set_permissions(channel.guild.default_role, overwrite=overwrite, reason=f"Unlocked by {interaction.user}")
        await interaction.response.send_message("🔓 Room unlocked. Anyone can join now.", ephemeral=True)

    @discord.ui.button(label="Hide", emoji="🙈", style=discord.ButtonStyle.secondary, custom_id="prooms:hide", row=0)
    async def hide(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = await self._get_channel_or_warn(interaction)
        if channel is None:
            return
        overwrite = channel.overwrites_for(channel.guild.default_role)
        overwrite.view_channel = False
        await channel.set_permissions(channel.guild.default_role, overwrite=overwrite, reason=f"Hidden by {interaction.user}")
        await interaction.response.send_message("🙈 Room hidden from the channel list.", ephemeral=True)

    @discord.ui.button(label="Unhide", emoji="👁️", style=discord.ButtonStyle.secondary, custom_id="prooms:unhide", row=0)
    async def unhide(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = await self._get_channel_or_warn(interaction)
        if channel is None:
            return
        overwrite = channel.overwrites_for(channel.guild.default_role)
        overwrite.view_channel = None
        await channel.set_permissions(channel.guild.default_role, overwrite=overwrite, reason=f"Unhidden by {interaction.user}")
        await interaction.response.send_message("👁️ Room visible again.", ephemeral=True)

    @discord.ui.button(label="Rename", emoji="✏️", style=discord.ButtonStyle.primary, custom_id="prooms:rename", row=1)
    async def rename(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = await self.cog.get_owned_channel(interaction)
        if channel is None:
            await interaction.response.send_message("You don't currently own a private room.", ephemeral=True)
            return
        await interaction.response.send_modal(RenameModal(self.cog, channel))

    @discord.ui.button(label="Limit", emoji="👥", style=discord.ButtonStyle.primary, custom_id="prooms:limit", row=1)
    async def limit(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = await self.cog.get_owned_channel(interaction)
        if channel is None:
            await interaction.response.send_message("You don't currently own a private room.", ephemeral=True)
            return
        await interaction.response.send_modal(LimitModal(self.cog, channel))

    @discord.ui.button(label="Kick", emoji="🦵", style=discord.ButtonStyle.danger, custom_id="prooms:kick", row=1)
    async def kick(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = await self._get_channel_or_warn(interaction)
        if channel is None:
            return
        if len(channel.members) <= 1:
            await interaction.response.send_message("There's nobody else in your room to remove.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Choose who to remove:", view=KickSelectView(self.cog, channel), ephemeral=True
        )

    @discord.ui.button(label="Claim", emoji="👑", style=discord.ButtonStyle.secondary, custom_id="prooms:claim", row=1)
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user
        if member.voice is None or member.voice.channel is None:
            await interaction.response.send_message("You need to be in the room you want to claim.", ephemeral=True)
            return
        channel = member.voice.channel
        rooms = await self.cog.config.guild(interaction.guild).rooms()
        owner_id = rooms.get(str(channel.id))
        if owner_id is None:
            await interaction.response.send_message("This isn't a private room.", ephemeral=True)
            return
        owner_still_here = any(m.id == owner_id for m in channel.members)
        if owner_still_here and owner_id != member.id:
            await interaction.response.send_message("The current owner is still in the room.", ephemeral=True)
            return
        await self.cog.set_owner(interaction.guild, channel, member)
        await interaction.response.send_message("👑 You are now the owner of this room.", ephemeral=True)


class PrivateRooms(commands.Cog):
    """Join-to-create private voice rooms with a shared control panel."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=847362910457, force_registration=True)
        default_guild = {
            "hub_channel": None,
            "category": None,
            "panel_channel": None,
            "panel_message": None,
            "default_limit": 0,
            "name_format": "{user}'s Room",
            "rooms": {},  # str(voice_channel_id) -> owner_id
        }
        self.config.register_guild(**default_guild)
        self._panel_view_added = False

    async def cog_load(self):
        if not self._panel_view_added:
            self.bot.add_view(ControlPanelView(self))
            self._panel_view_added = True

    # ---------- helpers ----------

    async def get_owned_channel(self, interaction: discord.Interaction) -> Optional[discord.VoiceChannel]:
        guild = interaction.guild
        if guild is None:
            return None
        rooms = await self.config.guild(guild).rooms()
        for channel_id_str, owner_id in rooms.items():
            if owner_id == interaction.user.id:
                channel = guild.get_channel(int(channel_id_str))
                if isinstance(channel, discord.VoiceChannel):
                    return channel
        return None

    async def set_owner(self, guild: discord.Guild, channel: discord.VoiceChannel, member: discord.Member):
        async with self.config.guild(guild).rooms() as rooms:
            old_owner_id = rooms.get(str(channel.id))
            rooms[str(channel.id)] = member.id
        try:
            if old_owner_id and old_owner_id != member.id:
                old_owner = guild.get_member(old_owner_id)
                if old_owner:
                    await channel.set_permissions(old_owner, overwrite=None, reason="Ownership transferred")
            await channel.set_permissions(
                member,
                connect=True,
                manage_channels=True,
                move_members=True,
                mute_members=True,
                deafen_members=True,
                reason="New room owner",
            )
        except discord.HTTPException:
            pass

    async def create_room(self, member: discord.Member):
        guild = member.guild
        settings = await self.config.guild(guild).all()
        category = guild.get_channel(settings["category"]) if settings["category"] else None
        hub = guild.get_channel(settings["hub_channel"])

        name = settings["name_format"].format(user=member.display_name)[:95]
        bitrate = guild.bitrate_limit  # max allowed for this server's boost tier

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(connect=True, view_channel=True),
            member: discord.PermissionOverwrite(
                connect=True,
                manage_channels=True,
                move_members=True,
                mute_members=True,
                deafen_members=True,
            ),
        }

        try:
            channel = await guild.create_voice_channel(
                name=name,
                category=category if isinstance(category, discord.CategoryChannel) else (hub.category if hub else None),
                bitrate=bitrate,
                user_limit=settings["default_limit"],
                overwrites=overwrites,
                reason=f"Private room created for {member}",
            )
        except discord.HTTPException:
            return

        async with self.config.guild(guild).rooms() as rooms:
            rooms[str(channel.id)] = member.id

        try:
            await member.move_to(channel, reason="Moved to their new private room")
        except discord.HTTPException:
            pass

    async def maybe_delete_room(self, channel: discord.VoiceChannel):
        guild = channel.guild
        rooms = await self.config.guild(guild).rooms()
        if str(channel.id) not in rooms:
            return
        if len(channel.members) > 0:
            return
        async with self.config.guild(guild).rooms() as rooms:
            rooms.pop(str(channel.id), None)
        try:
            await channel.delete(reason="Private room empty")
        except discord.HTTPException:
            pass

    # ---------- listener ----------

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        guild = member.guild
        settings = await self.config.guild(guild).all()
        hub_id = settings["hub_channel"]

        if after.channel is not None and hub_id is not None and after.channel.id == hub_id:
            await self.create_room(member)

        if before.channel is not None and str(before.channel.id) in settings["rooms"]:
            await self.maybe_delete_room(before.channel)

    # ---------- commands ----------

    @commands.group(name="prooms", aliases=["privaterooms", "privateroom"])
    @commands.guild_only()
    async def prooms(self, ctx: commands.Context):
        """Manage the private rooms system."""

    @prooms.group(name="setup")
    @checks.admin_or_permissions(manage_guild=True)
    async def prooms_setup(self, ctx: commands.Context):
        """Configure the private rooms system."""

    @prooms_setup.command(name="hub")
    async def setup_hub(self, ctx: commands.Context, channel: discord.VoiceChannel):
        """Set the voice channel people join to create their own room."""
        await self.config.guild(ctx.guild).hub_channel.set(channel.id)
        await ctx.send(f"✅ Hub voice channel set to {channel.mention}. Joining it will create a private room.")

    @prooms_setup.command(name="category")
    async def setup_category(self, ctx: commands.Context, category: discord.CategoryChannel):
        """Set the category new private rooms are created under."""
        await self.config.guild(ctx.guild).category.set(category.id)
        await ctx.send(f"✅ New rooms will be created under **{category.name}**.")

    @prooms_setup.command(name="limit")
    async def setup_limit(self, ctx: commands.Context, limit: int):
        """Set the default user limit for new rooms (0 = unlimited)."""
        limit = max(0, min(limit, 99))
        await self.config.guild(ctx.guild).default_limit.set(limit)
        await ctx.send(f"✅ Default user limit set to **{limit if limit else 'unlimited'}**.")

    @prooms_setup.command(name="panel")
    async def setup_panel(self, ctx: commands.Context, channel: discord.TextChannel):
        """
        Post (or refresh) the control panel in the given text channel.

        This is the single shared interface everyone uses to manage their
        own room — it's independent of any voice channel's built-in chat.
        """
        view = ControlPanelView(self)
        message = await channel.send(embed=room_embed(ctx.guild), view=view)
        await self.config.guild(ctx.guild).panel_channel.set(channel.id)
        await self.config.guild(ctx.guild).panel_message.set(message.id)
        await ctx.send(f"✅ Control panel posted in {channel.mention}.")

    @prooms.command(name="settings")
    async def prooms_settings(self, ctx: commands.Context):
        """Show the current configuration."""
        settings = await self.config.guild(ctx.guild).all()
        guild = ctx.guild
        hub = guild.get_channel(settings["hub_channel"]) if settings["hub_channel"] else None
        category = guild.get_channel(settings["category"]) if settings["category"] else None
        panel_channel = guild.get_channel(settings["panel_channel"]) if settings["panel_channel"] else None

        embed = discord.Embed(title="Private Rooms — Settings", colour=discord.Colour.blurple())
        embed.add_field(name="Hub channel", value=hub.mention if hub else "Not set", inline=False)
        embed.add_field(name="Category", value=category.name if category else "Same as hub", inline=False)
        embed.add_field(name="Control panel channel", value=panel_channel.mention if panel_channel else "Not set", inline=False)
        embed.add_field(name="Default user limit", value=str(settings["default_limit"]) or "Unlimited", inline=False)
        embed.add_field(name="Max bitrate (boost tier)", value=f"{guild.bitrate_limit // 1000} kbps", inline=False)
        embed.add_field(name="Active rooms", value=str(len(settings["rooms"])), inline=False)
        await ctx.send(embed=embed)
