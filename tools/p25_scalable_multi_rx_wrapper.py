#!/usr/bin/env python3
"""Upgrade the validated single-rx launch to an auto-discovered OP25 pool."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MARKER_PATH = PROJECT_ROOT / "runtime/settings/op25_validated_rx_command.env"
ACTIVE_CONFIG_PATH = PROJECT_ROOT / "runtime/settings/p25_systems.json"
DEFAULT_MULTI_CONFIG = PROJECT_ROOT / "runtime/op25/multi_rx_pool.json"
POOL_STATE_PATH = PROJECT_ROOT / "runtime/op25/scalable_receiver_pool.json"
OVERRIDES_PATH = PROJECT_ROOT / "runtime/settings/p25_receiver_overrides.json"

SERIAL_PATTERN_DEFAULT = r"^0000025[0-9]$"
AUDIO_BASE_PORT_DEFAULT = 23500
AUDIO_PORT_COUNT_DEFAULT = 10


def log(message: str) -> None:
    print(f"SCALABLE_RX_POOL: {message}", file=sys.stderr, flush=True)


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'").strip('"')
    return values


def atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
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


def discover_rtl_serials() -> tuple[list[str], str]:
    result = subprocess.run(
        ["timeout", "10", "rtl_test", "-t", "-d", "0"],
        text=True,
        capture_output=True,
        check=False,
    )
    output = (result.stdout or "") + (result.stderr or "")
    serials: list[str] = []
    seen: set[str] = set()
    pattern = re.compile(
        r"^\s*\d+:\s*.*?SN:\s*(\S+)\s*$",
        re.MULTILINE,
    )
    for match in pattern.finditer(output):
        serial = match.group(1)
        if serial not in seen:
            seen.add(serial)
            serials.append(serial)
    return serials, output


def parse_cli(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--args", dest="device_args", default="")
    parser.add_argument("-S", dest="sample_rate", default="960000")
    parser.add_argument("-q", dest="ppm", default="0")
    parser.add_argument("-N", dest="gains", default="LNA:40")
    parser.add_argument("-T", dest="trunk_tsv", default="")
    parser.add_argument("-l", dest="terminal", default="http:127.0.0.1:18091")
    parser.add_argument("--crypt-behavior", dest="crypt_behavior", default="2")
    parser.add_argument("-v", dest="verbosity", default="5")
    parsed, _unknown = parser.parse_known_args(argv)
    return parsed


def serial_from_device_args(device_args: str) -> str:
    if not device_args.startswith("rtl="):
        return ""
    return device_args.split("=", 1)[1].strip()


def read_active_system() -> dict[str, Any]:
    payload = json.loads(ACTIVE_CONFIG_PATH.read_text(encoding="utf-8"))
    systems = [
        item
        for item in payload.get("systems", [])
        if isinstance(item, dict) and item.get("enabled", True)
    ]
    if not systems:
        raise RuntimeError("no enabled P25 system")
    return systems[0]


def read_trunk_row(path: Path) -> dict[str, str]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    if not rows:
        raise RuntimeError(f"trunk TSV has no system row: {path}")
    return {str(key): str(value or "") for key, value in rows[0].items()}


def read_overrides() -> dict[str, Any]:
    if not OVERRIDES_PATH.exists():
        return {}
    payload = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def receiver_settings(
    serial: str,
    system: dict[str, Any],
    default_gains: str,
    default_ppm: float,
    overrides: dict[str, Any],
) -> tuple[str, float, bool]:
    gains = default_gains
    ppm = default_ppm
    enabled = True

    roles = system.get("receiver_roles")
    if isinstance(roles, dict):
        for role in roles.values():
            if (
                isinstance(role, dict)
                and str(role.get("rtl_serial") or "") == serial
            ):
                gain_db = role.get("gain_db")
                if gain_db not in (None, ""):
                    gains = f"LNA:{int(round(float(gain_db)))}"
                ppm = float(role.get("ppm") or 0)

    receiver_overrides = overrides.get("receivers")
    if isinstance(receiver_overrides, dict):
        item = receiver_overrides.get(serial)
        if isinstance(item, dict):
            enabled = bool(item.get("enabled", True))
            if item.get("gains"):
                gains = str(item["gains"])
            elif item.get("gain_db") not in (None, ""):
                gains = f"LNA:{int(round(float(item['gain_db'])))}"
            if item.get("ppm") not in (None, ""):
                ppm = float(item["ppm"])

    return gains, ppm, enabled


def stable_audio_port(serial: str, base_port: int) -> int:
    return base_port + int(serial[-1])


def fallback_exec(
    marker: dict[str, str],
    original_argv: list[str],
    reason: str,
) -> None:
    app = marker.get("P25_VALIDATED_SINGLE_RX_APP", "").strip()
    if not app:
        raise RuntimeError(
            "single-rx fallback app missing from validated marker"
        )
    log(f"FALLBACK_SINGLE_RX reason={reason} app={app}")
    os.execv(app, [app, *original_argv])


def main(argv: list[str] | None = None) -> int:
    original_argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_cli(original_argv)
    marker = read_env_file(MARKER_PATH)

    if marker.get("P25_SCALABLE_POOL_ENABLED", "0") != "1":
        fallback_exec(marker, original_argv, "pool_disabled")

    multi_app = marker.get("P25_VALIDATED_MULTI_RX_APP", "").strip()
    if not multi_app or not Path(multi_app).is_file():
        fallback_exec(marker, original_argv, "multi_rx_missing")

    control_serial = serial_from_device_args(args.device_args)
    system = read_active_system()
    roles = system.get("receiver_roles")
    if isinstance(roles, dict):
        control_role = roles.get("p25_control")
        if isinstance(control_role, dict) and control_role.get("rtl_serial"):
            control_serial = str(control_role["rtl_serial"])

    pattern_text = marker.get(
        "P25_SCALABLE_POOL_SERIAL_REGEX",
        SERIAL_PATTERN_DEFAULT,
    )
    serial_pattern = re.compile(pattern_text)
    discovered, rtl_output = discover_rtl_serials()
    pool = sorted(
        serial for serial in discovered
        if serial_pattern.fullmatch(serial)
    )

    if control_serial not in pool:
        fallback_exec(
            marker,
            original_argv,
            f"control_serial_not_available:{control_serial}",
        )

    pool = [control_serial] + [
        serial for serial in pool
        if serial != control_serial
    ]

    overrides = read_overrides()
    filtered: list[str] = []
    for serial in pool:
        _gains, _ppm, receiver_enabled = receiver_settings(
            serial,
            system,
            args.gains,
            float(args.ppm),
            overrides,
        )
        if receiver_enabled or serial == control_serial:
            filtered.append(serial)
    pool = filtered

    if len(pool) < 2:
        fallback_exec(
            marker,
            original_argv,
            f"fewer_than_two_pool_receivers:{pool}",
        )

    trunk_path = Path(args.trunk_tsv)
    trunk = read_trunk_row(trunk_path)
    system_name = (
        trunk.get("Sysname")
        or str(system.get("name") or "P25 System")
    )
    control_list = trunk.get("Control Channel List", "")
    control_frequencies = [
        float(value.strip()) * 1_000_000
        for value in control_list.split(",")
        if value.strip()
    ]
    if not control_frequencies:
        raise RuntimeError("no control frequencies in generated trunk TSV")
    initial_frequency = int(round(control_frequencies[0]))

    base_port = int(
        marker.get(
            "P25_SCALABLE_AUDIO_BASE_PORT",
            str(AUDIO_BASE_PORT_DEFAULT),
        )
    )
    count = int(
        marker.get(
            "P25_SCALABLE_AUDIO_PORT_COUNT",
            str(AUDIO_PORT_COUNT_DEFAULT),
        )
    )

    devices: list[dict[str, Any]] = []
    channels: list[dict[str, Any]] = []
    receiver_manifest: list[dict[str, Any]] = []

    modulation = str(
        trunk.get("Modulation")
        or system.get("modulation")
        or "CQPSK"
    ).lower()
    if modulation == "c4fm":
        modulation = "fsk4"

    for index, serial in enumerate(pool):
        port = stable_audio_port(serial, base_port)
        if not (base_port <= port < base_port + count):
            raise RuntimeError(
                f"serial {serial} maps outside audio port pool"
            )

        gains, ppm, _receiver_enabled = receiver_settings(
            serial,
            system,
            args.gains,
            float(args.ppm),
            overrides,
        )
        device_name = f"rtl_{serial}"
        role = "control" if index == 0 else "voice"

        devices.append(
            {
                "args": f"rtl={serial}",
                "frequency": initial_frequency,
                "gains": gains,
                "name": device_name,
                "offset": 0,
                "ppm": ppm,
                "rate": int(float(args.sample_rate)),
                "tunable": True,
            }
        )
        channels.append(
            {
                "name": f"P25 {role} {serial}",
                "device": device_name,
                "trunking_sysname": system_name,
                "demod_type": modulation,
                "destination": f"udp://127.0.0.1:{port}",
                "meta_stream_name": "",
                "excess_bw": 0.2,
                "filter_type": "rc",
                "frequency": initial_frequency,
                "if_rate": 24000,
                "symbol_rate": 4800,
                "enable_analog": "off",
                "blacklist": trunk.get("Blacklist", ""),
                "whitelist": trunk.get("Whitelist", ""),
                "crypt_behavior": int(args.crypt_behavior),
            }
        )
        receiver_manifest.append(
            {
                "serial": serial,
                "role": role,
                "device_name": device_name,
                "audio_port": port,
                "gains": gains,
                "ppm": ppm,
            }
        )


    terminal = args.terminal or "http:127.0.0.1:18091"
    config = {
        "devices": devices,
        "channels": channels,
        "trunking": {
            "module": "tk_p25.py",
            "chans": [
                {
                    "nac": trunk.get("NAC", "0") or "0",
                    "sysname": system_name,
                    "control_channel_list": control_list,
                    "whitelist": trunk.get("Whitelist", ""),
                    "blacklist": trunk.get("Blacklist", ""),
                    "tgid_tags_file": trunk.get("TGID Tags File", ""),
                    "crypt_behavior": int(args.crypt_behavior),
                }
            ],
        },
        "terminal": {
            "module": "terminal.py",
            "terminal_type": terminal,
            "curses_plot_interval": 0.1,
            "http_plot_interval": 1.0,
            "http_plot_directory": "../www/images",
        },
    }

    output_path = Path(
        marker.get(
            "P25_SCALABLE_MULTI_RX_CONFIG",
            str(DEFAULT_MULTI_CONFIG),
        )
    )
    atomic_json_write(output_path, config)
    atomic_json_write(
        POOL_STATE_PATH,
        {
            "mode": "multi_rx",
            "control_serial": control_serial,
            "voice_serials": pool[1:],
            "receiver_count": len(pool),
            "receivers": receiver_manifest,
            "serial_regex": pattern_text,
            "multi_rx_app": multi_app,
            "multi_rx_config": str(output_path),
            "rtl_test_output": rtl_output,
        },
    )

    log(
        "MULTI_RX_ENABLED "
        f"control={control_serial} "
        f"voice={','.join(pool[1:])} "
        f"count={len(pool)} "
        f"config={output_path}"
    )
    os.execv(
        multi_app,
        [
            multi_app,
            "-c",
            str(output_path),
            "-v",
            str(args.verbosity),
        ],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
