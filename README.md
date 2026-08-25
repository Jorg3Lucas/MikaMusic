# 🎵 Discord Music Bot (Python)

Bot de música para Discord feito em Python usando `discord.py`.

## Comandos

| Comando | Descrição |
|---------|-----------|
| `/play <query>` | Toca música de qualquer plataforma (usa Deezer) |
| `/stop` | Para a música e desconecta |
| `/skip` | Pula para a próxima música |
| `/pause` | Pausa a música atual |
| `/resume` | Retoma a música pausada |
| `/now-playing` | Mostra a música que está tocando |
| `/queue` | Mostra a fila de músicas |
| `/loop [mode]` | Alterna o modo de loop (off/track/queue) |
| `/volume [level]` | Ajusta o volume (0-100) |

## Requisitos

- Python 3.10+
- [FFmpeg](https://ffmpeg.org/download.html) instalado e no PATH

## Instalação

```bash
# Clonar o repositório
git clone <url>
cd discord-music-bot

# Criar ambiente virtual
python -m venv venv
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate   # Windows

# Instalar dependências
pip install -r requirements.txt

# Configurar variáveis de ambiente
cp .env.example .env
# Edite o .env com seu DISCORD_TOKEN e CLIENT_ID
```

## Configuração

Crie um arquivo `.env`:

```
DISCORD_TOKEN=seu_token_aqui
CLIENT_ID=seu_client_id_aqui
```

## Uso

```bash
# Registrar slash commands (uma vez)
python deploy.py

# Iniciar o bot
python main.py
```

## Estrutura

```
├── main.py          # Entry point do bot
├── config.py        # Configuração (.env)
├── deploy.py        # Registrar slash commands
├── cogs/
│   └── music.py     # Comandos de música
├── utils/
│   └── deezer.py    # API do Deezer
├── requirements.txt
└── .env
```

## Como funciona

O bot busca músicas na API do Deezer e toca os previews de 30 segundos. Para funcionalidade completa com filas e músicas inteiras, considere integrar com Spotify/YouTube via serviços como `spotify-dl` ou `yt-dlp`.
