"""Model worker: loads chatterbox-turbo once, then pulls tasks from the API until idle.

    python -m tts.worker --device cuda   # gpusched-gated Job (one lease per worker run)
    python -m tts.worker --device cpu    # CPU fallback, a subprocess of the API pod

Exit codes: 0 idle/max-run/SIGTERM (the API respawns if work remains), 3 model load failed
(a CUDA OOM at load has happened here once, n=1, 2026-10-12: the API backs off and the CPU serves).
"""
from __future__ import annotations

import argparse
import base64
import io
import os
import re
import signal
import socket
import sys
import tempfile
import threading
import time

import httpx

from tts import MODEL_REPO, MODEL_REVISION

MAX_CHUNK_CHARS = 300          # one T3 generation stays well under its speech-token cap
GAP_SEC = 0.12                 # silence between chunks


class Stop(Exception):
    """SIGTERM: gpusched preemption, lease timeout or pod deletion."""


def chunks(text: str) -> list[str]:
    """Sentences grouped up to MAX_CHUNK_CHARS; an over-long sentence is split on commas, then words."""
    out, cur = [], ""
    for sent in re.split(r"(?<=[.!?…])\s+", text.strip()):
        pieces = [sent]
        if len(sent) > MAX_CHUNK_CHARS:
            pieces, buf = [], ""
            for w in re.split(r"(?<=[,;:])\s+|\s+", sent):
                if buf and len(buf) + 1 + len(w) > MAX_CHUNK_CHARS:
                    pieces.append(buf)
                    buf = w
                else:
                    buf = f"{buf} {w}".strip()
            pieces.append(buf)
        for p in pieces:
            if cur and len(cur) + 1 + len(p) > MAX_CHUNK_CHARS:
                out.append(cur)
                cur = p
            else:
                cur = f"{cur} {p}".strip()
    if cur:
        out.append(cur)
    return [c for c in out if c]


class Engine:
    def __init__(self, device: str):
        import torch
        from huggingface_hub import snapshot_download
        from chatterbox.tts_turbo import ChatterboxTurboTTS
        self.torch, self.device = torch, device
        path = snapshot_download(MODEL_REPO, revision=MODEL_REVISION, local_files_only=True)
        self.model = ChatterboxTurboTTS.from_local(path, device)
        self.default_conds = self.model.conds
        # numpy 2 (NEP 50): float32 wav * float64 gain is float64, which then meets float32 mel
        # filters in the S3 tokenizer ("float != double"). chatterbox pins numpy<2; we keep NGC's.
        norm = self.model.norm_loudness
        self.model.norm_loudness = lambda wav, sr, *a, **k: norm(wav, sr, *a, **k).astype("float32")

    def synth(self, task: dict) -> bytes:
        import numpy as np
        from scipy.io import wavfile
        m, p = self.model, task.get("params") or {}
        kw = {k: p[k] for k in ("temperature", "top_p", "top_k", "repetition_penalty") if k in p}
        ref = task.get("reference_b64")
        try:
            if ref:
                with tempfile.NamedTemporaryFile(suffix=".wav") as f:     # never kept
                    f.write(base64.b64decode(ref))
                    f.flush()
                    m.prepare_conditionals(f.name)
            parts, gap = [], np.zeros(int(GAP_SEC * m.sr), dtype=np.float32)
            for c in chunks(task["text"]):
                parts += [m.generate(c, **kw).squeeze(0).numpy().astype(np.float32), gap]
        finally:
            m.conds = self.default_conds
            if self.device == "cuda":
                self.torch.cuda.empty_cache()
        wav = np.concatenate(parts[:-1]) if parts else np.zeros(0, dtype=np.float32)
        buf = io.BytesIO()
        wavfile.write(buf, m.sr, (np.clip(wav, -1, 1) * 32767).astype(np.int16))
        return buf.getvalue()


def mem_available_gib() -> float:
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 2**20
    return float("inf")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", choices=["cuda", "cpu"], required=True)
    ap.add_argument("--api", default=os.environ.get("TTS_WORKER_API", "http://tts-worker.tts.svc:8081"))
    ap.add_argument("--idle-sec", type=float, default=float(os.environ.get("TTS_IDLE_SEC", 300)))
    ap.add_argument("--max-run-sec", type=float, default=float(os.environ.get("TTS_MAX_RUN_SEC", 840)))
    ap.add_argument("--threads", type=int, default=int(os.environ.get("TTS_THREADS", 0)))
    ap.add_argument("--min-avail-gib", type=float, default=float(os.environ.get("TTS_MIN_AVAIL_GIB", 0)),
                    help="exit before the next task when host MemAvailable is below this (CPU fallback)")
    a = ap.parse_args()
    worker = os.environ.get("POD_NAME") or f"{socket.gethostname()}-{os.getpid()}"
    dev = "gpu" if a.device == "cuda" else "cpu"
    token = open(os.environ.get("TTS_WORKER_TOKEN_FILE", "/var/run/tts/worker_token")).read().strip()
    api = httpx.Client(base_url=a.api, headers={"Authorization": f"Bearer {token}"}, timeout=60)

    def post(path: str, retry_sec: float = 90, **kw) -> httpx.Response:
        """POST to the API, riding out a tts-api restart (its queue is lost; the worker stays loaded)."""
        end = time.monotonic() + retry_sec
        while True:
            try:
                return api.post(path, **kw)
            except httpx.TransportError as e:
                if time.monotonic() > end:
                    raise
                print(f"API unreachable ({type(e).__name__}): retrying", flush=True)
                time.sleep(3)

    state = lambda s, **kw: post("/internal/state", json={"worker": worker, "device": dev, "state": s, **kw})

    def heartbeat(stop: threading.Event) -> None:
        """Long syntheses: tell the API this worker is alive and busy (else it requeues after 60 s)."""
        with httpx.Client(base_url=a.api, headers=api.headers, timeout=10) as hb:
            while not stop.wait(15):
                try:
                    hb.post("/internal/state", json={"worker": worker, "device": dev, "state": "busy"})
                except httpx.HTTPError:
                    pass

    def on_term(*_):
        raise Stop()
    signal.signal(signal.SIGTERM, on_term)

    started = time.monotonic()
    try:
        state("loading")
        import torch
        if a.threads:
            torch.set_num_threads(a.threads)
        t = time.monotonic()
        try:
            eng = Engine(a.device)
        except Exception as e:                                   # incl. CUDA OOM at load
            print(f"load failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            state("failed", error=f"{type(e).__name__}: {str(e)[:200]}")
            return 3
        print(f"loaded on {a.device} in {time.monotonic() - t:.1f}s, threads {torch.get_num_threads()}", flush=True)
        state("ready", load_sec=round(time.monotonic() - t, 1))
        last_work = time.monotonic()
        while True:
            now = time.monotonic()
            if now - last_work > a.idle_sec:
                print("idle: exiting", flush=True)
                break
            if now - started > a.max_run_sec:                    # keep gpusched's est_sec honest
                print("max run reached: exiting", flush=True)
                break
            if a.min_avail_gib and mem_available_gib() < a.min_avail_gib:
                print(f"MemAvailable under {a.min_avail_gib} GiB: exiting", flush=True)
                break
            params = {"worker": worker, "device": dev, "wait": 20}
            if a.device == "cuda":                               # skip tasks that would overrun the lease
                params["left"] = round(a.max_run_sec - (now - started), 1)
            r = post("/internal/next", params=params, timeout=40)
            if r.status_code == 204:
                if r.headers.get("X-TTS-Exit") == "budget":
                    print("queued work needs a fresh lease: exiting", flush=True)
                    break
                continue
            r.raise_for_status()
            task = r.json()
            busy = threading.Event()
            threading.Thread(target=heartbeat, args=(busy,), daemon=True).start()
            try:
                t = time.monotonic()
                audio = eng.synth(task)
                post(f"/internal/result/{task['id']}", content=audio,
                         params={"worker": worker, "synth_sec": round(time.monotonic() - t, 2)},
                         headers={"Content-Type": "audio/wav"})
            except Stop:
                post(f"/internal/requeue/{task['id']}", retry_sec=5, params={"worker": worker})
                raise
            except Exception as e:                           # AssertionError: e.g. reference under 5 s
                post(f"/internal/fail/{task['id']}", params={"worker": worker},
                         json={"error": f"{type(e).__name__}: {str(e)[:300]}",
                               "client_error": isinstance(e, AssertionError)})
            finally:
                busy.set()
            last_work = time.monotonic()
    except Stop:
        print("SIGTERM: exiting", flush=True)
    finally:
        try:
            post("/internal/state", retry_sec=0, json={"worker": worker, "device": dev, "state": "exiting"})
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
