import re

import pandas
from freqtrade.strategy.interface import IStrategy
from pandas import DataFrame, Series, DatetimeIndex, merge
from functools import reduce
from datetime import timedelta, datetime, timezone
from typing import Optional, Union, List
from io import StringIO


class delist_shorter_strategy(IStrategy):
    INTERFACE_VERSION = 3

    minimal_roi = {
        '1440': -1,
        '0': 100
    }
    my_leverage = 10
    can_short = True
    timeframe = '1m'
    stoploss = -0.5
    trailing_stop = True
    trailing_stop_positive = 0.10
    process_only_new_candles = True

    # load processed.json into a dataframe once for all pairs
    with open('./user_data/strategies/processed.json', 'r') as file:
        json_data = file.read()
    json_df = pandas.read_json(StringIO(json_data))
    json_df['date'] = json_df['date'].apply(lambda x: x.ceil('min') if x.second != 0 else x)

    def leverage(self, pair: str, current_time: datetime, current_rate: float,
                 proposed_leverage: float, max_leverage: float, entry_tag: Optional[str], side: str,
                 **kwargs) -> float:
        return self.my_leverage

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Load the JSON file into a DataFrame

        result_df = pandas.merge(dataframe, self.json_df, on='date', how='left')

        result_df['delist_signal'] = False

        for index, row in result_df.iterrows():
            if isinstance(row['blacklisted_pairs'], list):
                for pattern in row['blacklisted_pairs']:
                    if re.search(pattern, metadata['pair']):
                        result_df.at[index, 'delist_signal'] = True
        return result_df

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe['delist_signal'], 'enter_short'] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe
