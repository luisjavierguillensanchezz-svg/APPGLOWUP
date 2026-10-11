import os
import time
import json
from datetime import date
import streamlit as st
import pandas as pd
import plotly.express as px
import libsql_client
from google import genai
from pydantic import BaseModel

# --- LECTURA SEGURA DE SECRETS ---
TURSO_URL = st.secrets.get("TURSO_DATABASE_URL", os.environ.get("TURSO_DATABASE_URL", ""))
TURSO_TOKEN = st.secrets.get("TURSO_AUTH_TOKEN", os.environ.get("TURSO_AUTH_TOKEN", ""))
GEMINI_KEY = st.secrets.get("GEMINI_API_KEY", os.environ.get("GEMINI_API_KEY", ""))

ai_client = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None

def get_db():
    if not TURSO_URL or not TURSO_TOKEN:
        return None
    url = TURSO_URL.replace("libsql://", "https://")
    return libsql_client.create_client_sync(url=url, auth_token=TURSO_TOKEN)

def ensure_tables():
    db = get_db()
    if not db:
        return
    try:
        db.batch([
            """CREATE TABLE IF NOT EXISTS meals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                description TEXT,
                calories REAL NOT NULL,
                protein_g REAL NOT NULL,
                carbs_g REAL NOT NULL,
                fat_g REAL NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS workouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                exercise TEXT NOT NULL,
                sets INTEGER NOT NULL,
                reps INTEGER NOT NULL,
                weight_kg REAL NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS daily_metrics (
                date TEXT PRIMARY KEY,
                sleep_hours REAL,
                target_calories REAL,
                target_protein_g REAL
            )"""
        ])
    except Exception as e:
        st.error(f"Error inicializando tablas en Turso: {e}")
    finally:
        db.close()

ensure_tables()

# --- MODELO PYDANTIC Y PARSER ---
class MealEstimate(BaseModel):
    summary: str
    total_calories: float
    total_protein_g: float
    total_carbs_g: float
    total_fat_g: float

def parse_meal(text: str) -> MealEstimate:
    prompt = f"Analiza para hipertrofia: '{text}'. Calcula calorías y macronutrientes estimados totales."
    candidates = ['gemini-flash-latest', 'gemini-3.8-flash']
    last_err = None

    for model_name in candidates:
        for wait_time in [1, 3]:
            try:
                response = ai_client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=dict(
                        response_mime_type="application/json",
                        response_schema=MealEstimate,
                        temperature=0.1
                    )
                )
                return MealEstimate.model_validate_json(response.text)
            except Exception as e:
                last_err = e
                err_str = str(e)
                if "503" in err_str:
                    time.sleep(wait_time)
                    continue
                break

    raise RuntimeError(f"Error con el servicio de IA: {last_err}")

# --- CONFIGURACIÓN DE PÁGINA ---
st.set_page_config(page_title="Hypertrophy Tracker", layout="wide")
today_str = str(date.today())

st.title("⚡ Hypertrophy Tracker")

if not TURSO_URL or not TURSO_TOKEN or not GEMINI_KEY:
    st.warning("⚠️ Faltan credenciales en Secrets. Ve a Manage app > Settings > Secrets y agrégalas.")

tab_input, tab_charts, tab_backup = st.tabs(["Registro Diario", "Gráficos & Progreso", "Respaldos"])

with tab_input:
    col1, col2 = st.columns(2)

    with col1:
        st.subheader("🍗 Nutrición (Voz / Texto)")
        meal_text = st.text_area("Comida (ej. '4 huevos con 2 tortillas y licuado de 30g de proteína'):")
        if st.button("Calcular y Guardar Comida"):
            if meal_text and ai_client and TURSO_URL:
                try:
                    data = parse_meal(meal_text)
                    db = get_db()
                    db.execute(
                        "INSERT INTO meals (date, description, calories, protein_g, carbs_g, fat_g) VALUES (?, ?, ?, ?, ?, ?)",
                        [today_str, data.summary, data.total_calories, data.total_protein_g, data.total_carbs_g, data.total_fat_g]
                    )
                    db.close()
                    st.success(f"Guardado: {data.total_calories} kcal | {data.total_protein_g}g Prot | {data.total_carbs_g}g Carb | {data.total_fat_g}g Grasa")
                except Exception as e:
                    st.error(f"Error procesando comida: {e}")
            else:
                st.error("Verifica credenciales o texto ingresado.")

        st.subheader("💤 Sueño y Metas de Hoy")
        sleep = st.number_input("Horas de sueño:", min_value=0.0, max_value=16.0, step=0.5, value=7.5)
        c_target = st.number_input("Meta Calórica:", value=2800)
        p_target = st.number_input("Meta Proteína (g):", value=160)
        if st.button("Guardar Métricas del Día"):
            db = get_db()
            if db:
                try:
                    db.execute(
                        "INSERT INTO daily_metrics (date, sleep_hours, target_calories, target_protein_g) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(date) DO UPDATE SET sleep_hours=excluded.sleep_hours, target_calories=excluded.target_calories, target_protein_g=excluded.target_protein_g",
                        [today_str, sleep, c_target, p_target]
                    )
                    st.success("Métricas del día actualizadas.")
                except Exception as e:
                    st.error(f"Error guardando métricas: {e}")
                finally:
                    db.close()

    with col2:
        st.subheader("🏋️ Registro de Series de Fuerza")
        exercise = st.text_input("Ejercicio (ej. Press banca mancuernas)")
        c_sets, c_reps, c_weight = st.columns(3)
        sets = c_sets.number_input("Series", value=4, min_value=1)
        reps = c_reps.number_input("Reps", value=8, min_value=1)
        weight = c_weight.number_input("Peso (kg)", value=30.0, step=0.5)

        if st.button("Guardar Serie"):
            db = get_db()
            if exercise and db:
                try:
                    db.execute(
                        "INSERT INTO workouts (date, exercise, sets, reps, weight_kg) VALUES (?, ?, ?, ?, ?)",
                        [today_str, exercise.strip().title(), sets, reps, weight]
                    )
                    st.success(f"Guardado: {exercise} - {sets}x{reps} @ {weight}kg")
                except Exception as e:
                    st.error(f"Error guardando serie: {e}")
                finally:
                    db.close()

with tab_charts:
    st.subheader("📈 Progresión")
    db = get_db()
    if db:
        try:
            meals_res = db.execute("SELECT date, SUM(calories) as total_cal, SUM(protein_g) as total_prot FROM meals GROUP BY date ORDER BY date DESC LIMIT 14")
            workouts_res = db.execute("SELECT date, exercise, MAX(weight_kg) as top_weight FROM workouts GROUP BY date, exercise ORDER BY date DESC LIMIT 30")

            if meals_res.rows:
                df_meals = pd.DataFrame(meals_res.rows, columns=["date", "total_cal", "total_prot"])
                fig_cal = px.bar(df_meals, x="date", y="total_cal", title="Consumo Calórico Diario")
                fig_prot = px.bar(df_meals, x="date", y="total_prot", title="Consumo de Proteína Diaria (g)")
                st.plotly_chart(fig_cal, width="stretch")
                st.plotly_chart(fig_prot, width="stretch")
            else:
                st.info("Aún no hay registros de comidas para graficar.")

            if workouts_res.rows:
                df_work = pd.DataFrame(workouts_res.rows, columns=["date", "exercise", "top_weight"])
                selected_ex = st.selectbox("Ejercicio para sobrecarga progresiva:", df_work["exercise"].unique())
                df_ex = df_work[df_work["exercise"] == selected_ex]
                fig_strength = px.line(df_ex, x="date", y="top_weight", markers=True, title=f"Sobrecarga: {selected_ex} (Carga máx kg)")
                st.plotly_chart(fig_strength, width="stretch")
            else:
                st.info("Aún no hay entrenamientos registrados para graficar.")
        except Exception as e:
            st.error(f"Error al consultar datos: {e}")
        finally:
            db.close()
    else:
        st.info("Configura la conexión para ver gráficos.")

with tab_backup:
    st.subheader("💾 Exportar Copia de Seguridad")
    if st.button("Generar Backup en JSON"):
        db = get_db()
        if db:
            try:
                m = db.execute("SELECT * FROM meals").rows
                w = db.execute("SELECT * FROM workouts").rows
                d = db.execute("SELECT * FROM daily_metrics").rows

                backup_data = {
                    "meals": [list(r) for r in m],
                    "workouts": [list(r) for r in w],
                    "daily_metrics": [list(r) for r in d]
                }
                st.download_button(
                    label="Descargar archivo JSON",
                    data=json.dumps(backup_data, indent=2),
                    file_name=f"gym_backup_{today_str}.json",
                    mime="application/json"
                )
            except Exception as e:
                st.error(f"Error generando respaldo: {e}")
            finally:
                db.close()
