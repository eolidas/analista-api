import os
import json
import requests
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from google import genai
from google.genai import types
from supabase import create_client, Client

# ==========================================
# MOTOR ANALISTA DE BOLSO - BACKEND (FastAPI)
# ==========================================

STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    raise RuntimeError(f"🚨 Falha crítica ao conectar com Supabase: {e}")

app = FastAPI(title="API Analista de Bolso")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class StravaAuthRequest(BaseModel):
    code: str

class IAAnaliseRequest(BaseModel):
    strava_id: int

# --- FUNÇÕES AUXILIARES ---
def atualizar_token_strava(refresh_token: str) -> str:
    url = 'https://www.strava.com/oauth/token'
    payload = {
        'client_id': STRAVA_CLIENT_ID,
        'client_secret': STRAVA_CLIENT_SECRET,
        'refresh_token': refresh_token,
        'grant_type': 'refresh_token'
    }
    res = requests.post(url, data=payload)
    if res.status_code == 200:
        return res.json().get('access_token')
    raise HTTPException(status_code=401, detail="Falha ao renovar token do Strava")

def baixar_e_tratar_treinos(access_token: str):
    url = 'https://www.strava.com/api/v3/athlete/activities'
    headers = {'Authorization': f'Bearer {access_token}'}
    params = {'per_page': 30, 'page': 1}
    
    res = requests.get(url, headers=headers, params=params)
    if res.status_code != 200: return []
        
    dados_pagina = res.json()
    if not dados_pagina: return []
        
    df_bruto = pd.DataFrame(dados_pagina)
    if 'type' not in df_bruto.columns: return []
        
    df_corridas = df_bruto[df_bruto['type'] == 'Run'].copy()
    if df_corridas.empty: return []
        
    df_corridas['distancia_km'] = df_corridas['distance'] / 1000.0
    
    def formatar_pace(linha):
        if linha['distancia_km'] == 0: return "00:00"
        pace_dec = (linha['moving_time'] / 60) / linha['distancia_km']
        return f"{int(pace_dec):02d}:{int(round((pace_dec - int(pace_dec)) * 60)):02d}"
        
    df_corridas['Pace_Medio'] = df_corridas.apply(formatar_pace, axis=1)
    df_corridas['Cadence_SPM'] = df_corridas['average_cadence'] * 2 if 'average_cadence' in df_corridas.columns else 0
    df_corridas['average_heartrate'] = df_corridas['average_heartrate'].fillna(0) if 'average_heartrate' in df_corridas.columns else 0
    df_corridas['total_elevation_gain'] = df_corridas['total_elevation_gain'] if 'total_elevation_gain' in df_corridas.columns else 0
        
    df_corridas['start_date_local'] = pd.to_datetime(df_corridas['start_date_local'])
    df_corridas = df_corridas.sort_values(by='start_date_local', ascending=False).reset_index(drop=True)

    colunas = ['name', 'distancia_km', 'Pace_Medio', 'Cadence_SPM', 'average_heartrate', 'total_elevation_gain', 'start_date_local']
    
    json_string = df_corridas[colunas].to_json(orient='records', force_ascii=False, date_format='iso')
    return json.loads(json_string)

# --- ENDPOINTS (TOMADAS) ---

@app.get("/")
def health_check():
    return {"status": "Motor Analista de Bolso Operante 🚀"}

@app.post("/auth/strava")
def autenticar_strava(requisicao: StravaAuthRequest):
    url = 'https://www.strava.com/oauth/token'
    payload = {
        'client_id': STRAVA_CLIENT_ID,
        'client_secret': STRAVA_CLIENT_SECRET,
        'code': requisicao.code,
        'grant_type': 'authorization_code'
    }
    
    res = requests.post(url, data=payload)
    if res.status_code != 200: raise HTTPException(status_code=400, detail="Código inválido.")
        
    token_payload = res.json()
    atleta = token_payload.get('athlete', {})
    atleta_id = atleta.get('id')
    
    upsert_data = {
        "id": atleta_id,
        "access_token": token_payload.get('access_token'),
        "refresh_token": token_payload.get('refresh_token'),
        "nome": atleta.get('firstname'),
        "sobrenome": atleta.get('lastname'),
        "foto_url": atleta.get('profile')
    }
    supabase.table("usuarios_strava").upsert(upsert_data).execute()
        
    return {"status": "success", "strava_id": atleta_id}

@app.get("/atleta/{strava_id}")
def obter_dados_atleta(strava_id: int):
    """Nova Rota: Entrega os dados do atleta para popular o Dashboard."""
    res_db = supabase.table("usuarios_strava").select("nome, foto_url, peso, idade, historico_json, ia_report_json").eq("id", strava_id).execute()
    if not res_db.data:
        raise HTTPException(status_code=404, detail="Atleta não encontrado.")
    return res_db.data[0]

@app.post("/atleta/{strava_id}/sincronizar")
def sincronizar_treinos(strava_id: int):
    """Nova Rota: Força o download dos treinos no Strava."""
    res_db = supabase.table("usuarios_strava").select("refresh_token").eq("id", strava_id).execute()
    if not res_db.data: raise HTTPException(status_code=404, detail="Atleta não encontrado.")
    
    refresh_token = res_db.data[0]['refresh_token']
    access_token = atualizar_token_strava(refresh_token)
    historico = baixar_e_tratar_treinos(access_token)
    
    supabase.table("usuarios_strava").update({"historico_json": historico}).eq("id", strava_id).execute()
    return {"status": "success", "historico": historico}

@app.post("/ia/analise")
def gerar_analise_ia(requisicao: IAAnaliseRequest):
    res_db = supabase.table("usuarios_strava").select("*").eq("id", requisicao.strava_id).execute()
    usuario = res_db.data[0]
    historico = usuario.get("historico_json")
    
    if not historico: raise HTTPException(status_code=400, detail="Sem treinos para analisar.")

    treinos_recentes = historico[:3]
    payload_para_ia = json.dumps(treinos_recentes, ensure_ascii=False)
    
    prompt = f"""
    Analise o atleta {usuario.get('nome')}. Use Jack Daniels e princípios biomecânicos para avaliar: {payload_para_ia}.
    Retorne ESTRITAMENTE um JSON: 'diagnostico_geral' (2 linhas), 'ponto_de_melhoria' (focado na biomecânica), 'nota_eficiencia' (0 a 10).
    """
    
    client = genai.Client(api_key=GEMINI_API_KEY)
    resposta_ia = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    analise = json.loads(resposta_ia.text)
    
    # Salva a análise no banco para a próxima vez que o usuário entrar
    supabase.table("usuarios_strava").update({"ia_report_json": analise}).eq("id", requisicao.strava_id).execute()
    
    return analise
