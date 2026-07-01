/* ---------------------------------------------------------------------------
 * Ledger — tiny vanilla-JS single-page front end for the Event-Sourced
 * Ledger API. No build step: open the page, it talks to /api/v1/* directly.
 * ------------------------------------------------------------------------- */

const API = "/api/v1";

const state = {
  token: localStorage.getItem("ledger_token") || null,
  user: null,
  accounts: [],
  view: "loading", // loading | auth | dashboard | account
  authTab: "login",
  selectedAccountId: null,
  accountDetail: null, // { account, balance, audit, txHistory }
  banner: null, // { type: 'error'|'success', text }
  modal: null, // 'new-account' | 'transfer' | 'journal' | 'tx-detail' | 'point-in-time'
  modalData: {},
};

const root = document.getElementById("app");

// ---------------------------------------------------------------------------
// API helper
// ---------------------------------------------------------------------------

async function api(method, path, { body, form, auth = true } = {}) {
  const headers = {};
  if (auth && state.token) headers["Authorization"] = `Bearer ${state.token}`;
  let payload;
  if (form) {
    payload = new URLSearchParams(form);
    headers["Content-Type"] = "application/x-www-form-urlencoded";
  } else if (body) {
    payload = JSON.stringify(body);
    headers["Content-Type"] = "application/json";
  }
  const resp = await fetch(`${API}${path}`, { method, headers, body: payload });
  let data = null;
  try {
    data = await resp.json();
  } catch (e) {
    /* empty body (e.g. 204) */
  }
  if (!resp.ok) {
    const msg = (data && (data.detail || data.message)) || `Request failed (${resp.status})`;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data;
}

function money(v, currency = "USD") {
  const n = Number(v);
  const sign = n < 0 ? "-" : "";
  return `${sign}${currency} ${Math.abs(n).toFixed(2)}`;
}

function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

function setBanner(type, text) {
  state.banner = { type, text };
  render();
  if (type === "success") {
    setTimeout(() => {
      if (state.banner && state.banner.text === text) {
        state.banner = null;
        render();
      }
    }, 3500);
  }
}

function closeModal() {
  state.modal = null;
  state.modalData = {};
  render();
}

// ---------------------------------------------------------------------------
// Bootstrap / auth
// ---------------------------------------------------------------------------

async function boot() {
  if (!state.token) {
    state.view = "auth";
    render();
    return;
  }
  try {
    state.user = await api("GET", "/auth/me");
    await loadAccounts();
    state.view = "dashboard";
  } catch (e) {
    logout(false);
  }
  render();
}

async function loadAccounts() {
  const accounts = await api("GET", "/accounts");
  // Fetch balances in parallel so the dashboard shows real numbers.
  const withBalances = await Promise.all(
    accounts.map(async (a) => {
      try {
        const bal = await api("GET", `/accounts/${a.id}/balance`);
        return { ...a, _balance: bal.balance, _fromSnapshot: bal.from_snapshot };
      } catch (e) {
        return { ...a, _balance: null };
      }
    })
  );
  state.accounts = withBalances;
}

function logout(doRender = true) {
  state.token = null;
  state.user = null;
  state.accounts = [];
  state.view = "auth";
  localStorage.removeItem("ledger_token");
  if (doRender) render();
}

async function handleRegister(e) {
  e.preventDefault();
  const f = e.target;
  try {
    await api("POST", "/auth/register", {
      auth: false,
      body: {
        username: f.username.value.trim(),
        email: f.email.value.trim(),
        password: f.password.value,
      },
    });
    setBanner("success", "Account created — now log in below.");
    state.authTab = "login";
    render();
  } catch (err) {
    setBanner("error", err.message);
  }
}

async function handleLogin(e) {
  e.preventDefault();
  const f = e.target;
  try {
    const tok = await api("POST", "/auth/login", {
      auth: false,
      form: { username: f.username.value.trim(), password: f.password.value },
    });
    state.token = tok.access_token;
    localStorage.setItem("ledger_token", state.token);
    state.banner = null;
    await boot();
  } catch (err) {
    setBanner("error", err.message);
  }
}

// ---------------------------------------------------------------------------
// Account actions
// ---------------------------------------------------------------------------

async function handleCreateAccount(e) {
  e.preventDefault();
  const f = e.target;
  try {
    await api("POST", "/accounts", {
      body: {
        name: f.name.value.trim(),
        account_type: f.account_type.value,
        currency: f.currency.value.trim().toUpperCase() || "USD",
        overdraft_limit: f.overdraft_limit.value || "0",
      },
    });
    closeModal();
    await loadAccounts();
    setBanner("success", "Account opened.");
    render();
  } catch (err) {
    setBanner("error", err.message);
  }
}

async function openAccount(id) {
  state.selectedAccountId = id;
  state.view = "account";
  state.accountDetail = null;
  render();
  await refreshAccountDetail();
}

async function refreshAccountDetail() {
  const id = state.selectedAccountId;
  try {
    const [account, balance, audit, txHistory] = await Promise.all([
      api("GET", `/accounts/${id}`),
      api("GET", `/accounts/${id}/balance`),
      api("GET", `/accounts/${id}/audit`),
      api("GET", `/transactions/account/${id}?page=1&page_size=50`),
    ]);
    state.accountDetail = { account, balance, audit, txHistory };
  } catch (err) {
    setBanner("error", err.message);
  }
  render();
}

async function handleCreateSnapshot() {
  try {
    await api("POST", `/accounts/${state.selectedAccountId}/snapshots`);
    setBanner("success", "Snapshot created.");
    await refreshAccountDetail();
  } catch (err) {
    setBanner("error", err.message);
  }
}

async function handleCloseAccount() {
  if (!confirm("Close this account? Its history stays intact, but it can no longer be used.")) return;
  try {
    await api("DELETE", `/accounts/${state.selectedAccountId}`);
    setBanner("success", "Account closed.");
    await loadAccounts();
    state.view = "dashboard";
    render();
  } catch (err) {
    setBanner("error", err.message);
  }
}

async function handlePointInTime(e) {
  e.preventDefault();
  const f = e.target;
  const asOf = new Date(f.as_of.value);
  try {
    const result = await api(
      "GET",
      `/accounts/${state.selectedAccountId}/balance/history?as_of=${encodeURIComponent(asOf.toISOString())}`
    );
    state.modalData.result = result;
    render();
  } catch (err) {
    setBanner("error", err.message);
  }
}

// ---------------------------------------------------------------------------
// Transaction actions
// ---------------------------------------------------------------------------

async function handleTransfer(e) {
  e.preventDefault();
  const f = e.target;
  try {
    await api("POST", "/transactions/transfer", {
      body: {
        from_account_id: f.from_account_id.value,
        to_account_id: f.to_account_id.value,
        amount: f.amount.value,
        currency: f.currency.value.trim().toUpperCase() || "USD",
        description: f.description.value.trim(),
      },
    });
    closeModal();
    setBanner("success", "Transfer completed.");
    if (state.view === "account") await refreshAccountDetail();
    await loadAccounts();
    render();
  } catch (err) {
    setBanner("error", err.message);
  }
}

function addJournalLeg() {
  state.modalData.legs = state.modalData.legs || [];
  state.modalData.legs.push({ account_id: "", entry_type: "DEBIT", amount: "" });
  render();
}

function removeJournalLeg(i) {
  state.modalData.legs.splice(i, 1);
  render();
}

async function handleJournal(e) {
  e.preventDefault();
  const f = e.target;
  const legs = state.modalData.legs.map((_, i) => ({
    account_id: f[`leg_account_${i}`].value,
    entry_type: f[`leg_type_${i}`].value,
    amount: f[`leg_amount_${i}`].value,
    currency: "USD",
  }));
  try {
    await api("POST", "/transactions/journal", {
      body: { description: f.description.value.trim(), legs },
    });
    closeModal();
    setBanner("success", "Journal entry posted.");
    if (state.view === "account") await refreshAccountDetail();
    await loadAccounts();
    render();
  } catch (err) {
    setBanner("error", err.message);
  }
}

async function openTransaction(txId) {
  try {
    const tx = await api("GET", `/transactions/${txId}`);
    state.modal = "tx-detail";
    state.modalData = { tx };
    render();
  } catch (err) {
    setBanner("error", err.message);
  }
}

async function handleReverse() {
  const reason = prompt("Why are you reversing this transaction?", "Entered in error");
  if (reason === null) return;
  try {
    await api("POST", `/transactions/${state.modalData.tx.id}/reverse`, { body: { reason } });
    setBanner("success", "Transaction reversed.");
    closeModal();
    if (state.view === "account") await refreshAccountDetail();
    await loadAccounts();
    render();
  } catch (err) {
    setBanner("error", err.message);
  }
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function bannerHtml() {
  if (!state.banner) return "";
  const cls = state.banner.type === "error" ? "error-box" : "success-box";
  return `<div class="${cls}">${escapeHtml(state.banner.text)}</div>`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function renderAuth() {
  root.innerHTML = `
    <div class="auth-wrap">
      <div class="card auth-card">
        <div class="brand" style="margin-bottom:18px;">
          <span class="brand-dot"></span> Ledger
          <small>event-sourced bookkeeping</small>
        </div>
        <div class="tabs">
          <button class="${state.authTab === "login" ? "active" : ""}" onclick="state.authTab='login'; state.banner=null; render()">Log in</button>
          <button class="${state.authTab === "register" ? "active" : ""}" onclick="state.authTab='register'; state.banner=null; render()">Create account</button>
        </div>
        ${state.authTab === "login" ? `
          <form onsubmit="handleLogin(event)">
            <label>Username</label>
            <input name="username" required autocomplete="username" />
            <label>Password</label>
            <input name="password" type="password" required autocomplete="current-password" />
            <button class="btn block" type="submit">Log in</button>
          </form>
        ` : `
          <form onsubmit="handleRegister(event)">
            <label>Username</label>
            <input name="username" required minlength="3" />
            <label>Email</label>
            <input name="email" type="email" required />
            <label>Password</label>
            <input name="password" type="password" required minlength="8" />
            <button class="btn block" type="submit">Create account</button>
          </form>
        `}
        ${bannerHtml()}
      </div>
    </div>
  `;
}

function accountIcon(type) {
  const map = { ASSET: "A", LIABILITY: "L", EQUITY: "E", REVENUE: "R", EXPENSE: "X" };
  const colors = {
    ASSET: "rgba(91,141,255,0.18)", LIABILITY: "rgba(248,113,113,0.18)",
    EQUITY: "rgba(124,91,255,0.18)", REVENUE: "rgba(52,211,153,0.18)",
    EXPENSE: "rgba(251,191,36,0.18)",
  };
  return `<div class="acc-icon" style="background:${colors[type]}">${map[type] || "?"}</div>`;
}

function renderDashboard() {
  const total = state.accounts
    .filter((a) => a.account_type === "ASSET")
    .reduce((sum, a) => sum + Number(a._balance || 0), 0);
  const liabilities = state.accounts
    .filter((a) => a.account_type === "LIABILITY")
    .reduce((sum, a) => sum + Number(a._balance || 0), 0);

  root.innerHTML = `
    <div class="topbar">
      <div class="brand"><span class="brand-dot"></span> Ledger <small>event-sourced bookkeeping</small></div>
      <div class="user-pill">
        👤 ${escapeHtml(state.user?.username || "")}
        <button onclick="logout()">Log out</button>
      </div>
    </div>
    ${bannerHtml()}
    <div class="summary-strip">
      <div class="stat"><div class="label">Accounts</div><div class="value">${state.accounts.length}</div></div>
      <div class="stat"><div class="label">Total assets</div><div class="value">${money(total)}</div></div>
      <div class="stat"><div class="label">Total owed (liabilities)</div><div class="value">${money(liabilities)}</div></div>
    </div>

    <div class="card">
      <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px;">
        <div>
          <h2>Your accounts</h2>
          <p class="hint">Every balance below is computed live from the full history of events — nothing is a stored, editable number.</p>
        </div>
        <div style="display:flex; gap:8px;">
          <button class="btn secondary small" onclick="state.modal='new-account'; render()">+ New account</button>
          <button class="btn small" onclick="state.modal='transfer'; render()" ${state.accounts.length < 2 ? "disabled" : ""}>⇄ Transfer money</button>
          <button class="btn secondary small" onclick="state.modal='journal'; state.modalData.legs=[{},{}]; render()" ${state.accounts.length < 2 ? "disabled" : ""}>Manual journal</button>
        </div>
      </div>
      <div style="margin-top:16px;">
        ${state.accounts.length === 0 ? `
          <div class="empty-state">
            <div class="big">🏦</div>
            <div>No accounts yet. Open one to get started.</div>
          </div>
        ` : state.accounts.map((a) => `
          <div class="account-row" onclick="openAccount('${a.id}')">
            <div class="account-left">
              ${accountIcon(a.account_type)}
              <div>
                <div class="acc-name">${escapeHtml(a.name)}</div>
                <div class="acc-sub">
                  <span class="badge ${a.account_type}">${a.account_type}</span>
                  <span class="badge ${a.status}">${a.status}</span>
                </div>
              </div>
            </div>
            <div class="acc-balance">
              ${a._balance === null ? "—" : money(a._balance, a.currency)}
              <small>${a.currency}${a.overdraft_limit && Number(a.overdraft_limit) > 0 ? ` · overdraft up to ${money(a.overdraft_limit, a.currency)}` : ""}</small>
            </div>
          </div>
        `).join("")}
      </div>
    </div>
    <div class="footer-note">Everything you see is a live read from the ledger's event log — reload any time, it never gets out of sync.</div>
  `;
  renderModal();
}

function renderAccountPage() {
  const d = state.accountDetail;
  root.innerHTML = `
    <div class="topbar">
      <div class="brand"><span class="brand-dot"></span> Ledger <small>event-sourced bookkeeping</small></div>
      <div class="user-pill">👤 ${escapeHtml(state.user?.username || "")} <button onclick="logout()">Log out</button></div>
    </div>
    <span class="back-link" onclick="state.view='dashboard'; render()">← Back to all accounts</span>
    ${bannerHtml()}
    ${!d ? `<div class="loading">Loading account…</div>` : renderAccountBody(d)}
  `;
  renderModal();
}

function renderAccountBody(d) {
  const { account, balance, audit, txHistory } = d;
  return `
    <div class="card">
      <div style="display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:14px;">
        <div>
          <h2 style="display:flex; align-items:center; gap:10px;">
            ${escapeHtml(account.name)}
            <span class="badge ${account.account_type}">${account.account_type}</span>
            <span class="badge ${account.status}">${account.status}</span>
          </h2>
          <p class="hint">Opened ${fmtDate(account.created_at)} · Currency ${account.currency}${Number(account.overdraft_limit) > 0 ? ` · overdraft limit ${money(account.overdraft_limit, account.currency)}` : ""}</p>
        </div>
        <div style="text-align:right;">
          <div style="font-size:13px; color:var(--muted);">Current balance ${balance.from_snapshot ? "(accelerated via snapshot)" : "(replayed from events)"}</div>
          <div style="font-size:30px; font-weight:800;">${money(balance.balance, balance.currency)}</div>
        </div>
      </div>
      <div style="display:flex; gap:8px; margin-top:16px; flex-wrap:wrap;">
        <button class="btn small" onclick="state.modal='transfer'; state.modalData={from:'${account.id}'}; render()">⇄ Transfer from this account</button>
        <button class="btn secondary small" onclick="state.modal='point-in-time'; state.modalData={}; render()">🕒 Balance at a past date</button>
        <button class="btn secondary small" onclick="handleCreateSnapshot()">📸 Create snapshot</button>
        ${account.status === "OPEN" ? `<button class="btn danger small" onclick="handleCloseAccount()">Close account</button>` : ""}
      </div>
    </div>

    <div class="grid grid-2">
      <div class="card">
        <h2>Audit trail</h2>
        <p class="hint">Every event that ever touched this account, in order, with the running balance after each one. This is the permanent record — nothing here can be edited.</p>
        ${audit.entries.length === 0 ? `<div class="empty-state">No activity yet.</div>` : `
        <table>
          <thead><tr><th>#</th><th>When</th><th>Type</th><th>Amount</th><th>Running balance</th></tr></thead>
          <tbody>
            ${audit.entries.map((e) => `
              <tr>
                <td class="mono">${e.sequence}</td>
                <td>${fmtDate(e.occurred_at)}</td>
                <td><span class="badge ${e.entry_type}">${e.entry_type}</span></td>
                <td>${money(e.amount, e.currency)}</td>
                <td><strong>${money(e.running_balance, e.currency)}</strong></td>
              </tr>
            `).join("")}
          </tbody>
        </table>
        `}
      </div>

      <div class="card">
        <h2>Transactions</h2>
        <p class="hint">Click one to see both legs of the double-entry, or reverse it.</p>
        ${txHistory.items.length === 0 ? `<div class="empty-state">No transactions yet.</div>` : `
        <table>
          <thead><tr><th>When</th><th>Description</th><th>Status</th></tr></thead>
          <tbody>
            ${txHistory.items.map((t) => `
              <tr style="cursor:pointer" onclick="openTransaction('${t.id}')">
                <td>${fmtDate(t.created_at)}</td>
                <td>${escapeHtml(t.description)}</td>
                <td><span class="badge ${t.status}">${t.status}</span></td>
              </tr>
            `).join("")}
          </tbody>
        </table>
        `}
      </div>
    </div>
  `;
}

// ---------------------------------------------------------------------------
// Modals
// ---------------------------------------------------------------------------

function accountOptions(selected) {
  return state.accounts
    .map((a) => `<option value="${a.id}" ${a.id === selected ? "selected" : ""}>${escapeHtml(a.name)} (${a.account_type}, ${a.currency})</option>`)
    .join("");
}

function renderModal() {
  if (!state.modal) return;
  let inner = "";

  if (state.modal === "new-account") {
    inner = `
      <div class="modal-head"><h3>Open a new account</h3><span onclick="closeModal()">✕</span></div>
      <form onsubmit="handleCreateAccount(event)">
        <label>Account name</label>
        <input name="name" placeholder="e.g. My Checking" required />
        <label>Account type</label>
        <select name="account_type">
          <option value="ASSET">Asset — money you own (checking, savings)</option>
          <option value="LIABILITY">Liability — money you owe (credit card, loan)</option>
          <option value="EQUITY">Equity</option>
          <option value="REVENUE">Revenue — incoming money source</option>
          <option value="EXPENSE">Expense — outgoing spend category</option>
        </select>
        <div class="two-col">
          <div><label>Currency</label><input name="currency" value="USD" maxlength="3" /></div>
          <div><label>Overdraft limit</label><input name="overdraft_limit" value="0" type="number" step="0.01" min="0" /></div>
        </div>
        <button class="btn block" type="submit">Open account</button>
      </form>
    `;
  } else if (state.modal === "transfer") {
    inner = `
      <div class="modal-head"><h3>Transfer money</h3><span onclick="closeModal()">✕</span></div>
      <p class="hint">Creates a matched debit + credit pair — the two legs always net to zero.</p>
      <form onsubmit="handleTransfer(event)">
        <label>From account</label>
        <select name="from_account_id" required>${accountOptions(state.modalData.from)}</select>
        <label>To account</label>
        <select name="to_account_id" required>${accountOptions()}</select>
        <div class="two-col">
          <div><label>Amount</label><input name="amount" type="number" step="0.01" min="0.01" required /></div>
          <div><label>Currency</label><input name="currency" value="USD" maxlength="3" /></div>
        </div>
        <label>Description</label>
        <input name="description" placeholder="What's this for?" required />
        <button class="btn block" type="submit">Send transfer</button>
      </form>
    `;
  } else if (state.modal === "journal") {
    state.modalData.legs = state.modalData.legs || [{}, {}];
    inner = `
      <div class="modal-head"><h3>Manual journal entry</h3><span onclick="closeModal()">✕</span></div>
      <p class="hint">For accountants: add any number of legs. Total debits must equal total credits.</p>
      <form onsubmit="handleJournal(event)">
        <label>Description</label>
        <input name="description" placeholder="e.g. Month-end accrual" required />
        <div style="margin-top:10px;">
          ${state.modalData.legs.map((leg, i) => `
            <div class="leg-row">
              <div>
                <label>Account</label>
                <select name="leg_account_${i}" required>${accountOptions(leg.account_id)}</select>
              </div>
              <div style="flex:0 0 100px;">
                <label>Type</label>
                <select name="leg_type_${i}">
                  <option value="DEBIT" ${leg.entry_type === "DEBIT" ? "selected" : ""}>Debit</option>
                  <option value="CREDIT" ${leg.entry_type === "CREDIT" ? "selected" : ""}>Credit</option>
                </select>
              </div>
              <div style="flex:0 0 110px;">
                <label>Amount</label>
                <input name="leg_amount_${i}" type="number" step="0.01" min="0.01" required />
              </div>
              <span class="leg-remove" onclick="removeJournalLeg(${i})">✕</span>
            </div>
          `).join("")}
        </div>
        <button type="button" class="btn secondary small" onclick="addJournalLeg()">+ Add leg</button>
        <button class="btn block" type="submit">Post journal entry</button>
      </form>
    `;
  } else if (state.modal === "point-in-time") {
    const result = state.modalData.result;
    inner = `
      <div class="modal-head"><h3>Balance at a past date</h3><span onclick="closeModal()">✕</span></div>
      <p class="hint">See exactly what this account's balance was at any moment in history.</p>
      <form onsubmit="handlePointInTime(event)">
        <label>As of date &amp; time</label>
        <input name="as_of" type="datetime-local" required />
        <button class="btn block" type="submit">Look up balance</button>
      </form>
      ${result ? `
        <div class="success-box" style="margin-top:14px;">
          Balance as of ${fmtDate(result.as_of)}: <strong>${money(result.balance, result.currency)}</strong>
          <br/><span style="color:var(--muted)">(${result.entry_count} event${result.entry_count === 1 ? "" : "s"} applied)</span>
        </div>
      ` : ""}
    `;
  } else if (state.modal === "tx-detail") {
    const tx = state.modalData.tx;
    inner = `
      <div class="modal-head"><h3>Transaction detail</h3><span onclick="closeModal()">✕</span></div>
      <p class="hint mono">${tx.id}</p>
      <p><strong>${escapeHtml(tx.description)}</strong><br/>
      <span class="badge ${tx.status}">${tx.status}</span> · ${fmtDate(tx.created_at)}</p>
      <table>
        <thead><tr><th>Account</th><th>Type</th><th>Amount</th></tr></thead>
        <tbody>
          ${tx.entries.map((e) => `
            <tr>
              <td class="mono">${e.account_id.slice(0, 8)}…</td>
              <td><span class="badge ${e.entry_type}">${e.entry_type}</span></td>
              <td>${money(e.amount, e.currency)}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
      ${tx.status === "COMMITTED" ? `<button class="btn danger block" onclick="handleReverse()">Reverse this transaction</button>` : `<p class="hint" style="margin-top:12px;">This transaction has already been reversed or is not eligible for reversal.</p>`}
    `;
  }

  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `<div class="modal">${inner}</div>`;
  overlay.addEventListener("mousedown", (e) => {
    if (e.target === overlay) closeModal();
  });
  root.appendChild(overlay);
}

function render() {
  if (state.view === "loading") {
    root.innerHTML = `<div class="loading">Loading…</div>`;
  } else if (state.view === "auth") {
    renderAuth();
  } else if (state.view === "dashboard") {
    renderDashboard();
  } else if (state.view === "account") {
    renderAccountPage();
  }
}

boot();
