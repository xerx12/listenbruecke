FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LISTENBRUECKE_DATA=/data
WORKDIR /app
RUN groupadd --gid 1000 listenbruecke && useradd --uid 1000 --gid 1000 --no-create-home listenbruecke
COPY pyproject.toml README.md LICENSE THIRD_PARTY_LICENSES.md requirements.lock ./
COPY src ./src
COPY web ./web
COPY browser-helper ./browser-helper
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps .
COPY entrypoint.py ./entrypoint.py
EXPOSE 8099
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8099/healthz', timeout=3)"
ENTRYPOINT ["python", "/app/entrypoint.py"]
