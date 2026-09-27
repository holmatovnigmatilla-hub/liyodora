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
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    Message,
    ReplyKeyboardMarkup,
)

# ---------------------------------------------------------------------------
# 1. КОНФИГУРАЦИЯ
# ---------------------------------------------------------------------------
load_dotenv()

CLIENT_BOT_TOKEN = os.getenv("BOT_TOKEN", "8968626105:AAFKGmGeQE0WZZsOy_44g9BxqymAGWf2Lho")
ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN", "8669663581:AAFrwpkjedduLz1G4XjhKHrXcXpYVkP0FqY")
ADMIN_IDS = [6450299048, 8914196755]
PAYMENT_TOKEN = os.getenv("PAYMENT_TOKEN", "")
MANAGER_USERNAME = os.getenv("MANAGER_USERNAME", "liyodora_admin")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "shop.db")
PHOTOS_DIR = os.path.join(BASE_DIR, "photos")
os.makedirs(PHOTOS_DIR, exist_ok=True)

PAGE_SIZE = 5           # товаров/заказов на одной странице
TEXT_PAGE_LIMIT = 3000  # максимум символов в одном сообщении

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

client_bot = Bot(token=CLIENT_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
admin_bot = Bot(token=ADMIN_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


# ---------------------------------------------------------------------------
# HEALTH CHECK (Render / Railway)
# ---------------------------------------------------------------------------
async def handle_health_check(request):
    return web.Response(text="Liyodora Dual-Bot System is active!")


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
# 2. БАЗА ДАННЫХ
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
                photo_path TEXT NOT NULL
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


async def add_product(code: str, title: str, desc: str, price: int, sizes: str, photo_path: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            INSERT INTO products (code_word, title, description, price, sizes, photo_path)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (code.strip().lower(), title, desc, price, sizes, photo_path))
        await db.commit()


async def delete_product(product_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM products WHERE id = ?", (product_id,))
        await db.commit()


async def create_order(user_id: int, username: str, full_name: str, phone: str, address: str,
                       product_id: int, size: str, amount: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("""
            INSERT INTO orders (user_id, username, full_name, phone, address, product_id, size, amount, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PAID', ?)
        """, (user_id, username or "", full_name, phone, address, product_id, size, amount,
              datetime.utcnow().isoformat()))
        await db.commit()
        return cursor.lastrowid


async def update_order_status(order_id: int, status: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
        await db.commit()


async def get_order_by_id(order_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
        return await cursor.fetchone()


async def get_orders_by_user(user_id: int, limit: int = 200):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit))
        return await cursor.fetchall()


async def get_recent_orders(limit: int = 200):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))
        return await cursor.fetchall()


# ---------------------------------------------------------------------------
# 3. ПОСТОЯННЫЕ МЕНЮ НА МЕСТЕ КЛАВИАТУРЫ
# ---------------------------------------------------------------------------
# Клиент
BTN_CODE = "🔎 Найти по коду"
BTN_CATALOG = "🛍 Каталог"
BTN_SUPPORT = "💬 Поддержка"
BTN_MY_ORDERS = "📦 Мои заказы"
BTN_DELIVERY = "🚚 Доставка"
BTN_CANCEL = "❌ Отмена"

# Админ
BTN_ADD = "➕ Добавить товар"
BTN_PRODUCTS = "📋 Товары"
BTN_ORDERS = "🧾 Заказы"
BTN_STATS = "📊 Статистика"


def client_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_CODE), KeyboardButton(text=BTN_CATALOG)],
            [KeyboardButton(text=BTN_MY_ORDERS), KeyboardButton(text=BTN_DELIVERY)],
            [KeyboardButton(text=BTN_SUPPORT), KeyboardButton(text=BTN_CANCEL)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите кнопку в меню",
    )


def admin_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ADD), KeyboardButton(text=BTN_PRODUCTS)],
            [KeyboardButton(text=BTN_ORDERS), KeyboardButton(text=BTN_STATS)],
            [KeyboardButton(text=BTN_CANCEL)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Панель администратора",
    )


def phone_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Отправить мой номер", request_contact=True)],
            [KeyboardButton(text=BTN_CANCEL)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def product_sizes_keyboard(product_id: int, sizes_str: str) -> InlineKeyboardMarkup:
    sizes = [s.strip() for s in sizes_str.split(",") if s.strip()]
    buttons = [
        InlineKeyboardButton(text=f"Размер {size}", callback_data=f"buy:{product_id}:{size}")
        for size in sizes
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_order_actions_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚚 Отправлен", callback_data=f"order_status:{order_id}:SHIPPED"),
        InlineKeyboardButton(text="❌ Отменить", callback_data=f"order_status:{order_id}:CANCELLED"),
    ]])


# ---------------------------------------------------------------------------
# 4. ПАГИНАЦИЯ (страницы с кнопками ◀️ 1/N ▶️)
# ---------------------------------------------------------------------------
def total_pages(items_count: int, per_page: int = PAGE_SIZE) -> int:
    return max(1, (items_count + per_page - 1) // per_page)


def page_slice(items, page: int, per_page: int = PAGE_SIZE):
    start = page * per_page
    return items[start:start + per_page]


def nav_row(prefix: str, page: int, pages: int) -> list:
    """Строка кнопок перелистывания: ◀️  1/5  ▶️"""
    if pages <= 1:
        return []
    prev_page = (page - 1) % pages
    next_page = (page + 1) % pages
    return [
        InlineKeyboardButton(text="◀️", callback_data=f"{prefix}:{prev_page}"),
        InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"),
        InlineKeyboardButton(text="▶️", callback_data=f"{prefix}:{next_page}"),
    ]


def split_text_pages(text: str, limit: int = TEXT_PAGE_LIMIT) -> list:
    """Делит большой текст на страницы по строкам, не ломая разметку."""
    pages, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit and current:
            pages.append(current.rstrip())
            current = ""
        current += line + "\n"
    if current.strip():
        pages.append(current.rstrip())
    return pages or ["—"]


async def show_page(bot: Bot, state: FSMContext, chat_id: int, text: str,
                    keyboard: InlineKeyboardMarkup, message_to_edit: Message | None = None):
    """Показывает страницу: редактирует текущее сообщение или отправляет новое."""
    if message_to_edit is not None:
        try:
            await message_to_edit.edit_text(text, reply_markup=keyboard)
            return message_to_edit
        except Exception:
            pass
    msg = await bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)
    await track(state, msg.message_id)
    return msg


# ---------------------------------------------------------------------------
# 5. АНТИ-МУСОР: трекинг сообщений и удаление по кнопке «Отмена»
# ---------------------------------------------------------------------------
async def track(state: FSMContext, *message_ids: int):
    data = await state.get_data()
    trash = list(data.get("trash", []))
    trash.extend([m for m in message_ids if m])
    await state.update_data(trash=trash[-80:])


async def clear_trash(bot: Bot, state: FSMContext, chat_id: int):
    data = await state.get_data()
    for mid in data.get("trash", []):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass
    await state.update_data(trash=[])


async def do_cancel(bot: Bot, state: FSMContext, message: Message, menu: ReplyKeyboardMarkup):
    await clear_trash(bot, state, message.chat.id)
    try:
        await message.delete()
    except Exception:
        pass
    await state.clear()
    notice = await bot.send_message(chat_id=message.chat.id,
                                    text="🔄 Действие отменено. Меню ниже 👇", reply_markup=menu)
    await track(state, notice.message_id)


# ---------------------------------------------------------------------------
# 6. FSM
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


# ===========================================================================
# 7. КЛИЕНТСКИЙ БОТ
# ===========================================================================
client_router = Router()


@client_router.message(F.text == BTN_CANCEL)
@client_router.message(Command("cancel"))
async def client_cancel(message: Message, state: FSMContext):
    await do_cancel(client_bot, state, message, client_menu())


@client_router.message(CommandStart())
@client_router.message(Command("menu"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    msg = await message.answer(
        "👋 <b>Добро пожаловать в Liyodora!</b>\n\n"
        "Здесь ничего не нужно печатать — просто нажимайте кнопки в меню ниже 👇",
        reply_markup=client_menu(),
    )
    await track(state, msg.message_id)


@client_router.message(F.text == BTN_CODE)
async def btn_code(message: Message, state: FSMContext):
    await state.set_state(OrderFSM.waiting_for_code)
    msg = await message.answer("🔎 Отправьте <b>кодовое слово</b> товара из Reels/Instagram:",
                               reply_markup=client_menu())
    await track(state, message.message_id, msg.message_id)


@client_router.message(F.text == BTN_SUPPORT)
async def btn_support(message: Message, state: FSMContext):
    msg = await message.answer(
        "💬 <b>Поддержка</b>\n\n"
        f"Напишите нашему менеджеру: @{MANAGER_USERNAME}\n"
        "Отвечаем в рабочее время 09:00–20:00.\n\n"
        "Быстрые действия — кнопками в меню ниже 👇",
        reply_markup=client_menu(),
    )
    await track(state, message.message_id, msg.message_id)


@client_router.message(F.text == BTN_DELIVERY)
async def btn_delivery(message: Message, state: FSMContext):
    msg = await message.answer(
        "🚚 <b>Доставка</b>\n\n"
        "• Ташкент и все регионы Узбекистана\n"
        "• Службы: BTS, EMU, CDEK, Почта\n"
        "• Срок: 1–3 дня по Ташкенту, 2–5 дней по регионам\n"
        "• Оплата в сумах",
        reply_markup=client_menu(),
    )
    await track(state, message.message_id, msg.message_id)


# ------------------------- КАТАЛОГ С ПАГИНАЦИЕЙ ----------------------------
async def build_catalog_page(page: int):
    products = await get_all_products()
    if not products:
        return "Каталог пока пуст. Загляните позже 🙌", InlineKeyboardMarkup(inline_keyboard=[])

    pages = total_pages(len(products))
    page = max(0, min(page, pages - 1))
    chunk = page_slice(products, page)

    lines = [f"🛍 <b>Каталог</b> — страница {page + 1} из {pages}\n"]
    rows = []
    for p in chunk:
        lines.append(f"👗 <b>{p['title']}</b>\n💰 {p['price']:,} сум | Размеры: {p['sizes']}\n")
        rows.append([InlineKeyboardButton(text=f"👗 {p['title'][:28]}", callback_data=f"pcard:{p['id']}")])

    nav = nav_row("catalog", page, pages)
    if nav:
        rows.append(nav)
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@client_router.message(F.text == BTN_CATALOG)
async def btn_catalog(message: Message, state: FSMContext):
    await track(state, message.message_id)
    text, kb = await build_catalog_page(0)
    await show_page(client_bot, state, message.chat.id, text, kb)


@client_router.callback_query(F.data.startswith("catalog:"))
async def on_catalog_page(callback: CallbackQuery, state: FSMContext):
    page = int(callback.data.split(":")[1])
    text, kb = await build_catalog_page(page)
    await show_page(client_bot, state, callback.message.chat.id, text, kb, callback.message)
    await callback.answer()


@client_router.callback_query(F.data.startswith("pcard:"))
async def on_product_card(callback: CallbackQuery, state: FSMContext):
    product = await get_product_by_id(int(callback.data.split(":")[1]))
    if not product:
        await callback.answer("Товар не найден", show_alert=True)
        return
    await send_product_card(client_bot, state, callback.message.chat.id, product)
    await callback.answer()


async def send_product_card(bot: Bot, state: FSMContext, chat_id: int, product):
    caption = (
        f"👗 <b>{product['title']}</b>\n\n{product['description'] or ''}\n\n"
        f"💰 <b>Цена:</b> {product['price']:,} сум\n🏷 <b>Выберите ваш размер:</b>"
    )
    parts = split_text_pages(caption, 900)
    kb = product_sizes_keyboard(product['id'], product['sizes'])
    if product['photo_path'] and os.path.exists(product['photo_path']):
        msg = await bot.send_photo(chat_id, FSInputFile(product['photo_path']), caption=parts[0],
                                   reply_markup=kb if len(parts) == 1 else None)
    else:
        msg = await bot.send_message(chat_id, parts[0], reply_markup=kb if len(parts) == 1 else None)
    await track(state, msg.message_id)
    for i, extra in enumerate(parts[1:], start=1):
        last = i == len(parts) - 1
        m = await bot.send_message(chat_id, extra, reply_markup=kb if last else None)
        await track(state, m.message_id)


# ---------------------- МОИ ЗАКАЗЫ С ПАГИНАЦИЕЙ ----------------------------
async def build_my_orders_page(user_id: int, page: int):
    orders = await get_orders_by_user(user_id)
    if not orders:
        return "У вас пока нет заказов.", InlineKeyboardMarkup(inline_keyboard=[])
    pages = total_pages(len(orders))
    page = max(0, min(page, pages - 1))
    lines = [f"📦 <b>Ваши заказы</b> — страница {page + 1} из {pages}\n"]
    for o in page_slice(orders, page):
        lines.append(f"#{o['id']} — {o['size']} — {o['amount']:,} сум — {o['status']}")
    rows = []
    nav = nav_row(f"myorders:{user_id}", page, pages)
    if nav:
        rows.append(nav)
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@client_router.message(F.text == BTN_MY_ORDERS)
async def btn_my_orders(message: Message, state: FSMContext):
    await track(state, message.message_id)
    text, kb = await build_my_orders_page(message.from_user.id, 0)
    await show_page(client_bot, state, message.chat.id, text, kb)


@client_router.callback_query(F.data.startswith("myorders:"))
async def on_my_orders_page(callback: CallbackQuery, state: FSMContext):
    _, user_id_str, page_str = callback.data.split(":")
    text, kb = await build_my_orders_page(int(user_id_str), int(page_str))
    await show_page(client_bot, state, callback.message.chat.id, text, kb, callback.message)
    await callback.answer()


@client_router.callback_query(F.data == "noop")
async def on_noop(callback: CallbackQuery):
    await callback.answer()


# ----------------------------- ЗАКАЗ ---------------------------------------
@client_router.message(OrderFSM.waiting_for_code, F.text)
async def process_code(message: Message, state: FSMContext):
    await track(state, message.message_id)
    product = await get_product_by_code(message.text.strip())
    if not product:
        msg = await message.answer("❌ Товар с таким кодом не найден. Откройте «🛍 Каталог» в меню.",
                                   reply_markup=client_menu())
        await track(state, msg.message_id)
        return
    await send_product_card(client_bot, state, message.chat.id, product)


@client_router.callback_query(F.data.startswith("buy:"))
async def on_size_selected(callback: CallbackQuery, state: FSMContext):
    _, product_id_str, size = callback.data.split(":")
    product = await get_product_by_id(int(product_id_str))
    if not product:
        await callback.answer("Товар не найден", show_alert=True)
        return

    await state.update_data(product_id=product['id'], size=size, price=product['price'], title=product['title'])
    await state.set_state(OrderFSM.waiting_for_name)
    msg = await callback.message.answer(
        f"✅ Вы выбрали: <b>{product['title']}</b> (Размер: <b>{size}</b>)\n\n"
        "Введите <b>Фамилию и Имя</b> получателя (или нажмите «❌ Отмена»):",
        reply_markup=client_menu(),
    )
    await track(state, msg.message_id)
    await callback.answer()


@client_router.message(OrderFSM.waiting_for_name, F.text)
async def process_name(message: Message, state: FSMContext):
    await track(state, message.message_id)
    await state.update_data(full_name=message.text.strip())
    await state.set_state(OrderFSM.waiting_for_phone)
    msg = await message.answer("📞 Отправьте <b>номер телефона</b> кнопкой ниже или напишите вручную:",
                               reply_markup=phone_menu())
    await track(state, msg.message_id)


@client_router.message(OrderFSM.waiting_for_phone)
async def process_phone(message: Message, state: FSMContext):
    await track(state, message.message_id)
    phone = message.contact.phone_number if message.contact else (message.text or "").strip()
    if not phone or len(phone) < 7:
        msg = await message.answer("Укажите корректный номер телефона:", reply_markup=phone_menu())
        await track(state, msg.message_id)
        return
    await state.update_data(phone=phone)
    await state.set_state(OrderFSM.waiting_for_address)
    msg = await message.answer("📍 Введите <b>город и адрес доставки</b> (или пункт BTS/EMU/CDEK):",
                               reply_markup=client_menu())
    await track(state, msg.message_id)


@client_router.message(OrderFSM.waiting_for_address, F.text)
async def process_address(message: Message, state: FSMContext):
    await track(state, message.message_id)
    address = message.text.strip()
    data = await state.get_data()
    price, title, size = data['price'], data['title'], data['size']

    order_id = await create_order(
        user_id=message.from_user.id,
        username=message.from_user.username or "",
        full_name=data['full_name'],
        phone=data['phone'],
        address=address,
        product_id=data['product_id'],
        size=size,
        amount=price,
    )

    # Сообщение клиенту с реквизитами для перевода
    payment_info = (
        f"🛍 <b>Заказ #{order_id} успешно оформлен!</b>\n\n"
        f"• Товар: <b>{title}</b>\n"
        f"• Размер: <b>{size}</b>\n"
        f"• Получатель: <b>{data['full_name']}</b>\n"
        f"• Телефон: <b>{data['phone']}</b>\n"
        f"• Адрес: <b>{address}</b>\n"
        f"• К оплате: <b>{price:,} сум</b>\n\n"
        f"💳 <b>Реквизиты для оплаты (Click / Payme):</b>\n"
        f"Номер карты (нажмите, чтобы скопировать):\n"
        f"<code>5614681852563877</code>\n"
        f"Получатель: <b>Mahliyo Burxanova</b>\n\n"
        f"⚠️ <i>После перевода отправьте скриншот чека: @{MANAGER_USERNAME}</i>"
    )

    check_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🧾 Отправить чек менеджеру",
                    url=f"https://t.me/{MANAGER_USERNAME}",
                )
            ]
        ]
    )

    await message.answer(payment_info, reply_markup=check_kb)
    await message.answer("Вы можете продолжить покупки в меню ниже 👇", reply_markup=client_menu())

    # Оповещение обоим администраторам
    order_notification = (
        f"🚨 <b>Новый заказ #{order_id}! (Ожидает оплаты)</b>\n\n"
        f"👤 Клиент: {data['full_name']} (@{message.from_user.username or 'нет'})\n"
        f"📞 Телефон: {data['phone']}\n"
        f"📍 Адрес: {address}\n"
        f"👗 Товар: {title}\n"
        f"📏 Размер: {size}\n"
        f"💰 Сумма: {price:,} сум"
    )

    for admin_id in ADMIN_IDS:
        try:
            await admin_bot.send_message(
                chat_id=admin_id,
                text=order_notification,
                reply_markup=admin_order_actions_keyboard(order_id),
            )
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")

    await state.clear()


@client_router.message(F.text)
async def client_fallback(message: Message, state: FSMContext):
    """Любой текст вне сценария: ищем товар по коду, иначе подсказываем меню."""
    await track(state, message.message_id)
    product = await get_product_by_code(message.text.strip())
    if product:
        await send_product_card(client_bot, state, message.chat.id, product)
        return
    msg = await message.answer(
        "Печатать ничего не нужно 🙂 Пользуйтесь кнопками меню ниже 👇\n"
        f"Нужен живой человек? Напишите @{MANAGER_USERNAME}",
        reply_markup=client_menu(),
    )
    await track(state, msg.message_id)


# ===========================================================================
# 8. АДМИН-БОТ
# ===========================================================================
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


@admin_router.message(F.text == BTN_CANCEL)
@admin_router.message(Command("cancel"))
async def admin_cancel(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await do_cancel(admin_bot, state, message, admin_menu())


@admin_router.message(CommandStart())
@admin_router.message(Command("admin"))
@admin_router.message(Command("menu"))
async def cmd_admin_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещен.")
        return
    await state.clear()
    msg = await message.answer("👑 <b>Панель управления Liyodora</b>\nВсё в меню ниже 👇",
                               reply_markup=admin_menu())
    await track(state, msg.message_id)


@admin_router.message(F.text == BTN_ADD)
async def start_add_product(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminAddProductFSM.waiting_for_code)
    msg = await message.answer("Шаг 1/6: <b>кодовое слово</b> (например <code>dress01</code>):",
                               reply_markup=admin_menu())
    await track(state, message.message_id, msg.message_id)


@admin_router.message(AdminAddProductFSM.waiting_for_code, F.text)
async def add_product_code(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await track(state, message.message_id)
    await state.update_data(code=message.text.strip().lower())
    await state.set_state(AdminAddProductFSM.waiting_for_title)
    msg = await message.answer("Шаг 2/6: <b>название товара</b>:", reply_markup=admin_menu())
    await track(state, msg.message_id)


@admin_router.message(AdminAddProductFSM.waiting_for_title, F.text)
async def add_product_title(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await track(state, message.message_id)
    await state.update_data(title=message.text.strip())
    await state.set_state(AdminAddProductFSM.waiting_for_desc)
    msg = await message.answer("Шаг 3/6: <b>описание</b>:", reply_markup=admin_menu())
    await track(state, msg.message_id)


@admin_router.message(AdminAddProductFSM.waiting_for_desc, F.text)
async def add_product_desc(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await track(state, message.message_id)
    await state.update_data(desc=message.text.strip())
    await state.set_state(AdminAddProductFSM.waiting_for_price)
    msg = await message.answer("Шаг 4/6: <b>цена в сумах</b> (например <code>250000</code>):",
                               reply_markup=admin_menu())
    await track(state, msg.message_id)


@admin_router.message(AdminAddProductFSM.waiting_for_price, F.text)
async def add_product_price(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await track(state, message.message_id)
    try:
        price = int(message.text.strip().replace(" ", ""))
    except ValueError:
        msg = await message.answer("Введите число:", reply_markup=admin_menu())
        await track(state, msg.message_id)
        return
    await state.update_data(price=price)
    await state.set_state(AdminAddProductFSM.waiting_for_sizes)
    msg = await message.answer("Шаг 5/6: <b>размеры через запятую</b> (<code>S, M, L</code>):",
                               reply_markup=admin_menu())
    await track(state, msg.message_id)


@admin_router.message(AdminAddProductFSM.waiting_for_sizes, F.text)
async def add_product_sizes(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await track(state, message.message_id)
    await state.update_data(sizes=message.text.strip())
    await state.set_state(AdminAddProductFSM.waiting_for_photo)
    msg = await message.answer("Шаг 6/6: отправьте <b>фото товара</b>:", reply_markup=admin_menu())
    await track(state, msg.message_id)


@admin_router.message(AdminAddProductFSM.waiting_for_photo, F.photo)
async def add_product_photo(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    code = data['code']

    photo = message.photo[-1]
    file_info = await admin_bot.get_file(photo.file_id)
    photo_path = os.path.join(PHOTOS_DIR, f"{code}.jpg")
    await admin_bot.download_file(file_info.file_path, photo_path)

    await add_product(code=code, title=data['title'], desc=data['desc'],
                      price=data['price'], sizes=data['sizes'], photo_path=photo_path)

    await clear_trash(admin_bot, state, message.chat.id)
    await state.clear()
    await message.answer(
        f"✅ <b>Товар добавлен!</b>\n\n• Код: <code>{code}</code>\n• Название: {data['title']}\n"
        f"• Цена: {data['price']:,} сум\n• Размеры: {data['sizes']}",
        reply_markup=admin_menu(),
    )


# --------------------- ТОВАРЫ АДМИНА С ПАГИНАЦИЕЙ --------------------------
async def build_admin_products_page(page: int):
    products = await get_all_products()
    if not products:
        return "В каталоге пока нет товаров.", InlineKeyboardMarkup(inline_keyboard=[])
    pages = total_pages(len(products))
    page = max(0, min(page, pages - 1))
    lines = [f"📋 <b>Товары</b> — страница {page + 1} из {pages}\n"]
    rows = []
    for p in page_slice(products, page):
        lines.append(f"🏷 <b>{p['title']}</b> (код: <code>{p['code_word']}</code>)\n"
                     f"{p['price']:,} сум | размеры: {p['sizes']}\n")
        rows.append([InlineKeyboardButton(text=f"🗑 Удалить: {p['title'][:22]}",
                                          callback_data=f"del_prod:{p['id']}:{page}")])
    nav = nav_row("aprod", page, pages)
    if nav:
        rows.append(nav)
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@admin_router.message(F.text == BTN_PRODUCTS)
async def list_products(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await track(state, message.message_id)
    text, kb = await build_admin_products_page(0)
    await show_page(admin_bot, state, message.chat.id, text, kb)


@admin_router.callback_query(F.data.startswith("aprod:"))
async def on_admin_products_page(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    page = int(callback.data.split(":")[1])
    text, kb = await build_admin_products_page(page)
    await show_page(admin_bot, state, callback.message.chat.id, text, kb, callback.message)
    await callback.answer()


@admin_router.callback_query(F.data.startswith("del_prod:"))
async def on_delete_product(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    parts = callback.data.split(":")
    product_id = int(parts[1])
    page = int(parts[2]) if len(parts) > 2 else 0
    await delete_product(product_id)
    text, kb = await build_admin_products_page(page)
    await show_page(admin_bot, state, callback.message.chat.id, text, kb, callback.message)
    await callback.answer("Товар удален!")


# --------------------- ЗАКАЗЫ АДМИНА С ПАГИНАЦИЕЙ --------------------------
async def build_admin_orders_page(page: int):
    orders = await get_recent_orders()
    if not orders:
        return "Заказов пока нет.", InlineKeyboardMarkup(inline_keyboard=[])
    pages = total_pages(len(orders))
    page = max(0, min(page, pages - 1))
    lines = [f"🧾 <b>Заказы</b> — страница {page + 1} из {pages}\n"]
    rows = []
    for o in page_slice(orders, page):
        lines.append(f"<b>#{o['id']}</b> — {o['status']}\n👤 {o['full_name']} | 📞 {o['phone']}\n"
                     f"📍 {o['address']}\n📏 {o['size']} | 💰 {o['amount']:,} сум\n")
        rows.append([
            InlineKeyboardButton(text=f"🚚 #{o['id']}", callback_data=f"order_status:{o['id']}:SHIPPED:{page}"),
            InlineKeyboardButton(text=f"❌ #{o['id']}", callback_data=f"order_status:{o['id']}:CANCELLED:{page}"),
        ])
    nav = nav_row("aord", page, pages)
    if nav:
        rows.append(nav)
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@admin_router.message(F.text == BTN_ORDERS)
async def list_orders(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await track(state, message.message_id)
    text, kb = await build_admin_orders_page(0)
    await show_page(admin_bot, state, message.chat.id, text, kb)


@admin_router.callback_query(F.data.startswith("aord:"))
async def on_admin_orders_page(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    page = int(callback.data.split(":")[1])
    text, kb = await build_admin_orders_page(page)
    await show_page(admin_bot, state, callback.message.chat.id, text, kb, callback.message)
    await callback.answer()


@admin_router.message(F.text == BTN_STATS)
async def admin_stats(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await track(state, message.message_id)
    products = await get_all_products()
    orders = await get_recent_orders(1000)
    total = sum(o['amount'] for o in orders if o['status'] != 'CANCELLED')
    msg = await message.answer(
        f"📊 <b>Статистика</b>\n\n• Товаров: <b>{len(products)}</b>\n"
        f"• Заказов: <b>{len(orders)}</b>\n• Сумма заказов: <b>{total:,} сум</b>",
        reply_markup=admin_menu(),
    )
    await track(state, msg.message_id)


@admin_router.callback_query(F.data.startswith("order_status:"))
async def on_change_order_status(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    parts = callback.data.split(":")
    order_id, status = int(parts[1]), parts[2]
    page = int(parts[3]) if len(parts) > 3 else None

    order = await get_order_by_id(order_id)
    if not order:
        await callback.answer("Заказ не найден", show_alert=True)
        return

    await update_order_status(order_id, status)
    if status == "SHIPPED":
        client_msg = f"🚚 Ваш заказ <b>#{order_id}</b> передан в доставку! Ожидайте."
        status_text = "🚚 <b>Отправлен</b>"
    else:
        client_msg = f"❌ Ваш заказ <b>#{order_id}</b> отменен. Менеджер свяжется с вами."
        status_text = "❌ <b>Отменен</b>"

    try:
        await client_bot.send_message(chat_id=order['user_id'], text=client_msg)
    except Exception as e:
        logger.error(f"Ошибка отправки статуса: {e}")

    if page is not None:
        text, kb = await build_admin_orders_page(page)
        await show_page(admin_bot, state, callback.message.chat.id, text, kb, callback.message)
    else:
        try:
            base = callback.message.text or callback.message.caption or ""
            await callback.message.edit_text(base + f"\n\nСтатус: {status_text}")
        except Exception:
            pass
    await callback.answer("Статус обновлен!")


@admin_router.callback_query(F.data == "noop")
async def on_admin_noop(callback: CallbackQuery):
    await callback.answer()


# ===========================================================================
# 9. ЗАПУСК ДВУХ БОТОВ
# ===========================================================================
async def main():
    await init_db()
    await start_health_check_server()

    client_dp = Dispatcher(storage=MemoryStorage())
    admin_dp = Dispatcher(storage=MemoryStorage())
    client_dp.include_router(client_router)
    admin_dp.include_router(admin_router)

    await client_bot.delete_webhook(drop_pending_updates=True)
    await admin_bot.delete_webhook(drop_pending_updates=True)

    logger.info("Оба бота запущены.")
    await asyncio.gather(
        client_dp.start_polling(client_bot),
        admin_dp.start_polling(admin_bot),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Боты остановлены.")
