import os
import shutil
import time
import re

import yt_dlp

# Caminho dos cookies: pode ser sobrescrito com a env YOUTUBE_COOKIES.
# Default: cookies.txt na raiz do projeto (funciona local e na VPS).
COOKIES_FILE = os.getenv("YOUTUBE_COOKIES", os.path.join(os.path.dirname(os.path.dirname(__file__)), "cookies.txt"))
COOKIES_BACKUP = COOKIES_FILE + ".bak"

# Backup dos cookies válidos para restaurar se forem sobrescritos
# Cookies original carregado no startup
_original_cookies: str = ""


def _load_and_protect_cookies():
    """Carrega cookies e salva backup para restaurar se sobrescritos."""
    global _original_cookies
    if os.path.exists(COOKIES_FILE):
        with open(COOKIES_FILE, "r") as f:
            _original_cookies = f.read()
        if _original_cookies.strip() and "youtube.com" in _original_cookies:
            # Salva backup
            with open(COOKIES_BACKUP, "w") as f:
                f.write(_original_cookies)
            print(f"✅ Cookies carregados e backup salvo: {COOKIES_FILE}")
        else:
            print(f"⚠️ cookies.txt existe mas parece vazio ou inválido")
            _original_cookies = ""
    elif os.path.exists(COOKIES_BACKUP):
        # Cookies.txt foi deletado, restaura do backup
        with open(COOKIES_BACKUP, "r") as f:
            _original_cookies = f.read()
        with open(COOKIES_FILE, "w") as f:
            f.write(_original_cookies)
        print(f"🔄 Cookies restaurados do backup: {COOKIES_BACKUP}")
    else:
        print(f"❌ cookies.txt não encontrado: {COOKIES_FILE}")
        _original_cookies = ""


def _ensure_cookies():
    """Verifica se cookies foram sobrescritos e restaura se necessário."""
    global _original_cookies
    if not _original_cookies:
        return

    current = ""
    if os.path.exists(COOKIES_FILE):
        with open(COOKIES_FILE, "r") as f:
            current = f.read()

    # Se o arquivo mudou (plugin sobrescreveu), restaura
    if current != _original_cookies:
        print(f"⚠️ cookies.txt foi alterado! Restaurando versão original...")
        with open(COOKIES_FILE, "w") as f:
            f.write(_original_cookies)
        print(f"✅ cookies.txt restaurado")


# Inicializa proteção dos cookies ao importar o módulo
_load_and_protect_cookies()


def _js_runtimes_opt() -> dict | None:
    """Formato exigido pela API Python do yt-dlp: dict de {runtime: {config}}.

    Node precisa ser >= 22 (mínimo suportado). Alternativa: instalar deno,
    que é detectado automaticamente (aí basta remover esta opção).
    """
    node_path = os.getenv("YT_NODE_PATH") or shutil.which("node")
    if not node_path or not os.path.exists(node_path):
        return None
    return {"node": {"path": node_path}}


def _get_opts(extra: dict = None) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        # Sem remote_components: evita fetch de JS do GitHub a cada chamada
        # (latência enorme em VPS com ping alto)
        "socket_timeout": 15,
        # Apenas player_client default: testar 3 clients é lento
        # Com cookies.txt válido, o default funciona sem PO Token
        "extractor_args": {
            "youtube": {
                "player_client": ["default"],
            }
        },
    }
    js_runtimes = _js_runtimes_opt()
    if js_runtimes:
        opts["js_runtimes"] = js_runtimes

    if os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    if extra:
        opts.update(extra)
    return opts


# YouTube Data API v3 — busca MUITO mais rápida que yt-dlp (1 request HTTP)
# Se não tiver API key, usa yt-dlp como fallback.
_YT_API_KEY = os.getenv("YOUTUBE_API_KEY", "")

# Cache simples em memória para buscas recentes
_search_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 600  # 10 minutos


def _cache_get(key: str) -> dict | None:
    entry = _search_cache.get(key)
    if entry and (time.time() - entry[0]) < _CACHE_TTL:
        return entry[1]
    _search_cache.pop(key, None)
    return None


def _cache_set(key: str, value: dict):
    if len(_search_cache) > 100:
        now = time.time()
        expired = [k for k, (t, _) in _search_cache.items() if (now - t) >= _CACHE_TTL]
        for k in expired:
            _search_cache.pop(k, None)
    _search_cache[key] = (time.time(), value)


def _http_get_json(url: str, params: dict, timeout: int = 10) -> dict:
    """GET simples via urllib (sync, para usar em thread)."""
    import urllib.request
    import urllib.parse
    import json

    qs = urllib.parse.urlencode(params)
    full = f"{url}?{qs}"
    req = urllib.request.Request(full, headers={"User-Agent": "MikaMusic/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _yt_api_search(query: str) -> dict:
    """Busca rápida via YouTube Data API v3 (1 request HTTP, ~200ms)."""
    if not _YT_API_KEY:
        raise ValueError("No API key")

    data = _http_get_json(
        "https://www.googleapis.com/youtube/v3/search",
        {
            "part": "snippet",
            "q": query,
            "type": "video",
            "videoCategoryId": "10",  # Music
            "maxResults": 1,
            "key": _YT_API_KEY,
        },
    )

    items = data.get("items", [])
    if not items:
        raise ValueError("Nenhum resultado encontrado no YouTube.")

    snippet = items[0]["snippet"]
    video_id = items[0]["id"]["videoId"]
    duration = _yt_api_get_duration(video_id)

    return {
        "title": snippet["title"],
        "author": snippet["channelTitle"],
        "duration": duration,
        "duration_raw": 0,
        "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url"),
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "id": video_id,
    }


def _yt_api_get_duration(video_id: str) -> str:
    """Busca duração do vídeo via API (1 request leve)."""
    if not _YT_API_KEY:
        return "?"

    try:
        data = _http_get_json(
            "https://www.googleapis.com/youtube/v3/videos",
            {
                "part": "contentDetails",
                "id": video_id,
                "key": _YT_API_KEY,
            },
            timeout=5,
        )
        items = data.get("items", [])
        if items:
            iso = items[0]["contentDetails"]["duration"]  # PT4M13S
            return _parse_iso_duration(iso)
    except Exception:
        pass
    return "?"


def _parse_iso_duration(iso: str) -> str:
    """Converte PT4M13S → 4:13"""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso)
    if not m:
        return "?"
    h, mi, s = int(m.group(1) or 0), int(m.group(2) or 0), int(m.group(3) or 0)
    total = h * 3600 + mi * 60 + s
    if h > 0:
        return f"{h}:{mi:02d}:{s:02d}"
    return f"{mi}:{s:02d}"


def search_and_get_info(query: str) -> dict:
    # 1. Verifica cache primeiro
    cached = _cache_get(query)
    if cached:
        print(f"⚡ Cache hit: {query}")
        return cached

    # 2. Tenta YouTube Data API v3 (MUITO mais rápido, ~200ms)
    if _YT_API_KEY:
        try:
            result = _yt_api_search(query)
            _cache_set(query, result)
            print(f"⚡ YouTube API: {result['title']}")
            return result
        except Exception as e:
            print(f"⚠️ YouTube API falhou, usando yt-dlp: {e}")

    # 3. Fallback: yt-dlp (lento)
    opts = _get_opts({
        "default_search": "ytsearch1",
        "noplaylist": True,
        "skip_download": True,
    })

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=False)
        if not info:
            raise ValueError("Nenhum resultado encontrado no YouTube.")

        if "entries" in info:
            info = info["entries"][0]
            if not info:
                raise ValueError("Nenhum resultado encontrado no YouTube.")

        result = _extract_track_info(info)
        _cache_set(query, result)
        return result


def search_and_get_playlist(query: str) -> list[dict]:
    opts = _get_opts({
        "default_search": "ytsearch5",
        "skip_download": True,
        "extract_flat": True,
    })

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=False)
        if not info:
            raise ValueError("Nenhum resultado encontrado no YouTube.")

        tracks = []
        if "entries" in info:
            playlist_title = info.get("title", "Playlist")
            for entry in info["entries"]:
                if entry is None:
                    continue
                video_id = entry.get("id") or entry.get("url", "")
                title = entry.get("title", "Desconhecido")
                duration = _format_duration(entry.get("duration"))
                tracks.append({
                    "title": title,
                    "author": entry.get("uploader", entry.get("channel", "?")),
                    "duration": duration,
                    "duration_raw": entry.get("duration", 0),
                    "thumbnail": None,
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "id": video_id,
                    "platform": "▶️ YouTube",
                    "playlist": playlist_title,
                })
        else:
            tracks.append(_extract_track_info(info))

        if not tracks:
            raise ValueError("Nenhum resultado encontrado no YouTube.")

        return tracks


def _extract_track_info(info: dict, playlist_title: str = None) -> dict:
    return {
        "title": info.get("title", "Desconhecido"),
        "author": info.get("uploader", info.get("channel", "?")),
        "duration": _format_duration(info.get("duration", 0)),
        "duration_raw": info.get("duration", 0),
        "thumbnail": info.get("thumbnail"),
        "url": info.get("webpage_url", info.get("url", "")),
        "id": info.get("id", ""),
        "playlist": playlist_title,
    }


def search_multiple(query: str, count: int = 5) -> list[dict]:
    """Busca múltiplos resultados no YouTube para o /search."""
    # Tenta YouTube Data API v3 primeiro
    if _YT_API_KEY:
        try:
            data = _http_get_json(
                "https://www.googleapis.com/youtube/v3/search",
                {
                    "part": "snippet",
                    "q": query,
                    "type": "video",
                    "videoCategoryId": "10",  # Music
                    "maxResults": count,
                    "key": _YT_API_KEY,
                },
            )
            items = data.get("items", [])
            if items:
                # Busca durações em batch (1 request)
                video_ids = [item["id"]["videoId"] for item in items]
                durations = _yt_api_get_durations_batch(video_ids)

                results = []
                for item, dur in zip(items, durations):
                    snippet = item["snippet"]
                    vid = item["id"]["videoId"]
                    results.append({
                        "title": snippet["title"],
                        "author": snippet["channelTitle"],
                        "duration": dur,
                        "duration_raw": 0,
                        "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url"),
                        "url": f"https://www.youtube.com/watch?v={vid}",
                        "id": vid,
                    })
                print(f"⚡ YouTube API: {len(results)} resultados para '{query}'")
                return results
        except Exception as e:
            print(f"⚠️ YouTube API falhou, usando yt-dlp: {e}")

    # Fallback: yt-dlp
    opts = _get_opts({
        "default_search": f"ytsearch{count}",
        "noplaylist": True,
        "skip_download": True,
    })

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=False)
        if not info:
            raise ValueError("Nenhum resultado encontrado no YouTube.")

        tracks = []
        if "entries" in info:
            for entry in info["entries"]:
                if entry is None:
                    continue
                tracks.append(_extract_track_info(entry))

        if not tracks:
            raise ValueError("Nenhum resultado encontrado no YouTube.")

        return tracks


def _yt_api_get_durations_batch(video_ids: list[str]) -> list[str]:
    """Busca duração de múltiplos vídeos em 1 request."""
    if not _YT_API_KEY or not video_ids:
        return ["?"] * len(video_ids)

    try:
        data = _http_get_json(
            "https://www.googleapis.com/youtube/v3/videos",
            {
                "part": "contentDetails",
                "id": ",".join(video_ids),
                "key": _YT_API_KEY,
            },
            timeout=5,
        )
        items = data.get("items", [])
        # Cria dict id→duration
        dur_map = {}
        for item in items:
            vid = item["id"]
            iso = item["contentDetails"]["duration"]
            dur_map[vid] = _parse_iso_duration(iso)
        # Retorna na mesma ordem
        return [dur_map.get(vid, "?") for vid in video_ids]
    except Exception:
        return ["?"] * len(video_ids)


def _validate_cookies() -> bool:
    """Verifica se os cookies existem e não estão obviamente expirados."""
    if not os.path.exists(COOKIES_FILE):
        print(f"❌ cookies.txt não encontrado: {COOKIES_FILE}")
        return False

    # Verifica se o arquivo não está vazio e tem formato Netscape
    try:
        with open(COOKIES_FILE, "r") as f:
            content = f.read()
        if not content.strip():
            print(f"❌ cookies.txt está vazio!")
            return False
        if "# Netscape HTTP Cookie File" not in content:
            print(f"⚠️ cookies.txt não parece formato Netscape válido")
        # Verifica se tem cookies do YouTube
        if "youtube.com" not in content:
            print(f"⚠️ cookies.txt não contém cookies do YouTube")
            return False
        print(f"✅ Cookies validados: {COOKIES_FILE}")
        return True
    except Exception as e:
        print(f"❌ Erro lendo cookies: {e}")
        return False


def _find_downloaded_file(output_path: str) -> str:
    """Encontra o arquivo baixado pelo yt-dlp."""
    # 1. Procura extensões conhecidas
    for ext in ["opus", "m4a", "webm", "mp3", "ogg", "wav", "aac"]:
        candidate = output_path + f".{ext}"
        if os.path.exists(candidate) and os.path.getsize(candidate) > 0:
            return candidate

    # 2. Procura qualquer arquivo que comece com o prefixo
    dir_name = os.path.dirname(output_path)
    base_name = os.path.basename(output_path)
    for f in os.listdir(dir_name):
        if f.startswith(base_name):
            full = os.path.join(dir_name, f)
            if os.path.getsize(full) > 0:
                return full

    return ""


def download_audio(video_id: str, output_path: str) -> str:
    """Baixa áudio do YouTube com validação de cookies e retry."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    _ensure_cookies()  # Restaura cookies se foram sobrescritos
    has_cookies = _validate_cookies()

    # Formatos em ordem de preferência (opus é mais leve para VPS)
    formats = [
        "bestaudio[ext=opus]/bestaudio[ext=m4a]/bestaudio/best",
        "bestaudio/best",
        "worstaudio",
    ]

    last_error = None
    for fmt in formats:
        opts = _get_opts({
            "outtmpl": output_path + ".%(ext)s",
            "format": fmt,
            "concurrent_fragment_downloads": 2,
            "fragment_retries": 5,
            "retries": 3,
            "socket_timeout": 15,
        })
        opts["ffmpeg_location"] = "/usr/bin"

        print(f"📥 Baixando: {url} (formato: {fmt})")
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except Exception as e:
            last_error = str(e)
            print(f"⚠️ Download falhou (formato {fmt}): {e}")
            continue

        # Verifica se o arquivo foi baixado e não está vazio
        found = _find_downloaded_file(output_path)
        if found:
            size = os.path.getsize(found)
            print(f"✅ Download completo: {found} ({size} bytes)")
            return found
        else:
            print(f"⚠️ Arquivo vazio ou não encontrado após download com formato {fmt}")

    # Se chegou aqui, todos os formatos falharam
    if not has_cookies:
        raise ValueError(
            "❌ Download falhou: cookies.txt ausente ou inválido!\n"
            "Sem cookies válidos do YouTube, a VPS é bloqueada.\n"
            "Gere novos cookies: https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies"
        )
    raise ValueError(f"❌ Download falhou após todas as tentativas. Último erro: {last_error}")


def _format_duration(seconds) -> str:
    if not seconds:
        return "?"
    minutes = int(seconds) // 60
    secs = int(seconds) % 60
    return f"{minutes}:{secs:02d}"