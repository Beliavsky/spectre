"""
@author: Heerozh (Zhang Jianhao)
@copyright: Copyright 2019, Heerozh. All rights reserved.
@license: Apache 2.0
@email: heeroz@gmail.com
"""
from abc import ABC
import pandas as pd
from collections import namedtuple
from .event import Event, EventReceiver, EventManager, EveryBarData, MarketOpen, MarketClose
from .metric import plot_cumulative_returns
from .blotter import BaseBlotter, SimulationBlotter
from ..factors import FactorEngine
from ..factors import DataLoader


class Recorder:
    def __init__(self):
        self._records = []

    def record(self, date, table):
        if 'date' in table:
            raise ValueError('`date` is reserved key for record.')
        table['date'] = date
        self._records.append(table)

    def to_df(self):
        ret = pd.DataFrame(self._records)
        if ret.shape[0] > 0:
            ret = ret.set_index('date').sort_index(axis=0)
        return ret


class CustomAlgorithm(EventReceiver, ABC):
    Results = namedtuple('Results', ['returns', 'positions', 'transactions'])
    """
    Base class for custom trading algorithm.
    """
    def __init__(self, blotter: BaseBlotter, **data_sources: DataLoader):
        """
        :param blotter: order management system for this algorithm.
        :param data_sources: key is data_source_name, value is dataloader
        """
        super().__init__()
        if not data_sources:
            raise ValueError("At least one data source.")

        self._history_window = pd.DateOffset(0)
        self._data = None
        self._engines = {name: FactorEngine(loader) for name, loader in data_sources.items()}
        self.blotter = blotter
        self._recorder = Recorder()
        self._current_dt = None
        self._results = CustomAlgorithm.Results(None, None, None)

    def clear(self):
        for engine in self._engines.values():
            engine.clear()

    def create_factor_engine(self, name: str, like: FactorEngine):
        assert name not in self._engines
        self._engines[name] = FactorEngine(like.loader_)
        return self._engines[name]

    def get_factor_engine(self, name: str = None):
        if name is None:
            name = next(iter(self._engines))

        if name not in self._engines:
            raise KeyError("Data source '{0}' not found, please pass in the algorithm "
                           "initialization: `YourAlgorithm({0}=DataLoader())`".format(name))
        return self._engines[name]

    def set_datetime(self, dt: pd.Timestamp) -> None:
        self._current_dt = dt
        self.blotter.set_datetime(dt)

    @property
    def current(self):
        return self._current_dt

    @property
    def results(self):
        return self._results

    def history_window(self, date_offset: pd.DateOffset):
        self._history_window = date_offset

    def record(self, **kwargs):
        self._recorder.record(self._current_dt, kwargs)

    def plot(self, annual_risk_free_rate=0.04, benchmark: pd.Series = None) -> None:
        returns = self._results.returns

        bench = None
        if benchmark is not None:
            bench = benchmark.loc[returns.index[0]:returns.index[-1]]

        plot_cumulative_returns(returns, self._results.positions,  self._results.transactions,
                                bench, annual_risk_free_rate)

    def schedule_rebalance(self, event: Event):
        """Can only be called in initialize()"""
        origin_callback = event.callback

        def _rebalance_callback(_):
            if isinstance(self._data, dict):
                last = {k: v.loc[v.index.get_level_values(0)[-1]]
                        for k, v in self._data.items()}
                history = {k: v.loc[(self._current_dt - self._history_window):]
                           for k, v in self._data.items()}
            else:
                last_dt = self._data.index.get_level_values(0)[-1]
                last = self._data.loc[last_dt]
                history = self._data.loc[(self._current_dt - self._history_window):]
            origin_callback(last, history)
        event.callback = _rebalance_callback
        self.schedule(event)

    def run_engine(self, start, end):
        if start is None:
            start = self._current_dt
            end = self._current_dt
        start = start - self._history_window

        if len(self._engines) == 1:
            name = next(iter(self._engines))
            return self._engines[name].run(start, end)
        else:
            return {name: engine.run(start, end) for name, engine in self._engines.items()}

    def _run_engine(self, event_source=None):
        self._data = self.run_engine(None, None)

    def on_run(self):
        # schedule first, so it will run before rebalance
        self.schedule(EveryBarData(self._run_engine))
        self.initialize()

    def on_end_of_run(self):
        self._results = CustomAlgorithm.Results(
            returns=self.blotter.get_returns(),
            positions=self.blotter.get_historical_positions(),
            transactions=self.blotter.get_transactions())
        self.terminate(self._recorder.to_df())

    def initialize(self):
        raise NotImplementedError("abstractmethod")

    def terminate(self, records: pd.DataFrame) -> None:
        pass


# ----------------------------------------------------------------


class SimulationEventManager(EventManager):
    _last_data = None

    @classmethod
    def _get_most_granular(cls, data):
        freq = {k: min(v.index.levels[0][1:]-v.index.levels[0][:-1]) for k, v in data.items()}
        return data[min(freq, key=freq.get)]

    def fire_before_event(self, event_type):
        for _, events in self._subscribers.items():
            for event in events:
                if isinstance(event, event_type):
                    if event.offset < 0:
                        event.callback(self)

    def fire_after_event(self, event_type):
        for _, events in self._subscribers.items():
            for event in events:
                if isinstance(event, event_type):
                    if event.offset >= 0:
                        event.callback(self)

    def fire_market_open(self, alg):
        self.fire_before_event(MarketOpen)
        alg.blotter.set_price('open')
        alg.blotter.update_portfolio_value()
        self.fire_after_event(MarketOpen)

    def fire_market_close(self, alg):
        alg.blotter.set_price('close')
        alg.blotter.update_portfolio_value()
        self.fire_before_event(MarketClose)
        self.fire_after_event(MarketClose)

    def run(self, start, end):
        from tqdm.auto import tqdm

        start, end = pd.to_datetime(start, utc=True), pd.to_datetime(end, utc=True)

        if not self._subscribers:
            raise ValueError("At least one subscriber.")

        for r, events in self._subscribers.items():
            # clear scheduled events
            events.clear()
            if isinstance(r, CustomAlgorithm):
                r.clear()
            r.on_run()

        for alg in self._subscribers:
            if not isinstance(alg, CustomAlgorithm):
                continue
            if not isinstance(alg.blotter, SimulationBlotter):
                raise ValueError('SimulationEventManager only supports SimulationBlotter.')
            alg.blotter.clear()
            # get factor data from algorithm
            data = alg.run_engine(start, end)
            alg.run_engine = lambda x, y: self._last_data
            if isinstance(data, dict):
                main = self._get_most_granular(data)
                main = main.loc[start:end]
            else:
                main = data
            # loop factor data
            last_day = None
            ticks = main[start:].index.get_level_values(0).unique()
            for dt in tqdm(ticks):
                if self._stop:
                    break
                # prepare data
                if isinstance(data, dict):
                    self._last_data = {k: v[:dt] for k, v in data.items()}
                else:
                    self._last_data = data[:dt]

                # if date changed
                if dt.day != last_day:
                    if last_day is not None:
                        self.fire_market_close(alg)
                    alg.set_datetime(dt)

                # fire daily data event
                if dt.hour == 0:
                    self.fire_event(self, EveryBarData)

                # fire open event
                if dt.day != last_day:
                    self.fire_market_open(alg)
                    last_day = dt.day

                # fire intraday data event
                if dt.hour != 0:
                    self.fire_event(self, EveryBarData)

            self.fire_market_close(alg)

        for r in self._subscribers.keys():
            r.on_end_of_run()
