"""Phase 0 passive Rako listener.

Logs one line per Rako status packet on UDP 9761: timestamp, source, raw bytes,
and python-rako's parse of the packet.

Two capture modes, tried in order:
  1. bind   — normal UDP bind on 9761 with SO_REUSEADDR/SO_REUSEPORT (works when
              nothing else on the host owns the port, or everything sets reuse flags)
  2. sniff  — Linux AF_PACKET raw socket filtered to UDP dst port 9761. Coexists with
              another process (e.g. Home Assistant) that has 9761 bound exclusively.
              Needs root / CAP_NET_RAW (a Docker container running as root is fine).

Requires: python-rako-2025 (pip install python-rako-2025).
Usage: python listen.py [logfile]   (default: broadcasts.log)
"""

import asyncio
import datetime
import socket
import struct
import sys

from python_rako.helpers import deserialise_byte_list

PORT = 9761
LOG = sys.argv[1] if len(sys.argv) > 1 else "broadcasts.log"
ETH_P_IP = 0x0800


def log_packet(f, data: bytes, src: tuple[str, int]) -> None:
    ts = datetime.datetime.now().isoformat(timespec="milliseconds")
    byte_list = list(data)
    try:
        msg = deserialise_byte_list(byte_list)
    except Exception as e:  # noqa: BLE001 — never let a parse error kill the log
        msg = f"PARSE_ERROR: {e!r}"
    f.write(f"{ts} from={src[0]}:{src[1]} raw={byte_list} parsed={msg}\n")


def try_bind_socket() -> socket.socket | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    try:
        sock.bind(("0.0.0.0", PORT))
    except OSError as e:
        sock.close()
        print(f"bind on {PORT} failed ({e}); falling back to packet sniffing", file=sys.stderr)
        return None
    sock.setblocking(False)
    return sock


async def run_bind_mode(f, sock: socket.socket) -> None:
    loop = asyncio.get_running_loop()
    f.write("# mode=bind\n")
    while True:
        data, addr = await loop.sock_recvfrom(sock, 4096)
        log_packet(f, data, (addr[0], addr[1]))


async def run_sniff_mode(f) -> None:
    if not hasattr(socket, "AF_PACKET"):
        sys.exit("packet sniffing needs Linux (AF_PACKET); port is busy and no fallback")
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_IP))
    sock.setblocking(False)
    loop = asyncio.get_running_loop()
    f.write("# mode=sniff\n")
    last: tuple[bytes, float] | None = None  # dedupe: same frame seen on bond + physical NIC
    while True:
        frame = await loop.sock_recv(sock, 65535)
        # Ethernet(14) + IPv4 header (IHL*4) + UDP(8)
        if len(frame) < 34:
            continue
        ihl = (frame[14] & 0x0F) * 4
        if frame[23] != 17:  # not UDP
            continue
        udp = 14 + ihl
        if len(frame) < udp + 8:
            continue
        src_port, dst_port, udp_len = struct.unpack("!HHH", frame[udp : udp + 6])
        if dst_port != PORT:
            continue
        src_ip = socket.inet_ntoa(frame[26:30])
        payload = frame[udp + 8 : udp + udp_len]
        now = loop.time()
        if last is not None and last[0] == payload and now - last[1] < 0.05:
            continue
        last = (payload, now)
        log_packet(f, payload, (src_ip, src_port))


async def main() -> None:
    with open(LOG, "a", buffering=1) as f:  # line-buffered so tail -f works
        f.write(
            f"# listener started {datetime.datetime.now().isoformat()} "
            f"host={socket.gethostname()}\n"
        )
        sock = try_bind_socket()
        if sock is not None:
            await run_bind_mode(f, sock)
        else:
            await run_sniff_mode(f)


if __name__ == "__main__":
    asyncio.run(main())
