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
Pull requests, Secrets and Workflows set to read/write. Docker Hub: a
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
