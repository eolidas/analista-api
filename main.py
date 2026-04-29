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
# Rollback de Segurança: Retorno à Versão Estável (Single Mood)
# =========================================================================

app = FastAPI(title="Analista de Bolso API", version="4.5.0-Stable")

# Configuração CRÍTICA de CORS: allow_credentials deve ser False com origins="*"
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=False, 
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def health_check():
    return {"status": "Motor FastAPI Online e Operante"}

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"⚠️ Alerta: Supabase Offline: {e}")

class AuthRequest(BaseModel):
    code: str

class ZonasFCPayload(BaseModel):
    metodo: str
    fc_max: int | None = None
    fc_repouso: int | None = None
    fc_limiar: int | None = None

class ZonasPacePayload(BaseModel):
    metodo: str
    distancia_km: float | None = None
    tempo_segundos: int | None = None
    pace_limiar: str | None = None
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

# ROLLBACK AQUI: Voltamos ao campo único 'mood' seguro.
class DiarioPayload(BaseModel):
    strava_id: str
    id_atividade_strava: int
    mood: str | None = None 
    comentario: str | None = None
    spotify_track_id: str | None = None
    spotify_track_name: str | None = None
    spotify_album_art: str | None = None

class ParseTreinoPayload(BaseModel):
    strava_id: int
    data_treino: str
    texto_bruto: str

def helper_formata_tempo(segundos):
    h = math.floor(segundos / 3600)
    m = math.floor((segundos % 3600) / 60)
    s = math.floor(segundos % 60)
    if h > 0: return f"{int(h):02d}:{int(m):02d}:{int(s):02d}"
    return f"{int(m):02d}:{int(s):02d}"

def obter_token_fresco(refresh_token: str):
    url = 'https://www.strava.com/oauth/token'
    payload = { 'client_id': STRAVA_CLIENT_ID, 'client_secret': STRAVA_CLIENT_SECRET, 'refresh_token': refresh_token, 'grant_type': 'refresh_token' }
    res = requests.post(url, data=payload)
    return res.json().get('access_token') if res.status_code == 200 else None

@app.post("/auth/strava")
def auth_strava(req: AuthRequest):
    if not supabase: raise HTTPException(status_code=500, detail="Supabase não configurado.")
    url = "https://www.strava.com/oauth/token"
    payload = { 'client_id': STRAVA_CLIENT_ID, 'client_secret': STRAVA_CLIENT_SECRET, 'code': req.code, 'grant_type': 'authorization_code' }
    res = requests.post(url, data=payload)
    if res.status_code != 200: raise HTTPException(status_code=400, detail="Erro na troca de código Strava.")
    data = res.json()
    atleta = data.get('athlete', {})
    atleta_id = atleta.get('id')
    upsert_data = {
        "id": atleta_id, "access_token": data.get('access_token'), "refresh_token": data.get('refresh_token'),
        "expires_at": data.get('expires_at'), "nome": atleta.get('firstname'), "sobrenome": atleta.get('lastname'),
        "cidade": atleta.get('city'), "estado": atleta.get('state'), "genero": atleta.get('sex'),
        "peso": atleta.get('weight'), "foto_url": atleta.get('profile')
    }
    supabase.table("usuarios_strava").upsert(upsert_data).execute()
    return {"msg": "Login efetuado", "strava_id": atleta_id}

@app.get("/atleta/{strava_id}")
def get_atleta(strava_id: int):
    if not supabase: raise HTTPException(status_code=500, detail="Banco Offline.")
    res = supabase.table("usuarios_strava").select("*").eq("id", strava_id).execute()
    if not res.data: raise HTTPException(status_code=404, detail="Atleta não cadastrado.")
    user = res.data[0]
    diarios_map = {}
    try:
        res_diario = supabase.table("diario_treinos").select("*").eq("strava_id", str(strava_id)).execute()
        for d in res_diario.data: diarios_map[d['id_atividade_strava']] = d
    except Exception: pass 
    
    return {
        "perfil": {
            "nome": user.get('nome'), "sobrenome": user.get('sobrenome'), "idade": user.get('idade'),
            "altura": user.get('altura'), "peso": user.get('peso'), "foto_url": user.get('foto_url'),
            "cidade": user.get('cidade'), "fisiologia": user.get('fisiologia_json') or {},
            "equipamentos": user.get('equipamentos_json') or {}, "clubes": user.get('clubes_json') or [],
            "data_criacao": user.get('created_at')
        },
        "historico_json": user.get('historico_json') or [], "trofeus_json": user.get('trofeus_json') or {},
        "ia_report_json": user.get('ia_report_json') or None, "diarios": diarios_map
    }

@app.put("/atleta/{strava_id}/biometria")
def update_biometria(strava_id: int, dados: dict):
    if not supabase: return {"err": "Offline"}
    supabase.table("usuarios_strava").update({"altura": dados.get("altura"), "idade": dados.get("idade"), "peso": dados.get("peso")}).eq("id", strava_id).execute()
    return {"msg": "Biometria atualizada."}

@app.post("/atleta/{strava_id}/sincronizar")
def sync_strava(strava_id: int):
    if not supabase: raise HTTPException(status_code=500, detail="Offline.")
    res_user = supabase.table("usuarios_strava").select("refresh_token").eq("id", strava_id).execute()
    if not res_user.data: raise HTTPException(status_code=404, detail="Usuário não encontrado.")
    access_token = obter_token_fresco(res_user.data[0]['refresh_token'])
    if not access_token: raise HTTPException(status_code=401, detail="Sessão expirou.")

    headers = {'Authorization': f'Bearer {access_token}'}
    atividades = []
    for page in [1, 2]:
        res = requests.get('https://www.strava.com/api/v3/athlete/activities', headers=headers, params={'per_page': 200, 'page': page})
        if res.status_code != 200: break
        data = res.json()
        if not data: break
        atividades.extend(data)

    if not atividades: return {"historico_json": []}
    df = pd.DataFrame(atividades)
    df = df[df['type'].isin(['Run', 'Walk', 'Hike'])].copy()
    if df.empty: return {"historico_json": []}

    df['distancia_km'] = df['distance'] / 1000.0
    def calc_pace(linha):
        if linha['distancia_km'] == 0: return "00:00"
        p = (linha['moving_time'] / 60) / linha['distancia_km']
        return f"{int(p):02d}:{int(round((p - int(p)) * 60)):02d}"
        
    df['Pace_Medio'] = df.apply(calc_pace, axis=1)
    df['average_heartrate'] = df['average_heartrate'].fillna(0) if 'average_heartrate' in df.columns else 0
    df['max_heartrate'] = df['max_heartrate'].fillna(0) if 'max_heartrate' in df.columns else 0
    df['Cadence_SPM'] = (df['average_cadence'].fillna(0) * 2).round().astype(int) if 'average_cadence' in df.columns else 0
    df['total_elevation_gain'] = df['total_elevation_gain'].fillna(0) if 'total_elevation_gain' in df.columns else 0
    if 'elapsed_time' not in df.columns: df['elapsed_time'] = df['moving_time']
    if 'start_latlng' not in df.columns: df['start_latlng'] = None

    colunas = ['id', 'type', 'workout_type', 'name', 'distancia_km', 'Pace_Medio', 'average_heartrate', 'max_heartrate', 'Cadence_SPM', 'total_elevation_gain', 'moving_time', 'elapsed_time', 'start_date_local', 'start_latlng']
    
    res_perfil = requests.get('https://www.strava.com/api/v3/athlete', headers=headers)
    equipamentos, clubes = {}, []
    if res_perfil.status_code == 200:
        p_json = res_perfil.json()
        equipamentos = {"tenis": p_json.get("shoes", [])}
        for c in p_json.get("clubs", []): clubes.append({"nome": c.get("name"), "foto": c.get("profile")})

    dados_formatados = json.loads(df[colunas].to_json(orient='records', date_format='iso', force_ascii=False))
    supabase.table("usuarios_strava").update({"historico_json": dados_formatados, "equipamentos_json": equipamentos, "clubes_json": clubes}).eq("id", strava_id).execute()
    return {"historico_json": dados_formatados}

@app.post("/fisiologia/calcular-zonas/{strava_id}")
def calcular_zonas(strava_id: int, payload: ZonasFCPayload):
    zonas, m = [], payload.metodo
    if m == 'max' and payload.fc_max:
        f = payload.fc_max
        zonas = [{"id": 1, "nome": "Z1 - Recuperação", "desc": "50-60% Max", "min": int(f*0.5), "max": int(f*0.6), "tema": "cinza"}, {"id": 2, "nome": "Z2 - Aeróbico", "desc": "60-70% Max", "min": int(f*0.6)+1, "max": int(f*0.7), "tema": "azul"}, {"id": 3, "nome": "Z3 - Tempo", "desc": "70-80% Max", "min": int(f*0.7)+1, "max": int(f*0.8), "tema": "verde"}, {"id": 4, "nome": "Z4 - Limiar", "desc": "80-90% Max", "min": int(f*0.8)+1, "max": int(f*0.9), "tema": "laranja"}, {"id": 5, "nome": "Z5 - Anaeróbico", "desc": "90-100% Max", "min": int(f*0.9)+1, "max": f, "tema": "vermelho"}]
    elif m == 'karvonen' and payload.fc_max and payload.fc_repouso:
        f_res, f_rep = payload.fc_max - payload.fc_repouso, payload.fc_repouso
        zonas = [{"id": 1, "nome": "Z1 - Recuperação", "desc": "50-60% Reserva", "min": int(f_res*0.5+f_rep), "max": int(f_res*0.6+f_rep), "tema": "cinza"}, {"id": 2, "nome": "Z2 - Aeróbico", "desc": "60-70% Reserva", "min": int(f_res*0.6+f_rep)+1, "max": int(f_res*0.7+f_rep), "tema": "azul"}, {"id": 3, "nome": "Z3 - Tempo", "desc": "70-80% Reserva", "min": int(f_res*0.7+f_rep)+1, "max": int(f_res*0.8+f_rep), "tema": "verde"}, {"id": 4, "nome": "Z4 - Limiar", "desc": "80-90% Reserva", "min": int(f_res*0.8+f_rep)+1, "max": int(f_res*0.9+f_rep), "tema": "laranja"}, {"id": 5, "nome": "Z5 - Anaeróbico", "desc": "90-100% Reserva", "min": int(f_res*0.9+f_rep)+1, "max": payload.fc_max, "tema": "vermelho"}]
    elif m == 'limiar' and payload.fc_limiar:
        l = payload.fc_limiar
        zonas = [{"id": 1, "nome": "Z1 - Recuperação", "desc": "< 85% LTHR", "min": 0, "max": int(l*0.85), "tema": "cinza"}, {"id": 2, "nome": "Z2 - Base Aeróbica", "desc": "85-89% LTHR", "min": int(l*0.85)+1, "max": int(l*0.89), "tema": "azul"}, {"id": 3, "nome": "Z3 - Tempo", "desc": "90-94% LTHR", "min": int(l*0.90), "max": int(l*0.94), "tema": "verde"}, {"id": 4, "nome": "Z4 - Limiar", "desc": "95-99% LTHR", "min": int(l*0.95), "max": int(l*0.99), "tema": "laranja"}, {"id": 5, "nome": "Z5 - VO2 Máx", "desc": "100-106% LTHR", "min": l, "max": int(l*1.06), "tema": "vermelho"}]

    res = supabase.table("usuarios_strava").select("fisiologia_json").eq("id", strava_id).execute()
    fis = res.data[0]['fisiologia_json'] if res.data and res.data[0].get('fisiologia_json') else {}
    fis.update({ "metodo": m, "fc_max": payload.fc_max, "fc_repouso": payload.fc_repouso, "fc_limiar": payload.fc_limiar, "zonas": zonas })
    supabase.table("usuarios_strava").update({"fisiologia_json": fis}).eq("id", strava_id).execute()
    return {"msg": "Zonas FC calibradas.", "zonas": zonas, "fisiologia_salva": fis}

@app.post("/fisiologia/salvar-zonas-pace/{strava_id}")
def salvar_zonas_pace(strava_id: int, payload: ZonasPacePayload):
    zp = []
    if payload.metodo == 'daniels' and payload.distancia_km and payload.tempo_segundos:
        p_seg = payload.tempo_segundos / payload.distancia_km
        zp = [{"id": 1, "nome": "E - Fácil", "desc": "Aquecimento", "min": helper_formata_tempo(p_seg*1.25-5), "max": helper_formata_tempo(p_seg*1.25+5), "tema": "cinza"}, {"id": 2, "nome": "M - Maratona", "desc": "Prova Alvo", "min": helper_formata_tempo(p_seg*1.15-5), "max": helper_formata_tempo(p_seg*1.15+5), "tema": "azul"}, {"id": 3, "nome": "T - Limiar", "desc": "Cruzeiro", "min": helper_formata_tempo(p_seg*1.05-5), "max": helper_formata_tempo(p_seg*1.05+5), "tema": "verde"}, {"id": 4, "nome": "I - Intervalado", "desc": "Tiros", "min": helper_formata_tempo(p_seg*0.95-5), "max": helper_formata_tempo(p_seg*0.95+5), "tema": "laranja"}, {"id": 5, "nome": "R - Repetição", "desc": "Pista", "min": helper_formata_tempo(p_seg*0.88-5), "max": helper_formata_tempo(p_seg*0.88+5), "tema": "vermelho"}]
    
    res = supabase.table("usuarios_strava").select("fisiologia_json").eq("id", strava_id).execute()
    fis = res.data[0]['fisiologia_json'] if res.data and res.data[0].get('fisiologia_json') else {}
    fis.update({ "metodo_pace": payload.metodo, "dist_ref_pace": payload.distancia_km, "tempo_ref_pace": payload.tempo_segundos, "pace_limiar": payload.pace_limiar, "zonas_pace": zp })
    supabase.table("usuarios_strava").update({"fisiologia_json": fis}).eq("id", strava_id).execute()
    return {"zonas_pace": zp, "fisiologia_salva": fis}

@app.get("/fisiologia/extrair-limiar/{strava_id}/{activity_id}")
def extrair_limiar(strava_id: int, activity_id: int):
    res = supabase.table("usuarios_strava").select("historico_json").eq("id", strava_id).execute()
    t = next((x for x in (res.data[0].get('historico_json') or []) if x['id'] == activity_id), None)
    if not t: raise HTTPException(status_code=404, detail="Treino não achado.")
    bpm, dist = t.get('average_heartrate', 0), t.get('distancia_km', 0)
    fat = 0.98 if dist < 7 else (1.05 if dist > 15 else 1.0)
    return { "limiar_estimado": int(bpm * fat), "nome_prova": t.get('name'), "bpm_medio_real": bpm, "fator_correcao": fat }

@app.post("/ia/analise")
def gerar_analise_ia(payload: AnaliseIAPayload):
    res = supabase.table("usuarios_strava").select("*").eq("id", payload.strava_id).execute()
    if not res.data: raise HTTPException(status_code=404, detail="Atleta off.")
    df_runs = pd.DataFrame(res.data[0].get("historico_json", []))
    df_runs = df_runs[df_runs['type'] == 'Run'].head(5)
    dados_ia = df_runs[['name', 'distancia_km', 'Pace_Medio', 'average_heartrate', 'Cadence_SPM']].to_json(orient='records', force_ascii=False)
    
    prompt = f"Treinador Jack Daniels. Atleta: Idade {res.data[0].get('idade')}, Altura {res.data[0].get('altura')}cm, Peso {res.data[0].get('peso')}kg. Treinos recentes: {dados_ia}. Retorne JSON com: 'diagnostico_geral', 'ponto_de_melhoria' e 'nota_eficiencia' (0-10)."
    try:
        r = genai.Client(api_key=GEMINI_API_KEY).models.generate_content(model='gemini-2.5-flash', contents=prompt, config=types.GenerateContentConfig(response_mime_type="application/json"))
        analise = json.loads(r.text)
        supabase.table("usuarios_strava").update({"ia_report_json": analise}).eq("id", payload.strava_id).execute()
        return analise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/treinos/parse")
def parse_treino(payload: ParseTreinoPayload):
    prompt = f"JSON: '{payload.texto_bruto}'. Chaves: descricao_limpa, distancia_estimada_km, blocos (tipo, repeticoes, distancia_metros, intensidade_alvo)."
    try:
        r = genai.Client(api_key=GEMINI_API_KEY).models.generate_content(model='gemini-2.5-flash', contents=prompt, config=types.GenerateContentConfig(response_mime_type="application/json"))
        novo = { "data_treino": payload.data_treino, "texto_bruto": payload.texto_bruto, "estrutura_json": json.loads(r.text) }
        res = supabase.table("usuarios_strava").select("planilha_json").eq("id", payload.strava_id).execute()
        plan = [t for t in (res.data[0].get('planilha_json') or []) if t.get('data_treino') != payload.data_treino]
        plan.append(novo)
        supabase.table("usuarios_strava").update({"planilha_json": plan}).eq("id", payload.strava_id).execute()
        return {"msg": "Ok", "treino": novo}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/trofeus/garimpar/{strava_id}")
def garimpar_trofeus(strava_id: int, payload: TrofeusPayload):
    res = supabase.table("usuarios_strava").select("historico_json").eq("id", strava_id).execute()
    df = pd.DataFrame(res.data[0].get('historico_json') or [])
    if df.empty: return {"trofeus": {}, "analisados": 0}
    df = df[df['type'] == 'Run']
    if payload.somente_provas: df = df[df['workout_type'] == 1]
    
    metas, trofeus = {'1k': (0.9, 1.1), '5k': (4.8, 5.3), '10k': (9.8, 10.3), 'Half Marathon': (20.8, 21.5), 'Marathon': (41.8, 42.6)}, {}
    for nome, (d_min, d_max) in metas.items():
        sub = df[(df['distancia_km'] >= d_min) & (df['distancia_km'] <= d_max)]
        if not sub.empty:
            best = sub.loc[sub['moving_time'].idxmin()]
            trofeus[nome] = { "nome_treino": best['name'], "data": best['start_date_local'], "tempo_segundos": int(best['moving_time']), "tempo_formatado": helper_formata_tempo(best['moving_time']), "distancia_exata": float(best['distancia_km']), "fc_media": int(best.get('average_heartrate', 0)), "fc_maxima": int(best.get('max_heartrate', 0)) }
            
    supabase.table("usuarios_strava").update({"trofeus_json": trofeus}).eq("id", strava_id).execute()
    return {"trofeus": trofeus, "analisados": len(df)}

def get_spotify_token():
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET: return None
    auth = base64.b64encode(f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()).decode()
    r = requests.post("https://accounts.spotify.com/api/token", headers={"Authorization": f"Basic {auth}"}, data={"grant_type": "client_credentials"})
    return r.json().get("access_token") if r.status_code == 200 else None

@app.get("/musica/buscar")
def buscar_musica(q: str):
    t = get_spotify_token()
    if not t: raise HTTPException(status_code=500, detail="Sem chaves Spotify.")
    r = requests.get(f"https://api.spotify.com/v1/search?q={q}&type=track&limit=5", headers={"Authorization": f"Bearer {t}"})
    if r.status_code != 200: raise HTTPException(status_code=r.status_code, detail="Recusa Spotify.")
    return {"resultados": [{"id": x["id"], "nome": x["name"], "artista": x["artists"][0]["name"] if x["artists"] else "Uk", "imagem": x["album"]["images"][0]["url"] if x["album"]["images"] else None} for x in r.json().get("tracks", {}).get("items", [])]}

# ROLLBACK AQUI: Voltamos à lógica de salvar usando apenas o campo 'mood' original
@app.post("/diario/salvar")
def salvar_diario(dados: DiarioPayload):
    if not supabase: raise HTTPException(status_code=500, detail="Offline")
    try:
        supabase.table("diario_treinos").upsert({
            "strava_id": str(dados.strava_id), "id_atividade_strava": dados.id_atividade_strava,
            "mood": dados.mood, "comentario": dados.comentario,
            "spotify_track_id": dados.spotify_track_id, "spotify_track_name": dados.spotify_track_name, "spotify_album_art": dados.spotify_album_art
        }, on_conflict="id_atividade_strava").execute()
        return {"msg": "Diário salvo com sucesso!"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/calendario/{strava_id}")
def get_calendario(strava_id: int):
    if not supabase: return {"treinos": []}
    res = supabase.table("usuarios_strava").select("planilha_json").eq("id", strava_id).execute()
    return {"treinos": res.data[0]['planilha_json'] if res.data and res.data[0].get('planilha_json') else []}
