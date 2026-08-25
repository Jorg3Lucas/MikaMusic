import asyncio
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import EMBED_COLOR, GUILD_ID
from utils.youtube import search_and_get_info, search_and_get_playlist, search_multiple, download_audio
from utils.platforms import detect_platform, is_ytdlp_platform, resolve_url_to_query

# Pool limitado: VPS com 2 vCPU não suporta muitas threads simultâneas
# (yt-dlp + ffmpeg consomem muita CPU)
_thread_pool = ThreadPoolExecutor(max_workers=2)


def _get_ffmpeg_path() -> str:
    """Find ffmpeg binary path."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        pass
    import shutil
    path = shutil.which("ffmpeg")
    if path:
        return path
    raise FileNotFoundError("FFmpeg não encontrado. Instale: pip install imageio-ffmpeg")


class TrackQueue:
    """Manages a queue of tracks for a guild."""

    def __init__(self):
        self.tracks: list[dict] = []
        self.history: list[dict] = []              # últimas músicas tocadas
        self.current: Optional[dict] = None
        self.voice_client: Optional[discord.VoiceClient] = None
        self.temp_file: Optional[str] = None
        self.playing = False
        self.channel = None                        # canal de voz atual
        self.prefetch_id: Optional[str] = None     # id da próxima música pré-baixada
        self.prefetch_path: Optional[str] = None   # arquivo já baixado da próxima
        self.prefetching = False
        self.started_at: Optional[float] = None    # timestamp de quando começou a tocar
        self.loop_mode: str = "off"                # off / track / queue
        self._queue_snapshot: list[dict] = []      # snapshot da fila para loop queue

    def add(self, track: dict):
        self.tracks.append(track)

    def add_many(self, tracks: list[dict]):
        self.tracks.extend(tracks)

    def next(self) -> Optional[dict]:
        """Get next track from queue. Returns None if empty."""
        if self.tracks:
            self.current = self.tracks.pop(0)
            return self.current
        self.current = None
        return None

    def add_to_history(self, track: dict):
        """Adiciona música ao histórico (máx 20)."""
        self.history.append(track)
        if len(self.history) > 20:
            self.history.pop(0)

    def shuffle(self):
        import random
        random.shuffle(self.tracks)

    def remove(self, position: int) -> dict:
        """Remove track at position (0-indexed). Returns removed track."""
        if position < 0 or position >= len(self.tracks):
            raise IndexError("Posição inválida.")
        return self.tracks.pop(position)

    def clear(self):
        self.tracks.clear()
        self.current = None

    @property
    def length(self) -> int:
        return len(self.tracks)

    @property
    def is_empty(self) -> bool:
        return len(self.tracks) == 0 and self.current is None


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.queues: dict[int, TrackQueue] = {}
        self._leave_tasks: dict[int, asyncio.Task] = {}  # tasks de timeout por guild

    # ── Voice State Events ───────────────────────────────────────
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """Detecta quando bot é expulso ou fica sozinho."""
        # 1. Bot foi desconectado manualmente (expulso de um canal)
        if member.id == self.bot.user.id and before.channel and not after.channel:
            guild_id = before.channel.guild.id
            queue = self.queues.get(guild_id)
            if queue:
                print(f"🔌 Bot expulso do canal em {before.channel.guild.name}")
                self._cleanup_queue(guild_id)
                # Tenta enviar mensagem no canal de texto
                try:
                    channel = before.channel.guild.system_channel or before.channel.guild.text_channels[0]
                    embed = discord.Embed(
                        title="🔌 Desconectado",
                        description="Fui expulso do canal de voz. A fila foi limpa.",
                        color=EMBED_COLOR,
                    )
                    await channel.send(embed=embed)
                except Exception:
                    pass
            return

        # 2. Bot ainda está no canal, verifica se ficou sozinho
        if member.id != self.bot.user.id and after.channel:
            # Alguém saiu de um canal onde o bot está
            bot_voice = member.guild.me.voice
            if bot_voice and bot_voice.channel and bot_voice.channel.id == after.channel.id:
                # Conta membros humanos (exclui bots)
                humans = [m for m in bot_voice.channel.members if not m.bot]
                if not humans and not self._leave_tasks.get(member.guild.id):
                    print(f"🔇 Bot sozinho em {bot_voice.channel.name}, aguardando 5s...")
                    self._leave_tasks[member.guild.id] = asyncio.create_task(
                        self._auto_leave_timeout(member.guild, bot_voice.channel)
                    )

        # 3. Alguém entrou no canal do bot → cancela leave pendente
        if member.id != self.bot.user.id and after.channel:
            bot_voice = member.guild.me.voice
            if bot_voice and bot_voice.channel and bot_voice.channel.id == after.channel.id:
                task = self._leave_tasks.pop(member.guild.id, None)
                if task and not task.done():
                    task.cancel()
                    print(f"✅ Humano entrou, cancelando auto-leave")

    async def _auto_leave_timeout(self, guild: discord.Guild, channel: discord.VoiceChannel):
        """Espera 5s e desconeca se ainda estiver sozinho."""
        try:
            await asyncio.sleep(5)
            # Verifica novamente se ainda está sozinho
            humans = [m for m in channel.members if not m.bot]
            if not humans:
                print(f"🔇 Saindo de {channel.name} (sozinho por 5s)")
                queue = self.queues.get(guild.id)
                if queue:
                    self._cleanup_queue(guild.id)
                if guild.me.voice and guild.me.voice.is_connected():
                    await guild.me.voice.disconnect()
                # Envia mensagem
                try:
                    ch = guild.system_channel or guild.text_channels[0]
                    embed = discord.Embed(
                        title="👋 Desconectado",
                        description="Fiquei sozinho no canal, desconectei.",
                        color=EMBED_COLOR,
                    )
                    await ch.send(embed=embed)
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass  # Humano entrou, não precisa sair
        finally:
            self._leave_tasks.pop(guild.id, None)

    def _cleanup_queue(self, guild_id: int):
        """Limpa a fila e para a música de um guild."""
        queue = self.queues.get(guild_id)
        if not queue:
            return
        # Para a música
        if queue.voice_client and queue.voice_client.is_playing():
            queue.voice_client.stop()
        # Limpa prefetch
        self._clear_prefetch(queue)
        # Limpa fila
        queue.clear()
        queue.playing = False
        queue.current = None
        queue.temp_file = None
        # Remove do dict
        if guild_id in self.queues:
            del self.queues[guild_id]

    # ── Prefetch ─────────────────────────────────────────────────
    def _schedule_prefetch(self, queue: TrackQueue):
        """Baixa a próxima música da fila em background enquanto a atual toca."""
        if not queue.tracks or queue.prefetching or queue.prefetch_id:
            return
        next_track = queue.tracks[0]
        queue.prefetching = True

        async def _do():
            temp_base = None
            try:
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix="", prefix="bot_")
                tmp.close()
                temp_base = tmp.name
                path = await asyncio.get_event_loop().run_in_executor(
                    _thread_pool, download_audio, next_track["id"], temp_base
                )
                queue.prefetch_id = next_track["id"]
                queue.prefetch_path = path
                print(f"⚡ Pré-baixado: {next_track['title']}")
            except Exception as e:
                print(f"⚠️ Pré-download falhou ({next_track['title']}): {e}")
                if temp_base and os.path.exists(temp_base):
                    try:
                        os.unlink(temp_base)
                    except OSError:
                        pass
            finally:
                queue.prefetching = False

        asyncio.create_task(_do())

    def _take_prefetched(self, queue: TrackQueue, track_id: str) -> Optional[str]:
        """Retorna o caminho do arquivo pré-baixado se for da faixa pedida."""
        path = queue.prefetch_path
        pid = queue.prefetch_id
        queue.prefetch_id = None
        queue.prefetch_path = None
        if path and pid == track_id and os.path.exists(path):
            return path
        # Arquivo obsoleto (fila mudou): descarta
        if path and os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass
        return None

    def _clear_prefetch(self, queue: TrackQueue):
        """Descarta qualquer arquivo pré-baixado pendente."""
        if queue.prefetch_path and os.path.exists(queue.prefetch_path):
            try:
                os.unlink(queue.prefetch_path)
            except OSError:
                pass
        queue.prefetch_id = None
        queue.prefetch_path = None

    def get_queue(self, guild_id: int) -> TrackQueue:
        if guild_id not in self.queues:
            self.queues[guild_id] = TrackQueue()
        return self.queues[guild_id]

    async def _play_track(self, interaction: discord.Interaction, track: dict, queue: TrackQueue):
        """Download and play a single track, then auto-play next from queue."""
        temp_base = None
        temp_path = None

        try:
            # Usa o arquivo pré-baixado se disponível (evita esperar o download)
            temp_path = self._take_prefetched(queue, track["id"])
            if temp_path:
                print(f"⚡ Usando pré-download: {track['title']}")
            else:
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix="", prefix="bot_")
                tmp.close()
                temp_base = tmp.name

                print(f"📥 Baixando: {track['title']}")
                temp_path = await asyncio.get_event_loop().run_in_executor(
                    _thread_pool, download_audio, track["id"], temp_base
                )

            if not os.path.exists(temp_path):
                raise ValueError("Falha ao baixar áudio.")

            print(f"✅ Áudio pronto: {temp_path}")

            # Reusa a conexão de voz existente (evita reconectar a cada música)
            if queue.voice_client and queue.voice_client.is_connected():
                voice_client = queue.voice_client
                if voice_client.is_playing() or voice_client.is_paused():
                    voice_client.stop()
            else:
                channel = queue.channel or (
                    interaction.user.voice.channel if interaction.user.voice else None
                )
                if not channel:
                    raise ValueError("Não estou em um canal de voz e não sei onde conectar.")
                queue.channel = channel
                voice_client = await channel.connect(self_deaf=True)

            # Play audio
            ffmpeg_path = _get_ffmpeg_path()
            source = discord.FFmpegPCMAudio(
                temp_path,
                executable=ffmpeg_path,
                before_options="-nostdin",
                options="-vn -f s16le",
            )
            source = discord.PCMVolumeTransformer(source, volume=1.0)

            queue.voice_client = voice_client
            queue.current = track
            queue.temp_file = temp_path
            queue.playing = True
            queue.started_at = __import__('time').time()

            def after_playing(error):
                if error:
                    print(f"Erro ao tocar: {error}")
                # Cleanup temp files
                for p in [temp_path, temp_base]:
                    try:
                        if p and os.path.exists(p):
                            os.unlink(p)
                    except OSError:
                        pass
                # Play next track from queue
                queue.playing = False
                queue.temp_file = None
                asyncio.run_coroutine_threadsafe(
                    self._auto_next(interaction, queue), self.bot.loop
                )

            voice_client.play(source, after=after_playing)

            # Envia painel de controle automaticamente
            try:
                # Envia no canal de texto onde o comando foi usado
                target_channel = interaction.channel
                if target_channel:
                    embed = self._build_np_embed(queue, interaction.guild)
                    view = self._build_np_view(queue)
                    np_msg = await target_channel.send(embed=embed, view=view)
                    # Inicia atualização periódica em background
                    asyncio.create_task(
                        self._np_updater(np_msg, queue, interaction.guild)
                    )
            except Exception as e:
                print(f"⚠️ Falha ao enviar painel: {e}")

            # Pré-baixa a próxima música em paralelo enquanto esta toca
            self._schedule_prefetch(queue)

        except Exception as e:
            print(f"Erro ao tocar: {e}")
            # Limpa arquivos temporários desta tentativa
            for p in [temp_path, temp_base]:
                try:
                    if p and os.path.exists(p):
                        os.unlink(p)
                except OSError:
                    pass
            queue.playing = False
            queue.temp_file = None
            # Try next track
            asyncio.run_coroutine_threadsafe(
                self._auto_next(interaction, queue), self.bot.loop
            )

    async def _auto_next(self, interaction: discord.Interaction, queue: TrackQueue):
        """Automatically play next track from queue, respecting loop mode."""
        # Salva a música atual no histórico
        if queue.current:
            queue.add_to_history(queue.current)

        # ── Loop: música atual ──
        if queue.loop_mode == "track" and queue.current:
            track = queue.current
            try:
                await self._play_track(interaction, track, queue)
                return
            except Exception as e:
                print(f"Erro ao repetir música: {e}")

        # ── Loop: fila inteira ──
        if queue.loop_mode == "queue":
            # Se a fila esvaziou, recicla do snapshot
            if not queue.tracks and queue._queue_snapshot:
                queue.tracks = list(queue._queue_snapshot)
                print(f"🔁 Loop queue: reciclando {len(queue.tracks)} músicas")

        # ── Próxima música normal ──
        next_track = queue.next()
        if next_track:
            # Salva snapshot da fila para loop queue
            if queue.loop_mode == "queue" and not queue._queue_snapshot:
                queue._queue_snapshot = list(queue.tracks) + [next_track]
            try:
                await self._play_track(interaction, next_track, queue)
            except Exception as e:
                print(f"Erro ao tocar próxima música: {e}")
        else:
            # Queue empty, disconnect
            self._clear_prefetch(queue)
            if queue.voice_client and queue.voice_client.is_connected():
                await queue.voice_client.disconnect()
            queue.clear()
            queue._queue_snapshot = []
            if interaction.guild_id in self.queues:
                del self.queues[interaction.guild_id]

    # ── /play ────────────────────────────────────────────────────
    @app_commands.command(name="play", description="Toca música ou playlist de qualquer plataforma")
    @app_commands.describe(query="Nome, link ou link de playlist")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def play(self, interaction: discord.Interaction, query: str):
        vc = interaction.user.voice
        if not vc or not vc.channel:
            return await interaction.response.send_message(
                "❌ Entre em um canal de voz!", ephemeral=True
            )

        channel = vc.channel
        bot_perms = channel.permissions_for(interaction.guild.me)
        if not bot_perms.connect or not bot_perms.speak:
            return await interaction.response.send_message(
                "❌ Preciso de permissão para Conectar e Falar!", ephemeral=True
            )

        await interaction.response.defer()

        try:
            queue = self.get_queue(interaction.guild_id)
            platform = detect_platform(query)

            # Check if it's a playlist URL and clean it
            is_playlist = "list=" in query.lower()
            if is_playlist:
                # Extract just the playlist ID to avoid conflicts with v= parameter
                list_match = re.search(r"list=([a-zA-Z0-9_-]+)", query)
                if list_match:
                    query = f"https://www.youtube.com/playlist?list={list_match.group(1)}"

            if is_playlist:
                print(f"📋 Buscando playlist: {query}")
                tracks = await asyncio.get_event_loop().run_in_executor(
                    _thread_pool, search_and_get_playlist, query
                )

                if not tracks:
                    raise ValueError("Nenhuma música encontrada na playlist.")

                # Add all tracks to queue (skip first, we play it immediately)
                first_track = tracks[0]
                remaining = tracks[1:]
                queue.add_many(remaining)

                playlist_name = first_track.get("playlist", "Playlist")
                print(f"📋 Playlist '{playlist_name}': {len(tracks)} músicas ({len(remaining)} na fila)")

                # Play first track
                await self._play_track(interaction, first_track, queue)

                embed = discord.Embed(
                    title="📋 Playlist carregada",
                    color=EMBED_COLOR,
                    description=(
                        f"**{playlist_name}**\n"
                        f"🎵 Tocando agora: **{first_track['title']}**\n"
                        f"📋 Adicionadas **{len(remaining)}** músicas à fila"
                    ),
                )
                if first_track.get("thumbnail"):
                    embed.set_thumbnail(url=first_track["thumbnail"])
                embed.set_footer(text=f"Pedido por {interaction.user}")

            else:
                # Single track — resolve platform URL to query if needed
                # Spotify/Deezer/Apple Music → extrai nome → busca no YouTube
                if platform and not is_ytdlp_platform(query):
                    search_query = await resolve_url_to_query(query)
                    platform_name = f"{platform['emoji']} {platform['name']}"
                    print(f"🔍 {platform['name']} → Buscando: {search_query}")
                else:
                    # YouTube, SoundCloud, Bandcamp, TikTok → yt-dlp direto
                    search_query = query
                    platform_name = f"{platform['emoji']} {platform['name']}" if platform else "▶️ YouTube"
                    print(f"🔍 Buscando: {query}")

                info = await asyncio.get_event_loop().run_in_executor(
                    _thread_pool, search_and_get_info, search_query
                )

                track_data = {
                    "title": info["title"],
                    "author": info["author"],
                    "duration": info["duration"],
                    "duration_raw": info.get("duration_raw", 0),
                    "thumbnail": info["thumbnail"],
                    "url": info["url"],
                    "id": info["id"],
                    "platform": platform_name,
                }

                # If something is playing, add to queue
                if queue.playing and queue.current:
                    queue.add(track_data)
                    embed = discord.Embed(
                        title="📋 Adicionado à fila",
                        color=EMBED_COLOR,
                        description=(
                            f"**[{track_data['title']}]({track_data['url']})**\n"
                            f"por {track_data['author']} | `{track_data['duration']}`\n"
                            f"Posição na fila: **{queue.length}**"
                        ),
                    )
                    if track_data["thumbnail"]:
                        embed.set_thumbnail(url=track_data["thumbnail"])
                    embed.set_footer(text=f"Pedido por {interaction.user}")
                else:
                    # Nothing playing, play immediately
                    await self._play_track(interaction, track_data, queue)

                    embed = discord.Embed(
                        title="▶️ Tocando agora",
                        color=EMBED_COLOR,
                        description=(
                            f"**[{track_data['title']}]({track_data['url']})**\n"
                            f"por {track_data['author']}\n"
                            f"Duração: `{track_data['duration']}`"
                        ),
                    )
                    if track_data["thumbnail"]:
                        embed.set_thumbnail(url=track_data["thumbnail"])
                    embed.set_footer(text=f"Pedido por {interaction.user}")

            await interaction.followup.send(embed=embed)

        except Exception as e:
            await interaction.followup.send(f"❌ {str(e)[:1900]}")

    # ── /stop ────────────────────────────────────────────────────
    @app_commands.command(name="stop", description="Para a música e desconecta")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def stop(self, interaction: discord.Interaction):
        queue = self.get_queue(interaction.guild_id)

        if not queue.voice_client or queue.is_empty and not queue.playing:
            return await interaction.response.send_message(
                "❌ Não há nada tocando!", ephemeral=True
            )

        # Stop current and clear queue
        if queue.voice_client and queue.voice_client.is_playing():
            queue.voice_client.stop()
        self._clear_prefetch(queue)
        queue.clear()
        if queue.voice_client and queue.voice_client.is_connected():
            await queue.voice_client.disconnect()
        if interaction.guild_id in self.queues:
            del self.queues[interaction.guild_id]

        embed = discord.Embed(
            title="⏹️ Parado",
            description="A fila foi limpa e o bot desconectou.",
            color=EMBED_COLOR,
        )
        await interaction.response.send_message(embed=embed)

    # ── /skip ────────────────────────────────────────────────────
    @app_commands.command(name="skip", description="Pula para a próxima música")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def skip(self, interaction: discord.Interaction):
        queue = self.get_queue(interaction.guild_id)

        if not queue.voice_client or (not queue.playing and queue.is_empty):
            return await interaction.response.send_message(
                "❌ Não há nada tocando!", ephemeral=True
            )

        current_title = queue.current["title"] if queue.current else "desconhecida"
        has_next = not queue.is_empty

        # Stop current (triggers after_playing → auto_next)
        if queue.voice_client and queue.voice_client.is_playing():
            queue.voice_client.stop()

        if has_next:
            embed = discord.Embed(
                title="⏭️ Pulando",
                description=f"**{current_title}** pulada. Próxima música entrando...",
                color=EMBED_COLOR,
            )
        else:
            embed = discord.Embed(
                title="⏭️ Pulando",
                description=f"**{current_title}** pulada. Fila vazia.",
                color=EMBED_COLOR,
            )

        await interaction.response.send_message(embed=embed)

    # ── /pause ───────────────────────────────────────────────────
    @app_commands.command(name="pause", description="Pausa a música atual")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def pause(self, interaction: discord.Interaction):
        queue = self.get_queue(interaction.guild_id)

        if not queue.voice_client or not queue.playing:
            return await interaction.response.send_message(
                "❌ Não há nada tocando!", ephemeral=True
            )

        if queue.voice_client.is_paused():
            return await interaction.response.send_message(
                "❌ A música já está pausada!", ephemeral=True
            )

        queue.voice_client.pause()

        title = queue.current["title"] if queue.current else "desconhecida"
        embed = discord.Embed(
            title="⏸️ Música pausada",
            description=f"**{title}** foi pausada.",
            color=EMBED_COLOR,
        )
        await interaction.response.send_message(embed=embed)

    # ── /resume ──────────────────────────────────────────────────
    @app_commands.command(name="resume", description="Retoma a música pausada")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def resume(self, interaction: discord.Interaction):
        queue = self.get_queue(interaction.guild_id)

        if not queue.voice_client or not queue.playing:
            return await interaction.response.send_message(
                "❌ Não há nada tocando!", ephemeral=True
            )

        if not queue.voice_client.is_paused():
            return await interaction.response.send_message(
                "❌ A música não está pausada!", ephemeral=True
            )

        queue.voice_client.resume()

        title = queue.current["title"] if queue.current else "desconhecida"
        embed = discord.Embed(
            title="▶️ Música retomada",
            description=f"**{title}** foi retomada.",
            color=EMBED_COLOR,
        )
        await interaction.response.send_message(embed=embed)

    # ── /now-playing ─────────────────────────────────────────────
    @app_commands.command(
        name="now-playing", description="Mostra a música que está tocando"
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def now_playing(self, interaction: discord.Interaction):
        queue = self.get_queue(interaction.guild_id)

        if not queue.current:
            return await interaction.response.send_message(
                "❌ Não há nada tocando!", ephemeral=True
            )

        track = queue.current
        if queue.voice_client and queue.voice_client.is_paused():
            status = "⏸️ Pausado"
        elif queue.playing:
            status = "▶️ Tocando"
        else:
            status = "⏹️ Parado"

        # Barra de progresso
        progress_str = ""
        if queue.started_at:
            import time as _time
            elapsed = int(_time.time() - queue.started_at)
            if queue.voice_client and queue.voice_client.is_paused():
                elapsed = 0
            duration_raw = track.get("duration_raw", 0)
            if duration_raw and duration_raw > 0:
                bar_len = 20
                pct = min(elapsed / duration_raw, 1.0)
                filled = int(bar_len * pct)
                bar = "█" * filled + "░" * (bar_len - filled)
                elapsed_str = f"{elapsed // 60}:{elapsed % 60:02d}"
                duration_str = track["duration"]
                progress_str = f"\n\n{bar}\n{elapsed_str} / {duration_str}"

        embed = discord.Embed(title=status, color=EMBED_COLOR)
        if track.get("thumbnail"):
            embed.set_thumbnail(url=track["thumbnail"])
        embed.add_field(
            name="Música",
            value=f"[{track['title']}]({track['url']}){progress_str}",
        )
        embed.add_field(name="Artista", value=track["author"], inline=True)
        embed.add_field(name="Fonte", value=track.get("platform", "YouTube"), inline=True)
        if queue.length > 0:
            embed.add_field(
                name="Fila", value=f"{queue.length} música(s) restante(s)", inline=True
            )
        if queue.loop_mode != "off":
            loop_labels = {"track": "🔂 Música", "queue": "🔁 Fila"}
            embed.add_field(name="Loop", value=loop_labels.get(queue.loop_mode, "off"), inline=True)
        embed.set_footer(text=f"Pedido por {interaction.user}")

        await interaction.response.send_message(embed=embed)

    # ── /queue ───────────────────────────────────────────────────
    @app_commands.command(name="queue", description="Mostra a fila de músicas")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def queue_cmd(self, interaction: discord.Interaction):
        queue = self.get_queue(interaction.guild_id)

        if not queue.current and queue.is_empty:
            return await interaction.response.send_message(
                "❌ Não há nada na fila!", ephemeral=True
            )

        # Current track
        lines = []
        if queue.current:
            track = queue.current
            lines.append(f"**🎵 Tocando agora:** {track['title']} — {track['author']} `{track['duration']}`")

        # Upcoming (limit to 10 to keep embed small)
        if not queue.is_empty:
            lines.append(f"\n**📋 Próximas ({queue.length}):**")
            for i, track in enumerate(queue.tracks[:10]):
                lines.append(f"{i+1}. {track['title']} — `{track['duration']}`")
            if queue.length > 10:
                lines.append(f"... e mais {queue.length - 10} música(s)")
        else:
            lines.append("\n**📋 Próximas:** Nenhuma música na fila.")

        embed = discord.Embed(title="📋 Fila de músicas", color=EMBED_COLOR, description="\n".join(lines))
        await interaction.response.send_message(embed=embed)

    # ── /loop ────────────────────────────────────────────────────
    @app_commands.command(name="loop", description="Alterna o modo de loop")
    @app_commands.describe(mode="Modo de loop")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Desligar", value="off"),
            app_commands.Choice(name="Música", value="track"),
            app_commands.Choice(name="Fila", value="queue"),
        ]
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def loop(
        self,
        interaction: discord.Interaction,
        mode: Optional[app_commands.Choice[str]] = None,
    ):
        queue = self.get_queue(interaction.guild_id)

        if not queue.voice_client or not queue.playing:
            return await interaction.response.send_message(
                "❌ Não há nada tocando!", ephemeral=True
            )

        if not mode:
            current = queue.loop_mode
            if current == "off":
                new_mode = "track"
            elif current == "track":
                new_mode = "queue"
            else:
                new_mode = "off"
        else:
            new_mode = mode.value

        queue.loop_mode = new_mode
        # Reseta snapshot quando muda o modo de loop
        if new_mode != "queue":
            queue._queue_snapshot = []

        labels = {
            "off": "🔒 Loop desligado",
            "track": "🔂 Loop: música atual",
            "queue": "🔁 Loop: fila inteira",
        }

        embed = discord.Embed(
            title=labels[new_mode],
            description=f"Modo de loop alterado para **{labels[new_mode][2:].strip()}**",
            color=EMBED_COLOR,
        )
        await interaction.response.send_message(embed=embed)

    # ── /volume ──────────────────────────────────────────────────
    @app_commands.command(name="volume", description="Ajusta o volume da música")
    @app_commands.describe(level="Volume (0-100)")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def volume(
        self,
        interaction: discord.Interaction,
        level: Optional[int] = None,
    ):
        queue = self.get_queue(interaction.guild_id)

        if not queue.voice_client or not queue.playing:
            return await interaction.response.send_message(
                "❌ Não há nada tocando!", ephemeral=True
            )

        if level is None:
            current_vol = int(queue.voice_client.source.volume * 100) if queue.voice_client.source else 100
            embed = discord.Embed(
                title="🔊 Volume",
                description=f"Volume atual: **{current_vol}%**",
                color=EMBED_COLOR,
            )
            return await interaction.response.send_message(embed=embed)

        if level < 0 or level > 100:
            return await interaction.response.send_message(
                "❌ Volume deve ser entre 0 e 100!", ephemeral=True
            )

        if queue.voice_client.source and hasattr(queue.voice_client.source, "volume"):
            queue.voice_client.source.volume = level / 100.0

        embed = discord.Embed(
            title="🔊 Volume alterado",
            description=f"Volume definido para **{level}%**",
            color=EMBED_COLOR,
        )
        await interaction.response.send_message(embed=embed)

    # ── /shuffle ─────────────────────────────────────────────────
    @app_commands.command(
        name="shuffle", description="Embaralha a fila de músicas"
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def shuffle(self, interaction: discord.Interaction):
        queue = self.get_queue(interaction.guild_id)

        if queue.is_empty:
            return await interaction.response.send_message(
                "❌ Não há músicas na fila para embaralhar!", ephemeral=True
            )

        queue.shuffle()

        embed = discord.Embed(
            title="🔀 Fila embaralhada",
            description=f"Embaralhei as **{queue.length}** músicas da fila!",
            color=EMBED_COLOR,
        )
        await interaction.response.send_message(embed=embed)

    # ── /remove ──────────────────────────────────────────────────
    @app_commands.command(
        name="remove", description="Remove uma música da fila"
    )
    @app_commands.describe(position="Posição da música na fila (começa em 1)")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def remove(self, interaction: discord.Interaction, position: int):
        queue = self.get_queue(interaction.guild_id)

        if queue.is_empty:
            return await interaction.response.send_message(
                "❌ Não há músicas na fila!", ephemeral=True
            )

        idx = position - 1
        if idx < 0 or idx >= queue.length:
            return await interaction.response.send_message(
                f"❌ Posição inválida! A fila tem **{queue.length}** música(s).",
                ephemeral=True,
            )

        removed = queue.remove(idx)

        embed = discord.Embed(
            title="🗑️ Música removida",
            description=(
                f"**{removed['title']}** foi removida da fila na posição **{position}**."
            ),
            color=EMBED_COLOR,
        )
        await interaction.response.send_message(embed=embed)

    # ── /history ────────────────────────────────────────────────
    @app_commands.command(
        name="history", description="Mostra as últimas músicas tocadas"
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def history(self, interaction: discord.Interaction):
        queue = self.get_queue(interaction.guild_id)

        if not queue.history:
            return await interaction.response.send_message(
                "❌ Nenhuma música foi tocada ainda nesta sessão.", ephemeral=True
            )

        # Últimas 15 músicas (mais recente primeiro)
        lines = []
        for i, track in enumerate(reversed(queue.history[-15:])):
            lines.append(f"{i+1}. **{track['title']}** — {track['author']} `{track['duration']}`")

        total = len(queue.history)
        embed = discord.Embed(
            title=f"📜 Histórico ({total} música(s))",
            color=EMBED_COLOR,
            description="\n".join(lines),
        )
        if total > 15:
            embed.set_footer(text=f"Mostrando as últimas 15 de {total}")

        await interaction.response.send_message(embed=embed)

    # ── Painel de controle persistente ──────────────────────────
    def _build_np_embed(self, queue: TrackQueue, guild: discord.Guild) -> discord.Embed:
        """Constrói o embed do painel de controle."""
        track = queue.current
        if not track:
            return discord.Embed(title="⏹️ Nada tocando", color=EMBED_COLOR)

        if queue.voice_client and queue.voice_client.is_paused():
            status = "⏸️ Pausado"
        elif queue.playing:
            status = "▶️ Tocando"
        else:
            status = "⏹️ Parado"

        # Barra de progresso
        progress_str = ""
        if queue.started_at:
            import time as _time
            elapsed = int(_time.time() - queue.started_at)
            if queue.voice_client and queue.voice_client.is_paused():
                elapsed = 0
            duration_raw = track.get("duration_raw", 0)
            if duration_raw and duration_raw > 0:
                bar_len = 20
                pct = min(elapsed / duration_raw, 1.0)
                filled = int(bar_len * pct)
                bar = "█" * filled + "░" * (bar_len - filled)
                elapsed_str = f"{elapsed // 60}:{elapsed % 60:02d}"
                duration_str = track["duration"]
                progress_str = f"\n\n{bar}\n\u2002{elapsed_str} / {duration_str}"

        embed = discord.Embed(title=status, color=EMBED_COLOR)
        if track.get("thumbnail"):
            embed.set_thumbnail(url=track["thumbnail"])
        embed.add_field(
            name="Música",
            value=f"[{track['title']}]({track['url']}){progress_str}",
            inline=False,
        )
        embed.add_field(name="Artista", value=track["author"], inline=True)
        embed.add_field(name="Fonte", value=track.get("platform", "YouTube"), inline=True)
        if queue.length > 0:
            embed.add_field(
                name="Fila", value=f"{queue.length} música(s)", inline=True
            )
        if queue.loop_mode != "off":
            loop_labels = {"track": "🔂 Música", "queue": "🔁 Fila"}
            embed.add_field(name="Loop", value=loop_labels.get(queue.loop_mode, "off"), inline=True)
        embed.set_footer(text=f"Mika Music • Atualiza a cada 10s")
        return embed

    def _build_np_view(self, queue: TrackQueue) -> discord.ui.View:
        """Constrói a view com botões de controle."""
        view = NowPlayingView(queue=queue, music_cog=self)
        return view

    # ── /now-playing ─────────────────────────────────────────────
    @app_commands.command(
        name="now-playing", description="Mostra painel de controle da música atual"
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def now_playing(self, interaction: discord.Interaction):
        queue = self.get_queue(interaction.guild_id)

        if not queue.current:
            return await interaction.response.send_message(
                "❌ Nada tocando!", ephemeral=True
            )

        embed = self._build_np_embed(queue, interaction.guild)
        view = self._build_np_view(queue)

        await interaction.response.send_message(embed=embed, view=view)

        # Inicia atualização periódica
        msg = await interaction.original_response()
        asyncio.create_task(
            self._np_updater(msg, queue, interaction.guild)
        )

    async def _np_updater(self, msg: discord.Message, queue: TrackQueue, guild: discord.Guild):
        """Atualiza o painel a cada 10 segundos."""
        try:
            while True:
                await asyncio.sleep(10)

                # Para se a música mudou ou acabou
                if not queue.current or not queue.playing:
                    break

                try:
                    embed = self._build_np_embed(queue, guild)
                    view = self._build_np_view(queue)
                    await msg.edit(embed=embed, view=view)
                except discord.NotFound:
                    break  # Mensagem foi deletada
                except Exception:
                    break  # Erro qualquer, para
        except asyncio.CancelledError:
            pass


class NowPlayingView(discord.ui.View):
    """View com botões de controle para o painel de música."""

    def __init__(self, queue: TrackQueue, music_cog: 'Music'):
        super().__init__(timeout=None)  # Sem timeout
        self.queue = queue
        self.music_cog = music_cog

    @discord.ui.button(emoji="⏸️", style=discord.ButtonStyle.secondary, custom_id="np_pause")
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.queue.voice_client:
            return await interaction.response.send_message("❌ Nada tocando!", ephemeral=True)
        if self.queue.voice_client.is_paused():
            return await interaction.response.send_message("⏸️ Já está pausado!", ephemeral=True)
        self.queue.voice_client.pause()
        await interaction.response.send_message("⏸️ Pausado", ephemeral=True)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary, custom_id="np_resume")
    async def resume_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.queue.voice_client:
            return await interaction.response.send_message("❌ Nada tocando!", ephemeral=True)
        if not self.queue.voice_client.is_paused():
            return await interaction.response.send_message("▶️ Já está tocando!", ephemeral=True)
        self.queue.voice_client.resume()
        await interaction.response.send_message("▶️ Retomado", ephemeral=True)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="np_skip")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.queue.voice_client or (not self.queue.playing and self.queue.is_empty):
            return await interaction.response.send_message("❌ Nada para pular!", ephemeral=True)
        title = self.queue.current["title"] if self.queue.current else "desconhecida"
        if self.queue.voice_client.is_playing():
            self.queue.voice_client.stop()
        await interaction.response.send_message(f"⏭️ **{title}** pulada", ephemeral=True)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="np_stop")
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.queue.voice_client or (self.queue.is_empty and not self.queue.playing):
            return await interaction.response.send_message("❌ Nada tocando!", ephemeral=True)
        if self.queue.voice_client.is_playing():
            self.queue.voice_client.stop()
        self.music_cog._clear_prefetch(self.queue)
        self.queue.clear()
        if self.queue.voice_client and self.queue.voice_client.is_connected():
            await self.queue.voice_client.disconnect()
        if interaction.guild_id in self.music_cog.queues:
            del self.music_cog.queues[interaction.guild_id]
        await interaction.response.send_message("⏹️ Parado e desconectado", ephemeral=True)

    @discord.ui.button(emoji="🔊", style=discord.ButtonStyle.secondary, custom_id="np_vol_up", row=1)
    async def vol_up_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.queue.voice_client or not self.queue.playing:
            return await interaction.response.send_message("❌ Nada tocando!", ephemeral=True)
        src = self.queue.voice_client.source
        if src and hasattr(src, "volume"):
            src.volume = min(src.volume + 0.1, 1.0)
            vol = int(src.volume * 100)
            await interaction.response.send_message(f"🔊 Volume: {vol}%", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Volume não disponível", ephemeral=True)

    @discord.ui.button(emoji="🔉", style=discord.ButtonStyle.secondary, custom_id="np_vol_down", row=1)
    async def vol_down_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.queue.voice_client or not self.queue.playing:
            return await interaction.response.send_message("❌ Nada tocando!", ephemeral=True)
        src = self.queue.voice_client.source
        if src and hasattr(src, "volume"):
            src.volume = max(src.volume - 0.1, 0.0)
            vol = int(src.volume * 100)
            await interaction.response.send_message(f"🔉 Volume: {vol}%", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Volume não disponível", ephemeral=True)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="np_shuffle", row=1)
    async def shuffle_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.queue.is_empty:
            return await interaction.response.send_message("❌ Fila vazia!", ephemeral=True)
        self.queue.shuffle()
        await interaction.response.send_message(f"🔀 Fila embaralhada ({self.queue.length} músicas)", ephemeral=True)


# ── /search ─────────────────────────────────────────────────
    @app_commands.command(name="search", description="Busca músicas e você escolhe qual tocar")
    @app_commands.describe(query="Nome ou artista para buscar")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def search(self, interaction: discord.Interaction, query: str):
        vc = interaction.user.voice
        if not vc or not vc.channel:
            return await interaction.response.send_message(
                "❌ Entre em um canal de voz!", ephemeral=True
            )

        await interaction.response.defer()

        try:
            results = await asyncio.get_event_loop().run_in_executor(
                _thread_pool, search_multiple, query, 5
            )
        except Exception as e:
            return await interaction.followup.send(f"❌ {str(e)[:1900]}")

        if not results:
            return await interaction.followup.send("❌ Nenhum resultado encontrado.")

        # Salva resultados temporariamente
        view = SearchView(
            results=results,
            user_id=interaction.user.id,
            music_cog=self,
            interaction=interaction,
        )

        # Embed com os resultados
        lines = []
        for i, track in enumerate(results):
            lines.append(f"**{i+1}.** {track['title']} — `{track['duration']}`")

        embed = discord.Embed(
            title=f"🔍 Resultados para: {query}",
            color=EMBED_COLOR,
            description="\n".join(lines) + "\n\n🔽 Selecione uma música abaixo:",
        )
        await interaction.followup.send(embed=embed, view=view)


class SearchView(discord.ui.View):
    """View com Select Menu para escolher resultado da busca."""

    def __init__(self, results: list[dict], user_id: int, music_cog: 'Music', interaction: discord.Interaction):
        super().__init__(timeout=60)  # 60s para escolher
        self.results = results
        self.user_id = user_id
        self.music_cog = music_cog
        self.interaction = interaction
        self.chosen = False

        # Adiciona o Select Menu
        options = []
        for i, track in enumerate(results[:5]):
            # Label max 100 chars, description max 100 chars
            label = track['title'][:100]
            desc = f"{track['author']} — {track['duration']}"[:100]
            options.append(discord.SelectOption(
                label=label,
                description=desc,
                value=str(i),
                emoji="🎵",
            ))

        select = discord.ui.Select(
            placeholder="Escolha uma música...",
            options=options,
            custom_id="search_select",
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "❌ Só quem usou /search pode escolher!", ephemeral=True
            )

        if self.chosen:
            return await interaction.response.send_message(
                "❌ Já foi escolhida!", ephemeral=True
            )

        self.chosen = True
        idx = int(interaction.data["values"][0])
        track = self.results[idx]

        # Desabilita o select
        for item in self.children:
            item.disabled = True

        await interaction.response.edit_message(view=self)

        # Toca a música escolhida
        queue = self.music_cog.get_queue(interaction.guild_id)

        if queue.playing and queue.current:
            queue.add(track)
            embed = discord.Embed(
                title="📋 Adicionado à fila",
                color=EMBED_COLOR,
                description=(
                    f"**[{track['title']}]({track['url']})**\n"
                    f"por {track['author']} | `{track['duration']}`\n"
                    f"Posição na fila: **{queue.length}**"
                ),
            )
            if track.get("thumbnail"):
                embed.set_thumbnail(url=track["thumbnail"])
            embed.set_footer(text=f"Escolhido por {interaction.user}")
            await interaction.followup.send(embed=embed)
        else:
            await self.music_cog._play_track(interaction, track, queue)
            embed = discord.Embed(
                title="▶️ Tocando agora",
                color=EMBED_COLOR,
                description=(
                    f"**[{track['title']}]({track['url']})**\n"
                    f"por {track['author']}\n"
                    f"Duração: `{track['duration']}"
                ),
            )
            if track.get("thumbnail"):
                embed.set_thumbnail(url=track["thumbnail"])
            embed.set_footer(text=f"Escolhido por {interaction.user}")
            await interaction.followup.send(embed=embed)

    async def on_timeout(self):
        # Desabilita tudo após timeout
        for item in self.children:
            item.disabled = True
        try:
            await self.interaction.edit_original_response(view=self)
        except Exception:
            pass


def _format_duration(seconds: int) -> str:
    minutes = seconds // 60
    secs = seconds % 60
    return f"{minutes}:{secs:02d}"


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
