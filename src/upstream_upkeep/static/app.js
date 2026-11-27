// Upstream Manager frontend — single-page app that talks to the Flask API.
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
    throw new Error("Can't reach Upstream Manager's server. Is it running?")
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
    const s = await api(apiUrl('/api/llm/status'))
    const ok = s.up && !!s.model
    box.className = `status-dot ${ok ? "ok" : "bad"}`
    box.lastChild.textContent = !s.base_url ? "No model set" : !s.up ? "Model server down" : s.model ? `Model: ${s.model}` : "No model chosen"
    const others = Object.entries(s.servers || {}).filter(([u]) => u !== s.base_url).map(([u, v]) => `${u}: ${v.up ? "up" : "down"}`)
    box.title = s.base_url ? `${s.base_url}${s.up && s.model && !s.serves_model ? " (doesn't list this model)" : ""}${others.length ? "\n" + others.join("\n") : ""}` : "Set the model connection in Settings"
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
  const [allRepos, reqs] = await Promise.all([api(apiUrl('/api/repos')), refreshPendingCount()])
  // Groups ("glance", "studio"...): a chip per group filters the page; Fetch and the daily check follow it.
  let group = ""; try { group = localStorage.getItem("repo-group") || "" } catch {}
  const allGroups = [...new Set(allRepos.flatMap(r => r.groups || []))].sort()
  if (group && !allGroups.includes(group)) group = ""
  const repos = group ? allRepos.filter(r => (r.groups || []).includes(group)) : allRepos
  const inView = new Set(repos.map(r => r.id))
  const pending = reqs.filter(r => r.status === "pending" && (!group || inView.has(r.repo_id)))
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
  const fetchBtn = el("button", { class: "btn ghost" }, group ? `Fetch ${group}` : "Fetch all")
  fetchBtn.onclick = () => busy(fetchBtn, "Fetching…", async () => {
    const failed = []
    await Promise.all(repos.map(r => api(apiUrl(`/api/repo/${r.id}/fetch`), { method: "POST", body: "{}" }).catch(() => failed.push(r.name || r.id))))
    if (failed.length) toast(`Fetch failed for ${failed.join(", ")}`, "error")
    await renderRepos()
  })
  const runBtn = el("button", { class: "btn" }, group ? `▶ Check ${group}` : "▶ Run daily check")
  runBtn.onclick = () => busy(runBtn, "Running…", async () => {
    const res = await api(apiUrl('/api/run-daily'), { method: "POST", body: JSON.stringify(group ? { group } : {}) })
    const n = (s) => res.results.filter(r => r.status === s).length
    toast(`Done: ${n("merged")} fast-forwarded, ${n("request_raised")} requests raised, ${n("skipped")} skipped, ${n("error")} errors`
      + res.results.filter(r => r.status === "skipped").map(r => `\n${r.repo_name}: ${r.detail}`).join(""), n("error") || n("skipped") ? "error" : "ok")
    await renderRepos()
  })
  content.append(el("div", { class: "col", style: "gap:16px" },
    el("div", { class: "row" }, el("h1", { class: "page" }, "Repositories"), el("div", { class: "spacer" }), addToggle, fetchBtn, runBtn),
    addBox))
  const wrap = content.firstChild
  if (allGroups.length) {
    const pick = (g) => { try { localStorage.setItem("repo-group", g) } catch {}; renderRepos() }
    wrap.append(el("div", { class: "row group-chips", role: "toolbar", "aria-label": "Repo groups", style: "gap:6px;flex-wrap:wrap" },
      el("span", { class: "muted small" }, "Groups:"),
      el("button", { class: `chip${group ? "" : " on"}`, "aria-pressed": String(!group), onclick: () => pick("") }, `All (${allRepos.length})`),
      ...allGroups.map(g => el("button", { class: `chip${g === group ? " on" : ""}`, "aria-pressed": String(g === group), onclick: () => pick(g) },
        `${g} (${allRepos.filter(r => (r.groups || []).includes(g)).length})`))))
  }

  if (!allRepos.length) {
    addBox.hidden = false
    wrap.append(el("p", { class: "muted" }, "No repos yet. Add one above, or put a clone in your Repos folder (ManagerFiles/Repos); it shows up here by itself."))
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
        r.integration?.state && r.integration.state !== "none" ? el("a", { href: `${BASE}/integrations`, style: "margin-left:6px;text-decoration:none", title: r.integration.note || "" },
          badge(MARK_CLS[r.integration.state], { integrated: "integrated", partial: "partly integrated", planned: "to integrate" }[r.integration.state])) : null,
        ...(r.groups || []).map(g => el("span", { class: "badge", style: "margin-left:6px", title: `group ${g}` }, `#${g}`)),
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
  if (u.waiting) out.push(badge("", `⏳ ${u.waiting} waiting`, `release wait ${u.wait_days} day(s): the newest ${u.waiting} commit(s) go in from ${(u.ready_at || "").slice(0, 16).replace("T", " ")}`))
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

// Reference repos you changed join every chat, the way extra folders join a coding agent's conversation.
// Remove one from a chat and it stays out of that chat until it changes again.
let _editedCache = { at: 0, items: [] }
async function editedRefs() {
  if (Date.now() - _editedCache.at < 30000) return _editedCache.items
  const r = await api(apiUrl("/api/refs/edited")).catch(() => null)
  _editedCache = { at: Date.now(), items: r?.ok ? r.items : [] }
  return _editedCache.items
}
const _editedOff = (key) => { try { return JSON.parse(localStorage.getItem(`edited-off:${key}`) || "{}") } catch { return {} } }
function dismissEdited(key, path, sig) { const m = _editedOff(key); m[path] = sig; try { localStorage.setItem(`edited-off:${key}`, JSON.stringify(m)) } catch {} }
async function joinEditedRefs(key) {
  const items = await editedRefs(), off = _editedOff(key)
  let tags = loadTags(key), added = 0, full = 0
  for (const e of items) {
    const sig = `${e.head}:${e.files_count}:${e.commits}`
    const at = tags.findIndex(t => t.type === "ref" && t.id === e.path)
    if (at >= 0) { if (tags[at].edited && tags[at].edited !== sig) { tags[at].edited = sig; added++ } continue }
    if (off[e.path] === sig) continue
    if (tags.length >= MAX_TAGS) { full++; continue }
    tags.push({ type: "ref", id: e.path, label: `✎ ${e.name} (your edited reference)`, edited: sig }); added++
  }
  // ones that are no longer edited leave again
  const still = new Set(items.map(e => e.path))
  const kept = tags.filter(t => !t.edited || still.has(t.id))
  if (kept.length !== tags.length) { tags = kept; added++ }
  if (added) saveTags(key, tags)
  if (full) toast(`${full} edited reference repo${full > 1 ? "s" : ""} couldn't join this chat: it already has ${MAX_TAGS} attachments`, "info")
  return added
}

// Ask the chat about something: attach it and start the question in the composer.
function askAbout(repoId, tag, prompt) {
  const t = loadTags(repoId)
  if (!t.some(x => x.type === tag.type && x.id === tag.id)) {
    if (t.length >= MAX_TAGS) return toast(`You can attach up to ${MAX_TAGS} items. Remove one from the chat tray first.`, "error")
    t.push(tag); saveTags(repoId, t)
  }
  refreshTray()
  if (tools && matchMedia("(max-width: 900px)").matches) tools.show("Chat")
  document.dispatchEvent(new CustomEvent("chat-prefill", { detail: { repoId, text: prompt } }))
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
  catch (e) {
    content.replaceChildren(el("div", { class: "card", style: "max-width:520px;margin:40px auto;text-align:center" },
      el("h1", { class: "page" }, e.status === 404 ? "Repo not found" : "Could not load this repo"),
      el("p", { class: "muted" }, e.status === 404 ? `No repo has the id “${repoId}”. It may have been removed.` : e.message),
      el("a", { href: `${BASE}/repos`, class: "btn" }, "Go to your repos")))
    return
  }
  content.innerHTML = ""
  const removeRepo = async () => {
    if (!confirm(`Stop tracking ${repo.name || repoId}? Its folder and git history are left as they are; its chats, requests, demo stacks and merge workspaces in Upstream Manager are removed.`)) return
    await api(apiUrl(`/api/repo/${repoId}`), { method: "DELETE", body: "{}" })
    toast(`${repo.name || repoId} removed`, "ok"); route("/repos")
  }
  if (repo.missing) {
    const pathIn = el("input", { placeholder: "/where/it/is/now", "aria-label": "New folder", style: "flex:1;min-width:200px" })
    const move = el("button", { class: "btn" }, "Use this folder")
    move.onclick = () => busy(move, "Checking…", async () => {
      await api(apiUrl(`/api/repo/${repoId}/path`), { method: "POST", body: JSON.stringify({ path: pathIn.value }) })
      toast("Folder updated", "ok"); await renderRepoPage(repoId)
    })
    content.append(el("div", { class: "repo-head" }, el("a", { href: `${BASE}/repos`, class: "muted", style: "text-decoration:none" }, "Repos"),
      el("span", { class: "muted" }, "/"), el("h1", { class: "page" }, repo.name || repoId)),
      el("section", { class: "notice attention", style: "margin-top:14px" },
        el("h2", {}, "This repo's folder is missing"),
        el("div", {}, "Nothing is at ", el("code", {}, repo.path), " any more, or it is no longer a git repository. It was probably moved or deleted. Upstream Manager won't fetch, compare or merge it until this is fixed."),
        el("div", { class: "row" }, pathIn, move),
        el("div", { class: "row" }, el("span", { class: "muted" }, "Or, if it's gone for good:"),
          el("button", { class: "btn ghost", onclick: (e) => busy(e.currentTarget, "Removing…", removeRepo) }, "Remove from Upstream Manager"))))
    return
  }

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
  const groupsEl = el("span", { class: "row", style: "gap:4px" })
  const drawGroups = (gs) => groupsEl.replaceChildren(...gs.map(g => el("button", { class: "chip", title: `Show the ${g} group`, onclick: () => { try { localStorage.setItem("repo-group", g) } catch {}; route("/repos") } }, `#${g}`)))
  drawGroups(repo.groups || [])
  const editGroups = async () => {
    const all = Object.keys((await api(apiUrl("/api/groups")).catch(() => ({ groups: {} }))).groups || {})
    const v = prompt(`Groups for ${repo.name || repoId}, comma separated` + (all.length ? ` (existing: ${all.join(", ")})` : " (e.g. glance, web)") + ":", (repo.groups || []).join(", "))
    if (v === null) return
    const r = await api(apiUrl(`/api/repo/${repoId}/groups`), { method: "POST", body: JSON.stringify({ groups: v }) }).catch(e => ({ ok: false, error: e.message }))
    if (!r.ok) return toast(r.error, "error")
    repo.groups = r.groups; drawGroups(r.groups); toast(r.groups.length ? `Groups: ${r.groups.join(", ")}` : "In no group", "ok")
  }
  const pathEl = el("button", { class: "path btn link", title: `${repo.path} (click to copy)`, onclick: () => { navigator.clipboard?.writeText(repo.path); toast("Path copied", "ok") } }, repo.path)
  content.append(el("div", { class: "repo-head" },
    el("a", { href: `${BASE}/repos`, class: "muted", style: "text-decoration:none" }, "Repos"), el("span", { class: "muted" }, "/"),
    el("h1", { class: "page" }, repo.name || repoId),
    el("label", { class: "row muted", style: "gap:6px" }, "Focus", focusSel), tree, groupsEl, el("div", { class: "spacer" }), pathEl,
    menu(`More actions for ${repo.name || repoId}`, [
      { label: "New task…", run: () => taskDialog({ repo: repoId }) },
      { label: "Groups…", run: editGroups },
      { label: "Import issues…", run: () => issuesDialog(repoId) },
      { label: "Copy folder path", run: () => { navigator.clipboard?.writeText(repo.path); toast("Path copied", "ok") } },
      { label: "Remove from Upstream Manager", danger: true, run: removeRepo },
    ])))

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

  const wantSession = new URLSearchParams(location.search).get("session")
  const chat = chatPanel(repoId, { sessionId: wantSession, focus: repo.focus_branch, onWide: () => setMode(bench.classList.contains("chat-wide") ? "normal" : "wide"), isWide: () => bench.classList.contains("chat-wide") })
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
      { label: `Release wait… (${u.wait_days ? `${u.wait_days} day(s)` : "off"})`, run: async () => {
        const v = prompt(`Only take ${u.name}'s commits once they are this many days old (0 = take everything at once). Upstreams sometimes publish a bad change and fix it a day later.`, String(u.wait_days || 0))
        if (v === null) return
        const r = await api(apiUrl(`/api/repo/${repoId}/release-wait`), { method: "POST", body: JSON.stringify({ upstream: u.name, days: v }) }).catch(e => ({ ok: false, error: e.message }))
        if (!r.ok) return toast(r.error, "error"); toast(`${u.name}: ${r.upstreams[u.name] ? `waits ${r.upstreams[u.name]} day(s)` : "no wait"}`, "ok"); await again() } },
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

// What the auto-approval rules did with a request: checking, passed, or failed (with the output).
function autoLine(r) {
  const a = r.auto
  if (!a || !a.state) return null
  const head = { checking: "⏳ Auto-approval: checking", passed: "✓ Auto-approval check passed", failed: "✗ Auto-approval check failed: it waits for you" }[a.state] || a.state
  const out = a.detail ? el("details", { style: "margin-top:4px" }, el("summary", { class: "small" }, "output"), el("pre", { class: "small mono", style: "white-space:pre-wrap;max-height:240px;overflow:auto" }, a.detail)) : null
  return el("div", { class: "small", style: `margin:6px 0;color:${a.state === "failed" ? "var(--bad)" : a.state === "passed" ? "var(--good, #6c6)" : "inherit"}` },
    head + (a.check ? ` (${a.check})` : ""), out)
}

// "Goes in by itself": which of this repo's requests approve themselves, and the check they must pass.
async function autoApproveCard(repoId) {
  const card = el("div", { class: "card", style: "margin-bottom:12px" })
  const r = (await api(apiUrl(`/api/repo/${repoId}/auto-approve`)).catch(() => null))?.rules
  if (!r) return card
  const box = (k, label) => { const i = el("input", { type: "checkbox", style: "height:auto", "aria-label": label }); i.checked = !!r[k]; return [i, el("label", { class: "row", style: "gap:6px" }, i, label)] }
  const [clean, cleanL] = box("clean", "Clean upstream pulls")
  const [comps, compsL] = box("components", "Submodule pin bumps")
  const [wsx, wsL] = box("workspaces", "Workspaces an agent submits (needs a check)")
  const check = el("input", { value: r.check || "", placeholder: "check command, e.g. npm ci && npm test", "aria-label": "Check command", style: "flex:1 1 320px" })
  const timeout = el("input", { type: "number", min: "30", value: String(r.timeout || 1200), "aria-label": "Check timeout (seconds)", style: "width:90px" })
  const save = el("button", { class: "btn small" }, "Save")
  save.onclick = () => busy(save, "Saving…", async () => {
    const res = await api(apiUrl(`/api/repo/${repoId}/auto-approve`), { method: "POST", body: JSON.stringify({ clean: clean.checked, components: comps.checked, workspaces: wsx.checked, check: check.value, timeout: timeout.value }) }).catch(e => ({ ok: false, error: e.message }))
    toast(res.ok ? "Saved. Waiting requests are checked against it now." : res.error, res.ok ? "ok" : "error")
  })
  card.append(el("div", { class: "row" }, el("strong", {}, "Goes in by itself"), el("span", { class: "muted small" }, "auto-approval: only you can change this")),
    el("div", { class: "row", style: "gap:16px;margin-top:6px;flex-wrap:wrap" }, cleanL, compsL, wsL),
    el("div", { class: "row", style: "gap:8px;margin-top:6px;flex-wrap:wrap" }, check, el("span", { class: "muted small" }, "timeout (s)"), timeout, save),
    el("p", { class: "muted small", style: "margin:6px 0 0" }, "When one of these is raised, its exact result is rebuilt in a throwaway checkout and the check runs there. It is approved only when the check passes; otherwise it waits for you with the output. Pushes never go by themselves."))
  return card
}

async function renderMerges(wrap, repoId) {
  const [res, auto] = await Promise.all([api(apiUrl(`/api/repo/${repoId}/workspaces`)), autoApproveCard(repoId)])
  wrap.innerHTML = ""
  wrap.append(auto)
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
      (w.reused_resolutions || []).length ? el("div", { class: "small", style: "margin-top:4px" }, `♻ Resolved as before (remembered): ${w.reused_resolutions.join(", ")}`) : null,
      el("div", { class: "muted small", style: "margin-top:4px" }, `Merge helpers: remembered resolutions on · Mergiraf ${w.merge_helpers?.mergiraf ? "on" : "not installed (mergiraf.org)"}`),
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
    const followIn = el("select", { "aria-label": `${c.name} follows` }, el("option", { value: "branch" }, "follow the branch head"), el("option", { value: "releases" }, "follow releases (tags)"))
    followIn.value = c.follow || "branch"
    const ageIn = el("input", { type: "number", min: "0", step: "0.5", value: String(c.min_age_days || 0), "aria-label": `${c.name} release wait (days)`, title: "Only move the pin to commits or releases this many days old", style: "width:70px" })
    const enabledIn = el("input", { type: "checkbox", "aria-label": `Track ${c.name}` })
    enabledIn.checked = c.enabled !== false
    const saveBtn = el("button", { class: "btn ghost" }, "Save")
    saveBtn.onclick = () => busy(saveBtn, "Saving…", async () => {
      await api(apiUrl(`/api/repo/${repoId}/components/${encodeURIComponent(c.name)}`), { method: "POST", body: JSON.stringify({
        branch: branchIn.value.trim(), watch_paths: watchIn.value, after_update: hintIn.value.trim(), enabled: enabledIn.checked, follow: followIn.value, min_age_days: ageIn.value }) })
      toast(`${c.name} settings saved`, "ok")
      await renderComponents(wrap, repoId)
    })
    const status = c.status === "disabled" ? [badge("", "not tracked")]
      : ["not_checked_out", "not_fetched", "error"].includes(c.status) ? [badge(c.status === "error" ? "bad" : "warn", c.status.replace(/_/g, " "), c.detail)]
      : [c.waiting ? badge("", `⏳ ${c.waiting} waiting`, `release wait ${c.min_age_days} day(s): ready ${(c.ready_at || "").slice(0, 16).replace("T", " ")}${c.waiting_tags ? ` (${c.waiting_tags.join(", ")})` : ""}`) : null,
        c.behind ? badge("warn", `↓ ${c.behind} behind${c.release ? ` release ${c.release}` : ""}`, "upstream commits newer than the pin") : badge("up", c.release ? `on the latest release ${c.release}` : "up to date"),
         c.unreleased ? badge("", `${c.unreleased} unreleased upstream`, "commits on the branch after the latest release; they are not compared") : null,
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
      el("div", { class: "muted" }, `${c.url}` + (c.pin ? ` · pinned ${c.pin_tag || c.pin.slice(0, 8)}` : "") + (c.upstream ? ` · ${c.release ? "release" : "upstream"} ${c.release || c.upstream.slice(0, 8)}` : "")),
      c.status === "error" && c.detail ? el("div", { class: "small", style: "margin-top:4px;color:var(--bad)" }, c.detail) : null,
      c.suggest_releases ? el("div", { class: "row small", style: "margin-top:4px;color:var(--info)" }, c.suggest_releases,
        el("button", { class: "btn ghost small", onclick: (e) => busy(e.currentTarget, "Saving…", async () => {
          await api(apiUrl(`/api/repo/${repoId}/components/${encodeURIComponent(c.name)}`), { method: "POST", body: JSON.stringify({ follow: "releases" }) })
          await renderComponents(wrap, repoId) }) }, "Follow releases")) : null,
      (c.watched_changes || []).length ? el("div", { style: "color:#e0a060;font-size:13px;margin-top:4px" },
        `Upstream changed files this repo depends on: ${c.watched_changes.slice(0, 8).join(", ")}${c.watched_changes.length > 8 ? " …" : ""}`,
        c.after_update ? el("div", {}, "After bumping, run: ", el("code", {}, c.after_update)) : null) : null,
      el("details", { style: "margin-top:6px" }, el("summary", { class: "muted" }, "Settings"),
        el("div", { style: "display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:6px" },
          el("label", { class: "muted" }, enabledIn, " track"), followIn, branchIn, watchIn, hintIn, el("label", { class: "muted small" }, "wait ", ageIn, " days"), saveBtn))
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
    if (hist.task) head.append(el("button", { class: "btn ghost small", title: "This chat works on a task", onclick: () => route(`/work?task=${hist.task}`) }, "Task ↗"))
    head.append(menu("Chat actions", [
      { label: "Make a task of this chat…", run: () => taskDialog({ repo: repoId === GENERAL ? undefined : repoId, title: hist.title || "", kind: "fix", dialogTitle: "Make a task of this chat",
        body: (hist.messages || []).filter(m => m.role !== "agent").slice(-4).map(m => `${m.role === "user" ? "You" : "Assistant"}: ${m.content.slice(0, 600)}`).join("\n\n"),
        about: (hist.tags || []).filter(t => ["commit", "request", "pr", "push"].includes(t.type)).map(t => ({ type: t.type, id: t.id, label: t.label, url: t.url })) }) },
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
        const x = el("button", { class: "chip-x", "aria-label": `Remove ${t.label}`, onclick: () => {
          if (t.edited) dismissEdited(trayKey, t.id, t.edited)   // it comes back only if it changes again
          saveTags(trayKey, loadTags(trayKey).filter(y => !(y.type === t.type && y.id === t.id))); refreshTray() } }, "×")
        tray.append(el("span", { class: "chip" }, t.type === "ref" ? t.label : `${t.type === "commit" ? "◆" : "▤"} ${t.label}`, x))
      }
    }
    refreshTray()
    joinEditedRefs(trayKey).then(n => { if (n && tray.isConnected) refreshTray() })
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
    const onPrefill = (e) => {
      if (!msgIn.isConnected) return document.removeEventListener("chat-prefill", onPrefill)
      if (e.detail.repoId !== repoId) return
      msgIn.value = e.detail.text + msgIn.value; msgIn.focus(); msgIn.setSelectionRange(msgIn.value.length, msgIn.value.length)
    }
    document.addEventListener("chat-prefill", onPrefill)
    try {  // a task sent here leaves its brief as the first message, for you to read and send
      const draft = sessionStorage.getItem(`draft:${sessionId}`)
      if (draft) { msgIn.value = draft; sessionStorage.removeItem(`draft:${sessionId}`); msgIn.rows = 8; setTimeout(() => msgIn.focus(), 0) }
    } catch {}
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
    `Forge: ${info.forge.configured ? `${info.forge.url}${acc.forge_token.set ? "" : " (no token)"}` : "not set up"} · `,
    `Docker Hub: ${acc.dockerhub_username.set && acc.dockerhub_token.set ? acc.dockerhub_username.hint : "not set"} · `,
    el("a", { href: `${BASE}/settings`, style: "color:#9fb4ff" }, "Accounts"))

  // --- push
  const where = (r) => r.forge === "forge" ? "forge" : r.forge === "github" ? "GitHub" : ""
  const remSel = el("select", { "aria-label": "Remote" }, ...info.remotes.map(r => el("option", { value: r.name },
    `${r.name}${r.owner ? ` (${where(r)}: ${r.owner}/${r.repo})` : ""}${r.name === info.home ? " · home" : ""}`)))
  if (info.home) remSel.value = info.home
  const ahead = () => { const r = info.remotes.find(x => x.name === remSel.value); return r?.ahead == null ? "new branch on the remote" : `${r.ahead} commit(s) to push` }
  const aheadEl = el("span", { class: "muted" }, ahead())
  remSel.onchange = () => { aheadEl.textContent = ahead() }
  const prIn = el("input", { type: "checkbox", "aria-label": "Open a pull request" })
  const baseIn = el("input", { value: "main", "aria-label": "Pull request base branch", style: "width:110px" })
  const pushBtn = el("button", { class: "btn" }, "Request push")
  const homeBtn = el("button", { class: "btn ghost", title: "Push here by default: Hermes, tasks and this page use it" }, "Make home")
  homeBtn.onclick = () => busy(homeBtn, "Saving…", async () => {
    const r = await api(apiUrl(`/api/repo/${repoId}/home`), { method: "POST", body: JSON.stringify({ remote: remSel.value }) })
    toast(r.ok ? `${r.home} is now this repo's home` : r.error, r.ok ? "ok" : "error")
    if (r.ok) await renderPublish(wrap, repoId)
  })
  pushBtn.onclick = () => busy(pushBtn, "Raising…", async () => {
    await api(apiUrl(`/api/repo/${repoId}/push/request`), { method: "POST", body: JSON.stringify({ remote: remSel.value, open_pr: prIn.checked, base: baseIn.value.trim() }) })
    toast("Push request raised. Approve it on the Requests page.", "ok"); route("/requests")
  })
  const pushCard = el("div", { class: "upstream" },
    el("strong", {}, `Push ${info.branch}`), " ", aheadEl,
    el("div", { style: "display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:6px" },
      el("label", { class: "muted" }, "to ", remSel),
      el("label", { class: "muted" }, prIn, " open a pull request into ", baseIn), pushBtn, homeBtn),
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

  wrap.append(accountLine, pushCard, await mirrorsCard(wrap, repoId, info), dockerCard, secCard)
}

// Mirrors: the repo's copy on the homelab forge. Make one (private), make it
// home, and mirror it on to GitHub so public repos stay in step by themselves.
async function mirrorsCard(wrap, repoId, info) {
  const when = (t) => !t || t.startsWith("1970") || t.startsWith("0001") ? "never" : new Date(t).toLocaleString()
  const card = el("div", { class: "upstream" }, el("strong", {}, "Home forge and mirrors"))
  if (!info.forge.configured) {
    card.append(el("div", { class: "muted" }, "No forge set up. Add its address and token in ",
      el("a", { href: `${BASE}/settings`, style: "color:#9fb4ff" }, "Settings → Accounts"), " to keep private copies at home and mirror them to GitHub."))
    return card
  }
  const m = await api(apiUrl(`/api/repo/${repoId}/mirrors`))
  const ghSel = (skip = []) => {
    const left = (m.github || []).filter(g => !skip.includes(`${g.url}.git`))
    const sel = el("select", { "aria-label": "GitHub repo to mirror to" }, el("option", { value: "" }, "no GitHub mirror"),
      ...left.map(g => el("option", { value: g.url }, `mirror to ${g.url.replace("https://", "")}`)))
    if (left.length) sel.value = left[0].url
    return [sel, left.length]
  }
  if (!m.remotes.length) {
    const nameIn = el("input", { value: info.github_repo || "", placeholder: "repo name", "aria-label": "Name on the forge", style: "width:180px" })
    const [mirror] = ghSel()
    const home = el("input", { type: "checkbox", "aria-label": "Make it home" }); home.checked = true
    const make = el("button", { class: "btn" }, "Create private copy on the forge")
    make.onclick = () => busy(make, "Creating…", async () => {
      const r = await api(apiUrl(`/api/repo/${repoId}/forge/create`), { method: "POST",
        body: JSON.stringify({ name: nameIn.value.trim() || undefined, make_home: home.checked, mirror_to: mirror.value || undefined }) })
      toast(r.ok ? r.detail.join("; ") + (r.mirror_error ? ` (mirror failed: ${r.mirror_error})` : "") + ". Push the branch to fill it." : r.error,
        r.ok && !r.mirror_error ? "ok" : "error")
      if (r.ok) await renderPublish(wrap, repoId)
    })
    card.append(el("div", { class: "muted" }, `This repo has no copy on ${m.url} yet.`),
      el("div", { style: "display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:6px" },
        nameIn, mirror, el("label", { class: "muted" }, home, " make it home"), make))
    return card
  }
  for (const r of m.remotes) {
    if (r.error || !r.found) {
      card.append(el("div", { class: "muted" }, `${r.remote}: ${r.error || "not found on the forge (deleted, or the token can't see it)"}`))
      continue
    }
    const lines = [el("div", {}, el("a", { href: r.web_url, target: "_blank", rel: "noopener", style: "color:#9fb4ff" }, `${r.owner}/${r.repo}`),
      el("span", { class: "muted" }, ` · remote ${r.remote} · ${r.private ? "private" : "visible to forge users"}` +
        (r.mirror ? ` · pull mirror of ${r.original_url} every ${r.mirror_interval}, last ${when(r.mirror_updated)}` : "")))]
    for (const pm of r.push_mirrors) {
      const rm = el("button", { class: "btn ghost" }, "Stop mirroring")
      rm.onclick = () => busy(rm, "Removing…", async () => {
        if (!confirm(`Stop mirroring ${r.owner}/${r.repo} to ${pm.address}? Nothing on GitHub is deleted.`)) return
        const x = await api(apiUrl(`/api/repo/${repoId}/mirrors/push/remove`), { method: "POST", body: JSON.stringify({ remote: r.remote, name: pm.name }) })
        toast(x.ok ? "Mirror removed" : x.error, x.ok ? "ok" : "error"); if (x.ok) await renderPublish(wrap, repoId)
      })
      lines.push(el("div", { class: "muted", style: "display:flex;gap:8px;align-items:center;flex-wrap:wrap" },
        `→ ${pm.address} (${pm.sync_on_commit ? "on every push" : `every ${pm.interval}`}, last ${when(pm.last_update)})`,
        pm.last_error ? el("span", { style: "color:#e08080" }, pm.last_error) : null, rm))
    }
    const syncBtn = el("button", { class: "btn ghost" }, "Sync now")
    syncBtn.onclick = () => busy(syncBtn, "Syncing…", async () => {
      const x = await api(apiUrl(`/api/repo/${repoId}/mirrors/sync`), { method: "POST", body: JSON.stringify({ remote: r.remote }) })
      toast(x.ok ? x.detail : x.error, x.ok ? "ok" : "error")
    })
    const row = el("div", { style: "display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:6px" })
    if (!r.mirror) {
      const [mirror, left] = ghSel(r.push_mirrors.map(pm => pm.address))
      const add = el("button", { class: "btn" }, "Add GitHub mirror")
      add.onclick = () => busy(add, "Adding…", async () => {
        if (!mirror.value) return toast("Pick the GitHub repo to mirror to", "info")
        const x = await api(apiUrl(`/api/repo/${repoId}/mirrors/push`), { method: "POST", body: JSON.stringify({ remote: r.remote, to: mirror.value }) })
        toast(x.ok ? x.detail : x.error, x.ok ? "ok" : "error"); if (x.ok) await renderPublish(wrap, repoId)
      })
      if (left) row.append(mirror, add)
    }
    if (r.mirror || r.push_mirrors.length) row.append(syncBtn)
    card.append(...lines, row)
  }
  card.append(el("div", { class: "muted", style: "font-size:12px;margin-top:4px" },
    "Commit on one side only: a mirror stops at the first conflict. The forge keeps the GitHub token for its mirrors."))
  return card
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
  const names = ["Chat", "Work", "Commits", "Files", "Merges", "Stack", "Memory", "Publish", "Actions"]
  const bar = el("div", { class: "tabs", role: "tablist", "aria-label": "Repo tools" })
  const panes = {}, tabs = {}, built = {}
  let filesScope = null  // a merge workspace name, or null for the repo itself
  const builders = {
    Chat: async () => {}, Work: renderRepoWork, Commits: renderCommits, Merges: renderMerges, Stack: (pane, id) => renderStack(pane, id), Memory: renderMemory, Publish: renderPublish, Actions: renderActions,
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
    el("option", { value: "all" }, "All commits"),
    el("option", { value: "branch" }, "Another branch…"))
  // A quick look at any other branch (local or fetched), without checking it out.
  const brSel = el("select", { "aria-label": "Branch to look at", hidden: "" })
  api(apiUrl(`/api/repo/${repoId}/refs`)).then(r => brSel.replaceChildren(...(r.items || []).map(b => el("option", { value: b }, b)))).catch(() => {})
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
    brSel.hidden = viewSel.value !== "branch"; upSel.hidden = viewSel.value === "branch"
    const q = `view=${viewSel.value}&${viewSel.value === "branch" ? `branch=${encodeURIComponent(brSel.value)}` : src}&skip=${skip}&limit=50&q=${encodeURIComponent(qIn.value.trim())}`
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
      const ask = el("button", { class: "btn ghost", title: "Ask the chat about this commit", "aria-label": `Ask about commit ${c.sha}` }, "Ask")
      ask.onclick = () => comp ? toast("Ask about component commits from the component's own repo.", "info")
        : askAbout(repoId, { type: "commit", id: c.full, label: `${c.sha} ${c.subject.slice(0, 40)}` }, `About commit ${c.sha} (“${c.subject.slice(0, 60)}”): `)
      const work = el("button", { class: "btn ghost", title: "Make a task of it: fix it here, send it to an agent, or park it", "aria-label": `Work on commit ${c.sha}` }, "Work on")
      work.onclick = () => taskDialog({ repo: repoId, kind: "fix", title: `Fix: ${c.subject}`.slice(0, 200), dialogTitle: `Work on ${c.sha}`,
        about: [comp ? { type: "component", id: comp, label: `${comp} ${c.sha}` } : { type: "commit", id: c.full, label: `${c.sha} ${c.subject.slice(0, 40)}` }] })
      list.append(el("div", { class: "mem" },
        el("code", { style: "color:#9fb4ff" }, c.sha),
        el("div", { style: "flex:1;min-width:0" }, el("div", {}, c.subject), el("div", { class: "muted", style: "font-size:12px" }, `${c.author} · ${c.date.slice(0, 10)}`)),
        view, ask, work, tag))
    }
    skip += res.items.length
    if (!list.children.length) info.textContent = viewSel.value === "behind" ? "Nothing missing: you have every upstream commit." : "No commits."
    more.style.display = res.more ? "" : "none"
  }
  refreshCommits = () => load(true)
  more.onclick = () => busy(more, "Loading…", () => load(false))
  viewSel.onchange = () => load(true)
  upSel.onchange = () => load(true)
  brSel.onchange = () => load(true)
  qIn.addEventListener("keydown", (e) => { if (e.key === "Enter") load(true) })
  wrap.append(el("div", { style: "display:flex;gap:6px;flex-wrap:wrap" }, viewSel, upSel.options.length > 1 ? upSel : "", brSel, qIn), info, list, more)
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
  const workBtn = el("button", { class: "btn ghost" }, "Work on it")
  workBtn.onclick = () => { close(); taskDialog({ repo: repoId, kind: "fix", title: `Fix: ${(d.message || "").split("\n")[0]}`.slice(0, 200), dialogTitle: `Work on ${d.sha.slice(0, 10)}`,
    about: [{ type: "commit", id: d.sha, label: `${d.sha.slice(0, 8)} ${(d.message || "").split("\n")[0].slice(0, 40)}` }] }) }
  if (component) workBtn.hidden = true
  const askBtn = el("button", { class: "btn ghost" }, "Ask about it")
  askBtn.onclick = () => { close(); askAbout(repoId, { type: "commit", id: d.sha, label: `${d.sha.slice(0, 8)} ${(d.message || "").split("\n")[0].slice(0, 40)}` },
    `About commit ${d.sha.slice(0, 8)} (“${(d.message || "").split("\n")[0].slice(0, 60)}”): `) }
  if (component) askBtn.hidden = true
  const closeBtn = el("button", { class: "btn ghost", onclick: close }, "Close")
  const titleId = `commit-title-${sha.slice(0, 10)}`
  const ov = el("div", { class: "overlay", onclick: (e) => { if (e.target === ov) close() } },
    el("div", { class: "overlay-panel", role: "dialog", "aria-modal": "true", "aria-labelledby": titleId },
      el("div", { style: "display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap" },
        el("h3", { style: "margin:0", id: titleId }, d.ok ? `Commit ${d.sha.slice(0, 10)}` : "Commit"), el("div", { style: "display:flex;gap:6px" }, d.ok ? askBtn : null, d.ok ? workBtn : null, d.ok ? tag : null, closeBtn)),
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
  up.onclick = () => busy(up, "Starting…", async () => { await api(apiUrl(`/api/repo/${repoId}/stack/up`), { method: "POST", body: body({ build: !!st.services.length }) }); toast("Starting: pulling and building can take a few minutes.", "info"); await again() })
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
  const extraIn = el("input", { style: "width:100%", placeholder: "override files in the project, comma separated (e.g. docker/compose.proxy-ca.yml)", value: (s.extra_files || []).join(", ") })
  const save = el("button", { class: "btn ghost small" }, "Save")
  save.onclick = () => busy(save, "Saving…", async () => {
    const env = Object.fromEntries(envIn.value.split("\n").map(l => l.trim()).filter(l => l && l.includes("=")).map(l => [l.slice(0, l.indexOf("=")).trim(), l.slice(l.indexOf("=") + 1)]))
    const list = (x) => x.value.split(",").map(v => v.trim()).filter(Boolean)
    await api(apiUrl(`/api/repo/${repoId}/stack/settings`), { method: "POST", body: JSON.stringify({ env, services: list(svcIn), profiles: list(profIn), extra_files: list(extraIn) }) })
    toast("Saved. Restart the stack to use it.", "ok"); await again()
  })
  wrap.append(el("details", { style: "margin-top:10px" }, el("summary", {}, "Demo settings"),
    el("div", { class: "col", style: "margin-top:8px" },
      st.generated_env?.length ? el("div", { class: "muted small" }, `Generated demo values for: ${st.generated_env.join(", ")} (kept until you reset).`) : null,
      el("label", { class: "col", style: "gap:4px" }, "Environment", envIn),
      el("label", { class: "col", style: "gap:4px" }, "Services", svcIn),
      el("label", { class: "col", style: "gap:4px" }, "Profiles", profIn),
      el("label", { class: "col", style: "gap:4px" }, "Override files", extraIn),
      st.meta?.moved_mounts?.length ? el("div", { class: "muted small" }, `Data folders replaced by demo folders: ${[...new Set(st.meta.moved_mounts.map(m => m.from))].join(", ")}`) : null,
      st.meta?.outside_files?.length ? el("div", { class: "small", style: "color:var(--warn)" }, `Builds read these files from outside the project: ${st.meta.outside_files.join(", ")}`) : null,
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
    if (res.note) container.append(el("div", { class: "muted", style: `padding:4px 8px 4px ${8 + depth * 14}px;font-size:12px` }, res.note))
    for (const e of res.entries || []) {
      const isDir = e.type === "dir"
      const mark = markFor(e.path, isDir)
      const row = el("div", { class: `trow${mark && !isDir ? ` ch-${mark === "?" ? "q" : mark}` : ""}${mark && isDir ? " has-ch" : ""}`, role: "treeitem", tabindex: "-1",
        "aria-level": String(depth + 1), "data-path": e.path, "data-type": e.type, style: `padding-left:${8 + depth * 14}px`, title: e.path },
        el("span", { "aria-hidden": "true", style: "width:12px;color:var(--muted)" }, isDir ? "▸" : ""),
        el("span", { "aria-hidden": "true" }, e.submodule ? "📦" : isDir ? "📁" : "📄"),
        el("span", { class: "nm" }, e.name),
        e.submodule ? el("span", { class: "muted", style: "font-size:11px;margin-left:6px" }, e.checked_out ? "component" : "component · not checked out") : null,
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
  const wikiBox = el("div")
  api(apiUrl(`/api/repo/${repoId}/wiki`)).then(r => {
    if (!r.notes?.length) return
    wikiBox.append(el("details", { style: "margin:8px 0" }, el("summary", {}, `Your LLM wiki says (${r.notes.length} note${r.notes.length > 1 ? "s" : ""})`),
      ...r.notes.map(n => el("div", { class: "mem" }, el("span", { class: `badge ${n.status === "draft" ? "warn" : "info"}` }, n.status || "note"),
        el("div", { style: "flex:1;min-width:0" }, el("strong", {}, n.note), el("div", { class: "muted small mono" }, n.path),
          ...n.lines.map(l => el("div", { style: "overflow-wrap:anywhere" }, l)))))))
  }).catch(() => {})
  wrap.append(head, wikiBox, statusEl,
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
      const workBtn = el("button", { class: "btn ghost", title: "Make a task of it: resolve it here, hand it to Hermes, or park it" }, "Work on this")
      workBtn.onclick = () => taskDialog({ repo: r.repo_id, kind: r.safe === false ? "merge" : "fix", dialogTitle: "Work on this request",
        title: (r.safe === false ? "Resolve: " : "") + requestLine(r).slice(0, 180),
        upstream: (r.upstreams || []).find(u => u.conflict)?.name,
        body: (r.upstreams || []).filter(u => (u.conflict_files || []).length).map(u => `Conflicts merging ${u.name}/${u.branch} (${u.conflict_files.length} files):\n`
          + u.conflict_files.slice(0, 30).map(f => `- ${f}`).join("\n") + (u.conflict_files.length > 30 ? "\n- …" : "")).join("\n\n"),
        about: [{ type: "request", id: r.id, label: requestLine(r).slice(0, 60) }] })
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
        autoLine(r),
        (r.result || []).length ? el("div", { class: "muted" }, "Result: " + r.result.map(o => `${o.upstream}: ${o.detail}`).join("; ")) : null,
        (r.result || []).filter(o => o.url).map(o => el("div", {}, el("a", { href: o.url, target: "_blank", rel: "noopener", style: "color:#9fb4ff" }, `Open the pull request: ${o.url}`))),
        (r.result || []).filter(o => o.after_update).map(o => el("div", { style: "color:#e0a060;font-size:13px" }, `Now run for ${o.name}: `, el("code", {}, o.after_update))),
        r.summary ? el("div", { style: "margin:6px 0;font-size:13px" }, el("strong", {}, "Agent's summary: "), r.summary) : null,
        r.workspace ? el("div", { class: "muted", style: "font-size:12px" }, `Resolved in merge workspace ${r.workspace}; approving fast-forwards ${r.focus_branch} to it and removes the workspace.`) : null,
        r.comment ? el("div", { class: "muted" }, `Note: ${r.comment}`) : null,
        pending ? el("div", { style: "display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px" }, ...(reportBtns.length ? reportBtns : [approveBtn]), denyBtn, workBtn, commentIn,
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

// Settings -> Notifications: where alerts go (ntfy, Gotify, a webhook) and which kinds go there.
async function notifyCard() {
  const card = el("div", { class: "repo-card", id: "notifications", style: "margin-top:16px" })
  const n = await api(apiUrl("/api/notify/settings")).catch(() => null)
  if (!n) return card
  const env = new Set(n.from_env || [])
  const field = (k, ph, label) => { const i = el("input", { value: n[k] || "", placeholder: ph, "aria-label": label, style: "width:420px;max-width:100%" }); if (env.has(k)) { i.disabled = true; i.title = "set in .env" }; return el("label", { class: "col", style: "gap:2px" }, label + (env.has(k) ? " (from .env)" : ""), i) }
  const ntfy = field("ntfy_url", "https://ntfy.sh/your-secret-topic", "ntfy topic URL")
  const ntfyTok = el("input", { type: "password", placeholder: n.ntfy_token_set ? "•••• saved" : "ntfy access token (optional)", "aria-label": "ntfy token", style: "width:260px" })
  const gotify = field("gotify_url", "https://gotify.example.com", "Gotify server URL")
  const gotifyTok = el("input", { type: "password", placeholder: n.gotify_token_set ? "•••• saved" : "Gotify application token", "aria-label": "Gotify token", style: "width:260px" })
  const hook = field("webhook_url", "https://example.com/hook (JSON POST)", "Webhook URL")
  const pub = field("public_url", "http://192.168.1.20:5001", "This app's address (for links in notifications)")
  const boxes = Object.entries(n.names).map(([k, label]) => { const i = el("input", { type: "checkbox", style: "height:auto", "aria-label": label }); i.checked = !!n.events[k]; i.dataset.k = k; return i })
  const save = el("button", { class: "btn" }, "Save")
  const test = el("button", { class: "btn ghost" }, "Send a test")
  const val = (w) => w.querySelector("input").value
  save.onclick = () => busy(save, "Saving…", async () => {
    const body = { events: Object.fromEntries(boxes.map(b => [b.dataset.k, b.checked])), ntfy_token: ntfyTok.value, gotify_token: gotifyTok.value }
    for (const [k, w] of [["ntfy_url", ntfy], ["gotify_url", gotify], ["webhook_url", hook], ["public_url", pub]]) if (!env.has(k)) body[k] = val(w)
    const r = await api(apiUrl("/api/notify/settings"), { method: "POST", body: JSON.stringify(body) }).catch(e => ({ ok: false, error: e.message }))
    toast(r.ok ? `Saved. Sending to: ${r.channels.join(", ") || "the feed only"}` : r.error, r.ok ? "ok" : "error")
  })
  test.onclick = () => busy(test, "Sending…", async () => {
    const r = await api(apiUrl("/api/notify/test"), { method: "POST", body: "{}" }).catch(e => ({ ok: false, error: e.message }))
    const sent = (r.sent || []).map(x => `${x.channel}: ${x.ok ? "ok" : (x.error || x.status)}`).join(", ")
    toast(r.ok ? (sent || "In the feed (no channel set up)") : r.error, r.ok && !(r.sent || []).some(x => !x.ok) ? "ok" : "error"); refreshBell()
  })
  card.append(el("h3", { style: "margin-top:0" }, "Notifications"),
    el("p", { class: "muted small" }, "Everything shows in the bell at the top. Set a channel to also get it on your phone: ntfy (free app, any topic name you keep secret), Gotify, or a webhook."),
    el("div", { class: "col", style: "gap:8px" }, el("div", { class: "row", style: "gap:8px;align-items:flex-end;flex-wrap:wrap" }, ntfy, ntfyTok),
      el("div", { class: "row", style: "gap:8px;align-items:flex-end;flex-wrap:wrap" }, gotify, gotifyTok), hook, pub,
      el("div", { class: "muted small" }, "Send to the phone:"),
      el("div", { class: "row", style: "gap:14px;flex-wrap:wrap" }, ...boxes.map(b => el("label", { class: "row small", style: "gap:4px" }, b, n.names[b.dataset.k]))),
      el("div", { class: "row", style: "gap:8px" }, save, test)))
  return card
}

async function renderSettings() {
  const content = $("#content")
  content.innerHTML = ""
  content.append(el("h1", { class: "page", style: "margin-bottom:12px" }, "Settings"))
  const card = el("div", { class: "repo-card", style: "margin-top:16px" }, loading())
  content.append(card)
  notifyCard().then(c => { content.insertBefore(c, card.nextSibling); if (location.hash === "#notifications") c.scrollIntoView() })
  try {
    const local = await api(apiUrl('/api/settings'))
    card.innerHTML = ""
    // What .env sets (UPSTREAM_LLM_*) wins over this page: show it, locked, and say where to change it.
    const envSet = local.llm_env || { values: {}, names: {} }
    const llm = { ...(local.llm || {}), ...envSet.values }
    const fromEnv = (k, input) => {
      if (!envSet.names[k]) return input
      input.disabled = true; input.title = `Set in .env (${envSet.names[k]}); change it there`
      return el("span", { class: "col", style: "gap:2px" }, input, el("span", { class: "muted small" }, `from .env: ${envSet.names[k]}`))
    }
    const urlIn = el("input", { value: llm.base_url || "http://127.0.0.1:8080/v1", placeholder: "http://host:8080/v1", style: "width:420px", "aria-label": "Server URL" })
    // The server only ever sends a masked key ("••••1234"); sending it back unchanged keeps the saved key.
    const keyIn = el("input", { value: llm.api_key || "", type: "password", autocomplete: "off", placeholder: "optional", style: "width:320px", "aria-label": "API key" })
    const fallIn = el("textarea", { rows: "3", style: "width:420px;max-width:100%;font:12px var(--mono)", placeholder: "http://host:8001/v1\nhttp://host:8002/v1",
      "aria-label": "Fallback server URLs" }, (llm.fallback_urls || []).join("\n"))
    const visionIn = el("input", { type: "checkbox", style: "height:auto" }); visionIn.checked = !!llm.vision
    const liveEl = el("div", { class: "muted small" })
    const showLive = async (refresh) => {
      const s = await api(apiUrl(`/api/llm/status${refresh ? "?refresh=1" : ""}`)).catch(() => null)
      if (!s) return
      liveEl.replaceChildren(...Object.entries(s.servers || {}).map(([u, v]) => el("div", {},
        badge(v.up ? (u === s.base_url ? "up" : "") : "bad", u === s.base_url ? "in use" : v.up ? "up" : "down"), ` ${u}`,
        v.up ? el("span", { class: "muted" }, ` · ${v.models.length} model(s)${s.model && v.models.includes(s.model) ? `, serves ${s.model}` : ""}`) : "")))
    }
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
      const fallback_urls = fallIn.value.split("\n").map(u => u.trim()).filter(Boolean)
      const saved = local.llm || {}, mine = (k, v) => envSet.names[k] ? (saved[k] ?? (k === "fallback_urls" ? [] : "")) : v
      const res = await api(apiUrl('/api/settings'), { method: "POST", body: JSON.stringify({ llm: { model: mine("model", modelSel.value), base_url: mine("base_url", urlIn.value.trim()),
        api_key: mine("api_key", keyIn.value), fallback_urls: mine("fallback_urls", fallback_urls), vision: visionIn.checked } }) })
      keyIn.value = res.settings?.llm?.api_key || ""
      toast("Model connection saved", "ok")
      await showLive(true); refreshModelStatus()
    })
    card.append(
      el("div", { style: "display:flex;flex-direction:column;gap:12px" },
        el("div", { class: "muted" }, "Any OpenAI-compatible server (llama-swap, llama-server, Argos). Independent of hermes-agent."),
        Object.keys(envSet.names).length ? el("div", { class: "muted small" }, "Locked fields come from the .env file, which wins over this page. Edit .env and restart to change them; the rest is saved here.") : null,
        el("label", { style: "display:flex;flex-direction:column;gap:4px" }, "Server URL", fromEnv("base_url", urlIn)),
        el("label", { style: "display:flex;flex-direction:column;gap:4px" }, "API key", fromEnv("api_key", keyIn)),
        el("label", { style: "display:flex;flex-direction:column;gap:4px" }, "Fallback URLs (one per line; used when the server above is down, e.g. on-demand llama.cpp ports)", fromEnv("fallback_urls", fallIn)),
        liveEl,
        el("label", { style: "display:flex;flex-direction:column;gap:4px" }, "Model", fromEnv("model", modelSel)),
        modelStatus,
        el("label", { class: "row", style: "gap:6px" }, visionIn, "This model can see images (send attached pictures, e.g. from References, as images)"),
        el("div", { style: "display:flex;gap:8px;flex-wrap:wrap" }, refreshBtn, saveBtn)
      )
    )
    loadModels()
    showLive(false)
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
      accRow("forge_url", "Home forge address (Forgejo or Gitea), as this machine reaches it", "text", "http://forgejo:3000"),
      accRow("forge_token", "Home forge token (write:repository, write:issue, read:user)", "password", "token from the forge's Settings → Applications"),
      accRow("forge_default_home", "Default home: forge (a repo with a forge remote pushes there unless you pick another), or blank for origin", "text", "forge"),
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
      if (!confirm("Remove the saved GitHub token, forge address and token, and Docker Hub login? (Environment variables are not affected.)")) return
      return busy(removeBtn, "Removing…", async () => {
        await api(apiUrl('/api/credentials'), { method: "POST", body: JSON.stringify({ github_token: "", forge_url: "", forge_token: "", forge_default_home: "", dockerhub_username: "", dockerhub_token: "" }) })
        toast("Removed", "ok"); await renderSettings()
      })
    }
    card.append(el("h4", { style: "margin-top:20px" }, "Accounts"),
      el("div", { class: "muted" }, "Used to push branches, open pull requests, set Actions secrets and set up mirrors. Each token goes only to its own host. Environment variables UPSTREAM_GITHUB_TOKEN, UPSTREAM_FORGE_URL, UPSTREAM_FORGE_TOKEN, UPSTREAM_DOCKERHUB_USERNAME and UPSTREAM_DOCKERHUB_TOKEN override these; UPSTREAM_FORGE_HOSTS lists other addresses of the same forge."),
      el("div", { style: "display:flex;flex-direction:column;gap:12px;margin-top:8px" }, ...accRows.map(r => r.row),
        el("div", { style: "display:flex;gap:8px;flex-wrap:wrap" }, accSave, testBtn("github", "Test GitHub"), testBtn("forge", "Test forge"), testBtn("dockerhub", "Test Docker Hub"), removeBtn)))
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
    card.append(await wikiSetup())
    card.append(await integrationsSetup())
    card.append(await storageSetup())
  } catch (e) { card.replaceChildren(el("p", { class: "muted" }, `Could not load settings: ${e.message}`)) }
}

// ------------------------------------------------------------------ Work: tasks for the agent
// A task is a piece of work about a repo (fix a commit, a bug, a change, an idea). It is worked on
// here (a chat plus its own branch), handed to Hermes for bigger jobs, or parked for later in the
// LLM wiki or Pondering Orb. See tasks.py.
const KIND_LABEL = { fix: "Fix", bug: "Bug", feature: "Change", chore: "Chore", merge: "Merge", idea: "Idea" }
const WHERE_LABEL = { here: "Worked on here", hermes: "With Hermes", herdr: "With a herdr agent", studio: "With Hermes Studio", wiki: "Parked in the wiki", orb: "Parked in Pondering Orb" }
const STATUS_CLS = { open: "warn", working: "info", parked: "", done: "ok", dropped: "" }
let integrations = null
async function loadIntegrations(force = false) {
  if (!integrations || force) integrations = await api(apiUrl("/api/integrations")).catch(() => ({ hermes: {}, orb: {}, wiki: {} }))
  return integrations
}
const aboutIcon = { commit: "◆", request: "✓", file: "▤", component: "📦", pr: "⇄", push: "↑", conflict: "⚠" }

// Open the task dialog, prefilled from where you started ("Work on" a commit, a request, a chat...).
// Upstream Manager suggests where the task should go (and why); you take the suggestion or pick another.
const DEST = {
  here: { label: "Work on it here", busy: "Making its branch…", why: "A chat about it, on its own branch" },
  herdr: { label: "Send to herdr", busy: "Starting the agent…", why: "Start a coding agent in herdr on the task's branch" },
  studio: { label: "Send to Hermes Studio", busy: "Starting Studio…", why: "A Studio run on the task's branch; follow it in Studio" },
  hermes: { label: "Hand to Hermes", busy: "Starting Hermes…", why: "A Hermes session on the task's branch" },
  wiki: { label: "Park in LLM wiki", busy: "Parking…", why: "A note in the wiki's inbox for later" },
  orb: { label: "Park in Pondering Orb", busy: "Parking…", why: "A to-do in Orb" },
}
// Which herdr agent gets the task: a new one of some kind, or one that is already running.
function herdrPicker(ints, current) {
  const h = ints.herdr || {}
  const sel = el("select", { "aria-label": "Which herdr agent" })
  const places = ["", ...(h.machines || []).map(m => m.label)]   // "" is this machine
  const here = (m) => m ? `on ${m}` : places.length > 1 ? "on this machine" : ""
  const dflt = `new:${h.machine || ""}:${h.kind || "hermes"}`
  const fill = (live) => {
    const keep = sel.value
    sel.replaceChildren(
      ...places.map(m => el("optgroup", { label: `Start a new agent ${here(m)}`.trim() }, ...(h.kinds || [h.kind || "hermes"]).map(k =>
        el("option", { value: `new:${m}:${k}` }, `new ${k}${m ? ` on ${m}` : ""}${k === h.kind && m === (h.machine || "") ? " (default)" : ""}${!m && h.installed && !h.installed.includes(k) ? " · not found here" : ""}`)))),
      live.length ? el("optgroup", { label: "Give it to a running agent" }, ...live.filter(a => a.name).map(a =>
        el("option", { value: `agent:${a.machine || ""}:${a.name}` }, `${a.name} · ${a.kind || "agent"} · ${a.status}${a.machine ? ` · on ${a.machine}` : ""}${a.task ? ` · on “${a.task.title.slice(0, 30)}”` : ""}`))) : el("optgroup", { label: loaded ? "No other agents are running in herdr" : "Looking for running agents…" }))
    sel.value = [...sel.options].some(o => o.value === keep) ? keep : dflt
  }
  let loaded = !h.ok
  fill([])
  if (h.ok) api(apiUrl("/api/herdr/agents")).then(r => { loaded = true; fill((r.items || []).filter(a => a.name !== current)) }).catch(() => { loaded = true; fill([]) })
  const row = el("label", { class: "row small muted", style: "gap:6px;align-items:center", hidden: h.ok ? null : "" }, "herdr agent:", sel)
  return { row, value: () => { const [what, machine, ...rest] = sel.value.split(":"); const name = rest.join(":")
    return what === "agent" ? { agent: name, machine } : { agent_kind: name, machine } } }
}

let drawDests = null
// Import GitHub or forge issues as Work tasks: pick a remote and the issues; ones already imported link to their task.
async function issuesDialog(repoId) {
  const body = el("div", { class: "col", style: "gap:10px;min-width:min(620px,86vw)" }, loading("Asking GitHub…"))
  let close = openDialog("Import issues", body), remote = "", state = "open"
  const draw = async () => {
    body.replaceChildren(loading("Fetching issues…"))
    const r = await api(apiUrl(`/api/repo/${repoId}/issues?state=${state}${remote ? `&remote=${encodeURIComponent(remote)}` : ""}`)).catch(e => ({ ok: false, error: e.message }))
    const remSel = el("select", { "aria-label": "Remote" }, ...(r.remotes || []).map(x => el("option", { value: x.remote }, `${x.remote} · ${x.slug}${x.forge === "forge" ? " (forge)" : ""}`)))
    remSel.value = r.source?.remote || remote; remSel.onchange = () => { remote = remSel.value; draw() }
    const stSel = el("select", { "aria-label": "Which issues" }, ...["open", "closed", "all"].map(x => el("option", { value: x }, x)))
    stSel.value = state; stSel.onchange = () => { state = stSel.value; draw() }
    const head = el("div", { class: "row", style: "gap:8px" }, (r.remotes || []).length ? remSel : null, stSel)
    if (!r.ok) return body.replaceChildren(head, el("p", { class: "muted" }, r.error))
    if (!r.items.length) return body.replaceChildren(head, el("p", { class: "muted" }, `No ${state === "all" ? "" : state + " "}issues on ${r.source.slug}.`))
    const boxes = []
    const list = el("div", { class: "col", style: "gap:2px;max-height:52vh;overflow:auto" }, ...r.items.map(i => {
      const cb = el("input", { type: "checkbox", style: "height:auto", "aria-label": `Import #${i.number}` }); cb.disabled = !!i.task; cb.dataset.n = i.number
      if (!i.task) boxes.push(cb)
      return el("label", { class: "row", style: "gap:8px;padding:4px 2px;align-items:flex-start" }, cb,
        el("span", { style: "flex:1" }, el("strong", {}, `#${i.number} `), i.title,
          ...(i.labels || []).map(lb => el("span", { class: "badge", style: "margin-left:4px" }, lb)),
          el("div", { class: "muted small" }, `${i.user || ""} · ${(i.updated_at || "").slice(0, 10)} · ${i.comments} comment(s)`)),
        i.task ? el("button", { class: "btn ghost small", onclick: (e) => { e.preventDefault(); close(); route(`/work?task=${i.task}`) } }, "Its task") :
          el("a", { href: i.url, target: "_blank", rel: "noopener", class: "small" }, "GitHub"))
    }))
    const go = el("button", { class: "btn" }, "Make tasks")
    go.onclick = () => busy(go, "Importing…", async () => {
      const numbers = boxes.filter(b => b.checked).map(b => Number(b.dataset.n))
      if (!numbers.length) return toast("Tick the issues to import", "error")
      const res = await api(apiUrl(`/api/repo/${repoId}/issues/import`), { method: "POST", body: JSON.stringify({ numbers, remote: r.source.remote }) }).catch(e => ({ ok: false, error: e.message }))
      if (!res.ok) return toast(res.error, "error")
      toast(`${res.created.length} task(s) made` + (res.skipped.length ? `, ${res.skipped.length} skipped` : ""), "ok")
      close(); route(`/work?repo=${encodeURIComponent(repoId)}`)
    })
    body.replaceChildren(head, el("div", { class: "muted small" }, `${r.source.slug}: tick issues to make Work tasks of them (title, text and a link back).`), list, el("div", {}, go))
  }
  draw()
}

async function taskDialog(pre = {}) {
  const ints = await loadIntegrations()
  loadIntegrations(true).then(fresh => { Object.assign(ints, fresh); drawDests?.() }).catch(() => {})
  const repos = await api(apiUrl("/api/repos")).catch(() => [])
  const list = Array.isArray(repos) ? repos : repos.repos || []
  const repoSel = el("select", { "aria-label": "Repository" }, el("option", { value: GENERAL }, "No particular repo"), ...list.map(r => el("option", { value: r.id }, r.name || r.id)))
  repoSel.value = pre.repo || GENERAL
  const ontoSel = el("select", { "aria-label": "Branch to work on" })
  const loadBranches = async () => {
    ontoSel.replaceChildren()
    if (repoSel.value === GENERAL) { ontoSel.disabled = true; ontoSel.append(el("option", { value: "" }, "—")); return }
    ontoSel.disabled = false
    const repo = list.find(r => r.id === repoSel.value) || {}
    const bs = await api(apiUrl(`/api/repo/${repoSel.value}/branches`)).catch(() => ({}))
    const names = (bs.branches || bs.items || []).map(b => typeof b === "string" ? b : b.name).filter(b => b && !b.startsWith("upkeep/"))
    ontoSel.append(el("option", { value: "" }, `${repo.focus_branch || "focus branch"} (focus)`), ...names.filter(b => b !== repo.focus_branch).map(b => el("option", { value: b }, b)))
    ontoSel.value = pre.onto || ""
  }
  const title = el("input", { value: pre.title || "", placeholder: "What needs doing, in a line", "aria-label": "Title", style: "width:100%" })
  const kind = el("select", { "aria-label": "Kind" }, ...Object.entries(KIND_LABEL).map(([k, v]) => el("option", { value: k }, v)))
  kind.value = pre.kind || "fix"
  const body = el("textarea", { rows: "6", placeholder: "Details: what's wrong, how to reproduce, what you want instead…", "aria-label": "Details", style: "width:100%" })
  body.value = pre.body || ""
  const about = [...(pre.about || [])]
  const chips = el("div", { class: "row", style: "flex-wrap:wrap;gap:6px" })
  const drawChips = () => chips.replaceChildren(...about.map((a, i) => el("span", { class: "chip" }, `${aboutIcon[a.type] || ""} ${a.label}`,
    el("button", { class: "chip-x", "aria-label": `Remove ${a.label}`, onclick: () => { about.splice(i, 1); drawChips() } }, "×"))),
    ...(about.length ? [] : [el("span", { class: "muted small" }, "Not linked to a commit or request.")]))
  drawChips()
  let close
  const send = (btn, to) => busy(btn, DEST[to]?.busy || "Saving…", async () => {
    if (!title.value.trim()) { title.focus(); return toast("Give the task a title", "error") }
    const res = await api(apiUrl("/api/tasks"), { method: "POST", body: JSON.stringify({ repo: repoSel.value, title: title.value, body: body.value, kind: kind.value, about,
      upstream: pre.upstream, onto: ontoSel.value || undefined, to: to || undefined, ...(to === "herdr" ? herdrPick.value() : {}) }) })
    if (!res.ok && !res.task) return toast(res.error, "error")
    close(); refreshWorkCount()
    if (!res.ok) { toast(`Saved the task, but: ${res.error}`, "error"); return route(`/work?task=${res.task.id}`) }
    afterSend(res, to)
  })
  const dests = el("div", { class: "dest-grid" })
  const herdrPick = herdrPicker(ints)
  const suggestLine = el("div", { class: "suggest" })
  let suggested = null
  drawDests = () => {
    dests.replaceChildren(...Object.entries(DEST).map(([k, d]) => {
      const st = k === "here" ? { ok: true } : ints[k] || {}
      const b = el("button", { class: `btn ${k === suggested ? "" : "ghost"} dest${st.ok ? "" : " dim"}`, title: st.ok ? d.why : `${d.label.replace(/^(Send to|Park in|Hand to) /, "")}: ${st.error || "not set up"} (Settings)` }, d.label)
      b.onclick = () => st.ok ? send(b, k) : toast(`${d.label}: ${st.error || "not set up"}. Set it up in Settings.`, "error")
      return b
    }))
  }
  let sugTimer
  const askSuggestion = () => { clearTimeout(sugTimer); sugTimer = setTimeout(async () => {
    const r = await api(apiUrl("/api/tasks/suggest"), { method: "POST", body: JSON.stringify({ repo: repoSel.value, title: title.value, body: body.value, kind: kind.value, about }) }).catch(() => null)
    if (!r) return
    suggested = r.to; drawDests()
    suggestLine.replaceChildren(el("span", {}, "Suggested: "), el("strong", {}, DEST[r.to].label), el("span", { class: "muted" }, ` — ${r.why}. `),
      el("span", { class: "muted small" }, "Take it, or pick another."))
  }, 250) }
  for (const x of [title, body]) x.addEventListener("input", askSuggestion)
  kind.onchange = askSuggestion
  repoSel.onchange = () => { loadBranches(); askSuggestion() }
  drawDests(); askSuggestion(); loadBranches()
  close = openDialog(pre.dialogTitle || "New task",
    el("div", { class: "col", style: "gap:10px;min-width:min(620px,80vw)" },
      el("div", { class: "row", style: "gap:8px;flex-wrap:wrap" }, el("label", { class: "col", style: "gap:4px;flex:1;min-width:160px" }, "Repo", repoSel),
        el("label", { class: "col", style: "gap:4px;min-width:150px" }, "Work on branch", ontoSel), el("label", { class: "col", style: "gap:4px" }, "Kind", kind)),
      el("label", { class: "col", style: "gap:4px" }, "Title", title),
      el("label", { class: "col", style: "gap:4px" }, "Details", body),
      el("div", { class: "col", style: "gap:4px" }, el("span", {}, "About"), chips),
      suggestLine, dests, herdrPick.row,
      el("div", { class: "row", style: "gap:8px;align-items:center" }, (() => { const b = el("button", { class: "btn ghost small" }, "Just save it"); b.onclick = () => send(b, null); return b })(),
        el("span", { class: "muted small" }, "Code work happens on the task's own branch; nothing reaches the branch you picked until you approve it."))))
  setTimeout(() => title.focus(), 0)
}

function afterSend(res, to) {
  const t = res.task
  if (to === "here") {
    try { sessionStorage.setItem(`draft:${res.where.session}`, res.draft || "") } catch {}
    toast(res.where.workspace ? `Branch upkeep/${res.where.workspace} is ready for this task` : "Chat opened for this task", "ok")
    return route(`/repo/${t.repo_id}?session=${res.where.session}`)
  }
  if (["hermes", "herdr", "studio"].includes(to)) { toast(`${DEST[to].label.replace(/^(Send to|Hand to) /, "")} is on it`, "ok"); return route(`/work?task=${t.id}`) }
  if (to === "wiki") { toast(`Parked in the LLM wiki: ${res.where.note}`, "ok"); return onWork() && route(`/work?task=${t.id}`) }
  if (to === "orb") { toast(`Parked in Pondering Orb (${res.where.list})`, "ok"); return onWork() && route(`/work?task=${t.id}`) }
  toast("Task saved", "ok")
  route(`/work?task=${t.id}`)   // show it where it now lives, ready to send somewhere
}
const onWork = () => currentView()[0] === "work"

async function refreshWorkCount() {
  const c = document.getElementById("work-count")
  if (!c) return
  const r = await api(apiUrl("/api/tasks")).catch(() => ({ items: [] }))
  const n = (r.items || []).filter(t => t.status === "open" || t.status === "working").length
  c.textContent = n ? String(n) : ""
}

function taskRow(t, onPick, selected) {
  const where = t.where?.type
  const running = t.hermes_run?.running
  const ag = { blocked: ["warn", "needs you"], working: ["info", "agent working"], done: ["ok", "agent done"], idle: ["", "agent idle"], gone: ["bad", "agent gone"] }[t.agent_status]
  return el("button", { class: `task-row${selected ? " sel" : ""}`, onclick: () => onPick(t), "aria-current": selected ? "true" : null },
    el("div", { class: "row", style: "gap:6px;align-items:center;min-width:0" },
      badge(STATUS_CLS[t.status] || "", t.status), ag ? badge(ag[0], ag[1]) : null,
      t.priority && t.priority !== "normal" ? badge(t.priority === "urgent" ? "bad" : t.priority === "high" ? "warn" : "", t.priority) : null,
      t.claimed_by && t.claimed_by.until > new Date().toISOString() ? badge("info", `🤖 ${t.claimed_by.agent}`) : null,
      el("span", { class: "muted small" }, KIND_LABEL[t.kind] || t.kind),
      el("strong", { style: "overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0" }, t.title)),
    el("div", { class: "muted small", style: "text-align:left" }, `${t.repo_name}${t.onto ? ` → ${t.onto}` : ""} · ${where ? WHERE_LABEL[where] : "not started"}${running ? " · working…" : ""} · ${t.updated_at.slice(0, 16).replace("T", " ")}`))
}

async function renderWork() {
  const params = new URLSearchParams(location.search)
  const main = $("#content")
  let filter = store_.get("workFilter", "active"), repoF = params.get("repo") || "", picked = params.get("task")
  const listBox = el("div", { class: "task-list", role: "list" }), detail = el("div", { class: "task-detail panel" })
  const repoSel = el("select", { "aria-label": "Repo" }, el("option", { value: "" }, "All repos"))
  const repos = await api(apiUrl("/api/repos")).catch(() => [])
  for (const r of (Array.isArray(repos) ? repos : repos.repos || [])) repoSel.append(el("option", { value: r.id }, r.name || r.id))
  repoSel.value = repoF
  const filters = { active: "Active", parked: "Parked", done: "Done", all: "All" }
  const fbar = el("div", { class: "tabs", role: "tablist", "aria-label": "Which tasks" })
  const draw = async () => {
    const r = await api(apiUrl(`/api/tasks${repoF ? `?repo=${encodeURIComponent(repoF)}` : ""}`))
    const items = (r.items || []).filter(t => filter === "all" || (filter === "active" ? ["open", "working"].includes(t.status) : filter === "done" ? ["done", "dropped"].includes(t.status) : t.status === filter))
    fbar.replaceChildren(...Object.entries(filters).map(([k, v]) => el("button", { class: `tab${k === filter ? " active" : ""}`, role: "tab", "aria-selected": String(k === filter),
      onclick: () => { filter = k; store_.set("workFilter", k); draw() } }, v)))
    listBox.replaceChildren(...items.map(t => taskRow(t, pick, t.id === picked)))
    if (!items.length) listBox.append(el("p", { class: "muted", style: "padding:12px" }, filter === "active"
      ? "Nothing on the go. Start a task from a commit (“Work on”), a request, a chat, or with New task." : "No tasks here."))
    const cur = (r.items || []).find(t => t.id === picked)
    if (cur) await taskDetail(detail, cur, draw); else detail.replaceChildren(el("p", { class: "muted", style: "padding:16px" }, "Pick a task to see it, work on it, or hand it on."))
  }
  const pick = (t) => { picked = t.id; history.replaceState({}, "", `${BASE}/work?task=${t.id}${repoF ? `&repo=${repoF}` : ""}`); draw() }
  // herdr's live agents: what each is doing and which task it has
  const roster = el("div", { class: "panel", style: "padding:8px 10px", hidden: "" })
  const drawRoster = async () => {
    if (!roster.isConnected) return
    const r = await api(apiUrl("/api/herdr/agents")).catch(() => ({ items: [] }))
    const live = (r.items || []).filter(a => a.name)
    roster.hidden = !(await loadIntegrations()).herdr?.ok
    const cls = { working: "info", blocked: "warn", done: "ok", idle: "", unknown: "" }
    roster.replaceChildren(el("div", { class: "row", style: "gap:6px;align-items:center;margin-bottom:4px" }, el("strong", { class: "small" }, "Agents in herdr"), el("span", { class: "muted small" }, r.error || `${live.length} running`)),
      ...live.map(a => el("button", { class: "file-row", title: a.cwd || "", onclick: () => a.task ? pick({ id: a.task.id }) : toast(`${a.name} isn't on a task from here. Pick it in “herdr agent” when you send one.`, "info") },
        badge(cls[a.status] ?? "", a.status === "blocked" ? "needs you" : a.status), el("span", { class: "nm", style: "flex:0 1 auto;min-width:90px", title: a.name }, a.name), a.machine ? badge("", a.machine, "runs on this herdr machine") : null, el("span", { class: "muted small", style: "flex:1 1 0;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" }, ` ${a.kind || ""}${a.task ? ` · ${a.task.title.slice(0, 40)}` : " · free"}`))),
      ...(live.length ? [] : [el("div", { class: "muted small" }, "None yet. Send a task to herdr and its agent shows here.")]))
    setTimeout(drawRoster, 8000)
  }
  repoSel.onchange = () => { repoF = repoSel.value; draw() }
  const newBtn = el("button", { class: "btn", onclick: () => taskDialog({ repo: repoF || undefined }) }, "+ New task")
  const issuesBtn = el("button", { class: "btn ghost", title: "Make tasks of a repo's GitHub or forge issues", onclick: () => repoF ? issuesDialog(repoF) : toast("Pick a repo first (the list above)", "info") }, "Import issues")
  let view = picked ? "list" : params.get("view") === "board" ? "board" : params.get("view") === "list" ? "list" : store_.get("workView", "list")
  const viewBar = el("div", { class: "tabs", role: "tablist", "aria-label": "View" })
  const grid = el("div", { class: "work-grid" }, el("div", { class: "col", style: "gap:12px;min-width:0" }, el("div", { class: "panel", style: "min-width:0" }, fbar, listBox), roster), detail)
  const boardHost = el("div")
  const showView = async () => {
    viewBar.replaceChildren(...[["list", "List"], ["board", "Board"]].map(([k, v]) => el("button", { class: `tab${k === view ? " active" : ""}`, role: "tab", "aria-selected": String(k === view),
      onclick: () => { view = k; store_.set("workView", k); showView() } }, v)))
    grid.hidden = view !== "list"; boardHost.hidden = view !== "board"
    if (view === "board") await renderBoard(boardHost, repoF, (t) => { view = "list"; picked = t.id; history.replaceState({}, "", `${BASE}/work?task=${t.id}`); showView() })
    else await draw()
  }
  const origPick = pick
  repoSel.onchange = () => { repoF = repoSel.value; showView() }
  main.replaceChildren(
    el("div", { class: "row", style: "margin-bottom:12px;gap:10px;flex-wrap:wrap;align-items:center" }, el("h1", { class: "page", style: "margin:0" }, "Work"),
      el("span", { class: "muted" }, "Fixes, bugs and changes for your coding agents: work on them here, send them to herdr or Studio, or park them."), el("div", { class: "spacer" }), viewBar, repoSel, issuesBtn, newBtn),
    grid, boardHost)
  void origPick
  await showView()
  drawRoster()
}

async function taskDetail(box, t, redraw) {
  stopAgentPoll()
  const ints = await loadIntegrations()
  const act = (label, fn, cls = "ghost") => { const b = el("button", { class: `btn ${cls} small` }, label); b.onclick = () => busy(b, null, fn); return b }
  const send = (to, message) => async () => {
    const st = to === "here" ? { ok: true } : ints[to] || {}
    if (!st.ok) return toast(`${DEST[to].label}: ${st.error || "not set up"}. Set it up in Settings.`, "error")
    const res = await api(apiUrl(`/api/tasks/${t.id}/send`), { method: "POST", body: JSON.stringify({ to, message }) }).catch(e => ({ ok: false, error: e.message }))
    if (!res.ok) return toast(res.error, "error")
    refreshWorkCount()
    if (to === "here") return afterSend(res, to)
    if (!["hermes", "herdr", "studio"].includes(to)) afterSend(res, to)
    await redraw()
  }
  const setStatus = (status) => async () => { await api(apiUrl(`/api/tasks/${t.id}`), { method: "POST", body: JSON.stringify({ status }) }); refreshWorkCount(); await redraw() }
  const where = t.where || {}
  const aboutChips = el("div", { class: "row", style: "flex-wrap:wrap;gap:6px" }, ...(t.about || []).map(a => {
    const chip = el("button", { class: "chip", title: `${a.type}: ${a.id}` }, `${aboutIcon[a.type] || ""} ${a.label}`)
    chip.onclick = () => a.type === "commit" ? showCommit(t.repo_id, a.id) : a.type === "request" ? route("/requests") : a.url ? open(a.url, "_blank", "noopener") : null
    return chip
  }))
  // where it should go, when it hasn't gone anywhere yet: agree, or pick another
  let suggestion = null
  if (!where.type && t.status === "open") {
    const sg = await api(apiUrl(`/api/tasks/${t.id}/suggest`)).catch(() => null)
    if (sg) suggestion = el("div", { class: "suggest panel", style: "padding:10px" },
      el("div", {}, el("span", {}, `${t.by === "agent" ? "An agent wrote this down. " : ""}Suggested: `), el("strong", {}, DEST[sg.to].label), el("span", { class: "muted" }, ` — ${sg.why}`)),
      el("div", { class: "row", style: "gap:6px;margin-top:6px" }, act("Agree", send(sg.to), ""), el("span", { class: "muted small" }, "or pick another below")))
  }
  const pickHerdr = async () => {
    if (!ints.herdr?.ok) return toast(`herdr: ${ints.herdr?.error || "not set up"}. Set it up in Settings.`, "error")
    const hp = herdrPicker(ints, where.type === "herdr" ? where.agent : null)
    const go = el("button", { class: "btn" }, "Send")
    let close
    go.onclick = () => busy(go, "Starting the agent…", async () => {
      const res = await api(apiUrl(`/api/tasks/${t.id}/send`), { method: "POST", body: JSON.stringify({ to: "herdr", ...hp.value() }) }).catch(e => ({ ok: false, error: e.message }))
      if (!res.ok) return toast(res.error, "error")
      close(); refreshWorkCount(); await redraw()
    })
    hp.row.hidden = false
    close = openDialog(where.type === "herdr" ? "Give this task to another herdr agent" : "Send this task to herdr", el("div", { class: "col", style: "gap:10px;min-width:min(440px,80vw)" },
      el("p", { class: "muted", style: "margin:0" }, "A new agent opens in its own herdr workspace, in the task's branch folder. A running agent is told where that folder is."), hp.row, el("div", {}, go)))
  }
  const destBtns = ["here", "herdr", "studio", "hermes", "wiki", "orb"].filter(k => !(where.type === k && !["here", "herdr"].includes(k)))
    .map(k => k === "here" && where.type === "here" ? act("Open its chat", async () => route(`/repo/${t.repo_id}?session=${where.session}`), "")
      : k === "herdr" ? act(where.type === "herdr" ? "Another herdr agent…" : DEST.herdr.label, pickHerdr) : act(DEST[k].label, send(k)))
  const actions = el("div", { class: "row", style: "gap:6px;flex-wrap:wrap" }, ...destBtns,
    el("div", { class: "spacer" }),
    t.status === "done" ? act("Reopen", setStatus("open")) : act("Mark done", setStatus("done")),
    t.status !== "dropped" && t.status !== "done" ? act("Drop", setStatus("dropped")) : null,
    menu("More", [{ label: "Delete task", danger: true, run: async () => { if (!confirm("Delete this task? Anything it started (a branch, an agent session, a parked note) stays.")) return
      await api(apiUrl(`/api/tasks/${t.id}`), { method: "DELETE", body: "{}" }); refreshWorkCount(); history.replaceState({}, "", `${BASE}/work`); renderWork() } }]))
  const whereLine = where.type ? el("div", { class: "small", style: "color:var(--info)" }, WHERE_LABEL[where.type],
    where.type === "herdr" ? ` · agent ${where.agent} (${where.kind})` : "",
    where.workspace ? ` · branch upkeep/${where.workspace}` : "", t.onto ? ` → ${t.onto}` : "", where.type === "wiki" ? ` · ${where.note}` : "",
    (where.type === "orb" || where.type === "studio") && where.url ? el("a", { href: where.url, target: "_blank", rel: "noopener", style: "margin-left:6px" }, where.type === "studio" ? "open in Studio" : "open in Orb") : null) : null
  const bodyBox = el("div", { class: "md task-body", style: "margin-top:4px" }); if (t.body) bodyBox.innerHTML = renderMarkdown(t.body); else { bodyBox.textContent = "No details."; bodyBox.classList.add("muted") }
  const log = el("details", {}, el("summary", { class: "muted small" }, `History (${t.log.length})`),
    el("ul", { class: "small muted", style: "margin:4px 0 0 18px;padding:0" }, ...t.log.slice().reverse().map(l => el("li", {}, `${l.at.slice(0, 16).replace("T", " ")} · ${l.text}`))))
  const parts = [el("div", { class: "row", style: "gap:8px;align-items:center;flex-wrap:wrap" }, badge(STATUS_CLS[t.status] || "", t.status), el("span", { class: "muted" }, `${KIND_LABEL[t.kind]} · ${t.repo_name}${t.by === "agent" ? " · written down by an agent" : ""}`)),
    el("h2", { style: "margin:6px 0" }, t.title), whereLine, aboutChips, bodyBox, suggestion, actions]
  const agentBox = ["hermes", "herdr", "studio"].includes(where.type) ? agentPanel(t) : null
  const branchBox = where.workspace ? branchPanel(t, redraw) : null
  box.replaceChildren(el("div", { class: "col", style: "gap:10px;padding:14px" }, ...parts,
    agentBox || branchBox ? el("div", { class: "agent-grid" }, agentBox, branchBox) : null, taskTalk(t, redraw), log))
}

// Priority, who holds it, and the task's thread (agents hand over and ask here; you answer here).
function taskTalk(t, redraw) {
  const prio = el("select", { "aria-label": "Priority" }, ...["urgent", "high", "normal", "low"].map(p => el("option", { value: p }, p)))
  prio.value = t.priority || "normal"
  prio.onchange = async () => { await api(apiUrl(`/api/tasks/${t.id}/priority`), { method: "POST", body: JSON.stringify({ priority: prio.value }) }); toast(`Priority: ${prio.value}`, "ok"); await redraw() }
  const c = t.claimed_by && t.claimed_by.until > new Date().toISOString() ? t.claimed_by : null
  const thread = el("div", { class: "col", style: "gap:6px" }, ...(t.comments || []).map(m => el("div", { class: "panel", style: "padding:6px 8px" },
    el("div", { class: "small muted" }, `${m.by}${m.to ? ` → ${m.to}` : ""} · ${m.at.slice(0, 16).replace("T", " ")}`), el("div", { style: "white-space:pre-wrap" }, m.text))))
  const input = el("textarea", { rows: "2", placeholder: "Write on this task (agents read it; @name: … reaches one agent)", "aria-label": "Comment", style: "width:100%" })
  const post = el("button", { class: "btn small" }, "Comment")
  post.onclick = () => busy(post, "Posting…", async () => {
    const text = input.value.trim(); if (!text) return
    const m = text.match(/^@([A-Za-z0-9][A-Za-z0-9._:@/-]*)[:,]?\s+/)
    await api(apiUrl(`/api/tasks/${t.id}/comments`), { method: "POST", body: JSON.stringify({ text: m ? text.slice(m[0].length) : text, to: m ? m[1] : null }) })
    await redraw()
  })
  return el("div", { class: "col", style: "gap:6px" },
    el("div", { class: "row", style: "gap:8px;align-items:center;flex-wrap:wrap" }, el("label", { class: "row small", style: "gap:4px" }, "Priority", prio),
      c ? badge("info", `held by ${c.agent} until ${c.until.slice(11, 16)} UTC`) : el("span", { class: "muted small" }, "nobody holds it"),
      t.why?.length ? el("span", { class: "muted small", title: t.why.join("; ") }, `Overseer score ${t.score}`) : null),
    el("strong", { class: "small" }, `Thread (${(t.comments || []).length})`), thread, input, el("div", {}, post))
}

// ---- the kanban: Work as a board. Drag a card to another column, or up and down to reorder it.
async function renderBoard(host, repoF, onPick) {
  const r = await api(apiUrl(`/api/board${repoF ? `?repo=${encodeURIComponent(repoF)}` : ""}`)).catch(e => ({ columns: {}, error: e.message }))
  const names = { open: "Open", working: "Working", parked: "Parked", done: "Done" }
  const cols = Object.entries(names).map(([k, label]) => {
    const cards = (r.columns?.[k] || [])
    const col = el("div", { class: "kcol", "data-status": k }, el("h3", {}, el("span", {}, label), el("span", { class: "muted" }, String(cards.length))))
    for (const c of cards) {
      const card = el("div", { class: `kcard prio-${c.priority || "normal"}`, draggable: "true", tabindex: "0", "data-id": c.id, "data-rank": c.rank ?? "" ,
        onclick: () => onPick({ id: c.id }), onkeydown: (e) => { if (e.key === "Enter") onPick({ id: c.id }) } },
        el("div", { class: "t" }, c.title),
        el("div", { class: "m" }, c.priority && c.priority !== "normal" ? badge(c.priority === "urgent" ? "bad" : c.priority === "high" ? "warn" : "", c.priority) : null,
          el("span", {}, KIND_LABEL[c.kind] || c.kind), el("span", {}, `· ${c.repo_id}`),
          c.claimed_by ? badge("info", `🤖 ${c.claimed_by.agent}`) : c.where?.type ? badge("", c.where.type) : null,
          c.comments ? el("span", { title: c.last_comment ? `${c.last_comment.by}: ${c.last_comment.text}` : "" }, `💬 ${c.comments}`) : null,
          c.by === "overseer" ? el("span", { title: "made by the Overseer" }, "🧭") : null))
      card.ondragstart = (e) => { e.dataTransfer.setData("text/x-task", c.id); card.style.opacity = ".5" }
      card.ondragend = () => { card.style.opacity = "" }
      col.append(card)
    }
    col.ondragover = (e) => { if (e.dataTransfer.types.includes("text/x-task")) { e.preventDefault(); col.classList.add("drop") } }
    col.ondragleave = () => col.classList.remove("drop")
    col.ondrop = async (e) => {
      e.preventDefault(); col.classList.remove("drop")
      const id = e.dataTransfer.getData("text/x-task"); if (!id) return
      // the new place: between the cards above and below where it was dropped
      const others = [...col.querySelectorAll(".kcard")].filter(x => x.dataset.id !== id)
      const below = others.find(x => e.clientY < x.getBoundingClientRect().top + x.offsetHeight / 2)
      const idx = below ? others.indexOf(below) : others.length
      const rk = (x) => x && x.dataset.rank !== "" ? Number(x.dataset.rank) : null
      const above = rk(others[idx - 1]), under = rk(others[idx])
      const rank = above == null && under == null ? null : above == null ? under + 10 : under == null ? above - 10 : (above + under) / 2
      const res = await api(apiUrl(`/api/tasks/${id}/move`), { method: "POST", body: JSON.stringify({ status: k, rank }) }).catch(er => ({ ok: false, error: er.message }))
      if (!res.ok) toast(res.error, "error")
      refreshWorkCount(); renderBoard(host, repoF, onPick)
    }
    return col
  })
  const agents = (r.agents || []).filter(a => a.on.length || (a.minutes_since_seen ?? 1e9) < 120)
  host.replaceChildren(
    agents.length ? el("div", { class: "row small", style: "gap:8px;flex-wrap:wrap;margin-bottom:8px" }, el("span", { class: "muted" }, "Agents:"),
      ...agents.map(a => badge(a.on.length ? "info" : "", `${a.name}${a.on.length ? `: ${a.on[0].title.slice(0, 30)}` : " (free)"}`, `last heard ${a.minutes_since_seen} min ago`))) : null,
    el("div", { class: "kanban" }, ...cols),
    el("p", { class: "muted small" }, "The Overseer keeps each column in order of importance; a card you drag stays where you drop it. 🧭 = made by the Overseer."))
}

let agentTimer = null
const stopAgentPoll = () => { if (agentTimer) clearTimeout(agentTimer); agentTimer = null }
const stopHermesPoll = stopAgentPoll
// What the agent is doing: a Hermes/Studio conversation, or a herdr agent's screen; and a box to tell it more.
function agentPanel(t) {
  const w = t.where || {}
  const name = { hermes: "Hermes", studio: "Hermes Studio", herdr: `herdr · ${w.kind || "agent"}` }[w.type]
  const wrap = el("div", { class: "agent-box panel" })
  const log = el("div", { class: "chat-box", role: "log", "aria-live": "polite", "aria-label": `${name} conversation`, style: "max-height:46vh" })
  const state = el("div", { class: "small muted" })
  const msgIn = el("textarea", { rows: "2", placeholder: `Tell ${name.split(" ·")[0]} more… (Enter to send)`, "aria-label": `Message to ${name}` })
  const sendBtn = el("button", { class: "btn" }, "Send")
  let shown = ""
  const poll = async () => {
    if (!wrap.isConnected) return stopAgentPoll()
    const r = await api(apiUrl(`/api/tasks/${t.id}/agent`)).catch(e => ({ ok: false, error: e.message, messages: [] }))
    const running = r.run?.running
    const says = { working: "working…", blocked: "is asking you something: answer it below", done: "finished: review its changes", idle: "is idle, waiting for you", unknown: "is running (herdr can't tell what it's doing)" }
    state.textContent = !r.ok ? (r.gone ? r.error : `Can't reach it: ${r.error}`) : r.type === "herdr" ? `${r.agent} (${r.kind || "agent"}${r.machine ? ` on ${r.machine}` : ""}) ${says[r.status] || r.status}${r.pending ? " — your task is held until it's ready" : ""}`
      : running ? "Working on it… (big jobs take a while; you can leave this page)" : r.run?.error ? `Last message failed: ${r.run.error}` : "Waiting for you"
    state.style.color = !r.ok || r.run?.error ? "var(--bad)" : r.status === "blocked" ? "#e0a060" : running ? "var(--info)" : r.status === "done" ? "var(--good)" : ""
    sendBtn.disabled = !!running && r.type !== "herdr"
    if (r.type === "herdr" || w.type === "herdr") {
      keysRow.hidden = !r.ok
      keysRow.classList.toggle("asking", r.status === "blocked")
      studioLink.hidden = !r.studio_url; if (r.studio_url) studioLink.href = r.studio_url
      closeBtn.hidden = !(r.ok && r.ours) || !!w.closed
    }
    const sig = JSON.stringify([r.messages?.length, r.output?.length, r.status])
    if (sig !== shown) {
      shown = sig
      if (r.type === "herdr") log.replaceChildren(el("pre", { class: "term" }, r.output || "(no output yet)"))
      else {
        log.replaceChildren(...(r.messages || []).map(m => {
          const d = el("div", { class: `msg ${m.role === "user" ? "user" : "bot"}${m.role === "tool" ? " tool" : ""}` })
          if (m.role === "assistant") d.innerHTML = renderMarkdown(m.content); else d.textContent = m.role === "tool" ? `⚙ ${m.content}` : m.content
          return d
        }))
        if (!(r.messages || []).length) log.append(el("p", { class: "muted" }, running ? "Waiting for the first reply…" : "No messages yet."))
      }
      log.scrollTop = log.scrollHeight
    }
    agentTimer = setTimeout(poll, running || r.status === "blocked" || r.pending ? 3000 : 12000)
  }
  sendBtn.onclick = () => {
    const text = msgIn.value.trim(); if (!text) return
    busy(sendBtn, "Sending…", async () => {
      const res = await api(apiUrl(`/api/tasks/${t.id}/send`), { method: "POST", body: JSON.stringify({ to: w.type, message: text }) }).catch(e => ({ ok: false, error: e.message }))
      if (!res.ok) return toast(res.error, "error")
      msgIn.value = ""; stopAgentPoll(); poll()
    })
  }
  msgIn.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendBtn.click() } })
  // herdr: answer its dialog or interrupt it with keys (only you answer an agent); its Hermes session opens in Studio
  const key = (label, keys, title) => { const b = el("button", { class: "btn ghost small", title }, label)
    b.onclick = () => busy(b, null, async () => {
      const res = await api(apiUrl(`/api/tasks/${t.id}/keys`), { method: "POST", body: JSON.stringify({ keys }) }).catch(e => ({ ok: false, error: e.message }))
      if (!res.ok) return toast(res.error, "error")
      stopAgentPoll(); setTimeout(poll, 600) }); return b }
  const keysRow = el("div", { class: "keys-row", hidden: "" }, el("span", { class: "muted small" }, "Answer or steer:"),
    key("Enter", ["enter"]), key("y", ["y"], "yes"), key("n", ["n"], "no"), key("1", ["1"]), key("2", ["2"]), key("3", ["3"]), key("↑", ["up"]), key("↓", ["down"]),
    key("Esc", ["esc"], "Interrupt or dismiss"), key("Ctrl+C", ["ctrl+c"], "Stop what it is doing"))
  const studioLink = el("a", { target: "_blank", rel: "noopener", class: "small", hidden: "" }, "its Hermes session in Studio ↗")
  const closeBtn = el("button", { class: "btn ghost small", hidden: "", title: "Close the herdr workspace this task opened (its branch and changes stay)" }, "Close its herdr workspace")
  closeBtn.onclick = () => busy(closeBtn, null, async () => {
    if (!confirm("Close this task's herdr workspace? The agent in it stops; the task's branch and changes stay.")) return
    const res = await api(apiUrl(`/api/tasks/${t.id}/herdr/close`), { method: "POST", body: "{}" }).catch(e => ({ ok: false, error: e.message }))
    if (!res.ok) return toast(res.error, "error")
    toast("Closed", "ok"); closeBtn.hidden = true; stopAgentPoll(); poll() })
  wrap.append(el("div", { class: "row", style: "gap:8px;align-items:center;flex-wrap:wrap" }, el("h4", { style: "margin:0" }, name),
      w.type === "studio" && w.url ? el("a", { href: w.url, target: "_blank", rel: "noopener", class: "small" }, "open in Studio ↗") : null,
      studioLink, el("div", { class: "spacer" }), closeBtn), state, log, keysRow,
    el("div", { class: "composer" }, msgIn, el("div", { class: "row" }, el("div", { class: "spacer" }), sendBtn)))
  setTimeout(poll, 0)
  return wrap
}

// The task's branch, coding-agent style: its commits, the files it changed (click for the diff),
// what's not committed yet, and a commit box with the message the agent proposed, for you to edit.
function branchPanel(t, redraw) {
  const wrap = el("div", { class: "branch-box panel" }, loading("Reading the task's branch…"))
  const draw = async () => {
    const c = await api(apiUrl(`/api/tasks/${t.id}/changes`)).catch(e => ({ ok: false, error: e.message }))
    if (!c.ok) return wrap.replaceChildren(el("p", { class: "muted", style: "padding:10px" }, c.error))
    const fileRow = (f) => {
      const b = el("button", { class: "file-row", title: "Show the diff" },
        el("span", { class: `fst ${f.committed ? "c" : "u"}` }, f.committed ? `+${f.added} −${f.removed}` : (f.status === "??" ? "new" : f.status)),
        el("span", { class: "nm" }, f.path))
      b.onclick = async () => {
        const d = await api(apiUrl(`/api/tasks/${t.id}/diff?path=${encodeURIComponent(f.path)}`))
        const pre = el("pre", { class: "code-pre", tabindex: "0" })
        for (const line of (d.diff || "(no changes)").split("\n")) {
          const cls = line.startsWith("+") && !line.startsWith("+++") ? "add" : line.startsWith("-") && !line.startsWith("---") ? "del" : line.startsWith("@@") ? "hunk" : ""
          pre.append(el("div", { class: `dl ${cls}` }, line || " "))
        }
        openDialog(f.path, pre)
      }
      return b
    }
    const uncommitted = c.files.filter(f => !f.committed), committed = c.files.filter(f => f.committed)
    const msg = el("textarea", { rows: "4", "aria-label": "Commit message", placeholder: "Subject line\n\nWhy this change", style: "width:100%;font:13px var(--mono)" })
    msg.value = c.proposed_message || ""
    const commitBtn = el("button", { class: "btn small" }, "Commit")
    commitBtn.onclick = () => busy(commitBtn, "Committing…", async () => {
      const r = await api(apiUrl(`/api/tasks/${t.id}/commit`), { method: "POST", body: JSON.stringify({ message: msg.value }) }).catch(e => ({ ok: false, error: e.message }))
      if (!r.ok) return toast(r.error, "error")
      toast(`Committed ${r.sha}`, "ok"); draw()
    })
    const submitBtn = el("button", { class: "btn small" }, "Ask for approval")
    submitBtn.onclick = () => busy(submitBtn, "Raising…", async () => {
      const r = await api(apiUrl(`/api/tasks/${t.id}/submit`), { method: "POST", body: "{}" }).catch(e => ({ ok: false, error: e.message }))
      if (!r.ok) return toast(r.error, "error")
      toast(`Request raised: it merges into ${c.onto} when you approve`, "ok"); refreshPendingCount()
    })
    wrap.replaceChildren(...[
      el("div", { class: "row", style: "gap:8px;align-items:center;flex-wrap:wrap" }, el("h4", { style: "margin:0" }, "Branch"),
        el("code", { class: "small" }, c.branch), el("span", { class: "muted small" }, `→ ${c.onto}`), el("div", { class: "spacer" }),
        el("button", { class: "btn ghost small", onclick: () => draw() }, "Refresh")),
      c.warning ? el("div", { class: "small", style: "color:var(--bad)" }, `⚠ ${c.warning}`) : null,
      el("div", { class: "muted small" }, c.next_step),
      el("h5", {}, `Commits (${c.commits.length})`),
      c.commits.length ? el("div", { class: "col", style: "gap:2px" }, ...c.commits.map(x => el("div", { class: "commit-line" },
        el("button", { class: "linkish", onclick: () => showCommit(t.repo_id, x.full) }, el("code", {}, x.sha)), el("span", {}, x.subject),
        el("span", { class: "muted small" }, ` · ${x.author}`)))) : el("div", { class: "muted small" }, "No commits on the branch yet."),
      el("h5", {}, `Changed files (${committed.length} committed, ${uncommitted.length} not yet)`),
      el("div", { class: "col", style: "gap:2px" }, ...uncommitted.map(fileRow), ...committed.slice(0, 25).map(fileRow),
        ...[committed.length > 25 ? (() => { const b = el("button", { class: "linkish small" }, `show ${committed.length - 25} more committed files`)
          b.onclick = () => b.replaceWith(...committed.slice(25).map(fileRow)); return b })() : null].filter(Boolean)),
      uncommitted.length ? el("div", { class: "col", style: "gap:6px;margin-top:8px" },
        el("label", { class: "small" }, c.proposed_message ? "Commit message (proposed by the agent — edit it before committing)" : "Commit message"), msg,
        el("div", { class: "row", style: "gap:6px" }, commitBtn)) : null,
      c.ready ? el("div", { class: "row", style: "gap:6px;margin-top:8px" }, submitBtn, el("span", { class: "muted small" }, `Raises an approval request to merge it into ${c.onto}.`)) : null].filter(Boolean))
  }
  draw()
  return wrap
}

// ------------------------------------------------------------------ References
// A library of things to look things up in: repos cloned for reference (with a branch of your own if
// you like) and any files (PDFs, zips, images, videos, notes), sorted into folders. Point a chat at a
// folder or file ("Use in a chat"); agents read it through MCP or straight from its folder.
const REF_ICON = { folder: "📁", repo: "📦", pdf: "📕", zip: "🗜", image: "🖼", video: "🎞", text: "📄", file: "📎" }
const fmtSize = b => b == null ? "" : fmtBytes(b)
async function renderReferences() {
  const main = $("#content")
  const params = new URLSearchParams(location.search)
  let current = params.get("path") || "", selected = null, sortBy = store_.get("refSort", "name")
  const tree = el("div", { class: "ref-tree", role: "tree", "aria-label": "Reference folders" })
  const right = el("div", { class: "ref-main panel" })
  const rootInfo = el("span", { class: "muted small" })
  const go = (path, sel = null) => { current = path; selected = sel; history.replaceState({}, "", `${BASE}/references${path ? `?path=${encodeURIComponent(path)}` : ""}`); drawRight(); markTree() }
  // left: folders and repos, expandable
  const branch = async (path, box, depth) => {
    const r = await api(apiUrl(`/api/refs?path=${encodeURIComponent(path)}`)).catch(e => ({ items: [], error: e.message }))
    if (!path && r.root) rootInfo.replaceChildren(el("span", {}, `on disk: ${r.root} `), el("button", { class: "linkish small", onclick: async () => {
      const to = prompt("Keep the library in this folder (absolute path; what's already there stays where it is):", r.root)
      if (!to || to === r.root) return
      const res = await api(apiUrl("/api/refs/settings"), { method: "POST", body: JSON.stringify({ root: to }) }).catch(e => ({ ok: false, error: e.message }))
      if (!res.ok) return toast(res.error, "error")
      toast(`The library is now ${res.root}`, "ok"); renderReferences() } }, "change"))
    box.replaceChildren(...(r.items || []).filter(i => i.kind === "folder" || i.kind === "repo").map(i => {
      const kids = el("div", { role: "group", hidden: "" })
      const row = el("div", { class: "trow", role: "treeitem", tabindex: "0", "data-path": i.path, "aria-expanded": "false", style: `padding-left:${8 + depth * 14}px` },
        el("span", { class: "tw", "aria-hidden": "true" }, "▸"), el("span", { "aria-hidden": "true" }, REF_ICON[i.kind]), el("span", { class: "nm" }, i.name),
        i.job ? el("span", { class: `muted small` }, i.job.state === "failed" ? " · clone failed" : " · cloning…") : i.branch ? el("span", { class: "muted small" }, ` · ${i.branch}`) : null)
      const toggle = async (open) => {
        row.setAttribute("aria-expanded", String(open)); row.firstChild.textContent = open ? "▾" : "▸"; kids.hidden = !open
        if (open && !kids.children.length && i.kind === "folder") await branch(i.path, kids, depth + 1)
      }
      row.firstChild.onclick = (e) => { e.stopPropagation(); toggle(row.getAttribute("aria-expanded") !== "true") }
      row.onclick = () => { go(i.path); if (i.kind === "folder") toggle(true) }
      row.onkeydown = (e) => { if (e.key === "Enter") row.click(); if (e.key === "ArrowRight") toggle(true); if (e.key === "ArrowLeft") toggle(false) }
      // drop items (dragged from the list) onto a folder to move them there
      if (i.kind === "folder") {
        row.ondragover = (e) => { e.preventDefault(); row.classList.add("drop") }
        row.ondragleave = () => row.classList.remove("drop")
        row.ondrop = async (e) => { e.preventDefault(); row.classList.remove("drop"); await dropOn(i.path, e) }
      }
      return el("div", {}, row, kids)
    }))
  }
  const rootRow = el("div", { class: "trow", role: "treeitem", tabindex: "0", "data-path": "", onclick: () => go("") }, el("span", { "aria-hidden": "true" }, "📚"), el("span", { class: "nm" }, "Library"))
  rootRow.ondragover = (e) => { e.preventDefault(); rootRow.classList.add("drop") }
  rootRow.ondragleave = () => rootRow.classList.remove("drop")
  rootRow.ondrop = async (e) => { e.preventDefault(); rootRow.classList.remove("drop"); await dropOn("", e) }
  const treeBody = el("div")
  const markTree = () => tree.querySelectorAll(".trow").forEach(r => r.classList.toggle("sel", r.dataset.path === (selected ?? current)))
  const refreshTree = async () => { await branch("", treeBody, 1); markTree() }
  tree.append(rootRow, treeBody)
  const dropOn = async (folder, e) => {
    const moving = e.dataTransfer.getData("text/x-ref-path")
    if (moving) {
      const r = await api(apiUrl("/api/refs/move"), { method: "POST", body: JSON.stringify({ path: moving, to: folder }) }).catch(x => ({ ok: false, error: x.message }))
      if (!r.ok) return toast(r.error, "error")
      toast(`Moved to ${folder || "the library"}`, "ok"); await refreshTree(); return drawRight()
    }
    if (e.dataTransfer.files?.length) await upload(folder, e.dataTransfer.files)
  }
  const upload = async (folder, files) => {
    const fd = new FormData(); fd.append("folder", folder)
    for (const f of files) fd.append("file", f)
    toast(`Uploading ${files.length} file${files.length > 1 ? "s" : ""}…`, "info")
    const resp = await fetch(apiUrl("/api/refs/upload"), { method: "POST", body: fd, headers: { "X-Upstream-Upload": "1" } })
    const r = await resp.json().catch(() => ({ ok: false, error: `HTTP ${resp.status}` }))
    if (!r.ok) toast(r.error, "error"); else toast(`Added ${r.saved.length} file${r.saved.length > 1 ? "s" : ""}`, "ok")
    await refreshTree(); drawRight()
  }
  // toolbar
  const fileIn = el("input", { type: "file", multiple: "", hidden: "" })
  fileIn.onchange = () => { if (fileIn.files.length) upload(current, fileIn.files); fileIn.value = "" }
  const newFolder = el("button", { class: "btn ghost small" }, "+ Folder")
  newFolder.onclick = async () => {
    const name = prompt(`New folder in ${current || "the library"}:`); if (!name) return
    const r = await api(apiUrl("/api/refs/folder"), { method: "POST", body: JSON.stringify({ parent: current, name }) }).catch(e => ({ ok: false, error: e.message }))
    if (!r.ok) return toast(r.error, "error")
    await refreshTree(); go(r.path)
  }
  const cloneBtn = el("button", { class: "btn small" }, "+ Clone a repo")
  cloneBtn.onclick = () => {
    const url = el("input", { placeholder: "https://github.com/owner/repo (or any git URL)", style: "width:100%", "aria-label": "Repo URL" })
    const name = el("input", { placeholder: "folder name (optional)", style: "width:100%", "aria-label": "Folder name" })
    const br = el("input", { placeholder: "branch (optional; default branch otherwise)", style: "width:100%", "aria-label": "Branch" })
    const go2 = el("button", { class: "btn" }, "Clone")
    let close
    go2.onclick = () => busy(go2, "Starting…", async () => {
      const r = await api(apiUrl("/api/refs/clone"), { method: "POST", body: JSON.stringify({ url: url.value, folder: current, name: name.value, branch: br.value }) }).catch(e => ({ ok: false, error: e.message }))
      if (!r.ok) return toast(r.error, "error")
      close()
      if (r.linked) toast(`Already in the library at ${r.link_to}: linked it here instead of cloning it twice`, "ok")
      else toast("Cloning in the background (history only; files download when opened)", "ok")
      await refreshTree(); drawRight(); pollClones()
    })
    close = openDialog(`Clone a reference repo into ${current || "the library"}`, el("div", { class: "col", style: "gap:8px;min-width:min(520px,80vw)" }, url, name, br, el("div", {}, go2)))
    setTimeout(() => url.focus(), 0)
  }
  let cloneTimer = null
  const pollClones = () => { clearTimeout(cloneTimer); cloneTimer = setTimeout(async () => {
    if (!right.isConnected) return
    await refreshTree(); await drawRight()
    if (right.querySelector("[data-cloning]") || tree.textContent.includes("cloning…")) pollClones()
  }, 2000) }
  const updAll = el("button", { class: "btn ghost small", title: "Bring every reference repo up to date with where it came from. Ones you edited are only fetched." }, "Update all")
  const updState = (r) => r.state === "running" ? `Updating ${r.total ?? "…"} repos…`
    : r.state === "finished" ? `${r.done.length} updated${r.edited.length ? `, ${r.edited.length} edited (fetched only)` : ""}${r.failed.length ? `, ${r.failed.length} failed: ${r.failed.map(f => `${f.path} (${f.error})`).join("; ")}` : ""}` : ""
  updAll.onclick = () => busy(updAll, "Updating…", async () => {
    let r = await api(apiUrl("/api/refs/update-all"), { method: "POST", body: "{}" }).catch(e => ({ ok: false, error: e.message }))
    if (r.ok === false) return toast(r.error, "error")
    for (let n = 0; r.state === "running" && n < 900 && updAll.isConnected; n++) { await new Promise(res => setTimeout(res, 2000)); r = await api(apiUrl("/api/refs/update-all")).catch(() => ({ state: "running" })) }
    if (r.state === "finished") toast(updState(r), r.failed?.length ? "error" : "ok")
    await refreshTree(); drawRight()
  })
  api(apiUrl("/api/refs/update-all")).then(r => { updAll.title += r.every_hours ? ` Runs on its own every ${r.every_hours} h${r.last ? `; last ${r.last.slice(0, 16).replace("T", " ")}` : ""}.` : " Automatic updates are off." }).catch(() => {})
  const sortSel = el("select", { "aria-label": "Sort by" }, ...[["name", "Name"], ["added", "Newest"], ["kind", "Type"], ["size", "Size"]].map(([v, l]) => el("option", { value: v }, `Sort: ${l}`)))
  sortSel.value = sortBy
  sortSel.onchange = () => { sortBy = sortSel.value; store_.set("refSort", sortBy); drawRight() }
  const searchIn = el("input", { type: "search", placeholder: "Search names and repo text…", "aria-label": "Search references", style: "min-width:200px" })
  searchIn.onkeydown = async (e) => {
    if (e.key !== "Enter" || !searchIn.value.trim()) return
    const r = await api(apiUrl(`/api/refs/search?q=${encodeURIComponent(searchIn.value.trim())}`)).catch(x => ({ ok: false, error: x.message, items: [] }))
    right.replaceChildren(el("div", { style: "padding:12px" }, el("h3", { style: "margin-top:0" }, `Found ${r.items.length} for “${searchIn.value.trim()}”`),
      r.ok ? null : el("p", { class: "muted" }, r.error),
      ...r.items.map(i => el("button", { class: "file-row", onclick: () => { const parent = i.path.split("/").slice(0, -1).join("/"); go(parent, i.path) } },
        el("span", {}, REF_ICON[i.kind] || "📄"), el("span", { class: "nm" }, i.path), el("span", { class: "muted small" }, i.match === "name" ? "" : ` ${i.match}`)))))
  }
  // right: the folder's contents, or one item
  const sorters = { name: (a, b) => a.name.localeCompare(b.name), added: (a, b) => (b.added || b.modified || "").localeCompare(a.added || a.modified || ""),
    kind: (a, b) => a.kind.localeCompare(b.kind) || a.name.localeCompare(b.name), size: (a, b) => (b.size || 0) - (a.size || 0) }
  const drawRight = async () => {
    if (selected && selected !== current) return drawItem(selected)
    const r = await api(apiUrl(`/api/refs?path=${encodeURIComponent(current)}`)).catch(e => ({ ok: false, error: e.message, items: [] }))
    if (r.ok === false) return right.replaceChildren(el("p", { class: "muted", style: "padding:12px" }, r.error))
    const items = [...r.items].sort((a, b) => ((a.kind === "folder") === (b.kind === "folder") ? 0 : a.kind === "folder" ? -1 : 1) || sorters[sortBy](a, b))
    const crumbs = el("div", { class: "row", style: "gap:4px;flex-wrap:wrap;align-items:center" }, el("button", { class: "linkish", onclick: () => go("") }, "Library"),
      ...current.split("/").filter(Boolean).flatMap((part, i, all) => [el("span", { class: "muted" }, "/"), el("button", { class: "linkish", onclick: () => go(all.slice(0, i + 1).join("/")) }, part)]))
    const isRepo = r.in_repo === current && current
    const rows = items.map(i => {
      const row = el("div", { class: "ref-row", draggable: "true", tabindex: "0", ...(i.job?.state === "cloning" ? { "data-cloning": "1" } : {}) },
        el("span", { "aria-hidden": "true" }, REF_ICON[i.kind] || "📎"),
        el("span", { class: "nm" }, i.name, i.link_to ? el("span", { class: "muted small", title: `A link: the one copy is at ${i.link_to}` }, ` 🔗 ${i.link_to}`) : null,
          i.edited ? el("span", { class: "badge warn", style: "margin-left:6px", title: `${i.edited.files_count} changed file(s), ${i.edited.commits} commit(s) of yours: it joins your chats and updates only fetch` }, "✎ edited") : null),
        el("span", { class: "muted small", style: i.incomplete ? "color:var(--bad)" : null }, i.job ? (i.job.state === "failed" ? `clone failed: ${i.job.error}` : "cloning…")
          : i.incomplete ? "incomplete clone: delete it and clone again" : i.kind === "repo" ? `${i.branch} · ${i.url || i.source || ""}` : i.kind === "folder" ? "" : i.kind),
        el("span", { class: "muted small", style: "text-align:right" }, fmtSize(i.size)),
        el("span", { class: "muted small" }, (i.added || i.modified || "").slice(0, 10)),
        i.job ? el("span") : (() => { const m = itemMenu(i); m.classList.add("row-menu"); m.addEventListener("click", (e) => e.stopPropagation()); return m })())
      row.ondragstart = (e) => e.dataTransfer.setData("text/x-ref-path", i.path)
      row.onclick = () => i.job?.state === "failed" ? api(apiUrl("/api/refs/dismiss"), { method: "POST", body: JSON.stringify({ path: i.path }) }).then(() => drawRight())
        : i.kind === "folder" ? go(i.path) : go(current, i.path)
      row.onkeydown = (e) => { if (e.key === "Enter") row.click() }
      return row
    })
    const drop = el("div", { class: "ref-drop", role: "list" }, ...rows,
      rows.length ? null : el("p", { class: "muted", style: "padding:16px" }, isRepo ? "This repo's files are shown on its page." : "Empty. Drop files here, upload, or clone a repo into this folder."))
    drop.ondragover = (e) => { if (e.dataTransfer.types.includes("Files")) { e.preventDefault(); drop.classList.add("drop") } }
    drop.ondragleave = () => drop.classList.remove("drop")
    drop.ondrop = async (e) => { e.preventDefault(); drop.classList.remove("drop"); if (e.dataTransfer.files?.length) await upload(current, e.dataTransfer.files) }
    right.replaceChildren(el("div", { class: "col", style: "gap:8px;padding:12px" },
      el("div", { class: "row", style: "gap:8px;align-items:center;flex-wrap:wrap" }, crumbs, el("div", { class: "spacer" }),
        current ? el("button", { class: "btn ghost small", onclick: () => useInChat(current, current.split("/").pop()) }, "Use this folder in a chat") : null,
        current ? itemMenu({ path: current, name: current.split("/").pop(), kind: "folder" }) : null),
      el("div", { class: "ref-head muted small" }, el("span", {}), el("span", {}, "Name"), el("span", {}, ""), el("span", { style: "text-align:right" }, "Size"), el("span", {}, "Added"), el("span", {})),
      r.more ? el("div", { class: "muted small" }, `…and ${r.more} more items not listed here (use Search to find them).`) : null,
      drop, el("div", { class: "muted small" }, "Drag files from your computer onto this list to upload them here; drag an item onto a folder on the left to move it.")))
    if (rows.some(r => r.dataset.cloning)) pollClones()
  }
  const itemMenu = (i) => menu(`More for ${i.name}`, [
    { label: "Copy its path on disk", run: async () => { const t = await api(apiUrl("/api/refs")); navigator.clipboard?.writeText(`${t.root}/${i.path}`); toast("Path copied", "ok") } },
    { label: "Rename…", run: async () => { const n = prompt("New name:", i.name); if (!n || n === i.name) return
      const r = await api(apiUrl("/api/refs/move"), { method: "POST", body: JSON.stringify({ path: i.path, to: i.path.split("/").slice(0, -1).join("/"), name: n }) }).catch(e => ({ ok: false, error: e.message }))
      if (!r.ok) return toast(r.error, "error"); await refreshTree(); go(r.path.split("/").slice(0, -1).join("/"), i.kind === "folder" ? null : r.path) } },
    i.kind === "folder" ? null : { label: "Use in a chat…", run: () => useInChat(i.path, i.name) },
    { label: "Also put it in another folder (link)…", run: () => linkDialog(i, async () => { await refreshTree(); drawRight() }) },
    { label: "Delete", danger: true, run: async () => {
      const links = (await api(apiUrl(`/api/refs/links?path=${encodeURIComponent(i.path)}`)).catch(() => ({ items: [] }))).items || []
      if (!confirm(`Delete ${i.path} from the reference library? This removes it from disk.` + (links.length ? `\n\nIt is also linked from ${links.length} place(s) (${links.slice(0, 3).join(", ")}${links.length > 3 ? "…" : ""}); those links go too.` : ""))) return
      const r = await api(apiUrl("/api/refs/delete"), { method: "POST", body: JSON.stringify({ path: i.path }) }).catch(e => ({ ok: false, error: e.message }))
      if (!r.ok) return toast(r.error, "error"); await refreshTree(); go(i.path.split("/").slice(0, -1).join("/")) } },
  ])
  const drawItem = async (path) => {
    right.replaceChildren(loading())
    const r = await api(apiUrl(`/api/refs/read?path=${encodeURIComponent(path)}`)).catch(e => ({ ok: false, error: e.message }))
    if (r.ok === false) return right.replaceChildren(el("p", { class: "muted", style: "padding:12px" }, r.error))
    const name = path.split("/").pop(), raw = apiUrl(`/api/refs/raw?path=${encodeURIComponent(path)}`)
    let view
    if (r.kind === "image") view = el("img", { src: raw, alt: name, style: "max-width:100%;max-height:70vh;border-radius:8px" })
    else if (r.kind === "video") view = el("video", { src: raw, controls: "", style: "max-width:100%;max-height:70vh" })
    else if (r.kind === "pdf") view = el("div", { class: "col", style: "gap:8px" }, el("iframe", { src: raw, title: name, style: "width:100%;height:60vh;border:0;background:#fff;border-radius:6px" }),
      el("details", {}, el("summary", { class: "muted small" }, "The text a model reads from it"), el("pre", { class: "code-pre", style: "white-space:pre-wrap;max-height:40vh" }, r.text || "")))
    else if (/\.md$/i.test(name)) { view = el("div", { class: "md" }); view.innerHTML = renderMarkdown(r.text || "") }
    else view = el("pre", { class: "code-pre", style: "white-space:pre-wrap;max-height:65vh" }, r.text ?? "(a binary file)")
    const note = el("textarea", { rows: "2", placeholder: "A note about this (why you kept it, what to look at)…", style: "width:100%", "aria-label": "Note" }); note.value = r.note || ""
    note.onchange = () => api(apiUrl("/api/refs/note"), { method: "POST", body: JSON.stringify({ path, note: note.value }) }).then(() => toast("Note saved", "ok"))
    const parts = [el("div", { class: "row", style: "gap:8px;align-items:center;flex-wrap:wrap" },
        el("button", { class: "linkish", onclick: () => go(current) }, "← back"), el("strong", {}, `${REF_ICON[r.kind] || ""} ${name}`),
        el("span", { class: "muted small" }, [r.kind, fmtSize(r.size), r.source ? `from ${r.source}` : "", r.added ? `added ${r.added.slice(0, 10)}` : ""].filter(Boolean).join(" · ")),
        el("div", { class: "spacer" }),
        el("button", { class: "btn small", onclick: () => useInChat(path, name) }, "Use in a chat"),
        r.kind !== "folder" && r.kind !== "repo" ? el("a", { class: "btn ghost small", href: raw + "&download=1" }, "Download") : null, itemMenu({ path, name, kind: r.kind })),
      note]
    if (r.kind === "repo") parts.push(await repoBox(path))
    parts.push(view)
    right.replaceChildren(el("div", { class: "col", style: "gap:10px;padding:12px" }, ...parts))
  }
  // a reference repo: its branch, switch to another, make your own, update
  const repoBox = async (path) => {
    const info = (await api(apiUrl(`/api/refs?path=${encodeURIComponent(path.split("/").slice(0, -1).join("/"))}`))).items.find(i => i.path === path) || {}
    const sel = el("select", { "aria-label": "Branch" }, ...(info.branches || []).map(b => el("option", { value: b }, b)))
    sel.value = info.branch
    const more = el("button", { class: "btn ghost small" }, "Remote branches…")
    more.onclick = () => busy(more, "Listing…", async () => {
      const r = await api(apiUrl(`/api/refs/branches?path=${encodeURIComponent(path)}`)).catch(e => ({ items: [], error: e.message }))
      for (const b of r.items) if (![...sel.options].some(o => o.value === b)) sel.append(el("option", { value: b }, `${b} (remote)`))
      toast(r.error || `${r.items.length} branches on the remote`, r.error ? "error" : "ok")
    })
    sel.onchange = async () => {
      const r = await api(apiUrl("/api/refs/switch"), { method: "POST", body: JSON.stringify({ path, branch: sel.value }) }).catch(e => ({ ok: false, error: e.message }))
      if (!r.ok) { toast(r.error, "error"); sel.value = info.branch; return }
      toast(`Now on ${r.branch}`, "ok"); drawItem(path); refreshTree()
    }
    const mk = el("button", { class: "btn ghost small" }, "+ My own branch")
    mk.onclick = async () => { const n = prompt(`New branch of ${path.split("/").pop()} (from ${sel.value}):`); if (!n) return
      const r = await api(apiUrl("/api/refs/branch"), { method: "POST", body: JSON.stringify({ path, name: n }) }).catch(e => ({ ok: false, error: e.message }))
      if (!r.ok) return toast(r.error, "error"); toast(`Made branch ${r.branch}`, "ok"); drawItem(path); refreshTree() }
    const upd = el("button", { class: "btn ghost small" }, "Update")
    upd.onclick = () => busy(upd, "Pulling…", async () => {
      const r = await api(apiUrl("/api/refs/update"), { method: "POST", body: JSON.stringify({ path }) }).catch(e => ({ ok: false, error: e.message }))
      toast(!r.ok ? r.error : r.skipped ? `Fetched only: ${r.skipped}` : "Up to date", r.ok ? "ok" : "error")
    })
    return el("div", { class: "row", style: "gap:8px;flex-wrap:wrap;align-items:center" }, el("span", { class: "muted small" }, "Branch"), sel, more, mk, upd,
      el("span", { class: "muted small" }, info.url || ""))
  }
  main.replaceChildren(
    el("div", { class: "row", style: "margin-bottom:12px;gap:10px;flex-wrap:wrap;align-items:center" }, el("h1", { class: "page", style: "margin:0" }, "References"),
      el("span", { class: "muted" }, "Repos and files to look things up in. Any chat or agent can read them."), el("div", { class: "spacer" }),
      searchIn, sortSel, updAll, newFolder, el("button", { class: "btn ghost small", onclick: () => fileIn.click() }, "Upload files"), cloneBtn, fileIn),
    el("div", { class: "ref-grid" }, el("div", { class: "panel", style: "min-width:0;padding:6px 0" }, tree, el("div", { style: "padding:6px 10px" }, rootInfo)), right))
  await refreshTree()
  const sel0 = params.get("item"); if (sel0) selected = sel0
  await drawRight()
}

// ------------------------------------------------------------------ Integrations
// What this setup is connected to and whether it works, your other containers, and how far each
// repo is integrated. This app's own record: nothing is written to the wiki or GitHub. See services.py.
const MARK_CLS = { integrated: "up", partial: "warn", planned: "info", none: "", "": "" }
// The Overseer: what it sees, what it did, and how it should run. Plain rules, not a model.
async function renderOverseer() {
  const main = $("#content")
  main.replaceChildren(el("h1", { class: "page" }, "Overseer"), loading("Asking the Overseer…"))
  let r = await api(apiUrl("/api/overseer")).catch(e => ({ ok: false, error: e.message }))
  if (!r.ok) return main.replaceChildren(el("h1", { class: "page" }, "Overseer"), el("p", { class: "muted" }, r.error))
  if (!r.report?.at) r = { ...r, report: (await api(apiUrl("/api/overseer/run"), { method: "POST", body: "{}" })).report }
  const rep = r.report, st = r.settings
  const runBtn = el("button", { class: "btn" }, "Look again now")
  runBtn.onclick = () => busy(runBtn, "Looking…", async () => { await api(apiUrl("/api/overseer/run"), { method: "POST", body: "{}" }); await renderOverseer() })
  const card = (title, ...body) => el("div", { class: "card", style: "margin-top:12px" }, el("h3", { style: "margin-top:0" }, title), ...body)
  const taskLink = (t) => el("a", { href: `${BASE}/work?task=${t.id}` }, t.title)
  // repos: priority you set + how much they need attention now
  const repoRows = (rep.repos || []).map(x => {
    const sel = el("select", { "aria-label": `${x.name} priority` }, ...["critical", "high", "normal", "low"].map(p => el("option", { value: p }, p)))
    sel.value = x.priority || "normal"
    sel.onchange = async () => { await api(apiUrl(`/api/repo/${x.id}/priority`), { method: "POST", body: JSON.stringify({ priority: sel.value }) }); toast(`${x.name}: ${sel.value}`, "ok") }
    return el("tr", {}, el("td", {}, el("a", { href: `${BASE}/repo/${x.id}` }, x.name)), el("td", {}, sel), el("td", {}, el("b", {}, String(Math.round(x.score)))),
      el("td", { class: "muted small" }, x.why.join(" · ") || "nothing needs doing"))
  })
  // settings
  const en = el("input", { type: "checkbox", style: "height:auto", "aria-label": "Overseer on" }); en.checked = st.enabled
  const mk = el("input", { type: "checkbox", style: "height:auto", "aria-label": "Make tasks" }); mk.checked = st.make_tasks
  const disp = el("select", { "aria-label": "Hand tasks to" }, el("option", { value: "off" }, "nobody (agents take work themselves)"), el("option", { value: "herdr" }, "herdr agents"), el("option", { value: "studio" }, "Hermes Studio"))
  disp.value = st.dispatch
  const maxA = el("input", { type: "number", min: "0", max: "20", value: String(st.max_agents), "aria-label": "At most this many agents", style: "width:60px" })
  const every = el("input", { type: "number", min: "1", value: String(st.every_minutes), "aria-label": "Every minutes", style: "width:70px" })
  const stale = el("input", { type: "number", min: "0.1", step: "0.5", value: String(st.stale_request_days), "aria-label": "Remind about requests after days", style: "width:70px" })
  const save = el("button", { class: "btn small" }, "Save")
  save.onclick = () => busy(save, "Saving…", async () => {
    const res = await api(apiUrl("/api/overseer/settings"), { method: "POST", body: JSON.stringify({ enabled: en.checked, make_tasks: mk.checked, dispatch: disp.value, max_agents: maxA.value, every_minutes: every.value, stale_request_days: stale.value }) }).catch(e => ({ ok: false, error: e.message }))
    toast(res.ok ? "Saved" : res.error, res.ok ? "ok" : "error")
  })
  main.replaceChildren(
    el("div", { class: "row", style: "gap:10px;align-items:center;flex-wrap:wrap" }, el("h1", { class: "page", style: "margin:0" }, "Overseer"),
      el("span", { class: "muted" }, `Ranks your repos and the board, turns problems into tasks, takes back stalled work and keeps agents going. Plain rules, no model. Last looked ${(rep.at || "").slice(0, 16).replace("T", " ")} UTC.`),
      el("div", { class: "spacer" }), el("a", { href: `${BASE}/work`, class: "btn ghost", onclick: () => store_.set("workView", "board") }, "Open the board"), runBtn),
    el("div", { class: "tiles", style: "margin-top:12px" },
      el("div", { class: "tile" }, el("span", { class: "muted" }, "Open tasks"), el("b", {}, String((rep.next || []).length))),
      el("div", { class: "tile" }, el("span", { class: "muted" }, "Being worked on"), el("b", {}, String((rep.working || []).length))),
      el("div", { class: `tile ${(rep.waiting_for_you || []).length ? "warn" : "good"}` }, el("span", { class: "muted" }, "Waiting for you"), el("b", {}, String((rep.waiting_for_you || []).length))),
      el("div", { class: "tile" }, el("span", { class: "muted" }, "Agents heard from"), el("b", {}, String((rep.agents || []).length)))),
    card("Next up (agents get these in this order)", (rep.next || []).length ? el("ol", { style: "margin:0;padding-left:20px" }, ...rep.next.map(t =>
      el("li", { style: "margin:4px 0" }, badge(t.priority === "urgent" ? "bad" : t.priority === "high" ? "warn" : "", t.priority || "normal"), " ", taskLink(t),
        el("span", { class: "muted small" }, ` · ${t.repo} · ${(t.why || []).join("; ")}`)))) : el("p", { class: "muted" }, "Nothing open. 🎉")),
    card("Being worked on", (rep.working || []).length ? el("ul", { style: "margin:0;padding-left:20px" }, ...rep.working.map(t => el("li", {}, taskLink(t), el("span", { class: "muted small" }, ` · ${t.repo}${t.agent ? ` · ${t.agent}` : ""}`)))) : el("p", { class: "muted" }, "Nobody is on anything right now.")),
    card("Repos, most in need first", el("p", { class: "muted small", style: "margin-top:0" }, "Set how much each repo matters to you; the Overseer weighs that first, then conflicts, waiting requests, failed checks, bugs and how far behind it is."),
      el("div", { class: "table-box" }, el("table", { class: "list" }, el("thead", {}, el("tr", {}, el("th", {}, "Repo"), el("th", {}, "Matters"), el("th", {}, "Need"), el("th", {}, "Why"))), el("tbody", {}, ...repoRows)))),
    card("What it did last time", (rep.actions || []).length ? el("ul", { class: "small", style: "margin:0;padding-left:20px" }, ...rep.actions.map(a => el("li", {}, a))) : el("p", { class: "muted small" }, "Nothing needed doing.")),
    card("How it runs", el("div", { class: "col", style: "gap:8px" },
      el("label", { class: "row", style: "gap:6px" }, en, `On, every`, every, "minutes"),
      el("label", { class: "row", style: "gap:6px" }, mk, "Turn problems into tasks (conflicts, failed checks, repo errors) and close them when they're gone"),
      el("label", { class: "row", style: "gap:6px" }, "Hand the next tasks to", disp, "at most", maxA, "at a time"),
      el("label", { class: "row", style: "gap:6px" }, "Remind me about requests waiting", stale, "days"),
      el("div", {}, save),
      el("p", { class: "muted small", style: "margin:0" }, "Agents ask it for work with next_task (an agent run by cron does that first), talk on tasks and to each other, and check in while they work; a claim that runs out goes back on the board."))))
}

async function renderIntegrations() {
  const main = $("#content")
  main.replaceChildren(el("h1", { class: "page" }, "Integrations"), loading("Checking each connection…"))
  const r = await api(apiUrl("/api/services")).catch(e => ({ ok: false, error: e.message }))
  if (!r.ok) return main.replaceChildren(el("h1", { class: "page" }, "Integrations"), el("p", { class: "muted" }, r.error))
  const works = (st) => !st ? el("span", { class: "muted" }, "-") : st.ok
    ? badge("up", "works", st.ms != null ? `${st.ms} ms` : "") : badge(st.error === "off" ? "" : "bad", st.error === "off" ? "off" : "not working", st.error || "")
  const repoLink = (id, name) => id ? el("a", { href: `${BASE}/repo/${encodeURIComponent(id)}` }, name || id) : el("span", { class: "muted" }, name ? `${name} (not tracked)` : "-")
  // 1. connections and services
  const svcBody = el("tbody")
  for (const s of r.services) {
    const st = s.status || {}
    svcBody.append(el("tr", {},
      el("td", {}, el("strong", {}, s.name), el("div", { class: "muted small" }, s.kind === "builtin" ? s.does : [s.container ? `container ${s.container}` : "", s.notes].filter(Boolean).join(" · "))),
      el("td", {}, repoLink(s.repo, s.repo_name)),
      el("td", {}, works(st), st.ok || !st.error || st.error === "off" ? null : el("div", { class: "muted small" }, st.error)),
      el("td", { class: "small", style: "word-break:break-all" }, s.kind === "builtin" ? (s.address || "") : `${s.url}${s.health || ""}`),
      el("td", {}, s.kind === "builtin" ? el("a", { class: "btn ghost small", href: `${BASE}/settings#connections` }, "Settings")
        : s.from_env ? el("span", { class: "muted small", title: "Set by UPSTREAM_SERVICES in the env file" }, "from .env")
        : el("div", { class: "row", style: "gap:4px" }, el("button", { class: "btn ghost small", onclick: () => serviceDialog(s, r.repos) }, "Edit"),
            el("button", { class: "btn ghost small danger", onclick: async () => { if (!confirm(`Stop tracking ${s.name}? (The container itself is not touched.)`)) return
              const x = await api(apiUrl("/api/services/delete"), { method: "POST", body: JSON.stringify({ id: s.id }) }).catch(e => ({ ok: false, error: e.message }))
              if (!x.ok) return toast(x.error, "error"); renderIntegrations() } }, "Remove")))))
  }
  const addBtn = el("button", { class: "btn small", onclick: () => serviceDialog(null, r.repos) }, "+ Add a service")
  const findBtn = el("button", { class: "btn ghost small" }, "Find running containers")
  const found = el("div")
  findBtn.onclick = () => busy(findBtn, "Looking…", async () => {
    const c = await api(apiUrl("/api/services/containers")).catch(e => ({ available: false, why: e.message, items: [] }))
    if (!c.available) return found.replaceChildren(el("p", { class: "muted small" }, `Can't list containers: ${c.why}. Add them by address instead.`))
    found.replaceChildren(el("div", { class: "table-box" }, el("table", { class: "list" },
      el("thead", {}, el("tr", {}, el("th", {}, "Container"), el("th", {}, "Image"), el("th", {}, "Ports"), el("th", {}, ""))),
      el("tbody", {}, ...c.items.map(x => el("tr", {}, el("td", {}, x.name, el("div", { class: "muted small" }, x.status)), el("td", { class: "small" }, x.image),
        el("td", { class: "small" }, x.ports.join(", ") || "none published"),
        el("td", {}, x.added ? el("span", { class: "muted small" }, "added") : el("button", { class: "btn ghost small", onclick: () => serviceDialog({ name: x.name, url: x.url || "", container: x.name, repo: x.repo }, r.repos) }, "Add")))),
        c.items.length ? null : el("tr", {}, el("td", { colspan: "4", class: "muted" }, "No containers are running."))))))
  })
  // 2. repos and their marks
  const repoBody = el("tbody")
  for (const x of r.repos) {
    const sel = el("select", { "aria-label": `How far ${x.name} is integrated` }, el("option", { value: "" }, "Not marked"),
      ...Object.entries(r.marks).map(([k, v]) => el("option", { value: k }, v)))
    sel.value = x.auto || x.from_env ? "" : x.state
    const note = el("input", { value: x.note || "", placeholder: "what works, what's missing…", style: "width:100%", "aria-label": `Note for ${x.name}` })
    const saveMark = async () => {
      const y = await api(apiUrl(`/api/repo/${encodeURIComponent(x.id)}/integration`), { method: "POST", body: JSON.stringify({ state: sel.value, note: note.value }) }).catch(e => ({ ok: false, error: e.message }))
      toast(y.ok ? `${x.name}: ${r.marks[sel.value] || "not marked"}` : y.error, y.ok ? "ok" : "error")
    }
    sel.onchange = saveMark; note.onchange = () => { if (!sel.value) sel.value = "planned"; saveMark() }
    repoBody.append(el("tr", {},
      el("td", {}, repoLink(x.id, x.name), x.auto ? el("div", { class: "muted small" }, "integrated (it runs a connection below)")
        : x.from_env ? el("div", { class: "muted small" }, "marked in the env file (UPSTREAM_MARKS); a mark here wins") : null),
      el("td", {}, sel),
      el("td", {}, x.services.length ? el("div", { class: "row", style: "gap:4px;flex-wrap:wrap" }, ...x.services.map(s => badge(!s.set_up ? "" : s.ok ? "up" : "bad", s.set_up ? s.name : `${s.name} (off)`, !s.set_up ? "not set up here" : s.ok ? "works" : "not working"))) : el("span", { class: "muted small" }, "-")),
      el("td", { style: "min-width:200px" }, note)))
  }
  const counts = Object.fromEntries(Object.keys(r.marks).map(k => [k, r.repos.filter(x => x.state === k).length]))
  main.replaceChildren(
    el("div", { class: "row", style: "gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:6px" }, el("h1", { class: "page", style: "margin:0" }, "Integrations"),
      el("span", { class: "muted" }, "What this setup connects to, whether it works right now, and how far each repo is integrated. Agents can read this too."),
      el("div", { class: "spacer" }), el("button", { class: "btn ghost small", onclick: () => renderIntegrations() }, "Check again")),
    el("div", { class: "card" }, el("div", { class: "row", style: "gap:8px;align-items:center;flex-wrap:wrap" }, el("h3", { style: "margin:0" }, "Connections and services"),
        el("div", { class: "spacer" }), findBtn, addBtn),
      el("p", { class: "muted small" }, "Add your other containers (or any service) by address; each is checked when you open this page. A tcp:// address only checks the port answers."),
      el("div", { class: "table-box" }, el("table", { class: "list" }, el("thead", {}, el("tr", {}, el("th", {}, "Name"), el("th", {}, "Repo"), el("th", {}, "Works"), el("th", {}, "Address"), el("th", {}, ""))), svcBody)), found),
    el("div", { class: "card", style: "margin-top:14px" }, el("h3", { style: "margin-top:0" }, "Your repos"),
      el("p", { class: "muted small" }, `${counts.integrated} integrated · ${counts.partial} partly · ${counts.planned} to integrate · ${counts.none} not needed. Mark each one; a note says what works or what is missing.`),
      el("div", { class: "table-box" }, el("table", { class: "list" }, el("thead", {}, el("tr", {}, el("th", {}, "Repo"), el("th", {}, "Integration"), el("th", {}, "Runs"), el("th", {}, "Note"))), repoBody))))
  main.append(await agentsCard())
}

// How coding agents reach Upstream's tools: on this machine (stdio), and over the network when it is on.
async function agentsCard() {
  const m = await api(apiUrl("/api/mcp")).catch(() => null)
  const card = el("div", { class: "card", style: "margin-top:14px" }, el("h3", { style: "margin-top:0" }, "Coding agents (MCP)"))
  if (!m) return card
  const code = (t) => el("pre", { class: "small mono", style: "white-space:pre-wrap;margin:4px 0" }, t)
  card.append(el("div", { class: "small" }, "On this machine: ", el("code", {}, m.stdio), " (stdio), e.g. ", el("code", {}, "claude mcp add upstream -- upstream-mcp")))
  if (m.http_url) card.append(el("div", { class: "small", style: "margin-top:8px" }, "Over the network: ", el("code", {}, m.http_url), " ",
      m.token_set ? badge("up", "token required") : badge("warn", "no token: this machine only")),
    code(`claude mcp add --transport http upstream ${m.http_url} --header "Authorization: Bearer $UPSTREAM_MCP_TOKEN"`))
  else card.append(el("p", { class: "muted small" }, "Over the network: off. Set UPSTREAM_MCP_PORT (and UPSTREAM_MCP_HOST=0.0.0.0 with UPSTREAM_MCP_TOKEN) in .env to let agents on other machines use these tools."))
  return card
}

function serviceDialog(s, repos) {
  s = s || {}
  const name = el("input", { value: s.name || "", placeholder: "e.g. firefly-iii", style: "width:100%", "aria-label": "Name" })
  const url = el("input", { value: s.url || "", placeholder: "http://127.0.0.1:8080  or  tcp://127.0.0.1:5432", style: "width:100%", "aria-label": "Address" })
  const health = el("input", { value: s.health || "", placeholder: "/health (optional; / otherwise)", style: "width:100%", "aria-label": "Health path" })
  const repo = el("select", { "aria-label": "Built from repo" }, el("option", { value: "" }, "No repo"), ...repos.map(x => el("option", { value: x.id }, x.name)))
  repo.value = s.repo || ""
  const notes = el("input", { value: s.notes || "", placeholder: "notes (optional)", style: "width:100%", "aria-label": "Notes" })
  const go = el("button", { class: "btn" }, s.id ? "Save" : "Add")
  let close
  go.onclick = () => busy(go, "Checking…", async () => {
    const x = await api(apiUrl("/api/services"), { method: "POST", body: JSON.stringify({ id: s.id, name: name.value, url: url.value, health: health.value, repo: repo.value, container: s.container, notes: notes.value }) }).catch(e => ({ ok: false, error: e.message }))
    if (!x.ok) return toast(x.error, "error")
    close(); toast(x.status.ok ? `${x.item.name} works` : `${x.item.name} added, but it doesn't answer: ${x.status.error}`, x.status.ok ? "ok" : "info"); renderIntegrations()
  })
  close = openDialog(s.id ? `Edit ${s.name}` : "Add a service", el("div", { class: "col", style: "gap:8px;min-width:min(480px,80vw)" },
    el("label", { class: "col", style: "gap:4px" }, "Name", name), el("label", { class: "col", style: "gap:4px" }, "Address", url),
    el("label", { class: "col", style: "gap:4px" }, "Health path", health), el("label", { class: "col", style: "gap:4px" }, "Built from", repo), notes, el("div", {}, go)))
  setTimeout(() => name.focus(), 0)
}

// Attach a reference to a chat: a general one, or one about a repo.
// Every folder of the reference library (not inside cloned repos), for picking where to link something.
async function refFolders(path = "", depth = 0, out = []) {
  if (depth > 5 || out.length > 400) return out
  const r = await api(apiUrl(`/api/refs?path=${encodeURIComponent(path)}`)).catch(() => ({ items: [] }))
  for (const i of r.items || []) {
    if (i.kind !== "folder" || i.link_to) continue
    out.push(i.path); await refFolders(i.path, depth + 1, out)
  }
  return out
}

// "Also put it in another folder": one copy on disk, shown in as many folders as want it (a symlink).
// Your repos are offered as folders too, so a reference several repos use is one click per repo.
async function linkDialog(item, done) {
  const [folders, repos, links] = await Promise.all([refFolders(),
    api(apiUrl("/api/repos")).then(r => Array.isArray(r) ? r : r.repos || []).catch(() => []),
    api(apiUrl(`/api/refs/links?path=${encodeURIComponent(item.link_to || item.path)}`)).then(r => r.items || []).catch(() => [])])
  const parent = item.path.split("/").slice(0, -1).join("/")
  const already = new Set([parent, ...links.map(l => l.split("/").slice(0, -1).join("/")), (item.link_to || "").split("/").slice(0, -1).join("/")])
  const own = item.kind === "folder" ? (f) => f === item.path || f.startsWith(item.path + "/") : () => false
  const free = folders.filter(f => !already.has(f) && !own(f))
  const repoFolders = repos.map(r => r.name || r.id).filter(n => !folders.includes(n) && n !== item.name)
  const sel = el("select", { "aria-label": "Folder", style: "width:100%" },
    el("option", { value: "" }, "Choose a folder…"),
    free.length ? el("optgroup", { label: "Folders" }, ...free.map(f => el("option", { value: f }, f))) : null,
    repoFolders.length ? el("optgroup", { label: "A new folder for one of your repos" }, ...repoFolders.map(n => el("option", { value: `new:${n}` }, `${n} (new folder)`))) : null,
    el("option", { value: "new:" }, "Another new folder…"))
  const go = el("button", { class: "btn" }, "Link it")
  let close
  go.onclick = () => busy(go, "Linking…", async () => {
    let into = sel.value
    if (!into) return toast("Choose a folder", "error")
    if (into.startsWith("new:")) {
      const name = into.slice(4) || prompt("New folder (in the library):") || ""
      if (!name.trim()) return
      const f = await api(apiUrl("/api/refs/folder"), { method: "POST", body: JSON.stringify({ parent: "", name: name.trim() }) }).catch(e => ({ ok: false, error: e.message }))
      if (!f.ok && !/exists/i.test(f.error || "")) return toast(f.error, "error")
      into = f.path || name.trim()
    }
    const r = await api(apiUrl("/api/refs/link"), { method: "POST", body: JSON.stringify({ target: item.path, into }) }).catch(e => ({ ok: false, error: e.message }))
    if (!r.ok) return toast(r.error, "error")
    close(); toast(`${item.name} now also shows in ${into}`, "ok"); await done?.()
  })
  close = openDialog(`Also show “${item.name}” in…`, el("div", { class: "col", style: "gap:10px;min-width:min(420px,80vw)" },
    el("p", { class: "muted", style: "margin:0" }, "It stays one copy on disk (updated once); the folder gets a link to it." +
      (links.length ? ` Already shown in: ${links.join(", ")}.` : "")),
    sel, el("div", {}, go)))
}

async function useInChat(path, label) {
  const repos = await api(apiUrl("/api/repos")).catch(() => [])
  const list = Array.isArray(repos) ? repos : repos.repos || []
  const sel = el("select", { "aria-label": "Which chat" }, el("option", { value: GENERAL }, "A general chat"), ...list.map(r => el("option", { value: r.id }, `${r.name || r.id}'s chat`)))
  const go = el("button", { class: "btn" }, "Attach and open the chat")
  let close
  go.onclick = () => {
    const key = sel.value, tags = loadTags(key)
    if (!tags.some(t => t.type === "ref" && t.id === path)) {
      if (tags.length >= MAX_TAGS) return toast(`That chat already has ${MAX_TAGS} attachments; remove one first.`, "error")
      tags.push({ type: "ref", id: path, label: `📚 ${label}` }); saveTags(key, tags)
    }
    close()
    route(key === GENERAL ? `/chats?repo=${GENERAL}` : `/repo/${key}`)
    toast(`${label} goes with your next message`, "ok")
  }
  close = openDialog(`Use “${label}” in a chat`, el("div", { class: "col", style: "gap:10px;min-width:min(420px,80vw)" },
    el("p", { class: "muted", style: "margin:0" }, "The model reads it with your next messages: a PDF's text, a zip's file list, a folder's tree and READMEs; pictures go to models that can see (Settings → model)."),
    sel, el("div", {}, go)))
}

// The Work tab on a repo page: that repo's tasks.
async function renderRepoWork(pane, repoId) {
  const r = await api(apiUrl(`/api/tasks?repo=${encodeURIComponent(repoId)}`))
  const items = (r.items || []).filter(t => !["done", "dropped"].includes(t.status))
  pane.replaceChildren(el("div", { class: "row", style: "margin-bottom:8px;gap:8px" }, el("strong", {}, `Tasks (${items.length} active)`), el("div", { class: "spacer" }),
      el("button", { class: "btn ghost small", onclick: () => route(`/work?repo=${encodeURIComponent(repoId)}`) }, "All on the Work page"),
      el("button", { class: "btn small", onclick: () => taskDialog({ repo: repoId }) }, "+ New task")),
    ...items.map(t => taskRow(t, (x) => route(`/work?task=${x.id}`), false)),
    items.length ? null : el("p", { class: "muted" }, "No active tasks. Use “Work on” next to a commit, or + New task."))
}

// Settings: where tasks can go.
async function integrationsSetup() {
  const box = el("div")
  const ints = await loadIntegrations(true)
  const h = ints.hermes || {}, o = ints.orb || {}
  const hOn = el("input", { type: "checkbox", style: "height:auto" }); hOn.checked = !!h.enabled
  const hUrl = el("input", { value: h.url || "", placeholder: "http://127.0.0.1:8642", style: "width:100%" })
  const hProfile = el("input", { value: h.profile || "", placeholder: "(default profile)", style: "width:100%" })
  const hModel = el("input", { value: h.model || "", placeholder: "(Hermes' own default)", style: "width:100%" })
  const hKey = el("input", { type: "password", autocomplete: "off", placeholder: h.key_set ? "•••• set (type to replace)" : "API_SERVER_KEY from Hermes", style: "width:100%" })
  const hd = ints.herdr || {}, st = ints.studio || {}
  const hdOn = el("input", { type: "checkbox", style: "height:auto" }); hdOn.checked = !!hd.enabled
  const hdSock = el("input", { value: (hd.socket || "").startsWith("/") && hd.enabled ? hd.socket : "", placeholder: hd.socket || "~/.config/herdr/herdr.sock", style: "width:100%" })
  const hdMachine = el("select", { "aria-label": "Where agents start" }, el("option", { value: "" }, "this machine"), ...(hd.machines || []).map(m => el("option", { value: m.label }, m.label)))
  if (hd.machine && ![...hdMachine.options].some(o => o.value === hd.machine)) hdMachine.append(el("option", { value: hd.machine }, hd.machine))
  hdMachine.value = hd.machine || ""
  const hdMap = el("input", { value: hd.path_map || "", placeholder: "/home/me/repos=/srv/repos (only if folders differ over there)", style: "width:100%" })
  const hdKind = el("select", { "aria-label": "Agent to start" }, ...(hd.kinds || ["hermes", "claude", "codex", "opencode"]).map(k => el("option", { value: k }, `${k}${hd.installed && !hd.installed.includes(k) ? " (not found here)" : ""}`)))
  hdKind.value = hd.kind || "hermes"
  const stOn = el("input", { type: "checkbox", style: "height:auto" }); stOn.checked = !!st.enabled
  const stUrl = el("input", { value: st.url || "", placeholder: "http://127.0.0.1:8648", style: "width:100%" })
  const stAgent = el("select", { "aria-label": "Studio agent" }, el("option", { value: "" }, "Hermes (Studio's own)"), ...["claude-code", "codex", "opencode", "pi", "grok"].map(k => el("option", { value: k }, k)))
  stAgent.value = st.agent || ""
  const stUser = el("input", { placeholder: "Studio username", autocomplete: "off", style: "width:100%" })
  const stPass = el("input", { type: "password", placeholder: st.token_set ? "•••• signed in (sign in again to replace)" : "password", autocomplete: "off", style: "width:100%" })
  const oUrl = el("input", { value: o.url || "", placeholder: "http://192.168.20.5:8095", style: "width:100%" })
  const oList = el("input", { value: o.list || "Inbox", style: "width:100%" })
  const oKey = el("input", { type: "password", autocomplete: "off", placeholder: o.token_set ? "•••• set (type to replace)" : "an ORB_API_TOKENS token (if Orb uses them)", style: "width:100%" })
  const line = (st, okText) => el("div", { class: "small", style: `color:${st.ok ? "var(--good)" : "var(--muted)"}` }, st.ok ? okText : `Not connected: ${st.error || "not set up"}`)
  const save = el("button", { class: "btn" }, "Save")
  save.onclick = () => busy(save, "Saving…", async () => {
    const keys = {}
    if (hKey.value.trim()) keys.hermes_api_key = hKey.value.trim()
    if (oKey.value.trim()) keys.orb_token = oKey.value.trim()
    if (Object.keys(keys).length) await api(apiUrl("/api/credentials"), { method: "POST", body: JSON.stringify(keys) })
    const r = await api(apiUrl("/api/integrations/settings"), { method: "POST", body: JSON.stringify({ hermes: { enabled: hOn.checked, url: hUrl.value, profile: hProfile.value, model: hModel.value },
      herdr: { enabled: hdOn.checked, socket: hdSock.value, kind: hdKind.value, machine: hdMachine.value, path_map: hdMap.value }, studio: { enabled: stOn.checked, url: stUrl.value, agent: stAgent.value },
      ...(stUser.value.trim() ? { studio_login: { username: stUser.value.trim(), password: stPass.value } } : {}),
      orb: { url: oUrl.value, list: oList.value } }) }).catch(e => ({ ok: false, error: e.message }))
    if (r.ok === false) return toast(r.error, "error")
    toast("Saved", "ok"); box.replaceWith(await integrationsSetup())
  })
  const mcpCmd = `hermes mcp add upstream --command upstream-mcp --env UPSTREAM_APP_URL=${location.origin}${BASE}`
  const ef = ints.env_file || {}
  const dispatchIn = el("input", { type: "checkbox", style: "height:auto" }); dispatchIn.checked = ints.agents_dispatch !== false
  dispatchIn.onchange = async () => {
    const r = await api(apiUrl("/api/integrations/settings"), { method: "POST", body: JSON.stringify({ agents_dispatch: dispatchIn.checked }) }).catch(e => ({ ok: false, error: e.message }))
    toast(r.ok === false ? r.error : dispatchIn.checked ? "Agents may hand tasks to other agents" : "Only you hand tasks to agents now", r.ok === false ? "error" : "ok")
  }
  box.append(el("h4", { style: "margin-top:20px" }, "Work"),
    el("label", { class: "row", style: "gap:6px" }, dispatchIn, `Agents (in Hermes Studio, herdr, Claude Code…) may hand tasks to herdr, Studio or Hermes through Upstream Manager`),
    el("div", { class: "muted small" }, `At most ${ints.agents_dispatch_limit || 5} agent-sent tasks run at once. Each still works on its own branch; you review, commit and approve. Answering an agent's questions stays with you.`),
    el("h4", { style: "margin-top:20px" }, "Connections"),
    el("div", { class: "muted", style: ef.missing ? "color:var(--bad)" : null }, ef.missing ? `The env file named by UPSTREAM_ENV_FILE isn't there: ${ef.missing}. No connections were read from a file.` : ef.path ? `Reading ${ef.keys.length} setting${ef.keys.length === 1 ? "" : "s"} from ${ef.path}${ef.keys.length ? ` (${ef.keys.join(", ")})` : ""}. What you save below wins over the file.`
      : `Every connection below can also be set in an env file: copy .env.example to .env (${(ef.looked || []).join(" or ")}) and restart. What you save here wins over the file.`),
    el("h4", { style: "margin-top:20px" }, "Hermes"),
    el("div", { class: "muted" }, "Hand bigger or longer tasks to a Hermes agent. Turn on Hermes' API server (API_SERVER_ENABLED=true and an API_SERVER_KEY), then point this at it. Each task gets its own branch; Hermes works there and you approve the result here."),
    el("div", { class: "col", style: "margin-top:8px" }, el("label", { class: "row" }, hOn, "Use Hermes"),
      el("label", { class: "col", style: "gap:4px" }, "API server address", hUrl),
      el("div", { class: "row", style: "gap:8px" }, el("label", { class: "col", style: "gap:4px;flex:1" }, "Profile", hProfile), el("label", { class: "col", style: "gap:4px;flex:1" }, "Model", hModel)),
      el("label", { class: "col", style: "gap:4px" }, "API key (kept with your accounts)", hKey), line(h, `Connected to ${h.url}${h.profile ? ` (profile ${h.profile})` : ""}.`),
      el("div", { class: "small muted" }, "So Hermes can see repos and hand changes back for approval, give it Upstream Manager's tools (run on the Hermes machine):"),
      el("pre", { class: "code-pre", style: "white-space:pre-wrap" }, mcpCmd)),
    el("h4", { style: "margin-top:20px" }, "herdr"),
    el("div", { class: "muted" }, "Start a coding agent (Claude Code, Codex, Hermes…) in herdr, on the task's branch. herdr is reached over its local socket, so it must run as you on this machine; it can hand work on to other machines itself (herdr --machine)."),
    el("div", { class: "col", style: "margin-top:8px" }, el("label", { class: "row" }, hdOn, "Use herdr"),
      el("div", { class: "row", style: "gap:8px" }, el("label", { class: "col", style: "gap:4px;flex:1" }, "Socket (empty for the default)", hdSock), el("label", { class: "col", style: "gap:4px" }, "Agent to start", hdKind)),
      el("div", { class: "row", style: "gap:8px" }, el("label", { class: "col", style: "gap:4px" }, "Start agents on", hdMachine),
        el("label", { class: "col", style: "gap:4px;flex:1" }, "Folder map for other machines", hdMap)),
      line(hd, hd.machine ? `Connected to herdr on ${hd.machine}, through this machine's herdr.` : `Connected to herdr ${hd.version || ""} on this machine${hd.auto ? " (found automatically)" : ""}${(hd.machines || []).length ? `; it also reaches ${hd.machines.map(m => m.label).join(", ")}` : ""}.`),
      el("div", { class: "muted small" }, "Same machine: nothing to set up, herdr is used as soon as it runs. Another machine: add it to herdr once (herdr machine add <ssh-host>) and it shows here; tasks then go through herdr's own connection."),
      el("details", {}, el("summary", { class: "muted small" }, "Give herdr's agents Upstream Manager's tools (once per agent kind)"),
        el("pre", { class: "code-pre", style: "white-space:pre-wrap" }, [
          `# Claude Code\nclaude mcp add upstream -e UPSTREAM_APP_URL=${location.origin}${BASE} -- upstream-mcp`,
          `# Codex\ncodex mcp add upstream --env UPSTREAM_APP_URL=${location.origin}${BASE} -- upstream-mcp`,
          `# Hermes (the same Hermes that Studio shows)\nhermes mcp add upstream --command upstream-mcp --env UPSTREAM_APP_URL=${location.origin}${BASE}\nherdr integration install hermes   # lets herdr see Hermes' state and session`].join("\n\n")),
        el("div", { class: "muted small" }, "Each task's workspace also carries UPSTREAM_TASK_ID and UPSTREAM_REPO_ID, so an agent knows which task it is on."))),
    el("h4", { style: "margin-top:20px" }, "Hermes Studio"),
    el("div", { class: "muted" }, "Hand a task to a Studio run in the task's folder; follow it here or in Studio's own chat."),
    el("div", { class: "col", style: "margin-top:8px" }, el("label", { class: "row" }, stOn, "Use Hermes Studio"),
      el("div", { class: "row", style: "gap:8px" }, el("label", { class: "col", style: "gap:4px;flex:1" }, "Studio address", stUrl), el("label", { class: "col", style: "gap:4px" }, "Agent", stAgent)),
      el("div", { class: "row", style: "gap:8px" }, el("label", { class: "col", style: "gap:4px;flex:1" }, "Sign in (kept as a token with your accounts)", stUser), el("label", { class: "col", style: "gap:4px;flex:1" }, "\u00a0", stPass)),
      line(st, `Connected to Studio at ${st.url}${st.agent ? `, using ${st.agent}` : ""}.`)),
    el("h4", { style: "margin-top:20px" }, "Pondering Orb"),
    el("div", { class: "muted" }, "Park a task as a to-do in Orb (it syncs to your CalDAV lists)."),
    el("div", { class: "col", style: "margin-top:8px" }, el("label", { class: "col", style: "gap:4px" }, "Orb address", oUrl),
      el("label", { class: "col", style: "gap:4px" }, "List for parked tasks", oList), el("label", { class: "col", style: "gap:4px" }, "API token", oKey),
      line(o, `Connected; parked tasks go to the ${o.list} list.`), el("div", {}, save)))
  return box
}

// Disk use and cleanup of Upstream Manager's own data. See storage.py.
const fmtBytes = b => b >= 2 ** 30 ? `${(b / 2 ** 30).toFixed(1)} GB` : b >= 2 ** 20 ? `${(b / 2 ** 20).toFixed(1)} MB` : `${Math.ceil((b || 0) / 1024)} KB`
async function storageSetup() {
  const box = el("div")
  box.append(el("h4", { style: "margin-top:20px" }, "Storage"), el("div", { class: "muted" }, "Measuring…"))
  let st
  try { st = await api(apiUrl("/api/storage")) } catch (e) { box.replaceChildren(el("h4", { style: "margin-top:20px" }, "Storage"), el("p", { class: "muted" }, `Unavailable: ${e.message}`)); return box }
  const total = st.repos.reduce((n, r) => n + (r.git || 0) + (r.component_copies || 0), 0)
  const c = st.cleanup, n = c.requests.length + c.component_copies.length + c.workspaces.length
  const rows = st.repos.map(r => el("tr", {},
    el("td", {}, r.name || r.id), el("td", { class: "muted", title: r.path, style: "max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" }, r.path),
    el("td", { style: "text-align:right" }, r.missing ? "missing" : fmtBytes(r.git)),
    el("td", { style: "text-align:right" }, r.component_copies ? fmtBytes(r.component_copies) : "–"),
    el("td", { style: "text-align:right" }, r.workspaces ? fmtBytes(r.workspaces) : "–"),
    el("td", {}, r.partial ? "on demand" : r.missing ? "" : "full")))
  const clean = el("button", { class: "btn", disabled: n ? null : "" }, n ? `Clean up ${n} item${n > 1 ? "s" : ""} (${fmtBytes(c.freed)})` : "Nothing to clean up")
  clean.onclick = () => busy(clean, "Cleaning…", async () => {
    const r = await api(apiUrl("/api/storage/cleanup"), { method: "POST", body: JSON.stringify({}) })
    toast(`Freed ${fmtBytes(r.freed)}`, "ok"); box.replaceWith(await storageSetup())
  })
  box.replaceChildren(el("h4", { style: "margin-top:20px" }, "Storage"),
    el("div", { class: "muted" }, `Upstream Manager's own data (${st.data_dir}): ${fmtBytes(st.app_total)} — settings ${fmtBytes(st.app["state.json"])}, memory ${fmtBytes(st.app.memory)}, workspaces ${fmtBytes(st.app.workspaces)}. Your repos stay where they are; their git folders take ${fmtBytes(total)}. "On demand" repos download file contents only when needed. Component copies are history-only (no files) and live inside each repo's .git.`),
    el("div", { style: "overflow:auto;max-height:320px;margin-top:8px" }, el("table", { class: "tbl", style: "width:100%;font-size:12px" },
      el("thead", {}, el("tr", {}, ...["Repo", "Location", "Git", "Components", "Workspaces", "Contents"].map(h => el("th", { style: "text-align:left" }, h)))),
      el("tbody", {}, ...rows))),
    el("div", { class: "row", style: "margin-top:8px;gap:8px;align-items:center" }, clean,
      el("span", { class: "muted small" }, `Removes decided requests older than 30 days, component copies nothing uses, and workspace folders of removed repos. Never your files, branches or commits.`)))
  return box
}

// The LLM wiki (an Obsidian vault kept by agents): read wiki/, write today's daily note. See wiki.py.
async function wikiSetup() {
  const box = el("div")
  let st
  try { st = await api(apiUrl("/api/wiki")) } catch (e) { box.append(el("p", { class: "muted" }, `LLM wiki settings unavailable: ${e.message}`)); return box }
  const on = el("input", { type: "checkbox", style: "height:auto" }); on.checked = st.enabled
  const mode = el("select", { "aria-label": "How to reach the vault" }, el("option", { value: "folder" }, "A folder on this machine"), el("option", { value: "rest" }, "Obsidian Local REST API"))
  mode.value = st.mode
  const path = el("input", { value: st.path || "", placeholder: "/path/to/LLMWIKI (the vault folder)", style: "width:100%" })
  const url = el("input", { value: st.url || "", placeholder: st.rest_url || "http://192.168.20.5:27123", style: "width:100%" })
  const key = el("input", { type: "password", placeholder: st.key_set ? "•••• set (type to replace)" : "the plugin's API key", style: "width:100%", autocomplete: "off" })
  const read = el("input", { type: "checkbox", style: "height:auto" }); read.checked = st.read
  const rec = el("input", { type: "checkbox", style: "height:auto" }); rec.checked = st.record_tasks !== false
  const prioNote = el("input", { value: st.priorities_note || "", placeholder: "Upstream priorities", style: "width:100%", "aria-label": "Priorities note" })
  // try it: search the long-term notes like an agent would
  const q = el("input", { type: "search", placeholder: "Search wiki/ (as agents do)…", "aria-label": "Search the wiki", style: "flex:1 1 240px" })
  const found = el("div", { class: "col small", style: "gap:4px" })
  q.onkeydown = async (e) => {
    if (e.key !== "Enter" || !q.value.trim()) return
    const r = await api(apiUrl(`/api/wiki/search?q=${encodeURIComponent(q.value)}`)).catch(er => ({ ok: false, error: er.message }))
    found.replaceChildren(...(!r.ok ? [el("span", { class: "muted" }, r.error)] : !r.items.length ? [el("span", { class: "muted" }, "Nothing found.")]
      : r.items.map(n => el("div", {}, el("strong", {}, `[[${n.note}]]`), n.status === "draft" ? badge("warn", "draft") : null, el("span", { class: "muted" }, ` ${n.lines.slice(0, 2).join(" … ")}`)))))
  }
  const pathRow = el("label", { class: "col", style: "gap:4px" }, "Vault folder", path)
  const restRows = el("div", { class: "col" }, el("label", { class: "col", style: "gap:4px" }, "REST API URL", url),
    el("label", { class: "col", style: "gap:4px" }, "API key (kept with your accounts, never shown again)", key))
  const sync = () => { pathRow.hidden = mode.value !== "folder"; restRows.hidden = mode.value !== "rest" }
  mode.onchange = sync; sync()
  const state = el("div", { class: "small", style: `color:${st.ok ? "var(--good)" : st.enabled ? "var(--bad)" : "var(--muted)"}` },
    !st.enabled ? "Off." : st.ok ? `Connected: ${st.notes} long-term notes in wiki/.` : `Not connected: ${st.error}`)
  const save = el("button", { class: "btn" }, "Save")
  save.onclick = () => busy(save, "Saving…", async () => {
    if (key.value.trim()) await api(apiUrl("/api/credentials"), { method: "POST", body: JSON.stringify({ wiki_api_key: key.value.trim() }) })
    const r = await api(apiUrl("/api/wiki/settings"), { method: "POST", body: JSON.stringify({ enabled: on.checked, mode: mode.value, path: path.value, url: url.value, read: read.checked, record_tasks: rec.checked, priorities_note: prioNote.value }) })
    toast(r.ok || !r.enabled ? "Saved" : `Saved, but: ${r.error}`, r.ok || !r.enabled ? "ok" : "error")
    box.replaceWith(await wikiSetup())
  })
  box.append(el("h4", { style: "margin-top:20px" }, "LLM wiki"),
    el("div", { class: "muted" }, "Your agent-kept Obsidian vault (like LLMWIKI). Reading: Upstream Manager gives chats and coding agents what its long-term notes (wiki/) say about each repo, citing the note. Agents can search and read wiki/ too, and the Overseer follows your priorities note there. It never reads the short-term tier as fact, and never writes into wiki/: daily lines, day logs and finished tasks go to the short-term tier (daily note, sources, inbox) for the vault's own workflows."),
    el("div", { class: "col", style: "margin-top:8px" },
      el("label", { class: "row" }, on, "Use the LLM wiki"), el("label", { class: "col", style: "gap:4px" }, "Reach it through", mode), pathRow, restRows,
      el("label", { class: "row" }, read, "Give chats and agents what wiki/ says about a repo (agents can also search and read wiki/)"),
      el("label", { class: "row" }, rec, "Record finished tasks in the vault (a line in the daily note and a note in inbox/, with the task's thread)"),
      el("label", { class: "col", style: "gap:4px" }, "The Overseer follows this note in wiki/ (name repos, #groups and words like urgent, high, low, pause)", prioNote),
      state, el("div", { class: "row" }, save),
      st.ok ? el("div", { class: "row", style: "margin-top:6px" }, q) : null, found,
      el("div", { class: "small", style: `margin-top:6px;color:${st.writer ? "var(--good)" : "var(--muted)"}` }, st.writer
        ? `Writing: on. After each daily check one line goes to today's daily note, and each finished day's logs become a source for the vault's Ingest, through its MCP at ${st.writer_url}.`
        : "Writing: off. Start Upstream Manager with LLMWIKI_MCP_URL (e.g. http://192.168.20.5:27123/mcp) and LLMWIKI_API_KEY to send it each daily check and each finished day's logs (short-term tier only).")))
  return box
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
  const wantRepo = params.get("repo") || chats[0]?.repo_id || GENERAL
  // a repo asked for without a chat: its newest chat (never another repo's)
  let pick = { repo: wantRepo, session: params.get("session") || chats.find(c => c.repo_id === wantRepo)?.id || null }
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
        badge(c.failing ? "" : "up", `${c.passing || 0} passing`), el("a", { href: r.url, target: "_blank", rel: "noopener", class: "small" }, r.forge === "forge" ? "Forge Actions ↗" : "GitHub Actions ↗"),
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
  if (['requests', 'settings', 'chats', 'workflows', 'work', 'references', 'integrations', 'overseer'].includes(parts[0])) return [parts[0]]
  if (!parts.length || parts[0] === 'repos') return ['repos']
  return ['notfound']
}

// Inside Hermes Studio's Upstream tab (same origin, under BASE): a link to a Studio chat opens in
// Studio itself, not inside this frame; and Studio is told which page is shown, so its address bar,
// back button and reloads keep your place.
const EMBEDDED = (() => { try { return window.top !== window && !!BASE && window.top.location.origin === location.origin } catch { return false } })()
if (EMBEDDED) {
  document.documentElement.classList.add("embedded")
  document.addEventListener("click", (e) => {
    const a = e.target.closest?.("a[href]")
    if (!a) return
    let u
    try { u = new URL(a.href, location.href) } catch { return }
    if (u.hash.startsWith("#/hermes/")) { e.preventDefault(); window.top.location.hash = u.hash }   // a Studio page
  }, true)
  const replace = history.replaceState.bind(history)
  history.replaceState = (...args) => { replace(...args); tellStudio() }
  addEventListener("popstate", () => tellStudio())
  window.addEventListener("message", (e) => {   // Studio asks for a page (a deep link, its back button)
    if (e.origin !== location.origin || e.data?.type !== "upstream:open" || typeof e.data.path !== "string") return
    const p = e.data.path.startsWith("/") ? e.data.path : "/" + e.data.path
    if (p !== location.pathname.slice(BASE.length) + location.search) route(p)
  })
}
function tellStudio() {
  if (EMBEDDED) window.parent.postMessage({ type: "upstream:route", path: location.pathname.slice(BASE.length) + location.search, title: document.title }, location.origin)
}

setTimeout(() => refreshWorkCount(), 500)
async function router() {
  const [view, id] = currentView()
  tools = null
  $("#main").classList.toggle("wide", view === "repo" || view === "chats")
  for (const a of $$("nav.main a")) {
    const on = a.dataset.view === (view === "repo" ? "repos" : view)
    a.classList.toggle("on", on)
    if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current")
  }
  document.title = ({ repos: "Repositories", repo: id, chats: "Chats", workflows: "Workflows", requests: "Requests", settings: "Settings", work: "Work", references: "References", integrations: "Integrations", overseer: "Overseer" }[view] || "Not found") + " · Upstream Manager"
  try {
    if (view === "repos") await renderRepos()
    else if (view === "repo") await renderRepoPage(id)
    else if (view === "chats") await renderChats()
    else if (view === "workflows") await renderWorkflows()
    else if (view === "requests") await renderRequests()
    else if (view === "work") await renderWork()
    else if (view === "references") await renderReferences()
    else if (view === "overseer") await renderOverseer()
    else if (view === "integrations") await renderIntegrations()
    else if (view === "settings") await renderSettings()
    else $("#content").replaceChildren(el("div", { class: "card", style: "max-width:520px;margin:40px auto;text-align:center" },
      el("h1", { class: "page" }, "Page not found"), el("p", { class: "muted" }, `There is no page at ${location.pathname}.`),
      el("a", { href: `${BASE}/repos`, class: "btn" }, "Go to your repos")))
  } catch (e) { $("#content").replaceChildren(el("p", { class: "muted" }, `Could not load this page: ${e.message}`)) }
  refreshPendingCount()
  tellStudio()
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

// ---- notifications: the bell and its feed
async function refreshBell() {
  const r = await api(apiUrl("/api/notifications?limit=1")).catch(() => null)
  if (r) $("#bell-count").textContent = r.unread || ""
}
async function toggleBell() {
  const open = document.querySelector(".bell-panel")
  if (open) { open.remove(); $("#bell").setAttribute("aria-expanded", "false"); return }
  const r = await api(apiUrl("/api/notifications?limit=60")).catch(e => ({ items: [], error: e.message }))
  const markAll = el("button", { class: "btn ghost small", onclick: async () => { await api(apiUrl("/api/notifications/read"), { method: "POST", body: "{}" }); panel.remove(); refreshBell() } }, "Mark all read")
  const panel = el("div", { class: "bell-panel", role: "dialog", "aria-label": "Notifications" },
    el("div", { class: "row", style: "margin-bottom:6px" }, el("strong", {}, "Notifications"), el("div", { class: "spacer" }), markAll,
      el("a", { href: `${BASE}/settings#notifications`, class: "small", style: "margin-left:8px" }, "Settings")),
    ...(r.items.length ? r.items.map(n => el("a", { class: `note lvl-${n.level}${n.read ? "" : " unread"}`, href: `${BASE}${n.path || "/overseer"}`, onclick: () => {
        api(apiUrl("/api/notifications/read"), { method: "POST", body: JSON.stringify({ ids: [n.id] }) }).then(refreshBell); panel.remove() } },
      el("div", { style: "font-weight:600;font-size:13px" }, n.title),
      n.message ? el("div", { class: "small muted" }, n.message.slice(0, 220)) : null,
      el("div", { class: "small muted" }, `${(n.at || "").slice(0, 16).replace("T", " ")} · ${n.event}${n.repo ? ` · ${n.repo}` : ""}`)))
      : [el("p", { class: "muted small" }, "Nothing yet. Requests waiting for you, failed checks, agents asking something and the Overseer's alerts show up here.")]))
  document.body.append(panel); $("#bell").setAttribute("aria-expanded", "true")
  const away = (e) => { if (!panel.contains(e.target) && e.target.id !== "bell" && !e.target.closest("#bell")) { panel.remove(); document.removeEventListener("click", away) } }
  setTimeout(() => document.addEventListener("click", away))
}
$("#bell")?.addEventListener("click", toggleBell)
refreshBell(); setInterval(() => { if (!document.hidden) { refreshBell(); refreshPendingCount() } }, 30000)
// The script tag sits at the end of <body>, so the DOM is ready already.
router()
refreshModelStatus()
