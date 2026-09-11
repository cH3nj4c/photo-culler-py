"""Export kept originals off the UI thread with progress and cancel support."""

from __future__ import annotations

import queue
import shutil
from pathlib import Path

from domain import PhotoGroup, selected_members, unique_destination
from workers import LatestOnlyWorker


class ExportService:
    """Copy selected originals; emit progress events for the UI poll loop.

    Event shape: ``(generation, copied, total, failures, done, cancelled)``
    """

    def __init__(self) -> None:
        self.events: queue.Queue = queue.Queue()
        self._generation = 0
        self._cancel_requested = False
        self._worker = LatestOnlyWorker("photo-culler-export")

    @property
    def generation(self) -> int:
        return self._generation

    def plan(
        self,
        kept_items: list[PhotoGroup],
        pair_modes: dict[str, str],
    ) -> list[Path]:
        sources: list[Path] = []
        for item in kept_items:
            sources.extend(selected_members(item, pair_modes.get(item.key, "both")))
        return sources

    def start(self, sources: list[Path], destination: Path) -> int:
        self._cancel_requested = False
        self._generation += 1
        generation = self._generation
        self.events.put((generation, 0, len(sources), [], False, False))
        self._worker.submit(generation, self._run, sources, destination)
        return generation

    def cancel(self) -> None:
        """Request stop; the running job will emit a cancelled-done event."""
        self._cancel_requested = True

    def _run(self, generation: int, sources: list[Path], destination: Path) -> None:
        copied = 0
        failures: list[str] = []
        total = len(sources)
        for source in sources:
            if self._cancel_requested or generation != self._generation:
                self.events.put((generation, copied, total, failures, True, True))
                return
            try:
                target = unique_destination(destination, source.name)
                shutil.copy2(source, target)
                copied += 1
            except OSError as exc:
                failures.append(f"{source.name}: {exc}")
            self.events.put((generation, copied, total, failures, False, False))
        self.events.put((generation, copied, total, failures, True, False))

    def close(self) -> None:
        self.cancel()
        self._worker.close()
