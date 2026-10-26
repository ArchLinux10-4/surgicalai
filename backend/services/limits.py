"""Caps shared by the prompt text and the code that enforces them.

A number in a prompt and a different number in the parser is how a model
gets told five and then handed eight.
"""

MAX_SEARCH_TERMS = 5
SEARCH_HITS_PER_FILE = 8
SEARCH_MAX_HITS = 40
READ_MAX_CHARS = 80_000
GITHUB_READ_MAX_CHARS = 14_000
LOCAL_OUTPUT_MAX_CHARS = 32_000
MCP_MAX_CHARS = 6_000
