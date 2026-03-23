FROM python:3.12-slim

WORKDIR /app

# Устанавливаем системные утилиты и docker.io
RUN apt-get update && apt-get install -y \
    iputils-ping curl wget nano net-tools htop docker.io \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Устанавливаем пакеты из requirements.txt И добавляем новый пакет ddgs
RUN pip install --no-cache-dir -r requirements.txt ddgs

COPY . .

CMD ["python", "bot.py"]
