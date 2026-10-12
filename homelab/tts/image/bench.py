"""Feasibility benchmark: load chatterbox-turbo, synthesise fixed sentences, report load time,
real-time factor (RTF = synth seconds / audio seconds; < 1 is faster than real time) and peak memory.

    python bench.py --device cuda|cpu [--threads N] [--runs 3]
Prints one JSON line at the end.
"""
import argparse, json, os, resource, statistics, subprocess, time

import torch

TEXTS = [
    "Hello from labzilla. This is a short test of the turbo voice.",
    "The quick brown fox jumps over the lazy dog, and then it takes a well deserved nap in the afternoon sun.",
    "Local text to speech on the DGX Spark should be fast enough for an interactive assistant, "
    "but we measure it instead of guessing, because the primary workload comes first.",
]
REVISION = "749d1c1a46eb10492095d68fbcf55691ccf137cd"   # ResembleAI/chatterbox-turbo, pinned


def gpu_mib_for_pid() -> int | None:
    try:
        out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    # In a container the PID namespace differs, so report the total across visible compute apps.
    vals = [int(l.split(",")[1]) for l in out.strip().splitlines() if l.strip() and l.split(",")[1].strip().isdigit()]
    return sum(vals) if vals else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--threads", type=int, default=0)
    ap.add_argument("--runs", type=int, default=3)
    a = ap.parse_args()
    if a.threads:
        torch.set_num_threads(a.threads)

    from huggingface_hub import snapshot_download
    from chatterbox.tts_turbo import ChatterboxTurboTTS

    t = time.perf_counter()
    path = snapshot_download("ResembleAI/chatterbox-turbo", revision=REVISION,
                             allow_patterns=["*.safetensors", "*.json", "*.txt", "*.pt", "*.model", "*.yaml"])
    t_dl = time.perf_counter() - t
    t = time.perf_counter()
    model = ChatterboxTurboTTS.from_local(path, a.device)
    t_load = time.perf_counter() - t
    print(f"loaded in {t_load:.1f}s (download {t_dl:.1f}s) on {a.device}", flush=True)

    model.generate("Warm up.")                                       # first call compiles/allocates
    rtfs, first = [], None
    for _ in range(a.runs):
        for text in TEXTS:
            t = time.perf_counter()
            wav = model.generate(text)
            if a.device == "cuda":
                torch.cuda.synchronize()
            dt = time.perf_counter() - t
            audio_s = wav.shape[-1] / model.sr
            rtfs.append(dt / audio_s)
            first = first or dt
            print(f"{dt:6.2f}s for {audio_s:5.2f}s audio  RTF {dt / audio_s:.3f}", flush=True)

    from scipy.io import wavfile                                     # torchaudio.save needs torchcodec
    wavfile.write(f"/tmp/sample-{a.device}.wav", model.sr, wav.squeeze(0).numpy())
    res = {
        "device": a.device, "threads": torch.get_num_threads(), "torch": torch.__version__,
        "load_sec": round(t_load, 1), "n": len(rtfs),
        "rtf_median": round(statistics.median(rtfs), 3), "rtf_max": round(max(rtfs), 3),
        "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024,
    }
    if a.device == "cuda":
        res |= {"cuda_max_allocated_mib": torch.cuda.max_memory_allocated() // 2**20,
                "cuda_max_reserved_mib": torch.cuda.max_memory_reserved() // 2**20,
                "nvidia_smi_used_mib": gpu_mib_for_pid(),
                "capability": ".".join(map(str, torch.cuda.get_device_capability()))}
    print("RESULT " + json.dumps(res), flush=True)


if __name__ == "__main__":
    main()
