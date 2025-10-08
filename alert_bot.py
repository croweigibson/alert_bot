import asyncio
import json
import os
import logging
from typing import Dict, Set, Optional
from collections import defaultdict
from datetime import datetime
import websockets
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from aiohttp import web

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- CONSTANTS ---
PORT = int(os.getenv("PORT", "10000"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "105850")
ALERT_FILE = "price_alerts.json"

# Performance tuning constants
WS_TIMEOUT = 30.0
RECONNECT_DELAY = 5.0
SAVE_DEBOUNCE_SECONDS = 10.0
MAX_RECONNECT_ATTEMPTS = 5
BATCH_NOTIFICATION_DELAY = 0.5

if not TELEGRAM_BOT_TOKEN:
    logger.critical("TELEGRAM_BOT_TOKEN is missing. Bot cannot be initialized.")


# --- OPTIMIZED STATE MANAGEMENT ---
class AlertManager:
    """Centralized alert management with optimized data structures"""
    
    def __init__(self):
        # Primary storage: symbol -> alert_id -> alert_data
        self.alerts: Dict[str, Dict[int, dict]] = {}
        # Index for fast user lookups: chat_id -> [(symbol, alert_id), ...]
        self.user_index: Dict[int, list] = defaultdict(list)
        # Track which symbols are currently subscribed
        self.active_subscriptions: Set[str] = set()
        # Debounce saving
        self.save_pending = False
        self.last_save_time = 0
        self.save_task: Optional[asyncio.Task] = None
    
    def add_alert(self, symbol: str, alert_id: int, alert_data: dict):
        """Add alert with automatic indexing"""
        if symbol not in self.alerts:
            self.alerts[symbol] = {}
        
        self.alerts[symbol][alert_id] = alert_data
        self.user_index[alert_data["chat_id"]].append((symbol, alert_id))
        self._schedule_save()
    
    def remove_alert(self, symbol: str, alert_id: int) -> bool:
        """Remove alert and update indexes"""
        if symbol not in self.alerts or alert_id not in self.alerts[symbol]:
            return False
        
        chat_id = self.alerts[symbol][alert_id]["chat_id"]
        del self.alerts[symbol][alert_id]
        
        # Update user index
        self.user_index[chat_id] = [
            (s, aid) for s, aid in self.user_index[chat_id]
            if not (s == symbol and aid == alert_id)
        ]
        
        # Clean up empty structures
        if not self.alerts[symbol]:
            del self.alerts[symbol]
        if not self.user_index[chat_id]:
            del self.user_index[chat_id]
        
        self._schedule_save()
        return True
    
    def get_user_alerts(self, chat_id: int) -> list:
        """Get all alerts for a user (O(1) lookup)"""
        return [
            (symbol, alert_id, self.alerts[symbol][alert_id])
            for symbol, alert_id in self.user_index.get(chat_id, [])
            if symbol in self.alerts and alert_id in self.alerts[symbol]
        ]
    
    def get_symbols_with_alerts(self) -> Set[str]:
        """Get all symbols that have active alerts"""
        return set(self.alerts.keys())
    
    def get_next_alert_id(self, symbol: str) -> int:
        """Get next available alert ID for a symbol"""
        if symbol not in self.alerts or not self.alerts[symbol]:
            return 1
        return max(self.alerts[symbol].keys()) + 1
    
    def _schedule_save(self):
        """Debounced save to prevent excessive disk writes"""
        if self.save_task and not self.save_task.done():
            return
        
        self.save_task = asyncio.create_task(self._debounced_save())
    
    async def _debounced_save(self):
        """Wait before saving to batch multiple changes"""
        await asyncio.sleep(SAVE_DEBOUNCE_SECONDS)
        await self.save_to_disk()
    
    async def save_to_disk(self):
        """Async file write"""
        try:
            # Use asyncio to avoid blocking
            data = json.dumps(self.alerts, indent=4)
            await asyncio.to_thread(self._write_file, data)
            logger.info(f"Saved {sum(len(a) for a in self.alerts.values())} alerts to disk")
        except Exception as e:
            logger.error(f"Error saving alerts: {e}")
    
    def _write_file(self, data: str):
        """Blocking file write (run in thread)"""
        with open(ALERT_FILE, 'w') as f:
            f.write(data)
    
    def load_from_disk(self):
        """Load alerts with automatic indexing"""
        if not os.path.exists(ALERT_FILE):
            return
        
        try:
            with open(ALERT_FILE, 'r') as f:
                data = json.load(f)
            
            # Rebuild data structures
            self.alerts = {
                symbol: {int(k): v for k, v in alerts.items()}
                for symbol, alerts in data.items()
            }
            
            # Rebuild user index
            self.user_index.clear()
            for symbol, alerts in self.alerts.items():
                for alert_id, alert_data in alerts.items():
                    self.user_index[alert_data["chat_id"]].append((symbol, alert_id))
            
            logger.info(f"Loaded {sum(len(a) for a in self.alerts.values())} alerts")
        except Exception as e:
            logger.error(f"Error loading alerts: {e}")


# --- OPTIMIZED WEBSOCKET MANAGER ---
class DerivWebSocketManager:
    """Manages WebSocket connection with reconnection logic and subscription tracking"""
    
    def __init__(self, app_id: str):
        self.app_id = app_id
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.uri = f"wss://ws.derivws.com/websockets/v3?app_id={app_id}"
        self.subscribed_symbols: Set[str] = set()
        self.reconnect_attempts = 0
        self.connection_lock = asyncio.Lock()
    
    async def connect(self) -> bool:
        """Connect with exponential backoff"""
        async with self.connection_lock:
            if self.ws and not self.ws.closed:
                return True
            
            for attempt in range(MAX_RECONNECT_ATTEMPTS):
                try:
                    self.ws = await websockets.connect(self.uri)
                    self.reconnect_attempts = 0
                    logger.info("Connected to Deriv WebSocket")
                    return True
                except Exception as e:
                    delay = min(RECONNECT_DELAY * (2 ** attempt), 60)
                    logger.warning(f"Connection attempt {attempt + 1} failed: {e}. Retrying in {delay}s")
                    await asyncio.sleep(delay)
            
            logger.error("Max reconnection attempts reached")
            return False
    
    async def subscribe(self, symbol: str):
        """Subscribe to a symbol"""
        if not await self.ensure_connected():
            return False
        
        if symbol in self.subscribed_symbols:
            return True
        
        try:
            await self.ws.send(json.dumps({"ticks": symbol, "subscribe": 1}))
            self.subscribed_symbols.add(symbol)
            logger.info(f"Subscribed to {symbol}")
            return True
        except Exception as e:
            logger.error(f"Error subscribing to {symbol}: {e}")
            return False
    
    async def unsubscribe(self, symbol: str):
        """Unsubscribe from a symbol"""
        if symbol not in self.subscribed_symbols:
            return
        
        try:
            if self.ws and not self.ws.closed:
                await self.ws.send(json.dumps({"forget_all": "ticks"}))
            self.subscribed_symbols.discard(symbol)
            logger.info(f"Unsubscribed from {symbol}")
        except Exception as e:
            logger.error(f"Error unsubscribing from {symbol}: {e}")
    
    async def ensure_connected(self) -> bool:
        """Ensure connection is active"""
        if not self.ws or self.ws.closed:
            return await self.connect()
        return True
    
    async def receive(self, timeout: float = WS_TIMEOUT) -> Optional[dict]:
        """Receive message with timeout"""
        try:
            if not await self.ensure_connected():
                return None
            
            message = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
            return json.loads(message)
        except asyncio.TimeoutError:
            logger.debug("WebSocket receive timeout")
            return None
        except websockets.exceptions.ConnectionClosed:
            logger.info("WebSocket connection closed")
            self.ws = None
            return None
        except Exception as e:
            logger.error(f"Error receiving message: {e}")
            return None
    
    async def send(self, data: dict):
        """Send message"""
        if not await self.ensure_connected():
            return False
        
        try:
            await self.ws.send(json.dumps(data))
            return True
        except Exception as e:
            logger.error(f"Error sending message: {e}")
            return False
    
    async def resubscribe_all(self, symbols: Set[str]):
        """Resubscribe to all symbols after reconnection"""
        self.subscribed_symbols.clear()
        for symbol in symbols:
            await self.subscribe(symbol)
    
    async def close(self):
        """Close connection"""
        if self.ws and not self.ws.closed:
            try:
                await self.ws.close()
            except Exception as e:
                logger.warning(f"Error closing WebSocket: {e}")
        self.ws = None
        self.subscribed_symbols.clear()


# --- GLOBAL INSTANCES ---
alert_manager = AlertManager()
ws_manager = DerivWebSocketManager(DERIV_APP_ID)
notification_queue = asyncio.Queue()


# --- HEALTH CHECK SERVER ---
async def health_check(request):
    """Health check endpoint"""
    return web.Response(text="OK", status=200)

async def start_health_server():
    """Start HTTP health check server"""
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    
    logger.info(f"Health check server started on port {PORT}")
    return runner


# --- NOTIFICATION QUEUE PROCESSOR ---
async def process_notifications(application: Application):
    """Batch process notifications to avoid rate limits"""
    while True:
        try:
            notifications = []
            
            # Collect notifications for batching
            try:
                notification = await asyncio.wait_for(
                    notification_queue.get(), 
                    timeout=BATCH_NOTIFICATION_DELAY
                )
                notifications.append(notification)
                
                # Collect more if available
                while not notification_queue.empty() and len(notifications) < 10:
                    notifications.append(notification_queue.get_nowait())
            except asyncio.TimeoutError:
                continue
            
            # Send notifications
            for chat_id, message in notifications:
                try:
                    await application.bot.send_message(chat_id=chat_id, text=message)
                    await asyncio.sleep(0.1)  # Rate limiting
                except Exception as e:
                    logger.error(f"Error sending notification to {chat_id}: {e}")
        
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in notification processor: {e}")


# --- PRICE MONITORING ---
async def monitor_prices(application: Application):
    """Optimized price monitoring with subscription management"""
    while True:
        try:
            # Sync subscriptions with active alerts
            required_symbols = alert_manager.get_symbols_with_alerts()
            current_symbols = ws_manager.subscribed_symbols.copy()
            
            # Subscribe to new symbols
            for symbol in required_symbols - current_symbols:
                await ws_manager.subscribe(symbol)
            
            # Unsubscribe from unused symbols
            for symbol in current_symbols - required_symbols:
                await ws_manager.unsubscribe(symbol)
            
            # Receive price data
            data = await ws_manager.receive()
            
            if not data:
                continue
            
            # Handle ping
            if data.get('msg_type') == 'ping':
                await ws_manager.send({'pong': 1})
                continue
            
            # Process tick data
            if "tick" in data:
                symbol = data["tick"]["symbol"]
                current_price = float(data["tick"]["quote"])
                
                if symbol not in alert_manager.alerts:
                    continue
                
                triggered_alerts = []
                
                # Check all alerts for this symbol
                for alert_id, alert in list(alert_manager.alerts[symbol].items()):
                    target_price = alert["target_price"]
                    condition = alert["condition"]
                    
                    if (condition == "above" and current_price >= target_price) or \
                       (condition == "below" and current_price <= target_price):
                        
                        message = f"🚨 ALERT: {symbol} is now {current_price:.2f} ({condition} {target_price:.2f})"
                        await notification_queue.put((alert["chat_id"], message))
                        triggered_alerts.append(alert_id)
                
                # Remove triggered alerts
                for alert_id in triggered_alerts:
                    alert_manager.remove_alert(symbol, alert_id)
        
        except asyncio.CancelledError:
            logger.info("Price monitoring cancelled")
            break
        except Exception as e:
            logger.error(f"Error in price monitoring: {e}")
            await asyncio.sleep(RECONNECT_DELAY)


# --- TELEGRAM COMMANDS ---
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
"""
    await update.message.reply_text(welcome_message)

async def symbols_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show available symbols"""
    symbols_list = """📊 Available Deriv Synthetic Indices:
• R_10 - Volatility 10 Index
• R_25 - Volatility 25 Index
• R_50 - Volatility 50 Index
• R_75 - Volatility 75 Index
• R_100 - Volatility 100 Index
• BOOM500 - Boom 500 Index
• BOOM1000 - Boom 1000 Index
• CRASH500 - Crash 500 Index
• CRASH1000 - Crash 1000 Index"""
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
        
        alert_id = alert_manager.get_next_alert_id(symbol)
        alert_data = {
            "target_price": target_price,
            "condition": condition,
            "chat_id": update.effective_chat.id,
            "created_at": datetime.now().isoformat()
        }
        
        alert_manager.add_alert(symbol, alert_id, alert_data)
        await ws_manager.subscribe(symbol)
        
        await update.message.reply_text(
            f"✅ Alert set!\nSymbol: {symbol}\nCondition: {condition} {target_price:.2f}\nAlert ID: {alert_id}"
        )
    
    except ValueError:
        await update.message.reply_text("Invalid price value. Please use a number.")
    except Exception as e:
        logger.error(f"Error in setalert: {e}")
        await update.message.reply_text(f"Error setting alert: {str(e)}")

async def listalerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all active alerts (optimized O(1) lookup)"""
    chat_id = update.effective_chat.id
    user_alerts = alert_manager.get_user_alerts(chat_id)
    
    if not user_alerts:
        await update.message.reply_text("You have no active alerts.")
        return
    
    alert_lines = [
        f"• {symbol}: {alert['condition']} {alert['target_price']:.2f} (ID: {alert_id})"
        for symbol, alert_id, alert in user_alerts
    ]
    
    message = "📋 Your Active Alerts:\n\n" + "\n".join(alert_lines)
    await update.message.reply_text(message)

async def deletealert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /deletealert command"""
    try:
        if len(context.args) != 2:
            await update.message.reply_text("Usage: /deletealert <symbol> <alert_id>")
            return
        
        symbol = context.args[0].upper()
        alert_id = int(context.args[1])
        chat_id = update.effective_chat.id
        
        # Verify ownership
        if symbol not in alert_manager.alerts or alert_id not in alert_manager.alerts[symbol]:
            await update.message.reply_text(f"Alert ID {alert_id} for {symbol} not found.")
            return
        
        if alert_manager.alerts[symbol][alert_id]["chat_id"] != chat_id:
            await update.message.reply_text("You can only delete your own alerts.")
            return
        
        alert_manager.remove_alert(symbol, alert_id)
        await update.message.reply_text(f"🗑️ Alert ID {alert_id} for {symbol} deleted successfully.")
    
    except ValueError:
        await update.message.reply_text("Invalid Alert ID. ID must be a number.")
    except Exception as e:
        logger.error(f"Error in deletealert: {e}")
        await update.message.reply_text(f"Error deleting alert: {str(e)}")

async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get current price for a symbol"""
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /price <symbol>\nExample: /price R_10")
        return
    
    symbol = context.args[0].upper()
    
    try:
        await ws_manager.send({"ticks": symbol, "passthrough": {"command": "price_request"}})
        await update.message.reply_text(f"Requesting current price for {symbol}...")
    except Exception as e:
        logger.error(f"Error in price command: {e}")
        await update.message.reply_text(f"Error requesting price: {str(e)}")


# --- APPLICATION LIFECYCLE ---
monitoring_task = None
notification_task = None
health_server_runner = None

async def start_and_monitor(application: Application):
    """Initialize all services"""
    global monitoring_task, notification_task, health_server_runner
    
    # Start health server
    health_server_runner = await start_health_server()
    
    # Load alerts
    alert_manager.load_from_disk()
    
    # Connect to Deriv
    await ws_manager.connect()
    
    # Resubscribe to loaded symbols
    await ws_manager.resubscribe_all(alert_manager.get_symbols_with_alerts())
    
    # Start background tasks
    monitoring_task = asyncio.create_task(monitor_prices(application))
    notification_task = asyncio.create_task(process_notifications(application))
    
    logger.info("Bot started successfully!")

async def shutdown_async(application: Application):
    """Cleanup all resources"""
    global monitoring_task, notification_task, health_server_runner
    
    logger.info("Starting graceful shutdown...")
    
    # Cancel tasks
    for task in [monitoring_task, notification_task]:
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    
    # Save alerts
    await alert_manager.save_to_disk()
    
    # Close connections
    await ws_manager.close()
    
    # Cleanup health server
    if health_server_runner:
        await health_server_runner.cleanup()
    
    logger.info("Shutdown complete")


# --- MAIN ENTRY POINT ---
def main():
    """Main entry point"""
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("Cannot run without TELEGRAM_BOT_TOKEN. Exiting.")
        return
    
    try:
        application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        
        # Register commands
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("setalert", setalert_command))
        application.add_handler(CommandHandler("listalerts", listalerts_command))
        application.add_handler(CommandHandler("deletealert", deletealert_command))
        application.add_handler(CommandHandler("symbols", symbols_command))
        application.add_handler(CommandHandler("price", price_command))
        
        # Set lifecycle hooks
        application.post_init = start_and_monitor
        application.post_shutdown = shutdown_async
        
        # Start polling
        application.run_polling(drop_pending_updates=True)
    
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
