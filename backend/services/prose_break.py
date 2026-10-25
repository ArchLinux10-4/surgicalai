"""Keep agent turns from running together in the chat transcript."""


def separate_turn_prose(content: str, *, break_pending: bool, needs_break: bool) -> tuple:
    """Insert a blank line before the first prose of a new turn.

    Returns (text, break_pending, needs_break). A pending break is consumed
    on the first non-empty chunk. If that chunk already starts with a newline,
    or the previous visible text already ended with one, nothing is inserted.
    """
    if not content:
        return content, break_pending, needs_break
    out = content
    pending = break_pending
    if pending:
        pending = False
        if needs_break and not content.startswith("\n"):
            out = "\n\n" + content
    return out, pending, not out.endswith("\n")
