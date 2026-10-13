# CLAUDE.md — rules for AI agents in labzilla

This file is tracked and **public**. Put conventions here, never facts about the owner,
credentials, or private findings. Personal, machine-specific instructions go in
`CLAUDE.local.md`, which is git-ignored.

## The one rule: this repo is public

- **Never commit anything from `secrets/`, `private/` or `personal/`**, or `.env.local`.
  Never use `git add -f` on them, and never commit with `--no-verify`.
- New sensitive material goes in `private/<project>/…`. That covers host, network or
  service inventories; security posture or incidents; primary-workload integration internals;
  and anything measured on production traffic. When unsure, use `private/` and mention it.
- Run `python3 tools/check-private.py --all` before any push or history rewrite.
- Never print secret values. To read `.env.local`, source it in a subshell; don't grep or cut it.
  To check a key exists, test the file (`[ -s secrets/lif-admin.key ]`); don't `cat` it.

## Layout

- `homelab/`: the k3s platform. Scripts `cd` into `homelab/` and use `../secrets/`.
- `lif/`: the Local Intelligence Fabric. Python package at `lif/lif/`, tests at `lif/tests/`,
  venv at `lif/.venv` (built by `tools/setup.sh`).
  - Run tests with `cd lif && .venv/bin/pytest -q`.
  - `lif/knowledge/` holds agent repos and the knowledge package registry (docs: `lif/docs/KNOWLEDGE.md`).
    Keep `knowledge --root lif/knowledge validate` clean. Never edit a published `registry/<pkg>/<version>/`;
    publish a new version. Operational history (events, incidents, production numbers) goes in
    `private/knowledge/`, never in the public repos. The `lif-knowledge` MCP server (`.mcp.json`) has
    resume/context/checkpoint tools.
- `lif/decision-packages/<pkg>/<name>/v<N>.yaml` are versioned Jev questions (docs: `lif/docs/DECISION_ENGINEERING.md`).
  Never edit a version in place; add `v<N+1>`. Keep `local-ai decision lint decision-packages` error-free. Drafts mined
  from real traces, calibration on production traffic and audit reports go in `private/lif/`.
- `lif/lif/understanding/` is the Understanding Compiler (docs: `lif/docs/UNDERSTANDING.md`). Reusable specs go in
  `lif/explanation-packages/`. Keep `local-ai explain --lint-packages` clean. Specs built from live state go in
  `private/lif/understanding/`.
- `tools/`: repo-level tooling. `assets/brand/` holds the logo.

## Production safety

- **A push to `main` is a production deploy.** Argo CD syncs `homelab/argocd/apps/**` and the
  paths those apps reference. Pushing is the owner's call.
- `homelab/argocd/root.yaml` is applied by hand and is not self-managed. If it changes,
  re-apply it with `kubectl apply -f homelab/argocd/root.yaml`.
- An optional primary workload (off unless `primary_workload.enabled`; on this host, Docker under `~/bnn`) has GPU priority when it is on. Don't restart docker, k3s or
  gpusched without the owner's go-ahead. When that integration is on, LIF leases the GPU through gpusched.
- LIF's memory guard owns the replicas of `ai-serving/tier0-small` and `embedding`. Their
  manifests deliberately have no `replicas:` field.
- Single node, 128 GB of unified memory shared with the GPU: every workload needs requests and limits.

## Style

- Conventional Commits with a scope (`feat(lif): …`, `fix(monitoring): …`).
- Pin versions: charts, images, Python deps.
- Docs: tables over prose. Measured numbers carry their date and n. Link private docs as
  *(private, local only)*.
