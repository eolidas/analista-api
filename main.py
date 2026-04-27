import os
import json
import requests
import pandas as pd
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from google import genai
from google.genai import types
from supabase import create_client, Client

# ==========================================
# MOTOR ANALISTA DE BOLSO - BACKEND (PRODUÇÃO)
# Versão: 3.3.0 - Extração de Limiar via STREAMS Dinâmico (Friel 33%)
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

# --- Módulo Fisiológico ---
class ConfigZonas(BaseModel):
    metodo: str  # 'max', 'karvonen', 'limiar'
    fc_max: Optional[int] = None
    fc_repouso: Optional[int] = None
    fc_limiar: Optional[int] = None

# ==========================================
# ⚙️ FUNÇÕES DE ENGENHARIA DE DADOS
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
    
    colunas_finais = ['id', 'type', 'workout_type', 'name', 'distancia_km', 'Pace_Medio', 'average_heartrate', 'max_heartrate', 'total_elevation_gain', 'moving_time', 'start_date_local', 'elapsed_time']
    
    if 'elapsed_time' not in df.columns:
        df['elapsed_time'] = df['moving_time']
        
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

# ==========================================
# 🌐 ROTAS DA API - OAUTH & SINCRONIZAÇÃO
# ==========================================

@app.get("/")
def health_check():
    return {"status": "Motor V8 Operante 🚀", "version": "3.3.0"}

@app.get("/keep-alive")
def manter_acordado():
    try:
        supabase.table("usuarios_strava").select("id").limit(1).execute()
        return {"status": "Motor e Banco de Dados 100% Quentes! 🔥"}
    except Exception as e:
        return {"status": "Motor acordado, mas banco falhou.", "erro": str(e)}

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
    perfil_atualizado_frontend = {}
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
        perfil_atualizado_frontend = perfil_upd

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
    return {"status": "success", "historico_json": lista_final, "perfil_atualizado": perfil_atualizado_frontend}

# ==========================================
# 🌐 ROTAS DA API - IA & TROFÉUS
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
        return {"status": "success", "analisados": 0, "trofeus": {}, "msg": "Nenhuma prova oficial encontrada. A sua Sala de Troféus foi limpa."}

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
# 🌐 ROTAS DA API - FISIOLOGIA (ZONAS E LIMIAR VIA STREAMS)
# ==========================================

@app.post("/fisiologia/calcular-zonas/{strava_id}")
def calcular_zonas_cardiacas(strava_id: int, req: ConfigZonas):
    """Motor de cálculo fisiológico com rigor científico (Friel, ACSM, Karvonen)."""
    zonas = []
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
                {"id": 4, "nome": "Z4 - Limiar de Lactato", "min": int(lt * 0.95), "max": int(lt * 0.99), "tema": "laranja", "desc": "No Limite do Ácido (95-99% LTHR)"},
                {"id": 5, "nome": "Z5 - Anaeróbico", "min": lt, "max": int(lt * 1.05), "tema": "vermelho", "desc": "Explosão / VO2 (>100% LTHR)"},
            ]
        else:
            raise ValueError("Metodologia inválida.")
        
        fisiologia_data = {"metodo": req.metodo, "fc_max": req.fc_max, "fc_repouso": req.fc_repouso, "fc_limiar": req.fc_limiar, "zonas": zonas}
        supabase.table("usuarios_strava").update({"fisiologia_json": fisiologia_data}).eq("id", strava_id).execute()
        return {"status": "success", "zonas": zonas, "fisiologia_salva": fisiologia_data}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/fisiologia/extrair-limiar/{strava_id}/{activity_id}")
def extrair_limiar_de_prova(strava_id: int, activity_id: int):
    """
    Motor Científico: Puxa Streams do Strava e aplica a Análise Dinâmica de Terços
    para expurgar tempos de aquecimento ou rampas cardíacas.
    """
    res_db = supabase.table("usuarios_strava").select("*").eq("id", strava_id).execute()
    usuario = res_db.data[0]
    token = atualizar_token_strava(usuario['refresh_token'])
    
    headers = {'Authorization': f'Bearer {token}'}
    res_atividade = requests.get(f"https://www.strava.com/api/v3/activities/{activity_id}", headers=headers)
    
    if res_atividade.status_code != 200: 
        raise HTTPException(status_code=500, detail="Erro ao acessar telemetria no Strava.")
    
    dados = res_atividade.json()
    distancia_km = dados.get('distance', 0) / 1000.0
    bpm_medio_geral = dados.get('average_heartrate', 0)
    bpm_maximo = dados.get('max_heartrate', 0)
    nome_prova = dados.get('name', 'Prova Oficial')
    
    if bpm_medio_geral == 0 or bpm_maximo == 0:
        raise HTTPException(status_code=400, detail="Esta atividade não possui registros cardíacos (cinta/relógio).")

    # ==========================================
    # ANÁLISE DE STREAMS (SEGUNDO A SEGUNDO)
    # ==========================================
    url_streams = f"https://www.strava.com/api/v3/activities/{activity_id}/streams?keys=heartrate&key_by_type=true"
    res_streams = requests.get(url_streams, headers=headers)
    
    bpm_base_calculo = bpm_medio_geral
    usou_streams = False
    
    if res_streams.status_code == 200:
        streams = res_streams.json()
        if 'heartrate' in streams:
            hr_data = streams['heartrate']['data']
            
            # PROTOCOLO PADRÃO OURO DE FRIEL (Análise de Rampa):
            # Descartes dos primeiros 33% da prova inteira para focar na estabilização
            start_idx = int(len(hr_data) * 0.33)
            hr_isolado = hr_data[start_idx:]
            
            if hr_isolado:
                bpm_base_calculo = sum(hr_isolado) / len(hr_isolado)
                usou_streams = True

    # ==========================================
    # MATEMÁTICA DE COMPENSAÇÃO INTELIGENTE
    # ==========================================
    if 4.5 <= distancia_km <= 5.5:
        if usou_streams:
            # O trecho final estabilizado do 5k é a mais pura expressão do limiar de lactato 
            # de um atleta rápido. Usamos fator 0.99 apenas como safety net.
            limiar_estimado = int(bpm_base_calculo * 0.99)
            detalhe_metodo = f"• Análise de Streams (Segundo a Segundo).\n• Primeiro terço (rampa cardíaca) expurgado.\n• Média Estabilizada: {int(bpm_base_calculo)} bpm.\n• Fator 0.99x (Ajuste fino de 5k)."
        else:
            # Sem os streams, a média sofre drásticamente por causa do aquecimento.
            # O mais seguro é extrair 92% do Pico Máximo para evitar "falsos baixos".
            if bpm_maximo > 140:
                limiar_estimado = int(bpm_maximo * 0.92)
                detalhe_metodo = f"• API de Streams indisponível (Resumo).\n• Média suja ignorada.\n• Estimado a 92% da FC Máxima atingida na prova ({bpm_maximo} bpm)."
            else:
                limiar_estimado = int(bpm_medio_geral * 1.03)
                detalhe_metodo = f"• API de Streams indisponível.\n• Limiar estimado com acréscimo de 3% sobre a média bruta."
                
    elif 9.5 <= distancia_km <= 10.5:
        if usou_streams:
            limiar_estimado = int(bpm_base_calculo * 1.00)
            detalhe_metodo = f"• Análise de Streams ativa.\n• Primeiro terço expurgado.\n• Média Estabilizada (10k) = Limiar Direto ({int(bpm_base_calculo)} bpm)."
        else:
            limiar_estimado = int(bpm_medio_geral * 1.00)
            detalhe_metodo = f"• Análise de Resumo.\n• Média Geral do 10k = Limiar Direto ({int(bpm_medio_geral)} bpm)."
            
    elif 20.0 <= distancia_km <= 22.0:
        limiar_estimado = int(bpm_base_calculo * 1.05)
        detalhe_metodo = f"• Análise de Meia Maratona.\n• Multiplicador de 1.05x (Esforço sub-limiar) sobre a média de {int(bpm_base_calculo)} bpm."
        
    else:
        limiar_estimado = int(bpm_base_calculo)
        detalhe_metodo = f"• Distância atípica de teste.\n• FC Média utilizada diretamente: {int(bpm_base_calculo)} bpm."
    
    return {
        "status": "success", 
        "limiar_estimado": limiar_estimado, 
        "bpm_medio_real": bpm_medio_geral,
        "metodo_usado": detalhe_metodo,
        "nome_prova": nome_prova
    }
