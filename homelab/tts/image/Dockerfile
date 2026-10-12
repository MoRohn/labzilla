# Chatterbox TTS (ResembleAI/chatterbox-turbo) on the GB10. arm64. One image, three roles:
#   python -m tts.api      API + work queue (never imports torch)
#   python -m tts.worker   GPU worker (gpusched-gated Job) or CPU fallback (subprocess of the API)
# chatterbox-tts 0.1.7 pins torch==2.6.0, which predates sm_121 (Blackwell GB10). So we keep
# NGC's torch and install chatterbox with --no-deps. The constraint files stop any other
# package from replacing torch. NGC ships no torchaudio, and the PyPI wheel's libtorchaudio
# fails to load against NGC's torch (ABI), so torchaudio is built from source against it
# (CPU only: chatterbox uses only resample and kaldi fbank). Commit a224ab2 = tag v2.9.1 (v2.10 needs
# torch headers newer than NGC's 2.10.0a0 snapshot).
FROM nvcr.io/nvidia/pytorch:25.11-py3@sha256:417cbf33f87b5378849df37983552cd1f8bc8b62fe1ceabe004de816a55dff21
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/models/hf HF_HUB_DISABLE_TELEMETRY=1 TQDM_DISABLE=1
COPY constraints.txt /etc/pip-constraints.txt
RUN python -c 'import torch; print("torch==" + torch.__version__)' > /etc/pip-torch.txt \
 && USE_CUDA=0 USE_FFMPEG=0 BUILD_SOX=0 MAX_JOBS=4 pip install --no-deps --no-build-isolation \
      "git+https://github.com/pytorch/audio.git@a224ab24a7f4797f6707051257265e223e12576f" \
 && pip install --no-deps "chatterbox-tts==0.1.7" "s3tokenizer==0.3.0" "conformer==0.3.2" \
 && pip install -c /etc/pip-torch.txt -c /etc/pip-constraints.txt \
      "transformers==5.2.0" "diffusers==0.29.0" "onnx==1.19.1" "resemble-perth==1.0.1" \
      "omegaconf==2.3.0" "pyloudnorm==0.1.1" \
      "fastapi==0.118.0" "uvicorn==0.37.0" "httpx==0.28.1" "python-multipart==0.0.20" \
 && python -c 'import torch, torchaudio, chatterbox.tts_turbo, fastapi; print(torch.__version__, torchaudio.__version__)' \
 && test "$(python -c 'import torch; print("torch==" + torch.__version__)')" = "$(cat /etc/pip-torch.txt)"
WORKDIR /app
COPY bench.py ./
COPY tts ./tts
USER 1000:1000
