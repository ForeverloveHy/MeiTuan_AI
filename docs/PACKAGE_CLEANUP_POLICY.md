# 交付包清理策略

本版开始，`data/` 只保留按正负包分类的模拟对话数据：

```text
data/dialogues/positive_pack/<domain>/
data/dialogues/negative_pack/<domain>/
```

以下内容不再随交付包保留：

- `data/graphs*`、`data/generated*`：LongCat 建图或编译后的图；
- `data/outputs*`：评估报告、回归 JSON、HTML 报告；
- `data/simulated_longcat*`：我在沙盒中模拟 LongCat 的中间返回；
- `.pytest_cache`、`__pycache__`、临时调试目录；
- 旧版本补丁说明、旧运行报告、历史审计产物。

真实运行时产生的图和报告应写入项目根目录下的 `runs/`，或者由命令行参数显式指定到外部目录；不要再把生成产物预置在 `data/` 中。
