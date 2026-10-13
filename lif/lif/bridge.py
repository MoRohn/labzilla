"""Pod-network bridge to the shared GPU engine's LIF door.

The primary workload publishes its LLM container only on 127.0.0.1 (never the LAN). Pods cannot reach the
host's loopback, so this tiny TCP relay runs as a hostNetwork pod and listens on the K3s
cni0 gateway address only (10.42.0.1 — reachable from pods, not from the LAN):

    pod → 10.42.0.1:18102 → 127.0.0.1:8102 (host engine's LIF door, bearer key, low priority)

The LIF door itself still enforces the key, so this relay adds reachability, not access.
cni0 may not exist yet at boot; the bind is retried until it does.
"""
from __future__ import annotations

import asyncio
import os

LISTEN_HOST = os.environ.get("BRIDGE_LISTEN_HOST", "10.42.0.1")
LISTEN_PORT = int(os.environ.get("BRIDGE_LISTEN_PORT", "18102"))
TARGET_HOST = os.environ.get("BRIDGE_TARGET_HOST", "127.0.0.1")
TARGET_PORT = int(os.environ.get("BRIDGE_TARGET_PORT", "8102"))


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _handle(c_reader: asyncio.StreamReader, c_writer: asyncio.StreamWriter) -> None:
    try:
        t_reader, t_writer = await asyncio.wait_for(asyncio.open_connection(TARGET_HOST, TARGET_PORT), 5)
    except (OSError, asyncio.TimeoutError):
        c_writer.close()
        return
    await asyncio.gather(_pipe(c_reader, t_writer), _pipe(t_reader, c_writer))


async def main() -> None:
    while True:
        try:
            server = await asyncio.start_server(_handle, LISTEN_HOST, LISTEN_PORT)
            break
        except OSError as exc:            # cni0 not up yet (boot ordering) → retry
            print(f"bind {LISTEN_HOST}:{LISTEN_PORT} failed ({exc}); retrying in 10 s", flush=True)
            await asyncio.sleep(10)
    print(f"bridging {LISTEN_HOST}:{LISTEN_PORT} → {TARGET_HOST}:{TARGET_PORT}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
