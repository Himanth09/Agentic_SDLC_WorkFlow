# Entry point for `python -m sdlc_agent`: import the CLI's main function.
from .cli import main

# Run the CLI and use its return value as the process exit code.
raise SystemExit(main())
