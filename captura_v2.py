"""
captura_v2.py  -  Zcode Contrarian Bets (TODAS las ligas) -> CSV + alertas por Telegram

Uso:
    python captura_v2.py          una captura
    python captura_v2.py loop     captura cada INTERVALO segundos
    python captura_v2.py test     envia un mensaje de prueba a Telegram
    python captura_v2.py resumen  envia ya el resumen de los partidos de manana (prueba)

Telegram: edita TG_TOKEN y TG_CHAT en la seccion de configuracion
(o define las variables de entorno TG_TOKEN y TG_CHAT, que tienen prioridad).

Requisitos:  pip install requests beautifulsoup4 tzdata
"""
import requests, csv, os, re, sys, time, json
import html as _html
from collections import Counter
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo          # en Windows requiere: pip install tzdata

NY = ZoneInfo("America/New_York")
BOG = ZoneInfo("America/Bogota")
from bs4 import BeautifulSoup

URL = "https://zcodesystem.com/vipclub/contrarianbets_games_list.php"
HOME = "https://zcodesystem.com/vipclub/contrarianbets.php"
SALIDA = "contrarian_capturas_v2.csv"   # archivo nuevo: las columnas cambiaron
ESTADO_DIR = os.getenv("ESTADO_DIR", ".")    # en GitHub Actions: carpeta "estado" (se guarda en cache)
os.makedirs(ESTADO_DIR, exist_ok=True)
ENVIADAS = os.path.join(ESTADO_DIR, "alertas_enviadas.json")
GUARDAR_CSV = os.getenv("GUARDAR_CSV", "1") != "0"   # en GitHub Actions: 0 (no se guarda historico)
INTERVALO = 300                          # segundos entre capturas en modo loop (5 min)
FMT = "%Y-%m-%d %H:%M:%S"

# ---------------- Configuracion ----------------
DIAS_ATRAS = 1          # fechas EDT a consultar: ayer ...
DIAS_ADELANTE = 1       # ... hasta manana (partidos de madrugada en Bogota)

# Ligas: vacio = TODAS. Usa fragmentos en minuscula, ej. ["premier", "la liga"]
LIGAS_INCLUIR = []
LIGAS_EXCLUIR = []

# Filtro de consenso
UMBRAL_RATIO = 1.0       # Cash Ratio minimo
MIN_DROP_OP = 3          # caida minima de la cuota desde apertura (%)
MIN_CASH_TOTAL = 10000   # cash total minimo del partido
SOLO_CONSENSO = False    # True = guarda en el CSV solo filas con consenso

# Hacia donde va el dinero: el lado alertado debe tener la mayor parte del
# "ALL Cash" entre los 3 resultados (local / empate / visitante)
EXIGIR_LADO_DINERO = True   # True = obligatorio; False = solo suma puntos
MIN_CASH_PCT = 40           # % minimo del cash total que debe tener ese lado

# Alertas
ALERTAR_EMPATE = True    # True = tambien alerta si el dinero va al empate

# Alerta por dinero: se dispara si un lado (local, visitante o empate) tiene
# >= ALERTA_CASH_PCT del "ALL Cash", SIN importar consenso, puntaje, ratio ni cuota.
ALERTA_CASH_ACTIVA = True
ALERTA_CASH_PCT = 60            # % minimo del ALL Cash
ALERTA_CASH_VENTANA_MIN = 20    # minutos antes del inicio (sube este numero para recibirla antes)
ALERTA_CASH_REALERTA = 10       # vuelve a alertar si el % sube N puntos mas
ALERTA_CASH_MAX = 10            # maximo de alertas de este tipo por captura

# Resumen diario: una vez al dia, a HORA_RESUMEN (hora de Bogota), envia en un solo mensaje
# los partidos del DIA SIGUIENTE que cumplen el consenso o tienen ALL Cash >= ALERTA_CASH_PCT.
RESUMEN_ACTIVO = True
HORA_RESUMEN = "21:20"          # HH:MM, hora de Bogota
RESUMEN_DIAS_ADELANTE = 1       # 1 = partidos de manana
RESUMEN_ESTADO = os.path.join(ESTADO_DIR, "resumen_enviado.json")
RESUMEN_VENTANA_MIN = int(os.getenv("RESUMEN_VENTANA_MIN", "0"))   # 0 = sin limite; en Actions: 12
MIN_SCORE = 0            # puntaje minimo (0-100). 0 = alerta todo consenso y el puntaje solo ordena/informa
CUOTA_MIN = 1.30         # no alertar cuotas mas comprimidas
MAX_ALERTAS = 10         # maximo por captura (varios partidos comparten hora)
VENTANA_MIN = 20         # alerta solo cuando faltan entre 0 y N minutos para el inicio
                         # (mantener VENTANA_MIN > INTERVALO/60 para no perder partidos)
REALERTA_SUBE = 15       # volver a alertar si el puntaje sube >= N puntos
DIV_TOTALES = 20         # divergencia minima (pts) dinero vs publico en Over/Under

# Telegram: pega aqui tu token (BotFather) y el id del chat.
# Si defines las variables de entorno TG_TOKEN / TG_CHAT, esas tienen prioridad.
TG_TOKEN = os.getenv("TG_TOKEN") or "PEGA_AQUI_TU_TOKEN"
TG_CHAT = os.getenv("TG_CHAT") or "-1003887899315"

CAMPOS = ["captura_utc", "game_id", "gdate_edt", "min_para", "liga", "team_idx",
          "team1", "team2", "equipo_fila", "odd1_attr", "odd2_attr", "ml_open", "ml_cur",
          "drop_1", "drop_3", "drop_6", "drop_12", "drop_24", "drop_OP",
          "tickets_all", "tickets_ml", "cash_all_pct", "cash_ml_pct",
          "cash_all_amt", "cash_ml_amt", "cash_ratio", "div_ml",
          "pub_over", "pub_under", "cash_over", "cash_under", "div_over",
          "cash_diff", "tot_diff", "t_tickets", "t_cash", "c_pc_diff", "c_diff",
          "marcador", "vegas_trap", "lr_signals", "prepartido",
          "lado_dinero", "es_lado_dinero", "dif_cash_lado", "consenso", "puntaje"]


# ---------------- Utilidades ----------------
def ahora_edt():
    return datetime.now(NY).replace(tzinfo=None)


def fechas_edt():
    base = ahora_edt()
    return [(base + timedelta(days=d)).strftime("%Y-%m-%d")
            for d in range(-DIAS_ATRAS, DIAS_ADELANTE + 1)]


def txt(el):
    return el.get_text(" ", strip=True) if el else ""


def num(patron, s):
    m = re.search(patron, s)
    return float(m.group(1)) if m else ""


def par(patron, s):
    m = re.search(patron, s)
    return (float(m.group(1)), float(m.group(2))) if m else ("", "")


def monto(prefijo, s):
    """Monto de cash (numero NO seguido de %). Ej: 'ALL Cash: 594068'."""
    for m in re.finditer(prefijo + r"\s*(-?\d+(?:\.\d+)?)(%?)", s):
        if not m.group(2):
            return float(m.group(1))
    return ""


def a_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def liga_ok(liga):
    l = (liga or "").lower()
    if LIGAS_INCLUIR and not any(x in l for x in LIGAS_INCLUIR):
        return False
    if any(x in l for x in LIGAS_EXCLUIR):
        return False
    return True


# ---------------- Descarga ----------------
def sesion():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/155.0.0.0 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://zcodesystem.com",
        "Referer": HOME,
        "Accept": "application/json, text/javascript, */*; q=0.01"})
    s.get(HOME, timeout=30)
    s.cookies.update({"TCB_SPORT_N1": "SOCCER", "TICKETS_RANKS_N1": "1",
                      "TICKETS_PUBLICPC_TYPE_N1": "all"})
    return s


def pedir(s, fecha):
    r = s.post(URL, data={"current_sport": "SOCCER", "current_sport2": "",
                          "current_date": fecha, "only_selected_game": 0,
                          "force_get_json": 1, "lock_test": 0, "format": 618},
               timeout=60)
    return r.json()


def diagnostico(data, html, fecha):
    """Avisa si el HTML trae menos partidos de los que declaran los bloques."""
    try:
        esperados = sum(c["length"] for c in data["time_chunks"]["chunks"])
    except Exception:
        return
    ids = set(re.findall(r'data-game-id="(\d+)"', html))
    if len(ids) < esperados:
        print(f"AVISO {fecha}: el HTML trae {len(ids)} partidos pero los bloques "
              f"declaran {esperados}. Puede faltar contenido (carga por bloques).")


# ---------------- Parseo ----------------
def filas(html, ahora_utc, ahora_e):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for g in soup.select("div.game[data-game-id]"):
        if "TeamData" in g.get("class", []):
            continue
        a = g.attrs
        if not liga_ok(a.get("data-league")):
            continue

        mls = [x.get_text(strip=True) for x in g.select(".MoneyLine")]
        if any("Unlock" in m for m in mls):
            continue  # bloqueado (solo VIP)

        drops = {x.get("data-interval"): x.get("data-value")
                 for x in g.select(".ML .DroppingOdd")}
        tk = " | ".join(txt(x) for x in g.select(".PublicTickets > div")
                        if "Short" not in x.get("class", []))
        ch = " | ".join(txt(x) for x in g.select(".PublicCash > div")
                        if "Short" not in x.get("class", []))

        ratio = num(r"Cash Ratio:\s*(\d+(?:\.\d+)?)", ch)
        drop_op = a_float(drops.get("OP"))
        t_cash = a_float(a.get("data-t_cash")) or 0

        tickets_all = num(r"ALL public:\s*(\d+(?:\.\d+)?)%", tk)
        cash_all_pct = num(r"ALL Cash:\s*(\d+(?:\.\d+)?)%", ch)
        div_ml = (cash_all_pct - tickets_all) if "" not in (cash_all_pct, tickets_all) else ""

        pub_over, pub_under = par(r"Public Over:\s*(\d+(?:\.\d+)?)%,?\s*Under:\s*(\d+(?:\.\d+)?)%", tk)
        cash_over, cash_under = par(r"Cash Over:\s*(\d+(?:\.\d+)?)%,?\s*Under:\s*(\d+(?:\.\d+)?)%", ch)
        div_over = (cash_over - pub_over) if "" not in (cash_over, pub_over) else ""

        minutos, prepartido = "", ""
        try:
            gd = datetime.strptime(a.get("data-gdate"), FMT)
            minutos = round((gd - ahora_e).total_seconds() / 60)
            prepartido = 1 if minutos > 0 else 0
        except Exception:
            pass

        consenso = 1 if (ratio != "" and ratio >= UMBRAL_RATIO
                         and drop_op is not None and drop_op >= MIN_DROP_OP
                         and t_cash >= MIN_CASH_TOTAL) else 0

        r = {
            "captura_utc": ahora_utc, "game_id": a.get("data-game-id"),
            "gdate_edt": a.get("data-gdate"), "min_para": minutos,
            "liga": a.get("data-league"), "team_idx": a.get("data-team-idx"),
            "team1": a.get("data-team1"), "team2": a.get("data-team2"),
            "equipo_fila": txt(g.select_one(".team")),
            "odd1_attr": a.get("data-odd1"), "odd2_attr": a.get("data-odd2"),
            "ml_open": mls[0] if mls else "", "ml_cur": mls[-1] if mls else "",
            "drop_1": drops.get("1", ""), "drop_3": drops.get("3", ""),
            "drop_6": drops.get("6", ""), "drop_12": drops.get("12", ""),
            "drop_24": drops.get("24", ""), "drop_OP": drops.get("OP", ""),
            "tickets_all": tickets_all,
            "tickets_ml": num(r"ML public:\s*(\d+(?:\.\d+)?)%", tk),
            "cash_all_pct": cash_all_pct,
            "cash_ml_pct": num(r"ML Cash:\s*(\d+(?:\.\d+)?)%", ch),
            "cash_all_amt": monto(r"ALL Cash:", ch),
            "cash_ml_amt": monto(r"ML Cash:", ch),
            "cash_ratio": ratio, "div_ml": div_ml,
            "pub_over": pub_over, "pub_under": pub_under,
            "cash_over": cash_over, "cash_under": cash_under, "div_over": div_over,
            "cash_diff": num(r"ALL cash difference:\s*(-?\d+)", ch),
            "tot_diff": num(r"Totals cash difference:\s*(-?\d+)", ch),
            "t_tickets": a.get("data-t_tickets"), "t_cash": a.get("data-t_cash"),
            "c_pc_diff": a.get("data-c_pc_diff"), "c_diff": a.get("data-c_diff"),
            "marcador": txt(g.select_one(".Score")),
            "vegas_trap": 1 if g.select_one(".VegasTrap") else 0,
            "lr_signals": " ".join(txt(p) for p in g.select(".LRSignals .Text p")),
            "lado_dinero": "", "es_lado_dinero": "", "dif_cash_lado": "",
            "prepartido": prepartido, "consenso": consenso, "puntaje": 0}
        out.append(r)
    return out


def enriquecer(rows):
    """Hacia donde va el dinero. Se calcula con las 3 filas del partido juntas
    (1 = local, 2 = visitante, 3 = empate), por eso va despues de parsear."""
    por_juego = {}
    for r in rows:
        por_juego.setdefault(r["game_id"], {})[r["team_idx"]] = r

    for filas_j in por_juego.values():
        pcts = {k: v["cash_all_pct"] for k, v in filas_j.items()
                if k in ("1", "2", "3") and v["cash_all_pct"] != ""}
        mayor = max(pcts.values()) if pcts else None
        lado = ",".join(sorted(k for k, p in pcts.items() if p == mayor)) if pcts else ""

        for k, r in filas_j.items():
            r["lado_dinero"] = lado
            r["es_lado_dinero"] = 1 if (mayor is not None and pcts.get(k) == mayor) else 0

            # Ventaja de cash de este lado sobre su rival, calculada con los MONTOS
            # (local vs visitante). No dependo del signo de 'ALL cash difference'.
            rival = filas_j.get("2" if k == "1" else "1") if k in ("1", "2") else None
            if rival and r["cash_all_amt"] != "" and rival["cash_all_amt"] != "":
                r["dif_cash_lado"] = r["cash_all_amt"] - rival["cash_all_amt"]

            # El consenso exige que el dinero realmente vaya hacia este lado
            if EXIGIR_LADO_DINERO and r["consenso"] == 1:
                pct = r["cash_all_pct"]
                if not r["es_lado_dinero"] or pct == "" or pct < MIN_CASH_PCT:
                    r["consenso"] = 0

            r["puntaje"] = puntaje(r)


# ---------------- Puntaje ----------------
def puntaje(r):
    """Prioridad 0-100. Pesos arbitrarios: ajustalos con tu historico.
    Ratio 25 | caida de cuota 15 | dinero-publico 15 | volumen 10 |
    parte del dinero total 15 | zona de cuota 10 | sin Vegas Trap 10"""
    ratio = r["cash_ratio"] if r["cash_ratio"] != "" else 0
    drop = a_float(r["drop_OP"]) or 0
    cash = r["cash_all_amt"] if r["cash_all_amt"] != "" else 0
    div = r["div_ml"] if r["div_ml"] != "" else 0
    pct = r["cash_all_pct"] if r["cash_all_pct"] != "" else 0
    cuota = a_float(r["ml_cur"]) or 0

    p = min(ratio, 10) / 10 * 25              # dinero vs publico (ratio)
    p += min(max(drop, 0), 20) / 20 * 15      # cuanto bajo la cuota desde apertura
    p += min(max(div, 0), 40) / 40 * 15       # % dinero - % apuestas
    p += min(cash / 100000, 1) * 10           # volumen
    if r["es_lado_dinero"] == 1:              # el dinero va hacia este lado
        p += min(pct, 70) / 70 * 15
    if 1.8 <= cuota <= 4.0:                   # zona con margen de valor
        p += 10
    elif cuota >= CUOTA_MIN:
        p += 4
    if r["vegas_trap"] == 0:
        p += 10
    return round(p)


# ---------------- Telegram ----------------
def enviar_telegram(texto):
    if not TG_TOKEN or TG_TOKEN.startswith("PEGA_AQUI") or not TG_CHAT:
        print("Telegram sin configurar: pega tu token en TG_TOKEN al inicio del archivo")
        return False
    for _ in range(3):
        resp = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                             data={"chat_id": TG_CHAT, "text": texto, "parse_mode": "HTML"},
                             timeout=30)
        if resp.status_code == 200:
            return True
        if resp.status_code == 429:
            time.sleep(resp.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        print("Telegram error:", resp.status_code, resp.text[:200])
        return False
    return False


def hora_bogota(gdate):
    try:
        d = datetime.strptime(gdate, FMT).replace(tzinfo=NY)
        return d.astimezone(BOG).strftime("%d/%m %H:%M")
    except Exception:
        return gdate


def fmt(x, nd=0):
    return "-" if x == "" or x is None else (f"{x:.{nd}f}" if isinstance(x, float) else str(x))


def mensaje(r, s, emp):
    nivel = "🔥" if s >= 75 else "⚡" if s >= 65 else "⚽"
    lineas = [
        f"{nivel} <b>{_html.escape(r['team1'])} vs {_html.escape(r['team2'])}</b>",
        f"{_html.escape(r['liga'] or '')} · {hora_bogota(r['gdate_edt'])} Bogotá "
        f"(<b>empieza en {fmt(r['min_para'])} min</b>)",
        f"Lado: <b>{_html.escape(r['equipo_fila'])}</b> @ {r['ml_cur']} "
        f"(apertura {r['ml_open']}, -{r['drop_OP']}%)",
        f"Cash Ratio: <b>{fmt(r['cash_ratio'], 2)}</b> · Público {fmt(r['tickets_all'])}% "
        f"vs Dinero {fmt(r['cash_all_pct'])}% (dif {fmt(r['div_ml'])} pts)",
        f"Dinero: {fmt(r['cash_all_amt'])} ({fmt(r['cash_all_pct'])}% del total, "
        f"{'es el lado con más dinero' if r['es_lado_dinero'] else 'NO es el lado con más dinero'})"
        + (f" · ventaja vs rival: {r['dif_cash_lado']:+,.0f}" if r["dif_cash_lado"] != "" else ""),
        f"Empate: {fmt(emp)}% del cash · Vegas Trap: {'sí' if r['vegas_trap'] else 'no'}"]
    d = r["div_over"]
    if d != "" and abs(d) >= DIV_TOTALES:
        lado = "Over" if d > 0 else "Under"
        lineas.append(f"Totales: dinero Over {fmt(r['cash_over'])}% vs público Over "
                      f"{fmt(r['pub_over'])}% → dinero en <b>{lado}</b>")
    if r["lr_signals"]:
        lineas.append(f"Señales LR: {_html.escape(r['lr_signals'][:120])}")
    lineas.append(f"Puntaje: <b>{s}/100</b>")
    return "\n".join(lineas)


def alertar(rows):
    try:
        vistas = json.load(open(ENVIADAS))
        if isinstance(vistas, list):              # formato antiguo
            vistas = {k: 100 for k in vistas}
    except Exception:
        vistas = {}

    por_juego = {}
    for r in rows:
        por_juego.setdefault(r["game_id"], {})[r["team_idx"]] = r

    lados = ("1", "2", "3") if ALERTAR_EMPATE else ("1", "2")
    candidatas = []
    for r in rows:
        if r["team_idx"] not in lados or r["consenso"] != 1 or r["prepartido"] != 1:
            continue
        # Solo en los minutos previos al partido
        if r["min_para"] == "" or not (0 < r["min_para"] <= VENTANA_MIN):
            continue
        if (a_float(r["ml_cur"]) or 0) < CUOTA_MIN:
            continue
        s = r["puntaje"]
        if s < MIN_SCORE:
            continue
        clave = f'{r["game_id"]}-{r["team_idx"]}'
        if clave in vistas and s < vistas[clave] + REALERTA_SUBE:
            continue
        candidatas.append((s, clave, r))

    candidatas.sort(key=lambda x: -x[0])
    for s, clave, r in candidatas[:MAX_ALERTAS]:
        emp = por_juego[r["game_id"]].get("3", {}).get("cash_all_pct", "")
        if enviar_telegram(mensaje(r, s, emp)):
            vistas[clave] = s
        time.sleep(0.6)
    json.dump(vistas, open(ENVIADAS, "w"))


def mensaje_dinero(r, emp):
    drop = a_float(r["drop_OP"])
    mov = "" if drop is None else (f" · cuota {'bajó' if drop > 0 else 'subió'} {abs(drop):g}%"
                                   if drop != 0 else " · cuota sin cambio")
    lineas = [
        f"💰 <b>ALERTA DE DINERO · {fmt(r['cash_all_pct'])}% del ALL Cash</b>",
        f"<b>{_html.escape(r['team1'])} vs {_html.escape(r['team2'])}</b>",
        f"{_html.escape(r['liga'] or '')} · {hora_bogota(r['gdate_edt'])} Bogotá "
        f"(<b>empieza en {fmt(r['min_para'])} min</b>)",
        f"Lado: <b>{_html.escape(r['equipo_fila'])}</b> @ {r['ml_cur']} "
        f"(apertura {r['ml_open']}{mov})",
        f"Dinero: {fmt(r['cash_all_amt'])} · Público: {fmt(r['tickets_all'])}% · "
        f"Cash Ratio: {fmt(r['cash_ratio'], 2)}"]
    if r["dif_cash_lado"] != "":
        lineas.append(f"Ventaja de cash vs rival: {r['dif_cash_lado']:+,.0f}")
    if r["team_idx"] in ("1", "2"):
        lineas.append(f"Empate: {fmt(emp)}% del cash")
    lineas.append(f"Vegas Trap: {'sí' if r['vegas_trap'] else 'no'}")
    return "\n".join(lineas)


def alertar_dinero(rows):
    """Alerta independiente: un lado con >= ALERTA_CASH_PCT del ALL Cash."""
    if not ALERTA_CASH_ACTIVA:
        return
    try:
        vistas = json.load(open(ENVIADAS))
        if isinstance(vistas, list):
            vistas = {k: 100 for k in vistas}
    except Exception:
        vistas = {}

    por_juego = {}
    for r in rows:
        por_juego.setdefault(r["game_id"], {})[r["team_idx"]] = r

    cand = []
    for r in rows:
        pct = r["cash_all_pct"]
        if r["team_idx"] not in ("1", "2", "3") or pct == "" or pct < ALERTA_CASH_PCT:
            continue
        if r["prepartido"] != 1 or r["min_para"] == "" \
                or not (0 < r["min_para"] <= ALERTA_CASH_VENTANA_MIN):
            continue
        clave = f'{r["game_id"]}-{r["team_idx"]}-cash'
        if clave in vistas and pct < vistas[clave] + ALERTA_CASH_REALERTA:
            continue
        cand.append((pct, clave, r))

    cand.sort(key=lambda x: -x[0])
    for pct, clave, r in cand[:ALERTA_CASH_MAX]:
        emp = por_juego[r["game_id"]].get("3", {}).get("cash_all_pct", "")
        if enviar_telegram(mensaje_dinero(r, emp)):
            vistas[clave] = pct
        time.sleep(0.6)
    json.dump(vistas, open(ENVIADAS, "w"))


# ---------------- Resumen diario ----------------
def partidos_del_dia(s, dia_bog):
    """Descarga y parsea los partidos que se juegan en la fecha dia_bog (hora de Bogota).
    Un dia de Bogota puede tocar 2 fechas de Nueva York (con horario de verano), se piden ambas."""
    ahora_utc = datetime.now(timezone.utc).strftime(FMT)
    ahora_e = ahora_edt()
    fechas_ny = [dia_bog.strftime("%Y-%m-%d"), (dia_bog + timedelta(days=1)).strftime("%Y-%m-%d")]
    todas = {}
    for f in fechas_ny:
        data = pedir(s, f)
        for r in filas(data["html"], ahora_utc, ahora_e):
            todas[(r["game_id"], r["team_idx"])] = r
    rows = list(todas.values())
    enriquecer(rows)

    out = []
    for r in rows:
        try:
            ko = datetime.strptime(r["gdate_edt"], FMT).replace(tzinfo=NY).astimezone(BOG)
        except Exception:
            continue
        if ko.date() == dia_bog:
            r["_ko_bog"] = ko
            out.append(r)
    return out


def linea_resumen(r, marca=""):
    drop = a_float(r["drop_OP"])
    mov = "" if not drop else f" · {'↓' if drop > 0 else '↑'}{abs(drop):g}%"
    return (f"{r['_ko_bog'].strftime('%H:%M')} {marca}<b>{_html.escape(r['equipo_fila'])}</b> "
            f"@{r['ml_cur']}{mov} · cash {fmt(r['cash_all_pct'])}% · R {fmt(r['cash_ratio'], 2)}\n"
            f"    {_html.escape(r['team1'])} vs {_html.escape(r['team2'])} · "
            f"{_html.escape((r['liga'] or '')[:30])}")


def construir_resumen(rows, dia):
    """Devuelve una lista de bloques de texto (cada partido es un bloque)."""
    lados = ("1", "2", "3")

    def alto(r):
        return r["cash_all_pct"] != "" and r["cash_all_pct"] >= ALERTA_CASH_PCT

    orden = lambda x: x["_ko_bog"]
    cons = sorted((r for r in rows if r["team_idx"] in lados and r["consenso"] == 1), key=orden)
    ids_cons = {(r["game_id"], r["team_idx"]) for r in cons}
    dinero = sorted((r for r in rows if r["team_idx"] in lados and alto(r)
                     and (r["game_id"], r["team_idx"]) not in ids_cons), key=orden)

    bloques = [f"📋 <b>Partidos del {dia.strftime('%d/%m')}</b> · resumen de las {HORA_RESUMEN} (Bogotá)",
               f"✅ <b>CONSENSO ({len(cons)})</b>"]
    bloques += [linea_resumen(r, "💰 " if alto(r) else "") for r in cons] or ["   (ninguno)"]
    bloques.append(f"💰 <b>ALL CASH ≥ {ALERTA_CASH_PCT}% ({len(dinero)})</b> · sin consenso")
    bloques += [linea_resumen(r) for r in dinero] or ["   (ninguno)"]
    bloques.append("R = Cash Ratio · ↓ = cuota bajó desde la apertura · 💰 = también ≥ "
                   f"{ALERTA_CASH_PCT}% de cash")
    return bloques


def enviar_en_partes(bloques, limite=3800):
    """Un solo mensaje; solo se divide si pasa el limite de Telegram (4096 caracteres)."""
    partes, actual = [], ""
    for b in bloques:
        if actual and len(actual) + len(b) + 1 > limite:
            partes.append(actual)
            actual = ""
        actual += ("\n" if actual else "") + b
    if actual:
        partes.append(actual)

    ok = True
    for i, p in enumerate(partes, 1):
        if len(partes) > 1:
            p = f"{p}\n\n(parte {i}/{len(partes)})"
        ok = enviar_telegram(p) and ok
        time.sleep(1)
    return ok, len(partes)


def _hora_resumen():
    hh, mm = HORA_RESUMEN.split(":")
    return int(hh), int(mm)


def _resumen_enviado_hoy(hoy):
    try:
        return json.load(open(RESUMEN_ESTADO)).get("fecha") == hoy
    except Exception:
        return False


def resumen_diario(forzar=False):
    """Envia el resumen del dia siguiente. forzar=True lo envia ya, sin marcarlo como enviado."""
    if not RESUMEN_ACTIVO and not forzar:
        return
    ahora = datetime.now(BOG)
    hoy = ahora.date().isoformat()
    if not forzar:
        hh, mm = _hora_resumen()
        pasados = ahora.hour * 60 + ahora.minute - (hh * 60 + mm)
        if pasados < 0 or _resumen_enviado_hoy(hoy):
            return
        if RESUMEN_VENTANA_MIN and pasados > RESUMEN_VENTANA_MIN:
            return   # ya paso la ventana: si se pierde el estado, no reenviar toda la noche
    dia = ahora.date() + timedelta(days=RESUMEN_DIAS_ADELANTE)
    rows = partidos_del_dia(sesion(), dia)
    ok, n = enviar_en_partes(construir_resumen(rows, dia))
    if ok and not forzar:
        json.dump({"fecha": hoy}, open(RESUMEN_ESTADO, "w"))
    print(f"Resumen del {dia}: {'enviado' if ok else 'FALLO el envio'} en {n} mensaje(s)")


def espera_hasta_resumen():
    """Segundos hasta la hora del resumen si hoy aun no se envia (para no pasarse de hora).
    None = no aplica (ya paso la hora y se enviara en la proxima captura, o esta desactivado)."""
    if not RESUMEN_ACTIVO:
        return None
    ahora = datetime.now(BOG)
    hh, mm = _hora_resumen()
    objetivo = ahora.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if _resumen_enviado_hoy(ahora.date().isoformat()):
        objetivo += timedelta(days=1)
    if objetivo <= ahora:
        return None
    return (objetivo - ahora).total_seconds()


# ---------------- Captura ----------------
def capturar():
    ahora_utc = datetime.now(timezone.utc).strftime(FMT)
    ahora_e = ahora_edt()
    s = sesion()
    todas = {}
    for f in fechas_edt():
        data = pedir(s, f)
        html = data["html"]
        diagnostico(data, html, f)
        for r in filas(html, ahora_utc, ahora_e):
            todas[(r["game_id"], r["team_idx"])] = r   # evita duplicados entre dias
    rows = list(todas.values())
    enriquecer(rows)   # lado con mas dinero, ventaja de cash, consenso final y puntaje

    a_guardar = [r for r in rows if r["consenso"] == 1 or not SOLO_CONSENSO]
    if GUARDAR_CSV:
        nuevo = not os.path.exists(SALIDA)
        with open(SALIDA, "a", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=CAMPOS)
            if nuevo:
                w.writeheader()
            w.writerows(a_guardar)

    try:
        alertar(rows)
    except Exception as e:
        print("error en alertas:", e)
    try:
        alertar_dinero(rows)
    except Exception as e:
        print("error en alerta de dinero:", e)

    cons = [r for r in rows if r["consenso"] == 1 and r["prepartido"] == 1]
    dinero = [r for r in rows if r["prepartido"] == 1 and r["cash_all_pct"] != ""
              and r["cash_all_pct"] >= ALERTA_CASH_PCT]
    ligas = Counter(r["liga"] for r in rows if r["team_idx"] == "1")
    print(ahora_utc, "| partidos:", len({r["game_id"] for r in rows}),
          "| ligas:", len(ligas), "| filas guardadas:", len(a_guardar),
          "| consenso prepartido:", len(cons),
          f"| lados con >={ALERTA_CASH_PCT}% cash prepartido:", len(dinero))
    resumen_consenso(cons)


def resumen_consenso(cons):
    """Lista los partidos con consenso y explica si ya entran en la ventana de alerta."""
    for r in sorted(cons, key=lambda x: x["min_para"] if x["min_para"] != "" else 10**9):
        m = r["min_para"]
        if m == "":
            estado = "sin hora"
        elif m > VENTANA_MIN:
            estado = f"espera: alerta cuando falten {VENTANA_MIN} min"
        elif r["puntaje"] < MIN_SCORE:
            estado = f"descartado: puntaje {r['puntaje']} < {MIN_SCORE}"
        elif (a_float(r["ml_cur"]) or 0) < CUOTA_MIN:
            estado = f"descartado: cuota {r['ml_cur']} < {CUOTA_MIN}"
        elif r["team_idx"] == "3" and not ALERTAR_EMPATE:
            estado = "descartado: empate desactivado"
        else:
            estado = "EN VENTANA: se alerta en esta captura"
        print(f"   - {r['liga']} | {r['team1']} vs {r['team2']} | lado: {r['equipo_fila']} "
              f"@ {r['ml_cur']} | empieza en {m} min | puntaje {r['puntaje']} | {estado}")


if __name__ == "__main__":
    modo = sys.argv[1] if len(sys.argv) > 1 else ""
    if modo == "test":
        print("OK" if enviar_telegram("✅ Prueba del bot Zcode") else "FALLO")
    elif modo == "resumen":
        resumen_diario(forzar=True)      # envia el resumen ya, para probarlo
    elif modo == "loop":
        fallos = 0
        while True:
            try:
                capturar()
                fallos = 0
            except Exception as e:
                fallos += 1
                print("error:", e)
                if fallos == 3:
                    enviar_telegram(f"⚠️ Captura Zcode falla {fallos} veces seguidas: {e}")
            try:
                resumen_diario()
            except Exception as e:
                print("error en resumen:", e)
            espera = INTERVALO
            s = espera_hasta_resumen()
            if s is not None and s < INTERVALO:
                espera = s + 1               # despierta justo a la hora del resumen
            time.sleep(espera)
    else:
        capturar()
        try:
            resumen_diario()
        except Exception as e:
            print("error en resumen:", e)
