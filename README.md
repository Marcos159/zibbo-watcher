# Zibbo Watcher

Avisa en un canal de Discord cuando alguien empieza a emitir **The Zibbo Show** en Twitch,
o sube un vídeo / empieza un directo del juego en YouTube.

## Puesta en marcha

1. **Webhook de Discord**: en el canal → *Editar canal → Integraciones → Webhooks → Nuevo webhook* → copiar URL.
2. **Twitch**: <https://dev.twitch.tv/console> → *Register Your Application* (OAuth Redirect `http://localhost`,
   tipo *Confidential*) → copiar *Client ID* y generar *Client Secret*.
3. **YouTube**: Google Cloud Console → proyecto nuevo → habilitar *YouTube Data API v3* →
   *Credenciales → Crear clave de API* (restríngela a esa API).
4. Configurar y lanzar:

```bash
pip install -r requirements.txt
cp .env.example .env      # rellena las claves
python zibbo_watcher.py   # bucle continuo
```

La primera ejecución solo registra lo que ya está en directo/publicado, sin anunciarlo,
para no inundar el canal. A partir de ahí, solo avisa de lo nuevo.

## Dónde ejecutarlo

### Opción A — GitHub Actions (sin servidor, gratis en repo público)

El workflow `.github/workflows/zibbo-watcher.yml` ejecuta el script con `--once`:
Twitch cada 10 min y YouTube a los :05 y :35 de cada hora. El estado (`state.json`)
se guarda haciendo commit en el propio repo.

1. Crea un repo **público** en GitHub y sube esta carpeta tal cual (incluida `.github/`).
   No subas el `.env`; ya está en `.gitignore`.
2. *Settings → Secrets and variables → Actions*:
   - **Secrets**: `DISCORD_WEBHOOK_URL`, `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `YOUTUBE_API_KEY`
   - **Variables** (opcionales): `DISCORD_MENTION`, `TWITCH_GAME_ID`
3. Pestaña *Actions* → *Zibbo Watcher* → *Run workflow* para la primera pasada (registra lo que
   ya existe sin anunciarlo). A partir de ahí corre solo.

Notas:
- Los secretos no son visibles aunque el repo sea público; lo único público es `state.json`
  (IDs de directos y vídeos ya anunciados).
- Los cron de GitHub no son puntuales: con carga pueden retrasarse bastantes minutos o saltarse
  alguna ejecución. Para avisos de directos es asumible.
- GitHub desactiva los cron de repos públicos tras 60 días sin actividad. El workflow hace un
  commit vacío de *keepalive* si pasan 50 días sin cambios.
- En un repo privado también funciona, pero consume la cuota gratuita de minutos del plan
  (cada ejecución cuenta como mínimo un minuto, y son ~190 al día).

### Opción B — Máquina siempre encendida

`python zibbo_watcher.py` en bucle continuo (VPS, Raspberry…), o `--once` desde un cron propio.
Con `--only twitch` / `--only youtube` puedes programar cada plataforma con su propia frecuencia.

## Cómo funciona y límites

**Twitch** — Consulta `Get Streams?game_id=…` (polling). EventSub no sirve aquí: solo permite
suscribirse a `stream.online` de *canales concretos*, no de una categoría.
- La categoría tiene que existir en Twitch, y Twitch las saca de **IGDB**. Si el script dice que no
  la encuentra, da de alta el juego en IGDB.
- Si alguien cambia a la categoría a mitad de directo, se anuncia en ese momento.
- Cada directo tiene un ID propio, así que si un streamer corta y vuelve a emitir se anuncia de nuevo.

**YouTube** — Usa `search.list` filtrando por texto (no existe una búsqueda “por juego” en la API).
- Cuota: 10.000 unidades/día y cada búsqueda cuesta 100 → como mucho ~100 búsquedas al día.
  Con 20 min de intervalo gastas ~7.200.
- El índice de búsqueda de YouTube tarda a veces horas en mostrar vídeos nuevos; por eso se mira
  una ventana de 48 h y se deduplica por ID.
- Solo detecta vídeos que mencionen “zibbo” en título o descripción. Si alguien sube un vídeo sin
  nombrar el juego, no hay forma fiable de pillarlo.
- Los directos programados (`upcoming`) se ignoran hasta que pasan a directo.
