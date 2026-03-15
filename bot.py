import os
import subprocess
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import google.generativeai as genai
import html
import re # <-- Добавили для красивого парсинга ошибок

# Загружаем ключи
TG_TOKEN = os.getenv("TG_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

bot = telebot.TeleBot(TG_TOKEN)
genai.configure(api_key=GEMINI_API_KEY)

# Глобальные переменные состояния
AVAILABLE_MODELS = []
CURRENT_MODEL = None
chat_agent = None
model_advisor = None
CURRENT_CHAT_ID = None
PENDING_RETRY_MESSAGE = None # <-- Память для автоматического повтора после 429 ошибки

def format_as_code(text: str) -> str:
    """Оборачивает текст в красивый блок кода с кнопкой копирования"""
    if not text:
        return '<pre><code class="language-bash">Команда выполнена (нет вывода).</code></pre>'
    clean_text = text.replace("```bash", "").replace("```html", "").replace("```", "").strip()
    escaped_text = html.escape(clean_text[:4000])
    return f'<pre><code class="language-bash">{escaped_text}</code></pre>'

def execute_bash(command: str) -> str:
    print(f"Выполнение: {command}")
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
        output = result.stdout if result.stdout else result.stderr
        return output[:2500]
    except Exception as e:
        return f"Ошибка: {str(e)}"

def send_file_to_telegram(filepath: str) -> str:
    global CURRENT_CHAT_ID
    if not CURRENT_CHAT_ID: return "Ошибка: ID чата неизвестен."
    if not os.path.exists(filepath): return f"Ошибка: Файл {filepath} не найден."
    
    try:
        with open(filepath, 'rb') as f:
            bot.send_document(CURRENT_CHAT_ID, f)
        return f"Успех: Файл {filepath} отправлен."
    except Exception as e:
        return f"Ошибка отправки файла: {str(e)}"

def init_models(model_name):
    global chat_agent, model_advisor
    model_agent = genai.GenerativeModel(
        model_name=model_name,
        tools=[execute_bash, send_file_to_telegram],
        system_instruction=(
            "Ты автономный системный администратор Linux. У тебя есть инструменты execute_bash и send_file_to_telegram.\n"
            "ПРАВИЛА:\n"
            "1. Если просят 'пришли', 'скачай' файл - используй ТОЛЬКО send_file_to_telegram.\n"
            "2. Если просят 'покажи текст' файла - читай через execute_bash (cat).\n"
            "3. ТЫ УМЕЕШЬ СЛУШАТЬ АУДИО. Выполняй команды из голосовых сообщений.\n"
            "ВАЖНОЕ ПРАВИЛО ФОРМАТИРОВАНИЯ ОТВЕТА:\n"
            "Твой финальный ответ ВСЕГДА должен содержать разделитель ===SPLIT===.\n"
            "До ===SPLIT===: напиши свои комментарии или пояснения (обычным текстом).\n"
            "После ===SPLIT===: вставь ТОЛЬКО голый вывод терминала или статус файла (СТРОГО БЕЗ markdown разметки и кавычек)."
        )
    )
    chat_agent = model_agent.start_chat(enable_automatic_function_calling=True)
    
    model_advisor = genai.GenerativeModel(
        model_name=model_name,
        system_instruction=(
            "Ты эксперт по Linux. Напиши только готовую bash-команду для решения задачи пользователя. "
            "Ничего не выполняй. Выдавай ТОЛЬКО саму команду, без текста, без markdown разметки и без кавычек."
        )
    )

def get_models_keyboard():
    global AVAILABLE_MODELS
    if not AVAILABLE_MODELS:
        AVAILABLE_MODELS = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
    
    markup = InlineKeyboardMarkup()
    for model_name in AVAILABLE_MODELS:
        clean_name = model_name.replace('models/', '')
        markup.add(InlineKeyboardButton(text=clean_name, callback_data=model_name))
    return markup

# --- Умная обработка ошибок API ---
def handle_api_error(e, chat_id, message_id, original_message, clean_model_name):
    """Парсит ошибку Google API и предлагает смену модели с авто-повтором"""
    error_text = str(e)
    
    # Ищем код 429 или слова о квотах
    if "429" in error_text or "Quota exceeded" in error_text:
        global PENDING_RETRY_MESSAGE
        PENDING_RETRY_MESSAGE = original_message # Запоминаем сообщение!
        
        # Пытаемся вытащить время ожидания (например, "retry in 35.9s")
        delay_match = re.search(r'retry in ([\d\.]+)s', error_text)
        delay_str = f"<b>{float(delay_match.group(1)):.0f} сек.</b>" if delay_match else "некоторое время"
        
        pretty_error = (
            "⚠️ <b>Ошибка 429: Лимиты API исчерпаны!</b>\n\n"
            f"Текущая модель (<code>{clean_model_name}</code>) достигла лимита бесплатного тарифа Google.\n"
            f"⏳ Блокировка этой модели спадет через: {delay_str}\n\n"
            "👇 <b>Выберите резервную модель ниже</b>, и я мгновенно повторю ваш запрос:"
        )
        bot.edit_message_text(chat_id=chat_id, message_id=message_id, 
                              text=pretty_error, parse_mode='HTML', reply_markup=get_models_keyboard())
    else:
        # Если это другая ошибка (не лимиты), выводим как есть
        bot.edit_message_text(chat_id=chat_id, message_id=message_id, 
                              text=f"❌ Ошибка ИИ: {html.escape(error_text)}", parse_mode='HTML')

# --- Хэндлеры команд ---
@bot.message_handler(commands=['start'])
def send_welcome(message):
    if message.from_user.id != ADMIN_ID: return
    bot.reply_to(message, "👋 Привет, Админ!\nВыбери модель Gemini:", reply_markup=get_models_keyboard())

@bot.message_handler(commands=['gemini'])
def change_model(message):
    if message.from_user.id != ADMIN_ID: return
    bot.reply_to(message, "Выберите модель:", reply_markup=get_models_keyboard())

@bot.callback_query_handler(func=lambda call: True)
def handle_query(call):
    if call.from_user.id != ADMIN_ID: return
    
    global CURRENT_MODEL, PENDING_RETRY_MESSAGE
    CURRENT_MODEL = call.data
    try:
        init_models(CURRENT_MODEL)
        clean_name = CURRENT_MODEL.replace('models/', '')
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, 
                              text=f"✅ Выбрана модель: <b>{clean_name}</b>", parse_mode='HTML')
        
        # МАГИЯ: Автоматический повтор запроса!
        if PENDING_RETRY_MESSAGE:
            msg_to_retry = PENDING_RETRY_MESSAGE
            PENDING_RETRY_MESSAGE = None # Очищаем память
            bot.send_message(call.message.chat.id, f"🔄 Повторяю прерванный запрос на новой модели (<b>{clean_name}</b>)...", parse_mode='HTML')
            handle_message(msg_to_retry) # Отправляем сообщение на обработку заново
            
    except Exception as e:
        bot.answer_callback_query(call.id, f"Ошибка инициализации: {e}")

@bot.message_handler(content_types=['voice', 'text'])
def handle_message(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ Доступ запрещен.")
        return

    global CURRENT_CHAT_ID
    CURRENT_CHAT_ID = message.chat.id
    
    if not CURRENT_MODEL:
        bot.reply_to(message, "⚠️ Сначала выберите модель (/gemini)", reply_markup=get_models_keyboard())
        return

    clean_model_name = CURRENT_MODEL.replace('models/', '')
    is_voice = message.content_type == 'voice'
    text = message.text.strip() if message.text else ""

    # Обработка прямых команд (! и #)
    if not is_voice:
        if text.startswith('!'):
            cmd = text[1:].strip()
            bot.send_message(message.chat.id, f"⚡ Выполняю напрямую:\n<code>{html.escape(cmd)}</code>", parse_mode='HTML')
            result = execute_bash(cmd)
            bot.reply_to(message, format_as_code(result), parse_mode='HTML')
            return

        if text.startswith('#'):
            task = text[1:].strip()
            msg_first = bot.send_message(message.chat.id, f"<b>{clean_model_name}:</b>", parse_mode='HTML')
            msg_wait = bot.send_message(message.chat.id, "🧠 Думаю...")
            try:
                response = model_advisor.generate_content(task)
                bot.edit_message_text(chat_id=message.chat.id, message_id=msg_wait.message_id,
                                      text=format_as_code(response.text), parse_mode='HTML')
            except Exception as e:
                # Используем нашу умную обработку ошибок
                handle_api_error(e, message.chat.id, msg_wait.message_id, message, clean_model_name)
            return

    # Отправляем первичные сообщения
    msg_first = bot.send_message(message.chat.id, f"<b>{clean_model_name}:</b>", parse_mode='HTML')
    msg_wait = bot.send_message(message.chat.id, "🤖 Обрабатываю запрос...")

    try:
        if is_voice:
            file_info = bot.get_file(message.voice.file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            voice_path = "temp_voice.ogg"
            with open(voice_path, 'wb') as new_file:
                new_file.write(downloaded_file)
            
            audio_file = genai.upload_file(path=voice_path, mime_type="audio/ogg")
            response = chat_agent.send_message([audio_file, "Слушай аудио и выполни команду."])
            os.remove(voice_path)
        else:
            response = chat_agent.send_message(text)
            
        full_text = response.text
        
        if "===SPLIT===" in full_text:
            parts = full_text.split("===SPLIT===", 1)
            comment = parts[0].strip()
            raw_out = parts[1].strip()
        else:
            comment = ""
            raw_out = full_text.strip()
            
        first_message_text = f"<b>{clean_model_name}:</b>"
        if comment:
            first_message_text += f"\n\n{html.escape(comment)}"
            
        bot.edit_message_text(chat_id=message.chat.id, message_id=msg_first.message_id,
                              text=first_message_text, parse_mode='HTML')
                              
        bot.edit_message_text(chat_id=message.chat.id, message_id=msg_wait.message_id,
                              text=format_as_code(raw_out), parse_mode='HTML')
                              
    except Exception as e:
        # Используем нашу умную обработку ошибок для автономного агента
        handle_api_error(e, message.chat.id, msg_wait.message_id, message, clean_model_name)

if __name__ == '__main__':
    print("AI-Админ запущен. Ожидание команд...")
    bot.polling(none_stop=True)
