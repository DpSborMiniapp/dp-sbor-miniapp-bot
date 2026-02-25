import os
import logging
from datetime import datetime
from flask import Flask, request, jsonify
import telebot
from telebot import types
from supabase import create_client
from dotenv import load_dotenv

# ==================== НАСТРОЙКА ====================
load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))
PORT = int(os.getenv('PORT', 10000))

if not all([BOT_TOKEN, SUPABASE_URL, SUPABASE_KEY]):
    raise ValueError("Не заданы обязательные переменные окружения")

bot = telebot.TeleBot(BOT_TOKEN)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== ФУНКЦИИ РАБОТЫ С БАЗОЙ ====================

def get_seller_by_address(address: str):
    addr = supabase.table('addresses').select('seller_id').eq('address', address).execute()
    if not addr.data:
        return None
    seller_id = addr.data[0]['seller_id']
    seller = supabase.table('sellers').select('*').eq('id', seller_id).execute()
    return seller.data[0] if seller.data else None

def generate_order_number(seller_name: str):
    first_letter = seller_name[0].upper()
    counter_res = supabase.table('order_counters').select('counter').eq('seller_letter', first_letter).execute()
    if counter_res.data:
        new_counter = counter_res.data[0]['counter'] + 1
        supabase.table('order_counters').update({'counter': new_counter}).eq('seller_letter', first_letter).execute()
    else:
        new_counter = 1
        supabase.table('order_counters').insert({'seller_letter': first_letter, 'counter': new_counter}).execute()
    return f"{first_letter}{new_counter}"

def save_order(order_data: dict):
    res = supabase.table('orders').insert(order_data).execute()
    return res.data[0] if res.data else None

def get_active_order_by_buyer(buyer_id: int):
    res = supabase.table('orders').select('*').eq('buyer_id', buyer_id).eq('status', 'active').execute()
    return res.data[0] if res.data else None

def get_active_orders_by_seller(seller_id: int):
    res = supabase.table('orders').select('*').eq('seller_id', seller_id).eq('status', 'active').execute()
    return res.data

def get_order_by_number(order_number: str):
    res = supabase.table('orders').select('*').eq('order_number', order_number).execute()
    return res.data[0] if res.data else None

def complete_order(order_id: int):
    supabase.table('orders').update({
        'status': 'completed',
        'completed_at': datetime.utcnow().isoformat()
    }).eq('id', order_id).execute()

def save_message(order_id: int, sender_id: int, sender_role: str, text: str):
    data = {
        'order_id': order_id,
        'sender_id': sender_id,
        'sender_role': sender_role,
        'text': text
    }
    supabase.table('messages').insert(data).execute()

def get_seller_by_telegram_id(telegram_id: int):
    res = supabase.table('sellers').select('*').eq('telegram_id', telegram_id).execute()
    return res.data[0] if res.data else None

def is_admin(telegram_id: int) -> bool:
    return telegram_id == ADMIN_ID

# ==================== ОБРАБОТЧИКИ TELEGRAM ====================

@bot.message_handler(commands=['start'])
def handle_start(message):
    bot.reply_to(message, "👋 Добро пожаловать! Здесь будут ваши заказы и общение с продавцом.")

# ----- ПОКУПАТЕЛИ -----
@bot.message_handler(func=lambda m: get_active_order_by_buyer(m.from_user.id) is not None)
def handle_buyer_message(message):
    """Если у пользователя есть активный заказ, пересылаем сообщение продавцу"""
    user_id = message.from_user.id
    order = get_active_order_by_buyer(user_id)
    if not order:
        return

    save_message(order['id'], user_id, 'buyer', message.text)

    seller_id = order['seller_id']
    seller_info = supabase.table('sellers').select('telegram_id').eq('id', seller_id).execute().data
    if seller_info:
        seller_tg = seller_info[0]['telegram_id']
        try:
            bot.send_message(
                seller_tg,
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

# ----- ПРОДАВЦЫ -----
@bot.message_handler(func=lambda m: get_seller_by_telegram_id(m.from_user.id) is not None)
def handle_seller_message(message):
    user_id = message.from_user.id
    text = message.text.strip()

    if not text.startswith('#'):
        # Если нет #, показываем список активных заказов
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

# ----- КНОПКА ДЛЯ ПРОДАВЦА (ТОЛЬКО ЗАВЕРШИТЬ) -----
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

    # Уведомляем покупателя
    try:
        bot.send_message(
            order['buyer_id'],
            f"✅ Ваш заказ {order_num} выполнен. Спасибо за покупку!"
        )
    except Exception as e:
        logger.error(f"Ошибка уведомления покупателя: {e}")

    # Уведомляем админа
    if ADMIN_ID:
        bot.send_message(
            ADMIN_ID,
            f"✅ Продавец {seller['name']} завершил заказ {order_num}."
        )

    # Убираем кнопки из сообщения продавца
    try:
        bot.edit_message_reply_markup(
            user_id,
            call.message.message_id,
            reply_markup=None
        )
    except:
        pass

# ----- ОСТАЛЬНЫЕ ПОЛЬЗОВАТЕЛИ -----
@bot.message_handler(func=lambda m: True)
def fallback_handler(message):
    bot.reply_to(message, "Используйте кнопки или начните новый заказ в нашем мини-аппе.")

# ==================== FLASK-ЭНДПОИНТ ДЛЯ МИНИ-АППА ====================

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
            return jsonify({'error': 'Seller not found for this address'}), 404

        order_number = generate_order_number(seller['name'])

        items_text = "\n".join([
            f"• {item['name']} x{item['quantity']} = {item['price']*item['quantity']} руб."
            for item in items
        ])
        order_text = f"{items_text}\n\nСумма: {total} руб.\nОплата: {'Наличные' if payment=='cash' else 'Перевод'}\nДоставка: {delivery}"

        order_data = {
            'order_number': order_number,
            'buyer_id': buyer_id,
            'buyer_name': buyer_name,
            'seller_id': seller['id'],
            'address_id': None,
            'items': items,
            'total': total,
            'payment_method': payment,
            'delivery_type': delivery,
            'status': 'active'
        }
        saved_order = save_order(order_data)
        if not saved_order:
            return jsonify({'error': 'Failed to save order'}), 500

        # Уведомление продавцу
        seller_tg = seller['telegram_id']
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("✅ Завершить", callback_data=f"complete_{order_number}"))

        try:
            bot.send_message(
                seller_tg,
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

# ==================== ЗАПУСК ====================

if __name__ == '__main__':
    bot.remove_webhook()
    app.run(host='0.0.0.0', port=PORT, debug=False)