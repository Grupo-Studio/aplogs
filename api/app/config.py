"""
config.py
=========
Configuração da aplogs via variáveis de ambiente (veja .env.example na raiz).
"""

import os
from urllib.parse import quote_plus


def _int(nome: str, padrao: int) -> int:
    try:
        return int(os.getenv(nome, str(padrao)))
    except ValueError:
        return padrao


# ── Segurança ────────────────────────────────────────────────────────────────
# Senha do painel (tela de login). Também vale como header x-api-secret para ler a API por script.
APLOGS_SECRET = os.getenv("APLOGS_SECRET", "").strip()
# Chave que os serviços mandam no header x-api-key para enviar heartbeat, logs, etc.
APLOGS_INGEST_KEY = os.getenv("APLOGS_INGEST_KEY", "").strip()
# Quanto tempo o login do painel dura
SESSAO_HORAS = _int("SESSAO_HORAS", 12)

# ── Banco (MySQL) ────────────────────────────────────────────────────────────
# DATABASE_URL substitui tudo (os testes usam sqlite). Sem ela, monta a URL do MySQL.
DB_HOST     = os.getenv("DB_HOST", "aplogs-db")
DB_PORT     = _int("DB_PORT", 3306)
DB_USER     = os.getenv("DB_USER", "aplogs")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME     = os.getenv("DB_NAME", "aplogs")
DATABASE_URL = os.getenv("DATABASE_URL") or (
    # usuário/senha codificados: senha com @ : / # % etc. quebraria a URL
    f"mysql+pymysql://{quote_plus(DB_USER)}:{quote_plus(DB_PASSWORD)}@{DB_HOST}:{DB_PORT}/{DB_NAME}?charset=utf8mb4"
)

# ── Monitor de heartbeat ─────────────────────────────────────────────────────
# De quanto em quanto tempo o monitor confere se algum serviço parou de mandar heartbeat
MONITOR_INTERVALO_S = _int("MONITOR_INTERVALO_S", 10)
# Intervalo de heartbeat assumido quando o serviço não informa o dele
HEARTBEAT_INTERVALO_PADRAO_S = _int("HEARTBEAT_INTERVALO_PADRAO_S", 30)
# Tolerância mínima: o serviço só fica offline depois de max(3 × intervalo, isto) sem heartbeat
TOLERANCIA_MINIMA_S = _int("TOLERANCIA_MINIMA_S", 60)

# ── Retenção (dias). 0 = guarda para sempre ──────────────────────────────────
RETENCAO_LOGS_DIAS       = _int("RETENCAO_LOGS_DIAS", 30)
RETENCAO_HEARTBEATS_DIAS = _int("RETENCAO_HEARTBEATS_DIAS", 7)
RETENCAO_JOBS_DIAS       = _int("RETENCAO_JOBS_DIAS", 30)
RETENCAO_PROBLEMAS_DIAS  = _int("RETENCAO_PROBLEMAS_DIAS", 90)

# ── Limites de ingestão ──────────────────────────────────────────────────────
MAX_LOGS_POR_ENVIO = _int("MAX_LOGS_POR_ENVIO", 2000)
MAX_MENSAGEM       = 60_000            # caracteres de uma mensagem de log (TEXT do MySQL)
