import os
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")


def get_connection():
    """
    Returns a new psycopg connection with dict-style rows.
    Caller is responsible for closing it (we use context managers everywhere).
    """
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)
