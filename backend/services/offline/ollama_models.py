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


def offline_model_choices(installed_names):
    """Models the chat picker can select while offline mode is on.

    The catalog is always included, even when nothing is downloaded yet.
    An empty Ollama tag list must not leave the picker blank.
    """
    installed = {str(n).strip().lower() for n in (installed_names or []) if str(n).strip()}
    models = []
    seen = set()
    for entry in CATALOG:
        seen.add(entry.id.lower())
        ready = entry.id.lower() in installed
        models.append({
            "id": f"ollama:{entry.id}",
            "name": entry.label,
            "role": "architect",
            "description": entry.description if ready else f"{entry.description} Not downloaded yet.",
            "provider": "ollama",
            "cost": 0,
            "installed": ready,
        })
    for name in installed_names or []:
        raw = str(name).strip()
        if not raw or raw.lower() in seen:
            continue
        seen.add(raw.lower())
        models.append({
            "id": f"ollama:{raw}",
            "name": raw,
            "role": "architect",
            "description": "Installed local model",
            "provider": "ollama",
            "cost": 0,
            "installed": True,
        })
    return models


def is_uncensored(model: str) -> bool:
    return (model or "").strip().lower().startswith(_UNCENSORED_REPO)
