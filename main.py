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

# --- MODELOS DE DADOS (Pydantic) ---

class StravaAuthRequest(BaseModel):
    code: str

class IAAnaliseRequest(BaseModel):
    strava_id: int

class BiometriaRequest(BaseModel):
    peso: Optional[float] = None
    altura: Optional[float] = None
    idade: Optional[int] = None

# ==========================================
# ⚙️ FUNÇÕES AUXILIARES & REGRAS DE NEGÓCIO
# ==========================================

def atualizar_token_strava(refresh_token: str) -> str:
    """Renova o access_token expirado."""
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
    Sincronização Unificada: Extrai sempre Corridas e Caminhadas.
    A filtragem de exibição ocorrerá nativamente no Frontend (Client-side).
    """
    url = 'https://www.strava.com/api/v3/athlete/activities'
    headers = {'Authorization': f'Bearer {access_token}'}
    
    atividades_totais = []
    pagina = 1
    
    while True:
        params = {'per_page': 200, 'page': pagina}
        if after_timestamp:
            params['after'] = after_timestamp

        res = requests.get(url, headers=headers, params=params)
        if res.status_code != 200: break
            
        dados_pagina = res.json()
        if not dados_pagina: break
            
        atividades_totais.extend(dados_pagina)
        pagina += 1
        
        if len(dados_pagina) < 200: break

    if not atividades_totais: return []
        
    df_bruto = pd.DataFrame(atividades_totais)
    if 'type' not in df_bruto.columns: return []
        
    # FILTRO MESTRE: Coleta tanto as Corridas quanto as Caminhadas
    df_atividades = df_bruto[df_bruto['type'].isin(['Run', 'Walk'])].copy()
    
    if df_atividades.empty: return []
        
    df_atividades['distancia_km'] = df_atividades['distance'] / 1000.0
    
    def formatar_pace(linha):
        if linha['distancia_km'] == 0: return "00:00"
        pace_dec = (linha['moving_time'] / 60) / linha['distancia_km']
        return f"{int(pace_dec):02d}:{int(round((pace_dec - int(pace_dec)) * 60)):02d}"
        
    df_atividades['Pace_Medio'] = df_atividades.apply(formatar_pace, axis=1)
    df_atividades['Cadence_SPM'] = df_atividades['average_cadence'] * 2 if 'average_cadence' in df_atividades.columns else 0
    df_atividades['average_heartrate'] = df_atividades['average_heartrate'].fillna(0) if 'average_heartrate' in df_atividades.columns else 0
    df_atividades['total_elevation_gain'] = df_atividades['total_elevation_gain'] if 'total_elevation_gain' in df_atividades.columns else 0
        
    df_atividades['start_date_local'] = pd.to_datetime(df_atividades['start_date_local'])
    df_atividades = df_atividades.sort_values(by='start_date_local', ascending=False).reset_index(drop=True)

    # NOVO: Adicionado a coluna 'type' para o Frontend saber o que é Run e Walk
    colunas = ['type', 'name', 'distancia_km', 'Pace_Medio', 'Cadence_SPM', 'average_heartrate', 'total_elevation_gain', 'moving_time', 'start_date_local']
    
    json_string = df_atividades[colunas].to_json(orient='records', force_ascii=False, date_format='iso')
    return json.loads(json_string)

def calcular_estatisticas(historico: list) -> dict:
    """Calculadora Backend de Volume."""
    if not historico:
        return {"totalDist": 0, "totalWorkouts": 0, "monthVolume": 0, "weekVolume": 0}

    total_dist = 0
    month_volume = 0
    week_volume = 0
    
    hoje = datetime.now()
    mes_atual = hoje.month
    ano_atual = hoje.year
    
    inicio_semana = hoje - timedelta(days=hoje.weekday())
    inicio_semana = inicio_semana.replace(hour=0, minute=0, second=0, microsecond=0)

    for treino in historico:
        dist = treino.get("distancia_km", 0)
        total_dist += dist
        data_str = treino.get("start_date_local")
        if data_str:
            try:
                data_limpa = data_str.replace("Z", "+00:00")[:19]
                data_treino = datetime.fromisoformat(data_limpa)
                if data_treino.month == mes_atual and data_treino.year == ano_atual:
                    month_volume += dist
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
    
    headers = {'Authorization': f'Bearer {access_token}'}
    res_perfil = requests.get('https://www.strava.com/api/v3/athlete', headers=headers)
    atleta = res_perfil.json() if res_perfil.status_code == 200 else atleta_resumo
    
    equipamentos = {"tenis": atleta.get('shoes', []), "bicicletas": atleta.get('bikes', [])}
    clubes = [{"nome": c.get("name"), "foto": c.get("profile")} for c in atleta.get('clubs', [])]
    
    upsert_data = {
        "id": atleta_id, "access_token": access_token, "refresh_token": token_payload.get('refresh_token'),
        "nome": atleta.get('firstname'), "sobrenome": atleta.get('lastname'), "foto_url": atleta.get('profile'),
        "peso": atleta.get('weight'), "cidade": atleta.get('city'), "estado": atleta.get('state'),
        "genero": atleta.get('sex'), "data_criacao": atleta.get('created_at'), "bio": atleta.get('bio'),
        "clubes": clubes, "equipamentos": equipamentos
    }
    supabase.table("usuarios_strava").upsert(upsert_data).execute()
    return {"status": "success", "strava_id": atleta_id}

@app.put("/atleta/{strava_id}/biometria")
def atualizar_biometria(strava_id: int, req: BiometriaRequest):
    update_data = {}
    if req.peso is not None: update_data["peso"] = req.peso
    if req.altura is not None: update_data["altura"] = req.altura
    if req.idade is not None: update_data["idade"] = req.idade
        
    if update_data:
        res = supabase.table("usuarios_strava").update(update_data).eq("id", strava_id).execute()
        if not res.data: raise HTTPException(status_code=404, detail="Atleta não encontrado")
    return {"status": "success"}

@app.get("/atleta/{strava_id}")
def obter_dados_atleta(strava_id: int):
    res_db = supabase.table("usuarios_strava").select("*").eq("id", strava_id).execute()
    if not res_db.data: raise HTTPException(status_code=404, detail="Atleta não encontrado.")
    
    dados = res_db.data[0]
    historico = dados.get("historico_json") or []
    return {
        "perfil": {
            "nome": dados.get("nome"), "sobrenome": dados.get("sobrenome"), "foto_url": dados.get("foto_url"),
            "peso": dados.get("peso"), "altura": dados.get("altura"), "idade": dados.get("idade"),
            "cidade": dados.get("cidade"), "estado": dados.get("estado"), "genero": dados.get("genero"),
            "data_criacao": dados.get("data_criacao"), "bio": dados.get("bio"),
            "clubes": dados.get("clubes"), "equipamentos": dados.get("equipamentos")
        },
        "estatisticas": calcular_estatisticas(historico),
        "historico_json": historico,
        "ia_report_json": dados.get("ia_report_json")
    }

@app.post("/atleta/{strava_id}/sincronizar")
def sincronizar_treinos(strava_id: int):
    """SINCRONIZAÇÃO INTELIGENTE (LIMPA E DIRETA). O Backend ignora parametros antigos (caminhadas, force_full) da URL."""
    res_db = supabase.table("usuarios_strava").select("*").eq("id", strava_id).execute()
    if not res_db.data: raise HTTPException(status_code=404, detail="Atleta não encontrado.")
    
    atleta_db = res_db.data[0]
    access_token = atualizar_token_strava(atleta_db['refresh_token'])
    
    # 1. Sync do Perfil
    headers = {'Authorization': f'Bearer {access_token}'}
    res_perfil = requests.get('https://www.strava.com/api/v3/athlete', headers=headers)
    perfil_final = None
    if res_perfil.status_code == 200:
        s = res_perfil.json()
        equipamentos = {"tenis": s.get('shoes', []), "bicicletas": s.get('bikes', [])}
        clubes = [{"nome": c.get("name"), "foto": c.get("profile")} for c in s.get('clubs', [])]
        perfil_upd = {
            "peso": s.get('weight'), "cidade": s.get('city'), "estado": s.get('state'),
            "equipamentos": equipamentos, "clubes": clubes, "foto_url": s.get('profile')
        }
        supabase.table("usuarios_strava").update(perfil_upd).eq("id", strava_id).execute()
        res_full = supabase.table("usuarios_strava").select("*").eq("id", strava_id).execute()
        perfil_final = res_full.data[0]

    # 2. Sync de Treinos (Apenas Delta Clássico)
    historico_antigo = atleta_db.get('historico_json') or []
    after_timestamp = None
    
    if historico_antigo:
        data_str = historico_antigo[0].get("start_date_local")
        if data_str:
            dt = datetime.fromisoformat(data_str.replace("Z", "+00:00")[:19])
            after_timestamp = int(dt.timestamp()) + 1 
            
    treinos_novos = baixar_novos_treinos(access_token, after_timestamp)
    todos_treinos = treinos_novos + historico_antigo if treinos_novos else historico_antigo

    # 3. Deduplicação
    treinos_unicos = {}
    for t in todos_treinos:
        data_chave = t.get('start_date_local')
        if data_chave not in treinos_unicos:
            treinos_unicos[data_chave] = t
            
    historico_atualizado = sorted(list(treinos_unicos.values()), key=lambda x: x.get('start_date_local', ''), reverse=True)
    
    if treinos_novos or len(historico_atualizado) != len(historico_antigo):
        supabase.table("usuarios_strava").update({"historico_json": historico_atualizado}).eq("id", strava_id).execute()
        
    return {
        "status": "success", 
        "novos": len(treinos_novos),
        "historico": historico_atualizado,
        "estatisticas": calcular_estatisticas(historico_atualizado),
        "perfil": perfil_final 
    }

@app.post("/ia/analise")
def gerar_analise_ia(requisicao: IAAnaliseRequest):
    """
    Motor Blindado: A IA analisa EXCLUSIVAMENTE corridas ('Run') para garantir
    que diagnósticos biomecânicos e cardíacos não sejam contaminados por passeios.
    """
    res_db = supabase.table("usuarios_strava").select("*").eq("id", requisicao.strava_id).execute()
    usuario = res_db.data[0]
    historico = usuario.get("historico_json")
    
    if not historico: raise HTTPException(status_code=400, detail="Sem treinos para analisar.")

    # BLINDAGEM IA: Filtramos apenas atividades que sejam corridas (padrão assumido se antigo)
    historico_corridas = [t for t in historico if t.get('type', 'Run') == 'Run']
    if not historico_corridas: raise HTTPException(status_code=400, detail="Sem corridas cadastradas para análise IA.")

    idade = usuario.get('idade') or "Não informada"
    peso = usuario.get('peso') or "Não informado"
    genero = usuario.get('genero') or "Não informado"
    altura_txt = f"{usuario.get('altura')}cm" if usuario.get('altura') else "Não informada"

    vol_total = 0
    for t in historico_corridas: vol_total += t.get('distancia_km', 0)
    treinos_total = len(historico_corridas)
    
    vol_30d = 0
    bpm_soma = 0
    treinos_30d = 0
    treinos_30_dias_brutos = []
    limite_30d = datetime.now() - timedelta(days=30)
    
    for t in historico_corridas:
        data_str = t.get("start_date_local")
        if data_str:
            dt = datetime.fromisoformat(data_str.replace("Z", "+00:00")[:19])
            if dt >= limite_30d:
                vol_30d += t.get("distancia_km", 0)
                bpm_soma += t.get("average_heartrate", 0)
                treinos_30d += 1
                treinos_30_dias_brutos.append(t)
                
    bpm_med_30d = int(bpm_soma / treinos_30d) if treinos_30d > 0 else 0

    # PROTEÇÃO: Se o corredor não treinou este mês, envia as últimas 3 de sempre
    if not treinos_30_dias_brutos:
        treinos_30_dias_brutos = historico_corridas[:3]

    payload_para_ia = json.dumps(treinos_30_dias_brutos, ensure_ascii=False)
    
    prompt = f"""
    Atue como um Fisiologista do Esporte e Treinador de Corrida de Elite.
    O seu objetivo não é apenas analisar a passada, mas gerar um dossiê profundo cruzando as estatísticas vitais, a carga crônica e a carga aguda.
    
    📋 PERFIL DO ATLETA E BIOMETRIA:
    - Nome: {usuario.get('nome')}
    - Idade: {idade}
    - Sexo: {genero}
    - Peso: {peso}kg
    - Altura: {altura_txt}
    
    📊 CARGA CRÔNICA DE CORRIDA (VISÃO MACRO - Experiência do Atleta):
    - Total de Corridas Registradas: {treinos_total}
    - Distância Total Acumulada em Corrida: {round(vol_total, 1)} km
    
    📈 CARGA AGUDA E BIOMECÂNICA (VISÃO DOS ÚLTIMOS 30 DIAS):
    - Volume dos últimos 30 dias: {round(vol_30d, 1)} km
    - Frequência Cardíaca Média (30d): {bpm_med_30d} BPM
    - Telemetria exata das corridas:
    {payload_para_ia}
    
    INSTRUÇÕES CRÍTICAS:
    1. Use metodologias consagradas (Jack Daniels, Joe Friel, Maffetone 80/20).
    2. Avalie o impacto da Biometria na Cadência (SPM vs Altura).
    3. O tom deve ser de um treinador sênior, provocativo e altamente embasado em dados.
    
    Retorne ESTRITAMENTE um JSON válido contendo exatamente estas chaves, sem blocos de markdown em volta:
    "diagnostico_geral": "Avaliação clínica e fisiológica profunda sobre o mês do atleta cruzando o volume total, o mês atual e a biometria (máximo de 4 linhas).",
    "ponto_de_melhoria": "Insight prático e direto baseado nos padrões repetitivos encontrados neste mês. Pode ser sobre risco de overtraining, falta de volume Z2 ou erro mecânico (passada vs altura).",
    "nota_eficiencia": <Um número inteiro de 0 a 10 que reflita a real economia mecânica e cardiovascular do atleta neste ciclo de 30 dias>
    """
    
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        res_ia = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        
        # BLINDAGEM DE JSON (Limpa blocos de markdown que a IA envia por engano)
        texto_limpo = res_ia.text.strip()
        if texto_limpo.startswith('```json'):
            texto_limpo = texto_limpo.replace('```json', '').replace('```', '').strip()
        elif texto_limpo.startswith('```'):
            texto_limpo = texto_limpo.replace('```', '').strip()
            
        analise = json.loads(texto_limpo)
        
        supabase.table("usuarios_strava").update({"ia_report_json": analise}).eq("id", requisicao.strava_id).execute()
        return analise
    except Exception as e:
        # Devolve exatamente o que falhou para o Frontend poder mostrar no ecrã (Ex: timeout ou API limits)
        raise HTTPException(status_code=500, detail=f"Erro interno de IA: {str(e)}")
