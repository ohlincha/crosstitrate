"""
Serve a simulated Titrino on a virtual serial port (Linux/macOS pty) or a TCP
port, so the real serial code path can be tested without hardware.

    python -m titrate.simserve            # prints e.g. /dev/pts/5
    python -m titrate.simserve --tcp 7000 # connect to socket://localhost:7000
"""

import argparse
import os
import select
import socket
import sys

from .simulator import Clock, SimulatedTitrino


def serve_pty(sim):
    import tty
    master, slave = os.openpty()
    tty.setraw(master)
    tty.setraw(slave)
    print(os.ttyname(slave), flush=True)
    while True:
        r, _, _ = select.select([master], [], [], 0.05)
        if r:
            data = os.read(master, 1024)
            out = sim.receive(data)
            if out:
                os.write(master, out)


def serve_tcp(sim, port):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    print(f"socket://localhost:{port}", flush=True)
    while True:
        conn, _ = srv.accept()
        with conn:
            while True:
                data = conn.recv(1024)
                if not data:
                    break
                out = sim.receive(data)
                if out:
                    conn.sendall(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="718")
    ap.add_argument("--speed", type=float, default=1.0, help="simulation speed factor")
    ap.add_argument("--tcp", type=int, help="serve on this TCP port instead of a pty")
    ap.add_argument("--rs-control-off", action="store_true", help="simulate 'RS control: OFF'")
    a = ap.parse_args(argv)
    sim = SimulatedTitrino(model=a.model, clock=Clock(a.speed), rs_control=not a.rs_control_off)
    try:
        if a.tcp:
            serve_tcp(sim, a.tcp)
        else:
            if sys.platform == "win32":
                sys.exit("pty mode is not available on Windows; use --tcp")
            serve_pty(sim)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
