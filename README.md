# Titrate

Cross-platform titration software for **Metrohm Titrino** instruments controlled over
RS232. Titrate runs potentiometric (EMF) titrations, plots them live, and saves the data.
It also includes connection diagnostics and a built-in simulator.

It replaces the old *WinTitrate* (Visual Basic 6, Windows-only) program used with a
718 STAT Titrino. It can still write WinTitrate's data format for existing analysis programs.

- **Windows, macOS and Linux.** Installers bundle everything.
- **Titrations:**
  - Equilibrate for a fixed time per point, or until the EMF drift is below a limit.
  - Optionally halve or double the volume step depending on how much the EMF changed.
  - Stop at a volume, at an EMF limit, or by hand.
- **Burettes:**
  - Several burettes, on several Titrinos or on the D0/D1/D2 burettes of multi-burette models.
  - Dispense from one burette, or from several together in fixed ratios.
  - **Change burette during a titration.** The new concentrations are recorded, and a new
    volume segment starts.
  - Automatic refilling before a burette runs empty. Dispensed volumes are checked against
    the instrument's own volume counter.
- **Live plot and data table.**
  - Every reading is saved immediately, so a crash or power cut loses nothing.
  - Pause and resume, delayed start, manual measure/monitor/add/fill.
- **Diagnostics:**
  - Port and permission checks, handshake-line status.
  - Automatic baud and parity scan, instrument identification.
  - A live communication log, and plain-language advice for each problem.
- **Simulator.** Try everything without an instrument, at up to 300× speed.

## Install

Download from the [Releases page](https://github.com/ohlincha/titrate/releases/latest):

| System | File | How |
|---|---|---|
| Windows | `Titrate.exe` | Run it. If SmartScreen warns, choose *More info → Run anyway*. |
| macOS | `Titrate-macOS.dmg` | Drag *Titrate* to Applications. The first time, right-click → *Open*. |
| Linux (Debian/Ubuntu) | `titrate_<version>_all.deb` | `sudo apt install ./titrate_*_all.deb`, then `sudo usermod -aG dialout $USER` and log out and in again. |

From source, on any OS, with Python ≥ 3.8 and Tk: `pip install pyserial`, then `python -m titrate`.

## Connecting a Titrino

1. **Cable.** Connect the Titrino's RS232 socket (25-pin, marked *A* or *RS232*) to the
   computer. Use Metrohm cable 6.2125.060, plus adapter 6.2125.010 for 9-pin ports, or any
   null-modem cable. Most computers need a **USB–RS232 adapter**; FTDI-based adapters are
   the most reliable.
2. **Titrino settings.** Press `<configuration>` and go to `>RS232 settings`:

   | Setting | Value |
   |---|---|
   | baud rate | 9600 |
   | data bit | 8 |
   | stop bit | 1 |
   | parity | none |
   | handshake | none (or HWs with a fully wired cable) |
   | **RS control** | **ON**. With OFF, the Titrino ignores all commands. |

   Under `>peripheral units`, set *send to* to `IBM`. Leave the instrument idle, at its
   normal display. Titrate selects MEAS mode and dispensing by itself.
3. **In Titrate:** *Instruments → Connect…*, choose the port, and press **Test connection**.
   If it fails, the diagnostics say why and try other settings for you.

Network serial servers (e.g. Moxa NPort) also work: enter `socket://host:port` as the port.

## Running a titration

1. **Burettes tab:** define each burette: the instrument that drives it, the dispensing
   rate, and the concentrations of components H, B, C and D in mM (negative H for base, as
   in WinTitrate). Tick the burette(s) to use.
2. **Sample tab:** start volume V₀, temperature, start concentrations, comment.
3. **Method tab:**
   - Choose point acceptance:
     - *constant*: wait the equilibration time, then read.
     - *drift*: read every interval until |EMF − EMF N readings earlier| is below the limit,
       or the maximum number of readings is reached.
   - Volume step, optional adaptive step, and stop conditions.
   - Methods can be saved and loaded as JSON.
4. Choose the data file and press **Start titration**. The plot shows accepted points in
   blue and intermediate drift readings in grey.
5. **Change burette…** switches burettes, or updates concentrations, at the next addition.
   The data file records the change, and the segment volume restarts at 0, as in WinTitrate.

## Data files

**CSV** (primary). A header of `# key: value` lines holds the sample, method, burettes,
instruments and start/end times as JSON. It's followed by one row per reading:

```
timestamp,elapsed_s,segment,kind,burettes,v_total_ml,v_segment_ml,emf_mv,temperature_c,drift_mv,note
```

`kind` is `eq` (accepted point), `drift` (intermediate reading) or `event` (a burette
change). `v_total_ml` is cumulative over the whole titration; `v_segment_ml` restarts at
each burette change.

**WinTitrate format** (`.txt`, optional). Written automatically at the end of a
titration, or later with *Export WinTitrate…* from any CSV:

```
"Datum",#2026-11-19#
"Start:konc.",50,10,0,0,0,0,0
"Byrkonc",-100,0,0,0,0,0,0
"10 23 45",.5,25,278.6,0,0
```

## Supported instruments

All Titrinos with RS232 remote control share one command language. Titrate identifies the
model from its program version and adapts to it.

| Model | Notes |
|---|---|
| 718 STAT Titrino | Reference model (manual + the WinTitrate program) |
| 702, 716, 719, 736, 794 | Standard Titrinos |
| 751 GPD, 785 DMP, 798 MPT, 799 GPT | Also burettes D0/D1/D2 on one instrument |
| 720, 758, 784, 795 (KF) | No mV measurement; usable as burettes |

**Hardware status:** so far this version has been tested only against the simulator. Please
report results with real instruments; the model table will mark verified models.

**USB:** RS232 Titrinos work through any USB–RS232 adapter, on all three systems. The newer
USB-only instruments (Titrino plus 848/877, Titrando, OMNIS) use proprietary Metrohm
protocols that are not publicly documented. They are not supported.

## Troubleshooting

Start with *Instruments → Diagnostics…*. Common causes:

- **No answer:** RS control is OFF; wrong port; straight instead of null-modem cable;
  Titrino busy or showing a question.
- **E42 on the Titrino display:** a handshake problem. Set handshake to `none` on the
  Titrino, or keep *Raise RTS/DTR* ticked and use a fully wired cable.
- **Garbled data / E36–E37:** baud, parity or data bits differ. The diagnostics scan finds
  the right settings.
- **Linux "permission denied":** `sudo usermod -aG dialout $USER`, then log in again.
- **Windows, Prolific adapter "code 10":** use an FTDI-based adapter.

The *Communication log* tab shows every command and reply.

## Development

```sh
python -m venv .venv && .venv/bin/pip install pyserial
.venv/bin/python -m titrate                  # GUI
.venv/bin/python -m unittest discover tests  # tests (use the simulator)
python -m titrate.simserve                   # simulated Titrino on a virtual serial port
```

Code layout: `protocol.py` (Titrino command language), `transport.py` (serial/simulator
I/O and the traffic log), `instrument.py` (driver), `titration.py` (titration logic and
worker thread), `diagnostics.py`, `datafile.py`, `simulator.py`, `gui/`.

Pushing a tag `vX.Y.Z` builds the Windows, macOS and Linux installers on GitHub Actions
and publishes a release.

## License

MIT
