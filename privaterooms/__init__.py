from .privaterooms import PrivateRooms


async def setup(bot):
    cog = PrivateRooms(bot)
    await bot.add_cog(cog)
