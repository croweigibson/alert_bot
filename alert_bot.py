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


# --- TELEGRAM COMMAND HANDLERS (Omitted for brevity, assuming existing correct logic) ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_message = "Welcome to Deriv Price Alert Bot! Use /setalert, /listalerts, and /deletealert."
    await update.message.reply_text(welcome_message)

async def symbols_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbols_list = "📊 Available Symbols: R_10, R_25, BOOM500, CRASH1000, etc."
    await update.message.reply_text(symbols_list)

async def setalert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This logic relies on connect_deriv and save_alerts.
    try:
        if len(context.args) != 3:
            await update.message.reply_text("Usage: /setalert <symbol> <above/below> <price>")
            return
        
        symbol = context.args[0].upper()
        condition = context.args[1].
