import asyncio
import json
import os
import logging
import websockets
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from aiohttp import web

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
PORT = int(os.getenv("PORT", "10000"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TOKEN_HERE")
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "105850")
ALERT_FILE = "price_alerts.json"

# Global state
price_alerts = {}
deriv_ws = None
monitoring_task = None
health_server_runner = None

if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "YOUR_TOKEN_HERE":
    logger.critical("TELEGRAM_BOT_TOKEN is missing!")
    exit(1)

# --- PERSISTENCE ---

def load_alerts():
    """Load alerts from file"""
    global price_alerts
    if os.path.exists(ALERT_FILE):
        try:
            with open(ALERT_FILE, 'r') as f:
                data = json.load(f)
                price_alerts = {
                    symbol: {int(k): v for k, v in alerts.items()}
                    for symbol, alerts in data.items()
                }
            logger.info(f"Loaded {sum(len(a) for a in price_alerts.values())} alerts")
        except Exception as e:
            logger.error(f"Error loading alerts: {e}")
            price_alerts = {}

def save_alerts():
    """Save alerts to file"""
    try:
        with open(ALERT_FILE, 'w') as f:
            json.dump(price_alerts, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving alerts: {e}")

# --- HEALTH CHECK SERVER ---

async def health_check(request):
    """Health endpoint"""
    return web.Response(text="Bot is running", status=200)

async def start_health_server():
    """Start health check server"""
    global health_server_runner
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    
    health_server_runner = web.AppRunner(app)
    await health_server_runner.setup()
    site = web.TCPSite(health_server_runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Health server started on port {PORT}")

# --- DERIV CONNECTION ---

async def connect_deriv():
    """Connect to Deriv WebSocket"""
    global deriv_ws
    uri = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
    deriv_ws = await websockets.connect(uri)
    logger.info("Connected to Deriv WebSocket")
    return deriv_ws

async def subscribe_to_symbol(symbol):
    """Subscribe to price updates"""
    try:
        subscribe_request = {"ticks": symbol, "subscribe": 1}
        await deriv_ws.send(json.dumps(subscribe_request))
        logger.info(f"Subscribed to {symbol}")
    except Exception as e:
        logger.error(f"Error subscribing to {symbol}: {e}")

# --- PRICE MONITORING ---

async def monitor_prices(application):
    """Monitor prices and send alerts"""
    global deriv_ws
    
    while True:
        try:
            if deriv_ws is None or deriv_ws.closed:
                await connect_deriv()
                # Resubscribe to all symbols with alerts
                for symbol in price_alerts.keys():
                    await subscribe_to_symbol(symbol)
            
            response = await asyncio.wait_for(deriv_ws.recv(), timeout=30.0)
            data = json.loads(response)
            
            # Handle ping
            if data.get('msg_type') == 'ping':
                await deriv_ws.send(json.dumps({'pong': 1}))
                continue
            
            # Process price tick
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
                                await application.bot.send_message(chat_id=chat_id, text=message)
                                alerts_to_remove.append(alert_id)
                            except Exception as e:
                                logger.error(f"Error sending alert: {e}")
                    
                    # Remove triggered alerts
                    for alert_id in alerts_to_remove:
                        del price_alerts[symbol][alert_id]
                    
                    # Clean up empty symbol
                    if not price_alerts[symbol]:
                        del price_alerts[symbol]
                    
                    # Save if alerts were triggered
                    if alerts_to_remove:
                        save_alerts()
        
        except asyncio.TimeoutError:
            logger.warning("WebSocket timeout, reconnecting...")
            deriv_ws = None
        except asyncio.CancelledError:
            logger.info("Monitoring cancelled")
            break
        except Exception as e:
            logger.error(f"Error in monitoring: {e}")
            await asyncio.sleep(5)
            deriv_ws = None

# --- TELEGRAM COMMANDS ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start"""
    message = """
Welcome to Deriv Price Alert Bot! 🤖

Commands:
/setalert <symbol> <above/below> <price> - Set alert
/listalerts - List your alerts
/deletealert <symbol> <id> - Delete alert
/symbols - Available symbols
/price <symbol> - Current price

Example: /setalert R_10 above 500.50
"""
    await update.message.reply_text(message)

async def symbols_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show symbols"""
    message = """
📊 Available Symbols:

Volatility: R_10, R_25, R_50, R_75, R_100
Crash/Boom: BOOM500, BOOM1000, CRASH500, CRASH1000
Step: stpRNG
Range Break: RDBEAR, RDBULL
"""
    await update.message.reply_text(message)

async def setalert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set price alert"""
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
        
        # Create alert
        if symbol not in price_alerts:
            price_alerts[symbol] = {}
            await subscribe_to_symbol(symbol)
        
        # Get next alert ID
        alert_id = max(price_alerts[symbol].keys(), default=0) + 1
        
        price_alerts[symbol][alert_id] = {
            "target_price": target_price,
            "condition": condition,
            "chat_id": update.effective_chat.id
        }
        
        save_alerts()
        
        await update.message.reply_text(
            f"✅ Alert set!\n"
            f"Symbol: {symbol}\n"
            f"Condition: {condition} {target_price}\n"
            f"Alert ID: {alert_id}"
        )
    
    except ValueError:
        await update.message.reply_text("Invalid price. Use a number.")
    except Exception as e:
        logger.error(f"Error in setalert: {e}")
        await update.message.reply_text(f"Error: {str(e)}")

async def listalerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List alerts"""
    chat_id = update.effective_chat.id
    user_alerts = []
    
    for symbol, alerts in price_alerts.items():
        for alert_id, alert in alerts.items():
            if alert["chat_id"] == chat_id:
                user_alerts.append(
                    f"• {symbol} (ID: {alert_id}): {alert['condition']} {alert['target_price']}"
                )
    
    if user_alerts:
        message = "📋 Your Active Alerts:\n\n" + "\n".join(user_alerts)
    else:
        message = "You have no active alerts."
    
    await update.message.reply_text(message)

async def deletealert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete alert"""
    try:
        if len(context.args) != 2:
            await update.message.reply_text(
                "Usage: /deletealert <symbol> <alert_id>\n"
                "Example: /deletealert R_10 1"
            )
            return
        
        symbol = context.args[0].upper()
        alert_id = int(context.args[1])
        chat_id = update.effective_chat.id
        
        # Check if alert exists
        if symbol not in price_alerts or alert_id not in price_alerts[symbol]:
            await update.message.reply_text(f"Alert ID {alert_id} for {symbol} not found.")
            return
        
        # Check ownership
        if price_alerts[symbol][alert_id]["chat_id"] != chat_id:
            await update.message.reply_text("You can only delete your own alerts.")
            return
        
        # Delete alert
        del price_alerts[symbol][alert_id]
        
        # Clean up empty symbol
        if not price_alerts[symbol]:
            del price_alerts[symbol]
        
        save_alerts()
        
        await update.message.reply_text(f"✅ Alert {alert_id} for {symbol} deleted!")
    
    except ValueError:
        await update.message.reply_text("Invalid alert ID. Must be a number.")
    except Exception as e:
        logger.error(f"Error in deletealert: {e}")
        await update.message.reply_text(f"Error: {str(e)}")

async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get current price"""
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /price <symbol>\nExample: /price R_10")
        return
    
    symbol = context.args[0].upper()
    
    try:
        if deriv_ws is None or deriv_ws.closed:
            await connect_deriv()
        
        request = {"ticks": symbol}
        await deriv_ws.send(json.dumps(request))
        
        response = await asyncio.wait_for(deriv_ws.recv(), timeout=10.0)
        data = json.loads(response)
        
        if "tick" in data:
            price = data["tick"]["quote"]
            await update.message.reply_text(f"💰 {symbol}: {price}")
        elif "error" in data:
            await update.message.reply_text(f"Error: {data['error']['message']}")
        else:
            await update.message.reply_text(f"Could not get price for {symbol}")
    
    except asyncio.TimeoutError:
        await update.message.reply_text("Request timed out.")
    except Exception as e:
        logger.error(f"Error in price: {e}")
        await update.message.reply_text(f"Error: {str(e)}")

# --- SHUTDOWN ---

async def shutdown():
    """Cleanup"""
    global deriv_ws, monitoring_task, health_server_runner
    
    logger.info("Shutting down...")
    
    # Cancel monitoring
    if monitoring_task and not monitoring_task.done():
        monitoring_task.cancel()
        try:
            await monitoring_task
        except asyncio.CancelledError:
            pass
    
    # Save alerts
    save_alerts()
    
    # Close WebSocket
    if deriv_ws and not deriv_ws.closed:
        await deriv_ws.close()
    
    # Close health server
    if health_server_runner:
        await health_server_runner.cleanup()
    
    logger.info("Shutdown complete")

# --- MAIN ---

async def main():
    """Main function"""
    global monitoring_task
    
    try:
        # Load saved alerts
        load_alerts()
        
        # Start health server
        await start_health_server()
        
        # Connect to Deriv
        await connect_deriv()
        
        # Resubscribe to saved alerts
        for symbol in price_alerts.keys():
            await subscribe_to_symbol(symbol)
        
        # Create bot
        application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        
        # Add commands
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("setalert", setalert_command))
        application.add_handler(CommandHandler("listalerts", listalerts_command))
        application.add_handler(CommandHandler("deletealert", deletealert_command))
        application.add_handler(CommandHandler("symbols", symbols_command))
        application.add_handler(CommandHandler("price", price_command))
        
        # Start monitoring
        monitoring_task = asyncio.create_task(monitor_prices(application))
        
        logger.info("Bot started!")
        
        # Run bot
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        
        # Keep running
        await asyncio.Event().wait()
    
    except (KeyboardInterrupt, SystemExit):
        logger.info("Received exit signal")
    except Exception as e:
        logger.error(f"Error: {e}")
        raise
    finally:
        if 'application' in locals():
            await shutdown()
            await application.updater.stop()
            await application.stop()
            await application.shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped")
