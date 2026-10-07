"""Monitor de vacantes docentes en Sistema Maestro (MinEducación).

Filtros: Secretaría = Bogotá, Área = "Sin asignación directa",
Tipo Priorización = "Vacantes Generales".

Solo CONSULTA la búsqueda pública y avisa por WhatsApp (CallMeBot) cuando
aparece una vacante nueva. No inicia sesión ni se postula a nada.

Uso:
  python vacantes_bogota.py             # consulta, compara y notifica
  python vacantes_bogota.py --inspect   # prueba selectores/paginador, no notifica ni guarda
  python vacantes_bogota.py --dry-run   # todo menos enviar WhatsApp

Destinatarios: variable de entorno CALLMEBOT_RECIPIENTS (secret de GitHub),
formato "telefono:apikey,telefono:apikey" (cada número tiene su propia apikey
de CallMeBot). Nunca se escriben en el repo.
"""
import argparse
import hashlib
import json
import os
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = "https://sistemamaestro.mineducacion.gov.co/SistemaMaestro/busquedaVacantes.xhtml"
STATE_FILE = Path(__file__).parent / "data" / "vacantes_vistas.json"
COT = timezone(timedelta(hours=-5))

# id del componente PrimeFaces -> texto de la opción a elegir
FILTROS = [
    ("form-busqueda:idInputSecretaria", "Secretaria", "Bogotá"),
    ("form-busqueda:idInputArea", "Área", "Sin asignación directa"),
    ("form-busqueda:idInputTipoPonderado", "Tipo Priorización", "Vacantes Generales"),
]
TABLA = "form-busqueda:tabla-vacantes"


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def css_id(i):
    return "#" + i.replace(":", "\\:")


def esperar_ajax(page, timeout=30000):
    page.wait_for_function(
        "() => !window.PrimeFaces || !PrimeFaces.ajax || PrimeFaces.ajax.Queue.isEmpty()",
        timeout=timeout,
    )
    page.wait_for_load_state("networkidle", timeout=timeout)


def elegir_opcion(page, comp_id, texto):
    """Selecciona una opción de un p:selectOneMenu por su texto visible.
    Usa el widget de PrimeFaces (dispara el ajax igual que un clic)."""
    sel = css_id(comp_id + "_input")
    page.wait_for_selector(sel, state="attached", timeout=30000)
    opciones = page.eval_on_selector_all(
        sel + " option", "os => os.map(o => [o.value, o.textContent.trim()])"
    )
    valor = next((v for v, t in opciones if norm(t) == norm(texto)), None)
    if valor is None:
        raise RuntimeError(f"No encontré la opción '{texto}' en {comp_id} ({len(opciones)} opciones)")
    ok = page.evaluate(
        """([id, v]) => {
            const w = Object.values(PrimeFaces.widgets || {}).find(w => w && w.id === id);
            if (w && w.selectValue) { w.selectValue(v); return 'widget'; }
            const s = document.getElementById(id + '_input');
            s.value = v; s.dispatchEvent(new Event('change', {bubbles: true}));
            return 'change';
        }""",
        [comp_id, valor],
    )
    esperar_ajax(page)
    actual = page.eval_on_selector(sel, "s => s.value")
    if actual != valor:
        raise RuntimeError(f"{comp_id}: quedó '{actual}' en vez de '{valor}'")
    return valor, ok, len(opciones)


def leer_pagina(page):
    """Devuelve las vacantes visibles como lista de dicts {campo: valor}."""
    return page.evaluate(
        """(tabla) => {
            const root = document.getElementById(tabla + '_content');
            if (!root) return [];
            return [...root.querySelectorAll('.vacante')].map(p => {
                const labels = [...p.querySelectorAll('label')].map(l => l.textContent.replace(/\\s+/g, ' ').trim()).filter(Boolean);
                const d = {cargo: labels[0] || ''};
                for (const t of labels.slice(1)) {
                    const i = t.indexOf(':');
                    if (i > 0) d[t.slice(0, i).trim()] = t.slice(i + 1).trim();
                }
                return d;
            });
        }""",
        TABLA,
    )


def info_paginador(page):
    return page.evaluate(
        """(tabla) => {
            const pg = document.getElementById(tabla + '_paginator_top') || document.querySelector('.ui-paginator');
            if (!pg) return null;
            const cur = pg.querySelector('.ui-paginator-current');
            const next = pg.querySelector('.ui-paginator-next');
            return {texto: cur ? cur.textContent.trim() : '',
                    hay_siguiente: !!next && !next.classList.contains('ui-state-disabled')};
        }""",
        TABLA,
    )


def poner_filas_por_pagina(page, n="24"):
    sel = css_id(TABLA) + " select.ui-paginator-rpp-options"
    if page.query_selector(sel):
        valores = page.eval_on_selector_all(sel + " >> nth=0 >> option", "os => os.map(o => o.value)")
        if n in valores:
            page.locator(sel).first.select_option(n)
            esperar_ajax(page)
            return True
    return False


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


def consultar(inspect=False):
    vacantes, log = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(locale="es-CO")
        page.goto(URL, wait_until="networkidle", timeout=90000)
        esperar_ajax(page)
        log.append(f"Página cargada: {page.title()!r}")

        for comp_id, nombre, texto in FILTROS:
            valor, modo, n = elegir_opcion(page, comp_id, texto)
            log.append(f"OK filtro {nombre}: '{texto}' (value={valor}, {n} opciones, vía {modo})")

        if poner_filas_por_pagina(page):
            log.append("OK paginador: 24 filas por página")
        else:
            log.append("Aviso: no se pudo cambiar filas por página (se usa el valor por defecto)")

        pagina = 1
        vistas = set()
        while True:
            filas = leer_pagina(page)
            pg = info_paginador(page)
            log.append(f"Página {pagina}: {len(filas)} vacantes; paginador={pg}")
            for f in filas:
                k = clave(f)
                n = 2
                while k in vistas:  # dos vacantes idénticas en texto
                    k = clave(f) + f"-{n}"; n += 1
                vistas.add(k)
                f["id"] = k
                vacantes.append(f)
            if not pg or not pg["hay_siguiente"] or pagina >= 50:
                break
            antes = page.eval_on_selector(css_id(TABLA) + "_content", "e => e.innerText")
            page.locator(css_id(TABLA) + "_paginator_top .ui-paginator-next").click()
            esperar_ajax(page)
            page.wait_for_function(
                "([sel, a]) => document.querySelector(sel).innerText !== a",
                arg=[css_id(TABLA) + "_content", antes], timeout=30000,
            )
            pagina += 1

        # Validación: todo lo leído debe cumplir los filtros.
        fuera = [v for v in vacantes
                 if norm(get_field(v, "Secretaría de Educación")) != norm("Bogotá")
                 or norm(get_field(v, "Área", "Area")) != norm("Sin asignación directa")
                 or norm(get_field(v, "Tipo Priorización")) != norm("Vacantes Generales")]
        log.append(f"Total: {len(vacantes)} vacantes; {len(fuera)} no cumplen los filtros")
        if fuera:
            raise RuntimeError(f"{len(fuera)} vacantes no cumplen los filtros; el filtrado falló: {fuera[:2]}")
        browser.close()
    return vacantes, log


def describir(v):
    return (f"• {v.get('cargo','')}\n  Zona: {get_field(v,'Zona')} · Cierre: {get_field(v,'Cierre vacante')}"
            f" · Postulados: {get_field(v,'Postulados')}")


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
    ap = argparse.ArgumentParser()
    ap.add_argument("--inspect", action="store_true", help="solo prueba selectores y paginador")
    ap.add_argument("--dry-run", action="store_true", help="no envía WhatsApp")
    a = ap.parse_args()

    try:
        vacantes, log = consultar(a.inspect)
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
    if primera_vez:
        msg = (f"✅ Monitor de vacantes activo ({ahora}).\nBogotá · Sin asignación directa · "
               f"Vacantes Generales\nHay {len(vacantes)} vacantes publicadas ahora. "
               f"Te aviso cuando salga una nueva.")
        enviado = a.dry_run or enviar_whatsapp(msg)
    elif nuevas:
        lineas = [f"🔔 {len(nuevas)} vacante(s) nueva(s) en Bogotá ({ahora}):"] + \
                 [describir(v) for v in nuevas] + [URL]
        for t in trozos(lineas):
            if a.dry_run:
                print("[dry-run] mensaje:\n" + t)
            else:
                enviado &= enviar_whatsapp(t)

    # Guardar estado: las vigentes + las ya vistas (para no re-avisar si reaparecen).
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
