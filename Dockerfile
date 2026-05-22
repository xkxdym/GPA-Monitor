FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY server.py /app/server.py
COPY sub_sync.py /app/sub_sync.py
COPY web /app/web

RUN mkdir -p /app/data

EXPOSE 8088

CMD ["python", "server.py"]
