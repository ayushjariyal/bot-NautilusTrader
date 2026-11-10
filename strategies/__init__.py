"""
Trading logic.

btc_straddle.py is the only module here that depends on NautilusTrader. The helper modules
added in later milestones (instrument selection, liquidity checks, premium maths, risk
rules) are plain Python so they can be unit-tested without an exchange connection.
"""
