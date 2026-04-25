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

# Carregamento das variáveis de ambiente
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

# ==========================================
# ⚙️ FUNÇÕES AUXILIARES & REGRAS DE NEGÓCIO
# ==========================================

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

def baixar_novos_treinos(access_token: str, after_timestamp: int = None):
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
        
    df_atividades = df_bruto[df_bruto['type'].isin(['Run', 'Walk'])].copy()
    
    if df_atividades.empty: return []
        
    df_atividades['distancia_km'] = df_atividades['distance'] / 1000.0
    
    # Tratamento Seguro do workout_type (Padrão 0 = Comum, 1 = Prova)
    if 'workout_type' not in df_atividades.columns:
        df_atividades['workout_type'] = 0
    else:
        df_atividades['workout_type'] = df_atividades['workout_type'].fillna(0).astype(int)
    
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

    colunas = ['id', 'type', 'workout_type', 'name', 'distancia_km', 'Pace_Medio', 'Cadence_SPM', 'average_heartrate', 'total_elevation_gain', 'moving_time', 'start_date_local']
    
    json_string = df_atividades[colunas].to_json(orient='records', force_ascii=False, date_format='iso')
    return json.loads(json_string)

def construir_perfil_seguro(dados: dict) -> dict:
    equip = dados.get("equipamentos")
    if not isinstance(equip, dict):
        equip = {"tenis": [], "bicicletas": []}
    
    return {
        "nome": dados.get("nome") or "Atleta",
        "sobrenome": dados.get("sobrenome") or "",
        "foto_url": dados.get("foto_url") or "",
        "peso": dados.get("peso"),
        "altura": dados.get("altura"),
        "idade": dados.get("idade"),
        "cidade": dados.get("cidade") or "",
        "estado": dados.get("estado") or "",
        "genero": dados.get("genero") or "",
        "data_criacao": dados.get("data_criacao") or "",
        "bio": dados.get("bio") or "",
        "clubes": dados.get("clubes") or [],
        "equipamentos": equip
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
    
    equip_raw = atleta.get('shoes') or []
    bikes_raw = atleta.get('bikes') or []
    equipamentos = {"tenis": equip_raw, "bicicletas": bikes_raw}
    
    clubes_raw = atleta.get('clubs') or []
    clubes = [{"nome": c.get("name"), "foto": c.get("profile")} for c in clubes_raw]
    
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
        "perfil": construir_perfil_seguro(dados),
        "historico_json": historico,
        "ia_report_json": dados.get("ia_report_json"),
        "trofeus_json": dados.get("trofeus_json") or {}
    }

@app.post("/atleta/{strava_id}/sincronizar")
def sincronizar_treinos(strava_id: int):
    res_db = supabase.table("usuarios_strava").select("*").eq("id", strava_id).execute()
    if not res_db.data: raise HTTPException(status_code=404, detail="Atleta não encontrado.")
    
    atleta_db = res_db.data[0]
    access_token = atualizar_token_strava(atleta_db['refresh_token'])
    
    headers = {'Authorization': f'Bearer {access_token}'}
    res_perfil = requests.get('https://www.strava.com/api/v3/athlete', headers=headers)
    
    if res_perfil.status_code == 200:
        s = res_perfil.json()
        equipamentos = {"tenis": s.get('shoes') or [], "bicicletas": s.get('bikes') or []}
        clubes = [{"nome": c.get("name"), "foto": c.get("profile")} for c in (s.get('clubs') or [])]
        
        perfil_upd = {
            "cidade": s.get('city'), "estado": s.get('state'),
            "equipamentos": equipamentos, "clubes": clubes, "foto_url": s.get('profile')
        }
        
        if s.get('weight'): perfil_upd["peso"] = s.get('weight')
        supabase.table("usuarios_strava").update(perfil_upd).eq("id", strava_id).execute()

    # GATILHO DE AUTOCURA: Verifica se o histórico atual está faltando os IDs ou o workout_type
    historico_antigo = atleta_db.get('historico_json') or []
    precisa_reconstruir = False
    
    if historico_antigo:
        # CORREÇÃO: Em vez de olhar só para o primeiro treino, 
        # verifica se ALGUM treino em todo o histórico está sem a tag.
        if any('workout_type' not in t or 'id' not in t for t in historico_antigo):
            precisa_reconstruir = True

    after_timestamp = None
    
    if precisa_reconstruir:
        # Esvazia a memória local para o algoritmo puxar TUDO do Strava do zero
        historico_antigo = []
    elif historico_antigo:
        try:
            data_str = historico_antigo[0].get("start_date_local")
            dt = datetime.fromisoformat(data_str.replace("Z", "+00:00")[:19])
            after_timestamp = int(dt.timestamp()) + 1 
        except: pass
            
    treinos_novos = baixar_novos_treinos(access_token, after_timestamp)
    todos_treinos = treinos_novos + historico_antigo if treinos_novos else historico_antigo

    treinos_unicos = {}
    for t in todos_treinos:
        data_chave = t.get('start_date_local')
        if data_chave not in treinos_unicos:
            treinos_unicos[data_chave] = t
            
    historico_atualizado = sorted(list(treinos_unicos.values()), key=lambda x: x.get('start_date_local', ''), reverse=True)
    
    if treinos_novos or precisa_reconstruir or len(historico_atualizado) != len(historico_antigo):
        supabase.table("usuarios_strava").update({"historico_json": historico_atualizado}).eq("id", strava_id).execute()
    
    res_final = supabase.table("usuarios_strava").select("*").eq("id", strava_id).execute()
    final_db = res_final.data[0]

    return {
        "status": "success", 
        "novos": len(treinos_novos),
        "historico_json": historico_atualizado,
        "perfil": construir_perfil_seguro(final_db)
    }

@app.post("/ia/analise")
def gerar_analise_ia(requisicao: IAAnaliseRequest):
    res_db = supabase.table("usuarios_strava").select("*").eq("id", requisicao.strava_id).execute()
    usuario = res_db.data[0]
    historico = usuario.get("historico_json")
    
    if not historico: raise HTTPException(status_code=400, detail="Sem treinos para analisar.")

    historico_corridas = [t for t in historico if t.get('type', 'Run') == 'Run']
    if not historico_corridas: raise HTTPException(status_code=400, detail="Sem corridas cadastradas para análise IA.")

    idade = usuario.get('idade') or "Não informada"
    peso = usuario.get('peso') or "Não informado"
    genero = usuario.get('genero') or "Não informado"
    altura_txt = f"{usuario.get('altura')}cm" if usuario.get('altura') else "Não informada"

    vol_total = sum(t.get('distancia_km', 0) for t in historico_corridas)
    treinos_total = len(historico_corridas)
    
    vol_30d = 0
    bpm_soma = 0
    treinos_30d = 0
    treinos_30_dias_brutos = []
    limite_30d = datetime.now() - timedelta(days=30)
    
    for t in historico_corridas:
        try:
            dt = datetime.fromisoformat(t.get("start_date_local").replace("Z", "+00:00")[:19])
            if dt >= limite_30d:
                vol_30d += t.get("distancia_km", 0)
                bpm_soma += t.get("average_heartrate", 0)
                treinos_30d += 1
                treinos_30_dias_brutos.append(t)
        except: pass
                
    bpm_med_30d = int(bpm_soma / treinos_30d) if treinos_30d > 0 else 0
    if not treinos_30_dias_brutos: treinos_30_dias_brutos = historico_corridas[:3]

    resumo_treinos = []
    for t in treinos_30_dias_brutos:
        linha = f"[{t.get('start_date_local', '')[:10]}] {round(t.get('distancia_km', 0), 1)}km | Pace: {t.get('Pace_Medio', '00:00')} | BPM: {int(t.get('average_heartrate', 0))} | SPM: {int(t.get('Cadence_SPM', 0))}"
        resumo_treinos.append(linha)
        
    payload_para_ia = "\n".join(resumo_treinos)
    
    prompt = f"""
    Atue como um Fisiologista do Esporte e Treinador de Corrida de Elite.
    📋 PERFIL DO ATLETA:
    - Nome: {usuario.get('nome')} | Idade: {idade} | Sexo: {genero} | Peso: {peso}kg | Altura: {altura_txt}
    
    📊 CARGA CRÔNICA: {treinos_total} treinos | {round(vol_total, 1)} km
    📈 ÚLTIMOS 30 DIAS: {round(vol_30d, 1)} km | {bpm_med_30d} BPM médio
    
    👟 TELEMETRIA TÁTICA:
    {payload_para_ia}
    
    Analise biomecânica e cardíaca profunda (Jack Daniels/Joe Friel).
    Retorne JSON: "diagnostico_geral" (4 linhas), "ponto_de_melhoria" (Insight prático), "nota_eficiencia" (0-10).
    """
    
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        res_ia = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        texto_limpo = res_ia.text.strip().replace('```json', '').replace('```', '').strip()
        analise = json.loads(texto_limpo)
        supabase.table("usuarios_strava").update({"ia_report_json": analise}).eq("id", requisicao.strava_id).execute()
        return analise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro interno de IA: {str(e)}")

# ==========================================
# NOVO MÓDULO: O ALGORITMO GARIMPEIRO (SALA DE TROFÉUS)
# ==========================================
@app.post("/trofeus/garimpar/{strava_id}")
def garimpar_trofeus(strava_id: int, req: TrofeusRequest):
    res_db = supabase.table("usuarios_strava").select("*").eq("id", strava_id).execute()
    if not res_db.data: raise HTTPException(status_code=404, detail="Atleta não encontrado.")
    
    usuario = res_db.data[0]
    
    try:
        access_token = atualizar_token_strava(usuario['refresh_token'])
    except Exception:
        raise HTTPException(status_code=401, detail="Falha ao renovar token. Faça login novamente.")

    historico = usuario.get("historico_json") or []
    
    corridas = [t for t in historico if t.get('type') == 'Run' and t.get('id') is not None]
    
    if req.somente_provas:
        alvos = [t for t in corridas if t.get('workout_type') == 1]
    else:
        alvos = sorted(corridas, key=lambda x: x.get('distancia_km', 0), reverse=True)[:20]
        
    # Devolve o número 0 para "analisados" caso a lista esteja vazia, para não quebrar o Frontend
    if not alvos:
        return {
            "status": "success", 
            "analisados": 0, 
            "trofeus": usuario.get("trofeus_json") or {}, 
            "msg": "Nenhuma Prova encontrada no banco. DICA: Faça uma nova sincronização do histórico para puxar as etiquetas oficiais do Strava."
        }

    headers = {'Authorization': f'Bearer {access_token}'}
    
    trofeus = usuario.get("trofeus_json") or {}
    distancias_alvo = ["1k", "5k", "10k", "Half Marathon", "Marathon"]
    for dist in distancias_alvo:
        if dist not in trofeus:
            trofeus[dist] = None
            
    for treino in alvos:
        act_id = treino.get('id')
        res = requests.get(f'https://www.strava.com/api/v3/activities/{act_id}', headers=headers)
        if res.status_code != 200: continue
        
        detalhes = res.json()
        best_efforts = detalhes.get('best_efforts') or []
        
        for effort in best_efforts:
            nome_esforco = effort.get('name')
            if nome_esforco in distancias_alvo:
                tempo_atual = effort.get('elapsed_time')
                
                if trofeus[nome_esforco] is None or tempo_atual < trofeus[nome_esforco]['tempo_segundos']:
                    h = int(tempo_atual // 3600)
                    m = int((tempo_atual % 3600) // 60)
                    s = int(tempo_atual % 60)
                    
                    if h > 0:
                        tempo_formatado = f"{h}h {m:02d}m {s:02d}s"
                    else:
                        tempo_formatado = f"{m}m {s:02d}s"

                    trofeus[nome_esforco] = {
                        "tempo_segundos": tempo_atual,
                        "tempo_formatado": tempo_formatado,
                        "nome_treino": treino.get('name'),
                        "data": treino.get('start_date_local'),
                        "id_treino": act_id
                    }
                    
    supabase.table("usuarios_strava").update({"trofeus_json": trofeus}).eq("id", strava_id).execute()
    
    return {
        "status": "success",
        "analisados": len(alvos),
        "trofeus": trofeus
    }
