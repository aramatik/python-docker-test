import os
import subprocess
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import google.generativeai as genai

# Загружаем ключи
TG_TOKEN = os.getenv("TG_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

bot = telebot.TeleBot(TG_TOKEN)
genai.configure(api_key=GEMINI_API_KEY)

# Глобальные переменные для хранения состояния (для одного админа работает идеально)
AVAILABLE_MODELS = []
CURRENT_MODEL = None
chat_agent = None
model_advisor = None

def execute_bash(command: str) -> str:
    """Выполняет bash-команду в Linux и возвращает результат."""
    print(f"Выполнение: {command}")
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
        output = result.stdout if result.stdout else result.stderr
        return output[:2000] # Защита от переполнения
    except Exception as e:
        return f"Ошибка: {str(e)}"

def init_models(model_name):
    """Инициализирует агентов с выбранной моделью"""
    global chat_agent, model_advisor
    
    # Модель 1: Полноценный Агент
    model_agent = genai.GenerativeModel(
        model_name=model_name,
        tools=[execute_bash],
        system_instruction=(
            "Ты автономный системный администратор. Пользователь дает тебе задачи. "
            "Ты должен сам писать команды, выполнять их через execute_bash, анализировать вывод "
            "и исправлять ошибки, пока задача не будет решена. В конце выдай краткий отчет."
        )
    )
    # Включаем функцию авто-вызова (руки)
    chat_agent = model_agent.start_chat(enable_automatic_function_calling=True)
    
    # Модель 2: Советник (без рук)
    model_advisor = genai.GenerativeModel(
        model_name=model_name,
        system_instruction=(
            "Ты эксперт по Linux. Пользователь описывает задачу. Напиши только готовую "
            "bash-команду (или скрипт) для её решения и очень краткое объяснение. "
            "Ничего не выполняй."
        )
    )

def get_models_keyboard():
    """Стучится в Google API, получает список доступных моделей и собирает кнопки"""
    global AVAILABLE_MODELS
    if not AVAILABLE_MODELS:
        # Фильтруем только те модели, которые умеют генерировать текст
        AVAILABLE_MODELS = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
    
    markup = InlineKeyboardMarkup()
    for model_name in AVAILABLE_MODELS:
        # Убираем префикс models/ для красоты на кнопке
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

# --- Обработка нажатий на кнопки ---

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
                              text=f"✅ Успешно выбрана модель: **{clean_name}**", parse_mode='Markdown')
    except Exception as e:
        bot.answer_callback_query(call.id, f"Ошибка инициализации: {e}")

# --- Обработка текста ---

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    # Жесткий контроль доступа
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ Доступ запрещен. Вы не являетесь администратором.")
        return

    text = message.text.strip()
    
    # Если модель еще не выбрана, блокируем работу
    if not CURRENT_MODEL:
        bot.reply_to(message, "⚠️ Сначала выберите модель с помощью команды /gemini", reply_markup=get_models_keyboard())
        return

    # Красивая подпись для ответов
    clean_model_name = CURRENT_MODEL.replace('models/', '')
    model_suffix = f"\n\n*(Модель: {clean_model_name})*"

    # 1. Прямое выполнение команды (!)
    if text.startswith('!'):
        cmd = text[1:].strip()
        bot.send_message(message.chat.id, f"⚡ Выполняю напрямую:\n`{cmd}`", parse_mode='Markdown')
        result = execute_bash(cmd)
        bot.reply_to(message, f"```bash\n{result}\n```" if result else "Команда выполнена (нет вывода).", parse_mode='Markdown')
        return

    # 2. Режим подсказки (#)
    if text.startswith('#'):
        task = text[1:].strip()
        bot.send_message(message.chat.id, "🧠 Думаю над командой...")
        try:
            response = model_advisor.generate_content(task)
            bot.reply_to(message, response.text + model_suffix, parse_mode='Markdown')
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка ИИ: {e}")
        return

    # 3. Режим автономного агента (обычный текст)
    bot.send_message(message.chat.id, "🤖 Принял задачу. Ушел в консоль...")
    try:
        response = chat_agent.send_message(text)
        bot.reply_to(message, response.text + model_suffix, parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка выполнения: {e}")

if __name__ == '__main__':
    print("AI-Админ запущен. Ожидание команд...")
    bot.polling(none_stop=True)
    
