    import unittest
from unittest.mock import MagicMock, patch
import asyncio
import sqlite3
from main import (
    start, handle_callback_query, get_user_wallet, get_wallet_balance,
    handle_wallet, display_transaction_sent, display_transaction_failed, 
    snipe_token, handle_refresh, cancel_snipe
)
from telegram import Update, CallbackQuery, Message
from telegram.ext import CallbackContext


class TestBotHandlers(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.c = self.conn.cursor()
        self.c.execute('''
            CREATE TABLE user_wallets (
                user_id INTEGER PRIMARY KEY, address TEXT, seed TEXT
            )
        ''')
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    @patch('main.get_user_wallet')
    @patch('main.get_wallet_balance')
    def test_handle_wallet(self, mock_get_wallet_balance, mock_get_user_wallet):
        mock_get_user_wallet.return_value = {'address': 'TestAddress', 'mnemonics': 'mnemonic phrase'}
        mock_get_wallet_balance.return_value = 10.5

        update = MagicMock(spec=Update)
        context = MagicMock(spec=CallbackContext)
        query = MagicMock(spec=CallbackQuery)
        query.from_user.id = 1
        query.message.photo = False

        handle_wallet(query, context)

        mock_get_user_wallet.assert_called_once_with(1)
        mock_get_wallet_balance.assert_called_once()
        query.edit_message_text.assert_called_once_with(
            text="👛 Wallet Menu\n\n💳 Your wallet address: `TestAddress`\n💰 Current Balance: 10.5 TON",
            reply_markup=unittest.mock.ANY,
            parse_mode="Markdown"
        )

    @patch('main.RouterV1')
    @patch('main.get_user_wallet')
    @patch('main.snipe_token')
    def test_snipe_token(self, mock_snipe_token, mock_get_user_wallet, mock_router_v1):
        mock_get_user_wallet.return_value = {'address': 'TestAddress', 'mnemonics': 'mnemonic phrase'}
        mock_router_v1.return_value = MagicMock()

        update = MagicMock(spec=Update)
        context = MagicMock(spec=CallbackContext)
        update.message.from_user.id = 1
        context.user_data = {'snipe_token_address': 'testToken'}

        asyncio.run(handle_snipe_token_amount(update, context))

        mock_snipe_token.assert_called_once()

    @patch('main.get_user_wallet')
    @patch('main.get_wallet_balance')
    @patch('main.display_transaction_sent')
    def test_display_transaction_sent(self, mock_display_transaction_sent, mock_get_wallet_balance, mock_get_user_wallet):
        mock_get_wallet_balance.return_value = 5.0
        mock_get_user_wallet.return_value = {'address': 'TestAddress', 'mnemonics': 'mnemonic phrase'}

        message = MagicMock(spec=Message)
        context = MagicMock(spec=CallbackContext)

        display_transaction_sent(message, context, 10, 'tokenAddress')

        mock_display_transaction_sent.assert_called_once_with(
            message,
            context,
            10,
            'tokenAddress'
        )

    @patch('main.get_user_wallet')
    @patch('main.get_wallet_balance')
    def test_handle_refresh(self, mock_get_wallet_balance, mock_get_user_wallet):
        mock_get_user_wallet.return_value = {'address': 'TestAddress', 'mnemonics': 'mnemonic phrase'}
        mock_get_wallet_balance.return_value = 5.0

        query = MagicMock(spec=CallbackQuery)
        context = MagicMock(spec=CallbackContext)
        query.message.caption = "💳 Your wallet address: `TestAddress`\n💰 Current Balance: 1 TON"

        handle_refresh(query, context)

        query.edit_message_caption.assert_called_once_with(
            chat_id=query.message.chat_id,
            message_id=query.message.message_id,
            caption="💳 Your wallet address: `TestAddress`\n💰 Current Balance: 5.0 TON",
            reply_markup=query.message.reply_markup,
            parse_mode="Markdown"
        )


if __name__ == '__main__':
    unittest.main()
