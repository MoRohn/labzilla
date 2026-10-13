# Deployment

Everything below runs as user `tiny` without sudo. `kubectl` uses `~/.kube/config`.

## 1. One-time prerequisites (already done 2026-09-30 / 10-01)

| Step | Command / file | Notes |
|---|---|---|
| Namespaces, PriorityClasses, model store | `deploy/k8s/base/00–02` | Part of the kustomization |
| Local registry | `deploy/k8s/base/03-registry.yaml` | hostPort bound to `127.0.0.1:5000`, so it is not reachable from the LAN. K3s pulls `127.0.0.1:5000/...` over HTTP without any registries.yaml change |
| Seed the pinned Tier 0 weights | `kubectl apply -f deploy/k8s/bootstrap/download-tier0.yaml` | Resumable, sha256-verified, atomic rename; a mismatch is quarantined as `*.bad` |
| Secrets | `scripts/create-secrets.sh` | See below |
| gpusched token (optional) | `metrics` line of `~/.config/gpusched.token`, or `LIF_GPUSCHED_TOKEN_FILE` | Only used when `primary_workload.enabled` is true. Absent token: `create-secrets.sh` skips Secret `lif-gpusched` |

### Secrets

`scripts/create-secrets.sh` is idempotent and never prints values.

- It reads `TYPE_SAFE_JEV_API_KEY` from `labzilla/.env.local`. A gpusched metrics token is optional (`LIF_GPUSCHED_TOKEN_FILE`, `~/.config/gpusched.token`, or the legacy `~/.config/bnn/gpusched.token`).
- It generates gateway and admin keys once into `labzilla/secrets/lif-{probe,batch,decision,workload,operator,admin}.key` (mode 0600, git-ignored). An existing `lif-bnn.key` is kept and still registered as client `bnn`; a new install does not create that file.
- For the Labzilla Console it also generates `lif-console-admin.key`, `lif-console-gateway.key` (added as `console:` lines to `LIF_ADMIN_KEYS` / `LIF_GATEWAY_KEYS`) and the first-run `lif-console-setup.code`.
- It applies `ai-system/lif-secrets`:
  - `TYPE_SAFE_JEV_API_KEY`
  - `LIF_GATEWAY_KEYS`
  - `LIF_ADMIN_KEYS`
  - `LIF_PROBE_KEY`
  - `LIF_BATCH_GATEWAY_KEY`
  - `LIF_DECISION_GATEWAY_KEY`
  - `LIF_CONSOLE_ADMIN_KEY`, `LIF_CONSOLE_GATEWAY_KEY`, `LIF_CONSOLE_SETUP_CODE`
- It applies `ai-system/lif-gpusched` (`token`).

Pods mount these at `/var/run/lif/secrets/` and `/var/run/lif/gpusched/token`. The console mounts only its three `LIF_CONSOLE_*` keys.

The gateway and controller read `LIF_GATEWAY_KEYS` / `LIF_ADMIN_KEYS` only at startup. After the script changes them, roll both out (`kubectl -n ai-system rollout restart deploy/gateway deploy/controller`) unless a deploy follows straight away.

## 2. Build and deploy

```bash
cd ~/labzilla/lif
.venv/bin/python -m pytest tests -q                       # 54 tests
TAG=$(date +%Y%m%d-%H%M)
docker build -t 127.0.0.1:5000/lif/fabric:$TAG . && docker push 127.0.0.1:5000/lif/fabric:$TAG
for m in deploy/k8s/serving/{tier0,tier0-small,embedding}.yaml; do   # a rejected flag = no chat (Recreate)
  .venv/bin/python scripts/smoke-server-args.py $m || break
done
kubectl kustomize . | sed "s/IMAGE_TAG/$TAG/g" | kubectl diff -f -   # review first
kubectl kustomize . | sed "s/IMAGE_TAG/$TAG/g" | kubectl apply -f -
for d in controller gateway decision-fabric batch console; do kubectl -n ai-system rollout status deploy/$d; done
```

- The image build has a Node stage that builds the console PWA (`apps/console`, `npm ci` + `npm run build`), so `docker build` needs npm registry access.

- `kustomization.yaml` sits at the LIF root, because kustomize won't read `config/` from below `deploy/`.
- It generates the `lif-config` ConfigMap (hashed name, so pods roll on change) from `config/lif.yaml`, `config/models.yaml`, `config/decisions/core.yaml`, `config/workflows/candidate-model-analysis.yaml` and `evals/core.yaml`.
- The ConfigMap is mounted at `/etc/lif`, which overrides the copies bundled in the image.

### After every apply

1. **Check host headroom.** `tier0-small` and `embedding` have no `replicas:` in their manifests, so an apply leaves the memory guard's count alone. LIF workloads still roll, and `gpu-engine-bridge` restarts, because it uses the same image.
   If `primary_workload.enabled` is true, confirm the external scheduler still reports headroom (`available` at or above ~8 GiB, and no `unmanaged` hold). With the integration off, skip that check.
2. **Smoke test:** `local-ai doctor` (see OPERATIONS.md).

## 3. What needs the owner

| Action | Why |
|---|---|
| Push to GitHub | Argo CD syncs `main` of `MoRohn/labzilla`. A push is a production deploy |
| Argo CD Application for LIF | Not active. Template: `homelab/argocd/templates/local-intelligence-fabric.yaml.example` (path `lif`), plus the image tag substitution (kustomize `images:` / an `IMAGE_TAG` overlay) |
| Restart gpusched | Activates the `lif:` token (only needed for GPU leases) |
| Any sudo step | The agent has no sudo: firewall, containerd config, systemd system units |
| `/etc/hosts` / LAN DNS for `*.tiny-dgx.lan` | For ingress. Phones need LAN DNS (or mDNS) to reach `labzilla.tiny-dgx.lan` |
| Labzilla Console first deploy | Not yet deployed. Run `create-secrets.sh`, roll out gateway + controller, build and apply, then create the admin with the setup code (`cat ~/labzilla/secrets/lif-console-setup.code`, owner only). See [CONSOLE.md → Deploy & access](CONSOLE.md#deploy--access) |
| ~~Publish `labzilla.local` via mDNS~~ | Done 2026-10-01: user unit `homelab/host/systemd/labzilla-mdns.service` (no sudo) |
| Trust the Labzilla Local CA on each device | The CA and cert are in place (2026-10-01, `homelab/networking/local-ca/issue-certs.sh`). Install `secrets/labzilla-ca.crt` on each phone and desktop once (iOS: profile, then *Certificate Trust Settings*; Android: *Install a certificate → CA certificate*). Rerun the script before 2027-10-03 to renew |

### Argo CD note: replicas versus the memory guard

The controller scales `ai-serving/tier0-small` (and, in the danger zone, `embedding`) to 0. A self-healing Argo app would undo that. Add:

```yaml
ignoreDifferences:
  - group: apps
    kind: Deployment
    namespace: ai-serving
    jsonPointers: [/spec/replicas]
```

Model server Deployments the controller creates (`lif-*`, `cand-*`) are not in git by design. Exclude them from pruning or keep them out of the app's resource scope.

## 4. Removing LIF

```bash
kubectl kustomize . | sed "s/IMAGE_TAG/x/g" | kubectl delete -f -
```

This deletes the PVCs (`registry.db`, `batch.db`, `decisions.db`, `console.db`, `model-store`), so back up first. The primary workload and gpusched are unaffected. Remove the `lif:` line from `gpusched.token` by hand.
