"""Ambiente dos testes (antes de importar o app). Usa sqlite; TEST_DATABASE_URL troca para um MySQL de verdade.
O login central é a réplica do apusers em mock_apusers.py (porta 8768)."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

os.environ.setdefault("APLOGS_SECRET", "senha-teste")
os.environ.setdefault("APLOGS_INGEST_KEY", "chave-teste")
os.environ.setdefault("MONITOR_INTERVALO_S", "3600")      # os testes chamam a rodada na mão
os.environ["APUSERS_URL"] = "http://127.0.0.1:8768"
os.environ["APUSERS_KEY"] = "aps_0123456789abcdef_chave-de-teste-da-aplogs"
os.environ["DATABASE_URL"] = os.getenv("TEST_DATABASE_URL") or \
    f"sqlite:///{tempfile.mkdtemp(prefix='aplogs-')}/teste.db"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


APUSERS = "http://127.0.0.1:8768"


@pytest.fixture(scope="session", autouse=True)
def mock_apusers():
    proc = subprocess.Popen([sys.executable, str(Path(__file__).parent / "mock_apusers.py"), "8768"])
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", 8768), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield
    proc.terminate()


def apusers(caminho: str, corpo: dict | None = None) -> dict:
    """Controle da réplica do apusers: apusers("/_mock/estado"), apusers("/_mock/expirar", {...})."""
    dados = json.dumps(corpo).encode() if corpo is not None else None
    req = urllib.request.Request(APUSERS + caminho, data=dados, method="POST" if dados is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read() or b"{}")


def entrar(client, nome: str):
    """Entra no painel como <nome>@exemplo.test (senha da réplica: "senha-<nome>")."""
    client.cookies.clear()
    r = client.post("/api/auth/login", json={"email": f"{nome}@exemplo.test", "senha": f"senha-{nome}"})
    assert r.status_code == 200, r.text
    return r.json()["usuario"]


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
