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

## Instalación en Windows

Requiere Docker Desktop con WSL 2 y que el equipo esté en la misma red que las cámaras (192.168.1.x).
Hay una guía completa en el PDF de esta carpeta.

```powershell
git clone -b develop https://github.com/jeansacrSantaella/gestorCamaras.git
cd gestorCamaras
copy .env.example .env     # edita .env con tu token y tu chat id de Telegram
docker compose up -d --build
```

La primera vez tarda varios minutos porque el detector descarga PyTorch y el modelo YOLO.
Abre `http://localhost:8080` (panel) y `http://localhost:1984` (go2rtc).
Para verlo desde el celular, permite el acceso en redes privadas en el firewall de Windows
(puertos 8080 y 1984) y entra a `http://IP_DE_LA_LAPTOP:8080`.

## Capturas por HTTP (solo lectura)

`GET /api/fotos/dias`, `GET /api/fotos?fecha=AAAA-MM-DD` y `GET /api/fotos/AAAA-MM-DD/<archivo>.jpg` exponen las
fotos que guarda el detector. Las usa panel-home para la línea de tiempo y la galería del día.
