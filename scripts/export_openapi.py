"""OpenAPI 스펙을 파일로 내보낸다. FE 는 이 파일로 TypeScript 타입을 생성한다.

uv run python scripts/export_openapi.py ../FE-Agent/openapi.json
"""

import json
import sys
from pathlib import Path

from be_agent.main import app

output = Path(sys.argv[1] if len(sys.argv) > 1 else "openapi.json")
output.write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2))
print(f"Wrote {output}")
