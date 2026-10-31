import logging
import os
import time
import shutil
import json
from datetime import datetime, timedelta

import requests
import numpy as np
import cv2
from ultralytics import YOLO

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("detector")

GO2RTC_URL = os.environ.get("GO2RTC_URL", "http://go2rtc:1984")
PANEL_URL = os.environ.get("PANEL_URL", "http://panel:8080")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
INTERVALO = float(os.environ.get("INTERVALO", "1.5"))
CONFIANZA = float(os.environ.get("CONFIANZA", "0.5"))
MOVIMIENTO = float(os.environ.get("MOVIMIENTO", "0.004"))
ENFRIAMIENTO = float(os.environ.get("ENFRIAMIENTO", "30"))
RETENCION_DIAS = int(os.environ.get("RETENCION_DIAS", "7"))
RESUMEN_CADA_H = int(os.environ.get("RESUMEN_CADA_H", "2"))
MODELO = os.environ.get("MODELO", "yolo11n.pt")
TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
SILENCIO_DESDE = int(os.environ.get("SILENCIO_DESDE", "23"))  # sin imágenes por Telegram de esta hora...
SILENCIO_HASTA = int(os.environ.get("SILENCIO_HASTA", "7"))   # ...hasta esta (las fotos se siguen guardando)

FOTOS = os.path.join(DATA_DIR, "fotos")
CLASES = {0: "persona", 2: "carro", 3: "moto", 7: "carro", 15: "gato", 16: "perro"}

_previo = {}
_ultima = {}
_camaras_cache = []
modelo = None

def camaras():
    global _camaras_cache
    try:
        r = requests.get(f"{PANEL_URL}/api/camaras", timeout=5)
        _camaras_cache = r.json()
    except Exception:
        pass
    return _camaras_cache

def foto(cid):
    try:
        r = requests.get(f"{GO2RTC_URL}/api/frame.jpeg", params={"src": cid}, timeout=15)
        return cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        return None

def mascara_movimiento(cid, img):
    gris = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gris.shape
    nw = 320
    nh = int(h * (nw / w))
    gris = cv2.resize(gris, (nw, nh))
    gris = cv2.GaussianBlur(gris, (5, 5), 0)
    
    if cid not in _previo or _previo[cid].shape != gris.shape:
        _previo[cid] = gris
        return None
    
    diff = cv2.absdiff(_previo[cid], gris)
    _, mask = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
    mask = cv2.dilate(mask, None, iterations=2)
    _previo[cid] = gris
    return mask

def movimiento_en(mascara, caja, ancho_original):
    x1, y1, x2, y2 = caja
    f = mascara.shape[1] / ancho_original
    mx1, my1 = max(0, int(x1 * f)), max(0, int(y1 * f))
    mx2, my2 = min(mascara.shape[1], int(x2 * f)), min(mascara.shape[0], int(y2 * f))
    if mx2 <= mx1 or my2 <= my1:
        return 0
    roi = mascara[my1:my2, mx1:mx2]
    return np.count_nonzero(roi) / roi.size

def procesar(c):
    cid = c["id"]
    img = foto(cid)
    if img is None:
        return
    
    m = mascara_movimiento(cid, img)
    if m is None or np.count_nonzero(m) / m.size < MOVIMIENTO:
        return
    
    res = modelo(img, conf=CONFIANZA, classes=list(CLASES), verbose=False)[0]
    detecciones = []
    for b in res.boxes:
        cls_id = int(b.cls)
        caja = b.xyxy[0].tolist()
        if movimiento_en(m, caja, img.shape[1]) >= 0.02:
            detecciones.append(CLASES[cls_id])
    
    if not detecciones:
        return
        
    clases_detectadas = sorted(list(set(detecciones)))
    ahora = time.time()
    validas = [cls for cls in clases_detectadas if ahora - _ultima.get((cid, cls), 0) >= ENFRIAMIENTO]
    
    if validas:
        for cls in validas:
            _ultima[(cid, cls)] = ahora
        
        img_plot = res.plot()
        fecha = time.strftime("%Y-%m-%d")
        hora = time.strftime("%H-%M-%S")
        clases_str = "-".join(validas)
        path = os.path.join(FOTOS, fecha, f"{cid}_{hora}_{clases_str}.jpg")
        
        os.makedirs(os.path.dirname(path), exist_ok=True)
        cv2.imwrite(path, img_plot)
        log.info(f"{cid}: {clases_str}")

def limpiar():
    ahora = time.time()
    if not os.path.exists(FOTOS):
        return
    for d in os.listdir(FOTOS):
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
            if (ahora - dt.timestamp()) > RETENCION_DIAS * 86400:
                shutil.rmtree(os.path.join(FOTOS, d))
        except (ValueError, OSError):
            pass

def fotos_entre(desde, hasta):
    fotos = []
    if not os.path.exists(FOTOS):
        return []
    for root, _, files in os.walk(FOTOS):
        for f in files:
            if not f.endswith(".jpg"):
                continue
            p = os.path.join(root, f)
            mt = os.path.getmtime(p)
            if desde <= mt < hasta:
                name = f[:-4]
                cid = name.split("_")[0]
                clases = name.split("_")[-1].split("-")
                fotos.append({"path": p, "mtime": mt, "cid": cid, "clases": clases})
    return sorted(fotos, key=lambda x: x["mtime"])

def en_silencio(t):
    h = time.localtime(t).tm_hour
    if SILENCIO_DESDE > SILENCIO_HASTA:
        return h >= SILENCIO_DESDE or h < SILENCIO_HASTA
    return SILENCIO_DESDE <= h < SILENCIO_HASTA


def resumen(desde, hasta):
    fotos = fotos_entre(desde, hasta)
    t_desde = time.strftime("%H:%M", time.localtime(desde))
    t_hasta = time.strftime("%H:%M", time.localtime(hasta))
    
    if not fotos:
        texto = f"📷 Sin detecciones de {t_desde} a {t_hasta}"
    else:
        texto = f"📷 Detecciones de {t_desde} a {t_hasta}\n"
        conteo = {}
        for f in fotos:
            cid = f["cid"]
            conteo.setdefault(cid, {})
            for cls in f["clases"]:
                conteo[cid][cls] = conteo[cid].get(cls, 0) + 1
        
        cams = {c["id"]: c["nombre"] for c in camaras()}
        for cid, cls_counts in conteo.items():
            nombre = cams.get(cid, cid)
            res_c = ", ".join([f"{v} {k}" for k, v in cls_counts.items()])
            texto += f"• {nombre}: {res_c}\n"
    
    silencio = bool(fotos) and en_silencio(time.time())
    if silencio:
        texto += f"(sin fotos de {SILENCIO_DESDE}:00 a {SILENCIO_HASTA}:00; pídelas con /hoy)"

    if TOKEN and CHAT:
        try:
            requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", 
                          json={"chat_id": CHAT, "text": texto}, timeout=60)
            if fotos and not silencio:
                seleccion = []
                vistos = set()
                for f in fotos:
                    for cls in f["clases"]:
                        if (f["cid"], cls) not in vistos:
                            seleccion.append(f)
                            vistos.add((f["cid"], cls))
                
                if len(seleccion) > 10:
                    seleccion = seleccion[:10]
                elif len(seleccion) < 10:
                    for f in reversed(fotos):
                        if f not in seleccion:
                            seleccion.append(f)
                            if len(seleccion) == 10: break
                
                if len(seleccion) == 1:
                    f = seleccion[0]
                    with open(f["path"], "rb") as img:
                        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
                                      data={"chat_id": CHAT, "caption": os.path.basename(f["path"])[:-4]},
                                      files={"photo": img}, timeout=60)
                elif len(seleccion) > 1:
                    files = {}
                    media = []
                    for i, f in enumerate(seleccion):
                        fid = f"f{i}"
                        files[fid] = open(f["path"], "rb")
                        media.append({"type": "photo", "media": f"attach://{fid}", "caption": os.path.basename(f["path"])[:-4]})
                    
                    requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMediaGroup",
                                  data={"chat_id": CHAT, "media": json.dumps(media)},
                                  files=files, timeout=60)
                    for img in files.values():
                        img.close()
        except Exception as e:
            log.warning("Error resumen telegram: %s", e)

def siguiente_resumen(t):
    dt = datetime.fromtimestamp(t)
    h_next = ((dt.hour // RESUMEN_CADA_H) + 1) * RESUMEN_CADA_H
    res = dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=h_next)
    return res.timestamp()

def main():
    global modelo
    modelo = YOLO(MODELO)
    limpiar()
    proximo = siguiente_resumen(time.time())
    inicio = time.time()
    
    if TOKEN and CHAT:
        try:
            requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", 
                          json={"chat_id": CHAT, "text": f"🔎 Detector iniciado: personas, carros, motos, gatos y perros. Resumen cada {RESUMEN_CADA_H} h."}, timeout=10)
        except Exception:
            pass

    while True:
        for c in camaras():
            try:
                procesar(c)
            except Exception:
                log.exception("procesar: fallo")
        
        ahora = time.time()
        if ahora >= proximo:
            resumen(inicio, proximo)
            inicio = proximo
            proximo = siguiente_resumen(ahora)
            limpiar()
        
        time.sleep(INTERVALO)

if __name__ == "__main__":
    main()
