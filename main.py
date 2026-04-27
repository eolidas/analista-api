import os
import json
import requests
import pandas as pd
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
from google import genai
from google.genai import types
from supabase import create_client, Client

# ==========================================
# MOTOR ANALISTA DE BOLSO - BACKEND (PRODUÇÃO)
# Versão: 5.1.0 - Módulo Master Coach (Read/Write)
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

class BiometriaRequest(BaseModel):
    peso: Optional[float] = None
    altura: Optional[float] = None
    idade: Optional[int] = None

class TrofeusRequest(BaseModel):
    somente_provas: bool

# --- Módulos Fisiológicos ---
class ConfigZonasFC(BaseModel):
    metodo: str  # 'max', 'karvonen', 'limiar'
    fc_max: Optional[int] = None
    fc_repouso: Optional[int] = None
    fc_limiar: Optional[int] = None

class ConfigZonasPace(BaseModel):
    metodo: str  # 'daniels', 'friel'
    distancia_km: Optional[float] = None
    tempo_segundos: Optional[int] = None
    pace_limiar: Optional[str] = None # Ex: "05:00"
    altitude_m: Optional[float] = None
    temperatura_c: Optional[float] = None

class ExtrairLimiarMultiRequest(BaseModel):
    activities: list[int]
    compensar_temp: bool = True
    compensar_alt: bool = True

# --- Master Coach (Text-to-Plan) ---
class ParseTreinoRequest(BaseModel):
    strava_id: int
    data_treino: str # Formato YYYY-MM-DD
    texto_bruto: str
    ciclo_id: Optional[str] = None

# ==========================================
# ⚙️ FUNÇÕES DE ENGENHARIA DE DADOS E CONVERSÃO
# ==========================================

def atualizar_token_strava(refresh_token: str) -> str:
    url = 'https://www.strava.com/oauth/token'
    payload = {'client_id': STRAVA_CLIENT_ID, 'client_secret': STRAVA_CLIENT_SECRET, 'refresh_token': refresh_token, 'grant_type': 'refresh_token'}
    res = requests.post(url, data=payload)
    if res.status_code == 200:
        return res.json().get('access_token')
    raise HTTPException(status_code=401, detail="Sessão Strava expirada. Faça login novamente no PWA.")

def formatar_atividades_para_banco(lista_bruta):
    if not lista_bruta: return []
    df = pd.DataFrame(lista_bruta)
    
    df = df[df['type'].isin(['Run', 'Walk'])].copy()
    if df.empty: return []

    df['distancia_km'] = df['distance'] / 1000.0
    df['workout_type'] = df['workout_type'].fillna(0).astype(int)
    
    def calc_pace_mm_ss(linha):
        if linha['distancia_km'] == 0: return "00:00"
        pace_dec = (linha['moving_time'] / 60) / linha['distancia_km']
        return f"{int(pace_dec):02d}:{int(round((pace_dec - int(pace_dec)) * 60)):02d}"
        
    df['Pace_Medio'] = df.apply(calc_pace_mm_ss, axis=1)
    
    df['average_heartrate'] = df['average_heartrate'].fillna(0) if 'average_heartrate' in df.columns else 0
    df['max_heartrate'] = df['max_heartrate'].fillna(0) if 'max_heartrate' in df.columns else 0
    df['Cadence_SPM'] = df['average_cadence'] * 2 if 'average_cadence' in df.columns else 0
    df['total_elevation_gain'] = df['total_elevation_gain'].fillna(0) if 'total_elevation_gain' in df.columns else 0
    
    if 'elapsed_time' not in df.columns:
        df['elapsed_time'] = df['moving_time']
        
    if 'start_latlng' not in df.columns:
        df['start_latlng'] = None
        
    colunas_finais = ['id', 'type', 'workout_type', 'name', 'distancia_km', 'Pace_Medio', 'average_heartrate', 'max_heartrate', 'total_elevation_gain', 'moving_time', 'start_date_local', 'elapsed_time', 'start_latlng']
        
    res_json = df[colunas_finais].to_json(orient='records', force_ascii=False, date_format='iso')
    return json.loads(res_json)

def construir_perfil_seguro(dados_db: dict) -> dict:
    equip = dados_db.get("equipamentos")
    if not isinstance(equip, dict):
        equip = {"tenis": [], "bicicletas": []}
    
    return {
        "nome": dados_db.get("nome") or "Atleta",
        "sobrenome": dados_db.get("sobrenome") or "",
        "foto_url": dados_db.get("foto_url") or "",
        "peso": dados_db.get("peso"),
        "altura": dados_db.get("altura"),
        "idade": dados_db.get("idade"),
        "cidade": dados_db.get("cidade") or "",
        "estado": dados_db.get("estado") or "",
        "genero": dados_db.get("genero") or "",
        "data_criacao": dados_db.get("data_criacao") or "",
        "clubes": dados_db.get("clubes") or [],
        "equipamentos": equip,
        "fisiologia": dados_db.get("fisiologia_json") or {} 
    }

def seg_to_pace_str(seg):
    if seg <= 0: return "00:00"
    m = int(seg // 60)
    s = int(round(seg % 60))
    if s == 60:
        m += 1
        s = 0
    return f"{m:02d}:{s:02d}"

def pace_str_to_seg(pace_str):
    if not pace_str or ':' not in pace_str: return 0
    partes = pace_str.split(':')
    if len(partes) == 2:
        m, s = map(int, partes)
        return m * 60 + s
    elif len(partes) == 3:
        h, m, s = map(int, partes)
        return h * 3600 + m * 60 + s
    return 0

# ==========================================
# 🌐 ROTAS DA API - OAUTH & SINCRONIZAÇÃO
# ==========================================

@app.get("/")
def health_check():
    return {"status": "Motor V8 Operante 🚀", "version": "5.1.0"}

@app.post("/auth/strava")
def autenticar_usuario(requisicao: StravaAuthRequest):
    url_token = 'https://www.strava.com/oauth/token'
    payload = {'client_id': STRAVA_CLIENT_ID, 'client_secret': STRAVA_CLIENT_SECRET, 'code': requisicao.code, 'grant_type': 'authorization_code'}
    res = requests.post(url_token, data=payload)
    if res.status_code != 200: raise HTTPException(status_code=400, detail="Código OAuth inválido.")
        
    token_data = res.json()
    access_token = token_data.get('access_token')
    atleta_resumo = token_data.get('athlete', {})
    atleta_id = atleta_resumo.get('id')
    
    headers = {'Authorization': f'Bearer {access_token}'}
    res_perfil = requests.get('https://www.strava.com/api/v3/athlete', headers=headers)
    atleta = res_perfil.json() if res_perfil.status_code == 200 else atleta_resumo
    
    equipamentos = {"tenis": atleta.get('shoes') or [], "bicicletas": atleta.get('bikes') or []}
    clubes = [{"nome": c.get("name"), "foto": c.get("profile")} for c in (atleta.get('clubs') or [])]
    
    upsert_data = {
        "id": atleta_id,
        "access_token": access_token,
        "refresh_token": token_data.get('refresh_token'),
        "nome": atleta.get('firstname'),
        "sobrenome": atleta.get('lastname'),
        "foto_url": atleta.get('profile'),
        "peso": atleta.get('weight'),
        "cidade": atleta.get('city'),
        "estado": atleta.get('state'),
        "genero": atleta.get('sex'),
        "data_criacao": atleta.get('created_at'),
        "clubes": clubes,
        "equipamentos": equipamentos
    }
    supabase.table("usuarios_strava").upsert(upsert_data).execute()
    return {"status": "success", "strava_id": atleta_id}

@app.get("/atleta/{strava_id}")
def obter_ficha_atleta(strava_id: int):
    res_db = supabase.table("usuarios_strava").select("*").eq("id", strava_id).execute()
    if not res_db.data: raise HTTPException(status_code=404, detail="Atleta não encontrado.")
    
    atleta = res_db.data[0]
    return {
        "perfil": construir_perfil_seguro(atleta),
        "historico_json": atleta.get("historico_json") or [],
        "ia_report_json": atleta.get("ia_report_json"),
        "trofeus_json": atleta.get("trofeus_json") or {}
    }

@app.post("/atleta/{strava_id}/sincronizar")
def sincronizar_e_atualizar(strava_id: int):
    res_db = supabase.table("usuarios_strava").select("*").eq("id", strava_id).execute()
    atleta_db = res_db.data[0]
    token_fresco = atualizar_token_strava(atleta_db['refresh_token'])
    headers = {'Authorization': f'Bearer {token_fresco}'}
    
    res_perfil = requests.get('https://www.strava.com/api/v3/athlete', headers=headers)
    if res_perfil.status_code == 200:
        s = res_perfil.json()
        equipamentos = {"tenis": s.get('shoes') or [], "bicicletas": s.get('bikes') or []}
        clubes = [{"nome": c.get("name"), "foto": c.get("profile")} for c in (s.get('clubs') or [])]
        perfil_upd = {"equipamentos": equipamentos, "clubes": clubes}
        if s.get('city'): perfil_upd["cidade"] = s.get('city')
        if s.get('state'): perfil_upd["estado"] = s.get('state')
        if s.get('sex'): perfil_upd["genero"] = s.get('sex')
        if s.get('profile'): perfil_upd["foto_url"] = s.get('profile')
        if s.get('weight'): perfil_upd["peso"] = s.get('weight')
        supabase.table("usuarios_strava").update(perfil_upd).eq("id", strava_id).execute()

    url_activities = 'https://www.strava.com/api/v3/athlete/activities'
    treinos_brutos = []
    pagina = 1
    
    while True:
        res_strava = requests.get(url_activities, headers=headers, params={'per_page': 200, 'page': pagina})
        if res_strava.status_code != 200: raise HTTPException(status_code=500, detail="Falha na API Strava.")
        dados = res_strava.json()
        if not dados: break 
        treinos_brutos.extend(dados)
        if len(dados) < 200: break
        pagina += 1
        
    lista_final = formatar_atividades_para_banco(treinos_brutos)
    supabase.table("usuarios_strava").update({"historico_json": lista_final}).eq("id", strava_id).execute()
    return {"status": "success", "historico_json": lista_final}

# ==========================================
# 🌐 ROTAS DA API - IA MASTER COACH & PARSING
# ==========================================

@app.get("/treinos/calendario/{strava_id}")
def obter_calendario_treinos(strava_id: int):
    """
    Recupera todos os treinos planejados do atleta para montar a prancheta visual (Calendário).
    """
    try:
        # Busca no Supabase ordenado pela data do treino
        res_db = supabase.table("calendario_treinos").select("*").eq("strava_id", strava_id).order("data_treino").execute()
        treinos = res_db.data if res_db.data else []
        return {"status": "success", "treinos": treinos}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao buscar calendário: {str(e)}")

@app.post("/treinos/parse")
def parse_treino_texto(req: ParseTreinoRequest):
    """
    O Cérebro do Master Coach: Pega um texto livre do atleta e estrutura
    via Gemini num JSON matemático rígido para o Calendário.
    """
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = f"""
        Atue como um treinador de corrida de elite e cientista de dados. 
        Analise o seguinte texto digitado pelo atleta (planejamento de treino):
        "{req.texto_bruto}"

        Retorne ESTRITAMENTE um objeto JSON válido (sem tags markdown de código, apenas as chaves/valores) com este formato exato:
        {{
            "descricao_limpa": "string (um resumo limpo e encorajador de 1 linha sobre o objetivo do treino)",
            "distancia_estimada_km": float (calcule a soma matemática de todas as distâncias descritas. Ex: aquece 3k + 10x400m + solta 2k = 9.0. Use 0.0 se não for possível deduzir),
            "blocos": [
                {{
                    "tipo": "string (opções estritas: 'aquecimento', 'principal', 'tiro', 'soltura', 'recuperacao')",
                    "repeticoes": int (padrão 1, mas se for 10x400, aqui é 10),
                    "distancia_metros": int (ex: 400. Se for por tempo, coloque null),
                    "tempo_minutos": int (opcional, null se for por distância),
                    "intensidade_alvo": "string (Ex: 'Z2', 'Pace Maratona', 'Forte', 'Leve')"
                }}
            ]
        }}
        """

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        
        # Garante que a IA retornou JSON válido
        estrutura_json = json.loads(response.text)
        
        # Upsert Inteligente (Proteção contra duplicatas no mesmo dia)
        res_busca = supabase.table("calendario_treinos").select("id").eq("strava_id", req.strava_id).eq("data_treino", req.data_treino).execute()
        
        payload_db = {
            "strava_id": req.strava_id,
            "data_treino": req.data_treino,
            "texto_bruto": req.texto_bruto,
            "estrutura_json": estrutura_json,
            "status": "planejado"
        }
        if req.ciclo_id:
            payload_db["ciclo_id"] = req.ciclo_id

        if res_busca.data and len(res_busca.data) > 0:
            # Se já havia um treino neste dia, ele atualiza (edição de treino)
            id_treino = res_busca.data[0]['id']
            supabase.table("calendario_treinos").update(payload_db).eq("id", id_treino).execute()
            acao = "atualizado"
        else:
            # Novo agendamento
            supabase.table("calendario_treinos").insert(payload_db).execute()
            acao = "inserido"
            
        return {"status": "success", "acao": acao, "dados_parseados": estrutura_json}

    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="A Inteligência Artificial falhou em gerar um modelo de dados estruturado.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Falha ao interpretar o treino: {str(e)}")

# ==========================================
# 🌐 ROTAS DA API - IA DIAGNÓSTICO & TROFÉUS
# ==========================================

@app.post("/trofeus/garimpar/{strava_id}")
def garimpar_recordes_pessoais(strava_id: int, req: TrofeusRequest):
    res_db = supabase.table("usuarios_strava").select("*").eq("id", strava_id).execute()
    usuario = res_db.data[0]
    token = atualizar_token_strava(usuario['refresh_token'])
    historico = usuario.get("historico_json") or []
    
    provas = [t for t in historico if t.get('workout_type') == 1]
    if not provas:
        supabase.table("usuarios_strava").update({"trofeus_json": {}}).eq("id", strava_id).execute()
        return {"status": "success", "analisados": 0, "trofeus": {}, "msg": "Nenhuma prova oficial encontrada."}

    headers = {'Authorization': f'Bearer {token}'}
    distancias_mapa = {"1k": "1k", "5k": "5k", "10k": "10k", "half marathon": "Half Marathon", "marathon": "Marathon"}
    trofeus_renovados = {} 
    
    for prova in provas:
        res_detalhe = requests.get(f"https://www.strava.com/api/v3/activities/{prova['id']}", headers=headers)
        if res_detalhe.status_code != 200: continue
        
        dados_detalhe = res_detalhe.json()
        best_efforts = dados_detalhe.get('best_efforts', [])
        
        for effort in best_efforts:
            nome_clean = effort.get('name', '').lower()
            if nome_clean in distancias_mapa:
                chave = distancias_mapa[nome_clean]
                tempo_segundos = effort.get('elapsed_time')
                atual = trofeus_renovados.get(chave)
                
                if not atual or tempo_segundos < atual['tempo_segundos']:
                    h, m, s = int(tempo_segundos // 3600), int((tempo_segundos % 3600) // 60), int(tempo_segundos % 60)
                    trofeus_renovados[chave] = {
                        "tempo_segundos": tempo_segundos,
                        "tempo_formatado": f"{h}h {m:02d}m {s:02d}s" if h > 0 else f"{m}m {s:02d}s",
                        "nome_treino": prova['name'],
                        "data": prova['start_date_local'],
                        "fc_media": int(dados_detalhe.get('average_heartrate', 0)),
                        "fc_maxima": int(dados_detalhe.get('max_heartrate', 0))
                    }
                    
    supabase.table("usuarios_strava").update({"trofeus_json": trofeus_renovados}).eq("id", strava_id).execute()
    return {"status": "success", "analisados": len(provas), "trofeus": trofeus_renovados}

@app.post("/ia/analise")
def motor_ia_gemini(requisicao: IAAnaliseRequest):
    res_db = supabase.table("usuarios_strava").select("*").eq("id", requisicao.strava_id).execute()
    atleta = res_db.data[0]
    historico = atleta.get("historico_json") or []
    if not historico: raise HTTPException(status_code=400, detail="Sem treinos para analisar.")

    resumo_treinos = [f"[{t['start_date_local'][:10]}] {round(t['distancia_km'], 1)}km | Pace: {t['Pace_Medio']} | BPM: {int(t['average_heartrate'])}" for t in historico[:12]]
    prompt = f"Analise o atleta {atleta['nome']}, {atleta.get('idade')} anos, {atleta.get('peso')}kg. \n{chr(10).join(resumo_treinos)}\nRetorne JSON: diagnostico_geral, ponto_de_melhoria, nota_eficiencia (0-10)."
    
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt, config=types.GenerateContentConfig(response_mime_type="application/json"))
        resultado = json.loads(response.text)
        supabase.table("usuarios_strava").update({"ia_report_json": resultado}).eq("id", requisicao.strava_id).execute()
        return resultado
    except Exception:
        raise HTTPException(status_code=500, detail="Falha no motor de IA.")

@app.put("/atleta/{strava_id}/biometria")
def atualizar_biometria(strava_id: int, req: BiometriaRequest):
    upd = {k: v for k, v in req.dict().items() if v is not None}
    if upd: supabase.table("usuarios_strava").update(upd).eq("id", strava_id).execute()
    return {"status": "success"}

# ==========================================
# 🌐 ROTAS DA API - ZONAS CARDÍACAS E DE RITMO
# ==========================================

@app.post("/fisiologia/calcular-zonas/{strava_id}")
def calcular_zonas_cardiacas(strava_id: int, req: ConfigZonasFC):
    try:
        if req.metodo == 'max':
            if not req.fc_max: raise ValueError("FC Máxima não informada.")
            zonas = [
                {"id": 1, "nome": "Z1 - Recuperação", "min": int(req.fc_max * 0.50), "max": int(req.fc_max * 0.59), "tema": "cinza", "desc": "Aquecimento / Regenerativo"},
                {"id": 2, "nome": "Z2 - Aeróbico", "min": int(req.fc_max * 0.60), "max": int(req.fc_max * 0.69), "tema": "azul", "desc": "Resistência Base / Longão"},
                {"id": 3, "nome": "Z3 - Tempo Run", "min": int(req.fc_max * 0.70), "max": int(req.fc_max * 0.79), "tema": "verde", "desc": "Ritmo de Maratona"},
                {"id": 4, "nome": "Z4 - Limiar", "min": int(req.fc_max * 0.80), "max": int(req.fc_max * 0.89), "tema": "laranja", "desc": "Pace de 10k / Desconfortável"},
                {"id": 5, "nome": "Z5 - Anaeróbico", "min": int(req.fc_max * 0.90), "max": req.fc_max, "tema": "vermelho", "desc": "Tiros / Esforço Máximo"},
            ]
        elif req.metodo == 'karvonen':
            if not req.fc_max or not req.fc_repouso: raise ValueError("Falta FC Máxima ou de Repouso.")
            fcr = req.fc_max - req.fc_repouso
            def calc_karvonen(perc): return int((fcr * perc) + req.fc_repouso)
            zonas = [
                {"id": 1, "nome": "Z1 - Recuperação", "min": calc_karvonen(0.50), "max": calc_karvonen(0.59), "tema": "cinza", "desc": "Aquecimento / Regenerativo"},
                {"id": 2, "nome": "Z2 - Aeróbico", "min": calc_karvonen(0.60), "max": calc_karvonen(0.69), "tema": "azul", "desc": "Resistência Base / Longão"},
                {"id": 3, "nome": "Z3 - Tempo Run", "min": calc_karvonen(0.70), "max": calc_karvonen(0.79), "tema": "verde", "desc": "Ritmo de Maratona"},
                {"id": 4, "nome": "Z4 - Limiar", "min": calc_karvonen(0.80), "max": calc_karvonen(0.89), "tema": "laranja", "desc": "Pace de 10k / Desconfortável"},
                {"id": 5, "nome": "Z5 - Anaeróbico", "min": calc_karvonen(0.90), "max": req.fc_max, "tema": "vermelho", "desc": "Tiros / Esforço Máximo"},
            ]
        elif req.metodo == 'limiar':
            if not req.fc_limiar: raise ValueError("Falta FC de Limiar (LTHR).")
            lt = req.fc_limiar
            zonas = [
                {"id": 1, "nome": "Z1 - Recuperação", "min": int(lt * 0.65), "max": int(lt * 0.84), "tema": "cinza", "desc": "Regenerativo (<85% LTHR)"},
                {"id": 2, "nome": "Z2 - Base Aeróbica", "min": int(lt * 0.85), "max": int(lt * 0.89), "tema": "azul", "desc": "Resistência (85-89% LTHR)"},
                {"id": 3, "nome": "Z3 - Tempo", "min": int(lt * 0.90), "max": int(lt * 0.94), "tema": "verde", "desc": "Ritmo Constante (90-94% LTHR)"},
                {"id": 4, "nome": "Z4 - Limiar", "min": int(lt * 0.95), "max": int(lt * 0.99), "tema": "laranja", "desc": "No Limite do Ácido (95-99% LTHR)"},
                {"id": 5, "nome": "Z5 - Anaeróbico", "min": lt, "max": int(lt * 1.05), "tema": "vermelho", "desc": "Explosão / VO2 (>100% LTHR)"},
            ]
        else:
            raise ValueError("Metodologia de FC inválida.")
        
        res_db = supabase.table("usuarios_strava").select("fisiologia_json").eq("id", strava_id).execute()
        fisiologia_atual = res_db.data[0].get("fisiologia_json") or {} if res_db.data else {}
        
        fisiologia_atual.update({
            "metodo": req.metodo, 
            "fc_max": req.fc_max, 
            "fc_repouso": req.fc_repouso, 
            "fc_limiar": req.fc_limiar, 
            "zonas": zonas
        })
        
        supabase.table("usuarios_strava").update({"fisiologia_json": fisiologia_atual}).eq("id", strava_id).execute()
        return {"status": "success", "zonas": zonas, "fisiologia_salva": fisiologia_atual}
        
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/fisiologia/calcular-zonas-pace/{strava_id}")
def calcular_zonas_ritmo(strava_id: int, req: ConfigZonasPace):
    try:
        pace_limiar_seg = 0
        
        if req.metodo in ['daniels', 'friel']:
            if req.distancia_km and req.tempo_segundos:
                
                tempo_ajustado = req.tempo_segundos
                if req.altitude_m and req.altitude_m > 500:
                    tempo_ajustado = tempo_ajustado * (1 - (((req.altitude_m - 500) / 1000) * 0.015))
                if req.temperatura_c and req.temperatura_c > 20:
                    tempo_ajustado = tempo_ajustado * (1 - (((req.temperatura_c - 20) / 5) * 0.015))

                t_10k_sec = tempo_ajustado * ((10.0 / req.distancia_km) ** 1.06)
                pace_limiar_seg = t_10k_sec / 10.0
            elif req.pace_limiar:
                pace_limiar_seg = pace_str_to_seg(req.pace_limiar)
            else:
                raise ValueError("Para gerar o Ritmo, informe a Distância/Tempo ou o Pace Manual.")
        else:
            raise ValueError("Metodologia de Pace inválida.")

        zonas = []
        
        if req.metodo == 'daniels':
            zonas = [
                {"id": 1, "nome": "Pace E (Fácil/Easy)", "min": seg_to_pace_str(pace_limiar_seg * 1.30), "max": seg_to_pace_str(pace_limiar_seg * 1.20), "tema": "cinza", "desc": "Aquecimento e Resistência Longa"},
                {"id": 2, "nome": "Pace M (Maratona)", "min": seg_to_pace_str(pace_limiar_seg * 1.15), "max": seg_to_pace_str(pace_limiar_seg * 1.08), "tema": "azul", "desc": "Ritmo Específico de Prova (42k)"},
                {"id": 3, "nome": "Pace T (Limiar)", "min": seg_to_pace_str(pace_limiar_seg * 1.03), "max": seg_to_pace_str(pace_limiar_seg * 0.98), "tema": "verde", "desc": "Desconfortável Constante (Tempo Run)"},
                {"id": 4, "nome": "Pace I (Intervalado)", "min": seg_to_pace_str(pace_limiar_seg * 0.95), "max": seg_to_pace_str(pace_limiar_seg * 0.90), "tema": "laranja", "desc": "Estímulos VO2 Max (3 a 5 min)"},
                {"id": 5, "nome": "Pace R (Repetição)", "min": seg_to_pace_str(pace_limiar_seg * 0.88), "max": seg_to_pace_str(pace_limiar_seg * 0.80), "tema": "vermelho", "desc": "Tiros de Velocidade Pura (Pista)"},
            ]
        
        elif req.metodo == 'friel':
            zonas = [
                {"id": 1, "nome": "Z1 - Recuperação", "min": seg_to_pace_str(pace_limiar_seg * 1.40), "max": seg_to_pace_str(pace_limiar_seg * 1.29), "tema": "cinza", "desc": "Pace Regenerativo (Trote)"},
                {"id": 2, "nome": "Z2 - Aeróbico", "min": seg_to_pace_str(pace_limiar_seg * 1.28), "max": seg_to_pace_str(pace_limiar_seg * 1.14), "tema": "azul", "desc": "Endurance e Construção Base"},
                {"id": 3, "nome": "Z3 - Tempo", "min": seg_to_pace_str(pace_limiar_seg * 1.13), "max": seg_to_pace_str(pace_limiar_seg * 1.06), "tema": "verde", "desc": "Ritmo Forte de Meia Maratona"},
                {"id": 4, "nome": "Z4 - Limiar", "min": seg_to_pace_str(pace_limiar_seg * 1.05), "max": seg_to_pace_str(pace_limiar_seg * 0.99), "tema": "laranja", "desc": "No limiar do Lactato (Pace 10k)"},
                {"id": 5, "nome": "Z5 - Anaeróbico", "min": seg_to_pace_str(pace_limiar_seg * 0.98), "max": seg_to_pace_str(pace_limiar_seg * 0.85), "tema": "vermelho", "desc": "Sprint / Capacidade Anaeróbica"},
            ]

        res_db = supabase.table("usuarios_strava").select("fisiologia_json").eq("id", strava_id).execute()
        fisiologia_atual = res_db.data[0].get("fisiologia_json") or {} if res_db.data else {}
        
        fisiologia_atual.update({
            "metodo_pace": req.metodo,
            "pace_limiar": seg_to_pace_str(pace_limiar_seg),
            "zonas_pace": zonas,
            "dist_ref_pace": req.distancia_km,
            "tempo_ref_pace": req.tempo_segundos,
            "pace_altitude": req.altitude_m,
            "pace_temp": req.temperatura_c
        })
        
        supabase.table("usuarios_strava").update({"fisiologia_json": fisiologia_atual}).eq("id", strava_id).execute()
        return {"status": "success", "zonas_pace": zonas, "fisiologia_salva": fisiologia_atual}
        
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/fisiologia/extrair-limiar/{strava_id}")
def extrair_limiar_multi_provas(strava_id: int, req: ExtrairLimiarMultiRequest):
    if not req.activities:
        raise HTTPException(status_code=400, detail="Nenhuma corrida selecionada.")
        
    res_db = supabase.table("usuarios_strava").select("*").eq("id", strava_id).execute()
    if not res_db.data: raise HTTPException(404)
    
    usuario = res_db.data[0]
    token = atualizar_token_strava(usuario['refresh_token'])
    headers = {'Authorization': f'Bearer {token}'}
    
    resultados_lthr = []
    log_relatorio = ""
    
    for act_id in req.activities[:3]:
        res_atividade = requests.get(f"https://www.strava.com/api/v3/activities/{act_id}", headers=headers)
        if res_atividade.status_code != 200: continue
        
        dados = res_atividade.json()
        nome_prova = dados.get('name', 'Treino')
        distancia_km = dados.get('distance', 0) / 1000.0
        bpm_medio_geral = dados.get('average_heartrate', 0)
        bpm_maximo = dados.get('max_heartrate', 0)
        total_elevacao = dados.get('total_elevation_gain', 0)
        
        if bpm_medio_geral == 0:
            log_relatorio += f"⚠️ {nome_prova}: Ignorada (Sem BPM registrado).\n\n"
            continue
            
        url_streams = f"https://www.strava.com/api/v3/activities/{act_id}/streams?keys=heartrate&key_by_type=true"
        res_streams = requests.get(url_streams, headers=headers)
        
        bpm_base = bpm_medio_geral
        usou_streams = False
        
        if res_streams.status_code == 200:
            streams = res_streams.json()
            if 'heartrate' in streams:
                hr_data = streams['heartrate']['data']
                start_idx = int(len(hr_data) * 0.33)
                hr_isolado = hr_data[start_idx:]
                if hr_isolado:
                    bpm_base = sum(hr_isolado) / len(hr_isolado)
                    usou_streams = True

        fator_correcao = 1.0
        logs = []
        
        if 4.5 <= distancia_km <= 5.5:
            fator_correcao -= 0.01
            logs.append("Distância (5k): -1% Fator (Supra-limiar)")
        elif 9.5 <= distancia_km <= 10.5:
            logs.append("Distância (10k): 0% Fator (Reflete Limiar)")
        elif 20.0 <= distancia_km <= 22.0:
            fator_correcao += 0.05
            logs.append("Distância (Meia): +5% Fator (Sub-limiar)")
            
        if req.compensar_alt:
            elev_por_km = total_elevacao / distancia_km if distancia_km > 0 else 0
            if elev_por_km > 20:
                fator_correcao -= 0.02
                logs.append("Elevação Alta (>20m/km): -2% Fator")
            elif elev_por_km > 10:
                fator_correcao -= 0.01
                logs.append("Elevação Média (>10m/km): -1% Fator")
            
            elev_high = dados.get('elev_high')
            if elev_high is not None and elev_high > 1500:
                fator_correcao -= 0.03
                logs.append(f"Altitude Extrema (>1500m): -3% Fator")
                
        if req.compensar_temp:
            avg_temp = dados.get('average_temp')
            if avg_temp is not None:
                if avg_temp >= 28:
                    fator_correcao -= 0.04
                    logs.append(f"Calor Extremo ({avg_temp}°C): -4% Fator (Termorregulação)")
                elif avg_temp >= 24:
                    fator_correcao -= 0.02
                    logs.append(f"Calor Alto ({avg_temp}°C): -2% Fator")
            
        if not usou_streams and (4.5 <= distancia_km <= 5.5) and bpm_maximo > 140:
            lthr_prova = int(bpm_maximo * (0.92 - (1.0 - fator_correcao)))
            logs.append(f"Sem Streams. Limiar deduzido da FC Máx ({bpm_maximo} bpm).")
        else:
            lthr_prova = int(bpm_base * fator_correcao)
            if usou_streams:
                logs.append(f"Streams: Média estabilizada isolada ({int(bpm_base)} bpm).")
            else:
                logs.append(f"Sem Streams: Média bruta ({int(bpm_base)} bpm) com forte ruído de aquecimento.")

        resultados_lthr.append(lthr_prova)
        log_relatorio += f"🏃‍♂️ {nome_prova}\n"
        for l in logs: log_relatorio += f"  {l}\n"
        log_relatorio += f"  ↳ Limiar Resultante: {lthr_prova} bpm\n\n"

    if not resultados_lthr:
        raise HTTPException(status_code=400, detail="Não foi possível extrair dados de nenhuma corrida selecionada.")
        
    media_final_limiar = int(sum(resultados_lthr) / len(resultados_lthr))
    
    return {
        "status": "success",
        "limiar_estimado": media_final_limiar,
        "metodo_usado": log_relatorio.strip(),
        "qtd_analisadas": len(resultados_lthr)
    }
