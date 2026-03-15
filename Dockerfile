FROM python:3.10-slim

WORKDIR /app

# Добавили пакет docker.io в список установки
RUN apt-get update && apt-get install -y \
    iputils-ping curl wget nano net-tools htop docker.io \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
