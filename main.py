import logging
import sqlite3
import os
import requests
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, CallbackQuery, Message, InputMediaPhoto
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
from bs4 import BeautifulSoup
import hashlib
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
import pytz


router = RouterV1()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

db_lock = asyncio.Lock()
keystore_dir = 'keystore'
if not os.path.exists(keystore_dir):
    os.makedirs(keystore_dir)

sniping_tasks = {}

async def create_database():
    async with db_lock:
        conn = await asyncio.to_thread(sqlite3.connect, 'userwallets.db', check_same_thread=False)
        c = await asyncio.to_thread(conn.cursor)

        await asyncio.to_thread(c.execute, '''CREATE TABLE IF NOT EXISTS user_wallets
                                            (user_id INTEGER PRIMARY KEY, address TEXT, seed TEXT)''')
        await asyncio.to_thread(c.execute, '''CREATE TABLE IF NOT EXISTS sniping_settings
                                            (user_id INTEGER PRIMARY KEY, liquidity_amount REAL DEFAULT 0.0, 
                                            mcap_amount REAL DEFAULT 0.0, slippage_percent REAL DEFAULT 0.0)''')
        await asyncio.to_thread(c.execute, '''CREATE TABLE IF NOT EXISTS token_parameters
                                            (user_id INTEGER PRIMARY KEY, name TEXT, symbol TEXT, supply REAL, decimals INTEGER, description TEXT)''')
        await asyncio.to_thread(c.execute, '''CREATE TABLE IF NOT EXISTS referrals
                                            (user_id INTEGER PRIMARY KEY, referrer_id INTEGER, referees INTEGER DEFAULT 0)''')
        await asyncio.to_thread(c.execute, '''
        CREATE TABLE IF NOT EXISTS allowed_users (
            user_id INTEGER PRIMARY KEY
        )
        ''')
        await asyncio.to_thread(conn.commit)
        return conn, c

async def handle_faq(query, context):
    faq_text = (
        "❓ FAQ\n\n"
        "💎 Gemz Trade is the #1 Trading App on the TON blockchain.\n\n"
        "📈 With Gemz Trade you can:\n"
        "• Trade Jettons easily\n"
        "• Automate trading strategies\n"
        "• Earn rewards and more\n\n"
        "👥 Invite your friends to earn even more!"
    )
    await send_or_edit_message(query, faq_text)

async def create_invite_button(user_id):
    ref_link = f"https://t.me/GemzTradeBot?start={user_id}"

    invite_button = InlineKeyboardButton(
        "🔗 invite",
        url=f"https://t.me/share/url?url={ref_link}"
    )
    return InlineKeyboardMarkup([
        [invite_button],
        [InlineKeyboardButton("⬅️ Back to main menu", callback_data='back_to_main')]
    ])

async def check_chat_members_periodically(context, interval=600):
    chat_id = -1002066392521
    while True:
        try:
            members = await context.bot.get_chat_members_count(chat_id)
            current_members = []
            for member_id in range(1, members + 1):
                member = await context.bot.get_chat_member(chat_id, member_id)
                current_members.append(member.user.id)

            async with db_lock:
                c = await asyncio.to_thread(conn.cursor)
                stored_users = await asyncio.to_thread(c.execute, "SELECT user_id FROM allowed_users")
                stored_users = await asyncio.to_thread(stored_users.fetchall)

                for stored_user in stored_users:
                    if stored_user[0] not in current_members:
                        await asyncio.to_thread(c.execute, "DELETE FROM allowed_users WHERE user_id = ?", (stored_user[0],))
                        logger.info(f"Removed user {stored_user[0]} from allowed users list.")

                for member_id in current_members:
                    await asyncio.to_thread(c.execute, "INSERT OR IGNORE INTO allowed_users (user_id) VALUES (?)", (member_id,))
                    logger.info(f"Added user {member_id} to allowed users list.")

                await asyncio.to_thread(conn.commit)

        except BadRequest as e:
            logger.error(f"Failed to get chat members: {e}")

        await asyncio.sleep(interval)

async def start(update: Update, context: CallbackContext) -> None:
    user_id = update.message.from_user.id
    
    welcome_text = (
        "👋 Hey there, crypto buddy!\n\n"
        "💎 Trade tokens and earn on TON blockchain right here. It’s fast, simple and exciting!\n\n"
        "💰 Read FAQ and invite your friends. We have loads of prizes waiting for you!"
    )
    
    keyboard = [
        [InlineKeyboardButton("💎 Farm $GEMZ", url='https://t.me/GemzTradeBot')],
        [InlineKeyboardButton("🎟️ Gemz Pass", url='https://getgems.io/collection/EQAZO_HuoR3aP7Pmi5kE3h91mmp4J5OwhbMcrkZlwSMVDt3M#stats'), InlineKeyboardButton("❓ FAQ", callback_data='faq')],
        [InlineKeyboardButton("🚀 Start Trading", callback_data='start_trading')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    image_path = 'fon2.jpg' 
    
    try:
        await update.message.reply_photo(
            photo=open(image_path, 'rb'),
            caption=welcome_text,
            reply_markup=reply_markup
        )
    except FileNotFoundError:
        await update.message.reply_text(welcome_text, reply_markup=reply_markup)
        logger.error(f"File {image_path} not found.")

async def wallet_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Tonviewer", callback_data='tonviewer'), InlineKeyboardButton("← Home", callback_data='back_to_main')],
        [InlineKeyboardButton("Deposit TON", callback_data='wallet_deposit')],
        [InlineKeyboardButton("Withdraw all TON", callback_data='withdraw_all_ton'), InlineKeyboardButton("Withdraw X TON", callback_data='withdraw_x_ton')],
        [InlineKeyboardButton("Export Seed Phrase", callback_data='export_seed_phrase')],
        [InlineKeyboardButton("↻ Refresh", callback_data='refresh')]
    ])

async def sniping_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Buy Token", callback_data='snipe_token')],
        [InlineKeyboardButton("Settings", callback_data='settings')],
        [InlineKeyboardButton("Cancel Buy", callback_data='cancel_snipe')],
        [InlineKeyboardButton("Back to Main Menu", callback_data='back_to_main')],
        [InlineKeyboardButton("Sell Tokens", callback_data='sell_tokens')],
    ])

async def confirm_export_seed_phrase_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✖ Cancel", callback_data='cancel_export_seed'), InlineKeyboardButton("Confirm", callback_data='confirm_export_seed')]
    ])

async def settings_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Set Slippage", callback_data='set_slippage_percent')],
        [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')],
    ])

async def token_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Choose name", callback_data='choose_name'), InlineKeyboardButton("✏️ Choose symbol", callback_data='choose_symbol')],
        [InlineKeyboardButton("Choose supply", callback_data='choose_supply'), InlineKeyboardButton("18 Decimals", callback_data='choose_decimals')],
        [InlineKeyboardButton("Token settings", callback_data='token_settings')],
        [InlineKeyboardButton("Deploy", callback_data='deploy_token')],
        [InlineKeyboardButton("Back to Main Menu", callback_data='back_to_main')],
    ])

async def handle_snipe_token_amount_directly(query, context, amount):
    user_id = query.from_user.id
    token_address = context.user_data.get('snipe_token_address')
    if not token_address:
        await send_or_edit_message(query, "⚠️ Адрес токена не найден. Пожалуйста, попробуйте снова.")
        return

    wallet_info = await get_user_wallet(user_id)
    if not wallet_info:
        await send_or_edit_message(query, "❌ Кошелек не найден. Пожалуйста, создайте кошелек сначала.")
        return

    client = await init_ton_client()
    current_balance = await get_wallet_balance(client, wallet_info['address'])

    if current_balance < amount:
        await send_or_edit_message(query, f"❌ У вас недостаточно средств на балансе. Ваш текущий баланс: {current_balance} TON.")
        return

    await send_or_edit_message(query, "💰 Запуск процесса покупки...")

    async def run_snipe_task():
        sniping_tasks[user_id] = {'task': asyncio.create_task(snipe_token(user_id, token_address, amount, query.message, context)), 'cancel': False}
        await sniping_tasks[user_id]['task']

    asyncio.create_task(run_snipe_task())

async def prompt_user_for_amount(query, context):
    user_id = query.from_user.id
    await send_or_edit_message(
        query,
        "Please enter the amount you wish to buy in TON (Example: 1.5):",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("Cancel", callback_data='cancel_snipe')]
        ])
    )
    context.user_data['next_action'] = 'snipe_token_amount'

async def handle_invite(query, context):
    user_id = query.from_user.id
    invite_text = "Начни торговать с Gemz Trade 👉"
    reply_markup = await create_invite_button(user_id)
    await query.message.reply_text(
        f"{invite_text}",
        reply_markup=reply_markup
    )

async def handle_refresh(query, context):
    user_id = query.from_user.id
    wallet = await get_user_wallet(user_id)

    if wallet:
        client = await init_ton_client()

        current_balance = await get_wallet_balance(client, wallet['address'])

        current_caption = query.message.caption

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
                await context.bot.edit_message_caption(
                    chat_id=query.message.chat_id,
                    message_id=query.message.message_id,
                    caption=new_caption,
                    reply_markup=query.message.reply_markup,
                    parse_mode="Markdown"
                )
            else:
                await query.answer("Your balance has not changed.")
        else:
            await query.answer("Could not find the balance line in the caption.")
    else:
        await query.answer("No wallet found for your account.")

async def generate_referral_link(user_id):
    return f"https://t.me/GemzTradeBot?start={user_id}"

async def handle_callback_query(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    logger.info(f"Callback query data: {query.data}")

    try:
        if query.data == 'wallet_withdraw':
            await handle_wallet_withdraw(query, context)
        elif query.data == 'wallet_deposit':
            await handle_wallet_deposit(query, context) 
        elif query.data == 'wallet_show_seed':
            await handle_wallet_show_seed(query, context)    
        elif query.data == 'token_info':
            await display_token_information(query, context)
        elif query.data == 'refresh':    
            await handle_refresh(query, context)
        elif query.data == 'close':
            await start(query, context)
        elif query.data == 'close_pnl':
            await query.message.delete()        
        elif query.data == 'faq':
            await display_faq1(query, context)  
        elif query.data == 'referrals':
            await handle_referrals(query, context)
        elif query.data == 'referrals2':
            await handle_referrals2(query, context)    
        elif query.data == 'buy_10':
            await handle_snipe_token_amount_directly(query, context, 10)
        elif query.data == 'buy_100':
            await handle_snipe_token_amount_directly(query, context, 100)
        elif query.data == 'buy_x':
            await prompt_user_for_amount(query, context)
        elif query.data == 'wallet':
            await handle_wallet(query, context)
        elif query.data == 'verify_pass':
            if await is_user_in_chat(user_id, context):
                await send_welcome_message(query, context)
            else:
                await show_gemz_pass_message(query, context, failed_verification=True)
        elif query.data == 'start_trading':
            if await is_user_in_chat(user_id, context):
                await send_welcome_messageFirst(query, context)
            else:
                await show_gemz_pass_message(query, context)
        elif query.data.startswith('sell_token_'):
            await handle_token_selection(query, context)
        elif query.data == 'snipe_token':
            await handle_snipe_token_start(query, context)
        elif query.data == 'sell_tokens':
            await handle_sell_tokens_start(query, context)
        elif query.data == 'settings':
            await handle_settings(query, context)
        elif query.data == 'invite':
            ref_link = await generate_referral_link(user_id)
            invite_button = InlineKeyboardButton("🔗 Поделиться ссылкой", url=f"https://t.me/share/url?url={ref_link}")
        
            keyboard = [
                [InlineKeyboardButton("❌ Close", callback_data='close'), InlineKeyboardButton("↻ Refresh", callback_data='refresh')],
                [invite_button]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_reply_markup(reply_markup=reply_markup)
        elif query.data == 'pnl':
            await send_or_edit_message(query, "PNL feature will be added after the beta phase.", InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Close", callback_data='close_pnl')]
            ]))
        elif query.data == 'help':
            await display_help(query, context)
        elif query.data == 'back_to_main':
            await send_welcome_message(query, context)
        elif query.data.startswith('set_'):
            await handle_set_setting_start(query, context, query.data[4:])
        elif query.data == 'cancel_snipe':
            await cancel_snipe(update, context)
    except BadRequest as e:
        logger.error(f"BadRequest error: {e.message}")

async def show_gemz_pass_message(query, context, failed_verification=False):
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
    
    await send_or_edit_message(query, message_text, reply_markup, parse_mode="HTML")

async def display_token_information(query, context: CallbackContext):
    token_info_text = (
        "<b>Token Information</b>\n\n"
        "🔍 <b>Name:</b> {name}\n"
        "📍 <b>Pool Address:</b> {pool_address}\n"
        "💵 <b>Fully Diluted Valuation:</b> ${fdv}\n"
        "💰 <b>Market Cap:</b> ${market_cap}\n"
        "💧 <b>Liquidity:</b> ${liquidity}\n"
        "🪙 <b>Price in TON:</b> {price} TON\n"
    ).format(
        name="Example Token",
        pool_address="EQC...9F2",
        fdv="10,000,000",
        market_cap="5,000,000",
        liquidity="2,000,000",
        price="1.5"
    )
    
    keyboard = [
        [InlineKeyboardButton("❌ Close", callback_data='close')],
        [InlineKeyboardButton("Buy 10 TON", callback_data='buy_10'), 
         InlineKeyboardButton("Buy 100 TON", callback_data='buy_100')],
        [InlineKeyboardButton("Buy X TON", callback_data='buy_x')],
        [InlineKeyboardButton("↻ Refresh", callback_data='refresh')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text=token_info_text,
        reply_markup=reply_markup,
        parse_mode="HTML"
    )

async def handle_wallet(query, context):
    user_id = query.from_user.id
    wallet = await get_user_wallet(user_id)
    if wallet:
        client = await init_ton_client()
        balance = await get_wallet_balance(client, wallet['address'])
        if balance is not None:
            await send_or_edit_message(
                query,
                f"👛 Wallet Menu\n\nBalance: {balance} TON\nYour wallet address: <code>{wallet['address']}</code>",
                await wallet_menu(),
                parse_mode="HTML"
            )
        else:
            await send_or_edit_message(
                query,
                "❌ Could not fetch balance. Please try again later.",
                await wallet_menu()
            )
    else:
        await send_or_edit_message(
            query,
            "❌ Depost first to see your wallet and balance",
            await wallet_menu()
        )

async def handle_wallet_deposit(query, context):
    await handle_deposit(query, context)

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

async def handle_wallet_show_seed(query, context):
    await send_or_edit_message(
        query,
        "Are you sure you want to export your Seed Phrase?\n\nOnce the seed phrase is exported we cannot guarantee the safety of your wallet.",
        await confirm_export_seed_phrase_menu()
    )

async def delete_message_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Delete Message", callback_data='delete_message')]
    ])

async def handle_confirm_export_seed(query, context):
    user_id = query.from_user.id
    wallet = await get_user_wallet(user_id)
    
    if wallet:
        seed_phrase = ' '.join(wallet['mnemonics'])
        message_text = (
            f"Your Seed Phrase is: {seed_phrase}\n\n"
            "You can now import your wallet for example into Tonkeeper, using this seed phrase.\n"
            "Delete this message once you are done."
        )
        await send_or_edit_message(
            query,
            message_text,
            await delete_message_menu()
        )
    else:
        await send_or_edit_message(
            query,
            "❌ No wallet found for your account. Please create a wallet first.",
            await wallet_menu()
        )

async def handle_wallet_balance(query, context):
    user_id = query.from_user.id
    wallet = await get_user_wallet(user_id)
    if wallet:
        client = await init_ton_client()
        balance = await get_wallet_balance(client, wallet['address'])
        if balance is not None:
            await send_or_edit_message(
                query,
                f"👛 Your wallet address: <code>{wallet['address']}</code>\nBalance: {balance} TON",
                await wallet_menu(),
                parse_mode="HTML"
            )
        else:
            await send_or_edit_message(
                query,
                "❌ Could not fetch balance. Please try again later.",
                await wallet_menu()
            )
    else:
        await send_or_edit_message(
            query,
            "❌ No wallet found for your account. Please create a wallet first.",
            await wallet_menu()
        )

async def token_information_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Close", callback_data='close')],
        [
            InlineKeyboardButton("Buy 10 TON", callback_data='buy_10'),
            InlineKeyboardButton("Buy 100 TON", callback_data='buy_100'),
            InlineKeyboardButton("Buy X TON", callback_data='buy_x')
        ],
        [InlineKeyboardButton("↻ Refresh", callback_data='refresh')]
    ])

async def send_or_edit_message(entity, text, reply_markup=None, parse_mode=None):
    try:
        if isinstance(entity, CallbackQuery):
            await entity.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        elif isinstance(entity, Message):
            await entity.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        logger.error(f"BadRequest error: {e.message}")

async def handle_language_selection(query, context):
    await query.message.delete()
    if query.data == 'lang_en':
        await send_welcome_message(query, context)

async def convert_to_user_friendly_format(raw_address):
    try:
        address_obj = str(AddressV1(raw_address))
        address_str = address_obj.replace("Address<", "").replace(">", "")
        return address_str
    except Exception as e:
        logger.error(f"Error converting address {raw_address}: {e}")
        return raw_address

async def display_help(query, context):
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

    await send_or_edit_message(
        query,
        help_text,
        InlineKeyboardMarkup([
            [InlineKeyboardButton("❓SUPPORT", url='https://t[.]me/GemzTradeCommunity/18819'.replace("[.]", "."))],
            [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
        ]),
        parse_mode='HTML',
    )

async def display_faq1(query, context):
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

    await send_or_edit_message(
        query,
        help_text,
        InlineKeyboardMarkup([
            [InlineKeyboardButton("❓SUPPORT", url='https://t[.]me/GemzTradeCommunity/18819'.replace("[.]", "."))],
            [InlineKeyboardButton("❌ Close", callback_data='close_pnl')]

        ]),
        parse_mode='HTML',
    )

async def send_welcome_messageFirst(entity, context: CallbackContext):
    user_id = entity.from_user.id
    wallet = await get_user_wallet(user_id)

    if wallet:
        client = await init_ton_client()
        balance = await get_wallet_balance(client, wallet['address'])
    else:
        balance = None

    wallet_address = wallet['address'] if wallet else "No wallet found"
    menu, welcome_text = await main_menu(wallet_address=wallet_address, balance=balance)

    image_path = 'fon2.jpg'

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

async def send_welcome_message(entity, context: CallbackContext):
    user_id = entity.from_user.id
    wallet = await get_user_wallet(user_id)

    async def process():
        if wallet:
            client = await init_ton_client()  
            balance = await get_wallet_balance(client, wallet['address'])
        else:
            balance = None

        wallet_address = wallet['address'] if wallet else "No wallet found"
        menu, welcome_text = await main_menu(wallet_address=wallet_address, balance=balance)

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

    await process()

async def create_and_activate_wallet_async(user_id, query):
    address, mnemonics = await create_and_activate_wallet()
    await save_user_wallet(user_id, address, mnemonics)
    await show_wallet_balance(query, address)

async def main_menu(wallet_address=None, balance=None) -> InlineKeyboardMarkup:
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
    config = await asyncio.to_thread(requests.get, url)
    config = config.json()
    keystore_dir = '/tmp/ton_keystore'
    Path(keystore_dir).mkdir(parents=True, exist_ok=True)
    client = TonlibClient(ls_index=2, config=config, keystore=keystore_dir, tonlib_timeout=10)
    await client.init()
    return client

async def handle_deposit(query, context):
    user_id = query.from_user.id
    wallet = await get_user_wallet(user_id)
    
    if not wallet:
        await send_or_edit_message(query, "Creating and activating your wallet, this will take up to 60 seconds...")
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

async def is_user_in_chat(user_id, context: CallbackContext):
    chat_id = -1002066392521  
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        logger.info(f"User {user_id} is in the chat with status: {member.status}")
        return member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        logger.error(f"Ошибка проверки пользователя в чате {chat_id}: {e}")
        return False

async def handle_referrals(query, context: CallbackContext) -> None:
    user_id = query.from_user.id
    
    async with db_lock:
        c = await asyncio.to_thread(conn.cursor)
        referees_count = await asyncio.to_thread(c.execute, "SELECT referees FROM referrals WHERE user_id=?", (user_id,))
        result = await asyncio.to_thread(referees_count.fetchone)

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
    
    await send_or_edit_message(
        query,
        message_text,
        reply_markup
    )

async def handle_referrals2(query, context: CallbackContext) -> None:
    user_id = query.from_user.id
    
    async with db_lock:
        c = await asyncio.to_thread(conn.cursor)
        referees_count = await asyncio.to_thread(c.execute, "SELECT referees FROM referrals WHERE user_id=?", (user_id,))
        result = await asyncio.to_thread(referees_count.fetchone)

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
    
    await send_or_edit_message(
        query,
        message_text,
        reply_markup
    )

async def token_information() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Close", callback_data='close')],
        [InlineKeyboardButton("Buy 10 TON", callback_data='buy_10'), InlineKeyboardButton("Buy 100 TON", callback_data='buy_100')],
        [InlineKeyboardButton("↻ Refresh", callback_data='refresh')]
    ])

async def handle_withdraw(query, context):
    logger.info(f"User {query.from_user.id} initiated a withdrawal process.")
    await send_or_edit_message(
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

    if next_action == 'withdraw_address':
        address = update.message.text
        context.user_data['withdraw_address'] = address
        context.user_data['next_action'] = 'withdraw_amount'
        await update.message.reply_text(
            "🏦 Please enter the amount you want to withdraw:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
            ])
        )
    elif next_action == 'withdraw_amount':
        amount = update.message.text
        context.user_data['withdraw_amount'] = amount
        try:
            amount = float(amount)
        except ValueError:
            await update.message.reply_text("❌ Please enter a valid number.")
            return

        address = context.user_data['withdraw_address']
        withdrawal_message = await process_withdrawal(user_id, address, amount)
        await update.message.reply_text(withdrawal_message)
        context.user_data['next_action'] = None

async def process_withdrawal(user_id, to_address, amount):
    wallet_info = await get_user_wallet(user_id)
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
    await asyncio.sleep(10)
    try:
        print(f"Attempting to send {amount} TON to {to_address}")

        non_bounceable_address = AddressTON(to_address).to_string(True, True, False)
        await my_wallet.transfer_ton(destination_address=non_bounceable_address, amount=amount, message='Withdrawal')
        print(f"Successfully sent {amount} TON to {to_address}")
        return f"✅ Successfully sent {amount} TON to {to_address}."
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
    params = await get_token_parameters(user_id)
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
        admin_address=Address(await get_user_wallet(user_id)['address']),
        jetton_content_uri=metadata_uri,
        jetton_wallet_code_hex=JettonWallet.code
    )

    return minter.create_state_init()['state_init'], minter.address.to_string()

async def increase_supply(user_id, jetton_address, total_supply):
    try:
        total_supply = float(total_supply)
    except ValueError as e:
        logger.error(f"Error converting total_supply to float: {e}")
        raise

    wallet_address = Address(await get_user_wallet(user_id)['address'])
    
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
        await send_or_edit_message(callback_query, f"❌ Error creating state init jetton: {e}")
        return

    wallet_info = await get_user_wallet(user_id)
    if not wallet_info:
        await send_or_edit_message(callback_query, "❌ Deposit first to see your wallet and balance...")
        return

    mnemonics = wallet_info['mnemonics']
    try:
        _, _, _, wallet = Wallets.from_mnemonics(mnemonics=mnemonics, version=WalletVersionEnum.v4r2, workchain=0)
    except Exception as e:
        logger.error(f"Error initializing wallet from mnemonics: {e}")
        await send_or_edit_message(callback_query, f"❌ Error initializing wallet from mnemonics: {e}")
        return

    try:
        client = await init_ton_client()
    except Exception as e:
        logger.error(f"Error initializing TON client: {e}")
        await send_or_edit_message(callback_query, f"❌ Error initializing TON client: {e}")
        return

    try:
        seqno = await get_seqno(client, wallet.address.to_string(True, True, True, True))
    except Exception as e:
        logger.error(f"Error getting seqno: {e}")
        await send_or_edit_message(callback_query, f"❌ Error getting seqno: {e}")
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
        await send_or_edit_message(callback_query, f"❌ Error creating deploy transfer message: {e}")
        return

    try:
        await client.raw_send_message(deploy_query['message'].to_boc(False))
    except Exception as e:
        logger.error(f"Error sending raw message: {e}")
        await send_or_edit_message(callback_query, f"❌ Error sending raw message: {e}")
        return

    token_params = await get_token_parameters(user_id)
    total_supply = token_params['supply']

    try:
        mint_body = await increase_supply(user_id, jetton_address, total_supply)
    except Exception as e:
        logger.error(f"Error creating mint body: {e}")
        await send_or_edit_message(callback_query, f"❌ Error creating mint body: {e}")
        return

    try:
        seqno = await get_seqno(client, wallet.address.to_string(True, True, True, True))
    except Exception as e:
        logger.error(f"Error getting seqno for mint: {e}")
        await send_or_edit_message(callback_query, f"❌ Error getting seqno for mint: {e}")
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
        await send_or_edit_message(callback_query, f"❌ Error creating mint transfer message: {e}")
        return

    try:
        await client.raw_send_message(mint_query['message'].to_boc(False))
    except Exception as e:
        logger.error(f"Error sending mint message: {e}")
        await send_or_edit_message(callback_query, f"❌ Error sending mint message: {e}")
        return

    await send_or_edit_message(callback_query, "🚀 Token deployed and minted successfully.")

async def get_client():
    url = 'https://ton.org/global.config.json'
    config = await asyncio.to_thread(requests.get, url)
    config = config.json()
    keystore_dir = '/tmp/ton_keystore'
    Path(keystore_dir).mkdir(parents=True, exist_ok=True)
    client = TonlibClient(ls_index=2, config=config, keystore=keystore_dir, tonlib_timeout=10)
    await client.init()
    return client

async def get_seqno(client: TonlibClient, address: str):
    data = await client.raw_run_method(method='seqno', stack_data=[], address=address)
    return int(data['stack'][0][1], 16)

async def handle_settings(query, context):
    user_id = query.from_user.id
    settings = await get_settings_from_database(user_id)
    settings_text = (
        f"⚙️ Current settings:\n"
        f"⚖️ Slippage Percent: {settings['slippage_percent']}\n"
    )
    logger.info(f"Settings for user {user_id}: {settings_text}")
    await send_or_edit_message(query, settings_text, await settings_menu())

async def handle_set_setting_start(query, context, setting_name) -> None:
    context.user_data['current_setting'] = setting_name
    context.user_data['next_action'] = 'setting_value'
    logger.info(f"Set next_action to 'setting_value' for setting {setting_name}")

    context.user_data['preserve'] = True
    
    await send_or_edit_message(
        query,
        f"🔧 Please enter the value for slippage precent:",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back to Settings Menu", callback_data='settings')]
        ])
    )

async def handle_setting_value(update: Update, context: CallbackContext) -> None:
    user_id = update.message.from_user.id
    setting_name = context.user_data.get('current_setting')
    
    if not setting_name:
        return

    value = update.message.text
    try:
        value = float(value)
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid number.")
        return

    await save_setting(user_id, setting_name, value)
    
    context.user_data['current_setting'] = None
    context.user_data['current_setting_step'] = None
    context.user_data['next_action'] = None 

    await update.message.reply_text(f"{setting_name.replace('_', ' ').title()} set to {value}.",
                              reply_markup=await settings_menu())

async def handle_set_token_parameter(query, context, parameter: str) -> None:
    context.user_data['current_parameter'] = parameter
    await send_or_edit_message(
        query,
        f"Please enter the value for {parameter.replace('_', ' ').title()}:"
    )

async def handle_token_parameter_value(update: Update, context: CallbackContext) -> None:
    user_id = update.message.from_user.id
    parameter = context.user_data.get('current_parameter')
    value = update.message.text

    if parameter in ['supply', 'decimals']:
        try:
            value = float(value) if parameter == 'supply' else int(value)
        except ValueError:
            await update.message.reply_text("Please enter a valid number.")
            return

    async with db_lock:
        c = await asyncio.to_thread(conn.cursor)
        await asyncio.to_thread(c.execute, f"INSERT OR REPLACE INTO token_parameters (user_id, {parameter}) VALUES (?, ?)", (user_id, value))
        await asyncio.to_thread(conn.commit)

    await update.message.reply_text(f"{parameter.replace('_', ' ').title()} set to {value}.")

async def handle_token_mint(query, context):
    await send_or_edit_message(
        query,
        "Token Minting Menu:",
        await token_menu()
    )

async def handle_token_deploy(query, context):
    await send_or_edit_message(
        query,
        "🚀 Token Deployment feature is coming soon!",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
        ])
    )

async def handle_message(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    next_action = context.user_data.get('next_action')

    logger.info(f"Received message: {update.message.text}")
    logger.info(f"Next action: {next_action}")
    logger.info(f"Context at handle_message start: {context.user_data}")

    if next_action == 'setting_value':
        await handle_setting_value(update, context)
    elif next_action == 'sell_token_amount':
        await handle_sell_token_amount(update, context)
    elif next_action == 'withdraw_address' or next_action == 'withdraw_amount':
        await handle_withdraw_amount(update, context)
    elif next_action == 'snipe_token_address':
        await handle_snipe_token_address(update, context)
    elif next_action == 'snipe_token_amount':
        await handle_snipe_token_amount(update, context)
    else:
        await update.message.reply_text("Unrecognized command or input. Please use the menu.")

async def get_user_wallet(user_id):
    async with db_lock:
        c = await asyncio.to_thread(conn.cursor)
        await asyncio.to_thread(c.execute, "SELECT address, seed FROM user_wallets WHERE user_id=?", (user_id,))
        result = await asyncio.to_thread(c.fetchone)
    if result:
        return {'address': result[0], 'mnemonics': result[1].split()}
    return None

async def save_user_wallet(user_id, address, mnemonics):
    async with db_lock:
        c = await asyncio.to_thread(conn.cursor)
        await asyncio.to_thread(c.execute, "INSERT OR REPLACE INTO user_wallets (user_id, address, seed) VALUES (?, ?, ?)",
                                (user_id, address, ' '.join(mnemonics)))
        await asyncio.to_thread(conn.commit)

async def get_settings_from_database(user_id):
    async with db_lock:
        c = await asyncio.to_thread(conn.cursor)
        await asyncio.to_thread(c.execute, "SELECT liquidity_amount, mcap_amount, slippage_percent "
                                "FROM sniping_settings WHERE user_id=?", (user_id,))
        result = await asyncio.to_thread(c.fetchone)
    keys = ["liquidity_amount", "mcap_amount", "slippage_percent"]
    if result:
        settings = {key: result[i] for i, key in enumerate(keys)}
        logger.info(f"Settings for user {user_id} loaded: {settings}")
        return settings
    else:
        logger.info(f"No settings found for user {user_id}. Inserting default settings.")
        async with db_lock:
            c = await asyncio.to_thread(conn.cursor)
            await asyncio.to_thread(c.execute, "INSERT INTO sniping_settings (user_id) VALUES (?)", (user_id,))
            await asyncio.to_thread(conn.commit)
        return {key: 0.0 for key in keys}

async def save_setting(user_id, setting_name, value):
    logger.info(f"Saving setting {setting_name} with value {value} for user {user_id}")
    try:
        async with db_lock:
            c = await asyncio.to_thread(conn.cursor)
            await asyncio.to_thread(c.execute, f"UPDATE sniping_settings SET {setting_name} = ? WHERE user_id = ?", (value, user_id))
            await asyncio.to_thread(conn.commit)
        logger.info(f"Setting {setting_name} updated successfully for user {user_id}")
    except sqlite3.Error as e:
        logger.error(f"Error updating setting {setting_name} for user {user_id}: {e}")

async def get_token_parameters(user_id):
    async with db_lock:
        c = await asyncio.to_thread(conn.cursor)
        await asyncio.to_thread(c.execute, "SELECT name, symbol, supply, decimals, description FROM token_parameters WHERE user_id=?", (user_id,))
        result = await asyncio.to_thread(c.fetchone)
    if result:
        return {'name': result[0], 'symbol': result[1], 'supply': result[2], 'decimals': result[3], 'description': result[4]}
    return None

async def save_token_param(user_id, param_name, value):
    logger.info(f"Saving token param {param_name} with value {value} for user {user_id}")
    try:
        async with db_lock:
            c = await asyncio.to_thread(conn.cursor)
            await asyncio.to_thread(c.execute, f"INSERT OR REPLACE INTO token_parameters (user_id, {param_name}) VALUES (?, ?)", (user_id, value))
            await asyncio.to_thread(conn.commit)
        logger.info(f"Token param {param_name} updated successfully for user {user_id}")
    except sqlite3.Error as e:
        logger.error(f"Error updating token param {param_name} for user {user_id}: {e}")

async def get_ton_token_pool(token_address):
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    
    try:
        response = await asyncio.to_thread(requests.get, url)
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
    settings = await get_settings_from_database(user_id)
    logger.info("Snipe process started successfully")
    await send_or_edit_message(message, "Buy process started successfully!")

    try:
        pool_info = await get_ton_token_pool(token_address)
        if pool_info:
            base_token_name, quote_token_name, pool_address, fdv_usd, reserve_in_usd, base_token_price_quote_token = pool_info
            logger.info(f"Pool: {base_token_name} / {quote_token_name}")
            logger.info(f"Address: {pool_address}")
            logger.info(f"FDV USD: {fdv_usd}")
            logger.info(f"Reserve in USD: {reserve_in_usd}")
            logger.info(f"Base Token Price Quote Token: {base_token_price_quote_token}")

            if float(reserve_in_usd) < settings['liquidity_amount']:
                logger.info("Liquidity is less than the set threshold. Sniping aborted.")
                await send_welcome_message(message, context)
                return

            if float(fdv_usd) < settings['mcap_amount']:
                logger.info("MCAP is less than the set threshold. Sniping aborted.")
                await send_welcome_message(message, context)
                return

            router = RouterV1()
            WTF = AddressV1(token_address)
            provider = LiteBalancer.from_mainnet_config(2)
            await provider.start_up()

            wallet_info = await get_user_wallet(user_id)
            if not wallet_info:
                await send_or_edit_message(message, "❌ No wallet found for your account. Please create a wallet first.")
                await send_welcome_message(message, context)
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
                await send_or_edit_message(message, "🚀 Transaction sent successfully! Waiting for confirmation…")
                await monitor_transaction_completion(user_id, context, message, "buy", initial_balances)

            else:
                logger.error("Transaction failed")
                await send_or_edit_message(message, "❌ Transaction failed")
                await send_welcome_message(message, context)
        else:
            logger.info("No pools found with TON token or an error occurred.")
            await send_or_edit_message(message, "⚠️ No pools found with TON token or an error occurred.")
            await send_welcome_message(message, context)
    except Exception as e:
        logger.error(f"An unexpected error occurred during sniping: {e}")
        await send_or_edit_message(message, "❌ An unexpected error occurred. Please try again.")
        await send_welcome_message(message, context)

async def cancel_snipe(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    if user_id in sniping_tasks:
        sniping_tasks[user_id]['cancel'] = True
        del sniping_tasks[user_id]
        await query.edit_message_text("Buy process was cancelled.")
    else:
        await query.edit_message_text("No buy process to cancel.")

async def handle_snipe_token_start(query, context):
    await send_or_edit_message(
        query,
        "Please enter the token address:",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
        ])
    )
    context.user_data['next_action'] = 'snipe_token_address'

async def handle_snipe_token_address(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    token_address = update.message.text
    context.user_data['snipe_token_address'] = token_address

    pool_info = await get_ton_token_pool(token_address)
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
        await update.message.reply_text(
            token_info_message,
            parse_mode="HTML",
            reply_markup=await token_information_menu()
        )
    else:
        await update.message.reply_text(
            "⚠️ No pools found with this token or an error occurred. Please check the token address and try again.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
            ])
        )

async def handle_snipe_token_amount(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    amount = update.message.text

    try:
        amount = float(amount)
    except ValueError:
        await update.message.reply_text("❌ Пожалуйста, введите корректное число.")
        return

    wallet_info = await get_user_wallet(user_id)
    if not wallet_info:
        await update.message.reply_text("❌ Кошелек не найден. Пожалуйста, создайте кошелек сначала.")
        return

    client = await init_ton_client()
    current_balance = await get_wallet_balance(client, wallet_info['address'])

    if current_balance < amount + 0.35:
        await update.message.reply_text(f"❌ Insufficient funds. You must have at least {amount + 0.35} TON in your balance to complete the purchase.\nThe fee is 0.1 TON, but you need to have at least 0.35 TON left to complete the transaction.")
        return

    token_address = context.user_data['snipe_token_address']

    sniping_tasks[user_id] = {'task': asyncio.create_task(snipe_token(user_id, token_address, amount, update.message, context)), 'cancel': False}

async def show_initial_welcome_message(query, context: CallbackContext):
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

        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=welcome_text,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
    elif isinstance(query, Message):
        chat_id = query.chat_id
        await query.reply_photo(
            photo=open(image_path, 'rb'),
            caption=welcome_text,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )

async def referral_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Close", callback_data='close'), InlineKeyboardButton("↻ Refresh", callback_data='refresh')],
        [InlineKeyboardButton("🔗 Invite", callback_data='invite')]
    ])

async def handle_sell_tokens_start(query, context: CallbackContext):
    user_id = query.from_user.id
    wallet = await get_user_wallet(user_id)
    
    if not wallet:
        await send_or_edit_message(
            query,
            "❌ No wallet found for your account. Please create a wallet first.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
            ])
        )
        return

    wallet_address = wallet['address']
    await fetch_and_show_tokens(query, context, wallet_address)

async def monitor_transaction_completion(user_id, context, message, transaction_type, initial_balances):
    attempts = 0
    wallet = await get_user_wallet(user_id)
    if not wallet:
        await context.bot.send_message(chat_id=message.chat_id, text="❌ No wallet found for your account.")
        await send_welcome_message(message, context)
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
            await context.bot.send_message(
                chat_id=message.chat_id,
                text=f"✅ {transaction_type.capitalize()} successful!"
            )
            logger.info(f"Transaction {transaction_type} was successful for user {user_id}")
            await send_welcome_message(message, context)
            return

        attempts += 1

    await context.bot.send_message(
        chat_id=message.chat_id,
        text=f"❌ {transaction_type.capitalize()} failed. Please try again."
    )
    logger.error(f"Transaction {transaction_type} failed for user {user_id} after {attempts} attempts")
    await send_welcome_message(message, context)

async def fetch_and_show_tokens(query, context, wallet_address):
    tokens = await get_user_tokens(wallet_address)

    if not tokens:
        await send_or_edit_message(
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

    await send_or_edit_message(
        query,
        "💰 Select a token to sell:",
        InlineKeyboardMarkup(token_buttons)
    )

async def handle_token_selection(query, context):
    user_id = query.from_user.id

    token_index = int(query.data.split('sell_token_')[-1])

    token_address = context.user_data.get(f'token_address_{token_index}')
    if not token_address:
        logger.error("Token address not found in context.user_data")
        await query.message.reply_text(
            "❌ An error occurred: Token address not found.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
            ])
        )
        return

    token_address = await convert_to_user_friendly_format(token_address)

    context.user_data['sell_token_address'] = token_address
    logger.info(f"Token address selected: {token_address}")

    await query.message.reply_text(
        f"Please enter the amount you want to sell for token: {token_address}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
        ])
    )

    context.user_data['next_action'] = 'sell_token_amount'

    async def fetch_balance():
        try:
            wallet_address = context.user_data.get('wallet_address')
            token_balance = await get_token_balance(wallet_address, token_address)
            context.user_data['token_balance'] = token_balance
        except Exception as e:
            logger.error(f"An error occurred while fetching token balance: {e}")
            await send_or_edit_message(query, f"❌ An error occurred: {str(e)}")
            
    await fetch_balance()
    context.user_data['next_action'] = 'sell_token_amount'

async def handle_sell_token_amount(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    amount = update.message.text

    try:
        amount = float(amount)
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid number.")
        return

    wallet_info = await get_user_wallet(user_id)
    if not wallet_info:
        await update.message.reply_text("❌ Wallet not found. Please create a wallet first.")
        return

    client = await init_ton_client()
    current_balance = await get_wallet_balance(client, wallet_info['address'])

    if current_balance < 0.35:
        await update.message.reply_text(f"❌ Insufficient funds to complete the transaction. You need to have at least 0.35 TON left in your balance to complete the sale.")
        return

    token_address = context.user_data['sell_token_address']

    await sell_tokens(user_id, token_address, amount, update.message, context)

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
    mnemonics = await get_user_mnemonics(user_id)
    if not mnemonics:
        await message.reply_text("❌ No wallet found for your account. Please create a wallet first.")
        await send_welcome_message(message, context)
        return
    
    router = RouterV1()
    jetton_sell_addressTWO = AddressV1(token_address)
    jetton_sell_address = AddressV1(token_address)

    try:
        provider = LiteBalancer.from_mainnet_config(2)
        await provider.start_up()
        wallet = await WalletV4R2.from_mnemonic(provider=provider, mnemonics=mnemonics)

        wallet_info = await get_user_wallet(user_id)
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

        await message.reply_text("🚀 Transaction sent successfully! Waiting for confirmation…")
        await monitor_transaction_completion(user_id, context, message, "sell", initial_balances)

    except Exception as e:
        logger.error(f"An unexpected error occurred during token sale: {e}")
        await send_or_edit_message(message, "❌ An unexpected error occurred. Please try again.")
        await send_welcome_message(message, context)

async def get_user_mnemonics(user_id):
    async with db_lock:
        c = await asyncio.to_thread(conn.cursor)
        await asyncio.to_thread(c.execute, "SELECT seed FROM user_wallets WHERE user_id=?", (user_id,))
        result = await asyncio.to_thread(c.fetchone)
    if result:
        return result[0].split()
    return None

async def show_seed_phrase(query, context):
    user_id = query.from_user.id
    wallet = await get_user_wallet(user_id)
    if wallet:
        seed_phrase = ' '.join(wallet['mnemonics'])
        message_text = (
            "You can now import your wallet, for example into Tonkeeper, using this seed phrase. "
            "Delete this message once you are done.\n\n"
            f"🔑 Your seed phrase: <code>{seed_phrase}</code>"
        )
        await send_or_edit_message(
            query,
            message_text,
            await wallet_menu(),
            parse_mode="HTML"
        )
    else:
        await send_or_edit_message(
            query,
            "❌ No wallet found for your account. Please create a wallet first.",
            await wallet_menu()
        )
async def handle_delete_message(query, context):
    await query.message.delete()

async def main():
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

    await updater.start_polling()
    await updater.idle()

if __name__ == '__main__':
    asyncio.run(main())
