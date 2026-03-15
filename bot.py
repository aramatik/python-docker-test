import os
import subprocess
import telebot
import google.generativeai as genai

# Получаем ключи из скрытых настроек сервера (не из кода!)
TG_TOKEN = os.getenv("TG_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

bot = telebot.TeleBot(TG_TOKEN)
genai.configure(api_key=GEMINI_API_KEY)

# Учим ИИ пользоваться терминалом
def execute_bash(command: str) -> str:
    """Выполняет bash-команду в Linux и возвращает результат или текст ошибки."""
    print(f"ИИ выполняет команду: {command}") # Для логов в Portainer
    try:
        # Запускаем команду, ждем максимум 30 секунд
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
        output = result.stdout if result.stdout else result.stderr
        return output[:2000] # Обрезаем слишком длинный вызов, чтобы не перегрузить память
    except Exception as e:
        return f"Критическая ошибка выполнения: {str(e)}"

# Настраиваем агента
model = genai.GenerativeModel(
    model_name='gemini-1.5-flash',
    tools=[execute_bash],
    system_instruction=(
        "Ты автономный системный администратор. У тебя есть доступ к консоли Linux (Ubuntu/Debian) "
        "через инструмент execute_bash. Пользователь дает тебе задачи. Ты должен сам писать команды, "
        "выполнять их, анализировать вывод и исправлять ошибки, пока задача не будет решена. "
        "После успеха напиши пользователю понятный и краткий отчет о проделанной работе на русском языке."
    )
)

# Включаем магию авто-вызова функций (тот самый цикл ReAct)
chat = model.start_chat(enable_automatic_function_calling=True)

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    bot.send_message(message.chat.id, "🤖 Принял задачу. Ушел в консоль, жди...")
    try:
        # Отправляем задачу ИИ, он сам будет дергать execute_bash сколько нужно раз
        response = chat.send_message(message.text)
        bot.reply_to(message, response.text)
    except Exception as e:
        bot.reply_to(message, f"❌ Упс, что-то пошло не так: {e}")

if __name__ == '__main__':
    print("AI-Админ запущен и готов к работе!")
    bot.polling(none_stop=True)
  
