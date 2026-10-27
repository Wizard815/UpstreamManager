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

// --- repos page ---
async function renderRepos() {
  const content = $("#content")
  content.innerHTML = ""
  content.append(el("h2", {}, "Watched Repositories"))

  const urlIn = el("input", { placeholder: "https://github.com/Wizard815/DNDWORLDAPP.git", style: "width:320px" })
  const dirIn = el("input", { placeholder: "or /local/path/to/repo", style: "width:320px" })
  const nameIn = el("input", { placeholder: "display name", style: "width:200px" })
  const addBtn = el("button", { class: "btn", style: "align-self:flex-end" }, "Add / Watch")
  addBtn.onclick = async () => {
    const url = urlIn.value.trim()
    const dir = dirIn.value.trim()
    if (!url && !dir) return toast("Enter a git URL or local path", "error")
    await busy(addBtn, url ? "Cloning…" : "Adding…", async () => {
      await api(apiUrl('/api/add'), { method: "POST", body: JSON.stringify({ url: url || null, path: dir || null, name: nameIn.value.trim() }) })
      toast("Repo added", "ok")
      await loadRepos(list)
    })
  }
  content.append(el("div", { class: "repo-card", style: "margin-bottom:24px" },
    el("h3", {}, "Add or watch a repo"),
    el("div", { style: "display:flex;gap:8px;flex-wrap:wrap;margin-top:8px" }, urlIn, dirIn, nameIn, addBtn),
    el("p", { class: "muted" }, "Drop a repo into the watch folder, or paste a git URL like https://github.com/Wizard815/DNDWORLDAPP.git. Track multiple upstreams per repo (e.g. origin + a llamacpp mirror).")
  ))

  const list = el("div", {}, loading("Checking repos…"))
  content.append(list)
  await loadRepos(list)
}

async function loadRepos(container) {
  const list = container || $("#content")
  try {
    const repos = await api(apiUrl('/api/repos'))
    list.innerHTML = ""
    if (!repos.length) { list.append(el("p", { class: "muted" }, "No repos yet. Add one above or drop a repo into the watch folder.")); return; }
    for (const r of repos) list.append(await repoCard(r))
    const runBtn = el("button", { class: "btn", style: "margin:12px 0" }, "▶ Run Daily Check")
    runBtn.onclick = () => busy(runBtn, "Running…", async () => {
      const res = await api(apiUrl('/api/run-daily'), { method: "POST" })
      const n = (s) => res.results.filter(r => r.status === s).length
      toast(`Done: ${n("merged")} fast-forwarded, ${n("request_raised")} requests raised, ${n("error")} errors`, n("error") ? "error" : "ok")
      await loadRepos(list)
    })
    const fetchBtn = el("button", { class: "btn ghost", style: "margin:12px 0 12px 8px" }, "Fetch all upstreams")
    fetchBtn.onclick = () => busy(fetchBtn, "Fetching…", async () => {
      const failed = []
      for (const r of repos) await api(apiUrl(`/api/repo/${r.id}/fetch`), { method: "POST" }).catch(() => failed.push(r.name || r.id))
      if (failed.length) toast(`Fetch failed for ${failed.join(", ")}`, "error")
      await loadRepos(list)
    })
    list.append(runBtn, fetchBtn)
  } catch (e) { toast(String(e), "error") }
}

const badge = (cls, txt, title) => el("span", { class: `badge ${cls}`, title: title || null }, txt)

// ↓ behind = commits the upstream has that you lack; ↑ ahead = commits only you have.
function upstreamBadges(u) {
  if (u.missing_ref) return [badge("warn", "not fetched", u.conflict_detail || "press Fetch")]
  const out = []
  if (u.up_to_date) out.push(badge("up", "up to date"))
  else {
    out.push(badge(u.behind ? "warn" : "", `↓ ${u.behind} behind`, "commits the upstream has that you lack"))
    out.push(badge("", `↑ ${u.ahead} ahead`, "commits only you have"))
  }
  if (u.conflict) out.push(badge("bad", `⚠ ${(u.conflict_files || []).length} conflicting files`, (u.conflict_files || []).join("\n")))
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

async function repoCard(repo) {
  const ups = repo.upstream_details || []
  const lc = repo.last_commit
  return el("div", { class: "repo-card", id: `repo-${repo.id}` },
    el("div", { style: "display:flex;justify-content:space-between;align-items:center;gap:12px" },
      el("h3", { style: "margin:0" }, repo.name || repo.id, " ", badge("", repo.focus_branch || "")),
      el("button", { class: "btn ghost", onclick: () => route(`/repo/${repo.id}`) }, "Open")),
    el("div", { class: "muted" }, `${repo.kind} · ${repo.path}`),
    lc ? el("div", { class: "muted" }, `last commit ${lc.sha} · ${(lc.date || "").slice(0, 10)} · ${lc.subject}`) : null,
    ...ups.map(u => el("div", { style: "margin-top:8px" }, el("strong", {}, `${u.name}/${u.branch} `), ...upstreamBadges(u))),
    !ups.length && !repo.error ? el("div", { class: "muted", style: "margin-top:8px" }, "No upstream configured.") : null,
    (repo.components || []).length ? el("div", { style: "margin-top:8px" },
      el("strong", {}, `components (${repo.components.length}) `),
      ...repo.components.map(c => componentBadge(c))) : null,
    repo.dirty ? el("div", { style: "margin-top:6px" }, badge("warn", `${repo.dirty} uncommitted change${repo.dirty > 1 ? "s" : ""}`)) : null,
    repo.error ? el("div", { style: "color:#e08080;margin-top:6px" }, repo.error) : null
  )
}

// ---- tagged items: commits/files the user pins to the chat so the model can read them
const loadTags = (id) => { try { return JSON.parse(localStorage.getItem(`tags:${id}`) || "[]") } catch { return [] } }
const saveTags = (id, t) => { try { localStorage.setItem(`tags:${id}`, JSON.stringify(t)) } catch {} }
// The server reads at most this many attachments per message (llm.MAX_ATTACHMENTS).
const MAX_TAGS = 6
let refreshTray = () => {}
let refreshCommits = () => {}
function addTag(repoId, tag) {
  const t = loadTags(repoId)
  if (t.some(x => x.type === tag.type && x.id === tag.id)) return toast(`${tag.label} is already tagged`, "info")
  if (t.length >= MAX_TAGS) return toast(`You can tag up to ${MAX_TAGS} items. Remove one from the chat tray first.`, "error")
  t.push(tag)
  saveTags(repoId, t)
  refreshTray()
  toast(`Tagged ${tag.label} for the chat`, "ok")
}

async function renderRepoPage(repoId) {
  const content = $("#content")
  content.replaceChildren(loading())
  let repo
  try { repo = await api(apiUrl(`/api/repo/${repoId}`)); }
  catch (e) { content.replaceChildren(el("p", { class: "muted" }, e.status === 404 ? "Repo not found." : e.message)); return; }
  content.innerHTML = ""

  content.append(el("a", { href: `${BASE}/repos`, style: "color:#9fb4ff", onclick: (e) => { e.preventDefault(); route('/repos') } }, "← All repos"))

  const focusSel = el("select", { id: `focus-${repoId}` })
  const branches = await repoBranches(repoId)
  for (const b of branches) focusSel.append(el("option", { value: b }, b))
  focusSel.value = repo.focus_branch
  focusSel.onchange = async () => {
    focusSel.disabled = true
    try {
      await api(apiUrl(`/api/repo/${repoId}/focus`), { method: "POST", body: JSON.stringify({ branch: focusSel.value }) })
      toast("Focus branch updated", "ok")
      await renderRepoPage(repoId)
    } catch (e) { toast(e, "error"); focusSel.disabled = false }
  }

  content.append(el("div", { class: "grid", style: "margin-top:16px" },
    el("div", {}, el("h2", {}, repo.name || repoId), el("div", { class: "muted" }, repo.path)),
    el("div", {},
      el("label", { for: `focus-${repoId}`, style: "display:block;margin-bottom:4px" }, "Focus branch"),
      focusSel
    )
  ))

  const upWrap = el("div", { style: "margin-top:20px" }, loading("Comparing upstreams…"))
  content.append(upWrap)
  await refreshUpstreams(upWrap, repoId).catch(e => upWrap.replaceChildren(el("p", { class: "muted" }, `Could not compare upstreams: ${e.message}`)))

  const compWrap = el("div", { style: "margin-top:20px" })
  content.append(compWrap)
  renderComponents(compWrap, repoId).catch(e => compWrap.replaceChildren(el("p", { class: "muted" }, `Could not read components: ${e.message}`)))

  const chatWrap = el("div")
  const toolsWrap = el("div")
  content.append(el("div", { class: "grid repo-grid", style: "margin-top:20px;align-items:start" }, chatWrap, toolsWrap))
  await Promise.all([renderChat(chatWrap, repoId, repo.focus_branch), renderTools(toolsWrap, repoId)])
}

async function repoBranches(repoId) {
  try {
    const res = await api(apiUrl(`/api/repo/${repoId}/branches`))
    return res.branches || []
  } catch { return []; }
}

async function refreshUpstreams(wrap, repoId) {
  const [repo, cmp] = await Promise.all([api(apiUrl(`/api/repo/${repoId}`)), api(apiUrl(`/api/repo/${repoId}/compare`))])
  const byName = Object.fromEntries((cmp.plan?.upstreams || []).map(u => [u.name, u]))
  wrap.innerHTML = ""
  const git = cmp.git || {}
  const fetchAll = el("button", { class: "btn" }, "Fetch all")
  fetchAll.onclick = () => busy(fetchAll, "Fetching…", async () => {
    await api(apiUrl(`/api/repo/${repoId}/fetch`), { method: "POST" })
    await refreshUpstreams(wrap, repoId); refreshCommits()
  })
  wrap.append(el("div", { style: "display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap" },
    el("h3", { style: "margin:0" }, "Upstreams"),
    el("div", { class: "muted" }, git.dirty ? `${git.dirty} uncommitted change(s) in working tree` : "working tree clean"),
    fetchAll))
  for (const u of repo.upstreams || []) {
    const c = byName[u.name]
    const files = c?.conflict_files || []
    const upEl = el("div", { class: "upstream" },
      el("div", { style: "display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap" },
        el("div", {},
          el("strong", {}, `${u.name} / ${u.branch} `),
          ...(u.enabled ? (c ? upstreamBadges(c) : []) : [badge("", "disabled")])
        ),
        el("div", { style: "display:flex;gap:6px;flex-wrap:wrap" },
          el("button", { class: "btn ghost", "aria-label": `Fetch ${u.name}`, onclick: (e) => busy(e.currentTarget, "Fetching…", async () => {
            const r = await api(apiUrl(`/api/repo/${repoId}/upstream/${u.name}/rebase`), { method: "POST" })
            const err = (r.plan?.upstreams || []).find(x => x.name === u.name)?.conflict_detail
            if (err && /fatal|could not|unable/i.test(err)) toast(err, "error")
            await refreshUpstreams(wrap, repoId); refreshCommits()
          }) }, "Fetch"),
          el("button", { class: "btn ghost", "aria-label": `Pull report for ${u.name}`, onclick: (e) => busy(e.currentTarget, "Analysing…", () => showPullReport(repoId, { upstream: u.name })) }, "Pull report"),
          el("button", { class: "btn ghost", "aria-label": `${u.enabled ? "Disable" : "Enable"} ${u.name}`, onclick: (e) => busy(e.currentTarget, null, async () => {
            await api(apiUrl(`/api/repo/${repoId}/upstream/${u.name}/enable`), { method: "POST", body: JSON.stringify({ enabled: !u.enabled }) })
            await refreshUpstreams(wrap, repoId)
          }) }, u.enabled ? "Disable" : "Enable"),
          el("button", { class: "btn danger", "aria-label": `Remove ${u.name}`, onclick: (e) => {
            if (!confirm(`Remove upstream ${u.name}?`)) return
            return busy(e.currentTarget, "Removing…", async () => {
              await api(apiUrl(`/api/repo/${repoId}/upstream/${u.name}`), { method: "DELETE" })
              await refreshUpstreams(wrap, repoId)
            })
          } }, "Remove")
        )
      ),
      el("div", { class: "muted" }, u.url),
      c && c.missing_ref ? el("div", { class: "muted", style: "color:#e0a060" }, c.conflict_detail) : null,
      files.length ? el("details", { style: "margin-top:6px" }, el("summary", {}, `Conflicting files (${files.length})`),
        el("div", { class: "muted", style: "font-family:monospace;font-size:12px" }, ...files.map(f => el("div", {}, f)))) : null
    )
    wrap.append(upEl)
  }
  const nameIn = el("input", { placeholder: "remote name (e.g. upstream)", "aria-label": "Remote name", style: "width:180px" })
  const urlIn = el("input", { placeholder: "https://github.com/org/repo.git", "aria-label": "Remote URL", style: "width:320px" })
  const branchIn = el("input", { placeholder: "its branch (default: focus)", "aria-label": "Upstream branch", style: "width:170px", title: "The upstream's branch to compare against, e.g. main or master" })
  const addUpBtn = el("button", { class: "btn", style: "align-self:flex-end" }, "Add upstream")
  addUpBtn.onclick = () => {
    if (!nameIn.value.trim() || !urlIn.value.trim()) return toast("Enter a remote name and URL", "error")
    return busy(addUpBtn, "Adding…", async () => {
      await api(apiUrl(`/api/repo/${repoId}/upstream`), { method: "POST", body: JSON.stringify({ name: nameIn.value.trim(), url: urlIn.value.trim(), branch: branchIn.value.trim() || null }) })
      toast("Upstream added. Press Fetch to compare.", "ok")
      await refreshUpstreams(wrap, repoId)
    })
  }
  wrap.append(el("div", { style: "display:flex;gap:8px;margin-top:12px;flex-wrap:wrap" }, nameIn, urlIn, branchIn, addUpBtn))
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
async function renderChat(wrap, repoId, focus, sessionId) {
  wrap.replaceChildren(loading("Loading chat…"))
  const sessions = await api(apiUrl(`/api/repo/${repoId}/sessions`))
  const sessSel = el("select", { style: "max-width:240px", "aria-label": "Chat session" })
  for (const s of sessions) sessSel.append(el("option", { value: s.id }, `${s.title || "New chat"} · ${s.count} msgs`))
  const hist = await api(apiUrl(`/api/chat/${repoId}` + (sessionId ? `?session=${sessionId}` : "")))
  if (![...sessSel.options].some(o => o.value === hist.session)) sessSel.append(el("option", { value: hist.session }, "New chat · 0 msgs"))
  sessSel.value = hist.session
  wrap.innerHTML = ""
  sessSel.onchange = async () => {
    await api(apiUrl(`/api/repo/${repoId}/sessions`), { method: "POST", body: JSON.stringify({ session: sessSel.value }) })
    await renderChat(wrap, repoId, focus, sessSel.value)
  }
  const newBtn = el("button", { class: "btn ghost", title: "Start a new conversation. The repo memory is kept." }, "New chat")
  newBtn.onclick = () => busy(newBtn, null, async () => {
    const res = await api(apiUrl(`/api/repo/${repoId}/sessions`), { method: "POST", body: "{}" })
    await renderChat(wrap, repoId, focus, res.session)
  })
  const delBtn = el("button", { class: "btn ghost", title: "Delete this conversation (memory is kept)" }, "Delete chat")
  delBtn.onclick = () => {
    if (!confirm("Delete this chat? The repo memory is kept.")) return
    return busy(delBtn, "Deleting…", async () => {
      const res = await api(apiUrl(`/api/repo/${repoId}/sessions/${hist.session}`), { method: "DELETE" })
      await renderChat(wrap, repoId, focus, res.active)
    })
  }
  wrap.append(
    el("div", { style: "display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap" },
      el("h3", { style: "margin:0" }, `Chat · ${focus}`),
      el("div", { style: "display:flex;gap:6px;flex-wrap:wrap" }, sessSel, newBtn, delBtn)),
  )
  const box = el("div", { class: "chat-box", style: "margin-top:8px", role: "log", "aria-live": "polite", "aria-label": "Conversation", tabindex: "0" })
  const msgIn = el("textarea", { placeholder: "Ask about this repo… (Enter to send, Shift+Enter for a new line)", "aria-label": "Message", rows: "2", style: "width:100%;resize:vertical;margin-top:8px" })
  const sendBtn = el("button", { class: "btn", style: "margin-top:8px" }, "Send")
  const tray = el("div", { class: "tray" })
  refreshTray = () => {
    tray.innerHTML = ""
    const tags = loadTags(repoId)
    if (!tags.length) { tray.append(el("span", { class: "muted" }, "Nothing tagged. Use Tag on a commit or file so the model can read it.")); return }
    tray.append(el("span", { class: "muted" }, `Sent with every message (${tags.length}/${MAX_TAGS}):`))
    for (const t of tags) {
      const x = el("button", { class: "chip-x", title: "Remove", "aria-label": `Remove ${t.label}` }, "×")
      x.onclick = () => { saveTags(repoId, loadTags(repoId).filter(y => !(y.type === t.type && y.id === t.id))); refreshTray() }
      tray.append(el("span", { class: "chip" }, `${t.type === "commit" ? "◆" : "▤"} ${t.label}`, x))
    }
  }
  refreshTray()
  // Replies are Markdown; renderMarkdown escapes everything first (see md.js).
  const setReply = (d, text) => { d.innerHTML = renderMarkdown(text) }
  const addMsg = (m, index) => {
    const d = el("div", { class: `msg ${m.role === "user" ? "user" : "bot"}${m.error ? " err" : ""}` })
    if (m.role === "user" || m.error) d.textContent = m.content
    else setReply(d, m.content)
    if (m.edits?.length) d.append(editCard(repoId, hist.session, index, m))
    if (m.attachments?.length) d.append(el("div", { class: "muted", style: "margin-top:4px;font-size:12px" }, "📎 " + m.attachments.map(a => a.label).join(", ")))
    box.append(d)
    return d
  }
  sendBtn.onclick = async () => {
    const text = msgIn.value.trim()
    if (!text || sendBtn.disabled) return
    const attachments = loadTags(repoId)
    msgIn.value = ""
    sendBtn.disabled = true
    sendBtn.textContent = "Sending…"
    if (!hist.messages?.length) box.querySelector("p.muted")?.remove()
    addMsg({ role: "user", content: text, attachments })
    const replyBox = el("div", { class: "msg bot" }, loading("Thinking…"))
    box.append(replyBox)
    box.scrollTop = box.scrollHeight
    try {
      const res = await api(apiUrl(`/api/repo/${repoId}/chat`), { method: "POST", body: JSON.stringify({ message: text, session: hist.session, attachments }) })
      if (res.ok) { setReply(replyBox, res.reply); if (res.edits?.length) replyBox.append(editCard(repoId, hist.session, res.index, { edits: res.edits })) }
      else { replyBox.textContent = `Error: ${res.error}`; replyBox.classList.add("err") }
    } catch (e) { replyBox.textContent = `Error: ${e.message}`; replyBox.classList.add("err") }
    sendBtn.disabled = false
    sendBtn.textContent = "Send"
    box.scrollTop = box.scrollHeight
  }
  msgIn.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendBtn.click() }
  })
  ;(hist.messages || []).forEach((m, i) => addMsg(m, i))
  if (!(hist.messages || []).length) box.append(el("p", { class: "muted" }, "New conversation. The assistant still knows this repo and what it remembers about it."))
  wrap.append(box, tray, msgIn, sendBtn)
  box.scrollTop = box.scrollHeight
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

// Right-hand tools: commits, files, memory, publish.
function renderTools(wrap, repoId) {
  const names = ["Commits", "Files", "Memory", "Publish"]
  const bar = el("div", { class: "tabs", role: "tablist", "aria-label": "Repo tools" })
  const panes = {}
  const tabs = {}
  const built = {}
  const builders = { Commits: renderCommits, Files: renderFiles, Memory: renderMemory, Publish: renderPublish }
  const show = async (n, focus = false) => {
    for (const k of names) {
      const on = k === n
      panes[k].hidden = !on
      tabs[k].classList.toggle("active", on)
      tabs[k].setAttribute("aria-selected", String(on))
      tabs[k].tabIndex = on ? 0 : -1
    }
    if (focus) tabs[n].focus()
    if (!built[n]) {
      built[n] = true
      panes[n].replaceChildren(loading())
      try { await builders[n](panes[n], repoId) }
      catch (e) { built[n] = false; panes[n].replaceChildren(el("p", { class: "muted" }, `Could not load ${n.toLowerCase()}: ${e.message}`)) }
    }
  }
  for (const n of names) {
    const id = `tab-${repoId}-${n}`
    panes[n] = el("div", { role: "tabpanel", id: `${id}-panel`, "aria-labelledby": id, hidden: "" })
    tabs[n] = el("button", { class: "tab", role: "tab", id, "aria-controls": `${id}-panel`, onclick: () => show(n) }, n)
    // Arrow keys move between tabs (WAI-ARIA tabs pattern).
    tabs[n].addEventListener("keydown", (e) => {
      const i = names.indexOf(n)
      const next = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: names.length - 1 }[e.key]
      if (next === undefined) return
      e.preventDefault()
      show(names[(next + names.length) % names.length], true)
    })
    bar.append(tabs[n])
  }
  wrap.append(bar, ...names.map(n => panes[n]))
  return show("Commits")
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
async function renderFiles(wrap, repoId) {
  const tree = el("div", { class: "tree", role: "tree", "aria-label": "Repository files" }, loading())
  const viewer = el("div", { class: "viewer" }, el("p", { class: "muted", style: "padding:12px" }, "Pick a file."))
  wrap.innerHTML = ""
  wrap.append(el("div", { class: "files" }, tree, viewer))
  const LH = 18
  const openFile = async (path) => {
    viewer.replaceChildren(loading(`Opening ${path}…`))
    const f = await api(apiUrl(`/api/repo/${repoId}/file?path=${encodeURIComponent(path)}`)).catch((e) => ({ ok: false, error: e.status === 404 ? "file not found" : e.message }))
    viewer.innerHTML = ""
    const tag = el("button", { class: "btn ghost" }, "Tag for chat")
    tag.onclick = () => addTag(repoId, { type: "file", id: path, label: path.split("/").slice(-2).join("/") })
    const copy = el("button", { class: "btn ghost" }, "Copy path")
    copy.onclick = () => { navigator.clipboard?.writeText(path); toast("Path copied", "ok") }
    const findIn = el("input", { placeholder: "Find…", "aria-label": "Find in file (Enter for next)", style: "width:110px" })
    const head = el("div", { class: "viewer-head" }, el("strong", { style: "flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis" }, path), findIn, tag, copy)
    viewer.append(head)
    if (!f.ok) { viewer.append(el("p", { class: "muted", style: "padding:12px" }, f.error || "cannot show this file")); return }
    const lines = f.content.split("\n")
    const gutter = el("pre", { class: "gutter" }, lines.map((_, i) => i + 1).join("\n"))
    const src = el("pre", { class: "src" }, f.content)
    const hl = el("div", { class: "hl", style: "display:none" })
    const body = el("div", { class: "code-body", tabindex: "0", "aria-label": `Contents of ${path}` }, el("div", { class: "code-inner" }, gutter, src, hl))
    viewer.append(body, el("div", { class: "muted", style: "padding:4px 8px;font-size:12px" }, `${lines.length} lines`))
    let last = -1
    const find = () => {
      const q = findIn.value.toLowerCase()
      if (!q) return
      let i = lines.findIndex((l, n) => n > last && l.toLowerCase().includes(q))
      if (i < 0) i = lines.findIndex(l => l.toLowerCase().includes(q))
      if (i < 0) { toast("Not found", "info"); return }
      last = i
      hl.style.display = ""; hl.style.top = `${i * LH + 8}px`
      body.scrollTop = Math.max(0, i * LH - 80)
    }
    findIn.addEventListener("keydown", (e) => { if (e.key === "Enter") find() })
  }
  // Keyboard: Up/Down move, Right opens a folder, Left closes it, Enter/Space activates.
  const rows = () => $$(".trow", tree).filter(r => r.offsetParent !== null)
  const focusRow = (r) => { if (!r) return; $$(".trow", tree).forEach(x => x.tabIndex = -1); r.tabIndex = 0; r.focus() }
  tree.addEventListener("keydown", (ev) => {
    const row = ev.target.closest(".trow")
    if (!row) return
    const all = rows()
    const i = all.indexOf(row)
    const isDir = row.hasAttribute("aria-expanded")
    const open = row.getAttribute("aria-expanded") === "true"
    if (ev.key === "ArrowDown") focusRow(all[i + 1])
    else if (ev.key === "ArrowUp") focusRow(all[i - 1])
    else if (ev.key === "Home") focusRow(all[0])
    else if (ev.key === "End") focusRow(all[all.length - 1])
    else if (ev.key === "ArrowRight" && isDir && !open) row.click()
    else if (ev.key === "ArrowLeft" && isDir && open) row.click()
    else if (ev.key === "Enter" || ev.key === " ") row.click()
    else return
    ev.preventDefault()
  })
  const loadDir = async (path, container, depth) => {
    let res
    try { res = await api(apiUrl(`/api/repo/${repoId}/tree?path=${encodeURIComponent(path)}`)) }
    catch (e) { container.replaceChildren(el("div", { class: "muted", style: "padding:6px 8px" }, e.message)); return }
    container.innerHTML = ""
    for (const e of res.entries || []) {
      const row = el("div", { class: "trow", role: "treeitem", tabindex: "-1", "aria-level": String(depth + 1), style: `padding-left:${8 + depth * 14}px` }, e.type === "dir" ? "▸ " : "· ", e.name)
      container.append(row)
      if (e.type === "dir") {
        const kids = el("div", { role: "group", hidden: "" })
        row.setAttribute("aria-expanded", "false")
        container.append(kids)
        row.onclick = async () => {
          const open = row.getAttribute("aria-expanded") !== "true"
          row.setAttribute("aria-expanded", String(open))
          row.firstChild.textContent = open ? "▾ " : "▸ "
          kids.hidden = !open
          if (open && !kids.children.length) { kids.replaceChildren(loading()); await loadDir(e.path, kids, depth + 1) }
        }
      } else {
        row.onclick = () => {
          $$(".trow.sel", tree).forEach(r => { r.classList.remove("sel"); r.removeAttribute("aria-selected") })
          row.classList.add("sel"); row.setAttribute("aria-selected", "true")
          openFile(e.path)
        }
      }
    }
  }
  await loadDir("", tree, 0)
  const first = tree.querySelector(".trow")
  if (first) first.tabIndex = 0
}

// Memory: what the assistant remembers about this repo (mem0). Search can span
// every watched repo to find similar changes elsewhere.
async function renderMemory(wrap, repoId) {
  wrap.innerHTML = ""
  const head = el("div", { style: "display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap" }, el("h3", { style: "margin:0" }, "Memory"))
  const statusEl = el("div", { class: "muted", style: "margin-top:4px" }, "Loading…")
  const qIn = el("input", { placeholder: "Search memories…", "aria-label": "Search memories", style: "flex:1;min-width:0" })
  const scopeSel = el("select", { "aria-label": "Search scope" }, el("option", { value: "repo" }, "This repo"), el("option", { value: "all" }, "All repos"))
  const list = el("div", { class: "mem-list" })
  const load = async () => {
    const q = qIn.value.trim()
    list.replaceChildren(loading(q ? "Searching…" : "Loading…"))
    let res
    try { res = await api(apiUrl(`/api/repo/${repoId}/memory?q=${encodeURIComponent(q)}&scope=${scopeSel.value}`)) }
    catch (e) { res = { ok: false, error: e.message, items: [] } }
    statusEl.textContent = !res.ok
      ? `Memory unavailable: ${res.error || "unknown error"}`
      : `${res.items.length} ${q ? "matches" : "memories"} · embeddings: ${res.embed_provider} ${res.embed_model} · auto-clean ${res.auto_cleanup ? "on" : "off"}`
    list.innerHTML = ""
    for (const m of res.items || []) {
      const del = el("button", { class: "btn ghost mem-del", title: "Forget this", "aria-label": `Forget: ${m.memory.slice(0, 60)}` }, "×")
      del.onclick = () => busy(del, null, async () => { await api(apiUrl(`/api/memory/${m.id}`), { method: "DELETE" }); await load() })
      list.append(el("div", { class: "mem" },
        el("div", {},
          el("span", { class: `badge kind-${m.kind}` }, m.kind),
          m.repo && m.repo !== repoId ? el("span", { class: "badge warn" }, m.repo) : "",
          m.score != null ? el("span", { class: "muted" }, ` ${Number(m.score).toFixed(2)}`) : ""),
        el("div", { style: "flex:1" }, m.memory), del))
    }
    if (!(res.items || []).length && res.ok) list.append(el("p", { class: "muted" }, q ? "No matches." : "Nothing remembered yet. Chat, index commits, or add a note."))
  }
  qIn.addEventListener("keydown", (e) => { if (e.key === "Enter") load() })
  scopeSel.onchange = load
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
  const cleanBtn = el("button", { class: "btn ghost", title: "Remove duplicates, junk and old events. Also runs automatically." }, "Clean up")
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
  head.append(el("div", { style: "display:flex;gap:6px;flex-wrap:wrap" }, cleanBtn, idxBtn))
  wrap.append(head, statusEl,
    el("div", { style: "display:flex;gap:6px;margin-top:8px" }, qIn, scopeSel),
    el("div", { style: "display:flex;gap:6px;margin-top:6px" }, noteIn, noteBtn),
    list)
  await load()
}

async function renderRequests() {
  const content = $("#content")
  content.innerHTML = ""
  content.append(el("h2", {}, "Approval Requests"))
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
        else if (isPush) toast(res.ok ? `Pushed. ${detail}` : `Not pushed. ${res.error || detail}`, res.ok ? "ok" : "error")
        else if (r.type === "components") toast(res.ok ? `Pins bumped. ${detail}` : `Not bumped. ${res.error || detail}`, res.ok ? "ok" : "error")
        else toast(res.ok ? `Merged. ${detail}` : `Not merged. ${res.error || detail}`, res.ok ? "ok" : "error")
        await renderRequests()
      })
      const isComp = r.type === "components"
      const isPush = r.type === "push"
      const approveBtn = el("button", { class: "btn" }, isPush ? (r.open_pr ? "Approve, push and open PR" : "Approve and push") : isComp ? "Approve and bump pins" : "Approve and merge")
      approveBtn.onclick = () => decide("approve", approveBtn)
      const denyBtn = el("button", { class: "btn danger" }, "Deny")
      denyBtn.onclick = () => decide("deny", denyBtn)
      wrap.append(el("div", { class: "repo-card", id: `req-${r.id}` },
        el("h3", {}, r.repo_name || r.repo_id, " ",
          isPush ? badge("", r.open_pr ? `push + PR into ${r.base}` : "push")
          : isComp ? badge(r.safe === false ? "bad" : "", r.safe === false ? "has pinned commits not upstream" : "submodule pins")
            : el("span", { class: `badge ${r.safe === false ? "bad" : ""}` }, r.safe === false ? "conflicts" : "needs a merge commit"),
          el("span", { class: `badge ${pending ? "warn" : r.status === "approved" ? "up" : ""}` }, r.status)
        ),
        el("div", { class: "muted" }, `focus: ${r.focus_branch} · raised ${(r.created_at || "").slice(0, 16).replace("T", " ")}`
          + (r.decided_at ? ` · decided ${r.decided_at.slice(0, 16).replace("T", " ")}` : "")),
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
        isComp || isPush ? null : el("div", { style: "margin:8px 0" }, ...(r.upstreams || []).map(u =>
          el("div", { class: "upstream" },
            el("div", {}, `${u.name} / ${u.branch}: ↓ ${u.behind} behind · ↑ ${u.ahead} ahead`),
            u.reason ? el("div", { class: "muted", style: "color:#e0a060" }, u.reason) : null,
            (u.conflict_files || []).length ? el("div", { class: "muted", style: "font-family:monospace;font-size:12px" }, u.conflict_files.slice(0, 20).join(", ")) : null
          )
        )),
        (r.result || []).length ? el("div", { class: "muted" }, "Result: " + r.result.map(o => `${o.upstream}: ${o.detail}`).join("; ")) : null,
        (r.result || []).filter(o => o.url).map(o => el("div", {}, el("a", { href: o.url, target: "_blank", rel: "noopener", style: "color:#9fb4ff" }, `Open the pull request: ${o.url}`))),
        (r.result || []).filter(o => o.after_update).map(o => el("div", { style: "color:#e0a060;font-size:13px" }, `Now run for ${o.name}: `, el("code", {}, o.after_update))),
        r.comment ? el("div", { class: "muted" }, `Note: ${r.comment}`) : null,
        pending ? el("div", { style: "display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px" }, approveBtn, denyBtn, commentIn,
          el("span", { class: "muted", style: "font-size:12px" }, isPush
            ? "Approve pushes this branch (never forced). A PR is opened if you asked for one."
            : isComp
            ? "Approve moves each pin to its upstream in one commit on your local branch. Nothing is pushed."
            : "Approve merges into your local branch. Conflicts are never auto-resolved; the merge is aborted instead. Nothing is pushed.")) : null
      ))
    }
  } catch (e) { wrap.replaceChildren(el("p", { class: "muted" }, `Could not load requests: ${e.message}`)) }
}

async function renderSettings() {
  const content = $("#content")
  content.innerHTML = ""
  content.append(el("h2", {}, "LLM Settings"))
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
      accRow("github_token", "GitHub token (fine-grained: Contents, Pull requests, Secrets, Workflows read/write)", "password", "github_pat_…"),
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
    const provSel = el("select", {}, el("option", { value: "fastembed" }, "fastembed (in-process, CPU)"), el("option", { value: "openai" }, "OpenAI-compatible server"))
    provSel.value = st.embed_provider
    const embModelIn = el("input", { value: st.embed_model || "", style: "width:320px" })
    const embUrlIn = el("input", { value: st.embed_base_url || "", placeholder: "http://host:8082/v1 (server only)", style: "width:420px" })
    const memSave = el("button", { class: "btn", style: "margin-top:8px" }, "Save & reload memory")
    const memStatus = el("div", { class: "muted" }, st.ok ? "Memory is running." : `Memory not running: ${st.error || "not started"}`)
    memSave.onclick = () => busy(memSave, "Reloading memory…", async () => {
      await api(apiUrl('/api/settings'), { method: "POST", body: JSON.stringify({ memory: { ...mem, embed_provider: provSel.value, embed_model: embModelIn.value, embed_base_url: embUrlIn.value } }) })
      const r = await api(apiUrl("/api/memory/status"))
      memStatus.textContent = r.ok ? "Memory is running." : `Memory not running: ${r.error}`
      toast(r.ok ? "Memory reloaded" : "Memory failed to start", r.ok ? "ok" : "error")
    })
    card.append(el("h4", { style: "margin-top:20px" }, "Memory embeddings (mem0)"),
      el("div", { style: "display:flex;flex-direction:column;gap:12px" },
        el("label", { style: "display:flex;flex-direction:column;gap:4px" }, "Provider", provSel),
        el("label", { style: "display:flex;flex-direction:column;gap:4px" }, "Embedding model", embModelIn),
        el("label", { style: "display:flex;flex-direction:column;gap:4px" }, "Embedding server URL", embUrlIn),
        memStatus, memSave))
  } catch (e) { card.replaceChildren(el("p", { class: "muted" }, `Could not load settings: ${e.message}`)) }
}

function currentView() {
  // location.pathname already carries the BASE when served behind Studio
  // (e.g. /upstream/requests); strip it to get the view-relative path.
  let path = location.pathname
  if (BASE && path.startsWith(BASE)) path = path.slice(BASE.length)
  if (!path) path = '/repos'
  const parts = path.split('/').filter(Boolean)
  if (parts[0] === 'repo' && parts[1]) return ['repo', parts[1]]
  if (parts[0] === 'requests') return ['requests']
  if (parts[0] === 'settings') return ['settings']
  return ['repos']
}

async function router() {
  const [view, id] = currentView()
  // Static heading belongs to the repo list; the other views render their own.
  $("#page-title").hidden = view !== "repos"
  if (view === "repos") await renderRepos()
  else if (view === "repo") await renderRepoPage(id)
  else if (view === "requests") await renderRequests()
  else if (view === "settings") await renderSettings()
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
