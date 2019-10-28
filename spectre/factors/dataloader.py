import pandas as pd
import os
from zipfile import ZipFile

"""
df1 = pd.DataFrame([[1,2,3],[4,5,6],[7,8,9]], columns=('a','b','c'))
df2 = pd.DataFrame([[1,20,30],[4,50,60],[7,80,90]], columns=('a','b','c'))
df1 = df1.set_index('a')
df2 = df2.set_index('a')
Out[66]: 
   b  c
a      
1  2  3
4  5  6
7  8  9
Out[67]: 
    b   c
a        
1  20  30
4  50  60
7  80  90

df = pd.concat({'df1': df1, 'df2':df2})
Out[87]: 
        b   c
    a        
df1 1   2   3
    4   5   6
    7   8   9
df2 1  20  30
    4  50  60
    7  80  90
    
df.loc[(slice(None), slice(3,8)), :]
Out[27]: 
        b   c
    a        
df1 4   5   6
    7   8   9
df2 4  50  60
    7  80  90
        
df = df.swaplevel(0, 1).sort_index(level=0)
Out[35]: 
        b   c
a            
1 df1   2   3
  df2  20  30
4 df1   5   6
  df2  50  60
7 df1   8   9
  df2  80  90
  
df['b'].unstack(level=[1])   
Out[88]: 
   df1  df2
a          
1    2   20
4    5   50
7    8   80
    
df.loc[3:8, :]
Out[39]: 
        b   c
a            
4 df1   5   6
  df2  50  60
7 df1   8   9
  df2  80  90
      
"""


class DataLoader:
    def __init__(self, ohlcv=('open', 'high', 'low', 'close', 'volume')) -> None:
        self._ohlcv = ohlcv

    def get_ohlcv_names(self):
        return self._ohlcv

    def load(self, start, end, backward: int) -> pd.DataFrame:
        """
        If for back-testing, `start` `end` parameter has no meaning,
        because should be same as 'now'.
        """
        raise NotImplementedError("abstractmethod")


class CsvDirLoader(DataLoader):
    def __init__(self, path: str, split_by_year=False, dividends_path='',
                 ohlcv=('open', 'high', 'low', 'close', 'volume'),
                 **read_csv):
        """
        Load data from csv dir, structured as xxx.csv per stock..
        :param split_by_year: If file name like 'spy_2017.csv', set this to True
        :param ohlcv: OHLCV column names, If set, you can access those data like
                      `spectre.factors.OHLCV.open`, also
                      TODO all those column will be adjust by Dividends/Splits(anchor at `end` time)
        :param read_csv: Parameters for pd.read_csv.
        """
        super().__init__(ohlcv)
        self._csv_dir = path
        # dividends: self._div_dir = dividends_path #dividends_path='',
        self._split_by_year = split_by_year
        self._read_csv = read_csv

        self._cache = (None, None, None)

    def _load_split_by_year(self, start, end):
        years = set(pd.date_range(start, end).year)
        dfs = {}
        for entry in os.scandir(self._csv_dir):
            if entry.name.endswith(".csv") and entry.is_file():
                year, name = entry.name[-8:-4], entry.name[:-9]  # like 'spy_2011.csv'
                if int(year) not in years or name in dfs:
                    continue
                df = pd.DataFrame()
                for year in years:
                    try:
                        csv_path = '{}{}_{}.csv'.format(self._csv_dir, name, year)
                        df_year = pd.read_csv(csv_path, **self._read_csv)
                        df = pd.concat([df_year, df])
                    except OSError:
                        pass
                df.sort_index(inplace=True)
                assert isinstance(df.index, pd.DatetimeIndex), \
                    "data must index by datetime, set correct `read_csv`, " \
                    "for example index_col='date', parse_dates=True"
                df = df[~df.index.duplicated(keep='last')]
                df = df.tz_localize('UTC')
                dfs[name] = df
        df_concat = pd.concat(dfs).swaplevel(0, 1).sort_index(level=0)
        times = df_concat.index.get_level_values(0)
        self._cache = (times[0], times[-1], df_concat)

    def _load(self, start, end):
        dfs = {}
        for entry in os.scandir(self._csv_dir):
            if entry.name.endswith(".csv") and entry.is_file():
                df = pd.read_csv(self._csv_dir + entry.name, **self._read_csv)
                assert isinstance(df.index, pd.DatetimeIndex), \
                    "data must index by datetime, set correct `read_csv`, " \
                    "for example index_col='date', parse_dates=True"
                df = df.tz_localize('UTC')
                dfs[entry.name[:-4]] = df
        df_concat = pd.concat(dfs).swaplevel(0, 1).sort_index(level=0)
        times = df_concat.index.get_level_values(0)
        self._cache = (times[0], times[-1], df_concat)

    def _load_from_cache(self, start, end, backward):
        if self._cache[0] and start >= self._cache[0] and end <= self._cache[1]:
            index = self._cache[2].index.get_level_values(0).unique()
            start_slice = index.get_loc(start, 'bfill')
            start_slice = max(start_slice - backward, 0)
            start_slice = index[start_slice]
            return self._cache[2].loc[start_slice:end]
        else:
            return None

    def load(self, start, end, backward):
        start, end = pd.Timestamp(start, tz='UTC'), pd.Timestamp(end, tz='UTC')
        rtn = self._load_from_cache(start, end, backward)
        if rtn is not None:
            return rtn

        if self._split_by_year:
            self._load_split_by_year(start, end)
        else:
            self._load(start, end)
        return self._load_from_cache(start, end, backward)


class QuandlLoader(DataLoader):
    def __init__(self, file: str, ohlcv=('open', 'high', 'low', 'close', 'volume')) -> None:
        """
        Usage:
        download data first:
        https://www.quandl.com/api/v3/datatables/WIKI/PRICES.csv?qopts.export=true&api_key=[yourapi_key]
        then:
        loader = factors.QuandlLoader('./quandl/WIKI_PRICES.zip')
        """
        super().__init__(ohlcv)

        with ZipFile(file) as zip:
            with zip.open(zip.namelist()[0]) as csv:
                df = pd.read_csv(csv, parse_dates=['date'],
                                 index_col=['date', 'ticker'],
                                 usecols=['ticker', 'date', 'open', 'high', 'low', 'close',
                                          'volume',
                                          'ex-dividend', 'split_ratio', ],
                                 )
        df.tz_localize('UTC', level=0, copy=False)
        df.sort_index(level=0, inplace=True)
        self._cache = df

    def load(self, start, end, backward: int) -> pd.DataFrame:
        start, end = pd.Timestamp(start, tz='UTC'), pd.Timestamp(end, tz='UTC')
        index = self._cache.index.get_level_values(0).unique()
        start_slice = index.get_loc(start, 'bfill')
        start_slice = max(start_slice - backward, 0)
        start_slice = index[start_slice]
        return self._cache.loc[start_slice:end]
