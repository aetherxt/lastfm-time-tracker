from flask import Flask, render_template, request
import requests
from datetime import datetime, timedelta
import time
from functools import lru_cache
import logging
import json
import csv
import os
import threading
import math

app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Last.fm API settings
API_KEY = "acbc44e8e15a6a470e3fc3372feea719"  # Replace with your Last.fm API key
API_URL = "http://ws.audioscrobbler.com/2.0/"

# Pagination settings
DEFAULT_ITEMS_PER_PAGE = 10

# CSV cache file path
DURATION_CACHE_FILE = "song_durations.csv"
DAILY_CACHE_FILE = "daily_stats.json"
cache_lock = threading.Lock()  # Lock for thread-safe CSV operations
daily_cache_lock = threading.Lock()  # Lock for thread-safe JSON operations


# Load song duration cache from CSV
def load_song_cache():
    cache = {}
    if os.path.exists(DURATION_CACHE_FILE):
        try:
            with open(DURATION_CACHE_FILE, "r", encoding="utf-8", newline="") as file:
                reader = csv.reader(file)
                next(reader, None)  # Skip header row
                for row in reader:
                    if len(row) >= 3:
                        artist, track_name, duration = row[0], row[1], int(row[2])
                        cache[(artist, track_name)] = duration
            logger.info(f"Loaded {len(cache)} song durations from cache")
        except Exception as e:
            logger.error(f"Error loading song cache: {e}")
    return cache


# Initialize song duration cache
song_duration_cache = load_song_cache()


# Load daily stats cache
def load_daily_stats_cache():
    cache = {}
    if os.path.exists(DAILY_CACHE_FILE):
        try:
            with open(DAILY_CACHE_FILE, "r", encoding="utf-8") as file:
                data = json.load(file)
                if "daily_stats" in data:
                    cache = data["daily_stats"]
            logger.info(f"Loaded daily stats for {len(cache)} entries")
        except Exception as e:
            logger.error(f"Error loading daily stats cache: {e}")
    return cache


# Initialize daily stats cache
daily_stats_cache = load_daily_stats_cache()


# Save daily stats to cache
def save_daily_stats_to_cache(username, date_str, stats):
    with daily_cache_lock:
        try:
            # Update in-memory cache
            if username not in daily_stats_cache:
                daily_stats_cache[username] = {}

            daily_stats_cache[username][date_str] = stats

            # Write to file
            with open(DAILY_CACHE_FILE, "w", encoding="utf-8") as file:
                json.dump({"daily_stats": daily_stats_cache}, file, indent=4)

            logger.debug(f"Saved daily stats for {username} on {date_str}")
        except Exception as e:
            logger.error(f"Error saving daily stats to cache: {e}")


# Save a song to the cache
def save_song_to_cache(artist, track_name, duration):
    with cache_lock:
        try:
            file_exists = os.path.exists(DURATION_CACHE_FILE)

            with open(DURATION_CACHE_FILE, "a", encoding="utf-8", newline="") as file:
                writer = csv.writer(file)

                # Write header if file is new
                if not file_exists:
                    writer.writerow(["artist", "track_name", "duration"])

                # Write song info
                writer.writerow([artist, track_name, duration])

            # Update in-memory cache
            song_duration_cache[(artist, track_name)] = duration
            logger.debug(f"Added to cache: {artist} - {track_name}: {duration}s")

        except Exception as e:
            logger.error(f"Error saving song to cache: {e}")


@app.template_filter("date_format")
def date_format(value, format="%m-%d-%Y"):
    """Format a date string (YYYY-MM-DD) to a readable format."""
    if not value:
        return ""
    try:
        date_obj = datetime.strptime(str(value), "%Y-%m-%d")
        return date_obj.strftime(format)
    except ValueError:
        return value


@app.template_filter("timestamp_to_time")
def timestamp_to_time(timestamp):
    """Convert a Unix timestamp to a readable time format."""
    dt = datetime.fromtimestamp(timestamp)
    return dt.strftime("%H:%M:%S")


@app.route("/", methods=["GET", "POST"])
def index():
    error = None
    scrobbles = []
    total_time = None
    username = ""
    selected_date = ""
    current_page = 1
    page_count = 0
    total_scrobbles = 0
    per_page = DEFAULT_ITEMS_PER_PAGE

    # Get parameters
    if request.method == "POST":
        per_page = int(request.form.get("per_page", DEFAULT_ITEMS_PER_PAGE))
    else:
        per_page = int(request.args.get("per_page", DEFAULT_ITEMS_PER_PAGE))

    # Get username from query parameter (when switching tabs)
    if (
        request.args.get("username")
        and request.method == "GET"
        and not request.args.get("date")
    ):
        username = request.args.get("username")
        # Just pre-fill the form, but don't fetch data yet

    # Get query parameters if we're navigating between pages
    elif (
        request.method == "GET"
        and "username" in request.args
        and "date" in request.args
    ):
        username = request.args.get("username")
        selected_date = request.args.get("date")
        current_page = int(request.args.get("page", 1))

        try:
            # Get all scrobbles for this day to calculate time
            all_scrobbles, total_time = get_scrobbles_and_time(username, selected_date)

            # Store total number of scrobbles
            total_scrobbles = len(all_scrobbles)

            # Calculate page count
            page_count = math.ceil(total_scrobbles / per_page)

            # Paginate the scrobbles
            start_idx = (current_page - 1) * per_page
            end_idx = start_idx + per_page
            scrobbles = all_scrobbles[start_idx:end_idx]

            if not scrobbles:
                error = f"No scrobbles found for {username} on {selected_date}"
        except Exception as e:
            error = f"Error: {str(e)}"
            logger.exception("Error occurred in index GET route")

    # Handle form submission (initial request)
    elif request.method == "POST":
        username = request.form.get("username")
        selected_date = request.form.get("date")
        current_page = 1

        if not username:
            error = "Please enter a Last.fm username"
        elif not selected_date:
            error = "Please select a date"
        else:
            try:
                # Get all scrobbles for this day to calculate time
                all_scrobbles, total_time = get_scrobbles_and_time(
                    username, selected_date
                )

                # Store total number of scrobbles
                total_scrobbles = len(all_scrobbles)

                # Calculate page count
                page_count = math.ceil(total_scrobbles / per_page)

                # Get only the first page of scrobbles
                scrobbles = all_scrobbles[:per_page]

                if not scrobbles:
                    error = f"No scrobbles found for {username} on {selected_date}"
            except Exception as e:
                error = f"Error: {str(e)}"
                logger.exception("Error occurred in index POST route")

    return render_template(
        "index.html",
        username=username,
        selected_date=selected_date,
        scrobbles=scrobbles,
        total_time=total_time,
        error=error,
        current_page=current_page,
        page_count=page_count,
        total_scrobbles=total_scrobbles,
        per_page=per_page,
        active_page="daily",
    )


@app.route("/weekly", methods=["GET", "POST"])
def weekly_stats():
    error = None
    weekly_data = None
    username = ""
    start_date = ""
    weekly_totals = None

    # Get username from query parameter (when switching tabs)
    if request.args.get("username") and request.method == "GET":
        username = request.args.get("username")
        # Just pre-fill the form, but don't fetch data yet

    elif request.method == "POST":
        username = request.form.get("username")
        start_date = request.form.get("start_date")

        if not username:
            error = "Please enter a Last.fm username"
        elif not start_date:
            error = "Please select a start date for the week"
        else:
            try:
                # Get weekly listening data
                weekly_data = get_weekly_listening_data(username, start_date)

                if not any(day["total_seconds"] > 0 for day in weekly_data):
                    error = f"No scrobbles found for {username} in the week starting {start_date}"
                else:
                    # Calculate weekly totals
                    total_week_seconds = sum(
                        day["total_seconds"] for day in weekly_data
                    )
                    total_week_tracks = sum(
                        day["scrobble_count"] for day in weekly_data
                    )

                    # Convert to hours and minutes
                    week_hours, week_remainder = divmod(total_week_seconds, 3600)
                    week_minutes, week_seconds = divmod(week_remainder, 60)

                    weekly_totals = {
                        "hours": int(week_hours),
                        "minutes": int(week_minutes),
                        "seconds": int(week_seconds),
                        "total_seconds": total_week_seconds,
                        "total_tracks": total_week_tracks,
                    }

            except Exception as e:
                error = f"Error: {str(e)}"
                logger.exception("Error occurred in weekly_stats route")

    return render_template(
        "weekly.html",
        username=username,
        start_date=start_date,
        weekly_data=weekly_data,
        weekly_totals=weekly_totals,
        error=error,
        active_page="weekly",
    )


def get_weekly_listening_data(username, start_date_str):
    """Get listening data for a full week starting from the given date."""
    start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
    today_str = datetime.now().strftime("%Y-%m-%d")

    # Initialize weekly data
    weekly_data = []

    # Fetch data for each day of the week
    for day_offset in range(7):  # 0 to 6, representing 7 days of the week
        current_date = start_date + timedelta(days=day_offset)
        day_str = current_date.strftime("%Y-%m-%d")

        # Check cache first (unless it's today)
        cached_data = None
        if day_str != today_str:
            if username in daily_stats_cache and day_str in daily_stats_cache[username]:
                cached_data = daily_stats_cache[username][day_str]
                logger.debug(f"Using cached daily stats for {username} on {day_str}")

        if cached_data:
            total_time = cached_data["total_time"]
            scrobble_count = cached_data["scrobble_count"]
            scrobbles = (
                []
            )  # We don't have the list of scrobbles, but we don't need them for weekly stats
        else:
            # Get scrobbles for this day
            try:
                scrobbles, total_time = get_scrobbles_and_time(username, day_str)
                scrobble_count = len(scrobbles)

                # Note: get_scrobbles_and_time now saves to cache automatically, so we don't need to do it here
            except Exception as e:
                logger.error(
                    f"Error getting scrobbles for {username} on {day_str}: {e}"
                )
                # Use empty values for this day if there was an error
                scrobbles = []
                total_time = {
                    "hours": 0,
                    "minutes": 0,
                    "seconds": 0,
                    "total_seconds": 0,
                }
                scrobble_count = 0

        # Format day name
        day_name = current_date.strftime("%A")  # Full day name (Monday, Tuesday, etc.)
        short_date = current_date.strftime("%m/%d")  # Short date format MM/DD

        # Add to weekly data
        weekly_data.append(
            {
                "day_name": day_name,
                "date": day_str,
                "short_date": short_date,
                "total_time": total_time,
                "scrobble_count": scrobble_count,
                "total_seconds": total_time["total_seconds"],
                "hours": total_time["hours"],
                "minutes": total_time["minutes"],
            }
        )

    return weekly_data


def get_scrobbles_and_time(username, date_str):
    """Get a user's scrobbles for a specific date and calculate total listening time."""
    # Convert date string to datetime object
    selected_date = datetime.strptime(date_str, "%Y-%m-%d")

    # Calculate unix timestamps for the start and end of the day
    start_timestamp = int(time.mktime(selected_date.timetuple()))
    end_timestamp = (
        int(time.mktime((selected_date + timedelta(days=1)).timetuple())) - 1
    )

    # Get user's scrobbles for the specified date
    scrobbles = get_user_scrobbles(username, start_timestamp, end_timestamp)

    # Calculate total listening time
    total_time = calculate_listening_time(scrobbles)

    # Save to daily stats cache (even if empty)
    save_daily_stats_to_cache(
        username, date_str, {"total_time": total_time, "scrobble_count": len(scrobbles)}
    )

    return scrobbles, total_time


def safe_get(data, keys, default=None):
    """Safely get nested dictionary values by checking each key exists."""
    if not isinstance(data, dict):
        return default

    current = data
    for key in keys:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return default
    return current


def get_user_scrobbles(username, from_timestamp, to_timestamp):
    """Fetch all tracks scrobbled by a user within a specified time range."""
    scrobbles = []
    page = 1
    total_pages = 1

    # Last.fm API uses pagination, so we need to fetch all pages
    while page <= total_pages:
        params = {
            "method": "user.getrecenttracks",
            "user": username,
            "from": from_timestamp,
            "to": to_timestamp,
            "api_key": API_KEY,
            "format": "json",
            "page": page,
            "limit": 200,  # Maximum allowed per page
        }

        try:
            response = requests.get(API_URL, params=params)
            response.raise_for_status()
            data = response.json()

            # Log the response structure for debugging
            logger.debug(f"API Response structure: {json.dumps(data)[:500]}...")

            if "error" in data:
                raise Exception(data["message"])

            if "recenttracks" in data and "track" in data["recenttracks"]:
                tracks = data["recenttracks"]["track"]

                # Update total pages info
                total_pages = int(
                    safe_get(data, ["recenttracks", "@attr", "totalPages"], 1)
                )

                # Add tracks to our list
                for track in tracks:
                    # Skip "now playing" tracks
                    if "@attr" in track and "nowplaying" in track["@attr"]:
                        continue

                    # Get album art
                    album_art = None
                    if (
                        "image" in track
                        and isinstance(track["image"], list)
                        and len(track["image"]) > 1
                    ):
                        for img in track["image"]:
                            if img.get("size") == "medium":
                                album_art = img.get("#text", "")
                                break

                    # Safely extract data using our helper function
                    artist = safe_get(track, ["artist", "#text"], "Unknown Artist")
                    # Handle case where artist might be a string directly
                    if isinstance(track.get("artist"), str):
                        artist = track["artist"]

                    track_name = track.get("name", "Unknown Track")

                    album = safe_get(track, ["album", "#text"], "Unknown Album")
                    # Handle case where album might be a string directly
                    if isinstance(track.get("album"), str):
                        album = track["album"]

                    # Check if date exists and has the expected structure
                    if (
                        "date" in track
                        and isinstance(track["date"], dict)
                        and "uts" in track["date"]
                    ):
                        timestamp = int(track["date"]["uts"])
                    else:
                        # Default to current time if timestamp not available
                        logger.warning(
                            f"Missing timestamp for track {track_name}, using current time"
                        )
                        timestamp = int(time.time())

                    scrobbles.append(
                        {
                            "artist": artist,
                            "name": track_name,
                            "album": album,
                            "timestamp": timestamp,
                            "image": album_art,
                        }
                    )

                page += 1
            else:
                break

        except requests.exceptions.RequestException as e:
            raise Exception(f"Network error when connecting to Last.fm API: {str(e)}")
        except Exception as e:
            logger.exception("Error in get_user_scrobbles")
            logger.error(f"Problem with response: {str(e)}")
            raise Exception(f"Error processing Last.fm API response: {str(e)}")

    # Sort scrobbles by timestamp (newest first)
    scrobbles.sort(key=lambda x: x["timestamp"], reverse=True)
    return scrobbles


def calculate_listening_time(scrobbles):
    """Calculate total listening time for a list of scrobbled tracks."""
    total_seconds = 0

    for track in scrobbles:
        try:
            # Get track duration from the cache or API
            duration = get_track_duration(track["artist"], track["name"])
            track["duration"] = duration  # Store the duration in the track dict

            # Add duration to total
            total_seconds += duration
        except Exception as e:
            logger.error(
                f"Error calculating duration for track {track.get('name', 'unknown')}: {e}"
            )
            # Use default duration in case of error
            track["duration"] = 180
            total_seconds += 180

    # Convert seconds to a readable format (hours, minutes, seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    return {
        "hours": int(hours),
        "minutes": int(minutes),
        "seconds": int(seconds),
        "total_seconds": total_seconds,
    }


def get_track_duration(artist, track_name):
    """Get the duration of a track from cache or Last.fm API."""
    # First check in memory cache
    cache_key = (artist, track_name)
    if cache_key in song_duration_cache:
        logger.debug(f"Cache hit: {artist} - {track_name}")
        return song_duration_cache[cache_key]

    logger.debug(f"Cache miss: {artist} - {track_name}, fetching from API")

    # If not in cache, fetch from Last.fm API
    params = {
        "method": "track.getInfo",
        "artist": artist,
        "track": track_name,
        "api_key": API_KEY,
        "format": "json",
    }

    try:
        response = requests.get(API_URL, params=params)
        response.raise_for_status()
        data = response.json()

        if "track" in data and "duration" in data["track"]:
            # Duration is in milliseconds, convert to seconds
            duration = int(data["track"]["duration"]) // 1000
            # If the duration is 0, set a default value (3 minutes)
            duration = duration if duration > 0 else 180
        else:
            # If duration not available, use a default value (3 minutes)
            duration = 180

        # Save to cache
        save_song_to_cache(artist, track_name, duration)

        return duration
    except Exception as e:
        # If there's an error, use a default value
        logger.error(f"Error getting track duration for {artist} - {track_name}: {e}")
        default_duration = 180

        # Still save the default duration to cache to avoid repeated API calls for the same song
        save_song_to_cache(artist, track_name, default_duration)

        return default_duration


if __name__ == "__main__":
    app.run(debug=True)
