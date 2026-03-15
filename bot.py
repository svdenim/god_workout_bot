import asyncio
import logging
import os
import re
import sqlite3
import base64
import uuid
import json
from datetime import datetime, timedelta
from typing import Optional, Tuple

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
ADMIN_ID = 295220429  # Твой ID
GOOGLE_SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID")
GOOGLE_SHEETS_CREDENTIALS = os.getenv("GOOGLE_SHEETS_CREDENTIALS")

# Инициализация бота
bot = Bot(token=TELEGRAM_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
scheduler = AsyncIOScheduler(timezone=pytz.timezone('Europe/Moscow'))

# FSM состояния
class WorkoutStates(StatesGroup):
    in_workout = State()
    entering_sets = State()

# Инициализация БД
def init_db():
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS workouts
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  workout_id TEXT,
                  start_time TEXT,
                  end_time TEXT,
                  duration_minutes INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS exercises
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  workout_id TEXT,
                  exercise_name TEXT,
                  timestamp TEXT)''')
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

# ============ ПРОВЕРКА НЕЗАВЕРШЁННЫХ ТРЕНИРОВОК ============

def get_unfinished_workout(user_id: int) -> Optional[dict]:
    """Проверяет есть ли незавершённая тренировка (end_time IS NULL)"""
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    
    c.execute('''SELECT workout_id, start_time 
                 FROM workouts 
                 WHERE user_id = ? AND end_time IS NULL
                 ORDER BY start_time DESC 
                 LIMIT 1''', (user_id,))
    
    result = c.fetchone()
    conn.close()
    
    if result:
        return {'workout_id': result[0], 'start_time': result[1]}
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
    """Завершает тренировку в БД и возвращает статистику"""
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    
    # Получаем время начала
    c.execute('SELECT start_time FROM workouts WHERE workout_id = ?', (workout_id,))
    result = c.fetchone()
    
    if not result:
        conn.close()
        return {'error': 'Тренировка не найдена'}
    
    start_time = datetime.fromisoformat(result[0])
    end_time = datetime.now()
    duration = int((end_time - start_time).total_seconds() / 60)
    
    # Обновляем тренировку
    c.execute('''UPDATE workouts 
                 SET end_time = ?, duration_minutes = ?
                 WHERE workout_id = ?''',
              (end_time.isoformat(), duration, workout_id))
    
    # Получаем статистику
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
    """Отменяет (удаляет) незавершённую тренировку"""
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    
    # Удаляем подходы
    c.execute('''DELETE FROM sets WHERE exercise_id IN 
                 (SELECT id FROM exercises WHERE workout_id = ?)''', (workout_id,))
    
    # Удаляем упражнения
    c.execute('DELETE FROM exercises WHERE workout_id = ?', (workout_id,))
    
    # Удаляем тренировку
    c.execute('DELETE FROM workouts WHERE workout_id = ?', (workout_id,))
    
    conn.commit()
    conn.close()
    return True

# ============ GOOGLE SHEETS ============

def get_google_sheets_client():
    try:
        creds_dict = json.loads(GOOGLE_SHEETS_CREDENTIALS)
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        return client
    except Exception as e:
        logger.error(f"Google Sheets auth error: {e}")
        return None

async def sync_to_google_sheets():
    """Синхронизация данных в Google Sheets (вызывается в 23:59)"""
    try:
        client = get_google_sheets_client()
        if not client:
            logger.error("Не удалось подключиться к Google Sheets")
            return
        
        sheet = client.open_by_key(GOOGLE_SPREADSHEET_ID)
        
        # Получаем данные за сегодня
        today = datetime.now().date()
        conn = sqlite3.connect('workouts.db')
        c = conn.cursor()
        
        c.execute('''SELECT w.user_id, w.start_time, w.end_time, w.duration_minutes,
                            e.exercise_name, s.weight_kg, s.reps, s.duration_seconds, s.set_type
                     FROM workouts w
                     JOIN exercises e ON w.workout_id = e.workout_id
                     JOIN sets s ON e.id = s.exercise_id
                     WHERE date(w.start_time) = ? AND w.end_time IS NOT NULL''', (today.isoformat(),))
        
        rows = c.fetchall()
        conn.close()
        
        if not rows:
            logger.info("Нет данных для синхронизации")
            return
        
        # Worksheet "Data"
        try:
            worksheet = sheet.worksheet("Data")
        except:
            worksheet = sheet.add_worksheet(title="Data", rows=1000, cols=10)
            worksheet.append_row(["User ID", "Дата", "Время начала", "Время конца", 
                                  "Длительность (мин)", "Упражнение", "Вес (кг)", 
                                  "Повторы", "Длительность (сек)", "Тип"])
        
        # Добавляем строки
        for row in rows:
            worksheet.append_row(list(row))
        
        logger.info(f"✅ Синхронизировано {len(rows)} записей в Google Sheets")
        
    except Exception as e:
        logger.error(f"Ошибка синхронизации с Google Sheets: {e}")

# ============ ПАРСЕР УПРАЖНЕНИЙ ============

def parse_workout_input(text: str) -> Tuple[Optional[str], Optional[float], Optional[int], Optional[int], str]:
    text = text.strip()
    
    # Паттерн 1: Упражнение + вес-повторы (Жим лежа 80-10)
    pattern1 = r'^(.+?)\s+(\d+(?:\.\d+)?)\s*[-xх×*]\s*(\d+)$'
    match = re.match(pattern1, text, re.IGNORECASE)
    if match:
        return (match.group(1).strip(), float(match.group(2)), int(match.group(3)), None, 'strength')
    
    # Паттерн 2: Только вес-повторы (80-10)
    pattern2 = r'^(\d+(?:\.\d+)?)\s*[-xх×*]\s*(\d+)$'
    match = re.match(pattern2, text)
    if match:
        return (None, float(match.group(1)), int(match.group(2)), None, 'strength')
    
    # Паттерн 3: Упражнение + минуты (Бег 5 минут)
    pattern3 = r'^(.+?)\s+(\d+)\s*(мин|минут|минуты|min|м).*$'
    match = re.match(pattern3, text, re.IGNORECASE)
    if match:
        return (match.group(1).strip(), None, None, int(match.group(2)) * 60, 'cardio')
    
    # Паттерн 4: Упражнение + секунды (Планка 60 секунд)
    pattern4 = r'^(.+?)\s+(\d+)\s*(сек|секунд|секунды|sec|с).*$'
    match = re.match(pattern4, text, re.IGNORECASE)
    if match:
        return (match.group(1).strip(), None, None, int(match.group(2)), 'static')
    
    # Паттерн 5: Только название упражнения
    if not any(char.isdigit() for char in text):
        return (text, None, None, None, 'unknown')
    
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
    try:
        conn = sqlite3.connect('workouts.db')
        c = conn.cursor()
        
        c.execute('''SELECT w.start_time, e.exercise_name, s.weight_kg, s.reps, s.duration_seconds, s.set_type
                     FROM workouts w
                     JOIN exercises e ON w.workout_id = e.workout_id
                     JOIN sets s ON e.id = s.exercise_id
                     WHERE w.user_id = ?
                     ORDER BY w.start_time DESC
                     LIMIT 100''', (user_id,))
        
        history = c.fetchall()
        conn.close()
        
        context = "История тренировок:\n"
        for row in history[:20]:
            date, exercise, weight, reps, duration, set_type = row
            if set_type == 'strength':
                context += f"{date}: {exercise} - {weight}кг x {reps} раз\n"
            elif set_type in ['cardio', 'static']:
                context += f"{date}: {exercise} - {duration} секунд\n"
        
        token = await get_gigachat_token()
        if not token:
            return "Ошибка подключения к GigaChat."
        
        url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'Authorization': f'Bearer {token}'
        }
        
        payload = {
            "model": "GigaChat",
            "messages": [
                {"role": "system", "content": f"Ты персональный тренер. {context}"},
                {"role": "user", "content": question}
            ],
            "temperature": 0.7,
            "max_tokens": 1024
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

# ============ КЛАВИАТУРЫ ============

def get_main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏋️ Начать тренировку", callback_data="start_workout")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="stats"),
         InlineKeyboardButton(text="📅 История", callback_data="history")],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="help")]
    ])

def get_workout_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Задать вопрос", callback_data="ask_question")],
        [InlineKeyboardButton(text="🏁 Завершить тренировку", callback_data="end_workout")]
    ])

def get_unfinished_workout_menu(workout_id: str):
    """Меню для незавершённой тренировки"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Продолжить", callback_data=f"continue_workout:{workout_id}")],
        [InlineKeyboardButton(text="🏁 Завершить", callback_data=f"finish_old_workout:{workout_id}")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data=f"cancel_workout:{workout_id}")]
    ])

# ============ ХЭНДЛЕРЫ ============

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "💪 Привет! Я твой персональный тренер.\n\n"
        "🏋️ Как записывать тренировки:\n"
        "• Жим лежа 80-10 (упражнение вес-повторы)\n"
        "• Бег 5 минут\n"
        "• Планка 60 секунд\n\n"
        "Выбери действие:",
        reply_markup=get_main_menu()
    )

@dp.message(F.text.lower().in_(["привет", "хай", "hi", "hello", "здравствуй", "здравствуйте", "прив"]))
async def greeting(message: types.Message):
    await message.answer(
        "💪 Привет! Я твой персональный тренер.\n\n"
        "🏋️ Как записывать тренировки:\n"
        "• Жим лежа 80-10 (упражнение вес-повторы)\n"
        "• Бег 5 минут\n"
        "• Планка 60 секунд\n\n"
        "Выбери действие:",
        reply_markup=get_main_menu()
    )

# ============ НАЧАЛО ТРЕНИРОВКИ С ПРОВЕРКАМИ ============

@dp.callback_query(F.data == "start_workout")
async def cb_start_workout(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    
    # Проверка 1: Уже идёт тренировка в FSM?
    current_state = await state.get_state()
    if current_state == WorkoutStates.entering_sets.state:
        await callback.message.answer(
            "⚠️ У тебя уже идёт тренировка!\n\n"
            "Продолжай вводить упражнения или заверши её:",
            reply_markup=get_workout_menu()
        )
        await callback.answer()
        return
    
    # Проверка 2: Есть незавершённая тренировка в БД?
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
    
    # Всё чисто — начинаем новую тренировку
    await start_new_workout(callback.from_user.id, callback.message, state)
    await callback.answer()

@dp.message(Command("start_workout"))
async def cmd_start_workout(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    
    # Проверка 1: Уже идёт тренировка в FSM?
    current_state = await state.get_state()
    if current_state == WorkoutStates.entering_sets.state:
        await message.answer(
            "⚠️ У тебя уже идёт тренировка!\n\n"
            "Продолжай вводить упражнения или заверши её:",
            reply_markup=get_workout_menu()
        )
        return
    
    # Проверка 2: Есть незавершённая тренировка в БД?
    unfinished = get_unfinished_workout(user_id)
    if unfinished:
        start_time = datetime.fromisoformat(unfinished['start_time'])
        exercises_count = get_workout_exercises_count(unfinished['workout_id'])
        
        await message.answer(
            f"⚠️ У тебя есть незавершённая тренировка!\n\n"
            f"📅 Начата: {start_time.strftime('%d.%m.%Y в %H:%M')}\n"
            f"📝 Упражнений записано: {exercises_count}\n\n"
            f"Что сделать с ней?",
            reply_markup=get_unfinished_workout_menu(unfinished['workout_id'])
        )
        return
    
    # Всё чисто — начинаем новую тренировку
    await start_new_workout(message.from_user.id, message, state)

async def start_new_workout(user_id: int, message: types.Message, state: FSMContext):
    """Создаёт новую тренировку"""
    workout_id = f"{user_id}_{datetime.now().timestamp()}"
    start_time = datetime.now().isoformat()
    
    # Сразу записываем в БД (с end_time = NULL)
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('''INSERT INTO workouts (user_id, workout_id, start_time, end_time, duration_minutes)
                 VALUES (?, ?, ?, NULL, NULL)''',
              (user_id, workout_id, start_time))
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
    
    await message.answer(
        "🏋️ Тренировка начата!\n"
        f"⏱ Время: {datetime.now().strftime('%H:%M')}\n\n"
        "Вводи упражнения:\n"
        "• Жим лежа 80-10\n"
        "• Бег 5 минут\n"
        "• 80-10 (следующий подход)",
        reply_markup=get_workout_menu()
    )

# ============ ОБРАБОТКА НЕЗАВЕРШЁННОЙ ТРЕНИРОВКИ ============

@dp.callback_query(F.data.startswith("continue_workout:"))
async def cb_continue_workout(callback: types.CallbackQuery, state: FSMContext):
    """Продолжить незавершённую тренировку"""
    workout_id = callback.data.split(":")[1]
    
    # Получаем данные тренировки
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('SELECT start_time FROM workouts WHERE workout_id = ?', (workout_id,))
    result = c.fetchone()
    
    if not result:
        await callback.message.answer("❌ Тренировка не найдена")
        await callback.answer()
        return
    
    # Получаем последнее упражнение
    c.execute('''SELECT id, exercise_name FROM exercises 
                 WHERE workout_id = ? 
                 ORDER BY id DESC LIMIT 1''', (workout_id,))
    last_exercise = c.fetchone()
    
    # Считаем подходы последнего упражнения
    set_count = 0
    current_exercise_id = None
    current_exercise = None
    
    if last_exercise:
        current_exercise_id = last_exercise[0]
        current_exercise = last_exercise[1]
        c.execute('SELECT COUNT(*) FROM sets WHERE exercise_id = ?', (current_exercise_id,))
        set_count = c.fetchone()[0]
    
    conn.close()
    
    # Восстанавливаем FSM состояние
    await state.update_data(
        workout_id=workout_id,
        start_time=result[0],
        current_exercise=current_exercise,
        current_exercise_id=current_exercise_id,
        set_count=set_count
    )
    await state.set_state(WorkoutStates.entering_sets)
    
    exercises_count = get_workout_exercises_count(workout_id)
    
    await callback.message.answer(
        f"🔄 Продолжаем тренировку!\n\n"
        f"📝 Уже записано упражнений: {exercises_count}\n"
        f"{'📍 Последнее: ' + current_exercise if current_exercise else ''}\n\n"
        f"Вводи следующее упражнение или подход:",
        reply_markup=get_workout_menu()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("finish_old_workout:"))
async def cb_finish_old_workout(callback: types.CallbackQuery, state: FSMContext):
    """Завершить незавершённую тренировку"""
    workout_id = callback.data.split(":")[1]
    
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
        f"Теперь можешь начать новую тренировку!",
        reply_markup=get_main_menu()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("cancel_workout:"))
async def cb_cancel_workout(callback: types.CallbackQuery, state: FSMContext):
    """Отменить незавершённую тренировку"""
    workout_id = callback.data.split(":")[1]
    
    cancel_workout_in_db(workout_id)
    await state.clear()
    
    await callback.message.answer(
        "❌ Тренировка отменена и удалена.\n\n"
        "Можешь начать новую!",
        reply_markup=get_main_menu()
    )
    await callback.answer()

# ============ КОМАНДА /cancel ============

@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    """Отменить текущую тренировку"""
    current_state = await state.get_state()
    data = await state.get_data()
    
    if current_state != WorkoutStates.entering_sets.state or 'workout_id' not in data:
        # Проверяем БД на незавершённые
        unfinished = get_unfinished_workout(message.from_user.id)
        if unfinished:
            cancel_workout_in_db(unfinished['workout_id'])
            await message.answer(
                "❌ Незавершённая тренировка отменена.\n\n"
                "Можешь начать новую!",
                reply_markup=get_main_menu()
            )
        else:
            await message.answer("❌ Нет активной тренировки для отмены.")
        return
    
    # Отменяем текущую
    workout_id = data['workout_id']
    cancel_workout_in_db(workout_id)
    await state.clear()
    
    await message.answer(
        "❌ Тренировка отменена и удалена.\n\n"
        "Можешь начать новую!",
        reply_markup=get_main_menu()
    )

# ============ ВВОД УПРАЖНЕНИЙ ============

@dp.callback_query(F.data == "ask_question")
async def ask_question_callback(callback: types.CallbackQuery):
    await callback.message.answer("💡 Задай свой вопрос следующим сообщением.")
    await callback.answer()

@dp.message(WorkoutStates.entering_sets)
async def process_workout_entry(message: types.Message, state: FSMContext):
    # Проверяем не вопрос ли это
    question_words = ['как', 'что', 'чем', 'почему', 'когда', 'стоит', 'можно', 'нужно', 'заменить', 'посоветуй']
    if any(word in message.text.lower() for word in question_words) and len(message.text.split()) > 2:
        await message.answer("🤔 Думаю...")
        answer = await ask_gigachat(message.from_user.id, message.text)
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Ещё вопрос", callback_data="ask_question")],
            [InlineKeyboardButton(text="🏁 Завершить тренировку", callback_data="end_workout")]
        ])
        
        await message.answer(answer, reply_markup=keyboard)
        return
    
    exercise_name, weight, reps, duration, set_type = parse_workout_input(message.text)
    
    if set_type == 'unknown':
        await message.answer(
            "❌ Не понял формат. Попробуй:\n"
            "• Жим лежа 80-10\n"
            "• 80-10 (продолжить упражнение)\n"
            "• Бег 5 минут"
        )
        return
    
    data = await state.get_data()
    workout_id = data['workout_id']
    current_exercise_id = data.get('current_exercise_id')
    set_count = data.get('set_count', 0)
    
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    
    # Если указано название упражнения — создаём новое
    if exercise_name:
        c.execute('''INSERT INTO exercises (workout_id, exercise_name, timestamp)
                     VALUES (?, ?, ?)''',
                  (workout_id, exercise_name, datetime.now().isoformat()))
        current_exercise_id = c.lastrowid
        current_exercise = exercise_name
        set_count = 0  # Сброс счётчика при новом упражнении
        
        await state.update_data(
            current_exercise=exercise_name,
            current_exercise_id=current_exercise_id,
            set_count=0
        )
    elif current_exercise_id is None:
        await message.answer("❌ Сначала укажи упражнение: Жим лежа 80-10")
        conn.close()
        return
    else:
        current_exercise = data.get('current_exercise')
    
    # Сохраняем подход
    c.execute('''INSERT INTO sets (exercise_id, weight_kg, reps, duration_seconds, set_type, timestamp)
                 VALUES (?, ?, ?, ?, ?, ?)''',
              (current_exercise_id, weight, reps, duration, set_type, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    
    set_count += 1
    await state.update_data(set_count=set_count)
    
    # Формируем ответ
    if set_type == 'strength':
        response = f"✅ {current_exercise} — Подход {set_count}: {weight} кг x {reps} раз"
    elif set_type == 'cardio':
        mins = duration // 60
        response = f"✅ {current_exercise}: {mins} минут"
    else:
        response = f"✅ {current_exercise}: {duration} секунд"
    
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
    
    # Используем функцию завершения
    stats = finish_workout_in_db(workout_id, callback.from_user.id)
    
    if 'error' in stats:
        await callback.message.answer(f"❌ {stats['error']}")
        await callback.answer()
        return
    
    await state.clear()
    
    tonnage_str = f"{stats['tonnage']:.0f}" if stats['tonnage'] else "0"
    
    await callback.message.answer(
        f"🏁 Тренировка завершена!\n"
        f"⏱ Длительность: {stats['duration']} минут\n"
        f"📊 Выполнено: {stats['exercises_count']} упражнений, {stats['sets_count']} подходов\n"
        f"💪 Общий тоннаж: {tonnage_str} кг",
        reply_markup=get_main_menu()
    )
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
    
    # Общая статистика за всё время (только завершённые)
    c.execute('''SELECT COUNT(*), SUM(duration_minutes)
                 FROM workouts WHERE user_id = ? AND end_time IS NOT NULL''', (user_id,))
    total_workouts, total_minutes = c.fetchone()
    
    # Рекорды по ВСЕМ упражнениям
    c.execute('''SELECT e.exercise_name, MAX(s.weight_kg)
                 FROM workouts w
                 JOIN exercises e ON w.workout_id = e.workout_id
                 JOIN sets s ON e.id = s.exercise_id
                 WHERE w.user_id = ? AND s.set_type = 'strength' AND s.weight_kg > 0
                 GROUP BY e.exercise_name
                 ORDER BY MAX(s.weight_kg) DESC''', (user_id,))
    records = c.fetchall()
    
    conn.close()
    
    stats = f"📊 Статистика за всё время:\n\n"
    stats += f"Всего тренировок: {total_workouts or 0}\n"
    stats += f"Общее время: {total_minutes or 0} минут\n\n"
    
    if records:
        stats += "🏆 Рекорды по весам:\n"
        for name, weight in records:
            stats += f"• {name}: {weight} кг\n"
    else:
        stats += "Пока нет записей с весами"
    
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
    # Определяем начало недели (понедельник)
    today = datetime.now().date()
    start_of_week = today - timedelta(days=today.weekday())
    end_of_week = start_of_week + timedelta(days=6)
    
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    
    # Только завершённые тренировки
    c.execute('''SELECT w.workout_id, w.start_time, w.duration_minutes
                 FROM workouts w
                 WHERE w.user_id = ? AND date(w.start_time) >= ? AND date(w.start_time) <= ?
                   AND w.end_time IS NOT NULL
                 ORDER BY w.start_time ASC''', 
              (user_id, start_of_week.isoformat(), end_of_week.isoformat()))
    
    workouts = c.fetchall()
    
    if not workouts:
        await message.answer(
            f"📅 Тренировки за неделю ({start_of_week.strftime('%d.%m')} - {end_of_week.strftime('%d.%m.%Y')}):\n\n"
            "Пока нет тренировок на этой неделе.\n"
            "Начни с /start_workout!"
        )
        conn.close()
        return
    
    history = f"📅 Тренировки за неделю ({start_of_week.strftime('%d.%m')} - {end_of_week.strftime('%d.%m.%Y')}):\n\n"
    
    weekdays = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']
    
    for idx, (workout_id, start_time, duration) in enumerate(workouts, 1):
        dt = datetime.fromisoformat(start_time)
        weekday = weekdays[dt.weekday()]
        
        history += f"Тренировка {idx} — {weekday} {dt.strftime('%d.%m')}\n"
        
        # Получаем упражнения
        c.execute('''SELECT e.exercise_name, s.weight_kg, s.reps, s.duration_seconds, s.set_type
                     FROM exercises e
                     JOIN sets s ON e.id = s.exercise_id
                     WHERE e.workout_id = ?
                     ORDER BY e.id, s.id''', (workout_id,))
        
        exercises = c.fetchall()
        
        # Группируем по упражнениям
        ex_groups = {}
        for ex_name, weight, reps, duration_sec, set_type in exercises:
            if ex_name not in ex_groups:
                ex_groups[ex_name] = {'type': set_type, 'weights': [], 'reps': [], 'duration': duration_sec}
            if set_type == 'strength':
                ex_groups[ex_name]['weights'].append(int(weight) if weight else 0)
                ex_groups[ex_name]['reps'].append(reps if reps else 0)
        
        for ex_name, data in ex_groups.items():
            if data['type'] == 'strength':
                weights_str = ' → '.join(map(str, data['weights']))
                history += f"• {ex_name} — {len(data['weights'])} подх. ({weights_str} кг)\n"
            elif data['type'] == 'cardio':
                mins = data['duration'] // 60 if data['duration'] else 0
                history += f"• {ex_name} — {mins} мин\n"
            else:
                history += f"• {ex_name} — {data['duration']} сек\n"
        
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
    help_text = (
        "ℹ️ Как пользоваться ботом:\n\n"
        "🏋️ ЗАПИСЬ ТРЕНИРОВКИ\n"
        "1. Нажми 'Начать тренировку' или /start_workout\n"
        "2. Вводи упражнения:\n"
        "   • Жим лежа 80-10 (название + вес-повторы)\n"
        "   • 80-8 (следующий подход)\n"
        "   • Бег 5 минут\n"
        "   • Планка 60 секунд\n"
        "3. Нажми 'Завершить тренировку'\n\n"
        "📊 СТАТИСТИКА (/stats)\n"
        "За всё время: количество тренировок, общее время, рекорды\n\n"
        "📅 ИСТОРИЯ (/history)\n"
        "Все тренировки за текущую неделю\n\n"
        "🗑 УДАЛЕНИЕ (/delete)\n"
        "Удаляет последний подход\n\n"
        "❌ ОТМЕНА (/cancel)\n"
        "Отменяет текущую тренировку\n\n"
        "💬 ВОПРОСЫ\n"
        "Нажми 'Задать вопрос' или просто напиши\n\n"
        "📝 ОБРАТНАЯ СВЯЗЬ (/feedback)\n"
        "Напиши пожелания или сообщи о баге"
    )
    
    await message.answer(help_text)

# ============ УДАЛЕНИЕ ============

@dp.message(Command("delete"))
async def cmd_delete(message: types.Message):
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    
    # Находим последний подход пользователя
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
        await message.answer(f"✅ Удалён подход: {ex_name} {weight}кг x {reps}")
    else:
        await message.answer("❌ Нет записей для удаления")
    
    conn.close()

# ============ FEEDBACK ============

@dp.message(Command("feedback"))
async def cmd_feedback(message: types.Message):
    text = message.text.replace('/feedback', '').strip()
    
    if not text:
        await message.answer("📝 Напиши: /feedback текст сообщения")
        return
    
    try:
        await bot.send_message(
            ADMIN_ID,
            f"📢 Feedback от пользователя {message.from_user.id}:\n"
            f"Имя: {message.from_user.full_name}\n"
            f"Username: @{message.from_user.username}\n\n"
            f"{text}"
        )
        await message.answer("✅ Сообщение отправлено разработчику")
    except Exception as e:
        logger.error(f"Ошибка отправки feedback: {e}")
        await message.answer("❌ Ошибка отправки сообщения")

# ============ АДМИНСКИЕ КОМАНДЫ ============

@dp.message(Command("export"))
async def cmd_export(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Команда недоступна")
        return
    
    try:
        file = FSInputFile('workouts.db')
        await message.answer_document(file, caption="📦 База данных workouts.db")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("sync"))
async def cmd_sync(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Команда недоступна")
        return
    
    await message.answer("🔄 Синхронизация с Google Sheets...")
    await sync_to_google_sheets()
    await message.answer("✅ Синхронизация завершена!")

# ============ ОБРАБОТКА ОСТАЛЬНЫХ СООБЩЕНИЙ ============

@dp.message()
async def handle_any_message(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    
    if current_state:
        return
    
    question_words = ['как', 'что', 'чем', 'почему', 'когда', 'стоит', 'можно', 'нужно', 'заменить', 'посоветуй', 'подскажи', '?']
    
    if any(word in message.text.lower() for word in question_words):
        await message.answer("🤔 Думаю...")
        answer = await ask_gigachat(message.from_user.id, message.text)
        await message.answer(answer)
    else:
        await message.answer(
            "Не понял команду. Напиши 'Привет' для начала или /help для справки",
            reply_markup=get_main_menu()
        )

# ============ MAIN ============

async def main():
    logger.info("🚀 Бот запущен!")
    
    # Настраиваем Google Sheets
    try:
        client = get_google_sheets_client()
        if client and GOOGLE_SPREADSHEET_ID:
            sheet = client.open_by_key(GOOGLE_SPREADSHEET_ID)
            try:
                worksheet = sheet.worksheet("Data")
                logger.info("✅ Лист Data найден")
            except:
                worksheet = sheet.add_worksheet(title="Data", rows=1000, cols=10)
                worksheet.append_row(["User ID", "Дата", "Время начала", "Время конца", 
                                      "Длительность (мин)", "Упражнение", "Вес (кг)", 
                                      "Повторы", "Длительность (сек)", "Тип"])
                logger.info("✅ Создан лист Data в Google Sheets")
    except Exception as e:
        logger.error(f"❌ Ошибка настройки Google Sheets: {e}")
    
    # Запускаем планировщик синхронизации (каждый день в 23:59 МСК)
    scheduler.add_job(
        sync_to_google_sheets,
        CronTrigger(hour=23, minute=59, timezone=pytz.timezone('Europe/Moscow')),
        id='daily_sync',
        replace_existing=True
    )
    scheduler.start()
    logger.info("✅ Планировщик синхронизации запущен (23:59 МСК)")
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
