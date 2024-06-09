import concurrent.futures
import copy
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ccxt
import freqtrade_client
import rapidjson
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from tqdm import tqdm


class StatVars:
    # Tries to scroll up multiple times to make the parsing of the data less often and thereby speed up drastically.
    driver = None

    # Please don't set it to 0 !
    scrollUpSleepTime = 0.5

    path_processed_file = 'processed.json'
    path_bots_file = 'bot-groups.json'
    CONFIG_PARSE_MODE = rapidjson.PM_COMMENTS | rapidjson.PM_TRAILING_COMMAS

    has_been_processed = []
    has_been_processed_without_date_scraped = []
    to_be_processed = []

    bot_groups = []
    datetimeFormat = '%Y-%m-%dT%H:%M:%S%z'

    loop_secs = 30

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )
    logger = logging.getLogger(__name__)

    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--remote-debugging-pipe")
    options.add_argument("--accept-lang=en")

    # Disable loading images
    prefs = {"profile.managed_default_content_settings.images": 2}
    options.add_experimental_option("prefs", prefs)

    # driver_subUrl = webdriver.Chrome(options=options)


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
                print(f"No markets available for {exchange_name}. Retrying after {sleep_timer_on_error}s ...")
                time.sleep(sleep_timer_on_error)
        except Exception as e:
            print(f"Error fetching markets for {exchange_name}: {e}. Retrying after {sleep_timer_on_error}s ...")


class BinanceScraper:
    exchange = "binance"

    url = "https://t.me/s/binance_announcements"

    initialScrollUpTimes = 100
    initialWaitSeconds = 0

    message_bubble = "tgme_widget_message_wrap"
    message_text = ["tgme_widget_message_text"]
    message_date = {"type": "a",
                    "class": "tgme_widget_message_date",
                    "format": "%Y-%m-%dT%H:%M:%S%z"}
    pairs = None

    def scrape(self, pairs):
        self.pairs = pairs
        StatVars.driver.get(self.url)
        time.sleep(self.initialWaitSeconds)

        for_loops_count = 0
        prev_message_count = 0
        # scan once without scrolling to have the loop faster if we just need to scrape the first 20 ish messages
        messages, prev_message_count, stop_loop = self.read_messages(StatVars.driver, prev_message_count, True)
        current_scroll_up_times = self.initialScrollUpTimes
        if self.initialScrollUpTimes > 0:
            while not stop_loop:
                # scrolling several times to make the overall loop faster, uses tqdm for a progression bar
                for _ in tqdm(range(current_scroll_up_times), desc="Scrolling up to fetch more news", unit="scroll"):
                    StatVars.driver.execute_script("window.scrollTo(0, 0);")
                    time.sleep(StatVars.scrollUpSleepTime)
                    for_loops_count += 1
                messages, prev_message_count, stop_loop = self.read_messages(StatVars.driver, prev_message_count)
                # stop_loop = True  # enable for quicker debugging, so it only scrolls for one rotation

        # now fill the message_html
        for message_html in messages[::-1]:
            prepared_message_dict = self.prepare_message_dict(message_html)
            message_dict = self.read_message(prepared_message_dict)
            message_dict_without_date_scraped = copy.deepcopy(message_dict)
            message_dict_without_date_scraped.pop("date_scraped", None)

            if message_dict['message'] == "":
                continue
            elif message_dict_without_date_scraped in StatVars.has_been_processed_without_date_scraped:
                # logging.info(f"message already exists for exchange {message_dict['exchange']}: "
                #              f"{message_dict['message']}")
                break
            else:
                StatVars.to_be_processed.append(message_dict)

        if len(StatVars.to_be_processed) > 0:
            StatVars.has_been_processed.extend(StatVars.to_be_processed)
            report_to_be_processed()
            save_processed()

        # make one big list of newly delisted pairs
        new_blacklist = []
        for message_dict in StatVars.to_be_processed:
            if message_dict is None:
                continue
            new_blacklist.extend(message_dict["blacklisted_pairs"])

        if len(new_blacklist) > 0:
            save_blacklist(self.exchange, new_blacklist)
            send_blacklists()
            # only do this if the bot didn't initially gather (or: just react on fresh news)
            if for_loops_count == 0:
                send_force_exit_long()
                send_force_enter_short()
        reset_static_variables()

    def read_messages(self, read_messages_driver, prev_message_count, first_try=False):
        stop_loop = False
        html_source = read_messages_driver.page_source
        soup = BeautifulSoup(html_source, "html.parser")
        messages = soup.find_all("div", class_=self.message_bubble)

        len_messages = len(messages)
        if len_messages == 0:
            StatVars.logger.warning(f"{self.exchange}: we didn't find any messages!? "
                                    f"Aborting for this loop... "
                                    f"(if this doesnt happen multiple times in a row then you can ignore this message)")
            raise ValueError(f"No messages found for {self.exchange}")
            #return messages, len_messages, True  # we didn't find any messages, well let s just exit...

        message_dict = self.prepare_message_dict(messages[0])
        message_dict_without_date_scraped = copy.deepcopy(message_dict)
        message_dict_without_date_scraped.pop("date_scraped", None)

        if message_dict_without_date_scraped in StatVars.has_been_processed_without_date_scraped:
            if not first_try:
                StatVars.logger.info(
                    f"{self.exchange}: We found a message that has already been scraped. "
                    f"Stopping to get additional news!")
            stop_loop = True
        elif len_messages == prev_message_count:
            StatVars.logger.info(f"{self.exchange}: We found {prev_message_count} messages overall! "
                                 f"The count didn't increase. "
                                 f"Stopping...")
            stop_loop = True
        elif self.initialScrollUpTimes == 0:
            pass
        else:
            StatVars.logger.info(
                f"{self.exchange}: Count of additional messages fetched in this loop: "
                f"{len_messages - prev_message_count}, now: {len_messages}. Continuing")

        return messages, len_messages, stop_loop

    def read_message(self, message_dict):
        if message_dict is None:
            return None

        delist_string = "BINANCE WILL DELIST "
        if "Binance Will Delist All ".upper() in message_dict['message'].upper():
            delist_string = "Binance Will Delist All "
        if "Binance Will Delist StableUSD".upper() in message_dict['message'].upper():
            pass
        elif "Binance Will Delist All FTX Leveraged Tokens".upper() in message_dict['message'].upper():
            pass
        elif "Binance Will Delist FTT Margin Pairs".upper() in message_dict['message'].upper():
            pass
        elif "DERIVATIVE".upper() in message_dict['message'].upper():
            pass
        elif delist_string.upper() in message_dict['message'].upper():
            arr_coins = self.get_blacklisted_coins(message_dict['message'])
            if arr_coins is not []:
                message_dict['blacklisted_pairs'].extend(arr_coins)

        return message_dict

    def get_blacklisted_coins(self, title: str):
        my_title = (title.upper()
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
        while "  " in my_title:
            my_title = my_title.replace("  ", " ")

        set_title = set(my_title.strip().split(" "))
        set_title_no_trailing_slash = [word.split('/')[0] for word in set_title]

        # prepare variables
        all_coins = {pair['id'].upper().replace("-", "") for pair in self.pairs.values()}
        all_coins.update({pair['base'].upper() for pair in self.pairs.values()})

        # Use list comprehension to build the set of coins directly
        set_coins = {coin for coin in set_title_no_trailing_slash if coin.upper() in all_coins}

        if len(set_coins) == 0:
            # report any news that did not contain a pair to be blacklisted
            logging.info(f"did not find any of those strings: {my_title}, "
                         f"maybe it wasn't a coin but a currency or it s not a coin that was on the exchange directly")

        # now we add wildcards before and after the coin itself since we assume the ban is exchange wide
        coins_with_wildcards = []
        for coin in set_coins:
            coins_with_wildcards.append(f".*{coin}/.*")

        return coins_with_wildcards

    def prepare_message_dict(self, message_html):
        message_text_elements = []
        for div in self.message_text:
            tag = message_html.find("div", class_=div)
            if tag is not None:
                message_text_elements.append(tag.text.strip())

        if len(message_text_elements) > 0:
            stripped_message = " _-_ ".join(message_text_elements)
        else:
            stripped_message = ""

        # Remove non-printable characters and multiple whitespaces
        message_content = re.sub(r'[^\x00-\x7F]+', ' ', stripped_message)
        message_content = re.sub(r'\s+', ' ', message_content)
        message_content = re.sub(r'(?i)(https://)', r' \1', message_content)

        # Replace double quotes with single quotes to not have to have \" in the strings and keep the quotes
        message_content = message_content.replace('"', "'")

        msg_datetime = self.extract_datetime(message_html)

        urls = re.findall(r'\bhttps://\S+', message_content, re.IGNORECASE)

        message_dict = {
            "exchange": self.exchange,
            "date": msg_datetime.strftime(StatVars.datetimeFormat),
            "date_scraped": datetime.now(timezone.utc).strftime(StatVars.datetimeFormat),
            "message": message_content,
            "linked_urls": urls,
            # to be filled in read_message, not to be saved into the bots file
            # - just in the blacklist.json file of the bot
            "blacklisted_pairs": [],
        }

        return message_dict

    def extract_datetime(self, message_html):
        if self.message_date['format'] == "":
            return datetime(1970, 1, 1)  # Return a default datetime object
        datetime_html = message_html.find(self.message_date['type'], class_=self.message_date['class'])
        msg_datetime = datetime.strptime(datetime_html.contents[0].attrs['datetime'], self.message_date['format'])
        return msg_datetime


class KucoinScraper(BinanceScraper):
    def __init__(self):
        super().__init__()

    exchange = "kucoin"
    url = "https://t.me/s/Kucoin_News"

    def read_message(self, message_dict):
        if message_dict is None:
            return None

        if "DAILY REPORT".upper() in message_dict['message'].upper():
            pass
        elif "KuCoin Will Delist the Sandbox Mode".upper() in message_dict['message'].upper():
            pass
        elif "DERIVATIVE".upper() in message_dict['message'].upper():
            pass
        elif "KuCoin Will Delist Certain Projects".upper() in message_dict['message'].upper():
            # found an indirect reference of pairs, searching...
            found_subpage_coins = self.read_message_of_news(message_dict['linked_urls'])
            arr_coins = self.get_blacklisted_coins(found_subpage_coins)
            message_dict['blacklisted_pairs'].extend(arr_coins)
        elif (
                "KUCOIN WILL DELIST THE".upper() in message_dict['message'].upper() or
                "WILL BE REMOVED FROM THE EXCHANGE".upper() in message_dict['message'].upper() or
                "RISK ANNOUNCEMENT".upper() in message_dict['message'].upper() or
                "WILL BE DELISTED FROM KUCOIN".upper() in message_dict['message'].upper()):
            arr_coins = self.get_blacklisted_coins(message_dict['message'])
            message_dict['blacklisted_pairs'].extend(arr_coins)
        return message_dict

    def read_message_of_news(self, urls):
        found_messages = []
        for url in urls:
            # If another website is stated here, then skip it. In the end we don't want to risk false positives
            if "https://www.kucoin.com/announcement" not in url:
                continue
            own_driver = webdriver.Chrome(options=StatVars.options)
            own_driver.get(url.split('#')[0])
            html_source = own_driver.page_source
            soup = BeautifulSoup(html_source, "html.parser")
            articles = soup.find_all("div")

            # we already know that the pairs names are surrounded by ( and )
            # so we just have to find those words and remove ( and )

            collecting = False
            for article in articles:
                paragraphs = article.find_all("p")
                for paragraph in paragraphs:
                    txt = paragraph.get_text(separator=" ", strip=True)
                    if txt == '':
                        pass
                    elif re.match(r'^\d+\.', txt):  # Paragraph starts with a number followed by a period
                        collecting = True
                        hits = re.findall(r'\(\w+\)', txt)
                        found_messages.extend(hit.strip('()') for hit in hits)
                    elif collecting:
                        if not re.match(r'^\d+\.', txt):  # Paragraph does not start with a number
                            return " ".join(found_messages)
                        found_messages.append(txt)

        # return a space separated string of those found words
        return ""


class BybitScraper(BinanceScraper):
    def __init__(self):
        super().__init__()

    exchange = "bybit"
    url = "https://t.me/s/Bybit_Announcements"

    def read_message(self, message_dict):
        if message_dict is None:
            return None

        if "Contact".upper() in message_dict['message'].upper():
            pass
        if "Perpetual".upper() in message_dict['message'].upper():
            pass
        if "Margin".upper() in message_dict['message'].upper():
            pass
        if "DERIVAT".upper() in message_dict['message'].upper():
            pass
        if "CONTRACT".upper() in message_dict['message'].upper():
            pass
        elif (
                "Delisting of".upper() in message_dict['message'].upper()):
            arr_coins = self.get_blacklisted_coins(message_dict['message'])
            if arr_coins is not []:
                message_dict['blacklisted_pairs'].extend(arr_coins)
        return message_dict


class OkxScraper(BinanceScraper):
    def __init__(self):
        super().__init__()

    exchange = "okx"
    url = "https://t.me/s/OKXAnnouncements"

    def read_message(self, message_dict):
        if message_dict is None:
            return None

        if "Contact".upper() in message_dict['message'].upper():
            pass
        elif "DERIVATIVE".upper() in message_dict['message'].upper():
            pass
        elif (
                "Delisting of".upper() in message_dict['message'].upper()):
            arr_coins = self.get_blacklisted_coins(message_dict['message'])
            if arr_coins is not []:
                message_dict['blacklisted_pairs'].extend(arr_coins)
        return message_dict


class GateioScraper(BinanceScraper):
    def __init__(self):
        super().__init__()

    exchange = "gateio"
    url = "https://t.me/s/GateioOfficialNews"

    def read_message(self, message_dict):
        if message_dict is None:
            return None

        if "Contact".upper() in message_dict['message'].upper():
            pass
        if "DERIVATIVE".upper() in message_dict['message'].upper():
            pass
        elif (
                "Delist".upper() in message_dict['message'].upper()):
            arr_coins = self.get_blacklisted_coins(message_dict['message'])
            if arr_coins is not []:
                message_dict['blacklisted_pairs'].extend(arr_coins)
        return message_dict


class HtxScraper(BinanceScraper):
    def __init__(self):
        super().__init__()

    exchange = "htx"
    url = "https://t.me/s/HTXGlobalAnnouncementChannel"

    def read_message(self, message_dict):
        if message_dict is None:
            return None

        if "Contact".upper() in message_dict['message'].upper():
            pass
        if "DERIVATIVE".upper() in message_dict['message'].upper():
            pass
        elif (
                "Delist".upper() in message_dict['message'].upper()):
            arr_coins = self.get_blacklisted_coins(message_dict['message'])
            if arr_coins is not []:
                message_dict['blacklisted_pairs'].extend(arr_coins)
        return message_dict


class KucoinScraperWeb(KucoinScraper):
    def __init__(self):
        super().__init__()

    exchange = "kucoin_web"
    url = "https://www.kucoin.com/announcement"

    initialScrollUpTimes = 0
    initialWaitSeconds = 5

    message_bubble = "css-jwocck"
    message_text = ["css-hr7j2u", "css-x0bekk"]
    message_date = {"type": "p",
                    "class": "css-121ce2o",
                    "format": "%m/%d/%Y, %H:%M:%S"}

    def extract_datetime(self, message_html):
        if self.message_date['format'] == "":
            return datetime(1970, 1, 1)  # Return a default datetime object
        datetime_html = message_html.find(self.message_date['type'], class_=self.message_date['class'])
        msg_datetime = datetime.strptime(datetime_html.contents[0], self.message_date['format'])
        return msg_datetime


class BinanceScraperWeb(BinanceScraper):
    def __init__(self):
        super().__init__()

    exchange = "binance_web"
    url = "https://www.binance.com/en/support/announcement/delisting?c=161"

    initialScrollUpTimes = 0
    initialWaitSeconds = 10

    message_bubble = "css-1tl1y3y"
    message_text = ["css-1yxx6id"]
    message_date = {"type": "p",
                    "class": "css-eoufru",
                    "format": ""}

    def extract_datetime(self, message_html):
        if self.message_date['format'] == "":
            return datetime(1970, 1, 1)  # Return a default datetime object
        datetime_html = message_html.find(self.message_date['type'], class_=self.message_date['class'])
        msg_datetime = datetime.strptime(datetime_html.contents[0], self.message_date['format'])
        return msg_datetime


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


def update_has_been_processed_without_date_scraped():
    StatVars.has_been_processed_without_date_scraped = copy.deepcopy(StatVars.has_been_processed)
    for msg in StatVars.has_been_processed_without_date_scraped:
        msg.pop("date_scraped", None)


def open_processed():
    StatVars.logger.info("Loading local processed file")
    try:
        # Read config from stdin if requested in the options
        with Path(StatVars.path_processed_file).open() if StatVars.path_processed_file != '-' else sys.stdin as file:
            StatVars.has_been_processed = rapidjson.load(file, parse_mode=StatVars.CONFIG_PARSE_MODE)
        update_has_been_processed_without_date_scraped()

    except FileNotFoundError:
        logging.error(f'Config file "{StatVars.path_processed_file}" not found!'
                      ' Please create a config file or check whether it exists.')
    except rapidjson.JSONDecodeError:
        logging.error('Please verify your configuration file for syntax errors.')


def save_processed():
    StatVars.logger.info("Saving local processed file")
    try:
        update_has_been_processed_without_date_scraped()
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
        update_has_been_processed_without_date_scraped()

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
                                logging.info(f"bot http://{ip}: Skipped sending the blacklist pair  {pair}"
                                             f"Reason: pair exists already")
                            else:
                                result = api_bot.blacklist(pair)
                                if 'error' in result:
                                    logging.error(f"bot http://{ip}: Attempted to send a blacklist pair and failed"
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


def main():
    open_processed()
    load_bots_data()

    exchanges_to_loop_through = get_exchanges_from_bot_groups()
    check_all_bots()
    # test force-enter and force exits
    # StatVars.bot_groups[0]['new_pair_blacklist'].append("BTC/USDT:USDT")
    # StatVars.bot_groups[0]['new_pair_blacklist'].append("ETH/USDT:USDT")
    # StatVars.bot_groups[0]['new_pair_blacklist'].append("SOL/USDT:USDT")
    # send_force_exit_long()
    # send_force_enter_short()
    # send_blacklists()

    heartbeat_time_pairs = datetime.min
    heartbeat_time = datetime.min  # will push a heartbeat out instantly
    exchanges = ['binance', 'kucoin', 'bybit', 'okx', 'gateio', 'htx']
    exchanges_pairs = {exchange: {} for exchange in exchanges}  # Initialize as empty dictionaries

    # Create the WebDriver instance
    StatVars.driver = webdriver.Chrome(options=StatVars.options)

    while True:
        try:
            StatVars.blacklist_changed = False

            # Only rescan if the minute is not modulo 5 == 0
            # This is done to avoid any potential conflicts with query weights for any timeframe >=5m
            if datetime.now() - heartbeat_time_pairs >= timedelta(hours=24) and datetime.now().minute % 5 > 0:
                refresh_ccxt_exchange_pairs(exchanges_pairs)
                heartbeat_time_pairs = datetime.now()

            # Even if the previous condition triggered, still run through it on startup
            elif all(not exchange_pairs for exchange_pairs in exchanges_pairs.values()):
                logging.info(f"waiting 1 minute, start time is at {datetime.now().minute} % 5 == 0 "
                             f"(to avoid potential issues with query weights)")
                time.sleep(60)
                refresh_ccxt_exchange_pairs(exchanges_pairs)
                heartbeat_time_pairs = datetime.now()

            start_time = time.monotonic()

            current_exchange = "binance"
            if current_exchange.lower() in exchanges_to_loop_through:
                BinanceScraper().scrape(exchanges_pairs[current_exchange])

            current_exchange = "bybit"
            if current_exchange.lower() in exchanges_to_loop_through:
                BybitScraper().scrape(exchanges_pairs[current_exchange])

            current_exchange = "okx"
            if current_exchange.lower() in exchanges_to_loop_through:
                OkxScraper().scrape(exchanges_pairs[current_exchange])

            current_exchange = "gateio"
            if current_exchange.lower() in exchanges_to_loop_through:
                GateioScraper().scrape(exchanges_pairs[current_exchange])

            current_exchange = "htx"
            if current_exchange.lower() in exchanges_to_loop_through:
                HtxScraper().scrape(exchanges_pairs[current_exchange])

            current_exchange = "kucoin"
            if current_exchange.lower() in exchanges_to_loop_through:
                KucoinScraper().scrape(exchanges_pairs[current_exchange])

            if datetime.now() - heartbeat_time >= timedelta(seconds=60):
                # Execute heartbeat action
                logging.info("delist-scraper heartbeat")

                # Update heartbeat time
                heartbeat_time = datetime.now()

            # duration_rounded = round((time.monotonic() - loop_start_time), 2)
            # logging.info(f"This loop took {duration_rounded} seconds")
            time_to_sleep_left = StatVars.loop_secs - ((time.monotonic() - start_time) % StatVars.loop_secs)
            logging.debug(f"for this loop we still have to wait for {time_to_sleep_left} seconds")

            time.sleep(StatVars.loop_secs - ((time.monotonic() - start_time) % StatVars.loop_secs))
        except Exception as ex:
            logging.error(f"An error occurred: {ex}")
            StatVars.driver.quit()
            time.sleep(60)  # an error happened, could be anything ... even being rate limited ... Take a nap bot!
            StatVars.driver = webdriver.Chrome(options=StatVars.options)


if __name__ == "__main__":
    main()
