#!/usr/bin/env bash
# Bring up the Chatterbox TTS service (docs/tts.md), verifying each step.
#   install-tts.sh           hand homelab/tts/k8s to Argo CD (needs the commit on main)
#   install-tts.sh --local   kubectl apply the working tree instead (testing before a push)
# Creates ../secrets/tts.env (git-ignored) with fresh tokens on first run; never prints them.
set -euo pipefail
cd "$(dirname "$0")/.."
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }
mode=${1:-argo}
ENV=../secrets/tts.env
GPUSCHED=$HOME/bnn/deploy/k8s/gpusched

echo "1. Tokens"
if [[ ! -s $ENV ]]; then
  ( umask 077; printf 'TTS_API_TOKEN=%s\nTTS_WORKER_TOKEN=%s\n' "$(openssl rand -hex 32)" "$(openssl rand -hex 32)" > "$ENV" )
  ok "created $ENV (give TTS_API_TOKEN to the client; it is never printed)"
else
  ok "$ENV exists"
fi
source "$ENV"
[[ ${#TTS_API_TOKEN} -ge 32 && ${#TTS_WORKER_TOKEN} -ge 32 ]] || fail "$ENV needs TTS_API_TOKEN and TTS_WORKER_TOKEN"

echo "2. Image"
tag=$(grep -o 'tts/chatterbox:[0-9-]*' tts/k8s/api.yaml | head -1 | cut -d: -f2)
curl -fsS -o /dev/null -H 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
  "http://127.0.0.1:5000/v2/tts/chatterbox/manifests/$tag" \
  || fail "127.0.0.1:5000/tts/chatterbox:$tag not in the local registry (build it: docs/tts.md)"
ok "127.0.0.1:5000/tts/chatterbox:$tag"

echo "3. Namespace, secrets and the gpusched gate"
kubectl create namespace tts --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n tts create secret generic tts-auth \
  --from-literal=api_token="$TTS_API_TOKEN" --from-literal=worker_token="$TTS_WORKER_TOKEN" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
grep -q '^k8s:' "$HOME/.config/bnn/gpusched.token" || fail "no k8s: line in ~/.config/bnn/gpusched.token"
grep '^k8s:' "$HOME/.config/bnn/gpusched.token" | cut -d: -f2- | tr -d '\n' \
  | kubectl -n tts create secret generic gpusched-token --from-file=token=/dev/stdin --dry-run=client -o yaml \
  | kubectl apply -f - >/dev/null
kubectl -n tts create configmap gpusched-gate --from-file="$GPUSCHED/gate.sh" --from-file="$GPUSCHED/hold.sh" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
ok "secrets tts-auth, gpusched-token; configmap gpusched-gate (from $GPUSCHED)"

echo "4. Deploying ($mode)"
if [[ $mode == --local ]]; then
  kubectl apply -k tts/k8s >/dev/null
  ok "applied tts/k8s from the working tree (Argo CD does not manage it yet)"
else
  git -C .. fetch -q origin main
  git -C .. diff --quiet origin/main -- homelab/tts || fail "homelab/tts differs from origin/main: push first, or use --local"
  kubectl apply -f tts/apps/ >/dev/null
  ok "Argo CD app tts"
fi

echo "5. Waiting for the API (first run downloads 3.8 GiB of weights)"
for i in $(seq 1 90); do kubectl -n tts rollout status deploy/tts-api --timeout=5s >/dev/null 2>&1 && break; sleep 5; done
kubectl -n tts rollout status deploy/tts-api --timeout=5s >/dev/null 2>&1 \
  || { kubectl -n tts logs deploy/tts-api --all-containers --tail=15 >&2 || true; fail "tts-api not ready (logs above)"; }
ok "tts-api running"

echo "6. Waiting for the tailnet hostname"
host=""
for i in $(seq 1 60); do
  host=$(kubectl -n tts get ingress tts -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)
  [[ -n $host ]] && break; sleep 5
done
[[ -n $host ]] || fail "ingress tts has no tailnet hostname yet (is the Tailscale operator running?)"
ok "https://$host"
echo
echo "Done. From a device on the tailnet (token in secrets/tts.env):"
echo "  curl -fsS https://$host/v1/audio/speech -H \"Authorization: Bearer \$TTS_API_TOKEN\" \\"
echo "    -H 'Content-Type: application/json' -d '{\"input\": \"Hello from labzilla.\"}' -o hello.wav"
