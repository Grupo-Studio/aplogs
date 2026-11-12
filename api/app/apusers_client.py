# Copiado de Grupo-Studio/apusers: clientes/python/apusers_client.py.
# Para atualizar, copie o arquivo de novo de lá; não edite aqui.
"""
apusers_client.py — cliente do apusers para serviços e robôs em Python 3.9+ (só biblioteca padrão).

Copie este arquivo para o seu projeto:

    from apusers_client import ApUsers, ApUsersErro

    apusers = ApUsers.do_ambiente()      # lê APUSERS_URL + APUSERS_KEY (ou APUSERS_KEY_FILE)

    # login central (o usuário digita e-mail/senha no SEU sistema); repasse o IP da pessoa
    try:
        sessao = apusers.login(email, senha, ip=request.client.host)
    except ApUsersErro as e:
        if e.code == "acesso_restrito":
            print(e.message)          # "Acesso restrito ... O pedido foi enviado ao administrador."

    # conferir o token que o seu app recebeu (consulta o apusers; resultado em cache por 30 s)
    info = apusers.verificar(token)   # {"active": True, "user": {...}, "access": {"role": ...}}
    usuario = apusers.usuario_do_header(request.headers.get("Authorization"))   # pronto para qualquer framework

    # gestão de usuários: o que não é do serviço vira PENDÊNCIA para o admin ({"pending": True, "request": ...})
    r = apusers.criar_usuario(email="nova@empresa.com.br", name="Nova", role="operador")   # conta pendente

    # atividade (fila em segundo plano; nunca derruba o robô)
    fim = apusers.tela(user_id, "Lotes") ... fim()
    apusers.acao(user_id, "exportar_excel", "Lote 42")
    apusers.presenca(user_id, screen="Lotes", activity="Conferindo o lote 43")

Validação: este cliente usa POST /v1/auth/verify (introspecção) para não depender de biblioteca de
criptografia. Para validar localmente pelo JWKS, use PyJWT (veja docs/INTEGRACAO.md).
"""

from __future__ import annotations

import atexit
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


class ApUsersErro(Exception):
    def __init__(self, status: int, corpo: dict | None):
        corpo = corpo or {}
        self.status = status
        self.code = corpo.get("error", "erro")
        self.message = corpo.get("message") or f"apusers respondeu {status}"
        self.corpo = corpo
        super().__init__(self.message)


def _agora_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ApUsers:
    def __init__(self, api_url: str, key: str, service: str | None = None, timeout: float = 8.0,
                 intervalo_envio: float = 5.0, cache_verificacao_s: float = 30.0):
        if not (api_url and key):
            raise ValueError("apusers: informe api_url e key (APUSERS_URL e APUSERS_KEY)")
        self.api_url = api_url.rstrip("/")
        self.key = key
        self.service = service
        self.timeout = timeout
        self.cache_verificacao_s = cache_verificacao_s
        self._cache: dict[str, tuple[float, dict]] = {}
        self._fila: list[dict] = []
        self._trava = threading.Lock()
        self._parar = threading.Event()
        self._thread = threading.Thread(target=self._laco, args=(intervalo_envio,), daemon=True, name="apusers-envio")
        self._thread.start()
        atexit.register(self.fechar)

    @classmethod
    def de_arquivo(cls, caminho: str, **extra) -> "ApUsers":
        with open(caminho, encoding="utf-8") as f:
            j = json.load(f)
        if j.get("type") != "apusers_service_key":
            raise ValueError(f"{caminho} não é uma chave do apusers")
        return cls(extra.pop("api_url", j["api_url"]), j["key"], j["service"], **extra)

    @classmethod
    def do_ambiente(cls, **extra) -> "ApUsers":
        """O jeito mais simples: as variáveis que o painel mostra ao gerar a chave.

        APUSERS_URL=http://192.168.1.100:3080   (mesma máquina, rede Docker: http://apusers-api:8000)
        APUSERS_KEY=aps_...
        Ou APUSERS_KEY_FILE=/caminho/apusers-rppp.json (APUSERS_URL, se vier, sobrescreve o endereço do arquivo).
        """
        arquivo = os.environ.get("APUSERS_KEY_FILE") or os.environ.get("APUSERS_CHAVE_ARQUIVO")
        if arquivo:
            if os.environ.get("APUSERS_URL"):
                extra.setdefault("api_url", os.environ["APUSERS_URL"])
            return cls.de_arquivo(arquivo, **extra)
        url, chave = os.environ.get("APUSERS_URL"), os.environ.get("APUSERS_KEY")
        if not (url and chave):
            raise ValueError("apusers: defina APUSERS_URL e APUSERS_KEY (ou APUSERS_KEY_FILE) no ambiente do serviço")
        return cls(url, chave, os.environ.get("APUSERS_SERVICE"), **extra)

    # ── HTTP ──────────────────────────────────────────────────────────────
    def _req(self, metodo: str, caminho: str, corpo=None, token: str | None = None, com_chave: bool = True,
             ip: str | None = None) -> dict:
        headers = {"Accept": "application/json"}
        if ip:
            headers["x-apusers-client-ip"] = str(ip).removeprefix("::ffff:")
        if corpo is not None:
            headers["Content-Type"] = "application/json"
        if com_chave:
            headers["x-service-key"] = self.key
        if token:
            headers["Authorization"] = f"Bearer {token}"
        dados = json.dumps(corpo).encode() if corpo is not None else None
        req = urllib.request.Request(self.api_url + caminho, data=dados, method=metodo, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                texto = r.read().decode() or "{}"
                return json.loads(texto)
        except urllib.error.HTTPError as e:
            try:
                corpo_erro = json.loads(e.read().decode() or "{}")
            except ValueError:
                corpo_erro = {}
            raise ApUsersErro(e.code, corpo_erro) from None

    # ── Autenticação ──────────────────────────────────────────────────────
    def login(self, email: str, password: str, solicitar_acesso: bool = True, ip: str | None = None) -> dict:
        """Login NESTE serviço. Sem acesso: ApUsersErro(403, code="acesso_restrito").

        `ip`: o IP da pessoa (FastAPI: request.client.host). Sem ele, o apusers só limita tentativas por e-mail.
        """
        return self._req("POST", "/v1/auth/login", {"email": email, "password": password, "solicitar_acesso": solicitar_acesso},
                         ip=ip)

    def cadastrar(self, email: str, name: str, password: str, ip: str | None = None, **dados) -> dict:
        """Autocadastro pelo SEU app: a conta fica pendente até o admin aprovar (e já vem com acesso a este serviço).

        Sempre {"ok": True, "pending": True, "message": "..."}. Extras: area, job_title, phone, justification.
        """
        return self._req("POST", "/v1/auth/register", {"email": email, "name": name, "password": password, **dados}, ip=ip)

    def refresh(self, refresh_token: str) -> dict:
        return self._req("POST", "/v1/auth/refresh", {"refresh_token": refresh_token}, com_chave=False)

    def logout(self, access_token: str) -> None:
        self._req("POST", "/v1/auth/logout", {}, token=access_token, com_chave=False)

    def verificar(self, token: str, usar_cache: bool = True) -> dict:
        """{"active": bool, "user": {...}, "access": {...}} — com cache curto para não consultar a cada chamada."""
        agora = time.monotonic()
        if usar_cache:
            em_cache = self._cache.get(token)
            if em_cache and agora - em_cache[0] < self.cache_verificacao_s:
                return em_cache[1]
        r = self._req("POST", "/v1/auth/verify", {"token": token})
        if len(self._cache) > 5000:
            self._cache.clear()
        self._cache[token] = (agora, r)
        return r

    def usuario_do_header(self, authorization: str | None) -> dict:
        """Recebe o header Authorization ("Bearer ...") e devolve {id, email, name, role, session_id}.

        Lança ApUsersErro(401) se o token não vale (ou o acesso foi tirado). Use em qualquer framework:
            FastAPI:  def usuario(authorization: str = Header("")): return apusers.usuario_do_header(authorization)
            Flask:    usuario = apusers.usuario_do_header(request.headers.get("Authorization"))
        """
        texto = (authorization or "").strip()
        token = texto[7:].strip() if texto.lower().startswith("bearer ") else ""
        if not token:
            raise ApUsersErro(401, {"error": "nao_autenticado", "message": "Faça login."})
        v = self.verificar(token)
        if not v.get("active"):
            raise ApUsersErro(401, {"error": v.get("reason") or "sessao_invalida", "message": "Sessão inválida."})
        u, a = v["user"], v.get("access") or {}
        return {"id": u["id"], "email": u["email"], "name": u["name"], "role": a.get("role"), "session_id": v.get("session_id")}

    # ── Serviço e usuários ────────────────────────────────────────────────
    def meu_servico(self) -> dict:
        return self._req("GET", "/v1/service/me")

    def atualizar_servico(self, **dados) -> dict:
        """name, description, url, config (dict), roles ([{"role": "...", "description": "..."}])."""
        return self._req("PATCH", "/v1/service/me", dados)

    def usuarios(self, q: str | None = None) -> dict:
        return self._req("GET", "/v1/service/users" + (f"?q={urllib.parse.quote(q)}" if q else ""))

    def usuario(self, id_ou_email: str) -> dict:
        return self._req("GET", f"/v1/service/users/{urllib.parse.quote(id_ou_email)}")

    def criar_usuario(self, **dados) -> dict:
        """Cria a conta PENDENTE (entra quando o admin aprovar): {"pending": True, "user": ..., "request": ...}."""
        return self._req("POST", "/v1/service/users", dados)

    def editar_usuario(self, id_ou_email: str, **dados) -> dict:
        """Nome/área/cargo/telefone só de quem este serviço criou; `role` pode virar pendência."""
        return self._req("PATCH", f"/v1/service/users/{urllib.parse.quote(id_ou_email)}", dados)

    def dar_acesso(self, id_ou_email: str, role: str | None = None, justificativa: str | None = None) -> dict:
        """Direto se o acesso é deste serviço ({"pending": False}); senão vira pendência ({"pending": True, ...})."""
        return self._req("PUT", f"/v1/service/users/{urllib.parse.quote(id_ou_email)}/access",
                         {"role": role, "justification": justificativa})

    def tirar_acesso(self, id_ou_email: str) -> dict:
        """Direto se o acesso é deste serviço ({"removed": True}); senão vira pendência de remoção."""
        return self._req("DELETE", f"/v1/service/users/{urllib.parse.quote(id_ou_email)}")

    def pedir_acesso(self, id_ou_email: str, justificativa: str | None = None, role: str | None = None) -> dict:
        return self._req("POST", "/v1/service/access-requests", {"user": id_ou_email, "justification": justificativa, "role": role})

    # ── Atividade (fila + envio em lote numa thread) ─────────────────────
    def _enfileirar(self, ev: dict) -> None:
        with self._trava:
            self._fila.append(ev)
            if len(self._fila) > 10_000:
                del self._fila[: len(self._fila) - 10_000]

    def enfileirar(self, evento: dict) -> None:
        """Põe na fila um evento já no formato do apusers e manda em segundo plano.

        Campos: user_id ou email, type ("tela" | "acao" | "evento"), name, detail, duration_ms, started_at,
        ended_at, metadata. Serve para REPASSAR o que um app seu (desktop, web) mediu, sem traduzir.
        """
        self._enfileirar(dict(evento))

    def tela(self, user_id: str, nome: str, metadata: dict | None = None):
        """Marca a entrada numa tela; chame o retorno ao sair."""
        inicio = datetime.now(timezone.utc)
        fechado = False

        def fim():
            nonlocal fechado
            if fechado:
                return
            fechado = True
            agora = datetime.now(timezone.utc)
            self._enfileirar({"user_id": user_id, "type": "tela", "name": nome, "started_at": inicio.isoformat(),
                              "ended_at": agora.isoformat(), "duration_ms": int((agora - inicio).total_seconds() * 1000),
                              "metadata": metadata})
        return fim

    def acao(self, user_id: str, nome: str, detalhe: str | None = None, metadata: dict | None = None) -> None:
        self._enfileirar({"user_id": user_id, "type": "acao", "name": nome, "detail": detalhe,
                          "started_at": _agora_iso(), "metadata": metadata})

    def evento(self, user_id: str, nome: str, **dados) -> None:
        self._enfileirar({"user_id": user_id, "type": "evento", "name": nome, "started_at": _agora_iso(), **dados})

    def presenca(self, user_id: str, screen: str | None = None, activity: str | None = None, status: str = "ativo") -> None:
        try:
            self._req("POST", "/v1/service/presence", {"user_id": user_id, "screen": screen, "activity": activity, "status": status})
        except Exception:  # noqa: BLE001 — o apusers fora nunca derruba o robô
            pass

    def descarregar(self) -> None:
        with self._trava:
            lote, self._fila = self._fila[:1000], self._fila[1000:]
        if not lote:
            return
        try:
            self._req("POST", "/v1/service/events", {"events": lote})
        except ApUsersErro as e:
            if e.status >= 500:
                with self._trava:
                    self._fila[:0] = lote
        except Exception:  # noqa: BLE001 — rede fora: tenta de novo depois
            with self._trava:
                self._fila[:0] = lote

    def _laco(self, intervalo: float) -> None:
        while not self._parar.wait(intervalo):
            self.descarregar()

    def fechar(self) -> None:
        self._parar.set()
        self.descarregar()
