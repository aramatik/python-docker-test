FROM python:3.12-slim

WORKDIR /app

# Убрали rhvoice, оставили ffmpeg для конвертации аудио
RUN apt-get update && apt-get install -y \
    iputils-ping curl wget nano net-tools htop docker.io ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Добавили установку edge-tts
RUN pip install --no-cache-dir -r requirements.txt ddgs edge-tts

COPY . .

CMD ["python", "bot.py"]
