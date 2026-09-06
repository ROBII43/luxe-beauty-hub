"""MySQL connection helpers. Set MYSQL_HOST, MYSQL_PORT, MYSQL_DATABASE, MYSQL_USER, MYSQL_PASSWORD."""
import os
import mysql.connector
from mysql.connector import pooling
import json

ROOT = os.path.dirname(os.path.abspath(__file__))

def load_dotenv():
    env_file = os.path.join(ROOT, '.env')
    if not os.path.exists(env_file):
        return
    for line in open(env_file, encoding='utf-8'):
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip())

load_dotenv()

_pool = None

def connection_pool():
    global _pool
    if _pool is None:
        _pool = pooling.MySQLConnectionPool(
            pool_name='luxe_pool', pool_size=5,
            host=os.environ.get('MYSQL_HOST', '127.0.0.1'),
            port=int(os.environ.get('MYSQL_PORT', '3306')),
            database=os.environ.get('MYSQL_DATABASE', 'luxe'),
            user=os.environ.get('MYSQL_USER', 'root'),
            password=os.environ.get('MYSQL_PASSWORD', ''),
            connection_timeout=3,
        )
    return _pool

def fetch_all(sql, params=()):
    connection = connection_pool().get_connection()
    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute(sql, params)
        return cursor.fetchall()
    finally:
        cursor.close()
        connection.close()

def read_state(seed_path):
    connection = connection_pool().get_connection()
    try:
        cursor = connection.cursor()
        cursor.execute('CREATE TABLE IF NOT EXISTS app_state (state_key VARCHAR(40) PRIMARY KEY, state_json JSON NOT NULL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP)')
        cursor.execute('SELECT state_json FROM app_state WHERE state_key = %s', ('store',))
        row = cursor.fetchone()
        if row:
            return json.loads(row[0] if isinstance(row[0], str) else row[0].decode())
        with open(seed_path, encoding='utf-8') as seed_file:
            state = json.load(seed_file)
        cursor.execute('INSERT INTO app_state (state_key, state_json) VALUES (%s, %s)', ('store', json.dumps(state, ensure_ascii=False)))
        connection.commit()
        return state
    finally:
        cursor.close()
        connection.close()

def write_state(state):
    connection = connection_pool().get_connection()
    try:
        cursor = connection.cursor()
        cursor.execute('CREATE TABLE IF NOT EXISTS app_state (state_key VARCHAR(40) PRIMARY KEY, state_json JSON NOT NULL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP)')
        cursor.execute('INSERT INTO app_state (state_key, state_json) VALUES (%s, %s) ON DUPLICATE KEY UPDATE state_json = VALUES(state_json)', ('store', json.dumps(state, ensure_ascii=False)))
        connection.commit()
    finally:
        cursor.close()
        connection.close()

def execute(sql, params=()):
    connection = connection_pool().get_connection()
    try:
        cursor = connection.cursor()
        cursor.execute(sql, params)
        connection.commit()
        return cursor.lastrowid
    finally:
        cursor.close()
        connection.close()
