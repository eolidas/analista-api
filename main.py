import os
import base64
import json
import requests
import pandas as pd
import math
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from google import genai
from google.genai import types

# =========================================================================
# ANALISTA DE BOLSO - BACKEND DE ALTA PERFORMANCE (FastAPI)
# Arquitetura Clean Code | Versão 4.5.2 (Correção CORS e Health Check)
# Motor Integrado: Strava + Supabase + Gemini 2.5 + Spotify Jukebox
# =========================================================================

app = FastAPI(title="Analista de Bolso API", version="4.5.2")

# Configuração de CORS para permitir acesso do PWA em qualquer domínio
# CORREÇÃO CRÍTICA: allow_credentials deve ser False quando allow_origins é ["*"]
app.add_middleware(
    CORSMiddleware,
app = FastAPI(title="Analista de Bolso API", version="4.5.1")
)
# Configuração de CORS para permitir acesso do PWA em qualquer domínio
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=False, 
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- COFRE DE SEGURANÇA (Variáveis de Ambiente) ---
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

# Inicialização Defensiva do Supabase
supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"⚠️ Alerta: Erro ao conectar ao Supabase: {e}")

# --- MODELOS DE DADOS (Pydantic para Validação Estrita) ---

class AuthRequest(BaseModel):
    code: str

class ZonasFCPayload(BaseModel):
    metodo: str
    fc_max: int = None
    fc_repouso: int = None
    fc_limiar: int = None

class ZonasPacePayload(BaseModel):
    metodo: str
    distancia_km: float = None
    tempo_segundos: int = None
    pace_limiar: str = None
    altitude_m: float = 0
    temperatura_c: float = 0

class ExtrairLimiarPayload(BaseModel):
    activities: list
    compensar_alt: bool = True
    compensar_temp: bool = True

class AnaliseIAPayload(BaseModel):
    strava_id: int

class TrofeusPayload(BaseModel):
    somente_provas: bool = True

class DiarioPayload(BaseModel):
    strava_id: str
    id_atividade_strava: int
    mood_emocional: str = None # Estado Psicológico
    mood_fisico: str = None    # Condição Física
    comentario: str = None
    spotify_track_id: str = None
    spotify_track_name: str = None
    spotify_album_art: str = None

class ParseTreinoPayload(BaseModel):
    strava_id: int
    data_treino: str
    texto_bruto: str

# --- UTILITÁRIOS GLOBAIS ---

def helper_formata_tempo(segundos):
    """Converte segundos brutos para formato humano MM:SS ou HH:MM:SS."""
    h = math.floor(segundos / 3600)
    m = math.floor((segundos % 3600) / 60)
    s = math.floor(segundos % 60)
    
    if h > 0:
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d}"
    return f"{int(m):02d}:{int(s):02d}"

def obter_token_fresco(refresh_token: str):
    """Renova o acesso ao Strava usando o Refresh Token."""
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
    return None

# =========================================================================
# 1. AUTENTICAÇÃO E PERFIL
# =========================================================================

@app.post("/auth/strava")
def auth_strava(req: AuthRequest):
    """Troca código temporário por tokens e inicializa o perfil do atleta."""
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase não configurado.")
    
    url = "https://www.strava.com/oauth/token"
    payload = {
        'client_id': STRAVA_CLIENT_ID,
        'client_secret': STRAVA_CLIENT_SECRET,
        'code': req.code,
        'grant_type': 'authorization_code'
    }
    
    res = requests.post(url, data=payload)
    
    if res.status_code != 200:
        raise HTTPException(status_code=400, detail="Erro na troca de código Strava.")
    
    data = res.json()
    atleta = data.get('athlete', {})
    atleta_id = atleta.get('id')
    
    # Dicionário expandido para fácil leitura
    upsert_data = {
        "id": atleta_id,
        "access_token": data.get('access_token'),
        "refresh_token": data.get('refresh_token'),
        "expires_at": data.get('expires_at'),
        "nome": atleta.get('firstname'),
        "sobrenome": atleta.get('lastname'),
        "cidade": atleta.get('city'),
        "estado": atleta.get('state'),
        "genero": atleta.get('sex'),
        "peso": atleta.get('weight'),
        "foto_url": atleta.get('profile')
    }
    
    supabase.table("usuarios_strava").upsert(upsert_data).execute()
    
    return {"msg": "Login efetuado", "strava_id": atleta_id}

@app.get("/atleta/{strava_id}")
def get_atleta(strava_id: int):
    """Recupera a ficha técnica completa do atleta e seus diários."""
    if not supabase:
        raise HTTPException(status_code=500, detail="Banco de Dados Offline.")
    
    res = supabase.table("usuarios_strava").select("*").eq("id", strava_id).execute()
    
    if not res.data:
        raise HTTPException(status_code=404, detail="Atleta não cadastrado.")
    
    user = res.data[0]
    
    # Mapeamento de diários salvos (Memórias)
    diarios_map = {}
    try:
        res_diario = supabase.table("diario_treinos").select("*").eq("strava_id", str(strava_id)).execute()
        for d in res_diario.data:
            diarios_map[d['id_atividade_strava']] = d
    except Exception as e:
        print(f"Erro ao buscar diários: {e}")
        pass 
    
    # Objeto de resposta expandido e organizado
    resposta = {
        "perfil": {
            "nome": user.get('nome'),
            "sobrenome": user.get('sobrenome'),
            "idade": user.get('idade'),
            "altura": user.get('altura'),
            "peso": user.get('peso'),
            "foto_url": user.get('foto_url'),
            "cidade": user.get('cidade'),
            "fisiologia": user.get('fisiologia_json') or {},
            "equipamentos": user.get('equipamentos_json') or {},
            "clubes": user.get('clubes_json') or [],
            "data_criacao": user.get('created_at')
        },
        "historico_json": user.get('historico_json') or [],
        "trofeus_json": user.get('trofeus_json') or {},
        "ia_report_json": user.get('ia_report_json') or None,
        "diarios": diarios_map
    }
    
    return resposta

@app.put("/atleta/{strava_id}/biometria")
def update_biometria(strava_id: int, dados: dict):
    """Atualização rápida de dados corporais."""
    if not supabase:
        return {"err": "Banco de Dados Offline"}
        
    supabase.table("usuarios_strava").update({
        "altura": dados.get("altura"),
        "idade": dados.get("idade"),
        "peso": dados.get("peso")
    }).eq("id", strava_id).execute()
    
    return {"msg": "Biometria atualizada com sucesso."}

# =========================================================================
# 2. MOTOR DE SINCRONIZAÇÃO E ETL (PANDAS)
# =========================================================================

@app.post("/atleta/{strava_id}/sincronizar")
def sync_strava(strava_id: int):
    """Deep Sync: Puxa atividades, limpa dados e calcula métricas biomecânicas."""
    if not supabase:
        raise HTTPException(status_code=500, detail="Banco de Dados Offline.")
    
    res_user = supabase.table("usuarios_strava").select("refresh_token").eq("id", strava_id).execute()
    
    if not res_user.data:
        raise HTTPException(status_code=404, detail="Usuário não encontrado no sistema.")
    
    access_token = obter_token_fresco(res_user.data[0]['refresh_token'])
    
    if not access_token:
        raise HTTPException(status_code=401, detail="Sessão com o Strava expirou.")

    headers = {'Authorization': f'Bearer {access_token}'}
    atividades = []
    
    # Busca até 400 treinos recentes (2 páginas de 200) para cobrir bom período
    for page in [1, 2]:
        params = {'per_page': 200, 'page': page}
        res = requests.get('https://www.strava.com/api/v3/athlete/activities', headers=headers, params=params)
        
        if res.status_code != 200:
            break
            
        data = res.json()
        if not data:
            break
            
        atividades.extend(data)

    if not atividades:
        return {"historico_json": []}

    df = pd.DataFrame(atividades)
    
    # Filtro: Apenas Corrida, Caminhada e Trilhas
    df = df[df['type'].isin(['Run', 'Walk', 'Hike'])].copy()
    
    if df.empty:
        return {"historico_json": []}

    # Transformações base
    df['distancia_km'] = df['distance'] / 1000.0

    def calc_pace(linha):
        if linha['distancia_km'] == 0:
            return "00:00"
        pace_dec = (linha['moving_time'] / 60) / linha['distancia_km']
        minutos = int(pace_dec)
        segundos = int(round((pace_dec - minutos) * 60))
        return f"{minutos:02d}:{segundos:02d}"
        
    df['Pace_Medio'] = df.apply(calc_pace, axis=1)
    
    # Tratamento de Nulos Defensivo (Expandido para clareza e segurança)
    if 'average_heartrate' in df.columns:
        df['average_heartrate'] = df['average_heartrate'].fillna(0)
    else:
        df['average_heartrate'] = 0
        
    if 'max_heartrate' in df.columns:
        df['max_heartrate'] = df['max_heartrate'].fillna(0)
    else:
        df['max_heartrate'] = 0
        
    if 'average_cadence' in df.columns:
        df['Cadence_SPM'] = (df['average_cadence'].fillna(0) * 2).round().astype(int)
    else:
        df['Cadence_SPM'] = 0
        
    if 'total_elevation_gain' in df.columns:
        df['total_elevation_gain'] = df['total_elevation_gain'].fillna(0)
    else:
        df['total_elevation_gain'] = 0
    
    if 'elapsed_time' not in df.columns:
        df['elapsed_time'] = df['moving_time']
        
    if 'start_latlng' not in df.columns:
        df['start_latlng'] = None

    colunas_finais = [
        'id', 'type', 'workout_type', 'name', 'distancia_km', 'Pace_Medio', 
        'average_heartrate', 'max_heartrate', 'Cadence_SPM', 'total_elevation_gain', 
        'moving_time', 'elapsed_time', 'start_date_local', 'start_latlng'
    ]
    
    # Puxar dados extras do perfil (Equipamentos e Clubes)
    res_perfil = requests.get('https://www.strava.com/api/v3/athlete', headers=headers)
    equipamentos = {}
    clubes = []
    
    if res_perfil.status_code == 200:
        p_json = res_perfil.json()
        equipamentos = {
            "tenis": p_json.get("shoes", [])
        }
        for c in p_json.get("clubs", []):
            clubes.append({
                "nome": c.get("name"),
                "foto": c.get("profile")
            })

    # Serialização Segura via json puro do Pandas
    res_json = df[colunas_finais].to_json(orient='records', date_format='iso', force_ascii=False)
    dados_formatados = json.loads(res_json)

    # Gravação no Banco de Dados
    supabase.table("usuarios_strava").update({
        "historico_json": dados_formatados,
        "equipamentos_json": equipamentos,
        "clubes_json": clubes
    }).eq("id", strava_id).execute()

    return {"historico_json": dados_formatados}

# =========================================================================
# 3. MOTOR FISIOLÓGICO (ZONAS CARDÍACAS E PACE VDOT)
# =========================================================================

@app.post("/fisiologia/calcular-zonas/{strava_id}")
def calcular_zonas(strava_id: int, payload: ZonasFCPayload):
    """Calcula zonas de Frequência Cardíaca (Max, Karvonen ou Limiar)."""
    zonas = []
    metodo = payload.metodo

    if metodo == 'max' and payload.fc_max:
        f = payload.fc_max
        zonas = [
            {"id": 1, "nome": "Z1 - Recuperação", "desc": "50-60% Max", "min": int(f * 0.5), "max": int(f * 0.6), "tema": "cinza"},
            {"id": 2, "nome": "Z2 - Aeróbico", "desc": "60-70% Max", "min": int(f * 0.6) + 1, "max": int(f * 0.7), "tema": "azul"},
            {"id": 3, "nome": "Z3 - Tempo", "desc": "70-80% Max", "min": int(f * 0.7) + 1, "max": int(f * 0.8), "tema": "verde"},
            {"id": 4, "nome": "Z4 - Limiar", "desc": "80-90% Max", "min": int(f * 0.8) + 1, "max": int(f * 0.9), "tema": "laranja"},
            {"id": 5, "nome": "Z5 - Anaeróbico", "desc": "90-100% Max", "min": int(f * 0.9) + 1, "max": f, "tema": "vermelho"}
        ]
        
    elif metodo == 'karvonen' and payload.fc_max and payload.fc_repouso:
        f_max = payload.fc_max
        f_rep = payload.fc_repouso
        f_res = f_max - f_rep
        zonas = [
            {"id": 1, "nome": "Z1 - Recuperação", "desc": "50-60% Reserva", "min": int(f_res * 0.5 + f_rep), "max": int(f_res * 0.6 + f_rep), "tema": "cinza"},
            {"id": 2, "nome": "Z2 - Aeróbico", "desc": "60-70% Reserva", "min": int(f_res * 0.6 + f_rep) + 1, "max": int(f_res * 0.7 + f_rep), "tema": "azul"},
            {"id": 3, "nome": "Z3 - Tempo", "desc": "70-80% Reserva", "min": int(f_res * 0.7 + f_rep) + 1, "max": int(f_res * 0.8 + f_rep), "tema": "verde"},
            {"id": 4, "nome": "Z4 - Limiar", "desc": "80-90% Reserva", "min": int(f_res * 0.8 + f_rep) + 1, "max": int(f_res * 0.9 + f_rep), "tema": "laranja"},
            {"id": 5, "nome": "Z5 - Anaeróbico", "desc": "90-100% Reserva", "min": int(f_res * 0.9 + f_rep) + 1, "max": f_max, "tema": "vermelho"}
        ]
        
    elif metodo == 'limiar' and payload.fc_limiar:
        lthr = payload.fc_limiar
        zonas = [
            {"id": 1, "nome": "Z1 - Recuperação", "desc": "< 85% LTHR", "min": 0, "max": int(lthr * 0.85), "tema": "cinza"},
            {"id": 2, "nome": "Z2 - Base Aeróbica", "desc": "85-89% LTHR", "min": int(lthr * 0.85) + 1, "max": int(lthr * 0.89), "tema": "azul"},
            {"id": 3, "nome": "Z3 - Tempo", "desc": "90-94% LTHR", "min": int(lthr * 0.90), "max": int(lthr * 0.94), "tema": "verde"},
            {"id": 4, "nome": "Z4 - Limiar", "desc": "95-99% LTHR", "min": int(lthr * 0.95), "max": int(lthr * 0.99), "tema": "laranja"},
            {"id": 5, "nome": "Z5 - VO2 Máx", "desc": "100-106% LTHR", "min": lthr, "max": int(lthr * 1.06), "tema": "vermelho"}
        ]

    # Atualiza apenas as chaves necessárias no JSONB do Supabase
    res = supabase.table("usuarios_strava").select("fisiologia_json").eq("id", strava_id).execute()
    
    if res.data and res.data[0].get('fisiologia_json'):
        fisiologia_atual = res.data[0]['fisiologia_json']
    else:
        fisiologia_atual = {}
        
    fisiologia_atual.update({
        "metodo": metodo,
        "fc_max": payload.fc_max,
        "fc_repouso": payload.fc_repouso,
        "fc_limiar": payload.fc_limiar,
        "zonas": zonas
    })
    
    supabase.table("usuarios_strava").update({"fisiologia_json": fisiologia_atual}).eq("id", strava_id).execute()
    
    return {"msg": "Frequência Cardíaca calibrada com sucesso.", "zonas": zonas, "fisiologia_salva": fisiologia_atual}

@app.post("/fisiologia/salvar-zonas-pace/{strava_id}")
def salvar_zonas_pace(strava_id: int, payload: ZonasPacePayload):
    """Calcula os ritmos de treino (VDOT ou Limiar de Joe Friel)."""
    zonas_pace = []
    
    if payload.metodo == 'daniels' and payload.distancia_km and payload.tempo_segundos:
        # Jack Daniels VDOT Simplificado
        pace_prova_seg = payload.tempo_segundos / payload.distancia_km
        
        pace_E = pace_prova_seg * 1.25 
        pace_M = pace_prova_seg * 1.15 
        pace_T = pace_prova_seg * 1.05 
        pace_I = pace_prova_seg * 0.95 
        pace_R = pace_prova_seg * 0.88 
        
        zonas_pace = [
            {
                "id": 1, "nome": "E - Fácil", "desc": "Aquecimento / Longos", 
                "min": helper_formata_tempo(pace_E - 5), "max": helper_formata_tempo(pace_E + 5), "tema": "cinza"
            },
            {
                "id": 2, "nome": "M - Maratona", "desc": "Ritmo de Prova Alvo", 
                "min": helper_formata_tempo(pace_M - 5), "max": helper_formata_tempo(pace_M + 5), "tema": "azul"
            },
            {
                "id": 3, "nome": "T - Limiar", "desc": "Threshold / Cruzeiro", 
                "min": helper_formata_tempo(pace_T - 5), "max": helper_formata_tempo(pace_T + 5), "tema": "verde"
            },
            {
                "id": 4, "nome": "I - Intervalado", "desc": "Tiros Longos (VO2)", 
                "min": helper_formata_tempo(pace_I - 5), "max": helper_formata_tempo(pace_I + 5), "tema": "laranja"
            },
            {
                "id": 5, "nome": "R - Repetição", "desc": "Tiros Curtos (Pista)", 
                "min": helper_formata_tempo(pace_R - 5), "max": helper_formata_tempo(pace_R + 5), "tema": "vermelho"
            }
        ]
        
    elif payload.metodo == 'friel' and payload.pace_limiar:
        try:
            minutos, segundos = map(int, payload.pace_limiar.split(':'))
            pace_lthr_segundos = minutos * 60 + segundos
            
            zonas_pace = [
                {
                    "id": 1, "nome": "Z1 - Recuperação", "desc": "> 129% Pace Limiar", 
                    "min": helper_formata_tempo(pace_lthr_segundos * 1.29), "max": "99:59", "tema": "cinza"
                },
                {
                    "id": 2, "nome": "Z2 - Aeróbico", "desc": "114-129% LTHR Pace", 
                    "min": helper_formata_tempo(pace_lthr_segundos * 1.14), "max": helper_formata_tempo(pace_lthr_segundos * 1.29), "tema": "azul"
                },
                {
                    "id": 3, "nome": "Z3 - Tempo", "desc": "106-113% LTHR Pace", 
                    "min": helper_formata_tempo(pace_lthr_segundos * 1.06), "max": helper_formata_tempo(pace_lthr_segundos * 1.13), "tema": "verde"
                },
                {
                    "id": 4, "nome": "Z4 - Limiar", "desc": "99-105% LTHR Pace", 
                    "min": helper_formata_tempo(pace_lthr_segundos * 0.99), "max": helper_formata_tempo(pace_lthr_segundos * 1.05), "tema": "laranja"
                },
                {
                    "id": 5, "nome": "Z5 - Anaeróbico", "desc": "< 99% Pace Limiar", 
                    "min": "00:00", "max": helper_formata_tempo(pace_lthr_segundos * 0.99), "tema": "vermelho"
                }
            ]
        except Exception as e:
            pass # Ignora formatações erradas silenciosamente

    res = supabase.table("usuarios_strava").select("fisiologia_json").eq("id", strava_id).execute()
    
    if res.data and res.data[0].get('fisiologia_json'):
        fisiologia_atual = res.data[0]['fisiologia_json']
    else:
        fisiologia_atual = {}
        
    fisiologia_atual.update({
        "metodo_pace": payload.metodo,
        "dist_ref_pace": payload.distancia_km,
        "tempo_ref_pace": payload.tempo_segundos,
        "pace_limiar": payload.pace_limiar,
        "zonas_pace": zonas_pace
    })
    
    supabase.table("usuarios_strava").update({"fisiologia_json": fisiologia_atual}).eq("id", strava_id).execute()
    return {"msg": "Ritmos de treino calibrados.", "zonas_pace": zonas_pace, "fisiologia_salva": fisiologia_atual}

@app.get("/fisiologia/extrair-limiar/{strava_id}/{activity_id}")
def extrair_limiar(strava_id: int, activity_id: int):
    """Análise retroativa de prova para extração do Limiar de Lactato estimado."""
    res = supabase.table("usuarios_strava").select("historico_json").eq("id", strava_id).execute()
    historico = res.data[0].get('historico_json', []) if res.data else []
    
    treino_alvo = None
    for t in historico:
        if t['id'] == activity_id:
            treino_alvo = t
            break
            
    if not treino_alvo:
        raise HTTPException(status_code=404, detail="Treino não encontrado no histórico.")
        
    bpm_medio = treino_alvo.get('average_heartrate', 0)
    distancia = treino_alvo.get('distancia_km', 0)
    
    # Fator de correção simples dependendo da distância percorrida na prova
    fator_correcao = 1.0
    if distancia < 7:
        fator_correcao = 0.98
    elif distancia > 15:
        fator_correcao = 1.05
        
    limiar_calculado = int(bpm_medio * fator_correcao)
    
    return {
        "limiar_estimado": limiar_calculado,
        "nome_prova": treino_alvo.get('name'),
        "bpm_medio_real": bpm_medio,
        "fator_correcao": fator_correcao
    }

# =========================================================================
# 4. MASTER COACH (INTELIGÊNCIA ARTIFICIAL GEMINI)
# =========================================================================

@app.post("/ia/analise")
def gerar_analise_ia(payload: AnaliseIAPayload):
    """Varre a biometria e os últimos treinos para gerar feedback técnico."""
    res = supabase.table("usuarios_strava").select("*").eq("id", payload.strava_id).execute()
    
    if not res.data:
        raise HTTPException(status_code=404, detail="Atleta não encontrado.")
        
    user = res.data[0]
    historico = user.get("historico_json", [])
    
    if not historico:
        raise HTTPException(status_code=400, detail="Sem treinos para analisar.")
        
    # Isola os últimos 5 treinos de corrida para contexto da IA
    df = pd.DataFrame(historico)
    df_runs = df[df['type'] == 'Run'].head(5)
    
    # Extrai apenas colunas vitais para economizar tokens
    colunas_ia = ['name', 'distancia_km', 'Pace_Medio', 'average_heartrate', 'Cadence_SPM']
    dados_ia_json = df_runs[colunas_ia].to_json(orient='records', force_ascii=False)
    
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    prompt_completo = f"""
    Atue como o renomado treinador de corrida Jack Daniels. 
    Analise o seguinte atleta: Idade {user.get('idade')}, Altura {user.get('altura')}cm, Peso {user.get('peso')}kg. 
    
    Os treinos recentes deste atleta são: 
    {dados_ia_json}
    
    Sua missão é retornar ESTRITAMENTE um arquivo JSON válido, contendo exatamente três chaves:
    1. "diagnostico_geral": Um resumo objetivo em texto.
    2. "ponto_de_melhoria": Uma observação focada na relação entre cadência, pace e altura corporal.
    3. "nota_eficiencia": Um número inteiro de 0 a 10 avaliando o desempenho biomecânico.
    
    NÃO forneça formatação em Markdown (sem crases). Quero apenas o JSON limpo.
    """
    
    try:
        resposta_ia = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt_completo,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        
        analise_json = json.loads(resposta_ia.text)
        
        # Salva o relatório gerado na conta do usuário
        supabase.table("usuarios_strava").update({"ia_report_json": analise_json}).eq("id", payload.strava_id).execute()
        
        return analise_json
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"O Motor Gemini falhou: {str(e)}")

@app.post("/treinos/parse")
def parse_treino(payload: ParseTreinoPayload):
    """IA Parser: Transforma linguagem natural (ex: WhatsApp) em JSON Estruturado."""
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    prompt = f"""
    Transforme a seguinte instrução de treino livre em um JSON estruturado.
    Texto do Treinador: '{payload.texto_bruto}'
    
    O JSON deve conter:
    - descricao_limpa (string)
    - distancia_estimada_km (float)
    - blocos (uma lista de objetos contendo: tipo, repeticoes, distancia_metros, intensidade_alvo)
    """
    
    try:
        resposta_ia = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        
        estrutura_treino = json.loads(resposta_ia.text)
        
        novo_treino = {
            "data_treino": payload.data_treino,
            "texto_bruto": payload.texto_bruto,
            "estrutura_json": estrutura_treino
        }
        
        res = supabase.table("usuarios_strava").select("planilha_json").eq("id", payload.strava_id).execute()
        
        planilha_atual = []
        if res.data and res.data[0].get('planilha_json'):
            planilha_atual = res.data[0]['planilha_json']
            
        # Remove o treino anterior desse dia se já existir (Atualização)
        planilha_limpa = []
        for t in planilha_atual:
            if t.get('data_treino') != payload.data_treino:
                planilha_limpa.append(t)
                
        planilha_limpa.append(novo_treino)
        
        supabase.table("usuarios_strava").update({"planilha_json": planilha_limpa}).eq("id", payload.strava_id).execute()
        
        return {"msg": "Plano de treino interpretado.", "treino": novo_treino}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Falha ao interpretar texto: {str(e)}")

# =========================================================================
# 5. O GARIMPEIRO (SALA DE TROFÉUS E RECORDES)
# =========================================================================

@app.post("/trofeus/garimpar/{strava_id}")
def garimpar_trofeus(strava_id: int, payload: TrofeusPayload):
    """Varre o histórico em busca de Recordes Pessoais (RPs) por distância."""
    res = supabase.table("usuarios_strava").select("historico_json").eq("id", strava_id).execute()
    
    if not res.data or not res.data[0].get('historico_json'):
        return {"trofeus": {}, "analisados": 0, "msg": "Histórico vazio."}
        
    df = pd.DataFrame(res.data[0]['historico_json'])
    df_corridas = df[df['type'] == 'Run'].copy()
    
    # Filtro de Competição Oficial (Tags do Strava)
    if payload.somente_provas:
        df_corridas = df_corridas[df_corridas['workout_type'] == 1]
        
    if df_corridas.empty:
        return {"trofeus": {}, "analisados": 0, "msg": "Nenhuma corrida encontrada sob estas condições."}
    
    # Faixas de Detecção Inteligente (Tolerância para marcações de GPS)
    metas_distancia = {
        '1k': (0.9, 1.1),
        '5k': (4.8, 5.3),
        '10k': (9.8, 10.3),
        'Half Marathon': (20.8, 21.5),
        'Marathon': (41.8, 42.6)
    }
    
    trofeus_encontrados = {}
    
    for nome_meta, (dist_min, dist_max) in metas_distancia.items():
        # Sub-dataframe filtrando apenas corridas que caem nesta faixa de distância
        df_faixa = df_corridas[(df_corridas['distancia_km'] >= dist_min) & (df_corridas['distancia_km'] <= dist_max)].copy()
        
        if not df_faixa.empty:
            # Encontra a linha (treino) com o menor tempo em movimento
            indice_melhor = df_faixa['moving_time'].idxmin()
            melhor_treino = df_faixa.loc[indice_melhor]
            
            trofeus_encontrados[nome_meta] = {
                "nome_treino": melhor_treino['name'],
                "data": melhor_treino['start_date_local'],
                "tempo_segundos": int(melhor_treino['moving_time']),
                "tempo_formatado": helper_formata_tempo(melhor_treino['moving_time']),
                "distancia_exata": float(melhor_treino['distancia_km']),
                "fc_media": int(melhor_treino['average_heartrate']),
                "fc_maxima": int(melhor_treino['max_heartrate'])
            }
    
    supabase.table("usuarios_strava").update({"trofeus_json": trofeus_encontrados}).eq("id", strava_id).execute()
    
    return {"trofeus": trofeus_encontrados, "analisados": len(df_corridas)}

# =========================================================================
# 6. DIÁRIO DO CORREDOR E JUKEBOX (SPOTIFY)
# =========================================================================

def get_spotify_token():
    """Gera token temporário Client Credentials para o Spotify."""
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return None
        
    auth_str = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
    b64_auth_str = base64.b64encode(auth_str.encode()).decode()
    
    headers = {
        "Authorization": f"Basic {b64_auth_str}",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    
    data_payload = {"grant_type": "client_credentials"}
    
    res = requests.post("https://accounts.spotify.com/api/token", headers=headers, data=data_payload)
    
    if res.status_code == 200:
        return res.json().get("access_token")
    return None

@app.get("/musica/buscar")
def buscar_musica(q: str):
    """Endpoint do Jukebox: Busca trilhas sonoras na API Pública do Spotify."""
    token = get_spotify_token()
    
    if not token:
        raise HTTPException(status_code=500, detail="Chaves do Spotify ausentes no servidor.")
        
    headers = {"Authorization": f"Bearer {token}"}
    
    res = requests.get(f"https://api.spotify.com/v1/search?q={q}&type=track&limit=5", headers=headers)
    
    if res.status_code != 200:
        raise HTTPException(status_code=res.status_code, detail="O Spotify recusou a conexão.")
    
    tracks_data = res.json().get("tracks", {}).get("items", [])
    
    resultados_formatados = []
    for track in tracks_data:
        nome_artista = "Desconhecido"
        if track.get("artists") and len(track["artists"]) > 0:
            nome_artista = track["artists"][0]["name"]
            
        url_imagem = None
        if track.get("album") and track["album"].get("images") and len(track["album"]["images"]) > 0:
            url_imagem = track["album"]["images"][0]["url"]
            
        resultados_formatados.append({
            "id": track["id"],
            "nome": track["name"],
            "artista": nome_artista,
            "imagem": url_imagem
        })
        
    return {"resultados": resultados_formatados}

@app.post("/diario/salvar")
def salvar_diario(dados: DiarioPayload):
    """Sela a memória emocional, física e musical na tabela diario_treinos."""
    if not supabase:
        raise HTTPException(status_code=500, detail="Banco de dados não configurado.")
        
    try:
        payload_db = {
            "strava_id": str(dados.strava_id),
            "id_atividade_strava": dados.id_atividade_strava,
            "mood_emocional": dados.mood_emocional,
            "mood_fisico": dados.mood_fisico,
            "comentario": dados.comentario,
            "spotify_track_id": dados.spotify_track_id,
            "spotify_track_name": dados.spotify_track_name,
            "spotify_album_art": dados.spotify_album_art
        }
        
        supabase.table("diario_treinos").upsert(payload_db, on_conflict="id_atividade_strava").execute()
        
        return {"msg": "Diário e emoções do treino salvos com sucesso!"}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro interno de gravação SQL: {str(e)}")

# =========================================================================
# 7. CALENDÁRIO GERAL
# =========================================================================

@app.get("/calendario/{strava_id}")
def get_calendario(strava_id: int):
    """Retorna os treinos estruturados para renderização na planilha visual."""
    if not supabase:
        return {"treinos": []}
        
    res = supabase.table("usuarios_strava").select("planilha_json").eq("id", strava_id).execute()
    
    treinos_planejados = []
    if res.data and res.data[0].get('planilha_json'):
        treinos_planejados = res.data[0]['planilha_json']
        
    return {"treinos": treinos_planejados}
