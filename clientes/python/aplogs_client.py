"""
aplogs_client.py
================
Cliente da aplogs para qualquer robô/serviço em Python. Só biblioteca padrão (copie o arquivo).

    from aplogs_client import Aplogs

    aplogs = Aplogs("http://192.168.1.100:3070", chave="<APLOGS_INGEST_KEY>",
                    servico="robo-dctf", nome="Robô DCTF", intervalo_s=30, versao="1.0")
    aplogs.iniciar()                        # heartbeat em segundo plano + envio dos logs
    aplogs.capturar_logging("INFO")         # manda os logs do módulo logging (INFO+) para a aplogs

    aplogs.job("lote-42", nome="Lote 42", status="rodando", progresso=30, etapa="baixando")
    aplogs.fila("pendentes", pendentes=12, rodando=1)
    aplogs.problema("certificado:3", "Certificado vence em 5 dias", severidade="aviso")
    aplogs.resolver("certificado:3")
    aplogs.status("degradado", "2 de 9 sessões caíram")   # vai no próximo heartbeat

    aplogs.parar()                          # avisa "parando" (desligamento normal, não é queda)

Nada aqui derruba o robô: se a aplogs estiver fora, os envios falham em silêncio
(os logs ficam num buffer limitado e vão quando ela voltar).
"""

import atexit
import json
import os
import logging
import queue
import socket
import threading
import time
import traceback
import urllib.request
from datetime import datetime, timezone

_log = logging.getLogger("aplogs_client")


def _agora() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class Aplogs:
    def __init__(self, url: str, chave: str, servico: str, nome: str | None = None, intervalo_s: int = 30,
                 versao: str | None = None, grupo: str | None = None, descricao: str | None = None,
                 repositorio: str | None = None, branch: str | None = None, commit: str | None = None,
                 timeout_s: float = 5, max_buffer: int = 5000):
        """repositorio: "dono/repo" ou a URL do GitHub. branch/commit: o que está rodando
        (sem informar, usa as variáveis de ambiente GIT_BRANCH / GIT_COMMIT, se existirem)."""
        self.url = url.rstrip("/")
        self.chave = chave
        self.servico = servico
        self.nome = nome
        self.intervalo_s = intervalo_s
        self.info = {"versao": versao, "grupo": grupo, "descricao": descricao, "host": socket.gethostname(),
                     "repositorio": repositorio, "branch": branch or os.getenv("GIT_BRANCH"),
                     "commit": commit or os.getenv("GIT_COMMIT")}
        self.timeout_s = timeout_s
        self.iniciado_em = _agora()
        self._status, self._mensagem, self._dados = "ok", None, None
        self._logs: queue.Queue = queue.Queue(maxsize=max_buffer)
        self._parar = threading.Event()
        self._thread: threading.Thread | None = None
        self.dados_fn = None          # opcional: função que devolve um dict para cada heartbeat

    # ── envio ────────────────────────────────────────────────────────────────

    def _post(self, caminho: str, corpo: dict) -> dict | None:
        dados = json.dumps({"servico": self.servico, "nome": self.nome, **corpo}, default=str).encode()
        req = urllib.request.Request(f"{self.url}/api/ingest/{caminho}", data=dados, method="POST",
                                     headers={"Content-Type": "application/json", "x-api-key": self.chave})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                return json.loads(r.read() or b"{}")
        except Exception as e:
            _log.debug(f"aplogs indisponível ({caminho}): {e}")
            return None

    def heartbeat(self) -> dict | None:
        dados = self._dados
        if self.dados_fn:
            try:
                dados = {**(dados or {}), **(self.dados_fn() or {})}
            except Exception:
                pass
        return self._post("heartbeat", {**{k: v for k, v in self.info.items() if v}, "intervalo_s": self.intervalo_s,
                                        "status": self._status, "mensagem": self._mensagem,
                                        "iniciado_em": self.iniciado_em, "dados": dados})

    def status(self, status: str = "ok", mensagem: str | None = None, dados: dict | None = None,
               enviar_agora: bool = False):
        """ok | degradado | erro. Vai no próximo heartbeat (ou agora)."""
        self._status, self._mensagem = status, mensagem
        if dados is not None:
            self._dados = dados
        if enviar_agora:
            self.heartbeat()

    def log(self, mensagem: str, nivel: str = "INFO", modulo: str | None = None, exc: str | None = None,
            contexto: dict | None = None):
        item = {"ts": _agora(), "nivel": nivel, "mensagem": mensagem, "modulo": modulo, "exc": exc,
                "contexto": contexto}
        try:
            self._logs.put_nowait(item)
        except queue.Full:                      # buffer cheio (aplogs fora há muito tempo): descarta o mais velho
            try:
                self._logs.get_nowait()
                self._logs.put_nowait(item)
            except queue.Empty:
                pass

    def enviar_logs(self):
        lote = []
        while len(lote) < 1000:
            try:
                lote.append(self._logs.get_nowait())
            except queue.Empty:
                break
        if lote and self._post("logs", {"logs": lote}) is None:
            for item in lote:                   # devolve para tentar de novo depois
                try:
                    self._logs.put_nowait(item)
                except queue.Full:
                    break

    def problema(self, chave: str, titulo: str, descricao: str | None = None, severidade: str = "erro",
                 dados: dict | None = None):
        return self._post("problemas", {"problemas": [{"chave": chave, "titulo": titulo, "descricao": descricao,
                                                       "severidade": severidade, "dados": dados}]})

    def resolver(self, chave: str):
        return self._post("problemas", {"problemas": [{"chave": chave, "resolvido": True}]})

    def problemas_estado(self, prefixos: list[str], problemas: list[dict]):
        """Manda a lista COMPLETA dos problemas atuais com esses prefixos; os que sumiram são resolvidos."""
        return self._post("problemas", {"sincronizar": prefixos, "problemas": problemas})

    def job(self, id: str, **campos):
        """campos: tipo, nome, status (fila|rodando|ok|erro|interrompido), progresso, etapa, mensagem,
        iniciado_em, terminado_em, dados"""
        return self._post("jobs", {"jobs": [{"id": str(id), **campos}]})

    def fila(self, nome: str, pendentes: int = 0, rodando: int = 0, concluidos: int = 0, erros: int = 0,
             workers: int | None = None, dados: dict | None = None):
        return self._post("filas", {"filas": [{"nome": nome, "pendentes": pendentes, "rodando": rodando,
                                               "concluidos": concluidos, "erros": erros, "workers": workers,
                                               "dados": dados}]})

    # ── segundo plano ────────────────────────────────────────────────────────

    def _loop(self):
        proximo_hb = 0.0
        while not self._parar.is_set():
            if time.monotonic() >= proximo_hb:
                self.heartbeat()
                proximo_hb = time.monotonic() + self.intervalo_s
            self.enviar_logs()
            self._parar.wait(2)

    def iniciar(self):
        if self._thread and self._thread.is_alive():
            return self
        self._parar.clear()
        self._thread = threading.Thread(target=self._loop, name="aplogs", daemon=True)
        self._thread.start()
        atexit.register(self.parar)
        return self

    def parar(self):
        """Desligamento normal: manda os logs que faltam e avisa 'parando' (fica offline sem ser 'queda')."""
        if self._parar.is_set():
            return
        self._parar.set()
        self.enviar_logs()
        self._status, self._mensagem = "parando", "desligamento normal"
        self.heartbeat()

    def capturar_logging(self, nivel: str = "INFO", logger: logging.Logger | None = None):
        handler = AplogsHandler(self)
        handler.setLevel(nivel)
        (logger or logging.getLogger()).addHandler(handler)
        return handler


class AplogsHandler(logging.Handler):
    """Manda os registros do módulo logging para a aplogs (em lote, pela thread do cliente)."""

    def __init__(self, cliente: Aplogs):
        super().__init__()
        self.cliente = cliente

    def emit(self, record: logging.LogRecord):
        if record.name.startswith(("aplogs_client", "urllib3")):
            return
        try:
            exc = "".join(traceback.format_exception(*record.exc_info)) if record.exc_info else None
            self.cliente.log(record.getMessage(), nivel=record.levelname, modulo=record.name, exc=exc)
        except Exception:
            pass
