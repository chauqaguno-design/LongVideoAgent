"""Start the local workbench with python -m cinetrace."""

import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv


if __name__ == "__main__":
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    uvicorn.run("cinetrace.app:app", host="127.0.0.1",
                port=int(os.getenv("CINETRACE_PORT", "8791")))
