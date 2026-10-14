"""LLM layer: prefilter, flagging, DD questions, memo. Implemented in M3 and M4.

The only package that calls the Anthropic API. Every function accepts `client=None`
for deterministic mode. The LLM never produces a number.
"""
