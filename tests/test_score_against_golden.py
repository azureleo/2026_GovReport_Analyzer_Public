"""골든셋 대조 채점기 회귀 테스트."""
from __future__ import annotations

from agents import organizer_agent


def test_organizer_public_aliases_are_existing_private_functions() -> None:
    """Given 기존 정규화 함수 When 공개 별칭을 확인하면 Then 같은 함수 객체다."""
    assert organizer_agent.dedup_key_text is organizer_agent._dedup_key_text
    assert organizer_agent.normalize_project_id is organizer_agent._normalize_project_id
    assert organizer_agent.to_float is organizer_agent._to_float
    assert organizer_agent.normalize_provenance_pages is organizer_agent._normalize_provenance_pages
