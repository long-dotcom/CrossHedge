FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --timeout 120 --retries 10 -r requirements.txt

COPY alembic.ini ./alembic.ini
COPY backend ./backend
COPY config ./config
COPY docker/backend-entrypoint.sh ./docker/backend-entrypoint.sh
RUN chmod +x ./docker/backend-entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["./docker/backend-entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
