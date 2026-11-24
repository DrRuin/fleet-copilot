const $ = (id) => document.getElementById(id);
const EXAMPLES = [
  "Which devices are low on disk space?",
  "Which laptops have batteries near end of life?",
  "Is there compliance drift in the fleet?",
  "Which devices are consistently RAM constrained?",
  "Order a disk upgrade for the device that has been low on storage longest.",
];
const ACTIONS = {
  create_upgrade_order: "Upgrade order",
  open_remediation_ticket: "Remediation ticket",
  flag_device_for_replacement: "Replacement flag",
  notify_employee: "Employee message",
};
const SHOWN = 40;
const STATUS = {
  awaiting_approval: "Waiting for you",
  refused: "Refused by policy",
  approved: "Approved",
  rejected: "Rejected",
  executed: "Carried out",
};

let session = null;
let busy = false;
let rows = {};

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  node.append(...children.filter((c) => c !== null && c !== undefined));
  return node;
}

async function* events(response) {
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += value.replaceAll("\r\n", "\n");
    let end;
    while ((end = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      let event = "message";
      const data = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
      }
      if (data.length) yield { event, data: JSON.parse(data.join("\n")) };
    }
  }
}

function openPanel(title, body) {
  $("panel-title").textContent = title;
  $("panel-body").replaceChildren(...body);
  $("panel").hidden = false;
  document.body.classList.add("panel-open");
}

function table(fact) {
  return el("table", {}, ...Object.entries(fact).map(([k, v]) =>
    el("tr", {}, el("td", {}, k.replaceAll("_", " ")), el("td", {}, typeof v === "object" ? JSON.stringify(v) : String(v)))));
}

async function showSnapshot(id) {
  const r = await fetch(`/api/sessions/${session}/snapshots/${encodeURIComponent(id)}`);
  const body = r.ok ? await r.json() : { error: "not in this company" };
  openPanel(id, [el("pre", {}, JSON.stringify(body, null, 2))]);
}

function showRow(ref) {
  const row = rows[ref];
  if (!row) return;
  const snaps = row.snapshots.slice(0, SHOWN).map((s) => el("button", { class: "snap", onclick: () => showSnapshot(s) }, s));
  const more = row.snapshots.length - snaps.length;
  openPanel(`${ref} from ${row.tool}`, [
    table(row.fact),
    el("p", { class: "label" }, `Snapshots behind this row (${row.snapshots.length})`),
    ...snaps,
    more > 0 ? el("p", { class: "muted" }, `and ${more} more`) : null,
  ]);
}

function renderAnswer(answer, checks) {
  const box = el("div", { class: "answer" });
  for (const s of answer.sentences) {
    box.append(el("p", {}, s.text, ...s.refs.map((r) => el("button", { class: "cite", onclick: () => showRow(r) }, r))));
  }
  const withheld = checks.filter((c) => c.problem).length;
  const claims = checks.filter((c) => c.verdict !== "no_claim").length;
  box.append(withheld
    ? el("span", { class: "check some" }, `${withheld} sentence${withheld > 1 ? "s" : ""} withheld: not supported by the cited rows`)
    : el("span", { class: "check ok" }, `${claims} of ${claims} factual sentences checked against their rows`));
  return box;
}

function renderProposal(p, pause) {
  const card = el("div", { class: `proposal ${p.status}`, "data-id": p.id },
    el("h4", {}, `${ACTIONS[p.kind] ?? p.kind} for ${p.target} `, el("span", { class: `badge ${p.status}` }, STATUS[p.status])),
    el("div", { class: "muted" }, Object.entries(p.args).filter(([k]) => k !== "refs").map(([k, v]) => `${k}: ${v}`).join(" · ")),
    p.status === "refused" ? el("div", {}, `Why: ${p.reason}`) : null,
    p.status === "rejected" ? el("div", {}, `Reviewer note: ${p.reason}`) : null,
    p.facts?.length ? el("ul", {}, ...p.facts.map((f) => el("li", {}, f))) : null,
    p.reference ? el("div", {}, `Reference ${p.reference}`) : null);
  if (pause) {
    const note = el("input", { placeholder: "Note for the audit log (optional)" });
    const chosen = el("span", { class: "muted" });
    const decide = (approve) => {
      pause.decisions[p.id] = { approve, note: note.value };
      card.querySelectorAll(".decide button, .decide input").forEach((b) => (b.disabled = true));
      const left = pause.ids.length - Object.keys(pause.decisions).length;
      chosen.textContent = `${approve ? "Approve" : "Reject"} chosen` + (left ? `, decide ${left} more to send` : "");
      if (!left) resume(pause.decisions, card);
    };
    card.append(el("div", { class: "decide" }, note,
      el("button", { class: "yes", onclick: () => decide(true) }, "Approve"),
      el("button", { onclick: () => decide(false) }, "Reject")), chosen);
  }
  return card;
}

async function play(response, reply) {
  const steps = el("ol");
  const thinking = el("div", { class: "thinking" }, "Working…");
  const details = el("details", { class: "steps" }, el("summary", {}, "How it was answered"), steps);
  reply.append(details, thinking);
  const proposals = el("div");
  for await (const { event, data } of events(response)) {
    if (event === "plan") {
      steps.append(el("li", {}, `Plan (${data.intent}): ${data.steps.join("; ")}`));
      thinking.textContent = "Reading telemetry…";
    } else if (event === "calls") {
      for (const c of data.calls) steps.append(el("li", {}, el("code", {}, c.name), " ", JSON.stringify(c.args)));
    } else if (event === "rows") {
      Object.assign(rows, data.rows);
      steps.append(el("li", {}, `Got rows ${Object.keys(data.rows).join(", ") || "none"}`));
      for (const p of data.proposals) if (p.status === "refused") proposals.append(renderProposal(p, null));
      thinking.textContent = "Checking the answer against its rows…";
    } else if (event === "verify") {
      if (data.answer) {
        thinking.remove();
        reply.append(renderAnswer(data.answer, data.checks), proposals);
      } else {
        steps.append(el("li", {}, "Answer sent back for a rewrite: a sentence was not supported"));
      }
    } else if (event === "approval") {
      const pause = { ids: data.proposals.map((p) => p.id), decisions: {} };
      for (const p of data.proposals) proposals.append(renderProposal(p, pause));
    } else if (event === "approve") {
      for (const p of data.proposals ?? []) {
        const old = document.querySelector(`.proposal[data-id="${p.id}"]`);
        const card = renderProposal(p, null);
        old ? old.replaceWith(card) : reply.append(card);
      }
    } else if (event === "error") {
      reply.append(el("p", { class: "error" }, data.error));
    }
  }
  thinking.remove();
  if (!steps.children.length) details.remove();
}

async function send(path, body, reply) {
  busy = true;
  $("send").disabled = true;
  try {
    const r = await fetch(`/api/sessions/${session}/${path}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (r.ok) await play(r, reply);
    else reply.append(el("p", { class: "error" }, (await r.json()).detail));
  } finally {
    busy = false;
    $("send").disabled = false;
  }
}

async function resume(decisions, card) {
  await send("decisions", decisions, card.closest(".reply"));
}

async function ask(text) {
  if (busy || !session) return;
  $("thread").querySelector(".empty")?.remove();
  const reply = el("div", { class: "reply" });
  const turn = el("div", { class: "turn" }, el("div", { class: "you" }, text), reply);
  $("thread").append(turn);
  await send("messages", { text }, reply);
  turn.scrollIntoView({ block: "end" });
}

async function showAudit() {
  const { events: log, actions } = await (await fetch(`/api/sessions/${session}/audit`)).json();
  openPanel("Audit log", [
    el("p", { class: "label" }, `Actions carried out (${actions.length})`),
    ...actions.map((a) => el("div", { class: "event" }, `${a.id} ${a.kind} ${a.target}, approved by ${a.approver}`)),
    el("p", { class: "label" }, `Events, newest first (${log.length})`),
    ...log.map((e) => el("div", { class: "event" }, el("b", {}, e.event), ` ${e.at} by ${e.actor}`,
      el("pre", {}, JSON.stringify(e.detail, null, 1).slice(0, 600)))),
  ]);
}

async function signIn() {
  const companies = await (await fetch("/api/companies")).json();
  $("company").replaceChildren(...Object.entries(companies).map(([c, n]) => el("option", { value: c }, `${c} (${n} devices)`)));
  $("signin").showModal();
}

$("signin").addEventListener("close", async () => {
  const r = await fetch("/api/sessions", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ company: $("company").value, actor: $("actor").value }),
  });
  const s = await r.json();
  session = s.session;
  rows = {};
  $("tenant").textContent = `${s.company}, ${s.devices} devices`;
  $("thread").replaceChildren(el("div", { class: "empty" }, el("h2", {}, `Ask about ${s.company}`),
    el("p", { class: "muted" }, "Every sentence cites the telemetry rows behind it. Actions wait for your approval.")));
});

$("ask").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("text").value.trim();
  if (!text || !session) return;
  $("text").value = "";
  ask(text);
});
$("text").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("ask").requestSubmit(); }
});
$("examples").append(...EXAMPLES.map((q) => el("button", { onclick: () => session && ask(q) }, q)));
$("show-audit").addEventListener("click", () => session && showAudit());
$("switch").addEventListener("click", signIn);
$("close").addEventListener("click", () => { $("panel").hidden = true; document.body.classList.remove("panel-open"); });
signIn();
