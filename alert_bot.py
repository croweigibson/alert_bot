import asyncio
import json
import os
import logging
import websockets
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8455948992:AAGbO8Hkw8OOgAMrhWhuc6JjVqI9QjOUQ0g")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_BOT_TOKEN", "824922767")
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "105850")
ALERT_FILE = "price_alerts.json" # File for persistence

# Price alerts storage
price_alerts = {}

# Deriv WebSocket connection and monitoring task global variables
deriv_ws = None
monitoring_task = None

# --- PERSISTENCE FUNCTIONS ---

def load_alerts():
    """Load price alerts from a JSON file."""
    global price_alerts
    if os.path.exists(ALERT_FILE):
        try:
            with open(ALERT_FILE, 'r') as f:
                data = json.load(f)
                # Convert string keys (from JSON) back to int for alert_ids
                price_alerts = {
                    symbol: {int(k): v for k, v in alerts.items()}
                    for symbol, alerts in data.items()
                }
            logger.info(f"Loaded {sum(len(a) for a in price_alerts.values())} alerts from {ALERT_FILE}")
        except Exception as e:
            logger.error(f"Error loading alerts from file: {e}")

def save_alerts():
    """Save price alerts to a JSON file."""
    try:
        with open(ALERT_FILE, 'w') as f:
            json.dump(price_alerts, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving alerts to file: {e}")

# --- DERIV CONNECTION AND MONITORING ---

async def connect_deriv():
    """Connect to Deriv WebSocket API"""
    global deriv_ws
    if deriv_ws and not deriv_ws.closed:
        return deriv_ws 

    uri = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
    deriv_ws = await websockets.connect(uri)
    logger.info("Connected to Deriv WebSocket")
    return deriv_ws

async def subscribe_to_symbol(symbol):
    """Subscribe to price updates for a symbol"""
    try:
        if deriv_ws is None or deriv_ws.closed:
            await connect_deriv()

        subscribe_request = {
            "ticks": symbol,
            "subscribe": 1
        }
        await deriv_ws.send(json.dumps(subscribe_request))
        logger.info(f"Subscribed to {symbol}")
    except Exception as e:
        logger.error(f"Error subscribing to {symbol}: {e}")

async def monitor_prices(application):
    """Monitor prices and send alerts"""
    global deriv_ws
    
    active_subscriptions = set()

    while True:
        try:
            if deriv_ws is None or deriv_ws.closed:
                await connect_deriv()
                for symbol in price_alerts:
                    if symbol not in active_subscriptions:
                         await subscribe_to_symbol(symbol)
                         active_subscriptions.add(symbol)
            
            symbols_with_alerts = set(price_alerts.keys())
            active_subscriptions = active_subscriptions.intersection(symbols_with_alerts)

            response = await asyncio.wait_for(deriv_ws.recv(), timeout=30.0)
            data = json.loads(response)
            
            if "tick" in data:
                symbol = data["tick"]["symbol"]
                current_price = float(data["tick"]["quote"])
                
                if symbol in price_alerts:
                    alerts_to_remove = []
                    
                    for alert_id, alert in list(price_alerts[symbol].items()):
                        target_price = alert["target_price"]
                        condition = alert["condition"]
                        chat_id = alert["chat_id"]
                        
                        triggered = False
                        if condition == "above" and current_price >= target_price:
                            triggered = True
                            message = f"🚨 ALERT: {symbol} is now {current_price:.2f} (above {target_price:.2f})"
                        elif condition == "below" and current_price <= target_price:
                            triggered = True
                            message = f"🚨 ALERT: {symbol} is now {current_price:.2f} (below {target_price:.2f})"
                        
                        if triggered:
                            try:
                                await application.bot.send_message(
                                    chat_id=chat_id,
                                    text=message
                                )
                                alerts_to_remove.append(alert_id)
                            except Exception as e:
                                logger.error(f"Error sending alert to chat {chat_id}: {e}")
                    
                    for alert_id in alerts_to_remove:
                        del price_alerts[symbol][alert_id]
                    
                    if not price_alerts[symbol]:
                        del price_alerts[symbol]
                        active_subscriptions.discard(symbol)
                        
                    # Save alerts after triggering/removing them
                    save_alerts() 
                        
            elif data.get('msg_type') == 'ping':
                await deriv_ws.send(json.dumps({'pong': 1}))
                
        except asyncio.TimeoutError:
            logger.warning("WebSocket timeout. Reconnecting...")
            deriv_ws = None 
        except asyncio.CancelledError:
            logger.info("Monitoring task cancelled")
            break
        except websockets.exceptions.ConnectionClosed:
            logger.info("Deriv WebSocket connection closed. Reconnecting...")
            deriv_ws = None
        except Exception as e:
            logger.error(f"Error monitoring prices: {e}")
            await asyncio.sleep(5)
            deriv_ws = None 

# --- TELEGRAM COMMAND HANDLERS ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    welcome_message = """
Welcome to Deriv Price Alert Bot! 🤖

Available commands:
/setalert <symbol> <above/below> <price> - Set a price alert
/listalerts - List your active alerts
/deletealert <symbol> <id> - Delete a specific alert
/symbols - Show available symbols
/price <symbol> - Get current price

Example:
/setalert R_10 above 500.50
/deletealert R_10 1
"""
    await update.message.reply_text(welcome_message)

async def symbols_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show available symbols"""
    symbols_list = """
📊 Available Deriv Synthetic Indices:

Volatility Indices:
- R_10 - Volatility 10 Index
- R_25 - Volatility 25 Index
- R_50 - Volatility 50 Index
- R_75 - Volatility 75 Index
- R_100 - Volatility 100 Index

Crash/Boom:
- BOOM500 - Boom 500 Index
- BOOM1000 - Boom 1000 Index
- CRASH500 - Crash 500 Index
- CRASH1000 - Crash 1000 Index

Step Indices:
- stpRNG - Step Index

Range Breaks:
- RDBEAR - Range Break Bear
- RDBULL - Range Break Bull
"""
    await update.message.reply_text(symbols_list)

async def setalert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /setalert command"""
    try:
        if len(context.args) != 3:
            await update.message.reply_text(
                "Usage: /setalert <symbol> <above/below> <price>\n"
                "Example: /setalert R_10 above 500.50"
            )
            return
        
        symbol = context.args[0].upper()
        condition = context.args[1].lower()
        target_price = float(context.args[2])
        
        if condition not in ["above", "below"]:
            await update.message.reply_text("Condition must be 'above' or 'below'")
            return
        
        if symbol not in price_alerts:
            price_alerts[symbol] = {}
            await subscribe_to_symbol(symbol)
        
        alert_id = len(price_alerts[symbol]) + 1
        price_alerts[symbol][alert_id] = {
            "target_price": target_price,
            "condition": condition,
            "chat_id": update.effective_chat.id
        }
        
        save_alerts() # Save the new alert

        await update.message.reply_text(
            f"✅ Alert set!\n"
            f"Symbol: {symbol}\n"
            f"Condition: {condition} {target_price:.2f}\n"
            f"Alert ID: {alert_id}"
        )
        
    except ValueError:
        await update.message.reply_text("Invalid price value. Please use a number.")
    except Exception as e:
        logger.error(f"Error in setalert: {e}")
        await update.message.reply_text(f"Error setting alert: {str(e)}")

async def listalerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all active alerts"""
    chat_id = update.effective_chat.id
    user_alerts = []
    
    for symbol, alerts in price_alerts.items():
        for alert_id, alert in alerts.items():
            if alert["chat_id"] == chat_id:
                user_alerts.append(
                    f"• {symbol}: {alert['condition']} {alert['target_price']:.2f} (ID: {alert_id})"
                )
    
    if user_alerts:
        message = "📋 Your Active Alerts:\n\n" + "\n".join(user_alerts)
    else:
        message = "You have no active alerts."
    
    await update.message.reply_text(message)

async def deletealert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /deletealert <symbol> <alert_id> command"""
    chat_id = update.effective_chat.id
    
    try:
        if len(context.args) != 2:
            await update.message.reply_text(
                "Usage: /deletealert <symbol> <alert_id>\n"
                "Example: /deletealert R_10 1\n"
                "Use /listalerts to see IDs."
            )
            return
        
        symbol = context.args[0].upper()
        alert_id = int(context.args[1])
        
        if symbol not in price_alerts or alert_id not in price_alerts[symbol]:
            await update.message.reply_text(f"Alert ID {alert_id} for {symbol} not found.")
            return
        
        alert = price_alerts[symbol][alert_id]
        
        if alert["chat_id"] != chat_id:
            await update.message.reply_text("You can only delete your own alerts.")
            return

        del price_alerts[symbol][alert_id]
        
        if not price_alerts[symbol]:
            del price_alerts[symbol]

        save_alerts() # Save after deleting

        await update.message.reply_text(f"🗑️ Alert ID {alert_id} for {symbol} deleted successfully.")
        
    except ValueError:
        await update.message.reply_text("Invalid Alert ID or format. ID must be a number.")
    except Exception as e:
        logger.error(f"Error in deletealert: {e}")
        await update.message.reply_text(f"Error deleting alert: {str(e)}")

async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (content remains the same as before) ...
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /price <symbol>\nExample: /price R_10")
        return
    
    symbol = context.args[0].upper()
    
    try:
        if deriv_ws is None or deriv_ws.closed:
            await connect_deriv()

        request = {"ticks": symbol, "passthrough": {"command": "price_request"}}
        await deriv_ws.send(json.dumps(request))
        
        while True:
            response = await asyncio.wait_for(deriv_ws.recv(), timeout=10.0)
            data = json.loads(response)
            
            if data.get("msg_type") == "tick" and data.get("echo_req", {}).get("passthrough", {}).get("command") == "price_request":
                price = data["tick"]["quote"]
                await update.message.reply_text(f"💰 {symbol}: {price}")
                break
            elif "error" in data and data.get("echo_req", {}).get("ticks") == symbol:
                await update.message.reply_text(f"Error: {data['error']['message']}")
                break
            if data.get("msg_type") != "tick":
                 continue
            else:
                 await update.message.reply_text(f"Could not get price for {symbol}")
                 break
        
    except asyncio.TimeoutError:
        await update.message.reply_text("Request timed out. Please try again.")
    except Exception as e:
        logger.error(f"Error in price command: {e}")
        await update.message.reply_text(f"Error: {str(e)}")

# --- APPLICATION HOOKS (The Fix) ---

async def start_and_monitor(application: Application):
    """
    Called by Application.run_polling() post-initialization.
    Loads alerts, connects to Deriv, and starts the monitoring task.
    """
    global monitoring_task
    
    # 1. Load persisted alerts
    load_alerts()
    
    # 2. Connect to Deriv
    await connect_deriv()
    
    # 3. Start price monitoring in background
    monitoring_task = asyncio.create_task(monitor_prices(application))
    
    # 4. Re-subscribe to symbols that were loaded from the file
    for symbol in price_alerts.keys():
        await subscribe_to_symbol(symbol)
    
    logger.info("Bot started! Running polling loop...")

async def shutdown_async(application: Application):
    """
    Called by Application.run_polling() post-shutdown.
    Cleans up external async tasks and connections.
    """
    global deriv_ws, monitoring_task
    
    logger.info("Starting graceful shutdown of external resources...")
    
    # 1. Cancel monitoring task
    if monitoring_task and not monitoring_task.done():
        logger.info("Cancelling price monitoring task...")
        monitoring_task.cancel()
        try:
            await monitoring_task
        except asyncio.CancelledError:
            logger.info("Monitoring task cancelled successfully.")
            pass
    
    # 2. Close Deriv WebSocket connection
    if deriv_ws and not deriv_ws.closed:
        logger.info("Closing Deriv WebSocket connection...")
        await deriv_ws.close()
    
    logger.info("Shutdown of external resources complete.")

# --- SYNCHRONOUS ENTRY POINT ---

def main():
    """Synchronous entry point that delegates event loop management to run_polling."""
    
    try:
        application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        
        # Add command handlers
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("setalert", setalert_command))
        application.add_handler(CommandHandler("listalerts", listalerts_command))
        application.add_handler(CommandHandler("deletealert", deletealert_command)) # New Handler
        application.add_handler(CommandHandler("symbols", symbols_command))
        application.add_handler(CommandHandler("price", price_command))

        # Assign hooks to integrate external async logic safely
        application.post_init = start_and_monitor
        application.post_shutdown = shutdown_async

        # Start the application. This blocks until terminated.
        application.run_polling(drop_pending_updates=True)

    except Exception as e:
        logger.error(f"Error in main: {e}", exc_info=True)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error outside main: {e}")
