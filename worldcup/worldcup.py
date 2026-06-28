"""
World Cup 2026 cog for Red-DiscordBot.

Features:
- .wc live        -> shows live matches with score + elapsed time
- .wc upcoming     -> shows today's upcoming matches
- .wc group <X>    -> shows the standings table for group X (A-L)
- .wc bet <team> <amount> -> bet server currency on a team to win (pre-kickoff only)
- .wc mybets       -> shows your bets today and remaining daily allowance
- .wc admin setup  -> guided admin setup
- admin subcommands to configure goal alert channel, match thread channel,
  and live-score channel groups (voice/text channels that get renamed)

Data source: football-data.org (v4 API), competition code "WC".
Store the API key with:  [p]set api footballdata api_key,YOUR_KEY
(do this in DMs with the bot, never in a public channel)
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
import discord
from discord.ext import tasks
from redbot.core import Config, bank, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box, humanize_number, pagify

log = logging.getLogger("red.worldcup2026")

API_BASE = "https://api.football-data.org/v4"
COMPETITION = "WC"

# Minimum seconds between renames of the *same* channel.
# Discord allows 2 renames per 10 minutes per channel; we leave headroom.
MIN_RENAME_INTERVAL = 280  # ~4.7 minutes


# Best-effort country -> flag emoji map. Falls back to a plain flag if missing.
# NOTE: these are used everywhere EXCEPT voice/text channel renames, since
# flag emoji don't render reliably in channel names on desktop.
FLAGS = {
    "argentina": "🇦🇷", "australia": "🇦🇺", "austria": "🇦🇹", "algeria": "🇩🇿",
    "belgium": "🇧🇪", "brazil": "🇧🇷", "canada": "🇨🇦", "cape verde": "🇨🇻",
    "colombia": "🇨🇴", "croatia": "🇭🇷", "curacao": "🇨🇼", "curaçao": "🇨🇼",
    "egypt": "🇪🇬", "ecuador": "🇪🇨", "england": "🏴", "france": "🇫🇷",
    "germany": "🇩🇪", "ghana": "🇬🇭", "haiti": "🇭🇹", "iran": "🇮🇷",
    "ivory coast": "🇨🇮", "côte d'ivoire": "🇨🇮", "japan": "🇯🇵", "jordan": "🇯🇴",
    "mexico": "🇲🇽", "morocco": "🇲🇦", "netherlands": "🇳🇱", "new zealand": "🇳🇿",
    "norway": "🇳🇴", "panama": "🇵🇦", "paraguay": "🇵🇾", "portugal": "🇵🇹",
    "qatar": "🇶🇦", "saudi arabia": "🇸🇦", "scotland": "🏴", "senegal": "🇸🇳",
    "south africa": "🇿🇦", "south korea": "🇰🇷", "korea republic": "🇰🇷",
    "spain": "🇪🇸", "switzerland": "🇨🇭", "tunisia": "🇹🇳", "united states": "🇺🇸",
    "usa": "🇺🇸", "uruguay": "🇺🇾", "uzbekistan": "🇺🇿", "italy": "🇮🇹",
    "wales": "🏴", "poland": "🇵🇱", "denmark": "🇩🇰", "sweden": "🇸🇪",
    "serbia": "🇷🇸", "turkey": "🇹🇷", "türkiye": "🇹🇷", "nigeria": "🇳🇬",
    "cameroon": "🇨🇲", "jamaica": "🇯🇲", "costa rica": "🇨🇷", "honduras": "🇭🇳",
}


def flag_for(name: str) -> str:
    return FLAGS.get((name or "").lower().strip(), "🏳️")


def team_name(team: dict) -> str:
    """Some knockout-stage fixtures don't have their teams determined yet,
    in which case the API legitimately returns null. Show 'TBD' instead of
    the literal word 'None'."""
    if not team:
        return "TBD"
    return team.get("name") or "TBD"


def short_status(status: str) -> str:
    return {
        "SCHEDULED": "⏳ Upcoming",
        "TIMED": "⏳ Upcoming",
        "IN_PLAY": "🔴 Live",
        "PAUSED": "⏸️ Half-time",
        "FINISHED": "✅ Finished",
        "POSTPONED": "⏸️ Postponed",
        "SUSPENDED": "⏸️ Suspended",
        "CANCELLED": "❌ Cancelled",
    }.get(status, status)


def approx_minute(match: dict) -> str:
    """Best-effort live minute. The free API tier doesn't always expose a
    live clock directly, so we approximate from kickoff time when needed."""
    status = match.get("status")
    if status == "PAUSED":
        return "HT"
    if status == "FINISHED":
        return "FT"
    if status != "IN_PLAY":
        return ""
    minute = match.get("minute")
    if minute:
        return f"{minute}'"
    try:
        kickoff = datetime.fromisoformat(match["utcDate"].replace("Z", "+00:00"))
        elapsed = (datetime.now(timezone.utc) - kickoff).total_seconds() / 60
        if elapsed <= 45:
            return f"~{int(elapsed)}'"
        elif elapsed <= 60:
            return "~HT"
        else:
            second_half = elapsed - 60
            return f"~{min(45 + int(second_half), 90)}'+"
    except Exception:
        return ""


def match_line(match: dict) -> str:
    home = team_name(match["homeTeam"])
    away = team_name(match["awayTeam"])
    hs = match["score"]["fullTime"]["home"]
    as_ = match["score"]["fullTime"]["away"]
    if hs is None:
        hs = 0
    if as_ is None:
        as_ = 0
    clock = approx_minute(match)
    clock_part = f" `{clock}`" if clock else ""
    return f"{flag_for(home)} **{home}** {hs}-{as_} **{away}** {flag_for(away)}{clock_part}"


class WorldCup(commands.Cog):
    """World Cup 2026 live scores, standings, alerts, channels & betting."""

    __version__ = "1.1.0"

    def __init__(self, bot: Red):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self.config = Config.get_conf(self, identifier=20260611, force_registration=True)

        default_guild = {
            "goal_channel": None,
            "thread_channel": None,
            "channel_groups": [],  # list of {status, score, clock, last_rename}
            "daily_limit": 15000,
            "payout_multiplier": 1.9,
            "active_threads": {},  # match_id (str) -> thread_id
            "bets": {},  # "YYYY-MM-DD" -> {user_id (str): {"total": int, "bets": [...]}}
        }
        default_global = {
            "last_status": {},  # match_id (str) -> last seen status, plus goal_score_<id> keys
        }
        self.config.register_guild(**default_guild)
        self.config.register_global(**default_global)

        # in-memory cache of today's matches, shared across guilds
        self._cache_matches = []
        self._cache_time = None
        self._cache_lock = asyncio.Lock()

        self.poll_loop.start()

    def cog_unload(self):
        self.poll_loop.cancel()
        if self.session and not self.session.closed:
            self.bot.loop.create_task(self.session.close())

    # ---------------------------------------------------------------- #
    # API helpers
    # ---------------------------------------------------------------- #

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def _api_get(self, path: str, params: Optional[dict] = None):
        tokens = await self.bot.get_shared_api_tokens("footballdata")
        key = tokens.get("api_key")
        if not key:
            return None, "no_key"
        session = await self._get_session()
        headers = {"X-Auth-Token": key}
        url = f"{API_BASE}{path}"
        try:
            async with session.get(url, headers=headers, params=params, timeout=15) as resp:
                if resp.status == 429:
                    return None, "rate_limited"
                if resp.status == 403:
                    return None, "forbidden"
                if resp.status != 200:
                    return None, f"http_{resp.status}"
                return await resp.json(), None
        except asyncio.TimeoutError:
            return None, "timeout"
        except aiohttp.ClientError as e:
            log.warning("football-data.org request failed: %s", e)
            return None, "client_error"

    async def _get_today_matches(self, force: bool = False) -> list:
        """Cached fetch of today's + tomorrow's (UTC) matches for the World Cup.
        Pass force=True to always hit the API fresh (used by `.wc live` so a
        match that just kicked off can never be missed by a stale cache)."""
        async with self._cache_lock:
            now = datetime.now(timezone.utc)
            if (
                not force
                and self._cache_time
                and (now - self._cache_time).total_seconds() < 50
            ):
                return self._cache_matches

            date_from = now.strftime("%Y-%m-%d")
            date_to = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            data, err = await self._api_get(
                f"/competitions/{COMPETITION}/matches",
                params={"dateFrom": date_from, "dateTo": date_to},
            )
            if err:
                log.warning("Could not refresh World Cup matches: %s", err)
                return self._cache_matches  # serve stale cache rather than nothing

            matches = data.get("matches", [])
            self._cache_matches = matches
            self._cache_time = now
            return matches

    async def _get_standings(self):
        data, err = await self._api_get(f"/competitions/{COMPETITION}/standings")
        if err:
            return None, err
        return data, None

    # ---------------------------------------------------------------- #
    # Background loop: goal alerts, match threads, bet settlement,
    # and live channel renaming.
    # ---------------------------------------------------------------- #

    @tasks.loop(seconds=120)
    async def poll_loop(self):
        try:
            matches = await self._get_today_matches(force=True)
            if not matches:
                return

            last_status = await self.config.last_status()
            changed_status = False

            live_matches = []
            for match in matches:
                mid = str(match["id"])
                status = match["status"]
                if status in ("IN_PLAY", "PAUSED"):
                    live_matches.append(match)

                prev = last_status.get(mid)
                if prev != status:
                    last_status[mid] = status
                    changed_status = True
                    await self._handle_status_change(match, prev, status)
                elif status in ("IN_PLAY", "PAUSED"):
                    # Only re-check for goals while the match is actually being played.
                    # Finished matches sometimes get tiny score corrections from the API
                    # after full-time; we don't want those treated as new goals.
                    await self._check_goal(match)

            if changed_status:
                await self.config.last_status.set(last_status)

            await self._update_channel_groups(live_matches)

        except Exception:
            log.exception("Error in World Cup poll loop")

    async def _check_goal(self, match: dict):
        """Compares current score to last known score (stored in global config)
        to detect goals and fire alerts. Note: the free API tier does not expose
        the scorer's name, so alerts show the score change only."""
        mid = str(match["id"])
        hs = match["score"]["fullTime"]["home"] or 0
        as_ = match["score"]["fullTime"]["away"] or 0
        key = f"goal_score_{mid}"
        all_scores = await self.config.last_status()
        stored = all_scores.get(key)
        current = f"{hs}-{as_}"
        if stored == current:
            return
        all_scores[key] = current
        await self.config.last_status.set(all_scores)
        if stored is None:
            return  # first time seeing this match, don't alert retroactively
        await self._send_goal_alert(match, stored, current)

    async def _send_goal_alert(self, match: dict, old_score: str, new_score: str):
        home = team_name(match["homeTeam"])
        away = team_name(match["awayTeam"])
        embed = discord.Embed(
            title="⚽ GOAL!",
            description=f"{flag_for(home)} **{home}** {new_score.replace('-', ' - ')} **{away}** {flag_for(away)}",
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"{approx_minute(match)}  •  Previous score: {old_score}")
        for guild in self.bot.guilds:
            channel_id = await self.config.guild(guild).goal_channel()
            if not channel_id:
                continue
            channel = guild.get_channel(channel_id)
            if channel:
                try:
                    await channel.send(embed=embed)
                except discord.HTTPException:
                    pass

    async def _handle_status_change(self, match: dict, prev: Optional[str], new: str):
        await self._check_goal(match)  # catch any score change tied to this transition

        if new == "IN_PLAY" and prev != "PAUSED":
            await self._create_match_thread(match)
        if new == "FINISHED":
            await self._archive_match_thread(match)
            await self._settle_bets(match)

    async def _create_match_thread(self, match: dict):
        home = team_name(match["homeTeam"])
        away = team_name(match["awayTeam"])
        mid = str(match["id"])
        title = f"{flag_for(home)} {home} vs {away} {flag_for(away)}"[:95]
        for guild in self.bot.guilds:
            channel_id = await self.config.guild(guild).thread_channel()
            if not channel_id:
                continue
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue
            active = await self.config.guild(guild).active_threads()
            if mid in active:
                continue
            try:
                thread = await channel.create_thread(
                    name=title, type=discord.ChannelType.public_thread
                )
                await thread.send(
                    embed=discord.Embed(
                        title="🟢 Kick-off!",
                        description=match_line(match),
                        color=discord.Color.green(),
                    )
                )
                active[mid] = thread.id
                await self.config.guild(guild).active_threads.set(active)
            except discord.HTTPException:
                pass

    async def _archive_match_thread(self, match: dict):
        mid = str(match["id"])
        for guild in self.bot.guilds:
            active = await self.config.guild(guild).active_threads()
            thread_id = active.get(mid)
            if not thread_id:
                continue
            thread = guild.get_thread(thread_id)
            if thread:
                try:
                    await thread.send(
                        embed=discord.Embed(
                            title="🏁 Full-time",
                            description=match_line(match),
                            color=discord.Color.red(),
                        )
                    )
                    await thread.edit(archived=True, locked=False)
                except discord.HTTPException:
                    pass
            active.pop(mid, None)
            await self.config.guild(guild).active_threads.set(active)

    async def _update_channel_groups(self, live_matches: list):
        live_matches = sorted(live_matches, key=lambda m: m["utcDate"])
        now = datetime.now(timezone.utc).timestamp()

        for guild in self.bot.guilds:
            groups = await self.config.guild(guild).channel_groups()
            if not groups:
                continue
            changed = False
            for i, group in enumerate(groups):
                last_rename = group.get("last_rename", 0)
                if now - last_rename < MIN_RENAME_INTERVAL:
                    continue
                match = live_matches[i] if i < len(live_matches) else None
                ok = await self._rename_group(guild, group, match)
                if ok:
                    group["last_rename"] = now
                    changed = True
            if changed:
                await self.config.guild(guild).channel_groups.set(groups)

    async def _rename_group(self, guild: discord.Guild, group: dict, match: Optional[dict]) -> bool:
        status_ch = guild.get_channel(group.get("status")) if group.get("status") else None
        score_ch = guild.get_channel(group.get("score")) if group.get("score") else None
        clock_ch = guild.get_channel(group.get("clock")) if group.get("clock") else None

        if match is None:
            names = {
                status_ch: "🏆 No live match",
                score_ch: "Waiting for kickoff",
                clock_ch: "⏱️ —",
            }
        else:
            home = team_name(match["homeTeam"])
            away = team_name(match["awayTeam"])
            hs = match["score"]["fullTime"]["home"] or 0
            as_ = match["score"]["fullTime"]["away"] or 0
            names = {
                status_ch: "🏆 LIVE",
                score_ch: f"{home} {hs}-{as_} {away}"[:95],
                clock_ch: approx_minute(match) or "🔴",
            }

        any_success = False
        for channel, name in names.items():
            if channel is None:
                continue
            try:
                await channel.edit(name=name)
                any_success = True
            except discord.HTTPException as e:
                log.warning("Could not rename channel %s: %s", channel.id, e)
        return any_success

    # ---------------------------------------------------------------- #
    # Betting
    # ---------------------------------------------------------------- #

    async def _settle_bets(self, match: dict):
        mid = str(match["id"])
        hs = match["score"]["fullTime"]["home"]
        as_ = match["score"]["fullTime"]["away"]
        if hs is None or as_ is None:
            return
        if hs > as_:
            result = "home"
        elif as_ > hs:
            result = "away"
        else:
            result = "draw"

        for guild in self.bot.guilds:
            async with self.config.guild(guild).bets() as all_bets:
                for date_key, day_bets in all_bets.items():
                    for user_id, info in day_bets.items():
                        for bet in info.get("bets", []):
                            if bet.get("match_id") != mid or bet.get("settled"):
                                continue
                            bet["settled"] = True
                            bet["result"] = result
                            if bet["side"] == result:
                                member = guild.get_member(int(user_id))
                                if member:
                                    multiplier = await self.config.guild(guild).payout_multiplier()
                                    payout = int(bet["amount"] * multiplier)
                                    try:
                                        await bank.deposit_credits(member, payout)
                                    except Exception:
                                        log.exception("Failed to pay out bet for %s", user_id)

    def _resolve_side(self, match: dict, team_query: str) -> Optional[str]:
        team_query = team_query.lower().strip()
        if team_query in ("draw", "tie", "x"):
            return "draw"
        home = team_name(match["homeTeam"]).lower()
        away = team_name(match["awayTeam"]).lower()
        if team_query in home or home in team_query:
            return "home"
        if team_query in away or away in team_query:
            return "away"
        return None

    async def _find_bettable_match(self, team_query: str):
        """Finds today's/tomorrow's match for a team that hasn't kicked off yet."""
        matches = await self._get_today_matches()
        for match in matches:
            if match["status"] not in ("SCHEDULED", "TIMED"):
                continue
            side = self._resolve_side(match, team_query)
            if side:
                return match, side
        return None, None

    # ---------------------------------------------------------------- #
    # Commands
    # ---------------------------------------------------------------- #

    @commands.group(name="wc", invoke_without_command=True)
    async def wc(self, ctx: commands.Context):
        """World Cup 2026 commands. Use `[p]help wc` to see all of them."""
        await ctx.send_help()

    @wc.command(name="live")
    async def wc_live(self, ctx: commands.Context):
        """Shows all currently live World Cup matches."""
        # Always fetch fresh here (not the soft cache) so a match that just
        # kicked off seconds ago is never missed.
        matches = await self._get_today_matches(force=True)
        live = [m for m in matches if m["status"] in ("IN_PLAY", "PAUSED")]
        if not live:
            await ctx.send("⚽ No matches are live right now. Try `[p]wc upcoming`.")
            return
        embed = discord.Embed(title="🔴 Live World Cup Matches", color=discord.Color.red())
        for m in live:
            group_name = m.get("group") or "Knockout"
            embed.add_field(name=group_name.replace("GROUP_", "Group "), value=match_line(m), inline=False)
        await ctx.send(embed=embed)

    @wc.command(name="upcoming")
    async def wc_upcoming(self, ctx: commands.Context):
        """Shows today's upcoming (not yet started) World Cup matches."""
        matches = await self._get_today_matches(force=True)
        now = datetime.now(timezone.utc)
        window_end = now + timedelta(hours=36)
        upcoming = [
            m for m in matches
            if m["status"] in ("SCHEDULED", "TIMED")
            and now <= datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00")) <= window_end
        ]
        if not upcoming:
            await ctx.send("📅 No more matches scheduled for today.")
            return
        upcoming.sort(key=lambda m: m["utcDate"])
        embed = discord.Embed(title="📅 Today's Upcoming Matches", color=discord.Color.blue())
        for m in upcoming:
            kickoff = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00"))
            ts = int(kickoff.timestamp())
            home = team_name(m["homeTeam"])
            away = team_name(m["awayTeam"])
            grp = (m.get("group") or "Knockout").replace("GROUP_", "Group ")
            embed.add_field(
                name=grp,
                value=f"{flag_for(home)} **{home}** vs **{away}** {flag_for(away)} — <t:{ts}:t> (<t:{ts}:R>)",
                inline=False,
            )
        await ctx.send(embed=embed)

    @wc.command(name="group")
    async def wc_group(self, ctx: commands.Context, letter: str):
        """Shows the standings table for a group, e.g. `[p]wc group A`."""
        letter = letter.strip().upper()
        if len(letter) != 1 or not letter.isalpha():
            await ctx.send("Please give a single group letter, e.g. `[p]wc group A`.")
            return
        data, err = await self._get_standings()
        if err == "no_key":
            await ctx.send("⚠️ No API key is set up yet. Ask an admin to run `[p]wc admin setup`.")
            return
        if err:
            await ctx.send(f"⚠️ Couldn't reach football-data.org right now ({err}). Try again shortly.")
            return

        standings_list = data.get("standings", [])
        target = None
        possible_names = {f"group_{letter}".upper(), letter, f"group {letter}".upper()}
        for standing in standings_list:
            grp = standing.get("group")
            if grp and str(grp).upper().replace(" ", "_") in possible_names:
                target = standing
                break
        if not target:
            if not standings_list:
                await ctx.send(
                    f"The API returned **zero** standings entries for the World Cup right now "
                    f"(competition: {data.get('competition', {}).get('name', 'unknown')}).\n"
                    "This usually means either the standings endpoint isn't populated yet for this "
                    "competition/season on your plan, or it needs a `season` parameter. "
                    "An admin can run `[p]wc admin debugstandings` to see the raw response."
                )
            else:
                seen = ", ".join(sorted({str(s.get("group")) for s in standings_list})) or "none"
                await ctx.send(
                    f"No standings found for Group {letter}. The API did return standings, but none "
                    f"labeled like `GROUP_{letter}`. Groups it actually returned: {seen}\n"
                    "An admin can run `[p]wc admin debugstandings` to see the full raw response."
                )
            return

        lines = [f"{'Pos':<4}{'Team':<20}{'P':>3}{'W':>3}{'D':>3}{'L':>3}{'GF':>4}{'GA':>4}{'GD':>4}{'Pts':>5}"]
        for row in target["table"]:
            name = row["team"]["name"]
            lines.append(
                f"{row['position']:<4}{name[:19]:<20}{row['playedGames']:>3}{row['won']:>3}"
                f"{row['draw']:>3}{row['lost']:>3}{row['goalsFor']:>4}{row['goalsAgainst']:>4}"
                f"{row['goalDifference']:>4}{row['points']:>5}"
            )
        table_text = "\n".join(lines)
        embed = discord.Embed(title=f"🏆 Group {letter} Standings", color=discord.Color.green())
        for page in pagify(table_text, page_length=1000):
            embed.add_field(name="\u200b", value=box(page), inline=False)
        await ctx.send(embed=embed)

    @wc.command(name="bet")
    async def wc_bet(self, ctx: commands.Context, team: str, amount: int):
        """Bet currency on a team to win a match before kickoff.

        Example: `[p]wc bet spain 1200`
        You can also bet on a draw with `[p]wc bet draw 1200`.
        Maximum 15,000 (configurable by admins) in total bets per day.
        """
        if amount <= 0:
            await ctx.send("Bet amount must be a positive number.")
            return

        match, side = await self._find_bettable_match(team)
        if not match:
            await ctx.send(
                f"Couldn't find an upcoming (not-yet-started) match today for **{team}**. "
                "You can only bet before kickoff — check `[p]wc upcoming`."
            )
            return

        currency = await bank.get_currency_name(ctx.guild)
        if not await bank.can_spend(ctx.author, amount):
            balance = await bank.get_balance(ctx.author)
            await ctx.send(
                f"You don't have enough {currency}. Your balance is {humanize_number(balance)}."
            )
            return

        today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_limit = await self.config.guild(ctx.guild).daily_limit()
        async with self.config.guild(ctx.guild).bets() as all_bets:
            day_bets = all_bets.setdefault(today_key, {})
            user_info = day_bets.setdefault(str(ctx.author.id), {"total": 0, "bets": []})
            if user_info["total"] + amount > daily_limit:
                remaining = daily_limit - user_info["total"]
                await ctx.send(
                    f"That would put you over today's betting limit of {humanize_number(daily_limit)} "
                    f"{currency}. You have {humanize_number(max(remaining, 0))} left today."
                )
                return

            await bank.withdraw_credits(ctx.author, amount)
            user_info["total"] += amount
            user_info["bets"].append(
                {
                    "match_id": str(match["id"]),
                    "side": side,
                    "amount": amount,
                    "settled": False,
                }
            )

        home = team_name(match["homeTeam"])
        away = team_name(match["awayTeam"])
        side_name = {"home": home, "away": away, "draw": "a draw"}[side]
        multiplier = await self.config.guild(ctx.guild).payout_multiplier()
        await ctx.send(
            f"✅ Bet placed: **{humanize_number(amount)} {currency}** on **{side_name}** "
            f"in {flag_for(home)} {home} vs {away} {flag_for(away)}.\n"
            f"Win payout would be **{humanize_number(int(amount * multiplier))} {currency}**."
        )

    @wc.command(name="mybets")
    async def wc_mybets(self, ctx: commands.Context):
        """Shows your bets for today and your remaining daily betting allowance."""
        today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        all_bets = await self.config.guild(ctx.guild).bets()
        daily_limit = await self.config.guild(ctx.guild).daily_limit()
        currency = await bank.get_currency_name(ctx.guild)
        user_info = all_bets.get(today_key, {}).get(str(ctx.author.id))

        if not user_info or not user_info["bets"]:
            await ctx.send(f"You haven't placed any bets today. Daily limit: {humanize_number(daily_limit)} {currency}.")
            return

        lines = []
        for bet in user_info["bets"]:
            if bet.get("settled"):
                state = "✅ won" if bet.get("result") == bet["side"] else "❌ lost"
            else:
                state = "⏳ pending"
            lines.append(f"{bet['side'].title()} — {humanize_number(bet['amount'])} {currency} ({state})")

        remaining = max(daily_limit - user_info["total"], 0)
        embed = discord.Embed(title="📋 Your bets today", color=discord.Color.purple())
        embed.description = "\n".join(lines)
        embed.set_footer(text=f"Remaining today: {humanize_number(remaining)}/{humanize_number(daily_limit)} {currency}")
        await ctx.send(embed=embed)

    # ---------------------------------------------------------------- #
    # Admin setup commands
    # ---------------------------------------------------------------- #

    @wc.group(name="admin")
    @commands.admin_or_permissions(manage_guild=True)
    async def wc_admin(self, ctx: commands.Context):
        """Admin configuration for the World Cup cog."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    @wc_admin.command(name="setup")
    async def wc_admin_setup(self, ctx: commands.Context):
        """Quick checklist for setting up this cog."""
        tokens = await self.bot.get_shared_api_tokens("footballdata")
        has_key = bool(tokens.get("api_key"))
        embed = discord.Embed(title="World Cup 2026 — Setup Checklist", color=discord.Color.orange())
        embed.add_field(
            name="1. API key",
            value=(
                "✅ Set" if has_key else
                "❌ Not set. Run `[p]set api footballdata api_key,YOUR_KEY` in DMs with the bot.\n"
                "Get a free key at https://www.football-data.org/client/register"
            ),
            inline=False,
        )
        embed.add_field(
            name="2. Goal alert channel",
            value="`[p]wc admin goalchannel #channel`",
            inline=False,
        )
        embed.add_field(
            name="3. Match thread channel",
            value="`[p]wc admin threadchannel #channel` (must be a text channel)",
            inline=False,
        )
        embed.add_field(
            name="4. Live score channels",
            value=(
                "`[p]wc admin addgroup <status_channel> <score_channel> <clock_channel>`\n"
                "Voice channels work best (text channel names get lowercased/hyphenated by Discord)."
            ),
            inline=False,
        )
        embed.add_field(
            name="5. Betting settings (optional)",
            value="`[p]wc admin dailylimit <amount>` (default 15000) and `[p]wc admin payout <multiplier>` (default 1.9)",
            inline=False,
        )
        await ctx.send(embed=embed)

    @wc_admin.command(name="goalchannel")
    async def wc_admin_goalchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Sets the channel where goal alerts are posted."""
        await self.config.guild(ctx.guild).goal_channel.set(channel.id)
        await ctx.send(f"⚽ Goal alerts will be posted in {channel.mention}.")

    @wc_admin.command(name="threadchannel")
    async def wc_admin_threadchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Sets the text channel where automatic match threads are created."""
        await self.config.guild(ctx.guild).thread_channel.set(channel.id)
        await ctx.send(f"🧵 Match threads will be created in {channel.mention}.")

    @wc_admin.command(name="addgroup")
    async def wc_admin_addgroup(
        self,
        ctx: commands.Context,
        status_channel: discord.abc.GuildChannel,
        score_channel: discord.abc.GuildChannel,
        clock_channel: discord.abc.GuildChannel,
    ):
        """Registers 3 channels (status, score, clock) to display one live match.

        Run this multiple times to track multiple simultaneous matches —
        each call adds another group. Voice channels look best for this.
        """
        groups = await self.config.guild(ctx.guild).channel_groups()
        groups.append(
            {
                "status": status_channel.id,
                "score": score_channel.id,
                "clock": clock_channel.id,
                "last_rename": 0,
            }
        )
        await self.config.guild(ctx.guild).channel_groups.set(groups)
        await ctx.send(
            f"✅ Added live-score group #{len(groups)}: {status_channel.mention}, "
            f"{score_channel.mention}, {clock_channel.mention}.\n"
            "It will update on the next poll cycle (within ~2 minutes), respecting Discord's rename rate limit."
        )

    @wc_admin.command(name="cleargroups")
    async def wc_admin_cleargroups(self, ctx: commands.Context):
        """Removes all configured live-score channel groups."""
        await self.config.guild(ctx.guild).channel_groups.set([])
        await ctx.send("🗑️ Cleared all live-score channel groups.")

    @wc_admin.command(name="dailylimit")
    async def wc_admin_dailylimit(self, ctx: commands.Context, amount: int):
        """Sets the maximum amount a user can bet per day."""
        if amount <= 0:
            await ctx.send("Amount must be positive.")
            return
        await self.config.guild(ctx.guild).daily_limit.set(amount)
        await ctx.send(f"💰 Daily betting limit set to {humanize_number(amount)}.")

    @wc_admin.command(name="payout")
    async def wc_admin_payout(self, ctx: commands.Context, multiplier: float):
        """Sets the win payout multiplier (e.g. 1.9 means a winning bet returns 1.9x stake)."""
        if multiplier <= 1:
            await ctx.send("Multiplier should be greater than 1.")
            return
        await self.config.guild(ctx.guild).payout_multiplier.set(multiplier)
        await ctx.send(f"💸 Payout multiplier set to {multiplier}x.")

    @wc_admin.command(name="debugstandings")
    async def wc_admin_debugstandings(self, ctx: commands.Context):
        """Shows the raw standings API response structure for troubleshooting."""
        data, err = await self._get_standings()
        if err:
            await ctx.send(f"API call failed with error: `{err}`")
            return
        comp = data.get("competition", {})
        season = data.get("season", {})
        standings = data.get("standings", [])
        summary = [
            f"Competition: {comp.get('name')} ({comp.get('code')}), type: {comp.get('type')}",
            f"Season: {season.get('startDate')} to {season.get('endDate')}, currentMatchday: {season.get('currentMatchday')}",
            f"Number of standings entries: {len(standings)}",
        ]
        for s in standings[:15]:
            summary.append(
                f"  stage={s.get('stage')} type={s.get('type')} group={s.get('group')!r} "
                f"teams_in_table={len(s.get('table', []))}"
            )
        text = "\n".join(summary)
        for page in pagify(text, page_length=1900):
            await ctx.send(box(page))

    @wc_admin.command(name="settings")
    async def wc_admin_settings(self, ctx: commands.Context):
        """Shows the current configuration for this server."""
        conf = await self.config.guild(ctx.guild).all()
        goal_ch = ctx.guild.get_channel(conf["goal_channel"]) if conf["goal_channel"] else None
        thread_ch = ctx.guild.get_channel(conf["thread_channel"]) if conf["thread_channel"] else None
        embed = discord.Embed(title="World Cup 2026 — Current Settings", color=discord.Color.teal())
        embed.add_field(name="Goal channel", value=goal_ch.mention if goal_ch else "Not set", inline=False)
        embed.add_field(name="Thread channel", value=thread_ch.mention if thread_ch else "Not set", inline=False)
        embed.add_field(name="Daily bet limit", value=humanize_number(conf["daily_limit"]), inline=True)
        embed.add_field(name="Payout multiplier", value=f"{conf['payout_multiplier']}x", inline=True)
        embed.add_field(name="Live-score groups", value=str(len(conf["channel_groups"])), inline=True)
        await ctx.send(embed=embed)
