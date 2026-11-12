# aplogs — documentação de implementação

Como a aplogs funciona por dentro: arquitetura, módulos, banco, autenticação, regras de status, tarefas de
fundo, painel e testes. Para **subir e operar**, veja o [README](../README.md); para **conectar um serviço**,
veja [INTEGRACAO.md](INTEGRACAO.md).

Versão descrita: **1.1.0** (`VERSAO` em `api/app/main.py`).

---

## 1. Visão geral

A aplogs é uma única aplicação **FastAPI** (Python 3.12) que faz três coisas no mesmo processo:

1. **Recebe** dados dos serviços/robôs (heartbeat, logs, problemas, jobs, filas) em `/api/ingest/*`.
2. **Monitora**: um laço em segundo plano marca como *offline* quem parou de mandar heartbeat e apaga dados
   antigos pela retenção.
3. **Serve o painel**: HTML/CSS/JS estáticos (sem build) mais a API de leitura em `/api/*`, com login pelo
   **apusers** (login central do grupo).

```
                       x-api-key                       ┌──────────────────────── container aplogs-api ───────────────┐
 serviços / robôs ───────────────► /api/ingest/*  ───► │ ingestao.py ─► servicos.py (regras) ─► SQLAlchemy ─┐        │
                                                       │                                                    │        │
                    cookie / x-api-secret              │ painel.py (leitura, permissões) ◄──────────────────┤        │
 navegador/scripts ──────────────► /api/*         ───► │ main.py (login, atividade, estáticos)              │        │
                                                       │ monitor.py (asyncio: status a cada 10 s, limpeza 1 h)┘      │
                                                       │ acesso.py + apusers_client.py ──► apusers (HTTP) │          │
                                                       └───────────────────────────────────────────────────┼─────────┘
                                                                                                           ▼
                                                                                         container aplogs-db (MySQL 8.4)
```

### Containers (`docker-compose.yml`)

| Container | Imagem | Porta | Observações |
|---|---|---|---|
| `aplogs-db` | `mysql:8.4` (utf8mb4) | `127.0.0.1:3307 → 3306` | Volume fixo `aplogs_mysql-data`; healthcheck com `mysqladmin ping`. |
| `aplogs-api` | `./api` (python:3.12-slim) | `3070 → 8000` | Sobe só com o banco *healthy*; roda como usuário `aplogs` (uid 1000); `TZ=America/Sao_Paulo`; healthcheck em `/api/health`. |

- Redes: `default` (API ↔ banco) e `aplogs` (nome fixo, para outros composes da mesma máquina chamarem
  `http://aplogs-api:8000`). O banco **não** entra na rede `aplogs`.
- `extra_hosts: host.docker.internal:host-gateway` permite chegar ao apusers rodando na mesma máquina.
- Comando: `uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --no-access-log` — **um único
  processo**, o que é premissa do desenho (ver §12).

### Dependências (`api/requirements.txt`)

`fastapi 0.115`, `uvicorn[standard] 0.30`, `SQLAlchemy 2.0`, `PyMySQL 1.1`, `cryptography` (necessário para o
`caching_sha2_password` do MySQL 8). Nenhuma dependência de frontend.

---

## 2. Estrutura do código

```
api/app/
  main.py            app FastAPI, lifespan, handlers de erro, login/logout/sessão/cadastro, /api/atividade, estáticos
  config.py          leitura das variáveis de ambiente
  db.py              engine, sessões, agora()/iso(), criação de tabelas e migração automática
  modelos.py         tabelas (SQLAlchemy 2, Mapped[...])
  auth.py            cookie assinado, sessões web no banco, renovação de token, dependências quem_chama/exigir/ingestao
  acesso.py          papéis/permissões, tradução de erros do apusers, atividade e presença
  apusers_client.py  cliente do apusers (cópia de Grupo-Studio/apusers — não editar aqui)
  ingestao.py        POST /api/ingest/*  (serviços → aplogs)
  servicos.py        regras de negócio: normalização, cadastro, cálculo/transição de status, problemas
  painel.py          rotas de leitura e gestão do painel
  monitor.py         tarefas de fundo: rodada de status e limpeza por retenção
  static/            index.html, style.css, app.js (JS puro)
api/tests/           pytest + réplica do apusers (mock_apusers.py)
clientes/python/     aplogs_client.py — cliente para robôs Python (só biblioteca padrão)
```

Dependência entre módulos (sem ciclos):

```
main ─► ingestao ─► servicos ─► modelos ─► db ─► config
  │        └──────► auth ─► acesso ─► apusers_client
  ├──► painel ─► auth, acesso, servicos
  └──► monitor ─► servicos
```

---

## 3. Ciclo de vida

`main.lifespan` roda na subida, nesta ordem:

1. Falha (`RuntimeError`) se `APLOGS_SECRET` ou `APLOGS_INGEST_KEY` estiverem vazios.
2. `db.preparar()`: tenta `SELECT 1` até 60 vezes, com 2 s de espera (o MySQL demora a aceitar conexão),
   depois `Base.metadata.create_all()` e `_migrar()` (§4.3).
3. `monitor.iniciar()`: cria duas `asyncio.Task` (status e limpeza).
4. Thread `apusers-papeis` → `acesso.declarar_papeis()`: declara os papéis no apusers. **Não bloqueia** o
   arranque; se o apusers estiver fora ou não configurado, só registra um aviso.

No desligamento: cancela as tarefas do monitor e chama `fechar()` do cliente apusers (descarrega a fila de
eventos pendentes).

As rotas são **síncronas** (`def`), então o FastAPI executa cada uma em thread do pool; o cliente do apusers
também é síncrono. As tarefas do monitor rodam no event loop mas delegam o trabalho com `asyncio.to_thread`.

---

## 4. Banco de dados

### 4.1 Convenções

- **Datas em UTC sem fuso** (`db.agora()` = `datetime.now(utc).replace(tzinfo=None)`), serializadas em ISO com
  `Z` por `db.iso()`. Conversão para o fuso local só no painel (navegador) e no texto do problema "offline"
  (fuso do container).
- Tipos que variam entre MySQL e sqlite (`modelos.py`):
  - `ID_GRANDE` = `BIGINT` (no sqlite `INTEGER`, para o autoincremento funcionar);
  - `DATA` = `DATETIME(3)` no MySQL (milissegundos);
  - `TEXTO_LONGO` = `MEDIUMTEXT` no MySQL (o `TEXT` tem 64 KB em *bytes*; 60 mil caracteres com acento em
    utf8mb4 estouram).
- Engine MySQL: `pool_pre_ping`, `pool_recycle=1800`, `pool_size=10`, `max_overflow=20`.
- Sessões: `get_db()` (dependência FastAPI, uma por requisição) e `sessao()` (context manager para código fora
  de rota). `expire_on_commit=False`, `autoflush=False`.
- Usuário/senha do banco entram na URL com `quote_plus` (senhas com `@ : / #` não quebram a conexão).
  `DATABASE_URL` substitui tudo (os testes usam sqlite).

### 4.2 Tabelas

| Tabela | Chave / unicidade | Papel | Índices |
|---|---|---|---|
| `servicos` | `id`; `slug` único | Um por serviço. Guarda metadados (nome, grupo, versão, host, ambiente, repositório/branch/commit), config de monitoramento (`intervalo_s`, `tolerancia_s`, `monitorar`), estado (`status`, `status_desde`, `status_reportado`, `mensagem`, `ultimo_heartbeat_em`, `iniciado_em`) e `dados` (JSON do último heartbeat). `nome_editado` impede o heartbeat de sobrescrever nome/descrição editados no painel. | — |
| `heartbeats` | `id` (bigint) | Cada heartbeat recebido; base da linha do tempo. | `(servico_id, recebido_em)`, `(recebido_em)` |
| `logs` | `id` (bigint) | Linhas de log. `ts` = quando aconteceu (no serviço), `recebido_em` = chegada. `nivel_num` (10–50) permite filtro "nível mínimo". Logs gerados pela própria aplogs usam `modulo = "aplogs"`. | `(servico_id, id)`, `(nivel_num, id)`, `(recebido_em)` |
| `problemas` | `id` | Incidentes. `chave` deduplica (só um aberto por serviço+chave). `status`: aberto → reconhecido → resolvido. `origem`: `servico` ou `monitor`. `ocorrencias`, `primeira_vez`, `ultima_vez`, `resolvido_por`. | `(servico_id, chave, status)`, `(status)` |
| `jobs` | `id`; único `(servico_id, externo_id)` | Execuções (upsert pelo id do job no serviço). Status: `fila`, `rodando`, `ok`, `erro`, `interrompido`. | `(atualizado_em)`, `(status)` |
| `filas` | `id`; único `(servico_id, nome)` | Foto atual de cada fila (upsert pelo nome). | — |
| `fila_amostras` | `id` (bigint) | Histórico das filas para o gráfico (no máximo 1 amostra a cada 20 s por fila). | `(fila_id, ts)`, `(ts)` |
| `web_sessoes` | `id` (string 64) | Sessões do painel: tokens do apusers, e-mail, `expira`/`criada`/`renovada` (epoch). | `(expira)` |

As FKs para `servicos.id` declaram `ON DELETE CASCADE`, mas o `DELETE /api/servicos/{slug}` apaga as tabelas
filhas explicitamente (no sqlite as FKs não são aplicadas por padrão).

### 4.3 Migração automática (`db._migrar`)

O `create_all` só cria tabelas que não existem. Para atualizar um banco antigo sem perder dados, no arranque:

- Toda coluna do modelo que **falta** no banco é criada com `ALTER TABLE … ADD COLUMN … NULL`. Colunas novas
  precisam, portanto, ser **opcionais**; uma coluna obrigatória faltando só gera aviso no log ("crie na mão").
- No MySQL, colunas que mudaram de `TEXT` para `MEDIUMTEXT` são alteradas com `MODIFY COLUMN`.

Não há remoção nem renomeação de colunas, nem criação de índices novos em tabelas existentes. Mudanças desse
tipo exigem script manual.

---

## 5. Configuração (`config.py`)

| Variável | Padrão | Uso |
|---|---|---|
| `APLOGS_SECRET` | — (obrigatória) | Header `x-api-secret` dos scripts e chave HMAC do cookie. Trocar derruba todos os logins. |
| `APLOGS_INGEST_KEY` | — (obrigatória) | Chave de envio dos serviços. Só envia, não lê. |
| `SESSAO_HORAS` | 12 | Validade da sessão do painel. |
| `APUSERS_URL`, `APUSERS_KEY` | vazio | Login central. Sem as duas, ninguém entra no painel (ingestão e scripts seguem). |
| `PAPEL_PADRAO` | `visualizador` | Papel de quem tem acesso sem papel no apusers. |
| `DB_HOST`/`DB_PORT`/`DB_USER`/`DB_PASSWORD`/`DB_NAME` | `aplogs-db`/3306/`aplogs`/—/`aplogs` | Monta a URL do MySQL. |
| `DATABASE_URL` | — | Substitui a URL montada (ex.: sqlite). |
| `MONITOR_INTERVALO_S` | 10 | Periodicidade da rodada de status. |
| `HEARTBEAT_INTERVALO_PADRAO_S` | 30 | Intervalo assumido quando o serviço não informa o dele. |
| `TOLERANCIA_MINIMA_S` | 60 | Piso da tolerância automática. |
| `RETENCAO_LOGS_DIAS` / `_HEARTBEATS_` / `_JOBS_` / `_PROBLEMAS_` | 30 / 7 / 30 / 90 | Retenção; `0` = para sempre. |
| `MAX_LOGS_POR_ENVIO` | 2000 | Limite de linhas por `POST /api/ingest/logs` (acima: 413). |

Constantes no código: `MAX_MENSAGEM = 60_000` caracteres por mensagem de log; `AMOSTRA_MIN_S = 20`
(`ingestao.py`); `TIMELINE_MIN = 60` (`painel.py`). Valores inteiros inválidos no ambiente caem no padrão.

---

## 6. Autenticação e autorização (`auth.py`, `acesso.py`)

Há três formas de acesso, independentes entre si:

| Quem | Credencial | Pode | Dependência |
|---|---|---|---|
| Serviços | `APLOGS_INGEST_KEY` em `x-api-key`, `Authorization: Bearer` ou `?key=` | Só `/api/ingest/*` | `auth.ingestao` (no router) |
| Scripts | `x-api-secret: APLOGS_SECRET` | Tudo no painel (`Script.pode()` → `True`) | `auth.quem_chama` |
| Pessoas | Cookie `aplogs_sessao` (login no apusers) | Conforme o papel | `auth.quem_chama` + `auth.exigir(ação)` |

Todas as comparações de segredo usam `hmac.compare_digest`.

### 6.1 Cookie e sessão web

- O cookie leva apenas `base64url({"sid", "exp"}).HMAC-SHA256`, com chave `sha256("aplogs-sessao:" + APLOGS_SECRET)`.
  Atributos: `HttpOnly`, `SameSite=Strict`, `path=/`, `max_age = SESSAO_HORAS`.
- O `sid` (32 bytes aleatórios, `secrets.token_urlsafe`) aponta para uma linha de `web_sessoes`, onde ficam o
  `access_token` e o `refresh_token` do apusers. **Os tokens nunca chegam ao navegador.**
- Ao abrir uma sessão, as sessões vencidas são apagadas; ao ler uma sessão vencida, ela é apagada.

### 6.2 Resolução do usuário (`auth.usuario`)

```
cookie → sid válido? → linha em web_sessoes? → acesso.usuario_do_token(access_token)
                                                    │
         ┌──────────────── ok ──────────────────────┤
         │                                          ├─ 503 (apusers fora) → propaga, sessão mantida
         ▼                                          ├─ 403 (acesso tirado/inativo/pendente) → apaga sessão, propaga
     Usuario                                        ├─ outro erro ≠ token_expirado → apaga sessão, None (401)
                                                    └─ token_expirado → _renovar()
```

`_renovar(sid, vencido)` serializa a renovação (o refresh token do apusers é de **uso único**):

- trava global de processo (`_trava_renovacao`) **e** `SELECT … FOR UPDATE` na linha;
- se o `access_token` gravado já é diferente do vencido, outra requisição renovou: devolve o novo;
- refresh recusado → apaga a sessão; erro de rede → 503 `login_central_fora`.

Com isso, duas abas renovando ao mesmo tempo não derrubam a sessão.

A verificação do token usa `POST /v1/auth/verify` do apusers com **cache de 30 s** no cliente; por isso tirar o
acesso, desativar a pessoa ou trocar o papel vale em até 30 s.

### 6.3 CSRF

Pela web (cookie), qualquer método diferente de `GET`/`HEAD` exige o header `X-Web: 1`; sem ele, 403 `csrf`.
Como o header customizado não pode ser enviado entre origens sem preflight CORS (e a aplogs não habilita CORS),
isso, somado ao `SameSite=Strict`, bloqueia requisições forjadas. O `app.js` envia o header em toda mutação.

### 6.4 Papéis e permissões

Definidos em `acesso.py` e declarados no apusers na subida (`PATCH /v1/service/me` com `roles`):

| Ação | Papéis | Rotas |
|---|---|---|
| `ver` | visualizador, operador, gestor | todas as leituras (`auth.painel = exigir("ver")`, no router do painel) |
| `tratar` | operador, gestor | `POST /api/problemas/{id}/reconhecer` e `/resolver` |
| `gerenciar` | gestor | `PATCH`/`DELETE /api/servicos/{slug}` |

`exigir(acao)` valida o nome da ação na importação (erro de programação vira `ValueError` no arranque) e, sem
permissão, responde 403 `sem_permissao` com o papel atual. Papel desconhecido vindo do apusers vira
`PAPEL_PADRAO`. O frontend esconde o que o papel não permite (`pode(acao)` em `app.js`), mas a API confere sempre.

### 6.5 Integração com o apusers

- `acesso.chamar()` traduz as falhas do cliente:
  - rede/timeout → 503 `login_central_fora`;
  - `chave_invalida`/`chave_ausente` → 503 `login_central_mal_configurado` (e log de erro);
  - erros da pessoa (ex.: `acesso_restrito`, `usuario_pendente`) → mesmo status e código do apusers,
    repassando `pedido` e `retry_after_s` quando existem.
- `login` envia `solicitar_acesso=True`: quem não tem acesso gera um pedido para o administrador.
- O IP do navegador é repassado ao apusers (`x-apusers-client-ip`) para o limite de tentativas.
- **Atividade**: ações feitas pelas rotas (`problema_reconhecido`, `problema_resolvido`, `servico_editado`,
  `servico_apagado`) entram em `registrar_acao` (fila do cliente, envio em lote a cada 5 s). Scripts não geram
  atividade. Tempo de tela e presença chegam por `POST /api/atividade` e são repassados (presença numa thread,
  sem esperar).
- apusers fora: login e sessão respondem 503; ingestão, monitor e scripts não dependem dele.

O arquivo `apusers_client.py` é cópia do repositório `Grupo-Studio/apusers`; para atualizar, copie de novo.

---

## 7. Ingestão (`ingestao.py`)

Todas as rotas recebem `servico` (identificador). `servicos.obter_ou_criar` normaliza o slug
(`[a-z0-9][a-z0-9._-]{0,79}`, minúsculas, caracteres inválidos viram `-`; inválido → 400) e **cadastra o serviço
no primeiro envio**, gravando um log "conectado à aplogs". Corrida na criação é tratada pelo `IntegrityError`
do slug único (rollback e releitura).

### 7.1 Heartbeat — `POST /api/ingest/heartbeat`, `GET|POST /api/ingest/heartbeat/{servico}`

`_registrar_heartbeat`:

1. Atualiza `nome` (se não foi editado no painel), `descricao` (idem), `grupo`, `versao`, `host`, `ambiente`
   quando vierem.
2. `intervalo_s` (1–86400) substitui o atual.
3. `repositorio` só é gravado se o serviço **ainda não tem** (quem troca é o painel); valor inválido é ignorado.
   `branch`/`commit` sempre substituem (string vazia limpa).
4. `status` é normalizado (`ok`, `degradado`, `erro`, `parando`; sinônimos em inglês e português; desconhecido = `ok`).
5. Grava `ultimo_heartbeat_em`, `dados`, insere uma linha em `heartbeats` e chama `aplicar_status` (§8).
6. Responde `{status, recebido_em, tolerancia_s}` — o serviço sabe na hora como está sendo visto.

A variante por URL aceita `status`, `mensagem`, `intervalo_s` e `nome` na query (para cron, curl, Uptime Kuma).

### 7.2 Logs — `POST /api/ingest/logs`

- Aceita um **lote** (`logs: [...]`) ou **uma linha** com os campos no próprio corpo (o validador converte em lote).
- `nivel`: texto (com sinônimos: `WARN`, `ERRO`, `FATAL`, `CRÍTICO`, `TRACE`…) ou número (vai para o nível
  padrão mais próximo); desconhecido = `INFO`.
- `ts`: ISO 8601 (sem fuso = UTC) ou epoch; inválido/vazio = momento do recebimento.
- Cortes: `mensagem` em 60 000 caracteres, `modulo` em 80, `exc` em 1 000 000 (com `…`).
- Mais de `MAX_LOGS_POR_ENVIO` linhas → 413.

### 7.3 Problemas — `POST /api/ingest/problemas`

Dois modos:

- **Eventos** (sem `sincronizar`): cada item abre o problema ou, se já houver um aberto/reconhecido com a mesma
  `chave`, atualiza título/descrição/severidade/`ultima_vez` e **soma uma ocorrência**. `resolvido: true` resolve.
- **Estado** (`sincronizar: ["prefixo:"]`): a lista enviada é a foto completa. Itens enviados são
  abertos/atualizados **sem** somar ocorrência; problemas abertos de origem `servico` cujas chaves começam com um
  dos prefixos e **não** vieram na lista são resolvidos (`resolvido_por = "servico"`).

Severidade normalizada para `info | aviso | erro | critico` (padrão `erro`). Problemas de origem `monitor` nunca
são resolvidos pelo modo estado.

### 7.4 Jobs — `POST /api/ingest/jobs`

Upsert por `(servico_id, externo_id)`, até 1000 por envio:

- status normalizado (sinônimos; desconhecido = `rodando`);
- campos `None` não apagam o valor gravado;
- entrou em `rodando` sem `iniciado_em` → `iniciado_em = agora`;
- status final (`ok`/`erro`/`interrompido`) → `terminado_em = agora` se vazio; `ok` com progresso → 100;
- `duracao_s` informada vale; senão é calculada de `iniciado_em`/`terminado_em`.

### 7.5 Filas — `POST /api/ingest/filas`

Upsert por `(servico_id, nome)`, até 200 por envio. Os contadores são **substituídos** (é uma foto). Uma linha
em `fila_amostras` é gravada se a última amostra da fila tiver 20 s ou mais.

---

## 8. Status dos serviços (`servicos.py`)

### 8.1 Cálculo (`calcular_status`)

Avaliado nesta ordem:

| Condição | Status |
|---|---|
| `monitorar = False` | `pausado` |
| nunca mandou heartbeat | `desconhecido` ("sem heartbeat" no painel) |
| último heartbeat reportou `parando` | `offline` |
| idade do último heartbeat > tolerância | `offline` |
| reportou `degradado` ou `erro` | `degradado` |
| senão | `online` |

Tolerância: `tolerancia_s` do serviço (editável no painel; `0` = automática) ou
`max(3 × intervalo_s, TOLERANCIA_MINIMA_S)`.

### 8.2 Transições (`aplicar_status`)

Chamada pelo heartbeat, pela edição no painel e pela rodada do monitor. Se o status mudou, atualiza
`status`/`status_desde` e registra efeitos (sem commit — quem chama faz):

| Transição | Efeito |
|---|---|
| → `offline` (queda) | Log `ERROR` + problema `aplogs:offline`, **crítico**, origem `monitor`, título "X está offline" com idade e tolerância. |
| → `offline` (reportou `parando`) | Log `ERROR` + mesmo problema com título "X foi parado" (desligamento normal). |
| `offline` → `online`/`degradado` | Log `INFO` "voltou a ficar online depois de …" + resolve `aplogs:offline` (`por = monitor`). |
| → `degradado` | Log `WARNING` com a mensagem reportada. |
| `degradado` → `online` | Log `INFO` "voltou ao normal". |
| → `pausado` | Log `INFO` + resolve `aplogs:offline` (`por = painel`). |

O problema de offline usa `contar=False`: rodadas repetidas não somam ocorrências nem duplicam o problema.

---

## 9. Tarefas de fundo (`monitor.py`)

| Tarefa | Frequência | O que faz |
|---|---|---|
| `_loop_status` → `rodada_status()` | a cada `MONITOR_INTERVALO_S` | Percorre **todos** os serviços, chama `aplicar_status` com um único `momento` e faz um commit. |
| `_loop_limpeza` → `limpeza()` | 1× por hora (checa a cada 60 s) | Apaga em lotes de 5000 ids: logs (`recebido_em`), heartbeats e amostras de fila (mesma retenção), jobs **terminados** (`atualizado_em`) e problemas **resolvidos** (`resolvido_em`). |

Exceções em uma rodada são registradas e o laço continua. Ambas rodam via `asyncio.to_thread` para não bloquear o
event loop.

---

## 10. API do painel (`painel.py` e `main.py`)

Rotas de leitura e gestão (cookie ou `x-api-secret`). Resumo; detalhes de parâmetros no README e em `/api/docs`.

| Rota | Implementação |
|---|---|
| `GET /api/painel` | Todos os serviços (ordem grupo, nome) + linha do tempo + agregados: problemas abertos por severidade, erros (≥ 40) e avisos (= 30) da última hora, filas, jobs rodando, totais por status. |
| `GET /api/servicos` / `/{slug}` | Lista simples / detalhe com problemas abertos, últimos 20 jobs, filas e últimos 20 heartbeats. |
| `PATCH /api/servicos/{slug}` | Só campos enviados (`exclude_unset`). Nome editado marca `nome_editado`; repositório é normalizado (422 se inválido); `tolerancia_s = 0` volta à automática. Recalcula o status. |
| `DELETE /api/servicos/{slug}` | Apaga amostras, filas, jobs, problemas, logs, heartbeats e o serviço. Se o serviço continuar enviando, é cadastrado de novo. |
| `GET /api/logs` | Mais novos primeiro, paginação por **id** (`antes=<id>` página anterior, `depois=<id>` modo ao vivo), `nivel` = mínimo, `q` = `LIKE` em mensagem e traceback, `limite` ≤ 2000. Devolve `ultimo_id` global. |
| `GET /api/logs/modulos` | `DISTINCT modulo`. |
| `GET /api/problemas` | `status=abertos|resolvidos|todos`; resolvidos ordenados por `resolvido_em`. |
| `POST /api/problemas/{id}/reconhecer` | `aberto → reconhecido` (continua aberto, sai do alerta). |
| `POST /api/problemas/{id}/resolver` | Resolve; `resolvido_por` = nome da pessoa (ou `painel` para scripts). |
| `GET /api/jobs` | Filtros `status` (lista separada por vírgula, normalizada), `tipo`, `q`; devolve também os tipos existentes. |
| `GET /api/filas` | Filas + amostras dos últimos `minutos` (5 a 1440, padrão 60). |
| `GET /api/heartbeats` | Últimos heartbeats de um serviço. |
| `GET /api/health` | Público. 200 com banco ok, 503 sem banco; informa se o login central está configurado. |

### 10.1 Linha do tempo (`_timelines`)

Para cada serviço, 60 posições (uma por minuto, da mais antiga para a mais recente):

- `-1` se o minuto termina antes de o serviço existir;
- `1` se algum heartbeat **cobre** o minuto: um heartbeat em `t` cobre de `t` até `t + max(60, intervalo + 15) s`
  (serviços com intervalo longo não aparecem "vermelhos" entre um envio e outro);
- `0` caso contrário.

Busca todos os heartbeats da janela (com margem do maior intervalo) numa só consulta e calcula em Python.

### 10.2 Formato das respostas e erros

- Sucesso: `{"ok": true, ...}`.
- `HTTPException`: `{"ok": false, "error": "<texto>"}`. Erros do login central/permissões trazem também
  `codigo` estável (`nao_autenticado`, `sem_permissao`, `csrf`, `login_central_fora`, `acesso_restrito`…) —
  o painel decide pelo código, não pelo texto.
- Validação (422): `{"ok": false, "error": "Dados inválidos", "detalhes": ["campo: mensagem", ...]}`.

---

## 11. Frontend (`static/`)

HTML + CSS + JS puro, sem build, servido por `GET /` (`Cache-Control: no-cache`) e `/static/*`.

- **`api()`**: wrapper de `fetch` com `credentials: same-origin`, `X-Web: 1` em mutações e tratamento central:
  401, ou 403 com código de "sair do painel" (`acesso_restrito`, `usuario_inativo`, …), volta para a tela de
  login com a mensagem do servidor.
- **Atualização por polling**: painel a cada 5 s; aba atual a cada 5 s (exceto Logs); Logs em modo ao vivo a
  cada 3 s com `depois=<ultimoId>`; "rolar para baixo" usa `antes=<id>`.
- **Notificações**: `avisarMudancas` compara o status anterior/atual dos serviços e mostra *toasts*.
- **Gaveta de serviço**: detalhe, edição (PATCH) e exclusão, conforme `pode("gerenciar")`.
- **Gráficos**: linha do tempo (barras) e *sparkline* das filas em SVG gerado no próprio JS.
- **Atividade** (módulo `atividade` em `app.js`): mede tempo por aba; fica ocioso após 5 min sem interação
  (mouse, teclado, clique, rolagem, toque) ou com a aba oculta; corta trechos longos a cada 5 min; envia a cada
  60 s para `/api/atividade` (máx. 500 eventos por envio; reenvia em falha, guardando até 2000); no
  `pagehide`/logout usa `keepalive`. Presença: `ativo`, `ocioso`, `saiu`.
- Todo texto dinâmico passa por `esc()` antes de ir para `innerHTML`; links de repositório são só `http(s)`
  (garantido também no servidor por `normalizar_repositorio`).

---

## 12. Cliente Python (`clientes/python/aplogs_client.py`)

Arquivo único, só biblioteca padrão, para copiar nos robôs.

- `Aplogs(url, chave, servico, ...)`; `branch`/`commit` caem em `GIT_BRANCH`/`GIT_COMMIT` do ambiente.
- `iniciar()` sobe uma thread que manda heartbeat a cada `intervalo_s` e descarrega logs a cada ~2 s
  (lotes de até 1000). `dados_fn` opcional complementa os `dados` de cada heartbeat.
- Logs vão para uma `Queue` limitada (`max_buffer=5000`); cheia, descarta o mais velho; falha no envio devolve
  o lote à fila.
- `capturar_logging(nivel)` instala o `AplogsHandler` no logger (ignora `aplogs_client` e `urllib3` para não
  entrar em laço).
- `status()`, `problema()`, `resolver()`, `problemas_estado()`, `job()`, `fila()` mapeiam direto para a ingestão.
- `parar()` (registrado no `atexit`) descarrega os logs e manda heartbeat `parando`.
- Nunca levanta exceção para o robô: falhas de rede viram `None`/log de debug.

---

## 13. Testes (`api/tests`)

```bash
cd api
pip install -r requirements.txt pytest httpx
python -m pytest tests -q
```

- `conftest.py` define o ambiente **antes** de importar o app: sqlite temporário (ou `TEST_DATABASE_URL` para
  MySQL real — o banco é recriado do zero), segredos de teste, `MONITOR_INTERVALO_S=3600` (os testes chamam
  `monitor.rodada_status()` na mão).
- `mock_apusers.py` é uma réplica mínima do apusers (porta 8768), subida como subprocesso, com usuários fixos por
  papel (`ana` operador, `vera` visualizador, `gil` gestor, `sem` sem papel, `bruno` sem acesso, `paula`
  pendente) e rotas de controle `/_mock/...` para simular token vencido, acesso revogado e troca de papel.
- Fixtures: `client` (TestClient com lifespan), `ingest` (`x-api-key`), `painel` (`x-api-secret`) e o helper
  `entrar(client, nome)`.
- Cobertura atual (29 testes): autenticação (chave, secret, login, pendente, renovação única do token, acesso
  revogado, papéis declarados, permissões por papel, atividade), heartbeat e cadastro, offline/volta, parada
  normal, degradado, pausa, slug inválido, logs (filtros, paginação, linha avulsa), problemas (eventos e estado),
  jobs, filas, apagar serviço, limpeza, repositório pelo heartbeat e pelo painel, heartbeat por URL.

Para simular tempo, os testes "envelhecem" `ultimo_heartbeat_em` direto no banco (`_envelhecer_heartbeat`).

---

## 14. Decisões de projeto e limites conhecidos

- **Um processo só.** O monitor roda dentro do processo da API e a trava de renovação de token é de processo.
  Subir com vários workers do uvicorn ou várias réplicas faria rodadas de status concorrentes (logs de
  transição possivelmente duplicados) e dependeria só do `FOR UPDATE` para a renovação. Para escalar, separe o
  monitor em processo próprio antes.
- **Status é armazenado, não calculado na leitura.** O painel lê `servicos.status`; a precisão do *offline*
  depende de `MONITOR_INTERVALO_S` (até ~10 s de atraso por padrão).
- **Busca em logs é `LIKE '%termo%'`**, sem índice de texto: em volumes grandes, filtre por serviço/nível
  junto ou reduza a retenção.
- **Linha do tempo calculada em Python** a cada `GET /api/painel` (a cada 5 s por aba aberta); custo cresce com
  serviços × heartbeats da última hora.
- **Migração só aditiva** (§4.3).
- **Polling, não push**: sem WebSocket/SSE; simples e suficiente para o volume atual.
- **Segredos separados por função**: quem tem só a chave de ingestão não lê nada; o segredo de scripts não é
  usado por pessoas; a chave do apusers nunca sai do servidor.

---

## 15. Como estender

**Novo campo no serviço/heartbeat**: coluna *opcional* em `modelos.Servico` (a migração cria sozinha) → campo
em `HeartbeatIn` e tratamento em `_registrar_heartbeat` → `painel.servico_json` → exibição em `app.js` →
teste em `test_aplogs.py` → contrato em `docs/INTEGRACAO.md`.

**Novo tipo de dado ingerido**: modelo em `modelos.py` → rota em `ingestao.py` (usar `_servico()` para o
cadastro automático) → retenção em `monitor.limpeza` → leitura em `painel.py` → aba no painel.

**Nova permissão**: chave em `acesso.PERMISSOES` e `acesso.DESCRICAO`; se precisar de papel novo, incluir em
`acesso.PAPEIS` (declarado no apusers na próxima subida) → `Depends(auth.exigir("<ação>"))` na rota →
`pode("<ação>")` no `app.js` → atualizar a tabela de papéis do README.
