"""Selection snapshots independent of GTK widgets and persistence."""

from dataclasses import dataclass, field

from dvdvideo.gui_preferences import SourcePreferences


@dataclass
class SelectionState:
    loaded_mode: bool | None = None
    omitted_ids: set[str] = field(default_factory=set[str])
    omitted_streams: dict[str, set[str]] = field(default_factory=dict[str, set[str]])

    def clear(self) -> None:
        self.loaded_mode = None
        self.omitted_ids.clear()
        self.omitted_streams.clear()

    def snapshot(
        self, previous: SourcePreferences | None, *, is_bluray: bool,
        selected: list[str], inspected_streams: dict[str, list[str]],
        profile_name: str, output_format: str, minimum_minutes: int,
    ) -> SourcePreferences:
        listing_loaded = self.loaded_mode is not None and self.loaded_mode == is_bluray
        previous = previous or SourcePreferences(dvd_inspected=False)
        saved = previous.playlists if is_bluray else previous.dvd_streams
        selected = list(selected) + [
            identifier for identifier in saved
            if identifier in self.omitted_ids and identifier not in selected
        ]
        streams = {key: list(value) for key, value in previous.playlist_streams.items()}
        if listing_loaded and is_bluray:
            for key, value in inspected_streams.items():
                streams[key] = list(value) + [
                    identifier for identifier in previous.playlist_streams.get(key, [])
                    if identifier in self.omitted_streams.get(key, set()) and identifier not in value
                ]
        return SourcePreferences(
            profile_name=profile_name, output_format=output_format,
            minimum_minutes=minimum_minutes,
            dvd_streams=list(selected) if listing_loaded and not is_bluray else list(previous.dvd_streams),
            playlists=list(selected) if listing_loaded and is_bluray else list(previous.playlists),
            playlist_streams=streams,
            dvd_inspected=previous.dvd_inspected or (listing_loaded and not is_bluray),
        )
