import gc
import logging
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ccxt
import freqtrade_client
import rapidjson
# import telethon
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from telethon.sync import TelegramClient  # Sync-compatible client


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

    loop_secs = 30

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )
    logger = logging.getLogger(__name__)

    telethon_client = None
    telegram_config = {}

    # Add Telethon session file path
    session_file = 'telegram.session'

    driver = None


def set_driver():
    logging.info("starting driver for browser")
    # Set up Firefox options
    options = webdriver.FirefoxOptions()
    options.add_argument("--headless")
    # options.add_argument("--no-sandbox")
    # options.add_argument("--disable-dev-shm-usage")
    options.set_preference("intl.accept_languages", "en")
    options.set_preference("permissions.default.image", 2)  # Disable loading images
    # options.add_argument("--single-process")
    options.add_argument("--disable-crash-reporter")
    options.add_argument("--disable-infobars")

    # Specify the path to the manually installed geckodriver
    geckodriver_path = "/usr/local/bin/geckodriver"
    service = FirefoxService(executable_path=geckodriver_path)

    # Initialize Firefox WebDriver with the specified options and service
    # logging.info("Initializing Firefox WebDriver")
    driver = webdriver.Firefox(service=service, options=options)

    # Set timeouts
    driver.set_page_load_timeout(120)  # Set the page load timeout to 60 seconds
    driver.implicitly_wait(120)  # Set the implicit wait timeout to 60 seconds

    # logging.info("Firefox WebDriver initialized successfully")
    return driver


def init_telethon():
    """Initialize Telethon client"""
    #try:
    # Load Telethon config
    with open('telegram_config.json') as f:
        StatVars.telegram_config = rapidjson.load(f)

    StatVars.telethon_client = TelegramClient(
        StatVars.session_file,  # Use your session_file variable
        StatVars.telegram_config['api_id'],
        StatVars.telegram_config['api_hash'],
        # the code is sync to not run into api limits, but hey ... better safe than sorry
        flood_sleep_threshold=10,
        request_retries=3,
        retry_delay=5
    )

    StatVars.telethon_client.start(phone=StatVars.telegram_config['phone_number'])

    #except Exception as e:
    #    logging.error(f"Failed to initialize Telethon: {e}")
    #    raise


def report_to_be_processed():
    for message_dict in StatVars.to_be_processed:
        logging.info(f"caught fresh news for {message_dict['exchange']}: {message_dict['message']}")


# returns pairs for exchange
def get_exchange_pairs(exchange_name):
    sleep_timer_on_error = 60
    while True:
        #try:
        exchange_class = getattr(ccxt, exchange_name)
        exchange = exchange_class({
            'timeout': 30000,
            'enableRateLimit': True,
            'rateLimit': 500,  # don't even try to endanger any potential bots by spamming the exchange
        })
        # Get available markets on exchange
        markets = exchange.load_markets()

        #if any("GFT" in key.upper() for key in markets.keys()):
        #    print("GFT is present in the market pairs.")
        #else:
        #    print("GFT is not present in the market pairs.")

        if markets:
            logging.info(f"We found {len(markets)} pairs for the Exchange {exchange}.")
            return markets
        else:
            logging.info(f"No markets available for {exchange_name}. Retrying after {sleep_timer_on_error}s ...")
            time.sleep(sleep_timer_on_error)
        #except Exception as e:
        #    logging.info(f"Error fetching markets for {exchange_name}: {e}. Retrying after {sleep_timer_on_error}s ...")


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

    def scrape(self):
        # try:
        telegram_channel = StatVars.telethon_client.get_entity(self.channel_username)
        while not self.found_processed:
            if len(StatVars.to_be_processed) > 0:
                logging.info(f"Found {len(StatVars.to_be_processed)} messages in {self.channel_username} "
                             f"that weren't registered. Asking for more!")
                time.sleep(
                    self.sleep_secs_between_queries)  # 1 sec sleep to not hit api limits, better safe than sorry.
            messages = StatVars.telethon_client.get_messages(
                telegram_channel,
                limit=self.message_limit,
                offset_id=self.offset_id
            )

            if not messages:
                break  # Reached the beginning of the channel

            for message in messages:
                self.offset_id = message.id  # update for next batch

                prepared_message_dict = prepare_message_dict_template(self.exchange, message)
                message_dict = self.read_message(prepared_message_dict)

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
                save_blacklist(self.exchange, new_blacklist)
                send_blacklists()
                if len(messages) == self.message_limit:
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

    def read_message(self, prepared_message_dict):
        raise "This method is not initialized in the main class itself, please use it in the derived classes."

    def read_web_message(self, dict_message):
        raise "This method is not initialized in the main class itself, please use it in the derived classes."


class BinanceScraper(TelegramScraper):
    def __init__(self):
        super().__init__()
        self.exchange = "binance"
        self.channel_username = "binance_announcements"  # Telegram channel username
        self.coin_prefixes = ["000"]
        self.coin_suffixes = ["DOWN", "UP", "BEAR", "BULL"]
        self.delist_website = ""

    def read_message(self, message_dict):
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
            r"Binance Margin & Binance Futures Will Delist BUSD"
        ]

        # Debug steps, leaving this in so it s quicker to debug.
        if any(re.search(pattern, message_dict['message'], re.IGNORECASE) for pattern in gimme_patterns):
            pass  # return the message_dict without looking into blacklisted pairs
        # Check for rejection
        elif any(re.search(pattern, message_dict['message'], re.IGNORECASE) for pattern in reject_patterns):
            pass  # return the message_dict without looking into blacklisted pairs
        # Check for delisting
        elif any(re.search(pattern, message_dict['message'], re.IGNORECASE) for pattern in delist_patterns):
            arr_coins = self.read_message_text(message_dict)
            if arr_coins:
                message_dict['blacklisted_pairs'].extend(arr_coins)
                StatVars.logger.info(f"Found delisting announcement for {self.exchange}: {arr_coins}")

        return message_dict

    def read_message_text(self, message_dict):

        patterns = [
            re.compile(r"^binance will delist\s+([A-Z0-9,\sand]+?)\s+(on|\(|by)", re.IGNORECASE),
            # You can add more patterns here if needed
            # re.compile(r"...")
        ]

        for pattern in patterns:
            match = pattern.search(message_dict['message'])
            if match:
                raw_list = match.group(1)
                # Split by comma or 'and' with ignorecase
                delisted_coins = [coin.strip() for coin in re.split(r",|\band\b", raw_list, flags=re.IGNORECASE)]
                logging.info(f"Delisted coins extracted: {delisted_coins}")
                return set(delisted_coins)

        return set()


class KucoinScraper(TelegramScraper):
    def __init__(self):
        super().__init__()
        self.exchange = "kucoin"
        self.channel_username = "Kucoin_News"
        self.coin_prefixes = ["000"]
        self.coin_suffixes = ["2L", "2S", "3L", "3S", "DOWN", "UP"]
        StatVars.driver = set_driver()

    def read_message(self, message_dict):
        if not message_dict:
            return None

        gimme_patterns = []
        delist_patterns = [
            "delist",
        ]
        web_scraper_patterns = [
            "Delist .*Certain Project",
            # "KuCoin Will Delist Certain Projects",
            "KuCoin .*Delist.* Projects"
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
            "Sandbox mode"
        ]

        # first we deny anything without the word "delist" in it
        if "delist" not in message_dict['message']:
            return message_dict

        # then all those contracts etc, we just want full removals not any fringe /BUSD pairs etc triggering a blacklist
        if any(re.search(pattern, message_dict['message'], re.IGNORECASE) for pattern in reject_patterns):
            return message_dict  # return the message_dict without looking into blacklisted pairs

        # Debug steps, leaving this in so it s quicker to debug.
        if any(re.search(pattern, message_dict['message'], re.IGNORECASE) for pattern in gimme_patterns):
            pass  # return the message_dict without looking into blacklisted pairs

        if any(re.search(pattern, message_dict['message'], re.IGNORECASE) for pattern in web_scraper_patterns):
            message_dict = self.read_web_message(message_dict)

        if any(re.search(pattern, message_dict['message'], re.IGNORECASE) for pattern in delist_patterns):
            message_dict = self.read_message_text(message_dict)

        if len(message_dict['blacklisted_pairs']) == 0:
            logging.info("Uh oh we didnt find any pairs with the scraping? Time to debug what's going on !!")
        return message_dict

    def read_message_text(self, message_dict):
        patterns = [
            re.compile(r'\(([A-Z0-9]+)\)\s+Token', re.IGNORECASE),  # original
            re.compile(r'delist\s+the\s+([A-Z0-9]+)\s+Project', re.IGNORECASE),  # new pattern
        ]
        found_token = False
        tokens_found_here = []
        for pattern in patterns:
            match = pattern.search(message_dict['message'])
            if match:
                found_token = True
                token = match.group(1).upper()

                message_dict['blacklisted_pairs'].append(token)
                tokens_found_here.append(token)
        if found_token:
            logging.info(f"read_message_text: Delisted token: {tokens_found_here} "
                         f"from text {message_dict['message']}")
        return message_dict

    def read_web_message(self, message_dict):
        for url in message_dict['linked_urls']:
            if "202306009" in url:
                pass
            if "https://www.kucoin.com/announcement" not in url.lower() and "https://www.kucoin.com/news" not in url.lower():
                continue

            StatVars.driver.get(url)

            try:
                # Wait until the div is there ... or 10s
                WebDriverWait(StatVars.driver, 10).until(
                    EC.presence_of_element_located((By.CLASS_NAME, "kucoin-article_oXGwp"))
                )

                # Additional wait until the article's text content is non-empty ... or 10s
                WebDriverWait(StatVars.driver, 10).until(
                    lambda driver: driver.find_element(By.CLASS_NAME, "kucoin-article_oXGwp").text.strip() != ""
                )
                time.sleep(1)  # additional time waiting since STILL it wont sometimes work properly ...
            except Exception as e:
                logging.warning(f"Timeout waiting for article content at {url}: {e}")
                continue

            # Parse the page source with BeautifulSoup
            html_source = StatVars.driver.page_source
            soup = BeautifulSoup(html_source, "html.parser")

            article_div = soup.find("div", class_="kucoin-article_oXGwp")
            if article_div:
                article_text = article_div.get_text()
                text = remove_markdown_from_text(article_text)
                if text:
                    message_dict['message'] += f" || {url}: {text}"

                    # Extract coin symbols enclosed in parentheses, excluding certain terms
                    arr_coins = re.findall(r'\(\s*(?!UTC|GMT|Twitter)([A-Z0-9]+)\s*\)', text, flags=re.IGNORECASE)

                    message_dict['blacklisted_pairs'].extend(arr_coins)
            else:
                logging.warning(f"Article content not found at {url}")
        if len(message_dict['blacklisted_pairs']) == 0:
            pass
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

    def read_message(self, message_dict):
        if message_dict is None:
            return None

        msg = message_dict['message'].upper()
        if any(term in msg for term in ["CONTACT", "PERPETUAL", "MARGIN", "DERIVAT", "CONTRACT"]):
            return message_dict

        if "DELISTING OF" in msg:
            arr_coins = self.read_message_text(message_dict)
            if arr_coins:
                message_dict['blacklisted_pairs'].extend(arr_coins)

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

    def read_message(self, message_dict):
        if message_dict is None:
            return None

        msg = message_dict['message'].upper()
        if "CONTACT" in msg or "DERIVATIVE" in msg:
            return message_dict

        if "DELISTING OF" in msg:
            arr_coins = self.read_message_text(message_dict)
            if arr_coins:
                message_dict['blacklisted_pairs'].extend(arr_coins)

        return message_dict


class GateioScraper(TelegramScraper):
    def __init__(self):
        super().__init__()
        self.coin_suffixes = ["3L", "3S", "5L", "5S", "TOKEN", "PLATFORM"]
        self.exchange = "gateio"
        self.channel_username = "GateioOfficialNews"

    def prepare_message_dict(self, message):
        return prepare_message_dict_template(self.exchange, message)

    def read_message(self, message_dict):
        if message_dict is None:
            return None

        msg = message_dict['message'].upper()
        if "CONTACT" in msg or "DERIVATIVE" in msg:
            return message_dict

        if "DELIST" in msg:
            arr_coins = self.read_message_text(message_dict)
            if arr_coins:
                message_dict['blacklisted_pairs'].extend(arr_coins)

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

    def read_message(self, message_dict):
        if message_dict is None:
            return None

        msg = message_dict['message'].upper()
        if "CONTACT" in msg or "DERIVATIVE" in msg:
            return message_dict

        if "DELIST" in msg:
            arr_coins = self.read_message_text(message_dict)
            if arr_coins:
                message_dict['blacklisted_pairs'].extend(arr_coins)

        return message_dict


def save_blacklist(exchange: str, new_blacklisted_pairs: []):
    for bot_group in StatVars.bot_groups:
        if exchange in bot_group['exchanges']:
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
    with Path(StatVars.path_bots_file).open() if StatVars.path_bots_file != '-' else sys.stdin as file:
        bot_groups = rapidjson.load(file, parse_mode=StatVars.CONFIG_PARSE_MODE)
        for bot_group in bot_groups:
            bot_group = add_backtest_json_file_info(bot_group)
            bot_group['new_pair_blacklist'] = []  # add a virtual property for future data handling
            StatVars.bot_groups.append(bot_group)


def add_backtest_json_file_info(bot_group):
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


def handle_exception(ex1):
    #try:
    StatVars.driver.quit()
    #except Exception as ex3:
    #    logging.error(f"an error occurred: {ex3}")
    #try:
    # StatVars.driver = set_driver()
    #    pass
    #except Exception as ex2:
    #    logging.error(f"an error occurred: {ex2}")
    #logging.error(f"An error occurred: {ex1}")
    time.sleep(30)  # an error happened, could be anything ... even being rate limited ... Take a nap bot!


def main():
    get_exchange_pairs("kucoin")
    os.nice(15)
    open_processed()
    load_bots_data()
    init_telethon()

    exchanges_to_loop_through = get_exchanges_from_bot_groups()
    check_all_bots()

    heartbeat_time_pairs = datetime.now()
    heartbeat_time = datetime.min.now()

    scrapers = {
        'binance': BinanceScraper(),
        'kucoin': KucoinScraper(),
        'bybit': BybitScraper(),
        'okx': OkxScraper(),
        'gateio': GateioScraper(),
        'htx': HtxScraper()
    }

    while True:
        #try:
        if datetime.now() - heartbeat_time_pairs >= timedelta(hours=24):
            heartbeat_time_pairs = datetime.now()

        start_time = time.monotonic()

        # Run all scrapers sequentially
        for exchange_name, scraper in scrapers.items():
            if exchange_name.lower() in exchanges_to_loop_through:
                scraper.scrape()

        if datetime.now() - heartbeat_time >= timedelta(minutes=15):
            logging.info("delist-scraper heartbeat")
            heartbeat_time = datetime.now()

        time_to_sleep_left = StatVars.loop_secs - ((time.monotonic() - start_time) % StatVars.loop_secs)
        logging.debug(f"for this loop we still have to wait for {time_to_sleep_left} seconds")

        time.sleep(StatVars.loop_secs - ((time.monotonic() - start_time) % StatVars.loop_secs))

        #except Exception as ex:
        #    logging.error(f"An error occurred: {ex}")
        #    time.sleep(30)

        gc.collect()


if __name__ == "__main__":
    main()
