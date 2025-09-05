from dotenv import load_dotenv
import os
import logging
import tempfile
import pickle
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from datetime import datetime
from telethon import TelegramClient
from telethon.tl.types import Document

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Load .env file
load_dotenv()

# Telegram Bot
TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")
CREATOR_USERNAME = os.getenv("CREATOR_USERNAME")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID"))

# Telethon
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")

# Google Drive
CREDENTIALS_FILE = os.getenv("CREDENTIALS_FILE")

# Google Drive API scope
SCOPES = ['https://www.googleapis.com/auth/drive']

# Store user subscription status
user_subscription_status = {}

# Global variable to store the folder ID
TELEGRAM_BOT_FOLDER_ID = None

# File size limits (in bytes)
MAX_FILE_SIZE = 5 * 1024 * 1024 * 1024  # 5GB (Google Drive limit for free accounts)

# Initialize Telethon client
telethon_client = None

def shorten_url(long_url):
    """
    Shorten a URL using TinyURL API (no limits)
    """
    try:
        response = requests.get(f"http://tinyurl.com/api-create.php?url={long_url}", timeout=30)
        if response.status_code == 200:
            return response.text.strip()
        else:
            logger.error(f"TinyURL API error: {response.status_code}")
            return long_url
    except Exception as e:
        logger.error(f"Error shortening URL with TinyURL: {e}")
        return long_url

async def send_download_notification(context, user, file_name, file_size, short_url):
    """
    Send a notification to admin ONLY when a user gets a download link
    """
    try:
        # Format the notification message
        notification = (
            "📊 Download Link Generated\n\n"
            f"👤 User: {user.first_name} {user.last_name or ''} (@{user.username or 'N/A'})\n"
            f"🆔 User ID: {user.id}\n"
            f"📁 File: {file_name}\n"
            f"📦 Size: {file_size} bytes\n"
            f"🔗 Short URL: {short_url}\n"
            f"⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        
        # Send the notification to admin
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=notification)
        logger.info(f"Download notification sent to admin {ADMIN_CHAT_ID}")
        
    except Exception as e:
        logger.error(f"Failed to send download notification: {e}")

async def check_subscription(user_id, context):
    """Check if a user is subscribed to the channel."""
    try:
        # Get the chat member status for the user in the channel
        chat_member = await context.bot.get_chat_member(chat_id=f"@{CHANNEL_USERNAME}", user_id=user_id)
        
        # Check if the user is a member, administrator, or creator of the channel
        if chat_member.status in ['member', 'administrator', 'creator']:
            return True
        else:
            return False
    except Exception as e:
        logger.error(f"Error checking subscription: {e}")
        return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command /start is issued."""
    user = update.effective_user
    
    # Create subscription keyboard
    keyboard = [
        [InlineKeyboardButton("Subscribe to Channel", url=f"https://t.me/{CHANNEL_USERNAME}")],
        [InlineKeyboardButton("I've Subscribed", callback_data="check_subscription")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Send message with subscription prompt
    await update.message.reply_html(
        f"Hi {user.mention_html()}!\n\n"
        f"File2Link is used to convert files into high-speed download links.\n"
        f"This bot is created by {CREATOR_USERNAME}.\n\n"
        f"To use this bot, please subscribe to our channel @{CHANNEL_USERNAME} first.\n\n"
        "After subscribing, click the 'I've Subscribed' button below.",
        reply_markup=reply_markup
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command /help is issued."""
    user_id = update.effective_user.id
    
    # Check if user has subscribed
    if user_id not in user_subscription_status:
        # Verify subscription status
        is_subscribed = await check_subscription(user_id, context)
        
        if is_subscribed:
            user_subscription_status[user_id] = True
        else:
            await update.message.reply_text("Please use /start first and subscribe to our channel.")
            return
    
    await update.message.reply_text(
        "Just send me any file (document, image, video, audio) and I'll upload it to Google Drive "
        "and generate a short download link for you! Files are stored permanently on Google Drive.\n\n"
        f"This bot is created by {CREATOR_USERNAME}."
    )

async def handle_subscription_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the subscription callback."""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    
    if query.data == "check_subscription":
        # Actually verify subscription
        is_subscribed = await check_subscription(user_id, context)
        
        if is_subscribed:
            user_subscription_status[user_id] = True
            await query.edit_message_text(
                "Thank you for subscribing! ✅\n\n"
                "Now you can send me any file and I'll upload it to Google Drive "
                "and generate a short download link for you! Files are stored permanently on Google Drive."
            )
        else:
            # Edit the message to show subscription is still required
            keyboard = [
                [InlineKeyboardButton("Subscribe to Channel", url=f"https://t.me/{CHANNEL_USERNAME}")],
                [InlineKeyboardButton("I've Subscribed", callback_data="check_subscription")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                "❌ I couldn't verify your subscription. Please make sure you've subscribed to the channel and try again.",
                reply_markup=reply_markup
            )

def get_or_create_folder(drive_service, folder_name="Telegram Bot"):
    """
    Check if a folder exists in Google Drive, and create it if it doesn't.
    Returns the folder ID.
    """
    global TELEGRAM_BOT_FOLDER_ID
    
    # If we already have the folder ID, return it
    if TELEGRAM_BOT_FOLDER_ID:
        return TELEGRAM_BOT_FOLDER_ID
    
    # Check if the folder already exists
    query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    results = drive_service.files().list(q=query, fields="files(id, name)").execute()
    folders = results.get('files', [])
    
    if folders:
        # Folder exists, return the first one found
        TELEGRAM_BOT_FOLDER_ID = folders[0]['id']
        return TELEGRAM_BOT_FOLDER_ID
    else:
        # Folder doesn't exist, create it
        folder_metadata = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder'
        }
        folder = drive_service.files().create(body=folder_metadata, fields='id').execute()
        TELEGRAM_BOT_FOLDER_ID = folder.get('id')
        logger.info(f"Created folder '{folder_name}' with ID: {TELEGRAM_BOT_FOLDER_ID}")
        return TELEGRAM_BOT_FOLDER_ID

def authenticate_google_drive():
    """Authenticate and create a Google Drive service for desktop application."""
    creds = None
    # The file token.pickle stores the user's access and refresh tokens
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)
    
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        
        # Save the credentials for the next run
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)
    
    return build('drive', 'v3', credentials=creds)

def upload_to_google_drive(file_path, file_name):
    """Upload a file to Google Drive in the 'Telegram Bot' folder and return the shareable link."""
    try:
        drive_service = authenticate_google_drive()
        
        # Get or create the Telegram Bot folder
        folder_id = get_or_create_folder(drive_service, "Telegram Bot")
        
        # Create file metadata with parent folder
        file_metadata = {
            'name': file_name,
            'mimeType': '*/*',
            'parents': [folder_id]  # Add the file to the specific folder
        }
        
        # Create media object
        media = MediaFileUpload(file_path, resumable=True)
        
        # Upload the file
        file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        ).execute()
        
        # Make the file publicly accessible
        drive_service.permissions().create(
            fileId=file.get('id'),
            body={'type': 'anyone', 'role': 'reader'}
        ).execute()
        
        # Get the shareable link
        file_url = f"https://drive.google.com/uc?id={file.get('id')}&export=download"
        
        return file_url
    except Exception as e:
        logger.error(f"Error uploading to Google Drive: {e}")
        return None

async def download_with_telethon(file_id, file_path, status_msg=None):
    """
    Download a file using Telethon for more reliable large file downloads
    """
    global telethon_client
    
    try:
        # Initialize Telethon client if not already done
        if telethon_client is None:
            telethon_client = TelegramClient('bot_session', API_ID, API_HASH)
            await telethon_client.start(bot_token=TOKEN)
        
        # Get the file using Telethon
        file = await telethon_client.get_messages(
            entity=await telethon_client.get_me(),
            ids=int(file_id)
        )
        
        if not file or not file.media:
            raise Exception("File not found using Telethon")
        
        # Download the file
        downloaded = 0
        with open(file_path, 'wb') as f:
            async for chunk in telethon_client.iter_download(file.media):
                f.write(chunk)
                downloaded += len(chunk)
                
                # Update progress every 5MB
                if downloaded % (5 * 1024 * 1024) == 0 and status_msg:
                    try:
                        await status_msg.edit_text(f"📥 Downloading... {downloaded // (1024 * 1024)}MB downloaded")
                    except:
                        pass  # Silently fail if we can't update the status
        
        return True
    except Exception as e:
        logger.error(f"Error downloading with Telethon: {e}")
        return False

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming files, upload to Google Drive, and generate short URLs."""
    # Check if update has a valid user
    if not update.effective_user:
        logger.error("Update does not have an effective user")
        try:
            if update.message:
                await update.message.reply_text("Sorry, I couldn't process this request. Please try again.")
        except:
            pass  # Silently fail if we can't send a message
        return
        
    user_id = update.effective_user.id
    user = update.effective_user
    
    # Check if user has subscribed
    if user_id not in user_subscription_status:
        # Verify subscription status
        is_subscribed = await check_subscription(user_id, context)
        
        if is_subscribed:
            user_subscription_status[user_id] = True
        else:
            if update.message:
                await update.message.reply_text("Please use /start first and subscribe to our channel to use this bot.")
            return
        
    temp_file_path = None
    try:
        # Check if update has a message
        if not update.message:
            logger.error("Update does not have a message")
            return
            
        message = update.message
        
        # Determine file type and get file object
        file_obj = None
        if message.document:
            file_obj = message.document
        elif message.photo:
            file_obj = message.photo[-1]  # Highest resolution
        elif message.video:
            file_obj = message.video
        elif message.audio:
            file_obj = message.audio
        elif message.voice:
            file_obj = message.voice
        elif message.video_note:
            file_obj = message.video_note
        else:
            await message.reply_text("Unsupported file type.")
            return

        # Check if we found a file object
        if not file_obj:
            await message.reply_text("Could not process the file. Please try again.")
            return

        # Get file information
        file_id = file_obj.file_id
        file_name = getattr(file_obj, 'file_name', 'file')
        file_size = getattr(file_obj, 'file_size', 0)
        
        # Check if file is too large for Telegram download (2GB limit for bots)
        if file_size and file_size > 2000 * 1024 * 1024:  # 2GB
            await message.reply_text("❌ File is too large for Telegram download. Maximum size is 2GB.")
            return
        
        # Check if file is too large for Google Drive (5GB limit for free accounts)
        if file_size and file_size > MAX_FILE_SIZE:
            await message.reply_text("❌ File is too large for Google Drive. Maximum size is 5GB.")
            return
        
        # Notify user that download is starting
        status_msg = await message.reply_text(f"📥 Downloading your file ({file_size} bytes)...")
        
        # Create a temporary file with a unique name
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(file_name)[1])
        temp_file_path = temp_file.name
        temp_file.close()
        
        # Try to download using Telethon (more reliable for large files)
        download_success = await download_with_telethon(file_id, temp_file_path, status_msg)
        
        if not download_success:
            # Fall back to the standard method if Telethon fails
            try:
                # Get the file object from Telegram servers
                file = await context.bot.get_file(file_id)
                
                # Download using the standard method
                await file.download_to_drive(temp_file_path)
            except Exception as e:
                logger.error(f"Error downloading file with standard method: {e}")
                await status_msg.edit_text("❌ Failed to download file. Please try again.")
                return
        
        # Get the actual file size after download
        downloaded_size = os.path.getsize(temp_file_path)
        
        # Update status to show download completed
        await status_msg.edit_text(f"📥 Download complete! ({downloaded_size} bytes)")
        
        # Update status
        await status_msg.edit_text("☁️ Uploading to Google Drive...")
        
        # Upload to Google Drive
        drive_url = upload_to_google_drive(temp_file_path, file_name)
        
        if not drive_url:
            await status_msg.edit_text("❌ Failed to upload file to Google Drive. Please try again later.")
            return
        
        # Shorten the URL
        await status_msg.edit_text("🔗 Generating short URL...")
        short_url = shorten_url(drive_url)
        
        # Format the response message
        response = (
            f"📁 File Name: `{file_name}`\n"
            f"📊 File Size: {file_size} bytes\n\n"
            f"✅ Your file is ready to Download!\n\n"
            f"Bot created by {CREATOR_USERNAME}"
        )
        
        # Create inline keyboard with download button
        keyboard = [
            [InlineKeyboardButton("✅ Download Now", url=short_url)]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Send message with download button
        await status_msg.edit_text(response, reply_markup=reply_markup)
        
        # ONLY send notification when a user successfully gets a download link
        await send_download_notification(context, user, file_name, file_size, short_url)
        
    except Exception as e:
        logger.error(f"Error handling file: {e}", exc_info=True)
        await update.message.reply_text("Sorry, I couldn't process that file. Please try again.")
    
    finally:
        # Clean up temporary file
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.unlink(temp_file_path)
            except Exception as e:
                logger.error(f"Error deleting temp file: {e}")

async def get_my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command to get the user's chat ID"""
    user_id = update.effective_user.id
    await update.message.reply_text(f"Your chat ID is: {user_id}")

async def test_notification(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Test command to verify notifications are working"""
    try:
        test_message = "🔔 Test notification from your bot!\nThis confirms your notification system is working correctly."
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=test_message)
        await update.message.reply_text("Test notification sent! Check your messages.")
    except Exception as e:
        await update.message.reply_text(f"Failed to send test notification: {e}")

def main():
    """Start the bot."""
    # Create the Application
    application = Application.builder().token(TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("myid", get_my_id))
    application.add_handler(CommandHandler("test", test_notification))
    application.add_handler(CallbackQueryHandler(handle_subscription_callback))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_file))

    # Start the Bot
    application.run_polling()

if __name__ == '__main__':
    main()