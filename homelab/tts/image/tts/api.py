"""TTS API and work queue. Never imports torch, so the pod stays small until it needs the CPU fallback.

Public (port 8080, tailnet ingress, bearer TTS_API_TOKEN):
    POST /v1/audio/speech        OpenAI-style, synchronous: JSON {input, ...} or multipart with a
                                 `reference` wav for voice cloning. Returns audio/wav.
    POST /v1/jobs                same body, queued (batch); GET /v1/jobs/{id}, GET /v1/jobs/{id}/audio
    GET  /v1/status              queue, workers, GPU Job, CPU fallback
    GET  /healthz                no auth
Internal (port 8081, cluster only, bearer worker token): workers pull with /internal/next.

GPU: one gpusched-gated Job at a time (template /etc/tts/worker-job.yaml), created when work is
queued and exiting on its own when idle. The API never retries a Job in place (backoffLimit 0):
gpusched deletes preempted pods, and a retry would re-queue at once. CPU: a subprocess started only
for interactive work the GPU isn't serving, and only while MemAvailable leaves the host headroom.
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import logging
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field

import httpx
import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

LOG = logging.getLogger("tts.api")
E = os.environ.get


def _f(name: str, default: float) -> float:
    return float(E(name, default))


INTERACTIVE_TIMEOUT = _f("TTS_INTERACTIVE_TIMEOUT_SEC", 180)
CPU_GRACE = _f("TTS_CPU_GRACE_SEC", 1)          # GPU worker not ready: CPU may take it after this
CPU_BACKUP = _f("TTS_CPU_BACKUP_SEC", 20)       # GPU worker ready but busy: CPU after this
CPU_MIN_AVAIL_GIB = _f("TTS_CPU_MIN_AVAIL_GIB", 15)   # 8 GiB host headroom + ~6.4 GiB engine RSS
CPU_KEEP_AVAIL_GIB = _f("TTS_CPU_KEEP_AVAIL_GIB", 8)  # running CPU worker exits between tasks below this
CPU_THREADS = int(_f("TTS_CPU_THREADS", 8))
CPU_IDLE = _f("TTS_CPU_IDLE_SEC", 120)
GPU_BACKOFF = _f("TTS_GPU_BACKOFF_SEC", 120)
GATE_IDLE_CANCEL = _f("TTS_GATE_IDLE_CANCEL_SEC", 300)   # nothing to do: stop waiting for a lease
RESULT_TTL = _f("TTS_RESULT_TTL_SEC", 3600)
MAX_CHARS_SYNC = int(_f("TTS_MAX_CHARS_SYNC", 2000))
MAX_CHARS_BATCH = int(_f("TTS_MAX_CHARS_BATCH", 20000))
MAX_REF_BYTES = int(_f("TTS_MAX_REF_BYTES", 5 * 2**20))
MAX_QUEUED = int(_f("TTS_MAX_QUEUED", 200))
GPU_SEC_PER_CHAR = _f("TTS_GPU_SEC_PER_CHAR", 0.03)  # measured ~0.017 (RTF 0.25, 2026-10-12, n=9) + margin
GPU_SEC_FIXED = _f("TTS_GPU_SEC_FIXED", 10)
WORKER_STALE = 60.0
NS = E("POD_NAMESPACE", "tts")
JOB_LABEL = "app.kubernetes.io/name=tts-gpu-worker"


def _secret(name: str) -> str:
    return open(f"/var/run/tts/{name}").read().strip()


@dataclass
class Task:
    kind: str                          # interactive | batch
    text: str
    params: dict
    reference_b64: str | None = None
    id: str = field(default_factory=lambda: "t_" + uuid.uuid4().hex[:16])
    created: float = field(default_factory=time.time)
    cpu_ok_after: float = float("inf")  # batch never runs on the CPU
    state: str = "queued"              # queued | running | done | failed | cancelled
    worker: str | None = None
    device: str | None = None
    assigned_at: float = 0.0
    finished: float = 0.0
    attempts: int = 0
    audio: bytes | None = None
    error: str | None = None
    client_error: bool = False         # the request was at fault (e.g. reference audio under 5 s)
    synth_sec: float | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)

    def public(self) -> dict:
        return {"id": self.id, "kind": self.kind, "state": self.state, "device": self.device,
                "created": self.created, "finished": self.finished or None, "chars": len(self.text),
                "synth_sec": self.synth_sec, "error": self.error}


class Service:
    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}
        self.workers: dict[str, dict] = {}
        self.gpu_backoff_until = 0.0
        self.gpu_last_error: str | None = None
        self.last_work = time.time()
        self.seen_failed_jobs: set[str] = set()
        self.jobs: list[dict] = []
        self.cpu_proc: subprocess.Popen | None = None
        self.cpu_refused: str | None = None
        self.lock = asyncio.Lock()

    # ── queue ──────────────────────────────────────────────────────────
    def queued(self) -> list[Task]:
        return sorted((t for t in self.tasks.values() if t.state == "queued"),
                      key=lambda t: (t.kind != "interactive", t.created))

    def gpu_ready(self) -> bool:
        now = time.time()
        return any(w["device"] == "gpu" and w["state"] == "ready" and now - w["last_seen"] < WORKER_STALE
                   for w in self.workers.values())

    def submit(self, t: Task) -> None:
        if len(self.queued()) >= MAX_QUEUED:
            raise HTTPException(429, "queue full")
        if t.kind == "interactive":
            t.cpu_ok_after = t.created + (CPU_BACKUP if self.gpu_ready() else CPU_GRACE)
        self.tasks[t.id] = t
        self.last_work = time.time()

    def pick(self, device: str, left: float | None = None) -> tuple[Task | None, bool]:
        """Next task for this device, and whether a GPU task was skipped for not fitting in `left` s."""
        now, skipped = time.time(), False
        for t in self.queued():
            if device == "gpu":
                if left is not None and GPU_SEC_FIXED + GPU_SEC_PER_CHAR * len(t.text) > left:
                    skipped = True                   # would overrun the lease: leave it for a fresh worker
                    continue
                return t, skipped
            if t.kind == "interactive" and now >= t.cpu_ok_after:
                return t, skipped
        return None, skipped

    def finish(self, t: Task, state: str, *, audio: bytes | None = None, error: str | None = None) -> None:
        t.state, t.audio, t.error, t.finished = state, audio, error, time.time()
        t.done.set()
        self.last_work = time.time()

    def requeue(self, t: Task, why: str) -> None:
        t.attempts += 1
        if t.attempts > 2:
            self.finish(t, "failed", error=f"gave up after {t.attempts} attempts ({why})")
            return
        LOG.info("requeue %s (%s)", t.id, why)
        t.state, t.worker, t.device = "queued", None, None
        if t.kind == "interactive":
            t.cpu_ok_after = time.time() + CPU_GRACE

    def why_waiting(self) -> str:
        parts = []
        if self.gpu_ready():
            parts.append("GPU worker busy")
        elif any(not _finished(j) for j in self.jobs):
            parts.append("GPU worker waiting for a gpusched lease or loading")
        elif time.time() < self.gpu_backoff_until:
            parts.append(f"GPU backing off after: {self.gpu_last_error}")
        if self.cpu_refused:
            parts.append(f"CPU fallback refused: {self.cpu_refused}")
        return "; ".join(parts) or "no worker available"

    # ── housekeeping (every 3 s) ───────────────────────────────────────
    async def manage(self) -> None:
        k8s = K8s()
        template = yaml.safe_load(open(E("TTS_WORKER_TEMPLATE", "/etc/tts/worker-job.yaml")))
        while True:
            for step in (self._expire, lambda: self._manage_gpu(k8s, template), self._manage_cpu):
                try:                                     # independent: a k8s API error must not stop the CPU path
                    r = step()
                    if asyncio.iscoroutine(r):
                        await r
                except Exception:
                    LOG.exception("manage step failed")
            await asyncio.sleep(3)

    def _expire(self) -> None:
        now = time.time()
        for t in list(self.tasks.values()):
            w = self.workers.get(t.worker or "")
            if t.state == "running" and (not w or now - w["last_seen"] > WORKER_STALE):
                self.requeue(t, "worker vanished")
            if t.state in ("done", "failed", "cancelled") and now - t.finished > RESULT_TTL:
                del self.tasks[t.id]
        for name, w in list(self.workers.items()):
            if now - w["last_seen"] > 600:
                del self.workers[name]

    async def _manage_gpu(self, k8s: "K8s", template: dict) -> None:
        self.jobs = await k8s.list_jobs()
        now = time.time()
        for j in self.jobs:
            name = j["metadata"]["name"]
            if _failed(j) and name not in self.seen_failed_jobs:
                self.seen_failed_jobs.add(name)
                self.gpu_backoff_until = now + GPU_BACKOFF
                self.gpu_last_error = self.gpu_last_error or f"job {name} failed"
                LOG.warning("GPU job %s failed; backing off %ss", name, GPU_BACKOFF)
        active = [j for j in self.jobs if not _finished(j)]
        has_work = bool(self.queued()) or any(t.state == "running" and t.device == "gpu" for t in self.tasks.values())
        if has_work and not active and now >= self.gpu_backoff_until:
            name = await k8s.create_job(template)
            LOG.info("created GPU worker job %s", name)
        elif active and not has_work and now - self.last_work > GATE_IDLE_CANCEL:
            for j in active:   # still waiting in gpusched (no worker registered for its pod): give up the request
                pod_prefix = j["metadata"]["name"]
                if not any(n.startswith(pod_prefix) for n in self.workers):
                    LOG.info("no work for %ss: deleting waiting GPU job %s", GATE_IDLE_CANCEL, pod_prefix)
                    await k8s.delete_job(pod_prefix)

    def _manage_cpu(self) -> None:
        if self.cpu_proc and self.cpu_proc.poll() is not None:
            LOG.info("CPU worker exited (%s)", self.cpu_proc.returncode)
            self.cpu_proc = None
        if self.cpu_proc or not self.pick("cpu")[0]:
            return
        avail = mem_available_gib()
        if avail < CPU_MIN_AVAIL_GIB:
            self.cpu_refused = f"MemAvailable {avail:.1f} GiB < {CPU_MIN_AVAIL_GIB:.0f} GiB"
            return
        self.cpu_refused = None
        LOG.info("starting CPU worker (MemAvailable %.1f GiB)", avail)
        self.cpu_proc = subprocess.Popen(
            [sys.executable, "-m", "tts.worker", "--device", "cpu", "--api", "http://127.0.0.1:8081",
             "--idle-sec", str(CPU_IDLE), "--max-run-sec", "86400", "--threads", str(CPU_THREADS),
             "--min-avail-gib", str(CPU_KEEP_AVAIL_GIB)],
            env={**os.environ, "POD_NAME": f"{os.environ.get('POD_NAME', 'api')}-cpu"})


def _cond(j: dict, kind: str) -> bool:
    return any(c["type"] == kind and c["status"] == "True" for c in j.get("status", {}).get("conditions") or [])


def _failed(j: dict) -> bool:
    return _cond(j, "Failed")


def _finished(j: dict) -> bool:
    return _cond(j, "Failed") or _cond(j, "Complete")


def mem_available_gib() -> float:
    for line in open("/proc/meminfo"):                 # host-wide, which is what the guard needs
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 2**20
    return 0.0


class K8s:
    """The few batch/v1 calls the API needs, with the pod's service account (Role: jobs in this namespace)."""
    BASE = "https://kubernetes.default.svc"
    SA = "/var/run/secrets/kubernetes.io/serviceaccount"

    def __init__(self) -> None:
        self.c = httpx.AsyncClient(base_url=self.BASE, verify=f"{self.SA}/ca.crt", timeout=10)

    def _h(self) -> dict:
        return {"Authorization": "Bearer " + open(f"{self.SA}/token").read().strip()}

    async def list_jobs(self) -> list[dict]:
        r = await self.c.get(f"/apis/batch/v1/namespaces/{NS}/jobs", params={"labelSelector": JOB_LABEL}, headers=self._h())
        r.raise_for_status()
        return r.json()["items"]

    async def create_job(self, template: dict) -> str:
        r = await self.c.post(f"/apis/batch/v1/namespaces/{NS}/jobs", json=template, headers=self._h())
        r.raise_for_status()
        return r.json()["metadata"]["name"]

    async def delete_job(self, name: str) -> None:
        r = await self.c.delete(f"/apis/batch/v1/namespaces/{NS}/jobs/{name}",
                                json={"propagationPolicy": "Background"}, headers=self._h())
        if r.status_code not in (200, 202, 404):
            r.raise_for_status()


svc = Service()


def _auth(req: Request, token: str) -> None:
    got = req.headers.get("authorization", "")
    if not hmac.compare_digest(got.encode(), f"Bearer {token}".encode()):
        raise HTTPException(401, "missing or invalid bearer token")


# ── public app ────────────────────────────────────────────────────────
public = FastAPI(title="labzilla TTS", docs_url=None, redoc_url=None, openapi_url=None)
API_TOKEN = ""


async def _parse(req: Request, max_chars: int) -> tuple[str, dict, str | None]:
    ref = None
    if req.headers.get("content-type", "").startswith("multipart/"):
        form = await req.form()
        body = {k: v for k, v in form.items() if isinstance(v, str)}
        f = form.get("reference")
        if f is not None and not isinstance(f, str):
            data = await f.read(MAX_REF_BYTES + 1)
            if len(data) > MAX_REF_BYTES:
                raise HTTPException(413, f"reference audio over {MAX_REF_BYTES // 2**20} MiB")
            ref = base64.b64encode(data).decode()
    else:
        try:
            body = await req.json()
        except ValueError:
            raise HTTPException(400, "body must be JSON or multipart/form-data")
    text = str(body.get("input") or "").strip()
    if not text:
        raise HTTPException(400, "`input` is required")
    if len(text) > max_chars:
        raise HTTPException(413, f"`input` over {max_chars} characters; use POST /v1/jobs for long text")
    if str(body.get("response_format") or "wav") != "wav":
        raise HTTPException(400, "only response_format=wav is supported")
    params = {}
    for k, cast in (("temperature", float), ("top_p", float), ("top_k", int), ("repetition_penalty", float)):
        if body.get(k) not in (None, ""):
            try:
                params[k] = cast(body[k])
            except ValueError:
                raise HTTPException(400, f"`{k}` must be a number")
    return text, params, ref


@public.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "gpu_ready": svc.gpu_ready(), "queued": len(svc.queued())}


@public.post("/v1/audio/speech")
async def speech(req: Request) -> Response:
    _auth(req, API_TOKEN)
    text, params, ref = await _parse(req, MAX_CHARS_SYNC)
    t = Task("interactive", text, params, ref)
    svc.submit(t)
    try:
        await asyncio.wait_for(t.done.wait(), INTERACTIVE_TIMEOUT)
    except asyncio.TimeoutError:
        why = svc.why_waiting() if t.state == "queued" else "synthesis still running"
        svc.finish(t, "cancelled", error="timed out")
        raise HTTPException(503, f"not served within {INTERACTIVE_TIMEOUT:.0f}s: {why}",
                            headers={"Retry-After": "60"})
    if t.state != "done":
        raise HTTPException(400 if t.client_error else 502, t.error or t.state)
    audio = t.audio
    del svc.tasks[t.id]                                      # sync results are not kept
    return Response(audio, media_type="audio/wav",
                    headers={"X-TTS-Device": t.device or "", "X-TTS-Synth-Sec": str(t.synth_sec or "")})


@public.post("/v1/jobs", status_code=202)
async def create_job(req: Request) -> dict:
    _auth(req, API_TOKEN)
    text, params, ref = await _parse(req, MAX_CHARS_BATCH)
    t = Task("batch", text, params, ref)
    svc.submit(t)
    return t.public()


def _task(req: Request, tid: str) -> Task:
    _auth(req, API_TOKEN)
    t = svc.tasks.get(tid)
    if not t or t.kind != "batch":
        raise HTTPException(404, "no such job (results are kept for 1 h)")
    return t


@public.get("/v1/jobs/{tid}")
async def get_job(req: Request, tid: str) -> dict:
    return _task(req, tid).public()


@public.get("/v1/jobs/{tid}/audio")
async def get_audio(req: Request, tid: str) -> Response:
    t = _task(req, tid)
    if t.state != "done":
        raise HTTPException(409, f"job is {t.state}")
    return Response(t.audio, media_type="audio/wav")


@public.get("/v1/status")
async def status(req: Request) -> dict:
    _auth(req, API_TOKEN)
    now = time.time()
    return {
        "queued": {k: sum(1 for t in svc.queued() if t.kind == k) for k in ("interactive", "batch")},
        "running": [t.public() for t in svc.tasks.values() if t.state == "running"],
        "workers": {n: {**w, "age_sec": round(now - w["last_seen"])} for n, w in svc.workers.items()},
        "gpu_jobs": [{"name": j["metadata"]["name"], "finished": _finished(j), "failed": _failed(j)} for j in svc.jobs],
        "gpu_backoff_sec": max(0, round(svc.gpu_backoff_until - now)), "gpu_last_error": svc.gpu_last_error,
        "cpu_worker_running": svc.cpu_proc is not None, "cpu_refused": svc.cpu_refused,
        "mem_available_gib": round(mem_available_gib(), 1),
    }


# ── internal app (workers) ────────────────────────────────────────────
internal = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
WORKER_TOKEN = ""


def _seen(worker: str, device: str, **kw) -> None:
    w = svc.workers.setdefault(worker, {"device": device, "state": "unknown"})
    w.update(kw, device=device, last_seen=time.time())


@internal.post("/internal/state")
async def worker_state(req: Request) -> dict:
    _auth(req, WORKER_TOKEN)
    b = await req.json()
    _seen(b["worker"], b["device"], state=b["state"], **{k: b[k] for k in ("load_sec", "error") if k in b})
    LOG.info("worker %s (%s): %s %s", b["worker"], b["device"], b["state"], b.get("error") or "")
    if b["device"] == "gpu" and b["state"] == "failed":
        svc.gpu_backoff_until = time.time() + GPU_BACKOFF
        svc.gpu_last_error = b.get("error")
    if b["state"] == "busy":
        return {"ok": True}
    if b["state"] in ("exiting", "failed"):
        for t in svc.tasks.values():
            if t.state == "running" and t.worker == b["worker"]:
                svc.requeue(t, f"worker {b['state']}")
    return {"ok": True}


@internal.post("/internal/next")
async def next_task(req: Request, worker: str, device: str, wait: float = 20,
                    left: float | None = None) -> Response:
    """`left`: seconds the worker may still run (GPU: inside its gpusched lease)."""
    _auth(req, WORKER_TOKEN)
    start = time.time()
    end = start + min(wait, 25)
    while True:
        _seen(worker, device, state="ready")       # only a loaded, idle worker polls
        async with svc.lock:
            t, skipped = svc.pick(device, None if left is None else left - (time.time() - start))
            if not t and skipped:                  # work this worker can't finish in time: make room
                return Response(status_code=204, headers={"X-TTS-Exit": "budget"})
            if t:
                t.state, t.worker, t.device, t.assigned_at = "running", worker, device, time.time()
                return JSONResponse({"id": t.id, "text": t.text, "params": t.params,
                                     "reference_b64": t.reference_b64})
        if time.time() >= end:
            return Response(status_code=204)
        await asyncio.sleep(0.25)


def _mine(tid: str, worker: str) -> Task | None:
    t = svc.tasks.get(tid)
    return t if t and t.state == "running" and t.worker == worker else None    # cancelled meanwhile: drop


@internal.post("/internal/result/{tid}")
async def result(req: Request, tid: str, worker: str, synth_sec: float = 0) -> dict:
    _auth(req, WORKER_TOKEN)
    audio = await req.body()
    if t := _mine(tid, worker):
        t.synth_sec = synth_sec
        svc.finish(t, "done", audio=audio)
    return {"ok": True}


@internal.post("/internal/fail/{tid}")
async def fail(req: Request, tid: str, worker: str) -> dict:
    _auth(req, WORKER_TOKEN)
    if t := _mine(tid, worker):
        b = await req.json()
        t.client_error = bool(b.get("client_error"))
        svc.finish(t, "failed", error=b.get("error"))
    return {"ok": True}


@internal.post("/internal/requeue/{tid}")
async def requeue(req: Request, tid: str, worker: str) -> dict:
    _auth(req, WORKER_TOKEN)
    if t := _mine(tid, worker):
        svc.requeue(t, "worker stopped")
    return {"ok": True}


async def main() -> None:
    global API_TOKEN, WORKER_TOKEN
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)      # the 3 s Job listing is not news
    API_TOKEN, WORKER_TOKEN = _secret("api_token"), _secret("worker_token")
    servers = [uvicorn.Server(uvicorn.Config(public, host="0.0.0.0", port=8080, log_level="warning")),
               uvicorn.Server(uvicorn.Config(internal, host="0.0.0.0", port=8081, log_level="warning"))]
    await asyncio.gather(*(s.serve() for s in servers), svc.manage())


if __name__ == "__main__":
    asyncio.run(main())
