"""
painel.py
=========
Rotas do painel (cookie do login central ou header x-api-secret).

Permissões (papel da pessoa no apusers; scripts com x-api-secret podem tudo):
  ver        todas as leituras                        visualizador, operador, gestor
  tratar     reconhecer / resolver problema           operador, gestor
  gerenciar  editar / apagar serviço                  gestor

  GET    /api/painel                      visão geral: serviços, status, linha do tempo de heartbeat, totais
  GET    /api/servicos/{slug}             detalhe de um serviço
  PATCH  /api/servicos/{slug}             nome, descrição, grupo, repositório, tolerância, monitorar
  DELETE /api/servicos/{slug}             apaga o serviço e tudo dele
  GET    /api/logs                        logs (?servico=&nivel=&modulo=&q=&antes=&depois=&limite=)
  GET    /api/logs/modulos                módulos que aparecem nos logs (?servico=)
  GET    /api/problemas                   problemas (?servico=&status=abertos|resolvidos|todos)
  POST   /api/problemas/{id}/reconhecer   "estou vendo" (continua aberto, sai do alerta)
  POST   /api/problemas/{id}/resolver     resolve na mão
  GET    /api/jobs                        jobs (?servico=&status=&tipo=&q=)
  GET    /api/filas                       filas + amostras da última hora (?servico=)
  GET    /api/heartbeats                  heartbeats recebidos (?servico=&limite=)
"""

from collections import defaultdict
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from . import acesso, auth
from . import servicos as sv
from .db import agora, get_db, iso
from .modelos import Fila, FilaAmostra, Heartbeat, Job, Log, Problema, Servico

router = APIRouter(prefix="/api", tags=["painel"], dependencies=[Depends(auth.painel)])

TIMELINE_MIN = 60          # minutos na linha do tempo de heartbeat dos cards


# ── Serialização ─────────────────────────────────────────────────────────────

def servico_json(s: Servico) -> dict:
    return {
        "id": s.id, "slug": s.slug, "nome": s.nome, "descricao": s.descricao, "grupo": s.grupo,
        "versao": s.versao, "host": s.host, "ambiente": s.ambiente,
        "repositorio": s.repositorio, "branch": s.branch, "commit": s.commit_sha,
        "status": s.status, "status_desde": iso(s.status_desde), "status_reportado": s.status_reportado,
        "mensagem": s.mensagem, "monitorar": s.monitorar, "intervalo_s": s.intervalo_s,
        "tolerancia_s": s.tolerancia_s, "tolerancia_efetiva_s": sv.tolerancia(s),
        "ultimo_heartbeat_em": iso(s.ultimo_heartbeat_em), "iniciado_em": iso(s.iniciado_em),
        "dados": s.dados, "criado_em": iso(s.criado_em),
    }


def log_json(l: Log, slug: str | None = None) -> dict:
    return {"id": l.id, "servico_id": l.servico_id, "servico": slug, "ts": iso(l.ts), "recebido_em": iso(l.recebido_em),
            "nivel": l.nivel, "modulo": l.modulo, "mensagem": l.mensagem, "exc": l.exc, "contexto": l.contexto}


def problema_json(p: Problema, slug: str | None = None) -> dict:
    return {"id": p.id, "servico_id": p.servico_id, "servico": slug, "chave": p.chave, "titulo": p.titulo,
            "descricao": p.descricao, "severidade": p.severidade, "status": p.status, "origem": p.origem,
            "ocorrencias": p.ocorrencias, "primeira_vez": iso(p.primeira_vez), "ultima_vez": iso(p.ultima_vez),
            "resolvido_em": iso(p.resolvido_em), "resolvido_por": p.resolvido_por, "dados": p.dados}


def job_json(j: Job, slug: str | None = None) -> dict:
    return {"id": j.id, "servico_id": j.servico_id, "servico": slug, "externo_id": j.externo_id, "tipo": j.tipo,
            "nome": j.nome, "status": j.status, "progresso": j.progresso, "etapa": j.etapa, "mensagem": j.mensagem,
            "criado_em": iso(j.criado_em), "iniciado_em": iso(j.iniciado_em), "terminado_em": iso(j.terminado_em),
            "duracao_s": j.duracao_s, "dados": j.dados, "atualizado_em": iso(j.atualizado_em)}


def fila_json(f: Fila, slug: str | None = None, amostras: list | None = None) -> dict:
    return {"id": f.id, "servico_id": f.servico_id, "servico": slug, "nome": f.nome, "pendentes": f.pendentes,
            "rodando": f.rodando, "concluidos": f.concluidos, "erros": f.erros, "workers": f.workers,
            "dados": f.dados, "atualizado_em": iso(f.atualizado_em),
            **({"amostras": amostras} if amostras is not None else {})}


def _slugs(db: Session) -> dict[int, str]:
    return dict(db.execute(select(Servico.id, Servico.slug)).all())


def _servico(db: Session, slug: str) -> Servico:
    s = sv.obter(db, slug)
    if not s:
        raise HTTPException(404, f"Serviço '{slug}' não encontrado")
    return s


def _servico_id(db: Session, slug: str | None) -> int | None:
    return _servico(db, slug).id if slug else None


# ── Linha do tempo de heartbeat ──────────────────────────────────────────────

def _timelines(db: Session, servicos: list[Servico], minutos: int = TIMELINE_MIN) -> dict[int, list[int]]:
    """Um valor por minuto (mais antigo → mais recente): 1 = teve heartbeat cobrindo aquele minuto,
    0 = não teve, -1 = antes do serviço existir. Para serviços com intervalo > 1 min, um heartbeat
    cobre os minutos seguintes até o próximo esperado."""
    momento = agora()
    inicio = momento - timedelta(minutes=minutos)
    margem = max((s.intervalo_s or 30) for s in servicos) if servicos else 60
    linhas = db.execute(select(Heartbeat.servico_id, Heartbeat.recebido_em)
                        .where(Heartbeat.recebido_em >= inicio - timedelta(seconds=margem))).all()
    por_servico: dict[int, list] = defaultdict(list)
    for sid, ts in linhas:
        por_servico[sid].append(ts)
    saida = {}
    for s in servicos:
        cobertura = max(60, int(s.intervalo_s or 30) + 15)
        batidas = sorted(por_servico.get(s.id, []))
        barras = []
        for i in range(minutos):
            ini = inicio + timedelta(minutes=i)
            fim = ini + timedelta(minutes=1)
            if s.criado_em and fim <= s.criado_em:
                barras.append(-1)
                continue
            barras.append(1 if any(ini - timedelta(seconds=cobertura - 60) <= t < fim for t in batidas) else 0)
        saida[s.id] = barras
    return saida


# ── Visão geral ──────────────────────────────────────────────────────────────

@router.get("/painel")
def painel(db: Session = Depends(get_db)):
    momento = agora()
    servicos = list(db.scalars(select(Servico).order_by(Servico.grupo, Servico.nome)))
    timelines = _timelines(db, servicos)

    abertos = defaultdict(lambda: {"total": 0, "critico": 0, "erro": 0, "aviso": 0, "info": 0})
    for sid, sev, n in db.execute(select(Problema.servico_id, Problema.severidade, func.count())
                                  .where(Problema.status != "resolvido")
                                  .group_by(Problema.servico_id, Problema.severidade)):
        abertos[sid]["total"] += n
        abertos[sid][sev] = abertos[sid].get(sev, 0) + n

    uma_hora = momento - timedelta(hours=1)
    erros_1h = dict(db.execute(select(Log.servico_id, func.count()).where(Log.recebido_em >= uma_hora,
                                                                          Log.nivel_num >= 40)
                               .group_by(Log.servico_id)).all())
    avisos_1h = dict(db.execute(select(Log.servico_id, func.count()).where(Log.recebido_em >= uma_hora,
                                                                           Log.nivel_num == 30)
                                .group_by(Log.servico_id)).all())
    filas = defaultdict(list)
    for f in db.scalars(select(Fila).order_by(Fila.nome)):
        filas[f.servico_id].append(fila_json(f))
    rodando = dict(db.execute(select(Job.servico_id, func.count()).where(Job.status == "rodando")
                              .group_by(Job.servico_id)).all())

    lista, totais = [], defaultdict(int)
    for s in servicos:
        totais[s.status] += 1
        lista.append({**servico_json(s), "timeline": timelines.get(s.id, []), "problemas": abertos[s.id],
                      "erros_1h": erros_1h.get(s.id, 0), "avisos_1h": avisos_1h.get(s.id, 0),
                      "filas": filas.get(s.id, []), "jobs_rodando": rodando.get(s.id, 0)})
    return {"ok": True, "agora": iso(momento), "totais": {"servicos": len(servicos), **totais},
            "problemas_abertos": sum(a["total"] for a in abertos.values()), "servicos": lista}


# ── Serviços ─────────────────────────────────────────────────────────────────

@router.get("/servicos")
def listar_servicos(db: Session = Depends(get_db)):
    return {"ok": True, "servicos": [servico_json(s) for s in db.scalars(select(Servico).order_by(Servico.nome))]}


@router.get("/servicos/{slug}")
def obter_servico(slug: str, db: Session = Depends(get_db)):
    s = _servico(db, slug)
    problemas = db.scalars(select(Problema).where(Problema.servico_id == s.id, Problema.status != "resolvido")
                           .order_by(Problema.ultima_vez.desc()))
    jobs = db.scalars(select(Job).where(Job.servico_id == s.id).order_by(Job.atualizado_em.desc()).limit(20))
    filas = db.scalars(select(Fila).where(Fila.servico_id == s.id).order_by(Fila.nome))
    hb = db.scalars(select(Heartbeat).where(Heartbeat.servico_id == s.id).order_by(Heartbeat.id.desc()).limit(20))
    return {"ok": True, "servico": {**servico_json(s), "timeline": _timelines(db, [s])[s.id]},
            "problemas": [problema_json(p, s.slug) for p in problemas],
            "jobs": [job_json(j, s.slug) for j in jobs],
            "filas": [fila_json(f, s.slug) for f in filas],
            "heartbeats": [{"recebido_em": iso(h.recebido_em), "status": h.status, "mensagem": h.mensagem}
                           for h in hb]}


class ServicoEdit(BaseModel):
    nome:         str | None = Field(None, min_length=1, max_length=160)
    descricao:    str | None = None
    grupo:        str | None = Field(None, max_length=80)
    repositorio:  str | None = Field(None, max_length=300)          # vazio = tira o repositório
    tolerancia_s: int | None = Field(None, ge=0, le=7 * 86400)      # 0 = automática
    monitorar:    bool | None = None


@router.patch("/servicos/{slug}")
def editar_servico(slug: str, corpo: ServicoEdit, db: Session = Depends(get_db), ator=Depends(auth.exigir("gerenciar"))):
    s = _servico(db, slug)
    dados = corpo.model_dump(exclude_unset=True)
    if "nome" in dados and dados["nome"]:
        s.nome, s.nome_editado = dados["nome"].strip(), True
    if "descricao" in dados:
        s.descricao = (dados["descricao"] or "").strip() or None
    if "grupo" in dados:
        s.grupo = (dados["grupo"] or "").strip() or None
    if "repositorio" in dados:
        try:
            s.repositorio = sv.normalizar_repositorio(dados["repositorio"])
        except ValueError as e:
            raise HTTPException(422, str(e))
    if "tolerancia_s" in dados:
        s.tolerancia_s = dados["tolerancia_s"] or None
    if "monitorar" in dados and dados["monitorar"] is not None:
        s.monitorar = dados["monitorar"]
    sv.aplicar_status(db, s)
    db.commit()
    acesso.registrar_acao(ator, "servico_editado", s.slug, campos=sorted(dados))
    return {"ok": True, "servico": servico_json(s)}


@router.delete("/servicos/{slug}")
def apagar_servico(slug: str, db: Session = Depends(get_db), ator=Depends(auth.exigir("gerenciar"))):
    """Apaga o serviço e todos os dados dele. Se ele continuar mandando dados, é cadastrado de novo."""
    s = _servico(db, slug)
    filas = select(Fila.id).where(Fila.servico_id == s.id)
    db.execute(delete(FilaAmostra).where(FilaAmostra.fila_id.in_(filas)))
    for modelo in (Fila, Job, Problema, Log, Heartbeat):
        db.execute(delete(modelo).where(modelo.servico_id == s.id))
    db.delete(s)
    db.commit()
    acesso.registrar_acao(ator, "servico_apagado", slug)
    return {"ok": True, "apagado": slug}


# ── Logs ─────────────────────────────────────────────────────────────────────

@router.get("/logs")
def logs(servico: str | None = None, nivel: str = "DEBUG", modulo: str | None = None, q: str | None = None,
         antes: int | None = None, depois: int | None = None, limite: int = 200, db: Session = Depends(get_db)):
    """Mais novos primeiro. depois=<id> = só os que chegaram depois (acompanhar ao vivo);
    antes=<id> = página anterior (mais antigos). nivel = mínimo."""
    consulta = select(Log)
    sid = _servico_id(db, servico)
    if sid:
        consulta = consulta.where(Log.servico_id == sid)
    minimo = sv.NIVEIS.get(sv.normalizar_nivel(nivel), 10)
    if minimo > 10:
        consulta = consulta.where(Log.nivel_num >= minimo)
    if modulo:
        consulta = consulta.where(Log.modulo == modulo)
    if q:
        termo = f"%{q.strip()}%"
        consulta = consulta.where(or_(Log.mensagem.like(termo), Log.exc.like(termo)))
    if antes:
        consulta = consulta.where(Log.id < antes)
    if depois:
        consulta = consulta.where(Log.id > depois)
    linhas = db.scalars(consulta.order_by(Log.id.desc()).limit(max(1, min(limite, 2000)))).all()
    slugs = _slugs(db)
    ultimo = db.scalar(select(func.max(Log.id))) or 0
    return {"ok": True, "ultimo_id": ultimo, "logs": [log_json(l, slugs.get(l.servico_id)) for l in linhas]}


@router.get("/logs/modulos")
def modulos(servico: str | None = None, db: Session = Depends(get_db)):
    consulta = select(Log.modulo).where(Log.modulo.is_not(None)).distinct()
    sid = _servico_id(db, servico)
    if sid:
        consulta = consulta.where(Log.servico_id == sid)
    return {"ok": True, "modulos": sorted(m for m in db.scalars(consulta) if m)}


# ── Problemas ────────────────────────────────────────────────────────────────

@router.get("/problemas")
def problemas(servico: str | None = None, status: str = "abertos", limite: int = 300,
              db: Session = Depends(get_db)):
    consulta = select(Problema)
    sid = _servico_id(db, servico)
    if sid:
        consulta = consulta.where(Problema.servico_id == sid)
    if status == "abertos":
        consulta = consulta.where(Problema.status != "resolvido")
    elif status == "resolvidos":
        consulta = consulta.where(Problema.status == "resolvido")
    ordem = Problema.ultima_vez.desc() if status != "resolvidos" else Problema.resolvido_em.desc()
    linhas = db.scalars(consulta.order_by(ordem).limit(max(1, min(limite, 2000))))
    slugs = _slugs(db)
    return {"ok": True, "problemas": [problema_json(p, slugs.get(p.servico_id)) for p in linhas]}


def _problema(db: Session, pid: int) -> Problema:
    p = db.get(Problema, pid)
    if not p:
        raise HTTPException(404, "Problema não encontrado")
    return p


def _quem(ator) -> str:
    return "painel" if ator is auth.SCRIPT else ator.nome[:80]


@router.post("/problemas/{pid}/reconhecer")
def reconhecer(pid: int, db: Session = Depends(get_db), ator=Depends(auth.exigir("tratar"))):
    p = _problema(db, pid)
    if p.status == "aberto":
        p.status = "reconhecido"
        db.commit()
        acesso.registrar_acao(ator, "problema_reconhecido", p.titulo[:200], problema=p.id, severidade=p.severidade)
    return {"ok": True, "problema": problema_json(p)}


@router.post("/problemas/{pid}/resolver")
def resolver(pid: int, db: Session = Depends(get_db), ator=Depends(auth.exigir("tratar"))):
    p = _problema(db, pid)
    if p.status != "resolvido":
        p.status, p.resolvido_em, p.resolvido_por = "resolvido", agora(), _quem(ator)
        db.commit()
        acesso.registrar_acao(ator, "problema_resolvido", p.titulo[:200], problema=p.id, severidade=p.severidade)
    return {"ok": True, "problema": problema_json(p)}


# ── Jobs ─────────────────────────────────────────────────────────────────────

@router.get("/jobs")
def listar_jobs(servico: str | None = None, status: str | None = None, tipo: str | None = None,
                q: str | None = None, limite: int = 200, db: Session = Depends(get_db)):
    consulta = select(Job)
    sid = _servico_id(db, servico)
    if sid:
        consulta = consulta.where(Job.servico_id == sid)
    if status:
        consulta = consulta.where(Job.status.in_([sv.normalizar_status_job(x) for x in status.split(",")]))
    if tipo:
        consulta = consulta.where(Job.tipo == tipo)
    if q:
        termo = f"%{q.strip()}%"
        consulta = consulta.where(or_(Job.nome.like(termo), Job.externo_id.like(termo), Job.mensagem.like(termo)))
    linhas = db.scalars(consulta.order_by(Job.atualizado_em.desc()).limit(max(1, min(limite, 2000))))
    slugs = _slugs(db)
    tipos = sorted(t for t in db.scalars(select(Job.tipo).distinct()) if t)
    return {"ok": True, "tipos": tipos, "jobs": [job_json(j, slugs.get(j.servico_id)) for j in linhas]}


# ── Filas ────────────────────────────────────────────────────────────────────

@router.get("/filas")
def listar_filas(servico: str | None = None, minutos: int = 60, db: Session = Depends(get_db)):
    consulta = select(Fila)
    sid = _servico_id(db, servico)
    if sid:
        consulta = consulta.where(Fila.servico_id == sid)
    filas = list(db.scalars(consulta.order_by(Fila.servico_id, Fila.nome)))
    inicio = agora() - timedelta(minutes=max(5, min(minutos, 24 * 60)))
    amostras = defaultdict(list)
    if filas:
        for a in db.scalars(select(FilaAmostra).where(FilaAmostra.fila_id.in_([f.id for f in filas]),
                                                      FilaAmostra.ts >= inicio).order_by(FilaAmostra.ts)):
            amostras[a.fila_id].append({"ts": iso(a.ts), "pendentes": a.pendentes, "rodando": a.rodando,
                                        "erros": a.erros})
    slugs = _slugs(db)
    return {"ok": True, "filas": [fila_json(f, slugs.get(f.servico_id), amostras.get(f.id, [])) for f in filas]}


# ── Heartbeats ───────────────────────────────────────────────────────────────

@router.get("/heartbeats")
def heartbeats(servico: str, limite: int = 100, db: Session = Depends(get_db)):
    s = _servico(db, servico)
    linhas = db.scalars(select(Heartbeat).where(Heartbeat.servico_id == s.id).order_by(Heartbeat.id.desc())
                        .limit(max(1, min(limite, 2000))))
    return {"ok": True, "heartbeats": [{"recebido_em": iso(h.recebido_em), "status": h.status,
                                        "mensagem": h.mensagem, "dados": h.dados} for h in linhas]}
