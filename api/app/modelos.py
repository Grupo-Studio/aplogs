"""
modelos.py
==========
Tabelas da aplogs. Datas sempre em UTC (DATETIME sem fuso).

  servicos       um por serviço/robô conectado (criado sozinho no primeiro envio)
  heartbeats     cada "estou vivo" recebido (base do status online/offline e da linha do tempo)
  logs           linhas de log enviadas pelos serviços (+ eventos da própria aplogs, modulo "aplogs")
  problemas      incidentes abertos/resolvidos (os serviços abrem; o monitor abre "offline" sozinho)
  jobs           execuções/tarefas dos serviços (upsert pelo id do job no serviço)
  filas          foto atual de cada fila de cada serviço (upsert pelo nome)
  fila_amostras  histórico curto das filas (gráfico da última hora)
  web_sessoes    sessões do painel (login central/apusers): o cookie leva só o id
"""

from sqlalchemy import (JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, agora

# Tipos que mudam entre MySQL e sqlite (testes)
ID_GRANDE = BigInteger().with_variant(Integer, "sqlite")          # autoincrement no sqlite só com INTEGER
DATA = DateTime().with_variant(mysql.DATETIME(fsp=3), "mysql")     # milissegundos no MySQL
TEXTO_LONGO = Text().with_variant(mysql.MEDIUMTEXT(), "mysql")      # tracebacks e textos longos
# (TEXT do MySQL = 64 KB em BYTES; com acento/emoji em utf8mb4, 60 mil caracteres podem passar disso)


class Servico(Base):
    __tablename__ = "servicos"

    id:                  Mapped[int] = mapped_column(Integer, primary_key=True)
    slug:                Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    nome:                Mapped[str] = mapped_column(String(160), nullable=False)
    nome_editado:        Mapped[bool] = mapped_column(Boolean, default=False)   # editado no painel: o heartbeat não troca
    descricao:           Mapped[str | None] = mapped_column(TEXTO_LONGO)
    grupo:               Mapped[str | None] = mapped_column(String(80))
    versao:              Mapped[str | None] = mapped_column(String(80))
    host:                Mapped[str | None] = mapped_column(String(160))
    ambiente:            Mapped[str | None] = mapped_column(String(40))
    endereco:            Mapped[str | None] = mapped_column(String(300))         # http://192.168.1.100:3067 (onde atende)
    repositorio:         Mapped[str | None] = mapped_column(String(300))         # https://github.com/dono/repo
    branch:              Mapped[str | None] = mapped_column(String(120))         # branch que está rodando
    commit_sha:          Mapped[str | None] = mapped_column(String(64))          # commit que está rodando
    intervalo_s:         Mapped[int] = mapped_column(Integer, default=30)        # intervalo de heartbeat do serviço
    tolerancia_s:        Mapped[int | None] = mapped_column(Integer)             # None = max(3 × intervalo, mínimo)
    monitorar:           Mapped[bool] = mapped_column(Boolean, default=True)     # False = pausado (sem alerta de offline)
    status:              Mapped[str] = mapped_column(String(20), default="desconhecido")
    status_desde:        Mapped[object | None] = mapped_column(DATA)
    status_reportado:    Mapped[str | None] = mapped_column(String(20))          # ok | degradado | erro | parando
    mensagem:            Mapped[str | None] = mapped_column(String(500))
    iniciado_em:         Mapped[object | None] = mapped_column(DATA)             # quando o processo do serviço subiu
    ultimo_heartbeat_em: Mapped[object | None] = mapped_column(DATA)
    dados:               Mapped[dict | None] = mapped_column(JSON)               # "dados" do último heartbeat
    criado_em:           Mapped[object] = mapped_column(DATA, default=agora)
    atualizado_em:       Mapped[object] = mapped_column(DATA, default=agora, onupdate=agora)


class Heartbeat(Base):
    __tablename__ = "heartbeats"
    __table_args__ = (Index("ix_hb_servico_recebido", "servico_id", "recebido_em"),
                      Index("ix_hb_recebido", "recebido_em"))

    id:          Mapped[int] = mapped_column(ID_GRANDE, primary_key=True, autoincrement=True)
    servico_id:  Mapped[int] = mapped_column(ForeignKey("servicos.id", ondelete="CASCADE"), nullable=False)
    recebido_em: Mapped[object] = mapped_column(DATA, default=agora, nullable=False)
    status:      Mapped[str | None] = mapped_column(String(20))
    mensagem:    Mapped[str | None] = mapped_column(String(500))
    dados:       Mapped[dict | None] = mapped_column(JSON)


class Log(Base):
    __tablename__ = "logs"
    __table_args__ = (Index("ix_logs_servico_id", "servico_id", "id"),
                      Index("ix_logs_nivel_id", "nivel_num", "id"),
                      Index("ix_logs_recebido", "recebido_em"))

    id:          Mapped[int] = mapped_column(ID_GRANDE, primary_key=True, autoincrement=True)
    servico_id:  Mapped[int] = mapped_column(ForeignKey("servicos.id", ondelete="CASCADE"), nullable=False)
    ts:          Mapped[object] = mapped_column(DATA, nullable=False)                # quando aconteceu (no serviço)
    recebido_em: Mapped[object] = mapped_column(DATA, default=agora, nullable=False)
    nivel:       Mapped[str] = mapped_column(String(10), nullable=False)             # DEBUG INFO WARNING ERROR CRITICAL
    nivel_num:   Mapped[int] = mapped_column(Integer, nullable=False)                # 10 20 30 40 50 (filtro "mínimo")
    modulo:      Mapped[str | None] = mapped_column(String(80))
    mensagem:    Mapped[str] = mapped_column(TEXTO_LONGO, nullable=False)
    exc:         Mapped[str | None] = mapped_column(TEXTO_LONGO)                     # traceback
    contexto:    Mapped[dict | None] = mapped_column(JSON)


class Problema(Base):
    __tablename__ = "problemas"
    __table_args__ = (Index("ix_prob_servico_chave_status", "servico_id", "chave", "status"),
                      Index("ix_prob_status", "status"))

    id:            Mapped[int] = mapped_column(Integer, primary_key=True)
    servico_id:    Mapped[int] = mapped_column(ForeignKey("servicos.id", ondelete="CASCADE"), nullable=False)
    chave:         Mapped[str] = mapped_column(String(191), nullable=False)          # identifica o problema (dedup)
    titulo:        Mapped[str] = mapped_column(String(300), nullable=False)
    descricao:     Mapped[str | None] = mapped_column(TEXTO_LONGO)
    severidade:    Mapped[str] = mapped_column(String(10), default="erro")           # info aviso erro critico
    status:        Mapped[str] = mapped_column(String(15), default="aberto")         # aberto reconhecido resolvido
    origem:        Mapped[str] = mapped_column(String(15), default="servico")        # servico | monitor
    ocorrencias:   Mapped[int] = mapped_column(Integer, default=1)
    primeira_vez:  Mapped[object] = mapped_column(DATA, default=agora)
    ultima_vez:    Mapped[object] = mapped_column(DATA, default=agora)
    resolvido_em:  Mapped[object | None] = mapped_column(DATA)
    resolvido_por: Mapped[str | None] = mapped_column(String(80))                    # servico | monitor | nome de quem resolveu no painel
    dados:         Mapped[dict | None] = mapped_column(JSON)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("servico_id", "externo_id", name="uq_job_servico_externo"),
                      Index("ix_jobs_atualizado", "atualizado_em"),
                      Index("ix_jobs_status", "status"))

    id:            Mapped[int] = mapped_column(ID_GRANDE, primary_key=True, autoincrement=True)
    servico_id:    Mapped[int] = mapped_column(ForeignKey("servicos.id", ondelete="CASCADE"), nullable=False)
    externo_id:    Mapped[str] = mapped_column(String(120), nullable=False)          # id do job no serviço
    tipo:          Mapped[str | None] = mapped_column(String(60))
    nome:          Mapped[str | None] = mapped_column(String(300))
    status:        Mapped[str] = mapped_column(String(20), default="fila")           # fila rodando ok erro interrompido
    progresso:     Mapped[float | None] = mapped_column(Float)                       # 0–100
    etapa:         Mapped[str | None] = mapped_column(String(300))
    mensagem:      Mapped[str | None] = mapped_column(TEXTO_LONGO)
    criado_em:     Mapped[object | None] = mapped_column(DATA)
    iniciado_em:   Mapped[object | None] = mapped_column(DATA)
    terminado_em:  Mapped[object | None] = mapped_column(DATA)
    duracao_s:     Mapped[float | None] = mapped_column(Float)
    dados:         Mapped[dict | None] = mapped_column(JSON)
    atualizado_em: Mapped[object] = mapped_column(DATA, default=agora, onupdate=agora)


class Fila(Base):
    __tablename__ = "filas"
    __table_args__ = (UniqueConstraint("servico_id", "nome", name="uq_fila_servico_nome"),)

    id:            Mapped[int] = mapped_column(Integer, primary_key=True)
    servico_id:    Mapped[int] = mapped_column(ForeignKey("servicos.id", ondelete="CASCADE"), nullable=False)
    nome:          Mapped[str] = mapped_column(String(120), nullable=False)
    pendentes:     Mapped[int] = mapped_column(Integer, default=0)
    rodando:       Mapped[int] = mapped_column(Integer, default=0)
    concluidos:    Mapped[int] = mapped_column(Integer, default=0)
    erros:         Mapped[int] = mapped_column(Integer, default=0)
    workers:       Mapped[int | None] = mapped_column(Integer)
    dados:         Mapped[dict | None] = mapped_column(JSON)
    atualizado_em: Mapped[object] = mapped_column(DATA, default=agora)


class FilaAmostra(Base):
    __tablename__ = "fila_amostras"
    __table_args__ = (Index("ix_fa_fila_ts", "fila_id", "ts"), Index("ix_fa_ts", "ts"))

    id:        Mapped[int] = mapped_column(ID_GRANDE, primary_key=True, autoincrement=True)
    fila_id:   Mapped[int] = mapped_column(ForeignKey("filas.id", ondelete="CASCADE"), nullable=False)
    ts:        Mapped[object] = mapped_column(DATA, default=agora, nullable=False)
    pendentes: Mapped[int] = mapped_column(Integer, default=0)
    rodando:   Mapped[int] = mapped_column(Integer, default=0)
    erros:     Mapped[int] = mapped_column(Integer, default=0)


class WebSessao(Base):
    """Sessão do painel. Os tokens do apusers ficam AQUI, nunca no navegador (o cookie leva só o id).
    A renovação do token (de uso único) trava a linha: duas abas renovando juntas não derrubam a sessão."""
    __tablename__ = "web_sessoes"

    id:            Mapped[str] = mapped_column(String(64), primary_key=True)
    access_token:  Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str] = mapped_column(String(300), nullable=False)
    email:         Mapped[str | None] = mapped_column(String(191))
    expira:        Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)    # epoch: fim da sessão do painel
    criada:        Mapped[int] = mapped_column(BigInteger, nullable=False)
    renovada:      Mapped[int | None] = mapped_column(BigInteger)
