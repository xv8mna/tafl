"""tafl-app.exe of the Windows download: the window."""

import multiprocessing

from tafl.ui.app import main

multiprocessing.freeze_support()  # a plugin's worker processes start this program again
raise SystemExit(main())
