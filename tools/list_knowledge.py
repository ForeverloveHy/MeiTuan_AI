from pathlib import Path
import json
from sceg.schema_compiler import compile_state_graph
from sceg.schema import StateGraph

graph=StateGraph.from_dict(compile_state_graph(json.load(open('/mnt/data/final_iter/reports_merchant/graph.json',encoding='utf-8'))))
for k in graph.knowledge:
    if '标准' in k.name or '延迟' in k.name:
        print(k.id,k.name,k.aliases,k.value_check)
        print('selector',k.selector_element_groups)
