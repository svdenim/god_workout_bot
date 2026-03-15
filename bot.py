import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime
from typing import Optional

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
import aiohttp

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Токены
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GIGACHAT_KEY = os.getenv("GIGACHAT_KEY")

# Инициализация бота
bot = Bot(token=TELEGRAM_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# FSM состояния
class WorkoutStates(StatesGroup):
    in_workout = State()
    entering_exercise = State()
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
                  timestamp TEXT)''')
    conn.commit()
    conn.close()

init_db()

# Парсер упражнений
def parse_workout_input(text: str):
    """Парсит ввод типа '80-10' или '80 10' или '80х10'"""
    patterns = [
        r'(\d+(?:\.\d+)?)\s*[-xх×*]\s*(\d+)',  # 80-10, 80х10, 80*10
        r'(\d+(?:\.\d+)?)\s+(\d+)',  # 80 10
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            weight = float(match.group(1))
            reps = int(match.group(2))
            return weight, reps
    return None, None

# GigaChat API
async def get_gigachat_token():
    """Получает access token для GigaChat"""
    url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Accept': 'application/json',
        'RqUID': str(datetime.now().timestamp()),
        'Authorization': f'Basic {GIGACHAT_KEY}'
    }
    data = {'scope': 'GIGACHAT_API_PERS'}
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, data=data, ssl=False) as response:
            if response.status == 200:
                result = await response.json()
                return result.get('access_token')
    return None

async def ask_gigachat(user_id: int, question: str):
    """Отправляет запрос в GigaChat с историей пользователя"""
    try:
        # Получаем историю тренировок
        conn = sqlite3.connect('workouts.db')
        c = conn.cursor()
        
        c.execute('''SELECT w.start_time, e.exercise_name, s.weight_kg, s.reps
                     FROM workouts w
                     JOIN exercises e ON w.workout_id = e.workout_id
                     JOIN sets s ON e.id = s.exercise_id
                     WHERE w.user_id = ?
                     ORDER BY w.start_time DESC
                     LIMIT 50''', (user_id,))
        
        history = c.fetchall()
        conn.close()
        
        # Формируем контекст
        context = "История тренировок пользователя:\n"
        for row in history:
            context += f"{row[0]}: {row[1]} - {row[2]}кг x {row[3]} раз\n"
        
        # Получаем токен
        token = await get_gigachat_token()
        if not token:
            return "Ошибка подключения к GigaChat"
        
        # Отправляем запрос
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
            "temperature": 0.7
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, ssl=False) as response:
                if response.status == 200:
                    result = await response.json()
                    return result['choices'][0]['message']['content']
                else:
                    return f"Ошибка GigaChat: {response.status}"
                    
    except Exception as e:
        logger.error(f"GigaChat error: {e}")
        return "Произошла ошибка при обращении к ИИ"

# Хэндлеры
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "💪 Привет! Я твой персональный тренер.\n\n"
        "Команды:\n"
        "/start_workout — начать тренировку\n"
        "/stats — статистика\n"
        "/history — последние тренировки\n"
        "/help — помощь\n\n"
        "Или просто задай вопрос — я дам совет!"
    )

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
    await state.set_state(WorkoutStates.entering_exercise)
    
    await message.answer(
        "🏋️ Тренировка начата!\n"
        f"⏱ Время: {datetime.now().strftime('%H:%M')}\n\n"
        "Напиши название первого упражнения:"
    )

@dp.message(WorkoutStates.entering_exercise)
async def process_exercise_name(message: types.Message, state: FSMContext):
    exercise_name = message.text.strip()
    
    # Сохраняем упражнение в БД
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    
    data = await state.get_data()
    workout_id = data['workout_id']
    
    c.execute('''INSERT INTO exercises (workout_id, exercise_name, timestamp)
                 VALUES (?, ?, ?)''',
              (workout_id, exercise_name, datetime.now().isoformat()))
    exercise_id = c.lastrowid
    conn.commit()
    conn.close()
    
    await state.update_data(
        current_exercise=exercise_name,
        current_exercise_id=exercise_id,
        set_count=0
    )
    await state.set_state(WorkoutStates.entering_sets)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➡️ Следующее упражнение", callback_data="next_exercise")],
        [InlineKeyboardButton(text="🏁 Завершить тренировку", callback_data="end_workout")]
    ])
    
    await message.answer(
        f"✅ Упражнение: {exercise_name}\n\n"
        "Вводи подходы в формате 'вес-повторы'\n"
        "Например: 80-10",
        reply_markup=keyboard
    )

@dp.message(WorkoutStates.entering_sets)
async def process_set(message: types.Message, state: FSMContext):
    weight, reps = parse_workout_input(message.text)
    
    if weight is None:
        await message.answer("❌ Не понял формат. Попробуй: 80-10 или 80х10")
        return
    
    # Сохраняем подход
    data = await state.get_data()
    exercise_id = data['current_exercise_id']
    
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('''INSERT INTO sets (exercise_id, weight_kg, reps, timestamp)
                 VALUES (?, ?, ?, ?)''',
              (exercise_id, weight, reps, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    
    set_count = data['set_count'] + 1
    await state.update_data(set_count=set_count)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➡️ Следующее упражнение", callback_data="next_exercise")],
        [InlineKeyboardButton(text="🏁 Завершить тренировку", callback_data="end_workout")]
    ])
    
    await message.answer(
        f"✅ Подход {set_count}: {weight} кг × {reps} раз",
        reply_markup=keyboard
    )

@dp.callback_query(F.data == "next_exercise")
async def next_exercise(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(WorkoutStates.entering_exercise)
    await callback.message.answer("Напиши название следующего упражнения:")
    await callback.answer()

@dp.callback_query(F.data == "end_workout")
async def end_workout(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    workout_id = data['workout_id']
    start_time = datetime.fromisoformat(data['start_time'])
    end_time = datetime.now()
    duration = int((end_time - start_time).total_seconds() / 60)
    
    # Сохраняем тренировку
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('''INSERT INTO workouts (user_id, workout_id, start_time, end_time, duration_minutes)
                 VALUES (?, ?, ?, ?, ?)''',
              (callback.from_user.id, workout_id, data['start_time'],
               end_time.isoformat(), duration))
    
    # Подсчёт статистики
    c.execute('''SELECT COUNT(DISTINCT e.id), COUNT(s.id), SUM(s.weight_kg * s.reps)
                 FROM exercises e
                 JOIN sets s ON e.id = s.exercise_id
                 WHERE e.workout_id = ?''', (workout_id,))
    exercises_count, sets_count, tonnage = c.fetchone()
    
    conn.commit()
    conn.close()
    
    await state.clear()
    
    await callback.message.answer(
        f"🏁 Тренировка завершена!\n"
        f"⏱ Длительность: {duration} минут\n"
        f"📊 Выполнено: {exercises_count} упражнений, {sets_count} подходов\n"
        f"💪 Общий тоннаж: {tonnage:.0f} кг"
    )
    await callback.answer()

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    
    c.execute('''SELECT COUNT(*), SUM(duration_minutes)
                 FROM workouts WHERE user_id = ?''', (message.from_user.id,))
    total_workouts, total_minutes = c.fetchone()
    
    c.execute('''SELECT e.exercise_name, MAX(s.weight_kg)
                 FROM workouts w
                 JOIN exercises e ON w.workout_id = e.workout_id
                 JOIN sets s ON e.id = s.exercise_id
                 WHERE w.user_id = ?
                 GROUP BY e.exercise_name
                 ORDER BY MAX(s.weight_kg) DESC
                 LIMIT 5''', (message.from_user.id,))
    records = c.fetchall()
    
    conn.close()
    
    stats = f"📊 Твоя статистика:\n\n"
    stats += f"Всего тренировок: {total_workouts or 0}\n"
    stats += f"Всего времени: {total_minutes or 0} минут\n\n"
    
    if records:
        stats += "🏆 Рекорды по весам:\n"
        for name, weight in records:
            stats += f"• {name}: {weight} кг\n"
    
    await message.answer(stats)

@dp.message(Command("history"))
async def cmd_history(message: types.Message):
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    
    c.execute('''SELECT start_time, duration_minutes
                 FROM workouts
                 WHERE user_id = ?
                 ORDER BY start_time DESC
                 LIMIT 5''', (message.from_user.id,))
    workouts = c.fetchall()
    conn.close()
    
    if not workouts:
        await message.answer("У тебя пока нет тренировок. Начни с /start_workout!")
        return
    
    history = "📅 Последние тренировки:\n\n"
    for start, duration in workouts:
        dt = datetime.fromisoformat(start)
        history += f"• {dt.strftime('%d.%m.%Y %H:%M')} — {duration} мин\n"
    
    await message.answer(history)

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "ℹ️ Как пользоваться:\n\n"
        "1. /start_workout — начать тренировку\n"
        "2. Введи название упражнения\n"
        "3. Вводи подходы: 80-10 (вес-повторы)\n"
        "4. Нажми кнопку для следующего упражнения\n"
        "5. Завершай кнопкой 'Завершить'\n\n"
        "Можешь задать любой вопрос про тренировки — я отвечу с учётом твоей истории!"
    )

@dp.message()
async def handle_question(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state:
        return  # Игнорируем, если в режиме тренировки
    
    await message.answer("🤔 Думаю...")
    answer = await ask_gigachat(message.from_user.id, message.text)
    await message.answer(answer)

# Запуск бота
async def main():
    logger.info("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())