#!/usr/bin/env bash
# Create/refresh LIF Kubernetes Secrets. Idempotent; never prints secret values.
#   * Jev key:        TYPE_SAFE_JEV_API_KEY from labzilla/.env.local (git-ignored)
#   * gpusched token (optional): metrics line of $LIF_GPUSCHED_TOKEN_FILE, else
#     ~/.config/gpusched.token, else the legacy ~/.config/bnn/gpusched.token.
#     Missing token skips Secret lif-gpusched; Labzilla still starts.
#   * gateway/admin keys: generated once into labzilla/secrets/lif-*.key (git-ignored, 0600)
#   * console setup code: generated once into labzilla/secrets/lif-console-setup.code; the owner
#     reads it on the host to create the first console admin (docs/CONSOLE.md → Deploy & access)
#   * earn gateway key: labzilla/secrets/earn-gateway.key, written by the arbies earning system's own
#     scripts/create-secrets.sh. Optional: when present it becomes gateway client `earn`
set -euo pipefail
# secrets/ and .env.local live, git-ignored, at the labzilla repo root (two levels up).
cd "${LABZILLA_DIR:-$(dirname "$(readlink -f "$0")")/../..}"
SECRETS=secrets; mkdir -p "$SECRETS"; chmod 700 "$SECRETS"
gen() { local f="$SECRETS/lif-$1.key"; [ -s "$f" ] || { umask 077; python3 -c 'import secrets;print(secrets.token_urlsafe(32))' > "$f"; }; cat "$f"; }
# Typed by a person once, so: 4×4 characters without look-alikes (no 0/O, 1/I/L), ~80 bits.
gen_code() { local f="$SECRETS/lif-$1.code"; [ -s "$f" ] || { umask 077; python3 -c '
import secrets; a = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
print("-".join("".join(secrets.choice(a) for _ in range(4)) for _ in range(4)))' > "$f"; }; cat "$f"; }

# Let the shell parse .env.local (handles quoting) — never grep/cut a dotenv file.
JEV=$(set -a; . ./.env.local >/dev/null 2>&1; printf '%s' "${TYPE_SAFE_JEV_API_KEY:-}")
[ -n "$JEV" ] || { echo "TYPE_SAFE_JEV_API_KEY missing from .env.local" >&2; exit 1; }
TOKEN_FILE="${LIF_GPUSCHED_TOKEN_FILE:-}"
if [ -z "$TOKEN_FILE" ]; then
  if [ -s "$HOME/.config/gpusched.token" ]; then TOKEN_FILE="$HOME/.config/gpusched.token"
  elif [ -s "$HOME/.config/bnn/gpusched.token" ]; then TOKEN_FILE="$HOME/.config/bnn/gpusched.token"
  fi
fi
MTOK=""
if [ -n "$TOKEN_FILE" ]; then
  MTOK=$(awk -F: '$1=="metrics"{print $2}' "$TOKEN_FILE")
  [ -n "$MTOK" ] || { echo "no metrics token in $TOKEN_FILE" >&2; exit 1; }
else
  echo "no gpusched token file: skipping ai-system/lif-gpusched (primary_workload stays off until you add one)"
fi

K_PROBE=$(gen probe); K_BATCH=$(gen batch); K_DEC=$(gen decision); K_WORKLOAD=$(gen workload); K_OPS=$(gen operator)
K_ADMIN=$(gen admin); K_INTERNAL=$(gen internal); K_ENGINE=$(gen engine)   # engine = primary-workload vLLM LIF door
# Labzilla Console: its own controller admin key and gateway client key (audited as `console`).
K_CON_ADMIN=$(gen console-admin); K_CON_GW=$(gen console-gateway); C_SETUP=$(gen_code console-setup)
K_SEARX=$(gen searxng)        # SearXNG's own secret_key (cookie/HMAC signing; the instance has no users)
# Earning system (arbies): read, never generated here, so LIF keeps working without it.
K_EARN=$(cat "$SECRETS/earn-gateway.key" 2>/dev/null || true)

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT; umask 077
printf '%s' "$JEV" > "$tmp/TYPE_SAFE_JEV_API_KEY"
# Optional heavy escalation (Kimi K3). Absent → the provider stays disabled; privacy policy
# (lif.yaml privacy.external_llm_allowed) still decides what may be sent even with a key.
MOONSHOT=$(set -a; . ./.env.local >/dev/null 2>&1; printf '%s' "${MOONSHOT_API_KEY:-}")
EXTRA=()
if [ -n "$MOONSHOT" ]; then printf '%s' "$MOONSHOT" > "$tmp/MOONSHOT_API_KEY"; EXTRA+=(--from-file="$tmp/MOONSHOT_API_KEY"); fi
printf 'controller-probe:%s\nbatch:%s\ndecision-fabric:%s\nworkload:%s\noperator:%s\nconsole:%s\n' \
  "$K_PROBE" "$K_BATCH" "$K_DEC" "$K_WORKLOAD" "$K_OPS" "$K_CON_GW" > "$tmp/LIF_GATEWAY_KEYS"
# Keep an existing external-workload key registered. Do not create lif-bnn.key on a new install.
if [ -s "$SECRETS/lif-bnn.key" ]; then printf 'bnn:%s\n' "$(cat "$SECRETS/lif-bnn.key")" >> "$tmp/LIF_GATEWAY_KEYS"; fi
if [ -n "$K_EARN" ]; then printf 'earn:%s\n' "$K_EARN" >> "$tmp/LIF_GATEWAY_KEYS"; fi
printf 'operator:%s\nconsole:%s\n' "$K_ADMIN" "$K_CON_ADMIN" > "$tmp/LIF_ADMIN_KEYS"
printf '%s' "$K_PROBE" > "$tmp/LIF_PROBE_KEY"
printf '%s' "$K_BATCH" > "$tmp/LIF_BATCH_GATEWAY_KEY"
printf '%s' "$K_DEC" > "$tmp/LIF_DECISION_GATEWAY_KEY"
printf '%s' "$K_INTERNAL" > "$tmp/LIF_INTERNAL_KEY"
printf '%s' "$K_ENGINE" > "$tmp/LIF_ENGINE_KEY"
printf '%s' "$K_CON_ADMIN" > "$tmp/LIF_CONSOLE_ADMIN_KEY"
printf '%s' "$K_CON_GW" > "$tmp/LIF_CONSOLE_GATEWAY_KEY"
printf '%s' "$C_SETUP" > "$tmp/LIF_CONSOLE_SETUP_CODE"
if [ -n "$MTOK" ]; then printf '%s' "$MTOK" > "$tmp/token"; fi
printf '%s' "$K_SEARX" > "$tmp/SEARXNG_SECRET"

kubectl -n ai-system create secret generic lif-secrets --from-file="$tmp/TYPE_SAFE_JEV_API_KEY" \
  --from-file="$tmp/LIF_GATEWAY_KEYS" --from-file="$tmp/LIF_ADMIN_KEYS" --from-file="$tmp/LIF_PROBE_KEY" \
  --from-file="$tmp/LIF_BATCH_GATEWAY_KEY" --from-file="$tmp/LIF_DECISION_GATEWAY_KEY" \
  --from-file="$tmp/LIF_INTERNAL_KEY" --from-file="$tmp/LIF_ENGINE_KEY" \
  --from-file="$tmp/LIF_CONSOLE_ADMIN_KEY" --from-file="$tmp/LIF_CONSOLE_GATEWAY_KEY" \
  --from-file="$tmp/LIF_CONSOLE_SETUP_CODE" "${EXTRA[@]}" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
if [ -n "$MTOK" ]; then
  kubectl -n ai-system create secret generic lif-gpusched --from-file="$tmp/token" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
fi
kubectl -n ai-system create secret generic searxng --from-file="$tmp/SEARXNG_SECRET" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
if [ -n "$MTOK" ]; then
  echo "secrets applied: ai-system/lif-secrets, ai-system/lif-gpusched, ai-system/searxng"
else
  echo "secrets applied: ai-system/lif-secrets, ai-system/searxng (no lif-gpusched)"
fi
echo "keys on disk: $SECRETS/lif-{probe,batch,decision,workload,operator,admin,console-admin,console-gateway}.key (optional external workload: lif-workload.key; the UI/CLI use lif-admin.key)"
echo "console setup code: $SECRETS/lif-console-setup.code (read it on the host when creating the first console admin)"
echo "gateway and controller read their key lists only at startup: roll them out so the console keys work:"
echo "  kubectl -n ai-system rollout restart deploy/gateway deploy/controller"
