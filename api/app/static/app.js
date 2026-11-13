/* aplogs — painel (JS puro, sem build). Consulta a API a cada poucos segundos. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const ATUALIZA_MS = 5000;
const LOGS_VIVO_MS = 3000;

const estado = {
  aba: "servicos",
  painel: null,
  statusAnterior: {},          // slug → status (para avisar quando cai)
  logs: { itens: [], ultimoId: 0, maisAntigo: null, acabou: false },
  probStatus: "abertos",
  gaveta: null,                // slug aberto na gaveta
  integracao: false,           // gaveta mostrando "Gerenciar integração"
  timers: [],
  usuario: null,               // quem entrou (login central): {nome, email, papel, permissoes}
};

// Permissão vem do papel no apusers (a API confere de novo; aqui só esconde o que não pode)
function pode(acao) { return !!estado.usuario && (estado.usuario.permissoes || []).includes(acao); }

// Motivos que tiram a pessoa do painel (a sessão já foi descartada no servidor)
const SAI_DO_PAINEL = ["nao_autenticado", "acesso_restrito", "usuario_inativo", "usuario_pendente", "servico_desativado",
  "token_invalido", "sessao_invalida", "sessao_encerrada"];

// ── Utilidades ──────────────────────────────────────────────────────────────

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function data(iso) { return iso ? new Date(iso) : null; }

function relativo(iso) {
  const d = data(iso);
  if (!d) return "nunca";
  const s = Math.max(0, Math.round((Date.now() - d.getTime()) / 1000));
  if (s < 5) return "agora";
  if (s < 60) return `há ${s} s`;
  if (s < 3600) return `há ${Math.floor(s / 60)} min`;
  if (s < 86400) return `há ${Math.floor(s / 3600)} h`;
  return `há ${Math.floor(s / 86400)} d`;
}

function hora(iso, comData = false) {
  const d = data(iso);
  if (!d) return "—";
  const hoje = new Date().toDateString() === d.toDateString();
  const h = d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  return hoje && !comData ? h : `${d.toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" })} ${h}`;
}

function completa(iso) { const d = data(iso); return d ? d.toLocaleString("pt-BR") : "—"; }

function duracao(s) {
  if (s == null) return "—";
  s = Math.round(s);
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${s % 60} s`;
  return `${Math.floor(s / 3600)} h ${Math.floor(s % 3600 / 60)} min`;
}

// Repositório: o backend só guarda URL http(s). No GitHub, branch e commit também viram links.
function repo(s) {
  const gh = /^https:\/\/github\.com\/[^/]+\/[^/]+$/i.test(s.repositorio || "");
  const sha = s.commit && /^[0-9a-f]{7,64}$/i.test(s.commit) ? s.commit : null;
  const caminho = b => b.split("/").map(encodeURIComponent).join("/");
  return {
    url: s.repositorio || null,
    nome: (s.repositorio || "").replace(/^https?:\/\/(www\.)?/i, "").replace(/^github\.com\//i, ""),
    branchUrl: gh && s.branch ? `${s.repositorio}/tree/${caminho(s.branch)}` : null,
    commitUrl: gh && sha ? `${s.repositorio}/commit/${sha}` : null,
    commitCurto: sha ? sha.slice(0, 7) : s.commit,
  };
}

function link(url, texto, cls = "") {
  return url ? `<a class="link ${cls}" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(texto)}</a>` : `<span class="${cls}">${esc(texto)}</span>`;
}

// "branch @ commit" (o que está rodando agora)
function rodando(s) {
  const r = repo(s);
  return [s.branch && link(r.branchUrl, s.branch, "mono"), s.commit && link(r.commitUrl, r.commitCurto, "mono")]
    .filter(Boolean).join(` <span class="fraco">@</span> `);
}

function linhaRepo(s) {
  if (!s.repositorio && !s.branch && !s.commit) return "";
  const r = repo(s);
  const versao = rodando(s);
  return `<div class="repo-card" title="Repositório · branch @ commit rodando">⎇ ${r.url ? link(r.url, r.nome) : ""}${r.url && versao ? ` <span class="fraco">·</span> ` : ""}${versao}</div>`;
}

const NOMES_STATUS = { online: "Online", degradado: "Degradado", offline: "Offline", desconhecido: "Sem heartbeat", pausado: "Pausado" };
const NOMES_JOB = { fila: "Na fila", rodando: "Rodando", ok: "OK", erro: "Erro", interrompido: "Interrompido" };

function pilula(status) {
  return `<span class="pilula st-${esc(status)}"><span class="ponto vivo"></span>${esc(NOMES_STATUS[status] || status)}</span>`;
}

function toast(texto, cor = "var(--azul)") {
  const el = document.createElement("div");
  el.className = "toast";
  el.style.setProperty("--c", cor);
  el.innerHTML = texto;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 7000);
}

async function api(caminho, opcoes = {}) {
  const headers = {};
  if (opcoes.body) headers["Content-Type"] = "application/json";
  if (opcoes.method && opcoes.method !== "GET") headers["X-Web"] = "1";       // proteção contra CSRF
  const r = await fetch(caminho, {
    credentials: "same-origin",
    ...opcoes,
    headers,
    body: opcoes.body ? JSON.stringify(opcoes.body) : undefined,
  });
  const j = await r.json().catch(() => ({}));
  const publica = caminho.startsWith("/api/auth/login") || caminho.startsWith("/api/auth/cadastro");
  if (!publica && (r.status === 401 || (r.status === 403 && SAI_DO_PAINEL.includes(j.codigo)))) {
    mostrarLogin(r.status === 401 && j.codigo === "nao_autenticado" ? "" : j.error);
    const e = new Error(j.error || "sessão expirada"); e.codigo = j.codigo; throw e;
  }
  if (!r.ok) {
    const msg = Array.isArray(j.detalhes) ? `${j.error}: ${j.detalhes.join("; ")}` : (j.error || `HTTP ${r.status}`);
    const e = new Error(msg); e.codigo = j.codigo; e.status = r.status; e.dados = j; throw e;
  }
  return j;
}

function qs(params) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== "" && v != null) p.set(k, v);
  const s = p.toString();
  return s ? `?${s}` : "";
}

// ── Login ───────────────────────────────────────────────────────────────────

function mostrarLogin(mensagem) {
  if (estado.usuario) atividade.sair(false);
  estado.usuario = null;
  pararTimers();
  $("#tela-app").hidden = true;
  $("#tela-login").hidden = false;
  trocarLogin("entrar");
  fecharGaveta();
  const erro = $("#login-erro");
  erro.textContent = mensagem || "";
  erro.hidden = !mensagem;
  try { $("#email").value = $("#email").value || localStorage.getItem("aplogs_email") || ""; } catch { /* sem storage */ }
  setTimeout(() => ($("#email").value ? $("#senha") : $("#email")).focus(), 50);
}

function mostrarApp(usuario) {
  estado.usuario = usuario;
  try { localStorage.setItem("aplogs_email", usuario.email); } catch { /* sem storage */ }
  const papel = usuario.papel
    ? `<span class="papel" title="${usuario.admin ? "Administrador do apusers: vale como gestor" : "Papel na aplogs (definido no apusers)"}">${esc(usuario.papel)}${usuario.admin ? " · admin" : ""}</span>` : "";
  $("#quem").innerHTML = `<b>${esc(usuario.nome)}</b>${papel}`;
  $("#quem").title = usuario.email;
  // quem entra começa em Serviços (a aba anterior pode ter sido de outra pessoa)
  estado.aba = "servicos";
  $$(".abas button").forEach(b => b.classList.toggle("ativa", b.dataset.aba === "servicos"));
  $$(".aba").forEach(s => { s.hidden = s.id !== "aba-servicos"; });
  $("#abrir-integracao").classList.toggle("bloqueado", !pode("gerenciar"));
  $("#tela-login").hidden = true;
  $("#tela-app").hidden = false;
  iniciarTimers();
  atividade.entrou();
}

function trocarLogin(qual) {
  $("#form-login").hidden = qual !== "entrar";
  $("#form-cadastro").hidden = qual !== "cadastro";
}

$("#ir-cadastro").addEventListener("click", e => { e.preventDefault(); trocarLogin("cadastro"); $("#form-cadastro").nome.focus(); });
$("#ir-login").addEventListener("click", e => { e.preventDefault(); trocarLogin("entrar"); });

$("#form-login").addEventListener("submit", async e => {
  e.preventDefault();
  const botao = $("#form-login button");
  const erro = $("#login-erro");
  botao.disabled = true;
  erro.hidden = true;
  try {
    const r = await api("/api/auth/login", { method: "POST", body: { email: $("#email").value, senha: $("#senha").value } });
    $("#senha").value = "";
    mostrarApp(r.usuario);
  } catch (err) {
    $("#senha").value = "";
    erro.textContent = err.codigo === "acesso_restrito" && err.dados?.pedido
      ? "Você ainda não tem acesso à aplogs. O pedido foi enviado ao administrador do apusers."
      : err.message;
    erro.hidden = false;
  } finally {
    botao.disabled = false;
  }
});

$("#form-cadastro").addEventListener("submit", async e => {
  e.preventDefault();
  const f = e.target, erro = $("#cadastro-erro"), ok = $("#cadastro-ok"), botao = f.querySelector("button");
  erro.hidden = ok.hidden = true;
  if (f.senha.value !== f.confirmar.value) { erro.textContent = "As senhas não conferem."; erro.hidden = false; return; }
  botao.disabled = true;
  try {
    const r = await api("/api/auth/cadastro", { method: "POST", body: {
      nome: f.nome.value.trim(), email: f.email.value.trim(), senha: f.senha.value,
      area: f.area.value.trim() || null, justificativa: f.justificativa.value.trim() || null } });
    f.reset();
    ok.textContent = r.message || "Cadastro enviado. Você entra quando o administrador aprovar.";
    ok.hidden = false;
  } catch (err) {
    erro.textContent = err.message;
    erro.hidden = false;
  } finally {
    botao.disabled = false;
  }
});

$("#sair").addEventListener("click", async () => {
  atividade.sair(true);
  estado.usuario = null;
  await fetch("/api/auth/logout", { method: "POST", headers: { "X-Web": "1" } }).catch(() => {});
  mostrarLogin();
});

// ── Tempo de tela e presença ────────────────────────────────────────────────
// A aba aberta é a "tela". A aplogs repassa ao apusers (POST /api/atividade, formato do apusers).
// Parado há 5 min (sem mouse/teclado) ou com a aba do navegador escondida: o tempo para de contar.
// Trechos longos são cortados a cada 5 min (não se perde muito se o navegador fechar de repente).

const NOME_ABA = { servicos: "Serviços", logs: "Logs", problemas: "Problemas", jobs: "Jobs", filas: "Filas" };

const atividade = (() => {
  const OCIOSO_MS = 5 * 60 * 1000, CORTE_MS = 5 * 60 * 1000, ENVIO_MS = 60 * 1000;
  const A = { tela: null, inicio: null, fila: [], ultimoInput: Date.now(), ocioso: false };
  const ligado = () => !!estado.usuario;

  function fechar(fim) {
    if (!A.tela || !A.inicio) return;
    fim = fim || new Date();
    const ms = fim - A.inicio;
    if (ms >= 1000) A.fila.push({ type: "tela", name: A.tela, started_at: A.inicio.toISOString(),
      ended_at: fim.toISOString(), duration_ms: ms, metadata: { app: "aplogs" } });
    A.inicio = null;
  }
  function abrir(nome) {
    fechar();
    A.tela = nome;
    if (ligado() && !document.hidden && !A.ocioso) A.inicio = new Date();
  }
  async function enviar(status, keepalive = false) {
    if (!ligado()) return;
    const events = A.fila.splice(0, 500);
    const corpo = JSON.stringify({ events, presence: status ? { screen: A.tela, status } : undefined });
    try {
      const r = await fetch("/api/atividade", { method: "POST", keepalive, credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Web": "1" }, body: corpo });
      if (!r.ok && r.status !== 401 && r.status !== 403) throw new Error(r.status);
    } catch { if (!keepalive) { A.fila.unshift(...events); A.fila.splice(2000); } }
  }
  function input() {
    A.ultimoInput = Date.now();
    if (A.ocioso && ligado()) { A.ocioso = false; abrir(A.tela); enviar("ativo"); }
  }
  ["mousemove", "keydown", "click", "scroll", "touchstart"].forEach(t => document.addEventListener(t, input, { passive: true }));
  setInterval(() => {
    if (!ligado()) return;
    const agora = Date.now();
    if (!A.ocioso && agora - A.ultimoInput > OCIOSO_MS) { fechar(new Date(A.ultimoInput)); A.ocioso = true; }
    else if (A.inicio && agora - A.inicio > CORTE_MS) abrir(A.tela);
    enviar(document.hidden || A.ocioso ? "ocioso" : "ativo");
  }, ENVIO_MS);
  document.addEventListener("visibilitychange", () => {
    if (!ligado()) return;
    if (document.hidden) { fechar(); enviar("ocioso"); } else { input(); abrir(A.tela); enviar("ativo"); }
  });
  window.addEventListener("pagehide", () => { if (ligado()) { fechar(); enviar("ocioso", true); } });

  return {
    tela(aba) { abrir(NOME_ABA[aba] || aba); },
    entrou() { A.ocioso = false; A.ultimoInput = Date.now(); abrir(NOME_ABA[estado.aba] || estado.aba); enviar("ativo"); },
    // avisar=true: ainda dá tempo de mandar (antes do logout); false: a sessão já caiu, só descarta
    sair(avisar) { fechar(); if (avisar) enviar("saiu", true); else A.fila.length = 0; A.tela = A.inicio = null; },
  };
})();

// ── Timers ──────────────────────────────────────────────────────────────────

function pararTimers() { estado.timers.forEach(clearInterval); estado.timers = []; }

function iniciarTimers() {
  pararTimers();
  atualizarPainel();
  atualizarAba(true);
  estado.timers.push(setInterval(atualizarPainel, ATUALIZA_MS));
  estado.timers.push(setInterval(() => { if (estado.aba !== "logs") atualizarAba(false); }, ATUALIZA_MS));
  estado.timers.push(setInterval(() => { if (estado.aba === "logs" && $("#logs-vivo").checked) carregarLogsNovos(); }, LOGS_VIVO_MS));
  estado.timers.push(setInterval(() => { $("#atualizado").textContent = estado.painel ? `atualizado ${relativo(estado.painel.agora)}` : ""; }, 1000));
}

// ── Abas ────────────────────────────────────────────────────────────────────

$$(".abas button").forEach(b => b.addEventListener("click", () => irPara(b.dataset.aba)));

function irPara(aba, filtros = {}) {
  if (estado.aba !== aba) atividade.tela(aba);
  estado.aba = aba;
  $$(".abas button").forEach(b => b.classList.toggle("ativa", b.dataset.aba === aba));
  $$(".aba").forEach(s => { s.hidden = s.id !== `aba-${aba}`; });
  for (const [id, v] of Object.entries(filtros)) { const el = $(id); if (el) el.value = v; }
  atualizarAba(true);
}

function atualizarAba(reset) {
  const f = { servicos: () => {}, logs: () => carregarLogs(reset), problemas: carregarProblemas, jobs: carregarJobs, filas: carregarFilas }[estado.aba];
  if (f) Promise.resolve(f()).catch(() => {});
}

// ── Painel (visão geral) ────────────────────────────────────────────────────

async function atualizarPainel() {
  let p;
  try { p = await api("/api/painel"); } catch { return; }
  estado.painel = p;
  avisarMudancas(p.servicos);
  renderResumo(p);
  renderServicos(p.servicos);
  preencherSelectServicos(p.servicos);
  if (estado.gaveta) atualizarGaveta();
}

function avisarMudancas(servicos) {
  const primeira = Object.keys(estado.statusAnterior).length === 0;
  for (const s of servicos) {
    const antes = estado.statusAnterior[s.slug];
    if (!primeira && antes && antes !== s.status) {
      if (s.status === "offline") toast(`<b>${esc(s.nome)}</b> ficou <b style="color:var(--vermelho)">offline</b>`, "var(--vermelho)");
      else if (antes === "offline") toast(`<b>${esc(s.nome)}</b> voltou a ficar online`, "var(--verde)");
      else if (s.status === "degradado") toast(`<b>${esc(s.nome)}</b> está degradado`, "var(--amarelo)");
    }
    estado.statusAnterior[s.slug] = s.status;
  }
}

function renderResumo(p) {
  const t = p.totais;
  const chips = [`<span class="chip"><b>${t.servicos}</b> serviço${t.servicos === 1 ? "" : "s"}</span>`];
  for (const st of ["online", "degradado", "offline", "desconhecido", "pausado"]) {
    if (t[st]) chips.push(`<span class="chip st-${st}"><span class="ponto"></span><b>${t[st]}</b> ${NOMES_STATUS[st].toLowerCase()}</span>`);
  }
  if (p.problemas_abertos) chips.push(`<span class="chip" style="cursor:pointer" onclick="irPara('problemas')">⚠ <b>${p.problemas_abertos}</b> problema${p.problemas_abertos === 1 ? "" : "s"} aberto${p.problemas_abertos === 1 ? "" : "s"}</span>`);
  $("#resumo").innerHTML = chips.join("");
  const badge = $("#badge-problemas");
  badge.hidden = !p.problemas_abertos;
  badge.textContent = p.problemas_abertos;
  const off = t.offline || 0;
  document.title = off ? `(${off} offline) aplogs` : "aplogs";
}

function linhaTempo(barras) {
  if (!barras || !barras.length) return "";
  return `<div class="linha-tempo" title="Heartbeats da última hora (1 barra = 1 min)">${barras.map(b => `<i class="${b === 1 ? "" : b === 0 ? "f" : "n"}"></i>`).join("")}</div>
          <div class="linha-tempo-legenda"><span>60 min</span><span>agora</span></div>`;
}

function chavesDados(dados) {
  if (!dados || typeof dados !== "object") return "";
  const itens = [];
  for (const [k, v] of Object.entries(dados)) {
    if (v == null) continue;
    if (typeof v === "object" && !Array.isArray(v)) {
      for (const [k2, v2] of Object.entries(v)) {
        if (v2 == null || typeof v2 === "object") continue;
        itens.push([`${k}.${k2}`, v2]);
      }
    } else if (typeof v !== "object") itens.push([k, v]);
    if (itens.length >= 8) break;
  }
  return itens.length ? `<div class="dados-chaves">${itens.slice(0, 8).map(([k, v]) => `<span>${esc(k)}: <b>${esc(v)}</b></span>`).join("")}</div>` : "";
}

function cardServico(s) {
  const prob = s.problemas.total;
  const pend = s.filas.reduce((a, f) => a + f.pendentes, 0);
  const msg = s.status === "offline"
    ? `Sem heartbeat ${relativo(s.ultimo_heartbeat_em)}${s.status_reportado === "parando" ? " (parado normalmente)" : ""}`
    : (s.mensagem && s.status !== "online" ? s.mensagem : "");
  return `
  <article class="card st-${esc(s.status)}" data-slug="${esc(s.slug)}">
    <div class="card-topo">
      <div><h3>${esc(s.nome)}</h3><div class="slug">${esc(s.slug)}</div></div>
      ${pilula(s.status)}
    </div>
    ${msg ? `<p class="msg">${esc(msg)}</p>` : ""}
    <div class="metricas">
      <div class="metrica ${prob ? (s.problemas.critico || s.problemas.erro ? "ruim" : "atencao") : ""}"><div class="v">${prob}</div><div class="r">problemas</div></div>
      <div class="metrica ${s.erros_1h ? "ruim" : ""}"><div class="v">${s.erros_1h}</div><div class="r">erros (1 h)</div></div>
      <div class="metrica"><div class="v">${s.jobs_rodando}${pend ? ` <span class="fraco" style="font-size:12px">/ ${pend}</span>` : ""}</div><div class="r">jobs rodando${pend ? " / fila" : ""}</div></div>
    </div>
    ${chavesDados(s.dados)}
    ${linhaTempo(s.timeline)}
    ${linhaRepo(s)}
    <div class="rodape-card">
      <span>♥ ${relativo(s.ultimo_heartbeat_em)}</span>
      <span>${[s.versao && `v${esc(s.versao)}`, s.host && esc(s.host)].filter(Boolean).join(" · ")}</span>
    </div>
  </article>`;
}

function renderServicos(servicos) {
  $("#vazio-servicos").hidden = servicos.length > 0;
  const grupos = {};
  for (const s of servicos) (grupos[s.grupo || ""] ||= []).push(s);
  const ordem = Object.keys(grupos).sort((a, b) => (a === "") - (b === "") || a.localeCompare(b));
  const usarTitulos = ordem.length > 1 || ordem[0] !== "";
  $("#grade-servicos").innerHTML = ordem.map(g =>
    (usarTitulos ? `<div class="grupo-titulo">${esc(g || "Outros")}</div>` : "") + grupos[g].map(cardServico).join("")
  ).join("");
}

$("#grade-servicos").addEventListener("click", e => {
  if (e.target.closest("a")) return;          // link do repositório: abre o GitHub, não a gaveta
  const card = e.target.closest(".card");
  if (card) abrirGaveta(card.dataset.slug);
});

function preencherSelectServicos(servicos) {
  for (const sel of $$(".sel-servico")) {
    const atual = sel.value;
    const opcoes = `<option value="">Todos os serviços</option>` + servicos.map(s => `<option value="${esc(s.slug)}">${esc(s.nome)}</option>`).join("");
    if (sel.dataset.assinatura !== opcoes) {
      sel.innerHTML = opcoes;
      sel.dataset.assinatura = opcoes;
      sel.value = atual;
    }
  }
}

function nomeServico(slug) {
  const s = estado.painel?.servicos.find(x => x.slug === slug);
  return s ? s.nome : slug;
}

// ── Logs ────────────────────────────────────────────────────────────────────

function filtrosLogs() {
  return { servico: $("#logs-servico").value, nivel: $("#logs-nivel").value, modulo: $("#logs-modulo").value, q: $("#logs-busca").value.trim() };
}

async function carregarModulos() {
  try {
    const r = await api("/api/logs/modulos" + qs({ servico: $("#logs-servico").value }));
    const sel = $("#logs-modulo");
    const atual = sel.value;
    sel.innerHTML = `<option value="">Todos os módulos</option>` + r.modulos.map(m => `<option>${esc(m)}</option>`).join("");
    sel.value = r.modulos.includes(atual) ? atual : "";
  } catch {}
}

async function carregarLogs(reset = true) {
  if (!reset && estado.logs.itens.length) return;
  carregarModulos();
  const r = await api("/api/logs" + qs({ ...filtrosLogs(), limite: 200 }));
  estado.logs = { itens: r.logs, ultimoId: r.logs.length ? r.logs[0].id : r.ultimo_id, acabou: r.logs.length < 200 };
  $("#logs-corpo").innerHTML = r.logs.map(l => linhaLog(l)).join("");
  $("#logs-vazio").hidden = r.logs.length > 0;
  $("#logs-mais").hidden = estado.logs.acabou;
}

async function carregarLogsNovos() {
  let r;
  try { r = await api("/api/logs" + qs({ ...filtrosLogs(), depois: estado.logs.ultimoId, limite: 500 })); } catch { return; }
  if (!r.logs.length) return;
  estado.logs.ultimoId = r.logs[0].id;
  estado.logs.itens = r.logs.concat(estado.logs.itens).slice(0, 3000);
  $("#logs-corpo").insertAdjacentHTML("afterbegin", r.logs.map(l => linhaLog(l, true)).join(""));
  const linhas = $("#logs-corpo").children;
  while (linhas.length > 3000) linhas[linhas.length - 1].remove();
  $("#logs-vazio").hidden = true;
}

async function carregarLogsAntigos() {
  const ultimo = estado.logs.itens[estado.logs.itens.length - 1];
  if (!ultimo) return;
  const r = await api("/api/logs" + qs({ ...filtrosLogs(), antes: ultimo.id, limite: 300 }));
  estado.logs.itens = estado.logs.itens.concat(r.logs);
  estado.logs.acabou = r.logs.length < 300;
  $("#logs-corpo").insertAdjacentHTML("beforeend", r.logs.map(l => linhaLog(l)).join(""));
  $("#logs-mais").hidden = estado.logs.acabou;
}

function linhaLog(l, novo = false) {
  const temExtra = l.exc || l.contexto;
  return `<tr class="nivel-${esc(l.nivel)} ${temExtra ? "expandivel" : ""} ${novo ? "novo" : ""}" data-id="${l.id}">
    <td class="col-hora" title="${esc(completa(l.ts))}">${esc(hora(l.ts))}</td>
    <td><span class="tag-servico" title="${esc(nomeServico(l.servico))}">${esc(l.servico)}</span></td>
    <td><span class="nivel ${esc(l.nivel)}">${esc(l.nivel)}</span></td>
    <td class="nowrap fraco">${esc(l.modulo || "")}</td>
    <td class="msg">${esc(l.mensagem)}${temExtra ? ` <span class="fraco">[+]</span>` : ""}<div class="extra" hidden>${l.exc ? esc(l.exc) : ""}${l.exc && l.contexto ? "\n\n" : ""}${l.contexto ? esc(JSON.stringify(l.contexto, null, 2)) : ""}</div></td>
  </tr>`;
}

$("#logs-corpo").addEventListener("click", e => {
  const tr = e.target.closest("tr.expandivel");
  if (!tr || e.target.closest(".extra")) return;
  const extra = $(".extra", tr);
  extra.hidden = !extra.hidden;
});

let tempoBusca;
["#logs-servico", "#logs-nivel", "#logs-modulo"].forEach(id => $(id).addEventListener("change", () => carregarLogs(true)));
$("#logs-busca").addEventListener("input", () => { clearTimeout(tempoBusca); tempoBusca = setTimeout(() => carregarLogs(true), 350); });
$("#logs-mais").addEventListener("click", carregarLogsAntigos);

// ── Problemas ───────────────────────────────────────────────────────────────

async function carregarProblemas() {
  const r = await api("/api/problemas" + qs({ servico: $("#prob-servico").value, status: estado.probStatus }));
  $("#prob-vazio").hidden = r.problemas.length > 0;
  $("#lista-problemas").innerHTML = r.problemas.map(cardProblema).join("");
}

function cardProblema(p) {
  const resolvido = p.status === "resolvido";
  return `<div class="problema sev-${esc(p.severidade)} ${resolvido ? "resolvido" : ""}">
    <div class="sev">${esc(p.severidade)}</div>
    <div>
      <h4>${esc(p.titulo)}</h4>
      ${p.descricao ? `<div class="desc">${esc(p.descricao)}</div>` : ""}
      <div class="meta">
        <span class="tag-servico">${esc(nomeServico(p.servico))}</span>
        <span>desde ${esc(hora(p.primeira_vez, true))}</span>
        ${p.ocorrencias > 1 ? `<span>${p.ocorrencias}× · última ${esc(relativo(p.ultima_vez))}</span>` : `<span>visto ${esc(relativo(p.ultima_vez))}</span>`}
        <span class="mono">${esc(p.chave)}</span>
        ${p.status === "reconhecido" ? `<span style="color:var(--azul)">reconhecido</span>` : ""}
        ${resolvido ? `<span>resolvido ${esc(relativo(p.resolvido_em))} (${esc(p.resolvido_por || "")})</span>` : ""}
      </div>
    </div>
    <div class="acoes">
      ${p.status === "aberto" && pode("tratar") ? `<button class="fantasma" data-acao="reconhecer" data-id="${p.id}">Reconhecer</button>` : ""}
      ${!resolvido && pode("tratar") ? `<button class="fantasma" data-acao="resolver" data-id="${p.id}">Resolver</button>` : ""}
    </div>
  </div>`;
}

document.addEventListener("click", async e => {
  const b = e.target.closest("[data-acao]");
  if (!b) return;
  b.disabled = true;
  try {
    await api(`/api/problemas/${b.dataset.id}/${b.dataset.acao}`, { method: "POST" });
    carregarProblemas();
    atualizarPainel();
  } catch (err) { toast(esc(err.message), "var(--vermelho)"); b.disabled = false; }
});

$("#prob-servico").addEventListener("change", carregarProblemas);
$$("#prob-status button").forEach(b => b.addEventListener("click", () => {
  estado.probStatus = b.dataset.v;
  $$("#prob-status button").forEach(x => x.classList.toggle("ativo", x === b));
  carregarProblemas();
}));

// ── Jobs ────────────────────────────────────────────────────────────────────

async function carregarJobs() {
  const r = await api("/api/jobs" + qs({ servico: $("#jobs-servico").value, status: $("#jobs-status").value, tipo: $("#jobs-tipo").value, q: $("#jobs-busca").value.trim() }));
  const selTipo = $("#jobs-tipo");
  const opcoes = `<option value="">Todos os tipos</option>` + r.tipos.map(t => `<option>${esc(t)}</option>`).join("");
  if (selTipo.dataset.assinatura !== opcoes) { const v = selTipo.value; selTipo.innerHTML = opcoes; selTipo.dataset.assinatura = opcoes; selTipo.value = v; }
  $("#jobs-vazio").hidden = r.jobs.length > 0;
  $("#jobs-corpo").innerHTML = r.jobs.map(linhaJob).join("");
}

function linhaJob(j) {
  const prog = j.progresso != null ? Math.round(j.progresso) : (j.status === "ok" ? 100 : null);
  const dur = j.duracao_s ?? (j.status === "rodando" && j.iniciado_em ? (Date.now() - data(j.iniciado_em)) / 1000 : null);
  return `<tr class="js-${esc(j.status)}">
    <td><span class="pilula js-${esc(j.status)}">${esc(NOMES_JOB[j.status] || j.status)}</span></td>
    <td><span class="tag-servico" title="${esc(nomeServico(j.servico))}">${esc(j.servico)}</span></td>
    <td><div class="job-nome">${esc(j.nome || j.externo_id)}</div>
        <div class="job-sub">${[j.tipo, j.etapa, j.mensagem].filter(Boolean).map(esc).join(" · ")}</div></td>
    <td>${prog != null ? `<div class="barra"><i style="width:${prog}%"></i></div><div class="job-sub">${prog}%</div>` : `<span class="fraco">—</span>`}</td>
    <td class="nowrap" title="${esc(completa(j.iniciado_em || j.criado_em))}">${esc(hora(j.iniciado_em || j.criado_em))}</td>
    <td class="nowrap">${esc(duracao(dur))}</td>
  </tr>`;
}

["#jobs-servico", "#jobs-status", "#jobs-tipo"].forEach(id => $(id).addEventListener("change", carregarJobs));
let tempoJobs;
$("#jobs-busca").addEventListener("input", () => { clearTimeout(tempoJobs); tempoJobs = setTimeout(carregarJobs, 350); });

// ── Filas ───────────────────────────────────────────────────────────────────

async function carregarFilas() {
  const r = await api("/api/filas");
  $("#filas-vazio").hidden = r.filas.length > 0;
  $("#grade-filas").innerHTML = r.filas.map(cardFila).join("");
}

function sparkline(amostras) {
  if (!amostras || amostras.length < 2) return `<div class="fraco" style="font-size:12px;margin-top:12px">Gráfico aparece com mais amostras…</div>`;
  const W = 300, H = 54, P = 3;
  const max = Math.max(1, ...amostras.map(a => a.pendentes + a.rodando));
  const t0 = data(amostras[0].ts).getTime(), t1 = data(amostras[amostras.length - 1].ts).getTime() || t0 + 1;
  const x = a => P + (data(a.ts).getTime() - t0) / Math.max(1, t1 - t0) * (W - 2 * P);
  const y = v => H - P - v / max * (H - 2 * P);
  const linha = amostras.map((a, i) => `${i ? "L" : "M"}${x(a).toFixed(1)},${y(a.pendentes + a.rodando).toFixed(1)}`).join(" ");
  const area = `${linha} L${x(amostras[amostras.length - 1]).toFixed(1)},${H - P} L${x(amostras[0]).toFixed(1)},${H - P} Z`;
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Itens na fila na última hora">
    <path d="${area}" fill="rgba(96,165,250,.14)"/><path d="${linha}" fill="none" stroke="var(--azul)" stroke-width="1.6" vector-effect="non-scaling-stroke"/>
  </svg><div class="linha-tempo-legenda"><span>${esc(hora(amostras[0].ts))}</span><span>máx ${max}</span><span>agora</span></div>`;
}

function cardFila(f) {
  return `<div class="fila-card">
    <div class="card-topo"><div><h4>${esc(f.nome)}</h4><div class="tag-servico">${esc(nomeServico(f.servico))}</div></div>
      <span class="fraco" style="font-size:12px">${esc(relativo(f.atualizado_em))}</span></div>
    <div class="fila-nums">
      <div class="metrica ${f.pendentes ? "atencao" : ""}"><div class="v">${f.pendentes}</div><div class="r">na fila</div></div>
      <div class="metrica"><div class="v">${f.rodando}${f.workers != null ? `<span class="fraco" style="font-size:12px">/${f.workers}</span>` : ""}</div><div class="r">rodando</div></div>
      <div class="metrica"><div class="v">${f.concluidos}</div><div class="r">concluídos</div></div>
      <div class="metrica ${f.erros ? "ruim" : ""}"><div class="v">${f.erros}</div><div class="r">erros</div></div>
    </div>
    ${sparkline(f.amostras)}
  </div>`;
}

// ── Gaveta: detalhe do serviço ──────────────────────────────────────────────

function abrirGaveta(slug) {
  estado.gaveta = slug;
  $("#gaveta").hidden = false;
  $("#gaveta-fundo").hidden = false;
  $("#gaveta").innerHTML = `<p class="mudo">Carregando…</p>`;
  atualizarGaveta(true);
}

function fecharGaveta() {
  estado.gaveta = null;
  estado.integracao = false;
  $("#gaveta").hidden = true;
  $("#gaveta-fundo").hidden = true;
}

$("#gaveta-fundo").addEventListener("click", fecharGaveta);
document.addEventListener("keydown", e => { if (e.key === "Escape") fecharGaveta(); });

async function atualizarGaveta(completo = false) {
  const slug = estado.gaveta;
  if (!slug) return;
  // não redesenha enquanto o usuário edita
  if (!completo && $("#gaveta").contains(document.activeElement) && document.activeElement.matches("input, textarea")) return;
  let r;
  try { r = await api(`/api/servicos/${encodeURIComponent(slug)}`); } catch (err) { $("#gaveta").innerHTML = `<p class="erro">${esc(err.message)}</p>`; return; }
  if (estado.gaveta !== slug) return;
  const s = r.servico;
  const rolagem = $("#gaveta").scrollTop;
  $("#gaveta").innerHTML = `
    <div class="gaveta-topo">
      <div><h2>${esc(s.nome)}</h2><div class="slug mono fraco">${esc(s.slug)}</div></div>
      <div style="display:flex;gap:8px;align-items:center">${pilula(s.status)}
        ${pode("gerenciar") ? `<button class="fantasma perigo" id="g-excluir" title="Apagar este serviço e todos os dados dele">🗑 Excluir</button>` : ""}
        <button class="fantasma" id="g-fechar">✕</button></div>
    </div>
    ${s.descricao ? `<p class="mudo">${esc(s.descricao)}</p>` : ""}
    ${linhaTempo(s.timeline)}

    <h5>Informações</h5>
    <dl class="info">
      <dt>Status</dt><dd>${esc(NOMES_STATUS[s.status] || s.status)} desde ${esc(completa(s.status_desde))}</dd>
      <dt>Status reportado</dt><dd>${esc(s.status_reportado || "—")}${s.mensagem ? ` — ${esc(s.mensagem)}` : ""}</dd>
      <dt>Último heartbeat</dt><dd>${esc(completa(s.ultimo_heartbeat_em))} (${esc(relativo(s.ultimo_heartbeat_em))})</dd>
      <dt>Heartbeat</dt><dd>a cada ${s.intervalo_s} s · offline após ${duracao(s.tolerancia_efetiva_s)} sem sinal</dd>
      <dt>Processo iniciado</dt><dd>${esc(completa(s.iniciado_em))}</dd>
      <dt>Versão</dt><dd>${esc(s.versao || "—")}</dd>
      <dt>Repositório</dt><dd>${s.repositorio ? link(s.repositorio, repo(s).nome) : `<span class="fraco">— (preencha em Configuração, abaixo)</span>`}</dd>
      <dt>Rodando</dt><dd>${rodando(s) || `<span class="fraco">— (o serviço não mandou branch/commit)</span>`}</dd>
      <dt>Host</dt><dd>${esc(s.host || "—")}${s.ambiente ? ` · ${esc(s.ambiente)}` : ""}</dd>
      <dt>Conectado em</dt><dd>${esc(completa(s.criado_em))}</dd>
    </dl>

    <h5>Problemas abertos (${r.problemas.length})</h5>
    ${r.problemas.length ? `<div class="lista">${r.problemas.map(cardProblema).join("")}</div>` : `<p class="mudo">Nenhum.</p>`}

    ${r.filas.length ? `<h5>Filas</h5><div class="mini-lista">${r.filas.map(f => `<div class="mini-item"><span>${esc(f.nome)}</span><span class="mudo">${f.pendentes} na fila · ${f.rodando} rodando · ${f.erros} erros</span></div>`).join("")}</div>` : ""}

    <h5>Jobs recentes</h5>
    ${r.jobs.length ? `<div class="mini-lista">${r.jobs.slice(0, 10).map(j => `<div class="mini-item js-${esc(j.status)}"><span><span class="pilula js-${esc(j.status)}">${esc(NOMES_JOB[j.status] || j.status)}</span> ${esc(j.nome || j.externo_id)}</span><span class="mudo nowrap">${esc(hora(j.iniciado_em || j.atualizado_em))}</span></div>`).join("")}</div>
      <button class="fantasma largo" id="g-jobs">Ver todos os jobs</button>` : `<p class="mudo">Nenhum.</p>`}

    <button class="fantasma largo" id="g-logs">Ver logs deste serviço</button>

    ${s.dados ? `<h5>Dados do último heartbeat</h5><pre class="json">${esc(JSON.stringify(s.dados, null, 2))}</pre>` : ""}

    <h5>Configuração</h5>
    ${pode("gerenciar") ? "" : `<p class="so-leitura">Só leitura: editar e apagar serviços é do papel gestor no apusers.</p>`}
    <form class="form-ed" id="g-form">
      <fieldset style="all:unset;display:contents" ${pode("gerenciar") ? "" : "disabled"}>
      <label>Nome<input name="nome" value="${esc(s.nome)}" maxlength="160"></label>
      <label>Descrição<textarea name="descricao" rows="2">${esc(s.descricao || "")}</textarea></label>
      <label>Grupo (agrupa os cards)<input name="grupo" value="${esc(s.grupo || "")}" maxlength="80" placeholder="ex.: Receita, RPA, APIs"></label>
      <label>Repositório no GitHub<input name="repositorio" value="${esc(s.repositorio || "")}" maxlength="300" placeholder="ex.: Grupo-Studio/aplogin ou a URL do repositório"><span class="dica">Se o serviço manda o repositório no heartbeat (APLOGS_REPOSITORIO), vale o dele.</span></label>
      <label>Tolerância sem heartbeat, em segundos (vazio = automática: ${Math.max(3 * s.intervalo_s, 60)} s)
        <input name="tolerancia_s" type="number" min="0" value="${s.tolerancia_s ?? ""}"></label>
      <label class="chave"><input name="monitorar" type="checkbox" ${s.monitorar ? "checked" : ""}> Monitorar (desligado = sem alerta de offline)</label>
      <div class="linha" ${pode("gerenciar") ? "" : "hidden"}>
        <button class="botao" type="submit">Salvar</button>
        <button class="fantasma" type="button" id="g-integracao" title="Gera o .env e as instruções deste serviço">Gerenciar integração</button>
        <span style="flex:1"></span>
        <button class="fantasma perigo" type="button" id="g-apagar">Apagar serviço</button>
      </div>
      </fieldset>
    </form>`;
  if (!completo) $("#gaveta").scrollTop = rolagem;

  $("#g-fechar").onclick = fecharGaveta;
  $("#g-logs").onclick = () => { fecharGaveta(); irPara("logs", { "#logs-servico": s.slug, "#logs-modulo": "" }); };
  const bj = $("#g-jobs"); if (bj) bj.onclick = () => { fecharGaveta(); irPara("jobs", { "#jobs-servico": s.slug }); };
  $("#g-form").onsubmit = async e => {
    e.preventDefault();
    const f = e.target;
    const tol = f.tolerancia_s.value.trim();
    try {
      await api(`/api/servicos/${encodeURIComponent(s.slug)}`, { method: "PATCH", body: {
        nome: f.nome.value.trim(), descricao: f.descricao.value, grupo: f.grupo.value,
        repositorio: f.repositorio.value.trim(),
        tolerancia_s: tol === "" ? 0 : Number(tol), monitorar: f.monitorar.checked } });
      toast("Salvo.", "var(--verde)");
      document.activeElement.blur();
      atualizarPainel();
      atualizarGaveta(true);
    } catch (err) { toast(esc(err.message), "var(--vermelho)"); }
  };
  $("#g-integracao").onclick = () => abrirIntegracao(s);
  $("#g-apagar").onclick = () => excluirServico(s);
  const bx = $("#g-excluir"); if (bx) bx.onclick = () => excluirServico(s);
}

async function excluirServico(s) {
  const vivo = ["online", "degradado"].includes(s.status);
  const aviso = vivo
    ? `\n\nATENÇÃO: "${s.nome}" está ${NOMES_STATUS[s.status].toLowerCase()} e continua mandando dados: ele volta a aparecer no próximo heartbeat. ` +
      `Desligue o serviço (ou tire o APLOGS_* do .env dele) antes, ou use "Monitorar" desligado para só silenciar.`
    : "\n\nSe o serviço voltar a mandar dados, ele aparece de novo.";
  if (!confirm(`Excluir "${s.nome}" (${s.slug}) e todos os logs, jobs, filas e problemas dele?\n\nNão dá para desfazer.${aviso}`)) return;
  try {
    await api(`/api/servicos/${encodeURIComponent(s.slug)}`, { method: "DELETE" });
    toast(`"${esc(s.nome)}" excluído.`, "var(--verde)");
    fecharGaveta();
    atualizarPainel();
  } catch (err) { toast(esc(err.message), "var(--vermelho)"); }
}

// ── Gerenciar integração ────────────────────────────────────────────────────
// A pessoa diz o que é o serviço; a aplogs cadastra e devolve o bloco do .env (com a chave de envio)
// e um .txt com o passo a passo. Só para quem pode "gerenciar" (a API confere).

function slugDe(texto) {
  return (texto || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-").replace(/^[-._]+|-+$/g, "").slice(0, 80);
}

async function copiarTexto(texto, campo) {
  try { await navigator.clipboard.writeText(texto); return true; } catch { /* http sem HTTPS: sem clipboard API */ }
  campo.focus();
  campo.select();
  try { return document.execCommand("copy"); } catch { return false; }
}

function baixarTexto(nome, texto) {
  const url = URL.createObjectURL(new Blob(["\ufeff" + texto], { type: "text/plain;charset=utf-8" }));
  const a = Object.assign(document.createElement("a"), { href: url, download: nome });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

$("#abrir-integracao").addEventListener("click", () => {
  if (!pode("gerenciar")) {
    toast(`Gerar a integração é do papel <b>gestor</b> (o .env leva a chave de envio). Seu papel: ${esc(estado.usuario?.papel || "—")}. ` +
      "Peça ao administrador do apusers.", "var(--amarelo)");
    return;
  }
  abrirIntegracao();
});

async function abrirIntegracao(s = null) {
  estado.gaveta = null;
  estado.integracao = true;
  $("#gaveta").hidden = false;
  $("#gaveta-fundo").hidden = false;
  $("#gaveta").scrollTop = 0;
  $("#gaveta").innerHTML = `<p class="mudo">Carregando…</p>`;
  let sug;
  try { sug = await api("/api/integracao"); } catch (err) {
    $("#gaveta").innerHTML = `<p class="erro">${esc(err.message)}</p>`;
    return;
  }
  if (!estado.integracao) return;
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);
  $("#gaveta").innerHTML = `
    <div class="gaveta-topo">
      <div><h2>Gerenciar integração</h2><div class="mudo">${s ? esc(s.nome) : "Conectar um serviço à aplogs"}</div></div>
      <button class="fantasma" id="i-fechar">✕</button>
    </div>
    <p class="mudo">Diga o que é o serviço. A aplogs já deixa ele cadastrado e gera o bloco para colar no
      <code>.env</code> dele, mais um <code>.txt</code> com o passo a passo.</p>
    <form class="form-ed" id="i-form" autocomplete="off">
      <label>Nome do serviço *<input name="nome" required maxlength="160" value="${esc(s?.nome || "")}" placeholder="ex.: Robô DCTF"></label>
      <label>Identificador *<input name="servico" class="mono" required maxlength="80" value="${esc(s?.slug || "")}"
        placeholder="ex.: robo-dctf" ${s ? "readonly" : ""}>
        <span class="dica">${s ? "O identificador não muda (é como o serviço se apresenta)." : "Minúsculas, números, - _ . (sai do nome sozinho)."}</span></label>
      <label>Grupo (agrupa os cards)<input name="grupo" maxlength="80" value="${esc(s?.grupo || "")}" placeholder="ex.: Receita, RPA, APIs"></label>
      <label>Descrição<textarea name="descricao" rows="2" maxlength="2000">${esc(s?.descricao || "")}</textarea></label>
      <label>Repositório no GitHub<input name="repositorio" maxlength="300" value="${esc(s?.repositorio || "")}" placeholder="ex.: Grupo-Studio/robo-dctf"></label>
      <label>Heartbeat a cada (segundos)<input name="intervalo_s" type="number" min="5" max="86400" required value="${s?.intervalo_s || sug.intervalo_padrao_s}">
        <span class="dica" id="i-tolerancia"></span></label>
      <label>Onde o serviço roda
        <select name="onde">
          <option value="rede">Em outra máquina (ou fora do Docker)</option>
          <option value="docker">Em um container nesta mesma máquina da aplogs</option>
        </select></label>
      <label id="i-url-campo">Endereço da aplogs, visto pelo serviço<input name="url" class="mono" maxlength="300" value="${esc(sug.url_rede)}">
        ${local ? `<span class="dica atencao">Você abriu o painel por ${esc(location.hostname)}: troque pelo IP da máquina da aplogs na rede.</span>` : ""}</label>
      <label>Linguagem do serviço
        <select name="linguagem">
          <option value="python">Python</option>
          <option value="node">Node.js</option>
          <option value="outro">Outra (curl / qualquer HTTP)</option>
        </select></label>
      <div class="linha"><button class="botao" type="submit">Gerar integração</button></div>
    </form>
    <div id="i-resultado"></div>`;

  const f = $("#i-form");
  const tolerancia = () => {
    const n = Number(f.intervalo_s.value) || sug.intervalo_padrao_s;
    $("#i-tolerancia").textContent = `Sem heartbeat por ${duracao(Math.max(3 * n, sug.tolerancia_minima_s))}, o serviço fica offline.`;
  };
  const onde = () => { $("#i-url-campo").hidden = f.onde.value !== "rede"; };
  tolerancia();
  onde();
  $("#i-fechar").onclick = fecharGaveta;
  f.intervalo_s.oninput = tolerancia;
  f.onde.onchange = onde;
  if (!s) {
    f.nome.oninput = () => { if (!f.servico.dataset.manual) f.servico.value = slugDe(f.nome.value); };
    f.servico.oninput = () => { f.servico.dataset.manual = f.servico.value ? "1" : ""; };
    f.servico.onblur = () => { f.servico.value = slugDe(f.servico.value); };
  }
  setTimeout(() => f.nome.focus(), 50);

  f.onsubmit = async e => {
    e.preventDefault();
    const botao = f.querySelector("button[type=submit]");
    botao.disabled = true;
    try {
      const r = await api("/api/integracao", { method: "POST", body: {
        servico: slugDe(f.servico.value), nome: f.nome.value.trim(), grupo: f.grupo.value.trim() || null,
        descricao: f.descricao.value.trim() || null, repositorio: f.repositorio.value.trim() || null,
        intervalo_s: Number(f.intervalo_s.value), onde: f.onde.value, linguagem: f.linguagem.value,
        url: f.url.value.trim() || null } });
      if (!estado.integracao) return;
      f.servico.value = r.servico.slug;
      mostrarResultadoIntegracao(r);
      atualizarPainel();
    } catch (err) {
      toast(esc(err.message), "var(--vermelho)");
    } finally {
      botao.disabled = false;
    }
  };
}

function mostrarResultadoIntegracao(r) {
  const linhas = r.env.trim().split("\n").length;
  $("#i-resultado").innerHTML = `
    <h5>Cole no .env do serviço</h5>
    <p class="mudo">${r.criado
      ? `Pronto: <b>${esc(r.servico.nome)}</b> já aparece em Serviços como “Sem heartbeat” até o primeiro envio.`
      : `<b>${esc(r.servico.nome)}</b> já existia: os dados foram atualizados.`}</p>
    <textarea class="codigo" id="i-env" readonly rows="${linhas}" spellcheck="false">${esc(r.env)}</textarea>
    <div class="integracao-acoes">
      <button class="botao" id="i-copiar">Copiar .env</button>
      <button class="fantasma" id="i-baixar">Baixar instruções (.txt)</button>
    </div>
    <p class="aviso-chave">⚠ Isso tem a chave de envio da aplogs: trate como senha e não suba o .env para o git.</p>
    <details><summary>Ver as instruções aqui</summary><pre class="json">${esc(r.txt)}</pre></details>`;
  $("#i-copiar").onclick = async () => {
    const ok = await copiarTexto(r.env, $("#i-env"));
    toast(ok ? "Copiado. Cole no .env do serviço." : "Não deu para copiar sozinho: o texto ficou selecionado, use Ctrl+C.",
      ok ? "var(--verde)" : "var(--amarelo)");
  };
  $("#i-baixar").onclick = () => baixarTexto(r.arquivo, r.txt);
  $("#i-resultado").scrollIntoView({ behavior: "smooth", block: "start" });
}

// ── Início ──────────────────────────────────────────────────────────────────

(async () => {
  try {
    const r = await api("/api/auth/sessao");
    if (!r.usuario) throw new Error("sem usuário");
    mostrarApp(r.usuario);
  } catch (err) {
    if ($("#tela-login").hidden) mostrarLogin(err.status === 503 ? err.message : "");
  }
})();
