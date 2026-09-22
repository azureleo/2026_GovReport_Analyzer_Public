"""Lossless position validation and conservative same-cell identity."""
import math


def coordinate_key(value):
    if isinstance(value, str):
        return ('text', value)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return ('number', value)
    if isinstance(value, list) and value:
        return ('path', tuple(coordinate_key(part) for part in value))
    raise ValueError('Expected a string, finite number, or nonempty coordinate path')


def same_cell_key(object_id, position):
    if not isinstance(position, dict):
        return None
    row, col = position.get('데이터행'), position.get('데이터열')
    if row is None or col is None:
        return None
    keys = (coordinate_key(row), coordinate_key(col))
    if isinstance(row, list) or isinstance(col, list):
        # Header paths can repeat in different physical cells. Do not infer
        # co-location from labels alone; explicit relations still work.
        case_id = position.get('case_id')
        if not isinstance(case_id, str) or not case_id.strip():
            return None
        page = position.get('PDF 실제 페이지')
        page_key = None if page is None else coordinate_key(page)
        return (object_id, 'path_cell', page_key, case_id, keys)
    return (object_id, 'scalar_cell', keys)
