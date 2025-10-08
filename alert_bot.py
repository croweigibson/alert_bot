import asyncio
import json
import websockets
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
TELEGRAM_BOT_TOKEN = "8455948992:AAGbO8Hkw8OOgAMrhWhuc6JjVqI9QjOUQ0g"
TELEGRAM_CHAT_ID = "824922767"
DERIV_APP_ID = "105850"

# Price alerts storage
price_alerts = {}

# Deriv WebSocket connection
deriv_ws = None

async def connect_deriv():
    """Connect to Deriv WebSocket API"""
    global deriv_ws
    uri = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
    deriv_ws = await websockets.connect(uri)
    logger.info("Connected to Deriv WebSocket")
    return deriv_ws

async def subscribe_to_symbol(symbol):
    """Subscribe to price updates for a symbol"""
    subscribe_request = {
        "ticks": symbol,
        "subscribe": 1
    }
    await deriv_ws.send(json.dumps(subscribe_request))
    logger.info(f"Subscribed to {symbol}")

async def monitor_prices(application):
    """Monitor prices and send alerts"""
    global deriv_ws
    
    while True:
        try:
            if deriv_ws is None:
                await connect_deriv()
            
            response = await deriv_ws.recv()
            data = json.loads(response)
            
            if "tick" in data:
                symbol = data["tick"]["symbol"]
                current_price = float(data["tick"]["quote"])
                
                # Check alerts for this symbol
                if symbol in price_alerts:
                    alerts_to_remove = []
                    
                    for alert_id, alert in price_alerts[symbol].items():
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
                            await application.bot.send_message(
                                chat_id=chat_id,
                                text=message
                            )
                            alerts_to_remove.append(alert_id)
                    
                    # Remove triggered alerts
                    for alert_id in alerts_to_remove:
                        del price_alerts[symbol][alert_id]
                        
        except Exception as e:
            logger.error(f"Error monitoring prices: {e}")
            await asyncio.sleep(5)
            deriv_ws = None

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    welcome_message = """
Welcome to Deriv Price Alert Bot! 🤖

Available commands:
/setalert <symbol> <above/below> <price> - Set a price alert
/listalerts - List your active alerts
/symbols - Show available symbols
/price <symbol> - Get current price

Example:
/setalert R_10 above 500.50
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
        
        # Create alert
        if symbol not in price_alerts:
            price_alerts[symbol] = {}
            # Subscribe to this symbol
            await subscribe_to_symbol(symbol)
        
        alert_id = len(price_alerts[symbol]) + 1
        price_alerts[symbol][alert_id] = {
            "target_price": target_price,
            "condition": condition,
            "chat_id": update.effective_chat.id
        }
        
        await update.message.reply_text(
            f"✅ Alert set!\n"
            f"Symbol: {symbol}\n"
            f"Condition: {condition} {target_price}\n"
            f"Alert ID: {alert_id}"
        )
        
    except ValueError:
        await update.message.reply_text("Invalid price value. Please use a number.")
    except Exception as e:
        await update.message.reply_text(f"Error setting alert: {str(e)}")

async def listalerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all active alerts"""
    chat_id = update.effective_chat.id
    user_alerts = []
    
    for symbol, alerts in price_alerts.items():
        for alert_id, alert in alerts.items():
            if alert["chat_id"] == chat_id:
                user_alerts.append(
                    f"• {symbol}: {alert['condition']} {alert['target_price']}"
                )
    
    if user_alerts:
        message = "📋 Your Active Alerts:\n\n" + "\n".join(user_alerts)
    else:
        message = "You have no active alerts."
    
    await update.message.reply_text(message)

async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get current price for a symbol"""
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /price <symbol>\nExample: /price R_10")
        return
    
    symbol = context.args[0].upper()
    
    try:
        # Send request for current price
        request = {"ticks": symbol}
        await deriv_ws.send(json.dumps(request))
        
        # Wait for response
        response = await deriv_ws.recv()
        data = json.loads(response)
        
        if "tick" in data:
            price = data["tick"]["quote"]
            await update.message.reply_text(f"💰 {symbol}: {price}")
        else:
            await update.message.reply_text(f"Could not get price for {symbol}")
            
    except Exception as e:
        await update.message.reply_text(f"Error: {str(e)}")

def main():
    """Start the bot"""
    # Create a new event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        # Connect to Deriv
        loop.run_until_complete(connect_deriv())
        
        # Create Telegram bot application
        application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        
        # Add command handlers
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("setalert", setalert_command))
        application.add_handler(CommandHandler("listalerts", listalerts_command))
        application.add_handler(CommandHandler("symbols", symbols_command))
        application.add_handler(CommandHandler("price", price_command))
        
        # Start price monitoring in background
        loop.create_task(monitor_prices(application))
        
        # Start the bot
        logger.info("Bot started!")
        application.run_polling()
        
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    finally:
        loop.close()

if __name__ == "__main__":
    main()