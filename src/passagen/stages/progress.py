from collections.abc import Callable

type ProgressCallback = Callable[[str], None]


def report_progress(progress: ProgressCallback | None, message: str) -> None:
    if progress is not None:
        progress(message)
