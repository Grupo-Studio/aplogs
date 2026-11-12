"""
auth.py
=======
Três acessos separados:

  Painel (pessoas)    login central (apusers): e-mail e senha do apusers → cookie com o id de uma sessão
                      guardada no banco (tabela web_sessoes). Os tokens do apusers nunca vão ao navegador.
                      O que cada pessoa pode fazer depende do papel dela no apusers (acesso.PERMISSOES).
  Scripts (máquinas)  header x-api-secret = APLOGS_SECRET. Pode tudo no painel.
  Ingestão (serviços) header x-api-key = APLOGS_INGEST_KEY. Só envia; não lê nada.

Pela web, quem muda algo manda o header X-Web: 1 (proteção contra CSRF; o painel já manda).
Trocar o APLOGS_SECRET derruba todos os logins do painel (a assinatura do cookie muda).
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time

from fastapi import Depends, Header, HTTPException, Request, Response
from sqlalchemy import delete, select

from . import acesso, config
from .apusers_client import ApUsersErro
from .db import sessao
from .modelos import WebSessao

logger = logging.getLogger("aplogs.auth")

COOKIE = "aplogs_sessao"
_trava_renovacao = threading.Lock()      # no sqlite (testes) não há trava de linha; aqui serializa no processo


# ── Cookie (só um id assinado) ───────────────────────────────────────────────

def _chave() -> bytes:
    return hashlib.sha256(("aplogs-sessao:" + config.APLOGS_SECRET).encode()).digest()


def _assinar(dados: dict) -> str:
    corpo = base64.urlsafe_b64encode(json.dumps(dados).encode()).decode().rstrip("=")
    return f"{corpo}.{hmac.new(_chave(), corpo.encode(), hashlib.sha256).hexdigest()}"


def sid_do_cookie(request: Request) -> str | None:
    token = request.cookies.get(COOKIE) or ""
    corpo, _, assinatura = token.partition(".")
    if not corpo or not assinatura:
        return None
    if not hmac.compare_digest(assinatura, hmac.new(_chave(), corpo.encode(), hashlib.sha256).hexdigest()):
        return None
    try:
        dados = json.loads(base64.urlsafe_b64decode(corpo + "=" * (-len(corpo) % 4)))
    except Exception:
        return None
    return dados.get("sid") if dados.get("exp", 0) >= time.time() else None


def ip(request: Request) -> str | None:
    return request.client.host if request.client else None


# ── Sessões no banco ─────────────────────────────────────────────────────────

def _ler(sid: str) -> WebSessao | None:
    with sessao() as db:
        s = db.get(WebSessao, sid)
        if s and s.expira < time.time():
            db.delete(s)
            db.commit()
            return None
        return s


def _apagar(sid: str):
    with sessao() as db:
        db.execute(delete(WebSessao).where(WebSessao.id == sid))
        db.commit()


def abrir_sessao(response: Response, tokens: dict, email: str) -> int:
    sid = secrets.token_urlsafe(32)
    validade = int(config.SESSAO_HORAS * 3600)
    agora = int(time.time())
    with sessao() as db:
        db.execute(delete(WebSessao).where(WebSessao.expira < agora))
        db.add(WebSessao(id=sid, access_token=tokens["access_token"], refresh_token=tokens["refresh_token"],
                         email=email, expira=agora + validade, criada=agora))
        db.commit()
    response.set_cookie(COOKIE, _assinar({"sid": sid, "exp": agora + validade}), max_age=validade,
                        httponly=True, samesite="strict", path="/")
    return validade


def _renovar(sid: str, vencido: str) -> str | None:
    """Renova o token (de uso único) com a sessão travada. Se outra requisição já renovou, só devolve o
    token novo. Refresh recusado → apaga a sessão e devolve None. apusers fora → sobe o erro."""
    with _trava_renovacao, sessao() as db:
        s = db.execute(select(WebSessao).where(WebSessao.id == sid).with_for_update()).scalar_one_or_none()
        if not s:
            return None
        if s.access_token != vencido:
            return s.access_token
        try:
            novos = acesso.cliente().refresh(s.refresh_token)
        except ApUsersErro:
            db.delete(s)
            db.commit()
            return None
        s.access_token, s.refresh_token, s.renovada = novos["access_token"], novos["refresh_token"], int(time.time())
        db.commit()
        return novos["access_token"]


def _codigo(e: HTTPException) -> str | None:
    return e.detail.get("codigo") if isinstance(e.detail, dict) else None


def usuario(request: Request) -> acesso.Usuario | None:
    """Quem está logado no painel (None = ninguém). Renova o token do apusers quando ele vence.
    Acesso tirado / pessoa desativada: descarta a sessão e o 403 sobe com o motivo.
    apusers fora do ar: o erro (503) sobe e a sessão é mantida."""
    sid = sid_do_cookie(request)
    s = _ler(sid) if sid else None
    if not s:
        return None
    try:
        return acesso.usuario_do_token(s.access_token)
    except HTTPException as e:
        if e.status_code == 503:
            raise
        if _codigo(e) != "token_expirado":
            _apagar(sid)
            if e.status_code == 403:
                raise
            return None
    try:
        novo = _renovar(sid, s.access_token)
    except Exception as e:                       # rede até o apusers
        raise acesso.erro(503, "Login central (apusers) fora do ar. Tente de novo em instantes.",
                          "login_central_fora") from e
    if not novo:
        return None
    try:
        return acesso.usuario_do_token(novo)
    except HTTPException as e:
        if e.status_code == 503:
            raise
        _apagar(sid)
        return None


def encerrar_sessao(request: Request, response: Response):
    sid = sid_do_cookie(request)
    s = _ler(sid) if sid else None
    if s:
        u = None
        try:
            u = acesso.usuario_do_token(s.access_token)
        except HTTPException:
            pass
        acesso.sair(s.access_token, u)
        _apagar(sid)
    response.delete_cookie(COOKIE, path="/")


# ── Dependências do FastAPI ──────────────────────────────────────────────────

class Script:
    """Quem chama com o x-api-secret (máquinas, não pessoas)."""
    origem = "script"
    papel = None

    def pode(self, _acao: str) -> bool:
        return True


SCRIPT = Script()


def quem_chama(request: Request, x_api_secret: str | None = Header(default=None)):
    if x_api_secret is not None:
        if not hmac.compare_digest(x_api_secret.encode(), config.APLOGS_SECRET.encode()):
            raise acesso.erro(401, "Secret inválido", "secret_invalido")
        ator = SCRIPT
    else:
        ator = usuario(request)
        if not ator:
            raise acesso.erro(401, "Entre com o seu usuário (ou a sessão expirou: entre de novo).",
                              "nao_autenticado")
        if request.method not in ("GET", "HEAD") and request.headers.get("x-web") != "1":
            raise acesso.erro(403, "Pedido do painel sem o header X-Web", "csrf")
    request.state.ator = ator
    return ator


def exigir(acao: str):
    """Dependência da rota: exige a permissão `acao` (acesso.PERMISSOES). Monte no roteador ou na rota."""
    if acao not in acesso.PERMISSOES:
        raise ValueError(f"ação desconhecida: {acao}")

    def dependencia(ator=Depends(quem_chama)):
        if ator.pode(acao):
            return ator
        raise acesso.erro(403, f"Seu papel ({ator.papel}) não permite {acesso.DESCRICAO[acao]}. "
                               f"Peça ao administrador do apusers.", "sem_permissao", papel=ator.papel)

    return dependencia


# Rotas de leitura do painel (cookie de sessão ou header x-api-secret)
painel = exigir("ver")


def ingestao(request: Request, x_api_key: str | None = Header(default=None),
             authorization: str | None = Header(default=None)):
    """Rotas que os serviços usam para enviar dados. A chave vale em qualquer um destes lugares
    (o que for mais fácil para a ferramenta que chama):
      header x-api-key: <chave>  ·  header Authorization: Bearer <chave>  ·  ?key=<chave> na URL"""
    chave = x_api_key
    if not chave and authorization and authorization.lower().startswith("bearer "):
        chave = authorization[7:].strip()
    if not chave:
        chave = request.query_params.get("key")
    if not chave:
        raise HTTPException(401, "Chave ausente: mande o header 'x-api-key' (ou Authorization: Bearer, ou ?key=)")
    if not hmac.compare_digest(chave.encode(), config.APLOGS_INGEST_KEY.encode()):
        raise HTTPException(401, "Chave de envio inválida")
