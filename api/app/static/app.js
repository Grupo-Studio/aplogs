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
  timers: [],
};

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
  const r = await fetch(caminho, {
    credentials: "same-origin",
    headers: opcoes.body ? { "Content-Type": "application/json" } : {},
    ...opcoes,
    body: opcoes.body ? JSON.stringify(opcoes.body) : undefined,
  });
  if (r.status === 401 && !caminho.startsWith("/api/auth/login")) {
    mostrarLogin();
    throw new Error("sessão expirada");
  }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

function qs(params) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== "" && v != null) p.set(k, v);
  const s = p.toString();
  return s ? `?${s}` : "";
}

// ── Login ───────────────────────────────────────────────────────────────────

function mostrarLogin() {
  pararTimers();
  $("#tela-app").hidden = true;
  $("#tela-login").hidden = false;
  fecharGaveta();
  setTimeout(() => $("#senha").focus(), 50);
}

function mostrarApp() {
  $("#tela-login").hidden = true;
  $("#tela-app").hidden = false;
  iniciarTimers();
}

$("#form-login").addEventListener("submit", async e => {
  e.preventDefault();
  const botao = $("#form-login button");
  const erro = $("#login-erro");
  botao.disabled = true;
  erro.hidden = true;
  try {
    await api("/api/auth/login", { method: "POST", body: { secret: $("#senha").value } });
    $("#senha").value = "";
    mostrarApp();
  } catch (err) {
    erro.textContent = err.message;
    erro.hidden = false;
  } finally {
    botao.disabled = false;
  }
});

$("#sair").addEventListener("click", async () => {
  await fetch("/api/auth/logout", { method: "POST" }).catch(() => {});
  mostrarLogin();
});

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
  estado.aba = aba;
  $$(".abas button").forEach(b => b.classList.toggle("ativa", b.dataset.aba === aba));
  $$(".aba").forEach(s => { s.hidden = s.id !== `aba-${aba}`; });
  for (const [id, v] of Object.entries(filtros)) { const el = $(id); if (el) el.value = v; }
  atualizarAba(true);
}

function atualizarAba(reset) {
  const f = { servicos: () => {}, logs: () => carregarLogs(reset), problemas: carregarProblemas, jobs: carregarJobs, filas: carregarFilas }[estado.aba];
  if (f) f().catch?.(() => {});
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
      ${p.status === "aberto" ? `<button class="fantasma" data-acao="reconhecer" data-id="${p.id}">Reconhecer</button>` : ""}
      ${!resolvido ? `<button class="fantasma" data-acao="resolver" data-id="${p.id}">Resolver</button>` : ""}
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
      <div style="display:flex;gap:8px;align-items:center">${pilula(s.status)}<button class="fantasma" id="g-fechar">✕</button></div>
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
    <form class="form-ed" id="g-form">
      <label>Nome<input name="nome" value="${esc(s.nome)}" maxlength="160"></label>
      <label>Descrição<textarea name="descricao" rows="2">${esc(s.descricao || "")}</textarea></label>
      <label>Grupo (agrupa os cards)<input name="grupo" value="${esc(s.grupo || "")}" maxlength="80" placeholder="ex.: Receita, RPA, APIs"></label>
      <label>Tolerância sem heartbeat, em segundos (vazio = automática: ${Math.max(3 * s.intervalo_s, 60)} s)
        <input name="tolerancia_s" type="number" min="0" value="${s.tolerancia_s ?? ""}"></label>
      <label class="chave"><input name="monitorar" type="checkbox" ${s.monitorar ? "checked" : ""}> Monitorar (desligado = sem alerta de offline)</label>
      <div class="linha">
        <button class="botao" type="submit">Salvar</button>
        <span style="flex:1"></span>
        <button class="fantasma perigo" type="button" id="g-apagar">Apagar serviço</button>
      </div>
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
        tolerancia_s: tol === "" ? 0 : Number(tol), monitorar: f.monitorar.checked } });
      toast("Salvo.", "var(--verde)");
      document.activeElement.blur();
      atualizarPainel();
      atualizarGaveta(true);
    } catch (err) { toast(esc(err.message), "var(--vermelho)"); }
  };
  $("#g-apagar").onclick = async () => {
    if (!confirm(`Apagar "${s.nome}" e todos os logs, jobs e problemas dele?\n\nSe o serviço continuar mandando dados, ele aparece de novo.`)) return;
    try {
      await api(`/api/servicos/${encodeURIComponent(s.slug)}`, { method: "DELETE" });
      fecharGaveta();
      atualizarPainel();
    } catch (err) { toast(esc(err.message), "var(--vermelho)"); }
  };
}

// ── Início ──────────────────────────────────────────────────────────────────

(async () => {
  try {
    await api("/api/auth/sessao");
    mostrarApp();
  } catch {
    mostrarLogin();
  }
})();
