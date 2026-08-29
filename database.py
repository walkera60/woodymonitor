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


import sqlite3
import threading
from pathlib import Path
from datetime import datetime, timezone


DB_PATH = Path(__file__).resolve().parent / "data" / "woody.db"


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

                rows = []

                cumulative_parameters = {
                    "feeder_time"
                }

                for parameter in parameters:

                    if parameter in cumulative_parameters:

                        sql = f"""
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

                    else:

                        sql = f"""
                            SELECT
                                CAST(
                                    strftime('%s', timestamp)
                                    AS INTEGER
                                ) / ? AS bucket,

                                MIN(timestamp) AS timestamp,
                                AVG(value) AS value

                            FROM measurements

                            WHERE parameter = ?
                              AND timestamp >= ?
                              AND timestamp <= ?

                            GROUP BY bucket

                            ORDER BY bucket ASC
                        """

                        parameter_rows = conn.execute(
                            sql,
                            [
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
