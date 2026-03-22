import os
import subprocess
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import google.generativeai as genai
import html
import re
import shlex
from collections import defaultdict

# Загружаем ключи
TG_TOKEN = os.getenv("TG_TOKEN")
API_KEY_1 = os.getenv("GEMINI_API_KEY")
API_KEY_2 = os.getenv("GEMINI2_API_KEY")
API_KEY_3 = os.getenv("GEMINI3_API_KEY")

# Безопасно собираем все ID админов в множество
ADMIN_IDS = set()
for env_var in ["ADMIN_ID", "ADMIN2_ID", "ADMIN3_ID"]:
    val = os.getenv(env_var)
    if val and val.strip().lstrip('-').isdigit():
        ADMIN_IDS.add(int(val.strip()))

if not ADMIN_IDS:
    print("ВНИМАНИЕ: Не задано ни одного ADMIN_ID! Бот никого не пустит.")

bot = telebot.TeleBot(TG_TOKEN)

genai.configure(api_key=API_KEY_1)
CURRENT_KEY_NUM = 1

AVAILABLE_MODELS = []
PRIORITY_MODELS_CACHE = []
OTHER_MODELS_CACHE = []
CURRENT_MODEL = None
chat_agent = None
model_advisor = None
CURRENT_CHAT_ID = None
PENDING_RETRY_MESSAGE = None
PENDING_FILES = {}

# Ваш идеальный порядок:
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

def log_admin_action(user_id, action):
    """Логирует действия админов в консоль"""
    print(f"[ADMIN {user_id}] {action}")

# --- УМНЫЙ ПРЕДОХРАНИТЕЛЬ ДЛЯ ТЕЛЕГРАМА ---
def safe_edit_message(chat_id, message_id, text, parse_mode='HTML', reply_markup=None):
    """Редактирует сообщение, игнорируя ошибку, если текст не изменился"""
    try:
        bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, 
                              parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            raise e

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

def get_models_lists():
    raw_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
    priority = []
    other = []
    used_models = set()
    
    for p in PRIORITY_MODELS:
        search_str = p.lower().replace("sep-2025", "09-2025")
        best_match = None
        
        for m in raw_models:
            if m in used_models: continue
            clean_m = m.replace('models/', '').lower()
            
            if clean_m == search_str or clean_m == f"{search_str}-it":
                best_match = m
                break
            elif search_str in clean_m:
                if "tts" in clean_m and "tts" not in search_str: continue
                if "image" in clean_m and "image" not in search_str: continue
                if not best_match:
                    best_match = m
                    
        if best_match:
            priority.append(best_match)
            used_models.add(best_match)
            
    for m in raw_models:
        if m not in used_models:
            other.append(m)
            
    return priority, other

def get_models_keyboard(show_all=False):
    global PRIORITY_MODELS_CACHE, OTHER_MODELS_CACHE, AVAILABLE_MODELS
    if not PRIORITY_MODELS_CACHE and not OTHER_MODELS_CACHE:
        PRIORITY_MODELS_CACHE, OTHER_MODELS_CACHE = get_models_lists()
        AVAILABLE_MODELS = PRIORITY_MODELS_CACHE + OTHER_MODELS_CACHE
        
    markup = InlineKeyboardMarkup()
    for model_name in PRIORITY_MODELS_CACHE:
        clean_name = model_name.replace('models/', '')
        markup.add(InlineKeyboardButton(text=clean_name, callback_data=f"mod_{model_name}"))
        
    if show_all:
        for model_name in OTHER_MODELS_CACHE:
            clean_name = model_name.replace('models/', '')
            markup.add(InlineKeyboardButton(text=clean_name, callback_data=f"mod_{model_name}"))
    else:
        if OTHER_MODELS_CACHE:
            markup.add(InlineKeyboardButton(text="⬇️ Другие модели", callback_data="show_all_mods"))
            
    return markup

def init_models(model_name):
    global chat_agent, model_advisor
    is_gemma = "gemma" in model_name.lower()
    
    if is_gemma:
        model_agent = genai.GenerativeModel(model_name=model_name)
        chat_agent = model_agent.start_chat()
        model_advisor = genai.GenerativeModel(model_name=model_name)
    else:
        model_agent = genai.GenerativeModel(
            model_name=model_name,
            tools=[execute_bash, send_file_to_telegram],
            system_instruction=(
                "Ты root-админ Debian. Инструменты: execute_bash, send_file_to_telegram.\n"
                "1. Пакеты: используй apt/apt-get. Ты root, sudo не нужен.\n"
                "2. Отправка файлов: ТОЛЬКО send_file_to_telegram. Чтение: cat.\n"
                "3. Ты слышишь аудио и читаешь файлы.\n"
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
        safe_edit_message(chat_id, message_id, pretty_error, reply_markup=get_models_keyboard())
    else:
        safe_edit_message(chat_id, message_id, f"❌ Ошибка ИИ: {html.escape(error_text)}")

@bot.message_handler(commands=['start'])
def send_welcome(message):
    if message.from_user.id not in ADMIN_IDS: return
    log_admin_action(message.from_user.id, "Команда /start")
    bot.reply_to(message, "👋 Привет, Админ!\nВыбери модель Gemini:", reply_markup=get_models_keyboard())

@bot.message_handler(commands=['gemini'])
def change_model(message):
    if message.from_user.id not in ADMIN_IDS: return
    log_admin_action(message.from_user.id, "Команда /gemini")
    bot.reply_to(message, "Выберите модель:", reply_markup=get_models_keyboard())

@bot.message_handler(commands=['changekey'])
def change_key_cmd(message):
    if message.from_user.id not in ADMIN_IDS: return
    log_admin_action(message.from_user.id, "Команда /changekey")
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton(text="🔑 KEY 1" + (" (Активен)" if CURRENT_KEY_NUM == 1 else ""), callback_data="key_1"),
        InlineKeyboardButton(text="🔑 KEY 2" + (" (Активен)" if CURRENT_KEY_NUM == 2 else ""), callback_data="key_2"),
        InlineKeyboardButton(text="🔑 KEY 3" + (" (Активен)" if CURRENT_KEY_NUM == 3 else ""), callback_data="key_3")
    )
    bot.reply_to(message, "Выберите API-ключ для работы:", reply_markup=markup)

@bot.message_handler(commands=['clear'])
def clear_cmd(message):
    if message.from_user.id not in ADMIN_IDS: return
    log_admin_action(message.from_user.id, "Команда /clear")
    
    global chat_agent, CURRENT_MODEL
    
    if not CURRENT_MODEL:
        bot.reply_to(message, "⚠️ Модель еще не выбрана. Память пуста.")
        return
        
    try:
        init_models(CURRENT_MODEL)
        bot.reply_to(message, "🧹 Контекст и память ИИ успешно очищены!")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка очистки памяти: {e}")

@bot.message_handler(commands=['search'])
def search_cmd(message):
    if message.from_user.id not in ADMIN_IDS: return
    log_admin_action(message.from_user.id, "Команда /search")
    msg = bot.reply_to(message, "Введите поисковый запрос (можно использовать несколько слов):")
    bot.register_next_step_handler(msg, process_search_query)

def process_search_query(message):
    if message.from_user.id not in ADMIN_IDS: return
    
    query = message.text.strip()
    if not query:
        bot.reply_to(message, "Пустой запрос. Поиск отменен.")
        return
        
    log_admin_action(message.from_user.id, f"Поиск: {query}")
    
    try:
        words = shlex.split(query)
    except ValueError:
        words = query.split()
        
    if not words:
        return

    # Добавлен флаг -H для принудительного вывода имени файла
    cmd = f"grep -iH {shlex.quote(words[0])} /app/downloads/база/*.csv 2>/dev/null"
    for word in words[1:]:
        cmd += f" | grep -i {shlex.quote(word)}"
    
    msg_wait = bot.send_message(message.chat.id, f"🔍 Ищу в базе: <code>{' | '.join(words)}</code>...", parse_mode='HTML')
    
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        output = result.stdout.strip()
        
        if not output:
            safe_edit_message(message.chat.id, msg_wait.message_id, "🤷‍♂️ По вашему запросу ничего не найдено.")
            return
            
        bot.delete_message(message.chat.id, msg_wait.message_id)
        
        # Группируем результаты по именам файлов
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

        # Умная разбивка на сообщения с лимитом 4000 символов
        formatted_chunks = []
        current_chunk = ""

        for filename, matches in grouped_results.items():
            header = f"📁 <b>{html.escape(filename)}:</b>\n"
            
            if len(current_chunk) + len(header) > 4000:
                formatted_chunks.append(current_chunk)
                current_chunk = header
            else:
                current_chunk += header
                
            for match in matches:
                # Добавлен \n\n для создания пустой строки между результатами
                line = f"{html.escape(match)}\n\n" 
                if len(current_chunk) + len(line) > 4000:
                    formatted_chunks.append(current_chunk)
                    current_chunk = line
                else:
                    current_chunk += line
            
        if current_chunk.strip():
            formatted_chunks.append(current_chunk)
            
        # Отправляем максимум 5 сообщений
        for chunk in formatted_chunks[:5]:
            bot.send_message(message.chat.id, f"<pre>{chunk.strip()}</pre>", parse_mode='HTML')
            
        if len(formatted_chunks) > 5:
            bot.send_message(message.chat.id, f"⚠️ <b>Внимание:</b> Показано 5 сообщений из {len(formatted_chunks)}. Остальной текст обрезан. Попробуйте сузить параметры поиска.", parse_mode='HTML')
            
    except subprocess.TimeoutExpired:
        safe_edit_message(message.chat.id, msg_wait.message_id, "❌ Ошибка: Превышено время ожидания поиска (более 60 секунд).")
    except Exception as e:
        safe_edit_message(message.chat.id, msg_wait.message_id, f"❌ Ошибка поиска: {html.escape(str(e))}")

@bot.message_handler(content_types=['document'])
def handle_document(message):
    if message.from_user.id not in ADMIN_IDS: return
    log_admin_action(message.from_user.id, f"Загрузил документ: {message.document.file_name}")
    
    global CURRENT_CHAT_ID
    CURRENT_CHAT_ID = message.chat.id
    
    file_id = message.document.file_id
    file_name = message.document.file_name
    mime_type = message.document.mime_type
    
    PENDING_FILES[message.chat.id] = {
        'file_id': file_id,
        'file_name': file_name,
        'mime_type': mime_type
    }
    
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("✅ Да", callback_data="file_yes"),
        InlineKeyboardButton("❌ Нет", callback_data="file_no")
    )
    markup.row(
        InlineKeyboardButton("🧠 Обработать ИИ", callback_data="file_ai")
    )
    
    bot.reply_to(message, f"📥 Загрузить файл <b>{html.escape(file_name)}</b> на сервер?", reply_markup=markup, parse_mode='HTML')

@bot.callback_query_handler(func=lambda call: True)
def handle_query(call):
    if call.from_user.id not in ADMIN_IDS: return
    log_admin_action(call.from_user.id, f"Callback: {call.data}")
    
    global CURRENT_MODEL, PENDING_RETRY_MESSAGE, CURRENT_KEY_NUM, PRIORITY_MODELS_CACHE, OTHER_MODELS_CACHE, AVAILABLE_MODELS
    data = call.data
    
    if data == "show_all_mods":
        safe_edit_message(call.message.chat.id, call.message.message_id, 
                          "Выберите модель (Полный список):", 
                          reply_markup=get_models_keyboard(show_all=True))
        return

    if data.startswith("key_"):
        key_num = int(data.split("_")[1])
        
        if key_num == 1:
            target_key = API_KEY_1
        elif key_num == 2:
            target_key = API_KEY_2
        elif key_num == 3:
            target_key = API_KEY_3
        else:
            target_key = None
            
        if not target_key:
            bot.answer_callback_query(call.id, f"❌ KEY {key_num} не задан в переменных!", show_alert=True)
            return
            
        CURRENT_KEY_NUM = key_num
        genai.configure(api_key=target_key)
        
        PRIORITY_MODELS_CACHE = []
        OTHER_MODELS_CACHE = []
        AVAILABLE_MODELS = []
        CURRENT_MODEL = None
        
        safe_edit_message(call.message.chat.id, call.message.message_id, 
                          f"✅ Активен <b>KEY {key_num}</b>.\nТеперь выберите модель /gemini")
        return

    if data.startswith("mod_"):
        model_name = data.replace("mod_", "")
        CURRENT_MODEL = model_name
        try:
            init_models(CURRENT_MODEL)
            clean_name = CURRENT_MODEL.replace('models/', '')
            
            is_gemma = "gemma" in clean_name.lower()
            mode_text = "(Режим Чатбота)" if is_gemma else "(Режим Админа)"
            
            safe_edit_message(call.message.chat.id, call.message.message_id, 
                              f"✅ Выбрана модель: <b>{clean_name}</b> {mode_text}")
            
            if PENDING_RETRY_MESSAGE:
                msg_to_retry = PENDING_RETRY_MESSAGE
                PENDING_RETRY_MESSAGE = None 
                bot.send_message(call.message.chat.id, f"🔄 Повторяю прерванный запрос на <b>{clean_name}</b>...", parse_mode='HTML')
                handle_message(msg_to_retry) 
                
        except Exception as e:
            bot.answer_callback_query(call.id, f"Ошибка инициализации: {e}")
        return

    if data in ["file_yes", "file_no", "file_ai"]:
        file_info_dict = PENDING_FILES.get(call.message.chat.id)
        
        if data == "file_no":
            safe_edit_message(call.message.chat.id, call.message.message_id, "❌ Операция с файлом отменена.")
            PENDING_FILES.pop(call.message.chat.id, None)
            return
            
        if not file_info_dict:
            bot.answer_callback_query(call.id, "❌ Файл устарел или не найден в памяти.", show_alert=True)
            return

        if data == "file_yes":
            safe_edit_message(call.message.chat.id, call.message.message_id, "⏳ Сохраняю файл на сервер...")
            try:
                file_info = bot.get_file(file_info_dict['file_id'])
                downloaded_file = bot.download_file(file_info.file_path)
                
                download_dir = "/app/downloads"
                os.makedirs(download_dir, exist_ok=True)
                
                save_path = os.path.join(download_dir, file_info_dict['file_name'])
                with open(save_path, 'wb') as new_file:
                    new_file.write(downloaded_file)
                    
                safe_edit_message(
                    call.message.chat.id, 
                    call.message.message_id, 
                    f"✅ Файл <b>{html.escape(file_info_dict['file_name'])}</b> успешно сохранен!\n\n"
                    f"📁 Путь в боте: <code>{html.escape(save_path)}</code>\n"
                    f"📁 Путь на сервере: <code>/root/ai_files/{html.escape(file_info_dict['file_name'])}</code>"
                )
            except Exception as e:
                safe_edit_message(call.message.chat.id, call.message.message_id, f"❌ Ошибка загрузки: {e}")
            
            PENDING_FILES.pop(call.message.chat.id, None)
            return
            
        if data == "file_ai":
            if not CURRENT_MODEL:
                bot.answer_callback_query(call.id, "⚠️ Сначала выберите модель (/gemini)!", show_alert=True)
                return
                
            clean_name = CURRENT_MODEL.replace('models/', '')
            is_gemma = "gemma" in clean_name.lower()
            
            safe_edit_message(call.message.chat.id, call.message.message_id, f"<b>{clean_name}:</b>\n🧠 Читаю и анализирую файл...")
            msg_wait = bot.send_message(call.message.chat.id, "🤖 Ожидайте вывода...")
            
            try:
                file_info = bot.get_file(file_info_dict['file_id'])
                downloaded_file = bot.download_file(file_info.file_path)
                temp_file_name = f"temp_ai_{file_info_dict['file_name']}"
                
                with open(temp_file_name, 'wb') as new_file:
                    new_file.write(downloaded_file)
                
                mime = file_info_dict['mime_type']
                gemini_file = genai.upload_file(path=temp_file_name, mime_type=mime) if mime else genai.upload_file(path=temp_file_name)
                
                if is_gemma:
                     response = chat_agent.send_message("Я получил файл, но как модель Gemma я пока не умею напрямую читать файлы через этот интерфейс.")
                else:
                     response = chat_agent.send_message([gemini_file, "Проанализируй этот файл. Расскажи, что в нём (сделай саммари текста или объясни код). Если есть инструкции к действию - выполни их."])
                
                os.remove(temp_file_name)
                
                full_text = response.text
                if is_gemma:
                    try:
                        safe_edit_message(call.message.chat.id, call.message.message_id, f"*{clean_name} (Чат):*\n\n{full_text}", parse_mode='Markdown')
                    except:
                        safe_edit_message(call.message.chat.id, call.message.message_id, f"<b>{clean_name} (Чат):</b>\n\n{html.escape(full_text)}")
                    bot.delete_message(chat_id=call.message.chat.id, message_id=msg_wait.message_id)
                else:
                    if "===SPLIT===" in full_text:
                        parts = full_text.split("===SPLIT===", 1)
                        comment = parts[0].strip()
                        raw_out = parts[1].strip()
                    else:
                        comment = ""
                        raw_out = full_text.strip()
                        
                    first_text = f"<b>{clean_name}:</b>" + (f"\n\n{html.escape(comment)}" if comment else "")
                    safe_edit_message(call.message.chat.id, call.message.message_id, first_text)
                    safe_edit_message(call.message.chat.id, msg_wait.message_id, format_as_code(raw_out))
                    
            except Exception as e:
                handle_api_error(e, call.message.chat.id, msg_wait.message_id, None, clean_name)
                
            PENDING_FILES.pop(call.message.chat.id, None)
            return

@bot.message_handler(content_types=['voice', 'text'])
def handle_message(message):
    if message.from_user.id not in ADMIN_IDS:
        bot.reply_to(message, "⛔ Доступ запрещен.")
        return

    log_admin_action(message.from_user.id, f"Сообщение: {message.content_type}")

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
            log_admin_action(message.from_user.id, f"Прямая команда: {cmd}")
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
                safe_edit_message(message.chat.id, msg_wait.message_id, format_as_code(response.text))
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
            try:
                safe_edit_message(message.chat.id, msg_first.message_id, f"*{clean_model_name} (Чат):*\n\n{full_text}", parse_mode='Markdown')
            except Exception:
                safe_edit_message(message.chat.id, msg_first.message_id, f"<b>{clean_model_name} (Чат):</b>\n\n{html.escape(full_text)}")
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
                
            safe_edit_message(message.chat.id, msg_first.message_id, first_message_text)
            safe_edit_message(message.chat.id, msg_wait.message_id, format_as_code(raw_out))
                              
    except Exception as e:
        handle_api_error(e, message.chat.id, msg_wait.message_id, message, clean_model_name)

if __name__ == '__main__':
    print(f"AI-Админ запущен. Допущено админов: {len(ADMIN_IDS)}. Ожидание команд...")
    bot.polling(none_stop=True)
                          
