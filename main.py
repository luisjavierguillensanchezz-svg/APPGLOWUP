import os
import time
import json
from datetime import date, datetime
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import libsql_client
from google import genai
from pydantic import BaseModel

# --- 1. CONFIGURACIÓN Y CSS (Contraste forzado y UI limpia) ---
st.set_page_config(page_title="Gains OS", layout="centered", initial_sidebar_state="collapsed")

st.markdown("""
<style>
    /* Ocultar UI de Streamlit */
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}
    .block-container {padding-top: 1rem; padding-bottom: 5rem; max-width: 500px;}
    
    /* Forzar contraste total para evitar textos invisibles */
    html, body, [class*="st-"] {
        color: #FFFFFF !important;
    }
    p, span, label, div { color: #E0E0E0 !important; }
    h1, h2, h3, h4, h5, h6, strong, b { color: #FFFFFF !important; font-weight: 600 !important; }
    
    /* Inputs y Textareas */
    div[data-baseweb="input"] > div, 
    div[data-baseweb="textarea"] > textarea,
    div[data-baseweb="select"] > div {
        background-color: #252733 !important;
        border: 1px solid #3A3D4A !important;
        color: #FFFFFF !important;
        border-radius: 8px;
    }
    
    /* Pestañas estilo App Bottom Nav */
    .stTabs [data-baseweb="tab-list"] {
        display: flex; justify-content: space-between;
        background-color: #1A1C23; padding: 5px; border-radius: 12px;
        border: 1px solid #2D303E;
    }
    .stTabs [data-baseweb="tab"] {
        color: #888888 !important; border: none; padding: 10px; font-size: 13px;
    }
    .stTabs [aria-selected="true"] {
        color: #FF7B00 !important; font-weight: bold; background-color: #2D303E; border-radius: 8px;
    }
    
    /* Tarjetas UI */
    .card {
        background-color: #1A1C23; padding: 16px; border-radius: 12px;
        margin-bottom: 12px; border: 1px solid #2D303E; 
        box-shadow: 0 4px 6px rgba(0,0,0,0.2);
    }
</style>
""", unsafe_allow_html=True)

# --- 2. BASE DE DATOS ---
TURSO_URL = st.secrets.get("TURSO_DATABASE_URL", os.environ.get("TURSO_DATABASE_URL", ""))
TURSO_TOKEN = st.secrets.get("TURSO_AUTH_TOKEN", os.environ.get("TURSO_AUTH_TOKEN", ""))
GEMINI_KEY = st.secrets.get("GEMINI_API_KEY", os.environ.get("GEMINI_API_KEY", ""))

ai_client = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None

def get_db():
    if not TURSO_URL or not TURSO_TOKEN: return None
    url = TURSO_URL.replace("libsql://", "https://")
    return libsql_client.create_client_sync(url=url, auth_token=TURSO_TOKEN)

def ensure_tables():
    db = get_db()
    if not db: return
    try:
        db.batch([
            """CREATE TABLE IF NOT EXISTS meals (
                id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, description TEXT,
                calories REAL NOT NULL, protein_g REAL NOT NULL, carbs_g REAL NOT NULL, fat_g REAL NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS workouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, exercise TEXT NOT NULL,
                sets INTEGER NOT NULL, reps INTEGER NOT NULL, weight_kg REAL NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS daily_metrics (
                date TEXT PRIMARY KEY, sleep_hours REAL, target_calories REAL, target_protein_g REAL
            )"""
        ])
    except: pass
    finally: db.close()

ensure_tables()

# --- 3. MODELOS IA ---
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

def parse_meal(text: str) -> MealEstimate:
    prompt = f"Analiza para hipertrofia: '{text}'. Desglosa por ingredientes y da el total de macros."
    candidates = ['gemini-flash-latest', 'gemini-3.8-flash']
    last_err = None
    for model_name in candidates:
        for wait_time in [1, 2]:
            try:
                response = ai_client.models.generate_content(
                    model=model_name, contents=prompt,
                    config=dict(response_mime_type="application/json", response_schema=MealEstimate, temperature=0.1)
                )
                return MealEstimate.model_validate_json(response.text)
            except Exception as e:
                last_err = e
                if "503" in str(e): time.sleep(wait_time); continue
                break
    raise RuntimeError(f"Error IA: {last_err}")

# --- 4. PLAN DE ENTRENAMIENTO (PROMPT 8 SEMANAS) ---
ROUTINES = {
    "Día 1: UPPER": ["Press inclinado con mancuernas", "Remo con pecho apoyado", "Press de banca con barra", "Jalón al pecho agarre neutro", "Elevación lateral en polea", "Apertura inversa en máquina", "Curl de bíceps en polea", "Extensión de tríceps en polea"],
    "Día 2: LEGS A": ["Sentadilla con barra", "Prensa de piernas", "Curl femoral sentado", "Extensión de cuádriceps", "Elevación de pantorrillas de pie", "Crunch en polea"],
    "Día 3: PUSH": ["Press de banca con barra", "Press inclinado en máquina", "Press de hombros sentado", "Aperturas en polea", "Elevaciones laterales con mancuernas", "Extensión de tríceps sobre la cabeza", "Jalón de tríceps con cuerda"],
    "Día 4: PULL": ["Jalón al pecho agarre neutro", "Remo en polea baja", "Remo unilateral", "Pullover en polea", "Apertura inversa en máquina", "Curl predicador barra EZ", "Curl martillo"],
    "Día 5: LEGS B": ["Peso muerto rumano", "Hip thrust", "Curl femoral tumbado", "Sentadilla búlgara", "Extensión de cuádriceps", "Elevación pantorrillas sentado", "Elevación rodillas en silla romana"],
    "Día 6: RECUPERACIÓN": ["Bicicleta / Caminata (15 min)", "Elevaciones laterales máquina", "Aperturas en polea", "Pullover en polea", "Movilidad"],
    "Día 7: DESCANSO": ["Descanso total"]
}

# Obtener rutina sugerida basada en el día de la semana (0=Lunes, 6=Domingo)
weekday = datetime.today().weekday()
routine_keys = list(ROUTINES.keys())
suggested_routine = routine_keys[weekday]

# --- 5. DATOS DEL DÍA ---
today_str = str(date.today())
db = get_db()

target_cal, target_prot = 2700, 160
sleep_today = 0.0
consumed_cal, consumed_prot, consumed_carbs, consumed_fats = 0, 0, 0, 0

if db:
    met = db.execute("SELECT sleep_hours, target_calories, target_protein_g FROM daily_metrics WHERE date=?", [today_str]).rows
    if met: sleep_today, target_cal, target_prot = met[0]
    
    meals_today = db.execute("SELECT SUM(calories), SUM(protein_g), SUM(carbs_g), SUM(fat_g) FROM meals WHERE date=?", [today_str]).rows
    if meals_today and meals_today[0][0] is not None:
        consumed_cal, consumed_prot, consumed_carbs, consumed_fats = meals_today[0]

# --- 6. INTERFAZ ---
st.markdown("### Hola, Luis 👋")

tab1, tab2, tab3, tab4, tab5 = st.tabs(["🏠 Inicio", "🍗 Comida", "🏋️ Rutina", "📈 Progreso", "⚙️ Plan"])

# --- INICIO ---
with tab1:
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown("<h4 style='margin-top:0;'>Calorías de hoy</h4>", unsafe_allow_html=True)
    c1, c2 = st.columns([1, 1.2])
    
    with c1:
        rem = max(0, target_cal - consumed_cal)
        fig = go.Figure(data=[go.Pie(
            labels=['Consumido', 'Restante'], values=[consumed_cal, rem],
            hole=.82, marker_colors=['#FF7B00', '#252733'], textinfo='none', hoverinfo='none'
        )])
        fig.update_layout(
            showlegend=False, margin=dict(t=0, b=0, l=0, r=0), height=140,
            paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
            annotations=[dict(text=f"<b style='font-size:24px; color:#FFF'>{int(consumed_cal)}</b><br><span style='font-size:11px; color:#888'>/ {int(target_cal)} kcal</span>", x=0.5, y=0.5, showarrow=False)]
        )
        st.plotly_chart(fig, width="stretch", config={'displayModeBar': False})
        
    with c2:
        st.markdown("<br>", unsafe_allow_html=True)
        st.progress(min(consumed_prot/target_prot, 1.0) if target_prot else 0)
        st.caption(f"Proteína: {int(consumed_prot)}g / {int(target_prot)}g")
        st.progress(min(consumed_carbs/350, 1.0))
        st.caption(f"Carbos: {int(consumed_carbs)}g")
        st.progress(min(consumed_fats/70, 1.0))
        st.caption(f"Grasas: {int(consumed_fats)}g")
    st.markdown('</div>', unsafe_allow_html=True)

# --- COMIDA ---
with tab2:
    meal_text = st.text_area("Registrar Comida", placeholder="Ej. 150g pechuga asada y arroz...", height=100)
    if st.button("✨ Analizar con IA", type="primary", use_container_width=True):
        if meal_text and db:
            with st.spinner("Calculando macros..."):
                try:
                    data = parse_meal(meal_text)
                    for item in data.items:
                        st.markdown(f"""
                        <div class="card" style="display:flex; justify-content:space-between; padding: 12px;">
                            <div><b>{item.name}</b><br><span style='font-size:12px;color:#AAA;'>P: {item.protein_g}g | C: {item.carbs_g}g | G: {item.fat_g}g</span></div>
                            <div style="text-align:right;"><b style='color:#FF7B00'>{item.calories} kcal</b></div>
                        </div>
                        """, unsafe_allow_html=True)
                    
                    desc = ", ".join([i.name for i in data.items])
                    db.execute("INSERT INTO meals (date, description, calories, protein_g, carbs_g, fat_g) VALUES (?, ?, ?, ?, ?, ?)",
                               [today_str, desc, data.total_calories, data.total_protein_g, data.total_carbs_g, data.total_fat_g])
                    st.success(f"Guardado: {data.total_calories} kcal totales.")
                except Exception as e:
                    st.error(str(e))

# --- RUTINA ---
with tab3:
    selected_routine = st.selectbox("Rutina de Hoy", routine_keys, index=routine_keys.index(suggested_routine))
    st.markdown("---")
    
    if "DESCANSO" in selected_routine:
        st.info("Hoy es día de recuperación total. ¡Descansa y crece! 💪")
    else:
        for ex in ROUTINES[selected_routine]:
            with st.expander(f"🏋️ {ex}"):
                c_s, c_r, c_w = st.columns(3)
                sets = c_s.number_input("Series", 1, 10, 3, key=f"s_{ex}")
                reps = c_r.number_input("Reps", 1, 50, 8, key=f"r_{ex}")
                weight = c_w.number_input("Kg / Lb", 0.0, 300.0, 20.0, step=2.5, key=f"w_{ex}")
                
                if st.button("Registrar Serie", key=f"btn_{ex}", type="secondary", use_container_width=True):
                    if db:
                        db.execute("INSERT INTO workouts (date, exercise, sets, reps, weight_kg) VALUES (?, ?, ?, ?, ?)", 
                                   [today_str, ex, sets, reps, weight])
                        st.success("Guardado")

    st.markdown("### Historial de hoy")
    if db:
        hist = db.execute("SELECT exercise, sets, reps, weight_kg FROM workouts WHERE date=?", [today_str]).rows
        for h in hist:
            st.markdown(f'<div class="card" style="padding:10px;"><b>{h[0]}</b><br><span style="color:#AAA;">{h[1]} series x {h[2]} reps @ {h[3]}</span></div>', unsafe_allow_html=True)

# --- PROGRESO ---
with tab4:
    if db:
        st.markdown("#### 💪 Evolución de Cargas")
        workouts_res = db.execute("SELECT date, exercise, MAX(weight_kg) as top_weight FROM workouts GROUP BY date, exercise ORDER BY date DESC LIMIT 100").rows
        if workouts_res:
            df_w = pd.DataFrame(workouts_res, columns=["date", "exercise", "top_weight"])
            sel_ex = st.selectbox("Selecciona ejercicio:", df_w["exercise"].unique())
            df_filt = df_w[df_w["exercise"] == sel_ex].sort_values("date")
            
            fig_str = px.line(df_filt, x="date", y="top_weight", markers=True)
            fig_str.update_traces(line_color="#FF7B00", line_shape="spline", marker=dict(size=8, color="#FFFFFF"))
            fig_str.update_layout(
                plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
                xaxis=dict(showgrid=False, title="", color="#AAA"), yaxis=dict(showgrid=False, title="Kg", color="#AAA"),
                margin=dict(l=0, r=0, t=10, b=0)
            )
            st.plotly_chart(fig_str, width="stretch", config={'displayModeBar': False})
            
        st.markdown("#### 🍗 Historial Calórico")
        meals_res = db.execute("SELECT date, SUM(calories) as cal FROM meals GROUP BY date ORDER BY date DESC LIMIT 7").rows
        if meals_res:
            df_m = pd.DataFrame(meals_res, columns=["date", "cal"]).sort_values("date")
            fig_bar = px.bar(df_m, x="date", y="cal", text_auto='.0f')
            fig_bar.update_traces(marker_color="#FF7B00", textposition="outside", textfont_color="#FFF")
            fig_bar.update_layout(
                plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
                xaxis=dict(showgrid=False, title="", color="#AAA"), yaxis=dict(showgrid=False, visible=False),
                margin=dict(l=0, r=0, t=10, b=0)
            )
            st.plotly_chart(fig_bar, width="stretch", config={'displayModeBar': False})

# --- PLAN ---
with tab5:
    st.markdown("#### ⚙️ Mis Metas")
    with st.form("plan_form"):
        new_cal = st.number_input("Meta Calórica", 1500, 5000, int(target_cal), 50)
        new_prot = st.number_input("Meta Proteína (g)", 50, 350, int(target_prot), 5)
        new_sleep = st.number_input("Horas de sueño hoy", 0.0, 16.0, float(sleep_today), 0.5)
        
        if st.form_submit_button("Guardar Ajustes", use_container_width=True):
            if db:
                db.execute(
                    "INSERT INTO daily_metrics (date, sleep_hours, target_calories, target_protein_g) VALUES (?, ?, ?, ?) ON CONFLICT(date) DO UPDATE SET sleep_hours=excluded.sleep_hours, target_calories=excluded.target_calories, target_protein_g=excluded.target_protein_g",
                    [today_str, new_sleep, new_cal, new_prot]
                )
                st.success("Plan actualizado. Reiniciando...")
                time.sleep(1)
                st.rerun()
