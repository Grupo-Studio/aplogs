"""Ambiente dos testes (antes de importar o app). Usa sqlite; TEST_DATABASE_URL troca para um MySQL de verdade."""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("APLOGS_SECRET", "senha-teste")
os.environ.setdefault("APLOGS_INGEST_KEY", "chave-teste")
os.environ.setdefault("MONITOR_INTERVALO_S", "3600")      # os testes chamam a rodada na mão
os.environ["DATABASE_URL"] = os.getenv("TEST_DATABASE_URL") or \
    f"sqlite:///{tempfile.mkdtemp(prefix='aplogs-')}/teste.db"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="session")
def client():
    from app import modelos  # noqa: F401
    from app.db import Base, engine
    from app.main import app
    Base.metadata.drop_all(engine)       # começa do zero (TEST_DATABASE_URL tem que ser um banco SÓ de teste)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def ingest():
    return {"x-api-key": "chave-teste"}


@pytest.fixture()
def painel():
    return {"x-api-secret": "senha-teste"}
