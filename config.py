import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CLIENT_ID = os.getenv("CLIENT_ID")
GUILD_ID = 1496674768215343325

# Cor do embed (azul padrão do Discord)
EMBED_COLOR = 0x5865F2
