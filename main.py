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

# 1. Carregamento de Variáveis de Ambiente
STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Validação de Segurança (Garante que o motor não ligue sem as chaves)
if not all([STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, SUPABASE_URL, SUPABASE_KEY, GEMINI_API_KEY]):
    print("⚠️ ATENÇÃO: Variáveis de ambiente ausentes. O sistema pode apresentar falhas.")

# 2. Inicialização do Banco de Dados (Supabase)
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    raise RuntimeError(f"🚨 Falha crítica ao conectar com Supabase: {e}")

# 3. Inicialização da Aplicação FastAPI
app = FastAPI(
    title="API Analista de Bolso",
    description="Motor backend para o PWA do Analista de Bolso",
    version="1.0.0"
)

# 4. Configuração de Segurança (CORS)
# Permite que o futuro Frontend (React/PWA) acesse esta API de qualquer domínio
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# MODELOS DE DADOS (Pydantic)
# ==========================================
class StravaAuthRequest(BaseModel):
    code: str

class IAAnaliseRequest(BaseModel):
    strava_id: int

# ==========================================
# FUNÇÕES AUXILIARES (Tratamento de Dados Strava)
# ==========================================
def atualizar_token_strava(refresh_token: str) -> str:
    """Busca um novo access_token no Strava caso o atual esteja expirado."""
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
    """Sincroniza os dados do Strava, trata com Pandas e retorna lista de dicionários."""
    url = 'https://www.strava.com/api/v3/athlete/activities'
    headers = {'Authorization': f'Bearer {access_token}'}
    params = {'per_page': 30, 'page': 1} # Busca os 30 mais recentes
    
    res = requests.get(url, headers=headers, params=params)
    if res.status_code != 200:
        raise HTTPException(status_code=502, detail="Erro de comunicação com API do Strava")
        
    dados_pagina = res.json()
    if not dados_pagina:
        return []
        
    df_bruto = pd.DataFrame(dados_pagina)
    if 'type' not in df_bruto.columns:
        return []
        
    df_corridas = df_bruto[df_bruto['type'] == 'Run'].copy()
    if df_corridas.empty:
        return []
        
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

    colunas = ['name', 'distancia_km', 'Pace_Medio', 'Cadence_SPM', 'average_heartrate', 'total_elevation_gain']
    
    # Tratamento para evitar TypeError no Supabase
    json_string = df_corridas[colunas].to_json(orient='records', force_ascii=False)
    return json.loads(json_string)


# ==========================================
# ENDPOINTS (As Tomadas da nossa API)
# ==========================================

@app.get("/")
def health_check():
    """Rota de Status para monitoramento do servidor."""
    return {"status": "Motor Analista de Bolso Operante 🚀"}

@app.post("/auth/strava")
def autenticar_strava(requisicao: StravaAuthRequest):
    """Recebe o código OAuth do Frontend, troca por tokens e cadastra o usuário."""
    url = 'https://www.strava.com/oauth/token'
    payload = {
        'client_id': STRAVA_CLIENT_ID,
        'client_secret': STRAVA_CLIENT_SECRET,
        'code': requisicao.code,
        'grant_type': 'authorization_code'
    }
    
    try:
        res = requests.post(url, data=payload)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Erro de rede ao contatar Strava: {e}")
        
    if res.status_code != 200:
        raise HTTPException(status_code=400, detail="Código de autorização inválido ou expirado.")
        
    token_payload = res.json()
    atleta = token_payload.get('athlete', {})
    atleta_id = atleta.get('id')
    
    if not atleta_id:
        raise HTTPException(status_code=400, detail="Perfil de atleta não retornado pelo Strava.")
    
    # Prepara dados para o Supabase (Upsert)
    upsert_data = {
        "id": atleta_id,
        "access_token": token_payload.get('access_token'),
        "refresh_token": token_payload.get('refresh_token'),
        "expires_at": token_payload.get('expires_at'),
        "nome": atleta.get('firstname'),
        "sobrenome": atleta.get('lastname'),
        "cidade": atleta.get('city'),
        "estado": atleta.get('state'),
        "genero": atleta.get('sex'),
        "peso": atleta.get('weight'),
        "foto_url": atleta.get('profile')
    }
    
    try:
        supabase.table("usuarios_strava").upsert(upsert_data).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Falha ao gravar usuário no banco de dados: {e}")
        
    return {"status": "success", "strava_id": atleta_id}


@app.post("/ia/analise")
def gerar_analise_ia(requisicao: IAAnaliseRequest):
    """Busca histórico do banco (ou atualiza do Strava) e injeta no Gemini para análise."""
    
    # 1. Recuperar o perfil do banco
    try:
        res_db = supabase.table("usuarios_strava").select("*").eq("id", requisicao.strava_id).execute()
        if not res_db.data:
            raise HTTPException(status_code=404, detail="Atleta não encontrado no banco de dados.")
        usuario = res_db.data[0]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao acessar banco de dados: {e}")

    # 2. Recuperar ou sincronizar o histórico
    historico = usuario.get("historico_json")
    
    if not historico: # Se estiver vazio, fazemos a primeira sincronização automaticamente
        access_token = atualizar_token_strava(usuario['refresh_token'])
        historico = baixar_e_tratar_treinos(access_token)
        if historico:
            supabase.table("usuarios_strava").update({"historico_json": historico}).eq("id", requisicao.strava_id).execute()

    if not historico:
        raise HTTPException(status_code=400, detail="Não há corridas suficientes para gerar a análise.")

    # 3. Preparar o Prompt
    treinos_recentes = historico[:3] # Pega apenas os 3 últimos para não sobrecarregar o prompt
    payload_para_ia = json.dumps(treinos_recentes, ensure_ascii=False)
    
    prompt = f"""
    Analise o atleta {usuario.get('nome')}, {usuario.get('idade', 'N/A')} anos, {usuario.get('altura', 'N/A')}cm de altura, {usuario.get('peso', 'N/A')}kg, sexo {usuario.get('genero', 'N/A')}. 
    Use Jack Daniels e princípios de biomecânica (relacionando altura com cadência/passada) para avaliar estes treinos em JSON: 
    {payload_para_ia}

    Você deve retornar estritamente um arquivo JSON válido contendo exatamente estas 3 chaves: 
    'diagnostico_geral' (resumo de 2 linhas), 
    'ponto_de_melhoria' (focado na relação pace/cadência/bpm e comprimento de passada baseada na altura do atleta), e 
    'nota_eficiencia' (um número inteiro de 0 a 10).
    """
    
    # 4. Enviar ao Gemini
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        resposta_ia = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        analise = json.loads(resposta_ia.text)
        return analise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Falha na comunicação com o motor de IA (Gemini): {e}")