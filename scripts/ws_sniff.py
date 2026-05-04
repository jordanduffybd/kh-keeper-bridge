#!/usr/bin/env python3
"""
Standalone WebSocket sniffer for the Reef Factory KH Keeper.

Connects to the device, joins the kh stream, and dumps every frame
in hex + decoded form. Useful for discovering new commands without
needing the device's web UI.

Workflow:
    1. Run this script.
    2. Trigger an action from the device's physical buttons or the
       Smart Reef mobile app (e.g. cuvette rinse, calibration).
    3. Watch the captured frames here. The frame layout is
       serial\\0command\\0subcommand\\0txid\\0payload\\0 — the
       command/subcommand strings are visible in the ASCII column.

Usage:
    python3 ws_sniff.py --host 192.168.1.229
    python3 ws_sniff.py --host 192.168.1.229 --filter khCommand
    python3 ws_sniff.py --host 192.168.1.229 --tx-only   # only outbound to device

Requires: pip install websockets
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

try:
    import websockets
except ImportError:
    print("Install: pip install websockets", file=sys.stderr)
    sys.exit(1)


def hex_ascii(data: bytes, width: int = 16) -> str:
    """Render bytes as hex + printable ASCII columns."""
    out = []
    for i in range(0, len(data), width):
        chunk = data[i:i + width]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"  {hex_part:<{width * 3}}  {ascii_part}")
    return "\n".join(out)


def decode_frame(data: bytes) -> tuple[str, str, str, str, bytes]:
    """Extract serial / command / subcommand / txid / payload."""
    parts: list[str] = []
    cursor = 0
    try:
        for _ in range(4):
            nul = data.index(0, cursor)
            parts.append(data[cursor:nul].decode("ascii", errors="replace"))
            cursor = nul + 1
        return parts[0], parts[1], parts[2], parts[3], data[cursor:]
    except (ValueError, IndexError):
        return "", "?", "?", "", data


def encode_frame(serial: str, command: str, subcommand: str, txid: str, payload: bytes = b"") -> bytes:
    return (
        serial.encode("ascii") + b"\x00"
        + command.encode("ascii") + b"\x00"
        + subcommand.encode("ascii") + b"\x00"
        + txid.encode("ascii") + b"\x00"
        + payload + b"\x00"
    )


async def main(args) -> None:
    uri = f"ws://{args.host}/controler"
    print(f"→ {uri} (subprotocol=arduino)")
    print("  (these devices accept one WS client at a time — if you see a")
    print("   handshake timeout, stop the kh-keeper-bridge add-on first)\n")

    try:
        ws_ctx = websockets.connect(
            uri,
            subprotocols=["arduino"],
            ping_interval=20,
            ping_timeout=15,
            open_timeout=args.connect_timeout,
            origin=f"http://{args.host}",
            user_agent_header="Mozilla/5.0",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"connect setup failed: {exc}", file=sys.stderr)
        return

    try:
        ws = await ws_ctx
    except (TimeoutError, asyncio.TimeoutError):
        print("Handshake timed out. Most likely the bridge add-on has the WS open —", file=sys.stderr)
        print("stop it (HA → Add-ons → KH Keeper Bridge → Stop), then re-run.", file=sys.stderr)
        sys.exit(2)
    except OSError as exc:
        print(f"TCP connect failed: {exc}", file=sys.stderr)
        sys.exit(2)

    async with ws:
        # Request config (gets us the serial back, then we can join)
        await ws.send(encode_frame("", "get", "config", "", b""))
        serial = None

        async for raw in ws:
            if not isinstance(raw, (bytes, bytearray)):
                continue
            data = bytes(raw)
            ts = time.strftime("%H:%M:%S")
            ser, cmd, sub, txid, payload = decode_frame(data)

            # Once we have the serial, send a join so we get all subsequent
            # khRefresh/* frames the device pushes.
            if serial is None and cmd == "refresh" and sub == "config":
                serial = data.split(b"\x00", 1)[0].decode("ascii", errors="replace")
                join = encode_frame(serial, "khConnect", "join", "join_sniff",
                                    serial.encode("ascii") + b"\x00")
                await ws.send(join)
                print(f"[{ts}] Joined as {serial}\n")

            if args.filter and args.filter not in cmd and args.filter not in sub:
                continue

            print(f"[{ts}] RX  cmd={cmd:<14} sub={sub:<20} txid={txid or '-':<14} len={len(payload)}")
            if args.show_payload and payload:
                print(hex_ascii(payload))
            if args.show_full:
                print(hex_ascii(data))
            print()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", required=True, help="KH Keeper IP, e.g. 192.168.1.229")
    p.add_argument("--filter", help="Only show frames whose cmd/sub contains this string")
    p.add_argument("--show-payload", action="store_true", help="Hex-dump the payload bytes")
    p.add_argument("--show-full", action="store_true", help="Hex-dump the entire frame")
    p.add_argument("--connect-timeout", type=float, default=10.0,
                   help="Seconds to wait for the WS handshake (default 10)")
    args = p.parse_args()
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        pass
