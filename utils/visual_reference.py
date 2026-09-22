"""Reference matching on source text, not serialized processing metadata."""
import json
import re


def reference_keyword_hits(text, keywords):
    text = str(text or '')
    hits = []
    for keyword in keywords:
        if re.fullmatch(r'[A-Za-z]+', keyword):
            # ASCII boundaries allow Korean particles (UN의) and COP28, but
            # exclude unknown_unit, unit, community, EUROP(E), and JSON keys.
            found = re.search(r'(?<![A-Za-z0-9_])' + re.escape(keyword)
                              + r'(?![A-Za-z_])', text, re.IGNORECASE)
        else:
            found = keyword.casefold() in text.casefold()
        if found:
            hits.append(keyword)
    return hits


_SOURCE_FIELDS = {
    '항목', '항목원문', '값원문', '설명', '사업명', '감축사업명', '거버넌스기구',
    '지역', '지역명', '자료지역', '자료구분', '범례원문', '출처', '출처원문',
    '근거', '근거문구', '근거 문구', '지역근거', '참고자료근거', '제목', '캡션',
    '단위', '원문단위', '지표명', '역할', 'source', 'title', 'caption', 'summary',
}
_CONTAINERS = {'_reading', '문맥 구분', '객체문맥', 'fields', '메타데이터', '공통문맥'}


def _source_values(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _SOURCE_FIELDS:
                if isinstance(item, str):
                    yield item
            elif key in _CONTAINERS:
                yield from _source_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _source_values(item)


def observation_reference_text(observation):
    parts = [str(observation.get(k) or '') for k in
             ('제목', '캡션', '단위', '그래프유형', '참고자료근거')]
    parts.extend(_source_values(observation.get('판독필드')))
    evidence = observation.get('근거')
    if isinstance(evidence, dict):
        parts.extend(_source_values(evidence))
    elif isinstance(evidence, str):
        try:
            # ImageAgent appends machine gate reasons after the JSON payload.
            payload, _ = json.JSONDecoder().raw_decode(evidence.lstrip())
        except (ValueError, TypeError):
            parts.append(evidence)  # Legacy plain-language source evidence.
        else:
            parts.extend(_source_values(payload))
    return ' '.join(parts)
