"""
main.py
=======
aplogs — central de logs e monitoramento dos serviços e robôs do grupo.

  Serviços → aplogs (header x-api-key):   POST /api/ingest/{heartbeat,logs,problemas,jobs,filas}
  Pessoas  → painel (login central/apusers): http://<host>:3070/
  Scripts  → API do painel (header x-api-secret)

O monitor de heartbeat marca como OFFLINE (e abre um problema) o serviço que parou de mandar
heartbeat por mais tempo que a tolerância dele.
"""

import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import acesso, auth, config, db, ingestao, integracao, monitor, painel

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("aplogs")

VERSAO = "1.2.0"
ESTATICO = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if not config.APLOGS_SECRET:
        raise RuntimeError("Defina APLOGS_SECRET (scripts com x-api-secret e assinatura do cookie) no .env")
    if not config.APLOGS_INGEST_KEY:
        raise RuntimeError("Defina APLOGS_INGEST_KEY (chave que os serviços usam para enviar dados) no .env")
    db.preparar()
    monitor.iniciar()
    threading.Thread(target=acesso.declarar_papeis, name="apusers-papeis", daemon=True).start()
    logger.info(f"aplogs {VERSAO} pronta")
    try:
        yield
    finally:
        monitor.encerrar()
        acesso.encerrar()


app = FastAPI(title="aplogs", version=VERSAO, lifespan=lifespan, docs_url="/api/docs", redoc_url=None,
              openapi_url="/api/openapi.json")


@app.exception_handler(HTTPException)
async def _http_erro(_req: Request, exc: HTTPException):
    # Erros do login central chegam como {"mensagem", "codigo", ...}: "error" segue sendo o texto,
    # "codigo" é estável (o painel decide pelo código, não pelo texto).
    if isinstance(exc.detail, dict) and "mensagem" in exc.detail:
        extra = {k: v for k, v in exc.detail.items() if k != "mensagem"}
        return JSONResponse({"ok": False, "error": exc.detail["mensagem"], **extra}, status_code=exc.status_code)
    return JSONResponse({"ok": False, "error": exc.detail}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def _validacao(_req: Request, exc: RequestValidationError):
    erros = [f"{'.'.join(str(x) for x in e['loc'][1:])}: {e['msg']}" for e in exc.errors()]
    return JSONResponse({"ok": False, "error": "Dados inválidos", "detalhes": erros}, status_code=422)


app.include_router(ingestao.router)
app.include_router(painel.router)
app.include_router(integracao.router)


# ── Saúde e login ────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    ok = db.banco_ok()
    return JSONResponse({"ok": ok, "status": "online" if ok else "sem banco", "versao": VERSAO,
                         "timestamp": db.iso(db.agora()),
                         "login_central": {"configurado": acesso.configurado()}}, status_code=200 if ok else 503)


class Entrada(BaseModel):
    email: str = Field(..., max_length=191)
    senha: str = Field(..., max_length=200)


class Cadastro(BaseModel):
    email: str = Field(..., max_length=191)
    nome: str = Field(..., min_length=2, max_length=160)
    senha: str = Field(..., min_length=8, max_length=200)
    area: str | None = Field(None, max_length=120)
    cargo: str | None = Field(None, max_length=120)
    justificativa: str | None = Field(None, max_length=1000)


class Evento(BaseModel):
    """Formato do apusers (POST /v1/service/events), repassado sem tradução."""
    type: Literal["tela", "acao", "evento"]
    name: str = Field(..., min_length=1, max_length=191)
    detail: str | None = Field(None, max_length=500)
    duration_ms: int | None = Field(None, ge=0, le=30 * 86400 * 1000)
    started_at: str | None = None
    ended_at: str | None = None
    metadata: dict[str, Any] | None = None


class Presenca(BaseModel):
    screen: str | None = Field(None, max_length=191)
    activity: str | None = Field(None, max_length=500)
    status: Literal["ativo", "ocioso", "saiu"] = "ativo"


class Atividade(BaseModel):
    events: list[Evento] = Field(default_factory=list, max_length=500)
    presence: Presenca | None = None


@app.post("/api/auth/login")
def login(corpo: Entrada, request: Request, response: Response):
    """E-mail e senha do apusers. Quem ainda não tem acesso à aplogs gera um pedido para o administrador.
    O limite de tentativas é do apusers (por IP de quem está no navegador, repassado pela aplogs)."""
    email = (corpo.email or "").strip().lower()
    if not email or not corpo.senha:
        raise acesso.erro(400, "Digite o e-mail e a senha.", "requisicao_invalida")
    tokens, u = acesso.login(email, corpo.senha, auth.ip(request))
    auth.abrir_sessao(response, tokens, u.email)
    return {"ok": True, "usuario": u.publico(), "expira_em_horas": config.SESSAO_HORAS}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    auth.encerrar_sessao(request, response)
    return {"ok": True}


@app.get("/api/auth/sessao")
def sessao_atual(ator=Depends(auth.painel)):
    if ator is auth.SCRIPT:
        return {"ok": True, "usuario": None, "script": True}
    return {"ok": True, "usuario": ator.publico(), "papeis": acesso.PAPEIS}


@app.post("/api/auth/cadastro", status_code=202)
def cadastro(c: Cadastro, request: Request):
    """Conta nova no apusers (fica pendente). Aprovada pelo administrador, já entra com acesso à aplogs."""
    r = acesso.cadastrar(c.email.strip().lower(), c.nome.strip(), c.senha, auth.ip(request),
                         area=c.area, job_title=c.cargo, justification=c.justificativa)
    return {"ok": True, "pending": True, "message": r.get("message")}


@app.post("/api/atividade")
def atividade(corpo: Atividade, ator=Depends(auth.painel)):
    """Tempo de tela por aba e presença, mandados pelo painel e repassados ao apusers."""
    if ator is auth.SCRIPT:
        raise acesso.erro(403, "Rota de pessoas (painel), não de scripts.", "so_pessoas")
    acesso.repassar_eventos(ator, [ev.model_dump(exclude_none=True) for ev in corpo.events])
    if corpo.presence:
        acesso.presenca(ator, corpo.presence.screen, corpo.presence.activity, corpo.presence.status)
    return {"ok": True, "recebidos": len(corpo.events)}


# ── Frontend (arquivos estáticos, sem build) ─────────────────────────────────

app.mount("/static", StaticFiles(directory=ESTATICO), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(ESTATICO / "index.html", headers={"Cache-Control": "no-cache"})
