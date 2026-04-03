import os
import subprocess
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import google.generativeai as genai
import html
import re
import shlex
import asyncio
import edge_tts
import time
from collections import deque

# Импортируем наши внешние модули
from markdown import split_text_safely, md_to_html
from search import parse_search_query, run_grep_search, run_archive_search, format_search_results
import web_search

# Загружаем ключи
TG_TOKEN = os.getenv("TG_TOKEN")
API_KEY_1 = os.getenv("GEMINI_API_KEY")
API_KEY_2 = os.getenv("GEMINI2_API_KEY")
API_KEY_3 = os.getenv("GEMINI3_API_KEY")

# Безопасно собираем все ID админов
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

# Единые настройки для ВСЕХ моделей
MODEL_ROLE = {}  # {chat_id: "admin" | "chat"}
MODEL_MODE = {}  # {chat_id: "auto" | "semi"}
PENDING_ACTION = {} 

ACTION_LOGS = {}
LAST_ACTIONS = {} 
VOICE_MODE = {}  
STATUS_MSG = {}

# ─────────────────────────────────────────────
#  КОНФИГУРАЦИЯ: ПРОМПТЫ, МОДЕЛИ, ЛИМИТЫ
# ─────────────────────────────────────────────

PROMPTS_FILE = "prompts.txt"
PROMPTS = {}

MODELS_FILE = "models.txt"
MODEL_RPM_LIMITS = {}
MODEL_RESTRICTED_KEYS = {} 
MODEL_TPM_LIMITS = {}
PRIORITY_MODELS = []

def load_prompts_config():
    global PROMPTS
    PROMPTS.clear()
    
    if not os.path.exists(PROMPTS_FILE):
        default_prompts = """[GEMINI_ADMIN]
Ты AI-агент и root-админ Ubuntu. Твои инструменты: execute_bash, send_file_to_telegram, search_web_tool, download_file_tool.
Используй поиск для получения актуальной информации (курсы валют, новости).

[GEMINI_ADVISOR]
Ты root-админ Ubuntu. Дай только bash-команду (без sudo).

[GEMMA_ADMIN_REACT]
[SYSTEM] Ты автономный AI-админ. Если нужно выполнить команду: <BASH>cmd</BASH>. Поиск в сети (обязательно для курсов валют, новостей, цен): <SEARCH>query</SEARCH>. Скачать файл: <DOWNLOAD>url</DOWNLOAD>. Отправить файл юзеру: <FILE>path</FILE>.

[CHAT_BOT]
Ты умный, полезный и дружелюбный ИИ-ассистент. Отвечай на вопросы пользователя и поддерживай диалог. Системные функции отключены."""
        with open(PROMPTS_FILE, "w", encoding="utf-8") as f:
            f.write(default_prompts)

    with open(PROMPTS_FILE, "r", encoding="utf-8") as f:
        current_key = None
        current_text = []
        for line in f:
            match = re.match(r'^\[(.*?)\]$', line.strip())
            if match:
                if current_key: PROMPTS[current_key] = "\n".join(current_text).strip()
                current_key = match.group(1)
                current_text = []
            else: current_text.append(line.rstrip('\n'))
        if current_key: PROMPTS[current_key] = "\n".join(current_text).strip()

def load_models_config():
    global PRIORITY_MODELS, MODEL_RPM_LIMITS, MODEL_RESTRICTED_KEYS, MODEL_TPM_LIMITS
    PRIORITY_MODELS.clear()
    MODEL_RPM_LIMITS.clear()
    MODEL_RESTRICTED_KEYS.clear()
    MODEL_TPM_LIMITS.clear()
    
    if not os.path.exists(MODELS_FILE):
        default_content = """gemini-2.5-flash [RPM:5,TPM:250000]
gemini-flash-latest [RPM:5,TPM:250000]
gemini-2.5-flash-lite [RPM:10,TPM:250000]
gemma-3-27b [RPM:15,TPM:15000]"""
        with open(MODELS_FILE, "w", encoding="utf-8") as f: f.write(default_content)
    
    with open(MODELS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"): continue
            
            model_match = re.search(r'^([\w\-\.]+)', line)
            if model_match:
                model_name = model_match.group(1)
                PRIORITY_MODELS.append(model_name)
                
                rpm_match = re.search(r'RPM:(\d+)', line)
                if rpm_match: MODEL_RPM_LIMITS[model_name] = int(rpm_match.group(1))
                
                tpm_match = re.search(r'TPM:(\d+)', line)
                if tpm_match: MODEL_TPM_LIMITS[model_name] = int(tpm_match.group(1))
                    
                key_match = re.search(r'KEY:([\d,]+)', line)
                if key_match: MODEL_RESTRICTED_KEYS[model_name] = [int(k) for k in key_match.group(1).split(',') if k.isdigit()]

load_prompts_config()
load_models_config()

API_REQUEST_HISTORY = {1: deque(), 2: deque(), 3: deque()}
API_TOKEN_HISTORY = {1: deque(), 2: deque(), 3: deque()}

def log_admin_action(user_id, action):
    print(f"[ADMIN {user_id}] {action}")

# ─────────────────────────────────────────────
#  Live-статус и Трекер Лимитов
# ─────────────────────────────────────────────

def set_status(chat_id, text: str):
    global STATUS_MSG
    msg_id = STATUS_MSG.get(chat_id)
    if msg_id:
        try:
            bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, parse_mode='HTML')
            return
        except Exception as e:
            if "message is not modified" in str(e).lower(): return
            STATUS_MSG.pop(chat_id, None)
    try:
        msg = bot.send_message(chat_id, text, parse_mode='HTML')
        STATUS_MSG[chat_id] = msg.message_id
    except Exception: pass

def clear_status(chat_id):
    msg_id = STATUS_MSG.pop(chat_id, None)
    if msg_id:
        try: bot.delete_message(chat_id, msg_id)
        except Exception: pass

def track_token_usage(token_count):
    global CURRENT_KEY_NUM
    if token_count: API_TOKEN_HISTORY[CURRENT_KEY_NUM].append((time.time(), token_count))

def check_api_rate_limit(chat_id, current_status_text):
    global CURRENT_MODEL, CURRENT_KEY_NUM
    if not CURRENT_MODEL: return

    clean_name = CURRENT_MODEL.replace('models/', '')
    
    # Берем лимиты строго из конфига (без дефолтных значений)
    rpm_limit = MODEL_RPM_LIMITS.get(clean_name)
    tpm_limit = MODEL_TPM_LIMITS.get(clean_name)
    
    now = time.time()
    
    if rpm_limit:
        history_rpm = API_REQUEST_HISTORY.setdefault(CURRENT_KEY_NUM, deque())
        while history_rpm and now - history_rpm[0] > 60.5: history_rpm.popleft()
        if len(history_rpm) >= rpm_limit:
            wait_time = 60.5 - (now - history_rpm[0])
            if wait_time > 0:
                set_status(chat_id, f"{current_status_text}\n⏳ <i>Пауза: ожидание {wait_time:.1f}с (лимит {rpm_limit} RPM)</i>")
                time.sleep(wait_time) 
                set_status(chat_id, current_status_text)
                now = time.time()

    if tpm_limit:
        history_tpm = API_TOKEN_HISTORY.setdefault(CURRENT_KEY_NUM, deque())
        while history_tpm and now - history_tpm[0][0] > 60.5: history_tpm.popleft()
        current_tpm = sum(count for _, count in history_tpm)
        if current_tpm > (tpm_limit - 1000): # Буфер 1000 токенов
            wait_time = 60.5 - (now - history_tpm[0][0])
            if wait_time > 0:
                set_status(chat_id, f"{current_status_text}\n⏳ <i>Тайм-аут токенов: ожидание {wait_time:.1f}с (использовано {current_tpm}/{tpm_limit} TPM)</i>")
                time.sleep(wait_time)
                set_status(chat_id, current_status_text)
                now = time.time()

    API_REQUEST_HISTORY[CURRENT_KEY_NUM].append(now)

# ─────────────────────────────────────────────
#  ФУНКЦИИ ВЫВОДА
# ─────────────────────────────────────────────

def safe_edit_message(chat_id, message_id, text, parse_mode='HTML', reply_markup=None):
    try: bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as e:
        if "message is not modified" in str(e).lower(): raise e

def send_long_text(chat_id, text, first_msg_id=None, is_code=False, prefix="", reply_markup=None):
    if not text: return
    text = text.replace('\r\n', '\n')
    chunks = split_text_safely(text, max_len=3500)

    for i, chunk in enumerate(chunks):
        is_last = (i == len(chunks) - 1)
        current_markup = reply_markup if is_last else None

        if is_code: formatted = f'<pre><code class="language-bash">{html.escape(chunk.strip())}</code></pre>'
        else:
            formatted_chunk = md_to_html(chunk)
            formatted = f"{prefix}{formatted_chunk}" if i == 0 else formatted_chunk

        if i == 0 and first_msg_id:
            try: safe_edit_message(chat_id, first_msg_id, formatted, parse_mode='HTML', reply_markup=current_markup)
            except Exception: safe_edit_message(chat_id, first_msg_id, f"{prefix}{chunk.strip()}" if i == 0 else chunk.strip(), reply_markup=current_markup)
        else:
            try: bot.send_message(chat_id, formatted, parse_mode='HTML', reply_markup=current_markup)
            except Exception: bot.send_message(chat_id, chunk.strip(), reply_markup=current_markup)

def clean_text_for_voice(text: str) -> str:
    if not text: return ""
    text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
    text = re.sub(r'`.*?`', '', text)
    text = re.sub(r'https?://\S+|www\.\S+', '', text)
    text = re.sub(r'<[^>]*>', '', text)
    text = re.sub(r'[*#_~()\[\]{}<>@|\\/]', '', text)
    text = text.replace('\n', '. ')
    text = re.sub(r'\.{2,}', '.', text)
    text = re.sub(r'\s{2,}', ' ', text)
    return text.strip()

def generate_and_send_voice(chat_id, text):
    clean_text = clean_text_for_voice(text)
    if not clean_text: return
    voice_chunks = split_text_safely(clean_text, max_len=2000)
    total_chunks = len(voice_chunks)

    for i, chunk in enumerate(voice_chunks):
        if not chunk.strip(): continue
        mp3_path = f"temp_tts_{chat_id}_{i}.mp3"
        ogg_path = f"temp_tts_{chat_id}_{i}.ogg"
        msg_wait_voice = None

        try:
            part_text = f" [часть {i+1}/{total_chunks}]" if total_chunks > 1 else ""
            msg_wait_voice = bot.send_message(chat_id, f"🎙 <i>Отправка голосового сообщения{part_text}...</i>", parse_mode='HTML')
            bot.send_chat_action(chat_id, 'record_voice')
            async def _generate():
                communicate = edge_tts.Communicate(chunk, "ru-RU-SvetlanaNeural")
                await communicate.save(mp3_path)
            asyncio.run(_generate())
            if os.path.exists(mp3_path):
                subprocess.run(f"ffmpeg -i {mp3_path} -c:a libopus -b:a 32k -v quiet -y {ogg_path}", shell=True, timeout=60)
                if os.path.exists(ogg_path):
                    with open(ogg_path, 'rb') as voice_file: bot.send_voice(chat_id, voice_file)
        except Exception as e: print(f"Ошибка синтеза: {e}")
        finally:
            if msg_wait_voice:
                try: bot.delete_message(chat_id, msg_wait_voice.message_id)
                except Exception: pass
            if os.path.exists(mp3_path): os.remove(mp3_path)
            if os.path.exists(ogg_path): os.remove(ogg_path)

# ─────────────────────────────────────────────
#  АГЕНТСКИЕ ИНСТРУМЕНТЫ
# ─────────────────────────────────────────────

def execute_bash(command: str) -> str:
    if CURRENT_CHAT_ID:
        ACTION_LOGS.setdefault(CURRENT_CHAT_ID, []).append(("bash", command))
        short_cmd = command if len(command) <= 80 else command[:77] + "…"
        status_text = f"⚙️ <b>Выполняю команду:</b>\n<code>{html.escape(short_cmd)}</code>"
        set_status(CURRENT_CHAT_ID, status_text)
        check_api_rate_limit(CURRENT_CHAT_ID, status_text) 
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
        output = result.stdout if result.stdout else result.stderr
        return output[:2500] 
    except Exception as e: return f"Ошибка: {str(e)}"

def search_web_tool(query: str) -> str:
    if CURRENT_CHAT_ID:
        ACTION_LOGS.setdefault(CURRENT_CHAT_ID, []).append(("search", query))
        short_q = query if len(query) <= 80 else query[:77] + "…"
        status_text = f"🔍 <b>Ищу в интернете:</b>\n<i>{html.escape(short_q)}</i>"
        set_status(CURRENT_CHAT_ID, status_text)
        check_api_rate_limit(CURRENT_CHAT_ID, status_text) 
    return web_search.search_web(query)

def download_file_tool(url: str) -> str:
    if CURRENT_CHAT_ID:
        ACTION_LOGS.setdefault(CURRENT_CHAT_ID, []).append(("download", url))
        short_url = url if len(url) <= 70 else url[:67] + "…"
        status_text = f"⬇️ <b>Скачиваю файл:</b>\n<code>{html.escape(short_url)}</code>"
        set_status(CURRENT_CHAT_ID, status_text)
        check_api_rate_limit(CURRENT_CHAT_ID, status_text) 
    return web_search.download_file_tool(url)

def send_file_to_telegram(filepath: str) -> str:
    global CURRENT_CHAT_ID
    if CURRENT_CHAT_ID:
        ACTION_LOGS.setdefault(CURRENT_CHAT_ID, []).append(("file", filepath))
        short_path = os.path.basename(filepath)
        set_status(CURRENT_CHAT_ID, f"📤 <b>Отправляю файл:</b>\n<code>{html.escape(short_path)}</code>")

    if not CURRENT_CHAT_ID: return "Ошибка: ID чата неизвестен."
    if not os.path.exists(filepath): return f"Ошибка: Файл {filepath} не найден."
    try:
        with open(filepath, 'rb') as f: bot.send_document(CURRENT_CHAT_ID, f)
        return f"Успех: Файл {filepath} отправлен."
    except Exception as e: return f"Ошибка отправки файла: {str(e)}"

# ─────────────────────────────────────────────
#  ЛОГИКА МОДЕЛЕЙ И УМНЫЙ ТРИММЕР
# ─────────────────────────────────────────────

def get_models_lists():
    raw_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
    priority, other, used_models = [], [], set()

    for p in PRIORITY_MODELS:
        if CURRENT_KEY_NUM in MODEL_RESTRICTED_KEYS.get(p, []): continue 
        search_str = p.lower().replace("sep-2025", "09-2025")
        best_match = None
        for m in raw_models:
            if m in used_models: continue
            clean_m = m.replace('models/', '').lower()
            if clean_m == search_str or clean_m == f"{search_str}-it":
                best_match = m
                break
            elif search_str in clean_m:
                if "tts" in clean_m or "image" in clean_m: continue
                if not best_match: best_match = m
        if best_match:
            priority.append(best_match)
            used_models.add(best_match)

    for m in raw_models:
        if m not in used_models:
            clean_m = m.replace('models/', '').lower()
            if CURRENT_KEY_NUM not in MODEL_RESTRICTED_KEYS.get(clean_m, []):
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
        rpm_info = MODEL_RPM_LIMITS.get(clean_name)
        # Выводим инфо о RPM только если оно явно задано в конфиге
        btn_text = f"{clean_name} (RPM:{rpm_info})" if rpm_info else clean_name
        markup.add(InlineKeyboardButton(text=btn_text, callback_data=f"mod_{model_name}"))

    if show_all:
        for model_name in OTHER_MODELS_CACHE:
            clean_name = model_name.replace('models/', '')
            rpm_info = MODEL_RPM_LIMITS.get(clean_name)
            btn_text = f"{clean_name} (RPM:{rpm_info})" if rpm_info else clean_name
            markup.add(InlineKeyboardButton(text=btn_text, callback_data=f"mod_{model_name}"))
    else:
        if OTHER_MODELS_CACHE:
            markup.add(InlineKeyboardButton(text="⬇️ Другие модели", callback_data="show_all_mods"))
    return markup

def init_models(model_name, role="admin", mode="auto"):
    global chat_agent, model_advisor
    is_gemma = "gemma" in model_name.lower()

    if is_gemma:
        model_agent = genai.GenerativeModel(model_name=model_name)
        chat_agent = model_agent.start_chat()
        model_advisor = genai.GenerativeModel(model_name=model_name)
    else:
        if role == "chat":
            model_agent = genai.GenerativeModel(
                model_name=model_name,
                system_instruction=PROMPTS.get("CHAT_BOT", "Ты ИИ-ассистент.")
            )
            chat_agent = model_agent.start_chat()
        else:
            enable_auto = (mode == "auto")
            model_agent = genai.GenerativeModel(
                model_name=model_name,
                tools=[execute_bash, send_file_to_telegram, search_web_tool, download_file_tool],
                system_instruction=PROMPTS.get("GEMINI_ADMIN", "Ты AI-админ.")
            )
            chat_agent = model_agent.start_chat(enable_automatic_function_calling=enable_auto)

        model_advisor = genai.GenerativeModel(
            model_name=model_name,
            system_instruction=PROMPTS.get("GEMINI_ADVISOR", "Дай bash-команду.")
        )

def handle_api_error(e, chat_id, message_id, original_message, clean_model_name):
    error_text = str(e)
    
    if "function response turn comes immediately after a function call" in error_text:
        global chat_agent
        if chat_agent:
            chat_agent.history.clear()
        safe_edit_message(chat_id, message_id, "⚠️ <b>Сбой синхронизации API!</b>\nКонтекст был поврежден (прерван вызов функции). Память ИИ автоматически очищена. Пожалуйста, повторите ваш последний запрос.")
        return

    if "429" in error_text or "Quota exceeded" in error_text:
        global PENDING_RETRY_MESSAGE
        PENDING_RETRY_MESSAGE = original_message
        delay_match = re.search(r'retry in ([\d\.]+)s', error_text)
        delay_str = f"<b>{float(delay_match.group(1)):.0f} сек.</b>" if delay_match else "некоторое время"
        pretty_error = (f"⚠️ <b>Ошибка 429: Лимит API!</b>\n\nМодель <code>{clean_model_name}</code> уперлась в квоту.\n"
                        f"⏳ Блокировка спадет через: {delay_str}")
        safe_edit_message(chat_id, message_id, pretty_error, reply_markup=get_models_keyboard())
    else:
        safe_edit_message(chat_id, message_id, f"❌ Ошибка ИИ: {html.escape(error_text)}")

def trim_chat_history(agent):
    MAX_HISTORY = 6
    if not hasattr(agent, 'history') or len(agent.history) <= MAX_HISTORY:
        return
        
    cut_idx = len(agent.history) - MAX_HISTORY
    
    while cut_idx < len(agent.history):
        msg = agent.history[cut_idx]
        if msg.role == 'user':
            is_function_response = False
            for part in msg.parts:
                if hasattr(part, 'function_response') and part.function_response:
                    is_function_response = True
                    break
            
            if not is_function_response:
                break 
        cut_idx += 1
        
    if cut_idx < len(agent.history):
        agent.history = agent.history[cut_idx:]

# ─────────────────────────────────────────────
#  ОБРАБОТЧИКИ КОМАНД
# ─────────────────────────────────────────────

@bot.message_handler(commands=['start'])
def send_welcome(message):
    if message.from_user.id not in ADMIN_IDS: return
    bot.reply_to(message, "👋 Привет, Админ!\nВыбери модель Gemini:", reply_markup=get_models_keyboard())

@bot.message_handler(commands=['help'])
def send_help(message):
    if message.from_user.id not in ADMIN_IDS: return
    log_admin_action(message.from_user.id, "Команда /help")
    help_text = (
        "🤖 <b>Справка по командам AI-Админа:</b>\n\n"
        "🔸 /start — Приветствие и выбор модели\n"
        "🔸 /help — Показать эту справку\n"
        "🔸 /gemini — Выбрать активную модель ИИ\n"
        "🔸 /changekey — Сменить текущий API-ключ Gemini\n"
        "🔸 /reload — Перезагрузить конфиги (models.txt, prompts.txt)\n"
        "🔸 /voice — Управление голосовыми ответами (Вкл/Выкл)\n"
        "🔸 /clear — Очистить память (контекст) текущей модели\n"
        "🔸 /search — Поиск по базам данных (csv) и архивам (zip, 7z)\n\n"
        "💡 <b>Скрытые команды:</b>\n"
        "<code>!команда</code> — Выполнить bash-команду напрямую (без ИИ)\n"
        "<code>#запрос</code> — Выполнить запрос через ИИ-советника (вернет только команду)"
    )
    bot.reply_to(message, help_text, parse_mode='HTML')

@bot.message_handler(commands=['gemini'])
def change_model(message):
    if message.from_user.id not in ADMIN_IDS: return
    bot.reply_to(message, "Выберите модель:", reply_markup=get_models_keyboard())

@bot.message_handler(commands=['reload'])
def reload_configs_cmd(message):
    if message.from_user.id not in ADMIN_IDS: return
    try:
        load_models_config()
        load_prompts_config()
        global PRIORITY_MODELS_CACHE, OTHER_MODELS_CACHE, AVAILABLE_MODELS
        PRIORITY_MODELS_CACHE, OTHER_MODELS_CACHE, AVAILABLE_MODELS = [], [], [] 
        bot.reply_to(message, f"✅ Конфигурация обновлена!\nЗагружено <b>{len(PRIORITY_MODELS)}</b> моделей.\nПромпты: <b>{', '.join(PROMPTS.keys())}</b>.", parse_mode='HTML')
    except Exception as e: bot.reply_to(message, f"❌ Ошибка: {e}")

@bot.message_handler(commands=['changekey'])
def change_key_cmd(message):
    if message.from_user.id not in ADMIN_IDS: return
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton(text="🔑 KEY 1" + (" (Активен)" if CURRENT_KEY_NUM == 1 else ""), callback_data="key_1"),
        InlineKeyboardButton(text="🔑 KEY 2" + (" (Активен)" if CURRENT_KEY_NUM == 2 else ""), callback_data="key_2"),
        InlineKeyboardButton(text="🔑 KEY 3" + (" (Активен)" if CURRENT_KEY_NUM == 3 else ""), callback_data="key_3")
    )
    bot.reply_to(message, "Выберите API-ключ для работы:", reply_markup=markup)

@bot.message_handler(commands=['voice'])
def voice_mode_cmd(message):
    if message.from_user.id not in ADMIN_IDS: return
    is_active = VOICE_MODE.get(message.chat.id, False)
    status_text = "ВКЛЮЧЕН 🟢" if is_active else "ВЫКЛЮЧЕН 🔴"
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("🟢 Включить", callback_data="voice_on"), InlineKeyboardButton("🔴 Выключить", callback_data="voice_off"))
    bot.reply_to(message, f"🎙 <b>Голосовой ответ ИИ</b>\n\nСейчас: <b>{status_text}</b>", reply_markup=markup, parse_mode='HTML')

@bot.message_handler(commands=['clear'])
def clear_cmd(message):
    if message.from_user.id not in ADMIN_IDS: return
    global chat_agent, CURRENT_MODEL
    if not CURRENT_MODEL:
        bot.reply_to(message, "⚠️ Модель еще не выбрана. Память пуста.")
        return
    try:
        init_models(CURRENT_MODEL, role=MODEL_ROLE.get(message.chat.id, "admin"), mode=MODEL_MODE.get(message.chat.id, "auto"))
        bot.reply_to(message, "🧹 Контекст и память ИИ успешно очищены!")
    except Exception as e: bot.reply_to(message, f"❌ Ошибка: {e}")

@bot.message_handler(commands=['search'])
def search_cmd(message):
    """Новое меню команды /search с выбором места поиска."""
    if message.from_user.id not in ADMIN_IDS: return
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("📂 Обычные базы", callback_data="search_type_regular"),
        InlineKeyboardButton("🗄 В архивах", callback_data="search_type_archive")
    )
    bot.reply_to(message, "Где искать?", reply_markup=markup)

def process_search_query(message, search_type="regular"):
    if message.from_user.id not in ADMIN_IDS: return
    query = (message.text or "").strip()
    if not query:
        bot.reply_to(message, "⚠️ Пустой запрос.")
        return
    words = parse_search_query(query)
    if not words: return
    
    mode_text = "в архивах (.zip, .7z)" if search_type == "archive" else "в обычных базах (.csv)"
    msg_wait = bot.send_message(message.chat.id, f"🔍 Ищу {mode_text}: <code>{' | '.join(words)}</code>...", parse_mode='HTML')
    
    try:
        if search_type == "archive":
            output = run_archive_search(words)
        else:
            output = run_grep_search(words)
            
        if not output:
            safe_edit_message(message.chat.id, msg_wait.message_id, "🤷‍♂️ По вашему запросу ничего не найдено.")
            return
            
        bot.delete_message(message.chat.id, msg_wait.message_id)
        formatted_chunks, clean_text_for_file = format_search_results(output, words)
        
        for chunk in formatted_chunks[:5]: 
            bot.send_message(message.chat.id, f"<pre>{chunk.strip()}</pre>", parse_mode='HTML')
            
        if len(formatted_chunks) > 5:
            PENDING_SEARCH_RESULTS[message.chat.id] = clean_text_for_file
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("📥 Скачать всё (.txt)", callback_data="download_search_txt"))
            bot.send_message(message.chat.id, f"⚠️ <b>Внимание:</b> Показано 5 сообщений. Остальной текст обрезан.\n\nСкачать полные результаты:", parse_mode='HTML', reply_markup=markup)
    except Exception as e: 
        safe_edit_message(message.chat.id, msg_wait.message_id, f"❌ Ошибка поиска: {html.escape(str(e))}")

@bot.message_handler(content_types=['document'])
def handle_document(message):
    if message.from_user.id not in ADMIN_IDS: return
    global CURRENT_CHAT_ID
    CURRENT_CHAT_ID = message.chat.id
    PENDING_FILES[message.chat.id] = {
        'file_id': message.document.file_id,
        'file_name': message.document.file_name,
        'mime_type': message.document.mime_type
    }
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("✅ Да", callback_data="file_yes"), InlineKeyboardButton("❌ Нет", callback_data="file_no"))
    markup.row(InlineKeyboardButton("🧠 Обработать ИИ", callback_data="file_ai"))
    bot.reply_to(message, f"📥 Загрузить файл <b>{html.escape(message.document.file_name)}</b> на сервер?", reply_markup=markup, parse_mode='HTML')

# ─────────────────────────────────────────────
#  ЕДИНЫЙ ПАРСЕР И МАРШРУТИЗАТОР ОТВЕТОВ
# ─────────────────────────────────────────────

def parse_and_route_response(chat_id, response, first_msg_id, original_text):
    clean_model_name = CURRENT_MODEL.replace('models/', '')
    is_gemma = "gemma" in clean_model_name.lower()
    role = MODEL_ROLE.get(chat_id, "admin")
    
    if role == "chat":
        finish_response(chat_id, response.text, first_msg_id, clean_model_name)
        return

    action = None
    
    if is_gemma:
        response_text = response.text or ""
        bash_match = re.search(r'<BASH>(.*?)</BASH>', response_text, re.DOTALL | re.IGNORECASE)
        search_match = re.search(r'<SEARCH>(.*?)</SEARCH>', response_text, re.DOTALL | re.IGNORECASE)
        dl_match = re.search(r'<DOWNLOAD>(.*?)</DOWNLOAD>', response_text, re.DOTALL | re.IGNORECASE)
        file_match = re.search(r'<FILE>(.*?)</FILE>', response_text, re.DOTALL | re.IGNORECASE)
        
        if bash_match: action = {"type": "react", "name": "bash", "val": bash_match.group(1).strip(), "disp_name": "Команда BASH", "disp_val": bash_match.group(1).strip()}
        elif search_match: action = {"type": "react", "name": "search", "val": search_match.group(1).strip(), "disp_name": "Поиск в сети", "disp_val": search_match.group(1).strip()}
        elif dl_match: action = {"type": "react", "name": "download", "val": dl_match.group(1).strip(), "disp_name": "Скачивание файла", "disp_val": dl_match.group(1).strip()}
        elif file_match: action = {"type": "react", "name": "file", "val": file_match.group(1).strip(), "disp_name": "Отправка файла в чат", "disp_val": file_match.group(1).strip()}
        
    else:
        if response.parts:
            for part in response.parts:
                if hasattr(part, 'function_call') and part.function_call:
                    fn_name = part.function_call.name
                    fn_args = {key: val for key, val in part.function_call.args.items()}
                    
                    disp_name = fn_name
                    disp_val = str(fn_args)
                    if fn_name == "execute_bash": disp_name, disp_val = "Команда BASH", fn_args.get("command", "")
                    elif fn_name == "search_web_tool": disp_name, disp_val = "Поиск в сети", fn_args.get("query", "")
                    elif fn_name == "download_file_tool": disp_name, disp_val = "Скачивание файла", fn_args.get("url", "")
                    elif fn_name == "send_file_to_telegram": disp_name, disp_val = "Отправка файла в чат", fn_args.get("filepath", "")
                    
                    action = {"type": "native", "name": fn_name, "args": fn_args, "disp_name": disp_name, "disp_val": disp_val}
                    break
                    
    if action:
        action["msg_id"] = first_msg_id
        action["orig_text"] = original_text
        process_action_request(chat_id, action)
    else:
        finish_response(chat_id, response.text, first_msg_id, clean_model_name)

def finish_response(chat_id, text, msg_id, clean_model_name):
    markup = None
    if ACTION_LOGS.get(chat_id):
        LAST_ACTIONS[msg_id] = ACTION_LOGS[chat_id].copy()
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🛠 Выполненные действия", callback_data=f"show_acts_{msg_id}"))
    
    send_long_text(chat_id, text, first_msg_id=msg_id, prefix=f"<b>{clean_model_name}:</b>\n\n", reply_markup=markup)
    if VOICE_MODE.get(chat_id): generate_and_send_voice(chat_id, text)
    clear_status(chat_id)

def process_action_request(chat_id, action):
    mode = MODEL_MODE.get(chat_id, "semi")
    if mode == "semi":
        PENDING_ACTION[chat_id] = action
        clear_status(chat_id)
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("✅ Выполнить", callback_data=f"act_yes_{chat_id}"), InlineKeyboardButton("❌ Отмена", callback_data=f"act_no_{chat_id}"))
        bot.send_message(chat_id, f"🤖 <b>Запрос действия ИИ:</b>\n\n{action['disp_name']}:\n<code>{html.escape(action['disp_val'])}</code>", parse_mode='HTML', reply_markup=markup)
    else:
        execute_pending_action(chat_id, action)

def execute_pending_action(chat_id, action):
    global chat_agent
    res = "Нет результата."
    
    if action["type"] == "react":
        if action["name"] == "bash": res = execute_bash(action["val"])
        elif action["name"] == "search": res = search_web_tool(action["val"])
        elif action["name"] == "download": res = download_file_tool(action["val"])
        elif action["name"] == "file": res = send_file_to_telegram(action["val"])
    elif action["type"] == "native":
        fn_name = action["name"]
        args = action["args"]
        if fn_name == "execute_bash": res = execute_bash(args.get("command", ""))
        elif fn_name == "search_web_tool": res = search_web_tool(args.get("query", ""))
        elif fn_name == "download_file_tool": res = download_file_tool(args.get("url", ""))
        elif fn_name == "send_file_to_telegram": res = send_file_to_telegram(args.get("filepath", ""))
        
    status_text = "🧠 <b>Анализирую результат...</b>"
    set_status(chat_id, status_text)
    
    try:
        check_api_rate_limit(chat_id, status_text)
        
        if action["type"] == "react":
            followup_prompt = f"РЕЗУЛЬТАТ ВЫПОЛНЕНИЯ ({action['name']}):\n{res}\nОсновываясь на этом, дай финальный ответ или вызови новый тег."
            response = chat_agent.send_message(followup_prompt)
            if hasattr(chat_agent, 'history') and len(chat_agent.history) >= 2:
                chat_agent.history[-2].parts[0].text = f"[Система сообщила результат действия {action['name']}]"
                
        elif action["type"] == "native":
            response = chat_agent.send_message({"function_response": {"name": action["name"], "response": {"result": str(res)}}})
            
        if hasattr(response, 'usage_metadata'): track_token_usage(response.usage_metadata.total_token_count)
        trim_chat_history(chat_agent)
        parse_and_route_response(chat_id, response, action["msg_id"], action["orig_text"])
        
    except Exception as e:
        clear_status(chat_id)
        bot.send_message(chat_id, f"❌ Ошибка ИИ (Loop): {e}")


# ─────────────────────────────────────────────
#  ОБРАБОТКА CALLBACKS (КНОПКИ)
# ─────────────────────────────────────────────

@bot.callback_query_handler(func=lambda call: True)
def handle_query(call):
    if call.from_user.id not in ADMIN_IDS: return
    global CURRENT_MODEL, CURRENT_KEY_NUM, PRIORITY_MODELS_CACHE, OTHER_MODELS_CACHE, AVAILABLE_MODELS
    global CURRENT_CHAT_ID, MODEL_ROLE, MODEL_MODE, PENDING_ACTION
    data = call.data

    # ВЫБОР РЕЖИМА ПОИСКА
    if data in ["search_type_regular", "search_type_archive"]:
        search_type = "archive" if "archive" in data else "regular"
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        
        mode_text = "В архивах (.zip, .7z)" if search_type == "archive" else "Обычные базы (.csv)"
        msg = bot.send_message(call.message.chat.id, f"🔍 Режим: <b>{mode_text}</b>\n\nВведите поисковый запрос (фразы в [квадратных скобках] ищутся целиком):", parse_mode='HTML')
        bot.register_next_step_handler(msg, process_search_query, search_type=search_type)
        return

    if data == "voice_on":
        VOICE_MODE[call.message.chat.id] = True
        safe_edit_message(call.message.chat.id, call.message.message_id, "🟢 Голосовой режим <b>ВКЛЮЧЕН</b>.", parse_mode='HTML')
        return
    if data == "voice_off":
        VOICE_MODE[call.message.chat.id] = False
        safe_edit_message(call.message.chat.id, call.message.message_id, "🔴 Голосовой режим <b>ВЫКЛЮЧЕН</b>.", parse_mode='HTML')
        return
    if data == "hide_message":
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        return

    if data.startswith("show_acts_"):
        try: target_msg_id = int(data.split("_")[2])
        except Exception: target_msg_id = 0
        actions = LAST_ACTIONS.get(target_msg_id)
        if not actions:
            bot.answer_callback_query(call.id, "❌ Данные об этих действиях устарели.", show_alert=True)
            return

        log_text = ""
        bash_commands = []
        for act_type, act_val in actions:
            if act_type == "bash": bash_commands.append(act_val)
            elif act_type == "search": log_text += f"🌐 <b>Поиск:</b> <i>{html.escape(act_val)}</i>\n"
            elif act_type == "download": log_text += f"⬇️ <b>Скачан файл:</b> <i>{html.escape(act_val)}</i>\n"
            elif act_type == "file": log_text += f"📤 <b>Отправлен файл:</b> <i>{html.escape(act_val)}</i>\n"

        if bash_commands:
            bash_str = "\n".join(bash_commands)
            log_text += f'\n<pre><code class="language-bash">{html.escape(bash_str)}</code></pre>'

        hide_markup = InlineKeyboardMarkup()
        hide_markup.add(InlineKeyboardButton("❌ Скрыть", callback_data="hide_message"))
        bot.send_message(call.message.chat.id, log_text.strip(), parse_mode='HTML', reply_markup=hide_markup)
        bot.answer_callback_query(call.id)
        return

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
        except Exception as e: safe_edit_message(call.message.chat.id, call.message.message_id, f"❌ Ошибка: {str(e)}")
        finally:
            if os.path.exists(temp_filename): os.remove(temp_filename)
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
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        bot.send_message(call.message.chat.id, f"✅ Активен <b>KEY {key_num}</b>.\nВызовите /gemini для выбора модели.", parse_mode='HTML')
        return

    # ВЫБОР МОДЕЛИ -> РОЛИ -> РЕЖИМА
    if data.startswith("mod_"):
        model_name = data.replace("mod_", "")
        CURRENT_MODEL = model_name
        clean_name = CURRENT_MODEL.replace('models/', '')
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except: pass
        
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("🛠 Админ", callback_data="role_admin"), InlineKeyboardButton("💬 Чат-бот", callback_data="role_chat"))
        bot.send_message(call.message.chat.id, f"Выбрана модель <b>{clean_name}</b>.\nВыберите роль ИИ:", reply_markup=markup, parse_mode='HTML')
        return

    if data.startswith("role_"):
        role = data.replace("role_", "")
        MODEL_ROLE[call.message.chat.id] = role
        clean_name = CURRENT_MODEL.replace('models/', '')
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except: pass
        
        if role == "admin":
            markup = InlineKeyboardMarkup()
            markup.row(InlineKeyboardButton("⚡ Авто", callback_data="mode_auto"), InlineKeyboardButton("🛑 Полуавтомат", callback_data="mode_semi"))
            bot.send_message(call.message.chat.id, f"Выберите режим выполнения команд для <b>{clean_name}</b>:", parse_mode='HTML', reply_markup=markup)
        else:
            MODEL_MODE[call.message.chat.id] = "auto"
            init_models(CURRENT_MODEL, role="chat", mode="auto")
            bot.send_message(call.message.chat.id, f"✅ <b>{clean_name}</b> запущен в роли: <b>Чат-бот</b>", parse_mode='HTML')
        return

    if data.startswith("mode_"):
        mode = data.replace("mode_", "")
        MODEL_MODE[call.message.chat.id] = mode
        clean_name = CURRENT_MODEL.replace('models/', '')
        init_models(CURRENT_MODEL, role="admin", mode=mode)
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except: pass
        m_text = "АВТО (выполняет функции сам)" if mode == "auto" else "ПОЛУАВТОМАТ (спрашивает разрешение)"
        bot.send_message(call.message.chat.id, f"✅ <b>{clean_name}</b> запущен в роли: <b>Админ -> {m_text}</b>", parse_mode='HTML')
        return

    # УПРАВЛЕНИЕ ДЕЙСТВИЯМИ (Полуавтомат)
    if data.startswith("act_yes_"):
        action = PENDING_ACTION.get(call.message.chat.id)
        if not action:
            bot.answer_callback_query(call.id, "❌ Действие устарело.", show_alert=True)
            return
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except: pass
        execute_pending_action(call.message.chat.id, action)
        return

    if data.startswith("act_no_"):
        action = PENDING_ACTION.pop(call.message.chat.id, None)
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except: pass
        bot.send_message(call.message.chat.id, "❌ Действие отменено пользователем.")
        
        if action:
            status_text = "🧠 <b>Сообщаю об отмене...</b>"
            set_status(call.message.chat.id, status_text)
            try:
                check_api_rate_limit(call.message.chat.id, status_text)
                if action["type"] == "react":
                    resp = chat_agent.send_message("ПОЛЬЗОВАТЕЛЬ ЗАПРЕТИЛ ВЫПОЛНЕНИЕ ЭТОЙ ОПЕРАЦИИ. Ответь пользователю.")
                elif action["type"] == "native":
                    resp = chat_agent.send_message({"function_response": {"name": action["name"], "response": {"result": "ERROR: User denied permission to execute."}}})
                parse_and_route_response(call.message.chat.id, resp, action["msg_id"], action["orig_text"])
            except Exception as e:
                clear_status(call.message.chat.id)
                bot.send_message(call.message.chat.id, f"❌ Ошибка ИИ: {e}")
        return

    # ФАЙЛЫ
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
            except Exception as e: safe_edit_message(call.message.chat.id, call.message.message_id, f"❌ Ошибка: {e}")
            PENDING_FILES.pop(call.message.chat.id, None)
            return

        if data == "file_ai":
            if not CURRENT_MODEL:
                bot.answer_callback_query(call.id, "⚠️ Выберите модель (/gemini)!", show_alert=True)
                return
            clean_name = CURRENT_MODEL.replace('models/', '')
            is_gemma = "gemma" in clean_name.lower()
            CURRENT_CHAT_ID = call.message.chat.id
            ACTION_LOGS[CURRENT_CHAT_ID] = []
            STATUS_MSG.pop(CURRENT_CHAT_ID, None)  
            safe_edit_message(call.message.chat.id, call.message.message_id, f"<b>{clean_name}:</b>\n🧠 Читаю файл...")
            status_text = "🧠 <b>Анализирую файл...</b>"
            set_status(call.message.chat.id, status_text)

            try:
                file_info = bot.get_file(file_info_dict['file_id'])
                temp_file_name = f"temp_ai_{file_info_dict['file_name']}"
                with open(temp_file_name, 'wb') as new_file: new_file.write(bot.download_file(file_info.file_path))
                mime = file_info_dict['mime_type']
                gemini_file = genai.upload_file(path=temp_file_name, mime_type=mime) if mime else genai.upload_file(path=temp_file_name)

                if is_gemma:
                    response = chat_agent.send_message("Я текстовая модель Gemma, я пока не умею читать файлы напрямую.")
                    parse_and_route_response(call.message.chat.id, response, call.message.message_id, "file")
                else:
                    check_api_rate_limit(call.message.chat.id, status_text)
                    response = chat_agent.send_message([gemini_file, "Проанализируй этот файл. Расскажи, что в нём, либо выполни инструкции."])
                    trim_chat_history(chat_agent)
                    parse_and_route_response(call.message.chat.id, response, call.message.message_id, "file")
                os.remove(temp_file_name)

            except Exception as e:
                clear_status(call.message.chat.id)
                err_msg = bot.send_message(call.message.chat.id, "❌")
                handle_api_error(e, call.message.chat.id, err_msg.message_id, None, clean_name)
            PENDING_FILES.pop(call.message.chat.id, None)
            return

# ─────────────────────────────────────────────
#  ГЛАВНЫЙ ОБРАБОТЧИК СООБЩЕНИЙ
# ─────────────────────────────────────────────

@bot.message_handler(content_types=['voice', 'text', 'photo'])
def handle_message(message):
    if message.from_user.id not in ADMIN_IDS: return

    global CURRENT_CHAT_ID
    CURRENT_CHAT_ID = message.chat.id
    text = (message.text or message.caption or "").strip()

    if text.startswith('/'):
        cmd = text.split()[0].lower()
        if cmd == '/voice': voice_mode_cmd(message)
        elif cmd == '/search': search_cmd(message)
        elif cmd == '/clear': clear_cmd(message)
        elif cmd == '/gemini': change_model(message)
        elif cmd == '/changekey': change_key_cmd(message)
        elif cmd == '/reload': reload_configs_cmd(message)
        elif cmd == '/help': send_help(message)
        elif cmd == '/start': send_welcome(message)
        else: bot.reply_to(message, "⚠️ Неизвестная команда. Введите /help для справки.")
        return

    if not CURRENT_MODEL:
        bot.reply_to(message, "⚠️ Сначала выберите модель (/gemini)", reply_markup=get_models_keyboard())
        return

    pending = PENDING_ACTION.pop(message.chat.id, None)
    if pending and pending.get("type") == "native":
        try:
            chat_agent.send_message({"function_response": {"name": pending["name"], "response": {"result": "Отменено пользователем (написал новое сообщение)"}}})
        except Exception:
            pass

    clean_model_name = CURRENT_MODEL.replace('models/', '')
    is_gemma = "gemma" in clean_model_name.lower()
    role = MODEL_ROLE.get(message.chat.id, "admin")

    is_voice = message.content_type == 'voice'
    is_photo = message.content_type == 'photo'

    if not is_voice and not is_photo:
        if text.startswith('!'):
            cmd = text[1:].strip()
            bot.send_message(message.chat.id, f"⚡ Выполняю напрямую:\n<code>{html.escape(cmd)}</code>", parse_mode='HTML')
            result = execute_bash(cmd)
            send_long_text(message.chat.id, result, is_code=True)
            return

        if text.startswith('#'):
            task = text[1:].strip()
            msg_first = bot.send_message(message.chat.id, f"<b>{clean_model_name}:</b>", parse_mode='HTML')
            status_text = "🧠 <b>Думаю...</b>"
            set_status(message.chat.id, status_text)
            try:
                check_api_rate_limit(message.chat.id, status_text)
                response = model_advisor.generate_content(task)
                clear_status(message.chat.id)
                send_long_text(message.chat.id, response.text, first_msg_id=msg_first.message_id, is_code=True)
            except Exception as e:
                clear_status(message.chat.id)
                handle_api_error(e, message.chat.id, msg_first.message_id, message, clean_model_name)
            return

    ACTION_LOGS[CURRENT_CHAT_ID] = []
    STATUS_MSG.pop(CURRENT_CHAT_ID, None)

    msg_first = bot.send_message(message.chat.id, f"<b>{clean_model_name}:</b>", parse_mode='HTML')
    status_text = "🤖 <b>Обрабатываю запрос...</b>"
    set_status(message.chat.id, status_text)

    try:
        if is_voice:
            set_status(message.chat.id, "🎙 <b>Слушаю голосовое сообщение...</b>")
            file_info = bot.get_file(message.voice.file_id)
            voice_path = f"temp_voice_{message.message_id}.ogg"
            with open(voice_path, 'wb') as new_file: new_file.write(bot.download_file(file_info.file_path))

            audio_file = genai.upload_file(path=voice_path, mime_type="audio/ogg")
            status_text = "🧠 <b>Анализирую аудио...</b>"
            set_status(message.chat.id, status_text)
            
            if is_gemma:
                response = chat_agent.send_message("Я текстовая модель Gemma и не умею слушать звук.")
                if hasattr(response, 'usage_metadata'): track_token_usage(response.usage_metadata.total_token_count)
            else:
                base_prompt = "Прослушай этот аудиофайл. Если в нём звучит команда для сервера — выполни её. Если обычный разговор — ответь."
                prompt = f"{text}\n\n{base_prompt}" if text else base_prompt
                
                check_api_rate_limit(message.chat.id, status_text)
                response = chat_agent.send_message([audio_file, prompt])
                if hasattr(response, 'usage_metadata'): track_token_usage(response.usage_metadata.total_token_count)
                trim_chat_history(chat_agent)

            os.remove(voice_path)

        elif is_photo:
            set_status(message.chat.id, "🖼 <b>Анализирую изображение...</b>")
            file_info = bot.get_file(message.photo[-1].file_id)
            photo_path = f"temp_photo_{message.message_id}.jpg"
            with open(photo_path, 'wb') as new_file: new_file.write(bot.download_file(file_info.file_path))

            img_file = genai.upload_file(path=photo_path)
            status_text = "🖼 <b>Анализирую изображение...</b>"
            set_status(message.chat.id, status_text)
            
            if is_gemma:
                response = chat_agent.send_message("Я текстовая модель Gemma и не умею смотреть картинки.")
                if hasattr(response, 'usage_metadata'): track_token_usage(response.usage_metadata.total_token_count)
            else:
                prompt = text if text else "Проанализируй это изображение."
                check_api_rate_limit(message.chat.id, status_text)
                response = chat_agent.send_message([img_file, prompt])
                if hasattr(response, 'usage_metadata'): track_token_usage(response.usage_metadata.total_token_count)
                trim_chat_history(chat_agent)

            os.remove(photo_path)

        else:
            check_api_rate_limit(message.chat.id, status_text)
            
            if is_gemma and role == "admin":
                react_sys = "\n\n" + PROMPTS.get("GEMMA_ADMIN_REACT", "[SYSTEM] Используй теги: <BASH>, <SEARCH>, <DOWNLOAD>, <FILE>.")
                final_text = text + react_sys
                response = chat_agent.send_message(final_text)
                
                if hasattr(chat_agent, 'history') and len(chat_agent.history) >= 2:
                    chat_agent.history[-2].parts[0].text = text 
                
                if hasattr(response, 'usage_metadata'): track_token_usage(response.usage_metadata.total_token_count)
                trim_chat_history(chat_agent) 
                
            else:
                response = chat_agent.send_message(text)
                if hasattr(response, 'usage_metadata'): track_token_usage(response.usage_metadata.total_token_count)
                trim_chat_history(chat_agent) 

        parse_and_route_response(message.chat.id, response, msg_first.message_id, text)

    except Exception as e:
        clear_status(message.chat.id)
        handle_api_error(e, message.chat.id, msg_first.message_id, message, clean_model_name)

if __name__ == '__main__':
    print(f"AI-Админ запущен. Допущено админов: {len(ADMIN_IDS)}.")
    bot.polling(none_stop=True)
