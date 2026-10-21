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
const route = (path) => { history.pushState({}, '', BASE + path.replace(/^\/+/, '')); router() }

const $ = (sel, root = document) => root.querySelector(sel)
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)]
const api = (path, opts = {}) => {
  const url = /^https?:\/\//.test(path) ? path : `${BASE}${path}`
  return fetch(url, { headers: { "Content-Type": "application/json", ...opts.headers }, ...opts }).then(r => r.json())
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

function toast(msg, type = "info") {
  const box = el("div", { class: `toast toast-${type}` }, msg)
  box.style.cssText = "position:fixed;bottom:20px;right:20px;padding:12px 18px;border-radius:8px;color:#fff;font-size:14px;z-index:999;box-shadow:0 4px 12px rgba(0,0,0,.4)"
  box.style.background = type === "error" ? "#b33" : type === "ok" ? "#2a7d3b" : "#2f5bff"
  document.body.append(box)
  setTimeout(() => box.remove(), 3000)
}

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
    try {
      const res = await api(apiUrl('/api/add'), { method: "POST", body: JSON.stringify({ url: url || null, path: dir || null, name: nameIn.value.trim() }) })
      if (res.ok) { toast("Repo added", "ok"); await loadRepos(); }
      else toast(res.error || "Failed to add", "error")
    } catch (e) { toast(String(e), "error"); }
  }
  content.append(el("div", { class: "repo-card", style: "margin-bottom:24px" },
    el("h3", {}, "Add or watch a repo"),
    el("div", { style: "display:flex;gap:8px;flex-wrap:wrap;margin-top:8px" }, urlIn, dirIn, nameIn, addBtn),
    el("p", { class: "muted" }, "Drop a repo into the watch folder, or paste a git URL like https://github.com/Wizard815/DNDWORLDAPP.git. Track multiple upstreams per repo (e.g. origin + a llamacpp mirror).")
  ))

  const list = el("div")
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
    runBtn.onclick = async () => {
      runBtn.disabled = true; runBtn.textContent = "Running…"
      try {
        const res = await api(apiUrl('/api/run-daily'), { method: "POST" })
        toast(`Done: ${res.results.filter(r => r.status === "merged").length} merged, ${res.results.filter(r => r.status === "request_raised").length} requests raised`, "ok")
        await loadRepos(list)
      } catch (e) { toast(String(e), "error"); }
      finally { runBtn.disabled = false; runBtn.textContent = "▶ Run Daily Check"; }
    }
    list.append(runBtn)
  } catch (e) { toast(String(e), "error") }
}

async function repoCard(repo) {
  const badge = (cls, txt) => el("span", { class: `badge ${cls}` }, txt)
  return el("div", { class: "repo-card", id: `repo-${repo.id}` },
    el("div", { style: "display:flex;justify-content:space-between;align-items:center;gap:12px" },
      el("div", {},
        el("h3", {}, repo.name || repo.id,
          repo.conflicts?.length ? badge("warn", `${repo.conflicts.length} conflict`) :
          repo.behind > 0 ? badge("up", `${repo.behind} behind`) : badge("up", "up to date")
        )
      ),
      el("div", { style: "display:flex;gap:6px" },
        el("button", { class: "btn ghost", onclick: () => route(`/repo/${repo.id}`) }, "Open")
      )
    ),
    el("div", { class: "muted" }, `${repo.kind} · ${repo.path}`),
    el("div", { style: "margin-top:6px" }, ...(repo.upstreams || []).map(u => badge("", `↑ ${u}`)))
  )
}

async function renderRepoPage(repoId) {
  const content = $("#content")
  content.innerHTML = ""
  let repo
  try { repo = await api(apiUrl(`/api/repo/${repoId}`)); }
  catch { content.append(el("p", { class: "muted" }, "Repo not found")); return; }

  content.append(el("a", { href: `${BASE}/repos`, style: "color:#9fb4ff", onclick: (e) => { e.preventDefault(); route('/repos') } }, "← All repos"))

  const focusSel = el("select")
  const branches = await repoBranches(repo.path)
  for (const b of branches) focusSel.append(el("option", { value: b }, b))
  focusSel.value = repo.focus_branch
  focusSel.onchange = async () => {
    await api(apiUrl(`/api/repo/${repoId}/focus`), { method: "POST", body: JSON.stringify({ branch: focusSel.value }) })
    toast("Focus branch updated", "ok")
    await renderRepoPage(repoId)
  }

  content.append(el("div", { class: "grid", style: "margin-top:16px" },
    el("div", {}, el("h2", {}, repo.name || repoId), el("div", { class: "muted" }, repo.path)),
    el("div", {},
      el("label", { style: "display:block;margin-bottom:4px" }, "Focus branch"),
      focusSel
    )
  ))

  const upWrap = el("div", { style: "margin-top:20px" })
  upWrap.append(el("h3", {}, "Upstreams"))
  await refreshUpstreams(upWrap, repoId)

  const chatWrap = el("div", { style: "margin-top:20px" })
  chatWrap.append(el("h3", {}, "Chat"))
  await renderChat(chatWrap, repoId, repo.focus_branch)

  content.append(upWrap, chatWrap)
}

async function repoBranches(path) {
  try {
    const res = await api(apiUrl(`/api/branches?repo=${encodeURIComponent(path)}`))
    return res.branches || []
  } catch { return []; }
}

async function refreshUpstreams(wrap, repoId) {
  const repo = await api(apiUrl(`/api/repo/${repoId}`))
  wrap.innerHTML = ""
  wrap.append(el("h3", {}, "Upstreams"))
  for (const u of repo.upstreams || []) {
    const upEl = el("div", { class: "upstream" },
      el("div", { style: "display:flex;justify-content:space-between;align-items:center;gap:12px" },
        el("div", {},
          el("strong", {}, `${u.name} / ${u.branch}`),
          el("span", { class: "badge" }, u.enabled ? "active" : "disabled")
        ),
        el("div", { style: "display:flex;gap:6px" },
          el("button", { class: "btn ghost", onclick: async () => {
            await api(apiUrl(`/api/repo/${repoId}/upstream/${u.name}/enable`), { method: "POST", body: JSON.stringify({ enabled: !u.enabled }) })
            await refreshUpstreams(wrap, repoId)
          } }, u.enabled ? "Disable" : "Enable"),
          el("button", { class: "btn ghost", onclick: async () => {
            toast("Fetching + recomputing…", "info")
            await api(apiUrl(`/api/repo/${repoId}/upstream/${u.name}/rebase`), { method: "POST" })
            await refreshUpstreams(wrap, repoId)
          } }, "Rebase"),
          el("button", { class: "btn danger", onclick: async () => {
            await api(apiUrl(`/api/repo/${repoId}/upstream/${u.name}`), { method: "DELETE" })
            await refreshUpstreams(wrap, repoId)
          } }, "Remove")
        )
      ),
      el("div", { class: "muted" }, u.url)
    )
    wrap.append(upEl)
  }
  const nameIn = el("input", { placeholder: "remote name (e.g. llamacpp)", style: "width:160px" })
  const urlIn = el("input", { placeholder: "https://github.com/ggerganov/llama.cpp.git", style: "width:320px" })
  const addUpBtn = el("button", { class: "btn", style: "align-self:flex-end" }, "Add upstream")
  addUpBtn.onclick = async () => {
    try {
      await api(apiUrl(`/api/repo/${repoId}/upstream`), { method: "POST", body: JSON.stringify({ name: nameIn.value.trim(), url: urlIn.value.trim() }) })
      toast("Upstream added", "ok")
      await refreshUpstreams(wrap, repoId)
    } catch (e) { toast(String(e), "error") }
  }
  wrap.append(el("div", { style: "display:flex;gap:8px;margin-top:12px" }, nameIn, urlIn, addUpBtn))
}

async function renderChat(wrap, repoId, focus) {
  wrap.append(el("h3", {}, `Chat · ${focus}`))
  const box = el("div", { class: "chat-box" })
  const msgIn = el("textarea", { placeholder: "Ask about this repo…", rows: "2", style: "width:100%;resize:vertical;margin-top:8px" })
  const sendBtn = el("button", { class: "btn", style: "margin-top:8px" }, "Send")
  sendBtn.onclick = async () => {
    const text = msgIn.value.trim()
    if (!text) return
    msgIn.value = ""
    box.append(el("div", { class: "msg user" }, text))
    const replyBox = el("div", { class: "msg bot" }, "Thinking…")
    box.append(replyBox)
    try {
      const res = await api(apiUrl(`/api/repo/${repoId}/chat`), { method: "POST", body: JSON.stringify({ message: text }) })
      replyBox.textContent = res.ok ? res.reply : `Error: ${res.error}`
    } catch (e) { replyBox.textContent = `Error: ${e}` }
    box.scrollTop = box.scrollHeight
  }
  const hist = await api(apiUrl(`/api/chat/${repoId}`))
  for (const m of hist.messages || []) {
    box.append(el("div", { class: `msg ${m.role === "user" ? "user" : "bot"}` }, m.content))
  }
  box.scrollTop = box.scrollHeight
  wrap.append(box, msgIn, sendBtn)
}

async function renderRequests() {
  const content = $("#content")
  content.innerHTML = ""
  content.append(el("h2", {}, "Approval Requests"))
  const wrap = el("div")
  content.append(wrap)
  try {
    const reqs = await api(apiUrl('/api/requests'))
    if (!reqs.length) { wrap.append(el("p", { class: "muted" }, "No pending requests.")); return; }
    for (const r of reqs) {
      const commentIn = el("input", { placeholder: "add a note…", style: "width:220px" })
      wrap.append(el("div", { class: "repo-card", id: `req-${r.id}` },
        el("h3", {}, r.repo_name || r.repo_id,
          el("span", { class: `badge ${r.safe === false ? "warn" : ""}` }, r.safe === false ? "CONFLICT" : "merge-ready")
        ),
        el("div", { class: "muted" }, `focus: ${r.focus_branch} · created ${r.created_at || "n/a"}`),
        el("div", { style: "margin:8px 0" }, ...(r.upstreams || []).map(u =>
          el("div", { class: "upstream" },
            el("div", {}, `↑ ${u.name} / ${u.branch}: behind ${u.behind} / ahead ${u.ahead}`),
            u.conflict ? el("div", { class: "muted", style: "color:#e0a060" }, u.conflict_detail || "conflict") : null
          )
        )),
        el("div", { style: "display:flex;gap:8px;align-items:center;flex-wrap:wrap" },
          el("button", { class: "btn", onclick: async () => {
            const res = await api(apiUrl(`/api/request/${r.id}/decide`), { method: "POST", body: JSON.stringify({ decision: "approve", comment: commentIn.value }) })
            if (res.ok) { toast("Approved — executing", "ok"); await renderRequests(); }
            else toast("Failed", "error")
          } }, "Approve"),
          el("button", { class: "btn danger", onclick: async () => {
            await api(apiUrl(`/api/request/${r.id}/decide`), { method: "POST", body: JSON.stringify({ decision: "deny", comment: commentIn.value }) })
            toast("Denied", "info")
            await renderRequests()
          } }, "Deny"),
          commentIn
        )
      ))
    }
  } catch (e) { toast(String(e), "error") }
}

async function renderSettings() {
  const content = $("#content")
  content.innerHTML = ""
  content.append(el("h2", {}, "LLM Settings"))
  const card = el("div", { class: "repo-card", style: "margin-top:16px" })
  content.append(card)
  try {
    const local = await api(apiUrl('/api/settings'))
    const hermes = await fetch(apiUrl('/api/hermes-llm')).then(r => r.json().catch(() => ({})))
    const llm = { ...hermes, ...(local.llm || {}) }
    const modelIn = el("input", { value: llm.model || "", style: "width:320px" })
    const urlIn = el("input", { value: llm.base_url || "", style: "width:420px" })
    const saveBtn = el("button", { class: "btn", style: "margin-top:8px" }, "Save")
    saveBtn.onclick = async () => {
      await api(apiUrl('/api/settings'), { method: "POST", body: JSON.stringify({ llm: { model: modelIn.value, base_url: urlIn.value } }) })
      toast("Settings saved (also written to hermes config.yaml)", "ok")
    }
    card.append(
      el("div", { style: "display:flex;flex-direction:column;gap:12px" },
        el("label", {}, "Model", modelIn),
        el("label", {}, "Base URL", urlIn),
        saveBtn
      )
    )
    try {
      const models = await fetch(apiUrl('/api/models')).then(r => r.json().catch(() => ({ data: [] })))
      card.append(el("h4", { style: "margin-top:16px" }, "Available models (llama-swap)"))
      const grid = el("div", { style: "display:flex;flex-wrap:wrap;gap:6px;margin-top:8px" })
      for (const m of (models.data || []).slice(0, 30)) {
        grid.append(el("button", { class: "btn ghost", onclick: () => { modelIn.value = m.id } }, m.id))
      }
      card.append(grid)
    } catch {}
  } catch (e) { toast(String(e), "error") }
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
document.addEventListener('DOMContentLoaded', router)
router()
