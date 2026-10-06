"""Load only the canonical local corpus, validated against approved knowledge."""
from hashlib import sha256
import json
from pathlib import Path

from app.ai.knowledge import KnowledgeBase, load_yaml_model
from app.quality.models import Dataset

DATASET_PATH = Path("config/ai_quality_cases.yaml")


def fingerprint(model) -> str:
    def canonical(value):
        if isinstance(value, dict):
            return {key: canonical(item) for key, item in value.items()}
        if isinstance(value, (set, frozenset)):
            return sorted(canonical(item) for item in value)
        if isinstance(value, (list, tuple)):
            return [canonical(item) for item in value]
        return value
    data = json.dumps(canonical(model.model_dump()), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return sha256(data.encode("utf-8")).hexdigest()


def load_dataset(knowledge: KnowledgeBase, max_comment_chars: int, path: Path = DATASET_PATH) -> Dataset:
    dataset = load_yaml_model(path, Dataset)
    for case in dataset.cases:
        if any(knowledge.find_fact(id) is None for id in case.expected.fact_ids):
            raise ValueError("Unknown expected fact ID")
        if len(case.viewer_text) > max_comment_chars:
            raise ValueError("Evaluation comment exceeds configured limit")
    return dataset
