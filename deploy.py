import asyncio
import aiohttp
from config import DISCORD_TOKEN, CLIENT_ID, GUILD_ID

# All commands to register
COMMANDS = [
    {
        "name": "play",
        "description": "Toca música ou playlist de qualquer plataforma",
        "options": [
            {
                "name": "query",
                "description": "Nome, link ou link de playlist",
                "type": 3,
                "required": True,
            }
        ],
    },
    {"name": "stop", "description": "Para a música e desconecta"},
    {"name": "skip", "description": "Pula para a próxima música"},
    {"name": "pause", "description": "Pausa a música atual"},
    {"name": "resume", "description": "Retoma a música pausada"},
    {"name": "now-playing", "description": "Mostra a música que está tocando"},
    {"name": "queue", "description": "Mostra a fila de músicas"},
    {
        "name": "loop",
        "description": "Alterna o modo de loop",
        "options": [
            {
                "name": "mode",
                "description": "Modo de loop",
                "type": 3,
                "required": False,
                "choices": [
                    {"name": "Desligar", "value": "off"},
                    {"name": "Música", "value": "track"},
                    {"name": "Fila", "value": "queue"},
                ],
            }
        ],
    },
    {
        "name": "volume",
        "description": "Ajusta o volume da música",
        "options": [
            {
                "name": "level",
                "description": "Volume (0-100)",
                "type": 4,
                "required": False,
                "min_value": 0,
                "max_value": 100,
            }
        ],
    },
    {"name": "shuffle", "description": "Embaralha a fila de músicas"},
    {"name": "history", "description": "Mostra as últimas músicas tocadas"},
    {
        "name": "search",
        "description": "Busca músicas e você escolhe qual tocar",
        "options": [
            {
                "name": "query",
                "description": "Nome ou artista para buscar",
                "type": 3,
                "required": True,
            }
        ],
    },
    {
        "name": "remove",
        "description": "Remove uma música da fila",
        "options": [
            {
                "name": "position",
                "description": "Posição da música na fila (começa em 1)",
                "type": 4,
                "required": True,
                "min_value": 1,
            }
        ],
    },
]


async def deploy():
    """Clear global commands and register only for the specific guild."""
    headers = {
        "Authorization": f"Bot {DISCORD_TOKEN}",
        "Content-Type": "application/json",
    }

    async with aiohttp.ClientSession() as session:
        # 1. Delete ALL global commands
        global_url = f"https://discord.com/api/v10/applications/{CLIENT_ID}/commands"
        async with session.get(global_url, headers=headers) as resp:
            if resp.status == 200:
                global_cmds = await resp.json()
                if global_cmds:
                    print(f"🔄 Removendo {len(global_cmds)} comandos globais...")
                    for cmd in global_cmds:
                        delete_url = f"{global_url}/{cmd['id']}"
                        async with session.delete(delete_url, headers=headers) as r:
                            if r.status == 204:
                                print(f"  ❌ Removido: /{cmd['name']}")
                else:
                    print("✅ Nenhum comando global para remover.")

        # 2. Delete ALL guild commands (clean slate)
        guild_url = f"https://discord.com/api/v10/applications/{CLIENT_ID}/guilds/{GUILD_ID}/commands"
        async with session.get(guild_url, headers=headers) as resp:
            if resp.status == 200:
                guild_cmds = await resp.json()
                if guild_cmds:
                    print(f"🔄 Removendo {len(guild_cmds)} comandos do servidor...")
                    for cmd in guild_cmds:
                        delete_url = f"{guild_url}/{cmd['id']}"
                        async with session.delete(delete_url, headers=headers) as r:
                            if r.status == 204:
                                print(f"  ❌ Removido: /{cmd['name']}")

        # 3. Register new guild commands
        print(f"🔄 Registrando {len(COMMANDS)} comandos no servidor {GUILD_ID}...")
        async with session.put(guild_url, json=COMMANDS, headers=headers) as resp:
            if resp.status == 200:
                data = await resp.json()
                print(f"✅ {len(data)} comandos registrados com sucesso!")
                for cmd in data:
                    print(f"  ✅ /{cmd['name']}")
            else:
                error = await resp.text()
                print(f"❌ Erro {resp.status}: {error}")


if __name__ == "__main__":
    asyncio.run(deploy())
