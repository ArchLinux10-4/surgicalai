"""The one closing line on a tool result."""


def tool_footer(kind: str, result: str, continues_at: int | None = None,
                start_line: int | None = None, end_line: int | None = None,
                total: int | None = None) -> str:
    """Result text, an optional window note, then Continue.

    A bare command still gets its stop line from `_terminal_followup`.
    Nothing else tells the model the turn is over.
    """
    del kind  # reserved so callers name the tool; the line is the same
    text = (result or "").rstrip()
    if continues_at and start_line and end_line and total:
        text += (
            f"\nLines {start_line}-{end_line} of {total}. "
            f"Continue from line {continues_at}."
        )
    return text + "\nContinue."
