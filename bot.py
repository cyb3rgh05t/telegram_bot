import asyncio
import nest_asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
import json
import os
import sqlite3
import re
import logging
import requests
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import pytz

# Apply nest_asyncio to handle running loops
nest_asyncio.apply()

# Load bot configuration from config/config.json
CONFIG_DIR = "config"
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')

with open(CONFIG_FILE, 'r') as config_file:
    config = json.load(config_file)

TOKEN = config.get("bot").get("TOKEN")
TIMEZONE = config.get("bot").get("TIMEZONE", "Europe/Berlin")
IMAGE_URL = config.get("welcome").get("IMAGE_URL")
BUTTON_URL = config.get("welcome").get("BUTTON_URL")
LOG_LEVEL = config.get("bot").get("LOG_LEVEL", "INFO").upper()
TMDB_API_KEY = config.get("tmdb").get("API_KEY")
SONARR_URL = config.get("sonarr").get("URL")
SONARR_API_KEY = config.get("sonarr").get("API_KEY")
SONARR_QUALITY_PROFILE_NAME = config.get("sonarr").get("QUALITY_PROFILE_NAME")
SONARR_ROOT_FOLDER_PATH = config.get("sonarr").get("ROOT_FOLDER_PATH")
RADARR_URL = config.get("radarr").get("URL")
RADARR_API_KEY = config.get("radarr").get("API_KEY")
RADARR_QUALITY_PROFILE_NAME = config.get("radarr").get("QUALITY_PROFILE_NAME")
RADARR_ROOT_FOLDER_PATH = config.get("radarr").get("ROOT_FOLDER_PATH")
DEFAULT_LANGUAGE = config.get("tmdb").get("DEFAULT_LANGUAGE")

# Configure logging
logging.basicConfig(
    format='%(levelname)s - %(message)s',
    level=getattr(logging, LOG_LEVEL, logging.INFO)
)

logger = logging.getLogger(__name__)

# Log the successful retrieval of the token with only first 6 characters visible
if TOKEN:
    redacted_token = TOKEN[:6] + '*' * (len(TOKEN) - 6)
    logger.info(f"Token retrieved successfully: {redacted_token}")
else:
    logger.error("Failed to retrieve bot token from config.")

# Ensure the config directory exists
if not os.path.exists(CONFIG_DIR):
    os.makedirs(CONFIG_DIR)

# Path for SQLite database file in the config folder
DATABASE_FILE = os.path.join(CONFIG_DIR, "group_data.db")

# Timezone configuration
try:
    TIMEZONE_OBJ = ZoneInfo(TIMEZONE)
    logger.info(f"Timezone is set to '{TIMEZONE}'.")
except Exception as e:
    logger.error(f"Invalid timezone '{TIMEZONE}' in config.json. Defaulting to 'Europe/Berlin'. {e}")
    TIMEZONE_OBJ = ZoneInfo("Europe/Berlin")

# Initialize SQLite connection and create table for storing group ID and language
def init_db():
    with sqlite3.connect(DATABASE_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS group_data (
                            id INTEGER PRIMARY KEY,
                            group_chat_id INTEGER,
                            language TEXT DEFAULT 'en'
                          )''')
        conn.commit()

# Load group chat ID and language from database
def load_group_id():
    with sqlite3.connect(DATABASE_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT group_chat_id, language FROM group_data WHERE id=1")
        row = cursor.fetchone()
    if row:
        logger.info(f"Loaded existing Group Chat ID: {row[0]}")
        logger.info(f"Loaded existing Tmdb Language: {row[1]}")
        return row[0], row[1]
    return None, DEFAULT_LANGUAGE

# Save group chat ID and language to database
def save_group_id(group_chat_id, language):
    with sqlite3.connect(DATABASE_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO group_data (id, group_chat_id, language) VALUES (1, ?, ?)", (group_chat_id, language))
        conn.commit()

# Initialize the group chat ID and language
init_db()
GROUP_CHAT_ID, LANGUAGE = load_group_id()
if GROUP_CHAT_ID is None:
    logger.info("Group Chat ID not set. Please set it using /set_group_id.")
else:
    logger.info(f"Group Chat ID is already set to: {GROUP_CHAT_ID}")

# Global variable to track if night mode is active
night_mode_active = False
# Define global locks
night_mode_lock = asyncio.Lock()
task_lock = asyncio.Lock()

# Get the current time in the desired timezone
def get_current_time():
    return datetime.now(TIMEZONE_OBJ)


# Function to check if the series is already in Sonarr
async def check_series_in_sonarr(series_tvdb_id):
    """Check if the series is already in Sonarr by TVDB ID."""
    try:
        response = requests.get(f"{SONARR_URL}/api/v3/series", params={"apikey": SONARR_API_KEY})
        response.raise_for_status()
        series_list = response.json()

        for series in series_list:
            if series['tvdbId'] == series_tvdb_id:
                logger.info(f"Series '{series['title']}' already exists in Sonarr (TVDB ID: {series['tvdbId']})")
                return True
        return False

    except requests.exceptions.HTTPError as http_err:
        logger.error(f"HTTP error while checking Sonarr: {http_err}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error while checking Sonarr: {e}")
        return False


# Function to check if the movie is already in Radarr
async def check_movie_in_radarr(movie_tmdb_id):
    """Check if the movie is already in Radarr by TMDb ID."""
    try:
        response = requests.get(f"{RADARR_URL}/api/v3/movie", params={"apikey": RADARR_API_KEY})
        response.raise_for_status()
        movie_list = response.json()

        for movie in movie_list:
            if movie['tmdbId'] == movie_tmdb_id:
                logger.info(f"Movie '{movie['title']}' already exists in Radarr.")
                return True
        return False

    except requests.exceptions.HTTPError as http_err:
        logger.error(f"HTTP error while checking Radarr: {http_err}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error while checking Radarr: {e}")
        return False


# Add media to Sonarr or Radarr after user confirmation
async def add_media_response(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    media_info = context.user_data.get('media_info')

    if media_info:
        title = media_info['title']
        media_type = media_info['media_type']

        if media_type == 'tv':
            await add_series_to_sonarr(title, update, context)
            await update.message.reply_text(f"'{title}' has been added to Sonarr.")
        elif media_type == 'movie':
            await add_movie_to_radarr(title, update, context)
            await update.message.reply_text(f"'{title}' has been added to Radarr.")
        else:
            await update.message.reply_text("Unexpected media type. Please try again.")

        # Clear media_info after adding the media
        context.user_data.pop('media_info', None)
    else:
        await update.message.reply_text("No media information found. Please search again.")

# Function to fetch additional details of the movie/TV show from TMDb
async def fetch_media_details(media_type, media_id):
    """Fetch detailed media information including poster, rating, summary, etc."""
    url = f"https://api.themoviedb.org/3/{media_type}/{media_id}?api_key={TMDB_API_KEY}&language={LANGUAGE}"
    logger.info(f"Fetching details from URL: {url}")
    try:
        response = requests.get(url)
        response.raise_for_status()
        logger.info(f"Details fetched successfully for media_id: {media_id}")
        return response.json()
    except Exception as e:
        logger.error(f"Error fetching media details: {e}")
        raise

def rating_to_stars(rating):
    """Convert a TMDb rating (out of 10) to a 10-star emoji string."""
    # Convert the rating to a 5-star scale
    stars = (rating / 10) * 10

    # Determine the number of full stars, half stars, and empty stars
    full_stars = int(stars)  # Full stars
    half_star = 1 if stars - full_stars >= 0.5 else 0  # Half star
    empty_stars = 10 - full_stars - half_star  # Empty stars

    # Build the star emoji string
    star_display = "⭐" * full_stars + "✨" * half_star + "★" * empty_stars
    return star_display

def extract_year_from_input(selected_title):
    """Extract the year from the user's input and ensure the format is correct."""
    # Use regex to find a year in parentheses, even if the parentheses are incomplete
    match = re.search(r'\((\d{4})', selected_title)
    if match:
        # Ensure the closing parenthesis is present and return the title up to the year
        return f"{selected_title[:match.end()]})"
    return selected_title  # If no year is found, return the original title

# Handle the user's media selection and display media details before confirming
async def handle_media_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):

    # Get the user's selected title and normalize it by extracting the year
    selected_title = extract_year_from_input(update.message.text.strip().lower())
    logger.info(f"User selected title: {selected_title}")

    media_options = context.user_data.get('media_options', None)
    if not media_options:
        await update.message.reply_text("No media options available. Please search again.")
        logger.error("No media options found in user data.")
        return

    # Log the available media options for debugging
    available_titles = []
    for option in media_options:
        media_type = option['media_type']
        media_title = option.get('title') if media_type == 'movie' else option.get('name')
        release_date = option.get('release_date', option.get('first_air_date', 'N/A'))
        release_year = release_date[:4] if release_date != 'N/A' else 'N/A'
        full_title = f"{media_title} ({release_year})".lower()
        available_titles.append(full_title)
    
    logger.info(f"Available media options: {available_titles}")

    # Find the selected media from the options
    media = None
    for option in media_options:
        media_type = option['media_type']
        media_title = option.get('title') if media_type == 'movie' else option.get('name')
        
        # Extract the release year from the date
        release_date = option.get('release_date', option.get('first_air_date', 'N/A'))
        release_year = release_date[:4] if release_date != 'N/A' else 'N/A'
        
        # Build the full title with release year
        full_title = f"{media_title} ({release_year})".lower()

        # Perform a case-insensitive and whitespace-tolerant comparison
        if selected_title == full_title:
            media = option
            logger.info(f"Selected media found: {media_title} ({release_year})")
            break

    if not media:
        await update.message.reply_text("Invalid selection. Please search again.")
        logger.error("Media selection did not match any option.")
        return

    # Clear the media options after selection
    context.user_data.pop('media_options', None)

    media_title = media['title'] if media['media_type'] == 'movie' else media['name']
    media_type = media['media_type']
    media_id = media['id']

    # Fetch additional media details from TMDb
    try:
        media_details = await fetch_media_details(media_type, media_id)
        logger.info(f"Fetched media details for {media_title} (TMDb ID: {media_id})")
    except Exception as e:
        await update.message.reply_text("Error fetching media details. Please try again later.")
        logger.error(f"Failed to fetch media details: {e}")
        return

    # Convert rating to stars using the helper function
    rating = media_details.get('vote_average', 0)
    star_rating = rating_to_stars(rating)

    # Extract the year from the release date for the detailed message as well
    full_release_date = media_details.get('release_date', media_details.get('first_air_date', 'N/A'))
    release_year_detailed = full_release_date[:4] if full_release_date != 'N/A' else 'N/A'

    # Prepare the message with media details and star rating
    message = (
        f"🎬 *{media_title}* ({release_year_detailed}) \n\n"
        f"{star_rating} - {rating}/10\n"
        f"\n{media_details.get('overview', 'No summary available.')}"
    )

    # Send media details regardless of existence in Sonarr/Radarr
    if media_details.get('poster_path'):
        poster_url = f"https://image.tmdb.org/t/p/w500{media_details['poster_path']}"
        await update.message.reply_photo(photo=poster_url, caption=message, parse_mode="Markdown")
    else:
        await update.message.reply_text(text=message, parse_mode="Markdown")

    # Check if the media already exists in Radarr or Sonarr
    if media_type == 'movie':
        if await check_movie_in_radarr(media_id):
            await update.message.reply_text(f"❌ The movie '{media_title}' already exists in Radarr.")
        else:
            await update.message.reply_text(f"The movie '{media_title}' is not in Radarr. Would you like to add it? Reply with 'yes' or 'no'.")
            context.user_data['media_info'] = {'title': media_title, 'media_type': 'movie'}
    elif media_type == 'tv':
        external_ids_url = f"https://api.themoviedb.org/3/tv/{media_id}/external_ids?api_key={TMDB_API_KEY}"
        external_ids_response = requests.get(external_ids_url)
        external_ids_response.raise_for_status()
        external_ids_data = external_ids_response.json()

        tvdb_id = external_ids_data.get('tvdb_id')
        if not tvdb_id:
            await update.message.reply_text(f"❌ No TVDB ID found for the series '{media_title}'.")
            logger.error(f"No TVDB ID found for the series '{media_title}'")
            return

        if await check_series_in_sonarr(tvdb_id):
            await update.message.reply_text(f"❌ The series '{media_title}' already exists in Sonarr.")
        else:
            await update.message.reply_text(f"The series '{media_title}' is not in Sonarr. Would you like to add it? Reply with 'yes' or 'no'.")
            context.user_data['media_info'] = {'title': media_title, 'media_type': 'tv', 'tvdb_id': tvdb_id}


# Search for a movie or TV show using TMDB API with multiple results handling
async def search_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not context.args:
            await update.message.reply_text("Please provide a title to search.")
            return

        title = " ".join(context.args)
        logger.info(f"Searching for media: {title}")
        
        url = f"https://api.themoviedb.org/3/search/multi?api_key={TMDB_API_KEY}&query={title}&language={LANGUAGE}"
        response = requests.get(url)
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 1))
            logger.warning(f"Rate limited by TMDb. Retrying after {retry_after} seconds.")
            await asyncio.sleep(retry_after)
            response = requests.get(url)

        
        response.raise_for_status()
        media_data = response.json()

        if not media_data['results']:
            await update.message.reply_text(f"No results found for the title '{title}'. Please try another title.")
            return

        # If more than one result is found, show a list to the user
        if len(media_data['results']) > 1:
            media_titles = []
            for media in media_data['results']:
                media_type = media['media_type']
                media_title = media['title'] if media_type == 'movie' else media['name']
                release_date = media.get('release_date', media.get('first_air_date', 'N/A'))
                # Extract the year from the release date, default to 'N/A' if missing
                release_year = release_date[:4] if release_date != 'N/A' else 'N/A'
                # Append index for easier selection
                media_titles.append(f"{media_title} ({release_year})")

            # Send the list of results to the user
            reply_keyboard = [[title] for title in media_titles]
            await update.message.reply_text(
                 "Multiple results found. Please choose the correct title:",
                 reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True)
            )

            # Store media results in user data for later selection
            context.user_data['media_options'] = media_data['results']
            logger.info(f"Media options stored: {len(media_data['results'])} results")
            return

        # If only one result, continue with displaying details and confirmation
        media = media_data['results'][0]
        await handle_media_selection(update, context, media)

    except requests.exceptions.HTTPError as http_err:
        logger.error(f"HTTP error occurred: {http_err}")
        await update.message.reply_text("❌ An HTTP error occurred while fetching data from TMDb. Please try again later.")
    except requests.exceptions.RequestException as e:
        logger.error(f"An error occurred: {e}")
        await update.message.reply_text("❌ An error occurred while fetching data. Please try again later.")
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        await update.message.reply_text("❌ An unexpected error occurred. Please try again later.")

# Handle user's confirmation (yes/no)
async def handle_user_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    media_info = context.user_data.get('media_info')

    if media_info:
        if update.message.text.lower() == 'yes':
            await add_media_response(update, context)
        elif update.message.text.lower() == 'no':
            await update.message.reply_text("Operation canceled. The media was not added.")
            context.user_data.pop('media_info', None)
        else:
            await update.message.reply_text("Please reply with 'yes' or 'no'.")
    else:
        await update.message.reply_text("No media information found. Please search for media first.")

# Message handler for general text
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get('media_info'):
        # Handle user confirmation for adding media to Sonarr or Radarr
        await handle_user_confirmation(update, context)
    elif context.user_data.get('media_options'):
        # Handle media selection if options were provided
        await handle_media_selection(update, context)
    else:
        # Handle other general messages
        if night_mode_active or await night_mode_checker(context):
            await restrict_night_mode(update, context)



# Function to get quality profile ID by name from Sonarr
async def get_quality_profile_id(sonarr_url, api_key, profile_name):
    try:
        response = requests.get(f"{sonarr_url}/api/v3/qualityprofile", params={"apikey": api_key})
        response.raise_for_status()
        profiles = response.json()

        for profile in profiles:
            if profile['name'] == profile_name:
                return profile['id']
    
        logger.warning(f"Quality profile '{profile_name}' not found in Sonarr.")
        return None
  
    except requests.exceptions.HTTPError as http_err:
        logger.error(f"HTTP error occurred while fetching quality profiles: {http_err}")
        return None
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        return None

# Function to add a series to Sonarr
async def add_series_to_sonarr(series_name, update: Update, context: ContextTypes.DEFAULT_TYPE):
    # First, get the TMDb ID for the series
    tmdb_url = f"https://api.themoviedb.org/3/search/tv?api_key={TMDB_API_KEY}&query={series_name}"
    tmdb_response = requests.get(tmdb_url)
    tmdb_response.raise_for_status()
    tmdb_data = tmdb_response.json()

    if not tmdb_data['results']:
        logger.error(f"No TMDb results found for the series '{series_name}'")
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ No TMDb results found for the series '{series_name}'.")
        return

    # Use the first search result for simplicity
    series_tmdb_id = tmdb_data['results'][0]['id']

    # Use TMDb ID to get TVDB ID (Sonarr uses TVDB)
    external_ids_url = f"https://api.themoviedb.org/3/tv/{series_tmdb_id}/external_ids?api_key={TMDB_API_KEY}"
    external_ids_response = requests.get(external_ids_url)
    external_ids_response.raise_for_status()
    external_ids_data = external_ids_response.json()

    tvdb_id = external_ids_data.get('tvdb_id')
    if not tvdb_id:
        logger.error(f"No TVDB ID found for the series '{series_name}'")
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ No TVDB ID found for the series '{series_name}'.")
        return

    # Check if the series is already in Sonarr
    if await check_series_in_sonarr(tvdb_id):
        logger.info(f"Series '{series_name}' already exists in Sonarr, skipping addition.")
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ The series '{series_name}' already exists in Sonarr.")
        return

    # Proceed with adding the series if it's not found in Sonarr
    quality_profile_id = await get_quality_profile_id(SONARR_URL, SONARR_API_KEY, SONARR_QUALITY_PROFILE_NAME)
    if quality_profile_id is None:
        logger.error("Quality profile not found in Sonarr.")
        await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Quality profile not found in Sonarr.")
        return

    data = {
        "title": series_name,
        "qualityProfileId": quality_profile_id,
        "rootFolderPath": SONARR_ROOT_FOLDER_PATH,
        "seasonFolder": True,
        "tvdbId": tvdb_id,
        "monitored": True,
        "addOptions": {
            "searchForMissingEpisodes": True  # Even if this fails, manual search will be triggered
        }
    }

    response = requests.post(f"{SONARR_URL}/api/v3/series", json=data, params={"apikey": SONARR_API_KEY})
    if response.status_code == 201:
        logger.info(f"Series '{series_name}' added to Sonarr successfully.")

        # Manually trigger a search for the series
        series_id = response.json().get('id')
        search_data = {"name": "SeriesSearch", "seriesId": series_id}
        search_response = requests.post(f"{SONARR_URL}/api/v3/command", json=search_data, params={"apikey": SONARR_API_KEY})

        if search_response.status_code == 201:
            logger.info(f"Search for series '{series_name}' started.")
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"✅ Series '{series_name}' added to Sonarr and search started.")
        else:
            logger.error(f"Failed to start search for series '{series_name}'. Status code: {search_response.status_code}, Response: {search_response.text}")
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ Failed to start search for series '{series_name}'.")
    else:
        logger.error(f"Failed to add series '{series_name}' to Sonarr. Status code: {response.status_code}, Response: {response.text}")
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ Failed to add series '{series_name}' to Sonarr. Status code: {response.status_code}")



# Function to get quality profile ID by name from Radarr
async def get_radarr_quality_profile_id(radarr_url, api_key, profile_name):
    try:
        response = requests.get(f"{radarr_url}/api/v3/qualityprofile", params={"apikey": api_key})
        response.raise_for_status()  # Raise an error for bad responses
        profiles = response.json()
        
        for profile in profiles:
            if profile['name'] == profile_name:
                return profile['id']
                
        logger.warning(f"Quality profile '{profile_name}' not found in Radarr.")
        return None
        
    except requests.exceptions.HTTPError as http_err:
        logger.error(f"HTTP error occurred while fetching quality profiles: {http_err}")
        return None
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        return None

# Function to add a movie to Radarr
async def add_movie_to_radarr(movie_name, update: Update, context: ContextTypes.DEFAULT_TYPE):
    # First, get the TMDb ID for the movie
    tmdb_url = f"https://api.themoviedb.org/3/search/movie?api_key={TMDB_API_KEY}&query={movie_name}"
    tmdb_response = requests.get(tmdb_url)
    tmdb_response.raise_for_status()
    tmdb_data = tmdb_response.json()

    if not tmdb_data['results']:
        logger.error(f"No TMDb results found for the movie '{movie_name}'")
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ No TMDb results found for the movie '{movie_name}'.")
        return

    # Use the first search result for simplicity
    movie_tmdb_id = tmdb_data['results'][0]['id']

    # Check if the movie is already in Radarr
    if await check_movie_in_radarr(movie_tmdb_id):
        logger.info(f"Movie '{movie_name}' already exists in Radarr, skipping addition.")
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ The movie '{movie_name}' already exists in Radarr.")
        return

    # Proceed with adding the movie if it's not found in Radarr
    quality_profile_id = await get_radarr_quality_profile_id(RADARR_URL, RADARR_API_KEY, RADARR_QUALITY_PROFILE_NAME)
    if quality_profile_id is None:
        logger.error("Quality profile not found in Radarr.")
        await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Quality profile not found in Radarr.")
        return

    data = {
        "title": movie_name,
        "qualityProfileId": quality_profile_id,
        "rootFolderPath": RADARR_ROOT_FOLDER_PATH,
        "tmdbId": movie_tmdb_id,
        "monitored": True,
        "addOptions": {
            "searchForMovie": True  # Even if this fails, manual search will be triggered
        }
    }

    response = requests.post(f"{RADARR_URL}/api/v3/movie", json=data, params={"apikey": RADARR_API_KEY})
    if response.status_code == 201:
        logger.info(f"Movie '{movie_name}' added to Radarr successfully.")

        # Manually trigger a search for the movie
        movie_id = response.json().get('id')
        search_data = {"name": "MoviesSearch", "movieIds": [movie_id]}
        search_response = requests.post(f"{RADARR_URL}/api/v3/command", json=search_data, params={"apikey": RADARR_API_KEY})

        if search_response.status_code == 201:
            logger.info(f"Search for movie '{movie_name}' started.")
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"✅ Movie '{movie_name}' added to Radarr and search started.")
        else:
            logger.error(f"Failed to start search for movie '{movie_name}'. Status code: {search_response.status_code}, Response: {search_response.text}")
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ Failed to start search for movie '{movie_name}'.")
    else:
        logger.error(f"Failed to add movie '{movie_name}' to Radarr. Status code: {response.status_code}, Response: {response.text}")
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ Failed to add movie '{movie_name}' to Radarr. Status code: {response.status_code}")


# Command to set the group ID
async def set_group_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global GROUP_CHAT_ID
    GROUP_CHAT_ID = update.message.chat_id
    save_group_id(GROUP_CHAT_ID, LANGUAGE)
    logger.info(f"Group chat ID set to: {GROUP_CHAT_ID} by user {update.message.from_user.id}")
    await update.message.reply_text(f"Group chat ID set to: {GROUP_CHAT_ID}")

# Enable or disable night mode
async def enable_night_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global night_mode_active
    if not night_mode_active:
        night_mode_active = True
        logger.info(f"Night mode enabled by user {update.message.from_user.id}.")
        await context.bot.send_message(chat_id=GROUP_CHAT_ID, text="🌙 NACHTMODUS AKTIVIERT.\n\nStreamNet TV Staff Team braucht auch mal eine Pause 😴😪🥱💤🛌🏼")

async def disable_night_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global night_mode_active
    if night_mode_active:
        night_mode_active = False
        logger.info(f"Night mode disabled by user {update.message.from_user.id}.")
        await context.bot.send_message(chat_id=GROUP_CHAT_ID, text="☀️ ENDE DES NACHTMODUS.\n\n✅ Ab jetzt kannst du wieder Mitteilungen in der Gruppe senden.")

# Night mode checker
async def night_mode_checker(context):
    global night_mode_active

    now = get_current_time()

    if now.hour == 0 and not night_mode_active:
        night_mode_active = True
        logger.info("Night mode activated at midnight.")
        await context.bot.send_message(chat_id=GROUP_CHAT_ID, 
                                       text="🌙 NACHTMODUS AKTIVIERT.\n\nStreamNet TV Staff Team braucht auch mal eine Pause 😴😪🥱💤🛌🏼")
    elif now.hour == 7 and night_mode_active:
        night_mode_active = False
        logger.info("Night mode deactivated at 7:00 AM.")
        await context.bot.send_message(chat_id=GROUP_CHAT_ID, 
                                       text="☀️ ENDE DES NACHTMODUS.\n\n✅ Ab jetzt kannst du wieder Mitteilungen in der Gruppe senden.")

# Restrict messages during night mode
async def restrict_night_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    now = get_current_time()
    if night_mode_active:
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        member = await context.bot.get_chat_member(chat_id, user_id)
        is_admin = member.status in ('administrator', 'creator')
        if not is_admin:
            logger.info(f"Deleting message from non-admin user {update.message.from_user.id} due to night mode.")
            await update.message.reply_text("❌ Sorry, solange der NACHTMODUS aktiviert ist (00:00 - 07:00 Uhr), kannst du keine Mitteilungen in der Gruppe oder in den Topics senden.")
            await context.bot.delete_message(chat_id=update.message.chat_id, message_id=update.message.message_id)

# Command to set the language for TMDB searches
async def set_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global LANGUAGE
    if context.args:
        language_code = context.args[0]
        if len(language_code) == 2:
            LANGUAGE = language_code
            save_group_id(GROUP_CHAT_ID, LANGUAGE)
            logger.info(f"Language set to: {LANGUAGE} by user {update.message.from_user.id}")
            await update.message.reply_text(f"Language set to: {LANGUAGE}")
        else:
            await update.message.reply_text("Invalid language code. Please use a 2-letter language code (e.g., 'en', 'de').")
    else:
        await update.message.reply_text("Please specify a language code (e.g., 'en', 'de').")

# Function to welcome new members
async def welcome_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    for member in update.message.new_chat_members:
        logger.info(f"New member {member.full_name} joined the group.")
        button = InlineKeyboardButton("StreamNet TV Store", url=BUTTON_URL)
        keyboard = InlineKeyboardMarkup([[button]])

        now = get_current_time()
        date_time = now.strftime("%d.%m.%Y %H:%M:%S")
        username = f"@{member.username}" if member.username else member.full_name

        welcome_message = (
            f"\n🎉 Howdy, {member.full_name}!\n\n"
            "Vielen Dank, dass du diesen Service ausgewählt hast ❤️.\n\n"
            f"Username: {username}\n"
            f"Beitritt: {date_time}\n\n"
            "Wir hoffen, du hast eine gute Unterhaltung mit StreamNet TV.\n\n"
            "Bei Fragen oder sonstiges einfach in die verschiedenen Topics reinschreiben."
        )

        await update.message.chat.send_photo(
            photo=IMAGE_URL,
            caption=welcome_message,
            reply_markup=keyboard
        )

# Start bot function
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_html(
        rf"Hi {user.mention_html()}! Welcome to the bot. Use /search to find a movie or TV show.",
        reply_markup=ReplyKeyboardRemove()
    )

# Main bot function
async def main() -> None:
    application = ApplicationBuilder().token(TOKEN).build()

    # Register the command handler
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("set_group_id", set_group_id))
    application.add_handler(CommandHandler("set_language", set_language))
    application.add_handler(CommandHandler("enable_night_mode", enable_night_mode))
    application.add_handler(CommandHandler("disable_night_mode", disable_night_mode))
    application.add_handler(CommandHandler("search", search_media))

    # Register the message handler for new members
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_members))

    # Start the night mode checker task with max_instances set to 1
    application.job_queue.run_repeating(night_mode_checker, interval=300, first=0)

    # Register the message handler for user confirmation and general messages
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    # Start the Bot
    logger.info("Bot started polling.")
    await application.run_polling()

if __name__ == '__main__':
    try:
        logger.info("Starting the bot...")
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped gracefully.")
    except Exception as e:
        logger.error(f"An unexpected error occurred during startup: {e}")
