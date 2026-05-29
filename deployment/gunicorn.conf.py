import multiprocessing

# Bind Settings
bind = "127.0.0.1:8000"
backlog = 2048

# Worker Process Architecture
# Recommended: (2 * cores) + 1
workers = (multiprocessing.cpu_count() * 2) + 1
worker_class = "uvicorn.workers.UvicornWorker"
worker_connections = 1000
timeout = 30
keepalive = 5

# Logging Configurations
errorlog = "-"
loglevel = "info"
accesslog = "-"
access_log_format = '%({X-Request-ID}i)s %({X-Forwarded-For}i)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)sµs'

# Process Naming
proc_name = "ts_tours_fastapi"
