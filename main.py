import asyncio
import logging
import os
import re
import urllib.parse
import requests
import warnings
from datetime import datetime
from bs4 import BeautifulSoup
from sqlalchemy import Column, Integer, String, Text, DateTime, select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
from groq import AsyncGroq
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
warnings.filterwarnings('ignore')

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = "openai/gpt-oss-20b"
MAX_TOKENS = 2000
DATABASE_URL = "sqlite+aiosqlite:///court_bot.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

Base = declarative_base()

class SearchHistory(Base):
    __tablename__ = "search_history"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, index=True)
    query = Column(String(500))
    query_type = Column(String(50))
    result_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("База даних готова")

async def save_history(user_id, query, query_type):
    async with AsyncSessionLocal() as db:
        h = SearchHistory(user_id=user_id, query=query, query_type=query_type, result_count=1)
        db.add(h)
        await db.commit()

async def get_history(user_id, limit=10):
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(SearchHistory)
            .where(SearchHistory.user_id == user_id)
            .order_by(SearchHistory.created_at.desc())
            .limit(limit)
        )
        return result.scalars().all()

class ReyestrParser:
    BASE_URL = 'https://reyestr.court.gov.ua'
    HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept-Language': 'uk-UA,uk;q=0.9',
        'Referer': 'https://reyestr.court.gov.ua/',
    }

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(self.HEADERS)
        try:
            self.session.get(self.BASE_URL, verify=False, timeout=15)
        except Exception:
            pass

    def get_decision(self, decision_id):
        try:
            r = self.session.get(
                self.BASE_URL + '/Review/' + str(decision_id),
                verify=False,
                timeout=15
            )
            raw_html = r.text
            start = raw_html.find('<!DOCTYPE HTML')
            if start == -1:
                start = raw_html.find('<!DOCTYPE html')

            decision_text = ''
            if start != -1:
                inner_html = raw_html[start:]
                inner_soup = BeautifulSoup(inner_html, 'lxml')
                raw_text = inner_soup.get_text(separator='\n', strip=True)
                lines = [l.strip() for l in raw_text.split('\n') if l.strip() and len(l.strip()) > 2]
                decision_text = '\n'.join(lines)

            meta_part = raw_html[:start] if start != -1 else raw_html
            main_soup = BeautifulSoup(meta_part, 'lxml')
            meta_text = main_soup.get_text(separator=' ', strip=True)

            case_number = 'Невідомо'
            for pattern in [r'справа\s*№\s*([\d/]+)', r'([\d]{3,4}/[\d]{4,6}/[\d]{2,4})']:
                match = re.search(pattern, meta_text)
                if match:
                    case_number = match.group(1)
                    break

            date = 'Невідомо'
            match = re.search(r'(\d{2}\s+\w+\s+\d{4}\s+року)', decision_text)
            if match:
                date = match.group(1)

            court = 'Невідомо'
            if 'Верховний Суд' in decision_text:
                court = 'Верховний Суд України'
            elif 'апеляційний' in decision_text.lower():
                court = 'Апеляційний суд'
            elif 'окружний' in decision_text.lower():
                court = 'Окружний суд'
            elif 'районний' in decision_text.lower():
                court = 'Районний суд'

            return {
                'id': decision_id,
                'case_number': case_number,
                'date': date,
                'court': court,
                'text': decision_text[:8000],
                'url': self.BASE_URL + '/Review/' + str(decision_id)
            }
        except Exception as e:
            logger.error('Помилка парсера: ' + str(e))
            return None

chat_histories = {}
parser = ReyestrParser()

class CourtAnalyzer:
    def __init__(self):
        self.client = AsyncGroq(api_key=GROQ_API_KEY)

    def add_message(self, user_id, role, content):
        if user_id not in chat_histories:
            chat_histories[user_id] = []
        chat_histories[user_id].append({"role": role, "content": content})
        if len(chat_histories[user_id]) > 10:
            chat_histories[user_id] = chat_histories[user_id][-10:]

    def clear_history(self, user_id):
        chat_histories[user_id] = []

    def extract_case_id(self, text):
        match = re.search(r'reyestr\.court\.gov\.ua/Review/(\d+)', text)
        if match:
            return match.group(1)
        match = re.search(r'/Review/(\d+)', text)
        if match:
            return match.group(1)
        if text.strip().isdigit() and len(text.strip()) > 5:
            return text.strip()
        return None

    def is_case_number(self, text):
        patterns = [r'\d{3,4}/\d{4,6}/\d{2,4}', r'\d+а-\d+/\d{2}']
        return any(re.search(p, text) for p in patterns)

    async def analyze_with_decision(self, user_id, message, decision):
        prompt = (
            "Користувач запитує: " + message + "\n\n"
            "Знайдено судове рішення:\n"
            "Справа: " + decision['case_number'] + "\n"
            "Суд: " + decision['court'] + "\n"
            "Дата: " + decision['date'] + "\n\n"
            "Текст рішення:\n" + decision['text'][:4000] + "\n\n"
            "Надай детальний структурований аналіз:\n"
            "1. СУТЬ СПРАВИ\n"
            "2. ПОЗИЦІЯ СУДУ\n"
            "3. ПРАВОВІ ПІДСТАВИ\n"
            "4. РЕЗУЛЬТАТ\n"
            "5. ПРАКТИЧНЕ ЗНАЧЕННЯ\n"
            "6. ВИСНОВОК ДЛЯ КОРИСТУВАЧА"
        )
        self.add_message(user_id, "user", prompt)
        messages = [
            {"role": "system", "content": "Ти експерт з українського права. Аналізуй судові рішення детально українською мовою."}
        ]
        messages.extend(chat_histories.get(user_id, []))
        try:
            response = await self.client.chat.completions.create(
                model=GROQ_MODEL,
                messages=messages,
                temperature=0.2,
                max_tokens=MAX_TOKENS
            )
            answer = response.choices[0].message.content
            self.add_message(user_id, "assistant", answer)
            return answer
        except Exception as e:
            return "Помилка аналізу: " + str(e)

    async def chat(self, user_id, message):
        if self.is_case_number(message):
            encoded = message.strip().replace('/', '%2F')
            search_url = 'https://reyestr.court.gov.ua/Search?q=' + encoded
            prompt = (
                "Користувач шукає судову справу номер: " + message + "\n\n"
                "Надай детальний аналіз:\n"
                "1. СТРУКТУРА НОМЕРА: поясни кожну частину\n"
                "2. ТИП СПРАВИ: визнач юрисдикцію та категорію\n"
                "3. ТИПОВА ПРАКТИКА: як суди вирішують такі справи\n"
                "4. МОЖЛИВИЙ РЕЗУЛЬТАТ: що зазвичай вирішує суд\n"
                "5. ЯК ЗНАЙТИ: " + search_url
            )
        else:
            prompt = message

        self.add_message(user_id, "user", prompt)
        messages = [
            {"role": "system", "content": "Ти експерт з українського права та судової практики України. Відповідай детально та структуровано українською мовою. Посилайся на конкретні статті законів України."}
        ]
        messages.extend(chat_histories.get(user_id, []))
        try:
            response = await self.client.chat.completions.create(
                model=GROQ_MODEL,
                messages=messages,
                temperature=0.2,
                max_tokens=MAX_TOKENS
            )
            answer = response.choices[0].message.content
            self.add_message(user_id, "assistant", answer)
            return answer
        except Exception as e:
            return "Помилка: " + str(e)

def main_menu():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📚 Реєстр судових рішень", url="https://reyestr.court.gov.ua"))
    builder.row(InlineKeyboardButton(text="🗑 Очистити історію", callback_data="clear_history"))
    builder.row(InlineKeyboardButton(text="📋 Історія пошуків", callback_data="history"))
    builder.row(InlineKeyboardButton(text="❓ Допомога", callback_data="help"))
    return builder.as_markup()

def search_keyboard(query="", decision_url=""):
    builder = InlineKeyboardBuilder()
    if decision_url:
        builder.row(InlineKeyboardButton(text="📄 Відкрити рішення", url=decision_url))
    if query:
        encoded = urllib.parse.quote(query)
        builder.row(InlineKeyboardButton(text="🔍 Шукати в реєстрі", url="https://reyestr.court.gov.ua/Search?q=" + encoded))
    builder.row(InlineKeyboardButton(text="📚 Реєстр судових рішень", url="https://reyestr.court.gov.ua"))
    builder.row(InlineKeyboardButton(text="🗑 Очистити чат", callback_data="clear_history"))
    return builder.as_markup()

router = Router()
analyzer = CourtAnalyzer()

WELCOME = (
    "Вітаю! Я бот аналізу судових рішень України\n\n"
    "Просто напишіть мені:\n"
    "🔢 Номер справи: 826/1234/21\n"
    "🔗 Посилання: reyestr.court.gov.ua/Review/114972508\n"
    "🔍 Правове питання: ст. 120 ПКУ\n"
    "💬 Будь-яке юридичне питання\n\n"
    "Я памятаю контекст нашої розмови!\n\n"
    "Команди:\n"
    "/clear - очистити історію чату\n"
    "/help - допомога"
)

@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(WELCOME, reply_markup=main_menu())

@router.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "ДОВІДКА\n\nПросто пишіть питання в чат!\n\n"
        "Приклади:\n"
        "826/1234/21 - аналіз справи\n"
        "reyestr.court.gov.ua/Review/114972508\n"
        "ст. 120 ПКУ - аналіз норми\n\n"
        "Бот памятає контекст розмови!",
        reply_markup=main_menu()
    )

@router.message(Command("clear"))
async def clear_cmd(message: Message):
    analyzer.clear_history(message.from_user.id)
    await message.answer("Історію чату очищено!")

@router.callback_query(F.data == "clear_history")
async def clear_history_cb(callback: CallbackQuery):
    analyzer.clear_history(callback.from_user.id)
    await callback.answer("Історію очищено!")
    await callback.message.edit_text(
        "Історію чату очищено!\n\nЗадайте нове питання:",
        reply_markup=main_menu()
    )

@router.callback_query(F.data == "history")
async def show_history(callback: CallbackQuery):
    history = await get_history(callback.from_user.id)
    if not history:
        await callback.answer("Історія порожня!")
        return
    lines = ["Ваша історія:\n"]
    for i, item in enumerate(history, 1):
        date_str = item.created_at.strftime("%d.%m.%Y %H:%M")
        lines.append(str(i) + ". " + item.query[:40])
        lines.append("   " + date_str)
    await callback.message.edit_text("\n".join(lines), reply_markup=main_menu())
    await callback.answer()

@router.callback_query(F.data == "help")
async def show_help(callback: CallbackQuery):
    await callback.message.edit_text(
        "ДОВІДКА\n\nПросто пишіть питання в чат!\n\n"
        "Приклади:\n"
        "826/1234/21 - аналіз справи\n"
        "reyestr.court.gov.ua/Review/114972508\n"
        "ст. 120 ПКУ - аналіз норми\n\n"
        "Бот памятає контекст розмови!",
        reply_markup=main_menu()
    )
    await callback.answer()

@router.message(F.text)
async def handle_message(message: Message, state: FSMContext):
    query = message.text.strip()
    user_id = message.from_user.id
    logger.info("Повідомлення від " + str(user_id) + ": " + query[:50])

    msg = await message.answer("Аналізую...")

    try:
        decision_url = ""
        decision_id = analyzer.extract_case_id(query)

        if analyzer.is_case_number(query) and not decision_id:
            encoded = query.strip().replace('/', '%2F')
            builder = InlineKeyboardBuilder()
            builder.row(InlineKeyboardButton(
                text="🔍 Знайти справу в реєстрі",
                url="https://reyestr.court.gov.ua/Search?q=" + encoded
            ))
            builder.row(InlineKeyboardButton(
                text="📚 Реєстр судових рішень",
                url="https://reyestr.court.gov.ua"
            ))
            builder.row(InlineKeyboardButton(
                text="🗑 Очистити чат",
                callback_data="clear_history"
            ))
            await msg.edit_text("Аналізую запит...")
            answer = await analyzer.chat(user_id, query)
            await msg.delete()
            if len(answer) > 3500:
                parts = [answer[i:i+3500] for i in range(0, len(answer), 3500)]
                for i, part in enumerate(parts):
                    if i == len(parts) - 1:
                        await message.answer(part, reply_markup=builder.as_markup())
                    else:
                        await message.answer(part)
            else:
                await message.answer(answer, reply_markup=builder.as_markup())
            await save_history(user_id, query, "number")
            return

        if decision_id:
            await msg.edit_text("Завантажую рішення з реєстру...")
            decision = parser.get_decision(decision_id)
            if decision:
                decision_url = decision['url']
                await msg.edit_text("Аналізую рішення...")
                analysis = await analyzer.analyze_with_decision(user_id, query, decision)
                answer = (
                    "Знайдено рішення:\n"
                    "Справа: " + decision['case_number'] + "\n"
                    "Суд: " + decision['court'] + "\n"
                    "Дата: " + decision['date'] + "\n\n"
                    + analysis
                )
            else:
                answer = await analyzer.chat(user_id, query)
        else:
            await msg.edit_text("Аналізую запит...")
            answer = await analyzer.chat(user_id, query)

        await msg.delete()

        if len(answer) > 3500:
            parts = [answer[i:i+3500] for i in range(0, len(answer), 3500)]
            for i, part in enumerate(parts):
                if i == len(parts) - 1:
                    await message.answer(part, reply_markup=search_keyboard(query, decision_url))
                else:
                    await message.answer(part)
        else:
            await message.answer(answer, reply_markup=search_keyboard(query, decision_url))

        await save_history(user_id, query, "chat")

    except Exception as e:
        logger.error("Помилка: " + str(e))
        try:
            await msg.edit_text("Виникла помилка. Спробуйте ще раз.")
        except Exception:
            await message.answer("Виникла помилка.")

async def main():
    logger.info("Запуск бота...")
    await init_db()
    bot = Bot(token=TELEGRAM_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("Бот готовий!")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
