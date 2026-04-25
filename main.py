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

def baixar_novos_treinos(access_token: str, after_timestamp: int = None, incluir_caminhadas: bool = True):
    """
    Sincronização Robusta: Baixa as atividades usando paginação.
    Se for o primeiro acesso, baixa tudo. Se for Delta, baixa apenas os novos.
    Inclui filtro opcional para Caminhadas (Walk).
    """
    url = 'https://www.strava.com/api/v3/athlete/activities'
    headers = {'Authorization': f'Bearer {access_token}'}
    
    atividades_totais = []
    pagina = 1
    
    # Loop de Paginação para baixar todo o histórico necessário
    while True:
        params = {'per_page': 200, 'page': pagina}
        if after_timestamp:
            params['after'] = after_timestamp

        res = requests.get(url, headers=headers, params=params)
        if res.status_code != 200: 
            break
            
        dados_pagina = res.json()
        if not dados_pagina: 
            break
            
        atividades_totais.extend(dados_pagina)
        pagina += 1
        
        # Se a página retornou menos de 200 itens, chegamos ao fim.
        if len(dados_pagina) < 200:
            break

    if not atividades_totais: 
        return []
        
    df_bruto = pd.DataFrame(atividades_totais)
    if 'type' not in df_bruto.columns: 
        return []
        
    # FILTRO: Corridas apenas ou Corridas + Caminhadas
    tipos_permitidos = ['Run', 'Walk'] if incluir_caminhadas else ['Run']
    df_corridas = df_bruto[df_bruto['type'].isin(tipos_permitidos)].copy()
    
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
    """Realiza a troca do code pelos tokens e guarda o utilizador no banco (Perfil Detalhado)."""
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
    
    # Busca o Perfil Detalhado no Strava
    headers = {'Authorization': f'Bearer {access_token}'}
    res_perfil = requests.get('https://www.strava.com/api/v3/athlete', headers=headers)
    atleta = res_perfil.json() if res_perfil.status_code == 200 else atleta_resumo
    
    equipamentos = {
        "tenis": atleta.get('shoes', []),
        "bicicletas": atleta.get('bikes', [])
    }
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
    """Atualiza Peso, Altura e Idade manualmente no Supabase (Atualização Parcial)."""
    update_data = {}
    
    if req.peso is not None:
        update_data["peso"] = req.peso
    if req.altura is not None:
        update_data["altura"] = req.altura
    if req.idade is not None:
        update_data["idade"] = req.idade
        
    if update_data:
        res = supabase.table("usuarios_strava").update(update_data).eq("id", strava_id).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="Atleta não encontrado")
            
    return {"status": "success"}

@app.get("/atleta/{strava_id}")
def obter_dados_atleta(strava_id: int):
    """FONTE DA VERDADE: Entrega Perfil, Estatísticas e Histórico."""
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
def sincronizar_treinos(strava_id: int, caminhadas: bool = True):
    """SINCRONIZAÇÃO INTELIGENTE COM AUTO-CLEANUP (Deduplicação). Aceita filtro de caminhadas."""
    res_db = supabase.table("usuarios_strava").select("*").eq("id", strava_id).execute()
    if not res_db.data: raise HTTPException(status_code=404, detail="Atleta não encontrado.")
    
    atleta_db = res_db.data[0]
    access_token = atualizar_token_strava(atleta_db['refresh_token'])
    
    # 1. Atualizar Perfil Detalhado em Segundo Plano
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
        
        # Puxa o objeto completo para o Frontend
        res_full = supabase.table("usuarios_strava").select("*").eq("id", strava_id).execute()
        perfil_final = res_full.data[0]

    # 2. Sincronização Delta de Treinos
    historico_antigo = atleta_db.get('historico_json') or []
    after_timestamp = None
    
    if historico_antigo:
        data_str = historico_antigo[0].get("start_date_local")
        if data_str:
            dt = datetime.fromisoformat(data_str.replace("Z", "+00:00")[:19])
            after_timestamp = int(dt.timestamp()) + 1 
    
    treinos_novos = baixar_novos_treinos(access_token, after_timestamp, incluir_caminhadas=caminhadas)
    todos_treinos = treinos_novos + historico_antigo if treinos_novos else historico_antigo
    
    # 3. FILTRO DE DEDUPLICAÇÃO BLINDADA
    treinos_unicos = {}
    for t in todos_treinos:
        data_chave = t.get('start_date_local')
        if not caminhadas and t.get('name') and "Walk" in t.get('name', ''):
            pass 
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
    O CÉREBRO DA OPERAÇÃO: "O Funil de Contexto". 
    Manda para o Gemini a biometria, o resumo Macro (total), Meso (30 dias) e Micro (3 treinos crus).
    Gasta poucos tokens, mas fornece um diagnóstico clínico profundo.
    """
    res_db = supabase.table("usuarios_strava").select("*").eq("id", requisicao.strava_id).execute()
    usuario = res_db.data[0]
    historico = usuario.get("historico_json")
    
    if not historico: raise HTTPException(status_code=400, detail="Sem treinos para analisar.")

    # 1. PROCESSAMENTO DE DADOS (Custo Zero de Tokens)
    
    # Biometria
    idade = usuario.get('idade') or "Não informada"
    peso = usuario.get('peso') or "Não informado"
    genero = usuario.get('genero') or "Não informado"
    altura_txt = f"{usuario.get('altura')}cm" if usuario.get('altura') else "Não informada"

    # Visão MACRO (Acumulado da vida)
    vol_total = 0
    for t in historico: vol_total += t.get('distancia_km', 0)
    treinos_total = len(historico)
    
    # Visão MESO (Últimos 30 dias) e MICRO (JSON Completo)
    vol_30d = 0
    bpm_soma = 0
    treinos_30d = 0
    treinos_30_dias_brutos = [] # NOVO: Array para guardar os treinos crus do mês
    limite_30d = datetime.now() - timedelta(days=30)
    
    for t in historico:
        data_str = t.get("start_date_local")
        if data_str:
            dt = datetime.fromisoformat(data_str.replace("Z", "+00:00")[:19])
            if dt >= limite_30d:
                vol_30d += t.get("distancia_km", 0)
                bpm_soma += t.get("average_heartrate", 0)
                treinos_30d += 1
                treinos_30_dias_brutos.append(t) # Guarda o treino na lista para a IA
                
    bpm_med_30d = int(bpm_soma / treinos_30d) if treinos_30d > 0 else 0

    # Agora a IA recebe TODOS os treinos dos últimos 30 dias
    payload_para_ia = json.dumps(treinos_30_dias_brutos, ensure_ascii=False)
    
    # 2. O SUPER PROMPT DE ENGENHARIA
    prompt = f"""
    Atue como um Fisiologista do Esporte e Treinador de Corrida de Elite.
    O seu objetivo não é apenas analisar a passada, mas gerar um dossiê profundo cruzando as estatísticas vitais, a carga crônica e a carga aguda.
    
    📋 PERFIL DO ATLETA E BIOMETRIA:
    - Nome: {usuario.get('nome')}
    - Idade: {idade}
    - Sexo: {genero}
    - Peso: {peso}kg
    - Altura: {altura_txt}
    
    📊 CARGA CRÔNICA (VISÃO MACRO - Experiência do Atleta):
    - Total de Treinos Registrados: {treinos_total}
    - Distância Total Acumulada: {round(vol_total, 1)} km
    
    📈 CARGA AGUDA E BIOMECÂNICA (VISÃO DOS ÚLTIMOS 30 DIAS):
    - Volume dos últimos 30 dias: {round(vol_30d, 1)} km
    - Frequência Cardíaca Média (30d): {bpm_med_30d} BPM
    - Telemetria exata de TODOS os treinos dos últimos 30 dias:
    {payload_para_ia}
    
    INSTRUÇÕES CRÍTICAS:
    1. Use metodologias consagradas (Jack Daniels, Joe Friel, Maffetone 80/20).
    2. Analise a evolução do atleta ao longo deste mês. Há picos de volume (risco de lesão)?
    3. Analise o BPM e a distribuição de esforço. Falta base aeróbica (Z2)?
    4. Avalie a biomecânica (SPM vs Altura vs Pace) cruzando os dados destes 30 dias.
    5. O tom deve ser de um treinador sênior, provocativo e altamente embasado em dados.
    
    Retorne ESTRITAMENTE um JSON válido contendo exatamente estas chaves:
    "diagnostico_geral": "Avaliação clínica e fisiológica profunda sobre o mês do atleta cruzando o volume total, o mês atual e a biometria (máximo de 4 linhas).",
    "ponto_de_melhoria": "Insight prático e direto baseado nos padrões repetitivos encontrados neste mês. Pode ser sobre risco de overtraining, falta de volume Z2 ou erro mecânico (passada vs altura).",
    "nota_eficiencia": <Um número inteiro de 0 a 10 que reflita a real economia mecânica e cardiovascular do atleta neste ciclo de 30 dias>
    """
    
    client = genai.Client(api_key=GEMINI_API_KEY)
    res_ia = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    analise = json.loads(res_ia.text)
    
    supabase.table("usuarios_strava").update({"ia_report_json": analise}).eq("id", requisicao.strava_id).execute()
    return analise
