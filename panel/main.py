import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
from urllib.parse import quote

import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from onvif import ONVIFCamera
from pydantic import BaseModel
from zeep.transports import Transport

log = logging.getLogger("uvicorn.error")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
ARCHIVO = os.path.join(DATA_DIR, "cameras.json")
GO2RTC = os.environ.get("GO2RTC_URL", "http://go2rtc:1984")
ADMIN_PIN = os.environ.get("ADMIN_PIN", "")
MAX_CAMARAS = int(os.environ.get("MAX_CAMARAS", "32"))
TIMEOUT = float(os.environ.get("ONVIF_TIMEOUT", "3"))
AUTOPARADA = 2.0
MAX_SESIONES = int(os.environ.get("MAX_SESIONES", "2"))
SESION_CADUCA = float(os.environ.get("SESION_CADUCA", "60"))
RUTA_OK = re.compile(r"^/[A-Za-z0-9_\-./?=&%]*$")

app = FastAPI()
_conexiones = {}
_temporizadores = {}
_estado = {}
_sesiones = {}
_expulsadas = {}
_fallidas = set()  # cámaras que go2rtc rechazó: no se reintentan en bucle
_candado = threading.Lock()
_cfg = threading.Lock()
_camaras = []


# ------------------------- almacenamiento (JSON) -------------------------
def _nueva(cid, nombre, ip, rtsp=554, ruta="/live/ch00_0", onvif=8899, usuario="", clave=""):
    return {"id": cid, "nombre": nombre, "ip": ip, "puerto_rtsp": rtsp, "ruta_rtsp": ruta,
            "puerto_onvif": onvif, "usuario": usuario, "clave": clave}


def _guardar():
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = ARCHIVO + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"camaras": _camaras}, f, indent=2, ensure_ascii=False)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, ARCHIVO)


def _cargar():
    global _camaras
    if os.path.exists(ARCHIVO):
        with open(ARCHIVO, encoding="utf-8") as f:
            _camaras = json.load(f).get("camaras", [])
        return
    # Primera vez: toma CAMARAS="camara1=ip,camara2=ip" para no perder lo que ya tenías.
    for par in os.environ.get("CAMARAS", "").split(","):
        if "=" in par:
            nombre, ip = (p.strip() for p in par.split("=", 1))
            cid = re.sub(r"[^a-z0-9_-]", "", nombre.lower()) or _nuevo_id()
            _camaras.append(_nueva(cid, nombre, ip))
    _guardar()


def _nuevo_id():
    usados = {c["id"] for c in _camaras}
    n = 1
    while f"camara{n}" in usados:
        n += 1
    return f"camara{n}"


def _publica(c):
    return {k: c[k] for k in ("id", "nombre", "ip", "puerto_rtsp", "ruta_rtsp", "puerto_onvif", "usuario")} \
        | {"tiene_clave": bool(c["clave"])}


def buscar(cid):
    with _cfg:
        for c in _camaras:
            if c["id"] == cid:
                return dict(c)
    raise HTTPException(404, f"No existe la cámara {cid}")


# ------------------------------ go2rtc ------------------------------
def fuente(c):
    cred = ""
    if c["usuario"]:
        cred = f'{quote(c["usuario"], safe="")}:{quote(c["clave"], safe="")}@'
    return f'ffmpeg:rtsp://{cred}{c["ip"]}:{c["puerto_rtsp"]}{c["ruta_rtsp"]}#video=h264#audio=aac'


def registrar_en_go2rtc(c):
    try:
        r = requests.put(f"{GO2RTC}/api/streams", params={"name": c["id"], "src": fuente(c)}, timeout=5)
        if not r.ok:
            log.warning("go2rtc rechazó la cámara %s (HTTP %s)", c["id"], r.status_code)
            _fallidas.add(c["id"])
            return
        p = requests.put(f"{GO2RTC}/api/preload", params={"src": c["id"], "query": "video&audio"}, timeout=5)
        if not p.ok:
            log.warning("go2rtc no activó preload para %s (HTTP %s)", c["id"], p.status_code)
    except requests.RequestException as e:
        log.warning("No se pudo hablar con go2rtc: %s", e)


def quitar_de_go2rtc(cid):
    try:
        requests.delete(f"{GO2RTC}/api/streams", params={"src": cid}, timeout=5)
    except requests.RequestException as e:
        log.warning("No se pudo quitar %s de go2rtc: %s", cid, e)


def _sincronizar():
    """Cada 10 s revisa que go2rtc tenga todas las cámaras (también tras reiniciarlo)."""
    caido = False
    while True:
        try:
            presentes = requests.get(f"{GO2RTC}/api/streams", timeout=3).json()
            if caido:
                log.warning("go2rtc volvió a responder")
                caido = False
            with _cfg:
                pendientes = [dict(c) for c in _camaras if c["id"] not in presentes and c["id"] not in _fallidas]
            for c in pendientes:
                registrar_en_go2rtc(c)
        except Exception as e:
            if not caido:
                log.warning("go2rtc no responde (%s); se reintenta cada 10 s", e)
                caido = True
        time.sleep(10)


# ----------------------------- ONVIF / PTZ -----------------------------
def conectar(nombre):
    if nombre not in _conexiones:
        c = buscar(nombre)
        transporte = Transport(timeout=TIMEOUT, operation_timeout=TIMEOUT)
        cam = ONVIFCamera(c["ip"], c["puerto_onvif"], c["usuario"], c["clave"], transport=transporte)
        ptz = cam.create_ptz_service()
        token = cam.create_media_service().GetProfiles()[0].token
        _conexiones[nombre] = (ptz, token)
    return _conexiones[nombre]


def llamar(nombre, accion):
    """Ejecuta accion(ptz, token). Si falla o pasa del tiempo, reconecta y reintenta una vez."""
    ultimo = None
    for intento in range(2):
        t0 = time.time()
        try:
            ptz, token = conectar(nombre)
            accion(ptz, token)
            dt = time.time() - t0
            if dt > 1:
                log.warning("%s onvif lento: %.1fs (intento %d)", nombre, dt, intento + 1)
            return
        except HTTPException:
            raise
        except Exception as e:
            ultimo = e
            log.warning("%s onvif fallo tras %.1fs (intento %d): %s", nombre, time.time() - t0, intento + 1, e)
            _conexiones.pop(nombre, None)
    raise HTTPException(502, str(ultimo))


def detener(nombre):
    def accion(ptz, token):
        ptz.Stop({"ProfileToken": token, "PanTilt": True, "Zoom": True})
    try:
        llamar(nombre, accion)
    except HTTPException:
        pass


def registrar(nombre, tipo):
    with _candado:
        n = _estado.get(nombre, (0, ""))[0] + 1
        _estado[nombre] = (n, tipo)
        return n


def programar_parada(nombre):
    with _candado:
        anterior = _temporizadores.pop(nombre, None)
        if anterior:
            anterior.cancel()
        t = threading.Timer(AUTOPARADA, detener, [nombre])
        t.daemon = True
        _temporizadores[nombre] = t
        t.start()


# --------------------------- límite de sesiones ---------------------------
def _limpiar():
    ahora = time.time()
    for sid, s in list(_sesiones.items()):
        if ahora - s["visto"] > SESION_CADUCA:
            del _sesiones[sid]
    for sid, t in list(_expulsadas.items()):
        if ahora - t > 600:
            del _expulsadas[sid]


def exigir_sesion(sid):
    with _candado:
        s = _sesiones.get(sid)
        if s:
            s["visto"] = time.time()
    if not s:
        raise HTTPException(403, "Tu sesión ya no está activa")


@app.post("/api/sesion")
def abrir_sesion():
    with _candado:
        _limpiar()
        desplazo = False
        while len(_sesiones) >= MAX_SESIONES:
            viejo = min(_sesiones, key=lambda k: _sesiones[k]["inicio"])
            del _sesiones[viejo]
            _expulsadas[viejo] = time.time()
            desplazo = True
        sid = secrets.token_urlsafe(12)
        ahora = time.time()
        _sesiones[sid] = {"inicio": ahora, "visto": ahora}
        if desplazo:
            log.warning("Sesión nueva: se cerró la más antigua (límite %d)", MAX_SESIONES)
    return {"id": sid, "desplazo": desplazo, "max": MAX_SESIONES}


@app.post("/api/sesion/{sid}/latido")
def latido(sid: str):
    with _candado:
        _limpiar()
        s = _sesiones.get(sid)
        if s:
            s["visto"] = time.time()
            return {"estado": "activa"}
        if sid in _expulsadas:
            return {"estado": "expulsada"}
    return {"estado": "desconocida"}


@app.post("/api/sesion/{sid}/salir")
def cerrar_sesion(sid: str):
    with _candado:
        _sesiones.pop(sid, None)
    return {"ok": True}


# ----------------------------- cámaras (API) -----------------------------
class NuevaCamara(BaseModel):
    ips: str  # una o varias IPs separadas por coma, espacio o salto de línea
    nombre: str = ""
    puerto_rtsp: int = 554
    ruta_rtsp: str = "/live/ch00_0"
    puerto_onvif: int = 8899
    usuario: str = ""
    clave: str = ""


def exigir_admin(pin):
    if ADMIN_PIN and not secrets.compare_digest(pin.encode(), ADMIN_PIN.encode()):
        raise HTTPException(401, "PIN de administrador incorrecto")


def _validar_ip(txt):
    try:
        ip = ipaddress.ip_address(txt)
    except ValueError:
        raise HTTPException(422, f"IP no válida: {txt}")
    if not ip.is_private or ip.is_loopback:
        raise HTTPException(422, f"Solo se permiten IPs de la red local: {txt}")
    return str(ip)


@app.get("/api/config/estado")
def estado_config():
    return {"protegido": bool(ADMIN_PIN)}


@app.get("/api/camaras")
def listar():
    with _cfg:
        return [_publica(c) for c in _camaras]


@app.post("/api/camaras")
def agregar(d: NuevaCamara, x_admin: str = Header(default="")):
    exigir_admin(x_admin)
    ips = [p for p in re.split(r"[\s,;]+", d.ips) if p]
    if not ips:
        raise HTTPException(422, "Escribe al menos una IP")
    ips = [_validar_ip(i) for i in dict.fromkeys(ips)]
    if not RUTA_OK.match(d.ruta_rtsp):
        raise HTTPException(422, "La ruta RTSP debe empezar con / y no llevar espacios ni #")
    if not (1 <= d.puerto_rtsp <= 65535 and 1 <= d.puerto_onvif <= 65535):
        raise HTTPException(422, "Puerto fuera de rango")
    nombre = d.nombre.strip()[:40]
    nuevas, omitidas = [], []
    with _cfg:
        existentes = {c["ip"] for c in _camaras}
        for ip in ips:
            if ip in existentes:
                omitidas.append(ip)
                continue
            if len(_camaras) >= MAX_CAMARAS:
                raise HTTPException(422, f"Límite de {MAX_CAMARAS} cámaras")
            cid = _nuevo_id()
            etiqueta = nombre if (nombre and len(ips) == 1) else f"{nombre or 'Cámara'} {cid.removeprefix('camara')}"
            c = _nueva(cid, etiqueta, ip, d.puerto_rtsp, d.ruta_rtsp, d.puerto_onvif, d.usuario, d.clave)
            _camaras.append(c)
            nuevas.append(c)
        if nuevas:
            _guardar()
    for c in nuevas:
        _fallidas.discard(c["id"])
        registrar_en_go2rtc(c)
    return {"agregadas": [c["id"] for c in nuevas], "omitidas": omitidas}


@app.delete("/api/camaras/{cid}")
def quitar(cid: str, x_admin: str = Header(default="")):
    exigir_admin(x_admin)
    with _cfg:
        antes = len(_camaras)
        _camaras[:] = [c for c in _camaras if c["id"] != cid]
        if len(_camaras) == antes:
            raise HTTPException(404, "No existe esa cámara")
        _guardar()
    _conexiones.pop(cid, None)
    _fallidas.discard(cid)
    quitar_de_go2rtc(cid)
    return {"ok": True}


@app.post("/api/{nombre}/mover")
def mover(nombre: str, m: dict, x_sesion: str = Header(default="")):
    exigir_sesion(x_sesion)
    x = max(-1.0, min(1.0, float(m.get("x", 0))))
    y = max(-1.0, min(1.0, float(m.get("y", 0))))
    n = registrar(nombre, "mover")

    def accion(ptz, token):
        req = ptz.create_type("ContinuousMove")
        req.ProfileToken = token
        req.Velocity = {"PanTilt": {"x": x, "y": y}}
        ptz.ContinuousMove(req)

    llamar(nombre, accion)

    with _candado:
        actual = _estado.get(nombre, (n, "mover"))
    if actual[0] == n:
        programar_parada(nombre)
    elif actual[1] == "parar":
        detener(nombre)  # soltaste el botón mientras el comando viajaba
    return {"ok": True}


@app.post("/api/{nombre}/parar")
def parar(nombre: str):
    registrar(nombre, "parar")
    with _candado:
        t = _temporizadores.pop(nombre, None)
        if t:
            t.cancel()
    detener(nombre)
    return {"ok": True}


_cargar()
threading.Thread(target=_sincronizar, daemon=True).start()
app.mount("/", StaticFiles(directory="static", html=True), name="static")
