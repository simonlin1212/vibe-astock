"""Product identity shared with the web UI; independent of engine versions."""
import json
from pathlib import Path

PRODUCT = json.loads((Path(__file__).resolve().parents[1] / "product.json").read_text(encoding="utf-8"))
PRODUCT_NAME = PRODUCT["name"]
PRODUCT_VERSION = PRODUCT["version"]
