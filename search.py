import subprocess
import shlex
import os
import html
import re
from collections import defaultdict

def parse_search_query(query):
    """
    Умный парсер запросов.
    Извлекает точные фразы из квадратных скобок [...] и отдельные слова.
    Например: "Слово1 [фраза два - 43] слово3" -> ['Слово1', 'фраза два - 43', 'слово3']
    """
    # Регулярка ищет: 1) текст в [...] 2) текст в "..." (на всякий случай) 3) обычные слова без пробелов
    pattern = r'\[(.*?)\]|"([^"]*)"|(\S+)'
    matches = re.findall(pattern, query)
    
    terms = []
    for match in matches:
        # match содержит кортеж из 3 элементов, только один из них заполнен
        term = match[0] or match[1] or match[2]
        if term:
            terms.append(term.strip())
            
    return terms

def run_grep_search(terms, base_path="/app/downloads/база/*.csv"):
    """
    Генерирует и выполняет команду grep по переданным аргументам.
    """
    if not terms:
        return ""
        
    # Формируем безопасную bash команду. Флаг -i делает поиск нечувствительным к регистру
    cmd = f"grep -iH {shlex.quote(terms[0])} {base_path} 2>/dev/null"
    for word in terms[1:]:
        cmd += f" | grep -i {shlex.quote(word)}"
        
    # Выполняем с таймаутом 60 секунд
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
    return result.stdout.strip()

def format_search_results(output, terms):
    """
    Группирует сырой вывод bash по файлам и разбивает на безопасные чанки для Telegram.
    Возвращает список чанков (до 4000 символов) и полный чистый текст для файла .txt
    """
    grouped_results = defaultdict(list)
    for line in output.split('\n'):
        if not line.strip(): continue
        parts = line.split(':', 1)
        if len(parts) == 2:
            filepath, match_text = parts
            filename = os.path.basename(filepath)
            grouped_results[filename].append(match_text)
        else:
            grouped_results["Другое"].append(line)

    formatted_chunks = []
    current_chunk = ""

    # Формируем чистый текст для потенциального файла .txt
    clean_text_for_file = f"Результаты поиска по запросу: {' '.join(terms)}\n"
    clean_text_for_file += "=" * 50 + "\n\n"

    for filename, matches in grouped_results.items():
        header = f"📁 <b>{html.escape(filename)}:</b>\n"
        clean_text_for_file += f"=== {filename} ===\n\n"
        
        if len(current_chunk) + len(header) > 4000:
            formatted_chunks.append(current_chunk)
            current_chunk = header
        else:
            current_chunk += header
            
        for match in matches:
            clean_text_for_file += f"{match}\n\n"
            
            # Добавляем \n\n для разделения пустой строкой
            line = f"{html.escape(match)}\n\n" 
            if len(current_chunk) + len(line) > 4000:
                formatted_chunks.append(current_chunk)
                current_chunk = line
            else:
                current_chunk += line
        
        clean_text_for_file += "\n"
        
    if current_chunk.strip():
        formatted_chunks.append(current_chunk)
        
    return formatted_chunks, clean_text_for_file
