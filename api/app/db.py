"""
db.py
=====
Conexão com o banco (MySQL em produção, sqlite nos testes) e criação das tabelas.
As tabelas são criadas no arranque se não existirem (create_all).
"""

import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from . import config

logger = logging.getLogger("aplogs.db")


class Base(DeclarativeBase):
    pass


def _criar_engine():
    url = config.DATABASE_URL
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url, pool_pre_ping=True, pool_recycle=1800, pool_size=10, max_overflow=20)


engine = _criar_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def agora() -> datetime:
    """UTC sem fuso (é assim que vai para o DATETIME do MySQL)."""
    return datetime.now(tz=timezone.utc).replace(tzinfo=None)


def iso(dt: datetime | None) -> str | None:
    return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


def preparar(tentativas: int = 60):
    """Espera o MySQL aceitar conexão (o container do banco demora a subir) e cria as tabelas."""
    from . import modelos  # noqa: F401  (registra as tabelas no Base)

    for n in range(1, tentativas + 1):
        try:
            with engine.connect() as c:
                c.execute(text("SELECT 1"))
            break
        except Exception as e:
            if n == tentativas:
                raise
            logger.warning(f"Banco ainda indisponível ({n}/{tentativas}): {e.__class__.__name__}")
            time.sleep(2)
    Base.metadata.create_all(engine)
    logger.info("Banco pronto")


def get_db():
    """Dependência do FastAPI: uma sessão por requisição."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def sessao():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def banco_ok() -> bool:
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
