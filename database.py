import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

def get_connection():
    return psycopg2.connect(
        host="localhost",
        port=5432,
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD")
    )

def get_company_info(company_id):
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT name, description
                FROM companies
                WHERE id = %s
                """,
                (company_id,)
            )

            company = cur.fetchone()

            if company:
                return {
                    "name": company[0],
                    "description": company[1] or ""
                }

            return None

    finally:
        conn.close()
