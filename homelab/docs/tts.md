# TTS: Chatterbox Turbo over the tailnet

> **Deprecated 2026-10-19.** The service is shut down: namespace `tts`, its tailnet ingress and the
> weights volume are deleted. `scripts/install-tts.sh` refuses to run. The code stays for reference.

`https://tts.<tailnet>.ts.net` serves [ResembleAI/chatterbox-turbo](https://huggingface.co/ResembleAI/chatterbox-turbo)
(MIT, revision `749d1c1a`) to any device on the tailnet that has the API token. The GPU belongs to the
primary workload, so the GPU is leased through gpusched only while there is work, and interactive
requests fall back to the CPU while it can't be had.

## How it runs

| Piece | What it does | Holds the GPU? |
|---|---|---|
| `tts-api` (Deployment) | API, in-memory work queue, starts workers. Never imports torch | no |
| GPU worker (Job, `k8s/worker-job.yaml`) | Created by the API when work is queued. gpusched gate + hold, class `interactive`, preemptible, 4096 MiB declared. Exits after 300 s idle or 840 s, inside its declared 900 s, and never starts a task estimated (0.03 s/char + 10 s) to run past 840 s; that task waits for the next worker | yes, one lease per run |
| CPU fallback (subprocess of `tts-api`) | Started only for interactive requests the GPU isn't serving, only while host `MemAvailable` ≥ 15 GiB. Exits after 120 s idle, or before its next task if `MemAvailable` < 8 GiB | no |
| Tailscale ingress `tts` | Exposes port 8080 only. The worker port (8081) stays in-cluster (NetworkPolicy) | no |

**When the GPU is unavailable.** gpusched admits one non-production job at a time, nothing while
production is live, and nothing whose run would overlap a reserved window. In those times:

| Request | Behaviour |
|---|---|
| `POST /v1/audio/speech` | Served on the CPU (about real time on 8 cores), or 503 with the reason after 180 s if the CPU guard refuses |
| `POST /v1/jobs` | Waits in the queue for the GPU. Never runs on the CPU |

## Measured (2026-10-12)

| Path | n | RTF (synth s / audio s) | Load | Memory |
|---|---|---|---|---|
| GPU, gated Job | 9 | median 0.249, max 0.258 | 8.6 s | 3396 MiB (nvidia-smi), RSS 4.9 GiB |
| CPU, 4 cores (Docker cpuset) | 6 | median 1.51 | 6.4 s | RSS 6.4 GiB |
| CPU, 8 cores (Docker cpuset) | 6 | median 1.04 | 6.2 s | RSS 6.4 GiB |
| GPU, through the API, worker warm | 2 | 0.8–1.1 s end to end for one short sentence | — | — |
| CPU, in `tts-api` (8-CPU quota) | 2 | 7.8 s for one ~5 s sentence | 12.0 s | — |

The first gated GPU run failed with a CUDA OOM at model load while host `MemFree` was ~2 GB
(n=1; identical rerun succeeded). Workers treat a load failure as normal: the API backs off
120 s and the CPU serves meanwhile.

## API

All `/v1` routes need `Authorization: Bearer $TTS_API_TOKEN` (`secrets/tts.env`).

| Route | Body | Returns |
|---|---|---|
| `POST /v1/audio/speech` | JSON `{input, temperature?, top_p?, top_k?, repetition_penalty?}`, ≤ 2000 chars. Or multipart with the same fields plus `reference` (wav, > 5 s, ≤ 5 MiB) to clone a voice | `audio/wav` (24 kHz mono). Headers `X-TTS-Device`, `X-TTS-Synth-Sec` |
| `POST /v1/jobs` | Same, ≤ 20000 chars | 202 `{id, state}` |
| `GET /v1/jobs/{id}` | | `{state, device, synth_sec, error}`. Kept 1 h |
| `GET /v1/jobs/{id}/audio` | | `audio/wav` once `state` is `done` |
| `GET /v1/status` | | Queue, workers, GPU Job, CPU guard |
| `GET /healthz` | no auth | `{status, gpu_ready, queued}` |

```bash
curl -fsS https://tts.<tailnet>.ts.net/v1/audio/speech -H "Authorization: Bearer $TTS_API_TOKEN" \
  -H 'Content-Type: application/json' -d '{"input": "Hello from labzilla."}' -o hello.wav
curl -fsS https://tts.<tailnet>.ts.net/v1/audio/speech -H "Authorization: Bearer $TTS_API_TOKEN" \
  -F input='Say this in my voice.' -F reference=@me-10s.wav -o clone.wav
```

Notes:

- The queue is in memory. A `tts-api` restart drops queued and finished jobs. Running workers
  stay loaded and reconnect (they retry for 90 s).
- Reference audio is never stored. Output carries Chatterbox's Perth watermark.
- Long text is split into sentences of ≤ 300 characters and joined with 120 ms gaps.

## Install and upgrade

```bash
cd ~/labzilla/homelab/tts/image
TAG=$(date +%Y%m%d-%H%M)
docker build -t 127.0.0.1:5000/tts/chatterbox:$TAG . && docker push 127.0.0.1:5000/tts/chatterbox:$TAG
sed -i -E "s/chatterbox:[0-9]{8}-[0-9]{4}/chatterbox:$TAG/" ../k8s/api.yaml ../k8s/worker-job.yaml
~/labzilla/homelab/scripts/install-tts.sh --local   # try the working tree
# commit + push, then hand it to Argo CD:
~/labzilla/homelab/scripts/install-tts.sh
```

The script creates `secrets/tts.env` with fresh tokens on first run (never printed), the
`tts-auth` and `gpusched-token` secrets and the `gpusched-gate` ConfigMap (from
`~/bnn/deploy/k8s/gpusched`), then waits for the API and the tailnet hostname. The first start
downloads 3.8 GiB of weights to the `tts-models` PVC.

**Image.** NGC `pytorch:25.11-py3` (torch 2.10.0a0, sm_121). `chatterbox-tts==0.1.7` pins torch 2.6,
torchaudio 2.6 and numpy < 2, so it is installed with `--no-deps`, torchaudio 2.9.1 is built from
source against NGC's torch (the PyPI wheel fails to load), and the worker casts `norm_loudness`
output back to float32 (numpy 2 promotion breaks voice cloning otherwise).

## Access

Any device signed in to the tailnet can reach the URL; the token is what keeps it to the client
you give it to. If a machine that isn't yours joins the tailnet, it can also reach the other
tailnet services (see [tailscale.md](tailscale.md) on Prometheus). Restrict it with a grant in the
Tailscale policy.
