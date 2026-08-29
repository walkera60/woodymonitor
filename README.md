# Woody Monitor

Woody Monitor is a local web-based monitoring interface for compatible
NBE / Scotte / Woody pellet burner controllers.

It communicates directly with the burner controller through a serial
connection and provides live values, historical data and pellet
consumption statistics.

## Features

- Live burner monitoring
- Local web interface
- Historical graphs
- Pellet consumption calculation
- Pellet silo monitoring
- SQLite database
- Automatic database retention
- Optional MQTT publishing
- Configurable timezone
- Runs as a systemd service
- No cloud service required

## Requirements

Recommended:

- Raspberry Pi or other Debian-based Linux computer
- Python 3
- USB to serial adapter compatible with the burner controller
- Network connection
- Supported NBE / Scotte / Woody controller

The installer is intended for Raspberry Pi OS and other Debian-based
Linux distributions using `apt` and `systemd`.

## Installation

Clone the repository:

    git clone <REPOSITORY-URL> woodymonitor
    cd woodymonitor

Run the installer:

    ./install.sh

The installer will:

- Install required Python packages
- Create a Python virtual environment
- Create the runtime data directory
- Create `.env` from `.env.example`
- Attempt to detect the USB serial adapter
- Add the current user to the `dialout` group
- Install the `woody-monitor.service`
- Enable Woody Monitor at boot

The installer does not start Woody Monitor automatically.

## Configuration

After installation, edit:

    nano .env

Main configuration:

    WOODY_SERIAL_DEVICE=/dev/ttyUSB0
    WOODY_HOST=0.0.0.0
    WOODY_PORT=8080

    WOODY_MQTT_BROKER=localhost
    WOODY_MQTT_PORT=1883
    WOODY_MQTT_USERNAME=
    WOODY_MQTT_PASSWORD=
    WOODY_MQTT_TOPIC=woodymonitor

### Serial device

A stable serial path is recommended.

List available devices:

    ls -l /dev/serial/by-id/

If available, use the complete `/dev/serial/by-id/...` path for
`WOODY_SERIAL_DEVICE`.

If exactly one serial device exists when `install.sh` is run, the
installer will automatically place that device path in `.env`.

## Starting Woody Monitor

After checking `.env`:

    sudo systemctl start woody-monitor

Check status:

    systemctl status woody-monitor --no-pager

View live logs:

    journalctl -u woody-monitor -f

## Web interface

Woody Monitor listens on port 8080 by default.

Open the following address in a browser:

    http://<RASPBERRY-PI-IP>:8080

For example:

    http://192.168.1.50:8080

## MQTT

MQTT publishing is optional.

Configure the MQTT broker in `.env`. Woody Monitor publishes live
values below the configured topic, for example:

    woodymonitor/live
    woodymonitor/boiler_temp
    woodymonitor/power
    woodymonitor/oxygen

The local web interface can operate even if an MQTT broker is not
available.

## Data storage

Runtime data is stored in:

    data/

The SQLite database is:

    data/woody.db

Runtime databases and local settings are intentionally excluded from Git.

Woody Monitor stores relevant graph history once per minute.

Raw `feeder_time` history is retained for approximately 48 hours.

Other measurement history is retained for 90 days.

Completed pellet consumption is stored separately as permanent hourly
records.

Runtime settings may be created automatically inside `data/`, including:

    feeder_settings.json
    silo_settings.json
    timezone_settings.json

## Updating

Stop Woody Monitor:

    sudo systemctl stop woody-monitor

Update the repository:

    git pull

Update Python dependencies:

    .venv/bin/pip install -r requirements.txt

Start Woody Monitor:

    sudo systemctl start woody-monitor

## Service commands

Start:

    sudo systemctl start woody-monitor

Stop:

    sudo systemctl stop woody-monitor

Restart:

    sudo systemctl restart woody-monitor

Status:

    systemctl status woody-monitor --no-pager

Logs:

    journalctl -u woody-monitor -f

## Third-party software

Woody Monitor contains and builds upon code originating from the
PellMon project:

https://github.com/motoz/PellMon

The PellMon-derived components retain their original copyright and
GPL license notices.

See `THIRD_PARTY_LICENSES.md` for additional information.

## License

Woody Monitor is distributed under the GNU General Public License v2.0
where applicable.

Third-party source files retain their original copyright and license
notices.

See `LICENSE` for the full license text.

## Disclaimer

Woody Monitor is an independent project.

It is not affiliated with, endorsed by, sponsored by, or maintained by
NBE, PellMon, Scotte, Woody or Anders Nylund.

Use of Woody Monitor is at your own risk.
