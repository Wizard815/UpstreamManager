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
