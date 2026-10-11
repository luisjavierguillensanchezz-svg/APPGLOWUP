import os
import time
import json
from datetime import date, datetime
import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import libsql_client
from google import genai
from pydantic import BaseModel

# --- CONFIGURACIÓN Y UX/UI MÓVIL ---
st.set_page_config(page_title="Gains OS - 8 Semanas", layout="centered", initial_sidebar_state="collapsed")

# Paleta: Fondo Carbón (#12141C), Tarjetas (#1A1C23), Texto (#E0E0E0), Acción/Coral (#FF5A5F), Datos/Azul (#00A6ED), Nutrición/Naranja (#FF7B00)
st.markdown("""
<style>
    #MainMenu, footer, header {visibility: hidden;}
    .block-container {padding-top: 1rem; padding-bottom: 5rem; max-width: 600px;}
    
    html, body, [class*="st-"] { color: #E0E0E0 !important; }
    h1, h2, h3, h4 { color: #FFFFFF !important; font-weight: 600 !important; }
    
    /* Inputs */
    div[data-baseweb="input"] > div, div[data-baseweb="select"] > div, div[data-baseweb="textarea"] > textarea {
        background-color: #252733 !important; border: 1px solid #3A3D4A !important; border-radius: 8px;
    }
    
    /* Pestañas (Bottom Nav simulado) */
    .stTabs [data-baseweb="tab-list"] {
        display: flex; justify-content: space-between; background-color: #1A1C23; padding: 5px; border-radius: 12px; border: 1px solid #2D303E;
    }
    .stTabs [data-baseweb="tab"] { color: #888888 !important; border: none; padding: 10px; font-size: 13px; }
    .stTabs [aria-selected="true"] { color: #FF5A5F !important; font-weight: bold; background-color: #2D303E; border-radius: 8px; }
    
    /* Tarjetas UI */
    .card { background-color: #1A1C23; padding: 16px; border-radius: 12px; margin-bottom: 12px; border: 1px solid #2D303E; }
    .set-row { display: flex; justify-content: space-between; align-items: center; padding: 8px; border-bottom: 1px solid #2D303E; font-size: 14px; }
    
    /* Botones primarios Coral */
    div[data-testid="stButton"] button[kind="primary"] { background-color: #FF5A5F !important; color: white !important; border: none; border-radius: 8px; }
</style>
""", unsafe_allow_html=True)

# --- BASE DE DATOS Y CONEXIÓN ---
TURSO_URL = st.secrets.get("TURSO_DATABASE_URL", os.environ.get("TURSO_DATABASE_URL", ""))
TURSO_TOKEN = st.secrets.get("TURSO_AUTH_TOKEN", os.environ.get("TURSO_AUTH_TOKEN", ""))
GEMINI_KEY = st.secrets.get("GEMINI_API_KEY", os.environ.get("GEMINI_API_KEY", ""))

ai_client = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None

def get_db():
    if not TURSO_URL or not TURSO_TOKEN: return None
    return libsql_client.create_client_sync(url=TURSO_URL.replace("libsql://", "https://"), auth_token=TURSO_TOKEN)

def ensure_advanced_schema():
    db = get_db()
    if not db: return
    try:
        db.batch([
            # Tablas Legacy (Se mantienen)
            "CREATE TABLE IF NOT EXISTS meals (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, description TEXT, calories REAL NOT NULL, protein_g REAL NOT NULL, carbs_g REAL NOT NULL, fat_g REAL NOT NULL)",
            "CREATE TABLE IF NOT EXISTS daily_metrics (date TEXT PRIMARY KEY, sleep_hours REAL, target_calories REAL, target_protein_g REAL)",
            
            # Nuevo Esquema Avanzado
            "CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER, weight REAL, height REAL, level TEXT, objective TEXT)",
            "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, date TEXT, routine_name TEXT, week INTEGER, status TEXT, duration_mins INTEGER)",
            "CREATE TABLE IF NOT EXISTS set_logs (id TEXT PRIMARY KEY, session_id TEXT, exercise TEXT, set_number INTEGER, weight_kg REAL, reps INTEGER, rir INTEGER, type TEXT, status TEXT)",
            "CREATE TABLE IF NOT EXISTS body_metrics (date TEXT PRIMARY KEY, weight REAL, waist REAL, notes TEXT)"
        ])
        
        # Insertar perfil por defecto si está vacío
        res = db.execute("SELECT COUNT(*) FROM users")
        if res.rows[0][0] == 0:
            db.execute("INSERT INTO users (id, name, age, weight, height, level, objective) VALUES (1, 'Luis Javier', 23, 75.0, 173.0, 'Intermedio', 'Recomposición')")
    except Exception as e:
        st.error(f"Error DB: {e}")
    finally:
        db.close()

ensure_advanced_schema()

# --- RUTINAS (BLOQUE 8 SEMANAS) ---
ROUTINES = {
    "Día 1: UPPER": [
        {"name": "Press inclinado mancuernas", "sets": 3, "rep_range": "6-10", "rir": 2, "rest": 180},
        {"name": "Remo pecho apoyado", "sets": 3, "rep_range": "6-10", "rir": 2, "rest": 180},
        {"name": "Press de banca barra", "sets": 3, "rep_range": "6-8", "rir": 2, "rest": 180},
        {"name": "Jalón pecho neutro", "sets": 3, "rep_range": "8-12", "rir": 2, "rest": 120},
        {"name": "Elevación lateral polea", "sets": 3, "rep_range": "12-20", "rir": 1, "rest": 90},
        {"name": "Apertura inversa máquina", "sets": 2, "rep_range": "12-20", "rir": 1, "rest": 90},
        {"name": "Curl bíceps polea", "sets": 2, "rep_range": "10-15", "rir": 1, "rest": 90},
        {"name": "Extensión tríceps polea", "sets": 2, "rep_range": "10-15", "rir": 1, "rest": 90}
    ],
    "Día 2: LEGS A": [
        {"name": "Sentadilla con barra", "sets": 3, "rep_range": "5-8", "rir": 2, "rest": 180},
        {"name": "Prensa de piernas", "sets": 3, "rep_range": "8-12", "rir": 2, "rest": 180},
        {"name": "Curl femoral sentado", "sets": 3, "rep_range": "8-12", "rir": 1, "rest": 120},
        {"name": "Extensión cuádriceps", "sets": 2, "rep_range": "12-15", "rir": 1, "rest": 90},
        {"name": "Elevación pantorrillas de pie", "sets": 3, "rep_range": "8-12", "rir": 1, "rest": 90},
        {"name": "Crunch polea", "sets": 3, "rep_range": "10-15", "rir": 1, "rest": 90}
    ],
    "Día 3: PUSH": [
        {"name": "Press banca barra", "sets": 3, "rep_range": "6-8", "rir": 2, "rest": 180},
        {"name": "Press inclinado máquina", "sets": 3, "rep_range": "8-12", "rir": 2, "rest": 120},
        {"name": "Press hombros sentado", "sets": 2, "rep_range": "8-12", "rir": 2, "rest": 120},
        {"name": "Aperturas polea", "sets": 2, "rep_range": "12-15", "rir": 1, "rest": 90},
        {"name": "Elevaciones laterales mancuernas", "sets": 3, "rep_range": "12-20", "rir": 1, "rest": 90},
        {"name": "Extensión tríceps sobre cabeza", "sets": 3, "rep_range": "10-15", "rir": 1, "rest": 90},
        {"name": "Jalón tríceps cuerda", "sets": 2, "rep_range": "10-15", "rir": 1, "rest": 90}
    ],
    "Día 4: PULL": [{"name": "Jalón pecho neutro", "sets": 3, "rep_range": "6-10", "rir": 2, "rest": 180}, {"name": "Remo polea baja", "sets": 3, "rep_range": "8-12", "rir": 2, "rest": 120}, {"name": "Remo unilateral", "sets": 2, "rep_range": "10-15", "rir": 1, "rest": 90}, {"name": "Pullover polea", "sets": 2, "rep_range": "12-15", "rir": 1, "rest": 90}, {"name": "Apertura inversa", "sets": 3, "rep_range": "12-20", "rir": 1, "rest": 90}, {"name": "Curl predicador", "sets": 3, "rep_range": "8-12", "rir": 1, "rest": 90}, {"name": "Curl martillo", "sets": 2, "rep_range": "10-15", "rir": 1, "rest": 90}],
    "Día 5: LEGS B": [{"name": "Peso muerto rumano", "sets": 3, "rep_range": "6-10", "rir": 2, "rest": 180}, {"name": "Hip thrust", "sets": 3, "rep_range": "8-12", "rir": 2, "rest": 180}, {"name": "Curl femoral tumbado", "sets": 3, "rep_range": "10-15", "rir": 1, "rest": 120}, {"name": "Sentadilla búlgara", "sets": 2, "rep_range": "8-12", "rir": 1, "rest": 120}, {"name": "Extensión cuádriceps", "sets": 2, "rep_range": "12-15", "rir": 1, "rest": 90}, {"name": "Elevación pantorrillas", "sets": 3, "rep_range": "12-20", "rir": 1, "rest": 90}, {"name": "Elevación rodillas", "sets": 2, "rep_range": "10-15", "rir": 1, "rest": 90}],
    "Día 6: RECUPERACIÓN": [{"name": "Bicicleta suave", "sets": 1, "rep_range": "15 min", "rir": 5, "rest": 0}, {"name": "Elevaciones laterales ligeras", "sets": 2, "rep_range": "15-25", "rir": 4, "rest": 60}, {"name": "Movilidad", "sets": 1, "rep_range": "8 min", "rir": 0, "rest": 0}],
    "Día 7: DESCANSO": []
}

# --- ESTADO GLOBAL ---
today_str = str(date.today())
weekday = datetime.today().weekday()
routine_keys = list(ROUTINES.keys())
suggested_routine = routine_keys[weekday]
current_session_id = f"SESS_{today_str}_{suggested_routine[:5].strip()}"

db = get_db()
if db:
    # Metadatos del usuario
    user_data = db.execute("SELECT name, weight, level FROM users WHERE id=1").rows[0]
    
    # Calorías y macros
    met = db.execute("SELECT sleep_hours, target_calories, target_protein_g FROM daily_metrics WHERE date=?", [today_str]).rows
    sleep_today, target_cal, target_prot = met[0] if met else (0.0, 2700, 160)
    
    meals_today = db.execute("SELECT SUM(calories), SUM(protein_g), SUM(carbs_g), SUM(fat_g) FROM meals WHERE date=?", [today_str]).rows
    consumed_cal, consumed_prot, consumed_carbs, consumed_fats = meals_today[0] if (meals_today and meals_today[0][0]) else (0, 0, 0, 0)
    
    # Estado de la sesión de hoy
    db.execute("INSERT OR IGNORE INTO sessions (id, date, routine_name, week, status) VALUES (?, ?, ?, 1, 'Pendiente')", [current_session_id, today_str, suggested_routine])

# --- CRONÓMETRO JAVASCRIPT ---
def render_rest_timer():
    components.html("""
    <div style="background:#1A1C23; padding:15px; border-radius:12px; border:1px solid #2D303E; color:#FFF; font-family:sans-serif; text-align:center;">
        <h4 style="margin:0; color:#00A6ED;">Cronómetro de Descanso</h4>
        <div style="font-size:36px; font-weight:bold; margin:10px 0;" id="timer">00:00</div>
        <button onclick="startTimer(90)" style="background:#2D303E; color:#FFF; border:none; padding:8px 15px; border-radius:5px; margin:2px;">90s</button>
        <button onclick="startTimer(120)" style="background:#2D303E; color:#FFF; border:none; padding:8px 15px; border-radius:5px; margin:2px;">120s</button>
        <button onclick="startTimer(180)" style="background:#2D303E; color:#FFF; border:none; padding:8px 15px; border-radius:5px; margin:2px;">180s</button>
        <button onclick="stopTimer()" style="background:#FF5A5F; color:#FFF; border:none; padding:8px 15px; border-radius:5px; margin:2px;">Parar</button>
    </div>
    <script>
        let interval;
        function startTimer(seconds) {
            clearInterval(interval);
            let end = Date.now() + seconds * 1000;
            interval = setInterval(() => {
                let remaining = Math.round((end - Date.now()) / 1000);
                if (remaining <= 0) { clearInterval(interval); document.getElementById('timer').innerText = "¡LISTO!"; navigator.vibrate(500); }
                else {
                    let m = Math.floor(remaining / 60); let s = remaining % 60;
                    document.getElementById('timer').innerText = (m < 10 ? "0"+m : m) + ":" + (s < 10 ? "0"+s : s);
                }
            }, 1000);
        }
        function stopTimer() { clearInterval(interval); document.getElementById('timer').innerText = "00:00"; }
    </script>
    """, height=160)

# --- NAVEGACIÓN ---
tab1, tab2, tab3, tab4, tab5 = st.tabs(["🏠 Inicio", "🍗 Comida", "🏋️ Rutina", "📈 Progreso", "⚙️ Plan"])

# --- 1. INICIO ---
with tab1:
    st.markdown(f"### Hola, {user_data[0]} 👋")
    st.caption("Semana 1 de 8 • Bloque de Hipertrofia")
    
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown("<h4 style='margin-top:0;'>Nutrición de Hoy</h4>", unsafe_allow_html=True)
    c1, c2 = st.columns([1, 1.2])
    with c1:
        fig = go.Figure(data=[go.Pie(labels=['Consumido', 'Restante'], values=[consumed_cal, max(0, target_cal - consumed_cal)], hole=.8, marker_colors=['#FF7B00', '#252733'], textinfo='none', hoverinfo='none')])
        fig.update_layout(showlegend=False, margin=dict(t=0, b=0, l=0, r=0), height=140, paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', annotations=[dict(text=f"<b style='font-size:24px; color:#FFF'>{int(consumed_cal)}</b><br><span style='font-size:11px; color:#888'>/ {int(target_cal)} kcal</span>", x=0.5, y=0.5, showarrow=False)])
        st.plotly_chart(fig, use_container_width=True, config={'displayModeBar': False})
    with c2:
        st.markdown("<br>", unsafe_allow_html=True)
        st.progress(min(consumed_prot/target_prot, 1.0) if target_prot else 0)
        st.caption(f"<span style='color:#00A6ED'>Proteína:</span> {int(consumed_prot)}g / {int(target_prot)}g", unsafe_allow_html=True)
        st.progress(min(consumed_carbs/350, 1.0))
        st.caption(f"<span style='color:#E0E0E0'>Carbos:</span> {int(consumed_carbs)}g", unsafe_allow_html=True)
        st.progress(min(consumed_fats/70, 1.0))
        st.caption(f"<span style='color:#E0E0E0'>Grasas:</span> {int(consumed_fats)}g", unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)

    if db:
        sets_today = db.execute("SELECT COUNT(*) FROM set_logs WHERE session_id=? AND status='Completada'", [current_session_id]).rows[0][0]
        st.markdown(f'<div class="card"><b>Sesión del día:</b> {suggested_routine}<br><span style="color:#00A6ED">{sets_today} series registradas hoy</span></div>', unsafe_allow_html=True)

# --- 2. COMIDA (IA) ---
with tab2:
    st.markdown("#### Registro Inteligente")
    meal_text = st.text_area("¿Qué comiste?", placeholder="Ej. 150g pechuga asada, arroz...", height=100)
    if st.button("Analizar con IA", type="primary", use_container_width=True):
        if meal_text and db:
            with st.spinner("Procesando..."):
                try:
                    class MealEst(BaseModel): items: list[FoodItem]; total_calories: float; total_protein_g: float; total_carbs_g: float; total_fat_g: float
                    resp = ai_client.models.generate_content(model='gemini-flash-latest', contents=f"Desglosa para hipertrofia: '{meal_text}'", config=dict(response_mime_type="application/json", response_schema=MealEst, temperature=0.1))
                    data = MealEst.model_validate_json(resp.text)
                    for i in data.items:
                        st.markdown(f"<div class='card set-row'><div><b>{i.name}</b><br><span style='font-size:12px;color:#AAA;'>P:{i.protein_g}g | C:{i.carbs_g}g | G:{i.fat_g}g</span></div><div style='color:#FF7B00; font-weight:bold;'>{i.calories} kcal</div></div>", unsafe_allow_html=True)
                    db.execute("INSERT INTO meals (date, description, calories, protein_g, carbs_g, fat_g) VALUES (?, ?, ?, ?, ?, ?)", [today_str, ", ".join([x.name for x in data.items]), data.total_calories, data.total_protein_g, data.total_carbs_g, data.total_fat_g])
                    st.success("Guardado en tu historial.")
                except Exception as e: st.error(f"Error: {e}")

# --- 3. RUTINA Y REGISTRO SERIE POR SERIE ---
with tab3:
    st.selectbox("Rutina Activa", routine_keys, index=routine_keys.index(suggested_routine), key="active_rout")
    render_rest_timer()
    st.markdown("<br>", unsafe_allow_html=True)
    
    if "DESCANSO" in suggested_routine:
        st.info("Día de descanso total.")
    else:
        for ex in ROUTINES[suggested_routine]:
            with st.expander(f"🏋️ {ex['name']}"):
                st.markdown(f"<span style='color:#888; font-size:13px;'>Objetivo: {ex['sets']} series de {ex['rep_range']} reps @ RIR {ex['rir']} | Descanso: {ex['rest']}s</span>", unsafe_allow_html=True)
                
                # Obtener última carga de este ejercicio (Lógica de Progresión)
                last_perf = None
                if db:
                    l = db.execute("SELECT weight_kg, reps FROM set_logs WHERE exercise=? AND status='Completada' ORDER BY id DESC LIMIT 1", [ex['name']]).rows
                    if l: last_perf = l[0]
                
                if last_perf:
                    st.markdown(f"<div style='background:#252733; padding:8px; border-radius:6px; margin-bottom:10px; font-size:13px;'>Última sesión: <b>{last_perf[0]} kg × {last_perf[1]} reps</b></div>", unsafe_allow_html=True)
                
                # Formulario de registro por serie
                c_s, c_w, c_r, c_rir = st.columns([1, 1.5, 1.5, 1.5])
                set_num = c_s.number_input("Ser", 1, 10, 1, key=f"s_{ex['name']}")
                weight = c_w.number_input("Kg", 0.0, 300.0, float(last_perf[0]) if last_perf else 20.0, step=2.5, key=f"w_{ex['name']}")
                reps = c_r.number_input("Reps", 1, 50, 8, key=f"r_{ex['name']}")
                rir = c_rir.number_input("RIR", 0, 5, ex['rir'], key=f"rir_{ex['name']}")
                
                if st.button("Registrar Serie", type="primary", key=f"btn_{ex['name']}", use_container_width=True):
                    if db:
                        set_id = f"{current_session_id}_{ex['name']}_{set_num}_{int(time.time())}"
                        db.execute("INSERT INTO set_logs (id, session_id, exercise, set_number, weight_kg, reps, rir, type, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'Efectiva', 'Completada')", [set_id, current_session_id, ex['name'], set_num, weight, reps, rir])
                        st.success("✅ Guardado. Inicia tu descanso.")
                
                # Mostrar series ya registradas en esta sesión
                if db:
                    logs = db.execute("SELECT set_number, weight_kg, reps, rir FROM set_logs WHERE session_id=? AND exercise=? AND status='Completada' ORDER BY set_number", [current_session_id, ex['name']]).rows
                    if logs:
                        st.markdown("**Series Completadas Hoy:**")
                        for l in logs:
                            st.markdown(f"<div class='set-row'><span>Serie {l[0]}</span><span>{l[1]} kg</span><span>{l[2]} reps</span><span style='color:#00A6ED;'>RIR {l[3]}</span></div>", unsafe_allow_html=True)

# --- 4. PROGRESO Y GRÁFICAS DE 8 SEMANAS ---
with tab4:
    st.markdown("#### 📈 Historial Real")
    if db:
        # Gráfica de Fuerza
        workouts_res = db.execute("SELECT substr(id, 23, 10) as dt, exercise, MAX(weight_kg) as top_weight FROM set_logs WHERE status='Completada' GROUP BY dt, exercise ORDER BY dt DESC LIMIT 200").rows
        if workouts_res:
            df_w = pd.DataFrame(workouts_res, columns=["date", "exercise", "top_weight"])
            sel_ex = st.selectbox("Evolución (Carga Máxima):", df_w["exercise"].unique())
            df_filt = df_w[df_w["exercise"] == sel_ex].sort_values("date")
            
            fig_str = px.line(df_filt, x="date", y="top_weight", markers=True)
            fig_str.update_traces(line_color="#00A6ED", line_shape="spline", marker=dict(size=8, color="#FFFFFF"))
            fig_str.update_layout(plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)', xaxis=dict(showgrid=False, title="", color="#AAA"), yaxis=dict(showgrid=False, title="Kg", color="#AAA"), margin=dict(l=0, r=0, t=10, b=0))
            st.plotly_chart(fig_str, use_container_width=True, config={'displayModeBar': False})
        else:
            st.info("Registra tu primer entrenamiento para ver la evolución de cargas.")

# --- 5. PLAN Y PERFIL ---
with tab5:
    st.markdown("#### ⚙️ Configuración")
    with st.form("plan_form"):
        st.markdown("**Usuario**")
        n_weight = st.number_input("Peso Corporal (kg)", 40.0, 150.0, user_data[1])
        st.markdown("**Nutrición**")
        new_cal = st.number_input("Meta Calórica", 1500, 5000, int(target_cal), 50)
        new_prot = st.number_input("Meta Proteína (g)", 50, 350, int(target_prot), 5)
        st.markdown("**Recuperación**")
        new_sleep = st.number_input("Horas de sueño dormidas", 0.0, 16.0, float(sleep_today), 0.5)
        
        if st.form_submit_button("Guardar Cambios", use_container_width=True):
            if db:
                db.execute("UPDATE users SET weight=? WHERE id=1", [n_weight])
                db.execute("INSERT INTO daily_metrics (date, sleep_hours, target_calories, target_protein_g) VALUES (?, ?, ?, ?) ON CONFLICT(date) DO UPDATE SET sleep_hours=excluded.sleep_hours, target_calories=excluded.target_calories, target_protein_g=excluded.target_protein_g", [today_str, new_sleep, new_cal, new_prot])
                st.success("Guardado.")
