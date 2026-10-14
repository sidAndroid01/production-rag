// RAG Console: a dependency-free test UI for the API served from the same origin.
// Everything user- or document-derived is inserted with textContent, never innerHTML.
"use strict";

const $ = (id) => document.getElementById(id);
const BACKGROUND_ABOVE_BYTES = 2_000_000;
const state = {
  key: sessionStorage.getItem("rag-key") || "",
  session: crypto.randomUUID(),
  connected: false,
  llm: null, // the user's own provider settings, if any
  llmRemember: false,
  file: null,
  maxFileBytes: 18_000_000, // replaced by the server's limit on connect

};

class ApiError extends Error {
  constructor(status, detail, requestId, retryAfter) {
    super(detail);
    this.status = status;
    this.requestId = requestId;
    this.retryAfter = retryAfter;
  }
}

async function api(method, path, body) {
  const started = performance.now();
  const response = await fetch(path, {
    method,
    headers: { "x-api-key": state.key, ...(body ? { "content-type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  const requestId = response.headers.get("x-request-id") || data.request_id;
  if (!response.ok) {
    throw new ApiError(response.status, data.detail || response.statusText, requestId,
      response.headers.get("retry-after"));
  }
  return { ...data, _ms: Math.round(performance.now() - started), _requestId: requestId };
}

function describeError(error) {
  if (!(error instanceof ApiError)) return "Could not reach the API. Is it running?";
  if (error.status === 401) return "That access key was not accepted. Check it with whoever runs this site.";
  if (error.status === 429) return `${error.message}. Try again in ${error.retryAfter || "a few"} s.`;
  return `${error.message} (HTTP ${error.status})`;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// ---------------------------------------------------------------- identity

async function connect() {
  state.key = $("api-key").value.trim();
  sessionStorage.setItem("rag-key", state.key);
  const identity = $("identity");
  try {
    const me = await api("GET", "/v1/me");
    state.connected = true;
    state.maxFileBytes = me.max_file_bytes || state.maxFileBytes;
    $("upload-limit").textContent = formatBytes(state.maxFileBytes);
    identity.replaceChildren(
      "Tenant ", el("strong", "", me.tenant_id),
      " · user ", el("strong", "", me.user_id),
      me.groups.length ? ` · groups ${me.groups.join(", ")}` : "",
      ` · ${me.storage === "postgres" ? "saved in database" : "in memory (lost on restart)"}`,
    );
    setupLlmOptions(me.llm, me.model);
    await refreshDocuments();
  } catch (error) {
    state.connected = false;
    identity.textContent = describeError(error);
    $("doc-list").replaceChildren();
  }
  updateButtons();
}

function formatBytes(bytes) {
  return `${Math.floor(bytes / 1_000_000)} MB`;
}

function updateButtons() {
  const hasContent = Boolean(state.file || $("paste-text").value.trim());
  $("upload-btn").disabled = !state.connected || !hasContent;
  $("ask-btn").disabled = !state.connected || !$("question").value.trim();
  // Say why Upload is unavailable instead of leaving a silently disabled button.
  const hint = $("upload-hint");
  if (!state.connected) hint.textContent = "Connect with your access key first (top of the page).";
  else if (!hasContent) hint.textContent = "Choose a file or paste some text.";
  else hint.textContent = "";
}

// ---------------------------------------------------------------- documents

async function refreshDocuments() {
  if (!state.connected) return;
  const query = $("show-old").checked ? "?include_superseded=true" : "";
  try {
    const { documents } = await api("GET", `/v1/documents${query}`);
    const list = $("doc-list");
    list.replaceChildren(...documents.map(renderDocument));
    $("doc-empty").hidden = documents.length > 0;
  } catch (error) {
    setUploadStatus(describeError(error), "error");
  }
}

function renderDocument(doc) {
  const item = $("tpl-doc").content.firstElementChild.cloneNode(true);
  item.querySelector(".doc-name").textContent = doc.source;
  const parts = [`v${doc.version}`, doc.content_type, `${doc.chunk_count} chunk${doc.chunk_count === 1 ? "" : "s"}`];
  if (doc.allowed_groups && doc.allowed_groups.length) parts.push(`🔒 ${doc.allowed_groups.join(", ")}`);
  if (doc.status === "superseded") {
    parts.push("superseded");
    item.classList.add("superseded");
  }
  if (doc.source_id && doc.source_id !== doc.source) parts.push(`id ${doc.source_id}`);
  item.querySelector(".doc-meta").textContent = parts.join(" · ");
  item.title = `Last checked ${new Date(doc.last_checked_at).toLocaleString()}`;
  const form = item.querySelector(".doc-form");
  item.querySelector(".doc-edit").addEventListener("click", () => {
    form.querySelector(".doc-form-name").value = doc.source;
    form.querySelector(".doc-form-groups").value = (doc.allowed_groups || []).join(", ");
    form.hidden = !form.hidden;
  });
  form.querySelector(".doc-form-cancel").addEventListener("click", () => { form.hidden = true; });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    saveDocument(doc, form);
  });
  item.querySelector(".doc-delete").addEventListener("click", async () => {
    if (!window.confirm(`Delete ${doc.source} (v${doc.version})?`)) return;
    try {
      await api("DELETE", `/v1/documents/${encodeURIComponent(doc.document_id)}`);
      setUploadStatus(`Deleted ${doc.source}.`, "ok");
      refreshDocuments();
    } catch (error) {
      setUploadStatus(describeError(error), "error");
    }
  });
  return item;
}

async function saveDocument(doc, form) {
  const name = form.querySelector(".doc-form-name").value.trim();
  const groups = form.querySelector(".doc-form-groups").value
    .split(",").map((g) => g.trim()).filter(Boolean);
  const changes = { allowed_groups: groups };
  if (name && name !== doc.source) changes.filename = name;
  try {
    const result = await api("PATCH", `/v1/documents/${encodeURIComponent(doc.document_id)}`, changes);
    setUploadStatus(
      result.visible
        ? `Updated ${result.source}.`
        : "Updated. You are not in those groups, so the document is now hidden from you.",
      "ok",
    );
    refreshDocuments();
  } catch (error) {
    setUploadStatus(describeError(error), "error");
  }
}

function setUploadStatus(text, kind = "") {
  const status = $("upload-status");
  status.textContent = text;
  status.className = `status ${kind}`;
}

function readAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

function chooseFile(file) {
  if (file && file.size > state.maxFileBytes) {
    setUploadStatus(
      `${file.name} is ${formatBytes(file.size)}; the limit is ${formatBytes(state.maxFileBytes)}.`,
      "error",
    );
    file = null;
  } else if (file) {
    setUploadStatus("");
  }
  state.file = file || null;
  $("file-name").textContent = state.file ? state.file.name : "";
  updateButtons();
}

async function upload(event) {
  event.preventDefault();
  const groups = $("groups").value.split(",").map((g) => g.trim()).filter(Boolean);
  let payload;
  if (state.file) {
    payload = { filename: state.file.name, content_base64: await readAsBase64(state.file) };
    if (state.file.type) payload.content_type = state.file.type;
  } else {
    // A pasted note without a name still uploads; the name only labels it.
    payload = { filename: $("paste-name").value.trim() || "pasted-text.txt", content: $("paste-text").value };
  }
  // Leaving groups empty keeps the current version's groups (or none for a new document).
  if (groups.length) payload.allowed_groups = groups;
  const sourceId = $("source-id").value.trim();
  if (sourceId) payload.source_id = sourceId;
  // Large files can take minutes to embed; queue them so the page stays responsive.
  const large = Boolean(state.file && state.file.size > BACKGROUND_ABOVE_BYTES);
  const background = $("as-job").checked || large;
  $("upload-btn").disabled = true;
  setUploadStatus(
    large ? "Large file: uploading, then indexing in the background…"
      : background ? "Queuing…" : "Uploading and indexing…",
  );
  try {
    if (background) {
      const job = await api("POST", "/v1/ingestion/jobs", payload);
      await followJob(job.job_id, payload.filename);
    } else {
      const result = await api("POST", "/v1/documents", payload);
      reportStored(payload.filename, result);
    }
    chooseFile(null);
    $("file").value = "";
    $("paste-text").value = "";
    $("paste-name").value = "";
    $("source-id").value = "";
    refreshDocuments();
  } catch (error) {
    setUploadStatus(describeError(error), "error");
  } finally {
    updateButtons();
  }
}

function reportStored(name, result) {
  if (result.unchanged) {
    setUploadStatus(`${name} is unchanged (still v${result.version}); nothing re-indexed.`, "ok");
  } else if (result.already_existed && !result.superseded.length) {
    setUploadStatus(`${name} was already indexed (v${result.version}).`, "ok");
  } else {
    const replaced = result.superseded.length ? `, replacing ${result.superseded.length} older version` : "";
    setUploadStatus(`${name} indexed as v${result.version}: ${result.chunks_created} chunk${result.chunks_created === 1 ? "" : "s"}${replaced}.`, "ok");
  }
}

async function followJob(jobId, name) {
  for (let attempt = 0; attempt < 900; attempt += 1) {
    const job = await api("GET", `/v1/ingestion/jobs/${jobId}`);
    if (job.status === "completed") return reportStored(name, job.result);
    if (job.status === "failed") throw new ApiError(422, job.error || "ingestion failed", job._requestId);
    setUploadStatus(`Indexing ${name} in the background (${job.status}, ${attempt + 1} s)…`);
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  setUploadStatus("Still processing; refresh the list later.");
}

// ---------------------------------------------------------------- chat

function addMessage(role, text, className = "") {
  $("messages").querySelector(".welcome")?.remove();
  const message = el("div", `msg ${role} ${className}`.trim());
  message.append(el("div", "bubble", text));
  $("messages").append(message);
  message.scrollIntoView({ block: "end", behavior: "smooth" });
  return message;
}

// Turn "text [1] more [2,3]" into text nodes and citation buttons.
function renderAnswer(bubble, answer, citations, sources) {
  bubble.replaceChildren();
  const known = new Set(citations.map((c) => c.source_number));
  let last = 0;
  for (const match of answer.matchAll(/\[(\d+(?:\s*,\s*\d+)*)\]/g)) {
    bubble.append(answer.slice(last, match.index));
    for (const number of match[1].split(",").map((n) => Number(n.trim()))) {
      if (!known.has(number)) continue;
      const chip = el("button", "cite-chip", String(number));
      chip.type = "button";
      chip.setAttribute("aria-label", `Show source ${number}`);
      chip.addEventListener("click", () => highlightSource(sources, number, chip));
      bubble.append(chip);
    }
    last = match.index + match[0].length;
  }
  bubble.append(answer.slice(last));
}

function highlightSource(sources, number, chip) {
  for (const node of sources.querySelectorAll(".source, .cite-chip")) node.classList.remove("active");
  for (const node of chip.parentElement.querySelectorAll(".cite-chip")) {
    node.classList.toggle("active", node.textContent === String(number));
  }
  const card = sources.querySelector(`[data-number="${number}"]`);
  if (card) {
    card.classList.add("active");
    card.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
}

function renderSources(citations) {
  const list = el("div", "sources");
  for (const citation of citations) {
    const card = el("div", "source");
    card.dataset.number = String(citation.source_number);
    const head = el("div", "source-head");
    head.append(el("span", "cite-chip", String(citation.source_number)));
    head.append(citation.page ? `${citation.source} · page ${citation.page}` : citation.source);
    head.append(el("span", "score", `score ${citation.score.toFixed(2)}`));
    card.append(head, el("p", "", `${citation.excerpt}${citation.excerpt.length >= 240 ? "…" : ""}`));
    list.append(card);
  }
  return list;
}

function renderMeta(result) {
  const meta = el("div", "meta");
  const generation = result.generation;
  if (generation) {
    meta.append(el("span", "", generation.model === "extractive" ? "quoted from your documents" : `written by ${generation.model}`));
    if (generation.prompt_tokens || generation.completion_tokens) {
      meta.append(el("span", "", `${generation.prompt_tokens + generation.completion_tokens} tokens`));
    }
    if (generation.cost_usd) meta.append(el("span", "", `$${generation.cost_usd.toFixed(5)}`));
    if (generation.own_key) meta.append(el("span", "badge", "your account"));
    if (generation.degraded) {
      const badge = el("span", "badge warn", "AI unavailable, showing quotes");
      if (generation.note) badge.title = generation.note;
      meta.append(badge);
      if (generation.note) meta.append(el("span", "", generation.note));
    }
  }
  if (result.search_query) meta.append(el("span", "", `searched: “${result.search_query}”`));
  meta.append(el("span", "", `${result._ms} ms`));
  meta.append(el("span", "", `id ${String(result._requestId).slice(0, 8)}`));
  return meta;
}

async function ask(event) {
  event.preventDefault();
  const question = $("question").value.trim();
  if (!question) return;
  $("question").value = "";
  updateButtons();
  addMessage("user", question);
  const pending = addMessage("assistant", "Thinking…", "typing");
  try {
    // Every question is part of the current conversation; "New chat" starts over.
    const body = { question, session_id: state.session };
    if (state.llm) body.llm = state.llm;
    const result = await api("POST", "/v1/chat", body);
    pending.classList.remove("typing");
    if (!result.grounded) pending.classList.add("abstain");
    const sources = renderSources(result.citations);
    renderAnswer(pending.querySelector(".bubble"), result.answer, result.citations, sources);
    if (result.citations.length) pending.append(sources);
    pending.append(renderMeta(result));
  } catch (error) {
    pending.className = "msg assistant error";
    pending.querySelector(".bubble").textContent = describeError(error);
    if (error.requestId) pending.append(el("div", "meta", `id ${error.requestId.slice(0, 8)}`));
  }
}

function newChat() {
  state.session = crypto.randomUUID();
  const welcome = el("div", "welcome");
  welcome.append(el("p", "", "New conversation. Earlier questions are forgotten."));
  $("messages").replaceChildren(welcome);
}

// ---------------------------------------------------------------- which AI answers
// One choice: the site's built-in setup, or the user's own AI account. The
// user's key lives only in this browser (sessionStorage, or localStorage when
// they tick "remember") and is sent with their questions.

const LLM_STORE = "rag-llm";
let llmPresets = { providers: [], custom_allowed: false };
let builtinModel = "extractive";

function storage(remember) {
  try {
    return remember ? localStorage : sessionStorage;
  } catch {
    return null;
  }
}

function loadLlm() {
  for (const store of [storage(false), storage(true)]) {
    try {
      const saved = store && store.getItem(LLM_STORE);
      if (saved) return JSON.parse(saved);
    } catch {
      /* unreadable storage: start without saved settings */
    }
  }
  return null;
}

function saveLlm(settings, remember) {
  for (const store of [storage(false), storage(true)]) {
    try { store && store.removeItem(LLM_STORE); } catch { /* ignore */ }
  }
  if (!settings) return;
  try { storage(remember)?.setItem(LLM_STORE, JSON.stringify(settings)); } catch { /* ignore */ }
}

function describeBuiltin() {
  // "llama3 → extractive" means llama3, with quotes as the fallback.
  const first = builtinModel.split("→")[0].trim();
  return first === "extractive"
    ? "free · answers are quotes from your documents, not written by an AI"
    : `free · answers written by ${first}, set up by the site owner`;
}

function setupLlmOptions(presets, serverModel) {
  llmPresets = presets || llmPresets;
  builtinModel = serverModel || builtinModel;
  $("llm-builtin-desc").textContent = describeBuiltin();
  const select = $("llm-provider");
  select.replaceChildren();
  for (const provider of llmPresets.providers) select.append(new Option(provider.label, provider.id));
  if (llmPresets.custom_allowed) select.append(new Option("Other (OpenAI-compatible address)", "custom"));
  fillLlmForm();
  showLlmChoice();
}

function choice() {
  return document.querySelector('input[name="llm-choice"]:checked').value;
}

function fillLlmForm() {
  const settings = state.llm;
  document.querySelector(`input[name="llm-choice"][value="${settings ? "own" : "builtin"}"]`).checked = true;
  if (settings) {
    $("llm-provider").value = settings.provider;
    $("llm-model").value = settings.model;
    $("llm-key").value = settings.api_key;
    $("llm-url").value = settings.base_url || "";
  } else {
    $("llm-key").value = "";
  }
  $("llm-remember").checked = Boolean(settings && state.llmRemember);
  onProviderChange(!settings);
  onChoiceChange();
}

function onChoiceChange() {
  $("llm-own").hidden = choice() !== "own";
  setLlmStatus("");
}

function onProviderChange(resetModel = true) {
  const id = $("llm-provider").value;
  const preset = llmPresets.providers.find((p) => p.id === id);
  $("llm-models").replaceChildren(...(preset ? preset.models.map((m) => new Option(m, m)) : []));
  if (resetModel) $("llm-model").value = preset ? preset.models[0] || "" : "";
  $("llm-url-row").hidden = id !== "custom";
  const link = $("llm-key-link");
  link.hidden = !preset;
  if (preset) link.href = preset.key_url;
}

function readLlmForm() {
  const settings = {
    provider: $("llm-provider").value,
    model: $("llm-model").value.trim(),
    api_key: $("llm-key").value.trim(),
  };
  if (settings.provider === "custom") settings.base_url = $("llm-url").value.trim();
  return settings;
}

function showLlmChoice() {
  const button = $("llm-toggle");
  if (state.llm) {
    const preset = llmPresets.providers.find((p) => p.id === state.llm.provider);
    const label = preset ? preset.label : "your AI";
    button.textContent = `AI: your ${label} · ${state.llm.model}`;
  } else {
    button.textContent = "AI: Built-in";
  }
  button.classList.toggle("own", Boolean(state.llm));
}

function setLlmStatus(text, kind = "") {
  $("llm-status").textContent = text;
  $("llm-status").className = `status ${kind}`;
}

function closeLlmPanel() {
  $("llm-panel").hidden = true;
  $("llm-toggle").setAttribute("aria-expanded", "false");
}

// Save always checks the key first, so a broken key is never silently in use.
async function saveLlmForm(event) {
  event.preventDefault();
  if (choice() === "builtin") {
    state.llm = null;
    saveLlm(null, false);
    showLlmChoice();
    $("llm-key").value = "";
    setLlmStatus("");
    closeLlmPanel();
    return;
  }
  const settings = readLlmForm();
  if (!settings.model || !settings.api_key || (settings.provider === "custom" && !settings.base_url)) {
    return setLlmStatus("Fill in the model and your API key.", "error");
  }
  setLlmStatus("Checking your key…");
  $("llm-save").disabled = true;
  try {
    const result = await api("POST", "/v1/llm/test", { llm: settings });
    if (!result.ok) return setLlmStatus(`That did not work: ${result.error}`, "error");
    const remember = $("llm-remember").checked;
    state.llm = settings;
    state.llmRemember = remember;
    saveLlm({ ...settings, remember }, remember);
    showLlmChoice();
    setLlmStatus(`Works (${result.latency_ms} ms). Your questions now use your account.`, "ok");
  } catch (error) {
    setLlmStatus(describeError(error), "error");
  } finally {
    $("llm-save").disabled = false;
  }
}

// ---------------------------------------------------------------- wiring

function init() {
  const saved = loadLlm();
  if (saved) {
    const { remember, ...settings } = saved;
    state.llm = settings;
    state.llmRemember = Boolean(remember);
  }
  $("llm-toggle").addEventListener("click", () => {
    const panel = $("llm-panel");
    if (panel.hidden) fillLlmForm();
    panel.hidden = !panel.hidden;
    $("llm-toggle").setAttribute("aria-expanded", String(!panel.hidden));
  });
  for (const radio of document.querySelectorAll('input[name="llm-choice"]')) {
    radio.addEventListener("change", onChoiceChange);
  }
  $("llm-provider").addEventListener("change", () => onProviderChange(true));
  $("llm-panel").addEventListener("submit", saveLlmForm);
  $("llm-cancel").addEventListener("click", closeLlmPanel);
  showLlmChoice();
  $("api-key").value = state.key;
  $("key-form").addEventListener("submit", (event) => { event.preventDefault(); connect(); });
  $("upload-form").addEventListener("submit", upload);
  $("ask-form").addEventListener("submit", ask);
  $("new-chat").addEventListener("click", newChat);
  $("show-old").addEventListener("change", refreshDocuments);
  $("file").addEventListener("change", (event) => chooseFile(event.target.files[0]));
  for (const id of ["paste-text", "paste-name", "question"]) $(id).addEventListener("input", updateButtons);
  $("question").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      $("ask-form").requestSubmit();
    }
  });
  // Handle drops ourselves; a file dropped anywhere else must not make the
  // browser navigate away to display it.
  const drop = $("drop");
  for (const type of ["dragover", "drop"]) {
    window.addEventListener(type, (event) => event.preventDefault());
  }
  drop.addEventListener("dragover", () => drop.classList.add("over"));
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (event) => {
    drop.classList.remove("over");
    const [file] = event.dataTransfer.files;
    if (file) chooseFile(file);
  });
  updateButtons();
  if (state.key) connect();
}

init();
