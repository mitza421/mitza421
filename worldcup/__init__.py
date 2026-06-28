from .worldcup import WorldCup


async def setup(bot):
    cog = WorldCup(bot)
    await bot.add_cog(cog)
