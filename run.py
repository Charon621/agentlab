import os
from pathlib import Path
from dotenv import load_dotenv
import uvicorn

if __name__ == "__main__":
    load_dotenv(Path(__file__).resolve().parent / ".env")
    uvicorn.run("agentlab.api:app", host=os.getenv("AGENTLAB_HOST", "127.0.0.1"), port=int(os.getenv("AGENTLAB_PORT", "8811")), workers=1)
