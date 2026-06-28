"""
World Cup 2026 cog for Red-DiscordBot.

Features:
- .wc live          -> shows live matches with real-time score + minute
- .wc upcoming       -> shows today's upcoming matches
- .wc group <X>      -> shows the standings table for group X (A-L)
- .wc bet <team> <amount> -> bet server currency on a team to win (pre-kickoff only)
- .wc mybets         -> shows your bets today and remaining daily allowance
- .wc admin setup    -> guided admin setup
- admin subcommands to configure goal alert channel, match thread channel,
  and live-score channel groups (voice/text channels that get renamed)

Data source: API-Football / API-Sports (api-football.com), league id 1 (World Cup),
season 2026. Store the API key with:
    [p]set api apifootball api_key,YOUR_KEY
(do this in DMs with the bot, never in a public channel)

QUOTA NOTE: the free plan only allows 100 requests/day. This cog is built around
that budget:
- The day's full fixture schedule is fetched ONCE per day (1 call).
- Live scores are only polled while a match is actually inside its kickoff
  window (kickoff time to roughly kickoff + 130 minutes), not all day.
- Goal-scorer details are only fetched the moment a goal is actually detected,
  not on every poll.
- A running quota counter (read from the API's own rate-limit headers) will
  pause non-essential polling if the daily budget is nearly exhausted.
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

API_BASE = "https://v3.football.api-sports.io"
WC_LEAGUE_ID = 1
SEASON = 2026

# Minimum seconds between renames of the *same* channel.
# Discord allows 2 renames per 10 minutes per channel; we leave headroom.
MIN_RENAME_INTERVAL = 280  # ~4.7 minutes

# How often to poll live scores while at least one match is in its window.
LIVE_POLL_INTERVAL = 120  # seconds (background loop tick)

# Statuses (API-Football "status.short" codes)
LIVE_STATUSES = {"1H", "2H", "HT", "ET", "BT", "P", "SUSP", "INT"}
FINISHED_STATUSES = {"FT", "AET", "PEN", "AWD", "WO"}
NOT_STARTED_STATUSES = {"NS", "TBD"}

# Best-effort country -> flag emoji map. Falls back to a plain flag if missing.
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


def clock_for(fixture: dict) -> str:
    status = fixture["fixture"]["status"]["short"]
    elapsed = fixture["fixture"]["status"].get("elapsed")
    if status == "HT":
        return "HT"
    if status == "BT":
        return "Break"
    if status in FINISHED_STATUSES:
        return status  # FT / AET / PEN
    if status in LIVE_STATUSES and elapsed is not None:
        return f"{elapsed}'"
    return ""


def match_line(fixture: dict) -> str:
    home = fixture["teams"]["home"]["name"]
    away = fixture["teams"]["away"]["name"]
    hs = fixture["goals"]["home"] or 0
    as_ = fixture["goals"]["away"] or 0
    clock = clock_for(fixture)
    clock_part = f" `{clock}`" if clock else ""
    return f"{flag_for(home)} **{home}** {hs}-{as_} **{away}** {flag_for(away)}{clock_part}"


class WorldCup(commands.Cog):
    """World Cup 2026 live scores, standings, alerts, channels & betting."""

    __version__ = "2.0.0"

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
            "active_threads": {},  # fixture_id (str) -> thread_id
            "bets": {},  # "YYYY-MM-DD" -> {user_id (str): {"total": int, "bets": [...]}}
        }
        default_global = {
            "last_status": {},  # fixture_id (str) -> last seen status code
        }
        self.config.register_guild(**default_guild)
        self.config.register_global(**default_global)

        # In-memory state, shared across guilds. None of this needs to persist
        # across bot restarts -- it's just an API-call budget saver.
        self._schedule_cache: list = []       # today's full fixture list
        self._schedule_date: Optional[str] = None
        self._live_cache: list = []           # currently-known live fixtures
        self._live_cache_time: Optional[datetime] = None
        self._standings_cache: Optional[dict] = None
        self._standings_cache_time: Optional[datetime] = None
        self._cache_lock = asyncio.Lock()
        self._quota = {"remaining": None, "limit": None}

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
        tokens = await self.bot.get_shared_api_tokens("apifootball")
        key = tokens.get("api_key")
        if not key:
            return None, "no_key"

        if self._quota["remaining"] is not None and self._quota["remaining"] <= 2:
            log.warning("API-Football daily quota nearly exhausted, skipping call to %s", path)
            return None, "quota_exhausted"

        session = await self._get_session()
        headers = {"x-apisports-key": "ba4f29a33a238fea43c1c1badecb0533"}
        url = f"{API_BASE}{path}"
        try:
            async with session.get(url, headers=headers, params=params, timeout=15) as resp:
                remaining = resp.headers.get("x-ratelimit-requests-remaining")
                limit = resp.headers.get("x-ratelimit-requests-limit")
                if remaining is not None:
                    self._quota["remaining"] = int(remaining)
                if limit is not None:
                    self._quota["limit"] = int(limit)

                if resp.status == 429:
                    return None, "rate_limited"
                if resp.status == 403:
                    return None, "forbidden"
                if resp.status != 200:
                    return None, f"http_{resp.status}"
                data = await resp.json()
                if data.get("errors"):
                    log.warning("API-Football returned errors for %s: %s", path, data["errors"])
                    return None, "api_error"
                return data, None
        except asyncio.TimeoutError:
            return None, "timeout"
        except aiohttp.ClientError as e:
            log.warning("api-football.com request failed: %s", e)
            return None, "client_error"

    # ---------------------------------------------------------------- #
    # Cached data fetchers
    # ---------------------------------------------------------------- #

    async def _ensure_schedule(self, force: bool = False) -> list:
        """Fetches today's full WC fixture list. Costs exactly 1 API call per
        UTC day unless force=True."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        async with self._cache_lock:
            if not force and self._schedule_date == today and self._schedule_cache:
                return self._schedule_cache
            data, err = await self._api_get(
                "/fixtures", params={"league": WC_LEAGUE_ID, "season": SEASON, "date": today}
            )
            if err:
                log.warning("Could not refresh World Cup schedule: %s", err)
                return self._schedule_cache  # serve stale cache rather than nothing
            self._schedule_cache = data.get("response", [])
            self._schedule_date = today
            return self._schedule_cache

    def _matches_in_window_now(self) -> list:
        """Returns today's scheduled fixtures whose kickoff window overlaps now."""
        now = datetime.now(timezone.utc)
        in_window = []
        for fx in self._schedule_cache:
            try:
                kickoff = datetime.fromisoformat(fx["fixture"]["date"])
            except Exception:
                continue
            window_end = kickoff + timedelta(minutes=130)
            window_start = kickoff - timedelta(minutes=5)
            if window_start <= now <= window_end:
                in_window.append(fx)
        return in_window

    async def _refresh_live(self, force: bool = False) -> list:
        """Refreshes the live-fixtures cache. Skips the API call entirely if
        nothing is scheduled to be live right now, to save quota."""
        async with self._cache_lock:
            now = datetime.now(timezone.utc)
            if (
                not force
                and self._live_cache_time
                and (now - self._live_cache_time).total_seconds() < 50
            ):
                return self._live_cache

            if not force and not self._matches_in_window_now():
                self._live_cache = []
                self._live_cache_time = now
                return self._live_cache

            data, err = await self._api_get("/fixtures", params={"live": "all"})
            if err:
                log.warning("Could not refresh live fixtures: %s", err)
                return self._live_cache
            all_live = data.get("response", [])
            self._live_cache = [fx for fx in all_live if fx["league"]["id"] == WC_LEAGUE_ID]
            self._live_cache_time = now
            return self._live_cache

    async def _get_standings(self, force: bool = False):
        now = datetime.now(timezone.utc)
        if (
            not force
            and self._standings_cache
            and self._standings_cache_time
            and (now - self._standings_cache_time).total_seconds() < 1800
        ):
            return self._standings_cache, None
        data, err = await self._api_get(
            "/standings", params={"league": WC_LEAGUE_ID, "season": SEASON}
        )
        if err:
            return None, err
        self._standings_cache = data
        self._standings_cache_time = now
        return data, None

    async def _get_goal_scorer(self, fixture_id: int) -> Optional[str]:
        """One extra call, only made the moment a goal is detected."""
        data, err = await self._api_get(
            "/fixtures/events", params={"fixture": fixture_id, "type": "Goal"}
        )
        if err or not data:
            return None
        events = data.get("response", [])
        if not events:
            return None
        latest = events[-1]
        player = latest.get("player", {}).get("name")
        assist = (latest.get("assist") or {}).get("name")
        minute = latest.get("time", {}).get("elapsed")
        if not player:
            return None
        text = f"⚽ {player}"
        if assist:
            text += f" (assist: {assist})"
        if minute:
            text += f" — {minute}'"
        return text

    # ---------------------------------------------------------------- #
    # Background loop: goal alerts, match threads, bet settlement,
    # and live channel renaming.
    # ---------------------------------------------------------------- #

    @tasks.loop(seconds=LIVE_POLL_INTERVAL)
    async def poll_loop(self):
        try:
            await self._ensure_schedule()
            live_fixtures = await self._refresh_live()

            last_status = await self.config.last_status()
            changed_status = False
            live_ids = set()

            for fx in live_fixtures:
                fid = str(fx["fixture"]["id"])
                live_ids.add(fid)
                status = fx["fixture"]["status"]["short"]
                prev = last_status.get(fid)
                if prev != status:
                    last_status[fid] = status
                    changed_status = True
                await self._check_goal(fx)
                if prev != status and status == "1H" and prev is None:
                    await self._create_match_thread(fx)

            # Detect matches that disappeared from the live list (i.e. finished)
            for fid, status in list(last_status.items()):
                if status in LIVE_STATUSES and fid not in live_ids:
                    fx = await self._confirm_finished(int(fid))
                    if fx:
                        new_status = fx["fixture"]["status"]["short"]
                        last_status[fid] = new_status
                        changed_status = True
                        if new_status in FINISHED_STATUSES:
                            await self._archive_match_thread(fx)
                            await self._settle_bets(fx)

            if changed_status:
                await self.config.last_status.set(last_status)

            await self._update_channel_groups(live_fixtures)

        except Exception:
            log.exception("Error in World Cup poll loop")

    async def _confirm_finished(self, fixture_id: int) -> Optional[dict]:
        data, err = await self._api_get("/fixtures", params={"id": fixture_id})
        if err or not data:
            return None
        response = data.get("response", [])
        return response[0] if response else None

    async def _check_goal(self, fixture: dict):
        """Compares current score to last known score (stored in global config)
        to detect goals and fire alerts, enriched with the real scorer name."""
        fid = str(fixture["fixture"]["id"])
        hs = fixture["goals"]["home"] or 0
        as_ = fixture["goals"]["away"] or 0
        key = f"goal_score_{fid}"
        all_scores = await self.config.last_status()
        stored = all_scores.get(key)
        current = f"{hs}-{as_}"
        if stored == current:
            return
        all_scores[key] = current
        await self.config.last_status.set(all_scores)
        if stored is None:
            return  # first time seeing this match, don't alert retroactively
        scorer_text = await self._get_goal_scorer(fixture["fixture"]["id"])
        await self._send_goal_alert(fixture, stored, current, scorer_text)

    async def _send_goal_alert(self, fixture: dict, old_score: str, new_score: str, scorer_text: Optional[str]):
        home = fixture["teams"]["home"]["name"]
        away = fixture["teams"]["away"]["name"]
        embed = discord.Embed(
            title="⚽ GOAL!",
            description=f"{flag_for(home)} **{home}** {new_score.replace('-', ' - ')} **{away}** {flag_for(away)}",
            color=discord.Color.gold(),
        )
        if scorer_text:
            embed.add_field(name="Scorer", value=scorer_text, inline=False)
        embed.set_footer(text=f"{clock_for(fixture)}  •  Previous score: {old_score}")
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

    async def _create_match_thread(self, fixture: dict):
        home = fixture["teams"]["home"]["name"]
        away = fixture["teams"]["away"]["name"]
        fid = str(fixture["fixture"]["id"])
        title = f"{flag_for(home)} {home} vs {away} {flag_for(away)}"[:95]
        for guild in self.bot.guilds:
            channel_id = await self.config.guild(guild).thread_channel()
            if not channel_id:
                continue
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue
            active = await self.config.guild(guild).active_threads()
            if fid in active:
                continue
            try:
                thread = await channel.create_thread(
                    name=title, type=discord.ChannelType.public_thread
                )
                await thread.send(
                    embed=discord.Embed(
                        title="🟢 Kick-off!",
                        description=match_line(fixture),
                        color=discord.Color.green(),
                    )
                )
                active[fid] = thread.id
                await self.config.guild(guild).active_threads.set(active)
            except discord.HTTPException:
                pass

    async def _archive_match_thread(self, fixture: dict):
        fid = str(fixture["fixture"]["id"])
        for guild in self.bot.guilds:
            active = await self.config.guild(guild).active_threads()
            thread_id = active.get(fid)
            if not thread_id:
                continue
            thread = guild.get_thread(thread_id)
            if thread:
                try:
                    await thread.send(
                        embed=discord.Embed(
                            title="🏁 Full-time",
                            description=match_line(fixture),
                            color=discord.Color.red(),
                        )
                    )
                    await thread.edit(archived=True, locked=False)
                except discord.HTTPException:
                    pass
            active.pop(fid, None)
            await self.config.guild(guild).active_threads.set(active)

    async def _update_channel_groups(self, live_fixtures: list):
        live_fixtures = sorted(live_fixtures, key=lambda f: f["fixture"]["date"])
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
                fixture = live_fixtures[i] if i < len(live_fixtures) else None
                ok = await self._rename_group(guild, group, fixture)
                if ok:
                    group["last_rename"] = now
                    changed = True
            if changed:
                await self.config.guild(guild).channel_groups.set(groups)

    async def _rename_group(self, guild: discord.Guild, group: dict, fixture: Optional[dict]) -> bool:
        status_ch = guild.get_channel(group.get("status")) if group.get("status") else None
        score_ch = guild.get_channel(group.get("score")) if group.get("score") else None
        clock_ch = guild.get_channel(group.get("clock")) if group.get("clock") else None

        if fixture is None:
            names = {
                status_ch: "🏆 No live match",
                score_ch: "⚽ Waiting for kickoff",
                clock_ch: "⏱️ —",
            }
        else:
            home = fixture["teams"]["home"]["name"]
            away = fixture["teams"]["away"]["name"]
            hs = fixture["goals"]["home"] or 0
            as_ = fixture["goals"]["away"] or 0
            names = {
                status_ch: "🏆 LIVE",
                score_ch: f"{flag_for(home)}{home} {hs}-{as_} {flag_for(away)}{away}"[:95],
                clock_ch: clock_for(fixture) or "🔴",
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

    async def _settle_bets(self, fixture: dict):
        fid = str(fixture["fixture"]["id"])
        hs = fixture["goals"]["home"]
        as_ = fixture["goals"]["away"]
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
                            if bet.get("match_id") != fid or bet.get("settled"):
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

    def _resolve_side(self, fixture: dict, team_query: str) -> Optional[str]:
        team_query = team_query.lower().strip()
        if team_query in ("draw", "tie", "x"):
            return "draw"
        home = fixture["teams"]["home"]["name"].lower()
        away = fixture["teams"]["away"]["name"].lower()
        if team_query in home or home in team_query:
            return "home"
        if team_query in away or away in team_query:
            return "away"
        return None

    async def _find_bettable_match(self, team_query: str):
        """Finds today's match for a team that hasn't kicked off yet."""
        matches = await self._ensure_schedule()
        for fx in matches:
            if fx["fixture"]["status"]["short"] not in NOT_STARTED_STATUSES:
                continue
            side = self._resolve_side(fx, team_query)
            if side:
                return fx, side
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
        await self._ensure_schedule()
        live = await self._refresh_live()
        if not live:
            await ctx.send("⚽ No matches are live right now. Try `[p]wc upcoming`.")
            return
        embed = discord.Embed(title="🔴 Live World Cup Matches", color=discord.Color.red())
        for fx in live:
            round_name = fx["league"].get("round", "World Cup")
            embed.add_field(name=round_name, value=match_line(fx), inline=False)
        await ctx.send(embed=embed)

    @wc.command(name="upcoming")
    async def wc_upcoming(self, ctx: commands.Context):
        """Shows today's upcoming (not yet started) World Cup matches."""
        matches = await self._ensure_schedule()
        upcoming = [fx for fx in matches if fx["fixture"]["status"]["short"] in NOT_STARTED_STATUSES]
        if not upcoming:
            await ctx.send("📅 No more matches scheduled for today.")
            return
        upcoming.sort(key=lambda fx: fx["fixture"]["date"])
        embed = discord.Embed(title="📅 Today's Upcoming Matches", color=discord.Color.blue())
        for fx in upcoming:
            kickoff = datetime.fromisoformat(fx["fixture"]["date"])
            ts = int(kickoff.timestamp())
            home = fx["teams"]["home"]["name"]
            away = fx["teams"]["away"]["name"]
            round_name = fx["league"].get("round", "World Cup")
            embed.add_field(
                name=round_name,
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
            await ctx.send(f"⚠️ Couldn't reach api-football.com right now ({err}). Try again shortly.")
            return

        response = data.get("response", [])
        if not response:
            await ctx.send("The API returned no standings data for the World Cup yet.")
            return

        all_groups = response[0]["league"].get("standings", [])
        target = None
        for group_table in all_groups:
            if not group_table:
                continue
            label = (group_table[0].get("group") or "")
            if label.upper().replace("GROUP ", "").strip() == letter:
                target = group_table
                break

        if not target:
            seen = ", ".join(sorted({(g[0].get("group") or "?") for g in all_groups if g})) or "none"
            await ctx.send(f"No standings found for Group {letter}. Groups available: {seen}")
            return

        lines = [f"{'Pos':<4}{'Team':<20}{'P':>3}{'W':>3}{'D':>3}{'L':>3}{'GF':>4}{'GA':>4}{'GD':>4}{'Pts':>5}"]
        for row in target:
            name = row["team"]["name"]
            all_stats = row["all"]
            lines.append(
                f"{row['rank']:<4}{name[:19]:<20}{all_stats['played']:>3}{all_stats['win']:>3}"
                f"{all_stats['draw']:>3}{all_stats['lose']:>3}{all_stats['goals']['for']:>4}"
                f"{all_stats['goals']['against']:>4}{row['goalsDiff']:>4}{row['points']:>5}"
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
                    "match_id": str(match["fixture"]["id"]),
                    "side": side,
                    "amount": amount,
                    "settled": False,
                }
            )

        home = match["teams"]["home"]["name"]
        away = match["teams"]["away"]["name"]
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
        tokens = await self.bot.get_shared_api_tokens("apifootball")
        has_key = bool(tokens.get("api_key"))
        embed = discord.Embed(title="World Cup 2026 — Setup Checklist", color=discord.Color.orange())
        embed.add_field(
            name="1. API key",
            value=(
                "✅ Set" if has_key else
                "❌ Not set. Run `[p]set api apifootball api_key,YOUR_KEY` in DMs with the bot.\n"
                "Get a free key at https://dashboard.api-football.com/register"
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
                "Use voice channels for the nicest formatting (text channels get "
                "lowercased/hyphenated by Discord automatically)."
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

    @wc_admin.command(name="quota")
    async def wc_admin_quota(self, ctx: commands.Context):
        """Shows the last-known API-Football daily quota usage."""
        remaining = self._quota.get("remaining")
        limit = self._quota.get("limit")
        if remaining is None:
            await ctx.send("No quota info yet — the cog hasn't made an API call since it started.")
            return
        await ctx.send(f"📊 API quota: **{remaining}/{limit}** requests remaining today (resets 00:00 UTC).")

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
