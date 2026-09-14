"""Keep generated artifacts in this project, independent of the startup folder."""
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent


def runtime_path(directory: str, filename: str) -> Path:
    """Create the output directory on demand, including on a fresh checkout."""
    target_dir = PROJECT_ROOT / directory
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / filename
