FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /opt/overseer

COPY pyproject.toml /opt/overseer/pyproject.toml

RUN python -m pip install --no-cache-dir --upgrade "pip==26.2.1"
RUN python -m pip install --no-cache-dir --group runtime
RUN python -m pip uninstall --yes setuptools

COPY overseer/ /opt/overseer/overseer

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=2)"]

CMD ["gunicorn", "--bind", "0.0.0.0:8765", "--workers", "1", "--threads", "8", "overseer:app"]
