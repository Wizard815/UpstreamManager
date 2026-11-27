# UpstreamManager
Upstreams and Branches, Manage it all and have a IDE and chat to work along with it

## Install

Two ways; both read their settings from `.env` (see `.env.example`).

### On the machine itself (recommended)

```bash
git clone https://github.com/Wizard815/UpstreamManager && cd UpstreamManager
scripts/install.sh            # add --system for a system-wide service, --port N for another port
```

The script installs the dependencies (and `uv` if it is missing), writes `.env` with what it
finds on the machine (herdr's socket, Hermes Studio, the Hermes gateway and its key), installs a
systemd service on **port 5001**, starts it and shows what it connected to. Run it again any time:
an existing `.env` is never changed. Afterwards:

```bash
systemctl --user status upstream-manager     # or restart / stop; logs: journalctl --user -u upstream-manager -f
```

Your files go in one folder, **`~/ManagerFiles`** unless you pass `--home /other/folder`:

```
ManagerFiles/
  Repos/        repos you clone in the app (and any you move here; they are picked up)
  References/   the reference library
```

`UPSTREAM_HOME` in `.env` names that folder; `UPSTREAM_REPOS_DIR` or `UPSTREAM_REFERENCES_DIR`
move one part on its own, and Settings can still move the library.

This is the better fit when herdr runs on the same machine: Upstream Manager, herdr and your repos
then see the same folders as the same user, with nothing to map.

### With Docker

```bash
cp .env.example .env          # set UPSTREAM_HOME to your ManagerFiles folder
mkdir -p ~/ManagerFiles/Repos ~/ManagerFiles/References   # so the folders are yours, not root's
docker compose up -d --build
```

The container shares the host's network and mounts your ManagerFiles folder, its data folder and herdr's socket
folder **at the same paths they have on the host**, because agents are told where a task's branch
folder is. Set `UPSTREAM_UID`/`UPSTREAM_GID` to your own ids (`id -u`, `id -g`) so files stay
yours. Two things differ from the native install: demo stacks (the Stack tab) need Docker inside
the container and are not available, and other machines through `herdr --machine` need the herdr
command, which the image does not carry. If Docker Hub rate-limits the build, set
`PYTHON_IMAGE=mirror.gcr.io/library/python:3.13-slim-bookworm`.

### Inside Hermes Studio

**On one machine, opening Studio's Upstream tab once connects the two**: Studio hands Upstream its
address and a login (renewed on its own), so there is nothing to put in `.env` for Studio. From then
on, on that machine, a task sent to herdr starts its agent **through Studio's herdr**: the agent is a
Studio chat in its own herdr space (Studio's models and folders), its header links back to the
task, and Studio tells Upstream when the agent works, waits for you or finishes. Agents on other
machines still go through Upstream's own herdr connection, and so does everything if Studio can't
start one (`UPSTREAM_HERDR_VIA_STUDIO=off` makes that the rule). Inside Studio, links to Studio
chats open in Studio, and Studio's address follows the Upstream page you are on.

Studio has an **Upstream** tab that shows this app. It looks for it at `http://127.0.0.1:5001`,
which is where both installs put it, so on one machine there is nothing to set. Otherwise start
Studio with `UPSTREAM_UPKEEP_TARGET=http://host:port`; if Studio runs in a container, also list
the address it calls from in `UPSTREAM_TRUSTED_PROXIES` and let this app listen there (`HOST`).
Studio checks that you are its administrator before it forwards anything.

For agents, set in `.env` (the install script fills in what it finds):

| What | Setting | Then |
|---|---|---|
| herdr on this machine | nothing | tasks start agents in herdr: Hermes by default; pick Claude Code, Codex... per task, or set `UPSTREAM_HERDR_KIND` |
| Hermes Studio | `HERMES_STUDIO_URL`, and a token or `HERMES_STUDIO_USER`/`HERMES_STUDIO_PASSWORD` | "Send to Hermes Studio", and Hermes agents link to their Studio session |
| Hermes gateway | `HERMES_API_URL` (8642), `HERMES_API_KEY` (Hermes' `API_SERVER_KEY`) | "Hand to Hermes"; its sessions show in Studio's history |

Give Hermes this app's tools once, so its agents can read repos and hand changes back for approval:
`hermes mcp add upstream --command /path/to/UpstreamManager/.venv/bin/upstream-mcp --env UPSTREAM_APP_URL=http://127.0.0.1:5001`.

### For development

```bash
uv sync
uv run python -m upstream_upkeep.run      # http://127.0.0.1:5000 (set HOST/PORT to change)
uv run pytest                              # tests (node runs the Markdown renderer cases)
```

The model connection is the app's own setting (LLM Settings page): any
OpenAI-compatible server, such as llama-server, llama-swap or Argos.

## Using it

- **Repos**: what is behind, what waits for your approval, and what needs
  attention (an upstream without the branch, a check that was skipped,
  uncommitted changes blocking a merge), with every repo's status below.
- **A repo's page**: its upstreams (fetch, pull report, resolve in a
  workspace; more under ⋯), then the tools on the left and the chat on the
  right. Drag the divider to give the chat more room, or ⤢ to make it fill
  the page. On a phone the chat is a tab.
- **Files** is a small editor: an explorer that marks changed files
  (right-click for new, rename, delete), tabs, syntax colours, Ctrl+S to save,
  Ctrl+F/Ctrl+H to find and replace, Ctrl+/ to comment, Ctrl+D for the next
  match. It warns before overwriting a file that changed on disk, and can edit
  a merge workspace to resolve conflicts by hand.
- **Merges** (merge workspaces): bring an upstream, or several branches at
  once, into the focus branch. **Merge branches…** takes e.g. three feature
  branches in the order you pick; they merge one after another in a separate
  checkout, each as it was when you started. A clean merge goes straight on;
  a conflict stops there: fix it in Files (or let an agent), then
  **Continue**. **Submit** raises one approval for the whole set. Your own
  working tree is not touched until you approve.
- **Chats**: every conversation, of every repo, plus chats about no repo
  (the assistant gets an overview of all of them). A chat can be about a
  commit, a pull request or a request (“About” above the messages); those
  go to the model with every message.
- **Workflows**: each repo's GitHub Actions: passing, failing, running or
  skipped, the latest run with a link, and the last ten results. Public repos
  need nothing; private ones need the GitHub token in Settings → Accounts
  (no GitHub password or server login is involved).

## Demo stacks (Stack tab)

A repo with a compose file can run as a **demo stack**: a copy that can run
next to the real install and never touches it. No fixed container names; every
port moves to a random port on 127.0.0.1; its own network; restarts off; data
folders outside the project (like `/mnt/user/appdata/...`) are replaced by a
demo folder; images it builds get demo-only names, so they never replace the
images your real stack runs; missing secrets and env files are filled with
generated demo values (kept until you reset). Override files can be layered on
(e.g. `docker/compose.proxy-ca.yml` when building behind a proxy).

You turn it on per repo; then you or a connected agent can start it (for the
repo, or for a merge workspace to test a change), see each service's state and
health, open it, read its logs with an error count, send it requests and run
commands in its containers. It needs Docker on the machine running the app.

## Components that follow releases

A hub that runs release images (one image tag per submodule pin) should
compare its pins with the newest **release**, not the branch head: set the
component to *follow releases* (Settings on its row). It then shows "on the
latest release v2.14.0 · 6 unreleased upstream" instead of "6 behind", and a
bump moves the pin to the next release. A component whose pin sits exactly on a
release tag while following the branch shows a one-click suggestion.

## Coding agents (OpenCode, Codex, Claude Code, Hermes)

Upstream Manager is also an MCP server, so a coding agent can work with what
it knows. **Settings → Coding agents** gives the config to paste for each
agent, filled in with this machine's paths; the server is
`uv run --project <this folder> upstream-mcp` and talks to the running app
(`UPSTREAM_APP_URL`, default `http://127.0.0.1:5000`), so keep the app running.

The agent gets:

- **`repo_briefing`**: branches, upstreams and what they would bring,
  conflicts, your notes and preferences, recent day logs, open requests and
  the rules below. `list_repos`, `list_commits`, `show_commit`, `pull_report`,
  `search_memory`, `day_logs` and `list_requests` read the rest.
- **`conflict_details`**: for each conflicting file, what your side and the
  upstream side each changed since they split, and the commits that did it.
- **Merge workspaces**: `open_merge_workspace` starts the merge in a separate
  checkout on its own branch (`upkeep/merge-…`), conflicts left in the files.
  The agent resolves them there with its own tools, runs the tests and
  commits; `submit_merge_workspace` (with a summary) raises an approval
  request. Approving fast-forwards your focus branch to it and removes the
  workspace. Your own working tree is never touched, and submitting is refused
  while conflicts or conflict markers are left.
- **Branch sets**: `list_branches`, then `open_branch_set` with several
  branches; after resolving a conflict, `continue_merge_workspace` commits it
  and merges the rest. One submit covers the whole set.
- **Components**: `components_status`, `fetch_components`, `set_component`
  (follow releases, watched paths, ...) and `request_component_update`.
- **Testing what it changes**: `open_change_workspace` for a change of its
  own, then `stack_up` (repo or workspace), `stack_status`, `stack_logs`,
  `stack_request` (HTTP to a service: seed demo data, call the API) and
  `stack_exec`; `stack_down` when done.
- **Your chats**: `list_chats`, `read_chat`, and `post_chat_note` to report
  back (the chat model sees those notes too).
- **Settings it may change**: focus branch, upstreams on/off or new ones,
  component settings, demo stack env/services. Approving requests, pushing,
  accounts, app settings and turning demo stacks on are refused for agents.
- `fetch_upstreams`, `add_note` and `request_clean_pull`.

It has no tool that approves, merges into your branch or pushes: those stay
with you on the Requests page (or with the auto-approval rules you set).

Also: `list_groups`, `set_repo_groups` and `fetch_group` (repo groups),
`list_issues` and `import_issues` (GitHub issues as Work tasks).

### Agents on other machines (MCP over HTTP)

The same tools are served over MCP's streamable HTTP when you set, in `.env`:

```sh
UPSTREAM_MCP_PORT=5002
UPSTREAM_MCP_HOST=0.0.0.0          # 127.0.0.1 (default) keeps it on this machine
UPSTREAM_MCP_TOKEN=<a long random string>   # required beyond this machine
```

The app then serves `http://<host>:5002/mcp`; every request must send
`Authorization: Bearer <token>`. For example, from Claude Code on another machine:
`claude mcp add --transport http upstream http://<host>:5002/mcp --header "Authorization: Bearer <token>"`.
`upstream-mcp --http 0.0.0.0:5002` runs it on its own. The Integrations page shows the address.

## Fewer conflicts, and requests that approve themselves

**Merge workspaces resolve less by hand.** Git's *rerere* is on in every
workspace (only there; your repo's own config is not changed): a conflict
resolved once is resolved the same way the next time it comes up, which it
does again and again when you keep a fork in sync. The workspace says which
files were "resolved as before". If [Mergiraf](https://mergiraf.org) is
installed, workspaces also merge with it: it merges by syntax tree, so two
sides adding separate functions, imports or keys no longer conflict.
`UPSTREAM_MERGIRAF` names the program (or `off`).

**Auto-approval** (repo page → Merges → "Goes in by itself"; yours only, agents
can't change it). Pick which requests may go in without asking: clean upstream
pulls, submodule pin bumps, and workspaces an agent submitted (those need a
check). Give a check command, e.g. `npm ci && npm test` or
`cd ui && npm ci && npm run build && npm test`. When such a request is raised,
its exact result is rebuilt in a throwaway checkout (submodules included), the
command runs there, and the request is approved only if it passes; otherwise it
waits for you, with the output on it. Pushes never approve themselves.

## The Overseer, the board and agents working together

**Work → Board** shows tasks as a kanban (Open, Working, Parked, Done). Drag a card
to another column, or up and down; a card you place stays there. Each task has a
priority (urgent, high, normal, low), the agent holding it, and a thread.

**The Overseer** (its own page) keeps the work moving with plain rules, no model.
Every few minutes (`UPSTREAM_OVERSEER_MINUTES`, default 10) it:

- scores each repo: the priority you set on the Overseer page (critical, high,
  normal, low) first, then conflicts with its upstreams, requests waiting for you,
  failed auto-approval checks, submodules that moved where you watch, open bugs,
  urgent tasks, errors, how far behind it is;
- turns problems into tasks once each (a conflict to resolve, a failed check to
  fix, a repo error) and closes them when the problem is gone;
- orders the board: urgent first, then priority, kind, age and the repo's need;
- takes back work from an agent that stopped checking in;
- tells you what needs you, once each;
- if you allow it, hands the next tasks to herdr or Studio, a few at a time.

**Agents** (MCP) start with `overseer_plan`, then `next_task` with their name: the
most important open task nobody has, claimed for them. They check in at least
every 30 minutes (`heartbeat_task`, `comment_task` or `update_task`), or the claim
runs out and the task goes back on the board. They talk on a task
(`comment_task`, to an agent or to you) or to each other (`send_message`; `*` is
everyone, `user` is you), read theirs with `read_messages`, see the board with
`board_view` and each other with `list_agents`, and `release_task` when done. An
agent started by cron calls `next_task`, does it, releases it, and repeats until
there's nothing left. Repo priority, auto-approval, the release wait,
notifications and the Overseer's settings stay yours.

**In Hermes Studio**, the Upstream tab shows what's next and what waits for you, opens the board
and the Overseer, and can give Hermes these tools with one click (it adds this app's MCP server to
the Hermes profile). herdr agents started for a task (here or through Studio) show on the board as
holding it while they work; the task prompt tells them their board name.

**Changes inside a submodule.** An agent that fixes code in a hub's submodule commits inside
the submodule in its workspace, then commits the new pin. That submodule commit exists only in the
workspace; Upstream fetches it from there for the auto-approval check, and on approval brings it into
your submodule checkout (kept under `refs/upkeep/local/` until you push it). The request says so.
Agents are told never to touch other repos, upstreams or git config outside their workspace.

**Finished but not handed in.** An agent that links its workspace to its task
(`open_change_workspace(..., task=…)`) and then goes quiet with the work ready but not submitted
gets a message from the Overseer after 15 minutes, and you get a notification.

## Notifications

Everything that needs you shows in the 🔔 at the top: a request waiting, a failed
check, an agent asking something, a message from an agent, stuck work, the
Overseer's alerts. To get them on your phone, set one of these on Settings →
Notifications (or in `.env`): ntfy (`UPSTREAM_NTFY_URL`, e.g.
`https://ntfy.sh/<a secret topic>`; the free ntfy app subscribes to it), Gotify
(`UPSTREAM_GOTIFY_URL` and `UPSTREAM_GOTIFY_TOKEN`) or a webhook
(`UPSTREAM_NOTIFY_WEBHOOK`, a JSON POST). Pick which kinds ring; the same alert
rings once, not every time it's seen again. `UPSTREAM_PUBLIC_URL` makes the
notifications link back to the page.

## Release wait

An upstream sometimes publishes a bad change and fixes it a day later. Repo page →
an upstream's ⋯ → Release wait: only commits at least that many days old are
taken (merged by the daily check, offered in requests); newer ones show as
"⏳ waiting" with the date they become due. Pinned submodules have their own wait
in their settings; when following releases, only releases that old count. Yours
to set, not agents'.

## Groups and GitHub issues

**Groups**: repo page → ⋯ → Groups… (e.g. `glance`, `studio`). The Repos page
then shows a chip per group: pick one and the list, "Fetch" and the daily
check cover only that group.

**GitHub issues as tasks**: Work → Import issues (or repo page → ⋯). Pick the
remote (origin, or an upstream on github.com) and tick issues; each becomes a
task with the issue's text and a link back, its kind taken from labels (bug,
enhancement...). An issue is never imported twice. Public repos work without a
token; nothing is written to GitHub.

## Connections: herdr, Hermes Studio, Hermes, Pondering Orb, the wiki

Tasks (the **Work** page) go to a coding agent or are parked. Each connection is an address and a
key, set in **Settings** or in an env file: copy `.env.example` to `.env` (beside the project, or
in `data/`; or name one with `UPSTREAM_ENV_FILE`) and restart. Real environment variables win over
the file, and what you save in Settings wins over both.

**herdr** runs the agents (Claude Code, Codex, Hermes, ...), and Upstream Manager follows its rules:

- **Same machine**: nothing to set up. As soon as herdr runs as the same user, its socket
  (`~/.config/herdr/herdr.sock`) is found and used. `UPSTREAM_HERDR=off` turns that off.
- **Another machine**: add it to herdr once (`herdr machine add <ssh-host>`). It then shows in
  Settings and in the "herdr agent" choice of every task; calls go through herdr's own connection
  (`herdr --machine <label> ...`). The task's branch folder must exist over there too: the same
  path on a shared mount, or a folder map (`UPSTREAM_HERDR_PATH_MAP=/home/me/repos=/srv/repos`).
- A task goes to a **new agent** of the kind you pick, or to an agent that is **already running**
  on any of those machines. An agent that is asking something (a trust dialog, an approval) keeps
  the task until you answer it on the task's page; only you answer agents.
- A **Hermes** agent started through herdr links to its own session in **Hermes Studio** when
  Studio's address is set (`HERMES_STUDIO_URL`); run `herdr integration install hermes` once so
  herdr knows Hermes' sessions.

**Agents can use all of this themselves.** An agent that has this app's MCP tools (Studio's
Hermes, a herdr agent, Claude Code) can check repos (`list_repos`, `repo_briefing`, `list_commits`,
`pull_report`), see herdr's agents (`herdr_agents`), write a task down (`create_task`), hand it to
herdr, Studio or Hermes or park it (`send_task`), and follow it (`task_progress`). At most five
agent-sent tasks run at once, and Settings → Work switches this off (`UPSTREAM_AGENT_DISPATCH=off`).
What stays with you: answering an agent's questions, committing, approving and deleting.

**Hermes Studio** (`HERMES_STUDIO_URL`, plus a token or `HERMES_STUDIO_USER`/`HERMES_STUDIO_PASSWORD`)
and the **Hermes API server** (`HERMES_API_URL`, `HERMES_API_KEY`) can also take a task directly.
**Pondering Orb** (`ORB_URL`, `UPSTREAM_ORB_TOKEN`) and the **LLM wiki** (`LLMWIKI_MCP_URL`,
`LLMWIKI_API_KEY`) are where a task is parked for later.

## Integrations

The Integrations page is this app's record of what your setup connects to (nothing is written
to the wiki or GitHub):

- **Connections and services.** The built-in ones (herdr, Hermes Studio, the Hermes gateway,
  Pondering Orb, the LLM wiki) and any services you add, usually your other containers: a name,
  an address (`http://127.0.0.1:8080`, or `tcp://127.0.0.1:5432` to only check the port), an
  optional health path and the repo it is built from. Each is checked when you open the page.
  **Find running containers** lists Docker containers with their ports to add in a click (it
  needs the `docker` command where the app runs).
- **Your repos**, each marked *integrated*, *partly integrated*, *to integrate* or *not needed*,
  with a note on what works or is missing. A repo that runs a working connection counts as
  integrated by itself. Marks show on the Repos list too.
- Everything can also come from `.env`: `UPSTREAM_SERVICES=firefly=http://127.0.0.1:8080/health@firefly-iii, db=tcp://127.0.0.1:5432`
  (name=address, a path is the health check, `@repo` the repo it is built from) and
  `UPSTREAM_MARKS=coinpile:planned, firefly-iii:integrated`. Those show as "from .env"; a mark set
  on the page wins.
- Agents read it with `list_integrations` and can record progress with `mark_integration`; chats
  about no particular repo see it as well. Adding or removing services stays with you.

## References

The References page is a library of repos and files to look things up in (PDFs, zips, images,
notes), sorted into folders under `ManagerFiles/References`. Any chat or agent can read them.

- **One copy, many folders.** "Also put it in another folder (link)" shows the same item under
  several projects without copying it. Cloning a repo that is already in the library makes a link
  to that copy. Links follow when the original is moved, and go when it is deleted.
- **Kept up to date.** Reference repos update themselves once a day (`UPSTREAM_REFS_UPDATE_HOURS`,
  0 = never), and **Update all** does every one now (agents: `update_references`).
- **Edited references join your chats.** A reference repo with changed files, or commits that are
  not on its origin, is marked *edited*. It is then only fetched, never changed by an update, and it
  joins every chat's attachments with its changes, as the repos you work on do. Remove it from a
  chat and it stays out of that chat until it changes again.
- **Agents may sort it**: make folders, move, rename, link, add notes and clone
  (`organize_references`, `clone_reference`). Deleting stays with you.

## Memory and day logs

Memory is one local SQLite file (`memory.db`, next to the state file); no
embedding model or vector database is needed.

- It keeps your notes, durable facts from chats (the chat model picks them
  out), daily-check events and indexed commits. Each is linked to the files and
  commit ids it mentions, so asking about `src/app.py` or a commit finds
  related memories exactly, in this repo or any other. Everything else is
  keyword search.
- Every chat message is kept there as well, even after you delete the chat.
- **Day logs**: for each repo and day with chats or events, the chat model
  writes a log of what was worked on, decided, went wrong and is still open.
  Finished days are logged automatically (hourly while the app runs, and after
  the daily check); **Write today's log** on the Memory tab does today now.
  Ask "what did we do yesterday?" or about a date (`2026-11-01`, "3 days ago")
  and that day's log goes to the model.
- Settings → Memory turns memory, day logs and automatic cleanup on or off
  (or set `UPSTREAM_MEMORY`, `UPSTREAM_DAILY_LOG`, `UPSTREAM_MEMORY_AUTOCLEAN`
  to `off`).

### Agents, the Overseer and finished work in the wiki

With `LLMWIKI_MCP_URL` and `LLMWIKI_API_KEY` in `.env`, reading switches on by itself too (over the
vault's Local REST API next to that address). Then:

- **Agents search and read the wiki**: `wiki_search` (a question works; best matches first, with
  the lines that match) and `wiki_read`, wiki/ only, secrets removed.
- **The Overseer follows a priorities page**: write a note `Upstream priorities` in wiki/ (or name
  another in Settings → LLM wiki). Each line that names a repo (by id or name) or a `#group` counts;
  its words say how much: *urgent / critical / now* (+60), *high / focus / this week* (+30), *low /
  later* (−20), *pause / on hold / skip* (paused: the Overseer gives its tasks to nobody unless an
  agent asks for that repo). A heading's word applies to the lines under it. Agents see the page in
  `overseer_plan`.
- **Finished tasks are recorded**: when a task is done, a line goes to today's daily note and a note
  with what was asked, who did it and the task's thread goes to the inbox (short-term tier; the
  vault's Capture and Ingest file it). Never into wiki/. Switch it off in Settings → LLM wiki.
- **Hermes gets the wiki directly**: Studio's Upstream tab has *Give Hermes the wiki tools*, which
  adds the vault's own MCP server to the Hermes profile (the key goes to the profile's `.env`).

`tests/fake_vault.py` is a stand-in vault (MCP memory tools and the REST API on one port) for trying
this without a real one: `python tests/fake_vault.py <folder> <port> <key>`.

### Sending your days to an LLMWIKI vault

If you keep an LLMWIKI vault (an Obsidian vault behind the Local REST API
plugin and its LLMWIKI MCP Bridge), Upstream Manager feeds its short-term
memory through the vault's MCP endpoint. It never writes the vault's long-term
wiki; the vault's own Ingest and Rollover workflows file what it sends.

```bash
export LLMWIKI_MCP_URL=http://192.168.20.5:27123/family-mcp
export LLMWIKI_API_KEY=llmw_...   # read from the environment, never stored
```

On a family vault (one wiki, a private folder per person), give Upstream
Manager its own **service token** and the `/family-mcp` endpoint. A service
writes only to the shared `household/` memory and can't read anyone's private
notes. Make the token in the vault:

```bash
node scripts/family-setup.mjs add-service upstream-manager "Upstream Manager"
node scripts/family-setup.mjs token upstream-manager
```

A single-user vault uses `/mcp` with the Local REST API key instead; the tool
calls are the same.

- After each daily check, one line in today's daily note (`memory_log`), for
  example `Upkeep: omi-local fast-forwarded 12; argos request raised (2 conflicts) (upstream-manager)`.
  Nothing is written on a day when nothing moved.
- When a finished day's repo logs are all written, they go to the vault once
  as `raw/sources/YYYY-MM-DD - Upstream Manager.md` (`memory_add_source`; `household/raw/sources/` on a family vault),
  one section per repo. A source is never rewritten; a day whose logs failed
  (the model was down) waits for the next pass.

Both run in the background; if the vault is down the app carries on and logs
a warning. Leave `LLMWIKI_MCP_URL` unset to turn this off.

## Components: one repo that pins other repos

A hub repo that includes other projects as git submodules (for example
ALLGlance with dayglance, lastglance, lifeglance and vikunja) gets a
**Components** panel:

- Each submodule is fetched and compared on its own, inside its own git
  directory: *behind* is upstream commits newer than the pin, and *pinned
  commits not upstream* are your own changes there.
- **Request update** (for all, or one row) raises one approval request.
  Approving it moves those pins to their upstream in a single local commit.
  Components with your own commits are skipped. Nothing is pushed.
- **Watched paths** (in a component's Settings) are files the hub copies or
  depends on. When upstream changes them, the panel, the request and the chat
  warn you and show the follow-up command you set (it is never run for you).
  Paths go straight to git, so `src/utils/a.js`, globs and
  `:(exclude)*.test.js` all work.
- The daily check fetches components and raises the same request; it never
  moves a pin by itself.

## Accounts and publishing

**Settings → Accounts** holds a GitHub token and a Docker Hub login. They are
kept in the OS keychain (Windows Credential Manager, macOS Keychain), or in a
private `secrets.json` next to the state file when there is no keychain, and
are never sent back to the page in full. On a server, set
`UPSTREAM_GITHUB_TOKEN`, `UPSTREAM_DOCKERHUB_USERNAME` and
`UPSTREAM_DOCKERHUB_TOKEN` instead; they take priority.

GitHub token: a fine-grained token limited to your repos, with Contents,
Pull requests, Secrets, Workflows and Actions set to read/write. Docker Hub: a
personal access token, not your password.

Each repo's **Publish** tab:

- **Push** the current branch, optionally opening a pull request. It goes
  through an approval request like a merge, never force-pushes, and sends the
  token only to github.com (through git's environment, never on disk or on the
  command line). Without a token, git uses the machine's own login.
- **Docker images on GitHub Actions**: pick the Dockerfiles, image names,
  registries (GHCR and/or Docker Hub), platforms and triggers, preview the
  workflow, and commit it (optionally removing workflows that don't belong).
  GHCR uses the run's own `GITHUB_TOKEN`; the Docker Hub steps are skipped
  until the repo has `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` secrets.
- **Copy Docker Hub login to GitHub secrets** sets those two secrets on the
  repo, encrypted with the repo's public key.

## Home forge (Forgejo or Gitea) and mirrors

Repos can live on your own forge instead of, or as well as, GitHub. Set the
forge's address and a token in **Settings → Accounts** (or
`UPSTREAM_FORGE_URL` and `UPSTREAM_FORGE_TOKEN`). If the same forge answers
under other addresses, list them in `UPSTREAM_FORGE_HOSTS`, comma separated
(`forgejo:3000,192.168.20.5:3030`). The forge token is sent only to those
hosts; the GitHub token only to github.com. The Unraid compose for the forge
is in the Overseer repo, `deploy/forgejo/`.

- **Home.** Each repo has one remote it pushes to by default. Pick it in
  Publish with **Make home**; otherwise it is `origin`, or the repo's forge
  remote when **Default home** in Settings → Accounts is `forge`
  (`UPSTREAM_FORGE_DEFAULT_HOME=forge`). Pushes, pull requests and Actions
  secrets go to whichever forge that remote is on.
- **Workflows** shows each repo's home CI: GitHub Actions, or Forgejo Actions
  for a forge home (the forge lists only workflows that have run).
- **Import issues** reads issues from a GitHub or forge remote.
- **Create private copy on the forge** makes an empty private repo there,
  adds it as remote `forge`, and can make it home and mirror it on to the
  repo's GitHub remote. Then push the branch as usual.
- **Mirrors** are the forge's own. A push mirror sends every push on the forge
  on to GitHub. A pull mirror (`POST /api/forge/import`) copies a GitHub repo
  onto the forge and fetches it every 8 hours. **Sync now** runs either one.
- Commit on one side only. A mirror stops at the first conflict, and the forge
  shows the error under the repo's mirror settings.
- The forge keeps the GitHub token for each mirror it runs, so give that token
  write access only to the repos you mirror.
- Pull mirrors need an `https://` source: Forgejo refuses plain http.

## Test somewhere clean (forge runners and Studio sandboxes)

The Publish tab's **Test somewhere clean** card runs a command on what is
pushed (the current branch), away from this machine. Agents get the same as
the MCP tools `forge_job_run`, `forge_job_status`, `forge_runners`,
`try_in_sandbox` and `sandbox`.

- **Run on forge runner** runs it on one of the forge's runners, like CI, with
  the repo's forge copy checked out. Type the runner label: `ubuntu-latest`,
  or your GPU runner's label (`gpu`). An image runs it in that container.
  Press **Set up jobs** once: it makes a private `YOUR-LOGIN/hermes-jobs` repo
  (or the one in Settings → Accounts, **Forge jobs repo**) with the job
  workflow and a `JOBS_TOKEN` secret for checkouts (`UPSTREAM_FORGE_JOBS_TOKEN`,
  a read-only token, else the forge token). Hermes's forgejo skill uses the
  same repo and file. A GitHub-only repo needs a forge copy or pull mirror
  first.
- **Run in Studio sandbox** makes a throwaway container on Hermes Studio's
  machine (ubuntu, debian, fedora, rocky, alpine, arch, opensuse, or node,
  python and go images), copies the repo in, runs the command and removes it.
  **No network** cuts it off from everything; **keep it** leaves it for more
  commands (Studio's Test Lab page lists and removes sandboxes). Needs the
  Hermes Studio connection.

What goes wrong: a job that stays **waiting** means no idle runner has that
label. "no job workflow" means **Set up jobs** wasn't run. "Studio isn't set
up" means the Hermes Studio connection is off (Settings → Hermes Studio).

## Actions (forks)

A fork runs every workflow it inherits, and upstream's release, deploy and
scheduled jobs fail there (they need upstream's secrets and infrastructure),
emailing you on every run. Each repo's **Actions** tab lists its workflows with
their triggers, the secrets they read and their last runs, and in a fork
suggests which to disable, with the reasons. **Request: disable selected**
raises an approval; approving it disables them on GitHub. Disabling is a
GitHub setting, so the files stay identical to upstream and pulls never
conflict; re-enable any time. The tab also flags workflows the next upstream
pull would add (they arrive active) and ones added since you last reviewed.
Reading works without a token for public repos; changing needs the token's
Actions permission.
