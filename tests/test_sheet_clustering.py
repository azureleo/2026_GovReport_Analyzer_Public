from __future__ import annotations

import config
from agents import extractor_agent as extractor_module
from agents.extractor_agent import ExtractorAgent
from utils.pdf_reader import PageContent


EXPECTED_CLUSTERS = [
    ["document_meta", "plan_overview"],
    ["regional_conditions"],
    ["emissions_regional", "emissions_management"],
    ["emissions_forecast"],
    ["reduction_targets"],
    ["vision_strategy"],
    ["mitigation_projects", "annual_implementation"],
    ["quantitative_reductions", "financial_plan"],
    ["foundation_measures"],
    ["governance_feedback", "monitoring_performance", "changes_actions"],
]


def _page(page_number: int) -> PageContent:
    return PageContent(
        page_number=page_number,
        text=f"페이지 {page_number} 본문",
        tables=[],
        images=[],
    )


def test_sheet_clusters_match_conservative_ab_contract() -> None:
    assert config.EXTRACTION_SHEET_CLUSTERS == EXPECTED_CLUSTERS
    assert max(map(len, config.EXTRACTION_SHEET_CLUSTERS)) == 3

    flattened = [sheet for cluster in config.EXTRACTION_SHEET_CLUSTERS for sheet in cluster]
    assert len(flattened) == len(set(flattened))
    assert set(flattened) == set(config.EXTRACTION_SHEETS)


def test_singleton_groups_use_sheet_path_and_combined_groups_use_cluster_path(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        config,
        "EXTRACTION_SHEET_CLUSTERS",
        [["document_meta", "plan_overview"], ["regional_conditions"]],
    )
    monkeypatch.setattr(config, "TEXT_QUEUE_DEDUP_ENABLED", True)
    monkeypatch.setattr(config, "TEXT_WORKERS", 1)
    monkeypatch.setattr(
        extractor_module,
        "parallel_map_collect",
        lambda worker, tasks, **_: [(worker(task), None) for task in tasks],
    )

    calls: list[tuple[str, tuple[str, ...]]] = []
    agent = ExtractorAgent()

    def run_cluster(task, municipality, prompts):
        calls.append(("cluster", tuple(task["members"])))
        return {
            sheet: [{"항목": sheet, "출처페이지": task["page_nums"]}]
            for sheet in task["members"]
        }

    def run_sheet(task, municipality):
        calls.append(("sheet", (task["sheet_key"],)))
        return [{"항목": task["sheet_key"], "출처페이지": task["page_nums"]}]

    monkeypatch.setattr(agent, "_run_cluster_task", run_cluster)
    monkeypatch.setattr(agent, "_run_sheet_task", run_sheet)

    shared_page = _page(1)
    standalone_page = _page(2)
    agent._extract_clustered(
        {
            "document_meta": [shared_page],
            "plan_overview": [shared_page],
            "regional_conditions": [standalone_page],
        },
        "가상시",
        {
            "document_meta": "문서 지침",
            "plan_overview": "개요 지침",
            "regional_conditions": "지역 지침",
        },
        batch_size=15,
    )

    assert calls == [
        ("cluster", ("document_meta", "plan_overview")),
        ("sheet", ("regional_conditions",)),
    ]
    assert len(agent._raw_results["document_meta"]) == 1
    assert len(agent._raw_results["plan_overview"]) == 1
    assert len(agent._raw_results["regional_conditions"]) == 1


def test_missing_cluster_members_fall_back_to_singleton_sheet_path(monkeypatch) -> None:
    monkeypatch.setattr(config, "EXTRACTION_SHEET_CLUSTERS", [["document_meta"]])
    monkeypatch.setattr(config, "TEXT_QUEUE_DEDUP_ENABLED", True)
    monkeypatch.setattr(config, "TEXT_WORKERS", 1)
    monkeypatch.setattr(
        extractor_module,
        "parallel_map_collect",
        lambda worker, tasks, **_: [(worker(task), None) for task in tasks],
    )

    called: list[str] = []
    agent = ExtractorAgent()

    def run_sheet(task, municipality):
        called.append(task["sheet_key"])
        return []

    monkeypatch.setattr(agent, "_run_sheet_task", run_sheet)
    agent._extract_clustered(
        {"document_meta": [_page(1)], "changes_actions": [_page(2)]},
        "가상시",
        {},
        batch_size=15,
    )

    assert called == ["document_meta", "changes_actions"]
