"""
test_aplogs.py
==============
    pip install -r requirements.txt pytest httpx
    python -m pytest tests -q
"""

from datetime import timedelta

from sqlalchemy import select

from app import monitor
from app.db import sessao
from app.modelos import Servico

SVC = "rpa-procuracoes-eletronicas"


def _envelhecer_heartbeat(slug: str, segundos: int):
    with sessao() as db:
        s = db.scalar(select(Servico).where(Servico.slug == slug))
        s.ultimo_heartbeat_em = s.ultimo_heartbeat_em - timedelta(seconds=segundos)
        db.commit()


# ── Autenticação ─────────────────────────────────────────────────────────────

def test_health_publico(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"]


def test_ingestao_exige_chave(client):
    assert client.post("/api/ingest/heartbeat", json={"servico": SVC}).status_code == 401
    assert client.post("/api/ingest/heartbeat", json={"servico": SVC}, headers={"x-api-key": "x"}).status_code == 401


def test_painel_exige_login(client, ingest):
    assert client.get("/api/painel").status_code == 401
    assert client.get("/api/painel", headers={"x-api-secret": "errada"}).status_code == 401
    # a chave de ingestão não abre o painel
    assert client.get("/api/painel", headers={"x-api-secret": ingest["x-api-key"]}).status_code == 401


def test_login_cookie(client):
    assert client.post("/api/auth/login", json={"secret": "errada"}).status_code == 401
    r = client.post("/api/auth/login", json={"secret": "senha-teste"})
    assert r.status_code == 200 and "aplogs_sessao" in r.cookies
    assert client.get("/api/auth/sessao").status_code == 200
    client.post("/api/auth/logout")
    client.cookies.clear()
    assert client.get("/api/auth/sessao").status_code == 401


def test_frontend_servido(client):
    r = client.get("/")
    assert r.status_code == 200 and "aplogs" in r.text


# ── Heartbeat e monitor ──────────────────────────────────────────────────────

def test_heartbeat_cadastra_servico(client, ingest, painel):
    r = client.post("/api/ingest/heartbeat", headers=ingest, json={
        "servico": SVC, "nome": "RPA Procurações Eletrônicas", "versao": "2.5.0", "host": "vm-receita",
        "intervalo_s": 30, "status": "ok", "dados": {"sessoes": {"ativas": 9, "total": 9}}})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "online"
    p = client.get("/api/painel", headers=painel).json()
    s = next(x for x in p["servicos"] if x["slug"] == SVC)
    assert s["nome"] == "RPA Procurações Eletrônicas" and s["status"] == "online"
    assert s["timeline"][-1] == 1 and len(s["timeline"]) == 60
    assert s["dados"]["sessoes"]["ativas"] == 9


def test_offline_abre_problema_e_volta(client, ingest, painel):
    client.post("/api/ingest/heartbeat", headers=ingest, json={"servico": SVC, "intervalo_s": 30})
    _envelhecer_heartbeat(SVC, 200)                  # tolerância = max(90, 60) = 90 s
    assert monitor.rodada_status() >= 1
    det = client.get(f"/api/servicos/{SVC}", headers=painel).json()
    assert det["servico"]["status"] == "offline"
    offline = [p for p in det["problemas"] if p["chave"] == "aplogs:offline"]
    assert offline and offline[0]["severidade"] == "critico" and offline[0]["origem"] == "monitor"
    # rodar de novo não duplica
    monitor.rodada_status()
    assert len([p for p in client.get(f"/api/servicos/{SVC}", headers=painel).json()["problemas"]
                if p["chave"] == "aplogs:offline"]) == 1
    # volta
    r = client.post("/api/ingest/heartbeat", headers=ingest, json={"servico": SVC})
    assert r.json()["status"] == "online"
    det = client.get(f"/api/servicos/{SVC}", headers=painel).json()
    assert not [p for p in det["problemas"] if p["chave"] == "aplogs:offline"]
    logs = client.get("/api/logs", headers=painel, params={"servico": SVC, "modulo": "aplogs"}).json()["logs"]
    textos = " ".join(l["mensagem"] for l in logs)
    assert "offline" in textos and "voltou a ficar online" in textos


def test_parada_normal_fica_offline(client, ingest, painel):
    client.post("/api/ingest/heartbeat", headers=ingest, json={"servico": "robo-parando", "status": "parando"})
    s = client.get("/api/servicos/robo-parando", headers=painel).json()
    assert s["servico"]["status"] == "offline"
    assert "parado" in s["problemas"][0]["titulo"]


def test_degradado(client, ingest, painel):
    r = client.post("/api/ingest/heartbeat", headers=ingest,
                    json={"servico": "robo-degradado", "status": "degradado", "mensagem": "3/9 sessões inativas"})
    assert r.json()["status"] == "degradado"


def test_pausar_monitoramento(client, ingest, painel):
    client.post("/api/ingest/heartbeat", headers=ingest, json={"servico": "robo-pausa"})
    _envelhecer_heartbeat("robo-pausa", 500)
    monitor.rodada_status()
    assert client.get("/api/servicos/robo-pausa", headers=painel).json()["servico"]["status"] == "offline"
    r = client.patch("/api/servicos/robo-pausa", headers=painel, json={"monitorar": False, "nome": "Robô Pausa"})
    assert r.json()["servico"]["status"] == "pausado"
    det = client.get("/api/servicos/robo-pausa", headers=painel).json()
    assert not det["problemas"]
    # nome editado no painel não é trocado pelo heartbeat
    client.post("/api/ingest/heartbeat", headers=ingest, json={"servico": "robo-pausa", "nome": "outro"})
    assert client.get("/api/servicos/robo-pausa", headers=painel).json()["servico"]["nome"] == "Robô Pausa"


def test_slug_invalido(client, ingest):
    r = client.post("/api/ingest/heartbeat", headers=ingest, json={"servico": "!!!"})
    assert r.status_code == 400


# ── Logs ─────────────────────────────────────────────────────────────────────

def test_logs_filtros_e_paginacao(client, ingest, painel):
    linhas = [{"mensagem": f"linha {i}", "nivel": "INFO", "modulo": "fila"} for i in range(30)]
    linhas += [{"mensagem": "deu ruim", "nivel": "erro", "modulo": "rpa_proc", "exc": "Traceback: KeyError",
                "ts": "2026-11-10T12:00:00-03:00", "contexto": {"cert": 3}},
               {"mensagem": "cuidado", "nivel": "warn"}]
    r = client.post("/api/ingest/logs", headers=ingest, json={"servico": "robo-logs", "logs": linhas})
    assert r.json()["recebidos"] == 32

    todos = client.get("/api/logs", headers=painel, params={"servico": "robo-logs", "limite": 10}).json()
    assert len(todos["logs"]) == 10 and todos["logs"][0]["mensagem"] == "cuidado"
    mais = client.get("/api/logs", headers=painel,
                      params={"servico": "robo-logs", "limite": 10, "antes": todos["logs"][-1]["id"]}).json()
    assert mais["logs"][0]["id"] < todos["logs"][-1]["id"]

    erros = client.get("/api/logs", headers=painel, params={"servico": "robo-logs", "nivel": "ERROR"}).json()["logs"]
    assert len(erros) == 1 and erros[0]["nivel"] == "ERROR" and erros[0]["ts"] == "2026-11-10T15:00:00Z"
    assert erros[0]["contexto"] == {"cert": 3}
    busca = client.get("/api/logs", headers=painel, params={"q": "KeyError"}).json()["logs"]
    assert len(busca) == 1
    avisos = client.get("/api/logs", headers=painel, params={"servico": "robo-logs", "nivel": "WARNING"}).json()
    assert {l["nivel"] for l in avisos["logs"]} == {"WARNING", "ERROR"}
    novos = client.get("/api/logs", headers=painel, params={"depois": todos["ultimo_id"]}).json()["logs"]
    assert novos == []
    mods = client.get("/api/logs/modulos", headers=painel, params={"servico": "robo-logs"}).json()["modulos"]
    assert mods == ["aplogs", "fila", "rpa_proc"]           # "aplogs" = "serviço conectado"
    p = client.get("/api/painel", headers=painel).json()
    s = next(x for x in p["servicos"] if x["slug"] == "robo-logs")
    assert s["erros_1h"] == 1 and s["avisos_1h"] == 1 and s["status"] == "desconhecido"


# ── Problemas ────────────────────────────────────────────────────────────────

def test_problemas_eventos_e_estado(client, ingest, painel):
    url = "/api/ingest/problemas"
    # evento avulso: cada envio soma ocorrência
    for _ in range(3):
        client.post(url, headers=ingest, json={"servico": "robo-prob", "problemas": [
            {"chave": "banco:conexao", "titulo": "Sem conexão com o banco", "severidade": "erro"}]})
    probs = client.get("/api/problemas", headers=painel, params={"servico": "robo-prob"}).json()["problemas"]
    assert len(probs) == 1 and probs[0]["ocorrencias"] == 3

    # estado: sessões 3 e 5 com problema
    corpo = {"servico": "robo-prob", "sincronizar": ["sessao:"], "problemas": [
        {"chave": "sessao:3", "titulo": "Sessão inativa: Studio Varejo", "severidade": "aviso"},
        {"chave": "sessao:5", "titulo": "Sessão inativa: Audit", "severidade": "aviso"}]}
    client.post(url, headers=ingest, json=corpo)
    client.post(url, headers=ingest, json=corpo)               # repetido: não soma
    abertos = {p["chave"]: p for p in client.get("/api/problemas", headers=painel,
                                                 params={"servico": "robo-prob"}).json()["problemas"]}
    assert set(abertos) == {"banco:conexao", "sessao:3", "sessao:5"} and abertos["sessao:3"]["ocorrencias"] == 1

    # sessão 3 voltou: só a 5 continua; o problema do banco (outro prefixo) não é mexido
    client.post(url, headers=ingest, json={**corpo, "problemas": corpo["problemas"][1:]})
    abertos = {p["chave"] for p in client.get("/api/problemas", headers=painel,
                                              params={"servico": "robo-prob"}).json()["problemas"]}
    assert abertos == {"banco:conexao", "sessao:5"}

    # resolver explícito + reconhecer/resolver pelo painel
    client.post(url, headers=ingest, json={"servico": "robo-prob", "problemas": [{"chave": "banco:conexao",
                                                                                  "resolvido": True}]})
    pid = client.get("/api/problemas", headers=painel, params={"servico": "robo-prob"}).json()["problemas"][0]["id"]
    assert client.post(f"/api/problemas/{pid}/reconhecer", headers=painel).json()["problema"]["status"] == "reconhecido"
    assert client.post(f"/api/problemas/{pid}/resolver", headers=painel).json()["problema"]["resolvido_por"] == "painel"
    resolvidos = client.get("/api/problemas", headers=painel,
                            params={"servico": "robo-prob", "status": "resolvidos"}).json()["problemas"]
    assert len(resolvidos) == 3


# ── Jobs e filas ─────────────────────────────────────────────────────────────

def test_jobs_upsert(client, ingest, painel):
    url = "/api/ingest/jobs"
    client.post(url, headers=ingest, json={"servico": "robo-jobs", "jobs": [
        {"id": "exec-1", "tipo": "rpa_proc", "nome": "Execução agendada", "status": "running", "progresso": 10,
         "etapa": "Studio Agro · Em Análise", "iniciado_em": "2026-11-10T10:00:00Z"}]})
    client.post(url, headers=ingest, json={"servico": "robo-jobs", "jobs": [
        {"id": "exec-1", "status": "concluida", "terminado_em": "2026-11-10T10:05:30Z", "mensagem": "42 itens"}]})
    jobs = client.get("/api/jobs", headers=painel, params={"servico": "robo-jobs"}).json()
    j = jobs["jobs"][0]
    assert len(jobs["jobs"]) == 1 and j["status"] == "ok" and j["duracao_s"] == 330.0 and j["progresso"] == 100
    assert j["nome"] == "Execução agendada" and j["etapa"] is None and jobs["tipos"] == ["rpa_proc"]
    assert client.get("/api/jobs", headers=painel, params={"status": "erro"}).json()["jobs"] == []


def test_filas(client, ingest, painel):
    client.post("/api/ingest/filas", headers=ingest, json={"servico": "robo-filas", "filas": [
        {"nome": "busca_cnpj", "pendentes": 120, "rodando": 4, "concluidos": 30, "erros": 1, "workers": 4}]})
    client.post("/api/ingest/filas", headers=ingest, json={"servico": "robo-filas", "filas": [
        {"nome": "busca_cnpj", "pendentes": 80, "rodando": 4, "concluidos": 70, "erros": 1, "workers": 4}]})
    f = client.get("/api/filas", headers=painel, params={"servico": "robo-filas"}).json()["filas"]
    assert len(f) == 1 and f[0]["pendentes"] == 80 and len(f[0]["amostras"]) == 1   # 2ª amostra < 20 s
    p = client.get("/api/painel", headers=painel).json()
    assert next(x for x in p["servicos"] if x["slug"] == "robo-filas")["filas"][0]["nome"] == "busca_cnpj"


def test_apagar_servico(client, ingest, painel):
    client.post("/api/ingest/logs", headers=ingest, json={"servico": "robo-apagar", "logs": [{"mensagem": "x"}]})
    client.post("/api/ingest/filas", headers=ingest, json={"servico": "robo-apagar", "filas": [{"nome": "f"}]})
    assert client.delete("/api/servicos/robo-apagar", headers=painel).json()["ok"]
    assert client.get("/api/servicos/robo-apagar", headers=painel).status_code == 404


def test_limpeza_nao_quebra(client):
    assert isinstance(monitor.limpeza(), dict)


# ── Integração fácil ─────────────────────────────────────────────────────────

def test_chave_por_bearer_e_query(client):
    corpo = {"servico": "facil-chave"}
    assert client.post("/api/ingest/heartbeat", json=corpo,
                       headers={"Authorization": "Bearer chave-teste"}).status_code == 200
    assert client.post("/api/ingest/heartbeat?key=chave-teste", json=corpo).status_code == 200
    assert client.post("/api/ingest/heartbeat", json=corpo,
                       headers={"Authorization": "Bearer errada"}).status_code == 401
    assert client.post("/api/ingest/heartbeat?key=errada", json=corpo).status_code == 401


def test_log_avulso_sem_lista(client, ingest, painel):
    r = client.post("/api/ingest/logs", headers=ingest,
                    json={"servico": "facil-log", "mensagem": "uma linha só", "nivel": "erro"})
    assert r.status_code == 200 and r.json()["recebidos"] == 1
    logs = client.get("/api/logs?servico=facil-log&nivel=ERROR", headers=painel).json()["logs"]
    assert [l["mensagem"] for l in logs] == ["uma linha só"] and logs[0]["nivel"] == "ERROR"
    # sem mensagem e sem lista: erro de validação
    assert client.post("/api/ingest/logs", headers=ingest, json={"servico": "facil-log"}).status_code == 422


def test_heartbeat_pela_url(client, painel):
    r = client.get("/api/ingest/heartbeat/facil-cron?key=chave-teste&intervalo_s=300&nome=Cron%20X")
    assert r.status_code == 200 and r.json()["status"] == "online"
    s = client.get("/api/servicos/facil-cron", headers=painel).json()["servico"]
    assert s["nome"] == "Cron X" and s["intervalo_s"] == 300
    assert client.post("/api/ingest/heartbeat/facil-cron?key=chave-teste&status=degradado").json()["status"] == "degradado"
    assert client.get("/api/ingest/heartbeat/facil-cron").status_code == 401
    assert client.get("/api/ingest/heartbeat/facil-cron?key=chave-teste&intervalo_s=0").status_code == 422


# ── Repositório (projetos) ───────────────────────────────────────────────────

def test_repositorio_pelo_heartbeat(client, ingest, painel):
    r = client.post("/api/ingest/heartbeat", headers=ingest, json={
        "servico": "proj-a", "repositorio": "git@github.com:Grupo-Studio/aplogin.git",
        "branch": "Receita-aplogs", "commit": "39d86b0f3e2a1c"})
    assert r.status_code == 200
    s = client.get("/api/servicos/proj-a", headers=painel).json()["servico"]
    assert s["repositorio"] == "https://github.com/Grupo-Studio/aplogin"
    assert s["branch"] == "Receita-aplogs" and s["commit"] == "39d86b0f3e2a1c"
    # o heartbeat não troca o repositório que já existe; branch/commit sempre atualizam
    client.post("/api/ingest/heartbeat", headers=ingest, json={
        "servico": "proj-a", "repositorio": "outro/repo", "commit": "abcdef1"})
    s = client.get("/api/servicos/proj-a", headers=painel).json()["servico"]
    assert s["repositorio"] == "https://github.com/Grupo-Studio/aplogin" and s["commit"] == "abcdef1"
    # repositório estranho não derruba o heartbeat
    assert client.post("/api/ingest/heartbeat", headers=ingest,
                       json={"servico": "proj-b", "repositorio": "javascript:alert(1)"}).status_code == 200
    assert client.get("/api/servicos/proj-b", headers=painel).json()["servico"]["repositorio"] is None


def test_repositorio_pelo_painel(client, ingest, painel):
    client.post("/api/ingest/heartbeat", headers=ingest, json={"servico": "proj-c"})
    r = client.patch("/api/servicos/proj-c", headers=painel, json={"repositorio": "Grupo-Studio/aplogs"})
    assert r.status_code == 200 and r.json()["servico"]["repositorio"] == "https://github.com/Grupo-Studio/aplogs"
    assert client.patch("/api/servicos/proj-c", headers=painel,
                        json={"repositorio": "javascript:alert(1)"}).status_code == 422
    r = client.patch("/api/servicos/proj-c", headers=painel, json={"repositorio": ""})
    assert r.json()["servico"]["repositorio"] is None
    assert any(s["slug"] == "proj-c" and "repositorio" in s
               for s in client.get("/api/painel", headers=painel).json()["servicos"])
