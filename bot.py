import os
import logging
from datetime import datetime
from flask import Flask, request, jsonify
import telebot
from telebot import types
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))
PORT = int(os.getenv('PORT', 10000))

if not BOT_TOKEN or not DATABASE_URL:
    raise ValueError("Не заданы BOT_TOKEN или DATABASE_URL")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

def generate_order_number(seller_name: str):
    first_letter = seller_name[0].upper()
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT counter FROM order_counters WHERE seller_letter = %s", (first_letter,))
            counter = cur.fetchone()
            if counter:
                new_counter = counter['counter'] + 1
                cur.execute("UPDATE order_counters SET counter = %s WHERE seller_letter = %s", (new_counter, first_letter))
            else:
                new_counter = 1
                cur.execute("INSERT INTO order_counters (seller_letter, counter) VALUES (%s, %s)", (first_letter, new_counter))
            conn.commit()
            return f"{first_letter}{new_counter}"

def save_order(order_data: dict):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO orders (order_number, buyer_id, buyer_name, seller_id, address_id, items, total, payment_method, delivery_type, status)
                VALUES (%(order_number)s, %(buyer_id)s, %(buyer_name)s, %(seller_id)s, %(address_id)s, %(items)s, %(total)s, %(payment_method)s, %(delivery_type)s, %(status)s)
                RETURNING id
            """, order_data)
            order_id = cur.fetchone()['id']
            conn.commit()
            return order_id

def get_active_order_by_buyer(buyer_id: int):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM orders WHERE buyer_id = %s AND status = 'active'", (buyer_id,))
            return cur.fetchone()

def get_active_orders_by_seller(seller_id: int):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM orders WHERE seller_id = %s AND status = 'active'", (seller_id,))
            return cur.fetchall()

def get_order_by_number(order_number: str):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM orders WHERE order_number = %s", (order_number,))
            return cur.fetchone()

def complete_order(order_id: int):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE orders SET status = 'completed', completed_at = %s WHERE id = %s", (datetime.utcnow().isoformat(), order_id))
            conn.commit()

def save_message(order_id: int, sender_id: int, sender_role: str, text: str):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO messages (order_id, sender_id, sender_role, text)
                VALUES (%s, %s, %s, %s)
            """, (order_id, sender_id, sender_role, text))
            conn.commit()

def get_seller_by_telegram_id(telegram_id: int):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM sellers WHERE telegram_id = %s", (telegram_id,))
            return cur.fetchone()

def is_admin(telegram_id: int) -> bool:
    return telegram_id == ADMIN_ID

# ---------- Обработчики Telegram ----------

@bot.message_handler(commands=['start'])
def handle_start(message):
    bot.reply_to(message, "👋 Добро пожаловать! Здесь будут ваши заказы и общение с продавцом.")

# Покупатели
@bot.message_handler(func=lambda m: get_active_order_by_buyer(m.from_user.id) is not None)
def handle_buyer_message(message):
    user_id = message.from_user.id
    order = get_active_order_by_buyer(user_id)
    if not order:
        return
    save_message(order['id'], user_id, 'buyer', message.text)
    seller_id = order['seller_id']
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT telegram_id FROM sellers WHERE id = %s", (seller_id,))
            seller = cur.fetchone()
    if seller:
        try:
            bot.send_message(
                seller['telegram_id'],
                f"💬 Сообщение от покупателя (заказ {order['order_number']}):\n\n{message.text}"
            )
        except Exception as e:
            logger.error(f"Ошибка отправки продавцу: {e}")
    if ADMIN_ID:
        bot.send_message(
            ADMIN_ID,
            f"📩 [Копия] Покупатель {order['buyer_name']} (заказ {order['order_number']}):\n{message.text}"
        )
    bot.reply_to(message, "✅ Сообщение отправлено продавцу.")

# Продавцы
@bot.message_handler(func=lambda m: get_seller_by_telegram_id(m.from_user.id) is not None)
def handle_seller_message(message):
    user_id = message.from_user.id
    text = message.text.strip()
    if not text.startswith('#'):
        seller = get_seller_by_telegram_id(user_id)
        if not seller:
            return
        orders = get_active_orders_by_seller(seller['id'])
        if orders:
            order_list = "\n".join([f"• Заказ {o['order_number']} – {o['buyer_name']}" for o in orders])
            bot.reply_to(
                message,
                f"📋 Ваши активные заказы:\n{order_list}\n\n"
                "Чтобы ответить покупателю, начните сообщение с #номера_заказа, например:\n"
                "`#А1 Здравствуйте! Ваш заказ будет готов через час`"
            )
        else:
            bot.reply_to(message, "У вас нет активных заказов.")
        return
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
        seller = get_seller_by_telegram_id(user_id)
        if not seller or order['seller_id'] != seller['id']:
            bot.reply_to(message, "❌ Этот заказ не ваш.")
            return
        save_message(order['id'], user_id, 'seller', reply_text)
        try:
            bot.send_message(
                order['buyer_id'],
                f"💬 Сообщение от продавца (заказ {order_num}):\n\n{reply_text}"
            )
        except Exception as e:
            logger.error(f"Ошибка отправки покупателю: {e}")
        if ADMIN_ID:
            bot.send_message(
                ADMIN_ID,
                f"📩 [Копия] Продавец {seller['name']} (заказ {order_num}):\n{reply_text}"
            )
        bot.reply_to(message, f"✅ Сообщение отправлено покупателю (заказ {order_num}).")
    except Exception as e:
        logger.error(f"Ошибка обработки сообщения продавца: {e}")
        bot.reply_to(message, "❌ Ошибка. Используйте формат: #А1 текст сообщения")

# Кнопка завершения
@bot.callback_query_handler(func=lambda call: call.data.startswith('complete_'))
def handle_seller_complete(call):
    user_id = call.from_user.id
    order_num = call.data.split('_')[1]
    order = get_order_by_number(order_num)
    if not order:
        bot.answer_callback_query(call.id, "❌ Заказ не найден")
        return
    seller = get_seller_by_telegram_id(user_id)
    if not seller or order['seller_id'] != seller['id']:
        bot.answer_callback_query(call.id, "❌ Заказ не ваш")
        return
    complete_order(order['id'])
    bot.answer_callback_query(call.id, "✅ Заказ завершён")
    try:
        bot.send_message(
            order['buyer_id'],
            f"✅ Ваш заказ {order_num} выполнен. Спасибо за покупку!"
        )
    except Exception as e:
        logger.error(f"Ошибка уведомления покупателя: {e}")
    if ADMIN_ID:
        bot.send_message(
            ADMIN_ID,
            f"✅ Продавец {seller['name']} завершил заказ {order_num}."
        )
    try:
        bot.edit_message_reply_markup(user_id, call.message.message_id, reply_markup=None)
    except:
        pass

# Остальные пользователи
@bot.message_handler(func=lambda m: True)
def fallback_handler(message):
    bot.reply_to(message, "Используйте кнопки или начните новый заказ в нашем мини-аппе.")

# ---------- Flask-эндпоинт для приёма заказов ----------
@app.route('/api/new-order', methods=['POST'])
def new_order():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data'}), 400
        buyer_id = data.get('userId')
        buyer_name = data.get('name', 'Покупатель')
        items = data.get('items')
        total = data.get('total')
        address = data.get('address')
        payment = data.get('paymentMethod')
        delivery = data.get('deliveryType')
        if not all([buyer_id, items, total, address]):
            return jsonify({'error': 'Missing required fields'}), 400
        seller = get_seller_by_address(address)
        if not seller:
            logger.error(f"Не найден продавец для адреса {address}")
            return jsonify({'error': 'Seller not found'}), 404
        order_number = generate_order_number(seller['name'])
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM pickup_locations WHERE address = %s", (address,))
                addr = cur.fetchone()
                address_id = addr['id'] if addr else None
        order_data = {
            'order_number': order_number,
            'buyer_id': buyer_id,
            'buyer_name': buyer_name,
            'seller_id': seller['id'],
            'address_id': address_id,
            'items': items,
            'total': total,
            'payment_method': payment,
            'delivery_type': delivery,
            'status': 'active'
        }
        order_id = save_order(order_data)
        items_text = "\n".join([f"• {item['name']} x{item['quantity']} = {item['price']*item['quantity']} руб." for item in items])
        order_text = f"{items_text}\n\nСумма: {total} руб.\nОплата: {'Наличные' if payment=='cash' else 'Перевод'}\nДоставка: {delivery}"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("✅ Завершить", callback_data=f"complete_{order_number}"))
        try:
            bot.send_message(
                seller['telegram_id'],
                f"📦 *НОВЫЙ ЗАКАЗ {order_number}*\n\n"
                f"👤 Покупатель: {buyer_name}\n"
                f"📍 {address}\n"
                f"📝 {order_text}\n\n"
                f"💬 Чтобы ответить покупателю, используйте `#{order_number} текст`",
                parse_mode='Markdown',
                reply_markup=markup
            )
        except Exception as e:
            logger.error(f"Ошибка уведомления продавца: {e}")
        if ADMIN_ID:
            bot.send_message(
                ADMIN_ID,
                f"🆕 *Новый заказ {order_number}*\n"
                f"Продавец: {seller['name']}\n"
                f"Покупатель: {buyer_name}\n"
                f"Адрес: {address}\n"
                f"Сумма: {total} руб.",
                parse_mode='Markdown'
            )
        logger.info(f"Заказ {order_number} сохранён и отправлен продавцу {seller['name']}")
        return jsonify({'status': 'ok', 'orderNumber': order_number})
    except Exception as e:
        logger.exception("Ошибка в /api/new-order")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    bot.remove_webhook()
    app.run(host='0.0.0.0', port=PORT, debug=False)
