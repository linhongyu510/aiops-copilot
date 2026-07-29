# Local read-only MySQL demo

Start the seeded observability database:

```powershell
docker compose -f demo-mysql.yml up -d
```

Add this local-only DSN to `.env`:

```dotenv
MYSQL_DSN=mysql+pymysql://oncall_reader:oncall_reader_demo@127.0.0.1:3307/observability
MYSQL_ALLOWED_SCHEMAS=observability
```

The init script revokes the image default privileges and grants `SELECT` only. The credentials are intentionally public and must never be reused outside this local demo.
