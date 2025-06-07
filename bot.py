import asyncio
import concurrent.futures
import fnmatch
import gc
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import playwright.async_api
from telethon import TelegramClient
from playwright.async_api import async_playwright

import ccxt
import freqtrade_client
import rapidjson


# Some exchanges use non-normalized letters which can throw off the comparison and finding of those pairs.
# So we normalize any message-string.
def remove_markdown_from_text(text):
    # Normalize Unicode (e.g., full-width characters)
    normalized = unicodedata.normalize('NFKC', text)

    # Remove common Markdown formatting characters
    cleaned = re.sub(r'[*_`~]', '', normalized)

    # Remove Markdown-style links: [text](url) → text
    cleaned = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', cleaned)

    return cleaned.strip()


class StatVars:
    # Please don't set it to 0 !
    scrollUpSleepTime = 0.5

    path_processed_file = 'processed.json'
    path_bots_file = 'bot-groups.json'
    path_telegram_config = 'telegram_config.json'

    CONFIG_PARSE_MODE = rapidjson.PM_COMMENTS | rapidjson.PM_TRAILING_COMMAS

    has_been_processed = []
    unique_identifiers = []

    to_be_processed = []

    bot_groups = []
    datetimeFormat = '%Y-%m-%dT%H:%M:%S%z'

    loop_secs = 10

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )
    logger = logging.getLogger(__name__)

    telethon_client = None
    telegram_config = {}

    # Add Telethon session file path
    session_file = 'telegram.session'

    playwright = None
    browser = None
    context = None
    page = None

    # was added to be able to fire an exit event multiple times for multiple exchanges
    # and to not fire the same pair multiple times
    # if the exchange mentions the delisting multiple times, among other things.
    blacklists_exchanges = {}


async def set_playwright():
    logging.info("Starting Playwright browser")

    # Clean up previous instance if it exists
    if hasattr(StatVars, "playwright_instance") and StatVars.playwright_instance:
        logging.info("Closing previous Playwright instance")
        try:
            await StatVars.playwright_instance.stop()
        except Exception as e:
            logging.warning(f"Error while stopping existing Playwright instance: {e}")
        StatVars.playwright_instance = None

    # Start new instance
    StatVars.playwright_instance = await async_playwright().start()
    browser = await StatVars.playwright_instance.chromium.launch(headless=True)
    context = await browser.new_context(
        accept_downloads=False,
        ignore_https_errors=True,
        java_script_enabled=True,
        locale="en-US",
    )
    page = await context.new_page()
    return page


async def init_telethon():
    with open('telegram_config.json') as f:
        StatVars.telegram_config = rapidjson.load(f)

    StatVars.telethon_client = TelegramClient(
        StatVars.session_file,
        StatVars.telegram_config['api_id'],
        StatVars.telegram_config['api_hash'],
        flood_sleep_threshold=10,
        request_retries=3,
        retry_delay=5
    )

    await StatVars.telethon_client.start(phone=StatVars.telegram_config['phone_number'])


def report_to_be_processed():
    for message_dict in StatVars.to_be_processed:
        logging.info(f"caught fresh news for {message_dict['exchange']}: {message_dict['message']}")


# returns pairs for exchange
def get_exchange_pairs(exchange_name):
    sleep_timer_on_error = 60
    while True:
        try:
            exchange_class = getattr(ccxt, exchange_name)
            exchange = exchange_class({
                'timeout': 30000,
                'enableRateLimit': True,
                'rateLimit': 500,  # don't even try to endanger any potential bots by spamming the exchange
            })
            # Get available markets on exchange
            markets = exchange.load_markets()
            if markets:
                logging.info(f"Refreshing pairs for exchange {exchange}, we found {len(markets)} pairs.")
                return markets
            else:
                logging.info(f"No markets available for {exchange_name}. Retrying after {sleep_timer_on_error}s ...")
                time.sleep(sleep_timer_on_error)
        except Exception as e:
            logging.info(f"Error fetching markets for {exchange_name}: {e}. Retrying after {sleep_timer_on_error}s ...")
            time.sleep(sleep_timer_on_error)


def get_unique_identifier(message_dict):
    unique_identifier = (message_dict.get("exchange"), message_dict.get("date"))
    return unique_identifier


def set_unique_identifiers():
    StatVars.unique_identifiers = set(
        (entry["exchange"], entry["date"]) for entry in StatVars.has_been_processed
    )
    pass


class TelegramScraper:
    def __init__(self):
        self.blacklist_exchange_config = None
        self.exchange = ""
        self.channel_username = ""
        self.coin_prefixes = []
        self.coin_suffixes = []
        self.pairs = None
        self.message_limit = 100
        self.offset_id = 0
        self.found_processed = False
        self.sleep_secs_between_queries = 1
        self.sleep_secs_at_error = 60
        self.delist_website = ""
        self.previously_found_messages = 0
        self.pairs = {}

    async def scrape(self, pairs):
        self.blacklist_exchange_config = StatVars.blacklists_exchanges[self.exchange]
        self.pairs = pairs
        telegram_channel = await StatVars.telethon_client.get_entity(self.channel_username)
        telegram_query_count = 0
        while not self.found_processed:
            if len(StatVars.to_be_processed) > 0:
                logging.info(
                    f"Found {len(StatVars.to_be_processed)} messages in {self.channel_username} "
                    f"that weren't registered. Asking for more!")
                time.sleep(self.sleep_secs_between_queries)

            messages = await StatVars.telethon_client.get_messages(
                telegram_channel,
                limit=self.message_limit,
                offset_id=self.offset_id
            )
            telegram_query_count += 1

            if not messages:
                break  # Reached the beginning of the channel

            for message in messages:
                self.offset_id = message.id  # update for next batch

                prepared_message_dict = prepare_message_dict_template(self.exchange, message)
                message_dict = await self.read_message(prepared_message_dict)

                if not message_dict or message_dict['message'] == "":
                    continue

                unique_id = get_unique_identifier(message_dict)

                if unique_id in StatVars.unique_identifiers:
                    self.found_processed = True
                    break
                else:
                    StatVars.to_be_processed.append(message_dict)

        if StatVars.to_be_processed:
            StatVars.has_been_processed.extend(StatVars.to_be_processed)
            report_to_be_processed()
            save_processed()

            new_blacklist = []
            for message_dict in StatVars.to_be_processed:
                if message_dict:
                    new_blacklist.extend(message_dict["blacklisted_pairs"])

            if new_blacklist:
                save_blacklists(self.exchange, new_blacklist)
                send_blacklists()

                # only send messages to the freqtrade bot if there s no historical data grabbed.
                # Outdated info would be bad to be force enter short.
                if telegram_query_count == 1:
                    send_force_exit_long()
                    send_force_enter_short()

        reset_static_variables()
        # except Exception as e:
        #     logging.error(f"Error scraping {self.exchange}: {e}")
        #     time.sleep(self.sleep_secs_at_error)

    def read_messages(self, prev_message_count, first_try=False):
        stop_loop = False

        # try:
        channel_entity = StatVars.telethon_client.get_entity(self.channel_username)

        messages = StatVars.telethon_client.get_messages(
            channel_entity,
            limit=self.message_limit
        )

        len_messages = len(messages)
        if len_messages == 0:
            raise ValueError(f"{self.exchange}: No messages found in channel!")

        message_dict = prepare_message_dict_template(self.exchange, messages[0])
        unique_identifier = (message_dict.get("exchange"), message_dict.get("date"))

        if unique_identifier in StatVars.unique_identifiers:
            if not first_try:
                StatVars.logger.info(
                    f"{self.exchange}: Found already processed message. "
                    f"Stopping further processing.")
            stop_loop = True
        elif len_messages == prev_message_count:
            StatVars.logger.info(
                f"{self.exchange}: Message count unchanged ({prev_message_count}). Stopping.")
            stop_loop = True
        elif self.message_limit == 0:  # Equivalent to initialScrollUpTimes == 0
            stop_loop = True
        else:
            StatVars.logger.info(
                f"{self.exchange}: Processing {len_messages} messages. "
                f"New messages: {len_messages - prev_message_count}")

        return messages, len_messages, stop_loop

    async def read_message(self, prepared_message_dict):
        raise "This method is not initialized in the main class itself, please use it in the derived classes."

    async def read_web_message(self, dict_message):
        raise "This method is not initialized in the main class itself, please use it in the derived classes."

    # This was changed to specifically looking for prefixes since a pair W and T was blacklisted, which would
    # blacklist all pairs ending on a T or W which ... sucks
    def get_blacklisted_coins(self, message_dict: {}):
        exchange_blacklist_config_content = (self.blacklist_exchange_config
        ['file_content']['exchange']['pair_blacklist'])

        modified_message = (message_dict['message'].upper()
                            .replace("and".upper(), " ")
                            .replace("&".upper(), " ")
                            .replace(",", " ")
                            .replace(".", " ")
                            .replace("(", " ")
                            .replace(")", " ")
                            .replace("$", " ")
                            .strip()
                            )

        # make splitting things easier by removing double spaces
        # (not strictly necessary but hey, ease of debugging > all)
        while "  " in modified_message:
            modified_message = modified_message.replace("  ", " ")

        set_title = set(modified_message.strip().split(" "))
        set_title_no_trailing_slash = [word.split('/')[0] for word in set_title]

        # prepare variables
        all_coins = {pair['id'].upper().replace("-", "") for pair in self.pairs.values()}
        all_coins.update({pair['base'].upper() for pair in self.pairs.values()})

        # Use list comprehension to build the set of coins directly
        set_coins = {coin for coin in set_title_no_trailing_slash if coin.upper() in all_coins}

        caught_coins = set(message_dict['blacklisted_pairs'])
        for set_coin in set_coins:
            # Add the coin itself without any prefix or suffix
            pattern_coin_itself = f"{set_coin}/.*"

            # only add it if the coin has never been caught for this exchange before
            if pattern_coin_itself not in exchange_blacklist_config_content:
                caught_coins.add(pattern_coin_itself)
                exchange_blacklist_config_content.append(pattern_coin_itself)

                # Check all combinations of prefixes and suffixes
                for prefix in self.coin_prefixes:
                    for suffix in self.coin_suffixes:
                        # Construct potential coin combinations
                        potential_coin_combo = f"{prefix}{set_coin}{suffix}".upper()
                        potential_coin_prefix = f"{prefix}{set_coin}".upper()
                        potential_coin_suffix = f"{set_coin}{suffix}".upper()

                        # Check if any of these patterns match 'base' values in self.pairs
                        for pair_key, pair_value in self.pairs.items():
                            if 'base' in pair_value:
                                base_value = pair_value['base'].upper()
                                if (fnmatch.fnmatch(base_value, potential_coin_combo) or
                                        fnmatch.fnmatch(base_value, potential_coin_prefix) or
                                        fnmatch.fnmatch(base_value, potential_coin_suffix)):
                                    caught_coins.add(base_value)
        message_dict['blacklisted_pairs'] = list(caught_coins)
        return message_dict


class BinanceScraper(TelegramScraper):
    def __init__(self):
        super().__init__()
        self.exchange = "binance"
        self.channel_username = "binance_announcements"  # Telegram channel username
        self.coin_prefixes = ["000"]
        self.coin_suffixes = ["DOWN", "UP", "BEAR", "BULL"]
        self.delist_website = ""

    async def read_message(self, message_dict):
        if not message_dict:
            return None

        gimme_patterns = []
        delist_patterns = [
            r"delist"
        ]

        reject_patterns = [
            r"introduces",
            r"vote to delist",
            r"binance will delist non-mica",
            r"delisting of binance loans",
            r"suspension of .* deposits",
            r"Notice on the Withdrawals of",
            r"from Cross and Isolated Margin",
            r"Binance Margin & Binance Futures Will Delist BUSD",
            r"Will Support .* Buyback"
            r"Binance Margin"
        ]

        # Debug steps, leaving this in so it s quicker to debug.
        if any(re.search(pattern, message_dict['message'], re.IGNORECASE) for pattern in gimme_patterns):
            pass  # return the message_dict without looking into blacklisted pairs
        # Check for rejection
        elif any(re.search(pattern, message_dict['message'], re.IGNORECASE) for pattern in reject_patterns):
            pass  # return the message_dict without looking into blacklisted pairs
        # Check for delisting
        elif any(re.search(pattern, message_dict['message'], re.IGNORECASE) for pattern in delist_patterns):
            message_dict = self.get_blacklisted_coins(message_dict)

        return message_dict


class KucoinScraper(TelegramScraper):
    def __init__(self):
        super().__init__()
        self.exchange = "kucoin"
        self.channel_username = "Kucoin_News"
        self.coin_prefixes = ["000"]
        self.coin_suffixes = ["2L", "2S", "3L", "3S", "DOWN", "UP"]

    async def read_message(self, message_dict):
        if not message_dict:
            return None

        gimme_patterns = []
        delist_patterns = [
            "delist",
        ]
        web_scraper_patterns = [
            "Delist .*Certain Project",
            # "KuCoin Will Delist Certain Projects",
            "KuCoin .*Delist.* Project"
        ]

        reject_patterns = [
            "perpetual contract",
            "Earn will delist",
            "The Margin Grid",
            "Trading Bot",
            "KuCoin Convert Will",
            "Delisting Optimization",
            "KuCoin Will Delist .* Spot Trading Pairs",
            'Leveraged Tokens',
            "KuCoin Earn",
            "contract",
            "KuCoin Convert",
            "KuCoin Will Launch",
            "Sandbox Mode",
            "Earn Trade",
            "referral program"
        ]

        # first we deny anything without the word "delist" in it
        if not any(re.search(pattern, message_dict['message'], re.IGNORECASE) for pattern in delist_patterns):
            return message_dict

        # then all those contracts etc.,
        # we just want full removals not any fringe /BUSD pairs etc. triggering a blacklist
        if any(re.search(pattern, message_dict['message'], re.IGNORECASE) for pattern in reject_patterns):
            return message_dict  # return the message_dict without looking into blacklisted pairs

        # Debug steps, leaving this in so it s quicker to debug.
        if any(re.search(pattern, message_dict['message'], re.IGNORECASE) for pattern in gimme_patterns):
            pass  # return the message_dict without looking into blacklisted pairs

        if any(re.search(pattern, message_dict['message'], re.IGNORECASE) for pattern in web_scraper_patterns):
            message_dict = await self.read_web_message(message_dict)

        message_dict = self.get_blacklisted_coins(message_dict)
        if len(message_dict['blacklisted_pairs']) == 0:
            logging.info("A news post that stated 'delist' didnt turn up any delisted pair. "
                         "The most likely case is that ccxt does not get that mentioned pair anymore "
                         "and you can't download that pair anymore either.")
        return message_dict

    async def read_web_message(self, message_dict):
        for url in message_dict['linked_urls']:
            if "https://www.kucoin.com/announcement" not in url.lower() and "https://www.kucoin.com/news" not in url.lower():
                continue

            success = False
            for attempt in range(3):  # Try up to 2 times
                try:
                    await StatVars.page.goto(url, timeout=10000)

                    # Sometimes the website is unable to load the actual news.
                    try:
                        await StatVars.page.wait_for_selector(".kucoin-article_oXGwp", state="visible", timeout=10000)
                    except playwright.async_api.Error:
                        logging.warning(f"[Attempt {attempt + 1}] Timeout waiting for selector on {url}")
                        continue  # Try again if this was the first attempt

                    article_text = await StatVars.page.inner_text(".kucoin-article_oXGwp")
                    if article_text:
                        text = remove_markdown_from_text(article_text)
                        text = text.replace('(', ' (').replace(')', ') ')
                        message_dict['message'] += " | " + text
                        success = True
                        break
                    else:
                        logging.warning(f"[Attempt {attempt + 1}] Article content not found at {url}")
                except Exception as e:
                    logging.error(f"[Attempt {attempt + 1}] Error retrieving content from {url}: {e}")
                    time.sleep(1)

            if not success:
                logging.warning(f"Failed to retrieve content from {url} after 2 attempts.")
                return message_dict  # Return after all attempts fail

        return message_dict


class BybitScraper(TelegramScraper):
    def __init__(self):
        super().__init__()
        self.coin_prefixes = ["000"]
        self.coin_suffixes = ["1000", "3L", "3S"]
        self.exchange = "bybit"
        self.channel_username = "Bybit_Announcements"

    def prepare_message_dict(self, message):
        return prepare_message_dict_template(self.exchange, message)

    async def read_message(self, message_dict):
        if message_dict is None:
            return None

        msg = message_dict['message'].upper()
        if any(term in msg for term in ["CONTACT", "PERPETUAL", "MARGIN", "DERIVAT", "CONTRACT"]):
            return message_dict

        if "DELISTING OF" in msg:
            message_dict = self.get_blacklisted_coins(message_dict)

        return message_dict


class OkxScraper(TelegramScraper):
    def __init__(self):
        super().__init__()
        self.coin_prefixes = []
        self.coin_suffixes = []
        self.exchange = "okx"
        self.channel_username = "OKXAnnouncements"

    def prepare_message_dict(self, message):
        return prepare_message_dict_template(self.exchange, message)

    async def read_message(self, message_dict):
        if message_dict is None:
            return None

        msg = message_dict['message'].upper()
        if "CONTACT" in msg or "DERIVATIVE" in msg:
            return message_dict

        if "DELISTING OF" in msg:
            message_dict = self.get_blacklisted_coins(message_dict)

        return message_dict


class GateioScraper(TelegramScraper):
    def __init__(self):
        super().__init__()
        self.coin_suffixes = ["3L", "3S", "5L", "5S", "TOKEN", "PLATFORM"]
        self.exchange = "gateio"
        self.channel_username = "GateioOfficialNews"

    def prepare_message_dict(self, message):
        return prepare_message_dict_template(self.exchange, message)

    async def read_message(self, message_dict):
        if message_dict is None:
            return None

        msg = message_dict['message'].upper()
        if "CONTACT" in msg or "DERIVATIVE" in msg:
            return message_dict

        if "DELIST" in msg:
            message_dict = self.get_blacklisted_coins(message_dict)

        return message_dict

    def get_blacklisted_coins(self, message_dict: {}):
        exchange_blacklist_config_content = (self.blacklist_exchange_config
        ['file_content']['exchange']['pair_blacklist'])

        # Extract words inside parentheses
        matches = re.findall(r'\(([^)]+)\)', message_dict['message'].upper())
        set_title_no_trailing_slash = [match.split('/')[0] for match in matches]

        # prepare variables
        all_coins = {pair['id'].upper().replace("-", "") for pair in self.pairs.values()}
        all_coins.update({pair['base'].upper() for pair in self.pairs.values()})

        # Use list comprehension to build the set of coins directly
        set_coins = {coin for coin in set_title_no_trailing_slash if coin.upper() in all_coins}

        caught_coins = set(message_dict['blacklisted_pairs'])
        for set_coin in set_coins:
            # Add the coin itself without any prefix or suffix
            pattern_coin_itself = f"{set_coin}/.*"

            # only add it if the coin has never been caught for this exchange before
            if pattern_coin_itself not in exchange_blacklist_config_content:
                caught_coins.add(pattern_coin_itself)
                exchange_blacklist_config_content.append(pattern_coin_itself)

                # Check all combinations of prefixes and suffixes
                for prefix in self.coin_prefixes:
                    for suffix in self.coin_suffixes:
                        # Construct potential coin combinations
                        potential_coin_combo = f"{prefix}{set_coin}{suffix}".upper()
                        potential_coin_prefix = f"{prefix}{set_coin}".upper()
                        potential_coin_suffix = f"{set_coin}{suffix}".upper()

                        # Check if any of these patterns match 'base' values in self.pairs
                        for pair_key, pair_value in self.pairs.items():
                            if 'base' in pair_value:
                                base_value = pair_value['base'].upper()
                                if (fnmatch.fnmatch(base_value, potential_coin_combo) or
                                        fnmatch.fnmatch(base_value, potential_coin_prefix) or
                                        fnmatch.fnmatch(base_value, potential_coin_suffix)):
                                    caught_coins.add(base_value)
        message_dict['blacklisted_pairs'] = list(caught_coins)
        return message_dict


class HtxScraper(TelegramScraper):
    def __init__(self):
        super().__init__()
        self.coin_prefixes = []
        self.coin_suffixes = ["1S", "2L", "2S", "3L", "3S", "2X"]
        self.exchange = "htx"
        self.channel_username = "htxglobalofficial"

    def prepare_message_dict(self, message):
        return prepare_message_dict_template(self.exchange, message)

    async def read_message(self, message_dict):
        if message_dict is None:
            return None

        msg = message_dict['message'].upper()
        if "CONTACT" in msg or "DERIVATIVE" in msg:
            return message_dict

        if "DELIST" in msg:
            message_dict = self.get_blacklisted_coins(message_dict)

        return message_dict


def save_blacklists(exchange: str, new_blacklisted_pairs: []):
    for bot_group in StatVars.bot_groups:
        if exchange in bot_group['exchanges']:
            # first we save the overall file
            file_name = bot_group['config_path']
            if os.path.exists(file_name):
                # Read existing data from file
                with open(file_name, 'r') as json_file:
                    data = rapidjson.load(json_file, parse_mode=StatVars.CONFIG_PARSE_MODE)
            else:
                # Create new data structure if file doesn't exist
                data = {
                    "exchange": {
                        "pair_blacklist": []
                    }
                }

            # Add new blacklisted pairs if they are not already present
            for pair in new_blacklisted_pairs:
                if pair not in data["exchange"]["pair_blacklist"]:
                    data["exchange"]["pair_blacklist"].append(pair)
                    bot_group['new_pair_blacklist'].append(pair)

            # Save modified data back to the file
            with open(file_name, 'w') as json_file:
                rapidjson.dump(data, json_file, indent=4)

            # Then we save the exchange-specific blacklist (to avoid doubly adding the same pairs)
            with open(StatVars.blacklists_exchanges[exchange]['file_path'], 'w', encoding='utf-8') as f:
                rapidjson.dump(StatVars.blacklists_exchanges[exchange]['file_content'], f, indent=4)


# Shared prepare_message_dict to reuse across all classes
def prepare_message_dict_template(exchange, message):
    if not message.text:
        return None

    message_content = re.sub(r'[^\x00-\x7F]+', ' ', message.text)
    message_content = re.sub(r'\s+', ' ', message_content).strip()
    message_content = re.sub(r'(?i)(https://)', r' \1', message_content)
    message_content = message_content.replace('"', "'")
    message_content = remove_markdown_from_text(message_content)

    if "www.kucoin.com/announcement/en-st-kucoin-will-delist-certain-projects #Announcement" in message_content:
        pass

    urls = [
        url.strip("()[]<>'\",.*")
        for url in re.findall(r'\bhttps://\S+', message_content, re.IGNORECASE)
    ]

    return {
        "exchange": exchange,
        "date": message.date.strftime(StatVars.datetimeFormat),
        "date_scraped": datetime.now(timezone.utc).strftime(StatVars.datetimeFormat),
        "message": message_content,
        "linked_urls": urls,
        "blacklisted_pairs": [],
    }


def open_processed():
    StatVars.logger.info("Loading local processed file")
    #try:
    set_unique_identifiers()

    # create new processed file
    if not os.path.isfile(StatVars.path_processed_file):
        create_processed_file(StatVars.path_processed_file)

    # Read config from stdin if requested in the options
    with Path(StatVars.path_processed_file).open() if StatVars.path_processed_file != '-' else sys.stdin as file:
        StatVars.has_been_processed = rapidjson.load(file, parse_mode=StatVars.CONFIG_PARSE_MODE)
    StatVars.unique_identifiers = set(
        (entry["exchange"], entry["date"]) for entry in StatVars.has_been_processed)
    #except FileNotFoundError:


#     logging.error(f'Config file "{StatVars.path_processed_file}" not found!'
# ' Please create a config file or check whether it exists.')
#except rapidjson.JSONDecodeError:
#    logging.error('Please verify your configuration file for syntax errors.')


def save_processed():
    StatVars.logger.info("Saving local processed file")
    try:
        set_unique_identifiers()
        sorted_json_obj = rapidjson.dumps(
            sorted(StatVars.has_been_processed, key=lambda x: (x['exchange'], x['date'])), indent=4)
        with open(StatVars.path_processed_file, "w") as outfile:
            outfile.write(sorted_json_obj)

    except Exception as e:
        logging.info(e)


def load_blacklist(config_file):
    StatVars.logger.info("opening local blacklist files")
    try:
        # Read config from stdin if requested in the options
        with Path(config_file).open() if StatVars.path_processed_file != '-' else sys.stdin as file:
            StatVars.has_been_processed = rapidjson.load(file, parse_mode=StatVars.CONFIG_PARSE_MODE)

    except FileNotFoundError:
        logging.error(f'Config file "{StatVars.path_processed_file}" not found!'
                      ' Please create a config file or check whether it exists.')
    except rapidjson.JSONDecodeError:
        logging.error('Please verify your configuration file for syntax errors.')


def load_bots_data():
    if not os.path.isfile(StatVars.path_bots_file):
        create_new_config(StatVars.path_bots_file)

    with Path(StatVars.path_bots_file).open() if StatVars.path_bots_file != '-' else sys.stdin as file:
        bot_groups = rapidjson.load(file, parse_mode=StatVars.CONFIG_PARSE_MODE)
        for bot_group in bot_groups:
            bot_group = add_backtest_json_file_info(bot_group)
            bot_group['new_pair_blacklist'] = []  # add a virtual property for future data handling
            StatVars.bot_groups.append(bot_group)


def add_backtest_json_file_info(bot_group):
    if not os.path.isfile(bot_group['config_path']):
        create_new_config(bot_group['config_path'])

    # Read the JSON file located at line['config_path']
    with open(bot_group['config_path'], 'r') as config_file:
        config_data = rapidjson.load(config_file)
        bot_group['pair_blacklist'] = config_data['exchange']['pair_blacklist']
    return bot_group


# Sends blacklisted pairs if they are not yet in the bots config file
def send_blacklists():
    for bot_group in StatVars.bot_groups:
        if 'new_pair_blacklist' in bot_group:
            for ip in bot_group['ips']:
                try:
                    api_bot = (
                        freqtrade_client.FtRestClient(
                            f"http://{ip}", bot_group['username'], bot_group['password']))
                    api_bot_status = api_bot.status()
                    if isinstance(api_bot_status, list):
                        blacklist_response = api_bot.blacklist()
                        if blacklist_response is None:
                            logging.warning(f"bot http://{ip} did not respond while trying to send the blacklist! "
                                            f"Skipping")
                            continue
                        for pair in bot_group['new_pair_blacklist']:
                            if pair in blacklist_response['blacklist']:
                                logging.info(f"bot http://{ip}: Skipped sending the blacklist pair  {pair} "
                                             f"Reason: pair exists already")
                            else:
                                result = api_bot.blacklist(pair)
                                if 'error' in result:
                                    logging.error(f"bot http://{ip}: Attempted to send a blacklist pair and failed "
                                                  f"Error: {result['result']}")
                                else:
                                    logging.info(f"bot http://{ip}: Successfully sent the pair {pair} to the blacklist")
                    else:
                        logging.warning(f"bot http://{ip}: connection failed. Skipping to send send_blacklists!")

                except Exception as ex:
                    logging.error(f"An error occurred: {ex}")


def send_force_enter_short():
    for bot_group in StatVars.bot_groups:
        if bot_group['force_enter_short']:
            if 'new_pair_blacklist' in bot_group:
                for ip in bot_group['ips']:
                    api_bot = (
                        freqtrade_client.FtRestClient(
                            f"http://{ip}", bot_group['username'], bot_group['password']))
                    api_bot_status = api_bot.status()
                    if isinstance(api_bot_status, list):
                        for pair in bot_group['new_pair_blacklist']:
                            result = api_bot.forceenter(pair, 'short')
                            if 'error' in result:
                                logging.error(f"bot http://{ip}: Attempted to force enter a short trade of {pair}"
                                              f" and failed. Error: {result['result']}")
                            else:
                                logging.info(f"bot http://{ip}: Successfully sent a force enter short order "
                                             f"of the pair {pair}")
                    else:
                        logging.warning(f"bot http://{ip}: connection failed. Skipping to send send_force_enter_short!")


def send_force_exit_long():
    for bot_group in StatVars.bot_groups:
        if bot_group['force_exit_long']:
            if 'new_pair_blacklist' in bot_group:
                for ip in bot_group['ips']:
                    api_bot = (
                        freqtrade_client.FtRestClient(
                            f"http://{ip}", bot_group['username'], bot_group['password']))
                    open_trades = api_bot.status()
                    if isinstance(open_trades, list):
                        for pair in bot_group['new_pair_blacklist']:
                            for open_trade in open_trades:
                                if pair == open_trade['pair']:
                                    if not open_trade['is_short']:  # only exit long, not short
                                        result = api_bot.forceexit(open_trade['trade_id'])
                                        if 'error' in result:
                                            logging.error(f"bot http://{ip}: Attempted to force exit a long trade "
                                                          f"of {pair} and failed. Error: {result['result']}")
                                        else:
                                            logging.info(f"bot http://{ip}: Successfully sent a force-exit-long order "
                                                         f"of the pair {pair}")
                    else:
                        logging.warning(f"bot http://{ip}: connection failed. Skipping to send_force_exit_long!")


# This checks all bots' connections ... just for the user as a sanity check
def check_all_bots():
    logging.info("checking all bot-connections:")
    for bot_group in StatVars.bot_groups:
        for ip in bot_group['ips']:
            api_bot = (freqtrade_client.FtRestClient(
                f"http://{ip}", bot_group['username'], bot_group['password']))
            response = api_bot.status()
            if isinstance(response, list):
                logging.info(f"bot http://{ip}: connection successful!")
            else:
                logging.warning(f"bot http://{ip}: connection failed?!")


def reset_static_variables():
    StatVars.to_be_processed = []
    for bot_group in StatVars.bot_groups:
        bot_group['new_pair_blacklist'] = []


def get_exchanges_from_bot_groups():
    # get exchanges from bot groups
    exchanges_list = [[exchange.lower() for exchange in entry["exchanges"]] for entry in StatVars.bot_groups]
    # flatten
    exchanges = [exchange for sublist in exchanges_list for exchange in sublist]
    # make unique
    exchanges = list(set(exchanges))
    return exchanges


def refresh_ccxt_exchange_pairs(exchanges_pairs):
    with ThreadPoolExecutor() as executor:
        futures = {executor.submit(get_exchange_pairs, exchange): exchange for exchange in exchanges_pairs.keys()}
        for future in concurrent.futures.as_completed(futures):
            exchange = futures[future]
            exchanges_pairs[exchange] = future.result()


async def handle_exception(ex1):
    #try:
    if StatVars.context:
        StatVars.context.close()
    if StatVars.browser:
        StatVars.browser.close()
    if StatVars.playwright:
        StatVars.playwright.stop()
    #except Exception as ex3:
    #    logging.error(f"an error occurred during cleanup: {ex3}")

    #try:
    await set_playwright()
    #except Exception as ex2:
    #    logging.error(f"an error occurred during playwright setup: {ex2}")

    logging.error(f"An error occurred: {ex1}")
    time.sleep(30)


def create_new_config(config_path: str):
    config = {
        "exchange": {
            "pair_blacklist": []
        }
    }
    with open(config_path, "w") as f:
        rapidjson.dump(config, f, indent=4)


def create_processed_file(config_path: str):
    with open(config_path, "w") as f:
        rapidjson.dump([], f, indent=4)


def load_data_of_blacklists_exchanges():
    for exchange_name, data in StatVars.blacklists_exchanges.items():
        path = data["file_path"]
        if os.path.exists(path):
            with open(path, "r") as f:
                data["file_content"] = rapidjson.load(f)
        else:
            data["file_content"] = {
                "exchange": {
                    "pair_blacklist": []
                }
            }


async def main():
    os.nice(15)
    open_processed()
    load_bots_data()
    await init_telethon()

    exchanges_to_loop_through = get_exchanges_from_bot_groups()
    check_all_bots()

    heartbeat_time_pairs = datetime.min
    heartbeat_time = datetime.min

    exchanges = {
        'binance': BinanceScraper(),
        'kucoin': KucoinScraper(),
        'bybit': BybitScraper(),
        'okx': OkxScraper(),
        'gateio': GateioScraper(),
        'htx': HtxScraper()
    }
    exchanges_pairs = {exchange: {} for exchange in exchanges}  # Initialize as empty dictionaries
    StatVars.blacklists_exchanges = {
        exchange_name: {
            "file_path": f"./config_{exchange_name}.json",
            "file_content": None
        }
        for exchange_name in exchanges
    }
    load_data_of_blacklists_exchanges()

    # Initial refresh at startup
    await set_playwright()

    heartbeat_time_pairs = datetime.now()

    # Optional: delay startup if it's right on a 5-minute boundary
    if heartbeat_time_pairs.minute % 5 == 0:
        logging.info("Startup time is divisible by 5 minutes, sleeping for 60s to avoid query weight issues...")
        await asyncio.sleep(60)
    await asyncio.to_thread(refresh_ccxt_exchange_pairs, exchanges_pairs)

    while True:
        StatVars.blacklist_changed = False
        now = datetime.now()

        # 24h heartbeat, avoid refreshing on /5-minute mark
        if now - heartbeat_time_pairs >= timedelta(hours=24):
            if now.minute % 5 != 0:
                await asyncio.to_thread(refresh_ccxt_exchange_pairs, exchanges_pairs)
                heartbeat_time_pairs = now

                await set_playwright()
            else:
                logging.info("24h refresh skipped to avoid 5-minute divisible minute. Will retry next iteration.")

        # All exchanges empty → wait + refresh
        if all(not exchange_pairs for exchange_pairs in exchanges_pairs.values()):
            logging.info(f"All pairs empty. Waiting 60s. Current minute: {now.minute}")
            time.sleep(60)
            await asyncio.to_thread(refresh_ccxt_exchange_pairs, exchanges_pairs)
            heartbeat_time_pairs = datetime.now()

        if datetime.now() - heartbeat_time_pairs >= timedelta(hours=24):
            heartbeat_time_pairs = datetime.now()
        #try:
        start_time = time.monotonic()

        # Run all scrapers sequentially
        for exchange_name, scraper in exchanges.items():
            if exchange_name.lower() in exchanges_to_loop_through:
                await scraper.scrape(exchanges_pairs[exchange_name])

        if datetime.now() - heartbeat_time >= timedelta(minutes=15):
            logging.info("delist-scraper heartbeat")
            heartbeat_time = datetime.now()

        time_to_sleep_left = StatVars.loop_secs - ((time.monotonic() - start_time) % StatVars.loop_secs)
        logging.debug(f"for this loop we still have to wait for {time_to_sleep_left} seconds")

        await asyncio.sleep(StatVars.loop_secs - ((time.monotonic() - start_time) % StatVars.loop_secs))

        #except Exception as ex:
        #    logging.error(f"An error occurred: {ex}")
        #    time.sleep(30)

        gc.collect()


if __name__ == "__main__":
    asyncio.run(main())
