"""Run the same-origin Web distribution as a standalone service."""

import multiprocessing
import os
from pathlib import Path


def main() -> None:
    os.environ["FIN_WEB"] = "1"
    os.environ.setdefault("TQDM_DISABLE", "1")
    if not os.environ.get("FRONTEND_DIST"):
        bundled = Path(__file__).resolve().parents[1] / "fronted" / "dist"
        if (bundled / "index.html").is_file():
            os.environ["FRONTEND_DIST"] = str(bundled)

    import uvicorn
    from finresearch.config import settings
    from finresearch.main import app

    forwarded_ips = os.environ.get("FIN_FORWARDED_ALLOW_IPS") or settings.fin_forwarded_allow_ips
    uvicorn.run(
        app,
        host=os.environ.get("FIN_HOST") or settings.host,
        port=int(os.environ.get("PORT") or os.environ.get("FIN_PORT") or settings.port),
        workers=1,
        timeout_graceful_shutdown=20,
        proxy_headers=True,
        forwarded_allow_ips=forwarded_ips,
    )


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
