FROM python:3.11-slim

# FFmpeg nativo disponível globalmente no container (primeira camada).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Diretório de cache (monte um volume aqui para persistir entre reinícios).
RUN mkdir -p /app/cache
VOLUME ["/app/cache"]

ENV PORT=7000
EXPOSE 7000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7000"]
