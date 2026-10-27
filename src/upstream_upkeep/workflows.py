"""Generate a GitHub Actions workflow that builds and publishes Docker images.

One workflow, one matrix entry per image. Images go to GHCR (always available:
it uses the run's own GITHUB_TOKEN) and/or Docker Hub (needs the repo secrets
DOCKERHUB_USERNAME and DOCKERHUB_TOKEN; those steps are skipped while the
secrets are not set, so the workflow works before you add them). Pull requests
build the images but never push.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from . import gitops

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__"}
_NAME = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")


def image_name(text: str) -> str:
    """A valid lowercase Docker repository name from any text."""
    s = re.sub(r"[^a-z0-9._-]+", "-", text.lower()).strip("-._")
    return re.sub(r"[-._]{2,}", "-", s) or "image"


def detect(repo: dict) -> list[dict]:
    """Dockerfiles in the repo's own code (submodules and dependency folders skipped)."""
    root = Path(repo["path"]).resolve()
    skip = {(root / c["path"]).resolve() for c in repo.get("components") or []}
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        dirnames[:] = [n for n in dirnames if n not in SKIP_DIRS and (d / n).resolve() not in skip]
        if len(d.relative_to(root).parts) > 4:
            dirnames[:] = []
        for f in filenames:
            if f == "Dockerfile" or f.startswith("Dockerfile.") or f.endswith(".Dockerfile"):
                rel_dir = d.relative_to(root).as_posix()
                context = rel_dir if rel_dir != "." else "."
                base = Path(repo["path"]).name if context == "." else f"{Path(repo['path']).name}-{d.name}"
                if f != "Dockerfile":
                    base += "-" + f.replace("Dockerfile", "").strip(".")
                found.append({"dockerfile": (Path(context) / f).as_posix() if context != "." else f,
                              "context": context, "image": image_name(base)})
    return sorted(found, key=lambda x: x["dockerfile"])


def existing(repo: dict) -> list[dict]:
    """Workflows already in the repo, with their name and triggers."""
    wf = Path(repo["path"]) / ".github" / "workflows"
    out = []
    for p in sorted(wf.glob("*.y*ml")) if wf.is_dir() else []:
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError):
            data = {}
        on = data.get("on", data.get(True, {}))  # YAML 1.1 reads a bare `on:` key as True
        triggers = list(on) if isinstance(on, dict) else ([on] if isinstance(on, str) else list(on or []))
        out.append({"file": f".github/workflows/{p.name}", "name": data.get("name") or p.stem, "triggers": triggers})
    return out


def validate(spec: dict) -> list[str]:
    errors = []
    images = spec.get("images") or []
    if not images:
        errors.append("pick at least one Dockerfile")
    names = [i.get("image", "") for i in images]
    for n in names:
        if not _NAME.match(n):
            errors.append(f"image name '{n}' must be lowercase letters, digits and . _ -")
    if len(set(names)) != len(names):
        errors.append("image names must be different")
    if not (spec.get("ghcr") or spec.get("dockerhub")):
        errors.append("pick GHCR, Docker Hub or both")
    if spec.get("ghcr") and not spec.get("owner"):
        errors.append("GHCR needs the GitHub owner (the repo has no github.com remote)")
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.ya?ml", spec.get("filename") or ""):
        errors.append("file name must look like docker-images.yml")
    for i in images:
        for k in ("context", "dockerfile"):
            v = i.get(k, "")
            if not v or v.startswith(("/", "..")) or ".." in Path(v).parts:
                errors.append(f"{k} '{v}' must be a path inside the repo")
    return errors


def render(spec: dict) -> str:
    """The workflow YAML for a spec:
    {images: [{image, context, dockerfile}], owner, ghcr, dockerhub, branch,
     on_branch, on_tags, on_release, on_pr, arm64, filename}"""
    platforms = "linux/amd64,linux/arm64" if spec.get("arm64") else "linux/amd64"
    branch = spec.get("branch") or "main"
    on = []
    if spec.get("on_branch", True) or spec.get("on_tags", True):
        on.append("  push:")
        if spec.get("on_branch", True):
            on.append(f"    branches: [{branch}]")
        if spec.get("on_tags", True):
            on.append("    tags: ['v*']")
    if spec.get("on_release"):
        on += ["  release:", "    types: [published]"]
    if spec.get("on_pr", True):
        on.append("  pull_request:        # builds the images to check them; never pushes")
    on.append("  workflow_dispatch:")
    matrix = []
    for i in spec["images"]:
        matrix += [f"          - image: {i['image']}", f"            context: {i['context']}",
                   f"            dockerfile: {i['dockerfile']}"]
    targets = []
    if spec.get("ghcr"):
        targets.append(f"            ghcr.io/{spec['owner'].lower()}/${{{{ matrix.image }}}}")
    if spec.get("dockerhub"):
        targets.append("            ${{ env.DOCKERHUB_USERNAME != '' && format('{0}/{1}', env.DOCKERHUB_USERNAME, matrix.image) || '' }}")
    where = []
    if spec.get("ghcr"):
        where.append(f"#   GHCR:       ghcr.io/{spec['owner'].lower()}/<image>  (uses the run's GITHUB_TOKEN; nothing to set up)")
    if spec.get("dockerhub"):
        where += ["#   Docker Hub: <DOCKERHUB_USERNAME>/<image>  (needs repo secrets DOCKERHUB_USERNAME and",
                  "#               DOCKERHUB_TOKEN; skipped until both are set)"]
    steps = [
        "      - name: Checkout",
        "        uses: actions/checkout@v4",
    ]
    if spec.get("arm64"):
        steps += ["", "      - name: Set up QEMU (arm64 builds)", "        uses: docker/setup-qemu-action@v3"]
    steps += ["", "      - name: Set up Docker Buildx", "        uses: docker/setup-buildx-action@v3"]
    if spec.get("ghcr"):
        steps += ["",
                  "      - name: Log in to GHCR",
                  "        if: env.PUSH == 'true'",
                  "        uses: docker/login-action@v3",
                  "        with:",
                  "          registry: ghcr.io",
                  "          username: ${{ github.actor }}",
                  "          password: ${{ secrets.GITHUB_TOKEN }}"]
    if spec.get("dockerhub"):
        steps += ["",
                  "      - name: Log in to Docker Hub",
                  "        if: env.PUSH == 'true' && env.DOCKERHUB_USERNAME != ''",
                  "        uses: docker/login-action@v3",
                  "        with:",
                  "          username: ${{ secrets.DOCKERHUB_USERNAME }}",
                  "          password: ${{ secrets.DOCKERHUB_TOKEN }}"]
    steps += ["",
              "      - name: Image names and tags",
              "        id: meta",
              "        uses: docker/metadata-action@v5",
              "        with:",
              "          images: |",
              *targets,
              "          # latest = the default branch; vX.Y.Z tags also give X.Y.Z and X.Y; sha-<short> always",
              "          tags: |",
              "            type=raw,value=latest,enable={{is_default_branch}}",
              "            type=ref,event=branch",
              "            type=ref,event=pr",
              "            type=semver,pattern={{version}}",
              "            type=semver,pattern={{major}}.{{minor}}",
              "            type=sha",
              "",
              "      - name: Build and push",
              "        uses: docker/build-push-action@v6",
              "        with:",
              "          context: ${{ matrix.context }}",
              "          file: ${{ matrix.dockerfile }}",
              f"          platforms: {platforms}",
              "          push: ${{ env.PUSH == 'true' }}",
              "          tags: ${{ steps.meta.outputs.tags }}",
              "          labels: ${{ steps.meta.outputs.labels }}",
              "          cache-from: type=gha,scope=${{ matrix.image }}",
              "          cache-to: type=gha,mode=max,scope=${{ matrix.image }}"]
    lines = [
        f"name: {spec.get('name') or 'Docker images'}",
        "",
        "# Builds and publishes Docker images (generated by Upstream Manager; edit freely).",
        *where,
        "on:",
        *on,
        "",
        "permissions:",
        "  contents: read",
        "  packages: write   # push to GHCR",
        "",
        "jobs:",
        "  image:",
        "    name: ${{ matrix.image }}",
        "    runs-on: ubuntu-latest",
        "    strategy:",
        "      fail-fast: false",
        "      matrix:",
        "        include:",
        *matrix,
        "    env:",
        "      DOCKERHUB_USERNAME: ${{ secrets.DOCKERHUB_USERNAME }}",
        "      PUSH: ${{ github.event_name != 'pull_request' }}",
        "    steps:",
        *steps,
    ]
    text = "\n".join(lines) + "\n"
    yaml.safe_load(text)  # must at least be valid YAML
    return text


def commit(repo: dict, spec: dict, remove: list[str], focus: str) -> dict:
    """Write the workflow (and delete `remove`) and commit just those files on the focus branch."""
    from . import components  # blocker(): focus branch checked out, nothing else staged
    path = repo["path"]
    target = f".github/workflows/{spec['filename']}"
    remove = [r for r in remove if r.startswith(".github/workflows/") and r != target and ".." not in r]
    reason = components.blocker(path, focus, [target, *remove])
    if reason:
        return {"ok": False, "detail": reason}
    text = render(spec)
    p = Path(path) / target
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    gitops._run(["git", "add", "--", target], cwd=path)
    for r in remove:
        gitops._run(["git", "rm", "-q", "--ignore-unmatch", "--", r], cwd=path)
    names = ", ".join(i["image"] for i in spec["images"])
    msg = f"Add Docker image workflow ({names})"
    body = f"Builds {names} on GitHub Actions and publishes to " + " and ".join(
        x for x, on in (("GHCR", spec.get("ghcr")), ("Docker Hub", spec.get("dockerhub"))) if on) + "."
    if remove:
        body += "\n\nRemoves workflows that don't apply to this repo: " + ", ".join(remove)
    res = gitops._run(["git", "commit", "-q", "-m", msg, "-m", body, "--", target, *remove], cwd=path)
    if res.returncode != 0:  # put everything back as it was
        gitops._run(["git", "reset", "-q", "--", target, *remove], cwd=path)
        if remove:
            gitops._run(["git", "checkout", "-q", "HEAD", "--", *remove], cwd=path)
        return {"ok": False, "detail": (res.stderr or res.stdout).strip()[-400:]}
    return {"ok": True, "detail": msg, "commit": gitops.short_sha(path)[:10], "file": target, "removed": remove}
