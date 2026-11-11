"""Run one SQL migration file against the live database.

    python scripts/run_migration.py ../supabase/migrations/<file>.sql [table ...]

Reads SUPABASE_DB_URL from backend/.env (never prints it), runs the file in
a single transaction (all of it applies or none of it), then reports whether
each table named afterwards exists.

The URL in .env is Supabase's "direct connection" (db.<ref>.supabase.co),
which is IPv6-only and unreachable from most home networks. When
SUPABASE_DB_POOLER_HOST is set, the same credentials are used through the
session pooler instead (user becomes postgres.<ref>). The password is passed
as a separate argument, so special characters in it need no URL-encoding.
"""
import re
import sys
from pathlib import Path

import psycopg


def _env() -> dict:
    out = {}
    for line in (Path(__file__).resolve().parents[1] / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def connect():
    env = _env()
    url = env.get("SUPABASE_DB_URL")
    if not url:
        raise SystemExit("SUPABASE_DB_URL is not set in backend/.env")
    m = re.match(r"^postgres(?:ql)?://([^:/@]+):(.*)@([^@/:]+):(\d+)/([^?]+)", url)
    if not m:
        raise SystemExit("SUPABASE_DB_URL is not in the form postgresql://user:password@host:port/db")
    user, password, host, port, db = m.groups()
    pooler = env.get("SUPABASE_DB_POOLER_HOST")
    direct = re.match(r"^db\.([a-z0-9]+)\.supabase\.co$", host)
    if pooler and direct:
        host, port, user = pooler, "5432", f"postgres.{direct.group(1)}"
    try:
        return psycopg.connect(host=host, port=int(port), dbname=db, user=user, password=password, connect_timeout=20, sslmode="require")
    except Exception as e:
        raise SystemExit(f"Could not connect: {type(e).__name__}: {str(e).splitlines()[0]}")


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    path = Path(sys.argv[1])
    sql = path.read_text(encoding="utf-8")
    tables = sys.argv[2:]
    conn = connect()
    with conn:
        with conn.cursor() as cur:
            cur.execute("select current_database(), current_user")
            print("connected:", cur.fetchone())
            cur.execute(sql)
            print(f"applied {path.name}")
            for t in tables:
                cur.execute("select to_regclass(%s)", (f"public.{t}",))
                print(f"  {t}: {'exists' if cur.fetchone()[0] else 'MISSING'}")
    conn.close()


if __name__ == "__main__":
    main()
