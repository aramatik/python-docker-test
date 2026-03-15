FROM python:3.10-slim

WORKDIR /app

# Устанавливаем базовые утилиты Linux для ИИ (чтобы он мог пинговать, качать и т.д.)
RUN apt-get update && apt-get install -y \
    iputils-ping curl wget nano net-tools htop \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
