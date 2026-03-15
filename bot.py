import asyncio
import logging
import os
import re
import sqlite3
import base64
import uuid
from datetime import datetime
from typing import Optional, Tuple

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
GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY")

# Инициализация бота
bot = Bot(token=TELEGRAM_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

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

# Улучшенный парсер упражнений
def parse_workout_input(text: str) -> Tuple[Optional[str], Optional[float], Optional[int], Optional[int], str]:
    """
    Парсит ввод упражнений в разных форматах:
    - "Жим лежа 80-10" -> (Жим лежа, 80, 10, None, 'strength')
    - "80-10" -> (None, 80, 10, None, 'strength')
    - "Бег 5 минут" -> (Бег, None, None, 300, 'cardio')
    - "Планка 60 секунд" -> (Планка, None, None, 60, 'static')
    
    Возвращает: (название, вес, повторы, секунды, тип)
    """
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

# GigaChat API (обновлённая авторизация)
async def get_gigachat_token():
    """Получает access token для GigaChat через Authorization key"""
    if not GIGACHAT_AUTH_KEY:
        logger.error("GIGACHAT_AUTH_KEY not set")
        return None
    
    url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Accept': 'application/json',
        'RqUID': str(uuid.uuid4()),
        'Authorization': f'Bearer {GIGACHAT_AUTH_KEY}'
    }
    data = {'scope': 'GIGACHAT_API_PERS'}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=data, ssl=False) as response:
                if response.status == 200:
                    result = await response.json()
                    return result.get('access_token')
                else:
                    error_text = await response.text()
                    logger.error(f"GigaChat auth error {response.status}: {error_text}")
    except Exception as e:
        logger.error(f"GigaChat token error: {e}")
    return None
    
async def ask_gigachat(user_id: int, question: str):
    """Отправляет запрос в GigaChat с историей пользователя"""
    try:
        # Получаем историю тренировок
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
        
        # Формируем контекст
        context = "История тренировок пользователя (последние записи):\n"
        for row in history[:20]:  # Ограничиваем для промпта
            date, exercise, weight, reps, duration, set_type = row
            if set_type == 'strength':
                context += f"{date}: {exercise} - {weight}кг x {reps} раз\n"
            elif set_type in ['cardio', 'static']:
                context += f"{date}: {exercise} - {duration} секунд\n"
        
        # Получаем токен
        token = await get_gigachat_token()
        if not token:
            return "⚠️ Ошибка подключения к GigaChat. Проверь настройки API ключа."
        
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
                    return f"⚠️ Ошибка GigaChat API: {response.status}"
                    
    except Exception as e:
        logger.error(f"GigaChat error: {e}")
        return "⚠️ Произошла ошибка при обращении к ИИ"

# Хэндлеры
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "💪 Привет! Я твой персональный тренер.\n\n"
        "🏋️ **Как записывать тренировки:**\n"
        "• Жим лежа 80-10 (упражнение вес-повторы)\n"
        "• Бег 5 минут\n"
        "• Планка 60 секунд\n\n"
        "📊 **Команды:**\n"
        "/start_workout — начать тренировку\n"
        "/ask — задать вопрос тренеру\n"
        "/stats — статистика\n"
        "/history — последние тренировки\n"
        "/help — помощь",
        parse_mode="Markdown"
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
    await state.set_state(WorkoutStates.entering_sets)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Задать вопрос", callback_data="ask_question")],
        [InlineKeyboardButton(text="🏁 Завершить тренировку", callback_data="end_workout")]
    ])
    
    await message.answer(
        "🏋️ **Тренировка начата!**\n"
        f"⏱ Время: {datetime.now().strftime('%H:%M')}\n\n"
        "Вводи упражнения в формате:\n"
        "• Жим лежа 80-10\n"
        "• Бег 5 минут\n"
        "• 80-10 (следующий подход)\n\n"
        "Или нажми 💬 чтобы задать вопрос",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "ask_question")
async def ask_question_callback(callback: types.CallbackQuery):
    await callback.message.answer(
        "💡 Задай свой вопрос следующим сообщением.\n"
        "Например: 'Чем заменить жим лежа?' или 'Стоит ли увеличить вес?'"
    )
    await callback.answer()

@dp.message(Command("ask"))
async def cmd_ask(message: types.Message):
    await message.answer("💡 Задай свой вопрос:")

@dp.message(WorkoutStates.entering_sets)
async def process_workout_entry(message: types.Message, state: FSMContext):
    # Проверяем не вопрос ли это (содержит вопросительные слова)
    question_words = ['как', 'что', 'чем', 'почему', 'когда', 'стоит', 'можно', 'нужно', 'заменить', 'посоветуй']
    if any(word in message.text.lower() for word in question_words) and len(message.text.split()) > 2:
        await message.answer("🤔 Думаю...")
        answer = await ask_gigachat(message.from_user.id, message.text)
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Ещё вопрос", callback_data="ask_question")],
            [InlineKeyboardButton(text="🏁 Завершить тренировку", callback_data="end_workout")]
        ])
        
        await message.answer(answer, reply_markup=keyboard, parse_mode="Markdown")
        return
    
    exercise_name, weight, reps, duration, set_type = parse_workout_input(message.text)
    
    # Если не распознали формат
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
    
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    
    # Если указано название упражнения — создаём новое
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
    # Иначе продолжаем текущее
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
    
    set_count = data.get('set_count', 0) + 1
    await state.update_data(set_count=set_count)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Задать вопрос", callback_data="ask_question")],
        [InlineKeyboardButton(text="🏁 Завершить тренировку", callback_data="end_workout")]
    ])
    
    # Формируем ответ
    if set_type == 'strength':
        response = f"✅ **{current_exercise}** — Подход {set_count}\n{weight} кг × {reps} раз"
    elif set_type == 'cardio':
        mins = duration // 60
        response = f"✅ **{current_exercise}**\n{mins} минут"
    elif set_type == 'static':
        response = f"✅ **{current_exercise}**\n{duration} секунд"
    
    await message.answer(response, reply_markup=keyboard, parse_mode="Markdown")

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
    c.execute('''SELECT COUNT(DISTINCT e.id), COUNT(s.id), 
                        SUM(CASE WHEN s.set_type = 'strength' THEN s.weight_kg * s.reps ELSE 0 END)
                 FROM exercises e
                 JOIN sets s ON e.id = s.exercise_id
                 WHERE e.workout_id = ?''', (workout_id,))
    exercises_count, sets_count, tonnage = c.fetchone()
    
    conn.commit()
    conn.close()
    
    await state.clear()
    
    await callback.message.answer(
        f"🏁 **Тренировка завершена!**\n"
        f"⏱ Длительность: {duration} минут\n"
        f"📊 Выполнено: {exercises_count} упражнений, {sets_count} подходов\n"
        f"💪 Общий тоннаж: {tonnage:.0f} кг",
        parse_mode="Markdown"
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
                 WHERE w.user_id = ? AND s.set_type = 'strength'
                 GROUP BY e.exercise_name
                 ORDER BY MAX(s.weight_kg) DESC
                 LIMIT 5''', (message.from_user.id,))
    records = c.fetchall()
    
    conn.close()
    
    stats = f"📊 **Твоя статистика:**\n\n"
    stats += f"Всего тренировок: **{total_workouts or 0}**\n"
    stats += f"Всего времени: **{total_minutes or 0}** минут\n\n"
    
    if records:
        stats += "🏆 **Рекорды по весам:**\n"
        for name, weight in records:
            stats += f"• {name}: **{weight} кг**\n"
    
    await message.answer(stats, parse_mode="Markdown")

@dp.message(Command("history"))
async def cmd_history(message: types.Message):
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    
    c.execute('''SELECT start_time, duration_minutes
                 FROM workouts
                 WHERE user_id = ?
                 ORDER BY start_time DESC
                 LIMIT 10''', (message.from_user.id,))
    workouts = c.fetchall()
    conn.close()
    
    if not workouts:
        await message.answer("У тебя пока нет тренировок. Начни с /start_workout!")
        return
    
    history = "📅 **Последние тренировки:**\n\n"
    for start, duration in workouts:
        dt = datetime.fromisoformat(start)
        history += f"• {dt.strftime('%d.%m.%Y %H:%M')} — {duration} мин\n"
    
    await message.answer(history, parse_mode="Markdown")

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "ℹ️ **Как пользоваться:**\n\n"
        "**1. Начни тренировку:**\n"
        "/start_workout\n\n"
        "**2. Вводи упражнения:**\n"
        "• Жим лежа 80-10\n"
        "• 80-8 (следующий подход)\n"
        "• Бег 5 минут\n\n"
        "**3. Задавай вопросы:**\n"
        "• Кнопка 💬 во время тренировки\n"
        "• Или команда /ask в любое время\n\n"
        "**4. Завершай:**\n"
        "Кнопка 🏁 Завершить",
        parse_mode="Markdown"
    )

@dp.message()
async def handle_question(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state:
        return  # В режиме тренировки обрабатываем отдельно
    
    await message.answer("🤔 Думаю...")
    answer = await ask_gigachat(message.from_user.id, message.text)
    await message.answer(answer, parse_mode="Markdown")

# Запуск бота
async def main():
    logger.info("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())


# Railway redeploy trigger
