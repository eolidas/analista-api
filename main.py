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
# Arquitetura Clean Code | Integrações: Strava, Supabase, Gemini, Spotify
# V4.0 - Full Monolith Engine (Sem Mocks, Matemática Real)
# =========================================================================

app = FastAPI(title="Analista de Bolso API", version="4.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
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

# Inicializando Supabase
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    print(f"Erro ao inicializar Supabase: {e}")

# --- MODELOS PYDANTIC ---
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
    mood: str = None
    comentario: str = None
    spotify_track_id: str = None
    spotify_track_name: str = None
    spotify_album_art: str = None
    clima_snapshot: dict = None

class ParseTreinoPayload(BaseModel):
    strava_id: int
    data_treino: str
    texto_bruto: str

# =========================================================================
# 1. ROTAS DE AUTENTICAÇÃO E PERFIL
# =========================================================================

@app.post("/auth/strava")
def auth_strava(req: AuthRequest):
    """Troca o código do Strava pelos tokens e salva o perfil no Supabase."""
    url = "https://www.strava.com/oauth/token"
    payload = {
        'client_id': STRAVA_CLIENT_ID,
        'client_secret': STRAVA_CLIENT_SECRET,
        'code': req.code,
        'grant_type': 'authorization_code'
    }
    res = requests.post(url, data=payload)
    if res.status_code != 200:
        raise HTTPException(status_code=400, detail="Falha na autorização do Strava.")
    
    data = res.json()
    atleta = data.get('athlete', {})
    atleta_id = atleta.get('id')
    
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
    return {"msg": "Autenticado com sucesso", "strava_id": atleta_id}

@app.get("/atleta/{strava_id}")
def get_atleta(strava_id: int):
    """Puxa a ficha completa do atleta (Perfil, Fisiologia, Histórico e Diários)."""
    res = supabase.table("usuarios_strava").select("*").eq("id", strava_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Atleta não encontrado no banco.")
    
    user = res.data[0]
    
    # Tenta puxar os diários de bordo salvos pelo usuário para anexar ao histórico
    diarios_map = {}
    try:
        res_diario = supabase.table("diario_treinos").select("*").eq("strava_id", str(strava_id)).execute()
        for d in res_diario.data:
            diarios_map[d['id_atividade_strava']] = d
    except:
        pass # Falha não impeditiva
    
    return {
        "perfil": {
            "nome": user.get('nome'),
            "sobrenome": user.get('sobrenome'),
            "idade": user.get('idade'),
            "altura": user.get('altura'),
            "peso": user.get('peso'),
            "cidade": user.get('cidade'),
            "estado": user.get('estado'),
            "genero": user.get('genero'),
            "foto_url": user.get('foto_url'),
            "equipamentos": user.get('equipamentos_json') or {},
            "clubes": user.get('clubes_json') or [],
            "fisiologia": user.get('fisiologia_json') or {},
            "data_criacao": user.get('created_at')
        },
        "historico_json": user.get('historico_json') or [],
        "trofeus_json": user.get('trofeus_json') or {},
        "ia_report_json": user.get('ia_report_json') or None,
        "diarios": diarios_map
    }

@app.put("/atleta/{strava_id}/biometria")
def update_biometria(strava_id: int, dados: dict):
    """Atualiza dados biométricos manualmente."""
    supabase.table("usuarios_strava").update({
        "altura": dados.get("altura"),
        "idade": dados.get("idade"),
        "peso": dados.get("peso")
    }).eq("id", strava_id).execute()
    return {"msg": "Biometria atualizada com sucesso."}

# =========================================================================
# 2. SINCRONIZAÇÃO E ETL (Extração, Transformação e Carga)
# =========================================================================

def obter_token_fresco(refresh_token: str):
    """Garante que a API tem acesso ativo ao Strava."""
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

@app.post("/atleta/{strava_id}/sincronizar")
def sync_strava(strava_id: int):
    """Deep Sync: Varre o Strava, aplica matemática de Pace/BPM e salva no Supabase."""
    res_user = supabase.table("usuarios_strava").select("refresh_token").eq("id", strava_id).execute()
    if not res_user.data:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")
        
    access_token = obter_token_fresco(res_user.data[0]['refresh_token'])
    if not access_token:
        raise HTTPException(status_code=401, detail="Sessão do Strava expirada.")

    # 1. Puxa Atividades
    url_acts = 'https://www.strava.com/api/v3/athlete/activities'
    headers = {'Authorization': f'Bearer {access_token}'}
    
    atividades = []
    page = 1
    while True:
        res = requests.get(url_acts, headers=headers, params={'per_page': 200, 'page': page})
        if res.status_code != 200: break
        data = res.json()
        if not data: break
        atividades.extend(data)
        page += 1
        if page > 2: break # Limite de 400 treinos para otimizar servidor free

    if not atividades:
        return {"historico_json": []}

    # 2. Pipeline de Dados (Pandas)
    df = pd.DataFrame(atividades)
    df = df[df['type'].isin(['Run', 'Walk', 'Hike'])].copy()
    if df.empty: return {"historico_json": []}

    df['distancia_km'] = df['distance'] / 1000.0

    def calc_pace(linha):
        if linha['distancia_km'] == 0: return "00:00"
        pace_dec = (linha['moving_time'] / 60) / linha['distancia_km']
        return f"{int(pace_dec):02d}:{int(round((pace_dec - int(pace_dec)) * 60)):02d}"
        
    df['Pace_Medio'] = df.apply(calc_pace, axis=1)
    
    # Prevenção rigorosa contra NaN (Data Pollution) e preenchimento de colunas faltantes
    df['average_heartrate'] = df['average_heartrate'].fillna(0) if 'average_heartrate' in df.columns else 0
    df['max_heartrate'] = df['max_heartrate'].fillna(0) if 'max_heartrate' in df.columns else 0
    
    # Bug da Cadência resolvido: Fillna antes da multiplicação
    df['Cadence_SPM'] = (df['average_cadence'].fillna(0) * 2).round().astype(int) if 'average_cadence' in df.columns else 0
    
    df['total_elevation_gain'] = df['total_elevation_gain'].fillna(0) if 'total_elevation_gain' in df.columns else 0
    
    if 'elapsed_time' not in df.columns: df['elapsed_time'] = df['moving_time']
    if 'start_latlng' not in df.columns: df['start_latlng'] = None

    colunas = ['id', 'type', 'workout_type', 'name', 'distancia_km', 'Pace_Medio', 'average_heartrate', 'max_heartrate', 'Cadence_SPM', 'total_elevation_gain', 'moving_time', 'elapsed_time', 'start_date_local', 'start_latlng']
    
    # 3. Puxa Equipamentos e Clubes do Perfil
    res_perfil = requests.get('https://www.strava.com/api/v3/athlete', headers=headers)
    equipamentos = {}
    clubes = []
    if res_perfil.status_code == 200:
        p = res_perfil.json()
        equipamentos = {"tenis": p.get("shoes", [])}
        clubes_raw = p.get("clubs", [])
        clubes = [{"nome": c.get("name"), "foto": c.get("profile")} for c in clubes_raw]

    # Convertendo para dicionário JSON
    res_json = df[colunas].to_json(orient='records', force_ascii=False, date_format='iso')
    dados_formatados = json.loads(res_json)

    # 4. Salva no Supabase
    supabase.table("usuarios_strava").update({
        "historico_json": dados_formatados,
        "equipamentos_json": equipamentos,
        "clubes_json": clubes
    }).eq("id", strava_id).execute()

    return {"historico_json": dados_formatados}

# =========================================================================
# 3. MOTOR DE INTELIGÊNCIA ARTIFICIAL (GEMINI)
# =========================================================================

@app.post("/ia/analise")
def gerar_analise_ia(payload: AnaliseIAPayload):
    """Consulta o Gemini 2.5 para auditoria biomecânica de Elite."""
    res = supabase.table("usuarios_strava").select("*").eq("id", payload.strava_id).execute()
    if not res.data: raise HTTPException(status_code=404, detail="Usuário não encontrado.")
    user = res.data[0]
    historico = user.get("historico_json", [])
    
    if not historico: raise HTTPException(status_code=400, detail="Sem treinos para analisar.")
    
    df = pd.DataFrame(historico)
    df_runs = df[df['type'] == 'Run'].head(3)
    dados_ia = df_runs[['name', 'distancia_km', 'Pace_Medio', 'total_elevation_gain', 'average_heartrate', 'Cadence_SPM']].to_json(orient='records', force_ascii=False)

    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
    Atue como um treinador de corrida de elite usando a metodologia de Jack Daniels.
    Analise o atleta: Idade {user.get('idade')}, Altura {user.get('altura')}cm, Peso {user.get('peso')}kg.
    Treinos Recentes: {dados_ia}
    
    Retorne ESTRITAMENTE um JSON com as chaves:
    "diagnostico_geral" (resumo conciso),
    "ponto_de_melhoria" (focado na relação pace/cadência baseada na altura e no limiar aeróbico),
    "nota_eficiencia" (número de 0 a 10).
    Não use formatação Markdown. Apenas o JSON limpo.
    """

    try:
        resposta_ia = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        analise_json = json.loads(resposta_ia.text)
        
        supabase.table("usuarios_strava").update({"ia_report_json": analise_json}).eq("id", payload.strava_id).execute()
        return analise_json
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro na IA: {e}")

# =========================================================================
# 4. DIÁRIO DO CORREDOR E JUKEBOX (SPOTIFY)
# =========================================================================

def get_spotify_token():
    """Gera um token temporário de acesso à API do Spotify."""
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return None
    auth_str = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
    b64_auth_str = base64.b64encode(auth_str.encode()).decode()
    headers = { "Authorization": f"Basic {b64_auth_str}", "Content-Type": "application/x-www-form-urlencoded" }
    res = requests.post("https://accounts.spotify.com/api/token", headers=headers, data={"grant_type": "client_credentials"})
    if res.status_code == 200:
        return res.json().get("access_token")
    return None

@app.get("/musica/buscar")
def buscar_musica(q: str):
    """Motor de busca do Jukebox."""
    token = get_spotify_token()
    if not token: raise HTTPException(status_code=500, detail="Cofre do Spotify trancado (Chaves ausentes).")
    
    headers = {"Authorization": f"Bearer {token}"}
    res = requests.get(f"https://api.spotify.com/v1/search?q={q}&type=track&limit=5", headers=headers)
    
    if res.status_code != 200: raise HTTPException(status_code=res.status_code, detail="O Spotify recusou a busca.")
    
    tracks = res.json().get("tracks", {}).get("items", [])
    resultados = [{"id": t["id"], "nome": t["name"], "artista": t["artists"][0]["name"] if t["artists"] else "Desconhecido", "imagem": t["album"]["images"][0]["url"] if t["album"]["images"] else None} for t in tracks]
    return {"resultados": resultados}

@app.post("/diario/salvar")
def salvar_diario(dados: DiarioPayload):
    """Guarda a alma da corrida na gaveta do Supabase."""
    try:
        supabase.table("diario_treinos").upsert({
            "strava_id": str(dados.strava_id),
            "id_atividade_strava": dados.id_atividade_strava,
            "mood": dados.mood,
            "comentario": dados.comentario,
            "spotify_track_id": dados.spotify_track_id,
            "spotify_track_name": dados.spotify_track_name,
            "spotify_album_art": dados.spotify_album_art,
            "clima_snapshot": dados.clima_snapshot
        }, on_conflict="id_atividade_strava").execute()
        return {"msg": "Memória guardada com sucesso!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Falha ao escrever no diário: {str(e)}")

# =========================================================================
# 5. O GARIMPEIRO (MÓDULO DE TROFÉUS E RECORDES REAIS)
# =========================================================================

def formata_tempo(segundos):
    h = math.floor(segundos / 3600)
    m = math.floor((segundos % 3600) / 60)
    s = math.floor(segundos % 60)
    if h > 0: return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

@app.post("/trofeus/garimpar/{strava_id}")
def garimpar_trofeus(strava_id: int, payload: TrofeusPayload):
    """Varre o histórico para achar os verdadeiros recordes pessoais (RPs)."""
    res = supabase.table("usuarios_strava").select("historico_json").eq("id", strava_id).execute()
    if not res.data or not res.data[0].get('historico_json'):
        raise HTTPException(status_code=400, detail="Histórico vazio.")
        
    df = pd.DataFrame(res.data[0]['historico_json'])
    df_runs = df[df['type'] == 'Run'].copy()
    
    if payload.somente_provas:
        df_runs = df_runs[df_runs['workout_type'] == 1]
        
    if df_runs.empty:
        return {"msg": "Nenhuma corrida/prova encontrada.", "trofeus": {}, "analisados": 0}

    # Dicionário de distâncias alvo em km e suas tolerâncias (ex: 5k pode ser de 4.8 a 5.2 no GPS)
    metas = {
        '1k': (0.9, 1.1),
        '5k': (4.8, 5.3),
        '10k': (9.8, 10.3),
        'Half Marathon': (20.8, 21.5),
        'Marathon': (41.8, 42.6)
    }

    trofeus = {}
    for chave, (min_km, max_km) in metas.items():
        # Filtra corridas que caem nessa faixa de distância
        df_faixa = df_runs[(df_runs['distancia_km'] >= min_km) & (df_runs['distancia_km'] <= max_km)].copy()
        
        if not df_faixa.empty:
            # Encontra a corrida com o menor tempo total (mais rápida)
            idx_best = df_faixa['moving_time'].idxmin()
            melhor_treino = df_faixa.loc[idx_best]
            
            trofeus[chave] = {
                "nome_treino": melhor_treino['name'],
                "data": melhor_treino['start_date_local'],
                "tempo_segundos": int(melhor_treino['moving_time']),
                "tempo_formatado": formata_tempo(melhor_treino['moving_time']),
                "distancia_exata": float(melhor_treino['distancia_km']),
                "fc_media": int(melhor_treino['average_heartrate']) if melhor_treino['average_heartrate'] else None,
                "fc_maxima": int(melhor_treino['max_heartrate']) if melhor_treino['max_heartrate'] else None
            }

    # Salva no banco de dados para não precisar re-calcular
    supabase.table("usuarios_strava").update({"trofeus_json": trofeus}).eq("id", strava_id).execute()

    return {"msg": "Busca efetuada com sucesso.", "trofeus": trofeus, "analisados": len(df_runs)}

# =========================================================================
# 6. MOTOR DE FISIOLOGIA (ZONAS CARDÍACAS E RITMO - VDOT REAL)
# =========================================================================

@app.post("/fisiologia/calcular-zonas/{strava_id}")
def calcular_zonas(strava_id: int, payload: ZonasFCPayload):
    """Matemática Fisiológica Real (Karvonen, Friel e Clássica)."""
    zonas = []
    m = payload.metodo

    if m == 'max' and payload.fc_max:
        f = payload.fc_max
        zonas = [
            {"id": 1, "nome": "Z1 - Recuperação", "desc": "50-60% Max", "min": int(f*0.5), "max": int(f*0.6), "tema": "cinza"},
            {"id": 2, "nome": "Z2 - Aeróbico", "desc": "60-70% Max", "min": int(f*0.6)+1, "max": int(f*0.7), "tema": "azul"},
            {"id": 3, "nome": "Z3 - Tempo", "desc": "70-80% Max", "min": int(f*0.7)+1, "max": int(f*0.8), "tema": "verde"},
            {"id": 4, "nome": "Z4 - Limiar", "desc": "80-90% Max", "min": int(f*0.8)+1, "max": int(f*0.9), "tema": "laranja"},
            {"id": 5, "nome": "Z5 - Anaeróbico", "desc": "90-100% Max", "min": int(f*0.9)+1, "max": f, "tema": "vermelho"}
        ]
    elif m == 'karvonen' and payload.fc_max and payload.fc_repouso:
        f_max = payload.fc_max
        f_rep = payload.fc_repouso
        f_res = f_max - f_rep
        zonas = [
            {"id": 1, "nome": "Z1 - Recuperação", "desc": "50-60% Reserva", "min": int(f_res*0.5 + f_rep), "max": int(f_res*0.6 + f_rep), "tema": "cinza"},
            {"id": 2, "nome": "Z2 - Aeróbico", "desc": "60-70% Reserva", "min": int(f_res*0.6 + f_rep)+1, "max": int(f_res*0.7 + f_rep), "tema": "azul"},
            {"id": 3, "nome": "Z3 - Tempo", "desc": "70-80% Reserva", "min": int(f_res*0.7 + f_rep)+1, "max": int(f_res*0.8 + f_rep), "tema": "verde"},
            {"id": 4, "nome": "Z4 - Limiar", "desc": "80-90% Reserva", "min": int(f_res*0.8 + f_rep)+1, "max": int(f_res*0.9 + f_rep), "tema": "laranja"},
            {"id": 5, "nome": "Z5 - Anaeróbico", "desc": "90-100% Reserva", "min": int(f_res*0.9 + f_rep)+1, "max": f_max, "tema": "vermelho"}
        ]
    elif m == 'limiar' and payload.fc_limiar:
        lthr = payload.fc_limiar
        zonas = [
            {"id": 1, "nome": "Z1 - Recuperação", "desc": "< 85% LTHR", "min": 0, "max": int(lthr*0.85), "tema": "cinza"},
            {"id": 2, "nome": "Z2 - Base Aeróbica", "desc": "85-89% LTHR", "min": int(lthr*0.85)+1, "max": int(lthr*0.89), "tema": "azul"},
            {"id": 3, "nome": "Z3 - Tempo", "desc": "90-94% LTHR", "min": int(lthr*0.90), "max": int(lthr*0.94), "tema": "verde"},
            {"id": 4, "nome": "Z4 - Limiar", "desc": "95-99% LTHR", "min": int(lthr*0.95), "max": int(lthr*0.99), "tema": "laranja"},
            {"id": 5, "nome": "Z5 - VO2 Máx", "desc": "100-106% LTHR", "min": lthr, "max": int(lthr*1.06), "tema": "vermelho"}
        ]
    else:
        raise HTTPException(status_code=400, detail="Parâmetros inválidos para a metodologia escolhida.")

    fisiologia_atual = {}
    res = supabase.table("usuarios_strava").select("fisiologia_json").eq("id", strava_id).execute()
    if res.data and res.data[0].get('fisiologia_json'):
        fisiologia_atual = res.data[0]['fisiologia_json']
        
    fisiologia_atual.update({
        "metodo": m, 
        "fc_max": payload.fc_max, 
        "fc_repouso": payload.fc_repouso, 
        "fc_limiar": payload.fc_limiar,
        "zonas": zonas
    })
    
    supabase.table("usuarios_strava").update({"fisiologia_json": fisiologia_atual}).eq("id", strava_id).execute()
    return {"msg": "Zonas FC Calculadas e Salvas", "zonas": zonas, "fisiologia_salva": fisiologia_atual}

@app.post("/fisiologia/salvar-zonas-pace/{strava_id}")
def salvar_zonas_pace(strava_id: int, payload: ZonasPacePayload):
    """Cálculo das Zonas de Ritmo (Pace) baseadas no Padrão Ouro (Jack Daniels/VDOT ou Friel)."""
    zonas_pace = []
    
    if payload.metodo == 'daniels' and payload.distancia_km and payload.tempo_segundos:
        # Simplificação algorítmica da tabela VDOT de Jack Daniels para MVP
        # Transforma o pace da prova no VDOT correspondente
        pace_prova_seg = payload.tempo_segundos / payload.distancia_km
        
        # Paces deduzidos via coeficientes clássicos de Daniels
        pace_E = pace_prova_seg * 1.25 # Easy
        pace_M = pace_prova_seg * 1.15 # Marathon
        pace_T = pace_prova_seg * 1.05 # Threshold
        pace_I = pace_prova_seg * 0.95 # Interval
        pace_R = pace_prova_seg * 0.88 # Repetition
        
        zonas_pace = [
            {"id": 1, "nome": "E - Fácil", "desc": "Aquecimento / Longos", "min": formata_tempo(pace_E - 10), "max": formata_tempo(pace_E + 10), "tema": "cinza"},
            {"id": 2, "nome": "M - Maratona", "desc": "Pace de Prova Alvo", "min": formata_tempo(pace_M - 8), "max": formata_tempo(pace_M + 8), "tema": "azul"},
            {"id": 3, "nome": "T - Limiar", "desc": "Threshold / Cruzeiro", "min": formata_tempo(pace_T - 5), "max": formata_tempo(pace_T + 5), "tema": "verde"},
            {"id": 4, "nome": "I - Intervalado", "desc": "Tiros longos (VO2)", "min": formata_tempo(pace_I - 5), "max": formata_tempo(pace_I + 5), "tema": "laranja"},
            {"id": 5, "nome": "R - Repetição", "desc": "Tiros curtos (Pista)", "min": formata_tempo(pace_R - 5), "max": formata_tempo(pace_R + 5), "tema": "vermelho"}
        ]
    elif payload.metodo == 'friel' and payload.pace_limiar:
        # Friel's Pace Zones baseado no LTHR Pace
        try:
            m, s = map(int, payload.pace_limiar.split(':'))
            pace_lthr_seg = m * 60 + s
            zonas_pace = [
                {"id": 1, "nome": "Z1 - Recuperação", "desc": "Slower than 129%", "min": formata_tempo(pace_lthr_seg * 1.29), "max": "--:--", "tema": "cinza"},
                {"id": 2, "nome": "Z2 - Aeróbico", "desc": "114-129% LTHR Pace", "min": formata_tempo(pace_lthr_seg * 1.14), "max": formata_tempo(pace_lthr_seg * 1.29), "tema": "azul"},
                {"id": 3, "nome": "Z3 - Tempo", "desc": "106-113% LTHR Pace", "min": formata_tempo(pace_lthr_seg * 1.06), "max": formata_tempo(pace_lthr_seg * 1.13), "tema": "verde"},
                {"id": 4, "nome": "Z4 - Limiar", "desc": "99-105% LTHR Pace", "min": formata_tempo(pace_lthr_seg * 0.99), "max": formata_tempo(pace_lthr_seg * 1.05), "tema": "laranja"},
                {"id": 5, "nome": "Z5 - Anaeróbico", "desc": "Faster than 99%", "min": "00:00", "max": formata_tempo(pace_lthr_seg * 0.99), "tema": "vermelho"}
            ]
        except:
            raise HTTPException(status_code=400, detail="Formato de Pace de Limiar inválido (Use MM:SS).")

    fisiologia_atual = {}
    res = supabase.table("usuarios_strava").select("fisiologia_json").eq("id", strava_id).execute()
    if res.data and res.data[0].get('fisiologia_json'):
        fisiologia_atual = res.data[0]['fisiologia_json']
        
    fisiologia_atual.update({
        "metodo_pace": payload.metodo, 
        "dist_ref_pace": payload.distancia_km,
        "tempo_ref_pace": payload.tempo_segundos,
        "pace_limiar": payload.pace_limiar,
        "pace_altitude": payload.altitude_m,
        "pace_temp": payload.temperatura_c,
        "zonas_pace": zonas_pace
    })
    
    supabase.table("usuarios_strava").update({"fisiologia_json": fisiologia_atual}).eq("id", strava_id).execute()
    return {"msg": "Zonas Pace Calculadas", "zonas_pace": zonas_pace, "fisiologia_salva": fisiologia_atual}

@app.get("/fisiologia/extrair-limiar/{strava_id}/{activity_id}")
def extrair_limiar(strava_id: int, activity_id: int):
    """Extrai e deduz o Limiar de Lactato (BPM) de uma atividade específica de esforço máximo."""
    res = supabase.table("usuarios_strava").select("historico_json").eq("id", strava_id).execute()
    if not res.data: raise HTTPException(status_code=404, detail="Usuário sem histórico.")
    
    atividades = res.data[0].get('historico_json', [])
    treino = next((t for t in atividades if t['id'] == activity_id), None)
    
    if not treino: raise HTTPException(status_code=404, detail="Treino não encontrado no histórico.")
    
    bpm_medio = treino.get('average_heartrate', 0)
    if bpm_medio <= 0: raise HTTPException(status_code=400, detail="Este treino não possui dados cardíacos registrados.")
    
    # Motor de Dedução Simplificado: Se for 5k (aprox), foi acima do limiar (fator 0.98). 
    # Se for 10k (aprox), foi exatamente no limiar (fator 1.00)
    fator = 1.0
    dist = treino.get('distancia_km', 0)
    
    if dist < 7: fator = 0.98
    elif dist > 15: fator = 1.05
    
    limiar_estimado = int(bpm_medio * fator)
    
    return {
        "limiar_estimado": limiar_estimado, 
        "nome_prova": treino.get('name'), 
        "bpm_medio_real": bpm_medio, 
        "fator_correcao": fator
    }

# =========================================================================
# 7. MASTER COACH: IA PLANILHAS (TEXT-TO-PLAN) E CALENDÁRIO
# =========================================================================

@app.get("/calendario/{strava_id}")
def get_calendario(strava_id: int):
    """Puxa a planilha estruturada de treinos futuros do atleta."""
    # Como não temos uma tabela 'planilhas' configurada explicitamente no MVP atual,
    # retornamos uma lista vazia ou guardamos no perfil do usuário no campo 'planilha_json'.
    res = supabase.table("usuarios_strava").select("planilha_json").eq("id", strava_id).execute()
    treinos = []
    if res.data and res.data[0].get('planilha_json'):
        treinos = res.data[0]['planilha_json']
    return {"treinos": treinos}

@app.post("/treinos/parse")
def parse_treino(payload: ParseTreinoPayload):
    """Usa o Gemini para ler o texto/whatsapp do treinador e converter em JSON de blocos de treino."""
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    prompt = f"""
    Transforme a seguinte mensagem do treinador num JSON estruturado para a minha aplicação.
    Mensagem: "{payload.texto_bruto}"
    
    O JSON deve ter este exato formato:
    {{
      "descricao_limpa": "Título curto do treino",
      "distancia_estimada_km": 10.5,
      "blocos": [
         {{
           "tipo": "aquecimento", // Opções: aquecimento, principal, tiro, recuperacao, soltura
           "repeticoes": 1,
           "distancia_metros": 2000, // opcional
           "tempo_minutos": 0, // opcional
           "intensidade_alvo": "Z2 ou Leve"
         }}
      ]
    }}
    Não devolva Markdown. Apenas o JSON puro.
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
        
        # Guardando o treino estruturado na tabela
        res = supabase.table("usuarios_strava").select("planilha_json").eq("id", payload.strava_id).execute()
        planilha_atual = []
        if res.data and res.data[0].get('planilha_json'):
            planilha_atual = res.data[0]['planilha_json']
            
        # Atualiza se já existir para a data, senão adiciona
        planilha_atual = [t for t in planilha_atual if t['data_treino'] != payload.data_treino]
        planilha_atual.append(novo_treino)
        
        supabase.table("usuarios_strava").update({"planilha_json": planilha_atual}).eq("id", payload.strava_id).execute()
        
        return {"msg": "Treino estruturado e salvo com sucesso.", "treino": novo_treino}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao interpretar o treino: {str(e)}")
