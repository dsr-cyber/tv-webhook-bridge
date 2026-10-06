"use strict";

const $ = (id) => document.getElementById(id);
let token = readToken();
let source = null;
let killOn = false;

function readToken() {
  try { return localStorage.getItem("bridgeToken") || ""; } catch { return ""; }
}

function saveToken(value) {
  try { value ? localStorage.setItem("bridgeToken", value) : localStorage.removeItem("bridgeToken"); } catch {}
}

function money(n) {
  if (n === null || n === undefined) return "-";
  const sign = n > 0 ? "+" : n < 0 ? "-" : "";
  return sign + Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function price(n) {
  return n === null || n === undefined ? "-" : Number(n).toFixed(2);
}

function tone(el, n) {
  el.classList.toggle("pos", n > 0);
  el.classList.toggle("neg", n < 0);
}

function cell(text, cls) {
  const td = document.createElement("td");
  td.textContent = text;
  if (cls) td.className = cls;
  return td;
}

function fillTable(tbody, rows, cols, emptyText) {
  tbody.replaceChildren();
  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = cell(emptyText, "empty");
    td.colSpan = cols;
    tr.append(td);
    tbody.append(tr);
    return;
  }
  for (const cells of rows) {
    const tr = document.createElement("tr");
    tr.append(...cells);
    tbody.append(tr);
  }
}

function render(s) {
  const err = $("broker-error");
  if (s.error) {
    err.textContent = "Broker unreachable: " + s.error;
    err.hidden = false;
    return;
  }
  err.hidden = true;

  $("broker").textContent = s.broker;
  setKill(s.kill_switch);

  for (const key of ["realized", "unrealized", "total"]) {
    const el = $("pnl-" + key);
    el.textContent = money(s.pnl[key]);
    tone(el, s.pnl[key]);
  }

  fillTable($("positions"), s.positions.map((p) => {
    const u = cell(money(p.unrealized), "r");
    tone(u, p.unrealized);
    return [cell(p.symbol), cell(p.qty, "r"), cell(price(p.avg_price), "r"), cell(price(p.last), "r"), u];
  }), 5, "Flat");

  fillTable($("orders"), s.orders.map((o) => {
    const px = o.type === "stop" ? o.stop_price : o.limit_price;
    const type = o.parent_id ? o.type + " (leg)" : o.type;
    return [cell(o.symbol), cell(o.side), cell(o.qty, "r"), cell(type), cell(price(px), "r"),
      cell(o.status), cell(o.id, "mono")];
  }), 7, "No working orders");

  fillTable($("decisions"), s.decisions.map((d) => {
    const when = new Date(d.ts).toLocaleTimeString([], { hour12: false });
    const badge = document.createElement("span");
    badge.className = "badge " + d.outcome;
    badge.textContent = d.rule ? d.outcome + ": " + d.rule : d.outcome;
    const outcome = document.createElement("td");
    outcome.append(badge);
    return [cell(when, "mono"), outcome, cell(d.symbol || ""), cell(d.side || ""),
      cell(d.qty ?? "", "r"), cell(d.reason || "")];
  }), 6, "Nothing yet");
}

function setKill(on) {
  killOn = on;
  const btn = $("kill");
  btn.disabled = false;
  btn.classList.toggle("on", on);
  btn.textContent = on ? "Kill switch ON: entries blocked" : "Kill switch off";
}

function setConn(state) {
  const el = $("conn");
  el.className = "conn " + state;
  el.textContent = { live: "live", down: "reconnecting", "": "connecting" }[state];
}

function connect() {
  if (source) source.close();
  source = new EventSource("/events?token=" + encodeURIComponent(token));
  source.onopen = () => setConn("live");
  source.onmessage = (e) => render(JSON.parse(e.data));
  // EventSource reconnects by itself; just show that it's trying.
  source.onerror = () => setConn("down");
}

async function start() {
  if (!token) return showLogin(false);
  const r = await fetch("/api/state", { headers: { "X-Dashboard-Token": token } });
  if (r.status === 401) return showLogin(true);
  $("login").hidden = true;
  $("main").hidden = false;
  render(await r.json());
  connect();
}

function showLogin(failed) {
  saveToken("");
  token = "";
  $("main").hidden = true;
  $("login").hidden = false;
  $("login-error").hidden = !failed;
  $("token").focus();
}

$("login").addEventListener("submit", (e) => {
  e.preventDefault();
  token = $("token").value.trim();
  saveToken(token);
  start();
});

$("kill").addEventListener("click", async () => {
  const turnOn = !killOn;
  if (!turnOn && !confirm("Turn the kill switch off and allow new entries again?")) return;
  const btn = $("kill");
  btn.disabled = true;
  try {
    const r = await fetch("/kill", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Dashboard-Token": token },
      body: JSON.stringify({ on: turnOn }),
    });
    if (r.status === 401) return showLogin(true);
    if (r.ok) setKill((await r.json()).kill_switch);
  } finally {
    btn.disabled = false;
  }
});

start();
