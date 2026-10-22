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
const api = (path, opts = {}) => {
  // callers usually pass apiUrl(...), which already carries BASE; never add it twice
  const url = /^https?:\/\//.test(path) || (BASE && path.startsWith(`${BASE}/`)) ? path : `${BASE}${path}`
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
    const fetchBtn = el("button", { class: "btn ghost", style: "margin:12px 0 12px 8px" }, "Fetch all upstreams")
    fetchBtn.onclick = async () => {
      fetchBtn.disabled = true; fetchBtn.textContent = "Fetching…"
      for (const r of repos) await api(apiUrl(`/api/repo/${r.id}/fetch`), { method: "POST" }).catch(() => {})
      await loadRepos(list)
    }
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
    repo.dirty ? el("div", { style: "margin-top:6px" }, badge("warn", `${repo.dirty} uncommitted change${repo.dirty > 1 ? "s" : ""}`)) : null,
    repo.error ? el("div", { style: "color:#e08080;margin-top:6px" }, repo.error) : null
  )
}

// ---- tagged items: commits/files the user pins to the chat so the model can read them
const loadTags = (id) => { try { return JSON.parse(localStorage.getItem(`tags:${id}`) || "[]") } catch { return [] } }
const saveTags = (id, t) => { try { localStorage.setItem(`tags:${id}`, JSON.stringify(t)) } catch {} }
let refreshTray = () => {}
let refreshCommits = () => {}
function addTag(repoId, tag) {
  const t = loadTags(repoId)
  if (!t.some(x => x.type === tag.type && x.id === tag.id)) { t.push(tag); saveTags(repoId, t) }
  refreshTray()
  toast(`Tagged ${tag.label} for the chat`, "ok")
}

async function renderRepoPage(repoId) {
  const content = $("#content")
  content.innerHTML = ""
  let repo
  try { repo = await api(apiUrl(`/api/repo/${repoId}`)); }
  catch { content.append(el("p", { class: "muted" }, "Repo not found")); return; }

  content.append(el("a", { href: `${BASE}/repos`, style: "color:#9fb4ff", onclick: (e) => { e.preventDefault(); route('/repos') } }, "← All repos"))

  const focusSel = el("select")
  const branches = await repoBranches(repoId)
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
  await refreshUpstreams(upWrap, repoId)

  const chatWrap = el("div")
  const toolsWrap = el("div")
  content.append(upWrap, el("div", { class: "grid repo-grid", style: "margin-top:20px;align-items:start" }, chatWrap, toolsWrap))
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
  fetchAll.onclick = async () => {
    fetchAll.disabled = true; fetchAll.textContent = "Fetching…"
    await api(apiUrl(`/api/repo/${repoId}/fetch`), { method: "POST" })
    await refreshUpstreams(wrap, repoId); refreshCommits()
  }
  wrap.append(el("div", { style: "display:flex;justify-content:space-between;align-items:center;gap:12px" },
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
        el("div", { style: "display:flex;gap:6px" },
          el("button", { class: "btn ghost", onclick: async (e) => {
            e.target.textContent = "Fetching…"
            const r = await api(apiUrl(`/api/repo/${repoId}/upstream/${u.name}/rebase`), { method: "POST" })
            const err = (r.plan?.upstreams || []).find(x => x.name === u.name)?.conflict_detail
            if (err && /fatal|could not|unable/i.test(err)) toast(err, "error")
            await refreshUpstreams(wrap, repoId); refreshCommits()
          } }, "Fetch"),
          el("button", { class: "btn ghost", onclick: async () => {
            await api(apiUrl(`/api/repo/${repoId}/upstream/${u.name}/enable`), { method: "POST", body: JSON.stringify({ enabled: !u.enabled }) })
            await refreshUpstreams(wrap, repoId)
          } }, u.enabled ? "Disable" : "Enable"),
          el("button", { class: "btn danger", onclick: async () => {
            if (!confirm(`Remove upstream ${u.name}?`)) return
            await api(apiUrl(`/api/repo/${repoId}/upstream/${u.name}`), { method: "DELETE" })
            await refreshUpstreams(wrap, repoId)
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
  const nameIn = el("input", { placeholder: "remote name (e.g. upstream)", style: "width:180px" })
  const urlIn = el("input", { placeholder: "https://github.com/org/repo.git", style: "width:320px" })
  const branchIn = el("input", { placeholder: "its branch (default: focus)", style: "width:170px", title: "The upstream's branch to compare against, e.g. main or master" })
  const addUpBtn = el("button", { class: "btn", style: "align-self:flex-end" }, "Add upstream")
  addUpBtn.onclick = async () => {
    try {
      const res = await api(apiUrl(`/api/repo/${repoId}/upstream`), { method: "POST", body: JSON.stringify({ name: nameIn.value.trim(), url: urlIn.value.trim(), branch: branchIn.value.trim() || null }) })
      if (res.ok === false) return toast(res.error || "Could not add upstream", "error")
      toast("Upstream added. Press Fetch to compare.", "ok")
      await refreshUpstreams(wrap, repoId)
    } catch (e) { toast(String(e), "error") }
  }
  wrap.append(el("div", { style: "display:flex;gap:8px;margin-top:12px;flex-wrap:wrap" }, nameIn, urlIn, branchIn, addUpBtn))
}

// Chat: one conversation per session. "New chat" resets what the model sees of
// the conversation; repo facts, memory and tagged commits/files go with every message.
async function renderChat(wrap, repoId, focus, sessionId) {
  wrap.innerHTML = ""
  const sessions = await api(apiUrl(`/api/repo/${repoId}/sessions`))
  const sessSel = el("select", { style: "max-width:240px" })
  for (const s of sessions) sessSel.append(el("option", { value: s.id }, `${s.title || "New chat"} · ${s.count} msgs`))
  const hist = await api(apiUrl(`/api/chat/${repoId}` + (sessionId ? `?session=${sessionId}` : "")))
  if (![...sessSel.options].some(o => o.value === hist.session)) sessSel.append(el("option", { value: hist.session }, "New chat · 0 msgs"))
  sessSel.value = hist.session
  sessSel.onchange = async () => {
    await api(apiUrl(`/api/repo/${repoId}/sessions`), { method: "POST", body: JSON.stringify({ session: sessSel.value }) })
    await renderChat(wrap, repoId, focus, sessSel.value)
  }
  const newBtn = el("button", { class: "btn ghost", title: "Start a new conversation. The repo memory is kept." }, "New chat")
  newBtn.onclick = async () => {
    const res = await api(apiUrl(`/api/repo/${repoId}/sessions`), { method: "POST", body: "{}" })
    await renderChat(wrap, repoId, focus, res.session)
  }
  const delBtn = el("button", { class: "btn ghost", title: "Delete this conversation (memory is kept)" }, "Delete chat")
  delBtn.onclick = async () => {
    if (!confirm("Delete this chat? The repo memory is kept.")) return
    const res = await api(apiUrl(`/api/repo/${repoId}/sessions/${hist.session}`), { method: "DELETE" })
    await renderChat(wrap, repoId, focus, res.active)
  }
  wrap.append(
    el("div", { style: "display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap" },
      el("h3", { style: "margin:0" }, `Chat · ${focus}`),
      el("div", { style: "display:flex;gap:6px" }, sessSel, newBtn, delBtn)),
  )
  const box = el("div", { class: "chat-box", style: "margin-top:8px;height:420px" })
  const msgIn = el("textarea", { placeholder: "Ask about this repo… (Enter to send, Shift+Enter for a new line)", rows: "2", style: "width:100%;resize:vertical;margin-top:8px" })
  const sendBtn = el("button", { class: "btn", style: "margin-top:8px" }, "Send")
  const tray = el("div", { class: "tray" })
  refreshTray = () => {
    tray.innerHTML = ""
    const tags = loadTags(repoId)
    if (!tags.length) { tray.append(el("span", { class: "muted" }, "Nothing tagged. Use Tag on a commit or file so the model can read it.")); return }
    tray.append(el("span", { class: "muted" }, "Sent with every message:"))
    for (const t of tags) {
      const x = el("button", { class: "chip-x", title: "Remove" }, "×")
      x.onclick = () => { saveTags(repoId, loadTags(repoId).filter(y => !(y.type === t.type && y.id === t.id))); refreshTray() }
      tray.append(el("span", { class: "chip" }, `${t.type === "commit" ? "◆" : "▤"} ${t.label}`, x))
    }
  }
  refreshTray()
  const addMsg = (m) => {
    const d = el("div", { class: `msg ${m.role === "user" ? "user" : "bot"}${m.error ? " err" : ""}` }, m.content)
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
    if (!hist.messages?.length) box.querySelector("p.muted")?.remove()
    addMsg({ role: "user", content: text, attachments })
    const replyBox = el("div", { class: "msg bot" }, "Thinking…")
    box.append(replyBox)
    box.scrollTop = box.scrollHeight
    try {
      const res = await api(apiUrl(`/api/repo/${repoId}/chat`), { method: "POST", body: JSON.stringify({ message: text, session: hist.session, attachments }) })
      replyBox.textContent = res.ok ? res.reply : `Error: ${res.error}`
      if (!res.ok) replyBox.classList.add("err")
    } catch (e) { replyBox.textContent = `Error: ${e}`; replyBox.classList.add("err") }
    sendBtn.disabled = false
    box.scrollTop = box.scrollHeight
  }
  msgIn.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendBtn.click() }
  })
  for (const m of hist.messages || []) addMsg(m)
  if (!(hist.messages || []).length) box.append(el("p", { class: "muted" }, "New conversation. The assistant still knows this repo and what it remembers about it."))
  wrap.append(box, tray, msgIn, sendBtn)
  box.scrollTop = box.scrollHeight
}

// Right-hand tools: commits, files, memory.
function renderTools(wrap, repoId) {
  const names = ["Commits", "Files", "Memory"]
  const bar = el("div", { class: "tabs" })
  const panes = {}
  const built = {}
  const builders = { Commits: renderCommits, Files: renderFiles, Memory: renderMemory }
  const show = async (n) => {
    for (const k of names) { panes[k].style.display = k === n ? "" : "none"; bar.children[names.indexOf(k)].classList.toggle("active", k === n) }
    if (!built[n]) { built[n] = true; await builders[n](panes[n], repoId) }
  }
  for (const n of names) {
    panes[n] = el("div", { style: "display:none" })
    bar.append(el("button", { class: "tab", onclick: () => show(n) }, n))
  }
  wrap.append(bar, ...names.map(n => panes[n]))
  return show("Commits")
}

// Commits: what the upstream has that you lack, what only you have, or everything.
async function renderCommits(wrap, repoId) {
  const repo = await api(apiUrl(`/api/repo/${repoId}`))
  const viewSel = el("select", {},
    el("option", { value: "behind" }, "Missing from upstream (behind)"),
    el("option", { value: "ahead" }, "Only on my branch (ahead)"),
    el("option", { value: "all" }, "All commits"))
  const upSel = el("select", {}, ...(repo.upstreams || []).map(u => el("option", { value: u.name }, u.name)))
  // open on the upstream that has the most commits you are missing
  const cmp = await api(apiUrl(`/api/repo/${repoId}/compare`)).catch(() => ({}))
  const worst = [...(cmp.plan?.upstreams || [])].sort((a, b) => b.behind - a.behind)[0]
  if (worst && worst.behind) upSel.value = worst.name
  const qIn = el("input", { placeholder: "Search commit messages…", style: "flex:1;min-width:0" })
  const list = el("div", { class: "mem-list", style: "max-height:520px" })
  const more = el("button", { class: "btn ghost", style: "margin-top:6px;display:none" }, "Load more")
  const info = el("div", { class: "muted", style: "margin-top:6px" })
  let skip = 0
  const load = async (reset) => {
    if (reset) { skip = 0; list.innerHTML = "" }
    const q = `view=${viewSel.value}&upstream=${encodeURIComponent(upSel.value)}&skip=${skip}&limit=50&q=${encodeURIComponent(qIn.value.trim())}`
    const res = await api(apiUrl(`/api/repo/${repoId}/commits?${q}`))
    if (!res.ok) { info.textContent = res.error; more.style.display = "none"; return }
    info.textContent = ""
    for (const c of res.items) {
      const tag = el("button", { class: "btn ghost", title: "Attach to the chat" }, "Tag")
      tag.onclick = () => addTag(repoId, { type: "commit", id: c.full, label: `${c.sha} ${c.subject.slice(0, 40)}` })
      const view = el("button", { class: "btn ghost" }, "View")
      view.onclick = () => showCommit(repoId, c.full)
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
  more.onclick = () => load(false)
  viewSel.onchange = () => load(true)
  upSel.onchange = () => load(true)
  qIn.addEventListener("keydown", (e) => { if (e.key === "Enter") load(true) })
  wrap.append(el("div", { style: "display:flex;gap:6px;flex-wrap:wrap" }, viewSel, upSel.options.length > 1 ? upSel : "", qIn), info, list, more)
  await load(true)
}

async function showCommit(repoId, sha) {
  const d = await api(apiUrl(`/api/repo/${repoId}/commit/${sha}`))
  const close = () => { ov.remove(); document.removeEventListener("keydown", onKey) }
  const onKey = (e) => { if (e.key === "Escape") close() }
  const patch = el("pre", { class: "code-pre" })
  for (const line of (d.patch || "").split("\n")) {
    const cls = line.startsWith("+") && !line.startsWith("+++") ? "add" : line.startsWith("-") && !line.startsWith("---") ? "del" : line.startsWith("@@") ? "hunk" : line.startsWith("diff ") ? "file" : ""
    patch.append(el("div", { class: `dl ${cls}` }, line || " "))
  }
  const tag = el("button", { class: "btn" }, "Tag for chat")
  tag.onclick = () => { addTag(repoId, { type: "commit", id: d.sha, label: `${d.sha.slice(0, 8)} ${(d.message || "").split("\n")[0].slice(0, 40)}` }) }
  const ov = el("div", { class: "overlay", onclick: (e) => { if (e.target === ov) close() } },
    el("div", { class: "overlay-panel" },
      el("div", { style: "display:flex;justify-content:space-between;gap:8px" },
        el("h3", { style: "margin:0" }, d.ok ? d.sha.slice(0, 10) : "Commit"), el("div", { style: "display:flex;gap:6px" }, tag, el("button", { class: "btn ghost", onclick: close }, "Close"))),
      d.ok ? el("div", {},
        el("div", { class: "muted" }, `${d.author} · ${(d.date || "").slice(0, 10)}`),
        el("pre", { style: "white-space:pre-wrap;margin:8px 0" }, d.message),
        el("pre", { class: "muted", style: "white-space:pre-wrap;font-size:12px" }, d.stat),
        patch, d.truncated ? el("p", { class: "muted" }, "Patch truncated.") : null) : el("p", {}, d.error || "Not found")))
  document.addEventListener("keydown", onKey)
  document.body.append(ov)
}

// Files: a read-only tree plus a line-numbered viewer with find.
async function renderFiles(wrap, repoId) {
  const tree = el("div", { class: "tree" })
  const viewer = el("div", { class: "viewer" }, el("p", { class: "muted", style: "padding:12px" }, "Pick a file."))
  wrap.append(el("div", { class: "files" }, tree, viewer))
  const LH = 18
  const openFile = async (path) => {
    const f = await api(apiUrl(`/api/repo/${repoId}/file?path=${encodeURIComponent(path)}`)).catch(() => ({ ok: false, error: "could not read file" }))
    viewer.innerHTML = ""
    const tag = el("button", { class: "btn ghost" }, "Tag for chat")
    tag.onclick = () => addTag(repoId, { type: "file", id: path, label: path.split("/").slice(-2).join("/") })
    const copy = el("button", { class: "btn ghost" }, "Copy path")
    copy.onclick = () => { navigator.clipboard?.writeText(path); toast("Path copied", "ok") }
    const findIn = el("input", { placeholder: "Find…", style: "width:110px" })
    const head = el("div", { class: "viewer-head" }, el("strong", { style: "flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis" }, path), findIn, tag, copy)
    viewer.append(head)
    if (!f.ok) { viewer.append(el("p", { class: "muted", style: "padding:12px" }, f.error || "cannot show this file")); return }
    const lines = f.content.split("\n")
    const gutter = el("pre", { class: "gutter" }, lines.map((_, i) => i + 1).join("\n"))
    const src = el("pre", { class: "src" }, f.content)
    const hl = el("div", { class: "hl", style: "display:none" })
    const body = el("div", { class: "code-body" }, el("div", { class: "code-inner" }, gutter, src, hl))
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
  const loadDir = async (path, container, depth) => {
    const res = await api(apiUrl(`/api/repo/${repoId}/tree?path=${encodeURIComponent(path)}`))
    container.innerHTML = ""
    for (const e of res.entries || []) {
      const row = el("div", { class: "trow", style: `padding-left:${8 + depth * 14}px` }, e.type === "dir" ? "▸ " : "· ", e.name)
      container.append(row)
      if (e.type === "dir") {
        const kids = el("div", { style: "display:none" })
        container.append(kids)
        let open = false
        row.onclick = async () => {
          open = !open
          row.firstChild.textContent = open ? "▾ " : "▸ "
          kids.style.display = open ? "" : "none"
          if (open && !kids.children.length) await loadDir(e.path, kids, depth + 1)
        }
      } else {
        row.onclick = () => { $$(".trow.sel", tree).forEach(r => r.classList.remove("sel")); row.classList.add("sel"); openFile(e.path) }
      }
    }
  }
  await loadDir("", tree, 0)
}

// Memory: what the assistant remembers about this repo (mem0). Search can span
// every watched repo to find similar changes elsewhere.
async function renderMemory(wrap, repoId) {
  wrap.innerHTML = ""
  const head = el("div", { style: "display:flex;justify-content:space-between;align-items:center;gap:8px" }, el("h3", { style: "margin:0" }, "Memory"))
  const statusEl = el("div", { class: "muted", style: "margin-top:4px" }, "Loading…")
  const qIn = el("input", { placeholder: "Search memories…", style: "flex:1;min-width:0" })
  const scopeSel = el("select", {}, el("option", { value: "repo" }, "This repo"), el("option", { value: "all" }, "All repos"))
  const list = el("div", { class: "mem-list" })
  const load = async () => {
    const q = qIn.value.trim()
    const res = await api(apiUrl(`/api/repo/${repoId}/memory?q=${encodeURIComponent(q)}&scope=${scopeSel.value}`))
    statusEl.textContent = !res.ok
      ? `Memory unavailable: ${res.error || "unknown error"}`
      : `${res.items.length} ${q ? "matches" : "memories"} · embeddings: ${res.embed_provider} ${res.embed_model} · auto-clean ${res.auto_cleanup ? "on" : "off"}`
    list.innerHTML = ""
    for (const m of res.items || []) {
      const del = el("button", { class: "btn ghost mem-del", title: "Forget this" }, "×")
      del.onclick = async () => { await api(apiUrl(`/api/memory/${m.id}`), { method: "DELETE" }); await load() }
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
  const noteIn = el("input", { placeholder: "Add a note to remember…", style: "flex:1;min-width:0" })
  const noteBtn = el("button", { class: "btn ghost" }, "Remember")
  noteBtn.onclick = async () => {
    if (!noteIn.value.trim()) return
    await api(apiUrl(`/api/repo/${repoId}/memory`), { method: "POST", body: JSON.stringify({ text: noteIn.value.trim() }) })
    noteIn.value = ""
    await load()
  }
  const idxBtn = el("button", { class: "btn ghost", title: "Store the last 50 commits (subject + files) so related changes can be found by meaning" }, "Index commits")
  idxBtn.onclick = async () => {
    idxBtn.disabled = true
    idxBtn.textContent = "Indexing…"
    try {
      const res = await api(apiUrl(`/api/repo/${repoId}/memory/index-commits`), { method: "POST", body: "{}" })
      toast(res.ok ? `Indexed ${res.added} new commits (${res.total} total)` : "Indexing failed", res.ok ? "ok" : "error")
    } catch (e) { toast(String(e), "error") }
    idxBtn.disabled = false
    idxBtn.textContent = "Index commits"
    await load()
  }
  const cleanBtn = el("button", { class: "btn ghost", title: "Remove duplicates, junk and old events. Also runs automatically." }, "Clean up")
  cleanBtn.onclick = async () => {
    cleanBtn.disabled = true
    try {
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
    } catch (e) { toast(String(e), "error") }
    cleanBtn.disabled = false
    await load()
  }
  head.append(el("div", { style: "display:flex;gap:6px" }, cleanBtn, idxBtn))
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
  const wrap = el("div")
  content.append(wrap)
  try {
    const reqs = (await api(apiUrl('/api/requests'))).slice().reverse()  // newest first
    if (!reqs.length) { wrap.append(el("p", { class: "muted" }, "No requests. The daily check raises one when a merge needs your approval.")); return; }
    for (const r of reqs) {
      const pending = r.status === "pending"
      const commentIn = el("input", { placeholder: "add a note…", style: "width:220px" })
      const decide = async (decision, btn) => {
        btn.disabled = true
        if (decision === "approve") btn.textContent = "Merging…"
        const res = await api(apiUrl(`/api/request/${encodeURIComponent(r.id)}/decide`), { method: "POST", body: JSON.stringify({ decision, comment: commentIn.value }) })
        const detail = (res.request?.result || []).map(o => `${o.upstream}: ${o.detail}`).join("; ")
        if (decision === "deny") toast("Denied", "info")
        else toast(res.ok ? `Merged. ${detail}` : `Not merged. ${res.error || detail}`, res.ok ? "ok" : "error")
        await renderRequests()
      }
      const approveBtn = el("button", { class: "btn" }, "Approve and merge")
      approveBtn.onclick = () => decide("approve", approveBtn)
      const denyBtn = el("button", { class: "btn danger" }, "Deny")
      denyBtn.onclick = () => decide("deny", denyBtn)
      wrap.append(el("div", { class: "repo-card", id: `req-${r.id}` },
        el("h3", {}, r.repo_name || r.repo_id, " ",
          el("span", { class: `badge ${r.safe === false ? "bad" : ""}` }, r.safe === false ? "conflicts" : "needs a merge commit"),
          el("span", { class: `badge ${pending ? "warn" : r.status === "approved" ? "up" : ""}` }, r.status)
        ),
        el("div", { class: "muted" }, `focus: ${r.focus_branch} · raised ${(r.created_at || "").slice(0, 16).replace("T", " ")}`
          + (r.decided_at ? ` · decided ${r.decided_at.slice(0, 16).replace("T", " ")}` : "")),
        el("div", { style: "margin:8px 0" }, ...(r.upstreams || []).map(u =>
          el("div", { class: "upstream" },
            el("div", {}, `${u.name} / ${u.branch}: ↓ ${u.behind} behind · ↑ ${u.ahead} ahead`),
            u.reason ? el("div", { class: "muted", style: "color:#e0a060" }, u.reason) : null,
            (u.conflict_files || []).length ? el("div", { class: "muted", style: "font-family:monospace;font-size:12px" }, u.conflict_files.slice(0, 20).join(", ")) : null
          )
        )),
        (r.result || []).length ? el("div", { class: "muted" }, "Result: " + r.result.map(o => `${o.upstream}: ${o.detail}`).join("; ")) : null,
        r.comment ? el("div", { class: "muted" }, `Note: ${r.comment}`) : null,
        pending ? el("div", { style: "display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px" }, approveBtn, denyBtn, commentIn,
          el("span", { class: "muted", style: "font-size:12px" }, "Approve merges into your local branch. Conflicts are never auto-resolved; the merge is aborted instead. Nothing is pushed.")) : null
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
    const llm = local.llm || {}
    const urlIn = el("input", { value: llm.base_url || "http://127.0.0.1:8080/v1", placeholder: "http://host:8080/v1", style: "width:420px" })
    const keyIn = el("input", { value: llm.api_key || "", type: "password", placeholder: "optional", style: "width:320px" })
    const modelSel = el("select", { style: "width:420px" })
    const modelStatus = el("div", { class: "muted" }, "")
    const loadModels = async () => {
      const q = `?base_url=${encodeURIComponent(urlIn.value)}&api_key=${encodeURIComponent(keyIn.value)}`
      const r = await fetch(apiUrl('/api/models') + q).then(r => r.json().catch(() => ({ data: [], error: "bad response" })))
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
    refreshBtn.onclick = loadModels
    const saveBtn = el("button", { class: "btn", style: "margin-top:8px" }, "Save")
    saveBtn.onclick = async () => {
      await api(apiUrl('/api/settings'), { method: "POST", body: JSON.stringify({ llm: { model: modelSel.value, base_url: urlIn.value.trim(), api_key: keyIn.value } }) })
      toast("Model connection saved", "ok")
    }
    card.append(
      el("div", { style: "display:flex;flex-direction:column;gap:12px" },
        el("div", { class: "muted" }, "Any OpenAI-compatible server (llama-swap, llama-server, Argos). Independent of hermes-agent."),
        el("label", {}, "Server URL", urlIn),
        el("label", {}, "API key", keyIn),
        el("label", {}, "Model", modelSel),
        modelStatus,
        el("div", { style: "display:flex;gap:8px" }, refreshBtn, saveBtn)
      )
    )
    loadModels()
    const mem = local.memory || {}
    const st = await api(apiUrl('/api/memory/status'))
    const provSel = el("select", {}, el("option", { value: "fastembed" }, "fastembed (in-process, CPU)"), el("option", { value: "openai" }, "OpenAI-compatible server"))
    provSel.value = st.embed_provider
    const embModelIn = el("input", { value: st.embed_model || "", style: "width:320px" })
    const embUrlIn = el("input", { value: st.embed_base_url || "", placeholder: "http://host:8082/v1 (server only)", style: "width:420px" })
    const memSave = el("button", { class: "btn", style: "margin-top:8px" }, "Save & reload memory")
    const memStatus = el("div", { class: "muted" }, st.ok ? "Memory is running." : `Memory not running: ${st.error || "not started"}`)
    memSave.onclick = async () => {
      await api(apiUrl('/api/settings'), { method: "POST", body: JSON.stringify({ memory: { ...mem, embed_provider: provSel.value, embed_model: embModelIn.value, embed_base_url: embUrlIn.value } }) })
      const r = await api(apiUrl("/api/memory/status"))
      memStatus.textContent = r.ok ? "Memory is running." : `Memory not running: ${r.error}`
      toast(r.ok ? "Memory reloaded" : "Memory failed to start", r.ok ? "ok" : "error")
    }
    card.append(el("h4", { style: "margin-top:20px" }, "Memory embeddings (mem0)"),
      el("div", { style: "display:flex;flex-direction:column;gap:12px" },
        el("label", {}, "Provider ", provSel),
        el("label", {}, "Embedding model", embModelIn),
        el("label", {}, "Embedding server URL", embUrlIn),
        memStatus, memSave))
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
