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

    git clone https://github.com/walkera60/woodymonitor.git woodymonitor
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

## Home Assistant integration

Woody Monitor can provide its live burner data directly to Home Assistant
through the built-in REST API.

**MQTT is not required.**

The data flow is:

    Woody Monitor
          |
          | HTTP REST API
          | /api/v1/live
          v
    Home Assistant
          |
          +-- sensor.woody_boiler_temperature
          +-- sensor.woody_power
          +-- sensor.woody_oxygen
          +-- sensor.woody_hotwater_temperature
          +-- sensor.woody_pellet_silo
          +-- sensor.woody_burner_alarm
          +-- sensor.woody_burner_mode
          +-- and more

Home Assistant periodically requests the Woody Monitor API and creates
Home Assistant sensors from the returned values.

### 1. Find the Woody Monitor IP address

Find the IP address of the computer running Woody Monitor.

    192.168.1.50

Test that Woody Monitor is accessible by opening:

    http://192.168.1.50:8080/api/v1/status

A working installation should return JSON containing:

    "application": "Woody Monitor"
    "connected": true

The complete live data is available at:

    http://192.168.1.50:8080/api/v1/live

### 2. Configure Home Assistant

Open Home Assistant configuration.yaml and add the following REST integration.

Replace 192.168.1.50 with the IP address of the computer running Woody Monitor.

```yaml
rest:
  - resource: http://192.168.1.50:8080/api/v1/live
    scan_interval: 30
    sensor:
      - name: "Woody Boiler Temperature"
        unique_id: woody_boiler_temperature
        value_template: "{{ value_json.values.boiler_temp | float(0) }}"
        unit_of_measurement: "°C"
        device_class: temperature
        state_class: measurement

      - name: "Woody Boiler Return Temperature"
        unique_id: woody_boiler_return_temperature
        value_template: "{{ value_json.values.boiler_return_temp | float(0) }}"
        unit_of_measurement: "°C"
        device_class: temperature
        state_class: measurement

      - name: "Woody Hot Water Temperature"
        unique_id: woody_hotwater_temperature
        value_template: "{{ value_json.values.hotwater_temp | float(0) }}"
        unit_of_measurement: "°C"
        device_class: temperature
        state_class: measurement

      - name: "Woody Outdoor Temperature"
        unique_id: woody_outdoor_temperature
        value_template: "{{ value_json.values.outside_temp | float(0) }}"
        unit_of_measurement: "°C"
        device_class: temperature
        state_class: measurement

      - name: "Woody Oxygen"
        unique_id: woody_oxygen
        value_template: "{{ value_json.values.oxygen | float(0) }}"
        unit_of_measurement: "%"
        state_class: measurement

      - name: "Woody Burner Power"
        unique_id: woody_burner_power
        value_template: "{{ value_json.values.power | float(0) }}"
        unit_of_measurement: "%"
        state_class: measurement

      - name: "Woody Burner kW"
        unique_id: woody_burner_kw
        value_template: "{{ value_json.values.power_kW | float(0) }}"
        unit_of_measurement: "kW"
        device_class: power
        state_class: measurement

      - name: "Woody Pellet Silo"
        unique_id: woody_pellet_silo
        value_template: "{{ value_json.values.magazine_content | float(0) }}"
        unit_of_measurement: "kg"
        state_class: measurement

      - name: "Woody Burner Alarm"
        unique_id: woody_burner_alarm
        value_template: "{{ value_json.values.alarm }}"

      - name: "Woody Burner Mode"
        unique_id: woody_burner_mode
        value_template: "{{ value_json.values.mode }}"
```

### 3. Finish the Home Assistant setup

After adding the REST configuration:

1. Replace 192.168.1.50 with the IP address of your Woody Monitor.
2. Save configuration.yaml.
3. Check the Home Assistant configuration for errors.
4. Restart Home Assistant.
5. The Woody Monitor sensors will then be available in Home Assistant.

Example entities include:

    sensor.woody_boiler_temperature
    sensor.woody_boiler_return_temperature
    sensor.woody_hotwater_temperature
    sensor.woody_outdoor_temperature
    sensor.woody_oxygen
    sensor.woody_burner_power
    sensor.woody_burner_kw
    sensor.woody_pellet_silo
    sensor.woody_burner_alarm
    sensor.woody_burner_mode

The example uses a 30-second update interval:

    scan_interval: 30

Home Assistant will request the Woody Monitor API every 30 seconds.
The interval can be changed if required.

### 4. Test the connection

Before configuring Home Assistant, the API can be tested from a browser:

    http://192.168.1.50:8080/api/v1/status

Live data:

    http://192.168.1.50:8080/api/v1/live

The live endpoint returns the burner parameters inside the values object.

### 5. MQTT is optional

Woody Monitor also supports MQTT publishing, but MQTT is not required for Home Assistant.

For a simple Home Assistant installation, the REST API provides a direct local connection between Woody Monitor and Home Assistant.


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
