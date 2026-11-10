"""
monitor.py
==========
Tarefas de fundo da aplogs:

  - Monitor de heartbeat (a cada MONITOR_INTERVALO_S): o serviço que passou da tolerância sem
    mandar heartbeat fica OFFLINE, ganha um log e um problema crítico "está offline".
    Quando volta a mandar heartbeat, o problema é resolvido sozinho.
  - Limpeza (a cada hora): apaga o que passou da retenção configurada.
"""

import asyncio
import logging
import time
from datetime import timedelta

from sqlalchemy import delete, select

from . import config
from . import servicos as sv
from .db import agora, sessao
from .modelos import FilaAmostra, Heartbeat, Job, Log, Problema, Servico

logger = logging.getLogger("aplogs.monitor")

_tarefas: list[asyncio.Task] = []


def rodada_status() -> int:
    """Confere todos os serviços uma vez. Devolve quantos mudaram de status."""
    mudaram = 0
    with sessao() as db:
        momento = agora()
        for s in db.scalars(select(Servico)):
            antigo = s.status
            if sv.aplicar_status(db, s, momento):
                mudaram += 1
                logger.info(f"[monitor] {s.slug}: {antigo} → {s.status}")
        db.commit()
    return mudaram


def _apagar_em_lotes(db, modelo, condicao, lote: int = 5000) -> int:
    total = 0
    while True:
        ids = list(db.scalars(select(modelo.id).where(condicao).limit(lote)))
        if not ids:
            return total
        db.execute(delete(modelo).where(modelo.id.in_(ids)))
        db.commit()
        total += len(ids)


def limpeza() -> dict:
    momento = agora()
    feito = {}
    with sessao() as db:
        if config.RETENCAO_LOGS_DIAS > 0:
            feito["logs"] = _apagar_em_lotes(db, Log, Log.recebido_em < momento - timedelta(days=config.RETENCAO_LOGS_DIAS))
        if config.RETENCAO_HEARTBEATS_DIAS > 0:
            limite = momento - timedelta(days=config.RETENCAO_HEARTBEATS_DIAS)
            feito["heartbeats"] = _apagar_em_lotes(db, Heartbeat, Heartbeat.recebido_em < limite)
            feito["fila_amostras"] = _apagar_em_lotes(db, FilaAmostra, FilaAmostra.ts < limite)
        if config.RETENCAO_JOBS_DIAS > 0:
            limite = momento - timedelta(days=config.RETENCAO_JOBS_DIAS)
            feito["jobs"] = _apagar_em_lotes(db, Job, (Job.atualizado_em < limite)
                                             & Job.status.in_(sv.STATUS_JOB_FINAIS))
        if config.RETENCAO_PROBLEMAS_DIAS > 0:
            limite = momento - timedelta(days=config.RETENCAO_PROBLEMAS_DIAS)
            feito["problemas"] = _apagar_em_lotes(db, Problema, (Problema.status == "resolvido")
                                                  & (Problema.resolvido_em < limite))
    if any(feito.values()):
        logger.info(f"[limpeza] apagados: {feito}")
    return feito


async def _loop_status():
    while True:
        try:
            await asyncio.to_thread(rodada_status)
        except Exception:
            logger.exception("[monitor] falha na rodada")
        await asyncio.sleep(config.MONITOR_INTERVALO_S)


async def _loop_limpeza():
    ultima = 0.0
    while True:
        if time.monotonic() - ultima >= 3600:
            try:
                await asyncio.to_thread(limpeza)
            except Exception:
                logger.exception("[limpeza] falha")
            ultima = time.monotonic()
        await asyncio.sleep(60)


def iniciar():
    _tarefas.append(asyncio.create_task(_loop_status()))
    _tarefas.append(asyncio.create_task(_loop_limpeza()))
    logger.info(f"[monitor] conferindo heartbeats a cada {config.MONITOR_INTERVALO_S}s")


def encerrar():
    for t in _tarefas:
        t.cancel()
    _tarefas.clear()
