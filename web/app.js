// RAG Console: a dependency-free test UI for the API served from the same origin.
// Everything user- or document-derived is inserted with textContent, never innerHTML.
"use strict";

const $ = (id) => document.getElementById(id);
const MAX_FILE_BYTES = 3_500_000; // base64 adds ~33%; the API accepts 5 MB bodies.
const state = {
  key: sessionStorage.getItem("rag-key") || "",
  session: crypto.randomUUID(),
  connected: false,
  file: null,
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
  if (error.status === 401) return "That API key was not accepted.";
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
    identity.replaceChildren(
      "Tenant ", el("strong", "", me.tenant_id),
      " · user ", el("strong", "", me.user_id),
      me.groups.length ? ` · groups ${me.groups.join(", ")}` : "",
      ` · ${me.storage} · model: ${me.model}`,
    );
    await refreshDocuments();
  } catch (error) {
    state.connected = false;
    identity.textContent = describeError(error);
    $("doc-list").replaceChildren();
  }
  updateButtons();
}

function updateButtons() {
  const pasted = $("paste-text").value.trim() && $("paste-name").value.trim();
  $("upload-btn").disabled = !state.connected || !(state.file || pasted);
  $("ask-btn").disabled = !state.connected || !$("question").value.trim();
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
  item.querySelector(".doc-meta").textContent = parts.join(" · ");
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
  if (file && file.size > MAX_FILE_BYTES) {
    setUploadStatus(`${file.name} is larger than 3.5 MB.`, "error");
    file = null;
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
    payload = { filename: $("paste-name").value.trim(), content: $("paste-text").value };
  }
  if (groups.length) payload.allowed_groups = groups;
  const background = $("as-job").checked;
  $("upload-btn").disabled = true;
  setUploadStatus(background ? "Queuing…" : "Uploading and indexing…");
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
    refreshDocuments();
  } catch (error) {
    setUploadStatus(describeError(error), "error");
  } finally {
    updateButtons();
  }
}

function reportStored(name, result) {
  if (result.already_existed && !result.superseded.length) {
    setUploadStatus(`${name} was already indexed (v${result.version}).`, "ok");
  } else {
    const replaced = result.superseded.length ? `, replacing ${result.superseded.length} older version` : "";
    setUploadStatus(`${name} indexed as v${result.version}: ${result.chunks_created} chunk${result.chunks_created === 1 ? "" : "s"}${replaced}.`, "ok");
  }
}

async function followJob(jobId, name) {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    const job = await api("GET", `/v1/ingestion/jobs/${jobId}`);
    if (job.status === "completed") return reportStored(name, job.result);
    if (job.status === "failed") throw new ApiError(422, job.error || "ingestion failed", job._requestId);
    setUploadStatus(`Background job ${job.status}…`);
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  setUploadStatus("Still processing; refresh the list later.");
}

// ---------------------------------------------------------------- chat

function mode() {
  return document.querySelector('input[name="mode"]:checked').value;
}

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
    meta.append(el("span", "", `model ${generation.model}`));
    if (generation.prompt_tokens || generation.completion_tokens) {
      meta.append(el("span", "", `${generation.prompt_tokens + generation.completion_tokens} tokens`));
    }
    if (generation.cost_usd) meta.append(el("span", "", `$${generation.cost_usd.toFixed(5)}`));
    if (generation.degraded) meta.append(el("span", "badge warn", "fallback answer"));
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
  const isChat = mode() === "chat";
  try {
    const result = await api("POST", isChat ? "/v1/chat" : "/v1/query",
      isChat ? { question, session_id: state.session } : { question });
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
  welcome.append(el("p", "", "New conversation started."));
  $("messages").replaceChildren(welcome);
}

// ---------------------------------------------------------------- wiring

function init() {
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
  const drop = $("drop");
  drop.addEventListener("dragover", () => drop.classList.add("over"));
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", () => drop.classList.remove("over"));
  if (state.key) connect();
}

init();
