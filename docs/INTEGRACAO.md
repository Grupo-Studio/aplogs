# Integração com a aplogs

Para conectar um serviço não precisa configurar nada na aplogs: é só chamar a API. O serviço
**se cadastra sozinho** na primeira chamada e já aparece no painel.

## Em 1 minuto

**Mais fácil:** no painel, *Serviços → Gerenciar integração* (papel gestor). Ele gera o bloco do `.env` e um `.txt`
com o passo a passo para o seu serviço. O resto desta página é o que está por trás disso.

Variáveis que o painel gera (o cliente Python lê todas com `Aplogs.do_ambiente()`):

| Variável | |
|---|---|
| `APLOGS_URL` | Endereço da aplogs visto pelo serviço |
| `APLOGS_KEY` | Chave de envio (`APLOGS_INGEST_KEY` da aplogs) |
| `APLOGS_SERVICO` | Identificador do serviço (campo `servico`) |
| `APLOGS_NOME` | Nome amigável (campo `nome`) |
| `APLOGS_INTERVALO_S` | Intervalo do heartbeat em segundos |
| `APLOGS_GRUPO`, `APLOGS_REPOSITORIO` | Opcionais |

**Endereço** (`APLOGS_URL` no seu serviço):

| De onde o serviço chama | URL |
|---|---|
| Container na mesma máquina, na rede `aplogs` (veja abaixo) | `http://aplogs-api:8000` |
| Outra máquina da rede (ou fora do Docker) | `http://<ip-da-aplogs>:3070` |

**Chave** (`APLOGS_INGEST_KEY` do `.env` da aplogs). Mande do jeito que for mais fácil:
header `x-api-key: <chave>`, header `Authorization: Bearer <chave>` ou `?key=<chave>` na URL.

**Pronto.** Os três envios que cobrem quase tudo:

```bash
# 1) "estou vivo" (a cada 30 s). Se parar, o serviço fica OFFLINE e abre um alerta no painel.
curl -X POST "$APLOGS_URL/api/ingest/heartbeat" -H "x-api-key: $APLOGS_KEY" -H "Content-Type: application/json" \
  -d '{"servico": "robo-x", "nome": "Robô X", "intervalo_s": 30}'

# 2) uma linha de log
curl -X POST "$APLOGS_URL/api/ingest/logs" -H "x-api-key: $APLOGS_KEY" -H "Content-Type: application/json" \
  -d '{"servico": "robo-x", "nivel": "ERROR", "mensagem": "Falha ao baixar a DCTF"}'

# 3) heartbeat só pela URL (cron, script shell, Uptime Kuma, Task Scheduler...)
curl "$APLOGS_URL/api/ingest/heartbeat/robo-x?key=$APLOGS_KEY&intervalo_s=300"
```

Node (18+, sem biblioteca):

```js
const APLOGS_URL = process.env.APLOGS_URL, APLOGS_KEY = process.env.APLOGS_KEY;
async function aplogs(tipo, corpo) {
  try {
    await fetch(`${APLOGS_URL}/api/ingest/${tipo}`, {
      method: "POST", signal: AbortSignal.timeout(5000),
      headers: { "Content-Type": "application/json", "x-api-key": APLOGS_KEY },
      body: JSON.stringify({ servico: "meu-servico", nome: "Meu Serviço", ...corpo }),
    });
  } catch { /* a aplogs fora nunca derruba o serviço */ }
}
setInterval(() => aplogs("heartbeat", { intervalo_s: 30 }), 30_000);
aplogs("logs", { nivel: "INFO", mensagem: "subiu" });
```

Python: copie `clientes/python/aplogs_client.py` (só biblioteca padrão), ou veja o *Exemplo mínimo* no fim.

### Rede Docker (serviço na mesma máquina)

A aplogs cria a rede `aplogs`. No `docker-compose.yml` do outro serviço:

```yaml
services:
  meu-servico:
    environment:
      APLOGS_URL: http://aplogs-api:8000
    networks: [default, aplogs]

networks:
  aplogs:
    external: true
```

Suba a aplogs primeiro (é ela que cria a rede). Só a API fica nessa rede; o banco não.

---

## Detalhes do contrato

Todo envio é `POST <APLOGS_URL>/api/ingest/<tipo>` com:

- a chave (veja acima)
- header `Content-Type: application/json`
- campo `servico`: identificador fixo do serviço (minúsculas, números, `-`, `_`, `.`; ex.: `robo-dctf`)
- campo `nome` (opcional): nome amigável. Vale para o cadastro automático e o heartbeat atualiza.
  Se alguém renomear no painel, o painel ganha.

O serviço é cadastrado no primeiro envio, de qualquer tipo. Datas: ISO 8601 (sem fuso = UTC) ou epoch em segundos.
Resposta de erro: `{"ok": false, "error": "...", "detalhes": [...]}`.

**Regra de ouro para quem integra:** a aplogs nunca pode derrubar o serviço. Use timeout curto
(≈5 s), engula as falhas e mande em segundo plano. O cliente `clientes/python/aplogs_client.py` já faz isso.

---

## Heartbeat — `POST /api/ingest/heartbeat`

Mande a cada `intervalo_s` segundos (30 é um bom padrão).

```json
{
  "servico": "rpa-procuracoes-eletronicas",
  "nome": "RPA Procurações Eletrônicas",
  "grupo": "Receita",
  "descricao": "…",
  "versao": "2.5.0",
  "host": "vm-receita",
  "ambiente": "producao",
  "repositorio": "Grupo-Studio/aplogin",
  "branch": "Receita-aplogs",
  "commit": "39d86b0f3e2a1c…",
  "intervalo_s": 30,
  "status": "ok",
  "mensagem": "9/9 sessões ativas",
  "iniciado_em": "2026-11-10T08:00:00Z",
  "dados": {"sessoes": {"ativas": 9, "total": 9}}
}
```

| `status` | Efeito no painel |
|---|---|
| `ok` | online |
| `degradado` / `erro` | degradado (amarelo), com a `mensagem` no card |
| `parando` | offline na hora, como "foi parado" (desligamento normal). Mande ao desligar. |

- **Offline:** sem heartbeat por mais que a tolerância (padrão `max(3 × intervalo_s, 60 s)`; ajustável no painel),
  o monitor marca offline e abre o problema `aplogs:offline` (crítico). Ele é resolvido sozinho no próximo heartbeat.
- `dados` é livre. Os campos simples (até 2 níveis) aparecem no card; o JSON inteiro, no detalhe.
- `repositorio` / `branch` / `commit`: veja *Repositório e versão rodando*, logo abaixo.

### Repositório e versão rodando

Cada serviço pode ter o **repositório do GitHub** (`dono/repo` ou a URL) e dizer **qual branch e commit
estão rodando**. O card mostra `⎇ dono/repo · branch @ commit`, tudo com link para o GitHub. Assim dá para
ver de relance qual versão de cada robô está no ar.

- `repositorio`: o heartbeat só preenche se o serviço ainda não tiver um. Para trocar, use o painel
  (detalhe do serviço → Configuração). Também dá para preencher **só pelo painel**, sem mexer no serviço.
- `branch` / `commit`: o heartbeat sempre atualiza (é o que está rodando agora).

Num serviço em Docker, o código não tem o `.git`; passe o commit no build. No `Dockerfile`:

```dockerfile
ARG GIT_COMMIT=""
ARG GIT_BRANCH=""
ENV GIT_COMMIT=$GIT_COMMIT GIT_BRANCH=$GIT_BRANCH
```

No `docker-compose.yml`:

```yaml
    build:
      context: ./api
      args:
        GIT_COMMIT: ${GIT_COMMIT:-}
        GIT_BRANCH: ${GIT_BRANCH:-}
```

E suba assim (PowerShell, no Windows):

```powershell
$env:GIT_COMMIT = git rev-parse HEAD; $env:GIT_BRANCH = git branch --show-current; docker compose up -d --build
```

O serviço manda `os.getenv("GIT_COMMIT")` / `process.env.GIT_COMMIT` no heartbeat (o cliente Python já faz isso sozinho).

**Pela URL** — `GET` ou `POST /api/ingest/heartbeat/{servico}?key=<chave>`, sem corpo. Parâmetros opcionais:
`status`, `mensagem`, `intervalo_s`, `nome`. Para quem só consegue chamar uma URL (cron, script, monitor externo).
Ponha em `intervalo_s` de quanto em quanto tempo a URL é chamada, senão o serviço cai como offline entre as chamadas.

## Logs — `POST /api/ingest/logs`

Uma linha: `{"servico": "robo-dctf", "nivel": "ERROR", "mensagem": "…"}` (os campos abaixo valem também).

Em lote (até 2000 por envio):

```json
{
  "servico": "robo-dctf",
  "logs": [
    {"ts": "2026-11-10T13:02:11.120Z", "nivel": "ERROR", "modulo": "download",
     "mensagem": "Falha ao baixar a DCTF de 12.345.678/0001-90",
     "exc": "Traceback (most recent call last): …",
     "contexto": {"cnpj": "12345678000190", "tentativa": 3}}
  ]
}
```

`nivel`: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` (aceita `warn`, `aviso`, `erro`, `fatal`, `critico`
ou o número do `logging`). Sem `ts` = hora da chegada.

## Problemas — `POST /api/ingest/problemas`

Um problema é identificado pela `chave` dentro do serviço. Enquanto estiver aberto, um envio com a
mesma chave **atualiza** o problema em vez de abrir outro.

**Evento**: cada envio é uma ocorrência nova (soma em `ocorrencias`).

```json
{"servico": "robo-dctf", "problemas": [
  {"chave": "banco:conexao", "titulo": "Sem conexão com o banco", "descricao": "timeout após 30 s",
   "severidade": "erro"}
]}
```

**Resolver:** `{"servico": "robo-dctf", "problemas": [{"chave": "banco:conexao", "resolvido": true}]}`

**Estado** (`sincronizar`): mande a lista **completa** dos problemas que existem agora, sob certos prefixos.
Os problemas abertos com esses prefixos que não vierem na lista são resolvidos. Repetir o envio não soma
ocorrências. É o jeito mais simples para condições contínuas (sessão caída, certificado vencendo):

```json
{"servico": "rpa-procuracoes-eletronicas", "sincronizar": ["sessao:"], "problemas": [
  {"chave": "sessao:3", "titulo": "Sessão inativa: Studio Varejo", "severidade": "aviso"}
]}
```

`severidade`: `info`, `aviso`, `erro`, `critico`. No painel, um problema pode ser **reconhecido** (continua
aberto, mas alguém já está olhando) ou **resolvido** na mão.

## Jobs — `POST /api/ingest/jobs`

Upsert pelo `id` (o id do job no seu serviço). Mande quando o job muda. Só os campos enviados são atualizados.

```json
{"servico": "robo-dctf", "jobs": [
  {"id": "lote-2026-11-10", "tipo": "download_dctf", "nome": "Lote diário", "status": "rodando",
   "progresso": 42.5, "etapa": "empresa 17 de 40", "iniciado_em": "2026-11-10T06:00:00Z",
   "mensagem": null, "dados": {"empresas": 40}}
]}
```

`status`: `fila`, `rodando`, `ok`, `erro`, `interrompido` (aceita sinônimos em inglês e português).
Ao terminar (`ok`/`erro`/`interrompido`) sem `terminado_em`, vale a hora da chegada. A duração é calculada se não vier.

## Filas — `POST /api/ingest/filas`

Foto atual de cada fila (upsert pelo `nome`). Mande junto com o heartbeat.

```json
{"servico": "rpa-procuracoes-eletronicas", "filas": [
  {"nome": "busca_cnpj", "pendentes": 120, "rodando": 4, "concluidos": 830, "erros": 2, "workers": 4}
]}
```

A aplogs guarda uma amostra a cada ≥ 20 s para o gráfico da última hora.

---

## Exemplo mínimo (sem cliente, qualquer linguagem)

```python
import json, time, urllib.request

def enviar(tipo, corpo):
    req = urllib.request.Request(f"http://192.168.1.100:3070/api/ingest/{tipo}", method="POST",
        data=json.dumps({"servico": "robo-x", "nome": "Robô X", **corpo}).encode(),
        headers={"Content-Type": "application/json", "x-api-key": "<APLOGS_INGEST_KEY>"})
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass   # a aplogs fora nunca derruba o robô

while True:
    enviar("heartbeat", {"intervalo_s": 30, "status": "ok"})
    time.sleep(30)
```
