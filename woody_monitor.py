#!/usr/bin/env python3

import sys
import time
import threading
import logging
import os
import socket
import fcntl
import struct
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, available_timezones
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

APP_VERSION = "1.1"

from fastapi import FastAPI, Query, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.background import BackgroundTask
import uvicorn

from Scotteprotocol.protocol import Protocol
from database import Database
import json
import csv
import io
import paho.mqtt.client as mqtt
import urllib.request
import urllib.error
import sqlite3
import tempfile
import zipfile
import shutil



# ============================================================
# HOME ASSISTANT INTEGRATION
# ============================================================

HOME_ASSISTANT_SETTINGS_FILE = (
    BASE_DIR / "data" / "home_assistant.json"
)

home_assistant_lock = threading.Lock()

home_assistant_settings = {
    "url": "",
    "token": "",
    "indoor_temperature_entity": ""
}


def load_home_assistant_settings():

    try:

        if not HOME_ASSISTANT_SETTINGS_FILE.exists():
            return

        with HOME_ASSISTANT_SETTINGS_FILE.open(
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)

        with home_assistant_lock:

            home_assistant_settings["url"] = str(
                data.get("url", "")
            ).strip().rstrip("/")

            home_assistant_settings["token"] = str(
                data.get("token", "")
            ).strip()

            home_assistant_settings[
                "indoor_temperature_entity"
            ] = str(
                data.get(
                    "indoor_temperature_entity",
                    ""
                )
            ).strip()

        logger.info(
            "Home Assistant settings loaded"
        )

    except Exception:
        logger.exception(
            "Could not load Home Assistant settings"
        )


def save_home_assistant_settings():

    HOME_ASSISTANT_SETTINGS_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with home_assistant_lock:

        data = dict(
            home_assistant_settings
        )

    temporary = (
        HOME_ASSISTANT_SETTINGS_FILE
        .with_suffix(".tmp")
    )

    with temporary.open(
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            indent=2
        )

    os.chmod(
        temporary,
        0o600
    )

    temporary.replace(
        HOME_ASSISTANT_SETTINGS_FILE
    )

    os.chmod(
        HOME_ASSISTANT_SETTINGS_FILE,
        0o600
    )


def get_home_assistant_temperature():

    with home_assistant_lock:

        url = home_assistant_settings[
            "url"
        ]

        token = home_assistant_settings[
            "token"
        ]

        entity = home_assistant_settings[
            "indoor_temperature_entity"
        ]

    if not url or not token or not entity:

        return {
            "configured": False,
            "connected": False,
            "entity": entity or None,
            "temperature": None,
            "unit": None,
            "error": None
        }

    request_url = (
        url.rstrip("/")
        + "/api/states/"
        + entity
    )

    request = urllib.request.Request(
        request_url,
        headers={
            "Authorization":
                "Bearer " + token,
            "Content-Type":
                "application/json"
        }
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=5
        ) as response:

            payload = json.loads(
                response.read().decode(
                    "utf-8"
                )
            )

        raw_state = payload.get(
            "state"
        )

        temperature = float(
            raw_state
        )

        attributes = payload.get(
            "attributes",
            {}
        )

        return {
            "configured": True,
            "connected": True,
            "entity": entity,
            "temperature": temperature,
            "unit": attributes.get(
                "unit_of_measurement",
                "°C"
            ),
            "friendly_name": attributes.get(
                "friendly_name",
                entity
            ),
            "error": None
        }

    except urllib.error.HTTPError as error:

        if error.code == 401:
            message = "Authentication failed"
        elif error.code == 404:
            message = "Entity not found"
        else:
            message = (
                "Home Assistant HTTP "
                + str(error.code)
            )

        return {
            "configured": True,
            "connected": False,
            "entity": entity,
            "temperature": None,
            "unit": None,
            "error": message
        }

    except Exception as error:

        return {
            "configured": True,
            "connected": False,
            "entity": entity,
            "temperature": None,
            "unit": None,
            "error": str(error)
        }


# ============================================================
# NETWORK MONITOR
# ============================================================

NETWORK_INTERFACE = os.environ.get(
    "WOODY_NETWORK_INTERFACE",
    "wlan0"
)

network_log_state = {
    "initialized": False,
    "connected": False,
    "ip": None,
    "changed_monotonic": None,
}


def get_interface_ipv4(interface):

    try:

        sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_DGRAM
        )

        result = fcntl.ioctl(
            sock.fileno(),
            0x8915,
            struct.pack(
                "256s",
                interface[:15].encode()
            )
        )

        sock.close()

        return socket.inet_ntoa(
            result[20:24]
        )

    except Exception:

        return None


def network_interface_is_up(interface):

    try:

        state_file = Path(
            f"/sys/class/net/{interface}/operstate"
        )

        return (
            state_file.read_text().strip()
            == "up"
        )

    except Exception:

        return False


def log_network_state(
    connected,
    ip=None
):

    global network_log_state

    initialized = (
        network_log_state["initialized"]
    )

    previous_connected = (
        network_log_state["connected"]
    )

    previous_ip = (
        network_log_state["ip"]
    )

    if not initialized:

        network_log_state["initialized"] = True
        network_log_state["connected"] = connected
        network_log_state["ip"] = ip
        network_log_state["changed_monotonic"] = time.monotonic()

        if connected:

            db.add_activity(
                "NETWORK",
                "Network connected",
                (
                    f"{NETWORK_INTERFACE}: "
                    f"{ip}"
                ),
                response="OK"
            )

        else:

            db.add_activity(
                "NETWORK",
                "Network disconnected",
                (
                    f"{NETWORK_INTERFACE} "
                    f"is unavailable"
                ),
                response="ERROR"
            )

        return


    if connected != previous_connected:

        network_log_state["connected"] = connected
        network_log_state["ip"] = ip
        network_log_state["changed_monotonic"] = time.monotonic()

        if connected:

            db.add_activity(
                "NETWORK",
                "Network connected",
                (
                    f"{NETWORK_INTERFACE}: "
                    f"{ip}"
                ),
                response="OK"
            )

        else:

            db.add_activity(
                "NETWORK",
                "Network disconnected",
                (
                    f"Connection lost on "
                    f"{NETWORK_INTERFACE}"
                ),
                response="ERROR"
            )

        return


    # Log an IP change while the interface remains connected.
    if (
        connected
        and ip
        and previous_ip
        and ip != previous_ip
    ):

        network_log_state["ip"] = ip

        db.add_activity(
            "NETWORK",
            "IP address changed",
            (
                f"{previous_ip} → {ip} "
                f"on {NETWORK_INTERFACE}"
            ),
            response="OK"
        )


def network_monitor_loop():

    while True:

        try:

            interface_up = (
                network_interface_is_up(
                    NETWORK_INTERFACE
                )
            )

            ip = get_interface_ipv4(
                NETWORK_INTERFACE
            )

            connected = bool(
                interface_up and ip
            )

            log_network_state(
                connected,
                ip
            )

        except Exception:

            logger.exception(
                "Network monitor error"
            )

        time.sleep(10)


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
    "magazine_content",
    "power",
    "power_kW",
    "feeder_time",
}

# MQTT
MQTT_SETTINGS_FILE = BASE_DIR / "data" / "mqtt.json"
mqtt_settings_lock = threading.RLock()

MQTT_DEFAULTS = {
    "enabled": True,
    "broker": os.environ.get(
        "WOODY_MQTT_BROKER",
        "localhost"
    ),
    "port": int(
        os.environ.get(
            "WOODY_MQTT_PORT",
            "1883"
        )
    ),
    "username": os.environ.get(
        "WOODY_MQTT_USERNAME",
        ""
    ),
    "password": os.environ.get(
        "WOODY_MQTT_PASSWORD",
        ""
    ),
    "topic": os.environ.get(
        "WOODY_MQTT_TOPIC",
        "woodymonitor"
    )
}


def load_mqtt_settings():

    settings = dict(MQTT_DEFAULTS)

    try:

        if MQTT_SETTINGS_FILE.exists():

            data = json.loads(
                MQTT_SETTINGS_FILE.read_text()
            )

            if isinstance(data, dict):

                for key in settings:
                    if key in data:
                        settings[key] = data[key]

    except Exception:
        logger.exception(
            "Could not load MQTT settings"
        )

    try:
        settings["port"] = int(
            settings["port"]
        )
    except Exception:
        settings["port"] = 1883

    settings["enabled"] = bool(
        settings.get("enabled", True)
    )

    settings["broker"] = str(
        settings.get("broker", "")
    ).strip()

    settings["username"] = str(
        settings.get("username", "")
    )

    settings["password"] = str(
        settings.get("password", "")
    )

    settings["topic"] = (
        str(
            settings.get(
                "topic",
                "woodymonitor"
            )
        )
        .strip()
        .strip("/")
    ) or "woodymonitor"

    return settings


mqtt_settings = load_mqtt_settings()

MQTT_ENABLED = mqtt_settings["enabled"]
MQTT_BROKER = mqtt_settings["broker"]
MQTT_PORT = mqtt_settings["port"]
MQTT_USERNAME = mqtt_settings["username"]
MQTT_PASSWORD = mqtt_settings["password"]
MQTT_TOPIC = mqtt_settings["topic"]

MQTT_INTERVAL = 5


def save_mqtt_settings():

    MQTT_SETTINGS_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temp_file = MQTT_SETTINGS_FILE.with_suffix(
        ".json.tmp"
    )

    with mqtt_settings_lock:

        data = {
            "enabled": bool(MQTT_ENABLED),
            "broker": MQTT_BROKER,
            "port": int(MQTT_PORT),
            "username": MQTT_USERNAME,
            "password": MQTT_PASSWORD,
            "topic": MQTT_TOPIC
        }

        temp_file.write_text(
            json.dumps(
                data,
                indent=2
            )
        )

        temp_file.replace(
            MQTT_SETTINGS_FILE
        )

    try:
        MQTT_SETTINGS_FILE.chmod(0o600)
    except Exception:
        pass


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
    version=APP_VERSION
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
# CLEANING SETTINGS
# ============================================================

CLEANING_SETTINGS_FILE = str(
    BASE_DIR / "data" / "cleaning_settings.json"
)

DEFAULT_CLEANING_INTERVAL_DAYS = 30

cleaning_settings_lock = threading.Lock()

cleaning_settings = {
    "last_cleaning": None,
    "interval_days": DEFAULT_CLEANING_INTERVAL_DAYS
}


def load_cleaning_settings():

    global cleaning_settings

    try:

        path = Path(CLEANING_SETTINGS_FILE)

        if not path.exists():
            return

        with path.open("r") as f:
            data = json.load(f)

        interval_days = int(
            data.get(
                "interval_days",
                DEFAULT_CLEANING_INTERVAL_DAYS
            )
        )

        last_cleaning = data.get(
            "last_cleaning"
        )

        if 1 <= interval_days <= 3650:
            cleaning_settings["interval_days"] = interval_days

        if (
            last_cleaning is None or
            isinstance(last_cleaning, str)
        ):
            cleaning_settings["last_cleaning"] = last_cleaning

        logger.info(
            "Loaded cleaning settings: %s / %d days",
            cleaning_settings["last_cleaning"],
            cleaning_settings["interval_days"]
        )

    except Exception:

        logger.exception(
            "Could not load cleaning settings"
        )


def save_cleaning_settings():

    path = Path(CLEANING_SETTINGS_FILE)

    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temporary = path.with_suffix(".tmp")

    with temporary.open("w") as f:
        json.dump(
            cleaning_settings,
            f,
            indent=2
        )

    temporary.replace(path)


def get_cleaning_settings():

    with cleaning_settings_lock:
        return dict(cleaning_settings)


# ============================================================
# ADVANCED TIMER
# ============================================================

ADVANCED_TIMER_FILE = str(
    BASE_DIR / "data" / "advanced_timer.json"
)

advanced_timer_lock = threading.Lock()

ADVANCED_TIMER_DAYS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]


def default_advanced_timer_schedule():

    return {
        day: [True] * 24
        for day in ADVANCED_TIMER_DAYS
    }


advanced_timer_settings = {
    "enabled": False,
    "test_mode": True,
    "schedule": default_advanced_timer_schedule(),
    "last_slot": None,
    "last_action": None,
}


def validate_advanced_timer_schedule(schedule):

    if not isinstance(schedule, dict):
        return False

    for day in ADVANCED_TIMER_DAYS:

        hours = schedule.get(day)

        if (
            not isinstance(hours, list)
            or len(hours) != 24
        ):
            return False

        if not all(
            isinstance(value, bool)
            for value in hours
        ):
            return False

    return True


def load_advanced_timer_settings():

    try:

        path = Path(ADVANCED_TIMER_FILE)

        if not path.exists():
            return

        with path.open("r") as f:
            data = json.load(f)

        schedule = data.get("schedule")

        if validate_advanced_timer_schedule(schedule):
            advanced_timer_settings["schedule"] = schedule

        advanced_timer_settings["enabled"] = bool(
            data.get("enabled", False)
        )

        advanced_timer_settings["test_mode"] = bool(
            data.get("test_mode", True)
        )

        advanced_timer_settings["last_slot"] = data.get(
            "last_slot"
        )

        advanced_timer_settings["last_action"] = data.get(
            "last_action"
        )

        logger.info(
            "Loaded advanced timer: enabled=%s",
            advanced_timer_settings["enabled"]
        )

    except Exception:

        logger.exception(
            "Could not load advanced timer settings"
        )


def save_advanced_timer_settings():

    path = Path(ADVANCED_TIMER_FILE)

    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temporary = path.with_suffix(".tmp")

    with temporary.open("w") as f:

        json.dump(
            advanced_timer_settings,
            f,
            indent=2
        )

    temporary.replace(path)


def get_advanced_timer_settings():

    with advanced_timer_lock:

        return {
            "enabled":
                advanced_timer_settings["enabled"],

            "test_mode":
                advanced_timer_settings["test_mode"],

            "schedule": {
                day: list(
                    advanced_timer_settings[
                        "schedule"
                    ][day]
                )
                for day in ADVANCED_TIMER_DAYS
            },

            "last_slot":
                advanced_timer_settings["last_slot"],

            "last_action":
                advanced_timer_settings["last_action"],
        }


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
# API: ABOUT
# ============================================================

@app.get("/api/v1/about")
def get_about():

    return {
        "application": "Woody Monitor",
        "version": APP_VERSION,
        "api": "online"
    }


# ============================================================
# PELLET CURRENCY
# ============================================================

PELLET_CURRENCY_FILE = (
    BASE_DIR / "data" / "pellet_currency.json"
)

PELLET_CURRENCIES = {
    "DKK",
    "EUR",
    "SEK",
    "NOK",
    "GBP",
    "USD",
}


def load_pellet_currency():

    try:

        if not PELLET_CURRENCY_FILE.exists():
            return "DKK"

        with PELLET_CURRENCY_FILE.open(
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)

        currency = str(
            data.get("currency", "DKK")
        ).upper()

        if currency not in PELLET_CURRENCIES:
            return "DKK"

        return currency

    except Exception:

        logger.exception(
            "Could not load pellet currency"
        )

        return "DKK"


def save_pellet_currency(currency):

    PELLET_CURRENCY_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with PELLET_CURRENCY_FILE.open(
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            {
                "currency": currency
            },
            f,
            indent=2
        )


@app.get("/api/v1/settings/pellet-currency")
def get_pellet_currency():

    return {
        "currency": load_pellet_currency()
    }


@app.post("/api/v1/settings/pellet-currency")
def set_pellet_currency(
    currency: str = Query(...)
):

    currency = currency.upper().strip()

    if currency not in PELLET_CURRENCIES:

        raise HTTPException(
            status_code=400,
            detail="Unsupported currency"
        )

    save_pellet_currency(currency)

    db.add_activity(
        "SETTING",
        "Pellet currency",
        currency,
        response="OK"
    )

    return {
        "currency": currency
    }


# ============================================================
# API: SYSTEM INFORMATION
# ============================================================

SD_STORAGE_WARNING_PERCENT = 80.0

storage_warning_state = {
    "active": False,
}


def _format_duration(seconds):
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        return "--"

    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)

    if days:
        return f"{days}d {hours}h {minutes}m"

    if hours:
        return f"{hours}h {minutes}m"

    return f"{minutes}m"


def _format_bytes(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "--"

    units = ["B", "KB", "MB", "GB", "TB"]

    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024

    return "--"


def _directory_size(path, exclude=None):
    total = 0

    try:
        root_path = Path(path).resolve()
        excluded = Path(exclude).resolve() if exclude else None

        for root, dirs, files in os.walk(root_path):
            root_obj = Path(root)

            if excluded is not None:
                dirs[:] = [
                    name
                    for name in dirs
                    if not (
                        root_obj / name
                    ).resolve().is_relative_to(excluded)
                ]

            for filename in files:
                file_path = root_obj / filename

                if excluded is not None:
                    try:
                        file_path.resolve().relative_to(excluded)
                        continue
                    except ValueError:
                        pass

                try:
                    if not file_path.is_symlink():
                        total += file_path.stat().st_size
                except (OSError, FileNotFoundError):
                    pass

    except Exception:
        logger.exception("Could not calculate application size")

    return total


def _wifi_uptime_seconds(interface="wlan0"):
    try:
        operstate = Path(
            f"/sys/class/net/{interface}/operstate"
        ).read_text().strip()

        if operstate != "up":
            return None

        result = os.popen(
            f"nmcli -t -f GENERAL.CONNECTION device show {interface} 2>/dev/null"
        ).read().strip()

        if not result:
            return None

        # NetworkManager does not expose a reliable connection-start
        # timestamp through sysfs. Use Woody Monitor's network state
        # transition timestamp when available.
        if (
            network_log_state.get("initialized")
            and network_log_state.get("connected")
            and network_log_state.get("changed_monotonic")
        ):
            return (
                time.monotonic()
                - network_log_state["changed_monotonic"]
            )

    except Exception:
        pass

    return None


@app.get("/api/v1/system/info")
def get_system_info():

    now = datetime.now(timezone.utc)

    try:
        with open("/proc/uptime", "r", encoding="utf-8") as f:
            system_uptime_seconds = float(
                f.read().split()[0]
            )
    except Exception:
        system_uptime_seconds = 0

    boot_time = (
        now - timedelta(seconds=system_uptime_seconds)
    )

    try:
        database_path = BASE_DIR / "data" / "woody.db"
        database_size = database_path.stat().st_size
    except Exception:
        database_size = 0

    application_size = _directory_size(
        BASE_DIR,
        exclude=BASE_DIR / "data"
    )

    try:
        disk = os.statvfs(BASE_DIR)

        disk_total = disk.f_blocks * disk.f_frsize
        disk_free = disk.f_bavail * disk.f_frsize
        disk_used = disk_total - disk_free

        disk_percent = (
            (disk_used / disk_total) * 100
            if disk_total
            else 0
        )

    except Exception:
        disk_total = 0
        disk_used = 0
        disk_free = 0
        disk_percent = 0

    storage_warning = disk_percent >= SD_STORAGE_WARNING_PERCENT

    # Log only when the storage warning changes from inactive to active.
    # It becomes eligible for a new warning after usage drops below 80%.
    if storage_warning and not storage_warning_state["active"]:
        try:
            db.add_activity(
                "SYSTEM",
                "Storage warning",
                (
                    f"SD card usage is {disk_percent:.1f}% "
                    f"(warning threshold {SD_STORAGE_WARNING_PERCENT:.0f}%)"
                ),
                response="WARNING"
            )
        except Exception:
            logger.exception("Could not log storage warning")

    storage_warning_state["active"] = storage_warning

    wifi_seconds = _wifi_uptime_seconds("wlan0")

    return {
        "system_uptime": _format_duration(
            system_uptime_seconds
        ),
        "system_uptime_seconds": int(
            system_uptime_seconds
        ),
        "last_boot": boot_time.isoformat(),
        "wifi_uptime": (
            _format_duration(wifi_seconds)
            if wifi_seconds is not None
            else "--"
        ),
        "wifi_uptime_seconds": (
            int(wifi_seconds)
            if wifi_seconds is not None
            else None
        ),
        "application_size": _format_bytes(
            application_size
        ),
        "application_size_bytes": application_size,
        "database_size": _format_bytes(
            database_size
        ),
        "database_size_bytes": database_size,
        "sd_total": _format_bytes(disk_total),
        "sd_used": _format_bytes(disk_used),
        "sd_free": _format_bytes(disk_free),
        "sd_used_percent": round(disk_percent, 1),
        "storage_warning": storage_warning,
        "storage_warning_threshold": SD_STORAGE_WARNING_PERCENT
    }


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

    # A complete read failure means the controller connection
    # is no longer usable. Individual parameter errors are still
    # allowed without marking the entire controller disconnected.
    if not values and errors:

        with state_lock:

            live_data["connected"] = False
            live_data["timestamp"] = timestamp
            live_data["values"] = {}
            live_data["errors"] = errors

        log_controller_changes(
            False,
            {}
        )

        try:
            if burner is not None:
                burner.close()
        except Exception:
            pass

        burner = None

        return

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

# MQTT recovery state.
# Access is protected because callbacks run in the Paho
# network thread while the publish loop runs separately.
mqtt_recovery_lock = threading.Lock()
mqtt_last_reconnect_attempt = 0.0
mqtt_last_publish_success = 0.0

MQTT_RECONNECT_MIN_DELAY = 2
MQTT_RECONNECT_MAX_DELAY = 60
MQTT_RECOVERY_INTERVAL = 15

# QoS 1 heartbeat verifies that the broker is actually
# acknowledging MQTT traffic. This detects connections that
# appear alive locally but no longer reach the broker.
MQTT_HEARTBEAT_INTERVAL = 15
MQTT_HEARTBEAT_TIMEOUT = 10

mqtt_log_state = {
    "initialized": False,
    "connected": False,
}


def log_mqtt_connection_state(
    connected,
    details=None
):

    global mqtt_log_state

    previous_initialized = (
        mqtt_log_state["initialized"]
    )

    previous_connected = (
        mqtt_log_state["connected"]
    )

    # First observed MQTT state.
    if not previous_initialized:

        mqtt_log_state["initialized"] = True
        mqtt_log_state["connected"] = connected

        if connected:

            db.add_activity(
                "MQTT",
                "MQTT connected",
                details or "MQTT connection established",
                response="OK"
            )

        else:

            db.add_activity(
                "MQTT",
                "MQTT disconnected",
                details or "MQTT connection unavailable",
                response="ERROR"
            )

        return

    # Only log actual state changes.
    if connected == previous_connected:
        return

    mqtt_log_state["connected"] = connected

    if connected:

        db.add_activity(
            "MQTT",
            "MQTT connected",
            details or "MQTT connection restored",
            response="OK"
        )

    else:

        db.add_activity(
            "MQTT",
            "MQTT disconnected",
            details or "MQTT connection lost",
            response="ERROR"
        )


def mqtt_on_connect(client, userdata, flags, reason_code, properties):
    global mqtt_connected
    global mqtt_last_publish_success

    if reason_code.is_failure:

        mqtt_connected = False

        logger.error(
            "MQTT connection failed: %s",
            reason_code
        )

        log_mqtt_connection_state(
            False,
            f"Connection failed: {reason_code}"
        )

    else:

        mqtt_connected = True

        with mqtt_recovery_lock:
            mqtt_last_publish_success = time.monotonic()

        log_mqtt_connection_state(
            True,
            f"Connected to {MQTT_BROKER}:{MQTT_PORT}"
        )

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

    log_mqtt_connection_state(
        False,
        f"MQTT connection lost: {reason_code}"
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



def mqtt_stop():

    global mqtt_client
    global mqtt_connected

    client = mqtt_client

    mqtt_client = None
    mqtt_connected = False

    if client is None:
        return

    try:
        client.disconnect()
    except Exception:
        pass

    try:
        client.loop_stop()
    except Exception:
        pass


def mqtt_apply_settings(
    enabled,
    broker,
    port,
    username,
    password,
    topic
):

    global MQTT_ENABLED
    global MQTT_BROKER
    global MQTT_PORT
    global MQTT_USERNAME
    global MQTT_PASSWORD
    global MQTT_TOPIC
    global mqtt_log_state

    clean_broker = str(
        broker
    ).strip()

    clean_username = str(
        username
    )

    clean_password = str(
        password
    )

    clean_topic = (
        str(topic)
        .strip()
        .strip("/")
    )

    try:
        clean_port = int(port)
    except Exception:
        raise ValueError(
            "MQTT port must be a number"
        )

    if not 1 <= clean_port <= 65535:
        raise ValueError(
            "MQTT port must be between 1 and 65535"
        )

    if enabled and not clean_broker:
        raise ValueError(
            "MQTT broker is required"
        )

    if not clean_topic:
        raise ValueError(
            "MQTT base topic is required"
        )

    mqtt_stop()

    with mqtt_settings_lock:

        MQTT_ENABLED = bool(enabled)
        MQTT_BROKER = clean_broker
        MQTT_PORT = clean_port
        MQTT_USERNAME = clean_username
        MQTT_PASSWORD = clean_password
        MQTT_TOPIC = clean_topic

        save_mqtt_settings()

    mqtt_log_state = {
        "initialized": False,
        "connected": False
    }

    if MQTT_ENABLED:
        mqtt_setup()


def mqtt_setup():

    global mqtt_client
    global mqtt_connected

    if not MQTT_ENABLED:
        mqtt_client = None
        mqtt_connected = False
        logger.info(
            "MQTT disabled"
        )
        return

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

        mqtt_client.reconnect_delay_set(
            min_delay=MQTT_RECONNECT_MIN_DELAY,
            max_delay=MQTT_RECONNECT_MAX_DELAY
        )

        mqtt_client.connect(
            MQTT_BROKER,
            MQTT_PORT,
            60
        )

        mqtt_client.loop_start()

        # Give the normal asynchronous MQTT connection callback
        # time to complete before active recovery is allowed.
        global mqtt_last_reconnect_attempt

        with mqtt_recovery_lock:
            mqtt_last_reconnect_attempt = time.monotonic()

        logger.info(
            "MQTT client started"
        )

    except Exception:

        mqtt_client = None

        logger.exception(
            "MQTT setup failed"
        )


def mqtt_try_reconnect():

    global mqtt_client
    global mqtt_last_reconnect_attempt

    if mqtt_client is None:
        return False

    if mqtt_connected:
        return True

    now = time.monotonic()

    with mqtt_recovery_lock:

        if (
            now - mqtt_last_reconnect_attempt
            < MQTT_RECOVERY_INTERVAL
        ):
            return False

        mqtt_last_reconnect_attempt = now

    try:

        logger.warning(
            "MQTT connection unavailable - attempting reconnect"
        )

        result = mqtt_client.reconnect()

        if result != mqtt.MQTT_ERR_SUCCESS:

            logger.warning(
                "MQTT reconnect returned status %s",
                result
            )

            return False

        return True

    except Exception as error:

        logger.warning(
            "MQTT reconnect attempt failed: %s",
            error
        )

        return False


def mqtt_publish_checked(
    topic,
    payload,
    *,
    retain=True
):

    global mqtt_connected
    global mqtt_last_publish_success

    if mqtt_client is None:
        return False

    try:

        info = mqtt_client.publish(
            topic,
            payload,
            qos=0,
            retain=retain
        )

        if info.rc != mqtt.MQTT_ERR_SUCCESS:

            logger.warning(
                "MQTT publish rejected for %s: rc=%s",
                topic,
                info.rc
            )

            mqtt_connected = False

            log_mqtt_connection_state(
                False,
                f"MQTT publish failed with status {info.rc}"
            )

            return False

        with mqtt_recovery_lock:
            mqtt_last_publish_success = time.monotonic()

        return True

    except Exception as error:

        mqtt_connected = False

        logger.warning(
            "MQTT publish failed for %s: %s",
            topic,
            error
        )

        log_mqtt_connection_state(
            False,
            f"MQTT publish error: {error}"
        )

        return False


def mqtt_heartbeat():

    global mqtt_connected
    global mqtt_last_publish_success

    if mqtt_client is None or not mqtt_connected:
        return False

    try:

        info = mqtt_client.publish(
            f"{MQTT_TOPIC}/status/heartbeat",
            str(int(time.time())),
            qos=1,
            retain=False
        )

        if info.rc != mqtt.MQTT_ERR_SUCCESS:

            raise RuntimeError(
                f"publish returned status {info.rc}"
            )

        # QoS 1 requires PUBACK from the broker.
        info.wait_for_publish(
            timeout=MQTT_HEARTBEAT_TIMEOUT
        )

        if not info.is_published():

            raise TimeoutError(
                "broker acknowledgement timeout"
            )

        with mqtt_recovery_lock:
            mqtt_last_publish_success = time.monotonic()

        return True

    except Exception as error:

        mqtt_connected = False

        logger.warning(
            "MQTT heartbeat failed: %s",
            error
        )

        log_mqtt_connection_state(
            False,
            f"MQTT heartbeat failed: {error}"
        )

        # Force the current Paho connection down so that
        # reconnect starts from a clean socket.
        try:
            mqtt_client.disconnect()
        except Exception:
            pass

        return False


def mqtt_publish_loop():

    global mqtt_client

    last_heartbeat = 0.0

    while True:

        try:

            if mqtt_client is None:

                time.sleep(MQTT_INTERVAL)
                continue


            # If Paho has not restored the connection itself,
            # make a rate-limited active reconnect attempt.
            if not mqtt_connected:

                mqtt_try_reconnect()

                time.sleep(MQTT_INTERVAL)
                continue


            now = time.monotonic()

            if (
                now - last_heartbeat
                >= MQTT_HEARTBEAT_INTERVAL
            ):

                last_heartbeat = now

                if not mqtt_heartbeat():

                    mqtt_try_reconnect()

                    time.sleep(MQTT_INTERVAL)
                    continue


            with state_lock:

                connected = live_data["connected"]
                timestamp = live_data["timestamp"]
                values = dict(
                    live_data["values"]
                )


            if connected and values:

                payload = {
                    "connected": connected,
                    "timestamp": timestamp,
                    "values": values
                }


                # Publish the complete dataset first.
                # If this fails, do not queue another 81
                # messages against a dead connection.
                if not mqtt_publish_checked(
                    f"{MQTT_TOPIC}/live",
                    json.dumps(payload),
                    retain=True
                ):

                    mqtt_try_reconnect()

                    time.sleep(MQTT_INTERVAL)
                    continue


                successful_values = 0

                for name, value in values.items():

                    if mqtt_publish_checked(
                        f"{MQTT_TOPIC}/{name}",
                        str(value),
                        retain=True
                    ):

                        successful_values += 1

                    else:

                        # Connection has failed while publishing.
                        # Stop this batch and let recovery handle it.
                        break


                if successful_values == len(values):

                    logger.debug(
                        "MQTT live data published: %d values",
                        len(values)
                    )

                elif not mqtt_connected:

                    mqtt_try_reconnect()


        except Exception:

            logger.exception(
                "MQTT publish loop error"
            )


        time.sleep(MQTT_INTERVAL)


# ============================================================
# DAILY STATISTICS
# ============================================================

_daily_outdoor_temperature_table_ready = False


def ensure_daily_outdoor_temperature_table():

    global _daily_outdoor_temperature_table_ready

    if _daily_outdoor_temperature_table_ready:
        return

    with db.lock:

        conn = db._connect()

        try:

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS
                daily_outdoor_temperature (
                    local_date TEXT PRIMARY KEY,
                    outside_temp_avg REAL,
                    sample_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )

            conn.commit()

        finally:

            conn.close()

    _daily_outdoor_temperature_table_ready = True


def calculate_daily_stats(local_date):

    ensure_daily_outdoor_temperature_table()

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

            # Complete-day outdoor temperature.
            #
            # This is intentionally independent of pellet consumption
            # and is stored separately from outside_temp_avg below,
            # which represents temperature only while pellets are fed.
            all_temperature_row = conn.execute(
                """
                SELECT
                    AVG(value) AS outside_temp_all_avg,
                    COUNT(*) AS sample_count
                FROM measurements
                WHERE timestamp >= ?
                  AND timestamp < ?
                  AND parameter = 'outside_temp'
                """,
                (
                    start_utc,
                    end_utc
                )
            ).fetchone()

            # Temperature and burner output are meaningful for
            # pellet-consumption comparisons only while pellets are
            # actually being fed.
            #
            # feeder_time is a cumulative counter. A positive change
            # therefore marks a real feeder-active measurement interval.
            # The sample immediately before the requested period is
            # included only to calculate the first delta correctly.
            measurement_row = conn.execute(
                """
                SELECT

                    CASE
                        WHEN SUM(outside_temp_samples) > 0
                        THEN
                            SUM(
                                outside_temp_avg *
                                outside_temp_samples
                            ) /
                            SUM(outside_temp_samples)
                    END AS outside_temp_avg,

                    CASE
                        WHEN SUM(power_samples) > 0
                        THEN
                            SUM(
                                power_avg *
                                power_samples
                            ) /
                            SUM(power_samples)
                    END AS power_avg,

                    MAX(power_max)
                        AS power_max,

                    CASE
                        WHEN SUM(power_kw_samples) > 0
                        THEN
                            SUM(
                                power_kw_avg *
                                power_kw_samples
                            ) /
                            SUM(power_kw_samples)
                    END AS power_kw_avg,

                    MAX(power_kw_max)
                        AS power_kw_max

                FROM pellet_consumption_hourly

                WHERE hour_start >= ?
                  AND hour_start < ?
                  AND kg > 0
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

    all_temperature_avg = (
        float(
            all_temperature_row[
                "outside_temp_all_avg"
            ]
        )
        if (
            all_temperature_row is not None
            and all_temperature_row[
                "outside_temp_all_avg"
            ] is not None
        )
        else None
    )

    all_temperature_samples = (
        int(
            all_temperature_row[
                "sample_count"
            ] or 0
        )
        if all_temperature_row is not None
        else 0
    )

    with db.lock:

        conn = db._connect()

        try:

            conn.execute(
                """
                INSERT INTO daily_outdoor_temperature (
                    local_date,
                    outside_temp_avg,
                    sample_count,
                    updated_at
                )
                VALUES (?, ?, ?, ?)

                ON CONFLICT(local_date)
                DO UPDATE SET
                    outside_temp_avg =
                        excluded.outside_temp_avg,
                    sample_count =
                        excluded.sample_count,
                    updated_at =
                        excluded.updated_at
                """,
                (
                    local_date.isoformat(),
                    all_temperature_avg,
                    all_temperature_samples,
                    datetime.now(
                        timezone.utc
                    ).isoformat()
                )
            )

            conn.commit()

        finally:

            conn.close()

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

    # feeder_time is cumulative. A positive difference between
    # consecutive feeder_time samples identifies a real interval
    # where pellets were fed.
    #
    # Temperature and burner output are stored only for those
    # feeder-active timestamps. Idle/standby periods therefore
    # cannot reduce the averages shown in pellet-consumption charts.

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
                SELECT
                    timestamp,
                    parameter,
                    value
                FROM measurements
                WHERE timestamp > ?
                  AND timestamp <= ?
                  AND parameter IN (
                      'feeder_time',
                      'outside_temp',
                      'power',
                      'power_kW'
                  )
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
    active_timestamps = set()
    metrics_by_timestamp = {}

    for row in rows:

        timestamp = row["timestamp"]
        parameter = row["parameter"]
        value = float(row["value"])

        if parameter == "feeder_time":

            delta = (
                value -
                previous_value
            )

            if delta > 0:

                feeder_seconds += delta
                active_timestamps.add(
                    timestamp
                )

            previous_value = value

        else:

            metrics_by_timestamp.setdefault(
                timestamp,
                {}
            )[parameter] = value

    outside_values = []
    power_values = []
    power_kw_values = []

    for timestamp in active_timestamps:

        values = metrics_by_timestamp.get(
            timestamp,
            {}
        )

        if "outside_temp" in values:
            outside_values.append(
                values["outside_temp"]
            )

        if "power" in values:
            power_values.append(
                values["power"]
            )

        if "power_kW" in values:
            power_kw_values.append(
                values["power_kW"]
            )

    def avg(values):

        if not values:
            return None

        return (
            sum(values) /
            len(values)
        )

    outside_temp_avg = avg(
        outside_values
    )

    power_avg = avg(
        power_values
    )

    power_max = (
        max(power_values)
        if power_values
        else None
    )

    power_kw_avg = avg(
        power_kw_values
    )

    power_kw_max = (
        max(power_kw_values)
        if power_kw_values
        else None
    )

    kg = (
        feeder_seconds *
        kg_per_second
    )

    db.upsert_pellet_hour(
        hour_start=start_iso,
        feeder_seconds=feeder_seconds,
        kg=kg,
        outside_temp_avg=outside_temp_avg,
        outside_temp_samples=len(
            outside_values
        ),
        power_avg=power_avg,
        power_samples=len(
            power_values
        ),
        power_max=power_max,
        power_kw_avg=power_kw_avg,
        power_kw_samples=len(
            power_kw_values
        ),
        power_kw_max=power_kw_max
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
        "Pellet hour stored: %s -> %.3f kg "
        "(%.0f feeder sec, %d active samples)",
        start_iso,
        kg,
        feeder_seconds,
        len(active_timestamps)
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
# HOURLY HISTORY CACHE
# ============================================================

def history_hourly_loop():

    last_completed_hour = None

    while True:

        try:
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

            hour_key = hour_start.isoformat()

            if hour_key != last_completed_hour:

                stored = db.rebuild_history_hour(
                    hour_start,
                    hour_end
                )

                if stored:
                    last_completed_hour = hour_key

                    logger.info(
                        "Hourly history stored: %s (%d parameters)",
                        hour_key,
                        stored
                    )

        except Exception:
            logger.exception(
                "Hourly history writer error"
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
                now - timedelta(days=7)
            ).isoformat()

            history_deleted = db.cleanup_measurement_history(
                history_cutoff
            )

            logger.info(
                "7-day raw history cleanup: %d rows deleted",
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

                # Indoor temperature is supplied by the optional
                # Home Assistant integration rather than the burner.
                try:
                    ha_status = get_home_assistant_temperature()

                    if (
                        ha_status.get("connected")
                        and ha_status.get("temperature") is not None
                    ):
                        history_values["indoor_temp"] = float(
                            ha_status["temperature"]
                        )

                except Exception:
                    logger.exception(
                        "Home Assistant indoor temperature history error"
                    )

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


@app.get("/pellet-system.png")
def pellet_system_image():
    return FileResponse(
        str(BASE_DIR / "web" / "pellet-system.png"),
        media_type="image/png"
    )


@app.get("/burner-card-image.png")
def burner_card_image():
    return FileResponse(
        str(BASE_DIR / "web" / "burner-card-image.png"),
        media_type="image/png"
    )


@app.get("/woody-icon.svg")
def woody_icon():
    return FileResponse(
        str(BASE_DIR / "web" / "woody-icon.svg"),
        media_type="image/svg+xml"
    )


@app.get("/manifest.webmanifest")
def woody_manifest():
    return FileResponse(
        str(BASE_DIR / "web" / "manifest.webmanifest"),
        media_type="application/manifest+json"
    )


# ============================================================
# API: WOODY MONITOR BACKUP
# ============================================================

BACKUP_CONFIG_FILES = (
    "advanced_timer.json",
    "cleaning_settings.json",
    "feeder_settings.json",
    "silo_settings.json",
    "timezone_settings.json",
    "weather_compensation.json",
)



# ============================================================
# API: WOODY MONITOR RESTORE VALIDATION
# ============================================================

RESTORE_REQUIRED_TABLES = {
    "measurements",
    "history_hourly",
    "pellet_consumption_hourly",
    "pellet_prices",
    "daily_stats",
    "activity_log",
}

RESTORE_MAX_UPLOAD_BYTES = 250 * 1024 * 1024
RESTORE_MAX_DATABASE_BYTES = 1024 * 1024 * 1024


@app.post("/api/v1/system/restore/validate")
async def validate_woody_monitor_restore(
    backup: UploadFile = File(...)
):
    """
    Validate a Woody Monitor backup without changing any
    existing database or configuration files.
    """

    filename = backup.filename or ""

    if not filename.lower().endswith(".zip"):
        raise HTTPException(
            status_code=400,
            detail="Backup file must be a ZIP file"
        )

    restore_dir = Path(tempfile.mkdtemp(
        prefix="woody-monitor-restore-validation-"
    ))

    archive_path = restore_dir / "backup.zip"
    extracted_db = restore_dir / "woody.db"

    try:

        # ----------------------------------------------------
        # Store uploaded ZIP with a strict size limit
        # ----------------------------------------------------

        total_size = 0

        with archive_path.open("wb") as output:

            while True:

                chunk = await backup.read(1024 * 1024)

                if not chunk:
                    break

                total_size += len(chunk)

                if total_size > RESTORE_MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="Backup file is too large"
                    )

                output.write(chunk)

        if total_size == 0:
            raise HTTPException(
                status_code=400,
                detail="Backup file is empty"
            )

        # ----------------------------------------------------
        # Open ZIP and validate structure
        # ----------------------------------------------------

        try:
            archive = zipfile.ZipFile(
                archive_path,
                "r"
            )
        except zipfile.BadZipFile:
            raise HTTPException(
                status_code=400,
                detail="Invalid or damaged ZIP file"
            )

        with archive:

            names = archive.namelist()

            if len(names) != len(set(names)):
                raise HTTPException(
                    status_code=400,
                    detail="Backup contains duplicate file names"
                )

            allowed_files = {
                "woody.db",
                "backup_manifest.json",
                *BACKUP_CONFIG_FILES,
            }

            unexpected = sorted(
                name
                for name in names
                if name not in allowed_files
            )

            if unexpected:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Backup contains unexpected files: " +
                        ", ".join(unexpected)
                    )
                )

            required_files = {
                "woody.db",
                "backup_manifest.json",
            }

            missing_files = sorted(
                required_files - set(names)
            )

            if missing_files:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Backup is missing required files: " +
                        ", ".join(missing_files)
                    )
                )

            # Reject suspicious paths even if future backup
            # formats allow more files.
            for info in archive.infolist():

                name_path = Path(info.filename)

                if (
                    name_path.is_absolute()
                    or ".." in name_path.parts
                ):
                    raise HTTPException(
                        status_code=400,
                        detail="Backup contains an unsafe file path"
                    )

            # ------------------------------------------------
            # Validate manifest
            # ------------------------------------------------

            try:
                manifest = json.loads(
                    archive.read(
                        "backup_manifest.json"
                    ).decode("utf-8")
                )
            except Exception:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid backup manifest"
                )

            if manifest.get("format") != "woody-monitor-backup":
                raise HTTPException(
                    status_code=400,
                    detail="Not a Woody Monitor backup"
                )

            if manifest.get("format_version") != 1:
                raise HTTPException(
                    status_code=400,
                    detail="Unsupported backup format version"
                )

            if manifest.get("database") != "woody.db":
                raise HTTPException(
                    status_code=400,
                    detail="Invalid database entry in backup manifest"
                )

            # ------------------------------------------------
            # Validate database size before extracting
            # ------------------------------------------------

            db_info = archive.getinfo("woody.db")

            if db_info.file_size <= 0:
                raise HTTPException(
                    status_code=400,
                    detail="Backup database is empty"
                )

            if db_info.file_size > RESTORE_MAX_DATABASE_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="Backup database is too large"
                )

            # ------------------------------------------------
            # Extract database only
            # ------------------------------------------------

            with archive.open("woody.db") as source:
                with extracted_db.open("wb") as destination:

                    copied = 0

                    while True:

                        chunk = source.read(1024 * 1024)

                        if not chunk:
                            break

                        copied += len(chunk)

                        if copied > RESTORE_MAX_DATABASE_BYTES:
                            raise HTTPException(
                                status_code=413,
                                detail="Backup database is too large"
                            )

                        destination.write(chunk)

            # ------------------------------------------------
            # Validate SQLite database
            # ------------------------------------------------

            try:

                connection = sqlite3.connect(
                    str(extracted_db)
                )

                try:

                    integrity = connection.execute(
                        "PRAGMA integrity_check"
                    ).fetchone()

                    if (
                        not integrity
                        or integrity[0] != "ok"
                    ):
                        raise HTTPException(
                            status_code=400,
                            detail="Backup database integrity check failed"
                        )

                    tables = {
                        row[0]
                        for row in connection.execute(
                            """
                            SELECT name
                            FROM sqlite_master
                            WHERE type='table'
                            """
                        )
                    }

                finally:
                    connection.close()

            except HTTPException:
                raise

            except sqlite3.DatabaseError:
                raise HTTPException(
                    status_code=400,
                    detail="Backup does not contain a valid SQLite database"
                )

            missing_tables = sorted(
                RESTORE_REQUIRED_TABLES - tables
            )

            if missing_tables:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Backup database is missing required tables: " +
                        ", ".join(missing_tables)
                    )
                )

            # ------------------------------------------------
            # Validate JSON configuration files
            # ------------------------------------------------

            config_files = []

            for config_name in BACKUP_CONFIG_FILES:

                if config_name not in names:
                    continue

                try:
                    json.loads(
                        archive.read(
                            config_name
                        ).decode("utf-8")
                    )
                except Exception:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "Invalid JSON configuration file: " +
                            config_name
                        )
                    )

                config_files.append(config_name)

        return {
            "valid": True,
            "filename": filename,
            "upload_size_bytes": total_size,
            "database_size_bytes": db_info.file_size,
            "application_version": manifest.get(
                "application_version"
            ),
            "created": manifest.get("created"),
            "config_files": config_files,
            "credentials_included": False,
            "message": (
                "Backup is valid. No files have been restored."
            )
        }

    except HTTPException:
        raise

    except Exception as exc:

        logger.exception(
            "Could not validate Woody Monitor backup"
        )

        raise HTTPException(
            status_code=500,
            detail=f"Could not validate backup: {exc}"
        )

    finally:

        try:
            await backup.close()
        except Exception:
            pass

        # Validation must leave no uploaded backup behind.
        shutil.rmtree(
            restore_dir,
            ignore_errors=True
        )



def cleanup_woody_backup_directory(path):
    """
    Remove temporary backup files after FileResponse has
    completed sending the archive.
    """
    try:
        shutil.rmtree(
            str(path),
            ignore_errors=True
        )
    except Exception:
        logger.exception(
            "Could not remove temporary backup directory"
        )


@app.get("/api/v1/system/backup")
def download_woody_monitor_backup():
    """
    Create a consistent Woody Monitor backup.

    Sensitive files such as .env and home_assistant.json are
    deliberately excluded.
    """

    source_db = BASE_DIR / "data" / "woody.db"

    if not source_db.exists():
        raise HTTPException(
            status_code=500,
            detail="Woody Monitor database not found"
        )

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    backup_dir = Path(tempfile.mkdtemp(
        prefix="woody-monitor-backup-"
    ))

    backup_db = backup_dir / "woody.db"

    try:
        # SQLite's backup API creates a transactionally
        # consistent copy while Woody Monitor remains online.
        source = sqlite3.connect(
            str(source_db),
            timeout=30
        )

        destination = sqlite3.connect(
            str(backup_db)
        )

        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()

        # Verify the copied database before packaging it.
        verify = sqlite3.connect(str(backup_db))

        try:
            result = verify.execute(
                "PRAGMA integrity_check"
            ).fetchone()

            if not result or result[0] != "ok":
                raise RuntimeError(
                    "Database integrity check failed"
                )

        finally:
            verify.close()

        manifest = {
            "format": "woody-monitor-backup",
            "format_version": 1,
            "application": "Woody Monitor",
            "application_version": APP_VERSION,
            "created": datetime.now(timezone.utc).isoformat(),
            "database": "woody.db",
            "credentials_included": False,
            "excluded_sensitive_files": [
                ".env",
                "home_assistant.json"
            ]
        }

        manifest_path = backup_dir / "backup_manifest.json"

        with manifest_path.open(
            "w",
            encoding="utf-8"
        ) as f:
            json.dump(
                manifest,
                f,
                indent=2
            )

        archive_path = (
            backup_dir /
            f"woody-monitor-backup-{timestamp}.zip"
        )

        with zipfile.ZipFile(
            archive_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6
        ) as archive:

            archive.write(
                backup_db,
                arcname="woody.db"
            )

            archive.write(
                manifest_path,
                arcname="backup_manifest.json"
            )

            for filename in BACKUP_CONFIG_FILES:

                config_path = BASE_DIR / "data" / filename

                if config_path.exists():
                    archive.write(
                        config_path,
                        arcname=filename
                    )

        logger.info(
            "Woody Monitor backup created: %s",
            archive_path.name
        )

        return FileResponse(
            str(archive_path),
            media_type="application/zip",
            filename=archive_path.name,
            background=BackgroundTask(
                cleanup_woody_backup_directory,
                backup_dir
            )
        )

    except HTTPException:
        raise

    except Exception as exc:

        logger.exception(
            "Could not create Woody Monitor backup"
        )

        raise HTTPException(
            status_code=500,
            detail=f"Could not create backup: {exc}"
        )



# ============================================================
# API: RESTORE WOODY MONITOR BACKUP
# ============================================================

@app.post("/api/v1/system/restore")
async def restore_woody_monitor_backup(
    backup: UploadFile = File(...)
):
    """
    Validate and restore a Woody Monitor backup.

    Existing data is copied to an emergency backup before any
    active files are replaced. Sensitive credential files are
    never restored.
    """

    filename = backup.filename or ""

    if not filename.lower().endswith(".zip"):
        raise HTTPException(
            status_code=400,
            detail="Backup file must be a ZIP file"
        )

    data_dir = BASE_DIR / "data"

    restore_dir = Path(tempfile.mkdtemp(
        prefix="woody-monitor-restore-"
    ))

    archive_path = restore_dir / "backup.zip"
    staging_dir = restore_dir / "staging"

    staging_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    emergency_root = (
        BASE_DIR /
        "data" /
        "restore-emergency"
    )

    timestamp = datetime.now().strftime(
        "%Y%m%d-%H%M%S"
    )

    emergency_dir = (
        emergency_root /
        timestamp
    )

    try:

        # ----------------------------------------------------
        # Receive uploaded ZIP
        # ----------------------------------------------------

        total_size = 0

        with archive_path.open("wb") as output:

            while True:

                chunk = await backup.read(
                    1024 * 1024
                )

                if not chunk:
                    break

                total_size += len(chunk)

                if total_size > RESTORE_MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="Backup file is too large"
                    )

                output.write(chunk)

        if total_size == 0:
            raise HTTPException(
                status_code=400,
                detail="Backup file is empty"
            )

        # ----------------------------------------------------
        # Validate ZIP
        # ----------------------------------------------------

        try:
            archive = zipfile.ZipFile(
                archive_path,
                "r"
            )
        except zipfile.BadZipFile:
            raise HTTPException(
                status_code=400,
                detail="Invalid or damaged ZIP file"
            )

        with archive:

            names = archive.namelist()

            if len(names) != len(set(names)):
                raise HTTPException(
                    status_code=400,
                    detail="Backup contains duplicate file names"
                )

            allowed_files = {
                "woody.db",
                "backup_manifest.json",
                *BACKUP_CONFIG_FILES,
            }

            unexpected = sorted(
                name
                for name in names
                if name not in allowed_files
            )

            if unexpected:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Backup contains unexpected files: " +
                        ", ".join(unexpected)
                    )
                )

            required_files = {
                "woody.db",
                "backup_manifest.json",
            }

            missing_files = sorted(
                required_files - set(names)
            )

            if missing_files:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Backup is missing required files: " +
                        ", ".join(missing_files)
                    )
                )

            for info in archive.infolist():

                name_path = Path(info.filename)

                if (
                    name_path.is_absolute()
                    or ".." in name_path.parts
                ):
                    raise HTTPException(
                        status_code=400,
                        detail="Backup contains an unsafe file path"
                    )

            # ------------------------------------------------
            # Manifest
            # ------------------------------------------------

            try:
                manifest = json.loads(
                    archive.read(
                        "backup_manifest.json"
                    ).decode("utf-8")
                )
            except Exception:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid backup manifest"
                )

            if manifest.get("format") != "woody-monitor-backup":
                raise HTTPException(
                    status_code=400,
                    detail="Not a Woody Monitor backup"
                )

            if manifest.get("format_version") != 1:
                raise HTTPException(
                    status_code=400,
                    detail="Unsupported backup format version"
                )

            if manifest.get("database") != "woody.db":
                raise HTTPException(
                    status_code=400,
                    detail="Invalid database entry in backup manifest"
                )

            # ------------------------------------------------
            # Extract allowed restore files to staging
            # ------------------------------------------------

            files_to_restore = [
                "woody.db"
            ]

            files_to_restore.extend(
                name
                for name in BACKUP_CONFIG_FILES
                if name in names
            )

            for name in files_to_restore:

                info = archive.getinfo(name)

                if (
                    name == "woody.db"
                    and info.file_size > RESTORE_MAX_DATABASE_BYTES
                ):
                    raise HTTPException(
                        status_code=413,
                        detail="Backup database is too large"
                    )

                destination = (
                    staging_dir /
                    name
                )

                copied = 0

                with archive.open(name) as source:
                    with destination.open("wb") as output:

                        while True:

                            chunk = source.read(
                                1024 * 1024
                            )

                            if not chunk:
                                break

                            copied += len(chunk)

                            if (
                                name == "woody.db"
                                and copied >
                                RESTORE_MAX_DATABASE_BYTES
                            ):
                                raise HTTPException(
                                    status_code=413,
                                    detail="Backup database is too large"
                                )

                            output.write(chunk)

            # ------------------------------------------------
            # Validate staged database
            # ------------------------------------------------

            staged_db = (
                staging_dir /
                "woody.db"
            )

            try:

                connection = sqlite3.connect(
                    str(staged_db)
                )

                try:

                    integrity = connection.execute(
                        "PRAGMA integrity_check"
                    ).fetchone()

                    if (
                        not integrity
                        or integrity[0] != "ok"
                    ):
                        raise HTTPException(
                            status_code=400,
                            detail="Backup database integrity check failed"
                        )

                    tables = {
                        row[0]
                        for row in connection.execute(
                            """
                            SELECT name
                            FROM sqlite_master
                            WHERE type='table'
                            """
                        )
                    }

                finally:
                    connection.close()

            except HTTPException:
                raise

            except sqlite3.DatabaseError:
                raise HTTPException(
                    status_code=400,
                    detail="Backup does not contain a valid SQLite database"
                )

            missing_tables = sorted(
                RESTORE_REQUIRED_TABLES - tables
            )

            if missing_tables:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Backup database is missing required tables: " +
                        ", ".join(missing_tables)
                    )
                )

            # ------------------------------------------------
            # Validate staged JSON
            # ------------------------------------------------

            for name in BACKUP_CONFIG_FILES:

                staged_file = (
                    staging_dir /
                    name
                )

                if not staged_file.exists():
                    continue

                try:
                    json.loads(
                        staged_file.read_text(
                            encoding="utf-8"
                        )
                    )
                except Exception:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "Invalid JSON configuration file: " +
                            name
                        )
                    )

        # ----------------------------------------------------
        # Create emergency backup BEFORE replacement
        # ----------------------------------------------------

        emergency_dir.mkdir(
            parents=True,
            exist_ok=False
        )

        current_db = (
            data_dir /
            "woody.db"
        )

        if not current_db.exists():
            raise HTTPException(
                status_code=500,
                detail="Current Woody Monitor database not found"
            )

        emergency_db = (
            emergency_dir /
            "woody.db"
        )

        source = sqlite3.connect(
            str(current_db),
            timeout=30
        )

        destination = sqlite3.connect(
            str(emergency_db)
        )

        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()

        # Verify emergency database before continuing.
        verify = sqlite3.connect(
            str(emergency_db)
        )

        try:

            result = verify.execute(
                "PRAGMA integrity_check"
            ).fetchone()

            if (
                not result
                or result[0] != "ok"
            ):
                raise RuntimeError(
                    "Emergency database backup failed integrity check"
                )

        finally:
            verify.close()

        for name in BACKUP_CONFIG_FILES:

            current_file = (
                data_dir /
                name
            )

            if current_file.exists():
                shutil.copy2(
                    current_file,
                    emergency_dir / name
                )

        # ----------------------------------------------------
        # Replace active files
        # ----------------------------------------------------

        replacement_files = [
            "woody.db"
        ]

        replacement_files.extend(
            name
            for name in BACKUP_CONFIG_FILES
            if (staging_dir / name).exists()
        )

        for name in replacement_files:

            staged_file = (
                staging_dir /
                name
            )

            active_file = (
                data_dir /
                name
            )

            replacement = (
                data_dir /
                f".{name}.restore-new"
            )

            shutil.copy2(
                staged_file,
                replacement
            )

            os.replace(
                replacement,
                active_file
            )

        logger.warning(
            "Woody Monitor restored from backup %s; "
            "emergency backup stored in %s",
            filename,
            emergency_dir
        )

        # ----------------------------------------------------
        # Restart after response has had time to leave server
        # ----------------------------------------------------

        def shutdown_after_restore():
            os._exit(0)

        threading.Timer(
            1.5,
            shutdown_after_restore
        ).start()

        return {
            "ok": True,
            "message": (
                "Backup restored successfully. "
                "Woody Monitor is restarting."
            ),
            "application_version": manifest.get(
                "application_version"
            ),
            "created": manifest.get(
                "created"
            ),
            "emergency_backup": timestamp,
            "credentials_restored": False
        }

    except HTTPException:
        raise

    except Exception as exc:

        logger.exception(
            "Could not restore Woody Monitor backup"
        )

        raise HTTPException(
            status_code=500,
            detail=f"Could not restore backup: {exc}"
        )

    finally:

        try:
            await backup.close()
        except Exception:
            pass

        shutil.rmtree(
            restore_dir,
            ignore_errors=True
        )


# ============================================================
# API: RESTART WOODY MONITOR
# ============================================================

@app.post("/api/v1/system/restart")
def restart_woody_monitor():
    """
    Restart Woody Monitor through systemd Restart=always.
    Response is returned before the process exits.
    """

    def shutdown():
        os._exit(0)

    threading.Timer(0.8, shutdown).start()

    return {
        "ok": True,
        "message": "Woody Monitor restarting"
    }


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



@app.get("/api/v1/settings/mqtt")
def get_mqtt_settings_api():

    with mqtt_settings_lock:

        return {
            "enabled": bool(MQTT_ENABLED),
            "broker": MQTT_BROKER,
            "port": MQTT_PORT,
            "username": MQTT_USERNAME,
            "password_set": bool(
                MQTT_PASSWORD
            ),
            "topic": MQTT_TOPIC,
            "connected": bool(
                mqtt_connected
            )
        }


@app.post("/api/v1/settings/mqtt")
async def set_mqtt_settings_api(
    request: Request
):

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON request"
        )

    enabled = bool(
        data.get("enabled", True)
    )

    broker = str(
        data.get("broker", "")
    )

    username = str(
        data.get("username", "")
    )

    password = str(
        data.get("password", "")
    )

    topic = str(
        data.get("topic", "woodymonitor")
    )

    try:
        port = int(
            data.get("port", 1883)
        )
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="MQTT port must be a number"
        )

    current_password = MQTT_PASSWORD

    # Blank password means keep the existing password.
    new_password = (
        password
        if password
        else current_password
    )

    try:

        mqtt_apply_settings(
            enabled,
            broker,
            port,
            username,
            new_password,
            topic
        )

    except ValueError as error:

        raise HTTPException(
            status_code=400,
            detail=str(error)
        )

    except Exception:

        logger.exception(
            "Could not apply MQTT settings"
        )

        raise HTTPException(
            status_code=500,
            detail="Could not apply MQTT settings"
        )

    try:
        db.add_activity(
            "SETTING",
            "MQTT",
            "MQTT connection settings updated",
            response="OK"
        )
    except Exception:
        pass

    return {
        "enabled": bool(MQTT_ENABLED),
        "broker": MQTT_BROKER,
        "port": MQTT_PORT,
        "username": MQTT_USERNAME,
        "password_set": bool(
            MQTT_PASSWORD
        ),
        "topic": MQTT_TOPIC,
        "connected": bool(
            mqtt_connected
        )
    }


@app.post("/api/v1/settings/mqtt/test")
async def test_mqtt_settings_api(
    request: Request
):

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON request"
        )

    clean_broker = str(
        data.get("broker", "")
    ).strip()

    username = str(
        data.get("username", "")
    )

    password = str(
        data.get("password", "")
    )

    try:
        port = int(
            data.get("port", 1883)
        )
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="MQTT port must be a number"
        )

    if not clean_broker:
        raise HTTPException(
            status_code=400,
            detail="MQTT broker is required"
        )

    if not 1 <= port <= 65535:
        raise HTTPException(
            status_code=400,
            detail="Invalid MQTT port"
        )

    # Blank password means test using the saved password.
    test_password = (
        password
        if password
        else MQTT_PASSWORD
    )

    connected_event = threading.Event()

    result = {
        "connected": False,
        "error": None
    }

    test_client = None

    def on_test_connect(
        client,
        userdata,
        flags,
        reason_code,
        properties
    ):

        if reason_code.is_failure:
            result["error"] = str(
                reason_code
            )
        else:
            result["connected"] = True

        connected_event.set()

    try:

        test_client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            protocol=mqtt.MQTTv5
        )

        if username or test_password:

            test_client.username_pw_set(
                username,
                test_password
            )

        test_client.on_connect = (
            on_test_connect
        )

        test_client.connect(
            clean_broker,
            port,
            10
        )

        test_client.loop_start()

        if not connected_event.wait(5):

            result["error"] = (
                "Connection timed out"
            )

    except Exception as error:

        result["error"] = str(error)

    finally:

        if test_client is not None:

            try:
                test_client.disconnect()
            except Exception:
                pass

            try:
                test_client.loop_stop()
            except Exception:
                pass

    return {
        "connected": bool(
            result["connected"]
        ),
        "error": result["error"]
    }


@app.get("/api/v1/settings/home-assistant")
def get_home_assistant_settings_api():

    with home_assistant_lock:

        url = home_assistant_settings[
            "url"
        ]

        entity = home_assistant_settings[
            "indoor_temperature_entity"
        ]

        token_configured = bool(
            home_assistant_settings[
                "token"
            ]
        )

    status = get_home_assistant_temperature()

    return {
        "url": url,
        "indoor_temperature_entity": entity,
        "token_configured": token_configured,
        "status": status
    }


@app.post("/api/v1/settings/home-assistant")
def set_home_assistant_settings_api(
    url: str = Query(...),
    indoor_temperature_entity: str = Query(...),
    token: str = Query("")
):

    clean_url = (
        str(url)
        .strip()
        .rstrip("/")
    )

    clean_entity = (
        str(indoor_temperature_entity)
        .strip()
    )

    clean_token = str(
        token
    ).strip()

    if clean_url and not (
        clean_url.startswith("http://")
        or clean_url.startswith("https://")
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Home Assistant URL must start "
                "with http:// or https://"
            )
        )

    with home_assistant_lock:

        home_assistant_settings[
            "url"
        ] = clean_url

        home_assistant_settings[
            "indoor_temperature_entity"
        ] = clean_entity

        # Blank token means keep the existing token.
        if clean_token:
            home_assistant_settings[
                "token"
            ] = clean_token

    save_home_assistant_settings()

    status = get_home_assistant_temperature()

    try:
        db.add_activity(
            "SETTING",
            "Home Assistant",
            "Home Assistant integration updated"
        )
    except Exception:
        pass

    return {
        "url": clean_url,
        "indoor_temperature_entity":
            clean_entity,
        "token_configured": bool(
            home_assistant_settings[
                "token"
            ]
        ),
        "status": status
    }


@app.post("/api/v1/settings/home-assistant/test")
def test_home_assistant_api():

    return get_home_assistant_temperature()


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
# API: CLEANING
# ============================================================

@app.get("/api/v1/settings/cleaning")
def get_cleaning_settings_api():

    return get_cleaning_settings()


@app.post("/api/v1/settings/cleaning")
def set_cleaning_interval(
    interval_days: int = Query(..., ge=1, le=3650)
):

    with cleaning_settings_lock:

        cleaning_settings["interval_days"] = interval_days

        save_cleaning_settings()

    logger.info(
        "Cleaning interval changed: %d days",
        interval_days
    )

    return get_cleaning_settings()


@app.post("/api/v1/cleaning")
def register_cleaning():

    with cleaning_settings_lock:

        cleaning_settings["last_cleaning"] = (
            datetime.now(timezone.utc).isoformat()
        )

        save_cleaning_settings()

    logger.info(
        "Burner cleaning registered: %s",
        cleaning_settings["last_cleaning"]
    )

    return get_cleaning_settings()


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
# ADVANCED TIMER ENGINE
# ============================================================

advanced_timer_thread_started = False


def advanced_timer_burner_is_running():

    with state_lock:
        values = dict(live_data.get("values", {}))

    mode = str(
        values.get("mode", "")
    ).strip().lower()

    if not mode:
        return None

    stopped_modes = {
        "stopped",
        "shut off",
        "summer stop"
    }

    return mode not in stopped_modes


def execute_advanced_timer_action(action):

    global burner

    with advanced_timer_lock:
        test_mode = bool(
            advanced_timer_settings.get(
                "test_mode",
                True
            )
        )

    commands = {
        "start": "burner_on",
        "stop": "burner_off"
    }

    command = commands.get(
        str(action).lower()
    )

    if command is None:
        return False

    if test_mode:

        logger.info(
            "TEST MODE: Advanced timer would send: %s",
            command
        )

        db.add_activity(
            "TEST",
            "Advanced Timer Test Mode",
            f"Would send: {command}"
        )

        return True

    if burner is None:
        logger.warning(
            "Advanced timer: controller not connected"
        )
        return False

    try:

        database = burner.getDataBase()

        if command not in database:
            logger.warning(
                "Advanced timer: command not supported: %s",
                command
            )
            return False

        response = burner.setItem(
            command,
            "0"
        )

        if str(response) != "OK":

            logger.warning(
                "Advanced timer command rejected: %s -> %s",
                command,
                response
            )

            return False

        logger.info(
            "Advanced timer sent: %s",
            command
        )

        db.add_activity(
            "CONTROLLER",
            "Advanced timer " + action.lower(),
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

        return True

    except Exception:

        logger.exception(
            "Advanced timer command failed: %s",
            command
        )

        return False


def advanced_timer_loop():

    global advanced_timer_thread_started

    if advanced_timer_thread_started:
        return

    advanced_timer_thread_started = True

    logger.info(
        "Advanced timer engine started"
    )

    last_checked_minute = None
    last_test_decision = None

    while True:

        try:

            now = local_now()

            minute_key = (
                now.strftime(
                    "%Y-%m-%d-%H-%M"
                )
            )

            if minute_key == last_checked_minute:

                time.sleep(2)
                continue

            with advanced_timer_lock:

                enabled = bool(
                    advanced_timer_settings[
                        "enabled"
                    ]
                )

                test_mode = bool(
                    advanced_timer_settings.get(
                        "test_mode",
                        True
                    )
                )

                day = (
                    ADVANCED_TIMER_DAYS[
                        now.weekday()
                    ]
                )

                timer_desired_on = bool(
                    advanced_timer_settings[
                        "schedule"
                    ][day][now.hour]
                )

            if not enabled:

                last_checked_minute = None
                last_test_decision = None

                time.sleep(2)
                continue

            desired_on = (
                timer_desired_on
            )

            decision_source = (
                "timer"
            )

            with weather_compensation_lock:

                weather_enabled = bool(
                    weather_compensation_settings[
                        "enabled"
                    ]
                )

            weather_result = None

            if weather_enabled:

                weather_result = (
                    calculate_weather_compensation()
                )

                if weather_result.get(
                    "available"
                ):

                    weather_desired = bool(
                        weather_result[
                            "weather_desired_on_now"
                        ]
                    )

                    if weather_result.get(
                        "preview_mode",
                        True
                    ):

                        # Preview:
                        # calculate and display Weather
                        # Compensation, but keep actual
                        # burner decision on Advanced Timer.
                        desired_on = (
                            timer_desired_on
                        )

                        decision_source = (
                            "weather-preview"
                        )

                    else:

                        desired_on = (
                            weather_desired
                        )

                        decision_source = (
                            "weather"
                        )

                else:

                    # Missing weather data must never
                    # disable the normal timer.
                    desired_on = (
                        timer_desired_on
                    )

                    decision_source = (
                        "weather-unavailable-timer"
                    )

            running = (
                advanced_timer_burner_is_running()
            )

            if running is None:

                last_checked_minute = (
                    minute_key
                )

                time.sleep(2)
                continue

            action = (
                "start"
                if desired_on
                else "stop"
            )

            should_command = (
                (desired_on and not running)
                or
                (
                    not desired_on
                    and running
                )
            )

            # Test Mode should only log a simulated
            # command when the desired state changes.
            if (
                test_mode
                and
                last_test_decision
                is not None
                and
                last_test_decision
                == desired_on
            ):
                should_command = False

            success = True

            if should_command:

                success = (
                    execute_advanced_timer_action(
                        action
                    )
                )

                if success:

                    with advanced_timer_lock:

                        advanced_timer_settings[
                            "last_slot"
                        ] = minute_key

                        advanced_timer_settings[
                            "last_action"
                        ] = action

                        save_advanced_timer_settings()

            if test_mode:
                last_test_decision = (
                    desired_on
                )

            last_checked_minute = (
                minute_key
            )

            logger.info(
                "Advanced timer: %s %02d:%02d "
                "timer=%s desired=%s "
                "source=%s running=%s action=%s",
                day,
                now.hour,
                now.minute,
                timer_desired_on,
                desired_on,
                decision_source,
                running,
                (
                    action
                    if should_command
                    else "none"
                )
            )

        except Exception:

            logger.exception(
                "Advanced timer loop error"
            )

        time.sleep(2)


# ============================================================
# WEATHER COMPENSATION
# ============================================================

WEATHER_COMPENSATION_FILE = str(
    BASE_DIR / "data" / "weather_compensation.json"
)

weather_compensation_lock = threading.Lock()


def default_weather_curves():

    return [
        {
            "to_temp": 12.0,
            "before_minutes": 15,
            "after_minutes": 15,
            "full_day": False
        },
        {
            "to_temp": 8.0,
            "before_minutes": 30,
            "after_minutes": 30,
            "full_day": False
        },
        {
            "to_temp": 4.0,
            "before_minutes": 60,
            "after_minutes": 60,
            "full_day": False
        },
        {
            "to_temp": 0.0,
            "before_minutes": 120,
            "after_minutes": 120,
            "full_day": False
        },
        {
            "to_temp": -5.0,
            "before_minutes": 0,
            "after_minutes": 0,
            "full_day": True
        }
    ]


weather_compensation_settings = {
    "enabled": False,

    # Weather control starts safely in preview.
    # Advanced Timer still controls the burner until
    # preview_mode is explicitly disabled.
    "preview_mode": True,

    "history_hours": 6.0,

    # Current outdoor temperature and 6-hour average
    # have equal weight.
    "history_weight": 0.50,

    "curves": default_weather_curves()
}


def validate_weather_curves(curves):

    if (
        not isinstance(curves, list)
        or len(curves) != 5
    ):
        return False

    parsed = []

    try:

        for index, curve in enumerate(curves):

            threshold = float(
                curve["to_temp"]
            )

            legacy = int(
                curve.get(
                    "extension_minutes",
                    0
                )
            )

            before_minutes = int(
                curve.get(
                    "before_minutes",
                    legacy
                )
            )

            after_minutes = int(
                curve.get(
                    "after_minutes",
                    legacy
                )
            )

            full_day = bool(
                curve.get(
                    "full_day",
                    index == 4
                )
            )

            if not 0 <= before_minutes <= 720:
                return False

            if not 0 <= after_minutes <= 720:
                return False

            if index == 4:

                full_day = True
                before_minutes = 0
                after_minutes = 0

            parsed.append({
                "to_temp": threshold,
                "before_minutes": before_minutes,
                "after_minutes": after_minutes,
                "full_day": full_day
            })

    except Exception:
        return False

    thresholds = [
        curve["to_temp"]
        for curve in parsed
    ]

    if not all(
        thresholds[index] >
        thresholds[index + 1]
        for index in range(4)
    ):
        return False

    return parsed



def load_weather_compensation_settings():

    try:

        path = Path(
            WEATHER_COMPENSATION_FILE
        )

        if not path.exists():
            return

        with path.open("r") as f:
            data = json.load(f)

        weather_compensation_settings[
            "enabled"
        ] = bool(
            data.get("enabled", False)
        )

        curves = data.get("curves")

        parsed = validate_weather_curves(
            curves
        )

        if parsed:

            # New time-extension format.
            weather_compensation_settings[
                "curves"
            ] = parsed

            weather_compensation_settings[
                "preview_mode"
            ] = bool(
                data.get(
                    "preview_mode",
                    True
                )
            )

        else:

            # Existing installation used the previous
            # "hours per day" weather model.
            #
            # Do not allow that old configuration to
            # activate the new logic automatically.
            weather_compensation_settings[
                "curves"
            ] = default_weather_curves()

            weather_compensation_settings[
                "preview_mode"
            ] = True

            logger.info(
                "Old Weather Compensation configuration "
                "converted to time-extension model in Preview Mode"
            )

        # This model intentionally always uses:
        # current temp + 6-hour average, 50/50.
        weather_compensation_settings[
            "history_hours"
        ] = 6.0

        weather_compensation_settings[
            "history_weight"
        ] = 0.50

        logger.info(
            "Loaded Weather Compensation: "
            "enabled=%s preview=%s",
            weather_compensation_settings[
                "enabled"
            ],
            weather_compensation_settings[
                "preview_mode"
            ]
        )

    except Exception:

        logger.exception(
            "Could not load Weather Compensation settings"
        )


def save_weather_compensation_settings():

    path = Path(
        WEATHER_COMPENSATION_FILE
    )

    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temporary = path.with_suffix(
        ".tmp"
    )

    with temporary.open("w") as f:

        json.dump(
            weather_compensation_settings,
            f,
            indent=2
        )

    temporary.replace(path)


def get_weather_temperature_data():

    end_dt = datetime.now(
        timezone.utc
    )

    start_dt = (
        end_dt -
        timedelta(hours=6)
    )

    rows = db.get_history(
        ["outside_temp"],
        start_dt.isoformat(),
        end_dt.isoformat(),
        bucket_seconds=900
    )

    values = []

    for row in rows:

        try:
            values.append(
                float(
                    row["value"]
                )
            )

        except (
            TypeError,
            ValueError,
            KeyError
        ):
            pass

    average_temp = (
        sum(values) / len(values)
        if values
        else None
    )

    current_temp = None

    try:

        with state_lock:

            current_value = (
                live_data
                .get("values", {})
                .get("outside_temp")
            )

        if current_value is not None:
            current_temp = float(
                current_value
            )

    except (
        TypeError,
        ValueError,
        AttributeError
    ):
        current_temp = None

    if (
        current_temp is None
        and values
    ):
        current_temp = values[-1]

    return (
        current_temp,
        average_temp,
        len(values)
    )


def weather_control_temperature(
    current_temp,
    average_temp
):

    if (
        current_temp is None
        and average_temp is None
    ):
        return None

    if current_temp is None:
        return average_temp

    if average_temp is None:
        return current_temp

    # Equal combination requested:
    #
    # Weather temperature =
    # (current + 6-hour average) / 2

    return (
        current_temp +
        average_temp
    ) / 2.0


def weather_active_curve(
    effective_temp,
    curves
):

    if effective_temp is None:
        return None

    active = None

    # Curves become progressively colder.
    # At -7 C, for example, all thresholds match,
    # therefore Curve 5 becomes the final selection.

    for index, curve in enumerate(
        curves,
        start=1
    ):

        if effective_temp <= float(
            curve["to_temp"]
        ):
            active = index

    return active


def timer_schedule_on_at(
    moment,
    timer
):

    day = ADVANCED_TIMER_DAYS[
        moment.weekday()
    ]

    return bool(
        timer["schedule"][
            day
        ][moment.hour]
    )


def timer_schedule_extended_on_at(
    moment,
    timer,
    before_minutes=0,
    after_minutes=0,
    full_day=False
):

    if full_day:
        return True

    before_minutes = max(
        0,
        min(
            720,
            int(before_minutes or 0)
        )
    )

    after_minutes = max(
        0,
        min(
            720,
            int(after_minutes or 0)
        )
    )

    if (
        before_minutes == 0
        and after_minutes == 0
    ):
        return timer_schedule_on_at(
            moment,
            timer
        )

    midnight = moment.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0
    )

    # Check neighbouring days because an extension
    # may cross midnight.
    for day_offset in (-1, 0, 1):

        day_start = (
            midnight +
            timedelta(days=day_offset)
        )

        day_name = (
            ADVANCED_TIMER_DAYS[
                day_start.weekday()
            ]
        )

        schedule = timer[
            "schedule"
        ][day_name]

        period_start = None

        # Build contiguous timer periods.
        # Example 08 + 09 becomes one period 08:00-10:00.
        for hour in range(25):

            enabled = (
                bool(schedule[hour])
                if hour < 24
                else False
            )

            if (
                enabled
                and period_start is None
            ):
                period_start = hour

            if (
                not enabled
                and period_start is not None
            ):

                normal_start = (
                    day_start +
                    timedelta(
                        hours=period_start
                    )
                )

                normal_end = (
                    day_start +
                    timedelta(hours=hour)
                )

                extended_start = (
                    normal_start -
                    timedelta(
                        minutes=before_minutes
                    )
                )

                extended_end = (
                    normal_end +
                    timedelta(
                        minutes=after_minutes
                    )
                )

                if (
                    extended_start <= moment <
                    extended_end
                ):
                    return True

                period_start = None

    return False



def calculate_weather_compensation():

    with weather_compensation_lock:

        settings = {
            "enabled": bool(
                weather_compensation_settings[
                    "enabled"
                ]
            ),
            "preview_mode": bool(
                weather_compensation_settings[
                    "preview_mode"
                ]
            ),
            "history_hours": 6.0,
            "history_weight": 0.50,
            "curves": [
                dict(curve)
                for curve in
                weather_compensation_settings[
                    "curves"
                ]
            ]
        }

    (
        current_temp,
        average_temp,
        sample_count
    ) = get_weather_temperature_data()

    effective_temp = (
        weather_control_temperature(
            current_temp,
            average_temp
        )
    )

    if effective_temp is None:

        return {
            "available": False,
            "enabled":
                settings["enabled"],
            "preview_mode":
                settings["preview_mode"],
            "reason":
                "No outdoor temperature data",
            "history_hours": 6.0,
            "history_samples":
                sample_count,
            "active_curve": None,
            "active_before_minutes": 0,
            "active_after_minutes": 0,
            "active_extension_minutes": 0,
            "full_day": False
        }

    active_curve = (
        weather_active_curve(
            effective_temp,
            settings["curves"]
        )
    )

    before_minutes = 0
    after_minutes = 0
    full_day = False
    active_range = None

    if active_curve is not None:

        curve = settings[
            "curves"
        ][active_curve - 1]

        before_minutes = int(
            curve.get(
                "before_minutes",
                curve.get(
                    "extension_minutes",
                    0
                )
            )
        )

        after_minutes = int(
            curve.get(
                "after_minutes",
                curve.get(
                    "extension_minutes",
                    0
                )
            )
        )

        full_day = bool(
            curve[
                "full_day"
            ]
        )

        active_range = {
            "to_temp":
                curve["to_temp"],
            "before_minutes":
                before_minutes,
            "after_minutes":
                after_minutes,
            "extension_minutes":
                max(
                    before_minutes,
                    after_minutes
                ),
            "full_day":
                full_day
        }

    timer = (
        get_advanced_timer_settings()
    )

    now = local_now()

    current_day = (
        ADVANCED_TIMER_DAYS[
            now.weekday()
        ]
    )

    timer_allowed_now = (
        timer_schedule_on_at(
            now,
            timer
        )
    )

    weather_desired_on_now = (
        timer_schedule_extended_on_at(
            now,
            timer,
            before_minutes,
            after_minutes,
            full_day=full_day
        )
    )

    # Hour-level representation is retained for
    # compatibility with older frontend/API clients.
    # Exact control uses the minute-level decision above.

    effective_schedule = {}

    for day_offset in range(7):

        reference = (
            now.replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0
            )
            +
            timedelta(
                days=(
                    day_offset -
                    now.weekday()
                )
            )
        )

        day_name = (
            ADVANCED_TIMER_DAYS[
                reference.weekday()
            ]
        )

        effective_schedule[
            day_name
        ] = []

        for hour in range(24):

            sample = (
                reference.replace(
                    hour=hour,
                    minute=30
                )
            )

            effective_schedule[
                day_name
            ].append(
                timer_schedule_extended_on_at(
                    sample,
                    timer,
                    before_minutes,
                    after_minutes,
                    full_day=full_day
                )
            )

    return {
        "available": True,
        "enabled":
            settings["enabled"],
        "preview_mode":
            settings["preview_mode"],

        "current_outside_temp": (
            round(
                current_temp,
                2
            )
            if current_temp is not None
            else None
        ),

        "average_outside_temp": (
            round(
                average_temp,
                2
            )
            if average_temp is not None
            else None
        ),

        "effective_outside_temp":
            round(
                effective_temp,
                2
            ),

        "weather_temperature":
            round(
                effective_temp,
                2
            ),

        "history_hours": 6.0,
        "history_samples":
            sample_count,

        "active_curve":
            active_curve,

        "active_range":
            active_range,

        "active_before_minutes":
            before_minutes,
        "active_after_minutes":
            after_minutes,
        "active_extension_minutes":
            max(
                before_minutes,
                after_minutes
            ),

        "full_day":
            full_day,

        "current_day":
            current_day,

        "current_hour":
            now.hour,

        "current_minute":
            now.minute,

        "timer_allowed_now":
            timer_allowed_now,

        "weather_desired_on_now":
            weather_desired_on_now,

        "preview_decision": (
            "ON"
            if weather_desired_on_now
            else "OFF"
        ),

        "effective_schedule":
            effective_schedule,

        "timezone":
            get_timezone_name(),

        "generated_at":
            now.isoformat()
    }


@app.get(
    "/api/v1/settings/weather-compensation"
)
def get_weather_compensation_api():

    result = (
        calculate_weather_compensation()
    )

    with weather_compensation_lock:

        result["settings"] = {
            "enabled":
                weather_compensation_settings[
                    "enabled"
                ],

            "preview_mode":
                weather_compensation_settings[
                    "preview_mode"
                ],

            "history_hours": 6.0,

            "history_weight": 0.50,

            "curves": [
                dict(curve)
                for curve in
                weather_compensation_settings[
                    "curves"
                ]
            ]
        }

    return result


@app.post(
    "/api/v1/settings/weather-compensation/enabled"
)
def set_weather_compensation_enabled(
    enabled: bool = Query(...)
):

    with weather_compensation_lock:

        weather_compensation_settings[
            "enabled"
        ] = enabled

        save_weather_compensation_settings()

    db.add_activity(
        "SETTING",
        "Weather compensation",
        "Enabled"
        if enabled
        else "Disabled"
    )

    return get_weather_compensation_api()


@app.post(
    "/api/v1/settings/weather-compensation/preview"
)
def set_weather_compensation_preview(
    enabled: bool = Query(...)
):

    with weather_compensation_lock:

        weather_compensation_settings[
            "preview_mode"
        ] = enabled

        save_weather_compensation_settings()

    db.add_activity(
        "SETTING",
        "Weather Compensation Preview",
        "Enabled"
        if enabled
        else "Disabled"
    )

    return get_weather_compensation_api()


@app.post(
    "/api/v1/settings/weather-compensation/config"
)
def set_weather_compensation_config(
    curve1_to: float = Query(...),
    curve1_before: int = Query(..., ge=0, le=720),
    curve1_after: int = Query(..., ge=0, le=720),

    curve2_to: float = Query(...),
    curve2_before: int = Query(..., ge=0, le=720),
    curve2_after: int = Query(..., ge=0, le=720),

    curve3_to: float = Query(...),
    curve3_before: int = Query(..., ge=0, le=720),
    curve3_after: int = Query(..., ge=0, le=720),

    curve4_to: float = Query(...),
    curve4_before: int = Query(..., ge=0, le=720),
    curve4_after: int = Query(..., ge=0, le=720),

    curve5_to: float = Query(...)
):

    limits = [
        float(curve1_to),
        float(curve2_to),
        float(curve3_to),
        float(curve4_to),
        float(curve5_to)
    ]

    if not all(
        limits[index] >
        limits[index + 1]
        for index in range(4)
    ):

        raise HTTPException(
            status_code=400,
            detail=(
                "Temperature thresholds must decrease "
                "from Curve 1 to Curve 5"
            )
        )

    curves = [
        {
            "to_temp": limits[0],
            "before_minutes": int(curve1_before),
            "after_minutes": int(curve1_after),
            "full_day": False
        },
        {
            "to_temp": limits[1],
            "before_minutes": int(curve2_before),
            "after_minutes": int(curve2_after),
            "full_day": False
        },
        {
            "to_temp": limits[2],
            "before_minutes": int(curve3_before),
            "after_minutes": int(curve3_after),
            "full_day": False
        },
        {
            "to_temp": limits[3],
            "before_minutes": int(curve4_before),
            "after_minutes": int(curve4_after),
            "full_day": False
        },
        {
            "to_temp": limits[4],
            "before_minutes": 0,
            "after_minutes": 0,
            "full_day": True
        }
    ]

    with weather_compensation_lock:

        weather_compensation_settings[
            "curves"
        ] = curves

        save_weather_compensation_settings()

    db.add_activity(
        "SETTING",
        "Weather compensation curves",
        "Timer-extension curves updated"
    )

    return get_weather_compensation_api()


# API: ADVANCED TIMER
# ============================================================

@app.get("/api/v1/settings/advanced-timer")
def get_advanced_timer_api():

    result = (
        get_advanced_timer_settings()
    )

    now = local_now()

    result["local_time"] = (
        now.isoformat()
    )

    result["timezone"] = (
        get_timezone_name()
    )

    day = (
        ADVANCED_TIMER_DAYS[
            now.weekday()
        ]
    )

    timer_desired_on = bool(
        result["schedule"][
            day
        ][now.hour]
    )

    desired_on = (
        timer_desired_on
    )

    decision_source = "timer"

    weather_enabled = False
    weather_desired_on = None
    weather_available = None
    weather_preview = None
    weather_curve = None
    weather_extension = 0
    weather_before = 0
    weather_after = 0
    weather_full_day = False

    with weather_compensation_lock:

        weather_enabled = bool(
            weather_compensation_settings[
                "enabled"
            ]
        )

    if weather_enabled:

        weather_result = (
            calculate_weather_compensation()
        )

        weather_available = bool(
            weather_result.get(
                "available"
            )
        )

        weather_preview = bool(
            weather_result.get(
                "preview_mode",
                True
            )
        )

        weather_curve = (
            weather_result.get(
                "active_curve"
            )
        )

        weather_extension = int(
            weather_result.get(
                "active_extension_minutes",
                0
            ) or 0
        )

        weather_before = int(
            weather_result.get(
                "active_before_minutes",
                0
            ) or 0
        )

        weather_after = int(
            weather_result.get(
                "active_after_minutes",
                0
            ) or 0
        )

        weather_full_day = bool(
            weather_result.get(
                "full_day",
                False
            )
        )

        if weather_available:

            weather_desired_on = bool(
                weather_result[
                    "weather_desired_on_now"
                ]
            )

            if weather_preview:

                desired_on = (
                    timer_desired_on
                )

                decision_source = (
                    "weather-preview"
                )

            else:

                desired_on = (
                    weather_desired_on
                )

                decision_source = (
                    "weather"
                )

        else:

            # Fail safely back to timer.
            desired_on = (
                timer_desired_on
            )

            decision_source = (
                "weather-unavailable-timer"
            )

    result["decision"] = {
        "day": day,
        "hour": now.hour,
        "minute": now.minute,

        "timer_on":
            timer_desired_on,

        "weather_enabled":
            weather_enabled,

        "weather_available":
            weather_available,

        "weather_preview":
            weather_preview,

        "weather_on":
            weather_desired_on,

        "weather_curve":
            weather_curve,

        "weather_extension_minutes":
            weather_extension,

        "weather_before_minutes":
            weather_before,

        "weather_after_minutes":
            weather_after,

        "weather_full_day":
            weather_full_day,

        "final_on":
            desired_on,

        "source":
            decision_source
    }

    return result


@app.post("/api/v1/settings/advanced-timer/enabled")
def set_advanced_timer_enabled(
    enabled: bool = Query(...)
):

    with advanced_timer_lock:

        advanced_timer_settings["enabled"] = enabled

        # Force evaluation of the current hour when the
        # timer is enabled later.
        advanced_timer_settings["last_slot"] = None
        advanced_timer_settings["last_action"] = None

        save_advanced_timer_settings()

    db.add_activity(
        "SETTING",
        "Advanced timer",
        "Enabled" if enabled else "Disabled"
    )

    return get_advanced_timer_settings()


@app.post("/api/v1/settings/advanced-timer/test-mode")
def set_advanced_timer_test_mode(
    enabled: bool = Query(...)
):

    with advanced_timer_lock:

        advanced_timer_settings["test_mode"] = enabled

        # Force a fresh evaluation of the current slot.
        advanced_timer_settings["last_slot"] = None
        advanced_timer_settings["last_action"] = None

        save_advanced_timer_settings()

    db.add_activity(
        "SETTING",
        "Advanced Timer Test Mode",
        "Enabled" if enabled else "Disabled"
    )

    return get_advanced_timer_settings()


@app.post("/api/v1/settings/advanced-timer/hour")
def set_advanced_timer_hour(
    day: str = Query(...),
    hour: int = Query(..., ge=0, le=23),
    enabled: bool = Query(...)
):

    day = day.strip().lower()

    if day not in ADVANCED_TIMER_DAYS:

        raise HTTPException(
            status_code=400,
            detail="Invalid weekday"
        )

    with advanced_timer_lock:

        advanced_timer_settings[
            "schedule"
        ][day][hour] = enabled

        # Re-evaluate immediately if the changed hour
        # is the currently active timer slot.
        advanced_timer_settings["last_slot"] = None

        save_advanced_timer_settings()

    return get_advanced_timer_settings()


@app.post("/api/v1/settings/advanced-timer/reset")
def reset_advanced_timer(
    state: str = Query(...)
):

    state = state.strip().lower()

    if state not in ("on", "off"):

        raise HTTPException(
            status_code=400,
            detail="State must be on or off"
        )

    value = state == "on"

    with advanced_timer_lock:

        advanced_timer_settings["schedule"] = {
            day: [value] * 24
            for day in ADVANCED_TIMER_DAYS
        }

        advanced_timer_settings["last_slot"] = None
        advanced_timer_settings["last_action"] = None

        save_advanced_timer_settings()

    db.add_activity(
        "SETTING",
        "Advanced timer",
        "All hours set to " + state.upper()
    )

    return get_advanced_timer_settings()


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


@app.get("/api/v1/settings/pellet-history/template")
def download_pellet_history_template():
    from fastapi.responses import Response

    content = (
        "period;pellets_kg\n"
        "2023-01;425\n"
        "2023-02;380\n"
    )

    return Response(
        content=content,
        media_type="text/csv",
        headers={
            "Content-Disposition":
            'attachment; filename="woody-monitor-pellet-history.csv"'
        }
    )


@app.post("/api/v1/settings/pellet-history/import")
async def import_pellet_history(request: Request):
    raw = await request.body()

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=400,
            detail="CSV file must use UTF-8 encoding"
        )

    rows = []
    reader = csv.reader(io.StringIO(text), delimiter=";")

    for line_number, row in enumerate(reader, start=1):
        if not row or not any(cell.strip() for cell in row):
            continue

        if line_number == 1:
            if [x.strip().lower() for x in row[:2]] != [
                "period", "pellets_kg"
            ]:
                raise HTTPException(
                    status_code=400,
                    detail="CSV must start with: period;pellets_kg"
                )
            continue

        if len(row) < 2:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid CSV row {line_number}"
            )

        period = row[0].strip()
        value = row[1].strip().replace(",", ".")

        try:
            kg = float(value)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid pellet value on row {line_number}"
            )

        if kg < 0:
            raise HTTPException(
                status_code=400,
                detail=f"Pellet value cannot be negative on row {line_number}"
            )

        period_type = None

        try:
            datetime.strptime(period, "%Y-%m")
            period_type = "month"
        except ValueError:
            try:
                datetime.strptime(period, "%Y-%m-%d")
                period_type = "day"
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Invalid period on row {line_number}. "
                        "Use YYYY-MM or YYYY-MM-DD"
                    )
                )

        rows.append((period, period_type, kg))

    if not rows:
        raise HTTPException(
            status_code=400,
            detail="CSV contains no data"
        )

    now = datetime.now(timezone.utc).isoformat()

    with db._connect() as conn:
        for period, period_type, kg in rows:
            conn.execute(
                """
                INSERT INTO pellet_consumption_imported
                    (period, period_type, pellet_kg, source, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(period) DO UPDATE SET
                    period_type=excluded.period_type,
                    pellet_kg=excluded.pellet_kg,
                    source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                (
                    period,
                    period_type,
                    kg,
                    "CSV import",
                    now
                )
            )

        conn.commit()

    return {
        "status": "ok",
        "imported": len(rows),
        "from": min(row[0] for row in rows),
        "to": max(row[0] for row in rows),
        "total_kg": round(sum(row[2] for row in rows), 3)
    }


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
# API: OUTDOOR TEMPERATURE - COMPLETE PERIOD
# ============================================================

@app.get("/api/v1/consumption/outdoor-temperature")
def pellet_outdoor_temperature(
    mode: str = Query("day"),
    hours: int = Query(24, gt=0, le=8760),
    days: int = Query(30, gt=0, le=4000),
    start: str | None = Query(None),
    end: str | None = Query(None)
):

    ensure_daily_outdoor_temperature_table()

    if mode not in ("hour", "day"):
        raise HTTPException(
            400,
            "mode must be hour or day"
        )

    zone = get_timezone()

    if mode == "hour":

        if start and end:

            try:

                start_dt = datetime.fromisoformat(
                    start.replace(
                        "Z",
                        "+00:00"
                    )
                )

                end_dt = datetime.fromisoformat(
                    end.replace(
                        "Z",
                        "+00:00"
                    )
                )

            except ValueError:

                raise HTTPException(
                    400,
                    "Invalid start or end timestamp"
                )

        else:

            end_dt = datetime.now(
                timezone.utc
            )

            start_dt = (
                end_dt -
                timedelta(hours=hours)
            )

        if start_dt >= end_dt:

            raise HTTPException(
                400,
                "start must be before end"
            )

        if (
            end_dt -
            start_dt
        ) > timedelta(days=365):

            raise HTTPException(
                400,
                "Hourly range cannot exceed 365 days"
            )

        with db.lock:

            conn = db._connect()

            try:

                rows = conn.execute(
                    """
                    SELECT
                        substr(timestamp, 1, 13)
                            AS hour_key,
                        AVG(value)
                            AS outside_temp_avg,
                        COUNT(*)
                            AS sample_count

                    FROM measurements

                    WHERE parameter = 'outside_temp'
                      AND timestamp >= ?
                      AND timestamp < ?

                    GROUP BY substr(
                        timestamp,
                        1,
                        13
                    )

                    ORDER BY hour_key
                    """,
                    (
                        start_dt.isoformat(),
                        end_dt.isoformat()
                    )
                ).fetchall()

            finally:

                conn.close()

        data = []

        for row in rows:

            try:

                hour_start = (
                    datetime.fromisoformat(
                        row["hour_key"] +
                        ":00:00+00:00"
                    )
                )

            except Exception:

                continue

            data.append({
                "timestamp":
                    (
                        hour_start +
                        timedelta(hours=1)
                    ).isoformat(),

                "outside_temp_avg":
                    (
                        float(
                            row[
                                "outside_temp_avg"
                            ]
                        )
                        if row[
                            "outside_temp_avg"
                        ] is not None
                        else None
                    ),

                "sample_count":
                    int(
                        row[
                            "sample_count"
                        ] or 0
                    )
            })

        return {
            "mode": "hour",
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "data": data
        }

    # --------------------------------------------------------
    # DAILY DATA
    # --------------------------------------------------------

    today = datetime.now(
        zone
    ).date()

    if start and end:

        try:

            start_dt = datetime.fromisoformat(
                start.replace(
                    "Z",
                    "+00:00"
                )
            )

            end_dt = datetime.fromisoformat(
                end.replace(
                    "Z",
                    "+00:00"
                )
            )

        except ValueError:

            raise HTTPException(
                400,
                "Invalid start or end timestamp"
            )

        start_date = (
            start_dt
            .astimezone(zone)
            .date()
            .isoformat()
        )

        end_date = (
            end_dt
            .astimezone(zone)
            .date()
            .isoformat()
        )

    else:

        start_date = (
            today -
            timedelta(days=days)
        ).isoformat()

        end_date = today.isoformat()

    with db.lock:

        conn = db._connect()

        try:

            rows = conn.execute(
                """
                SELECT
                    local_date,
                    outside_temp_avg,
                    sample_count

                FROM daily_outdoor_temperature

                WHERE local_date >= ?
                  AND local_date <= ?

                ORDER BY local_date
                """,
                (
                    start_date,
                    end_date
                )
            ).fetchall()

        finally:

            conn.close()

    return {
        "mode": "day",
        "start": start_date,
        "end": end_date,
        "data": [
            {
                "date":
                    row["local_date"],

                "outside_temp_avg":
                    (
                        float(
                            row[
                                "outside_temp_avg"
                            ]
                        )
                        if row[
                            "outside_temp_avg"
                        ] is not None
                        else None
                    ),

                "sample_count":
                    int(
                        row[
                            "sample_count"
                        ] or 0
                    )
            }
            for row in rows
        ]
    }


# ============================================================
# API: PELLET CONSUMPTION OVERVIEW
# ============================================================

@app.get("/api/v1/consumption/overview")
def pellet_consumption_overview(
    days: int = Query(1827, gt=0, le=4000)
):

    zone = get_timezone()
    today = datetime.now(zone).date()

    start_date = (
        today - timedelta(days=days)
    ).isoformat()

    end_date = today.isoformat()

    with db.lock:

        conn = db._connect()

        try:

            daily_rows = conn.execute(
                """
                SELECT
                    local_date,
                    pellet_kg,
                    outside_temp_avg,
                    power_avg
                FROM daily_stats
                WHERE local_date >= ?
                  AND local_date <= ?
                ORDER BY local_date
                """,
                (
                    start_date,
                    end_date
                )
            ).fetchall()

            imported_rows = conn.execute(
                """
                SELECT
                    period,
                    period_type,
                    pellet_kg
                FROM pellet_consumption_imported
                ORDER BY period
                """
            ).fetchall()

        finally:

            conn.close()


    daily = []

    measured_dates = set()

    for row in daily_rows:

        measured_dates.add(
            row["local_date"]
        )

        daily.append({
            "date":
                row["local_date"],

            "pellet_kg":
                float(
                    row["pellet_kg"] or 0
                ),

            "outside_temp_avg":
                (
                    float(
                        row["outside_temp_avg"]
                    )
                    if row["outside_temp_avg"]
                    is not None
                    else None
                ),

            "power_avg":
                (
                    float(
                        row["power_avg"]
                    )
                    if row["power_avg"]
                    is not None
                    else None
                ),

            "source":
                "Woody Monitor"
        })


    imported = []

    for row in imported_rows:

        period = row["period"]

        if (
            period < start_date[:len(period)] or
            period > end_date[:len(period)]
        ):
            continue

        imported.append({
            "period":
                period,

            "period_type":
                row["period_type"],

            "pellet_kg":
                float(
                    row["pellet_kg"] or 0
                ),

            "source":
                "CSV import"
        })


    return {
        "start_date": start_date,
        "end_date": end_date,
        "daily": daily,
        "imported": imported
    }



# ============================================================
# API: HOURLY CHART METRICS
# ============================================================

@app.get("/api/v1/consumption/overview/hourly")
def pellet_consumption_overview_hourly(
    hours: int = Query(24, gt=0, le=8760),
    start: str | None = Query(None),
    end: str | None = Query(None)
):

    if start and end:

        try:
            start_dt = datetime.fromisoformat(
                start.replace("Z", "+00:00")
            )
            end_dt = datetime.fromisoformat(
                end.replace("Z", "+00:00")
            )

        except ValueError:

            raise HTTPException(
                400,
                "Invalid start or end timestamp"
            )

    else:

        end_dt = datetime.now(
            timezone.utc
        )

        start_dt = (
            end_dt -
            timedelta(hours=hours)
        )

    if start_dt >= end_dt:

        raise HTTPException(
            400,
            "start must be before end"
        )

    if (
        end_dt -
        start_dt
    ) > timedelta(days=365):

        raise HTTPException(
            400,
            "Hourly range cannot exceed 365 days"
        )

    with db.lock:

        conn = db._connect()

        try:

            rows = conn.execute(
                """
                SELECT
                    hour_start,
                    outside_temp_avg,
                    power_avg

                FROM pellet_consumption_hourly

                WHERE hour_start >= ?
                  AND hour_start < ?
                  AND kg > 0
                  AND (
                      outside_temp_samples > 0
                      OR power_samples > 0
                  )

                ORDER BY hour_start ASC
                """,
                (
                    start_dt.isoformat(),
                    end_dt.isoformat()
                )
            ).fetchall()

        finally:

            conn.close()

    data = []

    for row in rows:

        try:

            hour_start = datetime.fromisoformat(
                row["hour_start"]
            )

            timestamp = (
                hour_start +
                timedelta(hours=1)
            )

        except Exception:

            continue

        data.append({
            "timestamp":
                timestamp.isoformat(),

            "outside_temp_avg":
                (
                    float(
                        row["outside_temp_avg"]
                    )
                    if row["outside_temp_avg"]
                    is not None
                    else None
                ),

            "power_avg":
                (
                    float(
                        row["power_avg"]
                    )
                    if row["power_avg"]
                    is not None
                    else None
                )
        })

    return {
        "start":
            start_dt.isoformat(),

        "end":
            end_dt.isoformat(),

        "data":
            data
    }

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

    # Raw history is stored once per minute, so buckets smaller
    # than 60 seconds cannot provide additional useful detail.
    bucket_seconds = max(
        bucket_seconds,
        60
    )

    # Keep short and medium history ranges compact enough for
    # fast browser rendering while retaining useful resolution.
    if duration_seconds > 12 * 3600:
        bucket_seconds = max(
            bucket_seconds,
            120
        )

    if duration_seconds > 24 * 3600:
        bucket_seconds = max(
            bucket_seconds,
            300
        )

    # Seven days or longer uses the pre-aggregated hourly cache.
    if duration_seconds >= 7 * 24 * 3600:
        bucket_seconds = max(
            bucket_seconds,
            3600
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
    load_cleaning_settings()
    load_advanced_timer_settings()
    load_weather_compensation_settings()
    load_home_assistant_settings()

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

    network_monitor = threading.Thread(
        target=network_monitor_loop,
        daemon=True
    )

    network_monitor.start()

    advanced_timer = threading.Thread(
        target=advanced_timer_loop,
        daemon=True
    )

    advanced_timer.start()

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

    history_hourly = threading.Thread(
        target=history_hourly_loop,
        daemon=True
    )

    feeder_retention = threading.Thread(
        target=feeder_retention_loop,
        daemon=True
    )

    collector.start()
    history.start()
    pellet_hourly.start()
    history_hourly.start()
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
