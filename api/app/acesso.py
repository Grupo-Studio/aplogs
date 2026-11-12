"""
acesso.py
=========
Login central (apusers) do painel: quem é a pessoa, o que ela pode fazer e a atividade dela.

O navegador NUNCA fala com o apusers: a chave do serviço (APUSERS_KEY) fica só aqui.
  - Token: conferido no apusers (POST /v1/auth/verify, cache de 30 s no cliente). Tirar o acesso,
    desativar a pessoa ou trocar o papel vale em até 30 s.
  - Permissões: por papel. A aplogs declara os papéis no apusers ao subir (PAPEIS); o administrador
    escolhe o papel de cada pessoa no painel do apusers. Sem papel = PAPEL_PADRAO (só ver).
    Administrador do apusers (is_admin) é sempre gestor aqui: ele já pode se dar qualquer papel lá.
  - Atividade: ações (tratar problema, editar/apagar serviço) registradas AQUI, na rota; tempo de tela e
    presença a página manda em POST /api/atividade (formato do apusers) e a aplogs repassa.
  - apusers fora do ar: entrar no painel responde 503; a ingestão dos serviços e o monitor seguem.

Síncrono de propósito: a aplogs usa rotas síncronas (o FastAPI roda cada uma numa thread), e o cliente
do apusers é síncrono. Nada aqui roda no event loop.
"""

import logging
import threading
import urllib.error
from dataclasses import dataclass

from fastapi import HTTPException

from . import config
from .apusers_client import ApUsers, ApUsersErro

logger = logging.getLogger("aplogs.acesso")

# Papéis que a aplogs declara no apusers (PATCH /v1/service/me ao subir). A lista é do código.
PAPEIS = [
    {"role": "visualizador", "description": "Vê serviços, logs, problemas, jobs e filas"},
    {"role": "operador", "description": "Também reconhece e resolve problemas"},
    {"role": "gestor", "description": "Também edita (nome, grupo, repositório, monitoramento) e apaga serviços"},
]
NOMES_PAPEIS = {p["role"] for p in PAPEIS}
PAPEL_ADMIN = "gestor"           # papel de quem é administrador do apusers (is_admin)

# Ação → papéis que podem. Scripts (x-api-secret) podem tudo.
PERMISSOES = {
    "ver":       {"visualizador", "operador", "gestor"},
    "tratar":    {"operador", "gestor"},
    "gerenciar": {"gestor"},
}
DESCRICAO = {"ver": "ver o painel", "tratar": "reconhecer ou resolver problemas",
             "gerenciar": "editar ou apagar serviços"}

MENSAGENS = {
    "token_expirado": "Sua sessão expirou: entre de novo.",
    "token_invalido": "Sessão inválida: entre de novo.",
    "sessao_invalida": "Sessão inválida: entre de novo.",
    "sessao_encerrada": "Sua sessão foi encerrada: entre de novo.",
    "acesso_restrito": "Você não tem mais acesso à aplogs. Peça ao administrador do apusers.",
    "usuario_inativo": "Seu usuário foi desativado. Procure o administrador.",
    "usuario_pendente": "Seu cadastro ainda está aguardando a aprovação do administrador.",
    "servico_desativado": "A aplogs foi desativada no apusers.",
}
MOTIVOS_403 = {"acesso_restrito", "usuario_inativo", "usuario_pendente", "servico_desativado"}


def erro(status: int, mensagem: str, codigo: str, **extra) -> HTTPException:
    return HTTPException(status, {"mensagem": mensagem, "codigo": codigo, **extra})


_cliente: ApUsers | None = None


def configurado() -> bool:
    return bool(config.APUSERS_URL and config.APUSERS_KEY)


def cliente() -> ApUsers:
    global _cliente
    if not configurado():
        raise erro(503, "Login central não configurado: defina APUSERS_URL e APUSERS_KEY no .env da aplogs.",
                   "login_central_desligado")
    if _cliente is None:
        _cliente = ApUsers(config.APUSERS_URL, config.APUSERS_KEY, timeout=8.0, intervalo_envio=5.0,
                           cache_verificacao_s=30.0)
    return _cliente


def chamar(fn, *args, **kw):
    """Chama o apusers e traduz as falhas: rede fora → 503; chave recusada → 503; erro da pessoa →
    mesmo status e código do apusers."""
    try:
        return fn(*args, **kw)
    except ApUsersErro as e:
        if e.code in ("chave_invalida", "chave_ausente"):
            logger.error(f"[login central] o apusers recusou a APUSERS_KEY ({e.code})")
            raise erro(503, "A chave da aplogs no apusers (APUSERS_KEY) foi recusada. Avise o responsável.",
                       "login_central_mal_configurado")
        extra = {k: v for k, v in (e.corpo or {}).items() if k in ("pedido", "retry_after_s")}
        raise erro(e.status, e.message, e.code, **extra)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        logger.warning(f"[login central] apusers fora do ar ({config.APUSERS_URL}): {e}")
        raise erro(503, "Login central (apusers) fora do ar. Tente de novo em instantes.", "login_central_fora")


@dataclass
class Usuario:
    id: str
    email: str
    nome: str
    papel: str
    admin: bool = False          # administrador do apusers (vale como gestor)
    origem: str = "web"

    def pode(self, acao: str) -> bool:
        return self.papel in PERMISSOES.get(acao, ())

    @property
    def permissoes(self) -> list[str]:
        return sorted(a for a, papeis in PERMISSOES.items() if self.papel in papeis)

    @property
    def rotulo(self) -> str:
        return f"{self.nome} <{self.email}>"

    def publico(self) -> dict:
        return {"id": self.id, "email": self.email, "nome": self.nome, "papel": self.papel, "admin": self.admin,
                "permissoes": self.permissoes}


def _usuario(user: dict, access: dict | None) -> Usuario:
    role = (access or {}).get("role")
    admin = bool(user.get("is_admin"))
    if admin:
        papel = PAPEL_ADMIN
    else:
        papel = role if role in NOMES_PAPEIS else config.PAPEL_PADRAO
    return Usuario(id=user["id"], email=user["email"], nome=user.get("name") or user["email"], papel=papel, admin=admin)


def usuario_do_token(token: str) -> Usuario:
    v = chamar(cliente().verificar, token)
    if not v.get("active"):
        motivo = v.get("reason") or "sessao_invalida"
        raise erro(403 if motivo in MOTIVOS_403 else 401, MENSAGENS.get(motivo, "Sessão inválida: entre de novo."), motivo)
    return _usuario(v["user"], v.get("access"))


def login(email: str, senha: str, ip: str | None) -> tuple[dict, Usuario]:
    r = chamar(cliente().login, email, senha, True, ip)
    u = _usuario(r["user"], r.get("access"))
    logger.info(f"[login central] {u.rotulo} entrou no painel (papel {u.papel})")
    return {k: r.get(k) for k in ("access_token", "refresh_token")}, u


def sair(access_token: str | None, usuario: Usuario | None = None):
    if usuario:
        presenca(usuario, status="saiu")
    if access_token and configurado():
        try:
            chamar(cliente().logout, access_token)
        except HTTPException:
            pass


def cadastrar(email: str, nome: str, senha: str, ip: str | None, **extra) -> dict:
    return chamar(cliente().cadastrar, email, nome, senha, ip=ip, **{k: v for k, v in extra.items() if v})


def registrar_acao(ator, nome: str, detalhe: str | None = None, **dados):
    """Ação de uma pessoa, registrada pela rota (em lote, em segundo plano). Scripts não entram aqui."""
    if not isinstance(ator, Usuario) or not configurado():
        return
    try:
        cliente().acao(ator.id, nome, detalhe, {"origem": "web", **dados})
    except Exception:
        logger.debug("[login central] falha ao enfileirar ação", exc_info=True)


def repassar_eventos(usuario: Usuario, eventos: list[dict]):
    for ev in eventos:
        cliente().enfileirar({**ev, "user_id": usuario.id, "metadata": {**(ev.get("metadata") or {}), "origem": "web"}})


def presenca(usuario: Usuario, screen: str | None = None, activity: str | None = None, status: str = "ativo"):
    """Dispara numa thread e não espera (o cliente engole as falhas)."""
    if not configurado():
        return
    c = cliente()
    threading.Thread(target=c.presenca, args=(usuario.id, screen, activity, status), daemon=True).start()


def declarar_papeis():
    """Ao subir (numa thread): confere a chave e declara os papéis da aplogs. Não impede o arranque."""
    if not configurado():
        logger.warning("[login central] APUSERS_URL/APUSERS_KEY ausentes: ninguém entra no painel "
                       "(a ingestão e os scripts com x-api-secret seguem funcionando)")
        return
    try:
        r = chamar(cliente().atualizar_servico, roles=PAPEIS)
        logger.info(f"[login central] conectado ao apusers em {config.APUSERS_URL} como "
                    f"'{(r.get('service') or {}).get('slug')}'; papéis: {', '.join(sorted(NOMES_PAPEIS))}")
    except HTTPException as e:
        logger.warning(f"[login central] não deu para declarar os papéis agora: {e.detail}")


def encerrar():
    if _cliente is not None:
        _cliente.fechar()
