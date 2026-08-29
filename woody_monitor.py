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
import time
import threading
import logging
import os
import socket
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from Scotteprotocol.protocol import Protocol
from database import Database
import json
import paho.mqtt.client as mqtt


# ============================================================
# SYSTEMD WATCHDOG
# ============================================================

def systemd_watchdog_loop():

    notify_socket = os.environ.get("NOTIFY_SOCKET")

    if not notify_socket:
        logger.info("systemd watchdog not available")
        return

    if notify_socket.startswith("@"):
        notify_socket = "\0" + notify_socket[1:]

    logger.info("systemd watchdog active")

    while True:

        try:
            sock = socket.socket(
                socket.AF_UNIX,
                socket.SOCK_DGRAM
            )

            sock.connect(notify_socket)
            sock.sendall(b"WATCHDOG=1")
            sock.close()

        except Exception:
            logger.exception(
                "systemd watchdog error"
            )

        time.sleep(10)


# ============================================================
# CONFIG
# ============================================================

DEVICE = "/dev/serial/by-id/usb-FTDI_Chipi-X_FT2UXS6M-if00-port0"

HOST = "0.0.0.0"
PORT = 8080

LIVE_INTERVAL = 5
HISTORY_INTERVAL = 10

# MQTT
MQTT_BROKER = os.environ.get("WOODY_MQTT_BROKER", "localhost")
MQTT_PORT = 1883
MQTT_USERNAME = os.environ.get("WOODY_MQTT_USERNAME", "")
MQTT_PASSWORD = os.environ.get("WOODY_MQTT_PASSWORD", "")
MQTT_TOPIC = "woodymonitor"
MQTT_INTERVAL = 5


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

logger = logging.getLogger("woody-monitor")


# ============================================================
# APPLICATION
# ============================================================

app = FastAPI(
    title="Woody Monitor",
    description="Local API for Woody pellet burner monitoring",
    version="1.1"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# DATABASE
# ============================================================

db = Database()


# ============================================================
# PELLET CALIBRATION
# ============================================================

# Default calibration:
# 1200 grams in 360 seconds (6 minutes)
DEFAULT_FEEDER_GRAMS = 1200.0
DEFAULT_FEEDER_SECONDS = 360.0

feeder_calibration_lock = threading.Lock()

FEEDER_SETTINGS_FILE = str(Path(__file__).resolve().parent / "data" / "feeder_settings.json")

feeder_calibration = {
    "grams": DEFAULT_FEEDER_GRAMS,
    "seconds": DEFAULT_FEEDER_SECONDS
}


def load_feeder_calibration():

    global feeder_calibration

    try:

        path = Path(FEEDER_SETTINGS_FILE)

        if path.exists():

            with path.open("r") as f:
                data = json.load(f)

            grams = float(data.get(
                "grams",
                DEFAULT_FEEDER_GRAMS
            ))

            if 1 <= grams <= 10000:

                feeder_calibration["grams"] = grams

            logger.info(
                "Loaded feeder calibration: %.1f g / %.0f s",
                feeder_calibration["grams"],
                feeder_calibration["seconds"]
            )

    except Exception:

        logger.exception(
            "Could not load feeder calibration"
        )


def save_feeder_calibration():

    path = Path(FEEDER_SETTINGS_FILE)

    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temporary = path.with_suffix(".tmp")

    with temporary.open("w") as f:

        json.dump(
            feeder_calibration,
            f,
            indent=2
        )

    temporary.replace(path)


def get_feeder_calibration():

    with feeder_calibration_lock:
        return dict(feeder_calibration)


# ============================================================
# PELLET SILO SETTINGS
# ============================================================

DEFAULT_SILO_CAPACITY_KG = 215.0

SILO_SETTINGS_FILE = str(Path(__file__).resolve().parent / "data" / "silo_settings.json")

silo_settings_lock = threading.Lock()

silo_settings = {
    "capacity_kg": DEFAULT_SILO_CAPACITY_KG
}


def load_silo_settings():

    global silo_settings

    try:

        path = Path(SILO_SETTINGS_FILE)

        if path.exists():

            with path.open("r") as f:
                data = json.load(f)

            capacity = float(
                data.get(
                    "capacity_kg",
                    DEFAULT_SILO_CAPACITY_KG
                )
            )

            if 1 <= capacity <= 5000:

                silo_settings["capacity_kg"] = capacity

            logger.info(
                "Loaded silo capacity: %.1f kg",
                silo_settings["capacity_kg"]
            )

    except Exception:

        logger.exception(
            "Could not load silo settings"
        )


def save_silo_settings():

    path = Path(SILO_SETTINGS_FILE)

    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temporary = path.with_suffix(".tmp")

    with temporary.open("w") as f:

        json.dump(
            silo_settings,
            f,
            indent=2
        )

    temporary.replace(path)


def get_silo_settings():

    with silo_settings_lock:
        return dict(silo_settings)


# ============================================================
# SILO SETTINGS
# ============================================================

DEFAULT_SILO_CAPACITY_KG = 215.0

SILO_SETTINGS_FILE = str(Path(__file__).resolve().parent / "data" / "silo_settings.json")

silo_settings_lock = threading.Lock()

silo_settings = {
    "capacity_kg": DEFAULT_SILO_CAPACITY_KG
}


def load_silo_settings():

    global silo_settings

    try:

        path = Path(SILO_SETTINGS_FILE)

        if path.exists():

            with path.open("r") as f:
                data = json.load(f)

            capacity = float(
                data.get(
                    "capacity_kg",
                    DEFAULT_SILO_CAPACITY_KG
                )
            )

            if 1 <= capacity <= 5000:
                silo_settings["capacity_kg"] = capacity

        logger.info(
            "Loaded silo capacity: %.1f kg",
            silo_settings["capacity_kg"]
        )

    except Exception:

        logger.exception(
            "Could not load silo settings"
        )


def save_silo_settings():

    path = Path(SILO_SETTINGS_FILE)

    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temporary = path.with_suffix(".tmp")

    with temporary.open("w") as f:

        json.dump(
            silo_settings,
            f,
            indent=2
        )

    temporary.replace(path)


def get_silo_settings():

    with silo_settings_lock:
        return dict(silo_settings)


# ============================================================
# CONTROLLER
# ============================================================

burner = None

state_lock = threading.Lock()

live_data = {
    "connected": False,
    "timestamp": None,
    "values": {},
    "errors": {}
}


# ============================================================
# PARAMETERS
# ============================================================

PARAMETERS = [
    "alarm",
    "autocalculation",
    "blower_cleaning",
    "blower_corr_high",
    "blower_corr_low",
    "blower_corr_mid",
    "blower_high",
    "blower_low",
    "blower_mid",
    "blower_off_time",

    "boiler_return_temp",
    "boiler_temp",
    "boiler_temp_diff_down",
    "boiler_temp_diff_up",
    "boiler_temp_min",
    "boiler_temp_set",

    "chute_temp",
    "chute_temp_max",

    "cleaning_interval",
    "cleaning_time",

    "comp_clean_blower",
    "comp_clean_interval",
    "comp_clean_time",
    "comp_clean_wait",

    "el_time",
    "el_time_perm",

    "feed_per_minute",
    "feeder_capacity",
    "feeder_capacity_max",
    "feeder_capacity_min",
    "feeder_high",
    "feeder_low",
    "feeder_time",

    "flow",

    "hotwater_temp",
    "hotwater_temp_diff",
    "hotwater_temp_set",

    "ignition_count",
    "ignition_time",

    "indoor_temp",

    "language",
    "light",
    "light_required",

    "magazine_content",

    "max_power",
    "min_power",
    "mode",
    "model",
    "motor_time",
    "motor_time_perm",

    "outside_temp",

    "oxygen",
    "oxygen_corr_10",
    "oxygen_corr_100",
    "oxygen_corr_50",
    "oxygen_corr_interval",
    "oxygen_desired",
    "oxygen_gain",
    "oxygen_high",
    "oxygen_low",
    "oxygen_mid",
    "oxygen_regulation",
    "oxygen_regulation_D",
    "oxygen_regulation_P",

    "power",
    "power_kW",

    "regulator_D",
    "regulator_I",
    "regulator_P",

    "smoke_temp",

    "time_minutes",

    "timer_heating_period",
    "timer_heating_start_1",
    "timer_heating_start_2",
    "timer_heating_start_3",
    "timer_heating_start_4",

    "timer_hotwater_period",
    "timer_hotwater_start_1",
    "timer_hotwater_start_2",
    "timer_hotwater_start_3",

    "version",
]


# ============================================================
# CONTROLLER CONNECTION
# ============================================================

def connect_controller():

    global burner

    logger.info(
        "Connecting to pellet controller: %s",
        DEVICE
    )

    try:

        burner = Protocol(
            DEVICE,
            "auto"
        )

        if burner.dummyDevice:

            logger.error(
                "Controller returned dummy device"
            )

            burner = None
            return False

        logger.info(
            "Controller connected "
            "(checksum=%s crlf=%s)",
            burner.checksum,
            burner.frame_term_crlf
        )

        return True

    except Exception:

        logger.exception(
            "Controller connection failed"
        )

        burner = None

        return False


# ============================================================
# READ CONTROLLER
# ============================================================

def read_controller():

    global burner

    if burner is None:

        if not connect_controller():

            return

    values = {}
    errors = {}

    for parameter in PARAMETERS:

        try:

            value = burner.getItem(parameter)

            values[parameter] = value

        except Exception as exc:

            errors[parameter] = str(exc)

    timestamp = datetime.now(
        timezone.utc
    ).isoformat()

    with state_lock:

        live_data["connected"] = True
        live_data["timestamp"] = timestamp
        live_data["values"] = values
        live_data["errors"] = errors

    return values


# ============================================================
# LIVE COLLECTOR
# ============================================================

def collector_loop():

    while True:

        try:

            values = read_controller()

            if values:

                logger.info(
                    "Live update: %d/%d parameters",
                    len(values),
                    len(PARAMETERS)
                )

        except Exception:

            logger.exception(
                "Collector error"
            )

            with state_lock:
                live_data["connected"] = False

            time.sleep(10)

        time.sleep(LIVE_INTERVAL)


# ============================================================
# MQTT
# ============================================================

mqtt_client = None
mqtt_connected = False


def mqtt_on_connect(client, userdata, flags, reason_code, properties):
    global mqtt_connected

    if reason_code.is_failure:
        mqtt_connected = False
        logger.error("MQTT connection failed: %s", reason_code)
    else:
        mqtt_connected = True
        logger.info(
            "MQTT connected to %s:%s",
            MQTT_BROKER,
            MQTT_PORT
        )


def mqtt_on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    global mqtt_connected

    mqtt_connected = False

    logger.warning(
        "MQTT disconnected: %s",
        reason_code
    )


def mqtt_setup():

    global mqtt_client

    try:

        mqtt_client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            protocol=mqtt.MQTTv5
        )

        mqtt_client.username_pw_set(
            MQTT_USERNAME,
            MQTT_PASSWORD
        )

        mqtt_client.on_connect = mqtt_on_connect
        mqtt_client.on_disconnect = mqtt_on_disconnect

        mqtt_client.connect(
            MQTT_BROKER,
            MQTT_PORT,
            60
        )

        mqtt_client.loop_start()

        logger.info(
            "MQTT client started"
        )

    except Exception:

        mqtt_client = None

        logger.exception(
            "MQTT setup failed"
        )


def mqtt_publish_loop():

    global mqtt_client

    while True:

        try:

            with state_lock:

                connected = live_data["connected"]
                timestamp = live_data["timestamp"]
                values = dict(
                    live_data["values"]
                )

            if connected and values and mqtt_client is not None:

                # Complete dataset
                payload = {
                    "connected": connected,
                    "timestamp": timestamp,
                    "values": values
                }

                mqtt_client.publish(
                    f"{MQTT_TOPIC}/live",
                    json.dumps(payload),
                    qos=0,
                    retain=True
                )

                # Individual values
                for name, value in values.items():

                    mqtt_client.publish(
                        f"{MQTT_TOPIC}/{name}",
                        str(value),
                        qos=0,
                        retain=True
                    )

                logger.debug(
                    "MQTT live data published: %d values",
                    len(values)
                )

        except Exception:

            logger.exception(
                "MQTT publish error"
            )

        time.sleep(MQTT_INTERVAL)


# ============================================================
# HISTORY WRITER
# ============================================================

def history_loop():

    while True:

        try:

            with state_lock:

                connected = live_data["connected"]
                values = dict(
                    live_data["values"]
                )

            if connected and values:

                db.insert_measurements(
                    values
                )

                logger.debug(
                    "History sample stored"
                )

        except Exception:

            logger.exception(
                "History writer error"
            )

        time.sleep(HISTORY_INTERVAL)


# ============================================================
# API: ROOT
# ============================================================

@app.get("/")
def root():
    return FileResponse(str(Path(__file__).resolve().parent / "web" / "index.html"))


# ============================================================
# API: STATUS
# ============================================================

@app.get("/api/v1/status")
def status():

    with state_lock:

        return {
            "application": "Woody Monitor",
            "connected": live_data["connected"],
            "timestamp": live_data["timestamp"],
            "parameter_count": len(
                live_data["values"]
            ),
            "errors": len(
                live_data["errors"]
            ),
            "history_rows": db.count()
        }


# ============================================================
# API: FEEDER CALIBRATION
# ============================================================

@app.get("/api/v1/settings/feeder")
def get_feeder_settings():

    calibration = get_feeder_calibration()

    grams = calibration["grams"]
    seconds = calibration["seconds"]

    return {
        "grams": grams,
        "seconds": seconds,
        "grams_per_second": grams / seconds,
        "kg_per_hour": grams / seconds * 3.6
    }


@app.post("/api/v1/settings/feeder")
def set_feeder_settings(
    grams: float = Query(..., gt=1, le=10000)
):

    with feeder_calibration_lock:

        feeder_calibration["grams"] = grams
        feeder_calibration["seconds"] = DEFAULT_FEEDER_SECONDS

        save_feeder_calibration()

    logger.info(
        "Feeder calibration changed: %.1f g / %.0f s",
        grams,
        DEFAULT_FEEDER_SECONDS
    )

    return get_feeder_settings()


# ============================================================
# API: SILO SETTINGS
# ============================================================

@app.get("/api/v1/settings/silo")
def get_silo_settings_api():

    return get_silo_settings()


@app.post("/api/v1/settings/silo")
def set_silo_settings(
    capacity_kg: float = Query(..., gt=1, le=5000)
):

    with silo_settings_lock:

        silo_settings["capacity_kg"] = capacity_kg

        save_silo_settings()

    logger.info(
        "Silo capacity changed: %.1f kg",
        capacity_kg
    )

    return get_silo_settings()


# ============================================================
# API: LIVE
# ============================================================

@app.get("/api/v1/live")
def live():

    with state_lock:

        return {
            "connected": live_data["connected"],
            "timestamp": live_data["timestamp"],
            "values": dict(
                live_data["values"]
            ),
            "errors": dict(
                live_data["errors"]
            )
        }


# ============================================================
# API: PARAMETERS
# ============================================================

@app.get("/api/v1/parameters")
def parameters():

    with state_lock:

        values = dict(
            live_data["values"]
        )

    return [
        {
            "id": name,
            "name": name.replace(
                "_",
                " "
            ).title(),
            "value": values.get(name)
        }
        for name in PARAMETERS
    ]


# ============================================================
# API: RAW
# ============================================================

@app.get("/api/v1/raw")
def raw():

    with state_lock:

        return dict(
            live_data["values"]
        )


# ============================================================
# API: PELLET PRICE HISTORY
# ============================================================

@app.get("/api/v1/settings/pellet-price")
def get_pellet_price():

    with db.lock:

        conn = db._connect()

        try:

            row = conn.execute("""
                SELECT
                    effective_at,
                    price_per_kg
                FROM pellet_prices
                ORDER BY effective_at DESC
                LIMIT 1
            """).fetchone()

            if row is None:

                return {
                    "price_per_kg": None,
                    "effective_at": None
                }

            return {
                "price_per_kg": row["price_per_kg"],
                "effective_at": row["effective_at"]
            }

        finally:

            conn.close()


@app.get("/api/v1/settings/pellet-prices")
def get_pellet_prices():

    with db.lock:

        conn = db._connect()

        try:

            rows = conn.execute("""
                SELECT
                    id,
                    effective_at,
                    price_per_kg
                FROM pellet_prices
                ORDER BY effective_at ASC
            """).fetchall()

            return {
                "prices": [
                    dict(row)
                    for row in rows
                ]
            }

        finally:

            conn.close()


@app.post("/api/v1/settings/pellet-price")
def set_pellet_price(
    price: float = Query(..., ge=0, le=100)
):

    effective_at = datetime.now(
        timezone.utc
    ).isoformat()

    with db.lock:

        conn = db._connect()

        try:

            conn.execute("""
                INSERT INTO pellet_prices
                (
                    effective_at,
                    price_per_kg
                )
                VALUES (?, ?)
            """, [
                effective_at,
                price
            ])

            conn.commit()

        finally:

            conn.close()

    return {
        "price_per_kg": price,
        "effective_at": effective_at
    }




# ============================================================
# API: PELLET CONSUMPTION
# ============================================================

@app.get("/api/v1/consumption/pellets")
def pellet_consumption(
    hours: float = Query(
        24,
        gt=0,
        le=8760
    ),
    start: str | None = Query(None),
    end: str | None = Query(None)
):

    # ---------------------------------------------------------
    # Determine requested period
    # ---------------------------------------------------------

    if start and end:

        start_dt = datetime.fromisoformat(
            start.replace("Z", "+00:00")
        )

        end_dt = datetime.fromisoformat(
            end.replace("Z", "+00:00")
        )

    else:

        end_dt = datetime.now(timezone.utc)

        start_dt = (
            end_dt -
            timedelta(hours=hours)
        )

    if start_dt >= end_dt:

        raise HTTPException(
            status_code=400,
            detail="start must be before end"
        )

    # ---------------------------------------------------------
    # Feeder calibration
    # ---------------------------------------------------------

    calibration = get_feeder_calibration()

    grams = float(calibration["grams"])
    seconds = float(calibration["seconds"])

    if grams <= 0 or seconds <= 0:

        raise HTTPException(
            status_code=500,
            detail="Invalid feeder calibration"
        )

    kg_per_second = (
        grams /
        seconds /
        1000.0
    )

    start_iso = start_dt.isoformat()
    end_iso = end_dt.isoformat()

    # ---------------------------------------------------------
    # Read feeder_time.
    #
    # The previous value is needed to calculate the first
    # interval correctly.
    #
    # IMPORTANT:
    # feeder_time is a cumulative counter containing actual
    # auger runtime in seconds.
    # ---------------------------------------------------------

    with db.lock:

        conn = db._connect()

        try:

            previous = conn.execute(
                """
                SELECT timestamp, value
                FROM measurements
                WHERE parameter = 'feeder_time'
                  AND timestamp < ?
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                (start_iso,)
            ).fetchone()

            rows = conn.execute(
                """
                SELECT timestamp, value
                FROM measurements
                WHERE parameter = 'feeder_time'
                  AND timestamp >= ?
                  AND timestamp <= ?
                ORDER BY timestamp ASC
                """,
                (start_iso, end_iso)
            ).fetchall()

            # -------------------------------------------------
            # Pellet prices.
            #
            # Only prices relevant to the requested period are
            # required. Include the latest price before start.
            # -------------------------------------------------

            price_rows = conn.execute(
                """
                SELECT effective_at, price_per_kg
                FROM pellet_prices
                WHERE effective_at <= ?
                ORDER BY effective_at ASC
                """,
                (end_iso,)
            ).fetchall()

        finally:

            conn.close()

    # ---------------------------------------------------------
    # No feeder data
    # ---------------------------------------------------------

    if not rows:

        return {
            "start": start_iso,
            "end": end_iso,
            "count": 0,
            "chart_count": 0,
            "total_kg": 0.0,
            "average_kg_per_day": 0.0,
            "total_cost": 0.0,
            "kg_per_second": kg_per_second,
            "data": []
        }

    # ---------------------------------------------------------
    # Build points.
    # ---------------------------------------------------------

    points = []

    if previous is not None:

        points.append({
            "timestamp": previous["timestamp"],
            "value": float(previous["value"])
        })

    points.extend(
        {
            "timestamp": row["timestamp"],
            "value": float(row["value"])
        }
        for row in rows
    )

    # ---------------------------------------------------------
    # FAST PATH:
    #
    # If feeder_time has not changed anywhere in the requested
    # period, there is absolutely no pellet consumption.
    #
    # This is currently the situation because the burner has
    # not been running.
    # ---------------------------------------------------------

    values = [
        point["value"]
        for point in points
    ]

    if values and all(
        value == values[0]
        for value in values
    ):

        return {
            "start": start_iso,
            "end": end_iso,
            "count": max(len(rows) - 1, 0),
            "chart_count": 1,
            "total_kg": 0.0,
            "average_kg_per_day": 0.0,
            "total_cost": 0.0,
            "kg_per_second": kg_per_second,
            "data": [
                {
                    "timestamp": rows[-1]["timestamp"],
                    "kg": 0.0,
                    "price": None,
                    "cost": 0.0
                }
            ]
        }

    # ---------------------------------------------------------
    # Build pellet price history.
    # ---------------------------------------------------------

    prices = []

    for row in price_rows:

        try:

            effective = datetime.fromisoformat(
                row["effective_at"].replace(
                    "Z",
                    "+00:00"
                )
            )

            price = float(
                row["price_per_kg"]
            )

            prices.append(
                (effective, price)
            )

        except Exception:

            continue

    def price_at(timestamp):

        active = None

        for effective, price in prices:

            if effective <= timestamp:
                active = price
            else:
                break

        return active

    # ---------------------------------------------------------
    # Calculate actual consumption.
    # ---------------------------------------------------------

    intervals = []

    total_kg = 0.0
    total_cost = 0.0

    previous_value = points[0]["value"]

    for point in points[1:]:

        current_value = point["value"]

        delta = current_value - previous_value

        # Counter reset.
        if delta < 0:
            delta = 0.0

        kg = delta * kg_per_second

        timestamp = datetime.fromisoformat(
            point["timestamp"].replace(
                "Z",
                "+00:00"
            )
        )

        price = price_at(timestamp)

        cost = (
            kg * price
            if price is not None
            else None
        )

        total_kg += kg

        if cost is not None:
            total_cost += cost

        intervals.append({
            "timestamp": point["timestamp"],
            "kg": kg,
            "price": price,
            "cost": cost
        })

        previous_value = current_value

    # ---------------------------------------------------------
    # Average consumption per day.
    # ---------------------------------------------------------

    period_seconds = (
        end_dt -
        start_dt
    ).total_seconds()

    period_days = max(
        period_seconds / 86400.0,
        1 / 1440.0
    )

    average_kg_per_day = (
        total_kg /
        period_days
    )

    # ---------------------------------------------------------
    # Downsample chart data.
    #
    # The total is calculated from ALL measurements.
    # Only the visual chart is reduced.
    # ---------------------------------------------------------

    MAX_POINTS = 1000

    chart_data = intervals

    if len(intervals) > MAX_POINTS:

        bucket_size = (
            len(intervals) +
            MAX_POINTS -
            1
        ) // MAX_POINTS

        chart_data = []

        for i in range(
            0,
            len(intervals),
            bucket_size
        ):

            bucket = intervals[
                i:i + bucket_size
            ]

            bucket_kg = sum(
                item["kg"]
                for item in bucket
            )

            bucket_cost = sum(
                item["cost"]
                for item in bucket
                if item["cost"] is not None
            )

            weighted_price = 0.0
            weighted_kg = 0.0

            for item in bucket:

                if (
                    item["price"] is not None
                    and item["kg"] > 0
                ):

                    weighted_price += (
                        item["kg"] *
                        item["price"]
                    )

                    weighted_kg += item["kg"]

            chart_data.append({
                "timestamp": bucket[-1]["timestamp"],
                "kg": bucket_kg,
                "price": (
                    weighted_price / weighted_kg
                    if weighted_kg > 0
                    else None
                ),
                "cost": bucket_cost
            })

    return {
        "start": start_iso,
        "end": end_iso,
        "count": len(intervals),
        "chart_count": len(chart_data),
        "total_kg": total_kg,
        "average_kg_per_day": average_kg_per_day,
        "total_cost": total_cost,
        "kg_per_second": kg_per_second,
        "data": chart_data
    }



# ============================================================
# API: HISTORY
# ============================================================

@app.get("/api/v1/history")
def history(
    parameter: str = Query(...),
    hours: float = Query(
        24,
        gt=0,
        le=8760
    ),
    start: str | None = Query(None),
    end: str | None = Query(None),
    bucket_seconds: int | None = Query(
        None,
        gt=0
    )
):

    if start and end:

        start_dt = datetime.fromisoformat(
            start.replace("Z", "+00:00")
        )

        end_dt = datetime.fromisoformat(
            end.replace("Z", "+00:00")
        )

    else:

        end_dt = datetime.now(
            timezone.utc
        )

        start_dt = end_dt - timedelta(
            hours=hours
        )

    if start_dt >= end_dt:
        raise HTTPException(
            status_code=400,
            detail="start must be before end"
        )

    rows = db.get_history(
        [parameter],
        start_dt.isoformat(),
        end_dt.isoformat(),
        bucket_seconds=bucket_seconds
    )

    return {
        "parameter": parameter,
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "count": len(rows),
        "data": rows
    }


# ============================================================
# API: MULTI-PARAMETER HISTORY
# ============================================================

@app.get("/api/v1/history/multi")
def history_multi(
    parameters: str = Query(...),
    hours: float = Query(
        24,
        gt=0,
        le=8760
    ),
    start: str | None = Query(None),
    end: str | None = Query(None)
):

    parameter_list = [
        p.strip()
        for p in parameters.split(",")
        if p.strip()
    ]

    if not parameter_list:
        raise HTTPException(
            status_code=400,
            detail="No parameters specified"
        )

    if start and end:

        start_dt = datetime.fromisoformat(
            start.replace("Z", "+00:00")
        )

        end_dt = datetime.fromisoformat(
            end.replace("Z", "+00:00")
        )

    else:

        end_dt = datetime.now(
            timezone.utc
        )

        start_dt = end_dt - timedelta(
            hours=hours
        )

    if start_dt >= end_dt:
        raise HTTPException(
            status_code=400,
            detail="start must be before end"
        )

    # ---------------------------------------------------------
    # Dynamic downsampling
    # ---------------------------------------------------------
    #
    # Target roughly 1000 points per parameter.
    #
    # Short periods retain much more detail while longer periods
    # are aggressively reduced so the browser stays responsive.
    #
    duration_seconds = (
        end_dt - start_dt
    ).total_seconds()

    TARGET_POINTS = 1000

    bucket_seconds = max(
        1,
        int(
            duration_seconds /
            TARGET_POINTS
        )
    )

    # Use sensible bucket sizes rather than arbitrary values.
    bucket_sizes = [
        1,
        2,
        5,
        10,
        15,
        30,
        60,
        120,
        300,
        600,
        900,
        1800,
        3600,
        7200,
        10800,
        21600,
        43200,
        86400
    ]

    bucket_seconds = next(
        (
            size
            for size in bucket_sizes
            if size >= bucket_seconds
        ),
        bucket_sizes[-1]
    )

    rows = db.get_history(
        parameter_list,
        start_dt.isoformat(),
        end_dt.isoformat(),
        bucket_seconds=bucket_seconds
    )

    return {
        "parameters": parameter_list,
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "count": len(rows),
        "bucket_seconds": bucket_seconds,
        "data": rows
    }


# ============================================================
# API: HISTORY PARAMETERS
# ============================================================

@app.get("/api/v1/history/parameters")
def history_parameters():

    return {
        "parameters": db.get_parameter_names()
    }


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
def startup():

    load_feeder_calibration()

    watchdog = threading.Thread(
        target=systemd_watchdog_loop,
        daemon=True
    )

    watchdog.start()

    # Tell systemd that Woody Monitor has completed startup.
    notify_socket = os.environ.get("NOTIFY_SOCKET")

    if notify_socket:
        try:
            if notify_socket.startswith("@"):
                notify_socket = "\0" + notify_socket[1:]

            sock = socket.socket(
                socket.AF_UNIX,
                socket.SOCK_DGRAM
            )

            sock.connect(notify_socket)
            sock.sendall(b"READY=1")
            sock.close()

            logger.info("systemd notified: READY=1")

        except Exception:
            logger.exception(
                "systemd READY notification failed"
            )

    collector = threading.Thread(
        target=collector_loop,
        daemon=True
    )

    history = threading.Thread(
        target=history_loop,
        daemon=True
    )

    collector.start()
    history.start()

    mqtt_setup()

    mqtt_thread = threading.Thread(
        target=mqtt_publish_loop,
        daemon=True
    )

    mqtt_thread.start()

    logger.info(
        "Woody Monitor collector started"
    )

    logger.info(
        "Woody Monitor history writer started"
    )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    logger.info(
        "Starting Woody Monitor API "
        "on %s:%s",
        HOST,
        PORT
    )

    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        log_level="info"
    )
