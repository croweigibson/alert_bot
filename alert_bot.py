import asyncio
import json
import os
import logging
import websockets
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from aiohttp import web # Used for the simple web server/health check

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- CONFIGURATION & GLOBALS ---

# Render sets the PORT env var (usually 10000). Read it dynamically.
PORT = int(os.getenv("PORT", "10000")) 
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "105850")
ALERT_FILE = "price_alerts.json"

# Global state variables
price_alerts = {}
deriv_ws = None
monitoring_task = None
health_server_runner = None # To hold the aiohttp server runner for cleanup

if not TELEGRAM_BOT_TOKEN:
    logger.critical("TELEGRAM_BOT_TOKEN is missing. Bot cannot be initialized.")


# --- HEALTH CHECK SERVER ---

async def health_check(request):
    """Simple health check endpoint for Render (Responds to / and /health)"""
    return web.Response(text="OK", status=200)

async def start_health_server():
    """Start a simple HTTP server for health checks, listening on $PORT."""
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Bind to 0.0.0.0 and the Render-specified PORT
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start() 
    logger.info(f"Health check server started successfully on port {PORT}")
    
    # Return the runner so we can clean it up later.
    return runner 


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
            logger.info(f"Loaded {sum(len(a) for a in price_alerts.values())} alerts.")
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
                # Re-subscribe to all symbols that have active alerts
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
        # **FIXED SYNTAX ERROR HERE**
        condition = context.args[1].lower() 
        target_price = float(context.args[2])
        
        if condition not in ["above", "below"]:
            await update.message.reply_text("Condition must be 'above' or 'below'")
            return
        
        if symbol not in price_alerts:
            price_alerts[symbol] = {}
            await subscribe_to_symbol(symbol)
        
        # Calculate next alert ID
        alert_ids = list(price_alerts[symbol].keys())
        alert_id = max(alert_ids) + 1 if alert_ids else 1
        
        price_alerts[symbol][alert_id] = {
            "target_price": target_price,
            "condition": condition,
            "chat_id": update.effective_chat.id
        }
        
        save_alerts()

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
        
        # Check ownership
        if price_alerts[symbol][alert_id]["chat_id"] != chat_id:
            await update.message.reply_text("You can only delete your own alerts.")
            return

        del price_alerts[symbol][alert_id]
        
        if not price_alerts[symbol]:
            del price_alerts[symbol]

        save_alerts()

        await update.message.reply_text(f"🗑️ Alert ID {alert_id} for {symbol} deleted successfully.")
        
    except ValueError:
        await update.message.reply_text("Invalid Alert ID or format. ID must be a number.")
    except Exception as e:
        logger.error(f"Error in deletealert: {e}")
        await update.message.reply_text(f"Error deleting alert: {str(e)}")

async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /price <symbol>\nExample: /price R_10")
        return
    
    symbol = context.args[0].upper()
    
    try:
        if deriv_ws is None or deriv_ws.closed:
            await connect_deriv()

        # Request the price
        request = {"ticks": symbol, "passthrough": {"command": "price_request"}}
        await deriv_ws.send(json.dumps(request))
        
        # NOTE: In a real-world bot, you'd handle this response async to avoid blocking
        # but for simplicity, we keep the original blocking logic with a timeout.
        await update.message.reply_text(f"Requesting current price for {symbol}...")

    except asyncio.TimeoutError:
        await update.message.reply_text("Request timed out. Please try again.")
    except Exception as e:
        logger.error(f"Error in price command: {e}")
        await update.message.reply_text(f"Error: {str(e)}")


# --- APPLICATION HOOKS (The Hybrid Fix) ---

async def start_and_monitor(application: Application):
    """
    Called by Application.run_polling() post-initialization.
    Starts the aiohttp server, loads alerts, and starts monitoring.
    """
    global monitoring_task, health_server_runner
    
    # 1. Start the web server to satisfy Render's PORT requirement
    health_server_runner = await start_health_server()
    
    # 2. Load persisted alerts
    load_alerts()
    
    # 3. Connect to Deriv
    await connect_deriv()
    
    # 4. Start price monitoring in background
    monitoring_task = asyncio.create_task(monitor_prices(application))
    
    # 5. Re-subscribe to symbols that were loaded from the file
    for symbol in price_alerts.keys():
        await subscribe_to_symbol(symbol)
    
    logger.info("Bot started! Running polling loop in background.")

async def shutdown_async(application: Application):
    """
    Called by Application.run_polling() post-shutdown.
    Cleans up all external async tasks and connections.
    """
    global deriv_ws, monitoring_task, health_server_runner
    
    logger.info("Starting graceful shutdown of external resources...")
    
    # 1. Cancel monitoring task
    if monitoring_task and not monitoring_task.done():
        logger.info("Cancelling price monitoring task...")
        monitoring_task.cancel()
        try:
            await monitoring_task
        except asyncio.CancelledError:
            pass
    
    # 2. Close Deriv WebSocket connection
    if deriv_ws and not deriv_ws.closed:
        logger.info("Closing Deriv WebSocket connection...")
        await deriv_ws.close()

    # 3. Cleanup the health check server
    if health_server_runner:
        logger.info("Shutting down health check server...")
        await health_server_runner.cleanup()
        
    logger.info("Shutdown of external resources complete.")


# --- SYNCHRONOUS ENTRY POINT ---

def main():
    """Synchronous entry point that delegates event loop management to run_polling."""
    
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("Cannot run without TELEGRAM_BOT_TOKEN. Exiting.")
        return

    try:
        application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        
        # Add command handlers
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("setalert", setalert_command))
        application.add_handler(CommandHandler("listalerts", listalerts_command))
        application.add_handler(CommandHandler("deletealert", deletealert_command))
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
