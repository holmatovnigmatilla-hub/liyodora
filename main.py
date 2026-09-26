import asyncio
import logging
import os
import sys
from datetime import datetime

import aiosqlite
from aiohttp import web
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

# ---------------------------------------------------------------------------
# 1. КОНФИГУРАЦИЯ
# ---------------------------------------------------------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "8968626105:AAFKGmGeQE0WZZsOy_44g9BxqymAGWf2Lho")
ADMIN_ID = int(os.getenv("ADMIN_ID", "6450299048"))
PAYMENT_TOKEN = os.getenv("PAYMENT_TOKEN", "")  # Токен ЮKassa/Stripe из BotFather (для боевых оплат)
DB_NAME = "shop.db"

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DUMMY WEB SERVER (HEALTH CHECK FOR RENDER WEB SERVICE)
# ---------------------------------------------------------------------------
async def handle_health_check(request):
    return web.Response(text="Liyodora Bot is running successfully!")

async def start_health_check_server():
    app = web.Application()
    app.router.add_get("/", handle_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Health check server running on port {port}")

# ---------------------------------------------------------------------------
# 2. БАЗА ДАННЫХ (aiosqlite)
# ---------------------------------------------------------------------------
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code_word TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                price INTEGER NOT NULL,
                sizes TEXT NOT NULL,
                photo_id TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                address TEXT NOT NULL,
                product_id INTEGER NOT NULL,
                size TEXT NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await db.commit()

async def get_product_by_code(code: str):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM products WHERE LOWER(code_word) = LOWER(?)", (code.strip(),))
        return await cursor.fetchone()

async def get_product_by_id(product_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM products WHERE id = ?", (product_id,))
        return await cursor.fetchone()

async def get_all_products():
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM products ORDER BY id DESC")
        return await cursor.fetchall()

async def add_product(code: str, title: str, desc: str, price: int, sizes: str, photo_id: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            INSERT INTO products (code_word, title, description, price, sizes, photo_id)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (code.strip().lower(), title, desc, price, sizes, photo_id))
        await db.commit()

async def delete_product(product_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM products WHERE id = ?", (product_id,))
        await db.commit()

async def create_order(user_id: int, username: str, full_name: str, phone: str, address: str, product_id: int, size: str, amount: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("""
            INSERT INTO orders (user_id, username, full_name, phone, address, product_id, size, amount, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PAID', ?)
        """, (user_id, username or "", full_name, phone, address, product_id, size, amount, datetime.utcnow().isoformat()))
        await db.commit()
        return cursor.lastrowid

# ---------------------------------------------------------------------------
# 3. FSM СОСТОЯНИЯ
# ---------------------------------------------------------------------------
class OrderFSM(StatesGroup):
    waiting_for_code = State()
    waiting_for_name = State()
    waiting_for_phone = State()
    waiting_for_address = State()

class AdminAddProductFSM(StatesGroup):
    waiting_for_code = State()
    waiting_for_title = State()
    waiting_for_desc = State()
    waiting_for_price = State()
    waiting_for_sizes = State()
    waiting_for_photo = State()

# ---------------------------------------------------------------------------
# 4. КЛАВИАТУРЫ
# ---------------------------------------------------------------------------
def admin_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить товар", callback_data="admin_add_product")],
        [InlineKeyboardButton(text="📋 Список товаров", callback_data="admin_list_products")],
    ])

def product_sizes_keyboard(product_id: int, sizes_str: str):
    sizes = [s.strip() for s in sizes_str.split(",") if s.strip()]
    buttons = [
        InlineKeyboardButton(text=f"Размер {size}", callback_data=f"buy:{product_id}:{size}")
        for size in sizes
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def phone_request_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить мой номер телефона", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

# ---------------------------------------------------------------------------
# 5. ХЕНДЛЕРЫ КЛИЕНТА
# ---------------------------------------------------------------------------
client_router = Router()

@client_router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(OrderFSM.waiting_for_code)
    await message.answer(
        "👋 <b>Добро пожаловать в магазин Liyodora!</b>\n\n"
        "Введите <b>кодовое слово</b> из нашего видео в Instagram/Reels, чтобы найти нужную модель:",
        reply_markup=ReplyKeyboardRemove()
    )

@client_router.message(OrderFSM.waiting_for_code, F.text)
async def process_code_search(message: Message, state: FSMContext):
    code = message.text.strip()
    product = await get_product_by_code(code)

    if not product:
        await message.answer(
            f"❌ Товар по кодовому слову «<b>{code}</b>» не найден.\n"
            "Пожалуйста, проверьте правильность написания и попробуйте еще раз:"
        )
        return

    caption = (
        f"👗 <b>{product['title']}</b>\n\n"
        f"{product['description']}\n\n"
        f"💰 <b>Цена:</b> {product['price']} руб.\n"
        f"🏷 <b>Выберите ваш размер:</b>"
    )
    keyboard = product_sizes_keyboard(product['id'], product['sizes'])

    if product['photo_id']:
        await message.answer_photo(photo=product['photo_id'], caption=caption, reply_markup=keyboard)
    else:
        await message.answer(text=caption, reply_markup=keyboard)

@client_router.callback_query(F.data.startswith("buy:"))
async def on_size_selected(callback: CallbackQuery, state: FSMContext):
    _, product_id_str, size = callback.data.split(":")
    product_id = int(product_id_str)
    product = await get_product_by_id(product_id)

    if not product:
        await callback.answer("Товар не найден", show_alert=True)
        return

    await state.update_data(product_id=product_id, size=size, price=product['price'], title=product['title'])
    await state.set_state(OrderFSM.waiting_for_name)

    await callback.message.answer(
        f"✅ Вы выбрали: <b>{product['title']}</b> (Размер: <b>{size}</b>)\n\n"
        "Для оформления доставки введите ваши <b>Фамилию и Имя</b> получателя:"
    )
    await callback.answer()

@client_router.message(OrderFSM.waiting_for_name, F.text)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(full_name=message.text.strip())
    await state.set_state(OrderFSM.waiting_for_phone)
    await message.answer(
        "📞 Укажите ваш <b>номер телефона</b> для связи (или нажмите кнопку ниже):",
        reply_markup=phone_request_keyboard()
    )

@client_router.message(OrderFSM.waiting_for_phone)
async def process_phone(message: Message, state: FSMContext):
    phone = message.contact.phone_number if message.contact else (message.text or "").strip()
    if not phone or len(phone) < 7:
        await message.answer("Пожалуйста, укажите корректный номер телефона:")
        return

    await state.update_data(phone=phone)
    await state.set_state(OrderFSM.waiting_for_address)
    await message.answer(
        "📍 Введите <b>адрес доставки</b> или удобный <b>пункт выдачи (СДЭК, Яндекс Маркет, Почта)</b>:",
        reply_markup=ReplyKeyboardRemove()
    )

@client_router.message(OrderFSM.waiting_for_address, F.text)
async def process_address(message: Message, state: FSMContext, bot: Bot):
    address = message.text.strip()
    await state.update_data(address=address)
    data = await state.get_data()

    price = data['price']
    title = data['title']
    size = data['size']

    summary = (
        f"🛍 <b>Подтверждение заказа</b>\n\n"
        f"• Товар: <b>{title}</b>\n"
        f"• Размер: <b>{size}</b>\n"
        f"• Получатель: <b>{data['full_name']}</b>\n"
        f"• Телефон: <b>{data['phone']}</b>\n"
        f"• Доставка: <b>{address}</b>\n"
        f"• Итого к оплате: <b>{price} руб.</b>\n\n"
    )

    if PAYMENT_TOKEN:
        await message.answer(summary + "💳 Нажмите кнопку ниже для безопасной онлайн-оплаты:")
        await bot.send_invoice(
            chat_id=message.chat.id,
            title=f"Оплата: {title}",
            description=f"Размер: {size}, Доставка: {address}",
            payload=f"order_{data['product_id']}_{size}",
            provider_token=PAYMENT_TOKEN,
            currency="RUB",
            prices=[LabeledPrice(label=f"{title} ({size})", amount=int(price * 100))],
            start_parameter="pay-order"
        )
    else:
        order_id = await create_order(
            user_id=message.from_user.id,
            username=message.from_user.username or "",
            full_name=data['full_name'],
            phone=data['phone'],
            address=address,
            product_id=data['product_id'],
            size=size,
            amount=price
        )
        await message.answer(
            summary +
            "🎉 <b>Заказ успешно оформлен!</b>\n"
            "Наш менеджер свяжется с вами для подтверждения доставки."
        )
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"🚨 <b>Новый заказ #{order_id}!</b>\n\n"
                f"👤 Клиент: {data['full_name']} (@{message.from_user.username or 'нет'})\n"
                f"📞 Телефон: {data['phone']}\n"
                f"📍 Адрес: {address}\n"
                f"👗 Товар: {title} (ID: {data['product_id']})\n"
                f"📏 Размер: {size}\n"
                f"💰 Сумма: {price} руб."
            )
        )
        await state.clear()

@client_router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)

@client_router.message(F.successful_payment)
async def process_successful_payment(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    amount = message.successful_payment.total_amount // 100

    order_id = await create_order(
        user_id=message.from_user.id,
        username=message.from_user.username or "",
        full_name=data.get('full_name', 'Не указано'),
        phone=data.get('phone', 'Не указано'),
        address=data.get('address', 'Не указано'),
        product_id=data.get('product_id', 0),
        size=data.get('size', '—'),
        amount=amount
    )

    await message.answer(
        "🎉 <b>Оплата успешно получена!</b>\n\n"
        f"Номер вашего заказа: <b>#{order_id}</b>\n"
        "Ваш товар уже готовится к отправке. Спасибо за покупку в Liyodora!"
    )

    await bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"💰 <b>ОПЛАЧЕН НОВЫЙ ЗАКАЗ #{order_id}!</b>\n\n"
            f"👤 Покупатель: {data.get('full_name')} (@{message.from_user.username or 'нет'})\n"
            f"📞 Телефон: {data.get('phone')}\n"
            f"📍 Адрес доставки: {data.get('address')}\n"
            f"👗 Товар: {data.get('title')} (Размер: {data.get('size')})\n"
            f"💳 Оплачено: {amount} руб."
        )
    )
    await state.clear()

# ---------------------------------------------------------------------------
# 6. ХЕНДЛЕРЫ АДМИНИСТРАТОРА
# ---------------------------------------------------------------------------
admin_router = Router()

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

@admin_router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer("👑 <b>Панель администратора Liyodora</b>", reply_markup=admin_menu_keyboard())

@admin_router.callback_query(F.data == "admin_add_product")
async def start_add_product(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(AdminAddProductFSM.waiting_for_code)
    await callback.message.answer(
        "Шаг 1/6: Введите <b>кодовое слово</b> (пароль из Reels/Stories, например: <code>dress01</code>):"
    )
    await callback.answer()

@admin_router.message(AdminAddProductFSM.waiting_for_code, F.text)
async def add_product_code(message: Message, state: FSMContext):
    await state.update_data(code=message.text.strip().lower())
    await state.set_state(AdminAddProductFSM.waiting_for_title)
    await message.answer("Шаг 2/6: Введите <b>название товара</b>:")

@admin_router.message(AdminAddProductFSM.waiting_for_title, F.text)
async def add_product_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(AdminAddProductFSM.waiting_for_desc)
    await message.answer("Шаг 3/6: Введите <b>описание товара</b>:")

@admin_router.message(AdminAddProductFSM.waiting_for_desc, F.text)
async def add_product_desc(message: Message, state: FSMContext):
    await state.update_data(desc=message.text.strip())
    await state.set_state(AdminAddProductFSM.waiting_for_price)
    await message.answer("Шаг 4/6: Введите <b>цену</b> в рублях (целое число, например: <code>3500</code>):")

@admin_router.message(AdminAddProductFSM.waiting_for_price, F.text)
async def add_product_price(message: Message, state: FSMContext):
    try:
        price = int(message.text.strip())
    except ValueError:
        await message.answer("Пожалуйста, введите корректное число для цены:")
        return
    await state.update_data(price=price)
    await state.set_state(AdminAddProductFSM.waiting_for_sizes)
    await message.answer("Шаг 5/6: Введите <b>доступные размеры через запятую</b> (например: <code>XS, S, M, L</code>):")

@admin_router.message(AdminAddProductFSM.waiting_for_sizes, F.text)
async def add_product_sizes(message: Message, state: FSMContext):
    await state.update_data(sizes=message.text.strip())
    await state.set_state(AdminAddProductFSM.waiting_for_photo)
    await message.answer("Шаг 6/6: Отправьте <b>фотографию товара</b>:")

@admin_router.message(AdminAddProductFSM.waiting_for_photo, F.photo)
async def add_product_photo(message: Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    data = await state.get_data()

    await add_product(
        code=data['code'],
        title=data['title'],
        desc=data['desc'],
        price=data['price'],
        sizes=data['sizes'],
        photo_id=photo_id
    )
    await message.answer(
        f"✅ <b>Товар успешно добавлен!</b>\n\n"
        f"• Кодовое слово: <code>{data['code']}</code>\n"
        f"• Название: {data['title']}\n"
        f"• Цена: {data['price']} руб.\n"
        f"• Размеры: {data['sizes']}",
        reply_markup=admin_menu_keyboard()
    )
    await state.clear()

@admin_router.callback_query(F.data == "admin_list_products")
async def list_products(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    products = await get_all_products()
    if not products:
        await callback.message.answer("В каталоге пока нет товаров.", reply_markup=admin_menu_keyboard())
        await callback.answer()
        return

    for p in products:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Удалить товар", callback_data=f"del_prod:{p['id']}")]
        ])
        text = (
            f"🏷 <b>{p['title']}</b> (Код: <code>{p['code_word']}</code>)\n"
            f"Цена: {p['price']} руб. | Размеры: {p['sizes']}"
        )
        if p['photo_id']:
            await callback.message.answer_photo(photo=p['photo_id'], caption=text, reply_markup=kb)
        else:
            await callback.message.answer(text=text, reply_markup=kb)

    await callback.answer()

@admin_router.callback_query(F.data.startswith("del_prod:"))
async def on_delete_product(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    product_id = int(callback.data.split(":")[1])
    await delete_product(product_id)
    await callback.message.answer("🗑 Товар удален.", reply_markup=admin_menu_keyboard())
    await callback.answer()

# ---------------------------------------------------------------------------
# 7. ГЛАВНАЯ ТОЧКА ВХОДА
# ---------------------------------------------------------------------------
async def main():
    await init_db()
    
    # Запускаем фоновый веб-сервер для проходимости проверки Render
    await start_health_check_server()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    dp.include_router(admin_router)
    dp.include_router(client_router)

    logger.info("Бот Liyodora запускается...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
