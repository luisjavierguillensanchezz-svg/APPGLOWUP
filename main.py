# -*- coding: utf-8 -*-
"""
Gains OS — seguimiento de hipertrofia (bloque de 8 semanas) en Streamlit.

Qué incluye
-----------
* Registro INDEPENDIENTE de cada serie (peso, repeticiones, RIR, estado, nota) con
  guardado inmediato e idempotente (id estable por sesión|ejercicio|serie).
* Cronómetro de descanso calculado con marcas de tiempo (no resta segundos), con
  pausa/reanudar/reiniciar/finalizar, persistido en la base de datos y con cuenta
  regresiva en el navegador (sin recargar la página cada segundo).
* Motor de progresión doble (rango de repeticiones + RIR) con recomendación que el
  usuario confirma o modifica. Nunca sube cargas solo.
* Historial por ejercicio, comparación entre sesiones, PRs y resumen de sesión.
* Dashboard de 8 semanas, gráficas con fechas reales y ejes honestos.
* Nutrición con exceso de meta visible, sueño/recuperación, peso y cintura.
* Exportación CSV/JSON.
* La semana de entrenamiento empieza en SÁBADO (Día 1 = sábado … Día 7 = viernes).

Datos
-----
Conserva las tablas originales (meals, workouts, daily_metrics). La tabla antigua
`workouts` ya no se escribe, pero se sigue LEYENDO como historial (se asume en kg).
Si no hay credenciales de Turso se usa un SQLite local (temporal en la nube).
Si el inicio del bloque guardado no cae en sábado, se mueve automáticamente al
sábado anterior.

Secrets / variables de entorno
------------------------------
TURSO_DATABASE_URL, TURSO_AUTH_TOKEN, GEMINI_API_KEY
Opcionales: APP_TZ (default America/Mexico_City), GEMINI_MODELS (coma-separados),
            GAINS_DB_PATH (SQLite local).
"""
from __future__ import annotations

import html as _html
import inspect
import json
import math
import os
import re
import sqlite3
import threading
import time
import unicodedata
import requests
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components
from pydantic import BaseModel

try:  # modo local si no está instalado
    import libsql_client
except ImportError:  # pragma: no cover
    libsql_client = None
try:
    from google import genai
except ImportError:  # pragma: no cover
    genai = None

# =============================================================================
# 0. CONSTANTES
# =============================================================================
UID = "luis"
PROGRAM_ID = "hipertrofia-8s"
WEEKS = 8
SESSIONS_PER_WEEK = 6          # días 1-6 (el 7 es descanso total)
WEEK_START = 5                 # 0=lunes … 5=sábado: la semana empieza en sábado
KG_PER_LB = 0.45359237

C_BG, C_CARD, C_CARD2, C_BORDER = "#0E1118", "#171B25", "#1E2330", "#2A2F3F"
C_TEXT, C_MUTED = "#F5F7FA", "#A0A6B8"
C_CORAL, C_BLUE, C_ORANGE, C_GREEN = "#FF5A5F", "#4DA3FF", "#FF9F43", "#34D399"

MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
         "septiembre", "octubre", "noviembre", "diciembre"]
MESES_AB = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

try:
    _ST_VER = tuple(int(z) for z in re.findall(r"\d+", st.__version__)[:2])
except Exception:  # pragma: no cover
    _ST_VER = (0, 0)
MATERIAL = _ST_VER >= (1, 36)   # iconos Material en botones y etiquetas


def _mi(name):
    return f":material/{name}: " if MATERIAL else ""


NAV = [_mi("home") + "Inicio", _mi("restaurant") + "Comida", _mi("fitness_center") + "Rutina",
       _mi("monitoring") + "Progreso", _mi("tune") + "Plan"]
RIR_OPTS = [0, 1, 2, 3, 4, 5]
REST_BY_KIND = {"heavy": 240, "compound": 180, "iso": 90, "activity": 0}

# =============================================================================
# 1. PLAN DE ENTRENAMIENTO (8 semanas, progresión doble)
#    Series / rangos / RIR son valores iniciales editables aquí.
#    El orden de DAYS sigue la semana: Día 1 = sábado, Día 2 = domingo, …,
#    Día 7 = viernes.
# =============================================================================
def _ex(name, group, muscle, sets, rmin, rmax, rir, kind, inc=2.5, rest=None):
    return dict(name=name, group=group, muscle=muscle, sets=sets, rmin=rmin, rmax=rmax,
                rir=rir, kind=kind, inc=inc, rest=REST_BY_KIND[kind] if rest is None else rest)


DAYS = {
    "Día 1: UPPER": dict(slug="d1", exercises=[
        _ex("Press inclinado con mancuernas", "Pecho", "Pecho (porción clavicular)", 3, 8, 10, 2, "compound", 2.0),
        _ex("Remo con pecho apoyado", "Espalda", "Espalda media", 3, 8, 10, 2, "compound"),
        _ex("Press de banca con barra", "Pecho", "Pecho", 3, 6, 8, 2, "compound"),
        _ex("Jalón al pecho agarre neutro", "Espalda", "Dorsales", 3, 8, 12, 2, "compound"),
        _ex("Elevación lateral en polea", "Hombros", "Deltoide lateral", 3, 12, 15, 1, "iso", 1.25),
        _ex("Apertura inversa en máquina", "Hombros", "Deltoide posterior", 3, 12, 15, 1, "iso", 2.5),
        _ex("Curl de bíceps en polea", "Bíceps", "Bíceps", 3, 10, 12, 1, "iso", 2.5),
        _ex("Extensión de tríceps en polea", "Tríceps", "Tríceps", 3, 10, 12, 1, "iso", 2.5),
    ]),
    "Día 2: LEGS A": dict(slug="d2", exercises=[
        _ex("Sentadilla con barra", "Cuádriceps", "Cuádriceps / glúteo", 4, 5, 8, 2, "heavy"),
        _ex("Prensa de piernas", "Cuádriceps", "Cuádriceps", 3, 8, 12, 2, "compound", 5.0),
        _ex("Curl femoral sentado", "Isquios/Glúteo", "Isquiotibiales", 3, 10, 12, 1, "iso"),
        _ex("Extensión de cuádriceps", "Cuádriceps", "Cuádriceps", 3, 10, 15, 1, "iso"),
        _ex("Elevación de pantorrillas de pie", "Pantorrillas", "Pantorrillas", 4, 8, 12, 1, "iso"),
        _ex("Crunch en polea", "Abdomen", "Abdomen", 3, 10, 15, 1, "iso"),
    ]),
    "Día 3: PUSH": dict(slug="d3", exercises=[
        _ex("Press de banca con barra", "Pecho", "Pecho", 3, 6, 8, 2, "compound"),
        _ex("Press inclinado en máquina", "Pecho", "Pecho (porción clavicular)", 3, 8, 10, 2, "compound"),
        _ex("Press de hombros sentado", "Hombros", "Deltoide anterior / lateral", 3, 8, 10, 2, "compound", 2.0),
        _ex("Aperturas en polea", "Pecho", "Pecho", 3, 12, 15, 1, "iso", 1.25),
        _ex("Elevaciones laterales con mancuernas", "Hombros", "Deltoide lateral", 3, 12, 20, 1, "iso", 1.0),
        _ex("Extensión de tríceps sobre la cabeza", "Tríceps", "Tríceps (cabeza larga)", 3, 10, 12, 1, "iso"),
        _ex("Jalón de tríceps con cuerda", "Tríceps", "Tríceps", 3, 10, 15, 1, "iso"),
    ]),
    "Día 4: PULL": dict(slug="d4", exercises=[
        _ex("Jalón al pecho agarre neutro", "Espalda", "Dorsales", 3, 8, 12, 2, "compound"),
        _ex("Remo en polea baja", "Espalda", "Espalda media", 3, 8, 12, 2, "compound"),
        _ex("Remo unilateral", "Espalda", "Dorsales / espalda media", 3, 8, 12, 2, "compound", 2.0),
        _ex("Pullover en polea", "Espalda", "Dorsales", 3, 10, 15, 1, "iso", 2.5),
        _ex("Apertura inversa en máquina", "Hombros", "Deltoide posterior", 3, 12, 15, 1, "iso"),
        _ex("Curl predicador barra EZ", "Bíceps", "Bíceps", 3, 8, 12, 1, "iso", 1.25),
        _ex("Curl martillo", "Bíceps", "Braquial / braquiorradial", 3, 10, 12, 1, "iso", 2.0),
    ]),
    "Día 5: LEGS B": dict(slug="d5", exercises=[
        _ex("Peso muerto rumano", "Isquios/Glúteo", "Isquiotibiales / glúteo", 3, 6, 10, 2, "heavy"),
        _ex("Hip thrust", "Isquios/Glúteo", "Glúteo", 3, 8, 12, 2, "compound", 5.0),
        _ex("Curl femoral tumbado", "Isquios/Glúteo", "Isquiotibiales", 3, 10, 12, 1, "iso"),
        _ex("Sentadilla búlgara", "Cuádriceps", "Cuádriceps / glúteo", 3, 8, 10, 2, "compound", 2.0),
        _ex("Extensión de cuádriceps", "Cuádriceps", "Cuádriceps", 3, 12, 15, 1, "iso"),
        _ex("Elevación pantorrillas sentado", "Pantorrillas", "Sóleo", 4, 10, 15, 1, "iso"),
        _ex("Elevación rodillas en silla romana", "Abdomen", "Abdomen", 3, 10, 15, 1, "iso", 2.5),
    ]),
    "Día 6: RECUPERACIÓN": dict(slug="d6", exercises=[
        _ex("Bicicleta / Caminata (15 min)", "Cardio", "Cardio suave", 1, 15, 15, 5, "activity"),
        _ex("Elevaciones laterales máquina", "Hombros", "Deltoide lateral", 3, 12, 15, 2, "iso", 2.5, rest=0),
        _ex("Aperturas en polea", "Pecho", "Pecho", 3, 12, 15, 2, "iso", 1.25, rest=0),
        _ex("Pullover en polea", "Espalda", "Dorsales", 3, 12, 15, 2, "iso", 2.5, rest=0),
        _ex("Movilidad", "Movilidad", "Movilidad general", 1, 10, 10, 5, "activity"),
    ]),
    "Día 7: DESCANSO": dict(slug="d7", exercises=[]),
}
DAY_KEYS = list(DAYS.keys())
SLUG_TO_DAY = {v["slug"]: k for k, v in DAYS.items()}
EX_GROUP = {}
for _d in DAYS.values():
    for _e in _d["exercises"]:
        EX_GROUP.setdefault(_e["name"], _e["group"])

TIPS = {
    "Press inclinado con mancuernas": "Banco a 30°. Baja controlado hasta estirar el pecho y empuja sin chocar las mancuernas arriba.",
    "Remo con pecho apoyado": "Pecho pegado al banco; lleva los codos hacia atrás y pausa 1 s arriba. Sin impulso de cadera.",
    "Press de banca con barra": "Escápulas retraídas y pies firmes. Baja a la parte baja del pecho y empuja en línea ligeramente diagonal.",
    "Jalón al pecho agarre neutro": "Pecho arriba, jala los codos hacia las costillas. Evita balancear el torso.",
    "Elevación lateral en polea": "Ligero adelanto del torso; sube el codo, no la mano. Tensión continua en todo el recorrido.",
    "Apertura inversa en máquina": "Hombros abajo, abre llevando los codos atrás. Recorrido corto y pausa al final.",
    "Curl de bíceps en polea": "Codos fijos al costado. Sube sin balancear y baja en 2-3 s.",
    "Extensión de tríceps en polea": "Codos pegados; extiende por completo y controla el regreso.",
    "Sentadilla con barra": "Respira y braza el abdomen. Profundidad constante (la misma en cada serie) y rodillas en línea con los pies.",
    "Prensa de piernas": "Espalda baja pegada al respaldo. No bloquees las rodillas arriba; profundidad controlada.",
    "Curl femoral sentado": "Ajusta el eje con la rodilla. Contrae fuerte abajo y regresa lento.",
    "Extensión de cuádriceps": "Pausa 1 s arriba. Evita despegar la cadera del asiento.",
    "Elevación de pantorrillas de pie": "Estiramiento completo abajo y pausa de 1-2 s arriba. Sin rebotar.",
    "Crunch en polea": "Redondea la columna llevando costillas a cadera; no jales con los brazos.",
    "Press inclinado en máquina": "Asiento a la altura que alinee el agarre con la parte alta del pecho. Recorrido completo.",
    "Press de hombros sentado": "Glúteos y espalda apoyados. Empuja sin arquear la zona lumbar.",
    "Aperturas en polea": "Ligera flexión de codos fija; junta las manos al frente apretando el pecho.",
    "Elevaciones laterales con mancuernas": "Sube hasta la altura del hombro con el codo ligeramente flexionado; baja lento.",
    "Extensión de tríceps sobre la cabeza": "Codos apuntando al frente; baja hasta estirar el tríceps sin abrir los codos.",
    "Jalón de tríceps con cuerda": "Abre la cuerda al final del recorrido; codos quietos.",
    "Remo en polea baja": "Torso estable; lleva el agarre al abdomen y junta escápulas sin encorvarte.",
    "Remo unilateral": "Mano y rodilla apoyadas; tira del codo hacia la cadera y evita rotar el torso.",
    "Pullover en polea": "Brazos casi rectos; baja la barra en arco sintiendo el dorsal, sin doblar los codos de más.",
    "Curl predicador barra EZ": "Brazos bien apoyados; no bloquees del todo abajo para cuidar el tendón.",
    "Curl martillo": "Agarre neutro, codos fijos. Sube sin balancear.",
    "Peso muerto rumano": "Cadera hacia atrás, espalda neutra y barra pegada a las piernas. Baja hasta sentir el estiramiento de isquios.",
    "Hip thrust": "Mentón recogido, costillas abajo y pausa de 1 s con glúteos contraídos arriba.",
    "Curl femoral tumbado": "Cadera pegada al banco; evita despegarla para ayudarte.",
    "Sentadilla búlgara": "Torso ligeramente inclinado; el peso va sobre el pie delantero. Misma profundidad cada serie (carga por mancuerna).",
    "Elevación pantorrillas sentado": "Rodillas a 90°, pausa arriba y estiramiento completo abajo.",
    "Elevación rodillas en silla romana": "Eleva con la pelvis (retroversión), no solo con las piernas. Sin balanceo.",
    "Bicicleta / Caminata (15 min)": "Ritmo suave (podrías conversar). Es recuperación, no entrenamiento duro.",
    "Elevaciones laterales máquina": "Carga ligera y buen control; queda lejos del fallo.",
    "Movilidad": "Cadera, hombros y columna torácica; movimientos amplios y sin dolor.",
}


# =============================================================================
# 2. UTILIDADES
# =============================================================================
def secret(name, default=""):
    try:
        v = st.secrets.get(name)
    except Exception:
        v = None
    return v if v not in (None, "") else os.environ.get(name, default)


_TZ = None


def tz():
    global _TZ
    if _TZ is None:
        try:
            from zoneinfo import ZoneInfo
            _TZ = ZoneInfo(secret("APP_TZ", "America/Mexico_City"))
        except Exception:
            _TZ = timezone(timedelta(hours=-6))
    return _TZ


def now():
    return datetime.now(tz())


def today():
    return now().date()


def iso(dt=None):
    return (dt or now()).isoformat(timespec="seconds")


def iso_from_ts(ts):
    return datetime.fromtimestamp(ts, tz()).isoformat(timespec="seconds")


def parse_iso(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s))
    except Exception:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=tz())


def week_start_of(d):
    """Primer día (sábado) de la semana de entrenamiento que contiene a d."""
    return d - timedelta(days=(d.weekday() - WEEK_START) % 7)


def day_index(d):
    """Posición del día dentro de la semana: 0 = sábado (Día 1) … 6 = viernes (Día 7)."""
    return (d.weekday() - WEEK_START) % 7


def weekday_name_for(day_key):
    """Nombre del día de la semana que corresponde a cada 'Día N' del plan."""
    return DIAS_ES[(WEEK_START + DAY_KEYS.index(day_key)) % 7]


def fecha_es(d):
    return f"{DIAS_ES[d.weekday()].capitalize()} {d.day} de {MESES[d.month - 1]}"


def fecha_corta(d):
    return f"{d.day} {MESES_AB[d.month - 1]}"


def slug(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def esc(s):
    return _html.escape(str(s))


def kg_to_unit(kg, unit_):
    return kg / KG_PER_LB if unit_ == "lb" else kg


def unit_to_kg(w, unit_):
    return w * KG_PER_LB if unit_ == "lb" else w


def fmt_w(x):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{round(float(x), 2):g}"


def fmt_n(x, dec=0):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{float(x):,.{dec}f}"


def mmss(sec):
    sec = int(max(0, sec or 0))
    return f"{sec // 60}:{sec % 60:02d}"


def hms(sec):
    sec = int(max(0, sec or 0))
    h, m = sec // 3600, (sec % 3600) // 60
    return f"{h} h {m:02d} min" if h else f"{m} min"


def sign(x, dec=0, suffix=""):
    if x is None:
        return "—"
    s = "+" if x > 0 else ("−" if x < 0 else "±")
    return f"{s}{abs(x):,.{dec}f}{suffix}"


def init_state(key, default):
    if key not in st.session_state:
        st.session_state[key] = default


def H(html_str):
    """Markdown con HTML sin sangrías (evita que Markdown lo interprete como código)."""
    st.markdown(re.sub(r"\n\s*", "", html_str.strip()), unsafe_allow_html=True)


def _w_kwargs(fn):
    try:
        params = inspect.signature(fn).parameters
    except Exception:
        return {"use_container_width": True}
    return {"width": "stretch"} if "width" in params else {"use_container_width": True}


def btn_w(icon=None):
    """kwargs de ancho completo + icono Material (si la versión de Streamlit lo soporta)."""
    kw = _w_kwargs(st.button)
    if icon and MATERIAL:
        kw["icon"] = f":material/{icon}:"
    return kw


def plot_w():
    return _w_kwargs(st.plotly_chart)


def df_w():
    return _w_kwargs(st.dataframe)


def fsb_w():
    return _w_kwargs(st.form_submit_button)


def dl_w(icon=None):
    kw = _w_kwargs(st.download_button)
    if icon and MATERIAL:
        try:
            if "icon" in inspect.signature(st.download_button).parameters:
                kw["icon"] = f":material/{icon}:"
        except Exception:
            pass
    return kw


def icon_label(text_if_no_icon):
    """Etiqueta para botones solo-icono: vacía si hay iconos, texto corto si no."""
    return "" if MATERIAL else text_if_no_icon


_SVG = {
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "circle": '<circle cx="12" cy="12" r="7.5"/>',
    "up": '<path d="M12 19V5M5 12l7-7 7 7"/>',
    "down": '<path d="M12 5v14M19 12l-7 7-7-7"/>',
    "flat": '<path d="M5 12h14"/>',
    "trophy": '<path d="M8 21h8M12 17v4M7 4h10v5a5 5 0 0 1-10 0V4zM17 5h3v2a3 3 0 0 1-3 3M7 5H4v2a3 3 0 0 0 3 3"/>',
}


def ico(name, color="currentColor", size=14):
    return (f'<svg class="gx-ico" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="{color}" '
            f'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{_SVG[name]}</svg>')


def page_icon():
    """Icono de pestaña: mancuerna blanca sobre fondo coral (sin emojis)."""
    try:
        from PIL import Image, ImageDraw
        im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        dr = ImageDraw.Draw(im)
        dr.rounded_rectangle([0, 0, 63, 63], radius=16, fill=(255, 90, 95, 255))
        w = (255, 255, 255, 255)
        dr.rectangle([18, 29, 46, 35], fill=w)
        for x0 in (10, 46):
            dr.rounded_rectangle([x0, 20, x0 + 8, 44], radius=3, fill=w)
        return im
    except Exception:
        return None


# =============================================================================
# 3. BASE DE DATOS (Turso/libSQL o SQLite local) CON INTERCEPTOR DE ERRORES
# =============================================================================
SCHEMA = [
    # --- tablas originales (se conservan) ---
    """CREATE TABLE IF NOT EXISTS meals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, description TEXT,
        calories REAL NOT NULL, protein_g REAL NOT NULL, carbs_g REAL NOT NULL, fat_g REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS workouts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, exercise TEXT NOT NULL,
        sets INTEGER NOT NULL, reps INTEGER NOT NULL, weight_kg REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS daily_metrics (
        date TEXT PRIMARY KEY, sleep_hours REAL, target_calories REAL, target_protein_g REAL)""",
    # --- tablas nuevas ---
    """CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, name TEXT, created_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS settings (
        user_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT, PRIMARY KEY (user_id, key))""",
    """CREATE TABLE IF NOT EXISTS programs (
        id TEXT PRIMARY KEY, user_id TEXT, name TEXT, start_date TEXT, weeks INTEGER, active INTEGER)""",
    """CREATE TABLE IF NOT EXISTS program_weeks (
        program_id TEXT NOT NULL, week_no INTEGER NOT NULL, closed INTEGER DEFAULT 0, closed_at TEXT, notes TEXT,
        PRIMARY KEY (program_id, week_no))""",
    """CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY, user_id TEXT, program_id TEXT, date TEXT, week_no INTEGER,
        day_key TEXT, day_name TEXT, started_at TEXT, finished_at TEXT, status TEXT, notes TEXT)""",
    """CREATE TABLE IF NOT EXISTS sets_log (
        id TEXT PRIMARY KEY, user_id TEXT, session_id TEXT, date TEXT, exercise TEXT, set_no INTEGER,
        weight REAL, unit TEXT, weight_kg REAL, reps INTEGER, rir INTEGER, status TEXT,
        started_at TEXT, finished_at TEXT, rest_seconds INTEGER, rest_actual INTEGER, notes TEXT,
        created_at TEXT, updated_at TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_sets_ex ON sets_log (exercise, date)",
    "CREATE INDEX IF NOT EXISTS idx_sets_session ON sets_log (session_id)",
    """CREATE TABLE IF NOT EXISTS rest_timer (
        user_id TEXT PRIMARY KEY, state TEXT, started_at REAL, duration INTEGER, remaining REAL,
        label TEXT, set_id TEXT, ended_at REAL, updated_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS body_metrics (
        user_id TEXT NOT NULL, date TEXT NOT NULL, weight_kg REAL, waist_cm REAL, PRIMARY KEY (user_id, date))""",
    """CREATE TABLE IF NOT EXISTS recovery_log (
        user_id TEXT NOT NULL, date TEXT NOT NULL, energy INTEGER, soreness INTEGER, notes TEXT,
        PRIMARY KEY (user_id, date))""",
    """CREATE TABLE IF NOT EXISTS exercise_config (
        user_id TEXT NOT NULL, exercise TEXT NOT NULL, rest_seconds INTEGER, increment REAL, inc_unit TEXT,
        PRIMARY KEY (user_id, exercise))""",
    """CREATE TABLE IF NOT EXISTS load_targets (
        user_id TEXT NOT NULL, exercise TEXT NOT NULL, weight REAL, unit TEXT, reason TEXT,
        set_on TEXT, status TEXT, PRIMARY KEY (user_id, exercise))""",
    """CREATE TABLE IF NOT EXISTS progression_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, date TEXT, exercise TEXT, action TEXT,
        last_weight REAL, suggested_weight REAL, accepted_weight REAL, unit TEXT, reason TEXT)""",
]
ALTERS = [  # migraciones aditivas (se ignoran si la columna ya existe)
    "ALTER TABLE daily_metrics ADD COLUMN target_carbs_g REAL",
    "ALTER TABLE daily_metrics ADD COLUMN target_fat_g REAL",
    # Posponer entrenamientos
    "ALTER TABLE sessions ADD COLUMN postponed_at TEXT",
    "ALTER TABLE sessions ADD COLUMN postponed_to TEXT",
    "ALTER TABLE sessions ADD COLUMN postpone_reason TEXT",
    "ALTER TABLE sessions ADD COLUMN postpone_count INTEGER DEFAULT 0",
    "ALTER TABLE sessions ADD COLUMN away_seconds INTEGER DEFAULT 0",
]
POSTPONE_REASONS = ["Me sentí mal", "Dolor o molestia", "Falta de tiempo", "Otro motivo"]


class Result:
    __slots__ = ("rows", "cols", "lastrowid")

    def __init__(self, rows, cols, lastrowid=None):
        self.rows, self.cols, self.lastrowid = rows, cols, lastrowid


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self._client = None
        url, token = secret("TURSO_DATABASE_URL"), secret("TURSO_AUTH_TOKEN")
        if url and token and libsql_client is not None:
            self.mode = "turso"
            # Limpiamos slashes extra que causan el error
            self.url = url.replace("libsql://", "https://").rstrip("/")
            self.token = token
        else:
            self.mode = "local"
            self.conn = sqlite3.connect(secret("GAINS_DB_PATH", "gains_local.db"), check_same_thread=False)
        self.init_schema()

    @staticmethod
    def _clean(p):
        if hasattr(p, "item") and not isinstance(p, (str, bytes)):
            try:
                p = p.item()
            except Exception:
                pass
        if isinstance(p, bool):
            return int(p)
        if isinstance(p, (date, datetime)):
            return p.isoformat()
        return p

    def _get_client(self):
        if self._client is None:
            self._client = libsql_client.create_client_sync(url=self.url, auth_token=self.token)
        return self._client

    def _reset(self):
        try:
            if self._client is not None:
                self._client.close()
        except Exception:
            pass
        self._client = None

    def execute(self, sql, params=()):
        params = [self._clean(p) for p in params]
        with self.lock:
            if self.mode == "local":
                cur = self.conn.execute(sql, params)
                rows = [tuple(r) for r in cur.fetchall()] if cur.description else []
                cols = [d[0] for d in cur.description] if cur.description else []
                self.conn.commit()
                return Result(rows, cols, cur.lastrowid)

            last = None
            for _ in range(2):
                try:
                    rs = self._get_client().execute(sql, params)
                    return Result([tuple(r) for r in rs.rows], list(rs.columns),
                                  getattr(rs, "last_insert_rowid", None))
                except KeyError as e:
                    # INTERCEPTOR DE ERRORES: Si la librería falla con 'result', le preguntamos a Turso directamente el motivo
                    if "result" in str(e):
                        try:
                            u = self.url + "/v2/pipeline"
                            h = {"Authorization": f"Bearer {self.token}"}
                            b = {"requests": [{"type": "execute", "stmt": {"sql": "SELECT 1", "args": []}}]}
                            resp = requests.post(u, json=b, headers=h)
                            raise RuntimeError(f"Rechazo de Turso. Respuesta exacta del servidor: {resp.text}")
                        except Exception as req_e:
                            if "Respuesta exacta" in str(req_e):
                                raise req_e
                            raise RuntimeError(f"Turso rechazó la conexión y falló la lectura del error: {req_e}")
                    last = e
                    self._reset()
                except Exception as e:
                    last = e
                    self._reset()
            raise last

    def init_schema(self):
        for s in SCHEMA:
            self.execute(s)
        for s in ALTERS:
            try:
                self.execute(s)
            except Exception:
                pass
        self.execute("INSERT OR IGNORE INTO users (id, name, created_at) VALUES (?, ?, ?)", [UID, "Luis", iso()])
        r = self.execute("SELECT start_date FROM programs WHERE id=?", [PROGRAM_ID]).rows
        if not r:
            self.execute("INSERT INTO programs (id, user_id, name, start_date, weeks, active) VALUES (?,?,?,?,?,1)",
                         [PROGRAM_ID, UID, "Hipertrofia 8 semanas", str(week_start_of(today())), WEEKS])
        elif r[0][0]:
            # Migración: la semana ahora empieza en sábado. Si el inicio guardado no es sábado,
            # se mueve al sábado anterior para que los números de semana cuadren.
            try:
                s = date.fromisoformat(str(r[0][0]))
                if s.weekday() != WEEK_START:
                    self.execute("UPDATE programs SET start_date=? WHERE id=?", [str(week_start_of(s)), PROGRAM_ID])
            except ValueError:
                pass


SCHEMA_VERSION = 3  # súbelo cada vez que se agreguen tablas o columnas


@st.cache_resource(show_spinner=False)
def get_store(schema_version=SCHEMA_VERSION):
    # El número de versión forma parte de la caché: al subirlo se crea una conexión
    # nueva y se vuelven a correr las migraciones (ALTERS) sin reiniciar la app.
    return Store()


def db():
    return get_store(SCHEMA_VERSION)


def q(sql, params=()):
    return db().execute(sql, params).rows


def q1(sql, params=()):
    r = q(sql, params)
    return r[0] if r else None


def qdf(sql, params=()):
    rs = db().execute(sql, params)
    return pd.DataFrame(rs.rows, columns=rs.cols)


def x(sql, params=()):
    return db().execute(sql, params)


# --- versiones de caché: se incrementan en cada escritura -------------------
def ver():
    return st.session_state.get("_ver", 0)


def hver():
    return st.session_state.get("_hver", 0)


def bump(hist=False):
    st.session_state["_ver"] = ver() + 1
    if hist:
        st.session_state["_hver"] = hver() + 1


def flash(kind, msg):
    st.session_state["_flash"] = (kind, msg)


def flash_show():
    f = st.session_state.pop("_flash", None)
    if f:
        {"warning": st.warning, "error": st.error, "success": st.success}.get(f[0], st.info)(f[1])


# =============================================================================
# 4. AJUSTES, PROGRAMA, METAS
# =============================================================================
DEFAULT_SETTINGS = {"unit": "kg", "auto_rest": "1", "sound": "1", "name": "Luis"}


def settings():
    if "_settings" not in st.session_state:
        d = dict(DEFAULT_SETTINGS)
        for k, v in q("SELECT key, value FROM settings WHERE user_id=?", [UID]):
            d[k] = v
        st.session_state["_settings"] = d
    return st.session_state["_settings"]


def set_setting(key, value):
    x("INSERT INTO settings (user_id, key, value) VALUES (?,?,?) "
      "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value", [UID, key, str(value)])
    settings()[key] = str(value)


def unit():
    return settings().get("unit", "kg")


def program_start():
    if "_pstart" not in st.session_state:
        r = q1("SELECT start_date FROM programs WHERE id=?", [PROGRAM_ID])
        st.session_state["_pstart"] = date.fromisoformat(r[0]) if r and r[0] else week_start_of(today())
    return st.session_state["_pstart"]


def week_number(d, start=None):
    return (d - (start or program_start())).days // 7 + 1


def suggested_day():
    """Sábado → Día 1, domingo → Día 2, …, viernes → Día 7."""
    return DAY_KEYS[day_index(today())]


def ex_cfg():
    if "_excfg" not in st.session_state:
        st.session_state["_excfg"] = {
            r[0]: dict(rest=r[1], inc=r[2], inc_unit=r[3])
            for r in q("SELECT exercise, rest_seconds, increment, inc_unit FROM exercise_config WHERE user_id=?", [UID])}
    return st.session_state["_excfg"]


def load_targets():
    if "_targets" not in st.session_state:
        st.session_state["_targets"] = {
            r[0]: dict(weight=r[1], unit=r[2], reason=r[3], set_on=r[4], status=r[5])
            for r in q("SELECT exercise, weight, unit, reason, set_on, status FROM load_targets WHERE user_id=?", [UID])}
    return st.session_state["_targets"]


def default_inc(ex, unit_):
    if unit_ == "kg":
        return float(ex["inc"])
    return 5.0 if ex["inc"] >= 2.5 else 2.5


def rest_for(ex):
    cfg = ex_cfg().get(ex["name"])
    return int(cfg["rest"]) if cfg and cfg.get("rest") is not None else int(ex["rest"])


def inc_for(ex, unit_):
    cfg = ex_cfg().get(ex["name"])
    if cfg and cfg.get("inc") and cfg.get("inc_unit") == unit_:
        return float(cfg["inc"])
    return default_inc(ex, unit_)


def get_targets(d):
    r = q1("SELECT target_calories, target_protein_g, target_carbs_g, target_fat_g FROM daily_metrics "
           "WHERE date<=? AND target_calories IS NOT NULL ORDER BY date DESC LIMIT 1", [str(d)])
    t = dict(cal=2700.0, prot=160.0, carbs=350.0, fat=70.0)
    if r:
        t["cal"] = float(r[0] or t["cal"])
        t["prot"] = float(r[1] or t["prot"])
        t["carbs"] = float(r[2] or t["carbs"])
        t["fat"] = float(r[3] or t["fat"])
    return t


# =============================================================================
# 5. LÓGICA PURA (testeable): sesiones, progresión, comparación, ejes
# =============================================================================
def summarize_sessions(df, unit_="kg"):
    """df con columnas session_id, date, set_no, weight_kg, reps, rir -> lista de sesiones ordenada."""
    if df is None or len(df) == 0:
        return []
    d = df.copy()
    d["weight_kg"] = pd.to_numeric(d["weight_kg"], errors="coerce")
    d["reps"] = pd.to_numeric(d["reps"], errors="coerce")
    d = d.dropna(subset=["weight_kg", "reps"])
    if d.empty:
        return []
    d["w"] = d["weight_kg"].map(lambda k: round(kg_to_unit(float(k), unit_), 2))
    d["date"] = pd.to_datetime(d["date"])
    out = []
    for sid, g in d.groupby("session_id", sort=False):
        g = g.sort_values("set_no")
        sets = []
        for _, r in g.iterrows():
            rir = None if pd.isna(r["rir"]) else int(r["rir"])
            sets.append(dict(w=float(r["w"]), reps=int(r["reps"]), rir=rir))
        cnt = Counter(s["w"] for s in sets)
        weight = max(cnt.items(), key=lambda kv: (kv[1], kv[0]))[0]
        rirs = [s["rir"] for s in sets if s["rir"] is not None]
        top = max(s["w"] for s in sets)
        out.append(dict(
            sid=sid, date=g["date"].iloc[0].date(), sets=sets, weight=weight, n_sets=len(sets),
            reps_total=sum(s["reps"] for s in sets),
            volume=round(sum(s["w"] * s["reps"] for s in sets), 1),
            avg_rir=(sum(rirs) / len(rirs)) if rirs else None,
            top=top, top_reps=max(s["reps"] for s in sets if s["w"] == top)))
    out.sort(key=lambda s: (s["date"], str(s["sid"])))
    return out


def _fatigue(sessions):
    vols = [s["volume"] for s in sessions[-3:]]
    return len(vols) == 3 and vols[0] > vols[1] > vols[2]


def recommend(sessions, rmin, rmax, target_sets, target_rir, inc):
    """Progresión doble. Devuelve dict(action, weight, last_weight, reason, fatigue)."""
    if not sessions:
        return dict(action="no_data", weight=None, last_weight=None, fatigue=False,
                    reason="Sin registros previos de este ejercicio: elige una carga de prueba con la que "
                           "termines las series cerca del RIR objetivo.")
    last = sessions[-1]
    w = last["weight"]
    base = dict(last_weight=w, fatigue=_fatigue(sessions))
    work = [s for s in last["sets"] if abs(s["w"] - w) < 1e-6]
    n = len(work)
    reps = [s["reps"] for s in work]
    rirs = [s["rir"] for s in work]

    def keep(reason):
        return dict(action="maintain", weight=w, reason=reason, **base)

    if n < target_sets:
        return keep(f"En la última sesión hiciste {n} de {target_sets} series con {fmt_w(w)}. "
                    f"Completa todas las series con esa carga antes de subir.")
    if min(reps) < rmin:
        return keep(f"Alguna serie quedó por debajo de {rmin} repeticiones. Mantén {fmt_w(w)} hasta "
                    f"lograr al menos {rmin} en todas.")
    if any(r is not None and r < target_rir for r in rirs):
        return keep(f"En alguna serie terminaste más cerca del fallo que el RIR objetivo ({target_rir}). "
                    f"No subas la carga todavía.")
    if all(r >= rmax for r in reps):
        if any(r is None for r in rirs):
            return keep(f"Llegaste al tope de {rmax} repeticiones, pero falta registrar el RIR. "
                        f"Confirma que quedaban ≥{target_rir} repeticiones en reserva antes de subir.")
        return dict(action="increase", weight=round(w + inc, 2), reason=(
            f"Alcanzaste {rmax} repeticiones en las {n} series con RIR ≥ {target_rir}. "
            f"Si la técnica fue buena, sube {fmt_w(inc)}."), **base)
    top = sum(r >= rmax for r in reps)
    return keep(f"{top} de {n} series llegaron a {rmax} repeticiones. Mantén {fmt_w(w)} e intenta llegar "
                f"al tope en todas las series.")


def session_from_sets(sets):
    """Arma una 'sesión' (mismo formato que summarize_sessions) a partir de series en curso."""
    sets = [dict(w=float(z["w"]), reps=int(z["reps"]), rir=z["rir"]) for z in sets]
    if not sets:
        return None
    cnt = Counter(z["w"] for z in sets)
    return dict(sets=sets, weight=max(cnt.items(), key=lambda kv: (kv[1], kv[0]))[0])


def overload_check(session, rmin, rmax, target_sets, target_rir):
    """¿Toca subir la carga? Evalúa la progresión doble criterio por criterio.

    Usa los mismos criterios que recommend(): todas las series prescritas con la carga
    de trabajo, todas en el tope del rango y todas con el RIR objetivo (registrado).
    """
    w = session["weight"]
    work = [z for z in session["sets"] if abs(z["w"] - w) < 1e-6]
    n = len(work)
    reps = [z["reps"] for z in work]
    rirs = [z["rir"] for z in work]
    at_top = sum(r >= rmax for r in reps)
    checks = [
        (f"Series con {fmt_w(w)}: {min(n, target_sets)} de {target_sets}", n >= target_sets),
        (f"Todas con al menos {rmin} reps", bool(reps) and min(reps) >= rmin),
        (f"Series en el tope ({rmax} reps): {min(at_top, target_sets)} de {target_sets}",
         n >= target_sets and at_top >= n),
        ("RIR registrado en todas", bool(rirs) and all(r is not None for r in rirs)),
        (f"RIR {target_rir} o más en todas", bool(rirs) and all(r is not None and r >= target_rir for r in rirs)),
    ]
    return dict(weight=w, ready=all(ok for _, ok in checks), checks=checks, at_top=at_top, n=n)


def compare_sessions(prev, cur):
    d = dict(dw=cur["weight"] - prev["weight"], dreps=cur["reps_total"] - prev["reps_total"],
             dvol=cur["volume"] - prev["volume"],
             dvol_pct=((cur["volume"] / prev["volume"] - 1) * 100) if prev["volume"] else None,
             dsets=cur["n_sets"] - prev["n_sets"], drir=None, notes=[])
    if cur["avg_rir"] is not None and prev["avg_rir"] is not None:
        d["drir"] = cur["avg_rir"] - prev["avg_rir"]
    n = d["notes"]
    if d["drir"] is None:
        n.append("Falta RIR en alguna de las dos sesiones: no se puede comparar el esfuerzo.")
    elif d["drir"] <= -1:
        n.append("El RIR bajó ≥1: parte de la diferencia puede deberse a mayor esfuerzo, no a mejor condición.")
    elif d["drir"] >= 1:
        n.append("El RIR subió ≥1: trabajaste más lejos del fallo, así que la comparación es favorable a la anterior.")
    if abs(d["dw"]) > 1e-6:
        n.append("La carga cambió: compara repeticiones y volumen con cuidado.")
    n.append("La app no registra técnica ni equipamiento; si cambiaste de máquina o técnica, anótalo en la serie.")
    better = d["dvol"] > 0 and (d["drir"] is None or d["drir"] >= -0.5) and d["dreps"] >= 0
    worse = d["dvol"] < 0 and (d["drir"] is None or d["drir"] <= 0.5)
    d["verdict"] = ("Mejora probable (más volumen con esfuerzo comparable)" if better and d["drir"] is not None
                    else "Más volumen, pero sin RIR no se puede confirmar" if better
                    else "Rendimiento menor" if worse else "Rendimiento similar")
    return d


def nice_range(vals, min_span_frac=0.12):
    """Rango Y legible: ni pegado a los datos ni exageradamente estrecho."""
    vals = [float(v) for v in vals if v is not None and not (isinstance(v, float) and math.isnan(v))]
    if not vals:
        return [0, 1]
    lo, hi = min(vals), max(vals)
    span = max(hi - lo, abs(hi) * min_span_frac, 1.0)
    pad = span * 0.35
    return [max(0.0, lo - pad), hi + pad]


def tick_subset(dates, max_ticks=6):
    ds = sorted(set(pd.to_datetime(list(dates))))
    if not ds:
        return []
    step = max(1, math.ceil(len(ds) / max_ticks))
    ticks = ds[::step]
    if ds[-1] not in ticks:
        ticks.append(ds[-1])
    return ticks


# --- temporizador (puro) ------------------------------------------------------
def timer_remaining(t, now_ts):
    if t["state"] in ("running", "done"):
        return t["duration"] - (now_ts - t["started_at"])
    if t["state"] == "paused":
        return t["remaining"]
    return 0.0


# =============================================================================
# 6. TEMPORIZADOR DE DESCANSO (persistente)
# =============================================================================
_TCOLS = ["state", "started_at", "duration", "remaining", "label", "set_id", "ended_at"]


def timer_row():
    r = q1("SELECT state, started_at, duration, remaining, label, set_id, ended_at FROM rest_timer WHERE user_id=?", [UID])
    if not r:
        return dict(state="idle", started_at=None, duration=0, remaining=0.0, label="", set_id=None, ended_at=None)
    t = dict(zip(_TCOLS, r))
    t["duration"] = int(t["duration"] or 0)
    t["remaining"] = float(t["remaining"] or 0)
    return t


def timer_save(t):
    x("INSERT INTO rest_timer (user_id, state, started_at, duration, remaining, label, set_id, ended_at, updated_at) "
      "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET state=excluded.state, "
      "started_at=excluded.started_at, duration=excluded.duration, remaining=excluded.remaining, "
      "label=excluded.label, set_id=excluded.set_id, ended_at=excluded.ended_at, updated_at=excluded.updated_at",
      [UID, t["state"], t["started_at"], int(t["duration"] or 0), float(t["remaining"] or 0),
       t["label"], t["set_id"], t["ended_at"], iso()])


def rest_end_guess(t, n):
    """Momento en que terminó el descanso, si se puede saber."""
    if t["state"] in ("running", "done") and t["started_at"] is not None:
        end = t["started_at"] + t["duration"]
        return end if n >= end else None
    if t["state"] == "idle" and t.get("ended_at"):
        return t["ended_at"]
    return None


def timer_finalize(reason="manual", n=None):
    """Cierra el descanso activo y guarda su duración real en la serie asociada."""
    n = n or time.time()
    t = timer_row()
    if t["state"] == "idle":
        return None
    if t["state"] == "paused":
        elapsed, end = t["duration"] - t["remaining"], (n if reason == "manual" else None)
    else:
        elapsed = n - t["started_at"]
        planned_end = t["started_at"] + t["duration"]
        end = n if reason == "manual" else (planned_end if n >= planned_end else None)
    if t["set_id"]:
        x("UPDATE sets_log SET rest_actual=?, rest_seconds=COALESCE(rest_seconds, ?) WHERE id=?",
          [int(round(max(0, elapsed))), t["duration"], t["set_id"]])
    t.update(state="idle", remaining=0.0, ended_at=end)
    timer_save(t)
    return end


def timer_start(duration, label="Descanso", set_id=None, n=None):
    n = n or time.time()
    timer_finalize("next_set", n)
    duration = int(max(5, duration))
    timer_save(dict(state="running", started_at=n, duration=duration, remaining=float(duration),
                    label=label, set_id=set_id, ended_at=None))
    if set_id:
        x("UPDATE sets_log SET rest_seconds=? WHERE id=?", [duration, set_id])


def timer_pause(n=None):
    n = n or time.time()
    t = timer_row()
    if t["state"] in ("running", "done"):
        t.update(state="paused", remaining=max(0.0, timer_remaining(t, n)))
        timer_save(t)


def timer_resume(n=None):
    n = n or time.time()
    t = timer_row()
    if t["state"] == "paused":
        t.update(state="running", started_at=n - (t["duration"] - t["remaining"]))
        timer_save(t)


def timer_restart(n=None):
    n = n or time.time()
    t = timer_row()
    if t["state"] != "idle" or t["duration"]:
        t.update(state="running", started_at=n, remaining=float(t["duration"]), ended_at=None)
        timer_save(t)


def timer_adjust(delta, n=None):
    n = n or time.time()
    t = timer_row()
    if t["state"] in ("running", "done"):
        rem = timer_remaining(t, n) + delta
        t["duration"] = int(max(5, t["duration"] + delta))
        t["state"] = "running" if rem > 0 else "done"
    elif t["state"] == "paused":
        t["duration"] = int(max(5, t["duration"] + delta))
        t["remaining"] = max(0.0, t["remaining"] + delta)
    else:
        return
    timer_save(t)


def timer_view(n=None):
    """Estado normalizado para la interfaz (marca 'done' cuando se agotó)."""
    n = n or time.time()
    t = timer_row()
    if t["state"] == "running":
        rem = timer_remaining(t, n)
        if rem <= 0:
            t["state"], t["ended_at"] = "done", t["started_at"] + t["duration"]
            timer_save(t)
            rem = 0.0
        t["rem"] = rem
    elif t["state"] == "paused":
        t["rem"] = t["remaining"]
    elif t["state"] == "done":
        t["rem"] = 0.0
    else:
        t["rem"] = 0.0
    return t


# =============================================================================
# 7. CARGA DE DATOS (con caché versionada)
# =============================================================================
_SET_COLS = ["session_id", "date", "exercise", "set_no", "weight_kg", "reps", "rir", "status", "rest_actual", "source"]


def _empty_sets():
    return pd.DataFrame(columns=_SET_COLS)


def _legacy_df(exercise=None):
    try:
        if exercise:
            df = qdf("SELECT date, exercise, sets, reps, weight_kg FROM workouts WHERE exercise=?", [exercise])
        else:
            df = qdf("SELECT date, exercise, sets, reps, weight_kg FROM workouts")
    except Exception:
        return _empty_sets()
    if df.empty:
        return _empty_sets()
    df["sets"] = pd.to_numeric(df["sets"], errors="coerce").fillna(1).astype(int).clip(lower=1, upper=20)
    df = df.loc[df.index.repeat(df["sets"])].reset_index(drop=True)
    df["session_id"] = "legacy|" + df["date"].astype(str) + "|" + df["exercise"].map(slug)
    df["set_no"] = df.groupby("session_id").cumcount() + 1
    df["rir"] = float("nan")
    df["status"] = "completed"
    df["rest_actual"] = float("nan")
    df["source"] = "legacy"
    return df[_SET_COLS]


def _app_sets(where="", params=()):
    df = qdf("SELECT session_id, date, exercise, set_no, weight_kg, reps, rir, status, rest_actual FROM sets_log "
             "WHERE user_id=? AND status='completed' " + where, [UID] + list(params))
    if df.empty:
        return _empty_sets()
    df["source"] = "app"
    return df[_SET_COLS]


def _concat(frames):
    frames = [f for f in frames if f is not None and not f.empty]
    if not frames:
        return _empty_sets()
    df = pd.concat(frames, ignore_index=True)
    for c in ("weight_kg", "reps", "rir", "set_no", "rest_actual"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date"] = pd.to_datetime(df["date"])
    return df


@st.cache_data(ttl=120, show_spinner=False)
def exercise_history(exercise, exclude_sid, hv):
    return _concat([_app_sets("AND exercise=? AND session_id<>?", [exercise, exclude_sid]), _legacy_df(exercise)])


@st.cache_data(ttl=60, show_spinner=False)
def all_sets_df(v):
    return _concat([_app_sets(), _legacy_df()])


@st.cache_data(ttl=60, show_spinner=False)
def sessions_df(v):
    df = qdf("SELECT id, date, week_no, day_key, day_name, started_at, finished_at, status FROM sessions "
             "WHERE user_id=? ORDER BY date", [UID])
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


@st.cache_data(ttl=60, show_spinner=False)
def meals_daily_df(v):
    df = qdf("SELECT date, SUM(calories) AS cal, SUM(protein_g) AS prot, SUM(carbs_g) AS carbs, SUM(fat_g) AS fat "
             "FROM meals GROUP BY date ORDER BY date")
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


@st.cache_data(ttl=60, show_spinner=False)
def body_df(v):
    df = qdf("SELECT date, weight_kg, waist_cm FROM body_metrics WHERE user_id=? ORDER BY date", [UID])
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        df["weight_kg"] = pd.to_numeric(df["weight_kg"], errors="coerce")
        df["waist_cm"] = pd.to_numeric(df["waist_cm"], errors="coerce")
    return df


@st.cache_data(ttl=60, show_spinner=False)
def recovery_df(v):
    df = qdf("SELECT d.date AS date, d.sleep_hours AS sleep, r.energy AS energy, r.soreness AS soreness, r.notes AS notes "
             "FROM daily_metrics d LEFT JOIN recovery_log r ON r.date=d.date AND r.user_id=? "
             "UNION SELECT r.date, d.sleep_hours, r.energy, r.soreness, r.notes FROM recovery_log r "
             "LEFT JOIN daily_metrics d ON d.date=r.date WHERE r.user_id=? ORDER BY 1", [UID, UID])
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        for c in ("sleep", "energy", "soreness"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


@st.cache_data(ttl=60, show_spinner=False)
def closed_weeks(v):
    return {int(r[0]) for r in q("SELECT week_no FROM program_weeks WHERE program_id=? AND closed=1", [PROGRAM_ID])}


@st.cache_data(ttl=60, show_spinner=False)
def home_data(v, today_str, start_str, day_slug):
    d = date.fromisoformat(today_str)
    start = date.fromisoformat(start_str)
    out = {"targets": get_targets(d)}
    m = q1("SELECT COALESCE(SUM(calories),0), COALESCE(SUM(protein_g),0), COALESCE(SUM(carbs_g),0), "
           "COALESCE(SUM(fat_g),0) FROM meals WHERE date=?", [today_str])
    out["macros"] = tuple(float(z) for z in m)
    r = q1("SELECT sleep_hours FROM daily_metrics WHERE date=?", [today_str])
    out["sleep"] = float(r[0]) if r and r[0] is not None else None
    wk = week_number(d, start)
    w0 = start + timedelta(days=7 * (wk - 1))
    sess = q("SELECT status FROM sessions WHERE user_id=? AND date>=? AND date<=?", [UID, str(w0), str(w0 + timedelta(days=6))])
    out["week_finished"] = sum(1 for s in sess if s[0] == "finished")
    out["week_started"] = len(sess)
    r = q1("SELECT status FROM sessions WHERE id=?", [f"{today_str}|{day_slug}"])
    out["today_status"] = r[0] if r else None
    out["last"] = q1(
        "SELECT s.date, s.day_name, s.status, "
        "(SELECT COUNT(*) FROM sets_log l WHERE l.session_id=s.id AND l.status='completed'), "
        "(SELECT COALESCE(SUM(l.weight_kg*l.reps),0) FROM sets_log l WHERE l.session_id=s.id AND l.status='completed') "
        "FROM sessions s WHERE EXISTS (SELECT 1 FROM sets_log l WHERE l.session_id=s.id AND l.status='completed') "
        "ORDER BY s.date DESC, s.started_at DESC LIMIT 1")
    r = q1("SELECT AVG(weight_kg), COUNT(*) FROM body_metrics WHERE user_id=? AND date>=? AND date<=?",
           [UID, str(d - timedelta(days=6)), today_str])
    out["body_avg"], out["body_n"] = (float(r[0]) if r and r[0] is not None else None), (r[1] if r else 0)
    r = q1("SELECT AVG(sleep_hours), COUNT(sleep_hours) FROM daily_metrics WHERE date>=? AND date<=? AND sleep_hours>0",
           [str(d - timedelta(days=6)), today_str])
    out["sleep_avg"], out["sleep_n"] = (float(r[0]) if r and r[0] is not None else None), (r[1] if r else 0)
    r = q1("SELECT energy, soreness FROM recovery_log WHERE user_id=? AND date=?", [UID, today_str])
    out["energy"], out["soreness"] = (r[0], r[1]) if r else (None, None)
    return out


# =============================================================================
# 8. SESIONES Y SERIES (escritura)
# =============================================================================
_SESS_COLS = ["id", "date", "day_key", "day_name", "started_at", "finished_at", "status",
              "postponed_at", "postponed_to", "postpone_reason", "postpone_count", "away_seconds"]


def get_session(sid):
    r = q1("SELECT " + ", ".join(_SESS_COLS) + " FROM sessions WHERE id=?", [sid])
    if not r:
        return None
    d = dict(zip(_SESS_COLS, r))
    d["away_seconds"] = int(d.get("away_seconds") or 0)
    d["postpone_count"] = int(d.get("postpone_count") or 0)
    return d


def session_active_seconds(sess, end=None):
    """Duración real de la sesión: descuenta el tiempo que estuvo pospuesta."""
    st_ = parse_iso(sess.get("started_at"))
    if not st_:
        return 0
    en = end or parse_iso(sess.get("finished_at")) or now()
    return max(0.0, (en - st_).total_seconds() - int(sess.get("away_seconds") or 0))


def pending_sessions(v, today_str):
    """Sesiones pospuestas o que quedaron a medias en días anteriores."""
    try:
        rows = _pending_rows(today_str)
    except Exception:
        return []
    out = []
    for sid, d_, day_name, status, pto, reason, done in rows:
        day = DAYS.get(day_name)
        if not day or not day["exercises"]:
            continue
        out.append(dict(id=sid, date=d_, day_key=day_name, status=status, postponed_to=pto, reason=reason,
                        done=int(done or 0), total=sum(e["sets"] for e in day["exercises"])))
    return out


def _pending_rows(today_str):
    return q("SELECT s.id, s.date, s.day_name, s.status, s.postponed_to, s.postpone_reason, "
             "(SELECT COUNT(*) FROM sets_log l WHERE l.session_id=s.id AND l.status IN ('completed','skipped')) "
             "FROM sessions s WHERE s.user_id=? AND (s.status='postponed' OR (s.status='in_progress' AND s.date<?)) "
             "ORDER BY s.date DESC LIMIT 6", [UID, today_str])


def start_session(sid, d, day_key):
    x("INSERT OR IGNORE INTO sessions (id, user_id, program_id, date, week_no, day_key, day_name, started_at, status) "
      "VALUES (?,?,?,?,?,?,?,?, 'in_progress')",
      [sid, UID, PROGRAM_ID, str(d), week_number(d), DAYS[day_key]["slug"], day_key, iso()])
    bump(True)


def session_sets(sid):
    df = qdf("SELECT id, exercise, set_no, weight_kg, reps, rir, status, started_at, finished_at, rest_seconds, "
             "rest_actual, notes FROM sets_log WHERE session_id=? ORDER BY exercise, set_no", [sid])
    for c in ("set_no", "weight_kg", "reps", "rir", "rest_seconds", "rest_actual"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def upsert_set(rid, sid, d, exercise, n, weight, unit_, reps, rir, status, started_at, finished_at, notes):
    wkg = round(unit_to_kg(float(weight), unit_), 3)
    x("INSERT INTO sets_log (id, user_id, session_id, date, exercise, set_no, weight, unit, weight_kg, reps, rir, "
      "status, started_at, finished_at, notes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
      "ON CONFLICT(id) DO UPDATE SET weight=excluded.weight, unit=excluded.unit, weight_kg=excluded.weight_kg, "
      "reps=excluded.reps, rir=excluded.rir, notes=excluded.notes, updated_at=excluded.updated_at, "
      "started_at=COALESCE(sets_log.started_at, excluded.started_at), "
      "finished_at=CASE WHEN sets_log.status = excluded.status "
      "THEN COALESCE(sets_log.finished_at, excluded.finished_at) ELSE excluded.finished_at END, "
      "status=excluded.status",
      [rid, UID, sid, str(d), exercise, int(n), float(weight), unit_, wkg, int(reps),
       None if rir is None else int(rir), status, started_at, finished_at, notes, iso(), iso()])


# =============================================================================
# 9. CALLBACKS DE INTERFAZ
# =============================================================================
def keep_state(key, keep_key):
    st.session_state[keep_key] = st.session_state.get(key)


def cb_start_training(day_key, go_to_workout=True):
    st.session_state.pop("active_sid", None)
    d = today()
    sid = f"{d}|{DAYS[day_key]['slug']}"
    start_session(sid, d, day_key)
    st.session_state["day_pick"] = day_key
    st.session_state["_keep_day"] = day_key
    if go_to_workout:
        st.session_state["nav"] = NAV[2]


def cb_finish_session(sid):
    timer_finalize("manual")
    st.session_state.pop("active_sid", None)
    x("UPDATE sessions SET status='finished', finished_at=? WHERE id=?", [iso(), sid])
    bump(True)


def cb_postpone_session(sid):
    """Pospone la sesión en curso: guarda lo hecho y deja el resto para otro día."""
    S = st.session_state
    reason = S.get("pp_reason") or POSTPONE_REASONS[0]
    to = S.get("pp_date") or (today() + timedelta(days=1))
    note = (S.get("pp_note") or "").strip()
    timer_finalize("manual")
    text = f"Pospuesta ({reason})" + (f": {note}" if note else "")
    x("UPDATE sessions SET status='postponed', postponed_at=?, postponed_to=?, postpone_reason=?, "
      "postpone_count=COALESCE(postpone_count,0)+1, "
      "notes=CASE WHEN notes IS NULL OR notes='' THEN ? ELSE notes || ' | ' || ? END WHERE id=?",
      [iso(), str(to), reason, text, text, sid])
    done = q1("SELECT COUNT(*) FROM sets_log WHERE session_id=? AND status='completed'", [sid])
    S.pop("active_sid", None)
    S["nav"] = NAV[0]
    bump(True)
    flash("success", f"Entrenamiento pospuesto para el {fecha_es(to).lower()}. "
                     f"Tus {done[0] if done else 0} series ya hechas quedaron guardadas.")


def cb_resume_session(sid):
    """Retoma una sesión pospuesta (o a medias) justo donde se quedó."""
    sess = get_session(sid)
    if not sess:
        return
    away = 0
    if sess["status"] == "postponed" and sess.get("postponed_at"):
        pa = parse_iso(sess["postponed_at"])
        away = int(max(0, (now() - pa).total_seconds())) if pa else 0
    x("UPDATE sessions SET status='in_progress', finished_at=NULL, postponed_to=NULL, "
      "away_seconds=COALESCE(away_seconds,0)+? WHERE id=?", [away, sid])
    if sess["date"] == str(today()):
        st.session_state.pop("active_sid", None)
        st.session_state["day_pick"] = st.session_state["_keep_day"] = sess["day_name"]
    else:
        st.session_state["active_sid"] = sid
    st.session_state["nav"] = NAV[2]
    bump(True)


def cb_close_pending(sid):
    """Da por terminada una sesión pendiente con lo que se alcanzó a hacer."""
    sess = get_session(sid)
    if not sess:
        return
    end = sess.get("postponed_at")
    if not end:
        r = q1("SELECT MAX(finished_at) FROM sets_log WHERE session_id=?", [sid])
        end = r[0] if r and r[0] else iso()
    x("UPDATE sessions SET status='finished', finished_at=?, postponed_to=NULL WHERE id=?", [end, sid])
    if st.session_state.get("active_sid") == sid:
        st.session_state.pop("active_sid", None)
    bump(True)
    st.toast("Sesión cerrada con lo que alcanzaste a hacer")


def cb_back_today():
    st.session_state.pop("active_sid", None)


def cb_reopen_session(sid):
    x("UPDATE sessions SET status='in_progress', finished_at=NULL WHERE id=?", [sid])
    if not str(sid).startswith(str(today())):
        st.session_state["active_sid"] = sid
    bump(True)


def cb_set_exercise(day_slug, names, name=None, step=0, rests=None):
    """Cambia el ejercicio activo (selectbox, anterior/siguiente) y ajusta el descanso objetivo."""
    pick_key = f"ex_pick_{day_slug}"
    if name is None:
        name = st.session_state.get(pick_key, names[0])
        if step:
            i = names.index(name) if name in names else 0
            name = names[max(0, min(len(names) - 1, i + step))]
    st.session_state[pick_key] = name
    st.session_state[f"_keep_ex_{day_slug}"] = name
    if rests and name in rests:
        st.session_state["rest_target"] = rests[name] if rests[name] > 0 else 60
        st.session_state["_keep_rest"] = st.session_state["rest_target"]


def cb_save_set(rid, sid, d_str, ex_name, n, status, rest_default, auto_rest, activity=False):
    u = unit()
    S = st.session_state
    if activity:
        w, reps, rir, note = 0.0, 0, None, ""
    else:
        w = float(S.get(f"w_{rid}", 0.0) or 0.0)
        reps = int(S.get(f"r_{rid}", 0) or 0)
        rir = int(S.get(f"i_{rid}", 2))
        note = S.get(f"n_{rid}", "") or ""
        if status == "completed" and reps <= 0:
            flash("warning", f"Serie {n}: introduce las repeticiones realizadas antes de guardar.")
            return
    prev = q1("SELECT status FROM sets_log WHERE id=?", [rid])
    is_new = status == "completed" and (not prev or prev[0] != "completed")
    t = timer_row()
    n_ts = time.time()
    guess = rest_end_guess(t, n_ts) if is_new else None
    started = iso_from_ts(guess) if guess and 0 <= n_ts - guess < 1800 else None
    upsert_set(rid, sid, d_str, ex_name, n, w, u, reps, rir, status, started, iso(), note)
    if is_new:
        timer_finalize("next_set", n_ts)
        if auto_rest and rest_default > 0:
            timer_start(rest_default, f"{ex_name} · tras serie {n}", set_id=rid, n=n_ts)
    # Prefill de la siguiente serie aún no guardada
    if status == "completed" and not activity:
        nid = rid.rsplit("|", 1)[0] + f"|{n + 1}"
        if not q1("SELECT 1 FROM sets_log WHERE id=?", [nid]):
            S[f"w_{nid}"], S[f"r_{nid}"], S[f"i_{nid}"] = w, reps, rir
    bump()
    st.toast(f"Serie {n} {'guardada' if status == 'completed' else 'omitida'}")


def cb_delete_set(rid):
    x("DELETE FROM sets_log WHERE id=?", [rid])
    for k in (f"w_{rid}", f"r_{rid}", f"i_{rid}", f"n_{rid}"):
        st.session_state.pop(k, None)
    bump()


def cb_add_extra(key):
    st.session_state[key] = st.session_state.get(key, 0) + 1


def cb_accept_target(name, es, sid, rec_w, last_w, action, reason, nsets):
    u = unit()
    w = float(st.session_state.get(f"tw_{es}", rec_w if rec_w is not None else last_w or 0.0))
    status = "accepted" if rec_w is not None and abs(w - rec_w) < 1e-6 else "modified"
    x("INSERT INTO load_targets (user_id, exercise, weight, unit, reason, set_on, status) VALUES (?,?,?,?,?,?,?) "
      "ON CONFLICT(user_id, exercise) DO UPDATE SET weight=excluded.weight, unit=excluded.unit, "
      "reason=excluded.reason, set_on=excluded.set_on, status=excluded.status",
      [UID, name, w, u, reason, str(today()), status])
    x("INSERT INTO progression_log (user_id, date, exercise, action, last_weight, suggested_weight, accepted_weight, unit, reason) "
      "VALUES (?,?,?,?,?,?,?,?,?)", [UID, str(today()), name, action, last_w, rec_w, w, u, reason])
    load_targets()[name] = dict(weight=w, unit=u, reason=reason, set_on=str(today()), status=status)
    done = {r[0] for r in q("SELECT set_no FROM sets_log WHERE session_id=? AND exercise=? AND status IN ('completed','skipped')",
                            [sid, name])}
    for n in range(1, max(nsets, 8) + 1):
        if n not in done:
            st.session_state[f"w_{sid}|{es}|{n}"] = w
    bump()
    st.toast("Carga confirmada")


def cb_save_ex_cfg(name, es, u):
    rest = int(st.session_state.get(f"cfg_rest_{es}", 90))
    inc = float(st.session_state.get(f"cfg_inc_{es}", 2.5))
    x("INSERT INTO exercise_config (user_id, exercise, rest_seconds, increment, inc_unit) VALUES (?,?,?,?,?) "
      "ON CONFLICT(user_id, exercise) DO UPDATE SET rest_seconds=excluded.rest_seconds, "
      "increment=excluded.increment, inc_unit=excluded.inc_unit", [UID, name, rest, inc, u])
    ex_cfg()[name] = dict(rest=rest, inc=inc, inc_unit=u)
    st.session_state["rest_target"] = rest if rest > 0 else 60
    bump(True)
    st.toast("Ajustes del ejercicio guardados")


def cb_timer(action, label="", dur_key="rest_target"):
    if action == "start":
        timer_start(int(st.session_state.get(dur_key, 120)), label or "Descanso")
    elif action == "pause":
        timer_pause()
    elif action == "resume":
        timer_resume()
    elif action == "restart":
        timer_restart()
    elif action == "finish":
        timer_finalize("manual")
    elif action == "plus":
        timer_adjust(30)
    elif action == "minus":
        timer_adjust(-15)


def cb_auto_rest():
    set_setting("auto_rest", "1" if st.session_state.get("auto_rest_tg") else "0")


def cb_save_goals():
    S = st.session_state
    d = str(today())
    sleep = float(S.get("pl_sleep", 0.0))
    x("INSERT INTO daily_metrics (date, sleep_hours, target_calories, target_protein_g, target_carbs_g, target_fat_g) "
      "VALUES (?,?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET sleep_hours=excluded.sleep_hours, "
      "target_calories=excluded.target_calories, target_protein_g=excluded.target_protein_g, "
      "target_carbs_g=excluded.target_carbs_g, target_fat_g=excluded.target_fat_g",
      [d, sleep, float(S["pl_cal"]), float(S["pl_prot"]), float(S["pl_carbs"]), float(S["pl_fat"])])
    bump()
    st.toast("Metas guardadas")


def cb_save_recovery():
    S = st.session_state
    d = str(today())
    t = get_targets(today())
    x("INSERT INTO daily_metrics (date, sleep_hours, target_calories, target_protein_g, target_carbs_g, target_fat_g) "
      "VALUES (?,?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET sleep_hours=excluded.sleep_hours",
      [d, float(S.get("rc_sleep", 0.0)), t["cal"], t["prot"], t["carbs"], t["fat"]])
    x("INSERT INTO recovery_log (user_id, date, energy, soreness, notes) VALUES (?,?,?,?,?) "
      "ON CONFLICT(user_id, date) DO UPDATE SET energy=excluded.energy, soreness=excluded.soreness, notes=excluded.notes",
      [UID, d, int(S.get("rc_energy", 3)), int(S.get("rc_sore", 3)), S.get("rc_notes", "")])
    bump()
    st.toast("Recuperación guardada")


def cb_save_body():
    S = st.session_state
    x("INSERT INTO body_metrics (user_id, date, weight_kg, waist_cm) VALUES (?,?,?,?) "
      "ON CONFLICT(user_id, date) DO UPDATE SET weight_kg=COALESCE(excluded.weight_kg, body_metrics.weight_kg), "
      "waist_cm=COALESCE(excluded.waist_cm, body_metrics.waist_cm)",
      [UID, str(S["bd_date"]), float(S["bd_w"]) or None, float(S["bd_waist"]) or None])
    bump()
    st.toast("Medidas guardadas")


def cb_save_program():
    start = week_start_of(st.session_state["pl_start"])
    x("UPDATE programs SET start_date=? WHERE id=?", [str(start), PROGRAM_ID])
    st.session_state["_pstart"] = start
    bump(True)
    st.toast(f"Bloque iniciado el sábado {start.day} de {MESES[start.month - 1]}")


def cb_unit_change():
    new = st.session_state.get("pl_unit", "kg")
    set_setting("unit", new)
    for k in [k for k in list(st.session_state.keys()) if str(k).startswith(("w_", "tw_"))]:
        st.session_state.pop(k, None)
    for k in ("_targets",):
        st.session_state.pop(k, None)
    bump(True)


def cb_pref(key, widget_key):
    set_setting(key, st.session_state.get(widget_key, ""))


def cb_week(week_no, close):
    if close:
        x("INSERT INTO program_weeks (program_id, week_no, closed, closed_at) VALUES (?,?,1,?) "
          "ON CONFLICT(program_id, week_no) DO UPDATE SET closed=1, closed_at=excluded.closed_at",
          [PROGRAM_ID, int(week_no), iso()])
    else:
        x("INSERT INTO program_weeks (program_id, week_no, closed) VALUES (?,?,0) "
          "ON CONFLICT(program_id, week_no) DO UPDATE SET closed=0, closed_at=NULL", [PROGRAM_ID, int(week_no)])
    bump()


def cb_delete_meal(mid):
    x("DELETE FROM meals WHERE id=?", [int(mid)])
    bump()


def cb_save_pending_meal():
    p = st.session_state.pop("pending_meal", None)  # pop => un segundo clic no duplica
    if not p:
        return
    e = p["est"]
    x("INSERT INTO meals (date, description, calories, protein_g, carbs_g, fat_g) VALUES (?,?,?,?,?,?)",
      [p["date"], p["desc"], e["cal"], e["prot"], e["carbs"], e["fat"]])
    st.session_state["meal_text"] = ""
    bump()
    st.toast(f"Comida guardada: {e['cal']:.0f} kcal")


def cb_discard_meal():
    st.session_state.pop("pending_meal", None)


def cb_save_manual_meal():
    S = st.session_state
    desc = (S.get("mm_desc") or "").strip() or "Comida manual"
    x("INSERT INTO meals (date, description, calories, protein_g, carbs_g, fat_g) VALUES (?,?,?,?,?,?)",
      [str(S.get("meal_date", today())), desc, float(S["mm_cal"]), float(S["mm_p"]), float(S["mm_c"]), float(S["mm_f"])])
    S["mm_desc"] = ""
    bump()
    st.toast("Comida guardada")


# =============================================================================
# 10. IA (Gemini) — análisis de comidas
# =============================================================================
class FoodItem(BaseModel):
    name: str
    calories: float
    protein_g: float
    carbs_g: float
    fat_g: float


class MealEstimate(BaseModel):
    items: list[FoodItem]
    total_calories: float
    total_protein_g: float
    total_carbs_g: float
    total_fat_g: float


def ai_client():
    key = secret("GEMINI_API_KEY")
    return genai.Client(api_key=key) if (key and genai is not None) else None


class QuotaError(RuntimeError):
    """Cuota de Gemini agotada (se muestra con un mensaje claro, sin el JSON crudo)."""


def _retry_seconds(err_text):
    m = re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s", err_text)
    if m:
        return float(m.group(1))
    m = re.search(r"retry in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", err_text)
    if m and any(m.groups()):
        h, mi, se = (float(g) if g else 0.0 for g in m.groups())
        return h * 3600 + mi * 60 + se
    return None


def _norm_meal_text(text):
    return re.sub(r"\s+", " ", str(text).strip().lower())


@st.cache_data(ttl=7 * 24 * 3600, show_spinner=False, max_entries=500)
def _parse_meal_cached(text_norm, models_csv):
    """Llama a Gemini. Resultado en caché: analizar el mismo texto otra vez no gasta cuota.
    Los errores no se guardan en caché."""
    client = ai_client()
    if client is None:
        raise RuntimeError("Falta GEMINI_API_KEY (o la librería google-genai).")
    prompt = f"Analiza para hipertrofia: '{text_norm}'. Desglosa por ingredientes y da el total de macros."
    candidates = [m.strip() for m in models_csv.split(",") if m.strip()]
    last_err, quota_models, waits = None, [], []
    for model_name in candidates:
        for attempt in range(2):
            try:
                resp = client.models.generate_content(
                    model=model_name, contents=prompt,
                    config=dict(response_mime_type="application/json", response_schema=MealEstimate, temperature=0.1))
                est = MealEstimate.model_validate_json(resp.text)
                # Los totales se recalculan desde los ingredientes (la IA suele fallar al sumar).
                if est.items:
                    est.total_calories = round(sum(i.calories for i in est.items), 1)
                    est.total_protein_g = round(sum(i.protein_g for i in est.items), 1)
                    est.total_carbs_g = round(sum(i.carbs_g for i in est.items), 1)
                    est.total_fat_g = round(sum(i.fat_g for i in est.items), 1)
                return est.model_dump()
            except Exception as e:
                last_err, txt = e, str(e)
                if "429" in txt or "RESOURCE_EXHAUSTED" in txt:
                    wait = _retry_seconds(txt)
                    daily = "PerDay" in txt or (wait is not None and wait > 60)
                    if daily or attempt == 1:
                        # Cuota diaria: reintentar no sirve; se pasa al siguiente modelo.
                        quota_models.append(model_name)
                        if wait:
                            waits.append(wait)
                        break
                    time.sleep(min(wait or 2, 10))
                    continue
                if "503" in txt or "UNAVAILABLE" in txt:
                    time.sleep(2)
                    continue
                break
    if quota_models and len(quota_models) == len(candidates):
        when = f" Se renueva en aproximadamente {hms(min(waits))}." if waits else ""
        raise QuotaError(
            f"Se agotó la cuota gratuita de Gemini para hoy ({', '.join(quota_models)}).{when} "
            "Mientras tanto usa el registro manual; o agrega otro modelo en el secret GEMINI_MODELS "
            "(cada modelo tiene su propia cuota) o activa la facturación en Google AI Studio.")
    raise RuntimeError(f"Error IA: {last_err}")


def parse_meal(text):
    models = secret("GEMINI_MODELS", "gemini-flash-latest,gemini-3.8-flash")
    return MealEstimate.model_validate(_parse_meal_cached(_norm_meal_text(text), models))


# =============================================================================
# 11. ESTILOS
# =============================================================================
CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Big+Shoulders+Display:wght@600;700;800&family=Figtree:wght@400;500;600;700&display=swap');
:root {{
  --disp: 'Big Shoulders Display', 'Arial Narrow', 'Roboto Condensed', sans-serif;
  --body: 'Figtree', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif;
  --bg:{C_BG}; --card:{C_CARD}; --card2:{C_CARD2}; --line:{C_BORDER}; --text:{C_TEXT}; --muted:{C_MUTED};
  --coral:{C_CORAL}; --blue:{C_BLUE}; --orange:{C_ORANGE}; --green:{C_GREEN}; --track:#252A38;
}}
#MainMenu, footer, header {{visibility:hidden;}}
html, body, .stApp, [data-testid="stAppViewContainer"] {{background:var(--bg) !important; color:var(--text);}}
.stApp, .stApp p, .stApp li, .stApp label, .stApp input, .stApp textarea, .stApp button {{font-family:var(--body);}}
.block-container {{padding:0.4rem 0.9rem 6rem; max-width:560px;}}
h1,h2,h3,h4,h5,h6 {{color:var(--text) !important; font-family:var(--disp) !important; font-weight:700 !important; letter-spacing:.01em;}}
[data-testid="stMarkdownContainer"] p, [data-testid="stMarkdownContainer"] li {{color:#E6E9F0;}}
[data-testid="stWidgetLabel"] p, label p {{color:#C4CAD8 !important; font-size:12.5px !important; font-weight:500;}}
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p {{color:var(--muted) !important; font-size:12px;}}
::selection {{background:rgba(255,90,95,.35);}}

/* ---------- Campos ---------- */
div[data-baseweb="input"] > div, div[data-baseweb="base-input"], div[data-baseweb="select"] > div,
div[data-baseweb="textarea"], div[data-baseweb="textarea"] > div {{
  background:var(--card2) !important; border-color:var(--line) !important; border-radius:10px !important;}}
div[data-baseweb="input"]:focus-within, div[data-baseweb="select"]:focus-within > div,
div[data-baseweb="textarea"]:focus-within {{border-color:var(--coral) !important;}}
div[data-baseweb="input"] input, div[data-baseweb="base-input"] input, textarea,
div[data-baseweb="select"] div {{color:#FFFFFF !important; -webkit-text-fill-color:#FFFFFF;}}
div[data-baseweb="input"] input {{font-variant-numeric:tabular-nums; font-weight:600;}}
/* Streamlit reciente (sin baseweb) */
[data-testid="stSelectbox"] [role="group"], [data-testid="stNumberInputContainer"],
[data-testid="stTextInputRootElement"], [data-testid="stTextArea"] textarea, [data-testid="stDateInput"] [role="group"] {{
  background:var(--card2) !important; border-color:var(--line) !important; border-radius:10px !important;}}
[data-testid="stSelectbox"] [role="group"]:focus-within, [data-testid="stNumberInputContainer"]:focus-within,
[data-testid="stTextInputRootElement"]:focus-within {{border-color:var(--coral) !important;}}
[data-testid="stNumberInputField"], [data-testid="stTextInputField"], [data-testid="stSelectbox"] input {{
  color:#fff !important; -webkit-text-fill-color:#fff; background:transparent !important; font-weight:600;
  font-variant-numeric:tabular-nums;}}
button[data-testid="stNumberInputStepUp"], button[data-testid="stNumberInputStepDown"] {{background:transparent !important; color:#C4CAD8 !important;}}

/* ---------- Botones ---------- */
.stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {{
  min-height:46px; border-radius:12px; border:1px solid var(--line) !important; background:transparent !important; color:var(--text) !important;
  font-weight:600; font-size:14px; transition:transform .08s ease, border-color .15s ease, background .15s ease;}}
.stButton > button:hover, .stDownloadButton > button:hover, .stFormSubmitButton > button:hover {{
  border-color:var(--coral) !important; color:#fff !important; background:rgba(255,90,95,.06) !important;}}
.stButton > button:active, .stFormSubmitButton > button:active {{transform:scale(.98);}}
.stButton > button:focus-visible, .stDownloadButton > button:focus-visible {{outline:2px solid var(--coral); outline-offset:2px;}}
.stButton > button[kind="primary"], .stButton > button[data-testid="stBaseButton-primary"],
.stFormSubmitButton > button[kind="primary"], .stFormSubmitButton > button[data-testid="stBaseButton-primary"] {{
  background:var(--coral) !important; border-color:var(--coral) !important; color:#fff !important;
  box-shadow:0 6px 18px -8px rgba(255,90,95,.7);}}
.stButton > button[kind="primary"]:hover, .stButton > button[data-testid="stBaseButton-primary"]:hover {{background:#FF6B70 !important;}}
.stButton > button [data-testid="stIconMaterial"] {{font-size:19px;}}
.stButton > button:disabled {{opacity:.45;}}

/* ---------- Columnas: en móvil no se apilan ---------- */
div[data-testid="stHorizontalBlock"] {{flex-wrap:nowrap !important; gap:0.5rem !important;}}
div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"],
div[data-testid="stHorizontalBlock"] > div[data-testid="column"] {{min-width:0 !important;}}

/* ---------- Navegación ---------- */
.st-key-topnav {{position:sticky; top:0; z-index:100; background:var(--bg); padding:6px 0 10px;}}
.st-key-topnav [data-testid="stElementContainer"], .st-key-topnav .element-container,
.st-key-topnav [data-testid="stRadio"], .st-key-topnav [data-testid="stRadio"] > div,
.st-key-topnav .stRadio, .st-key-topnav .stRadio > div {{width:100% !important;}}
div[role="radiogroup"] {{display:flex; flex-wrap:nowrap; gap:2px; width:100%; background:var(--card);
  padding:4px; border:1px solid var(--line); border-radius:16px;}}
div[role="radiogroup"] > div, div[role="radiogroup"] > label {{flex:1 1 0; min-width:0; margin:0;}}
div[role="radiogroup"] label {{width:100%; justify-content:center; margin:0; padding:7px 2px 6px; border-radius:12px;
  cursor:pointer; text-align:center; transition:background .15s ease;}}
div[role="radiogroup"] label > div {{justify-content:center; width:100%;}}
/* oculta el círculo del radio (estructura nueva y antigua de Streamlit) */
div[role="radiogroup"] label > div > div:first-child:not([data-testid]),
div[role="radiogroup"] > label > div:first-child:not([data-testid]) {{display:none !important;}}
div[role="radiogroup"] label p {{display:flex; flex-direction:column; align-items:center; gap:1px;
  font-size:11px !important; margin:0; color:var(--muted) !important; white-space:nowrap; font-weight:600; line-height:1.2;}}
div[role="radiogroup"] label p span[role="img"], div[role="radiogroup"] label p [data-testid="stIconMaterial"] {{
  font-size:21px !important; line-height:1;}}
div[role="radiogroup"] label:has(input:checked) {{background:rgba(255,90,95,.13);}}
div[role="radiogroup"] label:has(input:checked) p {{color:var(--coral) !important;}}
div[role="radiogroup"] label:focus-within {{outline:2px solid rgba(255,90,95,.5); outline-offset:-2px;}}

/* ---------- Cronómetro fijo mientras se entrena ---------- */
.st-key-timerbox {{position:sticky; top:72px; z-index:90; background:var(--bg); padding-bottom:6px;}}

/* ---------- Pestañas, expanders, contenedores ---------- */
.stTabs [data-baseweb="tab-list"] {{gap:4px; border-bottom:1px solid var(--line);}}
.stTabs [data-baseweb="tab"] {{padding:8px 8px; font-size:13px; color:var(--muted); font-weight:600;}}
.stTabs [aria-selected="true"] {{color:var(--coral) !important;}}
.stTabs [data-baseweb="tab-highlight"] {{background:var(--coral) !important;}}
[data-testid="stExpander"] {{border:1px solid var(--line); border-radius:14px; background:var(--card);}}
[data-testid="stExpander"] summary p {{font-weight:600; font-size:13px;}}
[data-testid="stVerticalBlockBorderWrapper"]:has(> div > [data-testid="stVerticalBlock"]) {{
  border-color:var(--line) !important; border-radius:14px !important; background:var(--card);}}
[data-testid="stAlert"] {{border-radius:12px;}}

/* ---------- Tipografía propia ---------- */
.gx-h1 {{font-family:var(--disp); font-size:38px; font-weight:800; color:#fff; line-height:1; letter-spacing:.01em;}}
.gx-title {{font-family:var(--disp); font-size:21px; font-weight:700; color:#fff; line-height:1.15; letter-spacing:.015em;}}
.gx-title.lg {{font-size:28px;}}
.gx-sec {{font-family:var(--disp); font-size:22px; font-weight:700; color:#fff; margin:18px 0 8px; letter-spacing:.015em;}}
.gx-muted {{color:var(--muted); font-size:12.5px; line-height:1.45;}}
.gx-text {{color:#D5DAE6; font-size:13.5px; margin-top:6px; line-height:1.5;}}
.gx-num {{font-family:var(--disp); font-variant-numeric:tabular-nums;}}
.gx-ico {{display:inline-block; vertical-align:-2px; flex:none;}}
.gx-row {{display:flex; justify-content:space-between; align-items:center; gap:10px;}}

/* ---------- Superficies ---------- */
.gx-card {{background:var(--card); border:1px solid var(--line); border-radius:16px; padding:16px; margin:0 0 10px;}}
.gx-card.flat {{background:var(--card2); border-color:transparent;}}
.gx-card.good {{border-color:rgba(52,211,153,.5);}}
.gx-ready {{border-color:rgba(52,211,153,.55) !important;}}

/* Portada de Inicio: número de semana como elemento gráfico */
.gx-hero {{position:relative; overflow:hidden; background:var(--card); border:1px solid var(--line);
  border-radius:20px; padding:18px 18px 16px; margin:0 0 10px;}}
.gx-hero > * {{position:relative;}}
.gx-hero-num {{position:absolute !important; right:-4px; top:-26px; font-family:var(--disp); font-weight:800;
  font-size:132px; line-height:1; color:transparent; -webkit-text-stroke:1.5px rgba(255,90,95,.30);
  pointer-events:none; user-select:none; letter-spacing:-.02em;}}
.gx-segs {{display:grid; grid-template-columns:repeat(8,minmax(0,1fr)); gap:4px; margin-top:8px;}}
.gx-seg {{height:6px; border-radius:3px; background:var(--track);}}
.gx-seg.done {{background:var(--blue);}}
.gx-seg.cur {{background:var(--coral);}}
.gx-today {{margin-top:16px; padding-top:14px; border-top:1px solid var(--line);}}

/* Tira de la semana (sábado a viernes) */
.gx-strip {{display:grid; grid-template-columns:repeat(7,minmax(0,1fr)); gap:4px; margin-top:12px;}}
.gx-day {{background:var(--card2); border-radius:10px; padding:6px 2px 7px; text-align:center; font-size:10.5px;
  color:var(--muted); border:1px solid transparent; line-height:1.25;}}
.gx-day b {{display:block; font-family:var(--disp); font-size:20px; font-weight:700; color:#fff; line-height:1.05;}}
.gx-day small {{display:block; font-size:9.5px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; padding:0 2px;}}
.gx-day.today {{border-color:var(--coral);}}
.gx-day.today b {{color:var(--coral);}}
.gx-day.done {{background:rgba(52,211,153,.13);}}
.gx-day.done b {{color:var(--green);}}
.gx-day.rest b {{color:var(--muted);}}
.gx-day.late {{background:rgba(255,159,67,.13);}}
.gx-day.late b {{color:var(--orange);}}

/* Chips y estados */
.gx-chip {{display:inline-block; padding:4px 9px; border-radius:8px; background:var(--card2);
  color:#C4CAD8; font-size:11.5px; font-weight:600; margin:6px 5px 0 0;}}
.gx-chip.blue {{color:var(--blue); background:rgba(77,163,255,.1);}}
.gx-chip.coral {{color:var(--coral); background:rgba(255,90,95,.1);}}
.gx-chip.orange {{color:var(--orange); background:rgba(255,159,67,.1);}}
.gx-badge {{display:inline-flex; align-items:center; gap:4px; padding:4px 9px; border-radius:8px; font-size:11.5px;
  font-weight:700; white-space:nowrap;}}
.gx-badge.ok {{background:rgba(52,211,153,.14); color:var(--green);}}
.gx-badge.pend {{background:var(--card2); color:var(--muted);}}
.gx-badge.skip {{background:rgba(255,159,67,.13); color:var(--orange);}}
.gx-badge.run {{background:rgba(77,163,255,.14); color:var(--blue);}}

/* Indicadores */
.gx-kpis {{display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px;}}
.gx-kpis.three {{grid-template-columns:repeat(3,minmax(0,1fr));}}
.gx-kpi {{background:var(--card2); border-radius:12px; padding:10px 12px 11px;}}
.gx-kpi .v {{font-family:var(--disp); font-size:27px; font-weight:700; color:#fff; line-height:1.05;
  font-variant-numeric:tabular-nums; overflow-wrap:anywhere; letter-spacing:.01em;}}
.gx-kpi .l {{font-size:11.5px; color:var(--muted); margin-top:3px; line-height:1.35;}}
.gx-track {{height:8px; border-radius:4px; background:var(--track); position:relative; display:flex; overflow:hidden; margin-top:6px;}}
.gx-mark {{position:absolute; top:0; bottom:0; width:2px; background:#fff; opacity:.85;}}
.gx-bar-head {{display:flex; justify-content:space-between; font-size:12.5px; color:#D5DAE6; gap:6px;}}
.gx-bar-head .gx-num {{font-size:15px; font-weight:700; color:#fff;}}
.gx-grid2 {{display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px;}}
.gx-week {{background:var(--card); border:1px solid var(--line); border-radius:14px; padding:12px; font-size:12px;
  color:#D5DAE6; line-height:1.55;}}
.gx-week b {{color:#fff;}}
.gx-week .wk {{font-family:var(--disp); font-size:22px; font-weight:700; color:#fff;}}
.gx-up {{color:var(--green);}} .gx-down {{color:var(--orange);}}
.gx-note {{color:var(--muted); font-size:12.5px; margin-top:6px; padding-left:10px; border-left:2px solid var(--line);}}

/* Lista de ejercicios de la sesión */
.gx-list-row {{display:flex; justify-content:space-between; align-items:center; gap:10px; padding:10px 0;
  border-bottom:1px solid var(--line);}}
.gx-list-row:last-child {{border-bottom:none;}}
.gx-list-row .n {{font-family:var(--disp); font-size:18px; font-weight:700; color:var(--muted); width:20px; flex:none;}}

/* Evaluación de sobrecarga progresiva */
.gx-check {{display:flex; align-items:center; gap:7px; font-size:12.5px; color:var(--muted); margin-top:6px;}}
.gx-check.ok {{color:var(--green);}}

@media (prefers-reduced-motion: reduce) {{ * {{transition:none !important; animation:none !important;}} }}
</style>
"""


# =============================================================================
# 12. COMPONENTES VISUALES
# =============================================================================
def chip(text, cls=""):
    return f'<span class="gx-chip {cls}">{esc(text)}</span>'


def badge(text, kind):
    return f'<span class="gx-badge {kind}">{esc(text)}</span>'


def kpi(label, value, extra=""):
    return f'<div class="gx-kpi"><div class="v">{value}</div><div class="l">{esc(label)}{extra}</div></div>'


def week_segments(wk):
    """Barra de 8 segmentos: una por semana del bloque."""
    cells = []
    for n in range(1, WEEKS + 1):
        cls = "done" if n < wk else ("cur" if n == wk else "")
        cells.append(f'<div class="gx-seg {cls}"></div>')
    return '<div class="gx-segs">' + "".join(cells) + "</div>"


def macro_bar(label, value, goal, unit_, color):
    """Barra que NO recorta el exceso: el sobrante se pinta en coral y se muestra el % real."""
    goal = float(goal or 0)
    value = float(value or 0)
    pct = (value / goal * 100) if goal else 0
    scale = max(goal, value) or 1
    base_w = min(value, goal) / scale * 100
    over_w = max(0.0, value - goal) / scale * 100
    mark = goal / scale * 100 if goal else 0
    over_txt = f" · <b style='color:{C_CORAL}'>+{value - goal:.0f} {unit_} sobre la meta</b>" if value > goal else ""
    return (f'<div style="margin-bottom:10px"><div class="gx-bar-head"><span>{esc(label)}</span>'
            f'<span><span class="gx-num">{value:.0f}</span> / {goal:.0f} {unit_} · {pct:.0f}%</span></div>'
            f'<div class="gx-track"><div style="width:{base_w:.1f}%;background:{color}"></div>'
            f'<div style="width:{over_w:.1f}%;background:{C_CORAL}"></div>'
            f'<div class="gx-mark" style="left:calc({mark:.1f}% - 1px)"></div></div>'
            f'<div class="gx-muted">{over_txt}</div></div>')


def overload_card(chk, title, inc, u, subtitle=""):
    """Tarjeta '¿Toca subir?' con cada criterio marcado."""
    items = "".join(f'<div class="gx-check {"ok" if ok else ""}">{ico("check" if ok else "circle")}<span>{esc(t)}</span></div>'
                    for t, ok in chk["checks"])
    if chk["ready"]:
        verdict = badge("Sí, toca subir", "ok")
        foot = (f'<div class="gx-text"><b>Próxima carga: {fmt_w(chk["weight"] + inc)} {u}</b> '
                f'(+{fmt_w(inc)}) si la técnica fue buena.</div>')
    else:
        verdict = badge("Todavía no", "pend")
        foot = f'<div class="gx-text">Mantén {fmt_w(chk["weight"])} {u} hasta cumplir todos los puntos.</div>'
    sub = f'<div class="gx-muted">{esc(subtitle)}</div>' if subtitle else ""
    return (f'<div class="gx-card {"gx-ready" if chk["ready"] else ""}" style="padding:10px">'
            f'<div class="gx-row"><div class="gx-title">{esc(title)}</div>{verdict}</div>{sub}{items}{foot}</div>')


def plain_bar(frac, color=C_BLUE):
    frac = max(0.0, min(1.0, frac))
    return f'<div class="gx-track"><div style="width:{frac * 100:.1f}%;background:{color}"></div></div>'


DAY_SHORT = {"Día 1: UPPER": "Upper", "Día 2: LEGS A": "Legs A", "Día 3: PUSH": "Push", "Día 4: PULL": "Pull",
             "Día 5: LEGS B": "Legs B", "Día 6: RECUPERACIÓN": "Recup.", "Día 7: DESCANSO": "Libre"}


def render_pending(items, prefix, exclude=()):
    """Tarjetas de sesiones pospuestas/pendientes con acciones para retomarlas o cerrarlas."""
    items = [p_ for p_ in items if p_["id"] not in exclude]
    if not items:
        return
    for p_ in items:
        d_ = date.fromisoformat(p_["date"])
        meta = [f"Del {DIAS_ES[d_.weekday()]} {fecha_corta(d_)}"]
        if p_["status"] == "postponed":
            if p_.get("reason"):
                meta.append(p_["reason"].lower())
            if p_.get("postponed_to"):
                to = date.fromisoformat(p_["postponed_to"])
                meta.append("para hoy" if to == today() else
                            (f"para el {DIAS_ES[to.weekday()]} {fecha_corta(to)}" if to > today()
                             else f"programada para el {fecha_corta(to)}"))
            tag = badge("Pospuesta", "skip")
        else:
            meta.append("quedó a medias")
            tag = badge("Sin terminar", "skip")
        frac = p_["done"] / p_["total"] if p_["total"] else 0
        H(f'<div class="gx-card" style="border-color:rgba(255,159,67,.45)"><div class="gx-row">'
          f'<div class="gx-title">{esc(p_["day_key"])}</div>{tag}</div>'
          f'<div class="gx-muted">{esc(" · ".join(meta))}</div>'
          f'<div class="gx-text">{p_["done"]} de {p_["total"]} series hechas</div>{plain_bar(frac, C_ORANGE)}</div>')
        a, b = st.columns(2)
        a.button("Retomar", type="primary", key=f"{prefix}_res_{p_['id']}", on_click=cb_resume_session,
                 args=(p_["id"],), **btn_w("play_arrow"))
        b.button("Terminar así", key=f"{prefix}_end_{p_['id']}", on_click=cb_close_pending,
                 args=(p_["id"],), **btn_w("flag"))


def week_strip(d, finished_dates, postponed_dates=()):
    """Tira de 7 días de la semana actual, de sábado a viernes."""
    w0 = week_start_of(d)
    cells = []
    for i, key in enumerate(DAY_KEYS):
        day_d = w0 + timedelta(days=i)
        cls = ["gx-day"]
        if day_d == d:
            cls.append("today")
        if str(day_d) in finished_dates:
            cls.append("done")
        elif str(day_d) in postponed_dates:
            cls.append("late")
        if not DAYS[key]["exercises"]:
            cls.append("rest")
        short = DAY_SHORT.get(key) or (key.split(":")[1].strip().title() if ":" in key else key)[:7]
        cells.append(f'<div class="{" ".join(cls)}">{DIAS_ES[day_d.weekday()][:3]}<b>{day_d.day}</b>'
                     f'<small>{esc(short)}</small></div>')
    return '<div class="gx-strip">' + "".join(cells) + "</div>"


def base_layout(fig, height=280, legend=False):
    fig.update_layout(
        height=height, margin=dict(l=6, r=6, t=10, b=6), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#C4CAD8", size=11, family="Figtree, system-ui, sans-serif"), showlegend=legend, dragmode=False,
        hoverlabel=dict(bgcolor=C_CARD2, font_color="#FFFFFF", bordercolor=C_BORDER),
        legend=dict(orientation="h", y=-0.2, x=0))
    fig.update_yaxes(gridcolor="#242938", zeroline=False, fixedrange=True, tickfont=dict(size=10))
    fig.update_xaxes(fixedrange=True, showgrid=False, tickfont=dict(size=10))
    return fig


def style_time_axis(fig, dates, labels=None):
    ds = pd.to_datetime(list(dates))
    if len(ds) == 0:
        return
    ticks = tick_subset(ds)
    texts = [labels(t) if labels else fecha_corta(t) for t in ticks]
    lo, hi = min(ds), max(ds)
    pad = max(pd.Timedelta(days=1), (hi - lo) * 0.06)
    fig.update_xaxes(type="date", tickmode="array", tickvals=ticks, ticktext=texts, tickangle=0,
                     range=[lo - pad, hi + pad])


def show_chart(fig, key):
    st.plotly_chart(fig, key=key, config={"displayModeBar": False, "scrollZoom": False}, **plot_w())


def timer_html(status, remaining, duration, label, sound):
    cls = {"done": "done", "paused": "paused"}.get(status, "")
    page = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Big+Shoulders+Display:wght@700;800&family=Figtree:wght@500;600&display=swap" rel="stylesheet">
<style>
body{margin:0;background:transparent;font-family:Figtree,-apple-system,Segoe UI,Roboto,sans-serif;color:#F5F7FA}
.w{background:#171B25;border:1px solid #2A2F3F;border-radius:16px;padding:12px 16px 14px;display:grid;
  grid-template-columns:auto 1fr;column-gap:16px;align-items:center}
.t{font-family:'Big Shoulders Display','Arial Narrow',sans-serif;font-size:68px;font-weight:800;line-height:.9;
  font-variant-numeric:tabular-nums;letter-spacing:.01em;min-width:3.2ch}
.side{min-width:0}
.l{font-size:12.5px;color:#A0A6B8;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.m{font-size:14px;font-weight:600;margin-top:3px;color:#F5F7FA;min-height:18px}
.b{grid-column:1 / -1;height:6px;border-radius:3px;background:#252A38;overflow:hidden;margin-top:12px}
.f{height:100%;width:0;background:#FF5A5F;transition:width .25s linear}
.done{border-color:rgba(52,211,153,.6)} .done .t,.done .m{color:#34D399} .done .f{background:#34D399}
.done .t{animation:p 1.1s ease-in-out 3}
.paused .t,.paused .m{color:#4DA3FF} .paused .f{background:#4DA3FF}
@keyframes p{0%,100%{opacity:1}50%{opacity:.35}}
@media (prefers-reduced-motion: reduce){.done .t{animation:none}.f{transition:none}}
</style>
<div class="w __CLS__" id="w"><div class="t" id="t">00:00</div>
<div class="side"><div class="l">__LABEL__</div><div class="m" id="m"></div></div>
<div class="b"><div class="f" id="f"></div></div></div>
<script>
(function(){
var status="__STATUS__", rem0=__REM__, dur=__DUR__, sound=__SOUND__, t0=performance.now(), fired=false;
var el=document.getElementById('t'), fb=document.getElementById('f'), mm=document.getElementById('m'), w=document.getElementById('w');
function fmt(s){s=Math.max(0,Math.ceil(s));var m=Math.floor(s/60),x=s%60;return (m<10?'0':'')+m+':'+(x<10?'0':'')+x;}
function beep(){
  try{ if(sound){var C=window.AudioContext||window.webkitAudioContext;var c=new C();
    [0,0.28,0.56].forEach(function(d){var o=c.createOscillator(),g=c.createGain();o.connect(g);g.connect(c.destination);
      o.frequency.value=880;g.gain.value=0.25;o.start(c.currentTime+d);o.stop(c.currentTime+d+0.16);});} }catch(e){}
  try{ if(navigator.vibrate){navigator.vibrate([300,120,300]);} }catch(e){}
}
var msgs={idle:'Listo para iniciar',paused:'En pausa',done:'Descanso terminado',running:'Descansando'};
mm.textContent=msgs[status]||'';
function tick(){
  var rem = status==='running' ? Math.max(0, rem0-(performance.now()-t0)/1000) : (status==='done'?0:rem0);
  el.textContent=fmt(rem);
  var p = (status==='idle'||dur<=0) ? 0 : (1-rem/dur)*100;
  fb.style.width=Math.min(100,Math.max(0,p))+'%';
  if(status==='running' && rem<=0 && !fired){fired=true;w.className='w done';mm.textContent='Descanso terminado';beep();}
}
tick(); setInterval(tick,200);
})();
</script>
"""
    return (page.replace("__CLS__", cls).replace("__LABEL__", esc(label or "Cronómetro de descanso"))
            .replace("__STATUS__", status).replace("__REM__", f"{max(0.0, remaining):.2f}")
            .replace("__DUR__", str(int(duration or 0))).replace("__SOUND__", "true" if sound else "false"))


# =============================================================================
# 13. VISTA: INICIO
# =============================================================================
def donut(consumed, goal):
    over = max(0.0, consumed - goal)
    if over > 0:
        vals, cols = [goal, over], [C_ORANGE, C_CORAL]
    else:
        vals, cols = [consumed, max(goal - consumed, 0)], [C_ORANGE, "#252A38"]
    if sum(vals) <= 0:
        vals = [0, 1]
    sub = f"+{over:.0f} sobre meta" if over else f"de {goal:.0f} kcal"
    fig = go.Figure(go.Pie(labels=["Consumido", "Restante/Exceso"], values=vals, hole=0.82, sort=False,
                           marker=dict(colors=cols), textinfo="none", hoverinfo="skip"))
    fig.update_layout(showlegend=False, margin=dict(t=0, b=0, l=0, r=0), height=150,
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      annotations=[dict(text=f"<b>{consumed:.0f}</b><br><span style='font-size:11px;color:{C_MUTED}'>{sub}</span>",
                                        x=0.5, y=0.5, showarrow=False, font=dict(size=24, color="#FFFFFF"))])
    return fig


def view_home():
    d, s, u = today(), settings(), unit()
    start, wk = program_start(), week_number(today())
    day_key = suggested_day()
    day = DAYS[day_key]
    D = home_data(ver(), str(d), str(start), day["slug"])
    T = D["targets"]

    if wk < 1:
        prog, frac = f"El bloque inicia el sábado {fecha_corta(start)}", 0.0
    elif wk > WEEKS:
        prog, frac = f"Bloque de {WEEKS} semanas finalizado", 1.0
    else:
        prog, frac = f"Semana {wk} de {WEEKS}", ((wk - 1) + (day_index(d) + 1) / 7) / WEEKS
    rest_day = not day["exercises"]
    w0 = week_start_of(d)
    wk_rows = q("SELECT date, status FROM sessions WHERE user_id=? AND date>=? AND date<=?",
                [UID, str(w0), str(w0 + timedelta(days=6))])
    finished_dates = {str(r[0]) for r in wk_rows if r[1] == "finished"}
    postponed_dates = {str(r[0]) for r in wk_rows if r[1] in ("postponed", "in_progress") and str(r[0]) < str(d)}
    big = f'<div class="gx-hero-num">{wk:02d}</div>' if 1 <= wk <= WEEKS else ""
    segs = week_segments(wk if 1 <= wk <= WEEKS else (WEEKS + 1 if wk > WEEKS else 0))
    today_txt = ("Descanso total. Recuperar también es entrenar." if rest_day
                 else f'{len(day["exercises"])} ejercicios · {sum(e["sets"] for e in day["exercises"])} series')
    H(f'<div class="gx-hero">{big}<div class="gx-muted">{esc(fecha_es(d))}</div>'
      f'<div class="gx-h1" style="margin-top:4px">Hola, {esc(s.get("name", "Luis"))}</div>'
      f'<div class="gx-row" style="margin-top:18px"><span class="gx-title">{esc(prog)}</span>'
      f'<span class="gx-muted">{D["week_finished"]} de {SESSIONS_PER_WEEK} sesiones</span></div>{segs}'
      f'{week_strip(d, finished_dates, postponed_dates)}'
      f'<div class="gx-today"><div class="gx-muted">Hoy toca</div>'
      f'<div class="gx-title lg">{esc(day_key)}</div>'
      f'<div class="gx-muted">{esc(today_txt)}</div></div></div>')
    if not rest_day:
        label = {None: "Comenzar entrenamiento", "in_progress": "Continuar entrenamiento",
                 "finished": "Ver resumen de hoy", "postponed": "Retomar entrenamiento"}.get(
            D["today_status"], "Comenzar entrenamiento")
        today_sid = f"{d}|{day['slug']}"
        if D["today_status"] == "postponed":
            st.button(label, type="primary", key="home_start", on_click=cb_resume_session, args=(today_sid,),
                      **btn_w("play_arrow"))
        else:
            st.button(label, type="primary", key="home_start", on_click=cb_start_training, args=(day_key,),
                      **btn_w("summarize" if D["today_status"] == "finished" else "play_arrow"))
    pend = pending_sessions(ver(), str(d))
    pend_other = [p_ for p_ in pend if p_["id"] != f"{d}|{day['slug']}"]
    if pend_other:
        H(f'<div class="gx-sec">Pendientes de retomar</div>')
        render_pending(pend_other, "home")

    # --- semana ---
    adher = D["week_finished"] / SESSIONS_PER_WEEK * 100
    H(f'<div class="gx-sec">Esta semana</div>'
      f'<div class="gx-muted" style="margin:-4px 0 8px">Del sábado {esc(fecha_corta(w0))} al viernes '
      f'{esc(fecha_corta(w0 + timedelta(days=6)))} · adherencia {adher:.0f}%</div>'
      f'<div class="gx-kpis three">{kpi("Completados", D["week_finished"])}'
      f'{kpi("Iniciados", D["week_started"])}{kpi("Programados", SESSIONS_PER_WEEK)}</div>')

    # --- nutrición ---
    cal, prot, carbs, fat = D["macros"]
    H('<div class="gx-sec">Nutrición de hoy</div>')
    c1, c2 = st.columns([1, 1.25])
    with c1:
        st.plotly_chart(donut(cal, T["cal"]), key="home_donut", config={"displayModeBar": False}, **plot_w())
        rest_k = T["cal"] - cal
        st.caption(f"Restan {rest_k:.0f} kcal" if rest_k >= 0 else f"Superaste la meta por {-rest_k:.0f} kcal")
    with c2:
        H(macro_bar("Proteína", prot, T["prot"], "g", C_ORANGE) + macro_bar("Carbohidratos", carbs, T["carbs"], "g", C_ORANGE)
          + macro_bar("Grasas", fat, T["fat"], "g", C_ORANGE))
    if cal == 0:
        st.caption("Aún no registras comidas hoy. Ve a Comida para empezar.")

    # --- KPIs ---
    if D["body_avg"] is not None:
        body_val = "%.1f kg" % D["body_avg"]
        body = kpi("Peso promedio 7 días", body_val, " · %s registros" % D["body_n"])
    else:
        body = kpi("Peso promedio", "—", " · regístralo en Progreso, pestaña Cuerpo")
    last = D["last"]
    if last:
        lv = kg_to_unit(last[4], u)
        lastk = kpi("Último entrenamiento", esc(last[1].split(":")[0]),
                    f" · {fecha_corta(date.fromisoformat(last[0]))} · {last[3]} series · {lv:,.0f} {u}·rep")
    else:
        lastk = kpi("Último entrenamiento", "—", " · aún sin sesiones")
    sleep_txt = f'{D["sleep"]:.1f} h' if D["sleep"] else "—"
    sleep_extra = f' · prom. 7d {D["sleep_avg"]:.1f} h' if D["sleep_avg"] else " · registra el sueño en Plan"
    rec_extra = (f' · energía {D["energy"]}/5 · dolor {D["soreness"]}/5' if D["energy"] is not None else "")
    H(f'<div class="gx-sec">Resumen</div><div class="gx-grid2">{body}{lastk}{kpi("Sueño hoy", sleep_txt, sleep_extra + rec_extra)}'
      f'{kpi("Semana del bloque", wk if 1 <= wk <= WEEKS else "—", f" de {WEEKS}")}</div>')


# =============================================================================
# 14. VISTA: COMIDA
# =============================================================================
def view_food():
    d = today()
    init_state("meal_date", d)
    st.date_input("Fecha", key="meal_date", max_value=d)
    meal_date = st.session_state["meal_date"]
    st.text_area("Registrar comida", placeholder="Ej. 150 g de pechuga asada con arroz y aguacate…", height=90, key="meal_text")
    if st.button("Analizar con IA", type="primary", key="btn_ai", **btn_w("auto_awesome")):
        text = (st.session_state.get("meal_text") or "").strip()
        if not text:
            st.warning("Escribe qué comiste.")
        else:
            with st.spinner("Calculando macros…"):
                try:
                    est = parse_meal(text)
                    st.session_state["pending_meal"] = dict(
                        date=str(meal_date), desc=", ".join(i.name for i in est.items) or text,
                        est=dict(cal=est.total_calories, prot=est.total_protein_g, carbs=est.total_carbs_g,
                                 fat=est.total_fat_g),
                        items=[i.model_dump() for i in est.items])
                except QuotaError as e:
                    st.warning(str(e))
                    st.session_state["_manual_open"] = True
                except Exception as e:
                    st.error(str(e))
                    st.session_state["_manual_open"] = True
    p = st.session_state.get("pending_meal")
    if p:
        H('<div class="gx-sec">Vista previa</div><div class="gx-muted" style="margin:-4px 0 8px">Revisa los valores antes de guardar.</div>')
        for it in p["items"]:
            H(f'<div class="gx-card" style="padding:10px"><div class="gx-row"><div><b>{esc(it["name"])}</b>'
              f'<div class="gx-muted">P {it["protein_g"]:.0f} g · C {it["carbs_g"]:.0f} g · G {it["fat_g"]:.0f} g</div></div>'
              f'<b style="color:{C_ORANGE}">{it["calories"]:.0f} kcal</b></div></div>')
        e = p["est"]
        H(f'<div class="gx-card"><div class="gx-row"><b>Total</b><b style="color:{C_ORANGE}">{e["cal"]:.0f} kcal</b></div>'
          f'<div class="gx-muted">P {e["prot"]:.0f} g · C {e["carbs"]:.0f} g · G {e["fat"]:.0f} g</div></div>')
        b1, b2 = st.columns([2, 1])
        b1.button("Guardar comida", type="primary", key="btn_save_meal", on_click=cb_save_pending_meal, **btn_w("check"))
        b2.button("Descartar", key="btn_discard_meal", on_click=cb_discard_meal, **btn_w("close"))

    with st.expander("Registro manual (sin IA)", expanded=st.session_state.get("_manual_open", False)):
        st.text_input("Descripción", key="mm_desc")
        a, b = st.columns(2)
        a.number_input("Calorías", 0.0, 5000.0, 0.0, 10.0, key="mm_cal")
        b.number_input("Proteína (g)", 0.0, 500.0, 0.0, 1.0, key="mm_p")
        c, e2 = st.columns(2)
        c.number_input("Carbohidratos (g)", 0.0, 800.0, 0.0, 1.0, key="mm_c")
        e2.number_input("Grasas (g)", 0.0, 300.0, 0.0, 1.0, key="mm_f")
        st.button("Guardar comida manual", key="btn_manual_meal", on_click=cb_save_manual_meal, **btn_w("save"))

    rows = q("SELECT id, description, calories, protein_g, carbs_g, fat_g FROM meals WHERE date=? ORDER BY id", [str(meal_date)])
    H(f'<div class="gx-sec">Comidas del {esc(fecha_corta(meal_date))}</div>')
    if not rows:
        st.caption("Sin comidas registradas para este día.")
    for mid, desc, cal, pr, ca, fa in rows:
        c1, c2 = st.columns([5, 1])
        with c1:
            H(f'<div class="gx-card" style="padding:10px;margin:0"><div class="gx-row"><b style="font-size:13px">{esc(desc or "Comida")}</b>'
              f'<b style="color:{C_ORANGE};white-space:nowrap">{cal:.0f} kcal</b></div>'
              f'<div class="gx-muted">P {pr:.0f} g · C {ca:.0f} g · G {fa:.0f} g</div></div>')
        c2.button(icon_label("Borrar"), key=f"del_meal_{mid}", on_click=cb_delete_meal, args=(mid,),
                  help="Borrar comida", **btn_w("delete"))
    if rows:
        T = get_targets(meal_date)
        tot = [sum(r[i] for r in rows) for i in (2, 3, 4, 5)]
        H('<div style="margin-top:10px">' + macro_bar("Calorías", tot[0], T["cal"], "kcal", C_ORANGE)
          + macro_bar("Proteína", tot[1], T["prot"], "g", C_ORANGE) + "</div>")


# =============================================================================
# 15. VISTA: RUTINA (ejecución del entrenamiento)
# =============================================================================
def view_timer_panel(ex_name, rest_default):
    S = st.session_state
    init_state("rest_target", S.get("_keep_rest", rest_default if rest_default > 0 else 60))
    t = timer_view()
    sound = settings().get("sound", "1") == "1"
    try:
        box = st.container(key="timerbox")
    except TypeError:
        box = st.container()
    with box:
        label = t["label"] if t["state"] != "idle" and t["label"] else f"Descanso · {ex_name}"
        dur = t["duration"] if t["state"] != "idle" else int(S.get("rest_target", 120))
        rem = t["rem"] if t["state"] != "idle" else float(dur)
        try:
            components.html(timer_html(t["state"], rem, dur, label, sound), height=124)
        except Exception:
            H(f'<div class="gx-card"><div class="gx-title">{esc(label)}</div><div class="gx-h1">{mmss(rem)}</div></div>')
        st_ = t["state"]
        if st_ in ("idle",):
            st.button("Iniciar descanso", type="primary", key="tm_start", on_click=cb_timer,
                      args=("start", ex_name), **btn_w("play_arrow"))
        elif st_ == "running":
            st.button("Pausar", type="primary", key="tm_pause", on_click=cb_timer, args=("pause",), **btn_w("pause"))
        elif st_ == "paused":
            st.button("Reanudar", type="primary", key="tm_resume", on_click=cb_timer, args=("resume",), **btn_w("play_arrow"))
        else:  # done
            st.button("Nuevo descanso", type="primary", key="tm_again", on_click=cb_timer, args=("restart",), **btn_w("replay"))
        if st_ != "idle":
            a, b, c = st.columns(3)
            a.button("Reiniciar", key="tm_restart", on_click=cb_timer, args=("restart",), **btn_w("restart_alt"))
            b.button("30 s", key="tm_plus", on_click=cb_timer, args=("plus",), help="Sumar 30 segundos", **btn_w("add"))
            c.button("Fin", key="tm_finish", on_click=cb_timer, args=("finish",), **btn_w("stop"))
    with st.expander("Ajustes del cronómetro"):
        st.number_input("Descanso objetivo (segundos)", 10, 900, step=15, key="rest_target",
                        on_change=keep_state, args=("rest_target", "_keep_rest"))
        init_state("auto_rest_tg", settings().get("auto_rest", "1") == "1")
        st.toggle("Iniciar descanso al completar una serie", key="auto_rest_tg", on_change=cb_auto_rest)
        init_state("sound_tg", sound)
        st.toggle("Sonido y vibración al terminar (si el navegador lo permite)", key="sound_tg",
                  on_change=lambda: set_setting("sound", "1" if st.session_state.get("sound_tg") else "0"))
        st.caption("El aviso funciona mientras la app está abierta; no hay notificaciones en segundo plano.")


def presc_text(e):
    if e["kind"] == "activity":
        return "15 min" if e["rmax"] == 15 else "libre"
    return "%d×%d-%d · RIR %d" % (e["sets"], e["rmin"], e["rmax"], e["rir"])


def _s(v):
    """Texto seguro: None/NaN/NA -> ''."""
    try:
        if v is None or pd.isna(v):
            return ""
    except Exception:
        pass
    return str(v)


def logged_map(sets_df, ex_name, u):
    m = {}
    if sets_df is None or sets_df.empty:
        return m
    for _, r in sets_df[sets_df["exercise"] == ex_name].iterrows():
        m[int(r["set_no"])] = dict(
            id=_s(r["id"]), w=round(kg_to_unit(0.0 if pd.isna(r["weight_kg"]) else float(r["weight_kg"]), u), 2),
            reps=0 if pd.isna(r["reps"]) else int(r["reps"]),
            rir=None if pd.isna(r["rir"]) else int(r["rir"]), status=_s(r["status"]), notes=_s(r["notes"]),
            started_at=_s(r["started_at"]), finished_at=_s(r["finished_at"]),
            rest_actual=None if pd.isna(r["rest_actual"]) else float(r["rest_actual"]))
    return m


def view_exercise(day, sid, d_str, sets_df, ex, auto_rest):
    u = unit()
    name, es = ex["name"], slug(ex["name"])
    activity = ex["kind"] == "activity"
    rest, inc = rest_for(ex), inc_for(ex, u)
    sessions = summarize_sessions(exercise_history(name, sid, hver()), u)
    last = sessions[-1] if sessions else None
    prev = sessions[-2] if len(sessions) >= 2 else None
    lmap = logged_map(sets_df, name, u)

    rec = recommend(sessions, ex["rmin"], ex["rmax"], ex["sets"], ex["rir"], inc)
    tgt = load_targets().get(name)
    prior_date = last["date"] if last else None
    tgt_ok = False
    if tgt and tgt["unit"] == u and tgt["weight"] is not None:
        so = date.fromisoformat(tgt["set_on"])
        tgt_ok = so == today() or prior_date is None or so > prior_date
    default_w = float(tgt["weight"]) if tgt_ok else (last["weight"] if last else 0.0)

    n_presc = ex["sets"]
    extra_key = f"extra_{sid}_{es}"
    n_total = max(n_presc + st.session_state.get(extra_key, 0), max(lmap) if lmap else 0, 1)
    done_n = sum(1 for n in range(1, n_presc + 1) if n in lmap and lmap[n]["status"] in ("completed", "skipped"))
    completed = [lmap[n] for n in sorted(lmap) if lmap[n]["status"] == "completed"]
    complete = done_n >= n_presc

    # --- tarjeta de resumen del ejercicio ---
    if last:
        last_txt = " · ".join(f"{fmt_w(s['w'])}×{s['reps']}" + (f" (RIR {s['rir']})" if s["rir"] is not None else "")
                              for s in last["sets"])
        last_line = f"Última sesión ({fecha_corta(last['date'])}): {last_txt}"
    else:
        last_line = "Sin sesiones previas registradas"
    trend = ""
    if prev and last and prev["volume"]:
        pct = (last["volume"] / prev["volume"] - 1) * 100
        trend = (f'<span class="gx-up">{ico("up")} {pct:+.0f}% de volumen frente a la sesión previa</span>' if pct > 0.5 else
                 f'<span class="gx-down">{ico("down")} {pct:+.0f}% de volumen frente a la sesión previa</span>' if pct < -0.5
                 else f'{ico("flat")} Volumen estable')
    state_b = badge("Completado", "ok") if complete else (badge(f"{done_n}/{n_presc}", "run") if done_n else badge("Pendiente", "pend"))
    rep_txt = "Duración 15 min" if activity and ex["rmax"] == 15 else ("Sin series: marca completado" if activity else
              f'{ex["sets"]} series × {ex["rmin"]}–{ex["rmax"]} reps' if ex["rmin"] != ex["rmax"] else f'{ex["sets"]} × {ex["rmin"]}')
    chips = chip(rep_txt, "blue") + ("" if activity else chip(f'RIR objetivo {ex["rir"]}', "coral")) + \
        chip(f'Descanso {rest} s' if rest else "Descanso libre", "orange")
    H(f'<div class="gx-card"><div class="gx-row"><div><div class="gx-title">{esc(name)}</div>'
      f'<div class="gx-muted">{esc(ex["muscle"])}</div></div>{state_b}</div><div>{chips}</div>'
      f'<div class="gx-text">{esc(last_line)}</div><div class="gx-muted" style="margin-top:2px">{trend}</div></div>')

    # --- recomendación de carga ---
    if not activity:
        if tgt_ok:
            H(f'<div class="gx-card" style="padding:10px"><span class="gx-badge ok">Carga confirmada</span> '
              f'<b>{fmt_w(tgt["weight"])} {u}</b> <span class="gx-muted">({"aceptada" if tgt["status"] == "accepted" else "modificada"})</span></div>')
        elif rec["action"] == "no_data":
            H(f'<div class="gx-card" style="padding:10px"><div class="gx-title">Carga recomendada</div>'
              f'<div class="gx-text">{esc(rec["reason"])}</div></div>')
        else:
            head = (f'Subir a {fmt_w(rec["weight"])} {u} (+{fmt_w(inc)})' if rec["action"] == "increase"
                    else f'Mantener {fmt_w(rec["weight"])} {u}')
            H(f'<div class="gx-card" style="padding:10px"><div class="gx-row"><div class="gx-title">Carga recomendada: {esc(head)}</div>'
              f'<span class="gx-badge pend">Pendiente de confirmación</span></div>'
              f'<div class="gx-text">{esc(rec["reason"])}</div></div>')
            ca, cb = st.columns([3, 2])
            init_state(f"tw_{es}", float(rec["weight"]))
            ca.number_input(f"Carga a usar ({u})", 0.0, 1000.0, step=float(inc), key=f"tw_{es}", format="%g")
            with cb:
                st.markdown("<div style='height:1.6rem'></div>", unsafe_allow_html=True)
                st.button("Confirmar", key=f"acc_{es}", on_click=cb_accept_target, type="primary",
                          args=(name, es, sid, rec["weight"],
                                rec["last_weight"], rec["action"], rec["reason"], n_presc), **btn_w("check"))
        if last:
            chk_last = overload_check(last, ex["rmin"], ex["rmax"], ex["sets"], ex["rir"])
            H(overload_card(chk_last, "¿Toca subir?", inc, u,
                            f"Según tu última sesión ({fecha_corta(last['date'])})"))
        if rec.get("fatigue"):
            st.warning("Tu volumen en este ejercicio bajó durante las últimas 3 sesiones. Antes de tocar la carga revisa "
                       "descanso entre series, sueño, calorías/proteína y fatiga acumulada.")

    # --- series ---
    if activity:
        row = lmap.get(1)
        rid = f"{sid}|{es}|1"
        if row and row["status"] == "completed":
            H(f'<div class="gx-card" style="padding:10px"><span class="gx-badge ok">Completado</span></div>')
            st.button("Deshacer", key=f"undo_{rid}", on_click=cb_delete_set, args=(rid,), **btn_w("undo"))
        else:
            st.button("Marcar completado", type="primary", key=f"ok_{rid}", on_click=cb_save_set,
                      args=(rid, sid, d_str, name, 1, "completed", 0, False, True), **btn_w("check"))
    else:
        for n in range(1, n_total + 1):
            rid = f"{sid}|{es}|{n}"
            row = lmap.get(n)
            if row and row["status"] != "skipped":
                dw, dr, di = row["w"], row["reps"], row["rir"] if row["rir"] is not None else ex["rir"]
            elif n > 1 and (n - 1) in lmap and lmap[n - 1]["status"] == "completed":
                p_ = lmap[n - 1]
                dw, dr, di = p_["w"], p_["reps"], p_["rir"] if p_["rir"] is not None else ex["rir"]
            else:
                ls = last["sets"][n - 1] if last and n - 1 < len(last["sets"]) else None
                dw, dr, di = default_w, (ls["reps"] if ls else ex["rmin"]), ex["rir"]
            init_state(f"w_{rid}", float(dw))
            init_state(f"r_{rid}", int(dr))
            init_state(f"i_{rid}", int(min(max(di, 0), 5)))
            init_state(f"n_{rid}", row["notes"] if row else "")
            status = row["status"] if row else "pending"
            b = {"completed": badge("Completada", "ok"), "skipped": badge("Omitida", "skip")}.get(status, badge("Pendiente", "pend"))
            info = []
            if n > 1 and (n - 1) in lmap and lmap[n - 1]["status"] == "completed":
                p_ = lmap[n - 1]
                info.append(f"Serie anterior: {fmt_w(p_['w'])} {u} × {p_['reps']}" + (f" · RIR {p_['rir']}" if p_["rir"] is not None else ""))
            if last and n - 1 < len(last["sets"]):
                ls = last["sets"][n - 1]
                info.append(f"Última sesión: {fmt_w(ls['w'])} × {ls['reps']}")
            extra = " · ".join(info)
            with st.container(border=True):
                extra_tag = ' <span class="gx-muted">extra</span>' if n > n_presc else ""
                H(f'<div class="gx-row"><span class="gx-title" style="font-size:19px">Serie {n}'
                  f'{extra_tag}</span>{b}</div>'
                  f'<div class="gx-muted">{esc(extra)}</div>')
                c1, c2, c3 = st.columns(3)
                c1.number_input(f"Peso ({u})", 0.0, 1000.0, step=float(inc), key=f"w_{rid}", format="%g")
                c2.number_input("Reps", 0, 100, step=1, key=f"r_{rid}")
                c3.selectbox("RIR", RIR_OPTS, key=f"i_{rid}", format_func=lambda v: "5+" if v >= 5 else str(v))
                b1, b2, b3 = st.columns([3, 2, 1])
                b1.button("Actualizar" if status == "completed" else "Completar", key=f"ok_{rid}",
                          type="secondary" if status == "completed" else "primary", on_click=cb_save_set,
                          args=(rid, sid, d_str, name, n, "completed", rest, auto_rest),
                          **btn_w("sync" if status == "completed" else "check"))
                b2.button("Omitir", key=f"skip_{rid}", on_click=cb_save_set,
                          args=(rid, sid, d_str, name, n, "skipped", rest, auto_rest), **btn_w("skip_next"))
                if row:
                    b3.button(icon_label("Borrar"), key=f"del_{rid}", on_click=cb_delete_set, args=(rid,),
                              help="Borrar serie", **btn_w("delete"))
                with st.expander("Nota de la serie"):
                    st.text_input("Nota", key=f"n_{rid}", label_visibility="collapsed",
                                  placeholder="Técnica, equipo, dolor, etc.")
        st.button("Añadir serie", key=f"add_{es}", on_click=cb_add_extra, args=(extra_key,), **btn_w("add"))

    # --- resumen en tiempo real ---
    reps_t = sum(r["reps"] for r in completed)
    vol = sum(r["w"] * r["reps"] for r in completed)
    rest_t = sum(r["rest_actual"] for r in completed if r["rest_actual"])
    fts = [z for z in (parse_iso(r["finished_at"]) for r in completed if r["finished_at"]) if z]
    elapsed = (max(fts) - min(fts)).total_seconds() if len(fts) >= 2 else None
    if elapsed and elapsed > 3 * 3600:
        elapsed = None
    cur_load = fmt_w(completed[-1]["w"]) + f" {u}" if completed else "—"
    vs = "—"
    if last and last["volume"] and completed:
        vs = f"{(vol / last['volume'] - 1) * 100:+.0f}% vol."
    next_set = next((n for n in range(1, n_total + 1) if n not in lmap), None)
    if not activity:
        H('<div class="gx-sec">Resumen del ejercicio</div>'
          '<div class="gx-kpis">' + kpi("Series completadas", f"{len(completed)} de {n_presc}") + kpi("Repeticiones", reps_t)
          + kpi(f"Volumen ({u}·rep)", fmt_n(vol)) + kpi("Descanso acumulado", mmss(rest_t) if rest_t else "—")
          + kpi("Tiempo del ejercicio", hms(elapsed) if elapsed else "—") + kpi("Carga actual", cur_load)
          + kpi("vs última sesión", vs) + kpi("Próxima serie", f"#{next_set}" if next_set and not complete else "—",
                                              f" · RIR objetivo {ex['rir']}") + "</div>")

    if not activity and completed:
        cur_s = session_from_sets(completed)
        chk_now = overload_check(cur_s, ex["rmin"], ex["rmax"], ex["sets"], ex["rir"])
        if complete or chk_now["ready"]:
            H(overload_card(chk_now, "Evaluación de hoy", inc, u,
                            "Con lo que registraste hoy, para la próxima sesión"))
        else:
            H(f'<div class="gx-muted" style="margin:6px 0 4px">Hacia subir carga: {chk_now["at_top"]} de {ex["sets"]} '
              f'series en el tope de {ex["rmax"]} reps con RIR ≥ {ex["rir"]}.</div>')

    if complete:
        names = [e["name"] for e in day["exercises"]]
        i = names.index(name)
        H(f'<div class="gx-card good" style="margin-top:10px"><div class="gx-title gx-up">{ico("check", size=18)} Ejercicio completado</div>'
          f'<div class="gx-text">{len(completed)} series · {reps_t} repeticiones · {fmt_n(vol)} {u}·rep'
          f'{" · " + vs + " vs la última sesión" if vs != "—" else ""}</div></div>')
        if i + 1 < len(names):
            rests = {e["name"]: rest_for(e) for e in day["exercises"]}
            st.button("Siguiente ejercicio", type="primary", key=f"next_{es}", on_click=cb_set_exercise,
                      args=(day["slug"], names), kwargs=dict(step=1, rests=rests), **btn_w("arrow_forward"))

    # --- historial / técnica / ajustes ---
    if st.toggle("Ver historial", key=f"hist_{es}"):
        if not sessions:
            st.caption("Aún no hay sesiones previas.")
        else:
            rows = [dict(Fecha=fecha_corta(s["date"]), Series=s["n_sets"], Carga=f"{fmt_w(s['weight'])} {u}",
                         Reps=" / ".join(str(z["reps"]) for z in s["sets"]),
                         RIR=" / ".join("—" if z["rir"] is None else str(z["rir"]) for z in s["sets"]),
                         Volumen=fmt_n(s["volume"])) for s in reversed(sessions[-8:])]
            st.dataframe(pd.DataFrame(rows), hide_index=True, **df_w())
    with st.expander("Indicaciones técnicas"):
        st.write(TIPS.get(name, "Controla el recorrido, mantén la técnica constante y termina cada serie con el RIR objetivo."))
    if not activity:
        with st.expander("Ajustes de este ejercicio"):
            init_state(f"cfg_rest_{es}", int(rest))
            init_state(f"cfg_inc_{es}", float(inc))
            a, b = st.columns(2)
            a.number_input("Descanso (s)", 0, 600, step=15, key=f"cfg_rest_{es}")
            b.number_input(f"Incremento mínimo ({u})", 0.25, 50.0, step=0.25, key=f"cfg_inc_{es}", format="%g")
            st.button("Guardar ajustes", key=f"cfgsave_{es}", on_click=cb_save_ex_cfg, args=(name, es, u), **btn_w("save"))


def session_summary(df_all, sid, d, u):
    cur = df_all[df_all["session_id"] == sid]
    out = dict(sets=len(cur), reps=int(cur["reps"].sum()) if len(cur) else 0, exercises=cur["exercise"].nunique() if len(cur) else 0,
               volume=kg_to_unit(float((cur["weight_kg"] * cur["reps"]).sum()), u) if len(cur) else 0.0, prs=[], prev=None)
    if cur.empty:
        return out
    prior = df_all[(df_all["session_id"] != sid) & (df_all["date"] < pd.Timestamp(d))]
    for exn, g in cur.groupby("exercise"):
        p = prior[prior["exercise"] == exn]
        if p.empty:
            continue
        top, ptop = g["weight_kg"].max(), p["weight_kg"].max()
        reps_top = int(g[g["weight_kg"] == top]["reps"].max())
        if top > ptop + 1e-6:
            out["prs"].append((exn, f"{fmt_w(kg_to_unit(top, u))} {u} × {reps_top} (carga máxima)"))
        elif abs(top - ptop) < 1e-6 and reps_top > int(p[p["weight_kg"] == ptop]["reps"].max()):
            out["prs"].append((exn, f"{reps_top} reps con {fmt_w(kg_to_unit(top, u))} {u} (más repeticiones)"))
    suffix = "|" + str(sid).split("|")[-1]
    cand = df_all[df_all["session_id"].astype(str).str.endswith(suffix) & (df_all["session_id"] != sid) & (df_all["date"] < pd.Timestamp(d))]
    if not cand.empty:
        last_sid = cand.sort_values("date")["session_id"].iloc[-1]
        pv = cand[cand["session_id"] == last_sid]
        out["prev"] = dict(date=pv["date"].iloc[0].date(), sets=len(pv),
                           volume=kg_to_unit(float((pv["weight_kg"] * pv["reps"]).sum()), u))
    return out


def view_session_done(sid, sess, d, u):
    dur = session_active_seconds(sess) if sess.get("finished_at") else 0
    S = session_summary(all_sets_df(ver()), sid, d, u)
    prs = "".join(f'<div class="gx-text" style="color:{C_ORANGE}">{ico("trophy", C_ORANGE)} <b style="color:#fff">{esc(e)}</b>: {esc(t)}</div>'
                  for e, t in S["prs"]) or \
        '<div class="gx-muted">Sin récords nuevos en esta sesión.</div>'
    diff = ""
    if S["prev"]:
        dv = S["volume"] - S["prev"]["volume"]
        pct = f" ({dv / S['prev']['volume'] * 100:+.0f}%)" if S["prev"]["volume"] else ""
        diff = (f'<div class="gx-text">vs sesión del {esc(fecha_corta(S["prev"]["date"]))}: '
                f'{sign(S["sets"] - S["prev"]["sets"])} series · {sign(dv, 0)} {u}·rep{pct}</div>')
    H(f'<div class="gx-card good"><div class="gx-title lg">Sesión finalizada</div>'
      f'<div class="gx-kpis" style="margin-top:8px">{kpi("Duración", hms(dur))}{kpi("Ejercicios", S["exercises"])}'
      f'{kpi("Series completadas", S["sets"])}{kpi("Repeticiones", S["reps"])}'
      f'{kpi(f"Volumen ({u}·rep)", fmt_n(S["volume"]))}</div>{diff}<div style="margin-top:8px">{prs}</div></div>')
    day = DAYS.get(sess.get("day_name") or "")
    if day:
        df_all = all_sets_df(ver())
        cur = df_all[df_all["session_id"] == sid]
        up, keep = [], []
        for e in day["exercises"]:
            if e["kind"] == "activity":
                continue
            ss_ = summarize_sessions(cur[cur["exercise"] == e["name"]], u)
            if not ss_:
                continue
            chk = overload_check(ss_[-1], e["rmin"], e["rmax"], e["sets"], e["rir"])
            if chk["ready"]:
                up.append(f'<div class="gx-text gx-up">{ico("up")} <b style="color:#fff">{esc(e["name"])}</b>: {fmt_w(chk["weight"])} → '
                          f'<b>{fmt_w(chk["weight"] + inc_for(e, u))} {u}</b></div>')
            else:
                keep.append(f'<div class="gx-muted" style="margin-top:6px">{ico("flat")} {esc(e["name"])}: mantén {fmt_w(chk["weight"])} {u} '
                            f'({min(chk["at_top"], e["sets"])} de {e["sets"]} series en el tope)</div>')
        if up or keep:
            up_html = "".join(up) or '<div class="gx-muted">Ningún ejercicio cumple aún para subir.</div>'
            H(f'<div class="gx-card"><div class="gx-title">Sobrecarga progresiva: próxima sesión</div>'
              f'{up_html}'
              f'{"".join(keep)}<div class="gx-muted" style="margin-top:8px">La subida se confirma en la próxima '
              f'sesión; la app nunca cambia la carga sola.</div></div>')
    st.button("Reabrir sesión", key="reopen", on_click=cb_reopen_session, args=(sid,), **btn_w("lock_open"))


def view_postpone_panel(sid, sets_df):
    """Opción para dejar el resto del entrenamiento para otro día."""
    done = int((sets_df["status"] == "completed").sum()) if not sets_df.empty else 0
    with st.expander("Posponer entrenamiento"):
        st.caption(f"Se guardan las {done} series que ya hiciste. Cuando la retomes, continúas justo donde te quedaste; "
                   "la sesión conserva su fecha original para el historial y la progresión.")
        init_state("pp_reason", POSTPONE_REASONS[0])
        st.selectbox("Motivo", POSTPONE_REASONS, key="pp_reason")
        init_state("pp_date", today() + timedelta(days=1))
        if st.session_state["pp_date"] < today():
            st.session_state["pp_date"] = today()
        st.date_input("Retomar el", key="pp_date", min_value=today())
        st.text_input("Nota (opcional)", key="pp_note", placeholder="Ej. mareo a mitad de la sesión")
        if st.session_state.get("pp_reason") in ("Me sentí mal", "Dolor o molestia"):
            st.caption("Si el malestar es fuerte o no se va, descansa y consúltalo con un médico antes de volver a entrenar.")
        st.button("Posponer", key=f"pp_btn_{sid}", on_click=cb_postpone_session, args=(sid,), **btn_w("schedule"))


def view_postponed_card(sess, day_key):
    d_ = date.fromisoformat(sess["date"])
    to = date.fromisoformat(sess["postponed_to"]) if sess.get("postponed_to") else None
    r = q1("SELECT COUNT(*) FROM sets_log WHERE session_id=? AND status IN ('completed','skipped')", [sess["id"]])
    total = sum(e["sets"] for e in DAYS[day_key]["exercises"])
    done = r[0] if r else 0
    when = f" Programada para el {DIAS_ES[to.weekday()]} {fecha_corta(to)}." if to else ""
    H(f'<div class="gx-card" style="border-color:rgba(255,159,67,.45)"><div class="gx-row">'
      f'<div class="gx-title lg">{esc(day_key)}</div>{badge("Pospuesta", "skip")}</div>'
      f'<div class="gx-muted">Del {DIAS_ES[d_.weekday()]} {fecha_corta(d_)} · {esc((sess.get("postpone_reason") or "").lower())}</div>'
      f'<div class="gx-text">{done} de {total} series hechas.{esc(when)}</div>'
      f'{plain_bar(done / total if total else 0, C_ORANGE)}</div>')
    a, b = st.columns(2)
    a.button("Retomar ahora", type="primary", key=f"res_now_{sess['id']}", on_click=cb_resume_session,
             args=(sess["id"],), **btn_w("play_arrow"))
    b.button("Terminar así", key=f"end_now_{sess['id']}", on_click=cb_close_pending,
             args=(sess["id"],), **btn_w("flag"))


def view_workout():
    flash_show()
    u = unit()
    d = today()
    S = st.session_state
    resumed = None
    if S.get("active_sid"):
        rs = get_session(S["active_sid"])
        if rs and rs["status"] == "in_progress" and rs["day_name"] in DAYS and rs["date"] != str(d):
            resumed = rs
        else:
            S.pop("active_sid", None)

    if resumed:
        day_key = resumed["day_name"]
        day = DAYS[day_key]
        sid, d_str = resumed["id"], resumed["date"]
        d = date.fromisoformat(d_str)
        H(f'<div class="gx-card flat" style="padding:12px 14px"><div class="gx-muted">Retomando la sesión del '
          f'{DIAS_ES[d.weekday()]} {fecha_corta(d)}. Las series se guardan con esa fecha.</div></div>')
        st.button("Ir a la sesión de hoy", key="back_today", on_click=cb_back_today, **btn_w("today"))
    else:
        render_pending(pending_sessions(ver(), str(d)), "rut", exclude={f"{d}|{DAYS[S.get('day_pick', suggested_day())]['slug']}"}
                       if S.get("day_pick") in DAYS else ())
        init_state("day_pick", S.get("_keep_day", suggested_day()))
        if S["day_pick"] not in DAYS:
            S["day_pick"] = suggested_day()
        day_key = st.selectbox("Sesión", DAY_KEYS, key="day_pick",
                               format_func=lambda k: f"{k} · {weekday_name_for(k)}",
                               on_change=keep_state, args=("day_pick", "_keep_day"))
        if day_key != suggested_day():
            st.caption(f"Hoy ({DIAS_ES[d.weekday()]}) toca {suggested_day()}. Estás registrando otra sesión con fecha de hoy.")
        day = DAYS[day_key]
        sid, d_str = f"{d}|{day['slug']}", str(d)
    if not day["exercises"]:
        H('<div class="gx-card"><div class="gx-title lg">Descanso total</div><div class="gx-text">Duerme bien, '
          'hidrátate y cumple tu meta de proteína. La recuperación también cuenta.</div></div>')
        return
    sess = get_session(sid)
    if not sess:
        def up_tag(e):
            if e["kind"] == "activity":
                return ""
            ss_ = summarize_sessions(exercise_history(e["name"], sid, hver()), u)
            if not ss_:
                return ""
            chk = overload_check(ss_[-1], e["rmin"], e["rmax"], e["sets"], e["rir"])
            if not chk["ready"]:
                return ""
            return (f'<div style="font-size:11.5px;color:{C_GREEN};margin-top:2px">{ico("up", size=12)} Toca subir: '
                    f'{fmt_w(chk["weight"])} → {fmt_w(chk["weight"] + inc_for(e, u))} {u}</div>')
        rows = "".join(
            f'<div class="gx-list-row"><span class="n">{i}</span><div style="flex:1;min-width:0">'
            f'<div style="font-size:14px;font-weight:600">{esc(e["name"])}</div>{up_tag(e)}</div>'
            f'<span class="gx-muted" style="white-space:nowrap">{esc(presc_text(e))}</span></div>'
            for i, e in enumerate(day["exercises"], 1))
        H(f'<div class="gx-card"><div class="gx-title lg">{esc(day_key)}</div><div class="gx-muted">{len(day["exercises"])} ejercicios · '
          f'{esc(weekday_name_for(day_key))}</div><div style="margin-top:8px">{rows}</div></div>')
        st.button("Comenzar entrenamiento", type="primary", key="start_session", on_click=cb_start_training,
                  args=(day_key, False), **btn_w("play_arrow"))
        return

    if sess["status"] == "finished":
        view_session_done(sid, sess, d, u)
        return
    if sess["status"] == "postponed":
        view_postponed_card(sess, day_key)
        return

    sets_df = session_sets(sid)
    names = [e["name"] for e in day["exercises"]]
    comp = sets_df[sets_df["status"] == "completed"] if not sets_df.empty else sets_df
    comp_n = len(comp)
    prog_n = sum(e["sets"] for e in day["exercises"])
    vol = kg_to_unit(float((comp["weight_kg"] * comp["reps"]).sum()), u) if comp_n else 0.0
    elapsed = session_active_seconds(sess, end=now())
    H(f'<div class="gx-card"><div class="gx-row"><div class="gx-title lg">{esc(day_key)}</div>{badge("En curso", "run")}</div>'
      f'<div class="gx-kpis three" style="margin-top:8px">{kpi("Series", f"{comp_n}/{prog_n}")}'
      f'{kpi("Tiempo", hms(elapsed))}{kpi(f"Vol. ({u}·rep)", fmt_n(vol))}</div>'
      f'{plain_bar(comp_n / prog_n if prog_n else 0, C_BLUE)}</div>')

    # selector de ejercicio
    done_by_ex = {}
    if not sets_df.empty:
        for exn, g in sets_df[sets_df["status"].isin(["completed", "skipped"])].groupby("exercise"):
            done_by_ex[exn] = len(g)
    pick_key = f"ex_pick_{day['slug']}"
    first_open = next((e["name"] for e in day["exercises"] if done_by_ex.get(e["name"], 0) < e["sets"]), names[0])
    init_state(pick_key, st.session_state.get(f"_keep_ex_{day['slug']}", first_open))
    if st.session_state[pick_key] not in names:
        st.session_state[pick_key] = first_open
    rests = {e["name"]: rest_for(e) for e in day["exercises"]}
    n1, n2, n3 = st.columns([1, 5, 1])
    n1.button(icon_label("<"), key="ex_prev", on_click=cb_set_exercise, args=(day["slug"], names),
              kwargs=dict(step=-1, rests=rests), help="Ejercicio anterior", **btn_w("chevron_left"))
    n2.selectbox("Ejercicio", names, key=pick_key, label_visibility="collapsed",
                 format_func=lambda n_: f"{names.index(n_) + 1}. {n_} · {done_by_ex.get(n_, 0)}/{next(e['sets'] for e in day['exercises'] if e['name'] == n_)}",
                 on_change=cb_set_exercise, args=(day["slug"], names), kwargs=dict(rests=rests))
    n3.button(icon_label(">"), key="ex_next", on_click=cb_set_exercise, args=(day["slug"], names),
              kwargs=dict(step=1, rests=rests), help="Ejercicio siguiente", **btn_w("chevron_right"))
    ex = next(e for e in day["exercises"] if e["name"] == st.session_state[pick_key])

    view_timer_panel(ex["name"], rest_for(ex))
    view_exercise(day, sid, d_str, sets_df, ex, settings().get("auto_rest", "1") == "1" and rest_for(ex) > 0)

    pending = sum(1 for e in day["exercises"] if done_by_ex.get(e["name"], 0) < e["sets"])
    st.divider()
    if pending:
        st.caption(f"Quedan {pending} ejercicio(s) con series sin registrar.")
    st.button("Finalizar entrenamiento", type="primary", key="finish_session", on_click=cb_finish_session,
              args=(sid,), **btn_w("flag"))
    view_postpone_panel(sid, sets_df)


# =============================================================================
# 16. VISTA: PROGRESO
# =============================================================================
def view_loads(df_all, u):
    if df_all.empty:
        st.info("Aún no hay series registradas. Completa tu primera sesión para ver la evolución de cargas.")
        return
    order = df_all.groupby("exercise")["date"].max().sort_values(ascending=False).index.tolist()
    pick = st.selectbox("Ejercicio", order, key="prog_ex")
    ss = summarize_sessions(df_all[df_all["exercise"] == pick], u)
    if not ss:
        st.info("Este ejercicio todavía no tiene registros válidos.")
        return
    best = max(ss[-8:], key=lambda s: (s["top"], s["top_reps"]))
    H('<div class="gx-kpis three">' + kpi("Carga máxima", f"{fmt_w(max(s['top'] for s in ss))} {u}")
      + kpi("Última carga", f"{fmt_w(ss[-1]['weight'])} {u}")
      + kpi("Mejor reciente", f"{fmt_w(best['top'])}×{best['top_reps']}", f" · {fecha_corta(best['date'])}") + "</div>")
    dates = [pd.Timestamp(s["date"]) for s in ss]
    ys = [s["weight"] for s in ss]
    cd = [[s["n_sets"], " / ".join(str(z["reps"]) for z in s["sets"]),
           " / ".join("—" if z["rir"] is None else str(z["rir"]) for z in s["sets"])] for s in ss]
    fig = go.Figure(go.Scatter(
        x=dates, y=ys, mode="lines+markers", line=dict(color=C_BLUE, width=3, shape="linear"),
        marker=dict(size=9, color="#FFFFFF", line=dict(color=C_BLUE, width=2)), customdata=cd, connectgaps=False,
        hovertemplate="<b>%{x|%d/%m/%Y}</b><br>Carga: %{y} " + u + "<br>Series: %{customdata[0]}"
                      "<br>Reps: %{customdata[1]}<br>RIR: %{customdata[2]}<extra></extra>"))
    rng = nice_range(ys)
    base_layout(fig, 280)
    fig.update_yaxes(range=rng, title_text=f"Carga ({u})" + (" · eje truncado" if rng[0] > 0 else ""))
    style_time_axis(fig, dates)
    show_chart(fig, "ch_load")
    if rng[0] > 0:
        st.caption("El eje vertical no empieza en cero para que los cambios pequeños se vean. Toca cada punto para ver el valor exacto.")
    fig2 = go.Figure(go.Bar(x=dates, y=[s["volume"] for s in ss], marker_color=C_BLUE,
                            hovertemplate="<b>%{x|%d/%m/%Y}</b><br>Volumen: %{y:,.0f} " + u + "·rep<extra></extra>"))
    base_layout(fig2, 220)
    fig2.update_yaxes(range=[0, max(s["volume"] for s in ss) * 1.15 or 1], title_text=f"Volumen ({u}·rep)")
    style_time_axis(fig2, dates)
    show_chart(fig2, "ch_vol")
    rows = [dict(Fecha=fecha_corta(s["date"]), Series=s["n_sets"], Carga=f"{fmt_w(s['weight'])} {u}",
                 Reps=" / ".join(str(z["reps"]) for z in s["sets"]),
                 RIR=" / ".join("—" if z["rir"] is None else str(z["rir"]) for z in s["sets"])) for s in reversed(ss[-12:])]
    st.dataframe(pd.DataFrame(rows), hide_index=True, **df_w())
    if any(str(s["sid"]).startswith("legacy|") for s in ss):
        st.caption("Incluye registros del sistema anterior (se asumen en kg y sin RIR).")


def view_compare(df_all, u):
    if df_all.empty:
        st.info("Necesitas al menos dos sesiones del mismo ejercicio para comparar.")
        return
    order = df_all.groupby("exercise")["date"].max().sort_values(ascending=False).index.tolist()
    pick = st.selectbox("Ejercicio", order, key="cmp_ex")
    ss = summarize_sessions(df_all[df_all["exercise"] == pick], u)
    if len(ss) < 2:
        st.info("Solo hay una sesión registrada; la comparación aparece desde la segunda.")
        return
    prev, cur = ss[-2], ss[-1]
    c = compare_sessions(prev, cur)

    def col(s, title):
        sets = " · ".join(f"{fmt_w(z['w'])}×{z['reps']}" for z in s["sets"])
        rir = f"{s['avg_rir']:.1f}" if s["avg_rir"] is not None else "—"
        return (f'<div class="gx-week"><b>{title}</b><br>{esc(fecha_corta(s["date"]))}<br>Carga {fmt_w(s["weight"])} {u}<br>'
                f'{esc(sets)}<br>Reps {s["reps_total"]} · Vol {fmt_n(s["volume"])}<br>RIR prom. {rir}</div>')
    H(f'<div class="gx-grid2">{col(prev, "Anterior")}{col(cur, "Actual")}</div>')
    pct = f" ({c['dvol_pct']:+.0f}%)" if c["dvol_pct"] is not None else ""
    rir = f"{c['drir']:+.1f}" if c["drir"] is not None else "—"
    H(f'<div class="gx-card" style="margin-top:10px"><div class="gx-title">{esc(c["verdict"])}</div>'
      f'<div class="gx-kpis three" style="margin-top:8px">{kpi("Δ Carga", sign(c["dw"], 2, " " + u))}{kpi("Δ Reps", sign(c["dreps"]))}'
      f'{kpi("Δ Volumen", sign(c["dvol"], 0) + pct)}{kpi("Δ RIR", rir)}{kpi("Δ Series", sign(c["dsets"]))}</div>'
      + "".join(f'<div class="gx-note">{esc(n)}</div>' for n in c["notes"]) + "</div>")
    ex = next((e for dd in DAYS.values() for e in dd["exercises"] if e["name"] == pick), None)
    if ex:
        r = recommend(ss, ex["rmin"], ex["rmax"], ex["sets"], ex["rir"], inc_for(ex, u))
        H(f'<div class="gx-card"><div class="gx-title">Recomendación para la próxima sesión</div>'
          f'<div class="gx-text"><b>{"Subir a " + fmt_w(r["weight"]) + " " + u if r["action"] == "increase" else "Mantener " + fmt_w(r["weight"]) + " " + u}</b>'
          f' (pendiente de tu confirmación)</div><div class="gx-text">{esc(r["reason"])}</div></div>')


def build_weeks(df_all, sess, meals, body, rec, closed, start, u):
    weeks = []
    today_ = pd.Timestamp(today())

    def wk_of(df):
        return ((df["date"] - pd.Timestamp(start)).dt.days // 7 + 1) if not df.empty else pd.Series(dtype=int)
    sd = df_all.copy()
    if not sd.empty:
        sd["wk"] = wk_of(sd)
        sd["group"] = sd["exercise"].map(lambda n_: EX_GROUP.get(n_, "Otros"))
    ss = sess.copy()
    if not ss.empty:
        ss["wk"] = wk_of(ss)
    ml = meals.copy()
    if not ml.empty:
        ml["wk"] = wk_of(ml)
    bd = body.copy()
    if not bd.empty:
        bd["wk"] = wk_of(bd)
    rc = rec.copy()
    if not rc.empty:
        rc["wk"] = wk_of(rc)
    for n in range(1, WEEKS + 1):
        w0 = pd.Timestamp(start) + pd.Timedelta(days=7 * (n - 1))
        w = dict(n=n, start=w0.date())
        s_ = ss[ss["wk"] == n] if not ss.empty else ss
        w["finished"] = int((s_["status"] == "finished").sum()) if not s_.empty else 0
        w["started"] = len(s_)
        w["adherence"] = w["finished"] / SESSIONS_PER_WEEK * 100
        g = sd[sd["wk"] == n] if not sd.empty else sd
        w["sets"] = len(g)
        w["by_group"] = g.groupby("group").size().sort_values(ascending=False).to_dict() if len(g) else {}
        w["top"] = {nm: kg_to_unit(float(g[g["exercise"] == nm]["weight_kg"].max()), u)
                    for nm in g["exercise"].unique()} if len(g) else {}
        b = bd[bd["wk"] == n] if not bd.empty else bd
        w["bw"] = float(b["weight_kg"].mean()) if len(b) and b["weight_kg"].notna().any() else None
        w["waist"] = float(b.dropna(subset=["waist_cm"])["waist_cm"].iloc[-1]) if len(b) and b["waist_cm"].notna().any() else None
        m_ = ml[ml["wk"] == n] if not ml.empty else ml
        w["kcal"] = float(m_["cal"].mean()) if len(m_) else None
        w["prot"] = float(m_["prot"].mean()) if len(m_) else None
        r_ = rc[rc["wk"] == n] if not rc.empty else rc
        w["sleep"] = float(r_["sleep"].replace(0, float("nan")).mean()) if len(r_) and r_["sleep"].replace(0, float("nan")).notna().any() else None
        w["energy"] = float(r_["energy"].mean()) if len(r_) and r_["energy"].notna().any() else None
        w["notes"] = [str(z) for z in (r_["notes"].dropna() if len(r_) else []) if str(z).strip()]
        if n in closed or (w["finished"] >= SESSIONS_PER_WEEK):
            w["status"] = "Completada"
        elif w0 > today_:
            w["status"] = "Pendiente"
        else:
            w["status"] = "En curso"
        w["closed"] = n in closed
        weeks.append(w)
    return weeks


def view_block(df_all, u):
    start = program_start()
    weeks = build_weeks(df_all, sessions_df(ver()), meals_daily_df(ver()), body_df(ver()),
                        recovery_df(ver()), closed_weeks(ver()), start, u)
    cur_wk = week_number(today())
    cards = []
    for w in weeks:
        kind = {"Completada": "ok", "En curso": "run", "Pendiente": "pend"}[w["status"]]
        grp = ", ".join(f"{k} {v}" for k, v in list(w["by_group"].items())[:4]) or "—"
        lifts = ", ".join(f"{k.split(' ')[0]} {fmt_w(v)}" for k, v in sorted(w["top"].items(), key=lambda kv: -kv[1])[:2]) or "—"
        notes = ("; ".join(w["notes"])[:80]) if w["notes"] else "—"
        w_end = w["start"] + timedelta(days=6)
        cards.append(
            f'<div class="gx-week"><div class="gx-row"><span class="wk">Semana {w["n"]}</span>{badge(w["status"], kind)}</div>'
            f'<div class="gx-muted">sáb {esc(fecha_corta(w["start"]))} – vie {esc(fecha_corta(w_end))}'
            f'{" · actual" if w["n"] == cur_wk else ""}</div>'
            f'Sesiones <b>{w["finished"]}/{SESSIONS_PER_WEEK}</b> · Adh. <b>{w["adherence"]:.0f}%</b><br>'
            f'Series: <b>{w["sets"]}</b> ({esc(grp)})<br>Cargas: {esc(lifts)} {u}<br>'
            f'Peso: <b>{fmt_n(w["bw"], 1) if w["bw"] else "—"}</b> kg · Cintura: <b>{fmt_n(w["waist"], 1) if w["waist"] else "—"}</b> cm<br>'
            f'Kcal prom.: <b>{fmt_n(w["kcal"]) if w["kcal"] else "—"}</b> · Prot.: <b>{fmt_n(w["prot"]) if w["prot"] else "—"}</b> g<br>'
            f'Sueño: {fmt_n(w["sleep"], 1) if w["sleep"] else "—"} h · Energía: {fmt_n(w["energy"], 1) if w["energy"] else "—"}/5<br>'
            f'Obs.: {esc(notes)}</div>')
    H('<div class="gx-grid2">' + "".join(cards) + "</div>")
    with st.expander("Cerrar / reabrir una semana"):
        wk_sel = st.selectbox("Semana", list(range(1, WEEKS + 1)), key="wk_close_sel")
        is_closed = weeks[wk_sel - 1]["closed"]
        st.button("Reabrir semana" if is_closed else "Marcar como completada", key="wk_close_btn", on_click=cb_week,
                  args=(wk_sel, not is_closed), **btn_w("lock_open" if is_closed else "task_alt"))
        st.caption("Una semana solo figura como completada cuando terminas sus 6 sesiones o la cierras tú.")

    H('<div class="gx-sec">Gráficas semanales</div>')
    metrics = ["Press de banca", "Sentadilla", "Remo en polea baja", "Curl femoral", "Peso corporal", "Cintura",
               "Series por grupo muscular", "Adherencia"]
    m = st.selectbox("Métrica", metrics, key="blk_metric")
    xs = [pd.Timestamp(w["start"]) for w in weeks]

    def label(t):
        n = (t - pd.Timestamp(start)).days // 7 + 1
        return f"S{n}<br>{fecha_corta(t)}"

    def line_chart(series, ytitle, key, truncated=True):
        fig = go.Figure()
        allv = []
        palette = [C_BLUE, C_ORANGE]
        for i, (nm, ys) in enumerate(series):
            allv += [v for v in ys if v is not None]
            fig.add_trace(go.Scatter(x=xs, y=ys, name=nm, mode="lines+markers", connectgaps=False,
                                     line=dict(color=palette[i % 2], width=3), marker=dict(size=9),
                                     hovertemplate="<b>%{x|%d/%m/%Y}</b><br>%{y:.1f}<extra>" + nm + "</extra>"))
        if not allv:
            st.info("Aún no hay datos para esta métrica en el bloque.")
            return
        base_layout(fig, 280, legend=len(series) > 1)
        rng = nice_range(allv)
        fig.update_yaxes(range=rng, title_text=ytitle + (" · eje truncado" if rng[0] > 0 else ""))
        style_time_axis(fig, xs, label)
        fig.update_xaxes(range=[xs[0] - pd.Timedelta(days=3), xs[-1] + pd.Timedelta(days=3)])
        show_chart(fig, key)
        if rng[0] > 0:
            st.caption("El eje vertical no empieza en cero. Toca los puntos para ver los valores exactos.")

    def top_series(names):
        out = []
        for nm in names:
            out.append((nm, [w["top"].get(nm) for w in weeks]))
        return out
    if m == "Press de banca":
        line_chart(top_series(["Press de banca con barra"]), f"Carga máx. ({u})", "wk_bench")
    elif m == "Sentadilla":
        line_chart(top_series(["Sentadilla con barra"]), f"Carga máx. ({u})", "wk_squat")
    elif m == "Remo en polea baja":
        line_chart(top_series(["Remo en polea baja"]), f"Carga máx. ({u})", "wk_row")
    elif m == "Curl femoral":
        line_chart(top_series(["Curl femoral sentado", "Curl femoral tumbado"]), f"Carga máx. ({u})", "wk_curl")
    elif m == "Peso corporal":
        line_chart([("Peso (kg)", [w["bw"] for w in weeks])], "Peso (kg)", "wk_bw")
    elif m == "Cintura":
        line_chart([("Cintura (cm)", [w["waist"] for w in weeks])], "Cintura (cm)", "wk_waist")
    elif m == "Series por grupo muscular":
        groups = sorted({g for w in weeks for g in w["by_group"]})
        if not groups:
            st.info("Aún no hay series registradas en el bloque.")
        else:
            fig = go.Figure()
            pal = [C_BLUE, C_CORAL, C_ORANGE, "#8B93A7", "#5B8DEF", "#E86A92", "#F2C14E", "#6FCF97", "#9B8CFF", "#4FD1C5"]
            for i, g in enumerate(groups):
                fig.add_trace(go.Bar(x=xs, y=[w["by_group"].get(g, 0) for w in weeks], name=g, marker_color=pal[i % len(pal)],
                                     hovertemplate="<b>%{x|%d/%m/%Y}</b><br>%{y} series<extra>" + g + "</extra>"))
            fig.update_layout(barmode="stack")
            base_layout(fig, 300, legend=True)
            fig.update_yaxes(title_text="Series completadas", rangemode="tozero")
            style_time_axis(fig, xs, label)
            show_chart(fig, "wk_groups")
    else:
        fig = go.Figure(go.Bar(x=xs, y=[w["adherence"] for w in weeks], marker_color=[C_GREEN if w["adherence"] >= 100 else C_BLUE for w in weeks],
                               text=[f"{w['adherence']:.0f}%" for w in weeks], textposition="outside",
                               hovertemplate="<b>%{x|%d/%m/%Y}</b><br>Adherencia: %{y:.0f}%<extra></extra>"))
        base_layout(fig, 260)
        fig.update_yaxes(range=[0, 115], title_text="Adherencia (%)")
        style_time_axis(fig, xs, label)
        show_chart(fig, "wk_adh")


def view_body():
    d = today()
    H('<div class="gx-sec" style="margin-top:6px">Registrar medidas</div>')
    a, b, c = st.columns(3)
    init_state("bd_date", d)
    a.date_input("Fecha", key="bd_date", max_value=d)
    b.number_input("Peso (kg)", 0.0, 300.0, 0.0, 0.1, key="bd_w", format="%g")
    c.number_input("Cintura (cm)", 0.0, 250.0, 0.0, 0.5, key="bd_waist", format="%g")
    st.button("Guardar medidas", key="bd_save", on_click=cb_save_body, **btn_w("save"))
    st.caption("Deja en 0 el dato que no quieras registrar; no se sobrescribe lo ya guardado.")
    bd = body_df(ver())
    if bd.empty:
        st.info("Sin registros todavía.")
        return
    for col_, title, color, key in (("weight_kg", "Peso corporal (kg)", C_BLUE, "bd_ch_w"), ("waist_cm", "Cintura (cm)", C_BLUE, "bd_ch_c")):
        s = bd.dropna(subset=[col_])
        if s.empty:
            continue
        fig = go.Figure(go.Scatter(x=s["date"], y=s[col_], mode="lines+markers", name="Registro", connectgaps=False,
                                   line=dict(color=color, width=2, shape="linear"), marker=dict(size=8),
                                   hovertemplate="<b>%{x|%d/%m/%Y}</b><br>%{y:.1f}<extra></extra>"))
        if len(s) >= 3:
            ma = s.set_index("date")[col_].rolling("7D").mean()
            fig.add_trace(go.Scatter(x=ma.index, y=ma.values, mode="lines", name="Prom. móvil 7 días",
                                     line=dict(color=C_ORANGE, width=2, dash="dot"),
                                     hovertemplate="<b>%{x|%d/%m/%Y}</b><br>Prom.: %{y:.1f}<extra></extra>"))
        rng = nice_range(s[col_].tolist())
        base_layout(fig, 250, legend=True)
        fig.update_yaxes(range=rng, title_text=title + (" · eje truncado" if rng[0] > 0 else ""))
        style_time_axis(fig, s["date"])
        show_chart(fig, key)
    st.caption("El promedio móvil usa solo tus registros reales (no se inventan días intermedios).")
    rc = recovery_df(ver())
    rc = rc.dropna(subset=["sleep"]) if not rc.empty else rc
    rc = rc[rc["sleep"] > 0] if not rc.empty else rc
    if not rc.empty:
        fig = go.Figure(go.Bar(x=rc["date"], y=rc["sleep"], marker_color=C_BLUE,
                               hovertemplate="<b>%{x|%d/%m/%Y}</b><br>%{y:.1f} h<extra></extra>"))
        base_layout(fig, 220)
        fig.update_yaxes(range=[0, max(10, rc["sleep"].max() * 1.1)], title_text="Sueño (h)")
        style_time_axis(fig, rc["date"])
        show_chart(fig, "bd_sleep")


def view_nutrition():
    md = meals_daily_df(ver())
    if md.empty:
        st.info("Aún no hay comidas registradas.")
        return
    T = get_targets(today())
    cutoff = pd.Timestamp(today()) - pd.Timedelta(days=13)
    m = md[md["date"] >= cutoff]
    fig = go.Figure(go.Bar(x=m["date"], y=m["cal"], marker_color=[C_CORAL if v > T["cal"] else C_ORANGE for v in m["cal"]],
                           text=[f"{v:.0f}" for v in m["cal"]], textposition="outside",
                           hovertemplate="<b>%{x|%d/%m/%Y}</b><br>%{y:.0f} kcal<extra></extra>"))
    fig.add_hline(y=T["cal"], line_dash="dash", line_color="#FFFFFF", annotation_text="Meta", annotation_position="top left")
    base_layout(fig, 270)
    fig.update_yaxes(range=[0, max(m["cal"].max(), T["cal"]) * 1.2], title_text="kcal")
    style_time_axis(fig, m["date"])
    show_chart(fig, "nu_cal")
    st.caption("Coral = día por encima de la meta calórica. Eje desde cero.")
    fig = go.Figure(go.Scatter(x=m["date"], y=m["prot"], mode="lines+markers", line=dict(color=C_ORANGE, width=3, shape="linear"),
                               marker=dict(size=8), connectgaps=False,
                               hovertemplate="<b>%{x|%d/%m/%Y}</b><br>%{y:.0f} g<extra></extra>"))
    fig.add_hline(y=T["prot"], line_dash="dash", line_color="#FFFFFF", annotation_text="Meta", annotation_position="top left")
    base_layout(fig, 230)
    rng = nice_range(m["prot"].tolist() + [T["prot"]])
    fig.update_yaxes(range=rng, title_text="Proteína (g)" + (" · eje truncado" if rng[0] > 0 else ""))
    style_time_axis(fig, m["date"])
    show_chart(fig, "nu_prot")
    wk = md.copy()
    # Semanas de sábado a viernes (W-FRI = la semana termina en viernes)
    wk["sem"] = wk["date"].dt.to_period("W-FRI").apply(lambda p: p.start_time)
    agg = wk.groupby("sem").agg(Kcal=("cal", "mean"), Proteina=("prot", "mean"), Carbs=("carbs", "mean"), Grasas=("fat", "mean"),
                                Dias=("cal", "size")).reset_index().sort_values("sem", ascending=False).head(8)
    agg["Semana"] = agg["sem"].map(lambda t: "sáb " + fecha_corta(t.date()))
    st.dataframe(agg[["Semana", "Dias", "Kcal", "Proteina", "Carbs", "Grasas"]].round(0), hide_index=True, **df_w())


BACKUP_TABLES = {  # nombre en el respaldo -> (tabla, orden)
    "series": ("sets_log", "date, session_id, exercise, set_no"),
    "sesiones": ("sessions", "date"),
    "comidas": ("meals", "date, id"),
    "metas_y_sueno": ("daily_metrics", "date"),
    "recuperacion": ("recovery_log", "date"),
    "medidas_corporales": ("body_metrics", "date"),
    "progresion": ("progression_log", "id"),
    "entrenamientos_antiguos": ("workouts", "date, id"),
    "ajustes": ("settings", "key"),
    "cargas_confirmadas": ("load_targets", "exercise"),
    "config_ejercicios": ("exercise_config", "exercise"),
    "programa": ("programs", "id"),
    "semanas_bloque": ("program_weeks", "week_no"),
}


def table_columns(table):
    return [r[1] for r in q(f"PRAGMA table_info({table})")]


def restore_backup(bundle):
    """Inserta (o reemplaza) las filas de un respaldo JSON. Devuelve {tabla: filas}."""
    done = {}
    for name, (table, _) in BACKUP_TABLES.items():
        rows = bundle.get(name) or []
        if not rows:
            continue
        cols = set(table_columns(table))
        n = 0
        for r in rows:
            keys = [k for k in r if k in cols]
            if not keys:
                continue
            vals = [r[k] for k in keys]
            x(f"INSERT OR REPLACE INTO {table} ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})", vals)
            n += 1
        done[name] = n
    for k in ("_settings", "_pstart", "_excfg", "_targets"):
        st.session_state.pop(k, None)
    bump(True)
    return done


def view_export():
    mode = db().mode
    if mode != "turso":
        st.warning("Tus datos se guardan en un archivo temporal del servidor. Streamlit Cloud lo borra cuando la app "
                   "se reinicia o se duerme por inactividad. Descarga un respaldo y configura Turso para que sean permanentes.")
    H('<div class="gx-sec" style="margin-top:6px">Respaldo</div>')
    if st.toggle("Preparar archivos de exportación", key="prep_export"):
        bundle = {}
        for name, (table, order) in BACKUP_TABLES.items():
            try:
                df = qdf(f"SELECT * FROM {table} ORDER BY {order}")
            except Exception:
                continue
            bundle[name] = json.loads(df.to_json(orient="records", force_ascii=False))
            if name in ("series", "sesiones", "comidas", "medidas_corporales"):
                st.download_button(f"{name}.csv · {len(df)} filas", df.to_csv(index=False).encode("utf-8-sig"),
                                   file_name=f"gains_{name}.csv", mime="text/csv", key=f"dl_{name}", **dl_w("download"))
        st.download_button("Respaldo completo (JSON)", json.dumps(bundle, ensure_ascii=False, indent=1).encode("utf-8"),
                           file_name=f"gains_backup_{today()}.json", mime="application/json", key="dl_json",
                           type="primary", **dl_w("download"))
        st.caption("El JSON incluye todo: series, sesiones, comidas, metas, medidas, ajustes y cargas confirmadas.")
    else:
        st.caption("Activa el interruptor para generar los archivos.")

    H('<div class="gx-sec">Restaurar respaldo</div>')
    st.caption("Sube un archivo gains_backup_….json para recuperar tus datos. Los registros con el mismo "
               "identificador se reemplazan; no se duplican.")
    up = st.file_uploader("Archivo de respaldo", type=["json"], key="restore_file", label_visibility="collapsed")
    if up is not None:
        try:
            bundle = json.loads(up.getvalue().decode("utf-8"))
            counts = {k: len(v or []) for k, v in bundle.items() if k in BACKUP_TABLES}
        except Exception:
            st.error("El archivo no es un respaldo válido de Gains OS.")
            return
        st.caption("Contenido: " + ", ".join(f"{k} {v}" for k, v in counts.items() if v) if any(counts.values())
                   else "El respaldo está vacío.")
        if any(counts.values()) and st.button("Restaurar datos", type="primary", key="restore_btn", **btn_w("restore")):
            try:
                res = restore_backup(bundle)
                st.success("Datos restaurados: " + ", ".join(f"{k} {v}" for k, v in res.items()))
            except Exception as e:
                st.error(f"No se pudo restaurar: {e}")


def view_progress():
    u = unit()
    t1, t2, t3, t4, t5, t6 = st.tabs(["Cargas", "Sesiones", "Bloque", "Cuerpo", "Nutrición", "Datos"])
    df_all = all_sets_df(ver())
    with t1:
        view_loads(df_all, u)
    with t2:
        view_compare(df_all, u)
    with t3:
        view_block(df_all, u)
    with t4:
        view_body()
    with t5:
        view_nutrition()
    with t6:
        view_export()


# =============================================================================
# 17. VISTA: PLAN
# =============================================================================
def view_plan():
    d, u = today(), unit()
    T = get_targets(d)
    H('<div class="gx-sec" style="margin-top:6px">Metas nutricionales y sueño</div>')
    r = q1("SELECT sleep_hours FROM daily_metrics WHERE date=?", [str(d)])
    sleep_today = float(r[0]) if r and r[0] is not None else 0.0
    with st.form("goals_form"):
        a, b = st.columns(2)
        a.number_input("Meta calórica (kcal)", 1000, 6000, int(T["cal"]), 50, key="pl_cal")
        b.number_input("Proteína (g)", 30, 400, int(T["prot"]), 5, key="pl_prot")
        c, e = st.columns(2)
        c.number_input("Carbohidratos (g)", 20, 800, int(T["carbs"]), 10, key="pl_carbs")
        e.number_input("Grasas (g)", 10, 300, int(T["fat"]), 5, key="pl_fat")
        st.number_input("Horas de sueño hoy", 0.0, 16.0, sleep_today, 0.5, key="pl_sleep")
        st.form_submit_button("Guardar metas", type="primary", on_click=cb_save_goals, **fsb_w())
    st.caption("Las metas se heredan a los días siguientes hasta que las cambies.")

    H('<div class="gx-sec">Recuperación de hoy</div>')
    rr = q1("SELECT energy, soreness, notes FROM recovery_log WHERE user_id=? AND date=?", [UID, str(d)])
    with st.form("rec_form"):
        st.number_input("Sueño (h)", 0.0, 16.0, sleep_today, 0.5, key="rc_sleep")
        a, b = st.columns(2)
        a.slider("Energía (1–5)", 1, 5, int(rr[0]) if rr and rr[0] else 3, key="rc_energy")
        b.slider("Dolor muscular (1–5)", 1, 5, int(rr[1]) if rr and rr[1] else 3, key="rc_sore")
        st.text_input("Observaciones", rr[2] if rr and rr[2] else "", key="rc_notes")
        st.form_submit_button("Guardar recuperación", on_click=cb_save_recovery, **fsb_w())

    H('<div class="gx-sec">Bloque de entrenamiento</div>')
    start = program_start()
    st.date_input("Inicio del bloque (se ajusta al sábado de esa semana)", start, key="pl_start")
    st.button("Guardar inicio del bloque", key="pl_start_btn", on_click=cb_save_program, **btn_w("event"))
    wk = week_number(d)
    st.caption(f"Hoy es la semana {wk} de {WEEKS} (semanas de sábado a viernes)." if 1 <= wk <= WEEKS else
               "El bloque aún no inicia." if wk < 1 else "El bloque de 8 semanas ya terminó.")
    sched = "".join(
        f'<div class="gx-list-row"><span style="font-size:13.5px;font-weight:600">{esc(weekday_name_for(k).capitalize())}</span>'
        f'<span class="gx-muted">{esc(k)}</span></div>' for k in DAY_KEYS)
    H(f'<div class="gx-card" style="margin-top:8px"><div class="gx-title">Calendario semanal</div>{sched}</div>')

    H('<div class="gx-sec">Preferencias</div>')
    init_state("pl_unit", u)
    st.selectbox("Unidad de peso", ["kg", "lb"], key="pl_unit", on_change=cb_unit_change)
    init_state("pl_name", settings().get("name", "Luis"))
    st.text_input("Tu nombre", key="pl_name", on_change=cb_pref, args=("name", "pl_name"))
    st.caption("Al cambiar de unidad, los registros guardados se convierten para mostrarse; los incrementos mínimos por ejercicio vuelven a sus valores por defecto.")
    mode = db().mode
    st.caption("Almacenamiento: Turso (persistente)." if mode == "turso" else
               "Almacenamiento local temporal: configura TURSO_DATABASE_URL y TURSO_AUTH_TOKEN para no perder datos.")


# =============================================================================
# 18. MAIN
# =============================================================================
def main():
    st.set_page_config(page_title="Gains OS", page_icon=page_icon(), layout="centered", initial_sidebar_state="collapsed")
    st.markdown(CSS, unsafe_allow_html=True)
    try:
        db()
    except Exception as e:
        st.error(f"No se pudo conectar a la base de datos: {e}")
        st.stop()
    # Versión de caché única por visita: al recargar la página no se reutilizan datos viejos.
    init_state("_ver", int(time.time() * 1000))
    init_state("_hver", int(time.time() * 1000))
    init_state("nav", NAV[0])
    if st.session_state.get("nav") not in NAV:
        st.session_state["nav"] = NAV[0]
    try:
        nav_box = st.container(key="topnav")
    except TypeError:
        nav_box = st.container()
    with nav_box:
        st.radio("Navegación", NAV, key="nav", horizontal=True, label_visibility="collapsed")
    flash_show()
    view = st.session_state["nav"]
    if view == NAV[0]:
        view_home()
    elif view == NAV[1]:
        view_food()
    elif view == NAV[2]:
        view_workout()
    elif view == NAV[3]:
        view_progress()
    else:
        view_plan()


if __name__ == "__main__":
    main()
