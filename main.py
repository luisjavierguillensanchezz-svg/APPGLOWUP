import os
import time
import json
from datetime import date
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import libsql_client
from google import genai
from pydantic import BaseModel

# --- CONFIGURACIÓN DE PÁGINA (Debe ir primero) ---
st.set_page_config(page_title="Gains OS", layout="centered", initial_sidebar_state="collapsed")

# --- CSS PARA SIMULAR APP MÓVIL OSCURA ---
st.markdown("""
<style>
    /* Tema general y ocultar elementos por defecto de Streamlit */
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}
    .block-container {padding-top: 1rem; padding-bottom: 5rem; max-width: 500px;}
    
    /* Estilo de las pestañas (simulando Bottom Nav) */
    .stTabs [data-baseweb="tab-list"] {
        display: flex; justify-content: space-between;
        background-color: #1E1E1E; padding: 10px; border-radius: 15px;
    }
    .stTabs [data-baseweb="tab"] {
        color: #888888; border: none; padding: 10px;
    }
    .stTabs [aria-selected="true"] {
        color: #FF7B00 !important; font-weight: bold;
    }
    
    /* Tarjetas personalizadas */
    div[data-testid="stMetricValue"] {font-size: 1.5rem; color: #FFF;}
    .card {
        background-color: #1A1C23; padding: 15px; border-radius: 15px;
        margin-bottom: 10px; border: 1px solid #2D303E;
    }
</style>
""", unsafe_allow_html=True)

# --- BASE DE DATOS Y CREDENCIALES ---
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

# --- IA Y MODELOS PYDANTIC ---
# Actualizado para devolver la lista de ingredientes como en tu imagen
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
    candidates = ['gemini-flash-latest', 'gemini-3.8-flash', 'gemini-2.5-flash']
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

# --- ESTADO Y DATOS DEL DÍA ---
today_str = str(date.today())
db = get_db()

# Cargar métricas del día (o valores por defecto)
target_cal, target_prot = 2400, 160
sleep_today = 0
if db:
    met = db.execute("SELECT sleep_hours, target_calories, target_protein_g FROM daily_metrics WHERE date=?", [today_str]).rows
    if met:
        sleep_today, target_cal, target_prot = met[0]

# Cargar consumos de hoy
consumed_cal, consumed_prot, consumed_carbs, consumed_fats = 0, 0, 0, 0
if db:
    meals_today = db.execute("SELECT SUM(calories), SUM(protein_g), SUM(carbs_g), SUM(fat_g) FROM meals WHERE date=?", [today_str]).rows
    if meals_today and meals_today[0][0] is not None:
        consumed_cal, consumed_prot, consumed_carbs, consumed_fats = meals_today[0]

# --- UI PRINCIPAL (LAS 5 PESTAÑAS) ---
st.markdown("### Hola 👋 Vamos por un mejor tú")

tab1, tab2, tab3, tab4, tab5 = st.tabs(["🏠 Inicio", "🍗 Comida", "🏋️ Rutina", "📈 Progreso", "⚙️ Plan"])

# --- PESTAÑA 1: INICIO (Dashboard) ---
with tab1:
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("Calorías del día")
    colA, colB = st.columns([1, 1.2])
    
    with colA:
        # Gráfico de Anillo Plotly (Donut Chart)
        remaining_cal = max(0, target_cal - consumed_cal)
        fig = go.Figure(data=[go.Pie(
            labels=['Consumido', 'Restante'], values=[consumed_cal, remaining_cal],
            hole=.75, marker_colors=['#FF7B00', '#2D303E'], textinfo='none', hoverinfo='label+value'
        )])
        fig.update_layout(
            showlegend=False, margin=dict(t=0, b=0, l=0, r=0),
            paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
            annotations=[dict(text=f"{int(consumed_cal)}<br><span style='font-size:12px'>de {int(target_cal)}</span>", x=0.5, y=0.5, font_size=20, showarrow=False, font_color="white")]
        )
        st.plotly_chart(fig, use_container_width=True, config={'displayModeBar': False})
        
    with colB:
        st.markdown("<br>", unsafe_allow_html=True)
        st.progress(min(consumed_prot/target_prot, 1.0) if target_prot else 0)
        st.caption(f"🔵 Proteína: {int(consumed_prot)}g / {int(target_prot)}g")
        
        st.progress(min(consumed_carbs/300, 1.0)) # Meta dura temporal
        st.caption(f"🟣 Carbohidratos: {int(consumed_carbs)}g")
        
        st.progress(min(consumed_fats/80, 1.0)) # Meta dura temporal
        st.caption(f"🟡 Grasas: {int(consumed_fats)}g")
    st.markdown('</div>', unsafe_allow_html=True)

    c1, c2 = st.columns(2)
    with c1:
        st.markdown(f'<div class="card">💤 <b>Sueño</b><br><span style="font-size:1.5rem">{sleep_today} h</span></div>', unsafe_allow_html=True)
    with c2:
        st.markdown(f'<div class="card">🔥 <b>Kcal quemadas</b><br><span style="font-size:1.5rem">--</span></div>', unsafe_allow_html=True)

# --- PESTAÑA 2: COMIDA ---
with tab2:
    st.subheader("Registrar Comida")
    meal_text = st.text_area("¿Qué comiste?", placeholder="Ej. 150g de pechuga asada con 1 taza de arroz blanco...", height=100)
    
    if st.button("✨ Analizar con IA", use_container_width=True, type="primary"):
        if meal_text and db:
            with st.spinner("Desglosando macros..."):
                try:
                    data = parse_meal(meal_text)
                    # Mostrar desglose como en la imagen
                    st.markdown("### Resultado de la IA")
                    for item in data.items:
                        st.markdown(f"""
                        <div class="card" style="display:flex; justify-content:space-between;">
                            <div><b>{item.name}</b><br><span style='font-size:12px;color:#888;'>P: {item.protein_g}g | C: {item.carbs_g}g | G: {item.fat_g}g</span></div>
                            <div style="text-align:right;"><b>{item.calories} kcal</b></div>
                        </div>
                        """, unsafe_allow_html=True)
                    
                    # Guardar en BD
                    desc = ", ".join([i.name for i in data.items])
                    db.execute(
                        "INSERT INTO meals (date, description, calories, protein_g, carbs_g, fat_g) VALUES (?, ?, ?, ?, ?, ?)",
                        [today_str, desc, data.total_calories, data.total_protein_g, data.total_carbs_g, data.total_fat_g]
                    )
                    st.success(f"Guardado: {data.total_calories} kcal totales.")
                except Exception as e:
                    st.error(str(e))

# --- PESTAÑA 3: RUTINA ---
with tab3:
    st.subheader("Entrenamiento de Hoy")
    exercise = st.text_input("Ejercicio", placeholder="Ej. Press de banca")
    c_sets, c_reps, c_weight = st.columns(3)
    sets = c_sets.number_input("Series", value=4, min_value=1)
    reps = c_reps.number_input("Reps", value=8, min_value=1)
    weight = c_weight.number_input("Peso (kg)", value=60.0, step=2.5)

    if st.button("Guardar Serie", use_container_width=True, type="primary"):
        if exercise and db:
            db.execute("INSERT INTO workouts (date, exercise, sets, reps, weight_kg) VALUES (?, ?, ?, ?, ?)", [today_str, exercise.strip().title(), sets, reps, weight])
            st.success(f"✅ {exercise} guardado.")

    st.markdown("---")
    st.markdown("### Historial de hoy")
    if db:
        hist = db.execute("SELECT exercise, sets, reps, weight_kg FROM workouts WHERE date=?", [today_str]).rows
        for h in hist:
            st.markdown(f'<div class="card">🏋️ <b>{h[0]}</b><br><span style="color:#888;">{h[1]} series • {h[2]} reps @ {h[3]} kg</span></div>', unsafe_allow_html=True)

# --- PESTAÑA 4: PROGRESO ---
with tab4:
    st.subheader("Estadísticas")
    if db:
        workouts_res = db.execute("SELECT date, exercise, MAX(weight_kg) as top_weight FROM workouts GROUP BY date, exercise ORDER BY date DESC LIMIT 30").rows
        if workouts_res:
            df_work = pd.DataFrame(workouts_res, columns=["date", "exercise", "top_weight"])
            selected_ex = st.selectbox("Evolución de fuerza:", df_work["exercise"].unique())
            fig_strength = px.line(df_work[df_work["exercise"] == selected_ex], x="date", y="top_weight", markers=True, template="plotly_dark")
            fig_strength.update_traces(line_color="#FF7B00")
            st.plotly_chart(fig_strength, use_container_width=True, config={'displayModeBar': False})
            
        meals_res = db.execute("SELECT date, SUM(calories) as cal FROM meals GROUP BY date ORDER BY date DESC LIMIT 7").rows
        if meals_res:
            df_m = pd.DataFrame(meals_res, columns=["date", "cal"])
            fig_bar = px.bar(df_m, x="date", y="cal", title="Calorías últimos 7 días", template="plotly_dark", color_discrete_sequence=["#FF7B00"])
            st.plotly_chart(fig_bar, use_container_width=True, config={'displayModeBar': False})

# --- PESTAÑA 5: PLAN / PERFIL ---
with tab5:
    st.subheader("Mi Plan y Metas")
    with st.form("plan_form"):
        new_cal = st.slider("Meta Calórica", 1500, 4000, int(target_cal), 50)
        new_prot = st.slider("Meta Proteína (g)", 80, 300, int(target_prot), 5)
        new_sleep = st.slider("Horas de sueño dormidas hoy", 0.0, 12.0, float(sleep_today), 0.5)
        
        if st.form_submit_button("Actualizar Mi Plan", use_container_width=True):
            if db:
                db.execute(
                    "INSERT INTO daily_metrics (date, sleep_hours, target_calories, target_protein_g) VALUES (?, ?, ?, ?) ON CONFLICT(date) DO UPDATE SET sleep_hours=excluded.sleep_hours, target_calories=excluded.target_calories, target_protein_g=excluded.target_protein_g",
                    [today_str, new_sleep, new_cal, new_prot]
                )
                st.success("Plan actualizado.")
                st.rerun()
