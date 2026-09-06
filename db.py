"""MySQL connection helpers.

Production configuration is supplied through environment variables:

MYSQL_HOST
MYSQL_PORT
MYSQL_DATABASE
MYSQL_USER
MYSQL_PASSWORD
MYSQL_SSL_MODE
MYSQL_SSL_CA (optional)

Aiven MySQL:

MYSQL_HOST=luxe-beauty-hub-uxe-beauty-hub.g.aivencloud.com
MYSQL_PORT=23898
MYSQL_DATABASE=defaultdb
MYSQL_USER=avnadmin
MYSQL_PASSWORD=<set in Render Environment Variables>
MYSQL_SSL_MODE=REQUIRED
"""

import os
import json

import mysql.connector
from mysql.connector import pooling


# ===================================================================
# BASE DIRECTORY
# ===================================================================

ROOT = os.path.dirname(os.path.abspath(__file__))


# ===================================================================
# LOAD LOCAL .ENV
# ===================================================================

def load_dotenv():
    """Load local .env values without overriding system variables."""

    env_file = os.path.join(ROOT, ".env")

    if not os.path.exists(env_file):
        return

    with open(env_file, encoding="utf-8") as file:
        for line in file:
            line = line.strip()

            if not line:
                continue

            if line.startswith("#"):
                continue

            if "=" not in line:
                continue

            key, value = line.split("=", 1)

            key = key.strip()
            value = value.strip()

            if (
                len(value) >= 2
                and value[0] == value[-1]
                and value[0] in ("'", '"')
            ):
                value = value[1:-1]

            # Render/system environment variables take priority.
            os.environ.setdefault(key, value)


load_dotenv()


# ===================================================================
# CONNECTION POOL
# ===================================================================

_pool = None


# ===================================================================
# DATABASE CONFIGURATION
# ===================================================================

def _database_config():
    """Build and validate the MySQL configuration."""

    required = [
        "MYSQL_HOST",
        "MYSQL_DATABASE",
        "MYSQL_USER",
        "MYSQL_PASSWORD",
    ]

    missing = [
        key
        for key in required
        if not os.environ.get(key)
    ]

    if missing:
        raise RuntimeError(
            "Missing required database environment variables: "
            + ", ".join(missing)
        )

    # ---------------------------------------------------------------
    # PORT
    # ---------------------------------------------------------------

    try:
        port = int(
            os.environ.get(
                "MYSQL_PORT",
                "3306"
            )
        )
    except (TypeError, ValueError):
        raise RuntimeError(
            "MYSQL_PORT must be a valid number."
        )

    # ---------------------------------------------------------------
    # SSL MODE
    # ---------------------------------------------------------------

    ssl_mode = os.environ.get(
        "MYSQL_SSL_MODE",
        "REQUIRED"
    ).strip().upper()

    valid_ssl_modes = {
        "DISABLED",
        "REQUIRED",
        "VERIFY_CA",
        "VERIFY_IDENTITY",
    }

    if ssl_mode not in valid_ssl_modes:
        raise RuntimeError(
            "MYSQL_SSL_MODE must be one of: "
            + ", ".join(sorted(valid_ssl_modes))
        )

    # ---------------------------------------------------------------
    # BASIC CONNECTION
    # ---------------------------------------------------------------

    config = {
        "host": os.environ["MYSQL_HOST"].strip(),

        "port": port,

        "database": os.environ[
            "MYSQL_DATABASE"
        ].strip(),

        "user": os.environ[
            "MYSQL_USER"
        ].strip(),

        "password": os.environ[
            "MYSQL_PASSWORD"
        ],

        "connection_timeout": 15,

        "autocommit": False,
    }

    # ---------------------------------------------------------------
    # SSL
    # ---------------------------------------------------------------

    if ssl_mode == "DISABLED":
        config["ssl_disabled"] = True

    else:
        # Aiven production connection.
        config["ssl_disabled"] = False

    # ---------------------------------------------------------------
    # OPTIONAL CA CERTIFICATE
    # ---------------------------------------------------------------

    ssl_ca = os.environ.get(
        "MYSQL_SSL_CA"
    )

    if ssl_ca:
        ssl_ca = ssl_ca.strip()

        if not os.path.exists(ssl_ca):
            raise RuntimeError(
                "MYSQL_SSL_CA was supplied but the certificate "
                f"does not exist: {ssl_ca}"
            )

        config["ssl_ca"] = ssl_ca

    # ---------------------------------------------------------------
    # CERTIFICATE VERIFICATION
    # ---------------------------------------------------------------

    if ssl_mode in (
        "VERIFY_CA",
        "VERIFY_IDENTITY",
    ):

        if not ssl_ca:
            raise RuntimeError(
                f"MYSQL_SSL_MODE={ssl_mode} requires "
                "MYSQL_SSL_CA."
            )

        config["ssl_verify_cert"] = True

    # ---------------------------------------------------------------
    # HOSTNAME VERIFICATION
    # ---------------------------------------------------------------

    if ssl_mode == "VERIFY_IDENTITY":
        config["ssl_verify_identity"] = True

    return config


# ===================================================================
# CONNECTION POOL
# ===================================================================

def connection_pool():
    """Return the global MySQL connection pool."""

    global _pool

    if _pool is None:

        config = _database_config()

        _pool = pooling.MySQLConnectionPool(
            pool_name="luxe_pool",
            pool_size=5,
            pool_reset_session=True,
            **config,
        )

    return _pool


# ===================================================================
# GET CONNECTION
# ===================================================================

def get_connection():
    """Get a connection from the MySQL connection pool."""

    return connection_pool().get_connection()


# ===================================================================
# FETCH ALL
# ===================================================================

def fetch_all(sql, params=()):
    """Execute SELECT and return all rows as dictionaries."""

    connection = get_connection()

    try:

        cursor = connection.cursor(
            dictionary=True
        )

        try:

            cursor.execute(
                sql,
                params
            )

            return cursor.fetchall()

        finally:
            cursor.close()

    finally:
        connection.close()


# ===================================================================
# FETCH ONE
# ===================================================================

def fetch_one(sql, params=()):
    """Execute SELECT and return one row."""

    connection = get_connection()

    try:

        cursor = connection.cursor(
            dictionary=True
        )

        try:

            cursor.execute(
                sql,
                params
            )

            return cursor.fetchone()

        finally:
            cursor.close()

    finally:
        connection.close()


# ===================================================================
# EXECUTE
# ===================================================================

def execute(sql, params=()):
    """Execute INSERT, UPDATE, or DELETE."""

    connection = get_connection()

    try:

        cursor = connection.cursor()

        try:

            cursor.execute(
                sql,
                params
            )

            connection.commit()

            return cursor.lastrowid

        except Exception:

            connection.rollback()

            raise

        finally:
            cursor.close()

    finally:
        connection.close()


# ===================================================================
# EXECUTE MANY
# ===================================================================

def execute_many(sql, params_list):
    """Execute a query for multiple parameter sets."""

    connection = get_connection()

    try:

        cursor = connection.cursor()

        try:

            cursor.executemany(
                sql,
                params_list
            )

            connection.commit()

            return cursor.rowcount

        except Exception:

            connection.rollback()

            raise

        finally:
            cursor.close()

    finally:
        connection.close()


# ===================================================================
# READ APPLICATION STATE
# ===================================================================

def read_state(seed_path):
    """Read application state from MySQL.

    If the store does not exist, the seed JSON file is loaded
    and inserted into the database.
    """

    connection = get_connection()

    try:

        cursor = connection.cursor()

        try:

            # -------------------------------------------------------
            # CREATE TABLE
            # -------------------------------------------------------

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS app_state (
                    state_key VARCHAR(40) PRIMARY KEY,
                    state_json JSON NOT NULL,
                    updated_at TIMESTAMP
                        DEFAULT CURRENT_TIMESTAMP
                        ON UPDATE CURRENT_TIMESTAMP
                )
                """
            )

            # -------------------------------------------------------
            # GET STATE
            # -------------------------------------------------------

            cursor.execute(
                """
                SELECT state_json
                FROM app_state
                WHERE state_key = %s
                """,
                ("store",)
            )

            row = cursor.fetchone()

            if row:

                value = row[0]

                if isinstance(value, bytes):
                    value = value.decode("utf-8")

                if isinstance(value, str):
                    return json.loads(value)

                return value

            # -------------------------------------------------------
            # FIRST RUN
            # -------------------------------------------------------

            if not os.path.exists(seed_path):

                raise FileNotFoundError(
                    "Seed file not found: "
                    f"{seed_path}"
                )

            with open(
                seed_path,
                encoding="utf-8"
            ) as seed_file:

                state = json.load(
                    seed_file
                )

            # -------------------------------------------------------
            # INSERT INITIAL STATE
            # -------------------------------------------------------

            cursor.execute(
                """
                INSERT INTO app_state (
                    state_key,
                    state_json
                )
                VALUES (
                    %s,
                    %s
                )
                """,
                (
                    "store",
                    json.dumps(
                        state,
                        ensure_ascii=False
                    ),
                )
            )

            connection.commit()

            return state

        except Exception:

            connection.rollback()

            raise

        finally:
            cursor.close()

    finally:
        connection.close()


# ===================================================================
# WRITE APPLICATION STATE
# ===================================================================

def write_state(state):
    """Write application state to MySQL."""

    connection = get_connection()

    try:

        cursor = connection.cursor()

        try:

            # -------------------------------------------------------
            # CREATE TABLE
            # -------------------------------------------------------

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS app_state (
                    state_key VARCHAR(40) PRIMARY KEY,
                    state_json JSON NOT NULL,
                    updated_at TIMESTAMP
                        DEFAULT CURRENT_TIMESTAMP
                        ON UPDATE CURRENT_TIMESTAMP
                )
                """
            )

            # -------------------------------------------------------
            # INSERT OR UPDATE
            # -------------------------------------------------------

            cursor.execute(
                """
                INSERT INTO app_state (
                    state_key,
                    state_json
                )
                VALUES (
                    %s,
                    %s
                )
                ON DUPLICATE KEY UPDATE
                    state_json = VALUES(state_json)
                """,
                (
                    "store",
                    json.dumps(
                        state,
                        ensure_ascii=False
                    ),
                )
            )

            connection.commit()

        except Exception:

            connection.rollback()

            raise

        finally:
            cursor.close()

    finally:
        connection.close()


# ===================================================================
# DATABASE HEALTH CHECK
# ===================================================================

def check_database_connection():
    """Return True when the production database is reachable."""

    connection = None
    cursor = None

    try:

        connection = get_connection()

        cursor = connection.cursor()

        cursor.execute(
            "SELECT 1"
        )

        result = cursor.fetchone()

        return (
            result is not None
            and result[0] == 1
        )

    except Exception:

        return False

    finally:

        if cursor:

            try:
                cursor.close()
            except Exception:
                pass

        if connection:

            try:
                connection.close()
            except Exception:
                pass


# ===================================================================
# DATABASE INFORMATION
# ===================================================================

def database_info():
    """Return safe database configuration.

    The password is NEVER returned.
    """

    try:

        port = int(
            os.environ.get(
                "MYSQL_PORT",
                "3306"
            )
        )

    except (TypeError, ValueError):

        port = "INVALID"

    return {
        "host": os.environ.get(
            "MYSQL_HOST",
            "NOT SET"
        ),

        "port": port,

        "database": os.environ.get(
            "MYSQL_DATABASE",
            "NOT SET"
        ),

        "user": os.environ.get(
            "MYSQL_USER",
            "NOT SET"
        ),

        "ssl_mode": os.environ.get(
            "MYSQL_SSL_MODE",
            "NOT SET"
        ),
    }


# ===================================================================
# DATABASE DIAGNOSTIC
# ===================================================================

def database_diagnostic():
    """Return safe database connection diagnostics."""

    result = {
        "configuration": database_info(),
        "connection": False,
        "error": None,
    }

    connection = None
    cursor = None

    try:

        connection = get_connection()

        cursor = connection.cursor()

        cursor.execute(
            "SELECT 1"
        )

        row = cursor.fetchone()

        if row and row[0] == 1:
            result["connection"] = True

    except Exception as error:

        result["error"] = str(error)

    finally:

        if cursor:

            try:
                cursor.close()
            except Exception:
                pass

        if connection:

            try:
                connection.close()
            except Exception:
                pass

    return result


# ===================================================================
# CLOSE CONNECTION POOL
# ===================================================================

def close_connection_pool():
    """Reset the MySQL connection pool."""

    global _pool

    _pool = None