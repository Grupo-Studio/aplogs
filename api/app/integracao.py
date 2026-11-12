"""
integracao.py
=============
"Gerenciar integração" no painel: a pessoa diz o que é o serviço e a aplogs devolve
  - o bloco para colar no .env do serviço (endereço, chave de envio, identificador...)
  - um .txt com o passo a passo (código pronto para Python, Node ou curl, rede Docker, como conferir)
e já deixa o serviço cadastrado (aparece no painel como "Sem heartbeat" até o primeiro envio).

O .env leva a chave de envio (APLOGS_INGEST_KEY): só quem pode "gerenciar" (gestor) usa estas rotas.

  GET  /api/integracao   valores sugeridos para o formulário (endereço da aplogs, intervalo padrão)
  POST /api/integracao   cadastra/atualiza o serviço e gera o .env e o .txt
"""

import re
from datetime import timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import acesso, auth, config
from . import servicos as sv
from .db import agora, get_db
from .modelos import Servico
from .painel import servico_json

router = APIRouter(prefix="/api/integracao", tags=["integração"], dependencies=[Depends(auth.exigir("gerenciar"))])

URL_DOCKER = "http://aplogs-api:8000"       # containers na mesma máquina, na rede Docker "aplogs"
_SEM_CACHE = {"Cache-Control": "no-store"}  # a resposta leva a chave de envio


def url_sugerida(request: Request) -> str:
    """APLOGS_URL_PUBLICA, se definida; senão o endereço pelo qual a pessoa abriu o painel."""
    if config.APLOGS_URL_PUBLICA:
        return config.APLOGS_URL_PUBLICA
    host = request.headers.get("host") or f"{request.url.hostname}:{request.url.port or 80}"
    return f"{request.url.scheme}://{host}"


@router.get("")
def sugestoes(request: Request, response: Response):
    response.headers.update(_SEM_CACHE)
    return {"ok": True, "url_rede": url_sugerida(request), "url_docker": URL_DOCKER,
            "intervalo_padrao_s": config.HEARTBEAT_INTERVALO_PADRAO_S,
            "tolerancia_minima_s": config.TOLERANCIA_MINIMA_S}


class IntegracaoIn(BaseModel):
    servico:     str = Field(..., min_length=1, max_length=80)        # identificador (slug)
    nome:        str = Field(..., min_length=1, max_length=160)
    descricao:   str | None = Field(None, max_length=2000)
    grupo:       str | None = Field(None, max_length=80)
    repositorio: str | None = Field(None, max_length=300)
    intervalo_s: int = Field(30, ge=5, le=86400)
    onde:        Literal["rede", "docker"] = "rede"                  # outra máquina | container na mesma máquina
    linguagem:   Literal["python", "node", "outro"] = "python"
    url:         str | None = Field(None, max_length=300)            # endereço da aplogs (onde = "rede")


@router.post("")
def gerar(corpo: IntegracaoIn, request: Request, response: Response, db: Session = Depends(get_db),
          ator=Depends(auth.exigir("gerenciar"))):
    try:
        slug = sv.normalizar_slug(corpo.servico)
        repositorio = sv.normalizar_repositorio(corpo.repositorio)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if corpo.onde == "docker":
        url = URL_DOCKER
    else:
        url = (corpo.url or "").strip().rstrip("/") or url_sugerida(request)
        if not re.match(r"^https?://[\w.\-\[\]:]+(/[^\s\"'<>]*)?$", url):
            raise HTTPException(422, f"Endereço da aplogs inválido: '{url}' (ex.: http://192.168.1.100:3070)")

    nome = corpo.nome.strip()
    grupo = (corpo.grupo or "").strip() or None
    descricao = (corpo.descricao or "").strip() or None
    quem = "script" if ator is auth.SCRIPT else ator.nome

    s, criado = sv.obter(db, slug), False
    if not s:
        s = Servico(slug=slug, nome=nome, status="desconhecido", status_desde=agora(), intervalo_s=corpo.intervalo_s)
        db.add(s)
        try:
            db.flush()
            criado = True
        except IntegrityError:                     # o serviço mandou o primeiro envio ao mesmo tempo
            db.rollback()
            s = sv.obter(db, slug)
    # Campo vazio não apaga o que já existe (limpar é na Configuração do serviço).
    s.nome, s.intervalo_s = nome, corpo.intervalo_s
    if grupo:
        s.grupo = grupo
    if descricao:
        s.descricao = descricao
    if repositorio:
        s.repositorio = repositorio
    if criado:
        sv.registrar_log(db, s, "INFO", f"Integração preparada no painel por {quem}. Aguardando o primeiro heartbeat.")
    db.commit()
    acesso.registrar_acao(ator, "integracao_gerada", s.slug, criado=criado, linguagem=corpo.linguagem, onde=corpo.onde)

    env = gerar_env(s, url)
    response.headers.update(_SEM_CACHE)
    return {"ok": True, "criado": criado, "servico": servico_json(s), "url": url, "env": env,
            "txt": gerar_txt(s, env, url, corpo.onde, corpo.linguagem, quem),
            "arquivo": f"aplogs-integracao-{s.slug}.txt"}


# ── .env ─────────────────────────────────────────────────────────────────────

def _valor_env(valor) -> str:
    """Valor de uma linha do .env que funcione no docker compose, no python-dotenv e no node --env-file.
    Aspas simples quando há caractere especial: o compose não troca $ dentro delas."""
    v = " ".join(str(valor).split())
    if not v or not re.search(r"[#$'\"\\`]", v):
        return v
    if "'" not in v:
        return f"'{v}'"
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def gerar_env(s: Servico, url: str) -> str:
    linhas = [
        f"# aplogs: {s.nome} ({s.slug})",
        f"APLOGS_URL={_valor_env(url)}",
        f"APLOGS_KEY={_valor_env(config.APLOGS_INGEST_KEY)}",
        f"APLOGS_SERVICO={s.slug}",
        f"APLOGS_NOME={_valor_env(s.nome)}",
        f"APLOGS_INTERVALO_S={s.intervalo_s}",
    ]
    if s.grupo:
        linhas.append(f"APLOGS_GRUPO={_valor_env(s.grupo)}")
    if s.repositorio:
        linhas.append(f"APLOGS_REPOSITORIO={_valor_env(s.repositorio)}")
    return "\n".join(linhas) + "\n"


# ── .txt ─────────────────────────────────────────────────────────────────────

def _titulo(n: int, texto: str) -> str:
    t = f"{n}. {texto}"
    return f"{t}\n{'-' * len(t)}"


def _codigo(texto: str) -> str:
    return "\n".join(("    " + l) if l else "" for l in texto.strip("\n").split("\n"))


_PYTHON = '''
# 1) Copie para o seu projeto o arquivo clientes/python/aplogs_client.py
#    (repositório Grupo-Studio/aplogs). Só usa a biblioteca padrão.
# 2) No início do programa:

from aplogs_client import Aplogs

aplogs = Aplogs.do_ambiente()       # lê APLOGS_URL, APLOGS_KEY, APLOGS_SERVICO... do ambiente
aplogs.iniciar()                    # heartbeat em segundo plano + envio dos logs
aplogs.capturar_logging("INFO")     # o que vai para o logging (INFO+) aparece na aplogs

# Opcional, quando fizer sentido:
aplogs.status("degradado", "2 de 9 sessões caíram")       # vai no próximo heartbeat; "ok" para voltar
aplogs.job("lote-42", nome="Lote 42", status="rodando", progresso=30, etapa="baixando")
aplogs.fila("pendentes", pendentes=12, rodando=1)
aplogs.problema("certificado:3", "Certificado vence em 5 dias", severidade="aviso")
aplogs.resolver("certificado:3")

# Ao desligar (já é chamado sozinho na saída normal do Python):
aplogs.parar()                      # avisa "parando": fica offline sem contar como queda

# Rodando fora do Docker, o Python não lê o .env sozinho: use python-dotenv
#   pip install python-dotenv      e no início:  from dotenv import load_dotenv; load_dotenv()
# ou passe os valores direto:  Aplogs(url, chave, servico, nome=..., intervalo_s=...)
'''

_NODE = '''
// Node 18+ (fetch nativo), sem biblioteca. Rodando fora do Docker: node --env-file=.env app.js
const A = {
  url: process.env.APLOGS_URL, key: process.env.APLOGS_KEY, servico: process.env.APLOGS_SERVICO,
  nome: process.env.APLOGS_NOME, intervalo: Number(process.env.APLOGS_INTERVALO_S || 30),
  grupo: process.env.APLOGS_GRUPO, repositorio: process.env.APLOGS_REPOSITORIO,
};
const iniciadoEm = new Date().toISOString();
let statusAtual = "ok", mensagemAtual = null;

async function aplogs(tipo, corpo) {        // nunca derruba o serviço
  try {
    await fetch(`${A.url}/api/ingest/${tipo}`, {
      method: "POST", signal: AbortSignal.timeout(5000),
      headers: { "Content-Type": "application/json", "x-api-key": A.key },
      body: JSON.stringify({ servico: A.servico, nome: A.nome, ...corpo }),
    });
  } catch { /* aplogs fora: tenta de novo no próximo ciclo */ }
}

const heartbeat = () => aplogs("heartbeat", {
  intervalo_s: A.intervalo, status: statusAtual, mensagem: mensagemAtual, iniciado_em: iniciadoEm,
  grupo: A.grupo, repositorio: A.repositorio, versao: process.env.npm_package_version,
});
heartbeat();
setInterval(heartbeat, A.intervalo * 1000).unref();

// logs:   aplogs("logs", { nivel: "ERROR", mensagem: "Falha ao baixar", modulo: "download" });
// status: statusAtual = "degradado"; mensagemAtual = "2 de 9 sessões caíram"; heartbeat();

for (const sinal of ["SIGTERM", "SIGINT"]) {
  process.once(sinal, async () => {
    statusAtual = "parando"; mensagemAtual = "desligamento normal";
    await heartbeat();                      // fica offline sem contar como queda
    process.exit(0);
  });
}
'''

_OUTRO = '''
# Qualquer linguagem ou ferramenta que faça uma requisição HTTP serve.
# Exemplos com curl (os valores vêm do .env):

# "estou vivo": repita a cada APLOGS_INTERVALO_S segundos
curl -X POST "$APLOGS_URL/api/ingest/heartbeat" -H "x-api-key: $APLOGS_KEY" \\
  -H "Content-Type: application/json" \\
  -d "{\\"servico\\": \\"$APLOGS_SERVICO\\", \\"intervalo_s\\": $APLOGS_INTERVALO_S, \\"status\\": \\"ok\\"}"

# heartbeat só pela URL (cron, Agendador de Tarefas, Uptime Kuma...)
curl "$APLOGS_URL/api/ingest/heartbeat/$APLOGS_SERVICO?key=$APLOGS_KEY&intervalo_s=$APLOGS_INTERVALO_S"

# uma linha de log
curl -X POST "$APLOGS_URL/api/ingest/logs" -H "x-api-key: $APLOGS_KEY" \\
  -H "Content-Type: application/json" \\
  -d "{\\"servico\\": \\"$APLOGS_SERVICO\\", \\"nivel\\": \\"ERROR\\", \\"mensagem\\": \\"Falha ao baixar\\"}"

# A chave também vale no header "Authorization: Bearer <chave>" ou em "?key=<chave>".
'''

_LINGUAGEM = {"python": ("Python", _PYTHON), "node": ("Node.js", _NODE), "outro": ("curl / qualquer HTTP", _OUTRO)}


def gerar_txt(s: Servico, env: str, url: str, onde: str, linguagem: str, quem: str) -> str:
    momento = agora().replace(tzinfo=timezone.utc).astimezone()        # fuso do container (TZ)
    tolerancia = sv.tolerancia(s)
    nome_ling, codigo = _LINGUAGEM[linguagem]
    partes = [
        f"INTEGRAÇÃO COM A APLOGS — {s.nome} ({s.slug})",
        "=" * 60,
        f"Gerado em {momento:%d/%m/%Y %H:%M} por {quem}.",
        "",
        "ATENÇÃO: este arquivo tem a CHAVE DE ENVIO da aplogs. Trate como senha:",
        "não suba para o git, não mande em grupo. Com ela dá para enviar dados (não para ler).",
        "",
        _titulo(1, "Cole no .env do serviço"),
        "",
        _codigo(env),
        "",
        "Confira se o .env está no .gitignore do serviço.",
        "",
        _titulo(2, "Rede"),
        "",
    ]
    if onde == "docker":
        partes += [
            "O serviço roda num container NESTA MESMA MÁQUINA da aplogs e fala com ela pela rede",
            f"Docker \"aplogs\" ({URL_DOCKER}). No docker-compose.yml do serviço:",
            "",
            _codigo('''
services:
  meu-servico:
    env_file: .env
    networks: [default, aplogs]

networks:
  aplogs:
    external: true
'''),
            "",
            "A rede \"aplogs\" é criada quando a aplogs sobe. Depois: docker compose up -d",
        ]
    else:
        partes += [
            f"O serviço chama a aplogs em {url}.",
            "Teste da máquina do serviço (deve responder {\"ok\": true, ...}):",
            "",
            _codigo(f"curl {url}/api/health"),
            "",
            "Não respondeu? Libere a porta da aplogs no firewall da máquina dela e confira o IP.",
            "Se o serviço roda em Docker, o env_file: .env do docker-compose.yml já carrega as variáveis.",
        ]
    partes += [
        "",
        _titulo(3, f"Código ({nome_ling})"),
        "",
        _codigo(codigo),
        "",
        _titulo(4, "Como conferir"),
        "",
        f"- Enquanto o serviço não manda nada, ele aparece no painel ({url if onde == 'rede' else 'aplogs'})",
        "  como \"Sem heartbeat\". No primeiro heartbeat, fica \"Online\".",
        f"- Heartbeat a cada {s.intervalo_s} s. Sem heartbeat por {tolerancia} s, o serviço fica OFFLINE",
        "  e abre um problema crítico, que se resolve sozinho quando ele volta.",
        "- Status \"degradado\" ou \"erro\" no heartbeat deixa o card amarelo (Degradado).",
        "- Status \"parando\" no último heartbeat = desligamento normal (\"foi parado\").",
        "",
        _titulo(5, "O que mais dá para enviar"),
        "",
        "- Logs:      POST /api/ingest/logs       uma linha ou um lote (até 2000)",
        "- Problemas: POST /api/ingest/problemas  abre/resolve por chave (sem duplicar)",
        "- Jobs:      POST /api/ingest/jobs       execuções com status, progresso e etapa",
        "- Filas:     POST /api/ingest/filas      na fila / rodando / concluídos / erros",
        "",
        "Contrato completo: docs/INTEGRACAO.md no repositório Grupo-Studio/aplogs.",
        "",
    ]
    return "\n".join(partes)
