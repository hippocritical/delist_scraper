import gc
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
import rapidjson
from bs4 import BeautifulSoup

from tqdm import tqdm
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

from libs.api import FtRestClient


class StatVars:
    # Tries to scroll up multiple times to make the parsing of the data less often and thereby speed up drastically.
    driver = None
    initialScrollUpTimes = 100

    # Please don't set it to 0 !
    scrollUpSleepTime = 0.5

    path_processed_file = 'processed.json'
    path_bots_file = 'bot-groups.json'
    CONFIG_PARSE_MODE = rapidjson.PM_COMMENTS | rapidjson.PM_TRAILING_COMMAS
    has_been_processed = []
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

    # Disable loading images
    prefs = {"profile.managed_default_content_settings.images": 2}
    options.add_experimental_option("prefs", prefs)

    # driver_subUrl = webdriver.Chrome(options=options)


def report_to_be_processed():
    for message_dict in StatVars.to_be_processed:
        logging.info(f"caught fresh news for {message_dict['exchange']}: {message_dict['message']}")


class BinanceScraper:
    exchange = "binance"
    telegram_url = "https://t.me/s/binance_announcements"

    def scrape_telegram(self):
        StatVars.driver.get(self.telegram_url)

        for_loops_count = 0
        prev_message_count = 0
        # scan once without scrolling to have the loop faster if we just need to scrape the first 20 ish messages
        messages, prev_message_count, stop_loop = self.read_messages(StatVars.driver, prev_message_count, True)
        current_scroll_up_times = StatVars.initialScrollUpTimes

        while not stop_loop:
            # scrolling several times to make the overall loop faster, uses tqdm for a progression bar
            for _ in tqdm(range(current_scroll_up_times), desc="Scrolling up to fetch more news", unit="scroll"):
                StatVars.driver.execute_script("window.scrollTo(0, 0);")
                time.sleep(StatVars.scrollUpSleepTime)
                for_loops_count += 1
            messages, prev_message_count, stop_loop = self.read_messages(StatVars.driver, prev_message_count)

        # now fill the message_html
        for message_html in messages[::-1]:
            prepared_message_dict = self.prepare_message_dict(message_html)
            message_dict = self.read_message(prepared_message_dict)

            if message_dict in StatVars.has_been_processed:
                # logging.info(f"message already exists for exchange {message_dict['exchange']}: "
                #              f"{message_dict['message']}")
                break
            else:
                StatVars.to_be_processed.append(message_dict)

        if len(StatVars.to_be_processed) > 0:
            StatVars.has_been_processed.extend(StatVars.to_be_processed)
            report_to_be_processed()
            save_processed(self.exchange)

        # make one big list of newly delisted pairs
        new_blacklist = []
        for message_dict in StatVars.to_be_processed:
            if message_dict is None:
                continue
            new_blacklist.extend(message_dict["blacklisted_pairs"])

        new_blacklist = set(new_blacklist)
        new_blacklist = list(new_blacklist)
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
        messages = soup.find_all("div", class_="tgme_widget_message_wrap")
        len_messages = len(messages)
        prepared_message_dict = self.prepare_message_dict(messages[0])
        first_message = self.read_message(prepared_message_dict)

        if first_message in StatVars.has_been_processed:
            if not first_try:
                StatVars.logger.info(
                    f"{self.exchange}: We found a message that has already been scraped. "
                    f"Stopping to get additional news!")
            stop_loop = True
        elif len_messages == prev_message_count:
            StatVars.logger.info(f"{self.exchange}: We found {prev_message_count} overall! "
                                 f"The count didn't increase. "
                                 f"Stopping...")
            stop_loop = True
        else:
            StatVars.logger.info(
                f"{self.exchange}: Count of additional messages fetched in this loop: "
                f"{len_messages - prev_message_count}, now: {len_messages}. Continuing.")

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
        elif delist_string.upper() in message_dict['message'].upper():

            title = (message_dict['message'].upper()
                     .replace(delist_string.upper(), "")
                     .replace(" and ".upper(), ", ")
                     .replace(" & ".upper(), ", ")
                     .replace("https://".upper(), " https://".upper())
                     )

            arr_title_before = re.split(r'\bon\b|\btrading\b|\bhttps?://\b', title, flags=re.IGNORECASE)
            if len(arr_title_before[0]) > 0:
                arr_coins = re.split(r",\s*", arr_title_before[0])

                # sometimes coins are defined as the name AND the shortcut ... this takes the (*) coin if it exists.
                for coin in set(arr_coins):
                    coin_currency: str = f"{coin.strip()}/.*".upper()
                    if "(" in coin:
                        words = coin.split(" ")
                        for word in words:
                            if "(" in word:
                                coin_currency = f"{word.strip('()')}/.*".upper()
                    message_dict['blacklisted_pairs'].append(coin_currency)

        return message_dict

    def prepare_message_dict(self, message_html):
        message_text_element = message_html.find("div", class_="tgme_widget_message_text")
        if message_text_element:
            stripped_message = message_text_element.text.strip()
        else:
            stripped_message = ""

        # Remove non-printable characters and multiple whitespaces
        message_content = re.sub(r'[^\x00-\x7F]+', ' ', stripped_message)
        message_content = re.sub(r'\s+', ' ', message_content)
        message_content = re.sub(r'(?i)(https://)', r' \1', message_content)

        # Replace double quotes with single quotes to not have to have \" in the strings and keep the quotes
        message_content = message_content.replace('"', "'")

        datetime_html = message_html.find("a", class_="tgme_widget_message_date")
        msg_datetime = datetime.strptime(datetime_html.contents[0].attrs['datetime'], StatVars.datetimeFormat)

        url_matches = re.findall(r'\bhttps://\S+', message_content, re.IGNORECASE)
        url_word = ""
        if url_matches:
            url_word = url_matches[0]
            url_word = url_word.split('#')[-1]

        message_dict = {
            "exchange": self.exchange,
            "date": msg_datetime.strftime(StatVars.datetimeFormat),
            "message": message_content,
            "linked_url": url_word,
            # to be filled in read_message, not to be saved into the bots file
            # - just in the blacklist.json file of the bot
            "blacklisted_pairs": [],
        }
        return message_dict


class KucoinScraper(BinanceScraper):
    def __init__(self):
        super().__init__()

    exchange = "kucoin"
    telegram_url = "https://t.me/s/Kucoin_News"

    def read_message(self, message_dict):
        if message_dict is None:
            return None
        arr_coins = []

        if (
                "KUCOIN DAILY REPORT".upper() in message_dict['message'].upper() or
                "KuCoin Will Delist the Sandbox Mode"):
            pass
        if (
                "KUCOIN WILL DELIST THE".upper() in message_dict['message'].upper() or
                "WILL BE REMOVED FROM THE EXCHANGE".upper() in message_dict['message'].upper() or
                "RISK ANNOUNCEMENT".upper() in message_dict['message'].upper() or
                "WILL BE DELISTED FROM KUCOIN".upper() in message_dict['message'].upper()):
            arr_coins = re.findall(r'\((\w+)\)', message_dict['message'].upper())
            if "KUCOIN WILL DELIST THE".upper() in message_dict['message'].upper():
                match = re.search(r"KUCOIN WILL DELIST THE (\w+)", message_dict['message'].upper())
                if match:
                    target_word = match.group(1)
                    arr_coins.append(target_word)

        for coin in set(arr_coins):
            coin_currency: str = f"{coin}/.*".upper()
            message_dict['blacklisted_pairs'].append(coin_currency)

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


def open_processed():
    StatVars.logger.info("Loading local processed file")
    try:
        # Read config from stdin if requested in the options
        with Path(StatVars.path_processed_file).open() if StatVars.path_processed_file != '-' else sys.stdin as file:
            StatVars.has_been_processed = rapidjson.load(file, parse_mode=StatVars.CONFIG_PARSE_MODE)
            # for line in config:
            #    statVars.has_been_processed.append(line)
    except FileNotFoundError:
        logging.error(f'Config file "{StatVars.path_processed_file}" not found!'
                      ' Please create a config file or check whether it exists.')
    except rapidjson.JSONDecodeError:
        logging.error('Please verify your configuration file for syntax errors.')


def save_processed(exchange):
    for bot in StatVars.bot_groups:
        if exchange in bot['exchanges']:
            StatVars.logger.info("Saving local processed file")
            try:
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
            # for line in config:
            #    statVars.has_been_processed.append(line)
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
                api_bot = (FtRestClient(f"http://{ip}", bot_group['username'], bot_group['password']))
                blacklist_response = api_bot.blacklist()
                for pair in bot_group['new_pair_blacklist']:
                    if pair in blacklist_response['blacklist']:
                        logging.info(f"bot http://{ip}: Skipped sending the blacklist pair  {pair}. "
                                     f"Reason: pair exists already.")
                    else:
                        result = api_bot.blacklist(pair)
                        if 'error' in result:
                            logging.error(f"bot http://{ip}: Attempted to send a blacklist pair and failed."
                                          f"Error: {result['result']}.")
                        else:
                            logging.info(f"bot http://{ip}: Successfully sent the pair {pair} to the blacklist.")


def send_force_enter_short():
    for bot_group in StatVars.bot_groups:
        if bot_group['force_enter_short']:
            if 'new_pair_blacklist' in bot_group:
                for ip in bot_group['ips']:
                    api_bot = (FtRestClient(f"http://{ip}", bot_group['username'], bot_group['password']))
                    for pair in bot_group['new_pair_blacklist']:
                        result = api_bot.forceenter(pair, 'short')
                        if 'error' in result:
                            logging.error(f"bot http://{ip}: Attempted to force enter a short trade of {pair}"
                                          f" and failed. Error: {result['result']}.")
                        else:
                            logging.info(f"bot http://{ip}: Successfully sent a force enter short order "
                                         f"of the pair {pair}.")


def send_force_exit_long():
    for bot_group in StatVars.bot_groups:
        if bot_group['force_exit_long']:
            if 'new_pair_blacklist' in bot_group:
                for ip in bot_group['ips']:
                    api_bot = (FtRestClient(f"http://{ip}", bot_group['username'], bot_group['password']))
                    open_trades = api_bot.status()
                    for pair in bot_group['new_pair_blacklist']:
                        for open_trade in open_trades:
                            if pair == open_trade['pair']:
                                if not open_trade['is_short']:  # only exit long, not short
                                    result = api_bot.forceexit(open_trade['trade_id'])
                                    if 'error' in result:
                                        logging.error(f"bot http://{ip}: Attempted to force exit a long trade of {pair}"
                                                      f" and failed. Error: {result['result']}.")
                                    else:
                                        logging.info(f"bot http://{ip}: Successfully sent a force-exit-long order "
                                                     f"of the pair {pair}.")


# This checks all bots' connections ... just for the user as a sanity check
def check_all_bots():
    logging.info("checking all bot-connections:")
    for bot_group in StatVars.bot_groups:
        for ip in bot_group['ips']:
            api_bot = (FtRestClient(f"http://{ip}", bot_group['username'], bot_group['password']))
            response = api_bot.status()
            if isinstance(response, list):
                logging.info(f"connection to bot http://{ip}: connection successful!")
            else:
                logging.warning(f"connection to bot http://{ip}: connection failed?!")


# Each bots data is being loaded at the start
# including which exchange it should get data from and which blacklist it already contains.
# Then it will try to scrape one exchange after the other and find news.
# At first, it will scroll up (toward older news) until it either finds nothing more
# or news that was previously found and will search through those news to find new pairs to blacklist.
# When it found something new then it will go through all bots and depending on which exchanges they "subscribed" to
# and send the new blacklist to the bots' API.

# will re check the blacklist of has_been_processed to to_be_processed
# this is just to be used to debug the blacklist-generation!
def recalculate_existing_messages():
    has_been_processed_recalculated = []
    for message_dict in StatVars.has_been_processed:
        message_dict['blacklisted_pairs'] = []
        if message_dict['exchange'] == "binance":
            has_been_processed_recalculated.append(BinanceScraper().read_message(message_dict))
            save_processed("binance")
        elif message_dict['exchange'] == "kucoin":
            has_been_processed_recalculated.append(KucoinScraper().read_message(message_dict))
            save_processed("kucoin")
        # elif message_dict['exchange'] == "bybit":
        #    has_been_processed_recalculated.append(BybitScraper().read_message(message_dict))
        #    save_processed("bybit")

    StatVars.has_been_processed = has_been_processed_recalculated


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


def main():
    try:
        # Create the WebDriver instance
        StatVars.driver = webdriver.Chrome(options=StatVars.options)
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

        start_time = time.monotonic()
        heartbeat_time = datetime.min  # will push a heartbeat out instantly
        while True:
            # loop_start_time = time.monotonic()
            StatVars.blacklist_changed = False

            if "binance".lower() in exchanges_to_loop_through:
                BinanceScraper.scrape_telegram(BinanceScraper())
                gc.collect()  # force garbage collection to kick in

            if "kucoin".lower() in exchanges_to_loop_through:
                KucoinScraper.scrape_telegram(KucoinScraper())
                gc.collect()  # force garbage collection to kick in

            # BybitScraper.scrape_telegram(BybitScraper())
            # gc.collect()  # force garbage collection to kick in

            if datetime.now() - heartbeat_time >= timedelta(seconds=60):
                # Execute heartbeat action
                logging.info("delist-scraper heartbeat")

                # Update heartbeat time
                heartbeat_time = datetime.now()

            # duration_rounded = round((time.monotonic() - loop_start_time), 2)
            # logging.info(f"This loop took {duration_rounded} seconds")

            time.sleep(StatVars.loop_secs - ((time.monotonic() - start_time) % StatVars.loop_secs))
    finally:
        StatVars.driver.quit()


if __name__ == "__main__":
    main()
