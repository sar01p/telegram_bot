import re
import os
import logging
import nest_asyncio
import requests
import asyncio
from telegram import Update
from telegram.ext import Application, ContextTypes, CommandHandler, MessageHandler, filters

# Apply nest_asyncio to allow nested event loops if needed
nest_asyncio.apply()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)

def is_solana_contract_address(text):
    pattern = r'[1-9A-HJ-NP-Za-km-z]{43,44}'
    matches = re.findall(pattern, text)
    if matches:
        return matches[0]  # Assuming the first match is the contract address
    return None

def format_number(num):
    if num == "N/A":
        return num
    num = float(num)
    if num >= 1_000_000:
        return f"{num/1_000_000:.1f}M"
    elif num >= 1_000:
        return f"{num/1_000:.1f}k"
    else:
        return str(num)

# Fetch market cap and ticker from DEX Screener
async def fetch_dex_screener_data(ca):
    try:
        url = f"https://api.dexscreener.io/latest/dex/tokens/{ca}"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if 'pairs' in data and data['pairs']:
            pair = data['pairs'][0]
            market_cap = pair.get('fdv', 'N/A')  # Fully Diluted Valuation, use 'mcap' for circulating market cap if available
            ticker = pair.get('baseToken', {}).get('symbol', 'N/A')
            logging.info(f"Market Cap for {ca}: {market_cap}")
            return format_number(market_cap), ticker
        else:
            logging.info(f"No pairs data for {ca}")
            return "N/A", "N/A"
    except requests.RequestException as e:
        logging.error(f"Failed to fetch data from DEX Screener: {e}")
        return "N/A", "N/A"
    except Exception as e:
        logging.error(f"Unexpected error in fetch_dex_screener_data: {e}")
        return "N/A", "N/A"

# Cache for processed contract addresses
processed_contracts = set()

# Global state for test
test_state = {
    'running': False, 
    'start_sol': 0.0, 
    'current_sol': 0.0, 
    'buy_amount': 0.0, 
    'in_positions': 0.0,
    'positions': {},
    'last_buy_time': 0
}

async def send_to_chats(context: ContextTypes.DEFAULT_TYPE, message, ca, bought_market_cap=None, selling_market_cap=None, ticker=None, invested_sol=None, profit_sol=None, remaining_percentage=None, exclude_chat=None):
    chat_id = -1002479118522  # The specific chat ID you want to send messages to
    
    if bought_market_cap is None and selling_market_cap is None:  # Buy notification
        formatted_message = message
    else:  # Sell notification
        growth_percentage = ((selling_market_cap - bought_market_cap) / bought_market_cap * 100) if bought_market_cap > 0 else 0
        formatted_message = (
            f"🔄 **Position Sold** 🔄\n\n"
            f"🔹 **Contract Address:** {ca}\n"
            f"🔹 **Token Ticker:** {ticker}\n"
            f"🔹 **Bought Market Cap:** {format_number(bought_market_cap)}\n"
            f"🔹 **Selling Market Cap:** {format_number(selling_market_cap)}\n"
            f"🔹 **Growth:** {growth_percentage:.2f}%\n"
            f"🔹 **Invested SOL:** {invested_sol:.3f}\n"
            f"🔹 **Profit SOL:** {profit_sol:.3f}\n"
            f"🔹 **Remaining:** {remaining_percentage}%"
        )

    try:
        await context.bot.send_message(chat_id=chat_id, 
                                       text=formatted_message,
                                       parse_mode='Markdown')
        logging.info(f"Successfully sent message for CA: {ca} to chat {chat_id}")
    except Exception as e:
        logging.error(f"Failed to send message to chat {chat_id}: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot is active!")

async def test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not test_state['running']:
        await update.message.reply_text("Enter the SOL amount to start trading (use dot for decimal, e.g., 100.000):")
        context.user_data['state'] = 'await_start_sol'
    else:
        await update.message.reply_text("A test is already running. Use /pause to stop it.")
        logging.info("monitor_positions has not been started because a test is already running")

async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Check if update.message exists
    if update.message is None:
        logging.info("Received update without message content")
        return

    text = update.message.text or update.message.caption  # Check both text and caption
    if 'state' in context.user_data:
        try:
            # Allow for various number formats like 100.000, 100,000, 100, or 100.0
            amount = float(text.replace(',', ''))
            logging.info(f"Received input '{text}' for state {context.user_data['state']}")
            if context.user_data['state'] == 'await_start_sol':
                test_state['start_sol'] = test_state['current_sol'] = amount
                context.user_data['state'] = 'await_buy_sol'
                await update.message.reply_text("Enter the SOL amount per buy (e.g., 1.000):")
            elif context.user_data['state'] == 'await_buy_sol':
                test_state['buy_amount'] = amount
                test_state['running'] = True
                await update.message.reply_text("Test started. Use /view to check status.")
                context.user_data.pop('state', None)
                asyncio.create_task(monitor_positions_automatic(context))
        except ValueError:
            await update.message.reply_text("Please enter a valid number for SOL. Use dots for decimals, e.g., 100.000.")
    else:
        if "ALWAYS DYOR" in text.upper():  # Check for the phrase case-insensitively
            ca_match = is_solana_contract_address(text)
            if ca_match and ca_match not in processed_contracts:
                processed_contracts.add(ca_match)  # Add to processed to avoid duplicates
                market_cap, ticker = await fetch_dex_screener_data(ca_match)
                if test_state['running']:
                    if test_state['buy_amount'] <= test_state['current_sol']:
                        fee = test_state['buy_amount'] * 0.01
                        actual_buy_amount = test_state['buy_amount'] - fee
                        test_state['current_sol'] -= test_state['buy_amount']
                        test_state['in_positions'] += actual_buy_amount
                        test_state['positions'][ca_match] = {'buy_market_cap': market_cap, 'amount': actual_buy_amount, 'sold': 0.0, 'ticker': ticker}
                        
                        buy_message = f"Bought {ca_match} (Ticker: {ticker}) at Market Cap: {market_cap} with {test_state['buy_amount']:.3f} SOL (Fee: {fee:.3f} SOL)"
                        await update.message.reply_text(buy_message)
                        
                        await send_to_chats(context, buy_message, ca_match)
                        
                        logging.info(f"Sent buy notification for Solana contract address: {ca_match}")
                    else:
                        await update.message.reply_text("Not enough SOL to buy this token.")
                else:
                    await update.message.reply_text("Start a test with /test first to buy tokens.")
            elif ca_match in processed_contracts:
                logging.info(f"Already processed contract address: {ca_match}")
            else:
                logging.info("No valid contract address found in the message.")
        else:
            # Only log if it's not a command message
            if not text.startswith('/'):
                logging.info("Message does not contain 'ALWAYS DYOR' phrase.")

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if test_state['running']:
        current_time = asyncio.get_event_loop().time()
        if current_time - test_state['last_buy_time'] < 1:  # Prevent buying too frequently
            await update.message.reply_text("Please wait a second before the next buy.")
            return

        buy_amount = test_state['buy_amount']
        if buy_amount <= test_state['current_sol']:
            # 1% fee deduction
            fee = buy_amount * 0.01
            actual_buy_amount = buy_amount - fee
            
            # Exponential increase for simulation
            if 'TOKEN' in test_state['positions']:
                increase_percentage = 300 if test_state['positions']['TOKEN']['amount'] < 500 else 1000
                token_amount = test_state['positions']['TOKEN']['amount'] * (1 + increase_percentage / 100.0)
            else:
                token_amount = buy_amount * 3  # Initial 300% increase for the first buy

            test_state['current_sol'] -= buy_amount
            test_state['in_positions'] += token_amount
            test_state['positions']['TOKEN'] = {
                'buy_price': 1.0, 
                'amount': token_amount, 
                'sold': 0.0
            }
            test_state['last_buy_time'] = current_time

            growth = (token_amount / actual_buy_amount - 1) * 100 if actual_buy_amount > 0 else 0
            await update.message.reply_text(f"Bought TOKEN with {buy_amount:.3f} SOL (Fee: {fee:.3f} SOL). Token growth: {growth:.2f}%")
        else:
            await update.message.reply_text("Not enough SOL to buy.")
    else:
        await update.message.reply_text("Start a test with /test first.")

async def sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if test_state['running']:
        if 'TOKEN' in test_state['positions']:
            position = test_state['positions']['TOKEN']
            sell_amount = position['amount'] - position['sold']
            test_state['current_sol'] += sell_amount
            test_state['in_positions'] -= sell_amount
            del test_state['positions']['TOKEN']
            await update.message.reply_text(f"Sold TOKEN for {sell_amount:.3f} SOL")
        else:
            await update.message.reply_text("No position to sell.")
    else:
        await update.message.reply_text("Start a test with /test first.")

async def view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if test_state['running']:
        growth = ((test_state['current_sol'] + test_state['in_positions']) / test_state['start_sol'] - 1) * 100 if test_state['start_sol'] > 0 else 0
        message = (
            "🎉 **Portfolio Overview** 🎉\n\n"
            f"🔹 **Started with:** {test_state['start_sol']:.3f} SOL\n"
            f"🔹 **Current SOL:** {test_state['current_sol']:.3f} SOL\n"
            f"🔹 **SOL in Positions:** {test_state['in_positions']:.3f} SOL\n"
            f"🔹 **Total Growth:** {growth:.2f}%\n"
            f"🔹 **Positions:** {len(test_state['positions'])}"
        )
        await update.message.reply_text(message, parse_mode='Markdown')
    else:
        await update.message.reply_text("No test is currently running.")

async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if test_state['running']:
        test_state['running'] = False
        test_state['start_sol'] = 0.0
        test_state['current_sol'] = 0.0
        test_state['buy_amount'] = 0.0
        test_state['in_positions'] = 0.0
        test_state['positions'] = {}
        test_state['last_buy_time'] = 0
        context.user_data.clear()
        await update.message.reply_text("Test paused and state reset.")
    else:
        await update.message.reply_text("No test is currently running to pause.")

async def monitor_positions_automatic(context: ContextTypes.DEFAULT_TYPE):
    logging.info("Starting automatic monitor_positions task")
    check_count = 0  # Counter for checks
    while test_state['running']:
        try:
            logging.info(f"Checking positions... (Check #{check_count})")
            for ca, position in list(test_state['positions'].items()):
                if ca != 'TOKEN':  # Skip the 'TOKEN' placeholder from previous tests
                    bought_market_cap = float(position['buy_market_cap'].replace('k', 'e3').replace('M', 'e6'))  # Convert to float
                    market_cap, ticker = await fetch_dex_screener_data(ca)
                    if market_cap == "N/A":
                        logging.warning(f"No market cap data for {ca}")
                        continue
                    try:
                        market_cap = float(market_cap.replace('k', 'e3').replace('M', 'e6'))  # Convert k and M to numeric values
                        
                        if market_cap < 50000:  # Below 50k, forget this token
                            test_state['positions'].pop(ca)
                            test_state['in_positions'] -= position['amount'] - position['sold']
                            logging.info(f"Removed position for {ca} due to low market cap.")
                            continue

                        growth = market_cap / bought_market_cap if bought_market_cap > 0 else 1
                        initial_investment = position['amount']
                        sold = position['sold']
                        
                        if growth >= 3.0 and sold < initial_investment * 0.5:  # 300%
                            sell_percentage = 0.5
                        elif growth >= 5.0 and sold < initial_investment * 0.75:  # 500%
                            sell_percentage = 0.25
                        elif growth >= 10.0 and sold < initial_investment:  # 1000%
                            sell_percentage = 1 - (sold / initial_investment)  # Sell all remaining
                        else:
                            continue  # No sell action if conditions aren't met

                        sell_amount = initial_investment * sell_percentage
                        profit_sol = sell_amount * growth - sold * (growth - 1)  # Profit calculation adjusted for growth
                        
                        test_state['current_sol'] += profit_sol
                        test_state['in_positions'] -= sell_amount
                        position['sold'] += sell_amount

                        # Send notification
                        remaining_percentage = round((1 - position['sold'] / initial_investment) * 100) if position['sold'] < initial_investment else 0
                        await send_to_chats(context, None, ca, bought_market_cap, market_cap, ticker, 
                                            position['amount'], profit_sol, 
                                            remaining_percentage)
                        
                        if position['sold'] >= position['amount']:
                            del test_state['positions'][ca]
                    
                    except ValueError as ve:
                        logging.error(f"Error converting market cap for {ca}: {ve}")
                    except Exception as e:
                        logging.error(f"Unexpected error in monitor_positions_automatic for CA {ca}: {e}")
            
            check_count += 1  # Increment check counter
            logging.info("Sleeping for next check...")
            await asyncio.sleep(300)  # Check every 5 minutes
        except Exception as e:
            logging.error(f"Unexpected error in automatic monitor_positions: {e}")

async def check_monitor_positions_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not test_state['running']:
        await update.message.reply_text("No test is currently running. Start one with /test.")
        return

    await update.message.reply_text("Checking positions now...")
    logging.info(f"Positions count: {len(test_state['positions'])}")

    try:
        for ca, position in test_state['positions'].items():
            logging.info(f"Checking position for CA: {ca}")
            if ca != 'TOKEN':  # Skip the 'TOKEN' placeholder from previous tests
                market_cap, ticker = await fetch_dex_screener_data(ca)
                if market_cap == "N/A":
                    logging.warning(f"No market cap data for {ca}")
                    await update.message.reply_text(f"Market cap data unavailable for {ca}")
                    continue
                
                # Store raw market cap for display later
                raw_market_cap = market_cap

                try:
                    # Convert market cap to float, handle different formats
                    if 'k' in market_cap:
                        market_cap = float(market_cap.replace('k', '')) * 1000
                    elif 'M' in market_cap:
                        market_cap = float(market_cap.replace('M', '')) * 1_000_000
                    else:
                        market_cap = float(market_cap)

                    buy_market_cap = position['buy_market_cap']
                    if 'k' in buy_market_cap:
                        buy_market_cap = float(buy_market_cap.replace('k', '')) * 1000
                    elif 'M' in buy_market_cap:
                        buy_market_cap = float(buy_market_cap.replace('M', '')) * 1_000_000
                    else:
                        buy_market_cap = float(buy_market_cap)
                    
                    growth_percentage = ((market_cap - buy_market_cap) / buy_market_cap * 100) if buy_market_cap > 0 else 0
                    
                    # Format market cap for display
                    formatted_current_market_cap = format_number(str(market_cap))
                    formatted_buy_market_cap = format_number(str(buy_market_cap))

                    position_details = (
                        f"🔹 **Contract Address:** {ca}\n"
                        f"🔹 **Token Ticker:** {ticker}\n"
                        f"🔹 **Bought Market Cap:** {formatted_buy_market_cap}\n"
                        f"🔹 **Current Market Cap:** {formatted_current_market_cap}\n"
                        f"🔹 **Growth:** {growth_percentage:.2f}%\n"
                    )
                    await update.message.reply_text(position_details, parse_mode='Markdown')
                    logging.info(f"Sent details for CA: {ca}")
                    
                    if market_cap < 50000:  # Below 50k, forget this token
                        test_state['positions'].pop(ca)
                        test_state['in_positions'] -= position['amount'] - position['sold']
                        logging.info(f"Removed position for {ca} due to low market cap.")
                        await update.message.reply_text(f"Removed position for {ca} because market cap is below 50k.")
                    
                except ValueError:
                    logging.error(f"Market cap for {ca} was not in expected format: {raw_market_cap}")
                    await update.message.reply_text(f"Market cap data for {ca} is not in a convertible format. Raw data: {raw_market_cap}")
                    continue  # Skip this position
        
        if not test_state['positions'] or all(ca == 'TOKEN' for ca in test_state['positions']):
            logging.info("No positions to monitor or only TOKEN placeholder exists.")
            await update.message.reply_text("No active positions to monitor.")

    except Exception as e:
        logging.error(f"Unexpected error in check_monitor_positions_manual: {e}")
        await update.message.reply_text("An error occurred while checking positions.")
async def main():
    application = Application.builder().token(os.getenv("BOT_TOKEN")).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("test", test))
    application.add_handler(CommandHandler("view", view))
    application.add_handler(CommandHandler("pause", pause))
    application.add_handler(CommandHandler("buy", buy))
    application.add_handler(CommandHandler("sell", sell))
    application.add_handler(CommandHandler("monitor_positions", check_monitor_positions_manual))
    application.add_handler(MessageHandler(filters.TEXT | filters.CAPTION & (~filters.COMMAND), handle_input))

    await application.run_polling()

if __name__ == '__main__':
    asyncio.run(main())
