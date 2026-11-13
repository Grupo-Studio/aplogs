"""
servicos.py
===========
Regras dos serviços: cadastro automático, normalização dos valores que chegam,
cálculo do status (online / degradado / offline) e o que acontece quando o status muda
(log da aplogs + problema "offline" aberto/resolvido sozinho).
"""

import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import config
from .db import agora
from .modelos import Log, Problema, Servico

logger = logging.getLogger("aplogs.servicos")

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
CHAVE_OFFLINE = "aplogs:offline"

NIVEIS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
_NIVEL_SINONIMOS = {"WARN": "WARNING", "AVISO": "WARNING", "ERRO": "ERROR", "FATAL": "CRITICAL",
                    "CRITICO": "CRITICAL", "CRÍTICO": "CRITICAL", "TRACE": "DEBUG", "NOTICE": "INFO"}

_STATUS_HB = {"ok": "ok", "online": "ok", "up": "ok", "healthy": "ok", "ativo": "ok",
              "degradado": "degradado", "degraded": "degradado", "warn": "degradado", "warning": "degradado",
              "aviso": "degradado", "erro": "erro", "error": "erro", "falha": "erro", "down": "erro",
              "parando": "parando", "stopping": "parando", "parado": "parando", "stopped": "parando"}

_STATUS_JOB = {"fila": "fila", "queued": "fila", "pending": "fila", "pendente": "fila", "aguardando": "fila",
               "rodando": "rodando", "running": "rodando", "executando": "rodando", "em_andamento": "rodando",
               "ok": "ok", "sucesso": "ok", "success": "ok", "done": "ok", "concluido": "ok", "concluida": "ok",
               "completed": "ok", "erro": "erro", "error": "erro", "failed": "erro", "falhou": "erro",
               "falha": "erro", "interrompido": "interrompido", "interrompida": "interrompido",
               "cancelado": "interrompido", "cancelada": "interrompido", "cancelled": "interrompido",
               "canceled": "interrompido", "stopped": "interrompido", "parado": "interrompido"}
STATUS_JOB_FINAIS = {"ok", "erro", "interrompido"}

SEVERIDADES = {"info": "info", "aviso": "aviso", "warning": "aviso", "warn": "aviso", "erro": "erro",
               "error": "erro", "critico": "critico", "crítico": "critico", "critical": "critico"}


# ── Normalização ──────────────────────────────────────────────────────────────

def normalizar_slug(valor: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", (valor or "").strip().lower()).strip("-")
    if not SLUG_RE.match(slug):
        raise ValueError(f"identificador de serviço inválido: '{valor}' (use letras, números, '-', '_' ou '.')")
    return slug


def normalizar_nivel(valor) -> str:
    if isinstance(valor, int):
        return min(NIVEIS, key=lambda n: abs(NIVEIS[n] - valor))
    v = str(valor or "INFO").strip().upper()
    v = _NIVEL_SINONIMOS.get(v, v)
    return v if v in NIVEIS else "INFO"


def normalizar_status_hb(valor) -> str:
    return _STATUS_HB.get(str(valor or "ok").strip().lower(), "ok")


def normalizar_status_job(valor) -> str:
    return _STATUS_JOB.get(str(valor or "fila").strip().lower().replace(" ", "_"), "rodando")


def normalizar_severidade(valor) -> str:
    return SEVERIDADES.get(str(valor or "erro").strip().lower(), "erro")


def normalizar_repositorio(valor) -> str | None:
    """Aceita "dono/repo", "github.com/dono/repo", https://..., git@host:dono/repo.git.
    Devolve a URL https sem ".git" (ou None se vazio). Só http(s): o valor vira link no painel."""
    v = str(valor or "").strip()
    if not v:
        return None
    m = re.match(r"^git@([\w.-]+):(.+)$", v)
    if m:
        v = f"https://{m.group(1)}/{m.group(2)}"
    elif re.match(r"^[\w.-]+/[\w.-]+$", v):
        v = f"https://github.com/{v}"
    elif re.match(r"^[\w-]+(\.[\w-]+)+/", v):            # github.com/dono/repo sem https://
        v = f"https://{v}"
    v = re.sub(r"\.git/?$", "", v).rstrip("/")
    if not re.match(r"^https?://[\w.-]+(:\d+)?/[^\s\"'<>]+$", v, re.IGNORECASE) or len(v) > 300:
        raise ValueError(f"repositório inválido: '{valor}' (use dono/repo ou a URL do GitHub)")
    return v


def normalizar_endereco(valor) -> str | None:
    """Onde o serviço atende na rede: "http://192.168.1.100:3067" (sem esquema, vira http://).
    Devolve a URL sem "/" no fim (ou None se vazio). Só http(s): o valor vira link no painel."""
    v = str(valor or "").strip()
    if not v:
        return None
    if not re.match(r"^[a-z][a-z0-9+.-]*://", v, re.IGNORECASE):
        v = f"http://{v}"
    v = v.rstrip("/")
    if not re.match(r"^https?://[\w.-]+(:\d{1,5})?(/[^\s\"'<>]*)?$", v, re.IGNORECASE) or len(v) > 300:
        raise ValueError(f"endereço inválido: '{valor}' (ex.: http://192.168.1.100:3067)")
    return v


def endereco_do_heartbeat(db: Session, s: Servico, valor) -> None:
    """APLOGS_ENDERECO que veio no heartbeat. Quem troca o endereço no painel ganha: o heartbeat só
    troca quando o valor que o serviço manda MUDA (o .env dele mudou = ele foi para outro lugar).
    Enquanto ele mandar o mesmo de sempre, fica o do painel. Nada disso apaga heartbeats ou logs."""
    try:
        novo = normalizar_endereco(valor)
    except ValueError:
        return                                       # endereço estranho não derruba o heartbeat
    if not novo or novo == s.endereco_reportado:
        return
    primeiro = s.endereco_reportado is None
    s.endereco_reportado = novo
    if primeiro and s.endereco_editado and s.endereco:
        return                                       # 1º envio depois de alguém trocar no painel: fica o do painel
    antes = s.endereco
    s.endereco, s.endereco_editado = novo, False
    if antes and antes != novo:
        registrar_log(db, s, "INFO", f"Endereço do serviço mudou (APLOGS_ENDERECO): {antes} → {novo}",
                      {"endereco_antes": antes, "endereco": novo, "origem": "heartbeat"})


def trocar_endereco_no_painel(db: Session, s: Servico, valor, quem: str) -> None:
    """Endereço trocado à mão (Configuração ou Gerenciar integração). Vale sobre o APLOGS_ENDERECO que o
    serviço continuar mandando, até o .env dele mudar. O histórico (heartbeats, logs) fica todo."""
    novo = normalizar_endereco(valor)                # ValueError sobe para a rota responder 422
    if novo == s.endereco:
        return
    antes = s.endereco
    s.endereco, s.endereco_editado = novo, novo is not None
    registrar_log(db, s, "INFO", f"Endereço trocado no painel por {quem}: {antes or '—'} → {novo or '—'}",
                  {"endereco_antes": antes, "endereco": novo, "origem": "painel", "por": quem})


def data_utc(valor) -> datetime | None:
    """ISO 8601 (com ou sem fuso; sem fuso = UTC) ou epoch em segundos → datetime UTC sem fuso."""
    if valor in (None, ""):
        return None
    try:
        if isinstance(valor, (int, float)):
            dt = datetime.fromtimestamp(float(valor), tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(valor).strip().replace("Z", "+00:00"))
    except (ValueError, OSError, OverflowError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def corta(valor, n: int) -> str | None:
    if valor is None:
        return None
    s = str(valor)
    return s if len(s) <= n else s[: n - 1] + "…"


# ── Cadastro ──────────────────────────────────────────────────────────────────

def obter(db: Session, slug: str) -> Servico | None:
    return db.scalar(select(Servico).where(Servico.slug == slug))


def obter_ou_criar(db: Session, identificador: str, nome: str | None = None) -> Servico:
    """O serviço se cadastra sozinho no primeiro envio (heartbeat, log, job...)."""
    slug = normalizar_slug(identificador)
    s = obter(db, slug)
    if s:
        return s
    s = Servico(slug=slug, nome=corta(nome or slug, 160), intervalo_s=config.HEARTBEAT_INTERVALO_PADRAO_S,
                status="desconhecido", status_desde=agora())
    db.add(s)
    try:
        db.commit()
    except IntegrityError:          # outro envio criou ao mesmo tempo
        db.rollback()
        return obter(db, slug)
    logger.info(f"Novo serviço conectado: {slug}")
    registrar_log(db, s, "INFO", f"Serviço '{s.nome}' conectado à aplogs.")
    db.commit()
    return s


# ── Status ────────────────────────────────────────────────────────────────────

def tolerancia(s: Servico) -> int:
    if s.tolerancia_s:
        return int(s.tolerancia_s)
    return max(3 * int(s.intervalo_s or config.HEARTBEAT_INTERVALO_PADRAO_S), config.TOLERANCIA_MINIMA_S)


def calcular_status(s: Servico, momento: datetime | None = None) -> str:
    """pausado | desconhecido | offline | degradado | online"""
    if not s.monitorar:
        return "pausado"
    if not s.ultimo_heartbeat_em:
        return "desconhecido"
    if s.status_reportado == "parando":
        return "offline"
    idade = ((momento or agora()) - s.ultimo_heartbeat_em).total_seconds()
    if idade > tolerancia(s):
        return "offline"
    if s.status_reportado in ("degradado", "erro"):
        return "degradado"
    return "online"


def registrar_log(db: Session, s: Servico, nivel: str, mensagem: str, contexto: dict | None = None):
    """Log da própria aplogs sobre o serviço (aparece junto com os logs dele, módulo 'aplogs')."""
    momento = agora()
    db.add(Log(servico_id=s.id, ts=momento, recebido_em=momento, nivel=nivel, nivel_num=NIVEIS[nivel],
               modulo="aplogs", mensagem=mensagem, contexto=contexto))


def _humano(segundos: float) -> str:
    segundos = int(segundos)
    if segundos < 90:
        return f"{segundos} s"
    if segundos < 5400:
        return f"{segundos // 60} min"
    return f"{segundos // 3600} h {segundos % 3600 // 60} min"


def aplicar_status(db: Session, s: Servico, momento: datetime | None = None) -> bool:
    """Recalcula o status e, se mudou, registra a transição. Não faz commit. True = mudou."""
    momento = momento or agora()
    novo = calcular_status(s, momento)
    antigo = s.status
    if novo == antigo:
        return False
    desde_antigo = s.status_desde
    s.status = novo
    s.status_desde = momento

    if novo == "offline":
        if s.status_reportado == "parando":
            titulo = f"{s.nome} foi parado"
            descricao = "O serviço avisou que estava parando (desligamento normal) e não mandou mais heartbeat."
        else:
            idade = (momento - s.ultimo_heartbeat_em).total_seconds() if s.ultimo_heartbeat_em else 0
            titulo = f"{s.nome} está offline"
            local = s.ultimo_heartbeat_em.replace(tzinfo=timezone.utc).astimezone()   # fuso do container (TZ)
            descricao = (f"Nenhum heartbeat há {_humano(idade)} (tolerância: {_humano(tolerancia(s))}). "
                         f"Último heartbeat recebido em {local:%d/%m/%Y %H:%M:%S}.")
        registrar_log(db, s, "ERROR", f"{titulo}. {descricao}")
        abrir_problema(db, s, CHAVE_OFFLINE, titulo, descricao, "critico", origem="monitor", contar=False)
    elif antigo == "offline" and novo in ("online", "degradado"):
        fora = f" depois de {_humano((momento - desde_antigo).total_seconds())} offline" if desde_antigo else ""
        registrar_log(db, s, "INFO", f"{s.nome} voltou a ficar online{fora}.")
        resolver_problema(db, s, CHAVE_OFFLINE, por="monitor")
    elif novo == "degradado":
        registrar_log(db, s, "WARNING", f"{s.nome} reportou status degradado: {s.mensagem or 'sem mensagem'}")
    elif antigo == "degradado" and novo == "online":
        registrar_log(db, s, "INFO", f"{s.nome} voltou ao normal.")
    elif novo == "pausado":
        registrar_log(db, s, "INFO", "Monitoramento pausado no painel.")
        resolver_problema(db, s, CHAVE_OFFLINE, por="painel")
    return True


# ── Problemas ─────────────────────────────────────────────────────────────────

def problema_aberto(db: Session, s: Servico, chave: str) -> Problema | None:
    return db.scalar(select(Problema).where(Problema.servico_id == s.id, Problema.chave == chave,
                                            Problema.status != "resolvido").order_by(Problema.id.desc()))


def abrir_problema(db: Session, s: Servico, chave: str, titulo: str, descricao: str | None, severidade: str,
                   origem: str = "servico", contar: bool = True, dados: dict | None = None) -> Problema:
    """Abre o problema, ou atualiza o que já está aberto com a mesma chave.
    contar=True soma uma ocorrência (cada envio é um acontecimento novo);
    contar=False só confirma que a condição continua (envio de estado, a cada ciclo)."""
    momento = agora()
    p = problema_aberto(db, s, chave)
    if p:
        p.titulo, p.descricao, p.severidade, p.ultima_vez = corta(titulo, 300), descricao, severidade, momento
        if dados is not None:
            p.dados = dados
        if contar:
            p.ocorrencias += 1
        return p
    p = Problema(servico_id=s.id, chave=corta(chave, 191), titulo=corta(titulo, 300), descricao=descricao,
                 severidade=severidade, status="aberto", origem=origem, ocorrencias=1, primeira_vez=momento,
                 ultima_vez=momento, dados=dados)
    db.add(p)
    return p


def resolver_problema(db: Session, s: Servico, chave: str, por: str) -> Problema | None:
    p = problema_aberto(db, s, chave)
    if p:
        p.status, p.resolvido_em, p.resolvido_por = "resolvido", agora(), por
    return p


def janela_heartbeats(momento: datetime, minutos: int) -> datetime:
    return momento - timedelta(minutes=minutos)
