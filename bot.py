import os
import subprocess
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import google.generativeai as genai
import html # <-- Добавили для безопасного экранирования спецсимволов

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

def format_as_code(text: str) -> str:
    """Безопасно оборачивает текст в моноширинный блок кода для Telegram"""
    if not text:
        return "<pre><code>Команда выполнена (нет вывода).</code></pre>"
    # Экранируем символы вроде < и >, чтобы Telegram не принял их за HTML-теги
    escaped_text = html.escape(text)
    return f"<pre><code>{escaped_text}</code></pre>"

def execute_bash(command: str) -> str:
    """Выполняет bash-команду в Linux и возвращает результат."""
    print(f"Выполнение: {command}")
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
        output = result.stdout if result.stdout else result.stderr
        return output[:2500] # Ограничение длины для безопасности
    except Exception as e:
        return f"Ошибка: {str(e)}"

def init_models(model_name):
    global chat_agent, model_advisor
    model_agent = genai.GenerativeModel(
        model_name=model_name,
        tools=[execute_bash],
        system_instruction=(
            "Ты автономный системный администратор. Пользователь дает тебе задачи. "
            "Ты должен сам писать команды, выполнять их через execute_bash, анализировать вывод "
            "и исправлять ошибки, пока задача не будет решена. В конце выдай краткий отчет без лишнего форматирования."
        )
    )
    chat_agent = model_agent.start_chat(enable_automatic_function_calling=True)
    
    model_advisor = genai.GenerativeModel(
        model_name=model_name,
        system_instruction=(
            "Ты эксперт по Linux. Пользователь описывает задачу. Напиши только готовую "
            "bash-команду (или скрипт) для её решения и очень краткое объяснение. "
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
    if message.from_user.id != ADMIN_ID:
        return
    bot.reply_to(message, "👋 Привет, Админ! Я твой кибер-ассистент.\nДавай выберем модель Gemini для работы:", reply_markup=get_models_keyboard())

@bot.message_handler(commands=['gemini'])
def change_model(message):
    if message.from_user.id != ADMIN_ID:
        return
    bot.reply_to(message, "Выберите модель из доступных для вашего API-ключа:", reply_markup=get_models_keyboard())

@bot.callback_query_handler(func=lambda call: True)
def handle_query(call):
    if call.from_user.id != ADMIN_ID:
        return
    
    global CURRENT_MODEL
    CURRENT_MODEL = call.data
    try:
        init_models(CURRENT_MODEL)
        clean_name = CURRENT_MODEL.replace('models/', '')
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, 
                              text=f"✅ Успешно выбрана модель: <b>{clean_name}</b>", parse_mode='HTML')
    except Exception as e:
        bot.answer_callback_query(call.id, f"Ошибка инициализации: {e}")

# --- Обработка текста ---

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ Доступ запрещен. Вы не являетесь администратором.")
        return

    text = message.text.strip()
    
    if not CURRENT_MODEL:
        bot.reply_to(message, "⚠️ Сначала выберите модель с помощью команды /gemini", reply_markup=get_models_keyboard())
        return

    clean_model_name = CURRENT_MODEL.replace('models/', '')
    model_suffix = f"\n\n[Модель: {clean_model_name}]"

    # 1. Прямое выполнение команды (!)
    if text.startswith('!'):
        cmd = text[1:].strip()
        bot.send_message(message.chat.id, f"⚡ Выполняю напрямую:\n<code>{html.escape(cmd)}</code>", parse_mode='HTML')
        result = execute_bash(cmd)
        bot.reply_to(message, format_as_code(result), parse_mode='HTML')
        return

    # 2. Режим подсказки (#)
    if text.startswith('#'):
        task = text[1:].strip()
        msg_wait = bot.send_message(message.chat.id, "🧠 Думаю над командой...")
        try:
            response = model_advisor.generate_content(task)
            bot.edit_message_text(chat_id=message.chat.id, message_id=msg_wait.message_id,
                                  text=format_as_code(response.text + model_suffix), parse_mode='HTML')
        except Exception as e:
            bot.edit_message_text(chat_id=message.chat.id, message_id=msg_wait.message_id, text=f"❌ Ошибка ИИ: {e}")
        return

    # 3. Режим автономного агента (обычный текст)
    msg_wait = bot.send_message(message.chat.id, "🤖 Принял задачу. Ушел в консоль...")
    try:
        response = chat_agent.send_message(text)
        bot.edit_message_text(chat_id=message.chat.id, message_id=msg_wait.message_id,
                              text=format_as_code(response.text + model_suffix), parse_mode='HTML')
    except Exception as e:
        bot.edit_message_text(chat_id=message.chat.id, message_id=msg_wait.message_id, text=f"❌ Ошибка выполнения: {e}")

if __name__ == '__main__':
    print("AI-Админ запущен. Ожидание команд...")
    bot.polling(none_stop=True)
    
