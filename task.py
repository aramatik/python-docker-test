import os
import json
import uuid
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

TASKS_DIR = "/app/downloads/tasks"
TASKS_FILE = os.path.join(TASKS_DIR, "tasks.json")

scheduler = BackgroundScheduler(timezone="Europe/Kiev") # Можете поменять таймзону на вашу
execute_callback = None

def init_scheduler(callback_fn):
    """Инициализирует планировщик и загружает сохраненные задачи."""
    global execute_callback
    execute_callback = callback_fn
    
    os.makedirs(TASKS_DIR, exist_ok=True)
    if not os.path.exists(TASKS_FILE):
        with open(TASKS_FILE, "w", encoding="utf-8") as f:
            json.dump([], f)
            
    load_tasks()
    scheduler.start()
    print("Планировщик задач успешно запущен.")

def load_tasks():
    """Загружает задачи из файла и добавляет их в расписание."""
    try:
        with open(TASKS_FILE, "r", encoding="utf-8") as f:
            tasks = json.load(f)
        for t in tasks:
            _schedule_job(t)
    except Exception as e:
        print(f"Ошибка загрузки задач: {e}")

def _schedule_job(task):
    """Добавляет задачу непосредственно в ядро apscheduler."""
    try:
        trigger = CronTrigger.from_crontab(task["cron"])
        scheduler.add_job(
            execute_callback,
            trigger=trigger,
            args=[task["chat_id"], task["prompt"], task["model"]],
            id=task["id"],
            replace_existing=True
        )
    except Exception as e:
        print(f"Не удалось запланировать задачу {task['id']}: {e}")

def add_task(chat_id, cron_expr, prompt, model):
    """Создает новую задачу, сохраняет в файл и запускает."""
    task_id = str(uuid.uuid4())[:8] # Генерируем короткий уникальный ID
    task = {
        "id": task_id,
        "chat_id": chat_id,
        "cron": cron_expr,
        "prompt": prompt,
        "model": model
    }
    
    with open(TASKS_FILE, "r", encoding="utf-8") as f:
        tasks = json.load(f)
        
    tasks.append(task)
    
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=4)
        
    _schedule_job(task)
    return task_id

def get_all_tasks(chat_id):
    """Возвращает список всех задач для конкретного чата."""
    with open(TASKS_FILE, "r", encoding="utf-8") as f:
        tasks = json.load(f)
    return [t for t in tasks if t["chat_id"] == chat_id]

def delete_task(chat_id, task_id):
    """Удаляет задачу по ID."""
    with open(TASKS_FILE, "r", encoding="utf-8") as f:
        tasks = json.load(f)
        
    initial_len = len(tasks)
    tasks = [t for t in tasks if not (t["id"] == task_id and t["chat_id"] == chat_id)]
    
    if len(tasks) < initial_len:
        with open(TASKS_FILE, "w", encoding="utf-8") as f:
            json.dump(tasks, f, ensure_ascii=False, indent=4)
        try:
            scheduler.remove_job(task_id)
        except:
            pass
        return True
    return False
    
