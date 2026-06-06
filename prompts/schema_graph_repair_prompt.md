当前项目使用五步建图契约，不再使用旧式整体补图提示词。
1. 主图生成：schema_core_graph_prompt.md。
2. 知识表生成：schema_knowledge_table_prompt.md。
3. 限制表生成：schema_constraint_tables_prompt.md。
4. 一级元素生成：schema_atom_element_refinement_prompt.md。
5. 二级元素扩张：schema_element_expansion_prompt.md。
本文件只保留为兼容入口说明。整体补图不得回流旧 requirements/evidence_groups 路线。
