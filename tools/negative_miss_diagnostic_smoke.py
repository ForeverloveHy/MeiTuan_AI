from __future__ import annotations

from sceg.dataset_interface import DatasetInterface


class DummyReq:
    requirement_id = 'a1'
    text = '询问是否可以处理'
    required = True
    score = 0.0
    matched = False
    aliases = []
    element_audit = {'verdict': 'miss', 'hit_elements': [], 'missing_elements': [{'value': '询问'}], 'candidate_results': []}
    group_matches = []


class DummyNode:
    node_id = 'n1'
    name = '确认处理意愿'
    active = True
    status = '缺失'
    aliases = []
    score = 0.0
    requirement_results = [DummyReq()]
    group_matches = []


class DummyEval:
    node_results = [DummyNode()]
    knowledge_events = []
    knowledge_checks = []
    constraint_events = []
    constraint_checks = []
    relation_events = []
    context_events = []
    evidence_units = []


def main() -> None:
    di = DatasetInterface({'thresholds': {}})
    err = {'error_family': 'flow_missing', 'description': '没有询问是否可以处理'}
    diag = di._diagnose_expected_miss(err, DummyEval(), {})
    assert diag['likely_root_cause']
    assert diag['closest_flow_targets']
    assert diag['closest_flow_targets'][0]['element_audit_digest']['missing_elements'] == ['询问']
    print('negative miss diagnostic smoke passed')


if __name__ == '__main__':
    main()
