// Upstream Management frontend — single-page app that talks to the Flask API.
// Pages: /repos (repo list + per-repo detail), /requests (approvals), /settings (LLM).
//
// Path-based routing (not hash) so it works both standalone (port 5001) and
// embedded behind Studio's `/upstream` proxy. API/static calls are prefixed
// with BASE so they resolve correctly in either layout.

const BASE = (
  document.getElementById('upstream-base')?.getAttribute('content') || ''
).replace(/\/+$/, '')
// route() takes a bare relative path (e.g. "/requests", "repo/123") and joins
// it with BASE. The click interceptor strips the leading BASE off absolute
// hrefs before calling this so we never double-prefix the path. Also triggers
// router() so every navigation (including top-nav clicks) re-renders.
const route = (path) => { history.pushState({}, '', `${BASE}/${path.replace(/^\/+/, '')}`); router() }

const $ = (sel, root = document) => root.querySelector(sel)
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)]
// JSON API call. Resolves with the parsed body on 2xx; otherwise rejects with an
// Error carrying the server's message (`.status`, `.data` attached), so every
// failure reaches a catch or the global handler below instead of breaking a panel.
async function api(path, opts = {}) {
  // callers usually pass apiUrl(...), which already carries BASE; never add it twice
  const url = /^https?:\/\//.test(path) || (BASE && path.startsWith(`${BASE}/`)) ? path : `${BASE}${path}`
  let r
  try {
    r = await fetch(url, { headers: { "Content-Type": "application/json", ...opts.headers }, ...opts })
  } catch {
    throw new Error("Can't reach the Upstream Management server. Is it running?")
  }
  const data = await r.json().catch(() => null)
  if (!r.ok) {
    const err = new Error(data?.error || `${r.status} ${r.statusText || "request failed"}`)
    err.status = r.status
    err.data = data
    throw err
  }
  return data
}
const apiUrl = (p) => `${BASE}${p}`

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag)
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v
    else if (k === "html") e.innerHTML = v
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v)
    else if (v !== null && v !== undefined) e.setAttribute(k, v)
  }
  for (const kid of kids.flat()) {
    if (kid == null) continue
    e.append(kid.nodeType ? kid : document.createTextNode(String(kid)))
  }
  return e
}

const loading = (text = "Loading…") => el("div", { class: "loading" }, text)

// Disable `btn` and show `label` while `fn` runs; errors become a toast. Returns fn's result.
async function busy(btn, label, fn) {
  const old = btn.textContent
  btn.disabled = true
  btn.setAttribute("aria-busy", "true")
  if (label) btn.textContent = label
  try { return await fn() }
  catch (e) { toast(e, "error") }
  finally {
    if (btn.isConnected) { btn.disabled = false; btn.removeAttribute("aria-busy"); btn.textContent = old }
  }
}

function toast(msg, type = "info") {
  if (msg instanceof Error) msg = msg.message
  const box = el("div", { class: `toast toast-${type}`, role: type === "error" ? "alert" : "status" }, msg)
  box.style.cssText = "position:fixed;bottom:20px;right:20px;padding:12px 18px;border-radius:8px;color:#fff;font-size:14px;z-index:999;box-shadow:0 4px 12px rgba(0,0,0,.4)"
  box.style.background = type === "error" ? "#b33" : type === "ok" ? "#2a7d3b" : "#2f5bff"
  document.body.append(box)
  setTimeout(() => box.remove(), type === "error" ? 6000 : 3000)
}

// Anything async that failed without its own handler still tells the user.
window.addEventListener("unhandledrejection", (e) => { toast(e.reason instanceof Error ? e.reason : String(e.reason), "error") })

// --- dashboard (repos page) ---
const badge = (cls, txt, title) => el("span", { class: `badge ${cls}`, title: title || null }, txt)
const ago = (iso) => {
  if (!iso) return ""
  const s = (Date.now() - new Date(iso).getTime()) / 1000
  if (s < 90) return "just now"
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86400)}d ago`
}

async function refreshPendingCount() {
  try {
    const reqs = await api(apiUrl('/api/requests'))
    $("#pending-count").textContent = reqs.filter(r => r.status === "pending").length || ""
    return reqs
  } catch { return [] }
}

async function refreshModelStatus() {
  const box = $("#model-status")
  try {
    const s = await api(apiUrl('/api/settings'))
    const ok = !!(s.llm?.base_url && s.llm?.model)
    box.className = `status-dot ${ok ? "ok" : "bad"}`
    box.lastChild.textContent = ok ? `Model: ${s.llm.model}` : "No model set"
    box.title = ok ? s.llm.base_url : "Set the model connection in Settings"
  } catch { box.className = "status-dot" }
}

// What a request would do, in one line.
function requestLine(r) {
  if (r.type === "push") return `push ${r.branch} to ${r.remote}` + (r.open_pr ? ` and open a PR into ${r.base}` : "")
  if (r.type === "actions") return `GitHub Actions: ${(r.disable || []).length} to disable, ${(r.enable || []).length} to enable`
  if (r.type === "components") return `bump ${(r.components || []).length} submodule pin(s)`
  return (r.upstreams || []).map(u => u.name === "workspace"
    ? (u.reason || "merge a workspace")
    : `merge ${u.behind} commit(s) from ${u.name}/${u.branch}` + (u.conflict ? `, conflicts in ${(u.conflict_files || []).slice(0, 3).join(", ")}` : u.ahead ? ` · ${u.ahead} of yours · merges cleanly` : "")).join("; ")
}

async function renderRepos() {
  const content = $("#content")
  content.replaceChildren(loading("Checking repos…"))
  const [repos, reqs] = await Promise.all([api(apiUrl('/api/repos')), refreshPendingCount()])
  const pending = reqs.filter(r => r.status === "pending")
  content.innerHTML = ""

  // header + add form
  const addBox = el("div", { class: "card", hidden: "" })
  const urlIn = el("input", { placeholder: "https://github.com/you/project.git", "aria-label": "Git URL", style: "flex:2 1 260px" })
  const dirIn = el("input", { placeholder: "or a local folder: /path/to/repo", "aria-label": "Local folder", style: "flex:2 1 260px" })
  const nameIn = el("input", { placeholder: "display name (optional)", "aria-label": "Display name", style: "flex:1 1 160px" })
  const addBtn = el("button", { class: "btn" }, "Add")
  addBtn.onclick = async () => {
    const url = urlIn.value.trim(), dir = dirIn.value.trim()
    if (!url && !dir) return toast("Enter a git URL or a local folder", "error")
    await busy(addBtn, url ? "Cloning…" : "Adding…", async () => {
      await api(apiUrl('/api/add'), { method: "POST", body: JSON.stringify({ url: url || null, path: dir || null, name: nameIn.value.trim() }) })
      toast("Repo added", "ok"); await renderRepos()
    })
  }
  addBox.append(el("div", { class: "row" }, urlIn, dirIn, nameIn, addBtn),
    el("p", { class: "muted", style: "margin:8px 0 0" }, "A URL is cloned into the watch folder. A local folder is watched where it is. Add more upstreams on the repo's page."))
  const addToggle = el("button", { class: "btn ghost" }, "+ Add repo")
  addToggle.onclick = () => { addBox.hidden = !addBox.hidden; if (!addBox.hidden) urlIn.focus() }
  const fetchBtn = el("button", { class: "btn ghost" }, "Fetch all")
  fetchBtn.onclick = () => busy(fetchBtn, "Fetching…", async () => {
    const failed = []
    await Promise.all(repos.map(r => api(apiUrl(`/api/repo/${r.id}/fetch`), { method: "POST", body: "{}" }).catch(() => failed.push(r.name || r.id))))
    if (failed.length) toast(`Fetch failed for ${failed.join(", ")}`, "error")
    await renderRepos()
  })
  const runBtn = el("button", { class: "btn" }, "▶ Run daily check")
  runBtn.onclick = () => busy(runBtn, "Running…", async () => {
    const res = await api(apiUrl('/api/run-daily'), { method: "POST", body: "{}" })
    const n = (s) => res.results.filter(r => r.status === s).length
    toast(`Done: ${n("merged")} fast-forwarded, ${n("request_raised")} requests raised, ${n("skipped")} skipped, ${n("error")} errors`
      + res.results.filter(r => r.status === "skipped").map(r => `\n${r.repo_name}: ${r.detail}`).join(""), n("error") || n("skipped") ? "error" : "ok")
    await renderRepos()
  })
  content.append(el("div", { class: "col", style: "gap:16px" },
    el("div", { class: "row" }, el("h1", { class: "page" }, "Repositories"), el("div", { class: "spacer" }), addToggle, fetchBtn, runBtn),
    addBox))
  const wrap = content.firstChild

  if (!repos.length) {
    addBox.hidden = false
    wrap.append(el("p", { class: "muted" }, "No repos yet. Add one above, or drop a clone into the watch folder."))
    return
  }

  // tiles
  const ups = (r) => (r.upstream_details || []).filter(u => u.enabled !== false)
  const behindRepos = repos.filter(r => ups(r).some(u => u.behind > 0))
  const attention = []
  for (const r of repos) {
    if (r.error) attention.push({ repo: r, text: r.error })
    for (const u of ups(r)) if (u.missing_ref) attention.push({ repo: r, text: `${u.name}/${u.branch}: ${u.conflict_detail || "not compared"}`, action: "Set upstream branch" })
    if (r.dirty_tracked && ups(r).some(u => u.behind > 0)) attention.push({ repo: r, text: `behind upstream, but ${r.dirty_tracked} uncommitted change(s) to tracked files block merging: commit or stash them`, action: "Show changes" })
  }
  const tile = (label, value, cls) => el("div", { class: `tile ${cls || ""}` }, el("span", { class: "muted" }, label), el("b", {}, value))
  wrap.append(el("div", { class: "tiles" },
    tile("Repos", repos.length),
    tile("Behind upstream", behindRepos.length ? `${behindRepos.length} repo${behindRepos.length > 1 ? "s" : ""}` : "None", behindRepos.length ? "warn" : "good"),
    tile("Waiting for approval", pending.length || "None", pending.length ? "warn" : "good"),
    tile("Needs attention", attention.length || "None", attention.length ? "bad" : "good")))

  // approvals
  if (pending.length) {
    const box = el("section", { class: "notice approve", "aria-labelledby": "h-approve" }, el("div", { class: "row" }, el("h2", { id: "h-approve" }, "Needs your approval"), el("div", { class: "spacer" }), el("a", { href: `${BASE}/requests`, class: "small" }, "All requests")))
    for (const r of pending.slice(0, 6)) {
      const conflicted = (r.upstreams || []).filter(u => u.conflict)
      const acts = el("div", { class: "row" })
      if (conflicted.length) for (const u of conflicted) acts.append(el("button", { class: "btn ghost small", onclick: () => showPullReport(r.repo_id, { upstream: u.name }) }, conflicted.length > 1 ? `Pull report: ${u.name}` : "Pull report"))
      else {
        const ok = el("button", { class: "btn small" }, r.type === "push" ? "Approve and push" : r.type === "actions" ? "Approve" : "Approve and merge")
        ok.onclick = () => busy(ok, "Working…", async () => {
          const res = await api(apiUrl(`/api/request/${encodeURIComponent(r.id)}/decide`), { method: "POST", body: JSON.stringify({ decision: "approve" }) })
          const detail = (res.request?.result || []).map(o => o.detail).join("; ")
          toast(res.ok ? `Done. ${detail}` : `Not done. ${res.error || detail}`, res.ok ? "ok" : "error")
          await renderRepos()
        })
        acts.append(el("button", { class: "btn ghost small", onclick: () => route("/requests") }, "Details"), ok)
      }
      box.append(el("div", { class: "item" }, el("div", {}, el("strong", {}, r.repo_name || r.repo_id), el("span", { class: "muted" }, ` · ${requestLine(r)}`),
        r.summary ? el("div", { class: "small", style: "margin-top:2px;color:#d9c9a8" }, `Agent: ${r.summary}`) : null), acts))
    }
    wrap.append(box)
  }

  // attention
  if (attention.length) {
    const box = el("section", { class: "notice attention", "aria-labelledby": "h-att" }, el("h2", { id: "h-att" }, "Needs attention"))
    for (const a of attention.slice(0, 8)) box.append(el("div", { class: "item" },
      el("div", {}, el("strong", {}, a.repo.name || a.repo.id), el("span", { class: "muted" }, ` · ${a.text}`)),
      el("button", { class: "btn ghost small", onclick: () => route(`/repo/${a.repo.id}`) }, a.action || "Open")))
    wrap.append(box)
  }

  // table
  const tbody = el("tbody")
  for (const r of repos) {
    const lc = r.last_commit
    const status = el("div", { class: "row", style: "gap:4px" })
    for (const u of ups(r)) status.append(...upstreamBadges(u).map(b => { b.title = `${u.name}/${u.branch}: ${b.title || b.textContent}`; return b }))
    if (!ups(r).length) status.append(badge("", "no upstream"))
    if (r.dirty) status.append(badge("warn", `${r.dirty} uncommitted`))
    for (const c of r.components || []) status.append(componentBadge(c))
    const tr = el("tr", { class: "click", onclick: (e) => { if (!e.target.closest("a")) route(`/repo/${r.id}`) } },
      el("td", {}, el("a", { href: `${BASE}/repo/${r.id}`, style: "font-weight:600;color:var(--text);text-decoration:none" }, r.name || r.id),
        el("div", { class: "muted small" }, `${r.focus_branch || ""} · ${r.path}`)),
      el("td", { class: "muted" }, ups(r).map(u => `${u.name}/${u.branch}`).join(", ") || "-"),
      el("td", {}, status),
      el("td", { class: "muted" }, lc ? [el("code", { style: "color:var(--accent2)" }, lc.sha), ` ${lc.subject} · ${ago(lc.date)}`] : ""))
    tbody.append(tr)
  }
  wrap.append(el("div", { class: "table-box" }, el("table", { class: "list" },
    el("thead", {}, el("tr", {}, el("th", {}, "Repository"), el("th", {}, "Upstreams"), el("th", {}, "Status"), el("th", {}, "Last commit"))), tbody)))
}

function upstreamBadges(u) {
  if (u.missing_ref) return [badge("warn", "not compared", u.conflict_detail || "press Fetch")]
  const out = []
  if (u.up_to_date) out.push(badge("up", "up to date"))
  else {
    if (u.behind) out.push(badge("warn", `${u.behind} behind`, "commits the upstream has that you lack"))
    if (u.ahead) out.push(badge("", `${u.ahead} ahead`, "commits only you have"))
  }
  if (u.conflict) out.push(badge("bad", `${(u.conflict_files || []).length} conflicts`, (u.conflict_files || []).join("\n")))
  else if (u.behind && u.ahead) out.push(badge("up", "merges cleanly"))
  return out
}

// One compact badge per component for the repo list.
function componentBadge(c) {
  if (c.status === "disabled") return badge("", `${c.name}: off`)
  if (c.status === "up_to_date") return badge("up", `${c.name} ✓`, "pin matches upstream")
  if (["not_checked_out", "not_fetched", "error"].includes(c.status)) return badge("warn", `${c.name}: ?`, c.detail || "press Fetch")
  return badge(c.local ? "bad" : "warn", `${c.name} ↓${c.behind}`, `${c.behind} upstream commits newer than the pin` + (c.local ? `; ${c.local} pinned commits not upstream` : ""))
}

// ---- tagged items: commits/files the user pins to the chat so the model can read them
const loadTags = (id) => { try { return JSON.parse(localStorage.getItem(`tags:${id}`) || "[]") } catch { return [] } }
const saveTags = (id, t) => { try { localStorage.setItem(`tags:${id}`, JSON.stringify(t)) } catch {} }
// The server reads at most this many attachments per message (llm.MAX_ATTACHMENTS).
const MAX_TAGS = 6
let refreshTray = () => {}
function addTag(repoId, tag) {
  const t = loadTags(repoId)
  if (t.some(x => x.type === tag.type && x.id === tag.id)) return toast(`${tag.label} is already tagged`, "info")
  if (t.length >= MAX_TAGS) return toast(`You can tag up to ${MAX_TAGS} items. Remove one from the chat tray first.`, "error")
  t.push(tag)
  saveTags(repoId, t)
  refreshTray()
  toast(`Tagged ${tag.label} for the chat`, "ok")
}

// ↓ behind = commits the upstream has that you lack; ↑ ahead = commits only you have.
let tools = null  // the open repo page's tool tabs: {show(name), openFiles(ws)}

const store_ = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v) } catch { return d } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)) } catch {} },
}

async function renderRepoPage(repoId) {
  const content = $("#content")
  content.replaceChildren(loading())
  let repo, branches
  try { [repo, branches] = await Promise.all([api(apiUrl(`/api/repo/${repoId}`)), repoBranches(repoId)]) }
  catch (e) { content.replaceChildren(el("p", { class: "muted" }, e.status === 404 ? "Repo not found." : e.message)); return }
  content.innerHTML = ""

  const focusSel = el("select", { id: `focus-${repoId}`, "aria-label": "Focus branch" })
  for (const b of branches.filter(b => !b.startsWith("upkeep/"))) focusSel.append(el("option", { value: b }, b))
  focusSel.value = repo.focus_branch
  focusSel.onchange = async () => {
    focusSel.disabled = true
    try {
      await api(apiUrl(`/api/repo/${repoId}/focus`), { method: "POST", body: JSON.stringify({ branch: focusSel.value }) })
      toast("Focus branch updated", "ok"); await renderRepoPage(repoId)
    } catch (e) { toast(e, "error"); focusSel.disabled = false; focusSel.value = repo.focus_branch }
  }
  const tree = el("span", { class: "badge" }, "…")
  const pathEl = el("button", { class: "path btn link", title: `${repo.path} (click to copy)`, onclick: () => { navigator.clipboard?.writeText(repo.path); toast("Path copied", "ok") } }, repo.path)
  content.append(el("div", { class: "repo-head" },
    el("a", { href: `${BASE}/repos`, class: "muted", style: "text-decoration:none" }, "Repos"), el("span", { class: "muted" }, "/"),
    el("h1", { class: "page" }, repo.name || repoId),
    el("label", { class: "row muted", style: "gap:6px" }, "Focus", focusSel), tree, el("div", { class: "spacer" }), pathEl))

  const upWrap = el("div", { class: "panel ups" }, loading("Comparing upstreams…"))
  const compWrap = el("div")
  content.append(upWrap, compWrap)
  refreshUpstreams(upWrap, repoId, tree).catch(e => upWrap.replaceChildren(el("p", { class: "muted", style: "padding:10px" }, `Could not compare upstreams: ${e.message}`)))
  renderComponents(compWrap, repoId).catch(e => compWrap.replaceChildren(el("p", { class: "muted" }, `Could not read components: ${e.message}`)))

  // workbench: tools | splitter | chat
  const bench = el("div", { class: "workbench" })
  const toolsPane = el("section", { class: "pane tools", "aria-label": "Repo tools" })
  const splitter = el("div", { class: "splitter", role: "separator", "aria-orientation": "vertical", "aria-label": "Resize chat", tabindex: "0" })
  const chatPane = el("aside", { class: "pane chat", "aria-label": "Chat" })
  bench.append(toolsPane, splitter, chatPane)
  content.append(bench)
  const setW = (w) => { w = Math.max(320, Math.min(w, bench.clientWidth - 360)); bench.style.setProperty("--chat-w", `${w}px`); return w }
  // The workbench fills the rest of the window, whatever the upstream panel above it takes.
  const fit = () => { if (!bench.isConnected) return removeEventListener("resize", fit); bench.style.setProperty("--bench-h", `${Math.max(480, innerHeight - bench.getBoundingClientRect().top + scrollY - 16)}px`) }
  addEventListener("resize", fit); new ResizeObserver(fit).observe(upWrap)
  setW(store_.get("chatWidth", 460))
  splitter.addEventListener("pointerdown", (e) => {
    e.preventDefault()  // or the browser starts a text selection and takes the pointer away
    splitter.setPointerCapture(e.pointerId); splitter.classList.add("drag"); document.body.style.userSelect = "none"
    const move = (ev) => setW(bench.getBoundingClientRect().right - ev.clientX)
    const up = () => {
      splitter.classList.remove("drag"); document.body.style.userSelect = ""
      splitter.removeEventListener("pointermove", move); splitter.removeEventListener("pointerup", up); splitter.removeEventListener("pointercancel", up)
      store_.set("chatWidth", parseInt(getComputedStyle(bench).getPropertyValue("--chat-w")))
    }
    splitter.addEventListener("pointermove", move); splitter.addEventListener("pointerup", up); splitter.addEventListener("pointercancel", up)
  })
  splitter.addEventListener("keydown", (e) => {
    const cur = parseInt(getComputedStyle(bench).getPropertyValue("--chat-w")) || 460
    const d = { ArrowLeft: 40, ArrowRight: -40 }[e.key]
    if (d) { e.preventDefault(); store_.set("chatWidth", setW(cur + d)) }
  })
  const setMode = (m) => {
    bench.classList.toggle("chat-wide", m === "wide"); bench.classList.toggle("chat-hidden", m === "hidden")
    store_.set("chatMode", m); chatToggle.textContent = m === "hidden" ? "Show chat" : "Hide chat"
  }
  const chatToggle = el("button", { class: "btn ghost small chat-ctl", onclick: () => setMode(bench.classList.contains("chat-hidden") ? "normal" : "hidden") }, "Hide chat")

  const chat = chatPanel(repoId, { focus: repo.focus_branch, onWide: () => setMode(bench.classList.contains("chat-wide") ? "normal" : "wide"), isWide: () => bench.classList.contains("chat-wide") })
  tools = renderTools(toolsPane, repoId, { extra: chatToggle })
  // Chat sits beside the tools on a wide screen and is a tab on a narrow one.
  const narrow = matchMedia("(max-width: 900px)")
  const place = () => {
    const target = narrow.matches ? tools.chatPanel : chatPane
    if (chat.parentNode !== target) target.append(chat)
  }
  place(); narrow.addEventListener("change", place)
  setMode(store_.get("chatMode", "normal"))
}

async function repoBranches(repoId) {
  try {
    const res = await api(apiUrl(`/api/repo/${repoId}/branches`))
    return res.branches || []
  } catch { return []; }
}

function menu(label, items) {
  const d = el("details", { class: "menu" }, el("summary", { class: "btn ghost icon", "aria-label": label, title: label }, "⋯"))
  const list = el("div", { class: "menu-list", role: "menu" })
  for (const it of items) {
    if (!it) continue
    list.append(el("button", { class: it.danger ? "danger" : "", role: "menuitem", onclick: (e) => { d.open = false; it.run(e) } }, it.label))
  }
  d.append(list)
  document.addEventListener("click", (e) => { if (!d.contains(e.target)) d.open = false })
  return d
}

let refreshCommits = () => {}
async function refreshUpstreams(wrap, repoId, treeBadge) {
  const [repo, cmp] = await Promise.all([api(apiUrl(`/api/repo/${repoId}`)), api(apiUrl(`/api/repo/${repoId}/compare`))])
  const byName = Object.fromEntries((cmp.plan?.upstreams || []).map(u => [u.name, u]))
  const git = cmp.git || {}
  if (treeBadge) { treeBadge.className = `badge ${git.dirty ? "warn" : "up"}`; treeBadge.textContent = git.dirty ? `${git.dirty} uncommitted` : "working tree clean" }
  wrap.innerHTML = ""
  const again = () => refreshUpstreams(wrap, repoId, treeBadge)
  const fetchAll = el("button", { class: "btn ghost small" }, "Fetch all")
  fetchAll.onclick = () => busy(fetchAll, "Fetching…", async () => {
    await api(apiUrl(`/api/repo/${repoId}/fetch`), { method: "POST", body: "{}" })
    await again(); refreshCommits()
  })
  const addBox = el("div", { class: "row", hidden: "", style: "padding:8px 12px;border-top:1px solid #1e2330" })
  const nameIn = el("input", { placeholder: "name, e.g. upstream", "aria-label": "Remote name", style: "flex:1 1 140px" })
  const urlIn = el("input", { placeholder: "https://github.com/org/repo.git", "aria-label": "Remote URL", style: "flex:3 1 260px" })
  const branchIn = el("input", { placeholder: "its branch (default: focus)", "aria-label": "Upstream branch", style: "flex:1 1 160px" })
  const addUpBtn = el("button", { class: "btn small" }, "Add")
  addUpBtn.onclick = () => {
    if (!nameIn.value.trim() || !urlIn.value.trim()) return toast("Enter a remote name and URL", "error")
    return busy(addUpBtn, "Adding…", async () => {
      await api(apiUrl(`/api/repo/${repoId}/upstream`), { method: "POST", body: JSON.stringify({ name: nameIn.value.trim(), url: urlIn.value.trim(), branch: branchIn.value.trim() || null }) })
      toast("Upstream added. Fetch it to compare.", "ok"); await again()
    })
  }
  addBox.append(nameIn, urlIn, branchIn, addUpBtn)
  wrap.append(el("div", { class: "up-row" }, el("h2", {}, "Upstreams"), el("div", { class: "spacer" }),
    el("button", { class: "btn ghost small", onclick: () => branchSetDialog(repoId) }, "Merge branches…"),
    el("button", { class: "btn ghost small", onclick: () => { addBox.hidden = !addBox.hidden; if (!addBox.hidden) nameIn.focus() } }, "+ Add upstream"), fetchAll))
  if (!(repo.upstreams || []).length) wrap.append(el("div", { class: "up-row muted" }, "No upstream yet. Add the project you forked from to compare against it."))
  for (const u of repo.upstreams || []) {
    const c = byName[u.name]
    const acts = el("div", { class: "row", style: "gap:6px" })
    acts.append(el("button", { class: "btn ghost small", "aria-label": `Fetch ${u.name}`, onclick: (e) => busy(e.currentTarget, "Fetching…", async () => {
      const r = await api(apiUrl(`/api/repo/${repoId}/upstream/${u.name}/rebase`), { method: "POST", body: "{}" })
      const err = (r.plan?.upstreams || []).find(x => x.name === u.name)?.conflict_detail
      if (err && /fatal|could not|unable/i.test(err)) toast(err, "error")
      await again(); refreshCommits()
    }) }, "Fetch"))
    if (c && c.behind) acts.append(el("button", { class: "btn ghost small", onclick: (e) => busy(e.currentTarget, "Analysing…", () => showPullReport(repoId, { upstream: u.name })) }, "Pull report"))
    if (c && c.behind && c.conflict) acts.append(el("button", { class: "btn small", title: "Start the merge in a separate checkout and resolve the conflicts there (you or an agent)", onclick: (e) => busy(e.currentTarget, "Opening…", () => openWorkspace(repoId, { upstream: u.name })) }, "Resolve in workspace"))
    acts.append(menu(`More actions for ${u.name}`, [
      c && c.behind && !c.conflict ? { label: "Merge in a workspace", run: () => openWorkspace(repoId, { upstream: u.name }) } : null,
      { label: u.enabled ? "Disable (stop comparing)" : "Enable", run: async () => {
        await api(apiUrl(`/api/repo/${repoId}/upstream/${u.name}/enable`), { method: "POST", body: JSON.stringify({ enabled: !u.enabled }) }); await again() } },
      { label: "Copy URL", run: () => { navigator.clipboard?.writeText(u.url); toast("URL copied", "ok") } },
      { label: "Remove upstream", danger: true, run: async () => {
        if (!confirm(`Remove upstream ${u.name}? Its git remote is removed too.`)) return
        await api(apiUrl(`/api/repo/${repoId}/upstream/${u.name}`), { method: "DELETE", body: "{}" }); await again() } },
    ]))
    const files = c?.conflict_files || []
    wrap.append(el("div", { class: "up-row" },
      el("strong", {}, `${u.name} / ${u.branch}`),
      ...(u.enabled ? (c ? upstreamBadges(c) : []) : [badge("", "disabled")]),
      el("span", { class: "muted small", style: "min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:320px", title: u.url }, u.url),
      el("div", { class: "spacer" }), acts,
      c && c.missing_ref ? el("div", { class: "small", style: "flex-basis:100%;color:var(--warn)" }, c.conflict_detail) : null,
      files.length ? el("details", { style: "flex-basis:100%" }, el("summary", { class: "small muted" }, `Conflicting files (${files.length})`),
        el("div", { class: "small mono", style: "color:var(--bad)" }, ...files.map(f => el("div", {}, f)))) : null))
  }
  wrap.append(addBox)
}

// ---- merge workspaces and branch sets
async function openWorkspace(repoId, body) {
  const res = await api(apiUrl(`/api/repo/${repoId}/workspaces`), { method: "POST", body: JSON.stringify(body) })
  const ws = res.workspace
  toast(ws.ready ? "Everything merged cleanly. Review it, then submit for approval." : ws.next_step, ws.ready ? "ok" : "info")
  tools?.show("Merges")
  return ws
}

async function branchSetDialog(repoId) {
  const [repo, branches] = await Promise.all([api(apiUrl(`/api/repo/${repoId}`)), repoBranches(repoId)])
  const choices = [...branches.filter(b => b !== repo.focus_branch && !b.startsWith("upkeep/")),
    ...(repo.upstreams || []).map(u => u.name)]
  const order = []
  const list = el("div", { class: "col", style: "gap:4px;max-height:50vh;overflow:auto;margin:10px 0" })
  const other = el("input", { placeholder: "or a remote branch, e.g. origin/feature-x", "aria-label": "Remote branch" })
  const summary = el("div", { class: "muted" })
  const redraw = () => {
    list.querySelectorAll("label").forEach(l => {
      const n = order.indexOf(l.dataset.name)
      l.querySelector(".ord").textContent = n >= 0 ? String(n + 1) : ""
    })
    summary.textContent = order.length ? `Merge order: ${order.join(" → ")} → into ${repo.focus_branch}` : "Pick the branches to merge, in the order to merge them."
  }
  for (const b of choices) {
    const isUp = (repo.upstreams || []).some(u => u.name === b)
    const cb = el("input", { type: "checkbox", style: "height:auto" })
    cb.onchange = () => { cb.checked ? order.push(b) : order.splice(order.indexOf(b), 1); redraw() }
    const l = el("label", { class: "row", style: "gap:8px;padding:6px 8px;border:1px solid var(--line);border-radius:8px;cursor:pointer", "data-name": b },
      cb, el("span", { class: "badge info ord", style: "min-width:22px;justify-content:center" }), el("span", {}, b), isUp ? badge("", "upstream") : null)
    list.append(l)
  }
  if (!choices.length) list.append(el("p", { class: "muted" }, "No other branches here. Create feature branches first, or type a remote branch below."))
  const go = el("button", { class: "btn" }, "Open merge workspace")
  let close
  go.onclick = () => {
    const extra = other.value.trim()
    const picks = extra ? [...order, extra] : order
    if (!picks.length) return toast("Pick at least one branch", "error")
    return busy(go, "Merging…", async () => { await openWorkspace(repoId, { branches: picks }); close() })
  }
  close = openDialog(`Merge branches into ${repo.focus_branch}`,
    el("p", { class: "muted", style: "margin:8px 0 0" }, "They merge in a separate checkout, one after another, each as it is right now. A clean merge goes straight on to the next; a conflict stops there so you (in Files) or an agent can resolve it, then Continue. Nothing reaches your branch until you approve the finished set."),
    list, other, summary, el("div", { class: "row", style: "margin-top:10px" }, go))
  redraw()
}

async function renderMerges(wrap, repoId) {
  const res = await api(apiUrl(`/api/repo/${repoId}/workspaces`))
  wrap.innerHTML = ""
  wrap.append(el("div", { class: "row", style: "margin-bottom:10px" },
    el("h3", {}, "Merge workspaces"), el("div", { class: "spacer" }),
    el("button", { class: "btn ghost small", onclick: () => branchSetDialog(repoId) }, "Merge branches…"),
    el("button", { class: "btn ghost small", onclick: () => renderMerges(wrap, repoId) }, "Refresh")))
  if (!res.items.length) {
    wrap.append(el("p", { class: "muted" }, "None open. Use “Resolve in workspace” on a conflicting upstream, or “Merge branches…” to combine several feature branches. A connected coding agent can open and finish these too."))
    return
  }
  const stateBadge = (s) => badge({ merged: "up", conflict: "bad", failed: "bad", pending: "" }[s] || "", s)
  for (const w of res.items) {
    if (w.error) { wrap.append(el("div", { class: "card" }, el("strong", {}, w.name), el("div", { class: "muted" }, w.error))); continue }
    const cont = el("button", { class: "btn small" }, "Continue")
    cont.onclick = () => busy(cont, "Merging…", async () => {
      const r = await api(apiUrl(`/api/repo/${repoId}/workspaces/${w.name}/continue`), { method: "POST", body: "{}" })
      toast(r.workspace.next_step, r.workspace.ready ? "ok" : "info"); await renderMerges(wrap, repoId)
    })
    const submit = el("button", { class: "btn small" }, "Submit for approval")
    submit.onclick = () => {
      const summary = prompt("Summary for the approval (how conflicts were resolved, what you tested):", "") 
      if (summary === null) return
      return busy(submit, "Submitting…", async () => {
        await api(apiUrl(`/api/repo/${repoId}/workspaces/${w.name}/submit`), { method: "POST", body: JSON.stringify({ summary }) })
        toast("Raised. Approve it on the Requests page.", "ok"); await refreshPendingCount(); await renderMerges(wrap, repoId)
      })
    }
    const files = el("button", { class: "btn ghost small", onclick: () => tools?.openFiles(w.name) }, "Open in Files")
    const discard = el("button", { class: "btn ghost small", onclick: () => {
      if (!confirm(`Discard workspace ${w.name}? Its merges and edits are lost.`)) return
      return busy(discard, "Discarding…", async () => { await api(apiUrl(`/api/repo/${repoId}/workspaces/${w.name}`), { method: "DELETE", body: "{}" }); await renderMerges(wrap, repoId) })
    } }, "Discard")
    const needsContinue = !w.ready && (w.merging || (w.sources || []).some(s => s.state !== "merged"))
    wrap.append(el("div", { class: "card", style: "margin-bottom:10px" },
      el("div", { class: "row" }, el("strong", {}, w.branch), w.ready ? badge("up", "ready") : badge("warn", "in progress"), el("div", { class: "spacer" }),
        files, needsContinue ? cont : null, w.ready ? submit : null, discard),
      el("div", { class: "row", style: "gap:6px;margin-top:8px" }, ...(w.sources || []).map((s, i) => el("span", { class: "row", style: "gap:4px" },
        el("span", { class: "muted small" }, `${i + 1}.`), el("span", {}, s.label), stateBadge(s.state)))),
      el("div", { style: "margin-top:8px" }, w.next_step),
      (w.unresolved || []).length ? el("div", { class: "small mono", style: "margin-top:4px;color:var(--bad)" }, `Conflicted: ${w.unresolved.join(", ")}`) : null,
      el("div", { class: "muted small", style: "margin-top:4px" }, w.path)))
  }
}

// Components: the repo's git submodules. Each keeps its own upstream and is
// fetched on its own; one approval moves every pin forward in a single commit.
async function renderComponents(wrap, repoId) {
  const res = await api(apiUrl(`/api/repo/${repoId}/components`))
  wrap.innerHTML = ""
  if (!res.items.length) return
  const fetchAll = el("button", { class: "btn" }, "Fetch components")
  fetchAll.onclick = () => busy(fetchAll, "Fetching…", async () => {
    const r = await api(apiUrl(`/api/repo/${repoId}/components/fetch`), { method: "POST", body: "{}" })
    const bad = r.items.filter(i => i.status === "error")
    if (bad.length) toast(bad.map(i => `${i.name}: ${i.detail}`).join("\n"), "error")
    await renderComponents(wrap, repoId); refreshCommits()
  })
  const behind = res.items.filter(i => i.behind)
  const reqBtn = el("button", { class: "btn ghost", title: "Raise one approval request that moves every component that is behind to its upstream" }, "Request update")
  reqBtn.disabled = !behind.length
  // names: null for every component that is behind, or a list for just those
  const raise = (btn, names) => busy(btn, "Raising…", async () => {
    const r = await api(apiUrl(`/api/repo/${repoId}/components/request`), { method: "POST", body: JSON.stringify(names ? { names } : {}) })
    if (!r.ok) return toast(r.error, "info")
    toast("Update request raised. Review it on the Requests page.", "ok")
    route("/requests")
  })
  reqBtn.textContent = "Request update (all)"
  reqBtn.onclick = () => raise(reqBtn, null)
  const total = behind.reduce((n, i) => n + i.behind, 0)
  wrap.append(el("div", { style: "display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap" },
    el("h3", { style: "margin:0" }, `Components (${res.items.length})`),
    el("div", { class: "muted" }, behind.length ? `${behind.length} behind · ${total} upstream commits in total` : "all pins checked are current"),
    el("div", { style: "display:flex;gap:6px;flex-wrap:wrap" }, reqBtn, fetchAll)))
  for (const c of res.items) {
    const fetchBtn = el("button", { class: "btn ghost", "aria-label": `Fetch ${c.name}` }, "Fetch")
    fetchBtn.onclick = () => busy(fetchBtn, "Fetching…", async () => {
      const r = await api(apiUrl(`/api/repo/${repoId}/components/fetch`), { method: "POST", body: JSON.stringify({ name: c.name }) })
      const it = r.items[0]
      if (it?.status === "error") toast(`${c.name}: ${it.detail}`, "error")
      await renderComponents(wrap, repoId); refreshCommits()
    })
    // settings: tracked branch, watched paths and the follow-up command
    const branchIn = el("input", { value: c.branch && c.status !== "not_fetched" ? c.branch : "", placeholder: "default branch", "aria-label": `${c.name} branch`, style: "width:150px" })
    const watchIn = el("input", { value: (c.watch_paths || []).join(", "), placeholder: "watched paths, comma separated", "aria-label": `${c.name} watched paths`, style: "width:320px" })
    const hintIn = el("input", { value: c.after_update || "", placeholder: "command to run after bumping (shown, never run)", "aria-label": `${c.name} follow-up command`, style: "width:360px" })
    const enabledIn = el("input", { type: "checkbox", "aria-label": `Track ${c.name}` })
    enabledIn.checked = c.enabled !== false
    const saveBtn = el("button", { class: "btn ghost" }, "Save")
    saveBtn.onclick = () => busy(saveBtn, "Saving…", async () => {
      await api(apiUrl(`/api/repo/${repoId}/components/${encodeURIComponent(c.name)}`), { method: "POST", body: JSON.stringify({
        branch: branchIn.value.trim(), watch_paths: watchIn.value, after_update: hintIn.value.trim(), enabled: enabledIn.checked }) })
      toast(`${c.name} settings saved`, "ok")
      await renderComponents(wrap, repoId)
    })
    const status = c.status === "disabled" ? [badge("", "not tracked")]
      : ["not_checked_out", "not_fetched", "error"].includes(c.status) ? [badge(c.status === "error" ? "bad" : "warn", c.status.replace(/_/g, " "), c.detail)]
      : [c.behind ? badge("warn", `↓ ${c.behind} behind`, "upstream commits newer than the pin") : badge("up", "up to date"),
         c.local ? badge("bad", `${c.local} pinned commits not upstream`, "your own commits inside the submodule; bump needs a merge there first") : null,
         c.moved ? badge("warn", "checked out ≠ pin") : null,
         c.dirty ? badge("warn", `${c.dirty} uncommitted`) : null,
         (c.watched_changes || []).length ? badge("bad", `⚠ ${c.watched_changes.length} watched files changed`, c.watched_changes.join("\n")) : null]
    wrap.append(el("div", { class: "upstream" },
      el("div", { style: "display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap" },
        el("div", {}, el("strong", {}, `${c.name} `), el("span", { class: "muted" }, `${c.path} · ${c.branch || "?"} `), ...status),
        el("div", { style: "display:flex;gap:6px" },
          c.behind ? (() => { const b = el("button", { class: "btn ghost", "aria-label": `Pull report for ${c.name}` }, "Pull report"); b.onclick = () => busy(b, "Analysing…", () => showPullReport(repoId, { component: c.name })); return b })() : null,
          c.behind ? (() => { const b = el("button", { class: "btn ghost", "aria-label": `Request update for ${c.name}` }, "Request update"); b.onclick = () => raise(b, [c.name]); return b })() : null,
          fetchBtn)),
      el("div", { class: "muted" }, `${c.url}` + (c.pin ? ` · pinned ${c.pin.slice(0, 8)}` : "") + (c.upstream ? ` · upstream ${c.upstream.slice(0, 8)}` : "")),
      (c.watched_changes || []).length ? el("div", { style: "color:#e0a060;font-size:13px;margin-top:4px" },
        `Upstream changed files this repo depends on: ${c.watched_changes.slice(0, 8).join(", ")}${c.watched_changes.length > 8 ? " …" : ""}`,
        c.after_update ? el("div", {}, "After bumping, run: ", el("code", {}, c.after_update)) : null) : null,
      el("details", { style: "margin-top:6px" }, el("summary", { class: "muted" }, "Settings"),
        el("div", { style: "display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:6px" },
          el("label", { class: "muted" }, enabledIn, " track"), branchIn, watchIn, hintIn, saveBtn))
    ))
  }
}

// Chat: one conversation per session. "New chat" resets what the model sees of
// the conversation; repo facts, memory and tagged commits/files go with every message.
// Chat panel: one conversation of a repo (or of no repo: GENERAL). Used beside the
// repo tools and on the Chats page. A chat can be "about" commits, PRs or requests
// (its tags, sent to the model with every message); the tray holds commits/files
// attached to the next messages only.
const GENERAL = "_general"
function chatPanel(repoId, opts = {}) {
  const root = el("div", { class: "col", style: "gap:0;flex:1;min-height:0;height:100%" })
  let sessionId = opts.sessionId || null
  const load = async () => {
    root.replaceChildren(loading("Loading chat…"))
    try { await draw() } catch (e) { root.replaceChildren(el("p", { class: "muted", style: "padding:10px" }, `Could not load the chat: ${e.message}`)) }
  }
  const draw = async () => {
    const [sessions, hist] = await Promise.all([
      api(apiUrl(`/api/repo/${repoId}/sessions`)),
      api(apiUrl(`/api/chat/${repoId}` + (sessionId ? `?session=${sessionId}` : ""))),
    ])
    sessionId = hist.session
    opts.onSession?.(sessionId)
    const sessSel = el("select", { "aria-label": "Conversation" })
    for (const s of sessions) sessSel.append(el("option", { value: s.id }, `${s.title || "New chat"} · ${s.count}`))
    if (![...sessSel.options].some(o => o.value === sessionId)) sessSel.append(el("option", { value: sessionId }, "New chat · 0"))
    sessSel.value = sessionId
    sessSel.onchange = async () => {
      await api(apiUrl(`/api/repo/${repoId}/sessions`), { method: "POST", body: JSON.stringify({ session: sessSel.value }) })
      sessionId = sessSel.value; await load()
    }
    const newBtn = el("button", { class: "btn ghost icon", title: "New chat (memory is kept)", "aria-label": "New chat" }, "+")
    newBtn.onclick = () => busy(newBtn, null, async () => {
      const res = await api(apiUrl(`/api/repo/${repoId}/sessions`), { method: "POST", body: "{}" })
      sessionId = res.session; await load(); opts.onChange?.()
    })
    const head = el("div", { class: "chat-head" }, opts.title ? el("strong", {}, opts.title) : el("strong", {}, "Chat"), sessSel, newBtn)
    if (opts.onWide) {
      const wide = el("button", { class: "btn ghost icon chat-ctl", title: "Make the chat bigger / smaller", "aria-label": "Toggle wide chat" }, opts.isWide?.() ? "⤡" : "⤢")
      wide.onclick = () => { opts.onWide(); wide.textContent = opts.isWide?.() ? "⤡" : "⤢" }
      head.append(wide)
    }
    head.append(menu("Chat actions", [
      { label: "Open on the Chats page", run: () => route(`/chats?repo=${repoId}&session=${sessionId}`) },
      { label: "Delete this chat", danger: true, run: async () => {
        if (!confirm("Delete this chat? What the assistant remembers is kept.")) return
        const res = await api(apiUrl(`/api/repo/${repoId}/sessions/${sessionId}`), { method: "DELETE", body: "{}" })
        sessionId = res.active; await load(); opts.onChange?.() } },
    ]))

    // about (session tags)
    const tags = hist.tags || []
    const saveAbout = async (next) => {
      await api(apiUrl(`/api/repo/${repoId}/sessions/${sessionId}/tags`), { method: "POST", body: JSON.stringify({ tags: next }) })
      await load(); opts.onChange?.()
    }
    const about = el("div", { class: "about" }, el("span", { class: "muted" }, "About:"))
    const icon = { commit: "◆", pr: "⇄", request: "✓", push: "↑" }
    for (const t of tags) {
      const x = el("button", { class: "chip-x", "aria-label": `Remove ${t.label}`, onclick: () => saveAbout(tags.filter(y => y !== t)) }, "×")
      const label = el("span", {}, `${icon[t.type] || ""} ${t.label}`)
      const chip = el("span", { class: "chip about-chip", title: `${t.type}${t.repo ? ` in ${t.repo}` : ""}: ${t.id}` }, t.url ? el("a", { href: t.url, target: "_blank", rel: "noopener", style: "color:inherit" }, label) : label, x)
      about.append(chip)
    }
    about.append(el("button", { class: "btn link small", onclick: () => aboutDialog(repoId, tags, saveAbout) }, tags.length ? "+ add" : "+ a commit, PR or request"))

    const box = el("div", { class: "chat-box", role: "log", "aria-live": "polite", "aria-label": "Conversation", tabindex: "0" })
    const setReply = (d, text) => { d.innerHTML = renderMarkdown(text) }
    const addMsg = (m, index) => {
      const d = el("div", { class: `msg ${m.role === "user" ? "user" : "bot"}${m.role === "agent" ? " agent" : ""}${m.error ? " err" : ""}` })
      if (m.role === "user" || m.error) d.textContent = m.content
      else setReply(d, m.content)
      if (m.role === "agent") d.prepend(el("div", { class: "small", style: "color:var(--info);font-weight:600;margin-bottom:4px" }, "Coding agent"))
      if (m.edits?.length && repoId !== GENERAL) d.append(editCard(repoId, sessionId, index, m))
      if (m.attachments?.length) d.append(el("div", { class: "small", style: "margin-top:4px;opacity:.8" }, "📎 " + m.attachments.map(a => a.label).join(", ")))
      box.append(d)
      return d
    }
    ;(hist.messages || []).forEach((m, i) => addMsg(m, i))
    if (!(hist.messages || []).length) box.append(el("p", { class: "muted" }, repoId === GENERAL
      ? "A chat about no particular repo. The assistant sees an overview of every watched repo."
      : "New conversation. The assistant knows this repo, its upstreams and what it remembers about it."))

    const msgIn = el("textarea", { placeholder: "Message… (Enter to send, Shift+Enter for a new line)", "aria-label": "Message", rows: "2" })
    const sendBtn = el("button", { class: "btn" }, "Send")
    const tray = el("div", { class: "tray" })
    const trayKey = repoId
    refreshTray = () => {
      tray.innerHTML = ""
      const items = loadTags(trayKey)
      if (!items.length) { tray.append(el("span", { class: "muted small" }, repoId === GENERAL ? "" : "Tip: “Tag” a commit or file to attach it to your next messages.")); return }
      tray.append(el("span", { class: "muted small" }, `Attached (${items.length}/${MAX_TAGS}):`))
      for (const t of items) {
        const x = el("button", { class: "chip-x", "aria-label": `Remove ${t.label}`, onclick: () => { saveTags(trayKey, loadTags(trayKey).filter(y => !(y.type === t.type && y.id === t.id))); refreshTray() } }, "×")
        tray.append(el("span", { class: "chip" }, `${t.type === "commit" ? "◆" : "▤"} ${t.label}`, x))
      }
    }
    refreshTray()
    sendBtn.onclick = async () => {
      const text = msgIn.value.trim()
      if (!text || sendBtn.disabled) return
      const attachments = loadTags(trayKey)
      msgIn.value = ""; sendBtn.disabled = true; sendBtn.textContent = "Sending…"
      box.querySelector("p.muted")?.remove()
      addMsg({ role: "user", content: text, attachments })
      const replyBox = el("div", { class: "msg bot" }, loading("Thinking…"))
      box.append(replyBox); box.scrollTop = box.scrollHeight
      try {
        const res = await api(apiUrl(`/api/repo/${repoId}/chat`), { method: "POST", body: JSON.stringify({ message: text, session: sessionId, attachments }) })
        if (res.ok) { setReply(replyBox, res.reply); if (res.edits?.length) replyBox.append(editCard(repoId, sessionId, res.index, { edits: res.edits })) }
        else { replyBox.textContent = `Error: ${res.error}`; replyBox.classList.add("err") }
      } catch (e) { replyBox.textContent = `Error: ${e.message}`; replyBox.classList.add("err") }
      sendBtn.disabled = false; sendBtn.textContent = "Send"; box.scrollTop = box.scrollHeight
      opts.onChange?.()
    }
    msgIn.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendBtn.click() } })
    root.replaceChildren(head, about, box, el("div", { class: "composer" }, tray, msgIn, el("div", { class: "row" }, el("div", { class: "spacer" }), sendBtn)))
    box.scrollTop = box.scrollHeight
  }
  load()
  return root
}

// Pick what a chat is about: a commit, a pull request, or an approval request (merge/push).
async function aboutDialog(repoId, current, save) {
  const [reqs, repos] = await Promise.all([api(apiUrl("/api/requests")), api(apiUrl("/api/repos"))])
  const repoSel = el("select", { "aria-label": "Repository" }, ...repos.map(r => el("option", { value: r.id }, r.name || r.id)))
  if (repoId !== GENERAL) repoSel.value = repoId
  const commitSel = el("select", { "aria-label": "Commit", style: "flex:1;min-width:0" })
  const loadCommits = async () => {
    commitSel.replaceChildren(el("option", { value: "" }, "Loading…"))
    const res = await api(apiUrl(`/api/repo/${repoSel.value}/commits?view=all&limit=40`)).catch(() => ({ items: [] }))
    commitSel.replaceChildren(el("option", { value: "" }, "Pick a commit…"), ...(res.items || []).map(c => el("option", { value: c.full || c.sha, "data-label": `${c.sha} ${c.subject}`.slice(0, 80) }, `${c.sha} ${c.subject}`)))
  }
  repoSel.onchange = loadCommits
  if (repos.length) loadCommits()
  const shaIn = el("input", { placeholder: "or a commit id", "aria-label": "Commit id", style: "width:160px" })
  const reqSel = el("select", { "aria-label": "Request", style: "flex:1;min-width:0" }, el("option", { value: "" }, "Pick a request…"),
    ...reqs.filter(r => repoId === GENERAL || r.repo_id === repoId).slice().reverse().map(r => el("option", { value: r.id, "data-type": r.type === "push" ? "push" : "request" },
      `${r.repo_name || r.repo_id}: ${requestLine(r).slice(0, 70)} (${r.status})`)))
  const prUrl = el("input", { placeholder: "https://github.com/owner/repo/pull/123", "aria-label": "Pull request URL", style: "flex:2;min-width:0" })
  const prTitle = el("input", { placeholder: "title (optional)", "aria-label": "Pull request title", style: "flex:1;min-width:0" })
  const add = el("button", { class: "btn" }, "Add")
  let close
  add.onclick = async () => {
    const next = [...current]
    const repo = repoId === GENERAL || repoSel.value !== repoId ? repoSel.value : undefined
    const sha = shaIn.value.trim() || commitSel.value
    if (sha) next.push({ type: "commit", id: sha, label: shaIn.value.trim() ? sha.slice(0, 10) : commitSel.selectedOptions[0].dataset.label, repo })
    if (reqSel.value) next.push({ type: reqSel.selectedOptions[0].dataset.type, id: reqSel.value, label: reqSel.selectedOptions[0].textContent.slice(0, 60) })
    const url = prUrl.value.trim()
    if (url) {
      const m = url.match(/github\.com\/([^/]+\/[^/]+)\/pull\/(\d+)/)
      next.push({ type: "pr", id: m ? `${m[1]}#${m[2]}` : url, label: prTitle.value.trim() || (m ? `#${m[2]} ${m[1]}` : url), url })
    }
    if (next.length === current.length) return toast("Pick a commit, a request or a PR", "error")
    await busy(add, "Saving…", async () => { await save(next); close() })
  }
  close = openDialog("What is this chat about?",
    el("p", { class: "muted" }, "These are sent to the model with every message of this chat: a commit's message and patch, a request's details and outcome, a pull request's link."),
    el("h4", {}, "A commit"), el("div", { class: "row" }, repos.length > 1 || repoId === GENERAL ? repoSel : null, commitSel, shaIn),
    el("h4", {}, "An approval request (merge or push)"), el("div", { class: "row" }, reqSel),
    el("h4", {}, "A pull request"), el("div", { class: "row" }, prUrl, prTitle),
    el("div", { class: "row", style: "margin-top:14px" }, add))
}

// Edits the model proposed in a reply: each shows its diff (or why it doesn't
// match the file). Nothing is written until Apply; Undo restores the old text.
function editCard(repoId, session, index, m) {
  const box = el("div", { class: "edits" })
  const render = (state) => {
    box.innerHTML = ""
    const results = state.edit_results || m.edits
    const okCount = m.edits.filter(e => e.ok).length
    box.append(el("div", { style: "display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap" },
      el("strong", {}, `Proposed edits (${okCount}/${m.edits.length} match the files)`),
      state.applied ? el("span", { class: "badge up" }, "applied") : null))
    results.forEach((e, i) => {
      const diff = el("pre", { class: "code-pre", style: "max-height:240px" })
      for (const line of (e.diff || "").split("\n")) {
        if (!line) continue
        const cls = line.startsWith("+") && !line.startsWith("+++") ? "add" : line.startsWith("-") && !line.startsWith("---") ? "del" : line.startsWith("@@") ? "hunk" : ""
        diff.append(el("div", { class: `dl ${cls}` }, line))
      }
      box.append(el("div", { class: "upstream" },
        el("div", {}, el("code", {}, e.path), " ", e.ok ? badge("up", e.new_file ? "new file" : "matches") : badge("bad", "won't apply")),
        e.error ? el("div", { class: "muted", style: "color:#e08080" }, e.error) : null,
        e.diff ? diff : null))
    })
    const act = el("div", { style: "display:flex;gap:6px;margin-top:6px" })
    if (!state.applied && okCount) {
      const b = el("button", { class: "btn" }, okCount === m.edits.length ? "Apply" : `Apply the ${okCount} that match`)
      b.onclick = () => busy(b, "Applying…", async () => {
        const r = await api(apiUrl(`/api/repo/${repoId}/edits/apply`), { method: "POST", body: JSON.stringify({ session, index, only: m.edits.map((e, i) => e.ok ? i : -1).filter(i => i >= 0) }) })
        toast(r.ok ? "Edits applied to the working tree (not committed)" : "Some edits did not apply", r.ok ? "ok" : "error")
        render({ applied: r.applied, edit_results: r.results })
      })
      act.append(b)
    }
    if (state.applied) {
      const u = el("button", { class: "btn ghost" }, "Undo")
      u.onclick = () => busy(u, "Undoing…", async () => {
        const r = await api(apiUrl(`/api/repo/${repoId}/edits/undo`), { method: "POST", body: JSON.stringify({ session, index }) })
        toast(`Restored ${r.restored.join(", ")}`, "ok")
        render({})
      })
      act.append(u)
    }
    box.append(act)
  }
  render({ applied: m.applied, edit_results: m.edit_results })
  return box
}

// Publish: push the branch (optionally opening a PR), build a GitHub Actions
// workflow that publishes Docker images, and copy the Docker Hub login into the
// repo's Actions secrets. Pushes go through an approval request like merges.
async function renderPublish(wrap, repoId) {
  const info = await api(apiUrl(`/api/repo/${repoId}/publish`))
  wrap.innerHTML = ""
  const acc = info.accounts
  const accountLine = el("div", { class: "muted" },
    `GitHub token: ${acc.github_token.set ? `set (${acc.github_token.source})` : "not set: pushes use this machine's git login"} · `,
    `Docker Hub: ${acc.dockerhub_username.set && acc.dockerhub_token.set ? acc.dockerhub_username.hint : "not set"} · `,
    el("a", { href: `${BASE}/settings`, style: "color:#9fb4ff" }, "Accounts"))

  // --- push
  const remSel = el("select", { "aria-label": "Remote" }, ...info.remotes.map(r => el("option", { value: r.name }, `${r.name}${r.owner ? ` (${r.owner}/${r.repo})` : ""}`)))
  const ahead = () => { const r = info.remotes.find(x => x.name === remSel.value); return r?.ahead == null ? "new branch on the remote" : `${r.ahead} commit(s) to push` }
  const aheadEl = el("span", { class: "muted" }, ahead())
  remSel.onchange = () => { aheadEl.textContent = ahead() }
  const prIn = el("input", { type: "checkbox", "aria-label": "Open a pull request" })
  const baseIn = el("input", { value: "main", "aria-label": "Pull request base branch", style: "width:110px" })
  const pushBtn = el("button", { class: "btn" }, "Request push")
  pushBtn.onclick = () => busy(pushBtn, "Raising…", async () => {
    await api(apiUrl(`/api/repo/${repoId}/push/request`), { method: "POST", body: JSON.stringify({ remote: remSel.value, open_pr: prIn.checked, base: baseIn.value.trim() }) })
    toast("Push request raised. Approve it on the Requests page.", "ok"); route("/requests")
  })
  const pushCard = el("div", { class: "upstream" },
    el("strong", {}, `Push ${info.branch}`), " ", aheadEl,
    el("div", { style: "display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:6px" },
      el("label", { class: "muted" }, "to ", remSel),
      el("label", { class: "muted" }, prIn, " open a pull request into ", baseIn), pushBtn),
    el("div", { class: "muted", style: "font-size:12px;margin-top:4px" }, "Never force-pushes. A remote that has moved on is refused: fetch and merge first."))

  // --- docker workflow builder
  const rows = info.dockerfiles.map(d => {
    const use = el("input", { type: "checkbox", "aria-label": `Build ${d.dockerfile}` }); use.checked = true
    const name = el("input", { value: d.image, "aria-label": `Image name for ${d.dockerfile}`, style: "width:220px" })
    return { d, use, name, row: el("div", { style: "display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:4px 0" },
      use, el("code", {}, d.dockerfile), el("span", { class: "muted" }, "→ image"), name) }
  })
  const chk = (label, on) => { const i = el("input", { type: "checkbox" }); i.checked = on; return [i, el("label", { class: "muted" }, i, " " + label)] }
  const [ghcr, ghcrL] = chk("GHCR (ghcr.io)", !!info.owner)
  const [hub, hubL] = chk("Docker Hub", acc.dockerhub_username.set)
  const [arm, armL] = chk("also arm64", false)
  const [onBranch, onBranchL] = chk("push to main", true)
  const [onTags, onTagsL] = chk("v* tags", true)
  const [onRel, onRelL] = chk("releases", false)
  const [onPr, onPrL] = chk("build PRs (no push)", true)
  const fileIn = el("input", { value: "docker-images.yml", "aria-label": "Workflow file name", style: "width:180px" })
  const removes = info.workflows.filter(w => w.file !== `.github/workflows/${fileIn.value}`).map(w => {
    const i = el("input", { type: "checkbox", "aria-label": `Remove ${w.file}` })
    return { w, i, row: el("label", { class: "muted", style: "display:block" }, i, ` remove ${w.file} — "${w.name}" (on: ${w.triggers.join(", ") || "?"})`) }
  })
  const spec = () => ({
    images: rows.filter(r => r.use.checked).map(r => ({ image: r.name.value.trim(), context: r.d.context, dockerfile: r.d.dockerfile })),
    owner: info.owner, ghcr: ghcr.checked, dockerhub: hub.checked, arm64: arm.checked, branch: "main",
    on_branch: onBranch.checked, on_tags: onTags.checked, on_release: onRel.checked, on_pr: onPr.checked, filename: fileIn.value.trim(),
  })
  const preview = el("pre", { class: "code-pre", style: "display:none;max-height:360px" })
  const errs = el("div", { style: "color:#e08080;font-size:13px" })
  const show = async () => {
    const r = await api(apiUrl(`/api/repo/${repoId}/docker/workflow`), { method: "POST", body: JSON.stringify({ spec: spec() }) })
    errs.textContent = r.ok ? "" : r.errors.join("; ")
    preview.style.display = r.ok ? "" : "none"
    preview.textContent = r.ok ? r.yaml : ""
    return r.ok
  }
  const prevBtn = el("button", { class: "btn ghost" }, "Preview")
  prevBtn.onclick = () => busy(prevBtn, "Rendering…", show)
  const commitBtn = el("button", { class: "btn" }, "Commit workflow")
  commitBtn.onclick = () => busy(commitBtn, "Committing…", async () => {
    if (!(await show())) return
    const remove = removes.filter(x => x.i.checked).map(x => x.w.file)
    if (!confirm(`Commit .github/workflows/${fileIn.value} to ${info.branch}` + (remove.length ? ` and remove ${remove.length} workflow(s)` : "") + "? Nothing is pushed.")) return
    const r = await api(apiUrl(`/api/repo/${repoId}/docker/workflow`), { method: "POST", body: JSON.stringify({ spec: spec(), remove, commit: true }) })
    toast(r.ok ? `Committed ${r.commit}: ${r.detail}` : r.detail, r.ok ? "ok" : "error")
    if (r.ok) await renderPublish(wrap, repoId)
  })
  const dockerCard = el("div", { class: "upstream" },
    el("strong", {}, "Docker images on GitHub Actions"),
    info.dockerfiles.length ? el("div", {}, ...rows.map(r => r.row)) : el("p", { class: "muted" }, "No Dockerfile found in this repo's own code."),
    el("div", { style: "display:flex;gap:12px;flex-wrap:wrap;margin-top:6px" }, el("span", { class: "muted" }, "Publish to:"), ghcrL, hubL, armL),
    el("div", { style: "display:flex;gap:12px;flex-wrap:wrap;margin-top:4px" }, el("span", { class: "muted" }, "Run on:"), onBranchL, onTagsL, onRelL, onPrL),
    el("div", { style: "display:flex;gap:8px;align-items:center;margin-top:6px" }, el("span", { class: "muted" }, "File .github/workflows/"), fileIn),
    removes.length ? el("details", { style: "margin-top:6px" }, el("summary", { class: "muted" }, `Existing workflows (${removes.length})`), ...removes.map(x => x.row)) : null,
    errs,
    el("div", { style: "display:flex;gap:6px;margin-top:6px" }, prevBtn, commitBtn),
    preview)

  // --- actions secrets
  const secBtn = el("button", { class: "btn ghost" }, "Copy Docker Hub login to GitHub secrets")
  secBtn.disabled = !(acc.github_token.set && acc.dockerhub_username.set && acc.dockerhub_token.set && info.owner)
  secBtn.onclick = () => busy(secBtn, "Saving secrets…", async () => {
    const r = await api(apiUrl(`/api/repo/${repoId}/github/secrets`), { method: "POST", body: "{}" })
    toast(r.ok ? `Saved DOCKERHUB_USERNAME and DOCKERHUB_TOKEN in ${r.repo}` : r.error, r.ok ? "ok" : "error")
  })
  const secCard = el("div", { class: "upstream" },
    el("strong", {}, "GitHub Actions secrets"),
    el("div", { class: "muted" }, info.owner ? `Sets DOCKERHUB_USERNAME and DOCKERHUB_TOKEN on ${info.owner}/${info.github_repo} (encrypted with the repo's key). GHCR needs no secret.` : "This repo has no github.com remote."),
    el("div", { style: "margin-top:6px" }, secBtn))

  wrap.append(accountLine, pushCard, dockerCard, secCard)
}

// Actions: the repo's GitHub workflows with recent failures and triggers. In a
// fork it suggests disabling upstream's release/deploy/scheduled jobs (disabling
// is a GitHub setting, so the files stay identical to upstream). Changes go
// through an approval request. Flags workflows the next upstream pull would add
// and ones added since you last reviewed the list.
async function renderActions(wrap, repoId, refresh = false) {
  const info = await api(apiUrl(`/api/repo/${repoId}/actions${refresh ? "?refresh=1" : ""}`))
  wrap.innerHTML = ""
  if (!info.ok) { wrap.append(el("p", { class: "muted" }, info.error)); return }
  const s = info.summary
  const picks = new Map()  // workflow id -> checkbox
  const head = el("div", { class: "muted" },
    `${info.owner}/${info.repo}` + (info.fork ? ` · fork of ${info.parent}` : "") +
    ` · ${s.active} of ${s.total} active · ${s.failed_runs} failed runs in the last 100 · ${s.suggested} suggested to disable` +
    (info.token ? "" : " · no GitHub token: you can review, but changing workflows needs one (Settings → Accounts)"))
  const row = (w) => {
    const box = el("input", { type: "checkbox", "aria-label": `Select ${w.name}` })
    picks.set(w.id, box)
    const st = w.stats
    return el("div", { class: "mem", style: "align-items:center" }, box,
      el("div", { style: "flex:1;min-width:0" },
        el("div", {}, el("strong", {}, w.name), " ",
          w.state === "active" ? null : badge("", w.state.replace(/_/g, " ")),
          w.new_since_review ? badge("warn", "new since review") : null,
          st.runs ? badge(st.failed ? "bad" : "up", `${st.failed}/${st.runs} failed`) : badge("", "no recent runs")),
        el("div", { class: "muted", style: "font-size:12px" }, `${w.path} · on: ${(w.triggers || []).join(", ") || "?"}` +
          (w.secrets?.length ? ` · secrets: ${w.secrets.slice(0, 5).join(", ")}${w.secrets.length > 5 ? " …" : ""}` : "")),
        w.reasons.length ? el("div", { style: "color:#e0a060;font-size:12px" }, "Suggest disabling: " + w.reasons.join("; ")) : null))
  }
  const list = el("div", { class: "mem-list", style: "max-height:520px" }, ...info.workflows.map(row))
  const select = (pred) => { for (const w of info.workflows) picks.get(w.id).checked = pred(w) }
  const sugBtn = el("button", { class: "btn ghost" }, `Select suggested (${s.suggested})`)
  sugBtn.onclick = () => select(w => w.reasons.length > 0)
  const noneBtn = el("button", { class: "btn ghost" }, "Clear")
  noneBtn.onclick = () => select(() => false)
  const raise = (kind, btn) => busy(btn, "Raising…", async () => {
    const ids = info.workflows.filter(w => picks.get(w.id).checked).map(w => w.id)
    if (!ids.length) return toast("Select workflows first", "info")
    const r = await api(apiUrl(`/api/repo/${repoId}/actions/request`), { method: "POST", body: JSON.stringify({ [kind]: ids }) })
    if (!r.ok) return toast(r.error, "info")
    toast(`Request raised to ${kind} ${(r.request[kind] || []).length} workflow(s). Approve it on the Requests page.`, "ok")
    route("/requests")
  })
  const disBtn = el("button", { class: "btn" }, "Request: disable selected")
  disBtn.onclick = () => raise("disable", disBtn)
  const enBtn = el("button", { class: "btn ghost" }, "Request: enable selected")
  enBtn.onclick = () => raise("enable", enBtn)
  const refBtn = el("button", { class: "btn ghost" }, "Refresh")
  refBtn.onclick = () => busy(refBtn, "Loading…", () => renderActions(wrap, repoId, true))
  const newOnes = info.workflows.filter(w => w.new_since_review)
  const reviewBtn = el("button", { class: "btn ghost" }, `Mark ${newOnes.length} new as reviewed`)
  reviewBtn.onclick = () => busy(reviewBtn, "Saving…", async () => {
    await api(apiUrl(`/api/repo/${repoId}/actions/reviewed`), { method: "POST", body: "{}" }); await renderActions(wrap, repoId)
  })
  wrap.append(head,
    info.incoming.length ? el("div", { class: "upstream", style: "border-color:#5a4a23" },
      el("strong", {}, `${info.incoming.length} workflow(s) arrive with the next pull from upstream`),
      el("div", { class: "muted", style: "font-size:12px" }, "They start active after the pull. Review them here once they land."),
      ...info.incoming.map(w => el("div", { style: "font-size:12px" }, el("code", {}, w.path), ` from ${w.upstream} · on: ${w.triggers.join(", ")}` + (w.schedule ? " · scheduled" : "")))) : null,
    newOnes.length ? el("div", { class: "upstream", style: "border-color:#5a4a23" },
      el("strong", {}, `${newOnes.length} workflow(s) added since you last reviewed`), " ", reviewBtn) : null,
    el("div", { style: "display:flex;gap:6px;flex-wrap:wrap;margin-top:8px" }, sugBtn, noneBtn, disBtn, enBtn, refBtn),
    list,
    el("p", { class: "muted", style: "font-size:12px" }, "Disabling is a GitHub setting: the workflow files stay as they are, so pulling from upstream never conflicts. Re-enable any time."))
}

// Right-hand tools: commits, files, memory, publish, actions.
function renderTools(wrap, repoId, { extra } = {}) {
  const names = ["Chat", "Commits", "Files", "Merges", "Stack", "Memory", "Publish", "Actions"]
  const bar = el("div", { class: "tabs", role: "tablist", "aria-label": "Repo tools" })
  const panes = {}, tabs = {}, built = {}
  let filesScope = null  // a merge workspace name, or null for the repo itself
  const builders = {
    Chat: async () => {}, Commits: renderCommits, Merges: renderMerges, Stack: (pane, id) => renderStack(pane, id), Memory: renderMemory, Publish: renderPublish, Actions: renderActions,
    Files: (pane, id) => renderFiles(pane, id, { ws: filesScope, onScope: (ws) => { filesScope = ws } }),
  }
  const flush = new Set(["Files", "Chat"])
  const show = async (n, focus = false) => {
    for (const k of names) {
      const on = k === n
      panes[k].hidden = !on
      tabs[k].classList.toggle("active", on)
      tabs[k].setAttribute("aria-selected", String(on))
      tabs[k].tabIndex = on ? 0 : -1
    }
    if (focus) tabs[n].focus()
    store_.set(`tab:${repoId}`, n)
    if (!built[n]) {
      built[n] = true
      if (n !== "Chat") panes[n].replaceChildren(loading())
      try { await builders[n](panes[n], repoId) }
      catch (e) { built[n] = false; panes[n].replaceChildren(el("p", { class: "muted" }, `Could not load ${n.toLowerCase()}: ${e.message}`)) }
    } else if (n === "Merges") renderMerges(panes[n], repoId).catch(() => {})
  }
  for (const n of names) {
    const id = `tab-${repoId}-${n}`
    panes[n] = el("div", { role: "tabpanel", id: `${id}-panel`, "aria-labelledby": id, hidden: "", class: `tabpanel${flush.has(n) ? " flush" : ""}` })
    tabs[n] = el("button", { class: `tab${n === "Chat" ? " chat-tab" : ""}`, role: "tab", id, "aria-controls": `${id}-panel`, onclick: () => show(n) }, n)
    tabs[n].addEventListener("keydown", (e) => {
      const i = names.indexOf(n)
      const next = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: names.length - 1 }[e.key]
      if (next === undefined) return
      e.preventDefault()
      show(names[(next + names.length) % names.length], true)
    })
    bar.append(tabs[n])
  }
  if (extra) bar.append(el("div", { class: "spacer" }), extra)
  wrap.append(bar, ...names.map(n => panes[n]))
  // Workspaces in progress show as a count on the Merges tab.
  api(apiUrl(`/api/repo/${repoId}/workspaces`)).then(r => {
    const open = (r.items || []).filter(w => !w.ready).length
    if (open) tabs.Merges.append(el("span", { class: "count" }, String(open)))
  }).catch(() => {})
  const narrow = matchMedia("(max-width: 900px)").matches
  let first = store_.get(`tab:${repoId}`, narrow ? "Chat" : "Commits")
  if (first === "Chat" && !narrow) first = "Commits"
  show(names.includes(first) ? first : "Commits")
  return {
    show,
    chatPanel: panes.Chat,
    openFiles: (ws) => { filesScope = ws; built.Files = false; show("Files") },
  }
}

// Commits: what the upstream has that you lack, what only you have, or everything.
async function renderCommits(wrap, repoId) {
  const repo = await api(apiUrl(`/api/repo/${repoId}`))
  wrap.innerHTML = ""
  const viewSel = el("select", { "aria-label": "Which commits" },
    el("option", { value: "behind" }, "Missing from upstream (behind)"),
    el("option", { value: "ahead" }, "Only on my branch (ahead)"),
    el("option", { value: "all" }, "All commits"))
  // Source: one of the repo's upstreams, or a component (its submodule history).
  const upSel = el("select", { "aria-label": "Source" },
    ...(repo.upstreams || []).map(u => el("option", { value: `up:${u.name}` }, u.name)),
    ...(repo.components || []).map(c => el("option", { value: `comp:${c.name}` }, `component: ${c.name}`)))
  // open on the upstream that has the most commits you are missing
  const cmp = await api(apiUrl(`/api/repo/${repoId}/compare`)).catch(() => ({}))
  const comps = await api(apiUrl(`/api/repo/${repoId}/components`)).catch(() => ({ items: [] }))
  const worst = [...(cmp.plan?.upstreams || []).map(u => ({ key: `up:${u.name}`, behind: u.behind })),
                 ...comps.items.map(c => ({ key: `comp:${c.name}`, behind: c.behind || 0 }))].sort((a, b) => b.behind - a.behind)[0]
  if (worst && worst.behind) upSel.value = worst.key
  const qIn = el("input", { placeholder: "Search commit messages…", "aria-label": "Search commit messages", style: "flex:1;min-width:120px" })
  const list = el("div", { class: "mem-list", style: "max-height:520px" })
  const more = el("button", { class: "btn ghost", style: "margin-top:6px;display:none" }, "Load more")
  const info = el("div", { class: "muted", style: "margin-top:6px" })
  let skip = 0
  const load = async (reset) => {
    if (reset) { skip = 0; list.replaceChildren(loading()) }
    const [kind, name] = upSel.value.split(/:(.*)/s)
    const src = kind === "comp" ? `component=${encodeURIComponent(name)}` : `upstream=${encodeURIComponent(name || "")}`
    const q = `view=${viewSel.value}&${src}&skip=${skip}&limit=50&q=${encodeURIComponent(qIn.value.trim())}`
    let res
    try { res = await api(apiUrl(`/api/repo/${repoId}/commits?${q}`)) }
    catch (e) { res = { ok: false, error: e.message } }
    list.querySelector(".loading")?.remove()
    if (!res.ok) { info.textContent = res.error; more.style.display = "none"; return }
    info.textContent = ""
    for (const c of res.items) {
      const tag = el("button", { class: "btn ghost", title: "Attach to the chat", "aria-label": `Tag commit ${c.sha} for the chat` }, "Tag")
      const comp = upSel.value.startsWith("comp:") ? upSel.value.slice(5) : null
      tag.onclick = () => comp ? toast("Tagging component commits isn't supported yet; tag the files instead.", "info")
        : addTag(repoId, { type: "commit", id: c.full, label: `${c.sha} ${c.subject.slice(0, 40)}` })
      const view = el("button", { class: "btn ghost", "aria-label": `View commit ${c.sha}` }, "View")
      view.onclick = () => busy(view, null, () => showCommit(repoId, c.full, comp))
      list.append(el("div", { class: "mem" },
        el("code", { style: "color:#9fb4ff" }, c.sha),
        el("div", { style: "flex:1;min-width:0" }, el("div", {}, c.subject), el("div", { class: "muted", style: "font-size:12px" }, `${c.author} · ${c.date.slice(0, 10)}`)),
        view, tag))
    }
    skip += res.items.length
    if (!list.children.length) info.textContent = viewSel.value === "behind" ? "Nothing missing: you have every upstream commit." : "No commits."
    more.style.display = res.more ? "" : "none"
  }
  refreshCommits = () => load(true)
  more.onclick = () => busy(more, "Loading…", () => load(false))
  viewSel.onchange = () => load(true)
  upSel.onchange = () => load(true)
  qIn.addEventListener("keydown", (e) => { if (e.key === "Enter") load(true) })
  wrap.append(el("div", { style: "display:flex;gap:6px;flex-wrap:wrap" }, viewSel, upSel.options.length > 1 ? upSel : "", qIn), info, list, more)
  if (!upSel.options.length) info.textContent = "No upstream or component to compare against."
  await load(true)
}

async function showCommit(repoId, sha, component = null) {
  const qs = component ? `?component=${encodeURIComponent(component)}` : ""
  const d = await api(apiUrl(`/api/repo/${repoId}/commit/${sha}${qs}`)).catch(e => ({ ok: false, error: e.message }))
  const opener = document.activeElement
  const close = () => { ov.remove(); document.removeEventListener("keydown", onKey); opener?.focus?.() }
  // Escape closes; Tab stays inside the dialog (it is modal).
  const onKey = (e) => {
    if (e.key === "Escape") return close()
    if (e.key !== "Tab") return
    const f = [...ov.querySelectorAll("button, [href], input, [tabindex]:not([tabindex='-1'])")].filter(x => !x.disabled)
    if (!f.length) return
    const first = f[0], last = f[f.length - 1]
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus() }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus() }
  }
  const patch = el("pre", { class: "code-pre", tabindex: "0", "aria-label": "Patch" })
  for (const line of (d.patch || "").split("\n")) {
    const cls = line.startsWith("+") && !line.startsWith("+++") ? "add" : line.startsWith("-") && !line.startsWith("---") ? "del" : line.startsWith("@@") ? "hunk" : line.startsWith("diff ") ? "file" : ""
    patch.append(el("div", { class: `dl ${cls}` }, line || " "))
  }
  const tag = el("button", { class: "btn" }, "Tag for chat")
  tag.onclick = () => { addTag(repoId, { type: "commit", id: d.sha, label: `${d.sha.slice(0, 8)} ${(d.message || "").split("\n")[0].slice(0, 40)}` }) }
  if (component) tag.hidden = true
  const closeBtn = el("button", { class: "btn ghost", onclick: close }, "Close")
  const titleId = `commit-title-${sha.slice(0, 10)}`
  const ov = el("div", { class: "overlay", onclick: (e) => { if (e.target === ov) close() } },
    el("div", { class: "overlay-panel", role: "dialog", "aria-modal": "true", "aria-labelledby": titleId },
      el("div", { style: "display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap" },
        el("h3", { style: "margin:0", id: titleId }, d.ok ? `Commit ${d.sha.slice(0, 10)}` : "Commit"), el("div", { style: "display:flex;gap:6px" }, d.ok ? tag : null, closeBtn)),
      d.ok ? el("div", {},
        el("div", { class: "muted" }, `${d.author} · ${(d.date || "").slice(0, 10)}`),
        el("pre", { style: "white-space:pre-wrap;margin:8px 0" }, d.message),
        el("pre", { class: "muted", style: "white-space:pre-wrap;font-size:12px" }, d.stat),
        patch, d.truncated ? el("p", { class: "muted" }, "Patch truncated.") : null) : el("p", {}, d.error || "Not found")))
  document.addEventListener("keydown", onKey)
  document.body.append(ov)
  closeBtn.focus()
}

// A modal dialog: Escape closes, Tab stays inside, focus returns to the opener.
function openDialog(title, ...body) {
  const opener = document.activeElement
  const close = () => { ov.remove(); document.removeEventListener("keydown", onKey); opener?.focus?.() }
  const onKey = (e) => {
    if (e.key === "Escape") return close()
    if (e.key !== "Tab") return
    const f = [...ov.querySelectorAll("button, [href], input, [tabindex]:not([tabindex='-1'])")].filter(x => !x.disabled)
    if (!f.length) return
    const first = f[0], last = f[f.length - 1]
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus() }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus() }
  }
  const closeBtn = el("button", { class: "btn ghost", onclick: () => close() }, "Close")
  const id = `dlg-${Math.random().toString(36).slice(2, 8)}`
  const ov = el("div", { class: "overlay", onclick: (e) => { if (e.target === ov) close() } },
    el("div", { class: "overlay-panel", role: "dialog", "aria-modal": "true", "aria-labelledby": id },
      el("div", { style: "display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap" }, el("h3", { style: "margin:0", id }, title), closeBtn),
      ...body))
  document.addEventListener("keydown", onKey)
  document.body.append(ov)
  closeBtn.focus()
  return close
}

// Pull report: of the incoming commits, how many are a no-issue pull and how many
// touch files that would conflict; plus the clean part that can be merged now.
async function showPullReport(repoId, { upstream, component }) {
  const qs = component ? `component=${encodeURIComponent(component)}` : `upstream=${encodeURIComponent(upstream)}`
  const r = await api(apiUrl(`/api/repo/${repoId}/pull-report?${qs}`))
  if (!r.ok) return toast(r.error, "error")
  const stat = (n, label, cls = "") => el("div", { class: `stat ${cls}` }, el("div", { class: "stat-n" }, String(n)), el("div", { class: "muted" }, label))
  const cp = r.clean_prefix || {}
  const list = (title, items) => items.length ? el("details", { style: "margin-top:10px" }, el("summary", {}, title),
    el("div", { class: "mem-list" }, ...items.map(c => el("div", { class: "mem" }, el("code", { style: "color:#9fb4ff" }, c.sha),
      el("div", { style: "flex:1;min-width:0" }, el("div", {}, c.subject), el("div", { class: "muted", style: "font-size:12px" }, c.files.join(", "))))))) : null
  const actions = el("div", { style: "display:flex;gap:8px;flex-wrap:wrap;margin-top:12px" })
  let close
  if (component) {
    const b = el("button", { class: "btn" }, "Request update for this component")
    b.onclick = () => busy(b, "Raising…", async () => {
      const x = await api(apiUrl(`/api/repo/${repoId}/components/request`), { method: "POST", body: JSON.stringify({ names: [component] }) })
      if (!x.ok) return toast(x.error, "info")
      close(); toast("Update request raised.", "ok"); route("/requests")
    })
    if (r.incoming) actions.append(b)
  } else if (cp.sha && cp.commits) {
    const b = el("button", { class: "btn" }, cp.complete ? `Request pull of all ${cp.commits} commits` : `Request pull of the clean part (${cp.commits} commits)`)
    b.onclick = () => busy(b, "Raising…", async () => {
      await api(apiUrl(`/api/repo/${repoId}/upstream/${encodeURIComponent(upstream)}/request-pull`), { method: "POST", body: JSON.stringify({ target: cp.sha }) })
      close(); toast("Pull request raised. Approve it on the Requests page.", "ok"); route("/requests")
    })
    actions.append(b)
  }
  close = openDialog(`Pull report · ${r.source} → ${r.into}`,
    el("div", { class: "stats" },
      stat(r.incoming, "incoming commits"),
      stat(r.no_issue, "no-issue changes", "good"),
      stat(r.conflicting, "touch conflicting files", r.conflicting ? "bad" : ""),
      r.watched ? stat(r.watched, "touch watched files", "warn") : null,
      stat(r.merge_commits, "merge commits")),
    el("p", { class: "muted" }, r.fast_forward
      ? "There are no commits of yours on this side, so every incoming commit comes in as a fast-forward: nothing can conflict."
      : `You have ${r.yours} commit(s) of your own here. ${r.conflict_files.length} file(s) would conflict in a full merge.`),
    !r.fast_forward ? el("p", {}, cp.complete ? "Everything merges cleanly right now."
      : cp.commits ? `The first ${cp.commits} of ${r.incoming} commits merge with no conflict (up to ${cp.short}); the first conflict comes at ${cp.first_conflict}.`
      : `Even the first incoming commit conflicts (${cp.first_conflict}).`) : null,
    r.conflict_files.length ? el("details", {}, el("summary", {}, `Conflicting files (${r.conflict_files.length})`),
      el("div", { class: "muted", style: "font-family:monospace;font-size:12px" }, ...r.conflict_files.map(f => el("div", {}, f)))) : null,
    list(`Commits that touch conflicting files (first ${r.conflicting_sample.length} of ${r.conflicting})`, r.conflicting_sample),
    list(`Commits that touch watched files (first ${r.watched_sample.length} of ${r.watched})`, r.watched_sample),
    el("p", { class: "muted", style: "font-size:12px" }, "No-issue commits are not pulled one by one past a conflict: they often build on the conflicting ones. The clean part is the safe unit."),
    actions)
}

// Files: a read-only tree plus a line-numbered viewer with find.
// Stack: run the repo's (or a merge workspace's) compose stack as an isolated demo,
// see what runs, read its logs and try requests against it. See stacks.py.
async function renderStack(wrap, repoId, scope = { ws: null }) {
  const q = () => (scope.ws ? `?ws=${encodeURIComponent(scope.ws)}` : "")
  let st
  try { st = await api(apiUrl(`/api/repo/${repoId}/stack${q()}`)) }
  catch (e) { wrap.replaceChildren(el("p", { class: "muted" }, e.message)); return }
  wrap.innerHTML = ""
  const again = () => renderStack(wrap, repoId, scope)
  const body = (x) => JSON.stringify({ ...(scope.ws ? { ws: scope.ws } : {}), ...x })

  // header: docker, on/off, scope
  const head = el("div", { class: "row", style: "margin-bottom:10px" }, el("h3", {}, "Demo stack"),
    st.docker.ok ? badge("up", `Docker ${st.docker.detail}`) : badge("bad", "Docker not running"),
    st.compose_file ? badge("", st.compose_file) : badge("warn", "no compose file"),
    el("div", { class: "spacer" }))
  const wsList = await api(apiUrl(`/api/repo/${repoId}/workspaces`)).then(r => r.items.filter(w => !w.error)).catch(() => [])
  if (wsList.length || scope.ws) {
    const sel = el("select", { "aria-label": "Run" }, el("option", { value: "" }, "This repo"),
      ...wsList.map(w => el("option", { value: w.name }, `Workspace: ${w.branch.replace("upkeep/", "")}`)))
    sel.value = scope.ws || ""
    sel.onchange = () => { scope.ws = sel.value || null; again() }
    head.append(el("label", { class: "row muted", style: "gap:6px" }, "Run", sel))
  }
  const toggle = el("button", { class: `btn ${st.enabled ? "ghost" : ""} small` }, st.enabled ? "Turn off" : "Turn on demo stacks")
  toggle.onclick = () => {
    if (!st.enabled && !confirm("A demo stack builds and runs this project's own code in Docker on this machine (isolated: its own network, random local ports, demo data). Agents you connect can then start it, read its logs and send it requests. Turn it on?")) return
    return busy(toggle, null, async () => { await api(apiUrl(`/api/repo/${repoId}/stack/settings`), { method: "POST", body: JSON.stringify({ enabled: !st.enabled }) }); await again() })
  }
  head.append(toggle)
  wrap.append(head)
  if (!st.compose_file) { wrap.append(el("p", { class: "muted" }, st.error || "This project has no compose.yaml or docker-compose.yml, so there is nothing to run.")); return }
  if (!st.enabled) {
    wrap.append(el("p", { class: "muted" }, "Off. When on, Upstream Manager runs a copy of this compose stack that cannot clash with the real one: no fixed container names, every port moved to a random port on 127.0.0.1, its own network, data folders outside the project replaced by a demo folder, and missing secrets filled with generated demo values. You and connected coding agents can then check it runs, read its logs and send it requests, including from a merge workspace to test a change before approving it."))
    return
  }
  if (!st.docker.ok) wrap.append(el("p", { style: "color:var(--bad)" }, st.docker.detail))

  // controls
  const up = el("button", { class: "btn small", disabled: st.busy ? "" : null }, st.services.length ? "Rebuild & restart" : "Start")
  up.onclick = () => busy(up, "Starting…", async () => { await api(apiUrl(`/api/repo/${repoId}/stack/up`), { method: "POST", body: body({}) }); toast("Starting: pulling and building can take a few minutes.", "info"); await again() })
  const down = el("button", { class: "btn ghost small", disabled: st.busy || !st.services.length ? "" : null }, "Stop")
  down.onclick = () => busy(down, "Stopping…", async () => { await api(apiUrl(`/api/repo/${repoId}/stack/down`), { method: "POST", body: body({}) }); await again() })
  const reset = el("button", { class: "btn ghost small", disabled: st.busy ? "" : null, title: "Stop and delete the demo data and generated secrets" }, "Stop & reset data")
  reset.onclick = () => { if (confirm("Stop the stack and delete its demo data and generated secrets?")) return busy(reset, "Resetting…", async () => { await api(apiUrl(`/api/repo/${repoId}/stack/down`), { method: "POST", body: body({ reset: true }) }); await again() }) }
  const refresh = el("button", { class: "btn ghost small", onclick: () => again() }, "Refresh")
  wrap.append(el("div", { class: "row", style: "margin-bottom:10px" }, up, down, reset, refresh,
    el("span", { class: "muted" }, st.busy ? "working…" : (st.summary || "not started"))))

  // services
  if (st.services.length) {
    const tb = el("tbody")
    for (const s of st.services) {
      const cls = s.state === "running" ? (s.health === "unhealthy" ? "bad" : "up") : s.state === "exited" ? "bad" : "warn"
      tb.append(el("tr", {},
        el("td", {}, el("strong", {}, s.service), el("div", { class: "muted small mono" }, s.image || "")),
        el("td", {}, badge(cls, s.state), s.health ? badge(s.health === "healthy" ? "up" : s.health === "unhealthy" ? "bad" : "", s.health) : ""),
        el("td", {}, ...(s.ports.length ? s.ports.map(p => el("div", {}, el("a", { href: p.url, target: "_blank", rel: "noopener" }, p.url), el("span", { class: "muted small" }, ` → ${p.target}`))) : [el("span", { class: "muted" }, "internal")])),
        el("td", { class: "muted small" }, s.status || "")))
    }
    wrap.append(el("div", { class: "table-box" }, el("table", { class: "list", style: "min-width:600px" },
      el("thead", {}, el("tr", {}, el("th", {}, "Service"), el("th", {}, "State"), el("th", {}, "Open"), el("th", {}, "Status"))), tb)))
  }
  if (st.op_log?.length) wrap.append(el("details", { style: "margin-top:8px", open: st.busy ? "" : null }, el("summary", { class: "muted small" }, "Start/stop log"),
    el("pre", { class: "code-pre", style: "padding:8px;white-space:pre-wrap" }, st.op_log.join("\n"))))

  // demo settings
  const s = st.settings
  const envIn = el("textarea", { rows: "3", style: "width:100%;font:12px var(--mono)", placeholder: "KEY=value, one per line (overrides the generated demo values)" },
    Object.entries(s.env || {}).map(([k, v]) => `${k}=${v}`).join("\n"))
  const svcIn = el("input", { style: "width:100%", placeholder: "all services (or a few, comma separated; their dependencies come along)", value: (s.services || []).join(", ") })
  const profIn = el("input", { style: "width:100%", placeholder: "compose profiles, comma separated", value: (s.profiles || []).join(", ") })
  const save = el("button", { class: "btn ghost small" }, "Save")
  save.onclick = () => busy(save, "Saving…", async () => {
    const env = Object.fromEntries(envIn.value.split("\n").map(l => l.trim()).filter(l => l && l.includes("=")).map(l => [l.slice(0, l.indexOf("=")).trim(), l.slice(l.indexOf("=") + 1)]))
    const list = (x) => x.value.split(",").map(v => v.trim()).filter(Boolean)
    await api(apiUrl(`/api/repo/${repoId}/stack/settings`), { method: "POST", body: JSON.stringify({ env, services: list(svcIn), profiles: list(profIn) }) })
    toast("Saved. Restart the stack to use it.", "ok"); await again()
  })
  wrap.append(el("details", { style: "margin-top:10px" }, el("summary", {}, "Demo settings"),
    el("div", { class: "col", style: "margin-top:8px" },
      st.generated_env?.length ? el("div", { class: "muted small" }, `Generated demo values for: ${st.generated_env.join(", ")} (kept until you reset).`) : null,
      el("label", { class: "col", style: "gap:4px" }, "Environment", envIn),
      el("label", { class: "col", style: "gap:4px" }, "Services", svcIn),
      el("label", { class: "col", style: "gap:4px" }, "Profiles", profIn),
      st.meta?.moved_mounts?.length ? el("div", { class: "muted small" }, `Data folders replaced by demo folders: ${[...new Set(st.meta.moved_mounts.map(m => m.from))].join(", ")}`) : null,
      el("div", {}, save))))

  if (!st.services.length) { if (st.busy) setTimeout(() => wrap.isConnected && again(), 3000); return }

  // logs
  const svcNames = st.services.map(x => x.service)
  const logSvc = el("select", { "aria-label": "Logs of" }, el("option", { value: "" }, "All services"), ...svcNames.map(n => el("option", { value: n }, n)))
  const logBox = el("pre", { class: "code-pre", style: "padding:8px;max-height:320px;white-space:pre-wrap" }, "")
  const errBox = el("div", { class: "row small", style: "gap:6px" })
  const loadLogs = async () => {
    logBox.textContent = "Loading…"
    try {
      const r = await api(apiUrl(`/api/repo/${repoId}/stack/logs${q() ? q() + "&" : "?"}tail=300&service=${encodeURIComponent(logSvc.value)}`))
      logBox.textContent = r.text || "(no output)"; logBox.scrollTop = logBox.scrollHeight
      errBox.replaceChildren(...Object.entries(r.errors || {}).map(([k, v]) => badge("bad", `${k}: ${v.count} error line${v.count > 1 ? "s" : ""}`)))
      if (!Object.keys(r.errors || {}).length) errBox.replaceChildren(badge("up", "no error lines"))
    } catch (e) { logBox.textContent = e.message }
  }
  logSvc.onchange = loadLogs
  wrap.append(el("h4", {}, "Logs"), el("div", { class: "row" }, logSvc, el("button", { class: "btn ghost small", onclick: loadLogs }, "Reload"), errBox), logBox)
  loadLogs()

  // try a request
  const reqSvc = el("select", { "aria-label": "Service" }, ...st.services.filter(x => x.ports.length).map(x => el("option", { value: x.service }, `${x.service} (:${x.ports.map(p => p.target).join(", ")})`)))
  const method = el("select", { "aria-label": "Method" }, ...["GET", "POST", "PUT", "PATCH", "DELETE"].map(m => el("option", {}, m)))
  const path = el("input", { value: "/", "aria-label": "Path", style: "flex:1;min-width:120px" })
  const hdrs = el("textarea", { rows: "2", placeholder: '{"Authorization": "Bearer …"}', style: "width:100%;font:12px var(--mono)" })
  const bodyIn = el("textarea", { rows: "3", placeholder: '{"title": "demo"} or plain text', style: "width:100%;font:12px var(--mono)" })
  const out = el("pre", { class: "code-pre", style: "padding:8px;white-space:pre-wrap;max-height:300px" }, "")
  const send = el("button", { class: "btn small" }, "Send")
  send.onclick = () => busy(send, "Sending…", async () => {
    let headers = {}, b = bodyIn.value.trim() || null
    try { headers = hdrs.value.trim() ? JSON.parse(hdrs.value) : {} } catch { return toast("Headers must be JSON", "error") }
    if (b) { try { b = JSON.parse(b) } catch { /* plain text */ } }
    const r = await api(apiUrl(`/api/repo/${repoId}/stack/request`), { method: "POST", body: body({ service: reqSvc.value, method: method.value, path: path.value, headers, body: b }) })
    out.textContent = r.error ? `Error: ${r.error}` : `${r.status} · ${r.ms} ms · ${r.url}\n${Object.entries(r.headers || {}).map(([k, v]) => `${k}: ${v}`).join("\n")}\n\n${r.json ? JSON.stringify(r.json, null, 2) : r.body}`
  })
  if (reqSvc.options.length) wrap.append(el("h4", {}, "Try a request"),
    el("div", { class: "row" }, reqSvc, method, path, send),
    el("details", {}, el("summary", { class: "muted small" }, "Headers and body"), hdrs, bodyIn), out)
  if (st.busy) setTimeout(() => wrap.isConnected && again(), 3000)
}

// Files: a small VS Code-like editor. Explorer (tree with change marks and a
// right-click menu), tabs, CodeMirror with syntax colours, Ctrl+S to save. It can
// edit the repo itself or a merge workspace (to resolve conflicts by hand).
let cmReady = null
function loadCodeMirror() {
  if (window.CodeMirror) return Promise.resolve()
  return cmReady ||= new Promise((ok, fail) => {
    const s = el("script", { src: `${BASE}/static/vendor/codemirror/codemirror.js` })
    s.onload = ok; s.onerror = () => { cmReady = null; fail(new Error("could not load the editor")) }
    document.head.append(s)
  })
}
const MODES = {
  js: "javascript", mjs: "javascript", cjs: "javascript", jsx: "jsx", ts: "text/typescript", tsx: "text/typescript-jsx",
  json: "application/json", py: "python", css: "css", scss: "text/x-scss", less: "text/x-less", html: "htmlmixed", htm: "htmlmixed",
  vue: "htmlmixed", xml: "xml", svg: "xml", md: "gfm", markdown: "gfm", yml: "yaml", yaml: "yaml", sh: "shell", bash: "shell",
  zsh: "shell", toml: "toml", go: "go", rs: "rust", c: "text/x-csrc", h: "text/x-csrc", cpp: "text/x-c++src", hpp: "text/x-c++src",
  cc: "text/x-c++src", java: "text/x-java", kt: "text/x-kotlin", cs: "text/x-csharp", sql: "sql", ini: "properties",
  cfg: "properties", conf: "nginx", env: "properties", properties: "properties", diff: "diff", patch: "diff",
}
const modeFor = (path) => {
  const base = path.split("/").pop().toLowerCase()
  if (base === "dockerfile" || base.startsWith("dockerfile.")) return "dockerfile"
  if (base === "makefile") return "shell"
  if (base.startsWith(".env")) return "properties"
  return MODES[base.includes(".") ? base.split(".").pop() : ""] || null
}
const modeName = (m) => !m ? "Plain text" : ({ gfm: "Markdown", htmlmixed: "HTML", "text/typescript": "TypeScript", "text/typescript-jsx": "TSX", "application/json": "JSON" }[m] || m.replace(/^text\/x-/, "").replace(/^\w/, c => c.toUpperCase()))

async function renderFiles(wrap, repoId, { ws = null, onScope } = {}) {
  wrap.replaceChildren(loading("Loading the editor…"))
  try { await loadCodeMirror() } catch (e) { wrap.replaceChildren(el("p", { class: "muted", style: "padding:10px" }, e.message)); return }
  const q = (extra = {}) => new URLSearchParams({ ...(ws ? { ws } : {}), ...extra }).toString()
  const post = (path, body) => api(apiUrl(`/api/repo/${repoId}${path}`), { method: "POST", body: JSON.stringify({ ...(ws ? { ws } : {}), ...body }) })

  const side = el("div", { class: "ide-side" })
  const tree = el("div", { class: "tree", role: "tree", "aria-label": "Files", tabindex: "-1" })
  const main = el("div", { class: "ide-main" })
  const tabBar = el("div", { class: "ide-tabs", role: "tablist", "aria-label": "Open files" })
  const host = el("div", { class: "ide-editor" })
  const status = el("div", { class: "ide-bar" })
  main.append(tabBar, host, status)
  wrap.replaceChildren(el("div", { class: "ide" }, side, main))

  // scope: the repo, or one of its merge workspaces
  const scopeRow = el("div", { class: "ide-scope" })
  api(apiUrl(`/api/repo/${repoId}/workspaces`)).then(r => {
    const items = (r.items || []).filter(w => !w.error)
    if (!items.length && !ws) return
    const sel = el("select", { "aria-label": "Edit in" }, el("option", { value: "" }, "This repo's working tree"),
      ...items.map(w => el("option", { value: w.name }, `Workspace: ${w.branch.replace("upkeep/", "")}${w.ready ? "" : " (in progress)"}`)))
    sel.value = ws || ""
    sel.onchange = () => { if (!confirmLeave()) { sel.value = ws || ""; return } onScope?.(sel.value || null); renderFiles(wrap, repoId, { ws: sel.value || null, onScope }) }
    scopeRow.append(el("span", { class: "muted" }, "Edit in"), sel)
  }).catch(() => {})

  const docs = new Map()  // path -> {doc, mtime, clean (generation), tab}
  let current = null, cm = null, changes = {}
  const dirty = (d) => !d.doc.isClean(d.clean)
  const confirmLeave = () => ![...docs.values()].some(dirty) || confirm("You have unsaved changes. Discard them?")

  // ---- explorer
  const btn = (label, title, fn) => el("button", { title, "aria-label": title, onclick: fn }, label)
  const selectedDir = () => {
    const row = tree.querySelector(".trow.sel")
    if (!row) return ""
    return row.dataset.type === "dir" ? row.dataset.path : row.dataset.path.split("/").slice(0, -1).join("/")
  }
  const ask = (title, value = "") => { const v = prompt(title, value); return v === null ? null : v.trim().replace(/^\/+/, "") }
  const newEntry = async (dir, isDir) => {
    const name = ask(isDir ? `New folder in /${dir}` : `New file in /${dir}`, "")
    if (!name) return
    const path = (dir ? `${dir}/` : "") + name
    try { await post("/files/new", { path, dir: isDir }); await reload(); await reveal(path); if (!isDir) openFile(path) }
    catch (e) { toast(e, "error") }
  }
  const rename = async (path) => {
    const to = ask(`Rename or move ${path} to:`, path)
    if (!to || to === path) return
    try {
      await post("/files/rename", { path, to })
      for (const [p, d] of [...docs]) if (p === path || p.startsWith(path + "/")) { closeTab(p, true) }
      await reload(); await reveal(to); toast(`Renamed to ${to}`, "ok")
    } catch (e) { toast(e, "error") }
  }
  const remove = async (path, isDir) => {
    if (!confirm(`Delete ${path}${isDir ? " and everything in it" : ""}? Committed files can be restored with git.`)) return
    try {
      await post("/files/delete", { path })
      for (const p of [...docs.keys()]) if (p === path || p.startsWith(path + "/")) closeTab(p, true)
      await reload()
    } catch (e) { toast(e, "error") }
  }
  side.append(el("div", { class: "ide-side-head" }, el("span", {}, ws ? "Workspace" : "Explorer"), el("span", { class: "spacer" }),
    btn("＋", "New file", () => newEntry(selectedDir(), false)),
    btn("▣", "New folder", () => newEntry(selectedDir(), true)),
    btn("↻", "Refresh", () => reload()),
    btn("⊟", "Collapse folders", () => { tree.querySelectorAll("[aria-expanded='true']").forEach(r => r.click()) })),
    scopeRow, tree)

  let ctx = null
  const closeCtx = () => { ctx?.remove(); ctx = null }
  document.addEventListener("click", closeCtx)
  const showCtx = (ev, row) => {
    ev.preventDefault(); closeCtx()
    const path = row?.dataset.path || "", isDir = !row || row.dataset.type === "dir"
    const items = [
      isDir ? ["New file", () => newEntry(path, false)] : null,
      isDir ? ["New folder", () => newEntry(path, true)] : null,
      row ? ["Rename… (F2)", () => rename(path)] : null,
      row ? ["Copy path", () => { navigator.clipboard?.writeText(path); toast("Path copied", "ok") }] : null,
      row && !isDir && !ws ? ["Tag for chat", () => addTag(repoId, { type: "file", id: path, label: path.split("/").slice(-2).join("/") })] : null,
      row ? ["Delete (Del)", () => remove(path, isDir), true] : null,
    ].filter(Boolean)
    ctx = el("div", { class: "menu-list", style: `position:fixed;left:${ev.clientX}px;top:${ev.clientY}px` },
      ...items.map(([label, fn, danger]) => el("button", { class: danger ? "danger" : "", onclick: (e) => { e.stopPropagation(); closeCtx(); fn() } }, label)))
    document.body.append(ctx)
  }
  tree.addEventListener("contextmenu", (ev) => showCtx(ev, ev.target.closest(".trow")))

  const rows = () => [...tree.querySelectorAll(".trow")].filter(r => r.offsetParent !== null)
  const focusRow = (r) => { if (!r) return; tree.querySelectorAll(".trow").forEach(x => x.tabIndex = -1); r.tabIndex = 0; r.focus() }
  tree.addEventListener("keydown", (ev) => {
    const row = ev.target.closest(".trow")
    if (!row) return
    const all = rows(), i = all.indexOf(row)
    const isDir = row.dataset.type === "dir", open = row.getAttribute("aria-expanded") === "true"
    if (ev.key === "ArrowDown") focusRow(all[i + 1])
    else if (ev.key === "ArrowUp") focusRow(all[i - 1])
    else if (ev.key === "ArrowRight" && isDir && !open) row.click()
    else if (ev.key === "ArrowLeft" && isDir && open) row.click()
    else if (ev.key === "Enter" || ev.key === " ") row.click()
    else if (ev.key === "F2") rename(row.dataset.path)
    else if (ev.key === "Delete") remove(row.dataset.path, isDir)
    else return
    ev.preventDefault()
  })
  const markFor = (path, isDir) => {
    if (!isDir) return changes[path]
    const pre = path + "/"
    return Object.keys(changes).some(p => p.startsWith(pre)) ? "•" : null
  }
  const loadDir = async (path, container, depth) => {
    let res
    try { res = await api(apiUrl(`/api/repo/${repoId}/tree?${q({ path })}`)) }
    catch (e) { container.replaceChildren(el("div", { class: "muted", style: "padding:6px 8px" }, e.message)); return }
    container.innerHTML = ""
    for (const e of res.entries || []) {
      const isDir = e.type === "dir"
      const mark = markFor(e.path, isDir)
      const row = el("div", { class: `trow${mark && !isDir ? ` ch-${mark === "?" ? "q" : mark}` : ""}${mark && isDir ? " has-ch" : ""}`, role: "treeitem", tabindex: "-1",
        "aria-level": String(depth + 1), "data-path": e.path, "data-type": e.type, style: `padding-left:${8 + depth * 14}px`, title: e.path },
        el("span", { "aria-hidden": "true", style: "width:12px;color:var(--muted)" }, isDir ? "▸" : ""),
        el("span", { "aria-hidden": "true" }, isDir ? "📁" : "📄"),
        el("span", { class: "nm" }, e.name),
        mark ? el("span", { class: "st", title: { M: "modified", A: "added", D: "deleted", R: "renamed", U: "conflicted", "?": "untracked" }[mark] || "changed" }, mark === "?" ? "U" : mark) : null)
      container.append(row)
      if (isDir) {
        const kids = el("div", { role: "group", hidden: "" })
        row.setAttribute("aria-expanded", "false")
        container.append(kids)
        row.onclick = async () => {
          const open = row.getAttribute("aria-expanded") !== "true"
          row.setAttribute("aria-expanded", String(open))
          row.firstChild.textContent = open ? "▾" : "▸"
          kids.hidden = !open
          tree.querySelectorAll(".trow.sel").forEach(r => r.classList.remove("sel")); row.classList.add("sel")
          if (open && !kids.children.length) { kids.replaceChildren(loading()); await loadDir(e.path, kids, depth + 1) }
        }
      } else {
        row.onclick = () => { tree.querySelectorAll(".trow.sel").forEach(r => r.classList.remove("sel")); row.classList.add("sel"); openFile(e.path) }
      }
    }
  }
  // Open the folders down to `path` and select it.
  const reveal = async (path) => {
    const parts = path.split("/")
    for (let i = 1; i < parts.length; i++) {
      const r = tree.querySelector(`.trow[data-path="${CSS.escape(parts.slice(0, i).join("/"))}"]`)
      if (r && r.getAttribute("aria-expanded") !== "true") await r.onclick()
    }
    const row = tree.querySelector(`.trow[data-path="${CSS.escape(path)}"]`)
    if (row) { tree.querySelectorAll(".trow.sel").forEach(x => x.classList.remove("sel")); row.classList.add("sel"); row.scrollIntoView({ block: "nearest" }) }
  }
  const reload = async () => {
    const open = [...tree.querySelectorAll("[aria-expanded='true']")].map(r => r.dataset.path)
    try { changes = Object.fromEntries((await api(apiUrl(`/api/repo/${repoId}/changes?${q()}`))).items.map(i => [i.path, i.status])) } catch { changes = {} }
    await loadDir("", tree, 0)
    for (const p of open) { const r = tree.querySelector(`.trow[data-path="${CSS.escape(p)}"]`); if (r && r.getAttribute("aria-expanded") !== "true") await r.onclick() }
    tree.querySelector(".trow")?.setAttribute("tabindex", "0")
  }

  // ---- editor
  const showStatus = () => {
    status.innerHTML = ""
    if (!current) return
    const d = docs.get(current)
    const pos = d.doc.getCursor()
    const save = el("button", { class: "btn small", disabled: dirty(d) ? null : "" }, "Save")
    save.onclick = () => saveFile(current)
    status.append(el("span", { class: "mono", style: "color:var(--soft)" }, (ws ? `[workspace] ` : "") + current),
      el("span", {}, `Ln ${pos.line + 1}, Col ${pos.ch + 1}`), el("span", {}, modeName(d.mode)),
      el("span", { style: `color:${dirty(d) ? "var(--warn)" : "var(--good)"}` }, dirty(d) ? "● unsaved" : "saved"),
      el("span", { class: "spacer" }),
      !ws ? el("button", { class: "btn ghost small", onclick: () => addTag(repoId, { type: "file", id: current, label: current.split("/").slice(-2).join("/") }) }, "Tag for chat") : null,
      save)
  }
  const drawTabs = () => {
    tabBar.innerHTML = ""
    for (const [path, d] of docs) {
      const x = el("span", { class: "x", role: "button", "aria-label": `Close ${path}`, title: "Close", onclick: (e) => { e.stopPropagation(); closeTab(path) } })
      d.tab = el("button", { class: `ide-tab${path === current ? " on" : ""}${dirty(d) ? " dirty" : ""}`, role: "tab", "aria-selected": String(path === current), title: path, onclick: () => activate(path) },
        path.split("/").pop(), x)
      tabBar.append(d.tab)
    }
  }
  const activate = (path) => {
    current = path
    const d = docs.get(path)
    if (!cm) {
      host.innerHTML = ""
      cm = CodeMirror(host, {
        lineNumbers: true, theme: "material-darker", keyMap: "sublime", indentUnit: 4, tabSize: 4, matchBrackets: true,
        autoCloseBrackets: true, styleActiveLine: true, lineWrapping: false,
        extraKeys: { "Ctrl-S": () => saveFile(current), "Cmd-S": () => saveFile(current), "Ctrl-/": "toggleComment", "Cmd-/": "toggleComment", "Alt-F": "findPersistent" },
      })
      cm.on("cursorActivity", showStatus)
    }
    cm.swapDoc(d.doc)
    cm.setOption("indentWithTabs", /\n\t/.test(d.doc.getValue().slice(0, 5000)))
    drawTabs(); showStatus(); setTimeout(() => { cm.refresh(); cm.focus() }, 0)
  }
  const openFile = async (path) => {
    if (docs.has(path)) return activate(path)
    const f = await api(apiUrl(`/api/repo/${repoId}/file?${q({ path })}`)).catch((e) => ({ ok: false, error: e.status === 404 ? "file not found" : e.message }))
    if (!f.ok) { toast(`${path}: ${f.error || "can't open this file"}`, "error"); return }
    const mode = modeFor(path)
    const doc = CodeMirror.Doc(f.content, mode)
    const d = { doc, mode, mtime: f.mtime, clean: doc.changeGeneration() }
    doc.on("change", () => { d.tab?.classList.toggle("dirty", dirty(d)); if (path === current) showStatus() })
    docs.set(path, d)
    activate(path)
    if (/^(<{7}|>{7}) /m.test(f.content)) {
      const line = f.content.split("\n").findIndex(l => l.startsWith("<<<<<<< "))
      cm.setCursor({ line, ch: 0 }); cm.scrollIntoView(null, 120)
      toast("This file has conflict markers: keep what you want, delete the <<<<<<< ======= >>>>>>> lines, then save.", "info")
    }
  }
  const saveFile = async (path, force = false) => {
    const d = docs.get(path)
    if (!d) return
    try {
      const r = await post("/file", { path, content: d.doc.getValue(), mtime: d.mtime, force })
      d.mtime = r.mtime; d.clean = d.doc.changeGeneration()
      drawTabs(); showStatus(); toast(`Saved ${path.split("/").pop()}`, "ok")
      reload()
    } catch (e) {
      if (e.status === 409 && e.data?.changed) {
        if (confirm(`${path} changed on disk since you opened it (maybe git or an agent changed it). Overwrite it with your version?`)) return saveFile(path, true)
      } else toast(e, "error")
    }
  }
  const closeTab = (path, discard = false) => {
    const d = docs.get(path)
    if (!d) return
    if (!discard && dirty(d) && !confirm(`${path} has unsaved changes. Close it anyway?`)) return
    docs.delete(path)
    if (current === path) {
      const next = [...docs.keys()].pop()
      if (next) activate(next)
      else { current = null; cm = null; host.replaceChildren(empty()); drawTabs(); showStatus() }
    } else drawTabs()
  }
  const empty = () => el("div", { class: "ide-empty" },
    el("p", {}, ws ? "Editing the merge workspace. Conflicted files are marked U in red." : "Open a file from the explorer."),
    el("p", { class: "small" }, "Ctrl+S save · Ctrl+F find · Ctrl+H replace · Ctrl+/ comment · Ctrl+D add next match · right-click in the explorer for more."))
  host.append(empty())
  await reload()
}

// Memory: what the assistant remembers about this repo (a local SQLite file:
// keyword search plus links to files and commits). Search can span every
// watched repo to find related changes elsewhere. Day logs are the model's log
// of each day's chats and events.
async function renderMemory(wrap, repoId) {
  wrap.innerHTML = ""
  const head = el("div", { style: "display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap" }, el("h3", { style: "margin:0" }, "Memory"))
  const statusEl = el("div", { class: "muted", style: "margin-top:4px" }, "Loading…")
  const qIn = el("input", { placeholder: "Search memories…", "aria-label": "Search memories", style: "flex:1;min-width:0" })
  const scopeSel = el("select", { "aria-label": "Search scope" }, el("option", { value: "repo" }, "This repo"), el("option", { value: "all" }, "All repos"))
  const kindSel = el("select", { "aria-label": "Show" }, el("option", { value: "" }, "Everything"), el("option", { value: "daylog" }, "Day logs"),
    el("option", { value: "note" }, "Notes"), el("option", { value: "chat" }, "Chat facts"), el("option", { value: "event" }, "Events"), el("option", { value: "commit" }, "Commits"))
  const list = el("div", { class: "mem-list" })
  const load = async () => {
    const q = qIn.value.trim()
    list.replaceChildren(loading(q ? "Searching…" : "Loading…"))
    let res
    try { res = await api(apiUrl(`/api/repo/${repoId}/memory?q=${encodeURIComponent(q)}&scope=${scopeSel.value}&kind=${kindSel.value}`)) }
    catch (e) { res = { ok: false, error: e.message, items: [] } }
    statusEl.textContent = !res.ok
      ? `Memory unavailable: ${res.error || "unknown error"}`
      : `${res.items.length} ${q ? "matches" : "memories"} · day logs ${res.daily_log ? "on" : "off"} · auto-clean ${res.auto_cleanup ? "on" : "off"}`
    list.innerHTML = ""
    for (const m of res.items || []) {
      const del = el("button", { class: "btn ghost mem-del", title: "Forget this", "aria-label": `Forget: ${m.memory.slice(0, 60)}` }, "×")
      del.onclick = () => busy(del, null, async () => { await api(apiUrl(`/api/memory/${m.id}`), { method: "DELETE" }); await load() })
      list.append(el("div", { class: "mem" },
        el("div", {},
          el("span", { class: `badge kind-${m.kind}` }, m.kind),
          m.repo && m.repo !== repoId ? el("span", { class: "badge warn" }, m.repo) : "",
          m.score != null ? el("span", { class: "muted" }, ` ${Number(m.score).toFixed(2)}`) : ""),
        m.kind === "daylog" ? daylogBody(m.memory) : el("div", { style: "flex:1" }, m.memory), del))
    }
    if (!(res.items || []).length && res.ok) list.append(el("p", { class: "muted" }, q ? "No matches." : "Nothing remembered yet. Chat, index commits, or add a note."))
  }
  // Day logs are Markdown and long: show the first lines, expand on click.
  const daylogBody = (text) => {
    const body = el("div", { class: "md", style: "flex:1;min-width:0" })
    const [title, ...rest] = text.split("\n")
    body.innerHTML = renderMarkdown(rest.join("\n").trim())
    const box = el("details", { style: "flex:1;min-width:0" }, el("summary", {}, title), body)
    return box
  }
  qIn.addEventListener("keydown", (e) => { if (e.key === "Enter") load() })
  scopeSel.onchange = load
  kindSel.onchange = load
  const noteIn = el("input", { placeholder: "Add a note to remember…", "aria-label": "Note to remember", style: "flex:1;min-width:0" })
  const noteBtn = el("button", { class: "btn ghost" }, "Remember")
  noteBtn.onclick = () => {
    if (!noteIn.value.trim()) return
    return busy(noteBtn, "Saving…", async () => {
      await api(apiUrl(`/api/repo/${repoId}/memory`), { method: "POST", body: JSON.stringify({ text: noteIn.value.trim() }) })
      noteIn.value = ""
      await load()
    })
  }
  noteIn.addEventListener("keydown", (e) => { if (e.key === "Enter") noteBtn.click() })
  const idxBtn = el("button", { class: "btn ghost", title: "Store the last 50 commits (subject + files) so related changes can be found by meaning" }, "Index commits")
  idxBtn.onclick = () => busy(idxBtn, "Indexing…", async () => {
    const res = await api(apiUrl(`/api/repo/${repoId}/memory/index-commits`), { method: "POST", body: "{}" })
    toast(`Indexed ${res.added} new commits (${res.total} total)`, "ok")
    await load()
  })
  const logBtn = el("button", { class: "btn ghost", title: "Have the model write a log of today's chats and events in this repo (replaces today's earlier log). Finished days are logged automatically." }, "Write today's log")
  logBtn.onclick = () => busy(logBtn, "Writing…", async () => {
    const res = await api(apiUrl(`/api/repo/${repoId}/memory/daylog`), { method: "POST", body: "{}" })
    if (res.ok) { toast(`Logged ${res.day} (${res.sources} messages and events)`, "ok"); kindSel.value = "daylog" }
    else toast(res.error || "Could not write the log", "error")
    await load()
  })
  const cleanBtn = el("button", { class: "btn ghost", title: "Remove duplicates, junk and old events (day logs are kept). Also runs automatically." }, "Clean up")
  cleanBtn.onclick = () => busy(cleanBtn, "Checking…", async () => {
    {
      // Preview first; deletion has no undo, so only the previewed ids are removed.
      const plan = await api(apiUrl(`/api/repo/${repoId}/memory/cleanup`), { method: "POST", body: JSON.stringify({ dry_run: true }) })
      if (!plan.items?.length) toast("Already clean", "ok")
      else {
        const shown = plan.items.slice(0, 12).map(i => `• [${i.reason}] ${i.memory.slice(0, 90)}`).join("\n")
        const more = plan.items.length > 12 ? `\n…and ${plan.items.length - 12} more` : ""
        if (confirm(`Remove ${plan.items.length} memories? This can't be undone.\n\n${shown}${more}`)) {
          const res = await api(apiUrl(`/api/repo/${repoId}/memory/cleanup`), { method: "POST", body: JSON.stringify({ ids: plan.items.map(i => i.id) }) })
          toast(`Removed ${res.removed}, ${res.kept} kept`, "ok")
        }
      }
    }
    await load()
  })
  head.append(el("div", { style: "display:flex;gap:6px;flex-wrap:wrap" }, logBtn, cleanBtn, idxBtn))
  wrap.append(head, statusEl,
    el("div", { style: "display:flex;gap:6px;margin-top:8px" }, qIn, scopeSel, kindSel),
    el("div", { style: "display:flex;gap:6px;margin-top:6px" }, noteIn, noteBtn),
    list)
  await load()
}

async function renderRequests() {
  const content = $("#content")
  content.innerHTML = ""
  content.append(el("h1", { class: "page", style: "margin-bottom:12px" }, "Approval requests"))
  const wrap = el("div", {}, loading())
  content.append(wrap)
  try {
    const reqs = (await api(apiUrl('/api/requests'))).slice().reverse()  // newest first
    wrap.innerHTML = ""
    if (!reqs.length) { wrap.append(el("p", { class: "muted" }, "No requests. The daily check raises one when a merge needs your approval.")); return; }
    for (const r of reqs) {
      const pending = r.status === "pending"
      const commentIn = el("input", { placeholder: "add a note…", "aria-label": `Note for ${r.repo_name || r.repo_id}`, style: "width:220px" })
      const decide = (decision, btn) => busy(btn, decision === "approve" ? (isPush ? "Pushing…" : r.type === "components" ? "Bumping…" : "Merging…") : "Denying…", async () => {
        const res = await api(apiUrl(`/api/request/${encodeURIComponent(r.id)}/decide`), { method: "POST", body: JSON.stringify({ decision, comment: commentIn.value }) })
        const detail = (res.request?.result || []).map(o => `${o.upstream}: ${o.detail}`).join("; ")
        if (decision === "deny") toast("Denied", "info")
        else if (isActions) toast(res.ok ? `Workflows updated. ${detail}` : `Some changes failed. ${res.error || detail}`, res.ok ? "ok" : "error")
        else if (isPush) toast(res.ok ? `Pushed. ${detail}` : `Not pushed. ${res.error || detail}`, res.ok ? "ok" : "error")
        else if (r.type === "components") toast(res.ok ? `Pins bumped. ${detail}` : `Not bumped. ${res.error || detail}`, res.ok ? "ok" : "error")
        else toast(res.ok ? `Merged. ${detail}` : `Not merged. ${res.error || detail}`, res.ok ? "ok" : "error")
        await renderRequests()
      })
      const isComp = r.type === "components"
      const isPush = r.type === "push"
      const isActions = r.type === "actions"
      const approveBtn = el("button", { class: "btn" }, isActions ? "Approve and apply on GitHub" : isPush ? (r.open_pr ? "Approve, push and open PR" : "Approve and push") : isComp ? "Approve and bump pins" : "Approve and merge")
      approveBtn.onclick = () => decide("approve", approveBtn)
      // A merge with conflicts can't be approved through: it is always aborted. Offer
      // the pull report instead, which can raise a request for the clean part.
      const conflicted = !isComp && !isPush && !isActions ? (r.upstreams || []).filter(u => u.conflict) : []
      const reportBtns = conflicted.map(u => {
        const b = el("button", { class: "btn" }, conflicted.length > 1 ? `Pull report: ${u.name}` : "Pull report")
        b.onclick = () => showPullReport(r.repo_id, { upstream: u.name })
        return b
      })
      const denyBtn = el("button", { class: "btn danger" }, "Deny")
      denyBtn.onclick = () => decide("deny", denyBtn)
      wrap.append(el("div", { class: "repo-card", id: `req-${r.id}` },
        el("h3", {}, r.repo_name || r.repo_id, " ",
          isActions ? badge("", `GitHub Actions: ${(r.disable || []).length} off, ${(r.enable || []).length} on`)
          : isPush ? badge("", r.open_pr ? `push + PR into ${r.base}` : "push")
          : isComp ? badge(r.safe === false ? "bad" : "", r.safe === false ? "has pinned commits not upstream" : "submodule pins")
            : el("span", { class: `badge ${r.safe === false ? "bad" : ""}` }, r.safe === false ? "conflicts" : "needs a merge commit"),
          el("span", { class: `badge ${pending ? "warn" : r.status === "approved" ? "up" : ""}` }, r.status)
        ),
        el("div", { class: "muted" }, `focus: ${r.focus_branch} · raised ${(r.created_at || "").slice(0, 16).replace("T", " ")}`
          + (r.decided_at ? ` · decided ${r.decided_at.slice(0, 16).replace("T", " ")}` : "")),
        isActions ? el("div", { class: "upstream", style: "margin:8px 0" },
          el("div", {}, `${r.owner}/${r.github_repo}` + (r.fork ? ` (fork of ${r.parent})` : "")),
          ...(r.disable || []).map(w => el("div", { style: "font-size:12px" }, "disable ", el("strong", {}, w.name), w.reasons?.length ? el("span", { class: "muted" }, ` — ${w.reasons.join("; ")}`) : null)),
          ...(r.enable || []).map(w => el("div", { style: "font-size:12px" }, "enable ", el("strong", {}, w.name)))) : null,
        isPush ? el("div", { class: "upstream", style: "margin:8px 0" },
          el("div", {}, `${r.branch} → ${r.remote} (${r.remote_url})`),
          el("div", { class: "muted", style: "font-size:12px" }, `${(r.commits || []).length}${(r.commits || []).length === 20 ? "+" : ""} commit(s): ` + (r.commits || []).slice(0, 5).map(c => `${c.sha} ${c.subject}`).join(" · "))) : null,
        isComp ? el("div", { style: "margin:8px 0" }, ...(r.components || []).map(c =>
          el("div", { class: "upstream" },
            el("div", {}, `${c.name} (${c.path}): ↓ ${c.behind} upstream commits · pinned ${(c.pin || "").slice(0, 8)} → ${(c.upstream || "").slice(0, 8)}`),
            (c.missing || []).length ? el("div", { class: "muted", style: "font-size:12px" }, "newest: " + c.missing.slice(0, 3).map(m => `${m.sha} ${m.subject}`).join(" · ")) : null,
            c.local ? el("div", { class: "muted", style: "color:#e08080" }, `${c.local} pinned commit(s) are not upstream: this one will be skipped`) : null,
            (c.watched_changes || []).length ? el("div", { style: "color:#e0a060;font-size:13px" },
              `⚠ upstream changed ${c.watched_changes.length} watched file(s): ${c.watched_changes.slice(0, 6).join(", ")}`,
              c.after_update ? el("div", {}, "After bumping, run: ", el("code", {}, c.after_update)) : null) : null
          ))) : null,
        isComp || isPush || isActions ? null : el("div", { style: "margin:8px 0" }, ...(r.upstreams || []).map(u =>
          el("div", { class: "upstream" },
            el("div", {}, `${u.name} / ${u.branch}: ↓ ${u.behind} behind · ↑ ${u.ahead} ahead`),
            u.reason ? el("div", { class: "muted", style: "color:#e0a060" }, u.reason) : null,
            (u.conflict_files || []).length ? el("div", { class: "muted", style: "font-family:monospace;font-size:12px" }, u.conflict_files.slice(0, 20).join(", ")) : null
          )
        )),
        (r.result || []).length ? el("div", { class: "muted" }, "Result: " + r.result.map(o => `${o.upstream}: ${o.detail}`).join("; ")) : null,
        (r.result || []).filter(o => o.url).map(o => el("div", {}, el("a", { href: o.url, target: "_blank", rel: "noopener", style: "color:#9fb4ff" }, `Open the pull request: ${o.url}`))),
        (r.result || []).filter(o => o.after_update).map(o => el("div", { style: "color:#e0a060;font-size:13px" }, `Now run for ${o.name}: `, el("code", {}, o.after_update))),
        r.summary ? el("div", { style: "margin:6px 0;font-size:13px" }, el("strong", {}, "Agent's summary: "), r.summary) : null,
        r.workspace ? el("div", { class: "muted", style: "font-size:12px" }, `Resolved in merge workspace ${r.workspace}; approving fast-forwards ${r.focus_branch} to it and removes the workspace.`) : null,
        r.comment ? el("div", { class: "muted" }, `Note: ${r.comment}`) : null,
        pending ? el("div", { style: "display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px" }, ...(reportBtns.length ? reportBtns : [approveBtn]), denyBtn, commentIn,
          el("span", { class: "muted", style: "font-size:12px" }, isActions
            ? "Approve changes these workflows on GitHub (needs a token with Actions: read/write). Files are not touched."
            : isPush
            ? "Approve pushes this branch (never forced). A PR is opened if you asked for one."
            : isComp
            ? "Approve moves each pin to its upstream in one commit on your local branch. Nothing is pushed."
            : reportBtns.length
            ? "These upstream commits conflict with yours. The pull report shows which ones merge cleanly and can raise a request for just those."
            : "Approve merges exactly the commits shown into your local branch. Conflicts are never auto-resolved; the merge is aborted instead. Nothing is pushed.")) : null
      ))
    }
  } catch (e) { wrap.replaceChildren(el("p", { class: "muted" }, `Could not load requests: ${e.message}`)) }
}

async function renderSettings() {
  const content = $("#content")
  content.innerHTML = ""
  content.append(el("h1", { class: "page", style: "margin-bottom:12px" }, "Settings"))
  const card = el("div", { class: "repo-card", style: "margin-top:16px" }, loading())
  content.append(card)
  try {
    const local = await api(apiUrl('/api/settings'))
    card.innerHTML = ""
    const llm = local.llm || {}
    const urlIn = el("input", { value: llm.base_url || "http://127.0.0.1:8080/v1", placeholder: "http://host:8080/v1", style: "width:420px" })
    // The server only ever sends a masked key ("••••1234"); sending it back unchanged keeps the saved key.
    const keyIn = el("input", { value: llm.api_key || "", type: "password", autocomplete: "off", placeholder: "optional", style: "width:320px" })
    const modelSel = el("select", { style: "width:420px" })
    const modelStatus = el("div", { class: "muted" }, "")
    const loadModels = async () => {
      modelStatus.textContent = "Loading models…"
      const q = `?base_url=${encodeURIComponent(urlIn.value)}&api_key=${encodeURIComponent(keyIn.value)}`
      const r = await api(apiUrl('/api/models') + q).catch(e => ({ data: [], error: e.message }))
      const ids = (r.data || []).map(m => m.id)
      if (llm.model && !ids.includes(llm.model)) ids.unshift(llm.model)
      modelSel.innerHTML = ""
      for (const id of ids) modelSel.append(el("option", { value: id }, id))
      modelSel.value = (modelSelValue || llm.model || ids[0] || "")
      modelStatus.textContent = r.error ? `Could not list models: ${r.error}` : `${(r.data || []).length} models available`
    }
    let modelSelValue = llm.model
    modelSel.onchange = () => { modelSelValue = modelSel.value }
    const refreshBtn = el("button", { class: "btn ghost" }, "Refresh models")
    refreshBtn.onclick = () => busy(refreshBtn, "Loading…", loadModels)
    const saveBtn = el("button", { class: "btn" }, "Save")
    saveBtn.onclick = () => busy(saveBtn, "Saving…", async () => {
      const res = await api(apiUrl('/api/settings'), { method: "POST", body: JSON.stringify({ llm: { model: modelSel.value, base_url: urlIn.value.trim(), api_key: keyIn.value } }) })
      keyIn.value = res.settings?.llm?.api_key || ""
      toast("Model connection saved", "ok")
    })
    card.append(
      el("div", { style: "display:flex;flex-direction:column;gap:12px" },
        el("div", { class: "muted" }, "Any OpenAI-compatible server (llama-swap, llama-server, Argos). Independent of hermes-agent."),
        el("label", { style: "display:flex;flex-direction:column;gap:4px" }, "Server URL", urlIn),
        el("label", { style: "display:flex;flex-direction:column;gap:4px" }, "API key", keyIn),
        el("label", { style: "display:flex;flex-direction:column;gap:4px" }, "Model", modelSel),
        modelStatus,
        el("div", { style: "display:flex;gap:8px;flex-wrap:wrap" }, refreshBtn, saveBtn)
      )
    )
    loadModels()
    // Accounts: GitHub token and Docker Hub login, stored in the OS keychain
    // (or env vars / a private file on servers), never shown again in full.
    const accounts = await api(apiUrl('/api/credentials'))
    const accRow = (key, label, type, placeholder) => {
      const info = accounts[key]
      const input = el("input", { type, value: "", placeholder: info.set ? `${info.hint} (saved in ${info.source})` : placeholder, autocomplete: "off", style: "width:320px" })
      return { key, input, row: el("label", { style: "display:flex;flex-direction:column;gap:4px" }, label, input) }
    }
    const accRows = [
      accRow("github_token", "GitHub token (fine-grained: Contents, Pull requests, Secrets, Workflows, Actions read/write)", "password", "github_pat_…"),
      accRow("dockerhub_username", "Docker Hub username", "text", "username"),
      accRow("dockerhub_token", "Docker Hub access token (Account settings → Personal access tokens)", "password", "dckr_pat_…"),
    ]
    const accSave = el("button", { class: "btn" }, "Save accounts")
    accSave.onclick = () => busy(accSave, "Saving…", async () => {
      const body = Object.fromEntries(accRows.filter(r => r.input.value.trim()).map(r => [r.key, r.input.value.trim()]))
      if (!Object.keys(body).length) return toast("Type a value to save", "info")
      await api(apiUrl('/api/credentials'), { method: "POST", body: JSON.stringify(body) })
      toast("Saved", "ok"); await renderSettings()
    })
    const testBtn = (which, label) => {
      const b = el("button", { class: "btn ghost" }, label)
      b.onclick = () => busy(b, "Checking…", async () => {
        const r = await api(apiUrl('/api/credentials/test'), { method: "POST", body: JSON.stringify({ which }) })
        toast(r.ok ? `Works: signed in as ${r.login}${r.scopes ? ` (scopes: ${r.scopes})` : ""}` : r.error, r.ok ? "ok" : "error")
      })
      return b
    }
    const removeBtn = el("button", { class: "btn ghost" }, "Remove all")
    removeBtn.onclick = () => {
      if (!confirm("Remove the saved GitHub token and Docker Hub login? (Environment variables are not affected.)")) return
      return busy(removeBtn, "Removing…", async () => {
        await api(apiUrl('/api/credentials'), { method: "POST", body: JSON.stringify({ github_token: "", dockerhub_username: "", dockerhub_token: "" }) })
        toast("Removed", "ok"); await renderSettings()
      })
    }
    card.append(el("h4", { style: "margin-top:20px" }, "Accounts"),
      el("div", { class: "muted" }, "Used to push branches, open pull requests and set Actions secrets. Environment variables UPSTREAM_GITHUB_TOKEN, UPSTREAM_DOCKERHUB_USERNAME and UPSTREAM_DOCKERHUB_TOKEN override these."),
      el("div", { style: "display:flex;flex-direction:column;gap:12px;margin-top:8px" }, ...accRows.map(r => r.row),
        el("div", { style: "display:flex;gap:8px;flex-wrap:wrap" }, accSave, testBtn("github", "Test GitHub"), testBtn("dockerhub", "Test Docker Hub"), removeBtn)))
    const mem = local.memory || {}
    const st = await api(apiUrl('/api/memory/status'))
    const onOff = (v) => { const sel = el("select", { style: "width:160px" }, el("option", { value: "on" }, "On"), el("option", { value: "off" }, "Off")); sel.value = v ? "on" : "off"; return sel }
    const enabledSel = onOff(st.enabled), dailySel = onOff(st.daily_log), cleanSel = onOff(st.auto_cleanup)
    const memSave = el("button", { class: "btn", style: "margin-top:8px" }, "Save memory settings")
    const describe = (r) => r.ok ? `Memory is running: ${r.memories} memories, ${r.messages} chat messages kept · ${r.path}` : `Memory not running: ${r.error || "not started"}`
    const memStatus = el("div", { class: "muted" }, describe(st))
    memSave.onclick = () => busy(memSave, "Saving…", async () => {
      await api(apiUrl('/api/settings'), { method: "POST", body: JSON.stringify({ memory: { ...mem, enabled: enabledSel.value, daily_log: dailySel.value, auto_cleanup: cleanSel.value } }) })
      const r = await api(apiUrl("/api/memory/status"))
      memStatus.textContent = describe(r)
      toast(r.ok || !r.enabled ? "Memory settings saved" : "Memory failed to start", r.ok || !r.enabled ? "ok" : "error")
    })
    card.append(el("h4", { style: "margin-top:20px" }, "Memory"),
      el("div", { class: "muted" }, "A local SQLite database: notes, chat facts, events and commits, linked to the files and commits they mention, with keyword search. No embedding model is needed. Day logs use the chat model above to write a log of each day's chats and events."),
      el("div", { style: "display:flex;flex-direction:column;gap:12px;margin-top:8px" },
        el("label", { style: "display:flex;flex-direction:column;gap:4px" }, "Memory", enabledSel),
        el("label", { style: "display:flex;flex-direction:column;gap:4px" }, "Write a log of each day with activity", dailySel),
        el("label", { style: "display:flex;flex-direction:column;gap:4px" }, "Automatic cleanup", cleanSel),
        memStatus, memSave))
    card.append(await agentSetup())
  } catch (e) { card.replaceChildren(el("p", { class: "muted" }, `Could not load settings: ${e.message}`)) }
}

// Coding agents connect to Upstream Manager as an MCP server (mcp_server.py).
// One config snippet per agent, filled in with this machine's paths.
async function agentSetup() {
  const box = el("div")
  let s
  try { s = await api(apiUrl("/api/agent-setup")) } catch (e) { box.append(el("p", { class: "muted" }, `Agent setup unavailable: ${e.message}`)); return box }
  const [cmd, ...args] = s.command
  const q = (x) => JSON.stringify(x)
  const snippets = {
    OpenCode: ["opencode.json (in the repo, or ~/.config/opencode/opencode.json)", JSON.stringify({
      $schema: "https://opencode.ai/config.json",
      mcp: { "upstream-manager": { type: "local", command: s.command, enabled: true, environment: { UPSTREAM_APP_URL: s.app_url } } },
    }, null, 2)],
    Codex: ["~/.codex/config.toml",
      `[mcp_servers.upstream-manager]\ncommand = ${q(cmd)}\nargs = [${args.map(q).join(", ")}]\nenv = { UPSTREAM_APP_URL = ${q(s.app_url)} }`],
    "Claude Code": ["run once in a terminal",
      `claude mcp add upstream-manager -e UPSTREAM_APP_URL=${s.app_url} -- ${s.command.join(" ")}`],
    Hermes: ["cli-config.yaml",
      `mcp_servers:\n  upstream-manager:\n    command: ${cmd}\n    args: [${args.map(q).join(", ")}]\n    env:\n      UPSTREAM_APP_URL: ${s.app_url}`],
  }
  const pick = el("select", { "aria-label": "Coding agent" }, ...Object.keys(snippets).map(k => el("option", { value: k }, k)))
  const where = el("div", { class: "muted", style: "font-size:12px" })
  const code = el("pre", { style: "white-space:pre-wrap;word-break:break-all;background:#0f1115;border:1px solid #2a2f3a;border-radius:8px;padding:10px;margin:0;font-size:12px" })
  const show = () => { where.textContent = `Put this in ${snippets[pick.value][0]}:`; code.textContent = snippets[pick.value][1] }
  const copy = el("button", { class: "btn ghost" }, "Copy")
  copy.onclick = () => { navigator.clipboard?.writeText(code.textContent); toast("Copied", "ok") }
  pick.onchange = show; show()
  box.append(el("h4", { style: "margin-top:20px" }, "Coding agents"),
    el("div", { class: "muted" }, "Connect OpenCode, Codex, Claude Code or Hermes so the agent can read each repo's briefing, incoming commits, conflicts and memory, and resolve upstream merges in a separate merge workspace. It can raise requests but never approve, merge into your branch or push: those stay here. Keep this app running while the agent works."),
    el("div", { style: "display:flex;flex-direction:column;gap:8px;margin-top:8px" },
      el("div", { style: "display:flex;gap:8px;align-items:center" }, pick, copy), where, code))
  return box
}

// Chats: every conversation, of every repo and the general ones, in one place.
async function renderChats() {
  const content = $("#content")
  content.replaceChildren(loading())
  const params = new URLSearchParams(location.search)
  let [chats, repos] = await Promise.all([api(apiUrl("/api/chats")), api(apiUrl("/api/repos"))])
  let pick = { repo: params.get("repo") || chats[0]?.repo_id || GENERAL, session: params.get("session") || chats[0]?.id || null }
  content.innerHTML = ""
  const filterSel = el("select", { "aria-label": "Show chats of" }, el("option", { value: "" }, "All chats"), el("option", { value: GENERAL }, "General (no repo)"),
    ...repos.map(r => el("option", { value: r.id }, r.name || r.id)))
  const findIn = el("input", { placeholder: "Find a chat…", "aria-label": "Find a chat", style: "flex:1;min-width:0" })
  const listBox = el("div", { style: "flex:1;overflow:auto" })
  const newRepo = el("select", { "aria-label": "New chat in", style: "flex:1;min-width:0" }, el("option", { value: GENERAL }, "General (no repo)"), ...repos.map(r => el("option", { value: r.id }, r.name || r.id)))
  const newBtn = el("button", { class: "btn small" }, "New chat")
  newBtn.onclick = () => busy(newBtn, null, async () => {
    const res = await api(apiUrl(`/api/repo/${newRepo.value}/sessions`), { method: "POST", body: "{}" })
    pick = { repo: newRepo.value, session: res.session }; openChat()
  })
  const side = el("aside", { class: "panel chat-list", "aria-label": "Chats" },
    el("div", { class: "col", style: "gap:6px;padding:8px;border-bottom:1px solid var(--line)" },
      el("div", { class: "row", style: "gap:6px" }, newRepo, newBtn), el("div", { class: "row", style: "gap:6px" }, filterSel, findIn)),
    listBox)
  const chatPane = el("section", { class: "pane chat", style: "flex:1 1 auto" })
  const drawList = () => {
    const f = filterSel.value, t = findIn.value.trim().toLowerCase()
    listBox.innerHTML = ""
    const shown = chats.filter(c => (!f || c.repo_id === f) && (!t || (c.title || "").toLowerCase().includes(t) || (c.tags || []).some(x => x.label.toLowerCase().includes(t))))
    if (!shown.length) listBox.append(el("p", { class: "muted", style: "padding:10px" }, chats.length ? "No chats match." : "No chats yet. Start one above."))
    for (const c of shown) {
      listBox.append(el("button", { class: `chat-item${c.id === pick.session ? " on" : ""}`, onclick: () => { pick = { repo: c.repo_id, session: c.id }; openChat() } },
        el("div", { class: "t" }, c.title || "New chat"),
        el("div", { class: "row small", style: "gap:4px;margin-top:3px" },
          badge(c.repo_id === GENERAL ? "info" : "", c.repo_id === GENERAL ? "General" : c.repo_name),
          ...(c.tags || []).slice(0, 3).map(x => badge("info", `${{ commit: "◆", pr: "⇄", request: "✓", push: "↑" }[x.type] || ""} ${x.label.slice(0, 24)}`)),
          el("span", { class: "muted" }, `${c.count} · ${ago(c.updated_at)}`))))
    }
  }
  const openChat = () => {
    history.replaceState({}, "", `${BASE}/chats?repo=${encodeURIComponent(pick.repo)}${pick.session ? `&session=${pick.session}` : ""}`)
    const name = pick.repo === GENERAL ? "General" : (repos.find(r => r.id === pick.repo)?.name || pick.repo)
    chatPane.replaceChildren(chatPanel(pick.repo, {
      sessionId: pick.session, title: name,
      onSession: (sid) => { pick.session = sid; drawList() },
      onChange: async () => { chats = await api(apiUrl("/api/chats")); drawList() },
    }))
    drawList()
  }
  filterSel.onchange = drawList; findIn.oninput = drawList
  content.append(el("div", { class: "chats-layout" }, side, chatPane))
  openChat()
}

// Workflows: is each repo's CI passing? Latest run per workflow, with links to GitHub.
async function renderWorkflows(refresh = false) {
  const content = $("#content")
  content.replaceChildren(el("div", { class: "row" }, el("h1", { class: "page" }, "Workflows")), loading("Asking GitHub…"))
  const items = await api(apiUrl(`/api/workflows${refresh ? "?refresh=1" : ""}`))
  const refreshBtn = el("button", { class: "btn ghost" }, "Refresh")
  refreshBtn.onclick = () => renderWorkflows(true)
  const anyToken = items.some(i => i.token)
  content.replaceChildren(el("div", { class: "col", style: "gap:14px" },
    el("div", { class: "row" }, el("h1", { class: "page" }, "Workflows"), el("div", { class: "spacer" }), refreshBtn),
    anyToken ? null : el("div", { class: "card muted" }, "Public repos work without signing in, up to GitHub's 60 requests an hour. For private repos, and to disable workflows or set secrets, save a GitHub token in ",
      el("a", { href: `${BASE}/settings` }, "Settings → Accounts"), ". No Unraid login or GitHub password is needed: a fine-grained token limited to your repos is enough.")))
  const wrap = content.firstChild
  const health = { passing: ["up", "passing"], failing: ["bad", "failing"], running: ["info", "running"], skipped: ["", "skipped"], "no runs": ["", "no runs"], disabled: ["", "disabled"] }
  const dot = (c) => el("span", { title: c || "", style: `display:inline-block;width:8px;height:8px;border-radius:2px;background:${c === "success" ? "#3fb96f" : ["failure", "timed_out", "startup_failure"].includes(c) ? "#e5484d" : ["cancelled", "skipped", "neutral"].includes(c) ? "#5c6575" : "#d6a01e"}` })
  if (!items.length) wrap.append(el("p", { class: "muted" }, "No repos yet."))
  for (const r of items) {
    const head = el("div", { class: "row" }, el("h2", {}, r.repo_name), el("div", { class: "spacer" }))
    if (r.ok) {
      const c = r.counts || {}
      head.append(c.failing ? badge("bad", `${c.failing} failing`) : "", c.running ? badge("info", `${c.running} running`) : "",
        badge(c.failing ? "" : "up", `${c.passing || 0} passing`), el("a", { href: r.url, target: "_blank", rel: "noopener", class: "small" }, "GitHub Actions ↗"),
        el("a", { href: `${BASE}/repo/${r.repo_id}`, class: "small", onclick: (e) => { e.preventDefault(); e.stopPropagation(); store_.set(`tab:${r.repo_id}`, "Actions"); route(`/repo/${r.repo_id}`) } }, "Manage"))
    }
    const card = el("section", { class: "card" }, head)
    if (!r.ok) card.append(el("div", { class: "muted", style: "margin-top:6px" }, r.error))
    else if (!r.workflows.length) card.append(el("div", { class: "muted", style: "margin-top:6px" }, "No workflows."))
    else {
      const tbody = el("tbody")
      for (const w of r.workflows) {
        const [cls, label] = health[w.health] || ["", w.health]
        const last = w.last
        tbody.append(el("tr", {},
          el("td", {}, el("div", { style: "font-weight:600" }, w.name), el("div", { class: "muted small mono" }, w.path)),
          el("td", {}, badge(cls, label)),
          el("td", {}, last ? [el("a", { href: last.url, target: "_blank", rel: "noopener" }, last.title || "run"),
            el("div", { class: "muted small" }, `${last.event} on ${last.branch} · ${ago(last.at)}`)] : el("span", { class: "muted" }, "-")),
          el("td", {}, el("div", { class: "row", style: "gap:3px" }, ...(w.recent || []).slice().reverse().map(dot)))))
      }
      card.append(el("div", { class: "table-box", style: "margin-top:10px" }, el("table", { class: "list" },
        el("thead", {}, el("tr", {}, el("th", {}, "Workflow"), el("th", {}, "Status"), el("th", {}, "Latest run"), el("th", {}, "Last 10 (oldest → newest)"))), tbody)))
    }
    wrap.append(card)
  }
}

function currentView() {
  // location.pathname already carries the BASE when served behind Studio
  // (e.g. /upstream/requests); strip it to get the view-relative path.
  let path = location.pathname
  if (BASE && path.startsWith(BASE)) path = path.slice(BASE.length)
  const parts = path.split('/').filter(Boolean)
  if (parts[0] === 'repo' && parts[1]) return ['repo', decodeURIComponent(parts[1])]
  if (['requests', 'settings', 'chats', 'workflows'].includes(parts[0])) return [parts[0]]
  return ['repos']
}

async function router() {
  const [view, id] = currentView()
  tools = null
  $("#main").classList.toggle("wide", view === "repo" || view === "chats")
  for (const a of $$("nav.main a")) {
    const on = a.dataset.view === (view === "repo" ? "repos" : view)
    a.classList.toggle("on", on)
    if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current")
  }
  document.title = { repos: "Repositories", repo: id, chats: "Chats", workflows: "Workflows", requests: "Requests", settings: "Settings" }[view] + " · Upstream Manager"
  try {
    if (view === "repos") await renderRepos()
    else if (view === "repo") await renderRepoPage(id)
    else if (view === "chats") await renderChats()
    else if (view === "workflows") await renderWorkflows()
    else if (view === "requests") await renderRequests()
    else if (view === "settings") await renderSettings()
  } catch (e) { $("#content").replaceChildren(el("p", { class: "muted" }, `Could not load this page: ${e.message}`)) }
  refreshPendingCount()
}

// SPA nav: intercept in-page link clicks so the shell is not reloaded.
document.addEventListener('click', (e) => {
  const link = e.target.closest('a[href]')
  if (!link) return
  const href = link.getAttribute('href')
  if (!href) return
  // Only intercept same-host, non-target links (internal Upstream nav).
  if (link.target === '_blank' || link.hasAttribute('data-no-intercept')) return
  const resolved = new URL(href, window.location.origin)
  if (resolved.origin !== window.location.origin) return
  e.preventDefault()
  // `resolved.pathname` is already BASE-prefixed (e.g. /upstream/requests).
  // Strip the BASE so route() can re-append it once.
  let navPath = resolved.pathname
  if (navPath.startsWith(BASE)) navPath = navPath.slice(BASE.length)
  route(navPath)
})
window.addEventListener('popstate', router)
// The script tag sits at the end of <body>, so the DOM is ready already.
router()
refreshModelStatus()
