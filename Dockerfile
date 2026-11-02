FROM python:3.15-slim@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913

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
