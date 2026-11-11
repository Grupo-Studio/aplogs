"""
auth.py
=======
Dois acessos separados:

  Painel (pessoas)   senha APLOGS_SECRET → cookie de sessão assinado (HMAC), válido por SESSAO_HORAS.
                     Scripts podem mandar o header x-api-secret no lugar do cookie.
  Ingestão (serviços) header x-api-key = APLOGS_INGEST_KEY. Só envia; não lê nada.

Trocar o APLOGS_SECRET derruba todos os logins do painel (a assinatura muda).
"""

import base64
import hashlib
import hmac
import json
import time
from collections import defaultdict, deque

from fastapi import Header, HTTPException, Request

from . import config

COOKIE = "aplogs_sessao"

# Proteção contra chute de senha: no máximo MAX_FALHAS erros por IP a cada JANELA_S
MAX_FALHAS = 10
JANELA_S = 600
_falhas: dict[str, deque] = defaultdict(deque)


def _assinar(dados: bytes) -> str:
    chave = hashlib.sha256(("aplogs-sessao:" + config.APLOGS_SECRET).encode()).digest()
    return base64.urlsafe_b64encode(hmac.new(chave, dados, hashlib.sha256).digest()).decode().rstrip("=")


def criar_token() -> str:
    corpo = json.dumps({"exp": int(time.time()) + config.SESSAO_HORAS * 3600}).encode()
    b64 = base64.urlsafe_b64encode(corpo).decode().rstrip("=")
    return f"{b64}.{_assinar(b64.encode())}"


def token_valido(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    b64, assinatura = token.rsplit(".", 1)
    if not hmac.compare_digest(assinatura, _assinar(b64.encode())):
        return False
    try:
        corpo = json.loads(base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)))
        return int(corpo.get("exp", 0)) > time.time()
    except (ValueError, TypeError):
        return False


def senha_confere(senha: str | None) -> bool:
    return bool(senha) and hmac.compare_digest(senha.encode(), config.APLOGS_SECRET.encode())


def ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def bloqueado(endereco: str) -> bool:
    fila = _falhas[endereco]
    limite = time.time() - JANELA_S
    while fila and fila[0] < limite:
        fila.popleft()
    return len(fila) >= MAX_FALHAS


def registrar_falha(endereco: str):
    _falhas[endereco].append(time.time())


def limpar_falhas(endereco: str):
    _falhas.pop(endereco, None)


# ── Dependências do FastAPI ──────────────────────────────────────────────────

def painel(request: Request, x_api_secret: str | None = Header(default=None)):
    """Rotas de leitura do painel: cookie de sessão ou header x-api-secret."""
    if token_valido(request.cookies.get(COOKIE)):
        return
    if x_api_secret is not None:
        if senha_confere(x_api_secret):
            return
        raise HTTPException(401, "Secret inválido")
    raise HTTPException(401, "Faça login no painel")


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
