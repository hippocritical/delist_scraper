# Delist scraper

**Delist scraper** is a tool designed to scrape Telegram news of exchanges for delisting announcements and promptly send API calls to specified bots to blacklist them via the REST API of freqtrade. Moreover, it can execute short-enter-orders and long-exit-orders. Presently, it exclusively checks for pairs that are delisted from entire exchanges, mainly focusing on those with high volume, such as those traded against USDT.

The rationale behind this decision is that a pair fully delisted from a major exchange like Binance tends to have a significant impact on other exchanges due to the sheer volume involved. 
If an exchange would only discontinue a /BUSD - pair then this will often have minimal to no ripple effects for the exchange or other exchanges.
Backtesting is also facilitated as the 'processed.json' file contains pertinent information such as news texts, blacklisted pairs, and timestamps for each scraped news item.

**Contributions and enhancements to Exchange Delist are encouraged,** including the addition of more exchanges or direct delisted pairs for specific stakes.

## Overview of Files:

- **bot-groups.json:** This file stores details of all bots, including the exchanges to scrape, IPs, usernames, passwords, and the name of the blacklist to be used for local storage. With the provided configuration file (e.g., 'blacklist.json'), transitioning to a VPS and integrating a new blacklist configuration becomes straightforward.
- **processed.json:** This file stores all news that were scraped.

## Initial Loop Logic:

Upon execution, the script first scrapes all news from the specified channels using telethon (a telegram api program).
Once it collects all news items from an exchange, it saves the blacklist and 'processed.json', 
attempting to send the blacklisted pairs to all bots as specified in 'bot-groups.json'.
By switching from browser to telethon you don't need anything powerful anymore cpu wise.

## Logic After Initial Loop:

After completing the initial loop, the program continues to monitor for fresh news.
When new delisting announcements are detected, the affected pairs are added to the blacklist as defined in 'bot-groups.json'.
Additionally, if the 'signal force_enter_new_blacklisted_pairs' parameter is set to true,
the program sends force-short-entry and force-long-exit signals to the relevant bots.

## Reasoning Behind This Tool:

The tool addresses situations where announcements from major exchanges, such as Binance,
regarding delisted pairs (especially significant pairs like XMR), can cause market turbulence across other exchanges.
By promptly blacklisting affected pairs, it aims to mitigate adverse market impacts and potentially capitalize 
on shorting opportunities.

## Backtesting with the Supplied Strategy:

To conduct backtesting with the provided strategy, extract the blacklist from your blacklist json file of choice
and migrate those pairs into a whitelist.
Additionally, modify the strategy to incorporate JSON data into the dataframe.
Note that the provided strategy is illustrative and requires adjustments based on actual trading preferences.

## Setup Process:

1. Define your bots in 'bot-groups.json,' specifying the exchanges for which blacklisted pairs should be sent.
2. Copy 'bot-groups.json.example' to 'bot-groups.json.'
3. Modify 'bot-groups.json' with your bot information.
4. Optionally, pre-fill your blacklist in 'bot-groups.json', or let the tool create it automatically upon saving.
5. Adjust the 'loop_secs' parameter to suit your scraping frequency preference.


## Setup process:
Define your bots in bot-groups.json including which exchanges' into it should get as blacklisted pairs.
``` 
cp bot-groups.json.example bot-groups.json
cp processed.json.example processed.json
cp global_blacklist.json.example global_blacklist.json
nano bot-groups.json
cp telegram_config.json.example telegram_config.json.example
```

* Modify bot-groups.json with the info of your bots
  * Optionally you can create and pre-fill your blacklist defined in bot-groups.json. Alternatively it will create that file you defined in bot-groups.json automatically upon saving the blacklist.
* Modify telegram_config.json
  * Go on the website https://my.telegram.org and log in.
    * Click on "API Development Tools", fill out the form, then transfer your api_id and api_hash to the file. 

## Non-docker
```
bash install.sh
source .venv/bin/activate
bash run.sh
```


## Docker
```
docker compose up --build
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
