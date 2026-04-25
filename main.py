import os
import json
import requests
import pandas as pd
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from google import genai
from google.genai import types
from supabase import create_client, Client

# ==========================================
# MOTOR ANALISTA DE BOLSO - BACKEND (FastAPI)
# Missão: Sincronização Inteligente & Fonte da Verdade
# ==========================================

# Carregamento das variáveis de ambiente (Configuradas no Render)
STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Inicialização do Cliente Supabase
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    raise RuntimeError(f"🚨 Falha crítica ao conectar com Supabase: {e}")

app = FastAPI(title="API Analista de Bolso")

# Configuração de CORS para permitir que a Vercel aceda ao motor
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

class BiometriaRequest(BaseModel):
    peso: float
    altura: float

# ==========================================
# ⚙️ FUNÇÕES AUXILIARES & REGRAS DE NEGÓCIO
# ==========================================

def atualizar_token_strava(refresh_token: str) -> str:
    """Renova o access_token expirado usando o refresh_token do utilizador."""
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

def baixar_novos_treinos(access_token: str, after_timestamp: int = None):
    """
    Sincronização Delta: Procura no Strava APENAS atividades após o timestamp informado.
    """
    url = 'https://www.strava.com/api/v3/athlete/activities'
    headers = {'Authorization': f'Bearer {access_token}'}
    params = {'per_page': 50, 'page': 1}
    
    if after_timestamp:
        params['after'] = after_timestamp

    res = requests.get(url, headers=headers, params=params)
    if res.status_code != 200: return []
        
    dados = res.json()
    if not dados: return []
        
    df_bruto = pd.DataFrame(dados)
    if 'type' not in df_bruto.columns: return []
        
    # Filtra apenas corridas
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

    colunas = ['name', 'distancia_km', 'Pace_Medio', 'Cadence_SPM', 'average_heartrate', 'total_elevation_gain', 'moving_time', 'start_date_local']
    
    json_string = df_corridas[colunas].to_json(orient='records', force_ascii=False, date_format='iso')
    return json.loads(json_string)

def calcular_estatisticas(historico: list) -> dict:
    """
    Calculadora Backend: Processa o histórico para extrair volumes de treino.
    """
    if not historico:
        return {"totalDist": 0, "totalWorkouts": 0, "monthVolume": 0, "weekVolume": 0}

    total_dist = 0
    month_volume = 0
    week_volume = 0
    
    hoje = datetime.now()
    mes_atual = hoje.month
    ano_atual = hoje.year
    
    # Define o início da semana (segunda-feira)
    inicio_semana = hoje - timedelta(days=hoje.weekday())
    inicio_semana = inicio_semana.replace(hour=0, minute=0, second=0, microsecond=0)

    for treino in historico:
        dist = treino.get("distancia_km", 0)
        total_dist += dist

        data_str = treino.get("start_date_local")
        if data_str:
            try:
                # Normalização da data para processamento
                data_limpa = data_str.replace("Z", "+00:00")[:19]
                data_treino = datetime.fromisoformat(data_limpa)

                # Verifica se o treino pertence ao mês atual
                if data_treino.month == mes_atual and data_treino.year == ano_atual:
                    month_volume += dist

                # Verifica se o treino pertence à semana atual
                if data_treino >= inicio_semana:
                    week_volume += dist
            except Exception:
                continue

    return {
        "totalDist": round(total_dist, 1),
        "totalWorkouts": len(historico),
        "monthVolume": round(month_volume, 1),
        "weekVolume": round(week_volume, 1)
    }

# ==========================================
# 🌐 ENDPOINTS (ROTAS DA API)
# ==========================================

@app.get("/")
def health_check():
    return {"status": "Motor Analista de Bolso Operante 🚀"}

@app.post("/auth/strava")
def autenticar_strava(requisicao: StravaAuthRequest):
    # 1. Troca o código pelo Token
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
    access_token = token_payload.get('access_token')
    atleta_resumo = token_payload.get('athlete', {})
    atleta_id = atleta_resumo.get('id')
    
    # 2. PULO DO GATO: Busca o Perfil Detalhado no Strava
    headers = {'Authorization': f'Bearer {access_token}'}
    res_perfil = requests.get('https://www.strava.com/api/v3/athlete', headers=headers)
    
    atleta = res_perfil.json() if res_perfil.status_code == 200 else atleta_resumo
    
    equipamentos = {
        "tenis": atleta.get('shoes', []),
        "bicicletas": atleta.get('bikes', [])
    }
    
    # Extração de Clubes
    clubes = [{"nome": c.get("name"), "foto": c.get("profile")} for c in atleta.get('clubs', [])]
    
    upsert_data = {
        "id": atleta_id,
        "access_token": access_token,
        "refresh_token": token_payload.get('refresh_token'),
        "nome": atleta.get('firstname'),
        "sobrenome": atleta.get('lastname'),
        "foto_url": atleta.get('profile'),
        "peso": atleta.get('weight'),
        "cidade": atleta.get('city'),
        "estado": atleta.get('state'),
        "genero": atleta.get('sex'),
        "data_criacao": atleta.get('created_at'),
        "bio": atleta.get('bio'),
        "clubes": clubes,
        "equipamentos": equipamentos
    }
    supabase.table("usuarios_strava").upsert(upsert_data).execute()
        
    return {"status": "success", "strava_id": atleta_id}

@app.put("/atleta/{strava_id}/biometria")
def atualizar_biometria(strava_id: int, req: BiometriaRequest):
    """Nova Rota: Atualiza Peso e Altura manualmente no Supabase"""
    res = supabase.table("usuarios_strava").update({
        "peso": req.peso,
        "altura": req.altura
    }).eq("id", strava_id).execute()
    
    if not res.data:
        raise HTTPException(status_code=404, detail="Atleta não encontrado")
    return {"status": "success"}

@app.get("/atleta/{strava_id}")
def obter_dados_atleta(strava_id: int):
    """FONTE DA VERDADE: Entrega Perfil, Estatísticas Calculadas e Histórico."""
    res_db = supabase.table("usuarios_strava").select("*").eq("id", strava_id).execute()
    if not res_db.data:
        raise HTTPException(status_code=404, detail="Atleta não encontrado.")
    
    dados = res_db.data[0]
    historico = dados.get("historico_json") or []
    estatisticas = calcular_estatisticas(historico)
    
    return {
        "perfil": {
            "nome": dados.get("nome"),
            "sobrenome": dados.get("sobrenome"),
            "foto_url": dados.get("foto_url"),
            "peso": dados.get("peso"),
            "altura": dados.get("altura"),
            "idade": dados.get("idade"),
            "cidade": dados.get("cidade"),
            "estado": dados.get("estado"),
            "genero": dados.get("genero"),
            "data_criacao": dados.get("data_criacao"),
            "bio": dados.get("bio"),
            "clubes": dados.get("clubes"),
            "equipamentos": dados.get("equipamentos")
        },
        "estatisticas": estatisticas,
        "historico_json": historico,
        "ia_report_json": dados.get("ia_report_json")
    }

@app.post("/atleta/{strava_id}/sincronizar")
def sincronizar_treinos(strava_id: int):
    """SINCRONIZAÇÃO INTELIGENTE (DELTA): Procura a data da última corrida e baixa apenas as novas."""
    res_db = supabase.table("usuarios_strava").select("refresh_token, historico_json").eq("id", strava_id).execute()
    if not res_db.data: raise HTTPException(status_code=404, detail="Atleta não encontrado.")
    
    atleta = res_db.data[0]
    historico_antigo = atleta.get('historico_json') or []
    after_timestamp = None
    
    if historico_antigo:
        data_mais_nova_str = historico_antigo[0].get("start_date_local")
        if data_mais_nova_str:
            data_limpa = data_mais_nova_str.replace("Z", "+00:00")[:19]
            dt = datetime.fromisoformat(data_limpa)
            after_timestamp = int(dt.timestamp())
    
    access_token = atualizar_token_strava(atleta['refresh_token'])
    treinos_novos = baixar_novos_treinos(access_token, after_timestamp)
    
    if treinos_novos:
        historico_atualizado = treinos_novos + historico_antigo
        supabase.table("usuarios_strava").update({"historico_json": historico_atualizado}).eq("id", strava_id).execute()
    else:
        historico_atualizado = historico_antigo
        
    return {
        "status": "success", 
        "novos_treinos_baixados": len(treinos_novos),
        "historico": historico_atualizado,
        "estatisticas": calcular_estatisticas(historico_atualizado)
    }

@app.post("/ia/analise")
def gerar_analise_ia(requisicao: IAAnaliseRequest):
    """Aciona o Gemini para gerar o Dossiê biomecânico baseado nos últimos 3 treinos."""
    res_db = supabase.table("usuarios_strava").select("*").eq("id", requisicao.strava_id).execute()
    usuario = res_db.data[0]
    historico = usuario.get("historico_json")
    
    if not historico: raise HTTPException(status_code=400, detail="Sem treinos para analisar.")

    treinos_recentes = historico[:3]
    payload_para_ia = json.dumps(treinos_recentes, ensure_ascii=False)
    
    altura_txt = f"{usuario.get('altura')}cm" if usuario.get('altura') else "Não informada"
    
    prompt = f"""
    Analise o atleta {usuario.get('nome')}. Altura: {altura_txt}. 
    Use Jack Daniels e princípios biomecânicos (relacione a altura com a cadência) para avaliar: {payload_para_ia}.
    Retorne ESTRITAMENTE um JSON: 'diagnostico_geral' (2 linhas), 'ponto_de_melhoria' (focado na biomecânica), 'nota_eficiencia' (0 a 10).
    """
    
    client = genai.Client(api_key=GEMINI_API_KEY)
    resposta_ia = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    analise = json.loads(resposta_ia.text)
    
    supabase.table("usuarios_strava").update({"ia_report_json": analise}).eq("id", requisicao.strava_id).execute()
    return analise
