"""Smoke test: boot the Streamlit app headlessly and assert it renders.

Not part of the pytest suite because it needs Streamlit's test harness and a
few seconds to run. Useful before deploying:

    python apptest_run.py
"""

from __future__ import annotations

import sys

from streamlit.testing.v1 import AppTest


def main() -> int:
    app = AppTest.from_file("app.py", default_timeout=300)
    app.run()

    if app.exception:
        print("FAILED — the app raised on startup:")
        for exception in app.exception:
            print(f"  {exception.value}")
        return 1

    uploaders = len(app.sidebar.get("file_uploader") or [])
    print(f"titles      : {[t.value for t in app.title]}")
    print(f"uploaders   : {uploaders}")
    print(f"sliders     : {[(s.label, s.value) for s in app.sidebar.slider]}")
    print(f"buttons     : {[b.label for b in app.button]}")

    if uploaders != 4:
        print(f"FAILED — expected 4 file uploaders, found {uploaders}")
        return 1

    print("\nOK — app boots cleanly with all upload widgets present.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
