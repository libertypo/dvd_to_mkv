#!/usr/bin/env bash

set -euo pipefail
# Keep captured metadata out of the environment even under bash -a.
set +a
# Keep the background launcher's PID tied to the session that setsid creates.
set +m
shopt -u nocasematch xpg_echo

usage() {
    cat <<'EOF'
Usage: dvd-to-mkv.sh --list-streams INPUT [TITLE_OR_PLAYLIST]
       dvd-to-mkv.sh --list-streams-tsv INPUT [TITLE_OR_PLAYLIST]
       dvd-to-mkv.sh --list-titles INPUT
       dvd-to-mkv.sh --disc-details INPUT [TITLE]
       dvd-to-mkv.sh [--stream INDEX[,INDEX...]]... [--retries N] INPUT OUTPUT [TITLE_OR_PLAYLIST]
       dvd-to-mkv.sh --bluray [--device DEVICE] [--keydb FILE] [--minlength MINUTES] --list-playlists
       dvd-to-mkv.sh --bluray [--device DEVICE] [--keydb FILE] --playlist ID --list-streams
       dvd-to-mkv.sh --bluray [--device DEVICE] [--keydb FILE] [--playlist ID] --disc-details
       dvd-to-mkv.sh --bluray [--device DEVICE] [--keydb FILE] [--playlist ID]... [--stream-id ID:PID]... [--output-format FORMAT] [--output-name NAME] [--playlist-output-name ID:FILENAME]... [--retries N] [OUTPUT]
       dvd-to-mkv.sh --arguments-file FILE

Remux a DVD title or Blu-ray playlist to Matroska without re-encoding.

Without --bluray, INPUT may be a DVD device, ISO image, or VIDEO_TS directory.
TITLE_OR_PLAYLIST defaults to 1.

Use --list-streams to print each stream's index, type, codec, language, and title.
Use --list-streams-tsv for the same DVD metadata as tab-separated values.
Use --disc-details for read-only JSON metadata (requires jq and, for DVDs,
python3; DVD title 1 by default).
Use --list-titles to list DVD titles with lsdvd.
If DVD inspection fails on an omitted title, lsdvd and jq can find one
alternative to try. Explicit titles and copy commands never switch titles.
Media inspection steps time out after 60 seconds by default
(DVD_BACKUP_INSPECTION_TIMEOUT, in seconds). Full disc copies are not timed out.
For Blu-ray, optionally add one --playlist ID for tracks and chapters;
without it, all playlist summaries are included, without a minimum-length filter.
Repeat --stream INDEX or pass comma-separated indexes to copy only selected streams
(for example, --stream 1,3,16). By default all streams copy.
For Blu-ray, --playlist ID --list-streams lists stream PIDs for one playlist.
Repeat --stream-id ID:PID to keep only those PIDs for that playlist (for example 0:0x1100).
Playlists without --stream-id selections copy all streams.
Use --list-playlists to show Blu-ray playlists at least MINUTES long
(0-4294967295, default: 2).
Repeat --playlist ID to copy selected Blu-ray playlists. OUTPUT defaults to the
current directory. With one playlist it may be a filename for the resolved
format; otherwise it must be an existing directory.
If a batch fails, completed copies are kept. Rerun only playlists not yet copied.
Use --keydb to set the AACS KEYDB.cfg; if omitted, one is auto-detected under ~/.config/aacs/,
otherwise bluray_copy/bluray_info fall back to their own default AACS database.
Use --retries N to retry a failed or invalid disc copy up to N extra times (0-9999, default: 0).
Use --output-name NAME with Blu-ray output directories to name a single playlist NAME.EXT,
or multiple playlists NAME-playlist-ID.EXT. Each playlist uses its resolved
extension (.mkv or .m2ts). An explicit output file takes precedence
and prints a warning if --output-name was also supplied.
Repeat --playlist-output-name ID:FILENAME to name individual Blu-ray playlist outputs in a directory.
Each filename must end in the playlist's resolved .mkv or .m2ts extension.
Playlist listings include a "# disc-name" metadata line when a disc name or UDF title exists.
3D discs include compatibility warnings; 3D stream inspection also requires
FFprobe with Blu-ray support, bluray_copy, and python3.
Use --output-format auto|mkv|m2ts for Blu-ray (default: auto).
Auto selects M2TS when MVC payload is detected in a playlist sample.
Neither MKV nor M2TS output is guaranteed to preserve a separate 3D dependent view.
If the Blu-ray reader cannot report the disc's 3D status, choose a format explicitly.
M2TS output does not support --stream-id filtering and requires a .m2ts filename or directory.
Blu-ray MKV copies embed the original playlist chapters. Original M2TS copies export
chapter markers to OUTPUT.m2ts.chapters.txt (OGM chapter format), without altering the video.
Blu-ray MKV audio/subtitle language tags are restored from playlist metadata by PID.
Copies require python3 for a conservative free-space check before each attempt.
The output filesystem must support hard links for safe publication; this is
checked before copying. GNU sync -f flushes published output before success.
Insufficient space or an unavailable estimate blocks copying.
Media inspection output and diagnostics are each limited to 16 MiB per command.

Still confused? Don't worry, so is everyone the first time. Run it once, read the error, repeat.
EOF
}

require_commands() {
    local command
    for command in "$@"; do
        if ! command -v "$command" >/dev/null 2>&1; then
            echo "Required command not found: $command" >&2
            exit 127
        fi
    done
}

if [[ ${1:-} == -h || ${1:-} == --help ]]; then
    usage
    exit 0
fi

require_commands dirname readlink

media_type=dvd
list_streams=false
list_streams_tsv=false
list_titles=false
disc_details=false
selected_streams=()
selected_stream_ids=()
declare -A seen_stream_ids=()
declare -A seen_streams=()
list_playlists=false
selected_playlists=()
declare -A seen_playlists=()
device=
output_name=
keydb=
output_format=auto
output_format_set=false
declare -A playlist_formats=()
declare -A playlist_names=()
declare -A gui_reviewed_formats=()
warning_3d='3D content reported by disc. MKV and M2TS copies may omit the dependent view; 3D preservation is not verified.'
minlength=5
minlength_set=false
retries=0
retries_set=false
temporary_output=
temporary_copy=
temporary_directory=
workspace_parent_fd=
workspace_parent=
publication_pending=false
dvd_output_committed=false
gui_dvd_batch=false
gui_output_mode=
gui_reviewed_directory=
dvd_batch_cancelled_after_publication=false
pending_output=
pending_chapter_sidecar=
pending_chapter_source=
inspection_pid=
inspection_timed_out=false
inspection_file=
inspection_error_file=
inspection_status_file=
arguments_snapshot=
completed_playlists=0
active_process_group=
copy_launching=false
copy_cancel_status=0
readonly MAX_ARGUMENT_FILE_BYTES=$((16 * 1024 * 1024))
readonly MAX_ARGUMENT_FILE_ARGUMENTS=425000
readonly MAX_INSPECTION_BYTES=$((16 * 1024 * 1024))
if ! IFS= read -r -d '' script_path < <(readlink -fz -- "$0") \
    || ! IFS= read -r -d '' module_directory < <(dirname -z -- "$script_path"); then
    printf 'Could not resolve the launcher directory: %s\n' "$0" >&2
    exit 1
fi
python_module_path=$module_directory
if [[ -n ${PYTHONPATH:-} ]]; then
    declare -a inherited_python_path_entries=()
    IFS=: read -r -a inherited_python_path_entries <<< "$PYTHONPATH"
    for python_path_entry in "${inherited_python_path_entries[@]}"; do
        if [[ -n $python_path_entry ]]; then
            python_module_path+=":$python_path_entry"
        fi
    done
fi
python_command=python3
if [[ -x $module_directory/dvdvideo-python ]]; then
    python_command=$module_directory/dvdvideo-python
fi

finish_inspection() {
    # timeout can exit before the group is empty, including if its grace delay fails.
    kill -KILL -- "-$inspection_pid" 2>/dev/null || true
    inspection_pid=
}

cleanup() {
    local status=$? cleanup_failed=false scratch
    if (( status != 0 && completed_playlists > 0 && completed_playlists < ${#selected_playlists[@]} )); then
        printf 'Batch incomplete: %d of %d playlists copied. Completed outputs were kept; rerun only playlists not yet copied.\n' \
            "$completed_playlists" "${#selected_playlists[@]}" >&2 || true
    fi
    if [[ -n $inspection_pid ]]; then
        kill -TERM "$inspection_pid" 2>/dev/null || true
        wait "$inspection_pid" 2>/dev/null || true
        finish_inspection
    fi
    if [[ -n $active_process_group ]]; then
        stop_active_process_group
    fi
    if [[ $publication_pending == true ]]; then
        # Remove only links created by this attempt, never a competing destination.
        if [[ ! -L $pending_output && $pending_output -ef $temporary_output ]]; then
            rm -f -- "$pending_output" || cleanup_failed=true
        fi
        if [[ -n $pending_chapter_sidecar && ! -L $pending_chapter_sidecar \
            && $pending_chapter_sidecar -ef $pending_chapter_source ]]; then
            rm -f -- "$pending_chapter_sidecar" || cleanup_failed=true
        fi
    fi
    for scratch in "$inspection_file" "$inspection_error_file" "$inspection_status_file" "$arguments_snapshot"; do
        if [[ -n $scratch ]]; then
            rm -f -- "$scratch" || cleanup_failed=true
        fi
    done
    if [[ -n $temporary_directory ]]; then
        rm -rf -- "$temporary_directory" || cleanup_failed=true
    fi
    if [[ $dvd_batch_cancelled_after_publication == true ]]; then
        if [[ $cleanup_failed == true ]]; then
            echo 'DVD output was published, but cleanup failed; temporary files may remain.' >&2
        fi
        exit 75
    fi
    if [[ $gui_dvd_batch == true ]] && (( status == 75 )); then
        echo 'Copy failed with a reserved GUI batch status.' >&2
        exit 1
    fi
    if (( status == 0 )) && [[ $cleanup_failed == true ]]; then
        exit 1
    fi
}

run_inspection() {
    local seconds=${DVD_BACKUP_INSPECTION_TIMEOUT-60} status=0 output_size error_size
    local block_bytes=1024 limit_bytes=$MAX_INSPECTION_BYTES limit_label='16 MiB' REPLY
    local first second third soft inherited_bytes=
    inspection_timed_out=false
    if [[ ! $seconds =~ ^[1-9][0-9]{0,3}$ ]]; then
        echo 'DVD_BACKUP_INSPECTION_TIMEOUT must be 1 to 9999 seconds.' >&2
        exit 2
    fi
    if ! command -v timeout >/dev/null; then
        echo 'Media inspection requires GNU timeout.' >&2
        return 127
    fi
    require_commands sleep
    # Bash uses 512-byte blocks in POSIX mode, 1024-byte blocks otherwise.
    if shopt -oq posix; then
        block_bytes=512
    fi
    # Bash reports limits in whole blocks; procfs exposes the exact byte count.
    while IFS=$' \t' read -r first second third soft _; do
        if [[ $first == Max && $second == file && $third == size ]]; then
            inherited_bytes=$soft
            break
        fi
    done </proc/self/limits
    if [[ $inherited_bytes != unlimited && ! $inherited_bytes =~ ^[0-9]+$ ]]; then
        echo 'Could not determine the inherited media inspection file-size limit.' >&2
        return 1
    fi
    if [[ $inherited_bytes != unlimited ]] \
        && (( ${#inherited_bytes} <= ${#MAX_INSPECTION_BYTES} && inherited_bytes < MAX_INSPECTION_BYTES )); then
        limit_bytes=$inherited_bytes
        limit_label="$limit_bytes bytes"
    fi
    if [[ -z $inspection_file ]]; then
        if ! inspection_file=$(mktemp) \
            || ! inspection_error_file=$(mktemp) \
            || ! inspection_status_file=$(mktemp); then
            echo 'Could not create media inspection scratch files.' >&2
            exit 1
        fi
    fi
    if ! { : >| "$inspection_file" && : >| "$inspection_error_file" && : >| "$inspection_status_file"; }; then
        echo 'Could not reset media inspection scratch files.' >&2
        exit 1
    fi
    # Waiting on an async child lets the EXIT trap forward a shell-only signal.
    (
        if (( limit_bytes == MAX_INSPECTION_BYTES )); then
            ulimit -f "$((limit_bytes / block_bytes))" || exit 1
        fi
        # shellcheck disable=SC2016
        exec timeout --kill-after=2s "${seconds}s" "$BASH" -c '
            set +m
            marker=$1
            shift
            # Keep timeout armed for the entire group, even if the reader exits.
            trap '\''trap "" TERM; sleep infinity; exit 143'\'' TERM
            "$@" &
            status=0
            wait "$!" || status=$?
            printf "%s\n" "$status" >| "$marker" || exit 1
            exit "$status"
        ' bash "$inspection_status_file" "$@"
    ) >|"$inspection_file" 2>|"$inspection_error_file" &
    inspection_pid=$!
    wait "$inspection_pid" || status=$?
    finish_inspection
    if [[ ! -s $inspection_status_file ]] && (( status == 124 || status == 137 )); then
        inspection_timed_out=true
        printf 'Media inspection did not complete: %s (timeout %s seconds).\n' "${1##*/}" "$seconds" >&2
        if error_size=$(stat -c '%s' -- "$inspection_error_file") && (( error_size < limit_bytes )); then
            cat -- "$inspection_error_file" >&2 || true
        fi
        return 124
    fi
    if ! output_size=$(stat -c '%s' -- "$inspection_file"); then
        echo 'Could not determine media inspection output size.' >&2
        return 1
    fi
    if ! error_size=$(stat -c '%s' -- "$inspection_error_file"); then
        echo 'Could not determine media inspection diagnostic size.' >&2
        return 1
    fi
    if (( error_size >= limit_bytes )); then
        printf 'Media inspection diagnostics reached the %s limit; refusing excessive tool output.\n' "$limit_label" >&2
        return 1
    fi
    cat -- "$inspection_error_file" >&2
    if (( output_size >= limit_bytes )); then
        printf 'Media inspection output reached the %s limit; refusing incomplete or oversized metadata.\n' "$limit_label" >&2
        return 1
    fi
    (( status == 0 )) || return "$status"
    if LC_ALL=C IFS= read -r -d '' <"$inspection_file"; then
        echo 'Media inspection output contains a NUL byte; refusing malformed metadata.' >&2
        return 1
    fi
    cat -- "$inspection_file"
}

capture_inspection() {
    run_inspection "$@" >/dev/null || return
    inspection_output=$(<"$inspection_file")
}

stop_active_process_group() {
    local process_group=$active_process_group group_only=${1:-false}
    [[ -n $process_group ]] || return 0
    # Before setsid creates the group, the launcher is reachable only by PID.
    if ! kill -TERM -- "-$process_group" 2>/dev/null && [[ $group_only == false ]]; then
        kill -TERM "$process_group" 2>/dev/null || true
        kill -TERM -- "-$process_group" 2>/dev/null || true
    fi
    for _ in {1..30}; do
        if ! kill -0 -- "-$process_group" 2>/dev/null \
            && { [[ $group_only == true ]] || ! kill -0 "$process_group" 2>/dev/null; }; then
            break
        fi
        if ! sleep 0.1; then
            echo 'Could not wait for copy termination; escalating to SIGKILL.' >&2
            break
        fi
    done
    if ! kill -KILL -- "-$process_group" 2>/dev/null && [[ $group_only == false ]]; then
        kill -KILL "$process_group" 2>/dev/null || true
        kill -KILL -- "-$process_group" 2>/dev/null || true
    fi
    wait "$process_group" 2>/dev/null || true
    active_process_group=
}

run_copy_command() {
    require_commands setsid sleep
    local status=0
    copy_launching=true
    setsid -- "$@" &
    active_process_group=$!
    copy_launching=false
    if (( copy_cancel_status != 0 )); then
        handle_signal "$copy_cancel_status"
    fi
    wait "$active_process_group" || status=$?
    if kill -0 -- "-$active_process_group" 2>/dev/null; then
        printf 'Copy command exited with helpers still running: %s. Stopping them and rejecting the attempt.\n' "${1##*/}" >&2
        stop_active_process_group true
        if (( status == 0 )); then
            status=1
        fi
    else
        active_process_group=
    fi
    return "$status"
}

handle_signal() {
    local status=$1
    if [[ $copy_launching == true ]]; then
        copy_cancel_status=$status
        return
    fi
    trap - INT TERM
    if [[ -n $active_process_group ]]; then
        stop_active_process_group
    fi
    if [[ $gui_dvd_batch == true && $dvd_output_committed == true ]]; then
        dvd_batch_cancelled_after_publication=true
        exit 75
    fi
    exit "$status"
}

validate_disc_details() {
    local media=$1 playlist=${2:-}
    if ! jq -se --arg media "$media" --arg playlist "$playlist" '
        def valid_pid:
            type == "string" and test("\\A0x0{0,4}[0-9a-fA-F]{1,4}\\z") and
            (ascii_downcase | sub("^0x0*"; "") | test("\\A(?:[0-9a-f]{0,3}|1[0-9a-f]{3})\\z"));
        def valid_chapters($start):
            type == "array" and all(.[];
                type == "object" and (.[$start] | type == "string" or type == "number" or type == "boolean"));
        def valid_tracks:
            type == "array" and all(.[]; type == "object" and (.stream | valid_pid));
        length == 1 and (.[0] |
            if type != "object" then false
            elif $media == "DVD" then
                (.format | type == "object" and length > 0) and
                (.streams | type == "array" and
                    all(.[]; type == "object" and
                        (.index | type == "number" and . >= 0 and . <= 9999 and . == floor)) and
                    (length == (map(.index) | unique | length))) and
                (if has("chapters") then .chapters | valid_chapters("start_time") else true end)
            else
                (.bluray | type == "object") and
                (.titles | type == "array" and all(.[];
                    type == "object" and (.playlist | tostring | test("\\A[0-9]{1,5}\\z")) and
                    (if has("video") then .video | valid_tracks else true end) and
                    (if has("audio") then .audio | valid_tracks else true end) and
                    (if has("subtitles") then .subtitles | valid_tracks else true end) and
                    (if has("chapters") then .chapters | valid_chapters("start time") else true end)) and
                    (length == (map(.playlist | tonumber) | unique | length))) and
                ($playlist == "" or
                    (.titles | length == 1 and (.[0].playlist | tonumber) == ($playlist | tonumber)))
            end)
    ' >/dev/null 2>&1 <<<"$inspection_output" ||
        { [[ $media == DVD ]] && ! "$python_command" -P -c '
import json
import sys

# jq can normalize decimal and exponent forms, but the GUI requires JSON integers.
streams = json.load(sys.stdin)["streams"]
sys.exit(not all(type(stream["index"]) is int for stream in streams))
' <<<"$inspection_output"; }; then
        printf 'Invalid or incomplete %s disc details returned by the metadata reader.\n' "$media" >&2
        return 1
    fi
}

pin_output_directory() {
    local output_directory=$1
    local owner mode identity

    # Pin the directory before checking it: ancestor renames/symlink swaps must
    # not redirect subsequent writes or rollback to another directory.
    if ! exec {workspace_parent_fd}< "$output_directory"; then
        echo "Cannot open output directory: $output_directory" >&2
        exit 1
    fi
    workspace_parent=/proc/$$/fd/$workspace_parent_fd
    if [[ -n $gui_reviewed_directory ]]; then
        if ! identity=$(stat -Lc '%d:%i' -- "$workspace_parent") || [[ $identity != "$gui_reviewed_directory" ]]; then
            echo 'The output directory changed after the copy summary; review it again.' >&2
            exit 1
        fi
    fi
    if [[ ! -d $workspace_parent ]] \
        || ! read -r owner mode < <(stat -Lc '%u %a' -- "$workspace_parent") \
        || [[ $owner != "$EUID" && $owner != 0 ]] \
        || (( (8#$mode & 0022) != 0 && (8#$mode & 01000) == 0 )); then
        echo "Unsafe output directory: must be owned by you or root and not group/other-writable unless sticky: $output_directory" >&2
        exit 1
    fi
}

make_temporary_workspace() {
    local output_directory=$1
    local hardlink_probe
    require_commands ln sync

    local sync_help
    if ! command -v sync >/dev/null \
        || ! sync_help=$(sync --help 2>&1) \
        || [[ $sync_help != *"--file-system"* ]]; then
        echo 'Safe output publication requires GNU sync with filesystem-sync support.' >&2
        exit 1
    fi

    # In a trusted parent, other users cannot replace our private workspace.
    temporary_directory=$(mktemp -d "$workspace_parent/.dvd-to-mkv.XXXXXX")
    temporary_output=$temporary_directory/output.mkv
    temporary_copy=$temporary_directory/output.m2ts
    hardlink_probe=$temporary_directory/.hardlink-probe
    if ! : > "$hardlink_probe"; then
        echo "Cannot create publication probe in output directory: $output_directory" >&2
        exit 1
    fi
    if ! ln -T -- "$hardlink_probe" "$hardlink_probe.link"; then
        echo "Output filesystem must support hard links for safe publication: $output_directory" >&2
        exit 1
    fi
    rm -f -- "$hardlink_probe" "$hardlink_probe.link"
}

normalize_directory_path() {
    local path=$1
    while [[ $path != / ]]; do
        case $path in
            */.) path=${path%/.}; path=${path:-/} ;;
            */) path=${path%/} ;;
            *) break ;;
        esac
    done
    printf '%s\0' "$path"
}

find_keydb() {
    local candidate

    [[ -n ${HOME:-} ]] || return 1
    for candidate in "$HOME"/.config/aacs/KEYDB.cfg "$HOME"/.config/aacs/keydb.cfg; do
        if [[ -f $candidate ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

check_mvc_tools() {
    if ! command -v "$python_command" >/dev/null || ! command -v bluray_copy >/dev/null; then
        echo 'MVC payload inspection requires python3 and bluray_copy.' >&2
        return 1
    fi
}

probe_mvc() {
    check_mvc_tools || return 1
    # The timed Bash child expands these positional arguments.
    # shellcheck disable=SC2016
    PYTHONPATH="$python_module_path" capture_inspection "$BASH" -c '
        set +m
        playlist=$1
        python_command=$2
        shift 2
        # Inspect both statuses even if inherited errexit and pipefail are on.
        set +e
        bluray_copy "$@" --playlist "$playlist" --output - |
            "$python_command" -P -m dvdvideo.mvc_probe
        statuses=("${PIPESTATUS[@]}")
        if (( statuses[1] != 0 || (statuses[0] != 0 && statuses[0] != 141) )); then
            echo "MVC inspection failed for playlist $playlist (reader ${statuses[0]}, parser ${statuses[1]})." >&2
            exit 1
        fi
    ' bash "$1" "$python_command" "$device" "${keydb_args[@]}"
}

playlist_output_path() {
    local playlist=$1
    local extension=${playlist_formats[$playlist]:-mkv}

    if [[ -n $output_file ]]; then
        printf '%s\0' "$output_file"
    elif [[ -v "playlist_names[$playlist]" ]]; then
        printf '%s/%s\0' "$output_directory" "${playlist_names[$playlist]}"
    elif [[ -n $output_name && ${#selected_playlists[@]} -eq 1 ]]; then
        printf '%s/%s.%s\0' "$output_directory" "$output_name" "$extension"
    elif [[ -n $output_name ]]; then
        printf '%s/%s-playlist-%s.%s\0' "$output_directory" "$output_name" "$playlist" "$extension"
    else
        printf '%s/playlist-%s.%s\0' "$output_directory" "$playlist" "$extension"
    fi
}

check_m2ts_sidecar() {
    local pinned_output=$1 display_output=$2
    if [[ -e $pinned_output.chapters.txt || -L $pinned_output.chapters.txt ]]; then
        echo "Refusing to overwrite existing file: $display_output.chapters.txt" >&2
        echo "The existing sidecar could be mistaken for this copy's chapters. Move it aside or choose a different output filename." >&2
        exit 1
    fi
}

publish_output() {
    local output_file=$workspace_parent/${1##*/}
    local chapter_file=${2:-}
    local format=${3:-mkv}

    if [[ ! -f $temporary_output || -L $temporary_output ]] \
        || { [[ -n $chapter_file ]] && [[ ! -f $chapter_file || -L $chapter_file ]]; }; then
        echo "Refusing to publish a non-regular file or symbolic link: $1" >&2
        exit 1
    fi
    pending_output=$output_file
    pending_chapter_source=$chapter_file
    pending_chapter_sidecar=
    if [[ -n $chapter_file ]]; then
        pending_chapter_sidecar=$output_file.chapters.txt
    fi
    publication_pending=true
    if [[ -n $chapter_file ]]; then
        if ! ln -T -- "$chapter_file" "$output_file.chapters.txt"; then
            echo "Failed to publish chapters (existing files are not overwritten): $1.chapters.txt" >&2
            exit 1
        fi
    elif [[ $format == m2ts ]]; then
        check_m2ts_sidecar "$output_file" "$1"
    fi

    if ! ln -T -- "$temporary_output" "$output_file"; then
        if [[ -e $output_file || -L $output_file ]]; then
            echo "Refusing to overwrite existing file: $1" >&2
        else
            echo "Failed to publish output: $1" >&2
        fi
        exit 1
    fi
    if ! sync -f -- "$workspace_parent"; then
        echo "Failed to flush published output to storage: $1" >&2
        exit 1
    fi
    if [[ $media_type == bluray ]]; then
        # Commit the rollback state and batch count in the same shell command.
        publication_pending=false completed_playlists=$((completed_playlists + 1))
        if [[ -n $chapter_file ]]; then
            printf 'Chapters: %s.chapters.txt (playlist-relative markers; player support varies)\n' "$1" >&2
        fi
        printf 'Copied: %s\n' "$1"
    else
        publication_pending=false dvd_output_committed=true
    fi
    rm -rf "$temporary_directory"
    temporary_directory=
    temporary_output=
    temporary_copy=
}

validate_output_filename() {
    local path=$1 quiet=${2:-false} directory filename limit
    local LC_ALL=C
    require_commands getconf

    filename=${path##*/}
    directory=$workspace_parent
    if ! limit=$(getconf NAME_MAX "$directory"); then
        echo "Cannot determine output filename limit: $directory" >&2
        exit 1
    fi
    if [[ $limit =~ ^[0-9]+$ ]]; then
        if (( limit > 0 && ${#filename} > limit )); then
            if [[ $quiet == true ]]; then
                return 2
            fi
            echo "Output filename exceeds the filesystem's $limit-byte limit: $path" >&2
            exit 2
        fi
    elif [[ $limit != undefined && $limit != -1 ]]; then
        echo "Invalid output filename limit for $directory: $limit" >&2
        exit 1
    fi
}

trap cleanup EXIT
trap 'handle_signal 130' INT
trap 'handle_signal 143' TERM
require_commands cat mktemp rm stat timeout

if [[ ${1:-} == --arguments-file ]]; then
    if [[ $# -ne 2 || ! -f $2 || ! -r $2 ]]; then
        echo '--arguments-file requires one readable regular file.' >&2
        exit 2
    fi
    require_commands head tail
    arguments_snapshot=$(mktemp) || exit 1
    arguments_path=$2
    if [[ $arguments_path == - ]]; then
        arguments_path=./-
    fi
    if ! head -c "$((MAX_ARGUMENT_FILE_BYTES + 1))" -- "$arguments_path" >|"$arguments_snapshot"; then
        echo 'Could not read arguments file.' >&2
        exit 2
    fi
    if ! arguments_size=$(stat -c '%s' -- "$arguments_snapshot"); then
        echo 'Could not determine arguments-file size.' >&2
        exit 2
    fi
    if ! [[ $arguments_size =~ ^[0-9]+$ ]] || (( arguments_size > MAX_ARGUMENT_FILE_BYTES )); then
        echo 'Arguments file exceeds the 16 MiB size limit.' >&2
        exit 2
    fi
    mapfile -d '' -t -n "$((MAX_ARGUMENT_FILE_ARGUMENTS + 2))" arguments < "$arguments_snapshot"
    if (( ${#arguments[@]} > MAX_ARGUMENT_FILE_ARGUMENTS + 1 )); then
        echo "Arguments file exceeds the $MAX_ARGUMENT_FILE_ARGUMENTS argument limit." >&2
        exit 2
    fi
    if [[ ${#arguments[@]} -eq 0 || ${arguments[-1]} != --end-arguments-file ]] \
        || ! IFS= read -r -d '' < <(tail -c 1 -- "$arguments_snapshot"); then
        echo 'Invalid or incomplete arguments file.' >&2
        exit 2
    fi
    rm -f -- "$arguments_snapshot"
    arguments_snapshot=
    unset 'arguments[-1]'
    set -- "${arguments[@]}"
    unset arguments
fi

while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--help)
            usage
            exit 0
            ;;
        --bluray)
            media_type=bluray
            shift
            ;;
        --gui-dvd-batch)
            gui_dvd_batch=true
            shift
            ;;
        --gui-output-mode)
            if [[ $# -lt 2 || ! $2 =~ ^(file|directory)$ || -n $gui_output_mode ]]; then
                echo '--gui-output-mode requires file or directory once.' >&2
                exit 2
            fi
            gui_output_mode=$2
            shift 2
            ;;
        --gui-reviewed-directory)
            if [[ $# -lt 2 || ! $2 =~ ^[0-9]+:[0-9]+$ || -n $gui_reviewed_directory ]]; then
                echo '--gui-reviewed-directory requires one device:inode identity.' >&2
                exit 2
            fi
            gui_reviewed_directory=$2
            shift 2
            ;;
        --gui-reviewed-format)
            if [[ $# -lt 2 || ! $2 =~ ^([0-9]{1,5}):(mkv|m2ts)$ ]]; then
                echo '--gui-reviewed-format requires a playlist ID and mkv or m2ts.' >&2
                exit 2
            fi
            id=$((10#${BASH_REMATCH[1]}))
            if [[ -v "gui_reviewed_formats[$id]" ]]; then
                echo "Duplicate reviewed format for playlist $id." >&2
                exit 2
            fi
            gui_reviewed_formats[$id]=${BASH_REMATCH[2]}
            shift 2
            ;;
        --list-streams)
            list_streams=true
            shift
            ;;
        --list-streams-tsv)
            list_streams=true
            list_streams_tsv=true
            shift
            ;;
        --list-playlists)
            list_playlists=true
            shift
            ;;
        --list-titles)
            list_titles=true
            shift
            ;;
        --disc-details)
            disc_details=true
            shift
            ;;
        --output-format)
            if [[ $# -lt 2 || ! $2 =~ ^(auto|mkv|m2ts)$ ]]; then
                echo '--output-format requires auto, mkv, or m2ts.' >&2
                exit 2
            fi
            output_format=$2
            output_format_set=true
            shift 2
            ;;
        --stream)
            if [[ $# -lt 2 ]]; then
                echo '--stream requires one or more comma-separated stream indexes from 0 to 9999.' >&2
                exit 2
            fi
            stream_list=$2
            while true; do
                if [[ $stream_list == *,* ]]; then
                    stream_token=${stream_list%%,*}
                    stream_list=${stream_list#*,}
                    more_streams=true
                else
                    stream_token=$stream_list
                    more_streams=false
                fi
                if ! [[ $stream_token =~ ^[0-9]{1,4}$ ]]; then
                    echo '--stream requires comma-separated stream indexes from 0 to 9999.' >&2
                    exit 2
                fi
                # Force base 10: a leading zero would otherwise be octal, and 1 and 01 must match.
                stream_index=$((10#$stream_token))
                if [[ -v "seen_streams[$stream_index]" ]]; then
                    echo "Duplicate stream index: $stream_index" >&2
                    exit 2
                fi
                seen_streams[$stream_index]=1
                selected_streams+=("$stream_index")
                [[ $more_streams == true ]] || break
            done
            shift 2
            ;;
        --stream-id)
            # MPEG-TS PIDs are 13 bits: 0x0000-0x1fff.
            if [[ $# -lt 2 || ! $2 =~ ^[0-9]{1,5}:0x0{0,4}[0-9a-fA-F]{1,4}$ ]] \
                || (( 16#${2#*:0x} > 0x1fff )); then
                echo '--stream-id requires PLAYLIST:PID with a PID from 0x0000 to 0x1fff, for example 0:0x1100.' >&2
                exit 2
            fi
            selection=$((10#${2%%:*})):$(printf '0x%04x' "$((16#${2#*:0x}))")
            if [[ -v "seen_stream_ids[$selection]" ]]; then
                echo "Duplicate stream selection: $selection" >&2
                exit 2
            fi
            seen_stream_ids[$selection]=1
            selected_stream_ids+=("$selection")
            shift 2
            ;;
        --device|--keydb|--minlength|--playlist|--retries|--output-name|--playlist-output-name)
            if [[ $# -lt 2 ]]; then
                echo "$1 requires a value." >&2
                exit 2
            fi
            case $1 in
                --device)
                    if [[ -z $2 ]]; then
                        echo '--device requires a non-empty path.' >&2
                        exit 2
                    fi
                    device=$2
                    ;;
                --keydb)
                    if [[ -z $2 ]]; then
                        echo '--keydb requires a non-empty path.' >&2
                        exit 2
                    fi
                    keydb=$2
                    ;;
                --output-name)
                    if [[ -z $2 || $2 == . || $2 == .. || $2 == */* || $2 == *\\* || $2 =~ [[:cntrl:]] ]]; then
                        echo '--output-name requires a non-empty filename stem without path separators or control characters.' >&2
                        exit 2
                    fi
                    if [[ ${2,,} == *.mkv || ${2,,} == *.m2ts ]]; then
                        echo '--output-name requires a filename stem without an .mkv or .m2ts extension.' >&2
                        exit 2
                    fi
                    output_name=$2
                    ;;
                --playlist-output-name)
                    if [[ ! $2 =~ ^([0-9]{1,5}):(.*)$ ]]; then
                        echo '--playlist-output-name requires PLAYLIST:NAME.' >&2
                        exit 2
                    fi
                    id=$((10#${BASH_REMATCH[1]}))
                    stem=${BASH_REMATCH[2]}
                    if [[ -z $stem || $stem == . || $stem == .. || $stem == */* || $stem == *\\* || $stem =~ [[:cntrl:]] || -v "playlist_names[$id]" ]]; then
                        echo 'Invalid or duplicate playlist output name.' >&2
                        exit 2
                    fi
                    playlist_names[$id]=$stem
                    ;;
                --minlength)
                    # bluray_info reads minutes as an unsigned 32-bit value.
                    if [[ -z $2 || ! $2 =~ ^0*([1-9][0-9]{0,9})?$ ]] \
                        || (( 10#${BASH_REMATCH[1]:-0} > 4294967295 )); then
                        echo '--minlength requires a number of minutes from 0 to 4294967295.' >&2
                        exit 2
                    fi
                    minlength=${BASH_REMATCH[1]:-0}
                    minlength_set=true
                    ;;
                --playlist)
                    if ! [[ $2 =~ ^[0-9]{1,5}$ ]]; then
                        echo '--playlist requires a playlist ID from 0 to 99999.' >&2
                        exit 2
                    fi
                    id=$((10#$2))
                    if [[ -v "seen_playlists[$id]" ]]; then
                        echo "Duplicate playlist ID: $id" >&2
                        exit 2
                    fi
                    seen_playlists[$id]=1
                    selected_playlists+=("$id")
                    ;;
                --retries)
                    if ! [[ $2 =~ ^[0-9]{1,4}$ ]]; then
                        echo '--retries must be a number of attempts from 0 to 9999.' >&2
                        exit 2
                    fi
                    # Base 10: "08" would otherwise abort the retry check and publish a failed copy.
                    retries=$((10#$2))
                    retries_set=true
                    ;;
            esac
            shift 2
            ;;
        --)
            shift
            break
            ;;
        -*)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
        *)
            break
            ;;
    esac
done

if [[ $gui_dvd_batch == true && ($media_type != dvd || $list_titles == true || $list_streams == true || $disc_details == true || $list_playlists == true) ]]; then
    echo '--gui-dvd-batch is only supported for DVD copying.' >&2
    exit 2
fi
if (( ${#gui_reviewed_formats[@]} )); then
    if [[ $media_type != bluray || -z $gui_output_mode || $list_titles == true || $list_streams == true || $disc_details == true || $list_playlists == true ]] \
        || (( ${#gui_reviewed_formats[@]} != ${#selected_playlists[@]} )); then
        echo 'Reviewed formats require exactly one entry per selected Blu-ray playlist.' >&2
        exit 2
    fi
    for id in "${selected_playlists[@]}"; do
        if [[ ! -v "gui_reviewed_formats[$id]" ]]; then
            echo "Missing reviewed format for playlist $id." >&2
            exit 2
        fi
    done
fi

if [[ $list_titles == true && ($media_type == bluray || $list_streams == true || $list_playlists == true || $disc_details == true || ${#selected_streams[@]} -ne 0 || $retries_set == true || $# -ne 1) ]]; then
    echo '--list-titles requires one DVD input and cannot be combined with other inspection or copy options.' >&2
    exit 2
fi

if [[ $disc_details == true && ($list_streams == true || $list_playlists == true || ${#selected_streams[@]} -ne 0 || ${#selected_stream_ids[@]} -ne 0 || -n $output_name || ${#playlist_names[@]} -ne 0 || $output_format_set == true || $minlength_set == true || $retries_set == true) ]]; then
    echo '--disc-details cannot be combined with listing, copying, or output options.' >&2
    exit 2
fi

if [[ $list_streams == true && (${#selected_streams[@]} -ne 0 || ${#selected_stream_ids[@]} -ne 0 || $list_playlists == true || -n $output_name || $output_format_set == true || $retries_set == true) ]]; then
    echo '--list-streams cannot be combined with stream selections, copy options, or --list-playlists.' >&2
    exit 2
fi
if [[ $list_playlists == true && (${#selected_playlists[@]} -ne 0 || ${#selected_streams[@]} -ne 0 || ${#selected_stream_ids[@]} -ne 0 || -n $output_name || $output_format_set == true || $retries_set == true) ]]; then
    echo '--list-playlists cannot be combined with playlist/stream selections or copy options.' >&2
    exit 2
fi
if [[ ${#playlist_names[@]} -ne 0 && ($list_playlists == true || $list_streams == true) ]]; then
    echo '--playlist-output-name cannot be combined with inspection options.' >&2
    exit 2
fi

if [[ $media_type == bluray && $list_playlists == false && $minlength_set == true ]]; then
    echo '--minlength can only be used with --list-playlists.' >&2
    exit 2
fi

if [[ $media_type != bluray && ($list_playlists == true || ${#selected_playlists[@]} -ne 0 || ${#selected_stream_ids[@]} -ne 0 || -n $device || -n $keydb || -n $output_name || ${#playlist_names[@]} -ne 0 || $minlength_set == true || $output_format_set == true) ]]; then
    echo 'Blu-ray options require --bluray.' >&2
    exit 2
fi

if [[ $media_type == bluray && $list_streams_tsv == true ]]; then
    echo '--list-streams-tsv is only supported for DVD input.' >&2
    exit 2
fi

if [[ $media_type == bluray ]]; then
    if [[ ${#selected_streams[@]} -ne 0 ]]; then
        echo '--stream is only supported for DVD input.' >&2
        exit 2
    fi
    if [[ -z $device ]]; then
        device=/dev/sr0
    fi
    if [[ $device == -* ]]; then
        echo "Blu-ray input must not begin with a dash: $device" >&2
        exit 2
    fi
    if ! [[ -b $device || -f $device || -d $device ]]; then
        echo "Blu-ray input must be an existing device, image file, or BDMV directory: $device" >&2
        exit 2
    fi
    if [[ -d $device ]]; then
        IFS= read -r -d '' device < <(normalize_directory_path "$device")
        device_folder=${device##*/}
        if [[ ${device_folder^^} == BDMV ]]; then
            IFS= read -r -d '' device < <(dirname -z -- "$device")
        fi
    fi
    if [[ -z $keydb ]]; then
        keydb=$(find_keydb || true)
    fi
    if [[ $keydb == -* ]]; then
        keydb=./$keydb
    fi
    if [[ -n $keydb && ! -f $keydb ]]; then
        echo "KEYDB.cfg file does not exist: $keydb" >&2
        exit 2
    fi
    if [[ -n $keydb ]]; then
        if ! keydb_metadata=$(stat -Lc '%u %a' -- "$keydb"); then
            echo "Could not inspect KEYDB.cfg ownership and permissions: $keydb" >&2
            exit 1
        fi
        read -r keydb_owner keydb_permissions <<<"$keydb_metadata"
        if [[ $keydb_owner != "$EUID" && $keydb_owner != 0 ]]; then
            echo "KEYDB.cfg must be owned by you or root: $keydb" >&2
            exit 1
        fi
        if (( (8#$keydb_permissions & 0077) != 0 )); then
            echo "KEYDB.cfg must not be readable by group or others: $keydb" >&2
            exit 1
        fi
    fi
    keydb_args=()
    if [[ -n $keydb ]]; then
        keydb_args=(-k "$keydb")
    fi
    if [[ $disc_details == true ]]; then
        if [[ $# -ne 0 || ${#selected_playlists[@]} -gt 1 ]]; then
            echo 'Blu-ray details accept at most one --playlist and no output path.' >&2
            exit 2
        fi
        if ! command -v bluray_info >/dev/null || ! command -v jq >/dev/null; then
            echo 'Blu-ray details require bluray_info and jq.' >&2
            exit 1
        fi
        if [[ ${#selected_playlists[@]} -eq 1 ]]; then
            capture_inspection bluray_info "$device" "${keydb_args[@]}" --playlist "${selected_playlists[0]}" --all --json
        else
            capture_inspection bluray_info "$device" "${keydb_args[@]}" --minutes 0 --json
        fi
        validate_disc_details 'Blu-ray' "${selected_playlists[0]:-}"
        printf '%s\n' "$inspection_output"
        exit 0
    fi
    if [[ $list_streams == true ]]; then
        if [[ ${#selected_playlists[@]} -ne 1 || $# -ne 0 ]]; then
            echo 'Listing Blu-ray streams requires exactly one --playlist ID and no output path.' >&2
            exit 2
        fi
        if ! command -v bluray_info >/dev/null || ! command -v jq >/dev/null; then
            echo 'Listing Blu-ray streams requires bluray_info and jq.' >&2
            exit 1
        fi
        capture_inspection bluray_info "$device" "${keydb_args[@]}" \
            --playlist "${selected_playlists[0]}" --all --json
        stream_metadata=$inspection_output
        if ! jq -se --arg playlist "${selected_playlists[0]}" '
            length == 1 and (.[0].titles | type == "array" and length == 1) and
            (.[0].titles[0].playlist | tostring | test("^[0-9]{1,5}$")) and
            (.[0].titles[0].playlist | tonumber) == ($playlist | tonumber)
        ' >/dev/null 2>&1 <<<"$stream_metadata"; then
            echo "Blu-ray metadata did not describe requested playlist ${selected_playlists[0]}." >&2
            exit 1
        fi
        probe_metadata='{"streams":[]}'
        if ! is_3d=$(jq -sr '
            if length == 1 and (.[0].bluray | type == "object") and
                (.[0].bluray["3D content"] | type == "boolean") then
                .[0].bluray["3D content"]
            else
                error("expected one metadata object with a boolean 3D content field")
            end
        ' 2>/dev/null <<<"$stream_metadata"); then
            echo 'Blu-ray metadata is missing a valid 3D content value; cannot list streams safely.' >&2
            exit 1
        fi
        if [[ $is_3d == true ]]; then
            echo "$warning_3d" >&2
            if ! command -v ffprobe >/dev/null; then
                echo 'Inspecting 3D video streams requires ffprobe with Blu-ray protocol support.' >&2
                exit 1
            fi
            check_mvc_tools || exit 1
            capture_inspection ffprobe -v error -playlist "${selected_playlists[0]}" \
                -analyzeduration 10000000 -probesize 10000000 -select_streams v \
                -show_entries stream=id,codec_name,width,height -of json "bluray:$device"
            probe_metadata=$inspection_output
            jq -se 'length == 1 and (.[0].streams | type == "array" and length > 0)' \
                <<<"$probe_metadata" >/dev/null || {
                echo 'FFprobe did not return video streams for the 3D playlist.' >&2
                exit 1
            }
            probe_mvc "${selected_playlists[0]}"
            mvc_streams=$inspection_output
        else
            mvc_streams=
        fi
        stream_listing=$(jq -r --slurpfile probes <(printf '%s\n' "$probe_metadata") \
            --argjson three_d "$is_3d" '
            def pid_key: ascii_downcase | sub("^0x0*"; "");
            def valid_pid:
                if type == "string" then
                    test("\\A0x0{0,4}[0-9a-fA-F]{1,4}\\z") and
                    (pid_key | test("\\A(?:[0-9a-f]{0,3}|1[0-9a-f]{3})\\z"))
                else false end;
            def valid_dimension:
                if type == "number" then isfinite and . > 0 and . == floor else false end;
            def known_dimensions:
                (.width | valid_dimension) and (.height | valid_dimension);
            $probes[0] as $probe |
            if (.titles | type) != "array" or (.titles | length) != 1 then
                error("bluray_info must return exactly one title")
            elif any($probe.streams[]; .id | valid_pid | not) then
                error("FFprobe video stream does not contain a valid PID")
            elif ($probe.streams | group_by(.id | pid_key) | any(.[]; length > 1)) then
                error("FFprobe video stream metadata contains duplicate PIDs")
            else
                INDEX($probe.streams[]; .id | pid_key) as $probe_by_pid |
                .titles[0] |
                . as $title |
                (["video", "audio", "subtitles"] |
                    map(select($title[.] != null and ($title[.] | type) != "array")) |
                    first) as $invalid_collection |
                if $invalid_collection != null then
                    error("bluray_info \($invalid_collection) stream collection must be an array")
                else . end |
                INDEX((.video // [])[] | .stream | strings; pid_key) as $known_video |
                [(["video", "audio", "subtitles"][] as $kind |
                    (.[$kind] // [])[] |
                    if (.stream | valid_pid | not) then
                        error("bluray_info stream does not contain a valid PID")
                    else
                        . as $record |
                        $probe_by_pid[$record.stream | pid_key] as $video |
                        [.stream, $kind, (.codec // .["codec name"] // ""),
                         (.language // ""), (.format // "")] +
                        (if $three_d then
                            [if $kind != "video" then ""
                             elif $video == null then "Video role/dimensions unverified"
                             elif ($video | known_dimensions | not) then
                                 "Dimensions unknown - MKV muxing unsupported"
                             else "\($video.width)x\($video.height) - video listed by disc metadata; not proof of full 3D"
                             end]
                         else [] end)
                    end),
                (if $three_d then
                    $probe.streams[] | .id as $pid |
                    if ($known_video | has($pid | pid_key)) then empty
                    else
                        [.id, "video", (.codec_name // ""), "", "",
                         (if (known_dimensions | not) then
                             "Additional video - possible MVC-dependent view; dimensions unknown; MKV muxing unsupported"
                          else "Additional video - role unverified; \(.width)x\(.height)"
                          end)]
                    end
                 else empty end)] as $rows |
                if ($rows | group_by(.[0] | pid_key) | any(.[]; length > 1)) then
                    error("Blu-ray stream metadata contains duplicate PIDs")
                else $rows[] |
                    map(if type == "string" then gsub("[\u0000-\u001f\u007f-\u009f\u2028\u2029]+"; " ") else . end) |
                    @tsv
                end
            end' <<<"$stream_metadata")
        if [[ -z $stream_listing ]]; then
            echo 'No streams found in the selected Blu-ray playlist.' >&2
            exit 1
        fi
        if [[ $is_3d == true ]]; then
            printf '# warning\t%s\n' "$warning_3d"
            if [[ -n $mvc_streams ]]; then
                while IFS= read -r mvc_pid; do
                    printf '# mvc\t%s\n' "$mvc_pid"
                done <<<"$mvc_streams"
            fi
            printf 'PID\tTYPE\tCODEC\tLANG\tFORMAT\tDETAILS\n'
        else
            printf 'PID\tTYPE\tCODEC\tLANG\tFORMAT\n'
        fi
        printf '%s\n' "$stream_listing"
        exit 0
    fi
    if [[ $list_playlists == true ]]; then
        require_commands tr
        if [[ ${#selected_stream_ids[@]} -ne 0 ]]; then
            echo '--list-playlists cannot be combined with --stream-id.' >&2
            exit 2
        fi
        if [[ $# -ne 0 ]]; then
            usage >&2
            exit 2
        fi
        if ! command -v bluray_info >/dev/null || ! command -v jq >/dev/null; then
            echo 'Listing Blu-ray playlists requires bluray_info and jq.' >&2
            exit 1
        fi
        capture_inspection bluray_info "$device" "${keydb_args[@]}" --minutes "$minlength" --json
        playlist_listing=$(jq -sr --arg warning "$warning_3d" \
            'def valid_playlist_id:
                (type == "number" or type == "string") and (tostring | test("\\A[0-9]{1,5}\\z"));
            def display_text:
                if type == "string" then
                    gsub("[\u000b\u000c\u001c-\u001e\u0085\u2028\u2029]"; " ")
                else . end;
            def valid_chapter_count:
                if type == "number" then isfinite and . >= 0 and . == floor
                elif type == "string" then test("\\A[0-9]+\\z")
                else false end;
            def reported_duration:
                .playlist as $playlist |
                (if .length != null then .length
                 elif .duration != null then .duration
                 else "unknown" end) |
                if type == "string" or type == "number" then display_text
                else error("bluray_info playlist \($playlist) duration must be a string or number")
                end;
            if length != 1 or (.[0] | type) != "object" then
                error("bluray_info must return exactly one metadata object")
            else .[0] end |
            if (.titles | type) != "array" then
                error("bluray_info JSON does not contain a titles array")
            elif any(.titles[]; .playlist | valid_playlist_id | not) then
                error("bluray_info title does not contain a valid playlist ID")
            else
                (.titles | to_entries | group_by(.value.playlist | tonumber) |
                    map(if (map(.value | del(.playlist)) | unique | length) == 1 then min_by(.key)
                        else error("bluray_info metadata contains conflicting records for playlist \(.[0].value.playlist)")
                        end) |
                    sort_by(.key) | map(.value)) as $titles |
                ([.bluray["disc name"], .bluray["udf title"]] |
                    map(select(type == "string") | gsub("^\\s+|\\s+$"; "") | select(length > 0)) |
                    .[0] // "") as $name |
                (if $name != "" then "# disc-name\t" + ($name | display_text | gsub("[\u0000-\u001f\u007f]"; " ")) else empty end),
                (if .bluray["3D content"] == true then "# warning\t" + $warning else empty end),
                (["main playlist", "longest playlist"][] as $kind |
                    .bluray[$kind] |
                    if . == null then empty
                    elif valid_playlist_id then
                        "# " + ($kind | gsub(" "; "-")) + "\t" + tostring
                    else error("bluray_info \($kind) is not a valid playlist ID")
                    end),
                ($titles[] |
                    [
                        (.playlist | tostring),
                        reported_duration,
                        (if (.chapter_count | valid_chapter_count) then
                            .chapter_count
                        elif (.chapters | type) == "array" then
                            (.chapters | length)
                        else
                            "unknown"
                        end)
                    ] | @tsv
                )
            end' <<<"$inspection_output")
        playlist_listing=$(LC_ALL=C tr -d '\000-\010\013\014\016-\037' <<<"$playlist_listing")
        printf 'playlist\treported_duration\tchapters\n'
        if [[ -n $playlist_listing ]]; then
            printf '%s\n' "$playlist_listing"
        fi
        if ! [[ $playlist_listing =~ (^|$'\n')[0-9]{1,5}$'\t' ]]; then
            printf 'No playlists found.\n' >&2
        fi
        exit 0
    fi
    if [[ ${#selected_playlists[@]} -eq 0 ]]; then
        echo 'Select at least one Blu-ray playlist with --playlist ID, or use --list-playlists.' >&2
        exit 2
    fi
    if [[ $# -gt 1 ]]; then
        usage >&2
        exit 2
    fi
    if [[ $# -eq 1 && -z $1 ]]; then
        echo 'Blu-ray output path must not be empty.' >&2
        exit 2
    fi
    output_path=${1-.}
    if [[ $output_path == -* ]]; then
        output_path=./$output_path
    fi
    if [[ $gui_output_mode == directory || ( -z $gui_output_mode && -d $output_path ) ]]; then
        if [[ ! -d $output_path ]]; then
            echo 'The reviewed output directory no longer exists; review the copy summary again.' >&2
            exit 1
        fi
        output_directory=$output_path
        output_file=
    elif [[ ${#selected_playlists[@]} -eq 1 ]]; then
        if [[ $gui_output_mode == file && -d $output_path ]]; then
            echo 'The reviewed output filename is now a directory; review the copy summary again.' >&2
            exit 1
        fi
        if [[ $output_path == */ ]]; then
            echo "Blu-ray output file must not end with '/': $output_path" >&2
            exit 2
        fi
        IFS= read -r -d '' output_directory < <(dirname -z -- "$output_path")
        output_file=$output_path
        if ! [[ -d $output_directory ]]; then
            echo "Output directory does not exist: $output_directory" >&2
            exit 2
        fi
    else
        echo 'Multiple Blu-ray playlists require an existing output directory.' >&2
        exit 2
    fi
    if (( ${#playlist_names[@]} )) && [[ -n $output_file || -n $output_name ]]; then
        echo 'Playlist output names require an output directory and cannot be combined with --output-name.' >&2
        exit 2
    fi
    for playlist in "${!playlist_names[@]}"; do
        if [[ ! -v "seen_playlists[$playlist]" ]]; then
            echo "Output name refers to an unselected playlist: $playlist" >&2
            exit 2
        fi
    done
    for selection in "${selected_stream_ids[@]}"; do
        if [[ ! -v "seen_playlists[${selection%%:*}]" ]]; then
            echo "Stream selection refers to an unselected playlist: $selection" >&2
            exit 2
        fi
    done
    if [[ -n $output_file && -n $output_name ]]; then
        echo 'Warning: --output-name is ignored when an explicit output file is given.' >&2
    fi
    if ! command -v bluray_copy >/dev/null; then
        echo 'Copying Blu-ray playlists requires bluray_copy.' >&2
        exit 1
    fi
    if ! command -v ffprobe >/dev/null; then
        echo 'Validating Blu-ray copies requires ffprobe.' >&2
        exit 1
    fi
    if ! command -v bluray_info >/dev/null || ! command -v "$python_command" >/dev/null; then
        echo 'Importing Blu-ray chapters requires bluray_info and python3.' >&2
        exit 1
    fi
    if ! command -v setsid >/dev/null; then
        echo 'Safe cancellation of Blu-ray copy commands requires setsid.' >&2
        exit 1
    fi
    echo 'Warning: MKV and M2TS copies may omit a 3D dependent view; 3D preservation is not verified.' >&2
    for playlist in "${selected_playlists[@]}"; do
        format=$output_format
        if [[ $format == auto ]]; then
            if ! command -v jq >/dev/null; then
                echo 'Automatic output format requires jq.' >&2
                exit 1
            fi
            capture_inspection bluray_info "$device" "${keydb_args[@]}" --playlist "$playlist" --all --json
            if ! is_3d=$(jq -sr '
                if length == 1 and (.[0].bluray["3D content"] | type == "boolean") then
                    .[0].bluray["3D content"]
                else
                    error("expected one metadata object with a boolean 3D content field")
                end
            ' 2>/dev/null <<<"$inspection_output"); then
                printf 'Playlist %s: cannot determine automatic output format from Blu-ray metadata; choose --output-format mkv or m2ts explicitly.\n' "$playlist" >&2
                exit 1
            fi
            if ! jq -se --arg playlist "$playlist" '
                length == 1 and (.[0].titles | type == "array" and length == 1) and
                (.[0].titles[0].playlist | tostring | test("^[0-9]{1,5}$")) and
                (.[0].titles[0].playlist | tonumber) == ($playlist | tonumber)
            ' >/dev/null 2>&1 <<<"$inspection_output"; then
                printf 'Playlist %s: Blu-ray metadata did not describe the requested playlist.\n' "$playlist" >&2
                exit 1
            fi
            format=mkv
            if [[ $is_3d == true ]]; then
                probe_mvc "$playlist"
                if [[ -n $inspection_output ]]; then
                    format=m2ts
                    echo "Playlist $playlist: verified MVC payload in sample; using unfiltered M2TS output." >&2
                else
                    echo "Playlist $playlist: MVC not verified in sample; retaining MKV output." >&2
                fi
            fi
        fi
        if [[ -v "gui_reviewed_formats[$playlist]" && ${gui_reviewed_formats[$playlist]} != "$format" ]]; then
            printf 'Playlist %s: automatic output format changed after review; review the copy again.\n' "$playlist" >&2
            exit 1
        fi
        playlist_formats[$playlist]=$format
        if [[ -v "playlist_names[$playlist]" && ${playlist_names[$playlist],,} != *."$format" ]]; then
            echo "Playlist $playlist output format changed; expected a .$format filename." >&2
            exit 2
        fi
        if [[ $format == m2ts ]]; then
            for selection in "${selected_stream_ids[@]}"; do
                if [[ ${selection%%:*} == "$playlist" ]]; then
                    echo "Playlist $playlist: M2TS does not support stream filtering; remove --stream-id selections." >&2
                    exit 2
                fi
            done
        fi
        if [[ -n $output_file && $format == m2ts && ${output_file,,} != *.m2ts ]]; then
            echo "Playlist $playlist requires a .m2ts output filename or an existing output directory." >&2
            exit 2
        fi
        if [[ -n $output_file && $format == mkv && ${output_file,,} == *.m2ts ]]; then
            echo 'MKV output must not use an .m2ts filename; choose --output-format m2ts explicitly.' >&2
            exit 2
        fi
    done
    pin_output_directory "$output_directory"
    declare -A seen_output_paths=()
    for playlist in "${selected_playlists[@]}"; do
        IFS= read -r -d '' playlist_output < <(playlist_output_path "$playlist")
        if [[ -v "seen_output_paths[$playlist_output]" ]]; then
            echo "Duplicate output filename: $playlist_output" >&2
            exit 2
        fi
        seen_output_paths[$playlist_output]=1
        validate_output_filename "$playlist_output"
        pinned_output=$workspace_parent/${playlist_output##*/}
        if [[ -e $pinned_output || -L $pinned_output ]]; then
            echo "Refusing to overwrite existing file: $playlist_output" >&2
            exit 1
        fi
        if [[ ${playlist_formats[$playlist]} == m2ts ]]; then
            check_m2ts_sidecar "$pinned_output" "$playlist_output"
            # Inspect only borderline names here so an invalid sidecar cannot
            # stop the batch after earlier copies have already been published.
            if ! validate_output_filename "$playlist_output.chapters.txt" true; then
                capture_inspection bluray_info "$device" "${keydb_args[@]}" --playlist "$playlist" --chapters --json
                chapter_count=$(
                    PYTHONPATH="$python_module_path" "$python_command" -P -c '
import json
import sys
from dvdvideo.bluray_chapters import export_chapters

print(export_chapters(json.load(sys.stdin), sys.argv[1])[2])
' "$playlist" <<<"$inspection_output")
                if (( chapter_count > 0 )); then
                    validate_output_filename "$playlist_output.chapters.txt"
                fi
            fi
        fi
    done
    if [[ " ${playlist_formats[*]} " == *" mkv "* ]] && ! command -v ffmpeg >/dev/null; then
        echo 'Copying Blu-ray playlists to MKV requires ffmpeg.' >&2
        exit 1
    fi
    for playlist in "${selected_playlists[@]}"; do
        IFS= read -r -d '' playlist_output < <(playlist_output_path "$playlist")
        map_args=()
        language_pid_args=()
        for selection in "${selected_stream_ids[@]}"; do
            if [[ ${selection%%:*} == "$playlist" ]]; then
                map_args+=(-map "0:i:${selection#*:}")
                language_pid_args+=(--pid "${selection#*:}")
            fi
        done
        if [[ ${#map_args[@]} -eq 0 ]]; then
            map_args=(-map 0)
        fi
        make_temporary_workspace "$output_directory"
        chapter_metadata=$temporary_directory/chapters.ffmetadata
        chapter_sidecar=$temporary_directory/chapters.txt
        copy_size_file=$temporary_directory/copy-size
        playlist_metadata=$temporary_directory/playlist.json
        language_args_file=$temporary_directory/language-args
        stream_metadata_file=$temporary_directory/streams.json
        language_validation_args=()
        if [[ ${playlist_formats[$playlist]} == mkv ]]; then
            language_validation_args=(--validate-languages)
        fi
        run_inspection bluray_info "$device" "${keydb_args[@]}" --playlist "$playlist" --chapters --json \
            >"$playlist_metadata" || {
                status=$?
                printf 'Playlist %s: could not read chapter and size metadata.\n' "$playlist" >&2
                exit "$status"
            }
        chapter_count=$(
            PYTHONPATH="$python_module_path" \
                "$python_command" -P -m dvdvideo.bluray_chapters "$playlist" "$chapter_metadata" "$chapter_sidecar" \
                    --size-file "$copy_size_file" "${language_validation_args[@]}" <"$playlist_metadata")
        if [[ ${playlist_formats[$playlist]} == m2ts && $chapter_count -gt 0 ]]; then
            validate_output_filename "$playlist_output.chapters.txt"
        fi
        expected_copy_bytes=$(<"$copy_size_file")
        echo "Playlist $playlist: imported $chapter_count chapter marker(s)." >&2
        attempt=0
        while true; do
            attempt=$((attempt + 1))
            inspection_timed_out=false
            rm -f "$temporary_copy" "$temporary_output"
            PYTHONPATH="$python_module_path" "$python_command" -P -m dvdvideo.output_space \
                "$workspace_parent" --format "${playlist_formats[$playlist]}" --size-file "$copy_size_file"
            copy_valid=false
            echo "Playlist $playlist: extracting." >&2
            if run_copy_command bluray_copy "$device" "${keydb_args[@]}" --playlist "$playlist" -o "$temporary_copy"; then
                echo "Playlist $playlist: extraction complete; validating copy." >&2
                if ! copied_bytes=$(stat -c '%s' -- "$temporary_copy"); then
                    echo "Playlist $playlist: could not determine copied size." >&2
                elif [[ $copied_bytes != "$expected_copy_bytes" ]]; then
                    echo "Playlist $playlist: incomplete copy (expected $expected_copy_bytes bytes, received $copied_bytes)." >&2
                elif [[ ${playlist_formats[$playlist]} == m2ts ]]; then
                    temporary_output=$temporary_copy
                    copy_valid=true
                elif run_inspection ffprobe -v error -show_entries stream=id,codec_type -of json "$temporary_copy" \
                    >| "$stream_metadata_file"; then
                    if ! PYTHONPATH="$python_module_path" "$python_command" -P -m dvdvideo.bluray_chapters \
                        language-args "$playlist" "$playlist_metadata" "$language_args_file" \
                        "${language_pid_args[@]}" < "$stream_metadata_file"; then
                        echo "Playlist $playlist: invalid language/stream metadata; retrying the copy cannot repair it." >&2
                        exit 1
                    fi
                    mapfile -d '' -t language_args < "$language_args_file"
                    if run_copy_command ffmpeg -nostdin -y -xerror -i "$temporary_copy" -f ffmetadata -i "$chapter_metadata" \
                        "${map_args[@]}" -map_chapters 1 "${language_args[@]}" -c copy "$temporary_output"; then
                        copy_valid=true
                    fi
                fi
            fi
            if [[ $copy_valid == true ]] \
                && [[ -s $temporary_output ]] \
                && run_inspection ffprobe -v error "$temporary_output" >/dev/null; then
                break
            fi
            if (( attempt > retries )); then
                echo "Failed to produce a valid copy of playlist $playlist after $attempt attempt(s)." >&2
                if [[ $inspection_timed_out == true ]]; then
                    exit 124
                fi
                exit 1
            fi
            echo "Retrying playlist $playlist (attempt $((attempt + 1)) of $((retries + 1)))..." >&2
        done
        temporary_copy=
        if [[ ${playlist_formats[$playlist]} == m2ts && $chapter_count -gt 0 ]]; then
            publish_output "$playlist_output" "$chapter_sidecar"
        else
            publish_output "$playlist_output" "" "${playlist_formats[$playlist]}"
        fi
    done
    exit 0
fi

if [[ $list_streams == true || $disc_details == true || $list_titles == true ]]; then
    if [[ $# -lt 1 || $# -gt 2 ]]; then
        usage >&2
        exit 2
    fi
else
    if [[ $# -lt 2 || $# -gt 3 ]]; then
        usage >&2
        exit 2
    fi
fi

input=$1
if [[ $input == -* ]]; then
    input=./$input
fi
if [[ -d $input ]]; then
    IFS= read -r -d '' input < <(normalize_directory_path "$input")
    dvd_folder=${input##*/}
    if [[ $dvd_folder != [Vv][Ii][Dd][Ee][Oo]_[Tt][Ss] ]]; then
        set +f
        unset GLOBIGNORE
        shopt -u failglob nocaseglob
        shopt -s nullglob
        dvd_folders=()
        for candidate in "$input"/[Vv][Ii][Dd][Ee][Oo]_[Tt][Ss]; do
            if [[ -d $candidate ]]; then
                dvd_folders+=("$candidate")
            fi
        done
        if [[ ${#dvd_folders[@]} -eq 1 ]]; then
            input=${dvd_folders[0]}
        else
            echo "DVD directory must contain exactly one VIDEO_TS folder (case-insensitive): $input" >&2
            exit 2
        fi
    fi

elif ! [[ -f $input || -b $input ]]; then
    echo "DVD input must be an existing ISO file, block device, or VIDEO_TS directory: $input" >&2
    exit 2
fi

if [[ $list_titles == true || $list_streams == true || $disc_details == true ]]; then
    lsdvd_source=$input
    if [[ -d $input ]]; then
        IFS= read -r -d '' lsdvd_source < <(dirname -z -- "$input")
    fi
fi
if [[ $list_titles == true ]]; then
    if ! command -v lsdvd >/dev/null; then
        echo '--list-titles requires lsdvd.' >&2
        exit 1
    fi
    run_inspection lsdvd "$lsdvd_source"
    exit 0
fi

if [[ $list_streams == true || $disc_details == true ]]; then
    title_or_playlist=${2-1}
    dvd_title_omitted=false
    if [[ $# -eq 1 ]]; then
        dvd_title_omitted=true
    fi
else
    output=$2
    if [[ -z $output ]]; then
        echo 'DVD output path must not be empty.' >&2
        exit 2
    fi
    if [[ $output == */ ]]; then
        echo "DVD output file must not end with '/': $output" >&2
        exit 2
    fi
    if [[ $# -ge 3 && -z $3 ]]; then
        echo 'DVD title number must not be empty.' >&2
        exit 2
    fi
    title_or_playlist=${3-1}

    if [[ $output == -* ]]; then
        output=./$output
    fi
fi

if ! [[ $title_or_playlist =~ ^[1-9][0-9]?$ ]]; then
    echo 'DVD title number must be between 1 and 99.' >&2
    exit 2
fi

if ! command -v ffprobe >/dev/null; then
    echo 'DVD inspection and copy validation require ffprobe.' >&2
    exit 1
fi

if [[ $list_streams == false && $disc_details == false ]]; then
    if ! command -v "$python_command" >/dev/null; then
        echo 'Checking DVD copy space requires python3.' >&2
        exit 1
    fi
    if ! command -v setsid >/dev/null; then
        echo 'Safe cancellation of DVD copy commands requires setsid.' >&2
        exit 1
    fi
    require_commands ffmpeg grep
    capture_inspection ffmpeg -hide_banner -demuxers
    if ! grep -q ' dvdvideo ' <<<"$inspection_output"; then
        echo 'ffmpeg must provide the dvdvideo input format; libdvdnav alone is not sufficient.' >&2
        exit 1
    fi
fi

input_args=(-f dvdvideo -title "$title_or_playlist" -i "$input")

inspect_dvd_title() {
    local title=$1
    shift
    capture_inspection ffprobe -v error -f dvdvideo -title "$title" -i "$input" "$@" || return
    if [[ $disc_details == true ]]; then
        validate_disc_details DVD || return
    fi
    if [[ $list_streams == true ]]; then
        stream_listing=$(
            PYTHONPATH="$python_module_path" "$python_command" -P -m dvdvideo.cli_output \
                <<<"$inspection_output"
        ) || return
        if [[ $stream_listing != *$'\n'* ]]; then
            printf 'No streams found in DVD title %s.\n' "$title" >&2
            return 1
        fi
    fi
}

probe_dvd_inspection() {
    local probe_status alternative
    if inspect_dvd_title "$title_or_playlist" "$@"; then
        return
    else
        probe_status=$?
    fi
    if [[ $dvd_title_omitted != true ]] || (( probe_status >= 124 )); then
        return "$probe_status"
    fi
    if ! command -v lsdvd >/dev/null || ! command -v jq >/dev/null; then
        echo 'Automatic DVD title fallback requires lsdvd and jq; choose a title explicitly.' >&2
        return "$probe_status"
    fi
    capture_inspection lsdvd -Oj "$lsdvd_source" || {
        local discovery_status=$?
        echo 'DVD title discovery failed; choose a title explicitly.' >&2
        return "$discovery_status"
    }
    if ! alternative=$(jq -ser '
        if length != 1 or (.[0] | type) != "object" or (.[0].track | type) != "array" then
            error("lsdvd must return one discovery object with a track array")
        else .[0] end |
        [.track[] | objects | select((.ix | type) == "number" and
            .ix >= 2 and .ix <= 99 and .ix == (.ix | floor) and
            (.length | type) == "number" and .length > 0)] |
        max_by(.length) | .ix | numbers | floor' <<<"$inspection_output"); then
        echo 'Could not find an alternative DVD title with lsdvd; choose a title explicitly.' >&2
        return "$probe_status"
    fi
    printf 'DVD title 1 could not be inspected; trying title %s (longest alternative reported by lsdvd).\n' "$alternative" >&2
    inspect_dvd_title "$alternative" "$@" || return
    printf 'Inspected DVD title %s. Pass that title explicitly when copying it.\n' "$alternative" >&2
}

if [[ $disc_details == true ]]; then
    if ! command -v jq >/dev/null; then
        echo 'DVD details require jq.' >&2
        exit 1
    fi
    require_commands "$python_command"
    probe_dvd_inspection \
        -show_entries 'format=format_long_name,duration,nb_streams:stream=index,codec_type,codec_name,profile,width,height,display_aspect_ratio,avg_frame_rate,channels,channel_layout,sample_rate:stream_tags=language,title:chapter=start_time,end_time' \
        -of json
    printf '%s\n' "$inspection_output"
    exit 0
fi

if [[ $list_streams == true ]]; then
    if ! command -v "$python_command" >/dev/null; then
        echo 'DVD stream listing requires python3.' >&2
        exit 1
    fi
    if [[ $list_streams_tsv == false ]]; then
        require_commands awk
    fi
    if ! PYTHONPATH="$python_module_path" "$python_command" -B -P -c 'import dvdvideo.cli_output' </dev/null; then
        echo 'DVD stream listing requires the dvdvideo.cli_output Python module.' >&2
        exit 1
    fi
    probe_dvd_inspection \
        -show_entries stream=index,codec_type,codec_name,profile,channels,channel_layout:stream_tags=language,title \
        -of json
    if [[ $list_streams_tsv == true ]]; then
        printf '%s\n' "$stream_listing"
    else
        formatted_listing=$(printf '%s\n' "$stream_listing" | awk -F '\t' '
            BEGIN { OFS = "\t" }
            {
                for (i = 1; i <= NF; i++) {
                    if ($i == "") $i = "-"
                }
                print
            }
        ')
        if command -v column >/dev/null; then
            column_locale=${LC_ALL:-${LC_CTYPE:-${LANG:-C}}}
            column_charmap=$(locale charmap 2>/dev/null) || column_charmap=
            if [[ $column_charmap != UTF-8 ]]; then
                column_locale=C.UTF-8
                column_charmap=$(LC_ALL="$column_locale" locale charmap 2>/dev/null) || column_charmap=
            fi
            if [[ $column_charmap != UTF-8 ]]; then
                printf 'Warning: no UTF-8 locale available for column; showing the tab-separated stream table.\n' >&2
            elif aligned_listing=$(LC_ALL="$column_locale" column -t -s $'\t' <<<"$formatted_listing"); then
                printf '%s\n' "$aligned_listing"
                exit 0
            else
                printf 'Warning: column failed; showing the tab-separated stream table.\n' >&2
            fi
        fi
        printf '%s\n' "$formatted_listing"
    fi
    exit 0
fi

if [[ -e $output || -L $output ]]; then
    echo "Refusing to overwrite existing file: $output" >&2
    exit 1
fi

IFS= read -r -d '' output_directory < <(dirname -z -- "$output")
if ! [[ -d $output_directory ]]; then
    echo "Output directory does not exist: $output_directory" >&2
    exit 2
fi
pin_output_directory "$output_directory"
validate_output_filename "$output"

map_args=()
for stream_index in "${selected_streams[@]}"; do
    map_args+=(-map "0:$stream_index")
done

if [[ ${#map_args[@]} -eq 0 ]]; then
    map_args=(-map 0)
fi

make_temporary_workspace "$output_directory"
attempt=0
while true; do
    attempt=$((attempt + 1))
    inspection_timed_out=false
    rm -f "$temporary_output"
    PYTHONPATH="$python_module_path" "$python_command" -P -m dvdvideo.output_space \
        "$workspace_parent" --source "$input"
    if run_copy_command ffmpeg -nostdin -y -xerror "${input_args[@]}" "${map_args[@]}" -c copy "$temporary_output" \
        && [[ -s $temporary_output ]] \
        && run_inspection ffprobe -v error "$temporary_output" >/dev/null; then
        break
    fi
    if (( attempt > retries )); then
        echo "Failed to produce a valid copy of title $title_or_playlist after $attempt attempt(s)." >&2
        if [[ $inspection_timed_out == true ]]; then
            exit 124
        fi
        exit 1
    fi
    echo "Retrying title $title_or_playlist (attempt $((attempt + 1)) of $((retries + 1)))..." >&2
done
publish_output "$output"