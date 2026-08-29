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


import serial
import time

PORT = "/dev/serial/by-id/usb-FTDI_Chipi-X_FT2UXS6M-if00-port0"
BAUD = 9600

print(f"Opening {PORT}")
print(f"Baud rate: {BAUD}")
print("Listening for 30 seconds...")
print("No data will be transmitted to the burner.")
print()

ser = serial.Serial(
    port=PORT,
    baudrate=BAUD,
    bytesize=serial.EIGHTBITS,
    parity=serial.PARITY_NONE,
    stopbits=serial.STOPBITS_ONE,
    timeout=0.1,
)

start = time.monotonic()

try:
    while time.monotonic() - start < 30:
        data = ser.read(256)

        if data:
            now = time.strftime("%H:%M:%S")
            hexdata = " ".join(f"{b:02X}" for b in data)
            print(f"{now}  {hexdata}")

finally:
    ser.close()
    print()
    print("Serial port closed.")
