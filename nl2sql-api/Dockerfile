FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn psycopg2-binary requests python-dotenv pyyaml

COPY . .

EXPOSE 8000

CMD ["uvicorn", "smart_api:app", "--host", "0.0.0.0", "--port", "8000"]
