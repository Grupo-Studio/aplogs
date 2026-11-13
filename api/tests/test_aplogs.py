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


def test_login_central(client):
    from conftest import apusers
    apusers("/_mock/limpar", {})
    client.cookies.clear()
    r = client.post("/api/auth/login", json={"email": "ana@exemplo.test", "senha": "errada"})
    assert r.status_code == 401 and r.json()["codigo"] == "credenciais_invalidas"
    assert isinstance(r.json()["error"], str)
    r = client.post("/api/auth/login", json={"email": "Ana@Exemplo.test", "senha": "senha-ana"})
    assert r.status_code == 200 and "aplogs_sessao" in r.cookies
    assert r.json()["usuario"]["papel"] == "operador"
    assert "ana@exemplo.test" not in r.cookies["aplogs_sessao"]          # o cookie leva só o id
    assert "at-" not in r.cookies["aplogs_sessao"]
    eu = client.get("/api/auth/sessao").json()
    assert eu["usuario"]["permissoes"] == ["tratar", "ver"]
    assert {p["role"] for p in eu["papeis"]} == {"visualizador", "operador", "gestor"}
    assert apusers("/_mock/estado")["ips"][-1] == "testclient"            # IP de quem está no navegador
    client.post("/api/auth/logout", headers={"X-Web": "1"})
    assert apusers("/_mock/estado")["logouts"] == ["ana@exemplo.test"]
    assert client.get("/api/auth/sessao").status_code == 401
    assert client.get("/api/painel").json()["codigo"] == "nao_autenticado"


def test_login_sem_acesso_e_pendente(client):
    client.cookies.clear()
    r = client.post("/api/auth/login", json={"email": "bruno@exemplo.test", "senha": "senha-bruno"})
    assert r.status_code == 403 and r.json()["codigo"] == "acesso_restrito" and r.json()["pedido"]["id"] == 7
    r = client.post("/api/auth/login", json={"email": "paula@exemplo.test", "senha": "senha-paula"})
    assert r.status_code == 403 and r.json()["codigo"] == "usuario_pendente"
    r = client.post("/api/auth/cadastro", json={"email": "nova@exemplo.test", "nome": "Nova Pessoa",
                                                "senha": "uma-senha-boa", "area": "Fiscal"})
    assert r.status_code == 202 and r.json()["pending"] is True
    from conftest import apusers
    c = apusers("/_mock/estado")["cadastros"][-1]
    assert c["email"] == "nova@exemplo.test" and c["area"] == "Fiscal" and c["ip"] == "testclient"


def test_papeis_declarados(client):
    from conftest import apusers
    from app import acesso
    acesso.declarar_papeis()             # roda numa thread ao subir; aqui, na mão
    assert [p["role"] for p in apusers("/_mock/estado")["papeis"]] == ["visualizador", "operador", "gestor"]


def test_token_vencido_renova_uma_vez(client):
    from conftest import apusers, entrar
    from app import acesso
    entrar(client, "vera")
    antes = apusers("/_mock/estado")["refresh_usados"]
    apusers("/_mock/expirar", {"email": "vera@exemplo.test"})
    acesso.cliente()._cache.clear()
    assert client.get("/api/painel").status_code == 200
    assert client.get("/api/painel").status_code == 200
    assert apusers("/_mock/estado")["refresh_usados"] == antes + 1


def test_acesso_revogado_derruba_sessao(client):
    from conftest import apusers, entrar
    from app import acesso
    entrar(client, "gil")
    apusers("/_mock/revogar", {"email": "gil@exemplo.test"})
    acesso.cliente()._cache.clear()
    r = client.get("/api/painel")
    assert r.status_code == 403 and r.json()["codigo"] == "acesso_restrito"
    assert client.get("/api/painel").json()["codigo"] == "nao_autenticado"     # a sessão foi descartada
    apusers("/_mock/limpar", {})


def test_secret_de_script(client, painel):
    assert client.get("/api/auth/sessao", headers=painel).json()["script"] is True
    r = client.get("/api/painel", headers={"x-api-secret": "errada"})
    assert r.status_code == 401 and r.json()["codigo"] == "secret_invalido"


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
    # repositório novo no heartbeat (o .env do serviço mudou): troca e fica registrado no log
    client.post("/api/ingest/heartbeat", headers=ingest, json={
        "servico": "proj-a", "repositorio": "outro/repo", "commit": "abcdef1"})
    s = client.get("/api/servicos/proj-a", headers=painel).json()["servico"]
    assert s["repositorio"] == "https://github.com/outro/repo" and s["commit"] == "abcdef1"
    logs = client.get("/api/logs?servico=proj-a", headers=painel).json()
    logs = logs.get("logs", logs) if isinstance(logs, dict) else logs
    assert any("outro repositório" in l["mensagem"] and "Grupo-Studio/aplogin" in l["mensagem"] for l in logs)
    # heartbeat sem repositório não apaga o que existe
    client.post("/api/ingest/heartbeat", headers=ingest, json={"servico": "proj-a"})
    assert client.get("/api/servicos/proj-a", headers=painel).json()["servico"]["repositorio"] \
        == "https://github.com/outro/repo"
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


# ── Endereço do serviço na rede ──────────────────────────────────────────────

def test_endereco_pelo_heartbeat(client, ingest, painel):
    r = client.post("/api/ingest/heartbeat", headers=ingest, json={
        "servico": "end-a", "endereco": "http://192.168.1.100:3067/",
        "repositorio": "https://github.com/Grupo-Studio/aprap"})
    assert r.status_code == 200
    s = client.get("/api/servicos/end-a", headers=painel).json()["servico"]
    assert s["endereco"] == "http://192.168.1.100:3067"
    assert s["repositorio"] == "https://github.com/Grupo-Studio/aprap"
    # o heartbeat sempre atualiza (é onde o serviço está agora); sem esquema vira http://
    client.post("/api/ingest/heartbeat", headers=ingest, json={"servico": "end-a", "endereco": "10.0.0.5:3080"})
    assert client.get("/api/servicos/end-a", headers=painel).json()["servico"]["endereco"] == "http://10.0.0.5:3080"
    # heartbeat sem endereço não apaga; endereço estranho não derruba o heartbeat nem troca o que existe
    client.post("/api/ingest/heartbeat", headers=ingest, json={"servico": "end-a"})
    for ruim in ("javascript:alert(1)", "ftp://10.0.0.5", "http://x\"><script>"):
        assert client.post("/api/ingest/heartbeat", headers=ingest,
                           json={"servico": "end-a", "endereco": ruim}).status_code == 200
    assert client.get("/api/servicos/end-a", headers=painel).json()["servico"]["endereco"] == "http://10.0.0.5:3080"
    # heartbeat pela URL também aceita
    client.get("/api/ingest/heartbeat/end-b?key=chave-teste&endereco=https%3A%2F%2Fapi.interna%3A8443")
    assert client.get("/api/servicos/end-b", headers=painel).json()["servico"]["endereco"] == "https://api.interna:8443"


def test_endereco_pelo_painel(client, ingest, painel):
    client.post("/api/ingest/heartbeat", headers=ingest, json={"servico": "end-c"})
    r = client.patch("/api/servicos/end-c", headers=painel, json={"endereco": "192.168.1.100:3067"})
    assert r.status_code == 200 and r.json()["servico"]["endereco"] == "http://192.168.1.100:3067"
    assert client.patch("/api/servicos/end-c", headers=painel, json={"endereco": "javascript:alert(1)"}).status_code == 422
    assert client.patch("/api/servicos/end-c", headers=painel, json={"endereco": ""}).json()["servico"]["endereco"] is None
    assert any(s["slug"] == "end-c" and "endereco" in s
               for s in client.get("/api/painel", headers=painel).json()["servicos"])


# ── Login central: permissões por papel e atividade ──────────────────────────

def test_permissoes_por_papel(client, ingest):
    from conftest import apusers, entrar
    import time as _t
    apusers("/_mock/limpar", {})
    client.post("/api/ingest/problemas", headers=ingest, json={
        "servico": "robo-papeis", "problemas": [{"chave": "fila-parada", "titulo": "Fila parada", "severidade": "alta"}]})
    client.post("/api/ingest/heartbeat", headers=ingest, json={"servico": "robo-papeis", "intervalo_s": 30})
    W = {"X-Web": "1"}

    entrar(client, "vera")                                          # visualizador: só vê
    probs = client.get("/api/problemas?servico=robo-papeis").json()["problemas"]
    pid = probs[0]["id"]
    r = client.post(f"/api/problemas/{pid}/reconhecer", headers=W)
    assert r.status_code == 403 and r.json()["codigo"] == "sem_permissao" and r.json()["papel"] == "visualizador"
    assert client.patch("/api/servicos/robo-papeis", headers=W, json={"grupo": "x"}).status_code == 403

    entrar(client, "ana")                                           # operador: trata problema
    assert client.post(f"/api/problemas/{pid}/reconhecer").json()["codigo"] == "csrf"      # sem X-Web
    assert client.post(f"/api/problemas/{pid}/reconhecer", headers=W).json()["problema"]["status"] == "reconhecido"
    r = client.post(f"/api/problemas/{pid}/resolver", headers=W).json()
    assert r["problema"]["resolvido_por"] == "Ana Operadora"
    assert client.delete("/api/servicos/robo-papeis", headers=W).status_code == 403

    entrar(client, "sem")                                           # sem papel = PAPEL_PADRAO (visualizador)
    assert client.get("/api/auth/sessao").json()["usuario"]["papel"] == "visualizador"

    entrar(client, "gil")                                           # gestor: edita e apaga serviço
    assert client.patch("/api/servicos/robo-papeis", headers=W, json={"grupo": "Testes"}).json()["servico"]["grupo"] == "Testes"
    assert client.delete("/api/servicos/robo-papeis", headers=W).json()["apagado"] == "robo-papeis"

    from app import acesso
    acesso.cliente().descarregar()
    for _ in range(30):
        acoes = [(e["user_id"], e["name"]) for e in apusers("/_mock/estado")["eventos"] if e["type"] == "acao"]
        if len(acoes) >= 4:
            break
        _t.sleep(0.1)
    assert acoes == [("u-ana", "problema_reconhecido"), ("u-ana", "problema_resolvido"),
                     ("u-gil", "servico_editado"), ("u-gil", "servico_apagado")]


def test_atividade_repassada(client, painel):
    from conftest import apusers, entrar
    from app import acesso
    import time as _t
    apusers("/_mock/limpar", {})
    entrar(client, "vera")
    r = client.post("/api/atividade", headers={"X-Web": "1"}, json={
        "events": [{"type": "tela", "name": "Logs", "duration_ms": 61000, "metadata": {"user_id": "u-outro"}}],
        "presence": {"screen": "Logs", "status": "ativo"}})
    assert r.status_code == 200 and r.json()["recebidos"] == 1
    acesso.cliente().descarregar()
    for _ in range(30):
        e = apusers("/_mock/estado")
        if e["eventos"] and e["presencas"]:
            break
        _t.sleep(0.1)
    ev = e["eventos"][0]
    assert ev["user_id"] == "u-vera" and ev["name"] == "Logs" and ev["metadata"]["origem"] == "web"
    assert e["presencas"][0]["user_id"] == "u-vera" and e["presencas"][0]["screen"] == "Logs"
    r = client.post("/api/atividade", headers=painel, json={"events": []})
    assert r.status_code == 403 and r.json()["codigo"] == "so_pessoas"
    client.post("/api/auth/logout", headers={"X-Web": "1"})


# ── Gerenciar integração ─────────────────────────────────────────────────────

def test_integracao_so_gestor(client):
    from conftest import entrar
    W = {"X-Web": "1"}
    corpo = {"servico": "robo-int", "nome": "Robô Int"}
    for nome in ("vera", "ana"):                                     # visualizador e operador: não veem a chave
        entrar(client, nome)
        assert client.get("/api/integracao").status_code == 403
        r = client.post("/api/integracao", headers=W, json=corpo)
        assert r.status_code == 403 and r.json()["codigo"] == "sem_permissao"
    entrar(client, "gil")
    assert client.post("/api/integracao", json=corpo).json()["codigo"] == "csrf"
    r = client.post("/api/integracao", headers=W, json=corpo)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    client.post("/api/auth/logout", headers=W)


def test_integracao_gera_env_e_txt(client, ingest, painel):
    sug = client.get("/api/integracao", headers=painel).json()
    assert sug["url_rede"] == "http://testserver" and sug["url_docker"] == "http://aplogs-api:8000"

    r = client.post("/api/integracao", headers=painel, json={
        "servico": "Robô DCTF!", "nome": "Robô DCTF", "grupo": "Fiscal", "repositorio": "Grupo-Studio/robo-dctf",
        "endereco": "192.168.1.120:3067", "intervalo_s": 60, "url": "http://192.168.1.100:3070/", "linguagem": "python"}).json()
    assert r["ok"] and r["criado"] and r["servico"]["slug"] == "rob-dctf"
    env = dict(l.split("=", 1) for l in r["env"].splitlines() if l and not l.startswith("#"))
    assert env == {"APLOGS_URL": "http://192.168.1.100:3070", "APLOGS_KEY": "chave-teste", "APLOGS_SERVICO": "rob-dctf",
                   "APLOGS_NOME": "Robô DCTF", "APLOGS_INTERVALO_S": "60", "APLOGS_GRUPO": "Fiscal",
                   "APLOGS_REPOSITORIO": "https://github.com/Grupo-Studio/robo-dctf",
                   "APLOGS_ENDERECO": "http://192.168.1.120:3067"}
    assert r["arquivo"] == "aplogs-integracao-rob-dctf.txt"
    assert r["env"] in "\n".join(l[4:] for l in r["txt"].splitlines()) + "\n"     # o .env vai no .txt
    assert "Aplogs.do_ambiente()" in r["txt"] and "180 s" in r["txt"]          # tolerância = 3 × 60

    # cadastrado, esperando o primeiro heartbeat
    det = client.get("/api/servicos/rob-dctf", headers=painel).json()["servico"]
    assert det["status"] == "desconhecido" and det["intervalo_s"] == 60 and det["grupo"] == "Fiscal"
    assert det["endereco"] == "http://192.168.1.120:3067"
    hb = client.post("/api/ingest/heartbeat", headers=ingest, json={"servico": env["APLOGS_SERVICO"],
                                                                     "intervalo_s": 60}).json()
    assert hb["status"] == "online"

    # de novo, para um serviço que já existe: campo vazio não apaga; docker usa a rede interna
    r = client.post("/api/integracao", headers=painel, json={
        "servico": "rob-dctf", "nome": "Robô DCTF", "onde": "docker", "linguagem": "node"}).json()
    assert not r["criado"] and r["servico"]["grupo"] == "Fiscal" and r["url"] == "http://aplogs-api:8000"
    assert "networks: [default, aplogs]" in r["txt"] and "process.env.APLOGS_KEY" in r["txt"]

    r = client.post("/api/integracao", headers=painel, json={"servico": "x1", "nome": "X", "linguagem": "outro"}).json()
    assert r["url"] == "http://testserver" and "curl" in r["txt"]


def test_env_num_clique(client, ingest, painel):
    client.post("/api/ingest/heartbeat", headers=ingest, json={
        "servico": "apusers", "nome": "APUsers", "grupo": "Plataforma", "endereco": "http://192.168.1.100:3080",
        "repositorio": "https://github.com/Grupo-Studio/apusers"})
    r = client.get("/api/integracao/apusers/env", headers=painel)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    env = dict(l.split("=", 1) for l in r.json()["env"].splitlines() if l and not l.startswith("#"))
    assert env == {"APLOGS_URL": "http://testserver", "APLOGS_KEY": "chave-teste", "APLOGS_SERVICO": "apusers",
                   "APLOGS_NOME": "APUsers", "APLOGS_INTERVALO_S": "30", "APLOGS_GRUPO": "Plataforma",
                   "APLOGS_REPOSITORIO": "https://github.com/Grupo-Studio/apusers",
                   "APLOGS_ENDERECO": "http://192.168.1.100:3080"}
    r = client.get("/api/integracao/apusers/env?onde=docker", headers=painel).json()
    assert r["url"] == "http://aplogs-api:8000" and "APLOGS_URL=http://aplogs-api:8000" in r["env"]
    assert client.get("/api/integracao/nao-existe/env", headers=painel).status_code == 404
    assert client.get("/api/integracao/apusers/env?onde=marte", headers=painel).status_code == 422


def test_env_num_clique_so_gestor(client, ingest):
    from conftest import apusers
    apusers("/_mock/limpar", {})
    client.post("/api/ingest/heartbeat", headers=ingest, json={"servico": "env-gestor"})
    for email, senha, esperado in (("ana@exemplo.test", "senha-ana", 403), ("gil@exemplo.test", "senha-gil", 200)):
        client.cookies.clear()
        assert client.post("/api/auth/login", json={"email": email, "senha": senha}).status_code == 200
        assert client.get("/api/integracao/env-gestor/env").status_code == esperado
    client.cookies.clear()


def test_integracao_valida(client, painel):
    def post(**kw):
        return client.post("/api/integracao", headers=painel, json={"servico": "robo-v", "nome": "V", **kw})
    assert post(servico="!!!").status_code == 422
    assert post(repositorio="não é repo").status_code == 422
    assert post(url="ftp://x").status_code == 422
    assert post(endereco="ftp://x").status_code == 422
    assert post(onde="marte").status_code == 422


def test_integracao_valor_env():
    from app.integracao import _valor_env
    assert _valor_env("Robô  DCTF") == "Robô DCTF"
    assert _valor_env("a$b#c") == "'a$b#c'"
    assert _valor_env("it's $x") == '"it\'s $x"'


def test_cliente_python_do_ambiente(monkeypatch):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clientes" / "python"))
    from aplogs_client import Aplogs
    for k in ("APLOGS_URL", "APLOGS_KEY", "APLOGS_SERVICO", "APLOGS_NOME", "APLOGS_INTERVALO_S", "APLOGS_GRUPO",
              "APLOGS_REPOSITORIO", "APLOGS_ENDERECO"):
        monkeypatch.delenv(k, raising=False)
    import pytest
    with pytest.raises(ValueError):
        Aplogs.do_ambiente()
    monkeypatch.setenv("APLOGS_URL", "http://x:3070/")
    monkeypatch.setenv("APLOGS_KEY", "k")
    monkeypatch.setenv("APLOGS_SERVICO", "robo-env")
    monkeypatch.setenv("APLOGS_NOME", "Robô Env")
    monkeypatch.setenv("APLOGS_INTERVALO_S", "45")
    monkeypatch.setenv("APLOGS_GRUPO", "G")
    monkeypatch.setenv("APLOGS_REPOSITORIO", "https://github.com/Grupo-Studio/aprap")
    monkeypatch.setenv("APLOGS_ENDERECO", "http://192.168.1.100:3067")
    a = Aplogs.do_ambiente(versao="2.0")
    assert (a.url, a.chave, a.servico, a.nome, a.intervalo_s) == ("http://x:3070", "k", "robo-env", "Robô Env", 45)
    assert a.info["grupo"] == "G" and a.info["versao"] == "2.0"
    assert a.info["repositorio"] == "https://github.com/Grupo-Studio/aprap"
    assert a.info["endereco"] == "http://192.168.1.100:3067"


def test_admin_do_apusers_e_gestor(client, ingest):
    """Administrador do apusers com acesso à aplogs mas sem papel: vale como gestor (exclui serviço, integra)."""
    from conftest import entrar
    W = {"X-Web": "1"}
    u = entrar(client, "adm")
    assert u["papel"] == "gestor" and u["admin"] is True and "gerenciar" in u["permissoes"]
    assert client.get("/api/auth/sessao").json()["usuario"]["papel"] == "gestor"
    assert client.post("/api/integracao", headers=W, json={"servico": "robo-adm", "nome": "Robô Adm"}).json()["criado"]
    assert client.delete("/api/servicos/robo-adm", headers=W).json()["apagado"] == "robo-adm"
    assert client.get("/api/servicos/robo-adm").status_code == 404
    entrar(client, "sem")                                            # quem não é admin e não tem papel segue visualizador
    assert client.get("/api/auth/sessao").json()["usuario"] == {**client.get("/api/auth/sessao").json()["usuario"],
                                                                "papel": "visualizador", "admin": False}
    client.post("/api/auth/logout", headers=W)
