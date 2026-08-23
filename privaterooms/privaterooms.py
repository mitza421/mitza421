import discord
from redbot.core import commands, Config, checks
from redbot.core.bot import Red
from typing import Optional


def room_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="<:frg_joinvc:1535209033420505138> Fried Gang | VOICE HUB",
        description=(
            "Join  **🔊 CREATE PUBLIC**  for a room anyone can join, or  **🔒 CREATE PRIVATE**  "
            "for a room that's locked to just you until you let others in.\n\n"
            "Use the buttons below to manage **your own** room from anywhere — "
            "you don't need to be in the room's own chat to use these.\n\n"
            "<:frg_lock:1535326926670008411> **Lock** — stop new people from joining\n"
            "<:frg_unlock:1535326925516709928> **Unlock** — let anyone join again\n"
            "<:frg_hide:1535326883216883752> **Hide** — hide the room from the channel list\n"
            "<:frg_unhide:1535326713880514602> **Unhide** — make the room visible again\n"
            "<:frg_rename:1535326922387488959> **Rename** — change your room's name\n"
            "<:frg_limit:1535326921271812236> **Limit** — set a max number of people (0 = unlimited)\n"
            "<:frg_kick:1535326920420630660> **Kick** — remove someone from your room\n"
            "<:frg_claim:1541154764660678727> **Claim** — take ownership of an empty-of-owner room"
        ),
        colour=discord.Colour.blurple(),
    )
    embed.set_footer(text="Rooms are always created at your server's maximum voice quality. You must own a room to manage it (except Claim).")
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


class ControlPanelView(discord.ui.View):
    """A single, persistent view. One instance is registered globally in
    cog_load and works for every guild/message it's attached to, since every
    callback resolves the acting member's room dynamically."""

    def __init__(self, cog: "PrivateRooms"):
        super().__init__(timeout=None)
        self.cog = cog

    async def _get_channel_or_warn(self, interaction: discord.Interaction) -> Optional[discord.VoiceChannel]:
        channel = await self.cog.get_owned_channel(interaction)
        if channel is None:
            await interaction.response.send_message(
                "You don't currently own a room. Join **CREATE PUBLIC** or **CREATE PRIVATE** to create one.",
                ephemeral=True,
            )
        return channel

    @discord.ui.button(label="Lock", emoji=discord.PartialEmoji(name="frg_lock", id=1535326926670008411), style=discord.ButtonStyle.secondary, custom_id="prooms:lock", row=0)
    async def lock(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = await self._get_channel_or_warn(interaction)
        if channel is None:
            return
        overwrite = channel.overwrites_for(channel.guild.default_role)
        overwrite.connect = False
        await channel.set_permissions(channel.guild.default_role, overwrite=overwrite, reason=f"Locked by {interaction.user}")
        await interaction.response.send_message("<:frg_lock:1535326926670008411> Room locked. Only existing members and anyone you allow can join.", ephemeral=True)

    @discord.ui.button(label="Unlock", emoji=discord.PartialEmoji(name="frg_unlock", id=1535326925516709928), style=discord.ButtonStyle.secondary, custom_id="prooms:unlock", row=0)
    async def unlock(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = await self._get_channel_or_warn(interaction)
        if channel is None:
            return
        overwrite = channel.overwrites_for(channel.guild.default_role)
        overwrite.connect = None
        await channel.set_permissions(channel.guild.default_role, overwrite=overwrite, reason=f"Unlocked by {interaction.user}")
        await interaction.response.send_message("<:frg_unlock:1535326925516709928> Room unlocked. Anyone can join now.", ephemeral=True)

    @discord.ui.button(label="Hide", emoji=discord.PartialEmoji(name="frg_hide", id=1535326883216883752), style=discord.ButtonStyle.secondary, custom_id="prooms:hide", row=0)
    async def hide(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = await self._get_channel_or_warn(interaction)
        if channel is None:
            return
        overwrite = channel.overwrites_for(channel.guild.default_role)
        overwrite.view_channel = False
        await channel.set_permissions(channel.guild.default_role, overwrite=overwrite, reason=f"Hidden by {interaction.user}")
        await interaction.response.send_message("<:frg_hide:1535326883216883752> Room hidden from the channel list.", ephemeral=True)

    @discord.ui.button(label="Unhide", emoji=discord.PartialEmoji(name="frg_unhide", id=1535326713880514602), style=discord.ButtonStyle.secondary, custom_id="prooms:unhide", row=0)
    async def unhide(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = await self._get_channel_or_warn(interaction)
        if channel is None:
            return
        overwrite = channel.overwrites_for(channel.guild.default_role)
        overwrite.view_channel = None
        await channel.set_permissions(channel.guild.default_role, overwrite=overwrite, reason=f"Unhidden by {interaction.user}")
        await interaction.response.send_message("<:frg_unhide:1535326713880514602> Room visible again.", ephemeral=True)

    @discord.ui.button(label="Rename", emoji=discord.PartialEmoji(name="frg_rename", id=1535326922387488959), style=discord.ButtonStyle.secondary, custom_id="prooms:rename", row=1)
    async def rename(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = await self.cog.get_owned_channel(interaction)
        if channel is None:
            await interaction.response.send_message("You don't currently own a room.", ephemeral=True)
            return
        await interaction.response.send_modal(RenameModal(self.cog, channel))

    @discord.ui.button(label="Limit", emoji=discord.PartialEmoji(name="frg_limit", id=1535326921271812236), style=discord.ButtonStyle.secondary, custom_id="prooms:limit", row=1)
    async def limit(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = await self.cog.get_owned_channel(interaction)
        if channel is None:
            await interaction.response.send_message("You don't currently own a room.", ephemeral=True)
            return
        await interaction.response.send_modal(LimitModal(self.cog, channel))

    @discord.ui.button(label="Kick", emoji=discord.PartialEmoji(name="frg_kick", id=1535326920420630660), style=discord.ButtonStyle.secondary, custom_id="prooms:kick", row=1)
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

    @discord.ui.button(label="Claim", emoji=discord.PartialEmoji(name="frg_claim", id=1541154764660678727), style=discord.ButtonStyle.secondary, custom_id="prooms:claim", row=1)
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user
        if member.voice is None or member.voice.channel is None:
            await interaction.response.send_message("You need to be in the room you want to claim.", ephemeral=True)
            return
        channel = member.voice.channel
        rooms = await self.cog.config.guild(interaction.guild).rooms()
        data = rooms.get(str(channel.id))
        if data is None:
            await interaction.response.send_message("This isn't a private room.", ephemeral=True)
            return
        owner_id = data.get("owner")
        owner_still_here = any(m.id == owner_id for m in channel.members)
        if owner_still_here and owner_id != member.id:
            await interaction.response.send_message("The current owner is still in the room.", ephemeral=True)
            return
        await self.cog.set_owner(interaction.guild, channel, member)
        await interaction.response.send_message("<:frg_claim:1535326919472586837> You are now the owner of this room.", ephemeral=True)


class PrivateRooms(commands.Cog):
    """Join-to-create voice rooms (public or private) with a shared control panel."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=847362910457, force_registration=True)
        default_guild = {
            "hub_private": None,
            "hub_public": None,
            "category": None,
            "panel_channel": None,
            "panel_message": None,
            "default_limit": 0,
            "name_format_private": "🔒 {user}'s Room",
            "name_format_public": "🌐 {user}'s Room",
            "rooms": {},  # str(voice_channel_id) -> {"owner": user_id, "public": bool}
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
        for channel_id_str, data in rooms.items():
            if data.get("owner") == interaction.user.id:
                channel = guild.get_channel(int(channel_id_str))
                if isinstance(channel, discord.VoiceChannel):
                    return channel
        return None

    async def set_owner(self, guild: discord.Guild, channel: discord.VoiceChannel, member: discord.Member):
        async with self.config.guild(guild).rooms() as rooms:
            entry = rooms.get(str(channel.id), {})
            old_owner_id = entry.get("owner")
            entry["owner"] = member.id
            rooms[str(channel.id)] = entry
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

    async def create_room(self, member: discord.Member, public: bool):
        guild = member.guild
        settings = await self.config.guild(guild).all()
        category = guild.get_channel(settings["category"]) if settings["category"] else None
        hub_id = settings["hub_public"] if public else settings["hub_private"]
        hub = guild.get_channel(hub_id) if hub_id else None

        name_format = settings["name_format_public"] if public else settings["name_format_private"]
        name = name_format.format(user=member.display_name)[:95]

        # Rooms are always created at the maximum bitrate the server's boost
        # level allows (e.g. up to 384 kbps on Tier 3).
        bitrate = guild.bitrate_limit

        everyone_overwrite = discord.PermissionOverwrite(
            connect=True if public else False,
            view_channel=True,
        )
        overwrites = {
            guild.default_role: everyone_overwrite,
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
                reason=f"{'Public' if public else 'Private'} room created for {member}",
            )
        except discord.HTTPException:
            return

        async with self.config.guild(guild).rooms() as rooms:
            rooms[str(channel.id)] = {"owner": member.id, "public": public}

        try:
            await member.move_to(channel, reason="Moved to their new room")
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
            await channel.delete(reason="Room empty")
        except discord.HTTPException:
            pass

    # ---------- listener ----------

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        guild = member.guild
        settings = await self.config.guild(guild).all()
        hub_private_id = settings["hub_private"]
        hub_public_id = settings["hub_public"]

        if after.channel is not None:
            if hub_private_id is not None and after.channel.id == hub_private_id:
                await self.create_room(member, public=False)
            elif hub_public_id is not None and after.channel.id == hub_public_id:
                await self.create_room(member, public=True)

        if before.channel is not None and str(before.channel.id) in settings["rooms"]:
            await self.maybe_delete_room(before.channel)

    # ---------- commands ----------

    @commands.group(name="prooms", aliases=["privaterooms", "privateroom"])
    @commands.guild_only()
    async def prooms(self, ctx: commands.Context):
        """Manage the private/public rooms system."""

    @prooms.group(name="setup")
    @checks.admin_or_permissions(manage_guild=True)
    async def prooms_setup(self, ctx: commands.Context):
        """Configure the rooms system."""

    @prooms_setup.command(name="autohubs")
    async def setup_autohubs(self, ctx: commands.Context, category: Optional[discord.CategoryChannel] = None):
        """
        Create the two "CREATE PUBLIC" / "CREATE PRIVATE" hub voice channels
        for you and wire them up automatically.
        """
        try:
            public_channel = await ctx.guild.create_voice_channel(
                name="CREATE PUBLIC", category=category, reason="Private rooms setup"
            )
            private_channel = await ctx.guild.create_voice_channel(
                name="CREATE PRIVATE", category=category, reason="Private rooms setup"
            )
        except discord.HTTPException as e:
            await ctx.send(f"Couldn't create the channels: {e}")
            return

        await self.config.guild(ctx.guild).hub_public.set(public_channel.id)
        await self.config.guild(ctx.guild).hub_private.set(private_channel.id)
        if category is not None:
            await self.config.guild(ctx.guild).category.set(category.id)

        await ctx.send(
            f"✅ Created {public_channel.mention} and {private_channel.mention} and set them as the hubs."
        )

    @prooms_setup.command(name="hubpublic")
    async def setup_hub_public(self, ctx: commands.Context, channel: discord.VoiceChannel):
        """Set an existing voice channel as the "create public room" hub."""
        await self.config.guild(ctx.guild).hub_public.set(channel.id)
        await ctx.send(f"✅ {channel.mention} will now create **public** rooms when joined.")

    @prooms_setup.command(name="hubprivate")
    async def setup_hub_private(self, ctx: commands.Context, channel: discord.VoiceChannel):
        """Set an existing voice channel as the "create private room" hub."""
        await self.config.guild(ctx.guild).hub_private.set(channel.id)
        await ctx.send(f"✅ {channel.mention} will now create **private** rooms when joined.")

    @prooms_setup.command(name="category")
    async def setup_category(self, ctx: commands.Context, category: discord.CategoryChannel):
        """Set the category new rooms are created under."""
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
        hub_public = guild.get_channel(settings["hub_public"]) if settings["hub_public"] else None
        hub_private = guild.get_channel(settings["hub_private"]) if settings["hub_private"] else None
        category = guild.get_channel(settings["category"]) if settings["category"] else None
        panel_channel = guild.get_channel(settings["panel_channel"]) if settings["panel_channel"] else None

        embed = discord.Embed(title="Rooms — Settings", colour=discord.Colour.blurple())
        embed.add_field(name="CREATE PUBLIC hub", value=hub_public.mention if hub_public else "Not set", inline=False)
        embed.add_field(name="CREATE PRIVATE hub", value=hub_private.mention if hub_private else "Not set", inline=False)
        embed.add_field(name="Category", value=category.name if category else "Same as hub", inline=False)
        embed.add_field(name="Control panel channel", value=panel_channel.mention if panel_channel else "Not set", inline=False)
        embed.add_field(name="Default user limit", value=str(settings["default_limit"]) or "Unlimited", inline=False)
        embed.add_field(name="Voice quality", value=f"Always max — {guild.bitrate_limit // 1000} kbps on this server", inline=False)
        embed.add_field(name="Active rooms", value=str(len(settings["rooms"])), inline=False)
        await ctx.send(embed=embed)
