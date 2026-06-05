FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY imap_api/ ./imap_api/

VOLUME ["/data"]

EXPOSE 8000

CMD ["python", "-m", "imap_api.main"]
