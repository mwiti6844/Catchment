"""Downstream agents: the gpt-researcher-based deep researcher and the recommender.

Both read from Postgres through the repository layer and trace their LLM calls
through Langfuse.
"""
