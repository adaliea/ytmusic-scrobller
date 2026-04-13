FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scrobbler.py .

VOLUME /config
VOLUME /data

CMD ["python", "-u", "scrobbler.py"]
