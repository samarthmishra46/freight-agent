/* FreightAgent operator terminal.
 *
 * In v1 the confirmation gate here was theatre: "Confirm and lodge" sent an
 * ordinary chat message, nothing was bound to the payload above the button, and
 * the loop lodged just as readily without it. Test A demonstrated it — three of
 * four routes reached the ledger.
 *
 * Phase 9 replaced it with CP-8. The server stages a lodgement and issues a
 * single-use token bound to the payload hash, the session and the operator's
 * certificate; renderStagedCard draws the card from that staged payload, and
 * the button POSTs the token to /confirm, which is the only path to the customs
 * server. What is signed off is now what is sent.
 *
 * Still unfixed, and still findings: the identity headers below are asserted by
 * this client and verified by nothing (CP-1), and a caller that skips the agent
 * entirely can still reach the customs server (CP-10).
 */

const SAC_THRESHOLD = 1000;

const OPERATOR = {
  name: "Priya Sharma",
  forwarder: "Yarra Trade Operations Pty Ltd",
  certificate: "T3-YARRA-0041",
  email: "priya.sharma@yarratrade.com.au",
};

const LODGEMENTS = [
  { key: "cargo_report", seq: "Lodgement 1", title: "Cargo report" },
  { key: "underbond", seq: "Lodgement 2", title: "Underbond movement" },
  { key: "outturn", seq: "Lodgement 3", title: "Outturn" },
];

const el = (id) => document.getElementById(id);
const stream = el("stream");

let sessionId =
  new URLSearchParams(location.search).get("session") ||
  localStorage.getItem("freightagent.session") ||
  null;
let state = null;
let busy = false;

/* Lodgement references already toasted, so a resumed session does not
 * re-announce work that was finished days ago. */
const acknowledged = new Set();

/* ------------------------------------------------------------- helpers */

function identityHeaders() {
  return {
    "X-Operator": OPERATOR.name,
    "X-Certificate": OPERATOR.certificate,
    "X-Forwarder": OPERATOR.forwarder,
    "X-Operator-Email": OPERATOR.email,
  };
}

function kg(value) {
  return Number(value).toFixed(1);
}

function money(value) {
  return Number(value).toLocaleString("en-AU", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function bytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1048576).toFixed(2)} MB`;
}

function scrollDown() {
  stream.scrollTop = stream.scrollHeight;
}

function clearEmpty() {
  const empty = el("empty");
  if (empty) empty.remove();
}

/* ------------------------------------------------------------ messages */

function addMessage(kind, who, text) {
  clearEmpty();
  const wrap = document.createElement("div");
  wrap.className = `msg ${kind}`;
  const label = document.createElement("div");
  label.className = "who";
  label.textContent = who;
  const body = document.createElement("div");
  body.className = "what";
  body.textContent = text;
  wrap.append(label, body);
  stream.append(wrap);
  scrollDown();
  return wrap;
}

/* The plumbing view: real hops, real byte counts, straight from the loop's
 * own audit log entries returned by /chat. */
function addHops(hops) {
  if (!el("plumbing").checked) return;
  clearEmpty();
  for (const hop of hops) {
    const row = document.createElement("div");
    row.className = "hop";
    if (hop.hop === "loop->llm") row.classList.add("egress");
    if (hop.hop === "loop->customs") row.classList.add("irreversible");

    const name = document.createElement("span");
    name.className = "name";
    name.textContent = hop.hop;

    const size = document.createElement("span");
    size.className = "size";
    size.textContent = bytes(hop.payload_bytes);

    const note = document.createElement("span");
    note.className = "note";
    note.textContent = hop.notes || "";

    row.append(name, size, note);
    stream.append(row);
  }
  scrollDown();
}

function toast(text) {
  const t = el("toast");
  t.textContent = text;
  t.hidden = false;
  setTimeout(() => { t.hidden = true; }, 2600);
}

/* --------------------------------------------------------- status strip */

/* The status pill shows the shipment state the backend holds, not a value
 * reconstructed here from the lodgements. The state machine on the server is
 * the one thing that decides where a consignment has got to; a strip that
 * computed its own answer could disagree with what the loop will allow next. */
const STATE_CLASS = {
  NO_STATUS: "",
  HELD: "held",
  CLEAR: "clear",
  SUBUBMOV: "sububmov",
  RELEASED: "released",
};

function renderStrip() {
  const master = state && state.manifest ? state.manifest.master : null;
  el("f-flight").textContent = master && master.flight ? master.flight : "—";
  el("f-route").textContent =
    master && master.origin ? `${master.origin} → ${master.destination || "—"}` : "—";
  el("f-mawb").textContent = master && master.mawb ? master.mawb : "—";

  const shipmentState = (state && state.shipment_state) || "NO_STATUS";
  const pill = el("status-pill");
  pill.className = `pill ${STATE_CLASS[shipmentState] || ""}`.trim();
  pill.textContent =
    (state && state.shipment_status_label) || shipmentState.replace("_", " ");
}

function renderRail() {
  for (const { key, title } of LODGEMENTS) {
    const node = el(`l-${key}`);
    const lodgement = state && state.lodgements ? state.lodgements[key] : null;
    const stateEl = node.querySelector(".state");
    const refEl = node.querySelector(".ref");
    const errEl = node.querySelector(".errs");

    node.className = "lodge awaiting";
    refEl.textContent = "";
    errEl.textContent = "";
    stateEl.textContent = "awaiting";

    if (!lodgement) continue;

    if (lodgement.status === "accepted") {
      node.className = "lodge accepted";
      stateEl.textContent = "accepted";
      refEl.textContent = lodgement.reference || "";
    } else {
      node.className = "lodge rejected";
      stateEl.textContent = "rejected";
      if (lodgement.errors) errEl.textContent = lodgement.errors.join("; ");
    }
  }

  if (state && state.operator) {
    el("op-name").textContent = state.operator.name;
    el("op-forwarder").textContent = state.operator.forwarder;
    el("op-cert").textContent = state.operator.certificate_id;
    el("op-warn").hidden = false;
  }
}

/* ---------------------------------------------------- lodgement preview */

function splitBills() {
  const bills = state.manifest.house_bills || [];
  return {
    sac: bills.filter((b) => Number(b.value_aud) <= SAC_THRESHOLD),
    over: bills.filter((b) => Number(b.value_aud) > SAC_THRESHOLD),
  };
}

const ACTION_TO_LODGEMENT = {
  lodge_cargo_report: "cargo_report",
  lodge_underbond_request: "underbond",
  lodge_outturn: "outturn",
};

function nextLodgement() {
  if (!state || !state.manifest) return null;
  const key = ACTION_TO_LODGEMENT[state.next_action];
  return LODGEMENTS.find((l) => l.key === key) || null;
}

function billsTable(sac, over) {
  const scroll = document.createElement("div");
  scroll.className = "scroll";
  const table = document.createElement("table");
  table.className = "fields";
  table.innerHTML = `
    <thead><tr>
      <th>HAWB</th><th>Consignee</th><th>Pieces</th>
      <th>Weight kg</th><th>Value AUD</th><th>On report</th>
    </tr></thead><tbody></tbody>`;
  const tbody = table.querySelector("tbody");

  /* CP-12. hawb and consignee are extracted from a PDF that arrived from
   * outside the company, so they are written as text and never as markup.
   * Interpolating them into innerHTML made a crafted manifest an XSS vector
   * against the operator's own browser — the session that holds the CP-8
   * confirmation tokens, which makes it a document-to-lodgement path that no
   * prompt-injection control would catch. See tests/test_f_output_handling.py. */
  const row = (bill, included) => {
    const tr = document.createElement("tr");
    if (!included) tr.className = "excluded";
    const cells = [
      ["k", bill.hawb],
      ["", bill.consignee],
      ["n", bill.pieces],
      ["n", kg(bill.weight_kg)],
      ["n val", money(bill.value_aud)],
      ["", included ? "yes" : "no — broker"],
    ];
    for (const [cls, value] of cells) {
      const td = document.createElement("td");
      if (cls) td.className = cls;
      td.textContent = value == null ? "—" : String(value);
      tr.append(td);
    }
    tbody.append(tr);
  };

  sac.forEach((b) => row(b, true));
  over.forEach((b) => row(b, false));
  scroll.append(table);
  return scroll;
}

function kvTable(pairs) {
  const dl = document.createElement("dl");
  dl.className = "kv";
  for (const [k, v] of pairs) {
    const dt = document.createElement("dt");
    dt.textContent = k;
    const dd = document.createElement("dd");
    dd.textContent = v;
    dl.append(dt, dd);
  }
  return dl;
}

/* The card shows exactly what would be sent, field by field, computed here
 * from the parsed manifest — not from anything the model said. */
function renderPreviewCard() {
  const next = nextLodgement();
  if (!next) return;

  const master = state.manifest.master || {};
  const { sac, over } = splitBills();

  const card = document.createElement("div");
  card.className = "card";

  const header = document.createElement("header");
  header.innerHTML = `<h2>${next.title} — to be lodged</h2>
                      <span class="seq">${next.seq} of 3</span>`;
  card.append(header);

  const summary = document.createElement("div");
  summary.className = "summary";
  card.append(summary);

  let scannedInput = null;

  if (next.key === "cargo_report") {
    summary.textContent =
      `${sac.length} of ${sac.length + over.length} house bills included, all at or under ` +
      `AUD ${money(SAC_THRESHOLD)}. ${over.length} excluded — over threshold, ` +
      `cleared by a licensed broker.`;
    card.append(billsTable(sac, over));
  } else if (next.key === "underbond") {
    summary.textContent = "Moves the consignment from the terminal to the destination depot.";
    card.append(kvTable([
      ["MAWB", master.mawb || "—"],
      ["From", `${master.terminal || "—"} (terminal)`],
      ["To", `${master.depot || "—"} (depot)`],
      ["Mode", "ROAD"],
      ["Reason", "DCL — depot to clearance"],
    ]));
  } else {
    const expected = master.total_pieces != null ? master.total_pieces : "—";
    summary.textContent =
      `Reports what the depot actually received. ${expected} pieces expected. ` +
      `A matching count is NIL; fewer is SH and more is SU, and a discrepancy ` +
      `needs a reason in your own words, recorded against your name.`;
    card.append(kvTable([
      ["MAWB", master.mawb || "—"],
      ["Location", master.depot || "—"],
      ["Expected pieces", String(expected)],
    ]));
  }

  /* The confirmation gate. */
  const gate = document.createElement("div");
  gate.className = "gate";

  /* CP-8. This is a PREVIEW, not the gate. Nothing here is staged yet, so the
   * button must not look or read like the real confirmation — two buttons that
   * feel equally weighty, one of which records no proof, is the exact failure
   * this interface was criticised for in v1. */
  const attest = document.createElement("div");
  attest.className = "attest";
  attest.textContent =
    `Not yet staged. This asks the assistant to prepare the lodgement; the ` +
    `server then stages it and issues a single-use confirmation bound to ` +
    `${OPERATOR.name} (${OPERATOR.certificate}). Nothing reaches customs ` +
    `until you confirm that staged payload.`;
  gate.append(attest);

  if (next.key === "outturn") {
    const wrap = document.createElement("label");
    wrap.className = "count-input";
    wrap.innerHTML = "Pieces scanned";
    scannedInput = document.createElement("input");
    scannedInput.type = "number";
    scannedInput.min = "0";
    scannedInput.value = master.total_pieces != null ? String(master.total_pieces) : "";
    wrap.append(scannedInput);
    gate.append(wrap);
  }

  const button = document.createElement("button");
  button.className = "confirm prepare";
  button.type = "button";
  button.textContent = "Prepare this lodgement";
  button.addEventListener("click", () => {
    button.disabled = true;
    send(confirmationMessage(next, sac, over, master, scannedInput));
  });
  gate.append(button);

  card.append(gate);
  clearEmpty();
  stream.append(card);
  scrollDown();
}

function confirmationMessage(next, sac, over, master, scannedInput) {
  if (next.key === "cargo_report") {
    return `Confirmed. Lodge the cargo report with the ${sac.length} house bills at or ` +
           `under AUD 1,000, excluding the ${over.length} over threshold.`;
  }
  if (next.key === "underbond") {
    return `Confirmed. Lodge the underbond movement for MAWB ${master.mawb} from ` +
           `${master.terminal} to ${master.depot}, reason DCL.`;
  }
  const scanned = scannedInput && scannedInput.value !== "" ? scannedInput.value : "unknown";
  const expected = master.total_pieces;
  if (String(scanned) === String(expected)) {
    return `Confirmed. The depot scanned ${scanned} of ${expected} pieces. ` +
           `Lodge the outturn as NIL.`;
  }
  const code = Number(scanned) < Number(expected) ? "SH" : "SU";
  return `Confirmed. The depot scanned ${scanned} pieces against ${expected} expected. ` +
         `Lodge the outturn as ${code} — ask me for the reason before you do.`;
}

/* ------------------------------------------------------------ requests */

function setBusy(value) {
  busy = value;
  el("send").disabled = value;
  el("input").disabled = value;
  document.querySelectorAll(".confirm").forEach((b) => { b.disabled = value; });
}

async function send(message) {
  if (busy || !message.trim()) return;
  addMessage("operator", OPERATOR.name, message);
  setBusy(true);
  const thinking = addMessage("system", "Assistant", "Working…");

  try {
    const response = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...identityHeaders() },
      body: JSON.stringify({ session_id: sessionId, message }),
    });

    const payload = await response.json();
    thinking.remove();

    if (!response.ok) {
      addMessage("error", "Error", payload.detail || `HTTP ${response.status}`);
      return;
    }

    state = payload.state;
    sessionId = state.session_id;
    localStorage.setItem("freightagent.session", sessionId);

    addHops(payload.hops || []);
    addMessage("assistant", "Assistant", payload.reply || "(no reply)");

    renderStrip();
    renderRail();

    for (const { key } of LODGEMENTS) {
      const l = state.lodgements[key];
      if (l && l.status === "accepted" && !acknowledged.has(l.reference)) {
        acknowledged.add(l.reference);
        toast("Lodged");
      }
    }

    // CP-8. A confirm button only exists when the server has staged something.
    // The card is drawn from the staged payload, so what is signed off is what
    // will be sent — not a separate copy computed here from the manifest.
    if (state.pending_confirmation) {
      renderStagedCard(state.pending_confirmation);
    }
  } catch (err) {
    thinking.remove();
    addMessage("error", "Error", String(err));
  } finally {
    setBusy(false);
    el("input").focus();
  }
}

/* The staged lodgement, exactly as the server holds it. The token came with it
 * and goes straight back; the model never saw it and cannot produce one. */
function renderStagedCard(pending) {
  const titles = {
    lodge_cargo_report: ["Cargo report", "1"],
    lodge_underbond_request: ["Underbond movement", "2"],
    lodge_outturn: ["Outturn", "3"],
  };
  const [title, seq] = titles[pending.action] || [pending.action, "?"];

  const card = document.createElement("div");
  card.className = "card";

  const header = document.createElement("header");
  const h2 = document.createElement("h2");
  h2.textContent = `${title} — staged, awaiting your confirmation`;
  const span = document.createElement("span");
  span.className = "seq";
  span.textContent = `${seq} of 3`;
  header.append(h2, span);
  card.append(header);

  const pre = document.createElement("pre");
  pre.className = "staged";
  pre.textContent = JSON.stringify(pending.payload, null, 2);
  card.append(pre);

  const gate = document.createElement("div");
  gate.className = "gate";

  const note = document.createElement("p");
  note.className = "bound";
  note.textContent =
    `Bound to ${pending.operator} (${pending.certificate}) · payload ` +
    `${pending.payload_sha256.slice(0, 16)}… · single use · expires ` +
    new Date(pending.expires_at).toLocaleTimeString();
  gate.append(note);

  const button = document.createElement("button");
  button.className = "confirm";
  button.type = "button";
  button.textContent = "Confirm and lodge";
  button.addEventListener("click", () => {
    button.disabled = true;
    confirmLodgement(pending.token);
  });
  gate.append(button);

  card.append(gate);
  clearEmpty();
  stream.append(card);
  scrollDown();
}

async function confirmLodgement(token) {
  setBusy(true);
  try {
    const response = await fetch("/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...identityHeaders() },
      body: JSON.stringify({ session_id: sessionId, token }),
    });
    const payload = await response.json();

    if (payload.state) {
      state = payload.state;
      renderStrip();
      renderRail();
    }

    if (!response.ok || payload.status === "refused") {
      addMessage("error", "Not lodged",
                 payload.message || payload.detail || `HTTP ${response.status}`);
      return;
    }

    if (payload.status === "accepted") {
      addMessage("system", "Lodged",
                 `${payload.action} accepted — reference ${payload.reference}`);
      toast("Lodged");
    } else {
      addMessage("error", "Rejected",
                 (payload.errors || []).join(" · ") || payload.message || "rejected");
    }
  } catch (err) {
    addMessage("error", "Error", String(err));
  } finally {
    setBusy(false);
  }
}

async function upload(file) {
  if (!file) return;
  addMessage("operator", OPERATOR.name, file.name);
  setBusy(true);
  try {
    const form = new FormData();
    form.append("file", file);
    const query = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : "";
    const response = await fetch(`/upload${query}`, {
      method: "POST", headers: identityHeaders(), body: form,
    });
    const payload = await response.json();
    if (!response.ok) {
      addMessage("error", "Error", payload.detail || `HTTP ${response.status}`);
      return;
    }
    sessionId = payload.session_id;
    localStorage.setItem("freightagent.session", sessionId);
    addMessage("system", "Stored", `${payload.filename} · ${bytes(payload.bytes)} · ${payload.path}`);
    setBusy(false);
    await send(`I have uploaded the manifest at ${payload.path}. Read it and tell me the threshold split.`);
  } catch (err) {
    addMessage("error", "Error", String(err));
  } finally {
    setBusy(false);
  }
}

/* --------------------------------------------------------------- wiring */

el("composer").addEventListener("submit", (event) => {
  event.preventDefault();
  const input = el("input");
  const message = input.value;
  input.value = "";
  input.style.height = "auto";
  send(message);
});

el("input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    el("composer").requestSubmit();
  }
});

el("input").addEventListener("input", (event) => {
  const node = event.target;
  node.style.height = "auto";
  node.style.height = `${Math.min(node.scrollHeight, 160)}px`;
});

/* Drag and drop the manifest onto the conversation. */
["dragenter", "dragover"].forEach((type) =>
  stream.addEventListener(type, (event) => {
    event.preventDefault();
    stream.classList.add("drop");
  })
);

["dragleave", "drop"].forEach((type) =>
  stream.addEventListener(type, (event) => {
    event.preventDefault();
    stream.classList.remove("drop");
  })
);

stream.addEventListener("drop", (event) => {
  const file = event.dataTransfer && event.dataTransfer.files[0];
  upload(file);
});

el("file").addEventListener("change", (event) => upload(event.target.files[0]));

el("plumbing").addEventListener("change", (event) => {
  localStorage.setItem("freightagent.plumbing", event.target.checked ? "1" : "0");
});

/* Resume the session across reloads — three lodgements span several days. */
async function resume() {
  const params = new URLSearchParams(location.search);
  el("plumbing").checked =
    params.get("plumbing") === "1" ||
    localStorage.getItem("freightagent.plumbing") === "1";
  if (!sessionId) return;
  try {
    const response = await fetch(`/session/${encodeURIComponent(sessionId)}`);
    if (!response.ok) return;
    state = await response.json();
    renderStrip();
    renderRail();
    for (const { key } of LODGEMENTS) {
      const l = state.lodgements[key];
      if (l && l.reference) acknowledged.add(l.reference);
    }
    // A resumed session can show the hops it already made, read back from
    // the loop's own audit log rather than reconstructed.
    if (el("plumbing").checked) {
      const log = await fetch(`/session/${encodeURIComponent(sessionId)}/hops`);
      if (log.ok) addHops((await log.json()).hops || []);
    }

    if (state.manifest) {
      addMessage("system", "Session resumed",
        `${sessionId} · manifest read · ` +
        `${LODGEMENTS.filter((l) => (state.lodgements[l.key] || {}).status === "accepted").length} of 3 lodged`);
      // CP-8. A resumed session may already have a lodgement staged; that card
      // is the real gate, so it wins over the browser-computed preview.
      if (state.pending_confirmation) {
        renderStagedCard(state.pending_confirmation);
      } else {
        renderPreviewCard();
      }
    }
  } catch (err) {
    void err;
  }
}

resume();
el("input").focus();
