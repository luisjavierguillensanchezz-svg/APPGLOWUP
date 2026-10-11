import os
import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import date
import libsql_client
from google import genai
from pydantic import BaseModel

# Configuración de variables de entorno
TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")

# Inicialización del cliente Gemini
ai_client = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None

def get_db():
    return libsql_client.create_client_sync(url=TURSO_URL, auth_token=TURSO_TOKEN)

# Esquema para estructurar la respuesta de nutrición
class MealEstimate(BaseModel):
    summary: str
    total_calories: float
    total_protein_g: float
    total_carbs_g: float
    total_fat_g: float

def parse_meal(text: str) -> MealEstimate:
    prompt = f"Analiza esta comida para un plan de hipertrofia y calcula calorías y macronutrientes estimados totales: '{text}'"
    response = ai_client.models.generate_content(
        model='gemini-1.5-flash',
        contents=prompt,
        config=dict(
            response_mime_type="application/json",
            response_schema=MealEstimate,
            temperature=0.1
        )
    )
    return MealEstimate.model_validate_json(response.text)

# Interfaz Streamlit
st.set_page_config(page_title="Hypertrophy Tracker", layout="wide")
today_str = str(date.today())

st.title("⚡ Hypertrophy Tracker")

tab_input, tab_charts, tab_backup = st.tabs(["Registro Diario", "Gráficos & Progreso", "Respaldos"])

with tab_input:
    col1, col2 = st.columns(2)

    with col1:
        st.subheader("🍗 Nutrición (Voz / Texto)")
        meal_text = st.text_area("Comida (ej. '4 huevos con 2 tortillas y licuado de 30g de proteína'):")
        if st.button("Calcular y Guardar Comida"):
            if meal_text and ai_client and TURSO_URL:
                data = parse_meal(meal_text)
                db = get_db()
                db.execute(
                    "INSERT INTO meals (date, description, calories, protein_g, carbs_g, fat_g) VALUES (?, ?, ?, ?, ?, ?)",
                    [today_str, data.summary, data.total_calories, data.total_protein_g, data.total_carbs_g, data.total_fat_g]
                )
                db.close()
                st.success(f"Guardado: {data.total_calories} kcal | {data.total_protein_g}g Prot | {data.total_carbs_g}g Carb | {data.total_fat_g}g Grasa")
            else:
                st.error("Revisa que el texto o las credenciales no estén vacíos.")

        st.subheader("💤 Sueño y Metas de Hoy")
        sleep = st.number_input("Horas de sueño:", min_value=0.0, max_value=16.0, step=0.5, value=7.5)
        c_target = st.number_input("Meta Calórica:", value=2800)
        p_target = st.number_input("Meta Proteína (g):", value=160)
        if st.button("Guardar Métricas del Día"):
            if TURSO_URL:
                db = get_db()
                db.execute(
                    "INSERT INTO daily_metrics (date, sleep_hours, target_calories, target_protein_g) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(date) DO UPDATE SET sleep_hours=excluded.sleep_hours, target_calories=excluded.target_calories, target_protein_g=excluded.target_protein_g",
                    [today_str, sleep, c_target, p_target]
                )
                db.close()
                st.success("Métricas del día actualizadas.")

    with col2:
        st.subheader("🏋️ Registro de Series de Fuerza")
        exercise = st.text_input("Ejercicio (ej. Press banca con mancuernas)")
        c_sets, c_reps, c_weight = st.columns(3)
        sets = c_sets.number_input("Series", value=4, min_value=1)
        reps = c_reps.number_input("Reps", value=8, min_value=1)
        weight = c_weight.number_input("Peso (kg)", value=30.0, step=0.5)

        if st.button("Guardar Serie / Ejercicio"):
            if exercise and TURSO_URL:
                db = get_db()
                db.execute(
                    "INSERT INTO workouts (date, exercise, sets, reps, weight_kg) VALUES (?, ?, ?, ?, ?)",
                    [today_str, exercise.strip().title(), sets, reps, weight]
                )
                db.close()
                st.success(f"Guardado: {exercise} - {sets}x{reps} @ {weight}kg")

with tab_charts:
    st.subheader("📈 Progresión")
    if TURSO_URL:
        db = get_db()
        meals_res = db.execute("SELECT date, SUM(calories) as total_cal, SUM(protein_g) as total_prot FROM meals GROUP BY date ORDER BY date DESC LIMIT 14")
        workouts_res = db.execute("SELECT date, exercise, MAX(weight_kg) as top_weight FROM workouts GROUP BY date, exercise ORDER BY date DESC LIMIT 30")
        db.close()

        if meals_res.rows:
            df_meals = pd.DataFrame(meals_res.rows, columns=["date", "total_cal", "total_prot"])
            fig_cal = px.bar(df_meals, x="date", y="total_cal", title="Consumo Calórico Diario")
            fig_prot = px.bar(df_meals, x="date", y="total_prot", title="Consumo de Proteína Diaria (g)")
            st.plotly_chart(fig_cal, use_container_width=True)
            st.plotly_chart(fig_prot, use_container_width=True)

        if workouts_res.rows:
            df_work = pd.DataFrame(workouts_res.rows, columns=["date", "exercise", "top_weight"])
            selected_ex = st.selectbox("Ejercicio para sobrecarga progresiva:", df_work["exercise"].unique())
            df_ex = df_work[df_work["exercise"] == selected_ex]
            fig_strength = px.line(df_ex, x="date", y="top_weight", markers=True, title=f"Sobrecarga: {selected_ex} (Carga máx kg)")
            st.plotly_chart(fig_strength, use_container_width=True)
    else:
        st.info("Configura la conexión para ver gráficos.")

with tab_backup:
    st.subheader("💾 Exportar Copia de Seguridad")
    if st.button("Generar Backup en JSON"):
        if TURSO_URL:
            db = get_db()
            m = db.execute("SELECT * FROM meals").rows
            w = db.execute("SELECT * FROM workouts").rows
            d = db.execute("SELECT * FROM daily_metrics").rows
            db.close()

            backup_data = {
                "meals": [dict(zip(["id", "date", "desc", "cal", "prot", "carb", "fat"], r)) for r in m],
                "workouts": [dict(zip(["id", "date", "ex", "sets", "reps", "kg"], r)) for r in w],
                "daily_metrics": [dict(zip(["date", "sleep", "target_cal", "target_prot"], r)) for r in d]
            }
            import json
            st.download_button(
                label="Descargar archivo JSON",
                data=json.dumps(backup_data, indent=2),
                file_name=f"gym_backup_{today_str}.json",
                mime="application/json"
            )