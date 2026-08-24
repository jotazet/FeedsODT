FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Warsaw

WORKDIR /app

# Zależności osobną warstwą — zmiana kodu nie unieważnia cache pip-a.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY feedsodt/ ./feedsodt/
COPY config/ ./config/

# Katalog wyjściowy jest montowany z hosta; UID 1000 odpowiada typowemu
# użytkownikowi, więc pliki na hoście nie należą do roota.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin feedsodt \
    && mkdir -p /data/gtfs \
    && chown -R feedsodt:feedsodt /data
USER feedsodt

ENV FEEDSODT_OUTPUT_DIR=/data/gtfs \
    FEEDSODT_SOURCES=/app/config/sources.toml \
    FEEDSODT_RUN_AT=00:30 \
    FEEDSODT_RUN_ON_START=true

# Zdrowy kontener to taki, który ma co serwować.
HEALTHCHECK --interval=5m --timeout=10s --start-period=10m --retries=3 \
    CMD python -c "import json,sys,os; p=os.environ['FEEDSODT_OUTPUT_DIR']+'/index.json'; d=json.load(open(p)); sys.exit(0 if d.get('feeds') else 1)"

ENTRYPOINT ["python", "-m", "feedsodt"]
CMD ["daemon"]
