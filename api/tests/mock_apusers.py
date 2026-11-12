"""
mock_apusers.py
===============
Réplica mínima do apusers (login central) para os testes: as rotas que o serviço usa, com usuários
fixos e um controle (/_mock/...) para simular token vencido, acesso revogado e troca de papel.

    python mock_apusers.py 8768

Usuários (senha = "senha-" + primeiro nome):
  ana@exemplo.test     operador          vera@exemplo.test    visualizador
  gil@exemplo.test     gestor            sem@exemplo.test     acesso sem papel (vale o PAPEL_PADRAO)
  bruno@exemplo.test   sem acesso (403 acesso_restrito, abre pedido)
  paula@exemplo.test   cadastro pendente (403 usuario_pendente)
"""

import itertools
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHAVE = "aps_0123456789abcdef_chave-de-teste-da-aplogs"
USUARIOS = {
    "ana@exemplo.test":   {"id": "u-ana", "name": "Ana Operadora", "role": "operador"},
    "vera@exemplo.test":  {"id": "u-vera", "name": "Vera Visual", "role": "visualizador"},
    "gil@exemplo.test":   {"id": "u-gil", "name": "Gil Gestor", "role": "gestor"},
    "sem@exemplo.test":   {"id": "u-sem", "name": "Sem Papel", "role": None},
    "bruno@exemplo.test": {"id": "u-bruno", "name": "Bruno", "role": None, "acesso": False},
    "paula@exemplo.test": {"id": "u-paula", "name": "Paula", "role": None, "status": "pendente"},
}
_trava = threading.Lock()
_seq = itertools.count(1)
E = {}


def limpar():
    E.update(acessos={}, refreshes={}, logins=[], ips=[], cadastros=[], eventos=[], presencas=[], logouts=[],
             papeis=None, revogados=set(), refresh_usados=0)


limpar()


def senha_de(email):
    return "senha-" + email.split("@")[0]


def _par(email, sid=None):
    n = next(_seq)
    sid = sid or f"sid-{n}"
    acesso, refresh = f"at-{n}-{email}", f"apr_rt-{n}-{email}"
    E["acessos"][acesso] = {"email": email, "sid": sid, "expirado": False}
    E["refreshes"][refresh] = {"email": email, "sid": sid, "usado": False}
    return {"token_type": "Bearer", "access_token": acesso, "expires_in": 900, "refresh_token": refresh,
            "refresh_expires_in": 2592000, "session_id": sid}


def _publico(email):
    u = USUARIOS[email]
    return {"id": u["id"], "email": email, "name": u["name"], "status": u.get("status", "ativo")}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, status, corpo):
        dados = json.dumps(corpo).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)

    def _corpo(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _erro(self, status, codigo, msg, **extra):
        self._json(status, {"ok": False, "error": codigo, "message": msg, **extra})

    def _chave_ok(self):
        if self.headers.get("x-service-key") != CHAVE:
            self._erro(401, "chave_invalida", "Chave de serviço inválida ou revogada.")
            return False
        return True

    def do_GET(self):
        with _trava:
            if self.path == "/_mock/estado":
                return self._json(200, {k: (sorted(v) if isinstance(v, set) else v) for k, v in E.items()
                                        if k not in ("acessos", "refreshes")})
            if self.path == "/v1/service/me" and self._chave_ok():
                return self._json(200, {"ok": True, "service": {"slug": "aplogs"}})
            self._erro(404, "rota_inexistente", "Rota não encontrada.")

    def do_PATCH(self):
        with _trava:
            if self.path == "/v1/service/me":
                if not self._chave_ok():
                    return
                E["papeis"] = self._corpo().get("roles")
                return self._json(200, {"ok": True, "service": {"slug": "aplogs"}, "roles": E["papeis"]})
            self._erro(404, "rota_inexistente", "Rota não encontrada.")

    def do_POST(self):
        with _trava:
            c, p = self._corpo(), self.path
            # ── controle dos testes ──
            if p == "/_mock/limpar":
                limpar()
                return self._json(200, {"ok": True})
            if p == "/_mock/expirar":
                for t in E["acessos"].values():
                    if c.get("email") in (None, t["email"]):
                        t["expirado"] = True
                return self._json(200, {"ok": True})
            if p == "/_mock/revogar":
                E["revogados"].add(c["email"])
                return self._json(200, {"ok": True})
            if p == "/_mock/papel":
                USUARIOS[c["email"]]["role"] = c.get("role")
                return self._json(200, {"ok": True})
            # ── apusers ──
            if p == "/v1/auth/login":
                if not self._chave_ok():
                    return
                email = (c.get("email") or "").lower()
                E["ips"].append(self.headers.get("x-apusers-client-ip"))
                u = USUARIOS.get(email)
                if not u or c.get("password") != senha_de(email):
                    return self._erro(401, "credenciais_invalidas", "E-mail ou senha incorretos.")
                if u.get("status") == "pendente":
                    return self._erro(403, "usuario_pendente", "Seu cadastro ainda está aguardando a aprovação do administrador.")
                if u.get("acesso") is False or email in E["revogados"]:
                    return self._erro(403, "acesso_restrito", 'Acesso restrito: você não tem acesso a "aplogs". '
                                                             "O pedido foi enviado ao administrador.",
                                      pedido={"id": 7, "status": "pendente"})
                E["logins"].append(email)
                return self._json(200, {"ok": True, **_par(email), "user": _publico(email),
                                        "access": {"service": "aplogs", "role": u["role"], "via": "admin"}})
            if p == "/v1/auth/verify":
                if not self._chave_ok():
                    return
                t = E["acessos"].get(c.get("token"))
                if not t:
                    return self._json(200, {"active": False, "reason": "token_invalido"})
                if t["expirado"]:
                    return self._json(200, {"active": False, "reason": "token_expirado"})
                if t["email"] in E["revogados"]:
                    return self._json(200, {"active": False, "reason": "acesso_restrito"})
                u = USUARIOS[t["email"]]
                return self._json(200, {"active": True, "user": _publico(t["email"]), "session_id": t["sid"],
                                        "access": {"service": "aplogs", "role": u["role"], "via": "admin"}})
            if p == "/v1/auth/refresh":
                r = E["refreshes"].get(c.get("refresh_token"))
                if not r:
                    return self._erro(401, "sessao_invalida", "Sessão inválida.")
                if r["usado"]:
                    return self._erro(401, "sessao_encerrada", "Token de renovação reutilizado: sessão encerrada.")
                r["usado"] = True
                E["refresh_usados"] += 1
                return self._json(200, {"ok": True, **_par(r["email"], r["sid"])})
            if p == "/v1/auth/logout":
                token = (self.headers.get("Authorization") or "")[7:]
                t = E["acessos"].pop(token, None)
                E["logouts"].append(t["email"] if t else None)
                return self._json(200, {"ok": True})
            if p == "/v1/auth/register":
                if not self._chave_ok():
                    return
                E["cadastros"].append({**c, "ip": self.headers.get("x-apusers-client-ip")})
                return self._json(202, {"ok": True, "pending": True, "message": "Cadastro recebido."})
            if p == "/v1/service/events":
                if not self._chave_ok():
                    return
                E["eventos"].extend(c.get("events") or [c])
                return self._json(200, {"ok": True, "aceitos": len(c.get("events") or [c]), "rejeitados": []})
            if p == "/v1/service/presence":
                if not self._chave_ok():
                    return
                E["presencas"].append(c)
                return self._json(200, {"ok": True})
            self._erro(404, "rota_inexistente", "Rota não encontrada.")


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 8768), H).serve_forever()
