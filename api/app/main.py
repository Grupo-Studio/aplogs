"""
main.py
=======
aplogs — central de logs e monitoramento dos serviços e robôs do grupo.

  Serviços → aplogs (header x-api-key):   POST /api/ingest/{heartbeat,logs,problemas,jobs,filas}
  Pessoas  → painel (senha APLOGS_SECRET): http://<host>:3070/

O monitor de heartbeat marca como OFFLINE (e abre um problema) o serviço que parou de mandar
heartbeat por mais tempo que a tolerância dele.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, config, db, ingestao, monitor, painel

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("aplogs")

VERSAO = "1.0.0"
ESTATICO = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if not config.APLOGS_SECRET:
        raise RuntimeError("Defina APLOGS_SECRET (senha do painel) no .env")
    if not config.APLOGS_INGEST_KEY:
        raise RuntimeError("Defina APLOGS_INGEST_KEY (chave que os serviços usam para enviar dados) no .env")
    db.preparar()
    monitor.iniciar()
    logger.info(f"aplogs {VERSAO} pronta")
    try:
        yield
    finally:
        monitor.encerrar()


app = FastAPI(title="aplogs", version=VERSAO, lifespan=lifespan, docs_url="/api/docs", redoc_url=None,
              openapi_url="/api/openapi.json")


@app.exception_handler(HTTPException)
async def _http_erro(_req: Request, exc: HTTPException):
    return JSONResponse({"ok": False, "error": exc.detail}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def _validacao(_req: Request, exc: RequestValidationError):
    erros = [f"{'.'.join(str(x) for x in e['loc'][1:])}: {e['msg']}" for e in exc.errors()]
    return JSONResponse({"ok": False, "error": "Dados inválidos", "detalhes": erros}, status_code=422)


app.include_router(ingestao.router)
app.include_router(painel.router)


# ── Saúde e login ────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    ok = db.banco_ok()
    return JSONResponse({"ok": ok, "status": "online" if ok else "sem banco", "versao": VERSAO,
                         "timestamp": db.iso(db.agora())}, status_code=200 if ok else 503)


class Login(BaseModel):
    secret: str


@app.post("/api/auth/login")
def login(corpo: Login, request: Request, response: Response):
    endereco = auth.ip(request)
    if auth.bloqueado(endereco):
        raise HTTPException(429, "Muitas tentativas. Espere alguns minutos e tente de novo.")
    if not auth.senha_confere(corpo.secret):
        auth.registrar_falha(endereco)
        logger.warning(f"Senha errada no painel (IP {endereco})")
        raise HTTPException(401, "Senha incorreta")
    auth.limpar_falhas(endereco)
    response.set_cookie(auth.COOKIE, auth.criar_token(), max_age=config.SESSAO_HORAS * 3600, httponly=True,
                        samesite="lax", path="/")
    return {"ok": True, "expira_em_horas": config.SESSAO_HORAS}


@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/sessao", dependencies=[Depends(auth.painel)])
def sessao_atual():
    return {"ok": True}


# ── Frontend (arquivos estáticos, sem build) ─────────────────────────────────

app.mount("/static", StaticFiles(directory=ESTATICO), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(ESTATICO / "index.html", headers={"Cache-Control": "no-cache"})
