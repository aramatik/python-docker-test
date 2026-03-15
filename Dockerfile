# Берем официальный минималистичный образ Python
FROM python:3.10-slim

# Создаем рабочую папку внутри контейнера
WORKDIR /app

# Копируем файл с зависимостями и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь остальной наш код
COPY . .

# Указываем, какую команду выполнить при старте контейнера
CMD ["python", "app.py"]
