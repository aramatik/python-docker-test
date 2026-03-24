import urllib.request
import ssl
import re
import shutil
import os
from duckduckgo_search import DDGS

def search_web(query: str, max_results=5) -> str:
    """Выполняет поиск в DuckDuckGo и возвращает результаты."""
    try:
        results = DDGS().text(query, max_results=max_results)
        if not results:
            return "Ничего не найдено. Попробуй изменить запрос."
        
        output = ""
        for idx, res in enumerate(results):
            output += f"{idx+1}. {res.get('title')}\nURL: {res.get('href')}\nSnippet: {res.get('body')}\n\n"
        return output
    except Exception as e:
        return f"Ошибка поиска: {e}"

def read_url_content(url: str) -> str:
    """Заходит на сайт, очищает HTML от мусора и возвращает текст (до 15000 символов)."""
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
        })
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        
        with urllib.request.urlopen(req, timeout=15, context=ctx) as response:
            html_content = response.read().decode('utf-8', errors='ignore')
            
        # Грубая, но эффективная очистка HTML от скриптов и тегов
        text = re.sub(r'<style.*?</style>', '', html_content, flags=re.DOTALL|re.IGNORECASE)
        text = re.sub(r'<script.*?</script>', '', text, flags=re.DOTALL|re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        
        # Возвращаем 15000 символов, чтобы дотягиваться до ссылок внизу страниц
        return text[:15000] if text else "Сайт пуст или блокирует парсинг."
    except Exception as e:
        return f"Ошибка чтения сайта: {str(e)}"

def download_file(url: str, filepath: str) -> str:
    """Скачивает файл, притворяясь полноценным браузером Chrome."""
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.8,en-US;q=0.5,en;q=0.3',
        })
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        # Создаем папку, если ее нет
        os.makedirs(os.path.dirname(filepath), exist_ok=True)

        with urllib.request.urlopen(req, timeout=30, context=ctx) as response, open(filepath, 'wb') as out_file:
            shutil.copyfileobj(response, out_file)

        return f"Успех! Файл сохранен по пути: {filepath}\nМожешь отправить его пользователю через send_file_to_telegram."
    except Exception as e:
        return f"Ошибка скачивания: {str(e)}"
    
