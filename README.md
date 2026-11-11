# aplogs

Central de **logs e monitoramento** dos serviços e robôs do grupo. Cada serviço manda para cá
heartbeat, status, logs, problemas, jobs e filas; o painel (porta **3070**, acessível pela rede)
mostra tudo junto, com login por senha.

O primeiro serviço conectado é a **aplogin** (branch Receita), que aparece como
**RPA Procurações Eletrônicas**.

```
 aplogin (RPA Procurações) ─┐
 robô X ────────────────────┼──►  aplogs API :3070  ──►  MySQL (container local)
 robô Y ────────────────────┘          │
                                       └──►  painel web  (http://<ip>:3070, senha)
```

## O que tem

| | |
|---|---|
| **Heartbeat** | Cada serviço manda "estou vivo" a cada N segundos. Sem heartbeat por mais que a tolerância (padrão: 3 × intervalo, mínimo 60 s), o serviço fica **offline**, ganha um log e um **problema crítico**, que é resolvido sozinho quando ele volta. Desligamento normal (heartbeat `parando`) aparece como "foi parado", não como queda. |
| **Status** | `online` · `degradado` (o serviço reportou problema) · `offline` · `sem heartbeat` · `pausado` (monitoramento desligado no painel). Linha do tempo da última hora em cada card. |
| **Logs** | Busca, filtro por serviço/nível/módulo, modo ao vivo, traceback e contexto expansíveis. |
| **Problemas** | Abertos pelos serviços (com deduplicação por chave e contagem de ocorrências) ou pelo monitor. Reconhecer / resolver no painel. |
| **Jobs** | Execuções e tarefas com status, progresso, etapa e duração (upsert pelo id do job). |
| **Filas** | Na fila / rodando / concluídos / erros de cada fila, com gráfico da última hora. |

Os serviços **se cadastram sozinhos** no primeiro envio. No painel dá para renomear, agrupar,
mudar a tolerância, pausar o monitoramento ou apagar um serviço.

## Subir

```bash
cp .env.example .env
```

Troque **todas** as senhas no `.env`:

- `APLOGS_SECRET`: senha do painel.
- `APLOGS_INGEST_KEY`: chave que os serviços usam para enviar dados. Quem tem só ela consegue enviar, mas não ler.
- `DB_PASSWORD` / `DB_ROOT_PASSWORD`: senhas do MySQL.

```bash
docker compose up -d --build
```

Painel em `http://<ip-da-máquina>:3070`. Se outra máquina da rede não abrir, libere a porta 3070 no firewall.

- O banco fica no volume `aplogs_mysql-data`. Ele sobrevive a `docker compose down` e `up --build`;
  **só `docker compose down -v` apaga**.
- O MySQL fica acessível só na própria máquina em `127.0.0.1:3307` (DBeaver/Workbench), com `DB_USER`/`DB_PASSWORD`.
- As tabelas são criadas sozinhas no arranque.
- Retenção (padrão): logs 30 dias, heartbeats 7, jobs terminados 30, problemas resolvidos 90 (`RETENCAO_*` no `.env`).

```bash
docker compose logs -f aplogs-api      # log da aplogs
docker compose ps                      # os dois containers devem estar "healthy"
```

## Conectar um serviço

Não precisa configurar nada aqui: o serviço chama a API e **se cadastra sozinho** na primeira chamada.
Guia rápido (curl, Node, Python, rede Docker) em **[docs/INTEGRACAO.md](docs/INTEGRACAO.md)**.

- **URL:** `http://aplogs-api:8000` para containers na mesma máquina (rede Docker `aplogs`);
  `http://<ip>:3070` para o resto.
- **Chave:** `APLOGS_INGEST_KEY`, no header `x-api-key`, em `Authorization: Bearer` ou em `?key=`.
- **Envio:** `POST /api/ingest/<tipo>` com o campo `servico`. Heartbeat também funciona só pela URL:
  `GET /api/ingest/heartbeat/<servico>?key=<chave>`.

```bash
curl -X POST http://<ip>:3070/api/ingest/heartbeat \
  -H "x-api-key: <APLOGS_INGEST_KEY>" -H "Content-Type: application/json" \
  -d '{"servico": "robo-dctf", "nome": "Robô DCTF", "intervalo_s": 30, "status": "ok"}'
```

Para robôs em Python, copie **`clientes/python/aplogs_client.py`** (só biblioteca padrão):

```python
from aplogs_client import Aplogs

aplogs = Aplogs("http://192.168.1.100:3070", chave="...", servico="robo-dctf", nome="Robô DCTF")
aplogs.iniciar()                 # heartbeat em segundo plano
aplogs.capturar_logging("INFO")  # logs do módulo logging vão para a aplogs
```

### aplogin (RPA Procurações Eletrônicas)

No `.env` da aplogin:

```
APLOGS_URL=http://<ip-da-aplogs>:3070        # mesma máquina, na rede aplogs: http://aplogs-api:8000
APLOGS_KEY=<APLOGS_INGEST_KEY daqui>
```

Depois, `docker compose up -d --build` na aplogin. Detalhes do que ela envia: seção *aplogs* do `api/README.md` de lá.

## API de leitura

O painel usa estas rotas, que também aceitam o header `x-api-secret: <APLOGS_SECRET>` (para scripts).
Documentação interativa em `/api/docs`.

| Método | Rota | |
|---|---|---|
| GET | `/api/health` | Saúde (sem senha) |
| GET | `/api/painel` | Visão geral: serviços, status, linha do tempo, totais |
| GET/PATCH/DELETE | `/api/servicos/{slug}` | Detalhe · editar (nome, descricao, grupo, tolerancia_s, monitorar) · apagar |
| GET | `/api/logs` | `?servico=&nivel=&modulo=&q=&antes=&depois=&limite=` |
| GET | `/api/problemas` | `?servico=&status=abertos\|resolvidos\|todos` |
| POST | `/api/problemas/{id}/reconhecer` · `/resolver` | |
| GET | `/api/jobs` | `?servico=&status=&tipo=&q=` |
| GET | `/api/filas` | Filas + amostras da última hora |
| GET | `/api/heartbeats` | `?servico=&limite=` |

## Desenvolvimento e testes

```bash
cd api
pip install -r requirements.txt pytest httpx
python -m pytest tests -q                       # sqlite
TEST_DATABASE_URL="mysql+pymysql://usuario:senha@127.0.0.1:3307/aplogs_teste?charset=utf8mb4" \
  python -m pytest tests -q                     # MySQL de verdade (banco vazio só para teste)
```

Rodar fora do Docker: `DATABASE_URL=sqlite:///aplogs.db APLOGS_SECRET=x APLOGS_INGEST_KEY=y uvicorn app.main:app --port 3070`.

## Estrutura

```
api/app/
  main.py        app, login do painel, arquivos do frontend
  ingestao.py    POST /api/ingest/*  (serviços → aplogs)
  painel.py      rotas de leitura do painel
  monitor.py     monitor de heartbeat (offline) + limpeza por retenção
  servicos.py    regras: cadastro, status, problemas
  modelos.py     tabelas (SQLAlchemy)
  static/        painel (HTML + CSS + JS puro, sem build)
clientes/python/aplogs_client.py   cliente para robôs em Python
docs/INTEGRACAO.md                 contrato de envio
```
