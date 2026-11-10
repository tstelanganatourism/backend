import os

# Bind Settings
bind = f"0.0.0.0:{os.getenv('PORT', '8000')}"
backlog = 512

# Worker Process Architecture
# Keep 1 worker by default on 512 MB containers; scale via WEB_CONCURRENCY only on larger instances.
workers = int(os.getenv("WEB_CONCURRENCY", "1"))
worker_class = "uvicorn.workers.UvicornWorker"
worker_connections = 256
timeout = 60
keepalive = 5

# Logging Configurations
errorlog = "-"
loglevel = "info"
accesslog = "-"
access_log_format = '%({X-Request-ID}i)s %({X-Forwarded-For}i)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)sµs'

# Process Naming
proc_name = "ts_tours_fastapi"
