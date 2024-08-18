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

keystore_dir = 'keystore'
if not os.path.exists(keystore_dir):
    os.makedirs(keystore_dir)

sniping_tasks = {}

def start(update: Update, context: CallbackContext) -> None:
    user = update.message.from_user
    logger.info("User %s started the conversation.", user.first_name)
    
    send_welcome_message(update.message, context)


def language_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("English", callback_data='lang_en')],
    ])

def sniping_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        # [InlineKeyboardButton("Token Mint", callback_data='token_mint')],
        [InlineKeyboardButton("Buy Token", callback_data='snipe_token')],
        [InlineKeyboardButton("Settings", callback_data='settings')],
        [InlineKeyboardButton("Cancel Buy", callback_data='cancel_snipe')],
        [InlineKeyboardButton("Back to Main Menu", callback_data='back_to_main')],
        [InlineKeyboardButton("Sell Tokens", callback_data='sell_tokens')],
    ])

def settings_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Set Liquidity", callback_data='set_liquidity_amount'), InlineKeyboardButton("Set MCAP", callback_data='set_mcap_amount')],
        [InlineKeyboardButton("Set Slippage", callback_data='set_slippage_percent')],
        [InlineKeyboardButton("⬅Back to Main Menu", callback_data='back_to_main')],
    ])

def token_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Choose name", callback_data='choose_name'), InlineKeyboardButton("✏️ Choose symbol", callback_data='choose_symbol')],
        [InlineKeyboardButton("Choose supply", callback_data='choose_supply'), InlineKeyboardButton("18 Decimals", callback_data='choose_decimals')],
        [InlineKeyboardButton("Token settings", callback_data='token_settings')],
        [InlineKeyboardButton("Deploy", callback_data='deploy_token')],
        [InlineKeyboardButton("Back to Main Menu", callback_data='back_to_main')],
    ])

def handle_callback_query(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id

    logger.info(f"Callback query data: {query.data}")

    try:
        if query.data.startswith('sell_token_'):
            handle_token_selection(query, context)  # Обработка выбора токена
        elif query.data == 'deposit':
            asyncio.run(handle_deposit(query, context))
        elif query.data == 'withdraw':
            handle_withdraw(query, context)
        elif query.data == 'snipe_token':
            handle_snipe_token_start(query, context)
        elif query.data == 'sell_tokens':
            handle_sell_tokens_start(query, context)
        elif query.data == 'settings':
            handle_settings(query, context)
        elif query.data == 'show_seed':
            show_seed_phrase(query, context)
        elif query.data == 'pnl':
            send_or_edit_message(query, "PNL feature will be added after the beta phase.")
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


def send_or_edit_message(entity, text, reply_markup=None, parse_mode=None):
    try:
        if isinstance(entity, CallbackQuery):
            if entity.message and entity.message.text:
                entity.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
            else:
                entity.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        elif isinstance(entity, Message):
            entity.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        logger.error(f"BadRequest error: {e.message}")

def handle_language_selection(query, context):
    query.message.delete()
    if query.data == 'lang_en':
        send_welcome_message(query, context)

def convert_to_user_friendly_format(raw_address):
    # Используем TonTools для преобразования адреса
    address_obj = AddressTON(raw_address)
    return address_obj

def display_help(query, context):
    help_text = (
        "Gemz Trade FAQ\n\n"
        "Q: What is Gemz Trade?\n"
        "A: Gemz Trade is the #1 Trading App on the TON blockchain. It’s fast, user-friendly, and packed with features to enhance trading strategies, minimize risks, and maximize profits. "
        "The main features include Quick Jetton Buy/Sell, Jetton Sniping, Copy Trading, Auto Buy, Advanced PnL, Limit Orders, Referral Earn, and many others.\n\n"
        
        "Q: What's Gemz Trade Mini App for?\n"
        "A: Currently, you can use it to farm points, which will later be converted into $GEMZ tokens. The Mini App will be continuously updated, and trading functionality will be added in the next phase.\n\n"
        
        "Q: What's Waitlist and how can I join it?\n"
        "A: Waitlist participants will get access to open beta after closed beta for Gemz Pass holders. If you're reading this, you're already on the waitlist.\n\n"
        
        "Q: How can I benefit from Waitlist?\n"
        "A: Invite friends and get up to 49% of their fees when they start trading with GEMZ. Earn points for each referral and get $GEMZ airdrop!\n\n"
        
        "Q: What is GEMZ PASS?\n"
        "A: GEMZ PASS is a collection of 555 OG NFTs offering exclusive benefits: 0% Trading Fee forever, Revenue Share from Gemz Trading Fees, Special $GEMZ Airdrop, Access to the Closed Beta, Private Gemz Trading Chat, Increased Referral Reward to 49%, and additional perks yet to be revealed.\n\n"
        
        "Q: Are you planning to launch your own token?\n"
        "A: Yes, we plan to launch $GEMZ, which will be traded on various exchanges. Early adopters will receive an airdrop.\n\n"
        
        "If you have any further questions you can ask them in our community👇"
    )

    # Send the help text along with the support button
    send_or_edit_message(
        query,
        help_text,
        InlineKeyboardMarkup([
            [InlineKeyboardButton("❓SUPPORT", url='https://t.me/GemzTradeCommunity/18819')],
            [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
        ])
    )

def send_welcome_message(entity, context: CallbackContext):
    image_path = 'fon2.jpg'
    welcome_text = """
    Ready to TRADE?
    Just click on the button below 👇
    """

    # Determine the chat_id based on whether entity is a CallbackQuery or Message
    if isinstance(entity, CallbackQuery):
        chat_id = entity.message.chat_id
    else:
        chat_id = entity.chat_id

    context.bot.send_photo(
        chat_id=chat_id,
        photo=open(image_path, 'rb'),
        caption=welcome_text,
        reply_markup=main_menu()
    )

def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Deposit", callback_data='deposit'), InlineKeyboardButton("Withdraw", callback_data='withdraw')],
        [InlineKeyboardButton("Buy Token", callback_data='snipe_token'), InlineKeyboardButton("Sell Tokens", callback_data='sell_tokens')],
        [InlineKeyboardButton("Settings", callback_data='settings'), InlineKeyboardButton("Show Seed", callback_data='show_seed')],
        [InlineKeyboardButton("PNL", callback_data='pnl'), InlineKeyboardButton("Help", callback_data='help')]
    ])

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
        address, mnemonics = await create_and_activate_wallet()
        save_user_wallet(user_id, address, mnemonics)
        wallet = {'address': address, 'mnemonics': mnemonics}
    else:
        address = wallet['address']

    # Fetch the balance of the wallet
    client = await init_ton_client()
    balance = await get_wallet_balance(client, wallet['address'])

    send_or_edit_message(
        query,
        f"💳 Please deposit TON to the following address:\n`{wallet['address']}`\n\n💰 Current Balance: {balance} TON",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
        ]),
        parse_mode="Markdown"  # Use Markdown to make the address copyable
    )

async def activate_wallet(wallet):
    try:
        await wallet.deploy()
        print("Wallet deployed successfully.")
    except Exception as e:
        print(f"Failed to deploy wallet: {e}")

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
        print("Transfer not yet completed. Exiting.")
        return new_wallet_address, new_wallet_mnemonics

    await activate_wallet(new_wallet)

    try:
        new_wallet_state = await new_wallet.get_state()
        print(f"New Wallet State: {new_wallet_state}")
    except Exception as e:
        print(f"Failed to get wallet state: {e}")

    return new_wallet_address, new_wallet_mnemonics

def handle_withdraw(query, context):
    send_or_edit_message(
        query,
        "🏦 Please enter the address to withdraw to:",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
        ])
    )
    context.user_data['next_action'] = 'withdraw_address'

def handle_withdraw_amount(update: Update, context: CallbackContext) -> None:
    user_id = update.message.from_user.id
    next_action = context.user_data.get('next_action')

    if next_action == 'withdraw_address':
        address = update.message.text
        context.user_data['withdraw_address'] = address
        context.user_data['next_action'] = 'withdraw_amount'
        update.message.reply_text(
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
            update.message.reply_text("❌ Please enter a valid number.")
            return

        address = context.user_data['withdraw_address']
        withdrawal_message = asyncio.run(process_withdrawal(user_id, address, amount))
        update.message.reply_text(withdrawal_message)
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
        return f"✅ Successfully sent {amount} TON to {to_address}."
    except Exception as e:
        logger.error(f"Error sending TON: {e}")
        return "❌ Error sending TON. Please try again later."

async def handle_wallet(query, context):
    user_id = query.from_user.id
    wallet = get_user_wallet(user_id)
    if wallet:
        client = await init_ton_client()
        balance = await get_wallet_balance(client, wallet['address'])
        if balance is not None:
            send_or_edit_message(
                query,
                f"👛 Your wallet address: <code>{wallet[address]}</code>\nBalance: {balance} TON",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')],
                    [InlineKeyboardButton("Show Seed", callback_data='show_seed')]
                ]),
                parse_mode="HTML"  # Ensure Markdown is used for formatting
            )
        else:
            send_or_edit_message(
                query,
                "❌ Could not fetch balance. Please try again later.",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')],
                    [InlineKeyboardButton("Show Seed", callback_data='show_seed')]
                ])
            )
    else:
        send_or_edit_message(
            query,
            "❌ No wallet found for your account. Please create a wallet first.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
            ])
        )
        
async def get_wallet_balance(client, address):
    try:
        client1 = TonCenterClient(base_url='https://toncenter.com/api/v2/')

        wallet = Wallet(provider=client1, address=address)
        balance = await wallet.get_balance()
        return balance / 10**9  # Convert from nanoTON to TON
    except Exception as e:
        print(f"Failed to get balance: {e}")
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
        send_or_edit_message(callback_query, "❌ No wallet found for your account. Please create a wallet first.")
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
        f"💧 Liquidity Amount: {settings['liquidity_amount']}\n"
        f"💸 MCAP Amount: {settings['mcap_amount']}\n"
        f"⚖️ Slippage Percent: {settings['slippage_percent']}\n"
    )
    logger.info(f"Settings for user {user_id}: {settings_text}")
    send_or_edit_message(query, settings_text, settings_menu())

def handle_set_setting_start(query, context, setting_name) -> None:
    context.user_data['current_setting'] = setting_name
    context.user_data['current_setting_step'] = 'awaiting_value'
    send_or_edit_message(
        query,
        f"🔧 Please enter the value for {setting_name}:",
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
    
    # Clear the context after setting is updated
    context.user_data['current_setting'] = None
    context.user_data['current_setting_step'] = None
    context.user_data['next_action'] = None  # Clear any other pending actions

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

# def handle_socials(query, context):
#     social_text = (
#         "🌐 Follow us on:\n"
#         "Telegram: [WTF on TON](https://t.me/WTF_on_ton)\n"
#         "Twitter: [WTF ON TON](https://x.com/WTF_ON_TON)\n"
#         "Website: [tonwtf.xyz](https://tonwtf.xyz/)"
#     )
#     send_or_edit_message(
#         query,
#         social_text,
#         InlineKeyboardMarkup([
#             [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
#         ]),
#         parse_mode='Markdown'
#     )

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

    if next_action == 'sell_token_amount':
        handle_sell_token_amount(update, context)  # Запуск процесса продажи
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
    
async def snipe_token(user_id, token_address, offer_amount, message):
    settings = get_settings_from_database(user_id)

    logger.info("Snipe process started successfully")
    send_or_edit_message(message, "Buy process started successfully!")

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
                return

            if float(fdv_usd) < settings['mcap_amount']:
                logger.info("MCAP is less than the set threshold. Sniping aborted.")
                return

            router = RouterV1()
            WTF = AddressV1(token_address)
            provider = LiteBalancer.from_mainnet_config(2)
            await provider.start_up()

            wallet_info = get_user_wallet(user_id)
            if not wallet_info:
                send_or_edit_message(message, "❌ No wallet found for your account. Please create a wallet first.")
                return

            wallet = await WalletV4R2.from_mnemonic(provider, wallet_info['mnemonics'])

            offer_amount_nanoton = round(offer_amount * 1e9)
            logger.info(f"offer_amount_nanoton: {offer_amount_nanoton}")

            min_ask_amount_nanoton = offer_amount_nanoton * 0.9

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
                send_or_edit_message(message, "✅ Transaction sent successfully")
            else:
                logger.error("Transaction failed")
                send_or_edit_message(message, "❌ Transaction failed")
        else:
            logger.info("No pools found with TON token or an error occurred.")
            send_or_edit_message(message, "⚠️ No pools found with TON token or an error occurred.")
    except Exception as e:
        error_message = str(e)
        if "cannot apply external message to current state" in error_message:
            send_or_edit_message(message, "❌ Insufficient funds. Please check your balance and try again.")
        else:
            logger.error(f"An unexpected error occurred during sniping: {e}")
            send_or_edit_message(message, f"❌ An unexpected error occurred: {e}.")
            

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
    user_id = query.from_user.id
    send_or_edit_message(
        query,
        "Please enter the token address:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
        ])
    )
    context.user_data['next_action'] = 'snipe_token_address'

def handle_snipe_token_address(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    token_address = update.message.text
    context.user_data['snipe_token_address'] = token_address

    # Fetch pool information
    pool_info = get_ton_token_pool(token_address)
    if pool_info:
        base_token_name, quote_token_name, pool_address, fdv_usd, reserve_in_usd, base_token_price_quote_token = pool_info
        token_info_message = (
            f"🔍 **Token Information:**\n"
            f"🪙 **Base Token Name:** {base_token_name}\n"
            f"💱 **Quote Token Name:** {quote_token_name}\n"
            f"📍 **Pool Address:** `{pool_address}`\n"
            f"💵 **Fully Diluted Valuation (FDV) USD:** ${fdv_usd}\n"
            f"💧 **Liquidity in USD:** ${reserve_in_usd}\n"
            f"💰 **Base Token Price in Quote Token:** {base_token_price_quote_token}\n\n"
            "Please enter the amount you want to buy:"
        )
        context.user_data['next_action'] = 'snipe_token_amount'
        update.message.reply_text(
            token_info_message,
            parse_mode="Markdown",  # Use Markdown to format the message nicely
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
            ])
        )
    else:
        update.message.reply_text(
            "⚠️ No pools found with this token or an error occurred. Please check the token address and try again.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
            ])
        )
async def get_user_tokens(wallet_address):
    url = f"https://tonapi.io/v2/accounts/{wallet_address}/jettons?currencies=ton,usd,rub"
    try:
        response = requests.get(url)
        response.raise_for_status()  # Поднять исключение для любых HTTP ошибок
        data = response.json()

        # Логирование для отладки
        logger.info(f"Received data: {data}")

        if "balances" in data:
            return data["balances"]
        else:
            return []
    except requests.exceptions.HTTPError as e:
        logger.error(f"HTTP error occurred: {e}")
        return []
    except ValueError as e:
        # Это поймает ошибку, если JSON не удалось распарсить
        logger.error(f"Error parsing JSON response: {e}")
        logger.error(f"Raw response content: {response.text}")  # Логируем сырое содержимое ответа
        return []
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        return []
def handle_snipe_token_amount(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    amount = update.message.text
    try:
        amount = float(amount)
    except ValueError:
        update.message.reply_text("❌ Please enter a valid number.")
        return

    token_address = context.user_data['snipe_token_address']
    update.message.reply_text("Buy process started successfully! Searching for token pool...")

    def run_snipe_task():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        sniping_tasks[user_id] = {'task': loop.create_task(snipe_token(user_id, token_address, amount, update.message)), 'cancel': False}
        loop.run_until_complete(sniping_tasks[user_id]['task'])
        loop.close()

    thread = threading.Thread(target=run_snipe_task)
    thread.start()

# Функция для создания кнопок с сокращенными данными и сохранением полного адреса в context.user_data
def handle_sell_tokens_start(query, context):
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
    
    # Fetching tokens
    async def fetch_and_show_tokens():
        tokens = await get_user_tokens(wallet_address)

        if not tokens:
            send_or_edit_message(
                query,
                "⚠️ No tokens found in your wallet.",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
                ])
            )
            return

        # Формируем сообщение с токенами и их балансами
        token_buttons = []
        for i, token in enumerate(tokens):
            token_address = token['jetton']['address']
            token_name = token.get('jetton', {}).get('name', 'Unknown Token')
            token_balance = token.get('balance', 0)
            token_symbol = token.get('jetton', {}).get('symbol', 'N/A')

            # Сохраняем полный адрес в context.user_data
            context.user_data[f'token_address_{i}'] = token_address

            # В callback_data передаем индекс токена вместо полного адреса
            token_buttons.append([
                InlineKeyboardButton(f"{token_name} ({token_symbol}) - {token_balance}",
                                     callback_data=f'sell_token_{i}')
            ])

        token_buttons.append([InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')])

        send_or_edit_message(
            query,
            "💰 Select a token to sell:",
            InlineKeyboardMarkup(token_buttons)
        )

    # Запускаем асинхронный процесс получения токенов
    asyncio.run(fetch_and_show_tokens())

def handle_token_selection(query, context):
    user_id = query.from_user.id

    # Извлекаем индекс токена из callback_data
    token_index = int(query.data.split('sell_token_')[-1])

    # Получаем полный адрес токена из context.user_data
    token_address = context.user_data.get(f'token_address_{token_index}')
    if not token_address:
        logger.error("Token address not found in context.user_data")
        send_or_edit_message(
            query,
            "❌ An error occurred: Token address not found.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
            ])
        )
        return

    # Преобразуем адрес в пользовательский формат
    token_address = convert_to_user_friendly_format(token_address)

    context.user_data['sell_token_address'] = token_address
    logger.info(f"Token address selected: {token_address}")

    # Далее вызывается функция для получения баланса
    async def fetch_balance():
        try:
            wallet_address = context.user_data.get('wallet_address')
            token_balance = await get_token_balance(wallet_address, token_address)
            context.user_data['token_balance'] = token_balance
            send_or_edit_message(
                query,
                f"💰 Your token balance: {token_balance} tokens.\nPlease enter the amount you want to sell:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
                ])
            )
        except Exception as e:
            logger.error(f"An error occurred while fetching token balance: {e}")
            send_or_edit_message(query, f"❌ An error occurred: {str(e)}")
            
    asyncio.run(fetch_balance())
    context.user_data['next_action'] = 'sell_token_amount'

def handle_sell_token_address(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    token_address = update.message.text
    context.user_data['sell_token_address'] = token_address
    context.user_data['next_action'] = None  # Clear next_action to prevent loop
    update.message.reply_text(
        "🔄 Fetching token balance...",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back to Buy Menu", callback_data='sniping')]
        ])
    )

    def run_fetch_balance():
        asyncio.set_event_loop(asyncio.new_event_loop())
        loop = asyncio.get_event_loop()
        loop.run_until_complete(fetch_token_balance(update, context, user_id, token_address))
        loop.close()

    threading.Thread(target=run_fetch_balance).start()


async def fetch_token_balance(update: Update, context: CallbackContext, user_id, token_address):
    logger.info(f"Fetching token balance for user_id: {user_id}, token_address: {token_address}")

    wallet = get_user_wallet(user_id)
    if not wallet:
        logger.error("No wallet found for the user.")
        update.message.reply_text("❌ No wallet found for your account. Please create a wallet first.")
        return

    wallet_address = wallet['address']
    logger.info(f"User's wallet address: {wallet_address}")

    token_balance = await get_token_balance(wallet_address, token_address)
    logger.info(f"Fetched token balance: {token_balance}")

    context.user_data['token_balance'] = token_balance

    update.message.reply_text(
        f"💰 Your token balance: {token_balance} tokens.\nEnter the amount you want to sell:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back to Sniping Menu", callback_data='sniping')]
        ])
    )
    context.user_data['next_action'] = 'sell_token_amount'  # Set next_action here after fetching balance


def handle_sell_token_amount(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    amount = update.message.text
    logger.info(f"User {user_id} entered amount: {amount}")

    try:
        amount = float(amount)
    except ValueError:
        update.message.reply_text("❌ Please enter a valid number.")
        return

    token_balance = context.user_data.get('token_balance')
    if token_balance is None:
        update.message.reply_text("❌ Unable to retrieve token balance. Please try again.")
        return

    logger.info(f"Current token balance: {token_balance}")

    # if amount > token_balance:
    #     update.message.reply_text("❌ Insufficient token balance.")
    #     return

    token_address = context.user_data['sell_token_address']
    update.message.reply_text("🚀 Selling tokens...")

    def run_sell_task():
        asyncio.set_event_loop(asyncio.new_event_loop())
        loop = asyncio.get_event_loop()
        loop.run_until_complete(sell_tokens(user_id, token_address, amount, update.message))
        loop.close()

    threading.Thread(target=run_sell_task).start()

async def get_token_balance(wallet_address, token_address):
    try:
        provider = LiteBalancer.from_mainnet_config(2)
        await provider.start_up()

        # Retrieve the address of the user's token wallet
        result_stack = await provider.run_get_method(
            address=token_address,
            method="get_wallet_address",
            stack=[begin_cell().store_address(Address(wallet_address)).end_cell().begin_parse()]
        )
        token_wallet_address = result_stack[0].load_address()
        logger.info(f"Token wallet address for {wallet_address}: {token_wallet_address}")

        # Query the token balance
        result_stack = await provider.run_get_method(
            address=token_wallet_address,
            method="get_wallet_data",
            stack=[]
        )
        balance = result_stack[0] if isinstance(result_stack[0], int) else result_stack[0].load_uint(128)
        logger.info(f"Token balance: {balance}")

        await provider.close_all()
        return balance / 10**9  # Assuming the balance is in nanoton and converting to ton
    except Exception as e:
        logger.error(f"An error occurred: {e}")
        return 0.0

import asyncio
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def sell_tokens(user_id, token_address, amount, message):
    # Конвертируем адрес токена в user-friendly формат
    converted_token_address = convert_to_user_friendly_format(token_address)
    
    mnemonics = get_user_mnemonics(user_id)  # Функция для получения мнемоник фразы пользователя из базы данных
    if not mnemonics:
        await message.reply_text("❌ No wallet found for your account. Please create a wallet first.")
        return

    jetton_sell_address = AddressV1(converted_token_address)

    try:    
        provider = LiteBalancer.from_mainnet_config(2)
        await provider.start_up()
        wallet = await WalletV4R2.from_mnemonic(provider=provider, mnemonics=mnemonics)

        params = await router.build_swap_jetton_to_ton_tx_params(
            user_wallet_address=wallet.address,
            offer_jetton_address=jetton_sell_address,
            offer_amount=int(amount * 1e9),
            min_ask_amount=0,
            provider=provider
        )

        await wallet.transfer(destination=params['to'],
                              amount=int(0.35 * 1e9),  # Gas_amount
                              body=params['payload'])
        await provider.close_all()

        await message.reply_text(f"✅ Successfully sold {amount} tokens.")
    except Exception as e:
        logger.error(f"An error occurred during the token sale: {e}")
        await message.reply_text(f"❌ An error occurred: {str(e)}")

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
            f"🔑 Your seed phrase: <code>{seed_phrase}</code>"  # Use <code> tags for HTML formatting
        )
        send_or_edit_message(
            query,
            message_text,
            InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
            ]),
            parse_mode="HTML"  # Ensure HTML is used for formatting
        )
    else:
        send_or_edit_message(
            query,
            "❌ No wallet found for your account. Please create a wallet first.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
            ])
        )

def main():
    TOKEN = '6792803709:AAGnevuXBzFJJ7bi0YYX3mm7zsvF2V1aIs0'
    updater = Updater(TOKEN, use_context=True)

    dispatcher = updater.dispatcher

    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(CallbackQueryHandler(handle_callback_query))
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    dispatcher.add_handler(CallbackQueryHandler(handle_token_selection, pattern=r'^sell_token_'))

    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()