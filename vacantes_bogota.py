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

Destinatarios (secrets de GitHub, nunca en el repo):
  - Telegram: TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_IDS ("id1,id2").
  - WhatsApp: CALLMEBOT_RECIPIENTS,
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
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

URL = "https://sistemamaestro.mineducacion.gov.co/SistemaMaestro/busquedaVacantes.xhtml"
UA = "VacantesBogotaMonitor/1.0 (consulta publica de vacantes, 1 vez por hora)"
STATE_FILE = Path(__file__).parent / "data" / "vacantes_vistas.json"
RESUMEN_FILE = Path(__file__).parent / "data" / "resumen.json"
HORA_RESUMEN = 22  # 10 p. m. hora Colombia
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


def describir(v, nueva=False):
    marca = "🟢 NUEVA · " if nueva else "• "
    return (f"{marca}{v.get('cargo','')}\n  📍 Zona: {get_field(v,'Zona')}\n"
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


def telegram_chats():
    raw = os.environ.get("TELEGRAM_CHAT_IDS", "")
    return [c.strip() for c in raw.replace(";", ",").split(",") if c.strip()]


def _wa(tel, key, texto):
    url = "https://api.callmebot.com/whatsapp.php?" + urllib.parse.urlencode(
        {"phone": tel, "text": texto, "apikey": key})
    with urllib.request.urlopen(url, timeout=60) as r:
        body = r.read().decode("utf-8", "replace")
    time.sleep(5)  # CallMeBot limita la frecuencia
    return r.status == 200 and "error" not in body.lower(), body


def _tg(token, chat, texto):
    data = urllib.parse.urlencode({"chat_id": chat, "text": texto}).encode("utf-8")
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
    return '"ok":true' in body.replace(" ", ""), body


def enviar(texto):
    """Envía por Telegram y/o WhatsApp (CallMeBot), según los secrets que existan.
    Devuelve (llegó a alguno, llegó a todos)."""
    envios = [("WhatsApp", f"#{i}", lambda t=tel, k=key: _wa(t, k, texto))
              for i, (tel, key) in enumerate(destinatarios(), 1)]
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if token:
        envios += [("Telegram", f"#{i}", lambda c=chat: _tg(token, c, texto))
                   for i, chat in enumerate(telegram_chats(), 1)]
    if not envios:
        print("Sin destinatarios (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_IDS o CALLMEBOT_RECIPIENTS): no se envía nada.")
        return False, False
    ok_any, ok_all = False, True
    for canal, n, fn in envios:
        try:
            ok, body = fn()
        except Exception as e:  # no imprimir URLs: llevan apikey/token
            ok, body = False, type(e).__name__
        if not ok and token:
            body = body.replace(token, "***")
        print(f"{canal} {n}: {'enviado' if ok else 'FALLÓ'}" + ("" if ok else f" ({body[:150]})"))
        ok_any |= ok
        ok_all &= ok
    return ok_any, ok_all


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
    ap.add_argument("--dry-run", action="store_true", help="no envía mensajes")
    ap.add_argument("--resumen", action="store_true", help="manda el resumen del día ya (sin esperar las 22:00)")
    a = ap.parse_args()

    for intento in range(1, 4):  # la página del Ministerio a veces no responde
        try:
            vacantes, log = consultar()
            break
        except Exception as e:
            print(f"Intento {intento}/3 falló: {e}")
            if intento == 3:
                print("ERROR consultando la página: se reintenta en la próxima corrida.")
                return 1
            time.sleep(20)
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

    # alguno = llegó al menos a un número (se marcan como vistas: no re-enviar a los demás)
    alguno, todos = True, True
    lineas = []
    if primera_vez:
        lineas = [f"✅ Monitor de vacantes activo ({ahora})",
                  "Bogotá · Sin asignación directa · Vacantes Generales", ""]
        if vacantes:
            lineas += [f"Estas son las {len(vacantes)} vacantes publicadas ahora:"] + \
                      [describir(v, nueva=True) for v in vacantes]
        else:
            lineas += ["Ahora mismo no hay vacantes publicadas."]
        lineas += ["", "👉 Para verlas y postularte entra aquí:", URL, "",
                   "Te aviso cada vez que salga una nueva."]
    elif nuevas:
        # Siempre la lista completa; las nuevas primero y marcadas con 🟢 NUEVA.
        ids_nuevas = {v["id"] for v in nuevas}
        viejas = [v for v in vacantes if v["id"] not in ids_nuevas]
        n = len(nuevas)
        titulo = "una vacante nueva" if n == 1 else f"{n} vacantes nuevas"
        lineas = [f"🔔 ¡Salió {titulo}! ({ahora})",
                  "Bogotá · Sin asignación directa · Vacantes Generales",
                  f"Publicadas ahora: {len(vacantes)} ({n} 🟢 nueva{'' if n == 1 else 's'})", ""] + \
                 [describir(v, nueva=True) for v in nuevas] + \
                 [describir(v) for v in viejas] + \
                 ["", "👉 Para verlas y postularte entra aquí:", URL]
    if lineas:
        for t in trozos(lineas):
            if a.dry_run:
                print("[dry-run] mensaje:\n" + t)
            else:
                a1, a2 = enviar(t)
                alguno &= a1
                todos &= a2

    if a.dry_run:
        if a.resumen:
            resumen_diario(previo, vacantes, forzar=True, dry_run=True)
        print("[dry-run] no se guarda estado.")
        return 0

    # Guardar estado: se acumulan las ya vistas (para no re-avisar si reaparecen).
    # Si no llegó a nadie (o falta el secret), las nuevas NO se marcan como vistas:
    # se reintenta en la próxima corrida (también el mensaje de bienvenida).
    estado = dict(previo)
    for v in vacantes:
        if v["id"] in previo or alguno:
            estado[v["id"]] = {"cargo": v.get("cargo", ""), "zona": get_field(v, "Zona"),
                               "cierre": get_field(v, "Cierre vacante"),
                               "visto": previo.get(v["id"], {}).get("visto", ahora)}
    if primera_vez and not alguno:
        print("No se guarda estado: la bienvenida se reintenta en la próxima corrida.")
        return 2
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(estado, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    if not resumen_diario(estado, vacantes, a.resumen):
        todos = False
    return 0 if todos else 2


def resumen_diario(estado, vacantes, forzar=False, dry_run=False):
    """Una vez al día, desde las 22:00 (Colombia), resume lo que se publicó hoy.
    Devuelve False solo si había que enviarlo y no llegó a nadie."""
    hoy = datetime.now(COT)
    fecha = hoy.strftime("%Y-%m-%d")
    previo = json.loads(RESUMEN_FILE.read_text(encoding="utf-8")) if RESUMEN_FILE.exists() else {}
    if not forzar and (hoy.hour < HORA_RESUMEN or previo.get("ultimo") == fecha):
        return True
    n = sum(1 for e in estado.values() if str(e.get("visto", "")).startswith(fecha))
    filtros = "📍 Bogotá · Sin asignación directa · Vacantes Generales"
    if n:
        lineas = [f"🌙 Resumen de hoy ({hoy.strftime('%d/%m/%Y')})", "",
                  f"Hoy se encontr{'ó 1 vacante nueva' if n == 1 else f'aron {n} vacantes nuevas'} con los filtros:",
                  filtros, f"Publicadas ahora mismo: {len(vacantes)}"]
    else:
        lineas = [f"🌙 Resumen de hoy ({hoy.strftime('%d/%m/%Y')})", "",
                  "Hoy no se publicaron vacantes nuevas con los filtros:", filtros,
                  f"Publicadas ahora mismo: {len(vacantes)}", "", "Mañana sigo pendiente 💪"]
    lineas += ["", "👉 Para revisar entra aquí:", URL]
    texto = "\n".join(lineas)
    if dry_run:
        print("[dry-run] resumen:\n" + texto)
        return True
    alguno, _ = enviar(texto)
    if alguno:
        RESUMEN_FILE.write_text(json.dumps({"ultimo": fecha}), encoding="utf-8")
    return alguno


if __name__ == "__main__":
    sys.exit(main())
