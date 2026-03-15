import os
import subprocess
import telebot
import google.generativeai as genai

# Загружаем ключи
TG_TOKEN = os.getenv("TG_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0)) # Если ID не задан, доступ будет закрыт всем

bot = telebot.TeleBot(TG_TOKEN)
genai.configure(api_key=GEMINI_API_KEY)

# Инструмент: руки агента
def execute_bash(command: str) -> str:
    """Выполняет bash-команду в Linux и возвращает результат."""
    print(f"Выполнение: {command}")
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
        output = result.stdout if result.stdout else result.stderr
        return output[:2000] # Защита от переполнения сообщения в ТГ
    except Exception as e:
        return f"Ошибка: {str(e)}"

# Модель 1: Полноценный Агент (с руками)
model_agent = genai.GenerativeModel(
    model_name='gemini-1.5-flash',
    tools=[execute_bash],
    system_instruction=(
        "Ты автономный системный администратор. Пользователь дает тебе задачи. "
        "Ты должен сам писать команды, выполнять их через execute_bash, анализировать вывод "
        "и исправлять ошибки, пока задача не будет решена. В конце выдай краткий отчет."
    )
)
chat_agent = model_agent.start_chat(enable_automatic_function_calling=True)

# Модель 2: Советник (без рук)
model_advisor = genai.GenerativeModel(
    model_name='gemini-1.5-flash',
    system_instruction=(
        "Ты эксперт по Linux. Пользователь описывает задачу. Напиши только готовую "
        "bash-команду (или скрипт) для её решения и очень краткое объяснение. "
        "Ничего не выполняй."
    )
)

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    # 1. Жесткий контроль доступа
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ Доступ запрещен. Вы не являетесь администратором.")
        print(f"Попытка доступа от неавторизованного ID: {message.from_user.id}")
        return

    text = message.text.strip()

    # 2. Прямое выполнение команды (начинается с !)
    if text.startswith('!'):
        cmd = text[1:].strip()
        bot.send_message(message.chat.id, f"⚡ Выполняю напрямую:\n`{cmd}`", parse_mode='Markdown')
        result = execute_bash(cmd)
        bot.reply_to(message, f"```bash\n{result}\n```" if result else "Команда выполнена (нет вывода).", parse_mode='Markdown')
        return

    # 3. Режим подсказки (начинается с #)
    if text.startswith('#'):
        task = text[1:].strip()
        bot.send_message(message.chat.id, "🧠 Думаю над командой...")
        try:
            response = model_advisor.generate_content(task)
            bot.reply_to(message, response.text, parse_mode='Markdown')
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка ИИ: {e}")
        return

    # 4. Режим автономного агента (обычный текст)
    bot.send_message(message.chat.id, "🤖 Принял задачу. Ушел в консоль...")
    try:
        response = chat_agent.send_message(text)
        bot.reply_to(message, response.text, parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка выполнения: {e}")

if __name__ == '__main__':
    print("AI-Админ запущен. Ожидание команд...")
    bot.polling(none_stop=True)
    
