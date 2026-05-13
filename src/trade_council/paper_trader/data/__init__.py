"""Data fetchers used by the paper-trader engine.

Each module here exposes a single class or function with a simple, stable
contract; the engine never knows which feed is behind it. Swap the
implementation (e.g. switch yfinance → Polygon) without touching engine.py.
"""
