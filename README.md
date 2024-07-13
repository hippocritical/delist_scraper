# Delist scraper

**Delist scraper** is a tool designed to scrape Telegram news of exchanges for delisting announcements and promptly send API calls to specified bots to blacklist them via the REST API of freqtrade. Moreover, it can execute short-enter-orders and long-exit-orders. Presently, it exclusively checks for pairs that are delisted from entire exchanges, mainly focusing on those with high volume, such as those traded against USDT.

The rationale behind this decision is that a pair fully delisted from a major exchange like Binance tends to have a significant impact on other exchanges due to the sheer volume involved. 
If an exchange would only discontinue a /BUSD - pair then this will often have minimal to no ripple effects for the exchange or other exchanges.
Backtesting is also facilitated as the 'processed.json' file contains pertinent information such as news texts, blacklisted pairs, and timestamps for each scraped news item.

**Contributions and enhancements to Exchange Delist are encouraged,** including the addition of more exchanges or direct delisted pairs for specific stakes.

## Overview of Files:

- **bot-groups.json:** This file stores details of all bots, including the exchanges to scrape, IPs, usernames, passwords, and the name of the blacklist to be used for local storage. With the provided configuration file (e.g., 'blacklist.json'), transitioning to a VPS and integrating a new blacklist configuration becomes straightforward.
- **processed.json:** This file stores all news that were scraped.
- **processed.json_prefilled.7z** This file is already pre-filled so the initial loop does not take for hours and does not need tons of RAM.

## Initial Loop Logic:

Upon execution, the script first scrapes all news from the specified channels using a Chromium instance, which demands considerable memory resources. Once it collects all news items from an exchange, it saves the blacklist and 'processed.json', attempting to send the blacklisted pairs to all bots as specified in 'bot-groups.json'. It's advised not to perform this initial step on a low-memory VPS. Instead, it's recommended to conduct it locally and then transfer the JSON file to the VPS if necessary. Alternatively, ample swap space (e.g., 10GB) can facilitate scraping, especially for exchanges like KuCoin.

**You can run this program on a weaker VPS or a Raspberry Pi with limited memory,** provided the initial data gathering is done on a more powerful machine. The initial run involves opening a browser window with approximately 20k messages, consuming over 8GB of memory. Subsequent runs are less resource-intensive.

## A special case for the exchange Kraken:
Kraken has abysmally slow download speeds, and additionally you have to download trade-data.
If you want to have pairlists, then please download the premade data and convert them to daily jsongz candle data previously.
This will speed up the calculation times by infinity.
https://support.kraken.com/hc/en-us/articles/360047543791-Downloadable-historical-market-data-time-and-sales-

https://www.freqtrade.io/en/stable/exchanges/#historic-kraken-data
For more info please read the docs how to convert the premade csv trade-data to candle-data.

## Logic After Initial Loop:

After completing the initial loop, the program continues to monitor for fresh news. When new delisting announcements are detected, the affected pairs are added to the blacklist as defined in 'bot-groups.json'. Additionally, if the 'signal force_enter_new_blacklisted_pairs' parameter is set to true, the program sends force-short-entry and force-long-exit signals to the relevant bots.

## Reasoning Behind This Tool:

The tool addresses situations where announcements from major exchanges, such as Binance, regarding delisted pairs (especially significant pairs like XMR), can cause market turbulence across other exchanges. By promptly blacklisting affected pairs, it aims to mitigate adverse market impacts and potentially capitalize on shorting opportunities.

## Backtesting with the Supplied Strategy:

To conduct backtesting with the provided strategy, extract the blacklist from your blacklist json file of choice
and migrate those pairs into a whitelist.
Additionally, modify the strategy to incorporate JSON data into the dataframe.
Note that the provided strategy is illustrative and requires adjustments based on actual trading preferences.

## Considerations if you want to run it on a weak VPS
The initial setup takes a lot of memory. It is advised to do the initial round on your home PC with at least 8GB RAM and SWAP.
After the initial run you can easily run the scraper on a 1GB VPS with a weak CPU.

## Note on using ARM processors
Selenium cannot run on ARM processors, sorry.

## Setup Process:

1. Define your bots in 'bot-groups.json,' specifying the exchanges for which blacklisted pairs should be sent.
2. Copy 'bot-groups.json.example' to 'bot-groups.json.'
3. Modify 'bot-groups.json' with your bot information.
4. Optionally, pre-fill your blacklist in 'bot-groups.json', or let the tool create it automatically upon saving.
5. Adjust the 'loop_secs' parameter to suit your scraping frequency preference (default is 10 seconds).


## Setup process:
Define your bots in bot-groups.json including which exchanges' into it should get as blacklisted pairs.
``` 
cp bot-groups.json.example bot-groups.json
cp processed.json.example processed.json
cp global_blacklist.json.example global_blacklist.json
nano bot-groups.json
```

* Modify bot-groups.json with the info of your bots
  * Optionally you can create and pre-fill your blacklist defined in bot-groups.json. Alternatively it will create that file you defined in bot-groups.json automatically upon saving the blacklist.
* Modify `loop_secs` to suit your preference of how often the bot scrape all exchanges. Default is 10 seconds.

## Non-docker
```
bash install.sh
bash install_firefox.sh
source .venv/bin/activate
bash run.sh
```


## Docker
```
docker compose up -d --build
```

# Frequenthippo - analytics
We have a website on http://frequenthippo.ddns.net which contains masses of dry runs and live runs, as well as backtest data.
You are welcome to join our community!
Please join our discord, the link is on the website.

## Donations
Any services and programs are free and publicly available. Please consider donating.

- BTC: 1JebmmqC3MdkNBubAVFP7Mpqu9Q6am6yL8
- ETH: 0x7d75c0cf33da8426ccaa10cd3b2380965ac5f8c2
- BEP20/BSC: 0x7d75c0cf33da8426ccaa10cd3b2380965ac5f8c2
- TRC20/TRON: TEPJvcfmuDSLqQtToXZ55LnncERkwAC14a

## Source repository
Big thanks to stash86 who built their binance_delist which was used as a framework.
