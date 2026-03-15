import os
import subprocess
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import google.generativeai as genai
import html

# Загружаем ключи
TG_TOKEN = os.getenv("TG_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

bot = telebot.TeleBot(TG_TOKEN)
genai.configure(api_key=GEMINI_API_KEY)

# Глобальные переменные
AVAILABLE_MODELS = []
CURRENT_MODEL = None
chat_agent = None
model_advisor = None
CURRENT_CHAT_ID = None # Нужен для функции отправки файла

def format_as_code(text: str) -> str:
    """Безопасно оборачивает текст в моноширинный блок кода для Telegram"""
    if not text:
        return "<pre><code>Команда выполнена (нет вывода).</code></pre>"
    escaped_text = html.escape(text[:4000]) # Ограничиваем длину ответа лимитами ТГ
    return f"<pre><code>{escaped_text}</code></pre>"

def execute_bash(command: str) -> str:
    """Выполняет bash-команду в Linux и возвращает результат."""
    print(f"Выполнение: {command}")
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
        output = result.stdout if result.stdout else result.stderr
        return output[:2500]
    except Exception as e:
        return f"Ошибка: {str(e)}"

def send_file_to_telegram(filepath: str) -> str:
    """Отправляет файл пользователю в Telegram как документ."""
    global CURRENT_CHAT_ID
    if not CURRENT_CHAT_ID: return "Ошибка: ID чата неизвестен."
    if not os.path.exists(filepath): return f"Ошибка: Файл {filepath} не найден."
    
    try:
        with open(filepath, 'rb') as f:
            bot.send_document(CURRENT_CHAT_ID, f)
        return f"Успех: Файл {filepath} отправлен пользователю."
    except Exception as e:
        return f"Ошибка отправки файла: {str(e)}"

def init_models(model_name):
    global chat_agent, model_advisor
    model_agent = genai.GenerativeModel(
        model_name=model_name,
        # Теперь у ИИ два инструмента: консоль и отправка файлов!
        tools=[execute_bash, send_file_to_telegram],
        system_instruction=(
            "Ты автономный системный администратор. У тебя есть инструменты execute_bash и send_file_to_telegram. "
            "ПРАВИЛА РАБОТЫ С ФАЙЛАМИ: "
            "1. Если пользователь просит 'пришли', 'скачай' или 'отправь' файл - используй ТОЛЬКО send_file_to_telegram. "
            "2. Если пользователь просит 'покажи текст' или 'выведи содержимое' - читай файл через execute_bash (cat). "
            "Анализируй ошибки и исправляй их автономно."
        )
    )
    chat_agent = model_agent.start_chat(enable_automatic_function_calling=True)
    
    model_advisor = genai.GenerativeModel(
        model_name=model_name,
        system_instruction=(
            "Ты эксперт по Linux. Напиши только готовую bash-команду для решения задачи пользователя. "
            "Ничего не выполняй."
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
    
    global CURRENT_MODEL
    CURRENT_MODEL = call.data
    try:
        init_models(CURRENT_MODEL)
        clean_name = CURRENT_MODEL.replace('models/', '')
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, 
                              text=f"✅ Выбрана модель: <b>{clean_name}</b>", parse_mode='HTML')
    except Exception as e:
        bot.answer_callback_query(call.id, f"Ошибка инициализации: {e}")

# --- Обработка Текста и Голоса ---

# Добавили content_types=['voice', 'text']
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

    # Обработка прямых команд (! и #) работает только для текста
    if not is_voice:
        if text.startswith('!'):
            cmd = text[1:].strip()
            bot.send_message(message.chat.id, f"⚡ Выполняю напрямую:\n<code>{html.escape(cmd)}</code>", parse_mode='HTML')
            result = execute_bash(cmd)
            bot.reply_to(message, format_as_code(result), parse_mode='HTML')
            return

        if text.startswith('#'):
            task = text[1:].strip()
            bot.send_message(message.chat.id, f"<b>{clean_model_name}:</b>", parse_mode='HTML')
            msg_wait = bot.send_message(message.chat.id, "🧠 Думаю...")
            try:
                response = model_advisor.generate_content(task)
                bot.edit_message_text(chat_id=message.chat.id, message_id=msg_wait.message_id,
                                      text=format_as_code(response.text), parse_mode='HTML')
            except Exception as e:
                bot.edit_message_text(chat_id=message.chat.id, message_id=msg_wait.message_id, text=f"❌ Ошибка: {e}")
            return

    # Автономный агент (Текст или Голос)
    
    # Отправляем первое сообщение с именем модели
    bot.send_message(message.chat.id, f"<b>{clean_model_name}:</b>", parse_mode='HTML')
    msg_wait = bot.send_message(message.chat.id, "🤖 Обрабатываю запрос...")

    try:
        if is_voice:
            # Скачиваем голосовое
            file_info = bot.get_file(message.voice.file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            voice_path = "temp_voice.ogg"
            with open(voice_path, 'wb') as new_file:
                new_file.write(downloaded_file)
            
            # Грузим в Gemini
            audio_file = genai.upload_file(path=voice_path)
            
            # Отправляем ИИ аудиофайл + текстовую инструкцию к нему
            response = chat_agent.send_message([audio_file, "Выполни то, что сказано в этом голосовом сообщении."])
            
            # Заметаем следы (удаляем временный файл)
            os.remove(voice_path)
        else:
            response = chat_agent.send_message(text)
            
        bot.edit_message_text(chat_id=message.chat.id, message_id=msg_wait.message_id,
                              text=format_as_code(response.text), parse_mode='HTML')
    except Exception as e:
        bot.edit_message_text(chat_id=message.chat.id, message_id=msg_wait.message_id, text=f"❌ Ошибка выполнения: {e}")

if __name__ == '__main__':
    print("AI-Админ запущен. Ожидание команд...")
    bot.polling(none_stop=True)
        
