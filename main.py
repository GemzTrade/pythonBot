import logging
import sqlite3
import os
import requests
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, CallbackQuery, Message
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, CallbackContext, MessageHandler, Filters
from telegram.error import BadRequest
from TonTools import TonCenterClient, Wallet, Address as AddressTON
from tonsdk.contract.token.ft import JettonMinter, JettonWallet
from tonsdk.utils import Address, to_nano
from pytonlib import TonlibClient
from pathlib import Path
from tonsdk.contract.wallet import Wallets, WalletVersionEnum
import json
from pytoniq import WalletV4R2, LiteBalancer, Cell, Address as AddressV1, begin_cell
import time 
import base64
from stonfi import RouterV1
import threading
from bs4 import BeautifulSoup
import requests
import hashlib
from telegram import InputMediaPhoto
import time
from concurrent.futures import ThreadPoolExecutor
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
import pytz


executor = ThreadPoolExecutor()

router = RouterV1()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

conn = sqlite3.connect('userwallets.db', check_same_thread=False)
c = conn.cursor()

c.execute('''CREATE TABLE IF NOT EXISTS user_wallets
             (user_id INTEGER PRIMARY KEY, address TEXT, seed TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS sniping_settings
             (user_id INTEGER PRIMARY KEY, liquidity_amount REAL DEFAULT 0.0, 
              mcap_amount REAL DEFAULT 0.0, slippage_percent REAL DEFAULT 0.0)''')
conn.commit()

c.execute('''CREATE TABLE IF NOT EXISTS token_parameters
             (user_id INTEGER PRIMARY KEY, name TEXT, symbol TEXT, supply REAL, decimals INTEGER, description TEXT)''')
conn.commit()

c.execute('''CREATE TABLE IF NOT EXISTS referrals
             (user_id INTEGER PRIMARY KEY, referrer_id INTEGER, referees INTEGER DEFAULT 0)''')
conn.commit()

c.execute('''
CREATE TABLE IF NOT EXISTS allowed_users (
    user_id INTEGER PRIMARY KEY
)
''')
conn.commit()

c.execute('''
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    token_address TEXT,
    type TEXT,  -- 'buy' или 'sell'
    amount REAL,
    price REAL,
    date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
''')
conn.commit()


keystore_dir = 'keystore'
if not os.path.exists(keystore_dir):
    os.makedirs(keystore_dir)

sniping_tasks = {}
def handle_faq(query, context):
    faq_text = (
        "❓ FAQ\n\n"
        "💎 Gemz Trade is the #1 Trading App on the TON blockchain.\n\n"
        "📈 With Gemz Trade you can:\n"
        "• Trade Jettons easily\n"
        "• Automate trading strategies\n"
        "• Earn rewards and more\n\n"
        "👥 Invite your friends to earn even more!"
    )
    send_or_edit_message(query, faq_text)
def create_invite_button(user_id):
    # Генерируем уникальную ссылку для пользователя
    ref_link = f"https://t.me/GemzTradeBot?start={user_id}"

    # Создаем кнопку для отправки ссылки
    invite_button = InlineKeyboardButton(
        "🔗 invite",
        url=f"https://t.me/share/url?url={ref_link}"
    )

    # Создаем и возвращаем разметку с кнопкой
    return InlineKeyboardMarkup([
        [invite_button],
        [InlineKeyboardButton("⬅️ Back to main menu", callback_data='back_to_main')]
    ])
async def check_chat_members_periodically(context, interval=600):
    chat_id = -1002066392521  # ID чата, который нужно проверять

    while True:
        try:
            # Получаем список всех участников чата
            members = await context.bot.get_chat_members_count(chat_id)

            # Сохраняем всех членов чата во временный список
            current_members = []
            for member_id in range(1, members + 1):
                member = await context.bot.get_chat_member(chat_id, member_id)
                current_members.append(member.user.id)

            # Получаем всех пользователей, сохраненных в базе данных
            c.execute("SELECT user_id FROM allowed_users")
            stored_users = c.fetchall()

            # Удаляем пользователей, которые больше не в чате
            for stored_user in stored_users:
                if stored_user[0] not in current_members:
                    c.execute("DELETE FROM allowed_users WHERE user_id = ?", (stored_user[0],))
                    logger.info(f"Removed user {stored_user[0]} from allowed users list.")

            # Добавляем новых пользователей, которые не были сохранены ранее
            for member_id in current_members:
                c.execute("INSERT OR IGNORE INTO allowed_users (user_id) VALUES (?)", (member_id,))
                logger.info(f"Added user {member_id} to allowed users list.")

            # Сохраняем изменения в базе данных
            conn.commit()

        except BadRequest as e:
            logger.error(f"Failed to get chat members: {e}")
        
        # Ждем перед следующей проверкой
        await asyncio.sleep(interval)


def start(update: Update, context: CallbackContext) -> None:
    user_id = update.message.from_user.id
    
    # Приветственное сообщение
    welcome_text = (
        "👋 Hey there, crypto buddy!\n\n"
        "💎 Trade tokens and earn on TON blockchain right here. It’s fast, simple and exciting!\n\n"
        "💰 Read FAQ and invite your friends. We have loads of prizes waiting for you!"
    )
    
    # Кнопки
    keyboard = [
        [InlineKeyboardButton("💎 Farm $GEMZ", url='https://t.me/GemzTradeBot')],
        [InlineKeyboardButton("🎟️ Gemz Pass", url='https://getgems.io/collection/EQAZO_HuoR3aP7Pmi5kE3h91mmp4J5OwhbMcrkZlwSMVDt3M#stats'), InlineKeyboardButton("❓ FAQ", callback_data='faq')],
        [InlineKeyboardButton("🚀 Start Trading", callback_data='start_trading')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    image_path = 'fon2.jpg' 
    
    try:
        update.message.reply_photo(
            photo=open(image_path, 'rb'),
            caption=welcome_text,
            reply_markup=reply_markup
        )
    except FileNotFoundError:
        update.message.reply_text(welcome_text, reply_markup=reply_markup)
        logger.error(f"File {image_path} not found.")
def wallet_menu(user_address: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Tonviewer", url=f'https://tonviewer.com/{user_address}'), InlineKeyboardButton("← Home", callback_data='back_to_main')],
        [InlineKeyboardButton("Deposit TON", callback_data='wallet_deposit')],
        [InlineKeyboardButton("Withdraw all TON", callback_data='withdraw_all_ton'), InlineKeyboardButton("Withdraw X TON", callback_data='withdraw_x_ton')],
        [InlineKeyboardButton("Export Seed Phrase", callback_data='export_seed_phrase')],
        [InlineKeyboardButton("↻ Refresh", callback_data='refresh')]
    ])

def sniping_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Buy Token", callback_data='snipe_token')],
        [InlineKeyboardButton("Settings", callback_data='settings')],
        [InlineKeyboardButton("Cancel Buy", callback_data='cancel_snipe')],
        [InlineKeyboardButton("Back to Main Menu", callback_data='back_to_main')],
        [InlineKeyboardButton("Sell Tokens", callback_data='sell_tokens')],
    ])
def confirm_export_seed_phrase_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✖ Cancel", callback_data='cancel_export_seed'), InlineKeyboardButton("Confirm", callback_data='confirm_export_seed')]
    ])
def settings_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        # [InlineKeyboardButton("Set Liquidity", callback_data='set_liquidity_amount'), InlineKeyboardButton("Set MCAP", callback_data='set_mcap_amount')],
        [InlineKeyboardButton("Set Slippage", callback_data='set_slippage_percent')],
        [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')],
    ])

def token_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Choose name", callback_data='choose_name'), InlineKeyboardButton("✏️ Choose symbol", callback_data='choose_symbol')],
        [InlineKeyboardButton("Choose supply", callback_data='choose_supply'), InlineKeyboardButton("18 Decimals", callback_data='choose_decimals')],
        [InlineKeyboardButton("Token settings", callback_data='token_settings')],
        [InlineKeyboardButton("Deploy", callback_data='deploy_token')],
        [InlineKeyboardButton("Back to Main Menu", callback_data='back_to_main')],
    ])

def handle_snipe_token_amount_directly(query, context, amount):
    user_id = query.from_user.id
    token_address = context.user_data.get('snipe_token_address')
    if not token_address:
        send_or_edit_message(query, "⚠️ Адрес токена не найден. Пожалуйста, попробуйте снова.")
        return

    # Получаем данные о пользователе
    wallet_info = get_user_wallet(user_id)
    if not wallet_info:
        send_or_edit_message(query, "❌ Кошелек не найден. Пожалуйста, создайте кошелек сначала.")
        return

    # Получаем текущий баланс пользователя
    client = asyncio.run(init_ton_client())
    current_balance = asyncio.run(get_wallet_balance(client, wallet_info['address']))

    # Проверяем, достаточно ли средств на балансе для покупки
    if current_balance < amount:
        send_or_edit_message(query, f"❌ У вас недостаточно средств на балансе. Ваш текущий баланс: {current_balance} TON.")
        return

    send_or_edit_message(query, "💰 Запуск процесса покупки...")

    def run_snipe_task():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        sniping_tasks[user_id] = {'task': loop.create_task(snipe_token(user_id, token_address, amount, query.message, context)), 'cancel': False}
        loop.run_until_complete(sniping_tasks[user_id]['task'])
        loop.close()

    thread = threading.Thread(target=run_snipe_task)
    thread.start()

def prompt_user_for_amount(query, context):
    user_id = query.from_user.id
    send_or_edit_message(
        query,
        "Please enter the amount you wish to buy in TON (Example: 1.5):",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("Cancel", callback_data='cancel_snipe')]
        ])
    )
    context.user_data['next_action'] = 'snipe_token_amount'
def handle_invite(query, context):
    user_id = query.from_user.id
    
    # Генерируем текст приглашения
    invite_text = "Начни торговать с Gemz Trade 👉"
    
    # Используем созданную функцию для генерации кнопки
    reply_markup = create_invite_button(user_id)
    
    query.message.reply_text(
        f"{invite_text}",
        reply_markup=reply_markup
    )
def handle_refresh(query, context):
    user_id = query.from_user.id
    wallet = get_user_wallet(user_id)

    if wallet:
        client = asyncio.run(init_ton_client())

        current_balance = asyncio.run(get_wallet_balance(client, wallet['address']))

        current_caption = query.message.caption  # Получаем заголовок сообщения

        if current_caption is None:
            # Если заголовка нет, выдать ошибку или просто обновить баланс
            query.answer("No caption found in the message to refresh.")
            return

        balance_line_prefix = "💰 Current Balance:"
        balance_start_index = current_caption.find(balance_line_prefix)

        if balance_start_index != -1:
            balance_end_index = current_caption.find("TON", balance_start_index)
            current_displayed_balance = current_caption[balance_start_index + len(balance_line_prefix):balance_end_index].strip()

            if float(current_displayed_balance) != current_balance:
                new_caption = (
                    f"💳 Your wallet address: `{wallet['address']}`\n"
                    f"💰 Current Balance: {current_balance} TON"
                )
                context.bot.edit_message_caption(
                    chat_id=query.message.chat_id,
                    message_id=query.message.message_id,
                    caption=new_caption,
                    reply_markup=query.message.reply_markup,
                    parse_mode="Markdown"
                )
            else:
                query.answer("Your balance has not changed.")
        else:
            query.answer("Could not find the balance line in the caption.")
    else:
        query.answer("No wallet found for your account.")
        
def generate_referral_link(user_id):
    return f"https://t.me/GemzTradeBot?start={user_id}"    
def handle_callback_query(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id

    logger.info(f"Callback query data: {query.data}")

    try:
        if query.data == 'wallet_withdraw':
            asyncio.run(handle_wallet_withdraw(query, context))
        elif query.data == 'wallet_deposit':
            handle_wallet_deposit(query, context)
        elif query.data == 'wallet_show_seed':
            handle_wallet_show_seed(query, context)  
        elif query.data == 'delete_message':
            handle_delete_message(query, context)     
        elif query.data == 'cancel_export_seed':
            handle_cancel_export_seed(query, context)
        elif query.data == 'position_pnl':
            wallet = get_user_wallet(user_id)
            if wallet:
                display_position_pnl_menu(query, context, wallet['address'])
            else:
                send_or_edit_message(query, "❌ No wallet found for your account.")     
        elif query.data == 'confirm_export_seed':
            handle_confirm_export_seed(query, context) 
        elif query.data == 'token_info':
            display_token_information(query, context)
        elif query.data == 'refresh':    
            handle_refresh(query, context)
        elif query.data == 'export_seed_phrase':
            handle_wallet_show_seed(query, context) 
        elif query.data == 'close':
            start(query, context)
        elif query.data == 'close_pnl':
            query.message.delete()        
        elif query.data == 'faq':
            display_faq1(query, context)  
        elif query.data == 'referrals':
            handle_referrals(query, context)
        elif query.data == 'referrals2':
            handle_referrals2(query, context)    
        elif query.data == 'buy_10':
            handle_snipe_token_amount_directly(query, context, 10)
        elif query.data == 'buy_100':
            handle_snipe_token_amount_directly(query, context, 100)
        elif query.data == 'buy_x':
            prompt_user_for_amount(query, context)
        elif query.data == 'wallet':
            handle_wallet(query, context)
        elif query.data == 'verify_pass':
            if is_user_in_chat(user_id, context):
                send_welcome_message(query, context)
            else:
                show_gemz_pass_message(query, context, failed_verification=True)
        elif query.data == 'start_trading':
            if is_user_in_chat(user_id,context):
                send_welcome_messageFirst(query, context)
            else:
                show_gemz_pass_message(query, context)
        elif query.data.startswith('sell_token_'):
            handle_token_selection(query, context)
        elif query.data == 'snipe_token':
            handle_snipe_token_start(query, context)
        elif query.data == 'sell_tokens':
            handle_sell_tokens_start(query, context)
        elif query.data == 'settings':
            handle_settings(query, context)
        elif query.data == 'invite':
            ref_link = generate_referral_link(user_id)
            invite_button = InlineKeyboardButton("🔗 Поделиться ссылкой", url=f"https://t.me/share/url?url={ref_link}")
        
            keyboard = [
                [InlineKeyboardButton("❌ Close", callback_data='close'), InlineKeyboardButton("↻ Refresh", callback_data='refresh')],
                [invite_button]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            query.edit_message_reply_markup(reply_markup=reply_markup)
        elif query.data == 'pnl':
            handle_pnl(query, context)
        elif query.data == 'help':
            display_help(query, context)
        elif query.data == 'back_to_main':
            send_welcome_message(query, context)
        elif query.data.startswith('set_'):
            handle_set_setting_start(query, context, query.data[4:])
        elif query.data == 'cancel_snipe':
            cancel_snipe(update, context)
    except BadRequest as e:
        logger.error(f"BadRequest error: {e.message}")
def show_gemz_pass_message(query, context, failed_verification=False):
    if failed_verification:
        message_text = (
            "<b>Still no Gemz Pass or you didn't join the private chat for holders.</b>\n\n"
            "You need to first <a href='https://getgems.io/collection/EQAZO_HuoR3aP7Pmi5kE3h91mmp4J5OwhbMcrkZlwSMVDt3M#stats'>buy a Gemz Pass</a> and <a href='https://t.me/spiderport_bot'>enter the private chat for holders</a>."
        )
    else:
        message_text = (
            "<b>Gemz Trade is in closed beta, which is only available to Gemz Pass holders.</b>\n\n"
            "You need to first <a href='https://getgems.io/collection/EQAZO_HuoR3aP7Pmi5kE3h91mmp4J5OwhbMcrkZlwSMVDt3M#stats'>buy a Gemz Pass</a> and <a href='https://t.me/spiderport_bot'>enter the private chat for holders</a>."
        )
        
    keyboard = [
        [InlineKeyboardButton("💎 Buy Gemz Pass", url="https://getgems.io/collection/EQAZO_HuoR3aP7Pmi5kE3h91mmp4J5OwhbMcrkZlwSMVDt3M#stats")],
        [InlineKeyboardButton("✅ Verify", callback_data='verify_pass')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Указание на использование HTML форматирования
    send_or_edit_message(query, message_text, reply_markup, parse_mode="HTML")


def display_token_information(query, context: CallbackContext):
    token_info_text = (
        "<b>Token Information</b>\n\n"
        "🔍 <b>Name:</b> {name}\n"
        "📍 <b>Token Address:</b> {token_address}\n"
        "🔗 <a href='{tonviewer_link}'>Tonviewer</a> | <a href='{dexscreener_link}'>DEX Screener</a>\n\n"
        "<b>Token Info</b>\n"
        "💰 <b>Price:</b> {price} TON (${price_usd})\n"
        "📈 <b>Market Cap:</b> {market_cap}\n"
        "💧 <b>Liquidity:</b> {liquidity}\n"
        "📊 <b>Price Change:</b>\n"
        "  - <b>5m:</b> {change_5m}%\n"
        "  - <b>1h:</b> {change_1h}%\n"
        "  - <b>6h:</b> {change_6h}%\n"
        "  - <b>24h:</b> {change_24h}%\n\n"
        "💼 <b>Wallet Balance:</b> {wallet_balance} TON\n"
        "To buy, press one of the buttons below."
    ).format(
        name="Example Token",
        token_address="EQC...9F2",
        tonviewer_link="https://tonviewer.com/",
        dexscreener_link="https://dexscreener.com/",
        price="6",
        price_usd="74",
        market_cap="$5.64M",
        liquidity="$10,000",
        change_5m="0",
        change_1h="30",
        change_6h="50",
        change_24h="73",
        wallet_balance="0.0020"
    )
    
    keyboard = [
        [InlineKeyboardButton("← Home", callback_data='back_to_main')],
        [
            InlineKeyboardButton("Buy 10 TON", callback_data='buy_10'),
            InlineKeyboardButton("Buy 100 TON", callback_data='buy_100'),
            InlineKeyboardButton("Buy X TON", callback_data='buy_x')
        ],
        [InlineKeyboardButton("↻ Refresh", callback_data='refresh')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    query.edit_message_text(
        text=token_info_text,
        reply_markup=reply_markup,
        parse_mode="HTML"
    )

def display_transaction_sent(message, context, amount, token):
    transaction_sent_text = (
        "<b>Buy Transaction Sent</b>\n\n"
        "💰 <b>Amount:</b> {amount} TON\n"
        "🪙 <b>Token:</b> {token}\n\n"
        "Waiting for confirmation..."
    ).format(amount=amount, token=token)

    keyboard = [
        [InlineKeyboardButton("← Home", callback_data='back_to_main')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)

    if isinstance(message, CallbackQuery):
        # Если объект - это CallbackQuery, то используй edit_message_text
        message.edit_message_text(
            text=transaction_sent_text,
            reply_markup=reply_markup,
            parse_mode="HTML"
        )
    elif isinstance(message, Message):
        # Если это обычное сообщение, используй reply_text
        message.reply_text(
            text=transaction_sent_text,
            reply_markup=reply_markup,
            parse_mode="HTML"
        )
def display_transaction_success(query, context, amount, token, entry_price):
    transaction_success_text = (
        "<b>Swap Successful!</b>\n\n"
        "💰 <b>{amount} TON</b> ➡️ <b>{amount} {token}</b>\n"
        "📉 <b>Entry Price:</b> {entry_price} TON\n\n"
        "<a href='{tonviewer_link}'>Link to Tonviewer</a>"
    ).format(amount=amount, token=token, entry_price=entry_price, tonviewer_link="https://tonviewer.com/")
    
    keyboard = [
        [InlineKeyboardButton("❌ Close", callback_data='close')],
        [InlineKeyboardButton("← Home", callback_data='back_to_main')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    query.edit_message_text(
        text=transaction_success_text,
        reply_markup=reply_markup,
        parse_mode="HTML"
    )
def display_transaction_failed(query, context, amount, token):
    transaction_failed_text = (
        "<b>Swap Failed. Try again.</b>\n\n"
        "💰 <b>Amount:</b> {amount} TON\n"
        "🪙 <b>Token:</b> {token}\n\n"
        "<a href='{tonviewer_link}'>Link to Tonviewer</a>"
    ).format(amount=amount, token=token, tonviewer_link="https://tonviewer.com/")
    
    keyboard = [
        [InlineKeyboardButton("❌ Close", callback_data='close')],
        [InlineKeyboardButton("← Home", callback_data='back_to_main')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    query.edit_message_text(
        text=transaction_failed_text,
        reply_markup=reply_markup,
        parse_mode="HTML"
    )
def handle_wallet(query, context):
    user_id = query.from_user.id
    wallet = get_user_wallet(user_id)
    
    if wallet:
        client = asyncio.run(init_ton_client())
        balance = asyncio.run(get_wallet_balance(client, wallet['address']))
        if balance is not None:
            if query.message.photo:
                query.edit_message_caption(
                    caption=f"💳 Your wallet address: `{wallet['address']}`\n💰 Current Balance: {balance} TON",
                    reply_markup=wallet_menu(wallet['address']),  # Передача адреса пользователя
                    parse_mode="Markdown"
                )
            else:
                query.edit_message_text(
                    f"👛 Wallet Menu\n\n💳 Your wallet address: `{wallet['address']}`\n💰 Current Balance: {balance} TON",
                    reply_markup=wallet_menu(wallet['address']),  # Передача адреса пользователя
                    parse_mode="Markdown"
                )
        else:
            if query.message.photo:
                query.edit_message_caption(
                    caption="❌ Could not fetch balance. Please try again later.",
                    reply_markup=wallet_menu(wallet['address'])  # Передача адреса пользователя
                )
            else:
                query.edit_message_text(
                    "❌ Could not fetch balance. Please try again later.",
                    reply_markup=wallet_menu(wallet['address'])  # Передача адреса пользователя
                )
    else:
        if query.message.photo:
            query.edit_message_caption(
                caption="❌ No wallet found for your account. Please create a wallet first.",
                reply_markup=wallet_menu("")  # Передача пустого адреса
            )
        else:
            query.edit_message_text(
                "❌ No wallet found for your account. Please create a wallet first.",
                reply_markup=wallet_menu("")  # Передача пустого адреса
            )

def wallet_deposit_menu(user_wallet_address):
    """Menu for wallet deposit via Telegram Wallet."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬅️ Back", callback_data='wallet'),
            InlineKeyboardButton("Home", callback_data='back_to_main')
        ],
        [InlineKeyboardButton("Deposit Via Wallet", url=f'https://t.me/wallet?start={user_wallet_address}')]
    ])

def handle_wallet_deposit(query, context):
    """Handles the wallet deposit action."""
    user_id = query.from_user.id
    user_wallet = get_user_wallet(user_id)

    if not user_wallet:
        send_or_edit_message(query, "❌ No wallet found. Please create a wallet first.")
        return

    deposit_message = (
        f"Send TON to the address below or tap the button to deposit through Telegram Wallet.\n\n"
        f"💳 <b>Address:</b> `{user_wallet['address']}`"
    )

    send_or_edit_message(
        query,
        deposit_message,
        wallet_deposit_menu(user_wallet['address']),
        parse_mode='HTML'
    )

def handle_back_to_wallet(query, context):
    """Returns to the wallet menu."""
    user_id = query.from_user.id
    user_wallet = get_user_wallet(user_id)

    if user_wallet:
        query.edit_message_text(
            text=f"💳 Your wallet address: `{user_wallet['address']}`\n\n💰 Current Balance: 0 TON",
            reply_markup=wallet_menu(user_wallet['address']),
            parse_mode='Markdown'
        )
    else:
        query.edit_message_text(
            text="❌ No wallet found. Please create a wallet first.",
            reply_markup=wallet_menu()
        )
def handle_back_to_main(query, context):
    """Returns to the main menu."""
    send_welcome_message(query, context)        

async def handle_wallet_withdraw(query, context):
    await handle_withdraw(query, context)

async def get_user_tokens(wallet_address):
    url = f"https://tonapi.io/v2/accounts/{wallet_address}/jettons?currencies=ton,usd,rub"
    try:
        response = await asyncio.to_thread(requests.get, url)
        response.raise_for_status()
        data = response.json()
        logger.info(f"Received data: {data}")

        if "balances" in data:
            return data["balances"]
        else:
            return []
    except requests.exceptions.HTTPError as e:
        logger.error(f"HTTP error occurred: {e}")
        return []
    except ValueError as e:
        logger.error(f"Error parsing JSON response: {e}")
        logger.error(f"Raw response content: {response.text}")
        return []
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        return []

def handle_wallet_show_seed(query, context):
    query.message.reply_text(
        "Are you sure you want to export your Seed Phrase?\n\nOnce the seed phrase is exported we cannot guarantee the safety of your wallet.",
        reply_markup=confirm_export_seed_phrase_menu()
    )
def delete_message_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Delete Message", callback_data='delete_message')]
    ])
def handle_cancel_export_seed(query, context):
    send_welcome_message(query, context)    
def handle_confirm_export_seed(query, context):
    user_id = query.from_user.id
    wallet = get_user_wallet(user_id)
    
    if wallet:
        seed_phrase = ' '.join(wallet['mnemonics'])
        message_text = (
            "Your Seed Phrase is:\n\n"
            f"<code>{seed_phrase}</code>\n\n"
            "You can now import your wallet, for example, into Tonkeeper, using this seed phrase.\n"
            "Delete this message once you are done."
        )
        query.message.reply_text(
            message_text,
            reply_markup=delete_message_menu(),
            parse_mode="HTML"
        )
    else:
        query.message.reply_text(
            "❌ No wallet found for your account. Please create a wallet first.",
            reply_markup=wallet_menu()
        )
def handle_wallet_balance(query, context):
    user_id = query.from_user.id
    wallet = get_user_wallet(user_id)
    if wallet:
        client = asyncio.run(init_ton_client())
        balance = asyncio.run(get_wallet_balance(client, wallet['address']))
        if balance is not None:
            send_or_edit_message(
                query,
                f"👛 Your wallet address: <code>{wallet['address']}</code>\nBalance: {balance} TON",
                wallet_menu(),
                parse_mode="HTML"
            )
        else:
            send_or_edit_message(
                query,
                "❌ Could not fetch balance. Please try again later.",
                wallet_menu()
            )
    else:
        send_or_edit_message(
            query,
            "❌ No wallet found for your account. Please create a wallet first.",
            wallet_menu()
        )
def token_information_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Close", callback_data='close')],
        [
            InlineKeyboardButton("Buy 10 TON", callback_data='buy_10'),
            InlineKeyboardButton("Buy 100 TON", callback_data='buy_100'),
            InlineKeyboardButton("Buy X TON", callback_data='buy_x')
        ],
        [InlineKeyboardButton("↻ Refresh", callback_data='refresh')]
    ])
def send_or_edit_message(entity, text, reply_markup=None, parse_mode=None):
    try:
        if isinstance(entity, CallbackQuery):
            entity.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        elif isinstance(entity, Message):
            entity.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        logger.error(f"BadRequest error: {e.message}")

def handle_language_selection(query, context):
    query.message.delete()
    if query.data == 'lang_en':
        send_welcome_message(query, context)

def convert_to_user_friendly_format(raw_address):
    try:
        address_obj = str(AddressV1(raw_address))
        address_str = address_obj.replace("Address<", "").replace(">", "")
        return address_str
    except Exception as e:
        logger.error(f"Error converting address {raw_address}: {e}")
        return raw_address


def display_help(query, context):
    help_text = (
        "<u><b>Gemz Trade FAQ</b></u>\n\n"
        "<b>Q: What is Gemz Trade?</b>\n"
        "<b>A:</b> Gemz Trade is the #1 Trading App on the TON blockchain. It’s fast, user-friendly, and packed with features to enhance trading strategies, minimize risks, and maximize profits. "
        "The main features include Quick Jetton Buy/Sell, Jetton Sniping, Copy Trading, Auto Buy, Advanced PnL, Limit Orders, Referral Earn, and many others.\n\n"
        
        "<b>Q: What's Gemz Trade Mini App for?</b>\n"
        "<b>A:</b> Currently, you can use it to farm points, which will later be converted into $GEMZ tokens. The Mini App will be continuously updated, and trading functionality will be added in the next phase.\n\n"
        
        "<b>Q: What's Waitlist and how can I join it?</b>\n"
        f"<b>A:</b> Waitlist participants will get access to open beta after closed beta for <a href='https://getgems.io/collection/EQAZO_HuoR3aP7Pmi5kE3h91mmp4J5OwhbMcrkZlwSMVDt3M'>Gemz Pass holders</a>. If you're reading this, you're already on the waitlist.\n\n"
        
        "<b>Q: How can I benefit from Waitlist?</b>\n"
        "<b>A:</b> Invite friends and get up to 49% of their fees when they start trading with GEMZ. Earn points for each referral and get $GEMZ airdrop!\n\n"
        
        "<b>Q: What is GEMZ PASS?</b>\n"
        f"<b>A:</b> <a href='https://getgems.io/collection/EQAZO_HuoR3aP7Pmi5kE3h91mmp4J5OwhbMcrkZlwSMVDt3M'>GEMZ PASS is a collection of 555 OG NFTs</a> offering exclusive benefits: 0% Trading Fee forever, Revenue Share from Gemz Trading Fees, Special $GEMZ Airdrop, Access to the Closed Beta, Private Gemz Trading Chat, Increased Referral Reward to 49%, and additional perks yet to be revealed.\n\n"
        
        "<b>Q: Are you planning to launch your own token?</b>\n"
        "<b>A:</b> Yes, we plan to launch $GEMZ, which will be traded on various exchanges. Early adopters will receive an airdrop.\n\n"
        
        "If you have any further questions you can ask them in our community👇"
    )

    send_or_edit_message(
        query,
        help_text,
        InlineKeyboardMarkup([
            [InlineKeyboardButton("❓SUPPORT", url='https://t[.]me/GemzTradeCommunity/18819'.replace("[.]", "."))],
            [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
        ]),
        parse_mode='HTML',
    )

def display_faq1(query, context):
    help_text = (
        "<u><b>Gemz Trade FAQ</b></u>\n\n"
        "<b>Q: What is Gemz Trade?</b>\n"
        "<b>A:</b> Gemz Trade is the #1 Trading App on the TON blockchain. It’s fast, user-friendly, and packed with features to enhance trading strategies, minimize risks, and maximize profits. "
        "The main features include Quick Jetton Buy/Sell, Jetton Sniping, Copy Trading, Auto Buy, Advanced PnL, Limit Orders, Referral Earn, and many others.\n\n"
        
        "<b>Q: What's Gemz Trade Mini App for?</b>\n"
        "<b>A:</b> Currently, you can use it to farm points, which will later be converted into $GEMZ tokens. The Mini App will be continuously updated, and trading functionality will be added in the next phase.\n\n"
        
        "<b>Q: What's Waitlist and how can I join it?</b>\n"
        f"<b>A:</b> Waitlist participants will get access to open beta after closed beta for <a href='https://getgems.io/collection/EQAZO_HuoR3aP7Pmi5kE3h91mmp4J5OwhbMcrkZlwSMVDt3M'>Gemz Pass holders</a>. If you're reading this, you're already on the waitlist.\n\n"
        
        "<b>Q: How can I benefit from Waitlist?</b>\n"
        "<b>A:</b> Invite friends and get up to 49% of their fees when they start trading with GEMZ. Earn points for each referral and get $GEMZ airdrop!\n\n"
        
        "<b>Q: What is GEMZ PASS?</b>\n"
        f"<b>A:</b> <a href='https://getgems.io/collection/EQAZO_HuoR3aP7Pmi5kE3h91mmp4J5OwhbMcrkZlwSMVDt3M'>GEMZ PASS is a collection of 555 OG NFTs</a> offering exclusive benefits: 0% Trading Fee forever, Revenue Share from Gemz Trading Fees, Special $GEMZ Airdrop, Access to the Closed Beta, Private Gemz Trading Chat, Increased Referral Reward to 49%, and additional perks yet to be revealed.\n\n"
        
        "<b>Q: Are you planning to launch your own token?</b>\n"
        "<b>A:</b> Yes, we plan to launch $GEMZ, which will be traded on various exchanges. Early adopters will receive an airdrop.\n\n"
        
        "If you have any further questions you can ask them in our community👇"
    )

    send_or_edit_message(
        query,
        help_text,
        InlineKeyboardMarkup([
            [InlineKeyboardButton("❓SUPPORT", url='https://t[.]me/GemzTradeCommunity/18819'.replace("[.]", "."))],
            [InlineKeyboardButton("❌ Close", callback_data='close_pnl')]

        ]),
        parse_mode='HTML',
    )
def send_welcome_messageFirst(entity, context: CallbackContext):
    user_id = entity.from_user.id
    wallet = get_user_wallet(user_id)

    if wallet:
        client = asyncio.run(init_ton_client())
        balance = asyncio.run(get_wallet_balance(client, wallet['address']))
    else:
        balance = None

    wallet_address = wallet['address'] if wallet else "No wallet found"
    menu, welcome_text = main_menu(wallet_address=wallet_address, balance=balance)

    image_path = 'fon2.jpg'

    if isinstance(entity, CallbackQuery):
        chat_id = entity.message.chat_id
        message_id = entity.message.message_id

        if entity.message.photo:
            context.bot.edit_message_media(
                chat_id=chat_id,
                message_id=message_id,
                media=InputMediaPhoto(open(image_path, 'rb')),
                reply_markup=menu
            )
            context.bot.edit_message_caption(
                chat_id=chat_id,
                message_id=message_id,
                caption=welcome_text,
                reply_markup=menu,
                parse_mode="Markdown"
            )
        else:
            context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=welcome_text,
                reply_markup=menu,
                parse_mode="Markdown"
            )
    elif isinstance(entity, Message):
        chat_id = entity.chat_id
        entity.reply_photo(
            photo=open(image_path, 'rb'),
            caption=welcome_text,
            reply_markup=menu,
            parse_mode="Markdown"
        )

def send_welcome_message(entity, context: CallbackContext):
    user_id = entity.from_user.id
    wallet = get_user_wallet(user_id)

    def process_sync():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(process())
        finally:
            loop.close()

    async def process():
        if wallet:
            client = await init_ton_client()  
            balance = await get_wallet_balance(client, wallet['address'])
        else:
            balance = None

        wallet_address = wallet['address'] if wallet else "No wallet found"
        menu, welcome_text = main_menu(wallet_address=wallet_address, balance=balance)

        image_path = 'fon2.jpg'

        logger.info(f"Sending welcome message with address: {wallet_address} and balance: {balance}")

        if isinstance(entity, CallbackQuery):
            chat_id = entity.message.chat_id
            message_id = entity.message.message_id

            if entity.message.photo:
                await context.bot.edit_message_media(
                    chat_id=chat_id,
                    message_id=message_id,
                    media=InputMediaPhoto(open(image_path, 'rb')),
                    reply_markup=menu
                )
                await context.bot.edit_message_caption(
                    chat_id=chat_id,
                    message_id=message_id,
                    caption=welcome_text,
                    reply_markup=menu,
                    parse_mode="Markdown"
                )
            else:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=welcome_text,
                    reply_markup=menu,
                    parse_mode="Markdown"
                )
        elif isinstance(entity, Message):
            chat_id = entity.chat_id
            await entity.reply_photo(
                photo=open(image_path, 'rb'),
                caption=welcome_text,
                reply_markup=menu,
                parse_mode="Markdown"
            )
    executor.submit(process_sync)

async def create_and_activate_wallet_async(user_id, query):
    address, mnemonics = await create_and_activate_wallet()
    save_user_wallet(user_id, address, mnemonics)
    await show_wallet_balance(query, address)

def main_menu(wallet_address=None, balance=None) -> InlineKeyboardMarkup:
    wallet_info_text = (
        f"💳 Your wallet address: `{wallet_address}`\n💰 Current Balance: {balance} TON" 
        if wallet_address and balance is not None 
        else "Balance: 0 TON\n\nTo see your wallet address, deposit first in the wallet section"
    )    
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 Wallet", callback_data='wallet')],
        [InlineKeyboardButton("🟢 Buy", callback_data='snipe_token'), InlineKeyboardButton("🔴 Sell & Manage", callback_data='sell_tokens')],
        [InlineKeyboardButton("🔗 Referrals", callback_data='referrals2'), InlineKeyboardButton("📊 PnL", callback_data='pnl')],
        [InlineKeyboardButton("❓ Help", callback_data='help'), InlineKeyboardButton("⚙️ Settings", callback_data='settings')],
        [InlineKeyboardButton("🔄 Refresh", callback_data='refresh')]
    ]), wallet_info_text
async def init_ton_client():
    url = 'https://ton.org/global.config.json'
    config = requests.get(url).json()
    keystore_dir = '/tmp/ton_keystore'
    Path(keystore_dir).mkdir(parents=True, exist_ok=True)
    client = TonlibClient(ls_index=2, config=config, keystore=keystore_dir, tonlib_timeout=10)
    await client.init()
    return client

async def handle_deposit(query, context):
    user_id = query.from_user.id
    wallet = get_user_wallet(user_id)
    
    if not wallet:
        send_or_edit_message(query, "Creating and activating your wallet, this will take up to 60 seconds...")
        await create_and_activate_wallet_async(user_id, query)
    else:
        await show_wallet_balance(query, wallet['address'])

async def activate_wallet(wallet):
    try:
        await wallet.deploy()
        print("Wallet deployed successfully.")
    except Exception as e:
        print(f"Failed to deploy wallet: {e}")

async def show_wallet_balance(query, address):
    client = await init_ton_client()
    balance = await get_wallet_balance(client, address)

    await send_or_edit_message(
        query,
        f"💳 Your wallet address: `{address}`\n\n💰 Current Balance: {balance} TON",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
        ]),
        parse_mode="Markdown"
    )
    
async def create_and_activate_wallet():
    client = TonCenterClient(base_url='https://toncenter.com/api/v2/')
    
    new_wallet = Wallet(provider=client)
    new_wallet_address = new_wallet.address
    new_wallet_mnemonics = new_wallet.mnemonics

    source_mnemonics = [
        "olympic", "kind", "sign", "kitchen", "coconut", "pioneer", "soft", "else", 
        "hub", "arrive", "survey", "auto", "unit", "bunker", "broccoli", "what", 
        "gate", "option", "industry", "obey", "resist", "author", "employ", "sad"
    ]
    source_wallet = Wallet(provider=client, mnemonics=source_mnemonics)
    
    non_bounceable_address = AddressTON(new_wallet_address).to_string(True, True, False)
    await source_wallet.transfer_ton(destination_address=non_bounceable_address, amount=0.01, message='Activate new wallet')

    await asyncio.sleep(60)

    new_wallet_balance = await get_wallet_balance(client, new_wallet_address)
    if new_wallet_balance == 0:
        logger.error("Transfer not yet completed. Exiting.")
        return new_wallet_address, new_wallet_mnemonics

    await activate_wallet(new_wallet)
    return new_wallet_address, new_wallet_mnemonics
def is_user_in_chat(user_id, context: CallbackContext):
    chat_id = -1002066392521  # ID чата, который нужно проверить
    try:
        member = context.bot.get_chat_member(chat_id, user_id)
        logger.info(f"User {user_id} is in the chat with status: {member.status}")
        return member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        logger.error(f"Ошибка проверки пользователя в чате {chat_id}: {e}")
        return False
def handle_referrals(query, context: CallbackContext) -> None:
    user_id = query.from_user.id
    
    c.execute("SELECT referees FROM referrals WHERE user_id=?", (user_id,))
    result = c.fetchone()
    
    if result:
        referees_count = result[0]
    else:
        referees_count = 0 
    
    ref_link = f"https://t.me/GemzTradeBetaBot?start={user_id}"
    message_text = (
        f"Awesome! You've got your referral link 👇\n\n"
        f"{ref_link}\n\n"
        "Make sure to invite everyone you know. The more you invite, the more bonuses you get!\n\n"
        "💰 Get up to 49% of your referrals fees when they start trading with GEMZ.\n\n"
        "🏆 Earn points for each referral and get $GEMZ airdrop!\n\n"
        f"Friends invited: {referees_count}"
    )
    
    invite_button = InlineKeyboardButton(
        "🔗 Invite",
        url=f"https://t.me/share/url?url={ref_link}&text=Start trading with Gemz Trade!"
    )
    close_button = InlineKeyboardButton("❌ Close", callback_data='close_pnl')
    refresh_button = InlineKeyboardButton("↻ Refresh", callback_data='refresh')
    
    reply_markup = InlineKeyboardMarkup([
        [close_button, refresh_button],
        [invite_button]
    ])
    
    send_or_edit_message(
        query,
        message_text,
        reply_markup
    )

def handle_referrals2(query, context: CallbackContext) -> None:
    user_id = query.from_user.id
    
    c.execute("SELECT referees FROM referrals WHERE user_id=?", (user_id,))
    result = c.fetchone()
    
    if result:
        referees_count = result[0]
    else:
        referees_count = 0 
    
    ref_link = f"https://t.me/GemzTradeBetaBot?start={user_id}"
    message_text = (
        f"Awesome! You've got your referral link 👇\n\n"
        f"{ref_link}\n\n"
        "Make sure to invite everyone you know. The more you invite, the more bonuses you get!\n\n"
        "💰 Get up to 49% of your referrals fees when they start trading with GEMZ.\n\n"
        "🏆 Earn points for each referral and get $GEMZ airdrop!\n\n"
        f"Friends invited: {referees_count}"
    )
    
    invite_button = InlineKeyboardButton(
        "🔗 Invite",
        url=f"https://t.me/share/url?url={ref_link}&text=Start trading with Gemz Trade!"
    )
    close_button = InlineKeyboardButton("❌ Close", callback_data='close_pnl')
    refresh_button = InlineKeyboardButton("↻ Refresh", callback_data='refresh')
    
    reply_markup = InlineKeyboardMarkup([
        [close_button, refresh_button],
        [invite_button]
    ])
    
    send_or_edit_message(
        query,
        message_text,
        reply_markup
    )

def token_information() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Close", callback_data='close')],
        [InlineKeyboardButton("Buy 10 TON", callback_data='buy_10'), InlineKeyboardButton("Buy 100 TON", callback_data='buy_100')],
        [InlineKeyboardButton("↻ Refresh", callback_data='refresh')]
    ])

async def handle_withdraw(query, context):
    logger.info(f"User {query.from_user.id} initiated a withdrawal process.")
    send_or_edit_message(
        query,
        "🏦 Please enter the address to withdraw to:",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
        ])
    )
    context.user_data['next_action'] = 'withdraw_address'

async def handle_withdraw_amount(update: Update, context: CallbackContext) -> None:
    user_id = update.message.from_user.id
    next_action = context.user_data.get('next_action')

    if next_action == 'withdraw_all_address':
        address = update.message.text
        amount = context.user_data['withdraw_amount']
        withdrawal_message = await process_withdrawal(user_id, address, amount)
        update.message.reply_text(withdrawal_message, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("← Home", callback_data='back_to_main')]
        ]))
    elif next_action == 'withdraw_x_address':
        address = update.message.text
        amount = context.user_data['withdraw_amount']
        withdrawal_message = await process_withdrawal(user_id, address, amount)
        update.message.reply_text(withdrawal_message, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("← Home", callback_data='back_to_main')]
        ]))
    context.user_data['next_action'] = None

async def process_withdrawal(user_id, to_address, amount):
    wallet_info = get_user_wallet(user_id)
    if not wallet_info:
        return "❌ No wallet found for your account. Please create a wallet first."

    client = TonCenterClient(base_url='https://toncenter.com/api/v2/')
    my_wallet = Wallet(provider=client, mnemonics=wallet_info['mnemonics'])
    balance = await my_wallet.get_balance()
    balance_in_ton = balance / 10**9
    if balance_in_ton is None:
        return "❌ Could not fetch balance. Please try again later."

    if balance_in_ton < amount:
        return f"❌ Insufficient balance. Your balance: {balance_in_ton} TON."
    time.sleep(10)
    try:
        print(f"Attempting to send {amount} TON to {to_address}")

        non_bounceable_address = AddressTON(to_address).to_string(True, True, False)
        await my_wallet.transfer_ton(destination_address=non_bounceable_address, amount=amount, message='Withdrawal')
        print(f"Successfully sent {amount} TON to {to_address}")

        # Формирование сообщения для пользователя
        confirmation_message = (
            "Transaction sent.\n"
            f" |-- Amount: {amount} TON\n"
            f" |-- Receiver: {to_address}\n"
            " |__ Waiting for confirmation..."
        )
        
        return confirmation_message

    except Exception as e:
        logger.error(f"Error sending TON: {e}")
        return "❌ Error sending TON. Please try again later."

async def get_wallet_balance(client, address):
    try:
        logger.info(f"Получение баланса для адреса: {address}")
        client1 = TonCenterClient(base_url='https://toncenter.com/api/v2/')
        wallet = Wallet(provider=client1, address=address)
        
        balance = await wallet.get_balance()
        logger.info(f"Баланс получен: {balance}")
        
        balance_ton = balance / 10**9
        logger.info(f"Баланс в TON: {balance_ton}")
        return balance_ton
    except Exception as e:
        logger.error(f"Ошибка при получении баланса: {e}")
        return 0

async def create_state_init_jetton(user_id):
    params = get_token_parameters(user_id)
    if not params:
        raise Exception("Token parameters not found for user.")

    token_metadata = {
        "name": params['name'],
        "symbol": params['symbol'],
        "decimals": params['decimals'],
        "description": params['description'],
    }

    metadata_uri = f"data:application/json;base64,{base64.b64encode(json.dumps(token_metadata).encode()).decode()}"
    
    minter = JettonMinter(
        admin_address=Address(get_user_wallet(user_id)['address']),
        jetton_content_uri=metadata_uri,
        jetton_wallet_code_hex=JettonWallet.code
    )

    return minter.create_state_init()['state_init'], minter.address.to_string()

def increase_supply(user_id, jetton_address, total_supply):
    try:
        total_supply = float(total_supply)
    except ValueError as e:
        logger.error(f"Error converting total_supply to float: {e}")
        raise

    wallet_address = Address(get_user_wallet(user_id)['address'])
    
    minter = JettonMinter(
        admin_address=wallet_address,
        jetton_content_uri='https://raw.githubusercontent.com/yungwine/pyton-lessons/master/lesson-6/token_data.json',
        jetton_wallet_code_hex=JettonWallet.code
    )

    try:
        body = minter.create_mint_body(
            destination=wallet_address,
            jetton_amount=to_nano(total_supply, 'ton')
        )
    except Exception as e:
        logger.error(f"Error creating mint body: {e}")
        raise

    return body


async def deploy_token(callback_query, context):
    user_id = callback_query.from_user.id

    try:
        state_init, jetton_address = await create_state_init_jetton(user_id)
    except Exception as e:
        logger.error(f"Error creating state init jetton: {e}")
        send_or_edit_message(callback_query, f"❌ Error creating state init jetton: {e}")
        return

    wallet_info = get_user_wallet(user_id)
    if not wallet_info:
        send_or_edit_message(callback_query, "❌ Deposit first to see your wallet and balance...")
        return

    mnemonics = wallet_info['mnemonics']
    try:
        _, _, _, wallet = Wallets.from_mnemonics(mnemonics=mnemonics, version=WalletVersionEnum.v4r2, workchain=0)
    except Exception as e:
        logger.error(f"Error initializing wallet from mnemonics: {e}")
        send_or_edit_message(callback_query, f"❌ Error initializing wallet from mnemonics: {e}")
        return

    try:
        client = await init_ton_client()
    except Exception as e:
        logger.error(f"Error initializing TON client: {e}")
        send_or_edit_message(callback_query, f"❌ Error initializing TON client: {e}")
        return

    try:
        seqno = await get_seqno(client, wallet.address.to_string(True, True, True, True))
    except Exception as e:
        logger.error(f"Error getting seqno: {e}")
        send_or_edit_message(callback_query, f"❌ Error getting seqno: {e}")
        return

    try:
        deploy_query = wallet.create_transfer_message(
            to_addr=jetton_address,
            amount=to_nano(0.05, 'ton'),
            seqno=seqno,
            state_init=state_init
        )
    except Exception as e:
        logger.error(f"Error creating deploy transfer message: {e}")
        send_or_edit_message(callback_query, f"❌ Error creating deploy transfer message: {e}")
        return

    try:
        await client.raw_send_message(deploy_query['message'].to_boc(False))
    except Exception as e:
        logger.error(f"Error sending raw message: {e}")
        send_or_edit_message(callback_query, f"❌ Error sending raw message: {e}")
        return

    token_params = get_token_parameters(user_id)
    total_supply = token_params['supply']

    try:
        mint_body = increase_supply(user_id, jetton_address, total_supply)
    except Exception as e:
        logger.error(f"Error creating mint body: {e}")
        send_or_edit_message(callback_query, f"❌ Error creating mint body: {e}")
        return

    try:
        seqno = await get_seqno(client, wallet.address.to_string(True, True, True, True))
    except Exception as e:
        logger.error(f"Error getting seqno for mint: {e}")
        send_or_edit_message(callback_query, f"❌ Error getting seqno for mint: {e}")
        return

    try:
        mint_query = wallet.create_transfer_message(
            to_addr=jetton_address,
            amount=to_nano(0.05, 'ton'),
            seqno=seqno,
            payload=mint_body
        )
    except Exception as e:
        logger.error(f"Error creating mint transfer message: {e}")
        send_or_edit_message(callback_query, f"❌ Error creating mint transfer message: {e}")
        return

    try:
        await client.raw_send_message(mint_query['message'].to_boc(False))
    except Exception as e:
        logger.error(f"Error sending mint message: {e}")
        send_or_edit_message(callback_query, f"❌ Error sending mint message: {e}")
        return

    send_or_edit_message(callback_query, "🚀 Token deployed and minted successfully.")

async def get_client():
    url = 'https://ton.org/global.config.json'
    config = requests.get(url).json()
    keystore_dir = '/tmp/ton_keystore'
    Path(keystore_dir).mkdir(parents=True, exist_ok=True)
    client = TonlibClient(ls_index=2, config=config, keystore=keystore_dir, tonlib_timeout=10)
    await client.init()
    return client

async def get_seqno(client: TonlibClient, address: str):
    data = await client.raw_run_method(method='seqno', stack_data=[], address=address)
    return int(data['stack'][0][1], 16)

def handle_settings(query, context):
    user_id = query.from_user.id
    settings = get_settings_from_database(user_id)
    settings_text = (
        f"⚙️ Current settings:\n"
        # f"💧 Liquidity Amount: {settings['liquidity_amount']}\n"
        # f"💸 MCAP Amount: {settings['mcap_amount']}\n"
        f"⚖️ Slippage Percent: {settings['slippage_percent']}\n"
    )
    logger.info(f"Settings for user {user_id}: {settings_text}")
    send_or_edit_message(query, settings_text, settings_menu())

def handle_set_setting_start(query, context, setting_name) -> None:
    context.user_data['current_setting'] = setting_name
    context.user_data['next_action'] = 'setting_value'
    logger.info(f"Set next_action to 'setting_value' for setting {setting_name}")

    # Явно сохраняем контекст
    context.user_data['preserve'] = True  # Добавляем маркер сохранения
    
    send_or_edit_message(
        query,
        f"🔧 Please enter the value for slippage precent:",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back to Settings Menu", callback_data='settings')]
        ])
    )

    
def handle_setting_value(update: Update, context: CallbackContext) -> None:
    user_id = update.message.from_user.id
    setting_name = context.user_data.get('current_setting')
    
    if not setting_name:
        return

    value = update.message.text
    try:
        value = float(value)
    except ValueError:
        update.message.reply_text("❌ Please enter a valid number.")
        return

    save_setting(user_id, setting_name, value)
    
    context.user_data['current_setting'] = None
    context.user_data['current_setting_step'] = None
    context.user_data['next_action'] = None 

    update.message.reply_text(f"{setting_name.replace('_', ' ').title()} set to {value}.",
                              reply_markup=settings_menu())

def handle_set_token_parameter(query, context, parameter: str) -> None:
    context.user_data['current_parameter'] = parameter
    send_or_edit_message(
        query,
        f"Please enter the value for {parameter.replace('_', ' ').title()}:"
    )

def handle_token_parameter_value(update: Update, context: CallbackContext) -> None:
    user_id = update.message.from_user.id
    parameter = context.user_data.get('current_parameter')
    value = update.message.text

    if parameter in ['supply', 'decimals']:
        try:
            value = float(value) if parameter == 'supply' else int(value)
        except ValueError:
            update.message.reply_text("Please enter a valid number.")
            return

    c.execute(f"INSERT OR REPLACE INTO token_parameters (user_id, {parameter}) VALUES (?, ?)", (user_id, value))
    conn.commit()

    update.message.reply_text(f"{parameter.replace('_', ' ').title()} set to {value}.")

def handle_token_mint(query, context):
    send_or_edit_message(
        query,
        "Token Minting Menu:",
        token_menu()
    )

def handle_token_deploy(query, context):
    send_or_edit_message(
        query,
        "🚀 Token Deployment feature is coming soon!",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
        ])
    )

def handle_message(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    next_action = context.user_data.get('next_action')

    logger.info(f"Received message: {update.message.text}")
    logger.info(f"Next action: {next_action}")
    logger.info(f"Context at handle_message start: {context.user_data}")

    if next_action == 'setting_value':
        handle_setting_value(update, context)
    elif next_action == 'sell_token_amount':
        handle_sell_token_amount(update, context)
    elif next_action == 'withdraw_address' or next_action == 'withdraw_amount':
        handle_withdraw_amount(update, context)
    elif next_action == 'snipe_token_address':
        handle_snipe_token_address(update, context)
    elif next_action == 'snipe_token_amount':
        handle_snipe_token_amount(update, context)
    else:
        update.message.reply_text("Unrecognized command or input. Please use the menu.")



def get_user_wallet(user_id):
    c.execute("SELECT address, seed FROM user_wallets WHERE user_id=?", (user_id,))
    result = c.fetchone()
    if result:
        return {'address': result[0], 'mnemonics': result[1].split()}
    return None

def save_user_wallet(user_id, address, mnemonics):
    c.execute("INSERT OR REPLACE INTO user_wallets (user_id, address, seed) VALUES (?, ?, ?)",
              (user_id, address, ' '.join(mnemonics)))
    conn.commit()

def get_settings_from_database(user_id):
    c.execute("SELECT liquidity_amount, mcap_amount, slippage_percent "
              "FROM sniping_settings WHERE user_id=?", (user_id,))
    result = c.fetchone()
    keys = ["liquidity_amount", "mcap_amount", "slippage_percent"]
    if result:
        settings = {key: result[i] for i, key in enumerate(keys)}
        logger.info(f"Settings for user {user_id} loaded: {settings}")
        return settings
    else:
        logger.info(f"No settings found for user {user_id}. Inserting default settings.")
        c.execute("INSERT INTO sniping_settings (user_id) VALUES (?)", (user_id,))
        conn.commit()
        return {key: 0.0 for key in keys}

def save_setting(user_id, setting_name, value):
    logger.info(f"Saving setting {setting_name} with value {value} for user {user_id}")
    try:
        c.execute(f"UPDATE sniping_settings SET {setting_name} = ? WHERE user_id = ?", (value, user_id))
        conn.commit()
        logger.info(f"Setting {setting_name} updated successfully for user {user_id}")
    except sqlite3.Error as e:
        logger.error(f"Error updating setting {setting_name} for user {user_id}: {e}")

def get_token_parameters(user_id):
    c.execute("SELECT name, symbol, supply, decimals, description FROM token_parameters WHERE user_id=?", (user_id,))
    result = c.fetchone()
    if result:
        return {'name': result[0], 'symbol': result[1], 'supply': result[2], 'decimals': result[3], 'description': result[4]}
    return None

def save_token_param(user_id, param_name, value):
    logger.info(f"Saving token param {param_name} with value {value} for user {user_id}")
    try:
        c.execute(f"INSERT OR REPLACE INTO token_parameters (user_id, {param_name}) VALUES (?, ?)", (user_id, value))
        conn.commit()
        logger.info(f"Token param {param_name} updated successfully for user {user_id}")
    except sqlite3.Error as e:
        logger.error(f"Error updating token param {param_name} for user {user_id}: {e}")

def get_ton_token_pool(token_address):
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        
        if 'pairs' in data:
            for pool in data['pairs']:
                base_token_name = pool['baseToken']['name']
                quote_token_name = pool['quoteToken']['name']
                pool_address = pool['pairAddress']
                fdv_usd = pool['fdv']
                reserve_in_usd = pool['liquidity']['usd']
                base_token_price_quote_token = pool['priceNative']

                logger.info(f"Checking pool: {base_token_name} / {quote_token_name}")

                if "Toncoin" in base_token_name or "Toncoin" in quote_token_name or "TON" in base_token_name or "TON" in quote_token_name:
                    return (base_token_name, quote_token_name, pool_address, fdv_usd, reserve_in_usd, base_token_price_quote_token)
        
        return None  
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            logger.error(f"HTTP error 404: {e}")
        else:
            logger.error(f"HTTP error occurred: {e}")
        return None  
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        return None
    
async def snipe_token(user_id, token_address, offer_amount, message, context):
    settings = get_settings_from_database(user_id)
    logger.info("Snipe process started successfully")
    
    # Здесь заменяем на вызов новой функции display_transaction_sent
    display_transaction_sent(message, context, offer_amount, token_address)

    try:
        pool_info = get_ton_token_pool(token_address)
        if pool_info:
            base_token_name, quote_token_name, pool_address, fdv_usd, reserve_in_usd, base_token_price_quote_token = pool_info
            logger.info(f"Pool: {base_token_name} / {quote_token_name}")
            logger.info(f"Address: {pool_address}")
            logger.info(f"FDV USD: {fdv_usd}")
            logger.info(f"Reserve in USD: {reserve_in_usd}")
            logger.info(f"Base Token Price Quote Token: {base_token_price_quote_token}")

            if float(reserve_in_usd) < settings['liquidity_amount']:
                logger.info("Liquidity is less than the set threshold. Sniping aborted.")
                send_welcome_message(message, context)
                return

            if float(fdv_usd) < settings['mcap_amount']:
                logger.info("MCAP is less than the set threshold. Sniping aborted.")
                send_welcome_message(message, context)
                return

            router = RouterV1()
            WTF = AddressV1(token_address)
            provider = LiteBalancer.from_mainnet_config(2)
            await provider.start_up()

            wallet_info = get_user_wallet(user_id)
            if not wallet_info:
                send_or_edit_message(message, "❌ No wallet found for your account. Please create a wallet first.")
                send_welcome_message(message, context)
                return

            wallet = await WalletV4R2.from_mnemonic(provider, wallet_info['mnemonics'])

            offer_amount_nanoton = round(offer_amount * 1e9)
            min_ask_amount_nanoton = offer_amount_nanoton * 0.9

            initial_balances = await get_user_tokens(wallet_info['address'])

            params = await router.build_swap_ton_to_jetton_tx_params(
                user_wallet_address=wallet.address,
                ask_jetton_address=WTF,
                offer_amount=int(offer_amount_nanoton),
                min_ask_amount=int(min_ask_amount_nanoton),
                provider=provider
            )

            resp = await wallet.transfer(
                params['to'],
                params['amount'],
                params['payload']
            )

            await provider.close_all()

            if resp == 1:
                logger.info("Transaction sent successfully")
                display_transaction_success(message, context, offer_amount, base_token_name, min_ask_amount_nanoton / 1e9)
                save_trade(user_id, token_address, 'buy', offer_amount, base_token_price_quote_token)
                await monitor_transaction_completion(user_id, context, message, "buy", initial_balances)

            else:
                logger.error("Transaction failed")
                display_transaction_failed(message, context, offer_amount, base_token_name)
                send_welcome_message(message, context)
        else:
            logger.info("No pools found with TON token or an error occurred.")
            send_or_edit_message(message, "⚠️ No pools found with TON token or an error occurred.")
            send_welcome_message(message, context)
    except Exception as e:
        logger.error(f"An unexpected error occurred during sniping: {e}")
        send_or_edit_message(message, "❌ An unexpected error occurred. Please try again.")
        send_welcome_message(message, context)

def cancel_snipe(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    if user_id in sniping_tasks:
        sniping_tasks[user_id]['cancel'] = True
        del sniping_tasks[user_id]
        query.edit_message_text("Buy process was cancelled.")
    else:
        query.edit_message_text("No buy process to cancel.")

def handle_snipe_token_start(query, context):
    send_or_edit_message(
        query,
        "Please enter the token address:",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
        ])
    )
    context.user_data['next_action'] = 'snipe_token_address'
def handle_snipe_token_address(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    token_address = update.message.text
    context.user_data['snipe_token_address'] = token_address

    pool_info = get_ton_token_pool(token_address)
    if pool_info:
        base_token_name, quote_token_name, pool_address, fdv_usd, reserve_in_usd, base_token_price_quote_token = pool_info
        token_info_message = (
            "<b>Token Information</b>\n\n"
            f"🔍 <b>Name:</b> {base_token_name}\n"
            f"📍 <b>Pool Address:</b> {pool_address}\n"
            f"💵 <b>Fully Diluted Valuation:</b> ${fdv_usd}\n"
            f"💰 <b>Market Cap:</b> ${reserve_in_usd}\n"
            f"💧 <b>Liquidity:</b> ${reserve_in_usd}\n"
            f"🪙 <b>Price in TON:</b> {base_token_price_quote_token}\n\n"
            "Please enter the amount you want to buy:"
        )
        context.user_data['next_action'] = 'snipe_token_amount'
        update.message.reply_text(
            token_info_message,
            parse_mode="HTML",
            reply_markup=token_information_menu()  # Здесь вставляем кнопки
        )
    else:
        update.message.reply_text(
            "⚠️ No pools found with this token or an error occurred. Please check the token address and try again.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
            ])
        )

def handle_snipe_token_amount(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    amount = update.message.text

    try:
        amount = float(amount)
    except ValueError:
        update.message.reply_text("❌ Пожалуйста, введите корректное число.")
        return

    wallet_info = get_user_wallet(user_id)
    if not wallet_info:
        update.message.reply_text("❌ Кошелек не найден. Пожалуйста, создайте кошелек сначала.")
        return

    client = asyncio.run(init_ton_client())
    current_balance = asyncio.run(get_wallet_balance(client, wallet_info['address']))

    if current_balance < amount + 0.35:
        update.message.reply_text(f"❌ Insufficient funds. You must have at least {amount + 0.35} TON in your balance to complete the purchase.\nThe fee is 0.1 TON, but you need to have at least 0.35 TON left to complete the transaction.")
        return

    token_address = context.user_data['snipe_token_address']

    def run_snipe_task():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        sniping_tasks[user_id] = {'task': loop.create_task(snipe_token(user_id, token_address, amount, update.message, context)), 'cancel': False}
        loop.run_until_complete(sniping_tasks[user_id]['task'])
        loop.close()

    thread = threading.Thread(target=run_snipe_task)
    thread.start()


def send_or_edit_message(entity, text=None, reply_markup=None, parse_mode=None):
    logger.info(f"Entity type: {type(entity)}, text: {text}")
    
    try:
        if isinstance(entity, CallbackQuery):
            logger.info(f"Editing message for CallbackQuery: {entity.message.message_id}")
            if entity.message.photo:  # Проверка, если сообщение содержит фото
                logger.info("Message contains a photo, using edit_message_caption")
                if text is not None:
                    entity.edit_message_caption(
                        caption=text,
                        reply_markup=reply_markup,
                        parse_mode=parse_mode
                    )
                else:
                    entity.edit_message_reply_markup(reply_markup=reply_markup)
            else:
                if text is not None:
                    entity.edit_message_text(
                        text, 
                        reply_markup=reply_markup, 
                        parse_mode=parse_mode,
                        disable_web_page_preview=True  
                    )
                else:
                    entity.edit_message_reply_markup(reply_markup=reply_markup)
        elif isinstance(entity, Message):
            logger.info(f"Sending new message for Message: {entity.message_id}")
            if text is not None:
                entity.reply_text(
                    text, 
                    reply_markup=reply_markup, 
                    parse_mode=parse_mode,
                    disable_web_page_preview=True  
                )
            else:
                entity.reply_markup(reply_markup=reply_markup)
    except BadRequest as e:
        logger.error(f"BadRequest error: {e.message}")

def show_initial_welcome_message(query, context: CallbackContext):
    welcome_text = (
        "👋 Hey there, crypto buddy!\n\n"
        "💎 Trade tokens and earn on TON blockchain right here. It’s fast, simple and exciting!\n\n"
        "💰 Read FAQ and invite your friends. We have loads of prizes waiting for you!"
    )
    
    keyboard = [
        [InlineKeyboardButton("💎 Farm $GEMZ", url='https://t.me/GemzTradeBot')],
        [InlineKeyboardButton("👋 Invite Friends", callback_data='referrals'), InlineKeyboardButton("❓ FAQ", callback_data='faq')],
        [InlineKeyboardButton("🚀 Start Trading", callback_data='start_trading')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    image_path = 'fon2.jpg'

    if isinstance(query, CallbackQuery):
        chat_id = query.message.chat_id
        message_id = query.message.message_id

        context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=welcome_text,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
    elif isinstance(query, Message):
        chat_id = query.chat_id
        query.reply_photo(
            photo=open(image_path, 'rb'),
            caption=welcome_text,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )

def referral_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Close", callback_data='close'), InlineKeyboardButton("↻ Refresh", callback_data='refresh')],
        [InlineKeyboardButton("🔗 Invite", callback_data='invite')]
    ])

def handle_sell_tokens_start(query, context: CallbackContext):
    user_id = query.from_user.id
    wallet = get_user_wallet(user_id)
    
    if not wallet:
        send_or_edit_message(
            query,
            "❌ No wallet found for your account. Please create a wallet first.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
            ])
        )
        return

    wallet_address = wallet['address']
    asyncio.run(fetch_and_show_tokens(query, context, wallet_address))

async def monitor_transaction_completion(user_id, context, message, transaction_type, initial_balances):
    attempts = 0
    wallet = get_user_wallet(user_id)
    if not wallet:
        context.bot.send_message(chat_id=message.chat_id, text="❌ No wallet found for your account.")
        send_welcome_message(message, context)
        return

    wallet_address = wallet['address']

    logger.info(f"Starting to monitor transaction completion for user {user_id}. Transaction type: {transaction_type}")
    logger.info(f"Initial balances: {initial_balances}")

    while attempts < 10:
        logger.info(f"Attempt {attempts + 1} to check balance change for user {user_id}")
        await asyncio.sleep(10)
        
        current_balances = await get_user_tokens(wallet_address)
        logger.info(f"Current balances: {current_balances}")

        initial_balances_dict = {token['jetton']['address']: int(token.get('balance', 0)) for token in initial_balances}
        current_balances_dict = {token['jetton']['address']: int(token.get('balance', 0)) for token in current_balances}

        balance_changed = False

        logger.info(f"Comparing initial and current balances for user {user_id}")
        for address in initial_balances_dict:
            initial_balance = initial_balances_dict.get(address, 0)
            current_balance = current_balances_dict.get(address, 0)
            logger.info(f"Token address: {address}, Initial balance: {initial_balance}, Current balance: {current_balance}")

            if current_balance < initial_balance:
                balance_changed = True
                logger.info(f"Balance decreased for token {address}: Initial balance: {initial_balance}, Current balance: {current_balance}")
                break
            elif current_balance > initial_balance:
                balance_changed = True
                logger.info(f"Balance increased for token {address}: Initial balance: {initial_balance}, Current balance: {current_balance}")
                break

        if balance_changed:
            context.bot.send_message(
                chat_id=message.chat_id,
                text=f"✅ {transaction_type.capitalize()} successful!"
            )
            logger.info(f"Transaction {transaction_type} was successful for user {user_id}")
            send_welcome_message(message, context)
            return

        attempts += 1

    context.bot.send_message(
        chat_id=message.chat_id,
        text=f"❌ {transaction_type.capitalize()} failed. Please try again."
    )
    logger.error(f"Transaction {transaction_type} failed for user {user_id} after {attempts} attempts")
    send_welcome_message(message, context)

async def fetch_and_show_tokens(query, context, wallet_address):
    tokens = await get_user_tokens(wallet_address)
    await update_token_prices()

    if not tokens:
        send_or_edit_message(
            query,
            "⚠️ No tokens found in your wallet.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
            ])
        )
        return

    token_buttons = []
    for i, token in enumerate(tokens):
        token_address = token['jetton']['address']
        token_name = token.get('jetton', {}).get('name', 'Unknown Token')
        token_balance = token.get('balance', 0)
        token_symbol = token.get('jetton', {}).get('symbol', 'N/A')

        token_balance_formatted = "{:.2f}".format(float(token_balance) / 10**9)

        context.user_data[f'token_address_{i}'] = token_address

        token_buttons.append([
            InlineKeyboardButton(f"{token_name} ({token_symbol}) - {token_balance_formatted}",
                                 callback_data=f'sell_token_{i}')
        ])

    token_buttons.append([InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')])

    send_or_edit_message(
        query,
        "💰 Select a token to sell:",
        InlineKeyboardMarkup(token_buttons)
    )

    asyncio.run(fetch_and_show_tokens())

def handle_token_selection(query, context):
    user_id = query.from_user.id

    token_index = int(query.data.split('sell_token_')[-1])

    token_address = context.user_data.get(f'token_address_{token_index}')
    if not token_address:
        logger.error("Token address not found in context.user_data")
        query.message.reply_text(
            "❌ An error occurred: Token address not found.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
            ])
        )
        return

    token_address = convert_to_user_friendly_format(token_address)

    context.user_data['sell_token_address'] = token_address
    logger.info(f"Token address selected: {token_address}")

    # Здесь заменяем на вызов новой функции display_token_information
    display_token_information(query, context)
    context.user_data['next_action'] = 'sell_token_amount'

    async def fetch_balance():
        try:
            wallet_address = context.user_data.get('wallet_address')
            token_balance = await get_token_balance(wallet_address, token_address)
            context.user_data['token_balance'] = token_balance
        except Exception as e:
            logger.error(f"An error occurred while fetching token balance: {e}")
            send_or_edit_message(query, f"❌ An error occurred: {str(e)}")
            
    asyncio.run(fetch_balance())
    context.user_data['next_action'] = 'sell_token_amount'

def handle_sell_token_amount(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    amount = update.message.text

    try:
        amount = float(amount)
    except ValueError:
        update.message.reply_text("❌ Please enter a valid number.")
        return

    wallet_info = get_user_wallet(user_id)
    if not wallet_info:
        update.message.reply_text("❌ Wallet not found. Please create a wallet first.")
        return

    client = asyncio.run(init_ton_client())
    current_balance = asyncio.run(get_wallet_balance(client, wallet_info['address']))

    if current_balance < 0.35:
        update.message.reply_text(f"❌ Insufficient funds to complete the transaction. You need to have at least 0.35 TON left in your balance to complete the sale.")
        return

    token_address = context.user_data['sell_token_address']

    def run_sell_task_sync():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(sell_tokens(user_id, token_address, amount, update.message, context))
        finally:
            loop.close()

    executor.submit(run_sell_task_sync)

async def get_token_balance(wallet_address, token_address):
    try:
        provider = LiteBalancer.from_mainnet_config(2)
        await provider.start_up()

        result_stack = await provider.run_get_method(
            address=token_address,
            method="get_wallet_address",
            stack=[begin_cell().store_address(Address(wallet_address)).end_cell().begin_parse()]
        )
        token_wallet_address = result_stack[0].load_address()
        logger.info(f"Token wallet address for {wallet_address}: {token_wallet_address}")

        result_stack = await provider.run_get_method(
            address=token_wallet_address,
            method="get_wallet_data",
            stack=[]
        )
        balance = result_stack[0] if isinstance(result_stack[0], int) else result_stack[0].load_uint(128)
        logger.info(f"Token balance: {balance}")

        await provider.close_all()
        return balance / 10**9
    except Exception as e:
        logger.error(f"An error occurred: {e}")
        return 0.0

async def sell_tokens(user_id, token_address, amount, message, context):
    mnemonics = get_user_mnemonics(user_id)
    if not mnemonics:
        message.reply_text("❌ No wallet found for your account. Please create a wallet first.")
        send_welcome_message(message, context)
        return
    
    router = RouterV1()
    jetton_sell_addressTWO = AddressV1(token_address)
    jetton_sell_address = AddressV1(token_address)

    try:
        provider = LiteBalancer.from_mainnet_config(2)
        await provider.start_up()
        wallet = await WalletV4R2.from_mnemonic(provider=provider, mnemonics=mnemonics)

        wallet_info = get_user_wallet(user_id)
        wallet_address = AddressV1(wallet.address)
        initial_balances = await get_user_tokens(wallet_info['address'])

        params = await router.build_swap_jetton_to_ton_tx_params(
            user_wallet_address=wallet.address,
            offer_jetton_address=jetton_sell_addressTWO,
            offer_amount=int(amount * 1e9),
            min_ask_amount=0,
            provider=provider
        )

        await wallet.transfer(destination=params['to'],
                              amount=int(0.35 * 1e9),
                              body=params['payload'])
        await provider.close_all()

        message.reply_text("🚀 Transaction sent successfully! Waiting for confirmation…")
        save_trade(user_id, token_address, 'sell', amount, base_token_price_quote_token)
        await monitor_transaction_completion(user_id, context, message, "sell", initial_balances)

    except Exception as e:
        logger.error(f"An unexpected error occurred during token sale: {e}")
        send_or_edit_message(message, "❌ An unexpected error occurred. Please try again.")
        send_welcome_message(message, context)
def display_position_pnl_menu(query, context, wallet_address):
    tokens = asyncio.run(get_user_tokens(wallet_address))
    
    if not tokens:
        send_or_edit_message(
            query,
            "⚠️ No tokens found in your wallet.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back", callback_data='back_to_pnl')],
                [InlineKeyboardButton("Home", callback_data='back_to_main')]
            ])
        )
        return

    token_buttons = []
    for token in tokens:
        token_address = token['jetton']['address']
        token_name = token.get('jetton', {}).get('symbol', 'Unknown Token')
        token_balance = token.get('balance', 0)
        token_balance_formatted = "{:.2f}".format(float(token_balance) / 10**9)

        token_buttons.append([
            InlineKeyboardButton(f"${token_name} ({token_balance_formatted})", callback_data=f'position_pnl_{token_address}')
        ])

    token_buttons.append([
        InlineKeyboardButton("⬅️ Back", callback_data='back_to_pnl'),
        InlineKeyboardButton("Home", callback_data='back_to_main')
    ])

    send_or_edit_message(
        query,
        "Select a token to get your Position PNL Card.",
        InlineKeyboardMarkup(token_buttons)
    )        
def get_user_mnemonics(user_id):
    c.execute("SELECT seed FROM user_wallets WHERE user_id=?", (user_id,))
    result = c.fetchone()
    if result:
        return result[0].split()
    return None

def get_user_wallet(user_id):
    c.execute("SELECT address, seed FROM user_wallets WHERE user_id=?", (user_id,))
    result = c.fetchone()
    if result:
        return {'address': result[0], 'mnemonics': result[1].split()}
    return None

def show_seed_phrase(query, context):
    user_id = query.from_user.id
    wallet = get_user_wallet(user_id)
    if wallet:
        seed_phrase = ' '.join(wallet['mnemonics'])
        message_text = (
            "You can now import your wallet, for example into Tonkeeper, using this seed phrase. "
            "Delete this message once you are done.\n\n"
            f"🔑 Your seed phrase: <code>{seed_phrase}</code>"
        )
        send_or_edit_message(
            query,
            message_text,
            wallet_menu(),
            parse_mode="HTML"
        )
    else:
        send_or_edit_message(
            query,
            "❌ No wallet found for your account. Please create a wallet first.",
            wallet_menu()
        )
def handle_delete_message(query, context):
    try:
        query.message.delete()
    except BadRequest as e:
        logger.error(f"Failed to delete message: {e.message}")

def calculate_volume(user_id):
    volume_1day = c.execute("SELECT SUM(amount) FROM trades WHERE user_id=? AND date >= DATE('now', '-1 day')", (user_id,)).fetchone()[0] or 0
    volume_7days = c.execute("SELECT SUM(amount) FROM trades WHERE user_id=? AND date >= DATE('now', '-7 days')", (user_id,)).fetchone()[0] or 0
    volume_month = c.execute("SELECT SUM(amount) FROM trades WHERE user_id=? AND date >= DATE('now', '-1 month')", (user_id,)).fetchone()[0] or 0
    volume_total = c.execute("SELECT SUM(amount) FROM trades WHERE user_id=?", (user_id,)).fetchone()[0] or 0

    return {
        '1day': volume_1day,
        '7days': volume_7days,
        'month': volume_month,
        'total': volume_total
    }
def calculate_trades(user_id):
    trades_1day = c.execute("SELECT COUNT(*) FROM trades WHERE user_id=? AND date >= DATE('now', '-1 day')", (user_id,)).fetchone()[0]
    trades_7days = c.execute("SELECT COUNT(*) FROM trades WHERE user_id=? AND date >= DATE('now', '-7 days')", (user_id,)).fetchone()[0]
    trades_month = c.execute("SELECT COUNT(*) FROM trades WHERE user_id=? AND date >= DATE('now', '-1 month')", (user_id,)).fetchone()[0]
    trades_total = c.execute("SELECT COUNT(*) FROM trades WHERE user_id=?", (user_id,)).fetchone()[0]

    return {
        '1day': trades_1day,
        '7days': trades_7days,
        'month': trades_month,
        'total': trades_total
    }
def calculate_pnl(user_id):
    trades = c.execute("SELECT token_address, type, amount, price FROM trades WHERE user_id=?", (user_id,)).fetchall()
    pnl_data = {}
    
    for token_address, trade_type, amount, price in trades:
        if token_address not in pnl_data:
            pnl_data[token_address] = {'total_amount': 0, 'total_cost': 0, 'realized_pnl': 0}

        if trade_type == 'buy':
            pnl_data[token_address]['total_amount'] += amount
            pnl_data[token_address]['total_cost'] += amount * price
        elif trade_type == 'sell':
            if pnl_data[token_address]['total_amount'] > 0:
                avg_price = pnl_data[token_address]['total_cost'] / pnl_data[token_address]['total_amount']
                pnl_data[token_address]['realized_pnl'] += (price - avg_price) * amount
                pnl_data[token_address]['total_amount'] -= amount
                pnl_data[token_address]['total_cost'] -= avg_price * amount

    for token in pnl_data:
        current_price = c.execute('SELECT price FROM trades WHERE user_id=? AND token_address=? ORDER BY date DESC LIMIT 1',
                                  (user_id, token)).fetchone()[0]
        unrealized_pnl = (current_price - (pnl_data[token]['total_cost'] / pnl_data[token]['total_amount'])) * pnl_data[token]['total_amount']
        pnl_data[token]['unrealized_pnl'] = unrealized_pnl
        pnl_data[token]['total_pnl'] = pnl_data[token]['realized_pnl'] + unrealized_pnl

    return pnl_data

async def update_token_prices():
    users = c.execute("SELECT DISTINCT user_id FROM trades").fetchall()
    for user in users:
        wallet = get_user_wallet(user[0])
        if wallet:
            tokens = await get_user_tokens(wallet['address'])
            for token in tokens:
                token_address = token['jetton']['address']
                price = token.get('price') 
                c.execute('UPDATE trades SET price=? WHERE user_id=? AND token_address=? AND type=?',
                          (price, user[0], token_address, 'buy'))
                conn.commit()
    logger.info("Token prices updated successfully.")
def save_trade(user_id, token_address, trade_type, amount, price):
    c.execute('INSERT INTO trades (user_id, token_address, type, amount, price) VALUES (?, ?, ?, ?, ?)',
              (user_id, token_address, trade_type, amount, price))
    conn.commit()
def handle_pnl(query, context):
    user_id = query.from_user.id
    display_pnl_menu(query, context, user_id)    
def display_pnl_menu(query, context, user_id):
    pnl_data = calculate_pnl(user_id)
    volume_data = calculate_volume(user_id)
    trades_data = calculate_trades(user_id)

    # Обработка отсутствующих ключей
    pnl_unrealised = pnl_data.get('unrealised', 0)
    pnl_realised = pnl_data.get('realised', 0)
    pnl_total = pnl_data.get('total', 0)

    pnl_text = (
        f"<b>PnL Menu</b>\n\n"
        f"<b>PnL</b>\n"
        f"|-- Unrealised: {pnl_unrealised} TON\n"
        f"|-- Realised: {pnl_realised} TON\n"
        f"|__ Total: {pnl_total} TON\n\n"
        f"<b>Volume</b>\n"
        f"|-- 1 Day: {volume_data['1day']} TON\n"
        f"|-- 7 Days: {volume_data['7days']} TON\n"
        f"|-- Month: {volume_data['month']} TON\n"
        f"|__ Total: {volume_data['total']} TON\n\n"
        f"<b>Trades (Buys, Sells)</b>\n"
        f"|-- 1 Day: {trades_data['1day']} trades\n"
        f"|-- 7 Days: {trades_data['7days']} trades\n"
        f"|-- Month: {trades_data['month']} trades\n"
        f"|__ Total: {trades_data['total']} trades\n"
    )
    
    keyboard = [
        [InlineKeyboardButton("← Home", callback_data='back_to_main'), InlineKeyboardButton("↻ Refresh", callback_data='refresh')],
        [InlineKeyboardButton("---- Get PnL Card ---", callback_data='get_pnl_card')],
        [InlineKeyboardButton("Total PnL", callback_data='total_pnl'), InlineKeyboardButton("Position PnL", callback_data='position_pnl')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    query.message.reply_text(pnl_text, reply_markup=reply_markup, parse_mode="HTML")

def main():
    TOKEN = '6005310380:AAHFhhB8ut6nq2ckpo-qcxcJPOFySZ8d6rk'
    updater = Updater(TOKEN, use_context=True)

    dispatcher = updater.dispatcher
    scheduler = AsyncIOScheduler(timezone=pytz.utc) 
    scheduler.start()

    scheduler.add_job(
        check_chat_members_periodically,
        trigger=IntervalTrigger(seconds=600, timezone=pytz.utc),
        args=[dispatcher.bot]
    )
    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(CallbackQueryHandler(handle_callback_query))
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    dispatcher.add_handler(CallbackQueryHandler(handle_token_selection, pattern=r'^sell_token_'))
    dispatcher.add_handler(CallbackQueryHandler(handle_confirm_export_seed, pattern='confirm_export_seed'))
    dispatcher.add_handler(CallbackQueryHandler(handle_delete_message, pattern='delete_message'))
    dispatcher.add_handler(CallbackQueryHandler(handle_callback_query))

    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()