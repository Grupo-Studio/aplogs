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

from sqlalchemy import create_engine, inspect, text
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
    _migrar()
    logger.info("Banco pronto")


def _migrar():
    """O create_all só cria tabela que não existe: não mexe nas que já existem. Aqui ficam os
    ajustes para um banco criado por uma versão anterior da aplogs:
      - adiciona as colunas novas (sempre opcionais / NULL)
      - MySQL: TEXT → MEDIUMTEXT nas colunas que mudaram de tipo"""
    insp = inspect(engine)
    q = engine.dialect.identifier_preparer.quote
    with engine.begin() as c:
        for tabela in Base.metadata.sorted_tables:
            if not insp.has_table(tabela.name):
                continue
            no_banco = {col["name"]: col for col in insp.get_columns(tabela.name)}
            for col in tabela.columns:
                tipo = col.type.compile(dialect=engine.dialect)
                if col.name not in no_banco:
                    if not col.nullable:
                        logger.warning(f"Coluna obrigatória {tabela.name}.{col.name} faltando: crie na mão")
                        continue
                    c.execute(text(f"ALTER TABLE {q(tabela.name)} ADD COLUMN {q(col.name)} {tipo} NULL"))
                    logger.info(f"Banco atualizado: coluna {tabela.name}.{col.name} criada")
                elif (engine.dialect.name == "mysql" and tipo == "MEDIUMTEXT"
                      and type(no_banco[col.name]["type"]).__name__ == "TEXT"):
                    nulo = "NULL" if col.nullable else "NOT NULL"
                    c.execute(text(f"ALTER TABLE {q(tabela.name)} MODIFY COLUMN {q(col.name)} MEDIUMTEXT {nulo}"))
                    logger.info(f"Banco atualizado: {tabela.name}.{col.name} agora é MEDIUMTEXT")


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
