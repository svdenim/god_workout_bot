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
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
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

# Google Sheets
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
                     WHERE date(w.start_time) = ?''', (today.isoformat(),))
        
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

# Парсер упражнений
def parse_workout_input(text: str) -> Tuple[Optional[str], Optional[float], Optional[int], Optional[int], str]:
    text = text.strip()
    
    pattern1 = r'^(.+?)\s+(\d+(?:\.\d+)?)\s*[-xх×*]\s*(\d+)$'
    match = re.match(pattern1, text, re.IGNORECASE)
    if match:
        return (match.group(1).strip(), float(match.group(2)), int(match.group(3)), None, 'strength')
    
    pattern2 = r'^(\d+(?:\.\d+)?)\s*[-xх×*]\s*(\d+)$'
    match = re.match(pattern2, text)
    if match:
        return (None, float(match.group(1)), int(match.group(2)), None, 'strength')
    
    pattern3 = r'^(.+?)\s+(\d+)\s*(мин|минут|минуты|min|м).*$'
    match = re.match(pattern3, text, re.IGNORECASE)
    if match:
        return (match.group(1).strip(), None, None, int(match.group(2)) * 60, 'cardio')
    
    pattern4 = r'^(.+?)\s+(\d+)\s*(сек|секунд|секунды|sec|с).*$'
    match = re.match(pattern4, text, re.IGNORECASE)
    if match:
        return (match.group(1).strip(), None, None, int(match.group(2)), 'static')
    
    if not any(char.isdigit() for char in text):
        return (text, None, None, None, 'unknown')
    
    return (None, None, None, None, 'unknown')

# GigaChat API
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

# Хэндлеры
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏋️ Начать тренировку", callback_data="start_workout")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="stats"),
         InlineKeyboardButton(text="📅 История", callback_data="history")],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="help")]
    ])
    
    await message.answer(
        "💪 Привет! Я твой персональный тренер.\n\n"
        "Выбери действие:",
        reply_markup=keyboard
    )

@dp.message(F.text.lower().in_(["привет", "хай", "hi", "hello", "здравствуй"]))
async def greeting(message: types.Message):
    await cmd_start(message)

@dp.callback_query(F.data == "start_workout")
@dp.message(Command("start_workout"))
async def cmd_start_workout(event: types.Message | types.CallbackQuery, state: FSMContext):
    message = event.message if isinstance(event, types.CallbackQuery) else event
    
    workout_id = f"{message.chat.id}_{datetime.now().timestamp()}"
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
        "Вводи упражнения:\n"
        "• Жим лежа 80-10\n"
        "• Бег 5 минут\n"
        "• 80-10 (следующий подход)",
        reply_markup=keyboard
    )
    
    if isinstance(event, types.CallbackQuery):
        await event.answer()

@dp.callback_query(F.data == "ask_question")
async def ask_question_callback(callback: types.CallbackQuery):
    await callback.message.answer("💡 Задай свой вопрос следующим сообщением.")
    await callback.answer()

@dp.message(WorkoutStates.entering_sets)
async def process_workout_entry(message: types.Message, state: FSMContext):
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
    
    if exercise_name:
        c.execute('''INSERT INTO exercises (workout_id, exercise_name, timestamp)
                     VALUES (?, ?, ?)''',
                  (workout_id, exercise_name, datetime.now().isoformat()))
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
    else:
        current_exercise = data.get('current_exercise')
    
    c.execute('''INSERT INTO sets (exercise_id, weight_kg, reps, duration_seconds, set_type, timestamp)
                 VALUES (?, ?, ?, ?, ?, ?)''',
              (current_exercise_id, weight, reps, duration, set_type, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    
    set_count += 1
    await state.update_data(set_count=set_count)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Задать вопрос", callback_data="ask_question")],
        [InlineKeyboardButton(text="🏁 Завершить тренировку", callback_data="end_workout")]
    ])
    
    if set_type == 'strength':
        response = f"✅ {current_exercise} — Подход {set_count}: {weight} кг x {reps} раз"
    elif set_type == 'cardio':
        mins = duration // 60
        response = f"✅ {current_exercise}: {mins} минут"
    else:
        response = f"✅ {current_exercise}: {duration} секунд"
    
    await message.answer(response, reply_markup=keyboard)

@dp.callback_query(F.data == "end_workout")
async def end_workout(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    
    if 'workout_id' not in data:
        await callback.message.answer("❌ Тренировка не начата.")
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
        f"💪 Общий тоннаж: {tonnage_str} кг"
    )
    await callback.answer()

@dp.callback_query(F.data == "stats")
@dp.message(Command("stats"))
async def cmd_stats(event: types.Message | types.CallbackQuery):
    message = event.message if isinstance(event, types.CallbackQuery) else event
    user_id = message.chat.id
    
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    
    c.execute('''SELECT COUNT(*), SUM(duration_minutes)
                 FROM workouts WHERE user_id = ?''', (user_id,))
    total_workouts, total_minutes = c.fetchone()
    
    c.execute('''SELECT e.exercise_name, MAX(s.weight_kg)
                 FROM workouts w
                 JOIN exercises e ON w.workout_id = e.workout_id
                 JOIN sets s ON e.id = s.exercise_id
                 WHERE w.user_id = ? AND s.set_type = 'strength'
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
    
    await message.answer(stats)
    
    if isinstance(event, types.CallbackQuery):
        await event.answer()

@dp.callback_query(F.data == "history")
@dp.message(Command("history"))
async def cmd_history(event: types.Message | types.CallbackQuery):
    message = event.message if isinstance(event, types.CallbackQuery) else event
    user_id = message.chat.id
    
    # Определяем начало недели (понедельник)
    today = datetime.now().date()
    start_of_week = today - timedelta(days=today.weekday())
    end_of_week = start_of_week + timedelta(days=6)
    
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    
    c.execute('''SELECT w.workout_id, w.start_time, w.duration_minutes
                 FROM workouts w
                 WHERE w.user_id = ? AND date(w.start_time) >= ? AND date(w.start_time) <= ?
                 ORDER BY w.start_time ASC''', 
              (user_id, start_of_week.isoformat(), end_of_week.isoformat()))
    
    workouts = c.fetchall()
    
    if not workouts:
        await message.answer("У тебя пока нет тренировок на этой неделе.")
        conn.close()
        if isinstance(event, types.CallbackQuery):
            await event.answer()
        return
    
    history = f"📅 Тренировки за неделю ({start_of_week.strftime('%d.%m')} - {end_of_week.strftime('%d.%m.%Y')}):\n\n"
    
    for idx, (workout_id, start_time, duration) in enumerate(workouts, 1):
        dt = datetime.fromisoformat(start_time)
        weekday = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье'][dt.weekday()]
        
        history += f"Тренировка {idx} — {weekday} {dt.strftime('%d.%m')}\n"
        
        # Получаем упражнения
        c.execute('''SELECT e.exercise_name, s.weight_kg, s.reps, s.duration_seconds, s.set_type
                     FROM exercises e
                     JOIN sets s ON e.id = s.exercise_id
                     WHERE e.workout_id = ?
                     ORDER BY e.id, s.id''', (workout_id,))
        
        exercises = c.fetchall()
        
        # Группируем по упражнениям
        current_ex = None
        weights = []
        reps_list = []
        
        for ex_name, weight, reps, duration, set_type in exercises:
            if ex_name != current_ex:
                if current_ex:
                    if set_type == 'strength':
                        history += f"   Веса: {' → '.join(map(str, weights))} кг\n"
                        history += f"   Повторы: {' → '.join(map(str, reps_list))}\n"
                weights = []
                reps_list = []
                current_ex = ex_name
                
                if set_type == 'strength':
                    history += f"💪 {ex_name}\n"
                elif set_type == 'cardio':
                    history += f"🏃 {ex_name} — {duration//60} минут\n"
                else:
                    history += f"⏱ {ex_name} — {duration} секунд\n"
            
            if set_type == 'strength':
                weights.append(int(weight))
                reps_list.append(reps)
        
        # Последнее упражнение
        if weights:
            history += f"   Веса: {' → '.join(map(str, weights))} кг\n"
            history += f"   Повторы: {' → '.join(map(str, reps_list))}\n"
        
        history += "\n"
    
    conn.close()
    
    await message.answer(history)
    
    if isinstance(event, types.CallbackQuery):
        await event.answer()

@dp.callback_query(F.data == "help")
@dp.message(Command("help"))
async def cmd_help(event: types.Message | types.CallbackQuery):
    message = event.message if isinstance(event, types.CallbackQuery) else event
    
    help_text = (
        "ℹ️ Как пользоваться ботом:\n\n"
        "🏋️ Начать тренировку:\n"
        "/start_workout или кнопка\n\n"
        "📝 Вводить упражнения:\n"
        "• Жим лежа 80-10 (упражнение вес-повторы)\n"
        "• 80-8 (следующий подход того же упражнения)\n"
        "• Бег 5 минут (кардио)\n"
        "• Планка 60 секунд (статика)\n\n"
        "📊 /stats — статистика за всё время + рекорды\n"
        "📅 /history — тренировки за текущую неделю\n"
        "🗑 /delete — удалить последнюю запись\n"
        "📢 /feedback — написать разработчику\n\n"
        "💬 Вопросы к ИИ:\n"
        "Просто напиши вопрос в чат или нажми кнопку 💬 во время тренировки"
    )
    
    await message.answer(help_text)
    
    if isinstance(event, types.CallbackQuery):
        await event.answer()

@dp.message(Command("delete"))
async def cmd_delete(message: types.Message):
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    
    # Находим последний подход пользователя
    c.execute('''SELECT s.id FROM sets s
                 JOIN exercises e ON s.exercise_id = e.id
                 JOIN workouts w ON e.workout_id = w.workout_id
                 WHERE w.user_id = ?
                 ORDER BY s.timestamp DESC LIMIT 1''', (message.from_user.id,))
    
    result = c.fetchone()
    
    if result:
        c.execute('DELETE FROM sets WHERE id = ?', (result[0],))
        conn.commit()
        await message.answer("✅ Последняя запись удалена")
    else:
        await message.answer("❌ Нет записей для удаления")
    
    conn.close()

@dp.message(Command("feedback"))
async def cmd_feedback(message: types.Message):
    text = message.text.replace('/feedback', '').strip()
    
    if not text:
        await message.answer("Напиши: /feedback текст сообщения")
        return
    
    try:
        await bot.send_message(
            ADMIN_ID,
            f"📢 Feedback от пользователя {message.from_user.id}:\n"
            f"Имя: {message.from_user.full_name}\n"
            f"Username: @{message.from_user.username}\n\n"
            f"{text}"
        )
        await message.answer("✅ Сообщение отправлено 
