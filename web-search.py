from duckduckgo_search import DDGS

def search_web(query: str) -> str:
    """
    Ищет актуальную информацию в интернете по запросу пользователя. 
    Возвращает заголовки, сниппеты текста и ссылки.
    """
    print(f"Веб-поиск: {query}")
    try:
        # max_results=5 оптимально, чтобы получить суть и не переполнить контекст ИИ
        results = DDGS().text(query, max_results=5)
        
        if not results:
            return "По вашему запросу ничего не найдено."
        
        formatted_result = f"Результаты поиска в интернете по запросу '{query}':\n\n"
        for i, res in enumerate(results):
            title = res.get('title', 'Без заголовка')
            body = res.get('body', '')
            href = res.get('href', '')
            formatted_result += f"[{i+1}] {title}\n{body}\nСсылка: {href}\n\n"
            
        # Обрезаем до 4000 символов на всякий случай, чтобы не перегрузить модель
        return formatted_result[:4000]
    
    except Exception as e:
        return f"Ошибка при выполнении веб-поиска: {str(e)}"
