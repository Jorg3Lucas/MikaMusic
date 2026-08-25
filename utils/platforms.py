import re

# Platform detection patterns
PLATFORMS = {
    "spotify": {
        "name": "Spotify",
        "emoji": "💚",
        "patterns": [
            r"open\.spotify\.com/(?:[a-z-]+/)?(?:track|album|playlist|artist)/",
        ],
    },
    "deezer": {
        "name": "Deezer",
        "emoji": "💙",
        "patterns": [
            r"(?:www\.)?deezer\.com/(?:[a-z]{2}/)?(?:track|album|playlist|artist)/\d+",
            r"link\.deezer\.com/s/",
            r"deezer\.page\.link/",
        ],
    },
    "apple_music": {
        "name": "Apple Music",
        "emoji": "🍎",
        "patterns": [r"music\.apple\.com/"],
    },
    "soundcloud": {
        "name": "SoundCloud",
        "emoji": "🟠",
        "patterns": [r"soundcloud\.com/[^/]+/[^/]+"],
        "yt_dlp": True,  # yt-dlp handles directly
    },
    "bandcamp": {
        "name": "Bandcamp",
        "emoji": "🏕️",
        "patterns": [r"[^.]+\.bandcamp\.com/track/[^?]+"],
        "yt_dlp": True,  # yt-dlp handles directly
    },
    "tiktok": {
        "name": "TikTok",
        "emoji": "📱",
        "patterns": [r"tiktok\.com/.+/video/\d+"],
        "yt_dlp": True,  # yt-dlp handles directly
    },
    "youtube": {
        "name": "YouTube",
        "emoji": "▶️",
        "patterns": [
            r"youtube\.com/watch\?v=",
            r"youtu\.be/",
            r"youtube\.com/shorts/",
        ],
        "yt_dlp": True,
    },
}

# Domínios com DRM que NUNCA devem chegar ao yt-dlp
DRM_DOMAINS = ["spotify.com", "deezer.com", "deezer.fr", "music.apple.com"]


def detect_platform(url: str) -> dict | None:
    """Detect which platform a URL belongs to."""
    for key, platform in PLATFORMS.items():
        for pattern in platform["patterns"]:
            if re.search(pattern, url, re.IGNORECASE):
                return {"key": key, **platform}
    return None


def is_ytdlp_platform(url: str) -> bool:
    """Check if this platform is handled directly by yt-dlp."""
    platform = detect_platform(url)
    return platform is not None and platform.get("yt_dlp", False)


def _extract_title_from_html(html: str) -> str:
    """Extrai título da música da página HTML do embed Spotify."""
    import re
    import json as _json

    # 1. Procura por <title> tag (Spotify colocou "Spotify Embed: Track Name")
    m = re.search(r'<title>Spotify Embed:\s*([^<]+)</title>', html)
    if m:
        return m.group(1).strip()

    # 2. Procura por "name":"..." no JSON embutido
    m = re.search(r'"name":"([^"]+)"', html)
    if m:
        return m.group(1)

    return ""


def _extract_artist_from_html(html: str) -> str:
    """Extrai nome do artista da página HTML do embed Spotify."""
    import re

    # Procura por "artist":"..." ou "artists":[{"name":"..."}]
    m = re.search(r'"artist":"([^"]+)"', html)
    if m:
        return m.group(1)

    m = re.search(r'"artists":\[\{"name":"([^"]+)"', html)
    if m:
        return m.group(1)

    return ""


def _http_get_sync(url: str, headers: dict = None, timeout: int = 10) -> str:
    """GET síncrono via urllib (mais confiável que aiohttp em VPS)."""
    import urllib.request
    import ssl

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read().decode("utf-8")


def get_spotify_track(url: str) -> str:
    """Extract track name from Spotify URL."""
    import json as _json

    # Detecta tipo de URL
    is_album = "/album/" in url
    is_playlist = "/playlist/" in url
    is_artist = "/artist/" in url

    # 1. Tenta oEmbed via urllib (mais confiável que aiohttp em VPS)
    try:
        html = _http_get_sync(
            f"https://open.spotify.com/oembed?url={url}",
            timeout=10,
        )
        data = _json.loads(html)
        title = data.get("title", "")
        if title:
            return title
    except Exception:
        pass

    # 2. Tenta scrape do embed page (só funciona para tracks)
    match = re.search(r"/(?:[a-z-]+/)?(?:track|album|playlist|artist)/([a-zA-Z0-9]+)", url)
    if match:
        spotify_id = match.group(1)
        try:
            embed_url = f"https://open.spotify.com/embed/track/{spotify_id}"
            html = _http_get_sync(embed_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            title = _extract_title_from_html(html)
            artist = _extract_artist_from_html(html)
            if title and artist:
                return f"{title} {artist}"
            if title:
                return title
        except Exception:
            pass

        # 3. Mensagem útil para álbuns/playlists/artists
        if is_album:
            raise ValueError(
                "❌ Não consegui extrair música do álbum.\n"
                "Cole o link de uma música específica que eu toco!"
            )
        if is_playlist:
            raise ValueError(
                "❌ Não consegui extrair da playlist.\n"
                "Cole o nome de uma música que eu busco!"
            )
        if is_artist:
            raise ValueError(
                "❌ Não consegui extrair do link do artista.\n"
                "Cole o nome da música que eu busco!"
            )

        # 4. Último recurso
        return f"spotify {spotify_id}"

    raise ValueError("Não foi possível extrair informação do Spotify.")


def _resolve_deezer_short_url(url: str) -> str:
    """Resolve link.deezer.com/s/... e deezer.page.link/... para URL real."""
    if "link.deezer.com" not in url and "deezer.page.link" not in url:
        return url
    try:
        html = _http_get_sync(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        # Procura por URL de track/album/playlist no HTML
        m = re.search(r"(?:www\.)?deezer\.com/[a-z]{2}/(track|album|playlist)/(\d+)", html)
        if m:
            return f"https://www.deezer.com/{m.group(1)}/{m.group(2)}"
        # Tenta sem country code
        m = re.search(r"(?:www\.)?deezer\.com/(track|album|playlist)/(\d+)", html)
        if m:
            return f"https://www.deezer.com/{m.group(1)}/{m.group(2)}"
    except Exception:
        pass
    return url


def get_deezer_track(url: str) -> str:
    """Extract track name from Deezer URL (including shortened links)."""
    import json as _json

    # Resolve link encurtado se necessário
    url = _resolve_deezer_short_url(url)

    match = re.search(r"(?:track|album|playlist)/(\d+)", url)
    if not match:
        raise ValueError("URL inválida do Deezer.")

    track_id = match.group(1)
    try:
        html = _http_get_sync(f"https://api.deezer.com/track/{track_id}", timeout=10)
        data = _json.loads(html)
        title = data.get("title", "")
        artist = data.get("artist", {}).get("name", "")
        if title:
            return f"{title} {artist}".strip()
    except Exception:
        pass

    raise ValueError("Não foi possível extrair informação do Deezer.")


def get_apple_music_track(url: str) -> str:
    """Extract track name from Apple Music URL."""
    import json as _json

    # 1. Tenta oEmbed
    try:
        html = _http_get_sync(
            f"https://itunes.apple.com/oembed?url={url}&type=song",
            timeout=10,
        )
        data = _json.loads(html)
        title = data.get("trackName", "")
        artist = data.get("artistName", "")
        if title:
            return f"{title} {artist}".strip()
    except Exception:
        pass

    # 2. Fallback: extrai da URL
    # Formato: /us/album/blinding-lights/1499378108?i=1499378109
    match = re.search(r"music\.apple\.com/[a-z]{2}/[^/]+/([^/]+)/\d+", url)
    if match:
        name = match.group(1).replace("-", " ").strip()
        return name

    raise ValueError("Não foi possível extrair informação do Apple Music.")


def is_drm_url(url: str) -> bool:
    """Verifica se a URL é de uma plataforma com DRM (Spotify, Deezer, etc)."""
    url_lower = url.lower()
    return any(domain in url_lower for domain in DRM_DOMAINS)


async def resolve_url_to_query(url: str) -> str:
    """Convert a platform URL to a search query for YouTube."""
    platform = detect_platform(url)
    if not platform:
        return url  # Already a search query

    key = platform["key"]

    if key == "spotify":
        return get_spotify_track(url)
    elif key == "deezer":
        return get_deezer_track(url)
    elif key == "apple_music":
        return get_apple_music_track(url)
    else:
        return url  # SoundCloud, Bandcamp, TikTok, YouTube — yt-dlp handles directly
