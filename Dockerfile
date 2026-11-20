FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/srv/app \
    WORK_DIR=/data/work \
    SQLITE_TMPDIR=/data/tmp \
    TMPDIR=/data/tmp

WORKDIR /srv/app

COPY app/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./

RUN mkdir -p /data/work /data/tmp

EXPOSE 8000

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "--timeout", "600", "app:app"]
