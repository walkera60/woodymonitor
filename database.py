#!/usr/bin/env python3

import sqlite3
import threading
from pathlib import Path
from datetime import datetime, timezone


DB_PATH = Path(str(Path(__file__).resolve().parent / "data" / "woody.db"))


class Database:

    def __init__(self, path=DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        self.lock = threading.Lock()

        self._init_database()

    def _connect(self):
        conn = sqlite3.connect(
            self.path,
            timeout=30
        )

        conn.row_factory = sqlite3.Row

        return conn

    def _init_database(self):

        with self.lock:

            conn = self._connect()

            try:

                conn.execute("""
                    CREATE TABLE IF NOT EXISTS measurements (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        parameter TEXT NOT NULL,
                        value REAL
                    )
                """)

                conn.execute("""
                    CREATE INDEX IF NOT EXISTS
                    idx_measurements_parameter_time
                    ON measurements(parameter, timestamp)
                """)

                conn.execute("""
                    CREATE INDEX IF NOT EXISTS
                    idx_measurements_time
                    ON measurements(timestamp)
                """)


                conn.execute("""
                    CREATE TABLE IF NOT EXISTS history_hourly (
                        hour_start TEXT NOT NULL,
                        parameter TEXT NOT NULL,
                        value_sum REAL NOT NULL,
                        value_count INTEGER NOT NULL,
                        last_value REAL,
                        last_timestamp TEXT NOT NULL,
                        PRIMARY KEY (hour_start, parameter)
                    )
                """)

                conn.execute("""
                    CREATE INDEX IF NOT EXISTS
                    idx_history_hourly_parameter_time
                    ON history_hourly(parameter, hour_start)
                """)


                conn.execute("""
                    CREATE TABLE IF NOT EXISTS pellet_prices (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        effective_at TEXT NOT NULL,
                        price_per_kg REAL NOT NULL
                    )
                """)

                conn.execute("""
                    CREATE INDEX IF NOT EXISTS
                    idx_pellet_prices_time
                    ON pellet_prices(effective_at)
                """)

                conn.execute("""
                    CREATE TABLE IF NOT EXISTS pellet_consumption_hourly (
                        hour_start TEXT PRIMARY KEY,
                        feeder_seconds REAL NOT NULL,
                        kg REAL NOT NULL,
                        outside_temp_avg REAL,
                        outside_temp_samples INTEGER NOT NULL DEFAULT 0,
                        power_avg REAL,
                        power_samples INTEGER NOT NULL DEFAULT 0,
                        power_max REAL,
                        power_kw_avg REAL,
                        power_kw_samples INTEGER NOT NULL DEFAULT 0,
                        power_kw_max REAL
                    )
                """)

                # Add active-heating metrics to older databases.
                existing_columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(pellet_consumption_hourly)"
                    ).fetchall()
                }

                pellet_hour_migrations = (
                    ("outside_temp_avg", "REAL"),
                    (
                        "outside_temp_samples",
                        "INTEGER NOT NULL DEFAULT 0"
                    ),
                    ("power_avg", "REAL"),
                    (
                        "power_samples",
                        "INTEGER NOT NULL DEFAULT 0"
                    ),
                    ("power_max", "REAL"),
                    ("power_kw_avg", "REAL"),
                    (
                        "power_kw_samples",
                        "INTEGER NOT NULL DEFAULT 0"
                    ),
                    ("power_kw_max", "REAL"),
                )

                schema_changed = False

                for column_name, column_type in pellet_hour_migrations:

                    if column_name not in existing_columns:

                        conn.execute(
                            "ALTER TABLE pellet_consumption_hourly "
                            f"ADD COLUMN {column_name} {column_type}"
                        )

                        schema_changed = True

                if schema_changed:
                    conn.commit()

                conn.execute("""
                    CREATE TABLE IF NOT EXISTS pellet_consumption_imported (
                        period TEXT PRIMARY KEY,
                        period_type TEXT NOT NULL,
                        pellet_kg REAL NOT NULL,
                        source TEXT,
                        updated_at TEXT NOT NULL
                    )
                """)

                conn.execute("""
                    CREATE TABLE IF NOT EXISTS daily_stats (
                        local_date TEXT PRIMARY KEY,
                        timezone TEXT NOT NULL,
                        pellet_kg REAL NOT NULL DEFAULT 0,
                        outside_temp_avg REAL,
                        power_avg REAL,
                        power_max REAL,
                        power_kw_avg REAL,
                        power_kw_max REAL,
                        updated_at TEXT NOT NULL
                    )
                """)

                conn.execute("""
                    CREATE TABLE IF NOT EXISTS activity_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        type TEXT NOT NULL,
                        event TEXT NOT NULL,
                        details TEXT,
                        payload TEXT,
                        response TEXT
                    )
                """)

                conn.execute("""
                    CREATE INDEX IF NOT EXISTS
                    idx_activity_log_time
                    ON activity_log(timestamp)
                """)

                conn.commit()

            finally:
                conn.close()

    def insert_measurements(self, values):

        timestamp = datetime.now(
            timezone.utc
        ).isoformat()

        rows = []

        for parameter, value in values.items():

            try:
                numeric = float(value)
            except (ValueError, TypeError):
                continue

            rows.append(
                (
                    timestamp,
                    parameter,
                    numeric
                )
            )

        if not rows:
            return

        with self.lock:

            conn = self._connect()

            try:

                conn.executemany(
                    """
                    INSERT INTO measurements
                    (timestamp, parameter, value)
                    VALUES (?, ?, ?)
                    """,
                    rows
                )

                conn.commit()

            finally:
                conn.close()

    def rebuild_history_hour(self, hour_start, hour_end):
        """
        Build one completed hourly history bucket from raw measurements.

        The hourly cache is written once for a completed hour instead of
        being updated for every one-minute history sample. This reduces
        unnecessary SD-card writes while keeping the cache recoverable
        from raw measurements.
        """

        start = hour_start.isoformat()
        end = hour_end.isoformat()
        hour_key = hour_start.isoformat()

        with self.lock:
            conn = self._connect()

            try:
                rows = conn.execute(
                    """
                    SELECT
                        parameter,
                        SUM(value) AS value_sum,
                        COUNT(*) AS value_count
                    FROM measurements
                    WHERE timestamp >= ?
                      AND timestamp < ?
                    GROUP BY parameter
                    """,
                    (start, end)
                ).fetchall()

                if not rows:
                    return 0

                hourly_rows = []

                for row in rows:
                    parameter = row["parameter"]

                    last = conn.execute(
                        """
                        SELECT value, timestamp
                        FROM measurements
                        WHERE parameter = ?
                          AND timestamp >= ?
                          AND timestamp < ?
                        ORDER BY timestamp DESC
                        LIMIT 1
                        """,
                        (parameter, start, end)
                    ).fetchone()

                    if last is None:
                        continue

                    hourly_rows.append(
                        (
                            hour_key,
                            parameter,
                            row["value_sum"],
                            row["value_count"],
                            last["value"],
                            last["timestamp"]
                        )
                    )

                conn.executemany(
                    """
                    INSERT INTO history_hourly (
                        hour_start,
                        parameter,
                        value_sum,
                        value_count,
                        last_value,
                        last_timestamp
                    )
                    VALUES (?, ?, ?, ?, ?, ?)

                    ON CONFLICT(hour_start, parameter)
                    DO UPDATE SET
                        value_sum = excluded.value_sum,
                        value_count = excluded.value_count,
                        last_value = excluded.last_value,
                        last_timestamp = excluded.last_timestamp
                    """,
                    hourly_rows
                )

                conn.commit()

                return len(hourly_rows)

            finally:
                conn.close()

    def get_history(
        self,
        parameters,
        start,
        end,
        max_points=None,
        bucket_seconds=None
    ):

        if isinstance(parameters, str):
            parameters = [parameters]

        if not parameters:
            return []

        placeholders = ",".join(
            "?" for _ in parameters
        )

        with self.lock:

            conn = self._connect()

            try:

                # -------------------------------------------------
                # RAW HISTORY
                # -------------------------------------------------
                #
                # Used when no downsampling is requested.
                #
                if not bucket_seconds:

                    sql = f"""
                        SELECT
                            timestamp,
                            parameter,
                            value
                        FROM measurements
                        WHERE parameter IN ({placeholders})
                          AND timestamp >= ?
                          AND timestamp <= ?
                        ORDER BY timestamp ASC
                    """

                    rows = conn.execute(
                        sql,
                        list(parameters) + [
                            start,
                            end
                        ]
                    ).fetchall()

                    return [
                        dict(row)
                        for row in rows
                    ]

                # -------------------------------------------------
                # DOWNSAMPLED HISTORY
                # -------------------------------------------------
                #
                # Normal parameters use AVG().
                #
                # CUMULATIVE parameters such as feeder_time MUST NOT
                # use AVG(), because the difference between consecutive
                # values is used to calculate consumption.
                #
                # For cumulative counters we therefore keep the LAST
                # real value in every time bucket.
                #

                # -------------------------------------------------
                # HOURLY HISTORY CACHE
                # -------------------------------------------------
                #
                # Long history ranges use pre-aggregated hourly data.
                # This avoids scanning hundreds of thousands of raw
                # measurements for every graph request.
                #
                if bucket_seconds >= 3600:

                    def utc_hour(value):
                        dt = datetime.fromisoformat(
                            value.replace("Z", "+00:00")
                        )

                        if dt.tzinfo is None:
                            dt = dt.replace(
                                tzinfo=timezone.utc
                            )

                        return (
                            dt.astimezone(timezone.utc)
                            .replace(
                                minute=0,
                                second=0,
                                microsecond=0
                            )
                            .isoformat()
                        )

                    start_hour = utc_hour(start)
                    end_hour = utc_hour(end)

                    rows = []

                    cumulative_parameters = {
                        "feeder_time"
                    }

                    normal_parameters = [
                        parameter
                        for parameter in parameters
                        if parameter not in cumulative_parameters
                    ]

                    if normal_parameters:

                        hourly_placeholders = ",".join(
                            "?" for _ in normal_parameters
                        )

                        sql = f"""
                            SELECT
                                parameter,

                                CAST(
                                    strftime('%s', hour_start)
                                    AS INTEGER
                                ) / ? AS bucket,

                                MIN(hour_start) AS timestamp,

                                SUM(value_sum) /
                                NULLIF(
                                    SUM(value_count),
                                    0
                                ) AS value

                            FROM history_hourly

                            WHERE parameter IN (
                                {hourly_placeholders}
                            )
                              AND hour_start >= ?
                              AND hour_start <= ?

                            GROUP BY parameter, bucket

                            ORDER BY bucket ASC
                        """

                        hourly_rows = conn.execute(
                            sql,
                            [
                                bucket_seconds,
                                *normal_parameters,
                                start_hour,
                                end_hour
                            ]
                        ).fetchall()

                        rows.extend(
                            {
                                "timestamp":
                                    row["timestamp"],
                                "parameter":
                                    row["parameter"],
                                "value":
                                    row["value"]
                            }
                            for row in hourly_rows
                        )

                    for parameter in parameters:

                        if parameter not in cumulative_parameters:
                            continue

                        sql = """
                            SELECT
                                bucket,
                                last_timestamp AS timestamp,
                                last_value AS value

                            FROM (
                                SELECT
                                    CAST(
                                        strftime(
                                            '%s',
                                            hour_start
                                        )
                                        AS INTEGER
                                    ) / ? AS bucket,

                                    last_timestamp,
                                    last_value,

                                    ROW_NUMBER() OVER (
                                        PARTITION BY
                                            CAST(
                                                strftime(
                                                    '%s',
                                                    hour_start
                                                )
                                                AS INTEGER
                                            ) / ?

                                        ORDER BY
                                            hour_start DESC
                                    ) AS rn

                                FROM history_hourly

                                WHERE parameter = ?
                                  AND hour_start >= ?
                                  AND hour_start <= ?
                            )

                            WHERE rn = 1

                            ORDER BY bucket ASC
                        """

                        hourly_rows = conn.execute(
                            sql,
                            [
                                bucket_seconds,
                                bucket_seconds,
                                parameter,
                                start_hour,
                                end_hour
                            ]
                        ).fetchall()

                        rows.extend(
                            {
                                "timestamp":
                                    row["timestamp"],
                                "parameter":
                                    parameter,
                                "value":
                                    row["value"]
                            }
                            for row in hourly_rows
                        )

                    rows.sort(
                        key=lambda row: row["timestamp"]
                    )

                    return rows

                rows = []

                cumulative_parameters = {
                    "feeder_time"
                }

                normal_parameters = [
                    parameter
                    for parameter in parameters
                    if parameter not in cumulative_parameters
                ]

                # -------------------------------------------------
                # NORMAL PARAMETERS
                # -------------------------------------------------
                #
                # Query all normal parameters in one SQL statement
                # instead of executing one query per parameter.
                #
                if normal_parameters:

                    normal_placeholders = ",".join(
                        "?" for _ in normal_parameters
                    )

                    sql = f"""
                        SELECT
                            parameter,

                            CAST(
                                strftime('%s', timestamp)
                                AS INTEGER
                            ) / ? AS bucket,

                            MIN(timestamp) AS timestamp,
                            AVG(value) AS value

                        FROM measurements

                        WHERE parameter IN ({normal_placeholders})
                          AND timestamp >= ?
                          AND timestamp <= ?

                        GROUP BY parameter, bucket

                        ORDER BY bucket ASC
                    """

                    parameter_rows = conn.execute(
                        sql,
                        [
                            bucket_seconds,
                            *normal_parameters,
                            start,
                            end
                        ]
                    ).fetchall()

                    rows.extend(
                        {
                            "timestamp": row["timestamp"],
                            "parameter": row["parameter"],
                            "value": row["value"]
                        }
                        for row in parameter_rows
                    )

                # -------------------------------------------------
                # CUMULATIVE PARAMETERS
                # -------------------------------------------------
                #
                # Keep the LAST real value in each bucket.
                #
                for parameter in parameters:

                    if parameter not in cumulative_parameters:
                        continue

                    sql = """
                        SELECT
                            bucket,
                            timestamp,
                            value
                        FROM (
                            SELECT
                                CAST(
                                    strftime('%s', timestamp)
                                    AS INTEGER
                                ) / ? AS bucket,

                                timestamp,
                                value,

                                ROW_NUMBER() OVER (
                                    PARTITION BY
                                        CAST(
                                            strftime('%s', timestamp)
                                            AS INTEGER
                                        ) / ?
                                    ORDER BY timestamp DESC
                                ) AS rn

                            FROM measurements

                            WHERE parameter = ?
                              AND timestamp >= ?
                              AND timestamp <= ?
                        )

                        WHERE rn = 1

                        ORDER BY timestamp ASC
                    """

                    parameter_rows = conn.execute(
                        sql,
                        [
                            bucket_seconds,
                            bucket_seconds,
                            parameter,
                            start,
                            end
                        ]
                    ).fetchall()

                    rows.extend(
                        {
                            "timestamp": row["timestamp"],
                            "parameter": parameter,
                            "value": row["value"]
                        }
                        for row in parameter_rows
                    )

                rows.sort(
                    key=lambda row: row["timestamp"]
                )

                return rows

            finally:
                conn.close()

    def upsert_pellet_hour(
        self,
        hour_start,
        feeder_seconds,
        kg,
        outside_temp_avg=None,
        outside_temp_samples=0,
        power_avg=None,
        power_samples=0,
        power_max=None,
        power_kw_avg=None,
        power_kw_samples=0,
        power_kw_max=None
    ):

        with self.lock:

            conn = self._connect()

            try:

                conn.execute(
                    """
                    INSERT INTO pellet_consumption_hourly (
                        hour_start,
                        feeder_seconds,
                        kg,
                        outside_temp_avg,
                        outside_temp_samples,
                        power_avg,
                        power_samples,
                        power_max,
                        power_kw_avg,
                        power_kw_samples,
                        power_kw_max
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

                    ON CONFLICT(hour_start)
                    DO UPDATE SET
                        feeder_seconds = excluded.feeder_seconds,
                        kg = excluded.kg,
                        outside_temp_avg = excluded.outside_temp_avg,
                        outside_temp_samples =
                            excluded.outside_temp_samples,
                        power_avg = excluded.power_avg,
                        power_samples = excluded.power_samples,
                        power_max = excluded.power_max,
                        power_kw_avg = excluded.power_kw_avg,
                        power_kw_samples =
                            excluded.power_kw_samples,
                        power_kw_max = excluded.power_kw_max
                    """,
                    (
                        hour_start,
                        float(feeder_seconds),
                        float(kg),
                        outside_temp_avg,
                        int(outside_temp_samples or 0),
                        power_avg,
                        int(power_samples or 0),
                        power_max,
                        power_kw_avg,
                        int(power_kw_samples or 0),
                        power_kw_max
                    )
                )

                conn.commit()

            finally:
                conn.close()

    def get_pellet_hours(
        self,
        start=None,
        end=None
    ):

        with self.lock:

            conn = self._connect()

            try:

                if start is not None and end is not None:

                    rows = conn.execute(
                        """
                        SELECT
                            hour_start,
                            feeder_seconds,
                            kg
                        FROM pellet_consumption_hourly
                        WHERE hour_start >= ?
                          AND hour_start < ?
                        ORDER BY hour_start ASC
                        """,
                        (start, end)
                    ).fetchall()

                else:

                    rows = conn.execute(
                        """
                        SELECT
                            hour_start,
                            feeder_seconds,
                            kg
                        FROM pellet_consumption_hourly
                        ORDER BY hour_start ASC
                        """
                    ).fetchall()

                return [
                    dict(row)
                    for row in rows
                ]

            finally:
                conn.close()

    def upsert_daily_stats(
        self,
        local_date,
        timezone_name,
        pellet_kg,
        outside_temp_avg=None,
        power_avg=None,
        power_max=None,
        power_kw_avg=None,
        power_kw_max=None
    ):

        updated_at = datetime.now(
            timezone.utc
        ).isoformat()

        with self.lock:

            conn = self._connect()

            try:

                conn.execute(
                    """
                    INSERT INTO daily_stats (
                        local_date,
                        timezone,
                        pellet_kg,
                        outside_temp_avg,
                        power_avg,
                        power_max,
                        power_kw_avg,
                        power_kw_max,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)

                    ON CONFLICT(local_date)
                    DO UPDATE SET
                        timezone = excluded.timezone,
                        pellet_kg = excluded.pellet_kg,
                        outside_temp_avg = excluded.outside_temp_avg,
                        power_avg = excluded.power_avg,
                        power_max = excluded.power_max,
                        power_kw_avg = excluded.power_kw_avg,
                        power_kw_max = excluded.power_kw_max,
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(local_date),
                        str(timezone_name),
                        float(pellet_kg or 0),
                        outside_temp_avg,
                        power_avg,
                        power_max,
                        power_kw_avg,
                        power_kw_max,
                        updated_at
                    )
                )

                conn.commit()

            finally:
                conn.close()


    def get_daily_stats(
        self,
        start_date=None,
        end_date=None
    ):

        with self.lock:

            conn = self._connect()

            try:

                sql = """
                    SELECT
                        local_date,
                        timezone,
                        pellet_kg,
                        outside_temp_avg,
                        power_avg,
                        power_max,
                        power_kw_avg,
                        power_kw_max,
                        updated_at
                    FROM daily_stats
                """

                conditions = []
                params = []

                if start_date is not None:
                    conditions.append(
                        "local_date >= ?"
                    )
                    params.append(
                        str(start_date)
                    )

                if end_date is not None:
                    conditions.append(
                        "local_date <= ?"
                    )
                    params.append(
                        str(end_date)
                    )

                if conditions:
                    sql += (
                        " WHERE " +
                        " AND ".join(conditions)
                    )

                sql += " ORDER BY local_date ASC"

                rows = conn.execute(
                    sql,
                    params
                ).fetchall()

                return [
                    dict(row)
                    for row in rows
                ]

            finally:
                conn.close()


    def cleanup_measurement_history(self, cutoff):
        with self.lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    """
                    DELETE FROM measurements
                    WHERE parameter != 'feeder_time'
                      AND timestamp < ?
                    """,
                    (cutoff,)
                )
                deleted = cursor.rowcount
                conn.commit()
                return deleted
            finally:
                conn.close()

    def cleanup_feeder_history(self, cutoff):

        with self.lock:

            conn = self._connect()

            try:
                cursor = conn.execute(
                    """
                    DELETE FROM measurements
                    WHERE parameter = 'feeder_time'
                      AND timestamp < ?
                    """,
                    (cutoff,)
                )

                deleted = cursor.rowcount
                conn.commit()

                return deleted

            finally:
                conn.close()

    def get_parameter_names(self):

        with self.lock:

            conn = self._connect()

            try:

                rows = conn.execute("""
                    SELECT DISTINCT parameter
                    FROM measurements
                    ORDER BY parameter
                """).fetchall()

                return [
                    row["parameter"]
                    for row in rows
                ]

            finally:
                conn.close()


    def add_activity(
        self,
        event_type,
        event,
        details=None,
        payload=None,
        response=None
    ):

        timestamp = datetime.now(
            timezone.utc
        ).isoformat()

        with self.lock:

            conn = self._connect()

            try:

                conn.execute("""
                    INSERT INTO activity_log
                    (
                        timestamp,
                        type,
                        event,
                        details,
                        payload,
                        response
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    timestamp,
                    str(event_type),
                    str(event),
                    None if details is None else str(details),
                    None if payload is None else str(payload),
                    None if response is None else str(response)
                ))

                conn.commit()

            finally:
                conn.close()


    def get_activity(self, limit=500):

        with self.lock:

            conn = self._connect()

            try:

                rows = conn.execute("""
                    SELECT
                        id,
                        timestamp,
                        type,
                        event,
                        details,
                        payload,
                        response
                    FROM activity_log
                    ORDER BY id DESC
                    LIMIT ?
                """, (int(limit),)).fetchall()

                return [dict(row) for row in rows]

            finally:
                conn.close()


    def clear_activity(self):

        with self.lock:

            conn = self._connect()

            try:

                conn.execute(
                    "DELETE FROM activity_log"
                )

                conn.commit()

            finally:
                conn.close()


    def count(self):

        with self.lock:

            conn = self._connect()

            try:

                row = conn.execute(
                    "SELECT COUNT(*) AS count FROM measurements"
                ).fetchone()

                return row["count"]

            finally:
                conn.close()
