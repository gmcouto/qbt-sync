FROM python:3.13-alpine

WORKDIR /app

COPY pyproject.toml requirements.txt ./
COPY qbittorrent_sync/ qbittorrent_sync/

RUN pip install --no-cache-dir .

CMD ["qbt-sync", "--daemon", "--verbose"]
