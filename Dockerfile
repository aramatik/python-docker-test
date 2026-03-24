FROM python:3.12-slim

WORKDIR /app

# Устанавливаем системные утилиты, docker, ffmpeg и rhvoice для синтеза речи
RUN apt-get update && apt-get install -y \
    iputils-ping curl wget nano net-tools htop docker.io \
    ffmpeg rhvoice rhvoice-russian \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt ddgs

COPY . .

CMD ["python", "bot.py"]
