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

from fastapi import FastAPI, Query, HTTPException
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

DEVICE = os.environ.get("WOODY_SERIAL_DEVICE", "/dev/ttyUSB0")

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

db.add_activity(
    "SYSTEM",
    "Woody Monitor started",
    "Woody Monitor service initialized"
)



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
# CONTROLLER CHANGE LOGGING
# ============================================================

# Only meaningful state changes are logged here.
# Fast-changing values such as temperatures, power and feeder_time
# are deliberately excluded to keep the activity log useful.

controller_log_state = {
    "initialized": False,
    "connected": False,
    "mode": None,
    "alarm": None,
}


def log_controller_changes(
    connected,
    values
):

    global controller_log_state

    previous_initialized = controller_log_state["initialized"]
    previous_connected = controller_log_state["connected"]
    previous_mode = controller_log_state["mode"]
    previous_alarm = controller_log_state["alarm"]

    current_mode = values.get("mode")
    current_alarm = values.get("alarm")

    # --------------------------------------------------------
    # First successful controller read
    # --------------------------------------------------------

    if not previous_initialized:

        controller_log_state["initialized"] = True
        controller_log_state["connected"] = connected
        controller_log_state["mode"] = current_mode
        controller_log_state["alarm"] = current_alarm

        db.add_activity(
            "CONTROLLER",
            "Controller connected",
            f"Initial state: mode={current_mode}, alarm={current_alarm}",
            response="OK"
        )

        return

    # --------------------------------------------------------
    # Connection state
    # --------------------------------------------------------

    if connected != previous_connected:

        if connected:

            db.add_activity(
                "CONTROLLER",
                "Controller connected",
                "Controller connection restored",
                response="OK"
            )

        else:

            db.add_activity(
                "CONTROLLER",
                "Controller disconnected",
                "Connection to pellet controller lost",
                response="ERROR"
            )

    # --------------------------------------------------------
    # Burner mode
    # --------------------------------------------------------

    if (
        current_mode is not None
        and previous_mode is not None
        and str(current_mode) != str(previous_mode)
    ):

        db.add_activity(
            "BURNER",
            "Mode changed",
            f"{previous_mode} → {current_mode}",
            response="OK"
        )

    # --------------------------------------------------------
    # Alarm
    # --------------------------------------------------------

    if (
        current_alarm is not None
        and previous_alarm is not None
        and str(current_alarm).lower()
            != str(previous_alarm).lower()
    ):

        old_alarm = str(previous_alarm)
        new_alarm = str(current_alarm)

        db.add_activity(
            "ALARM",
            "Alarm changed",
            f"{old_alarm} → {new_alarm}",
            response="OK"
        )

    # --------------------------------------------------------
    # Save current state
    # --------------------------------------------------------

    controller_log_state["connected"] = connected
    controller_log_state["mode"] = current_mode
    controller_log_state["alarm"] = current_alarm


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

    # Register meaningful controller state changes.
    log_controller_changes(
        True,
        values
    )

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

            # Register controller connection loss.
            log_controller_changes(
                False,
                {}
            )

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

        command_topic = f"{MQTT_TOPIC}/command/#"

        try:
            client.subscribe(
                command_topic,
                qos=0
            )

            logger.info(
                "MQTT command subscription active: %s",
                command_topic
            )

        except Exception:
            logger.exception(
                "MQTT command subscription failed"
            )

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


def mqtt_on_message(client, userdata, message):

    global burner

    topic = message.topic
    prefix = f"{MQTT_TOPIC}/command/"

    if not topic.startswith(prefix):
        return

    parameter = topic[len(prefix):].strip()

    if not parameter:
        logger.warning(
            "MQTT command ignored: empty parameter"
        )
        return

    try:
        value = message.payload.decode("utf-8").strip()
    except Exception:
        logger.warning(
            "MQTT command ignored: invalid UTF-8 payload"
        )
        return

    logger.info(
        "MQTT command received: %s = %s",
        parameter,
        value
    )

    # --------------------------------------------------------
    # Controller must be connected
    # --------------------------------------------------------

    if burner is None:

        db.add_activity(
            "MQTT",
            parameter,
            f"Command rejected: controller not connected",
            payload=value,
            response="Controller not connected"
        )

        return

    # --------------------------------------------------------
    # Only settings explicitly allowed by Woody Monitor
    # --------------------------------------------------------

    allowed = {
        item
        for names in CONTROLLER_SETTING_GROUPS.values()
        for item in names
    }

    if parameter not in allowed:

        db.add_activity(
            "MQTT",
            parameter,
            "Command rejected: unknown or read-only parameter",
            payload=value,
            response="Rejected"
        )

        logger.warning(
            "MQTT command rejected: %s",
            parameter
        )

        return

    controller_database = burner.getDataBase()
    dataparam = controller_database.get(parameter)

    if (
        dataparam is None
        or not hasattr(dataparam, "address")
        or not hasattr(dataparam, "frame")
    ):

        db.add_activity(
            "MQTT",
            parameter,
            "Command rejected: parameter is not writable",
            payload=value,
            response="Rejected"
        )

        return

    # --------------------------------------------------------
    # Read old value
    # --------------------------------------------------------

    old_value = None

    try:
        old_value = burner.getItem(parameter)
    except Exception as exc:
        logger.warning(
            "Could not read old value for MQTT command %s: %s",
            parameter,
            exc
        )

    # --------------------------------------------------------
    # Send through the same PellMon setItem() path
    # --------------------------------------------------------

    try:

        response = burner.setItem(
            parameter,
            value
        )

    except Exception as exc:

        logger.exception(
            "MQTT controller write failed: %s",
            parameter
        )

        db.add_activity(
            "MQTT",
            parameter,
            f"Old: {old_value} -> New: {value}",
            payload=value,
            response=str(exc)
        )

        return

    if str(response) != "OK":

        db.add_activity(
            "MQTT",
            parameter,
            f"Old: {old_value} -> New: {value}",
            payload=value,
            response=str(response)
        )

        logger.warning(
            "MQTT controller write rejected: %s = %s -> %s",
            parameter,
            value,
            response
        )

        return

    # --------------------------------------------------------
    # Force controller read-back
    # --------------------------------------------------------

    readback = None

    try:
        readback = burner.getItem(parameter)
    except Exception as exc:
        logger.warning(
            "MQTT write succeeded but read-back failed: %s: %s",
            parameter,
            exc
        )

    # --------------------------------------------------------
    # Technical serial frame
    # --------------------------------------------------------

    serial_payload = getattr(
        burner,
        "last_write_payload",
        None
    )

    # --------------------------------------------------------
    # Activity log
    #
    # payload = EXACT VALUE SENT FROM HOME ASSISTANT
    # serial frame is kept in details for diagnostics.
    # --------------------------------------------------------

    details = (
        f"Old: {old_value} -> New: {value} | "
        f"Read-back: {readback}"
    )

    if serial_payload:
        details += (
            f" | Serial payload: {serial_payload}"
        )

    db.add_activity(
        "MQTT",
        parameter,
        details,
        payload=value,
        response="OK"
    )

    logger.info(
        "MQTT command completed: %s = %s "
        "(readback=%s, serial=%s)",
        parameter,
        value,
        readback,
        serial_payload
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
        mqtt_client.on_message = mqtt_on_message

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
# DAILY STATISTICS
# ============================================================

def calculate_daily_stats(local_date):

    zone = get_timezone()
    timezone_name = get_timezone_name()

    if isinstance(local_date, str):
        local_date = datetime.strptime(
            local_date,
            "%Y-%m-%d"
        ).date()

    # Local midnight -> next local midnight.
    # Converting both boundaries separately to UTC also
    # handles 23/25 hour days around daylight saving time.
    start_local = datetime(
        local_date.year,
        local_date.month,
        local_date.day,
        tzinfo=zone
    )

    next_date = (
        local_date +
        timedelta(days=1)
    )

    end_local = datetime(
        next_date.year,
        next_date.month,
        next_date.day,
        tzinfo=zone
    )

    start_utc = (
        start_local
        .astimezone(timezone.utc)
        .isoformat()
    )

    end_utc = (
        end_local
        .astimezone(timezone.utc)
        .isoformat()
    )

    with db.lock:

        conn = db._connect()

        try:

            pellet_row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(kg), 0) AS pellet_kg
                FROM pellet_consumption_hourly
                WHERE hour_start >= ?
                  AND hour_start < ?
                """,
                (
                    start_utc,
                    end_utc
                )
            ).fetchone()

            measurement_row = conn.execute(
                """
                SELECT

                    AVG(
                        CASE
                            WHEN parameter = 'outside_temp'
                            THEN value
                        END
                    ) AS outside_temp_avg,

                    AVG(
                        CASE
                            WHEN parameter = 'power'
                            THEN value
                        END
                    ) AS power_avg,

                    MAX(
                        CASE
                            WHEN parameter = 'power'
                            THEN value
                        END
                    ) AS power_max,

                    AVG(
                        CASE
                            WHEN parameter = 'power_kW'
                            THEN value
                        END
                    ) AS power_kw_avg,

                    MAX(
                        CASE
                            WHEN parameter = 'power_kW'
                            THEN value
                        END
                    ) AS power_kw_max

                FROM measurements

                WHERE timestamp >= ?
                  AND timestamp < ?
                  AND parameter IN (
                      'outside_temp',
                      'power',
                      'power_kW'
                  )
                """,
                (
                    start_utc,
                    end_utc
                )
            ).fetchone()

        finally:
            conn.close()

    pellet_kg = float(
        pellet_row["pellet_kg"] or 0
    )

    db.upsert_daily_stats(
        local_date=local_date.isoformat(),
        timezone_name=timezone_name,
        pellet_kg=pellet_kg,
        outside_temp_avg=measurement_row["outside_temp_avg"],
        power_avg=measurement_row["power_avg"],
        power_max=measurement_row["power_max"],
        power_kw_avg=measurement_row["power_kw_avg"],
        power_kw_max=measurement_row["power_kw_max"]
    )

    logger.info(
        "Daily stats stored: %s -> "
        "%.3f kg, outside avg=%s, "
        "power avg=%s, power_kW avg=%s",
        local_date.isoformat(),
        pellet_kg,
        measurement_row["outside_temp_avg"],
        measurement_row["power_avg"],
        measurement_row["power_kw_avg"]
    )


def backfill_daily_stats():

    zone = get_timezone()
    today_local = datetime.now(zone).date()

    with db.lock:

        conn = db._connect()

        try:

            pellet_rows = conn.execute(
                """
                SELECT hour_start
                FROM pellet_consumption_hourly
                ORDER BY hour_start ASC
                """
            ).fetchall()

            existing_rows = conn.execute(
                """
                SELECT local_date
                FROM daily_stats
                """
            ).fetchall()

        finally:
            conn.close()

    if not pellet_rows:
        logger.info(
            "Daily stats backfill: no pellet history yet"
        )
        return

    existing_dates = {
        row["local_date"]
        for row in existing_rows
    }

    local_dates = sorted({
        datetime.fromisoformat(
            row["hour_start"].replace(
                "Z",
                "+00:00"
            )
        )
        .astimezone(zone)
        .date()
        for row in pellet_rows
    })

    calculated = 0
    skipped = 0

    for local_date in local_dates:

        date_key = local_date.isoformat()

        # Completed historical days are permanent once stored.
        # This prevents old temperature/output statistics from
        # being overwritten after raw measurement retention expires.
        if (
            local_date < today_local
            and date_key in existing_dates
        ):
            skipped += 1
            continue

        try:

            calculate_daily_stats(
                local_date
            )

            calculated += 1

        except Exception:

            logger.exception(
                "Daily stats backfill error for %s",
                local_date
            )

    logger.info(
        "Daily stats backfill completed: "
        "%d calculated, %d existing days preserved",
        calculated,
        skipped
    )


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

    try:

        local_date = (
            hour_start
            .astimezone(get_timezone())
            .date()
        )

        calculate_daily_stats(
            local_date
        )

    except Exception:

        logger.exception(
            "Daily stats update error for pellet hour %s",
            start_iso
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

def get_wifi_signal():
    """
    Read Raspberry Pi WiFi signal from /proc/net/wireless.

    Returns:
        interface: WiFi interface name
        dbm: signal strength in dBm
        percent: approximate signal percentage
    """

    try:
        path = Path("/proc/net/wireless")

        if not path.exists():
            return {
                "interface": None,
                "dbm": None,
                "percent": None
            }

        lines = path.read_text().splitlines()[2:]

        for line in lines:
            if ":" not in line:
                continue

            interface, values = line.split(":", 1)
            interface = interface.strip()

            fields = values.split()

            if len(fields) < 3:
                continue

            # /proc/net/wireless signal level
            dbm = int(float(fields[2].rstrip(".")))

            # Approximate percentage:
            # -100 dBm = 0%
            #  -50 dBm = 100%
            percent = max(
                0,
                min(
                    100,
                    int((dbm + 100) * 2)
                )
            )

            return {
                "interface": interface,
                "dbm": dbm,
                "percent": percent
            }

    except Exception as e:
        logging.debug("WiFi signal read failed: %s", e)

    return {
        "interface": None,
        "dbm": None,
        "percent": None
    }


@app.get("/api/v1/status")
def status():

    wifi = get_wifi_signal()

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
            "history_rows": db.count(),
            "wifi": wifi
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
# API: CONTROLLER SETTINGS / COMMANDS
# ============================================================

# Parameters that are deliberately exposed for editing in
# Woody Monitor. The actual protocol metadata (address,
# decimals, min/max and version support) comes directly from
# the active Scotteprotocol database.
#
# Measurements and counters are therefore never made writable
# merely because they exist in /api/v1/live.

CONTROLLER_SETTING_GROUPS = {
    "Temperatures": [
        "boiler_temp_set",
        "boiler_temp_min",
        "boiler_temp_diff_down",
        "boiler_temp_diff_up",
        "hotwater_temp_set",
        "hotwater_temp_diff",
        "chute_temp_max",
    ],

    "Power": [
        "min_power",
        "max_power",
        "regulator_P",
        "regulator_I",
        "regulator_D",
    ],

    "Feeder": [
        "feeder_low",
        "feeder_high",
        "feed_per_minute",
        "feeder_capacity",
        "feeder_capacity_min",
        "feeder_capacity_max",
        "magazine_content",
    ],

    "Blower": [
        "blower_low",
        "blower_mid",
        "blower_high",
        "blower_cleaning",
        "blower_off_time",
        "blower_corr_low",
        "blower_corr_mid",
        "blower_corr_high",
    ],

    "Oxygen": [
        "oxygen_regulation",
        "oxygen_low",
        "oxygen_mid",
        "oxygen_high",
        "oxygen_gain",
        "oxygen_corr_10",
        "oxygen_corr_50",
        "oxygen_corr_100",
        "oxygen_corr_interval",
        "oxygen_regulation_P",
        "oxygen_regulation_D",
    ],

    "Cleaning": [
        "cleaning_interval",
        "cleaning_time",
        "comp_clean_interval",
        "comp_clean_time",
        "comp_clean_blower",
        "comp_clean_wait",
    ],

    "Timers": [
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
    ],

    "System": [
        "autocalculation",
        "light_required",
        "language",
    ],
}


def controller_setting_metadata():

    global burner

    if burner is None:
        raise HTTPException(
            status_code=503,
            detail="Controller not connected"
        )

    database = burner.getDataBase()

    result = []

    for group, names in CONTROLLER_SETTING_GROUPS.items():

        for name in names:

            dataparam = database.get(name)

            if dataparam is None:
                continue

            # Writable PellMon parameters have an address and
            # a readable frame. Commands are deliberately
            # excluded from the settings page.
            if not hasattr(dataparam, "address"):
                continue

            if not hasattr(dataparam, "frame"):
                continue

            try:
                value = burner.getItem(name)
            except Exception:
                with state_lock:
                    value = live_data["values"].get(name)

            decimals = getattr(dataparam, "decimals", 0)

            if decimals > 0:
                step = 1 / (10 ** decimals)
            else:
                step = 1

            result.append({
                "id": name,
                "name": name.replace("_", " ").title(),
                "group": group,
                "value": value,
                "min": dataparam.min,
                "max": dataparam.max,
                "decimals": decimals,
                "step": step,
            })

    return result



@app.get("/api/v1/log")
def get_activity_log(limit: int = 500):

    limit = max(1, min(int(limit), 2000))

    return {
        "entries": db.get_activity(limit)
    }



@app.post("/api/v1/log")
def add_activity_log(
    type: str = Query(...),
    event: str = Query(...),
    details: str = Query(None)
):

    db.add_activity(
        type,
        event,
        details
    )

    return {"ok": True}


@app.delete("/api/v1/log")
def clear_activity_log():

    db.clear_activity()

    db.add_activity(
        "SYSTEM",
        "Log cleared",
        "Activity log cleared by user"
    )

    return {"ok": True}


@app.get("/api/v1/controller/settings")
def get_controller_settings():

    return {
        "settings": controller_setting_metadata()
    }


@app.post("/api/v1/controller/settings/{parameter}")
def set_controller_setting(
    parameter: str,
    value: str = Query(...)
):

    global burner

    if burner is None:
        raise HTTPException(
            status_code=503,
            detail="Controller not connected"
        )

    allowed = {
        item
        for names in CONTROLLER_SETTING_GROUPS.values()
        for item in names
    }

    if parameter not in allowed:
        raise HTTPException(
            status_code=404,
            detail="Unknown or read-only controller setting"
        )

    database = burner.getDataBase()
    dataparam = database.get(parameter)

    # Read current value before writing so the log can show
    # old value -> requested value.
    old_controller_value = None

    try:
        old_controller_value = burner.getItem(parameter)
    except Exception:
        pass

    if (
        dataparam is None
        or not hasattr(dataparam, "address")
        or not hasattr(dataparam, "frame")
    ):
        raise HTTPException(
            status_code=400,
            detail="Parameter is not writable on this controller"
        )

    try:
        response = burner.setItem(
            parameter,
            value
        )
    except Exception as exc:
        logger.exception(
            "Controller setting write failed: %s",
            parameter
        )

        raise HTTPException(
            status_code=500,
            detail=str(exc)
        )

    if str(response) != "OK":
        raise HTTPException(
            status_code=400,
            detail=str(response)
        )

    # PellMon getItem() knows that this parameter has just
    # been written and forces a fresh controller read.
    try:
        readback = burner.getItem(parameter)
    except Exception as exc:
        logger.warning(
            "Controller setting written but readback failed: %s: %s",
            parameter,
            exc
        )
        readback = None

    logger.info(
        "Controller setting changed: %s = %s (readback=%s)",
        parameter,
        value,
        readback
    )

    db.add_activity(
        "SETTING",
        parameter,
        f"Old: {old_controller_value} -> New: {value} | Read-back: {readback}",
        # Human-readable serial frame sent to controller.
        # Example: E010160C
        payload=getattr(
            burner,
            "last_write_payload",
            None
        ),
        response=(
            "OK"
            if str(
                getattr(
                    burner,
                    "last_write_response",
                    ""
                ) or str(response)
            ).startswith("OK")
            else (
                getattr(
                    burner,
                    "last_write_response",
                    None
                ) or str(response)
            )
        )
    )

    return {
        "ok": True,
        "parameter": parameter,
        "requested": value,
        "value": readback,
    }


@app.post("/api/v1/controller/burner/{action}")
def controller_burner_command(action: str):

    global burner

    if burner is None:
        raise HTTPException(
            status_code=503,
            detail="Controller not connected"
        )

    commands = {
        "start": "burner_on",
        "stop": "burner_off",
    }

    command = commands.get(action.lower())

    if command is None:
        raise HTTPException(
            status_code=400,
            detail="Action must be start or stop"
        )

    database = burner.getDataBase()

    if command not in database:
        raise HTTPException(
            status_code=400,
            detail="Command not supported by this controller"
        )

    try:
        response = burner.setItem(
            command,
            "0"
        )
    except Exception as exc:

        logger.exception(
            "Burner command failed: %s",
            command
        )

        raise HTTPException(
            status_code=500,
            detail=str(exc)
        )

    if str(response) != "OK":
        raise HTTPException(
            status_code=400,
            detail=str(response)
        )

    logger.info(
        "Burner command sent: %s",
        command
    )

    db.add_activity(
        "CONTROLLER",
        "Burner " + action.lower(),
        f"Command: {command}",
        payload=getattr(
            burner,
            "last_write_payload_hex",
            None
        ),
        response=getattr(
            burner,
            "last_write_response",
            None
        ) or str(response)
    )

    return {
        "ok": True,
        "action": action.lower(),
        "command": command,
    }


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


@app.get("/api/v1/statistics/daily")
def daily_statistics(
    days: int = Query(365, gt=0, le=3650)
):
    """
    Daily pellet and burner statistics.

    Only completed local calendar days are used for averages
    and record values. The current day is excluded because it
    is still incomplete.
    """

    zone = get_timezone()
    today_local = datetime.now(zone).date()

    start_date = (
        today_local - timedelta(days=days)
    ).isoformat()

    end_date = (
        today_local - timedelta(days=1)
    ).isoformat()

    completed_rows = db.get_daily_stats(
        start_date=start_date,
        end_date=end_date
    )

    all_rows = db.get_daily_stats()

    if all_rows:
        coverage = {
            "first_date": all_rows[0]["local_date"],
            "last_date": all_rows[-1]["local_date"],
            "total_days": len(all_rows),
            "completed_days_in_period": len(completed_rows)
        }
    else:
        coverage = {
            "first_date": None,
            "last_date": None,
            "total_days": 0,
            "completed_days_in_period": 0
        }

    if not completed_rows:
        return {
            "timezone": get_timezone_name(),
            "period_days": days,
            "coverage": coverage,
            "averages": {
                "kg_per_day": None,
                "kg_per_week": None,
                "kg_per_month": None,
                "kg_per_year": None
            },
            "max_consumption": None,
            "max_output": None
        }

    pellet_values = [
        float(row["pellet_kg"] or 0)
        for row in completed_rows
    ]

    total_kg = sum(pellet_values)
    completed_count = len(completed_rows)

    avg_per_day = total_kg / completed_count

    # Normalised averages based on the available completed days.
    avg_per_week = avg_per_day * 7.0
    avg_per_month = avg_per_day * (365.2425 / 12.0)
    avg_per_year = avg_per_day * 365.2425

    consumption_candidates = [
        row
        for row in completed_rows
        if float(row["pellet_kg"] or 0) > 0
    ]

    max_consumption_row = (
        max(
            consumption_candidates,
            key=lambda row: float(row["pellet_kg"] or 0)
        )
        if consumption_candidates
        else None
    )

    output_candidates = [
        row
        for row in completed_rows
        if row["power_kw_avg"] is not None
        and float(row["power_kw_avg"] or 0) > 0
    ]

    max_output_row = (
        max(
            output_candidates,
            key=lambda row: float(row["power_kw_avg"])
        )
        if output_candidates
        else None
    )

    if max_consumption_row is not None:
        max_consumption = {
            "date": max_consumption_row["local_date"],
            "pellet_kg": float(
                max_consumption_row["pellet_kg"] or 0
            ),
            "outside_temp_avg": (
                float(max_consumption_row["outside_temp_avg"])
                if max_consumption_row["outside_temp_avg"] is not None
                else None
            )
        }
    else:
        max_consumption = None

    if max_output_row is not None:
        max_output = {
            "date": max_output_row["local_date"],
            "power_kw_avg": float(
                max_output_row["power_kw_avg"]
            ),
            "power_kw_max": (
                float(max_output_row["power_kw_max"])
                if max_output_row["power_kw_max"] is not None
                else None
            ),
            "power_avg": (
                float(max_output_row["power_avg"])
                if max_output_row["power_avg"] is not None
                else None
            ),
            "power_max": (
                float(max_output_row["power_max"])
                if max_output_row["power_max"] is not None
                else None
            ),
            "pellet_kg": float(
                max_output_row["pellet_kg"] or 0
            ),
            "outside_temp_avg": (
                float(max_output_row["outside_temp_avg"])
                if max_output_row["outside_temp_avg"] is not None
                else None
            )
        }
    else:
        max_output = None

    return {
        "timezone": get_timezone_name(),
        "period_days": days,
        "coverage": coverage,
        "total_kg_completed_days": total_kg,
        "averages": {
            "kg_per_day": avg_per_day,
            "kg_per_week": avg_per_week,
            "kg_per_month": avg_per_month,
            "kg_per_year": avg_per_year
        },
        "max_consumption": max_consumption,
        "max_output": max_output
    }


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

    try:
        backfill_daily_stats()
    except Exception:
        logger.exception(
            "Daily stats startup backfill failed"
        )
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
