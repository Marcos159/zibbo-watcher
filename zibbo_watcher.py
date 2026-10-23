#!/usr/bin/env python3
"""
zibbo_watcher.py — Avisa en un canal de Discord cuando alguien empieza a emitir
o sube un vídeo de un juego en Twitch o YouTube.

Uso:
    python zibbo_watcher.py          # bucle continuo (servidor, Raspberry, VPS...)
    python zibbo_watcher.py --once   # una sola pasada (cron / GitHub Actions)

Toda la configuración va por variables de entorno (ver .env.example).
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

log = logging.getLogger("zibbo_watcher")


# ── Configuración ─────────────────────────────────────────────────────────────

def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


DISCORD_WEBHOOK_URL = _env("DISCORD_WEBHOOK_URL")
DISCORD_MENTION = _env("DISCORD_MENTION")  # p.ej. "<@&123456789>" para mencionar un rol
DISCORD_USERNAME = _env("DISCORD_USERNAME", "Zibbo Watcher")
DISCORD_AVATAR_URL = _env("DISCORD_AVATAR_URL")

TWITCH_CLIENT_ID = _env("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = _env("TWITCH_CLIENT_SECRET")
TWITCH_GAME_NAME = _env("TWITCH_GAME_NAME", "The Zibbo Show")
TWITCH_GAME_ID = _env("TWITCH_GAME_ID")  # opcional: evita buscar la categoría por nombre

YOUTUBE_API_KEY = _env("YOUTUBE_API_KEY")
YOUTUBE_QUERY = _env("YOUTUBE_QUERY", '"Zibbo Show"')
# Palabras que deben aparecer en título o descripción (separadas por comas, basta una)
YOUTUBE_MUST_CONTAIN = _env("YOUTUBE_MUST_CONTAIN", "zibbo")
YOUTUBE_LOOKBACK_HOURS = int(_env("YOUTUBE_LOOKBACK_HOURS", "48"))

TWITCH_INTERVAL = int(_env("TWITCH_INTERVAL_SECONDS", "120"))
YOUTUBE_INTERVAL = int(_env("YOUTUBE_INTERVAL_SECONDS", "1200"))

STATE_FILE = Path(_env("STATE_FILE", "state.json"))
ANNOUNCE_EXISTING_ON_FIRST_RUN = _env("ANNOUNCE_EXISTING_ON_FIRST_RUN", "false").lower() == "true"

SEEN_TTL_DAYS = 14
HTTP_TIMEOUT = 15

TWITCH_COLOR = 0x9146FF
YOUTUBE_COLOR = 0xFF0000


# ── Estado persistente (qué ya hemos anunciado) ───────────────────────────────

class State:
    """Guarda los IDs ya anunciados para no repetir avisos entre ejecuciones."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {"twitch": {}, "youtube": {}, "seeded": {}, "last_check": {}}
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError) as e:
                log.warning("No se pudo leer %s (%s); empiezo de cero", path, e)

    def seen(self, source: str, item_id: str) -> bool:
        return item_id in self.data[source]

    def mark(self, source: str, item_id: str) -> None:
        self.data[source][item_id] = time.time()

    def is_seeded(self, source: str) -> bool:
        return bool(self.data["seeded"].get(source))

    def set_seeded(self, source: str) -> None:
        self.data["seeded"][source] = True

    def last_check(self, source: str) -> float:
        return float(self.data["last_check"].get(source, 0))

    def set_last_check(self, source: str) -> None:
        self.data["last_check"][source] = time.time()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - SEEN_TTL_DAYS * 86400
        for source in ("twitch", "youtube"):
            self.data[source] = {k: v for k, v in self.data[source].items() if v >= cutoff}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        tmp.replace(self.path)  # escritura atómica


# ── Discord ───────────────────────────────────────────────────────────────────

def post_to_discord(embed: dict) -> None:
    payload: dict = {
        "username": DISCORD_USERNAME,
        "embeds": [embed],
        "allowed_mentions": {"parse": ["roles", "users", "everyone"] if DISCORD_MENTION else []},
    }
    if DISCORD_AVATAR_URL:
        payload["avatar_url"] = DISCORD_AVATAR_URL
    if DISCORD_MENTION:
        payload["content"] = DISCORD_MENTION

    for _ in range(5):
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=HTTP_TIMEOUT)
        if r.status_code == 429:
            retry_after = float(r.json().get("retry_after", 1))
            log.warning("Discord rate limit, espero %.1fs", retry_after)
            time.sleep(retry_after + 0.5)
            continue
        r.raise_for_status()
        time.sleep(1)  # margen para no encadenar varios avisos de golpe
        return
    raise RuntimeError("Discord sigue devolviendo 429 tras 5 intentos")


def _trim(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ── Twitch ────────────────────────────────────────────────────────────────────

class Twitch:
    API = "https://api.twitch.tv/helix"

    def __init__(self) -> None:
        self.token: str | None = None
        self.token_expiry = 0.0
        self.game_id: str | None = TWITCH_GAME_ID or None

    def _auth(self) -> None:
        r = requests.post(
            "https://id.twitch.tv/oauth2/token",
            data={
                "client_id": TWITCH_CLIENT_ID,
                "client_secret": TWITCH_CLIENT_SECRET,
                "grant_type": "client_credentials",
            },
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        d = r.json()
        self.token = d["access_token"]
        self.token_expiry = time.time() + d.get("expires_in", 3600) - 300

    def _get(self, path: str, params: dict) -> dict:
        if not self.token or time.time() > self.token_expiry:
            self._auth()
        for attempt in range(2):
            r = requests.get(
                f"{self.API}/{path}",
                params=params,
                headers={"Client-Id": TWITCH_CLIENT_ID, "Authorization": f"Bearer {self.token}"},
                timeout=HTTP_TIMEOUT,
            )
            if r.status_code == 401 and attempt == 0:
                self._auth()  # token revocado/caducado antes de tiempo
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError("Twitch devolvió 401 incluso tras renovar el token")

    def resolve_game_id(self) -> str:
        if self.game_id:
            return self.game_id
        d = self._get("games", {"name": TWITCH_GAME_NAME})
        if not d.get("data"):
            raise RuntimeError(
                f"Twitch no tiene ninguna categoría llamada '{TWITCH_GAME_NAME}'. "
                "Las categorías salen de IGDB: si el juego no está allí, hay que darlo de alta. "
                "También puedes fijar TWITCH_GAME_ID a mano."
            )
        self.game_id = d["data"][0]["id"]
        log.info("Categoría de Twitch '%s' → game_id=%s", TWITCH_GAME_NAME, self.game_id)
        return self.game_id

    def live_streams(self) -> list[dict]:
        game_id = self.resolve_game_id()
        streams: list[dict] = []
        cursor = None
        while True:
            params = {"game_id": game_id, "type": "live", "first": 100}
            if cursor:
                params["after"] = cursor
            d = self._get("streams", params)
            streams.extend(d.get("data", []))
            cursor = d.get("pagination", {}).get("cursor")
            if not cursor or not d.get("data"):
                return streams


def twitch_embed(s: dict) -> dict:
    thumb = s["thumbnail_url"].replace("{width}", "1280").replace("{height}", "720")
    return {
        "title": _trim(s.get("title") or f"{s['user_name']} está en directo", 256),
        "url": f"https://www.twitch.tv/{s['user_login']}",
        "description": f"🔴 **{s['user_name']}** está jugando a **{s['game_name']}** en Twitch",
        "color": TWITCH_COLOR,
        "image": {"url": f"{thumb}?t={int(time.time())}"},  # evita la miniatura cacheada
        "fields": [
            {"name": "Espectadores", "value": str(s.get("viewer_count", 0)), "inline": True},
            {"name": "Idioma", "value": (s.get("language") or "?").upper(), "inline": True},
        ],
        "footer": {"text": "Twitch"},
        "timestamp": s["started_at"],
    }


def check_twitch(twitch: Twitch, state: State) -> None:
    streams = twitch.live_streams()
    first_run = not state.is_seeded("twitch")
    announced = 0
    try:
        for s in streams:
            if state.seen("twitch", s["id"]):
                continue
            if not (first_run and not ANNOUNCE_EXISTING_ON_FIRST_RUN):
                post_to_discord(twitch_embed(s))
                announced += 1
            state.mark("twitch", s["id"])
        state.set_seeded("twitch")
    finally:
        state.save()
    log.info("Twitch: %d en directo, %d anunciados", len(streams), announced)


# ── YouTube ───────────────────────────────────────────────────────────────────

def youtube_search() -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(hours=YOUTUBE_LOOKBACK_HOURS)
    r = requests.get(
        "https://www.googleapis.com/youtube/v3/search",
        params={
            "part": "snippet",
            "q": YOUTUBE_QUERY,
            "type": "video",
            "order": "date",
            "maxResults": 50,
            "publishedAfter": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "key": YOUTUBE_API_KEY,
        },
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code == 403 and "quota" in r.text.lower():
        raise RuntimeError("Cuota diaria de YouTube agotada; se reinicia a medianoche (hora del Pacífico)")
    r.raise_for_status()

    keywords = [k.strip().lower() for k in YOUTUBE_MUST_CONTAIN.split(",") if k.strip()]
    results = []
    for item in r.json().get("items", []):
        sn = item["snippet"]
        text = html.unescape(f"{sn.get('title', '')} {sn.get('description', '')}").lower()
        if keywords and not any(k in text for k in keywords):
            continue  # la búsqueda de YouTube es difusa; filtramos falsos positivos
        results.append(item)
    return results


def youtube_embed(item: dict) -> dict:
    sn = item["snippet"]
    video_id = item["id"]["videoId"]
    live = sn.get("liveBroadcastContent") == "live"
    channel = html.unescape(sn.get("channelTitle", "Alguien"))
    thumbs = sn.get("thumbnails", {})
    thumb = (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url")

    embed = {
        "title": _trim(html.unescape(sn.get("title", "Nuevo vídeo")), 256),
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "description": (
            f"🔴 **{channel}** está en directo en YouTube"
            if live
            else f"🎬 **{channel}** ha subido un vídeo"
        ),
        "color": YOUTUBE_COLOR,
        "footer": {"text": "YouTube · Directo" if live else "YouTube"},
        "timestamp": sn.get("publishedAt"),
    }
    if thumb:
        embed["image"] = {"url": thumb}
    return embed


def check_youtube(state: State) -> None:
    # Cada búsqueda cuesta 100 unidades de cuota: se respeta el intervalo aunque el
    # script se lance más a menudo (cron de GitHub, disparador externo, a mano...).
    # Margen de 60 s para absorber la imprecisión de los disparadores.
    elapsed = time.time() - state.last_check("youtube")
    if elapsed < YOUTUBE_INTERVAL - 60:
        log.info("YouTube: próxima búsqueda en %d min", (YOUTUBE_INTERVAL - elapsed) // 60 + 1)
        return

    state.set_last_check("youtube")  # se cuenta el intento aunque falle: la cuota ya se gastó
    first_run = not state.is_seeded("youtube")
    announced = 0
    items: list[dict] = []
    try:
        items = youtube_search()
        for item in items:
            video_id = item["id"]["videoId"]
            if state.seen("youtube", video_id):
                continue
            # Directos programados: no se marcan, así se anuncian cuando pasen a "live"
            if item["snippet"].get("liveBroadcastContent") == "upcoming":
                continue
            if not (first_run and not ANNOUNCE_EXISTING_ON_FIRST_RUN):
                post_to_discord(youtube_embed(item))
                announced += 1
            state.mark("youtube", video_id)
        state.set_seeded("youtube")
    finally:
        state.save()
    log.info("YouTube: %d resultados relevantes, %d anunciados", len(items), announced)


# ── Main ──────────────────────────────────────────────────────────────────────

def run_safely(name: str, fn) -> bool:
    try:
        fn()
        return True
    except Exception as e:  # una fuente caída no debe tumbar la otra
        log.error("%s: %s", name, e)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true", help="hace una sola pasada y termina")
    parser.add_argument(
        "--only",
        choices=["twitch", "youtube"],
        help="consulta solo esa plataforma (útil para programar cada una con distinta frecuencia)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not DISCORD_WEBHOOK_URL:
        log.error("Falta DISCORD_WEBHOOK_URL")
        return 2

    state = State(STATE_FILE)
    sources: list[tuple[str, object, int]] = []

    want_twitch = args.only in (None, "twitch")
    want_youtube = args.only in (None, "youtube")

    if want_twitch:
        if TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET:
            twitch = Twitch()
            sources.append(("Twitch", lambda: check_twitch(twitch, state), TWITCH_INTERVAL))
        else:
            log.warning("Sin credenciales de Twitch: Twitch desactivado")

    if want_youtube:
        if YOUTUBE_API_KEY:
            sources.append(("YouTube", lambda: check_youtube(state), YOUTUBE_INTERVAL))
        else:
            log.warning("Sin YOUTUBE_API_KEY: YouTube desactivado")

    if not sources:
        log.error("No hay ninguna fuente configurada")
        return 2

    if args.once:
        ok = all([run_safely(name, fn) for name, fn, _ in sources])
        return 0 if ok else 1

    log.info("Vigilando: %s", ", ".join(f"{n} cada {i}s" for n, _, i in sources))
    next_run = {name: 0.0 for name, _, _ in sources}
    try:
        while True:
            now = time.time()
            for name, fn, interval in sources:
                if now >= next_run[name]:
                    run_safely(name, fn)
                    next_run[name] = now + interval
            time.sleep(5)
    except KeyboardInterrupt:
        log.info("Parado")
        return 0


if __name__ == "__main__":
    sys.exit(main())
