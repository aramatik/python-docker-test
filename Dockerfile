FROM python:3.10-slim

WORKDIR /app

# Устанавливаем системные утилиты и docker.io
RUN apt-get update && apt-get install -y \
    iputils-ping curl wget nano net-tools htop docker.io \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Устанавливаем пакеты из requirements.txt И добавляем duckduckgo-search для работы интернета
RUN pip install --no-cache-dir -r requirements.txt duckduckgo-search

COPY . .

CMD ["python", "bot.py"]
