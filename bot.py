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
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from dotenv import load_dotenv
import aiohttp

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Токены
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GIGACHAT_CLIENT_ID = os.getenv("GIGACHAT_CLIENT_ID")
GIGACHAT_CLIENT_SECRET = os.getenv("GIGACHAT_CLIENT_SECRET")
GOOGLE_SHEETS_CREDENTIALS = os.getenv("GOOGLE_SHEETS_CREDENTIALS")
GOOGLE_SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID")

# Админ ID
ADMIN_ID = 295220429

# Инициализация бота
bot = Bot(token=TELEGRAM_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# FSM состояния
class WorkoutStates(StatesGroup):
    in_workout = State()
    entering_sets = State()

# Дни недели на русском
WEEKDAYS_RU = {
    0: "Понедельник",
    1: "Вторник", 
    2: "Среда",
    3: "Четверг",
    4: "Пятница",
    5: "Суббота",
    6: "Воскресенье"
}

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
                  duration_minutes INTEGER,
                  synced_to_sheets INTEGER DEFAULT 0)''')
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
    c.execute('''CREATE TABLE IF NOT EXISTS feedback
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  username TEXT,
                  message TEXT,
                  timestamp TEXT)''')
    conn.commit()
    conn.close()

init_db()

# Google Sheets интеграция
async def get_sheets_service():
    if not GOOGLE_SHEETS_CREDENTIALS or not GOOGLE_SPREADSHEET_ID:
        logger.warning("Google Sheets credentials not configured")
        return None, None
    
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
        
        creds_dict = json.loads(GOOGLE_SHEETS_CREDENTIALS)
        creds = Credentials.from_service_account_info(
            creds_dict,
            scopes=['https://www.googleapis.com/auth/spreadsheets']
        )
        service = build('sheets', 'v4', credentials=creds)
        return service, GOOGLE_SPREADSHEET_ID
    except Exception as e:
        logger.error(f"Google Sheets init error: {e}")
        return None, None

async def sync_to_google_sheets():
    """Синхронизация данных в Google Sheets"""
    service, spreadsheet_id = await get_sheets_service()
    if not service:
        logger.warning("Google Sheets service not available")
        return
    
    try:
        conn = sqlite3.connect('workouts.db')
        c = conn.cursor()
        
        # Получаем несинхронизированные тренировки
        c.execute('''SELECT w.id, w.user_id, w.workout_id, w.start_time, w.end_time, w.duration_minutes
                     FROM workouts w
                     WHERE w.synced_to_sheets = 0''')
        workouts = c.fetchall()
        
        if not workouts:
            logger.info("No new workouts to sync")
            conn.close()
            return
        
        # Подготавливаем данные для листа "Тренировки"
        workout_rows = []
        detail_rows = []
        
        for workout in workouts:
            w_id, user_id, workout_id, start_time, end_time, duration = workout
            
            # Получаем упражнения и подходы
            c.execute('''SELECT e.exercise_name, s.weight_kg, s.reps, s.duration_seconds, s.set_type
                         FROM exercises e
                         JOIN sets s ON e.id = s.exercise_id
                         WHERE e.workout_id = ?
                         ORDER BY e.id, s.id''', (workout_id,))
            exercises = c.fetchall()
            
            exercise_count = len(set(ex[0] for ex in exercises))
            sets_count = len(exercises)
            tonnage = sum((ex[1] or 0) * (ex[2] or 0) for ex in exercises if ex[4] == 'strength')
            
            dt = datetime.fromisoformat(start_time) if start_time else datetime.now()
            
            workout_rows.append([
                user_id,
                dt.strftime('%d.%m.%Y'),
                dt.strftime('%H:%M'),
                duration or 0,
                exercise_count,
                sets_count,
                tonnage
            ])
            
            # Детали по упражнениям
            set_num = {}
            for ex in exercises:
                ex_name, weight, reps, duration_sec, set_type = ex
                if ex_name not in set_num:
                    set_num[ex_name] = 0
                set_num[ex_name] += 1
                
                detail_rows.append([
                    user_id,
                    dt.strftime('%d.%m.%Y'),
                    ex_name,
                    set_num[ex_name],
                    weight or '',
                    reps or '',
                    duration_sec or ''
                ])
            
            # Помечаем как синхронизированную
            c.execute('UPDATE workouts SET synced_to_sheets = 1 WHERE id = ?', (w_id,))
        
        conn.commit()
        conn.close()
        
        # Отправляем в Google Sheets
        sheets = service.spreadsheets()
        
        # Добавляем в лист "Тренировки"
        if workout_rows:
            sheets.values().append(
                spreadsheetId=spreadsheet_id,
                range='Тренировки!A:G',
                valueInputOption='USER_ENTERED',
                insertDataOption='INSERT_ROWS',
                body={'values': workout_rows}
            ).execute()
        
        # Добавляем в лист "Детали"
        if detail_rows:
            sheets.values().append(
                spreadsheetId=spreadsheet_id,
                range='Детали!A:G',
                valueInputOption='USER_ENTERED',
                insertDataOption='INSERT_ROWS',
                body={'values': detail_rows}
            ).execute()
        
        logger.info(f"Synced {len(workout_rows)} workouts to Google Sheets")
        
    except Exception as e:
        logger.error(f"Google Sheets sync error: {e}")

async def setup_sheets_headers():
    """Создаёт заголовки в таблице при первом запуске"""
    service, spreadsheet_id = await get_sheets_service()
    if not service:
        return
    
    try:
        sheets = service.spreadsheets()
        
        # Проверяем/создаём листы
        spreadsheet = sheets.get(spreadsheetId=spreadsheet_id).execute()
        existing_sheets = [s['properties']['title'] for s in spreadsheet['sheets']]
        
        requests = []
        if 'Тренировки' not in existing_sheets:
            requests.append({'addSheet': {'properties': {'title': 'Тренировки'}}})
        if 'Детали' not in existing_sheets:
            requests.append({'addSheet': {'properties': {'title': 'Детали'}}})
        
        if requests:
            sheets.batchUpdate(spreadsheetId=spreadsheet_id, body={'requests': requests}).execute()
        
        # Добавляем заголовки
        sheets.values().update(
            spreadsheetId=spreadsheet_id,
            range='Тренировки!A1:G1',
            valueInputOption='USER_ENTERED',
            body={'values': [['User ID', 'Дата', 'Время', 'Длительность (мин)', 'Упражнений', 'Подходов', 'Тоннаж (кг)']]}
        ).execute()
        
        sheets.values().update(
            spreadsheetId=spreadsheet_id,
            range='Детали!A1:G1',
            valueInputOption='USER_ENTERED',
            body={'values': [['User ID', 'Дата', 'Упражнение', 'Подход №', 'Вес (кг)', 'Повторы', 'Длительность (сек)']]}
        ).execute()
        
        logger.info("Google Sheets headers created")
        
    except Exception as e:
        logger.error(f"Setup sheets headers error: {e}")

# Планировщик синхронизации
async def daily_sync_scheduler():
    """Запускает синхронизацию каждый день в 23:59 МСК"""
    while True:
        now = datetime.utcnow() + timedelta(hours=3)  # МСК = UTC+3
        
        # Вычисляем время до 23:59
        target = now.replace(hour=23, minute=59, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        
        wait_seconds = (target - now).total_seconds()
        logger.info(f"Next sync in {wait_seconds/3600:.1f} hours")
        
        await asyncio.sleep(wait_seconds)
        
        logger.info("Running daily sync to Google Sheets...")
        await sync_to_google_sheets()

# Улучшенный парсер упражнений
def parse_workout_input(text: str) -> Tuple[Optional[str], Optional[float], Optional[int], Optional[int], str]:
    text = text.strip()
    
    # Паттерн 1: Упражнение + вес-повторы (Жим лежа 80-10)
    pattern1 = r'^(.+?)\s+(\d+(?:\.\d+)?)\s*[-xх×*]\s*(\d+)$'
    match = re.match(pattern1, text, re.IGNORECASE)
    if match:
        exercise = match.group(1).strip()
        weight = float(match.group(2))
        reps = int(match.group(3))
        return (exercise, weight, reps, None, 'strength')
    
    # Паттерн 2: Только вес-повторы (80-10)
    pattern2 = r'^(\d+(?:\.\d+)?)\s*[-xх×*]\s*(\d+)$'
    match = re.match(pattern2, text)
    if match:
        weight = float(match.group(1))
        reps = int(match.group(2))
        return (None, weight, reps, None, 'strength')
    
    # Паттерн 3: Упражнение + минуты (Бег 5 минут)
    pattern3 = r'^(.+?)\s+(\d+)\s*(мин|минут|минуты|min|м).*$'
    match = re.match(pattern3, text, re.IGNORECASE)
    if match:
        exercise = match.group(1).strip()
        minutes = int(match.group(2))
        return (exercise, None, None, minutes * 60, 'cardio')
    
    # Паттерн 4: Упражнение + секунды (Планка 60 секунд)
    pattern4 = r'^(.+?)\s+(\d+)\s*(сек|секунд|секунды|sec|с).*$'
    match = re.match(pattern4, text, re.IGNORECASE)
    if match:
        exercise = match.group(1).strip()
        seconds = int(match.group(2))
        return (exercise, None, None, seconds, 'static')
    
    # Паттерн 5: Только название упражнения
    if not any(char.isdigit() for char in text):
        return (text, None, None, None, 'unknown')
    
    return (None, None, None, None, 'unknown')

# GigaChat API
async def get_gigachat_token():
    if not GIGACHAT_CLIENT_ID or not GIGACHAT_CLIENT_SECRET:
        logger.error("GIGACHAT_CLIENT_ID or GIGACHAT_CLIENT_SECRET not set")
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
                    logger.info("GigaChat token получен успешно")
                    return result.get('access_token')
                else:
                    error_text = await response.text()
                    logger.error(f"GigaChat auth error {response.status}: {error_text}")
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
        
        context = "История тренировок пользователя:\n"
        for row in history[:20]:
            date, exercise, weight, reps, duration, set_type = row
            if set_type == 'strength':
                context += f"{date}: {exercise} - {weight}кг x {reps} раз\n"
            elif set_type in ['cardio', 'static']:
                context += f"{date}: {exercise} - {duration} секунд\n"
        
        token = await get_gigachat_token()
        if not token:
            return "Ошибка подключения к GigaChat. Проверь настройки API ключа."
        
        url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'Authorization': f'Bearer {token}'
        }
        
        payload = {
            "model": "GigaChat",
            "messages": [
                {"role": "system", "content": f"Ты опытный персональный тренер. Анализируй данные и давай конкретные советы. {context}"},
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
                else:
                    error_text = await response.text()
                    logger.error(f"GigaChat API error {response.status}: {error_text}")
                    return f"Ошибка GigaChat API: {response.status}"
                    
    except Exception as e:
        logger.error(f"GigaChat error: {e}")
        return "Произошла ошибка при обращении к ИИ"

# Кнопки главного меню
def get_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏋️ Начать тренировку", callback_data="start_workout")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="show_stats"),
         InlineKeyboardButton(text="📅 История", callback_data="show_history")],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="show_help")]
    ])

# Хэндлеры
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "💪 Привет! Я твой персональный тренер.\n\n"
        "🏋️ Как записывать тренировки:\n"
        "• Жим лежа 80-10 (упражнение вес-повторы)\n"
        "• Бег 5 минут\n"
        "• Планка 60 секунд\n\n"
        "Выбери действие:",
        reply_markup=get_main_keyboard()
    )

# Обработка приветствий
@dp.message(F.text.lower().in_(['привет', 'прив', 'хай', 'hello', 'hi', 'здравствуй', 'здравствуйте']))
async def handle_greeting(message: types.Message):
    await message.answer(
        "💪 Привет! Я твой персональный тренер.\n\n"
        "🏋️ Как записывать тренировки:\n"
        "• Жим лежа 80-10 (упражнение вес-повторы)\n"
        "• Бег 5 минут\n"
        "• Планка 60 секунд\n\n"
        "Выбери действие:",
        reply_markup=get_main_keyboard()
    )

@dp.callback_query(F.data == "start_workout")
async def cb_start_workout(callback: types.CallbackQuery, state: FSMContext):
    workout_id = f"{callback.from_user.id}_{datetime.now().timestamp()}"
    start_time = datetime.now().isoformat()
    
    await state.update_data(
        workout_id=workout_id,
        start_time=start_time,
        current_exercise=None,
        current_exercise_id=None,
        set_count=0
    )
    await state.set_state(WorkoutStates.entering_sets)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Задать вопрос", callback_data="ask_question")],
        [InlineKeyboardButton(text="🏁 Завершить тренировку", callback_data="end_workout")]
    ])
    
    await callback.message.answer(
        "🏋️ Тренировка начата!\n"
        f"⏱ Время: {datetime.now().strftime('%H:%M')}\n\n"
        "Вводи упражнения в формате:\n"
        "• Жим лежа 80-10\n"
        "• Бег 5 минут\n"
        "• 80-10 (следующий подход)\n\n"
        "Или нажми 💬 чтобы задать вопрос",
        reply_markup=keyboard
    )
    await callback.answer()

@dp.message(Command("start_workout"))
async def cmd_start_workout(message: types.Message, state: FSMContext):
    workout_id = f"{message.from_user.id}_{datetime.now().timestamp()}"
    start_time = datetime.now().isoformat()
    
    await state.update_data(
        workout_id=workout_id,
        start_time=start_time,
        current_exercise=None,
        current_exercise_id=None,
        set_count=0
    )
    await state.set_state(WorkoutStates.entering_sets)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Задать вопрос", callback_data="ask_question")],
        [InlineKeyboardButton(text="🏁 Завершить тренировку", callback_data="end_workout")]
    ])
    
    await message.answer(
        "🏋️ Тренировка начата!\n"
        f"⏱ Время: {datetime.now().strftime('%H:%M')}\n\n"
        "Вводи упражнения в формате:\n"
        "• Жим лежа 80-10\n"
        "• Бег 5 минут\n"
        "• 80-10 (следующий подход)\n\n"
        "Или нажми 💬 чтобы задать вопрос",
        reply_markup=keyboard
    )

@dp.callback_query(F.data == "show_stats")
async def cb_show_stats(callback: types.CallbackQuery):
    await show_stats(callback.from_user.id, callback.message)
    await callback.answer()

@dp.callback_query(F.data == "show_history")
async def cb_show_history(callback: types.CallbackQuery):
    await show_history(callback.from_user.id, callback.message)
    await callback.answer()

@dp.callback_query(F.data == "show_help")
async def cb_show_help(callback: types.CallbackQuery):
    await show_help(callback.message)
    await callback.answer()

@dp.callback_query(F.data == "ask_question")
async def ask_question_callback(callback: types.CallbackQuery):
    await callback.message.answer(
        "💡 Задай свой вопрос следующим сообщением.\n"
        "Например: Чем заменить жим лежа? или Стоит ли увеличить вес?"
    )
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
            "• 80-10 (для того же упражнения)\n"
            "• Бег 5 минут\n\n"
            "Или нажми 💬 чтобы задать вопрос"
        )
        return
    
    data = await state.get_data()
    workout_id = data['workout_id']
    current_exercise = data.get('current_exercise')
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
        await message.answer("❌ Сначала укажи название упражнения, например: Жим лежа 80-10")
        conn.close()
        return
    
    # Сохраняем подход
    c.execute('''INSERT INTO sets (exercise_id, weight_kg, reps, duration_seconds, set_type, timestamp)
                 VALUES (?, ?, ?, ?, ?, ?)''',
              (current_exercise_id, weight, reps, duration, set_type, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    
    set_count = set_count + 1
    await state.update_data(set_count=set_count)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Задать вопрос", callback_data="ask_question")],
        [InlineKeyboardButton(text="🏁 Завершить тренировку", callback_data="end_workout")]
    ])
    
    # Формируем ответ
    if set_type == 'strength':
        response = f"✅ {current_exercise} — Подход {set_count}: {weight} кг x {reps} раз"
    elif set_type == 'cardio':
        mins = duration // 60
        response = f"✅ {current_exercise}: {mins} минут"
    elif set_type == 'static':
        response = f"✅ {current_exercise}: {duration} секунд"
    
    await message.answer(response, reply_markup=keyboard)

@dp.callback_query(F.data == "end_workout")
async def end_workout(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    
    if 'workout_id' not in data:
        await callback.message.answer("❌ Тренировка не начата. Используй /start_workout")
        await callback.answer()
        return
    
    workout_id = data['workout_id']
    start_time = datetime.fromisoformat(data['start_time'])
    end_time = datetime.now()
    duration = int((end_time - start_time).total_seconds() / 60)
    
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('''INSERT INTO workouts (user_id, workout_id, start_time, end_time, duration_minutes)
                 VALUES (?, ?, ?, ?, ?)''',
              (callback.from_user.id, workout_id, data['start_time'],
               end_time.isoformat(), duration))
    
    c.execute('''SELECT COUNT(DISTINCT e.id), COUNT(s.id), 
                        SUM(CASE WHEN s.set_type = 'strength' THEN s.weight_kg * s.reps ELSE 0 END)
                 FROM exercises e
                 JOIN sets s ON e.id = s.exercise_id
                 WHERE e.workout_id = ?''', (workout_id,))
    exercises_count, sets_count, tonnage = c.fetchone()
    
    conn.commit()
    conn.close()
    
    await state.clear()
    
    tonnage_str = f"{tonnage:.0f}" if tonnage else "0"
    
    await callback.message.answer(
        f"🏁 Тренировка завершена!\n"
        f"⏱ Длительность: {duration} минут\n"
        f"📊 Выполнено: {exercises_count} упражнений, {sets_count} подходов\n"
        f"💪 Общий тоннаж: {tonnage_str} кг",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()

# /stats
@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    await show_stats(message.from_user.id, message)

async def show_stats(user_id: int, message: types.Message):
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    
    # Общая статистика за всё время
    c.execute('''SELECT COUNT(*), SUM(duration_minutes)
                 FROM workouts WHERE user_id = ?''', (user_id,))
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
    
    stats = f"📊 Твоя статистика за всё время:\n\n"
    stats += f"🏋️ Всего тренировок: {total_workouts or 0}\n"
    stats += f"⏱ Общее время: {total_minutes or 0} минут\n\n"
    
    if records:
        stats += "🏆 Рекорды по весам:\n"
        for name, weight in records:
            stats += f"• {name}: {weight} кг\n"
    else:
        stats += "Пока нет записей с весами"
    
    await message.answer(stats)

# /history — тренировки за текущую неделю
@dp.message(Command("history"))
async def cmd_history(message: types.Message):
    await show_history(message.from_user.id, message)

async def show_history(user_id: int, message: types.Message):
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    
    # Определяем границы текущей недели (пн-вс)
    today = datetime.now().date()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    
    c.execute('''SELECT workout_id, start_time, duration_minutes
                 FROM workouts
                 WHERE user_id = ? AND date(start_time) >= ? AND date(start_time) <= ?
                 ORDER BY start_time''', 
              (user_id, monday.isoformat(), sunday.isoformat()))
    workouts = c.fetchall()
    
    if not workouts:
        await message.answer(
            f"📅 Тренировки за неделю ({monday.strftime('%d.%m')} - {sunday.strftime('%d.%m')}):\n\n"
            "Пока нет тренировок на этой неделе.\n"
            "Начни с /start_workout!"
        )
        conn.close()
        return
    
    history = f"📅 Тренировки за неделю ({monday.strftime('%d.%m')} - {sunday.strftime('%d.%m')}):\n\n"
    
    for i, (workout_id, start_time, duration) in enumerate(workouts, 1):
        dt = datetime.fromisoformat(start_time)
        weekday = WEEKDAYS_RU[dt.weekday()]
        
        history += f"Тренировка {i} — {weekday} {dt.strftime('%d.%m')}\n"
        
        # Получаем упражнения этой тренировки
        c.execute('''SELECT e.exercise_name, s.weight_kg, s.reps, s.duration_seconds, s.set_type
                     FROM exercises e
                     JOIN sets s ON e.id = s.exercise_id
                     WHERE e.workout_id = ?
                     ORDER BY e.id, s.id''', (workout_id,))
        exercises = c.fetchall()
        
        # Группируем по упражнениям
        ex_data = {}
        for ex_name, weight, reps, duration_sec, set_type in exercises:
            if ex_name not in ex_data:
                ex_data[ex_name] = {'weights': [], 'reps': [], 'duration': None, 'type': set_type}
            if set_type == 'strength':
                ex_data[ex_name]['weights'].append(weight)
                ex_data[ex_name]['reps'].append(reps)
            else:
                ex_data[ex_name]['duration'] = duration_sec
        
        for ex_name, data in ex_data.items():
            if data['type'] == 'strength':
                weights_str = '→'.join(str(int(w)) for w in data['weights'])
                reps_str = '→'.join(str(r) for r in data['reps'])
                history += f"• {ex_name} — {len(data['weights'])} подх. ({weights_str} кг)\n"
            elif data['type'] == 'cardio':
                mins = data['duration'] // 60 if data['duration'] else 0
                history += f"• {ex_name} — {mins} мин\n"
            elif data['type'] == 'static':
                history += f"• {ex_name} — {data['duration']} сек\n"
        
        history += "\n"
    
    conn.close()
    await message.answer(history)

# /help
@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await show_help(message)

async def show_help(message: types.Message):
    help_text = """❓ Как пользоваться ботом:

🏋️ ЗАПИСЬ ТРЕНИРОВКИ
1. Нажми "Начать тренировку" или /start_workout
2. Вводи упражнения:
   • Жим лежа 80-10 (название + вес-повторы)
   • 80-8 (следующий подход того же упражнения)
   • Бег 5 минут
   • Планка 60 секунд
3. Нажми "Завершить тренировку"

📊 СТАТИСТИКА (/stats)
Показывает за ВСЁ время:
• Количество тренировок
• Общее время
• Рекорды по весам для каждого упражнения

📅 ИСТОРИЯ (/history)
Показывает все тренировки за текущую неделю (пн-вс) с деталями по упражнениям

🗑 УДАЛЕНИЕ (/delete)
Удаляет последний записанный подход (если ошибся)

💬 ВОПРОСЫ
Во время тренировки нажми "Задать вопрос" или просто напиши вопрос в чат

📝 ОБРАТНАЯ СВЯЗЬ (/feedback)
Напиши пожелания или сообщи о багах"""
    
    await message.answer(help_text)

# /delete — удалить последнюю запись
@dp.message(Command("delete"))
async def cmd_delete(message: types.Message, state: FSMContext):
    data = await state.get_data()
    
    if 'workout_id' not in data:
        await message.answer("❌ Нет активной тренировки. Удалять нечего.")
        return
    
    workout_id = data['workout_id']
    
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    
    # Находим последний подход в текущей тренировке
    c.execute('''SELECT s.id, e.exercise_name, s.weight_kg, s.reps
                 FROM sets s
                 JOIN exercises e ON s.exercise_id = e.id
                 WHERE e.workout_id = ?
                 ORDER BY s.id DESC LIMIT 1''', (workout_id,))
    last_set = c.fetchone()
    
    if not last_set:
        await message.answer("❌ Нет записей для удаления")
        conn.close()
        return
    
    set_id, ex_name, weight, reps = last_set
    
    # Удаляем подход
    c.execute('DELETE FROM sets WHERE id = ?', (set_id,))
    conn.commit()
    
    # Обновляем счётчик
    set_count = data.get('set_count', 1) - 1
    if set_count < 0:
        set_count = 0
    await state.update_data(set_count=set_count)
    
    conn.close()
    
    await message.answer(f"🗑 Удалён подход: {ex_name} {weight}кг x {reps}")

# /feedback — обратная связь
@dp.message(Command("feedback"))
async def cmd_feedback(message: types.Message):
    text = message.text.replace('/feedback', '').strip()
    
    if not text:
        await message.answer("📝 Напиши своё сообщение после команды:\n/feedback Хочу добавить тёмную тему")
        return
    
    # Сохраняем в базу
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('''INSERT INTO feedback (user_id, username, message, timestamp)
                 VALUES (?, ?, ?, ?)''',
              (message.from_user.id, message.from_user.username, text, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    
    # Отправляем админу
    try:
        await bot.send_message(
            ADMIN_ID,
            f"📝 Новый feedback!\n\n"
            f"От: @{message.from_user.username or 'unknown'} (ID: {message.from_user.id})\n"
            f"Сообщение: {text}"
        )
    except Exception as e:
        logger.error(f"Failed to send feedback to admin: {e}")
    
    await message.answer("✅ Спасибо за обратную связь! Сообщение отправлено.")

# /export — только для админа
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

# /sync — ручная синхронизация (для админа)
@dp.message(Command("sync"))
async def cmd_sync(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Команда недоступна")
        return
    
    await message.answer("🔄 Синхронизация с Google Sheets...")
    await sync_to_google_sheets()
    await message.answer("✅ Синхронизация завершена!")

# Обработка любых сообщений (вопросы вне тренировки)
@dp.message()
async def handle_message(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state:
        return
    
    # Если это похоже на вопрос — отвечаем через GigaChat
    question_words = ['как', 'что', 'чем', 'почему', 'когда', 'стоит', 'можно', 'нужно', 'заменить', 'посоветуй', 'подскажи', '?']
    if any(word in message.text.lower() for word in question_words):
        await message.answer("🤔 Думаю...")
        answer = await ask_gigachat(message.from_user.id, message.text)
        await message.answer(answer)
    else:
        await message.answer(
            "Не понял команду. Напиши 'Привет' для начала или /help для справки",
            reply_markup=get_main_keyboard()
        )

async def main():
    logger.info("Бот запущен!")
    
    # Настраиваем заголовки Google Sheets
    await setup_sheets_headers()
    
    # Запускаем планировщик синхронизации
    asyncio.create_task(daily_sync_scheduler())
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
