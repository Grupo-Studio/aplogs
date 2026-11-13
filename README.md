# aplogs

Central de **logs e monitoramento** dos serviços e robôs do grupo. Cada serviço manda para cá
heartbeat, status, logs, problemas, jobs e filas; o painel (porta **3070**, acessível pela rede)
mostra tudo junto, com o login central do grupo (**apusers**).

O primeiro serviço conectado é a **aplogin** (branch Receita), que aparece como
**RPA Procurações Eletrônicas**.

```
 aplogin (RPA Procurações) ─┐
 robô X ────────────────────┼──►  aplogs API :3070  ──►  MySQL (container local)
 robô Y ────────────────────┘          │
                                       └──►  painel web  (http://<ip>:3070) ──► login: apusers
```

## O que tem

| | |
|---|---|
| **Heartbeat** | Cada serviço manda "estou vivo" a cada N segundos. Sem heartbeat por mais que a tolerância (padrão: 3 × intervalo, mínimo 60 s), o serviço fica **offline**, ganha um log e um **problema crítico**, que é resolvido sozinho quando ele volta. Desligamento normal (heartbeat `parando`) aparece como "foi parado", não como queda. |
| **Projetos** | Cada serviço mostra o repositório do GitHub e qual **branch @ commit** está rodando, com link (o serviço manda no heartbeat ou você preenche no painel). |
| **Endereço** | Onde o serviço atende na rede interna (ex.: `http://192.168.1.100:3067`), com link no card. Vem no heartbeat (`APLOGS_ENDERECO`) ou é preenchido no painel. |
| **Status** | `online` · `degradado` (o serviço reportou problema) · `offline` · `sem heartbeat` · `pausado` (monitoramento desligado no painel). Linha do tempo da última hora em cada card. |
| **Logs** | Busca, filtro por serviço/nível/módulo, modo ao vivo, traceback e contexto expansíveis. |
| **Problemas** | Abertos pelos serviços (com deduplicação por chave e contagem de ocorrências) ou pelo monitor. Reconhecer / resolver no painel. |
| **Jobs** | Execuções e tarefas com status, progresso, etapa e duração (upsert pelo id do job). |
| **Filas** | Na fila / rodando / concluídos / erros de cada fila, com gráfico da última hora. |

Os serviços **se cadastram sozinhos** no primeiro envio. No painel dá para renomear, agrupar, ligar ao
repositório do GitHub, informar o endereço na rede, mudar a tolerância, pausar o monitoramento ou apagar um serviço.

Atualizar a aplogs não perde dados: no arranque ela cria sozinha as colunas novas no banco que já existe.

## Login do painel (apusers)

O painel entra com o usuário do **apusers**, o login central do grupo. A tela de entrada é da própria aplogs;
quem confere e-mail, senha, acesso e papel é o apusers. A chave do serviço (`APUSERS_KEY`) fica só no
servidor: o navegador nunca fala com o apusers.

- **Sessão:** fica na tabela `web_sessoes`; o cookie (`aplogs_sessao`, HttpOnly, SameSite=Strict) leva só um
  id assinado. Os tokens do apusers nunca chegam ao navegador. Duas abas renovando o token juntas não derrubam a
  sessão.
- **Papéis** (declarados no apusers quando a aplogs sobe; o administrador escolhe o de cada pessoa). Quem tem
  acesso sem papel fica com `PAPEL_PADRAO` (`visualizador`). **Administrador do apusers é sempre `gestor`** na aplogs
  (ele já poderia se dar qualquer papel lá).

  | | visualizador | operador | gestor |
  |---|:-:|:-:|:-:|
  | Ver serviços, logs, problemas, jobs e filas | ✓ | ✓ | ✓ |
  | Reconhecer / resolver problemas | | ✓ | ✓ |
  | Editar (nome, grupo, repositório, endereço, monitoramento) e apagar serviços | | | ✓ |
  | Gerenciar integração (gera o `.env` com a chave de envio) | | | ✓ |

  O que o papel não permite some da tela, e a API confere de novo (403 `sem_permissao`). Tirar o acesso,
  desativar a pessoa ou trocar o papel no apusers vale em até 30 s.
- **Sem acesso ainda?** Entrar gera um pedido para o administrador do apusers. *Criar conta* cria a conta
  pendente; aprovada, já vem com acesso à aplogs.
- **Atividade:** reconhecer/resolver problema e editar/apagar serviço são registrados no apusers (com quem
  fez). O tempo em cada aba (Serviços, Logs, Problemas, Jobs, Filas) e a presença vão para o apusers pelo
  `POST /api/atividade`. Parado 5 min, o tempo para de contar. Quem resolve um problema aparece no próprio problema.
- **apusers fora do ar:** entrar no painel responde 503 (`login_central_fora`). A ingestão dos serviços, o
  monitor de heartbeat e os scripts com `x-api-secret` seguem funcionando.

## Subir

```bash
cp .env.example .env
```

Troque **todas** as senhas no `.env`:

- `APLOGS_SECRET`: segredo dos scripts (header `x-api-secret`) e da assinatura do cookie do painel. Pessoas não usam.
- `APLOGS_INGEST_KEY`: chave que os serviços usam para enviar dados. Quem tem só ela consegue enviar, mas não ler.
- `DB_PASSWORD` / `DB_ROOT_PASSWORD`: senhas do MySQL.

E ligue o login central: no painel do apusers, *Serviços → Cadastrar* com o slug `aplogs` (o apusers mostra a
chave uma vez) e preencha `APUSERS_URL` e `APUSERS_KEY`. Mesma máquina do apusers:
`APUSERS_URL=http://host.docker.internal:3080`. Depois de subir, dê acesso à `aplogs` para as pessoas no apusers
e escolha o papel de cada uma.

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

**Pelo painel (gestor):** em *Serviços → Gerenciar integração* (ou no detalhe de um serviço) você diz o nome, o
grupo, o repositório, o endereço do serviço na rede, o intervalo de heartbeat, onde o serviço roda e a linguagem. A aplogs já cadastra o serviço
(aparece como "Sem heartbeat") e gera o bloco para colar no `.env` dele (`APLOGS_URL`, `APLOGS_KEY`,
`APLOGS_SERVICO`, `APLOGS_ENDERECO`...) e um `.txt` com o passo a passo e o código pronto (Python, Node ou curl). O endereço que vai no
`.env` é o pelo qual o painel foi aberto; para fixar outro (ex.: o IP da máquina), use `APLOGS_URL_PUBLICA`.
Como o `.env` leva a chave de envio, só o papel gestor vê o botão.

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

O painel usa estas rotas (pessoa logada, conforme o papel), que também aceitam o header
`x-api-secret: <APLOGS_SECRET>` (scripts: podem tudo). Pelo navegador, o que muda dado exige o header `X-Web: 1`.
Documentação interativa em `/api/docs`.

| Método | Rota | |
|---|---|---|
| GET | `/api/health` | Saúde (sem senha) |
| GET | `/api/painel` | Visão geral: serviços, status, linha do tempo, totais |
| GET/PATCH/DELETE | `/api/servicos/{slug}` | Detalhe · editar (nome, descricao, grupo, repositorio, tolerancia_s, monitorar) · apagar |
| GET | `/api/logs` | `?servico=&nivel=&modulo=&q=&antes=&depois=&limite=` |
| GET | `/api/problemas` | `?servico=&status=abertos\|resolvidos\|todos` |
| POST | `/api/problemas/{id}/reconhecer` · `/resolver` | |
| GET | `/api/jobs` | `?servico=&status=&tipo=&q=` |
| GET | `/api/filas` | Filas + amostras da última hora |
| GET | `/api/heartbeats` | `?servico=&limite=` |
| GET/POST | `/api/integracao` | Sugestões do formulário · cadastra o serviço e gera `env` + `txt` (gestor) |

Entrada do painel:

| Método | Rota | |
|---|---|---|
| POST | `/api/auth/login` | `{"email", "senha"}` do apusers → cookie |
| POST | `/api/auth/logout` | Encerra a sessão (aqui e no apusers) |
| GET | `/api/auth/sessao` | Quem está logado, papel e permissões |
| POST | `/api/auth/cadastro` | Conta nova no apusers (pendente até o administrador aprovar) |
| POST | `/api/atividade` | Tempo de tela e presença (formato do apusers) |

## Desenvolvimento e testes

```bash
cd api
pip install -r requirements.txt pytest httpx
python -m pytest tests -q                       # sqlite + réplica do apusers (tests/mock_apusers.py)
TEST_DATABASE_URL="mysql+pymysql://usuario:senha@127.0.0.1:3307/aplogs_teste?charset=utf8mb4" \
  python -m pytest tests -q                     # MySQL de verdade (banco vazio só para teste)
```

Rodar fora do Docker: `DATABASE_URL=sqlite:///aplogs.db APLOGS_SECRET=x APLOGS_INGEST_KEY=y APUSERS_URL=http://127.0.0.1:3080 APUSERS_KEY=aps_... uvicorn app.main:app --port 3070`.

## Estrutura

```
api/app/
  main.py        app, entrada do painel (login central), arquivos do frontend
  auth.py        sessões do painel, exigir(ação), x-api-secret e chave de ingestão
  acesso.py      login central (apusers): papéis, permissões e atividade
  apusers_client.py  cliente do apusers (copiado de Grupo-Studio/apusers)
  ingestao.py    POST /api/ingest/*  (serviços → aplogs)
  painel.py      rotas do painel (cada uma com a permissão que exige)
  integracao.py  "Gerenciar integração": cadastra o serviço, gera o .env e o .txt de instruções
  monitor.py     monitor de heartbeat (offline) + limpeza por retenção
  servicos.py    regras: cadastro, status, problemas
  modelos.py     tabelas (SQLAlchemy)
  static/        painel (HTML + CSS + JS puro, sem build)
clientes/python/aplogs_client.py   cliente para robôs em Python
docs/INTEGRACAO.md                 contrato de envio
docs/IMPLEMENTACAO.md              como funciona por dentro (arquitetura, banco, auth, status, testes)
```
