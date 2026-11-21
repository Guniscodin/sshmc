"""Start the SSHMC panel bound to 127.0.0.1 only (localhost)."""

import uvicorn

if __name__ == "__main__":
    uvicorn.run("mcpanel.main:app", host="127.0.0.1", port=8000, reload=False)
