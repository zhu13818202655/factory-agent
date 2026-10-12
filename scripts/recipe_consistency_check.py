"""Phase 0 一致性自检：配方 metric 引用必须与指标注册表对齐。"""
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from factory_agent.data_api.catalog import load_catalog
from factory_agent.execution.recipes import load_recipes
from factory_agent.execution.result_table import default_metric_registry


def main() -> int:
    catalog = load_catalog()
    recipes = load_recipes(catalog)
    print("RecipeRegistry:", type(recipes).__name__)

    reg = default_metric_registry()
    reg_names = set()
    for (name, version) in list(getattr(reg, "_metrics", {}).keys()):
        reg_names.add((name, version))
    print("registered metrics:", len(reg_names))

    problems: list[str] = []
    recipe_dir = Path(__file__).resolve().parents[1] / "configs" / "knowledge" / "recipes"
    for path in sorted(recipe_dir.glob("*.yaml")):
        data = yaml.safe_load(path.read_text())
        for cap in data.get("capabilities", []):
            cid = cap["capability_id"]
            vers = cap.get("metric_versions", {})
            for col in cap.get("result_columns", []):
                metric = col.get("metric")
                if not metric:
                    continue
                version = vers.get(metric)
                if version is None:
                    problems.append(
                        f"{path.name}:{cid} 列 {col['name']} metric={metric} 缺 metric_versions"
                    )
                elif (metric, version) not in reg_names:
                    problems.append(
                        f"{path.name}:{cid} metric_versions {metric}:{version} 未注册"
                    )
    print("问题数:", len(problems))
    for p in problems:
        print(" -", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
