import os
import json
import logging
import requests
from datetime import datetime
from flask import Flask, request, jsonify
import telebot
from telebot import types
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

# Настраиваем логирование для telebot
telebot.logger.setLevel(logging.INFO)

load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))
PORT = int(os.getenv('PORT', 10000))
STOCK_BOT_URL = os.getenv('STOCK_BOT_URL')

if not BOT_TOKEN or not DATABASE_URL:
    raise ValueError("Не заданы обязательные переменные окружения")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_URL = os.getenv('RENDER_EXTERNAL_URL', 'https://dp-sbor-miniapp-bot.onrender.com')
WEBHOOK_URL = f"{BASE_URL}/webhook"

def escape_markdown(text):
    """Экранирует специальные символы Markdown"""
    if not text:
        return text
    # Символы, которые нужно экранировать: _ * [ ] ( ) ~ ` > # + - = | { } . !
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, '\\' + char)
    return text

def parse_contact(contact_json):
    if isinstance(contact_json, dict):
        return contact_json
    try:
        return json.loads(contact_json)
    except:
        return {}

def parse_items(items_json):
    if isinstance(items_json, list):
        return items_json
    try:
        return json.loads(items_json)
    except:
        return []

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def get_seller_by_address(address: str):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT seller_id FROM pickup_locations WHERE address = %s", (address,))
            addr = cur.fetchone()
            if not addr or not addr['seller_id']:
                return None
            seller_id = addr['seller_id']
            cur.execute("SELECT * FROM sellers WHERE id = %s", (seller_id,))
            return cur.fetchone()

def get_seller_by_telegram_id(telegram_id: int):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM sellers WHERE telegram_id = %s", (telegram_id,))
            return cur.fetchone()

def get_admin_seller():
    """Возвращает запись продавца-администратора по ADMIN_ID"""
    seller = get_seller_by_telegram_id(ADMIN_ID)
    logger.info(f"get_admin_seller() вызван, ADMIN_ID={ADMIN_ID}, результат: {seller}")
    return seller

def generate_order_number(seller_id: int, delivery_type: str = None) -> str:
    """Генерирует номер заказа.
    Если это доставка (courier) - используем префикс 'D'.
    Если самовывоз - используем префикс продавца (A, U, E, T, R, J и т.д.).
    """
    # Если это доставка, всегда возвращаем D + номер
    if delivery_type == 'courier':
        prefix = 'D'
    else:
        # Для самовывоза - берем префикс продавца
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT seller_prefix FROM sellers WHERE id = %s", (seller_id,))
                result = cur.fetchone()
                
                if not result or not result['seller_prefix']:
                    # Если префикс не задан, используем первую букву имени
                    cur.execute("SELECT name FROM sellers WHERE id = %s", (seller_id,))
                    name = cur.fetchone()['name']
                    prefix = name[0].upper()
                else:
                    prefix = result['seller_prefix']
    
    # Обрезаем до 3 символов, если нужно
    if len(prefix) > 3:
        prefix = prefix[:3]
    
    # Получаем последний номер для этого префикса
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT order_number FROM orders 
                WHERE order_number LIKE %s 
                ORDER BY id DESC LIMIT 1
            """, (prefix + '%',))
            
            last = cur.fetchone()
            if last:
                num_str = last['order_number'][len(prefix):]
                if num_str.isdigit():
                    new_num = int(num_str) + 1
                else:
                    new_num = 1
            else:
                new_num = 1
            
            return f"{prefix}{new_num}"

def save_order(order_data: dict, contact: dict, request_id: str = None):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            items_json = json.dumps(order_data['items'])
            contact_json = json.dumps(contact)
            cur.execute("""
                INSERT INTO orders (order_number, user_id, seller_id, address_id, items, total, contact, status, request_id, notified_bool, delivery_type)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                order_data['order_number'],
                order_data['user_id'],
                order_data['seller_id'],
                order_data.get('address_id'),
                items_json,
                order_data['total'],
                contact_json,
                order_data['status'],
                request_id,
                False,
                order_data.get('delivery_type')
            ))
            order_id = cur.fetchone()['id']
            conn.commit()
            return order_id

def update_order_status(order_id: int, status: str):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE orders SET status = %s WHERE id = %s",
                (status, order_id)
            )
            conn.commit()

def get_active_order_by_buyer(buyer_id: int):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM orders WHERE user_id = %s AND status IN ('active', 'Активный')", (buyer_id,))
            order = cur.fetchone()
            if order:
                order['contact'] = parse_contact(order['contact'])
                order['items'] = parse_items(order['items'])
            return order

def get_active_orders_by_seller(seller_id: int):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM orders WHERE seller_id = %s AND status IN ('active', 'Активный')", (seller_id,))
            orders = cur.fetchall()
            for o in orders:
                o['contact'] = parse_contact(o['contact'])
                o['items'] = parse_items(o['items'])
            return orders

def get_order_by_number(order_number: str):
    logger.info(f"🔍 get_order_by_number: ищем заказ с номером '{order_number}'")
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM orders WHERE order_number = %s", (order_number,))
            order = cur.fetchone()
            if order:
                order['contact'] = parse_contact(order['contact'])
                order['items'] = parse_items(order['items'])
            return order

def complete_order(order_id: int):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE orders SET status = 'completed', completed_at = %s WHERE id = %s",
                        (datetime.utcnow().isoformat(), order_id))
            conn.commit()

def get_messages_for_order(order_id: int):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT sender_role, text, created_at 
                FROM messages 
                WHERE order_id = %s 
                ORDER BY created_at ASC
            """, (order_id,))
            return cur.fetchall()

def save_message(order_id: int, sender_id: int, sender_role: str, text: str):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO messages (order_id, sender_id, sender_role, text)
                VALUES (%s, %s, %s, %s)
            """, (order_id, sender_id, sender_role, text))
            conn.commit()

def is_admin(telegram_id: int) -> bool:
    return telegram_id == ADMIN_ID

# ========== Клавиатуры ==========
def seller_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(types.KeyboardButton("📋 Мои активные заказы"))
    return keyboard

def admin_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(types.KeyboardButton("📋 Мои активные заказы"))
    return keyboard

# ========== Хэндлеры ==========
@bot.message_handler(commands=['start'])
def handle_start(message):
    user_id = message.from_user.id
    parts = message.text.split()
    param = parts[1] if len(parts) > 1 else ''
    
    if param.startswith('order_'):
        order_num = param[6:]
        bot.send_message(
            user_id,
            f"✅ Здравствуйте! Ваш заказ №{order_num} оформлен. Здесь вы можете общаться с продавцом и получать уведомления о статусе заказа.\n\nЕсли у вас есть вопросы, просто напишите их в этот чат."
        )
        return

    seller = get_seller_by_telegram_id(user_id)
    if seller:
        bot.send_message(
            user_id,
            "👋 Добро пожаловать! Здесь будут ваши заказы и общение с покупателями.",
            reply_markup=seller_keyboard()
        )
    elif is_admin(user_id):
        bot.send_message(
            user_id,
            "👋 Добро пожаловать в панель администратора!",
            reply_markup=admin_keyboard()
        )
    else:
        bot.send_message(
            user_id,
            "👋 Добро пожаловать! Если вы оформили заказ, то здесь будет общение с продавцом."
        )

@bot.message_handler(func=lambda m: m.text == "📋 Мои активные заказы")
def handle_my_orders(message):
    logger.info("handle_my_orders вызван")
    user_id = message.from_user.id
    seller = get_seller_by_telegram_id(user_id)
    
    # Если пользователь не продавец и не админ - отказываем
    if not seller and not is_admin(user_id):
        bot.reply_to(message, "❌ У вас нет доступа к этой функции.")
        return

    # Для администратора показываем ВСЕ активные заказы
    if is_admin(user_id):
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT * FROM orders 
                    WHERE status IN ('active', 'Активный') 
                    ORDER BY id DESC
                """)
                orders = cur.fetchall()
                for o in orders:
                    o['contact'] = parse_contact(o['contact'])
                    o['items'] = parse_items(o['items'])
        
        if not orders:
            bot.reply_to(message, "Нет активных заказов.")
            return
            
        markup = types.InlineKeyboardMarkup(row_width=2)
        for order in orders:
            # Получаем имя продавца для отображения в кнопке
            seller_name = "Неизвестный"
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT name FROM sellers WHERE id = %s", (order['seller_id'],))
                    s = cur.fetchone()
                    if s:
                        seller_name = s['name']
            
            # В кнопке показываем номер заказа и имя продавца
            callback_data = f"view_order_{order['order_number']}"
            markup.add(types.InlineKeyboardButton(
                f"Заказ {order['order_number']} ({seller_name})",
                callback_data=callback_data
            ))
        
        bot.send_message(
            message.chat.id,
            "📋 *Все активные заказы:*\nВыберите заказ для просмотра деталей и истории сообщений.",
            parse_mode='Markdown',
            reply_markup=markup
        )
        return

    # Для обычного продавца показываем только его заказы
    orders = get_active_orders_by_seller(seller['id'])
    if not orders:
        bot.reply_to(message, "У вас нет активных заказов.")
        return

    markup = types.InlineKeyboardMarkup(row_width=2)
    for order in orders:
        callback_data = f"view_order_{order['order_number']}"
        logger.info(f"Создаём кнопку с callback_data: {callback_data}")
        markup.add(types.InlineKeyboardButton(
            f"Заказ {order['order_number']}",
            callback_data=callback_data
        ))
    bot.send_message(
        message.chat.id,
        "📋 *Ваши активные заказы:*\nВыберите заказ для просмотра деталей и истории сообщений.",
        parse_mode='Markdown',
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('view_order_'))
def view_order(call):
    user_id = call.from_user.id
    order_num = call.data.split('_')[2]
    logger.info(f"view_order вызван для заказа {order_num} пользователем {user_id}")
    
    order = get_order_by_number(order_num)
    if not order:
        logger.error(f"Заказ {order_num} не найден")
        bot.answer_callback_query(call.id, "❌ Заказ не найден")
        return
    
    # Проверяем права: админ может смотреть любой заказ, продавец только свой
    if not is_admin(user_id):
        seller = get_seller_by_telegram_id(user_id)
        if not seller or order['seller_id'] != seller['id']:
            bot.answer_callback_query(call.id, "❌ У вас нет прав для просмотра этого заказа")
            return
    
    logger.info("Заказ получен, приступаем к формированию данных")
    
    try:
        messages = get_messages_for_order(order['id'])
        logger.info(f"Получено сообщений: {len(messages)}")
    except Exception as e:
        logger.exception(f"Ошибка при получении сообщений: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка получения истории")
        return
    
    contact = order['contact']
    logger.info("Формируем текст заказа")
    try:
        items_text = "\n".join([
            f"• {item['name']} ({item.get('variantName', '')}) x{item['quantity']} = {item['price']*item['quantity']} руб."
            for item in order['items']
        ])
        delivery_text = "Самовывоз" if order.get('delivery_type') == 'pickup' else "Доставка"
        
        # Экранируем специальные символы
        username_raw = contact.get('username', 'не указан')
        username_escaped = escape_markdown(username_raw)
        username_display = f"@{username_escaped}" if username_raw != 'не указан' else "@не указан"
        
        name_escaped = escape_markdown(contact.get('name', 'Неизвестно'))
        address_escaped = escape_markdown(contact.get('address', 'Не указан'))
        phone_escaped = escape_markdown(contact.get('phone', 'Не указан'))
        
        info = (
            f"📦 *Заказ {order_num}*\n\n"
            f"👤 Покупатель: {name_escaped}\n"
            f"📍 Адрес: {address_escaped}\n"
            f"📞 Телефон: {phone_escaped}\n"
            f"📱 Username: {username_display}\n"
            f"💳 Оплата: {'Наличные' if contact.get('paymentMethod') == 'cash' else 'Перевод'}\n"
            f"🚚 Доставка: {delivery_text}\n\n"
            f"📝 *Состав заказа:*\n{items_text}\n\n"
            f"💰 *Итого: {order['total']} руб.*\n"
        )
        logger.info("Текст заказа сформирован")
        
        if messages:
            history_lines = []
            for msg in messages:
                sender = '👤 Покупатель' if msg['sender_role'] == 'buyer' else '🛒 Продавец'
                # Экранируем текст сообщения
                msg_text_escaped = escape_markdown(msg['text'])
                created_str = msg['created_at'].strftime('%Y-%m-%d %H:%M') if msg['created_at'] else ''
                history_lines.append(f"{sender} ({created_str}): {msg_text_escaped}")
            history = "\n".join(history_lines)
            info += f"\n💬 *История переписки:*\n{history}"
        else:
            info += "\n💬 *История переписки:*\nПока нет сообщений."
        logger.info("История переписки добавлена")
    except Exception as e:
        logger.exception(f"Ошибка при формировании текста: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка формирования данных")
        return

    markup = types.InlineKeyboardMarkup()
    if order['status'] in ('active', 'Активный'):
        markup.row(
            types.InlineKeyboardButton("✅ Завершить", callback_data=f"complete_{order_num}"),
            types.InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_{order_num}")
        )
    else:
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_orders"))
    logger.info("Клавиатура сформирована")

    try:
        bot.edit_message_text(
            info,
            call.message.chat.id,
            call.message.message_id,
            parse_mode='Markdown',
            reply_markup=markup
        )
        logger.info("Сообщение успешно отредактировано")
    except Exception as e:
        logger.exception(f"Ошибка при редактировании сообщения: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка отправки")
        return

    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "back_to_orders")
def back_to_orders(call):
    logger.info("back_to_orders вызван")
    user_id = call.from_user.id
    
    if is_admin(user_id):
        # Для админа показываем все заказы
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT * FROM orders 
                    WHERE status IN ('active', 'Активный') 
                    ORDER BY id DESC
                """)
                orders = cur.fetchall()
                for o in orders:
                    o['contact'] = parse_contact(o['contact'])
                    o['items'] = parse_items(o['items'])
        
        if not orders:
            bot.edit_message_text("Нет активных заказов.", call.message.chat.id, call.message.message_id)
            return
            
        markup = types.InlineKeyboardMarkup(row_width=2)
        for order in orders:
            seller_name = "Неизвестный"
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT name FROM sellers WHERE id = %s", (order['seller_id'],))
                    s = cur.fetchone()
                    if s:
                        seller_name = s['name']
            
            markup.add(types.InlineKeyboardButton(
                f"Заказ {order['order_number']} ({seller_name})",
                callback_data=f"view_order_{order['order_number']}"
            ))
        
        bot.edit_message_text(
            "📋 *Все активные заказы:*\nВыберите заказ для просмотра деталей и истории сообщений.",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='Markdown',
            reply_markup=markup
        )
    else:
        # Для продавца показываем только его заказы
        seller = get_seller_by_telegram_id(user_id)
        if not seller:
            bot.answer_callback_query(call.id, "❌ Ошибка доступа")
            return
            
        orders = get_active_orders_by_seller(seller['id'])
        if not orders:
            bot.edit_message_text("У вас нет активных заказов.", call.message.chat.id, call.message.message_id)
            return
            
        markup = types.InlineKeyboardMarkup(row_width=2)
        for order in orders:
            markup.add(types.InlineKeyboardButton(
                f"Заказ {order['order_number']}",
                callback_data=f"view_order_{order['order_number']}"
            ))
        
        bot.edit_message_text(
            "📋 *Ваши активные заказы:*\nВыберите заказ для просмотра деталей и истории сообщений.",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='Markdown',
            reply_markup=markup
        )
    
    bot.answer_callback_query(call.id)

# Обработчик сообщений от покупателей – только если нет символа # в начале
@bot.message_handler(func=lambda m: get_active_order_by_buyer(m.from_user.id) is not None and not m.text.startswith('#'))
def handle_buyer_message(message):
    user_id = message.from_user.id
    order = get_active_order_by_buyer(user_id)
    if not order:
        return

    save_message(order['id'], user_id, 'buyer', message.text)
    logger.info(f"Сообщение от покупателя сохранено для заказа {order['order_number']}")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT telegram_id, name FROM sellers WHERE id = %s", (order['seller_id'],))
            seller = cur.fetchone()
    if seller:
        seller_tg = seller['telegram_id']
        seller_name = seller['name']
        logger.info(f"Пересылка сообщения продавцу {seller_name} (id={order['seller_id']}, tg={seller_tg})")
        try:
            bot.send_message(
                seller_tg,
                f"💬 Сообщение от покупателя (заказ {order['order_number']}):\n\n{message.text}"
            )
            logger.info(f"Сообщение успешно отправлено продавцу {seller_tg}")
        except Exception as e:
            logger.error(f"Ошибка отправки продавцу {seller_tg}: {e}")
    else:
        logger.error(f"Продавец с id {order['seller_id']} не найден в таблице sellers")

    if ADMIN_ID and order['seller_id'] != ADMIN_ID:
        try:
            bot.send_message(
                ADMIN_ID,
                f"📩 [Копия] Покупатель {order['contact']['name']} (заказ {order['order_number']}):\n{message.text}"
            )
        except Exception as e:
            logger.error(f"Ошибка отправки копии админу: {e}")

    bot.reply_to(message, "✅ Сообщение отправлено.")

# Обработчик сообщений от продавцов и админа (с #)
@bot.message_handler(func=lambda m: (get_seller_by_telegram_id(m.from_user.id) is not None or is_admin(m.from_user.id)) and m.text.startswith('#'))
def handle_seller_message(message):
    user_id = message.from_user.id
    text = message.text.strip()
    logger.info(f"Сообщение от пользователя {user_id}: {text}")

    try:
        parts = text[1:].split(' ', 1)
        order_num = parts[0]
        reply_text = parts[1] if len(parts) > 1 else ""
        if not reply_text:
            bot.reply_to(message, "❌ Вы не написали текст сообщения.")
            return

        order = get_order_by_number(order_num)
        if not order:
            bot.reply_to(message, f"❌ Заказ {order_num} не найден.")
            return

        # Проверка прав:
        # - Админ может отвечать на любой заказ
        # - Продавец только на свои
        if not is_admin(user_id):
            seller = get_seller_by_telegram_id(user_id)
            if not seller or order['seller_id'] != seller['id']:
                bot.reply_to(message, "❌ Этот заказ не ваш.")
                return

        # Определяем роль отправителя
        sender_role = 'admin' if is_admin(user_id) else 'seller'
        
        save_message(order['id'], user_id, sender_role, reply_text)
        logger.info(f"Сообщение от {sender_role} сохранено для заказа {order_num}")

        try:
            buyer_id = order['user_id']
            logger.info(f"Отправка ответа покупателю {buyer_id} по заказу {order_num}")
            bot.send_message(
                buyer_id,
                f"💬 Сообщение от {'администратора' if is_admin(user_id) else 'продавца'} (заказ {order_num}):\n\n{reply_text}"
            )
            logger.info(f"Сообщение отправлено покупателю {buyer_id}")
        except Exception as e:
            logger.error(f"Ошибка отправки покупателю {buyer_id}: {e}")

        # Отправляем копию админу, если отвечал не админ
        if ADMIN_ID and not is_admin(user_id):
            seller_name = seller['name'] if 'seller' in locals() and seller else "Неизвестный продавец"
            try:
                bot.send_message(
                    ADMIN_ID,
                    f"📩 [Копия] Продавец {seller_name} (заказ {order_num}):\n{reply_text}"
                )
            except Exception as e:
                logger.error(f"Ошибка отправки админу: {e}")

        bot.reply_to(message, f"✅ Сообщение отправлено покупателю (заказ {order_num}).", 
                    reply_markup=admin_keyboard() if is_admin(user_id) else seller_keyboard())

    except Exception as e:
        logger.error(f"Ошибка обработки сообщения: {e}", exc_info=True)
        bot.reply_to(message, "❌ Ошибка. Используйте формат: #А1 текст сообщения")

@bot.callback_query_handler(func=lambda call: call.data.startswith('complete_'))
def handle_seller_complete(call):
    user_id = call.from_user.id
    order_num = call.data.split('_')[1]
    logger.info(f"Пользователь {user_id} нажал завершить для заказа {order_num}")

    order = get_order_by_number(order_num)
    if not order:
        logger.error(f"Заказ {order_num} не найден")
        bot.answer_callback_query(call.id, "❌ Заказ не найден")
        return

    if not is_admin(user_id):
        seller = get_seller_by_telegram_id(user_id)
        if not seller or order['seller_id'] != seller['id']:
            logger.error(f"Заказ {order_num} не принадлежит пользователю {user_id}")
            bot.answer_callback_query(call.id, "❌ Этот заказ не ваш")
            return

    if order['status'] not in ('active', 'Активный'):
        logger.error(f"Заказ {order_num} уже не активен (статус: {order['status']})")
        bot.answer_callback_query(call.id, f"❌ Заказ уже не активен")
        try:
            bot.edit_message_reply_markup(
                user_id,
                call.message.message_id,
                reply_markup=None
            )
        except:
            pass
        return

    complete_order(order['id'])
    logger.info(f"Заказ {order_num} завершён в БД")

    if STOCK_BOT_URL:
        try:
            response = requests.post(
                f"{STOCK_BOT_URL}/api/order-completed",
                json={"order_number": order_num},
                timeout=3
            )
            if response.ok:
                logger.info(f"Уведомление о завершении заказа {order_num} отправлено складскому боту")
            else:
                logger.error(f"Складской бот вернул ошибку: {response.status_code} - {response.text}")
        except Exception as e:
            logger.error(f"Ошибка отправки в складской бот: {e}")

    try:
        bot.send_message(
            order['user_id'],
            f"✅ Ваш заказ {order_num} выполнен. Спасибо за покупку!"
        )
        logger.info(f"Уведомление отправлено покупателю {order['user_id']}")
    except Exception as e:
        logger.error(f"Ошибка уведомления покупателя: {e}")

    if ADMIN_ID:
        completer = "Администратор" if is_admin(user_id) else (seller['name'] if 'seller' in locals() and seller else "Неизвестный продавец")
        bot.send_message(
            ADMIN_ID,
            f"✅ {completer} завершил заказ {order_num}."
        )

    try:
        bot.edit_message_text(
            f"✅ Заказ {order_num} завершён.",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Не удалось отредактировать сообщение: {e}")

    bot.answer_callback_query(call.id, "✅ Заказ завершён")

@bot.callback_query_handler(func=lambda call: call.data.startswith('cancel_'))
def handle_cancel_order(call):
    user_id = call.from_user.id
    order_num = call.data.split('_')[1]
    logger.info(f"Пользователь {user_id} нажал отменить для заказа {order_num}")

    order = get_order_by_number(order_num)
    if not order:
        logger.error(f"Заказ {order_num} не найден")
        bot.answer_callback_query(call.id, "❌ Заказ не найден")
        return

    if not is_admin(user_id):
        seller = get_seller_by_telegram_id(user_id)
        if not seller or order['seller_id'] != seller['id']:
            logger.error(f"Заказ {order_num} не принадлежит пользователю {user_id}")
            bot.answer_callback_query(call.id, "❌ Этот заказ не ваш")
            return

    if order['status'] not in ('active', 'Активный'):
        logger.error(f"Заказ {order_num} уже не активен (статус: {order['status']})")
        bot.answer_callback_query(call.id, f"❌ Заказ уже не активен")
        try:
            bot.edit_message_reply_markup(
                user_id,
                call.message.message_id,
                reply_markup=None
            )
        except:
            pass
        return

    update_order_status(order['id'], 'Отменен')
    logger.info(f"Заказ {order_num} отменён")

    try:
        bot.edit_message_text(
            f"❌ *Заказ {order_num} отменён.*",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Не удалось отредактировать сообщение: {e}")

    try:
        bot.send_message(
            order['user_id'],
            f"❌ *Ваш заказ {order_num} отменён продавцом.*",
            parse_mode='Markdown'
        )
        logger.info(f"Уведомление об отмене отправлено покупателю {order['user_id']}")
    except Exception as e:
        logger.error(f"Ошибка уведомления покупателя: {e}")

    if ADMIN_ID:
        completer = "Администратор" if is_admin(user_id) else (seller['name'] if 'seller' in locals() and seller else "Неизвестный продавец")
        bot.send_message(
            ADMIN_ID,
            f"❌ {completer} отменил заказ {order_num}."
        )

    bot.answer_callback_query(call.id, "✅ Заказ отменён")

@bot.message_handler(func=lambda m: True)
def fallback_handler(message):
    user_id = message.from_user.id
    if get_seller_by_telegram_id(user_id):
        bot.send_message(message.chat.id, "Используйте кнопки или начните новый заказ в нашем мини-аппе.", reply_markup=seller_keyboard())
    elif is_admin(user_id):
        bot.send_message(message.chat.id, "Используйте кнопки администратора.", reply_markup=admin_keyboard())
    else:
        bot.send_message(message.chat.id, "Если у вас есть вопросы, напишите продавцу.")

# ========== Flask эндпоинты ==========
@app.route('/')
def index():
    return '🤖 Бот работает'

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return ''
    return 'Bad Request', 400

@app.route('/api/new-order', methods=['POST'])
def new_order():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data'}), 400

        user_id = data.get('userId')
        buyer_name = data.get('name', 'Покупатель')
        items = data.get('items')
        total = data.get('total')
        address = data.get('address')
        payment = data.get('paymentMethod')
        delivery = data.get('deliveryType')
        contact = data.get('contact')
        request_id = data.get('requestId')

        if not all([user_id, items, total, address]):
            return jsonify({'error': 'Missing required fields'}), 400

        logger.info(f"Получен запрос на новый заказ: delivery={delivery}, address={address}")

        # Определяем продавца
        if delivery == 'courier':
            # Для доставки используем администратора
            seller = get_admin_seller()
            logger.info(f"get_admin_seller() вернул: {seller}")
            
            if not seller:
                logger.error("Администратор не найден в таблице sellers")
                return jsonify({'error': 'Admin seller not found'}), 500
            
            logger.info(f"Заказ с доставкой, назначен админ: id={seller['id']}, name={seller['name']}, tg={seller['telegram_id']}")
            
            # Проверяем, что telegram_id совпадает с ADMIN_ID
            if seller['telegram_id'] != ADMIN_ID:
                logger.warning(f"telegram_id продавца ({seller['telegram_id']}) не совпадает с ADMIN_ID ({ADMIN_ID})")
        else:
            seller = get_seller_by_address(address)
            if not seller:
                logger.error(f"Не найден продавец для адреса {address}")
                return jsonify({'error': 'Seller not found for this address'}), 404
            logger.info(f"Найден продавец: {seller['name']} (id {seller['id']}, tg={seller['telegram_id']})")

        if request_id:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT id, order_number, notified_bool FROM orders WHERE request_id = %s", (request_id,))
                    existing = cur.fetchone()
                    if existing:
                        logger.info(f"Найден существующий заказ с request_id {request_id}")
                        order_number = existing['order_number']
                        if not order_number:
                            # Генерируем номер с учётом типа доставки
                            order_number = generate_order_number(seller['id'], delivery)
                            cur.execute("UPDATE orders SET order_number = %s WHERE id = %s", (order_number, existing['id']))
                            conn.commit()
                            logger.info(f"Обновлён заказ {existing['id']} с новым номером {order_number}")
                        if not existing['notified_bool']:
                            items_lines = []
                            for item in items:
                                item_name = f"{item['name']} ({item['variantName']})" if item.get('variantName') else item['name']
                                items_lines.append(f"• {item_name} x{item['quantity']} = {item['price']*item['quantity']} руб.")
                            items_text = "\n".join(items_lines)
                            delivery_text = "Самовывоз" if delivery == 'pickup' else "Доставка"
                            order_text = f"{items_text}\n\nСумма: {total} руб.\nОплата: {'Наличные' if payment=='cash' else 'Перевод'}\nДоставка: {delivery_text}"
                            markup = types.InlineKeyboardMarkup(row_width=2)
                            markup.add(
                                types.InlineKeyboardButton("✅ Завершить", callback_data=f"complete_{order_number}"),
                                types.InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_{order_number}")
                            )
                            phone = contact.get('phone', 'не указан')
                            username = contact.get('username', 'не указан')
                            
                            # Формируем username с экранированием
                            username_escaped = escape_markdown(username) if username else ''
                            username_display = f"@{username_escaped}" if username else "@не указан"
                            
                            # Экранируем имя покупателя
                            buyer_name_escaped = escape_markdown(buyer_name)
                            
                            try:
                                bot.send_message(
                                    seller['telegram_id'],
                                    f"📦 *НОВЫЙ ЗАКАЗ {order_number}*\n\n"
                                    f"👤 Покупатель: {buyer_name_escaped}\n"
                                    f"📞 Телефон: {phone}\n"
                                    f"📱 Username: {username_display}\n"
                                    f"📍 {address}\n"
                                    f"📝 {order_text}\n\n"
                                    f"💬 Чтобы ответить покупателю, используйте `#{order_number} текст`",
                                    parse_mode='Markdown',
                                    reply_markup=markup
                                )
                                logger.info(f"✅ Уведомление успешно отправлено продавцу {seller['telegram_id']}")
                            except Exception as e:
                                logger.error(f"❌ Ошибка уведомления продавца {seller['telegram_id']}: {e}")
                            
                            # Отправляем копию админу с составом заказа
                            if ADMIN_ID and seller['telegram_id'] != ADMIN_ID:
                                try:
                                    bot.send_message(
                                        ADMIN_ID,
                                        f"🆕 *Новый заказ {order_number}*\n"
                                        f"Продавец: {seller['name']}\n"
                                        f"Покупатель: {buyer_name_escaped}\n"
                                        f"📞 Телефон: {phone}\n"
                                        f"📱 Username: {username_display}\n"
                                        f"📍 Адрес: {address}\n\n"
                                        f"📦 *Состав заказа:*\n{items_text}\n\n"
                                        f"💰 *Сумма: {total} руб.*",
                                        parse_mode='Markdown'
                                    )
                                    logger.info(f"✅ Уведомление админу отправлено с составом заказа")
                                except Exception as e:
                                    logger.error(f"❌ Ошибка уведомления админа: {e}")
                            
                            cur.execute("UPDATE orders SET notified_bool = TRUE WHERE id = %s", (existing['id'],))
                            conn.commit()
                        return jsonify({'status': 'ok', 'orderNumber': order_number}), 200

        # Генерация номера для нового заказа с учётом типа доставки
        order_number = generate_order_number(seller['id'], delivery)

        # Получаем address_id только для самовывоза
        address_id = None
        if delivery == 'pickup':
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT id FROM pickup_locations WHERE address = %s", (address,))
                    addr = cur.fetchone()
                    address_id = addr['id'] if addr else None
                    logger.info(f"address_id для {address}: {address_id}")

        if not contact:
            contact = {
                'name': buyer_name,
                'phone': '0000000000',
                'address': address,
                'paymentMethod': payment,
                'deliveryType': delivery
            }

        order_data = {
            'order_number': order_number,
            'user_id': user_id,
            'seller_id': seller['id'],
            'address_id': address_id,
            'items': items,
            'total': total,
            'status': 'active',
            'delivery_type': delivery
        }

        order_id = save_order(order_data, contact, request_id)
        logger.info(f"Заказ {order_number} сохранён с ID {order_id} (seller_id={seller['id']})")

        # Формируем текст с учётом вариантов
        items_lines = []
        for item in items:
            item_name = f"{item['name']} ({item['variantName']})" if item.get('variantName') else item['name']
            items_lines.append(f"• {item_name} x{item['quantity']} = {item['price']*item['quantity']} руб.")
        items_text = "\n".join(items_lines)

        delivery_text = "Самовывоз" if delivery == 'pickup' else "Доставка"
        order_text = f"{items_text}\n\nСумма: {total} руб.\nОплата: {'Наличные' if payment=='cash' else 'Перевод'}\nДоставка: {delivery_text}"

        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("✅ Завершить", callback_data=f"complete_{order_number}"),
            types.InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_{order_number}")
        )

        phone = contact.get('phone', 'не указан')
        username = contact.get('username', 'не указан')
        
        # Экранируем специальные символы
        username_escaped = escape_markdown(username) if username else ''
        username_display = f"@{username_escaped}" if username else "@не указан"
        buyer_name_escaped = escape_markdown(buyer_name)

        try:
            bot.send_message(
                seller['telegram_id'],
                f"📦 *НОВЫЙ ЗАКАЗ {order_number}*\n\n"
                f"👤 Покупатель: {buyer_name_escaped}\n"
                f"📞 Телефон: {phone}\n"
                f"📱 Username: {username_display}\n"
                f"📍 {address}\n"
                f"📝 {order_text}\n\n"
                f"💬 Чтобы ответить покупателю, используйте `#{order_number} текст`",
                parse_mode='Markdown',
                reply_markup=markup
            )
            logger.info(f"✅ Уведомление успешно отправлено продавцу {seller['telegram_id']}")
        except Exception as e:
            logger.error(f"❌ Ошибка уведомления продавца {seller['telegram_id']}: {e}")

        # Копия админу с составом заказа
        if ADMIN_ID and seller['telegram_id'] != ADMIN_ID:
            try:
                bot.send_message(
                    ADMIN_ID,
                    f"🆕 *Новый заказ {order_number}*\n"
                    f"Продавец: {seller['name']}\n"
                    f"Покупатель: {buyer_name_escaped}\n"
                    f"📞 Телефон: {phone}\n"
                    f"📱 Username: {username_display}\n"
                    f"📍 Адрес: {address}\n\n"
                    f"📦 *Состав заказа:*\n{items_text}\n\n"
                    f"💰 *Сумма: {total} руб.*",
                    parse_mode='Markdown'
                )
                logger.info(f"✅ Уведомление админу отправлено с составом заказа")
            except Exception as e:
                logger.error(f"❌ Ошибка уведомления админа: {e}")

        # ========== ПОДТВЕРЖДЕНИЕ ПОКУПАТЕЛЮ ==========
        try:
            bot.send_message(
                user_id,
                f"✅ *Ваш заказ {order_number} принят!*\n\n"
                f"📝 *Состав заказа:*\n{items_text}\n\n"
                f"💳 Оплата: {'Наличные' if payment=='cash' else 'Перевод'}\n"
                f"🚚 Доставка: {delivery_text}\n"
                f"📍 Адрес: {address}\n\n"
                f"📅 Дата: {datetime.now().strftime('%d %B')}\n"
                f"👤 Username: {username_display}\n\n"
                f"💬 Вы можете общаться с продавцом в этом чате.",
                parse_mode='Markdown'
            )
            logger.info(f"✅ Подтверждение отправлено покупателю {user_id}")
        except Exception as e:
            logger.error(f"❌ Ошибка отправки подтверждения покупателю {user_id}: {e}")

        # Помечаем как уведомлённое
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE orders SET notified_bool = TRUE WHERE id = %s", (order_id,))
                conn.commit()

        return jsonify({'status': 'ok', 'orderNumber': order_number})

    except Exception as e:
        logger.exception("❌ Ошибка в /api/new-order")
        return jsonify({'error': str(e)}), 500

@app.route('/api/order-cancelled', methods=['POST'])
def order_cancelled():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data'}), 400

        order_id = data.get('orderId')
        order_number = data.get('orderNumber')
        user_id = data.get('userId')
        seller_id = data.get('sellerId')

        if not all([order_id, seller_id, order_number]):
            logger.error(f"Missing fields: orderId={order_id}, sellerId={seller_id}, orderNumber={order_number}")
            return jsonify({'error': 'Missing fields'}), 400

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT telegram_id, name FROM sellers WHERE id = %s", (seller_id,))
                seller = cur.fetchone()
                if not seller:
                    return jsonify({'error': 'Seller not found'}), 404
                seller_tg = seller['telegram_id']
                seller_name = seller['name']

        bot.send_message(
            seller_tg,
            f"❌ *Заказ {order_number} отменён покупателем.*",
            parse_mode='Markdown'
        )
        logger.info(f"Уведомление об отмене заказа {order_number} отправлено продавцу {seller_tg}")

        if ADMIN_ID and seller_tg != ADMIN_ID:
            try:
                bot.send_message(
                    ADMIN_ID,
                    f"❌ *Заказ {order_number} отменён покупателем.*\nПродавец: {seller_name}",
                    parse_mode='Markdown'
                )
                logger.info(f"Уведомление об отмене заказа {order_number} отправлено администратору")
            except Exception as e:
                logger.error(f"Ошибка отправки уведомления администратору: {e}")

        return jsonify({'status': 'ok'})

    except Exception as e:
        logger.exception("Ошибка в /api/order-cancelled")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    logger.info(f"Webhook set to {WEBHOOK_URL}")
    app.run(host='0.0.0.0', port=PORT, debug=False)
