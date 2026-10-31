"""Vigila la salud de las cámaras y avisa por Telegram cuando se caen o vuelven."""
import logging
import os
import socket
import threading
import time

import requests

log = logging.getLogger("uvicorn.error")

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
VIGILAR_CADA = float(os.environ.get("VIGILAR_CADA", "10"))
CAIDA_TRAS = float(os.environ.get("CAIDA_TRAS", "45"))
TG = f"https://api.telegram.org/bot{TOKEN}"
MAX_COLA = 50

_candado = threading.Lock()
_salud = {}
_bytes = {}
_pendientes = []
_go2rtc = {"ok": True, "inicio_fallo": None, "avisado": False}
_obtener_camaras = lambda: []
_go2rtc_url = ""
_eventos = ""

EMOJI = {"ok": "🟢", "sin_video": "🟡", "sin_red": "🔴"}
LEGIBLE = {"ok": "en línea", "sin_video": "sin video", "sin_red": "sin red"}
MOTIVO = {
    "sin_red": "No responde en la red: revisa Wi-Fi, señal o corriente de la cámara.",
    "sin_video": "Responde en la red pero no envía video: el flujo RTSP se cortó (go2rtc reintenta solo).",
}


def duracion(seg):
    seg = int(seg)
    if seg < 60:
        return f"{seg} s"
    if seg < 3600:
        return f"{seg // 60} min"
    return f"{seg // 3600} h {seg % 3600 // 60} min"


def _evento(texto):
    try:
        with open(_eventos, "a", encoding="utf-8") as f:
            f.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} {texto}\n')
    except OSError:
        pass


# ------------------------------ Telegram ------------------------------
def avisar(texto):
    log.warning("aviso: %s", texto.replace("\n", " "))
    with _candado:
        _pendientes.append(texto)
        del _pendientes[:-MAX_COLA]


def _enviar(texto):
    r = requests.post(f"{TG}/sendMessage", json={"chat_id": CHAT, "text": texto}, timeout=10)
    return r.ok


def _vaciar_cola():
    """Envía en orden; si falla (p. ej. sin internet) deja el resto para la siguiente vuelta."""
    if not (TOKEN and CHAT):
        with _candado:
            _pendientes.clear()
        return
    while True:
        with _candado:
            if not _pendientes:
                return
            texto = _pendientes[0]
        try:
            if not _enviar(texto):
                return
        except requests.RequestException:
            return
        with _candado:
            if _pendientes and _pendientes[0] is texto:
                _pendientes.pop(0)


# ------------------------------ revisión ------------------------------
def _red(ip, puerto):
    t0 = time.monotonic()
    try:
        socket.create_connection((ip, puerto), timeout=3).close()
    except OSError:
        return None
    return int((time.monotonic() - t0) * 1000)


def _bytes_recibidos(info):
    total = 0
    for p in info.get("producers") or []:
        total += p.get("bytes_recv") or sum(r.get("bytes", 0) for r in p.get("receivers") or [])
    return total


def _revisar_go2rtc():
    try:
        return requests.get(f"{_go2rtc_url}/api/streams", timeout=3).json()
    except (requests.RequestException, ValueError):
        pass
    ahora = time.time()
    if _go2rtc["ok"]:
        _go2rtc.update(ok=False, inicio_fallo=ahora)
        _evento("go2rtc ok -> caido")
    if not _go2rtc["avisado"] and ahora - _go2rtc["inicio_fallo"] >= CAIDA_TRAS:
        _go2rtc["avisado"] = True
        avisar("🔴 go2rtc no responde (el servidor de video está caído)")
    return None


def _revisar():
    streams = _revisar_go2rtc()
    if streams is None:
        return
    if not _go2rtc["ok"]:
        _evento("go2rtc caido -> ok")
        if _go2rtc["avisado"]:
            avisar("🟢 go2rtc volvió a responder")
        _go2rtc.update(ok=True, inicio_fallo=None, avisado=False)

    camaras = _obtener_camaras()
    with _candado:
        for cid in set(_salud) - {c["id"] for c in camaras}:
            del _salud[cid]
            _bytes.pop(cid, None)

    for c in camaras:
        cid = c["id"]
        lat = _red(c["ip"], c["puerto_rtsp"])
        b = _bytes_recibidos(streams.get(cid) or {})
        hay_video = b > _bytes.get(cid, -1)
        _bytes[cid] = b
        nuevo = "ok" if hay_video else ("sin_red" if lat is None else "sin_video")
        ahora = time.time()

        with _candado:
            s = _salud.setdefault(cid, {"estado": "ok", "desde": ahora, "avisado": False,
                                        "cortes": [], "inicio_fallo": None})
            s.update(nombre=c["nombre"], ip=c["ip"], latencia_ms=lat)
            anterior = s["estado"]
            if nuevo != anterior:
                s.update(estado=nuevo, desde=ahora)
                _evento(f'{cid} {anterior} -> {nuevo} (latencia {lat if lat is not None else "-"})')
            if nuevo == "ok":
                if s["avisado"]:
                    texto = f'🟢 {c["nombre"]} volvió tras {duracion(ahora - s["inicio_fallo"])}'
                else:
                    texto = None
                s.update(avisado=False, inicio_fallo=None)
            else:
                texto = None
                if s["inicio_fallo"] is None:
                    s["inicio_fallo"] = ahora
                if not s["avisado"] and ahora - s["inicio_fallo"] >= CAIDA_TRAS:
                    s["avisado"] = True
                    s["cortes"].append(ahora)
                    texto = f'🔴 {c["nombre"]} ({c["ip"]}) sin video.\n{MOTIVO[nuevo]}'
        if texto:
            avisar(texto)


def _bucle():
    ultima = time.time()
    while True:
        ahora = time.time()
        if ahora - ultima > VIGILAR_CADA * 3 + 30:
            avisar(f"⚠️ El servidor estuvo suspendido unos {duracion(ahora - ultima)} "
                   "(¿el equipo se durmió?). Las cámaras no se vigilaron en ese tiempo.")
            _evento(f"servidor suspendido {duracion(ahora - ultima)}")
        ultima = ahora
        try:
            _revisar()
            _vaciar_cola()
        except Exception:
            log.exception("vigilante: fallo en la revisión")
        time.sleep(VIGILAR_CADA)


# ------------------------------ bot ------------------------------
def _cortes_24h(s):
    limite = time.time() - 86400
    s["cortes"] = [t for t in s["cortes"] if t > limite]
    return len(s["cortes"])


def _texto_estado():
    lineas = [f'go2rtc: {"ok" if _go2rtc["ok"] else "caído"}']
    ahora = time.time()
    with _candado:
        for s in _salud.values():
            lat = s["latencia_ms"] if s["latencia_ms"] is not None else "-"
            lineas.append(f'{EMOJI[s["estado"]]} {s["nombre"]} ({s["ip"]}) — {LEGIBLE[s["estado"]]} '
                          f'desde hace {duracion(ahora - s["desde"])}, {lat} ms, {_cortes_24h(s)} cortes en 24 h')
    if len(lineas) == 1:
        lineas.append("Aún no hay datos de cámaras.")
    return "\n".join(lineas)


def _foto(arg):
    camaras = [c for c in _obtener_camaras() if not arg or c["id"] == arg or c["nombre"].lower() == arg.lower()]
    if not camaras:
        _enviar(f"No existe la cámara {arg}")
    for c in camaras:
        try:
            r = requests.get(f"{_go2rtc_url}/api/frame.jpeg", params={"src": c["id"]}, timeout=15)
            r.raise_for_status()
            requests.post(f"{TG}/sendPhoto", data={"chat_id": CHAT, "caption": c["nombre"]},
                          files={"photo": ("foto.jpg", r.content, "image/jpeg")}, timeout=30)
        except requests.RequestException as e:
            _enviar(f'No pude sacar foto de {c["nombre"]}: {e}')


def _texto_eventos():
    try:
        with open(_eventos, encoding="utf-8") as f:
            ultimas = f.readlines()[-15:]
    except OSError:
        ultimas = []
    return "".join(ultimas) or "Sin eventos"


AYUDA = ("Comandos:\n/estado — salud de cada cámara\n/foto [id] — foto actual (todas o una)\n"
         "/eventos — últimos cambios registrados\n/ayuda — esta lista")


def _atender(texto):
    partes = texto.split()
    cmd = partes[0].split("@")[0].lower()
    arg = partes[1] if len(partes) > 1 else ""
    if cmd == "/estado":
        _enviar(_texto_estado())
    elif cmd == "/foto":
        _foto(arg)
    elif cmd == "/eventos":
        _enviar(_texto_eventos())
    elif cmd in ("/ayuda", "/start"):
        _enviar(AYUDA)


def _bot():
    offset = 0
    while True:
        try:
            r = requests.get(f"{TG}/getUpdates", params={"timeout": 25, "offset": offset}, timeout=35)
            for u in r.json().get("result", []):
                offset = u["update_id"] + 1
                m = u.get("message") or {}
                texto = m.get("text", "")
                if str(m.get("chat", {}).get("id")) == CHAT and texto.startswith("/"):
                    _atender(texto)
        except Exception as e:
            log.warning("bot de Telegram: %s", e)
            time.sleep(5)


# ------------------------------ API pública ------------------------------
def iniciar(obtener_camaras, go2rtc_url, data_dir):
    """obtener_camaras() devuelve una lista de dicts con id, nombre, ip, puerto_rtsp."""
    global _obtener_camaras, _go2rtc_url, _eventos
    _obtener_camaras, _go2rtc_url = obtener_camaras, go2rtc_url
    _eventos = os.path.join(data_dir, "eventos.log")
    _evento("vigilante iniciado")
    avisar("▶️ Vigilante de cámaras iniciado")
    threading.Thread(target=_bucle, daemon=True).start()
    if TOKEN and CHAT:
        threading.Thread(target=_bot, daemon=True).start()
    else:
        log.warning("Sin TELEGRAM_TOKEN/TELEGRAM_CHAT_ID: no habrá avisos por Telegram")


def salud():
    """Devuelve {"go2rtc": bool, "camaras": {cid: {...}}} para la API /api/salud."""
    with _candado:
        return {"go2rtc": _go2rtc["ok"], "camaras": {
            cid: {"nombre": s["nombre"], "ip": s["ip"], "estado": s["estado"], "desde": s["desde"],
                  "latencia_ms": s["latencia_ms"], "cortes_24h": _cortes_24h(s)}
            for cid, s in _salud.items()}}
