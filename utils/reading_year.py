"""Year-field aliases only. Short years require a local, explicit full-year anchor."""
import re

YEAR_FIELDS = frozenset(('연도', '기준연도', '목표연도', '점검연도', '기간시작', '기간종료'))


def full_year(value):
    if type(value) is int and 1000 <= value <= 9999:
        return value
    if isinstance(value, str) and re.fullmatch(r'\s*\d{4}\s*년?\s*', value):
        year = int(re.search(r'\d{4}', value).group())
        return year if year >= 1000 else None
    return None


def normalize_year(value, anchor=None):
    year = full_year(value)
    if year is not None:
        return year
    # Bare 24, numeric IDs, ranges and booleans are not year aliases.
    short = re.fullmatch(r"\s*['’‘ʼ]\s*(\d{2})\s*년?\s*", value) if isinstance(value, str) else None
    known = full_year(anchor)
    if short and known is not None and known % 100 == int(short.group(1)):
        return known
    return None


def same_year(left, right, *, allow_short=False):
    a, b = full_year(left), full_year(right)
    if allow_short:
        a = normalize_year(left, b)
        b = normalize_year(right, a)
    return a is not None and b is not None and a == b


def explicit_period(value, anchors=None):
    """Parse a complete range; short endpoints only confirm explicit anchors."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*(\d{4}|['’‘ʼ]\s*\d{2})\s*년?\s*[~～\-–]\s*"
                         r"(\d{4}|['’‘ʼ]\s*\d{2})\s*년?\s*(?:합계)?\s*", value)
    if not match:
        return None
    anchors = anchors or (None, None)
    start, end = [normalize_year(token, anchor) for token, anchor in zip(match.groups(), anchors)]
    return (start, end) if start is not None and end is not None and start <= end else None
