import asyncio
import logging
import os
import re
import sqlite3
import base64
import uuid
import json
from datetime import datetime, timedelta, date
from typing import Optional, Tuple
from dateutil.relativedelta import relativedelta

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from dotenv import load_dotenv
import aiohttp
import gspread
from google.oauth2.service_account import Credentials
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
import pytz

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Токены
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GIGACHAT_CLIENT_ID = os.getenv("GIGACHAT_CLIENT_ID")
GIGACHAT_CLIENT_SECRET = os.getenv("GIGACHAT_CLIENT_SECRET")
ADMIN_ID = 295220429
GOOGLE_SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID")
GOOGLE_SHEETS_CREDENTIALS = os.getenv("GOOGLE_SHEETS_CREDENTIALS")

# Часовой пояс
TIMEZONE = pytz.timezone('Europe/Moscow')

# Инициализация бота
bot = Bot(token=TELEGRAM_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

# Словарь для отслеживания напоминаний о завершении тренировки
workout_reminders = {}

# FSM состояния
class WorkoutStates(StatesGroup):
    entering_sets = State()

class ProfileStates(StatesGroup):
    entering_name = State()
    entering_gender = State()
    entering_birthdate = State()
    entering_weight = State()
    updating_weight = State()

# Группы мышц
MUSCLE_GROUPS = {
    # Грудь
    'жим лежа': 'Грудь', 'жим гантелей': 'Грудь', 'разводка': 'Грудь', 
    'отжимания': 'Грудь', 'жим на наклонной': 'Грудь', 'кроссовер': 'Грудь',
    'бабочка': 'Грудь', 'пуловер': 'Грудь',
    
    # Спина
    'становая тяга': 'Спина', 'тяга штанги': 'Спина', 'тяга гантели': 'Спина',
    'подтягивания': 'Спина', 'тяга верхнего блока': 'Спина', 'тяга горизонтального блока': 'Спина',
    'тяга нижнего блока': 'Спина', 'гиперэкстензия': 'Спина', 'тяга т-грифа': 'Спина',
    
    # Плечи
    'жим стоя': 'Плечи', 'жим сидя': 'Плечи', 'махи гантелей': 'Плечи',
    'махи в стороны': 'Плечи', 'махи вперед': 'Плечи', 'тяга к подбородку': 'Плечи',
    'разведение гантелей': 'Плечи', 'армейский жим': 'Плечи',
    
    # Бицепс
    'сгибания на бицепс': 'Бицепс', 'подъем на бицепс': 'Бицепс', 'молотки': 'Бицепс',
    'бицепс штанга': 'Бицепс', 'бицепс гантели': 'Бицепс', 'концентрированные сгибания': 'Бицепс',
    
    # Трицепс
    'французский жим': 'Трицепс', 'брусья': 'Трицепс', 'разгибания на трицепс': 'Трицепс',
    'отжимания на брусьях': 'Трицепс', 'разгибания рук': 'Трицепс', 'трицепс блок': 'Трицепс',
    
    # Ноги
    'приседания': 'Ноги', 'присед': 'Ноги', 'выпады': 'Ноги', 'жим ногами': 'Ноги',
    'разгибания ног': 'Ноги', 'сгибания ног': 'Ноги', 'икры': 'Ноги',
    'подъем на носки': 'Ноги', 'румынская тяга': 'Ноги', 'мертвая тяга': 'Ноги',
    
    # Пресс
    'пресс': 'Пресс', 'скручивания': 'Пресс', 'планка': 'Пресс', 'подъем ног': 'Пресс',
    'велосипед пресс': 'Пресс', 'косые мышцы': 'Пресс', 'вакуум': 'Пресс',
    
    # Кардио
    'бег': 'Кардио', 'велосипед': 'Кардио', 'эллипс': 'Кардио', 'ходьба': 'Кардио',
    'скакалка': 'Кардио', 'гребля': 'Кардио', 'степпер': 'Кардио', 'плавание': 'Кардио',
}

WEEKDAYS_RU = {
    0: 'Понедельник', 1: 'Вторник', 2: 'Среда', 3: 'Четверг',
    4: 'Пятница', 5: 'Суббота', 6: 'Воскресенье'
}

# ============ ИНИЦИАЛИЗАЦИЯ БД ============

def init_db():
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    
    # Таблица пользователей (профили)
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER UNIQUE,
                  name TEXT,
                  gender TEXT,
                  birthdate TEXT,
                  created_at TEXT)''')
    
    # Таблица весов пользователя
    c.execute('''CREATE TABLE IF NOT EXISTS user_weights
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  weight_kg REAL,
                  recorded_at TEXT)''')
    
    # Таблица тренировок
    c.execute('''CREATE TABLE IF NOT EXISTS workouts
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  workout_id TEXT,
                  workout_number INTEGER,
                  start_time TEXT,
                  end_time TEXT,
                  duration_minutes INTEGER)''')
    
    # Таблица упражнений
    c.execute('''CREATE TABLE IF NOT EXISTS exercises
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  workout_id TEXT,
                  exercise_name TEXT,
                  muscle_group TEXT,
                  timestamp TEXT)''')
    
    # Таблица подходов
    c.execute('''CREATE TABLE IF NOT EXISTS sets
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  exercise_id INTEGER,
                  weight_kg REAL,
                  reps INTEGER,
                  duration_seconds INTEGER,
                  set_type TEXT,
                  timestamp TEXT)''')
    
    conn.commit()
    conn.close()

init_db()

# ============ ФУНКЦИИ ДЛЯ ПРОФИЛЯ ============

def get_user_profile(user_id: int) -> Optional[dict]:
    """Получает профиль пользователя"""
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('SELECT name, gender, birthdate, created_at FROM users WHERE user_id = ?', (user_id,))
    result = c.fetchone()
    conn.close()
    
    if result:
        return {
            'name': result[0],
            'gender': result[1],
            'birthdate': result[2],
            'created_at': result[3]
        }
    return None

def create_user_profile(user_id: int, name: str, gender: str, birthdate: str):
    """Создаёт профиль пользователя"""
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO users (user_id, name, gender, birthdate, created_at)
                 VALUES (?, ?, ?, ?, ?)''',
              (user_id, name, gender, birthdate, datetime.now(TIMEZONE).isoformat()))
    conn.commit()
    conn.close()

def get_user_current_weight(user_id: int) -> Optional[float]:
    """Получает последний вес пользователя"""
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('''SELECT weight_kg FROM user_weights 
                 WHERE user_id = ? ORDER BY recorded_at DESC LIMIT 1''', (user_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None

def get_user_first_weight(user_id: int) -> Optional[float]:
    """Получает первый (начальный) вес пользователя"""
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('''SELECT weight_kg FROM user_weights 
                 WHERE user_id = ? ORDER BY recorded_at ASC LIMIT 1''', (user_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None

def get_user_previous_weight(user_id: int) -> Optional[float]:
    """Получает предыдущий вес (до последнего)"""
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('''SELECT weight_kg FROM user_weights 
                 WHERE user_id = ? ORDER BY recorded_at DESC LIMIT 2''', (user_id,))
    results = c.fetchall()
    conn.close()
    return results[1][0] if len(results) > 1 else None

def add_user_weight(user_id: int, weight: float):
    """Добавляет запись веса пользователя"""
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('''INSERT INTO user_weights (user_id, weight_kg, recorded_at)
                 VALUES (?, ?, ?)''',
              (user_id, weight, datetime.now(TIMEZONE).isoformat()))
    conn.commit()
    conn.close()

def get_last_weight_date(user_id: int) -> Optional[datetime]:
    """Получает дату последнего взвешивания"""
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('''SELECT recorded_at FROM user_weights 
                 WHERE user_id = ? ORDER BY recorded_at DESC LIMIT 1''', (user_id,))
    result = c.fetchone()
    conn.close()
    if result:
        return datetime.fromisoformat(result[0])
    return None

def needs_weight_update(user_id: int) -> bool:
    """Проверяет, нужно ли обновить вес (прошло больше 30 дней)"""
    last_date = get_last_weight_date(user_id)
    if not last_date:
        return True
    
    now = datetime.now(TIMEZONE)
    if last_date.tzinfo is None:
        last_date = TIMEZONE.localize(last_date)
    
    return (now - last_date).days >= 30

def calculate_age(birthdate_str: str) -> int:
    """Вычисляет возраст по дате рождения"""
    birthdate = datetime.strptime(birthdate_str, '%d.%m.%Y').date()
    today = date.today()
    age = today.year - birthdate.year - ((today.month, today.day) < (birthdate.month, birthdate.day))
    return age

# ============ ФУНКЦИИ ДЛЯ ТРЕНИРОВОК ============

def get_next_workout_number(user_id: int) -> int:
    """Получает номер следующей тренировки"""
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('SELECT MAX(workout_number) FROM workouts WHERE user_id = ?', (user_id,))
    result = c.fetchone()
    conn.close()
    return (result[0] or 0) + 1

def get_unfinished_workout(user_id: int) -> Optional[dict]:
    """Проверяет есть ли незавершённая тренировка"""
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('''SELECT workout_id, start_time, workout_number 
                 FROM workouts 
                 WHERE user_id = ? AND end_time IS NULL
                 ORDER BY start_time DESC LIMIT 1''', (user_id,))
    result = c.fetchone()
    conn.close()
    
    if result:
        return {'workout_id': result[0], 'start_time': result[1], 'workout_number': result[2]}
    return None

def get_workout_exercises_count(workout_id: str) -> int:
    """Подсчитывает количество упражнений в тренировке"""
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM exercises WHERE workout_id = ?', (workout_id,))
    count = c.fetchone()[0]
    conn.close()
    return count

def finish_workout_in_db(workout_id: str, user_id: int) -> dict:
    """Завершает тренировку в БД"""
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    
    c.execute('SELECT start_time FROM workouts WHERE workout_id = ?', (workout_id,))
    result = c.fetchone()
    
    if not result:
        conn.close()
        return {'error': 'Тренировка не найдена'}
    
    start_time = datetime.fromisoformat(result[0])
    end_time = datetime.now(TIMEZONE)
    
    if start_time.tzinfo is None:
        start_time = TIMEZONE.localize(start_time)
    
    duration = int((end_time - start_time).total_seconds() / 60)
    
    c.execute('''UPDATE workouts SET end_time = ?, duration_minutes = ? WHERE workout_id = ?''',
              (end_time.isoformat(), duration, workout_id))
    
    c.execute('''SELECT COUNT(DISTINCT e.id), COUNT(s.id), 
                        SUM(CASE WHEN s.set_type = 'strength' THEN s.weight_kg * s.reps ELSE 0 END)
                 FROM exercises e
                 LEFT JOIN sets s ON e.id = s.exercise_id
                 WHERE e.workout_id = ?''', (workout_id,))
    exercises_count, sets_count, tonnage = c.fetchone()
    
    conn.commit()
    conn.close()
    
    return {
        'duration': duration,
        'exercises_count': exercises_count or 0,
        'sets_count': sets_count or 0,
        'tonnage': tonnage or 0
    }

def cancel_workout_in_db(workout_id: str) -> bool:
    """Отменяет тренировку"""
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('DELETE FROM sets WHERE exercise_id IN (SELECT id FROM exercises WHERE workout_id = ?)', (workout_id,))
    c.execute('DELETE FROM exercises WHERE workout_id = ?', (workout_id,))
    c.execute('DELETE FROM workouts WHERE workout_id = ?', (workout_id,))
    conn.commit()
    conn.close()
    return True

# ============ ОПРЕДЕЛЕНИЕ ГРУППЫ МЫШЦ ============

async def get_muscle_group(exercise_name: str) -> str:
    """Определяет группу мышц по названию упражнения"""
    exercise_lower = exercise_name.lower()
    
    # Сначала ищем в локальном словаре
    for key, group in MUSCLE_GROUPS.items():
        if key in exercise_lower:
            return group
    
    # Если не нашли — спрашиваем GigaChat
    try:
        token = await get_gigachat_token()
        if token:
            url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
            headers = {
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {token}'
            }
            payload = {
                "model": "GigaChat",
                "messages": [
                    {"role": "system", "content": "Ответь одним словом — название группы мышц. Варианты: Грудь, Спина, Плечи, Бицепс, Трицепс, Ноги, Пресс, Кардио, Другое"},
                    {"role": "user", "content": f"Какая группа мышц: {exercise_name}"}
                ],
                "temperature": 0.1,
                "max_tokens": 20
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload, ssl=False) as response:
                    if response.status == 200:
                        result = await response.json()
                        return result['choices'][0]['message']['content'].strip()
    except Exception as e:
        logger.error(f"Error getting muscle group: {e}")
    
    return "Другое"

# ============ ПАРСЕР УПРАЖНЕНИЙ (ОБНОВЛЁННЫЙ) ============

def parse_workout_input(text: str, user_weight: float = 0) -> Tuple[Optional[str], Optional[float], Optional[int], Optional[int], str]:
    """Парсит ввод упражнения с учётом собственного веса"""
    text = text.strip()
    
    # Паттерн 1: Упражнение + вес-повторы (Жим лежа 80-10)
    pattern1 = r'^(.+?)\s+(\d+(?:\.\d+)?)\s*[-xх×*]\s*(\d+)$'
    match = re.match(pattern1, text, re.IGNORECASE)
    if match:
        exercise = match.group(1).strip()
        weight = float(match.group(2))
        reps = int(match.group(3))
        return (exercise, weight, reps, None, 'strength')
    
    # Паттерн 2: Только вес-повторы (80-10) — для продолжения упражнения
    pattern2 = r'^(\d+(?:\.\d+)?)\s*[-xх×*]\s*(\d+)$'
    match = re.match(pattern2, text)
    if match:
        weight = float(match.group(1))
        reps = int(match.group(2))
        return (None, weight, reps, None, 'strength')
    
    # Паттерн 3: Упражнение "с весом" + доп.вес-повторы (Подтягивания с весом 15-12)
    pattern3 = r'^(.+?)\s+с\s+весом\s+(\d+(?:\.\d+)?)\s*[-xх×*]\s*(\d+)$'
    match = re.match(pattern3, text, re.IGNORECASE)
    if match:
        exercise = match.group(1).strip()
        extra_weight = float(match.group(2))
        reps = int(match.group(3))
        total_weight = user_weight + extra_weight
        return (exercise, total_weight, reps, None, 'strength')
    
    # Паттерн 4: Упражнение + только повторы (Подтягивания 12) — свой вес
    pattern4 = r'^(.+?)\s+(\d+)$'
    match = re.match(pattern4, text, re.IGNORECASE)
    if match:
        exercise = match.group(1).strip()
        reps = int(match.group(2))
        # Проверяем что это не минуты/секунды
        if not any(word in exercise.lower() for word in ['мин', 'сек', 'час']):
            return (exercise, user_weight, reps, None, 'strength')
    
    # Паттерн 5: Упражнение + минуты (Бег 5 минут / Бег 5 мин / Бег 5м)
    pattern5 = r'^(.+?)\s+(\d+)\s*(мин|минут|минуты|min|м)\s*$'
    match = re.match(pattern5, text, re.IGNORECASE)
    if match:
        exercise = match.group(1).strip()
        minutes = int(match.group(2))
        return (exercise, None, None, minutes * 60, 'cardio')
    
    # Паттерн 6: Упражнение + часы (Велосипед 1 час)
    pattern6 = r'^(.+?)\s+(\d+)\s*(час|часа|часов|ч|hour|h)\s*$'
    match = re.match(pattern6, text, re.IGNORECASE)
    if match:
        exercise = match.group(1).strip()
        hours = int(match.group(2))
        return (exercise, None, None, hours * 3600, 'cardio')
    
    # Паттерн 7: Упражнение + секунды (Планка 60 секунд)
    pattern7 = r'^(.+?)\s+(\d+)\s*(сек|секунд|секунды|sec|с)\s*$'
    match = re.match(pattern7, text, re.IGNORECASE)
    if match:
        exercise = match.group(1).strip()
        seconds = int(match.group(2))
        return (exercise, None, None, seconds, 'static')
    
    return (None, None, None, None, 'unknown')

# ============ GIGACHAT API ============

async def get_gigachat_token():
    if not GIGACHAT_CLIENT_ID or not GIGACHAT_CLIENT_SECRET:
        return None
    
    credentials = f"{GIGACHAT_CLIENT_ID}:{GIGACHAT_CLIENT_SECRET}"
    auth_key = base64.b64encode(credentials.encode()).decode()
    
    url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Accept': 'application/json',
        'RqUID': str(uuid.uuid4()),
        'Authorization': f'Basic {auth_key}'
    }
    data = {'scope': 'GIGACHAT_API_PERS'}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=data, ssl=False) as response:
                if response.status == 200:
                    result = await response.json()
                    return result.get('access_token')
    except Exception as e:
        logger.error(f"GigaChat token error: {e}")
    return None

async def ask_gigachat(user_id: int, question: str):
    """Отправляет вопрос в GigaChat с КРАТКИМИ ответами"""
    try:
        conn = sqlite3.connect('workouts.db')
        c = conn.cursor()
        c.execute('''SELECT w.start_time, e.exercise_name, s.weight_kg, s.reps, s.duration_seconds, s.set_type
                     FROM workouts w
                     JOIN exercises e ON w.workout_id = e.workout_id
                     JOIN sets s ON e.id = s.exercise_id
                     WHERE w.user_id = ?
                     ORDER BY w.start_time DESC LIMIT 50''', (user_id,))
        history = c.fetchall()
        conn.close()
        
        context = "История тренировок (последние):\n"
        for row in history[:10]:
            date, exercise, weight, reps, duration, set_type = row
            if set_type == 'strength':
                context += f"- {exercise}: {weight}кг x {reps}\n"
        
        token = await get_gigachat_token()
        if not token:
            return "Ошибка подключения к GigaChat."
        
        url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {token}'
        }
        
        # ОБНОВЛЁННЫЙ ПРОМПТ для кратких ответов
        system_prompt = f"""Ты — персональный фитнес-тренер. 

ПРАВИЛА ОТВЕТА:
1. Отвечай КРАТКО — максимум 500 символов
2. Используй структуру: эмодзи + тезисы + короткий вывод
3. Без длинных вступлений и "воды"
4. Только суть и практические советы

{context}"""
        
        payload = {
            "model": "GigaChat",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question}
            ],
            "temperature": 0.7,
            "max_tokens": 500
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, ssl=False) as response:
                if response.status == 200:
                    result = await response.json()
                    return result['choices'][0]['message']['content']
                return "Ошибка GigaChat API"
                
    except Exception as e:
        logger.error(f"GigaChat error: {e}")
        return "Ошибка при обращении к ИИ"
# ============ GOOGLE SHEETS ============

def get_google_sheets_client():
    try:
        if not GOOGLE_SHEETS_CREDENTIALS:
            return None
        creds_dict = json.loads(GOOGLE_SHEETS_CREDENTIALS)
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        return client
    except Exception as e:
        logger.error(f"Google Sheets auth error: {e}")
        return None

def get_or_create_user_sheet(client, spreadsheet, user_id: int):
    """Получает или создаёт лист для пользователя"""
    sheet_name = str(user_id)
    try:
        worksheet = spreadsheet.worksheet(sheet_name)
    except:
        # Создаём новый лист
        worksheet = spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=20)
        # Добавляем заголовки
        headers = [
            "User ID", "Имя", "Дата рождения", "Вес user", "ID тренировки",
            "Дата и время начала", "Дата и время окончания", "День недели",
            "Длительность, мин", "Упражнение", "Группа мышц", "Вес (кг)",
            "Повторы", "Время кардио, мин"
        ]
        worksheet.append_row(headers)
    return worksheet

async def sync_to_google_sheets():
    """Синхронизация данных в Google Sheets"""
    try:
        client = get_google_sheets_client()
        if not client:
            logger.error("Не удалось подключиться к Google Sheets")
            return
        
        spreadsheet = client.open_by_key(GOOGLE_SPREADSHEET_ID)
        today = datetime.now(TIMEZONE).date()
        
        conn = sqlite3.connect('workouts.db')
        c = conn.cursor()
        
        # Получаем все завершённые тренировки за сегодня
        c.execute('''SELECT DISTINCT w.user_id, w.workout_id, w.workout_number, 
                            w.start_time, w.end_time, w.duration_minutes
                     FROM workouts w
                     WHERE date(w.start_time) = ? AND w.end_time IS NOT NULL''', 
                  (today.isoformat(),))
        
        workouts = c.fetchall()
        
        if not workouts:
            logger.info("Нет данных для синхронизации")
            conn.close()
            return
        
        for workout in workouts:
            user_id, workout_id, workout_number, start_time, end_time, duration = workout
            
            # Получаем профиль пользователя
            profile = get_user_profile(user_id)
            user_weight = get_user_current_weight(user_id) or 0
            
            name = profile['name'] if profile else 'Unknown'
            birthdate = profile['birthdate'] if profile else ''
            
            # Форматируем даты
            start_dt = datetime.fromisoformat(start_time)
            end_dt = datetime.fromisoformat(end_time) if end_time else None
            
            start_formatted = start_dt.strftime('%Y-%m-%dT%H:%M')
            end_formatted = end_dt.strftime('%Y-%m-%dT%H:%M') if end_dt else ''
            weekday = WEEKDAYS_RU[start_dt.weekday()]
            
            # Получаем лист пользователя
            worksheet = get_or_create_user_sheet(client, spreadsheet, user_id)
            
            # Получаем упражнения и подходы
            c.execute('''SELECT e.exercise_name, e.muscle_group, s.weight_kg, s.reps, 
                               s.duration_seconds, s.set_type
                         FROM exercises e
                         JOIN sets s ON e.id = s.exercise_id
                         WHERE e.workout_id = ?
                         ORDER BY e.id, s.id''', (workout_id,))
            
            sets_data = c.fetchall()
            
            for exercise_name, muscle_group, weight, reps, duration_sec, set_type in sets_data:
                cardio_time = ''
                if set_type == 'cardio' and duration_sec:
                    cardio_time = duration_sec // 60
                
                row = [
                    user_id,
                    name,
                    birthdate,
                    user_weight,
                    f"Тренировка {workout_number}",
                    start_formatted,
                    end_formatted,
                    weekday,
                    duration or 0,
                    exercise_name,
                    muscle_group or '',
                    weight if set_type == 'strength' else '',
                    reps if set_type == 'strength' else '',
                    cardio_time
                ]
                worksheet.append_row(row)
        
        conn.close()
        logger.info(f"✅ Синхронизировано {len(workouts)} тренировок в Google Sheets")
        
    except Exception as e:
        logger.error(f"Ошибка синхронизации с Google Sheets: {e}")

# ============ КЛАВИАТУРЫ ============

def get_main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏋️ Начать тренировку", callback_data="start_workout")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="stats"),
         InlineKeyboardButton(text="📅 История", callback_data="history")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile"),
         InlineKeyboardButton(text="❓ Помощь", callback_data="help")]
    ])

def get_workout_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Задать вопрос", callback_data="ask_question")],
        [InlineKeyboardButton(text="🏁 Завершить тренировку", callback_data="end_workout")]
    ])

def get_unfinished_workout_menu(workout_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Продолжить", callback_data=f"continue_workout:{workout_id}")],
        [InlineKeyboardButton(text="🏁 Завершить", callback_data=f"finish_old_workout:{workout_id}")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data=f"cancel_workout:{workout_id}")]
    ])

def get_gender_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👨 Мужской", callback_data="gender_m"),
         InlineKeyboardButton(text="👩 Женский", callback_data="gender_f")]
    ])

def get_reminder_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏁 Завершить", callback_data="end_workout")],
        [InlineKeyboardButton(text="💪 Ещё тренируюсь", callback_data="continue_training")]
    ])

# ============ НАПОМИНАНИЕ О ЗАВЕРШЕНИИ ТРЕНИРОВКИ ============

async def send_workout_reminder(user_id: int, workout_id: str):
    """Отправляет напоминание о завершении тренировки через 2 часа"""
    try:
        await bot.send_message(
            user_id,
            "⏰ Твоя тренировка идёт уже 2 часа!\n\n"
            "Завершить её?",
            reply_markup=get_reminder_keyboard()
        )
        
        # Планируем автозавершение через 15 минут
        run_time = datetime.now(TIMEZONE) + timedelta(minutes=15)
        scheduler.add_job(
            auto_finish_workout,
            DateTrigger(run_date=run_time),
            args=[user_id, workout_id],
            id=f"auto_finish_{workout_id}",
            replace_existing=True
        )
        
        workout_reminders[workout_id] = True
        
    except Exception as e:
        logger.error(f"Error sending reminder: {e}")

async def auto_finish_workout(user_id: int, workout_id: str):
    """Автоматически завершает тренировку"""
    try:
        # Проверяем, не была ли тренировка уже завершена
        unfinished = get_unfinished_workout(user_id)
        if not unfinished or unfinished['workout_id'] != workout_id:
            return
        
        stats = finish_workout_in_db(workout_id, user_id)
        
        if 'error' not in stats:
            tonnage_str = f"{stats['tonnage']:.0f}" if stats['tonnage'] else "0"
            await bot.send_message(
                user_id,
                f"🏁 Тренировка автоматически завершена!\n\n"
                f"⏱ Длительность: {stats['duration']} минут\n"
                f"📊 Выполнено: {stats['exercises_count']} упражнений, {stats['sets_count']} подходов\n"
                f"💪 Общий тоннаж: {tonnage_str} кг",
                reply_markup=get_main_menu()
            )
        
        # Удаляем из отслеживания
        if workout_id in workout_reminders:
            del workout_reminders[workout_id]
            
    except Exception as e:
        logger.error(f"Error auto-finishing workout: {e}")

def schedule_workout_reminder(user_id: int, workout_id: str):
    """Планирует напоминание через 2 часа"""
    run_time = datetime.now(TIMEZONE) + timedelta(hours=2)
    scheduler.add_job(
        send_workout_reminder,
        DateTrigger(run_date=run_time),
        args=[user_id, workout_id],
        id=f"reminder_{workout_id}",
        replace_existing=True
    )

def cancel_workout_reminders(workout_id: str):
    """Отменяет все напоминания для тренировки"""
    try:
        scheduler.remove_job(f"reminder_{workout_id}")
    except:
        pass
    try:
        scheduler.remove_job(f"auto_finish_{workout_id}")
    except:
        pass
    if workout_id in workout_reminders:
        del workout_reminders[workout_id]

# ============ ОБРАБОТЧИКИ ПРОФИЛЯ ============

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    profile = get_user_profile(user_id)
    
    if not profile:
        # Нет профиля — просим заполнить
        await message.answer(
            "👋 Привет! Для начала тренировок тебе необходимо заполнить свой профиль.\n\n"
            "Введи своё имя:"
        )
        await state.set_state(ProfileStates.entering_name)
    else:
        # Профиль есть — показываем главное меню
        await message.answer(
            f"💪 Привет, {profile['name']}! Я твой персональный тренер.\n\n"
            "🏋️ Как записывать тренировки:\n"
            "• Жим лежа 80-10 (упражнение вес-повторы)\n"
            "• Подтягивания 12 (свой вес)\n"
            "• Подтягивания с весом 15-10 (доп. вес)\n"
            "• Бег 5 минут\n\n"
            "Выбери действие:",
            reply_markup=get_main_menu()
        )

@dp.message(F.text.lower().in_(["привет", "хай", "hi", "hello", "здравствуй", "здравствуйте", "прив"]))
async def greeting(message: types.Message, state: FSMContext):
    await cmd_start(message, state)

@dp.message(ProfileStates.entering_name)
async def process_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if len(name) < 2:
        await message.answer("❌ Имя слишком короткое. Введи своё имя:")
        return
    
    await state.update_data(name=name)
    await message.answer(
        f"Отлично, {name}! 👋\n\nВыбери свой пол:",
        reply_markup=get_gender_keyboard()
    )
    await state.set_state(ProfileStates.entering_gender)

@dp.callback_query(F.data.startswith("gender_"))
async def process_gender(callback: types.CallbackQuery, state: FSMContext):
    gender = "М" if callback.data == "gender_m" else "Ж"
    await state.update_data(gender=gender)
    
    await callback.message.answer(
        "📅 Введи дату рождения в формате ДД.ММ.ГГГГ\n"
        "Например: 15.06.1990"
    )
    await state.set_state(ProfileStates.entering_birthdate)
    await callback.answer()

@dp.message(ProfileStates.entering_birthdate)
async def process_birthdate(message: types.Message, state: FSMContext):
    text = message.text.strip()
    
    # Проверяем формат даты
    try:
        birthdate = datetime.strptime(text, '%d.%m.%Y')
        age = calculate_age(text)
        
        if age < 10 or age > 100:
            await message.answer("❌ Проверь дату рождения. Введи в формате ДД.ММ.ГГГГ:")
            return
            
    except ValueError:
        await message.answer("❌ Неверный формат. Введи дату в формате ДД.ММ.ГГГГ\nНапример: 15.06.1990")
        return
    
    await state.update_data(birthdate=text)
    await message.answer(
        "⚖️ Введи свой текущий вес в кг:\n"
        "Например: 75"
    )
    await state.set_state(ProfileStates.entering_weight)

@dp.message(ProfileStates.entering_weight)
async def process_weight(message: types.Message, state: FSMContext):
    try:
        weight = float(message.text.strip().replace(',', '.'))
        if weight < 30 or weight > 300:
            await message.answer("❌ Проверь вес. Введи число от 30 до 300:")
            return
    except ValueError:
        await message.answer("❌ Введи вес числом, например: 75")
        return
    
    data = await state.get_data()
    user_id = message.from_user.id
    
    # Сохраняем профиль
    create_user_profile(user_id, data['name'], data['gender'], data['birthdate'])
    add_user_weight(user_id, weight)
    
    await state.clear()
    
    age = calculate_age(data['birthdate'])
    
    await message.answer(
        f"✅ Профиль создан!\n\n"
        f"👤 Имя: {data['name']}\n"
        f"⚧ Пол: {data['gender']}\n"
        f"🎂 Возраст: {age} лет\n"
        f"⚖️ Вес: {weight} кг\n\n"
        f"💪 Теперь можешь начать тренировку!",
        reply_markup=get_main_menu()
    )

@dp.message(ProfileStates.updating_weight)
async def process_weight_update(message: types.Message, state: FSMContext):
    """Обновление веса после тренировки"""
    try:
        new_weight = float(message.text.strip().replace(',', '.'))
        if new_weight < 30 or new_weight > 300:
            await message.answer("❌ Проверь вес. Введи число от 30 до 300:")
            return
    except ValueError:
        await message.answer("❌ Введи вес числом, например: 75")
        return
    
    user_id = message.from_user.id
    
    # Получаем предыдущие веса
    previous_weight = get_user_current_weight(user_id)
    first_weight = get_user_first_weight(user_id)
    
    # Сохраняем новый вес
    add_user_weight(user_id, new_weight)
    
    # Вычисляем разницу
    month_diff = new_weight - previous_weight if previous_weight else 0
    total_diff = new_weight - first_weight if first_weight else 0
    
    # Формируем сообщение
    result_msg = f"✅ Вес обновлён: {new_weight} кг\n\n"
    
    if month_diff > 0:
        result_msg += f"📈 За месяц: +{month_diff:.1f} кг\n"
    elif month_diff < 0:
        result_msg += f"📉 За месяц: {month_diff:.1f} кг — отлично! 🎉\n"
    else:
        result_msg += f"➡️ За месяц: вес не изменился\n"
    
    if first_weight and first_weight != previous_weight:
        if total_diff > 0:
            result_msg += f"📊 Итого с начала: +{total_diff:.1f} кг"
        elif total_diff < 0:
            result_msg += f"📊 Итого с начала: {total_diff:.1f} кг — поздравляю! 🏆"
        else:
            result_msg += f"📊 Итого с начала: вес не изменился"
    
    await state.clear()
    await message.answer(result_msg, reply_markup=get_main_menu())

# ============ КОМАНДА /profile ============

@dp.message(Command("profile"))
async def cmd_profile(message: types.Message):
    await show_profile(message.from_user.id, message)

@dp.callback_query(F.data == "profile")
async def cb_profile(callback: types.CallbackQuery):
    await show_profile(callback.from_user.id, callback.message)
    await callback.answer()

async def show_profile(user_id: int, message: types.Message):
    profile = get_user_profile(user_id)
    
    if not profile:
        await message.answer("❌ Профиль не найден. Напиши /start чтобы создать.")
        return
    
    weight = get_user_current_weight(user_id)
    first_weight = get_user_first_weight(user_id)
    age = calculate_age(profile['birthdate'])
    
    text = f"👤 Твой профиль:\n\n"
    text += f"📝 Имя: {profile['name']}\n"
    text += f"⚧ Пол: {profile['gender']}\n"
    text += f"🎂 Возраст: {age} лет\n"
    text += f"⚖️ Текущий вес: {weight} кг\n"
    
    if first_weight and first_weight != weight:
        diff = weight - first_weight
        text += f"📊 Изменение веса: {'+' if diff > 0 else ''}{diff:.1f} кг\n"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚖️ Обновить вес", callback_data="update_weight")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")]
    ])
    
    await message.answer(text, reply_markup=keyboard)

@dp.callback_query(F.data == "update_weight")
async def cb_update_weight(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("⚖️ Введи свой текущий вес в кг:")
    await state.set_state(ProfileStates.updating_weight)
    await callback.answer()

@dp.callback_query(F.data == "back_to_menu")
async def cb_back_to_menu(callback: types.CallbackQuery):
    await callback.message.answer("Выбери действие:", reply_markup=get_main_menu())
    await callback.answer()

# ============ НАЧАЛО ТРЕНИРОВКИ ============

@dp.callback_query(F.data == "start_workout")
async def cb_start_workout(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    
    # Проверяем профиль
    profile = get_user_profile(user_id)
    if not profile:
        await callback.message.answer(
            "❌ Сначала создай профиль!\n\nВведи своё имя:"
        )
        await state.set_state(ProfileStates.entering_name)
        await callback.answer()
        return
    
    # Проверка FSM состояния
    current_state = await state.get_state()
    if current_state == WorkoutStates.entering_sets.state:
        await callback.message.answer(
            "⚠️ У тебя уже идёт тренировка!\n\n"
            "Продолжай вводить упражнения или заверши её:",
            reply_markup=get_workout_menu()
        )
        await callback.answer()
        return
    
    # Проверка незавершённой тренировки в БД
    unfinished = get_unfinished_workout(user_id)
    if unfinished:
        start_time = datetime.fromisoformat(unfinished['start_time'])
        exercises_count = get_workout_exercises_count(unfinished['workout_id'])
        
        await callback.message.answer(
            f"⚠️ У тебя есть незавершённая тренировка!\n\n"
            f"📅 Начата: {start_time.strftime('%d.%m.%Y в %H:%M')}\n"
            f"📝 Упражнений записано: {exercises_count}\n\n"
            f"Что сделать с ней?",
            reply_markup=get_unfinished_workout_menu(unfinished['workout_id'])
        )
        await callback.answer()
        return
    
    # Начинаем новую тренировку
    await start_new_workout(user_id, callback.message, state)
    await callback.answer()

@dp.message(Command("start_workout"))
async def cmd_start_workout(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    
    profile = get_user_profile(user_id)
    if not profile:
        await message.answer("❌ Сначала создай профиль!\n\nВведи своё имя:")
        await state.set_state(ProfileStates.entering_name)
        return
    
    current_state = await state.get_state()
    if current_state == WorkoutStates.entering_sets.state:
        await message.answer(
            "⚠️ У тебя уже идёт тренировка!",
            reply_markup=get_workout_menu()
        )
        return
    
    unfinished = get_unfinished_workout(user_id)
    if unfinished:
        start_time = datetime.fromisoformat(unfinished['start_time'])
        exercises_count = get_workout_exercises_count(unfinished['workout_id'])
        
        await message.answer(
            f"⚠️ У тебя есть незавершённая тренировка!\n\n"
            f"📅 Начата: {start_time.strftime('%d.%m.%Y в %H:%M')}\n"
            f"📝 Упражнений: {exercises_count}\n\n"
            f"Что сделать?",
            reply_markup=get_unfinished_workout_menu(unfinished['workout_id'])
        )
        return
    
    await start_new_workout(user_id, message, state)

async def start_new_workout(user_id: int, message: types.Message, state: FSMContext):
    """Создаёт новую тренировку"""
    workout_id = f"{user_id}_{datetime.now().timestamp()}"
    start_time = datetime.now(TIMEZONE).isoformat()
    workout_number = get_next_workout_number(user_id)
    
    # Записываем в БД
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('''INSERT INTO workouts (user_id, workout_id, workout_number, start_time, end_time, duration_minutes)
                 VALUES (?, ?, ?, ?, NULL, NULL)''',
              (user_id, workout_id, workout_number, start_time))
    conn.commit()
    conn.close()
    
    # Сохраняем в FSM
    await state.update_data(
        workout_id=workout_id,
        start_time=start_time,
        current_exercise=None,
        current_exercise_id=None,
        set_count=0
    )
    await state.set_state(WorkoutStates.entering_sets)
    
    # Планируем напоминание через 2 часа
    schedule_workout_reminder(user_id, workout_id)
    
    current_time = datetime.now(TIMEZONE).strftime('%H:%M')
    
    await message.answer(
        f"🏋️ Тренировка #{workout_number} начата!\n"
        f"⏱ Время: {current_time}\n\n"
        "Вводи упражнения:\n"
        "• Жим лежа 80-10\n"
        "• Подтягивания 12 (свой вес)\n"
        "• Бег 5 минут",
        reply_markup=get_workout_menu()
    )
# ============ ОБРАБОТКА НЕЗАВЕРШЁННОЙ ТРЕНИРОВКИ ============

@dp.callback_query(F.data.startswith("continue_workout:"))
async def cb_continue_workout(callback: types.CallbackQuery, state: FSMContext):
    """Продолжить незавершённую тренировку"""
    workout_id = callback.data.split(":")[1]
    
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('SELECT start_time, workout_number FROM workouts WHERE workout_id = ?', (workout_id,))
    result = c.fetchone()
    
    if not result:
        await callback.message.answer("❌ Тренировка не найдена")
        await callback.answer()
        conn.close()
        return
    
    start_time, workout_number = result
    
    # Получаем последнее упражнение
    c.execute('''SELECT id, exercise_name FROM exercises 
                 WHERE workout_id = ? ORDER BY id DESC LIMIT 1''', (workout_id,))
    last_exercise = c.fetchone()
    
    set_count = 0
    current_exercise_id = None
    current_exercise = None
    
    if last_exercise:
        current_exercise_id = last_exercise[0]
        current_exercise = last_exercise[1]
        c.execute('SELECT COUNT(*) FROM sets WHERE exercise_id = ?', (current_exercise_id,))
        set_count = c.fetchone()[0]
    
    conn.close()
    
    await state.update_data(
        workout_id=workout_id,
        start_time=start_time,
        current_exercise=current_exercise,
        current_exercise_id=current_exercise_id,
        set_count=set_count
    )
    await state.set_state(WorkoutStates.entering_sets)
    
    # Перепланируем напоминание
    schedule_workout_reminder(callback.from_user.id, workout_id)
    
    exercises_count = get_workout_exercises_count(workout_id)
    
    await callback.message.answer(
        f"🔄 Продолжаем тренировку #{workout_number}!\n\n"
        f"📝 Записано упражнений: {exercises_count}\n"
        f"{'📍 Последнее: ' + current_exercise if current_exercise else ''}\n\n"
        f"Вводи следующее упражнение:",
        reply_markup=get_workout_menu()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("finish_old_workout:"))
async def cb_finish_old_workout(callback: types.CallbackQuery, state: FSMContext):
    """Завершить незавершённую тренировку"""
    workout_id = callback.data.split(":")[1]
    
    cancel_workout_reminders(workout_id)
    stats = finish_workout_in_db(workout_id, callback.from_user.id)
    
    if 'error' in stats:
        await callback.message.answer(f"❌ {stats['error']}")
        await callback.answer()
        return
    
    tonnage_str = f"{stats['tonnage']:.0f}" if stats['tonnage'] else "0"
    
    await callback.message.answer(
        f"🏁 Тренировка завершена!\n"
        f"⏱ Длительность: {stats['duration']} минут\n"
        f"📊 Выполнено: {stats['exercises_count']} упражнений, {stats['sets_count']} подходов\n"
        f"💪 Общий тоннаж: {tonnage_str} кг\n\n"
        f"Теперь можешь начать новую!",
        reply_markup=get_main_menu()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("cancel_workout:"))
async def cb_cancel_workout(callback: types.CallbackQuery, state: FSMContext):
    """Отменить тренировку"""
    workout_id = callback.data.split(":")[1]
    
    cancel_workout_reminders(workout_id)
    cancel_workout_in_db(workout_id)
    await state.clear()
    
    await callback.message.answer(
        "❌ Тренировка отменена и удалена.\n\nМожешь начать новую!",
        reply_markup=get_main_menu()
    )
    await callback.answer()

@dp.callback_query(F.data == "continue_training")
async def cb_continue_training(callback: types.CallbackQuery):
    """Пользователь нажал 'Ещё тренируюсь' на напоминании"""
    # Отменяем автозавершение
    data = await callback.message.bot.get_chat(callback.from_user.id)
    
    # Ищем активную тренировку
    unfinished = get_unfinished_workout(callback.from_user.id)
    if unfinished:
        workout_id = unfinished['workout_id']
        # Отменяем автозавершение
        try:
            scheduler.remove_job(f"auto_finish_{workout_id}")
        except:
            pass
        
        # Планируем новое напоминание через 1 час
        run_time = datetime.now(TIMEZONE) + timedelta(hours=1)
        scheduler.add_job(
            send_workout_reminder,
            DateTrigger(run_date=run_time),
            args=[callback.from_user.id, workout_id],
            id=f"reminder_{workout_id}",
            replace_existing=True
        )
    
    await callback.message.answer(
        "💪 Отлично! Продолжай тренировку.\n"
        "Напомню через час.",
        reply_markup=get_workout_menu()
    )
    await callback.answer()

# ============ КОМАНДА /cancel ============

@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    """Отменить текущую тренировку"""
    current_state = await state.get_state()
    data = await state.get_data()
    
    if current_state == WorkoutStates.entering_sets.state and 'workout_id' in data:
        workout_id = data['workout_id']
        cancel_workout_reminders(workout_id)
        cancel_workout_in_db(workout_id)
        await state.clear()
        await message.answer("❌ Тренировка отменена.", reply_markup=get_main_menu())
        return
    
    # Проверяем БД
    unfinished = get_unfinished_workout(message.from_user.id)
    if unfinished:
        cancel_workout_reminders(unfinished['workout_id'])
        cancel_workout_in_db(unfinished['workout_id'])
        await message.answer("❌ Незавершённая тренировка отменена.", reply_markup=get_main_menu())
    else:
        await message.answer("❌ Нет активной тренировки для отмены.")

# ============ ВВОД УПРАЖНЕНИЙ ============

@dp.callback_query(F.data == "ask_question")
async def ask_question_callback(callback: types.CallbackQuery):
    await callback.message.answer("💡 Задай свой вопрос:")
    await callback.answer()

@dp.message(WorkoutStates.entering_sets)
async def process_workout_entry(message: types.Message, state: FSMContext):
    text = message.text.strip()
    
    # Проверяем не вопрос ли это
    question_words = ['как', 'что', 'чем', 'почему', 'когда', 'стоит', 'можно', 'нужно', 'заменить', 'посоветуй', '?']
    if any(word in text.lower() for word in question_words) and len(text.split()) > 2:
        await message.answer("🤔 Думаю...")
        answer = await ask_gigachat(message.from_user.id, text)
        await message.answer(answer, reply_markup=get_workout_menu())
        return
    
    # Получаем вес пользователя для упражнений с собственным весом
    user_weight = get_user_current_weight(message.from_user.id) or 0
    
    exercise_name, weight, reps, duration, set_type = parse_workout_input(text, user_weight)
    
    if set_type == 'unknown':
        await message.answer(
            "❌ Не понял формат. Примеры:\n"
            "• Жим лежа 80-10 (вес-повторы)\n"
            "• Подтягивания 12 (свой вес)\n"
            "• Подтягивания с весом 15-10\n"
            "• Бег 5 минут\n"
            "• 80-10 (продолжить упражнение)"
        )
        return
    
    data = await state.get_data()
    workout_id = data['workout_id']
    current_exercise_id = data.get('current_exercise_id')
    current_exercise = data.get('current_exercise')
    set_count = data.get('set_count', 0)
    
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    
    # Если указано название упражнения — создаём новое
    if exercise_name:
        # Определяем группу мышц
        muscle_group = await get_muscle_group(exercise_name)
        
        c.execute('''INSERT INTO exercises (workout_id, exercise_name, muscle_group, timestamp)
                     VALUES (?, ?, ?, ?)''',
                  (workout_id, exercise_name, muscle_group, datetime.now(TIMEZONE).isoformat()))
        current_exercise_id = c.lastrowid
        current_exercise = exercise_name
        set_count = 0
        
        await state.update_data(
            current_exercise=exercise_name,
            current_exercise_id=current_exercise_id,
            set_count=0
        )
    elif current_exercise_id is None:
        await message.answer("❌ Сначала укажи упражнение: Жим лежа 80-10")
        conn.close()
        return
    
    # Сохраняем подход
    c.execute('''INSERT INTO sets (exercise_id, weight_kg, reps, duration_seconds, set_type, timestamp)
                 VALUES (?, ?, ?, ?, ?, ?)''',
              (current_exercise_id, weight, reps, duration, set_type, datetime.now(TIMEZONE).isoformat()))
    conn.commit()
    conn.close()
    
    set_count += 1
    await state.update_data(set_count=set_count)
    
    # Формируем ответ
    if set_type == 'strength':
        response = f"✅ {current_exercise} — Подход {set_count}: {weight} кг × {reps} раз"
    elif set_type == 'cardio':
        mins = duration // 60
        response = f"✅ {current_exercise}: {mins} мин"
    elif set_type == 'static':
        response = f"✅ {current_exercise}: {duration} сек"
    else:
        response = f"✅ {current_exercise} записано"
    
    await message.answer(response, reply_markup=get_workout_menu())

# ============ ЗАВЕРШЕНИЕ ТРЕНИРОВКИ ============

@dp.callback_query(F.data == "end_workout")
async def end_workout(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    
    if 'workout_id' not in data:
        await callback.message.answer("❌ Тренировка не начата.")
        await callback.answer()
        return
    
    workout_id = data['workout_id']
    user_id = callback.from_user.id
    
    # Отменяем напоминания
    cancel_workout_reminders(workout_id)
    
    # Завершаем тренировку
    stats = finish_workout_in_db(workout_id, user_id)
    
    if 'error' in stats:
        await callback.message.answer(f"❌ {stats['error']}")
        await callback.answer()
        return
    
    await state.clear()
    
    tonnage_str = f"{stats['tonnage']:.0f}" if stats['tonnage'] else "0"
    
    result_msg = (
        f"🏁 Тренировка завершена!\n\n"
        f"⏱ Длительность: {stats['duration']} мин\n"
        f"📊 Упражнений: {stats['exercises_count']}, подходов: {stats['sets_count']}\n"
        f"💪 Тоннаж: {tonnage_str} кг"
    )
    
    # Проверяем, нужно ли обновить вес
    if needs_weight_update(user_id):
        await callback.message.answer(result_msg)
        await callback.message.answer(
            "⚖️ Прошло больше месяца с последнего взвешивания.\n"
            "Введи свой текущий вес в кг:"
        )
        await state.set_state(ProfileStates.updating_weight)
    else:
        await callback.message.answer(result_msg, reply_markup=get_main_menu())
    
    await callback.answer()

# ============ СТАТИСТИКА ============

@dp.callback_query(F.data == "stats")
async def cb_stats(callback: types.CallbackQuery):
    await show_stats(callback.from_user.id, callback.message)
    await callback.answer()

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    await show_stats(message.from_user.id, message)

async def show_stats(user_id: int, message: types.Message):
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    
    # Общая статистика
    c.execute('''SELECT COUNT(*), SUM(duration_minutes)
                 FROM workouts WHERE user_id = ? AND end_time IS NOT NULL''', (user_id,))
    total_workouts, total_minutes = c.fetchone()
    
    # Рекорды
    c.execute('''SELECT e.exercise_name, MAX(s.weight_kg)
                 FROM workouts w
                 JOIN exercises e ON w.workout_id = e.workout_id
                 JOIN sets s ON e.id = s.exercise_id
                 WHERE w.user_id = ? AND s.set_type = 'strength' AND s.weight_kg > 0
                 GROUP BY e.exercise_name
                 ORDER BY MAX(s.weight_kg) DESC''', (user_id,))
    records = c.fetchall()
    
    conn.close()
    
    stats = f"📊 Статистика:\n\n"
    stats += f"🏋️ Тренировок: {total_workouts or 0}\n"
    stats += f"⏱ Общее время: {total_minutes or 0} мин\n\n"
    
    if records:
        stats += "🏆 Рекорды:\n"
        for name, weight in records[:10]:
            stats += f"• {name}: {weight} кг\n"
    else:
        stats += "Пока нет записей"
    
    await message.answer(stats)

# ============ ИСТОРИЯ ============

@dp.callback_query(F.data == "history")
async def cb_history(callback: types.CallbackQuery):
    await show_history(callback.from_user.id, callback.message)
    await callback.answer()

@dp.message(Command("history"))
async def cmd_history(message: types.Message):
    await show_history(message.from_user.id, message)

async def show_history(user_id: int, message: types.Message):
    today = datetime.now(TIMEZONE).date()
    start_of_week = today - timedelta(days=today.weekday())
    end_of_week = start_of_week + timedelta(days=6)
    
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    
    c.execute('''SELECT w.workout_id, w.workout_number, w.start_time, w.duration_minutes
                 FROM workouts w
                 WHERE w.user_id = ? AND date(w.start_time) >= ? AND date(w.start_time) <= ?
                   AND w.end_time IS NOT NULL
                 ORDER BY w.start_time ASC''', 
              (user_id, start_of_week.isoformat(), end_of_week.isoformat()))
    
    workouts = c.fetchall()
    
    if not workouts:
        await message.answer(
            f"📅 Неделя ({start_of_week.strftime('%d.%m')} - {end_of_week.strftime('%d.%m')}):\n\n"
            "Пока нет тренировок.\nНачни с /start_workout!"
        )
        conn.close()
        return
    
    history = f"📅 Неделя ({start_of_week.strftime('%d.%m')} - {end_of_week.strftime('%d.%m')}):\n\n"
    
    for workout_id, workout_num, start_time, duration in workouts:
        dt = datetime.fromisoformat(start_time)
        weekday = WEEKDAYS_RU[dt.weekday()]
        
        history += f"🏋️ Тренировка #{workout_num} — {weekday} {dt.strftime('%d.%m')}\n"
        
        c.execute('''SELECT e.exercise_name, e.muscle_group, s.weight_kg, s.reps, s.duration_seconds, s.set_type
                     FROM exercises e
                     JOIN sets s ON e.id = s.exercise_id
                     WHERE e.workout_id = ?
                     ORDER BY e.id, s.id''', (workout_id,))
        
        exercises = c.fetchall()
        
        ex_groups = {}
        for ex_name, muscle, weight, reps, dur, stype in exercises:
            if ex_name not in ex_groups:
                ex_groups[ex_name] = {'muscle': muscle, 'type': stype, 'weights': [], 'reps': [], 'duration': dur}
            if stype == 'strength':
                ex_groups[ex_name]['weights'].append(int(weight) if weight else 0)
                ex_groups[ex_name]['reps'].append(reps or 0)
        
        for ex_name, data in ex_groups.items():
            if data['type'] == 'strength':
                weights_str = '→'.join(map(str, data['weights']))
                history += f"  • {ex_name} ({data['muscle']}): {weights_str} кг\n"
            elif data['type'] == 'cardio':
                mins = data['duration'] // 60 if data['duration'] else 0
                history += f"  • {ex_name}: {mins} мин\n"
            elif data['type'] == 'static':
                history += f"  • {ex_name}: {data['duration']} сек\n"
        
        history += "\n"
    
    conn.close()
    await message.answer(history)

# ============ ПОМОЩЬ ============

@dp.callback_query(F.data == "help")
async def cb_help(callback: types.CallbackQuery):
    await show_help(callback.message)
    await callback.answer()

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await show_help(message)

async def show_help(message: types.Message):
    help_text = """❓ Как пользоваться ботом:

🏋️ ЗАПИСЬ ТРЕНИРОВКИ
• Жим лежа 80-10 — вес × повторы
• 80-8 — следующий подход
• Подтягивания 12 — свой вес × повторы
• Подтягивания с весом 15-10 — свой вес + 15 кг
• Бег 5 минут — кардио
• Планка 60 секунд — статика

📊 КОМАНДЫ
/start_workout — начать тренировку
/stats — статистика и рекорды
/history — тренировки за неделю
/profile — твой профиль
/delete — удалить последний подход
/cancel — отменить тренировку
/feedback — написать разработчику

⏰ НАПОМИНАНИЯ
Через 2 часа бот напомнит завершить тренировку.
Если не ответишь — завершит автоматически.

⚖️ ВЕС
Бот просит обновить вес раз в месяц и показывает прогресс."""
    
    await message.answer(help_text)

# ============ УДАЛЕНИЕ ============

@dp.message(Command("delete"))
async def cmd_delete(message: types.Message, state: FSMContext):
    data = await state.get_data()
    workout_id = data.get('workout_id')
    
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    
    if workout_id:
        # Удаляем из текущей тренировки
        c.execute('''SELECT s.id, e.exercise_name, s.weight_kg, s.reps
                     FROM sets s
                     JOIN exercises e ON s.exercise_id = e.id
                     WHERE e.workout_id = ?
                     ORDER BY s.id DESC LIMIT 1''', (workout_id,))
    else:
        # Удаляем последний подход пользователя
        c.execute('''SELECT s.id, e.exercise_name, s.weight_kg, s.reps
                     FROM sets s
                     JOIN exercises e ON s.exercise_id = e.id
                     JOIN workouts w ON e.workout_id = w.workout_id
                     WHERE w.user_id = ?
                     ORDER BY s.timestamp DESC LIMIT 1''', (message.from_user.id,))
    
    result = c.fetchone()
    
    if result:
        set_id, ex_name, weight, reps = result
        c.execute('DELETE FROM sets WHERE id = ?', (set_id,))
        conn.commit()
        
        # Обновляем счётчик
        set_count = data.get('set_count', 1) - 1
        if set_count < 0:
            set_count = 0
        await state.update_data(set_count=set_count)
        
        await message.answer(f"🗑 Удалено: {ex_name} {weight}кг × {reps}")
    else:
        await message.answer("❌ Нет записей для удаления")
    
    conn.close()

# ============ FEEDBACK ============

@dp.message(Command("feedback"))
async def cmd_feedback(message: types.Message):
    text = message.text.replace('/feedback', '').strip()
    
    if not text:
        await message.answer("📝 Напиши: /feedback твоё сообщение")
        return
    
    try:
        await bot.send_message(
            ADMIN_ID,
            f"📢 Feedback от {message.from_user.id}:\n"
            f"Имя: {message.from_user.full_name}\n"
            f"@{message.from_user.username}\n\n{text}"
        )
        await message.answer("✅ Сообщение отправлено!")
    except Exception as e:
        logger.error(f"Feedback error: {e}")
        await message.answer("❌ Ошибка отправки")

# ============ АДМИНСКИЕ КОМАНДЫ ============

@dp.message(Command("export"))
async def cmd_export(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Недоступно")
        return
    
    try:
        file = FSInputFile('workouts.db')
        await message.answer_document(file, caption="📦 База данных")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("sync"))
async def cmd_sync(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Недоступно")
        return
    
    await message.answer("🔄 Синхронизация...")
    await sync_to_google_sheets()
    await message.answer("✅ Готово!")

# ============ ОБРАБОТКА ОСТАЛЬНЫХ СООБЩЕНИЙ ============

@dp.message()
async def handle_any_message(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    
    if current_state:
        return
    
    # Проверяем профиль
    profile = get_user_profile(message.from_user.id)
    if not profile:
        await message.answer("👋 Привет! Для начала создай профиль.\n\nВведи своё имя:")
        await state.set_state(ProfileStates.entering_name)
        return
    
    question_words = ['как', 'что', 'чем', 'почему', 'когда', 'стоит', 'можно', 'нужно', 'заменить', 'посоветуй', '?']
    
    if any(word in message.text.lower() for word in question_words):
        await message.answer("🤔 Думаю...")
        answer = await ask_gigachat(message.from_user.id, message.text)
        await message.answer(answer)
    else:
        await message.answer(
            "Не понял. Напиши 'Привет' или /help",
            reply_markup=get_main_menu()
        )

# ============ MAIN ============

async def main():
    logger.info("🚀 Бот запускается...")
    
    # Запускаем планировщик
    scheduler.add_job(
        sync_to_google_sheets,
        CronTrigger(hour=23, minute=59, timezone=TIMEZONE),
        id='daily_sync',
        replace_existing=True
    )
    scheduler.start()
    logger.info("✅ Планировщик запущен (синхронизация в 23:59 МСК)")
    
    # Запускаем бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
