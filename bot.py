import os
import subprocess
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import google.generativeai as genai
import html
import re
import shlex
from collections import defaultdict

# Импортируем наши функции форматирования из отдельного файла
from markdown import split_text_safely, md_to_html

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
PENDING_SEARCH_RESULTS = {}

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

# --- ФУНКЦИИ ВЫВОДА ---

def safe_edit_message(chat_id, message_id, text, parse_mode='HTML', reply_markup=None):
    try:
        bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, 
                              parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            raise e

def send_long_text(chat_id, text, first_msg_id=None, is_code=False, prefix=""):
    """Отправляет или обновляет сообщение с учетом лимитов Telegram и разметки"""
    if not text: return
    text = text.replace('\r\n', '\n')
    chunks = split_text_safely(text, max_len=3500)
    
    for i, chunk in enumerate(chunks):
        if is_code:
            formatted = f'<pre><code class="language-bash">{html.escape(chunk.strip())}</code></pre>'
        else:
            formatted_chunk = md_to_html(chunk)
            formatted = f"{prefix}{formatted_chunk}" if i == 0 else formatted_chunk
            
        if i == 0 and first_msg_id:
            try:
                safe_edit_message(chat_id, first_msg_id, formatted, parse_mode='HTML')
            except Exception:
                safe_edit_message(chat_id, first_msg_id, f"{prefix}{chunk.strip()}" if i==0 else chunk.strip())
        else:
            try:
                bot.send_message(chat_id, formatted, parse_mode='HTML')
            except Exception:
                bot.send_message(chat_id, chunk.strip())

# --- БАЗОВЫЕ ИНСТРУМЕНТЫ АГЕНТА ---

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

# --- ЛОГИКА МОДЕЛЕЙ ---

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
                "Ты root-админ Ubuntu. Инструменты: execute_bash, send_file_to_telegram.\n"
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

# --- ОБРАБОТЧИКИ КОМАНД ---

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
        
    if not words: return

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

        clean_text_for_file = f"Результаты поиска по запросу: {' '.join(words)}\n"
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
                line = f"{html.escape(match)}\n\n" 
                if len(current_chunk) + len(line) > 4000:
                    formatted_chunks.append(current_chunk)
                    current_chunk = line
                else:
                    current_chunk += line
            clean_text_for_file += "\n"
            
        if current_chunk.strip():
            formatted_chunks.append(current_chunk)
            
        for chunk in formatted_chunks[:5]:
            bot.send_message(message.chat.id, f"<pre>{chunk.strip()}</pre>", parse_mode='HTML')
            
        if len(formatted_chunks) > 5:
            PENDING_SEARCH_RESULTS[message.chat.id] = clean_text_for_file
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton(text="📥 Скачать всё (.txt)", callback_data="download_search_txt"))
            bot.send_message(
                message.chat.id, 
                f"⚠️ <b>Внимание:</b> Показано 5 сообщений из {len(formatted_chunks)}. Остальной текст обрезан.\n\n"
                f"Вы можете скачать полные результаты поиска отдельным файлом:", 
                parse_mode='HTML',
                reply_markup=markup
            )
            
    except subprocess.TimeoutExpired:
        safe_edit_message(message.chat.id, msg_wait.message_id, "❌ Ошибка: Превышено время ожидания поиска (более 60 секунд).")
    except Exception as e:
        safe_edit_message(message.chat.id, msg_wait.message_id, f"❌ Ошибка поиска: {html.escape(str(e))}")

# --- ФАЙЛЫ И CALLBACKS ---

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
    markup.row(InlineKeyboardButton("✅ Да", callback_data="file_yes"), InlineKeyboardButton("❌ Нет", callback_data="file_no"))
    markup.row(InlineKeyboardButton("🧠 Обработать ИИ", callback_data="file_ai"))
    
    bot.reply_to(message, f"📥 Загрузить файл <b>{html.escape(file_name)}</b> на сервер?", reply_markup=markup, parse_mode='HTML')

@bot.callback_query_handler(func=lambda call: True)
def handle_query(call):
    if call.from_user.id not in ADMIN_IDS: return
    log_admin_action(call.from_user.id, f"Callback: {call.data}")
    
    global CURRENT_MODEL, PENDING_RETRY_MESSAGE, CURRENT_KEY_NUM, PRIORITY_MODELS_CACHE, OTHER_MODELS_CACHE, AVAILABLE_MODELS
    data = call.data
    
    if data == "download_search_txt":
        full_text = PENDING_SEARCH_RESULTS.get(call.message.chat.id)
        if not full_text:
            bot.answer_callback_query(call.id, "❌ Результаты поиска устарели.", show_alert=True)
            return
            
        safe_edit_message(call.message.chat.id, call.message.message_id, "⏳ Формирую файл...")
        temp_filename = f"search_results_temp_{call.message.chat.id}.txt"
        try:
            with open(temp_filename, "w", encoding="utf-8") as f: f.write(full_text)
            with open(temp_filename, "rb") as f: bot.send_document(call.message.chat.id, f, caption="📁 Полные результаты поиска")
            safe_edit_message(call.message.chat.id, call.message.message_id, "✅ Файл успешно отправлен.")
        except Exception as e:
            safe_edit_message(call.message.chat.id, call.message.message_id, f"❌ Ошибка файла: {str(e)}")
        finally:
            if os.path.exists(temp_filename): os.remove(temp_filename)
            PENDING_SEARCH_RESULTS.pop(call.message.chat.id, None)
        return

    if data == "show_all_mods":
        safe_edit_message(call.message.chat.id, call.message.message_id, "Выберите модель:", reply_markup=get_models_keyboard(show_all=True))
        return

    if data.startswith("key_"):
        key_num = int(data.split("_")[1])
        target_key = API_KEY_1 if key_num == 1 else (API_KEY_2 if key_num == 2 else API_KEY_3)
        if not target_key:
            bot.answer_callback_query(call.id, f"❌ KEY {key_num} не задан!", show_alert=True)
            return
            
        CURRENT_KEY_NUM = key_num
        genai.configure(api_key=target_key)
        PRIORITY_MODELS_CACHE, OTHER_MODELS_CACHE, AVAILABLE_MODELS, CURRENT_MODEL = [], [], [], None
        
        # Удаляем сообщение с меню ключей
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
            
        bot.send_message(call.message.chat.id, f"✅ Активен <b>KEY {key_num}</b>.\nВызовите /gemini для выбора модели.", parse_mode='HTML')
        return

    if data.startswith("mod_"):
        model_name = data.replace("mod_", "")
        CURRENT_MODEL = model_name
        try:
            init_models(CURRENT_MODEL)
            clean_name = CURRENT_MODEL.replace('models/', '')
            mode_text = "(Режим Чатбота)" if "gemma" in clean_name.lower() else "(Режим Админа)"
            
            # Полностью удаляем сообщение с кнопками моделей
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception:
                pass
                
            # Отправляем новое сообщение в самый низ
            bot.send_message(call.message.chat.id, f"✅ Выбрана модель: <b>{clean_name}</b> {mode_text}", parse_mode='HTML')
            
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
            safe_edit_message(call.message.chat.id, call.message.message_id, "❌ Отменено.")
            PENDING_FILES.pop(call.message.chat.id, None)
            return
            
        if not file_info_dict:
            bot.answer_callback_query(call.id, "❌ Файл устарел.", show_alert=True)
            return

        if data == "file_yes":
            safe_edit_message(call.message.chat.id, call.message.message_id, "⏳ Сохраняю...")
            try:
                file_info = bot.get_file(file_info_dict['file_id'])
                downloaded_file = bot.download_file(file_info.file_path)
                os.makedirs("/app/downloads", exist_ok=True)
                save_path = os.path.join("/app/downloads", file_info_dict['file_name'])
                with open(save_path, 'wb') as new_file: new_file.write(downloaded_file)
                safe_edit_message(call.message.chat.id, call.message.message_id, f"✅ Файл сохранен:\n<code>{html.escape(save_path)}</code>")
            except Exception as e:
                safe_edit_message(call.message.chat.id, call.message.message_id, f"❌ Ошибка: {e}")
            PENDING_FILES.pop(call.message.chat.id, None)
            return
            
        if data == "file_ai":
            if not CURRENT_MODEL:
                bot.answer_callback_query(call.id, "⚠️ Выберите модель (/gemini)!", show_alert=True)
                return
                
            clean_name = CURRENT_MODEL.replace('models/', '')
            is_gemma = "gemma" in clean_name.lower()
            safe_edit_message(call.message.chat.id, call.message.message_id, f"<b>{clean_name}:</b>\n🧠 Читаю файл...")
            msg_wait = bot.send_message(call.message.chat.id, "🤖 Ожидайте...")
            
            try:
                file_info = bot.get_file(file_info_dict['file_id'])
                temp_file_name = f"temp_ai_{file_info_dict['file_name']}"
                with open(temp_file_name, 'wb') as new_file: new_file.write(bot.download_file(file_info.file_path))
                
                mime = file_info_dict['mime_type']
                gemini_file = genai.upload_file(path=temp_file_name, mime_type=mime) if mime else genai.upload_file(path=temp_file_name)
                
                if is_gemma:
                     response = chat_agent.send_message("Я текстовая модель Gemma, я пока не умею читать файлы.")
                else:
                     response = chat_agent.send_message([gemini_file, "Проанализируй этот файл. Расскажи, что в нём, либо выполни инструкции."])
                os.remove(temp_file_name)
                
                full_text = response.text
                if is_gemma:
                    bot.delete_message(chat_id=call.message.chat.id, message_id=msg_wait.message_id)
                    send_long_text(call.message.chat.id, full_text, first_msg_id=call.message.message_id, prefix=f"<b>{clean_name} (Чат):</b>\n\n")
                else:
                    if "===SPLIT===" in full_text:
                        parts = full_text.split("===SPLIT===", 1)
                        comment, raw_out = parts[0].strip(), parts[1].strip()
                    else:
                        comment, raw_out = "", full_text.strip()
                        
                    if comment:
                        send_long_text(call.message.chat.id, comment, first_msg_id=call.message.message_id, prefix=f"<b>{clean_name}:</b>\n\n")
                    else:
                        safe_edit_message(call.message.chat.id, call.message.message_id, f"<b>{clean_name}:</b>")
                        
                    if raw_out:
                        send_long_text(call.message.chat.id, raw_out, first_msg_id=msg_wait.message_id, is_code=True)
                    else:
                        safe_edit_message(call.message.chat.id, msg_wait.message_id, "<i>Вывод пуст.</i>")
                    
            except Exception as e:
                handle_api_error(e, call.message.chat.id, msg_wait.message_id, None, clean_name)
                
            PENDING_FILES.pop(call.message.chat.id, None)
            return

# --- ГЛАВНЫЙ ОБРАБОТЧИК СООБЩЕНИЙ ---

@bot.message_handler(content_types=['voice', 'text', 'photo'])
def handle_message(message):
    if message.from_user.id not in ADMIN_IDS:
        return # Полное игнорирование неизвестных

    log_admin_action(message.from_user.id, f"Сообщение: {message.content_type}")

    global CURRENT_CHAT_ID
    CURRENT_CHAT_ID = message.chat.id
    
    if not CURRENT_MODEL:
        bot.reply_to(message, "⚠️ Сначала выберите модель (/gemini)", reply_markup=get_models_keyboard())
        return

    clean_model_name = CURRENT_MODEL.replace('models/', '')
    is_gemma = "gemma" in clean_model_name.lower()
    
    is_voice = message.content_type == 'voice'
    is_photo = message.content_type == 'photo'
    
    # Берем текст из самого сообщения ИЛИ из подписи к фото
    text = (message.text or message.caption or "").strip()

    if not is_voice and not is_photo:
        if text.startswith('!'):
            cmd = text[1:].strip()
            log_admin_action(message.from_user.id, f"Прямая команда: {cmd}")
            bot.send_message(message.chat.id, f"⚡ Выполняю напрямую:\n<code>{html.escape(cmd)}</code>", parse_mode='HTML')
            result = execute_bash(cmd)
            send_long_text(message.chat.id, result, is_code=True)
            return

        if text.startswith('#'):
            task = text[1:].strip()
            msg_first = bot.send_message(message.chat.id, f"<b>{clean_model_name}:</b>", parse_mode='HTML')
            msg_wait = bot.send_message(message.chat.id, "🧠 Думаю...")
            try:
                response = model_advisor.generate_content(task)
                send_long_text(message.chat.id, response.text, first_msg_id=msg_wait.message_id, is_code=True)
            except Exception as e:
                handle_api_error(e, message.chat.id, msg_wait.message_id, message, clean_model_name)
            return

    msg_first = bot.send_message(message.chat.id, f"<b>{clean_model_name}:</b>", parse_mode='HTML')
    msg_wait = bot.send_message(message.chat.id, "🤖 Обрабатываю запрос...")

    try:
        if is_voice:
            file_info = bot.get_file(message.voice.file_id)
            voice_path = f"temp_voice_{message.message_id}.ogg"
            with open(voice_path, 'wb') as new_file:
                new_file.write(bot.download_file(file_info.file_path))
            
            audio_file = genai.upload_file(path=voice_path, mime_type="audio/ogg")
            
            if is_gemma:
                 response = chat_agent.send_message("Я получил аудио, но я текстовая модель Gemma и не умею слушать звук.")
            else:
                 response = chat_agent.send_message([audio_file, "Слушай аудио и выполни команду."])
                 
            os.remove(voice_path)
            
        elif is_photo:
            file_info = bot.get_file(message.photo[-1].file_id)
            photo_path = f"temp_photo_{message.message_id}.jpg"
            with open(photo_path, 'wb') as new_file:
                new_file.write(bot.download_file(file_info.file_path))
                
            img_file = genai.upload_file(path=photo_path)
            
            if is_gemma:
                response = chat_agent.send_message("Я получил изображение, но я текстовая модель Gemma и не умею смотреть картинки.")
            else:
                prompt = text if text else "Проанализируй это изображение."
                response = chat_agent.send_message([img_file, prompt])
                
            os.remove(photo_path)
            
        else:
            response = chat_agent.send_message(text)
            
        full_text = response.text
        
        if is_gemma:
            bot.delete_message(chat_id=message.chat.id, message_id=msg_wait.message_id)
            prefix = f"<b>{clean_model_name} (Чат):</b>\n\n"
            send_long_text(message.chat.id, full_text, first_msg_id=msg_first.message_id, prefix=prefix)
        else:
            if "===SPLIT===" in full_text:
                parts = full_text.split("===SPLIT===", 1)
                comment, raw_out = parts[0].strip(), parts[1].strip()
            else:
                comment, raw_out = "", full_text.strip()
                
            prefix = f"<b>{clean_model_name}:</b>\n\n"
            if comment:
                send_long_text(message.chat.id, comment, first_msg_id=msg_first.message_id, prefix=prefix)
            else:
                safe_edit_message(message.chat.id, msg_first.message_id, f"<b>{clean_model_name}:</b>")
                
            if raw_out:
                send_long_text(message.chat.id, raw_out, first_msg_id=msg_wait.message_id, is_code=True)
            else:
                safe_edit_message(message.chat.id, msg_wait.message_id, "<i>Вывод терминала пуст.</i>")
                              
    except Exception as e:
        handle_api_error(e, message.chat.id, msg_wait.message_id, message, clean_model_name)

if __name__ == '__main__':
    print(f"AI-Админ запущен. Допущено админов: {len(ADMIN_IDS)}. Режим невидимки включен...")
    bot.polling(none_stop=True)
