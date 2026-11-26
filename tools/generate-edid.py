#!/usr/bin/env python3
"""Generate the PicoKVM WIDTHxHEIGHTp60 EDID presets in edid/.

Run from any directory. tools/edid_modes.py is shared with the NanoKVM and
NanoKVM Pro machine repositories; keep the copies identical.
"""

from pathlib import Path
from edid_modes import generate_common


if __name__ == "__main__":
    destination = Path(__file__).resolve().parents[1] / "edid"
    destination.mkdir(exist_ok=True)
    generate_common(destination, "picokvm")
