"""
ingestao.py
===========
Rotas que os serviços usam para mandar dados (header x-api-key). Todas recebem "servico"
(o identificador, ex.: "rpa-procuracoes-eletronicas"); o serviço é cadastrado no primeiro envio.

  POST /api/ingest/heartbeat   "estou vivo" + status + dados livres (a cada N segundos)
  GET|POST /api/ingest/heartbeat/{servico}   o mesmo, só pela URL (cron, curl, monitores externos)
  POST /api/ingest/logs        uma linha de log ({"servico", "mensagem"}) ou um lote ({"servico", "logs": [...]})
  POST /api/ingest/problemas   abre/resolve problemas (com "sincronizar": envio de estado)
  POST /api/ingest/jobs        lote de jobs (upsert pelo id do job no serviço)
  POST /api/ingest/filas       foto das filas (upsert pelo nome)

Chave: header x-api-key, Authorization: Bearer ou ?key= (veja auth.ingestao).
Contrato completo: docs/INTEGRACAO.md
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import auth, config
from . import servicos as sv
from .db import agora, get_db, iso
from .modelos import Fila, FilaAmostra, Heartbeat, Job, Log, Problema

router = APIRouter(prefix="/api/ingest", tags=["ingestão"], dependencies=[Depends(auth.ingestao)])


def _servico(db: Session, identificador: str, nome: str | None = None):
    try:
        return sv.obter_ou_criar(db, identificador, nome)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ── Heartbeat ────────────────────────────────────────────────────────────────

class HeartbeatIn(BaseModel):
    servico:     str = Field(..., max_length=120)
    nome:        str | None = Field(None, max_length=160)   # nome amigável (ex.: "RPA Procurações Eletrônicas")
    descricao:   str | None = None
    grupo:       str | None = Field(None, max_length=80)
    versao:      str | None = Field(None, max_length=80)
    host:        str | None = Field(None, max_length=160)
    ambiente:    str | None = Field(None, max_length=40)
    repositorio: str | None = Field(None, max_length=300)   # dono/repo ou URL do GitHub
    branch:      str | None = Field(None, max_length=120)   # branch que está rodando
    commit:      str | None = Field(None, max_length=64)    # commit que está rodando (sha)
    intervalo_s: int | None = Field(None, ge=1, le=86400)   # de quanto em quanto tempo o serviço manda heartbeat
    status:      str | None = "ok"                          # ok | degradado | erro | parando
    mensagem:    str | None = None
    iniciado_em: str | float | None = None                  # quando o processo subiu (ISO ou epoch)
    dados:       dict[str, Any] | None = None               # qualquer coisa (aparece no painel)


@router.post("/heartbeat")
def heartbeat(hb: HeartbeatIn, db: Session = Depends(get_db)):
    return _registrar_heartbeat(db, hb)


@router.api_route("/heartbeat/{servico}", methods=["GET", "POST"])
def heartbeat_url(servico: str, status: str = "ok", mensagem: str | None = None, intervalo_s: int | None = None,
                  nome: str | None = None, db: Session = Depends(get_db)):
    """Heartbeat sem corpo, para quem só consegue chamar uma URL:
    curl "http://<ip>:3070/api/ingest/heartbeat/robo-x?key=<chave>&intervalo_s=300" """
    if intervalo_s is not None and not 1 <= intervalo_s <= 86400:
        raise HTTPException(422, "intervalo_s deve ficar entre 1 e 86400")
    try:
        hb = HeartbeatIn(servico=servico, status=status, mensagem=mensagem, intervalo_s=intervalo_s,
                         nome=sv.corta(nome, 160))
    except ValidationError as e:
        raise HTTPException(422, "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()))
    return _registrar_heartbeat(db, hb)


def _registrar_heartbeat(db: Session, hb: HeartbeatIn) -> dict:
    s = _servico(db, hb.servico, hb.nome)
    momento = agora()
    if hb.nome and not s.nome_editado:
        s.nome = sv.corta(hb.nome, 160)
    for campo in ("descricao", "grupo", "versao", "host", "ambiente"):
        valor = getattr(hb, campo)
        if valor is not None and not (campo == "descricao" and s.nome_editado):
            setattr(s, campo, valor)
    if hb.intervalo_s:
        s.intervalo_s = hb.intervalo_s
    if hb.repositorio and not s.repositorio:        # o heartbeat só preenche; quem troca é o painel
        try:
            s.repositorio = sv.normalizar_repositorio(hb.repositorio)
        except ValueError:
            pass                                    # repositório estranho não derruba o heartbeat
    if hb.branch is not None:
        s.branch = hb.branch.strip() or None
    if hb.commit is not None:
        s.commit_sha = hb.commit.strip() or None
    s.status_reportado = sv.normalizar_status_hb(hb.status)
    s.mensagem = sv.corta(hb.mensagem, 500)
    if hb.iniciado_em is not None:
        s.iniciado_em = sv.data_utc(hb.iniciado_em)
    s.ultimo_heartbeat_em = momento
    s.dados = hb.dados
    db.add(Heartbeat(servico_id=s.id, recebido_em=momento, status=s.status_reportado,
                     mensagem=s.mensagem, dados=hb.dados))
    sv.aplicar_status(db, s, momento)
    db.commit()
    return {"ok": True, "servico": s.slug, "status": s.status, "recebido_em": iso(momento),
            "tolerancia_s": sv.tolerancia(s)}


# ── Logs ─────────────────────────────────────────────────────────────────────

class LogIn(BaseModel):
    mensagem: str
    nivel:    str | int | None = "INFO"
    ts:       str | float | None = None        # quando aconteceu (ISO ou epoch); vazio = agora
    modulo:   str | None = None
    exc:      str | None = None                # traceback
    contexto: dict[str, Any] | None = None


class LogsIn(BaseModel):
    """Lote ({"servico", "logs": [...]}) ou uma linha só, com os campos do log no próprio corpo
    ({"servico", "mensagem", "nivel", ...})."""
    servico:  str = Field(..., max_length=120)
    nome:     str | None = None
    logs:     list[LogIn] | None = None
    mensagem: str | None = None
    nivel:    str | int | None = "INFO"
    ts:       str | float | None = None
    modulo:   str | None = None
    exc:      str | None = None
    contexto: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _uma_ou_lote(self):
        if self.logs is None and self.mensagem is not None:
            self.logs = [LogIn(mensagem=self.mensagem, nivel=self.nivel, ts=self.ts, modulo=self.modulo,
                               exc=self.exc, contexto=self.contexto)]
        if not self.logs:
            raise ValueError("mande 'mensagem' (uma linha) ou 'logs' (lista com pelo menos uma linha)")
        return self


@router.post("/logs")
def logs(corpo: LogsIn, db: Session = Depends(get_db)):
    if len(corpo.logs) > config.MAX_LOGS_POR_ENVIO:
        raise HTTPException(413, f"No máximo {config.MAX_LOGS_POR_ENVIO} logs por envio")
    s = _servico(db, corpo.servico, corpo.nome)
    momento = agora()
    linhas = []
    for item in corpo.logs:
        nivel = sv.normalizar_nivel(item.nivel)
        linhas.append(Log(servico_id=s.id, ts=sv.data_utc(item.ts) or momento, recebido_em=momento, nivel=nivel,
                          nivel_num=sv.NIVEIS[nivel], modulo=sv.corta(item.modulo, 80),
                          mensagem=sv.corta(item.mensagem, config.MAX_MENSAGEM) or "",
                          exc=sv.corta(item.exc, 1_000_000), contexto=item.contexto))
    db.add_all(linhas)
    db.commit()
    return {"ok": True, "servico": s.slug, "recebidos": len(linhas)}


# ── Problemas ────────────────────────────────────────────────────────────────

class ProblemaIn(BaseModel):
    chave:      str = Field(..., min_length=1, max_length=191)   # identifica o problema (ex.: "sessao:3")
    titulo:     str | None = Field(None, max_length=300)
    descricao:  str | None = None
    severidade: str | None = "erro"                             # info | aviso | erro | critico
    resolvido:  bool = False
    dados:      dict[str, Any] | None = None


class ProblemasIn(BaseModel):
    servico:     str = Field(..., max_length=120)
    nome:        str | None = None
    problemas:   list[ProblemaIn] = Field(default_factory=list)
    # Envio de ESTADO: problemas abertos deste serviço cuja chave começa com um destes prefixos
    # e que NÃO vieram na lista são resolvidos. Ex.: ["sessao:"] → manda só as sessões com problema.
    sincronizar: list[str] | None = None


@router.post("/problemas")
def problemas(corpo: ProblemasIn, db: Session = Depends(get_db)):
    s = _servico(db, corpo.servico, corpo.nome)
    estado = corpo.sincronizar is not None
    abertos, resolvidos, chaves = 0, 0, set()
    for p in corpo.problemas:
        if p.resolvido:
            if sv.resolver_problema(db, s, p.chave, por="servico"):
                resolvidos += 1
            continue
        chaves.add(p.chave)
        sv.abrir_problema(db, s, p.chave, p.titulo or p.chave, p.descricao, sv.normalizar_severidade(p.severidade),
                          origem="servico", contar=not estado, dados=p.dados)
        abertos += 1
    if estado and corpo.sincronizar:
        db.flush()
        for p in db.scalars(select(Problema).where(Problema.servico_id == s.id, Problema.status != "resolvido",
                                                   Problema.origem == "servico")):
            if p.chave not in chaves and any(p.chave.startswith(px) for px in corpo.sincronizar if px):
                p.status, p.resolvido_em, p.resolvido_por = "resolvido", agora(), "servico"
                resolvidos += 1
    db.commit()
    return {"ok": True, "servico": s.slug, "abertos": abertos, "resolvidos": resolvidos}


# ── Jobs ─────────────────────────────────────────────────────────────────────

class JobIn(BaseModel):
    id:           str = Field(..., min_length=1, max_length=120)   # id do job no serviço
    tipo:         str | None = Field(None, max_length=60)
    nome:         str | None = Field(None, max_length=300)
    status:       str | None = "rodando"                          # fila | rodando | ok | erro | interrompido
    progresso:    float | None = Field(None, ge=0, le=100)
    etapa:        str | None = None
    mensagem:     str | None = None
    criado_em:    str | float | None = None
    iniciado_em:  str | float | None = None
    terminado_em: str | float | None = None
    duracao_s:    float | None = None
    dados:        dict[str, Any] | None = None


class JobsIn(BaseModel):
    servico: str = Field(..., max_length=120)
    nome:    str | None = None
    jobs:    list[JobIn] = Field(..., min_length=1, max_length=1000)


@router.post("/jobs")
def jobs(corpo: JobsIn, db: Session = Depends(get_db)):
    s = _servico(db, corpo.servico, corpo.nome)
    ids = [j.id for j in corpo.jobs]
    existentes = {j.externo_id: j for j in db.scalars(select(Job).where(Job.servico_id == s.id,
                                                                         Job.externo_id.in_(ids)))}
    momento = agora()
    for j in corpo.jobs:
        job = existentes.get(j.id)
        if not job:
            job = Job(servico_id=s.id, externo_id=j.id)
            db.add(job)
            existentes[j.id] = job
        status = sv.normalizar_status_job(j.status)
        job.status = status
        for campo in ("tipo", "nome", "progresso", "mensagem", "dados"):
            valor = getattr(j, campo)
            if valor is not None:
                setattr(job, campo, valor)
        job.etapa = sv.corta(j.etapa, 300) if j.etapa is not None or status in sv.STATUS_JOB_FINAIS else job.etapa
        for campo in ("criado_em", "iniciado_em", "terminado_em"):
            valor = sv.data_utc(getattr(j, campo))
            if valor:
                setattr(job, campo, valor)
        if status == "rodando" and not job.iniciado_em:
            job.iniciado_em = momento
        if status in sv.STATUS_JOB_FINAIS:
            job.terminado_em = job.terminado_em or momento
            if status == "ok" and job.progresso is not None:
                job.progresso = 100
        if j.duracao_s is not None:
            job.duracao_s = j.duracao_s
        elif job.iniciado_em and job.terminado_em:
            job.duracao_s = round((job.terminado_em - job.iniciado_em).total_seconds(), 1)
        job.atualizado_em = momento
    db.commit()
    return {"ok": True, "servico": s.slug, "recebidos": len(corpo.jobs)}


# ── Filas ────────────────────────────────────────────────────────────────────

class FilaIn(BaseModel):
    nome:       str = Field(..., min_length=1, max_length=120)
    pendentes:  int = Field(0, ge=0)
    rodando:    int = Field(0, ge=0)
    concluidos: int = Field(0, ge=0)
    erros:      int = Field(0, ge=0)
    workers:    int | None = Field(None, ge=0)
    dados:      dict[str, Any] | None = None


class FilasIn(BaseModel):
    servico: str = Field(..., max_length=120)
    nome:    str | None = None
    filas:   list[FilaIn] = Field(..., min_length=1, max_length=200)


AMOSTRA_MIN_S = 20     # no máximo uma amostra a cada 20 s por fila (gráfico)


@router.post("/filas")
def filas(corpo: FilasIn, db: Session = Depends(get_db)):
    s = _servico(db, corpo.servico, corpo.nome)
    momento = agora()
    existentes = {f.nome: f for f in db.scalars(select(Fila).where(Fila.servico_id == s.id))}
    for f in corpo.filas:
        fila = existentes.get(f.nome)
        if not fila:
            fila = Fila(servico_id=s.id, nome=f.nome)
            db.add(fila)
            db.flush()
            existentes[f.nome] = fila
            ultima = None
        else:
            ultima = db.scalar(select(FilaAmostra.ts).where(FilaAmostra.fila_id == fila.id)
                               .order_by(FilaAmostra.ts.desc()).limit(1))
        fila.pendentes, fila.rodando, fila.concluidos, fila.erros = f.pendentes, f.rodando, f.concluidos, f.erros
        fila.workers, fila.atualizado_em = f.workers, momento
        if f.dados is not None:
            fila.dados = f.dados
        if not ultima or (momento - ultima).total_seconds() >= AMOSTRA_MIN_S:
            db.add(FilaAmostra(fila_id=fila.id, ts=momento, pendentes=f.pendentes, rodando=f.rodando,
                               erros=f.erros))
    db.commit()
    return {"ok": True, "servico": s.slug, "recebidas": len(corpo.filas)}
