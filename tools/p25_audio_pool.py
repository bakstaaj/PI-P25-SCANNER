#!/usr/bin/env python3
"""Source-aware UDP PCM arbiter for scalable OP25 voice receivers."""

from __future__ import annotations

import argparse
import array
import json
import math
import os
import selectors
import signal
import socket
import tempfile
import time
from pathlib import Path
from typing import Any


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def pcm_energy(payload: bytes) -> float:
    if len(payload) < 2:
        return 0.0
    usable = payload[: min(len(payload), 4096)]
    if len(usable) % 2:
        usable = usable[:-1]
    samples = array.array("h")
    samples.frombytes(usable)
    if not samples:
        return 0.0
    total = sum(float(sample) * float(sample) for sample in samples)
    return math.sqrt(total / len(samples))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--base-port", type=int, default=23500)
    parser.add_argument("--port-count", type=int, default=10)
    parser.add_argument("--output-host", default="127.0.0.1")
    parser.add_argument("--output-port", type=int, default=23456)
    parser.add_argument("--source-hold-seconds", type=float, default=0.75)
    parser.add_argument("--minimum-rms", type=float, default=16.0)
    parser.add_argument(
        "--status-file",
        default="/run/pi-p25-audio-pool/status.json",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    selector = selectors.DefaultSelector()
    sockets: dict[int, socket.socket] = {}

    for port in range(args.base_port, args.base_port + args.port_count):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((args.listen_host, port))
        sock.setblocking(False)
        selector.register(sock, selectors.EVENT_READ, port)
        sockets[port] = sock

    output = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    output_target = (args.output_host, args.output_port)
    status_path = Path(args.status_file)

    running = True

    def stop_handler(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)

    active_port: int | None = None
    active_last_packet = 0.0
    last_status_write = 0.0
    started = time.time()
    counters: dict[int, dict[str, int]] = {
        port: {
            "packets_received": 0,
            "packets_forwarded": 0,
            "packets_dropped_non_owner": 0,
            "active_audio_packets": 0,
        }
        for port in sockets
    }
    switches = 0

    while running:
        now = time.monotonic()
        events = selector.select(timeout=0.2)

        for key, _mask in events:
            sock = key.fileobj
            port = int(key.data)
            payload, _address = sock.recvfrom(65535)
            metrics = counters[port]
            metrics["packets_received"] += 1
            energy = pcm_energy(payload)
            is_audio = energy >= args.minimum_rms
            if is_audio:
                metrics["active_audio_packets"] += 1

            owner_expired = (
                active_port is None
                or now - active_last_packet > args.source_hold_seconds
            )

            if active_port is None and is_audio:
                active_port = port
                active_last_packet = now
                switches += 1
            elif active_port == port:
                active_last_packet = now
            elif owner_expired and is_audio:
                active_port = port
                active_last_packet = now
                switches += 1

            if active_port == port:
                output.sendto(payload, output_target)
                metrics["packets_forwarded"] += 1
            else:
                metrics["packets_dropped_non_owner"] += 1

        now = time.monotonic()
        if (
            active_port is not None
            and now - active_last_packet > args.source_hold_seconds
        ):
            active_port = None

        if now - last_status_write >= 1.0:
            atomic_json(
                status_path,
                {
                    "ok": True,
                    "started_epoch": started,
                    "updated_epoch": time.time(),
                    "active_port": active_port,
                    "source_hold_seconds": args.source_hold_seconds,
                    "minimum_rms": args.minimum_rms,
                    "output": {
                        "host": args.output_host,
                        "port": args.output_port,
                    },
                    "switches": switches,
                    "sources": {
                        str(port): counters[port]
                        for port in sorted(counters)
                    },
                },
            )
            last_status_write = now

    for sock in sockets.values():
        selector.unregister(sock)
        sock.close()
    output.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
