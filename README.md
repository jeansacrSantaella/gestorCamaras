# gestorCamaras

## Avisos por Telegram

1. Copia `.env.example` a `.env` y pon `TELEGRAM_TOKEN` y `TELEGRAM_CHAT_ID`.
2. `docker compose up -d --build`

El panel revisa cada cámara cada 10 s (red TCP + que llegue video a go2rtc). Si una cámara pasa
45 s seguidos sin video avisa y dice si es **la red** (no responde: Wi-Fi/corriente) o **el flujo de video**
(responde pero no manda imagen). También avisa cuando vuelve, si go2rtc se cae y si el equipo estuvo suspendido.
Los avisos que no se pueden enviar (sin internet) se guardan y se mandan al volver.

Comandos del bot: `/estado`, `/foto [id]`, `/eventos`, `/ayuda`. Historial en `data/eventos.log`.
En el panel web, el punto de cada cámara es verde (ok), ámbar (sin video) o rojo (sin red).
