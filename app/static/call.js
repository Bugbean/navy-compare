// The call screen: one bar the rep types into, an editable sentence of what was understood, and the
// comparison that answers it.
//
// Speed comes from doing almost everything here, in the browser:
//   - the index (every live policy, fund, alias and the words for states, households, needs and
//     benefits) is downloaded once, so each keystroke is understood in a few milliseconds;
//   - what is typed is parsed as it is typed, recognised parts become chips, and the comparison is
//     fetched as soon as a competitor policy is certain;
//   - when a name matches several policies the screen asks the one question that best splits them
//     ("Which excess?"), worked out from the data, not from a model;
//   - the model is asked only about what was not recognised, runs alongside, and never overrides a
//     chip the rep has confirmed.
// Everything shown is HTML-escaped; the server renders the comparison itself.
(function () {
  "use strict";
  const root = document.querySelector(".call-screen");
  if (!root) return;

  const csrf = root.dataset.csrf;
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const words = (t) => (String(t || "").toLowerCase().replace(/['’]/g, "").replace(/\+/g, " plus ").replace(/&/g, " and ")
    .match(/[a-z0-9]+/g) || []);
  const bar = $("#bar"), suggestBox = $("#suggest"), sentence = $("#sentence"), questionBox = $("#question"),
    notice = $("#notice"), result = $("#result"), thinking = $("#thinking"), dock = $(".chat-dock");
  const MAX_POLICIES = 10;
  const QUESTION_WORDS = new Set(["what", "whats", "is", "does", "do", "how", "which", "can", "are"]);
  // Once a comparison is up, the same bar still takes free text — swap the placeholder so that is
  // obvious, instead of leaving the policy-naming example up as if that is all it does now.
  const START_PLACEHOLDER = bar.placeholder;
  const FOLLOW_UP_PLACEHOLDER = "Ask about anything: “what about physio?”, “dental”, “ambulance”, “add medibank”, “cheaper option?”";

  // The dock (the sentence, any question, and the bar) is fixed to the bottom of the screen like a
  // chat composer, so the content above always has enough bottom padding to clear it, even as a
  // clarifying question or a two-line sentence changes how tall the dock is.
  if (dock) {
    const syncDockHeight = () => document.documentElement.style.setProperty("--dock-h", `${dock.offsetHeight}px`);
    syncDockHeight();
    if (window.ResizeObserver) new ResizeObserver(syncDockHeight).observe(dock);
    else window.addEventListener("resize", syncDockHeight);
  }

  // ── the comparison on screen ─────────────────────────────────────────────────
  const S = {
    navy: [], comp: [],            // confirmed policies (index entries)
    autoNavy: null,                // the best match the server chose when no Navy policy was named
    alternatives: [], cheaper: [], // from the last card
    state: "NSW", stateFrom: "assumed",
    family: "couple", familyFrom: "assumed",
    needs: new Set(), focus: [], excess: null, showAll: false,
    questions: [],                 // unresolved references waiting on the rep
    // Bundles the rep has built in the panel below the sentence: a hospital policy (or any policy)
    // plus any number of extras policies, on one side, priced and compared as one product. Each
    // entry is [leaderId, ...extraIds], all ids real and all already in S.navy or S.comp.
    groups: [],
  };
  try {
    const saved = JSON.parse(localStorage.getItem("nc.household") || "{}");
    if (["NSW", "VIC", "QLD", "SA", "WA", "TAS", "NT", "ACT"].includes(saved.state)) { S.state = saved.state; S.stateFrom = "remembered"; }
    if (["single", "couple", "family", "single_parent", "dependants_only"].includes(saved.family)) { S.family = saved.family; S.familyFrom = "remembered"; }
  } catch (e) { /* storage unavailable: defaults stand */ }
  const remember = () => {
    try { localStorage.setItem("nc.household", JSON.stringify({ state: S.state, family: S.family })); } catch (e) {}
  };

  // ── the index ────────────────────────────────────────────────────────────────
  let IX = null;
  const TYPE = { h: "hospital", e: "extras", c: "combined" };
  async function loadIndex() {
    try {
      const r = await fetch("/call/index.json", { credentials: "same-origin" });
      if (!(r.headers.get("content-type") || "").includes("json")) throw new Error("Your session may have ended. Reload the page.");
      IX = build(await r.json());
      bar.disabled = false;
      bar.placeholder = bar.placeholder;   // ready
      if (bar.value) onInput();
    } catch (err) {
      showNotice(err.message || "The policy list could not be loaded. Reload the page.", "error");
    }
  }
  function build(raw) {
    const nameCount = new Map();
    const policies = raw.policies.map((r) => {
      const [id, name, fund, t, tier, navy, brand, corporate, excesses] = r;
      const f = raw.funds[fund] || {};
      const nameWords = words(name);
      nameWords.forEach((w) => nameCount.set(w, (nameCount.get(w) || 0) + 1));
      const other = new Set([...words(f.name), fund.toLowerCase(), ...words(brand),
        ...(f.brands || []).flatMap(words), ...(f.aliases || []).flatMap(words),
        ...(excesses || []).filter((x) => x > 0).map((x) => String(x))]);
      return { id, name, fund, fundName: f.name || fund, type: TYPE[t], tier, navy: !!navy, brand,
        corporate: !!corporate, excesses: excesses || [], nameWords, otherWords: [...other] };
    });
    const phrases = [];
    for (const [code, f] of Object.entries(raw.funds)) {
      const add = (text) => { const w = words(text); if (w.length) phrases.push({ w, kind: "fund", value: code }); };
      add(code);
      (f.aliases || []).forEach(add);
      (f.brands || []).forEach((b) => { if (words(b).length >= 2) add(b); });
    }
    for (const [w, v] of Object.entries(raw.states)) phrases.push({ w: words(w), kind: "state", value: v });
    for (const [w, v] of Object.entries(raw.households)) phrases.push({ w: words(w), kind: "family", value: v });
    phrases.sort((a, b) => b.w.length - a.w.length);
    const common = new Set([...nameCount].filter(([, n]) => n >= 3).map(([w]) => w));
    const labels = {};
    raw.features.forEach(([k, l]) => { labels[k] = l; });
    return {
      policies, byId: new Map(policies.map((p) => [p.id, p])), funds: raw.funds, phrases,
      needs: raw.needs, benefitWords: raw.benefit_words || {}, stop: new Set(raw.stop),
      navyMarkers: new Set(raw.navy_markers), chips: raw.chips, chipTags: raw.chip_tags || {},
      labels, common, snapshot: raw.snapshot,
    };
  }

  // ── matching ─────────────────────────────────────────────────────────────────
  function near(a, b) {           // one edit apart (typo tolerance for words of four letters or more)
    if (Math.abs(a.length - b.length) > 1) return false;
    let i = 0, j = 0, edits = 0;
    while (i < a.length && j < b.length) {
      if (a[i] === b[j]) { i++; j++; continue; }
      if (++edits > 1) return false;
      if (a.length > b.length) i++; else if (b.length > a.length) j++; else { i++; j++; }
    }
    return edits + (a.length - i) + (b.length - j) <= 1;
  }
  function tokenScore(t, p) {
    if (p.nameWords.some((w) => w.startsWith(t))) return 5;
    if (p.otherWords.some((w) => w.startsWith(t))) return 2;
    if (t.length >= 4 && p.nameWords.some((w) => near(t, w.slice(0, t.length)) || near(t, w))) return 3;
    if (t.length >= 4 && p.otherWords.some((w) => near(t, w))) return 1;
    return 0;
  }
  // Policies matching every token, best first. Words no policy here has are dropped first (they are
  // not part of a name), then at most two more, the ones that match fewest, if nothing matches them all.
  function match(tokens, { side, fund, phrase }) {
    let pool = IX.policies;
    if (side === "navy") pool = pool.filter((p) => p.navy);
    if (side === "comp") pool = pool.filter((p) => !p.navy);
    if (fund) pool = pool.filter((p) => p.fund === fund);
    const dropped = [];
    let use = tokens.filter((t) => { const ok = pool.some((p) => tokenScore(t, p)); if (!ok) dropped.push(t); return ok; });
    if (!use.length && !fund) return { cands: [], dropped };
    for (let attempt = 0; attempt < 3; attempt++) {
      const scored = [];
      for (const p of pool) {
        let total = 0, ok = true;
        for (const t of use) { const s = tokenScore(t, p); if (!s) { ok = false; break; } total += s; }
        if (!ok) continue;
        const name = p.nameWords.join(" ");
        if (use.length && (name.startsWith(use.join(" ")) || (phrase && name.startsWith(phrase)))) total += 6;
        if (use.length && (name === use.join(" ") || name === phrase)) total += 20;
        scored.push({ p, score: total });
      }
      if (scored.length || !use.length) {
        scored.sort((a, b) => b.score - a.score || (a.p.type !== "hospital") - (b.p.type !== "hospital")
          || a.p.name.localeCompare(b.p.name));
        return { cands: scored, dropped };
      }
      // Drop the token that matches the fewest policies and try again.
      const counts = use.map((t) => pool.filter((p) => tokenScore(t, p)).length);
      const worst = counts.indexOf(Math.min(...counts));
      dropped.push(use[worst]);
      use = use.filter((_, i) => i !== worst);
      if (!use.length && !fund) break;
    }
    return { cands: [], dropped };
  }

  // ── reading what was typed ───────────────────────────────────────────────────
  // Each word gets a role: part of a company, a state, a household, a need, a filler word, a word
  // of a policy name, or not understood. Everyday words that are also common in policy names
  // ("family", "excess", "premium", "teeth") are soft: they count towards a policy name when they
  // fit one, and mean what they usually mean when they do not.
  const STRONG = new Set(["vs", "versus", "against", "compared", "compare", "than", "or"]);
  const WEAK = new Set(["with", "and", "to"]);
  function parse(text) {
    const toks = words(text);
    const role = toks.map(() => "");
    const effect = toks.map(() => null);
    const out = { refs: [], state: null, family: null, needs: [], focus: [], ignored: [], cheaper: false,
      fresh: false, isQuestion: false, raw: text };
    const apply = (e) => {
      if (!e) return;
      if (e.kind === "state") out.state = e.value;
      else if (e.kind === "family") out.family = e.value;
      else {
        if (e.chip && !out.needs.includes(e.chip)) out.needs.push(e.chip);
        e.features.forEach((f) => { if (!out.focus.includes(f)) out.focus.push(f); });
      }
    };
    const funds = [];
    for (let i = 0; i < toks.length; i++) {
      if (role[i]) continue;
      for (const ph of IX.phrases) {
        const n = ph.w.length;
        if (i + n > toks.length) continue;
        let ok = true;
        for (let k = 0; k < n; k++) if (role[i + k] || toks[i + k] !== ph.w[k]) { ok = false; break; }
        if (!ok) continue;
        if (ph.kind === "fund") {
          for (let k = 0; k < n; k++) role[i + k] = "fund";
          funds.push({ code: ph.value, at: i });
        } else {
          const soft = ph.w.some((w) => IX.common.has(w));
          for (let k = 0; k < n; k++) role[i + k] = soft ? "soft" : ph.kind;
          effect[i] = { kind: ph.kind, value: ph.value, always: true };
          if (!soft) apply(effect[i]);
        }
        break;
      }
    }
    toks.forEach((t, i) => {
      if (t === "cheaper" || t === "cheapest") out.cheaper = true;
      if (t === "instead" || t === "new") out.fresh = true;
      if (i === 0 && QUESTION_WORDS.has(t)) out.isQuestion = true;
      if (role[i]) return;
      const need = IX.needs[t], bw = IX.benefitWords[t];
      if (need || bw) {
        effect[i] = { kind: "need", chip: need ? need.chip : null, features: need ? need.features : bw };
        if (IX.common.has(t)) role[i] = "soft";
        else { role[i] = "need"; apply(effect[i]); }
        return;
      }
      if (STRONG.has(t) || WEAK.has(t)) role[i] = "conn";
      else if (t.length < 2 || IX.stop.has(t) || IX.navyMarkers.has(t)) role[i] = "stop";
    });

    // Segments, each naming at most one policy: split at "vs", "against", "or"; at "with", "and" or
    // "to" when a company follows; and wherever a second company starts.
    const startsCompany = (i) => funds.some((f) => f.at === i) || IX.navyMarkers.has(toks[i]);
    const segs = [];
    let cur = [], curHasCompany = false;
    const close = () => { if (cur.length) segs.push(cur); cur = []; curHasCompany = false; };
    toks.forEach((t, i) => {
      if (role[i] === "conn" && STRONG.has(t)) { close(); return; }
      if (role[i] === "conn" && WEAK.has(t) && i + 1 < toks.length && startsCompany(i + 1)) { close(); return; }
      if (startsCompany(i) && curHasCompany) close();
      if (startsCompany(i)) curHasCompany = true;
      cur.push(i);
    });
    close();

    for (const seg of segs) {
      const fundHere = funds.find((f) => seg.includes(f.at));
      const navySide = seg.some((i) => IX.navyMarkers.has(toks[i])) || (fundHere && fundHere.code === "NHB");
      const fund = fundHere && fundHere.code !== "NHB" ? fundHere.code : "";
      const hard = seg.filter((i) => role[i] === "");
      const soft = seg.filter((i) => role[i] === "soft");
      // A company, a Navy marker with words after it, or a word that is no vocabulary: a policy reference.
      if (!fund && !hard.length && !(navySide && soft.length)) continue;
      const side = navySide ? "navy" : fund ? "comp" : "any";
      const ask = hard.concat(soft).sort((a, b) => a - b);
      // The whole reference as typed, company words included, so a full name is recognised as exact.
      const phrase = seg.filter((i) => role[i] !== "conn" && !IX.navyMarkers.has(toks[i])
          && !(role[i] === "fund" && fundHere && fundHere.code === "NHB"))
        .map((i) => toks[i]).join(" ");
      const { cands, dropped } = match(ask.map((i) => toks[i]), { side, fund, phrase });
      const gone = new Set(dropped);
      ask.forEach((i) => {
        if (role[i] === "soft") {
          if (gone.has(toks[i]) || !cands.length) { role[i] = "used"; apply(effect[i]); }
          else { role[i] = "name"; if (effect[i] && effect[i].always) apply(effect[i]); }
        } else role[i] = gone.has(toks[i]) || !cands.length ? "ignored" : "name";
      });
      if (!cands.length) continue;
      const label = seg.filter((i) => role[i] === "fund" || role[i] === "name").map((i) => toks[i]).join(" ")
        || seg.filter((i) => role[i] !== "stop" && role[i] !== "conn").map((i) => toks[i]).join(" ");
      out.refs.push(resolveRef({ text: label, side, fund, cands }));
    }
    toks.forEach((t, i) => {
      if (role[i] === "soft") { role[i] = "used"; apply(effect[i]); }
      if ((role[i] === "" || role[i] === "ignored") && t.length > 1 && !out.ignored.includes(t)) out.ignored.push(t);
    });
    return out;
  }
  function resolveRef(ref) {
    const c = ref.cands;
    const top = c[0];
    const exact = c.filter((x) => x.score >= 20);
    if (c.length === 1 || exact.length === 1) return { ...ref, policy: (exact[0] || top).p };
    if (c.length > 1 && (ref.fund || ref.side === "navy") && top.score >= c[1].score + 5) return { ...ref, policy: top.p, sure: false };
    return { ...ref, policy: null, question: splitQuestion(c.map((x) => x.p)) };
  }

  // The one question that best splits the candidates: the attribute with the most even split.
  function excessOf(p) {
    const m = p.name.match(/\$\s?(\d{2,4})/);
    if (m) return "$" + m[1] + " excess";
    if (p.excesses.length === 1) return p.excesses[0] ? "$" + p.excesses[0] + " excess" : "No excess";
    if (p.excesses.length > 1) return "$" + p.excesses.join(" / $") + " excess";
    return "Excess not listed";
  }
  function splitQuestion(cands) {
    const attrs = [
      ["Which kind of cover?", (p) => ({ hospital: "Hospital only", extras: "Extras only", combined: "Hospital and extras" }[p.type])],
      ["Which excess?", excessOf],
      ["Corporate cover or standard?", (p) => (p.corporate ? "Corporate" : "Standard")],
      ["Which tier?", (p) => p.tier || "No tier (extras)"],
      ["Which brand?", (p) => p.brand || p.fundName],
      ["Which company?", (p) => p.fundName],
    ];
    let best = null;
    for (const [ask, fn] of attrs) {
      const groups = new Map();
      cands.forEach((p) => { const v = fn(p); if (!groups.has(v)) groups.set(v, []); groups.get(v).push(p); });
      if (groups.size < 2) continue;
      const n = cands.length;
      let h = 0;
      groups.forEach((g) => { const q = g.length / n; h -= q * Math.log2(q); });
      // An even split into a few groups is best; a question whose six options leave most of the
      // policies out is worth less.
      const sizes = [...groups.values()].map((g) => g.length).sort((a, b) => b - a);
      const covered = sizes.slice(0, 6).reduce((s, x) => s + x, 0) / n;
      const usable = Math.min(groups.size, 6);
      const score = h / Math.log2(Math.max(2, groups.size)) * Math.log2(usable + 1) * covered;
      if (!best || score > best.score) best = { ask, score, groups };
    }
    if (!best || cands.length <= 3) {
      return { ask: "Which one?", options: cands.slice(0, 6).map((p) => ({ label: p.name, sub: describe(p), ids: [p.id] })) };
    }
    const options = [...best.groups.entries()].sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]))
      .slice(0, 6).map(([label, g]) => ({ label, sub: g.length === 1 ? g[0].name : `${g.length} policies`, ids: g.map((p) => p.id) }));
    return { ask: best.ask, options, total: cands.length };
  }
  function describe(p) {
    const bits = [p.fundName];
    if (p.brand && p.brand !== p.fundName) bits.push(p.brand);
    if (p.tier) bits.push(p.tier);
    bits.push(p.type);
    if (p.corporate) bits.push("corporate");
    return bits.join(" · ");
  }

  // ── what is being typed now ──────────────────────────────────────────────────
  let live = null;            // the parse of the bar's current text
  let activeSuggestion = -1;
  let suggestions = [];
  function onInput() {
    if (!IX) return;
    const text = bar.value;
    live = text.trim() ? parse(text) : null;
    renderSuggestions();
    renderSentence();
    previewSoon();
    modelSoon();
  }
  bar.addEventListener("input", onInput);
  bar.addEventListener("focus", () => { if (!bar.value) renderSuggestions(); });

  function renderSuggestions() {
    suggestions = [];
    let html = "";
    if (!live) {
      const recent = recents();
      if (document.activeElement === bar && recent.length) {
        html += `<div class="hdr">Recent</div>`;
        recent.forEach((p) => { suggestions.push({ kind: "policy", p }); html += optionHtml(p); });
      }
    } else {
      const last = live.refs[live.refs.length - 1];
      if (last) {
        if (last.fund && !last.policy && IX.funds[last.fund]) {
          const f = IX.funds[last.fund], n = last.cands.length;
          const count = n < f.count ? `${n} of its ${f.count} policies match` : `${f.count} policies`;
          html += `<div class="hdr">${esc(f.name)} · ${count}${f.restricted ? " · restricted membership" : ""}</div>`;
        } else {
          html += `<div class="hdr">${last.policy ? "Best match" : `${last.cands.length} match${last.cands.length === 1 ? "" : "es"}`}${last.side === "navy" ? " · Navy Health" : ""}</div>`;
        }
        last.cands.slice(0, 7).forEach(({ p }) => { suggestions.push({ kind: "policy", p }); html += optionHtml(p); });
      }
    }
    activeSuggestion = suggestions.length && live ? 0 : -1;
    suggestBox.innerHTML = html;
    suggestBox.hidden = !suggestions.length;
    bar.setAttribute("aria-expanded", String(!suggestBox.hidden));
    highlight();
    $$(".opt", suggestBox).forEach((el, i) => el.addEventListener("mousedown", (e) => { e.preventDefault(); pick(i); }));
  }
  function optionHtml(p) {
    const chosen = S.navy.some((x) => x.id === p.id) || S.comp.some((x) => x.id === p.id);
    return `<div class="opt${chosen ? " chosen-opt" : ""}" role="option"><span class="opt-name">${esc(p.name)}</span>
      <span class="muted small">${esc(describe(p))}${chosen ? " · already compared" : ""}</span></div>`;
  }
  function highlight() { $$(".opt", suggestBox).forEach((el, i) => el.classList.toggle("active", i === activeSuggestion)); }
  function pick(i) {
    const s = suggestions[i];
    if (!s) return;
    if (live && live.refs.length) live.refs[live.refs.length - 1] = { ...live.refs[live.refs.length - 1], policy: s.p, question: null };
    else live = { refs: [{ text: s.p.name, side: s.p.navy ? "navy" : "comp", policy: s.p, cands: [{ p: s.p }] }],
      state: null, family: null, needs: [], focus: [], ignored: [], raw: s.p.name };
    commit();
  }

  // ── the editable sentence ────────────────────────────────────────────────────
  function chip(label, cls, attrs = "", removable = false) {
    return `<span class="tok ${cls}" ${attrs}>${label}${removable ? ` <button type="button" class="x" aria-label="Remove">×</button>` : ""}</span>`;
  }
  function renderSentence() {
    const parts = [];
    const preview = live ? live.refs.filter((r) => r.policy && !S.comp.concat(S.navy).some((x) => x.id === r.policy.id)) : [];
    const comps = S.comp.map((p) => chip(esc(p.name), "tok-policy", `data-id="${p.id}"`, true))
      .concat(preview.filter((r) => !r.policy.navy).map((r) => chip(esc(r.policy.name), "tok-policy tok-preview", "", false)));
    // A hospital or combined Navy chip can be swapped for a different one from a list (click the
    // chip itself, not its ×) — an extras chip in the bundle has no "which one" to switch, so it
    // stays a plain removable chip.
    const navs = S.navy.map((p) => chip(esc(p.name), "tok-policy tok-navy" + (p.type !== "extras" ? " tok-menu" : ""),
        `data-id="${p.id}"${p.type !== "extras" ? ' data-menu="navy"' : ""}`, true))
      .concat(preview.filter((r) => r.policy.navy).map((r) => chip(esc(r.policy.name), "tok-policy tok-navy tok-preview")));
    const pendingQ = (live ? live.refs.filter((r) => !r.policy) : []).concat(S.questions);
    pendingQ.forEach((r) => comps.push(chip(`“${esc(r.text)}”?`, "tok-ask", "")));
    const needsPending = S.needs.size > 0 || (live && live.needs.length > 0);
    if (!comps.length && !navs.length && !pendingQ.length && !S.autoNavy && !needsPending) {
      sentence.innerHTML = live && live.ignored.length
        ? `<span class="muted small">Not recognised yet: ${live.ignored.map(esc).join(", ")}</span>` : "";
      renderQuestion();
      renderBundlePanel();
      return;
    }
    if (!comps.length && !navs.length && !pendingQ.length && !S.autoNavy) {
      // Only a need so far (no policy named yet): show it on its own, waiting for a policy to compare.
      const needs = new Set([...S.needs, ...(live ? live.needs : [])]);
      const chipLabel = (k) => (IX.chips.find((c) => c.key === k) || { label: k }).label;
      const needHtml = [...needs].map((k) => chip(esc(chipLabel(k)), "tok-need" + (S.needs.has(k) ? "" : " tok-preview"), `data-need="${k}"`, S.needs.has(k))).join(" ");
      sentence.innerHTML = `needs ${needHtml} <span class="muted small">— waiting on a policy</span>`;
      $$(".tok .x", sentence).forEach((b) => b.addEventListener("click", (e) => {
        const t = e.target.closest(".tok");
        if (t.dataset.need) { S.needs.delete(t.dataset.need); renderSentence(); compare(true); }
      }));
      renderQuestion();
      renderBundlePanel();
      return;
    }
    if (comps.length) parts.push(`Comparing ${comps.join(" ")}`);
    if (navs.length) parts.push(`${comps.length ? "with" : "Comparing"} ${navs.join(" ")}`);
    else if (S.autoNavy && S.comp.length) {
      parts.push(`with ${chip(`${esc(S.autoNavy.name)} <span class="best">best match ▾</span>`, "tok-policy tok-navy tok-auto", `data-menu="navy" data-id="${S.autoNavy.id}"`)}`);
    }
    const st = live && live.state ? live.state : S.state;
    const fam = live && live.family ? live.family : S.family;
    const famText = { single: "single", couple: "couple", family: "family", single_parent: "single parent", dependants_only: "dependants only" }[fam];
    parts.push(`for a ${chip(esc(famText) + " ▾", "tok-menu" + (S.familyFrom === "assumed" && !(live && live.family) ? " tok-assumed" : ""), 'data-menu="family"')}`);
    parts.push(`in ${chip(esc(st) + " ▾", "tok-menu" + (S.stateFrom === "assumed" && !(live && live.state) ? " tok-assumed" : ""), 'data-menu="state"')}`);
    const needs = new Set([...S.needs, ...(live ? live.needs : [])]);
    const chipLabel = (k) => (IX.chips.find((c) => c.key === k) || { label: k }).label;
    let needHtml = [...needs].map((k) => chip(esc(chipLabel(k)), "tok-need" + (S.needs.has(k) ? "" : " tok-preview"), `data-need="${k}"`, S.needs.has(k))).join(" ");
    needHtml += ` ${chip("+ need", "tok-menu tok-add", 'data-menu="need"')}`;
    parts.push(`· needs ${needHtml}`);
    let html = parts.join(" ");
    if (live && live.ignored.length) html += ` <span class="muted small ignored">Not used: ${live.ignored.map(esc).join(", ")}</span>`;
    sentence.innerHTML = html;
    $$(".tok .x", sentence).forEach((b) => b.addEventListener("click", (e) => {
      const t = e.target.closest(".tok");
      if (t.dataset.id) removePolicy(Number(t.dataset.id));
      if (t.dataset.need) { S.needs.delete(t.dataset.need); renderSentence(); compare(true); }
    }));
    $$(".tok[data-menu]", sentence).forEach((t) => t.addEventListener("click", (e) => { if (!e.target.classList.contains("x")) openMenu(t); }));
    renderQuestion();
    renderBundlePanel();
  }

  // ── small menus on the sentence ──────────────────────────────────────────────
  let menu = null;
  function closeMenu() { if (menu) { menu.remove(); menu = null; } }
  function openMenu(anchor) {
    closeMenu();
    const kind = anchor.dataset.menu;
    if (kind === "navy") { openNavyMenu(anchor); return; }
    let items = [];
    if (kind === "state") items = ["NSW", "VIC", "QLD", "SA", "WA", "TAS", "NT", "ACT"].map((s) => ({ label: s, run: () => { S.state = s; S.stateFrom = "chosen"; remember(); } }));
    if (kind === "family") items = [["single", "Single"], ["couple", "Couple"], ["family", "Family"], ["single_parent", "Single parent"], ["dependants_only", "Dependants only"]]
      .map(([v, l]) => ({ label: l, run: () => { S.family = v; S.familyFrom = "chosen"; remember(); } }));
    if (kind === "need") items = IX.chips.filter((c) => !S.needs.has(c.key)).map((c) => ({ label: c.label, run: () => { S.needs.add(c.key); S.focus = (IX.chipTags[c.key] || []).slice(); } }));
    menu = document.createElement("div");
    menu.className = "menu";
    menu.innerHTML = items.map((it, i) => `<button type="button" data-i="${i}">${esc(it.label)}${it.sub ? ` <span class="muted small">${esc(it.sub)}</span>` : ""}</button>`).join("");
    positionMenu(menu, anchor);
    $$("button", menu).forEach((b) => b.addEventListener("click", () => { items[Number(b.dataset.i)].run(); closeMenu(); renderSentence(); compare(true, kind === "need"); }));
  }
  // Swapping the Navy Health policy a card uses — the chip's own ▾, not the bottom bar: a full,
  // searchable list of every Navy hospital/combined policy, the closest-match and cheaper ones
  // called out first, since "click and get a short list" beats "click and have to go type a name".
  function switchNavyPolicy(oldId, newId) {
    const p = IX.byId.get(newId);
    if (!p) return;
    const i = oldId == null ? -1 : S.navy.findIndex((x) => x.id === oldId);
    if (i === -1) { setNavy(newId); return; }
    if (S.navy.some((x) => x.id === newId)) S.navy.splice(i, 1);   // already in the bundle elsewhere
    else S.navy[i] = p;
    S.groups.forEach((g) => { if (g[0] === oldId) g[0] = newId; });   // its attached extras follow it
    S.autoNavy = null;
  }
  // A pickable list for a (possibly long) set of candidate policies — a hospital/hundreds-of-extras
  // problem in one fund list is overwhelming on its own. When there are enough candidates to be
  // genuinely hard to scan, a qualifying question goes on top first — the same "which attribute
  // best splits them" logic the typed-name flow already asks with ("Corporate cover or standard?",
  // "Which tier?") — so picking from a list is "answer one question, then see six names" rather
  // than "scroll past eighty." A text filter is always there too, under or instead of the question,
  // for a rep who'd rather just type. `sections` is `[{label, items: [{id, name, sub}]}]`;
  // `onPick(id)` runs when a policy is chosen.
  function buildPickList(sections, onPick) {
    const all = sections.flatMap((s) => s.items);
    const optionHtml = (o) => `<button type="button" data-id="${o.id}">${esc(o.name)}${o.sub ? ` <span class="muted small">${esc(o.sub)}</span>` : ""}</button>`;
    const q = all.length > 8 ? splitQuestion(all.map((o) => IX.byId.get(o.id)).filter(Boolean)) : null;
    const qualify = q && q.total > 3 ? q : null;   // the un-splittable fallback ("Which one?") would
    const wrap = document.createElement("div");    // just repeat the list below it — skip it then
    wrap.className = "menu bundle-menu";
    wrap.innerHTML = `
      ${qualify ? `<div class="menu-qualify"><div class="hdr">${esc(qualify.ask)}</div>
        ${qualify.options.map((o) => `<button type="button" class="qualify-opt" data-ids="${o.ids.join(",")}">${esc(o.label)}
          <span class="muted small">${esc(o.sub || "")}</span></button>`).join("")}</div>` : ""}
      <input type="text" class="bundle-menu-filter" placeholder="${qualify ? "Or type to search…" : "Find a policy…"}">
      <div class="bundle-menu-list">${sections.map((s) => (s.items.length
        ? `<div class="hdr">${esc(s.label)}</div>${s.items.map(optionHtml).join("")}` : "")).join("")}</div>`;
    const list = $(".bundle-menu-list", wrap), filter = $(".bundle-menu-filter", wrap);
    let within = null;   // the id set the qualifying question narrowed to, if any
    const applyFilter = () => {
      const typed = words(filter.value);
      $$("button", list).forEach((b) => {
        const id = Number(b.dataset.id);
        const textOk = !typed.length || typed.every((t) => tokenScore(t, IX.byId.get(id)) > 0);
        b.hidden = !textOk || (within && !within.has(id));
      });
      $$(".hdr", list).forEach((h) => {
        let el = h.nextElementSibling, any = false;
        while (el && !el.classList.contains("hdr")) { if (!el.hidden) any = true; el = el.nextElementSibling; }
        h.hidden = !any;
      });
    };
    filter.addEventListener("input", applyFilter);
    $$(".qualify-opt", wrap).forEach((b) => b.addEventListener("click", () => {
      within = new Set(b.dataset.ids.split(",").map(Number));
      $(".menu-qualify", wrap).remove();
      applyFilter();
      filter.focus();
    }));
    $$("button", list).forEach((b) => b.addEventListener("click", () => onPick(Number(b.dataset.id))));
    return { el: wrap, focus: () => filter.focus() };
  }
  function openNavyMenu(anchor) {
    const oldId = anchor.dataset.id ? Number(anchor.dataset.id) : null;
    const suggested = S.alternatives.map((a) => ({ id: a.id, name: a.name, sub: `${Math.round(a.score * 100)}% cover match` }))
      .concat(S.cheaper.map((c) => ({ id: c.id, name: c.name, sub: `cheaper: $${c.premium.toFixed(2)} a month` })));
    const shown = new Set([oldId, ...suggested.map((s) => s.id)]);
    const rest = IX.policies.filter((p) => p.navy && p.type !== "extras" && !shown.has(p.id))
      .map((p) => ({ id: p.id, name: p.name }));
    const picker = buildPickList(
      [{ label: "Suggested", items: suggested }, { label: "All Navy Health policies", items: rest }],
      (id) => { switchNavyPolicy(oldId, id); closeMenu(); renderSentence(); compare(true); });
    menu = picker.el;
    positionMenu(menu, anchor);
    picker.focus();
  }
  // Shared by every popover anchored to a chip/button in the dock (the state/family/need/navy menus,
  // and the bundle panel's "+ add extras" picker): placed just under its anchor, or above it when the
  // dock leaves no room below, since the dock sits at the bottom of the screen.
  function positionMenu(el, anchor) {
    el.style.visibility = "hidden";
    document.body.appendChild(el);
    const r = anchor.getBoundingClientRect();
    const below = r.bottom + 4 + el.offsetHeight <= window.innerHeight;
    el.style.left = `${r.left + window.scrollX}px`;
    el.style.top = below ? `${r.bottom + window.scrollY + 4}px` : `${r.top + window.scrollY - el.offsetHeight - 4}px`;
    el.style.visibility = "";
  }
  document.addEventListener("click", (e) => {
    if (menu && !menu.contains(e.target) && !e.target.closest(".tok[data-menu]") && !e.target.closest(".bundle-add")) closeMenu();
  });

  // ── the bundle panel: a hospital policy plus any number of extras policies, stacked ──────────
  // S.groups holds [leaderId, ...extraIds]; every id in it is also a real entry in S.navy/S.comp —
  // the panel only ever groups chips the rep has already added, it never adds a policy on its own.
  function membersOf(leaderId) {
    const g = S.groups.find((g) => g[0] === leaderId);
    return g ? g.slice(1) : [];
  }
  function addToBundle(leaderId, p) {
    // The leader may only be the auto-suggested Navy policy so far (shown in the panel, but not
    // yet a real part of the comparison) — attaching extras to it is what confirms it.
    if (leaderId === (S.autoNavy && S.autoNavy.id)) addPolicy(S.autoNavy);
    addPolicy(p);
    let g = S.groups.find((g) => g[0] === leaderId);
    if (!g) { g = [leaderId]; S.groups.push(g); }
    if (!g.includes(p.id)) g.push(p.id);
  }
  function ungroupMember(leaderId, id) {
    const g = S.groups.find((g) => g[0] === leaderId);
    if (g) {
      const i = g.indexOf(id);
      if (i > 0) g.splice(i, 1);
      if (g.length < 2) S.groups = S.groups.filter((x) => x !== g);
    }
    removePolicy(id);   // detaching an extra from a bundle removes it from the comparison entirely —
    renderBundlePanel(); // the simplest mental model: the panel's × always means "take this back out".
  }
  function renderBundlePanel() {
    const panel = $("#bundle-panel");
    if (!panel || !IX) return;
    const nested = new Set(S.groups.flatMap((g) => g.slice(1)));   // shown under their leader, not at top level
    const sideHtml = (side, label) => {
      let leaders = S[side].filter((p) => !nested.has(p.id));
      // The auto-suggested Navy policy is not a real selection yet (the rep never named one), but
      // it's exactly what "add extras" should attach to — show it so there is something to click.
      if (side === "navy" && !leaders.length && S.autoNavy) leaders = [S.autoNavy];
      if (!leaders.length) return "";
      const rows = leaders.map((p) => {
        const suggested = side === "navy" && S.autoNavy && S.autoNavy.id === p.id && !S.navy.length;
        const extras = membersOf(p.id).map((id) => IX.byId.get(id)).filter(Boolean)
          .map((m) => `<span class="tok tok-bundle-extra" data-id="${m.id}">${esc(m.name)}
            <button type="button" class="x" aria-label="Remove from bundle">×</button></span>`).join(" ");
        return `<div class="bundle-row" data-leader="${p.id}">
          <span class="bundle-leader">${esc(p.name)}${suggested ? ` <span class="muted small">(best match)</span>` : ""}</span>${extras}
          <button type="button" class="link-btn bundle-add" data-leader="${p.id}" data-side="${side}">+ add extras</button>
        </div>`;
      }).join("");
      return `<div class="bundle-side"><div class="muted small bundle-side-label">${label}</div>${rows}</div>`;
    };
    const html = sideHtml("navy", "Navy Health") + sideHtml("comp", "Competitor");
    panel.innerHTML = html;
    panel.hidden = !html;
    $$(".bundle-add", panel).forEach((b) => b.addEventListener("click", () => openBundleMenu(b)));
    $$(".tok-bundle-extra .x", panel).forEach((b) => b.addEventListener("click", (e) => {
      const t = e.target.closest(".tok-bundle-extra");
      ungroupMember(Number(t.closest(".bundle-row").dataset.leader), Number(t.dataset.id));
      compare(true);
    }));
  }
  function openBundleMenu(btn) {
    closeMenu();
    const leaderId = Number(btn.dataset.leader), side = btn.dataset.side;
    const leader = IX.byId.get(leaderId);
    const already = new Set([leaderId, ...membersOf(leaderId)]);
    // Scoped to the leader's own fund by default — stacking a Bupa hospital policy with AAMI's
    // extras is not a real bundle anyone buys, and listing every extras policy on the side made the
    // picker unusable (dozens of unrelated funds). "Other funds" below still reaches the rest.
    const toItem = (p) => ({ id: p.id, name: p.name, sub: p.fundName });
    const sameFund = IX.policies.filter((p) => p.navy === (side === "navy") && p.type === "extras"
      && p.fund === leader.fund && !already.has(p.id)).map(toItem);
    const otherFund = IX.policies.filter((p) => p.navy === (side === "navy") && p.type === "extras"
      && p.fund !== leader.fund && !already.has(p.id)).map(toItem);
    const picker = buildPickList(
      [{ label: leader.fundName, items: sameFund }, { label: "Other funds", items: otherFund }],
      (id) => { const p = IX.byId.get(id); if (p) addToBundle(leaderId, p); closeMenu(); renderSentence(); compare(true); });
    menu = picker.el;
    positionMenu(menu, btn);
    picker.focus();
  }

  // The help drawer (estimator + pushback) is a corner toggle, not a permanent column, so it closes
  // on an outside click or Escape like any other popover.
  function closeAssist(panel) {
    panel.classList.remove("open");
    const fab = $(`.assist-fab[aria-controls="${panel.id}"]`, result);
    if (fab) fab.setAttribute("aria-expanded", "false");
  }
  document.addEventListener("click", (e) => {
    $$(".sheet-assist.open", result).forEach((panel) => {
      if (!panel.contains(e.target) && !e.target.closest(`.assist-fab[aria-controls="${panel.id}"]`)) closeAssist(panel);
    });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") $$(".sheet-assist.open", result).forEach(closeAssist);
  });

  // ── a question, when a name means more than one policy ───────────────────────
  function currentQuestion() {
    const fromBar = live ? live.refs.find((r) => !r.policy && r.question) : null;
    return fromBar ? { ref: fromBar, fromBar: true } : (S.questions[0] ? { ref: S.questions[0], fromBar: false } : null);
  }
  function renderQuestion() {
    const q = currentQuestion();
    if (!q) { questionBox.hidden = true; questionBox.innerHTML = ""; return; }
    const { ask, options, total } = q.ref.question;
    questionBox.hidden = false;
    questionBox.innerHTML = `<span class="q-ask">“${esc(q.ref.text)}”${total ? ` matches ${total} policies.` : ""} <strong>${esc(ask)}</strong></span>
      ${options.map((o, i) => `<button type="button" class="pill q-opt" data-i="${i}"><kbd>${i + 1}</kbd> ${esc(o.label)}<span class="muted small"> ${esc(o.sub || "")}</span></button>`).join("")}`;
    $$(".q-opt", questionBox).forEach((b) => b.addEventListener("click", () => answer(Number(b.dataset.i))));
  }
  function answer(i) {
    const q = currentQuestion();
    if (!q) return;
    const opt = q.ref.question.options[i];
    if (!opt) return;
    const cands = opt.ids.map((id) => IX.byId.get(id)).filter(Boolean);
    const ref = q.ref;
    if (cands.length === 1) {
      ref.policy = cands[0]; ref.question = null;
      if (!q.fromBar) { S.questions.shift(); addPolicy(cands[0]); renderSentence(); compare(true); return; }
      commit();
      return;
    }
    ref.cands = cands.map((p) => ({ p, score: 0 }));
    ref.question = splitQuestion(cands);
    if (q.fromBar) { live.refs = live.refs.map((r) => (r === ref ? ref : r)); commit(); }
    else renderSentence();
  }

  // ── committing what was typed ────────────────────────────────────────────────
  function addPolicy(p) {
    const list = p.navy ? S.navy : S.comp;
    if (list.some((x) => x.id === p.id)) return;
    if (S.navy.length + S.comp.length >= MAX_POLICIES) { showNotice(`Up to ${MAX_POLICIES} policies at a time.`); return; }
    // An extras policy added while the Navy side is still just a suggestion (the rep has not named
    // one) attaches to that suggestion instead of silently dropping it — adding extras was never a
    // request to forget which hospital policy the screen was comparing.
    if (p.navy && p.type === "extras" && !S.navy.length && S.autoNavy) {
      S.navy.push(S.autoNavy);
      S.groups.push([S.autoNavy.id, p.id]);
      S.autoNavy = null;
      list.push(p);
      return;
    }
    list.push(p);
    if (p.navy && p.type !== "extras") S.autoNavy = null;
    if (!p.navy) pushRecent(p);
  }
  function removePolicy(id) {
    S.navy = S.navy.filter((p) => p.id !== id);
    S.comp = S.comp.filter((p) => p.id !== id);
    if (S.autoNavy && S.autoNavy.id === id) S.autoNavy = null;
    // Removing a bundle's leader drops the whole bundle (there is nothing left to lead); removing
    // one of its extras just detaches that one, same as the panel's own × does.
    S.groups = S.groups.filter((g) => g[0] !== id).map((g) => g.filter((x) => x !== id)).filter((g) => g.length >= 2);
    renderSentence();
    compare(true);
  }
  function setNavy(id) {
    const p = IX.byId.get(id);
    if (!p) return;
    S.navy = [p];
    S.autoNavy = null;
  }
  function commit() {
    const p = live;
    if (!p) return;
    if (p.fresh && p.refs.some((r) => r.policy)) { S.navy = []; S.comp = []; S.autoNavy = null; S.focus = []; S.questions = []; S.groups = []; }
    // A name that still means several policies becomes the question; it replaces any older one, since
    // the rep has moved on to this.
    const asking = p.refs.filter((r) => !r.policy && r.question);
    if (asking.length) S.questions = asking;
    p.refs.forEach((r) => { if (r.policy) addPolicy(r.policy); });
    if (p.state) { S.state = p.state; S.stateFrom = "typed"; remember(); }
    if (p.family) { S.family = p.family; S.familyFrom = "typed"; remember(); }
    p.needs.forEach((n) => S.needs.add(n));
    if (p.focus.length) S.focus = p.focus.slice();
    clearTimeout(modelTimer);
    clearTimeout(previewTimer);
    const askModel = p.ignored.length > 0 || (!p.refs.length && !p.focus.length && !p.needs.length && !p.state && !p.family && !p.cheaper);
    const text = p.raw;
    if (p.cheaper && S.cheaper.length && !p.refs.some((r) => r.policy && r.policy.navy)) { setNavy(S.cheaper[0].id); S.focus = ["price"]; }
    else if (p.cheaper && !S.cheaper.length && (S.comp.length)) showNotice("Navy Health has no cheaper policy of this kind for this household. The card shows where it is stronger.");
    const scrollToFocus = p.focus.length > 0;
    live = null;
    bar.value = "";
    suggestBox.hidden = true;
    renderSentence();
    compare(true, scrollToFocus);
    if (askModel && text.trim().split(/\s+/).length >= 2) askTheModel(text);
  }

  // ── keyboard ─────────────────────────────────────────────────────────────────
  let lastEsc = 0, arrowed = false;
  bar.addEventListener("input", () => { arrowed = false; });
  bar.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" && suggestions.length) { activeSuggestion = Math.min(activeSuggestion + 1, suggestions.length - 1); arrowed = true; highlight(); e.preventDefault(); return; }
    if (e.key === "ArrowUp" && suggestions.length) { activeSuggestion = Math.max(activeSuggestion - 1, 0); arrowed = true; highlight(); e.preventDefault(); return; }
    if (e.key === "Tab" && suggestions.length && activeSuggestion >= 0 && live) { pick(activeSuggestion); e.preventDefault(); return; }
    if (e.key === "Enter") {
      e.preventDefault();
      // Enter picks a suggestion the rep moved to, or one from the recents; otherwise it confirms the
      // sentence, and any name that still means several policies stays as a question.
      if (arrowed && suggestions.length && activeSuggestion >= 0) { pick(activeSuggestion); return; }
      if (!live && suggestions.length && activeSuggestion >= 0) { pick(activeSuggestion); return; }
      if (live) commit();
      return;
    }
    if (/^[1-6]$/.test(e.key) && !bar.value && currentQuestion()) { answer(Number(e.key) - 1); e.preventDefault(); return; }
    if (e.key === "Escape") {
      e.preventDefault();
      if (!suggestBox.hidden && bar.value) { suggestBox.hidden = true; return; }
      if (bar.value) { bar.value = ""; onInput(); return; }
      const now = Date.now();
      if (now - lastEsc < 1200) newCall(); else showNotice("Press Esc again for a new call.");
      lastEsc = now;
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && document.activeElement !== bar && !/input|textarea|select/i.test(document.activeElement.tagName)) { e.preventDefault(); bar.focus(); }
  });
  bar.addEventListener("blur", () => setTimeout(() => { if (document.activeElement !== bar) suggestBox.hidden = true; }, 150));
  $("#new-call").addEventListener("click", newCall);
  function newCall() {
    S.navy = []; S.comp = []; S.autoNavy = null; S.alternatives = []; S.cheaper = []; S.needs = new Set();
    S.focus = []; S.excess = null; S.showAll = false; S.questions = []; S.groups = []; usage = [];
    live = null; bar.value = ""; notice.hidden = true; kept = null; bar.placeholder = START_PLACEHOLDER;
    renderSentence();
    result.innerHTML = `<div class="empty-state"><p><strong>New call.</strong> Type the competitor's policy the way the member says it.</p></div>`;
    bar.focus();
  }

  // ── recents ──────────────────────────────────────────────────────────────────
  function recents() {
    try { return (JSON.parse(localStorage.getItem("nc.recent") || "[]")).map((id) => IX.byId.get(id)).filter(Boolean).slice(0, 6); }
    catch (e) { return []; }
  }
  function pushRecent(p) {
    try {
      const ids = JSON.parse(localStorage.getItem("nc.recent") || "[]").filter((id) => id !== p.id);
      ids.unshift(p.id);
      localStorage.setItem("nc.recent", JSON.stringify(ids.slice(0, 12)));
    } catch (e) {}
  }

  // ── the comparison ───────────────────────────────────────────────────────────
  // A preview (what the comparison would be if the rep pressed Enter) is shown in place of the
  // confirmed answer, which is kept aside, talking point and all, and put back when the bar no longer
  // names anything new.
  let latestCard = 0, previewTimer = null, previewInFlight = false, kept = null;
  let lastGroupMeta = [];   // [[syntheticId, [memberIds]], ...] from the most recent /card response
  function previewSoon() {
    clearTimeout(previewTimer);
    const certain = live ? live.refs.filter((r) => r.policy) : [];
    if (!certain.length || live.refs.some((r) => !r.policy)) { restoreConfirmed(); return; }
    previewTimer = setTimeout(() => compare(false), 350);
  }
  function restoreConfirmed() {
    if (previewInFlight) { latestCard++; previewInFlight = false; result.classList.remove("loading"); }
    if (!kept) return;
    result.innerHTML = "";
    result.appendChild(kept);
    kept = null;
  }
  function showResult(html, committed) {
    if (committed) kept = null;
    else if (!kept) {
      kept = document.createDocumentFragment();
      while (result.firstChild) kept.appendChild(result.firstChild);
    }
    result.innerHTML = html;
  }
  function cardParams(extraIds) {
    const ids = [...S.navy.map((p) => p.id), ...S.comp.map((p) => p.id)];
    (extraIds || []).forEach((id) => { if (!ids.includes(id)) ids.push(id); });
    const p = new URLSearchParams({ ids: ids.join(","), state: (live && live.state) || S.state, family: (live && live.family) || S.family });
    if (S.groups.length) p.set("groups", S.groups.map((g) => g.join("+")).join(","));
    const needs = new Set([...S.needs, ...(live ? live.needs : [])]);
    if (needs.size) p.set("chips", [...needs].join(","));
    const focus = live && live.focus.length ? live.focus : S.focus;
    if (focus.length) p.set("focus", focus.join(","));
    if (S.excess) p.set("excess", S.excess);
    if (S.showAll) p.set("all", "1");
    return p;
  }
  function noPolicyMessage() {
    // Nothing to compare yet. Never leave the screen blank with no explanation — say exactly what
    // is still needed, using whatever was already understood (a question pending, a benefit named).
    if (S.questions.length) return `<div class="empty-state"><p><strong>Answer the question above to continue.</strong></p></div>`;
    const needLabels = [...S.needs].map((k) => (IX.chips.find((c) => c.key === k) || { label: k }).label);
    if (needLabels.length) {
      return `<div class="empty-state"><p><strong>Got it — ${esc(needLabels.join(" and "))} noted.</strong>
        Now name a policy to compare it against, for example “HCF bronze plus” or “our premium gold”.</p></div>`;
    }
    return `<div class="empty-state">
      <p><strong>Type the competitor's policy the way the member says it.</strong> The company is enough to start.</p>
      <p class="muted small">Try “bupa silver plus”, “defence gold”, “frank” or “our premium gold 750 vs hcf”. Then ask follow-ups in the same bar: “what about dental?”, “add medibank”, “cheaper option?”.</p>
    </div>`;
  }
  async function compare(committed, scrollToFocus) {
    const extra = !committed && live ? live.refs.filter((r) => r.policy).map((r) => r.policy.id) : [];
    const params = cardParams(extra);
    if (!params.get("ids")) {
      if (committed) { kept = null; result.innerHTML = noPolicyMessage(); }
      return;
    }
    const mine = ++latestCard;
    previewInFlight = !committed;
    result.classList.add("loading");
    try {
      const r = await fetch(`/card?${params}`, { credentials: "same-origin" });
      const html = await r.text();
      if (mine !== latestCard) return;
      previewInFlight = false;
      showResult(html, committed);
      const metaEl = $("#card-meta", result);
      const meta = metaEl ? JSON.parse(metaEl.textContent) : {};
      lastGroupMeta = meta.groups || [];
      if (committed) {
        S.autoNavy = meta.auto_navy || (S.navy.length ? null : S.autoNavy);
        S.alternatives = meta.alternatives || [];
        S.cheaper = meta.cheaper || [];
        renderSentence();
        bar.placeholder = FOLLOW_UP_PLACEHOLDER;
      }
      wireResult(committed);
      if (scrollToFocus) scrollToFocused();
    } catch (err) {
      if (mine === latestCard) result.innerHTML = `<p class="error">${esc(err.message)}</p>`;
    } finally {
      if (mine === latestCard) result.classList.remove("loading");
    }
  }
  // Typing a specific benefit ("physio", "ambulance") opens its row and moves it to the top of its
  // table, but scrolling the rep down to go find it is itself the problem — answer right at the bar
  // where they typed instead, so there's nothing to chase and nothing to land on "nothing".
  function scrollToFocused() {
    const row = $(".xgroup.focus", result);
    if (row) {
      row.classList.add("just-focused");
      setTimeout(() => row.classList.remove("just-focused"), 1600);
      const label = $(".x-open", row);
      const navyCell = $("tr.xrow > td:nth-child(2)", row);
      const compCell = $("tr.xrow > td:nth-child(3)", row);
      const gap = $(".x-gap-line", row);
      const navyTxt = navyCell ? navyCell.textContent.replace(/\s+/g, " ").trim() : "";
      const compTxt = compCell ? compCell.textContent.replace(/\s+/g, " ").trim() : "";
      const bits = [label ? label.textContent.trim() : "", navyTxt && `Navy Health: ${navyTxt}`, compTxt && `Theirs: ${compTxt}`, gap && gap.textContent.trim()].filter(Boolean);
      if (bits.length) showNotice(bits.join(" — "), "answer", 9000);
      return;
    }
    // Nothing to land on (e.g. the benefit asked about is an extras service and neither policy here
    // has extras at all) still needs to be seen — the server says why in a ".warn" line; surface that
    // text at the bar instead of leaving the screen looking unchanged.
    const warn = $(".warn", result);
    if (warn) showNotice(warn.textContent.trim(), "answer warn", 9000);
  }
  function wireResult(committed) {
    $$(".remove-policy", result).forEach((b) => b.addEventListener("click", () => {
      const id = Number(b.dataset.id);
      if (id >= 0) { removePolicy(id); return; }
      // A bundle column's id is synthetic (negative, server-assigned for this request only) —
      // map it back to the real policies it merged, and remove every one of them plus the group.
      const entry = lastGroupMeta.find((g) => g[0] === id);
      const members = entry ? entry[1] : [];
      S.groups = S.groups.filter((g) => !members.includes(g[0]));
      members.forEach((m) => { S.navy = S.navy.filter((p) => p.id !== m); S.comp = S.comp.filter((p) => p.id !== m); });
      if (S.autoNavy && members.includes(S.autoNavy.id)) S.autoNavy = null;
      renderSentence();
      compare(true);
    }));
    const toggle = $("#toggle-all", result);
    if (toggle) toggle.addEventListener("click", () => { S.showAll = toggle.dataset.all === "1"; compare(true); });
    $$(".excess-choice", result).forEach((b) => b.addEventListener("click", () => { S.excess = b.dataset.excess; compare(true); }));
    $$(".follow", result).forEach((b) => b.addEventListener("click", () => followUp(b)));
    $$(".talk", result).forEach((box) => {
      const go = $(".talk-go", box), again = $(".talk-again", box), copy = $(".talk-copy", box);
      if (go) go.addEventListener("click", () => talk(box));
      if (again) again.addEventListener("click", () => talk(box, true));
      if (copy) copy.addEventListener("click", () => copyTalk(box));
      if (!box.dataset.lazy && committed) talk(box);
      else if (!box.dataset.lazy) $(".talk-text", box).innerHTML = `<p class="muted small">Press Enter to confirm, and I'll write what to say.</p>`;
    });
    $$(".x-open", result).forEach((b) => b.addEventListener("click", () => {
      const group = b.closest(".xgroup");
      b.setAttribute("aria-expanded", String(group.classList.toggle("open")));
    }));
    $$(".x-show-all", result).forEach((b) => b.addEventListener("click", () => {
      $$(".x-more", b.closest(".sheet-block")).forEach((g) => { g.hidden = false; });
      b.remove();
    }));
    $$(".h-copy", result).forEach((b) => b.addEventListener("click", () => {
      const t = $(".h-say-text", b.closest(".h-say"));
      const text = (t.textContent || "").replace(/^“|”$/g, "");
      if (navigator.clipboard) navigator.clipboard.writeText(text).then(() => showNotice("Copied."), () => {});
    }));
    $$(".assist-fab", result).forEach((b) => b.addEventListener("click", () => {
      const panel = document.getElementById(b.getAttribute("aria-controls"));
      if (!panel) return;
      const open = panel.classList.toggle("open");
      b.setAttribute("aria-expanded", String(open));
    }));
    $$(".estimator", result).forEach(wireEstimator);
    $$(".objections", result).forEach((box) => {
      const input = $(".objection-input", box), ask = $(".objection-ask", box);
      $$(".objection-preset", box).forEach((b) => b.addEventListener("click", () => askObjection(box, { preset: b.dataset.preset, label: b.textContent })));
      const go = () => { const text = input.value.trim(); if (text) { askObjection(box, { text }); input.value = ""; } };
      if (ask) ask.addEventListener("click", go);
      if (input) input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } });
    });
  }
  function followUp(b) {
    const kind = b.dataset.kind;
    if (kind === "swap_navy") { setNavy(Number(b.dataset.id)); S.focus = ["price"]; }
    if (kind === "need") { S.needs.add(b.dataset.chip); S.focus = (IX.chipTags[b.dataset.chip] || []).slice(); }
    if (kind === "clear_focus") S.focus = [];
    if (kind === "add_comp") { bar.value = "add "; bar.focus(); onInput(); return; }
    if (kind === "table") { const m = $("#matrix", result); if (m) { m.open = true; m.scrollIntoView({ behavior: "smooth", block: "start" }); } return; }
    renderSentence();
    compare(true, kind === "need" || kind === "swap_navy");
  }

  // ── the talking point, streamed, then replaced by the checked text ──────────
  // A checked talking point is kept for the rest of the call, keyed on everything it was written
  // from, so adding a second competitor does not rewrite the first card's; a second request for one
  // still being written joins it. "Write it again" asks anew.
  const written = new Map(), writing = new Map();
  async function talk(box, fresh) {
    const mine = box.talkSeq = (box.talkSeq || 0) + 1;   // a newer request for this card wins
    const textEl = $(".talk-text", box), actions = $(".talk-actions", box), go = $(".talk-go", box);
    if (go) go.hidden = true;
    textEl.hidden = false; actions.hidden = true;
    const body = new FormData();
    body.append("csrf", csrf);
    body.append("navy", box.dataset.navy);
    body.append("comp", box.dataset.comp);
    const inputs = [];
    cardParams().forEach((v, k) => { if (k !== "ids" && k !== "focus") { body.append(k, v); inputs.push(k + "=" + v); } });
    const key = [box.dataset.navy, box.dataset.comp, ...inputs.sort()].join("|");
    const show = (ev) => {
      if (mine !== box.talkSeq) return;   // a box set aside under a preview still gets its text
      if (ev.type === "preview") {
        textEl.innerHTML = `<p class="talk-body draft">${esc(ev.text)}</p><p class="muted small">Checking every figure against the card…</p>`;
      } else if (ev.type === "done") {
        const note = ev.outcome === "pass" ? "Checked against the card."
          : ev.outcome === "partial" ? "Some sentences were dropped because they couldn't be checked."
          : ev.outcome === "fallback" ? "The written version didn't pass the checks, so here are the facts." : "";
        textEl.innerHTML = `<p class="talk-body">${esc(ev.text).split("\n").join("<br>")}</p>${note ? `<p class="muted small">${esc(note)} Premiums are before the government rebate.</p>` : ""}`;
        actions.hidden = false;
      } else if (ev.type === "error") {
        textEl.innerHTML = `<p class="error">${esc(ev.error)}</p>`;
        actions.hidden = false;
      }
    };
    if (!fresh && written.has(key)) { show(written.get(key)); return; }
    textEl.innerHTML = `<p class="muted small talk-wait">Writing what to say…</p>`;
    if (!fresh && writing.has(key)) { writing.get(key).add(show); return; }
    const listeners = new Set([show]);
    writing.set(key, listeners);
    const emit = (ev) => {
      if (ev.type === "done" && (ev.outcome === "pass" || ev.outcome === "partial")) written.set(key, ev);
      listeners.forEach((fn) => fn(ev));
    };
    try {
      await streamCompose("/script/stream", body, emit);
    } catch (err) {
      emit({ type: "error", error: err.message });
    } finally {
      if (writing.get(key) === listeners) writing.delete(key);
    }
  }
  // Shared by the talking point and objection replies: read one compose SSE stream, an event at a
  // time, falling back to a single JSON reply where streaming isn't available.
  async function streamCompose(url, body, emit) {
    const r = await fetch(url, { method: "POST", body, credentials: "same-origin" });
    const type = r.headers.get("content-type") || "";
    if (type.includes("application/json")) {
      const d = await r.json();
      emit(d.error ? { type: "error", error: d.error } : { type: "done", text: d.text, outcome: d.outcome });
      return;
    }
    if (!type.includes("text/event-stream")) throw new Error("Your session may have ended. Reload the page.");
    const handle = (chunk) => chunk.split("\n\n").forEach((block) => {
      const line = block.split("\n").find((l) => l.startsWith("data: "));
      if (line) { try { emit(JSON.parse(line.slice(6))); } catch (e) { /* a partial block */ } }
    });
    if (r.body && r.body.getReader) {
      const reader = r.body.getReader(), decoder = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const cut = buf.lastIndexOf("\n\n");
        if (cut >= 0) { handle(buf.slice(0, cut)); buf = buf.slice(cut + 2); }
      }
      if (buf) handle(buf);
    } else {
      handle(await r.text());
    }
  }
  function copyTalk(box) {
    const t = $(".talk-body", box);
    if (!t) return;
    const text = t.innerText || t.textContent;
    if (navigator.clipboard) navigator.clipboard.writeText(text).then(() => showNotice("Copied."), () => {});
  }

  // ── handling what the member actually said, mid-call ─────────────────────────
  // Every "ask" appends its own entry to the log, so a rep can prepare for more than one objection
  // (or handle several in the same call) and still see what was asked each time.
  async function askObjection(box, { preset, text, label }) {
    const log = $(".objection-log", box);
    const entry = document.createElement("div");
    entry.className = "objection-entry";
    entry.innerHTML = `<p class="objection-said">${esc(label || text)}</p><p class="objection-reply draft muted small">Thinking…</p>`;
    log.insertBefore(entry, log.firstChild);
    const body = new FormData();
    body.append("csrf", csrf);
    body.append("navy", box.dataset.navy);
    body.append("comp", box.dataset.comp);
    if (preset) body.append("preset", preset); else body.append("objection", text);
    cardParams().forEach((v, k) => { if (k !== "ids" && k !== "focus") body.append(k, v); });
    const replyEl = () => $(".objection-reply", entry);
    const show = (ev) => {
      if (ev.type === "preview") {
        replyEl().outerHTML = `<p class="objection-reply draft">${esc(ev.text)}</p>`;
      } else if (ev.type === "done") {
        const note = ev.outcome === "pass" ? "" : ev.outcome === "partial" ? "Part of this couldn't be checked, so it was dropped."
          : ev.outcome === "fallback" ? "That didn't pass the checks, so here are the facts instead." : "";
        replyEl().outerHTML = `<p class="objection-reply">${esc(ev.text).split("\n").join("<br>")}</p>${note ? `<p class="muted small objection-note">${esc(note)}</p>` : ""}`;
      } else if (ev.type === "error") {
        replyEl().outerHTML = `<p class="error">${esc(ev.error)}</p>`;
      }
    };
    try {
      await streamCompose("/script/objection/stream", body, show);
    } catch (err) {
      show({ type: "error", error: err.message });
    }
  }

  // ── what they would get back ─────────────────────────────────────────────────
  // One usage profile for the whole call (what this household claims in a year), priced against every
  // card. The model only reads the rep's words into lines (/call/usage); every dollar is worked out on
  // the server from the policies' own benefits and limits (/call/estimate). Every line stays editable.
  const ITEMS = (() => { try { return JSON.parse($("#extras-items").textContent); } catch (e) { return []; } })();
  const ITEM = new Map(ITEMS.map((i) => [i.item, i]));
  const QUICK = [
    ["Dental check-ups", [["DentalGeneral012PeriodicExam", 2], ["DentalGeneral114ScaleClean", 2]]],
    ["Glasses", [["OpticalSingleVisionLenses", 1]]],
    ["Physio course", [["PhysiotherapyInitial", 1], ["PhysiotherapySubsequent", 5]]],
    ["Massage", [["RemedialMassageInitial", 1], ["RemedialMassageSubsequent", 5]]],
    ["Chiro", [["ChiropracticInitial", 1], ["ChiropracticSubsequent", 5]]],
    ["Psychology", [["PsychologyInitial", 1], ["PsychologySubsequent", 5]]],
    ["Braces", [["Orthodontic881BracesUpperLowerPlusRetainer", 1]]],
    ["Crown", [["DentalMajor615FullCrownVeneered", 1]]],
  ];
  let usage = [];                       // [{item, times, people, fee}]
  const dollars = (v) => "$" + Math.round(v).toLocaleString("en-AU");
  const newLine = (item, times, people) => ({ item, times, people: people || 1, fee: (ITEM.get(item) || {}).fee ?? null });

  function wireEstimator(box) {
    const quick = $(".est-quick", box), input = $(".est-text", box), read = $(".est-read", box);
    quick.innerHTML = QUICK.map(([label], i) => `<button type="button" class="pill est-add" data-i="${i}">+ ${esc(label)}</button>`).join("");
    $$(".est-add", quick).forEach((b) => b.addEventListener("click", () => {
      QUICK[Number(b.dataset.i)][1].forEach(([item, times]) => usage.push(newLine(item, times, 1)));
      refreshEstimators();
    }));
    const go = () => { const text = input.value.trim(); if (text) readUsage(box, text); };
    read.addEventListener("click", go);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } });
    renderLines(box);
    if (usage.length) estimateFor(box);
  }
  function refreshEstimators() {
    $$(".estimator", result).forEach((box) => { renderLines(box); estimateSoon(box); });
  }
  function itemOptions(selected) {
    let html = "", group = "";
    ITEMS.forEach((i) => {
      if (i.service !== group) { html += `${group ? "</optgroup>" : ""}<optgroup label="${esc(i.service)}">`; group = i.service; }
      html += `<option value="${esc(i.item)}"${i.item === selected ? " selected" : ""}>${esc(i.label)}</option>`;
    });
    return html + "</optgroup>";
  }
  function renderLines(box) {
    const el = $(".est-lines", box);
    if (!usage.length) { el.innerHTML = ""; $(".est-result", box).innerHTML = ""; return; }
    el.innerHTML = usage.map((u, i) => `<div class="est-line" data-i="${i}">
          <select class="est-item" aria-label="Item">${itemOptions(u.item)}</select>
          <button type="button" class="x" title="Remove">×</button>
          <label>× a year <input type="number" class="est-times" min="1" max="52" value="${u.times}"></label>
          <label>people <input type="number" class="est-people" min="1" max="8" value="${u.people}"></label>
          <label title="Typical fee each time; change it if you know theirs">fee $<input type="number" class="est-fee" min="0" step="1" value="${u.fee ?? ""}"></label>
        </div>`).join("");
    $$(".est-line[data-i]", el).forEach((row) => {
      const i = Number(row.dataset.i);
      const changed = () => {
        const item = $(".est-item", row).value;
        if (item !== usage[i].item) { usage[i].item = item; usage[i].fee = (ITEM.get(item) || {}).fee ?? null; $(".est-fee", row).value = usage[i].fee ?? ""; }
        usage[i].times = Math.max(1, Math.min(52, Number($(".est-times", row).value) || 1));
        usage[i].people = Math.max(1, Math.min(8, Number($(".est-people", row).value) || 1));
        const fee = $(".est-fee", row).value;
        usage[i].fee = fee === "" ? null : Number(fee);
        $$(".estimator", result).forEach((b) => { if (b !== box) renderLines(b); estimateSoon(b); });
      };
      $$("select, input", row).forEach((f) => f.addEventListener("change", changed));
      $(".x", row).addEventListener("click", () => { usage.splice(i, 1); refreshEstimators(); });
    });
  }
  async function readUsage(box, text) {
    const out = $(".est-result", box);
    out.innerHTML = `<p class="muted small">Reading that…</p>`;
    const body = new FormData();
    body.append("csrf", csrf); body.append("text", text); body.append("family", (live && live.family) || S.family);
    try {
      const r = await fetch("/call/usage", { method: "POST", body, credentials: "same-origin" });
      const d = await r.json();
      if (d.error) { out.innerHTML = `<p class="error">${esc(d.error)}</p>`; return; }
      if (!d.uses.length) { out.innerHTML = `<p class="muted small">I couldn't find any extras in that. Try “check-ups for both, glasses, weekly physio”, or use the buttons.</p>`; return; }
      usage = d.uses.map((u) => newLine(u.item, u.times, u.people));
      $(".est-text", box).value = "";
      refreshEstimators();
      if (d.unclear && d.unclear.length) showNotice(`Left out, not an extras claim: ${d.unclear.join("; ")}.`);
    } catch (err) {
      out.innerHTML = `<p class="error">${esc(err.message)}</p>`;
    }
  }
  function estimateSoon(box) {
    clearTimeout(box.estTimer);
    box.estTimer = setTimeout(() => estimateFor(box), 250);
  }
  async function estimateFor(box) {
    const out = $(".est-result", box), sheet = box.closest(".sheet");
    $$(".xgroup", sheet).forEach((g) => { g.classList.remove("used"); const m = $(".x-used", g); if (m) m.remove(); });
    if (!usage.length) { out.innerHTML = ""; return; }
    const mine = box.estSeq = (box.estSeq || 0) + 1;
    const body = new FormData();
    body.append("csrf", csrf); body.append("navy", box.dataset.navy); body.append("comp", box.dataset.comp);
    body.append("uses", JSON.stringify(usage));
    cardParams().forEach((v, k) => { if (k !== "ids" && k !== "focus") body.append(k, v); });
    try {
      const r = await fetch("/call/estimate", { method: "POST", body, credentials: "same-origin" });
      const d = await r.json();
      if (mine !== box.estSeq) return;
      if (d.error) { out.innerHTML = `<p class="error">${esc(d.error)}</p>`; return; }
      out.innerHTML = estimateHtml(d);
      new Set(usage.map((u) => (ITEM.get(u.item) || {}).svc)).forEach((svc) => {
        const g = sheet && sheet.querySelector(`.xgroup[data-service="${svc}"]`);
        if (g) { g.classList.add("used"); g.hidden = false; $(".x-open", g).insertAdjacentHTML("afterend", `<span class="x-used">in use</span>`); }
      });
    } catch (err) {
      if (mine === box.estSeq) out.innerHTML = `<p class="error">${esc(err.message)}</p>`;
    }
  }
  function estimateHtml(d) {
    const n = d.navy, c = d.comp;
    // Rounded once, here, so the figures in the sentence always add up as said.
    const back = Math.round(n.total) - Math.round(c.total);
    const prem = n.premium_year != null && c.premium_year != null ? Math.round(c.premium_year - n.premium_year) : null;
    let say = back === 0 ? "On how they use their extras, both pay back about the same"
      : `On how they use their extras, ${back > 0 ? "Navy Health" : esc(c.fund)} pays back about ${dollars(Math.abs(back))} more a year`;
    if (prem) say += back > 0 ? `, and costs ${dollars(Math.abs(prem))} a year ${prem > 0 ? "less" : "more"}`
      : `, and Navy Health costs ${dollars(Math.abs(prem))} a year ${prem > 0 ? "less" : "more"}`;
    say += ".";
    if (prem != null && back + prem !== 0 && prem !== 0 && back !== 0) {
      const net = back + prem;
      say += ` Overall that's about ${dollars(Math.abs(net))} a year ${net > 0 ? "better off with Navy Health" : "better off with " + esc(c.fund)}.`;
    }
    const rows = n.lines.map((ln, i) => {
      const cl = c.lines[i] || {};
      const notes = [ln.note && `Navy: ${ln.note}`, cl.note && `Theirs: ${cl.note}`].filter(Boolean).join(" · ");
      return `<tr><td>${esc(ln.service)}: ${esc(ln.label)} ×${ln.claims}${notes ? `<br><span class="muted">${esc(notes)}</span>` : ""}</td>
        <td>${dollars(ln.paid)}</td><td>${dollars(cl.paid || 0)}</td></tr>`;
    }).join("");
    return `<div class="est-totals">
        <div class="est-total${back > 0 ? " win" : ""}"><span class="who">Navy Health pays back</span><strong>${dollars(n.total)}</strong> <span class="muted small">a year</span></div>
        <div class="est-total${back < 0 ? " win" : ""}"><span class="who">${esc(c.fund)} pays back</span><strong>${dollars(c.total)}</strong> <span class="muted small">a year</span></div>
      </div>
      <p class="est-say">${say}</p>
      <details class="est-break"><summary>How that's worked out</summary>
        <table><thead><tr><th></th><th>Navy</th><th>Theirs</th></tr></thead><tbody>${rows}</tbody></table>
      </details>
      <p class="muted small est-note">An estimate from each policy's published benefits and annual limits. Where a policy pays a percentage, it's of the typical fee shown. Waiting periods aren't applied. Premiums before the rebate.</p>`;
  }

  // ── the model, for what was not recognised ───────────────────────────────────
  let modelTimer = null, latestModel = 0;
  function modelSoon() {
    clearTimeout(modelTimer);
    if (!live || !live.ignored.length) return;
    const meaningful = live.ignored.filter((w) => w.length > 2);
    if (meaningful.length < 1 || live.raw.trim().split(/\s+/).length < 2) return;
    modelTimer = setTimeout(() => askTheModel(live ? live.raw : "", true), 900);
  }
  async function askTheModel(text, whileTyping) {
    if (!text.trim()) return;
    const mine = ++latestModel;
    thinking.hidden = false;
    const body = new FormData();
    body.append("csrf", csrf);
    body.append("request", text);
    if (S.navy.length || S.comp.length) {
      body.append("context", JSON.stringify({
        navy: S.navy.map((p) => p.name).concat(S.autoNavy ? [S.autoNavy.name] : []),
        competitors: S.comp.map((p) => p.name), state: S.state, household: S.family,
        needs: [...S.needs], benefits: S.focus.map((k) => IX.labels[k] || k),
      }));
    }
    try {
      const r = await fetch("/call/plan", { method: "POST", body, credentials: "same-origin" });
      const d = await r.json();
      if (mine !== latestModel) return;
      if (whileTyping && bar.value.trim() !== text.trim()) return;   // the rep has moved on
      applyModel(d, whileTyping);
    } catch (err) {
      /* the deterministic reading stands; nothing to show */
    } finally {
      if (mine === latestModel) thinking.hidden = true;
    }
  }
  function applyModel(d, whileTyping) {
    if (!d || d.error || !d.understood) return;
    // Only fill what the rep has not already confirmed or typed.
    const suggestionsFromModel = [];
    ["navy", "comp"].forEach((side) => (d[side] || []).forEach((g) => {
      const ids = (g.policies || []).map((p) => p.id).filter((id) => IX.byId.has(id));
      if (!ids.length) return;
      const already = (side === "navy" ? S.navy : S.comp).some((p) => ids.includes(p.id));
      if (already) return;
      const cands = ids.map((id) => IX.byId.get(id));
      const picked = (g.pick || []).filter((id) => IX.byId.has(id));
      suggestionsFromModel.push({ text: g.term, side, policy: picked.length === 1 ? IX.byId.get(picked[0]) : null,
        cands: cands.map((p) => ({ p, score: 0 })), question: picked.length === 1 ? null : splitQuestion(cands) });
    }));
    if (whileTyping && live) {
      // Merge into the live reading: add what the browser did not find.
      suggestionsFromModel.forEach((s) => { if (!live.refs.some((r) => r.text === s.text)) live.refs.push(s); });
      (d.features || []).forEach((f) => { if (!live.focus.includes(f.key)) live.focus.push(f.key); });
      if (d.state && !live.state) live.state = d.state;
      if (d.family && !live.family) live.family = d.family;
      live.ignored = [];
      renderSuggestions(); renderSentence(); previewSoon();
      return;
    }
    if (d.intent === "new") { S.navy = []; S.comp = []; S.autoNavy = null; S.questions = []; }
    const asked = (s) => S.questions.some((q) => q.cands.some((c) => s.cands.some((x) => x.p.id === c.p.id)));
    suggestionsFromModel.forEach((s) => { if (s.policy) addPolicy(s.policy); else if (!asked(s)) S.questions.push(s); });
    (d.features || []).forEach((f) => { if (!S.focus.includes(f.key)) S.focus.push(f.key); });
    if (d.state && S.stateFrom !== "chosen") { S.state = d.state; S.stateFrom = "typed"; remember(); }
    if (d.family && S.familyFrom !== "chosen") { S.family = d.family; S.familyFrom = "typed"; remember(); }
    if (d.intent === "cheaper" && S.cheaper.length) setNavy(S.cheaper[0].id);
    renderSentence();
    compare(true);
  }

  function showNotice(msg, kind, ms) {
    notice.hidden = false;
    notice.className = "notice" + (kind ? " " + kind : "");
    notice.textContent = msg;
    clearTimeout(showNotice.t);
    showNotice.t = setTimeout(() => { notice.hidden = true; }, ms || 4000);
  }

  bar.disabled = true;
  renderSentence();
  loadIndex().then(() => bar.focus());
})();
