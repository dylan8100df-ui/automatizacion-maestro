"""Monitor de vacantes docentes en Sistema Maestro (MinEducación).

Filtros: Secretaría = Bogotá, Área = "Sin asignación directa",
Tipo Priorización = "Vacantes Generales".

Solo CONSULTA la búsqueda pública y avisa por WhatsApp (CallMeBot) cuando
aparece una vacante nueva. No inicia sesión ni se postula a nada.

Usa peticiones HTTP normales (las mismas que hace la página al cambiar un
filtro o de página), sin navegador: el firewall del sitio bloquea Chromium
automatizado. El cliente se identifica con un User-Agent propio y honesto.

Uso:
  python vacantes_bogota.py             # consulta, compara y notifica
  python vacantes_bogota.py --inspect   # prueba filtros/paginador, no notifica ni guarda
  python vacantes_bogota.py --dry-run   # todo menos enviar WhatsApp

Destinatarios: variable de entorno CALLMEBOT_RECIPIENTS (secret de GitHub),
formato "telefono:apikey,telefono:apikey" (cada número tiene su propia apikey
de CallMeBot). Nunca se escriben en el repo.
"""
import argparse
import hashlib
import html
import http.cookiejar
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

URL = "https://sistemamaestro.mineducacion.gov.co/SistemaMaestro/busquedaVacantes.xhtml"
UA = "VacantesBogotaMonitor/1.0 (consulta publica de vacantes, 1 vez por hora)"
STATE_FILE = Path(__file__).parent / "data" / "vacantes_vistas.json"
COT = timezone(timedelta(hours=-5))
FORM = "form-busqueda"
TABLA = FORM + ":tabla-vacantes"

# (campo del formulario, nombre, texto de la opción a elegir)
FILTROS = [
    (FORM + ":idInputSecretaria", "Secretaría", "Bogotá"),
    (FORM + ":idInputArea", "Área", "Sin asignación directa"),
    (FORM + ":idInputTipoPonderado", "Tipo Priorización", "Vacantes Generales"),
]


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def decode(b):
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("iso-8859-1")  # el servidor mezcla codificaciones


class Sesion:
    def __init__(self):
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.viewstate = None

    def get(self):
        req = urllib.request.Request(URL, headers={"User-Agent": UA})
        with self.op.open(req, timeout=60) as r:
            t = decode(r.read())
        self._viewstate(t)
        return t

    def ajax(self, data):
        data = dict(data, **{"javax.faces.partial.ajax": "true", "javax.faces.ViewState": self.viewstate})
        req = urllib.request.Request(URL, data=urllib.parse.urlencode(data).encode("utf-8"), headers={
            "User-Agent": UA, "Faces-Request": "partial/ajax", "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
        with self.op.open(req, timeout=60) as r:
            t = decode(r.read())
        if "<partial-response" not in t:
            raise RuntimeError("Respuesta inesperada del servidor (¿bloqueo o cambio de la página?)")
        if "<error>" in t:
            raise RuntimeError("El servidor devolvió un error JSF: " + re.sub(r"\s+", " ", t)[:300])
        self._viewstate(t)
        return t

    def _viewstate(self, t):
        m = (re.search(r'<update id="[^"]*javax\.faces\.ViewState[^"]*"><!\[CDATA\[(.*?)\]\]>', t)
             or re.search(r'name="javax\.faces\.ViewState"[^>]*value="([^"]+)"', t))
        if m:
            self.viewstate = m.group(1)


def opciones(pagina, campo):
    m = re.search(r'<select id="%s_input".*?</select>' % re.escape(campo), pagina, re.S)
    if not m:
        raise RuntimeError(f"No encontré el selector {campo}_input en la página")
    return [(v, html.unescape(t).strip())
            for v, t in re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)', m.group(0))]


def leer_vacantes(fragmento):
    out = []
    bloques = re.split(r'(?=<div id="[^"]*" class="ui-panel[^"]*\bvacante\b)', fragmento)
    for b in bloques[1:]:
        labels = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", l))).strip()
                  for l in re.findall(r"<label[^>]*>(.*?)</label>", b, re.S)]
        labels = [l for l in labels if l]
        if not labels:
            continue
        d = {"cargo": labels[0]}
        for l in labels[1:]:
            if ":" in l:
                k, v = l.split(":", 1)
                d[k.strip()] = v.strip()
        out.append(d)
    return out


def total_filas(t):
    m = re.findall(r"rowCount:(\d+)", t)
    return int(m[-1]) if m else None


def get_field(d, *nombres):
    nn = {norm(k): v for k, v in d.items()}
    for n in nombres:
        if norm(n) in nn:
            return nn[norm(n)]
    return ""


def clave(v):
    # "Postulados" cambia con el tiempo: no entra en la clave.
    partes = [v.get("cargo", ""), get_field(v, "Cierre vacante"), get_field(v, "Zona"),
              get_field(v, "Área", "Area"), get_field(v, "Tipo Priorización"),
              get_field(v, "Secretaría de Educación")]
    return hashlib.sha1(norm("|".join(partes)).encode()).hexdigest()[:16]


def consultar():
    log, s = [], Sesion()
    pagina = s.get()
    log.append(f"Página cargada ({len(pagina)} bytes)")

    form = {FORM: FORM, FORM + ":idInputDepartamento_input": ""}
    for campo, nombre, texto in FILTROS:
        ops = opciones(pagina, campo)
        valor = next((v for v, t in ops if norm(t) == norm(texto)), None)
        if valor is None:
            raise RuntimeError(f"No encontré la opción '{texto}' en {nombre} ({len(ops)} opciones)")
        form[campo + "_input"] = valor
        log.append(f"OK filtro {nombre}: '{texto}' (value={valor}, {len(ops)} opciones)")

    # Igual que el onchange del último filtro: procesa todo el formulario.
    r = s.ajax(dict(form, **{
        "javax.faces.source": FILTROS[-1][0], "javax.faces.partial.execute": "@all",
        "javax.faces.partial.render": "accordion", "javax.faces.behavior.event": "change",
        "javax.faces.partial.event": "change"}))
    total = total_filas(r)
    vacantes = leer_vacantes(r)
    log.append(f"Resultados: {total} vacantes en total; página 1 trae {len(vacantes)}")
    if total is None:
        raise RuntimeError("No encontré el total de resultados (rowCount) en la respuesta")

    paso = len(vacantes) or 6
    n = 1
    while len(vacantes) < total and n < 50:
        n += 1
        r = s.ajax(dict(form, **{
            "javax.faces.source": TABLA, "javax.faces.partial.execute": TABLA,
            "javax.faces.partial.render": TABLA, TABLA: TABLA,
            TABLA + "_pagination": "true", TABLA + "_first": str(len(vacantes)),
            TABLA + "_rows": str(paso), TABLA + "_encodeFeature": "true"}))
        nuevas = leer_vacantes(r)
        log.append(f"Página {n}: {len(nuevas)} vacantes")
        if not nuevas:
            break
        vacantes += nuevas
        time.sleep(1)  # sin afán: no cargar el servidor

    if len(vacantes) != total:
        raise RuntimeError(f"Leí {len(vacantes)} vacantes pero el servidor dice {total}")

    vistas = set()
    for v in vacantes:
        k, i = clave(v), 2
        while k in vistas:  # dos vacantes idénticas en texto
            k = f"{clave(v)}-{i}"; i += 1
        vistas.add(k)
        v["id"] = k

    fuera = [v for v in vacantes
             if norm(get_field(v, "Secretaría de Educación")) != norm("Bogotá")
             or norm(get_field(v, "Área", "Area")) != norm("Sin asignación directa")
             or norm(get_field(v, "Tipo Priorización")) != norm("Vacantes Generales")]
    log.append(f"Total leído: {len(vacantes)}; {len(fuera)} no cumplen los filtros")
    if fuera:
        raise RuntimeError(f"{len(fuera)} vacantes no cumplen los filtros; el filtrado falló: {fuera[:2]}")
    return vacantes, log


def describir(v):
    return (f"• {v.get('cargo','')}\n  📍 Zona: {get_field(v,'Zona')}\n"
            f"  ⏰ Cierra: {get_field(v,'Cierre vacante')}\n  👥 Postulados: {get_field(v,'Postulados')}")


def destinatarios():
    raw = os.environ.get("CALLMEBOT_RECIPIENTS", "").strip()
    out = []
    for item in raw.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            print("Aviso: un destinatario del secret no tiene formato telefono:apikey (se omite)")
            continue
        tel, key = item.split(":", 1)
        out.append((tel.strip(), key.strip()))
    return out


def enviar_whatsapp(texto):
    dest = destinatarios()
    if not dest:
        print("Sin CALLMEBOT_RECIPIENTS: no se envía nada.")
        return False
    ok_all = True
    for i, (tel, key) in enumerate(dest, 1):
        url = "https://api.callmebot.com/whatsapp.php?" + urllib.parse.urlencode(
            {"phone": tel, "text": texto, "apikey": key})
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                body = r.read().decode("utf-8", "replace")
                ok = r.status == 200 and "error" not in body.lower()
        except Exception as e:  # no imprimir la URL: lleva la apikey
            ok, body = False, type(e).__name__
        print(f"Destinatario #{i}: {'enviado' if ok else 'FALLÓ'}" + ("" if ok else f" ({body[:120]})"))
        ok_all &= ok
        time.sleep(5)  # CallMeBot limita la frecuencia
    return ok_all


def trozos(lineas, maximo=1200):
    buf = ""
    for l in lineas:
        if buf and len(buf) + len(l) + 1 > maximo:
            yield buf; buf = ""
        buf += l + "\n"
    if buf:
        yield buf


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--inspect", action="store_true", help="solo prueba filtros y paginador")
    ap.add_argument("--dry-run", action="store_true", help="no envía WhatsApp")
    a = ap.parse_args()

    try:
        vacantes, log = consultar()
    except Exception as e:
        print(f"ERROR consultando la página: {e}")
        return 1
    print("\n".join(log))

    if a.inspect:
        print("\nMuestra:")
        for v in vacantes[:5]:
            print(describir(v), f"[id {v['id']}]")
        print("\nInspección terminada (no se notificó ni se guardó estado).")
        return 0

    primera_vez = not STATE_FILE.exists()
    previo = {} if primera_vez else json.loads(STATE_FILE.read_text(encoding="utf-8"))
    nuevas = [v for v in vacantes if v["id"] not in previo]
    ahora = datetime.now(COT).strftime("%Y-%m-%d %H:%M")
    print(f"\n{len(nuevas)} nuevas de {len(vacantes)} (primera vez: {primera_vez})")

    enviado = True
    lineas = []
    if primera_vez:
        lineas = [f"✅ Monitor de vacantes activo ({ahora})",
                  "Bogotá · Sin asignación directa · Vacantes Generales", ""]
        if vacantes:
            lineas += [f"Estas son las {len(vacantes)} vacantes publicadas ahora:"] + \
                      [describir(v) for v in vacantes]
        else:
            lineas += ["Ahora mismo no hay vacantes publicadas."]
        lineas += ["", "👉 Para verlas y postularte entra aquí:", URL, "",
                   "Te aviso cada vez que salga una nueva."]
    elif nuevas:
        lineas = [f"🔔 {len(nuevas)} vacante(s) nueva(s) en Bogotá ({ahora}):"] + \
                 [describir(v) for v in nuevas] + \
                 ["", "👉 Para verlas y postularte entra aquí:", URL]
    if lineas:
        for t in trozos(lineas):
            if a.dry_run:
                print("[dry-run] mensaje:\n" + t)
            else:
                enviado &= enviar_whatsapp(t)

    if a.dry_run:
        print("[dry-run] no se guarda estado.")
        return 0

    # Guardar estado: se acumulan las ya vistas (para no re-avisar si reaparecen).
    # Si el envío falló, las nuevas NO se marcan como vistas: se reintenta en la próxima corrida.
    estado = dict(previo)
    for v in vacantes:
        if v["id"] in previo or enviado or primera_vez:
            estado[v["id"]] = {"cargo": v.get("cargo", ""), "zona": get_field(v, "Zona"),
                               "cierre": get_field(v, "Cierre vacante"),
                               "visto": previo.get(v["id"], {}).get("visto", ahora)}
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(estado, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    return 0 if enviado else 2


if __name__ == "__main__":
    sys.exit(main())
