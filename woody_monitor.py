#!/usr/bin/env python3

import sys
import time
import threading
import logging
import os
import socket
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, available_timezones
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

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

DEVICE = os.environ.get(
    "WOODY_SERIAL_DEVICE",
    "/dev/ttyUSB0"
)

HOST = os.environ.get("WOODY_HOST", "0.0.0.0")
PORT = int(os.environ.get("WOODY_PORT", "8080"))

LIVE_INTERVAL = 5
HISTORY_INTERVAL = 60

HISTORY_PARAMETERS = {
    "alarm",
    "boiler_temp",
    "boiler_return_temp",
    "chute_temp",
    "hotwater_temp",
    "outside_temp",
    "indoor_temp",
    "smoke_temp",
    "oxygen",
    "light",
    "flow",
    "power",
    "power_kW",
    "feeder_time",
}

# MQTT
MQTT_BROKER = os.environ.get("WOODY_MQTT_BROKER", "localhost")
MQTT_PORT = int(os.environ.get("WOODY_MQTT_PORT", "1883"))
MQTT_USERNAME = os.environ.get("WOODY_MQTT_USERNAME", "")
MQTT_PASSWORD = os.environ.get("WOODY_MQTT_PASSWORD", "")
MQTT_TOPIC = os.environ.get("WOODY_MQTT_TOPIC", "woodymonitor")
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

FEEDER_SETTINGS_FILE = str(BASE_DIR / "data" / "feeder_settings.json")

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

SILO_SETTINGS_FILE = str(BASE_DIR / "data" / "silo_settings.json")

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

SILO_SETTINGS_FILE = str(BASE_DIR / "data" / "silo_settings.json")

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
# TIMEZONE SETTINGS
# ============================================================

TIMEZONE_SETTINGS_FILE = str(BASE_DIR / "data" / "timezone_settings.json")

DEFAULT_TIMEZONE = "Europe/Copenhagen"

timezone_settings_lock = threading.Lock()

timezone_settings = {
    "timezone": DEFAULT_TIMEZONE
}


def load_timezone_settings():

    global timezone_settings

    try:

        path = Path(TIMEZONE_SETTINGS_FILE)

        if path.exists():

            with path.open("r") as f:
                data = json.load(f)

            selected = str(
                data.get(
                    "timezone",
                    DEFAULT_TIMEZONE
                )
            ).strip()

            if selected in available_timezones():

                timezone_settings["timezone"] = selected

            else:

                logger.warning(
                    "Invalid timezone '%s', using %s",
                    selected,
                    DEFAULT_TIMEZONE
                )

        logger.info(
            "Loaded timezone: %s",
            timezone_settings["timezone"]
        )

    except Exception:

        logger.exception(
            "Could not load timezone settings"
        )


def save_timezone_settings():

    path = Path(TIMEZONE_SETTINGS_FILE)

    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temporary = path.with_suffix(".tmp")

    with temporary.open("w") as f:

        json.dump(
            timezone_settings,
            f,
            indent=2
        )

    temporary.replace(path)


def get_timezone_name():

    with timezone_settings_lock:
        return timezone_settings["timezone"]


def get_timezone():

    return ZoneInfo(
        get_timezone_name()
    )


def local_now():

    return datetime.now(
        timezone.utc
    ).astimezone(
        get_timezone()
    )


def utc_now():

    return datetime.now(
        timezone.utc
    )


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
# HOURLY PELLET CONSUMPTION
# ============================================================

def calculate_pellet_hour(
    hour_start,
    hour_end
):

    calibration = get_feeder_calibration()

    grams = float(calibration["grams"])
    seconds = float(calibration["seconds"])

    if grams <= 0 or seconds <= 0:
        raise ValueError(
            "Invalid feeder calibration"
        )

    kg_per_second = (
        grams /
        seconds /
        1000.0
    )

    start_iso = hour_start.isoformat()
    end_iso = hour_end.isoformat()

    # We need the latest feeder_time value at/before
    # the beginning of the hour plus all samples during
    # the hour. Positive counter differences are summed.
    #
    # This also handles a feeder_time counter reset:
    # negative differences contribute zero instead of
    # producing negative pellet consumption.

    with db.lock:

        conn = db._connect()

        try:

            previous = conn.execute(
                """
                SELECT timestamp, value
                FROM measurements
                WHERE parameter = 'feeder_time'
                  AND timestamp <= ?
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
                  AND timestamp > ?
                  AND timestamp <= ?
                ORDER BY timestamp ASC
                """,
                (
                    start_iso,
                    end_iso
                )
            ).fetchall()

        finally:

            conn.close()

    if previous is None or not rows:
        return False

    previous_value = float(
        previous["value"]
    )

    feeder_seconds = 0.0

    for row in rows:

        current_value = float(
            row["value"]
        )

        delta = (
            current_value -
            previous_value
        )

        if delta > 0:
            feeder_seconds += delta

        previous_value = current_value

    kg = (
        feeder_seconds *
        kg_per_second
    )

    db.upsert_pellet_hour(
        start_iso,
        feeder_seconds,
        kg
    )

    logger.info(
        "Pellet hour stored: %s -> %.3f kg (%.0f feeder sec)",
        start_iso,
        kg,
        feeder_seconds
    )

    return True


def pellet_hourly_loop():

    last_completed_hour = None

    while True:

        try:

            # Store only completed real elapsed hours.
            # UTC avoids DST ambiguity. The frontend can
            # convert timestamps to the configured local zone.

            hour_end = datetime.now(
                timezone.utc
            ).replace(
                minute=0,
                second=0,
                microsecond=0
            )

            hour_start = (
                hour_end -
                timedelta(hours=1)
            )

            hour_key = (
                hour_start.isoformat()
            )

            if hour_key != last_completed_hour:

                if calculate_pellet_hour(
                    hour_start,
                    hour_end
                ):
                    last_completed_hour = (
                        hour_key
                    )

        except Exception:

            logger.exception(
                "Pellet hourly writer error"
            )

        time.sleep(60)


# ============================================================
# FEEDER HISTORY RETENTION
# ============================================================

def feeder_retention_loop():

    while True:

        try:

            now = datetime.now(timezone.utc)

            feeder_cutoff = (
                now - timedelta(hours=48)
            ).isoformat()

            feeder_deleted = db.cleanup_feeder_history(
                feeder_cutoff
            )

            logger.info(
                "Feeder history cleanup: %d rows deleted",
                feeder_deleted
            )

            history_cutoff = (
                now - timedelta(days=90)
            ).isoformat()

            history_deleted = db.cleanup_measurement_history(
                history_cutoff
            )

            logger.info(
                "90-day history cleanup: %d rows deleted",
                history_deleted
            )

        except Exception:

            logger.exception(
                "Feeder history cleanup error"
            )

        time.sleep(86400)


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

                history_values = {
                    parameter: value
                    for parameter, value in values.items()
                    if parameter in HISTORY_PARAMETERS
                }

                if history_values:
                    db.insert_measurements(
                        history_values
                    )

                    logger.debug(
                        "History sample stored: %d parameters",
                        len(history_values)
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
    return FileResponse(str(BASE_DIR / "web" / "index.html"))


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
# API: TIMEZONE SETTINGS
# ============================================================

@app.get("/api/v1/settings/timezone")
def get_timezone_settings():

    name = get_timezone_name()
    zone = ZoneInfo(name)
    now = datetime.now(
        timezone.utc
    ).astimezone(zone)

    return {
        "timezone": name,
        "local_time": now.isoformat(),
        "utc_offset": now.strftime("%z"),
        "utc_offset_display": now.strftime("%z")[:3] + ":" + now.strftime("%z")[3:],
        "dst": bool(now.dst()),
        "automatic_dst": True
    }


@app.post("/api/v1/settings/timezone")
def set_timezone_settings(
    timezone_name: str = Query(...)
):

    timezone_name = timezone_name.strip()

    if timezone_name not in available_timezones():

        raise HTTPException(
            status_code=400,
            detail=f"Invalid timezone: {timezone_name}"
        )

    with timezone_settings_lock:

        timezone_settings["timezone"] = timezone_name

        save_timezone_settings()

    logger.info(
        "Timezone changed to: %s",
        timezone_name
    )

    return get_timezone_settings()


# ============================================================
# API: TIMEZONE LIST
# ============================================================

@app.get("/api/v1/settings/timezones")
def get_timezone_list():

    zones = sorted(
        zone
        for zone in available_timezones()
        if "/" in zone
        and not zone.startswith("Etc/")
        and not zone.startswith("SystemV/")
    )

    return {
        "count": len(zones),
        "timezones": zones
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
    hours: float = Query(24, gt=0, le=8760),
    start: str | None = Query(None),
    end: str | None = Query(None)
):
    if start and end:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    else:
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(hours=hours)

    if start_dt >= end_dt:
        raise HTTPException(400, "start must be before end")

    calibration = get_feeder_calibration()
    kg_per_second = (
        float(calibration["grams"]) /
        float(calibration["seconds"]) /
        1000.0
    )

    def hour_floor(dt):
        return dt.replace(minute=0, second=0, microsecond=0)

    def raw_segment(seg_start, seg_end):
        with db.lock:
            conn = db._connect()
            try:
                previous = conn.execute(
                    """
                    SELECT value FROM measurements
                    WHERE parameter='feeder_time'
                      AND timestamp <= ?
                    ORDER BY timestamp DESC LIMIT 1
                    """,
                    (seg_start.isoformat(),)
                ).fetchone()

                rows = conn.execute(
                    """
                    SELECT timestamp,value FROM measurements
                    WHERE parameter='feeder_time'
                      AND timestamp > ?
                      AND timestamp <= ?
                    ORDER BY timestamp
                    """,
                    (seg_start.isoformat(), seg_end.isoformat())
                ).fetchall()
            finally:
                conn.close()

        if previous is None or not rows:
            return None

        prev = float(previous["value"])
        seconds = 0.0

        for row in rows:
            current = float(row["value"])
            delta = current - prev
            if delta > 0:
                seconds += delta
            prev = current

        return seconds * kg_per_second

    with db.lock:
        conn = db._connect()
        try:
            price_rows = conn.execute(
                """
                SELECT effective_at,price_per_kg
                FROM pellet_prices
                WHERE effective_at <= ?
                ORDER BY effective_at
                """,
                (end_dt.isoformat(),)
            ).fetchall()
        finally:
            conn.close()

    prices = []
    for row in price_rows:
        try:
            prices.append((
                datetime.fromisoformat(
                    row["effective_at"].replace("Z", "+00:00")
                ),
                float(row["price_per_kg"])
            ))
        except Exception:
            pass

    def price_at(dt):
        active = None
        for effective, price in prices:
            if effective <= dt:
                active = price
            else:
                break
        return active

    intervals = []

    def add_interval(timestamp, kg):
        price = price_at(timestamp)
        intervals.append({
            "timestamp": timestamp.isoformat(),
            "kg": kg,
            "price": price,
            "cost": kg * price if price is not None else None
        })

    start_hour = hour_floor(start_dt)
    end_hour = hour_floor(end_dt)

    # Period entirely inside one hour
    if start_hour == end_hour:
        kg = raw_segment(start_dt, end_dt)
        if kg is not None:
            add_interval(end_dt, kg)

    else:
        # Partial first hour
        full_start = start_dt
        if start_dt != start_hour:
            first_end = start_hour + timedelta(hours=1)
            kg = raw_segment(start_dt, first_end)
            if kg is not None:
                add_interval(first_end, kg)
            full_start = first_end

        # Completed full hours
        full_end = end_hour
        for row in db.get_pellet_hours(
            full_start.isoformat(),
            full_end.isoformat()
        ):
            timestamp = (
                datetime.fromisoformat(
                    row["hour_start"].replace("Z", "+00:00")
                ) + timedelta(hours=1)
            )
            add_interval(timestamp, float(row["kg"]))

        # Partial last hour
        if end_dt != end_hour:
            kg = raw_segment(end_hour, end_dt)
            if kg is not None:
                add_interval(end_dt, kg)

    total_kg = sum(x["kg"] for x in intervals)
    total_cost = sum(
        x["cost"] for x in intervals
        if x["cost"] is not None
    )

    period_days = max(
        (end_dt - start_dt).total_seconds() / 86400.0,
        1 / 1440.0
    )

    return {
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "count": len(intervals),
        "chart_count": len(intervals),
        "total_kg": total_kg,
        "average_kg_per_day": total_kg / period_days,
        "total_cost": total_cost,
        "kg_per_second": kg_per_second,
        "data": intervals
    }


# ============================================================
# API: HOURLY PELLET CONSUMPTION
# ============================================================

@app.get("/api/v1/consumption/pellets/hourly")
def pellet_consumption_hourly(
    hours: float = Query(24, gt=0, le=8760),
    start: str | None = Query(None),
    end: str | None = Query(None)
):

    if start and end:
        start_dt = datetime.fromisoformat(
            start.replace("Z", "+00:00")
        )
        end_dt = datetime.fromisoformat(
            end.replace("Z", "+00:00")
        )
    else:
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(hours=hours)

    rows = db.get_pellet_hours(
        start_dt.isoformat(),
        end_dt.isoformat()
    )

    return {
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "count": len(rows),
        "total_kg": sum(float(r["kg"]) for r in rows),
        "data": rows
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

    load_timezone_settings()
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

    pellet_hourly = threading.Thread(
        target=pellet_hourly_loop,
        daemon=True
    )

    feeder_retention = threading.Thread(
        target=feeder_retention_loop,
        daemon=True
    )

    collector.start()
    history.start()
    pellet_hourly.start()
    feeder_retention.start()

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
