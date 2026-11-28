# @copyright: 2026 libertypo <libertypo@proton.me>
# @license: GNU GPL-3
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

# Keep ValueError for invalid data and the GUI's validation contract.
# ruff: noqa: TRY004

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from dvdvideo.gui_preferences_io import (
    MAX_PREFERENCES_BYTES,
    PreferencesDurabilityError,
    PreferenceSnapshot,
    write_preferences,
)
from dvdvideo.gui_support import absolute_path

DEFAULT_PROFILE_NAME = "Default"
VALID_OUTPUT_FORMATS = frozenset({"auto", "mkv", "m2ts"})
VALID_GUI_LANGUAGES = frozenset({"system", "en", "it_IT", "ro_RO"})
LANGUAGE_CODE = re.compile(r"[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8})*")
STREAM_PID = re.compile(r"0[xX]0{0,4}[0-9a-fA-F]{1,4}")
MAX_STREAM_PID = 0x1FFF
ISO_LANGUAGE_CODES = Path("/usr/share/iso-codes/json/iso_639-2.json")


def _new_string_list() -> list[str]:
    return []


def _new_string_list_map() -> dict[str, list[str]]:
    return {}


@dataclass(frozen=True)
class TrackInfo:
    identifier: str
    kind: str
    language: str


@dataclass(frozen=True)
class Profile:
    name: str = DEFAULT_PROFILE_NAME
    audio_languages: tuple[str, ...] = ()
    subtitle_languages: tuple[str, ...] = ()
    output_format: str = "auto"
    minimum_minutes: int = 5


@dataclass
class SourcePreferences:
    profile_name: str = DEFAULT_PROFILE_NAME
    output_format: str = "auto"
    minimum_minutes: int = 5
    dvd_streams: list[str] = field(default_factory=_new_string_list)
    playlists: list[str] = field(default_factory=_new_string_list)
    playlist_streams: dict[str, list[str]] = field(
        default_factory=_new_string_list_map
    )
    # Older records already contain explicit DVD selections, including empty.
    dvd_inspected: bool = True
    dvd_title: str = "1"


def _numeric_id(value: str) -> str:
    return str(int(value)) if re.fullmatch(r"[0-9]{1,5}", value) else value


def normalize_stream_pid(value: str) -> str:
    if STREAM_PID.fullmatch(value) is None or int(value, 16) > MAX_STREAM_PID:
        raise ValueError(f"Invalid stream PID: {value!r}")
    return f"0x{int(value, 16):04x}"


def _normalize_source(preferences: SourcePreferences) -> SourcePreferences:
    streams: dict[str, list[str]] = {}
    for playlist, pids in preferences.playlist_streams.items():
        key = _numeric_id(playlist)
        streams[key] = list(dict.fromkeys(
            streams.get(key, []) + [normalize_stream_pid(pid) for pid in pids]
        ))
    return replace(
        preferences,
        dvd_streams=list(
            dict.fromkeys(
                _numeric_id(value) for value in preferences.dvd_streams
            )
        ),
        playlists=list(
            dict.fromkeys(
                _numeric_id(value) for value in preferences.playlists
            )
        ),
        playlist_streams=streams,
    )


def _copy_source(preferences: SourcePreferences) -> SourcePreferences:
    return replace(
        preferences,
        dvd_streams=list(preferences.dvd_streams),
        playlists=list(preferences.playlists),
        playlist_streams={
            key: list(value)
            for key, value in preferences.playlist_streams.items()
        },
    )


def parse_language_codes(value: str) -> tuple[str, ...]:
    codes: list[str] = []
    for candidate in value.split(","):
        code = candidate.strip().lower()
        if not code:
            continue
        if LANGUAGE_CODE.fullmatch(code) is None:
            raise ValueError(f"Invalid language code: {candidate.strip()!r}")
        if code not in codes:
            codes.append(code)
    return tuple(codes)


@lru_cache(maxsize=1)
def language_aliases() -> dict[str, str]:
    try:
        data: object = json.loads(
            ISO_LANGUAGE_CODES.read_text(encoding="utf-8")
        )
        if not isinstance(data, dict):
            raise ValueError("expected an object")
        records: object = cast(dict[str, object], data).get("639-2")
        if not isinstance(records, list):
            raise ValueError("expected a 639-2 list")
        aliases: dict[str, str] = {}
        for value in cast(list[object], records):
            if not isinstance(value, dict):
                raise ValueError("expected a language record")
            record = cast(dict[str, object], value)
            canonical = record.get("alpha_3")
            if (
                canonical == "qaa-qtz"
                and "alpha_2" not in record
                and "bibliographic" not in record
            ):
                continue
            if (
                not isinstance(canonical, str)
                or re.fullmatch(r"[a-z]{3}", canonical) is None
            ):
                raise ValueError("invalid three-letter language code")
            for key in ("alpha_2", "bibliographic"):
                alias = record.get(key)
                if alias is not None:
                    if (
                        not isinstance(alias, str)
                        or re.fullmatch(r"[a-z]{2,3}", alias) is None
                    ):
                        raise ValueError("invalid language alias")
                    aliases[alias] = canonical
        if not aliases:
            raise ValueError("no language aliases")
        return aliases
    except (OSError, ValueError) as error:
        raise ValueError(
            f"Cannot load ISO language codes from {ISO_LANGUAGE_CODES}:"
            f" {error}"
        ) from error


def canonical_language(code: str, aliases: dict[str, str]) -> str:
    primary, separator, suffix = code.lower().partition("-")
    return aliases.get(primary, primary) + separator + suffix


def select_tracks(profile: Profile, tracks: list[TrackInfo]) -> list[str]:
    if not profile.audio_languages and not profile.subtitle_languages:
        return []
    aliases = language_aliases()
    audio_languages = {
        canonical_language(language, aliases)
        for language in profile.audio_languages
    }
    subtitle_languages = {
        canonical_language(language, aliases)
        for language in profile.subtitle_languages
    }
    if not audio_languages and not subtitle_languages:
        return []
    selected: list[str] = []
    for track in tracks:
        kind = track.kind.lower()
        language = canonical_language(track.language, aliases)
        if (
            kind == "video"
            or (kind == "audio" and language in audio_languages)
            or (
                kind in ("subtitle", "subtitles")
                and language in subtitle_languages
            )
        ):
            selected.append(track.identifier)
    return selected


def _preferences_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    candidate = Path(config_home) if config_home else None
    root = (
        candidate
        if candidate is not None and candidate.is_absolute()
        else Path.home() / ".config"
    )
    return root / "dvd-to-mkv-gui" / "preferences.json"


def reusable_source(source: str) -> bool:
    # A device path identifies a drive, not the disc currently inserted in it.
    path = os.path.abspath(source)
    return path != "/dev" and not path.startswith("/dev/")


class PreferencesStore:
    MAX_PREFERENCES_BYTES = MAX_PREFERENCES_BYTES

    def __init__(
        self, path: Path | None = None,
        writer: Callable[[Path, PreferenceSnapshot], None] | None = None,
    ) -> None:
        self.path = path or _preferences_path()
        self.writer = writer
        self.profiles: dict[str, Profile] = {
            DEFAULT_PROFILE_NAME: Profile(),
        }
        self.active_profile = DEFAULT_PROFILE_NAME
        self.gui_language = "system"
        self.legal_notice_seen = False
        self._sources: dict[str, SourcePreferences] = {}
        self.device_sources: set[str] = set()
        language_aliases()
        self._load()

    @staticmethod
    def source_key(source: str) -> str:
        path = absolute_path(source)
        if ".." not in path.split(os.sep):
            path = os.path.normpath(path)
        return os.path.normcase(path)

    @property
    def profile_names(self) -> list[str]:
        return list(self.profiles)

    @property
    def sources(self) -> dict[str, SourcePreferences]:
        return {
            key: _copy_source(value) for key, value in self._sources.items()
        }

    def get_profile(self, name: str | None = None) -> Profile:
        selected_name = name or self.active_profile
        return self.profiles.get(
            selected_name, self.profiles[DEFAULT_PROFILE_NAME]
        )

    def save_profile(
        self,
        profile: Profile,
        *,
        previous_name: str | None = None,
        activate: bool = False,
    ) -> None:
        name = _validated_string(profile.name, "Profile name").strip()
        _validate_profile_name(name)
        output_format = _validated_string(
            profile.output_format, "Output format"
        )
        if output_format not in VALID_OUTPUT_FORMATS:
            raise ValueError(
                f"Unsupported output format: {profile.output_format}"
            )
        minimum_minutes = _validated_minutes(profile.minimum_minutes)
        profile = Profile(
            name,
            parse_language_codes(",".join(profile.audio_languages)),
            parse_language_codes(",".join(profile.subtitle_languages)),
            output_format,
            minimum_minutes,
        )
        if previous_name is not None and previous_name not in self.profiles:
            raise ValueError(f"Profile does not exist: {previous_name}")
        renaming = previous_name is not None and previous_name != name
        if renaming and previous_name == DEFAULT_PROFILE_NAME:
            raise ValueError(
                "The Default profile cannot be renamed; use New to create"
                " another profile."
            )
        if renaming and name in self.profiles:
            raise ValueError(
                "A profile with that name already exists; select it to"
                " edit it."
            )
        profiles = self.profiles.copy()
        active_profile = self.active_profile
        sources = self._sources.copy()
        if renaming:
            assert previous_name is not None
            del self.profiles[previous_name]
            if self.active_profile == previous_name:
                self.active_profile = name
            for key, source in self._sources.items():
                if source.profile_name == previous_name:
                    self._sources[key] = replace(source, profile_name=name)
        self.profiles[name] = profile
        if activate:
            self.active_profile = name
        try:
            self._write()
        except PreferencesDurabilityError:
            raise
        except Exception:
            self.profiles = profiles
            self.active_profile = active_profile
            self._sources = sources
            raise

    def delete_profile(self, name: str) -> None:
        if name == DEFAULT_PROFILE_NAME:
            raise ValueError("The Default profile cannot be deleted.")
        if name not in self.profiles:
            raise ValueError(f"Profile does not exist: {name}")
        profiles = self.profiles.copy()
        active_profile = self.active_profile
        sources = self._sources.copy()
        del self.profiles[name]
        if self.active_profile == name:
            self.active_profile = DEFAULT_PROFILE_NAME
        for key, source in self._sources.items():
            if source.profile_name == name:
                self._sources[key] = replace(
                    source, profile_name=DEFAULT_PROFILE_NAME
                )
        try:
            self._write()
        except PreferencesDurabilityError:
            raise
        except Exception:
            self.profiles = profiles
            self.active_profile = active_profile
            self._sources = sources
            raise

    def set_active_profile(self, name: str) -> None:
        if name not in self.profiles:
            raise ValueError(f"Profile does not exist: {name}")
        previous = self.active_profile
        self.active_profile = name
        try:
            self._write()
        except PreferencesDurabilityError:
            raise
        except Exception:
            self.active_profile = previous
            raise

    def set_gui_language(self, language: str) -> None:
        language = _validated_string(language, "GUI language")
        if language not in VALID_GUI_LANGUAGES:
            raise ValueError(f"Unsupported GUI language: {language}")
        previous = self.gui_language
        self.gui_language = language
        try:
            self._write()
        except PreferencesDurabilityError:
            raise
        except Exception:
            self.gui_language = previous
            raise

    def mark_legal_notice_seen(self) -> None:
        previous = self.legal_notice_seen
        self.legal_notice_seen = True
        try:
            self._write()
        except PreferencesDurabilityError:
            raise
        except Exception:
            self.legal_notice_seen = previous
            raise

    def get_source(self, source: str) -> SourcePreferences | None:
        if (
            not reusable_source(source)
            or self.source_key(source) in self.device_sources
        ):
            return None
        preferences = self._sources.get(self.source_key(source))
        return _copy_source(preferences) if preferences is not None else None

    def exclude_device_source(self, source: str) -> None:
        self.device_sources.add(self.source_key(source))

    def save_source(self, source: str, preferences: SourcePreferences) -> None:
        profile_name = _validated_string(
            preferences.profile_name, "profile_name"
        )
        if profile_name not in self.profiles:
            raise ValueError(f"Profile does not exist: {profile_name}")
        output_format = _validated_string(
            preferences.output_format, "output_format"
        )
        if output_format not in VALID_OUTPUT_FORMATS:
            raise ValueError(
                f"Unsupported output format: {preferences.output_format}"
            )
        _validated_minutes(preferences.minimum_minutes)
        _json_string_list(preferences.dvd_streams, "dvd_streams")
        _json_string_list(preferences.playlists, "playlists")
        _string_list_map(
            {"playlist_streams": preferences.playlist_streams},
            "playlist_streams",
        )
        _optional_bool(
            {"dvd_inspected": preferences.dvd_inspected}, "dvd_inspected", True
        )
        if re.fullmatch(r"[1-9][0-9]?", preferences.dvd_title) is None:
            raise ValueError("Invalid DVD title.")
        preferences = _normalize_source(preferences)

        key = self.source_key(source)
        if not reusable_source(source) or key in self.device_sources:
            if key in self._sources:
                previous = self._sources.pop(key)
                try:
                    self._write()
                except PreferencesDurabilityError:
                    raise
                except Exception:
                    self._sources[key] = previous
                    raise
            return
        previous = self._sources.get(key)
        self._sources[key] = preferences
        try:
            self._write()
        except PreferencesDurabilityError:
            raise
        except Exception:
            if previous is None:
                del self._sources[key]
            else:
                self._sources[key] = previous
            raise

    def _load(self) -> None:
        descriptor = -1
        try:
            descriptor = os.open(
                self.path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC,
            )
        except FileNotFoundError:
            return
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("preferences path is not a regular file")
            if info.st_size > self.MAX_PREFERENCES_BYTES:
                raise ValueError("preferences file exceeds the 4 MiB limit")
            with os.fdopen(descriptor, "rb") as file:
                descriptor = -1
                content = file.read(self.MAX_PREFERENCES_BYTES + 1)
            if len(content) > self.MAX_PREFERENCES_BYTES:
                raise ValueError("preferences file exceeds the 4 MiB limit")
            text = content.decode("utf-8")
        except (OSError, ValueError) as error:
            raise ValueError(
                f"Cannot read GUI preferences at {self.path}: {error}"
            ) from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        try:
            data = json.loads(text)
            self._read_data(data)
        except (
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            raise ValueError(
                f"Cannot read GUI preferences at {self.path}: {error}"
            ) from error

    def _read_data(self, data: Any) -> None:
        data_object = _json_object(data)
        if data_object.get("version") != 1:
            raise ValueError("unsupported preferences format")
        profiles = _json_list(data_object.get("profiles"))
        sources = _json_object(data_object.get("sources"))
        loaded_profiles = {DEFAULT_PROFILE_NAME: Profile()}
        for value in profiles:
            item = _json_object(value)
            profile = Profile(
                name=_required_str(item, "name"),
                audio_languages=_language_list(item, "audio_languages"),
                subtitle_languages=_language_list(item, "subtitle_languages"),
                output_format=_required_str(item, "output_format"),
                minimum_minutes=_required_int(item, "minimum_minutes"),
            )
            _validate_profile_name(profile.name)
            if (
                profile.output_format not in VALID_OUTPUT_FORMATS
                or not 0 <= profile.minimum_minutes <= 600
            ):
                raise ValueError(
                    f"invalid settings in profile {profile.name!r}"
                )
            loaded_profiles[profile.name] = profile
        self.profiles = loaded_profiles
        active: Any = data_object.get("active_profile", DEFAULT_PROFILE_NAME)
        if not isinstance(active, str):
            raise ValueError("active_profile must be a string")
        self.active_profile = (
            active if active in loaded_profiles else DEFAULT_PROFILE_NAME
        )
        language = _optional_str(data_object, "gui_language", "system")
        if language not in VALID_GUI_LANGUAGES:
            raise ValueError("invalid GUI language")
        self.gui_language = language
        self.legal_notice_seen = _optional_bool(
            data_object, "legal_notice_seen", False
        )
        for key, value in sources.items():
            source_data = _json_object(value)
            settings = SourcePreferences(
                profile_name=_optional_str(
                    source_data, "profile_name", DEFAULT_PROFILE_NAME
                ),
                output_format=_optional_str(
                    source_data, "output_format", "auto"
                ),
                minimum_minutes=_optional_int(
                    source_data, "minimum_minutes", 5
                ),
                dvd_streams=_string_list(source_data, "dvd_streams"),
                playlists=_string_list(source_data, "playlists"),
                playlist_streams=_string_list_map(
                    source_data, "playlist_streams"
                ),
                dvd_inspected=_optional_bool(
                    source_data, "dvd_inspected", True
                ),
                dvd_title=_optional_str(source_data, "dvd_title", "1"),
            )
            if re.fullmatch(r"[1-9][0-9]?", settings.dvd_title) is None:
                raise ValueError(f"invalid DVD title for {key}")
            if (
                settings.output_format not in VALID_OUTPUT_FORMATS
                or not 0 <= settings.minimum_minutes <= 600
            ):
                raise ValueError(f"invalid source settings for {key}")
            if settings.profile_name not in loaded_profiles:
                settings.profile_name = DEFAULT_PROFILE_NAME
            self._sources[key] = _normalize_source(settings)

    def _write(self) -> None:
        active_profile = self.active_profile
        gui_language = self.gui_language
        legal_notice_seen = self.legal_notice_seen
        profiles = tuple(self.profiles.values())
        # Stored records are replaced, never mutated or exposed to callers.
        sources = self._sources.copy()

        def snapshot() -> dict[str, Any]:
            return {
                "version": 1,
                "active_profile": active_profile,
                "gui_language": gui_language,
                "legal_notice_seen": legal_notice_seen,
                "profiles": [
                    {
                        "name": profile.name,
                        "audio_languages": list(profile.audio_languages),
                        "subtitle_languages": list(profile.subtitle_languages),
                        "output_format": profile.output_format,
                        "minimum_minutes": profile.minimum_minutes,
                    }
                    for profile in profiles
                ],
                "sources": {
                    key: asdict(value) for key, value in sources.items()
                },
            }

        if self.writer is not None:
            self.writer(self.path, snapshot)
        else:
            write_preferences(self.path, snapshot())


def _required_str(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _validated_string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _validate_profile_name(name: str) -> None:
    if not name.strip() or len(name) > 80:
        raise ValueError("Profile name must contain 1 to 80 characters.")
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in name):
        raise ValueError("Profile name must not contain control characters.")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in name):
        raise ValueError("Profile name must not contain Unicode surrogates.")


def _validated_minutes(value: object) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 <= value <= 600
    ):
        raise ValueError(
            "Minimum playlist length must be an integer between 0 and 600."
        )
    return value


def _required_int(item: dict[str, Any], key: str) -> int:
    value = item.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return value


def _optional_str(item: dict[str, Any], key: str, default: str) -> str:
    value = item.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _optional_int(item: dict[str, Any], key: str, default: int) -> int:
    value = item.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return value


def _optional_bool(item: dict[str, Any], key: str, default: bool) -> bool:
    value = item.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _string_list(item: dict[str, Any], key: str) -> list[str]:
    return _json_string_list(item.get(key, []), key)


def _language_list(item: dict[str, Any], key: str) -> tuple[str, ...]:
    value = _string_list(item, key)
    return parse_language_codes(",".join(value))


def _string_list_map(item: dict[str, Any], key: str) -> dict[str, list[str]]:
    value = _json_object(item.get(key, {}))
    result: dict[str, list[str]] = {}
    for source, entries in value.items():
        result[source] = _json_string_list(entries, key)
    return result


def _json_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    object_value = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in object_value):
        raise ValueError("JSON object keys must be strings")
    return cast(dict[str, Any], value)


def _json_list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("expected a JSON list")
    return cast(list[Any], value)


def _json_string_list(value: Any, key: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list of strings")
    list_value = cast(list[object], value)
    if not all(isinstance(item, str) for item in list_value):
        raise ValueError(f"{key} must be a list of strings")
    return cast(list[str], value)
