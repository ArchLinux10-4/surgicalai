from dataclasses import dataclass


@dataclass(frozen=True)
class CatalogModel:
    id: str
    label: str
    description: str
    size_gb: float
    uncensored: bool = False


CATALOG = (
    CatalogModel(
        "qwen2.5-coder:7b",
        "Qwen2.5-Coder 7B",
        "Code-focused local model. Fast, the current default.",
        4.7,
    ),
    CatalogModel(
        "hf.co/RootMonsteR/Qwen3-14B-Abliterated-GGUF:Q5_K_M",
        "Qwen3 14B Uncensored",
        "Uncensored (abliterated) general model. About 90% fewer refusals than stock Qwen3.",
        10.5,
        True,
    ),
)
_UNCENSORED_REPO = "hf.co/rootmonster/qwen3-14b-abliterated-gguf"


def lookup(name: str):
    n = (name or "").strip().lower()
    return next((m for m in CATALOG if m.id.lower() == n), None)


def is_uncensored(model: str) -> bool:
    return (model or "").strip().lower().startswith(_UNCENSORED_REPO)
