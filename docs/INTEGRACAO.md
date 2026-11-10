# Integração com a aplogs

Todo envio é `POST http://<ip>:3070/api/ingest/<tipo>` com:

- header `x-api-key: <APLOGS_INGEST_KEY>`
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

## Logs — `POST /api/ingest/logs`

Em lote (até 2000 por envio).

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
