// The call screen: type-ahead pickers, the plain-words request, benefit and chip selection, and the
// comparison. Plain JavaScript, no build step. Everything shown is HTML-escaped; the server renders
// the comparison itself.
(function () {
  "use strict";
  const root = document.querySelector(".call-screen");
  if (!root) return;

  const csrf = root.dataset.csrf;
  const LABELS = window.FEATURE_LABELS || {};
  const MAX_POLICIES = 10;
  const sel = { navy: new Map(), comp: new Map() };
  const fund = { navy: "", comp: "" };
  let features = [];
  const chips = new Set();
  let showAll = false;
  let excessChoice = "";

  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

  async function getJSON(url, options) {
    const r = await fetch(url, Object.assign({ credentials: "same-origin" }, options || {}));
    const type = r.headers.get("content-type") || "";
    if (!type.includes("application/json")) throw new Error("Your session may have ended. Reload the page.");
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || "Something went wrong.");
    return data;
  }

  // ── household, remembered per rep in this browser ───────────────────────────
  const stateSel = $("#state"), familySel = $("#family"), deps = $("#dependants");
  try {
    const saved = JSON.parse(localStorage.getItem("nc.household") || "{}");
    if (saved.state) stateSel.value = saved.state;
    if (saved.family) familySel.value = saved.family;
  } catch (e) { /* storage unavailable: defaults stand */ }
  const showDeps = () => { deps.hidden = !["family", "single_parent", "dependants_only"].includes(familySel.value); };
  showDeps();
  [stateSel, familySel].forEach((el) => el.addEventListener("change", () => {
    try { localStorage.setItem("nc.household", JSON.stringify({ state: stateSel.value, family: familySel.value })); } catch (e) {}
    showDeps();
    refresh();
  }));
  $$("input", deps).forEach((el) => el.addEventListener("change", refresh));

  // ── chosen policies ──────────────────────────────────────────────────────────
  const total = () => sel.navy.size + sel.comp.size;
  function describe(p) {
    const bits = [p.fund_name || p.fund_code];
    if (p.brand && p.brand !== p.fund_name) bits.push(p.brand);
    if (p.tier) bits.push(p.tier);
    if (p.type) bits.push(p.type);
    return bits.filter(Boolean).join(" · ");
  }
  function addPolicy(p) {
    const side = p.is_navy ? "navy" : "comp";
    if (sel[side].has(p.id)) return;
    if (total() >= MAX_POLICIES) { flash(`Up to ${MAX_POLICIES} policies at a time.`); return; }
    sel[side].set(p.id, p);
    renderChosen();
    refresh();
  }
  function removePolicy(id) {
    sel.navy.delete(id); sel.comp.delete(id);
    renderChosen();
    refresh(true);
  }
  function renderChosen() {
    ["navy", "comp"].forEach((side) => {
      const box = $(`.picker[data-side="${side}"] .chosen`);
      box.innerHTML = Array.from(sel[side].values()).map((p) =>
        `<span class="sel"><strong>${esc(p.name)}</strong> <span class="muted">${esc(describe(p))}</span>
           <button type="button" class="x" data-id="${p.id}" aria-label="Remove">×</button></span>`).join("")
        || `<span class="muted small">None chosen yet.</span>`;
      $$(".x", box).forEach((b) => b.addEventListener("click", () => removePolicy(Number(b.dataset.id))));
    });
    const go = $("#compare-go");
    go.disabled = total() === 0;
    $("#compare-hint").textContent = sel.navy.size && sel.comp.size
      ? `${sel.navy.size} Navy Health and ${sel.comp.size} competitor polic${sel.comp.size === 1 ? "y" : "ies"} chosen.`
      : sel.comp.size ? "Compare now to see the closest Navy Health policies, or add one."
      : sel.navy.size >= 2 ? "Comparing Navy Health policies with each other."
      : "Pick a Navy Health policy and at least one competitor, or two Navy Health policies.";
  }

  // ── a dropdown with keyboard navigation ──────────────────────────────────────
  function dropdown(input, box, onPick) {
    let items = [], active = -1;
    const close = () => { box.hidden = true; active = -1; };
    const highlight = () => $$(".opt", box).forEach((el, i) => el.classList.toggle("active", i === active));
    input.addEventListener("keydown", (e) => {
      if (box.hidden) return;
      if (e.key === "ArrowDown") { active = Math.min(active + 1, items.length - 1); highlight(); e.preventDefault(); }
      else if (e.key === "ArrowUp") { active = Math.max(active - 1, 0); highlight(); e.preventDefault(); }
      else if (e.key === "Enter" && active >= 0) { onPick(items[active]); e.preventDefault(); }
      else if (e.key === "Escape") close();
    });
    document.addEventListener("click", (e) => { if (!box.contains(e.target) && e.target !== input) close(); });
    return {
      show(html, list) {
        items = list; active = list.length ? 0 : -1;
        box.innerHTML = html; box.hidden = false; highlight();
        $$(".opt", box).forEach((el, i) => el.addEventListener("mousedown", (e) => { e.preventDefault(); onPick(items[i]); }));
      },
      close,
    };
  }

  // ── the two policy pickers ───────────────────────────────────────────────────
  const pickers = {};
  $$(".picker").forEach((picker) => {
    const side = picker.dataset.side;
    const input = $("input", picker), box = $(".dropdown", picker), filter = $(".fund-filter", picker);
    const dd = dropdown(input, box, (item) => {
      if (item.kind === "fund") { setFund(side, item.code, item.name); input.value = ""; load(); input.focus(); }
      else { addPolicy(item.p); input.value = ""; dd.close(); }
    });
    let latest = 0;
    async function load() {
      const q = input.value.trim();
      const mine = ++latest;   // a slower, older answer must never replace a newer one
      if (!q && !fund[side]) { dd.close(); return; }
      let data;
      try {
        data = await getJSON(`/search?side=${side}&q=${encodeURIComponent(q)}&fund=${encodeURIComponent(fund[side])}`);
      } catch (err) { if (mine === latest) dd.show(`<div class="empty">${esc(err.message)}</div>`, []); return; }
      if (mine !== latest) return;
      const list = [];
      let html = "";
      if (data.funds && data.funds.length) {
        html += `<div class="hdr">Companies</div>`;
        data.funds.forEach((f) => {
          list.push({ kind: "fund", code: f.code, name: f.name });
          const brands = (f.brands || []).filter((b) => b && b !== f.name).join(", ");
          const code = f.code.toLowerCase() === String(f.name).toLowerCase() ? "" : esc(f.code) + " · ";
          html += `<div class="opt"><strong>${esc(f.name)}</strong> <span class="muted">${code}${f.count} policies${brands ? " · also sold as " + esc(brands) : ""}${f.restricted ? " · restricted membership" : ""}</span></div>`;
        });
      }
      if (data.policies.length) {
        html += `<div class="hdr">Policies${data.total > data.policies.length ? ` <span class="muted">(${data.total} match, keep typing to narrow)</span>` : ""}</div>`;
        data.policies.forEach((p) => {
          list.push({ kind: "policy", p });
          const chosen = sel.navy.has(p.id) || sel.comp.has(p.id);
          html += `<div class="opt${chosen ? " chosen-opt" : ""}"><strong>${esc(p.name)}</strong> <span class="muted">${esc(describe(p))}${chosen ? " · chosen" : ""}</span></div>`;
        });
      }
      if (!list.length) html = `<div class="empty">No match. Try the company name, or fewer words.</div>`;
      dd.show(html, list);
    }
    function setFund(s, code, name) {
      fund[s] = code;
      if (filter) {
        filter.hidden = !code;
        $("span", filter).textContent = code ? `Showing ${name} policies` : "";
      }
    }
    input.addEventListener("input", debounce(load, 120));
    input.addEventListener("focus", () => { if (input.value || fund[side]) load(); });
    if (filter) $(".clear-fund", filter).addEventListener("click", () => { setFund(side, "", ""); input.focus(); load(); });
    pickers[side] = { input, load, setFund: (code, name) => setFund(side, code, name) };
  });

  // ── benefits ─────────────────────────────────────────────────────────────────
  const benefitInput = $("#benefit-text"), benefitBox = $(".benefit-input .dropdown");
  const bdd = dropdown(benefitInput, benefitBox, (item) => { addFeatures([item.key]); benefitInput.value = ""; bdd.close(); });
  let latestBenefit = 0;
  benefitInput.addEventListener("input", debounce(async () => {
    const q = benefitInput.value.trim();
    const mine = ++latestBenefit;
    if (!q) { bdd.close(); return; }
    try {
      const data = await getJSON(`/search/benefits?q=${encodeURIComponent(q)}`);
      if (mine !== latestBenefit) return;
      const list = data.benefits.map((b) => ({ key: b.key }));
      bdd.show(data.benefits.length
        ? data.benefits.map((b) => `<div class="opt">${esc(b.label)} <span class="muted">${esc(b.group)}</span></div>`).join("")
        : `<div class="empty">No benefit by that name.</div>`, list);
    } catch (err) { bdd.show(`<div class="empty">${esc(err.message)}</div>`, []); }
  }, 100));
  function addFeatures(keys) {
    keys.forEach((k) => { if (LABELS[k] && !features.includes(k)) features.push(k); });
    renderFeatures(); refresh();
  }
  function renderFeatures() {
    const box = $("#benefit-chosen");
    box.innerHTML = features.map((k) =>
      `<span class="sel">${esc(LABELS[k] || k)} <button type="button" class="x" data-key="${esc(k)}" aria-label="Remove">×</button></span>`).join("")
      + (features.length ? ` <button type="button" class="link-btn" id="clear-benefits">clear all</button>` : "");
    $$(".x", box).forEach((b) => b.addEventListener("click", () => {
      features = features.filter((k) => k !== b.dataset.key); renderFeatures(); refresh();
    }));
    const clear = $("#clear-benefits");
    if (clear) clear.addEventListener("click", () => { features = []; renderFeatures(); refresh(); });
  }
  $$(".quick-add").forEach((b) => b.addEventListener("click", () => addFeatures(b.dataset.keys.split(","))));

  // ── member-need chips ────────────────────────────────────────────────────────
  $$(".chip").forEach((b) => b.addEventListener("click", () => {
    const k = b.dataset.chip;
    if (chips.has(k)) chips.delete(k); else chips.add(k);
    b.classList.toggle("on", chips.has(k));
    refresh();
  }));

  // ── the plain-words request ──────────────────────────────────────────────────
  const askText = $("#ask-text"), askBtn = $("#ask-go"), askOut = $("#ask-result");
  async function ask() {
    const text = askText.value.trim();
    if (!text) return;
    askBtn.disabled = true; askBtn.textContent = "Reading…";
    askOut.hidden = false; askOut.innerHTML = `<p class="muted">Finding the policies…</p>`;
    const body = new FormData();
    body.append("csrf", csrf); body.append("request", text);
    try {
      const data = await getJSON("/call/plan", { method: "POST", body });
      if (data.state) stateSel.value = data.state;
      if (data.family) { familySel.value = data.family; showDeps(); }
      if (data.features && data.features.length) addFeatures(data.features.map((f) => f.key));
      renderSuggestions(data);
    } catch (err) {
      askOut.innerHTML = `<p class="error">${esc(err.message)}</p>`;
    } finally {
      askBtn.disabled = false; askBtn.textContent = "Find";
    }
  }
  function renderSuggestions(data) {
    const groups = [...data.navy.map((g) => [g, "Navy Health"]), ...data.comp.map((g) => [g, "Competitor"])];
    if (!data.understood || !groups.length) {
      askOut.innerHTML = `<p class="warn">I couldn't tell which policies you mean. Try a company or policy name in the boxes below.</p>`;
      return;
    }
    const picked = [];
    let html = `<p class="muted small">Tap the policies you want. Ones I'm sure of are already added.</p>`;
    groups.forEach(([g, side], gi) => {
      html += `<div class="sugg"><div><strong>${esc(side)}: “${esc(g.term)}”</strong> <span class="muted small">${g.total} match${g.total === 1 ? "" : "es"}</span></div>`;
      (g.funds || []).forEach((f) => {
        html += `<button type="button" class="pill fund-pill" data-code="${esc(f.code)}" data-name="${esc(f.name)}">All ${esc(f.name)} policies…</button>`;
      });
      g.policies.forEach((p, pi) => {
        if (g.pick.includes(p.id)) picked.push(p);
        html += `<button type="button" class="pill add-sugg" data-g="${gi}" data-p="${pi}">+ ${esc(p.name)} <span class="muted">${esc(describe(p))}</span></button>`;
      });
      if (!g.policies.length && !(g.funds || []).length) html += `<span class="muted small">Nothing matched; try other words.</span>`;
      html += `</div>`;
    });
    askOut.innerHTML = html;
    $$(".add-sugg", askOut).forEach((b) => b.addEventListener("click", () => {
      const [g] = groups[Number(b.dataset.g)];
      addPolicy(g.policies[Number(b.dataset.p)]);
      b.classList.add("done");
    }));
    $$(".fund-pill", askOut).forEach((b) => b.addEventListener("click", () => {
      pickers.comp.setFund(b.dataset.code, b.dataset.name);
      pickers.comp.input.value = ""; pickers.comp.input.focus(); pickers.comp.load();
    }));
    picked.forEach(addPolicy);
  }
  askBtn.addEventListener("click", ask);
  askText.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); ask(); } });

  // ── the comparison ───────────────────────────────────────────────────────────
  const result = $("#result");
  let compared = false;
  function params() {
    const ids = [...sel.navy.keys(), ...sel.comp.keys()];
    const p = new URLSearchParams({ ids: ids.join(","), state: stateSel.value, family: familySel.value });
    if (!deps.hidden) p.set("titles", $$("input:checked", deps).map((i) => i.value).join(","));
    if (features.length) p.set("features", features.join(","));
    if (chips.size) p.set("chips", Array.from(chips).join(","));
    if (showAll) p.set("all", "1");
    if (excessChoice) p.set("excess", excessChoice);
    return p;
  }
  let latestCard = 0;
  async function compare() {
    if (!total()) { result.innerHTML = ""; compared = false; return; }
    const mine = ++latestCard;
    result.classList.add("loading");
    try {
      const r = await fetch(`/card?${params()}`, { credentials: "same-origin" });
      const html = await r.text();
      if (mine !== latestCard) return;
      result.innerHTML = html;
      compared = true;
      wireResult();
    } catch (err) {
      result.innerHTML = `<p class="error">${esc(err.message)}</p>`;
    } finally {
      result.classList.remove("loading");
    }
  }
  function refresh(force) { if (compared || force) compare(); }
  function wireResult() {
    $$(".add-policy", result).forEach((b) => b.addEventListener("click", () =>
      addPolicy({ id: Number(b.dataset.id), name: b.dataset.name, is_navy: b.dataset.navy === "1", fund_name: "Navy Health" })));
    $$(".remove-policy", result).forEach((b) => b.addEventListener("click", () => removePolicy(Number(b.dataset.id))));
    $$(".excess-choice", result).forEach((b) => b.addEventListener("click", () => {
      excessChoice = b.dataset.excess; compare();
    }));
    $$(".talk-go", result).forEach((b) => b.addEventListener("click", () => talk(b)));
    const toggle = $("#toggle-all", result);
    if (toggle) toggle.addEventListener("click", () => { showAll = toggle.dataset.all === "1"; compare(); });
  }
  $("#compare-go").addEventListener("click", compare);

  // ── the talking point for one card ───────────────────────────────────────────
  async function talk(button) {
    const out = button.parentElement.querySelector(".talk-text");
    button.disabled = true; button.textContent = "Writing…";
    out.hidden = false; out.innerHTML = `<p class="muted">Writing from the card's facts…</p>`;
    const body = new FormData();
    body.append("csrf", csrf);
    body.append("navy", button.dataset.navy);
    body.append("comp", button.dataset.comp);
    params().forEach((v, k) => { if (k !== "ids") body.append(k, v); });
    try {
      const data = await getJSON("/script", { method: "POST", body });
      const checked = data.outcome === "pass" ? "Checked against the card's facts."
        : data.outcome === "partial" ? "Some sentences were dropped because they couldn't be checked."
        : data.outcome === "fallback" ? "The written version didn't pass the checks, so here are the facts."
        : "";
      out.innerHTML = `<p class="talk-body">${esc(data.text).split("\n").join("<br>")}</p>` +
        (checked ? `<p class="muted small">${esc(checked)} Premiums are before the government rebate.</p>` : "");
    } catch (err) {
      out.innerHTML = `<p class="error">${esc(err.message)}</p>`;
    } finally {
      button.disabled = false; button.textContent = "Write it again";
    }
  }

  function flash(msg) {
    const hint = $("#compare-hint");
    hint.textContent = msg; hint.classList.add("warn");
    setTimeout(() => hint.classList.remove("warn"), 2500);
  }

  renderChosen();
  renderFeatures();
})();
