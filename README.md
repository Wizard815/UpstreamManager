# UpstreamManager
Upstreams and Branches, Manage it all and have a IDE and chat to work along with it

## Run

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
with you on the Requests page.

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
