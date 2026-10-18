FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY engine.py main.py rules.json cases.json library.json ./
COPY static ./static
COPY samples/*.png ./samples/
RUN useradd -m app
USER app
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}
