import sys
import io
import os
import glob as _glob
import signal
import asyncio
import discord
from discord.ext import commands
from config import DISCORD_TOKEN

# Fix Windows console encoding for emojis
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')


# ── Cleanup de temp files antigos ──────────────────────────────
def cleanup_temp_files():
    """Remove arquivos temporários de downloads anteriores."""
    temp_dir = "/tmp"
    count = 0
    for f in _glob.glob(os.path.join(temp_dir, "bot_*.webm")):
        try:
            os.unlink(f)
            count += 1
        except OSError:
            pass
    for f in _glob.glob(os.path.join(temp_dir, "bot_*.opus")):
        try:
            os.unlink(f)
            count += 1
        except OSError:
            pass
    for f in _glob.glob(os.path.join(temp_dir, "bot_*.m4a")):
        try:
            os.unlink(f)
            count += 1
        except OSError:
            pass
    if count > 0:
        print(f"🧹 {count} arquivo(s) temporário(s) removido(s)")


cleanup_temp_files()

intents = discord.Intents.default()
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ── Graceful shutdown ──────────────────────────────────────────
async def graceful_shutdown():
    """Para música e desconecta de todos os canais ao desligar."""
    print("\n🛑 Shutting down...")
    # Desconecta de todos os canais de voz
    for guild in bot.guilds:
        if guild.me.voice and guild.me.voice.is_connected():
            try:
                await guild.me.voice.disconnect()
                print(f"  🔌 Desconectado de {guild.name}")
            except Exception:
                pass
    # Cleanup temp files
    cleanup_temp_files()
    await bot.close()


def signal_handler(sig, frame):
    """Handler para SIGTERM/SIGINT."""
    print(f"\n📡 Signal {sig} recebido")
    asyncio.create_task(graceful_shutdown())

# Registra handlers (só no Linux)
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)


@bot.event
async def on_ready():
    print(f"🤖 Logado como {bot.user} (ID: {bot.user.id})")
    print(f"📡 Em {len(bot.guilds)} servidor(es)")
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.listening, name="/play"
        )
    )


async def main():
    async with bot:
        await bot.load_extension("cogs.music")
        print("✅ Cog de música carregada")
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
