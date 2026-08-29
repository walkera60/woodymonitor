#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Copyright (C) 2026 Woody Monitor contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.


import sys
import os
from pathlib import Path
import time
import logging

sys.path.insert(0, str(Path(__file__).resolve().parent))

from Scotteprotocol.protocol import Protocol

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s"
)

DEVICE = os.environ.get("WOODY_SERIAL_DEVICE", "/dev/ttyUSB0")

print("Connecting to pellet burner...")
print("Device:", DEVICE)
print("Protocol: Scotte/PellMon")
print()

try:
    burner = Protocol(DEVICE, "auto")

    print()
    print("Protocol object created.")
    print("Dummy device:", burner.dummyDevice)
    print("Checksum:", burner.checksum)
    print("CRLF:", burner.frame_term_crlf)
    print()

    print("Reading burner version...")

    try:
        version = burner.getItem("version")
        print("VERSION:", repr(version))
    except Exception as e:
        print("VERSION ERROR:", repr(e))

    print()
    print("Reading basic values...")

    parameters = [
        "power",
        "power_kW",
        "boiler_temp",
        "chute_temp",
        "smoke_temp",
        "oxygen",
        "light",
        "feeder_time",
        "ignition_time",
        "alarm",
        "mode",
        "model",
    ]

    for parameter in parameters:
        try:
            value = burner.getItem(parameter)
            print(f"{parameter:20} = {value!r}")
        except Exception as e:
            print(f"{parameter:20} ERROR = {e!r}")

    print()
    print("Test finished.")

except Exception as e:
    print()
    print("FATAL ERROR:")
    print(repr(e))
    raise
