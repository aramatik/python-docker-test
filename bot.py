import os
import subprocess
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import google.generativeai as genai
import html
import re

# Загружаем ключи
TG_TOKEN = os.getenv("TG_TOKEN")
API_KEY_1 = os.getenv("GEMINI_API_KEY")
API_KEY_2 = os.getenv("GEMINI2_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

bot = telebot.TeleBot(TG_TOKEN)

# Инициализируем первым ключом по умолчанию
genai.configure(api_key=API_KEY_1)
CURRENT_KEY_NUM = 1

# Глобальные переменные состояния
AVAILABLE_MODELS = []
CURRENT_MODEL = None
chat_agent = None
model_advisor = None
CURRENT_CHAT_ID = None
PENDING_RETRY_MESSAGE = None

# Тот самый приоритетный список
PRIORITY_MODELS = [
    "gemini-2.5-flash",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash-lite-preview-sep-2025",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-robotics-er-1.5-preview",
    "gemma-3-1b",
    "gemma-3-4b",
    "gemma-3-12b",
    "gemma-3-27b",
    "gemma-3n-e4b",
    "gemma-3n-e2b",
    "gemini-2.5-pro"
]

def format_as_code(text: str) -> str:
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

def sort_models_list(raw_models):
    sorted_list = []
    for priority_name in PRIORITY_MODELS:
        for actual_model in raw_models:
            if priority_name.lower() in actual_model.lower() and actual_model not in sorted_list:
                sorted_list.append(actual_model)
    for actual_model in raw_models:
        if actual_model not in sorted_list:
            sorted_list.append(actual_model)
    return sorted_list

def init_models(model_name):
    global chat_agent, model_advisor
    is_gemma = "gemma" in model_name.lower()
    
    if is_gemma:
        model_agent = genai.GenerativeModel(model_name=model_name)
        chat_agent = model_agent.start_chat()
        model_advisor = genai.GenerativeModel(model_name=model_name)
    else:
        # Максимально сжатый и оптимизированный промпт для админа
        model_agent = genai.GenerativeModel(
            model_name=model_name,
            tools=[execute_bash, send_file_to_telegram],
            system_instruction=(
                "Ты root-админ Debian. Инструменты: execute_bash, send_file_to_telegram.\n"
                "1. Пакеты: используй apt/apt-get. Ты root, sudo не нужен.\n"
                "2. Отправка файлов: ТОЛЬКО send_file_to_telegram. Чтение: cat.\n"
                "3. Ты слышишь аудио.\n"
                "ФОРМАТ ОТВЕТА СТРОГО:\n"
                "Комментарии\n"
                "===SPLIT===\n"
                "Голый вывод терминала (БЕЗ markdown/кавычек)."
            )
        )
        chat_agent = model_agent.start_chat(enable_automatic_function_calling=True)
        
        model_advisor = genai.GenerativeModel(
            model_name=model_name,
            system_instruction="Ты root-админ Debian. Дай только bash-команду (через apt-get, без sudo). Без markdown и пояснений."
        )

def get_models_keyboard():
    global AVAILABLE_MODELS
    if not AVAILABLE_MODELS:
        raw_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        AVAILABLE_MODELS = sort_models_list(raw_models)
    
    markup = InlineKeyboardMarkup()
    for model_name in AVAILABLE_MODELS:
        clean_name = model_name.replace('models/', '')
        markup.add(InlineKeyboardButton(text=clean_name, callback_data=f"mod_{model_name}"))
    return markup

def handle_api_error(e, chat_id, message_id, original_message, clean_model_name):
    error_text = str(e)
    if "429" in error_text or "Quota exceeded" in error_text:
        global PENDING_RETRY_MESSAGE
        PENDING_RETRY_MESSAGE = original_message 
        
        delay_match = re.search(r'retry in ([\d\.]+)s', error_text)
        delay_str = f"<b>{float(delay_match.group(1)):.0f} сек.</b>" if delay_match else "некоторое время"
        
        pretty_error = (
            f"⚠️ <b>Ошибка 429: Лимит API для KEY {CURRENT_KEY_NUM} исчерпан!</b>\n\n"
            f"Модель <code>{clean_model_name}</code> уперлась в квоту.\n"
            f"⏳ Блокировка спадет через: {delay_str}\n\n"
            "👇 Выберите другую модель ниже, либо смените ключ командой /changekey"
        )
        bot.edit_message_text(chat_id=chat_id, message_id=message_id, 
                              text=pretty_error, parse_mode='HTML', reply_markup=get_models_keyboard())
    else:
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

@bot.message_handler(commands=['changekey'])
def change_key_cmd(message):
    if message.from_user.id != ADMIN_ID: return
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton(text="🔑 KEY 1" + (" (Активен)" if CURRENT_KEY_NUM == 1 else ""), callback_data="key_1"),
        InlineKeyboardButton(text="🔑 KEY 2" + (" (Активен)" if CURRENT_KEY_NUM == 2 else ""), callback_data="key_2")
    )
    bot.reply_to(message, "Выберите API-ключ для работы:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def handle_query(call):
    if call.from_user.id != ADMIN_ID: return
    
    global CURRENT_MODEL, PENDING_RETRY_MESSAGE, CURRENT_KEY_NUM, AVAILABLE_MODELS
    data = call.data
    
    if data.startswith("key_"):
        key_num = int(data.split("_")[1])
        target_key = API_KEY_1 if key_num == 1 else API_KEY_2
        
        if not target_key:
            bot.answer_callback_query(call.id, f"❌ KEY {key_num} не задан в переменных!", show_alert=True)
            return
            
        CURRENT_KEY_NUM = key_num
        genai.configure(api_key=target_key)
        AVAILABLE_MODELS = [] 
        CURRENT_MODEL = None
        
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, 
                              text=f"✅ Активен <b>KEY {key_num}</b>.\nТеперь выберите модель /gemini", parse_mode='HTML')
        return

    if data.startswith("mod_"):
        model_name = data.replace("mod_", "")
        CURRENT_MODEL = model_name
        try:
            init_models(CURRENT_MODEL)
            clean_name = CURRENT_MODEL.replace('models/', '')
            
            is_gemma = "gemma" in clean_name.lower()
            mode_text = "(Режим Чатбота)" if is_gemma else "(Режим Админа)"
            
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, 
                                  text=f"✅ Выбрана модель: <b>{clean_name}</b> {mode_text}", parse_mode='HTML')
            
            if PENDING_RETRY_MESSAGE:
                msg_to_retry = PENDING_RETRY_MESSAGE
                PENDING_RETRY_MESSAGE = None 
                bot.send_message(call.message.chat.id, f"🔄 Повторяю прерванный запрос на <b>{clean_name}</b>...", parse_mode='HTML')
                handle_message(msg_to_retry) 
                
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
    is_gemma = "gemma" in clean_model_name.lower()
    is_voice = message.content_type == 'voice'
    text = message.text.strip() if message.text else ""

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
                handle_api_error(e, message.chat.id, msg_wait.message_id, message, clean_model_name)
            return

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
            
            if is_gemma:
                 response = chat_agent.send_message("Я получил аудиофайл, но я модель Gemma и не умею слушать звук.")
            else:
                 response = chat_agent.send_message([audio_file, "Слушай аудио и выполни команду."])
                 
            os.remove(voice_path)
        else:
            response = chat_agent.send_message(text)
            
        full_text = response.text
        
        if is_gemma:
            # Пытаемся отправить с красивым Markdown, если ИИ выдал кривую разметку - падаем в безопасный HTML
            try:
                bot.edit_message_text(chat_id=message.chat.id, message_id=msg_first.message_id,
                                      text=f"*{clean_model_name} (Чат):*\n\n{full_text}", parse_mode='Markdown')
            except Exception:
                bot.edit_message_text(chat_id=message.chat.id, message_id=msg_first.message_id,
                                      text=f"<b>{clean_model_name} (Чат):</b>\n\n{html.escape(full_text)}", parse_mode='HTML')
            bot.delete_message(chat_id=message.chat.id, message_id=msg_wait.message_id)
        else:
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
        handle_api_error(e, message.chat.id, msg_wait.message_id, message, clean_model_name)

if __name__ == '__main__':
    print("AI-Админ запущен. Ожидание команд...")
    bot.polling(none_stop=True)
    
