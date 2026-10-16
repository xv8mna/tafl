"""tafl-app.exe of the Windows download: the window."""

import multiprocessing

from etl_tool.ui.app import main

multiprocessing.freeze_support()  # a plugin's worker processes start this program again
raise SystemExit(main())
